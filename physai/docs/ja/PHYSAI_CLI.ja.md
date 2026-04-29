# physai CLI の詳細

physai CLI は、ローカルマシンから SSH 経由でリモートの HyperPod クラスター上のワークロードを操作するコマンドラインツールです。エージェントやデーモン、独自の状態管理は一切なく、ジョブの状態はすべて Slurm が管理します。

## 1. アーキテクチャ

```
Developer machine                          HyperPod cluster
──────────────────                         ─────────────────
physai CLI ──── SSH ────────────────────→  Login node
  │                                          │
  ├── rsync files to cluster                 ├── sbatch (submit jobs)
  ├── ssh: submit sbatch                     ├── squeue/sacct (query jobs)
  ├── ssh: tail -f log (stream)              └── scancel (cancel jobs)
  └── Ctrl-C detaches (job keeps running)
                                           Worker nodes (GPU/CPU)
                                             └── Slurm jobs run here
```

CLI は subprocess の `ssh` を使用するため、ユーザーの SSH 設定、エージェント転送、ProxyCommand をそのまま引き継ぎます。`ssh <host>` が通れば、それ以上のセットアップは不要です。

## 2. 設定

`~/.physai/config.yaml`:

```yaml
host: physai-login
model_config_roots:
  - <path-to-physai>/examples/so101-gr00t/model_configs
```

> **スキーマ**: [`cli-config.schema.json`](../../cli/physai/schemas/cli-config.schema.json)

クラスター上のパスは規約により固定されています。

| パス | 用途 |
|------|------|
| `/fsx/physai/logs/` | ジョブログ: `<job-id>.out` |
| `/fsx/physai/builds/` | ビルド作業ディレクトリ |
| `/fsx/physai/sync/` | rsync された設定ファイルとモデル設定 |
| `/fsx/enroot/` | エクスポート済み squashfs イメージ |
| `/fsx/datasets/` | データセット（名前で参照） |
| `/fsx/checkpoints/` | チェックポイント（名前で参照） |
| `/fsx/raw/` | 生データ（S3 からの DRA） |
| `/fsx/evaluations/` | 評価結果の出力先 |

## 3. コマンド

すべてのワークロードコマンドは Slurm ジョブを投入し、デフォルトではそのログをローカルターミナルにストリーミングします。Ctrl-C で切断してもリモートジョブは継続します。`-n` / `--no-stream` を指定すると、投入後すぐに制御を返します。

### 3.1. ジョブ管理コマンド

Slurm ジョブを管理するためのコマンドです。

```bash
physai list   [--host HOST]
physai status <job-id> [--host HOST]
physai logs   <job-id> [--host HOST]
physai cancel <job-id> [--host HOST]
physai clean  [--older-than DAYS] [--all] [--enroot] [--dry-run] [-f] [--host HOST]
physai doctor [--host HOST]
```

#### 3.1.1. ジョブのメタデータ

ジョブは完全に Slurm で追跡されます。外部データベースは使用しません。

- `--job-name`: 種別と名前をエンコードします — `physai/<type>/<name>`（例: `physai/build/leisaac-runtime`、`physai/train/so101-liftcube-gr00t`）
- `--comment`: 256 文字以内の自由記述です（例: `produces=/fsx/checkpoints/run-20260415-155400`）。`pipeline.py` の `_find_active_job_producing` が、現在のランが必要とするアーティファクトを生成中の待機中／実行中ジョブを検出するために利用します。後述の永続化に関する注意点も参照してください。
- `--output`: 常に `/fsx/physai/logs/%j.out` です

`physai list` はジョブ名をパースして種別と名前を抽出します。

**`--comment` の永続化に関する注意:** Slurm は `--comment` を `slurmctld` のメモリ上で保持し（`squeue` / `scontrol show job` で表示可能）、デフォルトでは `slurmdbd` の accounting には**永続化しません**。そのため `sacct` は完了したジョブの Comment カラムを空で返します。これは HyperPod の標準挙動です（`slurm.conf` に `AccountingStoreFlags=job_comment` が設定されていないため）。アクティブジョブ検出は `squeue` のみを参照するため、パイプライン依存関係の検出にはデフォルト設定で十分です。将来的に完了済みジョブが生成したアーティファクトを参照したい機能（例: 「どの過去のジョブが `/fsx/checkpoints/X` を生成したか？」）が必要になった場合は、クラスターの `slurm.conf` に `AccountingStoreFlags=job_comment` を追加し、`scontrol reconfigure` を実行してください。

