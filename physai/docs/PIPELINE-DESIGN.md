---
title: "Physical AI Pipeline Platform"
subtitle: "Design Document"
toc: true
toc-depth: 2
highlight-style: tango
colorlinks: true
geometry: margin=1in
code-block-font-size: \scriptsize
listings: true
listings-no-page-break: true
# Build: pandoc PIPELINE-DESIGN.md -o PIPELINE-DESIGN.pdf --pdf-engine=lualatex --template=eisvogel
---

A cloud-native platform on AWS for robot developers to go from collected demos to evaluated policies, without managing infrastructure.

## 1. Overview

The platform automates the full robot learning workflow:

```
Raw HDF5 demos (uploaded to S3 → appears on /fsx)
  │
  ├─ 1. Augmentation (optional, GPU)
  │     Generate more demos from seed demos via simulation perturbations
  │
  ├─ 2. Format Conversion (CPU)
  │     HDF5 → LeRobot v2.1 (parquet + H.264 video)
  │
  ├─ 3. Validation (CPU)
  │     Pre-flight checks: structure, dimensions, model compatibility
  │
  ├─ 4. Training (GPU)
  │     Fine-tune VLA model on validated LeRobot dataset
  │
  ├─ 5. Evaluation (GPU)
  │     Run policy in simulation, measure success rate
  │
  └─ 6. Registration (CPU)
        Record metrics, version checkpoint, conditional approval
```

Three subsystems are independently extensible:

| Subsystem | What varies |
|-----------|------------|
| Augmentation + Evaluation | Robot, simulation environment, task |
| Training | VLA model, model config |
| Conversion + Validation | Robot, source data format |

## 2. Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│  Developer Machine                                               │
│    └── physai CLI (orchestrates via SSH)                         │
└──────────────────────────────────────────────────────────────────┘
         │ SSH
         ▼
┌──────────────────────────────────────────────────────────────────┐
│                    SageMaker HyperPod Cluster                    │
│                                                                  │
│  Login Node (ml.c5.large)                                        │
│    ├── SSH entry point for developers                            │
│    └── MLflow client (experiment logging)                        │
│                                                                  │
│  Controller Node (ml.c5.large)                                   │
│    └── Slurm scheduler                                           │
│                                                                  │
│  Worker Partition: "gpu" (auto-scaling 0 → N)                    │
│    → Augmentation, Training, Evaluation                          │
│                                                                  │
│  Worker Partition: "cpu" (auto-scaling 0 → N)                    │
│    → Format conversion, validation, registration                 │
│                                                                  │
│  All nodes mount: /fsx (FSx for Lustre)                          │
└──────────────────────────────────────────────────────────────────┘
         │
         │
         ▼
┌──────────────────────┐   ┌──────────────────────┐
│  S3 (permanent)      │   │  ECR                 │
│  ├── raw/            │   │  (container images)  │
│  ├── datasets/       │   └──────────────────────┘
│  ├── checkpoints/    │
│  └── results/        │   ┌──────────────────────┐
│                      │   │  SageMaker MLflow    │
│  FSx (working)       │   │  (Tracking Server)   │
│  /fsx/               │   └──────────────────────┘
│  ├── raw/  ←DRA──S3  │
│  ├── datasets/       │
│  ├── checkpoints/    │
│  ├── evaluations/    │
│  ├── enroot/         │
│  └── physai/         │
└──────────────────────┘
```

**S3** is the permanent store for all inputs and outputs. **FSx for Lustre** is working storage — fast, shared across all nodes, but temporary.

- `/fsx/raw/` has a Data Repository Association (auto-import only) linked to `s3://bucket/raw/`. Users upload HDF5 to S3; it appears on `/fsx/raw/` via lazy-load on first access.
- All other `/fsx/` directories have no S3 link. The orchestrator stages data from S3 to `/fsx` when needed (e.g., pulling a previously published dataset for retraining).
- The registration stage publishes final results (datasets, checkpoints, metrics) from `/fsx` to S3 via explicit `aws s3 cp`.
- After registration, working data on `/fsx` can be cleaned up. Raw HDF5 is deleted from `/fsx/raw/` after conversion; if the user needs it again, they trigger an explicit re-import from S3.

