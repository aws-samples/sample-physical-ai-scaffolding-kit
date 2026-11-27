"""Tests for the RECEIVED and STAGING handlers."""

import copy
import dataclasses
import hashlib
import json
import os
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError

from pask_vams_poller import config, staging, view
from pask_vams_poller.config import PollerConfig
from pask_vams_poller.constants import (
    CONCURRENCY_ITEM_ID,
    CURATION_MANIFEST_FILE,
    INPUT_MANIFEST_FILE,
    VAMS_CONTEXT_FILE,
    VIEW_DATASET_DIR,
    ErrorName,
    State,
)
from pask_vams_poller.context import PollerContext
from pask_vams_poller.errors import PermanentFailure, TransientError
from pask_vams_poller.sfn import CallResult
from pask_vams_poller.slurm import SlurmClient
from pask_vams_poller.store import ExecutionItem, ExecutionStore

from .conftest import FIXED_EPOCH_SECONDS, TEST_REGION

OWNER = "login-node:invocation-a"
PID = "exec_0001-a"
OTHER_PID = "exec_0002-b"
TOKEN = "token-1"
SENT_AT = 1_789_999_000_000
WORKFLOW_BUCKET = "vams-test-workflow"
WORKFLOW_INPUTS_PREFIX = "pipelines/workflowExecutionInputs/"
ASSET_BUCKET = "vams-test-assets"
ASSET_ROOT = "asset-1/"
DATABASE_ID = "db1"
ASSET_ID = "asset-1"
OUTPUT_ASSET_ID = "model-1"
PIPELINE_ID = "pask-train"
HF_TOKEN_VALUE = "hf_secret_value_for_test"

ASSET_FILES: dict[str, bytes] = {
    "asset-1/meta/info.json": b'{"codebase_version": "v3.0"}',
    "asset-1/meta/stats.json": b'{"action": {}}',
    "asset-1/data/chunk-000/file-000.parquet": b"parquet-bytes-0123456789",
    "asset-1/videos/cam/file-000.mp4": b"mp4-bytes",
}
IGNORED_ASSET_KEYS: tuple[str, ...] = (
    "asset-1/",
    "asset-1/videos/",
    "asset-1/videos/cam/file-000.mp4.previewFile.gif",
)
# Curation view manifest contract (see tests/test_view.py).
CONTRACT_FILE = Path(__file__).parent / "fixtures" / "curation_view_contract.json"
CONTRACT: dict[str, Any] = json.loads(CONTRACT_FILE.read_text(encoding="utf-8"))
SOURCE_ASSET_ID = "source-1"
SOURCE_ROOT = "source-1/"
SOURCE_FILES: dict[str, bytes] = {
    "data/chunk-000/file-000.parquet": b"source-data-v1",
    "meta/episodes/chunk-000/file-000.parquet": b"source-episodes-v1",
    "meta/info.json": b'{"codebase_version": "v3.0", "total_episodes": 5}',
    "meta/tasks.parquet": b"source-tasks",
    "videos/cam/chunk-000/file-000.mp4": b"source-video-v1",
}
VIEW_STATS_BODY = b'{"action": {"mean": [0.0]}}'
DEFAULT_TRAINING_CONFIG: dict[str, Any] = {
    "POLICY_TYPE": "pi05",
    "FINETUNE_MODE": "lora",
    "STEPS": 100,
    "SAVE_FREQ": 50,
}


class FakeSfn:
    """Step Functions notifier that accepts a fixed number of heartbeats, then reports CLOSED."""

    def __init__(self, accepted_calls: int | None) -> None:
        """Create the fake.

        Args:
            accepted_calls: Heartbeats to accept before CLOSED, or None to always accept.
        """
        self._accepted_calls = accepted_calls
        self._lock = threading.Lock()
        self.tokens: list[str] = []

    def heartbeat(self, token: str) -> CallResult:
        """Record the heartbeat and return its result."""
        with self._lock:
            self.tokens.append(token)
            if self._accepted_calls is None or len(self.tokens) <= self._accepted_calls:
                return CallResult.ACCEPTED
            return CallResult.CLOSED


