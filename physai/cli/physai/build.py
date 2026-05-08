"""Container build: read project/container yaml, generate sbatch, submit."""

import re
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .ssh import Session

BUILD_SCRIPTS_DIR = Path(__file__).parent / "build-scripts"


def _container_sqsh_exists(session: Session, container_name: str) -> bool:
    """Whether /fsx/enroot/<name>.sqsh exists on the cluster."""
    try:
        session.run(f"test -f /fsx/enroot/{container_name}.sqsh")
    except RuntimeError:
        return False
    return True


def _find_active_build_job(session: Session, container_name: str) -> str | None:
    """Return the job ID of an active build for the container, or None.

    ``squeue`` only lists non-terminal jobs, so no state filter is needed.
    Picks the highest job ID if multiple exist.
    """
    out = session.run(f'squeue -h -o "%i" -n "physai/build/{container_name}"')
    ids = [line.strip() for line in out.splitlines() if line.strip()]
    return max(ids, key=int) if ids else None


def _find_project_yaml(container_dir: Path) -> Path | None:
    """Walk up from container_dir to find project.yaml."""
    d = container_dir.resolve().parent
    while d != d.parent:
        p = d / "project.yaml"
        if p.exists():
            return p
        d = d.parent
    return None


def _merge_configs(project: dict, container: dict) -> dict:
    """Merge project and container configs. Container overrides project. Dicts are deep-merged.

    ``base_image`` and ``base_container`` are mutually exclusive. If the container
    specifies either, it wholly overrides the project's choice of base.
    """
    merged = dict(project)
    if "base_image" in container or "base_container" in container:
        merged.pop("base_image", None)
        merged.pop("base_container", None)
    for k, v in container.items():
        if k in merged and isinstance(merged[k], dict) and isinstance(v, dict):
            merged[k] = {**merged[k], **v}
        else:
            merged[k] = v
    return merged


REQUIRED_FIELDS = ["name"]


def _validate_config(cfg: dict) -> None:
    if not cfg.get("name"):
        raise SystemExit("container.yaml: missing required field 'name'")
    has_image = bool(cfg.get("base_image"))
    has_container = bool(cfg.get("base_container"))
    if has_image and has_container:
        raise SystemExit(
            "container.yaml: set either base_image or base_container, not both"
        )
    if not (has_image or has_container):
        raise SystemExit("container.yaml: must set base_image or base_container")


def _discover_hooks(hooks_dir: Path) -> list[dict]:
    """Find setup hooks sorted by numeric prefix."""
    hooks = []
    for f in sorted(hooks_dir.glob("*.sh")):
        m = re.match(r"^(\d+)-", f.name)
        if not m:
            continue
        hooks.append(
            {
                "name": f.name,
                "root": f.name.endswith(".root.sh"),
            }
        )
    return hooks


def _generate_env_txt(env: dict) -> str:
    """Generate env.txt content (KEY=VALUE per line)."""
    return "\n".join(f"{k}={v}" for k, v in env.items()) + "\n" if env else ""


def _resolve_base(cfg: dict) -> str:
    """Return the --container-image argument for the base."""
    if cfg.get("base_container"):
        return f"/fsx/enroot/{cfg['base_container']}.sqsh"
    return cfg["base_image"]


def _generate_sbatch(
    cfg: dict, build_dir: str, build_name: str, rebuild: bool = False
) -> str:
    """Generate the build.sbatch script content."""
    name = cfg["name"]
    base_image = _resolve_base(cfg)
    partition = cfg.get("partition", "gpu")
    gres = cfg.get("gres", "gpu:1")
    hooks_dir = f"{build_dir}/setup-hooks"
    hooks = _discover_hooks(Path(cfg["_local_hooks_dir"]))
    prelude = f"{build_dir}/build-scripts"

    lines = [
        "#!/bin/bash",
        f"#SBATCH --job-name=physai/build/{name}",
        f'#SBATCH --comment="base={base_image}"',
        f"#SBATCH --partition={partition}",
        f"#SBATCH --gres={gres}",
        "#SBATCH --output=/fsx/physai/logs/%j.out",
        "set -eo pipefail",
        'trap \'echo "\\nBuild failed. Container may be left on the worker node."; '
        'echo "  Clean up: physai clean --enroot"\' ERR',
        "SECONDS=0",
        f"BUILD_DIR={build_dir}",
        f"BUILD_NAME={build_name}",
        f"SQSH=/fsx/enroot/{name}.sqsh",
        "",
    ]

    if rebuild:
        lines.extend(
            [
                'if [ -f "$SQSH" ]; then',
                '  echo "--rebuild: removing $SQSH"',
                '  rm -f "$SQSH"',
                "fi",
                "",
            ]
        )
    else:
        lines.extend(
            [
                'if [ -f "$SQSH" ]; then',
                '  echo "ERROR: $SQSH exists. Use --rebuild to replace."',
                "  exit 1",
                "fi",
                "",
            ]
        )

    lines.extend(
        [
            # Init: create container + write env vars
            'echo "=== init (${SECONDS}s) ==="',
            f"srun --container-image={base_image} --container-name=$BUILD_NAME"
            f" --container-mounts=/fsx:/fsx --container-remap-root"
            f' bash "{prelude}/init-env.root.sh"',
            "",
        ]
    )

    # Setup hooks
    for hook in hooks:
        root_flag = " --container-remap-root" if hook["root"] else ""
        lines.append(f'echo "=== {hook["name"]} (${{SECONDS}}s) ==="')
        lines.append(
            f"srun --container-name=$BUILD_NAME"
            f" --container-mounts=/fsx:/fsx{root_flag}"
            f' bash "{hooks_dir}/{hook["name"]}"'
        )
        lines.append("")

    lines.extend(
        [
            # Copy app/
            'echo "=== copy app/ (${SECONDS}s) ==="',
            f"srun --container-name=$BUILD_NAME --container-mounts=/fsx:/fsx"
            f" --container-remap-root"
            f' bash "{prelude}/mkdir-app.root.sh"',
            f"srun --container-name=$BUILD_NAME --container-mounts=/fsx:/fsx"
            f' bash "{prelude}/copy-app.sh"',
            "",
            # Export squashfs
            'echo "=== export squashfs (${SECONDS}s) ==="',
            'enroot export -o "$SQSH" pyxis_${BUILD_NAME}',
            "enroot remove -f pyxis_${BUILD_NAME}",
            "",
            'echo "Build complete: $SQSH (${SECONDS}s)"',
        ]
    )

    return "\n".join(lines) + "\n"


