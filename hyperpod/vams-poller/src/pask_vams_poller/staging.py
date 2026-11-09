"""RECEIVED and STAGING handlers: validate the request, then stage the input dataset on FSx.

RECEIVED reads and validates the manifest, the training configuration, and the metadata,
then takes a concurrency slot and moves to STAGING. STAGING lists the latest versions of
the asset files once (``input_manifest.json``), copies those versions to the staging
prefix of the PASK data bucket, waits until the FSx automatic import shows every file,
writes ``vams_context.json``, and moves to SUBMITTING.

STAGING tells the kind of the input from the listing. An asset with ``curation_manifest.json``
of mode ``view`` at its root is a curation view: the files to stage are the source asset
files that the view pins by S3 version, checked with ``HeadObject`` and recorded in
``input_manifest.json`` (``schemaVersion`` 2). The view's own files are not staged. After the
import, the poller writes the curation manifest to the run directory and builds the view
dataset root there (symbolic links to the staged files), and ``vams_context.json`` gets
``schemaVersion`` 2 with ``view``. Any other asset is a dataset and must hold
``meta/info.json`` at its root.

The helpers ``send_heartbeat``, ``run_state_handler``, and ``load_vams_input`` are also
used by the SUBMITTING handler.
"""

import hashlib
import json
import logging
import math
import os
import tempfile
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NoReturn

from boto3.s3.transfer import TransferConfig
from botocore.exceptions import ClientError, ConnectionError, HTTPClientError

from pask_vams_poller import config, messages, view
from pask_vams_poller.constants import (
    CURATION_MANIFEST_FILE,
    INPUT_KIND_DATASET,
    INPUT_KIND_VIEW,
    INPUT_MANIFEST_FILE,
    MLFLOW_RUN_FILE,
    VAMS_CONTEXT_FILE,
    VIEW_DATASET_DIR,
    ErrorName,
    State,
)
from pask_vams_poller.errors import PermanentFailure, StaleItemError, TransientError
from pask_vams_poller.keepalive import Keepalive
from pask_vams_poller.sfn import CallResult, call_with_latest_token

if TYPE_CHECKING:
    from pask_vams_poller.context import PollerContext
    from pask_vams_poller.store import ExecutionItem

logger = logging.getLogger(__name__)

INPUT_MANIFEST_SCHEMA_VERSION = 1
VIEW_INPUT_MANIFEST_SCHEMA_VERSION = 2
VAMS_CONTEXT_SCHEMA_VERSION = 1
VIEW_VAMS_CONTEXT_SCHEMA_VERSION = 2
VAMS_TRIGGER = "vams"
DATASET_DIGEST_PREFIX = "sha256:"
UNVERSIONED_VERSION_ID = "null"

FOLDER_MARKER_SUFFIX = "/"
PREVIEW_FILE_MARKER = ".previewFile."
RELATIVE_PATH_FORBIDDEN_SEGMENTS: frozenset[str] = frozenset({"", ".", ".."})
DATASET_INFO_FILE = "meta/info.json"

FSX_MOUNT_POINT = "/fsx"
INGEST_POLL_INTERVAL_SECONDS = 10.0

COPY_WORKERS = 8
GIB = 1024**3
MIB = 1024**2
COPY_TRANSFER_CONFIG = TransferConfig(
    multipart_threshold=5 * GIB,
    multipart_chunksize=512 * MIB,
    max_concurrency=4,
)

TRANSIENT_S3_ERROR_CODES: frozenset[str] = frozenset(
    {
        "SlowDown",
        "Throttling",
        "ThrottlingException",
        "RequestTimeout",
        "RequestLimitExceeded",
        "InternalError",
        "ServiceUnavailable",
    }
)
SERVER_ERROR_STATUS = 500

# HeadObject has no response body, so botocore reports the HTTP status as the error code.
VERSION_UNAVAILABLE_ERROR_CODES: frozenset[str] = frozenset({"NoSuchVersion", "NoSuchKey", "404"})
NOT_FOUND_STATUS = 404
INVALID_VERSION_ERROR_CODES: frozenset[str] = frozenset({"InvalidArgument", "400"})
BAD_REQUEST_STATUS = 400
ACCESS_DENIED_ERROR_CODES: frozenset[str] = frozenset({"AccessDenied", "403"})
FORBIDDEN_STATUS = 403
SOURCE_ACCESS_DENIED_CAUSE = "the poller role cannot read the source asset of the view"

# Free space check on FSx before the copy:
#   required_bytes = dataset_bytes + checkpoint_count * CHECKPOINT_BYTES_BY_MODE[FINETUNE_MODE]
#   checkpoint_count = ceil(STEPS / SAVE_FREQ)
# LeRobot saves a checkpoint every SAVE_FREQ steps and at the last step, and keeps all of
# them. The dataset counts in full because the imported files are read (and so loaded
# from S3 onto FSx) by the training job. pi0 and pi0.5 have about 3.3B parameters, and
# one checkpoint holds pretrained_model (weights in float32) and training_state
# (optimizer state of the trained parameters):
#   lora:        adapter and its optimizer state. Measured about 16 MB at LORA_R=16;
#                1 GiB leaves room for larger ranks.
#   expert_only: full weights (~13 GiB) + AdamW moments of the action expert (~3 GiB).
#   full:        full weights (~13 GiB) + AdamW moments of all parameters (~26 GiB).
CHECKPOINT_BYTES_BY_MODE: dict[str, int] = {
    "lora": 1 * GIB,
    "expert_only": 16 * GIB,
    "full": 40 * GIB,
}
# Values that slurm_train.sh uses when the training configuration omits the parameter.
SLURM_TRAIN_DEFAULT_FINETUNE_MODE = "lora"
SLURM_TRAIN_DEFAULT_STEPS = 30000
SLURM_TRAIN_DEFAULT_SAVE_FREQ = 5000


@dataclass(frozen=True)
class VamsInput:
    """The validated request files of one execution.

    Attributes:
        manifest: The parsed manifest.
        asset_entry: The whole-asset ``inputFiles`` entry.
        training_params: Training parameters that passed the allow list.
        metadata: ``assetName``, ``description``, and ``tags`` when present.
    """

    manifest: messages.Manifest
    asset_entry: dict[str, Any]
    training_params: dict[str, str]
    metadata: dict[str, Any]


