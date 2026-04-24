"""Clean old build dirs, log files, and stale enroot containers."""

from .ssh import Session

CLEAN_DIRS = ["/fsx/physai/builds", "/fsx/physai/logs", "/fsx/physai/sync"]


def _clean_files(session: Session, older_than: int, dry_run: bool, force: bool) -> None:
    """Remove old build dirs and log files from /fsx."""
    active = session.run("squeue -u $(whoami) -h -o %i 2>/dev/null || true").split()

    find_args = f"-mtime +{older_than}" if older_than > 0 else ""
    candidates = []
    for d in CLEAN_DIRS:
        out = session.run(
            f"find {d} -mindepth 1 -maxdepth 1 {find_args} 2>/dev/null || true"
        )
        for path in out.splitlines():
            if not path.strip():
                continue
            name = path.strip().rsplit("/", 1)[-1]
            job_id = name.split(".")[0].split("-")[0]
            if job_id in active:
                continue
            candidates.append(path.strip())

    if not candidates:
        print("Nothing to clean.")
        return

    print(f"{'Would remove' if dry_run else 'Will remove'} {len(candidates)} items:")
    for c in candidates:
        print(f"  {c}")

    if dry_run:
        return

    if not force:
        answer = input("\nProceed? [y/N] ")
        if answer.lower() != "y":
            print("Aborted.")
            return

    for c in candidates:
        session.run(f"rm -rf {c}")
    print(f"Removed {len(candidates)} items.")


def _clean_enroot(session: Session, dry_run: bool, force: bool) -> None:
    """Remove stale enroot containers from all nodes in a single srun."""
    # List containers on all nodes
    out = session.run(
        "srun --ntasks-per-node=1 --overlap"
        " bash -c 'for c in $(enroot list 2>/dev/null); do echo $SLURMD_NODENAME:$c; done'"
        " 2>/dev/null || true"
    )

    entries: list[tuple[str, str]] = []
    for line in out.splitlines():
        line = line.strip()
        if ":" in line:
            node, name = line.split(":", 1)
            entries.append((node, name))

    if not entries:
        print("No enroot containers found on any node.")
        return

    print(
        f"{'Would remove' if dry_run else 'Will remove'} {len(entries)} enroot containers:"
    )
    for node, name in entries:
        print(f"  {node}: {name}")

    if dry_run:
        return

    if not force:
        answer = input("\nProceed? [y/N] ")
        if answer.lower() != "y":
            print("Aborted.")
            return

    # Build per-node removal commands
    by_node: dict[str, list[str]] = {}
    for node, name in entries:
        by_node.setdefault(node, []).append(name)

    for node, names in by_node.items():
        rm_cmds = " && ".join(f"enroot remove -f {n}" for n in names)
        session.run(
            f"srun --nodelist={node} --ntasks=1 --overlap"
            f" bash -c '{rm_cmds}' 2>/dev/null || true"
        )
    print(f"Removed {len(entries)} enroot containers.")


def run_clean(
    session: Session,
    older_than: int,
    dry_run: bool,
    force: bool,
    enroot: bool = False,
) -> None:
    if enroot:
        _clean_enroot(session, dry_run, force)
    else:
        _clean_files(session, older_than, dry_run, force)
