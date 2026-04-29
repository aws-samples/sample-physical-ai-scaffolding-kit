# Detail of physai CLI

physai CLI is a local CLI that orchestrates workloads on a remote HyperPod cluster via SSH. No agent, no daemon, no state — all job state lives in Slurm.

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

The CLI uses subprocess `ssh`. So it inherits the user's SSH config, agent forwarding, and ProxyCommand. Zero setup beyond a working `ssh <host>`.

## 2. Configuration

`~/.physai/config.yaml`:

```yaml
host: physai-login
model_config_roots:
  - <path-to-physai>/examples/so101-gr00t/model_configs
```

> **Schema**: [`cli-config.schema.json`](../../cli/physai/schemas/cli-config.schema.json)

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

## 3. Commands

Every workload command submits a Slurm job and, by default, streams its log to the local terminal. The stream survives Ctrl-C (the remote job keeps running). Pass `-n` / `--no-stream` to submit and return immediately.

### 3.1. Job management commands

These commands are used to manage the Slurm job.

```bash
physai list   [--host HOST]
physai status <job-id> [--host HOST]
physai logs   <job-id> [--host HOST]
physai cancel <job-id> [--host HOST]
physai clean  [--older-than DAYS] [--all] [--enroot] [--dry-run] [-f] [--host HOST]
physai doctor [--host HOST]
```

#### 3.1.1. Job Metadata

Jobs are tracked entirely through Slurm. No external database.

- `--job-name`: Encodes type and name — `physai/<type>/<name>` (e.g., `physai/build/leisaac-runtime`, `physai/train/so101-liftcube-gr00t`)
- `--comment`: Free-form description up to 256 chars (e.g., `produces=/fsx/checkpoints/run-20260415-155400`). Used by `_find_active_job_producing` in `pipeline.py` to detect pending/running jobs that will create an artifact our current run needs. See the persistence caveat below.
- `--output`: Always `/fsx/physai/logs/%j.out`

`physai list` parses the job name to extract type and name.

**`--comment` persistence caveat:** Slurm keeps `--comment` in `slurmctld` memory (visible via `squeue` / `scontrol show job`) but does **not** persist it to `slurmdbd` accounting by default — so `sacct` returns an empty Comment column for completed jobs. This is the stock HyperPod behavior (no `AccountingStoreFlags=job_comment` in `slurm.conf`). Our active-job-producing check only queries `squeue`, so the default is fine for pipeline dependency detection. If a future feature needs to look up artifacts produced by completed jobs (e.g., "which past job produced `/fsx/checkpoints/X`?"), add `AccountingStoreFlags=job_comment` to the cluster's `slurm.conf` and run `scontrol reconfigure`.

#### 3.1.2. With sacct (completed jobs visible)

```bash
$ physai list
JOB_ID  TYPE   NAME                    STATE      ELAPSED  COMMENT
238     build  leisaac-runtime         RUNNING    12:34    base=nvcr.io/nvidia/cuda:12.8.1-cudnn-devel-ubuntu24.04
237     eval   so101-liftcube-gr00t    COMPLETED  16:22
236     train  so101-liftcube-gr00t    COMPLETED  3:42:10
```

The COMMENT column is populated only for pending/running jobs (from `squeue`); completed rows come from `sacct` and leave it blank unless the cluster sets `AccountingStoreFlags=job_comment` (see caveat above).

#### 3.1.3. Without sacct (only queued/running jobs)

```bash
$ physai list
JOB_ID  TYPE   NAME                    STATE      TIME     COMMENT
238     build  leisaac-runtime         RUNNING    12:34    --rebuild

(sacct not available — only active jobs shown)
```

`physai logs <job-id>` always works regardless of sacct — it tails `/fsx/physai/logs/<job-id>.out`.

#### 3.1.4. Cleanup

Build dirs (`/fsx/physai/builds/`) and log files (`/fsx/physai/logs/`) accumulate over time. `physai clean` removes old ones:

```bash
physai clean                    # remove items older than 7 days (default)
physai clean --older-than 3     # older than 3 days
physai clean --all              # remove all
physai clean --enroot           # also remove stale Enroot containers from worker nodes (leftover runtime instances from jobs that didn't exit cleanly; does NOT delete .sqsh images)
physai clean --dry-run          # show what would be removed
physai clean -f                 # skip confirmation
```

Files belonging to active jobs are never removed.

`physai cancel <job-id>` runs `scancel <job-id>`. Slurm's `--dependency=afterok` chain (set up by `physai run`) means cancelling one stage cascades: any pending downstream stages of the same run stop with reason `DependencyNeverSatisfied`.

#### 3.1.5. Doctor

`physai doctor` is a read-only health check over the login node's SSH session. It runs a fixed list of checks, each of which returns `PASS`, `FAIL`, or `WARN` with an optional message. For checks that have an auto-fix, `doctor` prompts interactively (`y/N`, default No) before applying it; after a fix, the check re-runs to confirm.

Current checks:

- **FSx directories**: `stat` each of `/fsx/{raw,datasets,checkpoints,evaluations,enroot,physai}`. Expects directory + exact mode per entry (`/fsx/enroot` is 1777, the rest are 0777). Fix: `mkdir -p` + per-dir `chmod` batched into a single remote command.
- **Slurm config drift among workers**: `sinfo -h -o "%N" -N | sort -u` → for each node, `srun -N1 -w <node> --overlap --time=0:00:30 md5sum /var/spool/slurmd/conf-cache/{slurm,cgroup,plugstack,gres,accounting}.conf`. All reachable nodes must return identical hash tuples. Unreachable nodes are WARN (not counted as drift). Fix: `scontrol reconfigure` on the login node (slurmctld pushes to all workers). **Limitation**: does not detect the case where the controller's on-disk `/opt/slurm*/etc/slurm.conf` has been edited but no reconfigure has been run — that would require SSM to the controller, which the doctor doesn't have.
- **slurmdbd reachable**: `sacct -n --parsable2 -S now-1hour -o JobID` exits 0. No auto-fix; the FAIL message points at the RDS instance, the slurmdbd credentials in Secrets Manager, and the SSM command to inspect slurmdbd on the controller.

Exits non-zero if any check is still FAIL after the user's chance to fix.

### 3.2. Data commands

These commands are used to manage the data.

```
physai ls <category> [<path>] [--host HOST]     # list remote data
physai upload <category> <local-path> [--host HOST]  # upload data to cluster
physai rm <category> <name> [-f] [--host HOST]  # remove a remote artifact
```

#### 3.2.1. physai ls

Browse remote data on the cluster. Shows human-readable sizes computed by `du -sh` per top-level entry.

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

#### 3.2.2. physai upload

Upload data to the cluster:

```bash
# Raw demos — prompts to recommend uploading to S3 first, then rsyncs to /fsx/raw/
physai upload raw /path/to/my-demo-dir/

# Pre-converted dataset → /fsx/datasets/ directly
physai upload datasets /path/to/so101_liftcube/

# Checkpoint → /fsx/checkpoints/ directly
physai upload checkpoints /path/to/checkpoint-dir/
```

`datasets` and `checkpoints` rsync directly to `/fsx/<category>/`. `raw` also
rsyncs to `/fsx/raw/`, but the CLI first recommends the S3 route and asks for
confirmation — for large raw data, S3 is usually preferred (see next section).

##### Raw Data & S3 Auto-import

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
aws s3 cp --recursive /path/to/my-demo-dir/ s3://<data-bucket>/raw/my-demo-dir/

