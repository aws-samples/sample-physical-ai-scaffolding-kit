"""Tests for the format and operational constraints of training parameters."""

import json
from pathlib import Path
from typing import Any

import pytest

from pask_training_advisor import constraints
from pask_training_advisor.constraints import (
    TagSpec,
    TemplateSchema,
    Violation,
    check_allow_list_consistency,
    check_format,
    check_operational,
    load_operational_constraints,
    load_template_schema,
    normalize_value,
    to_poller_string,
)
from pask_training_advisor.models import TEMPLATE_PATH, TRAINING_PARAM_KEYS, SchemaError

TEMPLATE_ID = "pask-lerobot-openpi-default"


@pytest.fixture(scope="module")
def schema() -> TemplateSchema:
    return load_template_schema()


@pytest.fixture(scope="module")
def operational() -> dict[str, Any]:
    return load_operational_constraints()


def template_defaults(schema: TemplateSchema) -> dict[str, int | bool | str]:
    """Return the template default of every training parameter."""
    return {key: schema.tags[key].default for key in TRAINING_PARAM_KEYS}


def rules_of(violations: list[Violation]) -> list[tuple[str | None, str]]:
    """Return ``(key, rule)`` of each violation."""
    return [(violation.key, violation.rule) for violation in violations]


def test_allow_list_keys_match_training_param_keys() -> None:
    assert set(constraints.ALLOWED_TRAINING_PARAMS) == set(TRAINING_PARAM_KEYS)
    check_allow_list_consistency()


def test_allow_list_mismatch_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    reduced = dict(constraints.ALLOWED_TRAINING_PARAMS)
    del reduced["IMAGE_TAG"]
    monkeypatch.setattr(constraints, "ALLOWED_TRAINING_PARAMS", reduced)

    with pytest.raises(RuntimeError, match="IMAGE_TAG"):
        check_allow_list_consistency()


def test_load_template_schema_reads_real_template(schema: TemplateSchema) -> None:
    assert schema.template_id == TEMPLATE_ID
    assert set(TRAINING_PARAM_KEYS) <= schema.tags.keys()
    assert schema.tags["POLICY_TYPE"] == TagSpec("POLICY_TYPE", "enum", ("pi05", "pi0"), "pi05")
    assert schema.tags["FINETUNE_MODE"].enum_values == ("lora", "expert_only", "full")
    assert schema.tags["STEPS"].type == "integer"
    assert schema.tags["COMPILE_MODEL"] == TagSpec("COMPILE_MODEL", "boolean", None, False)
    assert schema.tags["IMAGE_TAG"].type == "string"


def test_load_template_schema_rejects_missing_training_param(tmp_path: Path) -> None:
    template = json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))
    template["tagSchema"] = [
        entry for entry in template["tagSchema"] if entry["tagKey"] != "LOG_FREQ"
    ]
    path = tmp_path / "template.json"
    path.write_text(json.dumps(template), encoding="utf-8")

    with pytest.raises(SchemaError, match="LOG_FREQ"):
        load_template_schema(path)


def test_load_operational_constraints_reads_config(operational: dict[str, Any]) -> None:
    assert operational["schemaVersion"] == 1
    assert operational["ranges"]["NUM_WORKERS"] == {"min": 1, "max": 8}
    assert operational["fixedKeys"] == ["IMAGE_TAG", "PRETRAINED_PATH", "POLICY_TYPE"]


def test_load_operational_constraints_rejects_missing_key(
    tmp_path: Path, operational: dict[str, Any]
) -> None:
    broken = {key: value for key, value in operational.items() if key != "maxChangedKeys"}
    path = tmp_path / "constraints.json"
    path.write_text(json.dumps(broken), encoding="utf-8")

    with pytest.raises(SchemaError, match="maxChangedKeys"):
        load_operational_constraints(path)