#### 3.1.2. sacct が利用可能な場合（完了済みジョブも表示）

```bash
$ physai list
JOB_ID  TYPE   NAME                    STATE      ELAPSED  COMMENT
238     build  leisaac-runtime         RUNNING    12:34    base=nvcr.io/nvidia/cuda:12.8.1-cudnn-devel-ubuntu24.04
237     eval   so101-liftcube-gr00t    COMPLETED  16:22
236     train  so101-liftcube-gr00t    COMPLETED  3:42:10
```

COMMENT カラムは待機中／実行中のジョブ（`squeue` 由来）に対してのみ表示されます。完了済みの行は `sacct` 由来で、クラスターに `AccountingStoreFlags=job_comment` が設定されていない限り空欄となります（上記の注意事項を参照）。

#### 3.1.3. sacct が利用不可の場合（キュー中 / 実行中のジョブのみ）

```bash
$ physai list
JOB_ID  TYPE   NAME                    STATE      TIME     COMMENT
238     build  leisaac-runtime         RUNNING    12:34    --rebuild

(sacct not available — only active jobs shown)
```

`physai logs <job-id>` は sacct の有無にかかわらず動作します。`/fsx/physai/logs/<job-id>.out` を直接参照するためです。

#### 3.1.4. クリーンアップ

ビルドディレクトリ（`/fsx/physai/builds/`）やログファイル（`/fsx/physai/logs/`）は時間とともに蓄積されます。`physai clean` で古いものを削除できます。

```bash
physai clean                    # remove items older than 7 days (default)
physai clean --older-than 3     # older than 3 days
physai clean --all              # remove all
physai clean --enroot           # also remove stale Enroot containers from worker nodes (leftover runtime instances from jobs that didn't exit cleanly; does NOT delete .sqsh images)
physai clean --dry-run          # show what would be removed
physai clean -f                 # skip confirmation
```

実行中のジョブに属するファイルは削除されません。

`physai cancel <job-id>` は `scancel <job-id>` を実行します。`physai run` が設定する Slurm の `--dependency=afterok` チェーンにより、あるステージをキャンセルすると、同じ実行の保留中の後続ステージも `DependencyNeverSatisfied` の理由で停止します。

#### 3.1.5. Doctor

`physai doctor` はログインノードの SSH セッション上で実行される読み取り専用のヘルスチェックです。固定のチェックリストを実行し、各チェックが `PASS`、`FAIL`、`WARN` のいずれかを返します（オプションのメッセージ付き）。自動修復が可能なチェックについては、修復前にインタラクティブに確認を求め（`y/N`、デフォルトは No）、修復後にチェックを再実行して結果を確認します。

現在のチェック項目:

- **FSx ディレクトリ**: `/fsx/{raw,datasets,checkpoints,evaluations,enroot,physai}` の各パスに対して `stat` を実行します。ディレクトリの存在と、エントリごとの正確なパーミッションを確認します（`/fsx/enroot` は 1777、それ以外は 0777）。修復: `mkdir -p` + ディレクトリごとの `chmod` を単一のリモートコマンドにまとめて実行します。
- **ワーカー間の Slurm 設定のずれ**: `sinfo -h -o "%N" -N | sort -u` で全ノードを取得し、各ノードで `srun -N1 -w <node> --overlap --time=0:00:30 md5sum /var/spool/slurmd/conf-cache/{slurm,cgroup,plugstack,gres,accounting}.conf` を実行します。到達可能なすべてのノードが同一のハッシュを返す必要があります。到達不能なノードは WARN として扱います（ずれとはカウントしません）。修復: ログインノードで `scontrol reconfigure` を実行します（slurmctld が全ワーカーに設定をプッシュします）。**制限事項**: コントローラーのディスク上の `/opt/slurm*/etc/slurm.conf` が編集済みだが reconfigure が未実行のケースは検出できません（コントローラーへの SSM アクセスが必要であり、doctor はそれを行いません）。
- **slurmdbd への到達性**: `sacct -n --parsable2 -S now-1hour -o JobID` が終了コード 0 で完了するかを確認します。自動修復なし。FAIL メッセージには RDS インスタンス、Secrets Manager 内の slurmdbd 認証情報、コントローラー上の slurmdbd を調査するための SSM コマンドが示されます。

