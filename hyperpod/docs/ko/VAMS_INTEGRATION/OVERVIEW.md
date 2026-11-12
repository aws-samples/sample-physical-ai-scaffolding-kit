> **참고:** 이 문서는 AI로 자동 번역되었습니다. 정확한 내용은 [영어판](../../en/VAMS_INTEGRATION/OVERVIEW.md) 또는 [일본어판](../../ja/VAMS_INTEGRATION/OVERVIEW.md)을 확인하십시오.

# VAMS 연동 도입 및 운영

이 문서는 VAMS 워크플로에서 PASK의 HyperPod cluster로 `samples/lerobot-openpi` training job을 실행하는 연동의 도입과 운영 절차를 설명합니다.
VAMS를 사용해 본 적이 없는 독자를 대상으로 하며, VAMS를 AWS environment에 deploy하는 과정부터 설명합니다.

연동 구조, 데이터를 복사하는 이유, 데이터 규모의 기준과 대안, 권한은 [VAMS 연동 구조](/hyperpod/docs/ko/VAMS_INTEGRATION/ARCHITECTURE.md)에 있습니다.
메시지, 파일, 상태의 형식과 poller의 처리 규칙은 [VAMS 연동 사양](/hyperpod/docs/ko/VAMS_INTEGRATION/SPEC.md)에 있습니다.

## VAMS 소개

