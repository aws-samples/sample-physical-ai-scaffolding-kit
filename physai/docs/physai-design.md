# physai CLI Design

A local CLI that orchestrates workloads on a remote HyperPod cluster via SSH. No agent, no daemon, no state — all job state lives in Slurm.

## 1. Architecture

```
Developer machine                          HyperPod cluster
──────────────────                         ─────────────────
physai CLI ──── SSH ────────────────────→  Login node
  │                                          │
  ├── rsync files to cluster                 ├── sbatch (submit jobs)
  ├── ssh: submit sbatch                     ├── squeue/sacct (query jobs)
  ├── ssh: tail -f log (stream)              └── scancel (cancel jobs)
  └── Ctrl-C detaches (job keeps running)
                                           Worker nodes (GPU/CPU)
                                             └── Slurm jobs run here
```

The CLI uses subprocess `ssh` — not paramiko — so it inherits the user's SSH config, agent forwarding, and ProxyCommand. Zero setup beyond a working `ssh <host>`.

## 2. Commands

Every workload command submits a Slurm job, streams its log to the local terminal, and survives Ctrl-C (the remote job keeps running).

### Workload commands

```
physai build <container-folder> [--rebuild] [--host HOST]
physai run   --config <local-yaml> [--from STAGE] [--to STAGE] [--raw NAME] [--dataset NAME] [--checkpoint NAME] [--max-steps N] [--eval-rounds N] [--visual] [--model-config-root PATH] [--host HOST]
physai train --config <local-yaml> --dataset <name> [--max-steps N] [--model-config-root PATH] [--host HOST]
physai eval  --config <local-yaml> --checkpoint <name> [--eval-rounds N] [--visual] [--model-config-root PATH] [--host HOST]
```

`physai run` executes the stages listed in `pipeline.stages` from the config. `--from`/`--to` override the range. `physai train` and `physai eval` are shortcuts for single-stage runs.

Stages in order: `augment`, `convert`, `validate`, `train`, `eval`, `register`

| `--from` | Required args | Resolves to |
|----------|--------------|-------------|
| `augment` | `--raw` | `/fsx/raw/<name>` |
| `convert` | `--raw` | `/fsx/raw/<name>` |
| `validate` | `--dataset` | `/fsx/datasets/<name>` |
| `train` | `--dataset` | `/fsx/datasets/<name>` |
| `eval` | `--checkpoint` | `/fsx/checkpoints/<name>` |
| `register` | (none — reads from previous stage outputs) | |

Stage-specific parameters (e.g., `max_steps`, `rounds`) come from the config's `stages.<name>` section. `--max-steps` on the CLI overrides `stages.train.max_steps`. `--eval-rounds` overrides `stages.eval.rounds`.

### Data commands

```
physai ls <category> [<path>] [--host HOST]     # list remote data
physai upload <category> <local-path> [--host HOST]  # upload data to cluster
```

Categories: `raw`, `datasets`, `checkpoints`

### Job management commands

```
physai list   [--host HOST]
physai status <job-id> [--host HOST]
physai logs   <job-id> [--host HOST]
physai cancel <job-id> [--host HOST]
physai clean  [--older-than DAYS] [--all] [--dry-run] [-f] [--host HOST]
```

## 3. Path Resolution

The CLI uses two kinds of references:

- **Local paths** — configs, container definitions. These are rsynced to the cluster before job submission.
- **Names** — datasets, checkpoints, raw data. These are large and live on `/fsx`. The CLI resolves names to `/fsx/` paths.

| Argument | Source | Resolution |
|----------|--------|------------|
| `--config <path>` | Local file | rsynced to cluster |
| `model.config_dir` (in yaml) | Relative name | Resolved via model config search paths, then rsynced |
| `--raw <name>` | Name | → `/fsx/raw/<name>` |
| `--dataset <name>` | Name | → `/fsx/datasets/<name>` |
| `--checkpoint <name>` | Name | → `/fsx/checkpoints/<name>` |
| `<container-folder>` | Local directory | rsynced to cluster |

