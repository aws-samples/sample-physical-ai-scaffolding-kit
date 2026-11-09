"""SUBMITTING handler: submit the training job once, or record the job already submitted."""

import json
import logging
import os
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING

from pask_vams_poller import config
from pask_vams_poller.constants import (
    CURATION_MANIFEST_FILE,
    INPUT_KIND_DATASET,
    INPUT_KIND_VIEW,
    INPUT_MANIFEST_FILE,
    JOB_STARTED_FILE,
    VAMS_CONTEXT_FILE,
    VIEW_DATASET_DIR,
    ErrorName,
    State,
)
from pask_vams_poller.errors import PermanentFailure, TransientError
from pask_vams_poller.slurm import SlurmCommandError
from pask_vams_poller.staging import load_vams_input, run_state_handler, send_heartbeat

if TYPE_CHECKING:
    from pask_vams_poller.context import PollerContext
    from pask_vams_poller.store import ExecutionItem

logger = logging.getLogger(__name__)

TRAINING_SCRIPT = "slurm_train.sh"
SLURM_OUTPUT_FILE_PATTERN = re.compile(r"^slurm_(?P<job_id>[0-9]+)\.out$")
SLURM_JOB_ID_PATTERN = re.compile(r"^[0-9]+$")
INHERITED_ENV_KEYS: tuple[str, ...] = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "LANG",
    "LC_ALL",
    "TZ",
    "SLURM_CONF",
)
DATASET_REPO_ID_PREFIX = "vams"
VIEW_MANIFEST_ENV_KEY = "VAMS_VIEW_MANIFEST_FILE"


def process_submitting(item: "ExecutionItem", ctx: "PollerContext") -> None:
    """Handle an execution in SUBMITTING.

    Args:
        item: Execution in SUBMITTING whose lease this poller holds.
        ctx: Poller context.
    """
    run_state_handler(item, ctx, _submit)


def _submit(item: "ExecutionItem", ctx: "PollerContext") -> None:
    """Body of ``process_submitting``.

    Sends a heartbeat first. When the job is already in the queue or left its marker
    files in the run directory, only its id is recorded. Otherwise the job is submitted.

    Args:
        item: Execution in SUBMITTING.
        ctx: Poller context.

    Raises:
        ValueError: The execution is not in SUBMITTING or has no run or output directory.
        PermanentFailure: ``PASK.SubmitFailed`` when sbatch failed for good,
            ``PASK.InvalidRequest`` when the request files became invalid, or
            ``PASK.StagingFailed`` when the staged input manifest is missing or invalid.
        TransientError: Slurm, S3, or Step Functions failed temporarily.
        StaleItemError: The execution changed.
    """
    if item.state is not State.SUBMITTING:
        raise ValueError(f"{item.pipelineExecutionId} must be SUBMITTING, got {item.state.value}")
    if not item.runDir or not item.outputDir:
        raise ValueError(f"{item.pipelineExecutionId} has no runDir or outputDir")
    if not send_heartbeat(item, ctx):
        return
    job_name = config.job_name(item.pipelineExecutionId)
    job_id = find_submitted_job_id(ctx, job_name, item.runDir)
    if job_id is None:
        job_id = _submit_training_job(item, ctx, job_name)
        logger.info("Submitted %s as Slurm job %s", job_name, job_id)
    else:
        logger.info("Slurm job %s of %s was already submitted", job_id, job_name)
    ctx.store.transition(item, State.SUBMITTED, set_attrs={"slurmJobId": job_id})


