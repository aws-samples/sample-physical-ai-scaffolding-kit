"""Tests for the learning curve features and diagnosis labels."""

import json
import math
from collections.abc import Callable
from pathlib import Path

import pytest

from pask_training_advisor.diagnose import (
    EVAL_LOSS_METRIC,
    EVAL_NOT_RECORDED_FINDING,
    THRESHOLD_KEYS,
    classify,
    compute_features,
    diagnose_run,
    diagnose_snapshot,
    load_thresholds,
)
from pask_training_advisor.models import (
    CONFIG_DIR,
    SCHEMA_VERSION,
    ComparisonCriteria,
    Diagnosis,
    RunSnapshot,
    SchemaError,
    Snapshot,
    save_diagnosis,
)

CURVE_POINTS = 200
CURVE_LAST_STEP = CURVE_POINTS * 100
FEATURE_KEYS = (
    "num_points",
    "last_step",
    "planned_steps",
    "completion_ratio",
    "initial_loss",
    "final_loss",
    "min_loss",
    "min_loss_step",
    "loss_reduction",
    "tail_slope_per_1k",
    "tail_improvement",
    "tail_cv",
    "nonfinite_loss",
    "nonfinite_grad_norm",
    "loss_spikes",
    "grad_norm_spikes",
    "lr_progress",
    "epochs",
    "dataloading_ratio",
    "gpu_mem_gb_max",
    "gpu_memory_percent_max",
    "gpu_utilization_median",
    "samples_per_s_median",
    "estimated_frames",
    "estimated_episodes",
    "final_samples",
)
CURVE_SHAPE_LABELS = {"diverging", "unstable", "converging", "plateau", "overfit_risk"}

RunFactory = Callable[..., RunSnapshot]
CurveBuilder = Callable[..., list[float]]


@pytest.fixture
def thresholds() -> dict:
    """Return the thresholds shipped in ``config/diagnosis_thresholds.json``."""
    return load_thresholds()


def constant_curve(value: float) -> list[float]:
    """Return a flat metric curve with the same number of points as the loss curves.

    Args:
        value: Value of every point.

    Returns:
        ``CURVE_POINTS`` copies of the value.
    """
    return [value] * CURVE_POINTS


def linear_curve(start: float, end: float) -> list[float]:
    """Return a metric curve that changes linearly.

    Args:
        start: First value.
        end: Last value.

    Returns:
        ``CURVE_POINTS`` values from ``start`` to ``end``.
    """
    return [start + (end - start) * index / (CURVE_POINTS - 1) for index in range(CURVE_POINTS)]


def make_loss_run(
    make_run_snapshot: RunFactory,
    loss: list[float],
    extra_curves: dict[str, list[float]] | None = None,
    **overrides: object,
) -> RunSnapshot:
    """Build a finished run whose planned steps match the end of the loss curve.

    Args:
        make_run_snapshot: Run snapshot factory fixture.
        loss: Values of ``train/loss``.
        extra_curves: Other metric curves.
        **overrides: ``RunSnapshot`` fields to override.

    Returns:
        The run snapshot.
    """
    curves = {"train/loss": loss, **(extra_curves or {})}
    fields: dict[str, object] = {"planned_steps": len(loss) * 100, **overrides}
    return make_run_snapshot(curves=curves, **fields)


def diagnose_labels(run: RunSnapshot, thresholds: dict) -> tuple[list[str], list[str]]:
    """Compute the features of a run and classify it.

    Args:
        run: Run to diagnose.
        thresholds: Diagnosis thresholds.

    Returns:
        Labels and findings.
    """
    return classify(run, compute_features(run, thresholds), thresholds)


