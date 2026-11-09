"""Validation of ``proposal.json`` and its conversion to ``vams_parameters.json``.

``vams_parameters.json`` has the shape of ``pipelineExecutionParameters`` of the VAMS execute
API: ``{pipelineId: {"templateId": ..., "templateTags": [{"key": ..., "value": ...}]}}``. All 12
training parameters are written, because VAMS falls back to the template default for any tag
that is not given.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pask_training_advisor.constraints import (
    ParamValue,
    TemplateSchema,
    Violation,
    check_format,
    check_operational,
    normalize_value,
    to_poller_string,
)
from pask_training_advisor.models import TRAINING_PARAM_KEYS, Proposal, Snapshot, write_json

RULE_BASE_RUN_ID = "base_run_id"
RULE_BASE_VALUE = "base_value"
RULE_CHANGE_KEY = "change_key"
RULE_DUPLICATE_CHANGE = "duplicate_change"
RULE_CHANGE_FROM_MISMATCH = "change_from_mismatch"
RULE_CHANGE_TO_MISMATCH = "change_to_mismatch"
RULE_UNLISTED_CHANGE = "unlisted_change"


@dataclass
class ValidationResult:
    """Outcome of ``validate_proposal``.

    Attributes:
        ok: True when there is no violation.
        violations: Broken rules.
        warnings: Remarks that do not block the proposal.
        normalized_parameters: Parameters converted to the template tag types. It holds all
            12 parameters only when ``ok`` is True.
    """

    ok: bool
    violations: list[Violation]
    warnings: list[str]
    normalized_parameters: dict[str, ParamValue]

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON object reported to the agent.

        Returns:
            A dict with ``ok``, ``violations``, and ``warnings``.
        """
        return {
            "ok": self.ok,
            "violations": [violation.to_dict() for violation in self.violations],
            "warnings": list(self.warnings),
        }


def _normalize_base(
    training_params: dict[str, int | bool | str | None], schema: TemplateSchema
) -> tuple[dict[str, ParamValue | None], list[Violation]]:
    """Normalize the 12 parameters of the previous run.

    Args:
        training_params: ``trainingParams`` of the target run in the snapshot.
        schema: Template schema.

    Returns:
        Normalized values, with None for unknown values, and violations for values that
        cannot be converted. Those values are also set to None so they are not compared.
    """
    base: dict[str, ParamValue | None] = {}
    violations: list[Violation] = []
    for key in TRAINING_PARAM_KEYS:
        value = training_params.get(key)
        if value is None:
            base[key] = None
            continue
        try:
            base[key] = normalize_value(schema.tags[key], value)
        except ValueError as error:
            base[key] = None
            violations.append(
                Violation(key, RULE_BASE_VALUE, f"value of the previous run is invalid: {error}")
            )
    return base, violations


def _check_changes_list(
    proposal: Proposal,
    normalized: dict[str, ParamValue],
    base: dict[str, ParamValue | None],
    schema: TemplateSchema,
) -> tuple[list[Violation], list[str]]:
    """Check ``changes`` against the previous run and the proposed parameters.

    Args:
        proposal: The proposal.
        normalized: Normalized proposed parameters that passed the format rules.
        base: Normalized parameters of the previous run.
        schema: Template schema.

    Returns:
        The violations and the warnings.
    """
    violations: list[Violation] = []
    warnings: list[str] = []
    listed_keys: set[str] = set()
    for index, change in enumerate(proposal.changes):
        key = change.key
        context = f"changes[{index}]"
        if key not in TRAINING_PARAM_KEYS:
            violations.append(
                Violation(
                    key, RULE_CHANGE_KEY, f"{context}.key {key!r} is not a training parameter"
                )
            )
            continue
        if key in listed_keys:
            violations.append(
                Violation(key, RULE_DUPLICATE_CHANGE, f"{context} lists {key} more than once")
            )
            continue
        listed_keys.add(key)
        spec = schema.tags[key]
        previous = base[key]
        if previous is not None:
            try:
                from_value = normalize_value(spec, change.from_value)
            except ValueError as error:
                violations.append(
                    Violation(key, RULE_CHANGE_FROM_MISMATCH, f"{context}.from: {error}")
                )
            else:
                if from_value != previous:
                    violations.append(
                        Violation(
                            key,
                            RULE_CHANGE_FROM_MISMATCH,
                            f"{context}.from is {to_poller_string(from_value)!r}, but the "
                            f"previous run used {to_poller_string(previous)!r}",
                        )
                    )
        if key not in normalized:
            continue
        try:
            to_value = normalize_value(spec, change.to_value)
        except ValueError as error:
            violations.append(Violation(key, RULE_CHANGE_TO_MISMATCH, f"{context}.to: {error}"))
            continue
        if to_value != normalized[key]:
            violations.append(
                Violation(
                    key,
                    RULE_CHANGE_TO_MISMATCH,
                    f"{context}.to is {to_poller_string(to_value)!r}, but parameters.{key} is "
                    f"{to_poller_string(normalized[key])!r}",
                )
            )
        elif previous is not None and to_value == previous:
            warnings.append(f"{context} lists {key}, but the value does not change")
    for key, value in normalized.items():
        previous = base.get(key)
        if key in listed_keys or previous is None or value == previous:
            continue
        violations.append(
            Violation(
                key,
                RULE_UNLISTED_CHANGE,
                f"{key} changes from {to_poller_string(previous)!r} to "
                f"{to_poller_string(value)!r} but is not listed in changes",
            )
        )
    return violations, warnings


