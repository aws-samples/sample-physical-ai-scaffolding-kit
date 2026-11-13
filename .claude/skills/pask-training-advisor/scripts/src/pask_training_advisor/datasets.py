"""Diagnose the VAMS datasets of ``datasets.json`` and pick the data to leave out of training.

Nothing is deleted. The candidates are meant to be removed from the next training input through
a VAMS curation view: episodes are left out of the view, datasets are not used as input.

Episode candidates come from the VAMS review (``lerobot_review_episodes``). The per-episode
quality flags live in the quality parquet file of the asset, not in its metadata, so only their
counts (``lerobot_quality_flag_counts``) are reported.
"""

import json
from pathlib import Path
from typing import Any

from pask_training_advisor.models import CONFIG_DIR, SCHEMA_VERSION, SchemaError, read_json

DATASET_THRESHOLDS_FILE = "dataset_thresholds.json"
DATASET_THRESHOLD_KEYS: tuple[str, ...] = ("fewEpisodesMax",)

KIND_DATASET = "dataset"
KIND_VIEW = "view"
KIND_NON_LEROBOT = "non_lerobot"
FAILED_STATUSES = frozenset({"FAILED", "KILLED"})

REASON_ONLY_FAILED_RUNS = "only_failed_runs"
REASON_NOT_LEROBOT = "not_lerobot"
REASON_STATS_MISSING = "stats_missing"
REASON_STATS_COUNT_MISMATCH = "stats_count_mismatch"
REASON_FEW_EPISODES = "few_episodes"
REASON_REVIEW_EXCLUDE = "review_exclude"
REASON_REVIEW_FAILURE = "review_failure"
FAILURE_VERDICT = "failure"

TYPE_NUMBER = "number"
TYPE_BOOLEAN = "boolean"
TYPE_JSON = "json"
BOOLEAN_TEXTS: dict[str, bool] = {"true": True, "false": False}

FEATURE_KEYS: dict[str, str] = {
    "totalEpisodes": "lerobot_total_episodes",
    "totalFrames": "lerobot_total_frames",
    "totalDurationSec": "lerobot_total_duration_sec",
    "fps": "lerobot_fps",
    "totalTasks": "lerobot_total_tasks",
    "tasks": "lerobot_tasks",
    "robotType": "lerobot_robot_type",
    "codebaseVersion": "lerobot_codebase_version",
    "cameraKeys": "lerobot_camera_keys",
    "stateDim": "lerobot_state_dim",
    "actionDim": "lerobot_action_dim",
    "datasetKind": "lerobot_dataset_kind",
    "viewSourceAssetId": "lerobot_view_source_asset_id",
    "statsPresent": "lerobot_stats_present",
    "statsCountMatchesFrames": "lerobot_stats_count_matches_frames",
    "exportMetaFilters": "lerobot_export_meta_filters",
    "qualityVersion": "lerobot_quality_version",
    "qualityComputedAt": "lerobot_quality_computed_at",
    "qualityEpisodeCount": "lerobot_quality_episode_count",
    "qualityFlaggedCount": "lerobot_quality_flagged_count",
    "reviewUpdatedAt": "lerobot_review_updated_at",
}
FLAG_COUNTS_KEY = "lerobot_quality_flag_counts"
REVIEW_EPISODES_KEY = "lerobot_review_episodes"


def load_dataset_thresholds(path: Path | None = None) -> dict[str, Any]:
    """Read the dataset thresholds.

    Args:
        path: Thresholds file. ``CONFIG_DIR / "dataset_thresholds.json"`` when omitted.

    Returns:
        The thresholds keyed by their camelCase name, without ``_comments``.

    Raises:
        SchemaError: If a key is missing, is not an integer, or the schema version differs.
    """
    thresholds_path = CONFIG_DIR / DATASET_THRESHOLDS_FILE if path is None else path
    data = read_json(thresholds_path)
    if data.get("schemaVersion") != SCHEMA_VERSION:
        raise SchemaError(
            f"{thresholds_path.name}.schemaVersion is {data.get('schemaVersion')}, "
            f"expected {SCHEMA_VERSION}"
        )
    thresholds = {}
    for key in DATASET_THRESHOLD_KEYS:
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise SchemaError(f"{thresholds_path.name}.{key} must be an integer")
        thresholds[key] = value
    return thresholds


