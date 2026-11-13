> **참고:** 이 문서는 AI로 자동 번역되었습니다. 정확한 내용은 [영어판](../../en/VAMS_INTEGRATION/ARCHITECTURE.md) 또는 [일본어판](../../ja/VAMS_INTEGRATION/ARCHITECTURE.md)을 확인하십시오.

# VAMS 연동 구조

이 문서는 VAMS(Visual Asset Management System) workflow에서 PASK의 HyperPod cluster로 LeRobot(pi0 및 pi0.5) 학습을 실행하는 연동의 동작 방식과 제약을 정리합니다.
독자는 VAMS에서 학습을 실행하는 사용자와, 연동을 운영하거나 수정하는 개발자입니다.
설치 절차와 설정 항목 목록은 [VAMS 연동의 설치와 운영](/hyperpod/docs/ko/VAMS_INTEGRATION/OVERVIEW.md)을, 메시지와 파일 형식의 세부 사항은 [VAMS 연동 사양](/hyperpod/docs/ko/VAMS_INTEGRATION/SPEC.md)을 참고하세요.

## 연동 범위

VAMS 사용자는 LeRobot v3.0 dataset asset을 선택해 workflow를 실행하고, 학습된 모델을 별도의 asset으로 받습니다.
학습의 진행 상황과 결과는 PASK MLflow App의 experiment `vams-training`에 기록됩니다.

연동 기능은 모두 PASK 쪽에 있으며, VAMS의 코드와 배포 설정은 변경하지 않습니다.
VAMS에는 PASK에 포함된 pipeline과 workflow 정의(`hyperpod/vams/`)를 등록하기만 하면 됩니다.
이 연동은 VAMS와 PASK가 같은 AWS account의 같은 region에 있다고 가정합니다.
account나 region을 나누는 구성과 1주일을 넘는 학습은 대상이 아닙니다.

## 전체 흐름

VAMS는 내장된 SQS 실행 타입(`waitForCallback=Enabled`)의 pipeline으로 PASK를 호출합니다.
PASK의 login 노드에 상주하는 poller(systemd 서비스 `pask-vams-poller`)가 실행을 다음 순서로 진행합니다.

1. VAMS의 Step Functions가 PASK의 SQS 큐에 메시지를 보내고, TaskToken이 반환될 때까지 기다립니다.
2. poller는 메시지를 받아 실행을 PASK의 DynamoDB에 기록한 뒤, 메시지를 삭제합니다.
3. poller는 VAMS의 asset 버킷에서 PASK의 데이터 버킷 `vams/<pipelineExecutionId>/`로 입력 dataset을 복사합니다.
   입력이 curation view인 경우에는 view가 가리키는 원본 asset의 파일을 복사합니다("curation view 해석 과정" 절 참고).
   복사한 파일은 FSx for Lustre의 자동 가져오기를 통해 `/fsx/s3link/vams/<pipelineExecutionId>/`에 나타납니다.
4. poller는 `slurm_train.sh`를 Slurm job으로 제출하고, job이 끝날 때까지 감시합니다.
5. job이 성공하면 poller는 학습된 모델을 VAMS의 outputs에 다시 쓰고 `SendTaskSuccess`를 반환합니다.
   VAMS는 outputs의 파일을 출력 asset으로 가져갑니다.
6. poller는 PASK 데이터 버킷의 복사본과 FSx의 실행 디렉터리 및 출력 디렉터리를 삭제합니다.

실행 상태의 소유자는 VAMS와 PASK로 나뉩니다.
workflow와 step의 상태(실행 중, 성공, 실패, 중단)는 VAMS가 관리하며, PASK는 `SendTask*`를 반환하기만 합니다.
Slurm job ID, TaskToken, PASK 쪽 경로는 PASK의 DynamoDB가 관리합니다.
PASK가 이것을 직접 기록하는 이유는 hyperpod stack에 slurmdbd가 없고, 끝난 job이 몇 분 안에 `squeue`에서 사라지기 때문입니다.

## 사용자가 지정하는 것과 받는 것

사용자는 VAMS에서 workflow `pask-lerobot-openpi-training`을 실행할 때 다음을 지정합니다.

