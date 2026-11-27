"""``physai`` CLI surface checks.

These exercise the developer-facing CLI through a real subprocess so we
catch breakage in CLI argparse wiring, doctor checks, and Slurm
list/status/logs plumbing. The CLI talks to the cluster through the
regression's ``--ssh-config`` tempfile — ``~/.ssh/config`` is not used.
"""

import shlex

import pytest


@pytest.mark.platform
def test_doctor_passes(physai_cli) -> None:
    """``physai doctor`` exits 0 against a healthy fresh cluster.

    Asserting exit 0 alone is too weak: ``physai doctor`` exits non-zero
    only on FAIL, and some checks degrade to WARN (e.g. when they can't
    reach a node) without failing. So we also assert that the checks which
    actually examine the cluster reported ``[PASS]`` — proving the run
    inspected real cluster state rather than warning its way to a 0 exit.
    """
    r = physai_cli.run("doctor", check=False)
    assert r.returncode == 0, (
        f"`physai doctor` exited {r.returncode}.\n"
        f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}"
    )
    for expected in ("[PASS] FSx directories", "[PASS] slurmdbd reachable"):
        assert expected in r.stdout, (
            f"`physai doctor` did not report {expected!r} — the check may have "
            f"WARNed instead of examining the cluster.\nstdout:\n{r.stdout}"
        )


# Trivial Slurm job that finishes in seconds so the list/status/logs flow
# can observe it without long waits.
_TRIVIAL_SBATCH = """\
#!/bin/bash
#SBATCH --job-name=physai/run/regression-list-status/trivial
#SBATCH --partition=cpu
#SBATCH --output=/fsx/physai/logs/%j.out
echo "regression-list-status: hello"
sleep 1
"""


@pytest.mark.platform
def test_list_status_logs(physai_session, physai_cli) -> None:
    """Submit a trivial ``physai/run/...`` job, then exercise list/status/logs."""
    physai_session.run("mkdir -p /fsx/physai/logs")
    # mktemp creates a fresh file only the current user can write, so another
    # user of the shared login node cannot swap the job script before sbatch.
    sbatch_path = physai_session.run("mktemp --suffix=.sbatch").strip()
    try:
        physai_session.write_file(sbatch_path, _TRIVIAL_SBATCH)
        job_id = physai_session.run(
            f"sbatch --parsable {shlex.quote(sbatch_path)}"
        ).strip()
    finally:
        physai_session.run(f"rm -f {shlex.quote(sbatch_path)}")
    assert job_id.isdigit(), f"sbatch did not return a job id: {job_id!r}"

    try:
        # `physai list` should mention the job (ours starts with `physai/run/`).
        r_list = physai_cli.run("list")
        assert job_id in r_list.stdout, (
            f"`physai list` did not include job {job_id}.\nstdout:\n{r_list.stdout}"
        )

        # `physai status <id>` should print at least the Job header and the
        # log path. Don't assert on State — could be PENDING, RUNNING, or
        # COMPLETED depending on how fast the scheduler moves.
        r_status = physai_cli.run("status", job_id)
        assert f"Job:     {job_id}" in r_status.stdout, (
            f"`physai status {job_id}` malformed:\n{r_status.stdout}"
        )
        assert "/fsx/physai/logs/" in r_status.stdout, (
            f"`physai status {job_id}` did not name the log path:\n{r_status.stdout}"
        )

        # Wait for COMPLETED so the streamer's "exit when job finishes"
        # branch fires promptly instead of tailing.
        physai_session.run(
            f"for i in $(seq 1 60); do "
            f"st=$(squeue -h -j {job_id} -o %T 2>/dev/null || true); "
            f'if [ -z "$st" ]; then break; fi; sleep 1; done'
        )

        # `physai logs <id>` runs the streamer end-to-end (waits for the
        # log file to appear, reads to EOF, exits when job is no longer
        # active). On a finished job, it prints the full log and returns.
        r_logs = physai_cli.run("logs", job_id)
        assert "regression-list-status: hello" in r_logs.stdout, (
            f"`physai logs {job_id}` did not include the expected line.\n"
            f"stdout:\n{r_logs.stdout}\nstderr:\n{r_logs.stderr}"
        )
    finally:
        # Best-effort cleanup. `scancel` on a finished job is a no-op.
        physai_session.run(f"scancel {job_id} 2>/dev/null || true")
        physai_session.run(f"rm -f /fsx/physai/logs/{job_id}.out")
