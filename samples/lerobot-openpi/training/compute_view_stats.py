"""Write the normalization statistics of the kept episodes of a VAMS curation view.

LeRobot 0.6.1 ``lerobot-train`` normalizes with ``meta/stats.json`` as it is, even when
``--dataset.episodes`` selects a subset, and ``lerobot-edit-dataset recompute_stats`` reads
every row of a data file, including the rows of excluded episodes that share the file. This
script computes the statistics from the rows of the kept episodes only and writes them to
``<root>/meta/stats.json``.

The statistics follow the VAMS curation view: features that are not images, videos or text
(``observation.state``, ``action`` and so on) are computed from the kept rows with LeRobot's
``get_feature_stats`` (``min``, ``max``, ``mean``, ``std``, ``count`` and ``q01`` to ``q99``,
used by both ``MEAN_STD`` and ``QUANTILES`` normalization). Image and video features combine
the per-episode statistics stored in ``meta/episodes`` with LeRobot's ``aggregate_stats``;
they are always written because ``lerobot-train`` overwrites the camera keys with the
ImageNet statistics and fails when a key is missing.

Usage (from ``${LEROBOT_PI_BASE_DIR}`` inside the container):
    python training/compute_view_stats.py --root <DATASET_ROOT> --manifest <VAMS_VIEW_MANIFEST_FILE>

The root must be the view dataset root built by the VAMS poller, where ``meta/`` is a real
directory and ``meta/stats.json`` is not a symbolic link. The script exits with a non-zero
status and a reason on stderr when the dataset or the manifest cannot be used.
"""

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from lerobot.datasets.compute_stats import aggregate_stats, get_feature_stats
from lerobot.datasets.io_utils import write_stats

logger = logging.getLogger("compute_view_stats")

INFO_PATH = Path("meta") / "info.json"
STATS_PATH = Path("meta") / "stats.json"
EPISODES_DIR = Path("meta") / "episodes"
EPISODES_FILE_PATTERN = "*/*.parquet"
CODEBASE_VERSION_V3 = "v3.0"
VISUAL_DTYPES = ("image", "video")
TEXT_DTYPES = ("string", "language")
EPISODE_INDEX_COLUMN = "episode_index"
DATA_CHUNK_COLUMN = "data/chunk_index"
DATA_FILE_COLUMN = "data/file_index"
EPISODE_STATS_PREFIX = "stats/"
COUNT_STAT = "count"
STATISTICS_AXIS = 0
VISUAL_STATS_SHAPE = (-1, 1, 1)


class ViewStatsError(Exception):
    """The dataset or the manifest cannot be used to compute the view statistics."""


def read_json_object(path: Path, label: str) -> dict[str, Any]:
    """Read a JSON file that must hold an object.

    Args:
        path: The file to read.
        label: Name of the file used in error messages.

    Returns:
        The parsed object.

    Raises:
        ViewStatsError: If the file is missing, is not valid JSON or is not an object.
    """
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ViewStatsError(f"{label} does not exist") from error
    except ValueError as error:
        raise ViewStatsError(f"{label} is not valid JSON: {error}") from error
    if not isinstance(document, dict):
        raise ViewStatsError(f"{label} is not a JSON object")
    return document


def read_info(root: Path) -> dict[str, Any]:
    """Read ``meta/info.json`` and require a LeRobot v3.0 dataset.

    Args:
        root: The dataset root.

    Returns:
        The parsed info.json.

    Raises:
        ViewStatsError: If info.json cannot be read or the dataset is not v3.0.
    """
    info = read_json_object(root / INFO_PATH, str(INFO_PATH))
    version = info.get("codebase_version")
    if version != CODEBASE_VERSION_V3:
        raise ViewStatsError(
            f"The dataset is codebase_version {version!r}; only {CODEBASE_VERSION_V3} is supported"
        )
    total_episodes = info.get("total_episodes")
    if isinstance(total_episodes, bool) or not isinstance(total_episodes, int):
        raise ViewStatsError(f"total_episodes of {INFO_PATH} is not an integer")
    if not isinstance(info.get("data_path"), str):
        raise ViewStatsError(f"data_path of {INFO_PATH} is not a string")
    if not isinstance(info.get("features"), dict):
        raise ViewStatsError(f"features of {INFO_PATH} is not an object")
    return info


def read_kept_episodes(manifest_path: Path, total_episodes: int) -> list[int]:
    """Read the kept episodes of the view manifest and check them against the dataset.

    Args:
        manifest_path: The view's ``curation_manifest.json``.
        total_episodes: ``total_episodes`` of ``meta/info.json``.

    Returns:
        The kept episode indices, sorted.

    Raises:
        ViewStatsError: If ``episodes`` is empty, holds a non-integer or duplicate value, or
            holds an index outside ``0`` to ``total_episodes - 1``.
    """
    manifest = read_json_object(manifest_path, "The curation manifest")
    episodes = manifest.get("episodes")
    if not isinstance(episodes, list) or not episodes:
        raise ViewStatsError("episodes of the curation manifest is not a non-empty list")
    if any(isinstance(index, bool) or not isinstance(index, int) for index in episodes):
        raise ViewStatsError("episodes of the curation manifest holds a non-integer value")
    if len(set(episodes)) != len(episodes):
        raise ViewStatsError("episodes of the curation manifest holds a duplicate index")
    out_of_range = sorted(index for index in episodes if not 0 <= index < total_episodes)
    if out_of_range:
        raise ViewStatsError(
            f"episodes {out_of_range} are outside the dataset (total_episodes {total_episodes})"
        )
    return sorted(episodes)


