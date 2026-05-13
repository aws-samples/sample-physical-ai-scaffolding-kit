# Physical AI Pipeline Platform

A cloud-native pipeline platform on AWS for robot learning workflows — from raw
demos to evaluated policies. Developers submit pipeline jobs from their laptop
via the `physai` CLI, which orchestrates containerized workloads on a SageMaker
HyperPod Slurm cluster.

## What physai Does

physai is a CLI that runs on your laptop. It submits containerized robot-
learning pipelines — data conversion, validation, training, evaluation — to a
SageMaker HyperPod Slurm cluster over SSH. The cluster holds persistent shared
storage (FSx for Lustre) and long-lived accounting history (RDS). By default
the submitted job's log streams to your terminal and survives Ctrl-C (the
remote job keeps running and you can reconnect). Pass `-n` / `--no-stream` to
submit and return immediately.

## Sample

- Robot: SO-101
- Sim environment: LeIsaac (PickOrange, LiftCube)
- Model: GR00T N1.6

## Architecture at a Glance

```
Developer laptop                      AWS
────────────────                      ────────────────────────────────
physai CLI ───── SSH (SSM) ─────────► HyperPod cluster (login node)
                                      ├── Controller (Slurm scheduler + slurmdbd)
                                      ├── GPU workers  (g6e / L40S by default)
                                      └── CPU workers  (m5 by default)
                                      ──────────────────────────────
                                      FSx for Lustre (/fsx) — working storage
                                      S3 data bucket        — permanent store
                                      RDS MariaDB           — Slurm accounting
```

See the design docs below for the full story.

## Directory Structure

```
physai/
├── README.md                     # You are here
├── docs/
│   ├── DEPLOYMENT.md             # ▶ How to deploy (start here)
│   ├── USER_MANUAL.md            # How to use the platform
│   ├── PIPELINE-DESIGN.md        # Architecture & design (developers)
│   ├── PHYSAI-DESIGN.md          # CLI internals (developers)
│   └── INFRA.md                  # CDK stacks (developers)
├── infra/                        # CDK project
│   ├── bin/app.ts                # Entry point
│   ├── lib/
│   │   ├── infra-stack.ts        # VPC, S3, FSx, RDS, Secrets Manager
│   │   └── cluster-stack.ts      # HyperPod cluster, IAM, lifecycle bucket
│   ├── lifecycle/                # Node provisioning scripts (run on HyperPod nodes)
│   ├── scripts/
│   │   ├── setup-ssh.sh          # Upload SSH key to login node via SSM
│   │   ├── cleanup.sh            # Print teardown commands (manual review)
│   │   └── cleanup-failed-stacks.sh   # Clean up never-successfully-created stacks
│   └── cdk.json
├── cli/                          # physai CLI (Python, installed locally)
│   └── physai/
├── examples/
│   └── so101-gr00t/              # Phase 1: LeIsaac + SO-101 + GR00T N1.6
│       ├── project.yaml          # Shared config for containers
│       ├── containers/
│       │   ├── leisaac-runtime/       # Base: IsaacSim + LeIsaac (no GR00T)
│       │   ├── leisaac-gr00t-n1.6/    # Eval runtime: leisaac-runtime + GR00T N1.6
│       │   └── gr00t-n1.6-trainer/    # GR00T N1.6 fine-tuning
│       ├── configs/              # run_config.yaml files (per task)
│       └── model_configs/        # Per-model, per-robot config files
```

## Documentation

| Doc | For |
|-----|-----|
| [docs/en/DEPLOYMENT.md](docs/en/DEPLOYMENT.md) | Step by step deployment |
| [docs/en/RUN_SAMPLE.md](docs/en/RUN_SAMPLE.md) | Execute the sample project |
| [docs/en/PHYSAI_CLI.md](docs/en/PHYSAI_CLI.md) | Model Developers — CLI reference |
| [docs/en/PIPELINE_DEVELOP.md](docs/en/PIPELINE_DEVELOP.md) | Model Developers — Build your own pipeline project |
| [docs/en/PIPELINE-DESIGN.md](docs/en/PIPELINE_DESIGN.md) | Platform Developers — Project structure for pipeline |
| [docs/en/INFRA.md](docs/en/INFRA.md) | Platform Developers — CDK stack layout and lifecycle scripts |
| [docs/en/STATUS.md](docs/en/STATUS.md) | Phase 1 scope (LeIsaac + SO-101 + GR00T N1.6) and implementation status |

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
