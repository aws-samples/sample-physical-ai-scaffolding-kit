"""Clean old build dirs and log files."""

from .ssh import Session

CLEAN_DIRS = ["/fsx/physai/builds", "/fsx/physai/logs"]


def run_clean(session: Session, older_than: int, dry_run: bool, force: bool) -> None:
    # Get active job IDs to protect their files
    active = session.run("squeue -u $(whoami) -h -o %i 2>/dev/null || true").split()

    # Find candidates
    find_args = f"-mtime +{older_than}" if older_than > 0 else ""
    candidates = []
    for d in CLEAN_DIRS:
        out = session.run(
            f"find {d} -mindepth 1 -maxdepth 1 {find_args} 2>/dev/null || true"
        )
        for path in out.splitlines():
            if not path.strip():
                continue
            # Protect files belonging to active jobs
            name = path.strip().rsplit("/", 1)[-1]
            job_id = name.split(".")[0].split("-")[
                0
            ]  # extract job id from "174.out" or "name-ts"
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
