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
│  Worker Partition: "gpu" (fixed count, set in cdk.json)          │
│    → Augmentation, Training, Evaluation                          │
│                                                                  │
│  Worker Partition: "cpu" (fixed count, set in cdk.json)          │
│    → Format conversion, validation, registration                 │
│                                                                  │
│  All nodes mount: /fsx (FSx for Lustre)                          │
└──────────────────────────────────────────────────────────────────┘
         │
         │
         ▼
┌──────────────────────┐   ┌──────────────────────┐
│  S3 (permanent)      │   │  SageMaker MLflow    │
│  ├── raw/            │   │  (Tracking Server)   │
│  ├── datasets/       │   └──────────────────────┘
│  ├── checkpoints/    │
│  └── results/        │
│                      │
│  FSx (working)       │
│  /fsx/               │
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
pipeline:
  stages: [convert, validate, train, eval, register]
  # With augmentation: [augment, convert, validate, train, eval, register]

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

stages:
  augment:
    partition: gpu
    gres: "gpu:1"
    container: <sim_runtime_container>
  convert:
    partition: cpu
    container: <converter_container>
  validate:
    partition: cpu
    container: <converter_container>
  train:
    partition: gpu
    gres: "gpu:1"
    constraint: <feature>             # optional, e.g., l40s
    container: <trainer_container>
    max_steps: 10000
  eval:
    partition: gpu
    gres: "gpu:1"
    container: <sim_runtime_container>
    rounds: 20
  register:
    partition: cpu
```

`pipeline.stages` defines which stages run by default. The CLI's `--from`/`--to` flags override this. Each stage entry contains both Slurm resource config (partition, gres, constraint, container) and stage-specific parameters (max_steps, rounds).

The `physai` CLI rsyncs the config and resolves `model.config_dir` via configured search paths (see PHYSAI-DESIGN.md §3). Datasets and checkpoints are referenced by name and resolved to `/fsx/` paths.

### 3.2 Container Protocol

Each container implements fixed entrypoint scripts. The pipeline calls these and nothing else. The config is passed via `RUN_CONFIG` environment variable.

**Sim runtime container** (augmentation + evaluation):

| Entrypoint | Arguments | Contract |
|-----------|-----------|----------|
| `/app/eval.sh` | `<checkpoint_dir> <model_config_dir> <output_dir> <eval_rounds> [--visual]` | Run trained policy in simulation. Write to `<output_dir>`: `eval.log` (full stdout/stderr), `metrics.json` (see below). Pass `--visual` to render to display (DCV); otherwise run headless. Exit code reflects eval success. |
| `/app/augment.sh` (optional) | `<input_hdf5> <output_dir> <num_trials>` | Produce augmented HDF5 in same format with more episodes. |

**eval.sh output: `metrics.json`**

```json
{
  "eval_rounds": 20,
  "success_rate": 0.2,
  "checkpoint": "<checkpoint_dir argument>"
}
```

Containers may add additional fields (e.g., `task`, `language_instruction`).

**Trainer container** (model training):

| Entrypoint | Arguments | Contract |
|-----------|-----------|----------|
| `/app/train.sh` | `<dataset_dir> <model_config_dir> <output_dir> <max_steps>` | Run training. Dataset format is defined by the container — the pipeline doesn't inspect it. Write checkpoint files directly to `<output_dir>` (not into a subdirectory). |

> **TODO**: Define a `train_summary.json` or similar output for the `register` stage to consume (e.g., final loss, steps completed, checkpoint paths). Currently `train.sh` only writes model checkpoints.

**Converter container** (conversion + validation):

| Entrypoint | Arguments | Contract |
|-----------|-----------|----------|
| `/app/convert.sh` | `<input_hdf5> <output_dir>` | Convert source format → a dataset the trainer can consume. |
| `/app/validate.sh` | `<dataset_dir> <model_config_dir>` | Validate dataset against model requirements. Exit non-zero on failure. |

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
    ├── leisaac-runtime/              # Base: IsaacSim + LeIsaac (no GR00T)
    │   ├── container.yaml
    │   ├── app/                      # Empty; this is a pure base
    │   └── setup-hooks/              # Run in order during build
    │       ├── 10-system-packages.root.sh
    │       ├── 20-install-leisaac.sh
    │       └── ...
    ├── leisaac-gr00t-n1.6/           # Eval runtime: leisaac-runtime + GR00T N1.6
    │   ├── container.yaml            # base_container: leisaac-runtime
    │   ├── app/
    │   │   └── eval.sh
    │   └── setup-hooks/
    │       └── 10-install-gr00t.sh
    └── gr00t-n1.6-trainer/
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
  GR00T_REF: "n1.6-release"
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

1. Merge `project.yaml` + `container.yaml` locally. Exactly one of `base_image` or `base_container` must be set (mutually exclusive).
2. rsync the container's `setup-hooks/`, `app/`, and packaged build helpers to `/fsx/physai/builds/<name>-<ts>/` on the cluster; write the merged `env.txt` and the generated `build.sbatch` into the same directory.
3. Submit a Slurm job on the configured `partition` with `gres`:
   a. Create the container from the base (`srun --container-image=<base_image>` or `--container-image=/fsx/enroot/<base_container>.sqsh`) with `--container-remap-root`, loading env from `env.txt`.
   b. Run each setup hook in order via `srun --container-name=<name>` (root hooks add `--container-remap-root`).
   c. Copy `app/` contents into `/app/` in the container.
   d. Export to squashfs: `enroot export -o /fsx/enroot/<name>.sqsh`.

See PHYSAI-DESIGN.md §7 for the CLI-side details (preflight checks, in-flight-build dependencies, `--rebuild` semantics).

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

**Input**: Dataset on `/fsx/datasets/` + model config dir → **Output**: Checkpoint on `/fsx/checkpoints/`

The trainer container's `/app/train.sh` handles all training logic, including any dataset format expectations and model-specific preprocessing (e.g., normalization stats).

## 6. Evaluation Subsystem

**Input**: Checkpoint + task environment → **Output**: `metrics.json` on `/fsx/evaluations/`, published to S3 by registration stage

The sim runtime container's `/app/eval.sh` runs the trained policy in simulation. Architecture: policy server (model-specific) + simulation client (environment-specific) in the same Slurm job.

### 6.1 Visual Evaluation via DCV

For human review and demos, `physai eval --visual` streams a rendered simulation viewport to the developer's browser via NICE DCV.

```bash
$ physai eval --visual --config so101_pickorange_gr00t-n1.6.yaml \
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

