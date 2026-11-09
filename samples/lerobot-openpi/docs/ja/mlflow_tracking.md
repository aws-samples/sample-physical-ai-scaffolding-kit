# MLflow による学習の記録

本サンプルの学習ジョブは、学習の設定、metrics、LoRA のアダプタを Amazon SageMaker AI の **MLflow App** に記録します。
この文書では、MLflow App の構成、記録の仕組み、run に記録する内容を説明します。
環境変数の設定、ジョブの投入、MLflow UI の開き方は [LeRobot による π0.5 の学習手順](lerobot_pi05_training.md#mlflow-への記録) を参照してください。

## 構成

MLflow App は、Amazon SageMaker AI がサーバーを管理する MLflow の tracking server です。
本リポジトリの CDK（`hyperpod/lib/constructs/mlflow.ts`）が、MLflow App と関連するリソースを HyperPod のスタックと一緒に作成します。

| リソース | 内容 |
|---|---|
| MLflow App | 名前は `pask-mlflow`、MLflow のバージョンは 3.10 です。週次のメンテナンスは日曜 18:00（UTC）に始まります。モデルの自動登録は無効にしています |
| artifact 用の S3 バケット | artifact を `s3://<バケット>/mlflow` 以下に保存します。FSx for Lustre と連携する Data バケットとは別のバケットです。スタックを削除してもバケットは残ります |
| MLflow App のサービスロール | MLflow App が artifact 用のバケットを読み書きするためのロールです |
| ExecutionRole への権限 | HyperPod のノードが MLflow App の API（`sagemaker:CallMlflowAppApi`）を呼び、artifact 用のバケットを読み書きするための権限です |

スタックの Outputs には、MLflow App の ARN（`MlflowAppArn`）と artifact 用のバケット名（`MlflowArtifactBucketName`）を出力します。
HyperPod のノードは、既存の NAT Gateway を経由して MLflow App の公開エンドポイントに HTTPS で接続します。
閉域で使う場合は、インターフェイス VPC エンドポイント `aws.sagemaker.<region>.mlflow` と SageMaker API のエンドポイントを追加し、プライベート DNS を有効にします。

## 記録の仕組み

LeRobot には MLflow に記録する機能が無く、W&B（Weights & Biases）に記録する機能だけがあります。
そこで、W&B に記録するためのロガーを、MLflow に記録するロガーに差し替えて学習を実行します。
LeRobot 本体は変更しません。

記録は次の順に進みます。

1. `setup.sh` が、スタックの Outputs から MLflow App の ARN を読み、`~/.bashrc` に `MLFLOW_TRACKING_URI` として書き込みます。
2. `slurm_train.sh` は、`MLFLOW_TRACKING_URI` が設定されていると、`lerobot-train` の代わりに `training/mlflow_train.py` を起動します。
3. `mlflow_train.py` は、LeRobot の `WandBLogger` を `MlflowLogger` に置き換えてから、`lerobot-train` と同じ処理を呼び出します。
4. 学習ループが W&B のロガーに渡す値を、`MlflowLogger` が MLflow App に送ります。

コンテナ内の MLflow クライアントは、プラグイン `sagemaker-mlflow` を使って MLflow App の ARN を tracking URI として扱い、リクエストに SigV4 の署名を付けます。
署名に使う認証情報は、HyperPod のノードの ExecutionRole です。
アクセスキーやトークンをジョブに渡す必要はありません。

LeRobot の学習ループが呼ぶロガーのメソッドと、MLflow に記録する内容の対応は次のとおりです。

| LeRobot が呼ぶメソッド | 呼ばれる時点 | MLflow に記録する内容 |
|---|---|---|
| `log_dict` | `LOG_FREQ` ステップごと、評価のあと | metrics の `train/*`、`eval/*` |
| `log_policy` | チェックポイントを保存したとき | metrics の `checkpoint_step`、タグ `pask.last_checkpoint_dir`、最終ステップの LoRA のアダプタ |
| `log_video` | 評価で動画を作ったとき | artifact の `videos/eval/` |

## experiment と run

experiment は、run をまとめる単位です。
新しい run は `MLFLOW_EXPERIMENT_NAME` の experiment（既定は `lerobot-pi`）に作ります。

run は 1 回の学習に対応します。
run 名はジョブ名（`slurm_train.sh` の第 1 引数）です。
同じジョブ名で投入しても、MLflow は別の run ID の run を作ります。

`mlflow_train.py` は、run ID をチェックポイントの `train_config.json` の `wandb.run_id` に保存します。
`RESUME=true` で学習を再開すると、この run ID の run を開いて記録を続けます。
そのため、48 時間の制限時間で止まった学習を再開しても、metrics は 1 つの run に続けて記録されます。

run の状態は、ジョブの終わり方で決まります。

| ジョブの終わり方 | run の状態 |
|---|---|
| 学習が最後まで進んだ | `FINISHED` |
| 学習がエラーで止まった | `FAILED` |
| `scancel` や制限時間で止められた | `KILLED` |

## run に記録する内容

### params

学習の設定全体を、ドット区切りのキーに展開して params に記録します（例：`dataset.repo_id`、`peft.r`、`optimizer.lr`、`steps`）。
π0.5 の LoRA 学習では約 140 個になります。
1 つの値は 6000 文字までで、超えた部分は切り捨てます。

MLflow の params は記録したあとに変更できません。
そのため、params は run を作ったときだけ記録し、再開したときは記録しません。
再開時に `STEPS` で総ステップ数を延ばしても、params の `steps` は最初の値のままです。

### metrics の `train/*`

`LOG_FREQ` ステップ（既定は 100）ごとに、LeRobot が学習ログに出力する値と同じ値を記録します。
横軸のステップは学習のステップ数です。

| metrics | 内容 | 学習ログでの表記 |
|---|---|---|
| `train/loss` | 学習の損失 | `loss` |
| `train/grad_norm` | 勾配のノルム | `grdn` |
| `train/lr` | 学習率 | `lr` |
| `train/update_s` | 1 ステップの重みの更新にかかった秒数 | `updt_s` |
| `train/dataloading_s` | 1 ステップのデータの読み込みにかかった秒数 | `data_s` |
| `train/samples_per_s` | 1 秒あたりに処理したサンプル数 | `smp/s` |
| `train/gpu_mem_gb` | GPU のメモリ使用量（GB） | `mem_gb` |
| `train/steps` | ステップ数 | `step` |
| `train/samples` | 学習を始めてから処理したサンプル数 | `smpl` |
| `train/episodes` | 処理したサンプル数を、データセットのエピソード数に換算した値 | `ep` |
| `train/epochs` | 処理したサンプル数を、データセット全体を何周したかに換算した値 | `epch` |

学習がうまく進んでいるかは、まず `train/loss` が下がっているかで判断します。
`train/dataloading_s` が `train/update_s` に比べて大きい場合は、データの読み込みが学習の速さを制限しています。
その場合は `NUM_WORKERS` を増やすと速くなる可能性があります。

### metrics の `eval/*`

LeRobot の評価を `EXTRA_ARGS` で有効にした場合だけ、評価の結果を `eval/*` に記録します。
本サンプルの既定の設定では評価を実行しないため、記録されません。

### metrics の `checkpoint_step`

チェックポイントを保存したステップ数を、そのステップの値として記録します。
`SAVE_FREQ`（既定は 5000）ごとに 1 点ずつ増えます。
最後に保存したチェックポイントのディレクトリは、タグ `pask.last_checkpoint_dir` で分かります。

### metrics の `system/*`

MLflow の system metrics の機能で、ノードの使用状況を `MLFLOW_SYSTEM_METRICS_SAMPLING_INTERVAL` 秒（既定は 10）ごとに記録します。
横軸は学習のステップではなく、system metrics を取得した回数です。

| metrics | 内容 |
|---|---|
| `system/gpu_0_utilization_percentage` | GPU の使用率（%） |
| `system/gpu_0_memory_usage_megabytes` | GPU のメモリ使用量（MB） |
| `system/gpu_0_memory_usage_percentage` | GPU のメモリ使用率（%） |
| `system/gpu_0_power_usage_watts` | GPU の消費電力（W） |
| `system/gpu_0_power_usage_percentage` | GPU の消費電力の、上限に対する割合（%） |
| `system/cpu_utilization_percentage` | CPU の使用率（%） |
| `system/system_memory_usage_megabytes` | ノードのメモリ使用量（MB） |
| `system/system_memory_usage_percentage` | ノードのメモリ使用率（%） |
| `system/disk_usage_megabytes` | ディスクの使用量（MB） |
| `system/disk_usage_percentage` | ディスクの使用率（%） |
| `system/disk_available_megabytes` | ディスクの空き容量（MB） |
| `system/network_receive_megabytes` | 記録を始めてから受信したデータ量（MB） |
| `system/network_transmit_megabytes` | 記録を始めてから送信したデータ量（MB） |

GPU の metrics の `gpu_0` は GPU の番号です。
本サンプルの学習は GPU を 1 枚だけ使うため、`gpu_0` だけが記録されます。
`system/gpu_0_utilization_percentage` が低い場合は、`train/dataloading_s` と合わせて、データの読み込みで GPU が待たされていないかを確認します。

### artifact

| artifact | 内容 | 記録する時点 |
|---|---|---|
| `train_config.json` | 学習の設定全体 | run を作ったとき |
| `adapter/<step>/` | LoRA のアダプタ（`adapter_model.safetensors`、`adapter_config.json`、`config.json`） | 最終ステップ（`STEPS`）のチェックポイントを保存したとき |
| `videos/eval/` | 評価の動画 | 評価を有効にした場合だけ |

LoRA のアダプタは、3 つのファイルの合計が `MLFLOW_ADAPTER_MAX_MB`（既定は 200）以下で、`MLFLOW_DISABLE_ARTIFACT` が `false` のときだけ記録します。
`LORA_R=16` のアダプタは約 5 MB です。
`FINETUNE_MODE=expert_only` と `full` のチェックポイントにはアダプタが無く、重み全体は数 GB になるため、MLflow には記録しません。
これらの重みは、タグ `pask.last_checkpoint_dir` のパスから FSx 上のチェックポイントを参照します。

### タグ

run には、学習の条件と実行環境を示すタグを付けます。
値が空のタグは付けません。

| タグ | 値 |
|---|---|
| `lerobot.version` | LeRobot のバージョン |
| `lerobot.policy_type` | `pi05` または `pi0` |
| `lerobot.finetune_mode` | `lora`、`expert_only`、`full` のいずれか |
| `lerobot.dataset_repo_id` | データセットのリポジトリ ID |
| `lerobot.pretrained_path` | ベース重み。run を作ったときだけ記録します |
| `pask.output_dir` | 出力ディレクトリ |
| `pask.last_checkpoint_dir` | 最後に保存したチェックポイントのディレクトリ |
| `pask.image_tag` | コンテナイメージのタグ（`IMAGE_TAG`） |
| `pask.cluster` | HyperPod のクラスタ名 |
| `pask.run_type` | `train` |
| `slurm.job_id` | 最後にこの run に記録したジョブの ID |
| `slurm.job_ids` | この run に記録したすべてのジョブの ID（カンマ区切り） |

`lerobot.pretrained_path` をタグとして別に残すのは、再開すると LeRobot が設定の `pretrained_path` をチェックポイントのパスに書き換えるためです。
`slurm.job_ids` を見れば、再開を含めてどのジョブのログを確認すればよいかが分かります。

experiment には、タグ `mlflow.experimentKind=custom_model_development` を付けます。
MLflow 3.10 の UI では、このタグを付けても表示は変わりません。
モデル学習向けの表示は、UI の切り替えで選びます。
