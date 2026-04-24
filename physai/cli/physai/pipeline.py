"""Pipeline commands: run, train, eval.

Shared logic for loading run configs, resolving model configs, and generating sbatch scripts.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import yaml

from .build import _container_sqsh_exists, _find_active_build_job
from .ssh import Session

# Ordered list of all pipeline stages
ALL_STAGES = ["augment", "convert", "validate", "train", "eval", "register"]


# ── Run context ──
# A dict passed through stages. Each stage reads its inputs and writes its outputs.
# Keys: raw_dir, dataset_dir, checkpoint_dir, eval_dir, visual


# ── Stage abstraction ──


class Stage:
    """Base class for pipeline stages."""

    name: str

    def __init__(
        self,
        cfg: dict,
        run_id: str,
        remote_config: str,
        remote_model_config: str,
    ):
        self.cfg = cfg
        self.run_id = run_id
        self.remote_config = remote_config
        self.remote_model_config = remote_model_config

    def validate(self, ctx: dict) -> None:
        """Check that required inputs are present in ctx. Raise SystemExit if not."""

    def verify_inputs(self, session: Session, ctx: dict) -> None:
        """Verify that required inputs exist on the cluster."""

    def prepare(self, session: Session, ctx: dict) -> None:
        """Create output directories on the cluster."""

    def generate_sbatch(self, ctx: dict) -> str:
        """Generate sbatch content and update ctx with output paths."""
        raise NotImplementedError


def _sbatch_header(cfg: dict, job_name: str, comment: str) -> str:
    partition = cfg.get("partition", "gpu")
    lines = [
        "#!/bin/bash",
        f"#SBATCH --job-name={job_name}",
        f'#SBATCH --comment="{comment}"',
        f"#SBATCH --partition={partition}",
    ]
    if "gres" in cfg:
        lines.append(f"#SBATCH --gres={cfg['gres']}")
    if "constraint" in cfg:
        lines.append(f"#SBATCH --constraint={cfg['constraint']}")
    lines.append("#SBATCH --output=/fsx/physai/logs/%j.out")
    lines.append("set -eo pipefail")
    return "\n".join(lines)


class TrainStage(Stage):
    name = "train"

    def validate(self, ctx: dict) -> None:
        if not ctx.get("dataset_dir"):
            raise SystemExit("--dataset required when starting from 'train'")

    def verify_inputs(self, session: Session, ctx: dict) -> None:
        _verify_exists(session, ctx["dataset_dir"], "Dataset")

    def prepare(self, session: Session, ctx: dict) -> None:
        ctx["checkpoint_dir"] = f"/fsx/checkpoints/{self.run_id}"
        session.run(f"mkdir -p {ctx['checkpoint_dir']}")

    def generate_sbatch(self, ctx: dict) -> str:
        container = self.cfg["container"]
        max_steps = ctx.get("max_steps") or self.cfg.get("max_steps", 10000)
        header = _sbatch_header(
            self.cfg,
            f"physai/run/{self.run_id}/train",
            f"dataset={ctx['dataset_dir']}",
        )
        return f"""\
{header}
export RUN_CONFIG={self.remote_config}

srun --container-image=/fsx/enroot/{container}.sqsh \\
  --container-mounts=/fsx:/fsx \\
  bash /app/train.sh \\
    {ctx["dataset_dir"]} \\
    {self.remote_model_config} \\
    {ctx["checkpoint_dir"]} \\
    {max_steps}
"""


class EvalStage(Stage):
    name = "eval"

    def validate(self, ctx: dict) -> None:
        if not ctx.get("checkpoint_dir"):
            raise SystemExit("--checkpoint required when starting from 'eval'")

    def verify_inputs(self, session: Session, ctx: dict) -> None:
        _verify_exists(session, ctx["checkpoint_dir"], "Checkpoint")

    def prepare(self, session: Session, ctx: dict) -> None:
        ctx["eval_dir"] = f"/fsx/evaluations/{self.run_id}"
        session.run(f"mkdir -p {ctx['eval_dir']}")

    def generate_sbatch(self, ctx: dict) -> str:
        container = self.cfg["container"]
        rounds = ctx.get("eval_rounds") or self.cfg.get("rounds", 20)
        visual_flag = " --visual" if ctx.get("visual") else ""
        header = _sbatch_header(
            self.cfg,
            f"physai/run/{self.run_id}/eval",
            f"checkpoint={ctx['checkpoint_dir']}",
        )
        return f"""\
{header}
export RUN_CONFIG={self.remote_config}
export DISPLAY=${{DISPLAY:-:0}}

