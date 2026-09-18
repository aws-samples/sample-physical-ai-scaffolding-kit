# サンプルプロジェクトの実行

このドキュメントでは、サンプルプロジェクトを使って physai プラットフォームの使い方を学びます。

`examples/so101-gr00t/` 配下のサンプル（SO-101 + GR00T）には、すぐ実行できる
4 種類の設定が同梱されています（`{liftcube, pickorange} × {gr00t-n1.5,
gr00t-n1.6}`）。いずれの設定もデフォルトで 3 ステージのパイプラインを実行します。
`convert` は生の HDF5 デモを読み込んで LeRobot v2.1 データセットを書き出し、
`train` はそのデータセットで GR00T ポリシーをファインチューニングし、`eval`
は学習済みポリシーを LeIsaac 上で実行してメトリクスを記録します。

本ドキュメントではこのサンプルの 3 通りの使い方を紹介します。

- **パス A — 公開 LeRobot データセット**: Lightwheel AI が公開している
  PickOrange データセットはすでに LeRobot v2.1 形式のため、`convert` ステージ
  は不要で、パイプラインは `train` から開始されます。最短で動かすルート。
- **パス B — 独自の HDF5 デモ**: Isaac Lab / LeIsaac で記録した生の HDF5 から
  始めます。パイプラインは `convert` を含めて最後まで実行されます。CPU 側の
  コンバーターを 1 つ追加でビルドする必要があります。
- **パス C — pask-edge で収集した rosbag**: `pask-edge/` の実機テレオペで
  記録した rosbag2 ディレクトリから始めます。パイプラインは `convert` を含めて
  最後まで実行されます。専用の rosbag → LeRobot コンバーターコンテナを
  1 つ追加でビルドする必要があります。

CLI のインストールおよびコンテナビルドの大部分は全パスで共通です。

## physai CLI のインストールと設定

