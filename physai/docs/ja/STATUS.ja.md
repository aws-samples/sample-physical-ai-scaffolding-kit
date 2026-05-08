# プロジェクトステータス

Phase 1 の現在の状態：完了した内容と今後の予定です。本ドキュメントは [PIPELINE_DESIGN.ja.md](PIPELINE_DESIGN.ja.md) に記載されたプラットフォーム設計に対する実行状況を反映しています。

## Phase 1: LeIsaac + SO-101 + GR00T N1.6

タスク: PickOrange と LiftCube。同じコンテナとモデル設定を使い、`run_config.yaml` を変えるだけで異なるタスクに対応できることを示す2つのタスクです。

### コンテナ

| コンテナ | ベースイメージ | 用途 |
|-----------|-----------|---------|
| `leisaac-runtime` | NGC PyTorch + IsaacSim (pip) | ベースランタイム（GR00T なし） |
| `leisaac-gr00t-n1.6` | `leisaac-runtime` + Isaac-GR00T @ `n1.6-release` | 評価（ポリシーサーバ + LeIsaac クライアント） |
| `so101-converter` | python:3.11-slim + h5py/pyarrow/ffmpeg | HDF5 → LeRobot 変換 + バリデーション |
| `gr00t-n1.6-trainer` | NGC PyTorch + Isaac-GR00T @ `n1.6-release` | GR00T N1.6 ファインチューニング |

すべての LeIsaac タスク（PickOrange、LiftCube など）は同じ `leisaac-runtime` ベースに含まれています。タスクはビルド時ではなく、実行時に設定の `sim.environment` で選択されます。`leisaac-gr00t-n1.6` は `base_container:` を使って `leisaac-runtime` の上にレイヤーを追加し、評価時の GR00T サーバを N1.6 タグに固定しています。今後の作業で `leisaac-gr00t-n1.5` / `-n1.7` なども、ベースを再ビルドせずに同様の方法で追加できます。

コンテナはコンテナビルドシステム（PIPELINE_DESIGN.ja.md 3.4節を参照）でビルドされ、squashfs として `/fsx/enroot/` に保存されます。Slurm ジョブは Pyxis の `--container-image` でこれらを利用します。