| 지정하는 항목 | 내용 |
|---|---|
| 입력(일반 dataset) | dataset asset 전체. LeRobot v3.0 dataset이 asset의 root에 있어야 하며, root에 `meta/info.json`이 있어야 합니다 |
| 입력(curation view) | VAMS LeRobot Dataset Curation이 만든 view asset 전체. root에 `curation_manifest.json`이 있어야 하고, 그 `mode`가 `view`여야 합니다. view와 원본 asset은 같은 database에 있어야 합니다 |
| 출력 asset | 학습된 모델을 넣을 asset. dataset과는 별도의 asset으로 지정합니다 |
| 학습 파라미터 | `POLICY_TYPE`, `FINETUNE_MODE`, `STEPS`, `SAVE_FREQ` 등 template의 tag에 있는 항목만. 허용 목록에 없는 key가 있으면 실행이 실패합니다 |

어느 입력이든, 파일 하나만 선택하는 지정과 asset 안의 폴더를 선택하는 지정은 받지 않습니다.

실행이 성공하면 사용자는 다음을 받습니다.

- 출력 asset에 `checkpoints/last/pretrained_model` 아래의 파일(가중치, 설정, 전처리와 후처리 설정)이 들어갑니다. optimizer 상태는 들어가지 않습니다.
- VAMS 실행 상세의 results에 `pask_training.json`(Slurm job ID, 학습 파라미터, dataset digest, MLflow run ID, 입력 종류)이 들어갑니다. view인 경우에는 원본 asset과 남긴 episode 수도 들어갑니다.
- MLflow run에 `pask.trigger=vams`와 `vams.*` tag(VAMS 실행 ID, 입력과 출력 asset 등), 입력 dataset, 입력 파일 목록(artifact `vams/input_manifest.json`)이 기록됩니다. view인 경우에는 원본 asset의 tag, view manifest(artifact `vams/curation_manifest.json`), 학습에 사용한 정규화 통계(artifact `vams/norm_stats.json`)도 기록됩니다.

VAMS에서 실행을 중단하면, poller는 중단을 감지하고 job을 `scancel`합니다.
VAMS에서 오는 중단 알림은 없으므로, poller는 heartbeat가 TaskToken 만료 오류를 반환하는 것으로 중단을 감지합니다.
VAMS의 전송 재시도로 새로운 TaskToken이 도착할 수 있으므로, poller는 유예 시간(기본 30분)을 기다린 뒤에 중단으로 판단합니다.

실패하면 VAMS 실행 상세에 다음 오류 이름이 표시됩니다.

| 오류 이름 | 원인 |
|---|---|
| `PASK.InvalidRequest` | 입력 지정 또는 학습 파라미터가 조건에 맞지 않습니다. 입력의 경우는 다음과 같습니다. root에 `meta/info.json`도 `curation_manifest.json`도 없음. `curation_manifest_version`이 2가 아님. view manifest가 올바르지 않음(필수 항목이 없음, episode 번호가 잘못됨 등). manifest가 다른 database나 버킷을 가리킴. manifest가 기록한 버전이 원본 asset에 없거나 크기가 기록과 다름 |
| `PASK.StagingFailed` | 데이터 복사에 실패했거나, FSx의 여유 공간이 부족하거나, 가져오기가 대기 상한(기본 3600초)까지 끝나지 않았습니다. view인 경우에는 poller가 원본 asset을 읽을 수 없는(403) 경우도 포함합니다 |
| `PASK.SubmitFailed`, `PASK.JobLost` | job을 제출하지 못했거나, job의 종료를 확인하지 못했습니다 |
| `PASK.JobFailed` | 학습 job이 실패했습니다. view의 정규화 통계 계산 실패도 포함합니다 |
| `PASK.OutputUploadFailed` | 학습된 모델을 outputs에 다시 쓰지 못했습니다 |

## 데이터를 VAMS 버킷과 분리해서 복사하는 이유

학습 입력은 VAMS의 asset 버킷을 FSx에 직접 연결하지 않고, 실행마다 PASK의 데이터 버킷으로 복사합니다.
같은 S3 데이터를 VAMS와 FSx가 모두 동기화하면, 파일 내용과 VAMS의 버전 기록이 어긋나 데이터가 손상될 수 있기 때문입니다.

PASK의 FSx는 데이터 버킷과 data repository association을 가지며, 생성, 변경, 삭제를 모두 양방향으로 자동 반영합니다.
VAMS의 asset 버킷을 이 association으로 연결하면, 다음 두 경우에 VAMS의 데이터와 기록이 어긋납니다.

