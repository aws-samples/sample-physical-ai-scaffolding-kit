"""Convert Isaac Lab HDF5 demos to LeRobot v2.1 format — CPU-only, no Isaac Lab.

Isaac Lab's own convert script boots Omniverse solely to query static metadata
(joint names, action dims, cameras). This script skips all that: robot-level
metadata comes from a YAML (baked into the container), cameras are discovered
from the HDF5 obs keys, and the task string comes from the CLI (which is the
language_instruction from run_config.yaml).

Mirrors the logic in leisaac/scripts/convert/isaaclab2lerobot.py:
  - Skip episodes where success=False
  - Skip episodes with <10 frames
  - Skip the first 5 frames of each episode (settling)
  - Joint positions: radians → degrees → linearly mapped to motor range
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import h5py
import numpy as np
import yaml
from lerobot.datasets.lerobot_dataset import LeRobotDataset
from tqdm import tqdm

# Non-camera keys under obs/ — everything else is assumed to be a camera.
NON_CAMERA_OBS_KEYS = {
    "joint_pos",
    "joint_vel",
    "joint_pos_rel",
    "joint_vel_rel",
    "actions",
    "ee_frame_state",
    "joint_pos_target",
}

MIN_FRAMES = 10
SKIP_FIRST_FRAMES = 5


def _convert_action_rad_to_motor(
    action_rad: np.ndarray,
    joint_names: list[str],
    joint_limits_deg: dict[str, tuple[float, float]],
    motor_limits: dict[str, tuple[float, float]],
) -> np.ndarray:
    """Radians → degrees → linear map from joint limit range to motor limit range."""
    action_deg = action_rad / np.pi * 180.0
    out = np.zeros_like(action_deg, dtype=np.float32)
    for idx, name in enumerate(joint_names):
        j_lo, j_hi = joint_limits_deg[name]
        m_lo, m_hi = motor_limits[name]
        j_range = j_hi - j_lo
        m_range = m_hi - m_lo
        if j_range == 0:
            raise ValueError(f"Degenerate joint limit for {name!r}: {j_lo}=={j_hi}")
        out[..., idx] = (action_deg[..., idx] - j_lo) / j_range * m_range + m_lo
    return out


def _discover_cameras(episode_group: h5py.Group) -> dict[str, tuple[int, int, int]]:
    """Return {camera_key: (height, width, channels)} from HDF5 obs keys."""
    obs = episode_group["obs"]
    cameras: dict[str, tuple[int, int, int]] = {}
    for key in obs.keys():
        if key in NON_CAMERA_OBS_KEYS:
            continue
        shape = obs[key].shape
        # Camera datasets are (N, H, W, C). Skip anything that doesn't look like one.
        if len(shape) == 4 and shape[-1] == 3:
            cameras[key] = (int(shape[1]), int(shape[2]), int(shape[3]))
    return cameras


def _build_features(
    action_dim: int,
    joint_names: list[str],
    cameras: dict[str, tuple[int, int, int]],
    fps: int,
) -> dict:
    feature_joint_names = [f"{n}.pos" for n in joint_names]
    action_names = (
        feature_joint_names
        if action_dim == len(joint_names)
        else [f"dim_{i}" for i in range(action_dim)]
    )
    features: dict = {
        "action": {
            "dtype": "float32",
            "shape": (action_dim,),
            "names": action_names,
        },
        "observation.state": {
            "dtype": "float32",
            "shape": (len(feature_joint_names),),
            "names": feature_joint_names,
        },
    }
    for cam, (h, w, c) in cameras.items():
        features[f"observation.images.{cam}"] = {
            "dtype": "video",
            "shape": [h, w, c],
            "names": ["height", "width", "channels"],
            "video_info": {
                "video.height": h,
                "video.width": w,
                "video.codec": "av1",
                "video.pix_fmt": "yuv420p",
                "video.is_depth_map": False,
                "video.fps": float(fps),
                "video.channels": c,
                "has_audio": False,
            },
        }
    return features


def _episode_is_success(ep: h5py.Group) -> bool:
    # Isaac Lab's HDF5 writer stores success as an attribute on the episode group.
    if "success" in ep.attrs:
        return bool(ep.attrs["success"])
    if "success" in ep:
        return bool(np.asarray(ep["success"]).any())
    # If no success marker exists, treat as successful (conservative: keep data).
    return True


def _add_episode(
    dataset: LeRobotDataset,
    ep: h5py.Group,
    robot_cfg: dict,
    action_align: bool,
    task: str,
) -> bool:
    actions = np.asarray(ep["actions"])  # (N, action_dim), radians
    num_frames = actions.shape[0]
    if num_frames < MIN_FRAMES:
        return False

    joint_pos = np.asarray(ep["obs"]["joint_pos"])  # (N, len(joint_names)), radians
    obs_state = _convert_action_rad_to_motor(
        joint_pos,
        robot_cfg["joint_names"],
        robot_cfg["joint_limits_deg"],
        robot_cfg["motor_limits"],
    )
    if action_align:
        actions_out = _convert_action_rad_to_motor(
            actions,
            robot_cfg["joint_names"],
            robot_cfg["joint_limits_deg"],
            robot_cfg["motor_limits"],
        )
    else:
        actions_out = actions.astype(np.float32)

    cameras = _discover_cameras(ep)
    # Bulk-read each camera's slice once. Random-access reads on HDF5 chunks
    # over Lustre were ~96% of wall time before this change. For a 25s @ 30fps
    # episode with two 480×640 cameras, this peaks at ~1.3 GiB of resident RGB
    # bytes — fine on an m5.2xlarge (32 GiB RAM).
    cam_arrays = {
        cam: np.asarray(ep["obs"][cam][SKIP_FIRST_FRAMES:]) for cam in cameras
    }

    for rel_i in tqdm(
        range(num_frames - SKIP_FIRST_FRAMES), desc="frames", leave=False
    ):
        i = rel_i + SKIP_FIRST_FRAMES
        frame = {
            "action": actions_out[i].astype(np.float32),
            "observation.state": obs_state[i].astype(np.float32),
        }
        for cam in cameras:
            frame[f"observation.images.{cam}"] = cam_arrays[cam][rel_i]
        dataset.add_frame(frame=frame, task=task)
    return True


def _episode_group_root(f: h5py.File) -> h5py.Group:
    """Return the HDF5 group containing the episode subgroups.

    Isaac Lab nests episodes under a top-level `data/` group (keys like
    `demo_0`, `demo_1`, ...). Fall back to the file root if there's no `data/`.
    """
    if "data" in f and isinstance(f["data"], h5py.Group):
        return f["data"]
    return f


def convert(input_dir: Path, output_dir: Path, robot_cfg: dict, task: str) -> None:
    hdf5_files = sorted(input_dir.rglob("*.hdf5"))
    if not hdf5_files:
        sys.exit(f"No .hdf5 files found under {input_dir}")

    # Peek at the first episode to determine cameras + action_dim.
    with h5py.File(hdf5_files[0], "r") as f:
        root = _episode_group_root(f)
        first_ep_name = next(iter(root.keys()))
        first_ep = root[first_ep_name]
        cameras = _discover_cameras(first_ep)
        action_dim = int(np.asarray(first_ep["actions"]).shape[-1])

    joint_names = robot_cfg["joint_names"]
    action_align = action_dim == len(joint_names)
    features = _build_features(action_dim, joint_names, cameras, robot_cfg["fps"])

    # LeRobotDataset.create requires repo_id in "user/name" form. Use the output
    # dir's name — the dataset stays local, it's never pushed.
    # image_writer_threads: dataset.add_frame synchronously PIL-encodes each
    # camera frame to PNG before the per-episode save_episode step encodes
    # those PNGs to video. With the default (0 threads), that PNG write is
    # serial on our main loop and dominates wall time. A thread pool releases
    # the GIL inside zlib and gives near-linear speedup up to a few threads.
    repo_id = f"physai/{output_dir.name}"
    dataset = LeRobotDataset.create(
        repo_id=repo_id,
        fps=robot_cfg["fps"],
        root=output_dir,
        robot_type=robot_cfg["robot_type"],
        features=features,
        image_writer_threads=robot_cfg.get("image_writer_threads", 8),
    )

    saved = 0
    for hdf5_path in hdf5_files:
        print(f"Processing {hdf5_path}", flush=True)
        with h5py.File(hdf5_path, "r") as f:
            root = _episode_group_root(f)
            for ep_name in tqdm(list(root.keys()), desc="episodes"):
                ep = root[ep_name]
                if not _episode_is_success(ep):
                    print(f"  Skipping {ep_name}: not successful", flush=True)
                    continue
                ok = _add_episode(dataset, ep, robot_cfg, action_align, task)
                if ok:
                    dataset.save_episode()
                    saved += 1
                else:
                    dataset.clear_episode_buffer()
                    print(f"  Skipping {ep_name}: <{MIN_FRAMES} frames", flush=True)

    print(f"Saved {saved} episodes to {output_dir}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--robot-config", required=True, type=Path)
    parser.add_argument(
        "--task", required=True, help="LeRobot task string (language instruction)"
    )
    args = parser.parse_args()

    with open(args.robot_config) as f:
        robot_cfg = yaml.safe_load(f)

    # Normalize joint_limits_deg / motor_limits into tuples for fast access.
    robot_cfg["joint_limits_deg"] = {
        k: tuple(v) for k, v in robot_cfg["joint_limits_deg"].items()
    }
    robot_cfg["motor_limits"] = {
        k: tuple(v) for k, v in robot_cfg["motor_limits"].items()
    }

    convert(args.input_dir, args.output_dir, robot_cfg, args.task)


if __name__ == "__main__":
    main()