def write_thresholds(path: Path, data: dict) -> Path:
    """Write a thresholds file for a test.

    Args:
        path: Destination file.
        data: JSON object to write.

    Returns:
        The path.
    """
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class TestLoadThresholds:
    def test_default_file_has_every_key_and_comments(self, thresholds: dict) -> None:
        raw = json.loads((CONFIG_DIR / "diagnosis_thresholds.json").read_text(encoding="utf-8"))

        assert set(THRESHOLD_KEYS) <= set(thresholds)
        assert "_comments" not in thresholds
        assert set(THRESHOLD_KEYS) - {"schemaVersion"} <= set(raw["_comments"])

    def test_missing_key_is_an_error(self, thresholds: dict, tmp_path: Path) -> None:
        broken = {key: value for key, value in thresholds.items() if key != "minPoints"}
        path = write_thresholds(tmp_path / "thresholds.json", broken)

        with pytest.raises(SchemaError, match="minPoints"):
            load_thresholds(path)

    def test_non_numeric_value_is_an_error(self, thresholds: dict, tmp_path: Path) -> None:
        path = write_thresholds(tmp_path / "thresholds.json", {**thresholds, "maxSpikes": "3"})

        with pytest.raises(SchemaError, match="maxSpikes"):
            load_thresholds(path)

    def test_other_schema_version_is_an_error(self, thresholds: dict, tmp_path: Path) -> None:
        path = write_thresholds(tmp_path / "thresholds.json", {**thresholds, "schemaVersion": 2})

        with pytest.raises(SchemaError, match="schemaVersion"):
            load_thresholds(path)


class TestComputeFeatures:
    def test_converging_curve_features(
        self, make_run_snapshot: RunFactory, synthetic_curve: CurveBuilder, thresholds: dict
    ) -> None:
        run = make_loss_run(make_run_snapshot, synthetic_curve("converging"))

        features = compute_features(run, thresholds)

        assert tuple(features) == FEATURE_KEYS
        assert features["num_points"] == CURVE_POINTS
        assert features["last_step"] == CURVE_LAST_STEP
        assert features["completion_ratio"] == pytest.approx(1.0)
        assert features["initial_loss"] > features["final_loss"]
        assert 0.0 < features["loss_reduction"] < 1.0
        assert features["min_loss"] <= features["final_loss"]
        assert features["tail_slope_per_1k"] < 0.0
        assert features["tail_improvement"] > 0.0

    def test_missing_metric_gives_none(
        self, make_run_snapshot: RunFactory, thresholds: dict
    ) -> None:
        run = make_run_snapshot(curves={})

        features = compute_features(run, thresholds)

        assert features["num_points"] == 0
        assert features["last_step"] is None
        assert features["completion_ratio"] is None
        assert features["final_loss"] is None
        assert features["tail_slope_per_1k"] is None
        assert features["lr_progress"] is None
        assert features["loss_spikes"] == 0

    def test_nonfinite_values_are_dropped_and_counted(
        self, make_run_snapshot: RunFactory, synthetic_curve: CurveBuilder, thresholds: dict
    ) -> None:
        loss = synthetic_curve("converging")
        loss[150] = math.nan
        loss[160] = math.inf
        grad_norm = constant_curve(1.0)
        grad_norm[170] = math.nan
        run = make_loss_run(make_run_snapshot, loss, {"train/grad_norm": grad_norm})

        features = compute_features(run, thresholds)

        assert features["nonfinite_loss"] == 2
        assert features["nonfinite_grad_norm"] == 1
        assert features["num_points"] == CURVE_POINTS
        assert all(value is None or math.isfinite(value) for value in features.values()), features

    def test_resource_features(
        self, make_run_snapshot: RunFactory, synthetic_curve: CurveBuilder, thresholds: dict
    ) -> None:
        extra_curves = {
            "train/lr": linear_curve(1e-4, 1e-5),
            "train/epochs": linear_curve(0.1, 20.0),
            "train/samples": linear_curve(4.0, 80_000.0),
            "train/episodes": linear_curve(0.1, 1_000.0),
            "train/dataloading_s": constant_curve(0.1),
            "train/update_s": constant_curve(0.4),
            "train/gpu_mem_gb": linear_curve(10.0, 20.0),
            "system/gpu_0_memory_usage_percentage": linear_curve(30.0, 70.0),
            "system/gpu_0_utilization_percentage": constant_curve(85.0),
            "train/samples_per_s": constant_curve(12.0),
        }
        run = make_loss_run(make_run_snapshot, synthetic_curve("converging"), extra_curves)

        features = compute_features(run, thresholds)

        assert features["lr_progress"] == pytest.approx(0.1)
        assert features["epochs"] == pytest.approx(20.0)
        assert features["dataloading_ratio"] == pytest.approx(0.25)
        assert features["gpu_mem_gb_max"] == pytest.approx(20.0)
        assert features["gpu_memory_percent_max"] == pytest.approx(70.0)
        assert features["gpu_utilization_median"] == pytest.approx(85.0)
        assert features["samples_per_s_median"] == pytest.approx(12.0)
        assert features["final_samples"] == pytest.approx(80_000.0)
        assert features["estimated_frames"] == pytest.approx(4_000.0)
        assert features["estimated_episodes"] == pytest.approx(50.0)

    def test_spikes_are_counted(
        self, make_run_snapshot: RunFactory, synthetic_curve: CurveBuilder, thresholds: dict
    ) -> None:
        grad_norm = constant_curve(1.0)
        for index in (40, 80, 120, 160, 190):
            grad_norm[index] = 10.0
        run = make_loss_run(
            make_run_snapshot, synthetic_curve("spiky"), {"train/grad_norm": grad_norm}
        )

        features = compute_features(run, thresholds)

        assert features["loss_spikes"] == 8
        assert features["grad_norm_spikes"] == 5

    @pytest.mark.parametrize("kind", ["converging", "plateau", "diverging", "spiky", "short"])
    @pytest.mark.parametrize("n_points", [40, 200])
    def test_min_loss_does_not_exceed_initial_or_final_loss(
        self,
        make_run_snapshot: RunFactory,
        synthetic_curve: CurveBuilder,
        thresholds: dict,
        kind: str,
        n_points: int,
    ) -> None:
        for seed in range(10):
            for point_count in (4, 30, n_points):
                loss = synthetic_curve(kind, n_points=n_points, seed=seed)[:point_count]
                run = make_loss_run(make_run_snapshot, loss)

                features = compute_features(run, thresholds)

                assert features["min_loss"] <= features["initial_loss"], features
                assert features["min_loss"] <= features["final_loss"], features

    def test_four_point_run_keeps_min_loss_below_initial_loss(
        self, make_run_snapshot: RunFactory, thresholds: dict
    ) -> None:
        run = make_loss_run(make_run_snapshot, [0.2511, 0.30, 0.2543, 0.26])

        features = compute_features(run, thresholds)

        assert features["initial_loss"] == pytest.approx(0.25715)
        assert features["min_loss"] == pytest.approx(0.2543)
        assert features["min_loss_step"] == 200

    def test_min_loss_falls_back_to_the_tail_median(
        self, make_run_snapshot: RunFactory, thresholds: dict
    ) -> None:
        loss = [5.0] * 45 + [1.0] * 5 + [5.0] * 4 + [9.0] * 3 + [1.0] * 3

        features = compute_features(make_loss_run(make_run_snapshot, loss), thresholds)

        assert features["final_loss"] == pytest.approx(1.0)
        assert features["min_loss"] == pytest.approx(1.0)
        assert features["min_loss_step"] == 4600


