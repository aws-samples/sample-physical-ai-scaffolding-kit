"""Tests of reading the VAMS datasets of a snapshot through a fake VamsCLI."""

import json
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
from mlflow import MlflowClient

from pask_training_advisor.models import ComparisonCriteria, RunSnapshot, Snapshot
from pask_training_advisor.vams import (
    CURATION_MANIFEST_ARTIFACT,
    VAMS_CLI,
    VamsError,
    collect_asset_refs,
    fetch_vams_datasets,
    list_asset_metadata,
    resolve_profile,
    run_vamscli,
)

DATABASE_ID = "db-1"
DATASET_ASSET_ID = "asset-dataset"
VIEW_ASSET_ID = "asset-view"
SOURCE_ASSET_ID = "asset-source"
EXPERIMENT = "vams-training"
PROFILE = "test-profile"


def _completed(command: Sequence[str], stdout: str, returncode: int) -> subprocess.CompletedProcess:
    """Build the completed process a fake VamsCLI returns.

    Args:
        command: Command that was run.
        stdout: Standard output.
        returncode: Exit code.

    Returns:
        The completed process.
    """
    return subprocess.CompletedProcess(list(command), returncode, stdout=stdout, stderr="")


def _metadata_entry(asset_id: str, key: str, value: str, value_type: str) -> dict[str, Any]:
    """Build one entry of ``metadata asset list --json-output``.

    Args:
        asset_id: Asset ID.
        key: Metadata key.
        value: Metadata value text.
        value_type: Metadata type.

    Returns:
        The entry as VAMS returns it.
    """
    return {
        "databaseId": DATABASE_ID,
        "assetId": asset_id,
        "metadataKey": key,
        "metadataValue": value,
        "metadataValueType": value_type,
    }


def _fake_vams(calls: list[list[str]]) -> Callable[[Sequence[str]], subprocess.CompletedProcess]:
    """Return a fake VamsCLI that answers ``assets get`` and ``metadata asset list``.

    Args:
        calls: List that receives every command.

    Returns:
        The runner.
    """

    def runner(command: Sequence[str]) -> subprocess.CompletedProcess:
        calls.append(list(command))
        args = list(command[3:-1])
        if args[:2] == ["assets", "get"]:
            asset_id = args[-1]
            body = {"assetId": asset_id, "assetName": f"{asset_id}-name", "status": "active"}
        elif args[:3] == ["metadata", "asset", "list"]:
            asset_id = args[args.index("-a") + 1]
            body = {
                "metadata": [
                    _metadata_entry(asset_id, "lerobot_total_episodes", "3", "number"),
                    _metadata_entry(asset_id, "lerobot_review_episodes", "", "json"),
                ]
            }
        else:
            return _completed(command, json.dumps({"error": "unexpected"}), 1)
        return _completed(command, json.dumps(body), 0)

    return runner


def test_resolve_profile_prefers_argument_then_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VAMS_CLI_PROFILE", raising=False)
    assert resolve_profile(None) == "default"
    monkeypatch.setenv("VAMS_CLI_PROFILE", "from-env")
    assert resolve_profile(None) == "from-env"
    assert resolve_profile("from-arg") == "from-arg"


def test_run_vamscli_adds_profile_and_json_output() -> None:
    calls: list[list[str]] = []

    def runner(command: Sequence[str]) -> subprocess.CompletedProcess:
        calls.append(list(command))
        return _completed(command, '{"Items": []}', 0)

    assert run_vamscli(runner, PROFILE, ["database", "list"]) == {"Items": []}
    assert calls == [[VAMS_CLI, "--profile", PROFILE, "database", "list", "--json-output"]]


def test_run_vamscli_raises_with_the_error_of_vamscli() -> None:
    def runner(command: Sequence[str]) -> subprocess.CompletedProcess:
        return _completed(command, '{"error": "Authentication token has expired"}', 1)

    with pytest.raises(VamsError, match="Authentication token has expired"):
        run_vamscli(runner, PROFILE, ["database", "list"])


def test_run_vamscli_rejects_output_that_is_not_a_json_object() -> None:
    def runner(command: Sequence[str]) -> subprocess.CompletedProcess:
        return _completed(command, "[1, 2]", 0)

    with pytest.raises(VamsError, match="not a JSON object"):
        run_vamscli(runner, PROFILE, ["database", "list"])


def test_list_asset_metadata_keeps_type_and_text() -> None:
    metadata = list_asset_metadata(_fake_vams([]), PROFILE, DATABASE_ID, DATASET_ASSET_ID)

    assert metadata == {
        "lerobot_total_episodes": {"type": "number", "value": "3"},
        "lerobot_review_episodes": {"type": "json", "value": ""},
    }


def _snapshot(target: RunSnapshot, comparisons: list[RunSnapshot]) -> Snapshot:
    """Wrap runs in a snapshot.

    Args:
        target: Target run.
        comparisons: Comparison runs.

    Returns:
        The snapshot.
    """
    return Snapshot(
        schema_version=1,
        created_at="2026-10-27T00:00:00Z",
        tracking_uri="file:///mlruns",
        target=target,
        comparisons=comparisons,
        criteria=ComparisonCriteria(
            dataset_key=None,
            dataset_value=None,
            experiments=[EXPERIMENT],
            max_compare_runs=5,
            statuses=["FINISHED"],
        ),
    )


