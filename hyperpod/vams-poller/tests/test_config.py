"""Tests for the poller configuration, the training environment file, and path helpers."""

import os
from pathlib import Path
from typing import Any

import pytest

from pask_vams_poller.config import (
    PollerConfig,
    fsx_dataset_root,
    job_name,
    load_training_env,
    output_dir,
    run_dir,
    staging_prefix,
    training_dir,
)


def _write_env_file(path: Path, content: str, mode: int) -> str:
    """Write an environment file with the given permission bits and return its path."""
    path.write_text(content, encoding="utf-8")
    os.chmod(path, mode)
    return str(path)


def test_from_env_applies_documented_defaults(poller_env: dict[str, str]) -> None:
    config = PollerConfig.from_env(poller_env)

    assert config.queue_url == poller_env["PASK_VAMS_QUEUE_URL"]
    assert config.table_name == poller_env["PASK_VAMS_TABLE_NAME"]
    assert config.data_bucket == poller_env["PASK_VAMS_DATA_BUCKET"]
    assert config.region == "us-west-2"
    assert config.config_name == "test"
    assert config.lustre_fs_path == "/s3link"
    assert config.lustre_s3_prefix == ""
    assert config.visibility_timeout_seconds == 300
    assert config.heartbeat_interval_seconds == 300
    assert config.abort_grace_seconds == 1800
    assert config.lease_seconds == 900
    assert config.item_ttl_days == 30
    assert config.ingest_wait_seconds == 3600
    assert config.max_concurrent_runs == 1
    assert config.slurm_partition == ""
    assert config.mlflow_experiment_name == "vams-training"
    assert config.training_env_file == "/home/ubuntu/.config/pask/lerobot-openpi.env"
    assert config.reconcile_interval_seconds == 30


def test_from_env_reads_overrides(poller_env: dict[str, str]) -> None:
    overrides = {
        "PASK_VAMS_LUSTRE_S3_PREFIX": "datasets",
        "PASK_VAMS_VISIBILITY_TIMEOUT_SECONDS": "600",
        "PASK_VAMS_HEARTBEAT_INTERVAL_SECONDS": "60",
        "PASK_VAMS_ABORT_GRACE_SECONDS": "120",
        "PASK_VAMS_LEASE_SECONDS": "180",
        "PASK_VAMS_ITEM_TTL_DAYS": "7",
        "PASK_VAMS_INGEST_WAIT_SECONDS": "900",
        "PASK_VAMS_MAX_CONCURRENT_RUNS": "2",
        "PASK_VAMS_SLURM_PARTITION": "gpu",
        "PASK_VAMS_MLFLOW_EXPERIMENT_NAME": "custom",
        "PASK_VAMS_TRAINING_ENV_FILE": "/tmp/training.env",
        "PASK_VAMS_RECONCILE_INTERVAL_SECONDS": "10",
    }

    config = PollerConfig.from_env({**poller_env, **overrides})

    assert config.lustre_s3_prefix == "datasets"
    assert config.visibility_timeout_seconds == 600
    assert config.heartbeat_interval_seconds == 60
    assert config.abort_grace_seconds == 120
    assert config.lease_seconds == 180
    assert config.item_ttl_days == 7
    assert config.ingest_wait_seconds == 900
    assert config.max_concurrent_runs == 2
    assert config.slurm_partition == "gpu"
    assert config.mlflow_experiment_name == "custom"
    assert config.training_env_file == "/tmp/training.env"
    assert config.reconcile_interval_seconds == 10


@pytest.mark.parametrize(
    "name",
    [
        "PASK_VAMS_QUEUE_URL",
        "PASK_VAMS_TABLE_NAME",
        "PASK_VAMS_DATA_BUCKET",
        "PASK_VAMS_REGION",
        "PASK_VAMS_CONFIG_NAME",
        "PASK_VAMS_LUSTRE_FS_PATH",
    ],
)
def test_from_env_rejects_missing_required_value(poller_env: dict[str, str], name: str) -> None:
    missing = {key: value for key, value in poller_env.items() if key != name}
    with pytest.raises(ValueError, match=name):
        PollerConfig.from_env(missing)

    with pytest.raises(ValueError, match=name):
        PollerConfig.from_env({**poller_env, name: ""})


@pytest.mark.parametrize("raw_value", ["abc", "1.5", "0", "-1"])
def test_from_env_rejects_invalid_integer(poller_env: dict[str, str], raw_value: str) -> None:
    with pytest.raises(ValueError, match="PASK_VAMS_LEASE_SECONDS"):
        PollerConfig.from_env({**poller_env, "PASK_VAMS_LEASE_SECONDS": raw_value})


def test_from_env_rejects_relative_lustre_fs_path(poller_env: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="LUSTRE_FS_PATH"):
        PollerConfig.from_env({**poller_env, "PASK_VAMS_LUSTRE_FS_PATH": "s3link"})


def test_config_is_frozen(poller_config: PollerConfig) -> None:
    with pytest.raises(AttributeError):
        poller_config.lease_seconds = 1  # type: ignore[misc]


