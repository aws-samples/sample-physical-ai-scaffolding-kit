"""Learning curve features and diagnosis labels of MLflow runs.

The windows and thresholds come from ``config/diagnosis_thresholds.json`` so that they can be
tuned without changing the code. Every feature is a finite ``int`` / ``float`` or ``None`` so
that ``diagnosis.json`` stays strict JSON.
"""

import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from pask_training_advisor.models import (
    CONFIG_DIR,
    EVAL_METRIC_PREFIX,
    SCHEMA_VERSION,
    TRAINING_PARAM_KEYS,
    Diagnosis,
    MetricPoint,
    RunDiagnosis,
    RunSnapshot,
    SchemaError,
    Snapshot,
    read_json,
)
from pask_training_advisor.smoothing import smooth

THRESHOLDS_FILE = "diagnosis_thresholds.json"
COMMENTS_KEY = "_comments"

TRAIN_LOSS_METRIC = "train/loss"
EVAL_LOSS_METRIC = f"{EVAL_METRIC_PREFIX}eval_loss"
GRAD_NORM_METRIC = "train/grad_norm"
LR_METRIC = "train/lr"
EPOCHS_METRIC = "train/epochs"
DATALOADING_METRIC = "train/dataloading_s"
UPDATE_METRIC = "train/update_s"
GPU_MEM_GB_METRIC = "train/gpu_mem_gb"
GPU_MEMORY_PERCENT_METRIC = "system/gpu_0_memory_usage_percentage"
GPU_UTILIZATION_METRIC = "system/gpu_0_utilization_percentage"
SAMPLES_PER_S_METRIC = "train/samples_per_s"
SAMPLES_METRIC = "train/samples"
EPISODES_METRIC = "train/episodes"

STEPS_PER_SLOPE_UNIT = 1000
STOPPED_STATUSES = ("FAILED", "KILLED")
EVAL_NOT_RECORDED_FINDING = "eval loss is not recorded, so overfitting is not detected directly"

LABEL_INSUFFICIENT_DATA = "insufficient_data"
LABEL_FAILED_OR_KILLED = "failed_or_killed"
LABEL_DIVERGING = "diverging"
LABEL_UNSTABLE = "unstable"
LABEL_CONVERGING = "converging"
LABEL_PLATEAU = "plateau"
LABEL_OVERFIT_RISK = "overfit_risk"
LABEL_DATALOADER_BOUND = "dataloader_bound"
LABEL_GPU_MEMORY_HEADROOM = "gpu_memory_headroom"
LABEL_GPU_MEMORY_TIGHT = "gpu_memory_tight"

THRESHOLD_KEYS: dict[str, type | tuple[type, ...]] = {
    "schemaVersion": int,
    "headWindowFraction": (int, float),
    "tailWindowFraction": (int, float),
    "minWindowPoints": int,
    "smoothingWindow": int,
    "minPoints": int,
    "minCompletionRatio": (int, float),
    "divergingTailSlopePer1k": (int, float),
    "convergingTailImprovement": (int, float),
    "plateauTailImprovement": (int, float),
    "spikeFactor": (int, float),
    "maxSpikes": int,
    "maxTailCv": (int, float),
    "overfitEpochs": (int, float),
    "dataloaderBoundRatio": (int, float),
    "gpuMemoryHeadroomPercent": (int, float),
    "gpuMemoryTightPercent": (int, float),
}

Features = dict[str, float | int | None]


def load_thresholds(path: Path | None = None) -> dict[str, Any]:
    """Read the diagnosis windows and thresholds.

    Args:
        path: Thresholds file. ``CONFIG_DIR / "diagnosis_thresholds.json"`` when omitted.

    Returns:
        The thresholds keyed by their camelCase name, without ``_comments``.

    Raises:
        SchemaError: If a required key is missing, has the wrong type, or the schema version
            differs.
    """
    thresholds_path = CONFIG_DIR / THRESHOLDS_FILE if path is None else path
    data = read_json(thresholds_path)
    for key, expected_type in THRESHOLD_KEYS.items():
        if key not in data:
            raise SchemaError(f"{thresholds_path.name} is missing the required key '{key}'")
        value = data[key]
        if isinstance(value, bool) or not isinstance(value, expected_type):
            raise SchemaError(
                f"{thresholds_path.name}.{key} must be a number, got {type(value).__name__}"
            )
    if data["schemaVersion"] != SCHEMA_VERSION:
        raise SchemaError(
            f"{thresholds_path.name}.schemaVersion is {data['schemaVersion']}, "
            f"but this tool supports {SCHEMA_VERSION}"
        )
    return {key: value for key, value in data.items() if key != COMMENTS_KEY}