class TestClassifyCurveShape:
    def test_converging(
        self, make_run_snapshot: RunFactory, synthetic_curve: CurveBuilder, thresholds: dict
    ) -> None:
        run = make_loss_run(make_run_snapshot, synthetic_curve("converging"))

        labels, findings = diagnose_labels(run, thresholds)

        assert labels == ["converging"]
        assert EVAL_NOT_RECORDED_FINDING in findings

    def test_plateau(
        self, make_run_snapshot: RunFactory, synthetic_curve: CurveBuilder, thresholds: dict
    ) -> None:
        run = make_loss_run(make_run_snapshot, synthetic_curve("plateau"))

        labels, _findings = diagnose_labels(run, thresholds)

        assert labels == ["plateau"]

    def test_diverging(
        self, make_run_snapshot: RunFactory, synthetic_curve: CurveBuilder, thresholds: dict
    ) -> None:
        run = make_loss_run(make_run_snapshot, synthetic_curve("diverging"))

        labels, _findings = diagnose_labels(run, thresholds)

        assert "diverging" in labels
        assert "converging" not in labels
        assert "plateau" not in labels

    def test_nan_in_loss_is_diverging(
        self, make_run_snapshot: RunFactory, synthetic_curve: CurveBuilder, thresholds: dict
    ) -> None:
        loss = synthetic_curve("converging")
        loss[120] = math.nan
        run = make_loss_run(make_run_snapshot, loss)

        labels, findings = diagnose_labels(run, thresholds)

        assert "diverging" in labels
        assert any("1 and grad norm has 0 non-finite" in finding for finding in findings)

    def test_spiky_is_unstable(
        self, make_run_snapshot: RunFactory, synthetic_curve: CurveBuilder, thresholds: dict
    ) -> None:
        run = make_loss_run(make_run_snapshot, synthetic_curve("spiky"))

        labels, _findings = diagnose_labels(run, thresholds)

        assert "unstable" in labels

    def test_short_is_insufficient_without_shape_labels(
        self, make_run_snapshot: RunFactory, synthetic_curve: CurveBuilder, thresholds: dict
    ) -> None:
        loss = synthetic_curve("short")
        loss[-1] = math.nan
        run = make_loss_run(make_run_snapshot, loss)

        labels, findings = diagnose_labels(run, thresholds)

        assert labels == ["insufficient_data"]
        assert not CURVE_SHAPE_LABELS & set(labels)
        assert any("only 5 points" in finding for finding in findings)


