"""Tests for the curation view manifest validation and the view dataset root."""

import copy
import json
import os
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from pask_vams_poller import view
from pask_vams_poller.constants import ErrorName
from pask_vams_poller.errors import PermanentFailure

# Curation view manifest contract (curation_manifest_version 2): examples.view.manifest,
# examples.materialize.manifest and every entry of invalidManifests.
CONTRACT_FILE = Path(__file__).parent / "fixtures" / "curation_view_contract.json"
CONTRACT: dict[str, Any] = json.loads(CONTRACT_FILE.read_text(encoding="utf-8"))
INVALID_MANIFESTS: list[dict[str, Any]] = CONTRACT["invalidManifests"]
VERSION_OR_MODE_CASES = ("Unknown curation_manifest_version", "Unknown mode")

INPUT_BUCKET = "vams-asset-bucket-example"
INPUT_DATABASE_ID = "robot-data"


def _view_manifest() -> dict[str, Any]:
    """Return a deep copy of the valid view manifest of the contract.

    Returns:
        ``examples.view.manifest``.
    """
    return copy.deepcopy(CONTRACT["examples"]["view"]["manifest"])


def _parse(document: Any) -> view.ViewManifest:
    """Parse a manifest with the bucket and database of the contract example.

    Args:
        document: Manifest to parse.

    Returns:
        The validated manifest.
    """
    return view.parse_view_manifest(
        document, input_bucket=INPUT_BUCKET, input_database_id=INPUT_DATABASE_ID
    )


def _assert_invalid(document: Any, cause_part: str) -> None:
    """Check that parsing fails with ``PASK.InvalidRequest`` and a cause naming the field.

    Args:
        document: Manifest to parse.
        cause_part: Text that the cause must contain.
    """
    with pytest.raises(PermanentFailure) as raised:
        _parse(document)
    assert raised.value.error_name is ErrorName.INVALID_REQUEST
    assert cause_part in raised.value.cause


def _file_index(document: dict[str, Any], path: str) -> int:
    """Find the position of a source file by path.

    Args:
        document: View manifest.
        path: ``path`` of the source file.

    Returns:
        Index in ``source.files``.
    """
    paths = [entry["path"] for entry in document["source"]["files"]]
    return paths.index(path)


def test_parse_view_manifest_accepts_contract_example() -> None:
    document = _view_manifest()

    manifest = _parse(document)

    assert manifest.episodes == (0, 2, 4)
    assert len(manifest.files) == 6
    assert manifest.excluded_episode_count == 2
    assert manifest.database_id == "robot-data"
    assert manifest.asset_id == "x0a1b2c3d-source"
    assert manifest.asset_version == "1"
    assert manifest.bucket == INPUT_BUCKET
    assert manifest.asset_root_key == "x0a1b2c3d-source/"
    assert manifest.codebase_version == "v3.0"
    assert manifest.total_episodes == 5
    assert manifest.review_updated_at == "2026-10-21T08:59:42Z"
    assert manifest.created_at == "2026-10-23T01:02:03Z"
    assert manifest.stats_path == "meta/stats.json"
    assert manifest.files[0] == view.ViewSourceFile(
        path="data/chunk-000/file-000.parquet",
        version_id="3HL4kqtJlcpXroDTDmJ.rmSpXd3dIbrHY",
        size=91011,
    )
    assert manifest.document == document


def test_parse_view_manifest_reads_null_optional_fields_as_none() -> None:
    document = _view_manifest()
    document["source"]["asset_version"] = None
    document["source"]["review_updated_at"] = None
    del document["source"]["codebase_version"]
    del document["source"]["total_episodes"]
    del document["created_at"]
    del document["excluded_episodes"]

    manifest = _parse(document)

    assert manifest.asset_version is None
    assert manifest.review_updated_at is None
    assert manifest.codebase_version is None
    assert manifest.total_episodes is None
    assert manifest.created_at is None
    assert manifest.excluded_episode_count == 0


def test_contract_has_ten_invalid_manifests() -> None:
    assert len(INVALID_MANIFESTS) == 10


@pytest.mark.parametrize(
    "invalid_case", INVALID_MANIFESTS, ids=[case["case"] for case in INVALID_MANIFESTS]
)
def test_parse_view_manifest_rejects_contract_invalid_manifests(
    invalid_case: dict[str, Any],
) -> None:
    with pytest.raises(PermanentFailure) as raised:
        _parse(copy.deepcopy(invalid_case["manifest"]))

    assert raised.value.error_name is ErrorName.INVALID_REQUEST