class FakeSlurmRunner:
    """Replacement of ``subprocess.run`` that records Slurm commands."""

    def __init__(self, squeue_stdout: str, sbatch_returncode: int, sbatch_output: str) -> None:
        """Create the fake.

        Args:
            squeue_stdout: Standard output of squeue.
            sbatch_returncode: Exit status of sbatch.
            sbatch_output: Standard output of sbatch when it succeeds, else standard error.
        """
        self._squeue_stdout = squeue_stdout
        self._sbatch_returncode = sbatch_returncode
        self._sbatch_output = sbatch_output
        self.calls: list[tuple[list[str], dict[str, str] | None]] = []

    def __call__(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        """Record the command and return its fake result."""
        self.calls.append((list(args), kwargs.get("env")))
        if args[0] == "squeue":
            return subprocess.CompletedProcess(args, 0, self._squeue_stdout, "")
        if self._sbatch_returncode == 0:
            return subprocess.CompletedProcess(args, 0, self._sbatch_output, "")
        return subprocess.CompletedProcess(args, self._sbatch_returncode, "", self._sbatch_output)

    def commands(self) -> list[str]:
        """Return the executable of every recorded call."""
        return [args[0] for args, _ in self.calls]


class SlowCopyS3:
    """S3 client wrapper whose managed copy sleeps and records the copied keys."""

    def __init__(self, client: Any, delay_seconds: float) -> None:
        """Wrap ``client``."""
        self._client = client
        self._delay_seconds = delay_seconds
        self.copied_keys: list[str] = []

    def copy(self, copy_source: dict[str, str], bucket: str, key: str, **kwargs: Any) -> None:
        """Copy after a delay."""
        time.sleep(self._delay_seconds)
        self._client.copy(copy_source, bucket, key, **kwargs)
        self.copied_keys.append(key)

    def __getattr__(self, name: str) -> Any:
        """Delegate every other call to the wrapped client."""
        return getattr(self._client, name)


class AdvancingClock:
    """Clock that moves forward by a fixed step on every call."""

    def __init__(self, start: float, step: float) -> None:
        """Start at ``start`` epoch seconds."""
        self._now = start
        self._step = step

    def __call__(self) -> float:
        """Return the current time, then advance."""
        current = self._now
        self._now += self._step
        return current


def item_fields(pid: str) -> dict[str, str]:
    """Return the RECEIVED fields of an execution whose request files are in the workflow bucket."""
    return {
        "pipelineExecutionId": pid,
        "workflowDatabaseId": DATABASE_ID,
        "workflowId": "wf1",
        "workflowExecutionId": "wfx1",
        "inputManifestS3Location": f"s3://{WORKFLOW_BUCKET}/{WORKFLOW_INPUTS_PREFIX}{pid}/manifest.json",
        "inputConfigurationS3Location": f"s3://{WORKFLOW_BUCKET}/{WORKFLOW_INPUTS_PREFIX}{pid}/config.json",
        "inputMetadataS3Location": f"s3://{WORKFLOW_BUCKET}/{WORKFLOW_INPUTS_PREFIX}{pid}/metadata.json",
        "executingUserName": "alice",
    }


def build_manifest(pid: str) -> dict[str, Any]:
    """Return a VAMS manifest envelope that selects the whole test asset."""
    outputs_prefix = f"pipelines/{PIPELINE_ID}/job/output/wfx1"
    return {
        "inputFiles": [
            {
                "bucket": ASSET_BUCKET,
                "assetRootS3Key": ASSET_ROOT,
                "databaseId": DATABASE_ID,
                "assetId": ASSET_ID,
                "relativePath": "/",
            }
        ],
        "inputMetadataS3Location": f"s3://{WORKFLOW_BUCKET}/{WORKFLOW_INPUTS_PREFIX}{pid}/metadata.json",
        "outputs": {
            "bucket": WORKFLOW_BUCKET,
            "files": f"{outputs_prefix}/files/",
            "previews": f"{outputs_prefix}/previews/",
            "metadata": f"{outputs_prefix}/metadata/",
            "results": f"{outputs_prefix}/results/",
        },
        "outputTarget": {"databaseId": DATABASE_ID, "assetId": OUTPUT_ASSET_ID},
        "auxBucket": WORKFLOW_BUCKET,
        "auxTempPrefix": f"pipelines/{PIPELINE_ID}/wfx1/",
        "systemConfig": {"orchestrationEventPrefix": f"vams.execution.wfx1.pipeline.{pid}"},
    }


def put_request_files(s3_client: Any, pid: str, training_config: dict[str, Any]) -> None:
    """Write the manifest, the training configuration, and the metadata of an execution."""
    documents = {
        "manifest.json": build_manifest(pid),
        "config.json": training_config,
        "metadata.json": {"metadata": {"assetName": "Pick and place", "tags": ["demo"]}},
    }
    for name, document in documents.items():
        s3_client.put_object(
            Bucket=WORKFLOW_BUCKET,
            Key=f"{WORKFLOW_INPUTS_PREFIX}{pid}/{name}",
            Body=json.dumps(document).encode(),
        )


def create_bucket(s3_client: Any, bucket: str, versioned: bool) -> None:
    """Create a bucket in the test region."""
    s3_client.create_bucket(
        Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": TEST_REGION}
    )
    if versioned:
        s3_client.put_bucket_versioning(
            Bucket=bucket, VersioningConfiguration={"Status": "Enabled"}
        )


def put_asset_files(s3_client: Any) -> None:
    """Write the asset files, a folder marker, and a preview file to the asset bucket."""
    for key, body in ASSET_FILES.items():
        s3_client.put_object(Bucket=ASSET_BUCKET, Key=key, Body=body)
    for key in IGNORED_ASSET_KEYS:
        s3_client.put_object(Bucket=ASSET_BUCKET, Key=key, Body=b"" if key.endswith("/") else b"g")


def materialize_fsx(dataset_root: str, objects: list[dict[str, Any]]) -> None:
    """Create the files that the FSx automatic import would show, with the listed sizes."""
    for entry in objects:
        path = Path(dataset_root) / entry["relativePath"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * entry["size"])


@pytest.fixture
def asset_bucket_versioned() -> bool:
    """Tell whether the asset bucket has versioning enabled."""
    return True


@pytest.fixture
def buckets(s3_client: Any, data_bucket: str, asset_bucket_versioned: bool) -> dict[str, str]:
    """Create the workflow, asset, and PASK data buckets with the asset files."""
    create_bucket(s3_client, WORKFLOW_BUCKET, versioned=False)
    create_bucket(s3_client, ASSET_BUCKET, versioned=asset_bucket_versioned)
    put_asset_files(s3_client)
    return {"workflow": WORKFLOW_BUCKET, "asset": ASSET_BUCKET, "data": data_bucket}


@pytest.fixture
def base_dir(tmp_path: Path) -> Path:
    """Return the LEROBOT_PI_BASE_DIR of the test."""
    return tmp_path / "lerobot-openpi"


@pytest.fixture
def fsx_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[str], str]:
    """Point ``config.fsx_dataset_root`` at a temporary directory and stub the FSx checks.

    Returns:
        Function that returns the dataset root of a pipelineExecutionId.
    """

    def dataset_root(pid: str) -> str:
        return f"{tmp_path / 'fsx' / 's3link' / 'vams' / pid}/"

    monkeypatch.setattr(config, "fsx_dataset_root", lambda cfg, pid: dataset_root(pid))
    monkeypatch.setattr(staging, "fsx_free_bytes", lambda mount_point: 10**15)
    monkeypatch.setattr(staging, "INGEST_POLL_INTERVAL_SECONDS", 0.01)
    return dataset_root


@pytest.fixture
def job_training_env(base_dir: Path) -> dict[str, str]:
    """Return training environment values whose directories are under the test directory."""
    return {
        "LEROBOT_PI_BASE_DIR": str(base_dir),
        "LEROBOT_PI_OUTPUT_DIR": str(base_dir / "outputs"),
        "HF_TOKEN": HF_TOKEN_VALUE,
        "MLFLOW_TRACKING_URI": "arn:aws:sagemaker:us-west-2:<account-id>:mlflow-app/test",
        "AWS_DEFAULT_REGION": TEST_REGION,
    }


@pytest.fixture
def store(dynamodb_client: Any, executions_table: str, fixed_clock: Any) -> ExecutionStore:
    """Return a store owned by ``OWNER``."""
    return ExecutionStore(
        executions_table,
        region=TEST_REGION,
        owner_id=OWNER,
        lease_seconds=900,
        ttl_days=30,
        clock=fixed_clock,
        dynamodb_client=dynamodb_client,
    )