def _finite_series(points: list[MetricPoint]) -> tuple[np.ndarray, np.ndarray, int]:
    """Split a metric history into finite steps and values, sorted by step.

    Args:
        points: Metric history.

    Returns:
        Steps, values, and the number of NaN or infinite values that were dropped.
    """
    ordered = sorted(points, key=lambda point: point.step)
    steps = np.array([point.step for point in ordered], dtype=float)
    values = np.array([point.value for point in ordered], dtype=float)
    is_finite = np.isfinite(values)
    return steps[is_finite], values[is_finite], int(np.count_nonzero(~is_finite))


def _to_feature(value: float | np.floating | None) -> float | None:
    """Convert a computed number into a JSON safe feature value.

    Args:
        value: Number to convert.

    Returns:
        The value as a Python float, or None when it is missing or not finite.
    """
    if value is None:
        return None
    converted = float(value)
    return converted if math.isfinite(converted) else None


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    """Divide two optional numbers.

    Args:
        numerator: Dividend.
        denominator: Divisor.

    Returns:
        The quotient, or None when an operand is missing or the divisor is zero.
    """
    if numerator is None or denominator is None or denominator == 0:
        return None
    return _to_feature(numerator / denominator)


def _window_size(point_count: int, fraction: float, min_points: int) -> int:
    """Return the number of points in a head or tail window.

    Args:
        point_count: Number of finite points in the series.
        fraction: Fraction of the series covered by the window.
        min_points: Lower limit of the window size.

    Returns:
        The window size, at most ``point_count``.
    """
    return min(point_count, max(min_points, round(point_count * fraction)))


def _count_spikes(values: np.ndarray, window: int, spike_factor: float) -> int:
    """Count points that exceed ``spike_factor`` times the moving median.

    Args:
        values: Finite series.
        window: Number of points in the median window.
        spike_factor: Multiple of the moving median that makes a spike.

    Returns:
        The number of spikes. Zero for an empty series.
    """
    if values.size == 0:
        return 0
    smoothed = smooth(values, window)
    return int(np.count_nonzero(values > spike_factor * smoothed))


def _tail_slope_per_1k(steps: np.ndarray, values: np.ndarray) -> float | None:
    """Fit ``log(value)`` against the step and return the slope per 1000 steps.

    Args:
        steps: Steps of the tail window.
        values: Values of the tail window. Non positive values are dropped.

    Returns:
        The slope, or None when fewer than two positive points with distinct steps remain.
    """
    is_positive = values > 0
    positive_steps = steps[is_positive]
    if positive_steps.size < 2 or np.unique(positive_steps).size < 2:
        return None
    slope, _intercept = np.polyfit(positive_steps, np.log(values[is_positive]), 1)
    return _to_feature(slope * STEPS_PER_SLOPE_UNIT)


def _tail_improvement(values: np.ndarray) -> float | None:
    """Compare the medians of the first and second half of the tail window.

    Args:
        values: Values of the tail window.

    Returns:
        ``1 - second half median / first half median``, or None when the window has fewer than
        two points or the first half median is zero.
    """
    if values.size < 2:
        return None
    half = values.size // 2
    first_half_median = float(np.median(values[:half]))
    second_half_median = float(np.median(values[half:]))
    improvement_ratio = _ratio(second_half_median, first_half_median)
    return None if improvement_ratio is None else 1.0 - improvement_ratio


def _coefficient_of_variation(values: np.ndarray) -> float | None:
    """Return ``std / mean`` of a series.

    Args:
        values: Series.

    Returns:
        The coefficient of variation, or None when the series is empty or its mean is zero.
    """
    if values.size == 0:
        return None
    return _ratio(float(np.std(values)), float(np.mean(values)))


