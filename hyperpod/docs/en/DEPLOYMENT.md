# Deployment

This sample uses CDK to provision AWS resources. When deploying resources with CDK, you need to have AWS credentials with sufficient permissions.

## 1. Prerequisites

The following environments have been verified:

- Node.js v22.18.0
- npm 10.9.3
- aws-cli 2.32.21
- Python 3.14 (Lambda)
- Docker 25.0.8

## 2. Deployment

Set the following environment variables to specify your AWS credentials and region for CDK deployment. This sample has been tested in us-east-1 and us-west-2. Set `AWS_DEFAULT_REGION` to the region you want to use.

```bash
export AWS_ACCESS_KEY_ID=
export AWS_SECRET_ACCESS_KEY=
export AWS_DEFAULT_REGION=us-east-1
```

### 2.1. CDK Setup

If CDK is not installed, refer to [Getting started with the AWS CDK](https://docs.aws.amazon.com/cdk/v2/guide/getting-started.html) to install it.

### 2.2. Clone the Sample

Clone the sample code from GitHub.

```bash
git clone https://github.com/aws-samples/sample-physical-ai-scaffolding-kit.git
cd sample-physical-ai-scaffolding-kit/hyperpod
```

### 2.3. Install Node Modules

Install the libraries required for this CDK stack.

```bash
npm install
```

### 2.4. CDK Bootstrap

Prepare the environment required for CDK deployment. You can skip this step if you have already run it at least once in the same region.

```bash
cdk bootstrap
```

### 2.5. Configuration

You can configure several settings for the architecture deployed by CDK. The configuration file is located at [hyperpod/cdk.json](/hyperpod/cdk.json), and the configurable section is shown below.
The initial configuration does not include worker group settings. Since worker groups typically use GPU instances, deploying without them first prevents the entire deployment from failing if instance allocation is not available. Worker group deployment is covered in a later step.

To make changes, run `vim cdk.json` and edit using the command-line editor.

```json
"config": {
  "StackName": "PASK",  // Change this to modify the CloudFormation stack name
  "Cluster": {
    "Name": "pask-cluster",  // Amazon SageMaker HyperPod cluster name
    "ControllerGroup": {
      "Name": "controller-group",
      "Count": 1,
      "InstanceType": "ml.c5.large"
    },
    "LoginGroup": {
      "Name": "login-group",
      "Count": 1,
      "InstanceType": "ml.c5.large"
    },
    "WorkerGroup": []
  },
  "ClusterVPC": {
    "SubnetAZ": "",  // Specify a particular AZ in advance if needed
    "UseFlowLog": false  // Set to true to enable VPC Flow Logs
  },
  "Lustre": {
    "S3Prefix": "",  // Specify the S3 bucket path for the Lustre link target if needed
    "FileSystemPath": "/s3link"  // The path on the node will be `/fsx/<value specified here>`
  }
}
```

#### 2.5.1. Specifying the Cluster Availability Zone

By default, an AZ that supports g5, g6, g6e, and g7e instances is automatically selected.

[Custom resource for AZ selection](/hyperpod/lib/lambda/custom-resources/subnet-selector/index.py)

If you need to use other specific instance types, find the appropriate AZ using the following command and set it in `ClusterVPC.SubnetAZ` in cdk.json.

```bash
aws ec2 describe-instance-type-offerings --location-type availability-zone --filters Name=instance-type,Values=p4d.24xlarge
```

### 2.6. Deploy the Stack

Run the following command to build the environment. You will be prompted to confirm permissions -- enter `y` to proceed. This command creates all resources at once, so it may take several tens of minutes to complete.

```sh
cdk deploy
```

On successful deployment, you will see output similar to the following. You can also view this output in the AWS Management Console by opening CloudFormation, selecting the `PASK` stack, and checking the `Outputs` tab.

```bash
Outputs:
PASK.BucketDataBucketName = S3 bucket name for storing data
PASK.ClusterAZNameParameterStore = Parameter Store name containing the AZ where the cluster is deployed
PASK.ClusterClusterAvailabilityZone = AZ where the cluster is deployed
PASK.HyperPodClusterExecutionRoleARN = Role ARN for the cluster nodes
PASK.HyperPodClusterId = Cluster ID
PASK.HyperPodClusterSecurityGroup = Security group used by the cluster
PASK.HyperPodClusterSubnet = Subnet ID where the cluster is deployed
PASK.HyperPodLifeCycleScriptBucketName = S3 bucket for lifecycle scripts
PASK.HyperPodLoginGroupName = Login group name of the cluster
PASK.Region = Region where resources are deployed
```

To retrieve this information from the cluster, use the following command. (Replace the region and stack name with the ones you are actually using.)

```bash
export AWS_DEFAULT_REGION=us-east-1
aws cloudformation describe-stacks --stack-name PASK --query "Stacks[0].Outputs"
```

At this point, all the basic resources are ready.

### 2.7. Initial Cluster Setup

The deployment has provisioned the cluster on HyperPod. This section explains how to SSH into the cluster.

First, use the [SSH setup script](https://docs.aws.amazon.com/sagemaker/latest/dg/sagemaker-hyperpod-run-jobs-slurm-access-nodes.html) to configure SSH access from your local machine.

```bash
wget https://raw.githubusercontent.com/awslabs/awsome-distributed-training/refs/heads/main/1.architectures/5.sagemaker-hyperpod/easy-ssh.sh -O easy-ssh.sh
chmod +x easy-ssh.sh
```

**Note:** You need to set AWS credentials before running `easy-ssh.sh`.

During the process, you will be asked whether to add an entry to `~/.ssh/config` and whether to create an SSH key. Answering `yes` will simplify future logins.

`easy-ssh.sh` takes the value of `PASK.HyperPodLoginGroupName` and `PASK.HyperPodClusterId` as arguments. With the default settings, the command is:

```bash
./easy-ssh.sh -c login-group pask-cluster
```

On successful SSH connection, you will see output similar to:

```bash
Now you can run:

$ ssh pask-cluster

Starting session with SessionId: xxxxxxxx
#
```

This is an SSH session as root on the login node, so type `exit` to disconnect.

After exiting, use the `ssh pask-cluster` command shown during the script execution to reconnect.

**Note:** The SSH configuration added to `~/.ssh/config` by `easy-ssh.sh` uses ProxyCommand with [AWS Systems Manager Session Manager](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager.html) to establish the SSH connection. Therefore, **you need to set AWS credentials when using SSH**.

```bash
Host pask-cluster
    User ubuntu
    ProxyCommand sh -c "aws ssm start-session --target sagemaker-cluster:xxxxxxx_controller-group-i-xxxxxxxx --document-name AWS-StartSSHSession --parameters 'portNumber=%p'"
```

Session Manager sessions time out after 20 minutes (default) of inactivity. To increase the timeout, refer to the [AWS Systems Manager documentation](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-preferences-timeout.html).

On successful SSH connection, you will be logged into the login node as the ubuntu user:

```bash

...omitted...

You're on the login
Controller Node IP: 10.1.xxx.xxx
Login Node IP: 10.1.xxx.xxx
Instance Type: ml.c5.large
ubuntu@ip-10-1-155-217:~$
```

Set the permissions on the S3-linked Lustre storage directory so the ubuntu user can write to it.

```bash
sudo chmod -R 777 /fsx/s3link
```

Test by writing a file and verifying it appears in S3.

```bash
touch /fsx/s3link/test.txt
```

Open the [Management Console](https://console.aws.amazon.com/s3/buckets), select the bucket starting with `pask-bucketdata`, and verify that the file you just created exists.

Once verified, exit the login node with `exit`.

The remaining steps are performed in your local terminal.

### 2.8. Adding Worker Groups

To add worker groups, you need to edit cdk.json and redeploy.
Follow the steps below to add additional worker groups.

#### Requesting Instance Limit Increases and Reserving Instances

You may need to request a quota increase for the instances you want to use. Check that the current limit for the instance type matches the number you expect to need, and request an increase if necessary. **Approval may take time depending on the instance type and quantity.**

To request a limit increase, follow these steps. **Make sure you are signed in to the correct AWS account.**

1. Go to <https://console.aws.amazon.com/servicequotas/>
1. Select `AWS services` from the left menu
1. Search for and select `Amazon SageMaker`
1. Type `for cluster usage` in the search field and select the instance type you want to use from the results
1. Click the `Request increase at account level` button
1. Enter the desired value in `Increase quota value` and click `Request`

**Note:** This is a limit increase request and does not guarantee that the instances will be available.

If the instances for the worker group cannot be allocated, the deployment will fail. For GPU instance types in particular, you need to reserve instances using [Amazon SageMaker HyperPod flexible training plans](https://aws.amazon.com/blogs/aws/meet-your-training-timelines-and-budgets-with-new-amazon-sagemaker-hyperpod-flexible-training-plans/). Refer to the procedures in [Amazon SageMaker HyperPod flexible training plans](https://aws.amazon.com/blogs/aws/meet-your-training-timelines-and-budgets-with-new-amazon-sagemaker-hyperpod-flexible-training-plans/) for details.

**Note:** Training plans cannot be cancelled once purchased. Double-check the Target and Instance Type to avoid mistakes.

If using **Amazon SageMaker HyperPod flexible training plans**, search for `reserved capacity across training plans per Region` on the quota page and request a limit increase for the applicable instances.

### 2.9. Deploy CDK to Create Worker Groups

Add the worker group configuration to `cdk.json` as shown below. Specify the instance type you want to use. To add multiple worker groups, include multiple entries.

```json
"Cluster": {
  "Name": "pask-cluster",
  "ControllerGroup": {
    "Name": "controller-group",
    "Count": 1,
    "InstanceType": "ml.c5.large"
  },
  "LoginGroup": {
    "Name": "login-group",
    "Count": 1,
    "InstanceType": "ml.c5.large"
  },
  "WorkerGroup": [
    {
      "Name": "worker-group-1",
      "Count": 1,
      "InstanceType": "ml.g6e.2xlarge"
    }
  ]
},
```

After editing cdk.json, redeploy with the following command.

```bash
cdk deploy
```

On successful deployment, the worker nodes will become available.

If the deployment fails with an error similar to the following, it means the instances could not be allocated. Wait and try again later, or reserve the instances before deploying.

```bash
❌  PASK failed: ToolkitError: The stack named PASK failed to deploy: UPDATE_ROLLBACK_COMPLETE: Resource handler returned message: "Resource of type 'AWS::SageMaker::Cluster' with identifier 'Operation [UPDATE] on [arn:aws:sagemaker:us-east-1:0000000000:cluster/xxxxxxx] failed with status [InService] and error [We currently do not have sufficient capacity to launch new ml.g6e.2xlarge instances. Please try again.]' did not stabilize." (RequestToken: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxx, HandlerErrorCode: NotStabilized)
```

---

The basic Amazon SageMaker HyperPod environment is now ready.
To view the HyperPod cluster in the Management Console, go to [https://console.aws.amazon.com/sagemaker/home#/cluster-management](https://console.aws.amazon.com/sagemaker/home#/cluster-management). (If the cluster is not displayed, make sure you have selected the correct region.)

Nodes on HyperPod mount the shared Lustre storage at `/fsx`. The default user ubuntu's home directory is set to `/fsx/ubuntu` on Lustre. The Lustre path `/fsx/s3link` is [linked to the S3 bucket](https://docs.aws.amazon.com/fsx/latest/LustreGuide/create-dra-linked-data-repo.html) shown in `PASK.BucketDataBucketName`. To work with large datasets, upload data to this S3 bucket and access it from the cluster -- each node can access S3 data through Lustre, making data usage straightforward.
