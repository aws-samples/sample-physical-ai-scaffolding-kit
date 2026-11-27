"""Tests for the advisor data models and their JSON representation."""

import json
import math
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from pask_training_advisor.models import (
    DEFAULT_OUTPUT_ROOT,
    REPO_ROOT,
    SCHEMA_VERSION,
    TEMPLATE_PATH,
    TRAINING_PARAM_KEYS,
    Alternative,
    Change,
    ComparisonCriteria,
    Diagnosis,
    MetricPoint,
    Proposal,
    RunDiagnosis,
    RunSnapshot,
    SchemaError,
    Snapshot,
    default_output_dir,
    load_diagnosis,
    load_proposal,
    load_snapshot,
    save_diagnosis,
    save_snapshot,
    utc_timestamp,
    write_json,
)

CREATED_AT = "2026-10-18T23:50:46Z"

PLAN_PROPOSAL = {
    "schemaVersion": 1,
    "baseRunId": "run-target",
    "parameters": {
        "POLICY_TYPE": "pi05",
        "PRETRAINED_PATH": "lerobot/pi05_base",
        "FINETUNE_MODE": "lora",
        "LORA_R": 16,
        "LORA_ALPHA": 16,
        "STEPS": 60000,
        "BATCH_SIZE": 4,
        "SAVE_FREQ": 5000,
        "LOG_FREQ": 100,
        "NUM_WORKERS": 4,
        "COMPILE_MODEL": False,
        "IMAGE_TAG": "latest",
    },
    "changes": [
        {
            "key": "STEPS",
            "from": 30000,
            "to": 60000,
            "reason": "The tail slope is negative and the loss is still falling.",
            "evidence": ["converging", "tail_slope_per_1k=-0.012"],
        }
    ],
    "expectedEffect": "Lower final loss.",
    "risks": ["Training takes twice as long."],
    "alternatives": [{"parameters": {"STEPS": "45000"}, "reason": "Shorter run."}],
}


def _make_criteria() -> ComparisonCriteria:
    """Return comparison criteria with the default experiments."""
    return ComparisonCriteria(
        dataset_key="lerobot.dataset_repo_id",
        dataset_value="pask/test-dataset",
        experiments=["vams-training", "lerobot-pi"],
        max_compare_runs=5,
        statuses=["FINISHED"],
    )


def _make_run_diagnosis(run_id: str, param_diff: dict[str, dict[str, object]]) -> RunDiagnosis:
    """Return a run diagnosis with a few features.

    Args:
        run_id: MLflow run ID.
        param_diff: Differences from the target run.
    """
    return RunDiagnosis(
        run_id=run_id,
        run_name=f"{run_id}-name",
        features={"final_loss": 0.05, "point_count": 200, "tail_slope_per_1k": None},
        labels=["converging"],
        findings=["The loss is still falling."],
        eval_features=None,
        training_params={"STEPS": 30000, "COMPILE_MODEL": False},
        param_diff_from_target=param_diff,
    )


def test_metric_point_round_trip() -> None:
    point = MetricPoint(step=100, value=0.25, timestamp=1_790_000_000_000)

    assert MetricPoint.from_dict(point.to_dict()) == point


@pytest.mark.parametrize(
    ("value", "text"),
    [(math.inf, "inf"), (-math.inf, "-inf")],
)
def test_metric_point_infinity_round_trip(value: float, text: str) -> None:
    point = MetricPoint(step=1, value=value, timestamp=0)

    encoded = point.to_dict()

    assert encoded["value"] == text
    assert MetricPoint.from_dict(encoded).value == value


def test_metric_point_nan_round_trip_through_file(tmp_path: Path) -> None:
    path = tmp_path / "point.json"

    write_json(path, MetricPoint(step=1, value=math.nan, timestamp=0).to_dict())
    text = path.read_text(encoding="utf-8")

    assert "NaN" not in text
    assert json.loads(text)["value"] == "nan"
    assert math.isnan(MetricPoint.from_dict(json.loads(text)).value)


