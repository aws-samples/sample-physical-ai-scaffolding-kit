"""Tests for physai CLI — pure unit tests, no SSH required."""

from unittest.mock import MagicMock

import pytest

from physai.build import (
    _discover_hooks,
    _find_project_yaml,
    _generate_env_txt,
    _generate_sbatch,
    _merge_configs,
    _validate_config,
)
from physai.clean import run_clean
from physai.config import load
from physai.jobs import _parse_job_name, cancel_job, list_jobs


# ── config ──


def test_load_with_host_override(tmp_path, monkeypatch):
    monkeypatch.setattr("physai.config.CONFIG_PATH", tmp_path / "nonexistent.yaml")
    cfg = load(host_override="myhost")
    assert cfg["host"] == "myhost"


def test_load_from_file(tmp_path, monkeypatch):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("host: filehost\ns3_bucket: mybucket\n")
    monkeypatch.setattr("physai.config.CONFIG_PATH", config_file)
    cfg = load()
    assert cfg["host"] == "filehost"
    assert cfg["s3_bucket"] == "mybucket"


def test_load_override_beats_file(tmp_path, monkeypatch):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("host: filehost\n")
    monkeypatch.setattr("physai.config.CONFIG_PATH", config_file)
    cfg = load(host_override="clihost")
    assert cfg["host"] == "clihost"


def test_load_no_host_raises(tmp_path, monkeypatch):
    monkeypatch.setattr("physai.config.CONFIG_PATH", tmp_path / "nonexistent.yaml")
    with pytest.raises(SystemExit):
        load()


# ── build: _merge_configs ──


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


# ── build: _validate_config ──


def test_validate_passes():
    _validate_config({"name": "test", "base_image": "img"})


def test_validate_missing_name():
    with pytest.raises(SystemExit, match="name"):
        _validate_config({"base_image": "img"})


def test_validate_missing_base_image():
    with pytest.raises(SystemExit, match="base_image"):
        _validate_config({"name": "test"})


# ── build: _discover_hooks ──


def test_discover_hooks(tmp_path):
    (tmp_path / "10-sys.root.sh").write_text("#!/bin/bash\n")
    (tmp_path / "20-install.sh").write_text("#!/bin/bash\n")
    (tmp_path / "README.md").write_text("not a hook\n")
    (tmp_path / "no-prefix.sh").write_text("#!/bin/bash\n")
    hooks = _discover_hooks(tmp_path)
    assert len(hooks) == 2
    assert hooks[0] == {"name": "10-sys.root.sh", "root": True}
    assert hooks[1] == {"name": "20-install.sh", "root": False}


# ── build: _generate_env_txt ──


def test_generate_env_txt():
    assert _generate_env_txt({"A": "1", "B": "2"}) == "A=1\nB=2\n"


def test_generate_env_txt_empty():
    assert _generate_env_txt({}) == ""


# ── build: _find_project_yaml ──


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


# ── build: _generate_sbatch ──


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


# ── jobs: _parse_job_name ──


def test_parse_job_name():
    assert _parse_job_name("physai/build/leisaac-runtime") == (
        "build",
        "leisaac-runtime",
    )
    assert _parse_job_name("physai/train/so101") == ("train", "so101")
    assert _parse_job_name("other-job") == ("?", "other-job")


# ── jobs: list_jobs ──


def test_list_jobs_empty(capsys):
    session = MagicMock()
    session.run.return_value = ""
    session.has_sacct = False
    list_jobs(session)
    assert "No physai jobs found." in capsys.readouterr().out


def test_list_jobs_with_active(capsys):
    session = MagicMock()
    session.run.return_value = '"123|physai/build/test|RUNNING|5:00|base=foo"'
    session.has_sacct = False
    list_jobs(session)
    out = capsys.readouterr().out
    assert "123" in out
    assert "build" in out
    assert "RUNNING" in out


# ── jobs: cancel_job ──


def test_cancel_job(capsys):
    session = MagicMock()
    session.run.return_value = ""
    cancel_job(session, "123")
    session.run.assert_called_once_with("scancel 123")
    assert "Cancelled job 123" in capsys.readouterr().out


# ── clean ──


def test_clean_dry_run(capsys):
    session = MagicMock()
    session.run.side_effect = [
        "",  # squeue (no active jobs)
        "/fsx/physai/builds/old-build",  # find builds
        "/fsx/physai/logs/100.out",  # find logs
    ]
    run_clean(session, older_than=0, dry_run=True, force=False)
    out = capsys.readouterr().out
    assert "Would remove" in out
    assert "old-build" in out
    assert "100.out" in out
    # Should not have called rm
    assert not any("rm" in str(c) for c in session.run.call_args_list)


def test_clean_force(capsys):
    session = MagicMock()
    session.run.side_effect = [
        "",  # squeue
        "/fsx/physai/builds/old-build",  # find builds
        "/fsx/physai/logs/100.out",  # find logs
        "",  # rm builds
        "",  # rm logs
    ]
    run_clean(session, older_than=0, dry_run=False, force=True)
    out = capsys.readouterr().out
    assert "Removed 2 items." in out


def test_clean_nothing(capsys):
    session = MagicMock()
    session.run.side_effect = [
        "",  # squeue
        "",  # find builds
        "",  # find logs
    ]
    run_clean(session, older_than=7, dry_run=False, force=False)
    assert "Nothing to clean." in capsys.readouterr().out