@pytest.mark.parametrize(
    "invalid_case",
    [case for case in INVALID_MANIFESTS if case["case"] in VERSION_OR_MODE_CASES],
    ids=list(VERSION_OR_MODE_CASES),
)
def test_curation_manifest_mode_rejects_unknown_version_and_mode(
    invalid_case: dict[str, Any],
) -> None:
    with pytest.raises(PermanentFailure) as raised:
        view.curation_manifest_mode(copy.deepcopy(invalid_case["manifest"]))

    assert raised.value.error_name is ErrorName.INVALID_REQUEST


def test_curation_manifest_mode_returns_view_and_materialize() -> None:
    materialize_manifest = copy.deepcopy(CONTRACT["examples"]["materialize"]["manifest"])

    assert view.curation_manifest_mode(_view_manifest()) == "view"
    assert view.curation_manifest_mode(materialize_manifest) == "materialize"


@pytest.mark.parametrize("version", [1, True, "2", None])
def test_curation_manifest_mode_rejects_materialize_with_other_version(version: Any) -> None:
    materialize_manifest = copy.deepcopy(CONTRACT["examples"]["materialize"]["manifest"])
    materialize_manifest["curation_manifest_version"] = version

    with pytest.raises(PermanentFailure) as raised:
        view.curation_manifest_mode(materialize_manifest)

    assert raised.value.error_name is ErrorName.INVALID_REQUEST


@pytest.mark.parametrize("document", [[], "view", None])
def test_curation_manifest_mode_rejects_non_object(document: Any) -> None:
    with pytest.raises(PermanentFailure) as raised:
        view.curation_manifest_mode(document)

    assert raised.value.error_name is ErrorName.INVALID_REQUEST


def test_parse_view_manifest_rejects_materialize_manifest() -> None:
    materialize_manifest = copy.deepcopy(CONTRACT["examples"]["materialize"]["manifest"])

    _assert_invalid(materialize_manifest, "mode is not view")


def test_parse_view_manifest_rejects_source_in_another_bucket() -> None:
    with pytest.raises(PermanentFailure) as raised:
        view.parse_view_manifest(
            _view_manifest(), input_bucket="other-bucket", input_database_id=INPUT_DATABASE_ID
        )

    assert raised.value.error_name is ErrorName.INVALID_REQUEST
    assert raised.value.cause == view.SAME_LOCATION_CAUSE


def test_parse_view_manifest_rejects_source_in_another_database() -> None:
    with pytest.raises(PermanentFailure) as raised:
        view.parse_view_manifest(
            _view_manifest(), input_bucket=INPUT_BUCKET, input_database_id="other-database"
        )

    assert raised.value.error_name is ErrorName.INVALID_REQUEST
    assert raised.value.cause == view.SAME_LOCATION_CAUSE


def _set_episodes(episodes: list[Any]) -> Callable[[dict[str, Any]], None]:
    """Build a mutation that replaces ``episodes``.

    Args:
        episodes: New value of ``episodes``.

    Returns:
        Function that edits a manifest in place.
    """

    def mutate(document: dict[str, Any]) -> None:
        document["episodes"] = episodes
        document["excluded_episodes"] = []

    return mutate


def _set_file_field(path: str, field: str, value: Any) -> Callable[[dict[str, Any]], None]:
    """Build a mutation that changes one field of a source file.

    Args:
        path: ``path`` of the source file to change.
        field: Field to change.
        value: New value.

    Returns:
        Function that edits a manifest in place.
    """

    def mutate(document: dict[str, Any]) -> None:
        document["source"]["files"][_file_index(document, path)][field] = value

    return mutate


def _remove_files(predicate: Callable[[str], bool]) -> Callable[[dict[str, Any]], None]:
    """Build a mutation that removes the source files whose path matches.

    Args:
        predicate: Returns ``True`` for a path to remove.

    Returns:
        Function that edits a manifest in place.
    """

    def mutate(document: dict[str, Any]) -> None:
        document["source"]["files"] = [
            entry for entry in document["source"]["files"] if not predicate(entry["path"])
        ]

    return mutate


def _duplicate_first_file(document: dict[str, Any]) -> None:
    """Append a copy of the first source file.

    Args:
        document: View manifest edited in place.
    """
    document["source"]["files"].append(dict(document["source"]["files"][0]))


def _set_source_field(field: str, value: Any) -> Callable[[dict[str, Any]], None]:
    """Build a mutation that changes one field of ``source``.

    Args:
        field: Field to change.
        value: New value.

    Returns:
        Function that edits a manifest in place.
    """

    def mutate(document: dict[str, Any]) -> None:
        document["source"][field] = value

    return mutate


def _set_storage_field(field: str, value: Any) -> Callable[[dict[str, Any]], None]:
    """Build a mutation that changes one field of ``source.storage``.

    Args:
        field: Field to change.
        value: New value.

    Returns:
        Function that edits a manifest in place.
    """

    def mutate(document: dict[str, Any]) -> None:
        document["source"]["storage"][field] = value

    return mutate


