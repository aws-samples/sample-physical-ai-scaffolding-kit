"""Job monitoring, outcome decision, write back to VAMS, completion callbacks, and cleanup.

The handlers follow the order "record the intent, run the side effect, record the result".
A terminal state is written only after Step Functions took the outcome or the latest token
is closed. After the terminal state only the resend of the outcome to a new token and the
cleanup remain, and both are safe to repeat.
"""

import json
import logging
import mimetypes
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from boto3.exceptions import S3UploadFailedError
from botocore.exceptions import ClientError, ConnectionError, HTTPClientError

from pask_vams_poller.config import output_dir, run_dir, staging_prefix
from pask_vams_poller.constants import (
    INPUT_KIND_DATASET,
    INPUT_KIND_VIEW,
    JOB_EXIT_FILE,
    JOB_STARTED_FILE,
    MLFLOW_RUN_FILE,
    OUTPUT_FILES_LIMIT,
    PENDING_CLEANUP,
    RESULTS_FILE_MAX_BYTES,
    TERMINAL_STATES,
    VAMS_CONTEXT_FILE,
    ErrorName,
    State,
)
from pask_vams_poller.context import PollerContext
from pask_vams_poller.errors import PermanentFailure, StaleItemError, TransientError
from pask_vams_poller.keepalive import Keepalive
from pask_vams_poller.messages import (
    load_json_from_s3,
    parse_manifest,
    parse_s3_uri,
    validate_training_params,
)
from pask_vams_poller.sfn import CallResult, TokenCallOutcome, call_with_latest_token
from pask_vams_poller.slurm import SlurmCommandError, SqueueJob
from pask_vams_poller.store import (
    NOTIFY_ACCEPTED,
    NOTIFY_ALREADY_CLOSED,
    OUTCOME_FAILED,
    OUTCOME_SUCCEEDED,
    ExecutionItem,
    build_outcome,
)

logger = logging.getLogger(__name__)

CHECKPOINT_RELATIVE_PATH = os.path.join("checkpoints", "last", "pretrained_model")
RESULTS_FILE_NAME = "pask_training.json"
RESULTS_CONTENT_TYPE = "application/json"
DEFAULT_CONTENT_TYPE = "application/octet-stream"
SLURM_ERR_TAIL_BYTES = 4096
VIEW_SOURCE_REQUIRED_KEYS = ("databaseId", "assetId")
VIEW_SOURCE_OPTIONAL_KEYS = ("assetVersion",)
S3_DELETE_BATCH_SIZE = 1000

SQUEUE_RUNNING_STATE = "RUNNING"
SQUEUE_FINISHED_STATES: frozenset[str] = frozenset(
    {
        "BOOT_FAIL",
        "CANCELLED",
        "COMPLETED",
        "DEADLINE",
        "FAILED",
        "NODE_FAIL",
        "OUT_OF_MEMORY",
        "PREEMPTED",
        "TIMEOUT",
    }
)

UNSAFE_CONTENT_TYPES: frozenset[str] = frozenset(
    {
        "text/x-python",
        "text/x-python-source",
        "application/x-python-code",
        "application/x-sh",
        "text/x-sh",
        "application/x-csh",
        "application/javascript",
        "text/javascript",
    }
)
PERMANENT_S3_ERROR_CODES: frozenset[str] = frozenset(
    {
        "AccessDenied",
        "AllAccessDisabled",
        "InvalidBucketName",
        "NoSuchBucket",
    }
)
ABORTED_CAUSE = "The PASK execution was aborted because its task token was closed"

_MIME_TYPES = mimetypes.MimeTypes()


class CleanupTargetMismatch(Exception):
    """A stored cleanup target is not the path that the execution id determines."""


class MalformedRunFile(Exception):
    """A JSON file in the run directory is not a UTF-8 JSON object."""


@dataclass
class _ItemHolder:
    """The latest copy of an execution, shared with the keepalive thread.

    Attributes:
        item: Latest execution read by this handler.
        closed_token: Token whose heartbeat returned CLOSED, or None.
    """

    item: ExecutionItem
    closed_token: str | None = None


@dataclass
class _OutputPlan:
    """What to write back for a SUCCEEDED execution.

    Attributes:
        bucket: ``outputs.bucket`` of the manifest.
        files: ``(local path, S3 key)`` pairs under ``outputs.files``.
        results_key: S3 key of ``pask_training.json`` under ``outputs.results``.
        results_body: UTF-8 JSON of ``pask_training.json``.
    """

    bucket: str
    files: list[tuple[str, str]]
    results_key: str
    results_body: bytes = field(repr=False)


@dataclass
class _CleanupTargets:
    """Checked targets of the cleanup.

    Attributes:
        staging_prefix: Staging prefix in the PASK data bucket, or None when never set.
        directories: Output and run directories on FSx that were set.
    """

    staging_prefix: str | None
    directories: list[str]


