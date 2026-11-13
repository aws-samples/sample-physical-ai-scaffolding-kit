"""Data models shared by the advisor subcommands and their JSON representation.

Every JSON file written by the advisor uses camelCase keys and carries ``schemaVersion``. The
dataclasses use snake_case attributes and convert with ``from_dict`` / ``to_dict``.
"""

import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

TRAINING_PARAM_KEYS: tuple[str, ...] = (
    "POLICY_TYPE",
    "PRETRAINED_PATH",
    "FINETUNE_MODE",
    "LORA_R",
    "LORA_ALPHA",
    "STEPS",
    "BATCH_SIZE",
    "SAVE_FREQ",
    "LOG_FREQ",
    "NUM_WORKERS",
    "COMPILE_MODEL",
    "IMAGE_TAG",
)

METRIC_KEYS: tuple[str, ...] = (
    "train/loss",
    "train/grad_norm",
    "train/lr",
    "train/update_s",
    "train/dataloading_s",
    "train/samples_per_s",
    "train/gpu_mem_gb",
    "train/epochs",
    "train/samples",
    "train/episodes",
    "checkpoint_step",
    "system/gpu_0_utilization_percentage",
    "system/gpu_0_memory_usage_percentage",
)

EVAL_METRIC_PREFIX = "eval/"
DEFAULT_EXPERIMENTS: tuple[str, ...] = ("vams-training", "lerobot-pi")
DEFAULT_MAX_COMPARE_RUNS = 5
DATASET_KEY_TAGS: tuple[str, ...] = ("vams.dataset_digest", "lerobot.dataset_repo_id")

SNAPSHOT_FILE = "snapshot.json"
DIAGNOSIS_FILE = "diagnosis.json"
PROPOSAL_FILE = "proposal.json"
VAMS_PARAMETERS_FILE = "vams_parameters.json"
DATASETS_FILE = "datasets.json"
REPORT_FILE = "report.md"

SCRIPTS_DIR = Path(__file__).resolve().parents[2]
CONFIG_DIR = SCRIPTS_DIR / "config"
REPO_ROOT = Path(__file__).resolve().parents[6]
TEMPLATE_PATH = REPO_ROOT / "hyperpod/vams/template.json"
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "samples/lerobot-openpi/outputs/advisor"

TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"

NAN_TEXT = "nan"
POSITIVE_INFINITY_TEXT = "inf"
NEGATIVE_INFINITY_TEXT = "-inf"
NON_FINITE_FROM_TEXT: dict[str, float] = {
    NAN_TEXT: math.nan,
    POSITIVE_INFINITY_TEXT: math.inf,
    NEGATIVE_INFINITY_TEXT: -math.inf,
}


class SchemaError(ValueError):
    """Raised when a JSON document does not match the advisor schema."""


def utc_timestamp(now: datetime | None = None) -> str:
    """Format a moment as the compact UTC timestamp used for output directories.

    Args:
        now: Timezone aware moment to format. The current time is used when omitted.

    Returns:
        The moment in UTC formatted as ``%Y%m%dT%H%M%SZ``.

    Raises:
        ValueError: If ``now`` is a naive datetime.
    """
    if now is None:
        now = datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("now must be a timezone aware datetime")
    return now.astimezone(UTC).strftime(TIMESTAMP_FORMAT)


def default_output_dir(now: datetime | None = None) -> Path:
    """Return the default output directory for one advisor session.

    Args:
        now: Timezone aware moment that names the directory. The current time is used when
            omitted.

    Returns:
        ``DEFAULT_OUTPUT_ROOT / <UTC timestamp>``.
    """
    return DEFAULT_OUTPUT_ROOT / utc_timestamp(now)


