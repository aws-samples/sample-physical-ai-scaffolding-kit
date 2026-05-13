# NVIDIA IsaacSim Development Workstation

[日本語](/isaacsim-workstation/README.ja.md) | English

Deploy a GPU instance using the NVIDIA Isaac Sim Development Workstation AMI with CDK, and build a workstation accessible via remote desktop through NICE DCV.

## Architecture

- **GPU EC2 Instance**: NVIDIA Isaac Sim AMI (Ubuntu 24.04 based) + NICE DCV
- **VPC**: Create new or import existing VPC (auto-selects AZ supporting GPU instances)
- **S3 Files**: S3-backed file system with EFS compatibility (NFS mount)
- **UserData**: Automatically sets up ROS2 Jazzy + S3 Files mount

### Included in the AMI (no installation required)

<https://aws.amazon.com/marketplace/pp/prodview-bl35herdyozhw>

- NVIDIA drivers
- NICE DCV server + auto session
- NVIDIA Isaac Sim
- PyTorch
- SSM Agent

### Set up by UserData

- ROS2 Jazzy (Desktop + rosbridge)
- S3 Files (EFS) mount (`/mnt/s3files`)

## Prerequisites

- Node.js v20+
- AWS CLI configured (AdministratorAccess recommended)
- CDK bootstrapped (`npx cdk bootstrap`)
- NICE DCV client installed (for connection)
- **NVIDIA Isaac Sim AMI subscribed on AWS Marketplace**
  - Subscribe here: <https://aws.amazon.com/marketplace/pp/prodview-bl35herdyozhw>
  - Deployment will fail if the AMI is not subscribed

## Deploy

```bash
cd cdk
npm install
cdk deploy
```

### Configuration

You can customize settings in the `config` section of `cdk.json`:

| Parameter | Default | Description |
|-----------|---------|-------------|
| `StackPrefix` | `Dev` | Stack name suffix (`DevWorkstation`) |
| `VpcId` | `""` (create new) | Specify to use an existing VPC |
| `SubnetAZ` | `""` (auto-select) | Explicitly specify an AZ |
| `InstanceType` | `g6e.8xlarge` | GPU instance type |

To specify via command line:

```bash
cdk deploy -c VpcId=vpc-xxxxxxxx -c SubnetAZ=subnet-xxxxxxxx
```

### Stack Outputs

The following information is output after `cdk deploy` completes:

| Output | Description |
|--------|-------------|
| `DevWorkstation.SimulatorAZNameParameterStore` | Parameter Store name containing the VPC Id |
| `DevWorkstation.SimulatorClusterAvailabilityZone` | AZ Id where the EC2 instance is running |
| `DevWorkstation.WorkstationDCVApp` | DCV Native App URL |
| `DevWorkstation.WorkstationDCVWebURL` | DCV Web client URL |
| `DevWorkstation.WorkstationInstancePublicIP` | Instance Elastic IP |
| `DevWorkstation.WorkstationS3FilesBucketName` | S3 Files bucket name |
| `DevWorkstation.WorkstationS3FilesFileSystemId` | S3 Files file system ID |
| `DevWorkstation.WorkstationSSMSessionCommand` | Session Manager connection command (ssh) |
| `DevWorkstation.WorkstationSetPasswordCommand` | ubuntu password set command |
| `DevWorkstation.WorkstationWaitForInstanceCommand` | Status Check wait command |

To retrieve this information later, check the CloudFormation stack in the Management Console or run the following command (specify the actual region and stack name):

```bash
export AWS_DEFAULT_REGION=us-east-1
aws cloudformation describe-stacks --stack-name <your stack name> --query "Stacks[0].Outputs"
```

## Post-Deployment Steps

### Wait for Instance Status Check to Complete

After deployment, wait for instance initialization (including UserData execution) to complete. Do not set the password or connect via DCV until the EC2 instance Status Check shows `3/3 checks passed`. Run the `WaitForInstanceCommand` from the stack outputs:

```bash
aws ec2 wait instance-status-ok --instance-ids <instance-id>
```

### Connect via Session Manager (SSH Access)

Run the `WorkstationSSMSessionCommand` from the stack outputs:

```bash
aws ssm start-session --target <instance-id> --region <region>
```

You will be logged in as root. Switch to the ubuntu user:

```bash
sudo su - ubuntu
```

#### Check UserData Logs

```bash
cat /var/log/workstation-bootstrap.summary
```

If any step failed, check the detailed log:

```bash
tail -n 20 /var/log/workstation-bootstrap.log
```

### Set the ubuntu User Password

From the PC where you ran `cdk deploy`, set the password for DCV login. Use the `SetPasswordCommand` from the stack outputs:

```bash
export UBUNTU_PW="your-password-here"

# Run the SetPasswordCommand from the stack outputs as-is
aws ssm send-command --instance-ids i-0986d7fe5a672b6f5 --document-name "AWS-RunShellScript" --parameters "commands=[\"HASHED=\$(openssl passwd -6 '${UBUNTU_PW}') && sudo usermod --password \\\"\$HASHED\\\" ubuntu\"]" --region us-east-1 --output text --query "Command.CommandId"
```

### Connect via Amazon DCV

