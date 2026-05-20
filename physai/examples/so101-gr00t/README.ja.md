# SO-101 + GR00T サンプル

NVIDIA **GR00T** VLA モデルを **LeIsaac** (Isaac Sim) 上の **SO-101** フォロワーアームでファインチューニング・評価するための、そのまま実行できるパイプラインです。4つのバリアントが用意されています: 2つのタスク (`LiftCube`, `PickOrange`) × 2つのモデルバージョン (`gr00t-n1.5`, `gr00t-n1.6`)。

プラットフォーム自体（クラスターデプロイ、CLI インストール、ジョブ管理）については [README.ja.md](../../README.ja.md) と [docs/ja/PHYSAI_CLI.ja.md](../../docs/ja/PHYSAI_CLI.ja.md) を参照してください。本ドキュメントでは、このサンプルの内容と実行方法のみを扱います。

## 目次

| パス | 用途 |
|------|------|
| `project.yaml` | コンテナ共有デフォルト（ベースイメージ、環境変数、固定リファレンス） |
| `containers/` | パイプラインが使用する6つのコンテナのビルドレシピ |
| `configs/` | `run_config.yaml` ファイル — (タスク, モデルバージョン) の組み合わせごとに1つ |
| `model_configs/` | モデル・カメラ構成ごとの GR00T モダリティ設定 |

## パイプライン概要

4つの設定すべてが同じ3ステージパイプラインを実行します:

```
/fsx/raw/<name>/   ──►  convert  ──►  /fsx/datasets/<name>/
                        (CPU)
                                      /fsx/datasets/<name>/
                                              │
                                              ▼
                                      ──►  train  ──►  /fsx/checkpoints/<run-id>/
                                           (L40S GPU)
                                                      │
                                                      ▼
                                              ──►  eval  ──►  /fsx/evaluations/<run-id>/
                                                   (L40S GPU)        (metrics.json)
```

### 設定ファイル

| 設定ファイル | タスク | カメラ | モデル | 指示文 |
|-------------|--------|--------|--------|--------|
| `configs/so101_liftcube_gr00t-n1.5.yaml`   | LiftCube   | front      | GR00T N1.5 | "Lift the red cube up" |
| `configs/so101_liftcube_gr00t-n1.6.yaml`   | LiftCube   | front      | GR00T N1.6 | "Lift the red cube up" |
| `configs/so101_pickorange_gr00t-n1.5.yaml` | PickOrange | front+wrist | GR00T N1.5 | "Pick up the orange and place it on the plate" |
| `configs/so101_pickorange_gr00t-n1.6.yaml` | PickOrange | front+wrist | GR00T N1.6 | "Pick up the orange and place it on the plate" |

LiftCube はフロントカメラ1台のみを使用し、PickOrange はフロントカメラと手首カメラの両方を使用します。そのため LiftCube の設定は `so101-singlecam` モデル設定と、PickOrange の設定は `so101-dualcam` モデル設定とペアになっています。

## コンテナ

`containers/` 配下に6つのコンテナレシピがあります。3つは `project.yaml` の共有 `base_image: nvcr.io/nvidia/cuda:12.8.1-cudnn-devel-ubuntu24.04` を継承します (`leisaac-runtime`, `gr00t-n1.5-trainer`, `gr00t-n1.6-trainer`)。2つの評価ランタイムは `base_container:` を使って `leisaac-runtime` の上にレイヤーを追加します。`so101-converter` は独自の CPU 専用 `base_image` でオーバーライドします。

```
leisaac-runtime              (CUDA base; base simulation runtime — no entrypoint)
├── leisaac-gr00t-n1.5       (base_container: leisaac-runtime) + Isaac-GR00T N1.5 → eval.sh
└── leisaac-gr00t-n1.6       (base_container: leisaac-runtime) + Isaac-GR00T N1.6 → eval.sh
gr00t-n1.5-trainer           (CUDA base) standalone → train.sh
gr00t-n1.6-trainer           (CUDA base) standalone → train.sh
so101-converter              (python:3.12-slim-bookworm, CPU-only) → convert.sh
```

| コンテナ | パーティション | GRES | ステージ | 備考 |
|-----------|---------------|------|----------|------|
| `leisaac-runtime`       | gpu | gpu:1 | — (ベース) | IsaacSim 5.1.0 + LeIsaac（固定コミット）+ シーンアセット + シェーダウォームアップ。直接実行されることはありません。 |
| `leisaac-gr00t-n1.5`    | gpu | gpu:1 | eval     | `leisaac-runtime` を拡張。GR00T N1.5 ポリシーサーバ (`inference_service.py`) + LeIsaac シムクライアントを起動します。 |
| `leisaac-gr00t-n1.6`    | gpu | gpu:1 | eval     | `leisaac-runtime` を拡張。GR00T N1.6 ポリシーサーバ (`run_gr00t_server.py`) を `action_horizon=16` で起動します。 |
| `gr00t-n1.5-trainer`    | gpu | gpu:1 | train    | `nvidia/GR00T-N1.5-3B` を `gr00t_finetune.py` でファインチューニング (batch=32)。 |
| `gr00t-n1.6-trainer`    | gpu | gpu:1 | train    | `nvidia/GR00T-N1.6-3B` を `launch_finetune.py` でファインチューニング (global_batch=12)。 |
| `so101-converter`       | cpu | —     | convert  | HDF5 → LeRobot v2.1。純粋な `h5py`/`numpy`/`lerobot==0.3.3`; Isaac Lab 不要。 |

