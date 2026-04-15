"""Pipeline commands: eval (and later train, run).

Shared logic for loading run configs, resolving model configs, and generating sbatch scripts.
"""

from datetime import datetime
from pathlib import Path

import yaml

from .ssh import Session


def _load_run_config(config_path: str) -> dict:
    """Load and validate a run config yaml."""
    p = Path(config_path)
    if not p.exists():
        raise SystemExit(f"Config not found: {config_path}")
    with open(p) as f:
        cfg = yaml.safe_load(f)
    for key in ("model", "resources"):
        if key not in cfg:
            raise SystemExit(f"Missing '{key}' in {config_path}")
    if "config_dir" not in cfg.get("model", {}):
        raise SystemExit(f"Missing 'model.config_dir' in {config_path}")
    return cfg


def _resolve_model_config(config_dir: str, model_config_roots: list[str]) -> Path:
    """Resolve a relative model config dir against search paths."""
    for root in model_config_roots:
        candidate = Path(root).expanduser() / config_dir
        if candidate.is_dir():
            return candidate.resolve()
    searched = "\n  ".join(model_config_roots) if model_config_roots else "(none)"
    raise SystemExit(
        f"Model config dir '{config_dir}' not found.\n"
        f"Searched:\n  {searched}\n"
        f"Set model_config_roots in ~/.physai/config.yaml or pass --model-config-root."
    )


def _get_resources(run_cfg: dict, stage: str) -> dict:
    """Extract resources for a pipeline stage."""
    res = run_cfg.get("resources", {}).get(stage)
    if not res:
        raise SystemExit(f"No resources.{stage} in config")
    if "container" not in res:
        raise SystemExit(f"No container in resources.{stage}")
    return res


def _generate_eval_sbatch(
    run_cfg: dict,
    res: dict,
    run_id: str,
    remote_config: str,
    remote_model_config: str,
    checkpoint_dir: str,
    output_dir: str,
    eval_rounds: int,
    visual: bool,
) -> str:
    """Generate sbatch for an eval job."""
    container = res["container"]
    partition = res.get("partition", "gpu")
    gres = res.get("gres", "gpu:1")
    visual_flag = " --visual" if visual else ""

    return f"""\
#!/bin/bash
#SBATCH --job-name=physai/eval/{container}
#SBATCH --comment="checkpoint={checkpoint_dir}"
#SBATCH --partition={partition}
#SBATCH --gres={gres}
#SBATCH --output=/fsx/physai/logs/%j.out
set -eo pipefail
export RUN_CONFIG={remote_config}
export DISPLAY=${{DISPLAY:-:0}}
export PYTHONUNBUFFERED=1

srun --container-image=/fsx/enroot/{container}.sqsh \\
  --container-mounts=/fsx:/fsx,/tmp/.X11-unix:/tmp/.X11-unix \\
  bash /app/eval.sh \\
    {checkpoint_dir} \\
    {remote_model_config} \\
    {output_dir} \\
    {eval_rounds}{visual_flag}
"""


def run_eval(
    session: Session,
    config_path: str,
    checkpoint: str,
    model_config_roots: list[str],
    eval_rounds: int = 20,
    visual: bool = False,
) -> None:
    """Submit an eval job."""
    run_cfg = _load_run_config(config_path)
    res = _get_resources(run_cfg, "eval")

    # Resolve model config locally
    model_config_dir = run_cfg["model"]["config_dir"]
    local_model_config = _resolve_model_config(model_config_dir, model_config_roots)

    # Run ID and remote paths
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_id = f"eval-{ts}"
    sync_dir = f"/fsx/physai/sync/{run_id}"
    remote_config = f"{sync_dir}/run_config.yaml"
    remote_model_config = f"{sync_dir}/model_config"
    checkpoint_dir = f"/fsx/checkpoints/{checkpoint}"
    output_dir = f"/fsx/evaluations/{run_id}"

    sbatch_content = _generate_eval_sbatch(
        run_cfg,
        res,
        run_id,
        remote_config,
        remote_model_config,
        checkpoint_dir,
        output_dir,
        eval_rounds,
        visual,
    )

    # Sync to cluster
    session.run(f"mkdir -p {sync_dir} {output_dir}")
    session.rsync(f"{Path(config_path).resolve()}", f"{remote_config}")
    session.rsync(f"{local_model_config}/", f"{remote_model_config}/")
    session.write_file(f"{sync_dir}/eval.sbatch", sbatch_content)

    # Verify checkpoint exists
    try:
        session.run(f"test -d {checkpoint_dir}")
    except RuntimeError:
        raise SystemExit(f"Checkpoint not found on cluster: {checkpoint_dir}")

    # Submit
    job_id = session.run(f"sbatch --parsable {sync_dir}/eval.sbatch")
    print(f"Submitted eval job {job_id}")
    print(f"  Config:     {config_path}")
    print(f"  Checkpoint: {checkpoint}")
    print(f"  Output:     {output_dir}")
    print(f"  Reconnect:  physai logs {job_id}")
    print(flush=True)

    session.stream_log(job_id)


