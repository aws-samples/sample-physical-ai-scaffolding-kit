> **참고:** 이 문서는 AI로 자동 번역되었습니다. 정확한 내용은 [영어판](../en/training_advisor.md) 또는 [일본어판](../ja/training_advisor.md)을 확인하십시오.

# MLflow training 결과를 바탕으로 한 다음 training 파라미터 제안

이 sample의 training이 MLflow App에 기록한 결과를 진단하고, 다음 training 파라미터를 제안하며, 리포트를 작성하는 구조를 Claude Code project skill `pask-training-advisor`로 제공합니다.
이 문서에서는 구조, 진단 기준, 제안의 형식과 검증, 리포트 보는 방법을 설명합니다.
명령어 사용법과 절차는 [skill 절차서](../../../../.claude/skills/pask-training-advisor/SKILL.md)를 참조하십시오.
MLflow에 기록되는 params, tags, metrics의 의미는 [MLflow를 이용한 training 기록](mlflow_tracking.md)을 참조하십시오.

## 개요

판단의 근거는 MLflow에 이미 기록된 training 결과(metrics 이력, params, tags)와, VAMS에 연결할 수 있는 경우 training에 사용한 dataset의 메타데이터(episode 수, 품질 플래그, 리뷰)입니다.
시뮬레이션 환경에서의 closed-loop 평가는 수행하지 않습니다.
따라서 진단이 보여주는 것은 training data에서의 loss 추이이며, 로봇 동작의 좋고 나쁨이 아닙니다.

제안은 VAMS execute API의 `pipelineExecutionParameters`(`templateId`와 `templateTags`)에 그대로 전달할 수 있는 JSON(`vams_parameters.json`)으로 출력합니다.
skill은 제안한 training을 실행하지 않습니다.
사람이 제안과 리포트를 확인하고 승인한 뒤, 사람이 VAMS에서 실행합니다.

training에 사용한 dataset에 대해서는, 다음 training에서 제외할 episode와 dataset의 후보를 리포트에 작성합니다.
후보는 삭제 제안이 아니라, 사람이 VAMS의 LeRobot Dataset Curation에서 다음 training의 입력에서 제외하기 위한 제안입니다.
skill은 VAMS의 asset을 삭제하지 않으며, view도 만들지 않습니다.

리포트에는 MLflow의 training 결과가 필수입니다.
VAMS에만 연결할 수 있는 경우와 어느 쪽에도 연결할 수 없는 경우에는 리포트를 만들지 않고 중단합니다.
training에 사용한 dataset은 MLflow run의 tags를 따라가며 확인하므로, MLflow가 없으면 특정할 수 없기 때문입니다.

처리는 결정적인 처리를 수행하는 Python 도구와, 판단을 담당하는 Claude Code로 나뉩니다.
도구는 연결 대상 확인, MLflow와 VAMS에서의 데이터 가져오기, feature와 진단 label 계산, 제외 후보 추출, 제안의 검증과 VAMS 형식으로의 변환, 그림 생성, MLflow artifact 저장을 수행합니다.
Claude Code는 12개 항목 중 무엇을 어떻게 바꿀지 결정하여 근거를 적고, 리포트를 작성합니다.
수치 계산과 제약 검증을 도구에 맡기는 이유는, Claude Code가 수치를 다시 계산하거나 VAMS에서 실패하는 값을 제안하는 것을 막기 위해서입니다.

## 전체 흐름

```mermaid
flowchart LR
  X[MLflow App과 VAMS] -->|detect| Y{mode}
  Y -->|full / mlflow_only| A
  Y -.vams_only / none.-> Z[중단]
  A[MLflow App<br/>params / metrics / tags] -->|fetch| B[snapshot.json]
  B -->|fetch-vams| V[datasets.json]
  B -->|analyze| C[diagnosis.json]
  V -->|analyze| C
  C --> D[Claude Code<br/>다음 파라미터를 판단]
  D --> E[proposal.json]
  E -->|validate-proposal| F[vams_parameters.json]
  B -->|plot| P[PNG 그림]
  B --> R[Claude Code<br/>리포트를 작성]
  C --> R
  F --> R
  P --> R
  R --> G[report.md]
  G -.선택.->|publish| M[MLflow의 artifact]
  G -.승인 후.-> H[사람이 VAMS에서 실행]
```

