"""Validation of LeRobot curation view manifests and the dataset root used to train on a view.

A curation view asset holds ``curation_manifest.json`` (``curation_manifest_version`` 2,
``mode`` ``view``) that pins the files of a source asset by S3 version and lists the kept
episodes. The functions here take the parsed JSON document and do not call AWS; reading the
document from S3 is the job of the STAGING handler.
"""

import os
from dataclasses import dataclass
from typing import Any

from pask_vams_poller.constants import CURATION_MANIFEST_VERSION, ErrorName
from pask_vams_poller.errors import PermanentFailure

MODE_VIEW = "view"
MODE_MATERIALIZE = "materialize"
CURATION_MODES: frozenset[str] = frozenset({MODE_VIEW, MODE_MATERIALIZE})

RELATIVE_PATH_FORBIDDEN_SEGMENTS: frozenset[str] = frozenset({"", ".", ".."})
UNVERSIONED_VERSION_ID = "null"

REQUIRED_SOURCE_FILES: tuple[str, ...] = ("meta/info.json", "meta/tasks.parquet")
REQUIRED_SOURCE_PREFIXES: tuple[str, ...] = ("meta/episodes/", "data/")

META_DIR = "meta"
STATS_FILE = "stats.json"

SAME_LOCATION_CAUSE = "curation view source must be in the same database and bucket as the view"


@dataclass(frozen=True)
class ViewSourceFile:
    """A source asset file that a view pins by S3 version."""

    path: str
    version_id: str
    size: int


@dataclass(frozen=True)
class ViewManifest:
    """A validated ``curation_manifest.json`` with ``mode`` ``view``."""

    database_id: str
    asset_id: str
    asset_version: str | None
    bucket: str
    asset_root_key: str
    codebase_version: str | None
    total_episodes: int | None
    review_updated_at: str | None
    created_at: str | None
    files: tuple[ViewSourceFile, ...]
    episodes: tuple[int, ...]
    excluded_episode_count: int
    stats_path: str
    document: dict[str, Any]


def _invalid(cause: str) -> PermanentFailure:
    """Build the failure for a manifest that breaks the contract.

    Args:
        cause: English description of the field that is wrong.

    Returns:
        ``PermanentFailure`` with ``PASK.InvalidRequest``.
    """
    return PermanentFailure(ErrorName.INVALID_REQUEST, cause)


def _is_int(value: Any) -> bool:
    """Tell whether a JSON value is an integer, excluding booleans.

    Args:
        value: Parsed JSON value.

    Returns:
        ``True`` for an ``int`` that is not a ``bool``.
    """
    return isinstance(value, int) and not isinstance(value, bool)


def _is_non_negative_int(value: Any) -> bool:
    """Tell whether a JSON value is an integer of 0 or more, excluding booleans.

    Args:
        value: Parsed JSON value.

    Returns:
        ``True`` for an ``int`` that is not a ``bool`` and is 0 or more.
    """
    return _is_int(value) and value >= 0


def _require_dict(value: Any, field: str) -> dict[str, Any]:
    """Return a JSON object field or fail.

    Args:
        value: Parsed JSON value.
        field: Dotted name of the field, used in the cause.

    Returns:
        The value.

    Raises:
        PermanentFailure: ``PASK.InvalidRequest`` when the value is not an object.
    """
    if not isinstance(value, dict):
        raise _invalid(f"curation view: {field} is not an object")
    return value


def _require_non_empty_str(value: Any, field: str) -> str:
    """Return a non-empty string field or fail.

    Args:
        value: Parsed JSON value.
        field: Dotted name of the field, used in the cause.

    Returns:
        The value.

    Raises:
        PermanentFailure: ``PASK.InvalidRequest`` when the value is not a non-empty string.
    """
    if not isinstance(value, str) or not value:
        raise _invalid(f"curation view: {field} is empty or not a string")
    return value


