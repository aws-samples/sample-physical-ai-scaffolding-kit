"""Tests for job monitoring, write back, completion callbacks, resend, and cleanup."""

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from boto3.exceptions import S3UploadFailedError
from botocore.exceptions import ClientError

from pask_vams_poller.config import PollerConfig
from pask_vams_poller.constants import (
    CONCURRENCY_ITEM_ID,
    OUTPUT_FILES_LIMIT,
    PENDING_CLEANUP,
    ErrorName,
    State,
)
from pask_vams_poller.context import PollerContext
from pask_vams_poller.errors import TransientError
from pask_vams_poller.finalize import (
    CleanupTargetMismatch,
    content_type_for,
    process_cleanup,
    process_finalizing,
    process_running,
    process_submitted,
)
from pask_vams_poller.sfn import CallResult
from pask_vams_poller.slurm import SqueueJob
from pask_vams_poller.store import ExecutionItem, ExecutionStore

from .conftest import FIXED_EPOCH_SECONDS, TEST_REGION

OWNER = "login-node:invocation-a"
PID = "exec_0001-a"
JOB_NAME = f"vams-{PID}"
JOB_ID = "42"
TOKEN = "token-1"
NEW_TOKEN = "token-2"
SENT_AT = 1_789_999_000_000
VAMS_BUCKET = "vams-asset-bucket"
MANIFEST_KEY = "pipelines/pask-train/wfx1/manifest.json"
CONFIG_KEY = "pipelines/pask-train/wfx1/config.json"
FILES_PREFIX = "pipelines/pask-train/wfx1/output/files/"
RESULTS_PREFIX = "pipelines/pask-train/wfx1/output/results/"
TRAINING_PARAMS = {"POLICY_TYPE": "pi05", "STEPS": 3000}
SUCCEEDED_OUTCOME = {"status": "SUCCEEDED"}
DATASET_DIGEST = "sha256:" + "ab" * 32
IDENTITY = {
    "workflowDatabaseId": "db1",
    "workflowId": "wf1",
    "workflowExecutionId": "wfx1",
    "inputManifestS3Location": f"s3://{VAMS_BUCKET}/{MANIFEST_KEY}",
}


class FakeSfn:
    """Step Functions notifier that closes a token once it reported an outcome."""

    def __init__(self) -> None:
        """Start with no closed token and no recorded call."""
        self.closed: set[str] = set()
        self.calls: list[tuple[Any, ...]] = []

    def heartbeat(self, token: str) -> CallResult:
        """Record a heartbeat."""
        self.calls.append(("heartbeat", token))
        return CallResult.CLOSED if token in self.closed else CallResult.ACCEPTED

    def send_success(self, token: str, output: dict[str, Any]) -> CallResult:
        """Record a success and close the token."""
        self.calls.append(("success", token, output))
        return self._close(token)

    def send_failure(self, token: str, error: str, cause: str) -> CallResult:
        """Record a failure and close the token."""
        self.calls.append(("failure", token, error, cause))
        return self._close(token)

    def outcome_calls(self) -> list[tuple[Any, ...]]:
        """Return the recorded SendTaskSuccess and SendTaskFailure calls."""
        return [call for call in self.calls if call[0] != "heartbeat"]

    def _close(self, token: str) -> CallResult:
        """Close the token, returning CLOSED when it was already closed."""
        if token in self.closed:
            return CallResult.CLOSED
        self.closed.add(token)
        return CallResult.ACCEPTED


class FakeSlurm:
    """Slurm client whose queue the test sets."""

    def __init__(self) -> None:
        """Start with an empty queue."""
        self.queue: dict[str, list[SqueueJob]] = {}

    def find_by_name(self, job_name: str) -> list[SqueueJob]:
        """Return the queued jobs with the name."""
        return list(self.queue.get(job_name, []))


class DenyingS3:
    """S3 client wrapper whose ``upload_file`` fails like boto3's transfer manager does."""

    def __init__(self, client: Any, code: str) -> None:
        """Wrap ``client`` and fail uploads with the error ``code``."""
        self._client = client
        self._code = code

    def upload_file(self, filename: str, bucket: str, key: str, **_: Any) -> None:
        """Raise ``S3UploadFailedError`` with the ``ClientError`` as its context."""
        try:
            raise ClientError({"Error": {"Code": self._code, "Message": "no"}}, "PutObject")
        except ClientError as error:
            raise S3UploadFailedError(f"Failed to upload {filename}: {error}")  # noqa: B904

    def __getattr__(self, name: str) -> Any:
        """Delegate everything else to the wrapped client."""
        return getattr(self._client, name)


@pytest.fixture
def store(dynamodb_client: Any, executions_table: str, fixed_clock: Callable[[], float]) -> Any:
    """Return a store owned by ``OWNER`` on the moto table."""
    return ExecutionStore(
        executions_table,
        region=TEST_REGION,
        owner_id=OWNER,
        lease_seconds=900,
        ttl_days=30,
        clock=fixed_clock,
        dynamodb_client=dynamodb_client,
    )