@dataclass
class _JobResult:
    """Outcome decided from the marker files of a finished job.

    Attributes:
        outcome: ``{"status", "errorName", "cause"}``.
        mlflow_attributes: ``mlflowRunId`` and ``mlflowExperimentId`` when known.
    """

    outcome: dict[str, str | None]
    mlflow_attributes: dict[str, str]


def process_submitted(item: ExecutionItem, ctx: PollerContext) -> None:
    """Monitor a submitted job: heartbeat, move to RUNNING, or finalize when it is gone.

    Args:
        item: Execution in SUBMITTED, leased by this poller.
        ctx: Poller dependencies.

    Raises:
        ValueError: The execution is not in SUBMITTED or lacks its job attributes.
        StaleItemError: The item changed while it was processed.
        TransientError: Slurm, Step Functions, or DynamoDB was temporarily unavailable.
    """
    _require_state(item, State.SUBMITTED)
    _monitor_job(item, ctx)


def process_running(item: ExecutionItem, ctx: PollerContext) -> None:
    """Monitor a running job: heartbeat, or finalize when it left the queue.

    Args:
        item: Execution in RUNNING, leased by this poller.
        ctx: Poller dependencies.

    Raises:
        ValueError: The execution is not in RUNNING or lacks its job attributes.
        StaleItemError: The item changed while it was processed.
        TransientError: Slurm, Step Functions, or DynamoDB was temporarily unavailable.
    """
    _require_state(item, State.RUNNING)
    _monitor_job(item, ctx)


def process_finalizing(item: ExecutionItem, ctx: PollerContext) -> None:
    """Write the outputs back if needed, report the outcome, and move to the terminal state.

    Args:
        item: Execution in FINALIZING with an outcome, leased by this poller.
        ctx: Poller dependencies.

    Raises:
        ValueError: The execution is not in FINALIZING or has no valid outcome.
        StaleItemError: The item changed while it was processed.
        TransientError: S3, Step Functions, or DynamoDB was temporarily unavailable.
    """
    _require_state(item, State.FINALIZING)
    _require_outcome(item)
    if item.outcome["status"] == OUTCOME_SUCCEEDED and item.outputs is None:
        written = _write_back_outputs(item, ctx)
        if written is None:
            return
        item = written
    _notify_outcome(item, ctx)


def process_cleanup(item: ExecutionItem, ctx: PollerContext) -> None:
    """Resend the stored outcome to a new token if needed, then delete the run's data.

    Deletes the staging prefix in the PASK data bucket and the output and run
    directories on FSx, then removes ``pending`` and returns the concurrency slot.
    Objects in VAMS buckets are never deleted.

    Args:
        item: Terminal execution with ``pending=CLEANUP``, leased by this poller.
        ctx: Poller dependencies.

    Raises:
        ValueError: The execution is not terminal or has no stored outcome to resend.
        CleanupTargetMismatch: A stored path is not the one the execution id determines.
        StaleItemError: The item changed while it was processed.
        TransientError: S3, Step Functions, or DynamoDB was temporarily unavailable.
    """
    if item.state not in TERMINAL_STATES:
        raise ValueError(f"{item.pipelineExecutionId} is not terminal: {item.state.value}")
    if item.pending != PENDING_CLEANUP:
        logger.info("%s has no cleanup pending", item.pipelineExecutionId)
        return
    targets = _cleanup_targets(item, ctx)
    if item.resendPending:
        item = _resend_outcome(item, ctx)
    if targets.staging_prefix is not None:
        _delete_staging_prefix(ctx, targets.staging_prefix)
    for directory in targets.directories:
        _delete_fsx_directory(directory)
    ctx.store.complete_cleanup(item)
    logger.info("Cleaned up %s", item.pipelineExecutionId)


def _monitor_job(item: ExecutionItem, ctx: PollerContext) -> None:
    """Send a heartbeat, then advance the execution from what Slurm and the markers show.

    Args:
        item: Execution in SUBMITTED or RUNNING.
        ctx: Poller dependencies.
    """
    if item.jobName is None or item.runDir is None or item.outputDir is None:
        raise ValueError(f"{item.pipelineExecutionId} lacks jobName, runDir, or outputDir")
    item = _send_heartbeat(item, ctx)
    if item is None:
        return
    queued_jobs = _queued_jobs(ctx, item.jobName)
    if queued_jobs:
        if item.state is State.SUBMITTED and _has_started(item, queued_jobs):
            ctx.store.transition(item, State.RUNNING)
            logger.info("%s is RUNNING", item.pipelineExecutionId)
        return
    result = decide_job_result(item.runDir, item.outputDir, item.slurmJobId)
    ctx.store.to_finalizing(item, result.outcome, set_attrs=result.mlflow_attributes)
    logger.info(
        "%s left the queue, outcome %s %s",
        item.pipelineExecutionId,
        result.outcome["status"],
        result.outcome["errorName"] or "",
    )


