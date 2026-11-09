"""Fetch the target run and its comparison runs from MLflow into a ``Snapshot``.

The tracking URI comes from ``MLFLOW_TRACKING_URI``. For SageMaker MLflow Apps it is the App ARN,
and the sagemaker-mlflow plugin signs the requests with SigV4 using the current AWS credentials.
"""

import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from mlflow import MlflowClient
from mlflow.entities import Run
from pask_vams_poller.constants import PIPELINE_EXECUTION_ID_PATTERN

from pask_training_advisor.models import (
    DATASET_KEY_TAGS,
    DEFAULT_EXPERIMENTS,
    DEFAULT_MAX_COMPARE_RUNS,
    EVAL_METRIC_PREFIX,
    METRIC_KEYS,
    SCHEMA_VERSION,
    TRAINING_PARAM_KEYS,
    ComparisonCriteria,
    MetricPoint,
    RunSnapshot,
    Snapshot,
)

TRACKING_URI_ENV = "MLFLOW_TRACKING_URI"
PIPELINE_EXECUTION_ID_TAG = "vams.pipeline_execution_id"
COMPARISON_STATUS = "FINISHED"
PROGRESS_METRIC_PREFIX = "train/"
NONE_TEXT = "None"
TRUE_TEXTS = frozenset({"True", "true"})
FALSE_TEXTS = frozenset({"False", "false"})
LORA_FINETUNE_MODE = "lora"
LR_SCHEDULE_PARAM_PREFIXES: tuple[str, ...] = (
    "optimizer.",
    "scheduler.",
    "policy.optimizer_",
    "policy.scheduler_",
)
RESUMED_RUN_NOTE = (
    "planned_steps uses the last metric step because params.steps keeps the first value on "
    "resumed runs"
)

SOURCE_PARAMS = "params"
SOURCE_TAGS = "tags"
KIND_STR = "str"
KIND_INT = "int"
KIND_BOOL = "bool"


class FetchError(ValueError):
    """Raised when the runs cannot be fetched or identified."""


@dataclass(frozen=True)
class TrainingParamSource:
    """Where one VAMS training parameter is recorded in MLflow.

    Attributes:
        kind: Value type, one of ``KIND_STR``, ``KIND_INT``, ``KIND_BOOL``.
        lookups: ``(SOURCE_PARAMS | SOURCE_TAGS, key)`` pairs tried in order.
        lora_only: True when the value only exists for the LoRA finetune mode.
    """

    kind: str
    lookups: tuple[tuple[str, str], ...]
    lora_only: bool = False


TRAINING_PARAM_SOURCES: dict[str, TrainingParamSource] = {
    "POLICY_TYPE": TrainingParamSource(
        KIND_STR, ((SOURCE_PARAMS, "policy.type"), (SOURCE_TAGS, "lerobot.policy_type"))
    ),
    "PRETRAINED_PATH": TrainingParamSource(
        KIND_STR,
        ((SOURCE_TAGS, "lerobot.pretrained_path"), (SOURCE_PARAMS, "policy.pretrained_path")),
    ),
    "FINETUNE_MODE": TrainingParamSource(KIND_STR, ((SOURCE_TAGS, "lerobot.finetune_mode"),)),
    "LORA_R": TrainingParamSource(KIND_INT, ((SOURCE_PARAMS, "peft.r"),), lora_only=True),
    "LORA_ALPHA": TrainingParamSource(
        KIND_INT, ((SOURCE_PARAMS, "peft.lora_alpha"),), lora_only=True
    ),
    "STEPS": TrainingParamSource(KIND_INT, ((SOURCE_PARAMS, "steps"),)),
    "BATCH_SIZE": TrainingParamSource(KIND_INT, ((SOURCE_PARAMS, "batch_size"),)),
    "SAVE_FREQ": TrainingParamSource(KIND_INT, ((SOURCE_PARAMS, "save_freq"),)),
    "LOG_FREQ": TrainingParamSource(KIND_INT, ((SOURCE_PARAMS, "log_freq"),)),
    "NUM_WORKERS": TrainingParamSource(KIND_INT, ((SOURCE_PARAMS, "num_workers"),)),
    "COMPILE_MODEL": TrainingParamSource(KIND_BOOL, ((SOURCE_PARAMS, "policy.compile_model"),)),
    "IMAGE_TAG": TrainingParamSource(KIND_STR, ((SOURCE_TAGS, "pask.image_tag"),)),
}


