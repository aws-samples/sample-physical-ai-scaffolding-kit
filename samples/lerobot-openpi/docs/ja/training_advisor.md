# MLflow の学習結果からの次の学習パラメータの提案

本サンプルの学習が MLflow App に記録した結果を診断し、次の学習パラメータの提案とレポートを作る仕組みを、Claude Code の project skill `pask-training-advisor` として用意しています。
この文書では、仕組み、診断の基準、提案の形と検証、レポートの見方を説明します。
コマンドの使い方と手順は [skill の手順書](../../../../.claude/skills/pask-training-advisor/SKILL.md) を参照してください。
MLflow に記録される params、tags、metrics の意味は [MLflow による学習の記録](mlflow_tracking.md) を参照してください。

## 概要

判断の材料は、MLflow に記録済みの学習の結果（metrics の履歴、params、tags）と、VAMS に接続できる場合は学習に使ったデータセットのメタデータ（エピソード数、品質のフラグ、レビュー）です。
シミュレーション環境での closed-loop 評価は行いません。
そのため、診断が示すのは学習データでの損失の推移であり、ロボットの動作の良し悪しではありません。

提案は、VAMS の execute API の `pipelineExecutionParameters`（`templateId` と `templateTags`）にそのまま渡せる JSON（`vams_parameters.json`）で出します。
skill は提案した学習を実行しません。
人が提案とレポートを確認し、承認したあとに、人が VAMS から実行します。

学習に使ったデータセットについては、次の学習から外すエピソードとデータセットの候補をレポートに書きます。
候補は削除の提案ではなく、人が VAMS の LeRobot Dataset Curation で次の学習の入力から外すための提案です。
skill は VAMS のアセットを削除せず、view も作りません。

レポートには MLflow の学習の結果が必須です。
VAMS だけに接続できる場合と、どちらにも接続できない場合は、レポートを作らずに止まります。
学習に使ったデータセットは MLflow の run のタグから辿るため、MLflow が無いと特定できないためです。

処理は、決定的な処理を行う Python のツールと、判断を受け持つ Claude Code に分かれています。
ツールは接続先の確認、MLflow と VAMS からの取得、特徴量と診断のラベルの計算、除外の候補の抽出、提案の検証と VAMS の形への変換、図の生成、MLflow の artifact への保存を行います。
Claude Code は、12 項目のどれをどう変えるかを決めて根拠を書き、レポートを書きます。
数値の計算と制約の検証をツールに寄せているのは、Claude Code が数値を計算し直したり、VAMS で失敗する値を提案したりすることを防ぐためです。

## 全体の流れ

```mermaid
flowchart LR
  X[MLflow App と VAMS] -->|detect| Y{mode}
  Y -->|full / mlflow_only| A
  Y -.vams_only / none.-> Z[止まる]
  A[MLflow App<br/>params / metrics / tags] -->|fetch| B[snapshot.json]
  B -->|fetch-vams| V[datasets.json]
  B -->|analyze| C[diagnosis.json]
  V -->|analyze| C
  C --> D[Claude Code<br/>次のパラメータを判断]
  D --> E[proposal.json]
  E -->|validate-proposal| F[vams_parameters.json]
  B -->|plot| P[PNG の図]
  B --> R[Claude Code<br/>レポートを書く]
  C --> R
  F --> R
  P --> R
  R --> G[report.md]
  G -.任意.->|publish| M[MLflow の artifact]
  G -.承認後.-> H[人が VAMS で実行]
```

1. `detect`：MLflow と VAMS に接続できるかを確かめ、`full`（両方）、`mlflow_only`、`vams_only`、`none` のどれかを返します。`vams_only` と `none` では以降に進みません。
2. `fetch`：対象の run と比較対象の run を MLflow から取得し、`snapshot.json` に保存します。
3. `fetch-vams`（`full` の場合）：run が使った VAMS のアセットのメタデータを VamsCLI で読み、`datasets.json` に保存します。
4. `analyze`：`snapshot.json` から特徴量を計算し、診断のラベルと所見を `diagnosis.json` に書きます。`datasets.json` があれば、データセットの診断と除外の候補も書きます。
5. Claude Code が `diagnosis.json` と制約を読み、次の 12 項目の値と根拠を `proposal.json` に書きます。
6. `validate-proposal`：`proposal.json` を検証し、通った場合だけ `vams_parameters.json` を書きます。通らなければ Claude Code が `proposal.json` を直し、通るまで繰り返します。
7. `plot`：`snapshot.json` から学習曲線の図を作ります。
8. Claude Code が JSON と図を読んで `report.md` を書きます。
9. `publish`（任意）：`report.md`、図、JSON を対象の run の artifact に保存します。