def _set_excluded(excluded: Any) -> Callable[[dict[str, Any]], None]:
    """Build a mutation that replaces ``excluded_episodes``.

    Args:
        excluded: New value of ``excluded_episodes``.

    Returns:
        Function that edits a manifest in place.
    """

    def mutate(document: dict[str, Any]) -> None:
        document["excluded_episodes"] = excluded

    return mutate


def _set_stats(stats: Any) -> Callable[[dict[str, Any]], None]:
    """Build a mutation that replaces ``stats``.

    Args:
        stats: New value of ``stats``.

    Returns:
        Function that edits a manifest in place.
    """

    def mutate(document: dict[str, Any]) -> None:
        document["stats"] = stats

    return mutate


INFO_PATH = "meta/info.json"
DATA_PATH = "data/chunk-000/file-000.parquet"


@pytest.mark.parametrize(
    ("mutate", "cause_part"),
    [
        pytest.param(_set_episodes([0, 2, 5]), "episodes[2] is not below", id="episode-at-total"),
        pytest.param(_set_episodes([0, 7]), "episodes[1] is not below", id="episode-over-total"),
        pytest.param(_set_episodes([0, True]), "episodes[1] is not an integer", id="bool-episode"),
        pytest.param(_set_episodes([4, 2]), "not strictly ascending", id="descending-episodes"),
        pytest.param(_set_episodes([]), "episodes is not a non-empty list", id="empty-episodes"),
        pytest.param(
            _set_file_field(INFO_PATH, "path", "meta/../info.json"),
            "path has an empty, '.', or '..'",
            id="dot-dot-path",
        ),
        pytest.param(
            _set_file_field(INFO_PATH, "path", "meta//info.json"),
            "path has an empty, '.', or '..'",
            id="empty-segment-path",
        ),
        pytest.param(
            _set_file_field(INFO_PATH, "path", "/meta/info.json"),
            "path starts with '/'",
            id="leading-slash-path",
        ),
        pytest.param(_set_file_field(INFO_PATH, "path", ""), ".path is empty", id="empty-path"),
        pytest.param(_duplicate_first_file, "path is a duplicate", id="duplicate-path"),
        pytest.param(
            _set_file_field(INFO_PATH, "version_id", "null"),
            "version_id is not a pinned version",
            id="null-version-id",
        ),
        pytest.param(
            _set_file_field(INFO_PATH, "version_id", None),
            "version_id is empty",
            id="missing-version-id",
        ),
        pytest.param(_set_file_field(INFO_PATH, "size", -1), "size is not", id="negative-size"),
        pytest.param(_set_file_field(INFO_PATH, "size", True), "size is not", id="bool-size"),
        pytest.param(_set_file_field(INFO_PATH, "size", 1.5), "size is not", id="float-size"),
        pytest.param(
            _remove_files(lambda path: path == "meta/info.json"),
            "has no meta/info.json",
            id="missing-info",
        ),
        pytest.param(
            _remove_files(lambda path: path == "meta/tasks.parquet"),
            "has no meta/tasks.parquet",
            id="missing-tasks",
        ),
        pytest.param(
            _remove_files(lambda path: path.startswith("meta/episodes/")),
            "has no file under meta/episodes/",
            id="missing-episodes-meta",
        ),
        pytest.param(
            _remove_files(lambda path: path.startswith("data/")),
            "has no file under data/",
            id="missing-data",
        ),
        pytest.param(_remove_files(lambda path: True), "source.files is not", id="empty-files"),
        pytest.param(_set_source_field("asset_id", ""), "source.asset_id", id="empty-asset-id"),
        pytest.param(
            _set_source_field("database_id", None), "source.database_id", id="no-database-id"
        ),
        pytest.param(_set_source_field("files", {}), "source.files is not", id="files-object"),
        pytest.param(
            _set_storage_field("asset_root_key", "x0a1b2c3d-source"),
            "asset_root_key does not end with '/'",
            id="root-key-without-slash",
        ),
        pytest.param(_set_storage_field("bucket", ""), "source.storage.bucket", id="empty-bucket"),
        pytest.param(_set_excluded({}), "excluded_episodes is not a list", id="excluded-object"),
        pytest.param(_set_excluded([3]), "excluded_episodes[0] is not an object", id="excl-int"),
        pytest.param(
            _set_excluded([{"episode_index": -1, "reasons": []}]),
            "excluded_episodes[0].episode_index",
            id="excluded-negative",
        ),
        pytest.param(
            _set_excluded([{"episode_index": False, "reasons": []}]),
            "excluded_episodes[0].episode_index is not",
            id="excluded-bool",
        ),
        pytest.param(
            _set_excluded([{"episode_index": 4, "reasons": []}]),
            "also in episodes",
            id="excluded-overlaps",
        ),
        pytest.param(_set_stats({"path": ""}), "stats.path", id="empty-stats-path"),
        pytest.param(_set_stats(None), "stats is not an object", id="missing-stats"),
    ],
)
def test_parse_view_manifest_rejects_contract_violations(
    mutate: Callable[[dict[str, Any]], None], cause_part: str
) -> None:
    document = _view_manifest()
    mutate(document)

    _assert_invalid(document, cause_part)


