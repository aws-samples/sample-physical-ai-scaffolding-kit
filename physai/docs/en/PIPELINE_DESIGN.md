# Platform Architecture

This document describes the internal architecture of the physai platform — storage design, job orchestration, infrastructure, and cost model. It is intended for platform maintainers and operators.

For developing your own pipeline (container definitions, config format, entrypoint contracts), see [PIPELINE_DEVELOP.md](PIPELINE_DEVELOP.md). For CLI command reference, see [PHYSAI_CLI.md](PHYSAI_CLI.md). For CDK stack details, see [INFRA.md](INFRA.md).

## 1. System Overview

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

## 2. Storage Architecture

Two-tier model: **S3** is the permanent store, **FSx for Lustre** is fast working storage.

### S3 (permanent)

All pipeline inputs and outputs are durably stored here.

```
s3://<bucket>/
├── raw/                    # HDF5 demos uploaded by users
├── datasets/               # Published LeRobot v2.1 datasets
├── checkpoints/            # Published model checkpoints
└── results/                # Published evaluation metrics and videos
```

### FSx for Lustre (working)

Shared by all cluster nodes at GB/s throughput. Temporary — cleaned up after each run.

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

### Local NVMe

Fast local storage on GPU worker nodes (`/opt/dlami/nvme`). Used for temporary augmented HDF5 (600GB+) that never touches `/fsx`.

### Data flow

1. User uploads raw demo directory to `s3://bucket/raw/<name>/` → auto-imported to `/fsx/raw/<name>/` (lazy-load on first access)
2. Pipeline stages read/write on `/fsx` at Lustre speed
3. Registration stage publishes final results to S3 via explicit `aws s3 cp`
4. Raw HDF5 deleted from `/fsx/raw/` after conversion. User can re-import from S3 if needed.
5. For retraining from a published dataset, `physai train` stages it from S3 to `/fsx/datasets/`

`/fsx/raw/` has a Data Repository Association (auto-import only) linked to `s3://bucket/raw/`. Users upload demo directories to S3 (each demo set is a directory of HDF5 files); contents appear under `/fsx/raw/<name>/` via lazy-load on first access. All other `/fsx/` directories have no S3 link. The registration stage publishes final results from `/fsx` to S3 via explicit `aws s3 cp`.

### Storage budget per run

| Data | Size | Lifecycle |
|------|------|-----------|
| Raw HDF5 (100 episodes, dual camera) | ~600GB | Deleted after conversion |
| LeRobot dataset (H.264 compressed) | ~5-10GB | Deleted after published to S3 |
| Checkpoints (3B model, 3 saves) | ~10-15GB | Deleted after published to S3 |
| Eval logs + metrics | ~1GB | Deleted after published to S3 |
| Container squashfs images | ~40GB | Persistent on `/fsx` |

FSx starts at 1.2TB. Supports live capacity increases in 2.4TB increments (no downtime, increase only). CloudWatch alarm on `FreeStorageCapacity` warns before it fills up.

## 3. Slurm Job Chain

`physai run` submits one Slurm job per stage, linked by `--dependency=afterok`:

```bash
RUN_ID=run-20260415-155400
JOB1=$(sbatch --parsable --job-name=physai/run/$RUN_ID/convert  convert.sh)
JOB2=$(sbatch --parsable --job-name=physai/run/$RUN_ID/train    --dependency=afterok:$JOB1 train.sh)
JOB3=$(sbatch --parsable --job-name=physai/run/$RUN_ID/eval     --dependency=afterok:$JOB2 eval.sh)
```

All jobs share a run ID. If any step fails, downstream jobs are cancelled. `physai cancel` on any job cancels all jobs sharing the run ID.

The `register` stage shown elsewhere in this document is planned but not yet implemented; current pipelines stop after `eval`.

If a container image is currently being built (`physai build` in progress), the pipeline automatically adds the build job as a dependency — you can kick off `physai run` immediately after starting `physai build`.

## 4. Data Augmentation

When augmentation is enabled, the orchestrator runs augmentation and conversion as a single Slurm job on the same GPU node. The augmented HDF5 is written to local NVMe (not `/fsx`), then conversion reads from local NVMe and writes to `/fsx`. The augmented HDF5 — which can be 600GB+ — never touches shared storage and is automatically cleaned up when the job ends.