[uv venv](https://docs.astral.sh/uv/pip/environments/) などで作成した Python の仮想環境にインストールすることを推奨します。システム環境への直接インストールは避けてください。

```bash
# 先に physai/ ディレクトリへ移動:

pip install -e cli
```

CLI を設定します:

```bash
mkdir -p ~/.physai && cat > ~/.physai/config.yaml <<EOF
host: physai-login
model_config_roots:
  - $(pwd)/examples/so101-gr00t/model_configs
EOF
```

`~/.physai/config.yaml` は `physai` コマンドが参照する設定ファイルです。両方の値はコマンドライン引数で実行時に上書きできます:

- `--host HOST` は `host` を上書きします
- `--model-config-root PATH` は `model_config_roots` の先頭に追加します（複数指定可）

ホストとモデル設定のルートが 1 つだけの場合、ここで設定しておけばフラグは不要です。

## コンテナのビルド

クラスター上のコンテナは **Enroot**（軽量・ルートレスのコンテナランタイム）と **Pyxis**（`srun --container-image=...` で Enroot を呼び出せる Slurm プラグイン）でビルド・実行されます。重要な概念が 2 つあります:

- **イメージ** — ビルド成果物。`/fsx/enroot/<name>.sqsh` に置かれる squashfs ファイルです。`physai build` が 1 回ごとに 1 つ生成します。イメージはイミュータブルでジョブ間で共有され、`--rebuild` で再ビルドするかファイルを削除するまで残ります。
- **コンテナ** — イメージから生成された実行中のインスタンスです。ジョブ開始時にワーカーノード上で生成され、通常はジョブ終了時に破棄されます。ジョブが異常終了した場合に残ることがあり、`physai clean --enroot` で残骸を削除できます。

`physai build` でパイプライン用のイメージを生成します。イメージビルドは
Slurm ジョブとして実行され、全体で約 30〜40 分かかります。`/fsx/enroot/<name>.sqsh`
が存在するコンテナについては、`--rebuild` を付けない限り `physai build`
は再ビルドを拒否します。

ベースランタイム + GR00T N1.6 トレーナー + N1.6 評価コンテナは全パス（A / B / C）で必要です:

```bash
physai build -n examples/so101-gr00t/containers/leisaac-runtime
physai build -n examples/so101-gr00t/containers/leisaac-gr00t-n1.6
physai build -n examples/so101-gr00t/containers/gr00t-n1.6-trainer
```

パス B では CPU 用コンバーターも追加でビルドします:

```bash
physai build -n examples/so101-gr00t/containers/so101-converter
```

パス C では rosbag 用コンバーターを追加でビルドします（CPU パーティション、ROS2
本体はインストールせず pure-Python の `rosbags` ライブラリで読み取ります）:

```bash
physai build -n examples/so101-gr00t/containers/so101-rosbag-converter
```

`-n`（`--no-stream`）はビルドを投入してすぐに返るため、上記のコマンドはターミナルを占有することなく、それぞれが個別の Slurm ジョブとして連続して投入されます。特定のビルドを追う場合は `physai logs <job-id>` を使用します。ライブでログを見たい場合は `-n` を外してください。ストリーミング中に Ctrl-C を押すとデタッチできます（ジョブはキャンセルされず継続）。

実行済みジョブの履歴は次のコマンドで確認できます:

```bash
physai list

physai list
JOB_ID   TYPE    NAME                           STATE        SUBMIT (UTC)    START (UTC)     ELAPSED    COMMENT
3        build   leisaac-gr00t-n1.6             PENDING      04-28 09:40:55  N/A             0:00       base=/fsx/enroot/leisaac-runtime.sqsh
2        build   leisaac-runtime                RUNNING      04-28 09:39:35  04-28 09:39:35  2:50       base=nvcr.io/nvidia/cuda:12.8.1-cudnn-devel-ubuntu24.04
```

## 実行設定 (run config)

パイプライン定義は YAML ファイルで設定します。PickOrange + GR00T N1.6 の設定は [examples/so101-gr00t/configs/so101_pickorange_gr00t-n1.6.yaml](../../examples/so101-gr00t/configs/so101_pickorange_gr00t-n1.6.yaml) にあります:

```yaml
pipeline:
  stages: [convert, train, eval]

sim:
  platform: leisaac
  environment: LeIsaac-SO101-PickOrange-v0
  mimic_environment: LeIsaac-SO101-PickOrange-Mimic-v0
  language_instruction: "Pick up the orange and place it on the plate"

model:
  name: gr00t-n1.6
  config_dir: gr00t-n1.6/so101-dualcam

stages:
  convert:
    partition: cpu
    container: so101-converter
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

- `pipeline.stages`: デフォルトで実行するステージです。CLI の `--from` / `--to` フラグで実行時に範囲を絞り込めます。
- `stages.<name>`: 各ステージのリソースおよびパラメータ設定です。
- `model.config_dir`: `model_config_roots` に対して解決される相対名です（`run_config.yaml` の全リファレンスは [PIPELINE_DEVELOP.ja.md §4](PIPELINE_DEVELOP.ja.md#4-パイプライン設定) を参照）。

## パス A — 公開 PickOrange LeRobot データセットで実行

このサンプルでは、[LeIsaac](https://github.com/lightwheelai/leisaac) シミュレーション環境とともに Lightwheel AI が公開している [Pick Orange](https://huggingface.co/datasets/LightwheelAI/leisaac-pick-orange) データセットを使用します。すでに LeRobot v2.1 形式で提供されているため（60 エピソード、約 36k フレーム、698 MB）、`convert` ステージは不要です。`/fsx/datasets/` にダウンロードし、`train` から開始します。

**推奨: login node から直接 `/fsx/datasets/` に落とす**（ローカル PC の帯域を使わない、rsync 転送も不要）。login node には uv が入っているので、使い捨ての Python 環境を作って `hf` CLI を実行します:

```bash
ssh physai-login

# uv で使い捨ての venv を作り huggingface_hub を入れる（ホーム下だけで完結）
uv venv ~/.venv/hf
uv pip install --python ~/.venv/hf/bin/python -U huggingface_hub

# /fsx/datasets/ 直下に直接ダウンロード
~/.venv/hf/bin/hf download LightwheelAI/leisaac-pick-orange \
  --repo-type dataset \
  --local-dir /fsx/datasets/leisaac-pick-orange
```

配置を確認します:

```bash
physai ls datasets
```

login node ではなくローカル PC 側で作業したい場合は、代わりに次の 2 段でも同等です:

```bash
pip install -U huggingface_hub
hf download LightwheelAI/leisaac-pick-orange \
  --repo-type dataset --local-dir /tmp/leisaac-pick-orange
physai upload datasets /tmp/leisaac-pick-orange/
```

こちらはローカル → login → FSx の順で rsync が走るため、ネットワーク往復が余分にかかります（698 MB 程度なら実害はありません）。

パイプラインを `train` ステージから開始します:

```bash
physai run -n --config examples/so101-gr00t/configs/so101_pickorange_gr00t-n1.6.yaml \
  --from train --dataset leisaac-pick-orange
```

実行されたパイプラインは Slurm ジョブとして動作します。結果は次のコマンドで確認できます:

```bash
physai list
JOB_ID   TYPE    NAME                           STATE        SUBMIT (UTC)    START (UTC)     ELAPSED    COMMENT
5        run     run-20260430-011618/eval       COMPLETED    04-30 01:16:34  04-30 02:50:05  00:44:07
4        run     run-20260430-011618/train      COMPLETED    04-30 01:16:29  04-30 01:16:30  01:33:35
```

## パス B — 独自の HDF5 デモで実行

Isaac Lab / LeIsaac で記録した生の HDF5 から開始します。パイプラインの
`convert` ステージが `/fsx/raw/<name>/` から HDF5 を読み込み、LeRobot v2.1
データセットを `/fsx/datasets/<name>/` に書き出します。以降の流れはパス A
と同じです。

HDF5 ディレクトリを S3 にアップロードします（推奨。`/fsx/raw/` は Data
Repository Association のキャッシュで、初回アクセス時に S3 から自動インポート
されます）:

```bash
aws s3 cp --recursive /path/to/my-demos/ s3://<data-bucket>/raw/my-demos/
```

`physai upload raw` で `/fsx/raw/` に直接 rsync することもできますが、相応の
サイズになるデータセットでは S3 経由のほうが高速で、再開も可能です。

`convert` を最初のステージにする場合は `--raw <name>` が必須です:

```bash
physai run -n --config examples/so101-gr00t/configs/so101_pickorange_gr00t-n1.6.yaml \
  --raw my-demos
```

デフォルトではコンバーターは `/fsx/datasets/my-demos/` に出力します（`--raw` と同じ名前）。`--dataset <name>` で上書きできます:

```bash
physai run -n --config ... --raw my-demos --dataset pickorange-baseline
```

singlecam の LiftCube タスクを使う場合は LiftCube の設定ファイルと singlecam のモデル設定に切り替えてください。残りの流れは同じです。

## パス C — pask-edge で収集した rosbag で実行

[`pask-edge/`](../../../pask-edge/) の実機テレオペで記録した rosbag2 (mcap 形式) を
使います。エピソードごとの rosbag2 ディレクトリ（`metadata.yaml` + `.mcap`）を並べた
親ディレクトリを入力として渡します。パイプラインの `convert` ステージが
`/fsx/raw/<name>/` 直下の各エピソードを 1 つずつ読み込み、まとめて 1 つの
LeRobot v2.1 データセットを `/fsx/datasets/<name>/` に書き出します。

**このパスは pask-edge 側で自前収集したデータを持ち込む前提です**。公開の pask-edge
録画データセットは同梱していません。以下、`pen-demos` という自前収集データを使う
ケースを想定した具体例を示します。

### 想定するデータ構造

pask-edge で「Pick up a red pen」タスクを 4 エピソード録画し、次のような
ディレクトリ構造で持っているとします:

```
/path/to/pen-demos/
├── episode_000000/
│   ├── metadata.yaml
│   └── episode_000000_0.mcap
├── episode_000001/
│   ├── metadata.yaml
│   └── episode_000001_0.mcap
├── episode_000002/
│   ├── metadata.yaml
│   └── episode_000002_0.mcap
└── episode_20260906084813/           ← 命名は連番でもタイムスタンプでも良い
    ├── metadata.yaml
    └── episode_000000_0.mcap
```

各エピソードの `metadata.yaml` には rosbag2 標準の `rosbag2_bagfile_information` に
加えて、`custom_data.task` にタスクラベル（例: `"Pick up a red pen"`）と
`custom_data.episode_index` にエピソード番号が入っています。コンバーターはこの
`custom_data.task` を LeRobot データセットの task 文字列として **エピソードごとに**
埋め込みます。

含まれているべきトピック（`robot_configs/so101.yaml` のデフォルトに一致する場合の
例）:

| トピック | メッセージ型 | 用途 |
|---|---|---|
| `/follower/joint_states` | `sensor_msgs/JointState` | `observation.state`（実機関節の現在位置） |
| `/follower/forward_controller/commands` | `std_msgs/Float64MultiArray` | `action`（ros2_control に送信された目標関節位置） |
| `/follower/image_raw/compressed` | `sensor_msgs/CompressedImage` (JPEG) | 手首カメラ映像（`observation.images.wrist`） |
| `/static_camera/image_raw/compressed` | `sensor_msgs/CompressedImage` (JPEG) | 固定カメラ映像（`observation.images.front`） |

トピック名 / JointState 内の joint 名 / Float64MultiArray の並び順が異なる場合は
[`containers/so101-rosbag-converter/app/robot_configs/so101.yaml`](../../examples/so101-gr00t/containers/so101-rosbag-converter/app/robot_configs/so101.yaml)
の `topics` / `follower_joint_mapping` / `follower_commands_order` を編集してから
`physai build --rebuild` してください。コンバーターは最初のエピソード読み込み時に
観測した `JointState.name` を出力するので、そこを見て mapping を合わせられます。

### タスクラベルの解決順

LeRobot データセットに埋め込まれるタスク文字列は次の順で決定されます。

1. **各エピソードの `metadata.yaml` の `rosbag2_bagfile_information.custom_data.task`**
   — pask-edge が記録時に埋め込んだラベル（エピソードごとに異なる値を持てる、優先）
2. run_config の `rosbag.language_instruction`（無ければ `sim.language_instruction`）
   — 上記が欠けているエピソード用の fallback

上記サンプル構造の 4 エピソードは全て `custom_data.task: "Pick up a red pen"` を
含んでいるため、run_config 側の設定は補助的な役割になります。

### 実行設定 (run config) の作成

このデータ用の run_config は自前で用意します。`stages.convert.container` を
`so101-rosbag-converter` にすること、`rosbag.language_instruction` を metadata の
fallback として書いておくことがパス A / B との違いです:

```yaml
# examples/so101-gr00t/configs/so101_pen_gr00t-n1.6_rosbag.yaml (自作)
pipeline:
  stages: [convert, train]         # eval は "Pen" 用の LeIsaac sim env が無いので省略

rosbag:
  # metadata.custom_data.task が欠けている bag 用の fallback タスクラベル
  language_instruction: "Pick up a red pen"

model:
  name: gr00t-n1.6
  config_dir: gr00t-n1.6/so101-dualcam

stages:
  convert:
    partition: cpu
    container: so101-rosbag-converter
  train:
    partition: gpu
    gres: "gpu:1"
    constraint: l40s
    container: gr00t-n1.6-trainer
    max_steps: 10000
```

eval まで含めて回したい場合は、`sim.platform` / `sim.environment` /
`sim.mimic_environment` を用意した LeIsaac シミュレーション環境に合わせて追記し、
`pipeline.stages` に `eval` を戻してください（自作 sim env が必要）。

### データのアップロード

コンバーターは `--raw <name>` で指定された `/fsx/raw/<name>/` を **複数エピソードの
親ディレクトリ**として読み取り、直下の各サブディレクトリを 1 エピソード
= 1 rosbag2（`metadata.yaml` + `.mcap`）として順次処理します。エピソードディレクトリ
名や mcap ファイル名の付け方は自由で、`metadata.yaml` を持つサブディレクトリを名前順に
処理します。`<name>` 直下に `metadata.yaml` が直接ある場合は単一エピソードとして扱う
後方互換パスもあります。

配置方法は 2 通りあります。

**方法 1: S3 経由（推奨、大容量向け）** — `/fsx/raw/` は S3 データバケットの
Data Repository Association キャッシュで、初回アクセス時に S3 から自動インポート
されます。エピソードディレクトリの束を丸ごとアップロードします:

```bash
# /path/to/pen-demos/ 直下に episode_000000/, episode_000001/, ... がある想定
aws s3 cp --recursive /path/to/pen-demos/ \
  s3://<data-bucket>/raw/pen-demos/
```

`<data-bucket>` は `physai/infra/` でデプロイしたスタックの `DataBucket` 出力
（`aws cloudformation describe-stacks` または CDK 出力で確認可能）です。

**方法 2: `physai upload raw` で直接転送（小容量向け）** — ログインノード経由で
`/fsx/raw/` に rsync します。エピソードディレクトリの親をそのまま渡してください:

```bash
physai upload raw /path/to/pen-demos/
```

デフォルトではソースディレクトリ名がそのまま `<name>` になります（上の例では
`/fsx/raw/pen-demos/`）。別名にしたい場合はソース側をリネームしてからアップロード
してください。配置後の確認は次で行えます:

```bash
physai ls raw
```

### パイプラインの起動

`convert` を最初のステージにする場合は `--raw <name>` が必須です:

```bash
physai run -n --config examples/so101-gr00t/configs/so101_pen_gr00t-n1.6_rosbag.yaml \
  --raw pen-demos
```

デフォルトではコンバーターは `/fsx/datasets/pen-demos/` に出力します（`--raw` と
同じ名前）。`--dataset <name>` で上書きできます:

```bash
physai run -n --config ... --raw pen-demos --dataset pen-realworld-v1
```

singlecam モデル（`gr00t-n1.6/so101-singlecam`）を使う場合は `model.config_dir` を
`gr00t-n1.6/so101-singlecam` にし、`robot_configs/so101.yaml` の `topics.cameras`
から `wrist` を削除して `front` のみに絞ってください。

## 次のステップ

これでサンプルプロジェクトを使ったパイプライン入門は終了です。サンプル自体
（コンテナの全体構成、同梱されている 4 種類の設定、新しいタスクやロボットへの
適用方法）の詳細は
[examples/so101-gr00t/README.ja.md](../../examples/so101-gr00t/README.ja.md)
を参照してください。
