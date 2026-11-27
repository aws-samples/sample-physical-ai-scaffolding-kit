"""Constraints on the training parameters of a proposal.

Two kinds of constraints are checked:

1. Format constraints: the allow list of the PASK VAMS poller and the tag types of
   ``hyperpod/vams/template.json``. A proposal that breaks them always fails in VAMS.
2. Operational constraints: value ranges and relations between parameters read from
   ``config/proposal_constraints.json``.

The allow list is imported from ``pask_vams_poller.messages`` so that it is defined once.
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pask_vams_poller.messages import (
    ALLOWED_TRAINING_PARAMS,
    DENIED_TRAINING_PARAM_PREFIXES,
    DENIED_TRAINING_PARAMS,
)

from pask_training_advisor.models import (
    CONFIG_DIR,
    SCHEMA_VERSION,
    TEMPLATE_PATH,
    TRAINING_PARAM_KEYS,
    SchemaError,
    read_json,
)

OPERATIONAL_CONSTRAINTS_FILE = "proposal_constraints.json"

TAG_TYPE_ENUM = "enum"
TAG_TYPE_STRING = "string"
TAG_TYPE_INTEGER = "integer"
TAG_TYPE_BOOLEAN = "boolean"
TAG_TYPES: tuple[str, ...] = (TAG_TYPE_ENUM, TAG_TYPE_STRING, TAG_TYPE_INTEGER, TAG_TYPE_BOOLEAN)

INTEGER_TEXT_PATTERN = re.compile(r"^-?[0-9]+$")
BOOLEAN_FROM_TEXT: dict[str, bool] = {"true": True, "false": False}

RULE_MISSING_KEY = "missing_key"
RULE_UNKNOWN_KEY = "unknown_key"
RULE_DENIED_KEY = "denied_key"
RULE_TYPE = "type"
RULE_PATTERN = "pattern"
RULE_ENUM = "enum"
RULE_RANGE = "range"
RULE_CHOICES = "choices"
RULE_RELATION = "relation"
RULE_FIXED = "fixed"
RULE_MAX_CHANGED_KEYS = "max_changed_keys"
RULE_MAX_CHANGE_FACTOR = "max_change_factor"

OPERATIONAL_TOP_LEVEL_TYPES: dict[str, type | tuple[type, ...]] = {
    "ranges": dict,
    "relations": dict,
    "maxChangedKeys": int,
    "maxChangeFactor": (int, float),
    "fixedKeys": list,
    "warnings": list,
}
RELATION_TYPES: dict[str, type] = {
    "saveFreqLteSteps": bool,
    "logFreqLteSaveFreq": bool,
    "policyPretrainedPairs": dict,
}

ParamValue = int | bool | str


def check_allow_list_consistency() -> None:
    """Check that the poller allow list has exactly the 12 advisor training parameters.

    Raises:
        RuntimeError: If the keys of ``ALLOWED_TRAINING_PARAMS`` differ from
            ``TRAINING_PARAM_KEYS``.
    """
    allowed_keys = set(ALLOWED_TRAINING_PARAMS)
    advisor_keys = set(TRAINING_PARAM_KEYS)
    if allowed_keys != advisor_keys:
        raise RuntimeError(
            "The poller allow list and TRAINING_PARAM_KEYS differ: "
            f"only in the allow list {sorted(allowed_keys - advisor_keys)}, "
            f"only in TRAINING_PARAM_KEYS {sorted(advisor_keys - allowed_keys)}"
        )


check_allow_list_consistency()


@dataclass(frozen=True)
class TagSpec:
    """Type of one template tag read from ``tagSchema``.

    Attributes:
        key: Tag key, which is the training parameter name.
        type: One of ``enum``, ``string``, ``integer``, and ``boolean``.
        enum_values: Allowed values of an ``enum`` tag, otherwise None.
        default: Default value of the tag in the template.
    """

    key: str
    type: str
    enum_values: tuple[str, ...] | None
    default: ParamValue


@dataclass
class TemplateSchema:
    """The parts of the VAMS pipeline template the advisor needs.

    Attributes:
        template_id: ``templateId`` of the template.
        tags: Tag types keyed by tag key.
    """

    template_id: str
    tags: dict[str, TagSpec]


@dataclass
class Violation:
    """One broken rule.

    Attributes:
        key: Training parameter the rule applies to, or None for rules about the whole proposal.
        rule: Name of the rule, such as ``missing_key`` or ``range``.
        message: Human readable explanation.
    """

    key: str | None
    rule: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON object of this violation.

        Returns:
            A dict with ``key``, ``rule``, and ``message``.
        """
        return {"key": self.key, "rule": self.rule, "message": self.message}