いずれかのチェックが修復の機会を経てもなお FAIL の場合、コマンドは非ゼロで終了します。

### 3.2. データコマンド

データの管理に使用するコマンドです。

```
physai ls <category> [<path>] [--host HOST]     # list remote data
physai upload <category> <local-path> [--host HOST]  # upload data to cluster
physai rm <category> <name> [-f] [--host HOST]  # remove a remote artifact
```

#### 3.2.1. physai ls

クラスター上のリモートデータを参照します。`du -sh` で算出された各トップレベルエントリの人間が読めるサイズを表示します。

```bash
$ physai ls datasets
so101_liftcube/          455M
so101_pickorange/        1.2G

$ physai ls checkpoints
gr00t-n1.6-liftcube-30k/   12G
test-run-1/                 4.1G

$ physai ls raw
pickorange.hdf5          580G
```

実装: `ssh <host> ls -lh /fsx/<category>/`（ディレクトリの場合は `du -sh`）。

#### 3.2.2. physai upload

クラスターにデータをアップロードします。

```bash
# Raw demos — prompts to recommend uploading to S3 first, then rsyncs to /fsx/raw/
physai upload raw /path/to/my-demo-dir/

# Pre-converted dataset → /fsx/datasets/ directly
physai upload datasets /path/to/so101_liftcube/

# Checkpoint → /fsx/checkpoints/ directly
physai upload checkpoints /path/to/checkpoint-dir/
```

`datasets` と `checkpoints` は `/fsx/<category>/` に直接 rsync されます。`raw` も `/fsx/raw/` に rsync しますが、CLI はまず S3 経由のアップロードを推奨し、確認を求めます。大容量の生データの場合、通常は S3 の方が適しています（次のセクションを参照）。

##### 生データと S3 自動インポート

`/fsx/raw/` は S3 データバケットの `s3://<bucket>/raw/` に FSx **Data Repository Association (DRA)** でリンクされています。このリンクは**自動インポートのみ**です。`s3://<bucket>/raw/` に置いたオブジェクトは `/fsx/raw/` に自動的に表示され、最初のアクセス時に遅延読み込みされます。ジョブが実際にファイルを開くまで FSx の容量は消費しません。

実用上の影響:

- **大容量の生データは S3 へのアップロードを推奨します**（`aws s3 cp` を使用）。後でジョブがファイルを開くと、必要なバイトがオンデマンドで遅延読み込みされます。FSx は実際に読み取られた分のみキャッシュするため、600 GB の生 HDF5 を FSx に複製するよりもはるかにコスト効率が良くなります。
- **`physai upload raw`** は `/fsx/raw/` に直接 rsync します。S3 を経由するほどでもない小さなファイルには便利ですが、大容量データの場合は S3 へのアップロードを促すプロンプトが表示されます。
- **S3 での削除は `/fsx/raw/` に伝播します**（DRA ポリシーに `DELETED` が含まれるため）。`/fsx/raw/` での変更は S3 にエクスポートされません。これは一方向（インポートのみ）のリンクです。

S3 経由で生データをアップロードするには、まず `PhysaiInfraStack` の CloudFormation 出力からバケット名を取得します。

```bash
aws cloudformation describe-stacks --stack-name PhysaiInfraStack \
  --query 'Stacks[0].Outputs[?OutputKey==`DataBucketName`].OutputValue' \
  --output text
```

その後、アップロードします。

```bash
aws s3 cp --recursive /path/to/my-demo-dir/ s3://<data-bucket>/raw/my-demo-dir/

# verify it's visible on the cluster
physai ls raw
```

#### 3.2.3. physai rm

指定した名前のアーティファクトをクラスターから削除します:

```bash
physai rm datasets so101_liftcube
physai rm checkpoints gr00t-n1.6-liftcube-30k
physai rm raw my-demo-dir
physai rm evaluations run-20260429-154500
physai rm datasets foo -f   # 確認プロンプトをスキップ
```

カテゴリ: `raw`、`datasets`、`checkpoints`、`evaluations`。パスは `/fsx/<category>/<name>` に解決されます（`<name>` にスラッシュが含まれる場合はパスエスケープ防止のため拒否されます）。

削除前に `rm` は次の処理を行います:

- パスの種別（ファイル／ディレクトリ／存在しない）を確認します。存在しない場合は明示的なエラーで失敗します。
- `_find_active_job_producing` に対して、同じパスを生成中のアクティブなパイプラインジョブがあるかを問い合わせます。該当するジョブがある場合は削除を拒否し、ジョブ ID を表示します（ユーザーは先に `physai cancel` で取り消す必要があります）。
- 解決されたパス、種別、`du -sh` で算出したサイズを表示し、`[y/N]` のプロンプトを出します。`-f` / `--force` でプロンプトをスキップできます。
- `raw` の場合は、`/fsx/raw/` が S3 からの DRA キャッシュである旨も表示します — ローカルの退避は非破壊的であり、オブジェクトは遅延再インポートによって引き続き利用可能です。

### 3.3. パイプラインコマンド

パイプラインの管理に使用するコマンドです。

```bash
physai build <container-folder> [--rebuild] [-n|--no-stream] [--host HOST]
physai run   --config <local-yaml> [--from STAGE] [--to STAGE] [--raw NAME] [--dataset NAME] [--checkpoint NAME] [--max-steps N] [--eval-rounds N] [--visual] [--model-config-root PATH] [-n|--no-stream] [--host HOST]
physai train --config <local-yaml> --dataset <name> [--max-steps N] [--model-config-root PATH] [-n|--no-stream] [--host HOST]
physai eval  --config <local-yaml> --checkpoint <name> [--eval-rounds N] [--visual] [--model-config-root PATH] [-n|--no-stream] [--host HOST]
```

#### 3.3.1. パスの解決

CLI は 2 種類の参照を使用します。

- **ローカルパス** — 設定ファイル、コンテナ定義です。ジョブ投入前にクラスターへ rsync されます。
- **名前** — データセット、チェックポイント、生データです。これらは大容量のため `/fsx` 上に配置されます。CLI が名前を `/fsx/` パスに解決します。

| 引数 | 参照元 | 解決方法 |
|------|--------|----------|
| `--config <path>` | ローカルファイル | クラスターへ rsync |
| `model.config_dir`（YAML 内） | 相対名 | モデル設定の検索パスで解決後、rsync |
| `--raw <name>` | ディレクトリ名 | → `/fsx/raw/<name>/` |
| `--dataset <name>` | ディレクトリ名 | → `/fsx/datasets/<name>/` |
| `--checkpoint <name>` | ディレクトリ名 | → `/fsx/checkpoints/<name>/` |
| `<container-folder>` | ローカルディレクトリ | クラスターへ rsync |

#### 3.3.2. モデル設定の解決

YAML 内の `model.config_dir` は相対名です（例: `gr00t-n1.6/so101-singlecam`）。CLI は設定済みのモデル設定パスを検索して一致するローカルディレクトリを見つけ、クラスターへ rsync します。

検索パスは `--model-config-root`（コマンドごと）または `~/.physai/config.yaml` の `model_config_roots`（デフォルト）で設定します。CLI は各検索パスを順にチェックし、最初に一致したものを使用します。

例:

```bash
# Search path configured in ~/.physai/config.yaml
physai train --config examples/so101-gr00t/configs/so101_liftcube_gr00t-n1.6.yaml \
             --dataset so101_liftcube --max-steps 10000

# Or override per-command
physai train --config examples/so101-gr00t/configs/so101_liftcube_gr00t-n1.6.yaml \
             --model-config-root examples/so101-gr00t/model_configs \
             --dataset so101_liftcube --max-steps 10000
```

**CLI の動作:**

1. 設定 YAML から `model.config_dir: gr00t-n1.6/so101-singlecam` を読み取ります
2. モデル設定パスを検索し、`.../model_configs/gr00t-n1.6/so101-singlecam` で見つけます
3. 設定ファイルと解決済みのモデル設定ディレクトリを `/fsx/physai/sync/<run-id>/` へ rsync します
4. `so101_liftcube` を `/fsx/datasets/so101_liftcube` に解決します
5. 解決済みのクラスターパスをエントリポイントの引数として含む sbatch スクリプトを生成します

#### 3.3.3. ビルドワークフロー

```bash
physai build examples/so101-gr00t/containers/leisaac-runtime
```

クラスター上のコンテナは **Enroot**（軽量なルートレスコンテナランタイム）でビルド・実行され、**Pyxis**（`srun --container-image=...` を通じて Enroot を使えるようにする Slurm プラグイン）と組み合わせて使用します。2 つの概念を区別してください。