def _queued_jobs(ctx: PollerContext, job_name: str) -> list[SqueueJob]:
    """List the jobs with the execution's name that have not finished.

    Args:
        ctx: Poller dependencies.
        job_name: Slurm job name ``vams-<id>``.

    Returns:
        Jobs that are pending, running, or completing.

    Raises:
        TransientError: The Slurm controller was temporarily unavailable.
        SlurmCommandError: squeue failed for another reason.
    """
    try:
        jobs = ctx.slurm.find_by_name(job_name)
    except SlurmCommandError as error:
        if error.transient:
            raise TransientError(f"squeue failed: {error}") from error
        raise
    return [job for job in jobs if job.state not in SQUEUE_FINISHED_STATES]


def _has_started(item: ExecutionItem, queued_jobs: list[SqueueJob]) -> bool:
    """Tell whether the submitted job has started.

    Args:
        item: Execution with ``runDir``.
        queued_jobs: Jobs that squeue still lists.

    Returns:
        True when squeue reports RUNNING or ``job_started.json`` exists.
    """
    if any(job.state == SQUEUE_RUNNING_STATE for job in queued_jobs):
        return True
    return os.path.isfile(os.path.join(item.runDir, JOB_STARTED_FILE))


def decide_job_result(
    run_directory: str, output_directory: str, slurm_job_id: str | None
) -> _JobResult:
    """Decide the outcome of a job that left the queue from its marker files.

    Args:
        run_directory: Run directory with ``job_exit.json`` and ``mlflow_run.json``.
        output_directory: Training output directory with ``checkpoints/last``.
        slurm_job_id: Slurm job id, used in causes and log file names.

    Returns:
        SUCCEEDED when the exit code is 0 and the checkpoint exists. FAILED with
        ``PASK.JobFailed`` for a non-zero exit code or a missing checkpoint, and with
        ``PASK.JobLost`` when ``job_exit.json`` is missing or unreadable.
    """
    mlflow_attributes = _read_mlflow_run(run_directory)
    exit_code = _read_exit_code(run_directory)
    if exit_code is None:
        cause = f"Slurm job {slurm_job_id} ended without a readable {JOB_EXIT_FILE}"
        return _JobResult(
            build_outcome(OUTCOME_FAILED, ErrorName.JOB_LOST, cause), mlflow_attributes
        )
    if exit_code != 0:
        cause = f"Slurm job {slurm_job_id} exited with code {exit_code}"
        error_tail = _read_slurm_err_tail(run_directory, slurm_job_id)
        if error_tail:
            cause = f"{cause}\n--- tail of slurm_{slurm_job_id}.err ---\n{error_tail}"
        return _JobResult(
            build_outcome(OUTCOME_FAILED, ErrorName.JOB_FAILED, cause), mlflow_attributes
        )
    if not os.path.isdir(os.path.join(output_directory, CHECKPOINT_RELATIVE_PATH)):
        cause = (
            f"Slurm job {slurm_job_id} exited with code 0 but {CHECKPOINT_RELATIVE_PATH} is missing"
        )
        return _JobResult(
            build_outcome(OUTCOME_FAILED, ErrorName.JOB_FAILED, cause), mlflow_attributes
        )
    return _JobResult(build_outcome(OUTCOME_SUCCEEDED, None, None), mlflow_attributes)


def _read_json_file(path: str) -> dict[str, Any] | None:
    """Read a JSON object written by the training job.

    Args:
        path: File path.

    Returns:
        The object, or None when the file is missing.

    Raises:
        MalformedRunFile: The file is not a UTF-8 JSON object.
    """
    try:
        with open(path, encoding="utf-8") as json_file:
            document = json.load(json_file)
    except FileNotFoundError:
        return None
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MalformedRunFile(f"{os.path.basename(path)} is not valid JSON") from error
    if not isinstance(document, dict):
        raise MalformedRunFile(f"{os.path.basename(path)} is not a JSON object")
    return document


def _read_exit_code(run_directory: str) -> int | None:
    """Read the exit code from ``job_exit.json``.

    Args:
        run_directory: Run directory.

    Returns:
        The exit code, or None when the file is missing or malformed.
    """
    try:
        job_exit = _read_json_file(os.path.join(run_directory, JOB_EXIT_FILE))
    except MalformedRunFile:
        logger.warning("%s in the run directory is malformed", JOB_EXIT_FILE, exc_info=True)
        return None
    if job_exit is None:
        return None
    exit_code = job_exit.get("exitCode")
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        logger.warning("%s has no integer exitCode", JOB_EXIT_FILE)
        return None
    return exit_code


