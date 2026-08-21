# Workstation Image Builder

[日本語](/workstation-imagebuilder/README.ja.md) | English

A generic infrastructure for building custom AMIs with EC2 Image Builder and launching workstations via Lambda. Define your own AMI by placing component YAMLs in a directory.

The included `isaacsim6.0` / `isaacsim5.1` image definitions let you deploy NVIDIA Isaac Sim + DCV workstations out of the box.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│ Deployed by CDK                                          │
├─────────────────────────────────────────────────────────┤
│ ImageBuilder Stack                                       │
│   Image Builder pipeline × N (one per ImageNames entry)  │
│                                                         │
│ WorkstationEnv Stack                                     │
│   VPC / Security Group / S3 Files                        │
│   Launch Lambda / Connect Lambda                         │
└─────────────────────────────────────────────────────────┘
         │
         ▼ Lambda invoke
┌─────────────────────────────────────────────────────────┐
│ EC2 Instance (manual lifecycle management)               │
│ - Custom AMI                                            │
│ - Dynamic public IP                                     │
│ - S3 Files mount (UserData)                             │
└─────────────────────────────────────────────────────────┘
```

## Included Image Definitions

| Image Name | Contents |
|-----------|----------|
| [`isaacsim6.0`](lib/imagebuilder/isaacsim6.0/) | Isaac Sim 6.0.1 + Isaac Lab v3.0.0-beta2.patch1 + DCV + ROS2 Jazzy |
| [`isaacsim5.1`](lib/imagebuilder/isaacsim5.1/) | Isaac Sim 5.1.0 + Isaac Lab release/2.3.0 + DCV + ROS2 Jazzy |

For details on launching Isaac Sim / Isaac Lab and ROS2 integration, see [docs/IsaacSim.md](docs/IsaacSim.md).

## Prerequisites

- Node.js v20+
- AWS CLI configured (AdministratorAccess recommended)
- CDK bootstrapped (`npx cdk bootstrap`)
- NICE DCV client installed (for connection)

## Deploy

### 1. Deploy Infrastructure

```bash
cd workstation-imagebuilder
npm install
npx cdk deploy --all --require-approval never
```

This deploys:

- `ImageBuilder` — AMI build pipelines (one per image in `ImageNames`)
- `WorkstationEnv` — VPC, S3 Files, Launch Lambda, Connect Lambda

### 2. Build AMI

Image Builder pipelines run automatically on first deploy and when components change. AMI builds take 30-60 minutes.

After build completes, the AMI ID is stored in SSM Parameter Store (`/workstation-imagebuilder/ami-id/<image-name>`) automatically.

Monitor build progress in the AWS Console under EC2 Image Builder > Image pipelines.

### 3. Launch a Workstation

Invoke the Launch Lambda to start an instance:

```bash
aws lambda invoke \
  --function-name <WorkstationEnv.LaunchFunctionName> \
  --payload '{"imageName":"isaacsim6.0","instanceName":"my-workstation","instanceType":"g6e.4xlarge","diskSizeGb":100,"subnetId":"subnet-xxxxxxxx"}' \
  --cli-binary-format raw-in-base64-out \
  /dev/stdout
```

#### Launch Lambda Parameters

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `imageName` | Yes | — | Image name (must match an entry in `ImageNames`) |
| `instanceName` | Yes | — | Instance Name tag |
| `instanceType` | No | `g6e.4xlarge` | Instance type |
| `diskSizeGb` | No | AMI default | Root volume size (GiB). Overrides the AMI default when specified |
| `subnetId` | No | auto-selected | Subnet to launch in |

#### Launch Lambda Response

```json
{
  "instanceId": "i-0abc123def456789",
  "publicIp": "54.x.x.x",
  "waitCommand": "aws ec2 wait instance-status-ok --instance-ids i-0abc123def456789 --region ap-northeast-1",
  "connectCommand": "aws lambda invoke --function-name <connect-fn> --payload '{\"instanceId\":\"i-0abc123def456789\"}' ..."
}
```

### 4. Connect to Instance

Authentication uses DCV session tokens. No password setup required.

```bash
# 1. Wait for Status Check to pass (3-5 minutes after launch)
aws ec2 wait instance-status-ok --instance-ids <instanceId>

