"""Tests of the environment check."""

import subprocess
from collections.abc import Sequence

import pytest
from mlflow import MlflowClient
from mlflow.exceptions import MlflowException

from pask_training_advisor.detect import check_mlflow, check_vams, decide_mode, detect_environment

PROFILE = "test-profile"


def _vams_runner(returncode: int, stdout: str):
    """Return a fake VamsCLI that always prints the same output.

    Args:
        returncode: Exit code.
        stdout: Standard output.

    Returns:
        The runner.
    """

    def runner(command: Sequence[str]) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(list(command), returncode, stdout=stdout, stderr="")

    return runner


@pytest.mark.parametrize(
    ("mlflow_available", "vams_available", "mode"),
    [
        (True, True, "full"),
        (True, False, "mlflow_only"),
        (False, True, "vams_only"),
        (False, False, "none"),
    ],
)
def test_decide_mode(mlflow_available: bool, vams_available: bool, mode: str) -> None:
    assert decide_mode(mlflow_available, vams_available) == mode


def test_check_mlflow_without_tracking_uri(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MLFLOW_TRACKING_URI", raising=False)

    result = check_mlflow(lambda: pytest.fail("the client must not be created"))

    assert result == {"available": False, "reason": "MLFLOW_TRACKING_URI is not set"}


def test_check_mlflow_reads_the_store(local_mlflow_uri: str) -> None:
    result = check_mlflow(lambda: MlflowClient(tracking_uri=local_mlflow_uri))

    assert result["available"] is True


def test_check_mlflow_redacts_the_tracking_uri_in_errors(local_mlflow_uri: str) -> None:
    def factory() -> MlflowClient:
        raise MlflowException(f"cannot reach {local_mlflow_uri}")

    result = check_mlflow(factory)

    assert result["available"] is False
    assert local_mlflow_uri not in result["reason"]
    assert "<MLFLOW_TRACKING_URI>" in result["reason"]


def test_check_vams_lists_databases() -> None:
    result = check_vams(_vams_runner(0, '{"Items": [{"databaseId": "db-1"}]}'), PROFILE)

    assert result == {"available": True, "profile": PROFILE, "databaseIds": ["db-1"]}


def test_check_vams_reports_the_vamscli_error() -> None:
    result = check_vams(_vams_runner(1, '{"error": "Authentication token has expired"}'), PROFILE)

    assert result["available"] is False
    assert "Authentication token has expired" in result["reason"]


def test_vams_alone_cannot_report(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MLFLOW_TRACKING_URI", raising=False)

    result = detect_environment(
        lambda: pytest.fail("the client must not be created"),
        _vams_runner(0, '{"Items": []}'),
        PROFILE,
    )

    assert result["mode"] == "vams_only"
    assert result["canReport"] is False
