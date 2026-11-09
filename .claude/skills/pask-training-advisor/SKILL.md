---
name: pask-training-advisor
description: MLflow（SageMaker MLflow App）に記録された LeRobot pi0 / pi0.5 の学習結果（train/loss などの metrics、params、tags）を診断し、次の学習パラメータ（VAMS の 12 項目 POLICY_TYPE / PRETRAINED_PATH / FINETUNE_MODE / LORA_R / LORA_ALPHA / STEPS / BATCH_SIZE / SAVE_FREQ / LOG_FREQ / NUM_WORKERS / COMPILE_MODEL / IMAGE_TAG）の提案と Markdown のレポートを作るときに必ず使う。VAMS に接続できる場合は、学習に使ったデータセットの統計（エピソード数、品質のフラグ、レビュー）をレポートに加え、次の学習から外すエピソードとデータセットを VAMS のキュレーションでの除外として提案する。「学習結果を見て次のパラメータを提案して」「MLflow の run を診断してレポートにして」「VAMS の実行 ID の学習を評価して」「この run の学習曲線は収束している？」「前の学習と比べて次の STEPS と BATCH_SIZE を決めて」「学習に使ったデータセットも見てレポートにして」「次の学習から外すべきエピソードは？」のような依頼、および MLflow の run ID や VAMS の pipelineExecutionId を示して学習の良し悪しや次の設定を尋ねられたときに読む。学習の実行（sbatch / slurm_train.sh）、VAMS の execute、シミュレーションでの closed-loop 評価、許可リスト外のハイパーパラメータ（学習率、warmup、減衰ステップなど）の変更、VAMS のアセットの削除・archive とキュレーションの実行はこの skill の対象外。
---

# PASK Training Advisor

MLflow App に記録された LeRobot の pi0 / pi0.5 の学習を診断し、次の学習パラメータの提案（`vams_parameters.json`）とレポート（`report.md`）を作る手順。
VAMS に接続できる場合は、学習に使ったデータセットの統計と、次の学習から外すデータの候補もレポートに入れる。

役割の分担は次のとおり。

| 担当 | 受け持つこと |
|---|---|
| ツール（`scripts/`、`uv run` で実行） | 接続先の確認、MLflow と VAMS からの取得、特徴量と診断のラベルの計算、除外の候補の抽出、提案の検証と VAMS 形式への変換、図の生成、MLflow の artifact への保存 |
| Claude Code（この手順） | 12 項目のどれをどう変えるかの判断と根拠、`proposal.json` の作成、除外の候補をどう扱うかの判断、`report.md` の執筆 |

数値の計算と制約の検証はツールが行う。Claude Code は数値を計算し直さない。

**AWS のプロファイルとリージョンは、`.claude/settings.local.json`（git 管理外）の `env` に `DEPLOY_AWS_PROFILE` と `CDK_DEFAULT_REGION` の名前で置く。** `AWS_PROFILE` / `AWS_REGION` を `env` に直接書くと Claude Code 自身の AWS 認証まで変わるため、この名前にしている。このファイルのコマンド例はすべて `AWS_PROFILE="$DEPLOY_AWS_PROFILE" AWS_REGION="$CDK_DEFAULT_REGION"` を前に付けて、そのコマンドにだけ渡す形で書いてある。

## 前提

- 作業ディレクトリはプロジェクトのルート（`.claude/` がある場所）。
- ツールの呼び方は次のとおり。以下では `<sub>` の部分だけを書き換える。

  ```bash
  uv run --project .claude/skills/pask-training-advisor/scripts pask-training-advisor <sub>
  ```

