"""PNG figures of the learning curves of the target run and the runs it is compared with.

Axis labels, titles, and legends are English so that the figures render without CJK fonts.
"""

from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.axes import Axes
from matplotlib.figure import Figure
from matplotlib.ticker import ScalarFormatter

from pask_training_advisor.models import MetricPoint, RunSnapshot, Snapshot
from pask_training_advisor.smoothing import smooth

LOSS_FILE = "loss.png"
GRAD_NORM_FILE = "grad_norm.png"
LR_FILE = "lr.png"
THROUGHPUT_FILE = "throughput.png"
GPU_MEM_FILE = "gpu_mem.png"
COMPARE_LOSS_STEP_FILE = "compare_loss_step.png"
COMPARE_LOSS_SAMPLES_FILE = "compare_loss_samples.png"

LOSS_METRIC = "train/loss"
EVAL_LOSS_METRIC = "eval/eval_loss"
GRAD_NORM_METRIC = "train/grad_norm"
LR_METRIC = "train/lr"
DATALOADING_METRIC = "train/dataloading_s"
UPDATE_METRIC = "train/update_s"
SAMPLES_PER_S_METRIC = "train/samples_per_s"
GPU_MEM_METRIC = "train/gpu_mem_gb"
GPU_MEM_PERCENT_METRIC = "system/gpu_0_memory_usage_percentage"
SAMPLES_METRIC = "train/samples"
CHECKPOINT_METRIC = "checkpoint_step"

FIGURE_SIZE = (8.0, 4.5)
GPU_MEM_FIGURE_SIZE = (8.0, 8.0)
FIGURE_DPI = 120
RAW_LINE_ALPHA = 0.3
CHECKPOINT_LINE_ALPHA = 0.5
RUN_ID_LABEL_LENGTH = 8
MILLISECONDS_PER_MINUTE = 60_000
GPU_MEM_MIN_SPAN_GB = 1.0
PERCENT_AXIS_MAX = 100.0


def _finite_series(
    points: list[MetricPoint], is_positive_only: bool
) -> tuple[np.ndarray, np.ndarray]:
    """Convert metric points to step and value arrays without non-finite values.

    Args:
        points: Metric history.
        is_positive_only: Also drop values that are zero or negative, for log scale axes.

    Returns:
        Steps and values of the kept points, sorted by step.
    """
    ordered = sorted(points, key=lambda point: point.step)
    steps = np.array([point.step for point in ordered], dtype=float)
    values = np.array([point.value for point in ordered], dtype=float)
    keep = np.isfinite(values)
    if is_positive_only:
        keep &= values > 0
    return steps[keep], values[keep]


def _metric_series(
    run: RunSnapshot, metric_key: str, is_positive_only: bool
) -> tuple[np.ndarray, np.ndarray] | None:
    """Return the finite series of a metric, or None when the run has no usable value.

    Args:
        run: Run to read the metric from.
        metric_key: MLflow metric name.
        is_positive_only: Also drop values that are zero or negative, for log scale axes.

    Returns:
        Steps and values, or None when the metric is missing or has no finite value.
    """
    points = run.metrics.get(metric_key)
    if not points:
        return None
    steps, values = _finite_series(points, is_positive_only)
    if len(values) == 0:
        return None
    return steps, values


def _new_figure() -> tuple[Figure, Axes]:
    """Create a figure with one axes in the advisor's size.

    Returns:
        The figure and its axes.
    """
    return plt.subplots(figsize=FIGURE_SIZE)


