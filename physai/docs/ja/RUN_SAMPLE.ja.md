# サンプルプロジェクトの実行

このドキュメントでは、サンプルプロジェクトを使って physai プラットフォームの使い方を学ぶ手順を説明します。

## physai CLI のインストールと設定

Pythonはシステム環境にインストールするのではなく、[uv venv](https://docs.astral.sh/uv/pip/environments/) などを利用して仮想環境で利用することをお勧めします。

```bash
# まず physai/ ディレクトリに移動してください:

pip install -e cli
```

CLI の設定を行います:

```bash
mkdir -p ~/.physai && cat > ~/.physai/config.yaml <<EOF
host: physai-login
model_config_roots:
  - $(pwd)/examples/so101-gr00t/model_configs
EOF
```

`~/.physai/config.yaml` は `physai` コマンド実行時に参照される設定ファイルです。どちらの値も実行時にコマンドライン引数で上書きできます:

- `--host HOST` で `host` を上書きします
- `--model-config-root PATH` で `model_config_roots` の先頭に追加します（複数回指定可能です）

ホストとモデル設定ルートが1つずつしかない場合は、ここに設定しておけば毎回フラグを指定する必要がなくなります。

## データセットの取得

このサンプルでは、[LeIsaac](https://github.com/lightwheelai/leisaac) シミュレーション環境と合わせて Lightwheel AI が公開している [Pick Orange](https://huggingface.co/datasets/LightwheelAI/leisaac-pick-orange) データセットを使用します。既に LeRobot v2.1 形式で提供されており（60エピソード、約36,000フレーム、698 MB）、クラスタにアップロードするだけで `train` + `eval` を直接実行できます。

```bash
pip install -U huggingface_hub
hf download LightwheelAI/leisaac-pick-orange \
  --repo-type dataset --local-dir /tmp/leisaac-pick-orange
physai upload datasets /tmp/leisaac-pick-orange/
```

`physai upload datasets` コマンドにより、データセットはクラスタ上の `/fsx/datasets/leisaac-pick-orange/` に配置されます。

保存されているデータセットは以下のコマンドで確認できます:

```bash
physai ls datasets
```

## コンテナのビルド

クラスタ上のコンテナは **Enroot**（軽量でルートレスなコンテナランタイム）と **Pyxis**（`srun --container-image=...` で Enroot を利用可能にする Slurm プラグイン）を使ってビルド・実行されます。ここで2つの重要な概念を紹介します:

- **イメージ** — ビルド成果物です。`/fsx/enroot/<name>.sqsh` に配置される squashfs ファイルです。`physai build` を実行するたびに1つのイメージが生成されます。イメージは不変で、ジョブ間で共有され、`--rebuild` するかファイルを削除するまで保持されます。
- **コンテナ** — イメージの実行時インスタンスです。ジョブ開始時にワーカーノード上に作成され、通常はジョブ終了時に破棄されます。ジョブが異常終了した場合にコンテナが残ることがあります。`physai clean --enroot` で不要なコンテナを削除できます。

`physai build` はパイプラインで使用するイメージを生成します。このサンプルでは 3 つのイメージをビルドする必要があります。イメージビルドは Slurm ジョブとして実行されます。以下の 3 コマンドを順に実行すると、ジョブは順次実行されます。3 つすべてが完了するまで約 30 分かかります。

```bash
physai build -n examples/so101-gr00t/containers/leisaac-runtime
physai build -n examples/so101-gr00t/containers/leisaac-gr00t-n1.6
physai build -n examples/so101-gr00t/containers/gr00t-n1.6-trainer
```

ビルドログはターミナルにストリーミングされます。Ctrl-C でデタッチできます（ビルドは継続されます）。`physai logs <job-id>` でビルドログを確認できます。

実行済みジョブの履歴は以下のコマンドで確認できます:

```bash
physai list

physai list
JOB_ID   TYPE    NAME                           STATE        SUBMIT (UTC)    START (UTC)     ELAPSED    COMMENT
3        build   leisaac-gr00t-n1.6             PENDING      04-28 09:40:55  N/A             0:00       base=/fsx/enroot/leisaac-runtime.sqsh
2        build   leisaac-runtime                RUNNING      04-28 09:39:35  04-28 09:39:35  2:50       base=nvcr.io/nvidia/cuda:12.8.1-cudnn-devel-ubuntu24.04
```

## パイプラインの実行

パイプライン定義は YAML ファイルで設定します。この設定ファイルの情報に基づいてパイプラインが実行されます: [examples/so101-gr00t/configs/so101_liftcube_gr00t-n1.6.yaml](../../examples/so101-gr00t/configs/so101_liftcube_gr00t-n1.6.yaml)。

```yaml
pipeline:
  stages: [convert, train, eval]

sim:
  platform: leisaac
  environment: LeIsaac-SO101-LiftCube-v0
  mimic_environment: LeIsaac-SO101-LiftCube-Mimic-v0
  language_instruction: "Lift the red cube up"

model:
  name: gr00t-n1.6
  config_dir: gr00t-n1.6/so101-singlecam

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

- `pipeline.stages`: デフォルトで実行するステージです。
- `stages.<name>`: 各ステージのリソースおよびパラメータ設定です。
- `model.config_dir`: `model_config_roots` に対して解決される相対名です（`run_config.yaml` の全リファレンスは [PIPELINE_DEVELOP.ja.md §4](PIPELINE_DEVELOP.ja.md#4-パイプライン設定) を参照）。

### デフォルトステージの一括実行

以下のコマンドでパイプラインに指定されたステージを実行します:

```bash
# この LeRobot データセットは変換済みのため、設定ファイルがデフォルトで含む
# `convert` ステージをスキップし、`train` から開始します。
physai run -n --config examples/so101-gr00t/configs/so101_pickorange_gr00t-n1.6.yaml \
  --from train --dataset leisaac-pick-orange
```

実行されたパイプラインは Slurm ジョブとして動作します。結果は以下のコマンドで確認できます:

```bash
physai list
JOB_ID   TYPE    NAME                           STATE        SUBMIT (UTC)    START (UTC)     ELAPSED    COMMENT
5        run     run-20260430-011618/eval       COMPLETED    04-30 01:16:34  04-30 02:50:05  00:44:07
4        run     run-20260430-011618/train      COMPLETED    04-30 01:16:29  04-30 01:16:30  01:33:35
```

以上で、サンプルプロジェクトを使ったパイプラインの基本的な使い方の紹介は完了です。サンプルのスクリプトや設定を自分のニーズに合わせて変更してご活用ください。