def make_context(
    poller_config: PollerConfig,
    store: ExecutionStore,
    s3_client: Any,
    sfn: FakeSfn,
    slurm_runner: FakeSlurmRunner,
    training_env: dict[str, str],
    clock: Callable[[], float],
) -> PollerContext:
    """Build a poller context around the fakes and the moto clients."""
    return PollerContext(
        config=poller_config,
        store=store,
        sfn=sfn,
        slurm=SlurmClient(runner=slurm_runner),
        s3=s3_client,
        sqs=None,
        training_env=training_env,
        owner_id=OWNER,
        clock=clock,
    )


@pytest.fixture
def make_ctx(
    poller_config: PollerConfig,
    store: ExecutionStore,
    s3_client: Any,
    job_training_env: dict[str, str],
    fixed_clock: Any,
) -> Callable[..., PollerContext]:
    """Return a factory of poller contexts with a given fake Step Functions notifier."""

    def factory(
        sfn: FakeSfn,
        *,
        slurm_runner: FakeSlurmRunner | None = None,
        s3: Any = None,
        clock: Callable[[], float] | None = None,
        cfg: PollerConfig | None = None,
    ) -> PollerContext:
        return make_context(
            cfg or poller_config,
            store,
            s3 or s3_client,
            sfn,
            slurm_runner or FakeSlurmRunner("", 0, "1\n"),
            job_training_env,
            clock or fixed_clock,
        )

    return factory


def create_received_item(
    store: ExecutionStore, s3_client: Any, pid: str, training_config: dict[str, Any]
) -> ExecutionItem:
    """Write the request files, create the RECEIVED item, and take its lease."""
    put_request_files(s3_client, pid, training_config)
    assert store.create_received(item_fields(pid), TOKEN, SENT_AT)
    item = store.acquire_lease(pid)
    assert item is not None
    return item


def create_staging_item(
    store: ExecutionStore,
    s3_client: Any,
    ctx: PollerContext,
    pid: str,
    training_config: dict[str, Any],
) -> ExecutionItem:
    """Create an execution and move it to STAGING through ``process_received``."""
    staging.process_received(create_received_item(store, s3_client, pid, training_config), ctx)
    item = store.get(pid)
    assert item is not None and item.state is State.STAGING
    return item


def read_body(s3_client: Any, bucket: str, key: str) -> bytes:
    """Read an S3 object."""
    return s3_client.get_object(Bucket=bucket, Key=key)["Body"].read()


def test_received_moves_to_staging_with_paths(
    store: ExecutionStore, s3_client: Any, buckets: dict[str, str], make_ctx: Any, base_dir: Path
) -> None:
    sfn = FakeSfn(None)
    item = create_received_item(store, s3_client, PID, DEFAULT_TRAINING_CONFIG)

    staging.process_received(item, make_ctx(sfn))

    stored = store.get(PID)
    assert stored.state is State.STAGING
    assert stored.holdsSlot is True
    assert stored.stagingPrefix == f"vams/{PID}/"
    assert stored.runDir == f"{base_dir}/vams-runs/{PID}"
    assert stored.outputDir == f"{base_dir}/outputs/vams-{PID}"
    assert sfn.tokens == [TOKEN]


def test_received_stays_received_when_all_slots_are_taken(
    store: ExecutionStore,
    s3_client: Any,
    buckets: dict[str, str],
    make_ctx: Any,
    dynamodb_client: Any,
    executions_table: str,
) -> None:
    sfn = FakeSfn(None)
    ctx = make_ctx(sfn)
    create_staging_item(store, s3_client, ctx, OTHER_PID, DEFAULT_TRAINING_CONFIG)
    item = create_received_item(store, s3_client, PID, DEFAULT_TRAINING_CONFIG)

    staging.process_received(item, ctx)

    stored = store.get(PID)
    assert stored.state is State.RECEIVED
    assert stored.version == item.version
    assert not stored.holdsSlot
    assert sfn.tokens == [TOKEN, TOKEN]
    counter = dynamodb_client.get_item(
        TableName=executions_table, Key={"pipelineExecutionId": {"S": CONCURRENCY_ITEM_ID}}
    )["Item"]
    assert counter["active"]["N"] == "1"


def test_received_invalid_training_config_finalizes_failed(
    store: ExecutionStore, s3_client: Any, buckets: dict[str, str], make_ctx: Any
) -> None:
    item = create_received_item(store, s3_client, PID, {"EXTRA_ARGS": "--foo"})

    staging.process_received(item, make_ctx(FakeSfn(None)))

    stored = store.get(PID)
    assert stored.state is State.FINALIZING
    assert stored.outcome["status"] == "FAILED"
    assert stored.outcome["errorName"] == ErrorName.INVALID_REQUEST.value
    assert "EXTRA_ARGS" in stored.outcome["cause"]
    assert not stored.holdsSlot


def test_received_missing_manifest_finalizes_failed(
    store: ExecutionStore, s3_client: Any, buckets: dict[str, str], make_ctx: Any
) -> None:
    item = create_received_item(store, s3_client, PID, DEFAULT_TRAINING_CONFIG)
    s3_client.delete_object(
        Bucket=WORKFLOW_BUCKET, Key=f"{WORKFLOW_INPUTS_PREFIX}{PID}/manifest.json"
    )

    staging.process_received(item, make_ctx(FakeSfn(None)))

    stored = store.get(PID)
    assert stored.state is State.FINALIZING
    assert stored.outcome["errorName"] == ErrorName.INVALID_REQUEST.value


def test_received_closed_heartbeat_moves_to_abort_suspected(
    store: ExecutionStore, s3_client: Any, buckets: dict[str, str], make_ctx: Any
) -> None:
    item = create_received_item(store, s3_client, PID, DEFAULT_TRAINING_CONFIG)

    staging.process_received(item, make_ctx(FakeSfn(0)))

    stored = store.get(PID)
    assert stored.state is State.ABORT_SUSPECTED
    assert stored.prevState is State.RECEIVED