@pytest.fixture
def fsx_env(tmp_path: Path) -> dict[str, str]:
    """Return a training environment whose directories are under ``tmp_path``."""
    return {
        "LEROBOT_PI_BASE_DIR": str(tmp_path / "lerobot-openpi"),
        "LEROBOT_PI_OUTPUT_DIR": str(tmp_path / "lerobot-openpi" / "outputs"),
    }


@pytest.fixture
def run_path(fsx_env: dict[str, str]) -> Path:
    """Create and return the run directory of ``PID``."""
    path = Path(fsx_env["LEROBOT_PI_BASE_DIR"]) / "vams-runs" / PID
    path.mkdir(parents=True)
    return path


@pytest.fixture
def output_path(fsx_env: dict[str, str]) -> Path:
    """Return the output directory of ``PID`` (not created)."""
    return Path(fsx_env["LEROBOT_PI_OUTPUT_DIR"]) / f"vams-{PID}"


@pytest.fixture
def vams_bucket(s3_client: Any) -> str:
    """Create the VAMS bucket with the manifest and the training configuration."""
    s3_client.create_bucket(
        Bucket=VAMS_BUCKET, CreateBucketConfiguration={"LocationConstraint": TEST_REGION}
    )
    manifest = {
        "schemaVersion": 1,
        "inputFiles": [],
        "inputMetadataS3Location": f"s3://{VAMS_BUCKET}/metadata.json",
        "outputs": {
            "bucket": VAMS_BUCKET,
            "files": FILES_PREFIX,
            "previews": "pipelines/pask-train/wfx1/output/previews/",
            "metadata": "pipelines/pask-train/wfx1/output/metadata/",
            "results": RESULTS_PREFIX,
        },
        "outputTarget": {"locationType": "asset", "assetId": "a1", "databaseId": "db1"},
        "auxBucket": "aux",
        "auxTempPrefix": "pipelines/pask-train/wfx1/",
        "systemConfig": {"orchestrationEventPrefix": f"x.execution.wfx1.pipeline.{PID}"},
    }
    s3_client.put_object(Bucket=VAMS_BUCKET, Key=MANIFEST_KEY, Body=json.dumps(manifest))
    s3_client.put_object(Bucket=VAMS_BUCKET, Key=CONFIG_KEY, Body=json.dumps(TRAINING_PARAMS))
    return VAMS_BUCKET


@pytest.fixture
def sfn() -> FakeSfn:
    """Return the fake Step Functions notifier."""
    return FakeSfn()


@pytest.fixture
def slurm() -> FakeSlurm:
    """Return the fake Slurm client."""
    return FakeSlurm()


@pytest.fixture
def ctx(
    poller_config: PollerConfig,
    store: ExecutionStore,
    sfn: FakeSfn,
    slurm: FakeSlurm,
    s3_client: Any,
    data_bucket: str,
    vams_bucket: str,
    fsx_env: dict[str, str],
    fixed_clock: Callable[[], float],
) -> PollerContext:
    """Return a poller context wired to the fakes, moto, and ``tmp_path``."""
    return PollerContext(
        config=poller_config,
        store=store,
        sfn=sfn,
        slurm=slurm,
        s3=s3_client,
        sqs=None,
        training_env=fsx_env,
        owner_id=OWNER,
        clock=fixed_clock,
    )


def _put_item(
    dynamodb_client: Any,
    table: str,
    fsx_env: dict[str, str],
    state: State,
    **overrides: Any,
) -> None:
    """Write an execution leased by ``OWNER`` straight into the table."""
    values: dict[str, Any] = {
        "pipelineExecutionId": PID,
        "state": state,
        "version": 5,
        "taskToken": TOKEN,
        "tokenSentAt": SENT_AT,
        "leaseOwner": OWNER,
        "leaseExpiresAt": int(FIXED_EPOCH_SECONDS) + 900,
        **IDENTITY,
        "inputConfigurationS3Location": f"s3://{VAMS_BUCKET}/{CONFIG_KEY}",
        "jobName": JOB_NAME,
        "slurmJobId": JOB_ID,
        "stagingPrefix": f"vams/{PID}/",
        "runDir": f"{fsx_env['LEROBOT_PI_BASE_DIR']}/vams-runs/{PID}",
        "outputDir": f"{fsx_env['LEROBOT_PI_OUTPUT_DIR']}/vams-{PID}",
        "pending": "ACTIVE",
        "holdsSlot": True,
    }
    values.update(overrides)
    item = ExecutionItem(**values)
    dynamodb_client.put_item(TableName=table, Item=item.to_item())


@pytest.fixture
def put_item(
    dynamodb_client: Any, executions_table: str, fsx_env: dict[str, str]
) -> Callable[..., None]:
    """Return a function that writes an execution in a given state."""
    return lambda state, **overrides: _put_item(
        dynamodb_client, executions_table, fsx_env, state, **overrides
    )


@pytest.fixture
def counter(dynamodb_client: Any, executions_table: str) -> Callable[[], int]:
    """Create the concurrency counter with one active run and return a reader of it."""
    dynamodb_client.put_item(
        TableName=executions_table,
        Item={"pipelineExecutionId": {"S": CONCURRENCY_ITEM_ID}, "active": {"N": "1"}},
    )

    def read() -> int:
        item = dynamodb_client.get_item(
            TableName=executions_table, Key={"pipelineExecutionId": {"S": CONCURRENCY_ITEM_ID}}
        )["Item"]
        return int(item["active"]["N"])

    return read