コンテナのビルドにはプラットフォームの `physai build` コマンドを使用します — 例:

```bash
# ベースを最初にビルド (leisaac-gr00t-* イメージは base_container 経由でレイヤーを追加):
physai build containers/leisaac-runtime

# 使用する GR00T バージョンの評価ランタイム:
physai build containers/leisaac-gr00t-n1.6

# そのバージョンのトレーナー:
physai build containers/gr00t-n1.6-trainer

# コンバータ (HDF5 → LeRobot):
physai build containers/so101-converter
```

トレーニングと評価には対応するバージョンのペアが必要です: `gr00t-n1.5-trainer` と `leisaac-gr00t-n1.5`、または `gr00t-n1.6-trainer` と `leisaac-gr00t-n1.6` を組み合わせて使用します。実行設定ではすでに正しく紐付けられています。

## モデル設定

`model_configs/` には、GR00T がどのジョイント、カメラ、アクション表現を使用するかを指定する、モデル・カメラ構成ごとの設定が格納されています。コンバータでは使用されません。トレーナーおよび評価コンテナで使用されます。

```
model_configs/
├── gr00t-n1.5/
│   ├── so101-singlecam/    modality.json + data_config.py         (LiftCube)
│   └── so101-dualcam/      modality.json + data_config.py         (PickOrange)
└── gr00t-n1.6/
    ├── so101-singlecam/    modality.json + modality_config.py     (LiftCube)
    └── so101-dualcam/      modality.json + modality_config.py     (PickOrange)
```

`modality.json` は両バージョンで使用され、SO-101 エンボディメントを固定します:

- `state` と `action` は 6-DoF ベクトルです。最初の5要素 (インデックス 0–4) はアームジョイントで、`single_arm` グループとして公開されます。6番目の要素 (インデックス 5) はグリッパーで、`gripper` グループとして公開されます。LeRobot の範囲は半開区間のため、ファイルには `single_arm: {start:0, end:5}` と `gripper: {start:5, end:6}` と記述します。
- `video.front` (singlecam) または `video.front` + `video.wrist` (dualcam)。
- `annotation.human.task_description` は `task_index` (LeRobot のタスクテーブル) から取得されます。

Python ファイルは GR00T バージョンによって異なります:

- **N1.5** は `data_config.py` を使用し、`gr00t.experiment.data_config.So100DataConfig` を拡張して `video_keys` をオーバーライドします (singlecam: `["video.front"]`; dualcam は `"video.wrist"` を追加)。
- **N1.6** は `modality_config.py` を使用し、完全な `ModalityConfig` を構築して `EmbodimentTag.NEW_EMBODIMENT` に登録します。singlecam と dualcam の両バリアントで、`single_arm` + `gripper` ジョイントグループに対して全 ABSOLUTE アクション表現を宣言します。

これらは各実行設定の `model.config_dir` (例: `gr00t-n1.5/so101-dualcam`) から参照されます。`physai` CLI は `model_config_roots` (`~/.physai/config.yaml` を参照) に対してそのパスを解決し、一致するディレクトリをパイプラインの作業ディレクトリに rsync します。

## サンプルのエンドツーエンド実行

前提条件: プラットフォームがデプロイ済みで、`physai` CLI が設定済みであること（[DEPLOYMENT.ja.md](../../docs/ja/DEPLOYMENT.ja.md) と [PHYSAI_CLI.ja.md](../../docs/ja/PHYSAI_CLI.ja.md) を参照）。以下のコマンドはリポジトリルートをカレントディレクトリとして想定しています。

### オプション 1 — 公開 PickOrange LeRobot データセットを使用

convert ステージをスキップし、Lightwheel AI が LeIsaac と共に提供する変換済みデータセットを再利用します:

```bash
pip install -U huggingface_hub
hf download LightwheelAI/leisaac-pick-orange \
    --repo-type dataset --local-dir /tmp/leisaac-pick-orange
physai upload datasets /tmp/leisaac-pick-orange/

physai build -n examples/so101-gr00t/containers/leisaac-runtime
physai build -n examples/so101-gr00t/containers/leisaac-gr00t-n1.6
physai build -n examples/so101-gr00t/containers/gr00t-n1.6-trainer

physai run --from train --config examples/so101-gr00t/configs/so101_pickorange_gr00t-n1.6.yaml \
           --dataset leisaac-pick-orange
```

