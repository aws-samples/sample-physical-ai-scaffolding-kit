"""`physai doctor` — cluster health checks with interactive fixes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from .ssh import Session

FSX_DIRS: dict[str, str] = {
    "raw": "777",
    "datasets": "777",
    "checkpoints": "777",
    "evaluations": "777",
    "enroot": "1777",  # sticky; set by install_enroot_pyxis.sh so users can't
    # remove each other's named containers
    "physai": "777",
}
CONF_CACHE_FILES = [
    "slurm.conf",
    "cgroup.conf",
    "plugstack.conf",
    "gres.conf",
    "accounting.conf",
]


@dataclass
class CheckResult:
    status: Literal["PASS", "FAIL", "WARN"]
    message: str = ""


@dataclass
class Check:
    name: str
    run: Callable[[Session], CheckResult]
    fix: Callable[[Session], None] | None = None


# ── FSx directories ──


def check_fsx_dirs(session: Session) -> CheckResult:
    paths = " ".join(f"/fsx/{d}" for d in FSX_DIRS)
    # stat prints one line per arg; `2>&1` so missing dirs still produce a line.
    try:
        out = session.run(f"stat -c '%a %F %n' {paths} 2>&1 || true")
    except RuntimeError as e:
        return CheckResult("FAIL", f"stat failed: {e}")
    bad: list[str] = []
    lines = out.splitlines()
    for (d, expected_mode), line in zip(FSX_DIRS.items(), lines):
        path = f"/fsx/{d}"
        if "No such file" in line:
            bad.append(f"{path}: missing")
            continue
        parts = line.split(" ", 2)
        if len(parts) < 3 or parts[2] != path:
            bad.append(f"{path}: unexpected stat output: {line!r}")
            continue
        mode, ftype = parts[0], parts[1]
        if ftype != "directory":
            bad.append(f"{path}: not a directory ({ftype})")
        elif mode != expected_mode:
            bad.append(f"{path}: mode is {mode}, expected {expected_mode}")
    if bad:
        return CheckResult("FAIL", "\n       ".join(bad))
    return CheckResult("PASS")


def fix_fsx_dirs(session: Session) -> None:
    # Ensure every dir exists, then set its expected mode.
    cmds = [f"mkdir -p /fsx/{d}" for d in FSX_DIRS]
    cmds += [f"chmod 0{mode} /fsx/{d}" for d, mode in FSX_DIRS.items()]
    session.run(" && ".join(cmds))


# ── Slurm config drift among workers ──


def check_slurm_conf_drift(session: Session) -> CheckResult:
    try:
        nodes_out = session.run('sinfo -h -o "%N" -N | sort -u')
    except RuntimeError as e:
        return CheckResult("FAIL", f"sinfo failed: {e}")
    nodes = [n.strip() for n in nodes_out.splitlines() if n.strip()]
    if not nodes:
        return CheckResult("WARN", "no nodes returned by sinfo")

    files = " ".join(f"/var/spool/slurmd/conf-cache/{f}" for f in CONF_CACHE_FILES)
    hashes: dict[str, tuple[str, ...]] = {}
    unreachable: list[str] = []
    for node in nodes:
        try:
            # --overlap lets us run on a node alongside any running job; short
            # time limit so a non-responsive slurmd doesn't hang the check.
            out = session.run(
                f"srun -N1 -w {node} --overlap --time=00:00:30 "
                f"md5sum {files} 2>/dev/null"
            )
        except RuntimeError:
            unreachable.append(node)
            continue
        # Each line: "<hash>  <path>". Collect in CONF_CACHE_FILES order.
        by_path = {}
        for line in out.splitlines():
            h, _, p = line.partition("  ")
            by_path[p.strip()] = h.strip()
        try:
            hashes[node] = tuple(
                by_path[f"/var/spool/slurmd/conf-cache/{f}"] for f in CONF_CACHE_FILES
            )
        except KeyError:
            unreachable.append(node)

    if not hashes:
        return CheckResult(
            "WARN", f"could not reach any node to hash conf-cache: {unreachable}"
        )

    distinct = set(hashes.values())
    msg_parts: list[str] = []
    if unreachable:
        msg_parts.append(f"unreachable nodes: {', '.join(unreachable)}")

    if len(distinct) == 1:
        if msg_parts:
            return CheckResult("WARN", "; ".join(msg_parts))
        return CheckResult("PASS")

    # Group nodes by hash-tuple. Majority group is in sync; minority groups
    # are drifting.
    groups: dict[tuple[str, ...], list[str]] = {}
    for node, h in hashes.items():
        groups.setdefault(h, []).append(node)
    majority = max(groups.values(), key=len)
    for g_nodes in groups.values():
        if g_nodes is majority:
            continue
        msg_parts.append(
            f"drifting nodes (differ from majority): {', '.join(sorted(g_nodes))}"
        )
    return CheckResult("FAIL", "; ".join(msg_parts))


def fix_slurm_reconfigure(session: Session) -> None:
    session.run("scontrol reconfigure")


# ── slurmdbd reachable ──


def check_slurmdbd(session: Session) -> CheckResult:
    try:
        session.run("sacct -n --parsable2 -S now-1hour -o JobID")
    except RuntimeError as e:
        return CheckResult(
            "FAIL",
            f"{e}\n       "
            "Things to check:\n"
            "       • RDS instance in the AWS console (PhysaiInfraStack > SlurmDB).\n"
            "       • slurmdbd on the controller: "
            "`aws ssm start-session --target <controller-instance-id>`, "
            "then `systemctl status slurmdbd`.\n"
            "       • Slurm DB credentials in Secrets Manager "
            "(PhysaiInfraStack > slurm-db-credentials).",
        )
    return CheckResult("PASS")


# ── Runner ──

CHECKS: list[Check] = [
    Check("FSx directories", check_fsx_dirs, fix_fsx_dirs),
    Check(
        "Slurm config drift among workers",
        check_slurm_conf_drift,
        fix_slurm_reconfigure,
    ),
    Check("slurmdbd reachable", check_slurmdbd),
]


def _prompt_yes_no(question: str) -> bool:
    """Return True if the user confirms. Defaults to No on blank / EOF."""
    try:
        ans = input(f"{question} [y/N]: ").strip().lower()
    except EOFError:
        print()  # newline so next output doesn't glue to prompt
        return False
    return ans in ("y", "yes")


def _print(name: str, result: CheckResult) -> None:
    tag = {"PASS": "PASS", "FAIL": "FAIL", "WARN": "WARN"}[result.status]
    line = f"[{tag}] {name}"
    if result.message:
        line += f"\n       {result.message}"
    print(line)


def run_doctor(session: Session) -> None:
    """Run all checks. Offer per-check fixes interactively. Exit 1 on any FAIL."""
    any_fail = False
    for check in CHECKS:
        result = check.run(session)
        _print(check.name, result)

        if (
            result.status == "FAIL"
            and check.fix is not None
            and _prompt_yes_no(f"Apply fix for '{check.name}'?")
        ):
            try:
                check.fix(session)
            except RuntimeError as e:
                print(f"       Fix failed: {e}")
                any_fail = True
                continue
            result = check.run(session)
            _print(f"{check.name} (after fix)", result)

        if result.status == "FAIL":
            any_fail = True

    total = len(CHECKS)
    if any_fail:
        print("\nSome checks failed.")
        raise SystemExit(1)
    print(f"\nAll {total} checks passed.")