# verify it's visible on the cluster
physai ls raw
```

#### 3.2.3. physai rm

Remove a named artifact from the cluster:

```bash
physai rm datasets so101_liftcube
physai rm checkpoints gr00t-n1.6-liftcube-30k
physai rm raw my-demo-dir
physai rm evaluations run-20260429-154500
physai rm datasets foo -f   # skip confirmation
```

Categories: `raw`, `datasets`, `checkpoints`, `evaluations`. Resolves to
`/fsx/<category>/<name>` (slashes in `<name>` are rejected to prevent path
escape).

Before deleting, `rm`:

- Probes the path: file, directory, or missing. Missing fails with a clear error.
- Asks `_find_active_job_producing` whether an active pipeline job is producing
  the same path; if yes, refuses and prints the job id so the user can
  `physai cancel` it first.
- Prints the resolved path, kind, and `du -sh` size, then prompts `[y/N]`.
  `-f` / `--force` skips the prompt.
- For `raw`, also prints a note that `/fsx/raw/` is a DRA cache from S3 — the
  local eviction is non-destructive, and the object remains available via
  lazy-reimport.

### 3.3. Pipeline commands

These commands are used to manage the pipline.

```bash
physai build   <container-folder> [--rebuild] [-n|--no-stream] [--host HOST]
physai run     --config <local-yaml> [--from STAGE] [--to STAGE] [--raw NAME] [--dataset NAME] [--checkpoint NAME] [--max-steps N] [--eval-rounds N] [--visual] [--model-config-root PATH] [-n|--no-stream] [--host HOST]
physai convert --config <local-yaml> --raw <name> [--dataset <name>] [--model-config-root PATH] [-n|--no-stream] [--host HOST]
physai train   --config <local-yaml> --dataset <name> [--max-steps N] [--model-config-root PATH] [-n|--no-stream] [--host HOST]
physai eval    --config <local-yaml> --checkpoint <name> [--eval-rounds N] [--visual] [--model-config-root PATH] [-n|--no-stream] [--host HOST]
```

`physai run` executes the stages listed in `pipeline.stages` from the config. `--from`/`--to` narrow that list to a contiguous subrange. `physai convert`, `physai train`, and `physai eval` are shortcuts for single-stage runs.

`--dataset` has two roles depending on the starting stage:

- When `convert` is the first stage to run, `--dataset <name>` sets the **output** dataset name (written to `/fsx/datasets/<name>`). If omitted, the convert stage mirrors the `--raw` name.
- When `train` or `validate` is the first stage, `--dataset <name>` names the **input** dataset at `/fsx/datasets/<name>` (which must already exist).

The runner validates each case separately via its preflight input/output checks; the same flag carries different semantics based on the stage range.

#### 3.3.1. Path Resolution

The CLI uses two kinds of references:

- **Local paths** — configs, container definitions. These are rsynced to the cluster before job submission.
- **Names** — datasets, checkpoints, raw data. These are large and live on `/fsx`. The CLI resolves names to `/fsx/` paths.

| Argument | Source | Resolution |
|----------|--------|------------|
| `--config <path>` | Local file | rsynced to cluster |
| `model.config_dir` (in yaml) | Relative name | Resolved via model config search paths, then rsynced |
| `--raw <name>` | Directory name | → `/fsx/raw/<name>/` |
| `--dataset <name>` | Directory name | → `/fsx/datasets/<name>/` |
| `--checkpoint <name>` | Directory name | → `/fsx/checkpoints/<name>/` |
| `<container-folder>` | Local directory | rsynced to cluster |

#### 3.3.2. Model config resolution

`model.config_dir` in the yaml is a relative name (e.g., `gr00t-n1.6/so101-singlecam`). The CLI searches configured model config paths to find the matching local directory, then rsyncs it to the cluster.

Search paths are configured via `--model-config-root` (per-command) or `model_config_roots` in `~/.physai/config.yaml` (default). The CLI checks each search path in order and uses the first match.

Example:

```bash
# Search path configured in ~/.physai/config.yaml
physai train --config examples/so101-gr00t/configs/so101_liftcube_gr00t-n1.6.yaml \
             --dataset so101_liftcube --max-steps 10000

# Or override per-command
physai train --config examples/so101-gr00t/configs/so101_liftcube_gr00t-n1.6.yaml \
             --model-config-root examples/so101-gr00t/model_configs \
             --dataset so101_liftcube --max-steps 10000