## 3. Configuration

### 3.1 run_config.yaml

One config per pipeline variant (robot + task + model), stored in the project repo alongside container definitions.

```yaml
sim:
  platform: <sim_platform>           # e.g., leisaac, robosuite
  # Remaining fields are platform-specific.
  # The pipeline does not interpret them — they are read by the sim runtime container.
  # Example (LeIsaac):
  #   environment: LeIsaac-SO101-PickOrange-v0
  #   mimic_environment: LeIsaac-SO101-PickOrange-Mimic-v0

model:
  name: <model_name>
  config_dir: <relative-name>         # e.g., gr00t-n1.6/so101-singlecam

resources:
  augment:   { partition: gpu, gres: "gpu:1", container: <sim_runtime_container> }
  convert:   { partition: cpu, container: <converter_container> }
  validate:  { partition: cpu, container: <converter_container> }
  train:     { partition: gpu, gres: "gpu:1", container: <trainer_container> }
  eval:      { partition: gpu, gres: "gpu:1", container: <sim_runtime_container> }
  register:  { partition: cpu }
```

The `physai` CLI rsyncs the config and resolves `model.config_dir` via configured search paths (see physai-design.md §3). Datasets and checkpoints are referenced by name and resolved to `/fsx/` paths.

### 3.2 Container Protocol

Each container implements fixed entrypoint scripts. The pipeline calls these and nothing else. The config is passed via `RUN_CONFIG` environment variable.

**Sim runtime container** (augmentation + evaluation):

| Entrypoint | Arguments | Contract |
|-----------|-----------|----------|
| `/app/eval.sh` | `<checkpoint_dir> <model_config_dir> <output_dir> <eval_rounds> [--visual]` | Run trained policy in simulation, write `metrics.json` to output dir. Pass `--visual` to render to display (DCV); otherwise run headless. |
| `/app/augment.sh` (optional) | `<input_hdf5> <output_dir> <num_trials>` | Produce augmented HDF5 in same format with more episodes. |

**Converter container** (conversion + validation):

| Entrypoint | Arguments | Contract |
|-----------|-----------|----------|
| `/app/convert.sh` | `<input_hdf5> <output_dir>` | Convert source format → LeRobot v2.1. |
| `/app/validate.sh` | `<dataset_dir> <model_config_dir>` | Validate dataset against model requirements. Exit non-zero on failure. |

**Trainer container** (model training):

| Entrypoint | Arguments | Contract |
|-----------|-----------|----------|
| `/app/train.sh` | `<dataset_dir> <model_config_dir> <output_dir> <max_steps>` | Run training. Model-specific preprocessing (e.g., normalization stats) is handled internally. |

All containers read `sim.*` and `model.*` from `$RUN_CONFIG` when they need environment or model-specific information.

### 3.3 Model Config Directory

Per-model, per-robot config files stored locally. The `model.config_dir` in `run_config.yaml` is a relative name (e.g., `gr00t-n1.6/so101`). The CLI resolves it against configured search paths and rsyncs the matched directory to the cluster. The pipeline passes the directory to containers — it does not interpret the contents.

```
<model_config_path>/
└── <model>/<robot>/
    └── <model-specific config files>
```

### 3.4 Container Build System

Containers are built on the cluster using Pyxis/enroot named containers (not Docker) and exported as squashfs images to `/fsx/enroot/`. Each container is defined by a folder containing setup hooks, application files, and a `container.yaml` manifest. Shared configuration across containers in the same project is defined in `project.yaml`.

#### Project layout

```
examples/so101-gr00t/
├── project.yaml                      # Shared config for all containers
└── containers/
    ├── leisaac-runtime/
    │   ├── container.yaml
    │   ├── app/                      # Copied to /app/ in the container
    │   │   └── eval.sh
    │   └── setup-hooks/              # Run in order during build
    │       ├── 10-system-packages.root.sh
    │       ├── 20-install-leisaac.sh
    │       └── ...
    └── gr00t-trainer/
        ├── container.yaml
        ├── app/
        │   └── train.sh
        └── setup-hooks/
            └── ...
```

#### project.yaml

Shared defaults for all containers in a project. Discovered by walking up from the container folder.

