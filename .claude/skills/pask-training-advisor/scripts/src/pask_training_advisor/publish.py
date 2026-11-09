"""Upload the advisor report, figures, and JSON files to the target run as MLflow artifacts.

Only ``log_artifacts`` is used, so the params, metrics, and tags of the run are never changed.
"""

import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from mlflow.tracking import MlflowClient

from pask_training_advisor.models import (
    DATASETS_FILE,
    DIAGNOSIS_FILE,
    PROPOSAL_FILE,
    REPORT_FILE,
    SNAPSHOT_FILE,
    TIMESTAMP_FORMAT,
    VAMS_PARAMETERS_FILE,
    utc_timestamp,
)

ARTIFACT_ROOT = "advisor"
ARN_MARKER = "arn:aws"
FIGURE_SUFFIX = ".png"
PUBLISHED_JSON_FILES: tuple[str, ...] = (
    SNAPSHOT_FILE,
    DATASETS_FILE,
    DIAGNOSIS_FILE,
    PROPOSAL_FILE,
    VAMS_PARAMETERS_FILE,
)


class PublishError(ValueError):
    """Raised when the output directory cannot be published as advisor artifacts."""


@dataclass(frozen=True)
class PublishResult:
    """Outcome of one publish.

    Attributes:
        artifact_path: Artifact directory of the run the files were uploaded to.
        files: Names of the uploaded files in sorted order.
    """

    artifact_path: str
    files: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Convert the result to a JSON compatible dictionary with camelCase keys.

        Returns:
            The artifact path and the uploaded file names.
        """
        return {"artifactPath": self.artifact_path, "files": list(self.files)}


def contains_arn(text: str) -> bool:
    """Tell whether a text contains an AWS ARN.

    Args:
        text: Text to inspect.

    Returns:
        True if the text contains ``arn:aws``.
    """
    return ARN_MARKER in text


def _is_timestamp(value: str) -> bool:
    """Tell whether a string is a compact UTC timestamp in ``TIMESTAMP_FORMAT``.

    Args:
        value: String to inspect.

    Returns:
        True if the string parses as ``%Y%m%dT%H%M%SZ``.
    """
    try:
        datetime.strptime(value, TIMESTAMP_FORMAT)
    except ValueError:
        return False
    return True


def _resolve_timestamp(output_dir: Path, timestamp: str | None) -> str:
    """Choose the timestamp that names the artifact directory.

    Args:
        output_dir: Output directory of the advisor session.
        timestamp: Timestamp given by the caller, or None to derive one.

    Returns:
        ``timestamp`` when given, otherwise the output directory name when it is a timestamp,
        otherwise the current UTC timestamp.

    Raises:
        PublishError: If ``timestamp`` is given but is not in ``TIMESTAMP_FORMAT``.
    """
    if timestamp is not None:
        if not _is_timestamp(timestamp):
            raise PublishError(
                f"timestamp {timestamp!r} does not match the format {TIMESTAMP_FORMAT!r}"
            )
        return timestamp
    if _is_timestamp(output_dir.name):
        return output_dir.name
    return utc_timestamp()


def _is_publishable(path: Path) -> bool:
    """Tell whether a file in the output directory belongs in the advisor artifacts.

    Args:
        path: Entry of the output directory.

    Returns:
        True for the report, the figures, and the advisor JSON files.
    """
    if not path.is_file():
        return False
    return (
        path.name == REPORT_FILE
        or path.name in PUBLISHED_JSON_FILES
        or path.suffix == FIGURE_SUFFIX
    )


def _select_files(output_dir: Path) -> list[Path]:
    """Select the files to publish and check the report.

    Args:
        output_dir: Output directory of the advisor session.

    Returns:
        Files to publish sorted by name.

    Raises:
        PublishError: If the directory does not exist, the report is missing, or the report
            contains an ARN.
    """
    if not output_dir.is_dir():
        raise PublishError(f"Output directory {output_dir} does not exist")
    report_path = output_dir / REPORT_FILE
    if not report_path.is_file():
        raise PublishError(f"{REPORT_FILE} is missing in {output_dir}")
    if contains_arn(report_path.read_text(encoding="utf-8")):
        raise PublishError(f"{REPORT_FILE} contains an ARN, remove it before publishing")
    return sorted(
        (path for path in output_dir.iterdir() if _is_publishable(path)),
        key=lambda path: path.name,
    )


def publish_artifacts(
    client: MlflowClient,
    run_id: str,
    output_dir: Path,
    *,
    timestamp: str | None = None,
) -> PublishResult:
    """Upload the report, figures, and JSON files of a session to a run in one call.

    The selected files are copied to a temporary directory that is removed afterwards, and the
    directory is uploaded with ``log_artifacts`` under ``advisor/<timestamp>``. Other files and
    subdirectories of ``output_dir`` are not uploaded.

    Args:
        client: MLflow client connected to the tracking server.
        run_id: ID of the run that receives the artifacts.
        output_dir: Output directory of the advisor session.
        timestamp: Timestamp in ``%Y%m%dT%H%M%SZ`` that names the artifact directory. When
            omitted, the output directory name is used if it is a timestamp, otherwise the
            current UTC time.

    Returns:
        The artifact path and the names of the uploaded files.

    Raises:
        PublishError: If the report is missing or contains an ARN, the output directory does not
            exist, or ``timestamp`` is malformed.
    """
    files = _select_files(output_dir)
    artifact_path = f"{ARTIFACT_ROOT}/{_resolve_timestamp(output_dir, timestamp)}"
    with tempfile.TemporaryDirectory() as staging_dir:
        for path in files:
            shutil.copy2(path, Path(staging_dir) / path.name)
        client.log_artifacts(run_id, staging_dir, artifact_path=artifact_path)
    return PublishResult(artifact_path=artifact_path, files=tuple(path.name for path in files))
