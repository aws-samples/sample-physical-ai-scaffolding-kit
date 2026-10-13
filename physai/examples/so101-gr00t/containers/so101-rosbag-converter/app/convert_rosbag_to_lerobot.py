"""rosbag2 (mcap) → LeRobot v3.0 変換スクリプト

pask-edge の実機テレオペで記録した rosbag2 (mcap) を、SmolVLA / lerobot-train
(lerobot==0.6.2) が読み込める LeRobot v3.0 形式に変換する。

v3.0 は lerobot 0.6 で導入された新スキーマで、v2.1 とは互換性が無い。
本サンプルは SmolVLA を主要ターゲットにするため v3.0 のみをサポートする。
既存 GR00T / OpenPI 系トレーナは lerobot 0.3.x に依存しており v2.1 を要求する
点に注意（詳細は container/README.md 参照）。

想定入力レイアウト:
    <input_dir>/
    ├── episode_000000/          ← 各サブディレクトリが 1 エピソード = 1 rosbag2
    │   ├── metadata.yaml
    │   └── episode_000000_0.mcap
    ├── episode_000001/
    │   ├── metadata.yaml
    │   └── episode_000001_0.mcap
    └── ...

または <input_dir> 直下に metadata.yaml + .mcap がある場合は単一エピソードとして
処理する（後方互換）。

トピックの想定 (デフォルトの robot_configs/so101.yaml):
- observation.state ← /follower/joint_states (sensor_msgs/JointState)
- action           ← /follower/forward_controller/commands (std_msgs/Float64MultiArray)
- image (front)    ← /static_camera/image_raw/compressed (sensor_msgs/CompressedImage)
- image (wrist)    ← /follower/image_raw/compressed (sensor_msgs/CompressedImage)

タスクラベルの解決順:
1. 各エピソードの metadata.yaml の rosbag2_bagfile_information.custom_data.task
2. --task 引数 (無ければ空文字。両方欠けている場合はエラー)

ROS2 typestore は各エピソードの metadata.yaml の ros_distro から自動選択する
(humble / iron / jazzy / kilted / rolling)。
"""

from __future__ import annotations

import argparse
import io
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import yaml
from PIL import Image
from rosbags.highlevel import AnyReader
from rosbags.typesys import Stores, get_typestore

# lerobot 0.6+ ships LeRobotDataset under `lerobot.datasets`. Earlier legacy
# paths (`lerobot.common.datasets.*`) are not supported by this container.
from lerobot.datasets.lerobot_dataset import LeRobotDataset


def load_config(config_path: str) -> dict[str, Any]:
    with open(config_path) as f:
        return yaml.safe_load(f)


def rad_to_deg(rad: float) -> float:
    return rad * 180.0 / math.pi


def linear_map(
    value: float,
    src_range: tuple[float, float],
    dst_range: tuple[float, float],
) -> float:
    src_min, src_max = src_range
    dst_min, dst_max = dst_range
    normalized = (value - src_min) / (src_max - src_min)
    return dst_min + normalized * (dst_max - dst_min)


def deg_to_motor(
    values_deg: dict[str, float],
    lerobot_joint_names: list[str],
    joint_limits_deg: dict[str, list[float]],
    motor_limits: dict[str, list[float]],
) -> list[float]:
    motor_values = []
    for lr_name in lerobot_joint_names:
        deg_val = values_deg.get(lr_name)
        if deg_val is None:
            motor_values.append(0.0)
            continue
        jl = tuple(joint_limits_deg[lr_name])
        ml = tuple(motor_limits[lr_name])
        clamped = max(jl[0], min(jl[1], deg_val))
        motor_values.append(linear_map(clamped, jl, ml))
    return motor_values