```yaml
base_image: nvcr.io/nvidia/pytorch:25.04-py3
env:
  OMNI_KIT_ACCEPT_EULA: "YES"
  PIP_CONSTRAINT: ""
  NVIDIA_VISIBLE_DEVICES: all
  NVIDIA_DRIVER_CAPABILITIES: all
  LEISAAC_DIR: /workspace/leisaac
  GR00T_DIR: /workspace/gr00t
  GR00T_REF: "77866395d6ab601a770f95cf78cf51d5847f6fa1"
  LEISAAC_REF: "d2cbfd2e33517f2094e1904ff817aa17de6e8939"
```

#### container.yaml

Per-container config. Same schema as `project.yaml` with additional container-specific fields. Values override `project.yaml` on merge.

```yaml
name: leisaac-runtime
partition: gpu
gres: "gpu:1"
env:
  PIP_CONSTRAINT: ""
```

#### Setup hooks

Scripts in `setup-hooks/` run in numeric order during the build:

- `NN-name.sh` — runs as user
- `NN-name.root.sh` — runs as root (`--container-remap-root`)

All hooks receive merged `env` vars via `/etc/environment`. The first hook creates the container from `base_image`; subsequent hooks operate on the existing named container.

#### Application files

Everything under `app/` is copied to `/app/` in the container after all setup hooks complete. These are the entrypoint scripts defined by the container protocol (§3.2).

#### Build process

1. Sync container folder and `project.yaml` to the cluster
2. Submit a Slurm job on the configured `partition` with `gres`:
   a. Run each setup hook in order via `srun --container-name=<name>` (first hook includes `--container-image=<base>` to create the container; root hooks add `--container-remap-root`)
   b. Copy `app/` contents to `/app/` in the container
   c. Export to squashfs: `enroot export -o /fsx/enroot/<name>.sqsh`

## 4. Data Augmentation

**Input**: Seed HDF5 demos + task environment → **Output**: Augmented HDF5

The augmentation stage is optional. It runs inside the sim runtime container via `/app/augment.sh`. The output HDF5 must have the identical schema as the input so the converter can process both uniformly.

When augmentation is enabled, the orchestrator runs augmentation and conversion as a single Slurm job on the same GPU node. The augmented HDF5 is written to the node's local NVMe storage (not `/fsx`), then conversion reads from local NVMe and writes the LeRobot dataset to `/fsx`. The augmented HDF5 — which can be 600GB+ — never touches shared storage and is automatically cleaned up when the job ends.

## 5. Training Subsystem

### 5.1 Dataset Conversion

**Input**: HDF5 on `/fsx` → **Output**: LeRobot v2.1 dataset on `/fsx/datasets/`

The converter container's `/app/convert.sh` handles all conversion logic. The pipeline does not implement or parameterize conversion — it's baked into the container.

LeRobot v2.1 target format:
```
dataset/
├── data/chunk-000/
│   └── episode_000000.parquet    # action, observation.state, + 5 index columns
├── videos/chunk-000/
│   └── observation.images.<cam>/
│       └── episode_000000.mp4    # H.264, yuv420p
└── meta/
    ├── info.json                 # Feature schema
    ├── episodes.jsonl            # Per-episode metadata
    ├── tasks.jsonl               # Task descriptions
    └── episodes_stats.jsonl      # Per-episode normalization stats
```

Mandatory parquet columns:

| Column | Type | Description |
|--------|------|-------------|
| `index` | int64 | Globally unique, sequential across entire dataset |
| `episode_index` | int64 | Episode identifier |
| `frame_index` | int64 | Frame within episode (resets to 0) |
| `timestamp` | float32 | `frame_index / fps` |
| `task_index` | int64 | References task in tasks.jsonl |

### 5.2 Dataset Validation

**Input**: LeRobot v2.1 dataset + model config dir → **Output**: Pass/fail. Blocks training on failure.

The converter container's `/app/validate.sh` checks structural validity and model-specific requirements. Validation runs as a Slurm job between conversion and training.

### 5.3 Model Training

**Input**: Validated LeRobot v2.1 dataset + model config dir → **Output**: Checkpoint on `/fsx/checkpoints/`

