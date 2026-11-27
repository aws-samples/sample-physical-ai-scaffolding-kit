"""Run lerobot-train with MLflow logging in place of Weights & Biases.

LeRobot 0.6.1 creates ``lerobot_train.WandBLogger(cfg)`` when ``--wandb.enable=true``
and calls ``log_dict``, ``log_policy`` and ``log_video`` on it from the training loop.
This wrapper replaces that class with ``MlflowLogger`` and then calls
``lerobot_train.main()``, so LeRobot itself stays unmodified.

The ``cfg.wandb`` fields keep their LeRobot names but are interpreted as follows:
``project`` is the MLflow experiment name, ``run_id`` is the MLflow run ID (saved to
``train_config.json`` so that a resumed job continues the same run), and
``disable_artifact`` stops the adapter upload.

Usage (from ``${LEROBOT_PI_BASE_DIR}`` inside the container):
    python training/mlflow_train.py <lerobot-train arguments...>

When ``MLFLOW_TRACKING_URI`` is not set, ``lerobot_train.main()`` runs unchanged.

When ``VAMS_CONTEXT_FILE`` is set, the job was submitted by the VAMS poller. The file
(``vams_context.json``) is read before the run starts, and the run records the VAMS
execution IDs as tags, the input manifest as an artifact and the S3 input as a dataset.
The run ID is written back to the context's ``resultFile`` (``mlflow_run.json``).
When the input is a curation view (``schemaVersion`` 2, ``input.kind == "view"``), the
episodes of the view manifest are appended to ``sys.argv`` as ``--dataset.episodes``, and
the run also records the source asset, the view manifest and the normalization stats.
"""

import functools
import itertools
import json
import logging
import numbers
import os
import re
import shutil
import signal
import sys
import tempfile
from pathlib import Path

import mlflow
from mlflow.entities import Run, RunInfo
from mlflow.exceptions import MlflowException

import lerobot
import lerobot.scripts.lerobot_train as lerobot_train
from lerobot.utils.constants import PRETRAINED_MODEL_DIR

ADAPTER_MODEL_FILE = "adapter_model.safetensors"
ADAPTER_FILES = (ADAPTER_MODEL_FILE, "adapter_config.json", "config.json")
DEFAULT_ADAPTER_MAX_MB = "200"
MAX_PARAM_VALUE_LENGTH = 6000
INVALID_KEY_CHARS = re.compile(r"[^/\w.\- :]")

VAMS_CONTEXT_ENV = "VAMS_CONTEXT_FILE"
VAMS_CONTEXT_SCHEMA_VERSION_DATASET = 1
VAMS_CONTEXT_SCHEMA_VERSION_VIEW = 2
VAMS_CONTEXT_SCHEMA_VERSION = frozenset(
    {VAMS_CONTEXT_SCHEMA_VERSION_DATASET, VAMS_CONTEXT_SCHEMA_VERSION_VIEW}
)
INPUT_KIND_VIEW = "view"
INPUT_KIND_DATASET = "dataset"
DATASET_EPISODES_ARG = "--dataset.episodes"
VIEW_SOURCE_REQUIRED_KEYS = ("databaseId", "assetId", "bucket", "assetRootS3Key")
VAMS_ARTIFACT_DIR = "vams"
CURATION_MANIFEST_ARTIFACT = "curation_manifest.json"
NORM_STATS_ARTIFACT = "norm_stats.json"
DATASET_STATS_RELATIVE_PATH = Path("meta") / "stats.json"
PIPELINE_EXECUTION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
DATASET_DIGEST_PREFIX = "sha256:"
# MLflow rejects dataset digests longer than MAX_DATASET_DIGEST_SIZE (36) characters.
MLFLOW_DATASET_DIGEST_LENGTH = 36


def _sanitize_key(key: str) -> str:
    """Replace characters that MLflow rejects in param and metric names with '_'."""
    return INVALID_KEY_CHARS.sub("_", key)


def _flatten(d: dict, prefix: str) -> dict[str, str]:
    """Flatten a nested config dict into MLflow params keyed by dotted paths."""
    out = {}
    for k, v in d.items():
        key = _sanitize_key(f"{prefix}{k}")
        if isinstance(v, dict):
            out.update(_flatten(v, f"{key}."))
        else:
            out[key] = str(v)[:MAX_PARAM_VALUE_LENGTH]
    return out