`--from train` は `convert` ステージをスキップします（データセットはすでに LeRobot 形式です）。実行は train → eval と進み、ログをストリーミングします。Ctrl-C でジョブをキャンセルせずにデタッチできます。

### オプション 2 — 独自の HDF5 デモを変換

生の Isaac Lab / LeIsaac HDF5 録画から開始し、パイプラインで変換してからトレーニングと評価を行います:

```bash
# HDF5 ディレクトリを S3 にアップロード (推奨 — FSx DRA 経由で自動インポート):
aws s3 cp --recursive /path/to/my-demos/ s3://<data-bucket>/raw/my-demos/

# 必要な4つのコンテナをすべてビルド:
physai build -n examples/so101-gr00t/containers/so101-converter
physai build -n examples/so101-gr00t/containers/leisaac-runtime
physai build -n examples/so101-gr00t/containers/leisaac-gr00t-n1.6
physai build -n examples/so101-gr00t/containers/gr00t-n1.6-trainer

# フルパイプライン: convert → train → eval.
physai run --config examples/so101-gr00t/configs/so101_liftcube_gr00t-n1.6.yaml \
           --raw my-demos
```

コンバータは生ディレクトリ名に基づいて出力データセットに名前を付けます (`/fsx/datasets/my-demos/`)。別の名前にする場合は `--dataset` でオーバーライドしてください:

```bash
physai run --config ... --raw my-demos --dataset liftcube-baseline
```

### ステージの個別実行

```bash
# 変換のみ (--from convert --to convert の簡略形):
physai convert --config examples/so101-gr00t/configs/so101_liftcube_gr00t-n1.6.yaml \
               --raw my-demos [--dataset my-dataset]

# トレーニングのみ:
physai train --config ... --dataset my-dataset [--max-steps 30000]

# 評価のみ:
physai eval --config ... --checkpoint <run-id> [--eval-rounds 50]
```

## このサンプルの応用

### 新しいタスク、同じロボット

`configs/` 配下に既存の設定をモデルにした新しい設定を追加し、以下を変更します:

- `sim.environment` と `sim.mimic_environment` — LeIsaac の gym ID
- `sim.language_instruction` — 評価時に GR00T に渡す文字列
- `model.config_dir` — タスクが使用するカメラ数に応じて `so101-singlecam` または `so101-dualcam`

コンテナの再ビルドは不要です。

### 新しいロボット

より複雑な作業が必要です。以下が必要になります:

- `containers/so101-converter/app/robot_configs/` 配下に新しいロボットのジョイント名、ジョイントリミット（度数）、モーターリミットを含む新しい `hdf5_*.yaml`。`convert.sh` がそれを選択するよう調整が必要です。
- `model_configs/<gr00t-version>/<new-robot-layout>/` に新しいエンボディメントを反映した `modality.json` + `data_config.py`/`modality_config.py`。
- 本リポジトリ外での LeIsaac 側の作業: USD アセット、ロボット設定、環境登録、ポリシークライアントの更新。チェックリストは [docs/ja/STATUS.ja.md](../../docs/ja/STATUS.ja.md#新しいロボットの追加例-カメラ配置が異なる-so-101) を参照してください。

### 異なる GR00T バージョン

NVIDIA が N1.7（または類似のバージョン）をリリースした場合:

1. `containers/gr00t-n1.6-trainer/` を `gr00t-n1.7-trainer/` にコピーし、`GR00T_REF` / `--base-model-path` を更新します。
2. `containers/leisaac-gr00t-n1.6/` を `leisaac-gr00t-n1.7/` にコピーし、`GR00T_REF` を更新します。
3. `model_configs/gr00t-n1.6/` を `model_configs/gr00t-n1.7/` にコピーし、N1.7 でモダリティ設定 API が変更された場合はインポート/API を更新します。
4. 新しいコンテナとモデル設定を参照する新しい実行設定を `configs/` 配下に作成します。

## 参考リンク

- GR00T N1.6 ファインチューニング: <https://github.com/NVIDIA/Isaac-GR00T/blob/n1.6-release/getting_started/finetune_new_embodiment.md>
- GR00T データ準備: <https://github.com/NVIDIA/Isaac-GR00T/blob/n1.6-release/getting_started/data_preparation.md>
- LeIsaac: <https://github.com/LightwheelAI/leisaac>
- LeRobot v2.1 データセットフォーマット: <https://huggingface.co/docs/lerobot/lerobot-dataset-v3>
- 公開 PickOrange データセット: <https://huggingface.co/datasets/LightwheelAI/leisaac-pick-orange>