The trainer container's `/app/train.sh` handles all training logic. All supported models consume LeRobot v2 with absolute joint positions — models convert to their preferred internal representation during training.

## 6. Evaluation Subsystem

**Input**: Checkpoint + task environment → **Output**: `metrics.json` on `/fsx/evaluations/`, published to S3 by registration stage

The sim runtime container's `/app/eval.sh` runs the trained policy in simulation. Architecture: policy server (model-specific) + simulation client (environment-specific) in the same Slurm job.

### 6.1 Visual Evaluation via DCV

For human review and demos, `physai eval --visual` streams a rendered simulation viewport to the developer's browser via NICE DCV.

```bash
$ physai eval --visual --config so101_pickorange_gr00t.yaml \
    --checkpoint checkpoints/run-42/checkpoint-10000

Submitted job 456
Allocating GPU node...          gpu-worker-3 (i-0abc123def)
Starting DCV session...         physai-eval-456

Connect to the DCV session:
  aws ssm start-session --target i-0abc123def \
    --document-name AWS-StartPortForwardingSession \
    --parameters '{"portNumber":["8443"],"localPortNumber":["8443"]}'

Then open: https://localhost:8443
Username: ubuntu          Password: a3f8k2x9

Streaming eval log (Ctrl-C to detach)...
```

What the pipeline does:
1. Submits a Slurm job with `--gres=gpu:1,dcv:1` to allocate a GPU node with DCV (custom GRES ensures one DCV session per node)
2. Creates a DCV session on the allocated node
3. Prints the SSM port-forwarding command and DCV URL
4. Runs `/app/eval.sh` inside the container with `--visual` — the container renders to the DCV session instead of running headless
5. Streams the eval log to the local terminal
6. When evaluation finishes (or user cancels via `physai cancel`), the DCV session is cleaned up

DCV server is installed on GPU worker nodes via HyperPod lifecycle scripts. SSM port forwarding requires no security group changes — the tunnel only exists while the SSM CLI session is running.

## 7. Pipeline Orchestration

### 7.1 Slurm Job Chain

```bash
# Without augmentation:
JOB1=$(sbatch --parsable convert.sh)
JOB2=$(sbatch --parsable --dependency=afterok:$JOB1 validate.sh)
JOB3=$(sbatch --parsable --dependency=afterok:$JOB2 train.sh)
JOB4=$(sbatch --parsable --dependency=afterok:$JOB3 eval.sh)
JOB5=$(sbatch --parsable --dependency=afterok:$JOB4 register.sh)

# With augmentation (augment + convert run as single GPU job, using local NVMe for intermediate data):
JOB1=$(sbatch --parsable augment_and_convert.sh)
JOB2=$(sbatch --parsable --dependency=afterok:$JOB1 validate.sh)
JOB3=$(sbatch --parsable --dependency=afterok:$JOB2 train.sh)
JOB4=$(sbatch --parsable --dependency=afterok:$JOB3 eval.sh)
JOB5=$(sbatch --parsable --dependency=afterok:$JOB4 register.sh)
```

All jobs share a run ID via `--job-name=run-<id>/<stage>`. If any step fails, downstream jobs are automatically cancelled.

### 7.2 physai CLI

The `physai` CLI runs locally and orchestrates work on the cluster via SSH (similar to Ansible). Every subcommand that triggers a workload submits a Slurm job, streams its output to the local terminal by default, and survives Ctrl-C — if the user detaches, the remote job keeps running and can be reconnected via `physai logs`.

```bash
# Container builds (see §3.4 for project/container layout)
physai build <path-to-container-folder>           # build, stream log
physai build <path-to-container-folder> --rebuild  # remove existing sqsh first

# Pipeline runs (--config is a local file, --dataset/--checkpoint are names on /fsx)
physai run --config <local-yaml> --dataset <name> [--augment] --max-steps <N>
physai train --config <local-yaml> --dataset <name> --max-steps <N>
physai eval --config <local-yaml> --checkpoint <name> [--visual]

# Data management
physai ls <category> [<path>]         # list remote data (raw, datasets, checkpoints, enroot)
physai upload <category> <local-path> # upload data to cluster

# Job management (works for any job type: build, run, train, eval)
physai list                    # all jobs
physai status <job-id>
physai logs <job-id>           # tail/reconnect to log
physai cancel <job-id>
```