def _empty_loss_features() -> Features:
    """Return the loss curve features of a metric that has no finite point.

    Returns:
        Every loss curve feature set to None.
    """
    return {
        "initial_loss": None,
        "final_loss": None,
        "min_loss": None,
        "min_loss_step": None,
        "loss_reduction": None,
        "tail_slope_per_1k": None,
        "tail_improvement": None,
        "tail_cv": None,
    }


def _min_loss(
    steps: np.ndarray, values: np.ndarray, head_size: int, tail_size: int, window: int
) -> tuple[float, int]:
    """Return the lowest median level of a loss curve and the step where it is reached.

    The level is the lowest of the moving median, the head window median (``initial_loss``),
    and the tail window median (``final_loss``), so ``min_loss`` never exceeds either of them
    even when the series is short and the windows overlap. When the head or the tail median is
    the lowest, the step is that of the lowest moving median point inside that window.

    Args:
        steps: Finite steps sorted in ascending order. Must not be empty.
        values: Finite values matching ``steps``.
        head_size: Number of points in the head window.
        tail_size: Number of points in the tail window.
        window: Number of points in the moving median window.

    Returns:
        The lowest level and its step.
    """
    smoothed = smooth(values, window)
    point_count = values.size
    candidates = [
        (float(np.median(values[:head_size])), 0, head_size),
        (float(np.median(values[-tail_size:])), point_count - tail_size, point_count),
    ]
    min_index = int(np.argmin(smoothed))
    lowest_level = float(smoothed[min_index])
    for level, window_start, window_end in candidates:
        if level < lowest_level:
            lowest_level = level
            min_index = window_start + int(np.argmin(smoothed[window_start:window_end]))
    return lowest_level, int(steps[min_index])


def _loss_curve_features(steps: np.ndarray, values: np.ndarray, thresholds: dict) -> Features:
    """Compute the window based features of a loss curve.

    Args:
        steps: Finite steps sorted in ascending order.
        values: Finite values matching ``steps``.
        thresholds: Diagnosis thresholds.

    Returns:
        ``initial_loss``, ``final_loss``, ``min_loss``, ``min_loss_step``, ``loss_reduction``,
        ``tail_slope_per_1k``, ``tail_improvement``, and ``tail_cv``.
    """
    if values.size == 0:
        return _empty_loss_features()
    min_window_points = thresholds["minWindowPoints"]
    head_size = _window_size(values.size, thresholds["headWindowFraction"], min_window_points)
    tail_size = _window_size(values.size, thresholds["tailWindowFraction"], min_window_points)
    tail_steps = steps[-tail_size:]
    tail_values = values[-tail_size:]
    initial_loss = _to_feature(np.median(values[:head_size]))
    final_loss = _to_feature(np.median(tail_values))
    min_loss, min_loss_step = _min_loss(
        steps, values, head_size, tail_size, thresholds["smoothingWindow"]
    )
    loss_ratio = _ratio(final_loss, initial_loss)
    return {
        "initial_loss": initial_loss,
        "final_loss": final_loss,
        "min_loss": _to_feature(min_loss),
        "min_loss_step": min_loss_step,
        "loss_reduction": None if loss_ratio is None else 1.0 - loss_ratio,
        "tail_slope_per_1k": _tail_slope_per_1k(tail_steps, tail_values),
        "tail_improvement": _tail_improvement(tail_values),
        "tail_cv": _coefficient_of_variation(tail_values),
    }


def _metric_values(run: RunSnapshot, metric: str) -> np.ndarray:
    """Return the finite values of a metric sorted by step.

    Args:
        run: Run to read.
        metric: Metric name.

    Returns:
        The finite values. Empty when the metric was not recorded.
    """
    _steps, values, _nonfinite_count = _finite_series(run.metrics.get(metric, []))
    return values


def _last_value(run: RunSnapshot, metric: str) -> float | None:
    """Return the finite value of a metric at its largest step.

    Args:
        run: Run to read.
        metric: Metric name.

    Returns:
        The last finite value, or None when there is none.
    """
    values = _metric_values(run, metric)
    return _to_feature(values[-1]) if values.size else None


