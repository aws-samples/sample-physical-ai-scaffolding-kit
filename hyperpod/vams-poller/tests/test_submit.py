"""Tests for the SUBMITTING handler."""

import dataclasses
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from pask_vams_poller import submit
from pask_vams_poller.config import PollerConfig
from pask_vams_poller.constants import (
    INPUT_KIND_DATASET,
    INPUT_KIND_VIEW,
    INPUT_MANIFEST_FILE,
    JOB_STARTED_FILE,
    ErrorName,
    State,
)
from pask_vams_poller.errors import TransientError
from pask_vams_poller.store import ExecutionItem, ExecutionStore

from .test_staging import (
    ASSET_ID,
    DATABASE_ID,
    DEFAULT_TRAINING_CONFIG,
    HF_TOKEN_VALUE,
    PID,
    TOKEN,
    FakeSfn,
    FakeSlurmRunner,
    asset_bucket_versioned,  # noqa: F401
    base_dir,  # noqa: F401
    buckets,  # noqa: F401
    create_staging_item,
    fsx_root,  # noqa: F401
    job_training_env,  # noqa: F401
    make_ctx,  # noqa: F401
    store,  # noqa: F401
)

JOB_ID = "4242"
JOB_NAME = f"vams-{PID}"
DATASET_INPUT_MANIFEST: dict[str, Any] = {"schemaVersion": 1, "objects": []}
VIEW_INPUT_MANIFEST: dict[str, Any] = {"schemaVersion": 2, "kind": INPUT_KIND_VIEW, "objects": []}


def write_input_manifest(run_directory: str, document: Any) -> None:
    """Write ``input_manifest.json`` into a run directory."""
    (Path(run_directory) / INPUT_MANIFEST_FILE).write_text(json.dumps(document), encoding="utf-8")


@pytest.fixture
def submitting_item(
    store: ExecutionStore,  # noqa: F811
    s3_client: Any,
    buckets: dict[str, str],  # noqa: F811
    make_ctx: Callable[..., Any],  # noqa: F811
) -> ExecutionItem:
    """Create an execution in SUBMITTING with its run directory."""
    item = create_staging_item(
        store, s3_client, make_ctx(FakeSfn(None)), PID, DEFAULT_TRAINING_CONFIG
    )
    os.makedirs(item.runDir)
    write_input_manifest(item.runDir, DATASET_INPUT_MANIFEST)
    return store.transition(item, State.SUBMITTING, set_attrs={"jobName": JOB_NAME})


def sbatch_calls(runner: FakeSlurmRunner) -> list[tuple[list[str], dict[str, str] | None]]:
    """Return the recorded sbatch calls."""
    return [call for call in runner.calls if call[0][0] == "sbatch"]