def _finetune_mode(cfg) -> str:
    """Return lora / expert_only / full, matching FINETUNE_MODE in slurm_train.sh."""
    if cfg.peft is not None:
        return "lora"
    if getattr(cfg.trainable_config, "train_expert_only", False):
        return "expert_only"
    return "full"


def _append_job_id(existing: str, job_id: str) -> str:
    """Append job_id to a comma-separated list unless it is already present."""
    ids = [i for i in existing.split(",") if i]
    if job_id and job_id not in ids:
        ids.append(job_id)
    return ",".join(ids)


def _require_str(mapping: dict, key: str, label: str) -> str:
    """Return mapping[key] if it is a non-empty string, otherwise raise ValueError.

    Args:
        mapping: Dict read from vams_context.json.
        key: Key that must hold a non-empty string.
        label: Dotted path of the key, used in the error message.

    Returns:
        The string value.
    """
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"vams_context.json: {label} must be a non-empty string")
    return value


def _require_dict(mapping: dict, key: str) -> dict:
    """Return mapping[key] if it is a dict, otherwise raise ValueError.

    Args:
        mapping: Dict read from vams_context.json.
        key: Key that must hold an object.

    Returns:
        The nested dict.
    """
    value = mapping.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"vams_context.json: {key} must be an object")
    return value


def _require_non_negative_int(mapping: dict, key: str, label: str) -> int:
    """Return mapping[key] if it is an int of 0 or more, otherwise raise ValueError.

    Args:
        mapping: Dict read from vams_context.json.
        key: Key that must hold a non-negative integer.
        label: Dotted path of the key, used in the error message.

    Returns:
        The integer value.
    """
    value = mapping.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"vams_context.json: {label} must be a non-negative integer")
    return value


def is_view_context(vams_context: dict) -> bool:
    """Return whether a validated VAMS context describes a curation view input.

    Args:
        vams_context: Context from load_vams_context.

    Returns:
        True when input.kind is "view".
    """
    return vams_context["input"].get("kind") == INPUT_KIND_VIEW


def _validate_view_section(context: dict) -> None:
    """Validate the view section of a schemaVersion 2 vams_context.json.

    Args:
        context: Parsed vams_context.json whose input has already been checked.

    Raises:
        ValueError: If input.kind is not "view" or the view section is malformed.
    """
    if context["input"].get("kind") != INPUT_KIND_VIEW:
        raise ValueError(f'vams_context.json: schemaVersion 2 requires input.kind "{INPUT_KIND_VIEW}"')
    view = _require_dict(context, "view")
    _require_str(view, "manifestFile", "view.manifestFile")
    _require_non_negative_int(view, "episodeCount", "view.episodeCount")
    _require_non_negative_int(view, "excludedEpisodeCount", "view.excludedEpisodeCount")
    source = view.get("source")
    if not isinstance(source, dict):
        raise ValueError("vams_context.json: view.source must be an object")
    for key in VIEW_SOURCE_REQUIRED_KEYS:
        _require_str(source, key, f"view.source.{key}")


def load_vams_context(path: str) -> dict:
    """Read and validate vams_context.json written by the VAMS poller.

    An unreadable file or invalid JSON raises OSError or json.JSONDecodeError, so that
    a VAMS job fails before training instead of creating a run without its lineage.

    Args:
        path: Path of vams_context.json (the value of VAMS_CONTEXT_FILE).

    Returns:
        The parsed context.
    """
    with open(path, encoding="utf-8") as f:
        context = json.load(f)
    if not isinstance(context, dict):
        raise ValueError("vams_context.json: the top level must be an object")
    schema_version = context.get("schemaVersion")
    is_integer_version = isinstance(schema_version, int) and not isinstance(schema_version, bool)
    if not is_integer_version or schema_version not in VAMS_CONTEXT_SCHEMA_VERSION:
        raise ValueError(
            f"vams_context.json: unsupported schemaVersion {context.get('schemaVersion')!r}"
        )
    if context.get("trigger") != "vams":
        raise ValueError(f"vams_context.json: unexpected trigger {context.get('trigger')!r}")
    pipeline_execution_id = _require_str(context, "pipelineExecutionId", "pipelineExecutionId")
    if not PIPELINE_EXECUTION_ID_PATTERN.fullmatch(pipeline_execution_id):
        raise ValueError(f"vams_context.json: invalid pipelineExecutionId {pipeline_execution_id!r}")
    input_info = _require_dict(context, "input")
    for key in ("assetId", "bucket", "assetRootS3Key"):
        _require_str(input_info, key, f"input.{key}")
    if not isinstance(input_info.get("versioned"), bool):
        raise ValueError("vams_context.json: input.versioned must be a boolean")
    if schema_version == VAMS_CONTEXT_SCHEMA_VERSION_VIEW:
        _validate_view_section(context)
    elif input_info.get("kind") == INPUT_KIND_VIEW:
        # A view must come with the view section, or training would use every episode.
        raise ValueError(
            f'vams_context.json: input.kind "{INPUT_KIND_VIEW}" requires schemaVersion '
            f"{VAMS_CONTEXT_SCHEMA_VERSION_VIEW}"
        )
    _require_dict(context, "output")
    digest = _require_str(context, "datasetDigest", "datasetDigest")
    if not digest.startswith(DATASET_DIGEST_PREFIX):
        raise ValueError(f"vams_context.json: datasetDigest must start with {DATASET_DIGEST_PREFIX}")
    _require_str(context, "inputManifestFile", "inputManifestFile")
    _require_str(context, "resultFile", "resultFile")
    return context


