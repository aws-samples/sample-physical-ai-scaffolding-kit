"""Tests of the learning curve figures."""

import math
from collections.abc import Callable
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pytest
from matplotlib.ticker import ScalarFormatter

from pask_training_advisor.models import (
    DEFAULT_EXPERIMENTS,
    DEFAULT_MAX_COMPARE_RUNS,
    SCHEMA_VERSION,
    ComparisonCriteria,
    MetricPoint,
    RunSnapshot,
    Snapshot,
)
from pask_training_advisor.plots import _gpu_memory_figure, plot_all, plot_target

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
POINT_COUNT = 200
TARGET_FILES = {"loss.png", "grad_norm.png", "lr.png", "throughput.png", "gpu_mem.png"}
COMPARE_FILES = {"compare_loss_step.png", "compare_loss_samples.png"}
SYSTEM_POINT_COUNT = 40
SYSTEM_INTERVAL_MS = 30_000
RUN_START_MS = 1_790_000_000_000


def _full_curves(loss: list[float], batch_size: int) -> dict[str, list[float]]:
    """Build every metric the figures read, around the given loss curve.

    Args:
        loss: ``train/loss`` values.
        batch_size: Batch size used to derive ``train/samples``.

    Returns:
        Metric values keyed by metric name, all the length of ``loss``.
    """
    point_count = len(loss)
    steps = np.arange(1, point_count + 1) * 100
    return {
        "train/loss": loss,
        "train/grad_norm": list(np.linspace(5.0, 0.5, point_count)),
        "train/lr": list(np.linspace(1e-4, 1e-6, point_count)),
        "train/dataloading_s": [0.05] * point_count,
        "train/update_s": [0.4] * point_count,
        "train/samples_per_s": [70.0] * point_count,
        "train/gpu_mem_gb": [40.0] * point_count,
        "system/gpu_0_memory_usage_percentage": [55.0] * point_count,
        "train/samples": list(steps * batch_size),
        "checkpoint_step": [float(step) for step in steps],
        "eval/eval_loss": [value * 1.1 for value in loss],
    }


def _snapshot(target: RunSnapshot, comparisons: list[RunSnapshot]) -> Snapshot:
    """Wrap runs in a snapshot.

    Args:
        target: Target run.
        comparisons: Comparison runs.

    Returns:
        The snapshot.
    """
    return Snapshot(
        schema_version=SCHEMA_VERSION,
        created_at="2026-10-18T00:00:00Z",
        tracking_uri="file:///tmp/mlruns",
        target=target,
        comparisons=comparisons,
        criteria=ComparisonCriteria(
            dataset_key="lerobot.dataset_repo_id",
            dataset_value="pask/test-dataset",
            experiments=list(DEFAULT_EXPERIMENTS),
            max_compare_runs=DEFAULT_MAX_COMPARE_RUNS,
            statuses=["FINISHED"],
        ),
    )


def _assert_png_files(paths: list[Path], out_dir: Path, expected_names: set[str]) -> None:
    """Check that exactly the expected PNG files were written and returned.

    Args:
        paths: Paths returned by the plot function.
        out_dir: Output directory.
        expected_names: Expected file names.
    """
    assert {path.name for path in paths} == expected_names
    assert {path.name for path in out_dir.iterdir()} == expected_names
    for path in paths:
        assert path.read_bytes()[: len(PNG_SIGNATURE)] == PNG_SIGNATURE


def test_plot_all_writes_every_figure(
    tmp_path: Path,
    make_run_snapshot: Callable[..., RunSnapshot],
    synthetic_curve: Callable[..., list[float]],
) -> None:
    target = make_run_snapshot(curves=_full_curves(synthetic_curve("converging"), 32))
    comparisons = [
        make_run_snapshot(
            run_id="0123456789abcdef",
            run_name="",
            curves=_full_curves(synthetic_curve("plateau", seed=1), 64),
            training_params={"BATCH_SIZE": 64},
        ),
        make_run_snapshot(
            run_id="run-no-samples",
            run_name="no-samples",
            curves={"train/loss": synthetic_curve("spiky", seed=2)},
        ),
    ]

    paths = plot_all(_snapshot(target, comparisons), tmp_path)

    _assert_png_files(paths, tmp_path, TARGET_FILES | COMPARE_FILES)


def test_plot_target_skips_figures_of_missing_metrics(
    tmp_path: Path,
    make_run_snapshot: Callable[..., RunSnapshot],
    synthetic_curve: Callable[..., list[float]],
) -> None:
    run = make_run_snapshot(
        curves={
            "train/loss": synthetic_curve("converging"),
            "train/samples_per_s": [70.0] * POINT_COUNT,
        }
    )

    paths = plot_target(run, tmp_path)

    _assert_png_files(paths, tmp_path, {"loss.png", "throughput.png"})


def test_plot_target_without_any_metric_writes_nothing(
    tmp_path: Path, make_run_snapshot: Callable[..., RunSnapshot]
) -> None:
    paths = plot_target(make_run_snapshot(), tmp_path)

    assert paths == []
    assert not tmp_path.exists() or list(tmp_path.iterdir()) == []