def _optional_str(value: Any) -> str | None:
    """Return a string field, or ``None`` for null, a missing field, or another type.

    Args:
        value: Parsed JSON value.

    Returns:
        The value when it is a string, otherwise ``None``.
    """
    return value if isinstance(value, str) else None


def curation_manifest_mode(document: Any) -> str:
    """Check the version of a curation manifest and return its mode.

    Args:
        document: Parsed ``curation_manifest.json``.

    Returns:
        ``"view"`` or ``"materialize"``.

    Raises:
        PermanentFailure: ``PASK.InvalidRequest`` when the document is not an object, the
            version is not ``CURATION_MANIFEST_VERSION``, or the mode is unknown.
    """
    if not isinstance(document, dict):
        raise _invalid("curation manifest is not a JSON object")
    version = document.get("curation_manifest_version")
    if not _is_int(version) or version != CURATION_MANIFEST_VERSION:
        raise _invalid(
            f"curation manifest: curation_manifest_version must be {CURATION_MANIFEST_VERSION}"
        )
    mode = document.get("mode")
    if mode not in CURATION_MODES:
        raise _invalid("curation manifest: mode must be view or materialize")
    return mode


def _parse_source_path(path: Any, field: str) -> str:
    """Validate the relative path of a source file.

    Args:
        path: ``path`` of a ``source.files`` entry.
        field: Dotted name of the field, used in the cause.

    Returns:
        The path.

    Raises:
        PermanentFailure: ``PASK.InvalidRequest`` for an empty path, a leading ``/``, or an
            empty, ``.``, or ``..`` segment.
    """
    relative_path = _require_non_empty_str(path, field)
    if relative_path.startswith("/"):
        raise _invalid(f"curation view: {field} starts with '/'")
    if any(segment in RELATIVE_PATH_FORBIDDEN_SEGMENTS for segment in relative_path.split("/")):
        raise _invalid(f"curation view: {field} has an empty, '.', or '..' path segment")
    return relative_path


def _parse_source_file(entry: Any, index: int) -> ViewSourceFile:
    """Validate one ``source.files`` entry.

    Args:
        entry: Parsed entry.
        index: Position of the entry, used in the cause.

    Returns:
        The validated file.

    Raises:
        PermanentFailure: ``PASK.InvalidRequest`` when the entry breaks the contract.
    """
    field = f"source.files[{index}]"
    file_entry = _require_dict(entry, field)
    path = _parse_source_path(file_entry.get("path"), f"{field}.path")
    version_id = file_entry.get("version_id")
    if not isinstance(version_id, str) or not version_id:
        raise _invalid(f"curation view: {field}.version_id is empty")
    if version_id == UNVERSIONED_VERSION_ID:
        raise _invalid(f"curation view: {field}.version_id is not a pinned version")
    size = file_entry.get("size")
    if not _is_non_negative_int(size):
        raise _invalid(f"curation view: {field}.size is not an integer of 0 or more")
    return ViewSourceFile(path=path, version_id=version_id, size=size)


def _check_required_files(paths: set[str]) -> None:
    """Check that the source files hold what LeRobot needs to read the episodes.

    Args:
        paths: Paths of every ``source.files`` entry.

    Raises:
        PermanentFailure: ``PASK.InvalidRequest`` when a required file is missing.
    """
    for required_path in REQUIRED_SOURCE_FILES:
        if required_path not in paths:
            raise _invalid(f"curation view: source.files has no {required_path}")
    for required_prefix in REQUIRED_SOURCE_PREFIXES:
        if not any(path.startswith(required_prefix) for path in paths):
            raise _invalid(f"curation view: source.files has no file under {required_prefix}")