def _max_value(run: RunSnapshot, metric: str) -> float | None:
    """Return the largest finite value of a metric.

    Args:
        run: Run to read.
        metric: Metric name.

    Returns:
        The maximum, or None when there is no finite value.
    """
    values = _metric_values(run, metric)
    return _to_feature(np.max(values)) if values.size else None


def _median_value(run: RunSnapshot, metric: str) -> float | None:
    """Return the median of the finite values of a metric.

    Args:
        run: Run to read.
        metric: Metric name.

    Returns:
        The median, or None when there is no finite value.
    """
    values = _metric_values(run, metric)
    return _to_feature(np.median(values)) if values.size else None


def _dataloading_ratio(run: RunSnapshot) -> float | None:
    """Return the median of ``train/dataloading_s / train/update_s`` over shared steps.

    Args:
        run: Run to read.

    Returns:
        The median ratio, or None when no step has both values with a positive update time.
    """
    dataloading_steps, dataloading_values, _ = _finite_series(
        run.metrics.get(DATALOADING_METRIC, [])
    )
    update_steps, update_values, _ = _finite_series(run.metrics.get(UPDATE_METRIC, []))
    update_by_step = dict(zip(update_steps.tolist(), update_values.tolist(), strict=True))
    ratios = [
        dataloading / update_by_step[step]
        for step, dataloading in zip(
            dataloading_steps.tolist(), dataloading_values.tolist(), strict=True
        )
        if update_by_step.get(step, 0.0) > 0
    ]
    return _to_feature(np.median(ratios)) if ratios else None


def _resource_features(run: RunSnapshot) -> Features:
    """Compute the features that come from metrics other than the loss.

    Args:
        run: Run to read.

    Returns:
        Learning rate progress, epochs, data loading ratio, GPU memory and utilization,
        throughput, and dataset size estimates.
    """
    epochs = _last_value(run, EPOCHS_METRIC)
    final_samples = _last_value(run, SAMPLES_METRIC)
    return {
        "lr_progress": _ratio(_last_value(run, LR_METRIC), _max_value(run, LR_METRIC)),
        "epochs": epochs,
        "dataloading_ratio": _dataloading_ratio(run),
        "gpu_mem_gb_max": _max_value(run, GPU_MEM_GB_METRIC),
        "gpu_memory_percent_max": _max_value(run, GPU_MEMORY_PERCENT_METRIC),
        "gpu_utilization_median": _median_value(run, GPU_UTILIZATION_METRIC),
        "samples_per_s_median": _median_value(run, SAMPLES_PER_S_METRIC),
        "estimated_frames": _ratio(final_samples, epochs),
        "estimated_episodes": _ratio(_last_value(run, EPISODES_METRIC), epochs),
        "final_samples": final_samples,
    }


def compute_features(
    run: RunSnapshot, thresholds: dict, metric: str = TRAIN_LOSS_METRIC
) -> Features:
    """Compute the learning curve features of one run.

    NaN and infinite values are dropped before any computation and only counted. Features that
    cannot be computed because the metric is missing or has too few points are None.
    ``num_points`` counts every recorded point of ``metric``, including non finite ones.

    Args:
        run: Run to analyze.
        thresholds: Diagnosis thresholds from ``load_thresholds``.
        metric: Loss metric to analyze, ``train/loss`` or ``eval/eval_loss``.

    Returns:
        The features keyed by their snake_case name.
    """
    points = run.metrics.get(metric, [])
    steps, values, nonfinite_loss = _finite_series(points)
    _grad_norm_steps, grad_norm_values, nonfinite_grad_norm = _finite_series(
        run.metrics.get(GRAD_NORM_METRIC, [])
    )
    last_step = max(point.step for point in points) if points else None
    planned_steps = run.planned_steps
    completion_ratio = None
    if last_step is not None and planned_steps:
        completion_ratio = _to_feature(last_step / planned_steps)
    smoothing_window = thresholds["smoothingWindow"]
    spike_factor = thresholds["spikeFactor"]
    features: Features = {
        "num_points": len(points),
        "last_step": last_step,
        "planned_steps": planned_steps,
        "completion_ratio": completion_ratio,
        **_loss_curve_features(steps, values, thresholds),
        "nonfinite_loss": nonfinite_loss,
        "nonfinite_grad_norm": nonfinite_grad_norm,
        "loss_spikes": _count_spikes(values, smoothing_window, spike_factor),
        "grad_norm_spikes": _count_spikes(grad_norm_values, smoothing_window, spike_factor),
        **_resource_features(run),
    }
    return features