class TestClassifyRunState:
    @pytest.mark.parametrize("status", ["KILLED", "FAILED"])
    def test_stopped_status_is_failed_or_killed(
        self,
        make_run_snapshot: RunFactory,
        synthetic_curve: CurveBuilder,
        thresholds: dict,
        status: str,
    ) -> None:
        run = make_loss_run(make_run_snapshot, synthetic_curve("converging"), status=status)

        labels, findings = diagnose_labels(run, thresholds)

        assert "failed_or_killed" in labels
        assert any(f"status is {status}" in finding for finding in findings)

    def test_low_completion_ratio_is_failed_or_killed(
        self, make_run_snapshot: RunFactory, synthetic_curve: CurveBuilder, thresholds: dict
    ) -> None:
        run = make_loss_run(
            make_run_snapshot, synthetic_curve("converging"), planned_steps=CURVE_LAST_STEP * 2
        )

        features = compute_features(run, thresholds)
        labels, findings = classify(run, features, thresholds)

        assert features["completion_ratio"] == pytest.approx(0.5)
        assert "failed_or_killed" in labels
        assert any("completion ratio 0.5" in finding for finding in findings)

    def test_unknown_planned_steps_is_not_failed(
        self, make_run_snapshot: RunFactory, synthetic_curve: CurveBuilder, thresholds: dict
    ) -> None:
        run = make_loss_run(make_run_snapshot, synthetic_curve("converging"), planned_steps=None)

        labels, _findings = diagnose_labels(run, thresholds)

        assert "failed_or_killed" not in labels


class TestClassifyOverfitting:
    def test_many_epochs_while_converging_is_overfit_risk(
        self, make_run_snapshot: RunFactory, synthetic_curve: CurveBuilder, thresholds: dict
    ) -> None:
        run = make_loss_run(
            make_run_snapshot,
            synthetic_curve("converging"),
            {"train/epochs": linear_curve(0.5, 40.0)},
        )

        labels, findings = diagnose_labels(run, thresholds)

        assert "overfit_risk" in labels
        assert EVAL_NOT_RECORDED_FINDING in findings

    def test_few_epochs_is_not_overfit_risk(
        self, make_run_snapshot: RunFactory, synthetic_curve: CurveBuilder, thresholds: dict
    ) -> None:
        run = make_loss_run(
            make_run_snapshot,
            synthetic_curve("converging"),
            {"train/epochs": linear_curve(0.5, 10.0)},
        )

        labels, _findings = diagnose_labels(run, thresholds)

        assert "overfit_risk" not in labels

    def test_rising_eval_loss_is_overfit_risk(
        self, make_run_snapshot: RunFactory, synthetic_curve: CurveBuilder, thresholds: dict
    ) -> None:
        run = make_loss_run(
            make_run_snapshot,
            synthetic_curve("converging"),
            {EVAL_LOSS_METRIC: linear_curve(0.5, 1.0)},
        )

        diagnosis = diagnose_run(run, thresholds)

        assert "overfit_risk" in diagnosis.labels
        assert EVAL_NOT_RECORDED_FINDING not in diagnosis.findings
        assert diagnosis.eval_features is not None
        assert diagnosis.eval_features["tail_slope_per_1k"] > 0.0
        assert diagnosis.eval_features["final_loss"] == pytest.approx(1.0, rel=0.1)

    def test_falling_eval_loss_is_not_overfit_risk(
        self, make_run_snapshot: RunFactory, synthetic_curve: CurveBuilder, thresholds: dict
    ) -> None:
        run = make_loss_run(
            make_run_snapshot,
            synthetic_curve("converging"),
            {EVAL_LOSS_METRIC: synthetic_curve("converging", start=1.2, end=0.1)},
        )

        diagnosis = diagnose_run(run, thresholds)

        assert "overfit_risk" not in diagnosis.labels
        assert diagnosis.eval_features is not None

    def test_no_eval_gives_no_eval_features(
        self, make_run_snapshot: RunFactory, synthetic_curve: CurveBuilder, thresholds: dict
    ) -> None:
        run = make_loss_run(make_run_snapshot, synthetic_curve("converging"))

        diagnosis = diagnose_run(run, thresholds)

        assert diagnosis.eval_features is None
        assert EVAL_NOT_RECORDED_FINDING in diagnosis.findings


