"""Tests for uploading the advisor outputs to MLflow artifacts."""

from pathlib import Path

import pytest
from mlflow.tracking import MlflowClient

from pask_training_advisor.models import (
    DIAGNOSIS_FILE,
    PROPOSAL_FILE,
    REPORT_FILE,
    SNAPSHOT_FILE,
    TIMESTAMP_FORMAT,
    VAMS_PARAMETERS_FILE,
)
from pask_training_advisor.publish import (
    PublishError,
    PublishResult,
    contains_arn,
    publish_artifacts,
)

SESSION_TIMESTAMP = "20261018T235046Z"
EXPLICIT_TIMESTAMP = "20261019T010203Z"
RUN_PARAMS = {"steps": "3000", "batch_size": "32"}
RUN_METRICS = {"train/loss": 0.5}
FIGURE_FILES = ("loss.png", "grad_norm.png")
EXPECTED_FILES = tuple(
    sorted(
        (
            REPORT_FILE,
            SNAPSHOT_FILE,
            DIAGNOSIS_FILE,
            PROPOSAL_FILE,
            VAMS_PARAMETERS_FILE,
            *FIGURE_FILES,
        )
    )
)
SAMPLE_ARN = "arn:aws:sagemaker:us-west-2:123456789012:mlflow-app/app-example"


def _create_run(client: MlflowClient) -> str:
    """Create a run with fixed params and metrics in a fresh experiment.

    Args:
        client: MLflow client bound to the local file store.

    Returns:
        ID of the created run.
    """
    experiment_id = client.create_experiment("publish-test")
    run_id = client.create_run(experiment_id).info.run_id
    for key, value in RUN_PARAMS.items():
        client.log_param(run_id, key, value)
    for key, value in RUN_METRICS.items():
        client.log_metric(run_id, key, value, step=1)
    return run_id


def _write_session_outputs(output_dir: Path, report_text: str) -> None:
    """Write a full set of advisor outputs plus files that must not be published.

    Args:
        output_dir: Directory to fill.
        report_text: Content of the report.
    """
    output_dir.mkdir(parents=True)
    (output_dir / REPORT_FILE).write_text(report_text, encoding="utf-8")
    for name in (SNAPSHOT_FILE, DIAGNOSIS_FILE, PROPOSAL_FILE, VAMS_PARAMETERS_FILE):
        (output_dir / name).write_text('{"schemaVersion": 1}', encoding="utf-8")
    for name in FIGURE_FILES:
        (output_dir / name).write_bytes(b"\x89PNG")
    (output_dir / "notes.txt").write_text("scratch", encoding="utf-8")
    nested_dir = output_dir / "extra"
    nested_dir.mkdir()
    (nested_dir / "loss.png").write_bytes(b"\x89PNG")


def _artifact_names(client: MlflowClient, run_id: str, artifact_path: str) -> list[str]:
    """List the base names of the artifacts directly under a path.

    Args:
        client: MLflow client bound to the local file store.
        run_id: ID of the run.
        artifact_path: Artifact directory to list.

    Returns:
        Sorted base names of the artifacts.
    """
    return sorted(Path(info.path).name for info in client.list_artifacts(run_id, artifact_path))


@pytest.fixture
def mlflow_client(local_mlflow_uri: str) -> MlflowClient:
    """Return an MLflow client bound to the temporary file store."""
    return MlflowClient(tracking_uri=local_mlflow_uri)


def test_publish_uploads_only_advisor_files(mlflow_client: MlflowClient, tmp_path: Path) -> None:
    run_id = _create_run(mlflow_client)
    output_dir = tmp_path / "outputs" / SESSION_TIMESTAMP
    _write_session_outputs(output_dir, "# Report\n")

    result = publish_artifacts(mlflow_client, run_id, output_dir)

    expected_path = f"advisor/{SESSION_TIMESTAMP}"
    assert result == PublishResult(artifact_path=expected_path, files=EXPECTED_FILES)
    assert _artifact_names(mlflow_client, run_id, expected_path) == list(EXPECTED_FILES)
    assert _artifact_names(mlflow_client, run_id, "") == ["advisor"]
    assert result.to_dict() == {"artifactPath": expected_path, "files": list(EXPECTED_FILES)}