`physai run` submits one Slurm job per stage, linked by `--dependency=afterok`. The stages to run come from `pipeline.stages` in the config; `--from`/`--to` narrow that list to a contiguous subrange.

```bash
# Default stages from config: [convert, validate, train, eval, register]
RUN_ID=run-20260415-155400
JOB1=$(sbatch --parsable --job-name=physai/run/$RUN_ID/convert convert.sh)
JOB2=$(sbatch --parsable --job-name=physai/run/$RUN_ID/validate --dependency=afterok:$JOB1 validate.sh)
JOB3=$(sbatch --parsable --job-name=physai/run/$RUN_ID/train    --dependency=afterok:$JOB2 train.sh)
JOB4=$(sbatch --parsable --job-name=physai/run/$RUN_ID/eval     --dependency=afterok:$JOB3 eval.sh)
JOB5=$(sbatch --parsable --job-name=physai/run/$RUN_ID/register --dependency=afterok:$JOB4 register.sh)

# With augmentation: [augment, convert, validate, train, eval, register]
# (augment + convert co-scheduled as single GPU job per §4)
JOB1=$(sbatch --parsable --job-name=physai/run/$RUN_ID/augment_convert augment_and_convert.sh)
JOB2=$(sbatch --parsable --dependency=afterok:$JOB1 validate.sh)
JOB3=$(sbatch --parsable --dependency=afterok:$JOB2 train.sh)
JOB4=$(sbatch --parsable --dependency=afterok:$JOB3 eval.sh)
JOB5=$(sbatch --parsable --dependency=afterok:$JOB4 register.sh)

# Override: --from train
JOB1=$(sbatch --parsable --job-name=physai/run/$RUN_ID/train train.sh)
JOB2=$(sbatch --parsable --job-name=physai/run/$RUN_ID/eval     --dependency=afterok:$JOB1 eval.sh)
JOB3=$(sbatch --parsable --job-name=physai/run/$RUN_ID/register --dependency=afterok:$JOB2 register.sh)
```

