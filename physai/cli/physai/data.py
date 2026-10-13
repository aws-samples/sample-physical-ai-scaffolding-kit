"""Data commands: ls, upload, rm."""

from pathlib import Path, PurePosixPath

from .pipeline import Dir, File, _find_active_job_producing, _validate_artifact_name
from .ssh import Session

CATEGORIES = ["raw", "datasets", "checkpoints"]
# rm also accepts evaluations (produced by the eval stage; users never upload
# these but may want to clean them).
RM_CATEGORIES = [*CATEGORIES, "evaluations"]
# save only accepts artifacts that the pipeline produces and that the user
# would meaningfully want to archive to S3. Raw is excluded — it already lives
# in s3://<bucket>/raw/ via the DRA, so copying /fsx/raw/ back would be a loop.
SAVE_CATEGORIES = ["checkpoints"]


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
            "  aws s3 cp --recursive \\\n"
            "    --metadata file-owner=1000,file-group=1000 \\\n"
            "    <local-dir>/ s3://<data-bucket>/raw/<name>/\n"
            "\n"
            "  --metadata sets POSIX ownership on imported files to ubuntu\n"
            "  (UID/GID 1000 on HyperPod nodes). Without it, FSx imports\n"
            "  objects as root:root mode 755 — readable for jobs, but not\n"
            "  deletable via `physai rm`. (The intermediate directory created\n"
            "  by DRA is still root-owned; clean up via `aws s3 rm` or `sudo\n"
            "  rm` on the login node.)\n"
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

    try:
        session.run(f"rm -rf {path}")
    except RuntimeError as e:
        msg = str(e)
        if "Permission denied" in msg:
            raise SystemExit(
                f"`rm -rf {path}` failed: {msg}\n"
                "\n"
                "Hint: files under /fsx/ may be owned by root if they were imported\n"
                "from S3 via the FSx DRA without POSIX metadata. The unprivileged\n"
                "ssh user can't unlink them.\n"
                "\n"
                f"To remove anyway, ssh to the login node and run:  sudo rm -rf {path}"
            )
        raise
    print(f"Removed {path}")


def _find_active_save_job(session: Session, category: str, name: str) -> str | None:
    """Return the job ID of an in-flight `physai/save/<category>/<name>` job, or None."""
    out = session.run(
        f'squeue -h -o "%i" -n "physai/save/{category}/{name}"'
    )
    ids = [line.strip() for line in out.splitlines() if line.strip()]
    return max(ids, key=int) if ids else None


def _save_sbatch(
    src: PurePosixPath,
    dst: str,
    job_name: str,
    dry_run: bool,
    delete: bool,
) -> str:
    """sbatch script that runs `aws s3 cp --recursive` on a cpu worker.

    With `delete=True`, appends `rm -rf` after a successful copy. `set -eo
    pipefail` guarantees the rm only runs if the copy exited 0. `--dry-run`
    suppresses the rm.
    """
    dryrun_flag = " --dryrun" if dry_run else ""
    lines = [
        "#!/bin/bash",
        f"#SBATCH --job-name={job_name}",
        f'#SBATCH --comment="src={src}/ dst={dst}"',
        "#SBATCH --partition=cpu",
        "#SBATCH --output=/fsx/physai/logs/%j.out",
        "set -eo pipefail",
        "SECONDS=0",
        f'echo "aws s3 cp --recursive {src}/ {dst}"',
        f"aws s3 cp --recursive{dryrun_flag} {src}/ {dst}",
        f'echo "Saved {src} → {dst} (${{SECONDS}}s)"',
    ]
    if delete and not dry_run:
        lines += [
            f'echo "Removing {src} (--delete)"',
            f"rm -rf {src}",
            f'echo "Removed {src}"',
        ]
    return "\n".join(lines) + "\n"