def _alternative_warnings(
    proposal: Proposal,
    base: dict[str, ParamValue | None],
    schema: TemplateSchema,
    constraints: dict[str, Any],
) -> list[str]:
    """Check the alternatives against the format rules and the operational constraints.

    Alternatives do not block the proposal, so their violations become warnings. The warnings
    of the operational constraints, such as the GPU memory warning of ``FINETUNE_MODE``, are
    reported as for the main parameters.

    Args:
        proposal: The proposal.
        base: Normalized parameters of the previous run.
        schema: Template schema.
        constraints: Operational constraints.

    Returns:
        Warnings prefixed with the alternative position. Violations are written as
        ``rule: message``.
    """
    warnings: list[str] = []
    for index, alternative in enumerate(proposal.alternatives):
        prefix = f"alternatives[{index}]"
        normalized, format_violations = check_format(alternative.parameters, schema)
        operational_violations, operational_warnings = check_operational(
            normalized, base, constraints
        )
        warnings.extend(
            f"{prefix}: {violation.rule}: {violation.message}"
            for violation in [*format_violations, *operational_violations]
        )
        warnings.extend(f"{prefix}: {warning}" for warning in operational_warnings)
    return warnings


def validate_proposal(
    proposal: Proposal,
    snapshot: Snapshot,
    schema: TemplateSchema,
    constraints: dict[str, Any],
) -> ValidationResult:
    """Validate a proposal written by the agent.

    The checks are, in order: the 12 keys and no extra key, the template types and the poller
    allow list, the operational constraints, and the consistency of ``baseRunId`` and
    ``changes`` with the previous run. Alternatives are checked against the format rules and
    the operational constraints; their violations and warnings become warnings.

    Args:
        proposal: The proposal.
        snapshot: Snapshot whose target run is the previous run.
        schema: Template schema.
        constraints: Operational constraints.

    Returns:
        The validation result.
    """
    target = snapshot.target
    normalized, violations = check_format(proposal.parameters, schema)
    base, base_violations = _normalize_base(target.training_params, schema)
    violations.extend(base_violations)
    operational_violations, warnings = check_operational(normalized, base, constraints)
    violations.extend(operational_violations)
    if proposal.base_run_id != target.run_id:
        violations.append(
            Violation(
                None,
                RULE_BASE_RUN_ID,
                f"baseRunId {proposal.base_run_id!r} differs from the snapshot target run "
                f"{target.run_id!r}",
            )
        )
    change_violations, change_warnings = _check_changes_list(proposal, normalized, base, schema)
    violations.extend(change_violations)
    warnings.extend(change_warnings)
    warnings.extend(_alternative_warnings(proposal, base, schema, constraints))
    return ValidationResult(
        ok=not violations,
        violations=violations,
        warnings=warnings,
        normalized_parameters=normalized,
    )


def to_vams_parameters(
    normalized: dict[str, ParamValue], *, template_id: str, pipeline_id: str
) -> dict[str, Any]:
    """Build the ``pipelineExecutionParameters`` of the VAMS execute API.

    Args:
        normalized: All 12 normalized training parameters.
        template_id: ``templateId`` of the pipeline template.
        pipeline_id: VAMS pipeline ID.

    Returns:
        ``{pipeline_id: {"templateId": template_id, "templateTags": [...]}}`` with the tags in
        the order of ``TRAINING_PARAM_KEYS``.

    Raises:
        ValueError: If ``pipeline_id`` or ``template_id`` is empty or a parameter is missing.
    """
    if pipeline_id == "":
        raise ValueError("pipeline_id must not be empty")
    if template_id == "":
        raise ValueError("template_id must not be empty")
    missing_keys = [key for key in TRAINING_PARAM_KEYS if key not in normalized]
    if missing_keys:
        raise ValueError(f"normalized parameters are missing {missing_keys}")
    template_tags = [{"key": key, "value": normalized[key]} for key in TRAINING_PARAM_KEYS]
    return {pipeline_id: {"templateId": template_id, "templateTags": template_tags}}


def write_vams_parameters(path: Path, data: dict[str, Any]) -> None:
    """Write ``vams_parameters.json``.

    Args:
        path: Destination file.
        data: Result of ``to_vams_parameters``.
    """
    write_json(path, data)