## 5. Visual Evaluation via DCV

`physai eval --visual` streams a rendered simulation viewport to the developer's browser via NICE DCV:

```bash
$ physai eval --visual --config examples/so101-gr00t/configs/so101_pickorange_gr00t-n1.6.yaml \
  --checkpoint run-20260430-011618

Submitted 1 stage(s): eval
  Run ID:     run-20260515-030000
  Reconnect:  physai logs 123

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 Visual evaluation is ready on node ip-10-0-12-47.

 1) In a second terminal, open the SSM tunnel and KEEP IT RUNNING:

    aws ssm start-session \
      --target sagemaker-cluster:xxxxxxxxxxxx_gpu-workers-i-0123456789abcdef0 \
      --document-name AWS-StartPortForwardingSession \
      --parameters '{"portNumber":["8443"],"localPortNumber":["8443"]}' \
      --region us-west-2

 2) Open in your browser:

    https://localhost:8443/#console

 3) Accept the self-signed cert on first connect.

 4) Sign in with:

    Username: ubuntu
    Password: xK9mP2qL7nR4vT8w

 Session closes automatically when the job ends (`physai cancel 123`).
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

[eval] round 1/20 starting...
```

### How it works

The DCV `console` session is permanent — `dcvserver` auto-creates it at boot,
attached to the Xorg display that GDM3 brings up under `ubuntu`'s graphical
PAM session (auto-login). Per-job setup just rotates `ubuntu`'s PAM password
to a fresh OTP and prints the connect block.

1. The eval stage's sbatch adds `dcv` to its `--constraint` so Slurm only schedules it on GPU nodes that have DCV configured (the `dcv` feature is registered alongside the GPU type by `register_slurm_features.sh`).
2. Before `srun`, the sbatch acquires a flock on `/fsx/physai/dcv-claims/<host>.lock` (one visual session per node at a time). A second `--visual` job blocks on flock until the first releases (or until `--visual-timeout`, default 1 hour).
3. The sbatch sources `dcv_session_setup.sh`:
   - Resolves the `sagemaker-cluster:<cluster-id>_<group>-<instance-id>` SSM target from `/opt/ml/config/resource_config.json` + IMDSv2.
   - Generates a one-time password and sets it on the `ubuntu` account via `chpasswd`.
   - Prints the full connect block (SSM tunnel command + browser URL + credentials).
4. `srun --container-image=... eval.sh --visual` runs IsaacSim without `--headless`, rendering to Xorg `:0`. DCV captures `:0` and streams it to port 8443.
5. The developer runs the SSM tunnel in a second terminal, opens the URL, accepts the self-signed cert, and signs in.
6. On job exit (normal or `physai cancel`), the sbatch `EXIT TERM` trap runs `dcv_session_teardown.sh` which rotates `ubuntu`'s password to a random unguessable value. The `console` session itself stays running for the next job; only fresh logins are blocked. (An already-connected browser tab keeps streaming until the user closes it.) The kernel releases the flock automatically when sbatch exits.

### Infrastructure

- GPU workers run **GDM3 + GNOME** (lifecycle: `install_gdm.sh`) with auto-login as `ubuntu` and screen-lock disabled via dconf. GDM owns Xorg with the NVIDIA driver and headless `DFP-{0..3}` virtual display heads — this is the supported recipe for data-center GPUs (per AWS NICE DCV TAM Runbook). IsaacSim renders into this Xorg session.
- `dcvserver` runs as an always-on systemd service (lifecycle: `install_dcv.sh`), ordered after `gdm3`. It auto-creates the `console` session at boot owned by `ubuntu`. `nice-dcv-gl` is **not** installed — its GL interception layer conflicts with IsaacSim's CUDA/Vulkan path; console sessions don't need it.
- The Slurm `dcv` feature is registered on GPU nodes by `register_slurm_features.sh` (alongside the GPU-type feature like `l40s` or `h100`). The pipeline appends `&dcv` to the eval stage's `--constraint` when `--visual` is set.
- DCV exclusivity uses POSIX `flock(2)` advisory locks on FSx Lustre (`/fsx/physai/dcv-claims/<host>.lock`), held inside the sbatch for the job's lifetime. The kernel releases on any exit, so no stale-claim cleanup is needed. FSx is mounted with `flock` (not `localflock`).
- IAM policy grants `s3:GetObject` on `arn:aws:s3:::dcv-license.<region>/*` for automatic EC2 licensing.
- No security group changes — SSM port-forwarding needs no inbound rules.