def _require(
    data: dict[str, Any], key: str, expected_type: type | tuple[type, ...], context: str
) -> Any:
    """Return ``data[key]`` after checking that it exists and has the expected type.

    ``bool`` is rejected where ``int`` is expected because JSON booleans are not counts. Pass
    ``object`` to accept any JSON value.

    Args:
        data: JSON object to read.
        key: Key to read.
        expected_type: Type or types the value must have.
        context: Name of the object for the error message.

    Returns:
        The value of ``data[key]``.

    Raises:
        SchemaError: If the key is missing or the value has another type.
    """
    if not isinstance(data, dict):
        raise SchemaError(f"{context} must be a JSON object, got {type(data).__name__}")
    if key not in data:
        raise SchemaError(f"{context} is missing the required key '{key}'")
    value = data[key]
    if not _is_instance(value, expected_type):
        raise SchemaError(
            f"{context}.{key} must be {_type_names(expected_type)}, got {type(value).__name__}"
        )
    return value


def _is_instance(value: object, expected_type: type | tuple[type, ...]) -> bool:
    """Check a JSON value against a type, treating ``bool`` as distinct from ``int``.

    Args:
        value: Value to check.
        expected_type: Type or types the value must have.

    Returns:
        True when the value matches.
    """
    expected_types = expected_type if isinstance(expected_type, tuple) else (expected_type,)
    if isinstance(value, bool) and object not in expected_types:
        return bool in expected_types
    return isinstance(value, expected_types)


def _type_names(expected_type: type | tuple[type, ...]) -> str:
    """Return a readable name for one or more types.

    Args:
        expected_type: Type or types.

    Returns:
        Names joined with ``or``.
    """
    expected_types = expected_type if isinstance(expected_type, tuple) else (expected_type,)
    return " or ".join("null" if item is type(None) else item.__name__ for item in expected_types)


def _require_str_dict(data: dict[str, Any], key: str, context: str) -> dict[str, str]:
    """Return a JSON object whose values must all be strings.

    Args:
        data: JSON object to read.
        key: Key of the nested object.
        context: Name of the parent object for the error message.

    Returns:
        A copy of the nested object.

    Raises:
        SchemaError: If the key is missing or a value is not a string.
    """
    mapping = _require(data, key, dict, context)
    for item_key, item_value in mapping.items():
        if not isinstance(item_value, str):
            raise SchemaError(
                f"{context}.{key}['{item_key}'] must be str, got {type(item_value).__name__}"
            )
    return dict(mapping)


def _require_str_list(data: dict[str, Any], key: str, context: str) -> list[str]:
    """Return a JSON array whose items must all be strings.

    Args:
        data: JSON object to read.
        key: Key of the array.
        context: Name of the parent object for the error message.

    Returns:
        A copy of the array.

    Raises:
        SchemaError: If the key is missing or an item is not a string.
    """
    items = _require(data, key, list, context)
    for index, item in enumerate(items):
        if not isinstance(item, str):
            raise SchemaError(f"{context}.{key}[{index}] must be str, got {type(item).__name__}")
    return list(items)


def _require_typed_dict(
    data: dict[str, Any],
    key: str,
    value_type: type | tuple[type, ...],
    context: str,
) -> dict[str, Any]:
    """Return a JSON object whose values must all have the given type.

    Args:
        data: JSON object to read.
        key: Key of the nested object.
        value_type: Type or types every value must have.
        context: Name of the parent object for the error message.

    Returns:
        A copy of the nested object.

    Raises:
        SchemaError: If the key is missing or a value has another type.
    """
    mapping = _require(data, key, dict, context)
    for item_key, item_value in mapping.items():
        if not _is_instance(item_value, value_type):
            raise SchemaError(
                f"{context}.{key}['{item_key}'] must be {_type_names(value_type)}, "
                f"got {type(item_value).__name__}"
            )
    return dict(mapping)


def _require_schema_version(data: dict[str, Any], context: str) -> int:
    """Check that a document carries the supported ``schemaVersion``.

    Args:
        data: JSON object to read.
        context: Name of the document for the error message.

    Returns:
        The schema version.

    Raises:
        SchemaError: If the version is missing or differs from ``SCHEMA_VERSION``.
    """
    version = _require(data, "schemaVersion", int, context)
    if version != SCHEMA_VERSION:
        raise SchemaError(
            f"{context}.schemaVersion is {version}, but this tool supports {SCHEMA_VERSION}"
        )
    return version