def read_kept_episode_rows(root: Path, kept: list[int]) -> list[dict[str, Any]]:
    """Read the ``meta/episodes`` rows of the kept episodes.

    Args:
        root: The dataset root.
        kept: The kept episode indices.

    Returns:
        One row per kept episode, in ``kept`` order.

    Raises:
        ViewStatsError: If ``meta/episodes`` has no file or lacks a row for a kept episode.
    """
    paths = sorted((root / EPISODES_DIR).glob(EPISODES_FILE_PATTERN))
    if not paths:
        raise ViewStatsError(f"The dataset has no {EPISODES_DIR} file")
    filters = [(EPISODE_INDEX_COLUMN, "in", kept)]
    rows = pa.concat_tables(pq.read_table(path, filters=filters) for path in paths).to_pylist()
    rows_by_episode = {int(row[EPISODE_INDEX_COLUMN]): row for row in rows}
    missing = [index for index in kept if index not in rows_by_episode]
    if missing:
        raise ViewStatsError(f"{EPISODES_DIR} has no row for episodes {missing}")
    return [rows_by_episode[index] for index in kept]


def data_file_paths(episode_rows: list[dict[str, Any]], data_path_format: str) -> list[str]:
    """Return the data files that hold at least one kept episode.

    Args:
        episode_rows: The ``meta/episodes`` rows of the kept episodes.
        data_path_format: ``data_path`` of ``meta/info.json``.

    Returns:
        The paths relative to the dataset root, sorted.
    """
    locations = {(int(row[DATA_CHUNK_COLUMN]), int(row[DATA_FILE_COLUMN])) for row in episode_rows}
    return sorted(
        data_path_format.format(chunk_index=chunk_index, file_index=file_index)
        for chunk_index, file_index in locations
    )


def recomputed_features(info: dict[str, Any]) -> list[str]:
    """Return the features whose statistics are computed from the data rows.

    Args:
        info: The dataset's info.json.

    Returns:
        Every feature that is not an image, a video or text, in info.json order.
    """
    return [
        name
        for name, feature in info["features"].items()
        if feature.get("dtype") not in VISUAL_DTYPES + TEXT_DTYPES
    ]


def visual_features(info: dict[str, Any]) -> list[str]:
    """Return the image and video features, in info.json order.

    Args:
        info: The dataset's info.json.

    Returns:
        Every feature whose dtype is ``image`` or ``video``.
    """
    return [
        name for name, feature in info["features"].items() if feature.get("dtype") in VISUAL_DTYPES
    ]


def read_kept_rows(
    root: Path, data_paths: list[str], columns: list[str], kept: list[int]
) -> pa.Table:
    """Read the data rows of the kept episodes.

    Args:
        root: The dataset root.
        data_paths: The data files that hold a kept episode, relative to the root.
        columns: Data columns to read.
        kept: The kept episode indices.

    Returns:
        The given columns of every row whose ``episode_index`` is kept.

    Raises:
        ViewStatsError: If a data file is missing or no row is read.
    """
    filters = [(EPISODE_INDEX_COLUMN, "in", kept)]
    tables = []
    for relative_path in data_paths:
        path = root / relative_path
        if not path.is_file():
            raise ViewStatsError(f"The data file {relative_path} does not exist")
        tables.append(pq.read_table(path, columns=columns, filters=filters))
    table = pa.concat_tables(tables)
    if table.num_rows == 0:
        raise ViewStatsError("The data files hold no row of the kept episodes")
    return table


def column_values(table: pa.Table, feature: str) -> np.ndarray:
    """Return one feature column as an array with one row per frame.

    Args:
        table: Data rows holding the feature column.
        feature: The column name.

    Returns:
        A ``(frames,)`` array for a scalar feature, ``(frames, dim)`` for a vector feature.
    """
    column = table.column(feature).combine_chunks()
    is_list = pa.types.is_fixed_size_list(column.type) or pa.types.is_list(column.type)
    if is_list and not pa.types.is_nested(column.type.value_type):
        return column.flatten().to_numpy(zero_copy_only=False).reshape(len(column), -1)
    if pa.types.is_nested(column.type):
        return np.asarray(column.to_pylist())
    return column.to_numpy(zero_copy_only=False)


