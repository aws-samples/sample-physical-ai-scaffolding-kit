# Deployment

이 sample은 CDK를 사용해 AWS resource를 provision합니다. CDK로 resource를 deploy하려면 충분한 permission을 가진 AWS credentials가 필요합니다.

## 1. Prerequisites

다음 environment에서 검증되었습니다.

- Node.js v22.18.0
- npm 10.9.3
- aws-cli 2.32.21
- Python 3.14 (Lambda)
- Docker 25.0.8

## 2. Deployment

CDK deployment에 사용할 AWS credentials와 region을 지정하려면 다음 environment variables를 설정합니다. 이 sample은 us-east-1과 us-west-2에서 테스트되었습니다. 사용할 region을 `AWS_DEFAULT_REGION`에 설정하세요.

```bash
export AWS_ACCESS_KEY_ID=
export AWS_SECRET_ACCESS_KEY=
export AWS_DEFAULT_REGION=us-east-1
```

### 2.1. CDK Setup

CDK가 설치되어 있지 않다면 [Getting started with the AWS CDK](https://docs.aws.amazon.com/cdk/v2/guide/getting-started.html)를 참고해 설치하세요.

### 2.2. Clone the Sample

GitHub에서 sample code를 clone합니다.

```bash
git clone https://github.com/aws-samples/sample-physical-ai-scaffolding-kit.git
cd sample-physical-ai-scaffolding-kit/hyperpod
```

### 2.3. Install Node Modules

이 CDK stack에 필요한 library를 설치합니다.

```bash
npm install
```

### 2.4. CDK Bootstrap

CDK deployment에 필요한 environment를 준비합니다. 같은 region에서 한 번 이상 실행한 적이 있다면 이 단계는 건너뛸 수 있습니다.

```bash
cdk bootstrap
```

### 2.5. Configuration

CDK가 deploy하는 architecture의 여러 설정을 구성할 수 있습니다. Configuration file은 [hyperpod/cdk.json](/hyperpod/cdk.json)에 있으며, 설정 가능한 section은 아래와 같습니다.
초기 configuration에는 worker group 설정이 포함되어 있지 않습니다. Worker group은 일반적으로 GPU instance를 사용하므로, 먼저 worker group 없이 deploy하면 instance allocation이 불가능할 때 전체 deployment가 실패하는 것을 피할 수 있습니다. Worker group deployment는 이후 단계에서 다룹니다.

변경하려면 `vim cdk.json`을 실행하고 command-line editor로 수정합니다.

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

#### 2.5.1. Cluster Availability Zone 지정

기본적으로 g5, g6, g6e, g7e instance를 지원하는 AZ가 자동으로 선택됩니다.

[AZ selection custom resource](/hyperpod/lib/lambda/custom-resources/subnet-selector/index.py)

다른 특정 instance type을 사용해야 한다면 다음 command로 적절한 AZ를 찾고, cdk.json의 `ClusterVPC.SubnetAZ`에 설정하세요.

```bash
aws ec2 describe-instance-type-offerings --location-type availability-zone --filters Name=instance-type,Values=p4d.24xlarge
```

### 2.6. Deploy the Stack

다음 command를 실행해 environment를 build합니다. Permission 확인 prompt가 표시되면 `y`를 입력해 진행합니다. 이 command는 모든 resource를 한 번에 생성하므로 완료까지 수십 분이 걸릴 수 있습니다.

```sh
cdk deploy
```

Deployment가 성공하면 다음과 유사한 output이 표시됩니다. AWS Management Console에서 CloudFormation을 열고 `PASK` stack을 선택한 뒤 `Outputs` tab에서도 확인할 수 있습니다.

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

Cluster에서 이 정보를 가져오려면 다음 command를 사용합니다. 실제로 사용하는 region과 stack name으로 바꿔 실행하세요.

```bash
export AWS_DEFAULT_REGION=us-east-1
aws cloudformation describe-stacks --stack-name PASK --query "Stacks[0].Outputs"
```

이 시점에서 기본 resource 준비가 완료됩니다.

### 2.7. Initial Cluster Setup

Deployment를 통해 HyperPod에 cluster가 provision되었습니다. 이 section에서는 cluster에 SSH로 접속하는 방법을 설명합니다.

먼저 [SSH setup script](https://docs.aws.amazon.com/sagemaker/latest/dg/sagemaker-hyperpod-run-jobs-slurm-access-nodes.html)를 사용해 local machine에서 SSH access를 설정합니다.

```bash
wget https://raw.githubusercontent.com/awslabs/awsome-distributed-training/refs/heads/main/1.architectures/5.sagemaker-hyperpod/easy-ssh.sh -O easy-ssh.sh
chmod +x easy-ssh.sh
```

**Note:** `easy-ssh.sh`를 실행하기 전에 AWS credentials를 설정해야 합니다.

진행 중 `~/.ssh/config`에 entry를 추가할지, SSH key를 생성할지 질문을 받습니다. `yes`로 답하면 이후 login이 더 간단해집니다.

`easy-ssh.sh`는 `PASK.HyperPodLoginGroupName`과 `PASK.HyperPodClusterId` 값을 argument로 받습니다. Default settings에서는 command가 다음과 같습니다.

```bash
./easy-ssh.sh -c login-group pask-cluster
```

SSH connection이 성공하면 다음과 유사한 output이 표시됩니다.

```bash
Now you can run:

$ ssh pask-cluster

Starting session with SessionId: xxxxxxxx
#
```

이것은 login node의 root SSH session이므로 `exit`를 입력해 disconnect합니다.

종료한 뒤 script 실행 중 표시된 `ssh pask-cluster` command로 다시 접속합니다.

**Note:** `easy-ssh.sh`가 `~/.ssh/config`에 추가한 SSH configuration은 [AWS Systems Manager Session Manager](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager.html)를 사용하는 ProxyCommand로 SSH connection을 구성합니다. 따라서 **SSH를 사용할 때도 AWS credentials를 설정해야 합니다**.

```bash
Host pask-cluster
    User ubuntu
    ProxyCommand sh -c "aws ssm start-session --target sagemaker-cluster:xxxxxxx_controller-group-i-xxxxxxxx --document-name AWS-StartSSHSession --parameters 'portNumber=%p'"
```

Session Manager session은 inactivity가 20분(default) 지속되면 timeout됩니다. Timeout을 늘리려면 [AWS Systems Manager documentation](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-preferences-timeout.html)을 참고하세요.

SSH connection이 성공하면 login node에 ubuntu user로 로그인됩니다.

```bash

...omitted...

You're on the login
Controller Node IP: 10.1.xxx.xxx
Login Node IP: 10.1.xxx.xxx
Instance Type: ml.c5.large
ubuntu@ip-10-1-155-217:~$
```

ubuntu user가 S3-linked Lustre storage directory에 write할 수 있도록 permission을 설정합니다.

```bash
sudo chmod -R 777 /fsx/s3link
```

File을 write한 뒤 S3에 나타나는지 확인합니다.

```bash
touch /fsx/s3link/test.txt
```

[Management Console](https://console.aws.amazon.com/s3/buckets)을 열고 `pask-bucketdata`로 시작하는 bucket을 선택한 다음, 방금 만든 file이 존재하는지 확인합니다.

확인이 끝나면 `exit`로 login node에서 나옵니다.

남은 단계는 local terminal에서 수행합니다.

### 2.8. Adding Worker Groups

Worker group을 추가하려면 cdk.json을 수정하고 redeploy해야 합니다.
아래 절차에 따라 worker group을 추가합니다.

#### Instance Limit Increase 요청 및 Instance 예약

사용하려는 instance에 대해 quota increase를 요청해야 할 수 있습니다. 현재 limit이 필요한 instance 수와 일치하는지 확인하고, 필요하다면 increase를 요청하세요. **Instance type과 수량에 따라 approval에 시간이 걸릴 수 있습니다.**

Limit increase를 요청하려면 다음 절차를 따릅니다. **올바른 AWS account에 로그인되어 있는지 반드시 확인하세요.**

1. <https://console.aws.amazon.com/servicequotas/> 로 이동합니다.
1. 왼쪽 menu에서 `AWS services`를 선택합니다.
1. `Amazon SageMaker`를 검색해 선택합니다.
1. Search field에 `for cluster usage`를 입력하고 결과에서 사용할 instance type을 선택합니다.
1. `Request increase at account level` button을 클릭합니다.
1. `Increase quota value`에 원하는 값을 입력하고 `Request`를 클릭합니다.

**Note:** 이것은 limit increase request이며, instance availability를 보장하지 않습니다.

Worker group의 instance를 allocate할 수 없으면 deployment가 실패합니다. 특히 GPU instance type의 경우 [Amazon SageMaker HyperPod flexible training plans](https://aws.amazon.com/blogs/aws/meet-your-training-timelines-and-budgets-with-new-amazon-sagemaker-hyperpod-flexible-training-plans/)를 사용해 instance를 reserve해야 합니다. 자세한 절차는 [Amazon SageMaker HyperPod flexible training plans](https://aws.amazon.com/blogs/aws/meet-your-training-timelines-and-budgets-with-new-amazon-sagemaker-hyperpod-flexible-training-plans/)를 참고하세요.

**Note:** Training plan은 구매 후 취소할 수 없습니다. Target과 Instance Type을 다시 확인해 실수를 피하세요.

**Amazon SageMaker HyperPod flexible training plans**를 사용하는 경우 quota page에서 `reserved capacity across training plans per Region`을 검색하고, 해당 instance에 대한 limit increase를 요청합니다.

### 2.9. Deploy CDK to Create Worker Groups

아래처럼 worker group configuration을 `cdk.json`에 추가합니다. 사용할 instance type을 지정하세요. 여러 worker group을 추가하려면 여러 entry를 포함합니다.

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

cdk.json을 수정한 뒤 다음 command로 redeploy합니다.

```bash
cdk deploy
```

Deployment가 성공하면 worker node를 사용할 수 있습니다.

다음과 유사한 error로 deployment가 실패하면 instance를 allocate할 수 없다는 뜻입니다. 잠시 후 다시 시도하거나, deploy 전에 instance를 reserve하세요.

```bash
❌  PASK failed: ToolkitError: The stack named PASK failed to deploy: UPDATE_ROLLBACK_COMPLETE: Resource handler returned message: "Resource of type 'AWS::SageMaker::Cluster' with identifier 'Operation [UPDATE] on [arn:aws:sagemaker:us-east-1:0000000000:cluster/xxxxxxx] failed with status [InService] and error [We currently do not have sufficient capacity to launch new ml.g6e.2xlarge instances. Please try again.]' did not stabilize." (RequestToken: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxx, HandlerErrorCode: NotStabilized)
```

---

기본 Amazon SageMaker HyperPod environment 준비가 완료되었습니다.
Management Console에서 HyperPod cluster를 보려면 [https://console.aws.amazon.com/sagemaker/home#/cluster-management](https://console.aws.amazon.com/sagemaker/home#/cluster-management)로 이동합니다. Cluster가 표시되지 않으면 올바른 region이 선택되어 있는지 확인하세요.

HyperPod의 node들은 shared Lustre storage를 `/fsx`에 mount합니다. Default user인 ubuntu의 home directory는 Lustre의 `/fsx/ubuntu`로 설정됩니다. Lustre path `/fsx/s3link`는 `PASK.BucketDataBucketName`에 표시된 S3 bucket과 [linked data repository로 연결](https://docs.aws.amazon.com/fsx/latest/LustreGuide/create-dra-linked-data-repo.html)되어 있습니다. Large dataset을 사용할 때는 이 S3 bucket에 data를 upload하고 cluster에서 접근하면 됩니다. 각 node는 Lustre를 통해 S3 data에 접근할 수 있어 data 사용이 간단해집니다.