### Model config resolution

`model.config_dir` in the yaml is a relative name (e.g., `gr00t-n1.6/so101-singlecam`). The CLI searches configured model config paths to find the matching local directory, then rsyncs it to the cluster.

Search paths are configured via `--model-config-root` (per-command) or `model_config_roots` in `~/.physai/config.yaml` (default). The CLI checks each search path in order and uses the first match.

Example:

```bash
# Search path configured in ~/.physai/config.yaml
physai train --config examples/so101-gr00t/configs/so101_liftcube_gr00t.yaml \
             --dataset so101_liftcube --max-steps 10000

# Or override per-command
physai train --config examples/so101-gr00t/configs/so101_liftcube_gr00t.yaml \
             --model-config-root examples/so101-gr00t/model_configs \
             --dataset so101_liftcube --max-steps 10000
```

The CLI:
1. Reads `model.config_dir: gr00t-n1.6/so101-singlecam` from the config yaml
2. Searches model config paths, finds it at `.../model_configs/gr00t-n1.6/so101-singlecam`
3. rsyncs the config file and the resolved model config dir to `/fsx/physai/sync/<run-id>/`
4. Resolves `so101_liftcube` → `/fsx/datasets/so101_liftcube`
5. Generates sbatch with resolved cluster paths as entrypoint arguments

## 4. Data Commands

### physai ls

Browse remote data on the cluster:

```bash
$ physai ls datasets
so101_liftcube/          455M
so101_pickorange/        1.2G

$ physai ls checkpoints
gr00t-n1.6-liftcube-30k/   12G
test-run-1/                 4.1G

$ physai ls raw
pickorange.hdf5          580G
```

Implementation: `ssh <host> ls -lh /fsx/<category>/` (or `du -sh` for directories).

### physai upload

Upload data to the cluster:

```bash
# Raw demos — prompts to recommend uploading to S3 first, then rsyncs to /fsx/raw/
physai upload raw /path/to/demos.hdf5

# Pre-converted dataset → /fsx/datasets/ directly
physai upload datasets /path/to/so101_liftcube/

# Checkpoint → /fsx/checkpoints/ directly
physai upload checkpoints /path/to/checkpoint-dir/
```

All categories rsync to `/fsx/<category>/`. For `raw`, the CLI first recommends
uploading to S3 (`s3://<data-bucket>/raw/`) so the Data Repository Association
auto-imports lazily — this avoids duplicating large files on FSx. The user can
decline the recommendation to proceed with direct rsync.

## 5. Configuration

`~/.physai/config.yaml`:

```yaml
host: physai-login
s3_bucket: physai-pipeline-poc-588738614703
model_config_roots:
  - ~/projects/physai-pipeline-poc/examples/so101-gr00t/model_configs
```

Cluster paths are fixed by convention:

| Path | Purpose |
|------|---------|
| `/fsx/physai/logs/` | Job logs: `<job-id>.out` |
| `/fsx/physai/builds/` | Build working dirs |
| `/fsx/physai/sync/` | rsynced configs and model configs |
| `/fsx/enroot/` | Exported squashfs images |
| `/fsx/datasets/` | Datasets (referenced by name) |
| `/fsx/checkpoints/` | Checkpoints (referenced by name) |
| `/fsx/raw/` | Raw data (DRA from S3) |
| `/fsx/evaluations/` | Evaluation outputs |

## 6. Job Metadata

Jobs are tracked entirely through Slurm. No external database.

- `--job-name`: Encodes type and name — `physai/<type>/<name>` (e.g., `physai/build/leisaac-runtime`, `physai/train/so101-liftcube-gr00t`)
- `--comment`: Free-form description up to 256 chars (e.g., `dataset=so101_liftcube steps=10000`)
- `--output`: Always `/fsx/physai/logs/%j.out`