1. `detect`: MLflow와 VAMS에 연결할 수 있는지 확인하고, `full`(양쪽), `mlflow_only`, `vams_only`, `none` 중 하나를 반환합니다. `vams_only`와 `none`에서는 이후 단계로 진행하지 않습니다.
2. `fetch`: 대상 run과 비교 대상 run을 MLflow에서 가져와 `snapshot.json`에 저장합니다.
3. `fetch-vams`(`full`인 경우): run이 사용한 VAMS asset의 메타데이터를 VamsCLI로 읽어 `datasets.json`에 저장합니다.
4. `analyze`: `snapshot.json`에서 feature를 계산하고, 진단 label과 소견을 `diagnosis.json`에 씁니다. `datasets.json`이 있으면 dataset 진단과 제외 후보도 씁니다.
5. Claude Code가 `diagnosis.json`과 제약을 읽고, 다음 12개 항목의 값과 근거를 `proposal.json`에 씁니다.
6. `validate-proposal`: `proposal.json`을 검증하고, 통과한 경우에만 `vams_parameters.json`을 씁니다. 통과하지 못하면 Claude Code가 `proposal.json`을 고치고, 통과할 때까지 반복합니다.
7. `plot`: `snapshot.json`에서 학습 곡선 그림을 만듭니다.
8. Claude Code가 JSON과 그림을 읽고 `report.md`를 작성합니다.
9. `publish`(선택): `report.md`, 그림, JSON을 대상 run의 artifact에 저장합니다.

`analyze` 이후의 subcommand 중 MLflow에 연결하는 것은 `publish`뿐입니다.
`snapshot.json`과 `datasets.json`이 있으면, MLflow와 VAMS에 연결할 수 없는 환경에서도 진단과 리포트를 재현할 수 있습니다.

비교 대상 run은 사용자가 지정하지 않으면 experiment `vams-training`과 `lerobot-pi`에서, 대상 run과 같은 dataset을 사용했고 status가 `FINISHED`인 run을 최대 5개 고릅니다.
dataset이 같은지는 tag `vams.dataset_digest`로 판정하며, 이 tag가 없는 run에서는 tag `lerobot.dataset_repo_id`로 판정합니다.
experiment와 개수는 사용자가 바꿀 수 있습니다.

## 사용하는 metrics

| metrics | 진단에서의 용도 |
|---|---|
| `train/loss` | 수렴, plateau, 발산, 노이즈 판정 |
| `train/grad_norm` | 발산과 spike 판정 |
| `train/lr` | learning rate schedule의 진행 상황 |
| `train/update_s`, `train/dataloading_s`, `train/samples_per_s` | data 읽기가 학습 속도를 제한하고 있는지 여부 |
| `train/gpu_mem_gb` | GPU memory 사용량 |
| `train/epochs`, `train/samples`, `train/episodes` | dataset을 몇 바퀴 돌았는지, dataset 규모의 추정 |
| `checkpoint_step` | 저장된 checkpoint의 위치 |
| `system/gpu_0_utilization_percentage`, `system/gpu_0_memory_usage_percentage` | GPU 사용률과 memory 여유 |
| `eval/*` | 평가 loss. 기록된 run에만 있음 |

12개 항목의 값은 params(`policy.type`, `steps`, `batch_size`, `peft.r` 등)와 tags(`lerobot.finetune_mode`, `lerobot.pretrained_path`, `pask.image_tag` 등)에서 복원합니다.
재개한 run에서는 params의 `steps`가 처음 값 그대로이므로, 계획상의 총 step 수는 params의 `steps`와 metrics의 마지막 step 중 큰 값으로 취급하며, 그 사실을 `snapshot.json`의 주석에 남깁니다.

dataset의 frame 수와 episode 수는 MLflow에 기록되어 있지 않으므로, `train/samples / train/epochs`와 `train/episodes / train/epochs`에서 추정합니다.

## 진단 기준

