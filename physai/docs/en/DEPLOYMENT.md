# Deployment

## Prerequisites

- An AWS account with configured credentials (e.g., AWS SSO or an administrator profile in `~/.aws/config`)
- Sufficient service quotas in the target region:
  - HyperPod cluster and the instance types you plan to use (controller + login are `ml.c5.large`, GPU defaults to `ml.g6e.2xlarge`, CPU is `ml.m5.2xlarge`)
  - VPC quota: the stack creates one VPC with a NAT gateway and several subnets
- Local tools:
  - Node.js 20+ and `npm` (for CDK)
  - Python 3.12+ and `pip`
  - AWS CLI v2
  - [Session Manager plugin for AWS CLI](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html)
  - `rsync` and `ssh` (pre-installed on macOS/Linux)

## Configuration

Configure sizing in the `physai/infra/cdk.json` context:

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

`gpuWorkers` is a list. Adding another entry lets you run a different GPU type alongside the default. Each entry becomes a separate Slurm instance group that can be targeted by group name via Slurm constraints. Example with two GPU types:

```json
"gpuWorkers": [
  { "name": "gpu-workers-l40s", "instanceType": "ml.g6e.2xlarge", "count": 2 },
  { "name": "gpu-workers-h100", "instanceType": "ml.p5.48xlarge", "count": 1 }
]
```

`cpuWorkerType` / `cpuWorkerCount` configure a single CPU instance group. The controller and login nodes are fixed at `ml.c5.large × 1` and cannot be configured here.

### Requesting Instance Limit Increases and Reserving Instances

You may need to request a quota increase for the instances you want to use. Check that the current limit for the instance type matches the number you expect to need, and request an increase if necessary. **Approval may take time depending on the instance type and quantity.**

To request a limit increase, follow these steps. **Make sure you are signed in to the correct AWS account.**

1. Go to <https://console.aws.amazon.com/servicequotas/>
1. Select `AWS services` from the left menu
1. Search for and select `Amazon SageMaker`
1. Type `for cluster usage` in the search field and select the instance type you want to use from the results
1. Click the `Request increase at account level` button
1. Enter the desired value in `Increase quota value` and click `Request`

**Note:** This is a limit increase request and does not guarantee that the instances will be available.

## Deploy

Deployment takes approximately 20 minutes to complete.

```bash
cd physai/infra
npm install
npx cdk bootstrap
npx cdk deploy --all --require-approval never
```

**Two CloudFormation stacks are created:**

| Stack | Contents | Termination Protection |
|-------|----------|------------------------|
| `PhysaiInfraStack` | VPC, S3 data bucket, FSx, RDS (accounting), Secrets Manager | ON |
| `PhysaiClusterStack` | HyperPod cluster, IAM execution role, lifecycle scripts bucket | OFF |

To deploy individually:

```bash
npx cdk deploy PhysaiInfraStack
npx cdk deploy PhysaiClusterStack
```

### What Gets Deployed

```
┌──────────────────────────────────────────────────────────────────────┐
│  PhysaiInfraStack (stateful; retained on stack deletion)             │
│                                                                      │
│   VPC  (10.0.0.0/16, 2 AZs)                                         │
│   ├── Public subnets + NAT gateway + Internet gateway                │
│   ├── Private subnets                                                │
│   └── S3 gateway VPC endpoint                                        │
│                                                                      │
│   S3 data bucket     s3://<clusterName>-data-<account>               │
│   FSx for Lustre     1.2 TB PERSISTENT_2, DRA → s3://.../raw/        │
│   RDS MariaDB        db.t4g.small  (Slurm accounting)                │
│   Secrets Manager    DB credentials                                  │
└──────────────────────────────────────────────────────────────────────┘
          │ Exports VPC / subnets / SG / FSx / RDS / Secret
          ▼
┌──────────────────────────────────────────────────────────────────────┐
│  PhysaiClusterStack (stateless; safe to destroy and recreate)        │
│                                                                      │
│   S3 lifecycle scripts bucket                                        │
│   IAM execution role                                                 │
│   SageMaker HyperPod cluster                                         │
│   ├── controller-machine  ml.c5.large × 1  (Slurm scheduler)        │
│   ├── login-group         ml.c5.large × 1  (SSH entry point)        │
│   ├── gpu-workers         ml.g6e.2xlarge   (configurable)            │
│   └── cpu-workers         ml.m5.2xlarge    (configurable)            │
│   All nodes mount /fsx                                               │
│                                                                      │
│   CloudWatch alarm   FSx FreeStorageCapacity                         │
└──────────────────────────────────────────────────────────────────────┘
```

S3 layout used by the pipeline:

```
s3://<clusterName>-data-<account>/
└── raw/        # Raw HDF5 demos. Auto-imported to /fsx/raw/ on access via DRA.
```

FSx layout (shared mount at `/fsx/` on all cluster nodes):

```
/fsx/
├── raw/            # Lazy-loaded from s3://.../raw/ via DRA
├── datasets/       # LeRobot datasets
├── checkpoints/    # Training checkpoints
├── evaluations/    # Evaluation outputs
├── enroot/         # Container .sqsh images
└── physai/         # CLI working state (builds, logs, sync directories)
```

### Applying Lifecycle Script Changes to a Running Cluster (Advanced)

`npx cdk deploy PhysaiClusterStack` uploads edits under `infra/lifecycle/` to S3. Lifecycle scripts only run when a node is first created, so existing nodes are not affected by the upload. To apply new scripts to worker or login nodes, replace the node:

```bash
# From the login node (requires Slurm admin privileges)
scontrol update node=<node-name> state=fail reason="Action:Replace"
```

HyperPod destroys the node and provisions a new instance that runs the updated lifecycle scripts.

The controller node cannot be replaced this way. To apply lifecycle changes on the controller, SSH/SSM into the controller and re-run the relevant script manually (scripts are idempotent and safe to re-run). If that is not feasible, destroy and redeploy `PhysaiClusterStack` as a last resort.

## Accessing the Cluster

The login node has no public IP. Access is via SSH tunneled through AWS SSM.

```bash
# cd to physai/ first:
# With no options, uses ~/.ssh/id_rsa.pub, id_ed25519.pub, or id_ecdsa.pub
# Options: infra/scripts/setup-ssh.sh --key ~/.ssh/mykey.pub --profile myprofile --region us-west-2

infra/scripts/setup-ssh.sh
```

What the script does:

1. Retrieves the cluster name from `PhysaiClusterStack`
2. Looks up the login node instance
3. Uploads your public key to `/home/ubuntu/.ssh/authorized_keys` via SSM
4. Prints an SSH config snippet to add to `~/.ssh/config`

Add the snippet to `~/.ssh/config` and test:

```bash
ssh physai-login
```

You will be prompted to confirm the host key on the first connection. The ProxyCommand tunnels through SSM, so no security group changes are needed.

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