def save(
    session: Session,
    category: str,
    name: str,
    data_bucket: str | None,
    force: bool = False,
    dry_run: bool = False,
    submit_job: bool = False,
    stream: bool = True,
    delete: bool = False,
) -> None:
    """Copy /fsx/<category>/<name>/ to s3://<data_bucket>/<category>/<name>/.

    Two execution modes:
      - Default: runs `aws s3 cp --recursive` on the login node with live
        progress streaming. Fine for smaller checkpoints (~< 10 GB) where the
        operator wants to watch it finish.
      - `submit_job=True`: submits an sbatch job on the `cpu` partition and
        returns the job id, so 20+ GB transfers don't tie up the local shell.
        With `stream=True` (default), `physai save` tails the job log via
        `session.stream_log(job_id)` — Ctrl-C detaches without killing the job.

    With `delete=True`, the source `/fsx/<category>/<name>/` is `rm -rf`ed
    after a successful copy. `set -eo pipefail` in the sbatch script (and the
    Python-side `try/except` in inline mode) guarantee delete only runs when
    the copy succeeds. `--dry-run` suppresses the delete.

    In both modes the target prefix lives beside the DRA-linked `raw/` prefix
    but is not itself linked, so it won't lazy-import back into /fsx/.

    Safety:
      - Rejects categories outside SAVE_CATEGORIES.
      - Rejects `name` values with a slash / whitespace / '.' / '..'.
      - Requires `data_bucket` (set in ~/.physai/config.yaml).
      - Requires that /fsx/<category>/<name>/ exists as a directory.
      - Refuses if an active pipeline job is producing it (avoids uploading a
        half-written checkpoint) or if another `physai save` job is already
        in flight for the same (category, name).
      - Prompts with size + destination unless --force. If the destination
        prefix is non-empty, warns and requires explicit confirmation.
    """
    if category not in SAVE_CATEGORIES:
        raise SystemExit(
            f"Cannot save category '{category}'. "
            f"Valid: {', '.join(SAVE_CATEGORIES)}"
        )
    _validate_artifact_name(name, category)

    if not data_bucket:
        raise SystemExit(
            "No 'data_bucket' configured. Set it in ~/.physai/config.yaml.\n"
            "\n"
            "Look up the bucket name with:\n"
            "  aws cloudformation describe-stacks --stack-name PhysaiInfraStack \\\n"
            "    --query 'Stacks[0].Outputs[?OutputKey==`DataBucketName`].OutputValue' \\\n"
            "    --output text"
        )

    src = PurePosixPath(f"/fsx/{category}/{name}")
    artifact = Dir(src)

    try:
        kind = session.run(
            f"if [ -d {src} ]; then echo dir; "
            f"elif [ -f {src} ]; then echo file; "
            f"else echo none; fi"
        )
    except RuntimeError as e:
        raise SystemExit(f"Cannot reach cluster: {e}")

    if kind == "none":
        raise SystemExit(f"Nothing to save at {src}")
    if kind != "dir":
        raise SystemExit(f"{src} is not a directory; save only supports directories")

    active = _find_active_job_producing(session, artifact)
    if active:
        raise SystemExit(
            f"Active job {active} is producing {src}. Cancel it or wait for it "
            f"to finish before saving (avoids uploading a partial checkpoint)."
        )

    active_save = _find_active_save_job(session, category, name)
    if active_save:
        raise SystemExit(
            f"Save job {active_save} is already in flight for {category}/{name}. "
            f"Wait for it or cancel it (`physai cancel {active_save}`) before retrying."
        )

    dst = f"s3://{data_bucket}/{category}/{name}/"
    size = session.run(f"du -sh {src} 2>/dev/null | cut -f1").strip() or "?"

    existing = ""
    try:
        existing = session.run(
            f"aws s3 ls {dst} --recursive --summarize 2>/dev/null "
            "| tail -n 2 || true"
        ).strip()
    except RuntimeError:
        existing = ""

    if not force:
        print(f"About to save: {src}  ({size})")
        print(f"           to: {dst}")
        if submit_job:
            print("  Mode: sbatch job on partition 'cpu'")
        if delete:
            if dry_run:
                print("  --delete: source would be removed after copy (skipped by --dry-run)")
            else:
                print(f"  --delete: {src} will be removed after a successful copy")
        if existing:
            print(
                "  Note: destination prefix already contains objects — they may be\n"
                "        overwritten. Existing summary:"
            )
            for line in existing.splitlines():
                print(f"    {line}")
        if dry_run:
            print("  (--dry-run: no objects will be copied)")
        answer = input("Proceed? [y/N] ")
        if answer.lower() != "y":
            print("Aborted.")
            return

    if submit_job:
        job_name = f"physai/save/{category}/{name}"
        sbatch_path = f"/fsx/physai/jobs/save-{category}-{name}.sbatch"
        session.run("mkdir -p /fsx/physai/logs /fsx/physai/jobs")
        session.write_file(
            sbatch_path, _save_sbatch(src, dst, job_name, dry_run, delete)
        )
        job_id = session.run(f"sbatch --parsable {sbatch_path}")
        print(f"Submitted save job {job_id} for {category}/{name}")
        if not stream:
            print(f"Reconnect: physai logs {job_id}")
            return
        print(f"Reconnect: physai logs {job_id}", flush=True)
        session.stream_log(job_id)
        return

    cmd = f"aws s3 cp --recursive {src}/ {dst}"
    if dry_run:
        cmd += " --dryrun"
    print(f"Running on {session.host}: {cmd}")
    try:
        session.run_streaming(cmd)
    except RuntimeError as e:
        raise SystemExit(f"aws s3 cp failed: {e}")
    print(f"Saved {src} → {dst}")

    if delete and not dry_run:
        print(f"Removing {src} (--delete)")
        try:
            session.run(f"rm -rf {src}")
        except RuntimeError as e:
            msg = str(e)
            if "Permission denied" in msg:
                raise SystemExit(
                    f"S3 copy succeeded but `rm -rf {src}` failed: {msg}\n"
                    "\n"
                    "The FSx copy could not be removed by the ssh user. "
                    "The archive is safe in S3.\n"
                    f"To clean up manually: ssh to the login node and run  sudo rm -rf {src}"
                )
            raise SystemExit(f"S3 copy succeeded but rm -rf failed: {msg}")
        print(f"Removed {src}")