# 2. Get DCV connection URL via Connect Lambda
aws lambda invoke \
  --function-name <WorkstationEnv.ConnectFunctionName> \
  --payload '{"instanceId":"<instanceId>"}' \
  --cli-binary-format raw-in-base64-out \
  /dev/stdout
```

#### Connect Lambda Response

```json
{
  "instanceId": "i-0abc123def456789",
  "publicIp": "54.x.x.x",
  "dcvUrl": "https://54.x.x.x:8443/?authToken=<token>#console",
  "dcvNativeCmd": "/Applications/DCV\\ Viewer.app/Contents/MacOS/dcvviewer --certificate-validation-policy=accept-untrusted \"https://54.x.x.x:8443/?authToken=<token>#console\" &>/dev/null &",
  "ssmCommand": "aws ssm start-session --target i-0abc123def456789"
}
```

**Web browser**: Open `dcvUrl` in a browser to connect (accept the self-signed certificate warning). No username/password required.

**Native App (macOS)**: Run `dcvNativeCmd` in a terminal, or manually:

```bash
/Applications/DCV\ Viewer.app/Contents/MacOS/dcvviewer \
  --certificate-validation-policy=accept-untrusted \
  "https://<IP>:8443/?authToken=<token>#console" &>/dev/null &
```

**Native App (Windows)**:

```powershell
& "C:\Program Files\NICE\DCV\Client\bin\dcvviewer.exe" `
  --certificate-validation-policy=accept-untrusted `
  "https://<IP>:8443/?authToken=<token>#console"
```

> **Note**: The `dcv://` URI scheme is NOT supported with DCV 2025.0. Always use the `https://` URL format.

Tokens must be used to initiate a connection within 10 minutes of issuance. Once connected, the session has no time limit. Re-run the Connect Lambda if the token has expired.

#### Reconnecting

If you close the DCV client, the server-side session is preserved (open windows and running processes remain intact). To reconnect, simply re-run the Connect Lambda to get a new token and open the URL.

## Configuration

`cdk.json` config section:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `ImageNames` | `["isaacsim6.0", "isaacsim5.1"]` | Array of image names to build |
| `VpcId` | `""` (create new) | Specify to use an existing VPC |
| `DefaultInstanceType` | `g6e.4xlarge` | Default instance type for Launch Lambda |

## Adding a Custom Image

To create your own AMI, add an image definition directory.

### 1. Create Directory

Create a directory under `lib/imagebuilder/` with any name:

```bash
mkdir lib/imagebuilder/my-custom-image
```

### 2. Create config.json

```json
{
  "baseImage": "ubuntu-server-24-lts-x86",
  "buildInstanceType": "m5.xlarge",
  "diskSizeGb": 50,
  "parameters": {}
}
```

| Field | Description |
|-------|-------------|
| `baseImage` | EC2 Image Builder base image name |
| `buildInstanceType` | Instance type for AMI builds (use `g6e.4xlarge` etc. if GPU required) |
| `diskSizeGb` | Root volume size (GiB) |
| `parameters` | Parameters to pass to components (keyed by filename) |

### 3. Add Component YAMLs

Filename prefix numbers control execution order:

```
lib/imagebuilder/my-custom-image/
├── config.json
├── 01-ssm-agent.yml    ← required
├── 02-my-setup.yml
└── 03-my-app.yml
```

