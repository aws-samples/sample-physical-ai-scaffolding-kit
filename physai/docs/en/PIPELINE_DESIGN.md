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

## 5. Visual Evaluation via DCV — partially implemented

The CLI accepts `--visual` and forwards it to `eval.sh` (which omits `--headless` so Isaac Sim renders), but the surrounding session management — allocating a DCV session on the GPU node, printing the SSM port-forward command, cleaning up on job exit — is not yet automated. The end-to-end UX described below is the target.

`physai eval --visual` streams a rendered simulation viewport to the developer's browser via NICE DCV:

```bash
$ physai eval --visual --config examples/so101-gr00t/configs/so101_pickorange_gr00t-n1.6.yaml \
  --checkpoint run-20260430-011618

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
[DEPLOYMENT.md](DEPLOYMENT.md#applying-lifecycle-script-changes-to-a-running-cluster-advanced)
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