def table_stats(table: pa.Table, features: list[str]) -> dict[str, dict[str, np.ndarray]]:
    """Compute the statistics of the given features over every row of a data table.

    Args:
        table: Data rows holding a column for each feature.
        features: Data columns to compute statistics for.

    Returns:
        Feature name to ``min``, ``max``, ``mean``, ``std``, ``count`` and ``q01`` to ``q99``.
    """
    stats: dict[str, dict[str, np.ndarray]] = {}
    for feature in features:
        values = column_values(table, feature)
        stats[feature] = get_feature_stats(values, axis=STATISTICS_AXIS, keepdims=values.ndim == 1)
    return stats


def episode_stat_value(value: Any, stat_name: str) -> np.ndarray:
    """Return one stored per-episode statistic of an image or video feature as an array.

    Args:
        value: The ``stats/<feature>/<stat>`` cell of a ``meta/episodes`` row.
        stat_name: The statistic name.

    Returns:
        ``(1,)`` for ``count``, the channel-first ``(C, 1, 1)`` shape otherwise.
    """
    if stat_name == COUNT_STAT:
        return np.atleast_1d(np.asarray(value))
    return np.asarray(value, dtype=np.float64).reshape(VISUAL_STATS_SHAPE)


def aggregate_episode_stats(
    episode_rows: list[dict[str, Any]], features: list[str]
) -> dict[str, dict[str, np.ndarray]]:
    """Combine the stored per-episode statistics of the kept episodes with ``aggregate_stats``.

    Args:
        episode_rows: The ``meta/episodes`` rows of the kept episodes.
        features: Image and video features.

    Returns:
        Feature name to the combined statistics.

    Raises:
        ViewStatsError: If ``meta/episodes`` has no stored statistics for a feature.
    """
    if not features:
        return {}
    columns_by_feature = {
        feature: [
            column
            for column in episode_rows[0]
            if column.startswith(f"{EPISODE_STATS_PREFIX}{feature}/")
        ]
        for feature in features
    }
    without_stats = [feature for feature, columns in columns_by_feature.items() if not columns]
    if without_stats:
        raise ViewStatsError(f"{EPISODES_DIR} has no per-episode statistics for {without_stats}")
    episode_stats = [
        {
            feature: {
                column.rsplit("/", 1)[1]: episode_stat_value(row[column], column.rsplit("/", 1)[1])
                for column in columns
            }
            for feature, columns in columns_by_feature.items()
        }
        for row in episode_rows
    ]
    return aggregate_stats(episode_stats)


def check_stats_destination(root: Path) -> None:
    """Require ``meta/stats.json`` to be writable without replacing a symbolic link.

    Args:
        root: The dataset root.

    Raises:
        ViewStatsError: If ``meta/`` is not a real directory or ``meta/stats.json`` is a link.
    """
    meta_dir = root / STATS_PATH.parent
    if meta_dir.is_symlink() or not meta_dir.is_dir():
        raise ViewStatsError(f"{STATS_PATH.parent} of the dataset root is not a real directory")
    if (root / STATS_PATH).is_symlink():
        raise ViewStatsError(f"{STATS_PATH} is a symbolic link; it is not replaced")


def compute_view_stats(root: Path, manifest_path: Path) -> tuple[int, int]:
    """Compute the statistics of the kept episodes and write ``meta/stats.json``.

    Args:
        root: The view dataset root.
        manifest_path: The view's ``curation_manifest.json``.

    Returns:
        The number of kept episodes and the number of their frames.

    Raises:
        ViewStatsError: If the dataset or the manifest cannot be used.
    """
    check_stats_destination(root)
    info = read_info(root)
    kept = read_kept_episodes(manifest_path, info["total_episodes"])
    episode_rows = read_kept_episode_rows(root, kept)
    features = recomputed_features(info)
    columns = list(dict.fromkeys([*features, EPISODE_INDEX_COLUMN]))
    kept_rows = read_kept_rows(
        root, data_file_paths(episode_rows, info["data_path"]), columns, kept
    )
    stats: dict[str, Any] = table_stats(kept_rows, features)
    stats.update(aggregate_episode_stats(episode_rows, visual_features(info)))
    write_stats(stats, root)
    return len(kept), kept_rows.num_rows


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse the command line.

    Args:
        argv: Arguments without the program name.

    Returns:
        The parsed arguments with ``root`` and ``manifest``.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, required=True, help="View dataset root")
    parser.add_argument(
        "--manifest", type=Path, required=True, help="The view's curation_manifest.json"
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    """Run the script.

    Args:
        argv: Arguments without the program name.

    Returns:
        The exit status: 0 on success, 1 when the statistics cannot be computed.
    """
    # Importing lerobot installs a WARNING root handler, so force replaces it to show INFO.
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", force=True
    )
    args = parse_args(argv)
    try:
        episode_count, frame_count = compute_view_stats(args.root, args.manifest)
    except ViewStatsError as error:
        print(f"compute_view_stats: {error}", file=sys.stderr)
        return 1
    logger.info(
        "Wrote %s from %d kept episodes (%d frames)", STATS_PATH, episode_count, frame_count
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