**IsaacSim に関する注意事項**:
- `leisaac-runtime` には `50-warmup.sh` セットアップフックが含まれており、ビルド中に IsaacSim のシェーダキャッシュをウォームアップします（[上流の warmup.sh](https://github.com/isaac-sim/IsaacSim/blob/main/source/scripts/warmup.sh) と同等です）。pip インストール版の IsaacSim はスタンドアロン配布用レイアウトを前提とする `kit-gcov` しか同梱しないため、`kit` バイナリの代わりに `kit_app.py` を使用します。
- 評価ジョブには `DISPLAY=:0` と `/tmp/.X11-unix` のマウントが必要です。IsaacSim はヘッドレスモードでも GLFW/GLX を必要とします。GPU ノードにはライフサイクルスクリプト（`install_xorg.sh`）で Xorg がインストールされます。
- `policy_inference.py` の出力を `tee` 経由でキャプチャするには `PYTHONUNBUFFERED=1` が必要です。

### run_config.yaml

2つの設定ファイル — 同じコンテナ、同じモデル設定、異なるタスクです:

```yaml
# examples/so101-gr00t/configs/so101_pickorange_gr00t-n1.6.yaml
pipeline:
  stages: [train, eval]

sim:
  platform: leisaac
  environment: LeIsaac-SO101-PickOrange-v0
  mimic_environment: LeIsaac-SO101-PickOrange-Mimic-v0
  language_instruction: "Pick up the orange"

model:
  name: gr00t-n1.6
  config_dir: gr00t-n1.6/so101

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

```yaml
# examples/so101-gr00t/configs/so101_liftcube_gr00t-n1.6.yaml
pipeline:
  stages: [train, eval]

sim:
  platform: leisaac
  environment: LeIsaac-SO101-LiftCube-v0
  mimic_environment: LeIsaac-SO101-LiftCube-Mimic-v0
  language_instruction: "Lift the red cube up"

model:
  name: gr00t-n1.6
  config_dir: gr00t-n1.6/so101

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

異なるのは `sim.environment`、`sim.mimic_environment`、`sim.language_instruction` のみです。コンテナ、モデル設定、ステージパラメータはすべて同一です。

### モデル設定: GR00T N1.6 for SO-101

```
examples/so101-gr00t/model_configs/gr00t-n1.6/so101/
├── modality.json              # ジョイントグループ → インデックスのマッピング
└── modality_config.py         # ModalityConfig: アクション表現、正規化、ホライズン
```

### HDF5 入力フォーマット（LeIsaac）

SO-101 向け LeIsaac 環境: PickOrange、LiftCube。

Phase 1 のデータ収集方法:
- **リーダーアーム**: フォロワーがリーダーの関節位置をミラーリングします。`obs/joint_pos` にフォロワーの状態を記録します。
- **キーボードテレオペ**: IK デルタでアームを制御します。`obs/joint_pos` に得られた関節位置を記録します。

```
data/
  demo_0/
    obs/
      joint_pos        (T, 6)  float32, radians
      joint_pos_target (T, 6)  float32, radians
      actions          (T, N)  float32 — IK deltas (keyboard) or joint pos (leader)
      front            (T, 480, 640, 3) uint8
      wrist            (T, 480, 640, 3) uint8
      ee_frame_state   (T, 7)  float32
    initial_state      dict — full scene state for Mimic reset
    states             (T, ...) — articulation + rigid object states for Mimic
```

> **重要**: `obs/actions` はテレオペデバイスによって次元とセマンティクスが異なります。コンバータは observation.state とアクションの両方に `obs/joint_pos` を使用します（`obs/actions` ではありません）。

> **リスク**: テレオペデバイスが異なると `obs/joint_pos` の値域が異なる可能性があります（例: グリッパー値）。コンバータにデバイスごとの調整が必要になるかもしれません。実装時にリーダーアームとキーボードで同じタスクの HDF5 出力を比較して検証する必要があります。

### オーグメンテーション: Isaac Lab Mimic（ストレッチゴール）

オーグメンテーションは最後に実装します。時間が足りない場合やスムーズに進まない場合はスキップ可能です — パイプラインはオーグメンテーションなしでも動作します（オーグメンテーションはオプションです）。

`leisaac-runtime` 内の `/app/augment.sh` は4ステップの Mimic パイプラインを実行します:

1. **eef_action_process.py --to_ik**: 記録されたアクションを絶対 EEF ポーズに変換します（デバイス非依存）
2. **annotate_demos.py**: Mimic 環境バリアントを使ってサブタスク境界をアノテーションします
3. **generate_dataset.py**: ポーズ摂動 + リプレイによる拡張デモを生成します
4. **eef_action_process.py --to_joint**: IK アクションをジョイントアクションに逆変換します

Mimic にはシード HDF5 に `initial_state` と `states` が必要です（デモ収集時に記録されます）。LeRobot データセットは Mimic に使用できません — 変換時にこれらのフィールドが失われるためです。

**既知の問題**: `annotate_demos.py` に IsaacLab v2.3.0 との互換性バグがあります（`Se3Keyboard` の API 変更、`torch.any()` の型エラー）。パッチが必要です。

### 変換: SO-101 HDF5 → LeRobot v2.1

2つのパスがあります:

**オプション A: LeIsaac の `isaaclab2lerobot.py`**（Isaac Sim + GPU が必要）
```bash
python scripts/convert/isaaclab2lerobot.py \
  --task_name=LeIsaac-SO101-PickOrange-v0 \
  --repo_id=local/so101_pickorange \
  --hdf5_root=./datasets --hdf5_files=demos.hdf5 --headless
```

**オプション B: スタンドアロンの `hdf5_to_lerobot.py`**（CPU のみ、数時間ではなく数分）
```bash
python hdf5_to_lerobot.py \
  --hdf5_file ${INPUT_HDF5} \
  --output_dir /fsx/datasets/${RUN_ID}
```
SO-101 のジョイントリミット、カメラ名（`front`、`wrist`）、modality.json をハードコードしています。入力は `/fsx/raw/`（オーグメンテーションなし）またはローカル NVMe（オーグメンテーションあり）からです。

**変換後の処理**: `/app/convert.sh` は必要に応じて AV1 → H.264 の再エンコードも行うべきです（GR00T の decord ローダーは AV1 をサポートしていません）。

### バリデーション: GR00T N1.6

チェック項目: 6D アクション + 6D ステート + front/wrist カメラ + `modality.json` の存在確認です。

### トレーニング: GR00T N1.6

```bash
physai train --config examples/so101-gr00t/configs/so101_liftcube_gr00t-n1.6.yaml \
  --dataset so101_liftcube
```

`max_steps` は設定の `stages.train.max_steps` から取得されます（デフォルト: 10000）。CLI の `--max-steps` でオーバーライドできます。

上記コマンドは `gr00t-n1.6-trainer` コンテナ内で実行される Slurm ジョブをサブミットします:

```bash
bash /app/train.sh /fsx/datasets/so101_liftcube \
  /fsx/physai/sync/<run-id>/model_config \
  /fsx/checkpoints/<run-id> \
  10000
```

内部的に `/app/train.sh` は `gr00t/data/stats.py`（正規化統計量）を実行した後、`--embodiment-tag NEW_EMBODIMENT` を指定して `launch_finetune.py` を実行します。

### 評価: GR00T + LeIsaac

```bash
physai eval --config examples/so101-gr00t/configs/so101_liftcube_gr00t-n1.6.yaml \
  --checkpoint gr00t-n1.6-liftcube-30k
```

`rounds` は設定の `stages.eval.rounds` から取得されます（デフォルト: 20）。CLI の `--eval-rounds` でオーバーライドできます。

上記コマンドは `leisaac-runtime` コンテナ内で実行される Slurm ジョブをサブミットします:

```bash
bash /app/eval.sh /fsx/checkpoints/gr00t-n1.6-liftcube-30k \
  /fsx/physai/sync/<run-id>/model_config \
  /fsx/evaluations/<run-id> \
  20
```

内部的に `/app/eval.sh` は GR00T ポリシーサーバ（`run_gr00t_server.py`）を起動し、LeIsaac の `policy_inference.py` を `--policy_type=gr00tn1.6` で実行します。`--visual` が渡された場合（`physai eval --visual` 経由）は `--headless` を省略して Isaac Sim が DCV セッションにレンダリングします。それ以外の場合はバッチ評価のために `--headless` を渡します。`DISPLAY` は常に設定が必要です — IsaacSim はヘッドレスモードでも GLFW/GLX を必要とします。

### 拡張

#### 新しいロボットの追加（例: カメラ配置が異なる SO-101）

LeIsaac リポジトリ内の変更:

| 内容 | 場所（LeIsaac リポジトリ内） | 例 |
|------|------------------------|---------|
| USD アセットファイル | `assets/robots/so101_topcam.usd` | カメラをリストではなく上部に設置した新しい USD |
| ロボット設定 | `source/leisaac/leisaac/assets/robots/lerobot.py` | 新しい `SO101_TOPCAM_CFG`（ジョイントプロパティ、アクチュエータ、USD パスを定義する `ArticulationCfg`） |
| ジョイント + モーターリミット | `source/leisaac/leisaac/utils/constant.py` | 新しいリミット配列（SO-101 と異なる場合のみ） |
| 座標変換 | `source/leisaac/leisaac/utils/robot_utils.py` | `convert_leisaac_action_to_lerobot()` が SO-101 のリミットをハードコードしています — パラメータ化が必要です |
| カメラ設定 | `source/leisaac/leisaac/tasks/template/single_arm_env_cfg.py` → `SingleArmTaskSceneCfg` | `TiledCameraCfg` を変更します: `wrist` → `top` にリネーム、`prim_path` と `offset` を更新します |
| ポリシークライアント | `source/leisaac/leisaac/policy/service_policy_clients.py` | カメラ名が変わる場合はモダリティキーのマッピングを更新します |

LeIsaac 以外の変更:

| 内容 | 場所 | 例 |
|------|-------|---------|
| 変換スクリプト | `hdf5_to_lerobot.py` | ハードコードされたジョイントリミットとカメラ名を更新します |
| モデル設定 | `examples/.../model_configs/gr00t-n1.6/so101_topcam/` | 更新されたビデオキーマッピングを含む新しい `modality.json` |
| コンテナ再ビルド | `leisaac-runtime` + `so101-converter` | 更新されたコードでリビルドします |

**重要な結合**: `robot_utils.py` と `hdf5_to_lerobot.py` はどちらも SO-101 のジョイントリミットをハードコードしています。新しいロボットでは両方の更新が必要です。

#### 新しいタスクの追加（例: StackBlocks）

LeIsaac リポジトリ内の変更:

| 内容 | 場所（LeIsaac リポジトリ内） | 例 |
|------|------------------------|---------|
| USD シーン | `assets/scenes/table_with_blocks/scene.usd` | 物理プロパティ付きの 3D シーン |
| シーン設定 | `source/leisaac/leisaac/assets/scenes/table_with_blocks.py` | オブジェクトのスポーン位置、物理マテリアル |
| 環境設定 | `source/leisaac/leisaac/tasks/stack_blocks/env_cfg.py` | `SingleArmTaskEnvCfg` を継承します |
| 成功条件 | `source/leisaac/leisaac/tasks/stack_blocks/mdp/terminations.py` | 例: ブロックの Z > 閾値 |
| サブタスクシグナル | `source/leisaac/leisaac/tasks/stack_blocks/mdp/observations.py` | Mimic オーグメンテーションに必要です |
| Gym 登録 | `source/leisaac/leisaac/tasks/stack_blocks/__init__.py` | `gym.register(id="LeIsaac-SO101-StackBlocks-v0", ...)` |
| Mimic バリアント | `source/leisaac/leisaac/tasks/stack_blocks/mimic_env_cfg.py` | ポーズランダム化範囲、サブタスク定義 |

LeIsaac 以外の変更:

| 内容 | 場所 |
|------|-------|
| パイプライン設定 | `environment: LeIsaac-SO101-StackBlocks-v0` を含む新しい `run_config.yaml` |
| コンテナ再ビルド | 新しいタスクコードを含む `leisaac-runtime` |

ロボットが変わらないため、モデル設定ディレクトリは再利用可能です。

#### オーナーシップ

| 責任範囲 | オーナー |
|---------------|-------|
| HDF5 デモフォーマット | LeIsaac |
| HDF5 → LeRobot 変換 | 共同（LeIsaac の `isaaclab2lerobot.py` または本パイプラインの `hdf5_to_lerobot.py`） |
| データオーグメンテーション（Mimic） | LeIsaac |
| モデルトレーニング | モデルリポジトリ（Isaac-GR00T、OpenPI） |
| 評価 | LeIsaac（環境 + ポリシークライアント）+ モデルリポジトリ（ポリシーサーバ） |
| オーケストレーション + トラッキング | 本パイプライン |
| コンテナビルド | 本パイプライン |

---

## 実装ステータス

### 実装済み

#### インフラストラクチャ

- 2つの CDK スタック: `PhysaiInfraStack`（VPC、S3 データバケット、DRA 付き FSx for Lustre、RDS MariaDB、Secrets Manager）と `PhysaiClusterStack`（HyperPod クラスタ、IAM、ライフサイクルスクリプトバケット）
- デプロイごとに一意の HyperPod `ClusterName`（CloudFormation スタックの GUID がサフィックスとして付与）により、各デプロイが `sacct` で独自のアイデンティティを持ちます
- ライフサイクルスクリプト: FSx マウント、Slurm デーモン、Docker、Enroot + Pyxis、cgroup トラッキング、ノード機能マッピング、Slurm アカウンティング（slurmdbd → RDS）
- ライフサイクルスクリプトのコンテンツハッシュ付き S3 プレフィックスにより、スクリプト変更時に CloudFormation が `UpdateCluster` を発行します
- ヘルパースクリプト: `setup-ssh.sh`、`cleanup.sh`、`cleanup-failed-stacks.sh`

#### CLI (`physai`)

- コマンド: `build`、`run`、`train`、`eval`、`ls`、`upload`、`list`、`status`、`logs`、`cancel`、`clean`、`doctor`
- `ControlMaster` による SSH セッション多重化、Ctrl-C でのデタッチ/再接続
- `--from`/`--to` によるステージ選択と Stage レジストリ
- 設定可能な検索パスによるモデル設定ディレクトリの解決
- SSH エラーメッセージのわかりやすい表示（ホストキー不一致、認証失敗など）

#### コンテナビルドシステム

- `project.yaml` + `container.yaml` スキーマ（setup-hooks/app レイアウト）
- レジストリイメージ用の `base_image`、ビルド済みコンテナの上にレイヤーを追加する `base_container`
- `physai build` はコンテナフォルダをクラスタに同期し、Pyxis 経由で各フックを実行する sbatch を生成、`app/` を `/app/` にコピーし、squashfs を `/fsx/enroot/<name>.sqsh` にエクスポートします

#### パイプラインステージ

- `train`、`eval` — 公開 Pick Orange データセットを使ったエンドツーエンド動作を実装・確認済みです

#### オブザーバビリティ

- RDS MariaDB 経由の Slurm アカウンティング（`sacct`）
- `physai logs <job-id>` で Ctrl-C デタッチ付きストリーミング

### 未実装

#### 次の優先事項（フルパイプラインのブロッカー）

- `convert` ステージ — `so101-converter` コンテナ（HDF5 → LeRobot v2.1）
- `validate` ステージ — データセットの構造チェック + GR00T 固有のチェック
- `register` ステージ — チェックポイント + メトリクスを S3 に公開し、MLflow に記録します
- `train.sh` 出力契約 — `train_summary.json`（最終ロス、ステップ数、チェックポイントパス）を定義し、`register` がトレーニング出力を利用できるようにします。現在 `train.sh` はモデルチェックポイントのみを書き出します
- ステージ出力の S3 エクスポート — `/fsx/` 配下のデータセット、チェックポイント、評価結果を各ステージ終了時に `s3://<data-bucket>/{datasets,checkpoints,results}/` に公開します。パイプラインオーケストレータが明示的にエクスポートを実行します（例: `aws s3 cp`）。FSx の data-repository export は使用しません。`/fsx/` はワーキングストレージとしてのみ扱います
- CDK での MLflow トラッキングサーバ

#### 計画中

- DCV ビジュアル評価（`physai eval --visual`）
- GR00T N1.5 のサンプル（初回 PR 後のフォローアップ）

#### ストレッチ

- オーグメンテーションステージ — `leisaac-runtime` 内の `/app/augment.sh` による Isaac Lab Mimic
- π0 / OpenPI モデルサポート
- 追加ロボット（Panda、Unitree G1、双腕アーム）
- LeIsaac 以外のシミュレーション環境
- マルチ GPU トレーニング
