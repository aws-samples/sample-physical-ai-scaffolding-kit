# User Manual

This document is a complete, self-contained guide to using the physai platform.
If you just want to get started, jump to [Quick Start](#quick-start). If you
want to know what a specific command does, see [CLI Reference](#cli-reference).

## Contents

1. [What physai Does](#what-physai-does)
2. [Prerequisites](#prerequisites)
3. [Quick Start](#quick-start)
4. [Deployment](#deployment)
5. [Access to the Cluster](#access-to-the-cluster)
6. [Installing and Configuring the CLI](#installing-and-configuring-the-cli)
7. [Getting a Dataset](#getting-a-dataset)
8. [Building Containers](#building-containers)
9. [Running a Pipeline](#running-a-pipeline)
10. [Managing Data](#managing-data)
11. [Managing Jobs](#managing-jobs)
12. [CLI Reference](#cli-reference)
13. [The `run_config.yaml` Reference](#the-run_configyaml-reference)
14. [Costs](#costs)
15. [Troubleshooting](#troubleshooting)
16. [Tearing Down](#tearing-down)

## What physai Does

physai is a CLI that runs on your laptop. It submits containerized robot-
learning pipelines — data conversion, validation, training, evaluation — to a
SageMaker HyperPod Slurm cluster over SSH. The cluster holds persistent shared
storage (FSx for Lustre) and long-lived accounting history (RDS). By default
the submitted job's log streams to your terminal and survives Ctrl-C (the
remote job keeps running and you can reconnect). Pass `-n` / `--no-stream` to
submit and return immediately.

## Prerequisites

- AWS account with credentials configured (e.g., via AWS SSO or an admin
  profile named in `~/.aws/config`).
- Sufficient service quotas in the target region:
  - HyperPod cluster and instance types you plan to use (controller + login
    on `ml.c5.large`, GPU on `ml.g6e.2xlarge` by default, CPU on
    `ml.m5.2xlarge`).
  - VPC quota: the stack creates one VPC with NAT, plus a few subnets.
- Local tools:
  - Node.js 20+ and `npm` (for CDK).
  - Python 3.12+ with `pip`.
  - AWS CLI v2.
  - [Session Manager plugin for AWS CLI](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html).
  - `rsync` and `ssh` (preinstalled on macOS/Linux).

## Quick Start

```bash
# 1. Deploy (CDK is the only step that runs from infra/)
cd infra
npm install
npx cdk bootstrap
npx cdk deploy --all --require-approval never     # ~20 minutes
cd ..

# 2. SSH access
infra/scripts/setup-ssh.sh        # follow the printed instructions

# 3. Install CLI
pip install -e cli

# 4. Configure
mkdir -p ~/.physai && cat > ~/.physai/config.yaml <<EOF
host: physai-login
model_config_roots:
  - $(pwd)/examples/so101-gr00t/model_configs
EOF

# 5. First run — download the public PickOrange dataset, then build + submit.
#    `-n` submits without streaming logs so the commands return immediately;
#    use `physai list` / `physai logs <job-id>` to check on them.
pip install -U huggingface_hub
hf download LightwheelAI/leisaac-pick-orange \
  --repo-type dataset --local-dir /tmp/leisaac-pick-orange

physai build -n examples/so101-gr00t/containers/leisaac-runtime
physai build -n examples/so101-gr00t/containers/leisaac-gr00t-n1.6
physai build -n examples/so101-gr00t/containers/gr00t-n1.6-trainer
physai upload datasets /tmp/leisaac-pick-orange/
physai run -n --config examples/so101-gr00t/configs/so101_pickorange_gr00t-n1.6.yaml \
           --dataset leisaac-pick-orange
```

## Deployment

Two CloudFormation stacks are created:

| Stack | Contents | Termination protection |
|-------|----------|------------------------|
| `PhysaiInfraStack` | VPC, S3 data bucket, FSx, RDS (accounting), Secrets Manager | ON |
| `PhysaiClusterStack` | HyperPod cluster, IAM execution role, lifecycle-scripts bucket | OFF |

### What gets deployed

```
┌──────────────────────────────────────────────────────────────────────┐
│  PhysaiInfraStack  (stateful; retained on stack destroy)             │
│                                                                      │
│   VPC  (10.0.0.0/16, 2 AZs)                                          │
│   ├── public subnets + NAT gateway + internet gateway                │
│   ├── private subnets                                                │
│   └── S3 gateway VPC endpoint                                        │
│                                                                      │
│   S3 data bucket    s3://<clusterName>-data-<account>                │
│   FSx for Lustre    1.2 TB PERSISTENT_2, DRA → s3://.../raw/         │
│   RDS MariaDB       db.t4g.small  (Slurm accounting)                 │
│   Secrets Manager   DB credentials                                   │
└──────────────────────────────────────────────────────────────────────┘
          │ exports VPC / subnets / SG / FSx / RDS / Secret
          ▼
┌──────────────────────────────────────────────────────────────────────┐
│  PhysaiClusterStack  (stateless; safe to destroy and recreate)       │
│                                                                      │
│   S3 lifecycle-scripts bucket                                        │
│   IAM execution role                                                 │
│   SageMaker HyperPod cluster                                         │
│   ├── controller-machine  ml.c5.large × 1  (Slurm scheduler)         │
│   ├── login-group         ml.c5.large × 1  (SSH entry point)         │
│   ├── gpu-workers         ml.g6e.2xlarge   (configurable)            │
│   └── cpu-workers         ml.m5.2xlarge    (configurable)            │
│   All nodes mount /fsx                                               │
│                                                                      │
│   CloudWatch alarm  FSx FreeStorageCapacity                          │
└──────────────────────────────────────────────────────────────────────┘
```

S3 layout used by the pipeline:

```
s3://<clusterName>-data-<account>/
└── raw/        # Raw HDF5 demos. DRA auto-imports into /fsx/raw/ on access.
```

FSx layout (shared across all cluster nodes at `/fsx/`):

```
/fsx/
├── raw/            # Lazy-loaded from s3://.../raw/ via DRA
├── datasets/       # LeRobot datasets
├── checkpoints/    # Training checkpoints
├── evaluations/    # Eval outputs
├── enroot/         # Container .sqsh images
└── physai/         # CLI working state (builds, logs, sync dirs)
```

Deploy:

```bash
cd infra
npm install
npx cdk bootstrap                                # first time only, per account+region
npx cdk deploy --all --require-approval never
```

Deploy individually if needed:

```bash
npx cdk deploy PhysaiInfraStack
npx cdk deploy PhysaiClusterStack
```

Configure sizes via `infra/cdk.json` context:

```json
{
  "context": {
    "clusterName": "physai-cluster",
    "fsxCapacityGiB": 1200,
    "gpuWorkers": [
      { "name": "gpu-workers", "instanceType": "ml.g6e.2xlarge", "count": 1 }
    ],
    "cpuWorkerType": "ml.m5.2xlarge",
    "cpuWorkerCount": 1
  }
}
```

`gpuWorkers` is a list — add another entry to run a different GPU type
alongside the default. Each entry becomes a separate Slurm instance group;
use the name in Slurm constraints to target it. Example with two GPU types:

```json
"gpuWorkers": [
  { "name": "gpu-workers-l40s", "instanceType": "ml.g6e.2xlarge", "count": 2 },
  { "name": "gpu-workers-h100", "instanceType": "ml.p5.48xlarge", "count": 1 }
]
```

`cpuWorkerType` / `cpuWorkerCount` configure the single CPU instance group.
Controller and login nodes are fixed at `ml.c5.large × 1` each and aren't
configurable here.

### Applying lifecycle-script changes to a running cluster (advanced)

`npx cdk deploy PhysaiClusterStack` uploads any edits under `infra/lifecycle/`
to S3. Lifecycle scripts only run once, when a node is first created — so
existing nodes are unaffected by the upload. To pick up the new scripts on
worker and login nodes, replace them:

```bash
# from the login node (requires Slurm admin privileges)
scontrol update node=<node-name> state=fail reason="Action:Replace"
```

HyperPod tears the node down and re-provisions a fresh instance that runs
the updated lifecycle scripts.

The controller node can't be replaced this way. Apply controller-side
lifecycle changes by SSM-ing into the controller and re-running the relevant
script manually (the scripts are idempotent and safe to re-run). If that's
impractical, destroy and redeploy `PhysaiClusterStack` as a last resort.

## Access to the Cluster

The login node has no public IP. Access is via SSH tunneled through AWS SSM.

```bash
infra/scripts/setup-ssh.sh                       # uses ~/.ssh/id_rsa.pub, id_ed25519.pub, or id_ecdsa.pub
# optional: infra/scripts/setup-ssh.sh --key ~/.ssh/mykey.pub --profile myprofile --region us-west-2
```

The script:

1. Queries `PhysaiClusterStack` for the cluster name.
2. Finds the login node instance.
3. Uploads your public key to `/home/ubuntu/.ssh/authorized_keys` via SSM.
4. Prints a snippet to add to `~/.ssh/config`.

Add the snippet to `~/.ssh/config`, then test:

```bash
ssh physai-login
```

The first connection will prompt to accept the host key. The ProxyCommand
tunnels through SSM — no security groups need to be opened.

## Installing and Configuring the CLI

From the repo root:

```bash
pip install -e cli
```

Create `~/.physai/config.yaml`:

```yaml
host: physai-login                   # SSH host alias (required)
model_config_roots:                  # search path for model.config_dir (optional)
  - <path-to-physai>/examples/so101-gr00t/model_configs
```

Both values can be overridden per-invocation:

- `--host HOST` overrides `host`.
- `--model-config-root PATH` prepends to `model_config_roots` (can be given multiple times).

If you only ever use one host and one model-config root, fill them in here
and you won't need the flags.

## Getting a Dataset

For a first run, use the public **Pick Orange** dataset published by Lightwheel
AI alongside the LeIsaac simulation environment. It's already in LeRobot v2.1
format (60 episodes, ~36k frames, 698 MB), so you can upload it to the cluster
and run `train` + `eval` directly.

```bash
pip install -U huggingface_hub
hf download LightwheelAI/leisaac-pick-orange \
  --repo-type dataset --local-dir /tmp/leisaac-pick-orange
physai upload datasets /tmp/leisaac-pick-orange/
```

The dataset will appear at `/fsx/datasets/leisaac-pick-orange/` on the
cluster. Pair it with
`examples/so101-gr00t/configs/so101_pickorange_gr00t-n1.6.yaml` (which declares
`pipeline.stages: [train, eval]`) to run end-to-end.

Sources:

- LeIsaac policy guide (introduces the dataset): <https://lightwheelai.github.io/leisaac/docs/getting_started/policy_support>
- Dataset on Hugging Face: <https://huggingface.co/datasets/LightwheelAI/leisaac-pick-orange>

For your own data, convert HDF5 demos into LeRobot v2.1 format yourself, then
upload with `physai upload datasets`. (A `convert` stage is planned — see
[docs/STATUS.md](STATUS.md).)

## Building Containers

Containers on the cluster are built and run with **Enroot** (a lightweight,
rootless container runtime) via **Pyxis** (a Slurm plugin that lets jobs use
Enroot through `srun --container-image=...`). Two concepts to keep straight:

- **Image** — the built artifact, a squashfs file at `/fsx/enroot/<name>.sqsh`.
  One image per `physai build`. Images are immutable, shared across jobs, and
  live until you `--rebuild` them or delete the file.
- **Container** — a live runtime instance of an image, created on a worker
  node when a job starts and normally destroyed when the job ends. If a job
  is killed ungracefully the container can be left behind; `physai clean
  --enroot` removes these stale containers.

`physai build` produces images. Pipeline jobs consume them.

A container folder has this layout:

```
my-container/
├── container.yaml          # Container-specific config (see below)
├── app/                    # Copied to /app/ in the container
│   └── entrypoint.sh
└── setup-hooks/
    ├── 10-system-packages.root.sh   # Runs as root
    └── 20-install-deps.sh           # Runs as user (build step)
```

`project.yaml` in a parent directory defines shared defaults for all
containers in the same project. The builder walks up from the container
folder and uses the nearest `project.yaml` it finds.

```yaml
base_image: nvcr.io/nvidia/cuda:12.8.1-cudnn-devel-ubuntu24.04
env:
  PIP_CONSTRAINT: ""
  NVIDIA_VISIBLE_DEVICES: all
```

`container.yaml` overrides and adds container-specific fields. Values deep-
merge on top of `project.yaml` (scalars override, `env` dicts merge):

```yaml
name: gr00t-n1.6-trainer
partition: gpu
gres: "gpu:1"
```

### Layering on another container

Instead of a registry image, a container can start from another container
you've already built. Use `base_container` (mutually exclusive with
`base_image`):

```yaml
name: leisaac-gr00t-n1.6
base_container: leisaac-runtime   # requires /fsx/enroot/leisaac-runtime.sqsh
partition: gpu
gres: "gpu:1"
```

`physai build` checks that the parent image exists on the cluster before
submitting. Build the base first, then the derived container.

### Setup hooks

Files in `setup-hooks/` run in numeric prefix order during the build and
shape the image's contents. Two variants:

- `NN-name.sh` — runs as an unprivileged user inside the container.
- `NN-name.root.sh` — runs as root (typical for `apt-get install`, system files).

The merged `env` from `project.yaml` + `container.yaml` is exported into the
container so hooks and entrypoints see the same variables. The builder creates the
container from the base (`base_image` or `base_container`) up front and
exports the env; your hooks then run in order and operate on that
existing container.

`app/` contents are copied to `/app/` in the image after all hooks succeed.

### Entrypoint scripts

Each stage expects a fixed entrypoint script under `app/` in its container.
The pipeline calls the entrypoint with a known argument order — your
`setup-hooks/` build the environment; your `app/*.sh` are what the pipeline
invokes at run time.

A single container may implement multiple entrypoints — e.g., a simulation
container that provides both `eval.sh` and (future) `augment.sh`.

**Shared environment** passed to every entrypoint:

- `RUN_CONFIG` — path to the resolved `run_config.yaml` on the cluster. The
  script reads whatever it needs from it (e.g., `sim.environment`,
  `sim.language_instruction`, `model.name`).
- `DISPLAY` — set to `:0` for evaluation jobs (needed even for headless
  simulation clients that use GLX/GLFW).
- Merged `env` from `project.yaml` and `container.yaml`.

**`train.sh` contract**

| Field | Value |
|-|-|
| Arguments | `<dataset_dir> <model_config_dir> <output_dir> <max_steps>` |
| `<dataset_dir>` | Dataset directory on `/fsx/datasets/`. Format is defined by the container — the pipeline doesn't inspect it. |
| `<model_config_dir>` | Resolved `model.config_dir`. |
| `<output_dir>` | Empty per-run directory. The script writes checkpoint files directly here (not into a subdirectory); `eval.sh` later reads from this path verbatim. |
| `<max_steps>` | Training steps, from `stages.train.max_steps` or `--max-steps`. |
| Exit code | Non-zero on training failure. |

**`eval.sh` contract**

| Field | Value |
|-|-|
| Arguments | `<checkpoint_dir> <model_config_dir> <output_dir> <rounds> [--visual]` |
| `<checkpoint_dir>` | Directory from `/fsx/checkpoints/`. |
| `<model_config_dir>` | Resolved `model.config_dir`. |
| `<output_dir>` | Empty per-run directory. Write `eval.log` (full stdout/stderr) and `metrics.json` (schema below) here. |
| `<rounds>` | Number of evaluation rounds, from `stages.eval.rounds` or `--eval-rounds`. |
| `--visual` | Render to the attached virtual display for interactive viewing. Without the flag, the script should use headless mode if the evaluation supports it. |
| Exit code | Non-zero on eval failure. |

**`metrics.json` schema (minimum)**:

```json
{
  "eval_rounds": 20,
  "success_rate": 0.2,
  "checkpoint": "<checkpoint_dir argument>"
}
```

Containers may add additional fields (e.g., `task`, `language_instruction`).

### Model Config Directory

`model.config_dir` in `run_config.yaml` is a relative name like
`gr00t-n1.6/so101-singlecam`. The CLI resolves it against the
`model_config_roots` search path (from `~/.physai/config.yaml` or
`--model-config-root`), rsyncs the matched directory to the cluster, and
passes its remote path to entrypoints as `<model_config_dir>`.

The pipeline **does not interpret** the directory contents. Whatever files
your container needs for that model + robot combination go here. Recommended
layout:

```
<model_config_root>/
└── <model>/<robot>/
    └── <model-specific files>
```

The Phase 1 example uses this for GR00T N1.6 on SO-101:

```
examples/so101-gr00t/model_configs/
└── gr00t-n1.6/
    ├── so101-singlecam/
    │   ├── modality.json
    │   └── modality_config.py
    └── so101-dualcam/
        └── ...
```

### Build it

```bash
physai build examples/so101-gr00t/containers/gr00t-n1.6-trainer
physai build examples/so101-gr00t/containers/gr00t-n1.6-trainer --rebuild  # replace existing
```

`--rebuild` replaces an existing image. Without it, the build fails if the
image already exists.

The build log streams to your terminal. Press Ctrl-C to detach — the build
keeps running; reconnect with `physai logs <job-id>`.

## Running a Pipeline

A pipeline run is configured by a single YAML file:

```yaml
# examples/so101-gr00t/configs/so101_liftcube_gr00t-n1.6.yaml
pipeline:
  stages: [train, eval]

sim:
  platform: leisaac
  environment: LeIsaac-SO101-LiftCube-v0
  language_instruction: "Lift the red cube up"

model:
  name: gr00t-n1.6
  config_dir: model_configs/gr00t-n1.6/so101-singlecam

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

- `pipeline.stages`: the stages to run by default.
- `stages.<name>`: resource + parameter config per stage.
- `model.config_dir`: a relative name resolved against `model_config_roots`
  (see [The `run_config.yaml` Reference](#the-run_configyaml-reference)).

### Run the default stages

```bash
physai run --config examples/so101-gr00t/configs/so101_liftcube_gr00t-n1.6.yaml \
           --dataset so101_liftcube
```

### Run a subset of stages

```bash
# Just train (shortcut for `run --from train --to train`)
physai train --config ... --dataset so101_liftcube

# Just eval (shortcut for `run --from eval --to eval`)
physai eval --config ... --checkpoint gr00t-n1.6-liftcube-30k

# Arbitrary range
physai run --config ... --from train --to eval --dataset so101_liftcube
```

### Required arguments per starting stage

Only `train` and `eval` are implemented today. Other stages are listed for
forward reference — see [docs/STATUS.md](STATUS.md).

| `--from` | Required | Resolves to | Implemented? |
|----------|----------|-------------|--------------|
| `augment` | `--raw <name>` | `/fsx/raw/<name>` | No (planned) |
| `convert` | `--raw <name>` | `/fsx/raw/<name>` | No (planned) |
| `validate` | `--dataset <name>` | `/fsx/datasets/<name>` | No (planned) |
| `train` | `--dataset <name>` | `/fsx/datasets/<name>` | Yes |
| `eval` | `--checkpoint <name>` | `/fsx/checkpoints/<name>` | Yes |
| `register` | (none) | | No (planned) |

### Override stage parameters

```bash
--max-steps 50000        # overrides stages.train.max_steps
--eval-rounds 50         # overrides stages.eval.rounds
--visual                 # render eval to a virtual display (planned; not implemented yet)
```

### What happens

1. The CLI packages your config and model config and sends them to the cluster.
2. It submits one job per stage, chained so each waits for the previous to succeed.
3. Each stage's log streams to your terminal in turn — when one finishes, the next stage's log starts streaming. Ctrl-C detaches from the current stream; the chain keeps running.

## Managing Data

The cluster has shared storage mounted at `/fsx/` that every job can read and
write. Under `/fsx/`, the CLI exposes three directories by name:

- **`raw`** — raw demos (e.g., HDF5). Linked to S3 via a Data Repository
  Association (DRA); see [Raw Data & S3 Auto-import](#raw-data--s3-auto-import).
- **`datasets`** — training-ready datasets.
- **`checkpoints`** — training checkpoints.

`physai ls <category>` lists one of these; `physai upload <category>` uploads
into one.

### List data

```bash
physai ls datasets
physai ls checkpoints
physai ls raw subfolder/
```

Shows human-readable sizes computed by `du -sh` per top-level entry.

### Upload data

```bash
physai upload datasets /path/to/so101_liftcube/
physai upload checkpoints /path/to/checkpoint-dir/
physai upload raw /path/to/demos.hdf5
```

`datasets` and `checkpoints` rsync directly to `/fsx/<category>/`. `raw` also
rsyncs to `/fsx/raw/`, but the CLI first recommends the S3 route and asks for
confirmation — for large raw data, S3 is usually preferred (see next section).

### Raw Data & S3 Auto-import

`/fsx/raw/` is linked to the S3 data bucket at `s3://<bucket>/raw/` via an FSx
**Data Repository Association (DRA)**. The link is **auto-import only**: any
object you put in `s3://<bucket>/raw/` appears in `/fsx/raw/` automatically,
loaded on first access (lazy). Files never take FSx capacity until a job
actually opens them.

Practical implications:

- **Prefer uploading raw demos to S3** (via `aws s3 cp`) when they're large.
  Jobs that open them later get lazy-loaded bytes on demand; FSx only caches
  what's actually read. This is much cheaper than duplicating 600 GB of raw
  HDF5 on FSx.
- **`physai upload raw`** does a direct rsync to `/fsx/raw/`. Useful for
  small files where the S3 round-trip isn't worth it, but the CLI prompts
  before doing this to nudge you toward S3 for larger data.
- **Deletions in S3 propagate to `/fsx/raw/`** (the DRA policy includes
  `DELETED`). Changes in `/fsx/raw/` are NOT exported back to S3 — this is a
  one-way (import-only) link.

To upload raw data via S3, first get the bucket name from the
`PhysaiInfraStack` CloudFormation output:

```bash
aws cloudformation describe-stacks --stack-name PhysaiInfraStack \
  --query 'Stacks[0].Outputs[?OutputKey==`DataBucketName`].OutputValue' \
  --output text
```

Then upload:

```bash
aws s3 cp /path/to/demos.hdf5 s3://<data-bucket>/raw/
# directories:
aws s3 cp --recursive /path/to/dir/ s3://<data-bucket>/raw/

# verify it's visible on the cluster
physai ls raw
```

## Managing Jobs

A job is any Slurm job the CLI submitted — build, run, train, eval, etc.

```bash
physai list                   # active + recently-completed physai jobs
physai status <job-id>        # detailed status
physai logs <job-id>          # tail the job log; also works for completed jobs (Ctrl-C detaches)
physai cancel <job-id>        # cancels this job; all downstream stages of the same run are cancelled with it
physai clean                  # remove build dirs, logs, sync dirs older than 7 days
physai clean --all            # remove all (ignoring age)
physai clean --enroot         # remove stale Enroot containers on worker nodes (see Building Containers)
physai clean --dry-run        # show what would be removed
physai doctor                 # run cluster health checks; offer fixes interactively
```

## CLI Reference

### Global

```
physai [--host HOST] <command> [options]
```

- `--host HOST`: override `host` in `~/.physai/config.yaml`.

### `physai build <container-dir>`

Build a container as a squashfs image on the cluster.

- `<container-dir>`: local path to a folder containing `container.yaml`, `app/`, and `setup-hooks/`.
- `--rebuild`: remove the existing `.sqsh` first.
- `-n`, `--no-stream`: submit the job and return immediately without streaming the build log.

### `physai run`

Run one or more pipeline stages.

- `--config <local-yaml>` (required): path to the run config.
- `--from STAGE` / `--to STAGE`: override `pipeline.stages` to a subrange.
- `--raw NAME` / `--dataset NAME` / `--checkpoint NAME`: input names (required by the starting stage — see [Required arguments per starting stage](#required-arguments-per-starting-stage)).
- `--max-steps N`: override `stages.train.max_steps`.
- `--eval-rounds N`: override `stages.eval.rounds`.
- `--visual`: render evaluation to a remote virtual display you can connect to from your browser (planned; not implemented yet).
- `--model-config-root PATH`: prepend to the model-config search path.
- `-n`, `--no-stream`: submit the job(s) and return immediately without streaming any logs. Useful for scripting or pasting a series of `physai` commands to submit jobs back-to-back.

`physai train` and `physai eval` accept the same `-n` / `--no-stream` flag.

### `physai train` / `physai eval`

Shortcuts:
- `physai train --config ... --dataset ...` ≡ `physai run --from train --to train --config ... --dataset ...`.
- `physai eval --config ... --checkpoint ...` ≡ `physai run --from eval --to eval --config ... --checkpoint ...`.

### `physai ls <category> [subpath]`

List a remote directory under `/fsx/`. Categories: `raw`, `datasets`, `checkpoints`.

### `physai upload <category> <local-path>`

Upload a local file or directory to `/fsx/<category>/`. For `raw`, the CLI first
recommends using S3 and asks for confirmation before rsyncing.

### `physai list`

Show physai-owned jobs.

### `physai status <job-id>` / `physai logs <job-id>` / `physai cancel <job-id>`

Job inspection and cancellation.

### `physai doctor`

Check cluster health. Runs a series of read-only probes and prints
`[PASS]` / `[FAIL]` / `[WARN]` per check. When a check fails AND has a
known fix, `doctor` interactively asks before applying it.

Current checks:

- **FSx directories**: each expected top-level dir (`raw`, `datasets`, `checkpoints`, `evaluations`, `physai`) is a directory at mode 0777, and `/fsx/enroot` is at mode 1777 (sticky; set by the enroot lifecycle script so users can't remove each other's named containers). Fix: `mkdir -p` + `chmod` to each dir's expected mode.
- **Slurm config drift among workers**: every slurmd node's cached conf
  (`/var/spool/slurmd/conf-cache/*.conf`) matches the majority. Fix:
  `scontrol reconfigure`. Unreachable nodes are reported as WARN,
  not FAIL.
- **slurmdbd reachable**: `sacct` returns successfully. No auto-fix —
  the message lists the RDS instance, Secrets Manager entry, and the
  SSM command to inspect slurmdbd on the controller.

Exits non-zero if any check is still FAIL after the user's chance to fix.

### `physai clean`

Remove old build dirs, logs, and sync dirs.

- `--older-than DAYS` (default 7)
- `--all`: ignore age, remove everything not belonging to active jobs.
- `--enroot`: remove stale Enroot containers from all worker nodes. These are
  leftover runtime instances from jobs that didn't exit cleanly; the
  `.sqsh` image files themselves are NOT removed.
- `--dry-run`, `-f` (no prompt).

## The `run_config.yaml` Reference

A `run_config.yaml` describes a pipeline run: which stages to execute, which
containers and resources to use, and what simulation/model parameters to pass
into the containers. See [Running a Pipeline](#running-a-pipeline) for
examples.

Top-level keys:

- `pipeline.stages` — ordered list of stages to run by default. `--from`/`--to` select a subrange of this list at invocation time.
- `model.name` — identifier for logging (free-form).
- `model.config_dir` — relative path resolved against `model_config_roots` (see [Installing and Configuring the CLI](#installing-and-configuring-the-cli)). The resolved directory is passed to container entrypoints as `<model_config_dir>`.
- `stages.<name>` — one block per stage listed in `pipeline.stages`:
  - `partition`: Slurm partition (`gpu` or `cpu`).
  - `gres` (optional): GPU/resource request, e.g. `"gpu:1"`.
  - `constraint` (optional): Slurm feature constraint. The lifecycle scripts tag each GPU node with a feature derived from its instance type family (see table below). Use the feature name to target a specific GPU. Slurm's boolean expression syntax is supported, e.g. `l40s|h100` to allow either.
  - `container`: the `name` field from the container's `container.yaml`.
  - Stage-specific parameters (e.g. `max_steps` for `train`, `rounds` for `eval`).
- `sim.*` — simulation-specific fields (environment name, language instruction, etc.). Consumed by the sim container's entrypoint.

### GPU feature constraints

The cluster tags GPU nodes automatically with a Slurm feature based on their
instance type family. Use these names as the `constraint` value on a stage:

| Instance family | Feature |
|-----------------|---------|
| `ml.g6e.*` | `l40s` |
| `ml.g6.*` | `l4` |
| `ml.g5.*` | `a10g` |
| `ml.p3.*` | `v100` |
| `ml.p4d.*`, `ml.p4de.*` | `a100` |
| `ml.p5.*` | `h100` |

Slurm supports boolean expressions for `--constraint`:

- `l40s` — only L40S GPUs.
- `l40s|h100` — either L40S or H100 (OR).
- `l40s&h100` — must have both features (AND).
- `!a10g` — anything except A10G.

See the [Slurm `--constraint` documentation](https://slurm.schedmd.com/sbatch.html#OPT_constraint) for the full syntax.

## Costs

The default deployment runs these resources 24/7 — SageMaker HyperPod does
NOT stop idle nodes. Figures are for **us-west-2** at on-demand rates, 730 h
per month.

| Resource | Configuration | Monthly |
|----------|---------------|---------|
| HyperPod controller | 1× `ml.c5.large` | ~$74 |
| HyperPod login | 1× `ml.c5.large` | ~$74 |
| HyperPod GPU worker | 1× `ml.g6e.2xlarge` (L40S) | ~$2,044 |
| HyperPod CPU worker | 1× `ml.m5.2xlarge` | ~$337 |
| FSx for Lustre | 1.2 TB PERSISTENT_2 SSD, 125 MB/s/TiB | ~$174 |
| RDS MariaDB | `db.t4g.small` + 20 GiB gp3 | ~$26 |
| NAT Gateway | 1× (hourly; no data transfer) | ~$33 |
| Secrets Manager, CloudWatch alarm | — | ~$1 |
| **Total (always-on)** | | **~$2,763** |

The GPU worker dominates the bill. To pause compute without destroying the
cluster, set `cpuWorkerCount` and/or `gpuWorkers[*].count` to `0` in
`infra/cdk.json` and redeploy `PhysaiClusterStack`; bring them back by
restoring the counts and redeploying.

Verify current pricing for your region at:
[SageMaker](https://aws.amazon.com/sagemaker/ai/pricing/) ·
[FSx Lustre](https://aws.amazon.com/fsx/lustre/pricing/) ·
[RDS MariaDB](https://aws.amazon.com/rds/mariadb/pricing/) ·
[VPC](https://aws.amazon.com/vpc/pricing/)

## Troubleshooting

### `ssh physai-login` prompts for host key authenticity

Expected on first connection, or after the cluster has been redeployed and the
login node's host key has changed. Accept once (`yes`) — the connection is
tunneled through SSM, so the host-key prompt is purely about SSH trust.

If you see a `WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED` message (not
just the "unknown host" prompt), remove the old key:

```bash
ssh-keygen -R physai-login
```

### `physai --host ... list` prints an SSH error

The CLI surfaces the full SSH stderr before its own summary. Common causes
shown in the message:

- **Host key mismatch**: `ssh-keygen -R <alias>`, retry.
- **Host not in `~/.ssh/config`**: run `infra/scripts/setup-ssh.sh` to get the snippet and add it to `~/.ssh/config` yourself.
- **Expired credentials**: `aws sso login`, refresh your profile.

### Jobs fail with "No space left on device" or FSx is nearly full

FSx fills up from accumulated container images (`/fsx/enroot/`), old
checkpoints, eval outputs, and physai working dirs. Quick wins:

```bash
physai clean --all --dry-run   # see what can be removed from /fsx/physai/
physai clean --all             # actually remove

# remove container images you no longer need:
ssh physai-login 'ls -lh /fsx/enroot/'
ssh physai-login 'rm /fsx/enroot/<name>.sqsh'
```

A CloudWatch alarm on `FreeStorageCapacity` triggers when FSx drops below
~100 GiB free. To grow FSx, increase `fsxCapacityGiB` in `infra/cdk.json` and
redeploy `PhysaiInfraStack` — FSx for Lustre supports live capacity
increases with no downtime (only scaling up).

### `cdk destroy` fails because subnets/SGs have dependencies

FSx or RDS is still holding ENIs. Use `infra/scripts/cleanup.sh` — the printed
sequence deletes FSx/RDS before destroying `PhysaiInfraStack`.

### Deployment stuck in `ROLLBACK_COMPLETE` or `ROLLBACK_FAILED`

Any CloudFormation-level failure during the initial CREATE leaves the stack
in `ROLLBACK_COMPLETE` (when rollback finished) or `ROLLBACK_FAILED` (when
rollback itself hit an error — common for `PhysaiInfraStack` because it has
retained resources like FSx and RDS that rollback can't auto-delete). CDK
won't let you re-run `cdk deploy` against a stack in either state. Use
`infra/scripts/cleanup-failed-stacks.sh` to remove the failed stack and any
resources it retained.

Common cause: insufficient AWS quota. Example (VPC limit reached during
`PhysaiInfraStack` CREATE):

```
❌  PhysaiInfraStack failed: ... ROLLBACK_FAILED:
"The maximum number of VPCs has been reached."
```

Fix:

```bash
infra/scripts/cleanup-failed-stacks.sh    # confirms + removes the failed stack
# request a quota increase, or free up unused resources, then:
npx cdk deploy --all --require-approval never
```

## Tearing Down

```bash
infra/scripts/cleanup.sh             # prints commands; review and run each
```

The script prints concrete commands (with resolved resource IDs) in the
correct order:

1. `cdk destroy PhysaiClusterStack` — releases cluster ENIs.
2. Delete FSx, RDS (disables RDS deletion protection first).
3. Delete the S3 data bucket (emptied first).
4. Disable termination protection on `PhysaiInfraStack`.
5. `cdk destroy PhysaiInfraStack`.
6. Optional: force-delete the Secrets Manager secret to bypass its recovery window.

Review each command before running. The script does not execute anything.