```

**The CLI:**

1. Reads `model.config_dir: gr00t-n1.6/so101-singlecam` from the config yaml
2. Searches model config paths, finds it at `.../model_configs/gr00t-n1.6/so101-singlecam`
3. rsyncs the config file and the resolved model config dir to `/fsx/physai/sync/<run-id>/`
4. Resolves `so101_liftcube` → `/fsx/datasets/so101_liftcube`
5. Generates sbatch with resolved cluster paths as entrypoint arguments

#### 3.3.3. Build Workflow

```bash
physai build examples/so101-gr00t/containers/leisaac-runtime
```

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

**The CLI:**

1. Read `container.yaml` from the given folder.
2. Walk up to find `project.yaml`, merge configs (container overrides project). Exactly one of `base_image` (registry image) or `base_container` (another built squashfs) must be set — if the container sets either, it wholly replaces the project's choice of base.
3. Preflight (fail fast before rsync or submit):
   - **This container** (when `--rebuild` is not set): fail if an active build job for `<name>` is already queued (a duplicate non-rebuild submission will fail inside sbatch anyway), or if `/fsx/enroot/<name>.sqsh` already exists (the user probably meant `--rebuild`).
   - **Base container** (only when `base_container` is set): look up an active build job for the base. If one exists, capture its job id to chain as an `afterok` dependency. Otherwise, the base's sqsh must already exist on disk or the build fails with a `"Build it first."` message.
4. rsync the container's `setup-hooks/`, `app/`, and the packaged `build-scripts/` to `/fsx/physai/builds/<name>-<ts>/` on the cluster.
5. Write `env.txt` (merged `env`) and `build.sbatch` into the build dir.
6. `sbatch [--dependency=afterok:<base_build_id> --kill-on-invalid-dep=yes] build.sbatch` → capture JOB_ID.
7. Unless `-n` / `--no-stream`, `tail -f /fsx/physai/logs/<JOB_ID>.out` → stream to terminal. Ctrl-C prints a reconnect hint and exits.

##### 3.3.3.1 Generated build.sbatch (example)

```bash
#!/bin/bash
#SBATCH --job-name=physai/build/leisaac-runtime
#SBATCH --comment="base=nvcr.io/nvidia/cuda:12.8.1-cudnn-devel-ubuntu24.04"
#SBATCH --partition=gpu
#SBATCH --gres=gpu:1
#SBATCH --output=/fsx/physai/logs/%j.out
set -eo pipefail
trap 'echo "\nBuild failed. Container may be left on the worker node."; echo "  Clean up: physai clean --enroot"' ERR
SECONDS=0
BUILD_DIR=/fsx/physai/builds/leisaac-runtime-20260422-080000
BUILD_NAME=leisaac-runtime-20260422-080000
SQSH=/fsx/enroot/leisaac-runtime.sqsh

if [ -f "$SQSH" ]; then
  echo "ERROR: $SQSH exists. Use --rebuild to replace."
  exit 1
fi

echo "=== init (${SECONDS}s) ==="
srun --container-image=nvcr.io/nvidia/cuda:12.8.1-cudnn-devel-ubuntu24.04 \
     --container-name=$BUILD_NAME --container-mounts=/fsx:/fsx \
     --container-remap-root \
     bash "$BUILD_DIR/build-scripts/init-env.root.sh"

echo "=== 10-system-packages.root.sh (${SECONDS}s) ==="
srun --container-name=$BUILD_NAME --container-mounts=/fsx:/fsx \
     --container-remap-root \
     bash "$BUILD_DIR/setup-hooks/10-system-packages.root.sh"

echo "=== 20-install-leisaac.sh (${SECONDS}s) ==="
srun --container-name=$BUILD_NAME --container-mounts=/fsx:/fsx \
     bash "$BUILD_DIR/setup-hooks/20-install-leisaac.sh"

# ... more hooks ...

echo "=== copy app/ (${SECONDS}s) ==="
srun --container-name=$BUILD_NAME --container-mounts=/fsx:/fsx \
     --container-remap-root \
     bash "$BUILD_DIR/build-scripts/mkdir-app.root.sh"
srun --container-name=$BUILD_NAME --container-mounts=/fsx:/fsx \
     bash "$BUILD_DIR/build-scripts/copy-app.sh"