def test_input_manifest_lists_latest_versions_and_is_not_rebuilt(
    s3_client: Any, buckets: dict[str, str], tmp_path: Path
) -> None:
    asset_entry = build_manifest(PID)["inputFiles"][0]

    first = staging.resolve_input(s3_client, str(tmp_path), asset_entry).input_manifest
    s3_client.put_object(Bucket=ASSET_BUCKET, Key="asset-1/meta/info.json", Body=b"changed")
    s3_client.put_object(Bucket=ASSET_BUCKET, Key="asset-1/meta/new.json", Body=b"new")
    second = staging.resolve_input(s3_client, str(tmp_path), asset_entry).input_manifest

    assert second == first
    assert first["schemaVersion"] == 1
    assert first["bucket"] == ASSET_BUCKET
    assert first["assetRootS3Key"] == ASSET_ROOT
    assert first["versioned"] is True
    assert [entry["key"] for entry in first["objects"]] == sorted(ASSET_FILES)
    for entry in first["objects"]:
        head = s3_client.head_object(Bucket=ASSET_BUCKET, Key=entry["key"])
        assert entry["bucket"] == ASSET_BUCKET
        assert entry["relativePath"] == entry["key"][len(ASSET_ROOT) :]
        assert entry["size"] == len(ASSET_FILES[entry["key"]])
        assert entry["versionId"] != "null"
        assert not entry["etag"].startswith('"') and not entry["etag"].endswith('"')
        if entry["key"] != "asset-1/meta/info.json":
            assert entry["etag"] == head["ETag"].strip('"')
    written = json.loads((tmp_path / INPUT_MANIFEST_FILE).read_text(encoding="utf-8"))
    assert written == first
    assert [path.name for path in tmp_path.iterdir()] == [INPUT_MANIFEST_FILE]


def test_input_manifest_skips_deleted_files(
    s3_client: Any, buckets: dict[str, str], tmp_path: Path
) -> None:
    s3_client.delete_object(Bucket=ASSET_BUCKET, Key="asset-1/meta/stats.json")
    asset_entry = build_manifest(PID)["inputFiles"][0]

    document = staging.resolve_input(s3_client, str(tmp_path), asset_entry).input_manifest

    assert "asset-1/meta/stats.json" not in [entry["key"] for entry in document["objects"]]


@pytest.mark.parametrize("asset_bucket_versioned", [False])
def test_input_manifest_of_unversioned_bucket(
    s3_client: Any, buckets: dict[str, str], tmp_path: Path
) -> None:
    asset_entry = build_manifest(PID)["inputFiles"][0]

    document = staging.resolve_input(s3_client, str(tmp_path), asset_entry).input_manifest

    assert document["versioned"] is False
    assert {entry["versionId"] for entry in document["objects"]} == {"null"}


def test_dataset_digest_is_sha256_of_sorted_lines() -> None:
    objects = [
        {"key": "b/2", "versionId": "v2", "etag": "e2", "size": 20},
        {"key": "a/1", "versionId": "v1", "etag": "e1", "size": 10},
    ]
    expected = hashlib.sha256(b"a/1\tv1\te1\t10\nb/2\tv2\te2\t20\n").hexdigest()

    assert staging.dataset_digest(objects) == f"sha256:{expected}"
    assert staging.dataset_digest(list(reversed(objects))) == f"sha256:{expected}"
    changed = [dict(objects[0]), dict(objects[1], versionId="v1b")]
    assert staging.dataset_digest(changed) != f"sha256:{expected}"


@pytest.mark.parametrize(
    ("params", "expected_bytes"),
    [
        ({}, 6 * staging.GIB),
        ({"FINETUNE_MODE": "full", "STEPS": "1001", "SAVE_FREQ": "500"}, 3 * 40 * staging.GIB),
        ({"FINETUNE_MODE": "expert_only", "STEPS": "100", "SAVE_FREQ": "100"}, 16 * staging.GIB),
    ],
)
def test_estimate_checkpoint_bytes(params: dict[str, str], expected_bytes: int) -> None:
    assert staging.estimate_checkpoint_bytes(params) == expected_bytes


def test_staging_copies_listed_versions_and_moves_to_submitting(
    store: ExecutionStore,
    s3_client: Any,
    buckets: dict[str, str],
    make_ctx: Any,
    fsx_root: Callable[[str], str],
) -> None:
    sfn = FakeSfn(None)
    ctx = make_ctx(sfn)
    item = create_staging_item(store, s3_client, ctx, PID, DEFAULT_TRAINING_CONFIG)
    os.makedirs(item.runDir)
    input_manifest = staging.resolve_input(
        s3_client, item.runDir, build_manifest(PID)["inputFiles"][0]
    ).input_manifest
    s3_client.put_object(Bucket=ASSET_BUCKET, Key="asset-1/meta/info.json", Body=b"newer")
    materialize_fsx(fsx_root(PID), input_manifest["objects"])

    staging.process_staging(item, ctx)

    stored = store.get(PID)
    assert stored.state is State.SUBMITTING
    assert stored.jobName == f"vams-{PID}"
    for key, body in ASSET_FILES.items():
        staged_key = f"vams/{PID}/{key[len(ASSET_ROOT) :]}"
        assert read_body(s3_client, buckets["data"], staged_key) == body
    staged = s3_client.list_objects_v2(Bucket=buckets["data"], Prefix=f"vams/{PID}/")
    assert len(staged["Contents"]) == len(ASSET_FILES)

    context_document = json.loads(
        (Path(item.runDir) / VAMS_CONTEXT_FILE).read_text(encoding="utf-8")
    )
    assert context_document == {
        "schemaVersion": 1,
        "trigger": "vams",
        "workflowDatabaseId": DATABASE_ID,
        "workflowId": "wf1",
        "workflowExecutionId": "wfx1",
        "pipelineExecutionId": PID,
        "pipelineName": PIPELINE_ID,
        "executingUserName": "alice",
        "input": {
            "databaseId": DATABASE_ID,
            "assetId": ASSET_ID,
            "assetName": "Pick and place",
            "bucket": ASSET_BUCKET,
            "assetRootS3Key": ASSET_ROOT,
            "versioned": True,
        },
        "output": {"databaseId": DATABASE_ID, "assetId": OUTPUT_ASSET_ID},
        "datasetDigest": staging.dataset_digest(input_manifest["objects"]),
        "inputManifestFile": f"{item.runDir}/{INPUT_MANIFEST_FILE}",
        "resultFile": f"{item.runDir}/mlflow_run.json",
    }


