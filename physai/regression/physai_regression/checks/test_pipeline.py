"""Pipeline-stage checks using the fake-project containers."""

import json
import subprocess
import time

import pytest

from physai_regression.checks._parsing import _extract_run_id, _extract_stage_jobs
from physai_regression.orchestration.deploy import aws_cli_args

CONFIG_REL = "configs/fake-pipeline.yaml"
RAW_NAME = "regression-pipeline-raw"


# Slurm states that are NOT terminal — a job in one of these may still
# transition. sacct frequently reports COMPLETING (or has no row yet) for a
# beat after a job leaves squeue, so a single read races that lag.
_NON_TERMINAL_STATES = frozenset({"PENDING", "RUNNING", "COMPLETING", "CONFIGURING"})


def _sacct_states(session, run_id: str) -> dict[str, str]:
    """Return ``{job_name: state}`` for every job tagged with ``run_id``.

    Reads sacct directly. Skips Slurm step rows (``<id>.batch``,
    ``<id>.extern``) and trims the ``by <user>`` suffix Slurm appends to
    CANCELLED states.
    """
    out = session.run(
        f"sacct -n --parsable2 -o JobID,JobName,State -S 2000-01-01 "
        f"| grep 'physai/run/{run_id}' || true"
    )
    states: dict[str, str] = {}
    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) < 3:
            continue
        job_id, job_name, state = parts[0], parts[1], parts[2]
        if "." in job_id:
            continue
        states[job_name] = state.split(" ", 1)[0]
    return states


def _wait_for_stage_states(
    session, run_id: str, stages: tuple[str, ...], timeout: int = 300
) -> dict[str, str]:
    """Poll sacct until every named stage has a terminal state, then return
    the full ``{job_name: state}`` map.

    A bare ``_sacct_states`` read races the squeue→sacct accounting lag: a
    job that just left the queue can still show ``COMPLETING`` or have no
    row yet. This polls until each requested stage both has a row and is in
    a terminal state, so callers can assert on the final outcome.
    """
    deadline = time.time() + timeout
    states: dict[str, str] = {}
    while time.time() < deadline:
        states = _sacct_states(session, run_id)
        pending = False
        for stage in stages:
            matches = [s for n, s in states.items() if n.endswith(f"/{stage}")]
            if not matches or any(s in _NON_TERMINAL_STATES for s in matches):
                pending = True
                break
        if not pending:
            return states
        time.sleep(5)
    raise AssertionError(
        f"stages {stages} for {run_id} did not reach a terminal state within "
        f"{timeout}s.\nlast states: {states}"
    )


def _wait_for_run_jobs(session, run_id: str, timeout: int) -> dict[str, str]:
    """Poll ``squeue`` until every ``physai/run/<run_id>/*`` job leaves it,
    then return their terminal states from sacct."""
    deadline = time.time() + timeout
    out = ""
    while time.time() < deadline:
        out = session.run(
            f"squeue -h -u $(whoami) -o '%i|%j|%T' | grep 'physai/run/{run_id}' || true"
        )
        if not out.strip():
            return _sacct_states(session, run_id)
        time.sleep(5)
    raise AssertionError(
        f"jobs for {run_id} did not leave the queue within {timeout}s.\nsqueue:\n{out}"
    )


def _seed_raw(session, profile: str | None, region: str | None, bucket: str) -> str:
    """Stage a tiny raw input under ``/fsx/raw/<RAW_NAME>/`` via S3 + DRA.

    Returns ``RAW_NAME``. Idempotent: if the marker file is already present
    from an earlier run, the function returns without re-uploading.
    """
    target = f"/fsx/raw/{RAW_NAME}"
    # Only short-circuit when the marker file itself is present, not merely
    # the directory. A prior run that created the dir but never completed the
    # DRA import of marker.txt would otherwise return a fixture the convert
    # stage then reads as empty.
    out = session.run(f"test -f {target}/marker.txt && echo y || echo n")
    if out.strip() == "y":
        return RAW_NAME
    # Upload via S3 so the DRA imports it into /fsx/raw/.
    s3_uri = f"s3://{bucket}/raw/{RAW_NAME}/marker.txt"
    cmd = [
        "aws",
        *aws_cli_args(profile, region),
        "s3",
        "cp",
        "-",
        s3_uri,
    ]
    r = subprocess.run(
        cmd,
        input="regression-pipeline\n",
        text=True,
        capture_output=True,
        check=False,
    )
    if r.returncode != 0:
        raise AssertionError(f"`aws s3 cp` to seed raw failed: {r.stderr.strip()}")
    # Wait for the namespace import.
    deadline = time.time() + 60
    while time.time() < deadline:
        out = session.run(f"test -d {target} && ls {target} || true")
        if "marker.txt" in out:
            return RAW_NAME
        time.sleep(2)
    raise AssertionError(f"DRA did not import {s3_uri} → {target} within 60s.")


