> **참고:** 이 문서는 AI로 자동 번역되었습니다. 정확한 내용은 [영어판](../en/mlflow_tracking.md) 또는 [일본어판](../ja/mlflow_tracking.md)을 확인하십시오.

# MLflow를 이용한 training 기록

이 sample의 training job은 training 설정, metrics, LoRA adapter를 Amazon SageMaker AI의 **MLflow App**에 기록합니다.
이 문서에서는 MLflow App의 구성, 기록 방식, run에 기록되는 내용을 설명합니다.
환경 변수 설정, job 제출, MLflow UI 여는 방법은 [LeRobot을 이용한 π0.5 training 절차](lerobot_pi05_training.md#mlflow-tracking)를 참조하십시오.

## 구성

MLflow App은 Amazon SageMaker AI가 server를 관리하는 MLflow tracking server입니다.
이 repository의 CDK(`hyperpod/lib/constructs/mlflow.ts`)가 MLflow App과 관련 resource를 HyperPod stack과 함께 생성합니다.

| Resource | 내용 |
|---|---|
| MLflow App | 이름은 `pask-mlflow`, MLflow 버전은 3.10입니다. 주간 maintenance는 일요일 18:00(UTC)에 시작됩니다. model 자동 등록은 비활성화되어 있습니다 |
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

## run에 기록되는 내용

### params

training 설정 전체를 점으로 구분된 key로 펼쳐 params에 기록합니다(예: `dataset.repo_id`, `peft.r`, `optimizer.lr`, `steps`).
π0.5의 LoRA training에서는 약 140개가 됩니다.
값 하나는 6000자까지이며, 초과한 부분은 잘립니다.

MLflow의 params는 기록한 후 변경할 수 없습니다.
그래서 params는 run을 생성할 때만 기록하고, 재개할 때는 기록하지 않습니다.
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

LoRA adapter는 세 file의 합계가 `MLFLOW_ADAPTER_MAX_MB`(기본값 200) 이하이고 `MLFLOW_DISABLE_ARTIFACT`가 `false`일 때만 기록합니다.
`LORA_R=16`의 adapter는 약 5 MB입니다.
`FINETUNE_MODE=expert_only`와 `full`의 checkpoint에는 adapter가 없고 weight 전체가 수 GB가 되므로 MLflow에는 기록하지 않습니다.
이 weight는 tag `pask.last_checkpoint_dir`의 경로로 FSx의 checkpoint를 참조합니다.

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
| `slurm.job_id` | 마지막으로 이 run에 기록한 job의 ID |
| `slurm.job_ids` | 이 run에 기록한 모든 job의 ID(쉼표 구분) |

`lerobot.pretrained_path`를 tag로 따로 남기는 이유는, 재개하면 LeRobot이 설정의 `pretrained_path`를 checkpoint 경로로 바꾸기 때문입니다.
`slurm.job_ids`를 보면 재개를 포함하여 어떤 job의 log를 확인하면 되는지 알 수 있습니다.

experiment에는 tag `mlflow.experimentKind=custom_model_development`를 붙입니다.
MLflow 3.10의 UI에서는 이 tag를 붙여도 표시가 바뀌지 않습니다.
model training용 표시는 UI의 전환으로 선택합니다.