def _tag_spec_from_entry(entry: dict[str, Any], index: int) -> TagSpec:
    """Build a tag spec from one ``tagSchema`` entry.

    Args:
        entry: JSON object of the entry.
        index: Position of the entry, used in error messages.

    Returns:
        The tag spec.

    Raises:
        SchemaError: If the entry lacks a key or has an unknown type.
    """
    context = f"tagSchema[{index}]"
    if not isinstance(entry, dict):
        raise SchemaError(f"{context} must be a JSON object")
    for required_key in ("tagKey", "type", "default"):
        if required_key not in entry:
            raise SchemaError(f"{context} is missing the required key '{required_key}'")
    tag_type = entry["type"]
    if tag_type not in TAG_TYPES:
        raise SchemaError(f"{context}.type is {tag_type!r}, expected one of {TAG_TYPES}")
    enum_values = None
    if tag_type == TAG_TYPE_ENUM:
        values = entry.get("enumValues")
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise SchemaError(f"{context}.enumValues must be a list of strings for an enum tag")
        enum_values = tuple(values)
    return TagSpec(
        key=entry["tagKey"],
        type=tag_type,
        enum_values=enum_values,
        default=entry["default"],
    )


def load_template_schema(path: Path | None = None) -> TemplateSchema:
    """Read the template ID and tag types from a VAMS pipeline template.

    Args:
        path: Template file. ``hyperpod/vams/template.json`` is used when omitted.

    Returns:
        The template schema.

    Raises:
        SchemaError: If ``templateId`` or ``tagSchema`` is malformed, or a training parameter
            has no tag.
    """
    template = read_json(TEMPLATE_PATH if path is None else path)
    template_id = template.get("templateId")
    if not isinstance(template_id, str) or template_id == "":
        raise SchemaError("templateId must be a non-empty string")
    entries = template.get("tagSchema")
    if not isinstance(entries, list):
        raise SchemaError("tagSchema must be a list")
    tags: dict[str, TagSpec] = {}
    for index, entry in enumerate(entries):
        spec = _tag_spec_from_entry(entry, index)
        tags[spec.key] = spec
    missing_keys = [key for key in TRAINING_PARAM_KEYS if key not in tags]
    if missing_keys:
        raise SchemaError(f"tagSchema has no tag for {missing_keys}")
    return TemplateSchema(template_id=template_id, tags=tags)


def load_operational_constraints(path: Path | None = None) -> dict[str, Any]:
    """Read the operational constraints.

    Args:
        path: Constraints file. ``config/proposal_constraints.json`` is used when omitted.

    Returns:
        The constraints as a JSON object.

    Raises:
        SchemaError: If ``schemaVersion`` is unsupported or a top level key is missing or has
            the wrong type.
    """
    constraints = read_json(CONFIG_DIR / OPERATIONAL_CONSTRAINTS_FILE if path is None else path)
    if constraints.get("schemaVersion") != SCHEMA_VERSION:
        raise SchemaError(
            f"proposal constraints schemaVersion must be {SCHEMA_VERSION}, "
            f"got {constraints.get('schemaVersion')!r}"
        )
    for key, expected_type in OPERATIONAL_TOP_LEVEL_TYPES.items():
        _require_constraint_type(constraints, key, expected_type, "proposal constraints")
    for key, expected_type in RELATION_TYPES.items():
        _require_constraint_type(constraints["relations"], key, expected_type, "relations")
    return constraints


def _require_constraint_type(
    data: dict[str, Any], key: str, expected_type: type | tuple[type, ...], context: str
) -> None:
    """Check that a constraints key exists and has the expected type.

    Args:
        data: JSON object to read.
        key: Key to check.
        expected_type: Type or types the value must have.
        context: Name of the object for the error message.

    Raises:
        SchemaError: If the key is missing or the value has another type.
    """
    if key not in data:
        raise SchemaError(f"{context} is missing the required key '{key}'")
    value = data[key]
    if isinstance(value, bool) and expected_type is not bool:
        raise SchemaError(f"{context}.{key} must not be a boolean")
    if not isinstance(value, expected_type):
        raise SchemaError(f"{context}.{key} has the wrong type {type(value).__name__}")