def _get(store: ExecutionStore) -> ExecutionItem:
    """Read the execution ``PID``."""
    item = store.get(PID)
    assert item is not None
    return item


def _write_json(path: Path, document: dict[str, Any]) -> None:
    """Write a JSON file."""
    path.write_text(json.dumps(document), encoding="utf-8")


def _finish_job(run_path: Path, exit_code: int) -> None:
    """Write the marker files of a job that ended with ``exit_code``."""
    _write_json(
        run_path / "job_started.json",
        {"slurmJobId": JOB_ID, "startedAt": "2026-09-21T10:00:00Z", "hostname": "worker-1"},
    )
    _write_json(
        run_path / "job_exit.json",
        {"slurmJobId": JOB_ID, "exitCode": exit_code, "finishedAt": "2026-09-21T12:00:00Z"},
    )
    _write_json(
        run_path / "mlflow_run.json",
        {"run_id": "run-1", "experiment_id": "7", "tracking_uri": "arn:test"},
    )
    _write_json(
        run_path / "vams_context.json", {"schemaVersion": 1, "datasetDigest": DATASET_DIGEST}
    )


def _write_checkpoint(output_path: Path, extra_files: int = 0) -> Path:
    """Create ``checkpoints/last`` as a symlink with a model and a training state."""
    step_dir = output_path / "checkpoints" / "003000"
    model_dir = step_dir / "pretrained_model"
    (model_dir / "sub").mkdir(parents=True)
    (model_dir / "model.safetensors").write_bytes(b"\0" * 64)
    (model_dir / "config.json").write_text("{}", encoding="utf-8")
    (model_dir / "sub" / "processor.json").write_text("{}", encoding="utf-8")
    for index in range(extra_files):
        (model_dir / f"shard-{index:04d}.bin").write_bytes(b"x")
    state_dir = step_dir / "training_state"
    state_dir.mkdir()
    (state_dir / "optimizer_state.safetensors").write_bytes(b"\0" * 16)
    os.symlink("003000", output_path / "checkpoints" / "last")
    return model_dir


def _list_keys(s3_client: Any, bucket: str, prefix: str) -> list[str]:
    """List the keys under a prefix."""
    response = s3_client.list_objects_v2(Bucket=bucket, Prefix=prefix)
    return sorted(entry["Key"] for entry in response.get("Contents", []))


@pytest.mark.parametrize(
    ("file_name", "expected"),
    [
        ("model.safetensors", "application/octet-stream"),
        ("config.json", "application/json"),
        ("helper.py", "application/octet-stream"),
        ("run.sh", "application/octet-stream"),
        ("weights.bin", "application/octet-stream"),
        ("no_extension", "application/octet-stream"),
    ],
)
def test_content_type_for(file_name: str, expected: str) -> None:
    assert content_type_for(file_name) == expected


def test_submitted_moves_to_running_when_squeue_reports_running(
    ctx: PollerContext,
    put_item: Callable[..., None],
    slurm: FakeSlurm,
    sfn: FakeSfn,
    run_path: Path,
) -> None:
    put_item(State.SUBMITTED)
    slurm.queue[JOB_NAME] = [SqueueJob(job_id=JOB_ID, state="RUNNING")]

    process_submitted(_get(ctx.store), ctx)

    assert _get(ctx.store).state is State.RUNNING
    assert sfn.calls == [("heartbeat", TOKEN)]


def test_submitted_moves_to_running_when_started_marker_exists(
    ctx: PollerContext, put_item: Callable[..., None], slurm: FakeSlurm, run_path: Path
) -> None:
    put_item(State.SUBMITTED)
    slurm.queue[JOB_NAME] = [SqueueJob(job_id=JOB_ID, state="PENDING")]
    _write_json(run_path / "job_started.json", {"slurmJobId": JOB_ID})

    process_submitted(_get(ctx.store), ctx)

    assert _get(ctx.store).state is State.RUNNING


def test_submitted_stays_while_pending(
    ctx: PollerContext, put_item: Callable[..., None], slurm: FakeSlurm, run_path: Path
) -> None:
    put_item(State.SUBMITTED)
    slurm.queue[JOB_NAME] = [SqueueJob(job_id=JOB_ID, state="PENDING")]

    process_submitted(_get(ctx.store), ctx)

    item = _get(ctx.store)
    assert item.state is State.SUBMITTED
    assert item.version == 5


def test_running_stays_while_queued(
    ctx: PollerContext,
    put_item: Callable[..., None],
    slurm: FakeSlurm,
    sfn: FakeSfn,
    run_path: Path,
) -> None:
    put_item(State.RUNNING)
    slurm.queue[JOB_NAME] = [SqueueJob(job_id=JOB_ID, state="RUNNING")]

    process_running(_get(ctx.store), ctx)

    assert _get(ctx.store).state is State.RUNNING
    assert sfn.calls == [("heartbeat", TOKEN)]