def _model_config_root(fake_project_dir) -> str:
    return str(fake_project_dir / "model_configs")


@pytest.mark.platform
def test_convert_stage(
    physai_session,
    physai_cli,
    fake_project_dir,
    fake_containers_built,
    data_bucket_name,
    aws_profile,
    aws_region,
) -> None:
    """``physai convert`` of fake-raw via fake-converter writes /fsx/datasets/<name>."""
    raw_name = _seed_raw(physai_session, aws_profile, aws_region, data_bucket_name)
    dataset_name = "regression-convert-out"
    target = f"/fsx/datasets/{dataset_name}"
    physai_session.run(f"rm -rf {target}")

    try:
        r = physai_cli.run(
            "convert",
            "--config",
            str(fake_project_dir / CONFIG_REL),
            "--raw",
            raw_name,
            "--dataset",
            dataset_name,
            "--model-config-root",
            _model_config_root(fake_project_dir),
            timeout=600,
        )
        run_id = _extract_run_id(r.stdout)
        states = _wait_for_stage_states(physai_session, run_id, ("convert",))
        convert_jobs = [s for n, s in states.items() if n.endswith("/convert")]
        assert convert_jobs == ["COMPLETED"], (
            f"convert stage did not complete cleanly: {states}"
        )
        ls = physai_session.run(f"test -d {target} && ls {target} || echo MISSING")
        assert "info.txt" in ls, (
            f"convert stage did not produce {target}/info.txt. ls: {ls}"
        )
    finally:
        physai_session.run(f"rm -rf {target}")


@pytest.mark.platform
def test_train_eval_chain(
    physai_session, physai_cli, fake_project_dir, fake_containers_built
) -> None:
    """``physai run --from train --to eval`` chains afterok and writes metrics.json."""
    dataset_name = "regression-train-eval-in"
    dataset_dir = f"/fsx/datasets/{dataset_name}"
    physai_session.run(f"mkdir -p {dataset_dir}")
    physai_session.run(f"echo regression > {dataset_dir}/marker.txt")
    run_id = ""
    try:
        r = physai_cli.run(
            "run",
            "--config",
            str(fake_project_dir / CONFIG_REL),
            "--from",
            "train",
            "--to",
            "eval",
            "--dataset",
            dataset_name,
            "--model-config-root",
            _model_config_root(fake_project_dir),
            timeout=900,
        )
        run_id = _extract_run_id(r.stdout)
        states = _wait_for_stage_states(physai_session, run_id, ("train", "eval"))

        for stage in ("train", "eval"):
            matches = [s for n, s in states.items() if n.endswith(f"/{stage}")]
            assert matches == ["COMPLETED"], (
                f"{stage} stage did not complete cleanly: {states}"
            )

        eval_dir = f"/fsx/evaluations/{run_id}"
        metrics_raw = physai_session.run(f"cat {eval_dir}/metrics.json")
        metrics = json.loads(metrics_raw)
        for key in ("eval_rounds", "success_rate", "checkpoint"):
            assert key in metrics, (
                f"metrics.json missing required key {key!r}: {metrics}"
            )
    finally:
        physai_session.run(f"rm -rf {dataset_dir}")
        if run_id:
            physai_session.run(
                f"rm -rf /fsx/checkpoints/{run_id}* /fsx/evaluations/{run_id}*"
            )


@pytest.mark.platform
def test_full_pipeline(
    physai_session,
    physai_cli,
    fake_project_dir,
    fake_containers_built,
    data_bucket_name,
    aws_profile,
    aws_region,
) -> None:
    """Convert → train → eval in one ``physai run``.

    Exercises the full preflight path where each stage's output is
    satisfied by the next stage's plan rather than already on disk. The
    ``--dataset`` arg names the convert output, so the convert→train hop
    is pinned; the train→eval checkpoint hop is the one resolved with no
    user-supplied hint.
    """
    raw_name = _seed_raw(physai_session, aws_profile, aws_region, data_bucket_name)
    dataset_name = "regression-full-pipeline"
    dataset_dir = f"/fsx/datasets/{dataset_name}"
    physai_session.run(f"rm -rf {dataset_dir}")

    run_id = ""
    try:
        r = physai_cli.run(
            "run",
            "--config",
            str(fake_project_dir / CONFIG_REL),
            "--raw",
            raw_name,
            "--dataset",
            dataset_name,
            "--model-config-root",
            _model_config_root(fake_project_dir),
            timeout=900,
        )
        run_id = _extract_run_id(r.stdout)
        states = _wait_for_stage_states(
            physai_session, run_id, ("convert", "train", "eval")
        )

        for stage in ("convert", "train", "eval"):
            matches = [s for n, s in states.items() if n.endswith(f"/{stage}")]
            assert matches == ["COMPLETED"], (
                f"{stage} stage did not complete cleanly: {states}"
            )

        # Convert wrote info.txt, train wrote checkpoint.bin, eval wrote
        # metrics.json — confirms each stage's outputs reached disk and
        # the next stage's inputs resolved against them.
        assert "info.txt" in physai_session.run(f"ls {dataset_dir}")
        assert "checkpoint.bin" in physai_session.run(f"ls /fsx/checkpoints/{run_id}")
        metrics = json.loads(
            physai_session.run(f"cat /fsx/evaluations/{run_id}/metrics.json")
        )
        assert "success_rate" in metrics, (
            f"metrics.json missing required key: {metrics}"
        )
    finally:
        physai_session.run(f"rm -rf {dataset_dir}")
        if run_id:
            physai_session.run(
                f"rm -rf /fsx/checkpoints/{run_id}* /fsx/evaluations/{run_id}*"
            )


