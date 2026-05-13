# Project Status

Current state of Phase 1: what's built, what's next. This file reflects
execution against the platform design in [PIPELINE-DESIGN.md](PIPELINE-DESIGN.md).

## Phase 1: LeIsaac + SO-101 + GR00T N1.6

Tasks: PickOrange and LiftCube. Two tasks demonstrate how the same containers and model config serve different tasks with only a new `run_config.yaml`.

### Containers

| Container | Base Image | Purpose |
|-----------|-----------|---------|
| `leisaac-runtime` | NGC PyTorch + IsaacSim (pip) | Base runtime (no GR00T) |
| `leisaac-gr00t-n1.6` | `leisaac-runtime` + Isaac-GR00T @ `n1.6-release` | Evaluation (policy server + LeIsaac client) |
| `so101-converter` | python:3.11-slim + h5py/pyarrow/ffmpeg | HDF5 → LeRobot conversion + validation |
| `gr00t-n1.6-trainer` | NGC PyTorch + Isaac-GR00T @ `n1.6-release` | GR00T N1.6 fine-tuning |

All LeIsaac tasks (PickOrange, LiftCube, etc.) are baked into the same `leisaac-runtime` base. The task is selected at runtime via `sim.environment` in the config — not at build time. `leisaac-gr00t-n1.6` layers on `leisaac-runtime` using `base_container:`, pinning the eval-time GR00T server to the N1.6 tag; follow-up work can add `leisaac-gr00t-n1.5` / `-n1.7` similarly without rebuilding the base.

Containers are built via the container build system (see PIPELINE-DESIGN.md §3.4) and stored as squashfs on `/fsx/enroot/`. Slurm jobs use them via Pyxis `--container-image`.