def _parse_source_files(value: Any) -> tuple[ViewSourceFile, ...]:
    """Validate ``source.files``.

    Args:
        value: Parsed ``source.files``.

    Returns:
        The validated files in the manifest order.

    Raises:
        PermanentFailure: ``PASK.InvalidRequest`` when the list is empty, an entry is
            invalid, a path repeats, or a required file is missing.
    """
    if not isinstance(value, list) or not value:
        raise _invalid("curation view: source.files is not a non-empty list")
    files: list[ViewSourceFile] = []
    seen_paths: set[str] = set()
    for index, entry in enumerate(value):
        source_file = _parse_source_file(entry, index)
        if source_file.path in seen_paths:
            raise _invalid(f"curation view: source.files[{index}].path is a duplicate")
        seen_paths.add(source_file.path)
        files.append(source_file)
    _check_required_files(seen_paths)
    return tuple(files)


def _parse_episodes(value: Any, total_episodes: int | None) -> tuple[int, ...]:
    """Validate ``episodes``.

    Args:
        value: Parsed ``episodes``.
        total_episodes: ``source.total_episodes`` when it is an integer, otherwise ``None``.

    Returns:
        The kept source episode indexes in ascending order.

    Raises:
        PermanentFailure: ``PASK.InvalidRequest`` when the list is empty, an index is not an
            integer of 0 or more, the order is not strictly ascending, or an index is not
            below ``total_episodes``.
    """
    if not isinstance(value, list) or not value:
        raise _invalid("curation view: episodes is not a non-empty list")
    for index, episode in enumerate(value):
        if not _is_non_negative_int(episode):
            raise _invalid(f"curation view: episodes[{index}] is not an integer of 0 or more")
        if index > 0 and episode <= value[index - 1]:
            raise _invalid("curation view: episodes is not strictly ascending")
        if total_episodes is not None and episode >= total_episodes:
            raise _invalid(f"curation view: episodes[{index}] is not below source.total_episodes")
    return tuple(value)


def _count_excluded_episodes(value: Any, episodes: tuple[int, ...]) -> int:
    """Validate ``excluded_episodes`` and count its entries.

    Args:
        value: Parsed ``excluded_episodes``, or ``None`` when the field is missing.
        episodes: Validated kept episode indexes.

    Returns:
        Number of excluded episodes.

    Raises:
        PermanentFailure: ``PASK.InvalidRequest`` when the value is not a list, an entry is
            not an object with an ``episode_index`` of 0 or more, or an index is also kept.
    """
    if value is None:
        return 0
    if not isinstance(value, list):
        raise _invalid("curation view: excluded_episodes is not a list")
    kept_episodes = set(episodes)
    for index, entry in enumerate(value):
        field = f"excluded_episodes[{index}]"
        episode_index = _require_dict(entry, field).get("episode_index")
        if not _is_non_negative_int(episode_index):
            raise _invalid(f"curation view: {field}.episode_index is not an integer of 0 or more")
        if episode_index in kept_episodes:
            raise _invalid(f"curation view: {field}.episode_index is also in episodes")
    return len(value)