def test_load_training_env_parses_key_value_lines(tmp_path: Path) -> None:
    content = (
        "# lerobot-openpi\n"
        "\n"
        "LEROBOT_PI_BASE_DIR=/fsx/ubuntu/lerobot-openpi\n"
        "HF_TOKEN=hf_abc=def\n"
        "   # indented comment\n"
        "EMPTY_VALUE=\n"
    )
    path = _write_env_file(tmp_path / "training.env", content, 0o600)

    assert load_training_env(path) == {
        "LEROBOT_PI_BASE_DIR": "/fsx/ubuntu/lerobot-openpi",
        "HF_TOKEN": "hf_abc=def",
        "EMPTY_VALUE": "",
    }


def test_load_training_env_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not found"):
        load_training_env(str(tmp_path / "missing.env"))


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644, 0o610])
def test_load_training_env_rejects_group_or_other_permissions(tmp_path: Path, mode: int) -> None:
    path = _write_env_file(tmp_path / "training.env", "KEY=value\n", mode)

    with pytest.raises(ValueError, match="group or others"):
        load_training_env(path)


@pytest.mark.parametrize(
    "line",
    [
        "lower_case=value",
        "1STARTS_WITH_DIGIT=value",
        "export KEY=value",
        "KEY WITH SPACE=value",
        " KEY=value",
        "NO_SEPARATOR",
    ],
)
def test_load_training_env_rejects_malformed_line(tmp_path: Path, line: str) -> None:
    path = _write_env_file(tmp_path / "training.env", f"{line}\n", 0o600)

    with pytest.raises(ValueError, match=":1:"):
        load_training_env(path)


def test_load_training_env_rejects_duplicated_key(tmp_path: Path) -> None:
    path = _write_env_file(tmp_path / "training.env", "KEY=one\nKEY=two\n", 0o600)

    with pytest.raises(ValueError, match="duplicated"):
        load_training_env(path)


def test_staging_prefix_without_s3_prefix(poller_config: PollerConfig) -> None:
    assert staging_prefix(poller_config, "exec-01_A") == "vams/exec-01_A/"


@pytest.mark.parametrize("s3_prefix", ["datasets", "datasets/"])
def test_staging_prefix_normalizes_s3_prefix(poller_env: dict[str, str], s3_prefix: str) -> None:
    config = PollerConfig.from_env({**poller_env, "PASK_VAMS_LUSTRE_S3_PREFIX": s3_prefix})

    assert staging_prefix(config, "exec1") == "datasets/vams/exec1/"


@pytest.mark.parametrize("fs_path", ["/s3link", "/s3link/"])
def test_fsx_dataset_root(poller_env: dict[str, str], fs_path: str) -> None:
    config = PollerConfig.from_env({**poller_env, "PASK_VAMS_LUSTRE_FS_PATH": fs_path})

    assert fsx_dataset_root(config, "exec1") == "/fsx/s3link/vams/exec1/"


def test_training_paths(training_env: dict[str, str]) -> None:
    assert run_dir(training_env, "exec1") == "/fsx/ubuntu/lerobot-openpi/vams-runs/exec1"
    assert output_dir(training_env, "exec1") == "/fsx/ubuntu/lerobot-openpi/outputs/vams-exec1"
    assert training_dir(training_env) == "/fsx/ubuntu/lerobot-openpi/training"
    assert job_name("exec1") == "vams-exec1"


def test_training_paths_strip_trailing_slash(training_env: dict[str, str]) -> None:
    env = {
        **training_env,
        "LEROBOT_PI_BASE_DIR": "/fsx/base/",
        "LEROBOT_PI_OUTPUT_DIR": "/fsx/out/",
    }

    assert run_dir(env, "exec1") == "/fsx/base/vams-runs/exec1"
    assert output_dir(env, "exec1") == "/fsx/out/vams-exec1"


@pytest.mark.parametrize(
    ("key", "build_path"),
    [
        ("LEROBOT_PI_BASE_DIR", lambda env: run_dir(env, "exec1")),
        ("LEROBOT_PI_BASE_DIR", training_dir),
        ("LEROBOT_PI_OUTPUT_DIR", lambda env: output_dir(env, "exec1")),
    ],
)
def test_training_paths_require_absolute_directory(
    training_env: dict[str, str], key: str, build_path: Any
) -> None:
    missing = {name: value for name, value in training_env.items() if name != key}
    relative = {**training_env, key: "relative/dir"}

    for env in (missing, relative):
        with pytest.raises(ValueError, match=key):
            build_path(env)


@pytest.mark.parametrize("pid", ["", "a/b", "../x", "a.b", "a b", "id\n"])
def test_path_helpers_reject_invalid_pipeline_execution_id(
    poller_config: PollerConfig, training_env: dict[str, str], pid: str
) -> None:
    helpers: list[Any] = [
        lambda: staging_prefix(poller_config, pid),
        lambda: fsx_dataset_root(poller_config, pid),
        lambda: run_dir(training_env, pid),
        lambda: output_dir(training_env, pid),
        lambda: job_name(pid),
    ]
    for helper in helpers:
        with pytest.raises(ValueError, match="pipelineExecutionId"):
            helper()