def test_load_operational_constraints_rejects_other_schema_version(
    tmp_path: Path, operational: dict[str, Any]
) -> None:
    path = tmp_path / "constraints.json"
    path.write_text(json.dumps({**operational, "schemaVersion": 2}), encoding="utf-8")

    with pytest.raises(SchemaError, match="schemaVersion"):
        load_operational_constraints(path)


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        ("STEPS", 60000, 60000),
        ("STEPS", "60000", 60000),
        ("COMPILE_MODEL", False, False),
        ("COMPILE_MODEL", "false", False),
        ("COMPILE_MODEL", "true", True),
        ("IMAGE_TAG", "latest", "latest"),
        ("POLICY_TYPE", "pi0", "pi0"),
    ],
)
def test_normalize_value_converts_to_tag_type(
    schema: TemplateSchema, key: str, value: object, expected: object
) -> None:
    normalized = normalize_value(schema.tags[key], value)

    assert normalized == expected
    assert type(normalized) is type(expected)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("STEPS", True),
        ("STEPS", 1.5),
        ("STEPS", "1e3"),
        ("COMPILE_MODEL", 0),
        ("COMPILE_MODEL", "False"),
        ("IMAGE_TAG", 3),
    ],
)
def test_normalize_value_rejects_wrong_type(
    schema: TemplateSchema, key: str, value: object
) -> None:
    with pytest.raises(ValueError, match=key):
        normalize_value(schema.tags[key], value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(True, "true"), (False, "false"), (16, "16"), ("lora", "lora")],
)
def test_to_poller_string_follows_poller_rules(value: int | bool | str, expected: str) -> None:
    assert to_poller_string(value) == expected


def test_check_format_accepts_template_defaults(schema: TemplateSchema) -> None:
    normalized, violations = check_format(template_defaults(schema), schema)

    assert violations == []
    assert normalized == template_defaults(schema)


def test_check_format_normalizes_string_values(schema: TemplateSchema) -> None:
    parameters = {**template_defaults(schema), "COMPILE_MODEL": "false", "STEPS": "60000"}

    normalized, violations = check_format(parameters, schema)

    assert violations == []
    assert normalized["COMPILE_MODEL"] is False
    assert normalized["STEPS"] == 60000


def test_check_format_reports_missing_unknown_and_denied_keys(schema: TemplateSchema) -> None:
    parameters = {key: value for key, value in template_defaults(schema).items() if key != "LORA_R"}
    parameters.update({"LEARNING_RATE": "1e-4", "RESUME": "true", "MLFLOW_TRACKING_URI": "x"})

    _, violations = check_format(parameters, schema)

    assert sorted(rules_of(violations)) == sorted(
        [
            ("LORA_R", "missing_key"),
            ("LEARNING_RATE", "unknown_key"),
            ("RESUME", "denied_key"),
            ("MLFLOW_TRACKING_URI", "denied_key"),
        ]
    )


@pytest.mark.parametrize(
    ("key", "value", "rule"),
    [
        ("STEPS", 1.5, "type"),
        ("COMPILE_MODEL", "yes", "type"),
        ("STEPS", 0, "pattern"),
        ("BATCH_SIZE", -4, "pattern"),
        ("PRETRAINED_PATH", "../etc", "pattern"),
        ("IMAGE_TAG", "bad tag", "pattern"),
        ("FINETUNE_MODE", "qlora", "pattern"),
    ],
)
def test_check_format_reports_invalid_values(
    schema: TemplateSchema, key: str, value: object, rule: str
) -> None:
    normalized, violations = check_format({**template_defaults(schema), key: value}, schema)

    assert rules_of(violations) == [(key, rule)]
    assert key not in normalized


def test_check_format_reports_enum_outside_template(schema: TemplateSchema) -> None:
    narrowed = dict(schema.tags)
    narrowed["FINETUNE_MODE"] = TagSpec("FINETUNE_MODE", "enum", ("lora",), "lora")
    narrowed_schema = TemplateSchema(template_id=schema.template_id, tags=narrowed)

    _, violations = check_format(
        {**template_defaults(schema), "FINETUNE_MODE": "full"}, narrowed_schema
    )

    assert rules_of(violations) == [("FINETUNE_MODE", "enum")]


def test_check_operational_accepts_template_defaults(
    schema: TemplateSchema, operational: dict[str, Any]
) -> None:
    defaults = template_defaults(schema)

    violations, warnings = check_operational(defaults, defaults, operational)

    assert violations == []
    assert warnings == []