`analyze` 以降のサブコマンドのうち、MLflow に接続するのは `publish` だけです。
`snapshot.json` と `datasets.json` があれば、MLflow と VAMS に接続できない環境でも診断とレポートを再現できます。

比較対象の run は、利用者が指定しなければ experiment `vams-training` と `lerobot-pi` から、対象の run と同じデータセットで status が `FINISHED` の run を最大 5 件選びます。
データセットが同じかどうかは、タグ `vams.dataset_digest` で判定し、このタグが無い run ではタグ `lerobot.dataset_repo_id` で判定します。
experiment と件数は利用者が変えられます。

## 使う metrics

| metrics | 診断での用途 |
|---|---|
| `train/loss` | 収束、プラトー、発散、ノイズの判定 |
| `train/grad_norm` | 発散とスパイクの判定 |
| `train/lr` | 学習率のスケジュールの進み具合 |
| `train/update_s`、`train/dataloading_s`、`train/samples_per_s` | データの読み込みが学習の速さを制限しているか |
| `train/gpu_mem_gb` | GPU のメモリ使用量 |
| `train/epochs`、`train/samples`、`train/episodes` | データセットを何周したか、データセットの規模の推定 |
| `checkpoint_step` | 保存されたチェックポイントの位置 |
| `system/gpu_0_utilization_percentage`、`system/gpu_0_memory_usage_percentage` | GPU の使用率とメモリの余地 |
| `eval/*` | 評価の損失。記録されている run だけ |

12 項目の値は、params（`policy.type`、`steps`、`batch_size`、`peft.r` など）とタグ（`lerobot.finetune_mode`、`lerobot.pretrained_path`、`pask.image_tag` など）から復元します。
再開した run では params の `steps` が最初の値のままなので、計画上の総ステップ数は params の `steps` と metrics の最終ステップの大きい方として扱い、そのことを `snapshot.json` の注記に残します。

データセットのフレーム数とエピソード数は MLflow に記録されていないため、`train/samples / train/epochs` と `train/episodes / train/epochs` から推定します。

## 診断の基準

`analyze` は、run ごとに次のラベルのうち当てはまるものをすべて付けます。
`insufficient_data` が付いた run には、曲線の形のラベル（`diverging`、`unstable`、`converging`、`plateau`、`overfit_risk`）を付けません。
判定に足りる点が無いと、末尾の傾きなどの特徴量が意味を持たないためです。
`train/loss` の記録点が 1 つも無い run には、GPU メモリのラベル（`gpu_memory_headroom`、`gpu_memory_tight`）も付けません。
学習が始まる前のシステム metrics は、学習に要るメモリを表さないためです。