> **Important**: `01-ssm-agent.yml` must always be included. Without [SSM Agent](https://docs.aws.amazon.com/systems-manager/latest/userguide/ssm-agent.html), the Connect Lambda cannot issue session tokens and SSM Session Manager access will not work. You can copy the `01-ssm-agent.yml` from any of the included image definitions.

Component YAMLs follow the [EC2 Image Builder component document format](https://docs.aws.amazon.com/imagebuilder/latest/userguide/toe-use-documents.html).

### 4. Add to cdk.json

```json
{
  "config": {
    "ImageNames": ["isaacsim6.0", "my-custom-image"]
  }
}
```

### 5. Deploy

```bash
npx cdk deploy --all
```

## Instance Management

Launched instances are managed manually:

```bash
# Stop (only EBS charges apply)
aws ec2 stop-instances --instance-ids <instance-id>

# Start
aws ec2 start-instances --instance-ids <instance-id>

# Terminate
aws ec2 terminate-instances --instance-ids <instance-id>

# List running workstations
aws ec2 describe-instances \
  --filters "Name=tag:ManagedBy,Values=workstation-imagebuilder" \
            "Name=instance-state-name,Values=running,stopped" \
  --query "Reservations[].Instances[].[Tags[?Key=='Name'].Value|[0],InstanceId,State.Name,PublicIpAddress]" \
  --output table
```

### Resuming Work on a Stopped Instance

```bash
# 1. Start the instance
aws ec2 start-instances --instance-ids <instance-id>

# 2. Wait for Status Check to pass (3-5 minutes)
aws ec2 wait instance-status-ok --instance-ids <instance-id>

# 3. Get DCV connection URL via Connect Lambda
aws lambda invoke \
  --function-name <WorkstationEnv.ConnectFunctionName> \
  --payload '{"instanceId":"<instance-id>"}' \
  --cli-binary-format raw-in-base64-out \
  /dev/stdout
```

The DCV server starts automatically with the instance.

> **Note**: Stopping an instance is equivalent to an OS shutdown — windows and running processes from before the stop are not preserved. Files on disk remain intact. The public IP address may change after restart since dynamic IP is used — always use the Connect Lambda to get the current IP and token.

## Using Data

[Amazon S3 Files](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-files.html) is mounted at `/mnt/s3files`. Upload data to the S3 bucket and it becomes available on all workstations.

The bucket name is shown in the stack output `WorkstationEnv.S3FilesBucketName`.

## Troubleshooting

### Check UserData Logs

```bash
cat /var/log/workstation-bootstrap.summary
tail -n 30 /var/log/workstation-bootstrap.log
```

### Check Service Status

```bash
sudo systemctl status dcvserver --no-pager       # DCV server
sudo dcv list-sessions                           # DCV sessions
snap services amazon-ssm-agent                   # SSM Agent
mount | grep s3files                             # S3 Files mount
```

### Rebuild AMI

1. Modify component YAMLs in the image directory
2. `npx cdk deploy --all` — component changes are detected and the pipeline auto-triggers
3. After build completes, the SSM Parameter AMI ID updates automatically
4. Launch new instances (existing instances are unaffected)

## Cost (us-east-1)

| Resource | Cost | Notes |
|----------|------|-------|
| EC2 Instance | Varies by instance type | On-Demand pricing, charged while running |
| EBS (gp3, 512 GiB) | ~$40.96/month | $0.08/GiB/month, charged even when stopped |
| S3 Files | S3 + EFS pricing | Pay-as-you-go |
| Lambda | Nearly free | Only runs at launch/connect time |
| Image Builder | EC2 build time only | Pipeline itself is free |

### Cost Saving Tips

- Stop instances when not in use (only EBS charges)
- Terminate unused instances
- Choose a smaller instance type appropriate for your workload

## Cleanup

```bash
# Delete all infrastructure (running instances are unaffected)
npx cdk destroy --all
```

Terminate running instances manually:

```bash
# Terminate all instances launched by the launcher
aws ec2 describe-instances \
  --filters "Name=tag:ManagedBy,Values=workstation-imagebuilder" \
  --query "Reservations[].Instances[].InstanceId" \
  --output text | xargs -n1 aws ec2 terminate-instances --instance-ids
```