def test_plot_all_draws_curves_with_non_finite_values(
    tmp_path: Path,
    make_run_snapshot: Callable[..., RunSnapshot],
    synthetic_curve: Callable[..., list[float]],
) -> None:
    loss = synthetic_curve("diverging")
    loss[10] = math.nan
    loss[50] = math.inf
    loss[-1] = math.nan
    curves = _full_curves(loss, 32)
    curves["train/grad_norm"][20] = math.nan
    target = make_run_snapshot(curves=curves)
    comparison = make_run_snapshot(
        run_id="run-compare",
        run_name="compare",
        curves=_full_curves(synthetic_curve("converging", seed=3), 32),
    )

    paths = plot_all(_snapshot(target, [comparison]), tmp_path)

    _assert_png_files(paths, tmp_path, TARGET_FILES | COMPARE_FILES)


def test_plot_target_skips_metric_with_only_non_finite_values(
    tmp_path: Path, make_run_snapshot: Callable[..., RunSnapshot]
) -> None:
    run = make_run_snapshot(curves={"train/loss": [math.nan] * 10, "train/lr": [1e-4] * 10})

    paths = plot_target(run, tmp_path)

    _assert_png_files(paths, tmp_path, {"lr.png"})


def test_plot_all_without_comparisons_skips_compare_figures(
    tmp_path: Path,
    make_run_snapshot: Callable[..., RunSnapshot],
    synthetic_curve: Callable[..., list[float]],
) -> None:
    target = make_run_snapshot(curves=_full_curves(synthetic_curve("converging"), 32))

    paths = plot_all(_snapshot(target, []), tmp_path)

    _assert_png_files(paths, tmp_path, TARGET_FILES)


def test_plot_all_with_short_curve(
    tmp_path: Path,
    make_run_snapshot: Callable[..., RunSnapshot],
    synthetic_curve: Callable[..., list[float]],
) -> None:
    target = make_run_snapshot(curves=_full_curves(synthetic_curve("short"), 32))
    comparison = make_run_snapshot(
        run_id="run-compare",
        run_name="compare",
        curves=_full_curves(synthetic_curve("short", seed=4), 16),
        training_params={"BATCH_SIZE": 16},
    )

    paths = plot_all(_snapshot(target, [comparison]), tmp_path)

    _assert_png_files(paths, tmp_path, TARGET_FILES | COMPARE_FILES)


def _system_points(values: list[float]) -> list[MetricPoint]:
    """Build system metric points whose step is a sequence number, sampled every 30 seconds.

    Args:
        values: Metric values in logging order.

    Returns:
        Points with steps 0, 1, 2, ... starting at the run start time.
    """
    return [
        MetricPoint(
            step=index,
            value=value,
            timestamp=RUN_START_MS + index * SYSTEM_INTERVAL_MS,
        )
        for index, value in enumerate(values)
    ]


def test_gpu_memory_draws_system_metrics_against_elapsed_time(
    make_run_snapshot: Callable[..., RunSnapshot],
) -> None:
    run = make_run_snapshot(
        start_time=RUN_START_MS,
        curves={"train/gpu_mem_gb": [9.702] * POINT_COUNT},
        metrics={
            "system/gpu_0_memory_usage_percentage": _system_points(
                list(np.linspace(50.0, 55.0, SYSTEM_POINT_COUNT))
            )
        },
    )

    fig = _gpu_memory_figure(run)

    assert fig is not None
    memory_axes, percent_axes = fig.axes
    assert memory_axes.get_xlabel() == "step"
    assert memory_axes.lines[0].get_xdata()[-1] == POINT_COUNT * 100
    assert percent_axes.get_xlabel() == "elapsed time since run start (min)"
    elapsed_minutes = percent_axes.lines[0].get_xdata()
    expected_last_minute = (SYSTEM_POINT_COUNT - 1) * SYSTEM_INTERVAL_MS / 60_000
    assert elapsed_minutes[0] == pytest.approx(0.0)
    assert elapsed_minutes[-1] == pytest.approx(expected_last_minute)
    assert percent_axes.get_ylim() == pytest.approx((0.0, 100.0))
    plt.close(fig)


def test_gpu_memory_axis_of_constant_series_is_readable(
    make_run_snapshot: Callable[..., RunSnapshot],
) -> None:
    run = make_run_snapshot(curves={"train/gpu_mem_gb": [9.702] * POINT_COUNT})

    fig = _gpu_memory_figure(run)

    assert fig is not None
    (memory_axes,) = fig.axes
    formatter = memory_axes.yaxis.get_major_formatter()
    assert isinstance(formatter, ScalarFormatter)
    assert formatter.get_useOffset() is False
    lower, upper = memory_axes.get_ylim()
    assert upper - lower == pytest.approx(1.0)
    assert lower < 9.702 < upper
    fig.canvas.draw()
    assert formatter.get_offset() == ""
    plt.close(fig)


def test_gpu_memory_uses_first_sample_without_run_start(
    make_run_snapshot: Callable[..., RunSnapshot],
) -> None:
    run = make_run_snapshot(
        start_time=None,
        curves={"train/gpu_mem_gb": list(np.linspace(8.0, 20.0, POINT_COUNT))},
        metrics={"system/gpu_0_memory_usage_percentage": _system_points([60.0, 61.0, 62.0])},
    )

    fig = _gpu_memory_figure(run)

    assert fig is not None
    memory_axes, percent_axes = fig.axes
    assert percent_axes.get_xlabel() == "elapsed time since the first sample (min)"
    assert percent_axes.lines[0].get_xdata()[0] == pytest.approx(0.0)
    lower, upper = memory_axes.get_ylim()
    assert lower <= 8.0 and upper >= 20.0
    plt.close(fig)