def _view_tags() -> dict[str, str]:
    """Return the VAMS tags of a run whose input was a curation view.

    Returns:
        The tags.
    """
    return {
        "vams.input.database_id": DATABASE_ID,
        "vams.input.asset_id": VIEW_ASSET_ID,
        "vams.input.kind": "view",
        "vams.source.database_id": DATABASE_ID,
        "vams.source.asset_id": SOURCE_ASSET_ID,
    }


def _dataset_tags() -> dict[str, str]:
    """Return the VAMS tags of a run whose input was a dataset.

    Returns:
        The tags.
    """
    return {
        "vams.input.database_id": DATABASE_ID,
        "vams.input.asset_id": DATASET_ASSET_ID,
        "vams.input.kind": "dataset",
    }


def test_collect_asset_refs_merges_roles_and_skips_manual_runs(
    make_run_snapshot: Callable[..., RunSnapshot],
) -> None:
    snapshot = _snapshot(
        make_run_snapshot(tags=_view_tags()),
        [
            make_run_snapshot(run_id="run-dataset", tags=_dataset_tags()),
            make_run_snapshot(run_id="run-manual", tags={}),
            make_run_snapshot(run_id="run-view-again", tags=_view_tags()),
        ],
    )

    refs = collect_asset_refs(snapshot)

    assert [(ref.asset_id, ref.roles) for ref in refs] == [
        (VIEW_ASSET_ID, ("input",)),
        (SOURCE_ASSET_ID, ("view_source",)),
        (DATASET_ASSET_ID, ("input",)),
    ]


def _create_run(client: MlflowClient, tags: dict[str, str], status: str, start: int) -> str:
    """Create a terminated run with tags.

    Args:
        client: MLflow client.
        tags: Tags of the run.
        status: Terminal status.
        start: Start time in epoch milliseconds.

    Returns:
        The run ID.
    """
    experiment_id = client.get_experiment_by_name(EXPERIMENT).experiment_id
    run_id = client.create_run(experiment_id, start_time=start, tags=tags).info.run_id
    client.set_terminated(run_id, status=status, end_time=start + 1000)
    return run_id


def test_fetch_vams_datasets_reads_assets_runs_and_view_manifest(
    local_mlflow_uri: str,
    tmp_path: Path,
    make_run_snapshot: Callable[..., RunSnapshot],
) -> None:
    client = MlflowClient(tracking_uri=local_mlflow_uri)
    client.create_experiment(EXPERIMENT)
    view_run_id = _create_run(client, _view_tags(), "FINISHED", 2000)
    failed_run_id = _create_run(client, _dataset_tags(), "FAILED", 1000)
    manifest_dir = tmp_path / "manifest" / "vams"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "curation_manifest.json").write_text(
        json.dumps(
            {
                "mode": "view",
                "episodes": [0, 2],
                "excluded_episodes": [{"episode_index": 1, "reasons": ["review"]}],
            }
        ),
        encoding="utf-8",
    )
    client.log_artifact(view_run_id, str(manifest_dir / "curation_manifest.json"), "vams")
    snapshot = _snapshot(
        make_run_snapshot(run_id=view_run_id, tags=_view_tags()),
        [make_run_snapshot(run_id=failed_run_id, status="FAILED", tags=_dataset_tags())],
    )
    calls: list[list[str]] = []

    datasets = fetch_vams_datasets(snapshot, client, _fake_vams(calls), PROFILE)

    assert datasets["targetRunId"] == view_run_id
    assert datasets["vamsProfile"] == PROFILE
    assert [asset["assetId"] for asset in datasets["assets"]] == [
        VIEW_ASSET_ID,
        SOURCE_ASSET_ID,
        DATASET_ASSET_ID,
    ]
    assert datasets["assets"][0]["assetName"] == f"{VIEW_ASSET_ID}-name"
    assert datasets["assets"][0]["metadata"]["lerobot_total_episodes"]["value"] == "3"
    assert datasets["snapshotRuns"][0]["view"] == {
        "keptEpisodes": [0, 2],
        "excludedEpisodes": [{"episodeIndex": 1, "reasons": ["review"]}],
    }
    assert datasets["snapshotRuns"][1]["view"] is None
    assert [(run["runId"], run["status"]) for run in datasets["runsUsingAssets"]] == [
        (view_run_id, "FINISHED"),
        (failed_run_id, "FAILED"),
    ]
    assert all(call[1:3] == ["--profile", PROFILE] for call in calls)
    assert CURATION_MANIFEST_ARTIFACT == "vams/curation_manifest.json"


def test_fetch_vams_datasets_without_vams_runs_reads_nothing(
    local_mlflow_uri: str, make_run_snapshot: Callable[..., RunSnapshot]
) -> None:
    client = MlflowClient(tracking_uri=local_mlflow_uri)
    client.create_experiment(EXPERIMENT)
    calls: list[list[str]] = []

    datasets = fetch_vams_datasets(
        _snapshot(make_run_snapshot(tags={}), []), client, _fake_vams(calls), PROFILE
    )

    assert datasets["assets"] == []
    assert datasets["runsUsingAssets"] == []
    assert calls == []
