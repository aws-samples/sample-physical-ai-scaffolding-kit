"""Tests for compute_view_stats.py against a small LeRobot v3.0 dataset.

LeRobot is only installed in the training container, so the lerobot modules that
compute_view_stats.py imports are replaced with numpy stubs before it is loaded.

Run from samples/lerobot-openpi/training:
    uv run --with mlflow --with pytest --with pyarrow --with numpy pytest tests
"""

import importlib.util
import json
import sys
import types
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

TRAINING_DIR = Path(__file__).resolve().parent.parent
EPISODE_LENGTHS = [2, 3, 2, 3, 2]
KEPT_EPISODES = [0, 2, 4]
EXCLUDED_VALUE = 1000.0
CAMERA_KEY = "observation.images.front"
CAMERA_CHANNELS = 3
STATE_DIM = 2


def _stub_get_feature_stats(array: np.ndarray, axis: int, keepdims: bool) -> dict[str, np.ndarray]:
    """Return simple numpy statistics in place of LeRobot's get_feature_stats."""
    return {
        "min": np.min(array, axis=axis, keepdims=keepdims),
        "max": np.max(array, axis=axis, keepdims=keepdims),
        "mean": np.mean(array, axis=axis, keepdims=keepdims),
        "std": np.std(array, axis=axis, keepdims=keepdims),
        "count": np.array([array.shape[0]]),
    }


def _stub_aggregate_stats(stats_list: list[dict[str, dict]]) -> dict[str, dict[str, np.ndarray]]:
    """Combine min, max and count in place of LeRobot's aggregate_stats."""
    keys = {key for stats in stats_list for key in stats}
    return {
        key: {
            "min": np.min([stats[key]["min"] for stats in stats_list], axis=0),
            "max": np.max([stats[key]["max"] for stats in stats_list], axis=0),
            "count": np.sum([stats[key]["count"] for stats in stats_list], axis=0),
        }
        for key in keys
    }


def _stub_write_stats(stats: dict, local_dir: Path) -> None:
    """Write the statistics as JSON in place of LeRobot's write_stats."""
    serialized = {
        feature: {name: np.asarray(value).tolist() for name, value in values.items()}
        for feature, values in stats.items()
    }
    (local_dir / "meta" / "stats.json").write_text(json.dumps(serialized), encoding="utf-8")


def _install_lerobot_stubs() -> None:
    """Register lerobot modules so compute_view_stats.py can be imported without LeRobot."""
    modules = {
        "lerobot": {"__version__": "0.0.0"},
        "lerobot.datasets": {},
        "lerobot.datasets.compute_stats": {
            "get_feature_stats": _stub_get_feature_stats,
            "aggregate_stats": _stub_aggregate_stats,
        },
        "lerobot.datasets.io_utils": {"write_stats": _stub_write_stats},
    }
    for name, attributes in modules.items():
        module = sys.modules.setdefault(name, types.ModuleType(name))
        for attribute, value in attributes.items():
            setattr(module, attribute, value)