def _read_mlflow_run(run_directory: str) -> dict[str, str]:
    """Read the MLflow run ids from ``mlflow_run.json``.

    A malformed file is logged and treated as missing, so that it cannot block the
    report of the training result.

    Args:
        run_directory: Run directory.

    Returns:
        ``mlflowRunId`` and ``mlflowExperimentId`` for the values that are non-empty strings.
    """
    try:
        mlflow_run = _read_json_file(os.path.join(run_directory, MLFLOW_RUN_FILE))
    except MalformedRunFile:
        logger.warning("%s in the run directory is malformed", MLFLOW_RUN_FILE, exc_info=True)
        return {}
    if mlflow_run is None:
        return {}
    attributes: dict[str, str] = {}
    for source_key, attribute in (
        ("run_id", "mlflowRunId"),
        ("experiment_id", "mlflowExperimentId"),
    ):
        value = mlflow_run.get(source_key)
        if isinstance(value, str) and value:
            attributes[attribute] = value
    return attributes


def _read_slurm_err_tail(run_directory: str, slurm_job_id: str | None) -> str:
    """Read the last ``SLURM_ERR_TAIL_BYTES`` bytes of the job's standard error.

    Args:
        run_directory: Run directory.
        slurm_job_id: Slurm job id.

    Returns:
        The tail decoded as UTF-8 with replacement characters, or an empty string when
        the job id or the file is missing.
    """
    if slurm_job_id is None:
        return ""
    path = os.path.join(run_directory, f"slurm_{slurm_job_id}.err")
    try:
        with open(path, "rb") as err_file:
            err_file.seek(0, os.SEEK_END)
            size = err_file.tell()
            err_file.seek(max(0, size - SLURM_ERR_TAIL_BYTES))
            tail = err_file.read()
    except FileNotFoundError:
        return ""
    return tail.decode("utf-8", errors="replace").strip()


def _write_back_outputs(item: ExecutionItem, ctx: PollerContext) -> ExecutionItem | None:
    """Upload the checkpoint and the training summary to the manifest outputs.

    Args:
        item: FINALIZING execution with a SUCCEEDED outcome and no ``outputs``.
        ctx: Poller dependencies.

    Returns:
        The updated execution (with ``outputs``, or with the outcome rewritten to FAILED),
        or None when the heartbeat showed that the token is closed.
    """
    latest = _send_heartbeat(item, ctx)
    if latest is None:
        return None
    holder = _ItemHolder(item=latest)
    try:
        plan = _build_output_plan(holder.item, ctx)
        keys = _upload_outputs(holder, ctx, plan)
    except PermanentFailure as failure:
        logger.warning("Write back of %s failed permanently: %s", item.pipelineExecutionId, failure)
        return ctx.store.rewrite_outcome_failed(holder.item, failure.error_name, failure.cause)
    if keys is None:
        return None
    logger.info("Wrote %d outputs of %s", len(keys), item.pipelineExecutionId)
    return ctx.store.record_outputs(holder.item, keys)


def _build_output_plan(item: ExecutionItem, ctx: PollerContext) -> _OutputPlan:
    """Read the manifest and the training config and list what to upload.

    Args:
        item: FINALIZING execution with a SUCCEEDED outcome.
        ctx: Poller dependencies.

    Returns:
        The plan.

    Raises:
        PermanentFailure: The manifest or config is invalid, an output prefix is empty,
            or a limit of VAMS is exceeded.
    """
    if item.inputManifestS3Location is None or item.inputConfigurationS3Location is None:
        raise ValueError(f"{item.pipelineExecutionId} has no manifest or configuration location")
    manifest = parse_manifest(load_json_from_s3(ctx.s3, item.inputManifestS3Location))
    parameters = validate_training_params(
        load_json_from_s3(ctx.s3, item.inputConfigurationS3Location)
    )
    outputs = manifest.outputs
    for name in ("bucket", "files", "results"):
        if not outputs[name]:
            raise PermanentFailure(
                ErrorName.OUTPUT_UPLOAD_FAILED, f"manifest outputs.{name} is empty"
            )
    # Intake accepts only manifests stored in the workflow input/output bucket, and VAMS
    # writes the outputs to that same bucket, so the manifest bucket is the expected one.
    workflow_io_bucket, _ = parse_s3_uri(item.inputManifestS3Location)
    if outputs["bucket"] != workflow_io_bucket:
        raise PermanentFailure(
            ErrorName.OUTPUT_UPLOAD_FAILED,
            "manifest outputs.bucket is not the workflow input/output bucket",
        )
    checkpoint_dir = os.path.join(item.outputDir, CHECKPOINT_RELATIVE_PATH)
    files = [
        (local_path, _join_key(outputs["files"], relative_path))
        for local_path, relative_path in _list_checkpoint_files(checkpoint_dir)
    ]
    if len(files) > OUTPUT_FILES_LIMIT:
        raise PermanentFailure(
            ErrorName.OUTPUT_UPLOAD_FAILED,
            f"{len(files)} checkpoint files exceed the VAMS limit of {OUTPUT_FILES_LIMIT}",
        )
    results_body = _build_results_body(item, parameters)
    if len(results_body) > RESULTS_FILE_MAX_BYTES:
        raise PermanentFailure(
            ErrorName.OUTPUT_UPLOAD_FAILED,
            f"{RESULTS_FILE_NAME} has {len(results_body)} bytes, over {RESULTS_FILE_MAX_BYTES}",
        )
    return _OutputPlan(
        bucket=outputs["bucket"],
        files=files,
        results_key=_join_key(outputs["results"], RESULTS_FILE_NAME),
        results_body=results_body,
    )


