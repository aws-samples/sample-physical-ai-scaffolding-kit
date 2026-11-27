"""Check which information sources the advisor can use: MLflow and VAMS.

A report needs the training runs from MLflow. VAMS adds the datasets the runs trained on, and
cannot replace MLflow because the datasets are found through the runs.
"""

import os
from collections.abc import Callable
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError
from mlflow import MlflowClient
from mlflow.exceptions import MlflowException

from pask_training_advisor.fetch import TRACKING_URI_ENV
from pask_training_advisor.vams import CliRunner, VamsError, list_databases

MODE_FULL = "full"
MODE_MLFLOW_ONLY = "mlflow_only"
MODE_VAMS_ONLY = "vams_only"
MODE_NONE = "none"
MLFLOW_ERRORS: tuple[type[BaseException], ...] = (
    MlflowException,
    BotoCoreError,
    ClientError,
    OSError,
)


def _redact(message: str) -> str:
    """Replace the tracking URI in a message so that the App ARN is not printed.

    Args:
        message: Message that may contain the value of ``MLFLOW_TRACKING_URI``.

    Returns:
        The message with the tracking URI replaced by the variable name.
    """
    uri = os.environ.get(TRACKING_URI_ENV, "")
    return message.replace(uri, f"<{TRACKING_URI_ENV}>") if uri else message


def check_mlflow(client_factory: Callable[[], MlflowClient]) -> dict[str, Any]:
    """Check that the MLflow App can be read.

    Args:
        client_factory: Function that creates the MLflow client.

    Returns:
        ``{"available": bool, "experimentCount": int}`` or ``{"available": False, "reason":
        ...}``. The tracking URI is never included.
    """
    if not os.environ.get(TRACKING_URI_ENV):
        return {"available": False, "reason": f"{TRACKING_URI_ENV} is not set"}
    try:
        experiments = client_factory().search_experiments()
    except MLFLOW_ERRORS as error:
        return {"available": False, "reason": _redact(f"{type(error).__name__}: {error}")}
    return {"available": True, "experimentCount": len(experiments)}


def check_vams(runner: CliRunner, profile: str) -> dict[str, Any]:
    """Check that VamsCLI can read VAMS with the profile.

    Args:
        runner: Function that runs VamsCLI.
        profile: VamsCLI profile.

    Returns:
        ``{"available": True, "profile": ..., "databaseIds": [...]}`` or ``{"available":
        False, "profile": ..., "reason": ...}``.
    """
    try:
        databases = list_databases(runner, profile)
    except VamsError as error:
        return {"available": False, "profile": profile, "reason": str(error)}
    return {
        "available": True,
        "profile": profile,
        "databaseIds": [database.get("databaseId") for database in databases],
    }


def decide_mode(mlflow_available: bool, vams_available: bool) -> str:
    """Name the combination of available sources.

    Args:
        mlflow_available: Whether MLflow can be read.
        vams_available: Whether VAMS can be read.

    Returns:
        ``full``, ``mlflow_only``, ``vams_only`` or ``none``.
    """
    if mlflow_available:
        return MODE_FULL if vams_available else MODE_MLFLOW_ONLY
    return MODE_VAMS_ONLY if vams_available else MODE_NONE


def detect_environment(
    client_factory: Callable[[], MlflowClient], runner: CliRunner, profile: str
) -> dict[str, Any]:
    """Check MLflow and VAMS and decide whether a report can be written.

    Args:
        client_factory: Function that creates the MLflow client.
        runner: Function that runs VamsCLI.
        profile: VamsCLI profile.

    Returns:
        The result of each check, the ``mode``, and ``canReport`` (true when MLflow is
        available).
    """
    mlflow = check_mlflow(client_factory)
    vams = check_vams(runner, profile)
    mode = decide_mode(mlflow["available"], vams["available"])
    return {
        "mlflow": mlflow,
        "vams": vams,
        "mode": mode,
        "canReport": mlflow["available"],
    }