| ラベル | 意味 | 主な条件（括弧内は閾値のキー） |
|---|---|---|
| `insufficient_data` | 判定に足りる点が無い | `train/loss` の記録点数が下限未満（`minPoints`） |
| `failed_or_killed` | run が途中で止まった | status が `FAILED` か `KILLED`、または計画ステップに対する完了率が下限未満（`minCompletionRatio`） |
| `diverging` | 発散している | `train/loss` か `train/grad_norm` に NaN や inf がある、または末尾の `log(loss)` の傾きが閾値を超えて正で、末尾の相対改善が負（`divergingTailSlopePer1k`） |
| `unstable` | 不安定 | スパイクの数か末尾の変動係数が閾値を超える（`spikeFactor`、`maxSpikes`、`maxTailCv`） |
| `converging` | まだ下がっている | 末尾の傾きが負で、末尾の相対改善が閾値を超える（`convergingTailImprovement`） |
| `plateau` | 下がりきっている | `diverging` ではなく、末尾の相対改善が閾値未満（`plateauTailImprovement`） |
| `overfit_risk` | 過学習の可能性 | `converging` で、最終の `train/epochs` が閾値を超える（`overfitEpochs`）。`eval/eval_loss` が記録されていれば、`converging` で評価の損失の末尾の傾きが正の場合も付く |
| `dataloader_bound` | データの読み込みが学習の速さを制限している | `train/dataloading_s / train/update_s` の中央値が閾値を超える（`dataloaderBoundRatio`） |
| `gpu_memory_headroom` | GPU メモリに余裕がある | `system/gpu_0_memory_usage_percentage` の最大値が閾値未満（`gpuMemoryHeadroomPercent`） |
| `gpu_memory_tight` | GPU メモリが逼迫している | `system/gpu_0_memory_usage_percentage` の最大値が閾値を超える（`gpuMemoryTightPercent`） |

末尾の傾きと相対改善は、記録点の末尾の窓（`tailWindowFraction`）で計算します。
傾きは `log(loss)` をステップに対して線形回帰した 1000 ステップあたりの値（`tail_slope_per_1k`）で、相対改善は末尾の窓の前半と後半の中央値の比から求めた値（`tail_improvement`）です。

閾値は [`diagnosis_thresholds.json`](../../../../.claude/skills/pask-training-advisor/scripts/config/diagnosis_thresholds.json) にあり、キーごとの意味は同じファイルの `_comments` に書いてあります。
末尾の窓は記録点の 25%、`converging` と `plateau` を分ける末尾の相対改善の閾値は 0.01 です。
記録点が 30 点程度以下の run では末尾の窓が短く、曲線の形のラベルがノイズに左右されやすくなります。
ファイルを書き換えるか、`analyze` に別のファイルを渡すと、閾値を変えられます。
`diagnosis.json` には判定に使った閾値も残るので、あとから判定の根拠を確かめられます。

過学習は、`eval/*` が記録されていなければ直接は判定できません。
`train/loss` は学習データでの損失であり、学習データに過剰に合わせても下がり続けるためです。
本サンプルの既定の設定は評価を実行しないので、`overfit_risk` はデータの周回数（`train/epochs`）による代理の判定になります。

## 学習に使ったデータセットの診断

`fetch-vams` が読むのは、対象の run と比較対象の run が使ったアセットだけです。
run の入力（タグ `vams.input.asset_id`）と、入力が curation の view の場合は view の元のデータセット（タグ `vams.source.asset_id`）が対象です。
データベースのほかのアセットは読みません。
VamsCLI は保存済みのログインで読み取りのコマンドだけを使い、プロファイルは `--vams-profile` か環境変数 `VAMS_CLI_PROFILE` で指定します。

`fetch-vams` は、アセットごとに次の情報を `datasets.json` に保存します。

| 情報 | 取得元 |
|---|---|
| 規模（エピソード数、フレーム数、時間、fps、タスク） | アセットのメタデータ `lerobot_total_*`、`lerobot_fps`、`lerobot_tasks` |
| 品質（フラグが立ったエピソード数とフラグの内訳） | アセットのメタデータ `lerobot_quality_*`。VAMS の LeRobot Episode Quality パイプラインが書きます |
| レビュー（エピソードごとの判定と除外） | アセットのメタデータ `lerobot_review_episodes`。VAMS の画面でのレビューが書きます |
| 統計（`meta/stats.json` の有無とフレーム数の一致） | アセットのメタデータ `lerobot_stats_*` |
| view が残したエピソードと外したエピソード | run の artifact `vams/curation_manifest.json` |
| 同じアセットを使ったすべての run の status | MLflow の run の検索 |

`analyze` は、`datasets.json` から次の候補を `diagnosis.json` の `datasets` に書きます。