def _list_checkpoint_files(checkpoint_dir: str) -> list[tuple[str, str]]:
    """List the files under ``checkpoints/last/pretrained_model``.

    ``training_state`` is a sibling of ``pretrained_model``, so it is never listed.

    Args:
        checkpoint_dir: The ``pretrained_model`` directory.

    Returns:
        ``(local path, path relative to pretrained_model with / separators)`` sorted by
        the relative path.

    Raises:
        PermanentFailure: The directory does not exist, or a file resolves to a path outside
            the directory (for example a symbolic link to another file on the cluster).
    """
    if not os.path.isdir(checkpoint_dir):
        raise PermanentFailure(
            ErrorName.OUTPUT_UPLOAD_FAILED, f"{CHECKPOINT_RELATIVE_PATH} does not exist"
        )
    real_checkpoint_dir = os.path.realpath(checkpoint_dir)
    listed: list[tuple[str, str]] = []
    for directory, _, file_names in os.walk(checkpoint_dir):
        for file_name in file_names:
            local_path = os.path.join(directory, file_name)
            relative_path = os.path.relpath(local_path, checkpoint_dir).replace(os.sep, "/")
            real_path = os.path.realpath(local_path)
            if os.path.commonpath([real_path, real_checkpoint_dir]) != real_checkpoint_dir:
                raise PermanentFailure(
                    ErrorName.OUTPUT_UPLOAD_FAILED,
                    f"checkpoint file {relative_path} resolves outside {CHECKPOINT_RELATIVE_PATH}",
                )
            listed.append((local_path, relative_path))
    return sorted(listed, key=lambda entry: entry[1])


def _join_key(prefix: str, relative_path: str) -> str:
    """Join an S3 key prefix and a relative path with exactly one ``/``.

    Args:
        prefix: Non-empty key prefix from the manifest.
        relative_path: Path relative to the prefix.

    Returns:
        The key.
    """
    return f"{prefix.rstrip('/')}/{relative_path}"


def _build_results_body(item: ExecutionItem, parameters: dict[str, str]) -> bytes:
    """Build ``pask_training.json``.

    Args:
        item: FINALIZING execution with a SUCCEEDED outcome.
        parameters: Training parameters that passed the allow list.

    Returns:
        The UTF-8 JSON.
    """
    started = _read_optional_marker(os.path.join(item.runDir, JOB_STARTED_FILE))
    finished = _read_optional_marker(os.path.join(item.runDir, JOB_EXIT_FILE))
    context = _read_optional_marker(os.path.join(item.runDir, VAMS_CONTEXT_FILE))
    summary = {
        "pipelineExecutionId": item.pipelineExecutionId,
        "slurmJobId": item.slurmJobId,
        "parameters": parameters,
        "datasetDigest": context.get("datasetDigest"),
        **_input_summary(context),
        "mlflowRunId": item.mlflowRunId,
        "mlflowExperimentId": item.mlflowExperimentId,
        "startedAt": started.get("startedAt"),
        "finishedAt": finished.get("finishedAt"),
        "status": item.outcome["status"],
    }
    return json.dumps(summary, ensure_ascii=False, indent=2).encode("utf-8")


def _input_summary(context: dict[str, Any]) -> dict[str, Any]:
    """Summarize the training input of ``vams_context.json`` for ``pask_training.json``.

    The values are only reported, so a context of an unexpected shape is logged and the
    affected fields are left out instead of failing the write back.

    Args:
        context: The ``vams_context.json`` document, or an empty dict when it was not read.

    Returns:
        ``inputKind`` (``None`` without a context), plus ``viewSource`` and
        ``viewEpisodeCount`` for a curation view.
    """
    if not context:
        return {"inputKind": None}
    input_info = context.get("input")
    kind = input_info.get("kind") if isinstance(input_info, dict) else None
    if kind is None:
        return {"inputKind": INPUT_KIND_DATASET}
    if kind != INPUT_KIND_VIEW:
        logger.warning("%s has an unknown input kind", VAMS_CONTEXT_FILE)
        return {"inputKind": None}
    summary: dict[str, Any] = {"inputKind": INPUT_KIND_VIEW}
    view_info = context.get("view")
    if not isinstance(view_info, dict):
        logger.warning("%s has no view object", VAMS_CONTEXT_FILE)
        return summary
    source = view_info.get("source")
    if isinstance(source, dict) and all(key in source for key in VIEW_SOURCE_REQUIRED_KEYS):
        summary["viewSource"] = {
            **{key: source[key] for key in VIEW_SOURCE_REQUIRED_KEYS},
            **{key: source[key] for key in VIEW_SOURCE_OPTIONAL_KEYS if key in source},
        }
    else:
        logger.warning("%s has no view source", VAMS_CONTEXT_FILE)
    summary["viewEpisodeCount"] = view_info.get("episodeCount")
    return summary


