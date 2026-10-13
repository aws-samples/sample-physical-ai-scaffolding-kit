# so101-rosbag-converter

**Role.** rosbag2 (mcap) — recorded by pask-edge teleop on a real SO-101 arm —
into a `LeRobotDataset` **v3.0** consumable by lerobot 0.6+ trainers
(SmolVLA, ACT/DiffPolicy, `lerobot-train`).

Runs on the `cpu` partition; `physai run` invokes it as the `convert` stage.

## Contract

- **Input**: `/fsx/raw/<name>/` — a parent directory whose children are per-episode
  rosbag2 directories (`metadata.yaml` + `<episode>.mcap`). `<name>` matches
  `--raw <name>` on `physai run`.
- **Output**: `/fsx/datasets/<name>/` — a LeRobot v3.0 dataset with
  `data/`, `videos/`, `meta/` (parquet + info.json + episodes.jsonl).
- Fallback for a single-episode input: if `<name>/` itself contains
  `metadata.yaml`, it is treated as one episode.

## Output format — LeRobot v3.0 only

The container targets **`codebase_version: v3.0`** (the schema introduced with
lerobot 0.6). Downstream trainers that pin lerobot 0.3.x — e.g. GR00T
(`Isaac-GR00T`) and OpenPI — still expect **v2.1** and will refuse to load
v3.0 datasets. Use of this converter therefore commits the pipeline to
`lerobot-train`-based policies (SmolVLA, etc.) as the primary training path.

Rationale (see [../../plan.md](../../../../plan.md) for the full trade-off):
this sample opts to move the source-of-truth to v3.0 rather than emit v2.1 and
convert per-run — SmolVLA is the intended trainer, and dual-emitting adds
lasting complexity for no immediate benefit.

If you need a v2.1 output for a legacy trainer, the reverse migration is not
provided; either downgrade this container's `LEROBOT_VERSION` back to `0.3.3`
(and revert the API calls to the `add_frame(frame, task=)` shape), or convert
downstream with `python -m lerobot.scripts.convert_dataset_v21_to_v30` after
regenerating a v2.1 dataset with an older container.

## Feature schema

| Feature key | Type | Shape | Source topic (default) |
|---|---|---|---|
| `observation.state` | `float32` | `(6,)` | `/follower/joint_states` (position) |
| `action` | `float32` | `(6,)` | `/follower/forward_controller/commands` |
| `observation.images.front` | `video` | `(H, W, 3)` | `/static_camera/image_raw/compressed` |
| `observation.images.wrist` | `video` | `(H, W, 3)` | `/follower/image_raw/compressed` |

Joint order is fixed to
`[shoulder_pan, shoulder_lift, elbow_flex, wrist_flex, wrist_roll, gripper]`
(LeRobot convention). `robot_configs/so101.yaml` maps ROS joint names → LeRobot
joint names and defines the joint / motor limits used for degree-to-motor-tick
conversion.

Cameras are auto-discovered by name from the `topics.cameras` map — swap
topics or add/remove cameras there, then `physai build --rebuild` this
container.

## Task labels (per-episode)

Resolution order for the task string written to each episode:

1. `rosbag2_bagfile_information.custom_data.task` in the episode's
   `metadata.yaml` (preferred; pask-edge records it during teleop)
2. `--task <str>` CLI arg, sourced from
   `run_config.rosbag.language_instruction` at pipeline time
3. Empty (skip; converter warns)

## Invocation

Direct (inside the container):

```bash
/app/convert.sh /fsx/raw/<name> /fsx/datasets/<name>
```

`convert.sh` reads `RUN_CONFIG` (exported by the sbatch wrapper) to pull
`rosbag.language_instruction`, then invokes
`python /app/convert_rosbag_to_lerobot.py …`.

From `physai`:

```bash
physai run --config <run_config.yaml> --raw <name>
# or convert-only:
physai convert --config <run_config.yaml> --raw <name>
```

## Dependencies (installed at build time)

- Python 3.12 (base image `python:3.12-slim-bookworm`)
- `lerobot[dataset]==0.6.1` (v3.0 writer path, CPU torch wheels; latest PyPI release. 0.6.2 exists only on `main` — pinning to a tagged release keeps this container reproducible.)
- `rosbags>=0.10` (pure-Python rosbag2 + mcap reader; no ROS2 install needed)
- `Pillow` (decodes JPEG payload from `sensor_msgs/CompressedImage`)
- `pyyaml`

## Failure modes to know about

- **Multi-episode inputs use only shared topic names.** If two episodes were
  recorded with different topic sets, only the intersection is exported.
- **JPEG-only cameras.** `CompressedImage` is assumed; raw image topics
  (`sensor_msgs/Image`) are not supported.
- **`ros_distro` per episode.** Each episode's `metadata.yaml` is inspected for
  its `ros_distro` to pick the right rosbags typestore. Unknown distros fall
  back to `rolling`; mixed-distro inputs are supported.
- **Empty episodes.** Bags with no `follower_joint_states` messages are
  skipped with a log line and do not appear in the output.
- **Skipped frames.** Frames missing any camera image at the sync tolerance
  are dropped from that episode; the count is reported.