| 候補 | 理由 | 規則 |
|---|---|---|
| データセット（次の学習の入力にしない） | `only_failed_runs` | そのアセットを入力にした run がすべて `FAILED` か `KILLED` |
| | `not_lerobot` | LeRobot のデータセットでも view でもない |
| | `stats_missing` | 正規化の統計が無い |
| | `stats_count_mismatch` | 統計のフレーム数が実際のフレーム数と合わない |
| | `few_episodes` | エピソード数が閾値（`fewEpisodesMax`、既定 1）以下 |
| エピソード（view の残すエピソードから外す） | `review_exclude` | レビューで `exclude` が付いている |
| | `review_failure` | レビューの判定が `failure` |

エピソードの候補は、その run がまだ学習に使ったものだけです。
入力が view の場合、view が既に外したエピソードは候補にせず、`alreadyExcludedEpisodes` に分けて残します。
閾値は [`dataset_thresholds.json`](../../../../.claude/skills/pask-training-advisor/scripts/config/dataset_thresholds.json) にあります。

品質のフラグは件数だけを扱い、フラグだけを理由にエピソードの候補にはしません。
エピソードごとのフラグはアセットの品質ファイル（`meta/vams_episode_quality.parquet`）にあり、メタデータには件数しか無いためです。
また、フラグはトリアージのためのもので、エピソードの成功や失敗を表しません。
フラグが立っていてレビューが無い場合は、VAMS の画面でレビューすることをレポートで勧めます。

データセットの診断は、次のパラメータの判断にも使います。
例えば `few_episodes` の入力で `overfit_risk` が付いた場合は、`STEPS` を増やすより、エピソードを足したデータセットか view に入力を替えることを優先して提案します。

## 提案の形と検証

提案の対象は、VAMS から学習に渡せる次の 12 項目です。

`POLICY_TYPE`、`PRETRAINED_PATH`、`FINETUNE_MODE`、`LORA_R`、`LORA_ALPHA`、`STEPS`、`BATCH_SIZE`、`SAVE_FREQ`、`LOG_FREQ`、`NUM_WORKERS`、`COMPILE_MODEL`、`IMAGE_TAG`

Claude Code は `proposal.json` に、12 項目すべての値、前の run から変えた項目ごとの理由と根拠（診断のラベル名や `diagnosis.json` の特徴量の値）、期待する効果、リスク、代わりの案を書きます。
ラベルと 12 項目の対応の目安は [tuning-guide.md](../../../../.claude/skills/pask-training-advisor/references/tuning-guide.md) にあります。
目安は推奨であり、Claude Code が目安と違う判断をした場合も、理由と根拠が `proposal.json` に残ります。

`validate-proposal` は、提案を 2 段の制約で検証します。

| 制約 | 内容 | 定義している場所 |
|---|---|---|
| 形式の制約 | 12 項目がそろっていること、許可リスト外と deny list のキーが無いこと、値がポーラーの許可リストの正規表現に合うこと、型と enum の値が `tagSchema` に合うこと | ポーラーの `ALLOWED_TRAINING_PARAMS`（`hyperpod/vams-poller`）、[`template.json`](../../../../hyperpod/vams/template.json) の `tagSchema` |
| 運用の制約 | 値の範囲、項目間の関係（`SAVE_FREQ <= STEPS`、`LOG_FREQ <= SAVE_FREQ`、`POLICY_TYPE` と `PRETRAINED_PATH` の組み合わせ）、1 回で変える項目の数の上限、1 項目の変化の倍率の上限、前の run と同じに固定する項目 | [`proposal_constraints.json`](../../../../.claude/skills/pask-training-advisor/scripts/config/proposal_constraints.json) |

形式の制約を満たさない提案は、VAMS で実行してもポーラーが受け付けません。
ポーラーの許可リストを複製せずに import して使っているので、許可リストを変えると検証の規則も同じように変わります。