All jobs share a run ID via `--job-name=physai/run/<run-id>/<stage>`. If any step fails, downstream jobs are automatically cancelled by Slurm's dependency mechanism. `physai cancel` on any job in the chain cancels all jobs sharing the run ID.

### 7.2 physai CLI

The `physai` CLI runs locally and orchestrates work on the cluster via SSH (similar to Ansible). Every subcommand that triggers a workload submits a Slurm job, streams its output to the local terminal by default, and survives Ctrl-C — if the user detaches, the remote job keeps running and can be reconnected via `physai logs`.

```bash
# Container builds (see §3.4 for project/container layout)
physai build <path-to-container-folder>           # build, stream log
physai build <path-to-container-folder> --rebuild  # remove existing sqsh first

# Pipeline runs (--config is a local file, --dataset/--checkpoint are names on /fsx)
physai run   --config <local-yaml> --raw <name>                        # run default stages from config
physai run   --config <local-yaml> --from train --dataset <name>       # override: train → end
physai run   --config <local-yaml> --from eval --checkpoint <name>     # override: eval only
physai train --config <local-yaml> --dataset <name>                    # shortcut for --from train --to train
physai eval  --config <local-yaml> --checkpoint <name> [--eval-rounds N] [--visual]      # shortcut for --from eval --to eval

# Data management
physai ls <category> [<path>]         # list remote data (raw, datasets, checkpoints, enroot)
physai upload <category> <local-path> # upload data to cluster

# Job management (works for any job type: build, run, train, eval)
physai list                    # all jobs
physai status <job-id>
physai logs <job-id>           # tail/reconnect to log
physai cancel <job-id>
physai clean                   # remove old build dirs, logs, and stale enroot containers
physai doctor                  # cluster health checks with interactive fixes
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

GPU and CPU partitions run fixed worker counts configured in `infra/cdk.json` (see INFRA.md). HyperPod does not auto-scale — change counts and redeploy `PhysaiClusterStack` to add or remove workers.

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

Provisions: HyperPod cluster, FSx for Lustre (1.2TB) with DRA to S3 for `/fsx/raw/` (auto-import only), S3 bucket (permanent storage), SageMaker MLflow, IAM roles, CloudWatch alarm on FSx `FreeStorageCapacity`.

## 9. Cost Model

All cluster nodes run 24/7 — HyperPod does not stop idle instances. A default deployment (1× GPU worker, 1× CPU worker, both always on) costs roughly **$2,700/month** in us-west-2, dominated by the GPU worker (~$2,000/month for a single `ml.g6e.2xlarge`).

Scale cost by setting worker counts in `infra/cdk.json`:

- Idle (no workers): ~$310/month (controller + login + FSx + RDS + NAT + small services).
- Each additional `ml.g6e.2xlarge` GPU worker: ~$2,000/month.
- Each additional `ml.m5.2xlarge` CPU worker: ~$340/month.

## 10. References

- [AWS Sample: Embodied AI Platform](https://github.com/aws-samples/sample-embodied-ai-platform)
- [AWS Sample: Physical AI Scaffolding Kit](https://github.com/aws-samples/sample-physical-ai-scaffolding-kit)
- [SageMaker HyperPod Documentation](https://docs.aws.amazon.com/sagemaker/latest/dg/sagemaker-hyperpod.html)
- [LeRobot Dataset Format](https://huggingface.co/docs/lerobot/lerobot-dataset-v3)
- [GR00T N1.6 Fine-tuning Guide](https://github.com/NVIDIA/Isaac-GR00T/blob/main/getting_started/finetune_new_embodiment.md)
- [GR00T Data Preparation](https://github.com/NVIDIA/Isaac-GR00T/blob/main/getting_started/data_preparation.md)
- [OpenPI (π0)](https://github.com/Physical-Intelligence/openpi)
- [LeIsaac](https://github.com/LightwheelAI/leisaac)
- [Isaac Lab Mimic](https://isaac-sim.github.io/IsaacLab/latest/source/extensions/omni.isaac.lab_tasks/omni.isaac.lab_tasks.utils.imitation_learning.html)
