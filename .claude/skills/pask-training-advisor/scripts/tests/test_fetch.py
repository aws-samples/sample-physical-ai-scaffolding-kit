"""Tests for fetch.py against a local MLflow file store."""

import pytest
from mlflow import MlflowClient

from pask_training_advisor.fetch import (
    RESUMED_RUN_NOTE,
    FetchError,
    create_client,
    extract_lr_schedule_params,
    fetch_run,
    fetch_snapshot,
    find_comparison_runs,
    resolve_target_run_id,
    restore_training_params,
    tracking_uri,
)
from pask_training_advisor.models import TRAINING_PARAM_KEYS, Snapshot

EXPERIMENT = "vams-training"
OTHER_EXPERIMENT = "lerobot-pi"
EXPERIMENTS = (EXPERIMENT, OTHER_EXPERIMENT)
DATASET_DIGEST = "sha256:" + "ab" * 32
OTHER_DATASET_DIGEST = "sha256:" + "cd" * 32
BASE_START_TIME_MS = 1_790_000_000_000
RUN_DURATION_MS = 3_600_000

LORA_PARAMS = {
    "policy.type": "pi05",
    "policy.pretrained_path": "lerobot/pi05_base",
    "policy.compile_model": "False",
    "policy.optimizer_lr": "2.5e-05",
    "policy.scheduler_warmup_steps": "1000",
    "optimizer.type": "adamw",
    "scheduler.type": "cosine_decay_with_warmup",
    "steps": "3000",
    "batch_size": "32",
    "save_freq": "1000",
    "log_freq": "100",
    "num_workers": "4",
    "peft.r": "16",
    "peft.lora_alpha": "32",
    "dataset.repo_id": "pask/test-dataset",
}
LORA_TAGS = {
    "lerobot.policy_type": "pi05",
    "lerobot.finetune_mode": "lora",
    "lerobot.pretrained_path": "lerobot/pi05_base",
    "lerobot.dataset_repo_id": "pask/test-dataset",
    "pask.image_tag": "v1.2.3",
    "pask.trigger": "vams",
    "vams.pipeline_execution_id": "exec-0001_a",
    "vams.dataset_digest": DATASET_DIGEST,
}


@pytest.fixture
def client(local_mlflow_uri: str) -> MlflowClient:
    """Return an MLflow client of the local file store with both default experiments."""
    mlflow_client = MlflowClient(tracking_uri=local_mlflow_uri)
    for name in EXPERIMENTS:
        mlflow_client.create_experiment(name)
    return mlflow_client


def _create_run(
    client: MlflowClient,
    *,
    experiment: str,
    start_offset_ms: int,
    params: dict[str, str],
    tags: dict[str, str],
    status: str,
    loss_steps: list[int],
) -> str:
    """Create a terminated run with params, tags, and a ``train/loss`` history.

    Args:
        client: MLflow client.
        experiment: Experiment name.
        start_offset_ms: Start time relative to ``BASE_START_TIME_MS``.
        params: Params to log.
        tags: Tags to set.
        status: Terminal status of the run.
        loss_steps: Steps at which ``train/loss`` and ``train/lr`` are logged.

    Returns:
        The MLflow run ID.
    """
    experiment_id = client.get_experiment_by_name(experiment).experiment_id
    start_time = BASE_START_TIME_MS + start_offset_ms
    run = client.create_run(experiment_id, start_time=start_time, tags=tags)
    run_id = run.info.run_id
    for key, value in params.items():
        client.log_param(run_id, key, value)
    for index, step in enumerate(reversed(loss_steps)):
        client.log_metric(run_id, "train/loss", 1.0 / step, timestamp=start_time + index, step=step)
        client.log_metric(run_id, "train/lr", 1e-5, timestamp=start_time + index, step=step)
    client.set_terminated(run_id, status=status, end_time=start_time + RUN_DURATION_MS)
    return run_id


def _create_lora_run(
    client: MlflowClient,
    *,
    start_offset_ms: int = 0,
    experiment: str = EXPERIMENT,
    status: str = "FINISHED",
    tags: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    loss_steps: list[int] | None = None,
) -> str:
    """Create a LoRA run based on ``LORA_PARAMS`` and ``LORA_TAGS`` with optional overrides.

    Args:
        client: MLflow client.
        start_offset_ms: Start time relative to ``BASE_START_TIME_MS``.
        experiment: Experiment name.
        status: Terminal status of the run.
        tags: Tags merged into ``LORA_TAGS``.
        params: Params merged into ``LORA_PARAMS``.
        loss_steps: Steps of ``train/loss``. Every ``log_freq`` up to ``steps`` when omitted.

    Returns:
        The MLflow run ID.
    """
    return _create_run(
        client,
        experiment=experiment,
        start_offset_ms=start_offset_ms,
        params={**LORA_PARAMS, **(params or {})},
        tags={**LORA_TAGS, **(tags or {})},
        status=status,
        loss_steps=loss_steps or list(range(100, 3001, 100)),
    )


