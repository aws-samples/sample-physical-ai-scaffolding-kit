"""End-to-end tests of the command line subcommands against a local MLflow file store."""

import json
from pathlib import Path
from typing import Any

import pytest
from mlflow import MlflowClient

from pask_training_advisor.cli import main
from pask_training_advisor.models import (
    DATASETS_FILE,
    DIAGNOSIS_FILE,
    PROPOSAL_FILE,
    REPORT_FILE,
    SCHEMA_VERSION,
    SNAPSHOT_FILE,
    VAMS_PARAMETERS_FILE,
)

EXPERIMENT = "vams-training"
OTHER_EXPERIMENT = "lerobot-pi"
PIPELINE_ID = "pask-lerobot-openpi"
TEMPLATE_ID = "pask-lerobot-openpi-default"
PIPELINE_EXECUTION_ID = "exec-0001_a"
DATASET_DIGEST = "sha256:" + "ab" * 32
BASE_START_TIME_MS = 1_790_000_000_000
RUN_DURATION_MS = 3_600_000
LOG_STEPS = list(range(100, 3001, 100))
BATCH_SIZE = 32

RUN_PARAMS = {
    "policy.type": "pi05",
    "policy.pretrained_path": "lerobot/pi05_base",
    "policy.compile_model": "False",
    "steps": "3000",
    "batch_size": str(BATCH_SIZE),
    "save_freq": "1000",
    "log_freq": "100",
    "num_workers": "4",
    "peft.r": "16",
    "peft.lora_alpha": "32",
}
RUN_TAGS = {
    "lerobot.policy_type": "pi05",
    "lerobot.finetune_mode": "lora",
    "lerobot.pretrained_path": "lerobot/pi05_base",
    "lerobot.dataset_repo_id": "pask/test-dataset",
    "pask.image_tag": "v1.2.3",
    "pask.trigger": "vams",
    "vams.dataset_digest": DATASET_DIGEST,
}


def _create_run(client: MlflowClient, *, start_offset_ms: int, tags: dict[str, str]) -> str:
    """Create a finished run with the metrics every figure needs.

    Args:
        client: MLflow client bound to the local file store.
        start_offset_ms: Start time relative to ``BASE_START_TIME_MS``.
        tags: Tags merged into ``RUN_TAGS``.

    Returns:
        The MLflow run ID.
    """
    experiment_id = client.get_experiment_by_name(EXPERIMENT).experiment_id
    start_time = BASE_START_TIME_MS + start_offset_ms
    run_id = client.create_run(
        experiment_id, start_time=start_time, tags={**RUN_TAGS, **tags}
    ).info.run_id
    for key, value in RUN_PARAMS.items():
        client.log_param(run_id, key, value)
    for index, step in enumerate(LOG_STEPS):
        timestamp = start_time + index
        step_metrics = {
            "train/loss": 1.0 / (1.0 + step / 500.0),
            "train/grad_norm": 2.0 / (1.0 + step / 1000.0),
            "train/lr": 2.5e-5,
            "train/samples_per_s": 40.0,
            "train/gpu_mem_gb": 18.0,
            "train/samples": float(step * BATCH_SIZE),
        }
        for metric_key, value in step_metrics.items():
            client.log_metric(run_id, metric_key, value, timestamp=timestamp, step=step)
    client.set_terminated(run_id, status="FINISHED", end_time=start_time + RUN_DURATION_MS)
    return run_id


@pytest.fixture
def mlflow_runs(local_mlflow_uri: str) -> dict[str, str]:
    """Create a target run and one comparison run on the same dataset.

    Returns:
        ``{"target": run_id, "comparison": run_id}``.
    """
    client = MlflowClient(tracking_uri=local_mlflow_uri)
    for name in (EXPERIMENT, OTHER_EXPERIMENT):
        client.create_experiment(name)
    comparison_run_id = _create_run(client, start_offset_ms=0, tags={})
    target_run_id = _create_run(
        client,
        start_offset_ms=RUN_DURATION_MS * 2,
        tags={"vams.pipeline_execution_id": PIPELINE_EXECUTION_ID},
    )
    return {"target": target_run_id, "comparison": comparison_run_id}