def test_running_job_that_left_the_queue_succeeds(
    ctx: PollerContext,
    put_item: Callable[..., None],
    slurm: FakeSlurm,
    run_path: Path,
    output_path: Path,
) -> None:
    put_item(State.RUNNING)
    _finish_job(run_path, 0)
    _write_checkpoint(output_path)
    slurm.queue[JOB_NAME] = [SqueueJob(job_id=JOB_ID, state="COMPLETED")]

    process_running(_get(ctx.store), ctx)

    item = _get(ctx.store)
    assert item.state is State.FINALIZING
    assert item.outcome == {"status": "SUCCEEDED", "errorName": None, "cause": None}
    assert item.mlflowRunId == "run-1"
    assert item.mlflowExperimentId == "7"


def test_non_zero_exit_code_is_job_failed_with_err_tail(
    ctx: PollerContext, put_item: Callable[..., None], run_path: Path, output_path: Path
) -> None:
    put_item(State.RUNNING)
    _finish_job(run_path, 3)
    _write_checkpoint(output_path)
    (run_path / f"slurm_{JOB_ID}.err").write_text(
        "x" * 10000 + "\nTraceback: CUDA out of memory\n", encoding="utf-8"
    )

    process_running(_get(ctx.store), ctx)

    outcome = _get(ctx.store).outcome
    assert outcome is not None
    assert outcome["status"] == "FAILED"
    assert outcome["errorName"] == ErrorName.JOB_FAILED.value
    assert "exited with code 3" in outcome["cause"]
    assert outcome["cause"].endswith("Traceback: CUDA out of memory")
    assert len(outcome["cause"]) < 4096 + 200


def test_missing_job_exit_is_job_lost(
    ctx: PollerContext, put_item: Callable[..., None], run_path: Path
) -> None:
    put_item(State.SUBMITTED)

    process_submitted(_get(ctx.store), ctx)

    item = _get(ctx.store)
    assert item.state is State.FINALIZING
    assert item.outcome is not None
    assert item.outcome["errorName"] == ErrorName.JOB_LOST.value
    assert item.mlflowRunId is None


def test_exit_code_zero_without_checkpoint_is_job_failed(
    ctx: PollerContext, put_item: Callable[..., None], run_path: Path
) -> None:
    put_item(State.RUNNING)
    _finish_job(run_path, 0)

    process_running(_get(ctx.store), ctx)

    outcome = _get(ctx.store).outcome
    assert outcome is not None
    assert outcome["errorName"] == ErrorName.JOB_FAILED.value
    assert "pretrained_model is missing" in outcome["cause"]


def test_closed_heartbeat_moves_to_abort_suspected(
    ctx: PollerContext, put_item: Callable[..., None], sfn: FakeSfn, run_path: Path
) -> None:
    put_item(State.RUNNING)
    sfn.closed.add(TOKEN)

    process_running(_get(ctx.store), ctx)

    item = _get(ctx.store)
    assert item.state is State.ABORT_SUSPECTED
    assert item.prevState is State.RUNNING


def test_heartbeat_uses_replaced_token_and_continues(
    ctx: PollerContext,
    put_item: Callable[..., None],
    sfn: FakeSfn,
    slurm: FakeSlurm,
    run_path: Path,
) -> None:
    put_item(State.SUBMITTED)
    stale = _get(ctx.store)
    assert ctx.store.replace_token(PID, NEW_TOKEN, SENT_AT + 1000, IDENTITY) is not None
    sfn.closed.add(TOKEN)
    slurm.queue[JOB_NAME] = [SqueueJob(job_id=JOB_ID, state="RUNNING")]

    process_submitted(stale, ctx)

    assert sfn.calls == [("heartbeat", TOKEN), ("heartbeat", NEW_TOKEN)]
    assert _get(ctx.store).state is State.RUNNING


def test_finalizing_writes_back_and_reports_success(
    ctx: PollerContext,
    put_item: Callable[..., None],
    sfn: FakeSfn,
    s3_client: Any,
    run_path: Path,
    output_path: Path,
) -> None:
    put_item(
        State.FINALIZING, outcome=SUCCEEDED_OUTCOME, mlflowRunId="run-1", mlflowExperimentId="7"
    )
    _finish_job(run_path, 0)
    _write_checkpoint(output_path)

    process_finalizing(_get(ctx.store), ctx)

    expected_files = [
        f"{FILES_PREFIX}config.json",
        f"{FILES_PREFIX}model.safetensors",
        f"{FILES_PREFIX}sub/processor.json",
    ]
    assert _list_keys(s3_client, VAMS_BUCKET, FILES_PREFIX) == expected_files
    assert _list_keys(s3_client, VAMS_BUCKET, RESULTS_PREFIX) == [
        f"{RESULTS_PREFIX}pask_training.json"
    ]
    model_head = s3_client.head_object(Bucket=VAMS_BUCKET, Key=f"{FILES_PREFIX}model.safetensors")
    config_head = s3_client.head_object(Bucket=VAMS_BUCKET, Key=f"{FILES_PREFIX}config.json")
    assert model_head["ContentType"] == "application/octet-stream"
    assert config_head["ContentType"] == "application/json"

    results_object = s3_client.get_object(
        Bucket=VAMS_BUCKET, Key=f"{RESULTS_PREFIX}pask_training.json"
    )
    assert results_object["ContentType"] == "application/json"
    assert json.loads(results_object["Body"].read()) == {
        "pipelineExecutionId": PID,
        "slurmJobId": JOB_ID,
        "parameters": {"POLICY_TYPE": "pi05", "STEPS": "3000"},
        "datasetDigest": DATASET_DIGEST,
        "inputKind": "dataset",
        "mlflowRunId": "run-1",
        "mlflowExperimentId": "7",
        "startedAt": "2026-09-21T10:00:00Z",
        "finishedAt": "2026-09-21T12:00:00Z",
        "status": "SUCCEEDED",
    }

    item = _get(ctx.store)
    assert item.state is State.SUCCEEDED
    assert item.notifyResult == "ACCEPTED"
    assert item.pending == PENDING_CLEANUP
    assert item.outputs == [*expected_files, f"{RESULTS_PREFIX}pask_training.json"]
    assert all("training_state" not in key for key in item.outputs)
    assert sfn.outcome_calls() == [
        ("success", TOKEN, {"status": "SUCCEEDED", "slurmJobId": JOB_ID, "mlflowRunId": "run-1"})
    ]