def build_trigger_tags(vams_context: dict | None) -> dict[str, str]:
    """Build pask.trigger and, for a VAMS job, the vams.* tags.

    Empty values are kept here; MlflowLogger drops them together with its other tags.

    Args:
        vams_context: Context from load_vams_context, or None for a manual run.

    Returns:
        Tag names mapped to string values.
    """
    if vams_context is None:
        return {"pask.trigger": "manual"}
    input_info = vams_context["input"]
    output_info = vams_context["output"]
    tags = {
        "pask.trigger": "vams",
        "vams.workflow_database_id": vams_context.get("workflowDatabaseId", ""),
        "vams.workflow_id": vams_context.get("workflowId", ""),
        "vams.workflow_execution_id": vams_context.get("workflowExecutionId", ""),
        "vams.pipeline_execution_id": vams_context["pipelineExecutionId"],
        "vams.pipeline_name": vams_context.get("pipelineName", ""),
        "vams.executing_user_name": vams_context.get("executingUserName", ""),
        "vams.input.database_id": input_info.get("databaseId", ""),
        "vams.input.asset_id": input_info["assetId"],
        "vams.input.asset_name": input_info.get("assetName", ""),
        "vams.input.versioned": "true" if input_info["versioned"] else "false",
        "vams.output.asset_id": output_info.get("assetId", ""),
        "vams.dataset_digest": vams_context["datasetDigest"],
        "vams.input.kind": input_info.get("kind") or INPUT_KIND_DATASET,
    }
    if is_view_context(vams_context):
        tags.update(_build_view_tags(vams_context["view"]))
    return tags


def _build_view_tags(view: dict) -> dict[str, str]:
    """Build the vams.source.* and vams.view.* tags of a curation view input.

    Args:
        view: The view section of a validated vams_context.json.

    Returns:
        Tag names mapped to string values; optional fields that are absent or null map
        to "" so that MlflowLogger drops them.
    """
    source = view["source"]
    return {
        "vams.source.database_id": source["databaseId"],
        "vams.source.asset_id": source["assetId"],
        "vams.source.asset_version": source.get("assetVersion") or "",
        "vams.view.episode_count": str(view["episodeCount"]),
        "vams.view.excluded_episode_count": str(view["excludedEpisodeCount"]),
        "vams.view.review_updated_at": view.get("reviewUpdatedAt") or "",
    }


def _read_view_episodes(manifest_file: str, expected_count: int) -> list[int]:
    """Read and validate the episodes list of a curation view manifest.

    Args:
        manifest_file: Path of curation_manifest.json (view.manifestFile).
        expected_count: view.episodeCount from vams_context.json.

    Returns:
        The episode indices, non-empty and strictly ascending.

    Raises:
        ValueError: If the manifest or its episodes list is malformed, or the number of
            episodes differs from view.episodeCount.
    """
    with open(manifest_file, encoding="utf-8") as f:
        manifest = json.load(f)
    if not isinstance(manifest, dict):
        raise ValueError("curation_manifest.json: the top level must be an object")
    episodes = manifest.get("episodes")
    if not isinstance(episodes, list) or not episodes:
        raise ValueError("curation_manifest.json: episodes must be a non-empty list")
    for episode in episodes:
        if isinstance(episode, bool) or not isinstance(episode, int) or episode < 0:
            raise ValueError(
                f"curation_manifest.json: episode {episode!r} is not a non-negative integer"
            )
    if any(earlier >= later for earlier, later in itertools.pairwise(episodes)):
        raise ValueError("curation_manifest.json: episodes must be strictly ascending")
    if len(episodes) != expected_count:
        raise ValueError(
            f"curation_manifest.json has {len(episodes)} episodes, "
            f"but vams_context.json view.episodeCount is {expected_count}"
        )
    return episodes


