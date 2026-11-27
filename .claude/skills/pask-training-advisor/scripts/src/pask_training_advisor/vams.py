"""Read the VAMS datasets that the runs of a snapshot trained on, through VamsCLI.

Only the assets referenced by the target and comparison runs are read: the input asset of each
run (tag ``vams.input.asset_id``) and, for curation views, the source asset of the view (tag
``vams.source.asset_id``). VamsCLI is called with read-only commands and ``--json-output``; its
saved login of the chosen profile is used, so no VAMS credentials pass through this module.
"""

import json
import os
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mlflow import MlflowClient
from mlflow.entities import Run

from pask_training_advisor.fetch import find_experiment_ids, quote_filter_value
from pask_training_advisor.models import SCHEMA_VERSION, RunSnapshot, Snapshot

VAMS_CLI = "vamscli"
VAMS_PROFILE_ENV = "VAMS_CLI_PROFILE"
FALLBACK_VAMS_PROFILE = "default"

INPUT_DATABASE_TAG = "vams.input.database_id"
INPUT_ASSET_TAG = "vams.input.asset_id"
INPUT_ASSET_NAME_TAG = "vams.input.asset_name"
INPUT_KIND_TAG = "vams.input.kind"
SOURCE_DATABASE_TAG = "vams.source.database_id"
SOURCE_ASSET_TAG = "vams.source.asset_id"
VIEW_KIND = "view"
CURATION_MANIFEST_ARTIFACT = "vams/curation_manifest.json"

ROLE_INPUT = "input"
ROLE_VIEW_SOURCE = "view_source"
ROLE_TAGS: dict[str, tuple[str, str]] = {
    ROLE_INPUT: (INPUT_DATABASE_TAG, INPUT_ASSET_TAG),
    ROLE_VIEW_SOURCE: (SOURCE_DATABASE_TAG, SOURCE_ASSET_TAG),
}

CliRunner = Callable[[Sequence[str]], subprocess.CompletedProcess]


class VamsError(RuntimeError):
    """Raised when VamsCLI fails or prints something that is not the expected JSON."""


@dataclass(frozen=True)
class AssetRef:
    """One VAMS asset referenced by the runs of a snapshot.

    Attributes:
        database_id: VAMS database ID.
        asset_id: VAMS asset ID.
        roles: How the runs used the asset: ``input`` and/or ``view_source``.
    """

    database_id: str
    asset_id: str
    roles: tuple[str, ...]


def resolve_profile(profile: str | None) -> str:
    """Decide the VamsCLI profile.

    Args:
        profile: Profile given on the command line, or None.

    Returns:
        ``profile`` when given, else ``VAMS_CLI_PROFILE`` when set, else ``default``.
    """
    if profile is not None:
        return profile
    return os.environ.get(VAMS_PROFILE_ENV) or FALLBACK_VAMS_PROFILE


def subprocess_runner(command: Sequence[str]) -> subprocess.CompletedProcess:
    """Run a command and capture its text output.

    Args:
        command: Command and arguments.

    Returns:
        The completed process. A non-zero exit code is returned, not raised.

    Raises:
        VamsError: If the VamsCLI executable is not found.
    """
    try:
        return subprocess.run(list(command), capture_output=True, text=True, check=False)
    except FileNotFoundError as error:
        raise VamsError(f"{VAMS_CLI} is not installed or not on PATH") from error


def _error_text(process: subprocess.CompletedProcess) -> str:
    """Extract the error message VamsCLI printed.

    Args:
        process: Failed VamsCLI process.

    Returns:
        The ``error`` value of the JSON output when present, else the stripped output.
    """
    try:
        data = json.loads(process.stdout)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict) and isinstance(data.get("error"), str):
        return data["error"]
    return (process.stdout.strip() or process.stderr.strip())[-1000:]


def run_vamscli(runner: CliRunner, profile: str, args: Sequence[str]) -> dict[str, Any]:
    """Run one VamsCLI command with ``--json-output`` and parse its output.

    Args:
        runner: Function that runs the command.
        profile: VamsCLI profile.
        args: Command and options after the profile, without ``--json-output``.

    Returns:
        The JSON object printed by VamsCLI.

    Raises:
        VamsError: If the command fails or the output is not a JSON object.
    """
    command = [VAMS_CLI, "--profile", profile, *args, "--json-output"]
    process = runner(command)
    if process.returncode != 0:
        raise VamsError(
            f"'{' '.join(args)}' failed with exit code {process.returncode}: {_error_text(process)}"
        )
    try:
        data = json.loads(process.stdout)
    except json.JSONDecodeError as error:
        raise VamsError(f"'{' '.join(args)}' did not print JSON: {error}") from error
    if not isinstance(data, dict):
        raise VamsError(f"'{' '.join(args)}' printed {type(data).__name__}, not a JSON object")
    return data