def _read_optional_marker(path: str) -> dict[str, Any]:
    """Read a run directory file whose values are only reported.

    Args:
        path: File path.

    Returns:
        The object, or an empty dict when the file is missing or malformed (logged).
    """
    try:
        document = _read_json_file(path)
    except MalformedRunFile:
        logger.warning("%s is malformed", os.path.basename(path), exc_info=True)
        return {}
    if document is None:
        logger.warning("%s is missing", os.path.basename(path))
        return {}
    return document


def content_type_for(file_name: str) -> str:
    """Choose the ``ContentType`` of an uploaded file from its extension.

    Args:
        file_name: File name or path.

    Returns:
        The MIME type known to Python's built-in table, or ``application/octet-stream``
        when the extension is unknown or its type is one that VAMS rejects.
    """
    guessed, _ = _MIME_TYPES.guess_type(file_name, strict=True)
    if guessed is None or guessed in UNSAFE_CONTENT_TYPES:
        return DEFAULT_CONTENT_TYPE
    return guessed


def _upload_outputs(holder: _ItemHolder, ctx: PollerContext, plan: _OutputPlan) -> list[str] | None:
    """Upload the plan while a keepalive sends heartbeats and extends the lease.

    Args:
        holder: Latest execution, updated by the keepalive thread.
        ctx: Poller dependencies.
        plan: What to upload.

    Returns:
        The written keys, or None when the token was closed and the execution moved to
        ABORT_SUSPECTED.

    Raises:
        PermanentFailure: S3 denied the upload.
        StaleItemError: The lease was lost.
        TransientError: S3 or the keepalive failed temporarily.
    """
    keepalive = Keepalive(
        lambda: _keepalive_tick(holder, ctx), ctx.config.heartbeat_interval_seconds
    )
    written: list[str] = []
    with keepalive:
        for local_path, key in plan.files:
            if keepalive.should_stop.is_set():
                break
            _upload_file(ctx, local_path, plan.bucket, key)
            written.append(key)
        if not keepalive.should_stop.is_set():
            _put_results(ctx, plan)
            written.append(plan.results_key)
    if holder.closed_token is not None:
        ctx.store.to_abort_suspected(holder.item, holder.closed_token)
        logger.warning(
            "%s moved to ABORT_SUSPECTED during write back", holder.item.pipelineExecutionId
        )
        return None
    if keepalive.error is not None:
        raise TransientError(f"Keepalive failed during write back: {keepalive.error}") from (
            keepalive.error
        )
    if keepalive.should_stop.is_set():
        raise StaleItemError(
            f"Lost the lease of {holder.item.pipelineExecutionId} during write back"
        )
    return written


def _keepalive_tick(holder: _ItemHolder, ctx: PollerContext) -> bool:
    """Send one heartbeat and extend the lease from the keepalive thread.

    Args:
        holder: Latest execution. Updated when the token was read again.
        ctx: Poller dependencies.

    Returns:
        True to continue, False when the token is closed or the lease was lost.
    """
    outcome, latest = _call_with_latest_token(holder.item, ctx, ctx.sfn.heartbeat)
    holder.item = latest
    if outcome.result is CallResult.CLOSED:
        holder.closed_token = outcome.token
        return False
    return ctx.store.renew_lease(latest)


def _upload_file(ctx: PollerContext, local_path: str, bucket: str, key: str) -> None:
    """Upload one checkpoint file with its ``ContentType``.

    Args:
        ctx: Poller dependencies.
        local_path: File on FSx.
        bucket: Output bucket.
        key: Output key.

    Raises:
        PermanentFailure: S3 denied the upload.
        TransientError: Any other S3 or connection error.
    """
    try:
        ctx.s3.upload_file(
            local_path, bucket, key, ExtraArgs={"ContentType": content_type_for(local_path)}
        )
    except S3UploadFailedError as error:
        _raise_for_s3_error(error, error.__context__, key)
    except (ClientError, ConnectionError, HTTPClientError) as error:
        _raise_for_s3_error(error, error, key)