運用の制約は、本サンプルの環境で妥当な範囲に提案を収めるためのものです。
現在の値では、変えられる項目は 1 回に 3 つまで、整数の項目の変化は 4 倍までで、`IMAGE_TAG`、`PRETRAINED_PATH`、`POLICY_TYPE` は前の run の値に固定しています。
`FINETUNE_MODE` を `expert_only` か `full` にした提案は通りますが、24 GB の GPU に収まらない可能性があるという警告が出ます（[GPU メモリと finetuning 方式の目安](lerobot_pi05_training.md#gpu-メモリと-finetuning-方式の目安)）。
代わりの案（`alternatives`）も同じ 2 段の制約で検証します。
代わりの案の違反は提案を止めず、警告として出ます。
`FINETUNE_MODE` の警告も、代わりの案に対して同じように出ます。
`STEPS` と `SAVE_FREQ` の下限は、ポーラーの許可リストと同じ 1 にしています。
`STEPS=20` のような動作確認用の短い run でも、前の run の値のままの提案や、変化の倍率の上限の範囲で延ばす提案が通るようにするためです。

検証では、`proposal.json` に書いた変更の一覧と実際の値の差が食い違っていないかも確かめます。
変更の一覧に書いた前の値が前の run の値と違う場合や、一覧に無い項目が前の run から変わっている場合は通りません。

`vams_parameters.json` の `templateTags` には、変えない項目も含めて 12 項目すべてを入れます。
VAMS では、`templateTags` で指定しなかったタグはテンプレートの既定値になります（[VAMS 連携の「学習の実行」](../../../../hyperpod/docs/ja/VAMS_INTEGRATION/OVERVIEW.md#学習の実行)）。
変えた項目だけを入れると、前の run でテンプレートの既定値以外にしていた項目が、知らないうちに既定値に戻ります。

```json
{
  "<pipelineId>": {
    "templateId": "pask-lerobot-openpi-default",
    "templateTags": [{"key": "POLICY_TYPE", "value": "pi05"}, {"key": "STEPS", "value": 60000}]
  }
}
```

上の例では `templateTags` を 2 項目に省略しています。
実際のファイルには 12 項目すべてが入ります。
pipelineId には既定値が無く、`validate-proposal` を実行するときに利用者が指定します。

## レポートの見方

`report.md` は Claude Code が書き、言語は利用者が会話で使っている言語に合わせます。
コマンド、パラメータ名、メトリクス名、診断のラベルは、言語によらず原文のまま書きます。
図の軸の名前と凡例は英語です。
数値は JSON の値をそのまま転記したもので、Claude Code が計算し直した値ではありません。

レポートは次の 8 つの節で構成します。
節の名前と、各節に載せる JSON の項目は [report-template.md](../../../../.claude/skills/pask-training-advisor/references/report-template.md) にあります。

| 節 | 見るポイント |
|---|---|
| 対象の run の要約 | run 名、run ID、experiment、status、開始と終了の時刻（UTC と JST）、トリガー（`manual` か `vams`）、データセット、12 項目の値、計画の総ステップ数、比較の条件 |
| 学習曲線の図 | `train/loss`（生の値と平滑化した値、対数軸）、`train/grad_norm`、`train/lr`、処理速度、GPU メモリの図と、比較対象の run の `train/loss` を重ねた図。比較の図は、横軸がステップの図と処理したサンプル数の図の 2 種類 |
| 診断の結果 | ラベル、特徴量の表、所見、付いたラベルに関係する閾値。`eval/*` が無い run では、過学習を直接は判定していないという注記 |
| 次のパラメータの提案 | 変える項目ごとの前の値、提案の値、理由、根拠。期待する効果、リスク、`validate-proposal` の警告、代わりの案 |
| 比較表 | 対象の run と比較対象の run の 12 項目の値、損失、完了率、処理速度、ラベル。対象と違う項目は太字 |
| 学習に使ったデータセット | データセットごとの規模、品質のフラグ、レビュー、使った run の status。次の学習から外すデータセットとエピソードの候補と、VAMS のキュレーションで外す手順。VAMS に接続できなかった場合はその理由 |
| 実行する場合の手順 | `vams_parameters.json` の中身と VamsCLI のコマンドの例。実行は人の承認後に人が行う |
| 許可リスト外の所見 | 学習率など 12 項目に無い項目を変えるべきと判断した場合だけ |

比較表で最終の損失を比べるときは、`BATCH_SIZE` の違いに注意します。
`BATCH_SIZE` が違うと同じステップ数でも処理したサンプル数が違うので、最終の損失の下に並ぶ処理したサンプル数と、横軸が処理したサンプル数の比較の図で比べます。

レポートには MLflow App の ARN など AWS のアカウント ID を含む値を載せず、run は run ID と experiment 名で示します。

## 出力先

出力ファイル（`snapshot.json`、`datasets.json`、`diagnosis.json`、`proposal.json`、`vams_parameters.json`、`report.md`、図の PNG）は、すべて同じディレクトリに置きます。
既定の出力先は `samples/lerobot-openpi/outputs/advisor/<UTC タイムスタンプ>/` で、`fetch` が作ります。
タイムスタンプの形は `%Y%m%dT%H%M%SZ`（例 `20261019T010203Z`）です。
`samples/lerobot-openpi/outputs` は `.gitignore` で除外しているので、出力は git で管理されません。
利用者は skill の実行時に別の出力先を指定できます。

MLflow への保存は任意で、利用者が求めた場合だけ `publish` が対象の run の artifact `advisor/<UTC タイムスタンプ>/` に保存します。
出力先のディレクトリ名がタイムスタンプであればその値を、そうでなければ保存した時刻を使います。
`publish` は artifact を追加するだけで、params、metrics、tags は変えません。
`report.md` に ARN が含まれている場合、`publish` は保存せずに失敗します。

出力の例は [出力サンプル](../../../../hyperpod/docs/ja/VAMS_INTEGRATION/sample-report/README.md)（MLflow だけの run）と [VAMS から実行した run の出力サンプル](../../../../hyperpod/docs/ja/VAMS_INTEGRATION/sample-report-vams/README.md)（データセットの節を含む）にあります。

## 必要な権限

`fetch` と `publish` は作業端末から MLflow App に接続します。
作業端末の AWS のプロファイルに、MLflow App の API を呼ぶ権限 `sagemaker:CallMlflowAppApi` が必要です。
権限が無い場合、`fetch` と `publish` は権限エラーで失敗します。
接続先の MLflow App の ARN は環境変数 `MLFLOW_TRACKING_URI` で渡します。
プロファイルとリージョンの渡し方は [skill の手順書](../../../../.claude/skills/pask-training-advisor/SKILL.md) に従います。

`detect` と `fetch-vams` は、VamsCLI の保存済みのログインで VAMS を読みます。
PASK と連携した VAMS に向けた VamsCLI のプロファイルで、`vamscli auth login` を済ませておきます。
ログインが切れていると、`detect` は VAMS を使えないと判定し、`mlflow_only` として進めます。

`analyze`、`validate-proposal`、`plot` は出力先のファイルだけを読むので、AWS の認証は要りません。

## 制限

提案できるのは 12 項目だけです。
学習率、warmup のステップ数、減衰のステップ数は、ポーラーの許可リストにも `template.json` にも無いため、提案に入れられません。
本サンプルの学習率は `slurm_train.sh` から渡しておらず、LeRobot の policy の preset の値が使われます。
発散やプラトーのように学習率で対処すべき症状でも、提案は `BATCH_SIZE`、`STEPS`、`LORA_R` などの間接的な手段に限られます。
学習率などを変えるべきと Claude Code が判断した場合は、レポートの「許可リスト外の所見」に参考として書くだけです。
これらを提案の対象にするには、ポーラーの許可リスト、`template.json`、`slurm_train.sh` の変更が必要です。

`STEPS` を変えると、preset の学習率のスケジュール（warmup と cosine 減衰）も変わります。
`STEPS` と学習率のスケジュールの関係は [tuning-guide.md の「学習率のスケジュールと STEPS」](../../../../.claude/skills/pask-training-advisor/references/tuning-guide.md#学習率のスケジュールと-stepslerobot-061) にあります。

データセットの除外の候補は、VAMS のメタデータとレビューから決めます。
エピソードごとの品質のフラグ、動画の品質、行動の中身は見ません。
候補を外すかどうかは、人が VAMS の画面でエピソードを確かめてから決めます。