`analyze`는 run마다 다음 label 중 해당하는 것을 모두 붙입니다.
`insufficient_data`가 붙은 run에는 곡선 형태의 label(`diverging`, `unstable`, `converging`, `plateau`, `overfit_risk`)을 붙이지 않습니다.
판정에 충분한 점이 없으면 말미의 기울기 등의 feature가 의미를 갖지 않기 때문입니다.
`train/loss`의 기록점이 하나도 없는 run에는 GPU memory label(`gpu_memory_headroom`, `gpu_memory_tight`)도 붙이지 않습니다.
학습이 시작되기 전의 system metrics는 학습에 필요한 memory를 나타내지 않기 때문입니다.

| label | 의미 | 주요 조건(괄호 안은 임계값의 key) |
|---|---|---|
| `insufficient_data` | 판정에 충분한 점이 없음 | `train/loss`의 기록점 수가 하한 미만(`minPoints`) |
| `failed_or_killed` | run이 도중에 멈춤 | status가 `FAILED` 또는 `KILLED`, 또는 계획 step에 대한 완료율이 하한 미만(`minCompletionRatio`) |
| `diverging` | 발산하고 있음 | `train/loss` 또는 `train/grad_norm`에 NaN이나 inf가 있거나, 말미의 `log(loss)` 기울기가 임계값을 넘어 양수이고, 말미의 상대 개선이 음수(`divergingTailSlopePer1k`) |
| `unstable` | 불안정 | spike의 수 또는 말미의 변동 계수가 임계값을 넘음(`spikeFactor`, `maxSpikes`, `maxTailCv`) |
| `converging` | 아직 내려가고 있음 | 말미의 기울기가 음수이고, 말미의 상대 개선이 임계값을 넘음(`convergingTailImprovement`) |
| `plateau` | 충분히 내려가 평탄해짐 | `diverging`이 아니고, 말미의 상대 개선이 임계값 미만(`plateauTailImprovement`) |
| `overfit_risk` | 과학습 가능성 | `converging`이고, 마지막 `train/epochs`가 임계값을 넘음(`overfitEpochs`). `eval/eval_loss`가 기록되어 있으면, `converging`이면서 평가 loss의 말미 기울기가 양수인 경우에도 붙음 |
| `dataloader_bound` | data 읽기가 학습 속도를 제한함 | `train/dataloading_s / train/update_s`의 중앙값이 임계값을 넘음(`dataloaderBoundRatio`) |
| `gpu_memory_headroom` | GPU memory에 여유가 있음 | `system/gpu_0_memory_usage_percentage`의 최대값이 임계값 미만(`gpuMemoryHeadroomPercent`) |
| `gpu_memory_tight` | GPU memory가 빠듯함 | `system/gpu_0_memory_usage_percentage`의 최대값이 임계값을 넘음(`gpuMemoryTightPercent`) |

말미의 기울기와 상대 개선은, 기록점의 말미 window(`tailWindowFraction`)에서 계산합니다.
기울기는 `log(loss)`를 step에 대해 선형 회귀한 1000 step당 값(`tail_slope_per_1k`)이고, 상대 개선은 말미 window의 전반과 후반의 중앙값의 비율에서 구한 값(`tail_improvement`)입니다.

임계값은 [`diagnosis_thresholds.json`](../../../../.claude/skills/pask-training-advisor/scripts/config/diagnosis_thresholds.json)에 있으며, key별 의미는 같은 파일의 `_comments`에 적혀 있습니다.
말미 window는 기록점의 25%이고, `converging`과 `plateau`를 나누는 말미 상대 개선의 임계값은 0.01입니다.
기록점이 30점 정도 이하인 run에서는 말미 window가 짧아, 곡선 형태의 label이 노이즈에 쉽게 영향을 받습니다.
파일을 고치거나 `analyze`에 다른 파일을 전달하면 임계값을 바꿀 수 있습니다.
`diagnosis.json`에는 판정에 사용한 임계값도 남으므로, 나중에 판정의 근거를 확인할 수 있습니다.

과학습은 `eval/*`이 기록되어 있지 않으면 직접 판정할 수 없습니다.
`train/loss`는 training data에서의 loss이며, training data에 과하게 맞춰져도 계속 내려가기 때문입니다.
이 sample의 기본 설정은 평가를 실행하지 않으므로, `overfit_risk`는 dataset의 주회 수(`train/epochs`)에 의한 대리 판정이 됩니다.

