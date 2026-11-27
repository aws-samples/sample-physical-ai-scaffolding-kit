# レポートの構成と書き方

SKILL.md の Step 7 で `report.md` を書くときの構成と、各節で使う JSON の項目。
`report.md` は出力ディレクトリ（`snapshot.json` などと同じ場所）に置く。

## 共通の規則

1. 見出しと本文はレポートの言語（SKILL.md の Step 2 で決めた言語）で書く。下の節の名前は日本語で書いた場合の例で、ほかの言語では訳す。
2. コマンド、ファイル名、パラメータ名（`STEPS` など）、メトリクス名（`train/loss` など）、診断のラベル（`plateau` など）、JSON のキー名は原文のまま書く。
3. 数値は JSON の値をそのまま転記する。計算し直したり、JSON に無い数値を作ったりしない。桁が多い値を丸めるときは「約」を付けて丸めたことを示す。値が `null` の項目は「記録なし」と書く。
4. 時刻は UTC と JST を併記する。`startTime` と `endTime` は UTC のエポックミリ秒なので、1000 で割って秒にしてから `date` で変換する。

   ```bash
   date -u -d @<秒> '+%Y-%m-%d %H:%M:%S UTC'
   TZ=Asia/Tokyo date -d @<秒> '+%Y-%m-%d %H:%M:%S JST'
   ```

   `endTime` が `null` の場合は「記録なし」と書く。
5. AWS のアカウント ID を含む値を載せない。`snapshot.json` の `trackingUri`（MLflow App の ARN）、ロールの ARN、アカウント ID を含む S3 のパスは書かない。run は run ID と experiment 名で示す。`publish` は ARN を含む `report.md` を拒否する。
6. 図は `report.md` と同じディレクトリの相対パスで埋め込む（例 `![Training loss](loss.png)`）。`plot` が作らなかった図（metrics が無い、比較対象が無い）は埋め込まず、作られなかった理由を 1 文で書く。
7. JSON のパスの表記: 以下の `snapshot.` は `snapshot.json`、`diagnosis.` は `diagnosis.json`、`proposal.` は `proposal.json` を指す。`features` のキーは snake_case、それ以外のキーは camelCase である。

## 1. 対象の run の要約

| 書く内容 | JSON の項目 |
|---|---|
| run 名、run ID | `snapshot.target.runName`、`snapshot.target.runId` |
| experiment 名 | `snapshot.target.experimentName` |
| status | `snapshot.target.status` |
| 開始と終了の時刻（UTC と JST） | `snapshot.target.startTime`、`snapshot.target.endTime` |
| トリガー | `snapshot.target.tags["pask.trigger"]`（`manual` / `vams`） |
| VAMS の実行 ID | `snapshot.target.tags["vams.pipeline_execution_id"]`（`vams` のときだけ） |
| データセット | `snapshot.target.tags["lerobot.dataset_repo_id"]`、`snapshot.target.tags["vams.input.asset_name"]` |
| 推定のフレーム数とエピソード数 | `diagnosis.target.features.estimated_frames`、`diagnosis.target.features.estimated_episodes`（`train/samples / train/epochs` などからの推定であることを書く） |
| 12 項目の値 | `snapshot.target.trainingParams`（12 行の表にする） |
| 計画の総ステップ数 | `snapshot.target.plannedSteps` |
| 注記 | `snapshot.target.notes` |

`snapshot.target.notes` に `planned_steps` の注記（再開した run では params の `steps` が最初の値のままのため、metrics の最終ステップを使った旨）がある場合は、その内容をレポートの言語で書く。

比較の条件として、`snapshot.criteria.datasetKey`、`snapshot.criteria.datasetValue`、`snapshot.criteria.experiments`、`snapshot.criteria.maxCompareRuns`、`snapshot.criteria.statuses` も短く書く。

## 2. 学習曲線の図

次の順に埋め込む。各図の下に、何を示す図かを 1 文で書く。

| 図 | 内容 |
|---|---|
| `loss.png` | `train/loss` の生の値と平滑化（移動中央値）、対数軸。`eval/eval_loss` があれば重ねる。チェックポイントの位置 |
| `grad_norm.png` | `train/grad_norm` |
| `lr.png` | `train/lr` |
| `throughput.png` | `train/dataloading_s` と `train/update_s`、`train/samples_per_s` |
| `gpu_mem.png` | 上の図は `train/gpu_mem_gb`（横軸はステップ）。`system/gpu_0_memory_usage_percentage` があれば、下の図に run の開始からの経過時間（分）を横軸にして描く。`system/*` の metrics の step はシステム metrics の記録の通し番号で、学習のステップと対応しないため。上下の図の横軸が違うことを図の下に書く |
| `compare_loss_step.png` | 対象と比較対象の run の `train/loss`（横軸はステップ） |
| `compare_loss_samples.png` | 対象と比較対象の run の `train/loss`（横軸は `train/samples`） |

