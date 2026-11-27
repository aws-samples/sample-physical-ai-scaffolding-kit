# MLflow による学習の記録

本サンプルの学習ジョブは、学習の設定、metrics、LoRA のアダプタを Amazon SageMaker AI の **MLflow App** に記録します。
この文書では、MLflow App の構成、記録の仕組み、run に記録する内容を説明します。
環境変数の設定、ジョブの投入、MLflow UI の開き方は [LeRobot による π0.5 の学習手順](lerobot_pi05_training.md#mlflow-への記録) を参照してください。

## 構成

MLflow App は、Amazon SageMaker AI がサーバーを管理する MLflow の tracking server です。
本リポジトリの CDK（`hyperpod/lib/constructs/mlflow.ts`）が、MLflow App と関連するリソースを HyperPod のスタックと一緒に作成します。
既定では作成しないので、`hyperpod/cdk.json` の `Mlflow.Enabled` を `true` にしてデプロイします。

| リソース | 内容 |
|---|---|
| MLflow App | 名前は `pask-mlflow` です。MLflow のバージョンは指定しないので、作成した時点の SageMaker AI の既定のバージョンになります（2026 年 11 月の時点では 3.16）。週次のメンテナンスは日曜 18:00（UTC）に始まります。モデルの自動登録は無効にしています |
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

### VAMS から起動した run

VAMS のワークフローから起動したジョブには、ポーラーが環境変数 `VAMS_CONTEXT_FILE` で `vams_context.json` のパスを渡します。
`mlflow_train.py` は、この環境変数が設定されていれば VAMS からの実行、設定されていなければ手動の実行とみなします。
設定されているのにファイルを読めない場合や、JSON が不正な場合は、run を作らずに学習を始める前に失敗します。
入力の来歴が無い run を残さないためです。

experiment は、ポーラーが `MLFLOW_EXPERIMENT_NAME` で渡す VAMS 専用の experiment（既定は `vams-training`）です。

1 回の VAMS の実行（`pipelineExecutionId`）には、run を 1 つだけ作ります。
`mlflow_train.py` は、新しい run を作る前に、experiment の中からタグ `vams.pipeline_execution_id` が同じ run を探します。
見つかった場合は、その run を開いて記録を続けます。
この場合、params はすでに記録されていて変更できないため、params と `train_config.json` は記録しません。

run を開始すると、`vams_context.json` の `resultFile` が指すファイル（`mlflow_run.json`）に、run への逆リンクを書きます。
ポーラーはジョブの終了後にこのファイルを読み、run ID を VAMS への結果に入れます。
書き込みは一時ファイルに書いてから名前を変えるので、ポーラーが書きかけのファイルを読むことはありません。

```json
{"run_id": "...", "experiment_id": "...", "tracking_uri": "..."}
```

`vams_context.json` の形と、ポーラーが行う処理は [VAMS 連携の仕様](../../../../hyperpod/docs/ja/VAMS_INTEGRATION/SPEC.md#mlflow-に記録するもの) を参照してください。

## run に記録する内容

### params

学習の設定全体を、ドット区切りのキーに展開して params に記録します（例：`dataset.repo_id`、`peft.r`、`optimizer.lr`、`steps`）。
π0.5 の LoRA 学習では約 140 個になります。
1 つの値は 6000 文字までで、超えた部分は切り捨てます。

MLflow の params は記録したあとに変更できません。
そのため、params は run を作ったときだけ記録し、再開したときと、同じ VAMS の実行の run を続けるときは記録しません。
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
| `vams/input_manifest.json` | 入力のファイルの一覧（bucket、key、versionId、ETag、size）。curation のビューの場合は、元のアセットのファイルの一覧と元のアセットの情報（`source`） | VAMS から起動した run を開始したとき |
| `vams/curation_manifest.json` | curation のビューのマニフェスト | VAMS から curation のビューで起動した run を開始したとき |
| `vams/norm_stats.json` | 学習に使った正規化統計。`compute_view_stats.py` が残すエピソードから計算した `meta/stats.json` です | VAMS から curation のビューで起動した run を開始したとき |

LoRA のアダプタは、3 つのファイルの合計が `MLFLOW_ADAPTER_MAX_MB`（既定は 200）以下で、`MLFLOW_DISABLE_ARTIFACT` が `false` のときだけ記録します。
`LORA_R=16` のアダプタは約 5 MB です。
`FINETUNE_MODE=expert_only` と `full` のチェックポイントにはアダプタが無く、重み全体は数 GB になるため、MLflow には記録しません。
これらの重みは、タグ `pask.last_checkpoint_dir` のパスから FSx 上のチェックポイントを参照します。

### dataset

VAMS から起動した run では、入力のデータセットを `mlflow.log_input` で run の dataset として登録します。
手動の実行では登録しません。

| 項目 | 値 |
|---|---|
| source | `s3://<input.bucket>/<input.assetRootS3Key>`（VAMS のアセットのルート） |
| name | アセット名（`input.assetName`）。無ければアセット ID（`input.assetId`） |
| digest | `datasetDigest` の 16 進の先頭 36 文字 |
| context | `training` |

入力が curation のビューの場合、source は元のアセットのルート（`s3://<view.source.bucket>/<view.source.assetRootS3Key>`）です。
ビューのアセットには学習に使うファイルが無く、学習するファイルは元のアセットにあるためです。
name はビューのアセットの名前（無ければビューのアセット ID）のままです。

MLflow は dataset の digest を 36 文字までに制限しているため、`sha256:<hex>` の形の値はそのまま入りません。
そこで、dataset の digest には 16 進の先頭 36 文字を使い、完全な値はタグ `vams.dataset_digest` に残します。

dataset の登録に失敗しても、警告をログに出して学習を続けます。
入力の来歴は、タグ `vams.*` と artifact の `vams/input_manifest.json` に残るためです。

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
| `pask.trigger` | VAMS から起動した run は `vams`、手動の実行は `manual` |
| `slurm.job_id` | 最後にこの run に記録したジョブの ID |
| `slurm.job_ids` | この run に記録したすべてのジョブの ID（カンマ区切り） |

VAMS から起動した run には、次のタグも付けます。
値は `vams_context.json` から取ります。

| タグ | 値 |
|---|---|
| `vams.workflow_database_id` | ワークフローの database ID（`workflowDatabaseId`） |
| `vams.workflow_id` | ワークフローの ID（`workflowId`） |
| `vams.workflow_execution_id` | ワークフローの実行の ID（`workflowExecutionId`） |
| `vams.pipeline_execution_id` | パイプラインの実行の ID（`pipelineExecutionId`）。同じ実行の run を探すのに使います |
| `vams.pipeline_name` | パイプラインの名前（`pipelineName`） |
| `vams.executing_user_name` | ワークフローを実行したユーザーの名前（`executingUserName`） |
| `vams.input.database_id` | 入力のアセットの database ID（`input.databaseId`） |
| `vams.input.asset_id` | 入力のアセットの ID（`input.assetId`） |
| `vams.input.asset_name` | 入力のアセットの名前（`input.assetName`） |
| `vams.input.versioned` | 入力のファイルの版を固定できたか（`true` または `false`）。アセットのバケットのバージョニングが無効なら `false` です |
| `vams.input.kind` | 入力の種類（`input.kind`）。curation のビューなら `view`、通常のデータセットなら `dataset` |
| `vams.output.asset_id` | 学習済みモデルを取り込むアセットの ID（`output.assetId`） |
| `vams.dataset_digest` | 入力のファイルの版から求めた digest（`sha256:<hex>`） |

入力が VAMS の curation のビューの場合、`vams.input.*` のタグはビューのアセットの値です。
ビューが指す元のアセットと、残したエピソードは、次のタグで記録します。
値は `vams_context.json` の `view` から取ります。

| タグ | 値 |
|---|---|
| `vams.source.database_id` | 元のアセットの database ID（`view.source.databaseId`） |
| `vams.source.asset_id` | 元のアセットの ID（`view.source.assetId`） |
| `vams.source.asset_version` | 元のアセットの版（`view.source.assetVersion`）。無ければ付けません |
| `vams.view.episode_count` | 学習に使ったエピソードの数（`view.episodeCount`） |
| `vams.view.excluded_episode_count` | ビューで除外したエピソードの数（`view.excludedEpisodeCount`） |
| `vams.view.review_updated_at` | ビューのマニフェストの `source.review_updated_at`（`view.reviewUpdatedAt`）。無ければ付けません |

`lerobot.pretrained_path` をタグとして別に残すのは、再開すると LeRobot が設定の `pretrained_path` をチェックポイントのパスに書き換えるためです。
`slurm.job_ids` を見れば、再開を含めてどのジョブのログを確認すればよいかが分かります。

experiment には、タグ `mlflow.experimentKind=custom_model_development` を付けます。
MLflow 3.10 の UI で確かめた時点では、このタグを付けても表示は変わりませんでした。
モデル学習向けの表示は、UI の切り替えで選びます。