def parse_metadata_value(entry: dict[str, str], key: str) -> Any:
    """Convert one VAMS metadata text into a JSON value by its metadata type.

    Args:
        entry: ``{"type": ..., "value": ...}`` from ``datasets.json``.
        key: Metadata key, for the error message.

    Returns:
        None for an empty text; an int or float for ``number``; a bool for ``boolean``; the
        parsed value for ``json``; the text for every other type.

    Raises:
        SchemaError: If the text cannot be read as its type.
    """
    value_type = entry["type"]
    text = entry["value"]
    if text.strip() == "":
        return None
    if value_type == TYPE_NUMBER:
        try:
            number = float(text)
        except ValueError as error:
            raise SchemaError(f"metadata {key} is not a number: {text!r}") from error
        return int(number) if number.is_integer() else number
    if value_type == TYPE_BOOLEAN:
        if text.lower() not in BOOLEAN_TEXTS:
            raise SchemaError(f"metadata {key} is not a boolean: {text!r}")
        return BOOLEAN_TEXTS[text.lower()]
    if value_type == TYPE_JSON:
        try:
            return json.loads(text)
        except json.JSONDecodeError as error:
            raise SchemaError(f"metadata {key} is not valid JSON: {error}") from error
    return text


def _metadata_value(metadata: dict[str, dict[str, str]], key: str) -> Any:
    """Return the parsed value of a metadata key, or None when the asset does not have it.

    Args:
        metadata: Metadata of one asset from ``datasets.json``.
        key: Metadata key.

    Returns:
        The parsed value, or None.
    """
    if key not in metadata:
        return None
    return parse_metadata_value(metadata[key], key)


def asset_kind(features: dict[str, Any]) -> str:
    """Classify an asset as a LeRobot dataset, a curation view, or something else.

    Args:
        features: Features of the asset.

    Returns:
        ``view``, ``dataset`` or ``non_lerobot``.
    """
    if features["datasetKind"] == KIND_VIEW:
        return KIND_VIEW
    if features["codebaseVersion"]:
        return KIND_DATASET
    return KIND_NON_LEROBOT


def review_episodes(metadata: dict[str, dict[str, str]]) -> dict[int, dict[str, Any]]:
    """Read the review of each episode.

    Args:
        metadata: Metadata of one asset.

    Returns:
        The review record keyed by episode index. Empty when the asset was not reviewed.

    Raises:
        SchemaError: If the review is not an object keyed by episode index.
    """
    reviews = _metadata_value(metadata, REVIEW_EPISODES_KEY)
    if reviews is None:
        return {}
    if not isinstance(reviews, dict):
        raise SchemaError(f"metadata {REVIEW_EPISODES_KEY} must be a JSON object")
    try:
        return {int(index): dict(record) for index, record in reviews.items()}
    except (TypeError, ValueError) as error:
        raise SchemaError(f"metadata {REVIEW_EPISODES_KEY} has a bad episode entry") from error


def review_reasons(record: dict[str, Any]) -> list[str]:
    """Return the reasons a review gives to leave an episode out.

    Args:
        record: Review record of one episode.

    Returns:
        ``review_exclude`` when ``exclude`` is true, ``review_failure`` when the verdict is
        ``failure``. Empty when the review keeps the episode.
    """
    reasons = []
    if record.get("exclude") is True:
        reasons.append(REASON_REVIEW_EXCLUDE)
    if record.get("verdict") == FAILURE_VERDICT:
        reasons.append(REASON_REVIEW_FAILURE)
    return reasons