@pytest.mark.platform
def test_cancel_cascades(
    physai_session, physai_cli, fake_project_dir, fake_containers_built
) -> None:
    """Cancelling train (while still PENDING or briefly RUNNING) prevents eval from completing.

    The fake-trainer sleeps 20s before exiting, so by the time
    ``physai cancel`` lands train is guaranteed to still be active —
    train cannot reach COMPLETED. Its terminal state is CANCELLED in the
    common case (occasionally FAILED if the cancel lands in the
    launch/teardown gap); the check asserts train did not COMPLETE rather
    than the exact string. Eval terminal state is then either absent (train
    was PENDING; --kill-on-invalid-dep=yes drops eval pre-run, no sacct
    record) or DependencyNeverSatisfied (train was RUNNING). Either is a
    successful cascade.
    """
    dataset_name = "regression-cancel-in"
    dataset_dir = f"/fsx/datasets/{dataset_name}"
    physai_session.run(f"mkdir -p {dataset_dir}")
    physai_session.run(f"echo regression > {dataset_dir}/marker.txt")

    run_id = ""
    try:
        r = physai_cli.run(
            "run",
            "--config",
            str(fake_project_dir / CONFIG_REL),
            "--from",
            "train",
            "--to",
            "eval",
            "--dataset",
            dataset_name,
            "--model-config-root",
            _model_config_root(fake_project_dir),
            "-n",
        )
        run_id = _extract_run_id(r.stdout)

        # `physai run -n` returns after sbatch has assigned a job ID for
        # every stage; the IDs are printed one-per-line ("  train: job N").
        stage_jobs = _extract_stage_jobs(r.stdout)
        train_id = stage_jobs.get("train")
        assert train_id, (
            f"could not find train job id in `physai run` output:\n{r.stdout}"
        )

        # Cancel immediately. The fake-trainer's 20s sleep ensures train
        # cannot have reached COMPLETED yet, so this cancel always lands
        # before train terminates normally.
        physai_cli.run("cancel", train_id)

        states = _wait_for_run_jobs(physai_session, run_id, timeout=300)
        train_states = [s for n, s in states.items() if n.endswith("/train")]
        eval_states = [s for n, s in states.items() if n.endswith("/eval")]
        # The cancel must have prevented train from completing. Slurm records
        # a cancelled job as CANCELLED in the common case, but if the cancel
        # lands in the launch/teardown gap it can occasionally be FAILED —
        # both prove train did not reach COMPLETED, which is the property the
        # cascade depends on. Assert on that property rather than the exact
        # string so legitimate scheduler variance isn't a red test.
        assert len(train_states) == 1, (
            f"expected exactly one train terminal row, got {train_states}: {states}"
        )
        assert train_states[0] not in _NON_TERMINAL_STATES, (
            f"train did not reach a terminal state: {states}"
        )
        assert train_states[0] != "COMPLETED", (
            f"train reached COMPLETED despite cancel — cascade did not "
            f"trigger: {states}"
        )
        # Eval terminal state depends on train's state at cancel:
        #   - PENDING → eval killed pre-run, no sacct record (eval_states=[])
        #   - RUNNING → eval recorded as DependencyNeverSatisfied
        # Both are valid cascade outcomes; anything else is a real bug.
        valid_eval_outcomes = ([], ["DependencyNeverSatisfied"])
        assert eval_states in valid_eval_outcomes, (
            f"eval job's final state {eval_states} is not one of "
            f"{valid_eval_outcomes}. Full states: {states}"
        )
    finally:
        physai_session.run(f"rm -rf {dataset_dir}")
        if run_id:
            # fake-trainer mkdir's its output dir before the sleep, so a
            # checkpoint dir exists by the time cancel lands; clean it (and
            # any eval dir) like the sibling checks do.
            physai_session.run(
                f"rm -rf /fsx/checkpoints/{run_id}* /fsx/evaluations/{run_id}*"
            )