All subcommands connect to the cluster via SSH. The host can be specified per-command with `--host` or defaulted in `~/.physai/config.yaml`:

```yaml
host: physai-login            # SSH host alias (default)
```

### 7.3 Experiment Tracking (MLflow)

Each completed run logs to SageMaker MLflow:

| Category | What's logged |
|----------|--------------|
| Parameters | model, dataset, max_steps, batch_size, augmentation config |
| Metrics | Training loss (per step), eval success rate |
| Artifacts | Checkpoint path (S3), evaluation videos (S3), run_config.yaml |
| Tags | Run ID, model type, task name, robot |

## 8. Infrastructure

### 8.1 HyperPod Cluster

| Node | Instance | Role |
|------|----------|------|
| Login | ml.c5.large | SSH entry, MLflow client |
| Controller | ml.c5.large | Slurm scheduler |
| GPU workers | ml.g6e.2xlarge (1× L40S 48GB) | Augmentation, training, evaluation |
| CPU workers | ml.m5.2xlarge | Conversion, validation, registration |

GPU and CPU partitions auto-scale from 0 to N workers.

**Applying system-level changes to running nodes**:
1. Update lifecycle scripts in S3
2. Drain target nodes: `scontrol update node=X state=drain` (finishes running jobs, accepts no new ones)
3. Once drained, replace: `scontrol update node=X state=fail reason="Action:Replace"` (HyperPod provisions fresh instances with updated lifecycle scripts)

Note: controller nodes cannot be replaced this way — only worker and login nodes. `UpdateClusterSoftware` only re-provisions when the AMI changes; it cannot be used to force lifecycle script re-execution on an existing AMI. For controller-only config changes (e.g., Slurm settings), apply manually via SSM.

### 8.2 Storage

Two-tier model: S3 is the permanent store, FSx is fast working storage.

**S3 (permanent)**: All pipeline inputs and outputs are durably stored here.

```
s3://<bucket>/
├── raw/                    # HDF5 demos uploaded by users
├── datasets/               # Published LeRobot v2.1 datasets
├── checkpoints/            # Published model checkpoints
└── results/                # Published evaluation metrics and videos
```

**FSx for Lustre (working)**: Shared by all cluster nodes at GB/s throughput. Temporary — cleaned up after each run.

```
/fsx/
├── raw/                    # DRA auto-import from S3 (read-only link)
├── datasets/               # Converted LeRobot datasets (staged from S3 or written by converter)
├── checkpoints/            # Training checkpoints (published to S3 by registration)
├── evaluations/            # Eval logs and metrics (published to S3 by registration)
├── enroot/                 # Container squashfs images
└── physai/                 # CLI working state
    ├── logs/               # Job logs: <job-id>.out
    ├── builds/             # Build working dirs
    └── sync/               # rsynced configs and model configs
```

**Local NVMe** (`/opt/dlami/nvme`): Fast local storage on GPU worker nodes. Used for temporary augmented HDF5 (600GB+) that never touches `/fsx`.

Data flow:
1. User uploads raw HDF5 to `s3://bucket/raw/` → auto-imported to `/fsx/raw/` (lazy-load on first access)
2. Pipeline stages read/write on `/fsx` at Lustre speed
3. Registration stage publishes final results to S3 via explicit `aws s3 cp`
4. Raw HDF5 deleted from `/fsx/raw/` after conversion. User can re-import from S3 if needed.
5. For retraining from a published dataset, `physai train` stages it from S3 to `/fsx/datasets/`

Storage budget per run on `/fsx`:

| Data | Size | Lifecycle |
|------|------|-----------|
| Raw HDF5 (100 episodes, dual camera) | ~600GB | Deleted after conversion |
| LeRobot dataset (H.264 compressed) | ~5-10GB | Deleted after published to S3 |
| Checkpoints (3B model, 3 saves) | ~10-15GB | Deleted after published to S3 |
| Eval logs + metrics | ~1GB | Deleted after published to S3 |
| Container squashfs images | ~40GB | Persistent on `/fsx` |

