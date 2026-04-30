# physaiを使用してパイプラインを開発する

**physai** は、ロボット開発者がインフラを管理することなく、収集したデモデータから評価済みポリシーまでを一貫して処理できる、AWS上のクラウドネイティブプラットフォームです。

## 1. 概要

physai プラットフォームは、ロボット学習ワークフロー全体を自動化します：

```
生の HDF5 デモデータ (S3 にアップロード → /fsx に出現)
  │
  ├─ 1. データ拡張 (オプション, GPU)
  │     シミュレーション摂動によりシードデモから追加デモを生成
  │
  ├─ 2. フォーマット変換 (CPU)
  │     HDF5 → LeRobot v2.1 (parquet + H.264 動画)
  │
  ├─ 3. バリデーション (CPU)
  │     事前チェック: 構造、次元、モデル互換性
  │
  ├─ 4. 学習 (GPU)
  │     検証済み LeRobot データセットで VLA モデルをファインチューン
  │
  ├─ 5. 評価 (GPU)
  │     シミュレーションでポリシーを実行し、成功率を測定
  │
  └─ 6. 登録 (CPU)
        メトリクス記録、チェックポイントのバージョニング、条件付き承認
```

3つのサブシステムが独立して拡張可能です：

| サブシステム | 変更可能な要素 |
|-----------|------------|
| データ拡張 + 評価 | ロボット、シミュレーション環境、タスク |
| 学習 | VLA モデル、モデル設定 |
| 変換 + バリデーション | ロボット、ソースデータフォーマット |

### プラットフォームが提供するもの vs. 開発者が実装するもの

| プラットフォームが提供 | 開発者が実装 |
|---|---|
| Slurm ジョブチェーン構築 (`--dependency=afterok`) | `train.sh` / `eval.sh` エントリポイントのロジック |
| コンテナのビルドとデプロイ (Pyxis/enroot) | `setup-hooks/` インストールスクリプト |
| データパス解決 (`--dataset <name>` → `/fsx/datasets/<name>`) | コンテナ内のデータフォーマット入出力処理 |
| `RUN_CONFIG` の生成とクラスタへの配置 | スクリプトからの `RUN_CONFIG` パラメータ読み取り |
| SSH オーケストレーション、ログストリーミング、ジョブ管理 | なし — CLI が処理します |
| MLflow 実験トラッキング | なし — 登録ステージが処理します |

---

## 2. クイックスタート: 自分のパイプラインを作る

自分のモデルを physai で動かすには、以下の3つを用意します：

```
your-project/
├── project.yaml                        # 1. コンテナ共通デフォルト
├── containers/
│   ├── your-trainer/                   # 2. コンテナ定義
│   │   ├── container.yaml
│   │   ├── app/
│   │   │   └── train.sh               #    エントリポイント (仕様を満たす必要あり)
│   │   └── setup-hooks/
│   │       ├── 10-system-packages.root.sh
│   │       └── 20-install-deps.sh
│   └── your-eval-runtime/
│       ├── container.yaml
│       ├── app/
│       │   └── eval.sh
│       └── setup-hooks/
│           └── ...
├── configs/
│   └── your_robot_task_model.yaml      # 3. パイプライン設定
└── model_configs/
    └── your-model/
        └── your-robot/                 # 4. モデル固有の設定ファイル
            └── ...
```

### ステップバイステップ