def apply_view_episodes(vams_context: dict, argv: list[str]) -> list[str]:
    """Return argv with --dataset.episodes of the view appended, for a curation view input.

    The episodes are added inside the process rather than on the srun command line, so a
    long list is not limited by the per-argument size limit of Linux (MAX_ARG_STRLEN).

    Args:
        vams_context: Context from load_vams_context.
        argv: Command line to extend (normally sys.argv); it is not modified.

    Returns:
        A new list; for a non-view input it equals argv.

    Raises:
        ValueError: If argv already has --dataset.episodes or the view manifest is invalid.
    """
    if not is_view_context(vams_context):
        return list(argv)
    if any(arg == DATASET_EPISODES_ARG or arg.startswith(f"{DATASET_EPISODES_ARG}=") for arg in argv):
        raise ValueError(f"{DATASET_EPISODES_ARG} must not be given for a VAMS view input")
    view = vams_context["view"]
    episodes = _read_view_episodes(view["manifestFile"], view["episodeCount"])
    # No spaces, so that draccus parses the value as a list.
    return [*argv, f"{DATASET_EPISODES_ARG}=[{','.join(str(episode) for episode in episodes)}]"]


def find_vams_run(experiment_id: str, pipeline_execution_id: str) -> Run | None:
    """Find the run that an earlier job of the same VAMS execution created.

    Args:
        experiment_id: Experiment to search.
        pipeline_execution_id: VAMS pipelineExecutionId of this job.

    Returns:
        The run tagged with this pipelineExecutionId, or None if there is none.
    """
    # The ID is put into the filter string, so reject anything that could change the query.
    if not PIPELINE_EXECUTION_ID_PATTERN.fullmatch(pipeline_execution_id):
        raise ValueError(f"Invalid pipelineExecutionId {pipeline_execution_id!r}")
    runs = mlflow.search_runs(
        experiment_ids=[experiment_id],
        filter_string=f"tags.`vams.pipeline_execution_id` = '{pipeline_execution_id}'",
        max_results=1,
        output_format="list",
    )
    return runs[0] if runs else None