- **イメージ** — ビルド成果物です。`/fsx/enroot/<name>.sqsh` にある squashfs ファイルです。`physai build` ごとに 1 つ作成されます。イメージは不変で、ジョブ間で共有され、`--rebuild` するかファイルを削除するまで存在し続けます。
- **コンテナ** — イメージのライブランタイムインスタンスです。ジョブ開始時にワーカーノード上に作成され、通常はジョブ終了時に破棄されます。ジョブが異常終了した場合、コンテナが残存することがあります。`physai clean --enroot` でこれらの残存コンテナを削除できます。

**CLI の動作:**

1. 指定フォルダから `container.yaml` を読み取ります。
2. 上位ディレクトリをたどって `project.yaml` を見つけ、設定をマージします（コンテナがプロジェクトを上書きします）。`base_image`（レジストリイメージ）または `base_container`（別のビルド済み squashfs）のいずれか一方のみを設定する必要があります。コンテナ側でいずれかを設定した場合、プロジェクトのベース選択を完全に置き換えます。
3. プリフライトチェック（rsync や投入前に早期に失敗します）:
   - **対象コンテナ**（`--rebuild` 未指定時）: `<name>` のアクティブなビルドジョブが既にキューにある場合は失敗します（重複した非 rebuild の投入は sbatch 内でも失敗します）。`/fsx/enroot/<name>.sqsh` が既に存在する場合も失敗します（おそらく `--rebuild` を指定すべきです）。
   - **ベースコンテナ**（`base_container` 設定時のみ）: ベースのアクティブなビルドジョブを検索します。存在する場合、そのジョブ ID を `afterok` 依存関係としてチェーンに取り込みます。存在しない場合、ベースの sqsh がディスク上に存在する必要があり、なければ `"Build it first."` メッセージでビルドが失敗します。
4. コンテナの `setup-hooks/`、`app/`、およびパッケージ化された `build-scripts/` をクラスター上の `/fsx/physai/builds/<name>-<ts>/` へ rsync します。
5. ビルドディレクトリに `env.txt`（マージ済みの `env`）と `build.sbatch` を書き込みます。
6. `sbatch [--dependency=afterok:<base_build_id> --kill-on-invalid-dep=yes] build.sbatch` を実行し、JOB_ID を取得します。
7. `-n` / `--no-stream` が指定されていなければ、`tail -f /fsx/physai/logs/<JOB_ID>.out` でターミナルにストリーミングします。Ctrl-C で再接続のヒントを表示して終了します。

##### 3.3.3.1 生成される build.sbatch の例

```bash
#!/bin/bash
#SBATCH --job-name=physai/build/leisaac-runtime
#SBATCH --comment="base=nvcr.io/nvidia/cuda:12.8.1-cudnn-devel-ubuntu24.04"
#SBATCH --partition=gpu
#SBATCH --gres=gpu:1
#SBATCH --output=/fsx/physai/logs/%j.out
set -eo pipefail
trap 'echo "\nBuild failed. Container may be left on the worker node."; echo "  Clean up: physai clean --enroot"' ERR
SECONDS=0
BUILD_DIR=/fsx/physai/builds/leisaac-runtime-20260422-080000
BUILD_NAME=leisaac-runtime-20260422-080000
SQSH=/fsx/enroot/leisaac-runtime.sqsh

if [ -f "$SQSH" ]; then
  echo "ERROR: $SQSH exists. Use --rebuild to replace."
  exit 1
fi

echo "=== init (${SECONDS}s) ==="
srun --container-image=nvcr.io/nvidia/cuda:12.8.1-cudnn-devel-ubuntu24.04 \
     --container-name=$BUILD_NAME --container-mounts=/fsx:/fsx \
     --container-remap-root \
     bash "$BUILD_DIR/build-scripts/init-env.root.sh"

echo "=== 10-system-packages.root.sh (${SECONDS}s) ==="
srun --container-name=$BUILD_NAME --container-mounts=/fsx:/fsx \
     --container-remap-root \
     bash "$BUILD_DIR/setup-hooks/10-system-packages.root.sh"

echo "=== 20-install-leisaac.sh (${SECONDS}s) ==="
srun --container-name=$BUILD_NAME --container-mounts=/fsx:/fsx \
     bash "$BUILD_DIR/setup-hooks/20-install-leisaac.sh"

# ... more hooks ...

echo "=== copy app/ (${SECONDS}s) ==="
srun --container-name=$BUILD_NAME --container-mounts=/fsx:/fsx \
     --container-remap-root \
     bash "$BUILD_DIR/build-scripts/mkdir-app.root.sh"
srun --container-name=$BUILD_NAME --container-mounts=/fsx:/fsx \
     bash "$BUILD_DIR/build-scripts/copy-app.sh"

echo "=== export squashfs (${SECONDS}s) ==="
enroot export -o "$SQSH" pyxis_${BUILD_NAME}
enroot remove -f pyxis_${BUILD_NAME}

echo "Build complete: $SQSH (${SECONDS}s)"
```

