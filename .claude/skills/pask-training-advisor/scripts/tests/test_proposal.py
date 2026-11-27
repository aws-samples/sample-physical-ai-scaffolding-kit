"""Tests for the validation of proposal.json and its conversion to vams_parameters.json."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from pask_training_advisor.constraints import (
    TemplateSchema,
    load_operational_constraints,
    load_template_schema,
)
from pask_training_advisor.models import (
    DEFAULT_EXPERIMENTS,
    DEFAULT_MAX_COMPARE_RUNS,
    SCHEMA_VERSION,
    TRAINING_PARAM_KEYS,
    ComparisonCriteria,
    Proposal,
    RunSnapshot,
    Snapshot,
)
from pask_training_advisor.proposal import (
    ValidationResult,
    to_vams_parameters,
    validate_proposal,
    write_vams_parameters,
)

TEMPLATE_ID = "pask-lerobot-openpi-default"
PIPELINE_ID = "pask-lerobot-openpi"
BASE_RUN_ID = "run-target"


@pytest.fixture(scope="module")
def schema() -> TemplateSchema:
    return load_template_schema()


@pytest.fixture(scope="module")
def operational() -> dict[str, Any]:
    return load_operational_constraints()


@pytest.fixture
def snapshot(make_run_snapshot: Callable[..., RunSnapshot]) -> Snapshot:
    return make_snapshot(make_run_snapshot(run_id=BASE_RUN_ID))


def make_snapshot(target: RunSnapshot) -> Snapshot:
    """Wrap a target run in a snapshot without comparison runs."""
    return Snapshot(
        schema_version=SCHEMA_VERSION,
        created_at="2026-10-18T23:50:46Z",
        tracking_uri="file:///tmp/mlruns",
        target=target,
        comparisons=[],
        criteria=ComparisonCriteria(
            dataset_key="lerobot.dataset_repo_id",
            dataset_value="pask/test-dataset",
            experiments=list(DEFAULT_EXPERIMENTS),
            max_compare_runs=DEFAULT_MAX_COMPARE_RUNS,
            statuses=["FINISHED"],
        ),
    )


def make_proposal(
    base_params: dict[str, Any],
    changed: dict[str, Any],
    **overrides: Any,
) -> Proposal:
    """Build a proposal that changes ``changed`` from ``base_params`` and lists each change.

    ``overrides`` replaces top level keys of the proposal JSON, such as ``changes``.
    """
    data: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "baseRunId": BASE_RUN_ID,
        "parameters": {**base_params, **changed},
        "changes": [
            {
                "key": key,
                "from": base_params[key],
                "to": value,
                "reason": "tail slope is still negative",
                "evidence": ["converging"],
            }
            for key, value in changed.items()
        ],
        "expectedEffect": "lower final loss",
        "risks": ["longer training time"],
        "alternatives": [],
    }
    data.update(overrides)
    return Proposal.from_dict(data)


def rules_of(result: ValidationResult) -> list[tuple[str | None, str]]:
    """Return ``(key, rule)`` of each violation."""
    return [(violation.key, violation.rule) for violation in result.violations]


def test_valid_proposal_builds_vams_parameters(
    snapshot: Snapshot, schema: TemplateSchema, operational: dict[str, Any], tmp_path: Path
) -> None:
    base_params = snapshot.target.training_params
    proposal = make_proposal(base_params, {"STEPS": 60000})

    result = validate_proposal(proposal, snapshot, schema, operational)

    assert result.ok, result.to_dict()
    assert result.to_dict() == {"ok": True, "violations": [], "warnings": []}

    data = to_vams_parameters(
        result.normalized_parameters, template_id=schema.template_id, pipeline_id=PIPELINE_ID
    )
    path = tmp_path / "vams_parameters.json"
    write_vams_parameters(path, data)

    written = json.loads(path.read_text(encoding="utf-8"))
    assert list(written) == [PIPELINE_ID]
    assert written[PIPELINE_ID]["templateId"] == TEMPLATE_ID
    tags = written[PIPELINE_ID]["templateTags"]
    assert [tag["key"] for tag in tags] == list(TRAINING_PARAM_KEYS)
    values = {tag["key"]: tag["value"] for tag in tags}
    assert values == {**base_params, "STEPS": 60000}
    assert values["STEPS"] == 60000
    assert values["COMPILE_MODEL"] is False
    assert values["LORA_R"] == 16


def test_compile_model_false_string_is_normalized(
    snapshot: Snapshot, schema: TemplateSchema, operational: dict[str, Any]
) -> None:
    base_params = snapshot.target.training_params
    proposal = make_proposal(base_params, {}, parameters={**base_params, "COMPILE_MODEL": "false"})

    result = validate_proposal(proposal, snapshot, schema, operational)

    assert result.ok, result.to_dict()
    assert result.normalized_parameters["COMPILE_MODEL"] is False
    data = to_vams_parameters(
        result.normalized_parameters, template_id=schema.template_id, pipeline_id=PIPELINE_ID
    )
    tags = {tag["key"]: tag["value"] for tag in data[PIPELINE_ID]["templateTags"]}
    assert tags["COMPILE_MODEL"] is False


def test_previous_run_values_as_strings_are_normalized(
    make_run_snapshot: Callable[..., RunSnapshot],
    schema: TemplateSchema,
    operational: dict[str, Any],
) -> None:
    string_snapshot = make_snapshot(
        make_run_snapshot(
            run_id=BASE_RUN_ID, training_params={"STEPS": "30000", "COMPILE_MODEL": "false"}
        )
    )
    base_params = {**string_snapshot.target.training_params, "STEPS": 30000, "COMPILE_MODEL": False}
    proposal = make_proposal(base_params, {"STEPS": 60000})

    result = validate_proposal(proposal, string_snapshot, schema, operational)

    assert result.ok, result.to_dict()


def test_change_from_mismatch_is_reported(
    snapshot: Snapshot, schema: TemplateSchema, operational: dict[str, Any]
) -> None:
    base_params = snapshot.target.training_params
    proposal = make_proposal(
        base_params,
        {},
        parameters={**base_params, "STEPS": 60000},
        changes=[
            {"key": "STEPS", "from": 20000, "to": 60000, "reason": "r", "evidence": []},
        ],
    )

    result = validate_proposal(proposal, snapshot, schema, operational)

    assert not result.ok
    assert rules_of(result) == [("STEPS", "change_from_mismatch")]


def test_change_to_mismatch_is_reported(
    snapshot: Snapshot, schema: TemplateSchema, operational: dict[str, Any]
) -> None:
    base_params = snapshot.target.training_params
    proposal = make_proposal(
        base_params,
        {},
        parameters={**base_params, "STEPS": 60000},
        changes=[
            {"key": "STEPS", "from": 30000, "to": 50000, "reason": "r", "evidence": []},
        ],
    )

    result = validate_proposal(proposal, snapshot, schema, operational)

    assert rules_of(result) == [("STEPS", "change_to_mismatch")]


def test_unlisted_change_is_reported(
    snapshot: Snapshot, schema: TemplateSchema, operational: dict[str, Any]
) -> None:
    base_params = snapshot.target.training_params
    proposal = make_proposal(base_params, {"STEPS": 60000})
    unlisted = make_proposal(
        base_params,
        {},
        parameters={**proposal.parameters, "BATCH_SIZE": 8},
        changes=[change.to_dict() for change in proposal.changes],
    )

    result = validate_proposal(unlisted, snapshot, schema, operational)

    assert rules_of(result) == [("BATCH_SIZE", "unlisted_change")]


def test_duplicate_and_unknown_change_keys_are_reported(
    snapshot: Snapshot, schema: TemplateSchema, operational: dict[str, Any]
) -> None:
    base_params = snapshot.target.training_params
    proposal = make_proposal(base_params, {"STEPS": 60000})
    change = proposal.changes[0].to_dict()
    broken = make_proposal(
        base_params,
        {},
        parameters=proposal.parameters,
        changes=[
            change,
            change,
            {"key": "LEARNING_RATE", "from": 1, "to": 2, "reason": "r", "evidence": []},
        ],
    )

    result = validate_proposal(broken, snapshot, schema, operational)

    assert rules_of(result) == [
        ("STEPS", "duplicate_change"),
        ("LEARNING_RATE", "change_key"),
    ]


def test_listed_change_without_new_value_is_a_warning(
    snapshot: Snapshot, schema: TemplateSchema, operational: dict[str, Any]
) -> None:
    base_params = snapshot.target.training_params
    proposal = make_proposal(base_params, {"STEPS": base_params["STEPS"]})

    result = validate_proposal(proposal, snapshot, schema, operational)

    assert result.ok
    assert result.warnings == ["changes[0] lists STEPS, but the value does not change"]


def test_base_run_id_mismatch_is_reported(
    snapshot: Snapshot, schema: TemplateSchema, operational: dict[str, Any]
) -> None:
    proposal = make_proposal(snapshot.target.training_params, {}, baseRunId="run-other")

    result = validate_proposal(proposal, snapshot, schema, operational)

    assert rules_of(result) == [(None, "base_run_id")]


def test_format_and_operational_violations_are_combined(
    snapshot: Snapshot, schema: TemplateSchema, operational: dict[str, Any]
) -> None:
    base_params = snapshot.target.training_params
    parameters = {key: value for key, value in base_params.items() if key != "LOG_FREQ"}
    parameters.update({"RESUME": "true", "NUM_WORKERS": 16, "STEPS": "abc"})
    proposal = make_proposal(
        base_params,
        {},
        parameters=parameters,
        changes=[{"key": "NUM_WORKERS", "from": 4, "to": 16, "reason": "r", "evidence": []}],
    )

    result = validate_proposal(proposal, snapshot, schema, operational)

    assert not result.ok
    assert sorted(rules_of(result)) == sorted(
        [
            ("LOG_FREQ", "missing_key"),
            ("RESUME", "denied_key"),
            ("STEPS", "type"),
            ("NUM_WORKERS", "range"),
        ]
    )
    assert result.to_dict()["violations"][0].keys() == {"key", "rule", "message"}


def test_invalid_previous_run_value_is_reported(
    make_run_snapshot: Callable[..., RunSnapshot],
    schema: TemplateSchema,
    operational: dict[str, Any],
) -> None:
    broken_snapshot = make_snapshot(
        make_run_snapshot(run_id=BASE_RUN_ID, training_params={"STEPS": "many"})
    )
    base_params = {**broken_snapshot.target.training_params, "STEPS": 30000}
    proposal = make_proposal(base_params, {})

    result = validate_proposal(proposal, broken_snapshot, schema, operational)

    assert rules_of(result) == [("STEPS", "base_value")]


def test_unknown_previous_values_are_not_compared(
    make_run_snapshot: Callable[..., RunSnapshot],
    schema: TemplateSchema,
    operational: dict[str, Any],
) -> None:
    partial_snapshot = make_snapshot(
        make_run_snapshot(run_id=BASE_RUN_ID, training_params={"IMAGE_TAG": None})
    )
    parameters = {**partial_snapshot.target.training_params, "IMAGE_TAG": "v2"}
    proposal = make_proposal(parameters, {})

    result = validate_proposal(proposal, partial_snapshot, schema, operational)

    assert result.ok, result.to_dict()


def test_alternative_violations_are_warnings(
    snapshot: Snapshot, schema: TemplateSchema, operational: dict[str, Any]
) -> None:
    base_params = snapshot.target.training_params
    alternative = {**base_params, "BATCH_SIZE": 0, "STEPS": 999999}
    proposal = make_proposal(
        base_params, {}, alternatives=[{"parameters": alternative, "reason": "smaller batch"}]
    )

    result = validate_proposal(proposal, snapshot, schema, operational)

    assert result.ok
    assert len(result.warnings) == 3
    assert result.warnings[0].startswith("alternatives[0]: pattern: BATCH_SIZE=")
    assert result.warnings[1].startswith("alternatives[0]: range: STEPS=999999")
    assert result.warnings[2].startswith("alternatives[0]: max_change_factor: STEPS ")


def test_alternative_gets_the_gpu_memory_warning(
    snapshot: Snapshot, schema: TemplateSchema, operational: dict[str, Any]
) -> None:
    base_params = snapshot.target.training_params
    alternative = {**base_params, "FINETUNE_MODE": "expert_only"}
    proposal = make_proposal(
        base_params,
        {"STEPS": 60000},
        alternatives=[{"parameters": alternative, "reason": "train the action expert"}],
    )

    result = validate_proposal(proposal, snapshot, schema, operational)

    assert result.ok, result.to_dict()
    assert len(result.warnings) == 1
    assert result.warnings[0].startswith("alternatives[0]: FINETUNE_MODE=expert_only: ")
    assert "24 GB" in result.warnings[0]


def test_gpu_memory_warning_does_not_block(
    snapshot: Snapshot, schema: TemplateSchema, operational: dict[str, Any]
) -> None:
    proposal = make_proposal(snapshot.target.training_params, {"FINETUNE_MODE": "full"})

    result = validate_proposal(proposal, snapshot, schema, operational)

    assert result.ok
    assert len(result.warnings) == 1
    assert "24 GB" in result.warnings[0]


def test_to_vams_parameters_rejects_empty_pipeline_id(
    snapshot: Snapshot, schema: TemplateSchema
) -> None:
    with pytest.raises(ValueError, match="pipeline_id"):
        to_vams_parameters(
            dict(snapshot.target.training_params), template_id=schema.template_id, pipeline_id=""
        )


def test_to_vams_parameters_rejects_missing_parameter(
    snapshot: Snapshot, schema: TemplateSchema
) -> None:
    normalized = {
        key: value for key, value in snapshot.target.training_params.items() if key != "STEPS"
    }

    with pytest.raises(ValueError, match="STEPS"):
        to_vams_parameters(normalized, template_id=schema.template_id, pipeline_id=PIPELINE_ID)