class TestClassifyResources:
    def test_dataloader_bound(
        self, make_run_snapshot: RunFactory, synthetic_curve: CurveBuilder, thresholds: dict
    ) -> None:
        run = make_loss_run(
            make_run_snapshot,
            synthetic_curve("converging"),
            {"train/dataloading_s": constant_curve(0.3), "train/update_s": constant_curve(0.4)},
        )

        labels, findings = diagnose_labels(run, thresholds)

        assert "dataloader_bound" in labels
        assert any("0.75 of the update time" in finding for finding in findings)

    def test_fast_dataloading_is_not_dataloader_bound(
        self, make_run_snapshot: RunFactory, synthetic_curve: CurveBuilder, thresholds: dict
    ) -> None:
        run = make_loss_run(
            make_run_snapshot,
            synthetic_curve("converging"),
            {"train/dataloading_s": constant_curve(0.05), "train/update_s": constant_curve(0.4)},
        )

        labels, _findings = diagnose_labels(run, thresholds)

        assert "dataloader_bound" not in labels

    @pytest.mark.parametrize(
        ("memory_percent", "expected_label", "unexpected_label"),
        [
            (40.0, "gpu_memory_headroom", "gpu_memory_tight"),
            (95.0, "gpu_memory_tight", "gpu_memory_headroom"),
        ],
    )
    def test_gpu_memory_labels(
        self,
        make_run_snapshot: RunFactory,
        synthetic_curve: CurveBuilder,
        thresholds: dict,
        memory_percent: float,
        expected_label: str,
        unexpected_label: str,
    ) -> None:
        run = make_loss_run(
            make_run_snapshot,
            synthetic_curve("converging"),
            {"system/gpu_0_memory_usage_percentage": linear_curve(10.0, memory_percent)},
        )

        labels, findings = diagnose_labels(run, thresholds)

        assert expected_label in labels
        assert unexpected_label not in labels
        assert any(f"{memory_percent:.4g}%" in finding for finding in findings)

    def test_moderate_gpu_memory_has_no_memory_label(
        self, make_run_snapshot: RunFactory, synthetic_curve: CurveBuilder, thresholds: dict
    ) -> None:
        run = make_loss_run(
            make_run_snapshot,
            synthetic_curve("converging"),
            {"system/gpu_0_memory_usage_percentage": constant_curve(75.0)},
        )

        labels, _findings = diagnose_labels(run, thresholds)

        assert "gpu_memory_headroom" not in labels
        assert "gpu_memory_tight" not in labels

    def test_resource_labels_are_kept_for_insufficient_data(
        self, make_run_snapshot: RunFactory, synthetic_curve: CurveBuilder, thresholds: dict
    ) -> None:
        run = make_loss_run(
            make_run_snapshot,
            synthetic_curve("short"),
            {"system/gpu_0_memory_usage_percentage": [40.0] * 5},
        )

        labels, _findings = diagnose_labels(run, thresholds)

        assert labels == ["insufficient_data", "gpu_memory_headroom"]

    @pytest.mark.parametrize("memory_percent", [2.0, 95.0])
    def test_failed_run_without_training_points_has_no_gpu_memory_label(
        self, make_run_snapshot: RunFactory, thresholds: dict, memory_percent: float
    ) -> None:
        run = make_run_snapshot(
            status="FAILED",
            curves={"system/gpu_0_memory_usage_percentage": [memory_percent] * 5},
        )

        labels, findings = diagnose_labels(run, thresholds)

        assert labels == ["insufficient_data", "failed_or_killed"]
        assert any("no training point is recorded" in finding for finding in findings)