echo "=== export squashfs (${SECONDS}s) ==="
enroot export -o "$SQSH" pyxis_${BUILD_NAME}
enroot remove -f pyxis_${BUILD_NAME}

echo "Build complete: $SQSH (${SECONDS}s)"
```

The init step loads env vars from `env.txt` into the Pyxis named container. With `--rebuild`, the pre-check is replaced with an `rm -f "$SQSH"` so pending jobs using the old image keep working until the rebuild finishes (pending jobs that depend on this rebuild via afterok wait, others use the current sqsh). The intermediate Pyxis container uses the timestamped `BUILD_NAME` to avoid collisions with running containers or concurrent builds. The final squashfs uses the stable container name.

#### 3.3.4. Run Workflow(Train / Eval)

`physai run` executes the stages listed in `pipeline.stages` from the config. `--from`/`--to` narrow that list to a contiguous subrange. `physai convert`, `physai train`, and `physai eval` are shortcuts for single-stage runs.

Stages in order: `augment`, `convert`, `validate`, `train`, `eval`, `register`. Currently implemented: `convert`, `train`, `eval`. The runner rejects pipelines that include any other stage.

| `--from` | Required args | Resolves to | Implemented? |
|----------|--------------|-------------|--------------|
| `augment` | `--raw` | `/fsx/raw/<name>/` | No (planned) |
| `convert` | `--raw` | `/fsx/raw/<name>/` | Yes |
| `validate` | `--dataset` | `/fsx/datasets/<name>/` | No (planned) |
| `train` | `--dataset` | `/fsx/datasets/<name>/` | Yes |
| `eval` | `--checkpoint` | `/fsx/checkpoints/<name>/` | Yes |
| `register` | (none — reads from previous stage outputs) | | No (planned) |

Stage-specific parameters (e.g., `max_steps`, `rounds`) come from the config's `stages.<name>` section. `--max-steps` on the CLI overrides `stages.train.max_steps`. `--eval-rounds` overrides `stages.eval.rounds`.

Run default stages from config (e.g., convert → validate → train → eval → register)

```bash
physai run --config examples/so101-gr00t/configs/so101_liftcube_gr00t-n1.6.yaml \
           --raw so101_liftcube
```

Run from train onwards

```bash
physai run --config examples/so101-gr00t/configs/so101_liftcube_gr00t-n1.6.yaml \
           --from train --dataset so101_liftcube
```

Single-stage shortcuts

```bash
# Just convert (shortcut for `run --from convert --to convert`)
physai convert --config ... --raw so101_liftcube_raw

# Just train (shortcut for `run --from train --to train`)
physai train --config examples/so101-gr00t/configs/so101_liftcube_gr00t-n1.6.yaml \
             --dataset so101_liftcube

# Just eval (shortcut for `run --from eval --to eval`)
physai eval --config examples/so101-gr00t/configs/so101_liftcube_gr00t-n1.6.yaml \
            --checkpoint gr00t-n1.6-liftcube-30k