def _put_results(ctx: PollerContext, plan: _OutputPlan) -> None:
    """Write ``pask_training.json`` to ``outputs.results``.

    Args:
        ctx: Poller dependencies.
        plan: Plan with the results key and body.

    Raises:
        PermanentFailure: S3 denied the write.
        TransientError: Any other S3 or connection error.
    """
    try:
        ctx.s3.put_object(
            Bucket=plan.bucket,
            Key=plan.results_key,
            Body=plan.results_body,
            ContentType=RESULTS_CONTENT_TYPE,
        )
    except (ClientError, ConnectionError, HTTPClientError) as error:
        _raise_for_s3_error(error, error, plan.results_key)


def _raise_for_s3_error(error: Exception, cause: BaseException | None, key: str) -> None:
    """Classify an S3 write error as permanent or transient and raise it.

    Args:
        error: The raised error.
        cause: The underlying ``ClientError`` when there is one.
        key: Key being written, used in the message.

    Raises:
        PermanentFailure: The error code is in ``PERMANENT_S3_ERROR_CODES``.
        TransientError: Otherwise.
    """
    code = cause.response.get("Error", {}).get("Code", "") if isinstance(cause, ClientError) else ""
    if code in PERMANENT_S3_ERROR_CODES:
        raise PermanentFailure(
            ErrorName.OUTPUT_UPLOAD_FAILED, f"S3 rejected the write of {key}: {code}"
        ) from error
    raise TransientError(f"S3 write of {key} failed: {error}") from error


def _notify_outcome(item: ExecutionItem, ctx: PollerContext) -> None:
    """Send the stored outcome with SendTaskSuccess or SendTaskFailure and go terminal.

    Args:
        item: FINALIZING execution with an outcome.
        ctx: Poller dependencies.
    """
    outcome, latest = _call_with_latest_token(item, ctx, _outcome_sender(item, ctx))
    notify_result = _notify_result(outcome)
    terminal = ctx.store.to_notified_terminal(latest, notify_result)
    logger.info("%s is %s (%s)", item.pipelineExecutionId, terminal.state.value, notify_result)


def _resend_outcome(item: ExecutionItem, ctx: PollerContext) -> ExecutionItem:
    """Send the stored outcome of a terminal execution to its new token.

    Args:
        item: Terminal execution with ``resendPending``.
        ctx: Poller dependencies.

    Returns:
        The execution without ``resendPending``.
    """
    outcome, latest = _call_with_latest_token(item, ctx, _outcome_sender(item, ctx))
    notify_result = _notify_result(outcome)
    logger.info("Resent the outcome of %s (%s)", item.pipelineExecutionId, notify_result)
    return ctx.store.update_notify_result(latest, notify_result)


def _outcome_sender(item: ExecutionItem, ctx: PollerContext) -> Callable[[str], CallResult]:
    """Build the Step Functions call that reports the stored outcome.

    Args:
        item: FINALIZING or terminal execution.
        ctx: Poller dependencies.

    Returns:
        A function that sends the outcome with a given token.

    Raises:
        ValueError: The execution has no valid outcome and is not ABORTED.
    """
    if item.state is State.ABORTED:
        return lambda token: ctx.sfn.send_failure(token, ErrorName.ABORTED.value, ABORTED_CAUSE)
    _require_outcome(item)
    if item.outcome["status"] == OUTCOME_SUCCEEDED:
        output = {"status": OUTCOME_SUCCEEDED, "slurmJobId": item.slurmJobId}
        if item.mlflowRunId is not None:
            output["mlflowRunId"] = item.mlflowRunId
        return lambda token: ctx.sfn.send_success(token, output)
    error_name = item.outcome["errorName"] or ""
    cause = item.outcome["cause"] or ""
    return lambda token: ctx.sfn.send_failure(token, error_name, cause)


def _notify_result(outcome: TokenCallOutcome) -> str:
    """Map the result of a SendTask* call to ``notifyResult``.

    Args:
        outcome: Result of ``call_with_latest_token``.

    Returns:
        ``ACCEPTED``, or ``ALREADY_CLOSED`` when the latest token is closed.
    """
    return NOTIFY_ACCEPTED if outcome.result is CallResult.ACCEPTED else NOTIFY_ALREADY_CLOSED


def _send_heartbeat(item: ExecutionItem, ctx: PollerContext) -> ExecutionItem | None:
    """Send a heartbeat with the latest token, moving to ABORT_SUSPECTED when it is closed.

    Args:
        item: Non terminal execution.
        ctx: Poller dependencies.

    Returns:
        The latest execution, or None when it moved to ABORT_SUSPECTED.
    """
    outcome, latest = _call_with_latest_token(item, ctx, ctx.sfn.heartbeat)
    if outcome.result is CallResult.CLOSED:
        ctx.store.to_abort_suspected(latest, outcome.token)
        logger.warning("%s moved to ABORT_SUSPECTED", item.pipelineExecutionId)
        return None
    return latest