def normalize_value(spec: TagSpec, value: object) -> ParamValue:
    """Convert a proposed value to the type of its template tag.

    Args:
        spec: Tag type of the parameter.
        value: Value as written in the JSON.

    Returns:
        An int for ``integer`` tags, a bool for ``boolean`` tags, and a str otherwise.

    Raises:
        ValueError: If the value cannot be converted to the tag type.
    """
    if spec.type == TAG_TYPE_INTEGER:
        if isinstance(value, bool):
            raise ValueError(f"{spec.key} must be an integer, got a boolean")
        if isinstance(value, int):
            return value
        if isinstance(value, str) and INTEGER_TEXT_PATTERN.fullmatch(value):
            return int(value)
        raise ValueError(f"{spec.key} must be an integer, got {value!r}")
    if spec.type == TAG_TYPE_BOOLEAN:
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value in BOOLEAN_FROM_TEXT:
            return BOOLEAN_FROM_TEXT[value]
        raise ValueError(f"{spec.key} must be a boolean or 'true' / 'false', got {value!r}")
    if isinstance(value, str):
        return value
    raise ValueError(f"{spec.key} must be a string, got {type(value).__name__}")


def to_poller_string(value: ParamValue) -> str:
    """Convert a value to the string the poller passes to the job.

    Follows ``pask_vams_poller.messages._training_param_to_string``.

    Args:
        value: Normalized value.

    Returns:
        ``"true"`` / ``"false"`` for booleans, the decimal text for integers, and the string
        itself otherwise.

    Raises:
        TypeError: If the value is not a bool, int, or str.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return value
    raise TypeError(f"training parameter value must be a bool, int, or str, got {value!r}")


def _is_denied(key: str) -> bool:
    """Tell whether a key is on the poller deny list.

    Args:
        key: Parameter name.

    Returns:
        True for a denied key or a key with a denied prefix.
    """
    return key in DENIED_TRAINING_PARAMS or key.startswith(DENIED_TRAINING_PARAM_PREFIXES)


def check_format(
    parameters: dict[str, Any], schema: TemplateSchema
) -> tuple[dict[str, ParamValue], list[Violation]]:
    """Check the parameters against the allow list and the template tag types.

    Args:
        parameters: Parameters as written in the JSON.
        schema: Template schema.

    Returns:
        The normalized values that passed every format rule, and the violations.
    """
    violations: list[Violation] = []
    normalized: dict[str, ParamValue] = {}
    for key in TRAINING_PARAM_KEYS:
        if key not in parameters:
            violations.append(Violation(key, RULE_MISSING_KEY, f"{key} is missing"))
    for key, value in parameters.items():
        if _is_denied(key):
            violations.append(Violation(key, RULE_DENIED_KEY, f"{key} is on the deny list"))
            continue
        pattern = ALLOWED_TRAINING_PARAMS.get(key)
        if pattern is None:
            violations.append(Violation(key, RULE_UNKNOWN_KEY, f"{key} is not on the allow list"))
            continue
        spec = schema.tags[key]
        try:
            normalized_value = normalize_value(spec, value)
        except ValueError as error:
            violations.append(Violation(key, RULE_TYPE, str(error)))
            continue
        text = to_poller_string(normalized_value)
        if not pattern.fullmatch(text):
            violations.append(
                Violation(key, RULE_PATTERN, f"{key}={text!r} does not match {pattern.pattern}")
            )
            continue
        if spec.enum_values is not None and normalized_value not in spec.enum_values:
            violations.append(
                Violation(key, RULE_ENUM, f"{key}={text!r} is not one of {list(spec.enum_values)}")
            )
            continue
        normalized[key] = normalized_value
    return normalized, violations


def _check_ranges(normalized: dict[str, ParamValue], ranges: dict[str, Any]) -> list[Violation]:
    """Check the value ranges and the candidate lists.

    Args:
        normalized: Normalized parameters.
        ranges: ``ranges`` of the constraints.

    Returns:
        The violations.
    """
    violations: list[Violation] = []
    for key, bounds in ranges.items():
        if key not in normalized:
            continue
        value = normalized[key]
        if "choices" in bounds and value not in bounds["choices"]:
            violations.append(
                Violation(key, RULE_CHOICES, f"{key}={value} is not one of {bounds['choices']}")
            )
        if "min" in bounds and value < bounds["min"]:
            violations.append(
                Violation(key, RULE_RANGE, f"{key}={value} is less than {bounds['min']}")
            )
        if "max" in bounds and value > bounds["max"]:
            violations.append(
                Violation(key, RULE_RANGE, f"{key}={value} is greater than {bounds['max']}")
            )
    return violations


def _check_relations(
    normalized: dict[str, ParamValue],
    base: dict[str, ParamValue | None],
    relations: dict[str, Any],
) -> list[Violation]:
    """Check the rules between parameters.

    Args:
        normalized: Normalized parameters.
        base: Normalized parameters of the previous run.
        relations: ``relations`` of the constraints.

    Returns:
        The violations.
    """
    violations: list[Violation] = []
    if (
        relations["saveFreqLteSteps"]
        and {"SAVE_FREQ", "STEPS"} <= normalized.keys()
        and normalized["SAVE_FREQ"] > normalized["STEPS"]
    ):
        violations.append(
            Violation(
                "SAVE_FREQ",
                RULE_RELATION,
                f"SAVE_FREQ={normalized['SAVE_FREQ']} must not exceed STEPS={normalized['STEPS']}",
            )
        )
    if (
        relations["logFreqLteSaveFreq"]
        and {"LOG_FREQ", "SAVE_FREQ"} <= normalized.keys()
        and normalized["LOG_FREQ"] > normalized["SAVE_FREQ"]
    ):
        violations.append(
            Violation(
                "LOG_FREQ",
                RULE_RELATION,
                f"LOG_FREQ={normalized['LOG_FREQ']} must not exceed "
                f"SAVE_FREQ={normalized['SAVE_FREQ']}",
            )
        )
    if {"POLICY_TYPE", "PRETRAINED_PATH"} <= normalized.keys():
        policy_type = normalized["POLICY_TYPE"]
        pretrained_path = normalized["PRETRAINED_PATH"]
        allowed_paths = relations["policyPretrainedPairs"].get(policy_type, [])
        if pretrained_path not in allowed_paths and pretrained_path != base.get("PRETRAINED_PATH"):
            violations.append(
                Violation(
                    "PRETRAINED_PATH",
                    RULE_RELATION,
                    f"PRETRAINED_PATH={pretrained_path!r} is not allowed with "
                    f"POLICY_TYPE={policy_type!r}; expected one of {allowed_paths} "
                    "or the value of the previous run",
                )
            )
    return violations


def _check_changes(
    normalized: dict[str, ParamValue],
    base: dict[str, ParamValue | None],
    constraints: dict[str, Any],
) -> list[Violation]:
    """Check the fixed parameters, the number of changes, and the change factor.

    Args:
        normalized: Normalized parameters.
        base: Normalized parameters of the previous run. None values are not compared.
        constraints: Operational constraints.

    Returns:
        The violations.
    """
    violations: list[Violation] = []
    changed_keys = [
        key for key, value in normalized.items() if base.get(key) is not None and base[key] != value
    ]
    for key in constraints["fixedKeys"]:
        if key in changed_keys:
            violations.append(
                Violation(
                    key,
                    RULE_FIXED,
                    f"{key} must keep the value of the previous run {base[key]!r}, "
                    f"got {normalized[key]!r}",
                )
            )
    max_changed_keys = constraints["maxChangedKeys"]
    if len(changed_keys) > max_changed_keys:
        violations.append(
            Violation(
                None,
                RULE_MAX_CHANGED_KEYS,
                f"{len(changed_keys)} parameters changed {changed_keys}, "
                f"but at most {max_changed_keys} may change",
            )
        )
    max_factor = constraints["maxChangeFactor"]
    for key in changed_keys:
        value = normalized[key]
        previous = base[key]
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        if isinstance(previous, bool) or not isinstance(previous, int):
            continue
        if value <= 0 or previous <= 0:
            continue
        factor = max(value, previous) / min(value, previous)
        if factor > max_factor:
            violations.append(
                Violation(
                    key,
                    RULE_MAX_CHANGE_FACTOR,
                    f"{key} changes from {previous} to {value} ({factor:g}x), "
                    f"but at most {max_factor:g}x is allowed",
                )
            )
    return violations


def _collect_warnings(
    normalized: dict[str, ParamValue], warning_rules: list[dict[str, Any]]
) -> list[str]:
    """Collect the warnings for values that are allowed but risky.

    Args:
        normalized: Normalized parameters.
        warning_rules: ``warnings`` of the constraints.

    Returns:
        Warning messages.
    """
    warnings: list[str] = []
    for rule in warning_rules:
        key = rule["key"]
        if key in normalized and normalized[key] in rule["values"]:
            warnings.append(f"{key}={to_poller_string(normalized[key])}: {rule['message']}")
    return warnings


def check_operational(
    normalized: dict[str, ParamValue],
    base: dict[str, ParamValue | None],
    constraints: dict[str, Any],
) -> tuple[list[Violation], list[str]]:
    """Check the parameters against the operational constraints.

    Args:
        normalized: Normalized parameters. Only the keys present are checked.
        base: Normalized 12 parameters of the previous run. None values are not compared.
        constraints: Operational constraints from ``load_operational_constraints``.

    Returns:
        The violations and the warnings.
    """
    violations = [
        *_check_ranges(normalized, constraints["ranges"]),
        *_check_relations(normalized, base, constraints["relations"]),
        *_check_changes(normalized, base, constraints),
    ]
    return violations, _collect_warnings(normalized, constraints["warnings"])