- FSx에서 파일을 덮어쓰면(예를 들어 학습 전처리가 `meta/stats.json`을 다시 만들면), 그 변경이 VAMS 버킷에 자동으로 기록됩니다. VAMS는 자체 API를 거치지 않은 변경을 버전으로 기록하지 않으므로, VAMS가 관리하는 asset의 버전과 S3의 내용이 일치하지 않게 됩니다. FSx에서의 삭제도 VAMS 원본 데이터의 삭제가 됩니다.
- 학습 도중 VAMS 쪽에서 asset을 갱신하면, 그 변경이 FSx로 가져와집니다. 학습 job은 읽는 도중에 다른 버전의 파일로 바뀐 데이터를 읽게 됩니다.

이 밖에도, FSx 자동 가져오기는 대상 버킷에 S3 이벤트 알림을 설정하므로, VAMS가 asset 버킷에 설정하는 알림과 겹칩니다.

복사에서는 입력을 목록화한 시점의 VersionId를 지정해서 복사합니다.
따라서 실행 중에 VAMS에서 asset이 갱신되어도, 학습은 목록화한 시점의 버전을 읽습니다.
복사 대상은 실행마다 나뉘고, 실행이 끝나면 삭제되므로, PASK 쪽 변경이 VAMS 데이터에 영향을 미치지 않습니다.
poller에는 VAMS 버킷에 대한 삭제 권한을 부여하지 않습니다.

Mountpoint for Amazon S3 등으로 VAMS asset 버킷을 각 노드에 직접 마운트하고, 복사 없이 읽는 방식도 채택하지 않습니다.
이 방식에서는 학습 job이 asset 버킷의 최신 버전을 읽게 됩니다. Mountpoint for Amazon S3는 버전을 지정해서 읽을 수 없기 때문입니다.
학습은 몇 시간에서 며칠 동안 계속됩니다. 그 사이에 VAMS에서 asset이 갱신되거나 삭제되면, job은 학습 도중부터 다른 버전의 데이터를 읽게 되므로 데이터 불일치가 발생할 수 있습니다.
읽기 전용으로 마운트해도 이 문제는 남습니다.
이를 막으려면 버전 고정이나, 학습 전후의 입력 변화 감지 등 여러 대책이 필요합니다.
그래서 현재로서는 실행 시점의 데이터를 복사하고, 그 복사본을 학습 동안 고정해서 사용합니다.

## curation view 해석 과정

VAMS LeRobot Dataset Curation의 view 방식은, episode를 선택한 결과를 데이터를 복사하지 않고 view asset으로 남깁니다.
view asset에 있는 것은 `curation_manifest.json`과, 남긴 episode로 계산한 `meta/stats.json`뿐입니다.
data와 동영상은 원본 asset에 그대로 남아 있고, manifest가 원본 파일을 S3 버전(VersionId)으로 가리킵니다.
PASK는 이 manifest에서 원본 파일을 해석해서 학습합니다.
VAMS에서 PASK로 보내는 메시지의 형식은 일반 dataset과 같습니다.

poller와 학습 job은 다음 순서로 view를 해석합니다.

1. STAGING에서 poller는 입력 asset의 root에 있는 최신 버전을 목록화합니다. root에 `curation_manifest.json`이 있으면, 목록화한 버전을 읽습니다. `mode`가 `view`이면 view로 취급하고 manifest를 검증합니다.
2. poller는 manifest의 `source.files`에 있는 각 파일에 대해, 기록된 버전(VersionId)을 지정해 `HeadObject`를 수행하고, 버전이 존재하는지와 크기가 기록과 같은지를 확인합니다. 버전이 없으면 이유를 붙여 실행을 실패시킵니다.
3. poller는 기록된 버전을 실행마다의 버킷(PASK 데이터 버킷의 staging prefix)에 복사합니다. view 자체의 파일은 복사하지 않습니다. 버전을 지정해서 복사하므로, view를 만든 후 원본 asset이 갱신되어도, 학습은 view를 만든 시점의 버전을 읽습니다.
4. 가져오기가 끝나면 poller는 실행 디렉터리에 manifest를 `curation_manifest.json`으로 쓰고, 학습에 사용할 dataset root인 `view_dataset/`을 만듭니다. `view_dataset/meta/`는 실제 디렉터리이며, 그 아래의 각 파일과, root 바로 아래의 `data`, `videos` 등은 FSx에 가져온 파일로의 심볼릭 링크입니다. FSx에 가져온 파일은 root 소유이고 학습 job이 쓸 수 없으므로, poller는 학습 job이 `meta/stats.json`을 쓸 수 있는 위치를 실행 디렉터리에 준비합니다.
5. 학습 job은 학습 전에 `compute_view_stats.py`로 남길 episode의 정규화 통계를 계산하고, `view_dataset/meta/stats.json`에 씁니다. 수치 feature(`observation.state`, `action` 등)는 남긴 episode의 행에서 LeRobot의 `get_feature_stats`로 계산합니다. 이미지와 동영상 feature는 `meta/episodes`에 있는 episode별 통계를 LeRobot의 `aggregate_stats`로 합칩니다.
6. `mlflow_train.py`는 manifest의 `episodes`를 `--dataset.episodes`로 `lerobot-train`에 넘깁니다. episode 번호는 원본 dataset의 `episode_index` 그대로입니다.

