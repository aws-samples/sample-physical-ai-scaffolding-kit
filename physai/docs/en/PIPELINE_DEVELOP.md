# Develop pipeline with using physai

**physai** is a cloud-native platform on AWS for robot developers to go from collected demos to evaluated policies, without managing infrastructure.

## 1. Overview

The physai platform automates the full robot learning workflow:

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

### What the platform provides vs. what you implement

| Platform provides | You implement |
|---|---|
| Slurm job chain construction (`--dependency=afterok`) | `train.sh` / `eval.sh` entrypoint logic |
| Container build & deploy (Pyxis/enroot) | `setup-hooks/` installation scripts |
| Data path resolution (`--dataset <name>` → `/fsx/datasets/<name>`) | Data format I/O inside your container |
| `RUN_CONFIG` generation & placement on cluster | Reading parameters from `RUN_CONFIG` in your scripts |
| SSH orchestration, log streaming, job management | Nothing — the CLI handles this |
| MLflow experiment tracking | Nothing — the registration stage handles this |

---

## 2. Quick Start: Creating Your Own Pipeline

To run your model on physai, prepare these three things:

```
your-project/
├── project.yaml                        # 1. Shared container defaults
├── containers/
│   ├── your-trainer/                   # 2. Container definitions
│   │   ├── container.yaml
│   │   ├── app/
│   │   │   └── train.sh               #    Entrypoint (must fulfill contract)
│   │   └── setup-hooks/
│   │       ├── 10-system-packages.root.sh
│   │       └── 20-install-deps.sh
│   └── your-eval-runtime/
│       ├── container.yaml
│       ├── app/
│       │   └── eval.sh
│       └── setup-hooks/
│           └── ...
├── configs/
│   └── your_robot_task_model.yaml      # 3. Pipeline config
└── model_configs/
    └── your-model/
        └── your-robot/                 # 4. Model-specific config files
            └── ...
```

### Step-by-step