def joint_state_to_motor(
    positions_rad: list[float],
    ros_joint_names: list[str],
    joint_mapping: dict[str, str],
    lerobot_joint_names: list[str],
    joint_limits_deg: dict[str, list[float]],
    motor_limits: dict[str, list[float]],
) -> list[float]:
    values_deg: dict[str, float] = {}
    for ros_name, pos in zip(ros_joint_names, positions_rad):
        lr_name = joint_mapping.get(ros_name, ros_name)
        values_deg[lr_name] = rad_to_deg(pos)
    return deg_to_motor(values_deg, lerobot_joint_names, joint_limits_deg, motor_limits)


def commands_to_motor(
    command_values: list[float],
    commands_order: list[str],
    lerobot_joint_names: list[str],
    joint_limits_deg: dict[str, list[float]],
    motor_limits: dict[str, list[float]],
) -> list[float]:
    values_deg: dict[str, float] = {
        name: rad_to_deg(float(v))
        for name, v in zip(commands_order, command_values)
    }
    return deg_to_motor(values_deg, lerobot_joint_names, joint_limits_deg, motor_limits)


def compressed_image_to_pil(msg: Any) -> Image.Image:
    return Image.open(io.BytesIO(bytes(msg.data))).convert("RGB")


ROS_DISTRO_STORE_ATTRS = {
    "humble": "ROS2_HUMBLE",
    "iron": "ROS2_IRON",
    "jazzy": "ROS2_JAZZY",
    "kilted": "ROS2_KILTED",
    "rolling": "ROS2_ROLLING",
}


def get_typestore_for_distro(distro: str | None):
    """metadata.yaml の ros_distro に対応する typestore を返す。未対応は最新を fallback。"""
    if distro:
        attr = ROS_DISTRO_STORE_ATTRS.get(distro.lower())
        if attr and hasattr(Stores, attr):
            return get_typestore(getattr(Stores, attr))
    for attr in ("LATEST", "ROS2_JAZZY", "ROS2_HUMBLE"):
        if hasattr(Stores, attr):
            return get_typestore(getattr(Stores, attr))
    raise RuntimeError("No supported ROS2 typestore found in rosbags")


def read_bag_metadata(bag_path: Path) -> dict[str, Any]:
    with open(bag_path / "metadata.yaml") as f:
        return yaml.safe_load(f) or {}


def read_topics(
    bag_path: Path,
    target_topics: list[str],
    typestore,
) -> dict[str, list[tuple[int, Any]]]:
    """rosbag2 を読み取り、対象トピックごとにタイムスタンプ付きメッセージを返す。"""
    result: dict[str, list[tuple[int, Any]]] = {t: [] for t in target_topics}
    with AnyReader([bag_path], default_typestore=typestore) as reader:
        connections = [c for c in reader.connections if c.topic in target_topics]
        for connection, timestamp, rawdata in reader.messages(connections=connections):
            msg = reader.deserialize(rawdata, connection.msgtype)
            result[connection.topic].append((timestamp, msg))
    return result


def build_time_aligned_frames(
    topic_msgs: dict[str, list[tuple[int, Any]]],
    follower_topic: str,
    command_topic: str,
    camera_topics: dict[str, str],
) -> list[dict[str, Any]]:
    """follower_joint_states のタイムスタンプを基準に最近傍マッチングでフレームを構築。"""
    follower_msgs = topic_msgs.get(follower_topic, [])
    if not follower_msgs:
        return []

    def find_nearest(sorted_msgs: list[tuple[int, Any]], target_ts: int) -> Any | None:
        if not sorted_msgs:
            return None
        best_idx = 0
        best_diff = abs(sorted_msgs[0][0] - target_ts)
        for i in range(1, len(sorted_msgs)):
            diff = abs(sorted_msgs[i][0] - target_ts)
            if diff < best_diff:
                best_diff = diff
                best_idx = i
            elif sorted_msgs[i][0] > target_ts:
                break
        return sorted_msgs[best_idx][1]

    command_msgs = topic_msgs.get(command_topic, [])
    frames = []
    for ts, follower_msg in follower_msgs:
        frame: dict[str, Any] = {
            "timestamp": ts,
            "follower": follower_msg,
            "command": find_nearest(command_msgs, ts),
        }
        for cam_name, cam_topic in camera_topics.items():
            frame[f"camera_{cam_name}"] = find_nearest(topic_msgs.get(cam_topic, []), ts)
        frames.append(frame)
    return frames