@dataclass(frozen=True)
class ResolvedInput:
    """The kind of the input of one execution and its ``input_manifest.json``.

    Attributes:
        input_manifest: The input manifest document. ``None`` for a view whose source files
            are not resolved yet; ``build_view_input_manifest`` writes it.
        view_manifest: The validated curation view manifest, or ``None`` for a dataset.
        view_objects: Latest versions of the view asset files, set only while
            ``input_manifest`` is ``None``.
    """

    input_manifest: dict[str, Any] | None
    view_manifest: view.ViewManifest | None
    view_objects: list[dict[str, Any]] | None


def send_heartbeat(item: "ExecutionItem", ctx: "PollerContext") -> bool:
    """Send a heartbeat with the latest task token, and suspect an abort when it is closed.

    Args:
        item: Execution as last read by the caller.
        ctx: Poller context.

    Returns:
        True when the heartbeat was accepted. False when the latest token is closed; the
        execution was then moved to ABORT_SUSPECTED and the caller must stop.

    Raises:
        ValueError: The execution has no task token.
        StaleItemError: The move to ABORT_SUSPECTED failed its condition.
        TransientError: The heartbeat failed temporarily.
    """
    if item.taskToken is None:
        raise ValueError(f"{item.pipelineExecutionId} has no task token")
    outcome = call_with_latest_token(
        ctx.sfn.heartbeat, item.taskToken, lambda: _read_latest_token(item, ctx)
    )
    if outcome.result is CallResult.ACCEPTED:
        return True
    logger.warning("Task token of %s is closed; suspecting an abort", item.pipelineExecutionId)
    ctx.store.to_abort_suspected(item, outcome.token)
    return False


def _read_latest_token(item: "ExecutionItem", ctx: "PollerContext") -> str | None:
    """Read the latest task token of an execution with a strongly consistent read.

    Args:
        item: Execution whose token to read.
        ctx: Poller context.

    Returns:
        The stored token, or None when the item no longer exists.
    """
    latest = ctx.store.get(item.pipelineExecutionId)
    return None if latest is None else latest.taskToken


def run_state_handler(
    item: "ExecutionItem",
    ctx: "PollerContext",
    handler: Callable[["ExecutionItem", "PollerContext"], None],
) -> None:
    """Run a state handler body with the shared handling of stale items and failures.

    ``StaleItemError`` ends the handler; the reconciler reads the item again in its next
    cycle. ``PermanentFailure`` moves the execution to FINALIZING with a FAILED outcome.

    Args:
        item: Execution as last read by the caller.
        ctx: Poller context.
        handler: Body of the state handler.
    """
    try:
        handler(item, ctx)
    except StaleItemError as error:
        logger.info("Stopped handling %s: %s", item.pipelineExecutionId, error)
    except PermanentFailure as failure:
        logger.warning("Execution %s failed: %s", item.pipelineExecutionId, failure)
        _finalize_failed(item, ctx, failure)


def _finalize_failed(
    item: "ExecutionItem", ctx: "PollerContext", failure: PermanentFailure
) -> None:
    """Move an execution to FINALIZING with the FAILED outcome of a permanent failure.

    Args:
        item: Execution as last read by the caller.
        ctx: Poller context.
        failure: The failure that decides the outcome.
    """
    outcome = {"status": "FAILED", "errorName": failure.error_name.value, "cause": failure.cause}
    try:
        ctx.store.to_finalizing(item, outcome)
    except StaleItemError as error:
        logger.info("Could not record the failure of %s: %s", item.pipelineExecutionId, error)


def _s3_failure(error: ClientError, action: str, error_name: ErrorName) -> Exception:
    """Classify an S3 error left after botocore's retries.

    Args:
        error: The error.
        action: Description of the failed call, used in the message.
        error_name: Error name of a permanent failure.

    Returns:
        ``TransientError`` for throttling and 5xx, otherwise ``PermanentFailure``.
    """
    code = error.response.get("Error", {}).get("Code", "")
    status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
    if code in TRANSIENT_S3_ERROR_CODES or status >= SERVER_ERROR_STATUS:
        return TransientError(f"{action} failed temporarily: {code or status}")
    return PermanentFailure(error_name, f"{action} failed: {code or status}")


def _load_json(ctx: "PollerContext", uri: str) -> dict[str, Any]:
    """Read one request file from S3 and classify the S3 errors.

    Args:
        ctx: Poller context.
        uri: ``s3://bucket/key`` of the file.

    Returns:
        The decoded JSON object.

    Raises:
        PermanentFailure: ``PASK.InvalidRequest`` for a missing, unreadable, or invalid file.
        TransientError: Throttling, 5xx, or a connection error.
    """
    try:
        return messages.load_json_from_s3(ctx.s3, uri)
    except ClientError as error:
        raise _s3_failure(error, f"reading {uri}", ErrorName.INVALID_REQUEST) from error
    except (ConnectionError, HTTPClientError) as error:
        raise TransientError(f"reading {uri} failed: {error}") from error


def load_vams_input(item: "ExecutionItem", ctx: "PollerContext") -> VamsInput:
    """Read and validate the manifest, the training configuration, and the metadata.

    Args:
        item: Execution with the S3 locations of the request files.
        ctx: Poller context.

    Returns:
        The validated request files.

    Raises:
        PermanentFailure: ``PASK.InvalidRequest`` when a file is missing or invalid, or
            the manifest names another pipelineExecutionId.
        TransientError: S3 failed temporarily.
    """
    if not item.inputManifestS3Location or not item.inputConfigurationS3Location:
        raise PermanentFailure(
            ErrorName.INVALID_REQUEST, "execution has no manifest or configuration location"
        )
    manifest = messages.parse_manifest(_load_json(ctx, item.inputManifestS3Location))
    if messages.pipeline_execution_id(manifest) != item.pipelineExecutionId:
        raise PermanentFailure(
            ErrorName.INVALID_REQUEST, "manifest names another pipelineExecutionId"
        )
    messages.pipeline_name(manifest)
    asset_entry = messages.whole_asset_input(manifest)
    training_params = messages.validate_training_params(
        _load_json(ctx, item.inputConfigurationS3Location)
    )
    metadata: dict[str, Any] = {}
    if item.inputMetadataS3Location:
        metadata = messages.parse_metadata(_load_json(ctx, item.inputMetadataS3Location))
    return VamsInput(
        manifest=manifest,
        asset_entry=asset_entry,
        training_params=training_params,
        metadata=metadata,
    )


