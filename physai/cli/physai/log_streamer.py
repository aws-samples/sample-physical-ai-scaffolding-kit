"""Stream a Slurm job's log file from the beginning. Exits when the job completes."""

import subprocess
import sys
import time
from pathlib import Path

LOG_DIR = "/fsx/physai/logs"


def job_is_active(job_id: str) -> bool:
    r = subprocess.run(
        ["squeue", "-j", job_id, "-h"],
        capture_output=True,
        text=True,
        check=False,
    )
    return bool(r.stdout.strip())


def stream(job_id: str) -> None:
    log_path = Path(LOG_DIR) / f"{job_id}.out"

    # Wait for log file to appear
    for _ in range(30):
        if log_path.exists():
            break
        if not job_is_active(job_id):
            if log_path.exists():
                break
            print(f"Job {job_id} finished but no log file found.", file=sys.stderr)
            sys.exit(1)
        time.sleep(1)
    else:
        print(f"Log file not found: {log_path}", file=sys.stderr)
        sys.exit(1)

    with open(log_path, "rb") as f:
        while True:
            line = f.readline()
            if line:
                sys.stdout.buffer.write(line)
                sys.stdout.buffer.flush()
            else:
                if not job_is_active(job_id):
                    rest = f.read()
                    if rest:
                        sys.stdout.buffer.write(rest)
                        sys.stdout.buffer.flush()
                    break
                time.sleep(0.5)


if __name__ == "__main__":
    stream(sys.argv[1])
