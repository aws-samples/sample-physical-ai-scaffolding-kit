# SO-101 + GR00T Example

Ready-to-run pipelines that fine-tune and evaluate NVIDIA **GR00T** VLA models
on the **SO-101** follower arm in **LeIsaac** (Isaac Sim). Four variants ship
out of the box: two tasks (`LiftCube`, `PickOrange`) × two model versions
(`gr00t-n1.5`, `gr00t-n1.6`).

For the platform itself (cluster deployment, CLI install, job management), see
[README.md](../../README.md) and [docs/en/PHYSAI_CLI.md](../../docs/en/PHYSAI_CLI.md).
This file documents only the contents of this example and how to run them.

## Contents

| Path | Purpose |
|------|---------|
| `project.yaml` | Shared container defaults (base image, env vars, pinned refs) |
| `containers/` | Build recipes for the six containers the pipeline uses |
| `configs/` | `run_config.yaml` files — one per (task, model version) combination |
| `model_configs/` | Per-model, per-camera-layout GR00T modality configs |

## Pipelines at a glance

All four configs execute the same three-stage pipeline:

```
/fsx/raw/<name>/   ──►  convert  ──►  /fsx/datasets/<name>/
                        (CPU)
                                      /fsx/datasets/<name>/
                                              │
                                              ▼
                                      ──►  train  ──►  /fsx/checkpoints/<run-id>/
                                           (L40S GPU)
                                                      │
                                                      ▼
                                              ──►  eval  ──►  /fsx/evaluations/<run-id>/
                                                   (L40S GPU)        (metrics.json)
```

### Configs

| Config file | Task | Cameras | Model | Instruction |
|-------------|------|---------|-------|-------------|
| `configs/so101_liftcube_gr00t-n1.5.yaml`   | LiftCube   | front      | GR00T N1.5 | "Lift the red cube up" |
| `configs/so101_liftcube_gr00t-n1.6.yaml`   | LiftCube   | front      | GR00T N1.6 | "Lift the red cube up" |
| `configs/so101_pickorange_gr00t-n1.5.yaml` | PickOrange | front+wrist | GR00T N1.5 | "Pick up the orange and place it on the plate" |
| `configs/so101_pickorange_gr00t-n1.6.yaml` | PickOrange | front+wrist | GR00T N1.6 | "Pick up the orange and place it on the plate" |

LiftCube uses a single front camera; PickOrange uses both the front camera
and the wrist camera — that's the only reason LiftCube configs pair with
`so101-singlecam` model configs and PickOrange configs pair with
`so101-dualcam`.

## Containers

Six container recipes under `containers/`. Three inherit the shared
`base_image: nvcr.io/nvidia/cuda:12.8.1-cudnn-devel-ubuntu24.04` from
`project.yaml` (`leisaac-runtime`, `gr00t-n1.5-trainer`, `gr00t-n1.6-trainer`).
The two eval runtimes layer on top of `leisaac-runtime` via `base_container:`.
`so101-converter` overrides with its own CPU-only `base_image`.

```
leisaac-runtime              (CUDA base; base simulation runtime — no entrypoint)
├── leisaac-gr00t-n1.5       (base_container: leisaac-runtime) + Isaac-GR00T N1.5 → eval.sh
└── leisaac-gr00t-n1.6       (base_container: leisaac-runtime) + Isaac-GR00T N1.6 → eval.sh
gr00t-n1.5-trainer           (CUDA base) standalone → train.sh
gr00t-n1.6-trainer           (CUDA base) standalone → train.sh
so101-converter              (python:3.12-slim-bookworm, CPU-only) → convert.sh
```