## 학습에 사용한 dataset의 진단

`fetch-vams`가 읽는 것은 대상 run과 비교 대상 run이 사용한 asset뿐입니다.
run의 입력(tag `vams.input.asset_id`)과, 입력이 curation view인 경우 view의 원본 dataset(tag `vams.source.asset_id`)이 대상입니다.
database의 다른 asset은 읽지 않습니다.
VamsCLI는 저장된 로그인으로 읽기 명령만 사용하며, 프로파일은 `--vams-profile` 또는 환경 변수 `VAMS_CLI_PROFILE`로 지정합니다.

`fetch-vams`는 asset마다 다음 정보를 `datasets.json`에 저장합니다.

| 정보 | 가져오는 곳 |
|---|---|
| 규모(episode 수, frame 수, 시간, fps, task) | asset의 메타데이터 `lerobot_total_*`, `lerobot_fps`, `lerobot_tasks` |
| 품질(플래그가 선 episode 수와 플래그의 내역) | asset의 메타데이터 `lerobot_quality_*`. VAMS의 LeRobot Episode Quality pipeline이 기록합니다 |
| 리뷰(episode마다의 판정과 제외) | asset의 메타데이터 `lerobot_review_episodes`. VAMS 화면에서의 리뷰가 기록합니다 |
| 통계(`meta/stats.json`의 유무와 frame 수의 일치) | asset의 메타데이터 `lerobot_stats_*` |
| view가 남긴 episode와 제외한 episode | run의 artifact `vams/curation_manifest.json` |
| 같은 asset을 사용한 모든 run의 status | MLflow의 run 검색 |

`analyze`는 `datasets.json`에서 다음 후보를 `diagnosis.json`의 `datasets`에 씁니다.

| 후보 | 이유 | 규칙 |
|---|---|---|
| 데이터셋(다음 training의 입력으로 사용하지 않음) | `only_failed_runs` | 그 asset을 입력으로 한 run이 모두 `FAILED` 또는 `KILLED` |
| | `not_lerobot` | LeRobot dataset도 view도 아님 |
| | `stats_missing` | 정규화 통계가 없음 |
| | `stats_count_mismatch` | 통계의 frame 수가 실제 frame 수와 맞지 않음 |
| | `few_episodes` | episode 수가 임계값(`fewEpisodesMax`, 기본값 1) 이하 |
| episode(view가 남기는 episode에서 제외) | `review_exclude` | 리뷰에서 `exclude`가 붙음 |
| | `review_failure` | 리뷰의 판정이 `failure` |

episode의 후보는, 그 run이 아직 training에 사용한 것에 한합니다.
입력이 view인 경우, view가 이미 제외한 episode는 후보로 하지 않고 `alreadyExcludedEpisodes`로 나누어 남깁니다.
임계값은 [`dataset_thresholds.json`](../../../../.claude/skills/pask-training-advisor/scripts/config/dataset_thresholds.json)에 있습니다.

품질 플래그는 건수만 다루며, 플래그만을 이유로 episode를 후보로 하지 않습니다.
episode마다의 플래그는 asset의 품질 file(`meta/vams_episode_quality.parquet`)에 있고, 메타데이터에는 건수만 있기 때문입니다.
또한 플래그는 triage를 위한 것이며, episode의 성공이나 실패를 나타내지 않습니다.
플래그가 서 있고 리뷰가 없는 경우에는, VAMS 화면에서 리뷰하도록 리포트에서 권장합니다.

dataset 진단은 다음 파라미터의 판단에도 사용합니다.
예를 들어 `few_episodes`인 입력에서 `overfit_risk`가 붙은 경우, `STEPS`를 늘리기보다 episode를 더한 dataset이나 view로 입력을 바꾸는 것을 우선하여 제안합니다.

## 제안의 형식과 검증

제안의 대상은 VAMS에서 training에 전달할 수 있는 다음 12개 항목입니다.