def get_asset(runner: CliRunner, profile: str, database_id: str, asset_id: str) -> dict[str, Any]:
    """Read the name and status of an asset.

    Args:
        runner: Function that runs VamsCLI.
        profile: VamsCLI profile.
        database_id: VAMS database ID.
        asset_id: VAMS asset ID.

    Returns:
        ``{"assetName": ..., "status": ...}`` as returned by VAMS.

    Raises:
        VamsError: If VamsCLI fails or a key is missing.
    """
    data = run_vamscli(runner, profile, ["assets", "get", "-d", database_id, asset_id])
    for key in ("assetName", "status"):
        if not isinstance(data.get(key), str):
            raise VamsError(f"assets get {asset_id} returned no string '{key}'")
    return {"assetName": data["assetName"], "status": data["status"]}


def list_asset_metadata(
    runner: CliRunner, profile: str, database_id: str, asset_id: str
) -> dict[str, dict[str, str]]:
    """Read every metadata entry of an asset.

    Args:
        runner: Function that runs VamsCLI.
        profile: VamsCLI profile.
        database_id: VAMS database ID.
        asset_id: VAMS asset ID.

    Returns:
        ``{metadataKey: {"type": metadataValueType, "value": metadataValue}}``. Values stay
        the text VAMS stores; an empty text means the key has no value.

    Raises:
        VamsError: If VamsCLI fails or the output does not hold the metadata list.
    """
    data = run_vamscli(
        runner, profile, ["metadata", "asset", "list", "-d", database_id, "-a", asset_id]
    )
    entries = data.get("metadata")
    if not isinstance(entries, list):
        raise VamsError(f"metadata asset list {asset_id} returned no 'metadata' list")
    metadata: dict[str, dict[str, str]] = {}
    for entry in entries:
        key = entry.get("metadataKey") if isinstance(entry, dict) else None
        if not isinstance(key, str):
            raise VamsError(f"metadata asset list {asset_id} returned an entry without a key")
        value = entry.get("metadataValue")
        metadata[key] = {
            "type": str(entry.get("metadataValueType")),
            "value": "" if value is None else str(value),
        }
    return metadata


def list_databases(runner: CliRunner, profile: str) -> list[dict[str, Any]]:
    """List the VAMS databases visible to the profile.

    Args:
        runner: Function that runs VamsCLI.
        profile: VamsCLI profile.

    Returns:
        The database records.

    Raises:
        VamsError: If VamsCLI fails or the output does not hold the database list.
    """
    data = run_vamscli(runner, profile, ["database", "list"])
    items = data.get("Items")
    if not isinstance(items, list):
        raise VamsError("database list returned no 'Items' list")
    return items


def collect_asset_refs(snapshot: Snapshot) -> list[AssetRef]:
    """Collect the VAMS assets referenced by the target and comparison runs.

    Args:
        snapshot: Snapshot from ``fetch``.

    Returns:
        One reference per (database ID, asset ID), the target run's assets first.
    """
    roles: dict[tuple[str, str], list[str]] = {}
    for run in [snapshot.target, *snapshot.comparisons]:
        for role, (database_tag, asset_tag) in ROLE_TAGS.items():
            database_id = run.tags.get(database_tag)
            asset_id = run.tags.get(asset_tag)
            if not database_id or not asset_id:
                continue
            asset_roles = roles.setdefault((database_id, asset_id), [])
            if role not in asset_roles:
                asset_roles.append(role)
    return [
        AssetRef(database_id=database_id, asset_id=asset_id, roles=tuple(asset_roles))
        for (database_id, asset_id), asset_roles in roles.items()
    ]


def _read_curation_manifest(client: MlflowClient, run_id: str) -> dict[str, Any]:
    """Download the curation manifest a view run logged.

    Args:
        client: MLflow client.
        run_id: MLflow run ID of a run whose input was a curation view.

    Returns:
        The manifest JSON object.

    Raises:
        VamsError: If the manifest is not a JSON object with an ``episodes`` list.
    """
    with tempfile.TemporaryDirectory() as directory:
        path = client.download_artifacts(run_id, CURATION_MANIFEST_ARTIFACT, directory)
        manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or not isinstance(manifest.get("episodes"), list):
        raise VamsError(f"{CURATION_MANIFEST_ARTIFACT} of run {run_id} has no 'episodes' list")
    return manifest


def _view_episodes(client: MlflowClient, run: RunSnapshot) -> dict[str, Any] | None:
    """Return the episodes a view run kept and excluded, or None for a dataset input.

    Args:
        client: MLflow client.
        run: Run of the snapshot.

    Returns:
        ``{"keptEpisodes": [...], "excludedEpisodes": [{"episodeIndex", "reasons"}]}`` for a
        view input, else None.
    """
    if run.tags.get(INPUT_KIND_TAG) != VIEW_KIND:
        return None
    manifest = _read_curation_manifest(client, run.run_id)
    excluded = manifest.get("excluded_episodes", [])
    return {
        "keptEpisodes": [int(index) for index in manifest["episodes"]],
        "excludedEpisodes": [
            {"episodeIndex": int(entry["episode_index"]), "reasons": list(entry["reasons"])}
            for entry in excluded
        ],
    }