**IsaacSim-specific notes**:
- `leisaac-runtime` includes a `50-warmup.sh` setup hook that warms up IsaacSim shader caches during build (equivalent to [upstream warmup.sh](https://github.com/isaac-sim/IsaacSim/blob/main/source/scripts/warmup.sh)). Uses `kit_app.py` instead of the `kit` binary since the pip-installed IsaacSim only ships `kit-gcov` which requires the standalone distribution layout.
- Evaluation jobs need `DISPLAY=:0` and `/tmp/.X11-unix` mounted — IsaacSim requires GLFW/GLX even in headless mode. Xorg is installed on GPU nodes via lifecycle scripts (`install_xorg.sh`).
- `PYTHONUNBUFFERED=1` is required for `policy_inference.py` output to be captured through `tee`.

### run_config.yaml

Two configs — same containers, same model config, different task:

```yaml
# examples/so101-gr00t/configs/so101_pickorange_gr00t-n1.6.yaml
pipeline:
  stages: [train, eval]

sim:
  platform: leisaac
  environment: LeIsaac-SO101-PickOrange-v0
  mimic_environment: LeIsaac-SO101-PickOrange-Mimic-v0
  language_instruction: "Pick up the orange"

model:
  name: gr00t-n1.6
  config_dir: gr00t-n1.6/so101

stages:
  train:
    partition: gpu
    gres: "gpu:1"
    constraint: l40s
    container: gr00t-n1.6-trainer
    max_steps: 10000
  eval:
    partition: gpu
    gres: "gpu:1"
    container: leisaac-gr00t-n1.6
    rounds: 20
```

```yaml
# examples/so101-gr00t/configs/so101_liftcube_gr00t-n1.6.yaml
pipeline:
  stages: [train, eval]

sim:
  platform: leisaac
  environment: LeIsaac-SO101-LiftCube-v0
  mimic_environment: LeIsaac-SO101-LiftCube-Mimic-v0
  language_instruction: "Lift the red cube up"

model:
  name: gr00t-n1.6
  config_dir: gr00t-n1.6/so101

stages:
  train:
    partition: gpu
    gres: "gpu:1"
    constraint: l40s
    container: gr00t-n1.6-trainer
    max_steps: 10000
  eval:
    partition: gpu
    gres: "gpu:1"
    container: leisaac-gr00t-n1.6
    rounds: 20
```

Only `sim.environment`, `sim.mimic_environment`, and `sim.language_instruction` differ. Everything else — containers, model config, stage parameters — is identical.

### Model Config: GR00T N1.6 for SO-101

```
examples/so101-gr00t/model_configs/gr00t-n1.6/so101/
├── modality.json              # Joint group → index mapping
└── modality_config.py         # ModalityConfig: action representation, normalization, horizon
```

### HDF5 Input Format (LeIsaac)

LeIsaac environments for SO-101: PickOrange, LiftCube.

Phase 1 collection methods:
- **Leader arm**: Follower mirrors leader joint positions. `obs/joint_pos` records follower state.
- **Keyboard teleoperation**: IK deltas control the arm. `obs/joint_pos` records resulting joint positions.

```
data/
  demo_0/
    obs/
      joint_pos        (T, 6)  float32, radians
      joint_pos_target (T, 6)  float32, radians
      actions          (T, N)  float32 — IK deltas (keyboard) or joint pos (leader)
      front            (T, 480, 640, 3) uint8
      wrist            (T, 480, 640, 3) uint8
      ee_frame_state   (T, 7)  float32
    initial_state      dict — full scene state for Mimic reset
    states             (T, ...) — articulation + rigid object states for Mimic
```

> **Critical**: `obs/actions` has different dimensions and semantics depending on the teleop device. The converter uses `obs/joint_pos` (not `obs/actions`) as both observation.state and action.

> **Risk**: Different teleop devices may produce different value ranges in `obs/joint_pos` (e.g., gripper values). The converter may need per-device adjustments. This needs to be validated during implementation by comparing HDF5 outputs from leader arm vs keyboard for the same task.

### Augmentation: Isaac Lab Mimic (Stretch Goal)

Augmentation is implemented last. If time is tight or progress is not smooth, it can be skipped — the pipeline works without it (augmentation is optional).

`/app/augment.sh` in `leisaac-runtime` runs a 4-step Mimic pipeline:

1. **eef_action_process.py --to_ik**: Convert recorded actions → absolute EEF poses (device-independent)
2. **annotate_demos.py**: Annotate subtask boundaries using the Mimic environment variant
3. **generate_dataset.py**: Generate augmented demos via pose perturbation + replay
4. **eef_action_process.py --to_joint**: Convert IK actions back → joint actions

Mimic requires `initial_state` and `states` in the seed HDF5 (recorded during demo collection). A LeRobot dataset cannot be used for Mimic — these fields are lost during conversion.

**Known issues**: `annotate_demos.py` has bugs with IsaacLab v2.3.0 (`Se3Keyboard` API change, `torch.any()` type error). Requires patch.

### Conversion: SO-101 HDF5 → LeRobot v2.1

Two paths:

**Option A: LeIsaac's `isaaclab2lerobot.py`** (requires Isaac Sim + GPU)
```bash
python scripts/convert/isaaclab2lerobot.py \
  --task_name=LeIsaac-SO101-PickOrange-v0 \
  --repo_id=local/so101_pickorange \
  --hdf5_root=./datasets --hdf5_files=demos.hdf5 --headless
```

**Option B: Standalone `hdf5_to_lerobot.py`** (CPU only, minutes not hours)
```bash
python hdf5_to_lerobot.py \
  --hdf5_file ${INPUT_HDF5} \
  --output_dir /fsx/datasets/${RUN_ID}
```
Hardcodes SO-101 joint limits, camera names (`front`, `wrist`), and modality.json. Input is either from `/fsx/raw/` (no augmentation) or local NVMe (with augmentation).

**Post-conversion**: `/app/convert.sh` should also re-encode AV1 → H.264 if needed (GR00T's decord loader doesn't support AV1).

### Validation: GR00T N1.6

Checks: 6D action + 6D state + front/wrist cameras + `modality.json` present.

### Training: GR00T N1.6

```bash
physai train --config examples/so101-gr00t/configs/so101_liftcube_gr00t-n1.6.yaml \
  --dataset so101_liftcube
```

`max_steps` comes from `stages.train.max_steps` in the config (default: 10000). Override with `--max-steps` on the CLI.

This submits a Slurm job that runs inside the `gr00t-n1.6-trainer` container:

```bash
bash /app/train.sh /fsx/datasets/so101_liftcube \
  /fsx/physai/sync/<run-id>/model_config \
  /fsx/checkpoints/<run-id> \
  10000
```

Internally, `/app/train.sh` runs `gr00t/data/stats.py` (normalization stats) then `launch_finetune.py` with `--embodiment-tag NEW_EMBODIMENT`.

### Evaluation: GR00T + LeIsaac

```bash
physai eval --config examples/so101-gr00t/configs/so101_liftcube_gr00t-n1.6.yaml \
  --checkpoint gr00t-n1.6-liftcube-30k
```

`rounds` comes from `stages.eval.rounds` in the config (default: 20). Override with `--eval-rounds` on the CLI.

This submits a Slurm job that runs inside the `leisaac-runtime` container:

```bash
bash /app/eval.sh /fsx/checkpoints/gr00t-n1.6-liftcube-30k \
  /fsx/physai/sync/<run-id>/model_config \
  /fsx/evaluations/<run-id> \
  20
```

Internally, `/app/eval.sh` starts the GR00T policy server (`run_gr00t_server.py`) and runs LeIsaac's `policy_inference.py` with `--policy_type=gr00tn1.6`. When `--visual` is passed (via `physai eval --visual`), it omits `--headless` so Isaac Sim renders to the DCV session; otherwise it passes `--headless` for batch evaluation. `DISPLAY` must always be set — IsaacSim requires GLFW/GLX even in headless mode.

### Extending

#### Adding a New Robot (e.g., SO-101 with different camera placement)

Changes in LeIsaac repo:

| What | Where (in LeIsaac repo) | Example |
|------|------------------------|---------|
| USD asset file | `assets/robots/so101_topcam.usd` | New USD with camera mounted on top instead of wrist |
| Robot config | `source/leisaac/leisaac/assets/robots/lerobot.py` | New `SO101_TOPCAM_CFG` (`ArticulationCfg` defining joint properties, actuators, USD path) |
| Joint + motor limits | `source/leisaac/leisaac/utils/constant.py` | New limit arrays (only if limits differ from SO-101) |
| Coordinate conversion | `source/leisaac/leisaac/utils/robot_utils.py` | `convert_leisaac_action_to_lerobot()` hardcodes SO-101 limits — must be parameterized |
| Camera config | `source/leisaac/leisaac/tasks/template/single_arm_env_cfg.py` → `SingleArmTaskSceneCfg` | Change `TiledCameraCfg`: rename `wrist` → `top`, update `prim_path` and `offset` |
| Policy client | `source/leisaac/leisaac/policy/service_policy_clients.py` | Update modality key mapping if camera names change |

Changes outside LeIsaac:

| What | Where | Example |
|------|-------|---------|
| Conversion script | `hdf5_to_lerobot.py` | Update hardcoded joint limits and camera names |
| Model config | `examples/.../model_configs/gr00t-n1.6/so101_topcam/` | New `modality.json` with updated video key mapping |
| Container rebuild | `leisaac-runtime` + `so101-converter` | Rebuild with updated code |

**Key coupling**: `robot_utils.py` and `hdf5_to_lerobot.py` both hardcode SO-101 joint limits. Both need updating for a new robot.

#### Adding a New Task (e.g., StackBlocks)

Changes in LeIsaac repo:

| What | Where (in LeIsaac repo) | Example |
|------|------------------------|---------|
| USD scene | `assets/scenes/table_with_blocks/scene.usd` | 3D scene with physics properties |
| Scene config | `source/leisaac/leisaac/assets/scenes/table_with_blocks.py` | Object spawn positions, physics materials |
| Environment config | `source/leisaac/leisaac/tasks/stack_blocks/env_cfg.py` | Inherits `SingleArmTaskEnvCfg` |
| Success condition | `source/leisaac/leisaac/tasks/stack_blocks/mdp/terminations.py` | e.g., block Z > threshold |
| Subtask signals | `source/leisaac/leisaac/tasks/stack_blocks/mdp/observations.py` | Required for Mimic augmentation |
| Gym registration | `source/leisaac/leisaac/tasks/stack_blocks/__init__.py` | `gym.register(id="LeIsaac-SO101-StackBlocks-v0", ...)` |
| Mimic variant | `source/leisaac/leisaac/tasks/stack_blocks/mimic_env_cfg.py` | Pose randomization ranges, subtask definitions |

Changes outside LeIsaac:

| What | Where |
|------|-------|
| Pipeline config | New `run_config.yaml` with `environment: LeIsaac-SO101-StackBlocks-v0` |
| Container rebuild | `leisaac-runtime` with new task code |

Model config directory is reused since the robot is unchanged.

#### Ownership

| Responsibility | Owner |
|---------------|-------|
| HDF5 demo format | LeIsaac |
| HDF5 → LeRobot conversion | Shared (LeIsaac's `isaaclab2lerobot.py` or our `hdf5_to_lerobot.py`) |
| Data augmentation (Mimic) | LeIsaac |
| Model training | Model repo (Isaac-GR00T, OpenPI) |
| Evaluation | LeIsaac (env + policy clients) + Model repo (policy server) |
| Orchestration + tracking | This pipeline |
| Container builds | This pipeline |

---

## Implementation Status

### Implemented

#### Infrastructure

- Two CDK stacks: `PhysaiInfraStack` (VPC, S3 data bucket, FSx for Lustre with DRA, RDS MariaDB, Secrets Manager) and `PhysaiClusterStack` (HyperPod cluster, IAM, lifecycle-scripts bucket)
- Unique HyperPod `ClusterName` per deploy (suffixed with the CloudFormation stack GUID) so each fresh deployment has its own identity in `sacct`
- Lifecycle scripts: FSx mount, Slurm daemons, Docker, Enroot + Pyxis, cgroup tracking, node feature mapping, Slurm accounting (slurmdbd → RDS)
- Content-hashed S3 prefix for lifecycle scripts so CloudFormation issues `UpdateCluster` when scripts change
- Helper scripts: `setup-ssh.sh`, `cleanup.sh`, `cleanup-failed-stacks.sh`

#### CLI (`physai`)

- Commands: `build`, `run`, `train`, `eval`, `ls`, `upload`, `list`, `status`, `logs`, `cancel`, `clean`, `doctor`
- SSH session multiplexing via `ControlMaster`, Ctrl-C detach/reconnect
- `--from`/`--to` stage selection with a Stage registry
- Model-config directory resolution via configurable search paths
- Graceful SSH error messages (host key mismatch, auth failure, etc.)

#### Container build system

- `project.yaml` + `container.yaml` schema with setup-hooks/app layout
- `base_image` for registry images; `base_container` to layer one built container on another
- `physai build` syncs the container folder to the cluster, generates an sbatch that runs each hook via Pyxis, copies `app/` to `/app/`, and exports a squashfs to `/fsx/enroot/<name>.sqsh`

#### Pipeline stages

- `train`, `eval` — implemented and working end-to-end with the public Pick Orange dataset

#### Observability

- Slurm accounting (`sacct`) via RDS MariaDB
- `physai logs <job-id>` streams with Ctrl-C detach

### Not yet implemented

#### Next up (blocking full pipeline)

- `convert` stage — `so101-converter` container (HDF5 → LeRobot v2.1)
- `validate` stage — dataset structural + GR00T-specific checks
- `register` stage — publish checkpoint + metrics to S3, log to MLflow
- `train.sh` output contract — define `train_summary.json` (final loss, steps, checkpoint paths) so `register` can consume training outputs; currently `train.sh` only writes model checkpoints
- Export stage outputs to S3 — datasets, checkpoints, evaluations under `/fsx/` should be published to `s3://<data-bucket>/{datasets,checkpoints,results}/` at the end of each stage. The pipeline orchestrator performs the export explicitly (e.g., `aws s3 cp`); it is NOT done via FSx data-repository export. `/fsx/` is treated as working storage only.
- MLflow tracking server in CDK

#### Planned

- DCV visual evaluation (`physai eval --visual`)
- Examples for GR00T N1.5 (follow-up after the initial PR)

#### Stretch

- Augmentation stage — Isaac Lab Mimic via `/app/augment.sh` in `leisaac-runtime`
- π0 / OpenPI model support
- Additional robots (Panda, Unitree G1, bimanual arms)
- Non-LeIsaac simulation environments
- Multi-GPU training
