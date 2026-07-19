"""Lifecycle-script state checks.

Each check asserts a steady-state property of the cluster that a
lifecycle script set up at boot. See ``docs/en/INFRA.md`` for the
script-to-property mapping (``create_fsx_dirs.sh`` →
``test_fsx_dirs_present``, ``register_slurm_features.sh`` →
``test_slurm_features_registered``, ``install_dcv.sh`` →
``test_dcv_session_running``).
"""

import re

import pytest

from physai_regression.checks._parsing import _LABEL_RE

# Expected layout under /fsx/. dcv-claims is created lazily by the eval
# stage's flock — so it's checked separately.
FSX_DIRS = ["raw", "datasets", "checkpoints", "evaluations", "enroot", "physai"]
DCV_CLAIMS_DIR = "/fsx/physai/dcv-claims"


@pytest.mark.platform
def test_fsx_dirs_present(physai_session) -> None:
    """Every directory create_fsx_dirs.sh + the visual eval flock dir must exist."""
    paths = [f"/fsx/{d}" for d in FSX_DIRS] + [DCV_CLAIMS_DIR]
    out = physai_session.run("stat -c '%n %F' " + " ".join(paths) + " 2>&1 || true")
    found: dict[str, str] = {}
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        # Lines look like:
        #   /fsx/raw directory
        #   stat: cannot statx '/fsx/missing': No such file or directory
        if line.startswith("stat:"):
            m = re.search(r"'([^']+)'", line)
            if m:
                found[m.group(1)] = "missing"
            continue
        parts = line.split(" ", 1)
        if len(parts) == 2:
            found[parts[0]] = parts[1]

    missing = [p for p in paths if found.get(p) != "directory"]
    assert not missing, (
        f"Missing or non-directory FSx paths: {', '.join(missing)}.\n"
        f"stat output:\n{out}"
    )


GPU_FEATURES = {"l40s", "l4", "a10g", "v100", "a100", "h100"}


@pytest.mark.platform
def test_slurm_features_registered(physai_session) -> None:
    """Every GPU node must register its GPU-type feature plus ``dcv``.

    ``register_slurm_features.sh`` writes both. Without it the eval stage's
    ``--constraint=<gpu-type>&dcv`` would never schedule.
    """
    out = physai_session.run("sinfo -h -p gpu -N -o '%N|%f' | sort -u")
    rows = [line.strip() for line in out.splitlines() if line.strip()]
    assert rows, "No nodes returned for gpu partition."

    bad: list[str] = []
    for row in rows:
        node, _, features = row.partition("|")
        feat_set = {f.strip() for f in features.split(",") if f.strip()}
        if "dcv" not in feat_set:
            bad.append(f"{node}: missing 'dcv' feature (got: {features or '(none)'})")
            continue
        gpu = feat_set & GPU_FEATURES
        if not gpu:
            bad.append(
                f"{node}: no GPU-type feature in {sorted(feat_set)} "
                f"(expected one of {sorted(GPU_FEATURES)})"
            )

    assert not bad, "\n".join(bad)


@pytest.mark.platform
def test_slurm_partitions_up(physai_session) -> None:
    """Both gpu and cpu partitions exist with at least one schedulable node."""
    out = physai_session.run("sinfo -h -o '%P|%a|%t|%N' | grep -v '^$' || true")
    rows = [line.strip() for line in out.splitlines() if line.strip()]
    assert rows, "sinfo returned no rows."

    # Strip trailing '*' that sinfo uses to mark the default partition.
    seen: dict[str, list[tuple[str, str, str]]] = {}
    for row in rows:
        part, avail, state, nodes = row.split("|", 3)
        seen.setdefault(part.rstrip("*"), []).append((avail, state, nodes))

    for partition in ("gpu", "cpu"):
        assert partition in seen, f"Partition '{partition}' missing from sinfo."
        # At least one row must be 'up' and in a schedulable state
        # (idle/mix/alloc — drain/down/fail/inval do not count).
        schedulable_states = {"idle", "mix", "alloc"}
        ok = [
            (a, s, n)
            for (a, s, n) in seen[partition]
            if a == "up" and s in schedulable_states
        ]
        assert ok, (
            f"Partition '{partition}' has no schedulable nodes. Rows: {seen[partition]}"
        )


@pytest.mark.platform
def test_dcv_session_running(physai_session) -> None:
    """``dcv list-sessions`` on every GPU node must report a licensed ``console`` session."""
    nodelist = physai_session.run(
        "sinfo -h -p gpu -N -t idle,mix,alloc -o '%N' | sort -u | paste -sd,"
    ).strip()
    assert nodelist, "No schedulable GPU nodes in `gpu` partition."
    node_count = len(nodelist.split(","))

    remote = (
        f"srun --partition gpu --nodelist={nodelist} --ntasks-per-node=1 "
        f"--immediate=30 --label "
        "bash -c '"
        "h=$(hostname); "
        # `dcv list-sessions` exits 0 even with no sessions — match the
        # console line ourselves.
        "out=$(sudo dcv list-sessions 2>&1 || true); "
        'if echo "$out" | grep -E "Session: .console.|^console" >/dev/null; '
        'then echo "$h:OK"; '
        'else echo "$h:MISSING:$(echo "$out" | tr -d "\\n" | head -c 120)"; '
        "fi"
        "'"
    )
    output = physai_session.run(remote)

    statuses: dict[str, str] = {}
    for line in output.splitlines():
        line = _LABEL_RE.sub("", line).strip()
        if not line or ":" not in line:
            continue
        host, _, status = line.partition(":")
        statuses[host] = status

    missing = sorted((h, st) for h, st in statuses.items() if not st.startswith("OK"))
    reported = len(statuses)
    assert reported == node_count, (
        f"Expected {node_count} GPU node reports, got {reported}.\n"
        f"Raw output:\n{output}"
    )
    assert not missing, "DCV `console` session missing on:\n" + "\n".join(
        f"  {h}: {st}" for h, st in missing
    )