FSx starts at 1.2TB. Supports live capacity increases in 2.4TB increments (no downtime, increase only). CloudWatch alarm on `FreeStorageCapacity` warns before it fills up.

### 8.3 CDK Stack

Provisions: HyperPod cluster, FSx for Lustre (1.2TB) with DRA to S3 for `/fsx/raw/` (auto-import only), S3 bucket (permanent storage), ECR repositories, SageMaker MLflow, IAM roles, CloudWatch alarm on FSx `FreeStorageCapacity`.

## 9. Cost Model

### Fixed (always-on)

| Component | Cost |
|-----------|------|
| Login + Controller nodes | ~$0.17/hr |
| FSx for Lustre (1.2TB) | ~$170/month |
| SageMaker MLflow server | ~$50/month |
| **Total idle** | **~$340/month** |

### Per-Run (auto-scaling workers)

| Step | Instance | Duration | Cost |
|------|----------|----------|------|
| Augmentation | ml.g6e.2xlarge | ~30 min | ~$0.50 |
| Conversion + Validation | ml.m5.2xlarge | ~10 min | ~$0.07 |
| Training (10k steps) | ml.g6e.2xlarge | ~3-4 hrs | ~$3.50 |
| Evaluation (20 rounds) | ml.g6e.2xlarge | ~30 min | ~$0.50 |
| **Total per run** | | | **~$4.57** |

At 5 runs/week: ~$430/month total.

## 10. POC: LeIsaac + SO-101 + GR00T N1.6

Tasks: PickOrange and LiftCube. Two tasks demonstrate how the same containers and model config serve different tasks with only a new `run_config.yaml`.

### 10.1 Containers

| Container | Base Image | Purpose |
|-----------|-----------|---------|
| `leisaac-runtime` | NGC PyTorch + IsaacSim (pip) | Augmentation (Mimic) + Evaluation |
| `so101-converter` | python:3.11-slim + h5py/pyarrow/ffmpeg | HDF5 → LeRobot conversion + validation |
| `gr00t-trainer` | NGC PyTorch + Isaac-GR00T | GR00T N1.6 fine-tuning |

All LeIsaac tasks (PickOrange, LiftCube, etc.) are baked into the same `leisaac-runtime` container. The task is selected at runtime via `sim.environment` in the config — not at build time.

Containers are built via the container build system (see §3.4) and stored as squashfs on `/fsx/enroot/`. Slurm jobs use them via Pyxis `--container-image`.