1. **Define containers** — Create a trainer container and (optionally) an eval runtime container. Each has `setup-hooks/` to build the environment, and `app/*.sh` entrypoints that fulfill the contracts in [§3](#3-container-definitions). See [§3.3](#33-setup-hooks) for hook patterns.

2. **Write a pipeline config** — Create a YAML file in `configs/` that declares which stages to run, which containers to use, and stage parameters. See [§4](#4-pipeline-configuration).

3. **Prepare model config** — Place model-specific config files (e.g., modality definitions) in `model_configs/<model>/<robot>/`. See [§5](#5-model-config-directory).

4. **Build containers on the cluster**:

   ```bash
   physai build containers/your-trainer
   physai build containers/your-eval-runtime
   ```

5. **Upload data and run**:

   ```bash
   physai upload datasets ./my-lerobot-dataset
   physai run --config configs/your_robot_task_model.yaml --dataset my-lerobot-dataset
   ```

See [CLI Reference](PHYSAI_CLI.md) for the full command documentation.

---

## 3. Container Definitions

Each container is a folder with a build recipe and entrypoint scripts. Containers are built on the cluster using Pyxis/enroot (not Docker) and exported as squashfs images to `/fsx/enroot/`.

### 3.1 Directory structure

```
my-container/
├── container.yaml          # Container manifest
├── app/                    # Copied to /app/ in the container
│   └── train.sh            # Entrypoint script(s)
└── setup-hooks/
    ├── 10-system-packages.root.sh   # Runs as root
    └── 20-install-deps.sh           # Runs as user
```

### 3.2 project.yaml and container.yaml

`project.yaml` in a parent directory defines shared defaults for all containers in the same project. The builder walks up from the container folder and uses the nearest `project.yaml` it finds. A project can set `base_image` or `base_container` as the default base for all its containers.

```yaml
# project.yaml — shared defaults
base_image: nvcr.io/nvidia/cuda:12.8.1-cudnn-devel-ubuntu24.04
env:
  PIP_CONSTRAINT: ""
  NVIDIA_VISIBLE_DEVICES: all
```

> **Schema**: [`project.schema.json`](../../cli/physai/schemas/project.schema.json)

`container.yaml` overrides and extends `project.yaml`. Scalars override; `env` dicts merge.

```yaml
# container.yaml — per-container config
name: my-trainer
partition: gpu
gres: "gpu:1"
env:
  MY_CUSTOM_VAR: "value"    # merged into project.yaml env
```

> **Schema**: [`container.schema.json`](../../cli/physai/schemas/container.schema.json)

Exactly one of `base_image` or `base_container` must be set (mutually exclusive). To layer on another container you've already built:

```yaml
name: my-extended-runtime
base_container: my-base-runtime   # requires /fsx/enroot/my-base-runtime.sqsh
partition: gpu
gres: "gpu:1"
```

`physai build` checks that the parent image exists on the cluster before submitting. Build the base first, then the derived container.

### 3.3 Setup hooks

Files in `setup-hooks/` run in numeric prefix order during the build. Two variants:

- `NN-name.sh` — runs as an unprivileged user inside the container.
- `NN-name.root.sh` — runs as root (typical for `apt-get install`, system files).

The merged `env` from `project.yaml` + `container.yaml` is exported into the container, so hooks and entrypoints see the same variables.

**Typical hook pattern:**

| Order | Purpose | Example |
|-------|---------|---------|
| `10-*.root.sh` | System packages (`apt-get install build-essential git cmake ...`) | Install OS-level deps |
| `20-*.sh` | Clone repos, install Python deps (`pip install`, `uv sync`) | Install your model framework |
| `30-40-*.sh` | Download assets, pre-trained weights | Download model weights, robot URDFs |
| `50-*.sh` | Warm-up caches (e.g., shader compilation) | Avoid first-launch delays at runtime |
| `90-*.sh` | Cleanup (`pip cache purge`, remove `.git` dirs) | Reduce squashfs image size |

`app/` contents are copied to `/app/` in the image after all hooks succeed.

### 3.4 Entrypoint contracts

Each pipeline stage invokes a fixed entrypoint script. Your `setup-hooks/` build the environment; your `app/*.sh` scripts are what the pipeline calls at runtime. A single container may implement multiple entrypoints (e.g., a sim runtime container providing both `eval.sh` and `augment.sh`).

**Shared environment** passed to every entrypoint:

- `RUN_CONFIG` — path to the resolved run config YAML on the cluster. Read whatever you need from it (e.g., `sim.environment`, `model.name`).
- `DISPLAY` — set to `:0` for evaluation jobs (needed even for headless simulation that use GLX/GLFW).
- Merged `env` from `project.yaml` and `container.yaml`.

#### train.sh

| Item | Description |
|------|-------------|
| **Arguments** | `<dataset_dir> <model_config_dir> <output_dir> <max_steps>` |
| `<dataset_dir>` | Dataset directory on `/fsx/datasets/`. Format is defined by the container — the pipeline doesn't inspect it. |
| `<model_config_dir>` | Resolved model config directory (see [§5](#5-model-config-directory)). |
| `<output_dir>` | Empty per-run directory. Write checkpoint files **directly here** (not into a subdirectory). `eval.sh` later reads from this path verbatim. |
| `<max_steps>` | Training steps, from `stages.train.max_steps` or `--max-steps`. |
| **Exit code** | Non-zero on training failure. Downstream stages are cancelled. |

Minimal template:

```bash
#!/bin/bash
set -euo pipefail
DATASET_DIR=$1
MODEL_CONFIG_DIR=$2
OUTPUT_DIR=$3
MAX_STEPS=$4

# Your training command here.
# Write checkpoint files directly to $OUTPUT_DIR (not a subdirectory).
your_train_command \
  --data "$DATASET_DIR" \
  --config "$MODEL_CONFIG_DIR" \
  --output "$OUTPUT_DIR" \
  --steps "$MAX_STEPS"
```

#### eval.sh

| Item | Description |
|------|-------------|
| **Arguments** | `<checkpoint_dir> <model_config_dir> <output_dir> <rounds> [--visual]` |
| `<checkpoint_dir>` | Checkpoint directory on `/fsx/checkpoints/`. |
| `<model_config_dir>` | Resolved model config directory. |
| `<output_dir>` | Empty per-run directory. Write `metrics.json` (required) and optionally `eval.log`. |
| `<rounds>` | Number of evaluation rounds, from `stages.eval.rounds` or `--eval-rounds`. |
| `--visual` | If present, render to the attached virtual display (DCV). Otherwise use headless mode. |
| **Exit code** | Non-zero on evaluation failure. |

**Required output — `metrics.json`:**

```json
{
  "eval_rounds": 20,
  "success_rate": 0.2,
  "checkpoint": "<checkpoint_dir argument>"
}
```

Containers may add additional fields (e.g., `task`, `language_instruction`).

Minimal template:

```bash
#!/bin/bash
set -euo pipefail
CHECKPOINT_DIR=$1
MODEL_CONFIG_DIR=$2
OUTPUT_DIR=$3
ROUNDS=$4
VISUAL_FLAG="${5:-}"

# Read simulation config from RUN_CONFIG if needed:
#   ENVIRONMENT=$(python3 -c "import yaml; print(yaml.safe_load(open('$RUN_CONFIG'))['sim']['environment'])")

if [ "$VISUAL_FLAG" = "--visual" ]; then
  HEADLESS_ARG=""
else
  HEADLESS_ARG="--headless"
fi

your_eval_command \
  --checkpoint "$CHECKPOINT_DIR" \
  --config "$MODEL_CONFIG_DIR" \
  --rounds "$ROUNDS" \
  $HEADLESS_ARG \
  2>&1 | tee "$OUTPUT_DIR/eval.log"

# Write metrics.json (must include at least eval_rounds, success_rate, checkpoint)
python3 -c "
import json
metrics = {
    'eval_rounds': $ROUNDS,
    'success_rate': <parse from output>,
    'checkpoint': '$CHECKPOINT_DIR'
}
json.dump(metrics, open('$OUTPUT_DIR/metrics.json', 'w'))
"
```

#### convert.sh (planned — not yet implemented)

| Item | Description |
|------|-------------|
| **Arguments** | `<input_hdf5> <output_dir>` |
| **Contract** | Convert source format to a dataset the trainer can consume. |

#### validate.sh (planned — not yet implemented)

| Item | Description |
|------|-------------|
| **Arguments** | `<dataset_dir> <model_config_dir>` |
| **Contract** | Validate dataset against model requirements. Exit non-zero on failure. |

#### augment.sh (optional, planned — not yet implemented)

| Item | Description |
|------|-------------|
| **Arguments** | `<input_hdf5> <output_dir> <num_trials>` |
| **Contract** | Produce augmented HDF5 in the same format with more episodes. |

### 3.5 Build process

1. Merge `project.yaml` + `container.yaml` locally.
2. rsync `setup-hooks/`, `app/`, and build helpers to `/fsx/physai/builds/<name>-<ts>/` on the cluster.
3. Submit a Slurm job on the configured `partition` with `gres`:
   a. Create the container from the base (`--container-image=<base_image>` or `--container-image=/fsx/enroot/<base_container>.sqsh`).
   b. Run each setup hook in order (root hooks add `--container-remap-root`).
   c. Copy `app/` contents into `/app/` in the container.
   d. Export to squashfs: `/fsx/enroot/<name>.sqsh`.

See PHYSAI-DESIGN.md §7 for CLI-side details (preflight checks, in-flight-build dependencies, `--rebuild` semantics).

---

## 4. Pipeline Configuration

One config file per pipeline variant (robot + task + model), stored in `configs/`. This YAML describes which stages to run, which containers and resources to use, and what parameters to pass.

### 4.1 Config file format

```yaml
pipeline:
  stages: [convert, validate, train, eval, register]
  # With augmentation: [augment, convert, validate, train, eval, register]

sim:
  platform: <sim_platform>           # e.g., leisaac, robosuite
  # Remaining fields are platform-specific — the pipeline does not interpret them.
  # They are passed to containers via RUN_CONFIG.
  # Example (LeIsaac):
  #   environment: LeIsaac-SO101-PickOrange-v0
  #   mimic_environment: LeIsaac-SO101-PickOrange-Mimic-v0
  #   language_instruction: "Pick up the orange and place it on the plate"

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
    constraint: <feature>             # optional GPU type constraint, e.g., l40s
    container: <trainer_container>
    max_steps: 10000
  eval:
    partition: gpu
    gres: "gpu:1"
    container: <sim_runtime_container>
    rounds: 20
  register:
    partition: cpu
    container: <register_container>
```

> **Schema**: [`run-config.schema.json`](../../cli/physai/schemas/run-config.schema.json)

**Key fields:**

- `pipeline.stages` — which stages run by default. The CLI's `--from`/`--to` flags narrow this to a contiguous subrange.
- `sim.*` — platform-specific simulation parameters. The pipeline passes them through to containers via `RUN_CONFIG`; it does not interpret them. Your `eval.sh` / `augment.sh` reads what it needs.
- `model.config_dir` — relative name resolved via configured search paths (see [§5](#5-model-config-directory)).
- `stages.<name>.container` — container name (must match `name` in `container.yaml`).
- `stages.<name>.partition` / `gres` / `constraint` — Slurm resource allocation.
- `stages.train.max_steps` / `stages.eval.rounds` — stage-specific parameters passed to entrypoints.

### 4.2 GPU feature constraints

The cluster automatically tags GPU nodes with a Slurm feature based on instance type. Use these as the `constraint` value:

| Instance family | Feature |
|-----------------|---------|
| `ml.g6e.*` | `l40s` |
| `ml.g6.*` | `l4` |
| `ml.g5.*` | `a10g` |
| `ml.p3.*` | `v100` |
| `ml.p4d.*`, `ml.p4de.*` | `a100` |
| `ml.p5.*` | `h100` |

Slurm supports boolean expressions: `l40s` (exact), `l40s|h100` (OR), `!a10g` (NOT). See the [Slurm `--constraint` documentation](https://slurm.schedmd.com/sbatch.html#OPT_constraint).

### 4.3 Required arguments per starting stage

Only `train` and `eval` are implemented today. Other stages are listed for forward reference.

| `--from` | Required CLI arg | Resolves to | Implemented? |
|----------|------------------|-------------|--------------|
| `augment` | `--raw <name>` | `/fsx/raw/<name>` | No (planned) |
| `convert` | `--raw <name>` | `/fsx/raw/<name>` | No (planned) |
| `validate` | `--dataset <name>` | `/fsx/datasets/<name>` | No (planned) |
| `train` | `--dataset <name>` | `/fsx/datasets/<name>` | Yes |
| `eval` | `--checkpoint <name>` | `/fsx/checkpoints/<name>` | Yes |
| `register` | (none) | | No (planned) |

### 4.4 CLI overrides for stage parameters

```bash
--max-steps 50000        # overrides stages.train.max_steps
--eval-rounds 50         # overrides stages.eval.rounds
--visual                 # render eval to a virtual display (DCV)
```

---

## 5. Model Config Directory

Per-model, per-robot config files stored locally. The `model.config_dir` in the pipeline config is a relative name (e.g., `gr00t-n1.6/so101-singlecam`). The CLI resolves it against configured search paths and rsyncs the matched directory to the cluster. The pipeline passes the resolved directory to containers — it does not interpret the contents.

```
model_configs/
└── <model>/
    └── <robot>/
        └── <model-specific config files>
```

Search paths are configured in `~/.physai/config.yaml`:

```yaml
model_config_roots:
  - ~/projects/physai/examples/so101-gr00t/model_configs
  - ~/projects/my-custom-model/model_configs
```

Or per-command with `--model-config-root <path>` (repeatable).

What goes in the model config directory depends entirely on your model. The pipeline does not inspect the contents — it just passes the path to your `train.sh` and `eval.sh`.

---

## 6. Example: SO-101 + GR00T N1.6

This section walks through the shipped example to illustrate how all pieces fit together for a real model.

### 6.1 Project layout

```
examples/so101-gr00t/
├── project.yaml
├── configs/
│   ├── so101_pickorange_gr00t-n1.6.yaml
│   └── so101_liftcube_gr00t-n1.6.yaml
├── model_configs/
│   └── gr00t-n1.6/
│       ├── so101-singlecam/
│       │   ├── modality.json
│       │   └── modality_config.py
│       └── so101-dualcam/
│           ├── modality.json
│           └── modality_config.py
└── containers/
    ├── leisaac-runtime/              # Base: IsaacSim + LeIsaac (no GR00T)
    │   ├── container.yaml
    │   ├── app/                      # Empty — pure base image
    │   └── setup-hooks/
    │       ├── 10-system-packages.root.sh
    │       ├── 20-install-leisaac.sh
    │       ├── 40-download-assets.sh
    │       ├── 50-warmup.sh
    │       └── 90-cleanup.sh
    ├── leisaac-gr00t-n1.6/           # Eval runtime: leisaac-runtime + GR00T
    │   ├── container.yaml            # base_container: leisaac-runtime
    │   ├── app/
    │   │   └── eval.sh
    │   └── setup-hooks/
    │       ├── 10-install-gr00t.sh
    │       └── 90-cleanup.sh
    └── gr00t-n1.6-trainer/           # Training: CUDA base + GR00T
        ├── container.yaml
        ├── app/
        │   └── train.sh
        └── setup-hooks/
            ├── 10-system-packages.root.sh
            ├── 20-install-gr00t.sh
            └── 90-cleanup.sh
```

### 6.2 project.yaml

```yaml
base_image: nvcr.io/nvidia/cuda:12.8.1-cudnn-devel-ubuntu24.04
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

### 6.3 Pipeline config

```yaml
# configs/so101_pickorange_gr00t-n1.6.yaml
pipeline:
  stages: [train, eval]    # Only implemented stages listed

sim:
  platform: leisaac
  environment: LeIsaac-SO101-PickOrange-v0
  mimic_environment: LeIsaac-SO101-PickOrange-Mimic-v0
  language_instruction: "Pick up the orange and place it on the plate"

model:
  name: gr00t-n1.6
  config_dir: gr00t-n1.6/so101-dualcam

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

A second config, `so101_liftcube_gr00t-n1.6.yaml`, differs only in task (`LeIsaac-SO101-LiftCube-v0`, single camera) and uses `config_dir: gr00t-n1.6/so101-singlecam`.

### 6.4 Model config

`modality.json` defines the state/action dimensions and camera mappings for the robot configuration. For example, `so101-singlecam` maps a single `front` camera while `so101-dualcam` maps both `front` and `wrist` cameras.

### 6.5 Setup hooks

| Container | Hook | What it does |
|-----------|------|-------------|
| `leisaac-runtime` | `10-system-packages.root.sh` | Installs build-essential, git, cmake, ffmpeg, EGL/GLX libs, and `uv` (Python package manager) |
| | `20-install-leisaac.sh` | Clones LeIsaac, installs Python 3.11 venv, PyTorch 2.7, IsaacSim 5.1.0 (pip), IsaacLab, and ZMQ deps |
| | `40-download-assets.sh` | Downloads SO-101 robot USD and scene assets from GitHub releases |
| | `50-warmup.sh` | Warms up IsaacSim shader caches (avoids 5-10 min first-launch delay) |
| | `90-cleanup.sh` | Removes pip/uv caches, .git dirs to reduce squashfs size |
| `leisaac-gr00t-n1.6` | `10-install-gr00t.sh` | Clones Isaac-GR00T at `n1.6-release`, installs via `uv sync` + flash-attn |
| | `90-cleanup.sh` | Removes caches |
| `gr00t-n1.6-trainer` | `10-system-packages.root.sh` | Installs build-essential, git, cmake, ffmpeg, libaio, and `uv` |
| | `20-install-gr00t.sh` | Same GR00T install (clone + uv sync + flash-attn) |
| | `90-cleanup.sh` | Removes caches |

### 6.6 train.sh implementation details

The `gr00t-n1.6-trainer` container's `train.sh`:

1. Copies `modality.json` from the model config dir into the dataset's `meta/` directory (required by GR00T's data loader)
2. Sets up PyTorch distributed env vars (`MASTER_ADDR`, `WORLD_SIZE`, etc.) even for single-GPU
3. Runs `launch_finetune.py` with `--base-model-path nvidia/GR00T-N1.6-3B`
4. After training, GR00T writes to `<output_dir>/.work/checkpoint-<step>/`. The script moves checkpoint files to `<output_dir>` directly (per the contract) and cleans up the work directory.

Key training parameters (hardcoded in the example):

- `--global-batch-size 12`
- `--save-steps` = `max_steps` (single final checkpoint)
- `--save-total-limit 1`
- `--dataloader-num-workers 4`
- `--embodiment-tag NEW_EMBODIMENT`

### 6.7 eval.sh implementation details

The `leisaac-gr00t-n1.6` container's `eval.sh` implements a two-process architecture:

1. **GR00T policy server** — Starts `run_gr00t_server.py` in the background on a random port. The server loads the checkpoint and exposes a gRPC inference endpoint. The script polls the server (up to 120s) until it responds to `ping()`.

2. **LeIsaac simulation client** — Runs `policy_inference.py` which connects to the policy server and rolls out the policy in the IsaacSim environment for `<rounds>` episodes.

Key implementation details:

- Reads `sim.environment` and `sim.language_instruction` from `$RUN_CONFIG`
- A watchdog process monitors the eval log for fatal patterns (`CUDA error`, `Segmentation fault`, etc.) and kills the eval if IsaacSim becomes unrecoverable
- `DISPLAY` must be set even in headless mode (IsaacSim requires GLFW/GLX)
- `--headless` flag controls whether IsaacSim renders to a window or runs offscreen
- Parses success rate from stdout and writes `metrics.json`

---

## 7. Platform Architecture

### 7.1 System overview

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

The CLI establishes a single SSH ControlMaster connection at session start and multiplexes all subsequent commands over it.

### 7.2 Slurm job chain

`physai run` submits one Slurm job per stage, linked by `--dependency=afterok`:

```bash
RUN_ID=run-20260415-155400
JOB1=$(sbatch --parsable --job-name=physai/run/$RUN_ID/train    train.sh)
JOB2=$(sbatch --parsable --job-name=physai/run/$RUN_ID/eval     --dependency=afterok:$JOB1 eval.sh)
JOB3=$(sbatch --parsable --job-name=physai/run/$RUN_ID/register --dependency=afterok:$JOB2 register.sh)
```

All jobs share a run ID. If any step fails, downstream jobs are cancelled. `physai cancel` on any job cancels all jobs sharing the run ID.

If a container image is currently being built (`physai build` in progress), the pipeline automatically adds the build job as a dependency — you can kick off `physai run` immediately after starting `physai build`.

### 7.3 Data augmentation details

When augmentation is enabled, the orchestrator runs augmentation and conversion as a single Slurm job on the same GPU node. The augmented HDF5 is written to local NVMe (not `/fsx`), then conversion reads from local NVMe and writes to `/fsx`. The augmented HDF5 — which can be 600GB+ — never touches shared storage and is automatically cleaned up when the job ends.

### 7.4 Visual evaluation via DCV

`physai eval --visual` streams a rendered simulation viewport to the developer's browser via NICE DCV:

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
Username: ubuntu          Password: xxxxxxx

Streaming eval log (Ctrl-C to detach)...
```

The pipeline submits a Slurm job with `--gres=gpu:1,dcv:1`, creates a DCV session, prints the SSM port-forwarding command, and runs `eval.sh` with `--visual`. DCV server is installed on GPU workers via HyperPod lifecycle scripts. SSM port forwarding requires no security group changes.

### 7.5 Experiment tracking (MLflow) — planned, not yet implemented

Once implemented, each completed run will log to SageMaker MLflow:

| Category | What's logged |
|----------|--------------|
| Parameters | model, dataset, max_steps, batch_size, augmentation config |
| Metrics | Training loss (per step), eval success rate |
| Artifacts | Checkpoint path (S3), evaluation videos (S3), run_config.yaml |
| Tags | Run ID, model type, task name, robot |

---

## 8. Dataset Format Reference

The pipeline uses LeRobot v2.1 as the standard dataset format:

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

---

## 9. Related Documents

- [Platform Architecture (PIPELINE_DESIGN.md)](PIPELINE_DESIGN.md) — storage design, Slurm job chain internals, DCV visual evaluation, cost model
- [CLI Reference (PHYSAI_CLI.md)](PHYSAI_CLI.md) — full command documentation
- [Infrastructure (INFRA.md)](INFRA.md) — CDK stack layout and lifecycle scripts

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