@pytest.mark.parametrize("asset_bucket_versioned", [False])
def test_staging_copies_unversioned_objects(
    store: ExecutionStore,
    s3_client: Any,
    buckets: dict[str, str],
    make_ctx: Any,
    fsx_root: Callable[[str], str],
) -> None:
    ctx = make_ctx(FakeSfn(None))
    item = create_staging_item(store, s3_client, ctx, PID, DEFAULT_TRAINING_CONFIG)
    os.makedirs(item.runDir)
    input_manifest = staging.resolve_input(
        s3_client, item.runDir, build_manifest(PID)["inputFiles"][0]
    ).input_manifest
    materialize_fsx(fsx_root(PID), input_manifest["objects"])

    staging.process_staging(item, ctx)

    assert store.get(PID).state is State.SUBMITTING
    staged = read_body(s3_client, buckets["data"], f"vams/{PID}/meta/info.json")
    assert staged == ASSET_FILES["asset-1/meta/info.json"]
    context_document = json.loads((Path(item.runDir) / VAMS_CONTEXT_FILE).read_text())
    assert context_document["input"]["versioned"] is False


def test_staging_fails_when_import_wait_limit_passes(
    store: ExecutionStore,
    s3_client: Any,
    buckets: dict[str, str],
    make_ctx: Any,
    fsx_root: Callable[[str], str],
    poller_config: PollerConfig,
) -> None:
    ctx = make_ctx(FakeSfn(None))
    item = create_staging_item(store, s3_client, ctx, PID, DEFAULT_TRAINING_CONFIG)
    advancing_clock = AdvancingClock(FIXED_EPOCH_SECONDS, poller_config.ingest_wait_seconds + 1)

    staging.process_staging(item, make_ctx(FakeSfn(None), clock=advancing_clock))

    stored = store.get(PID)
    assert stored.state is State.FINALIZING
    assert stored.outcome["errorName"] == ErrorName.STAGING_FAILED.value
    assert not (Path(item.runDir) / VAMS_CONTEXT_FILE).exists()