`physai list` parses the job name to extract type and name.

### With sacct (completed jobs visible)

```
$ physai list
JOB_ID  TYPE   NAME                    STATE      ELAPSED  COMMENT
238     build  leisaac-runtime         RUNNING    12:34    --rebuild
237     eval   so101-liftcube-gr00t    COMPLETED  16:22    checkpoint=gr00t-n1.6-liftcube-30k
236     train  so101-liftcube-gr00t    COMPLETED  3:42:10  dataset=so101_liftcube steps=10000
```

### Without sacct (only queued/running jobs)

```
$ physai list
JOB_ID  TYPE   NAME                    STATE      TIME     COMMENT
238     build  leisaac-runtime         RUNNING    12:34    --rebuild

(sacct not available — only active jobs shown)
```

`physai logs <job-id>` always works regardless of sacct — it tails `/fsx/physai/logs/<job-id>.out`.

### Cleanup

Build dirs (`/fsx/physai/builds/`) and log files (`/fsx/physai/logs/`) accumulate over time. `physai clean` removes old ones:

```bash
physai clean                    # remove items older than 7 days (default)
physai clean --older-than 3     # older than 3 days
physai clean --all              # remove all
physai clean --dry-run          # show what would be removed
physai clean -f                 # skip confirmation
```

Files belonging to active jobs are never removed.

## 7. Build Workflow

`physai build examples/so101-gr00t/containers/leisaac-runtime`

### Steps

1. Read `container.yaml` from the given folder
2. Walk up to find `project.yaml`, merge configs (container overrides project)
3. rsync container folder + `project.yaml` → `<host>:/fsx/physai/builds/<name>-<ts>/`
4. Generate `build.sbatch` from the merged config
5. Write `build.sbatch` to the build dir on the cluster
6. `ssh: sbatch` → capture JOB_ID
7. `ssh: tail -f /fsx/physai/logs/<JOB_ID>.out` → stream to terminal
8. On Ctrl-C: print reconnect hint and exit

### Generated build.sbatch (example)

```bash
#!/bin/bash
#SBATCH --job-name=physai/build/leisaac-runtime
#SBATCH --comment="base=nvcr.io/nvidia/pytorch:25.04-py3"
#SBATCH --partition=gpu
#SBATCH --gres=gpu:1
#SBATCH --output=/fsx/physai/logs/%j.out
set -eo pipefail
SECONDS=0
BUILD_DIR=/fsx/physai/builds/leisaac-runtime-20260413-153000
BUILD_NAME=leisaac-runtime-20260413-153000
SQSH=/fsx/enroot/leisaac-runtime.sqsh

if [ -f "$SQSH" ]; then
  echo "ERROR: $SQSH exists. Use --rebuild to replace."
  exit 1
fi

echo "=== 10-system-packages.root.sh (root) ==="
srun --container-image=nvcr.io/nvidia/pytorch:25.04-py3 \
     --container-name=$BUILD_NAME \
     --container-mounts=/fsx:/fsx \
     --container-remap-root \
     bash "$BUILD_DIR/setup-hooks/10-system-packages.root.sh"

echo "=== 20-install-leisaac.sh (${SECONDS}s) ==="
srun --container-name=$BUILD_NAME \
     --container-mounts=/fsx:/fsx \
     bash "$BUILD_DIR/setup-hooks/20-install-leisaac.sh"

# ... more hooks ...

echo "=== Copying app/ to /app/ (${SECONDS}s) ==="
srun --container-name=$BUILD_NAME \
     --container-mounts=/fsx:/fsx \
     bash -c "cp -r $BUILD_DIR/app/* /app/ && chmod +x /app/*.sh"

echo "=== Exporting squashfs (${SECONDS}s) ==="
enroot export -o "$SQSH" pyxis_${BUILD_NAME}

echo "Build complete: $SQSH (${SECONDS}s)"
```