def process_received(item: "ExecutionItem", ctx: "PollerContext") -> None:
    """Handle an execution in RECEIVED.

    Sends a heartbeat (also while the execution waits for a concurrency slot), validates
    the request files, and moves to STAGING when a slot is free. Without a free slot the
    execution stays in RECEIVED.

    Args:
        item: Execution in RECEIVED whose lease this poller holds.
        ctx: Poller context.
    """
    run_state_handler(item, ctx, _receive)


def _receive(item: "ExecutionItem", ctx: "PollerContext") -> None:
    """Body of ``process_received``.

    Args:
        item: Execution in RECEIVED.
        ctx: Poller context.

    Raises:
        ValueError: The execution is not in RECEIVED.
        PermanentFailure: The request is invalid.
        StaleItemError: The execution changed.
    """
    _require_state(item, State.RECEIVED)
    if not send_heartbeat(item, ctx):
        return
    load_vams_input(item, ctx)
    pid = item.pipelineExecutionId
    paths = {
        "stagingPrefix": config.staging_prefix(ctx.config, pid),
        "runDir": config.run_dir(ctx.training_env, pid),
        "outputDir": config.output_dir(ctx.training_env, pid),
    }
    if not ctx.store.start_staging(item, paths, ctx.config.max_concurrent_runs):
        logger.info("All concurrency slots are taken; %s stays RECEIVED", pid)
        return
    logger.info("Execution %s moved to STAGING", pid)


def process_staging(item: "ExecutionItem", ctx: "PollerContext") -> None:
    """Handle an execution in STAGING.

    Args:
        item: Execution in STAGING whose lease this poller holds.
        ctx: Poller context.
    """
    run_state_handler(item, ctx, _stage)


def _stage(item: "ExecutionItem", ctx: "PollerContext") -> None:
    """Body of ``process_staging``.

    Args:
        item: Execution in STAGING.
        ctx: Poller context.

    Raises:
        ValueError: The execution is not in STAGING or has no run directory or prefix.
        PermanentFailure: The copy or the import failed for good (``PASK.StagingFailed``)
            or the request is invalid (``PASK.InvalidRequest``).
        TransientError: S3 or Step Functions failed temporarily.
        StaleItemError: The execution changed, the lease was lost, or an abort is suspected.
    """
    _require_state(item, State.STAGING)
    if not item.runDir or not item.stagingPrefix:
        raise ValueError(f"{item.pipelineExecutionId} has no runDir or stagingPrefix")
    if not send_heartbeat(item, ctx):
        return
    vams_input = load_vams_input(item, ctx)
    os.makedirs(item.runDir, exist_ok=True)
    resolved = resolve_input(ctx.s3, item.runDir, vams_input.asset_entry)
    if resolved.input_manifest is not None:
        check_fsx_capacity(
            resolved.input_manifest["objects"],
            vams_input.training_params,
            fsx_free_bytes(FSX_MOUNT_POINT),
        )

    keeper = _LeaseKeeper(item, ctx)
    with Keepalive(keeper, ctx.config.heartbeat_interval_seconds) as keepalive:
        input_manifest = _stage_files(item, ctx, vams_input, resolved, keepalive.should_stop)
    if input_manifest is None or keepalive.should_stop.is_set():
        _raise_keepalive_stop(item, keeper, keepalive)
    if resolved.view_manifest is not None:
        prepare_view_run_directory(ctx, item, resolved.view_manifest)

    digest = input_manifest_digest(input_manifest)
    context_document = build_vams_context(
        item, vams_input, input_manifest, digest, view_manifest=resolved.view_manifest
    )
    write_json_atomically(os.path.join(item.runDir, VAMS_CONTEXT_FILE), context_document)
    ctx.store.transition(
        item, State.SUBMITTING, set_attrs={"jobName": config.job_name(item.pipelineExecutionId)}
    )
    logger.info("Execution %s moved to SUBMITTING", item.pipelineExecutionId)


def _stage_files(
    item: "ExecutionItem",
    ctx: "PollerContext",
    vams_input: VamsInput,
    resolved: ResolvedInput,
    should_stop: threading.Event,
) -> dict[str, Any] | None:
    """Resolve the view source files when needed, then copy the files and wait for the import.

    Runs inside the keepalive of ``_stage``.

    Args:
        item: Execution in STAGING with ``runDir`` and ``stagingPrefix``.
        ctx: Poller context.
        vams_input: The validated request files.
        resolved: The kind of the input and its input manifest.
        should_stop: Event set by the keepalive when the work must stop.

    Returns:
        The input manifest document, or ``None`` when the work was stopped.

    Raises:
        PermanentFailure: A view source file is unusable, FSx has no room, or the copy or
            the import failed for good.
        TransientError: S3 failed temporarily.
    """
    input_manifest = resolved.input_manifest
    if input_manifest is None:
        input_manifest = build_view_input_manifest(
            ctx.s3, item.runDir, vams_input.asset_entry, resolved, should_stop
        )
        if input_manifest is None:
            return None
        check_fsx_capacity(
            input_manifest["objects"],
            vams_input.training_params,
            fsx_free_bytes(FSX_MOUNT_POINT),
        )
    objects = input_manifest["objects"]
    if not copy_objects(ctx.s3, objects, ctx.config.data_bucket, item.stagingPrefix, should_stop):
        return None
    dataset_root = config.fsx_dataset_root(ctx.config, item.pipelineExecutionId)
    deadline = ctx.clock() + ctx.config.ingest_wait_seconds
    if not wait_for_ingest(objects, dataset_root, deadline, ctx.clock, should_stop):
        return None
    return input_manifest


def prepare_view_run_directory(
    ctx: "PollerContext", item: "ExecutionItem", view_manifest: view.ViewManifest
) -> None:
    """Write the curation manifest and build the view dataset root in the run directory.

    Both steps overwrite what an earlier STAGING attempt left.

    Args:
        ctx: Poller context.
        item: Execution in STAGING whose files FSx has imported.
        view_manifest: The validated curation view manifest.
    """
    write_json_atomically(os.path.join(item.runDir, CURATION_MANIFEST_FILE), view_manifest.document)
    view.build_view_dataset_root(
        config.fsx_dataset_root(ctx.config, item.pipelineExecutionId),
        os.path.join(item.runDir, VIEW_DATASET_DIR),
    )