| Container | Partition | GRES | Stage | Notes |
|-----------|-----------|------|-------|-------|
| `leisaac-runtime`       | gpu | gpu:1 | — (base) | IsaacSim 5.1.0 + LeIsaac @ pinned commit + scene assets + shader warmup. Not run directly. |
| `leisaac-gr00t-n1.5`    | gpu | gpu:1 | eval     | Extends `leisaac-runtime`. Starts GR00T N1.5 policy server (`inference_service.py`) + LeIsaac sim client. |
| `leisaac-gr00t-n1.6`    | gpu | gpu:1 | eval     | Extends `leisaac-runtime`. Starts GR00T N1.6 policy server (`run_gr00t_server.py`) with `action_horizon=16`. |
| `gr00t-n1.5-trainer`    | gpu | gpu:1 | train    | Fine-tune `nvidia/GR00T-N1.5-3B` via `gr00t_finetune.py` (batch=32). |
| `gr00t-n1.6-trainer`    | gpu | gpu:1 | train    | Fine-tune `nvidia/GR00T-N1.6-3B` via `launch_finetune.py` (global_batch=12). |
| `so101-converter`       | cpu | —     | convert  | HDF5 → LeRobot v2.1. Pure `h5py`/`numpy`/`lerobot==0.3.3`; no Isaac Lab. |

Building any of them uses the platform's `physai build` command — for example:

```bash
# Base first (the leisaac-gr00t-* images layer on it via base_container):
physai build containers/leisaac-runtime

# Eval runtime for the GR00T version you plan to use:
physai build containers/leisaac-gr00t-n1.6

# Trainer for that version:
physai build containers/gr00t-n1.6-trainer

# Converter (HDF5 → LeRobot):
physai build containers/so101-converter
```

Training and eval need the matching version pair: pair `gr00t-n1.5-trainer` with
`leisaac-gr00t-n1.5`, or `gr00t-n1.6-trainer` with `leisaac-gr00t-n1.6`. The
run configs already wire this up correctly.

## Model configs

`model_configs/` carries per-model, per-camera-layout configuration that
tells GR00T which joints, cameras, and action representation to use. The
converter does not read these; the trainer and eval containers do.

```
model_configs/
├── gr00t-n1.5/
│   ├── so101-singlecam/    modality.json + data_config.py         (LiftCube)
│   └── so101-dualcam/      modality.json + data_config.py         (PickOrange)
└── gr00t-n1.6/
    ├── so101-singlecam/    modality.json + modality_config.py     (LiftCube)
    └── so101-dualcam/      modality.json + modality_config.py     (PickOrange)
```

`modality.json` is consumed by both versions and pins the SO-101 embodiment:

- `state` and `action` are 6-DoF vectors. The first 5 elements (indices 0–4)
  are the arm joints, exposed as the `single_arm` group. The 6th element
  (index 5) is the gripper, exposed as the `gripper` group. LeRobot ranges
  are half-open, so the file writes `single_arm: {start:0, end:5}` and
  `gripper: {start:5, end:6}`.
- `video.front` (singlecam) or `video.front` + `video.wrist` (dualcam).
- `annotation.human.task_description` is sourced from `task_index` (LeRobot's task table).

The Python files differ by GR00T version:

- **N1.5** uses `data_config.py`, extending `gr00t.experiment.data_config.So100DataConfig`
  and overriding `video_keys` (singlecam: `["video.front"]`; dualcam adds
  `"video.wrist"`).
- **N1.6** uses `modality_config.py`, constructing a full `ModalityConfig` and
  registering it against `EmbodimentTag.NEW_EMBODIMENT`. Both the singlecam
  and dualcam variants declare an all-ABSOLUTE action representation for the
  `single_arm` + `gripper` joint groups.

These are referenced from each run config's `model.config_dir` (e.g.
`gr00t-n1.5/so101-dualcam`). The `physai` CLI resolves that path against
your `model_config_roots` (see `~/.physai/config.yaml`) and rsyncs the
matched directory into the pipeline working dir.

## Running the example end-to-end

Prerequisites: the platform is deployed and your `physai` CLI is configured
(see [DEPLOYMENT.md](../../docs/en/DEPLOYMENT.md) and
[PHYSAI_CLI.md](../../docs/en/PHYSAI_CLI.md)). The commands below assume the
repo root is the current directory.

### Option 1 — use the public PickOrange LeRobot dataset

Skip the convert stage and reuse the pre-converted dataset that Lightwheel
AI ships with LeIsaac:

```bash
pip install -U huggingface_hub
hf download LightwheelAI/leisaac-pick-orange \
    --repo-type dataset --local-dir /tmp/leisaac-pick-orange
physai upload datasets /tmp/leisaac-pick-orange/

physai build -n examples/so101-gr00t/containers/leisaac-runtime
physai build -n examples/so101-gr00t/containers/leisaac-gr00t-n1.6
physai build -n examples/so101-gr00t/containers/gr00t-n1.6-trainer

physai run --from train --config examples/so101-gr00t/configs/so101_pickorange_gr00t-n1.6.yaml \
           --dataset leisaac-pick-orange
```