def find_submitted_job_id(ctx: "PollerContext", job_name: str, run_directory: str) -> str | None:
    """Find the job of an execution that an earlier attempt already submitted.

    The queue is checked first. A job that already left the queue is found through
    ``job_started.json`` or a ``slurm_<id>.out`` file in the run directory.

    Args:
        ctx: Poller context.
        job_name: Slurm job name of the execution.
        run_directory: Run directory of the execution.

    Returns:
        The job id, or None when the job was never submitted.

    Raises:
        TransientError: squeue failed temporarily.
        SlurmCommandError: squeue failed for another reason.
        ValueError: ``job_started.json`` does not hold a job id.
    """
    try:
        queued_jobs = ctx.slurm.find_by_name(job_name)
    except SlurmCommandError as error:
        if error.transient:
            raise TransientError(f"squeue failed temporarily: {error}") from error
        raise
    if queued_jobs:
        if len(queued_jobs) > 1:
            logger.warning("%d jobs are named %s; recording the first", len(queued_jobs), job_name)
        return queued_jobs[0].job_id
    return job_id_from_run_directory(run_directory)


def job_id_from_run_directory(run_directory: str) -> str | None:
    """Read the job id from the marker files that a submitted job leaves in the run directory.

    Args:
        run_directory: Run directory of the execution.

    Returns:
        The ``slurmJobId`` of ``job_started.json``, else the id in the newest
        ``slurm_<id>.out`` file name, else None.

    Raises:
        ValueError: ``job_started.json`` does not hold a numeric job id.
    """
    started_path = os.path.join(run_directory, JOB_STARTED_FILE)
    if os.path.exists(started_path):
        with open(started_path, encoding="utf-8") as started_file:
            job_id = json.load(started_file).get("slurmJobId")
        if not isinstance(job_id, str) or not SLURM_JOB_ID_PATTERN.fullmatch(job_id):
            raise ValueError(f"{JOB_STARTED_FILE} has no valid slurmJobId: {job_id!r}")
        return job_id

    output_job_ids = [
        int(match.group("job_id"))
        for match in (
            SLURM_OUTPUT_FILE_PATTERN.fullmatch(name) for name in os.listdir(run_directory)
        )
        if match is not None
    ]
    if not output_job_ids:
        return None
    if len(output_job_ids) > 1:
        logger.warning("Run directory has output files of %d jobs", len(output_job_ids))
    return str(max(output_job_ids))


def read_input_kind(run_directory: str) -> str:
    """Read the kind of the staged input from ``input_manifest.json`` in the run directory.

    A manifest without ``kind`` belongs to a dataset asset.

    Args:
        run_directory: Run directory of the execution.

    Returns:
        ``INPUT_KIND_VIEW`` or ``INPUT_KIND_DATASET``.

    Raises:
        PermanentFailure: ``PASK.StagingFailed`` when the manifest is missing, is not a
            UTF-8 JSON object, or names an unknown kind.
    """
    manifest_path = os.path.join(run_directory, INPUT_MANIFEST_FILE)
    try:
        with open(manifest_path, encoding="utf-8") as manifest_file:
            manifest = json.load(manifest_file)
    except FileNotFoundError as error:
        raise PermanentFailure(
            ErrorName.STAGING_FAILED, f"{INPUT_MANIFEST_FILE} is missing from the run directory"
        ) from error
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PermanentFailure(
            ErrorName.STAGING_FAILED, f"{INPUT_MANIFEST_FILE} is not valid JSON"
        ) from error
    if not isinstance(manifest, dict):
        raise PermanentFailure(
            ErrorName.STAGING_FAILED, f"{INPUT_MANIFEST_FILE} is not a JSON object"
        )
    kind = manifest.get("kind", INPUT_KIND_DATASET)
    if kind not in (INPUT_KIND_VIEW, INPUT_KIND_DATASET):
        raise PermanentFailure(
            ErrorName.STAGING_FAILED, f"{INPUT_MANIFEST_FILE} has an unknown kind: {kind!r}"
        )
    return kind