1. **コンテナを定義する** — トレーナーコンテナと（任意で）評価ランタイムコンテナを作成します。各コンテナは `setup-hooks/` で環境を構築し、`app/*.sh` エントリポイントが [§3](#3-コンテナ定義) の仕様を満たす必要があります。フックのパターンは [§3.3](#33-セットアップフック) を参照してください。

2. **パイプライン設定を書く** — `configs/` に YAML ファイルを作成し、実行するステージ、使用するコンテナ、ステージパラメータを宣言します。[§4](#4-パイプライン設定) を参照してください。

3. **モデル設定を用意する** — モデル固有の設定ファイル（例: モダリティ定義）を `model_configs/<model>/<robot>/` に配置します。[§5](#5-モデル設定ディレクトリ) を参照してください。

4. **クラスタ上でコンテナをビルドする**:

   ```bash
   physai build containers/your-trainer
   physai build containers/your-eval-runtime
   ```

5. **データをアップロードして実行する**:

   ```bash
   physai upload datasets ./my-lerobot-dataset
   physai run --config configs/your_robot_task_model.yaml --dataset my-lerobot-dataset
   ```

CLI の全リファレンスは [CLI リファレンス](../en/PHYSAI_CLI.md) を参照してください。

---

## 3. コンテナ定義

各コンテナは、ビルドレシピとエントリポイントスクリプトを含むフォルダです。コンテナはクラスタ上で Pyxis/enroot（Docker ではない）を使用してビルドされ、squashfs イメージとして `/fsx/enroot/` にエクスポートされます。

### 3.1 ディレクトリ構造

```
my-container/
├── container.yaml          # コンテナマニフェスト
├── app/                    # コンテナ内の /app/ にコピーされる
│   └── train.sh            # エントリポイントスクリプト
└── setup-hooks/
    ├── 10-system-packages.root.sh   # root として実行
    └── 20-install-deps.sh           # ユーザーとして実行
```

### 3.2 project.yaml と container.yaml

`project.yaml` は親ディレクトリに配置し、同じプロジェクト内の全コンテナの共通デフォルトを定義します。ビルダーはコンテナフォルダから上方向に探索し、最初に見つかった `project.yaml` を使用します。

```yaml
# project.yaml — 共通デフォルト
base_image: nvcr.io/nvidia/cuda:12.8.1-cudnn-devel-ubuntu24.04
env:
  PIP_CONSTRAINT: ""
  NVIDIA_VISIBLE_DEVICES: all
```

`container.yaml` は `project.yaml` を上書き・拡張します。スカラー値は上書きされ、`env` 辞書はマージされます。

```yaml
# container.yaml — コンテナ固有の設定
name: my-trainer
partition: gpu
gres: "gpu:1"
env:
  MY_CUSTOM_VAR: "value"    # project.yaml の env にマージ
```

`base_image` と `base_container` のいずれか一方のみを設定する必要があります（排他的）。既にビルド済みの別のコンテナの上に構築する場合：

```yaml
name: my-extended-runtime
base_container: my-base-runtime   # /fsx/enroot/my-base-runtime.sqsh が必要
partition: gpu
gres: "gpu:1"
```

`physai build` はサブミット前に親イメージがクラスタ上に存在するか確認します。ベースを先にビルドしてから、派生コンテナをビルドしてください。

### 3.3 セットアップフック

`setup-hooks/` 内のファイルは、数値プレフィックス順にビルド中に実行されます。2つのバリアントがあります：

- `NN-name.sh` — コンテナ内で非特権ユーザーとして実行されます。
- `NN-name.root.sh` — root として実行されます（`apt-get install` やシステムファイル操作に典型的です）。

`project.yaml` + `container.yaml` からマージされた `env` がコンテナにエクスポートされるため、フックとエントリポイントは同じ変数を参照できます。

**典型的なフックパターン:**

| 順序 | 目的 | 例 |
|------|------|---|
| `10-*.root.sh` | システムパッケージ (`apt-get install build-essential git cmake ...`) | OS レベルの依存関係をインストールします |
| `20-*.sh` | リポジトリのクローン、Python 依存のインストール (`pip install`, `uv sync`) | モデルフレームワークをインストールします |
| `30-40-*.sh` | アセット、事前学習済み重みのダウンロード | モデル重み、ロボット URDF をダウンロードします |
| `50-*.sh` | キャッシュのウォームアップ（例: シェーダーコンパイル） | ランタイム時の初回起動遅延を回避します |
| `90-*.sh` | クリーンアップ (`pip cache purge`, `.git` ディレクトリ削除) | squashfs イメージサイズを削減します |

`app/` の内容は、全フックが成功した後にイメージ内の `/app/` にコピーされます。

### 3.4 エントリポイント仕様

各パイプラインステージは固定のエントリポイントスクリプトを呼び出します。`setup-hooks/` が環境を構築し、`app/*.sh` スクリプトがパイプラインから実行時に呼び出されます。1つのコンテナが複数のエントリポイントを実装できます（例: `eval.sh` と `augment.sh` の両方を持つシミュレーションランタイムコンテナ）。

**全エントリポイントに渡される共通環境変数:**

- `RUN_CONFIG` — クラスタ上の解決済みランコンフィグ YAML へのパスです。必要な情報（例: `sim.environment`, `model.name`）を読み取ります。
- `DISPLAY` — 評価ジョブでは `:0` に設定されます（GLX/GLFW を使用するヘッドレスシミュレーションでも必要です）。
- `project.yaml` と `container.yaml` からマージされた `env` です。

#### train.sh

| 項目 | 説明 |
|------|------|
| **引数** | `<dataset_dir> <model_config_dir> <output_dir> <max_steps>` |
| `<dataset_dir>` | `/fsx/datasets/` 上のデータセットディレクトリです。フォーマットはコンテナが定義します — パイプラインは検査しません。 |
| `<model_config_dir>` | 解決済みモデル設定ディレクトリです（[§5](#5-モデル設定ディレクトリ) 参照）。 |
| `<output_dir>` | 空の実行ごとのディレクトリです。チェックポイントファイルを**直接ここに**書き込みます（サブディレクトリに入れないでください）。`eval.sh` はこのパスをそのまま読み取ります。 |
| `<max_steps>` | 学習ステップ数です。`stages.train.max_steps` または `--max-steps` から取得されます。 |
| **終了コード** | 学習失敗時に非ゼロとなります。後続ステージはキャンセルされます。 |

最小テンプレート：

```bash
#!/bin/bash
set -euo pipefail
DATASET_DIR=$1
MODEL_CONFIG_DIR=$2
OUTPUT_DIR=$3
MAX_STEPS=$4

# ここに学習コマンドを記述。
# チェックポイントファイルは $OUTPUT_DIR 直下に書き込む（サブディレクトリではない）。
your_train_command \
  --data "$DATASET_DIR" \
  --config "$MODEL_CONFIG_DIR" \
  --output "$OUTPUT_DIR" \
  --steps "$MAX_STEPS"
```

#### eval.sh

| 項目 | 説明 |
|------|------|
| **引数** | `<checkpoint_dir> <model_config_dir> <output_dir> <rounds> [--visual]` |
| `<checkpoint_dir>` | `/fsx/checkpoints/` 上のチェックポイントディレクトリです。 |
| `<model_config_dir>` | 解決済みモデル設定ディレクトリです。 |
| `<output_dir>` | 空の実行ごとのディレクトリです。`metrics.json`（必須）とオプションで `eval.log` を書き込みます。 |
| `<rounds>` | 評価ラウンド数です。`stages.eval.rounds` または `--eval-rounds` から取得されます。 |
| `--visual` | 指定された場合、接続されたバーチャルディスプレイ (DCV) にレンダリングします。指定なしの場合はヘッドレスモードです。 |
| **終了コード** | 評価失敗時に非ゼロとなります。 |

**必須出力 — `metrics.json`:**

```json
{
  "eval_rounds": 20,
  "success_rate": 0.2,
  "checkpoint": "<checkpoint_dir 引数>"
}
```

コンテナは追加フィールド（例: `task`, `language_instruction`）を付加できます。

最小テンプレート：

```bash
#!/bin/bash
set -euo pipefail
CHECKPOINT_DIR=$1
MODEL_CONFIG_DIR=$2
OUTPUT_DIR=$3
ROUNDS=$4
VISUAL_FLAG="${5:-}"

# 必要に応じて RUN_CONFIG からシミュレーション設定を読み取り：
#   ENVIRONMENT=$(python3 -c "import yaml; print(yaml.safe_load(open('$RUN_CONFIG'))['sim']['environment'])")

if [ "$VISUAL_FLAG" = "--visual" ]; then
  HEADLESS_ARG=""
else
  HEADLESS_ARG="--headless"
fi

your_eval_command \
  --checkpoint "$CHECKPOINT_DIR" \
  --config "$MODEL_CONFIG_DIR" \
  --rounds "$ROUNDS" \
  $HEADLESS_ARG \
  2>&1 | tee "$OUTPUT_DIR/eval.log"

# metrics.json を書き込む (eval_rounds, success_rate, checkpoint は必須)
python3 -c "
import json
metrics = {
    'eval_rounds': $ROUNDS,
    'success_rate': <出力からパース>,
    'checkpoint': '$CHECKPOINT_DIR'
}
json.dump(metrics, open('$OUTPUT_DIR/metrics.json', 'w'))
"
```

#### convert.sh (計画中 — 未実装)

| 項目 | 説明 |
|------|------|
| **引数** | `<input_hdf5> <output_dir>` |
| **仕様** | ソースフォーマットをトレーナーが利用できるデータセットに変換します。 |

#### validate.sh (計画中 — 未実装)

| 項目 | 説明 |
|------|------|
| **引数** | `<dataset_dir> <model_config_dir>` |
| **仕様** | データセットをモデル要件に対して検証します。失敗時に非ゼロで exit します。 |

#### augment.sh (オプション、計画中 — 未実装)

| 項目 | 説明 |
|------|------|
| **引数** | `<input_hdf5> <output_dir> <num_trials>` |
| **仕様** | 同じフォーマットでより多くのエピソードを含む拡張 HDF5 を生成します。 |

### 3.5 ビルドプロセス

1. `project.yaml` + `container.yaml` をローカルでマージします。
2. `setup-hooks/`、`app/`、ビルドヘルパーをクラスタの `/fsx/physai/builds/<name>-<ts>/` に rsync します。
3. 設定された `partition` と `gres` で Slurm ジョブをサブミットします：
   a. ベースからコンテナを作成します (`--container-image=<base_image>` または `--container-image=/fsx/enroot/<base_container>.sqsh`)。
   b. 各セットアップフックを順番に実行します（root フックは `--container-remap-root` を追加します）。
   c. `app/` の内容をコンテナ内の `/app/` にコピーします。
   d. squashfs にエクスポートします: `/fsx/enroot/<name>.sqsh`。

CLI 側の詳細（プリフライトチェック、インフライトビルド依存関係、`--rebuild` セマンティクス）は PHYSAI-DESIGN.md §7 を参照してください。

---

## 4. パイプライン設定

パイプラインバリアント（ロボット + タスク + モデル）ごとに1つの設定ファイルを `configs/` に保存します。この YAML は、実行するステージ、使用するコンテナとリソース、渡すパラメータを記述します。

### 4.1 設定ファイルフォーマット

```yaml
pipeline:
  stages: [convert, validate, train, eval, register]
  # データ拡張あり: [augment, convert, validate, train, eval, register]

sim:
  platform: <sim_platform>           # 例: leisaac, robosuite
  # 残りのフィールドはプラットフォーム固有 — パイプラインは解釈しない。
  # RUN_CONFIG 経由でコンテナに渡される。
  # 例 (LeIsaac):
  #   environment: LeIsaac-SO101-PickOrange-v0
  #   mimic_environment: LeIsaac-SO101-PickOrange-Mimic-v0
  #   language_instruction: "Pick up the orange and place it on the plate"

model:
  name: <model_name>
  config_dir: <relative-name>         # 例: gr00t-n1.6/so101-singlecam

stages:
  augment:
    partition: gpu
    gres: "gpu:1"
    container: <sim_runtime_container>
  convert:
    partition: cpu
    container: <converter_container>
  validate:
    partition: cpu
    container: <converter_container>
  train:
    partition: gpu
    gres: "gpu:1"
    constraint: <feature>             # オプションの GPU タイプ制約, 例: l40s
    container: <trainer_container>
    max_steps: 10000
  eval:
    partition: gpu
    gres: "gpu:1"
    container: <sim_runtime_container>
    rounds: 20
  register:
    partition: cpu
    container: <register_container>
```

**主要フィールド:**

- `pipeline.stages` — デフォルトで実行するステージです。CLI の `--from`/`--to` フラグで連続する部分範囲に絞り込めます。
- `sim.*` — プラットフォーム固有のシミュレーションパラメータです。パイプラインは `RUN_CONFIG` 経由でコンテナに透過的に渡します。`eval.sh` / `augment.sh` が必要なものを読み取ります。
- `model.config_dir` — 設定済み検索パスで解決される相対名です（[§5](#5-モデル設定ディレクトリ) 参照）。
- `stages.<name>.container` — コンテナ名です（`container.yaml` の `name` と一致する必要があります）。
- `stages.<name>.partition` / `gres` / `constraint` — Slurm リソース割り当てです。
- `stages.train.max_steps` / `stages.eval.rounds` — エントリポイントに渡されるステージ固有パラメータです。

### 4.2 GPU フィーチャー制約

クラスタはインスタンスタイプに基づいて GPU ノードに Slurm フィーチャーを自動的にタグ付けします。`constraint` 値としてこれらを使用します：

| インスタンスファミリー | フィーチャー |
|-----------------|---------|
| `ml.g6e.*` | `l40s` |
| `ml.g6.*` | `l4` |
| `ml.g5.*` | `a10g` |
| `ml.p3.*` | `v100` |
| `ml.p4d.*`, `ml.p4de.*` | `a100` |
| `ml.p5.*` | `h100` |

Slurm はブール式をサポートします: `l40s`（完全一致）、`l40s|h100`（OR）、`!a10g`（NOT）。[Slurm `--constraint` ドキュメント](https://slurm.schedmd.com/sbatch.html#OPT_constraint) を参照してください。

### 4.3 開始ステージごとの必須引数

現在 `train` と `eval` のみ実装済みです。その他のステージは将来の参考として記載しています。

| `--from` | 必須 CLI 引数 | 解決先 | 実装済み? |
|----------|--------------|--------|----------|
| `augment` | `--raw <name>` | `/fsx/raw/<name>` | いいえ (計画中) |
| `convert` | `--raw <name>` | `/fsx/raw/<name>` | いいえ (計画中) |
| `validate` | `--dataset <name>` | `/fsx/datasets/<name>` | いいえ (計画中) |
| `train` | `--dataset <name>` | `/fsx/datasets/<name>` | はい |
| `eval` | `--checkpoint <name>` | `/fsx/checkpoints/<name>` | はい |
| `register` | (なし) | | いいえ (計画中) |

### 4.4 ステージパラメータの CLI オーバーライド

```bash
--max-steps 50000        # stages.train.max_steps を上書き
--eval-rounds 50         # stages.eval.rounds を上書き
--visual                 # 評価をバーチャルディスプレイ (DCV) にレンダリング
```

---

## 5. モデル設定ディレクトリ

モデルごと、ロボットごとの設定ファイルをローカルに保存します。パイプライン設定の `model.config_dir` は相対名（例: `gr00t-n1.6/so101-singlecam`）です。CLI は設定済み検索パスに対して解決し、マッチしたディレクトリをクラスタに rsync します。パイプラインは解決済みディレクトリをコンテナに渡しますが、内容は解釈しません。

```
model_configs/
└── <model>/
    └── <robot>/
        └── <モデル固有の設定ファイル>
```

検索パスは `~/.physai/config.yaml` で設定します：

```yaml
model_config_roots:
  - ~/projects/physai/examples/so101-gr00t/model_configs
  - ~/projects/my-custom-model/model_configs
```

またはコマンドごとに `--model-config-root <path>` で指定できます（繰り返し可能）。

モデル設定ディレクトリの内容は完全にモデルに依存します。パイプラインは内容を検査せず、パスを `train.sh` と `eval.sh` に渡すだけです。

---

## 6. 実装例: SO-101 + GR00T N1.6

このセクションでは、同梱の例を使って全パーツがどのように組み合わさるかを説明します。

### 6.1 プロジェクトレイアウト

```
examples/so101-gr00t/
├── project.yaml
├── configs/
│   ├── so101_pickorange_gr00t-n1.6.yaml
│   └── so101_liftcube_gr00t-n1.6.yaml
├── model_configs/
│   └── gr00t-n1.6/
│       ├── so101-singlecam/
│       │   ├── modality.json
│       │   └── modality_config.py
│       └── so101-dualcam/
│           ├── modality.json
│           └── modality_config.py
└── containers/
    ├── leisaac-runtime/              # ベース: IsaacSim + LeIsaac (GR00T なし)
    │   ├── container.yaml
    │   ├── app/                      # 空 — 純粋なベースイメージ
    │   └── setup-hooks/
    │       ├── 10-system-packages.root.sh
    │       ├── 20-install-leisaac.sh
    │       ├── 40-download-assets.sh
    │       ├── 50-warmup.sh
    │       └── 90-cleanup.sh
    ├── leisaac-gr00t-n1.6/           # 評価ランタイム: leisaac-runtime + GR00T
    │   ├── container.yaml            # base_container: leisaac-runtime
    │   ├── app/
    │   │   └── eval.sh
    │   └── setup-hooks/
    │       ├── 10-install-gr00t.sh
    │       └── 90-cleanup.sh
    └── gr00t-n1.6-trainer/           # 学習: CUDA ベース + GR00T
        ├── container.yaml
        ├── app/
        │   └── train.sh
        └── setup-hooks/
            ├── 10-system-packages.root.sh
            ├── 20-install-gr00t.sh
            └── 90-cleanup.sh
```

### 6.2 project.yaml

```yaml
base_image: nvcr.io/nvidia/cuda:12.8.1-cudnn-devel-ubuntu24.04
env:
  OMNI_KIT_ACCEPT_EULA: "YES"
  PIP_CONSTRAINT: ""
  NVIDIA_VISIBLE_DEVICES: all
  NVIDIA_DRIVER_CAPABILITIES: all
  LEISAAC_DIR: /workspace/leisaac
  GR00T_DIR: /workspace/gr00t
  GR00T_REF: "n1.6-release"
  LEISAAC_REF: "d2cbfd2e33517f2094e1904ff817aa17de6e8939"
```

### 6.3 パイプライン設定

```yaml
# configs/so101_pickorange_gr00t-n1.6.yaml
pipeline:
  stages: [train, eval]    # 実装済みステージのみ記載

sim:
  platform: leisaac
  environment: LeIsaac-SO101-PickOrange-v0
  mimic_environment: LeIsaac-SO101-PickOrange-Mimic-v0
  language_instruction: "Pick up the orange and place it on the plate"

model:
  name: gr00t-n1.6
  config_dir: gr00t-n1.6/so101-dualcam

stages:
  train:
    partition: gpu
    gres: "gpu:1"
    constraint: l40s
    container: gr00t-n1.6-trainer
    max_steps: 10000
  eval:
    partition: gpu
    gres: "gpu:1"
    container: leisaac-gr00t-n1.6
    rounds: 20
```

もう1つの設定 `so101_liftcube_gr00t-n1.6.yaml` は、タスク (`LeIsaac-SO101-LiftCube-v0`, シングルカメラ) のみ異なり、`config_dir: gr00t-n1.6/so101-singlecam` を使用します。

### 6.4 モデル設定

`modality.json` はロボット構成の状態/アクション次元とカメラマッピングを定義します。例えば、`so101-singlecam` は `front` カメラ1台のみをマップし、`so101-dualcam` は `front` と `wrist` の両方をマップします。

### 6.5 セットアップフック

| コンテナ | フック | 内容 |
|---------|--------|------|
| `leisaac-runtime` | `10-system-packages.root.sh` | build-essential, git, cmake, ffmpeg, EGL/GLX ライブラリ, `uv` (Python パッケージマネージャ) をインストールします |
| | `20-install-leisaac.sh` | LeIsaac をクローンし、Python 3.11 venv, PyTorch 2.7, IsaacSim 5.1.0 (pip), IsaacLab, ZMQ 依存をインストールします |
| | `40-download-assets.sh` | SO-101 ロボット USD とシーンアセットを GitHub Releases からダウンロードします |
| | `50-warmup.sh` | IsaacSim シェーダーキャッシュをウォームアップします（ランタイム時の 5-10 分の初回起動遅延を回避します） |
| | `90-cleanup.sh` | pip/uv キャッシュ、.git ディレクトリを削除して squashfs サイズを削減します |
| `leisaac-gr00t-n1.6` | `10-install-gr00t.sh` | Isaac-GR00T を `n1.6-release` でクローンし、`uv sync` + flash-attn でインストールします |
| | `90-cleanup.sh` | キャッシュを削除します |
| `gr00t-n1.6-trainer` | `10-system-packages.root.sh` | build-essential, git, cmake, ffmpeg, libaio, `uv` をインストールします |
| | `20-install-gr00t.sh` | 同じ GR00T インストール (クローン + uv sync + flash-attn) を行います |
| | `90-cleanup.sh` | キャッシュを削除します |

### 6.6 train.sh 実装の詳細

`gr00t-n1.6-trainer` コンテナの `train.sh`:

1. モデル設定ディレクトリから `modality.json` をデータセットの `meta/` ディレクトリにコピーします（GR00T のデータローダーが要求します）
2. 単一 GPU でも PyTorch 分散環境変数 (`MASTER_ADDR`, `WORLD_SIZE` 等) を設定します
3. `launch_finetune.py` を `--base-model-path nvidia/GR00T-N1.6-3B` で実行します
4. 学習後、GR00T は `<output_dir>/.work/checkpoint-<step>/` に書き込みます。スクリプトはチェックポイントファイルを `<output_dir>` 直下に移動（仕様に従い）し、ワークディレクトリをクリーンアップします。

主要な学習パラメータ（この例ではハードコード）:

- `--global-batch-size 12`
- `--save-steps` = `max_steps` (最終チェックポイント1つのみ)
- `--save-total-limit 1`
- `--dataloader-num-workers 4`
- `--embodiment-tag NEW_EMBODIMENT`

### 6.7 eval.sh 実装の詳細

`leisaac-gr00t-n1.6` コンテナの `eval.sh` は2プロセスアーキテクチャを実装します：

1. **GR00T ポリシーサーバー** — `run_gr00t_server.py` をランダムポートでバックグラウンド起動します。サーバーはチェックポイントをロードし、gRPC 推論エンドポイントを公開します。スクリプトは `ping()` に応答するまでサーバーをポーリングします（最大 120 秒）。

2. **LeIsaac シミュレーションクライアント** — `policy_inference.py` を実行し、ポリシーサーバーに接続して IsaacSim 環境で `<rounds>` エピソード分ポリシーをロールアウトします。

主要な実装の詳細:

- `$RUN_CONFIG` から `sim.environment` と `sim.language_instruction` を読み取ります
- ウォッチドッグプロセスが評価ログの致命的パターン (`CUDA error`, `Segmentation fault` 等) を監視し、IsaacSim が復旧不能になった場合に評価をキルします
- ヘッドレスモードでも `DISPLAY` の設定が必要です（IsaacSim が GLFW/GLX を要求します）
- `--headless` フラグで IsaacSim がウィンドウにレンダリングするかオフスクリーンで実行するかを制御します
- 標準出力から成功率をパースし `metrics.json` を書き込みます

---

## 7. プラットフォームアーキテクチャ

### 7.1 システム概要

```
┌──────────────────────────────────────────────────────────────────┐
│  開発者マシン                                                      │
│    └── physai CLI (SSH 経由でオーケストレーション)                    │
└──────────────────────────────────────────────────────────────────┘
         │ SSH
         ▼
┌──────────────────────────────────────────────────────────────────┐
│                    SageMaker HyperPod クラスタ                     │
│                                                                  │
│  ログインノード (ml.c5.large)                                      │
│    ├── 開発者の SSH エントリポイント                                  │
│    └── MLflow クライアント (実験ロギング)                             │
│                                                                  │
│  コントローラーノード (ml.c5.large)                                  │
│    └── Slurm スケジューラ                                          │
│                                                                  │
│  ワーカーパーティション: "gpu" (固定数, cdk.json で設定)               │
│    → データ拡張、学習、評価                                          │
│                                                                  │
│  ワーカーパーティション: "cpu" (固定数, cdk.json で設定)               │
│    → フォーマット変換、バリデーション、登録                              │
│                                                                  │
│  全ノード共通マウント: /fsx (FSx for Lustre)                         │
└──────────────────────────────────────────────────────────────────┘
         │
         ▼
┌──────────────────────┐   ┌──────────────────────┐
│  S3 (永続)            │   │  SageMaker MLflow    │
│  ├── raw/            │   │  (トラッキングサーバー)  │
│  ├── datasets/       │   └──────────────────────┘
│  ├── checkpoints/    │
│  └── results/        │
│                      │
│  FSx (作業用)         │
│  /fsx/               │
│  ├── raw/  ←DRA──S3  │
│  ├── datasets/       │
│  ├── checkpoints/    │
│  ├── evaluations/    │
│  ├── enroot/         │
│  └── physai/         │
└──────────────────────┘
```

CLI はセッション開始時に単一の SSH ControlMaster 接続を確立し、後続の全コマンドをその上で多重化します。

### 7.2 Slurm ジョブチェーン

`physai run` はステージごとに1つの Slurm ジョブをサブミットし、`--dependency=afterok` でリンクします：

```bash
RUN_ID=run-20260415-155400
JOB1=$(sbatch --parsable --job-name=physai/run/$RUN_ID/train    train.sh)
JOB2=$(sbatch --parsable --job-name=physai/run/$RUN_ID/eval     --dependency=afterok:$JOB1 eval.sh)
JOB3=$(sbatch --parsable --job-name=physai/run/$RUN_ID/register --dependency=afterok:$JOB2 register.sh)
```

全ジョブはラン ID を共有します。いずれかのステップが失敗すると、後続ジョブはキャンセルされます。`physai cancel` は同じラン ID の全ジョブをキャンセルします。

コンテナイメージが現在ビルド中（`physai build` が進行中）の場合、パイプラインは自動的にビルドジョブを依存関係として追加します。`physai build` の直後に `physai run` を実行できます。

### 7.3 データ拡張の詳細

データ拡張が有効な場合、オーケストレーターは拡張と変換を同じ GPU ノード上で単一の Slurm ジョブとして実行します。拡張された HDF5 はローカル NVMe（`/fsx` ではない）に書き込まれ、変換はローカル NVMe から読み取って `/fsx` に書き込みます。600GB 以上になり得る拡張 HDF5 は共有ストレージに触れることなく、ジョブ終了時に自動クリーンアップされます。

### 7.4 DCV によるビジュアル評価

`physai eval --visual` は、レンダリングされたシミュレーションビューポートを NICE DCV 経由で開発者のブラウザにストリーミングします：

```bash
$ physai eval --visual --config so101_pickorange_gr00t-n1.6.yaml \
    --checkpoint checkpoints/run-42/checkpoint-10000

Submitted job 456
Allocating GPU node...          gpu-worker-3 (i-0abc123def)
Starting DCV session...         physai-eval-456

Connect to the DCV session:
  aws ssm start-session --target i-0abc123def \
    --document-name AWS-StartPortForwardingSession \
    --parameters '{"portNumber":["8443"],"localPortNumber":["8443"]}'

Then open: https://localhost:8443
Username: ubuntu          Password: xxxxxxx

Streaming eval log (Ctrl-C to detach)...
```

パイプラインは `--gres=gpu:1,dcv:1` で Slurm ジョブをサブミットし、DCV セッションを作成し、SSM ポートフォワーディングコマンドを表示して、`eval.sh` を `--visual` 付きで実行します。DCV サーバーは HyperPod ライフサイクルスクリプトで GPU ワーカーにインストールされます。SSM ポートフォワーディングにセキュリティグループの変更は不要です。

## 8. データセットフォーマットリファレンス

パイプラインは LeRobot v2.1 を標準データセットフォーマットとして使用します：

```
dataset/
├── data/chunk-000/
│   └── episode_000000.parquet    # action, observation.state, + 5 インデックスカラム
├── videos/chunk-000/
│   └── observation.images.<cam>/
│       └── episode_000000.mp4    # H.264, yuv420p
└── meta/
    ├── info.json                 # フィーチャースキーマ
    ├── episodes.jsonl            # エピソードごとのメタデータ
    ├── tasks.jsonl               # タスク説明
    └── episodes_stats.jsonl      # エピソードごとの正規化統計
```

必須 parquet カラム：

| カラム | 型 | 説明 |
|--------|------|------|
| `index` | int64 | グローバルにユニーク、データセット全体で連番 |
| `episode_index` | int64 | エピソード識別子 |
| `frame_index` | int64 | エピソード内のフレーム (0 にリセット) |
| `timestamp` | float32 | `frame_index / fps` |
| `task_index` | int64 | tasks.jsonl 内のタスクを参照 |

---

## 9. リファレンス

- [AWS Sample: Embodied AI Platform](https://github.com/aws-samples/sample-embodied-ai-platform)
- [AWS Sample: Physical AI Scaffolding Kit](https://github.com/aws-samples/sample-physical-ai-scaffolding-kit)
- [SageMaker HyperPod ドキュメント](https://docs.aws.amazon.com/sagemaker/latest/dg/sagemaker-hyperpod.html)
- [LeRobot Dataset Format](https://huggingface.co/docs/lerobot/lerobot-dataset-v3)
- [GR00T N1.6 Fine-tuning Guide](https://github.com/NVIDIA/Isaac-GR00T/blob/main/getting_started/finetune_new_embodiment.md)
- [GR00T Data Preparation](https://github.com/NVIDIA/Isaac-GR00T/blob/main/getting_started/data_preparation.md)
- [OpenPI (π0)](https://github.com/Physical-Intelligence/openpi)
- [LeIsaac](https://github.com/LightwheelAI/leisaac)
- [Isaac Lab Mimic](https://isaac-sim.github.io/IsaacLab/latest/source/extensions/omni.isaac.lab_tasks/omni.isaac.lab_tasks.utils.imitation_learning.html)
