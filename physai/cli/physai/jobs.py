"""Job management: list, status, logs, cancel via Slurm."""

from .ssh import Session

# squeue fields: JobID | JobName | State | SubmitTime | StartTime | Elapsed | Comment
SQUEUE_FORMAT = '"%i|%j|%T|%V|%S|%M|%k"'
# sacct fields in the same order.
SACCT_FORMAT = "JobID,JobName%40,State,Submit,Start,Elapsed,Comment%60"


def _parse_job_name(job_name: str) -> tuple[str, str]:
    """Parse physai/<type>/<name> into (type, name)."""
    parts = job_name.split("/", 2)
    if len(parts) == 3 and parts[0] == "physai":
        return parts[1], parts[2]
    return "?", job_name


def _fmt_time(t: str) -> str:
    """Slurm times look like 'YYYY-MM-DDTHH:MM:SS'. Shorten to 'MM-DD HH:MM:SS'.

    Non-standard values ('Unknown', 'N/A', empty) are returned unchanged.
    """
    if len(t) >= 19 and t[4] == "-" and t[10] == "T":
        return f"{t[5:10]} {t[11:19]}"
    return t


def list_jobs(session: Session) -> None:
    """List physai jobs.

    `TZ=UTC` is prepended so Slurm formats SubmitTime/StartTime in UTC
    regardless of the cluster's or the user's local timezone.
    """
    out = session.run(f"TZ=UTC squeue -u $(whoami) --format={SQUEUE_FORMAT} --noheader")
    active = []
    for line in out.splitlines():
        parts = line.strip().strip('"').split("|", 6)
        if len(parts) < 7:
            continue
        job_id, job_name, state, submit, start, elapsed, comment = parts
        if not job_name.startswith("physai/"):
            continue
        jtype, name = _parse_job_name(job_name)
        active.append(
            (
                job_id,
                jtype,
                name,
                state,
                _fmt_time(submit),
                _fmt_time(start),
                elapsed,
                comment,
            )
        )

    completed = []
    active_jobs = {x[0] for x in active}
    if session.has_sacct:
        out = session.run(
            f"TZ=UTC sacct -u $(whoami) --format={SACCT_FORMAT} --noheader --parsable2 -S now-7days"
        )
        for line in out.splitlines():
            parts = line.split("|", 6)
            if len(parts) < 7:
                continue
            job_id, job_name, state, submit, start, elapsed, comment = parts
            if not job_name.startswith("physai/") or job_id in active_jobs:
                continue
            if "." in job_id:
                continue
            jtype, name = _parse_job_name(job_name)
            completed.append(
                (
                    job_id,
                    jtype,
                    name,
                    state,
                    _fmt_time(submit),
                    _fmt_time(start),
                    elapsed,
                    comment,
                )
            )

    if not active and not completed:
        print("No physai jobs found.")
        return

    # Sort most-recent-first by numeric job id. Job IDs are monotonic within a
    # cluster, so this is a stable proxy for submission order and works even
    # when start/submit times are N/A (e.g., pending jobs).
    rows = sorted(active + completed, key=lambda r: int(r[0]), reverse=True)

    header = (
        f"{'JOB_ID':<8} {'TYPE':<7} {'NAME':<30} {'STATE':<12} "
        f"{'SUBMIT (UTC)':<15} {'START (UTC)':<15} {'ELAPSED':<10} COMMENT"
    )
    print(header)
    for row in rows:
        job_id, jtype, name, state, submit, start, elapsed, comment = row
        print(
            f"{job_id:<8} {jtype:<7} {name:<30} {state:<12} "
            f"{submit:<15} {start:<15} {elapsed:<10} {comment}"
        )

    if not session.has_sacct and not completed:
        print("\n(sacct not available — only active jobs shown)")


def status_job(session: Session, job_id: str) -> None:
    """Show status of a specific job."""
    out = session.run(
        f"TZ=UTC squeue -j {job_id} --format={SQUEUE_FORMAT} --noheader 2>/dev/null || true"
    )
    if out.strip():
        parts = out.strip().strip('"').split("|", 4)
        if len(parts) >= 5:
            _, job_name, state, elapsed, comment = parts
            jtype, name = _parse_job_name(job_name)
            print(f"Job:     {job_id}")
            print(f"Type:    {jtype}")
            print(f"Name:    {name}")
            print(f"State:   {state}")
            print(f"Elapsed: {elapsed}")
            print(f"Comment: {comment}")
            print(f"Log:     /fsx/physai/logs/{job_id}.out")
            return

    if session.has_sacct:
        out = session.run(
            f"TZ=UTC sacct -j {job_id} --format=JobID,JobName%40,State,Elapsed,Start,End,NodeList,Comment%60 --noheader --parsable2 2>/dev/null || true"
        )
        for line in out.splitlines():
            parts = line.split("|")
            if len(parts) >= 8 and "." not in parts[0]:
                _, job_name, state, elapsed, start, end, node, comment = parts[:8]
                jtype, name = _parse_job_name(job_name)
                print(f"Job:     {job_id}")
                print(f"Type:    {jtype}")
                print(f"Name:    {name}")
                print(f"State:   {state}")
                print(f"Elapsed: {elapsed}")
                print(f"Start:   {start}")
                print(f"End:     {end}")
                print(f"Node:    {node}")
                print(f"Comment: {comment}")
                print(f"Log:     /fsx/physai/logs/{job_id}.out")
                return

    print(f"Job {job_id} not found in queue.")
    print(f"Log file: /fsx/physai/logs/{job_id}.out")
    print(f"Use: physai logs {job_id}")


def logs_job(session: Session, job_id: str) -> None:
    """Tail the log of a job."""
    session.stream_log(job_id)


def cancel_job(session: Session, job_id: str) -> None:
    """Cancel a job."""
    session.run(f"scancel {job_id}")
    print(f"Cancelled job {job_id}")