- MLflow の接続先は環境変数 `MLFLOW_TRACKING_URI`（MLflow App の ARN）で渡す。ARN はアカウント ID を含むため、レポート・commit メッセージ・利用者への要約に書かない。
- 作業端末のプロファイルに MLflow App を読む権限 `sagemaker:CallMlflowAppApi` が必要。無い場合、`fetch` と `publish` は権限エラーで失敗する。
- VAMS は VamsCLI（`vamscli`）の保存済みのログインで読む。プロファイルは `--vams-profile`、省略時は環境変数 `VAMS_CLI_PROFILE`（`.claude/settings.local.json` の `env`）、それも無ければ `default` を使う。PASK と連携した VAMS を向いたプロファイルを使う。ツールが使う VamsCLI のコマンドは読み取りだけである。
- MLflow の params と metrics は変更しない。`publish` は artifact を追加するだけである。
- 提案した学習は実行しない。VAMS の execute は人が承認したあとに人が行う。
- データは削除しない。除外の候補は、人が VAMS のキュレーションで次の学習の入力から外すための提案である。

## 出力ファイル

すべて同じ出力ディレクトリに置く。

| ファイル | 書く者 | 内容 |
|---|---|---|
| `snapshot.json` | `fetch` | 対象の run と比較対象の run の RunInfo、params、tags、metrics の全履歴、12 項目の値（`trainingParams`）、学習率のスケジュールの params（`lrScheduleParams`）、注記（`notes`） |
| `datasets.json` | `fetch-vams` | run が使った VAMS のアセット（入力と view の元）のメタデータ（`assets`）、snapshot の run の入力と view が残したエピソード（`snapshotRuns`）、同じアセットを使った全 run の status（`runsUsingAssets`）。VAMS に接続できない場合は作らない |
| `diagnosis.json` | `analyze` | run ごとの特徴量（`features`）、診断のラベル（`labels`）、所見（`findings`）、比較対象との 12 項目の差（`paramDiffFromTarget`）、使った閾値（`thresholds`）。`datasets.json` がある場合はデータセットの診断（`datasets`）。無い場合 `datasets` は `null` |
| `proposal.json` | Claude Code | 次の 12 項目の値と、変えた項目ごとの根拠 |
| `vams_parameters.json` | `validate-proposal` | VAMS の `pipelineExecutionParameters` の形の提案。検証を通ったときだけ書かれる |
| `loss.png`、`grad_norm.png`、`lr.png`、`throughput.png`、`gpu_mem.png` | `plot` | 対象の run の学習曲線。metrics が無い図は作られない |
| `compare_loss_step.png`、`compare_loss_samples.png` | `plot` | 比較対象の run の `train/loss` を重ねた図。比較対象が無いときは作られない |
| `report.md` | Claude Code | レポート |

出力ディレクトリの既定値は `samples/lerobot-openpi/outputs/advisor/<UTC タイムスタンプ>/`（`.gitignore` 済み）で、`fetch` が作る。

## 手順

### Step 0: 接続先を確かめる（detect）

MLflow と VAMS のどちらを読めるかを確かめる。`MLFLOW_TRACKING_URI` は Step 3 の表と同じ方法で用意してから実行する。

```bash
AWS_PROFILE="$DEPLOY_AWS_PROFILE" AWS_REGION="$CDK_DEFAULT_REGION" \
  MLFLOW_TRACKING_URI="$MLFLOW_TRACKING_URI" \
  uv run --project .claude/skills/pask-training-advisor/scripts pask-training-advisor detect
```

stdout の `mode` で進め方を決める。

| `mode` | 意味 | 対応 |
|---|---|---|
| `full` | MLflow と VAMS を読める | 全手順を行う。データセットの節を含むレポートを作る |
| `mlflow_only` | MLflow だけを読める | `fetch-vams` を飛ばして進める。`vams.reason` を利用者に示し、レポートのデータセットの節に VAMS に接続できなかったことと理由を書く |
| `vams_only` | VAMS だけを読める | **レポートを作らずに止まる。** 学習に使ったデータセットは MLflow の run から辿るため、MLflow が無いと特定できない。`mlflow.reason` を示し、レポートに必要な学習結果が無いことを伝える |
| `none` | どちらも読めない | **レポートを作らずに止まる。** `mlflow.reason` と `vams.reason` を示し、レポートに必要な情報が無いことを伝える |