**VAMS**(Visual Asset Management System)는 3D 모델, point cloud, 동영상, 로봇 학습 데이터 등의 file을 AWS account 안에 저장하고 version을 관리하며, 웹 브라우저에서 열람하기 위한 오픈소스 solution입니다.
[awslabs/visual-asset-management-system](https://github.com/awslabs/visual-asset-management-system)에서 공개되어 있으며, AWS CDK로 사용자의 account에 deploy합니다.
File에 대한 처리는 pipeline과 workflow로 등록한 뒤 화면이나 CLI에서 실행합니다.

이 문서에서 사용하는 VAMS 용어는 다음과 같습니다.

| 용어 | 의미 |
|---|---|
| 데이터베이스 | Asset을 묶는 단위입니다. Pipeline과 workflow도 데이터베이스별로 등록합니다 |
| 아셋 | File의 모음입니다. 이 연동에서는 LeRobot dataset 1개와 학습된 model 1개가 각각 asset 1개가 됩니다 |
| 파이프라인 | 처리 1단계의 정의입니다. 이 연동에서는 PASK의 SQS queue에 메시지를 보내고, PASK로부터 완료 알림을 기다리는 처리입니다 |
| 템플릿 | 파이프라인에 전달하는 설정의 틀입니다. 이 연동에서는 학습 parameter의 기본값과 실행할 때 바꿀 수 있는 항목을 정합니다 |
| 워크플로 | 파이프라인을 나열한 것이며, 사용자가 실행하는 단위입니다 |

연동의 기능은 모두 PASK 쪽에 있으며, VAMS의 코드와 deploy 설정은 변경하지 않습니다.
VAMS에는 PASK에 포함된 파이프라인과 워크플로 정의(`hyperpod/vams/`)를 등록하기만 하면 됩니다.

## 도입 절차

도입은 다음 순서로 진행합니다.

1. 사전 요구 사항을 충족하는지 확인합니다("사전 요구 사항" 섹션).
2. VAMS를 deploy합니다("VAMS 배포" 섹션).
3. VamsCLI를 준비합니다("VamsCLI 준비" 섹션).
4. 연동 설정에 사용할 VAMS 값을 확인합니다("VAMS 값 확인" 섹션).
5. `cdk.json`의 `Vams`를 설정합니다("설정" 섹션).
6. PASKStack을 deploy합니다("배포" 섹션).
7. login 노드에 poller를 설치합니다("login 노드에 poller 설치" 섹션).
8. VAMS에 데이터베이스와 학습의 입력 및 출력 아셋을 준비합니다("데이터베이스와 아셋 준비" 섹션).
9. VAMS에 파이프라인과 워크플로를 등록합니다("파이프라인 등록" 섹션).
10. VAMS에서 학습을 실행합니다("학습 실행" 섹션).

이미 VAMS를 deploy해 두었다면 2번 단계는 건너뜁니다.

## 사전 요구 사항

PASK의 HyperPod cluster에서 [LeRobot을 사용한 pi0.5 학습](/samples/lerobot-openpi/docs/ko/lerobot_pi05_training.md)을 수동으로 실행할 수 있는 상태로 준비합니다.
VAMS에서의 학습도 같은 `slurm_train.sh`와 같은 container image(`lerobot-pi-train+<IMAGE_TAG>.sqsh`)를 사용하기 때문입니다.
Container image가 없으면 training job이 시작하는 시점에 실패합니다.

학습은 MLflow에 기록하므로, `cdk.json`의 `Mlflow.Enabled`를 `true`로 설정하여 PASKStack의 MLflow App도 사용할 수 있는 상태로 준비합니다.

VAMS는 PASK와 같은 AWS account의 같은 region에 deploy합니다.
연동은 VAMS 워크플로의 role이 PASK의 queue에 직접 메시지를 보내는 전제로 만들어져 있으며, account나 region을 넘나드는 설정을 갖지 않기 때문입니다.

작업용 PC에는 PASK를 deploy할 때 사용하는 도구에 더해 다음을 준비합니다.

| 도구 | 용도 |
|---|---|
| Python 3.13 이상 | VamsCLI를 실행합니다 |
| Docker 또는 호환되는 container engine | VAMS의 `cdk deploy`에서 container image를 build합니다 |
| Node.js 22 이상과 npm | VAMS 화면의 build와 CDK에 사용합니다 |
| `jq` | 이 문서의 절차에서 정의 file에서 값을 꺼낼 때 사용합니다 |

## VAMS 배포

VAMS deploy의 자세한 내용은 VAMS [Deployment Guide](https://awslabs.github.io/visual-asset-management-system/deployment/deploy-the-solution)에 있습니다.
여기서는 연동에 필요한 최소한의 절차와 연동을 위해 정할 값을 설명합니다.
처음 deploy하는 데는 30분에서 45분 정도 걸립니다.

1. Repository를 가져오고 안정 버전의 tag로 전환합니다.

    ```bash
    git clone https://github.com/awslabs/visual-asset-management-system.git
    cd visual-asset-management-system
    git fetch --all --tags
    git checkout tags/<릴리스 tag>
    ```

    릴리스 tag는 [Releases](https://github.com/awslabs/visual-asset-management-system/releases)에서 확인합니다.
    이 문서의 절차는 VAMS 2.6.2와 VamsCLI 2.6.2에서 검증했습니다.

2. 화면을 build하고 CDK 의존성을 설치합니다.

    ```bash
    cd web && nvm use && npm install && npm run build
    cd ../infra && npm install
    ```

3. 이 account와 region에서 CDK를 아직 사용한 적이 없다면 bootstrap합니다.

    ```bash
    cdk bootstrap aws://<계정 ID>/<PASK와 같은 region>
    ```

4. 설정 file을 템플릿에서 만들고, 다음 항목을 수정합니다.

    ```bash
    cp config/config.template.commercial.json config/config.json
    ```

    | 항목 | 값 |
    |---|---|
    | `name` | 템플릿의 `vams` 그대로 둡니다. 이 값이 PASK의 `Vams.ConfigName`이 됩니다 |
    | `env.region` | PASK와 같은 region |
    | `app.adminEmailAddress` | 최초 관리자의 이메일 주소. 임시 비밀번호가 여기로 전송됩니다 |
    | `app.adminUserId` | 최초 관리자의 사용자 이름 |
    | `app.baseStackName` | Stack 이름에 들어가는 environment 이름(예 `dev`) |

5. Deploy합니다.

    ```bash
    cdk deploy --all --require-approval never
    ```

    Docker(또는 설정한 container engine)가 실행 중이어야 합니다.

6. 출력의 `WebAppCloudFrontDistributionDomainName`에 표시되는 URL을 브라우저에서 열고, 이메일로 받은 임시 비밀번호로 로그인한 뒤 비밀번호를 설정합니다.

## VamsCLI 준비

연동의 등록과 실행에는 VAMS의 command-line 도구인 VamsCLI(`vamscli`)를 사용합니다.
VamsCLI는 1단계에서 가져온 VAMS repository의 `tools/VamsCLI`에 있습니다.

1. 가상 환경에 VamsCLI를 설치합니다.

    ```bash
    cd visual-asset-management-system/tools/VamsCLI
    python3 -m venv .venv
    source .venv/bin/activate
    pip install -e .
    vamscli --version
    ```

2. VAMS의 URL을 설정하고 관리자로 로그인합니다.

    ```bash
    vamscli setup https://<VAMS URL>
    vamscli auth login -u <관리자 사용자 이름>
    ```

VamsCLI의 자세한 사용법은 VAMS [CLI Reference](https://awslabs.github.io/visual-asset-management-system/cli/getting-started)에 있습니다.

## VAMS 값 확인

PASK의 `cdk.json`에 쓸 VAMS 값을 확인합니다.

1. VAMS의 `config.json`에서 `name`을 확인합니다.
    템플릿 그대로라면 `vams`입니다.
    이 값을 `Vams.ConfigName`에 씁니다.

2. VAMS의 아셋 bucket을 목록으로 봅니다.

    ```bash
    vamscli database list-buckets
    ```

    표시된 모든 bucket의 이름을 `Vams.AssetBucketNames`에 씁니다.
    기본 bucket(`isDefault`가 `true`인 것)의 이름을 `Vams.WorkflowBucketName`에 씁니다.
    VAMS는 파이프라인 실행의 입력과 출력을 기본 bucket의 `pipelines/`에 두기 때문입니다.

3. Bucket의 암호화와 버전 관리를 확인합니다.

    ```bash
    aws s3api get-bucket-encryption --bucket <bucket 이름>
    aws s3api get-bucket-versioning --bucket <bucket 이름>
    ```

    암호화가 SSE-KMS인 경우에만 key ARN을 `Vams.KmsKeyArns`에 씁니다.
    버전 관리가 활성화되어 있지 않을 때의 영향은 "VAMS 측에서 기대하는 운영 설정" 섹션에 있습니다.

## 설정

VAMS 연동의 설정은 [hyperpod/cdk.json](/hyperpod/cdk.json)의 `config.Vams`에 씁니다.
`Enabled`가 `false`이거나 `Vams`가 없으면 연동 리소스를 아무것도 만들지 않으며, stack template은 연동이 없을 때와 같아집니다.

`Enabled`를 `true`로 설정하면 `ConfigName`, `AssetBucketNames`, `WorkflowBucketName`, `MlflowExperimentName`과 숫자 항목은 생략할 수 없습니다.
`Mlflow.Enabled`도 `true`여야 하며, `false`이면 `cdk synth`가 실패합니다.
빈 값이거나 항목을 생략하면 `cdk synth`와 `cdk deploy`가 실패합니다.
`KmsKeyArns`와 `SlurmPartition`은 생략할 수 있습니다.

| 항목 | `cdk.json`의 값 | 내용과 값을 정하는 방법 |
|---|---|---|
| `Enabled` | `false` | `true`로 설정하면 연동 리소스를 만듭니다 |
| `ConfigName` | `vams` | VAMS deploy의 `config.name`. Queue 이름(`pask-<ConfigName>-training`)에 들어갑니다. VAMS 워크플로의 role은 이름에 `config.name`을 포함하는 queue에만 메시지를 보낼 수 있으므로, VAMS의 값과 같게 합니다. 영문자, 숫자, `-`, `_`만 사용할 수 있습니다 |
| `AssetBucketNames` | `[]` | 외부 아셋 bucket도 포함한, VAMS의 모든 아셋 bucket 이름. `vamscli database list-buckets`로 확인합니다 |
| `WorkflowBucketName` | `""` | VAMS가 실행의 manifest와 outputs를 두는 bucket(메시지의 `workflowExecutionS3InputOutputBucket`). VAMS의 기본 아셋 bucket입니다 |
| `SeparateLoginRole` | `true` | `true`이면 login 그룹의 role을 controller와 worker 그룹의 role에서 분리합니다. 이미 만들어 둔 cluster에서는 `false`로 설정합니다("배포" 섹션). 생략하면 `true`와 같습니다 |
| `KmsKeyArns` | `[]` | VAMS의 bucket이 SSE-KMS로 암호화되어 있는 경우에만 key ARN을 씁니다. 암호화 방식은 `aws s3api get-bucket-encryption --bucket <bucket 이름>`으로 확인합니다 |
| `QueueVisibilityTimeoutSeconds` | `300` | Queue의 visibility timeout(초). 접수 처리(manifest 읽기와 DynamoDB 기록)가 끝날 수 있는 길이로 설정합니다 |
| `MaxReceiveCount` | `5` | 접수되지 않은 메시지를 DLQ로 옮기기 전까지의 수신 횟수 |
| `HeartbeatIntervalSeconds` | `300` | VAMS의 Step Functions에 heartbeat를 보내는 간격(초) |
| `AbortGraceSeconds` | `1800` | Heartbeat 오류 후 중단으로 판단하기까지의 유예 시간(초). VAMS의 송신 재시도로 새 token이 도착할 때까지의 시간(1분 이내)보다 길게 합니다 |
| `LeaseSeconds` | `900` | 실행을 처리하는 poller의 lease 길이(초). `HeartbeatIntervalSeconds`의 3배로 설정합니다 |
| `ItemTtlDays` | `30` | 종료된 실행의 DynamoDB item을 보관하는 일수. SQS 보존 기간(14일)과 파이프라인 `taskTimeout`의 상한(7일)을 합친 값보다 길게 합니다 |
| `StagingExpirationDays` | `10` | PASK의 데이터 bucket에 있는 `<S3Prefix>vams/` 객체를 만료시키는 일수. 정리 과정에서 지워지지 않은 복사본을 지우기 위한 설정이며, `taskTimeout`의 상한(7일)보다 길게 합니다 |
| `IngestWaitSeconds` | `3600` | 복사한 데이터셋이 FSx에 나타날 때까지 기다리는 상한(초) |
| `MaxConcurrentRuns` | `1` | 동시에 데이터 복사 이후 단계로 진행할 수 있는 실행의 수. FSx 용량은 1200 GiB로 고정되어 있으므로, 데이터셋과 checkpoint가 들어갈 수로 설정합니다 |
| `SlurmPartition` | `""` | 학습 job을 제출할 Slurm partition. 비워 두면 Slurm의 기본 partition입니다 |
| `MlflowExperimentName` | `vams-training` | VAMS에서의 학습을 기록할 MLflow experiment. 수동 학습의 experiment(`lerobot-pi`)와 분리합니다 |

`QueueVisibilityTimeoutSeconds`와 `MaxReceiveCount`는 queue에, `StagingExpirationDays`는 bucket의 lifecycle rule에 설정합니다.
`MaxReceiveCount`와 `StagingExpirationDays`를 제외한 값은 login 노드의 poller 설정 file `/etc/pask/vams-poller.env`에도 `PASK_VAMS_*` 환경 변수로 기록됩니다.
이 설정 file은 poller를 설치했을 때에만 기록됩니다.
값을 바꾸고 deploy한 뒤에는 "login 노드에 poller 설치" 절차에 따라 설치를 다시 진행합니다.

`cdk.json` 설정의 예입니다.
`<...>`는 실제 값으로 바꿔 넣습니다.

```json
"Vams": {
  "Enabled": true,
  "ConfigName": "<VAMS config.name>",
  "AssetBucketNames": ["<VAMS 아셋 bucket>"],
  "WorkflowBucketName": "<VAMS 기본 아셋 bucket>",
  "SeparateLoginRole": true,
  "KmsKeyArns": [],
  "QueueVisibilityTimeoutSeconds": 300,
  "MaxReceiveCount": 5,
  "HeartbeatIntervalSeconds": 300,
  "AbortGraceSeconds": 1800,
  "LeaseSeconds": 900,
  "ItemTtlDays": 30,
  "StagingExpirationDays": 10,
  "IngestWaitSeconds": 3600,
  "MaxConcurrentRuns": 1,
  "SlurmPartition": "",
  "MlflowExperimentName": "vams-training"
}
```

## 배포

Deploy는 [배포 안내](/hyperpod/docs/ko/DEPLOYMENT.md)의 절차를 따르되, `cdk deploy` 전에 `cdk.json`의 `Vams.Enabled`와 `Mlflow.Enabled`를 `true`로 설정하고 나머지 항목을 입력합니다.
`Vams.Enabled`를 `true`로 설정하고 `Vams.SeparateLoginRole`도 `true`로 설정하면, login 그룹의 role이 controller와 worker 그룹의 role에서 분리됩니다.
VAMS bucket과 Step Functions에 대한 권한을 login 노드의 poller에만 부여하기 위해서입니다.

HyperPod는 이미 만들어진 instance group의 ExecutionRole 변경을 허용하지 않습니다.
이미 만들어진 cluster에서 `SeparateLoginRole`을 `true`로 설정해 deploy하면, `cdk diff`에는 `AWS::SageMaker::Cluster`의 업데이트로 표시되지만, deploy는 `Updating fields ExecutionRoleArn on an InstanceGroup not supported`로 실패하고 stack은 롤백됩니다.
이미 만들어진 cluster에 연동을 추가할 때는 `SeparateLoginRole`을 `false`로 설정합니다.
이 경우 login role은 분리되지 않고, 모든 그룹이 공통 role을 사용하며, poller의 권한도 그 role에 붙습니다([VAMS 연동 구조](/hyperpod/docs/ko/VAMS_INTEGRATION/ARCHITECTURE.md)의 "권한과 보안").
`SeparateLoginRole`을 `true`로 설정할 수 있는 것은 cluster를 새로 만드는 경우뿐입니다.

Deploy 전에 `cdk diff`로 `AWS::SageMaker::Cluster`의 `InstanceGroups`에 있는 `ExecutionRole`이 바뀌지 않는지 확인합니다.

```bash
cdk diff
```

`AWS::SageMaker::Cluster` 리소스에 `(requires replacement)` 또는 `(may cause replacement)`가 표시되면 deploy하지 마십시오.
Cluster가 다시 만들어지면 노드의 로컬 상태와 실행 중인 job이 사라집니다.
`Vams.Enabled`를 `true`에서 `false`로 되돌릴 때도 role 할당이 바뀌지 않는지 같은 방법으로 확인합니다.

확인이 끝나면 deploy합니다.

```bash
cdk deploy
```

Deploy가 성공하면 [배포 안내](/hyperpod/docs/ko/DEPLOYMENT.md)의 Outputs에 더해 다음 Outputs가 출력됩니다.

```bash
Outputs:
PASK.HyperPodLoginExecutionRoleARN = login 그룹 node의 Role ARN
PASK.VamsVamsDeadLetterQueueUrl = 접수되지 않은 메시지가 들어가는 DLQ의 URL
PASK.VamsVamsQueueArn = VAMS 워크플로가 메시지를 보내는 SQS queue의 ARN
PASK.VamsVamsQueueUrl = VAMS 워크플로가 메시지를 보내는 SQS queue의 URL(파이프라인 등록에 사용하는 값)
PASK.VamsVamsTableName = 실행 상태를 기록하는 DynamoDB 테이블의 이름
```

CloudFormation Outputs의 키에는 위 이름 뒤에 8자리 영숫자가 붙습니다.
이 문서의 command는 키의 앞부분을 기준으로 일치시켜 값을 꺼냅니다.

DynamoDB 테이블은 stack을 삭제해도 삭제되지 않습니다(`RemovalPolicy.RETAIN`).
더 이상 필요하지 않다면 수동으로 삭제하십시오.

## login 노드에 poller 설치

Poller(systemd 서비스 `pask-vams-poller`)는 login 노드의 lifecycle script `on_init_complete.sh`가 설치합니다.
Lifecycle script는 노드를 만들 때에만 실행되므로, 이미 만들어 둔 cluster의 login 노드에는 수동으로 설치합니다.
새로 만든 cluster에서도 학습용 env file을 만드는 2단계는 필요합니다.

학습용 env file `~/.config/pask/lerobot-openpi.env`가 없는 동안 poller는 시작에 실패하고 재시작을 반복합니다.
`~/.bashrc`는 systemd 서비스에서 읽히지 않으므로, poller는 학습 job의 환경 변수를 이 file에서 읽습니다.

다음 절차는 login 노드에 ubuntu 사용자로 SSH 접속해 실행합니다([배포 안내](/hyperpod/docs/ko/DEPLOYMENT.md)의 "Initial Cluster Setup").

1. Login 노드의 role을 확인합니다.

    ```bash
    aws sts get-caller-identity --query Arn --output text
    ```

    `SeparateLoginRole`이 `true`인 경우, 표시된 role 이름이 Outputs의 `HyperPodLoginExecutionRoleARN` role 이름과 같은지 확인합니다.
    `false`인 경우에는 `HyperPodClusterExecutionRoleARN` role 이름과 같은지 확인합니다.

2. `setup.sh`에 `--poller-env`를 붙여 실행하고, 학습용 env file을 만듭니다.

    ```bash
    cd ~/sample-physical-ai-scaffolding-kit/samples/lerobot-openpi/training
    bash setup.sh --hf-token "hf_xxxxx" --poller-env
    source ~/.bashrc
    ```

    `setup.sh`는 `~/.bashrc`에 쓰는 값과 같은 값을 전개한 뒤, `~/.config/pask/lerobot-openpi.env`(권한 0600)에 씁니다.
    `--hf-token` 등 다른 옵션은 [LeRobot을 사용한 pi0.5 학습](/samples/lerobot-openpi/docs/ko/lerobot_pi05_training.md)과 같습니다.

3. Lifecycle script bucket에서 설치 script와 poller 소스를 꺼내 실행합니다.

    ```bash
    LIFECYCLE_BUCKET="$(aws cloudformation describe-stacks --stack-name PASK \
      --query "Stacks[0].Outputs[?starts_with(OutputKey, 'HyperPodLifeCycleScriptBucketName')].OutputValue | [0]" \
      --output text)"
    WORK_DIR="$(mktemp -d)"
    aws s3 cp "s3://${LIFECYCLE_BUCKET}/install_vams_poller.sh" "${WORK_DIR}/"
    aws s3 cp --recursive "s3://${LIFECYCLE_BUCKET}/vams_poller/" "${WORK_DIR}/vams_poller/"
    sudo bash "${WORK_DIR}/install_vams_poller.sh"
    ```

    `install_vams_poller.sh`는 `vams_poller/`가 같은 디렉터리에 있다고 가정합니다.
    Script 안의 설정 값(queue URL 등)은 deploy할 때 포함됩니다.
    Script는 다음 처리를 수행합니다.

    1. Poller 소스를 `/opt/pask/vams-poller/`에 복사하고, venv를 만들어 설치합니다.
       HyperPod image의 `python3`는 poller가 지원하는 버전보다 오래되었으므로, 버전을 고정한 uv로 CPython 3.14를 `/opt/pask/python/`에 설치하고 그 Python으로 venv를 만듭니다. uv와 CPython은 GitHub에서 가져옵니다.
    2. 설정 file `/etc/pask/vams-poller.env`를 작성합니다.
    3. `pask-vams-poller.service`를 작성하고, 활성화한 뒤 재시작합니다.

    Login 그룹이 아닌 노드에서 실행했거나 `Vams.Enabled`가 `true`가 아닌 경우에는 아무 작업도 하지 않습니다.
    몇 번을 실행해도 결과는 같습니다.

4. Poller가 동작하는지 확인합니다.

    ```bash
    systemctl status pask-vams-poller
    journalctl -u pask-vams-poller -f
    ```

    `systemctl status`가 `active (running)`을 표시하고 `journalctl`에 오류가 계속 나오지 않으면 설치가 끝난 것입니다.
    시작에 실패한 경우에는 `journalctl -u pask-vams-poller`로 원인을 확인하고 수정한 뒤 3단계를 다시 진행합니다.

Login 노드를 다시 만들면 lifecycle script가 poller를 설치합니다.
학습용 env file은 FSx 위의 home 디렉터리에 있으므로, 다시 만들어도 남아 있습니다.
Lifecycle script에 의한 설치가 실패해도 노드 생성은 실패하지 않습니다.
새 login 노드에서 poller가 동작하지 않는다면 3단계와 4단계를 다시 진행합니다.

## 데이터베이스와 아셋 준비

학습에는 VAMS 데이터베이스 1개와 그 안의 아셋 2개를 사용합니다.
파이프라인과 워크플로도 같은 데이터베이스에 등록하므로, 이 섹션을 먼저 마칩니다.

1. 데이터베이스를 만듭니다.

    ```bash
    DATABASE_ID="<데이터베이스 ID(예 robot-datasets)>"
    vamscli database create -d "${DATABASE_ID}" --description "<설명>"
    ```

    아셋을 둘 bucket을 묻는 경우에는 기본 bucket을 선택합니다.

2. 입력 아셋을 만들고, LeRobot v3.0 데이터셋을 업로드합니다.

    ```bash
    vamscli assets create -d "${DATABASE_ID}" --name "<데이터셋 이름>" --description "<설명>"
    vamscli file upload -d "${DATABASE_ID}" -a "<입력 아셋 ID>" --directory <데이터셋 디렉터리> --recursive
    ```

    아셋 ID는 `assets create`의 출력에 표시됩니다.
    `--directory`에는 `meta/info.json`을 바로 아래에 가진 디렉터리를 지정합니다.

3. 학습된 model을 넣을 출력 아셋을 만듭니다.
    내용은 비워 둡니다.

    ```bash
    vamscli assets create -d "${DATABASE_ID}" --name "<모델 이름>" --description "<설명>"
    ```

입력 아셋은 루트에 LeRobot 데이터셋을 가진 아셋으로 합니다.
루트에 `meta/info.json`이 있고, 그 아래에 `meta/`, `data/`, `videos/` 등이 있는 형태입니다.
Poller는 아셋 전체를 PASK의 데이터 bucket에 복사하고, 그 루트를 `DATASET_ROOT`로 학습 job에 전달합니다.
데이터셋이 아셋의 하위 디렉터리(예 `lerobot/<데이터셋 이름>/`)에 있는 아셋에서는 학습 job이 데이터셋을 읽지 못해 실패합니다.
아셋에 데이터셋 이외의 file(변환 전 MCAP 등)이 있으면 그것도 복사되어 FSx 용량을 사용합니다.
VAMS 폴더 표시(`/`로 끝나는 키)와 미리보기 file(`.previewFile.`을 포함하는 키)은 복사하지 않습니다.

VAMS에 LeRobot Dataset Curation 파이프라인이 있는 경우에는, 그것으로 만든 view 아셋도 입력으로 사용할 수 있습니다. 이 파이프라인은 VAMS 버전에 따라 포함되어 있지 않을 수 있습니다.
View 아셋은 루트에 `curation_manifest.json`(`mode`가 `view`)을 가지며, data와 동영상은 가지지 않습니다.
Poller는 manifest가 기록한 원본 아셋 file의 버전을 복사하고, manifest의 `episodes`에 있는 에피소드만으로 학습합니다.
View를 입력으로 사용할 때는 다음 두 가지를 지킵니다.

- View와 원본 아셋을 같은 데이터베이스에 둡니다. Manifest가 입력 아셋과 다른 데이터베이스나 bucket을 가리키고 있으면, poller는 `PASK.InvalidRequest`로 실패시킵니다.
- 학습이 끝날 때까지 원본 아셋과, view가 기록한 버전을 삭제하지 않습니다. 기록된 버전이 없으면 poller는 `PASK.InvalidRequest`로 실패시킵니다.

View의 해석 흐름은 [VAMS 연동 구조](/hyperpod/docs/ko/VAMS_INTEGRATION/ARCHITECTURE.md)의 "curation view 해석 과정" 섹션에 있습니다.

출력 아셋은 입력과 다른 아셋으로 합니다.
Poller는 학습된 model file을 출력 아셋의 루트에 다시 기록합니다.
출력 아셋을 입력과 같게 하면 model file이 데이터셋의 루트에 놓이고, 같은 아셋으로 다음에 학습할 때 데이터셋으로 복사되어 버립니다.

## 파이프라인 등록

VAMS에 등록할 때는 PASK에 포함된 다음 정의를 사용합니다.

| File | 내용 |
|---|---|
| [hyperpod/vams/pipeline.json](/hyperpod/vams/pipeline.json) | 파이프라인 이름, `executionConfig`, `systemConfig` |
| [hyperpod/vams/template.json](/hyperpod/vams/template.json) | 학습 parameter 템플릿(`configBody`)과 태그 정의(`tagSchema`) |
| [hyperpod/vams/workflow.json](/hyperpod/vams/workflow.json) | 워크플로 이름과 `systemConfig` |

VamsCLI의 command는 file 전체가 아니라 `executionConfig`나 `systemConfig` 등 항목별로 값을 받습니다.
다음 절차에서는 `jq`로 각 항목을 꺼내서 전달합니다.

### 정의 내용

파이프라인은 SQS 실행 타입(`executionType: SQS`)이며, TaskToken이 반환될 때까지 기다립니다(`waitForCallback: Enabled`).
`sqs.queueUrl`은 file에서는 비워 두었고, 등록할 때 Outputs의 `VamsVamsQueueUrl` 값을 넣습니다.
빈 상태로 등록하면 VAMS가 `sqs.queueUrl is required`로 거부합니다.

`taskHeartbeatTimeout`은 비워 둡니다.
값을 넣으면 poller가 멈춰 있는 동안 heartbeat가 끊겨, 학습이 계속되고 있어도 VAMS 단계가 실패하기 때문입니다.

`taskTimeout`은 `432000`초(5일)로 설정했습니다.
VAMS는 이 시간이 지나면 단계를 실패로 처리하고, poller는 다음 heartbeat 오류부터 중단으로 간주해 학습 job을 멈춥니다.
Heartbeat는 `taskTimeout`을 연장하지 않으므로, 한 번의 실행에 걸리는 시간의 합계보다 길어야 합니다.
합계에는 다음 시간이 포함됩니다.

| 시간 | 길이의 기대치 |
|---|---|
| 학습 | 최대 48시간(`slurm_train.sh`의 `#SBATCH --time=48:00:00`). 분위수 재계산도 job 안에서 수행하므로 이 시간에 포함됩니다 |
| 데이터 복사와 FSx로의 적재 | 적재 대기의 상한은 1시간(`IngestWaitSeconds`) |
| 결과 반영(write-back) | Checkpoint 크기에 따라 다르지만, 1시간 안에 들어갈 것으로 예상합니다 |
| 먼저 실행 중인 실행이 끝나기를 기다리는 시간 | `MaxConcurrentRuns`가 1이면, 앞선 실행 1건당 최대 50시간 |
| Slurm 대기열에서의 대기 | Worker 노드가 비기까지의 시간 |

학습, 복사, write-back을 합쳐 약 50시간, 앞선 실행 1건을 기다리는 시간을 더하면 약 100시간이 되고, 나머지 약 20시간은 Slurm 대기열에서의 대기에 할당합니다.
VAMS의 상한은 604800초(7일)입니다.
동시에 3건 이상의 실행을 제출하는 운영에서는 대기 시간이 늘어나므로, 상한 범위 안에서 값을 키웁니다.
다만 `taskTimeout`을 길게 하면, DLQ에 들어간 메시지의 실행도 VAMS 측에서 그 시간만큼 계속 기다립니다.

`systemConfig`의 `inputFileArity: one`과 `assetScope`로 입력을 아셋 1개 전체로 한정합니다.
`metadataInputs.assetMetadata`를 `true`로 하는 것은 아셋 이름 등을 MLflow에 기록하기 위해서입니다.
`requireTemplate`을 `true`, `allowCustomTemplateOverride`를 `false`로 하여, 학습 parameter는 템플릿의 태그로만 받습니다.

템플릿의 `configBody`는 학습 parameter의 환경 변수 이름을 키로, 태그(`{{STEPS}}` 등)를 값으로 한 평평한(flat) JSON입니다.
키 목록은 poller의 허용 목록([messages.py](/hyperpod/vams-poller/src/pask_vams_poller/messages.py)의 `ALLOWED_TRAINING_PARAMS`)에서 옮겨 왔습니다.
태그의 기본값은 [slurm_train.sh](/samples/lerobot-openpi/training/slurm_train.sh)의 기본값과 같게 했습니다.
허용 목록이나 `slurm_train.sh`의 기본값을 바꾼 경우에는, `template.json`의 `configBody`와 `tagSchema`도 같은 방식으로 바꿉니다.
허용 목록에 없는 키가 템플릿에 남아 있으면, poller는 모든 실행을 `PASK.InvalidRequest`로 실패시킵니다.

`configBody`에서는 문자열 태그를 따옴표 안에 쓰고, 정수와 불리언 태그(`{{STEPS}}`, `{{COMPILE_MODEL}}` 등)는 따옴표 없이 씁니다.
VAMS는 따옴표가 없는 태그를 태그의 타입에 따라 JSON의 정수와 불리언으로 치환합니다.
Poller는 정수가 아닌 숫자를 받지 않으므로, 숫자 태그의 타입은 `number`가 아니라 `integer`로 했습니다.
모든 태그는 기본값을 가지며 `required`를 `true`로 했으므로, VAMS가 만드는 설정 JSON에는 항상 모든 키가 들어갑니다.

워크플로는 아셋 전체의 입력을 허용하고(`wholeAssetAllowed: true`), 출력 아셋을 실행할 때 바꿀 수 있도록 합니다(`outputTarget.allowOverride: true`).

### 등록 절차

다음 command는 `hyperpod` 디렉터리에서 VamsCLI를 인증한 상태로 실행합니다.
파이프라인, 템플릿, 워크플로는 학습에 사용하는 아셋과 같은 VAMS 데이터베이스에 등록합니다.

1. 등록에 사용할 값을 변수에 넣습니다.
    `DATABASE_ID`는 "데이터베이스와 아셋 준비" 섹션에서 만든 데이터베이스의 ID입니다.

    ```bash
    DATABASE_ID="<VAMS 데이터베이스 ID>"
    PIPELINE_ID="pask-lerobot-openpi-training"
    WORKFLOW_ID="pask-lerobot-openpi-training"
    TEMPLATE_ID="$(jq -r .templateId vams/template.json)"
    QUEUE_URL="$(aws cloudformation describe-stacks --stack-name PASK \
      --query "Stacks[0].Outputs[?starts_with(OutputKey, 'VamsVamsQueueUrl')].OutputValue | [0]" \
      --output text)"
    ```

    `PIPELINE_ID`와 `WORKFLOW_ID`는 영문자, 숫자, `-`, `_`로 된 3~63자이면 바꿔도 됩니다.
    VAMS는 manifest의 `auxTempPrefix`에 파이프라인 이름이 아니라 `PIPELINE_ID`를 넣으므로, 이 값이 MLflow 태그 `vams.pipeline_name`에 들어갑니다.

2. 파이프라인을 등록합니다.

    ```bash
    vamscli pipeline create -d "${DATABASE_ID}" -p "${PIPELINE_ID}" \
      -n "$(jq -r .pipelineName vams/pipeline.json)" \
      --category "$(jq -r .category vams/pipeline.json)" \
      --description "$(jq -r .description vams/pipeline.json)" \
      --execution-config "$(jq -c --arg url "${QUEUE_URL}" '.executionConfig | .sqs.queueUrl = $url' vams/pipeline.json)" \
      --system-config "$(jq -c .systemConfig vams/pipeline.json)"
    ```

3. 템플릿을 등록합니다.

    ```bash
    vamscli pipeline template create -d "${DATABASE_ID}" -p "${PIPELINE_ID}" -t "${TEMPLATE_ID}" \
      -n "$(jq -r .templateName vams/template.json)" \
      --description "$(jq -r .description vams/template.json)" \
      --config-format json \
      --config-body "$(jq -r .configBody vams/template.json)" \
      --tag-schema "$(jq -c .tagSchema vams/template.json)" \
      --input-instructions "$(jq -r .inputInstructions vams/template.json)" \
      --default
    ```

    `--allow-custom-edit`는 붙이지 않습니다.
    붙이면 실행할 때 설정 JSON을 다시 쓸 수 있게 됩니다.

4. 워크플로를 등록합니다.

    ```bash
    vamscli workflow create -d "${DATABASE_ID}" -w "${WORKFLOW_ID}" \
      -n "$(jq -r .workflowName vams/workflow.json)" \
      --category "$(jq -r .category vams/workflow.json)" \
      --description "$(jq -r .description vams/workflow.json)" \
      --pipeline "${DATABASE_ID}:${PIPELINE_ID}:${TEMPLATE_ID}" \
      --system-config "$(jq -c .systemConfig vams/workflow.json)"
    ```

5. 워크플로의 `systemConfig`를 확인합니다.

    ```bash
    vamscli workflow get -d "${DATABASE_ID}" -w "${WORKFLOW_ID}" --json-output
    ```

    `systemConfig.assetScope.wholeAssetAllowed`와 `systemConfig.outputTarget.allowOverride`가 `true`인지 확인합니다.
    `systemConfig`를 붙이지 않고 만든 워크플로에서는 `wholeAssetAllowed`가 `false`가 되는 것을 확인했습니다.
    `false`인 경우에는 다음 command로 업데이트합니다.

    ```bash
    vamscli workflow update -d "${DATABASE_ID}" -w "${WORKFLOW_ID}" \
      --system-config "$(jq -c .systemConfig vams/workflow.json)"
    ```

## 학습 실행

VAMS에서 학습을 실행할 때는 입력으로 데이터셋 아셋이나 curation view 아셋의 전체를 선택하고, 출력으로 학습된 model 아셋을 선택합니다("데이터베이스와 아셋 준비" 섹션).
아셋의 file을 1개만 선택한 실행과 폴더를 선택한 실행은 `PASK.InvalidRequest`로 실패합니다.
View를 입력으로 사용한 경우에는, 학습이 끝날 때까지 원본 아셋과 view가 기록한 버전을 삭제하지 마십시오.

다음은 VamsCLI로 실행하는 예입니다.
`templateTags`에는 바꾸고 싶은 학습 parameter만 지정합니다.
지정하지 않은 태그는 템플릿의 기본값이 됩니다.

```bash
vamscli workflow execute --workflow-database-id "${DATABASE_ID}" -w "${WORKFLOW_ID}" \
  --input-file "${DATABASE_ID}:<입력 아셋 ID>:/" \
  --output-asset-id "<출력 아셋 ID>" \
  --pipeline-parameters "$(jq -cn --arg pipeline "${PIPELINE_ID}" --arg template "${TEMPLATE_ID}" \
    '{($pipeline): {templateId: $template, templateTags: [{key: "STEPS", value: 1000}]}}')"
```

`--input-file`의 마지막 `/`가 아셋 전체를 나타냅니다.
VAMS 화면에서 실행하는 경우에는 입력 아셋의 화면에서 "Execute Workflow"로 워크플로 `pask-lerobot-openpi-training`을 선택하고, 입력에 아셋 전체를, 출력에 출력 아셋을 지정합니다.

실행 상태는 VAMS의 실행 상세(`vamscli execution details <실행 ID>`)에서 확인합니다.
Cluster에서는 학습 job이 `vams-<pipelineExecutionId>`라는 이름으로 Slurm에 제출됩니다.

```bash
squeue --name "vams-<pipelineExecutionId>"
```

동시에 데이터 복사 이후 단계로 진행할 수 있는 실행의 수는 `MaxConcurrentRuns`까지입니다.
상한을 넘은 실행은 앞선 실행의 정리가 끝날 때까지 기다립니다.
기다리는 동안에도 heartbeat를 보내므로, `taskTimeout` 안이라면 실패하지 않습니다.

VAMS에서 실행을 중단하면, poller는 유예 시간(`AbortGraceSeconds`)이 지난 후 학습 job을 `scancel`로 멈춥니다.

## 학습 출력

학습 출력(출력 아셋에 들어가는 file, results의 `pask_training.json`, MLflow 기록)과 실행 후 삭제는 [VAMS 연동 구조](/hyperpod/docs/ko/VAMS_INTEGRATION/ARCHITECTURE.md)의 "사용자가 지정하는 것과 받는 것"과 "전체 흐름" 섹션에 있습니다.
Write-back 규칙은 [VAMS 연동 사양](/hyperpod/docs/ko/VAMS_INTEGRATION/SPEC.md)의 "완료 통지와 write-back" 섹션에, MLflow 태그 목록은 같은 문서의 "MLflow에 기록하는 것" 섹션에 있습니다.

## VAMS 측에서 기대하는 운영 설정

다음 VAMS 설정은 PASK에서는 바꿀 수 없습니다.
VAMS를 운영하는 쪽에서 다음과 같이 설정해 둡니다.

1. 워크플로의 `metadataInputs`에서 아셋 메타데이터(`assetMetadata`)를 활성화합니다.
    비활성화하면 `metadata.json`에 아셋의 이름, 설명, 태그가 들어가지 않습니다.
    MLflow run에 태그 `vams.input.asset_name`이 붙지 않으며, dataset의 이름이 assetId가 됩니다.
    동봉된 `pipeline.json`과 `workflow.json`에서는 활성화되어 있습니다.
2. 아셋 bucket의 버전 관리를 활성화합니다.
    비활성화해도 학습은 수행되지만, 복사한 입력의 버전을 고정할 수 없습니다.
    `input_manifest.json`과 `vams_context.json`의 `versioned`가 `false`가 되고, `versionId`가 `"null"`이 됩니다.
    버전 관리 상태는 `aws s3api get-bucket-versioning --bucket <bucket 이름>`으로 확인합니다.
3. 파이프라인은 동봉된 템플릿과 태그 정의로 등록하고, `allowCustomTemplateOverride`를 `false`로 설정합니다.
    `true`로 설정하면 실행할 때 임의의 설정 JSON을 전달할 수 있게 됩니다.
    Poller는 허용 목록에 없는 키를 거부하지만, 그 경우의 실패는 학습 job을 제출하기 전이 아니라 VAMS에서 실행한 후에만 알 수 있습니다.

## DLQ 확인

Poller가 접수하지 못한 메시지는 `MaxReceiveCount`(5회) 수신된 후 DLQ에 들어갑니다.
DLQ에 들어가는 것은 본문에서 TaskToken을 읽을 수 없는 메시지와, DynamoDB 기록에 계속 실패한 메시지입니다.
DLQ에 들어간 메시지의 실행은 PASK에서 결과가 돌아오지 않으므로, VAMS 측에서는 `taskTimeout`(동봉된 정의에서는 5일)까지 계속 기다립니다.

DLQ의 메시지는 로컬 PC에서, DLQ를 읽을 권한이 있는 AWS 자격 증명으로 확인합니다.
Login 노드의 role에는 DLQ 권한이 없습니다.

1. DLQ의 URL을 꺼냅니다.

    ```bash
    DLQ_URL="$(aws cloudformation describe-stacks --stack-name PASK \
      --query "Stacks[0].Outputs[?starts_with(OutputKey, 'VamsVamsDeadLetterQueueUrl')].OutputValue | [0]" \
      --output text)"
    ```

2. 메시지 수를 확인합니다.

    ```bash
    aws sqs get-queue-attributes --queue-url "${DLQ_URL}" \
      --attribute-names ApproximateNumberOfMessages
    ```

3. 메시지 본문을 확인합니다.

    ```bash
    aws sqs receive-message --queue-url "${DLQ_URL}" \
      --max-number-of-messages 10 --visibility-timeout 60 \
      --attribute-names All
    ```

    본문의 `body.workflowExecutionId`가 VAMS 실행의 ID입니다.
    접수되지 않은 이유는 그 시각의 poller 로그(login 노드의 `journalctl -u pask-vams-poller`)에서 확인합니다.
    본문에는 TaskToken이 들어 있습니다.
    TaskToken과 `states:SendTask*` 권한이 있으면 VAMS 단계를 끝낼 수 있으므로, 본문을 티켓이나 채팅에 붙여 넣지 마십시오.

4. VAMS 실행을 기다리게 해 둘 필요가 없다면, VAMS에서 실행을 중단하고 DLQ 메시지를 삭제합니다.

    ```bash
    vamscli execution abort "<workflowExecutionId>"
    aws sqs delete-message --queue-url "${DLQ_URL}" --receipt-handle "<ReceiptHandle>"
    ```

    `<ReceiptHandle>`은 3단계 출력의 `ReceiptHandle` 값입니다.

접수된 후의 실행 상태는 DynamoDB 테이블의 item으로 확인할 수 있습니다.

```bash
TABLE_NAME="$(aws cloudformation describe-stacks --stack-name PASK \
  --query "Stacks[0].Outputs[?starts_with(OutputKey, 'VamsVamsTableName')].OutputValue | [0]" \
  --output text)"
aws dynamodb get-item --table-name "${TABLE_NAME}" \
  --key '{"pipelineExecutionId": {"S": "<pipelineExecutionId>"}}'
```

`state` 값의 의미는 [VAMS 연동 사양](/hyperpod/docs/ko/VAMS_INTEGRATION/SPEC.md)의 "상태 전이" 섹션에 있습니다.

## login 노드 권한 주의 사항

Login 노드에는 poller 외에도 사람이 SSH로 접속합니다.
Login 노드에 SSH로 접속한 사람은 poller와 같은 권한을 사용할 수 있습니다.
권한 목록과 `SeparateLoginRole`에 따른 차이는 [VAMS 연동 구조](/hyperpod/docs/ko/VAMS_INTEGRATION/ARCHITECTURE.md)의 "권한과 보안" 섹션에 있습니다.