def write_mlflow_run_file(result_file: str, run_info: RunInfo) -> None:
    """Write mlflow_run.json, the link from the VAMS execution back to the MLflow run.

    The file is written to a temporary file in the same directory and renamed, so the
    poller never reads a partially written file.

    Args:
        result_file: Destination path (resultFile in vams_context.json).
        run_info: Info of the active run.
    """
    content = {
        "run_id": run_info.run_id,
        "experiment_id": run_info.experiment_id,
        "tracking_uri": mlflow.get_tracking_uri(),
    }
    result_path = Path(result_file)
    fd, tmp_path = tempfile.mkstemp(dir=result_path.parent, prefix=f".{result_path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(content, f)
        os.replace(tmp_path, result_path)
    except BaseException:
        os.unlink(tmp_path)
        raise


def _mlflow_dataset_digest(dataset_digest: str) -> str:
    """Shorten a "sha256:<hex>" digest to the hex prefix that fits in an MLflow dataset.

    Args:
        dataset_digest: datasetDigest from vams_context.json.

    Returns:
        The first MLFLOW_DATASET_DIGEST_LENGTH hex characters. The full value stays in
        the vams.dataset_digest tag.
    """
    return dataset_digest.removeprefix(DATASET_DIGEST_PREFIX)[:MLFLOW_DATASET_DIGEST_LENGTH]


def log_vams_dataset(vams_context: dict) -> None:
    """Register the S3 input of a VAMS job as an MLflow dataset input of the active run.

    The dataset only adds a view of the lineage that the tags and the input manifest
    artifact already record, so a failure is logged as a warning and training continues.

    Args:
        vams_context: Context from load_vams_context.
    """
    input_info = vams_context["input"]
    # A view has no files of its own; the files it trains on are those of the source asset.
    location = vams_context["view"]["source"] if is_view_context(vams_context) else input_info
    source_uri = f"s3://{location['bucket']}/{location['assetRootS3Key']}"
    try:
        from mlflow.data.dataset_source_registry import resolve_dataset_source
        from mlflow.data.meta_dataset import MetaDataset

        dataset = MetaDataset(
            source=resolve_dataset_source(source_uri),
            name=input_info.get("assetName") or input_info["assetId"],
            digest=_mlflow_dataset_digest(vams_context["datasetDigest"]),
        )
        mlflow.log_input(dataset, context="training")
    except (ImportError, MlflowException) as e:
        logging.warning(f"Could not log the VAMS input {source_uri} as an MLflow dataset: {e}")


def _log_artifact_as(local_path: Path, artifact_name: str) -> None:
    """Log a file as VAMS_ARTIFACT_DIR/artifact_name regardless of its local file name.

    Args:
        local_path: File to upload.
        artifact_name: File name of the artifact under VAMS_ARTIFACT_DIR.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        renamed_path = Path(tmp_dir) / artifact_name
        shutil.copyfile(local_path, renamed_path)
        mlflow.log_artifact(str(renamed_path), VAMS_ARTIFACT_DIR)


def log_vams_inputs(vams_context: dict, dataset_root: str | Path | None) -> None:
    """Record the input artifacts and the dataset input of a VAMS job.

    For a curation view, the view manifest and the normalization stats that
    compute_view_stats.py wrote to <dataset_root>/meta/stats.json are also recorded.

    Args:
        vams_context: Context from load_vams_context.
        dataset_root: cfg.dataset.root of the run; required for a view input.

    Raises:
        ValueError: If the input is a view and dataset_root is not set.
    """
    # The poller names the file input_manifest.json, so it is stored as vams/input_manifest.json.
    mlflow.log_artifact(vams_context["inputManifestFile"], VAMS_ARTIFACT_DIR)
    if is_view_context(vams_context):
        if not dataset_root:
            raise ValueError("--dataset.root must be set for a VAMS view input")
        _log_artifact_as(Path(vams_context["view"]["manifestFile"]), CURATION_MANIFEST_ARTIFACT)
        _log_artifact_as(Path(dataset_root) / DATASET_STATS_RELATIVE_PATH, NORM_STATS_ARTIFACT)
    log_vams_dataset(vams_context)


class MlflowLogger:
    """Drop-in replacement for lerobot.common.wandb_utils.WandBLogger."""

    def __init__(self, cfg, vams_context: dict | None):
        self._cfg = cfg
        resume_run_id = cfg.wandb.run_id if cfg.resume else None
        if resume_run_id:
            # Follow the run's own experiment; start_run rejects a run from another experiment.
            existing_run = mlflow.get_run(resume_run_id)
            mlflow.set_experiment(experiment_id=existing_run.info.experiment_id)
        else:
            experiment = mlflow.set_experiment(cfg.wandb.project)
            # A VAMS execution keeps one run even if the poller submits it more than once.
            existing_run = (
                find_vams_run(experiment.experiment_id, vams_context["pipelineExecutionId"])
                if vams_context
                else None
            )
        existing_tags = existing_run.data.tags if existing_run else {}
        # Opens the experiment in the model-training view of the MLflow UI.
        mlflow.set_experiment_tag("mlflow.experimentKind", "custom_model_development")

        if existing_run:
            mlflow.start_run(run_id=existing_run.info.run_id, log_system_metrics=True)
        else:
            mlflow.start_run(run_name=cfg.job_name, log_system_metrics=True)
            # Params are immutable in MLflow, so they are logged only when the run is created.
            config = json.loads(json.dumps(cfg.to_dict(), default=str))
            mlflow.log_params(_flatten(config, ""))
            mlflow.log_dict(config, "train_config.json")
            # Resuming overwrites pretrained_path with the checkpoint dir, so keep the base weights.
            pretrained_path = cfg.trainable_config.pretrained_path
            if pretrained_path:
                mlflow.set_tag("lerobot.pretrained_path", str(pretrained_path))

        run_info = mlflow.active_run().info
        # Saved in train_config.json so that RESUME=true continues this run.
        cfg.wandb.run_id = run_info.run_id
        if vams_context:
            write_mlflow_run_file(vams_context["resultFile"], run_info)

        job_id = os.environ.get("SLURM_JOB_ID", "")
        tags = {
            "lerobot.version": lerobot.__version__,
            "lerobot.policy_type": cfg.trainable_config.type,
            "lerobot.finetune_mode": _finetune_mode(cfg),
            "lerobot.dataset_repo_id": cfg.dataset.repo_id if cfg.dataset else "",
            "pask.output_dir": str(cfg.output_dir) if cfg.output_dir else "",
            "pask.image_tag": os.environ.get("IMAGE_TAG", ""),
            "pask.cluster": os.environ.get("PASK_CLUSTER_NAME", ""),
            "pask.run_type": "train",
            "slurm.job_id": job_id,
            "slurm.job_ids": _append_job_id(existing_tags.get("slurm.job_ids", ""), job_id),
            **build_trigger_tags(vams_context),
        }
        mlflow.set_tags({k: v for k, v in tags.items() if v})
        if vams_context:
            log_vams_inputs(vams_context, cfg.dataset.root if cfg.dataset else None)
        logging.info(f"Logging to MLflow run {cfg.wandb.run_id}")

    def log_dict(
        self, d: dict, step: int | None = None, mode: str = "train", custom_step_key: str | None = None
    ):
        """Log the numeric values of d as '<mode>/<key>' metrics at step."""
        metrics = {
            _sanitize_key(f"{mode}/{k}"): float(v)
            for k, v in d.items()
            if isinstance(v, numbers.Real) and not isinstance(v, bool)
        }
        if metrics:
            mlflow.log_metrics(metrics, step=step)

    def log_policy(self, checkpoint_dir: Path):
        """Record the checkpoint and upload the LoRA adapter of the final step."""
        step = int(checkpoint_dir.name)
        mlflow.log_metric("checkpoint_step", step, step=step)
        mlflow.set_tag("pask.last_checkpoint_dir", str(checkpoint_dir))
        if self._cfg.wandb.disable_artifact or step != self._cfg.steps:
            return

        model_dir = checkpoint_dir / PRETRAINED_MODEL_DIR
        if not (model_dir / ADAPTER_MODEL_FILE).exists():
            logging.info(f"No {ADAPTER_MODEL_FILE} in {model_dir}; skipping the MLflow adapter upload")
            return
        files = [model_dir / name for name in ADAPTER_FILES if (model_dir / name).exists()]
        total_bytes = sum(f.stat().st_size for f in files)
        max_mb = int(os.environ.get("MLFLOW_ADAPTER_MAX_MB", DEFAULT_ADAPTER_MAX_MB))
        if total_bytes > max_mb * 1024 * 1024:
            logging.info(
                f"Adapter files total {total_bytes / 1024 / 1024:.1f} MB, over MLFLOW_ADAPTER_MAX_MB={max_mb}; "
                "skipping the MLflow adapter upload"
            )
            return
        for f in files:
            mlflow.log_artifact(str(f), f"adapter/{checkpoint_dir.name}")

    def log_video(self, video_path: str, step: int, mode: str = "train"):
        """Upload an evaluation video as an artifact."""
        mlflow.log_artifact(str(video_path), f"videos/{mode}")


def _end_run_on_sigterm(signum, frame):
    # Slurm sends SIGTERM at the time limit or on scancel; atexit would mark the run FINISHED.
    if mlflow.active_run():
        mlflow.end_run(status="KILLED")
    sys.exit(128 + signum)


def main():
    if not os.environ.get("MLFLOW_TRACKING_URI"):
        lerobot_train.main()
        return

    vams_context_file = os.environ.get(VAMS_CONTEXT_ENV, "")
    vams_context = load_vams_context(vams_context_file) if vams_context_file else None
    if vams_context:
        sys.argv = apply_view_episodes(vams_context, sys.argv)
    lerobot_train.WandBLogger = functools.partial(MlflowLogger, vams_context=vams_context)
    # sagemaker-mlflow resolves credentials for every request, and lerobot-train logs
    # at INFO, so botocore would log "Found credentials" each time metrics are sent.
    logging.getLogger("botocore.credentials").setLevel(logging.WARNING)
    signal.signal(signal.SIGTERM, _end_run_on_sigterm)
    try:
        lerobot_train.main()
    except BaseException:
        # Without this, MLflow's atexit handler would mark a failed run as FINISHED.
        if mlflow.active_run():
            mlflow.end_run(status="FAILED")
        raise
    if mlflow.active_run():
        mlflow.end_run()


if __name__ == "__main__":
    main()