def test_metric_point_rejects_unknown_text() -> None:
    with pytest.raises(SchemaError, match="nan, inf, -inf"):
        MetricPoint.from_dict({"step": 1, "value": "infinity", "timestamp": 0})


def test_metric_point_rejects_bool_step() -> None:
    with pytest.raises(SchemaError, match="step"):
        MetricPoint.from_dict({"step": True, "value": 1.0, "timestamp": 0})


def test_run_snapshot_round_trip(
    make_run_snapshot: Callable[..., RunSnapshot],
    synthetic_curve: Callable[..., list[float]],
) -> None:
    run = make_run_snapshot(curves={"train/loss": synthetic_curve("converging")})

    restored = RunSnapshot.from_dict(run.to_dict())

    assert restored == run
    assert set(restored.training_params) == set(TRAINING_PARAM_KEYS)
    assert len(restored.metrics["train/loss"]) == 200


def test_run_snapshot_uses_camel_case_keys(make_run_snapshot: Callable[..., RunSnapshot]) -> None:
    encoded = make_run_snapshot().to_dict()

    assert {"runId", "trainingParams", "plannedSteps", "lrScheduleParams"} <= set(encoded)


def test_run_snapshot_defaults_follow_template(
    make_run_snapshot: Callable[..., RunSnapshot],
) -> None:
    template = json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))
    defaults = {entry["tagKey"]: entry["default"] for entry in template["tagSchema"]}

    assert make_run_snapshot().training_params == defaults


def test_run_snapshot_missing_key_raises(make_run_snapshot: Callable[..., RunSnapshot]) -> None:
    encoded = make_run_snapshot().to_dict()
    del encoded["status"]

    with pytest.raises(SchemaError, match="status"):
        RunSnapshot.from_dict(encoded)


def test_run_snapshot_param_type_mismatch_raises(
    make_run_snapshot: Callable[..., RunSnapshot],
) -> None:
    encoded = make_run_snapshot().to_dict()
    encoded["params"]["steps"] = 30000

    with pytest.raises(SchemaError, match="params"):
        RunSnapshot.from_dict(encoded)


def test_comparison_criteria_round_trip() -> None:
    criteria = _make_criteria()

    assert ComparisonCriteria.from_dict(criteria.to_dict()) == criteria


def test_snapshot_round_trip_through_file(
    tmp_path: Path,
    make_run_snapshot: Callable[..., RunSnapshot],
    synthetic_curve: Callable[..., list[float]],
) -> None:
    diverging = synthetic_curve("diverging")
    diverging[-1] = math.inf
    snapshot = Snapshot(
        schema_version=SCHEMA_VERSION,
        created_at=CREATED_AT,
        tracking_uri="file:///tmp/mlruns",
        target=make_run_snapshot(curves={"train/loss": diverging}),
        comparisons=[make_run_snapshot(run_id="run-other", run_name="other")],
        criteria=_make_criteria(),
    )
    path = tmp_path / "nested" / "snapshot.json"

    save_snapshot(snapshot, path)

    assert load_snapshot(path) == snapshot


def test_snapshot_schema_version_mismatch_raises(
    make_run_snapshot: Callable[..., RunSnapshot],
) -> None:
    snapshot = Snapshot(
        schema_version=SCHEMA_VERSION,
        created_at=CREATED_AT,
        tracking_uri="file:///tmp/mlruns",
        target=make_run_snapshot(),
        comparisons=[],
        criteria=_make_criteria(),
    )
    encoded = snapshot.to_dict()
    encoded["schemaVersion"] = SCHEMA_VERSION + 1

    with pytest.raises(SchemaError, match="schemaVersion"):
        Snapshot.from_dict(encoded)


def test_run_diagnosis_round_trip() -> None:
    diagnosis = _make_run_diagnosis("run-other", {"STEPS": {"target": 30000, "run": 60000}})

    assert RunDiagnosis.from_dict(diagnosis.to_dict()) == diagnosis


