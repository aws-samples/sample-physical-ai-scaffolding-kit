"""Data commands: ls, upload, rm."""

from pathlib import Path, PurePosixPath

from .pipeline import Dir, File, _find_active_job_producing, _validate_artifact_name
from .ssh import Session

CATEGORIES = ["raw", "datasets", "checkpoints"]
# rm also accepts evaluations (produced by the eval stage; users never upload
# these but may want to clean them).
RM_CATEGORIES = [*CATEGORIES, "evaluations"]


def _check_category(category: str) -> None:
    if category not in CATEGORIES:
        raise SystemExit(
            f"Unknown category '{category}'. Valid: {', '.join(CATEGORIES)}"
        )


def ls(session: Session, category: str, path: str | None = None) -> None:
    """List remote data on the cluster with accurate sizes (du -sh for each entry)."""
    _check_category(category)
    target = f"/fsx/{category}"
    if path:
        target = f"{target}/{path.lstrip('/')}"
    # `du -sh` on each top-level entry gives correct sizes for both files and
    # directories. `ls -l` reports directory inode size (~32K) instead of content.
    out = session.run(
        f"cd {target} 2>/dev/null && "
        "find . -mindepth 1 -maxdepth 1 -exec du -sh {} + 2>/dev/null | "
        "sed 's|\\t\\./|\\t|' | sort -k2"
        " || true"
    )
    if not out.strip():
        print(f"(empty) {target}")
        return
    for line in out.splitlines():
        size, _, name = line.partition("\t")
        if name:
            print(f"{name:<40} {size}")


def upload(session: Session, category: str, local_path: str) -> None:
    """Upload data to /fsx/<category>/ via rsync.

    For `raw`, recommend uploading to S3 first (DRA auto-imports).
    """
    _check_category(category)
    src = Path(local_path).expanduser().resolve()
    if not src.exists():
        raise SystemExit(f"Path not found: {src}")

    if category == "raw":
        print(
            "Recommendation: upload raw data to S3 instead — the Data Repository\n"
            "Association will auto-import it to /fsx/raw/ on first access.\n"
            "Raw data is expected to be a directory of demo files (e.g., HDF5).\n"
            "  aws s3 cp --recursive <local-dir>/ s3://<data-bucket>/raw/<name>/\n"
        )
        answer = input(f"Proceed with rsync to {session.host}:/fsx/raw/ ? [y/N] ")
        if answer.lower() != "y":
            print("Aborted.")
            return

    dst = f"/fsx/{category}/"
    print(f"Uploading {src} → {session.host}:{dst}")
    session.rsync(str(src), dst, show_progress=True)


def rm(session: Session, category: str, name: str, force: bool = False) -> None:
    """Remove a named artifact from /fsx/<category>/<name> on the cluster.

    Safety:
      - Refuses categories outside RM_CATEGORIES.
      - Refuses `name` values that contain a slash, whitespace, or are '.'/'..'
        (no path escapes; no comment-breaking whitespace).
      - Refuses if no artifact exists at the resolved path.
      - Refuses if an active pipeline job is producing the artifact.
      - Prompts with size and type unless --force.
      - For `raw`, warns that /fsx/raw/ is a DRA cache from S3: removal only
        evicts the local copy; S3 objects under s3://<bucket>/raw/ are
        unaffected and will lazy-re-import on next access.
    """
    if category not in RM_CATEGORIES:
        raise SystemExit(
            f"Unknown category '{category}'. Valid: {', '.join(RM_CATEGORIES)}"
        )
    _validate_artifact_name(name, category)

    path = PurePosixPath(f"/fsx/{category}/{name}")

    # Probe what's actually at that path.
    try:
        kind = session.run(
            f"if [ -d {path} ]; then echo dir; "
            f"elif [ -f {path} ]; then echo file; "
            f"else echo none; fi"
        )
    except RuntimeError as e:
        raise SystemExit(f"Cannot reach cluster: {e}")

    if kind == "none":
        raise SystemExit(f"Nothing to remove at {path}")

    artifact = Dir(path) if kind == "dir" else File(path)
    active = _find_active_job_producing(session, artifact)
    if active:
        raise SystemExit(
            f"Active job {active} is producing {path}. Cancel it first "
            f"(`physai cancel {active}`) before removing."
        )

    # Get a human-readable size for the prompt.
    size = session.run(f"du -sh {path} 2>/dev/null | cut -f1").strip() or "?"

    if not force:
        kind_label = "directory" if kind == "dir" else "file"
        print(f"About to remove {kind_label}: {path}  ({size})")
        if category == "raw":
            print(
                "  Note: /fsx/raw/ is a DRA cache from S3. Removing here only "
                "evicts the\n        local copy; s3://<bucket>/raw/ is untouched "
                "and will lazy-re-import\n        on next access."
            )
        answer = input("Proceed? [y/N] ")
        if answer.lower() != "y":
            print("Aborted.")
            return

    session.run(f"rm -rf {path}")
    print(f"Removed {path}")