class _LeaseKeeper:
    """Keepalive callback that sends heartbeats and extends the lease during staging."""

    def __init__(self, item: "ExecutionItem", ctx: "PollerContext") -> None:
        """Create the callback.

        Args:
            item: Execution being staged.
            ctx: Poller context.
        """
        self._item = item
        self._ctx = ctx
        self.abort_suspected = False
        self.lease_lost = False

    def __call__(self) -> bool:
        """Send one heartbeat and extend the lease.

        Returns:
            True to keep going. False when an abort is suspected or the lease was lost.
        """
        if not send_heartbeat(self._item, self._ctx):
            self.abort_suspected = True
            return False
        if not self._ctx.store.renew_lease(self._item):
            self.lease_lost = True
            return False
        return True


def _raise_keepalive_stop(
    item: "ExecutionItem", keeper: _LeaseKeeper, keepalive: Keepalive
) -> NoReturn:
    """Turn a stopped keepalive into the exception that ends the handler.

    Args:
        item: Execution being staged.
        keeper: The keepalive callback.
        keepalive: The stopped keepalive.

    Raises:
        StaleItemError: An abort is suspected, the lease was lost, or the item changed.
        Exception: Any other error raised by the keepalive callback, unchanged.
    """
    pid = item.pipelineExecutionId
    if keeper.abort_suspected:
        raise StaleItemError(f"{pid} moved to ABORT_SUSPECTED during staging")
    if keeper.lease_lost:
        raise StaleItemError(f"{pid} lease was lost during staging")
    if keepalive.error is not None:
        raise keepalive.error
    raise StaleItemError(f"{pid} keepalive stopped during staging")


def _require_state(item: "ExecutionItem", state: State) -> None:
    """Check the state of the caller's copy of an execution.

    Args:
        item: Execution.
        state: Required state.

    Raises:
        ValueError: The execution is in another state.
    """
    if item.state is not state:
        raise ValueError(
            f"{item.pipelineExecutionId} must be {state.value}, got {item.state.value}"
        )


def _asset_prefix(asset_root_key: str) -> str:
    """Return the asset root key as a prefix that ends with ``/``.

    Args:
        asset_root_key: ``assetRootS3Key`` of the manifest.

    Returns:
        The prefix used to list and to cut the relative paths.
    """
    if asset_root_key.endswith("/"):
        return asset_root_key
    return asset_root_key + "/"


def _is_asset_file(key: str) -> bool:
    """Tell whether a listed key is an asset file to copy.

    Folder markers (keys ending with ``/``) and VAMS preview files (keys containing
    ``.previewFile.``) are not asset files.

    Args:
        key: S3 key.

    Returns:
        True for a key to copy.
    """
    return not key.endswith(FOLDER_MARKER_SUFFIX) and PREVIEW_FILE_MARKER not in key


def _validate_relative_path(relative_path: str) -> None:
    """Check that a relative path stays below the staging prefix and the dataset root.

    Args:
        relative_path: Key relative to the asset root.

    Raises:
        PermanentFailure: ``PASK.InvalidRequest`` for an empty, ``.``, or ``..`` segment.
    """
    if any(segment in RELATIVE_PATH_FORBIDDEN_SEGMENTS for segment in relative_path.split("/")):
        raise PermanentFailure(
            ErrorName.INVALID_REQUEST, "asset has a key with an empty, '.', or '..' path segment"
        )


def list_latest_objects(s3: Any, bucket: str, asset_root_key: str) -> list[dict[str, Any]]:
    """List the latest version of every asset file under the asset root.

    Args:
        s3: boto3 S3 client.
        bucket: Asset bucket.
        asset_root_key: ``assetRootS3Key`` of the manifest.

    Returns:
        ``{"bucket", "key", "versionId", "etag", "size", "relativePath"}`` per file,
        sorted by key. ``versionId`` is ``"null"`` when versioning is disabled.

    Raises:
        PermanentFailure: ``PASK.StagingFailed`` for a permanent S3 error,
            ``PASK.InvalidRequest`` for an unsafe key.
        TransientError: Throttling, 5xx, or a connection error.
    """
    prefix = _asset_prefix(asset_root_key)
    paginator = s3.get_paginator("list_object_versions")
    objects: list[dict[str, Any]] = []
    try:
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for version in page.get("Versions", []):
                if not version.get("IsLatest") or not _is_asset_file(version["Key"]):
                    continue
                relative_path = version["Key"][len(prefix) :]
                _validate_relative_path(relative_path)
                objects.append(
                    {
                        "bucket": bucket,
                        "key": version["Key"],
                        "versionId": version["VersionId"],
                        "etag": version["ETag"].strip('"'),
                        "size": int(version["Size"]),
                        "relativePath": relative_path,
                    }
                )
    except ClientError as error:
        raise _s3_failure(
            error, f"listing s3://{bucket}/{prefix}", ErrorName.STAGING_FAILED
        ) from error
    except (ConnectionError, HTTPClientError) as error:
        raise TransientError(f"listing s3://{bucket}/{prefix} failed: {error}") from error
    return sorted(objects, key=lambda entry: entry["key"])


def read_input_manifest(run_directory: str) -> dict[str, Any] | None:
    """Read ``input_manifest.json`` of the run directory when an earlier attempt wrote it.

    Args:
        run_directory: Run directory of the execution.

    Returns:
        The input manifest document, or ``None`` when the file does not exist.
    """
    manifest_path = os.path.join(run_directory, INPUT_MANIFEST_FILE)
    if not os.path.exists(manifest_path):
        return None
    with open(manifest_path, encoding="utf-8") as manifest_file:
        return json.load(manifest_file)


def _find_listed_file(objects: list[dict[str, Any]], relative_path: str) -> dict[str, Any] | None:
    """Find a listed file by its path relative to the asset root.

    Args:
        objects: Result of ``list_latest_objects``.
        relative_path: Path to find.

    Returns:
        The listed entry, or ``None`` when the asset has no such file.
    """
    for entry in objects:
        if entry["relativePath"] == relative_path:
            return entry
    return None