## 3. 診断の結果

| 書く内容 | JSON の項目 |
|---|---|
| 診断のラベル | `diagnosis.target.labels` |
| 特徴量の表 | `diagnosis.target.features` |
| 所見 | `diagnosis.target.findings`（レポートの言語に訳してよい。数値は変えない） |
| 評価の損失の特徴量 | `diagnosis.target.evalFeatures`（`null` でなければ） |
| 判定に使った閾値 | `diagnosis.thresholds`（付いたラベルに関係するものだけ） |

特徴量の表には次のキーを、この順に載せる。

`num_points`、`last_step`、`planned_steps`、`completion_ratio`、`initial_loss`、`final_loss`、`min_loss`、`min_loss_step`、`loss_reduction`、`tail_slope_per_1k`、`tail_improvement`、`tail_cv`、`nonfinite_loss`、`nonfinite_grad_norm`、`loss_spikes`、`grad_norm_spikes`、`lr_progress`、`epochs`、`dataloading_ratio`、`gpu_mem_gb_max`、`gpu_memory_percent_max`、`gpu_utilization_median`、`samples_per_s_median`、`estimated_frames`、`estimated_episodes`、`final_samples`

`diagnosis.target.evalFeatures` が `null`（`eval/*` が記録されていない）の場合は、次の注記を必ず書く。

> 評価の損失が無いため、過学習は直接は判定していない。`overfit_risk` は `train/epochs` による代理の判定である。

レポートの言語が日本語以外の場合は、同じ意味の文に訳して書く。

## 4. 次のパラメータの提案

| 書く内容 | JSON の項目 |
|---|---|
| 変える項目の表（項目、前の値、提案の値、根拠、証拠） | `proposal.changes[]` の `key`、`from`、`to`、`reason`、`evidence` |
| 期待する効果 | `proposal.expectedEffect` |
| リスク | `proposal.risks`、`validate-proposal` の stdout の `warnings` |
| 代わりの案 | `proposal.alternatives[]` の `parameters`（前の run と違う項目だけ）と `reason` |
| 検証の結果 | `validate-proposal` を通ったこと |

変えない項目は、表の後に「ほかの項目は前の run と同じ」と書く。12 項目すべての値は「7. 実行する場合の手順」の `vams_parameters.json` にある。

## 5. 比較表

対象の run（先頭の列）と比較対象の run を並べる。比較対象が無い場合は、比較の条件（`snapshot.criteria`）に合う run が無かったと書く。

| 行 | JSON の項目 |
|---|---|
| run 名、run ID、experiment 名 | `diagnosis.target` / `diagnosis.comparisons[]` の `runName`、`runId`。experiment 名は `snapshot.comparisons[].experimentName` |
| 12 項目の値 | `diagnosis.target.trainingParams`、`diagnosis.comparisons[].trainingParams`。対象と違う項目（`diagnosis.comparisons[].paramDiffFromTarget` のキー）は太字にする |
| 最終の損失 | `features.final_loss` |
| 処理したサンプル数 | `features.final_samples` |
| 最小の損失 | `features.min_loss` |
| 減少率 | `features.loss_reduction` |
| 完了率 | `features.completion_ratio` |
| 処理速度 | `features.samples_per_s_median` |
| ラベル | `labels` |

`BATCH_SIZE` が違う run の最終の損失は、同じステップ数でも処理したサンプル数が違うため単純には比べられない。最終の損失の行のすぐ下に `final_samples` を置き、`compare_loss_samples.png` を参照して比べる。

## 6. 学習に使ったデータセット

`diagnosis.datasets` と `references/dataset-guide.md` に従って書く。

`diagnosis.datasets` が `null` の場合は、節の見出しだけを置き、次のどちらかを 1 文で書く。

1. `detect` が `mlflow_only` だった: VAMS に接続できなかったことと、`detect` の `vams.reason`
2. 利用者が VAMS を使わないと決めた: そのこと

`diagnosis.datasets.assets` が空の場合は、snapshot の run がどれも VAMS から実行されていないため、データセットの情報が無いと書く。