def _is_above(value: float | None, threshold: float) -> bool:
    """Check that an optional feature is known and greater than a threshold.

    Args:
        value: Feature value.
        threshold: Threshold.

    Returns:
        True when the value is not None and exceeds the threshold.
    """
    return value is not None and value > threshold


def _is_below(value: float | None, threshold: float) -> bool:
    """Check that an optional feature is known and less than a threshold.

    Args:
        value: Feature value.
        threshold: Threshold.

    Returns:
        True when the value is not None and is below the threshold.
    """
    return value is not None and value < threshold


def _format_number(value: float | None) -> str:
    """Format a feature for a finding sentence.

    Args:
        value: Feature value.

    Returns:
        Integers as they are, floats with four significant digits, or ``n/a`` for None.
    """
    if value is None:
        return "n/a"
    if isinstance(value, int):
        return str(value)
    return f"{value:.4g}"


def _classify_run_state(
    run: RunSnapshot, features: Features, thresholds: dict
) -> tuple[list[str], list[str]]:
    """Decide ``insufficient_data`` and ``failed_or_killed``.

    Args:
        run: Run being diagnosed.
        features: Features of the run.
        thresholds: Diagnosis thresholds.

    Returns:
        Labels and findings.
    """
    labels: list[str] = []
    findings: list[str] = []
    num_points = features["num_points"] or 0
    if num_points < thresholds["minPoints"]:
        labels.append(LABEL_INSUFFICIENT_DATA)
        findings.append(
            f"only {num_points} points are recorded, fewer than the {thresholds['minPoints']} "
            "needed to judge the curve shape"
        )
    completion_ratio = features["completion_ratio"]
    is_incomplete = _is_below(completion_ratio, thresholds["minCompletionRatio"])
    if run.status in STOPPED_STATUSES or is_incomplete:
        labels.append(LABEL_FAILED_OR_KILLED)
        findings.append(
            f"run status is {run.status} and it reached step "
            f"{_format_number(features['last_step'])} of "
            f"{_format_number(features['planned_steps'])} planned steps "
            f"(completion ratio {_format_number(completion_ratio)})"
        )
    return labels, findings


def _classify_curve_shape(features: Features, thresholds: dict) -> tuple[list[str], list[str]]:
    """Decide ``diverging``, ``unstable``, ``converging``, and ``plateau``.

    Args:
        features: Features of the run.
        thresholds: Diagnosis thresholds.

    Returns:
        Labels and findings.
    """
    labels: list[str] = []
    findings: list[str] = []
    slope = features["tail_slope_per_1k"]
    improvement = features["tail_improvement"]
    nonfinite_count = (features["nonfinite_loss"] or 0) + (features["nonfinite_grad_norm"] or 0)
    is_rising = _is_above(slope, thresholds["divergingTailSlopePer1k"]) and _is_below(
        improvement, 0.0
    )
    if nonfinite_count > 0 or is_rising:
        labels.append(LABEL_DIVERGING)
        findings.append(
            f"loss has {features['nonfinite_loss']} and grad norm has "
            f"{features['nonfinite_grad_norm']} non-finite values; tail slope of log(loss) is "
            f"{_format_number(slope)} per 1k steps with tail improvement "
            f"{_format_number(improvement)}"
        )
    max_spikes = thresholds["maxSpikes"]
    has_many_spikes = _is_above(features["loss_spikes"], max_spikes) or _is_above(
        features["grad_norm_spikes"], max_spikes
    )
    if has_many_spikes or _is_above(features["tail_cv"], thresholds["maxTailCv"]):
        labels.append(LABEL_UNSTABLE)
        findings.append(
            f"{features['loss_spikes']} loss spikes and {features['grad_norm_spikes']} grad norm "
            f"spikes exceed {thresholds['spikeFactor']}x the moving median; tail coefficient of "
            f"variation is {_format_number(features['tail_cv'])}"
        )
    if _is_below(slope, 0.0) and _is_above(improvement, thresholds["convergingTailImprovement"]):
        labels.append(LABEL_CONVERGING)
        findings.append(
            f"loss is still decreasing: tail improvement {_format_number(improvement)}, "
            f"tail slope {_format_number(slope)} per 1k steps, final loss "
            f"{_format_number(features['final_loss'])}"
        )
    if LABEL_DIVERGING not in labels and _is_below(
        improvement, thresholds["plateauTailImprovement"]
    ):
        labels.append(LABEL_PLATEAU)
        findings.append(
            f"loss has flattened: tail improvement {_format_number(improvement)} is below "
            f"{thresholds['plateauTailImprovement']}, final loss "
            f"{_format_number(features['final_loss'])}, learning rate progress "
            f"{_format_number(features['lr_progress'])}"
        )
    return labels, findings


