# サンプルプロジェクトの実行

このドキュメントでは、サンプルプロジェクトを使って physai プラットフォームの使い方を学びます。

`examples/so101-gr00t/` 配下のサンプル（SO-101 + GR00T）には、すぐ実行できる
4 種類の設定が同梱されています（`{liftcube, pickorange} × {gr00t-n1.5,
gr00t-n1.6}`）。いずれの設定もデフォルトで 3 ステージのパイプラインを実行します。
`convert` は生の HDF5 デモを読み込んで LeRobot v2.1 データセットを書き出し、
`train` はそのデータセットで GR00T ポリシーをファインチューニングし、`eval`
は学習済みポリシーを LeIsaac 上で実行してメトリクスを記録します。

本ドキュメントではこのサンプルの 2 通りの使い方を紹介します。

- **パス A — 公開 LeRobot データセット**: Lightwheel AI が公開している
  PickOrange データセットはすでに LeRobot v2.1 形式のため、`convert` ステージ
  は不要で、パイプラインは `train` から開始されます。最短で動かすルート。
- **パス B — 独自の HDF5 デモ**: Isaac Lab / LeIsaac で記録した生の HDF5 から
  始めます。パイプラインは `convert` を含めて最後まで実行されます。CPU 側の
  コンバーターを 1 つ追加でビルドする必要があります。

CLI のインストールおよびコンテナビルドの大部分は両パスで共通です。

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

ベースランタイム + GR00T N1.6 トレーナー + N1.6 評価コンテナは両パスで必要です:

```bash
physai build -n examples/so101-gr00t/containers/leisaac-runtime
physai build -n examples/so101-gr00t/containers/leisaac-gr00t-n1.6
physai build -n examples/so101-gr00t/containers/gr00t-n1.6-trainer
```

パス B では CPU 用コンバーターも追加でビルドします:

```bash
physai build -n examples/so101-gr00t/containers/so101-converter
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

このサンプルでは、[LeIsaac](https://github.com/lightwheelai/leisaac) シミュレーション環境とともに Lightwheel AI が公開している [Pick Orange](https://huggingface.co/datasets/LightwheelAI/leisaac-pick-orange) データセットを使用します。すでに LeRobot v2.1 形式で提供されているため（60 エピソード、約 36k フレーム、698 MB）、`convert` ステージは不要です。`/fsx/datasets/` に直接アップロードし、`train` から開始します。

データセットのダウンロードとアップロード:

```bash
pip install -U huggingface_hub
hf download LightwheelAI/leisaac-pick-orange \
  --repo-type dataset --local-dir /tmp/leisaac-pick-orange
physai upload datasets /tmp/leisaac-pick-orange/
```

`physai upload datasets` はクラスター上の `/fsx/datasets/leisaac-pick-orange/` にデータセットを配置します。次のコマンドで確認できます:

```bash
physai ls datasets
```

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

## 次のステップ

これでサンプルプロジェクトを使ったパイプライン入門は終了です。サンプル自体
（コンテナの全体構成、同梱されている 4 種類の設定、新しいタスクやロボットへの
適用方法）の詳細は
[examples/so101-gr00t/README.ja.md](../../examples/so101-gr00t/README.ja.md)
を参照してください。