def read_curation_manifest(s3: Any, entry: dict[str, Any]) -> Any:
    """Read the listed version of ``curation_manifest.json`` and decode it as JSON.

    Args:
        s3: boto3 S3 client.
        entry: Listed entry of ``curation_manifest.json``.

    Returns:
        The decoded JSON value. The caller checks its shape.

    Raises:
        PermanentFailure: ``PASK.StagingFailed`` for a permanent S3 error,
            ``PASK.InvalidRequest`` when the file is not UTF-8 JSON.
        TransientError: Throttling, 5xx, or a connection error.
    """
    request = {"Bucket": entry["bucket"], "Key": entry["key"]}
    if entry["versionId"] != UNVERSIONED_VERSION_ID:
        request["VersionId"] = entry["versionId"]
    action = f"reading {CURATION_MANIFEST_FILE} of the input asset"
    try:
        body = s3.get_object(**request)["Body"].read()
    except ClientError as error:
        raise _s3_failure(error, action, ErrorName.STAGING_FAILED) from error
    except (ConnectionError, HTTPClientError) as error:
        raise TransientError(f"{action} failed: {error}") from error
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PermanentFailure(
            ErrorName.INVALID_REQUEST, f"{CURATION_MANIFEST_FILE} is not valid JSON"
        ) from error


def _parse_input_view_manifest(document: Any, asset_entry: dict[str, Any]) -> view.ViewManifest:
    """Validate a curation view manifest against the input asset.

    Args:
        document: Decoded ``curation_manifest.json``.
        asset_entry: The whole-asset ``inputFiles`` entry.

    Returns:
        The validated manifest.

    Raises:
        PermanentFailure: ``PASK.InvalidRequest`` when the manifest is invalid.
    """
    return view.parse_view_manifest(
        document, input_bucket=asset_entry["bucket"], input_database_id=asset_entry["databaseId"]
    )


def _load_recorded_view_manifest(
    s3: Any, input_manifest: dict[str, Any], asset_entry: dict[str, Any]
) -> view.ViewManifest:
    """Read the curation manifest of a view again at the version that the input manifest lists.

    Args:
        s3: boto3 S3 client.
        input_manifest: Input manifest document of a view written by an earlier attempt.
        asset_entry: The whole-asset ``inputFiles`` entry.

    Returns:
        The validated manifest.

    Raises:
        PermanentFailure: ``PASK.StagingFailed`` when the input manifest does not list the
            curation manifest or S3 fails for good, ``PASK.InvalidRequest`` when the
            manifest is invalid.
        TransientError: S3 failed temporarily.
    """
    entry = _find_listed_file(input_manifest["view"]["objects"], CURATION_MANIFEST_FILE)
    if entry is None:
        raise PermanentFailure(
            ErrorName.STAGING_FAILED,
            f"{INPUT_MANIFEST_FILE} of the view does not list {CURATION_MANIFEST_FILE}",
        )
    return _parse_input_view_manifest(read_curation_manifest(s3, entry), asset_entry)


def _resolve_existing_input(
    s3: Any, input_manifest: dict[str, Any], asset_entry: dict[str, Any]
) -> ResolvedInput:
    """Resolve the input of a retried STAGING from the input manifest it already wrote.

    Args:
        s3: boto3 S3 client.
        input_manifest: Input manifest document written by an earlier attempt.
        asset_entry: The whole-asset ``inputFiles`` entry.

    Returns:
        The resolved input with the existing input manifest.

    Raises:
        PermanentFailure: ``PASK.StagingFailed`` for an unknown kind, or a failure of
            ``_load_recorded_view_manifest``.
        TransientError: S3 failed temporarily.
    """
    kind = input_manifest.get("kind", INPUT_KIND_DATASET)
    if kind == INPUT_KIND_DATASET:
        return ResolvedInput(input_manifest=input_manifest, view_manifest=None, view_objects=None)
    if kind == INPUT_KIND_VIEW:
        view_manifest = _load_recorded_view_manifest(s3, input_manifest, asset_entry)
        return ResolvedInput(
            input_manifest=input_manifest, view_manifest=view_manifest, view_objects=None
        )
    raise PermanentFailure(
        ErrorName.STAGING_FAILED, f"{INPUT_MANIFEST_FILE} has an unknown kind: {kind!r}"
    )


def resolve_input(s3: Any, run_directory: str, asset_entry: dict[str, Any]) -> ResolvedInput:
    """Tell the kind of the input and write ``input_manifest.json`` of a dataset.

    An existing ``input_manifest.json`` is never rebuilt, so that a retried STAGING uses
    the same kind and copies the same versions. Without one, the latest versions under the
    asset root are listed. ``curation_manifest.json`` at the root with mode ``view`` makes
    the input a view; its input manifest is left to ``build_view_input_manifest``. Any
    other input is a dataset, which must hold ``meta/info.json`` at its root.

    Args:
        s3: boto3 S3 client.
        run_directory: Run directory of the execution.
        asset_entry: The whole-asset ``inputFiles`` entry.

    Returns:
        The resolved input.

    Raises:
        PermanentFailure: ``PASK.InvalidRequest`` when the asset has no files, the curation
            manifest is invalid, or a dataset has no ``meta/info.json``;
            ``PASK.StagingFailed`` for a permanent S3 error.
        TransientError: S3 failed temporarily.
    """
    existing = read_input_manifest(run_directory)
    if existing is not None:
        return _resolve_existing_input(s3, existing, asset_entry)

    bucket = asset_entry["bucket"]
    asset_root_key = asset_entry["assetRootS3Key"]
    objects = list_latest_objects(s3, bucket, asset_root_key)
    if not objects:
        raise PermanentFailure(ErrorName.INVALID_REQUEST, "asset has no files to train on")
    curation_entry = _find_listed_file(objects, CURATION_MANIFEST_FILE)
    if curation_entry is not None:
        document = read_curation_manifest(s3, curation_entry)
        if view.curation_manifest_mode(document) == view.MODE_VIEW:
            view_manifest = _parse_input_view_manifest(document, asset_entry)
            return ResolvedInput(
                input_manifest=None, view_manifest=view_manifest, view_objects=objects
            )
    if _find_listed_file(objects, DATASET_INFO_FILE) is None:
        raise PermanentFailure(ErrorName.INVALID_REQUEST, f"asset root has no {DATASET_INFO_FILE}")
    document = {
        "schemaVersion": INPUT_MANIFEST_SCHEMA_VERSION,
        "bucket": bucket,
        "assetRootS3Key": asset_root_key,
        "versioned": all(entry["versionId"] != UNVERSIONED_VERSION_ID for entry in objects),
        "objects": objects,
    }
    write_json_atomically(os.path.join(run_directory, INPUT_MANIFEST_FILE), document)
    return ResolvedInput(input_manifest=document, view_manifest=None, view_objects=None)