def _classify_overfitting(
    run: RunSnapshot, features: Features, thresholds: dict, is_converging: bool
) -> tuple[list[str], list[str]]:
    """Decide ``overfit_risk`` from the epochs and, when recorded, the evaluation loss.

    Args:
        run: Run being diagnosed.
        features: Features of the run.
        thresholds: Diagnosis thresholds.
        is_converging: Whether the training loss is labeled ``converging``.

    Returns:
        Labels and findings.
    """
    findings: list[str] = []
    is_overfit_risk = False
    epochs = features["epochs"]
    if is_converging and _is_above(epochs, thresholds["overfitEpochs"]):
        is_overfit_risk = True
        findings.append(
            f"data was seen {_format_number(epochs)} epochs, more than "
            f"{thresholds['overfitEpochs']}, while the training loss keeps decreasing"
        )
    if EVAL_LOSS_METRIC in run.metrics:
        eval_features = compute_features(run, thresholds, metric=EVAL_LOSS_METRIC)
        eval_slope = eval_features["tail_slope_per_1k"]
        if is_converging and _is_above(eval_slope, 0.0):
            is_overfit_risk = True
            findings.append(
                f"eval loss rises at {_format_number(eval_slope)} per 1k steps while the "
                f"training loss keeps decreasing (final train loss "
                f"{_format_number(features['final_loss'])}, final eval loss "
                f"{_format_number(eval_features['final_loss'])})"
            )
    else:
        findings.append(EVAL_NOT_RECORDED_FINDING)
    return ([LABEL_OVERFIT_RISK] if is_overfit_risk else []), findings


def _classify_resources(features: Features, thresholds: dict) -> tuple[list[str], list[str]]:
    """Decide ``dataloader_bound``, ``gpu_memory_headroom``, and ``gpu_memory_tight``.

    GPU memory labels are not attached when the loss metric has no recorded point, because the
    system metrics of such a run do not show the memory the training needs.

    Args:
        features: Features of the run.
        thresholds: Diagnosis thresholds.

    Returns:
        Labels and findings.
    """
    labels: list[str] = []
    findings: list[str] = []
    dataloading_ratio = features["dataloading_ratio"]
    if _is_above(dataloading_ratio, thresholds["dataloaderBoundRatio"]):
        labels.append(LABEL_DATALOADER_BOUND)
        findings.append(
            f"median data loading time is {_format_number(dataloading_ratio)} of the update time, "
            f"above {thresholds['dataloaderBoundRatio']}"
        )
    memory_percent = features["gpu_memory_percent_max"]
    if not features["num_points"]:
        if memory_percent is not None:
            findings.append(
                f"no training point is recorded, so maximum GPU memory usage "
                f"{_format_number(memory_percent)}% is not used for GPU memory labels"
            )
        return labels, findings
    if _is_below(memory_percent, thresholds["gpuMemoryHeadroomPercent"]):
        labels.append(LABEL_GPU_MEMORY_HEADROOM)
        findings.append(
            f"maximum GPU memory usage is {_format_number(memory_percent)}%, below "
            f"{thresholds['gpuMemoryHeadroomPercent']}% "
            f"({_format_number(features['gpu_mem_gb_max'])} GB allocated by training)"
        )
    if _is_above(memory_percent, thresholds["gpuMemoryTightPercent"]):
        labels.append(LABEL_GPU_MEMORY_TIGHT)
        findings.append(
            f"maximum GPU memory usage is {_format_number(memory_percent)}%, above "
            f"{thresholds['gpuMemoryTightPercent']}% "
            f"({_format_number(features['gpu_mem_gb_max'])} GB allocated by training)"
        )
    return labels, findings