def tracking_uri() -> str:
    """Return the MLflow tracking URI from the environment.

    Returns:
        The value of ``MLFLOW_TRACKING_URI``.

    Raises:
        FetchError: If ``MLFLOW_TRACKING_URI`` is not set or empty.
    """
    uri = os.environ.get(TRACKING_URI_ENV, "")
    if not uri:
        raise FetchError(
            f"{TRACKING_URI_ENV} is not set. Set it to the ARN of the SageMaker MLflow App."
        )
    return uri


def create_client() -> MlflowClient:
    """Create an MLflow client for the tracking URI in ``MLFLOW_TRACKING_URI``.

    Returns:
        The MLflow client.

    Raises:
        FetchError: If ``MLFLOW_TRACKING_URI`` is not set.
    """
    return MlflowClient(tracking_uri=tracking_uri())


def find_experiment_ids(client: MlflowClient, experiments: Sequence[str]) -> list[str]:
    """Look up the IDs of the experiments that exist.

    Args:
        client: MLflow client.
        experiments: Experiment names. Names that do not exist are skipped.

    Returns:
        IDs of the existing experiments in the given order.
    """
    ids = []
    for name in experiments:
        experiment = client.get_experiment_by_name(name)
        if experiment is not None:
            ids.append(experiment.experiment_id)
    return ids


def quote_filter_value(value: str, context: str) -> str:
    """Quote a value for an MLflow filter string.

    Args:
        value: Value to put in the filter.
        context: Name of the value for the error message.

    Returns:
        The value enclosed in single quotes.

    Raises:
        FetchError: If the value contains a single quote.
    """
    if "'" in value:
        raise FetchError(f"{context} must not contain a single quote: {value!r}")
    return f"'{value}'"


def resolve_target_run_id(
    client: MlflowClient,
    *,
    run_id: str | None,
    pipeline_execution_id: str | None,
    experiments: Sequence[str],
) -> str:
    """Decide the MLflow run ID of the target run.

    Args:
        client: MLflow client.
        run_id: MLflow run ID given directly.
        pipeline_execution_id: VAMS pipeline execution ID recorded in the tag
            ``vams.pipeline_execution_id``.
        experiments: Experiment names searched for ``pipeline_execution_id``.

    Returns:
        The MLflow run ID.

    Raises:
        FetchError: If both or neither identifiers are given, the pipeline execution ID is
            malformed, or it does not match exactly one run.
    """
    if (run_id is None) == (pipeline_execution_id is None):
        raise FetchError("Specify exactly one of run_id and pipeline_execution_id")
    if run_id is not None:
        return run_id
    if not PIPELINE_EXECUTION_ID_PATTERN.fullmatch(pipeline_execution_id):
        raise FetchError(
            f"pipeline_execution_id must match {PIPELINE_EXECUTION_ID_PATTERN.pattern}: "
            f"{pipeline_execution_id!r}"
        )
    experiment_ids = find_experiment_ids(client, experiments)
    if not experiment_ids:
        raise FetchError(f"None of the experiments exist: {list(experiments)}")
    filter_string = f"tags.`{PIPELINE_EXECUTION_ID_TAG}` = '{pipeline_execution_id}'"
    runs = client.search_runs(experiment_ids=experiment_ids, filter_string=filter_string)
    if len(runs) != 1:
        raise FetchError(
            f"Expected one run tagged {PIPELINE_EXECUTION_ID_TAG}={pipeline_execution_id} in "
            f"{list(experiments)}, found {len(runs)}"
        )
    return runs[0].info.run_id


def _convert_param(key: str, kind: str, text: str) -> int | bool | str:
    """Convert the recorded text of a training parameter to its type.

    Args:
        key: Training parameter name for the error message.
        kind: One of ``KIND_STR``, ``KIND_INT``, ``KIND_BOOL``.
        text: Recorded text other than ``"None"``.

    Returns:
        The converted value.

    Raises:
        FetchError: If the text cannot be converted.
    """
    if kind == KIND_INT:
        try:
            return int(text)
        except ValueError as error:
            raise FetchError(f"{key} must be an integer, got {text!r}") from error
    if kind == KIND_BOOL:
        if text in TRUE_TEXTS:
            return True
        if text in FALSE_TEXTS:
            return False
        raise FetchError(f"{key} must be a boolean, got {text!r}")
    return text