init ステップでは `env.txt` から環境変数を Pyxis の名前付きコンテナに読み込みます。`--rebuild` を指定した場合、事前チェックの代わりに `rm -f "$SQSH"` が実行されるため、古いイメージを使用中のジョブはリビルド完了まで動作し続けます（afterok で依存するジョブは待機し、それ以外は現在の sqsh を使用します）。中間的な Pyxis コンテナにはタイムスタンプ付きの `BUILD_NAME` を使用して、実行中のコンテナや同時ビルドとの衝突を回避します。最終的な squashfs には安定したコンテナ名を使用します。

#### 3.3.4. 実行ワークフロー（Train / Eval）

`physai run` は設定ファイルの `pipeline.stages` に記載されたステージを実行します。`--from` / `--to` でそのリストの連続する部分範囲に絞り込みます。`physai train` と `physai eval` は単一ステージ実行のショートカットです。

ステージの順序: `augment`、`convert`、`validate`、`train`、`eval`、`register`

| `--from` | 必須引数 | 解決先 |
|----------|----------|--------|
| `augment` | `--raw` | `/fsx/raw/<name>` |
| `convert` | `--raw` | `/fsx/raw/<name>` |
| `validate` | `--dataset` | `/fsx/datasets/<name>` |
| `train` | `--dataset` | `/fsx/datasets/<name>` |
| `eval` | `--checkpoint` | `/fsx/checkpoints/<name>` |
| `register` | （なし — 前のステージの出力を参照） | |

ステージ固有のパラメータ（例: `max_steps`、`rounds`）は設定ファイルの `stages.<name>` セクションから取得します。CLI の `--max-steps` は `stages.train.max_steps` を上書きし、`--eval-rounds` は `stages.eval.rounds` を上書きします。

設定ファイルのデフォルトステージを実行する場合（例: convert → validate → train → eval → register）

```bash
physai run --config examples/so101-gr00t/configs/so101_liftcube_gr00t-n1.6.yaml \
           --raw so101_liftcube
```

train 以降を実行する場合

```bash
physai run --config examples/so101-gr00t/configs/so101_liftcube_gr00t-n1.6.yaml \
           --from train --dataset so101_liftcube
```

単一ステージのショートカット

```bash
physai train --config examples/so101-gr00t/configs/so101_liftcube_gr00t-n1.6.yaml \
             --dataset so101_liftcube
physai eval --config examples/so101-gr00t/configs/so101_liftcube_gr00t-n1.6.yaml \
            --checkpoint gr00t-n1.6-liftcube-30k
```

##### 3.3.4.1 コマンドの処理手順

1. ローカルパスから `run_config.yaml` を読み取ります。
2. 実行するステージを決定します: 設定ファイルの `pipeline.stages` を起点に、`--from` / `--to` でそのステージリストの連続する部分範囲に絞り込みます。
3. 開始ステージに必要な CLI 引数が指定されているか検証します。
4. 設定ファイルとその `model.config_dir` を `/fsx/physai/sync/<run-id>/` へ rsync します。
5. 各ステージについて sbatch スクリプトを生成します:
   - `stages.<name>` からパーティション、gres、constraint、コンテナを設定します。
   - `srun --container-image=/fsx/enroot/<container>.sqsh`（レジストリ参照は使わず、ステージはビルド済みイメージのみを使用します）。
   - `--container-mounts=/fsx:/fsx,/tmp/.X11-unix:/tmp/.X11-unix`。
   - 解決済みパスを引数としてコンテナプロトコルのエントリポイントを呼び出します。
   - ステージパラメータ（max_steps、rounds）は設定から取得し、CLI で上書き可能です。
   - `RUN_CONFIG`（同期済み設定のパス）を設定します。eval では `DISPLAY=${DISPLAY:-:0}` を設定します。