학습 job이 정규화 통계를 다시 계산하는 이유는, LeRobot 0.6.1의 `lerobot-train`이 episode를 지정하더라도 `meta/stats.json`을 그대로 정규화에 사용하기 때문입니다.
view의 `meta/stats.json`은 표시와 참고를 위한 값으로만 취급하고, 학습에는 사용하지 않습니다.
LeRobot의 `lerobot-edit-dataset`의 통계 재계산도 사용하지 않습니다.
LeRobot v3.0의 data 파일은 여러 episode의 행을 가지므로, 이 재계산은 같은 파일에 있는 제외된 episode의 행도 통계에 포함시켜 버리기 때문입니다.
openpi의 `compute_norm_stats`도 사용하지 않습니다.
openpi 학습용 `norm_stats.json`을 쓰는 것이며, PASK의 VAMS 연동 학습(LeRobot의 `lerobot-train`에 의한 pi0와 pi0.5)은 이 파일을 읽지 않기 때문입니다.

episode 목록은 `slurm_train.sh`의 명령줄에 넣지 않고, `mlflow_train.py`가 프로세스 안에서 인수에 추가합니다. Linux에서는 인수 하나가 128 KiB를 넘을 수 없기 때문입니다.
episode가 수만 개이면 `srun`의 인수에 들어가지 않을 가능성이 있습니다.

view의 학습 job에는 `HF_HUB_OFFLINE=1`을 전달하지 않습니다.
dataset 파일이 부족한 경우에도, dataset을 Hugging Face Hub에서 가져오는 일은 없습니다. view dataset의 `repo_id`(`vams/<databaseId>/<assetId>`)는 세 부분으로 나뉘어 있으므로, `huggingface_hub`이 Hub와 통신하기 전에 `HFValidationError`로 멈추기 때문입니다.
사전 학습 가중치와 토크나이저는, 일반 학습과 마찬가지로, cluster의 `LEROBOT_PI_HF_HOME` 캐시에 없으면 Hub에서 가져옵니다.

## 데이터 규모의 기준과 규모가 커질 때의 대안

현재 구현은 poller가 하나의 프로세스로, 객체마다 S3의 서버 측 복사를 수행합니다(객체 8개를 병렬로 복사하고, 5 GiB를 넘는 객체는 멀티파트로 복사합니다).
동작을 확인한 것은 약 100 MB, 파일 11개의 dataset입니다.
한 번의 학습 데이터가 커지면, 다음 이유로 실행이 늦어지거나 실패할 가능성이 있습니다.

- 복사에 걸리는 시간은 객체 수와 전체 크기에 비례하여 늘어납니다. 이 시간은 Slurm 대기 및 학습 시간과 함께 VAMS pipeline의 `taskTimeout`(포함된 `hyperpod/vams/pipeline.json`에서는 432000초(5일), VAMS의 상한은 7일)에 포함됩니다.
- 복사 도중 poller가 멈추면, 재개된 poller는 모든 객체를 다시 복사합니다. 긴 복사의 끝 무렵에 멈추면, 그때까지의 시간이 그대로 낭비됩니다.
- poller는 실행을 하나씩 처리하므로, 복사 중에는 다른 실행의 접수 후 처리가 기다리게 됩니다.
- FSx 자동 가져오기는 파일의 메타데이터만 가져오고, 내용은 학습 job이 처음 읽을 때 S3에서 읽어옵니다. 데이터가 크면 첫 epoch가 느려집니다.
- FSx 용량은 1200 GiB로 고정되어 있습니다. 복사 전에, dataset 합계와 checkpoint 예상치(`FINETUNE_MODE`가 `full`인 경우 하나당 약 40 GiB)가 여유 공간에 들어가는지 확인하고, 들어가지 않으면 `PASK.StagingFailed`로 실패시킵니다.