def _view_source_file_failure(error: ClientError, path: str) -> Exception:
    """Classify an S3 error of ``HeadObject`` on a view source file.

    The cause names the path only, not the bucket or the version.

    Args:
        error: The error.
        path: ``path`` of the source file.

    Returns:
        ``PermanentFailure`` with ``PASK.InvalidRequest`` when the version is gone or is
        malformed, ``PASK.StagingFailed`` when access is denied, otherwise the result of
        ``_s3_failure``.
    """
    code = error.response.get("Error", {}).get("Code", "")
    status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
    is_not_found = code in VERSION_UNAVAILABLE_ERROR_CODES or status == NOT_FOUND_STATUS
    is_invalid_version = code in INVALID_VERSION_ERROR_CODES and status == BAD_REQUEST_STATUS
    if is_not_found or is_invalid_version:
        return PermanentFailure(
            ErrorName.INVALID_REQUEST, f"view source file {path} version is no longer available"
        )
    if code in ACCESS_DENIED_ERROR_CODES or status == FORBIDDEN_STATUS:
        return PermanentFailure(
            ErrorName.STAGING_FAILED, f"{SOURCE_ACCESS_DENIED_CAUSE}: view source file {path}"
        )
    return _s3_failure(error, f"reading view source file {path}", ErrorName.STAGING_FAILED)


def head_view_source_file(
    s3: Any, view_manifest: view.ViewManifest, source_file: view.ViewSourceFile
) -> dict[str, Any]:
    """Check the pinned version of one view source file and build its ``objects`` entry.

    Args:
        s3: boto3 S3 client.
        view_manifest: The validated curation view manifest.
        source_file: One ``source.files`` entry.

    Returns:
        ``{"bucket", "key", "versionId", "etag", "size", "relativePath"}`` of the file.

    Raises:
        PermanentFailure: ``PASK.InvalidRequest`` when the version is gone or its size
            differs from the manifest, ``PASK.StagingFailed`` when access is denied or S3
            fails for good.
        TransientError: Throttling, 5xx, or a connection error.
    """
    key = view_manifest.asset_root_key + source_file.path
    try:
        head = s3.head_object(
            Bucket=view_manifest.bucket, Key=key, VersionId=source_file.version_id
        )
    except ClientError as error:
        raise _view_source_file_failure(error, source_file.path) from error
    except (ConnectionError, HTTPClientError) as error:
        raise TransientError(
            f"reading view source file {source_file.path} failed: {error}"
        ) from error
    if int(head["ContentLength"]) != source_file.size:
        raise PermanentFailure(
            ErrorName.INVALID_REQUEST,
            f"view source file {source_file.path} size differs from the curation manifest",
        )
    return {
        "bucket": view_manifest.bucket,
        "key": key,
        "versionId": source_file.version_id,
        "etag": head["ETag"].strip('"'),
        "size": source_file.size,
        "relativePath": source_file.path,
    }


def resolve_view_source_objects(
    s3: Any, view_manifest: view.ViewManifest, should_stop: threading.Event
) -> list[dict[str, Any]] | None:
    """Check every view source file in parallel and build the ``objects`` of the input manifest.

    Each worker checks ``should_stop`` before it starts the next file. After the first
    failure, the remaining files are skipped and the failure is raised.

    Args:
        s3: boto3 S3 client.
        view_manifest: The validated curation view manifest.
        should_stop: Event set by the keepalive when the work must stop.

    Returns:
        The entries sorted by path, or ``None`` when the work was stopped.

    Raises:
        PermanentFailure: A failure of ``head_view_source_file``.
        TransientError: S3 failed temporarily.
    """
    failure_seen = threading.Event()

    def head_entry(source_file: view.ViewSourceFile) -> dict[str, Any] | None:
        """Check one file unless the work was stopped or another check failed.

        Args:
            source_file: One ``source.files`` entry.

        Returns:
            The ``objects`` entry, or ``None`` when the file was skipped.
        """
        if should_stop.is_set() or failure_seen.is_set():
            return None
        try:
            return head_view_source_file(s3, view_manifest, source_file)
        except Exception:
            failure_seen.set()
            raise

    with ThreadPoolExecutor(max_workers=COPY_WORKERS) as executor:
        futures = [executor.submit(head_entry, source_file) for source_file in view_manifest.files]
    entries = [future.result() for future in futures]
    if should_stop.is_set():
        return None
    resolved_entries = [entry for entry in entries if entry is not None]
    if len(resolved_entries) != len(entries):
        return None
    return sorted(resolved_entries, key=lambda entry: entry["relativePath"])


def _view_source_info(view_manifest: view.ViewManifest) -> dict[str, Any]:
    """Describe the source asset of a view for the input manifest and the context.

    Args:
        view_manifest: The validated curation view manifest.

    Returns:
        ``databaseId``, ``assetId``, ``assetVersion`` (left out without a value),
        ``bucket``, and ``assetRootS3Key`` of the source asset.
    """
    source: dict[str, Any] = {
        "databaseId": view_manifest.database_id,
        "assetId": view_manifest.asset_id,
    }
    if view_manifest.asset_version is not None:
        source["assetVersion"] = view_manifest.asset_version
    source["bucket"] = view_manifest.bucket
    source["assetRootS3Key"] = view_manifest.asset_root_key
    return source


def build_view_input_manifest(
    s3: Any,
    run_directory: str,
    asset_entry: dict[str, Any],
    resolved: ResolvedInput,
    should_stop: threading.Event,
) -> dict[str, Any] | None:
    """Write ``input_manifest.json`` (``schemaVersion`` 2) of a view whose files are not resolved.

    Args:
        s3: boto3 S3 client.
        run_directory: Run directory of the execution.
        asset_entry: The whole-asset ``inputFiles`` entry of the view asset.
        resolved: Result of ``resolve_input`` with ``view_manifest`` and ``view_objects`` set.
        should_stop: Event set by the keepalive when the work must stop.

    Returns:
        The input manifest document, or ``None`` when the work was stopped; nothing is
        written then.

    Raises:
        ValueError: ``resolved`` is not an unresolved view.
        PermanentFailure: A failure of ``resolve_view_source_objects``.
        TransientError: S3 failed temporarily.
    """
    if resolved.view_manifest is None or resolved.view_objects is None:
        raise ValueError("build_view_input_manifest needs an unresolved view")
    objects = resolve_view_source_objects(s3, resolved.view_manifest, should_stop)
    if objects is None:
        return None
    document = {
        "schemaVersion": VIEW_INPUT_MANIFEST_SCHEMA_VERSION,
        "kind": INPUT_KIND_VIEW,
        "bucket": asset_entry["bucket"],
        "assetRootS3Key": asset_entry["assetRootS3Key"],
        "versioned": True,
        "view": {"objects": resolved.view_objects},
        "source": _view_source_info(resolved.view_manifest),
        "episodes": list(resolved.view_manifest.episodes),
        "objects": objects,
    }
    write_json_atomically(os.path.join(run_directory, INPUT_MANIFEST_FILE), document)
    return document