`canReport` が `false` のとき（`vams_only` と `none`）は、以降の手順に進まない。

`vams.reason` が認証切れ（`Authentication token has expired` など）の場合は、VamsCLI に再ログインすれば `full` にできることを利用者に伝え、進め方を確かめる。ログインは利用者が行う（パスワードをこの会話に出さないため）。

### Step 1: 対象と出力先の確認（必須・スキップ不可）

次の値を利用者の依頼から読み取る。足りないものは推測せず、利用者に尋ねる（AskUserQuestion）。

| 値 | 引数 | 必須 | 確認のポイント |
|---|---|---|---|
| 対象の run | `--run-id` または `--pipeline-execution-id` | どちらか一方 | VAMS から実行した学習は pipelineExecutionId（タグ `vams.pipeline_execution_id`）でも探せる |
| 比較対象の experiment | `--experiment`（複数可） | 任意 | 省略時は `vams-training` と `lerobot-pi`。同じデータセット（`vams.dataset_digest`、無ければ `lerobot.dataset_repo_id`）で status が `FINISHED` の run だけが比較対象になる |
| 比較する run の数 | `--max-compare-runs` | 任意 | 省略時は 5 |
| VAMS の pipelineId | `--pipeline-id`（`validate-proposal`） | 必須 | 既定値は無い。`vams_parameters.json` のキーになる。VAMS から実行した run ではタグ `vams.pipeline_name` に pipelineId が入っている（`snapshot.json` の `target.tags`）ので、その値を利用者に示して確かめる |
| 出力先 | `--output-dir` | 任意 | 利用者が指定したらその場所を使う。指定が無ければ既定値（`fetch` が作る） |
| VamsCLI のプロファイル | `--vams-profile`（`detect`、`fetch-vams`） | 任意 | 省略時は `VAMS_CLI_PROFILE`、それも無ければ `default`。`detect` で `full` になったプロファイルを `fetch-vams` にも渡す |

### Step 2: レポートの言語を決める

次の規則で決める。

1. 利用者がこの会話で使っている言語に合わせる。
2. 複数の言語が使われているなど、どの言語で書くか判断できない場合は、レポートを書く前に利用者に確認する。
3. 利用者が言語を指定した場合は、その言語で書く。
4. コマンド、ファイル名、パラメータ名（`STEPS` など）、メトリクス名（`train/loss` など）、診断のラベル（`plateau` など）は、言語によらず原文のまま書く。

図の軸の名前と凡例は、レポートの言語によらず英語である（`plot` が英語で描く）。

### Step 3: 取得と診断（fetch / fetch-vams / analyze）

`.claude/settings.local.json` の `env` を確認する。

```bash
cat .claude/settings.local.json
echo "profile=$DEPLOY_AWS_PROFILE region=$CDK_DEFAULT_REGION"
```

| 状態 | 対応 |
|---|---|
| `DEPLOY_AWS_PROFILE` / `CDK_DEFAULT_REGION` が未設定・片方のみ | コマンドを実行せず、プロファイルとリージョンを利用者に確認し、承認を得てから `settings.local.json` の `env` に追記する。値を推測しない |
| `MLFLOW_TRACKING_URI` がシェルに無い | MLflow App の ARN を利用者に尋ね、下のコマンドの `"$MLFLOW_TRACKING_URI"` をその値に置き換えて、このプロセスにだけ渡す。ARN を推測しない |
| `env` に `AWS_PROFILE` / `AWS_REGION` が直接書かれている | そのまま使わない。値はそのままで名前を `DEPLOY_AWS_PROFILE` / `CDK_DEFAULT_REGION` に変えることを利用者に提案し、承認を得てから変える |

`fetch` で MLflow から取得する。run ID で指定する場合の例:

```bash
AWS_PROFILE="$DEPLOY_AWS_PROFILE" AWS_REGION="$CDK_DEFAULT_REGION" \
  MLFLOW_TRACKING_URI="$MLFLOW_TRACKING_URI" \
  uv run --project .claude/skills/pask-training-advisor/scripts pask-training-advisor fetch \
  --run-id "<run ID>"
```

VAMS の実行から辿る場合と、比較対象や出力先を指定する場合の例:

```bash
AWS_PROFILE="$DEPLOY_AWS_PROFILE" AWS_REGION="$CDK_DEFAULT_REGION" \
  MLFLOW_TRACKING_URI="$MLFLOW_TRACKING_URI" \
  uv run --project .claude/skills/pask-training-advisor/scripts pask-training-advisor fetch \
  --pipeline-execution-id "<pipelineExecutionId>" \
  --experiment vams-training --experiment lerobot-pi \
  --max-compare-runs 5 \
  --output-dir "<出力先>"
```

`fetch` は stdout に JSON を出す。その `outputDir` が以降のすべてのサブコマンドの `--output-dir` になる。以下では `<DIR>` と書く。

Step 0 が `full` の場合は、`fetch-vams` で学習に使った VAMS のデータセットを読む。MLflow も読む（同じアセットを使った run の status と、view の run の `vams/curation_manifest.json`）。

```bash
AWS_PROFILE="$DEPLOY_AWS_PROFILE" AWS_REGION="$CDK_DEFAULT_REGION" \
  MLFLOW_TRACKING_URI="$MLFLOW_TRACKING_URI" \
  uv run --project .claude/skills/pask-training-advisor/scripts pask-training-advisor fetch-vams \
  --output-dir "<DIR>"
```

対象の run と比較対象の run がどれも VAMS から実行されていない（手動の学習）場合、`assets` が空の `datasets.json` ができる。`analyze` は所見に `no run of the snapshot was started from VAMS` を書く。

`analyze` は `snapshot.json` と、あれば `<DIR>/datasets.json` を読む。MLflow への接続も AWS の認証も要らない。`datasets.json` が別の対象の run のものだと失敗する。

```bash
uv run --project .claude/skills/pask-training-advisor/scripts pask-training-advisor analyze \
  --output-dir "<DIR>"
```

どのサブコマンドも stdout に JSON を 1 つ出す。ファイルが無い、run を取得できないなどの失敗は終了コード 2 で `{"ok": false, "error": ...}` が出る（スタックトレースは stderr）。

`fetch` の失敗の扱い:

| 症状 | 対応 |
|---|---|
| `AccessDenied` などの権限エラー | プロファイルに `sagemaker:CallMlflowAppApi` が無い。事実を利用者に伝えて止まる。権限を勝手に追加しない |
| `MLFLOW_TRACKING_URI` が無いというエラー | Step 3 の表に戻る |
| pipelineExecutionId の書式エラー | `^[A-Za-z0-9_-]+$` に合う値を利用者に確かめる |
| run が見つからない | run ID や experiment を利用者に確かめる |

`fetch-vams` が `VamsError` で失敗した場合（Step 0 のあとで認証が切れたなど）は、事実を利用者に伝え、再ログインして再実行するか、`mlflow_only` として進めるかを確かめる。

### Step 4: 提案を書く（proposal.json）

次のファイルを読む。

1. `<DIR>/diagnosis.json`（`target.labels`、`target.features`、`target.findings`、`comparisons[]`）
2. `<DIR>/snapshot.json` の `target.trainingParams`、`target.lrScheduleParams`、`target.notes`
3. `references/tuning-guide.md`（ラベルと 12 項目の対応、学習率のスケジュールと `STEPS` の関係）
4. `scripts/config/proposal_constraints.json`（値の範囲、項目間の関係、変える項目の数と倍率の上限、固定する項目）
5. `hyperpod/vams/template.json`（`tagSchema` の型と既定値）
6. `<DIR>/diagnosis.json` の `datasets`（`null` でなければ）と `references/dataset-guide.md`