def build_job_env(
    item: "ExecutionItem",
    ctx: "PollerContext",
    training_params: Mapping[str, str],
    database_id: str,
    asset_id: str,
    base_environ: Mapping[str, str],
    *,
    input_kind: str,
) -> dict[str, str]:
    """Build the complete environment of the sbatch process.

    Later sources override earlier ones: the basic variables of the poller process, the
    training environment file, the training parameters, and the values the poller decides.
    For a view, the job reads the dataset from the view root in the run directory and
    gets the curation manifest through ``VAMS_VIEW_MANIFEST_FILE``. For a dataset,
    ``VAMS_VIEW_MANIFEST_FILE`` is left out even when an earlier source sets it.

    Args:
        item: Execution with ``runDir`` and ``outputDir``.
        ctx: Poller context.
        training_params: Training parameters that passed the allow list.
        database_id: VAMS database id of the input asset.
        asset_id: VAMS asset id of the input asset.
        base_environ: Environment of the poller process, such as ``os.environ``.
        input_kind: ``INPUT_KIND_VIEW`` or ``INPUT_KIND_DATASET``.

    Returns:
        The environment.

    Raises:
        ValueError: ``input_kind`` is not a known kind.
    """
    run_directory = item.runDir
    if input_kind == INPUT_KIND_VIEW:
        input_values = {
            "DATASET_ROOT": os.path.join(run_directory, VIEW_DATASET_DIR),
            VIEW_MANIFEST_ENV_KEY: os.path.join(run_directory, CURATION_MANIFEST_FILE),
        }
    elif input_kind == INPUT_KIND_DATASET:
        input_values = {
            "DATASET_ROOT": config.fsx_dataset_root(ctx.config, item.pipelineExecutionId),
        }
    else:
        raise ValueError(f"unknown input kind: {input_kind!r}")
    poller_values = {
        **input_values,
        "DATASET_REPO_ID": f"{DATASET_REPO_ID_PREFIX}/{database_id}/{asset_id}",
        "OUTPUT_DIR": item.outputDir,
        "VAMS_RUN_DIR": run_directory,
        "VAMS_CONTEXT_FILE": os.path.join(run_directory, VAMS_CONTEXT_FILE),
        "RESUME": "false",
        "MLFLOW_EXPERIMENT_NAME": ctx.config.mlflow_experiment_name,
    }
    inherited = {key: base_environ[key] for key in INHERITED_ENV_KEYS if key in base_environ}
    env = {**inherited, **ctx.training_env, **training_params, **poller_values}
    if input_kind == INPUT_KIND_DATASET:
        env.pop(VIEW_MANIFEST_ENV_KEY, None)
    return env


def _submit_training_job(item: "ExecutionItem", ctx: "PollerContext", job_name: str) -> str:
    """Submit ``slurm_train.sh`` for an execution.

    Args:
        item: Execution in SUBMITTING.
        ctx: Poller context.
        job_name: Slurm job name of the execution.

    Returns:
        The job id.

    Raises:
        PermanentFailure: ``PASK.SubmitFailed`` when sbatch failed for good, or
            ``PASK.StagingFailed`` when the staged input manifest is missing or invalid.
        TransientError: sbatch failed temporarily.
    """
    vams_input = load_vams_input(item, ctx)
    kind = read_input_kind(item.runDir)
    env = build_job_env(
        item,
        ctx,
        vams_input.training_params,
        vams_input.asset_entry["databaseId"],
        vams_input.asset_entry["assetId"],
        os.environ,
        input_kind=kind,
    )
    try:
        return ctx.slurm.submit(
            job_name=job_name,
            script=TRAINING_SCRIPT,
            script_args=[job_name],
            chdir=config.training_dir(ctx.training_env),
            output=os.path.join(item.runDir, "slurm_%j.out"),
            error=os.path.join(item.runDir, "slurm_%j.err"),
            env=env,
            partition=ctx.config.slurm_partition or None,
        )
    except SlurmCommandError as error:
        if error.transient:
            raise TransientError(f"sbatch failed temporarily: {error}") from error
        raise PermanentFailure(ErrorName.SUBMIT_FAILED, str(error)) from error