The intermediate Pyxis container uses the timestamped `BUILD_NAME` to avoid collisions with running containers or concurrent builds. The final squashfs uses the stable container name.

## 8. Train / Eval / Run Workflow

```bash
# Run default stages from config (e.g., convert → validate → train → eval → register)
physai run --config examples/so101-gr00t/configs/so101_liftcube_gr00t.yaml \
           --raw so101_liftcube.hdf5

# Run from train onwards
physai run --config examples/so101-gr00t/configs/so101_liftcube_gr00t.yaml \
           --from train --dataset so101_liftcube

# Single-stage shortcuts
physai train --config examples/so101-gr00t/configs/so101_liftcube_gr00t.yaml \
             --dataset so101_liftcube
physai eval --config examples/so101-gr00t/configs/so101_liftcube_gr00t.yaml \
            --checkpoint gr00t-n1.6-liftcube-30k
```

### Steps

1. Read `run_config.yaml` from the local path
2. Determine stages to run: `pipeline.stages` from config, overridden by `--from`/`--to`
3. Validate that required CLI args are present for the starting stage
4. rsync the config file and its `model.config_dir` to `/fsx/physai/sync/<run-id>/`
5. For each stage, generate an sbatch script:
   - Partition, gres, constraint, container from `stages.<name>`
   - `--container-name=<container>` (Pyxis named container)
   - `--container-mounts=/fsx:/fsx,/tmp/.X11-unix:/tmp/.X11-unix`
   - Calls the container protocol entrypoint with resolved paths
   - Stage parameters (max_steps, rounds) from config, overridden by CLI
   - Sets `RUN_CONFIG`, `DISPLAY`, `PYTHONUNBUFFERED=1` as needed
6. Submit jobs with `--dependency=afterok` linking each stage to the previous
7. Stream log of first job, Ctrl-C detaches

`physai train` is equivalent to `physai run --from train --to train`. `physai eval` is equivalent to `physai run --from eval --to eval`.

## 9. SSH Interface

All cluster interaction goes through a single SSH module:

```python
def run(host, cmd) -> str           # ssh <host> <cmd>, return stdout
def run_stream(host, cmd)           # ssh -t <host> <cmd>, stream to terminal
def rsync(host, src, dst)           # rsync -az <src> <host>:<dst>
def tail_log(host, job_id)          # ssh <host> tail -f /fsx/physai/logs/<job_id>.out
                                    # handles Ctrl-C gracefully
```

## 10. Package Structure

```
cli/
├── pyproject.toml
└── physai/
    ├── __main__.py       # python -m physai
    ├── cli.py            # argparse dispatch
    ├── config.py         # load ~/.physai/config.yaml + --host override
    ├── ssh.py            # run, run_stream, rsync, tail_log
    ├── build.py          # read project/container yaml, generate build.sbatch
    ├── clean.py          # remove old build dirs and logs
    ├── pipeline.py       # read run_config, generate train/eval/run sbatch
    ├── data.py           # ls, upload
    └── jobs.py           # list, status, logs, cancel (squeue/sacct wrappers)
```

Dependencies: `pyyaml`. No other external dependencies.

Install: `pip install -e cli/`

Entry point: `physai` (via pyproject.toml `[project.scripts]`)

## 11. Error Handling

| Scenario | Behavior |
|----------|----------|
| SSH connection fails | Print error, suggest checking `ssh <host>` manually |
| sbatch fails | Print Slurm error message |
| Container sqsh already exists | Error with message; `--rebuild` removes it first |
| run_config references missing container | Error before submitting job |
| Dataset/checkpoint name not found | Error: `dataset 'foo' not found. Run: physai ls datasets` |
| Ctrl-C during log streaming | Print reconnect hint, exit 0 |
| Job fails (non-zero exit) | Shown in `physai status`, last lines of log printed |