def _view_context(source: dict[str, Any]) -> dict[str, Any]:
    """Build a schema version 2 ``vams_context.json`` of a curation view."""
    return {
        "schemaVersion": 2,
        "input": {
            "databaseId": "db1",
            "assetId": "asset-view",
            "bucket": VAMS_BUCKET,
            "assetRootS3Key": "asset-view/",
            "versioned": True,
            "kind": "view",
        },
        "view": {
            "manifestFile": "/fsx/runs/curation_manifest.json",
            "createdAt": "2026-10-20T00:00:00Z",
            "episodeCount": 12,
            "excludedEpisodeCount": 3,
            "source": source,
        },
        "datasetDigest": DATASET_DIGEST,
    }


def _finalize_and_read_results(
    ctx: PollerContext,
    put_item: Callable[..., None],
    s3_client: Any,
    run_path: Path,
    output_path: Path,
    context: dict[str, Any] | None,
) -> dict[str, Any]:
    """Finalize a succeeded job with ``context`` as ``vams_context.json`` and read the results.

    ``None`` removes ``vams_context.json``.
    """
    put_item(
        State.FINALIZING, outcome=SUCCEEDED_OUTCOME, mlflowRunId="run-1", mlflowExperimentId="7"
    )
    _finish_job(run_path, 0)
    context_path = run_path / "vams_context.json"
    if context is None:
        context_path.unlink()
    else:
        _write_json(context_path, context)
    _write_checkpoint(output_path)

    process_finalizing(_get(ctx.store), ctx)

    assert _get(ctx.store).state is State.SUCCEEDED
    results_object = s3_client.get_object(
        Bucket=VAMS_BUCKET, Key=f"{RESULTS_PREFIX}pask_training.json"
    )
    return json.loads(results_object["Body"].read())


def test_results_of_a_dataset_have_no_view_fields(
    ctx: PollerContext,
    put_item: Callable[..., None],
    s3_client: Any,
    run_path: Path,
    output_path: Path,
) -> None:
    results = _finalize_and_read_results(
        ctx,
        put_item,
        s3_client,
        run_path,
        output_path,
        {"schemaVersion": 1, "input": {"assetId": "asset-1"}, "datasetDigest": DATASET_DIGEST},
    )

    assert results["inputKind"] == "dataset"
    assert results["datasetDigest"] == DATASET_DIGEST
    assert "viewSource" not in results
    assert "viewEpisodeCount" not in results


def test_results_of_a_view_carry_the_source_asset(
    ctx: PollerContext,
    put_item: Callable[..., None],
    s3_client: Any,
    run_path: Path,
    output_path: Path,
) -> None:
    source = {
        "databaseId": "db-src",
        "assetId": "asset-src",
        "assetVersion": "3",
        "bucket": "source-bucket",
        "assetRootS3Key": "asset-src/",
    }

    results = _finalize_and_read_results(
        ctx, put_item, s3_client, run_path, output_path, _view_context(source)
    )

    assert results["inputKind"] == "view"
    assert results["viewSource"] == {
        "databaseId": "db-src",
        "assetId": "asset-src",
        "assetVersion": "3",
    }
    assert results["viewEpisodeCount"] == 12
    assert results["datasetDigest"] == DATASET_DIGEST


def test_results_of_a_view_without_asset_version_omit_the_key(
    ctx: PollerContext,
    put_item: Callable[..., None],
    s3_client: Any,
    run_path: Path,
    output_path: Path,
) -> None:
    source = {
        "databaseId": "db-src",
        "assetId": "asset-src",
        "bucket": "source-bucket",
        "assetRootS3Key": "asset-src/",
    }

    results = _finalize_and_read_results(
        ctx, put_item, s3_client, run_path, output_path, _view_context(source)
    )

    assert results["viewSource"] == {"databaseId": "db-src", "assetId": "asset-src"}
    assert results["viewEpisodeCount"] == 12