def write_json_atomically(path: str, document: dict[str, Any]) -> None:
    """Write a JSON file through a temporary file in the same directory and a rename.

    Args:
        path: Destination path.
        document: JSON serializable object.
    """
    directory = os.path.dirname(path)
    file_descriptor, temp_path = tempfile.mkstemp(
        dir=directory, prefix=os.path.basename(path) + ".tmp."
    )
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as temp_file:
            json.dump(document, temp_file, ensure_ascii=False, indent=2)
            temp_file.write("\n")
        os.replace(temp_path, path)
    except BaseException:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise


def dataset_digest(objects: list[dict[str, Any]]) -> str:
    """Compute the dataset digest of an input manifest without reading the files.

    Args:
        objects: ``objects`` of ``input_manifest.json``.

    Returns:
        ``sha256:<hex>`` of the lines ``<key> TAB <versionId> TAB <etag> TAB <size> LF``
        sorted by key.
    """
    lines = [
        f"{entry['key']}\t{entry['versionId']}\t{entry['etag']}\t{entry['size']}\n"
        for entry in sorted(objects, key=lambda entry: entry["key"])
    ]
    return DATASET_DIGEST_PREFIX + hashlib.sha256("".join(lines).encode("utf-8")).hexdigest()


def input_manifest_digest(input_manifest: dict[str, Any]) -> str:
    """Compute the dataset digest of an input manifest of either kind.

    For a view, the files of the view asset (``view.objects``) count with the source files,
    so another version of the curation manifest gives another digest.

    Args:
        input_manifest: The input manifest document.

    Returns:
        ``dataset_digest`` of ``objects``, plus ``view.objects`` for a view.
    """
    objects = list(input_manifest["objects"])
    if input_manifest.get("kind") == INPUT_KIND_VIEW:
        objects += input_manifest["view"]["objects"]
    return dataset_digest(objects)


def estimate_checkpoint_bytes(training_params: dict[str, str]) -> int:
    """Estimate the bytes of all checkpoints that the training job saves.

    Args:
        training_params: Validated training parameters. Missing keys take the
            ``slurm_train.sh`` defaults.

    Returns:
        ``ceil(STEPS / SAVE_FREQ) * CHECKPOINT_BYTES_BY_MODE[FINETUNE_MODE]``.
    """
    finetune_mode = training_params.get("FINETUNE_MODE", SLURM_TRAIN_DEFAULT_FINETUNE_MODE)
    steps = int(training_params.get("STEPS", SLURM_TRAIN_DEFAULT_STEPS))
    save_freq = int(training_params.get("SAVE_FREQ", SLURM_TRAIN_DEFAULT_SAVE_FREQ))
    checkpoint_count = math.ceil(steps / save_freq)
    return checkpoint_count * CHECKPOINT_BYTES_BY_MODE[finetune_mode]


def fsx_free_bytes(mount_point: str) -> int:
    """Return the bytes available to unprivileged users on a file system.

    Args:
        mount_point: Mount point of the file system, such as ``/fsx``.

    Returns:
        ``f_bavail * f_frsize`` of ``os.statvfs``.
    """
    file_system_stats = os.statvfs(mount_point)
    return file_system_stats.f_bavail * file_system_stats.f_frsize


def check_fsx_capacity(
    objects: list[dict[str, Any]], training_params: dict[str, str], free_bytes: int
) -> None:
    """Check that the dataset and the checkpoints fit in the free space of FSx.

    Args:
        objects: ``objects`` of ``input_manifest.json``.
        training_params: Validated training parameters.
        free_bytes: Free bytes of FSx.

    Raises:
        PermanentFailure: ``PASK.StagingFailed`` when they do not fit.
    """
    dataset_bytes = sum(entry["size"] for entry in objects)
    required_bytes = dataset_bytes + estimate_checkpoint_bytes(training_params)
    if required_bytes > free_bytes:
        raise PermanentFailure(
            ErrorName.STAGING_FAILED,
            f"FSx has {free_bytes} free bytes but the dataset and checkpoints need about "
            f"{required_bytes} bytes",
        )


def _copy_one_object(
    s3: Any, entry: dict[str, Any], destination_bucket: str, destination_prefix: str
) -> None:
    """Copy one listed version to the staging prefix with a managed copy.

    Args:
        s3: boto3 S3 client.
        entry: One ``objects`` entry of ``input_manifest.json``.
        destination_bucket: PASK data bucket.
        destination_prefix: Staging prefix ending with ``/``.

    Raises:
        PermanentFailure: ``PASK.StagingFailed`` for a permanent S3 error.
        TransientError: Throttling, 5xx, or a connection error.
    """
    copy_source = {"Bucket": entry["bucket"], "Key": entry["key"]}
    if entry["versionId"] != UNVERSIONED_VERSION_ID:
        copy_source["VersionId"] = entry["versionId"]
    destination_key = destination_prefix + entry["relativePath"]
    action = f"copying s3://{entry['bucket']}/{entry['key']}"
    try:
        s3.copy(copy_source, destination_bucket, destination_key, Config=COPY_TRANSFER_CONFIG)
    except ClientError as error:
        raise _s3_failure(error, action, ErrorName.STAGING_FAILED) from error
    except (ConnectionError, HTTPClientError) as error:
        raise TransientError(f"{action} failed: {error}") from error


