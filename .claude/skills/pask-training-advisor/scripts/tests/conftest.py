"""Shared pytest fixtures: dummy AWS credentials, run snapshot factories, and synthetic curves."""

import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from pask_training_advisor.models import (
    TEMPLATE_PATH,
    TRAINING_PARAM_KEYS,
    MetricPoint,
    RunSnapshot,
)

TEST_REGION = "us-west-2"
CURVE_SEED = 20261018
CURVE_STEP_INTERVAL = 100
CURVE_START_TIMESTAMP_MS = 1_790_000_000_000
CURVE_TIMESTAMP_INTERVAL_MS = 60_000
SHORT_CURVE_POINTS = 5
CURVE_KINDS = ("converging", "plateau", "diverging", "spiky", "short")


@pytest.fixture(autouse=True)
def aws_test_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point boto3 at a fixed region with dummy credentials so no real account is used."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", TEST_REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.delenv("AWS_PROFILE", raising=False)


def _template_defaults() -> dict[str, int | bool | str]:
    """Read the default value of the 12 training parameters from template.json.

    Returns:
        Default values keyed by training parameter name.
    """
    template = json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))
    defaults = {entry["tagKey"]: entry["default"] for entry in template["tagSchema"]}
    return {key: defaults[key] for key in TRAINING_PARAM_KEYS}


def curve_to_points(values: Sequence[float]) -> list[MetricPoint]:
    """Turn metric values into points logged every ``CURVE_STEP_INTERVAL`` steps.

    Args:
        values: Metric values in logging order.

    Returns:
        Metric points starting at step ``CURVE_STEP_INTERVAL``.
    """
    return [
        MetricPoint(
            step=(index + 1) * CURVE_STEP_INTERVAL,
            value=float(value),
            timestamp=CURVE_START_TIMESTAMP_MS + index * CURVE_TIMESTAMP_INTERVAL_MS,
        )
        for index, value in enumerate(values)
    ]


@pytest.fixture
def make_run_snapshot() -> Callable[..., RunSnapshot]:
    """Return a factory of run snapshots whose 12 parameters are the template defaults.

    The factory takes ``RunSnapshot`` fields as keyword overrides. ``curves`` maps metric names
    to plain value sequences and is converted to metric points; ``training_params`` is merged
    into the template defaults instead of replacing them.
    """
    defaults = _template_defaults()

    def factory(**overrides: Any) -> RunSnapshot:
        curves: dict[str, Sequence[float]] = overrides.pop("curves", {})
        training_params = {**defaults, **overrides.pop("training_params", {})}
        metrics = {metric_key: curve_to_points(values) for metric_key, values in curves.items()}
        metrics.update(overrides.pop("metrics", {}))
        fields: dict[str, Any] = {
            "run_id": "run-target",
            "run_name": "target-run",
            "experiment_id": "1",
            "experiment_name": "vams-training",
            "status": "FINISHED",
            "start_time": CURVE_START_TIMESTAMP_MS,
            "end_time": CURVE_START_TIMESTAMP_MS + 3_600_000,
            "params": {"steps": str(training_params["STEPS"])},
            "tags": {"lerobot.dataset_repo_id": "pask/test-dataset"},
            "metrics": metrics,
            "training_params": training_params,
            "planned_steps": training_params["STEPS"],
            "lr_schedule_params": {},
            "notes": [],
        }
        fields.update(overrides)
        return RunSnapshot(**fields)

    return factory


def _make_curve(
    kind: str,
    n_points: int,
    start: float,
    end: float,
    noise: float,
    seed: int,
) -> list[float]:
    """Build a synthetic loss curve of the given shape.

    Args:
        kind: One of ``CURVE_KINDS``.
        n_points: Number of points. Ignored for ``short``, which has ``SHORT_CURVE_POINTS``.
        start: Loss at the first point.
        end: Loss the curve approaches.
        noise: Relative standard deviation of the multiplicative noise.
        seed: Seed of the random generator.

    Returns:
        Loss values in logging order.

    Raises:
        ValueError: If ``kind`` is unknown.
    """
    if kind not in CURVE_KINDS:
        raise ValueError(f"Unknown curve kind {kind!r}, expected one of {CURVE_KINDS}")
    rng = np.random.default_rng(seed)
    point_count = SHORT_CURVE_POINTS if kind == "short" else n_points
    progress = np.linspace(0.0, 1.0, point_count)
    if kind == "plateau":
        base = end + (start - end) * np.exp(-progress * 25.0)
    elif kind == "diverging":
        base = end + (start - end) * np.exp(-progress * 5.0) + start * np.expm1(progress * 3.0)
    else:
        base = end + (start - end) * np.exp(-progress * 3.0)
    values = base * (1.0 + rng.normal(0.0, noise, point_count))
    if kind == "spiky":
        spike_indices = rng.choice(np.arange(point_count // 4, point_count), 8, replace=False)
        values[spike_indices] *= 10.0
    return [float(value) for value in values]


@pytest.fixture
def synthetic_curve() -> Callable[..., list[float]]:
    """Return a builder of synthetic loss curves with a fixed seed.

    The builder takes ``kind`` (converging / plateau / diverging / spiky / short) and the
    keyword arguments ``n_points`` (200), ``start`` (1.0), ``end`` (0.05), ``noise`` (0.01),
    and ``seed`` (``CURVE_SEED``).
    """

    def builder(
        kind: str,
        n_points: int = 200,
        start: float = 1.0,
        end: float = 0.05,
        noise: float = 0.01,
        seed: int = CURVE_SEED,
    ) -> list[float]:
        return _make_curve(kind, n_points, start, end, noise, seed)

    return builder


@pytest.fixture
def local_mlflow_uri(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Point MLflow at a file store in a temporary directory.

    Returns:
        The ``file://`` tracking URI, also set as ``MLFLOW_TRACKING_URI``.
    """
    monkeypatch.setenv("MLFLOW_ALLOW_FILE_STORE", "true")
    tracking_uri = (tmp_path / "mlruns").as_uri()
    monkeypatch.setenv("MLFLOW_TRACKING_URI", tracking_uri)
    return tracking_uri