データセットの診断は、12 項目の判断の根拠にもなる。例えば `few_episodes` が付いた入力で `overfit_risk` が出ている場合、`STEPS` を増やすより先に、データを増やすことを `risks` と「学習に使ったデータセット」の節に書く。

`<DIR>/proposal.json` を次の形で書く。

```json
{
  "schemaVersion": 1,
  "baseRunId": "<対象の run の ID>",
  "parameters": {
    "POLICY_TYPE": "pi05",
    "PRETRAINED_PATH": "lerobot/pi05_base",
    "FINETUNE_MODE": "lora",
    "LORA_R": 16,
    "LORA_ALPHA": 16,
    "STEPS": 60000,
    "BATCH_SIZE": 4,
    "SAVE_FREQ": 5000,
    "LOG_FREQ": 100,
    "NUM_WORKERS": 4,
    "COMPILE_MODEL": false,
    "IMAGE_TAG": "latest"
  },
  "changes": [
    {
      "key": "STEPS",
      "from": 30000,
      "to": 60000,
      "reason": "末尾の傾きが負で、損失がまだ下がっている",
      "evidence": ["converging", "tail_slope_per_1k=-0.012"]
    }
  ],
  "expectedEffect": "…",
  "risks": ["…"],
  "alternatives": [{"parameters": {"...": "12 項目すべて"}, "reason": "…"}]
}
```

書き方の規則:

- `parameters` には 12 項目すべてを入れる。変えない項目も前の run の値（`snapshot.json` の `target.trainingParams`）をそのまま入れる。VAMS では指定しなかったタグがテンプレートの既定値に戻るためである。
- 型は `tagSchema` に合わせる。整数は JSON の数値、`COMPILE_MODEL` は `true` / `false`、それ以外は文字列。
- 前の run から変えた項目は、すべて `changes[]` に書く。`from` は前の run の値、`to` は `parameters` の値と同じにする。
- `changes[].reason` に判断の理由を、`changes[].evidence` に根拠を書く。根拠には診断のラベル名（`converging` など）か、`diagnosis.json` の特徴量を `キー=値` の形（`tail_slope_per_1k=-0.012` など）で書く。値は `diagnosis.json` の値をそのまま写す。
- `references/tuning-guide.md` の規則は推奨である。ガイドと違う判断をする場合も、理由と根拠を `changes[]` に書く。
- 学習率、warmup、減衰ステップなど許可リスト外の項目を変えるべきと判断した場合は、`parameters` に入れない。`risks` に参考として書き、レポートの「許可リスト外の所見」にも書く。
- `alternatives[].parameters` にも 12 項目すべてを入れる。`validate-proposal` は代わりの案を形式の規則と運用の制約で検証し、違反と `FINETUNE_MODE` などの警告を `alternatives[i]: ` で始まる `warnings` に出す。代わりの案の違反は検証を止めない。
- `insufficient_data` か `failed_or_killed` が付いた run では、変更を最小にし、原因の確認を `risks` に書く。

### Step 5: 提案を検証する（validate-proposal）

```bash
uv run --project .claude/skills/pask-training-advisor/scripts pask-training-advisor validate-proposal \
  --output-dir "<DIR>" --pipeline-id "<pipelineId>"
```

- 通った場合は終了コード 0 で、`<DIR>/vams_parameters.json` が書かれる。
- 失敗した場合は終了コード 1 で、stdout に `{"ok": false, "violations": [{"key": ..., "rule": ..., "message": ...}], ...}` が出る。`vams_parameters.json` は書かれない。`proposal.json` が JSON として読めない、または形が違う場合も終了コード 1 で、`rule` が `schema` の violation になる。
- 失敗したら `violations` の `key`、`rule`、`message` を読んで `proposal.json` を直し、通るまで繰り返す。制約（`proposal_constraints.json`）を緩めて通さない。
- stdout の `warnings`（例: `FINETUNE_MODE` を `expert_only` / `full` にしたときの GPU メモリの警告）は、レポートのリスクに書く。