@pytest.mark.parametrize(
    ("key", "value", "rule"),
    [
        ("LORA_R", 12, "choices"),
        ("LORA_ALPHA", 256, "range"),
        ("STEPS", 300000, "range"),
        ("BATCH_SIZE", 128, "range"),
        ("NUM_WORKERS", 16, "range"),
        ("LOG_FREQ", 0, "range"),
    ],
)
def test_check_operational_reports_ranges(
    schema: TemplateSchema, operational: dict[str, Any], key: str, value: int, rule: str
) -> None:
    proposed = {**template_defaults(schema), key: value}

    violations, _ = check_operational(proposed, dict.fromkeys(TRAINING_PARAM_KEYS), operational)

    assert rules_of(violations) == [(key, rule)]


def test_check_operational_reports_relations(
    schema: TemplateSchema, operational: dict[str, Any]
) -> None:
    proposed = {**template_defaults(schema), "STEPS": 2000, "SAVE_FREQ": 3000, "LOG_FREQ": 4000}

    violations, _ = check_operational(proposed, dict.fromkeys(TRAINING_PARAM_KEYS), operational)

    assert rules_of(violations) == [("SAVE_FREQ", "relation"), ("LOG_FREQ", "relation")]


def test_check_operational_reports_policy_pretrained_mismatch(
    schema: TemplateSchema, operational: dict[str, Any]
) -> None:
    proposed = {**template_defaults(schema), "POLICY_TYPE": "pi0"}

    violations, _ = check_operational(proposed, dict.fromkeys(TRAINING_PARAM_KEYS), operational)

    assert rules_of(violations) == [("PRETRAINED_PATH", "relation")]


def test_check_operational_allows_pretrained_path_of_previous_run(
    schema: TemplateSchema, operational: dict[str, Any]
) -> None:
    previous = {**template_defaults(schema), "PRETRAINED_PATH": "my-org/pi05_custom"}

    violations, _ = check_operational(dict(previous), previous, operational)

    assert violations == []


def test_check_operational_reports_fixed_keys(
    schema: TemplateSchema, operational: dict[str, Any]
) -> None:
    previous = template_defaults(schema)
    proposed = {**previous, "IMAGE_TAG": "v2"}

    violations, _ = check_operational(proposed, previous, operational)

    assert rules_of(violations) == [("IMAGE_TAG", "fixed")]


def test_check_operational_reports_too_many_changes(
    schema: TemplateSchema, operational: dict[str, Any]
) -> None:
    previous = template_defaults(schema)
    proposed = {**previous, "STEPS": 40000, "BATCH_SIZE": 8, "LORA_R": 32, "NUM_WORKERS": 8}

    violations, _ = check_operational(proposed, previous, operational)

    assert rules_of(violations) == [(None, "max_changed_keys")]


def test_check_operational_reports_change_factor(
    schema: TemplateSchema, operational: dict[str, Any]
) -> None:
    previous = template_defaults(schema)
    proposed = {**previous, "STEPS": 150000}

    violations, _ = check_operational(proposed, previous, operational)

    assert rules_of(violations) == [("STEPS", "max_change_factor")]


def test_check_operational_skips_unknown_previous_values(
    schema: TemplateSchema, operational: dict[str, Any]
) -> None:
    previous = {**template_defaults(schema), "IMAGE_TAG": None, "STEPS": None}
    proposed = {**template_defaults(schema), "IMAGE_TAG": "v2", "STEPS": 150000}

    violations, _ = check_operational(proposed, previous, operational)

    assert violations == []


@pytest.mark.parametrize("mode", ["expert_only", "full"])
def test_check_operational_warns_about_gpu_memory(
    schema: TemplateSchema, operational: dict[str, Any], mode: str
) -> None:
    previous = template_defaults(schema)

    violations, warnings = check_operational(
        {**previous, "FINETUNE_MODE": mode}, previous, operational
    )

    assert violations == []
    assert len(warnings) == 1
    assert warnings[0].startswith(f"FINETUNE_MODE={mode}: ")
    assert "24 GB" in warnings[0]


def test_violation_to_dict() -> None:
    violation = Violation("STEPS", "range", "STEPS=1 is less than 1000")

    assert violation.to_dict() == {
        "key": "STEPS",
        "rule": "range",
        "message": "STEPS=1 is less than 1000",
    }
