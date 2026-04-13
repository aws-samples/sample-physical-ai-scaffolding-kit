# physai-pipeline-poc

Cloud-native pipeline for robot learning workflows on AWS — from raw demos to evaluated policies.

See [docs/PIPELINE-DESIGN.md](docs/PIPELINE-DESIGN.md) for the full architecture.

## POC Scope

- Robot: SO-101
- Sim environment: LeIsaac (PickOrange, LiftCube)
- Model: GR00T N1.6

## Directory Structure

```
physai-pipeline-poc/
├── docs/
│   └── PIPELINE-DESIGN.md          # Architecture and design document
│
├── infra/                           # CDK stack — deploys the pipeline system
│   ├── bin/
│   │   └── app.ts                   # CDK app entry point
│   ├── lib/
│   │   ├── cluster-stack.ts         # HyperPod cluster (login, controller, GPU/CPU workers)
│   │   ├── storage-stack.ts         # FSx for Lustre + S3 bucket + DRA
│   │   ├── registry-stack.ts        # ECR repositories
│   │   ├── tracking-stack.ts        # SageMaker MLflow tracking server
│   │   └── monitoring-stack.ts      # CloudWatch alarms (FSx capacity, budget)
│   ├── lifecycle/                    # HyperPod lifecycle scripts
│   │   └── on_create.sh             # Installs DCV, configures Slurm GRES (dcv:1), etc.
│   ├── cdk.json
│   ├── tsconfig.json
│   └── package.json
│
├── cli/                             # physai CLI
│   └── physai                       # Pipeline submission, status, cancel, compare
│
├── examples/
│   └── so101-gr00t/                 # POC: LeIsaac + SO-101 + GR00T N1.6
│       ├── README.md                # Setup instructions: build containers, deploy configs
│       ├── configs/
│       │   ├── so101_pickorange_gr00t.yaml
│       │   └── so101_liftcube_gr00t.yaml
│       ├── model_configs/
│       │   └── gr00t-n1.6/
│       │       └── so101/
│       │           ├── modality.json
│       │           └── modality_config.py
│       ├── containers/
│       │   ├── pins.env             # Pinned commits shared by all containers
│       │   ├── leisaac-runtime/
│       │   │   ├── Dockerfile
│       │   │   ├── eval.sh          # /app/eval.sh entrypoint
│       │   │   └── augment.sh       # /app/augment.sh entrypoint
│       │   ├── so101-converter/
│       │   │   ├── Dockerfile
│       │   │   ├── convert.sh       # /app/convert.sh entrypoint
│       │   │   ├── validate.sh      # /app/validate.sh entrypoint
│       │   │   └── hdf5_to_lerobot.py
│       │   └── gr00t-trainer/
│       │       ├── Dockerfile
│       │       └── train.sh         # /app/train.sh entrypoint
│       └── patches/                 # LeIsaac patches (e.g., annotate_demos.py fix)
│           └── leisaac_annotate_demos.patch
│
└── README.md                        # This file
```

### What goes where

| Directory | What it contains | Deployed to |
|-----------|-----------------|-------------|
| `infra/` | CDK stack — generic pipeline infrastructure | AWS (CloudFormation) |
| `cli/` | physai CLI — pipeline orchestration | HyperPod login node |
| `examples/so101-gr00t/` | POC-specific configs, containers, patches | `/fsx/` on HyperPod + ECR |
| `docs/` | Design document | Not deployed |

## Implementation Phases

1. **Infrastructure + Training** — CDK stack, `gr00t-trainer` container, manual sbatch
2. **Evaluation** — `leisaac-runtime` container, headless + DCV visual eval, MLflow registration
3. **Conversion + Validation** — `so101-converter` container, convert → validate → train chain
4. **Full Pipeline + CLI** — `physai` CLI, Slurm job chain, end-to-end test
5. **Augmentation (stretch)** — Isaac Lab Mimic via `leisaac-runtime`
