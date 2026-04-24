"""Data commands: ls, upload."""

from pathlib import Path

from .ssh import Session

CATEGORIES = ["raw", "datasets", "checkpoints"]


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
            "  aws s3 cp --recursive <path> s3://<data-bucket>/raw/\n"
        )
        answer = input(f"Proceed with rsync to {session.host}:/fsx/raw/ ? [y/N] ")
        if answer.lower() != "y":
            print("Aborted.")
            return

    dst = f"/fsx/{category}/"
    print(f"Uploading {src} → {session.host}:{dst}")
    session.rsync(str(src), dst)