def _run_reference(run: Run, experiment_names: dict[str, str]) -> dict[str, Any]:
    """Convert an MLflow run into the run reference kept in ``datasets.json``.

    Args:
        run: MLflow run.
        experiment_names: Experiment name keyed by experiment ID.

    Returns:
        The run ID, name, experiment, status and the VAMS tags of the run.
    """
    tags = run.data.tags
    return {
        "runId": run.info.run_id,
        "runName": run.info.run_name or "",
        "experimentName": experiment_names.get(run.info.experiment_id, ""),
        "status": run.info.status,
        "startTime": run.info.start_time,
        "inputDatabaseId": tags.get(INPUT_DATABASE_TAG),
        "inputAssetId": tags.get(INPUT_ASSET_TAG),
        "inputAssetName": tags.get(INPUT_ASSET_NAME_TAG),
        "inputKind": tags.get(INPUT_KIND_TAG),
        "sourceDatabaseId": tags.get(SOURCE_DATABASE_TAG),
        "sourceAssetId": tags.get(SOURCE_ASSET_TAG),
    }


def find_runs_using_assets(
    client: MlflowClient, experiments: Sequence[str], refs: Sequence[AssetRef]
) -> list[dict[str, Any]]:
    """Find every run of the experiments that used one of the assets, in any status.

    Args:
        client: MLflow client.
        experiments: Experiment names to search.
        refs: Assets to look for.

    Returns:
        Run references sorted by start time, newest first, without duplicates.

    Raises:
        FetchError: If an asset ID cannot be put in a filter string.
    """
    ids = find_experiment_ids(client, experiments)
    if not ids:
        return []
    experiment_names = {
        experiment_id: client.get_experiment(experiment_id).name for experiment_id in ids
    }
    runs: dict[str, Run] = {}
    for ref in refs:
        for role in ref.roles:
            asset_tag = ROLE_TAGS[role][1]
            filter_string = f"tags.`{asset_tag}` = {quote_filter_value(ref.asset_id, asset_tag)}"
            for run in client.search_runs(experiment_ids=ids, filter_string=filter_string):
                runs[run.info.run_id] = run
    ordered = sorted(runs.values(), key=lambda run: run.info.start_time or 0, reverse=True)
    return [_run_reference(run, experiment_names) for run in ordered]


def fetch_vams_datasets(
    snapshot: Snapshot, client: MlflowClient, runner: CliRunner, profile: str
) -> dict[str, Any]:
    """Build the contents of ``datasets.json`` for a snapshot.

    Args:
        snapshot: Snapshot from ``fetch``.
        client: MLflow client, used to find the runs of each asset and the view manifests.
        runner: Function that runs VamsCLI.
        profile: VamsCLI profile.

    Returns:
        The ``datasets.json`` document. ``assets`` is empty when no run came from VAMS.

    Raises:
        VamsError: If VamsCLI fails or a view manifest cannot be read.
        FetchError: If an asset ID cannot be searched in MLflow.
    """
    refs = collect_asset_refs(snapshot)
    assets = []
    for ref in refs:
        assets.append(
            {
                "databaseId": ref.database_id,
                "assetId": ref.asset_id,
                "roles": list(ref.roles),
                **get_asset(runner, profile, ref.database_id, ref.asset_id),
                "metadata": list_asset_metadata(runner, profile, ref.database_id, ref.asset_id),
            }
        )
    snapshot_runs = []
    for role, run in [("target", snapshot.target)] + [
        ("comparison", run) for run in snapshot.comparisons
    ]:
        snapshot_runs.append(
            {
                "runId": run.run_id,
                "role": role,
                "inputDatabaseId": run.tags.get(INPUT_DATABASE_TAG),
                "inputAssetId": run.tags.get(INPUT_ASSET_TAG),
                "inputKind": run.tags.get(INPUT_KIND_TAG),
                "sourceAssetId": run.tags.get(SOURCE_ASSET_TAG),
                "view": _view_episodes(client, run),
            }
        )
    experiments = snapshot.criteria.experiments
    return {
        "schemaVersion": SCHEMA_VERSION,
        "createdAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "vamsProfile": profile,
        "experiments": list(experiments),
        "targetRunId": snapshot.target.run_id,
        "assets": assets,
        "snapshotRuns": snapshot_runs,
        "runsUsingAssets": find_runs_using_assets(client, experiments, refs) if refs else [],
    }