def copy_objects(
    s3: Any,
    objects: list[dict[str, Any]],
    destination_bucket: str,
    destination_prefix: str,
    should_stop: threading.Event,
) -> bool:
    """Copy the listed versions to the staging prefix in parallel.

    Each worker checks ``should_stop`` before it starts the next object, so a stop takes
    effect at an object boundary. After the first failure, the remaining objects are
    skipped and the failure is raised.

    Args:
        s3: boto3 S3 client.
        objects: ``objects`` of ``input_manifest.json``.
        destination_bucket: PASK data bucket.
        destination_prefix: Staging prefix ending with ``/``.
        should_stop: Event set by the keepalive when the work must stop.

    Returns:
        True when every object was copied, False when the copy stopped early.

    Raises:
        PermanentFailure: ``PASK.StagingFailed`` for a permanent S3 error.
        TransientError: Throttling, 5xx, or a connection error.
    """
    failure_seen = threading.Event()

    def copy_entry(entry: dict[str, Any]) -> bool:
        """Copy one object unless the work was stopped or another copy failed.

        Args:
            entry: One ``objects`` entry.

        Returns:
            True when the object was copied.
        """
        if should_stop.is_set() or failure_seen.is_set():
            return False
        try:
            _copy_one_object(s3, entry, destination_bucket, destination_prefix)
        except Exception:
            failure_seen.set()
            raise
        return True

    with ThreadPoolExecutor(max_workers=COPY_WORKERS) as executor:
        futures = [executor.submit(copy_entry, entry) for entry in objects]
    copied = [future.result() for future in futures]
    return all(copied) and not should_stop.is_set()


def _is_ingested(dataset_root: str, entry: dict[str, Any]) -> bool:
    """Tell whether one staged file is visible on FSx with its full size.

    Args:
        dataset_root: FSx directory of the staged dataset.
        entry: One ``objects`` entry of ``input_manifest.json``.

    Returns:
        True when the file exists with the listed size.
    """
    try:
        return os.stat(os.path.join(dataset_root, entry["relativePath"])).st_size == entry["size"]
    except FileNotFoundError:
        return False


def wait_for_ingest(
    objects: list[dict[str, Any]],
    dataset_root: str,
    deadline: float,
    clock: Callable[[], float],
    should_stop: threading.Event,
) -> bool:
    """Wait until the FSx automatic import shows every staged file with its size.

    Args:
        objects: ``objects`` of ``input_manifest.json``.
        dataset_root: FSx directory of the staged dataset.
        deadline: Epoch seconds after which the wait fails.
        clock: Function that returns the current epoch seconds.
        should_stop: Event set by the keepalive when the work must stop.

    Returns:
        True when every file is visible, False when the wait was stopped.

    Raises:
        PermanentFailure: ``PASK.StagingFailed`` when the deadline passed.
    """
    pending = list(objects)
    while True:
        pending = [entry for entry in pending if not _is_ingested(dataset_root, entry)]
        if not pending:
            return True
        if clock() >= deadline:
            raise PermanentFailure(
                ErrorName.STAGING_FAILED,
                f"{len(pending)} of {len(objects)} staged files did not appear on FSx "
                "before the import wait limit",
            )
        if should_stop.wait(INGEST_POLL_INTERVAL_SECONDS):
            return False


def _non_empty_strings(source: dict[str, Any], keys: tuple[str, ...]) -> dict[str, str]:
    """Pick the keys whose value is a non-empty string.

    Args:
        source: Object to read.
        keys: Keys to pick.

    Returns:
        The picked keys and values.
    """
    return {key: source[key] for key in keys if isinstance(source.get(key), str) and source[key]}


def _view_context(run_directory: str, view_manifest: view.ViewManifest) -> dict[str, Any]:
    """Build the ``view`` object of ``vams_context.json``.

    Args:
        run_directory: Run directory of the execution.
        view_manifest: The validated curation view manifest.

    Returns:
        The view summary. ``createdAt`` and ``reviewUpdatedAt`` are left out without a value.
    """
    optional_fields = {
        "createdAt": view_manifest.created_at,
        "reviewUpdatedAt": view_manifest.review_updated_at,
    }
    return {
        "manifestFile": os.path.join(run_directory, CURATION_MANIFEST_FILE),
        **{key: value for key, value in optional_fields.items() if value is not None},
        "episodeCount": len(view_manifest.episodes),
        "excludedEpisodeCount": view_manifest.excluded_episode_count,
        "source": _view_source_info(view_manifest),
    }


def build_vams_context(
    item: "ExecutionItem",
    vams_input: VamsInput,
    input_manifest: dict[str, Any],
    digest: str,
    *,
    view_manifest: view.ViewManifest | None,
) -> dict[str, Any]:
    """Build ``vams_context.json``, the VAMS execution information and input provenance.

    ``pipelineName`` holds the pipeline id that VAMS puts in ``auxTempPrefix``. A view gets
    ``schemaVersion`` 2, ``input.kind``, and ``view``; a dataset keeps ``schemaVersion`` 1.

    Args:
        item: Execution being staged.
        vams_input: The validated request files.
        input_manifest: The input manifest document.
        digest: Dataset digest of the input manifest.
        view_manifest: The validated curation view manifest, or ``None`` for a dataset.

    Returns:
        The context document. Fields without a value are left out.
    """
    run_directory = item.runDir
    asset_entry = vams_input.asset_entry
    input_info: dict[str, Any] = {
        "databaseId": asset_entry["databaseId"],
        "assetId": asset_entry["assetId"],
        **_non_empty_strings(vams_input.metadata, ("assetName",)),
        "bucket": asset_entry["bucket"],
        "assetRootS3Key": asset_entry["assetRootS3Key"],
        "versioned": input_manifest["versioned"],
    }
    if view_manifest is not None:
        input_info["kind"] = INPUT_KIND_VIEW
    item_fields = {
        "workflowDatabaseId": item.workflowDatabaseId,
        "workflowId": item.workflowId,
        "workflowExecutionId": item.workflowExecutionId,
        "pipelineExecutionId": item.pipelineExecutionId,
        "pipelineName": messages.pipeline_name(vams_input.manifest),
        "executingUserName": item.executingUserName,
    }
    view_fields: dict[str, Any] = {}
    if view_manifest is not None:
        view_fields["view"] = _view_context(run_directory, view_manifest)
    schema_version = (
        VAMS_CONTEXT_SCHEMA_VERSION if view_manifest is None else VIEW_VAMS_CONTEXT_SCHEMA_VERSION
    )
    return {
        "schemaVersion": schema_version,
        "trigger": VAMS_TRIGGER,
        **{key: value for key, value in item_fields.items() if value},
        "input": input_info,
        **view_fields,
        "output": _non_empty_strings(vams_input.manifest.output_target, ("databaseId", "assetId")),
        "datasetDigest": digest,
        "inputManifestFile": os.path.join(run_directory, INPUT_MANIFEST_FILE),
        "resultFile": os.path.join(run_directory, MLFLOW_RUN_FILE),
    }