def parse_view_manifest(
    document: Any, *, input_bucket: str, input_database_id: str
) -> ViewManifest:
    """Validate a curation view manifest.

    Args:
        document: Parsed ``curation_manifest.json``.
        input_bucket: Bucket of the view asset given as the training input.
        input_database_id: ``databaseId`` of the view asset given as the training input.

    Returns:
        The validated manifest.

    Raises:
        PermanentFailure: ``PASK.InvalidRequest`` when the manifest breaks the contract or
            its source is in another database or bucket than the view.
    """
    mode = curation_manifest_mode(document)
    if mode != MODE_VIEW:
        raise _invalid("curation view: mode is not view")
    source = _require_dict(document.get("source"), "source")
    database_id = _require_non_empty_str(source.get("database_id"), "source.database_id")
    asset_id = _require_non_empty_str(source.get("asset_id"), "source.asset_id")
    storage = _require_dict(source.get("storage"), "source.storage")
    bucket = _require_non_empty_str(storage.get("bucket"), "source.storage.bucket")
    asset_root_key = _require_non_empty_str(
        storage.get("asset_root_key"), "source.storage.asset_root_key"
    )
    if not asset_root_key.endswith("/"):
        raise _invalid("curation view: source.storage.asset_root_key does not end with '/'")
    if bucket != input_bucket or database_id != input_database_id:
        raise _invalid(SAME_LOCATION_CAUSE)
    files = _parse_source_files(source.get("files"))
    raw_total_episodes = source.get("total_episodes")
    total_episodes = raw_total_episodes if _is_int(raw_total_episodes) else None
    episodes = _parse_episodes(document.get("episodes"), total_episodes)
    excluded_episode_count = _count_excluded_episodes(document.get("excluded_episodes"), episodes)
    stats = _require_dict(document.get("stats"), "stats")
    stats_path = _require_non_empty_str(stats.get("path"), "stats.path")
    return ViewManifest(
        database_id=database_id,
        asset_id=asset_id,
        asset_version=_optional_str(source.get("asset_version")),
        bucket=bucket,
        asset_root_key=asset_root_key,
        codebase_version=_optional_str(source.get("codebase_version")),
        total_episodes=total_episodes,
        review_updated_at=_optional_str(source.get("review_updated_at")),
        created_at=_optional_str(document.get("created_at")),
        files=files,
        episodes=episodes,
        excluded_episode_count=excluded_episode_count,
        stats_path=stats_path,
        document=document,
    )


def _replace_with_symlink(link_path: str, target_path: str) -> None:
    """Point ``link_path`` at ``target_path``, replacing a symbolic link left by a retry.

    Args:
        link_path: Path of the symbolic link to create.
        target_path: Absolute path the link points to.

    Raises:
        FileExistsError: When ``link_path`` exists and is not a symbolic link.
    """
    if os.path.islink(link_path):
        os.unlink(link_path)
    os.symlink(target_path, link_path)


def _remove_stale_stats(stats_path: str) -> None:
    """Remove ``meta/stats.json`` written by an earlier training job.

    Args:
        stats_path: Path of ``meta/stats.json`` under the view dataset root.
    """
    if os.path.lexists(stats_path):
        os.unlink(stats_path)


def build_view_dataset_root(staged_root: str, view_root: str) -> None:
    """Build a dataset root where the training job can write ``meta/stats.json``.

    The staged dataset on FSx is read-only for the training job, so ``view_root/meta`` is a
    real directory and every other entry is an absolute symbolic link into ``staged_root``.
    ``meta/stats.json`` of the staged dataset is not linked; the training job computes it.
    Calling the function again rebuilds the links and removes an earlier ``meta/stats.json``.

    Args:
        staged_root: Dataset root that FSx imported from the staging prefix.
        view_root: Dataset root to build. The caller makes sure it is inside the run directory.

    Raises:
        FileExistsError: When ``view_root/meta`` is a symbolic link, or an entry to link
            exists and is not a symbolic link.
        FileNotFoundError: When ``staged_root`` or ``staged_root/meta`` does not exist.
    """
    staged_root_path = os.path.abspath(staged_root)
    staged_meta_path = os.path.join(staged_root_path, META_DIR)
    view_meta_path = os.path.join(os.path.abspath(view_root), META_DIR)
    if os.path.islink(view_meta_path):
        raise FileExistsError(f"{META_DIR} of the view dataset root is a symbolic link")
    os.makedirs(view_meta_path, exist_ok=True)
    for name in sorted(os.listdir(staged_root_path)):
        if name == META_DIR:
            continue
        _replace_with_symlink(
            os.path.join(os.path.dirname(view_meta_path), name),
            os.path.join(staged_root_path, name),
        )
    for name in sorted(os.listdir(staged_meta_path)):
        if name == STATS_FILE:
            continue
        _replace_with_symlink(
            os.path.join(view_meta_path, name), os.path.join(staged_meta_path, name)
        )
    _remove_stale_stats(os.path.join(view_meta_path, STATS_FILE))