`POLICY_TYPE`, `PRETRAINED_PATH`, `FINETUNE_MODE`, `LORA_R`, `LORA_ALPHA`, `STEPS`, `BATCH_SIZE`, `SAVE_FREQ`, `LOG_FREQ`, `NUM_WORKERS`, `COMPILE_MODEL`, `IMAGE_TAG`

Claude Code는 `proposal.json`에 12개 항목 모두의 값, 이전 run에서 바꾼 항목마다의 이유와 근거(진단 label의 이름이나 `diagnosis.json`의 feature 값), 기대하는 효과, 리스크, 대안을 씁니다.
label과 12개 항목의 대응 기준은 [tuning-guide.md](../../../../.claude/skills/pask-training-advisor/references/tuning-guide.md)에 있습니다.
기준은 권장이며, Claude Code가 기준과 다른 판단을 한 경우에도 이유와 근거가 `proposal.json`에 남습니다.

`validate-proposal`은 제안을 2단계의 제약으로 검증합니다.

| 제약 | 내용 | 정의하고 있는 곳 |
|---|---|---|
| 형식의 제약 | 12개 항목이 모두 있을 것, 허용 목록 밖의 key와 deny list의 key가 없을 것, 값이 poller의 허용 목록 정규식에 맞을 것, 타입과 enum 값이 `tagSchema`에 맞을 것 | poller의 `ALLOWED_TRAINING_PARAMS`(`hyperpod/vams-poller`), [`template.json`](../../../../hyperpod/vams/template.json)의 `tagSchema` |
| 운용의 제약 | 값의 범위, 항목 간 관계(`SAVE_FREQ <= STEPS`, `LOG_FREQ <= SAVE_FREQ`, `POLICY_TYPE`과 `PRETRAINED_PATH`의 조합), 한 번에 바꾸는 항목 수의 상한, 항목 하나의 변화 배율의 상한, 이전 run과 같은 값으로 고정하는 항목 | [`proposal_constraints.json`](../../../../.claude/skills/pask-training-advisor/scripts/config/proposal_constraints.json) |

형식의 제약을 만족하지 않는 제안은, VAMS에서 실행해도 poller가 받아들이지 않습니다.
poller의 허용 목록을 복제하지 않고 import하여 사용하므로, 허용 목록을 바꾸면 검증 규칙도 같은 방식으로 바뀝니다.

