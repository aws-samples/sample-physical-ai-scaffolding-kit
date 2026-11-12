> **참고:** 이 문서는 AI로 자동 번역되었습니다. 정확한 내용은 [영어판](../en/mlflow_tracking.md) 또는 [일본어판](../ja/mlflow_tracking.md)을 확인하십시오.

# MLflow를 이용한 training 기록

이 sample의 training job은 training 설정, metrics, LoRA adapter를 Amazon SageMaker AI의 **MLflow App**에 기록합니다.
이 문서에서는 MLflow App의 구성, 기록 방식, run에 기록되는 내용을 설명합니다.
환경 변수 설정, job 제출, MLflow UI 여는 방법은 [LeRobot을 이용한 π0.5 training 절차](lerobot_pi05_training.md#mlflow-tracking)를 참조하십시오.

## 구성

MLflow App은 Amazon SageMaker AI가 server를 관리하는 MLflow tracking server입니다.
이 repository의 CDK(`hyperpod/lib/constructs/mlflow.ts`)가 MLflow App과 관련 resource를 HyperPod stack과 함께 생성합니다.
기본값으로는 생성하지 않으므로 `hyperpod/cdk.json`의 `Mlflow.Enabled`를 `true`로 하여 배포합니다.

| Resource | 내용 |
|---|---|
| MLflow App | 이름은 `pask-mlflow`입니다. MLflow 버전은 지정하지 않으므로 생성 시점의 SageMaker AI 기본 버전이 됩니다(2026년 11월 기준 3.16). 주간 maintenance는 일요일 18:00(UTC)에 시작됩니다. model 자동 등록은 비활성화되어 있습니다 |
| artifact용 S3 bucket | artifact를 `s3://<bucket>/mlflow` 아래에 저장합니다. FSx for Lustre와 연결된 Data bucket과는 별도의 bucket입니다. stack을 삭제해도 bucket은 남습니다 |
| MLflow App의 service role | MLflow App이 artifact용 bucket을 읽고 쓰기 위한 role입니다 |
| ExecutionRole에 대한 권한 | HyperPod node가 MLflow App API(`sagemaker:CallMlflowAppApi`)를 호출하고 artifact용 bucket을 읽고 쓰기 위한 권한입니다 |

stack의 Outputs에는 MLflow App의 ARN(`MlflowAppArn`)과 artifact용 bucket 이름(`MlflowArtifactBucketName`)이 출력됩니다.
HyperPod node는 기존 NAT Gateway를 경유하여 MLflow App의 public endpoint에 HTTPS로 연결합니다.
인터넷 연결 없이 사용하려면 interface VPC endpoint `aws.sagemaker.<region>.mlflow`와 SageMaker API endpoint를 추가하고 private DNS를 활성화합니다.

## 기록 방식

LeRobot에는 MLflow에 기록하는 기능이 없고, W&B(Weights & Biases)에 기록하는 기능만 있습니다.
따라서 W&B에 기록하는 logger를 MLflow에 기록하는 logger로 교체하여 training을 실행합니다.
LeRobot 자체는 변경하지 않습니다.

기록은 다음 순서로 진행됩니다.

1. `setup.sh`가 stack의 Outputs에서 MLflow App의 ARN을 읽어 `~/.bashrc`에 `MLFLOW_TRACKING_URI`로 기록합니다.
2. `MLFLOW_TRACKING_URI`가 설정되어 있으면 `slurm_train.sh`는 `lerobot-train` 대신 `training/mlflow_train.py`를 실행합니다.
3. `mlflow_train.py`는 LeRobot의 `WandBLogger`를 `MlflowLogger`로 교체한 후 `lerobot-train`과 같은 처리를 호출합니다.
4. training loop가 W&B logger에 전달하는 값을 `MlflowLogger`가 MLflow App에 보냅니다.

container 안의 MLflow client는 plugin `sagemaker-mlflow`를 사용하여 MLflow App의 ARN을 tracking URI로 다루고, request에 SigV4 서명을 붙입니다.
서명에 사용하는 자격 증명은 HyperPod node의 ExecutionRole입니다.
access key나 token을 job에 전달할 필요가 없습니다.

LeRobot의 training loop가 호출하는 logger method와 MLflow에 기록되는 내용의 대응은 다음과 같습니다.

| LeRobot이 호출하는 method | 호출 시점 | MLflow에 기록되는 내용 |
|---|---|---|
| `log_dict` | `LOG_FREQ` step마다, 평가 후 | metrics `train/*`, `eval/*` |
| `log_policy` | checkpoint를 저장했을 때 | metrics `checkpoint_step`, tag `pask.last_checkpoint_dir`, 마지막 step의 LoRA adapter |
| `log_video` | 평가에서 동영상을 만들었을 때 | artifact `videos/eval/` |

## Experiment와 run

experiment는 run을 묶는 단위입니다.
새 run은 `MLFLOW_EXPERIMENT_NAME`의 experiment(기본값 `lerobot-pi`)에 생성됩니다.

run은 한 번의 training에 해당합니다.
run 이름은 job 이름(`slurm_train.sh`의 첫 번째 인수)입니다.
같은 job 이름으로 제출해도 MLflow는 다른 run ID의 run을 생성합니다.

`mlflow_train.py`는 run ID를 checkpoint의 `train_config.json`의 `wandb.run_id`에 저장합니다.
`RESUME=true`로 training을 재개하면 이 run ID의 run을 열어 기록을 이어 갑니다.
따라서 48시간 제한 시간으로 멈춘 training을 재개해도 metrics는 하나의 run에 이어서 기록됩니다.

run의 상태는 job이 끝나는 방식에 따라 정해집니다.

| job이 끝나는 방식 | run의 상태 |
|---|---|
| training이 끝까지 진행됨 | `FINISHED` |
| training이 error로 멈춤 | `FAILED` |
| `scancel`이나 제한 시간으로 중지됨 | `KILLED` |

### VAMS에서 시작한 run

VAMS workflow에서 시작한 job에는 poller가 환경 변수 `VAMS_CONTEXT_FILE`로 `vams_context.json`의 경로를 전달합니다.
`mlflow_train.py`는 이 환경 변수가 설정되어 있으면 VAMS에서의 실행, 설정되어 있지 않으면 수동 실행으로 간주합니다.
설정되어 있는데 file을 읽을 수 없거나 JSON이 올바르지 않으면 run을 만들지 않고 training을 시작하기 전에 실패합니다.
입력의 이력이 없는 run을 남기지 않기 위해서입니다.

experiment는 poller가 `MLFLOW_EXPERIMENT_NAME`으로 전달하는 VAMS 전용 experiment(기본값 `vams-training`)입니다.

한 번의 VAMS 실행(`pipelineExecutionId`)에는 run을 하나만 만듭니다.
`mlflow_train.py`는 새 run을 만들기 전에 experiment 안에서 tag `vams.pipeline_execution_id`가 같은 run을 찾습니다.
찾은 경우에는 그 run을 열어 기록을 이어 갑니다.
이 경우 params는 이미 기록되어 있어 변경할 수 없으므로 params와 `train_config.json`은 기록하지 않습니다.

run을 시작하면 `vams_context.json`의 `resultFile`이 가리키는 file(`mlflow_run.json`)에 run으로의 역링크를 씁니다.
poller는 job이 끝난 후 이 file을 읽어 run ID를 VAMS에 보내는 결과에 넣습니다.
임시 file에 쓴 후 이름을 바꾸므로 poller가 쓰는 중인 file을 읽는 일은 없습니다.

```json
{"run_id": "...", "experiment_id": "...", "tracking_uri": "..."}
```

`vams_context.json`의 형식과 poller가 수행하는 처리는 [VAMS 연동 사양](../../../../hyperpod/docs/ko/VAMS_INTEGRATION/SPEC.md#mlflow에-기록하는-것)을 참조하십시오.

## run에 기록되는 내용

### params

training 설정 전체를 점으로 구분된 key로 펼쳐 params에 기록합니다(예: `dataset.repo_id`, `peft.r`, `optimizer.lr`, `steps`).
π0.5의 LoRA training에서는 약 140개가 됩니다.
값 하나는 6000자까지이며, 초과한 부분은 잘립니다.

MLflow의 params는 기록한 후 변경할 수 없습니다.
그래서 params는 run을 생성할 때만 기록하고, 재개할 때와 같은 VAMS 실행의 run을 이어 갈 때는 기록하지 않습니다.
재개할 때 `STEPS`로 총 step 수를 늘려도 params의 `steps`는 처음 값 그대로입니다.

### metrics `train/*`

`LOG_FREQ` step(기본값 100)마다 LeRobot이 training log에 출력하는 값과 같은 값을 기록합니다.
가로축의 step은 training의 step 수입니다.

| metrics | 내용 | training log에서의 표기 |
|---|---|---|
| `train/loss` | training의 loss | `loss` |
| `train/grad_norm` | gradient의 norm | `grdn` |
| `train/lr` | learning rate | `lr` |
| `train/update_s` | 1 step의 weight 갱신에 걸린 초 | `updt_s` |
| `train/dataloading_s` | 1 step의 data 읽기에 걸린 초 | `data_s` |
| `train/samples_per_s` | 1초당 처리한 sample 수 | `smp/s` |
| `train/gpu_mem_gb` | GPU memory 사용량(GB) | `mem_gb` |
| `train/steps` | step 수 | `step` |
| `train/samples` | training 시작 이후 처리한 sample 수 | `smpl` |
| `train/episodes` | 처리한 sample 수를 dataset의 episode 수로 환산한 값 | `ep` |
| `train/epochs` | 처리한 sample 수를 dataset 전체를 몇 바퀴 돌았는지로 환산한 값 | `epch` |

training이 잘 진행되는지는 먼저 `train/loss`가 내려가는지로 판단합니다.
`train/dataloading_s`가 `train/update_s`에 비해 크면 data 읽기가 training 속도를 제한하고 있습니다.
이 경우 `NUM_WORKERS`를 늘리면 빨라질 수 있습니다.

### metrics `eval/*`

LeRobot의 평가를 `EXTRA_ARGS`로 활성화한 경우에만 평가 결과를 `eval/*`에 기록합니다.
이 sample의 기본 설정에서는 평가를 실행하지 않으므로 기록되지 않습니다.

### metrics `checkpoint_step`

checkpoint를 저장한 step 수를 그 step의 값으로 기록합니다.
`SAVE_FREQ`(기본값 5000)마다 한 점씩 늘어납니다.
마지막으로 저장한 checkpoint의 directory는 tag `pask.last_checkpoint_dir`로 알 수 있습니다.

### metrics `system/*`

MLflow의 system metrics 기능으로 node의 사용 상황을 `MLFLOW_SYSTEM_METRICS_SAMPLING_INTERVAL`초(기본값 10)마다 기록합니다.
가로축은 training의 step이 아니라 system metrics를 수집한 횟수입니다.

| metrics | 내용 |
|---|---|
| `system/gpu_0_utilization_percentage` | GPU 사용률(%) |
| `system/gpu_0_memory_usage_megabytes` | GPU memory 사용량(MB) |
| `system/gpu_0_memory_usage_percentage` | GPU memory 사용률(%) |
| `system/gpu_0_power_usage_watts` | GPU 소비 전력(W) |
| `system/gpu_0_power_usage_percentage` | GPU 소비 전력의 상한 대비 비율(%) |
| `system/cpu_utilization_percentage` | CPU 사용률(%) |
| `system/system_memory_usage_megabytes` | node의 memory 사용량(MB) |
| `system/system_memory_usage_percentage` | node의 memory 사용률(%) |
| `system/disk_usage_megabytes` | disk 사용량(MB) |
| `system/disk_usage_percentage` | disk 사용률(%) |
| `system/disk_available_megabytes` | disk 여유 공간(MB) |
| `system/network_receive_megabytes` | 기록 시작 이후 수신한 data 양(MB) |
| `system/network_transmit_megabytes` | 기록 시작 이후 송신한 data 양(MB) |

GPU metrics의 `gpu_0`은 GPU 번호입니다.
이 sample의 training은 GPU를 1장만 사용하므로 `gpu_0`만 기록됩니다.
`system/gpu_0_utilization_percentage`가 낮으면 `train/dataloading_s`와 함께 data 읽기 때문에 GPU가 기다리고 있지 않은지 확인합니다.

### artifact

| artifact | 내용 | 기록 시점 |
|---|---|---|
| `train_config.json` | training 설정 전체 | run을 생성했을 때 |
| `adapter/<step>/` | LoRA adapter(`adapter_model.safetensors`, `adapter_config.json`, `config.json`) | 마지막 step(`STEPS`)의 checkpoint를 저장했을 때 |
| `videos/eval/` | 평가 동영상 | 평가를 활성화한 경우에만 |
| `vams/input_manifest.json` | 입력 file 목록(bucket, key, versionId, ETag, size). curation view인 경우 원본 asset의 file 목록과 원본 asset의 정보(`source`) | VAMS에서 시작한 run을 시작했을 때 |
| `vams/curation_manifest.json` | curation view의 manifest | VAMS에서 curation view로 시작한 run을 시작했을 때 |
| `vams/norm_stats.json` | training에 사용한 정규화 통계. `compute_view_stats.py`가 남긴 episode로 계산한 `meta/stats.json`입니다 | VAMS에서 curation view로 시작한 run을 시작했을 때 |

LoRA adapter는 세 file의 합계가 `MLFLOW_ADAPTER_MAX_MB`(기본값 200) 이하이고 `MLFLOW_DISABLE_ARTIFACT`가 `false`일 때만 기록합니다.
`LORA_R=16`의 adapter는 약 5 MB입니다.
`FINETUNE_MODE=expert_only`와 `full`의 checkpoint에는 adapter가 없고 weight 전체가 수 GB가 되므로 MLflow에는 기록하지 않습니다.
이 weight는 tag `pask.last_checkpoint_dir`의 경로로 FSx의 checkpoint를 참조합니다.

### dataset

VAMS에서 시작한 run에서는 입력 dataset을 `mlflow.log_input`으로 run의 dataset으로 등록합니다.
수동 실행에서는 등록하지 않습니다.

| 항목 | 값 |
|---|---|
| source | `s3://<input.bucket>/<input.assetRootS3Key>`(VAMS asset의 root) |
| name | asset 이름(`input.assetName`). 없으면 asset ID(`input.assetId`) |
| digest | `datasetDigest`의 16진수 앞 36자 |
| context | `training` |

입력이 curation view인 경우 source는 원본 asset의 root(`s3://<view.source.bucket>/<view.source.assetRootS3Key>`)입니다.
view asset에는 training에 사용할 file이 없고, training할 file은 원본 asset에 있기 때문입니다.
name은 view asset의 이름(없으면 view asset의 ID) 그대로입니다.

MLflow는 dataset의 digest를 36자까지로 제한하므로 `sha256:<hex>` 형식의 값은 그대로 들어가지 않습니다.
그래서 dataset의 digest에는 16진수 앞 36자를 사용하고, 전체 값은 tag `vams.dataset_digest`에 남깁니다.

dataset 등록에 실패해도 경고를 log에 출력하고 training을 계속합니다.
입력의 이력은 tag `vams.*`와 artifact `vams/input_manifest.json`에 남기 때문입니다.

### tag

run에는 training 조건과 실행 환경을 나타내는 tag를 붙입니다.
값이 비어 있는 tag는 붙이지 않습니다.

| tag | 값 |
|---|---|
| `lerobot.version` | LeRobot의 버전 |
| `lerobot.policy_type` | `pi05` 또는 `pi0` |
| `lerobot.finetune_mode` | `lora`, `expert_only`, `full` 중 하나 |
| `lerobot.dataset_repo_id` | dataset의 repository ID |
| `lerobot.pretrained_path` | base weight. run을 생성할 때만 기록합니다 |
| `pask.output_dir` | 출력 directory |
| `pask.last_checkpoint_dir` | 마지막으로 저장한 checkpoint의 directory |
| `pask.image_tag` | container image의 tag(`IMAGE_TAG`) |
| `pask.cluster` | HyperPod cluster 이름 |
| `pask.run_type` | `train` |
| `pask.trigger` | VAMS에서 시작한 run은 `vams`, 수동 실행은 `manual` |
| `slurm.job_id` | 마지막으로 이 run에 기록한 job의 ID |
| `slurm.job_ids` | 이 run에 기록한 모든 job의 ID(쉼표 구분) |

VAMS에서 시작한 run에는 다음 tag도 붙입니다.
값은 `vams_context.json`에서 가져옵니다.

| tag | 값 |
|---|---|
| `vams.workflow_database_id` | workflow의 database ID(`workflowDatabaseId`) |
| `vams.workflow_id` | workflow의 ID(`workflowId`) |
| `vams.workflow_execution_id` | workflow 실행의 ID(`workflowExecutionId`) |
| `vams.pipeline_execution_id` | pipeline 실행의 ID(`pipelineExecutionId`). 같은 실행의 run을 찾는 데 사용합니다 |
| `vams.pipeline_name` | pipeline의 이름(`pipelineName`) |
| `vams.executing_user_name` | workflow를 실행한 사용자의 이름(`executingUserName`) |
| `vams.input.database_id` | 입력 asset의 database ID(`input.databaseId`) |
| `vams.input.asset_id` | 입력 asset의 ID(`input.assetId`) |
| `vams.input.asset_name` | 입력 asset의 이름(`input.assetName`) |
| `vams.input.versioned` | 입력 file의 버전을 고정할 수 있었는지(`true` 또는 `false`). asset bucket의 versioning이 비활성화되어 있으면 `false`입니다 |
| `vams.input.kind` | 입력의 종류(`input.kind`). curation view이면 `view`, 일반 dataset이면 `dataset` |
| `vams.output.asset_id` | training한 model을 가져올 asset의 ID(`output.assetId`) |
| `vams.dataset_digest` | 입력 file의 버전으로 구한 digest(`sha256:<hex>`) |

입력이 VAMS의 curation view인 경우 `vams.input.*` tag는 view asset의 값입니다.
view가 가리키는 원본 asset과 남긴 episode는 다음 tag로 기록합니다.
값은 `vams_context.json`의 `view`에서 가져옵니다.

| tag | 값 |
|---|---|
| `vams.source.database_id` | 원본 asset의 database ID(`view.source.databaseId`) |
| `vams.source.asset_id` | 원본 asset의 ID(`view.source.assetId`) |
| `vams.source.asset_version` | 원본 asset의 버전(`view.source.assetVersion`). 없으면 붙이지 않습니다 |
| `vams.view.episode_count` | training에 사용한 episode의 수(`view.episodeCount`) |
| `vams.view.excluded_episode_count` | view에서 제외한 episode의 수(`view.excludedEpisodeCount`) |
| `vams.view.review_updated_at` | view manifest의 `source.review_updated_at`(`view.reviewUpdatedAt`). 없으면 붙이지 않습니다 |

`lerobot.pretrained_path`를 tag로 따로 남기는 이유는, 재개하면 LeRobot이 설정의 `pretrained_path`를 checkpoint 경로로 바꾸기 때문입니다.
`slurm.job_ids`를 보면 재개를 포함하여 어떤 job의 log를 확인하면 되는지 알 수 있습니다.

experiment에는 tag `mlflow.experimentKind=custom_model_development`를 붙입니다.
MLflow 3.10의 UI에서 확인한 시점에서는 이 tag를 붙여도 표시가 바뀌지 않았습니다.
model training용 표시는 UI의 전환으로 선택합니다.
