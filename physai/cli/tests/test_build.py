"""Tests for physai build system."""

from unittest.mock import MagicMock

import pytest

from physai.build import (
    _discover_hooks,
    _find_active_build_job,
    _find_project_yaml,
    _generate_env_txt,
    _generate_sbatch,
    _merge_configs,
    _validate_config,
)

# ── _merge_configs ──


def test_merge_scalar_override():
    p = {"base_image": "a", "partition": "cpu"}
    c = {"name": "test", "partition": "gpu"}
    m = _merge_configs(p, c)
    assert m == {"base_image": "a", "partition": "gpu", "name": "test"}


def test_merge_dicts_deep():
    p = {"env": {"A": "1", "B": "2"}, "tags": {"x": 1}}
    c = {"env": {"B": "3", "C": "4"}, "tags": {"y": 2}}
    m = _merge_configs(p, c)
    assert m["env"] == {"A": "1", "B": "3", "C": "4"}
    assert m["tags"] == {"x": 1, "y": 2}


def test_merge_non_dict_replaces():
    p = {"gres": "gpu:1"}
    c = {"gres": "gpu:2"}
    assert _merge_configs(p, c)["gres"] == "gpu:2"


def test_merge_base_container_drops_project_base_image():
    """Container's base_container wholly overrides project's base_image."""
    p = {"base_image": "nvcr.io/foo", "partition": "cpu"}
    c = {"name": "t", "base_container": "parent"}
    m = _merge_configs(p, c)
    assert "base_image" not in m
    assert m["base_container"] == "parent"


def test_merge_base_image_drops_project_base_container():
    """Container's base_image wholly overrides project's base_container."""
    p = {"base_container": "parent"}
    c = {"name": "t", "base_image": "nvcr.io/foo"}
    m = _merge_configs(p, c)
    assert "base_container" not in m
    assert m["base_image"] == "nvcr.io/foo"


# ── _validate_config ──


def test_validate_passes_base_image():
    _validate_config({"name": "test", "base_image": "img"})


def test_validate_passes_base_container():
    _validate_config({"name": "test", "base_container": "parent"})


def test_validate_missing_name():
    with pytest.raises(SystemExit, match="name"):
        _validate_config({"base_image": "img"})


def test_validate_missing_base():
    with pytest.raises(SystemExit, match="base_image or base_container"):
        _validate_config({"name": "test"})


def test_validate_both_bases_rejected():
    with pytest.raises(SystemExit, match="not both"):
        _validate_config({"name": "t", "base_image": "img", "base_container": "parent"})


# ── _discover_hooks ──


def test_discover_hooks(tmp_path):
    (tmp_path / "10-sys.root.sh").write_text("#!/bin/bash\n")
    (tmp_path / "20-install.sh").write_text("#!/bin/bash\n")
    (tmp_path / "README.md").write_text("not a hook\n")
    (tmp_path / "no-prefix.sh").write_text("#!/bin/bash\n")
    hooks = _discover_hooks(tmp_path)
    assert len(hooks) == 2
    assert hooks[0] == {"name": "10-sys.root.sh", "root": True}
    assert hooks[1] == {"name": "20-install.sh", "root": False}


# ── _generate_env_txt ──


def test_generate_env_txt():
    assert _generate_env_txt({"A": "1", "B": "2"}) == "A=1\nB=2\n"


def test_generate_env_txt_empty():
    assert _generate_env_txt({}) == ""


# ── _find_project_yaml ──


def test_find_project_yaml(tmp_path):
    project = tmp_path / "project.yaml"
    project.write_text("base_image: foo\n")
    container_dir = tmp_path / "containers" / "mycontainer"
    container_dir.mkdir(parents=True)
    assert _find_project_yaml(container_dir) == project


def test_find_project_yaml_not_found(tmp_path):
    container_dir = tmp_path / "deep" / "nested"
    container_dir.mkdir(parents=True)
    assert _find_project_yaml(container_dir) is None


# ── _generate_sbatch ──


