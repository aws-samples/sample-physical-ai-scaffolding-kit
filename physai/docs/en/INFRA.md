# Infrastructure (CDK)

Deploys the physai platform on AWS using two CDK stacks (TypeScript).

## Stack Overview

| Stack | Purpose | Stateful | Termination Protection |
|-------|---------|----------|----------------------|
| **PhysaiInfraStack** | Networking, storage, database | Yes | ON |
| **PhysaiClusterStack** | HyperPod cluster, IAM, monitoring | No | OFF |

PhysaiInfraStack resources are long-lived — they persist across cluster rebuilds. PhysaiClusterStack is safe to destroy and recreate without losing data.

## PhysaiInfraStack

### VPC

- CIDR: `10.0.0.0/16`
- 2 AZs (`maxAzs: 2`), one public and one private subnet per AZ. HyperPod and FSx run in a single AZ (`privateSubnets[0]`); the second AZ exists only because RDS's DB subnet group requires subnets in at least two AZs. RDS itself is single-AZ (`multiAz: false`).
- Internet gateway + 1 NAT gateway (for private subnet outbound)
- Security group: self-referencing ingress (all protocols) for cluster + FSx communication
- S3 gateway VPC endpoint on the private route table

### S3

- **Data bucket** (`<clusterName>-data-<account>-<region>`): permanent storage for raw demos, datasets, checkpoints, results

### FSx for Lustre

- 1.2 TB default (configurable via `fsxCapacityGiB`), PERSISTENT_2, SSD, 125 MB/s/TiB throughput
- Deployed in the private subnet
- Data Repository Association: auto-import from `s3://<data-bucket>/raw/` → `/fsx/raw/`

### RDS (Slurm Accounting)

- MariaDB on `db.t4g.small`, single-AZ, gp3 storage
- Private subnet group (no public access)
- Security group: inbound TCP 3306 from cluster security group only
- Database: `slurm_acct_db`
- Credentials stored in Secrets Manager (auto-generated password, rotation not enabled by default)
- Used by `slurmdbd` on the HyperPod controller for `sacct` job history

### Exports to PhysaiClusterStack

- VPC ID, private subnet ID, security group ID
- FSx DNS name, mount name
- Data bucket name/ARN
- RDS endpoint
- Secrets Manager secret ARN (for DB password)

### Outputs

- `DataBucketName` — the S3 data bucket. Users query it when uploading raw data via S3 (see USER_MANUAL.md → Raw Data & S3 Auto-import). Exported as `${stackName}-DataBucketName`.

## PhysaiClusterStack

Depends on PhysaiInfraStack.

### Lifecycle Scripts

- **Lifecycle scripts bucket** (`<clusterName>-lifecycle-<account>-<region>`): populated via `BucketDeployment` from `infra/lifecycle/`
- CDK generates `physai-config.json` at deploy time (RDS endpoint, Secrets Manager ARN from PhysaiInfraStack) and deploys it alongside the scripts
- HyperPod downloads scripts from this bucket during node provisioning

### IAM

- **Execution role** (`<clusterName>-ExecutionRole`): assumed by `sagemaker.amazonaws.com`
  - `AmazonSageMakerClusterInstanceRolePolicy` (managed)
  - EC2 networking permissions (create/delete network interfaces)
  - S3 access to data bucket and lifecycle scripts bucket
  - FSx describe
  - Secrets Manager read for the RDS secret (controller fetches DB password at boot)

### HyperPod Cluster

- Orchestrator: `Slurm` with `SlurmConfigStrategy: Merge`
- **Cluster name**: `<baseClusterName>-<stackGuid8>` where `baseClusterName`
  comes from `cdk.json` context (default `physai-cluster`) and `stackGuid8`
  is the first 8 characters of the PhysaiClusterStack's CloudFormation stack
  GUID. The GUID is stable across stack updates but changes on
  destroy+redeploy, so every fresh deployment gets a new `ClusterName` in
  Slurm accounting — new jobs start from job ID 1 and the `sacct` default
  view is clean. Old clusters' accounting history remains queryable via:

    ```bash
    sacctmgr list clusters
    sacct --clusters=<old-cluster-name>   # or --clusters=all
    ```