def test_results_of_a_view_without_source_omit_view_source(
    ctx: PollerContext,
    put_item: Callable[..., None],
    s3_client: Any,
    run_path: Path,
    output_path: Path,
) -> None:
    context = _view_context({})
    del context["view"]["source"]

    results = _finalize_and_read_results(ctx, put_item, s3_client, run_path, output_path, context)

    assert results["inputKind"] == "view"
    assert "viewSource" not in results
    assert results["viewEpisodeCount"] == 12


def test_results_without_vams_context_report_null_input_kind(
    ctx: PollerContext,
    put_item: Callable[..., None],
    s3_client: Any,
    run_path: Path,
    output_path: Path,
) -> None:
    results = _finalize_and_read_results(ctx, put_item, s3_client, run_path, output_path, None)

    assert results["inputKind"] is None
    assert results["datasetDigest"] is None
    assert "viewSource" not in results
    assert "viewEpisodeCount" not in results


def test_too_many_files_fail_with_output_upload_failed(
    ctx: PollerContext,
    put_item: Callable[..., None],
    sfn: FakeSfn,
    s3_client: Any,
    run_path: Path,
    output_path: Path,
) -> None:
    put_item(State.FINALIZING, outcome=SUCCEEDED_OUTCOME)
    _finish_job(run_path, 0)
    _write_checkpoint(output_path, extra_files=OUTPUT_FILES_LIMIT)

    process_finalizing(_get(ctx.store), ctx)

    item = _get(ctx.store)
    assert item.state is State.FAILED
    assert item.outcome is not None
    assert item.outcome["errorName"] == ErrorName.OUTPUT_UPLOAD_FAILED.value
    assert item.outputs is None
    assert _list_keys(s3_client, VAMS_BUCKET, FILES_PREFIX) == []
    [call] = sfn.outcome_calls()
    assert call[:3] == ("failure", TOKEN, ErrorName.OUTPUT_UPLOAD_FAILED.value)


def test_checkpoint_symlink_outside_the_model_fails_without_upload(
    ctx: PollerContext,
    put_item: Callable[..., None],
    sfn: FakeSfn,
    s3_client: Any,
    run_path: Path,
    output_path: Path,
    tmp_path: Path,
) -> None:
    put_item(State.FINALIZING, outcome=SUCCEEDED_OUTCOME)
    _finish_job(run_path, 0)
    model_dir = _write_checkpoint(output_path)
    outside_file = tmp_path / "other-user-secret.txt"
    outside_file.write_text("secret", encoding="utf-8")
    os.symlink(outside_file, model_dir / "linked.txt")

    process_finalizing(_get(ctx.store), ctx)

    item = _get(ctx.store)
    assert item.state is State.FAILED
    assert item.outcome is not None
    assert item.outcome["errorName"] == ErrorName.OUTPUT_UPLOAD_FAILED.value
    assert _list_keys(s3_client, VAMS_BUCKET, FILES_PREFIX) == []
    [call] = sfn.outcome_calls()
    assert call[:3] == ("failure", TOKEN, ErrorName.OUTPUT_UPLOAD_FAILED.value)


def test_checkpoint_symlink_inside_the_model_is_uploaded(
    ctx: PollerContext,
    put_item: Callable[..., None],
    s3_client: Any,
    run_path: Path,
    output_path: Path,
) -> None:
    put_item(State.FINALIZING, outcome=SUCCEEDED_OUTCOME)
    _finish_job(run_path, 0)
    model_dir = _write_checkpoint(output_path)
    os.symlink("config.json", model_dir / "linked.json")

    process_finalizing(_get(ctx.store), ctx)

    item = _get(ctx.store)
    assert item.state is State.SUCCEEDED
    assert f"{FILES_PREFIX}linked.json" in _list_keys(s3_client, VAMS_BUCKET, FILES_PREFIX)


def test_outputs_bucket_other_than_workflow_bucket_fails_without_upload(
    ctx: PollerContext,
    put_item: Callable[..., None],
    sfn: FakeSfn,
    s3_client: Any,
    run_path: Path,
    output_path: Path,
) -> None:
    other_bucket = "vams-other-bucket"
    s3_client.create_bucket(
        Bucket=other_bucket, CreateBucketConfiguration={"LocationConstraint": TEST_REGION}
    )
    manifest = json.loads(s3_client.get_object(Bucket=VAMS_BUCKET, Key=MANIFEST_KEY)["Body"].read())
    manifest["outputs"]["bucket"] = other_bucket
    s3_client.put_object(Bucket=VAMS_BUCKET, Key=MANIFEST_KEY, Body=json.dumps(manifest))
    put_item(State.FINALIZING, outcome=SUCCEEDED_OUTCOME)
    _finish_job(run_path, 0)
    _write_checkpoint(output_path)

    process_finalizing(_get(ctx.store), ctx)

    item = _get(ctx.store)
    assert item.state is State.FAILED
    assert item.outcome is not None
    assert item.outcome["errorName"] == ErrorName.OUTPUT_UPLOAD_FAILED.value
    assert _list_keys(s3_client, other_bucket, "") == []
    [call] = sfn.outcome_calls()
    assert call[:3] == ("failure", TOKEN, ErrorName.OUTPUT_UPLOAD_FAILED.value)


