"""Poller configuration, the training environment file, and path helpers."""

import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass

from pask_vams_poller.constants import PIPELINE_EXECUTION_ID_PATTERN

ENV_PREFIX = "PASK_VAMS_"

REQUIRED_STR_SETTINGS: dict[str, str] = {
    "queue_url": "QUEUE_URL",
    "table_name": "TABLE_NAME",
    "data_bucket": "DATA_BUCKET",
    "region": "REGION",
    "config_name": "CONFIG_NAME",
    "lustre_fs_path": "LUSTRE_FS_PATH",
}

OPTIONAL_STR_SETTINGS: dict[str, tuple[str, str]] = {
    "lustre_s3_prefix": ("LUSTRE_S3_PREFIX", ""),
    "slurm_partition": ("SLURM_PARTITION", ""),
    "mlflow_experiment_name": ("MLFLOW_EXPERIMENT_NAME", "vams-training"),
    "training_env_file": ("TRAINING_ENV_FILE", "/home/ubuntu/.config/pask/lerobot-openpi.env"),
}

INT_SETTINGS: dict[str, tuple[str, int]] = {
    "visibility_timeout_seconds": ("VISIBILITY_TIMEOUT_SECONDS", 300),
    "heartbeat_interval_seconds": ("HEARTBEAT_INTERVAL_SECONDS", 300),
    "abort_grace_seconds": ("ABORT_GRACE_SECONDS", 1800),
    "lease_seconds": ("LEASE_SECONDS", 900),
    "item_ttl_days": ("ITEM_TTL_DAYS", 30),
    "ingest_wait_seconds": ("INGEST_WAIT_SECONDS", 3600),
    "max_concurrent_runs": ("MAX_CONCURRENT_RUNS", 1),
    "reconcile_interval_seconds": ("RECONCILE_INTERVAL_SECONDS", 30),
}

TRAINING_ENV_KEY_PATTERN = re.compile(r"^[A-Z_][A-Z0-9_]*$")
GROUP_OR_OTHER_PERMISSION_BITS = 0o077


@dataclass(frozen=True)
class PollerConfig:
    """Settings of the poller, read from the ``PASK_VAMS_*`` environment variables."""

    queue_url: str
    table_name: str
    data_bucket: str
    lustre_s3_prefix: str
    lustre_fs_path: str
    region: str
    config_name: str
    visibility_timeout_seconds: int
    heartbeat_interval_seconds: int
    abort_grace_seconds: int
    lease_seconds: int
    item_ttl_days: int
    ingest_wait_seconds: int
    max_concurrent_runs: int
    slurm_partition: str
    mlflow_experiment_name: str
    training_env_file: str
    reconcile_interval_seconds: int

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = os.environ) -> "PollerConfig":
        """Build the configuration from environment variables.

        Args:
            environ: Mapping that holds the ``PASK_VAMS_*`` variables.

        Returns:
            The validated configuration.

        Raises:
            ValueError: A required variable is missing or empty, or a value is malformed.
        """
        values: dict[str, str | int] = {}
        for field_name, suffix in REQUIRED_STR_SETTINGS.items():
            values[field_name] = _read_required_str(environ, ENV_PREFIX + suffix)
        for field_name, (suffix, default) in OPTIONAL_STR_SETTINGS.items():
            values[field_name] = environ.get(ENV_PREFIX + suffix, default)
        for field_name, (suffix, default) in INT_SETTINGS.items():
            values[field_name] = _read_positive_int(environ, ENV_PREFIX + suffix, default)

        _validate_lustre_fs_path(str(values["lustre_fs_path"]))
        if not values["mlflow_experiment_name"]:
            raise ValueError(f"{ENV_PREFIX}MLFLOW_EXPERIMENT_NAME must not be empty")
        if not values["training_env_file"]:
            raise ValueError(f"{ENV_PREFIX}TRAINING_ENV_FILE must not be empty")
        return cls(**values)


def _read_required_str(environ: Mapping[str, str], name: str) -> str:
    """Read a required, non-empty string variable.

    Args:
        environ: Environment mapping.
        name: Variable name.

    Returns:
        The value of the variable.

    Raises:
        ValueError: The variable is missing or empty.
    """
    value = environ.get(name, "")
    if not value:
        raise ValueError(f"{name} is required")
    return value


def _read_positive_int(environ: Mapping[str, str], name: str, default: int) -> int:
    """Read a positive integer variable, falling back to its documented default.

    Args:
        environ: Environment mapping.
        name: Variable name.
        default: Value used when the variable is unset.

    Returns:
        The integer value.

    Raises:
        ValueError: The value is not an integer or is not positive.
    """
    raw_value = environ.get(name)
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer, got {raw_value!r}") from error
    if value <= 0:
        raise ValueError(f"{name} must be positive, got {value}")
    return value


def _validate_lustre_fs_path(lustre_fs_path: str) -> None:
    """Check that the Lustre data repository path is absolute.

    Args:
        lustre_fs_path: Value of ``PASK_VAMS_LUSTRE_FS_PATH`` such as ``/s3link``.

    Raises:
        ValueError: The path does not start with ``/``.
    """
    if not lustre_fs_path.startswith("/"):
        raise ValueError(f"{ENV_PREFIX}LUSTRE_FS_PATH must start with '/', got {lustre_fs_path!r}")