def test_run_diagnosis_eval_features_round_trip() -> None:
    base = _make_run_diagnosis("run-target", {})
    encoded = base.to_dict()
    encoded["evalFeatures"] = {"final_loss": 0.07}

    assert RunDiagnosis.from_dict(encoded).eval_features == {"final_loss": 0.07}


def test_diagnosis_round_trip_through_file(tmp_path: Path) -> None:
    diagnosis = Diagnosis(
        schema_version=SCHEMA_VERSION,
        created_at=CREATED_AT,
        thresholds={"minPoints": 20},
        target=_make_run_diagnosis("run-target", {}),
        comparisons=[_make_run_diagnosis("run-other", {"STEPS": {"target": 30000, "run": 1}})],
        datasets=None,
    )
    path = tmp_path / "diagnosis.json"

    save_diagnosis(diagnosis, path)

    assert load_diagnosis(path) == diagnosis


def test_diagnosis_missing_schema_version_raises() -> None:
    with pytest.raises(SchemaError, match="schemaVersion"):
        Diagnosis.from_dict({"createdAt": CREATED_AT})


def test_change_and_alternative_round_trip() -> None:
    change = Change(key="COMPILE_MODEL", from_value=False, to_value=True, reason="r", evidence=[])
    alternative = Alternative(parameters={"STEPS": 45000}, reason="Shorter run.")

    assert change.to_dict()["from"] is False
    assert Change.from_dict(change.to_dict()) == change
    assert Alternative.from_dict(alternative.to_dict()) == alternative


def test_load_proposal_keeps_values_as_written(tmp_path: Path) -> None:
    path = tmp_path / "proposal.json"
    path.write_text(json.dumps(PLAN_PROPOSAL), encoding="utf-8")

    proposal = load_proposal(path)

    assert proposal.base_run_id == "run-target"
    assert proposal.parameters["STEPS"] == 60000
    assert proposal.parameters["COMPILE_MODEL"] is False
    assert proposal.changes[0].from_value == 30000
    assert proposal.changes[0].evidence == ["converging", "tail_slope_per_1k=-0.012"]
    assert proposal.alternatives[0].parameters == {"STEPS": "45000"}
    assert proposal.to_dict() == PLAN_PROPOSAL


def test_proposal_schema_version_mismatch_raises() -> None:
    with pytest.raises(SchemaError, match="schemaVersion"):
        Proposal.from_dict({**PLAN_PROPOSAL, "schemaVersion": 2})


def test_proposal_change_without_from_raises() -> None:
    change = {key: value for key, value in PLAN_PROPOSAL["changes"][0].items() if key != "from"}

    with pytest.raises(SchemaError, match="'from'"):
        Proposal.from_dict({**PLAN_PROPOSAL, "changes": [change]})


def test_write_json_rejects_nan(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        write_json(tmp_path / "bad.json", {"value": math.nan})


def test_utc_timestamp_converts_to_utc() -> None:
    moment = datetime(2026, 10, 19, 8, 50, 46, tzinfo=timezone(timedelta(hours=9)))

    assert utc_timestamp(moment) == "20261018T235046Z"


def test_utc_timestamp_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError):
        utc_timestamp(datetime(2026, 10, 18, 23, 50, 46))


def test_default_output_dir_format() -> None:
    moment = datetime(2026, 10, 18, 23, 50, 46, tzinfo=UTC)

    output_dir = default_output_dir(moment)

    assert output_dir == DEFAULT_OUTPUT_ROOT / "20261018T235046Z"
    assert output_dir.parent == REPO_ROOT / "samples/lerobot-openpi/outputs/advisor"


def test_default_output_dir_uses_current_time() -> None:
    assert len(default_output_dir().name) == len("20261018T235046Z")


def test_repo_root_holds_template() -> None:
    assert TEMPLATE_PATH.is_file()