There are two ways to use Amazon DCV for remote desktop access: through a web browser with a dedicated URL, or by downloading the [DCV client](https://www.amazondcv.com/) (DCV Viewer).

#### Web Browser Access

1. Open the `WorkstationDCVWebURL` (`https://<EIP>:8443`) from the stack outputs in your browser
1. If a certificate warning appears, select "Trust and connect"
1. Log in with username: `ubuntu` and the password set above

#### Native App Access

1. Open the `WorkstationDCVApp` (`dcv://<EIP>:8443`) from the stack outputs in your browser
1. Log in with username: `ubuntu` and the password set above

## Launching Isaac Sim / Isaac Lab

After connecting to the EC2 remote desktop via DCV, open a terminal and follow the steps below to launch the applications.

### Launching Isaac Sim

<https://docs.isaacsim.omniverse.nvidia.com/5.1.0/index.html>

On first launch, various assets are loaded, which may cause the screen to appear frozen or display a "Not Responding" message. Please wait a few minutes for Isaac Sim to start.

```bash
cd ~
./IsaacSim/isaac-sim.sh
```

#### Running Samples

Try the NVIDIA Isaac Sim tutorials:
<https://docs.isaacsim.omniverse.nvidia.com/5.1.0/introduction/quickstart_isaacsim_robot.html>

### Running Isaac Lab

<https://isaac-sim.github.io/IsaacLab/main/index.html>

Next, try the Isaac Lab samples. If Isaac Sim is running, close it first.

```bash
cd ~/IsaacLab
conda activate env_isaaclab
```

To exit Isaac Lab, press `Control + C` in the terminal where it was launched.

On first launch, various assets are loaded, which may cause the screen to appear frozen or display a "Not Responding" message. Please wait a few minutes for Isaac Sim to start.

```bash
# Tutorial: Launch an empty simulation environment
./isaaclab.sh -p scripts/tutorials/00_sim/create_empty.py
```

You can run the [sample robot training](https://isaac-sim.github.io/IsaacLab/main/source/setup/installation/source_installation.html#train-a-robot) with the following command:

```bash
# Reinforcement learning: Ant locomotion training
./isaaclab.sh -p scripts/reinforcement_learning/rsl_rl/train.py --task=Isaac-Ant-v0
```

### ROS2 Integration

<https://docs.isaacsim.omniverse.nvidia.com/5.1.0/installation/install_ros.html>

To integrate Isaac Sim with ROS2, you need to set environment variables before launching Isaac Sim. If Isaac Sim is running, close it first. If the Isaac Lab conda environment is active, run `conda deactivate` first.

```bash
export ROS_DISTRO=jazzy
export RMW_IMPLEMENTATION=rmw_fastrtps_cpp
export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:/opt/IsaacSim/exts/isaacsim.ros2.bridge/jazzy/lib
```

Launch Isaac Sim with the above environment variables set. The ROS2 Bridge will fail to enable if these are not configured.

```bash
~/IsaacSim/isaac-sim.sh
```

#### Verifying the ROS2 Bridge

After Isaac Sim starts, open a separate terminal and verify that ROS2 topics are visible:

```bash
ros2 topic list
```

#### Running Tutorials

The ROS2 workspace samples introduced in the [IsaacSim ROS2 tutorial](https://docs.isaacsim.omniverse.nvidia.com/5.1.0/installation/install_ros.html#setting-up-workspaces) are pre-built and available at `~/IsaacSim-ros_workspaces`.

For detailed instructions including the TurtleBot3 sample, see:

<https://docs.isaacsim.omniverse.nvidia.com/5.1.0/ros2_tutorials/index.html>

## Using Data

This sample uses [Amazon S3 Files](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-files.html). It is mounted at `/mnt/s3files`, so you can upload the necessary data to the S3 bucket and access it using standard file operations, just like working with local files. The mounted S3 bucket is the one output as `DevWorkstation.WorkstationS3FilesBucketName` during `cdk deploy`.

For data that is accessed repeatedly, we recommend copying it from S3 to local storage.

## EC2 Instance Troubleshooting

### Check Cloud Init Logs

```bash
sudo tail -n 20 /var/log/cloud-init-output.log
```

### Check Service Status

```bash
sudo systemctl status dcvserver --no-pager       # DCV server
sudo dcv list-sessions                           # DCV sessions
snap services amazon-ssm-agent                   # SSM Agent
mount | grep s3files                             # S3 Files mount
```

### Re-run UserData

Only failed steps can be re-run (idempotent):

```bash
sudo ls -la /var/lib/workstation-bootstrap/
# Delete the marker for a specific step and re-run
sudo rm /var/lib/dcv-bootstrap/install-ros2-jazzy.done
sudo bash /var/lib/cloud/instance/scripts/part-001
```

## Cost (us-east-1)

| Resource | Cost | Notes |
|----------|------|-------|
| g6e.8xlarge (EC2) | $4.52856/hour | On-Demand pricing |
| NVIDIA Isaac Sim AMI | $0.00/hour | No software charge |
| EBS (gp3, 512 GiB) | ~$40.96/month | $0.08/GiB/month |
| Elastic IP (running) | $0.005/hour | While instance is running |
| Elastic IP (stopped) | $0.005/hour | Charged even when instance is stopped |
| S3 Files | S3 + EFS pricing | Pay-as-you-go |

### Cost Estimate (g6e.8xlarge)

- **Per hour**: ~$4.53
- **8 hours/day x 5 days**: ~$181/week
- **24/7 for a month**: ~$3,302/month (including EBS)

### Cost Saving Tips

- Stop the instance when not in use (only EBS charges apply)
- Delete the entire stack with `npx cdk destroy` after demos
- Use a smaller instance type (`g6e.4xlarge`) if sufficient

## Cleanup

```bash
cdk destroy
```

All resources have `RemovalPolicy.DESTROY` set, so S3 buckets and other resources will be completely deleted.