```

##### 3.3.4.1 Steps in the command

1. Read `run_config.yaml` from the local path.
2. Determine stages to run: start from `pipeline.stages` in the config; `--from`/`--to` narrow the list to a contiguous subrange of those stages.
3. Validate that required CLI args are present for the starting stage.
4. rsync the config file and its `model.config_dir` to `/fsx/physai/sync/<run-id>/`.
5. For each stage, generate an sbatch script:
   - Partition, gres, constraint, container from `stages.<name>`.
   - `srun --container-image=/fsx/enroot/<container>.sqsh` (never a registry ref — stages only run pre-built images).
   - `--container-mounts=/fsx:/fsx,/tmp/.X11-unix:/tmp/.X11-unix`.
   - Calls the container protocol entrypoint with resolved paths.
   - Stage parameters (max_steps, rounds) from config, overridden by CLI.
   - Sets `RUN_CONFIG` (path to the synced config); `DISPLAY=${DISPLAY:-:0}` for eval.
6. Submit each stage with `sbatch --dependency=afterok:<deps> --kill-on-invalid-dep=yes`, where `<deps>` is the previous stage's job id colon-separated with the job id of any active build for this stage's container. If the container has neither a live sqsh nor an active build, fail before submitting.
7. Unless `-n` / `--no-stream`, stream each submitted job's log to the terminal in order (moving on to the next when the current one finishes). Ctrl-C detaches from the current stream (all submitted jobs keep running).

`physai convert` is equivalent to `physai run --from convert --to convert`. `physai train` is equivalent to `physai run --from train --to train`. `physai eval` is equivalent to `physai run --from eval --to eval`.

## 4.Error Handling

| Scenario | Behavior |
|----------|----------|
| SSH connection fails | Print the ssh stderr, a separator, and a hint to test with `ssh <host>` manually (host-key mismatch, missing config entry, expired credentials). |
| sbatch fails | Print Slurm error message. |
| Target `.sqsh` exists and `--rebuild` not passed, or an active build job for the same container is already queued | Error before submitting: `"already exists. Use --rebuild to replace."` or `"Build job N is already active..."`. With `--rebuild` the check is skipped (the new job will delete the sqsh at start). |
| `base_container` referenced but neither on disk nor being built | Error: `"Base container '<name>' not found at /fsx/enroot/<name>.sqsh. Build it first."` |
| Pipeline stage's `container` neither on disk nor being built | Same pattern as above. |
| run_config references unknown stage or missing `container` | Error before submitting any jobs. |
| Dataset / checkpoint path not found on cluster | Error: `"<label> not found on cluster: <path>"`. |
| Ctrl-C during log streaming | Print reconnect hint (`physai logs <job-id>`), exit 0. |
| Job fails (non-zero exit) | Shown in `physai status`; `physai logs <job-id>` reopens the full log. |

## 5. SSH Interface

All cluster interaction goes through a multiplexed SSH `Session` in `ssh.py`:

```python
class Session:
    def __init__(self, host: str)         # starts a ControlMaster (ControlPersist=10m)
    def run(self, cmd: str) -> str        # one-shot command, returns stdout
    def rsync(self, src: str, dst: str, show_progress: bool = False)
                                          # rsync -az over the control socket;
                                          # show_progress=True streams `--info=progress2` live
    def write_file(self, path, content)   # cat > <path> on remote
    def stream_log(self, job_id: str)     # streams /fsx/physai/logs/<id>.out via a Python helper
    def clone(self) -> "Session"          # reuses the same control socket
    def close(self)                       # tears down the control socket
    has_sacct: bool                       # cached capability probe
```

One `ControlMaster` per invocation keeps every subsequent `ssh`/`rsync` free of re-auth latency. The helper script at `cli/physai/log_streamer.py` is piped to a remote `python3 -` so log tailing works without installing anything on the cluster.

## 6. Package Structure

```
cli/
├── pyproject.toml
└── physai/
    ├── cli.py            # argparse dispatch
    ├── config.py         # load ~/.physai/config.yaml + --host override
    ├── schema.py         # JSON Schema validation helper
    ├── schemas/          # JSON schemas for cli-config, project, container, run-config
    ├── ssh.py            # Session (ControlMaster), rsync, stream_log
    ├── log_streamer.py   # piped to remote `python3 -` to tail job logs
    ├── build.py          # read project/container yaml, generate build.sbatch
    ├── build-scripts/    # packaged snippets shipped to the cluster per build
    │   ├── init-env.root.sh
    │   ├── mkdir-app.root.sh
    │   └── copy-app.sh
    ├── clean.py          # remove old build dirs, logs, stale enroot containers
    ├── doctor.py          # cluster health checks with interactive fixes
    ├── pipeline.py       # read run_config, generate convert/train/eval/run sbatch
    ├── data.py           # ls, upload, rm
    └── jobs.py           # list, status, logs, cancel (squeue/sacct wrappers)
```

Dependencies: `pyyaml`, `jsonschema`.

Install: `pip install -e cli/`

Entry point: `physai` (via pyproject.toml `[project.scripts]`)