운용의 제약은 이 sample의 환경에서 타당한 범위에 제안을 맞추기 위한 것입니다.
현재 값에서는 바꿀 수 있는 항목은 한 번에 3개까지, 정수 항목의 변화는 4배까지이며, `IMAGE_TAG`, `PRETRAINED_PATH`, `POLICY_TYPE`은 이전 run의 값으로 고정합니다.
`FINETUNE_MODE`를 `expert_only`나 `full`로 한 제안은 통과하지만, 24 GB GPU에 들어가지 않을 가능성이 있다는 경고가 나옵니다([GPU 메모리와 finetuning 방식 가이드](lerobot_pi05_training.md#6-gpu-메모리-가이드)).
대안(`alternatives`)도 같은 2단계의 제약으로 검증합니다.
대안의 위반은 제안을 멈추지 않으며, 경고로 나옵니다.
`FINETUNE_MODE`의 경고도 대안에 대해 같은 방식으로 나옵니다.
`STEPS`와 `SAVE_FREQ`의 하한은 poller의 허용 목록과 같은 1로 했습니다.
`STEPS=20`처럼 동작 확인용의 짧은 run에서도, 이전 run의 값을 그대로 둔 제안이나 변화 배율의 상한 범위에서 늘리는 제안이 통과하도록 하기 위해서입니다.

검증에서는 `proposal.json`에 쓴 변경 목록과 실제 값의 차이가 어긋나지 않았는지도 확인합니다.
변경 목록에 쓴 이전 값이 이전 run의 값과 다르거나, 목록에 없는 항목이 이전 run에서 바뀌어 있으면 통과하지 않습니다.

`vams_parameters.json`의 `templateTags`에는 바꾸지 않는 항목을 포함하여 12개 항목 모두를 넣습니다.
VAMS에서는 `templateTags`로 지정하지 않은 tag가 템플릿의 기본값이 됩니다([VAMS 연동의 “학습의 실행”](../../../../hyperpod/docs/ko/VAMS_INTEGRATION/OVERVIEW.md#학습-실행)).
바꾼 항목만 넣으면, 이전 run에서 템플릿의 기본값 이외로 설정했던 항목이 모르는 사이에 기본값으로 돌아갑니다.

```json
{
  "<pipelineId>": {
    "templateId": "pask-lerobot-openpi-default",
    "templateTags": [{"key": "POLICY_TYPE", "value": "pi05"}, {"key": "STEPS", "value": 60000}]
  }
}
```

위 예시에서는 `templateTags`를 2개 항목으로 생략하고 있습니다.
실제 파일에는 12개 항목 모두가 들어갑니다.
pipelineId에는 기본값이 없으며, `validate-proposal`을 실행할 때 사용자가 지정합니다.

## 리포트 보는 방법

`report.md`는 Claude Code가 작성하며, 언어는 사용자가 대화에서 사용하는 언어에 맞춥니다.
명령어, 파라미터 이름, metrics 이름, 진단 label은 언어와 관계없이 원문 그대로 씁니다.
그림의 축 이름과 범례는 영어입니다.
수치는 JSON의 값을 그대로 옮긴 것이며, Claude Code가 다시 계산한 값이 아닙니다.

리포트는 다음 8개 절로 구성합니다.
절의 이름과 각 절에 싣는 JSON 항목은 [report-template.md](../../../../.claude/skills/pask-training-advisor/references/report-template.md)에 있습니다.

| 절 | 보는 포인트 |
|---|---|
| 대상 run의 요약 | run 이름, run ID, experiment, status, 시작과 종료 시각(UTC와 JST), trigger(`manual` 또는 `vams`), dataset, 12개 항목의 값, 계획상의 총 step 수, 비교 조건 |
| 학습 곡선 그림 | `train/loss`(원시 값과 평활화한 값, 로그 축), `train/grad_norm`, `train/lr`, 처리 속도, GPU memory의 그림과, 비교 대상 run의 `train/loss`를 겹친 그림. 비교 그림은 가로축이 step인 그림과 처리한 sample 수인 그림의 2종류 |
| 진단 결과 | label, feature 표, 소견, 붙은 label과 관련된 임계값. `eval/*`가 없는 run에서는 과학습을 직접 판정하지 않았다는 주석 |
| 다음 파라미터의 제안 | 바꾸는 항목마다의 이전 값, 제안 값, 이유, 근거. 기대하는 효과, 리스크, `validate-proposal`의 경고, 대안 |
| 비교표 | 대상 run과 비교 대상 run의 12개 항목의 값, loss, 완료율, 처리 속도, label. 대상과 다른 항목은 굵게 표시 |
| 학습에 사용한 dataset | dataset마다의 규모, 품질 플래그, 리뷰, 사용한 run의 status. 다음 training에서 제외할 dataset과 episode의 후보, 그리고 VAMS 큐레이션에서 제외하는 절차. VAMS에 연결하지 못한 경우에는 그 이유 |
| 실행하는 경우의 절차 | `vams_parameters.json`의 내용과 VamsCLI 명령어의 예. 실행은 사람의 승인 후 사람이 수행 |
| 허용 목록 밖의 소견 | learning rate 등 12개 항목에 없는 항목을 바꿔야 한다고 판단한 경우에만 |

비교표에서 최종 loss를 비교할 때는 `BATCH_SIZE`의 차이에 주의합니다.
`BATCH_SIZE`가 다르면 같은 step 수에서도 처리한 sample 수가 다르므로, 최종 loss 아래에 나열된 처리한 sample 수와, 가로축이 처리한 sample 수인 비교 그림으로 비교합니다.

리포트에는 MLflow App의 ARN 등 AWS 계정 ID를 포함하는 값을 싣지 않으며, run은 run ID와 experiment 이름으로 나타냅니다.

## 출력 위치

출력 파일(`snapshot.json`, `datasets.json`, `diagnosis.json`, `proposal.json`, `vams_parameters.json`, `report.md`, 그림 PNG)은 모두 같은 directory에 둡니다.
기본 출력 위치는 `samples/lerobot-openpi/outputs/advisor/<UTC 타임스탬프>/`이며, `fetch`가 만듭니다.
타임스탬프의 형식은 `%Y%m%dT%H%M%SZ`(예: `20261019T010203Z`)입니다.
`samples/lerobot-openpi/outputs`는 `.gitignore`로 제외하고 있으므로, 출력은 git으로 관리되지 않습니다.
사용자는 skill을 실행할 때 다른 출력 위치를 지정할 수 있습니다.

MLflow에 저장하는 것은 선택 사항이며, 사용자가 요청한 경우에만 `publish`가 대상 run의 artifact `advisor/<UTC 타임스탬프>/`에 저장합니다.
출력 directory의 이름이 타임스탬프라면 그 값을, 그렇지 않으면 저장한 시각을 사용합니다.
`publish`는 artifact를 추가만 하며, params, metrics, tags는 바꾸지 않습니다.
`report.md`에 ARN이 포함되어 있으면, `publish`는 저장하지 않고 실패합니다.

출력 예시는 [출력 샘플](../../../../hyperpod/docs/ja/VAMS_INTEGRATION/sample-report/README.md)(MLflow만 있는 run)과 [VAMS에서 실행한 run의 출력 샘플](../../../../hyperpod/docs/ja/VAMS_INTEGRATION/sample-report-vams/README.md)(dataset 절을 포함)에 있습니다.

## 필요한 권한

`fetch`와 `publish`는 작업 단말에서 MLflow App에 연결합니다.
작업 단말의 AWS 프로파일에는 MLflow App API를 호출하는 권한 `sagemaker:CallMlflowAppApi`가 필요합니다.
권한이 없으면 `fetch`와 `publish`는 권한 오류로 실패합니다.
연결할 MLflow App의 ARN은 환경 변수 `MLFLOW_TRACKING_URI`로 전달합니다.
프로파일과 리전의 전달 방법은 [skill 절차서](../../../../.claude/skills/pask-training-advisor/SKILL.md)를 따릅니다.

`detect`와 `fetch-vams`는 VamsCLI의 저장된 로그인으로 VAMS를 읽습니다.
PASK와 연동한 VAMS를 대상으로 하는 VamsCLI 프로파일로 `vamscli auth login`을 미리 완료해 둡니다.
로그인이 만료되어 있으면 `detect`는 VAMS를 사용할 수 없다고 판정하고, `mlflow_only`로 진행합니다.

`analyze`, `validate-proposal`, `plot`은 출력 위치의 파일만 읽으므로 AWS 인증이 필요하지 않습니다.

## 제한 사항

제안할 수 있는 것은 12개 항목뿐입니다.
learning rate, warmup step 수, decay step 수는 poller의 허용 목록에도 `template.json`에도 없으므로, 제안에 넣을 수 없습니다.
이 sample에서는 learning rate를 `slurm_train.sh`에서 전달하지 않으며, LeRobot policy의 preset 값을 사용합니다.
발산이나 plateau처럼 learning rate로 대처해야 하는 증상이라도, 제안은 `BATCH_SIZE`, `STEPS`, `LORA_R` 등 간접적인 수단으로 한정됩니다.
learning rate 등을 바꿔야 한다고 Claude Code가 판단한 경우에는, 리포트의 “허용 목록 밖의 소견”에 참고로만 적습니다.
이들을 제안의 대상으로 하려면 poller의 허용 목록, `template.json`, `slurm_train.sh`의 변경이 필요합니다.

`STEPS`를 바꾸면 preset의 learning rate schedule(warmup과 cosine 감쇠)도 바뀝니다.
`STEPS`와 learning rate schedule의 관계는 [tuning-guide.md의 “学習率のスケジュールと STEPS”](../../../../.claude/skills/pask-training-advisor/references/tuning-guide.md#学習率のスケジュールと-stepslerobot-061)에 있습니다.

dataset의 제외 후보는 VAMS의 메타데이터와 리뷰에서 결정합니다.
episode마다의 품질 플래그, 동영상 품질, 행동의 내용은 확인하지 않습니다.
후보를 제외할지는, 사람이 VAMS 화면에서 episode를 확인한 뒤에 결정합니다.