def _save_figure(fig: Figure, path: Path) -> Path:
    """Save a figure as PNG and release it.

    Args:
        fig: Figure to save.
        path: Destination file.

    Returns:
        The destination file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(path, dpi=FIGURE_DPI)
    plt.close(fig)
    return path


def _merge_legends(axes: Axes, twin_axes: Axes) -> None:
    """Show the lines of an axes and its twin in one legend.

    Args:
        axes: Primary axes that holds the legend.
        twin_axes: Axes that shares the x axis.
    """
    handles, labels = axes.get_legend_handles_labels()
    twin_handles, twin_labels = twin_axes.get_legend_handles_labels()
    axes.legend(handles + twin_handles, labels + twin_labels, loc="best")


def _draw_checkpoints(axes: Axes, run: RunSnapshot) -> None:
    """Draw a dotted vertical line at every step that saved a checkpoint.

    Args:
        axes: Axes to draw on.
        run: Run whose ``checkpoint_step`` points give the steps.
    """
    checkpoint_steps = sorted({point.step for point in run.metrics.get(CHECKPOINT_METRIC, [])})
    for index, step in enumerate(checkpoint_steps):
        axes.axvline(
            step,
            color="gray",
            linestyle=":",
            alpha=CHECKPOINT_LINE_ALPHA,
            label="checkpoint" if index == 0 else None,
        )


def _plot_loss(run: RunSnapshot, out_dir: Path, smoothing_window: int) -> Path | None:
    """Draw ``train/loss`` raw and smoothed on a log axis, with eval loss and checkpoints.

    Args:
        run: Target run.
        out_dir: Output directory.
        smoothing_window: Window of the moving median in points.

    Returns:
        The written file, or None when the run has no usable ``train/loss``.
    """
    loss_series = _metric_series(run, LOSS_METRIC, is_positive_only=True)
    if loss_series is None:
        return None
    steps, values = loss_series
    fig, axes = _new_figure()
    axes.plot(steps, values, color="tab:blue", alpha=RAW_LINE_ALPHA, label="train/loss (raw)")
    axes.plot(
        steps,
        smooth(values, smoothing_window),
        color="tab:blue",
        label=f"train/loss (median of {smoothing_window})",
    )
    eval_series = _metric_series(run, EVAL_LOSS_METRIC, is_positive_only=True)
    if eval_series is not None:
        eval_steps, eval_values = eval_series
        axes.plot(eval_steps, eval_values, color="tab:orange", marker="o", label="eval/eval_loss")
    _draw_checkpoints(axes, run)
    axes.set_yscale("log")
    axes.set_xlabel("step")
    axes.set_ylabel("loss")
    axes.set_title("Training loss")
    axes.legend(loc="best")
    return _save_figure(fig, out_dir / LOSS_FILE)


def _plot_single_metric(
    run: RunSnapshot,
    metric_key: str,
    path: Path,
    title: str,
    is_log_scale: bool,
) -> Path | None:
    """Draw one metric against step.

    Args:
        run: Target run.
        metric_key: MLflow metric name.
        path: Destination file.
        title: Figure title.
        is_log_scale: Use a log scale y axis.

    Returns:
        The written file, or None when the run has no usable value of the metric.
    """
    series = _metric_series(run, metric_key, is_positive_only=is_log_scale)
    if series is None:
        return None
    steps, values = series
    fig, axes = _new_figure()
    axes.plot(steps, values, color="tab:blue", label=metric_key)
    if is_log_scale:
        axes.set_yscale("log")
    axes.set_xlabel("step")
    axes.set_ylabel(metric_key)
    axes.set_title(title)
    axes.legend(loc="best")
    return _save_figure(fig, path)


def _plot_throughput(run: RunSnapshot, out_dir: Path) -> Path | None:
    """Draw data loading and update time per step, and samples per second on the right axis.

    Args:
        run: Target run.
        out_dir: Output directory.

    Returns:
        The written file, or None when none of the three metrics has a usable value.
    """
    time_series = {
        metric_key: _metric_series(run, metric_key, is_positive_only=False)
        for metric_key in (DATALOADING_METRIC, UPDATE_METRIC)
    }
    rate_series = _metric_series(run, SAMPLES_PER_S_METRIC, is_positive_only=False)
    if rate_series is None and all(series is None for series in time_series.values()):
        return None
    fig, axes = _new_figure()
    for (metric_key, series), color in zip(
        time_series.items(), ("tab:blue", "tab:orange"), strict=True
    ):
        if series is not None:
            axes.plot(series[0], series[1], color=color, label=metric_key)
    axes.set_xlabel("step")
    axes.set_ylabel("seconds per step")
    axes.set_title("Throughput")
    rate_axes = axes.twinx()
    if rate_series is not None:
        rate_axes.plot(
            rate_series[0], rate_series[1], color="tab:green", label=SAMPLES_PER_S_METRIC
        )
    rate_axes.set_ylabel("samples per second")
    _merge_legends(axes, rate_axes)
    return _save_figure(fig, out_dir / THROUGHPUT_FILE)


def _metric_time_series(
    run: RunSnapshot, metric_key: str
) -> tuple[np.ndarray, np.ndarray, str] | None:
    """Return the finite series of a metric against the wall-clock time elapsed, in minutes.

    The time is measured from the start of the run when it is known, otherwise from the first
    point of the metric. System metrics need this axis because their ``step`` is a sequence
    number of the system metrics logger and does not match the training step.

    Args:
        run: Run to read the metric from.
        metric_key: MLflow metric name.

    Returns:
        Elapsed minutes, values, and the x axis label, or None when the metric is missing or
        has no finite value.
    """
    points = run.metrics.get(metric_key)
    if not points:
        return None
    ordered = sorted(points, key=lambda point: point.timestamp)
    timestamps = np.array([point.timestamp for point in ordered], dtype=float)
    values = np.array([point.value for point in ordered], dtype=float)
    keep = np.isfinite(values)
    if not keep.any():
        return None
    if run.start_time is None:
        origin = float(timestamps[keep][0])
        x_label = "elapsed time since the first sample (min)"
    else:
        origin = float(run.start_time)
        x_label = "elapsed time since run start (min)"
    elapsed_minutes = (timestamps[keep] - origin) / MILLISECONDS_PER_MINUTE
    return elapsed_minutes, values[keep], x_label


def _use_plain_y_tick_labels(axes: Axes) -> None:
    """Show plain y tick labels without an offset or scientific notation.

    A nearly constant series otherwise gets tick labels such as ``+9.702`` that cannot be read.

    Args:
        axes: Axes to format.
    """
    formatter = ScalarFormatter(useOffset=False)
    formatter.set_scientific(False)
    axes.yaxis.set_major_formatter(formatter)


def _ensure_min_y_span(axes: Axes, values: np.ndarray, min_span: float) -> None:
    """Widen the y limits to at least ``min_span`` around the middle of the values.

    The lower limit is kept at or above zero. Nothing changes when the values already spread
    over ``min_span`` or more.

    Args:
        axes: Axes to adjust.
        values: Finite values drawn on the axes.
        min_span: Smallest distance between the lower and the upper y limit.
    """
    lowest = float(np.min(values))
    highest = float(np.max(values))
    if highest - lowest >= min_span:
        return
    lower = max(0.0, (lowest + highest) / 2 - min_span / 2)
    axes.set_ylim(lower, lower + min_span)


def _gpu_memory_figure(run: RunSnapshot) -> Figure | None:
    """Build the GPU memory figure.

    The top axes draws ``train/gpu_mem_gb`` against the training step. When
    ``system/gpu_0_memory_usage_percentage`` is logged, a second axes below draws it against
    the elapsed wall-clock time, because the ``step`` of system metrics does not match the
    training step.

    Args:
        run: Target run.

    Returns:
        The figure, or None when the run has no usable ``train/gpu_mem_gb``.
    """
    memory_series = _metric_series(run, GPU_MEM_METRIC, is_positive_only=False)
    if memory_series is None:
        return None
    percent_series = _metric_time_series(run, GPU_MEM_PERCENT_METRIC)
    if percent_series is None:
        fig, memory_axes = _new_figure()
        _draw_gpu_memory(memory_axes, memory_series)
    else:
        fig, (memory_axes, percent_axes) = plt.subplots(2, 1, figsize=GPU_MEM_FIGURE_SIZE)
        _draw_gpu_memory(memory_axes, memory_series)
        _draw_gpu_memory_percent(percent_axes, percent_series)
    return fig


def _draw_gpu_memory(axes: Axes, memory_series: tuple[np.ndarray, np.ndarray]) -> None:
    """Draw ``train/gpu_mem_gb`` against the training step.

    Args:
        axes: Axes to draw on.
        memory_series: Steps and values of ``train/gpu_mem_gb``.
    """
    axes.plot(memory_series[0], memory_series[1], color="tab:blue", label=GPU_MEM_METRIC)
    axes.set_xlabel("step")
    axes.set_ylabel("GPU memory (GB)")
    axes.set_title("GPU memory allocated by training")
    _use_plain_y_tick_labels(axes)
    _ensure_min_y_span(axes, memory_series[1], GPU_MEM_MIN_SPAN_GB)
    axes.legend(loc="best")


def _draw_gpu_memory_percent(
    axes: Axes, percent_series: tuple[np.ndarray, np.ndarray, str]
) -> None:
    """Draw ``system/gpu_0_memory_usage_percentage`` against the elapsed wall-clock time.

    Args:
        axes: Axes to draw on.
        percent_series: Elapsed minutes, values and the x axis label.
    """
    elapsed_minutes, percent_values, x_label = percent_series
    axes.plot(elapsed_minutes, percent_values, color="tab:red", label=GPU_MEM_PERCENT_METRIC)
    axes.set_xlabel(x_label)
    axes.set_ylabel("GPU memory usage (%)")
    axes.set_title("GPU memory usage (system metrics)")
    axes.set_ylim(0.0, max(PERCENT_AXIS_MAX, float(np.max(percent_values))))
    _use_plain_y_tick_labels(axes)
    axes.legend(loc="best")


def _plot_gpu_memory(run: RunSnapshot, out_dir: Path) -> Path | None:
    """Draw the GPU memory figure.

    Args:
        run: Target run.
        out_dir: Output directory.

    Returns:
        The written file, or None when the run has no usable ``train/gpu_mem_gb``.
    """
    fig = _gpu_memory_figure(run)
    if fig is None:
        return None
    return _save_figure(fig, out_dir / GPU_MEM_FILE)


def plot_target(run: RunSnapshot, out_dir: Path, smoothing_window: int = 11) -> list[Path]:
    """Draw the learning curve figures of the target run.

    A figure is skipped when the metric it needs is missing or has no finite value.

    Args:
        run: Target run.
        out_dir: Output directory. Created when it does not exist.
        smoothing_window: Window of the moving median applied to ``train/loss``, in points.

    Returns:
        The written files.
    """
    written = [
        _plot_loss(run, out_dir, smoothing_window),
        _plot_single_metric(
            run, GRAD_NORM_METRIC, out_dir / GRAD_NORM_FILE, "Gradient norm", is_log_scale=True
        ),
        _plot_single_metric(run, LR_METRIC, out_dir / LR_FILE, "Learning rate", is_log_scale=False),
        _plot_throughput(run, out_dir),
        _plot_gpu_memory(run, out_dir),
    ]
    return [path for path in written if path is not None]


def _run_label(run: RunSnapshot, is_target: bool) -> str:
    """Build the legend label of a run: its name, or the start of its ID, and the batch size.

    Args:
        run: Run to label.
        is_target: Whether the run is the target run.

    Returns:
        The legend label.
    """
    name = run.run_name or run.run_id[:RUN_ID_LABEL_LENGTH]
    prefix = "target: " if is_target else ""
    batch_size = run.training_params.get("BATCH_SIZE")
    return f"{prefix}{name} (BATCH_SIZE={batch_size})"


def _loss_against_samples(run: RunSnapshot) -> tuple[np.ndarray, np.ndarray] | None:
    """Pair each finite positive ``train/loss`` value with ``train/samples`` of the same step.

    Args:
        run: Run to read the metrics from.

    Returns:
        Processed samples and loss values sorted by step, or None when no step has both.
    """
    samples_series = _metric_series(run, SAMPLES_METRIC, is_positive_only=False)
    loss_series = _metric_series(run, LOSS_METRIC, is_positive_only=True)
    if samples_series is None or loss_series is None:
        return None
    samples_by_step = dict(zip(samples_series[0], samples_series[1], strict=True))
    loss_steps, loss_values = loss_series
    has_samples = np.array([step in samples_by_step for step in loss_steps], dtype=bool)
    if not has_samples.any():
        return None
    samples = np.array([samples_by_step[step] for step in loss_steps[has_samples]], dtype=float)
    return samples, loss_values[has_samples]


def _plot_compare(
    runs: list[tuple[RunSnapshot, bool]],
    series_by_run: list[tuple[np.ndarray, np.ndarray] | None],
    path: Path,
    x_label: str,
    smoothing_window: int,
) -> Path | None:
    """Overlay the smoothed loss of several runs.

    Args:
        runs: Runs paired with whether each is the target run.
        series_by_run: x values and loss values of each run, or None to leave the run out.
        path: Destination file.
        x_label: Label of the x axis.
        smoothing_window: Window of the moving median in points.

    Returns:
        The written file, or None when no run has a series.
    """
    drawable = [
        (run, is_target, series)
        for (run, is_target), series in zip(runs, series_by_run, strict=True)
        if series is not None
    ]
    if not drawable:
        return None
    fig, axes = _new_figure()
    for run, is_target, (x_values, loss_values) in drawable:
        axes.plot(
            x_values,
            smooth(loss_values, smoothing_window),
            linewidth=2.0 if is_target else 1.2,
            label=_run_label(run, is_target),
        )
    axes.set_yscale("log")
    axes.set_xlabel(x_label)
    axes.set_ylabel(f"train/loss (median of {smoothing_window})")
    axes.set_title(f"Training loss by {x_label}")
    axes.legend(loc="best", fontsize="small")
    return _save_figure(fig, path)


def plot_comparison(snapshot: Snapshot, out_dir: Path, smoothing_window: int = 11) -> list[Path]:
    """Overlay the smoothed ``train/loss`` of the target and comparison runs.

    ``compare_loss_step.png`` uses step as x. ``compare_loss_samples.png`` uses ``train/samples``
    of the same step as x and leaves out runs without ``train/samples``. Nothing is drawn when
    there is no comparison run.

    Args:
        snapshot: Snapshot with the target and comparison runs.
        out_dir: Output directory. Created when it does not exist.
        smoothing_window: Window of the moving median in points.

    Returns:
        The written files.
    """
    if not snapshot.comparisons:
        return []
    runs = [(snapshot.target, True)] + [(run, False) for run in snapshot.comparisons]
    step_series = [
        _metric_series(run, LOSS_METRIC, is_positive_only=True) for run, _is_target in runs
    ]
    samples_series = [_loss_against_samples(run) for run, _is_target in runs]
    written = [
        _plot_compare(
            runs, step_series, out_dir / COMPARE_LOSS_STEP_FILE, "step", smoothing_window
        ),
        _plot_compare(
            runs,
            samples_series,
            out_dir / COMPARE_LOSS_SAMPLES_FILE,
            SAMPLES_METRIC,
            smoothing_window,
        ),
    ]
    return [path for path in written if path is not None]


def plot_all(snapshot: Snapshot, out_dir: Path, smoothing_window: int = 11) -> list[Path]:
    """Draw every figure of the snapshot.

    Args:
        snapshot: Snapshot with the target and comparison runs.
        out_dir: Output directory. Created when it does not exist.
        smoothing_window: Window of the moving median in points.

    Returns:
        The written files, target figures first.
    """
    return plot_target(snapshot.target, out_dir, smoothing_window) + plot_comparison(
        snapshot, out_dir, smoothing_window
    )