srun --container-image=/fsx/enroot/{container}.sqsh \\
  --container-mounts=/fsx:/fsx,/tmp/.X11-unix:/tmp/.X11-unix \\
  bash /app/eval.sh \\
    {ctx["checkpoint_dir"]} \\
    {self.remote_model_config} \\
    {ctx["eval_dir"]} \\
    {rounds}{visual_flag}
"""


STAGE_REGISTRY: dict[str, type[Stage]] = {
    "train": TrainStage,
    "eval": EvalStage,
}


# ── Config loading ──


def _load_run_config(config_path: Path) -> dict:
    if not config_path.exists():
        raise SystemExit(f"Config not found: {config_path}")
    with open(config_path) as f:
        cfg = yaml.safe_load(f)
    if "model" not in cfg:
        raise SystemExit(f"Missing 'model' in {config_path}")
    if "config_dir" not in cfg.get("model", {}):
        raise SystemExit(f"Missing 'model.config_dir' in {config_path}")
    if "stages" not in cfg:
        raise SystemExit(f"Missing 'stages' in {config_path}")
    return cfg


def _resolve_stages(
    run_cfg: dict, from_stage: str | None, to_stage: str | None
) -> list[str]:
    stages = run_cfg.get("pipeline", {}).get("stages")
    if not stages:
        raise SystemExit("Missing 'pipeline.stages' in config")
    stages = list(stages)

    for s in stages:
        if s not in ALL_STAGES:
            raise SystemExit(
                f"Unknown stage '{s}' in pipeline.stages. Valid: {', '.join(ALL_STAGES)}"
            )

    if from_stage and from_stage not in stages:
        raise SystemExit(f"--from '{from_stage}' not in pipeline.stages: {stages}")
    if to_stage and to_stage not in stages:
        raise SystemExit(f"--to '{to_stage}' not in pipeline.stages: {stages}")

    if from_stage or to_stage:
        start = stages.index(from_stage) if from_stage else 0
        end = stages.index(to_stage) + 1 if to_stage else len(stages)
        if start >= end:
            raise SystemExit(f"--from {from_stage} must come before --to {to_stage}")
        return stages[start:end]

    return stages


def _get_stage_config(run_cfg: dict, stage: str) -> dict:
    cfg = run_cfg.get("stages", {}).get(stage)
    if not cfg:
        raise SystemExit(f"No stages.{stage} in config")
    if "container" not in cfg:
        raise SystemExit(f"No container in stages.{stage}")
    return cfg


def _resolve_model_config(config_dir: str, model_config_roots: list[Path]) -> Path:
    for root in model_config_roots:
        candidate = root.expanduser() / config_dir
        if candidate.is_dir():
            return candidate.resolve()
    searched = (
        "\n  ".join(str(r) for r in model_config_roots)
        if model_config_roots
        else "(none)"
    )
    raise SystemExit(
        f"Model config dir '{config_dir}' not found.\n"
        f"Searched:\n  {searched}\n"
        f"Set model_config_roots in ~/.physai/config.yaml or pass --model-config-root."
    )


# ── Pipeline runner ──


def run_pipeline(
    session: Session,
    config_path: Path,
    model_config_roots: list[Path],
    from_stage: str | None = None,
    to_stage: str | None = None,
    raw: str | None = None,
    dataset: str | None = None,
    checkpoint: str | None = None,
    max_steps: int | None = None,
    eval_rounds: int | None = None,
    visual: bool = False,
    stream: bool = True,
) -> None:
    """Submit a pipeline run (one or more stages)."""
    run_cfg = _load_run_config(config_path)
    stage_names = _resolve_stages(run_cfg, from_stage, to_stage)
    if not stage_names:
        raise SystemExit("No stages to run")

    # Build run context
    ctx: dict = {
        "raw_dir": f"/fsx/raw/{raw}" if raw else None,
        "dataset_dir": f"/fsx/datasets/{dataset}" if dataset else None,
        "checkpoint_dir": f"/fsx/checkpoints/{checkpoint}" if checkpoint else None,
        "max_steps": max_steps,
        "eval_rounds": eval_rounds,
        "visual": visual,
    }

    # Run ID and remote paths
    ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
    run_id = f"run-{ts}"
    sync_dir = f"/fsx/physai/sync/{run_id}"
    remote_config = f"{sync_dir}/run_config.yaml"
    remote_model_config = f"{sync_dir}/model_config"

    # Instantiate stages
    stages: list[Stage] = []
    for name in stage_names:
        cls = STAGE_REGISTRY.get(name)
        if not cls:
            raise SystemExit(f"Stage '{name}' is not yet implemented")
        stage_cfg = _get_stage_config(run_cfg, name)
        stages.append(cls(stage_cfg, run_id, remote_config, remote_model_config))

    # Validate and verify first stage inputs
    stages[0].validate(ctx)

    # Resolve and sync model config
    model_config_dir = run_cfg["model"]["config_dir"]
    local_model_config = _resolve_model_config(model_config_dir, model_config_roots)
    session.run(f"mkdir -p {sync_dir}")
    session.rsync(str(config_path.resolve()), remote_config)
    session.rsync(f"{local_model_config}/", f"{remote_model_config}/")

    stages[0].verify_inputs(session, ctx)

    # Submit stages
    prev_job_id = None
    job_ids: list[str] = []

    for stage in stages:
        stage.prepare(session, ctx)
        content = stage.generate_sbatch(ctx)
        sbatch_path = f"{sync_dir}/{stage.name}.sbatch"
        session.write_file(sbatch_path, content)

        # Collect dependencies: previous stage + in-flight build for this container.
        deps: list[str] = []
        if prev_job_id:
            deps.append(prev_job_id)
        container_name = stage.cfg["container"]
        build_job = _find_active_build_job(session, container_name)
        if build_job:
            deps.append(build_job)
            print(
                f"  {stage.name}: depends on build job {build_job} ({container_name})"
            )
        elif not _container_sqsh_exists(session, container_name):
            raise SystemExit(
                f"Container '{container_name}' not found at /fsx/enroot/{container_name}.sqsh. "
                "Build it first."
            )

        dep = (
            f" --dependency=afterok:{':'.join(deps)} --kill-on-invalid-dep=yes"
            if deps
            else ""
        )
        job_id = session.run(f"sbatch --parsable{dep} {sbatch_path}")
        print(f"  {stage.name}: job {job_id}")
        prev_job_id = job_id
        job_ids.append(job_id)

    print(f"\nSubmitted {len(stages)} stage(s): {' → '.join(stage_names)}")
    print(f"  Run ID:     {run_id}")
    if not stream:
        return
    print(f"  Reconnect:  physai logs {job_ids[0]}")
    print(flush=True)

    for job_id in job_ids:
        session.stream_log(job_id)


def _verify_exists(session: Session, path: str, label: str) -> None:
    try:
        session.run(f"test -e {path}")
    except RuntimeError:
        raise SystemExit(f"{label} not found on cluster: {path}")


# ── Convenience shortcuts ──


def run_train(
    session: Session,
    config_path: Path,
    dataset: str,
    model_config_roots: list[Path],
    max_steps: int | None = None,
    stream: bool = True,
) -> None:
    """Shortcut: physai train ≡ physai run --from train --to train."""
    run_pipeline(
        session,
        config_path,
        model_config_roots,
        from_stage="train",
        to_stage="train",
        dataset=dataset,
        max_steps=max_steps,
        stream=stream,
    )


def run_eval(
    session: Session,
    config_path: Path,
    checkpoint: str,
    model_config_roots: list[Path],
    eval_rounds: int | None = None,
    visual: bool = False,
    stream: bool = True,
) -> None:
    """Shortcut: physai eval ≡ physai run --from eval --to eval."""
    run_pipeline(
        session,
        config_path,
        model_config_roots,
        from_stage="eval",
        to_stage="eval",
        checkpoint=checkpoint,
        eval_rounds=eval_rounds,
        visual=visual,
        stream=stream,
    )
