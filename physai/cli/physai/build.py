"""Container build: read project/container yaml, generate sbatch, submit."""

import re
from datetime import datetime
from pathlib import Path

import yaml

from .ssh import Session

BUILD_SCRIPTS_DIR = Path(__file__).parent / "build-scripts"


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
    """Merge project and container configs. Container overrides project. Dicts are deep-merged."""
    merged = dict(project)
    for k, v in container.items():
        if k in merged and isinstance(merged[k], dict) and isinstance(v, dict):
            merged[k] = {**merged[k], **v}
        else:
            merged[k] = v
    return merged


REQUIRED_FIELDS = ["name", "base_image"]


def _validate_config(cfg: dict) -> None:
    missing = [f for f in REQUIRED_FIELDS if not cfg.get(f)]
    if missing:
        raise SystemExit(f"Missing required config fields: {', '.join(missing)}")


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


def _generate_sbatch(cfg: dict, build_dir: str, build_name: str) -> str:
    """Generate the build.sbatch script content."""
    name = cfg["name"]
    base_image = cfg["base_image"]
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
        'if [ -f "$SQSH" ]; then',
        '  echo "ERROR: $SQSH exists. Use --rebuild to replace."',
        "  exit 1",
        "fi",
        "",
        # Init: create container + write env vars
        'echo "=== init (${SECONDS}s) ==="',
        f"srun --container-image={base_image} --container-name=$BUILD_NAME"
        f" --container-mounts=/fsx:/fsx --container-remap-root"
        f' bash "{prelude}/init-env.root.sh"',
        "",
    ]

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


def run_build(session: Session, container_dir: str, rebuild: bool = False) -> None:
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
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    build_name = f"{name}-{ts}"
    build_dir = f"/fsx/physai/builds/{build_name}"

    cfg["_local_hooks_dir"] = str(hooks_dir)

    sbatch_content = _generate_sbatch(cfg, build_dir, build_name)
    env_txt = _generate_env_txt(cfg.get("env", {}))

    # Ensure remote dirs
    session.run("mkdir -p /fsx/physai/logs /fsx/physai/builds /fsx/enroot")

    if rebuild:
        session.run(f"rm -f /fsx/enroot/{name}.sqsh")

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
    job_id = session.run(f"sbatch --parsable {build_dir}/build.sbatch")
    print(f"Submitted build job {job_id} for {name}")
    print(f"Reconnect: physai logs {job_id}", flush=True)

    session.stream_log(job_id)