def restore_training_params(
    params: dict[str, str], tags: dict[str, str]
) -> dict[str, int | bool | str | None]:
    """Restore the 12 VAMS training parameters from MLflow params and tags.

    Args:
        params: MLflow params of the run.
        tags: MLflow tags of the run.

    Returns:
        Values keyed by training parameter name. Missing values and ``"None"`` become None,
        and ``LORA_R`` / ``LORA_ALPHA`` are None unless ``FINETUNE_MODE`` is ``lora``.

    Raises:
        FetchError: If a recorded value cannot be converted to its type.
    """
    recorded = {SOURCE_PARAMS: params, SOURCE_TAGS: tags}
    restored: dict[str, int | bool | str | None] = {}
    for key in TRAINING_PARAM_KEYS:
        source = TRAINING_PARAM_SOURCES[key]
        text = next(
            (
                recorded[origin][name]
                for origin, name in source.lookups
                if recorded[origin].get(name, NONE_TEXT) != NONE_TEXT
            ),
            None,
        )
        restored[key] = None if text is None else _convert_param(key, source.kind, text)
    if restored["FINETUNE_MODE"] != LORA_FINETUNE_MODE:
        for key in TRAINING_PARAM_KEYS:
            if TRAINING_PARAM_SOURCES[key].lora_only:
                restored[key] = None
    return restored


def extract_lr_schedule_params(params: dict[str, str]) -> dict[str, str]:
    """Pick the params that describe the optimizer and the learning rate schedule.

    Args:
        params: MLflow params of the run.

    Returns:
        Params whose key starts with one of ``LR_SCHEDULE_PARAM_PREFIXES``.
    """
    return {
        key: value for key, value in params.items() if key.startswith(LR_SCHEDULE_PARAM_PREFIXES)
    }


def _fetch_metric_histories(client: MlflowClient, run: Run) -> dict[str, list[MetricPoint]]:
    """Fetch the full history of the advisor metrics that the run logged.

    Args:
        client: MLflow client.
        run: MLflow run.

    Returns:
        Points in ascending step order keyed by metric name. Metrics the run did not log are
        left out.
    """
    logged = run.data.metrics
    metric_keys = [key for key in METRIC_KEYS if key in logged]
    metric_keys += sorted(key for key in logged if key.startswith(EVAL_METRIC_PREFIX))
    histories = {}
    for metric_key in metric_keys:
        history = client.get_metric_history(run.info.run_id, metric_key)
        points = [
            MetricPoint(
                step=int(metric.step), value=float(metric.value), timestamp=metric.timestamp
            )
            for metric in history
        ]
        histories[metric_key] = sorted(points, key=lambda point: (point.step, point.timestamp))
    return histories


def _planned_steps(
    steps_param: int | None, metrics: dict[str, list[MetricPoint]]
) -> tuple[int | None, list[str]]:
    """Decide the planned total steps of a run.

    Args:
        steps_param: Value of params ``steps``, if recorded.
        metrics: Metric histories of the run.

    Returns:
        The larger of ``steps_param`` and the last step of the ``train/`` metrics, and the notes
        that explain the choice.
    """
    last_steps = [
        points[-1].step
        for metric_key, points in metrics.items()
        if metric_key.startswith(PROGRESS_METRIC_PREFIX) and points
    ]
    last_step = max(last_steps, default=None)
    if last_step is None:
        return steps_param, []
    if steps_param is None:
        return last_step, []
    if last_step > steps_param:
        return last_step, [RESUMED_RUN_NOTE]
    return steps_param, []