한 번의 학습 입력이 다음 중 하나에 해당하면 복사 방법을 재검토하세요.
아래 값은 위의 이유에서 추정한 기준이며, 측정해서 확인한 값이 아닙니다.

| 재검토 기준 | 이유 |
|---|---|
| 합계가 100 GiB를 넘음 | 복사와 첫 epoch의 읽기에 수십 분에서 수 시간이 걸리고, 중간에 멈췄을 때 다시 하는 비용이 커집니다 |
| 객체가 1만 개를 넘음 | 객체마다의 복사와 가져오기 확인(가져오지 않은 파일의 유무와 크기를 10초마다 확인합니다)이 병목이 됩니다 |
| dataset과 checkpoint의 합계가 FSx 여유 공간(수백 GiB)에 근접 | 용량 확인에서 실패하거나, 학습 중에 checkpoint를 쓸 수 없게 됩니다 |

재검토의 대안은 다음과 같습니다.
작은 변경으로 끝나는 것부터 나열합니다.

1. dataset 폴더만 복사: 학습 파라미터에 asset 내의 상대 경로를 추가하고, 그 아래만 복사합니다. 학습에 쓰지 않는 파일(원본 rosbag 등)을 복사하지 않게 됩니다.
2. 복사가 끝난 객체 건너뛰기: 복사 대상에 같은 크기와 ETag의 객체가 있으면 복사하지 않도록 하여, 멈춘 후 이어서 재개할 수 있게 합니다.
3. S3 Batch Operations로 복사: poller는 목록으로 manifest를 만들어 job을 시작하고, 완료를 감시하기만 합니다. 수백만 개의 객체까지 다룰 수 있고, 재시도와 완료 보고는 S3 쪽에 맡길 수 있습니다. 객체당 크기 상한은 도입 전에 확인해야 합니다.
4. AWS DataSync로 복사: TB 규모의 데이터에서, 전송 검증과 차분 전송이 필요한 경우에 사용합니다.
5. 학습 전에 FSx 읽기를 마치기: 복사 후 `lfs hsm_restore` 또는 data repository task로 내용을 읽어 두어, 첫 epoch의 지연을 학습 시간에서 제외합니다.
6. FSx 용량을 늘리거나, 실행마다 FSx를 준비하기: dataset과 checkpoint가 고정된 1200 GiB에 들어가지 않는 경우에 검토합니다.

VAMS의 asset 버킷을 FSx에 직접 연결해서 복사를 없애는 방안은, 위의 "데이터를 VAMS 버킷과 분리해서 복사하는 이유" 절에서 설명한 문제가 있으므로 채택하지 않습니다.

## 권한과 보안

poller는 login 노드의 인스턴스 role로 AWS에 액세스합니다.
이미 만들어 둔 cluster에서는, HyperPod가 인스턴스 그룹의 role 변경을 받지 않으므로, `Vams.SeparateLoginRole`을 `false`로 설정하여 모든 그룹에 공통인 role로 poller의 권한을 부여합니다.
login용 role을 분리할 수 있는 것은 cluster를 새로 만드는 경우뿐입니다.

login 노드에는 poller 외에도 사람이 SSH로 접속합니다.
login 노드에 SSH로 접속한 사람은, 인스턴스 role을 통해 poller와 같은 다음 권한을 사용할 수 있습니다.

- VAMS asset 버킷의 읽기, 그리고 실행용 버킷의 outputs prefix(`*pipelines/*/output/*`)로의 쓰기
- 이름이 `vams-`로 시작하거나 `*<ConfigName>*`에 일치하는 Step Functions state machine에 대한 `SendTaskSuccess`, `SendTaskFailure`, `SendTaskHeartbeat`
- PASK 큐의 메시지 수신과 삭제, 실행 상태 테이블의 읽기와 쓰기
- PASK 데이터 버킷의 `<S3Prefix>vams/`에 대한 읽기, 쓰기, 삭제

login 노드에 SSH할 수 있는 사람(login 노드에 대한 `ssm:StartSession`을 허가받은 사람)을, 이 권한을 가져도 되는 사람으로 제한하세요.
`SeparateLoginRole`이 `true`인 경우, controller와 worker 노드의 role에는 이 권한이 없습니다.
학습 job은 worker 노드에서 실행되므로, 학습 코드에서 VAMS 버킷에 액세스할 수 없습니다.