def encode_float(value: float) -> float | str:
    """Encode a float for strict JSON, writing NaN and infinities as text.

    Args:
        value: Float to encode.

    Returns:
        The float itself, or ``"nan"`` / ``"inf"`` / ``"-inf"``.
    """
    if math.isnan(value):
        return NAN_TEXT
    if math.isinf(value):
        return POSITIVE_INFINITY_TEXT if value > 0 else NEGATIVE_INFINITY_TEXT
    return value


def decode_float(value: object, context: str) -> float:
    """Decode a float written by ``encode_float``.

    Args:
        value: JSON number or one of ``"nan"`` / ``"inf"`` / ``"-inf"``.
        context: Location of the value for the error message.

    Returns:
        The decoded float.

    Raises:
        SchemaError: If the value is neither a number nor a known non finite text.
    """
    if isinstance(value, str):
        if value not in NON_FINITE_FROM_TEXT:
            raise SchemaError(f"{context} must be a number or one of nan, inf, -inf, got {value!r}")
        return NON_FINITE_FROM_TEXT[value]
    if not _is_instance(value, (int, float)):
        raise SchemaError(f"{context} must be a number, got {type(value).__name__}")
    return float(value)


@dataclass(frozen=True)
class MetricPoint:
    """One recorded value of an MLflow metric.

    Attributes:
        step: Training step of the value.
        value: Metric value. NaN and infinities are kept as they were recorded.
        timestamp: UTC epoch milliseconds when the value was recorded.
    """

    step: int
    value: float
    timestamp: int

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MetricPoint":
        """Build a metric point from its JSON object.

        Args:
            data: JSON object with ``step``, ``value``, and ``timestamp``.

        Returns:
            The metric point.

        Raises:
            SchemaError: If a key is missing or has the wrong type.
        """
        context = "MetricPoint"
        return cls(
            step=_require(data, "step", int, context),
            value=decode_float(
                _require(data, "value", (int, float, str), context), f"{context}.value"
            ),
            timestamp=_require(data, "timestamp", int, context),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON object of this metric point.

        Returns:
            A dict with ``step``, ``value``, and ``timestamp``.
        """
        return {"step": self.step, "value": encode_float(self.value), "timestamp": self.timestamp}


def _metrics_from_dict(data: dict[str, Any], context: str) -> dict[str, list[MetricPoint]]:
    """Decode the ``metrics`` object of a run.

    Args:
        data: JSON object of the run.
        context: Name of the run for the error message.

    Returns:
        Metric histories keyed by metric name.

    Raises:
        SchemaError: If the object or a history has the wrong shape.
    """
    metrics = _require(data, "metrics", dict, context)
    histories: dict[str, list[MetricPoint]] = {}
    for metric_key, points in metrics.items():
        if not isinstance(points, list):
            raise SchemaError(f"{context}.metrics['{metric_key}'] must be list")
        histories[metric_key] = [MetricPoint.from_dict(point) for point in points]
    return histories


@dataclass(frozen=True)
class RunSnapshot:
    """Everything the advisor keeps about one MLflow run.

    Attributes:
        run_id: MLflow run ID.
        run_name: MLflow run name.
        experiment_id: MLflow experiment ID.
        experiment_name: MLflow experiment name.
        status: Run status such as ``FINISHED`` or ``FAILED``.
        start_time: UTC epoch milliseconds when the run started, if known.
        end_time: UTC epoch milliseconds when the run ended, if known.
        params: MLflow params of the run.
        tags: MLflow tags of the run.
        metrics: Full history of each fetched metric.
        training_params: The 12 VAMS training parameters restored from params and tags.
        planned_steps: Planned total steps, the larger of params ``steps`` and the last step.
        lr_schedule_params: Params that describe the learning rate schedule.
        notes: Remarks about how the values were restored.
    """

    run_id: str
    run_name: str
    experiment_id: str
    experiment_name: str
    status: str
    start_time: int | None
    end_time: int | None
    params: dict[str, str]
    tags: dict[str, str]
    metrics: dict[str, list[MetricPoint]]
    training_params: dict[str, int | bool | str | None]
    planned_steps: int | None
    lr_schedule_params: dict[str, str]
    notes: list[str]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunSnapshot":
        """Build a run snapshot from its JSON object.

        Args:
            data: JSON object of the run.

        Returns:
            The run snapshot.

        Raises:
            SchemaError: If a key is missing or has the wrong type.
        """
        context = "RunSnapshot"
        return cls(
            run_id=_require(data, "runId", str, context),
            run_name=_require(data, "runName", str, context),
            experiment_id=_require(data, "experimentId", str, context),
            experiment_name=_require(data, "experimentName", str, context),
            status=_require(data, "status", str, context),
            start_time=_require(data, "startTime", (int, type(None)), context),
            end_time=_require(data, "endTime", (int, type(None)), context),
            params=_require_str_dict(data, "params", context),
            tags=_require_str_dict(data, "tags", context),
            metrics=_metrics_from_dict(data, context),
            training_params=_require_typed_dict(
                data, "trainingParams", (int, bool, str, type(None)), context
            ),
            planned_steps=_require(data, "plannedSteps", (int, type(None)), context),
            lr_schedule_params=_require_str_dict(data, "lrScheduleParams", context),
            notes=_require_str_list(data, "notes", context),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON object of this run snapshot.

        Returns:
            A dict with camelCase keys.
        """
        return {
            "runId": self.run_id,
            "runName": self.run_name,
            "experimentId": self.experiment_id,
            "experimentName": self.experiment_name,
            "status": self.status,
            "startTime": self.start_time,
            "endTime": self.end_time,
            "params": dict(self.params),
            "tags": dict(self.tags),
            "metrics": {
                metric_key: [point.to_dict() for point in points]
                for metric_key, points in self.metrics.items()
            },
            "trainingParams": dict(self.training_params),
            "plannedSteps": self.planned_steps,
            "lrScheduleParams": dict(self.lr_schedule_params),
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class ComparisonCriteria:
    """How the comparison runs were selected.

    Attributes:
        dataset_key: Tag used to decide that two runs share a dataset, if any was found.
        dataset_value: Value of that tag on the target run.
        experiments: Experiment names searched for comparison runs.
        max_compare_runs: Upper limit of the number of comparison runs.
        statuses: Run statuses accepted for comparison runs.
    """

    dataset_key: str | None
    dataset_value: str | None
    experiments: list[str]
    max_compare_runs: int
    statuses: list[str]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ComparisonCriteria":
        """Build the criteria from their JSON object.

        Args:
            data: JSON object of the criteria.

        Returns:
            The criteria.

        Raises:
            SchemaError: If a key is missing or has the wrong type.
        """
        context = "ComparisonCriteria"
        return cls(
            dataset_key=_require(data, "datasetKey", (str, type(None)), context),
            dataset_value=_require(data, "datasetValue", (str, type(None)), context),
            experiments=_require_str_list(data, "experiments", context),
            max_compare_runs=_require(data, "maxCompareRuns", int, context),
            statuses=_require_str_list(data, "statuses", context),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON object of the criteria.

        Returns:
            A dict with camelCase keys.
        """
        return {
            "datasetKey": self.dataset_key,
            "datasetValue": self.dataset_value,
            "experiments": list(self.experiments),
            "maxCompareRuns": self.max_compare_runs,
            "statuses": list(self.statuses),
        }


@dataclass(frozen=True)
class Snapshot:
    """Contents of ``snapshot.json``: the target run and the runs it is compared with.

    Attributes:
        schema_version: Schema version of the document.
        created_at: UTC ISO 8601 time when the snapshot was taken.
        tracking_uri: MLflow tracking URI the runs were read from.
        target: The run being diagnosed.
        comparisons: Runs on the same dataset used for comparison.
        criteria: How the comparison runs were selected.
    """

    schema_version: int
    created_at: str
    tracking_uri: str
    target: RunSnapshot
    comparisons: list[RunSnapshot]
    criteria: ComparisonCriteria

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Snapshot":
        """Build a snapshot from its JSON object.

        Args:
            data: JSON object of ``snapshot.json``.

        Returns:
            The snapshot.

        Raises:
            SchemaError: If a key is missing, has the wrong type, or the schema version differs.
        """
        context = "Snapshot"
        return cls(
            schema_version=_require_schema_version(data, context),
            created_at=_require(data, "createdAt", str, context),
            tracking_uri=_require(data, "trackingUri", str, context),
            target=RunSnapshot.from_dict(_require(data, "target", dict, context)),
            comparisons=[
                RunSnapshot.from_dict(run) for run in _require(data, "comparisons", list, context)
            ],
            criteria=ComparisonCriteria.from_dict(_require(data, "criteria", dict, context)),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON object of ``snapshot.json``.

        Returns:
            A dict with camelCase keys.
        """
        return {
            "schemaVersion": self.schema_version,
            "createdAt": self.created_at,
            "trackingUri": self.tracking_uri,
            "target": self.target.to_dict(),
            "comparisons": [run.to_dict() for run in self.comparisons],
            "criteria": self.criteria.to_dict(),
        }


FEATURE_VALUE_TYPES: tuple[type, ...] = (int, float, type(None))


@dataclass(frozen=True)
class RunDiagnosis:
    """Diagnosis of one run.

    Attributes:
        run_id: MLflow run ID.
        run_name: MLflow run name.
        features: Learning curve features computed from ``train/loss`` and the other metrics.
        labels: Diagnosis labels such as ``converging`` or ``plateau``.
        findings: Sentences that explain the labels.
        eval_features: Features of the evaluation loss, or None when no ``eval/*`` was logged.
        training_params: The 12 VAMS training parameters of the run.
        param_diff_from_target: ``{key: {"target": value, "run": value}}`` for every
            parameter that differs from the target run. Empty for the target itself.
    """

    run_id: str
    run_name: str
    features: dict[str, float | int | None]
    labels: list[str]
    findings: list[str]
    eval_features: dict[str, float | int | None] | None
    training_params: dict[str, Any]
    param_diff_from_target: dict[str, dict[str, object]]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunDiagnosis":
        """Build a run diagnosis from its JSON object.

        Args:
            data: JSON object of the run diagnosis.

        Returns:
            The run diagnosis.

        Raises:
            SchemaError: If a key is missing or has the wrong type.
        """
        context = "RunDiagnosis"
        eval_features = None
        if _require(data, "evalFeatures", (dict, type(None)), context) is not None:
            eval_features = _require_typed_dict(data, "evalFeatures", FEATURE_VALUE_TYPES, context)
        return cls(
            run_id=_require(data, "runId", str, context),
            run_name=_require(data, "runName", str, context),
            features=_require_typed_dict(data, "features", FEATURE_VALUE_TYPES, context),
            labels=_require_str_list(data, "labels", context),
            findings=_require_str_list(data, "findings", context),
            eval_features=eval_features,
            training_params=dict(_require(data, "trainingParams", dict, context)),
            param_diff_from_target=_require_typed_dict(data, "paramDiffFromTarget", dict, context),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON object of this run diagnosis.

        Returns:
            A dict with camelCase keys.
        """
        return {
            "runId": self.run_id,
            "runName": self.run_name,
            "features": dict(self.features),
            "labels": list(self.labels),
            "findings": list(self.findings),
            "evalFeatures": None if self.eval_features is None else dict(self.eval_features),
            "trainingParams": dict(self.training_params),
            "paramDiffFromTarget": {
                key: dict(diff) for key, diff in self.param_diff_from_target.items()
            },
        }


@dataclass(frozen=True)
class Diagnosis:
    """Contents of ``diagnosis.json``.

    Attributes:
        schema_version: Schema version of the document.
        created_at: UTC ISO 8601 time when the diagnosis was made.
        thresholds: Thresholds the labels were decided with.
        target: Diagnosis of the target run.
        comparisons: Diagnoses of the comparison runs.
        datasets: Diagnosis of the VAMS datasets the runs used, or None when ``datasets.json``
            was not given to ``analyze``.
    """

    schema_version: int
    created_at: str
    thresholds: dict[str, Any]
    target: RunDiagnosis
    comparisons: list[RunDiagnosis]
    datasets: dict[str, Any] | None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Diagnosis":
        """Build a diagnosis from its JSON object.

        Args:
            data: JSON object of ``diagnosis.json``.

        Returns:
            The diagnosis.

        Raises:
            SchemaError: If a key is missing, has the wrong type, or the schema version differs.
        """
        context = "Diagnosis"
        datasets = None
        if "datasets" in data:
            datasets = _require(data, "datasets", (dict, type(None)), context)
        return cls(
            schema_version=_require_schema_version(data, context),
            created_at=_require(data, "createdAt", str, context),
            thresholds=dict(_require(data, "thresholds", dict, context)),
            target=RunDiagnosis.from_dict(_require(data, "target", dict, context)),
            comparisons=[
                RunDiagnosis.from_dict(run) for run in _require(data, "comparisons", list, context)
            ],
            datasets=None if datasets is None else dict(datasets),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON object of ``diagnosis.json``.

        Returns:
            A dict with camelCase keys.
        """
        return {
            "schemaVersion": self.schema_version,
            "createdAt": self.created_at,
            "thresholds": dict(self.thresholds),
            "target": self.target.to_dict(),
            "comparisons": [run.to_dict() for run in self.comparisons],
            "datasets": None if self.datasets is None else dict(self.datasets),
        }


@dataclass(frozen=True)
class Change:
    """One parameter changed by a proposal. Values are kept exactly as written in the JSON.

    Attributes:
        key: Training parameter name.
        from_value: Value of the previous run (``from`` in JSON).
        to_value: Proposed value (``to`` in JSON).
        reason: Why the value is changed.
        evidence: Labels and features that support the change.
    """

    key: str
    from_value: object
    to_value: object
    reason: str
    evidence: list[str]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Change":
        """Build a change from its JSON object.

        Args:
            data: JSON object with ``key``, ``from``, ``to``, ``reason``, and ``evidence``.

        Returns:
            The change.

        Raises:
            SchemaError: If a key is missing or has the wrong type.
        """
        context = "Change"
        return cls(
            key=_require(data, "key", str, context),
            from_value=_require(data, "from", object, context),
            to_value=_require(data, "to", object, context),
            reason=_require(data, "reason", str, context),
            evidence=_require_str_list(data, "evidence", context),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON object of this change.

        Returns:
            A dict with ``key``, ``from``, ``to``, ``reason``, and ``evidence``.
        """
        return {
            "key": self.key,
            "from": self.from_value,
            "to": self.to_value,
            "reason": self.reason,
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True)
class Alternative:
    """Another set of parameters the agent considered.

    Attributes:
        parameters: Training parameters of the alternative, kept as written in the JSON.
        reason: Why the alternative is worth considering.
    """

    parameters: dict[str, Any]
    reason: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Alternative":
        """Build an alternative from its JSON object.

        Args:
            data: JSON object with ``parameters`` and ``reason``.

        Returns:
            The alternative.

        Raises:
            SchemaError: If a key is missing or has the wrong type.
        """
        context = "Alternative"
        return cls(
            parameters=dict(_require(data, "parameters", dict, context)),
            reason=_require(data, "reason", str, context),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON object of this alternative.

        Returns:
            A dict with ``parameters`` and ``reason``.
        """
        return {"parameters": dict(self.parameters), "reason": self.reason}


@dataclass(frozen=True)
class Proposal:
    """Contents of ``proposal.json`` written by the agent.

    Parameter values are not converted so that ``validate-proposal`` can report the exact
    value that breaks a rule.

    Attributes:
        schema_version: Schema version of the document.
        base_run_id: MLflow run ID the proposal starts from.
        parameters: All 12 training parameters of the next run.
        changes: Parameters that differ from the base run and why.
        expected_effect: What the change is expected to improve.
        risks: Points to check before running.
        alternatives: Other parameter sets considered.
    """

    schema_version: int
    base_run_id: str
    parameters: dict[str, int | bool | str]
    changes: list[Change]
    expected_effect: str
    risks: list[str]
    alternatives: list[Alternative]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Proposal":
        """Build a proposal from its JSON object.

        Args:
            data: JSON object of ``proposal.json``.

        Returns:
            The proposal.

        Raises:
            SchemaError: If a key is missing, has the wrong type, or the schema version differs.
        """
        context = "Proposal"
        return cls(
            schema_version=_require_schema_version(data, context),
            base_run_id=_require(data, "baseRunId", str, context),
            parameters=dict(_require(data, "parameters", dict, context)),
            changes=[
                Change.from_dict(change) for change in _require(data, "changes", list, context)
            ],
            expected_effect=_require(data, "expectedEffect", str, context),
            risks=_require_str_list(data, "risks", context),
            alternatives=[
                Alternative.from_dict(alternative)
                for alternative in _require(data, "alternatives", list, context)
            ],
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON object of ``proposal.json``.

        Returns:
            A dict with camelCase keys.
        """
        return {
            "schemaVersion": self.schema_version,
            "baseRunId": self.base_run_id,
            "parameters": dict(self.parameters),
            "changes": [change.to_dict() for change in self.changes],
            "expectedEffect": self.expected_effect,
            "risks": list(self.risks),
            "alternatives": [alternative.to_dict() for alternative in self.alternatives],
        }


def write_json(path: Path, data: dict[str, Any]) -> None:
    """Write a JSON object as UTF-8, creating the parent directory.

    Args:
        path: Destination file.
        data: JSON object to write. NaN and infinities are rejected.

    Raises:
        ValueError: If ``data`` contains NaN or an infinity.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False)
    path.write_text(text + "\n", encoding="utf-8")


def read_json(path: Path) -> dict[str, Any]:
    """Read a JSON object from a UTF-8 file.

    Args:
        path: File to read.

    Returns:
        The JSON object.

    Raises:
        SchemaError: If the top level value is not a JSON object.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SchemaError(f"{path.name} must hold a JSON object, got {type(data).__name__}")
    return data


def load_snapshot(path: Path) -> Snapshot:
    """Read ``snapshot.json``.

    Args:
        path: File to read.

    Returns:
        The snapshot.
    """
    return Snapshot.from_dict(read_json(path))


def save_snapshot(snapshot: Snapshot, path: Path) -> None:
    """Write ``snapshot.json``.

    Args:
        snapshot: Snapshot to write.
        path: Destination file.
    """
    write_json(path, snapshot.to_dict())


def load_diagnosis(path: Path) -> Diagnosis:
    """Read ``diagnosis.json``.

    Args:
        path: File to read.

    Returns:
        The diagnosis.
    """
    return Diagnosis.from_dict(read_json(path))


def save_diagnosis(diagnosis: Diagnosis, path: Path) -> None:
    """Write ``diagnosis.json``.

    Args:
        diagnosis: Diagnosis to write.
        path: Destination file.
    """
    write_json(path, diagnosis.to_dict())


def load_proposal(path: Path) -> Proposal:
    """Read ``proposal.json`` without converting the parameter values.

    Args:
        path: File to read.

    Returns:
        The proposal.
    """
    return Proposal.from_dict(read_json(path))
