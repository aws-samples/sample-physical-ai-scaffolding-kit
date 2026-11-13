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
"""

import json
import logging
import numbers
import os
import re
import signal
import sys
from pathlib import Path

import mlflow

import lerobot
import lerobot.scripts.lerobot_train as lerobot_train
from lerobot.utils.constants import PRETRAINED_MODEL_DIR

ADAPTER_MODEL_FILE = "adapter_model.safetensors"
ADAPTER_FILES = (ADAPTER_MODEL_FILE, "adapter_config.json", "config.json")
DEFAULT_ADAPTER_MAX_MB = "200"
MAX_PARAM_VALUE_LENGTH = 6000
INVALID_KEY_CHARS = re.compile(r"[^/\w.\- :]")


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


class MlflowLogger:
    """Drop-in replacement for lerobot.common.wandb_utils.WandBLogger."""

    def __init__(self, cfg):
        self._cfg = cfg
        resume_run_id = cfg.wandb.run_id if cfg.resume else None
        if resume_run_id:
            # Follow the run's own experiment; start_run rejects a run from another experiment.
            existing_run = mlflow.get_run(resume_run_id)
            mlflow.set_experiment(experiment_id=existing_run.info.experiment_id)
            existing_tags = existing_run.data.tags
        else:
            mlflow.set_experiment(cfg.wandb.project)
            existing_tags = {}
        # Opens the experiment in the model-training view of the MLflow UI.
        mlflow.set_experiment_tag("mlflow.experimentKind", "custom_model_development")

        if resume_run_id:
            mlflow.start_run(run_id=resume_run_id, log_system_metrics=True)
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

        # Saved in train_config.json so that RESUME=true continues this run.
        cfg.wandb.run_id = mlflow.active_run().info.run_id

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
        }
        mlflow.set_tags({k: v for k, v in tags.items() if v})
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

    lerobot_train.WandBLogger = MlflowLogger
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