def _load_compute_view_stats() -> types.ModuleType:
    """Import training/compute_view_stats.py as a module."""
    _install_lerobot_stubs()
    spec = importlib.util.spec_from_file_location(
        "compute_view_stats", TRAINING_DIR / "compute_view_stats.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


compute_view_stats = _load_compute_view_stats()


def _state_value(episode_index: int, frame_index: int) -> float:
    """Return the state value of a frame; excluded episodes hold an outlier."""
    if episode_index in KEPT_EPISODES:
        return float(episode_index * 10 + frame_index)
    return EXCLUDED_VALUE


def _write_data_file(source: Path) -> None:
    """Write one data file holding every episode."""
    episode_column, frame_column, state_column = [], [], []
    for episode_index, length in enumerate(EPISODE_LENGTHS):
        for frame_index in range(length):
            episode_column.append(episode_index)
            frame_column.append(frame_index)
            state_column.append([_state_value(episode_index, frame_index)] * STATE_DIM)
    frame_count = len(episode_column)
    vector_type = pa.list_(pa.float32(), STATE_DIM)
    table = pa.table(
        {
            "observation.state": pa.array(state_column, type=vector_type),
            "action": pa.array(state_column, type=vector_type),
            "timestamp": pa.array([frame / 10 for frame in frame_column], type=pa.float32()),
            "frame_index": pa.array(frame_column, type=pa.int64()),
            "episode_index": pa.array(episode_column, type=pa.int64()),
            "index": pa.array(range(frame_count), type=pa.int64()),
            "task_index": pa.array([0] * frame_count, type=pa.int64()),
        }
    )
    path = source / "data" / "chunk-000" / "file-000.parquet"
    path.parent.mkdir(parents=True)
    pq.write_table(table, path)


def _camera_stat(value: float) -> list[list[list[float]]]:
    """Return a channel-first (C, 1, 1) per-episode statistic."""
    return [[[value]] for _ in range(CAMERA_CHANNELS)]


def _write_episodes_file(source: Path, episodes: list[int]) -> None:
    """Write meta/episodes with the per-episode camera statistics of the given episodes."""
    table = pa.table(
        {
            "episode_index": pa.array(episodes, type=pa.int64()),
            "length": pa.array([EPISODE_LENGTHS[index] for index in episodes], type=pa.int64()),
            "data/chunk_index": pa.array([0] * len(episodes), type=pa.int64()),
            "data/file_index": pa.array([0] * len(episodes), type=pa.int64()),
            f"stats/{CAMERA_KEY}/min": [_camera_stat(index / 10) for index in episodes],
            f"stats/{CAMERA_KEY}/max": [_camera_stat(index / 10 + 0.5) for index in episodes],
            f"stats/{CAMERA_KEY}/mean": [_camera_stat(index / 10 + 0.2) for index in episodes],
            f"stats/{CAMERA_KEY}/std": [_camera_stat(0.1) for _ in episodes],
            f"stats/{CAMERA_KEY}/count": [[EPISODE_LENGTHS[index]] for index in episodes],
        }
    )
    path = source / "meta" / "episodes" / "chunk-000" / "file-000.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path)


def _info(codebase_version: str) -> dict[str, Any]:
    """Return the info.json of the test dataset."""
    return {
        "codebase_version": codebase_version,
        "total_episodes": len(EPISODE_LENGTHS),
        "total_frames": sum(EPISODE_LENGTHS),
        "fps": 10,
        "data_path": "data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet",
        "video_path": "videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4",
        "features": {
            "observation.state": {"dtype": "float32", "shape": [STATE_DIM], "names": None},
            "action": {"dtype": "float32", "shape": [STATE_DIM], "names": None},
            CAMERA_KEY: {"dtype": "video", "shape": [8, 8, 3], "names": None},
            "timestamp": {"dtype": "float32", "shape": [1], "names": None},
            "frame_index": {"dtype": "int64", "shape": [1], "names": None},
            "episode_index": {"dtype": "int64", "shape": [1], "names": None},
            "index": {"dtype": "int64", "shape": [1], "names": None},
            "task_index": {"dtype": "int64", "shape": [1], "names": None},
            "task": {"dtype": "string", "shape": [1], "names": None},
        },
    }


def _build_dataset(tmp_path: Path, codebase_version: str, listed_episodes: list[int]) -> Path:
    """Create a staged source dataset and a view root that links to it, as the poller does.

    Args:
        tmp_path: The pytest temporary directory.
        codebase_version: codebase_version written to info.json.
        listed_episodes: Episodes that meta/episodes has rows for.

    Returns:
        The view dataset root.
    """
    source = tmp_path / "source"
    (source / "meta").mkdir(parents=True)
    (source / "meta" / "info.json").write_text(
        json.dumps(_info(codebase_version)), encoding="utf-8"
    )
    (source / "meta" / "stats.json").write_text("{}", encoding="utf-8")
    _write_episodes_file(source, listed_episodes)
    _write_data_file(source)
    root = tmp_path / "view_dataset"
    (root / "meta").mkdir(parents=True)
    (root / "meta" / "info.json").symlink_to(source / "meta" / "info.json")
    (root / "meta" / "episodes").symlink_to(source / "meta" / "episodes")
    (root / "data").symlink_to(source / "data")
    return root


def _write_manifest(tmp_path: Path, episodes: list[Any]) -> Path:
    """Write a view curation_manifest.json with the given kept episodes."""
    path = tmp_path / "curation_manifest.json"
    path.write_text(
        json.dumps({"curation_manifest_version": 2, "mode": "view", "episodes": episodes}),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def view_root(tmp_path: Path) -> Path:
    """Create a v3.0 view root whose meta/episodes lists every episode."""
    return _build_dataset(tmp_path, "v3.0", list(range(len(EPISODE_LENGTHS))))


def test_writes_stats_of_kept_episodes_only(
    tmp_path: Path, view_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    feature_calls: list[np.ndarray] = []
    aggregate_calls: list[list[dict]] = []

    def recording_get_feature_stats(
        array: np.ndarray, axis: int, keepdims: bool
    ) -> dict[str, np.ndarray]:
        feature_calls.append(array)
        return _stub_get_feature_stats(array, axis, keepdims)

    def recording_aggregate_stats(stats_list: list[dict[str, dict]]) -> dict:
        aggregate_calls.append(stats_list)
        return _stub_aggregate_stats(stats_list)

    monkeypatch.setattr(compute_view_stats, "get_feature_stats", recording_get_feature_stats)
    monkeypatch.setattr(compute_view_stats, "aggregate_stats", recording_aggregate_stats)
    manifest = _write_manifest(tmp_path, [4, 0, 2])

    episode_count, frame_count = compute_view_stats.compute_view_stats(view_root, manifest)

    kept_frames = sum(EPISODE_LENGTHS[index] for index in KEPT_EPISODES)
    assert (episode_count, frame_count) == (len(KEPT_EPISODES), kept_frames)
    assert len(feature_calls) == 7
    assert all(len(array) == kept_frames for array in feature_calls)
    assert all(EXCLUDED_VALUE not in array for array in feature_calls)

    assert len(aggregate_calls) == 1
    episode_stats = aggregate_calls[0]
    assert [list(stats) for stats in episode_stats] == [[CAMERA_KEY]] * len(KEPT_EPISODES)
    assert [float(stats[CAMERA_KEY]["min"][0, 0, 0]) for stats in episode_stats] == [
        index / 10 for index in KEPT_EPISODES
    ]
    assert episode_stats[0][CAMERA_KEY]["mean"].shape == (CAMERA_CHANNELS, 1, 1)
    assert episode_stats[0][CAMERA_KEY]["count"].shape == (1,)

    stats = json.loads((view_root / "meta" / "stats.json").read_text(encoding="utf-8"))
    assert "task" not in stats
    assert set(stats) == {
        "observation.state",
        "action",
        CAMERA_KEY,
        "timestamp",
        "frame_index",
        "episode_index",
        "index",
        "task_index",
    }
    assert stats["observation.state"]["max"] == [41.0, 41.0]
    assert stats["observation.state"]["count"] == [kept_frames]
    assert stats["episode_index"]["min"] == [0]
    assert stats["episode_index"]["max"] == [4]
    assert stats[CAMERA_KEY]["count"] == [kept_frames]
    source_stats = view_root.parent / "source" / "meta" / "stats.json"
    assert source_stats.read_text(encoding="utf-8") == "{}"


@pytest.mark.parametrize("episodes", [[0, 5], [-1, 2], []])
def test_fails_on_episodes_outside_the_dataset(
    tmp_path: Path, view_root: Path, episodes: list[int]
) -> None:
    manifest = _write_manifest(tmp_path, episodes)
    with pytest.raises(compute_view_stats.ViewStatsError):
        compute_view_stats.compute_view_stats(view_root, manifest)
    assert not (view_root / "meta" / "stats.json").exists()


@pytest.mark.parametrize("episodes", [[0, "2"], [0, True], [0, 0]])
def test_fails_on_invalid_episode_values(
    tmp_path: Path, view_root: Path, episodes: list[Any]
) -> None:
    manifest = _write_manifest(tmp_path, episodes)
    with pytest.raises(compute_view_stats.ViewStatsError):
        compute_view_stats.compute_view_stats(view_root, manifest)


def test_fails_on_codebase_version_v21(tmp_path: Path) -> None:
    root = _build_dataset(tmp_path, "v2.1", list(range(len(EPISODE_LENGTHS))))
    manifest = _write_manifest(tmp_path, KEPT_EPISODES)
    with pytest.raises(compute_view_stats.ViewStatsError, match="v2.1"):
        compute_view_stats.compute_view_stats(root, manifest)
    assert not (root / "meta" / "stats.json").exists()


def test_fails_on_episode_missing_from_meta_episodes(tmp_path: Path) -> None:
    root = _build_dataset(tmp_path, "v3.0", [0, 1, 2, 3])
    manifest = _write_manifest(tmp_path, KEPT_EPISODES)
    with pytest.raises(compute_view_stats.ViewStatsError, match=r"\[4\]"):
        compute_view_stats.compute_view_stats(root, manifest)
    assert not (root / "meta" / "stats.json").exists()


def test_fails_when_stats_json_is_a_symbolic_link(tmp_path: Path, view_root: Path) -> None:
    source_stats = view_root.parent / "source" / "meta" / "stats.json"
    (view_root / "meta" / "stats.json").symlink_to(source_stats)
    manifest = _write_manifest(tmp_path, KEPT_EPISODES)
    with pytest.raises(compute_view_stats.ViewStatsError, match="symbolic link"):
        compute_view_stats.compute_view_stats(view_root, manifest)
    assert source_stats.read_text(encoding="utf-8") == "{}"


def test_main_returns_zero_and_writes_stats(tmp_path: Path, view_root: Path) -> None:
    manifest = _write_manifest(tmp_path, KEPT_EPISODES)
    status = compute_view_stats.main(["--root", str(view_root), "--manifest", str(manifest)])
    assert status == 0
    assert (view_root / "meta" / "stats.json").is_file()


def test_main_reports_failure_on_stderr(
    tmp_path: Path, view_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = _write_manifest(tmp_path, [7])
    status = compute_view_stats.main(["--root", str(view_root), "--manifest", str(manifest)])
    assert status != 0
    assert "outside the dataset" in capsys.readouterr().err