def test_parse_view_manifest_keeps_version_id_out_of_cause() -> None:
    document = _view_manifest()
    document["source"]["files"][_file_index(document, DATA_PATH)]["size"] = -1

    with pytest.raises(PermanentFailure) as raised:
        _parse(document)

    assert "3HL4kqtJlcpXroDTDmJ.rmSpXd3dIbrHY" not in raised.value.cause
    assert INPUT_BUCKET not in raised.value.cause


def _write_file(path: Path, content: str) -> None:
    """Write a text file, creating its parent directories.

    Args:
        path: File to write.
        content: Text to write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _make_staged_root(tmp_path: Path) -> Path:
    """Create a staged LeRobot dataset under ``tmp_path``.

    Args:
        tmp_path: Temporary directory of the test.

    Returns:
        Root of the staged dataset.
    """
    staged_root = tmp_path / "fsx" / "vams" / "exec-1"
    _write_file(staged_root / "meta" / "info.json", "{}")
    _write_file(staged_root / "meta" / "tasks.parquet", "tasks")
    _write_file(staged_root / "meta" / "episodes" / "chunk-000" / "file-000.parquet", "eps")
    _write_file(staged_root / "meta" / "stats.json", "{}")
    _write_file(staged_root / "data" / "chunk-000" / "file-000.parquet", "data")
    _write_file(staged_root / "videos" / "front" / "chunk-000" / "file-000.mp4", "video")
    return staged_root


def _assert_view_root_layout(staged_root: Path, view_root: Path) -> None:
    """Check the links of a built view dataset root.

    Args:
        staged_root: Root of the staged dataset.
        view_root: Root of the view dataset.
    """
    meta_dir = view_root / "meta"
    assert meta_dir.is_dir()
    assert not meta_dir.is_symlink()
    assert sorted(os.listdir(view_root)) == ["data", "meta", "videos"]
    assert sorted(os.listdir(meta_dir)) == ["episodes", "info.json", "tasks.parquet"]
    for name in ("data", "videos"):
        link = view_root / name
        assert link.is_symlink()
        assert os.readlink(link) == str(staged_root / name)
        assert os.path.isabs(os.readlink(link))
    for name in ("episodes", "info.json", "tasks.parquet"):
        link = meta_dir / name
        assert link.is_symlink()
        assert os.readlink(link) == str(staged_root / "meta" / name)
    assert not (meta_dir / "stats.json").exists()


def test_build_view_dataset_root_links_staged_dataset(tmp_path: Path) -> None:
    staged_root = _make_staged_root(tmp_path)
    view_root = tmp_path / "run" / "view_dataset"

    view.build_view_dataset_root(f"{staged_root}/", str(view_root))

    _assert_view_root_layout(staged_root, view_root)
    assert (view_root / "data" / "chunk-000" / "file-000.parquet").read_text() == "data"


def test_build_view_dataset_root_is_idempotent(tmp_path: Path) -> None:
    staged_root = _make_staged_root(tmp_path)
    view_root = tmp_path / "run" / "view_dataset"
    view.build_view_dataset_root(str(staged_root), str(view_root))
    (view_root / "meta" / "stats.json").write_text('{"written": "by training"}')

    view.build_view_dataset_root(str(staged_root), str(view_root))

    _assert_view_root_layout(staged_root, view_root)
    assert (staged_root / "meta" / "stats.json").read_text() == "{}"


def test_build_view_dataset_root_fails_when_entry_is_not_a_link(tmp_path: Path) -> None:
    staged_root = _make_staged_root(tmp_path)
    view_root = tmp_path / "run" / "view_dataset"
    (view_root / "data").mkdir(parents=True)

    with pytest.raises(FileExistsError):
        view.build_view_dataset_root(str(staged_root), str(view_root))


def test_removing_view_dataset_root_keeps_staged_files(tmp_path: Path) -> None:
    staged_root = _make_staged_root(tmp_path)
    view_root = tmp_path / "run" / "view_dataset"
    view.build_view_dataset_root(str(staged_root), str(view_root))

    shutil.rmtree(view_root)

    assert not view_root.exists()
    assert (staged_root / "meta" / "info.json").is_file()
    assert (staged_root / "meta" / "episodes" / "chunk-000" / "file-000.parquet").is_file()
    assert (staged_root / "data" / "chunk-000" / "file-000.parquet").is_file()
    assert (staged_root / "videos" / "front" / "chunk-000" / "file-000.mp4").is_file()