def _runs_of(asset_id: str, runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the runs that used an asset as input or as view source.

    Args:
        asset_id: VAMS asset ID.
        runs: ``runsUsingAssets`` of ``datasets.json``.

    Returns:
        The matching runs.
    """
    return [run for run in runs if asset_id in (run["inputAssetId"], run["sourceAssetId"])]


def describe_asset(asset: dict[str, Any], runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize one asset: features, quality flag counts, review, and the runs that used it.

    Args:
        asset: Asset of ``datasets.json``.
        runs: ``runsUsingAssets`` of ``datasets.json``.

    Returns:
        The asset summary of ``diagnosis.json``.
    """
    metadata = asset["metadata"]
    features = {name: _metadata_value(metadata, key) for name, key in FEATURE_KEYS.items()}
    reviews = review_episodes(metadata)
    features["reviewedEpisodeCount"] = len(reviews)
    flag_counts = _metadata_value(metadata, FLAG_COUNTS_KEY) or {}
    asset_runs = _runs_of(asset["assetId"], runs)
    status_counts: dict[str, int] = {}
    for run in asset_runs:
        status_counts[run["status"]] = status_counts.get(run["status"], 0) + 1
    return {
        "databaseId": asset["databaseId"],
        "assetId": asset["assetId"],
        "assetName": asset["assetName"],
        "status": asset["status"],
        "roles": list(asset["roles"]),
        "kind": asset_kind(features),
        "features": features,
        "flagCounts": {flag: count for flag, count in flag_counts.items() if count},
        "reviewExcludedEpisodes": sorted(
            index for index, record in reviews.items() if record.get("exclude") is True
        ),
        "reviewFailureEpisodes": sorted(
            index for index, record in reviews.items() if record.get("verdict") == FAILURE_VERDICT
        ),
        "runStatusCounts": status_counts,
        "runIds": [run["runId"] for run in asset_runs],
    }


def dataset_exclusion_reasons(
    summary: dict[str, Any], runs: list[dict[str, Any]], thresholds: dict[str, Any]
) -> list[str]:
    """Decide why a training input should not be used as input again.

    Args:
        summary: Asset summary from ``describe_asset``.
        runs: ``runsUsingAssets`` of ``datasets.json``.
        thresholds: Dataset thresholds.

    Returns:
        The reasons, empty when the input can stay.
    """
    features = summary["features"]
    reasons = []
    input_statuses = [run["status"] for run in runs if run["inputAssetId"] == summary["assetId"]]
    if input_statuses and all(status in FAILED_STATUSES for status in input_statuses):
        reasons.append(REASON_ONLY_FAILED_RUNS)
    if summary["kind"] == KIND_NON_LEROBOT:
        reasons.append(REASON_NOT_LEROBOT)
    if features["statsPresent"] is False:
        reasons.append(REASON_STATS_MISSING)
    if features["statsCountMatchesFrames"] is False:
        reasons.append(REASON_STATS_COUNT_MISMATCH)
    total_episodes = features["totalEpisodes"]
    if total_episodes is not None and total_episodes <= thresholds["fewEpisodesMax"]:
        reasons.append(REASON_FEW_EPISODES)
    return reasons


def _trained_episode_sets(datasets: dict[str, Any]) -> list[dict[str, Any]]:
    """List which episodes of which dataset each snapshot run trained on.

    Args:
        datasets: ``datasets.json`` document.

    Returns:
        One entry per (dataset asset, input asset), the target run first. ``keptEpisodes`` is
        None when the run used the whole dataset.
    """
    entries: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for run in datasets["snapshotRuns"]:
        input_asset_id = run["inputAssetId"]
        if not input_asset_id:
            continue
        view = run["view"]
        dataset_asset_id = run["sourceAssetId"] if view is not None else input_asset_id
        key = (dataset_asset_id, input_asset_id)
        if dataset_asset_id is None or key in seen:
            continue
        seen.add(key)
        entries.append(
            {
                "runId": run["runId"],
                "datasetAssetId": dataset_asset_id,
                "inputAssetId": input_asset_id,
                "keptEpisodes": None if view is None else list(view["keptEpisodes"]),
                "viewExcludedEpisodes": [] if view is None else list(view["excludedEpisodes"]),
            }
        )
    return entries


def episode_exclusions(
    datasets: dict[str, Any], summaries: dict[str, dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Pick the trained episodes the review says to leave out, and the ones already left out.

    Args:
        datasets: ``datasets.json`` document.
        summaries: Asset summaries keyed by asset ID.

    Returns:
        The candidates (episodes the run still trained on) and the episodes the view had
        already excluded.
    """
    candidates = []
    already_excluded = []
    metadata_by_asset = {asset["assetId"]: asset["metadata"] for asset in datasets["assets"]}
    for entry in _trained_episode_sets(datasets):
        dataset_asset_id = entry["datasetAssetId"]
        if dataset_asset_id not in metadata_by_asset:
            continue
        via_view = entry["keptEpisodes"] is not None
        base = {
            "runId": entry["runId"],
            "assetId": dataset_asset_id,
            "assetName": summaries[dataset_asset_id]["assetName"],
            "viewAssetId": entry["inputAssetId"] if via_view else None,
        }
        kept = None if not via_view else set(entry["keptEpisodes"])
        for index, record in sorted(review_episodes(metadata_by_asset[dataset_asset_id]).items()):
            reasons = review_reasons(record)
            if reasons and (kept is None or index in kept):
                candidates.append({**base, "episodeIndex": index, "reasons": reasons})
        for excluded in entry["viewExcludedEpisodes"]:
            already_excluded.append(
                {**base, "episodeIndex": excluded["episodeIndex"], "reasons": excluded["reasons"]}
            )
    return candidates, already_excluded


def dataset_findings(summary: dict[str, Any]) -> list[str]:
    """Explain the quality state of one dataset in sentences.

    Args:
        summary: Asset summary from ``describe_asset``.

    Returns:
        Findings about missing quality results, flags without a review, and per-episode flags.
    """
    if summary["kind"] != KIND_DATASET:
        return []
    features = summary["features"]
    name = summary["assetName"]
    findings = []
    if features["qualityComputedAt"] is None:
        findings.append(f"{name}: the VAMS episode quality pipeline has not been run")
    flagged = features["qualityFlaggedCount"]
    if flagged:
        flags = ", ".join(f"{flag}={count}" for flag, count in summary["flagCounts"].items())
        findings.append(
            f"{name}: {flagged} of {features['qualityEpisodeCount']} episodes have quality "
            f"flags ({flags}); the per-episode flags are in the quality file of the asset, "
            "not in its metadata"
        )
        if features["reviewedEpisodeCount"] == 0:
            findings.append(f"{name}: flagged episodes exist but no episode has been reviewed")
    return findings


def diagnose_datasets(datasets: dict[str, Any], thresholds: dict[str, Any]) -> dict[str, Any]:
    """Build the ``datasets`` section of ``diagnosis.json``.

    Args:
        datasets: ``datasets.json`` document.
        thresholds: Dataset thresholds.

    Returns:
        Asset summaries, dataset and episode exclusion candidates, and findings.

    Raises:
        SchemaError: If the document or a metadata value does not have the expected shape.
    """
    for key in ("schemaVersion", "assets", "snapshotRuns", "runsUsingAssets", "targetRunId"):
        if key not in datasets:
            raise SchemaError(f"datasets.json is missing the required key '{key}'")
    if datasets["schemaVersion"] != SCHEMA_VERSION:
        raise SchemaError(
            f"datasets.json.schemaVersion is {datasets['schemaVersion']}, expected {SCHEMA_VERSION}"
        )
    runs = datasets["runsUsingAssets"]
    summaries = {asset["assetId"]: describe_asset(asset, runs) for asset in datasets["assets"]}
    dataset_candidates = []
    for summary in summaries.values():
        if "input" not in summary["roles"]:
            continue
        reasons = dataset_exclusion_reasons(summary, runs, thresholds)
        if reasons:
            dataset_candidates.append(
                {
                    "assetId": summary["assetId"],
                    "assetName": summary["assetName"],
                    "reasons": reasons,
                }
            )
    episode_candidates, already_excluded = episode_exclusions(datasets, summaries)
    findings = [finding for summary in summaries.values() for finding in dataset_findings(summary)]
    if not datasets["assets"]:
        findings.append("no run of the snapshot was started from VAMS")
    return {
        "targetRunId": datasets["targetRunId"],
        "thresholds": dict(thresholds),
        "assets": list(summaries.values()),
        "datasetExclusions": dataset_candidates,
        "episodeExclusions": episode_candidates,
        "alreadyExcludedEpisodes": already_excluded,
        "findings": findings,
    }