def _call_with_latest_token(
    item: ExecutionItem, ctx: PollerContext, operation: Callable[[str], CallResult]
) -> tuple[TokenCallOutcome, ExecutionItem]:
    """Call a task token operation, reading the item again when the token is closed.

    Args:
        item: Execution as last read.
        ctx: Poller dependencies.
        operation: Function that takes a token.

    Returns:
        The call outcome and the latest execution read on the way (``item`` when the
        first call was accepted).

    Raises:
        ValueError: The execution has no task token.
        StaleItemError: The execution disappeared.
    """
    if item.taskToken is None:
        raise ValueError(f"{item.pipelineExecutionId} has no task token")
    holder = _ItemHolder(item=item)

    def read_latest_token() -> str | None:
        fresh = ctx.store.get(item.pipelineExecutionId)
        if fresh is None:
            raise StaleItemError(f"{item.pipelineExecutionId} no longer exists")
        holder.item = fresh
        return fresh.taskToken

    outcome = call_with_latest_token(operation, item.taskToken, read_latest_token)
    return outcome, holder.item


def _cleanup_targets(item: ExecutionItem, ctx: PollerContext) -> _CleanupTargets:
    """Check the stored cleanup targets against the paths that the execution id determines.

    Every target is checked before anything is deleted, so that a wrong value in the item
    never deletes data.

    Args:
        item: Terminal execution.
        ctx: Poller dependencies.

    Returns:
        The targets. Attributes that were never set are left out.

    Raises:
        CleanupTargetMismatch: A stored value differs from the expected one.
    """
    pid = item.pipelineExecutionId
    expected_prefix = staging_prefix(ctx.config, pid)
    if item.stagingPrefix is not None:
        _check_cleanup_target(item, "stagingPrefix", item.stagingPrefix, expected_prefix)
    directories: list[str] = []
    for attribute, stored, expected in (
        ("outputDir", item.outputDir, output_dir(ctx.training_env, pid)),
        ("runDir", item.runDir, run_dir(ctx.training_env, pid)),
    ):
        if stored is None:
            continue
        _check_cleanup_target(item, attribute, stored.rstrip("/"), expected)
        directories.append(expected)
    return _CleanupTargets(
        staging_prefix=None if item.stagingPrefix is None else expected_prefix,
        directories=directories,
    )


def _check_cleanup_target(item: ExecutionItem, attribute: str, stored: str, expected: str) -> None:
    """Check that a stored cleanup target is exactly the expected one.

    Args:
        item: Execution, used in the message.
        attribute: Attribute name, used in the message.
        stored: Stored value.
        expected: Value that the execution id determines.

    Raises:
        CleanupTargetMismatch: The values differ.
    """
    if stored != expected:
        logger.error(
            "Refusing to clean up %s of %s: %s is not %s",
            attribute,
            item.pipelineExecutionId,
            stored,
            expected,
        )
        raise CleanupTargetMismatch(
            f"{attribute} {stored} of {item.pipelineExecutionId} is not {expected}"
        )


def _delete_staging_prefix(ctx: PollerContext, prefix: str) -> None:
    """Delete the objects under a checked staging prefix in the PASK data bucket.

    Args:
        ctx: Poller dependencies.
        prefix: ``<S3Prefix>vams/<id>/``, already checked.

    Raises:
        TransientError: S3 did not delete some objects.
    """
    bucket = ctx.config.data_bucket
    paginator = ctx.s3.get_paginator("list_objects_v2")
    keys = [
        entry["Key"]
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix)
        for entry in page.get("Contents", [])
    ]
    for start in range(0, len(keys), S3_DELETE_BATCH_SIZE):
        batch = keys[start : start + S3_DELETE_BATCH_SIZE]
        response = ctx.s3.delete_objects(
            Bucket=bucket,
            Delete={"Objects": [{"Key": key} for key in batch], "Quiet": True},
        )
        errors = response.get("Errors", [])
        if errors:
            codes = sorted({error.get("Code", "") for error in errors})
            raise TransientError(f"S3 did not delete {len(errors)} objects under {prefix}: {codes}")
    logger.info("Deleted %d staging objects under %s", len(keys), prefix)


def _delete_fsx_directory(directory: str) -> None:
    """Delete a checked directory on FSx. A missing directory counts as deleted.

    Args:
        directory: Run or output directory, already checked.
    """
    try:
        shutil.rmtree(directory)
    except FileNotFoundError:
        return


def _require_state(item: ExecutionItem, state: State) -> None:
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


def _require_outcome(item: ExecutionItem) -> None:
    """Check that an execution has a valid outcome.

    Args:
        item: Execution.

    Raises:
        ValueError: The outcome is missing or has an unknown status.
    """
    if item.outcome is None or item.outcome.get("status") not in (
        OUTCOME_SUCCEEDED,
        OUTCOME_FAILED,
    ):
        raise ValueError(f"{item.pipelineExecutionId} has no valid outcome")