def test_restore_training_params_reads_all_twelve_lora_values() -> None:
    restored = restore_training_params(LORA_PARAMS, LORA_TAGS)

    assert restored == {
        "POLICY_TYPE": "pi05",
        "PRETRAINED_PATH": "lerobot/pi05_base",
        "FINETUNE_MODE": "lora",
        "LORA_R": 16,
        "LORA_ALPHA": 32,
        "STEPS": 3000,
        "BATCH_SIZE": 32,
        "SAVE_FREQ": 1000,
        "LOG_FREQ": 100,
        "NUM_WORKERS": 4,
        "COMPILE_MODEL": False,
        "IMAGE_TAG": "v1.2.3",
    }
    assert tuple(restored) == TRAINING_PARAM_KEYS


def test_restore_training_params_falls_back_and_drops_lora_values_for_full_finetune() -> None:
    params = {
        **LORA_PARAMS,
        "policy.type": "None",
        "policy.pretrained_path": "lerobot/pi0_base",
        "policy.compile_model": "true",
        "peft.r": "None",
    }
    tags = {
        **LORA_TAGS,
        "lerobot.policy_type": "pi0",
        "lerobot.finetune_mode": "full",
        "lerobot.pretrained_path": "None",
    }
    del tags["pask.image_tag"]

    restored = restore_training_params(params, tags)

    assert restored["POLICY_TYPE"] == "pi0"
    assert restored["PRETRAINED_PATH"] == "lerobot/pi0_base"
    assert restored["FINETUNE_MODE"] == "full"
    assert restored["LORA_R"] is None
    assert restored["LORA_ALPHA"] is None
    assert restored["COMPILE_MODEL"] is True
    assert restored["IMAGE_TAG"] is None


def test_restore_training_params_rejects_non_integer_steps() -> None:
    with pytest.raises(FetchError, match="STEPS"):
        restore_training_params({**LORA_PARAMS, "steps": "many"}, LORA_TAGS)


def test_extract_lr_schedule_params_picks_optimizer_and_scheduler_keys() -> None:
    assert extract_lr_schedule_params(LORA_PARAMS) == {
        "policy.optimizer_lr": "2.5e-05",
        "policy.scheduler_warmup_steps": "1000",
        "optimizer.type": "adamw",
        "scheduler.type": "cosine_decay_with_warmup",
    }


def test_fetch_run_restores_values_and_sorts_metric_history(client: MlflowClient) -> None:
    run_id = _create_lora_run(client)

    run = fetch_run(client, run_id)

    assert run.experiment_name == EXPERIMENT
    assert run.status == "FINISHED"
    assert run.start_time == BASE_START_TIME_MS
    assert run.end_time == BASE_START_TIME_MS + RUN_DURATION_MS
    assert run.training_params == restore_training_params(LORA_PARAMS, LORA_TAGS)
    assert set(run.metrics) == {"train/loss", "train/lr"}
    steps = [point.step for point in run.metrics["train/loss"]]
    assert steps == sorted(steps)
    assert steps[-1] == 3000
    assert run.planned_steps == 3000
    assert run.notes == []


def test_fetch_run_includes_eval_metrics(client: MlflowClient) -> None:
    run_id = _create_lora_run(client)
    client.log_metric(run_id, "eval/loss", 0.5, step=1000)
    client.log_metric(run_id, "eval/loss", 0.4, step=2000)

    run = fetch_run(client, run_id)

    assert [point.value for point in run.metrics["eval/loss"]] == [0.5, 0.4]


def test_fetch_run_uses_last_metric_step_for_resumed_run(client: MlflowClient) -> None:
    run_id = _create_lora_run(client, loss_steps=list(range(100, 6001, 100)))

    run = fetch_run(client, run_id)

    assert run.training_params["STEPS"] == 3000
    assert run.planned_steps == 6000
    assert run.notes == [RESUMED_RUN_NOTE]


def test_resolve_target_run_id_by_pipeline_execution_id(client: MlflowClient) -> None:
    run_id = _create_lora_run(client, experiment=OTHER_EXPERIMENT)
    _create_lora_run(client, tags={"vams.pipeline_execution_id": "exec-0002"})

    resolved = resolve_target_run_id(
        client,
        run_id=None,
        pipeline_execution_id="exec-0001_a",
        experiments=("missing-experiment", *EXPERIMENTS),
    )

    assert resolved == run_id


@pytest.mark.parametrize("pipeline_execution_id", ["a' OR '1'='1", "exec 1", ""])
def test_resolve_target_run_id_rejects_malformed_pipeline_execution_id(
    client: MlflowClient, pipeline_execution_id: str
) -> None:
    with pytest.raises(FetchError, match="pipeline_execution_id must match"):
        resolve_target_run_id(
            client,
            run_id=None,
            pipeline_execution_id=pipeline_execution_id,
            experiments=EXPERIMENTS,
        )