def test_staging_fails_when_fsx_has_no_room(
    store: ExecutionStore,
    s3_client: Any,
    buckets: dict[str, str],
    make_ctx: Any,
    fsx_root: Callable[[str], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(FakeSfn(None))
    item = create_staging_item(store, s3_client, ctx, PID, DEFAULT_TRAINING_CONFIG)
    monkeypatch.setattr(staging, "fsx_free_bytes", lambda mount_point: staging.GIB)

    staging.process_staging(item, ctx)

    stored = store.get(PID)
    assert stored.state is State.FINALIZING
    assert stored.outcome["errorName"] == ErrorName.STAGING_FAILED.value
    staged = s3_client.list_objects_v2(Bucket=buckets["data"], Prefix=f"vams/{PID}/")
    assert staged["KeyCount"] == 0


def test_staging_stops_copy_when_heartbeat_is_closed(
    store: ExecutionStore,
    s3_client: Any,
    buckets: dict[str, str],
    make_ctx: Any,
    fsx_root: Callable[[str], str],
    poller_config: PollerConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = make_ctx(FakeSfn(None))
    item = create_staging_item(store, s3_client, ctx, PID, DEFAULT_TRAINING_CONFIG)
    monkeypatch.setattr(staging, "COPY_WORKERS", 1)
    slow_s3 = SlowCopyS3(s3_client, delay_seconds=0.3)
    fast_heartbeat_config = dataclasses.replace(poller_config, heartbeat_interval_seconds=0.02)

    staging.process_staging(item, make_ctx(FakeSfn(1), s3=slow_s3, cfg=fast_heartbeat_config))

    stored = store.get(PID)
    assert stored.state is State.ABORT_SUSPECTED
    assert stored.prevState is State.STAGING
    assert 1 <= len(slow_s3.copied_keys) < len(ASSET_FILES)
    assert not (Path(item.runDir) / VAMS_CONTEXT_FILE).exists()


def test_copy_error_classification(s3_client: Any, buckets: dict[str, str]) -> None:
    missing = {
        "bucket": ASSET_BUCKET,
        "key": "asset-1/missing.bin",
        "versionId": "null",
        "etag": "e",
        "size": 1,
        "relativePath": "missing.bin",
    }

    with pytest.raises(PermanentFailure) as raised:
        staging.copy_objects(s3_client, [missing], buckets["data"], "vams/x/", threading.Event())

    assert raised.value.error_name is ErrorName.STAGING_FAILED


class StubHeadS3:
    """S3 client whose ``head_object`` raises a fixed ``ClientError``."""

    def __init__(self, code: str, status: int) -> None:
        """Create the stub.

        Args:
            code: Error code of the raised error.
            status: HTTP status of the raised error.
        """
        self._error = ClientError(
            {
                "Error": {"Code": code, "Message": "stub"},
                "ResponseMetadata": {"HTTPStatusCode": status},
            },
            "HeadObject",
        )

    def head_object(self, **kwargs: Any) -> dict[str, Any]:
        """Raise the fixed error."""
        raise self._error


class SlowHeadS3:
    """S3 client wrapper whose ``head_object`` sleeps and counts the calls."""

    def __init__(self, client: Any, delay_seconds: float) -> None:
        """Wrap ``client``."""
        self._client = client
        self._delay_seconds = delay_seconds
        self.head_calls = 0

    def head_object(self, **kwargs: Any) -> dict[str, Any]:
        """Call ``head_object`` after a delay."""
        time.sleep(self._delay_seconds)
        self.head_calls += 1
        return self._client.head_object(**kwargs)

    def __getattr__(self, name: str) -> Any:
        """Delegate every other call to the wrapped client."""
        return getattr(self._client, name)


def put_source_files(s3_client: Any) -> dict[str, str]:
    """Write the source asset files of the view.

    Returns:
        Version id of every file by path.
    """
    return {
        path: s3_client.put_object(Bucket=ASSET_BUCKET, Key=SOURCE_ROOT + path, Body=body)[
            "VersionId"
        ]
        for path, body in SOURCE_FILES.items()
    }


def build_view_document(version_ids: dict[str, str]) -> dict[str, Any]:
    """Return the contract view manifest that pins the test source files.

    Args:
        version_ids: Version id of every source file by path.
    """
    document = copy.deepcopy(CONTRACT["examples"]["view"]["manifest"])
    source = document["source"]
    source["database_id"] = DATABASE_ID
    source["asset_id"] = SOURCE_ASSET_ID
    source["storage"] = {"bucket": ASSET_BUCKET, "asset_root_key": SOURCE_ROOT}
    source["files"] = [
        {"path": path, "version_id": version_ids[path], "size": len(body)}
        for path, body in SOURCE_FILES.items()
    ]
    return document


def put_view_asset(s3_client: Any, curation_manifest: bytes) -> None:
    """Turn the input asset into a view that holds only its manifest and statistics.

    Args:
        s3_client: S3 client.
        curation_manifest: Body of ``curation_manifest.json``.
    """
    for key in [*ASSET_FILES, *IGNORED_ASSET_KEYS]:
        s3_client.delete_object(Bucket=ASSET_BUCKET, Key=key)
    s3_client.put_object(
        Bucket=ASSET_BUCKET, Key=ASSET_ROOT + CURATION_MANIFEST_FILE, Body=curation_manifest
    )
    s3_client.put_object(
        Bucket=ASSET_BUCKET, Key=ASSET_ROOT + "meta/stats.json", Body=VIEW_STATS_BODY
    )


def put_view(s3_client: Any) -> dict[str, Any]:
    """Write the source files and a view that pins them.

    Returns:
        The view manifest document.
    """
    document = build_view_document(put_source_files(s3_client))
    put_view_asset(s3_client, json.dumps(document).encode())
    return document


def source_objects_on_fsx() -> list[dict[str, Any]]:
    """Return the relative paths and sizes of the source files as FSx would show them."""
    return [{"relativePath": path, "size": len(body)} for path, body in SOURCE_FILES.items()]


def read_run_file(run_directory: str, name: str) -> Any:
    """Read a JSON file of the run directory."""
    return json.loads((Path(run_directory) / name).read_text(encoding="utf-8"))


def assert_failed(store: ExecutionStore, error_name: ErrorName) -> str:
    """Check that the execution failed with an error name, and return the cause."""
    stored = store.get(PID)
    assert stored.state is State.FINALIZING
    assert stored.outcome["status"] == "FAILED"
    assert stored.outcome["errorName"] == error_name.value
    return stored.outcome["cause"]


def test_staging_rejects_dataset_without_info_json(
    store: ExecutionStore,
    s3_client: Any,
    buckets: dict[str, str],
    make_ctx: Any,
    fsx_root: Callable[[str], str],
) -> None:
    ctx = make_ctx(FakeSfn(None))
    item = create_staging_item(store, s3_client, ctx, PID, DEFAULT_TRAINING_CONFIG)
    s3_client.delete_object(Bucket=ASSET_BUCKET, Key="asset-1/meta/info.json")

    staging.process_staging(item, ctx)

    assert assert_failed(store, ErrorName.INVALID_REQUEST) == "asset root has no meta/info.json"
    assert not (Path(item.runDir) / INPUT_MANIFEST_FILE).exists()


def test_staging_view_copies_recorded_versions(
    store: ExecutionStore,
    s3_client: Any,
    buckets: dict[str, str],
    make_ctx: Any,
    fsx_root: Callable[[str], str],
) -> None:
    document = put_view(s3_client)
    ctx = make_ctx(FakeSfn(None))
    item = create_staging_item(store, s3_client, ctx, PID, DEFAULT_TRAINING_CONFIG)
    s3_client.put_object(
        Bucket=ASSET_BUCKET, Key=SOURCE_ROOT + "data/chunk-000/file-000.parquet", Body=b"newer-data"
    )
    materialize_fsx(fsx_root(PID), source_objects_on_fsx())

    staging.process_staging(item, ctx)

    assert store.get(PID).state is State.SUBMITTING
    for path, body in SOURCE_FILES.items():
        assert read_body(s3_client, buckets["data"], f"vams/{PID}/{path}") == body
    staged = s3_client.list_objects_v2(Bucket=buckets["data"], Prefix=f"vams/{PID}/")
    assert sorted(entry["Key"] for entry in staged["Contents"]) == sorted(
        f"vams/{PID}/{path}" for path in SOURCE_FILES
    )

    input_manifest = read_run_file(item.runDir, INPUT_MANIFEST_FILE)
    source_versions = {entry["path"]: entry["version_id"] for entry in document["source"]["files"]}
    assert input_manifest["schemaVersion"] == 2
    assert input_manifest["kind"] == "view"
    assert input_manifest["bucket"] == ASSET_BUCKET
    assert input_manifest["assetRootS3Key"] == ASSET_ROOT
    assert input_manifest["versioned"] is True
    assert input_manifest["episodes"] == [0, 2, 4]
    assert input_manifest["source"] == {
        "databaseId": DATABASE_ID,
        "assetId": SOURCE_ASSET_ID,
        "assetVersion": "1",
        "bucket": ASSET_BUCKET,
        "assetRootS3Key": SOURCE_ROOT,
    }
    assert [entry["relativePath"] for entry in input_manifest["objects"]] == sorted(SOURCE_FILES)
    for entry in input_manifest["objects"]:
        path = entry["relativePath"]
        head = s3_client.head_object(
            Bucket=ASSET_BUCKET, Key=SOURCE_ROOT + path, VersionId=source_versions[path]
        )
        assert entry == {
            "bucket": ASSET_BUCKET,
            "key": SOURCE_ROOT + path,
            "versionId": source_versions[path],
            "etag": head["ETag"].strip('"'),
            "size": len(SOURCE_FILES[path]),
            "relativePath": path,
        }
    assert [entry["relativePath"] for entry in input_manifest["view"]["objects"]] == [
        CURATION_MANIFEST_FILE,
        "meta/stats.json",
    ]

    context_document = read_run_file(item.runDir, VAMS_CONTEXT_FILE)
    expected_digest = staging.dataset_digest(
        input_manifest["objects"] + input_manifest["view"]["objects"]
    )
    assert expected_digest != staging.dataset_digest(input_manifest["objects"])
    assert context_document["schemaVersion"] == 2
    assert context_document["datasetDigest"] == expected_digest
    assert context_document["input"] == {
        "databaseId": DATABASE_ID,
        "assetId": ASSET_ID,
        "assetName": "Pick and place",
        "bucket": ASSET_BUCKET,
        "assetRootS3Key": ASSET_ROOT,
        "versioned": True,
        "kind": "view",
    }
    assert context_document["view"] == {
        "manifestFile": f"{item.runDir}/{CURATION_MANIFEST_FILE}",
        "createdAt": document["created_at"],
        "reviewUpdatedAt": document["source"]["review_updated_at"],
        "episodeCount": 3,
        "excludedEpisodeCount": 2,
        "source": input_manifest["source"],
    }
    assert context_document["inputManifestFile"] == f"{item.runDir}/{INPUT_MANIFEST_FILE}"

    assert read_run_file(item.runDir, CURATION_MANIFEST_FILE) == document
    view_root = Path(item.runDir) / VIEW_DATASET_DIR
    staged_root = Path(fsx_root(PID))
    assert (view_root / "meta").is_dir() and not (view_root / "meta").is_symlink()
    assert os.readlink(view_root / "meta" / "info.json") == str(staged_root / "meta" / "info.json")
    assert os.readlink(view_root / "data") == str(staged_root / "data")
    assert os.readlink(view_root / "videos") == str(staged_root / "videos")
    assert not (view_root / "meta" / "stats.json").exists()


def test_staging_view_context_leaves_out_missing_optional_fields(
    store: ExecutionStore,
    s3_client: Any,
    buckets: dict[str, str],
    make_ctx: Any,
    fsx_root: Callable[[str], str],
) -> None:
    document = build_view_document(put_source_files(s3_client))
    document["created_at"] = None
    document["source"]["asset_version"] = None
    document["source"]["review_updated_at"] = None
    put_view_asset(s3_client, json.dumps(document).encode())
    ctx = make_ctx(FakeSfn(None))
    item = create_staging_item(store, s3_client, ctx, PID, DEFAULT_TRAINING_CONFIG)
    materialize_fsx(fsx_root(PID), source_objects_on_fsx())

    staging.process_staging(item, ctx)

    context_view = read_run_file(item.runDir, VAMS_CONTEXT_FILE)["view"]
    assert "createdAt" not in context_view
    assert "reviewUpdatedAt" not in context_view
    assert "assetVersion" not in context_view["source"]
    assert "assetVersion" not in read_run_file(item.runDir, INPUT_MANIFEST_FILE)["source"]


def test_staging_view_fails_when_recorded_version_is_gone(
    store: ExecutionStore,
    s3_client: Any,
    buckets: dict[str, str],
    make_ctx: Any,
    fsx_root: Callable[[str], str],
) -> None:
    document = put_view(s3_client)
    removed_path = "data/chunk-000/file-000.parquet"
    removed_version = next(
        entry["version_id"]
        for entry in document["source"]["files"]
        if entry["path"] == removed_path
    )
    s3_client.delete_object(
        Bucket=ASSET_BUCKET, Key=SOURCE_ROOT + removed_path, VersionId=removed_version
    )
    ctx = make_ctx(FakeSfn(None))
    item = create_staging_item(store, s3_client, ctx, PID, DEFAULT_TRAINING_CONFIG)

    staging.process_staging(item, ctx)

    cause = assert_failed(store, ErrorName.INVALID_REQUEST)
    assert cause == f"view source file {removed_path} version is no longer available"
    assert removed_version not in cause and ASSET_BUCKET not in cause
    assert not (Path(item.runDir) / INPUT_MANIFEST_FILE).exists()
    staged = s3_client.list_objects_v2(Bucket=buckets["data"], Prefix=f"vams/{PID}/")
    assert staged["KeyCount"] == 0


def test_staging_view_fails_when_size_differs(
    store: ExecutionStore,
    s3_client: Any,
    buckets: dict[str, str],
    make_ctx: Any,
    fsx_root: Callable[[str], str],
) -> None:
    document = build_view_document(put_source_files(s3_client))
    document["source"]["files"][0]["size"] += 1
    put_view_asset(s3_client, json.dumps(document).encode())
    ctx = make_ctx(FakeSfn(None))
    item = create_staging_item(store, s3_client, ctx, PID, DEFAULT_TRAINING_CONFIG)

    staging.process_staging(item, ctx)

    cause = assert_failed(store, ErrorName.INVALID_REQUEST)
    assert document["source"]["files"][0]["path"] in cause
    assert not (Path(item.runDir) / INPUT_MANIFEST_FILE).exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [("database_id", "other-db"), ("bucket", "vams-other-assets")],
)
def test_staging_view_rejects_source_in_another_location(
    store: ExecutionStore,
    s3_client: Any,
    buckets: dict[str, str],
    make_ctx: Any,
    fsx_root: Callable[[str], str],
    field: str,
    value: str,
) -> None:
    document = build_view_document(put_source_files(s3_client))
    if field == "bucket":
        document["source"]["storage"]["bucket"] = value
    else:
        document["source"][field] = value
    put_view_asset(s3_client, json.dumps(document).encode())
    ctx = make_ctx(FakeSfn(None))
    item = create_staging_item(store, s3_client, ctx, PID, DEFAULT_TRAINING_CONFIG)

    staging.process_staging(item, ctx)

    assert assert_failed(store, ErrorName.INVALID_REQUEST) == view.SAME_LOCATION_CAUSE


@pytest.mark.parametrize(
    "curation_manifest",
    [b"{not json", json.dumps({"curation_manifest_version": 1, "mode": "materialize"}).encode()],
)
def test_staging_rejects_unusable_curation_manifest(
    store: ExecutionStore,
    s3_client: Any,
    buckets: dict[str, str],
    make_ctx: Any,
    fsx_root: Callable[[str], str],
    curation_manifest: bytes,
) -> None:
    s3_client.put_object(
        Bucket=ASSET_BUCKET, Key=ASSET_ROOT + CURATION_MANIFEST_FILE, Body=curation_manifest
    )
    ctx = make_ctx(FakeSfn(None))
    item = create_staging_item(store, s3_client, ctx, PID, DEFAULT_TRAINING_CONFIG)

    staging.process_staging(item, ctx)

    assert_failed(store, ErrorName.INVALID_REQUEST)
    assert not (Path(item.runDir) / INPUT_MANIFEST_FILE).exists()


def test_staging_treats_materialize_version_2_as_dataset(
    store: ExecutionStore,
    s3_client: Any,
    buckets: dict[str, str],
    make_ctx: Any,
    fsx_root: Callable[[str], str],
) -> None:
    materialize = CONTRACT["examples"]["materialize"]["manifest"]
    assert materialize["curation_manifest_version"] == 2 and materialize["mode"] == "materialize"
    s3_client.put_object(
        Bucket=ASSET_BUCKET,
        Key=ASSET_ROOT + CURATION_MANIFEST_FILE,
        Body=json.dumps(materialize).encode(),
    )
    ctx = make_ctx(FakeSfn(None))
    item = create_staging_item(store, s3_client, ctx, PID, DEFAULT_TRAINING_CONFIG)
    os.makedirs(item.runDir)
    input_manifest = staging.resolve_input(
        s3_client, item.runDir, build_manifest(PID)["inputFiles"][0]
    ).input_manifest
    materialize_fsx(fsx_root(PID), input_manifest["objects"])

    staging.process_staging(item, ctx)

    assert store.get(PID).state is State.SUBMITTING
    assert input_manifest["schemaVersion"] == 1 and "kind" not in input_manifest
    context_document = read_run_file(item.runDir, VAMS_CONTEXT_FILE)
    assert context_document["schemaVersion"] == 1
    assert "view" not in context_document and "kind" not in context_document["input"]
    assert not (Path(item.runDir) / VIEW_DATASET_DIR).exists()


def test_retried_view_staging_keeps_input_manifest_and_recorded_manifest(
    store: ExecutionStore,
    s3_client: Any,
    buckets: dict[str, str],
    make_ctx: Any,
    fsx_root: Callable[[str], str],
) -> None:
    document = put_view(s3_client)
    ctx = make_ctx(FakeSfn(None))
    item = create_staging_item(store, s3_client, ctx, PID, DEFAULT_TRAINING_CONFIG)
    os.makedirs(item.runDir)
    asset_entry = build_manifest(PID)["inputFiles"][0]
    resolved = staging.resolve_input(s3_client, item.runDir, asset_entry)
    first = staging.build_view_input_manifest(
        s3_client, item.runDir, asset_entry, resolved, threading.Event()
    )
    changed = copy.deepcopy(document)
    changed["episodes"] = [0, 2]
    s3_client.put_object(
        Bucket=ASSET_BUCKET,
        Key=ASSET_ROOT + CURATION_MANIFEST_FILE,
        Body=json.dumps(changed).encode(),
    )
    materialize_fsx(fsx_root(PID), source_objects_on_fsx())

    staging.process_staging(item, ctx)

    assert store.get(PID).state is State.SUBMITTING
    assert read_run_file(item.runDir, INPUT_MANIFEST_FILE) == first
    assert read_run_file(item.runDir, CURATION_MANIFEST_FILE) == document
    assert read_run_file(item.runDir, VAMS_CONTEXT_FILE)["view"]["episodeCount"] == 3


def test_staging_stops_view_resolution_when_heartbeat_is_closed(
    store: ExecutionStore,
    s3_client: Any,
    buckets: dict[str, str],
    make_ctx: Any,
    fsx_root: Callable[[str], str],
    poller_config: PollerConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    put_view(s3_client)
    ctx = make_ctx(FakeSfn(None))
    item = create_staging_item(store, s3_client, ctx, PID, DEFAULT_TRAINING_CONFIG)
    monkeypatch.setattr(staging, "COPY_WORKERS", 1)
    slow_s3 = SlowHeadS3(s3_client, delay_seconds=0.3)
    fast_heartbeat_config = dataclasses.replace(poller_config, heartbeat_interval_seconds=0.02)

    staging.process_staging(item, make_ctx(FakeSfn(1), s3=slow_s3, cfg=fast_heartbeat_config))

    stored = store.get(PID)
    assert stored.state is State.ABORT_SUSPECTED
    assert 1 <= slow_s3.head_calls < len(SOURCE_FILES)
    assert not (Path(item.runDir) / INPUT_MANIFEST_FILE).exists()
    staged = s3_client.list_objects_v2(Bucket=buckets["data"], Prefix=f"vams/{PID}/")
    assert staged["KeyCount"] == 0


def contract_view_manifest() -> view.ViewManifest:
    """Return the validated contract view manifest."""
    document = CONTRACT["examples"]["view"]["manifest"]
    storage = document["source"]["storage"]
    return view.parse_view_manifest(
        document,
        input_bucket=storage["bucket"],
        input_database_id=document["source"]["database_id"],
    )


@pytest.mark.parametrize(
    ("code", "status", "error_name"),
    [
        ("NoSuchVersion", 404, ErrorName.INVALID_REQUEST),
        ("404", 404, ErrorName.INVALID_REQUEST),
        ("InvalidArgument", 400, ErrorName.INVALID_REQUEST),
        ("400", 400, ErrorName.INVALID_REQUEST),
        ("AccessDenied", 403, ErrorName.STAGING_FAILED),
        ("403", 403, ErrorName.STAGING_FAILED),
        ("InvalidObjectState", 409, ErrorName.STAGING_FAILED),
    ],
)
def test_view_source_head_permanent_errors(code: str, status: int, error_name: ErrorName) -> None:
    manifest = contract_view_manifest()
    source_file = manifest.files[0]

    with pytest.raises(PermanentFailure) as raised:
        staging.head_view_source_file(StubHeadS3(code, status), manifest, source_file)

    assert raised.value.error_name is error_name
    assert source_file.path in raised.value.cause
    assert source_file.version_id not in raised.value.cause
    assert manifest.bucket not in raised.value.cause
    if status == 403:
        assert staging.SOURCE_ACCESS_DENIED_CAUSE in raised.value.cause


@pytest.mark.parametrize(
    ("code", "status"), [("SlowDown", 503), ("InternalError", 500), ("ServiceUnavailable", 503)]
)
def test_view_source_head_transient_errors(code: str, status: int) -> None:
    manifest = contract_view_manifest()

    with pytest.raises(TransientError):
        staging.head_view_source_file(StubHeadS3(code, status), manifest, manifest.files[0])