`SeparateLoginRole`이 `false`인 경우, 모든 그룹이 공통 role을 사용하므로, controller와 worker 노드, 그리고 그곳에서 실행되는 학습 코드도 위의 권한을 사용할 수 있습니다.
cluster의 어떤 노드든 SSH할 수 있는 사람을, 위의 권한을 가져도 되는 사람으로 제한하세요.

VAMS의 어떤 버킷에도 삭제 권한(`s3:DeleteObject`)은 부여하지 않습니다.
poller가 삭제할 수 있는 것은 PASK 데이터 버킷의 `<S3Prefix>vams/` 아래뿐입니다.

view를 입력으로 하는 실행에서는, poller는 원본 asset의 파일을 기록된 버전을 지정해서 읽습니다.
버전을 지정한 읽기에 필요한 권한은 `s3:GetObjectVersion`입니다.
poller의 role은 `Vams.AssetBucketNames`의 모든 버킷에 이 권한을 가지므로, view를 위해 IAM을 변경할 필요는 없습니다.

PASK는 VAMS의 권한(Tier 2 제약 등)을 판정하지 않습니다.
view와 원본 asset이 같은 database에 있고, VAMS의 권한이 같은 조건으로 적용된다고 가정합니다.
view의 manifest는 view asset의 파일이므로, 그 asset에 쓸 수 있는 사람은 같은 asset 버킷에 있는 다른 database의 asset을 가리키는 manifest를 둘 수 있습니다.
poller의 role은 asset 버킷 전체를 읽을 수 있으므로, IAM만으로는 이 지정을 막을 수 없습니다.
그래서 poller는 manifest의 `source.storage.bucket`이 입력 asset의 버킷과 같고, `source.database_id`가 입력 asset의 database와 같은지를 확인합니다.
다르면 `PASK.InvalidRequest`로 실패시킵니다.

학습 파라미터는 허용 목록과 정규 표현식으로 검증하고, 셸을 거치지 않고 `subprocess`의 인수와 환경 변수로 학습 job에 전달합니다.
pipelineExecutionId는 문자 종류(`^[A-Za-z0-9_-]+$`)를 검증한 뒤에 job 이름, S3 키, FSx 경로에 사용합니다.

poller의 설정 파일 `/etc/pask/vams-poller.env`는 소유자가 root, 그룹이 ubuntu이고, 권한은 0640입니다.
`HF_TOKEN`을 가지는 학습용 env 파일 `~/.config/pask/lerobot-openpi.env`는 ubuntu만 읽을 수 있습니다(0600).

## 현재 구현의 제약

- 동시에 처리하는 실행은 1건입니다(`Vams.MaxConcurrentRuns`). 나중에 들어온 실행은, 앞선 실행이 끝날 때까지 접수 상태로 대기합니다.
- dataset은 asset의 root에 있어야 합니다. 하위 폴더에 dataset이 있는 asset은 그대로는 학습에 사용할 수 없습니다.
- FSx에 가져온 파일은 root 소유가 되므로, 학습 job은 dataset 안에 쓸 수 없습니다. `meta/stats.json`에 q01과 q99가 없는 dataset은, 분위수를 계산할 수 없어 실패할 수 있습니다. VAMS의 rosbag에서 LeRobot으로의 변환 출력에는 q01과 q99가 포함됩니다.
- 학습된 모델은 출력 asset의 root에 놓입니다.
- view는 원본 asset의 파일을 기록된 버전으로 가리킵니다. 원본 asset을 삭제하거나, 기록된 버전을 삭제하면, 그 view로는 학습할 수 없게 됩니다(`PASK.InvalidRequest`). view로 학습하는 동안에는 원본 asset과 그 버전을 삭제하지 마세요.
- view manifest의 형식은 `curation_manifest_version` 2의 계약에 맞춰져 있습니다. VAMS가 manifest의 형식을 바꾸면, poller의 검증도 맞춰 수정해야 합니다.
- view에서 받는 것은 LeRobot의 `codebase_version`이 `v3.0`인 dataset뿐입니다. 그 외의 버전은 정규화 통계 계산에서 실패합니다(`PASK.JobFailed`).
- view dataset의 파일이 FSx에 부족하면, 학습은 dataset 읽기 단계에서 실패합니다(`PASK.JobFailed`). `repo_id`가 세 부분으로 나뉘어 있으므로, Hugging Face Hub와 통신하기 전에 `HFValidationError`로 멈춥니다.