def fetch_run(client: MlflowClient, run_id: str) -> RunSnapshot:
    """Fetch one run with its params, tags, and metric histories.

    Args:
        client: MLflow client.
        run_id: MLflow run ID.

    Returns:
        The run snapshot.

    Raises:
        FetchError: If a training parameter cannot be restored.
    """
    run = client.get_run(run_id)
    params = dict(run.data.params)
    tags = dict(run.data.tags)
    experiment = client.get_experiment(run.info.experiment_id)
    metrics = _fetch_metric_histories(client, run)
    training_params = restore_training_params(params, tags)
    planned_steps, notes = _planned_steps(training_params["STEPS"], metrics)
    return RunSnapshot(
        run_id=run.info.run_id,
        run_name=run.info.run_name or "",
        experiment_id=run.info.experiment_id,
        experiment_name=experiment.name,
        status=run.info.status,
        start_time=run.info.start_time,
        end_time=run.info.end_time,
        params=params,
        tags=tags,
        metrics=metrics,
        training_params=training_params,
        planned_steps=planned_steps,
        lr_schedule_params=extract_lr_schedule_params(params),
        notes=notes,
    )


def find_comparison_runs(
    client: MlflowClient,
    target: RunSnapshot,
    *,
    experiments: Sequence[str],
    max_compare_runs: int,
) -> tuple[list[RunSnapshot], ComparisonCriteria]:
    """Find finished runs trained on the same dataset as the target run.

    Args:
        client: MLflow client.
        target: Target run.
        experiments: Experiment names to search. Names that do not exist are skipped.
        max_compare_runs: Upper limit of the number of comparison runs.

    Returns:
        Comparison runs in descending start time order, and the criteria used to select them.

    Raises:
        FetchError: If ``max_compare_runs`` is negative or the dataset value contains a single
            quote.
    """
    if max_compare_runs < 0:
        raise FetchError(f"max_compare_runs must be 0 or more, got {max_compare_runs}")
    dataset_key = next((key for key in DATASET_KEY_TAGS if key in target.tags), None)
    dataset_value = None if dataset_key is None else target.tags[dataset_key]
    criteria = ComparisonCriteria(
        dataset_key=dataset_key,
        dataset_value=dataset_value,
        experiments=list(experiments),
        max_compare_runs=max_compare_runs,
        statuses=[COMPARISON_STATUS],
    )
    experiment_ids = find_experiment_ids(client, experiments)
    if dataset_key is None or max_compare_runs == 0 or not experiment_ids:
        return [], criteria
    filter_string = (
        f"tags.`{dataset_key}` = {quote_filter_value(dataset_value, dataset_key)} "
        f"and attributes.status = '{COMPARISON_STATUS}'"
    )
    candidates: list[Run] = []
    for experiment_id in experiment_ids:
        candidates += client.search_runs(
            experiment_ids=[experiment_id],
            filter_string=filter_string,
            order_by=["attributes.start_time DESC"],
            max_results=max_compare_runs + 1,
        )
    candidates = [run for run in candidates if run.info.run_id != target.run_id]
    candidates.sort(key=lambda run: run.info.start_time or 0, reverse=True)
    comparisons = [fetch_run(client, run.info.run_id) for run in candidates[:max_compare_runs]]
    return comparisons, criteria


def fetch_snapshot(
    client: MlflowClient,
    *,
    run_id: str | None = None,
    pipeline_execution_id: str | None = None,
    experiments: Sequence[str] = DEFAULT_EXPERIMENTS,
    max_compare_runs: int = DEFAULT_MAX_COMPARE_RUNS,
) -> Snapshot:
    """Fetch the target run and its comparison runs.

    Args:
        client: MLflow client.
        run_id: MLflow run ID of the target run.
        pipeline_execution_id: VAMS pipeline execution ID of the target run.
        experiments: Experiment names searched for the target and comparison runs.
        max_compare_runs: Upper limit of the number of comparison runs.

    Returns:
        The snapshot. ``tracking_uri`` holds the URI of the client and must not be shown in
        reports.

    Raises:
        FetchError: If the target run cannot be identified or a value cannot be restored.
    """
    target_run_id = resolve_target_run_id(
        client,
        run_id=run_id,
        pipeline_execution_id=pipeline_execution_id,
        experiments=experiments,
    )
    target = fetch_run(client, target_run_id)
    comparisons, criteria = find_comparison_runs(
        client, target, experiments=experiments, max_compare_runs=max_compare_runs
    )
    return Snapshot(
        schema_version=SCHEMA_VERSION,
        created_at=datetime.now(timezone.utc).isoformat(),
        tracking_uri=client.tracking_uri,
        target=target,
        comparisons=comparisons,
        criteria=criteria,
    )