- Fixed instance groups:
  - `controller-machine`: ml.c5.large × 1, NodeType: Controller
  - `login-group`: ml.c5.large × 1, NodeType: Login
- Configurable instance groups (from CDK context):
  - GPU workers: each gets NodeType: Compute, PartitionNames: ["gpu"]
  - CPU workers: NodeType: Compute, PartitionNames: ["cpu"]
- All groups mount FSx at `/fsx` via `FsxLustreConfig`
- All groups use the lifecycle scripts S3 URI

### CloudWatch

- Alarm on FSx `FreeStorageCapacity` < threshold

### Outputs

- `ClusterName` — the HyperPod cluster name (suffixed with the stack GUID). Used by `setup-ssh.sh` and other tooling to locate the cluster. Exported as `${stackName}-ClusterName`.

## Configuration

`cdk.json` context:

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

Users adjust `gpuWorkers` to add/remove GPU instance groups. Examples:

```json
// Single GPU type
"gpuWorkers": [
  { "name": "gpu-workers", "instanceType": "ml.g6e.2xlarge", "count": 2 }
]

// Mixed GPU types
"gpuWorkers": [
  { "name": "gpu-workers-l40s", "instanceType": "ml.g6e.2xlarge", "count": 1 },
  { "name": "gpu-workers-a10g", "instanceType": "ml.g5.2xlarge", "count": 1 }
]

// Large-scale training
"gpuWorkers": [
  { "name": "gpu-workers-h100", "instanceType": "ml.p5.48xlarge", "count": 4 }
]
```

Controller and login nodes are fixed at ml.c5.large × 1 each.

## Lifecycle Scripts

The `infra/lifecycle/` directory is deployed to S3 via `BucketDeployment`. Scripts run during node provisioning.

Every lifecycle script sources `_lib.sh`, which auto-detects the node type from `/opt/ml/config/resource_config.json` (populates `$NODE_TYPE` as `controller` / `login` / `compute`) and exposes `require_node_type <type>` for scripts that only apply to specific node types. The orchestrator runs every script on every node; each script self-guards.

| Script | Runs on | Purpose |
|--------|---------|---------|
| `on_create.sh` | All | Entry point, calls `lifecycle_script.py` |
| `lifecycle_script.py` | All | Orchestrator: runs every lifecycle script in order (each self-guards by node type) |
| `_lib.sh` | All | Shared helpers: node-type detection + `require_node_type` guard |
| `create_fsx_dirs.sh` | Controller | Create `/fsx/{raw,datasets,checkpoints,evaluations,physai}` directories |
| `start_slurm.sh` | All | Start `slurmctld` (controller) or `slurmd` (compute/login), disable the other |
| `configure_slurm_accounting.sh` | Controller | Set up `slurmdbd` + RDS connection for `sacct` |
| `install_docker.sh` | All | Docker with containerd on NVMe |
| `install_enroot_pyxis.sh` | All | Enroot + Pyxis + Vulkan ICD hook + NGX patch |
| `configure_slurm_cgroup.sh` | Controller + Compute | Enable cgroup process tracking for `scancel` |
| `register_slurm_features.sh` | Compute | Install systemd `.service` + `.path` units that self-register the node's Slurm `Feature` (e.g. `l40s`) via `scontrol update`. The `.path` unit watches `/var/spool/slurmd/conf-cache/slurm.conf` (rewritten by slurmd on every `scontrol reconfigure` in configless mode), so features are re-applied after any reconfigure — necessary because `scontrol update` features don't survive reconfigure in slurmctld's memory. |
| `install_xorg.sh` | Compute (GPU only) | Xorg for IsaacSim headless rendering |

### Authoring lifecycle scripts: HyperPod timing notes

One asymmetry in HyperPod's behavior matters when you write a lifecycle script that interacts with `slurmctld` from a compute node.

`slurmctld` only recognizes nodes that appear as `NodeName=...` lines in `slurm.conf`. HyperPod owns those lines and writes them on the controller — but the timing differs between cluster create and instance-group add:

- **Initial cluster create.** The `NodeName` line for each worker is written before that worker's lifecycle scripts run. A compute-node script can issue `scontrol update NodeName=<self> ...` immediately and the controller will recognize the node.
- **UpdateCluster (new instance group added to a live cluster).** The new node bootstraps and runs its lifecycle scripts *first*. HyperPod writes the corresponding `NodeName` line on the controller several minutes *later*, after the node reaches `InService`. During the lifecycle window, `slurmctld` doesn't know the node exists and rejects any `scontrol update` referring to it.

Practical guidance:

1. **Don't fail the lifecycle on this.** A compute-node script that needs `slurmctld` to recognize the node should attempt the operation, but if it doesn't succeed within a short retry window, log a warning and `exit 0`. Failing the lifecycle fails the node, which triggers a CloudFormation rollback.
2. **Use `slurm.conf` writes as your eventual-consistency signal.** In configless mode, `slurmd` rewrites `/var/spool/slurmd/conf-cache/slurm.conf` on every `scontrol reconfigure` — including the one HyperPod issues after writing the new `NodeName` line. A systemd `.path` unit watching that file is a reliable trigger to retry the operation once the node is finally known to `slurmctld`.

`register_slurm_features.sh` follows both rules — copy its pattern when adding similar slurmctld-dependent steps.

### Slurm Accounting Setup

`configure_slurm_accounting.sh` runs on the controller:

1. Reads RDS endpoint and Secrets Manager secret ARN from `physai-config.json` (deployed alongside lifecycle scripts, contains only non-secret values)
2. Fetches DB password from Secrets Manager via AWS CLI (password held in memory, never stored on disk in plaintext)
3. Writes `/opt/slurm/etc/slurmdbd.conf` (chmod 600)
4. Appends accounting settings to `slurm.conf` (idempotent)
5. Starts `slurmdbd`, runs `scontrol reconfigure`
6. Registers the cluster with `sacctmgr`

CDK generates `physai-config.json` at deploy time and uploads it to the lifecycle scripts bucket via `BucketDeployment`. It contains the RDS endpoint and secret ARN — the DB password is always fetched from Secrets Manager at runtime per AWS Well-Architected security best practices.

## Deployment

```bash
cd infra
npm install
npx cdk bootstrap   # first time only
npx cdk deploy --all
```

To update lifecycle scripts on existing nodes **without replacing them**, use
`infra/scripts/run-lifecycle.sh`:

```bash
# Re-run the full lifecycle on all cluster nodes
infra/scripts/run-lifecycle.sh --all

# Just one node type
infra/scripts/run-lifecycle.sh --group gpu-workers

# One specific script on one node
infra/scripts/run-lifecycle.sh --node ip-10-0-2-124 --script register_slurm_features.sh

# Dry run
infra/scripts/run-lifecycle.sh --all --dry-run
```

The script packages `infra/lifecycle/` into a base64 tarball and dispatches
via SSM (`AWS-StartNonInteractiveCommand`) to each node, so it works even if
SSH, Slurm, or `/fsx` is broken. Each lifecycle script self-guards by node
type, so running "all scripts on all nodes" is safe — the ones that don't
apply exit 0 with a clear "skipped" message. Per-node logs are written under
`/tmp/physai-lifecycle-runs/<timestamp>/`.

For a full redeploy (e.g., to upload new lifecycle scripts to S3 so
newly-replaced nodes pick them up):

```bash
npx cdk deploy PhysaiClusterStack   # re-uploads scripts to S3 under a new hashed prefix and calls UpdateCluster
# Then on the cluster, for each compute/login node to refresh:
# scontrol update node=<node> state=fail reason="Action:Replace"
```

The lifecycle scripts are deployed to S3 under a content-hashed prefix (e.g.,
`s3://bucket/lifecycle/<hash>/`), so any change to the scripts changes
`SourceS3Uri` on the cluster and triggers CloudFormation to call
`UpdateCluster`. Without this, replaced nodes would pull the previously cached
scripts — HyperPod does not re-fetch from S3 on node replacement alone.

**Note**: The controller node cannot be replaced via `scontrol update ... state=fail`. Use `run-lifecycle.sh --node <controller-hostname>` to re-run the lifecycle on the controller in place.
