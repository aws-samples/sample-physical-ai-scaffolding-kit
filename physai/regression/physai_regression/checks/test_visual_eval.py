"""Visual-evaluation host-context check.

Submits ``physai eval --visual`` against the fake-evaluator. The fake
``eval.sh`` doesn't render anything, but the sbatch wrapper still runs
the real flock + ``dcv_session_setup.sh`` + ``dcv_session_teardown.sh``
plumbing — which is what we're regressing against. End-to-end frame
capture (loading ``https://localhost:8443/`` and verifying frames
render) requires a human and is out of scope here.
"""

import json

import pytest

from physai_regression.checks._parsing import _extract_run_id, _extract_stage_jobs

CONFIG_REL = "configs/fake-pipeline.yaml"


@pytest.mark.platform
def test_visual_eval_setup(
    physai_session, physai_cli, fake_project_dir, fake_containers_built
) -> None:
    """``physai eval --visual`` runs flock + DCV setup + teardown around fake-evaluator.

    Asserts:
    - The sbatch's host-context preamble printed a connect block
      (ProxyCommand-style ``aws ssm start-session`` line).
    - The DCV teardown sentinel is in the log (trap fired, script ran
      to completion).
    - The flock file under ``/fsx/physai/dcv-claims/`` was written and is
      released after the job ends (``flock -n`` succeeds post-eval).
    - The fake-evaluator received and recorded ``--visual`` in metrics.json.
    """
    # Stage a trivial checkpoint matching what fake-trainer would write.
    checkpoint_name = "regression-visual-ckpt"
    checkpoint_dir = f"/fsx/checkpoints/{checkpoint_name}"
    physai_session.run(f"mkdir -p {checkpoint_dir}")
    physai_session.run(
        f"dd if=/dev/zero of={checkpoint_dir}/checkpoint.bin bs=1024 count=1 status=none"
    )

    run_id = ""
    try:
        r = physai_cli.run(
            "eval",
            "--config",
            str(fake_project_dir / CONFIG_REL),
            "--checkpoint",
            checkpoint_name,
            "--visual",
            "--eval-rounds",
            "1",
            "--model-config-root",
            str(fake_project_dir / "model_configs"),
            timeout=600,
        )
        run_id = _extract_run_id(r.stdout)

        # `physai eval` prints "  eval: job <ID>" right after sbatch returns.
        stage_jobs = _extract_stage_jobs(r.stdout)
        job_id = stage_jobs.get("eval")
        assert job_id, f"could not parse eval job id from output:\n{r.stdout}"

        log = physai_session.run(f"cat /fsx/physai/logs/{job_id}.out")

        # Connect block is printed by dcv_session_setup.sh in host context.
        assert "aws ssm start-session" in log, (
            f"connect block missing from job log:\n{log[:2000]}"
        )

        # Teardown sentinel: dcv_session_teardown.sh prints
        # "dcv-teardown: completed for job <id>" as its last line. Catching
        # this in the log proves the sbatch's `trap '... teardown.sh' EXIT
        # TERM` fired AND the script ran to completion (the chpasswd inside
        # is wrapped in `set -e`, so a partial run aborts before this line).
        assert f"dcv-teardown: completed for job {job_id}" in log, (
            f"DCV teardown sentinel missing from job log — trap may not have "
            f"fired or teardown script crashed:\n{log[:2000]}"
        )

        # `flock -n` (non-blocking) acquires the lock or exits non-zero
        # immediately. After the job ends the kernel should have released
        # the per-host DCV lock, so we can grab it now.
        ok = physai_session.run(
            "flock -n /fsx/physai/dcv-claims/$(sinfo -h -p gpu -N -t idle,mix,alloc "
            "-o '%N' | sort -u | head -n1).lock -c true && echo ok || echo busy"
        )
        assert "ok" in ok, f"DCV flock not released after eval finished: {ok}"

        # End-to-end: fake-evaluator wrote metrics.json, so the sbatch
        # body actually invoked the container after the host-context preamble.
        # The fake records its $5 argv (the optional --visual flag) into
        # metrics.json so we can verify it was forwarded — this is what
        # proves the CLI's --visual reaches the entrypoint, not just the
        # surrounding flock/dcv plumbing.
        eval_dir = f"/fsx/evaluations/{run_id}"
        metrics = json.loads(physai_session.run(f"cat {eval_dir}/metrics.json"))
        assert "success_rate" in metrics, (
            f"metrics.json missing required key: {metrics}"
        )
        assert metrics.get("visual") == "--visual", (
            f"fake-evaluator did not receive --visual; metrics.json: {metrics}"
        )
    finally:
        physai_session.run(f"rm -rf {checkpoint_dir}")
        if run_id:
            physai_session.run(f"rm -rf /fsx/evaluations/{run_id}* 2>/dev/null || true")