class TestDiagnoseSnapshot:
    def test_param_diff_from_target(
        self, make_run_snapshot: RunFactory, synthetic_curve: CurveBuilder, thresholds: dict
    ) -> None:
        target = make_loss_run(make_run_snapshot, synthetic_curve("converging"))
        comparison = make_loss_run(
            make_run_snapshot,
            synthetic_curve("plateau"),
            run_id="run-compare",
            training_params={"BATCH_SIZE": 8, "LORA_R": 32},
        )

        diagnosis = diagnose_run(comparison, thresholds, target=target)

        assert diagnosis.param_diff_from_target == {
            "LORA_R": {"target": 16, "run": 32},
            "BATCH_SIZE": {"target": 4, "run": 8},
        }
        assert diagnose_run(target, thresholds).param_diff_from_target == {}

    def test_snapshot_diagnoses_target_and_comparisons(
        self,
        make_run_snapshot: RunFactory,
        synthetic_curve: CurveBuilder,
        thresholds: dict,
        tmp_path: Path,
    ) -> None:
        samples = {"train/samples": linear_curve(4.0, 80_000.0)}
        target = make_loss_run(make_run_snapshot, synthetic_curve("converging"), samples)
        comparison = make_loss_run(
            make_run_snapshot,
            synthetic_curve("plateau"),
            {"train/samples": linear_curve(8.0, 160_000.0)},
            run_id="run-compare",
            training_params={"BATCH_SIZE": 8},
        )
        snapshot = Snapshot(
            schema_version=SCHEMA_VERSION,
            created_at="2026-10-18T23:50:46Z",
            tracking_uri="file:///tmp/mlruns",
            target=target,
            comparisons=[comparison],
            criteria=ComparisonCriteria(
                dataset_key="lerobot.dataset_repo_id",
                dataset_value="pask/test-dataset",
                experiments=["vams-training", "lerobot-pi"],
                max_compare_runs=5,
                statuses=["FINISHED"],
            ),
        )

        diagnosis = diagnose_snapshot(snapshot, thresholds)
        path = tmp_path / "diagnosis.json"
        save_diagnosis(diagnosis, path)
        restored = Diagnosis.from_dict(json.loads(path.read_text(encoding="utf-8")))

        assert diagnosis.schema_version == SCHEMA_VERSION
        assert diagnosis.created_at.endswith("Z")
        assert diagnosis.thresholds == thresholds
        assert diagnosis.target.labels == ["converging"]
        assert diagnosis.target.features["final_samples"] == pytest.approx(80_000.0)
        assert [run.run_id for run in diagnosis.comparisons] == ["run-compare"]
        assert diagnosis.comparisons[0].labels == ["plateau"]
        assert diagnosis.comparisons[0].features["final_samples"] == pytest.approx(160_000.0)
        assert diagnosis.comparisons[0].param_diff_from_target == {
            "BATCH_SIZE": {"target": 4, "run": 8}
        }
        assert restored == diagnosis

    def test_snapshot_with_nan_loss_is_written_as_strict_json(
        self, make_run_snapshot: RunFactory, thresholds: dict, tmp_path: Path
    ) -> None:
        loss = [math.nan] * CURVE_POINTS
        target = make_loss_run(make_run_snapshot, loss)
        snapshot = Snapshot(
            schema_version=SCHEMA_VERSION,
            created_at="2026-10-18T23:50:46Z",
            tracking_uri="file:///tmp/mlruns",
            target=target,
            comparisons=[],
            criteria=ComparisonCriteria(
                dataset_key=None,
                dataset_value=None,
                experiments=["vams-training"],
                max_compare_runs=5,
                statuses=["FINISHED"],
            ),
        )

        diagnosis = diagnose_snapshot(snapshot, thresholds)
        save_diagnosis(diagnosis, tmp_path / "diagnosis.json")

        assert "diverging" in diagnosis.target.labels
        assert diagnosis.target.features["final_loss"] is None
        assert diagnosis.target.features["nonfinite_loss"] == CURVE_POINTS
