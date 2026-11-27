"""Tests of the dataset diagnosis and the exclusion candidates."""

import json
from pathlib import Path
from typing import Any

import pytest

from pask_training_advisor.datasets import (
    REASON_FEW_EPISODES,
    REASON_NOT_LEROBOT,
    REASON_ONLY_FAILED_RUNS,
    REASON_REVIEW_EXCLUDE,
    REASON_REVIEW_FAILURE,
    REASON_STATS_COUNT_MISMATCH,
    REASON_STATS_MISSING,
    diagnose_datasets,
    load_dataset_thresholds,
    parse_metadata_value,
)
from pask_training_advisor.models import SchemaError

THRESHOLDS = {"fewEpisodesMax": 1}
DATABASE_ID = "db-1"
TARGET_RUN_ID = "run-target"


def _entry(value: str, value_type: str) -> dict[str, str]:
    """Build one metadata entry of ``datasets.json``.

    Args:
        value: Metadata value text.
        value_type: Metadata type.

    Returns:
        The entry.
    """
    return {"type": value_type, "value": value}


def _dataset_metadata(**overrides: dict[str, str]) -> dict[str, dict[str, str]]:
    """Build the metadata of a healthy LeRobot dataset with five episodes.

    Args:
        **overrides: Metadata entries that replace the defaults.

    Returns:
        The metadata keyed by metadata key.
    """
    metadata = {
        "lerobot_codebase_version": _entry("v3.0", "string"),
        "lerobot_total_episodes": _entry("5", "number"),
        "lerobot_total_frames": _entry("500", "number"),
        "lerobot_stats_present": _entry("true", "boolean"),
        "lerobot_stats_count_matches_frames": _entry("true", "boolean"),
        "lerobot_dataset_kind": _entry("", "string"),
        "lerobot_quality_computed_at": _entry("2026-10-23T06:16:03Z", "date"),
        "lerobot_quality_episode_count": _entry("5", "number"),
        "lerobot_quality_flagged_count": _entry("0", "number"),
        "lerobot_quality_flag_counts": _entry('{"jerk_high": 0}', "json"),
        "lerobot_review_episodes": _entry("", "json"),
    }
    metadata.update(overrides)
    return metadata


def _asset(asset_id: str, roles: list[str], metadata: dict[str, Any]) -> dict[str, Any]:
    """Build one asset of ``datasets.json``.

    Args:
        asset_id: Asset ID.
        roles: Roles of the asset.
        metadata: Metadata of the asset.

    Returns:
        The asset.
    """
    return {
        "databaseId": DATABASE_ID,
        "assetId": asset_id,
        "assetName": f"{asset_id}-name",
        "status": "active",
        "roles": roles,
        "metadata": metadata,
    }


def _run(run_id: str, status: str, input_asset_id: str, source_asset_id: str | None) -> dict:
    """Build one run of ``runsUsingAssets``.

    Args:
        run_id: Run ID.
        status: Run status.
        input_asset_id: Input asset ID.
        source_asset_id: View source asset ID, or None.

    Returns:
        The run reference.
    """
    return {
        "runId": run_id,
        "status": status,
        "inputAssetId": input_asset_id,
        "sourceAssetId": source_asset_id,
    }