def test_access_denied_upload_fails_permanently(
    ctx: PollerContext,
    put_item: Callable[..., None],
    sfn: FakeSfn,
    s3_client: Any,
    run_path: Path,
    output_path: Path,
) -> None:
    put_item(State.FINALIZING, outcome=SUCCEEDED_OUTCOME)
    _finish_job(run_path, 0)
    _write_checkpoint(output_path)
    ctx.s3 = DenyingS3(s3_client, "AccessDenied")

    process_finalizing(_get(ctx.store), ctx)

    item = _get(ctx.store)
    assert item.state is State.FAILED
    assert item.outcome is not None
    assert item.outcome["errorName"] == ErrorName.OUTPUT_UPLOAD_FAILED.value
    assert "AccessDenied" in item.outcome["cause"]


def test_transient_upload_error_keeps_finalizing(
    ctx: PollerContext,
    put_item: Callable[..., None],
    sfn: FakeSfn,
    s3_client: Any,
    run_path: Path,
    output_path: Path,
) -> None:
    put_item(State.FINALIZING, outcome=SUCCEEDED_OUTCOME)
    _finish_job(run_path, 0)
    _write_checkpoint(output_path)
    ctx.s3 = DenyingS3(s3_client, "SlowDown")

    with pytest.raises(TransientError):
        process_finalizing(_get(ctx.store), ctx)

    item = _get(ctx.store)
    assert item.state is State.FINALIZING
    assert item.outputs is None
    assert sfn.outcome_calls() == []


def test_failed_outcome_is_reported_without_write_back(
    ctx: PollerContext, put_item: Callable[..., None], sfn: FakeSfn, s3_client: Any
) -> None:
    put_item(
        State.FINALIZING,
        outcome={"status": "FAILED", "errorName": ErrorName.JOB_LOST.value, "cause": "lost"},
    )

    process_finalizing(_get(ctx.store), ctx)

    assert _get(ctx.store).state is State.FAILED
    assert sfn.calls == [("failure", TOKEN, ErrorName.JOB_LOST.value, "lost")]
    assert _list_keys(s3_client, VAMS_BUCKET, FILES_PREFIX) == []