def _run_cli(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict[str, Any]]:
    """Run the command line and parse the JSON it printed.

    Args:
        argv: Arguments without the program name.
        capsys: Pytest capture fixture.

    Returns:
        The exit code and the printed JSON object.
    """
    exit_code = main(argv)
    return exit_code, json.loads(capsys.readouterr().out)


def _write_proposal(output_dir: Path, base_run_id: str, changed: dict[str, Any]) -> None:
    """Write ``proposal.json`` that changes ``changed`` from the parameters of the test runs.

    Args:
        output_dir: Output directory of the session.
        base_run_id: Run the proposal starts from.
        changed: Parameters to change.
    """
    snapshot = json.loads((output_dir / SNAPSHOT_FILE).read_text(encoding="utf-8"))
    base_params = snapshot["target"]["trainingParams"]
    proposal = {
        "schemaVersion": SCHEMA_VERSION,
        "baseRunId": base_run_id,
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
    (output_dir / PROPOSAL_FILE).write_text(json.dumps(proposal), encoding="utf-8")


def _fetch(output_dir: Path, run_id: str, capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    """Run ``fetch`` for a run ID and return its JSON result.

    Args:
        output_dir: Output directory of the session.
        run_id: Target run ID.
        capsys: Pytest capture fixture.

    Returns:
        The printed JSON object.
    """
    exit_code, result = _run_cli(
        ["fetch", "--run-id", run_id, "--output-dir", str(output_dir)], capsys
    )
    assert exit_code == 0
    return result


def test_full_session(
    mlflow_runs: dict[str, str],
    local_mlflow_uri: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output_dir = tmp_path / "session"
    target_run_id = mlflow_runs["target"]

    exit_code, fetched = _run_cli(
        [
            "fetch",
            "--pipeline-execution-id",
            PIPELINE_EXECUTION_ID,
            "--output-dir",
            str(output_dir),
        ],
        capsys,
    )
    assert exit_code == 0
    assert fetched == {
        "ok": True,
        "outputDir": str(output_dir),
        "snapshot": str(output_dir / SNAPSHOT_FILE),
        "targetRunId": target_run_id,
        "comparisonRunIds": [mlflow_runs["comparison"]],
    }
    assert local_mlflow_uri not in json.dumps(fetched)

    exit_code, analyzed = _run_cli(["analyze", "--output-dir", str(output_dir)], capsys)
    assert exit_code == 0
    assert analyzed["ok"] is True
    assert analyzed["targetRunId"] == target_run_id
    assert isinstance(analyzed["labels"], list)
    assert [run["runId"] for run in analyzed["comparisons"]] == [mlflow_runs["comparison"]]
    assert (output_dir / DIAGNOSIS_FILE).is_file()

    _write_proposal(output_dir, target_run_id, {"STEPS": 6000})
    exit_code, validated = _run_cli(
        ["validate-proposal", "--output-dir", str(output_dir), "--pipeline-id", PIPELINE_ID],
        capsys,
    )
    assert exit_code == 0
    assert validated["ok"] is True
    assert validated["vamsParameters"] == str(output_dir / VAMS_PARAMETERS_FILE)
    vams_parameters = json.loads((output_dir / VAMS_PARAMETERS_FILE).read_text(encoding="utf-8"))
    assert vams_parameters[PIPELINE_ID]["templateId"] == TEMPLATE_ID
    tags = {tag["key"]: tag["value"] for tag in vams_parameters[PIPELINE_ID]["templateTags"]}
    assert tags["STEPS"] == 6000

    exit_code, plotted = _run_cli(["plot", "--output-dir", str(output_dir)], capsys)
    assert exit_code == 0
    figure_names = {Path(path).name for path in plotted["figures"]}
    assert {"loss.png", "compare_loss_step.png", "compare_loss_samples.png"} <= figure_names
    assert all(Path(path).is_file() for path in plotted["figures"])

    (output_dir / REPORT_FILE).write_text("# Report\n", encoding="utf-8")
    exit_code, published = _run_cli(["publish", "--output-dir", str(output_dir)], capsys)
    assert exit_code == 0
    assert published["ok"] is True
    assert published["runId"] == target_run_id
    assert published["artifactPath"].startswith("advisor/")
    client = MlflowClient(tracking_uri=local_mlflow_uri)
    artifact_names = {
        Path(info.path).name
        for info in client.list_artifacts(target_run_id, published["artifactPath"])
    }
    assert {REPORT_FILE, SNAPSHOT_FILE, VAMS_PARAMETERS_FILE, "loss.png"} <= artifact_names


def test_validate_proposal_reports_violations_and_removes_parameters(
    mlflow_runs: dict[str, str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output_dir = tmp_path / "session"
    _fetch(output_dir, mlflow_runs["target"], capsys)
    (output_dir / VAMS_PARAMETERS_FILE).write_text("{}", encoding="utf-8")
    _write_proposal(output_dir, mlflow_runs["target"], {"BATCH_SIZE": 1000})

    exit_code, result = _run_cli(
        ["validate-proposal", "--output-dir", str(output_dir), "--pipeline-id", PIPELINE_ID],
        capsys,
    )

    assert exit_code == 1
    assert result["ok"] is False
    assert result["violations"]
    assert all(set(violation) == {"key", "rule", "message"} for violation in result["violations"])
    assert "BATCH_SIZE" in {violation["key"] for violation in result["violations"]}
    assert isinstance(result["warnings"], list)
    assert not (output_dir / VAMS_PARAMETERS_FILE).exists()


@pytest.mark.parametrize(
    ("proposal_text", "message_part"),
    [
        ("{not json", "not valid JSON"),
        (json.dumps({"schemaVersion": SCHEMA_VERSION}), "baseRunId"),
    ],
)
def test_validate_proposal_reports_unreadable_proposal_as_schema_violation(
    mlflow_runs: dict[str, str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    proposal_text: str,
    message_part: str,
) -> None:
    output_dir = tmp_path / "session"
    _fetch(output_dir, mlflow_runs["target"], capsys)
    (output_dir / PROPOSAL_FILE).write_text(proposal_text, encoding="utf-8")

    exit_code, result = _run_cli(
        ["validate-proposal", "--output-dir", str(output_dir), "--pipeline-id", PIPELINE_ID],
        capsys,
    )

    assert exit_code == 1
    assert result["ok"] is False
    assert [violation["rule"] for violation in result["violations"]] == ["schema"]
    assert message_part in result["violations"][0]["message"]
    assert not (output_dir / VAMS_PARAMETERS_FILE).exists()


def test_validate_proposal_requires_pipeline_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["validate-proposal", "--output-dir", str(tmp_path)])

    assert raised.value.code == 2
    assert "--pipeline-id" in capsys.readouterr().err


def test_fetch_rejects_both_target_identifiers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as raised:
        main(
            [
                "fetch",
                "--run-id",
                "run-1",
                "--pipeline-execution-id",
                PIPELINE_EXECUTION_ID,
                "--output-dir",
                str(tmp_path),
            ]
        )

    assert raised.value.code == 2
    assert "not allowed with argument" in capsys.readouterr().err


def test_fetch_requires_a_target_identifier(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["fetch", "--output-dir", str(tmp_path)])

    assert raised.value.code == 2
    assert "one of the arguments" in capsys.readouterr().err


def test_fetch_reports_unknown_pipeline_execution_id(
    mlflow_runs: dict[str, str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code, result = _run_cli(
        [
            "fetch",
            "--pipeline-execution-id",
            "exec-unknown",
            "--output-dir",
            str(tmp_path / "session"),
        ],
        capsys,
    )

    assert exit_code == 2
    assert result["ok"] is False
    assert "FetchError" in result["error"]


def test_analyze_reports_missing_snapshot(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code, result = _run_cli(["analyze", "--output-dir", str(tmp_path)], capsys)

    assert exit_code == 2
    assert result["ok"] is False
    assert SNAPSHOT_FILE in result["error"]


def test_publish_requires_report(
    mlflow_runs: dict[str, str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output_dir = tmp_path / "session"
    _fetch(output_dir, mlflow_runs["target"], capsys)

    exit_code, result = _run_cli(["publish", "--output-dir", str(output_dir)], capsys)

    assert exit_code == 2
    assert "PublishError" in result["error"]


def test_publish_requires_tracking_uri(
    mlflow_runs: dict[str, str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_dir = tmp_path / "session"
    _fetch(output_dir, mlflow_runs["target"], capsys)
    (output_dir / REPORT_FILE).write_text("# Report\n", encoding="utf-8")
    monkeypatch.delenv("MLFLOW_TRACKING_URI")

    exit_code, result = _run_cli(["publish", "--output-dir", str(output_dir)], capsys)

    assert exit_code == 2
    assert "MLFLOW_TRACKING_URI" in result["error"]


def _write_datasets(output_dir: Path, target_run_id: str) -> None:
    """Write a ``datasets.json`` with one dataset of a single episode.

    Args:
        output_dir: Output directory of the session.
        target_run_id: Target run ID recorded in the document.
    """
    document = {
        "schemaVersion": 1,
        "targetRunId": target_run_id,
        "assets": [
            {
                "databaseId": "db-1",
                "assetId": "asset-1",
                "assetName": "asset-1-name",
                "status": "active",
                "roles": ["input"],
                "metadata": {
                    "lerobot_codebase_version": {"type": "string", "value": "v3.0"},
                    "lerobot_total_episodes": {"type": "number", "value": "1"},
                },
            }
        ],
        "snapshotRuns": [],
        "runsUsingAssets": [],
    }
    (output_dir / DATASETS_FILE).write_text(json.dumps(document), encoding="utf-8")


def test_analyze_adds_datasets_when_datasets_json_exists(
    mlflow_runs: dict[str, str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output_dir = tmp_path / "session"
    _fetch(output_dir, mlflow_runs["target"], capsys)
    _write_datasets(output_dir, mlflow_runs["target"])

    exit_code, result = _run_cli(["analyze", "--output-dir", str(output_dir)], capsys)

    assert exit_code == 0
    assert result["datasets"] == str(output_dir / DATASETS_FILE)
    assert result["datasetExclusions"] == 1
    diagnosis = json.loads((output_dir / DIAGNOSIS_FILE).read_text(encoding="utf-8"))
    assert diagnosis["datasets"]["datasetExclusions"][0]["reasons"] == ["few_episodes"]


def test_analyze_without_datasets_json_writes_null_datasets(
    mlflow_runs: dict[str, str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output_dir = tmp_path / "session"
    _fetch(output_dir, mlflow_runs["target"], capsys)

    exit_code, result = _run_cli(["analyze", "--output-dir", str(output_dir)], capsys)

    assert exit_code == 0
    assert result["datasets"] is None
    diagnosis = json.loads((output_dir / DIAGNOSIS_FILE).read_text(encoding="utf-8"))
    assert diagnosis["datasets"] is None


def test_analyze_rejects_datasets_of_another_run(
    mlflow_runs: dict[str, str], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output_dir = tmp_path / "session"
    _fetch(output_dir, mlflow_runs["target"], capsys)
    _write_datasets(output_dir, mlflow_runs["comparison"])

    exit_code, result = _run_cli(["analyze", "--output-dir", str(output_dir)], capsys)

    assert exit_code == 2
    assert "not for the target run" in result["error"]