### 6.1 データセットの統計

`diagnosis.datasets.assets` を 1 行 1 アセットの表にする。対象の run の入力を先頭にする。

| 列 | JSON の項目 |
|---|---|
| アセット名、アセット ID | `assetName`、`assetId` |
| 使われ方 | `roles`（`input` / `view_source`）、`kind` |
| エピソード数、フレーム数、時間（秒） | `features.totalEpisodes`、`features.totalFrames`、`features.totalDurationSec` |
| fps、タスク数 | `features.fps`、`features.totalTasks` |
| 品質のフラグ | `features.qualityFlaggedCount` / `features.qualityEpisodeCount` と `flagCounts` |
| レビュー済みのエピソード数 | `features.reviewedEpisodeCount` |
| 統計 | `features.statsPresent`、`features.statsCountMatchesFrames` |
| このアセットを使った run | `runStatusCounts` |

`kind` が `view` のアセットには品質とレビューが無い。「元のデータセットの行を参照」と書く。

### 6.2 品質とレビューの所見

`diagnosis.datasets.findings` をレポートの言語に訳して箇条書きにする。数値は変えない。

### 6.3 次の学習から外す候補

節の先頭に、**削除ではなく、VAMS のキュレーションで次の学習の入力から外す提案であること、この skill は実行しないこと**を書く。

| 書く内容 | JSON の項目 |
|---|---|
| データセットの候補（アセット名、理由、理由の説明） | `diagnosis.datasets.datasetExclusions[]` の `assetName`、`reasons` |
| エピソードの候補（元のデータセット、エピソードの番号、理由、経由した view） | `diagnosis.datasets.episodeExclusions[]` の `assetName`、`episodeIndex`、`reasons`、`viewAssetId` |
| 既に view で外したエピソード | `diagnosis.datasets.alreadyExcludedEpisodes[]` の件数と番号 |
| 除外の手順 | `references/dataset-guide.md` の「VAMS のキュレーションで除外する手順の書き方」 |

候補が無い場合は、規則に当たるデータが無かったと書く。
理由の説明は `references/dataset-guide.md` の表に従う。

## 7. 実行する場合の手順

節の先頭に、**実行は人が提案とレポートを確認し、承認したあとに人が行うこと、この skill は実行しないこと**を書く。
続けて `vams_parameters.json` の中身（12 項目すべてを含む `templateTags`）を載せ、VamsCLI で実行する場合のコマンドの例を書く。

コマンドの例（`hyperpod/docs/ja/VAMS_INTEGRATION/OVERVIEW.md` の「学習の実行」に合わせた形）:

```bash
vamscli workflow execute --workflow-database-id "${DATABASE_ID}" -w "${WORKFLOW_ID}" \
  --input-file "${DATABASE_ID}:<入力のアセットの ID>:/" \
  --output-asset-id "<出力のアセットの ID>" \
  --pipeline-parameters "$(jq -c . <出力ディレクトリ>/vams_parameters.json)"
```

書き添えること:

1. `vams_parameters.json` は `{"<pipelineId>": {"templateId": ..., "templateTags": [...]}}` の形で、`--pipeline-parameters` にそのまま渡せる。キーの pipelineId は `validate-proposal` の `--pipeline-id` の値である。
2. `templateTags` には変えない項目も含めて 12 項目すべてを入れてある。指定しなかったタグはテンプレートの既定値になるため、項目を削らない。
3. `--input-file` の最後の `/` はアセットの全体を表す。入力にはルートに LeRobot のデータセットを持つアセットを、出力には入力と別のアセットを選ぶ。
4. `DATABASE_ID`、`WORKFLOW_ID`、アセットの ID は利用者の環境の値で、この skill は決めない。

## 8. 許可リスト外の所見

学習率、warmup、減衰のステップ数など、12 項目に無いハイパーパラメータを変えるべきと判断した場合だけ書く。無ければ節ごと省く。

| 書く内容 | 根拠にする JSON の項目 |
|---|---|
| 変えるべきと考えた項目と方向 | `proposal.risks` の該当する記述 |
| 根拠 | `diagnosis.target.labels`、`diagnosis.target.features`、`snapshot.target.lrScheduleParams` |
| 補足 | 12 項目に無いため、この提案には含めていないこと。変えるにはポーラーの許可リスト、`template.json`、`slurm_train.sh` の変更が要ること |

学習率のスケジュールと `STEPS` の関係は `references/tuning-guide.md` の「学習率のスケジュールと STEPS」に従って書く。