def _generate_train_sbatch(
    run_cfg: dict,
    res: dict,
    run_id: str,
    remote_config: str,
    remote_model_config: str,
    dataset_dir: str,
    output_dir: str,
    max_steps: int,
) -> str:
    """Generate sbatch for a train job."""
    container = res["container"]
    partition = res.get("partition", "gpu")
    gres = res.get("gres", "gpu:1")

    return f"""\
#!/bin/bash
#SBATCH --job-name=physai/train/{container}
#SBATCH --comment="dataset={dataset_dir}"
#SBATCH --partition={partition}
#SBATCH --gres={gres}
#SBATCH --output=/fsx/physai/logs/%j.out
set -eo pipefail
export RUN_CONFIG={remote_config}
export PYTHONUNBUFFERED=1

srun --container-image=/fsx/enroot/{container}.sqsh \\
  --container-mounts=/fsx:/fsx \\
  bash /app/train.sh \\
    {dataset_dir} \\
    {remote_model_config} \\
    {output_dir} \\
    {max_steps}
"""


def run_train(
    session: Session,
    config_path: str,
    dataset: str,
    model_config_roots: list[str],
    max_steps: int = 10000,
) -> None:
    """Submit a train job."""
    run_cfg = _load_run_config(config_path)
    res = _get_resources(run_cfg, "train")

    # Resolve model config locally
    model_config_dir = run_cfg["model"]["config_dir"]
    local_model_config = _resolve_model_config(model_config_dir, model_config_roots)

    # Run ID and remote paths
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_id = f"train-{ts}"
    sync_dir = f"/fsx/physai/sync/{run_id}"
    remote_config = f"{sync_dir}/run_config.yaml"
    remote_model_config = f"{sync_dir}/model_config"
    dataset_dir = f"/fsx/datasets/{dataset}"
    output_dir = f"/fsx/checkpoints/{run_id}"

    sbatch_content = _generate_train_sbatch(
        run_cfg,
        res,
        run_id,
        remote_config,
        remote_model_config,
        dataset_dir,
        output_dir,
        max_steps,
    )

    # Sync to cluster
    session.run(f"mkdir -p {sync_dir} {output_dir}")
    session.rsync(f"{Path(config_path).resolve()}", f"{remote_config}")
    session.rsync(f"{local_model_config}/", f"{remote_model_config}/")
    session.write_file(f"{sync_dir}/train.sbatch", sbatch_content)

    # Verify dataset exists
    try:
        session.run(f"test -d {dataset_dir}")
    except RuntimeError:
        raise SystemExit(f"Dataset not found on cluster: {dataset_dir}")

    # Submit
    job_id = session.run(f"sbatch --parsable {sync_dir}/train.sbatch")
    print(f"Submitted train job {job_id}")
    print(f"  Config:     {config_path}")
    print(f"  Dataset:    {dataset}")
    print(f"  Output:     {output_dir}")
    print(f"  Max steps:  {max_steps}")
    print(f"  Reconnect:  physai logs {job_id}")
    print(flush=True)

    session.stream_log(job_id)