def test_generate_sbatch(tmp_path):
    (tmp_path / "10-sys.root.sh").write_text("")
    (tmp_path / "20-app.sh").write_text("")
    cfg = {
        "name": "test-container",
        "base_image": "nvcr.io/nvidia/pytorch:25.04-py3",
        "partition": "gpu",
        "gres": "gpu:1",
        "_local_hooks_dir": str(tmp_path),
    }
    sbatch = _generate_sbatch(cfg, "/fsx/physai/builds/test-123", "test-123")
    expected = """\
#!/bin/bash
#SBATCH --job-name=physai/build/test-container
#SBATCH --comment="base=nvcr.io/nvidia/pytorch:25.04-py3"
#SBATCH --partition=gpu
#SBATCH --gres=gpu:1
#SBATCH --output=/fsx/physai/logs/%j.out
set -eo pipefail
trap 'echo "\\nBuild failed. Container may be left on the worker node."; echo "  Clean up: physai clean --enroot"' ERR
SECONDS=0
BUILD_DIR=/fsx/physai/builds/test-123
BUILD_NAME=test-123
SQSH=/fsx/enroot/test-container.sqsh

if [ -f "$SQSH" ]; then
  echo "ERROR: $SQSH exists. Use --rebuild to replace."
  exit 1
fi

echo "=== init (${SECONDS}s) ==="
srun --container-image=nvcr.io/nvidia/pytorch:25.04-py3 --container-name=$BUILD_NAME --container-mounts=/fsx:/fsx --container-remap-root bash "/fsx/physai/builds/test-123/build-scripts/init-env.root.sh"

echo "=== 10-sys.root.sh (${SECONDS}s) ==="
srun --container-name=$BUILD_NAME --container-mounts=/fsx:/fsx --container-remap-root bash "/fsx/physai/builds/test-123/setup-hooks/10-sys.root.sh"

echo "=== 20-app.sh (${SECONDS}s) ==="
srun --container-name=$BUILD_NAME --container-mounts=/fsx:/fsx bash "/fsx/physai/builds/test-123/setup-hooks/20-app.sh"

echo "=== copy app/ (${SECONDS}s) ==="
srun --container-name=$BUILD_NAME --container-mounts=/fsx:/fsx --container-remap-root bash "/fsx/physai/builds/test-123/build-scripts/mkdir-app.root.sh"
srun --container-name=$BUILD_NAME --container-mounts=/fsx:/fsx bash "/fsx/physai/builds/test-123/build-scripts/copy-app.sh"

echo "=== export squashfs (${SECONDS}s) ==="
enroot export -o "$SQSH" pyxis_${BUILD_NAME}
enroot remove -f pyxis_${BUILD_NAME}

echo "Build complete: $SQSH (${SECONDS}s)"
"""
    assert sbatch == expected


def test_generate_sbatch_base_container(tmp_path):
    """base_container resolves to /fsx/enroot/<name>.sqsh in the --container-image arg."""
    (tmp_path / "10-x.sh").write_text("")
    cfg = {
        "name": "child",
        "base_container": "parent",
        "partition": "gpu",
        "gres": "gpu:1",
        "_local_hooks_dir": str(tmp_path),
    }
    sbatch = _generate_sbatch(cfg, "/fsx/physai/builds/child-1", "child-1")
    assert "--container-image=/fsx/enroot/parent.sqsh" in sbatch
    assert "base=/fsx/enroot/parent.sqsh" in sbatch


def test_generate_sbatch_rebuild(tmp_path):
    """--rebuild removes the sqsh at job start instead of aborting."""
    (tmp_path / "10-x.sh").write_text("")
    cfg = {
        "name": "c",
        "base_image": "nvcr.io/foo",
        "partition": "gpu",
        "gres": "gpu:1",
        "_local_hooks_dir": str(tmp_path),
    }
    sbatch = _generate_sbatch(cfg, "/fsx/physai/builds/c-1", "c-1", rebuild=True)
    assert "--rebuild: removing $SQSH" in sbatch
    assert 'rm -f "$SQSH"' in sbatch
    assert "ERROR: $SQSH exists" not in sbatch


def test_generate_sbatch_no_rebuild(tmp_path):
    """Without --rebuild, the sbatch aborts if sqsh already exists."""
    (tmp_path / "10-x.sh").write_text("")
    cfg = {
        "name": "c",
        "base_image": "nvcr.io/foo",
        "partition": "gpu",
        "gres": "gpu:1",
        "_local_hooks_dir": str(tmp_path),
    }
    sbatch = _generate_sbatch(cfg, "/fsx/physai/builds/c-1", "c-1")
    assert "ERROR: $SQSH exists" in sbatch
    assert "--rebuild: removing" not in sbatch


# ── _find_active_build_job ──


def test_find_active_build_job_none():
    session = MagicMock()
    session.run.return_value = ""
    assert _find_active_build_job(session, "foo") is None


def test_find_active_build_job_one():
    session = MagicMock()
    session.run.return_value = "123\n"
    assert _find_active_build_job(session, "foo") == "123"


def test_find_active_build_job_picks_highest():
    session = MagicMock()
    session.run.return_value = "100\n200\n150\n"
    assert _find_active_build_job(session, "foo") == "200"


def test_find_active_build_job_queries_by_name():
    session = MagicMock()
    session.run.return_value = ""
    _find_active_build_job(session, "leisaac-runtime")
    cmd = session.run.call_args[0][0]
    assert '-n "physai/build/leisaac-runtime"' in cmd