def test_stop_after_accepted_success_finishes_as_already_closed(
    ctx: PollerContext,
    put_item: Callable[..., None],
    sfn: FakeSfn,
    run_path: Path,
    output_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    put_item(State.FINALIZING, outcome=SUCCEEDED_OUTCOME, mlflowRunId="run-1")
    _finish_job(run_path, 0)
    _write_checkpoint(output_path)

    def stop_before_terminal(item: ExecutionItem, notify_result: str) -> ExecutionItem:
        raise RuntimeError("poller stopped")

    with monkeypatch.context() as patch:
        patch.setattr(ctx.store, "to_notified_terminal", stop_before_terminal)
        with pytest.raises(RuntimeError, match="poller stopped"):
            process_finalizing(_get(ctx.store), ctx)

    stopped = _get(ctx.store)
    assert stopped.state is State.FINALIZING
    assert stopped.outputs is not None
    uploads_before = list(ctx.s3.list_objects_v2(Bucket=VAMS_BUCKET)["Contents"])

    process_finalizing(stopped, ctx)

    item = _get(ctx.store)
    assert item.state is State.SUCCEEDED
    assert item.notifyResult == "ALREADY_CLOSED"
    assert [call[0] for call in sfn.outcome_calls()] == ["success", "success"]
    assert ctx.s3.list_objects_v2(Bucket=VAMS_BUCKET)["Contents"] == uploads_before


def test_finalizing_resends_with_replaced_token(
    ctx: PollerContext, put_item: Callable[..., None], sfn: FakeSfn
) -> None:
    put_item(
        State.FINALIZING,
        outcome={"status": "FAILED", "errorName": ErrorName.JOB_FAILED.value, "cause": "exit 1"},
    )
    stale = _get(ctx.store)
    assert ctx.store.replace_token(PID, NEW_TOKEN, SENT_AT + 1000, IDENTITY) is not None
    sfn.closed.add(TOKEN)

    process_finalizing(stale, ctx)

    assert [call[1] for call in sfn.outcome_calls()] == [TOKEN, NEW_TOKEN]
    item = _get(ctx.store)
    assert item.state is State.FAILED
    assert item.notifyResult == "ACCEPTED"


def test_cleanup_resends_outcome_to_new_token(
    ctx: PollerContext,
    put_item: Callable[..., None],
    sfn: FakeSfn,
    counter: Callable[[], int],
    run_path: Path,
) -> None:
    put_item(
        State.SUCCEEDED,
        outcome=SUCCEEDED_OUTCOME,
        mlflowRunId="run-1",
        notifyResult="ACCEPTED",
        pending=PENDING_CLEANUP,
    )
    sfn.closed.add(TOKEN)
    replaced = ctx.store.replace_token(PID, NEW_TOKEN, SENT_AT + 1000, IDENTITY)
    assert replaced is not None and replaced.resendPending

    process_cleanup(_get(ctx.store), ctx)

    assert sfn.outcome_calls() == [
        (
            "success",
            NEW_TOKEN,
            {"status": "SUCCEEDED", "slurmJobId": JOB_ID, "mlflowRunId": "run-1"},
        )
    ]
    item = _get(ctx.store)
    assert item.notifyResult == "ACCEPTED"
    assert item.resendPending is False
    assert item.pending is None
    assert counter() == 0


def test_cleanup_resends_aborted_as_failure(
    ctx: PollerContext,
    put_item: Callable[..., None],
    sfn: FakeSfn,
    counter: Callable[[], int],
    run_path: Path,
) -> None:
    put_item(State.ABORTED, pending=PENDING_CLEANUP)
    assert ctx.store.replace_token(PID, NEW_TOKEN, SENT_AT + 1000, IDENTITY) is not None

    process_cleanup(_get(ctx.store), ctx)

    [call] = sfn.outcome_calls()
    assert call[:3] == ("failure", NEW_TOKEN, ErrorName.ABORTED.value)
    assert _get(ctx.store).pending is None


def test_cleanup_deletes_only_the_execution_paths(
    ctx: PollerContext,
    put_item: Callable[..., None],
    counter: Callable[[], int],
    s3_client: Any,
    data_bucket: str,
    run_path: Path,
    output_path: Path,
    fsx_env: dict[str, str],
) -> None:
    put_item(
        State.FAILED,
        outcome={"status": "FAILED", "errorName": "PASK.JobLost", "cause": "x"},
        pending=PENDING_CLEANUP,
    )
    for index in range(1001):
        s3_client.put_object(
            Bucket=data_bucket, Key=f"vams/{PID}/data/{index:04d}.parquet", Body=b""
        )
    s3_client.put_object(Bucket=data_bucket, Key=f"vams/{PID}x/keep.parquet", Body=b"")
    s3_client.put_object(Bucket=data_bucket, Key="vams/other/keep.parquet", Body=b"")
    s3_client.put_object(Bucket=VAMS_BUCKET, Key=f"vams/{PID}/asset.parquet", Body=b"")
    _write_checkpoint(output_path)
    sibling_run = Path(fsx_env["LEROBOT_PI_BASE_DIR"]) / "vams-runs" / "other"
    sibling_run.mkdir()

    process_cleanup(_get(ctx.store), ctx)

    assert _list_keys(s3_client, data_bucket, f"vams/{PID}/") == []
    assert _list_keys(s3_client, data_bucket, "vams/") == sorted(
        ["vams/other/keep.parquet", f"vams/{PID}x/keep.parquet"]
    )
    assert _list_keys(s3_client, VAMS_BUCKET, "vams/") == [f"vams/{PID}/asset.parquet"]
    assert not run_path.exists()
    assert not output_path.exists()
    assert sibling_run.exists()
    assert _get(ctx.store).pending is None
    assert _get(ctx.store).holdsSlot is False
    assert counter() == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"stagingPrefix": "vams/"},
        {"stagingPrefix": f"vams/{PID}"},
        {"runDir": "/fsx/ubuntu/lerobot-openpi/vams-runs"},
        {"outputDir": "/"},
    ],
)
def test_cleanup_refuses_unexpected_paths(
    ctx: PollerContext,
    put_item: Callable[..., None],
    counter: Callable[[], int],
    s3_client: Any,
    data_bucket: str,
    run_path: Path,
    overrides: dict[str, str],
) -> None:
    put_item(State.ABORTED, pending=PENDING_CLEANUP, **overrides)
    s3_client.put_object(Bucket=data_bucket, Key="vams/other/keep.parquet", Body=b"")

    with pytest.raises(CleanupTargetMismatch):
        process_cleanup(_get(ctx.store), ctx)

    assert _list_keys(s3_client, data_bucket, "vams/") == ["vams/other/keep.parquet"]
    assert _get(ctx.store).pending == PENDING_CLEANUP
    assert counter() == 1


def test_cleanup_can_run_again_after_a_stop(
    ctx: PollerContext,
    put_item: Callable[..., None],
    counter: Callable[[], int],
    run_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    put_item(State.ABORTED, pending=PENDING_CLEANUP)

    with monkeypatch.context() as patch:
        patch.setattr(
            ctx.store, "complete_cleanup", lambda item: (_ for _ in ()).throw(RuntimeError("stop"))
        )
        with pytest.raises(RuntimeError, match="stop"):
            process_cleanup(_get(ctx.store), ctx)

    assert not run_path.exists()
    process_cleanup(_get(ctx.store), ctx)
    assert _get(ctx.store).pending is None
    assert counter() == 0

    process_cleanup(_get(ctx.store), ctx)
    assert counter() == 0


def test_cleanup_without_created_paths_succeeds(
    ctx: PollerContext, put_item: Callable[..., None], dynamodb_client: Any, executions_table: str
) -> None:
    put_item(
        State.FAILED,
        outcome={"status": "FAILED", "errorName": "PASK.InvalidRequest", "cause": "bad"},
        pending=PENDING_CLEANUP,
        holdsSlot=False,
        stagingPrefix=None,
        runDir=None,
        outputDir=None,
    )

    process_cleanup(_get(ctx.store), ctx)

    assert _get(ctx.store).pending is None