6. 各ステージを `sbatch --dependency=afterok:<deps> --kill-on-invalid-dep=yes` で投入します。`<deps>` は前のステージのジョブ ID と、そのステージのコンテナのアクティブなビルドジョブ ID をコロン区切りで結合したものです。コンテナに sqsh もアクティブなビルドもない場合、投入前にエラーとなります。
7. `-n` / `--no-stream` が指定されていなければ、投入された各ジョブのログを順番にターミナルにストリーミングします（現在のジョブが終了したら次へ進みます）。Ctrl-C で現在のストリームから切断します（投入済みのすべてのジョブは継続します）。

`physai train` は `physai run --from train --to train` と同等です。`physai eval` は `physai run --from eval --to eval` と同等です。

## 4. エラー処理

| シナリオ | 動作 |
|----------|------|
| SSH 接続失敗 | ssh の stderr を表示し、区切り線の後に `ssh <host>` で手動テストするヒントを表示します（ホスト鍵の不一致、設定エントリの欠如、認証情報の期限切れなど）。 |
| sbatch の失敗 | Slurm のエラーメッセージを表示します。 |
| 対象の `.sqsh` が存在し `--rebuild` 未指定、または同じコンテナのアクティブなビルドジョブが既にキューにある場合 | 投入前にエラー: `"already exists. Use --rebuild to replace."` または `"Build job N is already active..."`。`--rebuild` ではこのチェックをスキップします（新しいジョブが開始時に sqsh を削除します）。 |
| `base_container` を参照しているが、ディスク上にもビルド中にも存在しない | エラー: `"Base container '<name>' not found at /fsx/enroot/<name>.sqsh. Build it first."` |
| パイプラインステージの `container` がディスク上にもビルド中にもない | 上記と同じパターンです。 |
| run_config が不明なステージまたは `container` の欠如を参照 | ジョブ投入前にエラーとなります。 |
| データセット / チェックポイントのパスがクラスター上に見つからない | エラー: `"<label> not found on cluster: <path>"`。 |
| ログストリーミング中の Ctrl-C | 再接続のヒント（`physai logs <job-id>`）を表示し、終了コード 0 で終了します。 |
| ジョブの失敗（非ゼロ終了） | `physai status` で確認可能です。`physai logs <job-id>` で完全なログを再表示します。 |

## 5. SSH インターフェース

クラスターとのすべての通信は `ssh.py` 内のマルチプレクス化された SSH `Session` を通じて行われます。

```python
class Session:
    def __init__(self, host: str)         # starts a ControlMaster (ControlPersist=10m)
    def run(self, cmd: str) -> str        # one-shot command, returns stdout
    def rsync(self, src: str, dst: str)   # rsync -az over the control socket
    def write_file(self, path, content)   # cat > <path> on remote
    def stream_log(self, job_id: str)     # streams /fsx/physai/logs/<id>.out via a Python helper
    def clone(self) -> "Session"          # reuses the same control socket
    def close(self)                       # tears down the control socket
    has_sacct: bool                       # cached capability probe
```

1 回の CLI 呼び出しにつき 1 つの `ControlMaster` を使用することで、以降の `ssh` / `rsync` の再認証レイテンシを排除します。`cli/physai/log_streamer.py` のヘルパースクリプトはリモートの `python3 -` にパイプされるため、クラスター側にソフトウェアをインストールせずにログのテーリングが機能します。

## 6. パッケージ構成

```
cli/
├── pyproject.toml
└── physai/
    ├── cli.py            # argparse dispatch
    ├── config.py         # load ~/.physai/config.yaml + --host override
    ├── ssh.py            # Session (ControlMaster), rsync, stream_log
    ├── log_streamer.py   # piped to remote `python3 -` to tail job logs
    ├── build.py          # read project/container yaml, generate build.sbatch
    ├── build-scripts/    # packaged snippets shipped to the cluster per build
    │   ├── init-env.root.sh
    │   ├── mkdir-app.root.sh
    │   └── copy-app.sh
    ├── clean.py          # remove old build dirs, logs, stale enroot containers
    ├── doctor.py          # cluster health checks with interactive fixes
    ├── pipeline.py       # read run_config, generate train/eval/run sbatch
    ├── data.py           # ls, upload
    └── jobs.py           # list, status, logs, cancel (squeue/sacct wrappers)
```

依存パッケージ: `pyyaml` のみです。その他の外部依存はありません。

インストール: `pip install -e cli/`

エントリポイント: `physai`（pyproject.toml の `[project.scripts]` で定義されています）