def test_submit_runs_sbatch_without_env_values_in_argv(
    store: ExecutionStore,  # noqa: F811
    submitting_item: ExecutionItem,
    make_ctx: Callable[..., Any],  # noqa: F811
    fsx_root: Callable[[str], str],  # noqa: F811
    base_dir: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin:/bin")
    monkeypatch.setenv("PASK_VAMS_TABLE_NAME", "not-for-the-job")
    runner = FakeSlurmRunner("", 0, f"{JOB_ID}\n")
    sfn = FakeSfn(None)

    submit.process_submitting(submitting_item, make_ctx(sfn, slurm_runner=runner))

    stored = store.get(PID)
    assert stored.state is State.SUBMITTED
    assert stored.slurmJobId == JOB_ID
    assert sfn.tokens[-1] == TOKEN
    assert runner.commands() == ["squeue", "sbatch"]
    [(argv, env)] = sbatch_calls(runner)
    run_dir = submitting_item.runDir
    assert argv == [
        "sbatch",
        "--parsable",
        "--no-requeue",
        f"--job-name={JOB_NAME}",
        f"--chdir={base_dir}/training",
        f"--output={run_dir}/slurm_%j.out",
        f"--error={run_dir}/slurm_%j.err",
        "--export=ALL",
        "slurm_train.sh",
        JOB_NAME,
    ]
    assert not any(HF_TOKEN_VALUE in argument for argument in argv)
    assert env["HF_TOKEN"] == HF_TOKEN_VALUE
    assert env["PATH"] == "/usr/local/bin:/usr/bin:/bin"
    assert "PASK_VAMS_TABLE_NAME" not in env
    assert env["POLICY_TYPE"] == "pi05"
    assert env["STEPS"] == "100"
    assert env["DATASET_ROOT"] == fsx_root(PID)
    assert env["DATASET_REPO_ID"] == f"vams/{DATABASE_ID}/{ASSET_ID}"
    assert env["OUTPUT_DIR"] == submitting_item.outputDir
    assert env["VAMS_RUN_DIR"] == run_dir
    assert env["VAMS_CONTEXT_FILE"] == f"{run_dir}/vams_context.json"
    assert env["RESUME"] == "false"
    assert env["MLFLOW_EXPERIMENT_NAME"] == "vams-training"
    assert "VAMS_VIEW_MANIFEST_FILE" not in env


def test_submit_view_points_dataset_root_at_view_root(
    submitting_item: ExecutionItem,
    make_ctx: Callable[..., Any],  # noqa: F811
    fsx_root: Callable[[str], str],  # noqa: F811
) -> None:
    run_dir = submitting_item.runDir
    write_input_manifest(run_dir, VIEW_INPUT_MANIFEST)
    runner = FakeSlurmRunner("", 0, f"{JOB_ID}\n")

    submit.process_submitting(submitting_item, make_ctx(FakeSfn(None), slurm_runner=runner))

    [(argv, env)] = sbatch_calls(runner)
    assert env["DATASET_ROOT"] == f"{run_dir}/view_dataset"
    assert env["VAMS_VIEW_MANIFEST_FILE"] == f"{run_dir}/curation_manifest.json"
    assert env["DATASET_REPO_ID"] == f"vams/{DATABASE_ID}/{ASSET_ID}"
    assert env["DATASET_ROOT"] != fsx_root(PID)
    assert not any("view_dataset" in argument for argument in argv)
    assert not any("curation_manifest.json" in argument for argument in argv)
    assert not any(HF_TOKEN_VALUE in argument for argument in argv)


def context_with_overriding_settings(ctx: Any) -> Any:
    """Return a context whose training environment sets the poller-decided values."""
    return dataclasses.replace(
        ctx,
        training_env={
            **ctx.training_env,
            "DATASET_ROOT": "/from/training/env",
            "VAMS_VIEW_MANIFEST_FILE": "/from/training/env.json",
        },
    )


OVERRIDING_TRAINING_PARAMS = {
    "DATASET_ROOT": "/from/params",
    "VAMS_VIEW_MANIFEST_FILE": "/from/params.json",
}


def test_build_job_env_view_values_override_training_settings(
    submitting_item: ExecutionItem,
    make_ctx: Callable[..., Any],  # noqa: F811
) -> None:
    ctx = context_with_overriding_settings(make_ctx(FakeSfn(None)))

    env = submit.build_job_env(
        submitting_item,
        ctx,
        OVERRIDING_TRAINING_PARAMS,
        DATABASE_ID,
        ASSET_ID,
        {},
        input_kind=INPUT_KIND_VIEW,
    )

    run_dir = submitting_item.runDir
    assert env["DATASET_ROOT"] == f"{run_dir}/view_dataset"
    assert env["VAMS_VIEW_MANIFEST_FILE"] == f"{run_dir}/curation_manifest.json"


def test_build_job_env_dataset_drops_view_manifest_from_training_settings(
    submitting_item: ExecutionItem,
    make_ctx: Callable[..., Any],  # noqa: F811
    fsx_root: Callable[[str], str],  # noqa: F811
) -> None:
    ctx = context_with_overriding_settings(make_ctx(FakeSfn(None)))

    env = submit.build_job_env(
        submitting_item,
        ctx,
        OVERRIDING_TRAINING_PARAMS,
        DATABASE_ID,
        ASSET_ID,
        {},
        input_kind=INPUT_KIND_DATASET,
    )

    assert env["DATASET_ROOT"] == fsx_root(PID)
    assert "VAMS_VIEW_MANIFEST_FILE" not in env


def test_build_job_env_rejects_unknown_kind(
    submitting_item: ExecutionItem,
    make_ctx: Callable[..., Any],  # noqa: F811
) -> None:
    with pytest.raises(ValueError, match="unknown input kind"):
        submit.build_job_env(
            submitting_item,
            make_ctx(FakeSfn(None)),
            {},
            DATABASE_ID,
            ASSET_ID,
            {},
            input_kind="other",
        )


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (None, "missing"),
        ("{not json", "not valid JSON"),
        ("[]", "not a JSON object"),
        (json.dumps({"schemaVersion": 2, "kind": "materialized"}), "unknown kind"),
    ],
)
def test_submit_invalid_input_manifest_fails_without_sbatch(
    store: ExecutionStore,  # noqa: F811
    submitting_item: ExecutionItem,
    make_ctx: Callable[..., Any],  # noqa: F811
    content: str | None,
    message: str,
) -> None:
    manifest_path = Path(submitting_item.runDir) / INPUT_MANIFEST_FILE
    if content is None:
        manifest_path.unlink()
    else:
        manifest_path.write_text(content, encoding="utf-8")
    runner = FakeSlurmRunner("", 0, f"{JOB_ID}\n")

    submit.process_submitting(submitting_item, make_ctx(FakeSfn(None), slurm_runner=runner))

    stored = store.get(PID)
    assert stored.state is State.FINALIZING
    assert stored.outcome["errorName"] == ErrorName.STAGING_FAILED.value
    assert message in stored.outcome["cause"]
    assert sbatch_calls(runner) == []