def test_publish_keeps_params_and_metrics(mlflow_client: MlflowClient, tmp_path: Path) -> None:
    run_id = _create_run(mlflow_client)
    before = mlflow_client.get_run(run_id).data
    output_dir = tmp_path / SESSION_TIMESTAMP
    _write_session_outputs(output_dir, "# Report\n")

    publish_artifacts(mlflow_client, run_id, output_dir)

    after = mlflow_client.get_run(run_id).data
    assert after.params == before.params == RUN_PARAMS
    assert after.metrics == before.metrics == RUN_METRICS
    assert after.tags == before.tags


def test_publish_uses_explicit_timestamp(mlflow_client: MlflowClient, tmp_path: Path) -> None:
    run_id = _create_run(mlflow_client)
    output_dir = tmp_path / SESSION_TIMESTAMP
    _write_session_outputs(output_dir, "# Report\n")

    result = publish_artifacts(mlflow_client, run_id, output_dir, timestamp=EXPLICIT_TIMESTAMP)

    assert result.artifact_path == f"advisor/{EXPLICIT_TIMESTAMP}"
    assert _artifact_names(mlflow_client, run_id, result.artifact_path) == list(EXPECTED_FILES)


def test_publish_falls_back_to_current_timestamp(
    mlflow_client: MlflowClient, tmp_path: Path
) -> None:
    run_id = _create_run(mlflow_client)
    output_dir = tmp_path / "custom-output"
    output_dir.mkdir()
    (output_dir / REPORT_FILE).write_text("# Report\n", encoding="utf-8")

    result = publish_artifacts(mlflow_client, run_id, output_dir)

    root, timestamp = result.artifact_path.split("/")
    assert root == "advisor"
    assert len(timestamp) == len(SESSION_TIMESTAMP)
    assert timestamp.endswith("Z")
    assert result.files == (REPORT_FILE,)
    assert _artifact_names(mlflow_client, run_id, result.artifact_path) == [REPORT_FILE]


def test_publish_rejects_malformed_timestamp(mlflow_client: MlflowClient, tmp_path: Path) -> None:
    run_id = _create_run(mlflow_client)
    output_dir = tmp_path / SESSION_TIMESTAMP
    _write_session_outputs(output_dir, "# Report\n")

    with pytest.raises(PublishError, match=TIMESTAMP_FORMAT):
        publish_artifacts(mlflow_client, run_id, output_dir, timestamp="../escape")

    assert _artifact_names(mlflow_client, run_id, "") == []


def test_publish_requires_report(mlflow_client: MlflowClient, tmp_path: Path) -> None:
    run_id = _create_run(mlflow_client)
    output_dir = tmp_path / SESSION_TIMESTAMP
    _write_session_outputs(output_dir, "# Report\n")
    (output_dir / REPORT_FILE).unlink()

    with pytest.raises(PublishError, match=REPORT_FILE):
        publish_artifacts(mlflow_client, run_id, output_dir)

    assert _artifact_names(mlflow_client, run_id, "") == []


def test_publish_rejects_report_with_arn(mlflow_client: MlflowClient, tmp_path: Path) -> None:
    run_id = _create_run(mlflow_client)
    output_dir = tmp_path / SESSION_TIMESTAMP
    _write_session_outputs(output_dir, f"# Report\n\nTracking URI: {SAMPLE_ARN}\n")

    with pytest.raises(PublishError, match="ARN"):
        publish_artifacts(mlflow_client, run_id, output_dir)

    assert _artifact_names(mlflow_client, run_id, "") == []


def test_publish_rejects_missing_output_dir(mlflow_client: MlflowClient, tmp_path: Path) -> None:
    run_id = _create_run(mlflow_client)

    with pytest.raises(PublishError, match="does not exist"):
        publish_artifacts(mlflow_client, run_id, tmp_path / "missing")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f"uri {SAMPLE_ARN}", True),
        ("arn:aws-cn:s3:::bucket", True),
        ("no resource names here", False),
        ("", False),
    ],
)
def test_contains_arn(text: str, expected: bool) -> None:
    assert contains_arn(text) is expected
