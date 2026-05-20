"""Pipeline commands: run, train, eval.

Shared logic for loading run configs, resolving model configs, and generating sbatch scripts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import yaml

from .build import _container_sqsh_exists, _find_active_build_job
from .schema import validate
from .ssh import Session

# Ordered list of all pipeline stages
ALL_STAGES = ["augment", "convert", "validate", "train", "eval", "register"]


# ── Run context ──
# A dict passed through stages. Each stage reads its inputs and writes its outputs.
# Keys: raw_dir, dataset_dir, checkpoint_dir, eval_dir, visual


# ── Stage abstraction ──


@dataclass(frozen=True)
class Artifact:
    """A cluster path produced or consumed by a stage.

    Sum type: `File(path)` or `Dir(path)`. `str()` is the raw path (safe for
    shell interpolation); `as_token()` / `from_token()` are the kind-aware
    serialization pair used for the sbatch --comment (directories have a
    trailing `/`).
    """

    path: PurePosixPath

    def __str__(self) -> str:
        return str(self.path)

    def as_token(self) -> str:
        raise NotImplementedError

    @staticmethod
    def from_token(token: str) -> Artifact:
        if token.endswith("/"):
            return Dir(PurePosixPath(token.rstrip("/")))
        return File(PurePosixPath(token))


@dataclass(frozen=True)
class File(Artifact):
    def as_token(self) -> str:
        return str(self.path)


@dataclass(frozen=True)
class Dir(Artifact):
    def as_token(self) -> str:
        return f"{self.path}/"


@dataclass
class JobMetadata:
    """Slurm job metadata for a pipeline stage."""

    name: str  # Slurm --job-name, e.g. "physai/run/<run_id>/<stage>"
    outputs: list[Artifact] = field(default_factory=list)
    partition: str = "gpu"
    gres: str | None = None
    constraint: str | None = None


class Stage:
    """Base class for pipeline stages.

    A stage declares:
      - `inputs(ctx)` / `outputs(ctx)`: pure functions returning cluster paths
      - `metadata(ctx)`: Slurm job metadata (name, resources, outputs)
      - `prepare(ctx)`: update ctx for downstream stages
      - `sbatch_body(ctx)`: the per-stage body (srun + exports) of the sbatch script

    The runner uses inputs/outputs to verify reachability & chain dependencies,
    and calls `generate_sbatch()` (final) which composes header + body.
    """

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
        """Check required ctx keys are present. Called on every stage."""

    def inputs(self, ctx: dict) -> list[Artifact]:
        """Cluster paths this stage reads. Pure; no side effects."""
        return []

    def outputs(self, ctx: dict) -> list[Artifact]:
        """Cluster paths this stage produces. Pure; no side effects."""
        return []

    def metadata(self, ctx: dict) -> JobMetadata:
        """Slurm job metadata. Subclasses override to set resources."""
        return JobMetadata(
            name=f"physai/run/{self.run_id}/{self.name}",
            outputs=self.outputs(ctx),
            partition=self.cfg.get("partition", "gpu"),
            gres=self.cfg.get("gres"),
            constraint=self.cfg.get("constraint"),
        )

    def prepare(self, ctx: dict) -> None:
        """Update ctx for downstream stages."""

    def sbatch_body(self, ctx: dict) -> str:
        """Return the body of the sbatch script (exports + srun). Override this."""
        raise NotImplementedError

    def generate_sbatch(self, ctx: dict) -> str:
        """Final — do not override. Composes header from metadata + stage body."""
        return _sbatch_header(self.metadata(ctx)) + "\n" + self.sbatch_body(ctx)


def _format_produces(output: Artifact) -> str:
    """Serialize an Artifact into a `produces=<path>` comment token.

    Kind (file vs dir) is encoded by `as_token()` — directories render with a
    trailing `/`. Whitespace in paths would break space-separated tokenization,
    so we reject it.
    """
    s = output.as_token()
    if any(c.isspace() for c in s):
        raise SystemExit(
            f"Pipeline output path contains whitespace: {s!r}. "
            "Whitespace is not supported in cluster paths."
        )
    return f"produces={s}"


def _sbatch_header(meta: JobMetadata) -> str:
    # Comment encodes every output the job will produce (space-separated, one
    # `produces=<path>` token per output). `_find_active_job_producing` looks
    # for an exact token match, so multi-output jobs work automatically.
    comment = " ".join(_format_produces(p) for p in meta.outputs)
    lines = [
        "#!/bin/bash",
        f"#SBATCH --job-name={meta.name}",
        f'#SBATCH --comment="{comment}"',
        f"#SBATCH --partition={meta.partition}",
    ]
    if meta.gres:
        lines.append(f"#SBATCH --gres={meta.gres}")
    if meta.constraint:
        lines.append(f"#SBATCH --constraint={meta.constraint}")
    lines.append("#SBATCH --output=/fsx/physai/logs/%j.out")
    lines.append("set -eo pipefail")
    return "\n".join(lines)


def _find_active_job_producing(session: Session, artifact: Artifact) -> str | None:
    """Find an active physai/run job that will produce `artifact`.

    Each stage's sbatch --comment stores one or more `produces=<path>` tokens
    (space-separated, emitted by `_format_produces`). We match exact-token
    equality client-side (pipeline job names include run_id so `squeue -n`
    isn't an option).
    """
    out = session.run('squeue -u $(whoami) -h -o "%i|%j|%k"')
    target = _format_produces(artifact)
    candidates: list[int] = []
    for line in out.splitlines():
        parts = line.split("|", 2)
        if len(parts) < 3:
            continue
        job_id, name, comment = parts
        if name.startswith("physai/run/") and target in comment.split():
            candidates.append(int(job_id))
    return str(max(candidates)) if candidates else None


def _artifact_exists(session: Session, artifact: Artifact) -> bool:
    """Check that the artifact exists AND matches its kind (file vs directory)."""
    flag = "-d" if isinstance(artifact, Dir) else "-f"
    try:
        session.run(f"test {flag} {artifact}")
        return True
    except RuntimeError:
        return False


def _validate_artifact_name(name: str, role: str) -> None:
    """Reject names that would escape paths or break comment tokenization.

    `role` is a user-facing label (e.g. 'dataset', 'raw data', 'checkpoint')
    used in the error message.
    """
    if "/" in name or name in ("", ".", ".."):
        raise SystemExit(
            f"Invalid {role} name {name!r}: must not contain '/' or be '.', '..'"
        )
    if any(c.isspace() for c in name):
        raise SystemExit(
            f"Invalid {role} name {name!r}: whitespace is not supported in cluster paths"
        )


class ConvertStage(Stage):
    name = "convert"

    def validate(self, ctx: dict) -> None:
        if not ctx.get("raw_dir"):
            raise SystemExit("--raw required when starting from 'convert'")

    def _dataset_dir(self, ctx: dict) -> Dir:
        # If the user passed --dataset, use that name as the output; otherwise
        # mirror the raw directory name (format-agnostic default).
        if ctx.get("dataset_dir"):
            return ctx["dataset_dir"]
        return Dir(PurePosixPath("/fsx/datasets") / ctx["raw_dir"].path.name)

    def inputs(self, ctx: dict) -> list[Artifact]:
        return [ctx["raw_dir"]]

    def outputs(self, ctx: dict) -> list[Artifact]:
        return [self._dataset_dir(ctx)]

    def prepare(self, ctx: dict) -> None:
        # Only populate if absent: if the user passed --dataset, it's already set.
        if not ctx.get("dataset_dir"):
            ctx["dataset_dir"] = self._dataset_dir(ctx)

    def sbatch_body(self, ctx: dict) -> str:
        container = self.cfg["container"]
        return f"""\
export RUN_CONFIG={self.remote_config}

srun --container-image=/fsx/enroot/{container}.sqsh \\
  --container-mounts=/fsx:/fsx \\
  bash /app/convert.sh \\
    {ctx["raw_dir"]} \\
    {ctx["dataset_dir"]}
"""


class TrainStage(Stage):
    name = "train"

    def validate(self, ctx: dict) -> None:
        if not ctx.get("dataset_dir"):
            raise SystemExit("--dataset required when starting from 'train'")

    def _checkpoint_dir(self) -> Dir:
        return Dir(PurePosixPath("/fsx/checkpoints") / self.run_id)

    def inputs(self, ctx: dict) -> list[Artifact]:
        return [ctx["dataset_dir"]]

    def outputs(self, ctx: dict) -> list[Artifact]:
        return [self._checkpoint_dir()]

    def prepare(self, ctx: dict) -> None:
        ctx["checkpoint_dir"] = self._checkpoint_dir()

    def sbatch_body(self, ctx: dict) -> str:
        container = self.cfg["container"]
        max_steps = ctx.get("max_steps") or self.cfg.get("max_steps", 10000)
        return f"""\
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

    def _eval_dir(self) -> Dir:
        return Dir(PurePosixPath("/fsx/evaluations") / self.run_id)

    def inputs(self, ctx: dict) -> list[Artifact]:
        return [ctx["checkpoint_dir"]]

    def outputs(self, ctx: dict) -> list[Artifact]:
        return [self._eval_dir()]

    def prepare(self, ctx: dict) -> None:
        ctx["eval_dir"] = self._eval_dir()

    def sbatch_body(self, ctx: dict) -> str:
        container = self.cfg["container"]
        rounds = ctx.get("eval_rounds") or self.cfg.get("rounds", 20)
        visual_flag = " --visual" if ctx.get("visual") else ""
        return f"""\
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
    "convert": ConvertStage,
    "train": TrainStage,
    "eval": EvalStage,
}


# ── Config loading ──


def _load_run_config(config_path: Path) -> dict:
    if not config_path.exists():
        raise SystemExit(f"Config not found: {config_path}")
    with open(config_path) as f:
        cfg = yaml.safe_load(f) or {}
    validate(cfg, "run-config", str(config_path))
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

    # Validate name inputs — reject slashes, whitespace, and path escapes so
    # they don't surface as confusing downstream errors (or, worse, silently
    # point at unexpected paths).
    if raw is not None:
        _validate_artifact_name(raw, "raw data")
    if dataset is not None:
        _validate_artifact_name(dataset, "dataset")
    if checkpoint is not None:
        _validate_artifact_name(checkpoint, "checkpoint")

    # Build run context
    ctx: dict = {
        "raw_dir": Dir(PurePosixPath("/fsx/raw") / raw) if raw else None,
        "dataset_dir": Dir(PurePosixPath("/fsx/datasets") / dataset)
        if dataset
        else None,
        "checkpoint_dir": Dir(PurePosixPath("/fsx/checkpoints") / checkpoint)
        if checkpoint
        else None,
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

    # Resolve and sync model config
    model_config_dir = run_cfg["model"]["config_dir"]
    local_model_config = _resolve_model_config(model_config_dir, model_config_roots)
    session.run(f"mkdir -p {sync_dir}")
    session.rsync(str(config_path.resolve()), remote_config)
    session.rsync(f"{local_model_config}/", f"{remote_model_config}/")

    # Pre-flight: validate all stages, verify inputs/outputs, run prepare, and
    # render each stage's sbatch content BEFORE submitting anything. Rendering
    # in preflight matters because a stage's prepare() may mutate ctx keys —
    # doing it later would make generate_sbatch see the final ctx, not the one
    # that stage saw during preflight.
    planned_outputs: set[Artifact] = set()
    stage_plans: list[
        tuple[Stage, list[str], str]
    ] = []  # (stage, input_deps, sbatch_content)

    for stage in stages:
        # Check required ctx keys are present. For the first stage they come
        # from CLI args; for later stages they must have been populated by an
        # upstream prepare().
        stage.validate(ctx)

        # Verify inputs: must exist on disk, be produced by an upstream stage
        # in this run, or be produced by an active job (chain via afterok).
        input_dep_jobs: list[str] = []
        for artifact in stage.inputs(ctx):
            if artifact in planned_outputs or _artifact_exists(session, artifact):
                continue
            active = _find_active_job_producing(session, artifact)
            if active:
                input_dep_jobs.append(active)
                print(f"  {stage.name}: waits on job {active} to produce {artifact}")
                continue
            raise SystemExit(
                f"{stage.name}: input {artifact} does not exist and no job is producing it"
            )

        # Verify outputs: must NOT exist on disk AND no active job is writing it.
        for artifact in stage.outputs(ctx):
            if artifact in planned_outputs:
                raise SystemExit(
                    f"{stage.name}: output {artifact} is already claimed by an earlier stage in this run"
                )
            if _artifact_exists(session, artifact):
                raise SystemExit(
                    f"{stage.name}: output {artifact} already exists on cluster. Remove it or use a different name."
                )
            active = _find_active_job_producing(session, artifact)
            if active:
                raise SystemExit(
                    f"{stage.name}: active job {active} is already producing {artifact}. Wait or cancel it."
                )
            planned_outputs.add(artifact)

        # Confirm the container image is available (or an active build will
        # produce it). Do this in preflight so a missing container on stage N
        # doesn't strand already-submitted earlier stages.
        container_name = stage.cfg["container"]
        if not _find_active_build_job(
            session, container_name
        ) and not _container_sqsh_exists(session, container_name):
            raise SystemExit(
                f"Container '{container_name}' not found at /fsx/enroot/{container_name}.sqsh. "
                "Build it first."
            )

        stage.prepare(ctx)
        stage_plans.append((stage, input_dep_jobs, stage.generate_sbatch(ctx)))

    # Submit stages. If any submission fails, cancel everything we've submitted
    # so far in this run (Slurm would eventually invalidate dependents, but
    # scancel makes it immediate and unambiguous).
    prev_job_id: str | None = None
    job_ids: list[str] = []
    try:
        for stage, input_dep_jobs, content in stage_plans:
            sbatch_path = f"{sync_dir}/{stage.name}.sbatch"
            session.write_file(sbatch_path, content)

            deps: list[str] = list(input_dep_jobs)
            if prev_job_id:
                deps.append(prev_job_id)
            # Re-check for an active build at submit time so we chain on one
            # that started during preflight (rare, but cheap).
            build_job = _find_active_build_job(session, stage.cfg["container"])
            if build_job:
                deps.append(build_job)
                print(
                    f"  {stage.name}: depends on build job {build_job} ({stage.cfg['container']})"
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
    except Exception:
        if job_ids:
            print(
                f"\nSubmission failed. Cancelling {len(job_ids)} already-submitted job(s): {' '.join(job_ids)}"
            )
            try:
                session.run(f"scancel {' '.join(job_ids)}")
            except RuntimeError as e:
                print(f"  scancel failed: {e}")
        raise

    print(f"\nSubmitted {len(stages)} stage(s): {' → '.join(stage_names)}")
    print(f"  Run ID:     {run_id}")
    if not stream:
        return
    print(f"  Reconnect:  physai logs {job_ids[0]}")
    print(flush=True)

    for job_id in job_ids:
        session.stream_log(job_id)


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


def run_convert(
    session: Session,
    config_path: Path,
    raw: str,
    model_config_roots: list[Path],
    dataset: str | None = None,
    stream: bool = True,
) -> None:
    """Shortcut: physai convert ≡ physai run --from convert --to convert.

    When `dataset` is given, the convert stage writes to /fsx/datasets/<dataset>
    instead of mirroring the raw name.
    """
    run_pipeline(
        session,
        config_path,
        model_config_roots,
        from_stage="convert",
        to_stage="convert",
        raw=raw,
        dataset=dataset,
        stream=stream,
    )