def classify(run: RunSnapshot, features: Features, thresholds: dict) -> tuple[list[str], list[str]]:
    """Attach every diagnosis label that applies to a run.

    Curve shape labels (``diverging``, ``unstable``, ``converging``, ``plateau``, and
    ``overfit_risk``) are not attached when the run is ``insufficient_data``.

    Args:
        run: Run being diagnosed.
        features: Features from ``compute_features`` on ``train/loss``.
        thresholds: Diagnosis thresholds.

    Returns:
        Labels and English findings that explain them with numbers.
    """
    labels, findings = _classify_run_state(run, features, thresholds)
    if LABEL_INSUFFICIENT_DATA not in labels:
        shape_labels, shape_findings = _classify_curve_shape(features, thresholds)
        labels.extend(shape_labels)
        findings.extend(shape_findings)
        overfit_labels, overfit_findings = _classify_overfitting(
            run, features, thresholds, LABEL_CONVERGING in shape_labels
        )
        labels.extend(overfit_labels)
        findings.extend(overfit_findings)
    elif EVAL_LOSS_METRIC not in run.metrics:
        findings.append(EVAL_NOT_RECORDED_FINDING)
    resource_labels, resource_findings = _classify_resources(features, thresholds)
    labels.extend(resource_labels)
    findings.extend(resource_findings)
    return labels, findings


def _param_diff(run: RunSnapshot, target: RunSnapshot) -> dict[str, dict[str, object]]:
    """List the training parameters whose values differ between a run and the target.

    Args:
        run: Comparison run.
        target: Target run.

    Returns:
        ``{key: {"target": value, "run": value}}`` for each differing parameter.
    """
    diff: dict[str, dict[str, object]] = {}
    for key in TRAINING_PARAM_KEYS:
        target_value = target.training_params.get(key)
        run_value = run.training_params.get(key)
        if target_value != run_value:
            diff[key] = {"target": target_value, "run": run_value}
    return diff


def diagnose_run(
    run: RunSnapshot, thresholds: dict, target: RunSnapshot | None = None
) -> RunDiagnosis:
    """Compute the features and labels of one run.

    Args:
        run: Run to diagnose.
        thresholds: Diagnosis thresholds.
        target: Target run to compare the training parameters with. None for the target itself.

    Returns:
        The run diagnosis.
    """
    features = compute_features(run, thresholds)
    labels, findings = classify(run, features, thresholds)
    eval_features = None
    if EVAL_LOSS_METRIC in run.metrics:
        eval_features = compute_features(run, thresholds, metric=EVAL_LOSS_METRIC)
    return RunDiagnosis(
        run_id=run.run_id,
        run_name=run.run_name,
        features=features,
        labels=labels,
        findings=findings,
        eval_features=eval_features,
        training_params=dict(run.training_params),
        param_diff_from_target={} if target is None else _param_diff(run, target),
    )


def diagnose_snapshot(snapshot: Snapshot, thresholds: dict) -> Diagnosis:
    """Diagnose the target run and every comparison run of a snapshot.

    Each run diagnosis carries ``final_samples`` in its features so that runs with a different
    ``BATCH_SIZE`` can be compared by the number of processed samples.

    Args:
        snapshot: Snapshot from ``fetch``.
        thresholds: Diagnosis thresholds.

    Returns:
        The diagnosis to write as ``diagnosis.json``.
    """
    return Diagnosis(
        schema_version=SCHEMA_VERSION,
        created_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        thresholds=dict(thresholds),
        target=diagnose_run(snapshot.target, thresholds),
        comparisons=[
            diagnose_run(run, thresholds, target=snapshot.target) for run in snapshot.comparisons
        ],
        datasets=None,
    )
