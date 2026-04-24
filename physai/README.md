# Physical AI Pipeline Platform

A cloud-native pipeline platform on AWS for robot learning workflows — from raw
demos to evaluated policies. Developers submit pipeline jobs from their laptop
via the `physai` CLI, which orchestrates containerized workloads on a SageMaker
HyperPod Slurm cluster.

## Scope

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
│   ├── USER_MANUAL.md            # ▶ How to use the platform (start here)
│   ├── PIPELINE-DESIGN.md        # Architecture & design (developers)
│   ├── PHYSAI-DESIGN.md          # CLI internals (developers)
│   └── INFRA.md                  # CDK stacks (developers)
├── infra/                        # CDK deployment (TypeScript)
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

| Directory | Deployed to |
|-----------|-------------|
| `infra/` | AWS (CloudFormation) |
| `cli/` | Developer's local machine |
| `examples/so101-gr00t/` | Built on the cluster; squashfs images land on `/fsx/enroot/` |

## Quick Start

Prerequisites: AWS credentials configured, Node.js, Python 3.12+, AWS CLI,
Session Manager plugin for AWS CLI, rsync.

```bash
# 1. Deploy infrastructure (takes ~20 min). CDK is the only step that runs
#    from infra/; all subsequent commands run from the repo root.
cd infra
npm install
npx cdk bootstrap        # first time only, per account+region
npx cdk deploy --all --require-approval never
cd ..

# 2. Grant yourself SSH access to the login node
infra/scripts/setup-ssh.sh   # uploads ~/.ssh/id_*.pub via SSM, prints SSH config snippet
# Add the snippet to ~/.ssh/config, then test: ssh physai-login

# 3. Install the physai CLI
pip install -e cli

# 4. Point the CLI at the cluster
mkdir -p ~/.physai && cat > ~/.physai/config.yaml <<EOF
host: physai-login
model_config_roots:
  - $(pwd)/examples/so101-gr00t/model_configs
EOF

# 5. Download the public Pick Orange dataset, build containers, and submit
#    the pipeline. `-n` submits without streaming logs so the commands return
#    immediately; use `physai list` / `physai logs <job-id>` to check on them.
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

The Pick Orange dataset is a public LeRobot v2.1 dataset (60 episodes,
~698 MB) published alongside the LeIsaac simulation environment. See
[docs/USER_MANUAL.md](docs/USER_MANUAL.md#getting-a-dataset) for details and
citation.

For the full user guide, see **[docs/USER_MANUAL.md](docs/USER_MANUAL.md)**.

## Tearing Down

```bash
infra/scripts/cleanup.sh            # prints the exact commands to run
# ...review and run each command...
```

If a deployment failed mid-create and left a stack in `ROLLBACK_COMPLETE` or
`ROLLBACK_FAILED`:

```bash
infra/scripts/cleanup-failed-stacks.sh   # interactive cleanup
```

## Documentation

| Doc | For |
|-----|-----|
| [docs/USER_MANUAL.md](docs/USER_MANUAL.md) | End users — how to use the CLI, run pipelines, manage data |
| [docs/STATUS.md](docs/STATUS.md) | Phase 1 scope (LeIsaac + SO-101 + GR00T N1.6) and implementation status |
| [docs/PIPELINE-DESIGN.md](docs/PIPELINE-DESIGN.md) | Developers — platform architecture and design rationale |
| [docs/PHYSAI-DESIGN.md](docs/PHYSAI-DESIGN.md) | Developers — CLI internals |
| [docs/INFRA.md](docs/INFRA.md) | Developers — CDK stack layout and lifecycle scripts |