## 6. Experiment Tracking (MLflow) — planned, not yet implemented

Once implemented, each completed run will log to SageMaker MLflow:

| Category | What's logged |
|----------|--------------|
| Parameters | model, dataset, max_steps, batch_size, augmentation config |
| Metrics | Training loss (per step), eval success rate |
| Artifacts | Checkpoint path (S3), evaluation videos (S3), run_config.yaml |
| Tags | Run ID, model type, task name, robot |

## 7. HyperPod Cluster

| Node | Instance | Role |
|------|----------|------|
| Login | ml.c5.large | SSH entry, MLflow client |
| Controller | ml.c5.large | Slurm scheduler |
| GPU workers | ml.g6e.2xlarge (1x L40S 48GB) | Augmentation, training, evaluation |
| CPU workers | ml.m5.2xlarge | Conversion, validation, registration |

GPU and CPU partitions run fixed worker counts configured in `infra/cdk.json` (see [INFRA.md](INFRA.md)). HyperPod does not auto-scale — change counts and redeploy `PhysaiClusterStack` to add or remove workers.

**Applying system-level changes to running nodes**: lifecycle scripts only
run on first node provisioning, so existing nodes don't automatically pick
up edits under `infra/lifecycle/`. Three options, from least to most invasive:

- **In-place re-run**: `infra/scripts/run-lifecycle.sh --all` packages the
  updated scripts and dispatches via SSM to every node, including the
  controller. Scripts self-guard by node type and are idempotent. This is
  the common case and the only way to apply changes to the controller (which
  can't be replaced).
- **Replace**: for worker/login nodes only, `npx cdk deploy
  PhysaiClusterStack` (uploads new scripts to S3) followed by `scontrol
  update node=X state=fail reason="Action:Replace"` on the login node causes
  HyperPod to reprovision the node from the new scripts.
- **Full cluster-stack redeploy** (last resort): `npx cdk destroy
  PhysaiClusterStack && npx cdk deploy PhysaiClusterStack`. Slow (~25 min),
  running jobs are lost, but safe — `PhysaiClusterStack` is stateless by
  design; `PhysaiInfraStack` (FSx, RDS, S3 data bucket) is untouched. Use
  when the cluster is wedged badly enough that neither of the above is
  recovering it, or when the lifecycle tarball has outgrown the SSM size
  limit `run-lifecycle.sh` operates under.

`UpdateClusterSoftware` only reprovisions when the AMI changes; it cannot
force lifecycle script re-execution on an existing AMI. See
[DEPLOYMENT.md](DEPLOYMENT.md#applying-lifecycle-script-changes-to-a-running-cluster)
for full workflows.

## 8. Cost Model

All cluster nodes run 24/7 — HyperPod does not stop idle instances. A default deployment (1x GPU worker, 1x CPU worker, both always on) costs roughly **$2,700/month** in us-west-2, dominated by the GPU worker (~$2,000/month for a single `ml.g6e.2xlarge`).

Scale cost by setting worker counts in `infra/cdk.json`:

- Idle (no workers): ~$310/month (controller + login + FSx + RDS + NAT + small services).
- Each additional `ml.g6e.2xlarge` GPU worker: ~$2,000/month.
- Each additional `ml.m5.2xlarge` CPU worker: ~$340/month.

## 9. References

- [AWS Sample: Embodied AI Platform](https://github.com/aws-samples/sample-embodied-ai-platform)
- [AWS Sample: Physical AI Scaffolding Kit](https://github.com/aws-samples/sample-physical-ai-scaffolding-kit)
- [SageMaker HyperPod Documentation](https://docs.aws.amazon.com/sagemaker/latest/dg/sagemaker-hyperpod.html)
- [LeRobot Dataset Format](https://huggingface.co/docs/lerobot/lerobot-dataset-v3)