def run_build(
    session: Session, container_dir: str, rebuild: bool = False, stream: bool = True
) -> None:
    """Execute a container build."""
    container_path = Path(container_dir).resolve()
    container_yaml = container_path / "container.yaml"
    if not container_yaml.exists():
        raise SystemExit(f"No container.yaml in {container_dir}")

    hooks_dir = container_path / "setup-hooks"
    if not hooks_dir.is_dir() or not list(hooks_dir.glob("*.sh")):
        raise SystemExit(f"No setup hooks in {hooks_dir}")

    app_dir = container_path / "app"
    if not app_dir.is_dir():
        raise SystemExit(f"No app/ directory in {container_dir}")

    with open(container_yaml) as f:
        container_cfg = yaml.safe_load(f)

    project_yaml = _find_project_yaml(container_path)
    project_cfg = {}
    if project_yaml:
        with open(project_yaml) as f:
            project_cfg = yaml.safe_load(f) or {}

    cfg = _merge_configs(project_cfg, container_cfg)
    _validate_config(cfg)
    name = cfg["name"]
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    build_name = f"{name}-{ts}"
    build_dir = f"/fsx/physai/builds/{build_name}"

    cfg["_local_hooks_dir"] = str(hooks_dir)

    # Preflight: without --rebuild, fail early if another build is active
    # for this container (our sbatch would hit "sqsh exists" at run time) or
    # the sqsh already exists on disk.
    if not rebuild:
        active = _find_active_build_job(session, name)
        if active:
            raise SystemExit(
                f"Build job {active} is already active for '{name}'. "
                "Re-run with --rebuild to replace its output, "
                f"or cancel it first: physai cancel {active}"
            )
        if _container_sqsh_exists(session, name):
            raise SystemExit(
                f"/fsx/enroot/{name}.sqsh already exists. Use --rebuild to replace."
            )

    # Preflight: if base_container, verify it's either on disk OR has an
    # in-flight build job we can depend on.
    dep_job_id: str | None = None
    if cfg.get("base_container"):
        base_name = cfg["base_container"]
        dep_job_id = _find_active_build_job(session, base_name)
        if dep_job_id:
            print(f"Depends on build job {dep_job_id} ({base_name})")
        elif not _container_sqsh_exists(session, base_name):
            raise SystemExit(
                f"Base container '{base_name}' not found at /fsx/enroot/{base_name}.sqsh. "
                "Build it first."
            )

    sbatch_content = _generate_sbatch(cfg, build_dir, build_name, rebuild=rebuild)
    env_txt = _generate_env_txt(cfg.get("env", {}))

    # Ensure remote dirs
    session.run("mkdir -p /fsx/physai/logs /fsx/physai/builds")

    # Sync files
    print(f"Syncing {container_path.name} to {session.host}:{build_dir}/")
    session.run(f"mkdir -p {build_dir}/build-scripts")
    session.rsync(f"{hooks_dir}/", f"{build_dir}/setup-hooks/")
    session.rsync(f"{app_dir}/", f"{build_dir}/app/")
    session.rsync(f"{BUILD_SCRIPTS_DIR}/", f"{build_dir}/build-scripts/")

    if env_txt:
        session.write_file(f"{build_dir}/build-scripts/env.txt", env_txt)
    session.write_file(f"{build_dir}/build.sbatch", sbatch_content)

    # Submit
    dep = (
        f" --dependency=afterok:{dep_job_id} --kill-on-invalid-dep=yes"
        if dep_job_id
        else ""
    )
    job_id = session.run(f"sbatch --parsable{dep} {build_dir}/build.sbatch")
    print(f"Submitted build job {job_id} for {name}")
    if not stream:
        return
    print(f"Reconnect: physai logs {job_id}", flush=True)

    session.stream_log(job_id)