def test_submit_passes_configured_partition(
    submitting_item: ExecutionItem,
    make_ctx: Callable[..., Any],  # noqa: F811
    fsx_root: Callable[[str], str],  # noqa: F811
    poller_config: PollerConfig,
) -> None:
    runner = FakeSlurmRunner("", 0, f"{JOB_ID}\n")
    cfg = dataclasses.replace(poller_config, slurm_partition="gpu")

    submit.process_submitting(
        submitting_item, make_ctx(FakeSfn(None), slurm_runner=runner, cfg=cfg)
    )

    [(argv, _)] = sbatch_calls(runner)
    assert "--partition=gpu" in argv


def test_submit_records_job_found_in_squeue(
    store: ExecutionStore,  # noqa: F811
    submitting_item: ExecutionItem,
    make_ctx: Callable[..., Any],  # noqa: F811
) -> None:
    runner = FakeSlurmRunner(f"{JOB_ID}|PENDING\n", 0, "9999\n")

    submit.process_submitting(submitting_item, make_ctx(FakeSfn(None), slurm_runner=runner))

    stored = store.get(PID)
    assert stored.state is State.SUBMITTED
    assert stored.slurmJobId == JOB_ID
    assert sbatch_calls(runner) == []


@pytest.mark.parametrize(
    ("file_name", "content"),
    [
        (
            JOB_STARTED_FILE,
            json.dumps(
                {"slurmJobId": JOB_ID, "startedAt": "2026-01-01T00:00:00Z", "hostname": "w"}
            ),
        ),
        (f"slurm_{JOB_ID}.out", "training log"),
    ],
)
def test_submit_records_job_from_run_directory_marker(
    store: ExecutionStore,  # noqa: F811
    submitting_item: ExecutionItem,
    make_ctx: Callable[..., Any],  # noqa: F811
    file_name: str,
    content: str,
) -> None:
    (Path(submitting_item.runDir) / file_name).write_text(content, encoding="utf-8")
    runner = FakeSlurmRunner("", 0, "9999\n")

    submit.process_submitting(submitting_item, make_ctx(FakeSfn(None), slurm_runner=runner))

    stored = store.get(PID)
    assert stored.state is State.SUBMITTED
    assert stored.slurmJobId == JOB_ID
    assert sbatch_calls(runner) == []


def test_job_id_from_run_directory_uses_newest_output_file(tmp_path: Path) -> None:
    (tmp_path / "slurm_7.out").write_text("", encoding="utf-8")
    (tmp_path / "slurm_12.out").write_text("", encoding="utf-8")
    (tmp_path / "slurm_12.err").write_text("", encoding="utf-8")

    assert submit.job_id_from_run_directory(str(tmp_path)) == "12"


def test_job_id_from_run_directory_without_markers(tmp_path: Path) -> None:
    assert submit.job_id_from_run_directory(str(tmp_path)) is None


def test_submit_permanent_sbatch_failure_finalizes_failed(
    store: ExecutionStore,  # noqa: F811
    submitting_item: ExecutionItem,
    make_ctx: Callable[..., Any],  # noqa: F811
    fsx_root: Callable[[str], str],  # noqa: F811
) -> None:
    runner = FakeSlurmRunner("", 1, "sbatch: error: Invalid partition name specified")

    submit.process_submitting(submitting_item, make_ctx(FakeSfn(None), slurm_runner=runner))

    stored = store.get(PID)
    assert stored.state is State.FINALIZING
    assert stored.outcome["status"] == "FAILED"
    assert stored.outcome["errorName"] == ErrorName.SUBMIT_FAILED.value
    assert "Invalid partition" in stored.outcome["cause"]
    assert HF_TOKEN_VALUE not in stored.outcome["cause"]


def test_submit_transient_sbatch_failure_keeps_submitting(
    store: ExecutionStore,  # noqa: F811
    submitting_item: ExecutionItem,
    make_ctx: Callable[..., Any],  # noqa: F811
    fsx_root: Callable[[str], str],  # noqa: F811
) -> None:
    runner = FakeSlurmRunner("", 1, "sbatch: error: Socket timed out on send/recv operation")

    with pytest.raises(TransientError):
        submit.process_submitting(submitting_item, make_ctx(FakeSfn(None), slurm_runner=runner))

    assert store.get(PID).state is State.SUBMITTING


def test_submit_closed_heartbeat_moves_to_abort_suspected_without_sbatch(
    store: ExecutionStore,  # noqa: F811
    submitting_item: ExecutionItem,
    make_ctx: Callable[..., Any],  # noqa: F811
) -> None:
    runner = FakeSlurmRunner("", 0, f"{JOB_ID}\n")

    submit.process_submitting(submitting_item, make_ctx(FakeSfn(0), slurm_runner=runner))

    stored = store.get(PID)
    assert stored.state is State.ABORT_SUSPECTED
    assert stored.prevState is State.SUBMITTING
    assert runner.calls == []