def test_resolve_target_run_id_rejects_unknown_and_ambiguous_ids(client: MlflowClient) -> None:
    _create_lora_run(client)
    _create_lora_run(client, start_offset_ms=1)

    with pytest.raises(FetchError, match="found 0"):
        resolve_target_run_id(
            client, run_id=None, pipeline_execution_id="exec-9999", experiments=EXPERIMENTS
        )
    with pytest.raises(FetchError, match="found 2"):
        resolve_target_run_id(
            client, run_id=None, pipeline_execution_id="exec-0001_a", experiments=EXPERIMENTS
        )


@pytest.mark.parametrize(
    ("run_id", "pipeline_execution_id"), [(None, None), ("run-1", "exec-0001_a")]
)
def test_resolve_target_run_id_requires_exactly_one_identifier(
    client: MlflowClient, run_id: str | None, pipeline_execution_id: str | None
) -> None:
    with pytest.raises(FetchError, match="exactly one"):
        resolve_target_run_id(
            client,
            run_id=run_id,
            pipeline_execution_id=pipeline_execution_id,
            experiments=EXPERIMENTS,
        )


def test_find_comparison_runs_filters_and_limits(client: MlflowClient) -> None:
    target_id = _create_lora_run(client, start_offset_ms=10_000)
    finished_ids = [
        _create_lora_run(
            client,
            start_offset_ms=offset,
            experiment=EXPERIMENTS[offset % 2],
            tags={"vams.pipeline_execution_id": f"exec-{offset}"},
        )
        for offset in range(1, 5)
    ]
    _create_lora_run(client, start_offset_ms=20_000, status="FAILED")
    _create_lora_run(
        client, start_offset_ms=30_000, tags={"vams.dataset_digest": OTHER_DATASET_DIGEST}
    )
    target = fetch_run(client, target_id)

    comparisons, criteria = find_comparison_runs(
        client, target, experiments=EXPERIMENTS, max_compare_runs=3
    )

    assert [run.run_id for run in comparisons] == finished_ids[::-1][:3]
    assert all(run.status == "FINISHED" for run in comparisons)
    assert all(run.tags["vams.dataset_digest"] == DATASET_DIGEST for run in comparisons)
    assert criteria.dataset_key == "vams.dataset_digest"
    assert criteria.dataset_value == DATASET_DIGEST
    assert criteria.experiments == list(EXPERIMENTS)
    assert criteria.max_compare_runs == 3
    assert criteria.statuses == ["FINISHED"]


def test_find_comparison_runs_falls_back_to_dataset_repo_id(client: MlflowClient) -> None:
    target_id = _create_lora_run(client, start_offset_ms=10)
    client.delete_tag(target_id, "vams.dataset_digest")
    other_id = _create_lora_run(client, tags={"pask.trigger": "manual"})
    client.delete_tag(other_id, "vams.dataset_digest")
    target = fetch_run(client, target_id)

    comparisons, criteria = find_comparison_runs(
        client, target, experiments=EXPERIMENTS, max_compare_runs=5
    )

    assert criteria.dataset_key == "lerobot.dataset_repo_id"
    assert [run.run_id for run in comparisons] == [other_id]


def test_find_comparison_runs_without_dataset_key_returns_nothing(client: MlflowClient) -> None:
    target_id = _create_lora_run(client)
    client.delete_tag(target_id, "vams.dataset_digest")
    client.delete_tag(target_id, "lerobot.dataset_repo_id")
    _create_lora_run(client, start_offset_ms=1)
    target = fetch_run(client, target_id)

    comparisons, criteria = find_comparison_runs(
        client, target, experiments=EXPERIMENTS, max_compare_runs=5
    )

    assert comparisons == []
    assert criteria.dataset_key is None
    assert criteria.dataset_value is None


def test_find_comparison_runs_rejects_quote_in_dataset_value(client: MlflowClient) -> None:
    target_id = _create_lora_run(client, tags={"vams.dataset_digest": "x' OR '1'='1"})
    target = fetch_run(client, target_id)

    with pytest.raises(FetchError, match="single quote"):
        find_comparison_runs(client, target, experiments=EXPERIMENTS, max_compare_runs=5)


def test_fetch_snapshot_round_trips(client: MlflowClient, local_mlflow_uri: str) -> None:
    target_id = _create_lora_run(client, start_offset_ms=10)
    comparison_id = _create_lora_run(client, tags={"vams.pipeline_execution_id": "exec-0000"})

    snapshot = fetch_snapshot(client, pipeline_execution_id="exec-0001_a")

    assert snapshot.target.run_id == target_id
    assert [run.run_id for run in snapshot.comparisons] == [comparison_id]
    assert snapshot.tracking_uri == local_mlflow_uri
    assert snapshot.created_at.endswith("+00:00")
    assert Snapshot.from_dict(snapshot.to_dict()) == snapshot


def test_create_client_uses_tracking_uri(local_mlflow_uri: str) -> None:
    assert tracking_uri() == local_mlflow_uri
    assert create_client().tracking_uri == local_mlflow_uri


def test_create_client_requires_tracking_uri(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MLFLOW_TRACKING_URI", raising=False)

    with pytest.raises(FetchError, match="MLFLOW_TRACKING_URI"):
        create_client()