### Step 6: 図を作る（plot）

```bash
uv run --project .claude/skills/pask-training-advisor/scripts pask-training-advisor plot \
  --output-dir "<DIR>"
```

作られた PNG を Read で開き、曲線の形が `diagnosis.json` のラベルと食い違っていないかを見る。食い違っていれば、レポートの所見にそのことを書く（ラベルや数値は書き換えない）。

### Step 7: レポートを書く（report.md）

`snapshot.json`、`diagnosis.json`、`proposal.json`、`vams_parameters.json`、図を読み、`references/report-template.md` に従って `<DIR>/report.md` を書く。

- 言語は Step 2 で決めた言語にする。見出しもその言語で書く。
- 数値は JSON の値をそのまま転記する。計算し直したり、JSON に無い数値を作ったりしない。値が `null` の項目は、記録が無いと書く。
- 時刻は UTC と JST を併記する（変換の方法は `references/report-template.md`）。
- AWS のアカウント ID を含む値（`snapshot.json` の `trackingUri` など）は載せない。run ID と experiment 名だけを載せる。
- 評価の損失（`eval/*`）が記録されていない場合は、「評価の損失が無いため、過学習は直接は判定していない」ことを必ず書く。
- 「学習に使ったデータセット」の節は、`diagnosis.json` の `datasets` と `references/dataset-guide.md` に従って書く。`mlflow_only` の場合は、VAMS に接続できなかったことと `detect` の `vams.reason` を書く。
- 図は `report.md` と同じディレクトリの相対パス（`![...](loss.png)`）で埋め込む。

### Step 8: MLflow の artifact に保存する（任意）

利用者が求めた場合だけ実行する。`report.md`、図、JSON を対象の run の artifact `advisor/<UTC タイムスタンプ>/` に保存する。`--run-id` を省略すると対象の run に保存する。

```bash
AWS_PROFILE="$DEPLOY_AWS_PROFILE" AWS_REGION="$CDK_DEFAULT_REGION" \
  MLFLOW_TRACKING_URI="$MLFLOW_TRACKING_URI" \
  uv run --project .claude/skills/pask-training-advisor/scripts pask-training-advisor publish \
  --output-dir "<DIR>"
```

`report.md` に ARN が含まれていると `publish` は失敗する。ARN を消してから再実行する。

### Step 9: 要点を示して止まる

利用者に次を示して止まる。

1. 診断のラベルと、変えた項目と根拠（`changes[]` の要約）
2. 除外の候補（`datasetExclusions` と `episodeExclusions` の要約）。候補が無ければ無いと書く
3. `report.md` と `vams_parameters.json` のパス
4. VamsCLI で実行する場合のコマンドの例（`references/report-template.md` の「実行する場合の手順」と同じもの）。実行は人が提案とレポートを確認し、承認したあとに人が行うことを添える

## 禁止事項

- **VAMS の execute（`vamscli workflow execute` など）を実行しない。** 学習の実行（`sbatch`、`slurm_train.sh`）も行わない
- **MLflow の params、metrics、tags を変更しない。** run を削除しない
- **VAMS のアセットを削除・archive しない。キュレーションの view を作らない。メタデータを書き換えない。** 除外は提案に留め、人が VAMS で行う
- **MLflow か VAMS に接続できないのに、記録が無い値を推測してレポートを作らない。** `canReport` が `false` なら止まる
- **レポートの数値を計算し直さない。JSON に無い数値を書かない**
- **AWS のアカウント ID を含む ARN をレポートと要約に書かない**
- **`validate-proposal` を通っていない提案をレポートや `vams_parameters.json` として出さない。** 検証を通すために `proposal_constraints.json` や `template.json` を書き換えない
- **許可リスト外の項目を `parameters` に入れない**
- **プロファイル名・リージョン名・ARN をコマンド・ドキュメントにハードコードしない**