def discover_episodes(input_dir: Path) -> list[Path]:
    """input_dir から rosbag2 ディレクトリのリストを返す。

    - input_dir 直下に metadata.yaml があれば [input_dir] を返す (単一エピソード)
    - なければ子ディレクトリで metadata.yaml を持つものを列挙 (マルチエピソード)
    """
    if (input_dir / "metadata.yaml").is_file():
        return [input_dir]
    episodes = sorted(p for p in input_dir.iterdir() if p.is_dir() and (p / "metadata.yaml").is_file())
    if not episodes:
        raise FileNotFoundError(
            f"No rosbag2 directories found under {input_dir}. "
            "Each episode must be a directory containing metadata.yaml."
        )
    return episodes


def resolve_task(metadata: dict[str, Any], fallback: str | None) -> str:
    """metadata.yaml の custom_data.task を最優先、なければ fallback を使う。"""
    info = metadata.get("rosbag2_bagfile_information") or {}
    custom = info.get("custom_data") or {}
    task = custom.get("task")
    if task:
        return str(task)
    if fallback:
        return fallback
    raise ValueError(
        "No task label available: metadata.custom_data.task is empty and no --task fallback was given"
    )


def convert(
    input_dir: str,
    output_dir: str,
    robot_config_path: str,
    task_fallback: str | None,
    repo_id: str = "local/so101_rosbag",
) -> None:
    config = load_config(robot_config_path)
    joint_names = config["joint_names"]
    joint_limits_deg = config["joint_limits_deg"]
    motor_limits = config["motor_limits"]
    follower_mapping = config["follower_joint_mapping"]
    commands_order = config["follower_commands_order"]
    fps = config["fps"]
    robot_type = config["robot_type"]
    camera_topics = config["topics"]["cameras"]
    follower_topic = config["topics"]["follower_joint_states"]
    command_topic = config["topics"]["follower_commands"]

    target_topics = [follower_topic, command_topic, *camera_topics.values()]

    episodes = discover_episodes(Path(input_dir))
    print(f"Found {len(episodes)} episode directory(ies) under {input_dir}")

    # 最初のエピソードのメタデータから画像サイズを推定 (features 定義に使う)
    first_meta = read_bag_metadata(episodes[0])
    first_distro = (first_meta.get("rosbag2_bagfile_information") or {}).get("ros_distro")
    first_typestore = get_typestore_for_distro(first_distro)
    first_msgs = read_topics(episodes[0], target_topics, first_typestore)
    cam_h, cam_w = 480, 640
    for cam_topic in camera_topics.values():
        msgs = first_msgs.get(cam_topic, [])
        if msgs:
            pil = compressed_image_to_pil(msgs[0][1])
            cam_w, cam_h = pil.size
            break

    features: dict[str, Any] = {
        "observation.state": {
            "dtype": "float32",
            "shape": (len(joint_names),),
            "names": [joint_names],
        },
        "action": {
            "dtype": "float32",
            "shape": (len(joint_names),),
            "names": [joint_names],
        },
    }
    for cam_name in camera_topics:
        features[f"observation.images.{cam_name}"] = {
            "dtype": "video",
            "shape": (cam_h, cam_w, 3),
            "names": ["height", "width", "channels"],
        }

    dataset = LeRobotDataset.create(
        repo_id=repo_id,
        fps=fps,
        robot_type=robot_type,
        features=features,
        root=output_dir,
    )

    total_frames = 0
    total_skipped = 0
    saved_episodes = 0

    for ep_idx, ep_path in enumerate(episodes):
        meta = read_bag_metadata(ep_path)
        info = meta.get("rosbag2_bagfile_information") or {}
        distro = info.get("ros_distro")
        typestore = get_typestore_for_distro(distro) if distro != first_distro else first_typestore
        task = resolve_task(meta, task_fallback)

        if ep_idx == 0:
            topic_msgs = first_msgs
        else:
            topic_msgs = read_topics(ep_path, target_topics, typestore)

        frames = build_time_aligned_frames(topic_msgs, follower_topic, command_topic, camera_topics)
        if not frames:
            print(f"  [{ep_idx}] {ep_path.name}: no follower_joint_states, skipping")
            continue

        # 初回エピソードで、実際に観測された ROS joint 名を出力 (設定合わせの手がかり)
        if ep_idx == 0 and frames[0]["follower"] is not None:
            observed = list(frames[0]["follower"].name)
            print(f"  Observed JointState.name: {observed}")

        skipped = 0
        for frame in frames:
            follower_msg = frame["follower"]
            command_msg = frame["command"]

            state = joint_state_to_motor(
                list(follower_msg.position),
                list(follower_msg.name),
                follower_mapping,
                joint_names,
                joint_limits_deg,
                motor_limits,
            )

            if command_msg is not None:
                action = commands_to_motor(
                    list(command_msg.data),
                    commands_order,
                    joint_names,
                    joint_limits_deg,
                    motor_limits,
                )
            else:
                action = state

            # lerobot 0.6 requires the frame dict itself to include the task
            # string (was a `task=` kwarg on add_frame in 0.3).
            frame_dict: dict[str, Any] = {
                "observation.state": np.array(state, dtype=np.float32),
                "action": np.array(action, dtype=np.float32),
                "task": task,
            }

            all_cameras_available = True
            for cam_name in camera_topics:
                cam_msg = frame.get(f"camera_{cam_name}")
                if cam_msg is None:
                    all_cameras_available = False
                    break
                frame_dict[f"observation.images.{cam_name}"] = compressed_image_to_pil(cam_msg)

            if not all_cameras_available:
                skipped += 1
                continue

            dataset.add_frame(frame_dict)

        dataset.save_episode()
        saved_episodes += 1
        kept = len(frames) - skipped
        total_frames += kept
        total_skipped += skipped
        print(f"  [{ep_idx}] {ep_path.name}: {kept} frames (skipped {skipped}) task='{task}'")

    # lerobot 0.6 requires an explicit finalize() to flush parquet footers and
    # close writers; without it the dataset is left in a partial state.
    dataset.finalize()

    print("Conversion complete:")
    print(f"  Output:   {output_dir}")
    print(f"  Episodes: {saved_episodes}")
    print(f"  Frames:   {total_frames} (skipped {total_skipped} without full camera coverage)")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="rosbag2 (mcap) → LeRobot v3.0 converter for PASK",
    )
    parser.add_argument(
        "--input-dir",
        required=True,
        help="Parent directory containing per-episode rosbag2 subdirectories (or a single rosbag2 dir)",
    )
    parser.add_argument("--output-dir", required=True, help="LeRobot dataset output directory")
    parser.add_argument(
        "--robot-config",
        default="/app/robot_configs/so101.yaml",
        help="Robot config YAML path",
    )
    parser.add_argument(
        "--task",
        default=None,
        help="Fallback task label (used when metadata.custom_data.task is missing)",
    )
    parser.add_argument(
        "--repo-id",
        default="local/so101_rosbag",
        help="LeRobot dataset repo ID",
    )
    args = parser.parse_args()

    convert(
        input_dir=args.input_dir,
        output_dir=args.output_dir,
        robot_config_path=args.robot_config,
        task_fallback=args.task,
        repo_id=args.repo_id,
    )


if __name__ == "__main__":
    main()