`--from train` skips the `convert` stage (the dataset is already in LeRobot
format). The run proceeds through train → eval and streams logs; Ctrl-C
detaches without cancelling the jobs.

### Option 2 — convert your own HDF5 demos

Start from raw Isaac Lab / LeIsaac HDF5 recordings, let the pipeline
convert them, then train and eval:

```bash
# Upload your HDF5 directory to S3 (preferred — auto-imports via FSx DRA):
aws s3 cp --recursive /path/to/my-demos/ s3://<data-bucket>/raw/my-demos/

# Build all four containers you'll need:
physai build -n examples/so101-gr00t/containers/so101-converter
physai build -n examples/so101-gr00t/containers/leisaac-runtime
physai build -n examples/so101-gr00t/containers/leisaac-gr00t-n1.6
physai build -n examples/so101-gr00t/containers/gr00t-n1.6-trainer

# Full pipeline: convert → train → eval.
physai run --config examples/so101-gr00t/configs/so101_liftcube_gr00t-n1.6.yaml \
           --raw my-demos
```

The converter names its output dataset after the raw directory
(`/fsx/datasets/my-demos/`). Override with `--dataset` if you want a
different name:

```bash
physai run --config ... --raw my-demos --dataset liftcube-baseline
```

### Running stages individually

```bash
# Convert only (shortcut for --from convert --to convert):
physai convert --config examples/so101-gr00t/configs/so101_liftcube_gr00t-n1.6.yaml \
               --raw my-demos [--dataset my-dataset]

# Train only:
physai train --config ... --dataset my-dataset [--max-steps 30000]

# Eval only:
physai eval --config ... --checkpoint <run-id> [--eval-rounds 50]
```

## Adapting this example

### New task, same robot

Add a new config under `configs/` modeled on an existing one and change:

- `sim.environment` and `sim.mimic_environment` — the LeIsaac gym IDs
- `sim.language_instruction` — the string passed to GR00T at eval
- `model.config_dir` — `so101-singlecam` or `so101-dualcam` depending on
  how many cameras the task uses

No container rebuild needed.

### New robot

Much more involved. You'll need:

- A new `hdf5_*.yaml` under `containers/so101-converter/app/robot_configs/`
  with the new robot's joint names, joint limits (degrees), and motor
  limits; adjust `convert.sh` to pick it.
- A new `model_configs/<gr00t-version>/<new-robot-layout>/` with
  `modality.json` + `data_config.py`/`modality_config.py` reflecting the
  new embodiment.
- LeIsaac-side work outside this repo: a USD asset, robot config, env
  registration, and policy client updates. See
  [docs/en/STATUS.md](../../docs/en/STATUS.md#adding-a-new-robot-eg-so-101-with-different-camera-placement)
  for a checklist.

### Different GR00T version

If NVIDIA ships an N1.7 (or similar):

1. Copy `containers/gr00t-n1.6-trainer/` to `gr00t-n1.7-trainer/` and
   update `GR00T_REF` / `--base-model-path`.
2. Copy `containers/leisaac-gr00t-n1.6/` to `leisaac-gr00t-n1.7/` and
   update `GR00T_REF`.
3. Copy `model_configs/gr00t-n1.6/` to `model_configs/gr00t-n1.7/` and
   update imports/API if N1.7 changes the modality-config API.
4. Write new run configs under `configs/` referencing the new containers
   and model configs.

## References

- GR00T N1.6 fine-tuning: <https://github.com/NVIDIA/Isaac-GR00T/blob/n1.6-release/getting_started/finetune_new_embodiment.md>
- GR00T data prep: <https://github.com/NVIDIA/Isaac-GR00T/blob/n1.6-release/getting_started/data_preparation.md>
- LeIsaac: <https://github.com/LightwheelAI/leisaac>
- LeRobot v2.1 dataset format: <https://huggingface.co/docs/lerobot/lerobot-dataset-v3>
- Public PickOrange dataset: <https://huggingface.co/datasets/LightwheelAI/leisaac-pick-orange>