def _document(
    assets: list[dict[str, Any]],
    snapshot_runs: list[dict[str, Any]],
    runs: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a ``datasets.json`` document.

    Args:
        assets: Assets.
        snapshot_runs: ``snapshotRuns``.
        runs: ``runsUsingAssets``.

    Returns:
        The document.
    """
    return {
        "schemaVersion": 1,
        "targetRunId": TARGET_RUN_ID,
        "assets": assets,
        "snapshotRuns": snapshot_runs,
        "runsUsingAssets": runs,
    }


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        (_entry("", "number"), None),
        (_entry("60", "number"), 60),
        (_entry("62.433", "number"), 62.433),
        (_entry("false", "boolean"), False),
        (_entry('{"a": 1}', "json"), {"a": 1}),
        (_entry("text", "multiline_string"), "text"),
    ],
)
def test_parse_metadata_value_by_type(entry: dict[str, str], expected: object) -> None:
    assert parse_metadata_value(entry, "key") == expected


@pytest.mark.parametrize(
    "entry", [_entry("abc", "number"), _entry("yes", "boolean"), _entry("{", "json")]
)
def test_parse_metadata_value_rejects_bad_text(entry: dict[str, str]) -> None:
    with pytest.raises(SchemaError):
        parse_metadata_value(entry, "key")


def test_healthy_dataset_has_no_candidates() -> None:
    document = _document(
        [_asset("a", ["input"], _dataset_metadata())],
        [{"runId": TARGET_RUN_ID, "inputAssetId": "a", "sourceAssetId": None, "view": None}],
        [_run(TARGET_RUN_ID, "FINISHED", "a", None)],
    )

    result = diagnose_datasets(document, THRESHOLDS)

    assert result["datasetExclusions"] == []
    assert result["episodeExclusions"] == []
    assert result["findings"] == []
    assert result["assets"][0]["kind"] == "dataset"
    assert result["assets"][0]["runStatusCounts"] == {"FINISHED": 1}


def test_dataset_exclusion_rules() -> None:
    metadata = _dataset_metadata(
        lerobot_total_episodes=_entry("1", "number"),
        lerobot_stats_present=_entry("false", "boolean"),
        lerobot_stats_count_matches_frames=_entry("false", "boolean"),
    )
    non_lerobot = {"lerobot_codebase_version": _entry("", "string")}
    document = _document(
        [_asset("a", ["input"], metadata), _asset("b", ["input"], non_lerobot)],
        [],
        [
            _run("r1", "FAILED", "a", None),
            _run("r2", "KILLED", "a", None),
            _run("r3", "FINISHED", "b", None),
        ],
    )

    result = diagnose_datasets(document, THRESHOLDS)

    assert result["datasetExclusions"] == [
        {
            "assetId": "a",
            "assetName": "a-name",
            "reasons": [
                REASON_ONLY_FAILED_RUNS,
                REASON_STATS_MISSING,
                REASON_STATS_COUNT_MISMATCH,
                REASON_FEW_EPISODES,
            ],
        },
        {"assetId": "b", "assetName": "b-name", "reasons": [REASON_NOT_LEROBOT]},
    ]


def test_view_source_is_not_a_dataset_exclusion_candidate() -> None:
    metadata = _dataset_metadata(lerobot_total_episodes=_entry("1", "number"))
    document = _document([_asset("src", ["view_source"], metadata)], [], [])

    assert diagnose_datasets(document, THRESHOLDS)["datasetExclusions"] == []


def test_episode_candidates_of_a_whole_dataset_come_from_the_review() -> None:
    reviews = {
        "0": {"verdict": "success", "exclude": False},
        "1": {"verdict": "failure", "exclude": False},
        "2": {"verdict": "success", "exclude": True},
    }
    metadata = _dataset_metadata(lerobot_review_episodes=_entry(json.dumps(reviews), "json"))
    document = _document(
        [_asset("a", ["input"], metadata)],
        [{"runId": TARGET_RUN_ID, "inputAssetId": "a", "sourceAssetId": None, "view": None}],
        [_run(TARGET_RUN_ID, "FINISHED", "a", None)],
    )

    result = diagnose_datasets(document, THRESHOLDS)

    assert [(e["episodeIndex"], e["reasons"]) for e in result["episodeExclusions"]] == [
        (1, [REASON_REVIEW_FAILURE]),
        (2, [REASON_REVIEW_EXCLUDE]),
    ]
    assert result["episodeExclusions"][0]["viewAssetId"] is None
    assert result["assets"][0]["reviewFailureEpisodes"] == [1]
    assert result["assets"][0]["reviewExcludedEpisodes"] == [2]


def test_episode_candidates_of_a_view_skip_episodes_the_view_left_out() -> None:
    reviews = {"1": {"verdict": "failure", "exclude": True}, "2": {"verdict": "failure"}}
    source = _dataset_metadata(lerobot_review_episodes=_entry(json.dumps(reviews), "json"))
    view = {
        "lerobot_codebase_version": _entry("v3.0", "string"),
        "lerobot_dataset_kind": _entry("view", "string"),
        "lerobot_total_episodes": _entry("4", "number"),
    }
    document = _document(
        [_asset("view", ["input"], view), _asset("src", ["view_source"], source)],
        [
            {
                "runId": TARGET_RUN_ID,
                "inputAssetId": "view",
                "sourceAssetId": "src",
                "view": {
                    "keptEpisodes": [0, 2, 3, 4],
                    "excludedEpisodes": [{"episodeIndex": 1, "reasons": ["review"]}],
                },
            }
        ],
        [_run(TARGET_RUN_ID, "FINISHED", "view", "src")],
    )

    result = diagnose_datasets(document, THRESHOLDS)

    assert [(e["episodeIndex"], e["viewAssetId"]) for e in result["episodeExclusions"]] == [
        (2, "view")
    ]
    assert result["alreadyExcludedEpisodes"][0]["episodeIndex"] == 1
    assert result["assets"][0]["kind"] == "view"


def test_findings_for_flags_without_review_and_missing_quality() -> None:
    flagged = _dataset_metadata(
        lerobot_quality_flagged_count=_entry("2", "number"),
        lerobot_quality_flag_counts=_entry('{"jerk_high": 2, "idle_high": 0}', "json"),
    )
    no_quality = _dataset_metadata(lerobot_quality_computed_at=_entry("", "date"))
    document = _document(
        [_asset("a", ["input"], flagged), _asset("b", ["input"], no_quality)], [], []
    )

    result = diagnose_datasets(document, THRESHOLDS)

    assert result["assets"][0]["flagCounts"] == {"jerk_high": 2}
    assert any("2 of 5 episodes have quality flags (jerk_high=2)" in f for f in result["findings"])
    assert any("no episode has been reviewed" in f for f in result["findings"])
    assert any("b-name" in f and "has not been run" in f for f in result["findings"])


def test_no_vams_run_is_a_finding() -> None:
    result = diagnose_datasets(_document([], [], []), THRESHOLDS)

    assert result["findings"] == ["no run of the snapshot was started from VAMS"]


def test_diagnose_datasets_rejects_missing_key() -> None:
    document = _document([], [], [])
    del document["snapshotRuns"]

    with pytest.raises(SchemaError, match="snapshotRuns"):
        diagnose_datasets(document, THRESHOLDS)


def test_load_dataset_thresholds_reads_the_shipped_file() -> None:
    assert load_dataset_thresholds() == THRESHOLDS


def test_load_dataset_thresholds_rejects_non_integer(tmp_path: Path) -> None:
    path = tmp_path / "thresholds.json"
    path.write_text('{"schemaVersion": 1, "fewEpisodesMax": 1.5}', encoding="utf-8")

    with pytest.raises(SchemaError, match="fewEpisodesMax"):
        load_dataset_thresholds(path)