def load_training_env(path: str) -> dict[str, str]:
    """Read the training environment file written by the lerobot-openpi setup.

    Each line holds one ``KEY=VALUE`` pair with an already expanded value. Empty lines
    and lines that start with ``#`` are skipped. The line is split at the first ``=``.

    Args:
        path: Path of the environment file.

    Returns:
        Mapping from key to value.

    Raises:
        ValueError: The file is missing, is readable by others than its owner, or has
            a malformed or duplicated line.
    """
    _validate_training_env_permissions(path)
    with open(path, encoding="utf-8") as env_file:
        lines = env_file.read().splitlines()

    training_env: dict[str, str] = {}
    for line_number, line in enumerate(lines, start=1):
        stripped_line = line.strip()
        if not stripped_line or stripped_line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator:
            raise ValueError(f"{path}:{line_number}: expected KEY=VALUE")
        if not TRAINING_ENV_KEY_PATTERN.fullmatch(key):
            raise ValueError(f"{path}:{line_number}: invalid key {key!r}")
        if key in training_env:
            raise ValueError(f"{path}:{line_number}: duplicated key {key!r}")
        training_env[key] = value
    return training_env


def _validate_training_env_permissions(path: str) -> None:
    """Check that the training environment file exists and only its owner can access it.

    Args:
        path: Path of the environment file.

    Raises:
        ValueError: The file is missing, is not a regular file, or has group or other
            permission bits.
    """
    try:
        file_status = os.stat(path)
    except FileNotFoundError as error:
        raise ValueError(f"training environment file not found: {path}") from error
    if not stat.S_ISREG(file_status.st_mode):
        raise ValueError(f"training environment file is not a regular file: {path}")
    permission_bits = stat.S_IMODE(file_status.st_mode)
    if permission_bits & GROUP_OR_OTHER_PERMISSION_BITS:
        raise ValueError(
            f"training environment file must not be accessible by group or others "
            f"(mode {permission_bits:o}): {path}"
        )


def validate_pipeline_execution_id(pipeline_execution_id: str) -> str:
    """Check that a pipelineExecutionId is safe to use in keys, paths, and job names.

    Args:
        pipeline_execution_id: The VAMS pipelineExecutionId.

    Returns:
        The same id.

    Raises:
        ValueError: The id has characters other than ``[A-Za-z0-9_-]`` or is empty.
    """
    if not PIPELINE_EXECUTION_ID_PATTERN.fullmatch(pipeline_execution_id):
        raise ValueError(f"invalid pipelineExecutionId: {pipeline_execution_id!r}")
    return pipeline_execution_id


def normalized_s3_prefix(cfg: PollerConfig) -> str:
    """Return the Lustre S3 prefix with a trailing ``/``, or an empty string.

    Args:
        cfg: Poller configuration.

    Returns:
        ``""`` when the prefix is empty, otherwise the prefix ending with ``/``.
    """
    prefix = cfg.lustre_s3_prefix
    if not prefix or prefix.endswith("/"):
        return prefix
    return prefix + "/"


def staging_prefix(cfg: PollerConfig, pid: str) -> str:
    """Return the staging prefix of an execution in the PASK data bucket.

    Args:
        cfg: Poller configuration.
        pid: The pipelineExecutionId.

    Returns:
        ``<S3Prefix>vams/<pid>/``.
    """
    validate_pipeline_execution_id(pid)
    return f"{normalized_s3_prefix(cfg)}vams/{pid}/"


def fsx_dataset_root(cfg: PollerConfig, pid: str) -> str:
    """Return the FSx directory where the staged dataset of an execution appears.

    Args:
        cfg: Poller configuration.
        pid: The pipelineExecutionId.

    Returns:
        ``/fsx<FileSystemPath>/vams/<pid>/``.
    """
    validate_pipeline_execution_id(pid)
    return f"/fsx{cfg.lustre_fs_path.rstrip('/')}/vams/{pid}/"


def _read_training_dir_setting(training_env: Mapping[str, str], key: str) -> str:
    """Read a directory setting from the training environment without a trailing ``/``.

    Args:
        training_env: Values of the training environment file.
        key: Key such as ``LEROBOT_PI_BASE_DIR``.

    Returns:
        The directory path.

    Raises:
        ValueError: The key is missing, empty, or not an absolute path.
    """
    directory = training_env.get(key, "").rstrip("/")
    if not directory.startswith("/"):
        raise ValueError(f"{key} in the training environment must be an absolute path")
    return directory


def run_dir(training_env: Mapping[str, str], pid: str) -> str:
    """Return the run directory of an execution, which holds the marker and context files.

    Args:
        training_env: Values of the training environment file.
        pid: The pipelineExecutionId.

    Returns:
        ``<LEROBOT_PI_BASE_DIR>/vams-runs/<pid>`` without a trailing ``/``.
    """
    validate_pipeline_execution_id(pid)
    base_dir = _read_training_dir_setting(training_env, "LEROBOT_PI_BASE_DIR")
    return f"{base_dir}/vams-runs/{pid}"


def output_dir(training_env: Mapping[str, str], pid: str) -> str:
    """Return the training output directory of an execution.

    Args:
        training_env: Values of the training environment file.
        pid: The pipelineExecutionId.

    Returns:
        ``<LEROBOT_PI_OUTPUT_DIR>/vams-<pid>``.
    """
    validate_pipeline_execution_id(pid)
    base_output_dir = _read_training_dir_setting(training_env, "LEROBOT_PI_OUTPUT_DIR")
    return f"{base_output_dir}/vams-{pid}"


def training_dir(training_env: Mapping[str, str]) -> str:
    """Return the directory that holds ``slurm_train.sh``.

    Args:
        training_env: Values of the training environment file.

    Returns:
        ``<LEROBOT_PI_BASE_DIR>/training``.
    """
    base_dir = _read_training_dir_setting(training_env, "LEROBOT_PI_BASE_DIR")
    return f"{base_dir}/training"


def job_name(pid: str) -> str:
    """Return the Slurm job name of an execution.

    Args:
        pid: The pipelineExecutionId.

    Returns:
        ``vams-<pid>``.
    """
    validate_pipeline_execution_id(pid)
    return f"vams-{pid}"
