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

Lifecycle scripts under `infra/lifecycle/` run when HyperPod first provisions
a node. If you edit them (or pull upstream changes), **existing nodes don't
automatically pick up the new scripts** — HyperPod has already moved past the
"initial provisioning" step.

There are two things you may want:

1. **Apply the new scripts to existing nodes right now.**
2. **Make sure future node replacements / scale-outs also use the new scripts.**

Do either or both depending on your need.

#### Re-run scripts in place

Use `infra/scripts/run-lifecycle.sh`. It packages `infra/lifecycle/` into a
tarball and dispatches via SSM to the target nodes — so it works even if
Slurm, SSH, or `/fsx` is broken on the cluster.

```bash
# Re-run the full lifecycle everywhere (controller, login, all workers)
infra/scripts/run-lifecycle.sh --all

# Preview targets without executing
infra/scripts/run-lifecycle.sh --all --dry-run

# Only one instance group
infra/scripts/run-lifecycle.sh --group gpu-workers

# One specific script on one node (fastest iteration loop while editing a script)
infra/scripts/run-lifecycle.sh --node ip-10-0-2-124 --script register_slurm_features.sh
```

Each lifecycle script auto-detects the node type and self-guards — running
"all scripts on all nodes" is safe; scripts that don't apply to a given node
exit 0 with a clear "skipped" message. The scripts are also idempotent: the
"already installed" fast path is hit on re-runs, so the normal `--all` run
finishes in seconds.

Per-node logs are written under `/tmp/physai-lifecycle-runs/<timestamp>/` for
later inspection. The summary at the end of each run prints that path.

#### Also update S3 for future replacements

`run-lifecycle.sh` ships your local copy of `infra/lifecycle/` to the nodes,
but it does NOT update the copy in S3 that HyperPod uses for *new* nodes.
When you're happy with the changes, also run:

```bash
npx cdk deploy PhysaiClusterStack   # re-uploads scripts to S3 under a new hashed prefix
```

This ensures that if a node is later replaced (e.g., you run `scontrol update
node=<name> state=fail reason="Action:Replace"`, or HyperPod auto-replaces an
unhealthy node, or you scale up), the new node provisions with the updated
scripts.

#### Replacing nodes instead of in-place re-runs

If you'd rather have HyperPod provision fresh nodes from scratch, you can
still use the replace workflow — it just requires `cdk deploy` first so the
new nodes pick up your changes:

```bash
npx cdk deploy PhysaiClusterStack
# from the login node (requires Slurm admin privileges):
scontrol update node=<node-name> state=fail reason="Action:Replace"
```

This works for worker and login nodes. The **controller cannot be replaced
this way** — use `run-lifecycle.sh --group controller-machine` to re-run
its lifecycle scripts in place.

#### Last resort: destroy and redeploy `PhysaiClusterStack`

If the cluster is wedged badly enough that neither `run-lifecycle.sh` nor
node replacement is getting it into a good state — or `run-lifecycle.sh`
refuses to run because `infra/lifecycle/` has grown past the SSM payload
limit — destroy `PhysaiClusterStack` and redeploy it:

```bash
npx cdk destroy PhysaiClusterStack     # ~10 min
npx cdk deploy PhysaiClusterStack      # ~15 min
```

It's slow (~25 min) and all running jobs are lost. But it's safe:
`PhysaiClusterStack` is stateless by design (IAM role, HyperPod cluster,
lifecycle bucket), and `PhysaiInfraStack` — which holds every piece of
persistent state (FSx, RDS, S3 data bucket) — is untouched.

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