**IsaacSim-specific notes**:
- `leisaac-runtime` includes a `50-warmup.sh` setup hook that warms up IsaacSim shader caches during build (equivalent to [upstream warmup.sh](https://github.com/isaac-sim/IsaacSim/blob/main/source/scripts/warmup.sh)). Uses `kit_app.py` instead of the `kit` binary since the pip-installed IsaacSim only ships `kit-gcov` which requires the standalone distribution layout.
- Evaluation jobs need `DISPLAY=:0` and `/tmp/.X11-unix` mounted — IsaacSim requires GLFW/GLX even in headless mode. Xorg is installed on GPU nodes via lifecycle scripts (`install_xorg.sh`).
- `PYTHONUNBUFFERED=1` is required for `policy_inference.py` output to be captured through `tee`.

### 10.2 run_config.yaml

Two configs — same containers, same model config, different task:

```yaml
# examples/so101-gr00t/configs/so101_pickorange_gr00t.yaml
sim:
  platform: leisaac
  environment: LeIsaac-SO101-PickOrange-v0
  mimic_environment: LeIsaac-SO101-PickOrange-Mimic-v0

model:
  name: gr00t-n1.6
  config_dir: gr00t-n1.6/so101

resources:
  augment:   { partition: gpu, gres: "gpu:1", container: leisaac-runtime }
  convert:   { partition: cpu, container: so101-converter }
  validate:  { partition: cpu, container: so101-converter }
  train:     { partition: gpu, gres: "gpu:1", container: gr00t-trainer }
  eval:      { partition: gpu, gres: "gpu:1", container: leisaac-runtime }
  register:  { partition: cpu }
```

```yaml
# examples/so101-gr00t/configs/so101_liftcube_gr00t.yaml
sim:
  platform: leisaac
  environment: LeIsaac-SO101-LiftCube-v0
  mimic_environment: LeIsaac-SO101-LiftCube-Mimic-v0

model:
  name: gr00t-n1.6
  config_dir: gr00t-n1.6/so101

resources:
  augment:   { partition: gpu, gres: "gpu:1", container: leisaac-runtime }
  convert:   { partition: cpu, container: so101-converter }
  validate:  { partition: cpu, container: so101-converter }
  train:     { partition: gpu, gres: "gpu:1", container: gr00t-trainer }
  eval:      { partition: gpu, gres: "gpu:1", container: leisaac-runtime }
  register:  { partition: cpu }
```

Only `sim.environment` and `sim.mimic_environment` differ. Everything else — containers, model config, resources — is identical.

### 10.3 Model Config: GR00T N1.6 for SO-101

```
examples/so101-gr00t/model_configs/gr00t-n1.6/so101/
├── modality.json              # Joint group → index mapping
└── modality_config.py         # ModalityConfig: action representation, normalization, horizon
```

### 10.4 HDF5 Input Format (LeIsaac)

LeIsaac environments for SO-101: PickOrange, LiftCube.

POC collection methods:
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

### 10.5 Augmentation: Isaac Lab Mimic (Stretch Goal)

Augmentation is implemented last. If time is tight or progress is not smooth, it can be skipped — the pipeline works without it (augmentation is optional).

`/app/augment.sh` in `leisaac-runtime` runs a 4-step Mimic pipeline:

1. **eef_action_process.py --to_ik**: Convert recorded actions → absolute EEF poses (device-independent)
2. **annotate_demos.py**: Annotate subtask boundaries using the Mimic environment variant
3. **generate_dataset.py**: Generate augmented demos via pose perturbation + replay
4. **eef_action_process.py --to_joint**: Convert IK actions back → joint actions

Mimic requires `initial_state` and `states` in the seed HDF5 (recorded during demo collection). A LeRobot dataset cannot be used for Mimic — these fields are lost during conversion.

**Known issues**: `annotate_demos.py` has bugs with IsaacLab v2.3.0 (`Se3Keyboard` API change, `torch.any()` type error). Requires patch.

### 10.6 Conversion: SO-101 HDF5 → LeRobot v2.1

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

### 10.7 Validation: GR00T N1.6

Checks: 6D action + 6D state + front/wrist cameras + `modality.json` present.

### 10.8 Training: GR00T N1.6

```bash
physai train --config examples/so101-gr00t/configs/so101_liftcube_gr00t.yaml \
  --dataset so101_liftcube --max-steps 10000
```

This submits a Slurm job that runs inside the `gr00t-trainer` container:

```bash
bash /app/train.sh /fsx/datasets/so101_liftcube \
  /fsx/model_configs/gr00t-n1.6/so101 \
  /fsx/checkpoints/<run-id> \
  10000
```

Internally, `/app/train.sh` runs `gr00t/data/stats.py` (normalization stats) then `launch_finetune.py` with `--embodiment-tag NEW_EMBODIMENT`.

### 10.9 Evaluation: GR00T + LeIsaac

```bash
physai eval --config examples/so101-gr00t/configs/so101_liftcube_gr00t.yaml \
  --checkpoint gr00t-n1.6-liftcube-30k
```

This submits a Slurm job that runs inside the `leisaac-runtime` container:

```bash
bash /app/eval.sh /fsx/checkpoints/<run-id>/checkpoint-10000 \
  /fsx/model_configs/gr00t-n1.6/so101 \
  /fsx/evaluations/<run-id> \
  20
```

Internally, `/app/eval.sh` starts the GR00T policy server (`run_gr00t_server.py`) and runs LeIsaac's `policy_inference.py` with `--policy_type=gr00tn1.6`. When `--visual` is passed (via `physai eval --visual`), it omits `--headless` so Isaac Sim renders to the DCV session; otherwise it passes `--headless` for batch evaluation. `DISPLAY` must always be set — IsaacSim requires GLFW/GLX even in headless mode.

### 10.10 Extending

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

## 11. Implementation Plan

### Repo Structure

The repo contains three parts:

1. **Pipeline system** (`cli/`): The `physai` CLI, installed on the developer's local machine. Orchestrates builds and pipeline runs on the cluster via SSH. Contains no model/robot/sim-specific code.

2. **Infrastructure** (`infra/`): CDK/CloudFormation stack that deploys the physai platform (HyperPod, FSx, S3, ECR, MLflow), plus lifecycle scripts for cluster node provisioning.

3. **POC example** (`examples/so101-gr00t/`): LeIsaac + SO-101 + GR00T N1.6 integration. Contains `project.yaml`, container definitions (with `setup-hooks/` and `app/`), run configs, and model configs.

These are deployed separately. The infrastructure is deployed once. The CLI is installed locally. The POC example (or any future integration) is built and deployed via `physai build`.

### Phase 1: Infrastructure + Training (done)
- CDK stack: HyperPod cluster + FSx + S3 + ECR + MLflow
- Write `run_config.yaml` for SO-101 PickOrange + LiftCube with GR00T N1.6
- Build `gr00t-trainer` container
- Manual `sbatch` for GR00T training on pre-converted dataset
- Verify: checkpoint output on /fsx, published to S3

### Phase 2: Evaluation (done)
- Build `leisaac-runtime` container (Pyxis-based, with shader warmup)
- Headless evaluation Slurm job: GR00T policy server + LeIsaac sim client
- Xorg + Vulkan ICD setup via lifecycle scripts on GPU workers
- Registration script: metrics + checkpoint → MLflow + S3
- Verify: end-to-end from training to logged evaluation metrics
- DCV visual evaluation deferred to Phase 4

### Phase 3: Container Build System + Conversion + Validation
- `physai build` CLI command (local → SSH → sbatch)
- Container build system: `project.yaml`, `container.yaml`, `setup-hooks/`, `app/`
- Migrate `leisaac-runtime` and `gr00t-trainer` to new build system
- Build `so101-converter` container implementing container protocol
- GR00T model config directory with modality.json and modality_config.py
- Validation checks (structural + GR00T-specific)
- Manual `sbatch`: convert → validate → train chain
- Verify: LeRobot v2.1 dataset passes validation, GR00T trains successfully

### Phase 4: Full Pipeline + CLI
- `physai` CLI: run, train, eval, list, status, logs, cancel
- Full Slurm job chain with dependencies
- DCV visual evaluation via interactive Slurm job + DCV streaming
- End-to-end test: raw HDF5 → full pipeline → MLflow results for both PickOrange and LiftCube
- Documentation

### Phase 5 (Stretch): Augmentation
- Augmentation Slurm job (Isaac Lab Mimic) — `/app/augment.sh` in `leisaac-runtime`
- Add augmentation step to Slurm job chain
- End-to-end test with augmentation enabled

### Future (beyond POC)
- π0/OpenPI model support
- Additional robots (Panda, Unitree G1, bimanual arms)
- Non-LeIsaac simulation environments
- Real-robot demo collection integration
- Multi-GPU training
- Cost optimization: spot instances via HyperPod Flexible Training Plans

## 12. References

- [AWS Sample: Embodied AI Platform](https://github.com/aws-samples/sample-embodied-ai-platform)
- [AWS Sample: Physical AI Scaffolding Kit](https://github.com/aws-samples/sample-physical-ai-scaffolding-kit)
- [SageMaker HyperPod Documentation](https://docs.aws.amazon.com/sagemaker/latest/dg/sagemaker-hyperpod.html)
- [LeRobot Dataset Format](https://huggingface.co/docs/lerobot/lerobot-dataset-v3)
- [GR00T N1.6 Fine-tuning Guide](https://github.com/NVIDIA/Isaac-GR00T/blob/main/getting_started/finetune_new_embodiment.md)
- [GR00T Data Preparation](https://github.com/NVIDIA/Isaac-GR00T/blob/main/getting_started/data_preparation.md)
- [OpenPI (π0)](https://github.com/Physical-Intelligence/openpi)
- [LeIsaac](https://github.com/LightwheelAI/leisaac)
- [Isaac Lab Mimic](https://isaac-sim.github.io/IsaacLab/latest/source/extensions/omni.isaac.lab_tasks/omni.isaac.lab_tasks.utils.imitation_learning.html)
