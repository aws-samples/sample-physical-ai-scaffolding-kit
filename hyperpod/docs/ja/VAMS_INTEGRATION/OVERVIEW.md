# VAMS 連携の導入と運用

この文書は、VAMS のワークフローから、PASK の HyperPod クラスタで `samples/lerobot-openpi` の学習ジョブを実行する連携の、導入と運用の手順を示します。
VAMS を使ったことが無い読者を想定し、VAMS を AWS の環境にデプロイするところから説明します。

連携の仕組み、データをコピーする理由、データの量の目安と代替案、権限は [VAMS 連携の仕組み](/hyperpod/docs/ja/VAMS_INTEGRATION/ARCHITECTURE.md) にあります。
メッセージ、ファイル、状態の形とポーラーの処理の規則は [VAMS 連携の仕様](/hyperpod/docs/ja/VAMS_INTEGRATION/SPEC.md) にあります。

## VAMS について

**VAMS**（Visual Asset Management System）は、3D モデル、点群、動画、ロボットの学習データなどのファイルを、AWS のアカウントの中で保管し、版を管理し、ブラウザで閲覧するためのオープンソースのソリューションです。
[awslabs/visual-asset-management-system](https://github.com/awslabs/visual-asset-management-system) で公開されていて、AWS CDK で利用者のアカウントにデプロイします。
ファイルに対する処理は、パイプラインとワークフローとして登録して、画面か CLI から実行します。

この文書で使う VAMS の用語は次のとおりです。

| 用語 | 意味 |
|---|---|
| データベース | アセットをまとめる単位です。パイプラインとワークフローもデータベースごとに登録します |
| アセット | ファイルの集まりです。この連携では、LeRobot のデータセット 1 つと、学習済みモデル 1 つが、それぞれ 1 つのアセットになります |
| パイプライン | 1 つの処理の定義です。この連携では、PASK の SQS キューにメッセージを送り、PASK からの完了の通知を待つ処理です |
| テンプレート | パイプラインに渡す設定の雛形です。この連携では、学習パラメータの既定値と、実行のときに変えられる項目を定めます |
| ワークフロー | パイプラインを並べたもので、利用者が実行する単位です |

連携の機能はすべて PASK 側にあり、VAMS のコードとデプロイの設定は変更しません。
VAMS には、PASK に同梱するパイプラインとワークフローの定義（`hyperpod/vams/`）を登録するだけです。

## 導入の流れ

導入は次の順に進めます。

1. 前提を満たしていることを確かめます（「前提」の章）。
2. VAMS をデプロイします（「VAMS のデプロイ」の章）。
3. VamsCLI を準備します（「VamsCLI の準備」の章）。
4. 連携の設定に使う VAMS の値を確かめます（「VAMS の値の確認」の章）。
5. `cdk.json` の `Vams` を設定します（「設定」の章）。
6. PASKStack をデプロイします（「デプロイ」の章）。
7. login ノードにポーラーを導入します（「login ノードへのポーラーの導入」の章）。
8. VAMS にデータベースと、学習の入力と出力のアセットを用意します（「データベースとアセットの用意」の章）。
9. VAMS にパイプラインとワークフローを登録します（「パイプラインの登録」の章）。
10. VAMS から学習を実行します（「学習の実行」の章）。

すでに VAMS をデプロイしてある場合は、手順 2 を飛ばします。

## 前提

PASK の HyperPod クラスタで、[LeRobot での pi0.5 の学習](/samples/lerobot-openpi/docs/ja/lerobot_pi05_training.md)を手動で実行できる状態にしておきます。
VAMS からの学習も同じ `slurm_train.sh` と同じコンテナイメージ（`lerobot-pi-train+<IMAGE_TAG>.sqsh`）を使うためです。
コンテナイメージが無いと、学習ジョブが起動の時点で失敗します。

学習を MLflow に記録するので、`cdk.json` の `Mlflow.Enabled` を `true` にして、PASKStack の MLflow App も使える状態にしておきます。

VAMS は、PASK と同じ AWS アカウントの同じリージョンにデプロイします。
連携は、VAMS のワークフローのロールが PASK のキューに直接メッセージを送る前提で作られていて、アカウントやリージョンをまたぐ設定を持たないためです。

作業する PC には、PASK のデプロイに使うツールに加えて、次のものを用意します。

| ツール | 用途 |
|---|---|
| Python 3.13 以上 | VamsCLI を動かします |
| Docker か互換のコンテナエンジン | VAMS の `cdk deploy` でコンテナイメージをビルドします |
| Node.js 22 以上と npm | VAMS の画面のビルドと CDK に使います |
| `jq` | この文書の手順で、定義のファイルから値を取り出します |

## VAMS のデプロイ

VAMS のデプロイの詳細は、VAMS の [Deployment Guide](https://awslabs.github.io/visual-asset-management-system/deployment/deploy-the-solution) にあります。
ここでは、連携に必要な最小の手順と、連携のために決める値を示します。
初回のデプロイには 30 分から 45 分ほどかかります。

1. リポジトリを取得し、安定版のタグに切り替えます。

    ```bash
    git clone https://github.com/awslabs/visual-asset-management-system.git
    cd visual-asset-management-system
    git fetch --all --tags
    git checkout tags/<リリースのタグ>
    ```

    リリースのタグは [Releases](https://github.com/awslabs/visual-asset-management-system/releases) で確かめます。
    この文書の手順は、VAMS 2.6.2 と VamsCLI 2.6.2 で確かめました。

2. 画面をビルドし、CDK の依存をインストールします。

    ```bash
    cd web && nvm use && npm install && npm run build
    cd ../infra && npm install
    ```

3. CDK をまだ使ったことの無いアカウントとリージョンなら、bootstrap します。

    ```bash
    cdk bootstrap aws://<アカウント ID>/<PASK と同じリージョン>
    ```

4. 設定ファイルを雛形から作り、次の項目を書き換えます。

    ```bash
    cp config/config.template.commercial.json config/config.json
    ```

    | 項目 | 値 |
    |---|---|
    | `name` | 雛形の `vams` のままにします。この値が、PASK の `Vams.ConfigName` になります |
    | `env.region` | PASK と同じリージョン |
    | `app.adminEmailAddress` | 最初の管理者のメールアドレス。仮のパスワードがここに届きます |
    | `app.adminUserId` | 最初の管理者のユーザー名 |
    | `app.baseStackName` | スタックの名前に入る環境の名前（例 `dev`） |

5. デプロイします。

    ```bash
    cdk deploy --all --require-approval never
    ```

    Docker（か設定したコンテナエンジン）が起動している必要があります。

6. 出力の `WebAppCloudFrontDistributionDomainName` に表示される URL をブラウザで開き、メールで届いた仮のパスワードでサインインして、パスワードを設定します。

## VamsCLI の準備

連携の登録と実行には、VAMS のコマンドラインツールの VamsCLI（`vamscli`）を使います。
VamsCLI は、手順 1 で取得した VAMS のリポジトリの `tools/VamsCLI` にあります。

1. 仮想環境に VamsCLI をインストールします。

    ```bash
    cd visual-asset-management-system/tools/VamsCLI
    python3 -m venv .venv
    source .venv/bin/activate
    pip install -e .
    vamscli --version
    ```

2. VAMS の URL を設定し、管理者でログインします。

    ```bash
    vamscli setup https://<VAMS の URL>
    vamscli auth login -u <管理者のユーザー名>
    ```

VamsCLI の詳しい使い方は、VAMS の [CLI Reference](https://awslabs.github.io/visual-asset-management-system/cli/getting-started) にあります。

## VAMS の値の確認

PASK の `cdk.json` に書く VAMS の値を確かめます。

1. VAMS の `config.json` の `name` を確かめます。
    雛形のままなら `vams` です。
    この値を `Vams.ConfigName` に書きます。

2. VAMS のアセットバケットを一覧にします。

    ```bash
    vamscli database list-buckets
    ```

    表示されたすべてのバケットの名前を `Vams.AssetBucketNames` に書きます。
    既定のバケット（`isDefault` が `true` のもの）の名前を `Vams.WorkflowBucketName` に書きます。
    VAMS は、パイプラインの実行の入力と出力を、既定のバケットの `pipelines/` に置くためです。

3. バケットの暗号化とバージョニングを確かめます。

    ```bash
    aws s3api get-bucket-encryption --bucket <バケットの名前>
    aws s3api get-bucket-versioning --bucket <バケットの名前>
    ```

    暗号化が SSE-KMS の場合だけ、鍵の ARN を `Vams.KmsKeyArns` に書きます。
    バージョニングが有効でない場合の影響は「VAMS 側に期待する運用設定」の章にあります。

## 設定

VAMS 連携の設定は、[hyperpod/cdk.json](/hyperpod/cdk.json) の `config.Vams` に書きます。
`Enabled` が `false` の場合や `Vams` が無い場合は、連携のリソースを何も作らず、スタックのテンプレートは連携が無い場合と同じになります。

`Enabled` を `true` にした場合、`ConfigName`、`AssetBucketNames`、`WorkflowBucketName`、`MlflowExperimentName` と数値の項目は省略できません。
`Mlflow.Enabled` も `true` にする必要があり、`false` のままでは `cdk synth` が失敗します。
空の値や省略した項目があると、`cdk synth` と `cdk deploy` が失敗します。
`KmsKeyArns` と `SlurmPartition` は省略できます。

| 項目 | `cdk.json` の値 | 内容と値の決め方 |
|---|---|---|
| `Enabled` | `false` | `true` で連携のリソースを作ります |
| `ConfigName` | `vams` | VAMS のデプロイの `config.name`。キューの名前（`pask-<ConfigName>-training`）に入ります。VAMS のワークフローのロールは、名前に `config.name` を含むキューにだけメッセージを送れるため、VAMS の値と同じにします。英数字、`-`、`_` だけを使えます |
| `AssetBucketNames` | `[]` | VAMS のすべてのアセットバケットの名前。外部のアセットバケットも含めます。`vamscli database list-buckets` で確かめます |
| `WorkflowBucketName` | `""` | VAMS が実行の manifest と outputs を置くバケット（メッセージの `workflowExecutionS3InputOutputBucket`）。VAMS の既定のアセットバケットです |
| `SeparateLoginRole` | `true` | `true` で login グループのロールを、controller と worker のグループのロールから分けます。作ってあるクラスタでは `false` にします（「デプロイ」の節）。省略した場合は `true` と同じです |
| `KmsKeyArns` | `[]` | VAMS のバケットが SSE-KMS で暗号化されている場合だけ、鍵の ARN を書きます。暗号化の方式は `aws s3api get-bucket-encryption --bucket <バケット名>` で確かめます |
| `QueueVisibilityTimeoutSeconds` | `300` | キューの可視性タイムアウト（秒）。受付の処理（manifest の読み込みと DynamoDB への記録）が収まる長さにします |
| `MaxReceiveCount` | `5` | 受け付けられなかったメッセージを DLQ に移すまでの受信の回数 |
| `HeartbeatIntervalSeconds` | `300` | VAMS の Step Functions に heartbeat を送る間隔（秒） |
| `AbortGraceSeconds` | `1800` | heartbeat のエラーから中断と判断するまでの猶予（秒）。VAMS の送信のリトライで新しい token が届くまでの時間（1 分以内）より長くします |
| `LeaseSeconds` | `900` | 実行を処理するポーラーのリースの長さ（秒）。`HeartbeatIntervalSeconds` の 3 倍にします |
| `ItemTtlDays` | `30` | 終わった実行の DynamoDB の item を残す日数。SQS の保持期間（14 日）とパイプラインの `taskTimeout` の上限（7 日）の合計より長くします |
| `StagingExpirationDays` | `10` | PASK のデータバケットの `<S3Prefix>vams/` のオブジェクトを期限切れにする日数。後片付けで消えなかったコピーを消すためのもので、`taskTimeout` の上限（7 日）より長くします |
| `IngestWaitSeconds` | `3600` | コピーしたデータセットが FSx に現れるまで待つ上限（秒） |
| `MaxConcurrentRuns` | `1` | 同時にデータのコピー以降へ進める実行の数。FSx の容量は 1200 GiB で固定なので、データセットとチェックポイントが収まる数にします |
| `SlurmPartition` | `""` | 学習ジョブを投入する Slurm のパーティション。空なら Slurm の既定のパーティションです |
| `MlflowExperimentName` | `vams-training` | VAMS からの学習を記録する MLflow の experiment。手動の学習の experiment（`lerobot-pi`）と分けます |

`QueueVisibilityTimeoutSeconds` と `MaxReceiveCount` はキューに、`StagingExpirationDays` はバケットのライフサイクルルールに設定します。
`MaxReceiveCount` と `StagingExpirationDays` を除く値は、login ノードのポーラーの設定ファイル `/etc/pask/vams-poller.env` にも `PASK_VAMS_*` の環境変数として書かれます。
この設定ファイルは、ポーラーを導入したときにだけ書かれます。
値を変えてデプロイした後は、「login ノードへのポーラーの導入」の手順で導入をやり直します。

`cdk.json` の設定の例です。
`<...>` は実際の値に置き換えます。

```json
"Vams": {
  "Enabled": true,
  "ConfigName": "<VAMS の config.name>",
  "AssetBucketNames": ["<VAMS のアセットバケット>"],
  "WorkflowBucketName": "<VAMS の既定のアセットバケット>",
  "SeparateLoginRole": true,
  "KmsKeyArns": [],
  "QueueVisibilityTimeoutSeconds": 300,
  "MaxReceiveCount": 5,
  "HeartbeatIntervalSeconds": 300,
  "AbortGraceSeconds": 1800,
  "LeaseSeconds": 900,
  "ItemTtlDays": 30,
  "StagingExpirationDays": 10,
  "IngestWaitSeconds": 3600,
  "MaxConcurrentRuns": 1,
  "SlurmPartition": "",
  "MlflowExperimentName": "vams-training"
}
```

## デプロイ

デプロイは [デプロイについて](/hyperpod/docs/ja/DEPLOYMENT.md) の手順に従い、`cdk deploy` の前に `cdk.json` の `Vams.Enabled` と `Mlflow.Enabled` を `true` にして、ほかの項目を書きます。
`Vams.Enabled` を `true` にして `Vams.SeparateLoginRole` を `true` にすると、login グループのロールが、controller と worker のグループのロールから分かれます。
VAMS のバケットと Step Functions への権限を、login ノードのポーラーにだけ持たせるためです。

HyperPod は、作ってあるインスタンスグループの ExecutionRole の変更を受け付けません。
作ってあるクラスタで `SeparateLoginRole` を `true` にしてデプロイすると、`cdk diff` には `AWS::SageMaker::Cluster` の更新と表示されますが、デプロイは `Updating fields ExecutionRoleArn on an InstanceGroup not supported` で失敗し、スタックはロールバックされます。
作ってあるクラスタに連携を加える場合は、`SeparateLoginRole` を `false` にします。
この場合、login のロールは分かれず、すべてのグループが共通のロールを使い、ポーラーの権限もそのロールに付きます（[VAMS 連携の仕組み](/hyperpod/docs/ja/VAMS_INTEGRATION/ARCHITECTURE.md) の「権限とセキュリティ」）。
`SeparateLoginRole` を `true` にできるのは、クラスタを新しく作る場合だけです。

デプロイの前に `cdk diff` で、`AWS::SageMaker::Cluster` の `InstanceGroups` の `ExecutionRole` が変わらないことを確かめます。

```bash
cdk diff
```

`AWS::SageMaker::Cluster` のリソースに `(requires replacement)` か `(may cause replacement)` が表示された場合は、デプロイしないでください。
クラスタが作り直されると、ノードのローカルの状態と実行中のジョブが失われます。
`Vams.Enabled` を `true` から `false` に戻す場合も、ロールの割り当てが変わらないことを同じように確かめます。

確かめたら、デプロイします。

```bash
cdk deploy
```

デプロイが成功すると、[デプロイについて](/hyperpod/docs/ja/DEPLOYMENT.md) の Outputs に加えて、次の Outputs が出力されます。

```bash
Outputs:
PASK.HyperPodLoginExecutionRoleARN = login グループの node の Role arn
PASK.VamsVamsDeadLetterQueueUrl = 受け付けられなかったメッセージが入る DLQ の URL
PASK.VamsVamsQueueArn = VAMS のワークフローがメッセージを送る SQS キューの ARN
PASK.VamsVamsQueueUrl = VAMS のワークフローがメッセージを送る SQS キューの URL（パイプラインの登録に使う値）
PASK.VamsVamsTableName = 実行の状態を記録する DynamoDB テーブルの名前
```

CloudFormation の Outputs のキーには、上の名前の後ろに 8 文字の英数字が付きます。
この文書のコマンドは、キーの先頭の一致で値を取り出します。

DynamoDB のテーブルは、スタックを削除しても削除されません（`RemovalPolicy.RETAIN`）。
不要になった場合は、手動で削除してください。

## login ノードへのポーラーの導入

ポーラー（systemd のサービス `pask-vams-poller`）は、login ノードのライフサイクルスクリプト `on_init_complete.sh` が導入します。
ライフサイクルスクリプトはノードを作るときにだけ実行されるので、作ってあるクラスタの login ノードには、手動で導入します。
新しく作ったクラスタでも、学習用の env ファイルを作る手順 2 は必要です。

ポーラーは、学習用の env ファイル `~/.config/pask/lerobot-openpi.env` が無いあいだ、起動に失敗して再起動を繰り返します。
`~/.bashrc` は systemd のサービスからは読まれないため、ポーラーはこのファイルから学習ジョブの環境変数を読みます。

次の手順は、login ノードに ubuntu ユーザーで SSH して実行します（[デプロイについて](/hyperpod/docs/ja/DEPLOYMENT.md) の「クラスタの初期設定」）。

1. login ノードのロールを確かめます。

    ```bash
    aws sts get-caller-identity --query Arn --output text
    ```

    `SeparateLoginRole` が `true` の場合は、表示されるロールの名前が、Outputs の `HyperPodLoginExecutionRoleARN` のロールの名前と同じであることを確かめます。
    `false` の場合は、`HyperPodClusterExecutionRoleARN` のロールの名前と同じであることを確かめます。

2. `setup.sh` に `--poller-env` を付けて実行し、学習用の env ファイルを作ります。

    ```bash
    cd ~/sample-physical-ai-scaffolding-kit/samples/lerobot-openpi/training
    bash setup.sh --hf-token "hf_xxxxx" --poller-env
    source ~/.bashrc
    ```

    `setup.sh` は、`~/.bashrc` に書く値と同じ値を、展開してから `~/.config/pask/lerobot-openpi.env`（権限 0600）に書きます。
    `--hf-token` などのほかのオプションは、[LeRobot での pi0.5 の学習](/samples/lerobot-openpi/docs/ja/lerobot_pi05_training.md) と同じです。

3. ライフサイクルスクリプトのバケットから、導入のスクリプトとポーラーのソースを取り出して実行します。

    ```bash
    LIFECYCLE_BUCKET="$(aws cloudformation describe-stacks --stack-name PASK \
      --query "Stacks[0].Outputs[?starts_with(OutputKey, 'HyperPodLifeCycleScriptBucketName')].OutputValue | [0]" \
      --output text)"
    WORK_DIR="$(mktemp -d)"
    aws s3 cp "s3://${LIFECYCLE_BUCKET}/install_vams_poller.sh" "${WORK_DIR}/"
    aws s3 cp --recursive "s3://${LIFECYCLE_BUCKET}/vams_poller/" "${WORK_DIR}/vams_poller/"
    sudo bash "${WORK_DIR}/install_vams_poller.sh"
    ```

    `install_vams_poller.sh` は、`vams_poller/` が同じディレクトリにあることを前提にします。
    スクリプトの中の設定値（キューの URL など）は、デプロイのときに埋め込まれています。
    スクリプトは次の処理を行います。

    1. ポーラーのソースを `/opt/pask/vams-poller/` にコピーし、venv を作ってインストールします。
       HyperPod のイメージの `python3` はポーラーが対応する版より古いので、バージョンを固定した uv で CPython 3.14 を `/opt/pask/python/` に入れ、その Python で venv を作ります。uv と CPython は GitHub から取得します。
    2. 設定ファイル `/etc/pask/vams-poller.env` を書きます。
    3. `pask-vams-poller.service` を書き、有効にして再起動します。

    login グループ以外のノードで実行した場合と、`Vams.Enabled` が `true` でない場合は、何もしません。
    何度実行しても結果は同じです。

4. ポーラーが動いていることを確かめます。

    ```bash
    systemctl status pask-vams-poller
    journalctl -u pask-vams-poller -f
    ```

    `systemctl status` が `active (running)` を表示し、`journalctl` にエラーが続いていなければ、導入は終わりです。
    起動に失敗している場合は、`journalctl -u pask-vams-poller` で原因を確かめ、直してから手順 3 をやり直します。

login ノードを作り直したときは、ライフサイクルスクリプトがポーラーを導入します。
学習用の env ファイルは FSx 上のホームディレクトリにあるので、作り直しても残ります。
ライフサイクルスクリプトでの導入に失敗しても、ノードの作成は失敗しません。
新しい login ノードでポーラーが動いていなければ、手順 3 と手順 4 をやり直します。

## データベースとアセットの用意

学習には、VAMS のデータベースを 1 つと、その中のアセットを 2 つ使います。
パイプラインとワークフローも同じデータベースに登録するので、この章を先に済ませます。

1. データベースを作ります。

    ```bash
    DATABASE_ID="<データベースの ID（例 robot-datasets）>"
    vamscli database create -d "${DATABASE_ID}" --description "<説明>"
    ```

    アセットを置くバケットを尋ねられたら、既定のバケットを選びます。

2. 入力のアセットを作り、LeRobot v3.0 のデータセットをアップロードします。

    ```bash
    vamscli assets create -d "${DATABASE_ID}" --name "<データセットの名前>" --description "<説明>"
    vamscli file upload -d "${DATABASE_ID}" -a "<入力のアセットの ID>" --directory <データセットのディレクトリ> --recursive
    ```

    アセットの ID は、`assets create` の出力に表示されます。
    `--directory` には、`meta/info.json` を直下に持つディレクトリを指定します。

3. 学習済みモデルを入れる出力のアセットを作ります。
    中身は空のままにします。

    ```bash
    vamscli assets create -d "${DATABASE_ID}" --name "<モデルの名前>" --description "<説明>"
    ```

入力のアセットは、ルートに LeRobot のデータセットを持つアセットにします。
ルートに `meta/info.json` があり、`meta/`、`data/`、`videos/` などがその下にある形です。
ポーラーはアセットの全体を PASK のデータバケットにコピーし、そのルートを `DATASET_ROOT` として学習ジョブに渡します。
データセットがアセットのサブディレクトリ（例 `lerobot/<データセット名>/`）にあるアセットでは、学習ジョブがデータセットを読めずに失敗します。
アセットにデータセット以外のファイル（変換前の MCAP など）があると、それもコピーされ、FSx の容量を使います。
VAMS のフォルダの印（`/` で終わるキー）とプレビューのファイル（`.previewFile.` を含むキー）は、コピーしません。

VAMS に LeRobot Dataset Curation のパイプラインがある場合は、それで作ったビューのアセットも入力にできます。このパイプラインは VAMS のバージョンによっては含まれません。
ビューのアセットは、ルートに `curation_manifest.json`（`mode` が `view`）を持ち、data と動画を持ちません。
ポーラーは、マニフェストが記録した元のアセットのファイルの版をコピーし、マニフェストの `episodes` にあるエピソードだけで学習します。
ビューを入力にするときは、次の 2 点を守ります。

- ビューと元のアセットを同じデータベースに置きます。ポーラーは、マニフェストが入力のアセットと別のデータベースかバケットを指していると、`PASK.InvalidRequest` で失敗にします。
- 学習が終わるまで、元のアセットと、ビューが記録した版を消しません。記録した版が無いと、ポーラーは `PASK.InvalidRequest` で失敗にします。

ビューの解決の流れは、[VAMS 連携の仕組み](/hyperpod/docs/ja/VAMS_INTEGRATION/ARCHITECTURE.md) の「curation のビューの解決の流れ」にあります。

出力のアセットは、入力とは別のアセットにします。
ポーラーは学習済みのモデルのファイルを、出力のアセットのルートに書き戻します。
出力のアセットを入力と同じにすると、モデルのファイルがデータセットのルートに置かれ、同じアセットで次に学習したときにデータセットとしてコピーされます。

## パイプラインの登録

VAMS への登録には、PASK に同梱する次の定義を使います。

| ファイル | 内容 |
|---|---|
| [hyperpod/vams/pipeline.json](/hyperpod/vams/pipeline.json) | パイプラインの名前、`executionConfig`、`systemConfig` |
| [hyperpod/vams/template.json](/hyperpod/vams/template.json) | 学習パラメータのテンプレート（`configBody`）とタグの定義（`tagSchema`） |
| [hyperpod/vams/workflow.json](/hyperpod/vams/workflow.json) | ワークフローの名前と `systemConfig` |

VamsCLI のコマンドは、ファイルの全体ではなく `executionConfig` や `systemConfig` などの項目ごとに値を受け取ります。
次の手順では、`jq` で各項目を取り出して渡します。

### 定義の内容

パイプラインは、SQS の実行タイプ（`executionType: SQS`）で、TaskToken が返るまで待ちます（`waitForCallback: Enabled`）。
`sqs.queueUrl` はファイルでは空にしてあり、登録のときに Outputs の `VamsVamsQueueUrl` の値を入れます。
空のまま登録すると、VAMS が `sqs.queueUrl is required` で拒否します。

`taskHeartbeatTimeout` は空にします。
値を入れると、ポーラーが止まっているあいだに heartbeat が途絶え、学習が続いていても VAMS のステップが失敗するためです。

`taskTimeout` は `432000` 秒（5 日）にしています。
VAMS は、この時間を過ぎるとステップを失敗にし、ポーラーは次の heartbeat のエラーから中断として学習ジョブを止めます。
heartbeat は `taskTimeout` を延ばさないので、1 回の実行にかかる時間の合計より長くする必要があります。
合計には次の時間が入ります。

| 時間 | 長さの見込み |
|---|---|
| 学習 | 最大 48 時間（`slurm_train.sh` の `#SBATCH --time=48:00:00`）。分位点の再計算もジョブの中で行うので、この時間に入ります |
| データのコピーと FSx への取り込み | 取り込みの待ちの上限は 1 時間（`IngestWaitSeconds`） |
| 書き戻し | チェックポイントの大きさによりますが、1 時間に収まる見込みです |
| 先に動いている実行の終わりを待つ時間 | `MaxConcurrentRuns` が 1 の場合、先の実行 1 つ分の最大 50 時間 |
| Slurm の待ち行列での待ち | worker のノードが空くまでの時間 |

学習、コピー、書き戻しで約 50 時間、先の実行 1 つを待つ時間を足して約 100 時間になり、残りの約 20 時間を Slurm の待ち行列での待ちに充てています。
VAMS の上限は 604800 秒（7 日）です。
同時に 3 つ以上の実行を投入する運用では、待ち時間が延びるので、上限の範囲で値を大きくします。
ただし `taskTimeout` を長くすると、DLQ に入ったメッセージの実行も、VAMS 側でその時間だけ待ち続けます。

`systemConfig` の `inputFileArity: one` と `assetScope` で、入力をアセット 1 つの全体に限ります。
`metadataInputs.assetMetadata` を `true` にするのは、アセットの名前などを MLflow に記録するためです。
`requireTemplate` を `true`、`allowCustomTemplateOverride` を `false` にして、学習パラメータをテンプレートのタグでだけ受け取ります。

テンプレートの `configBody` は、学習パラメータの環境変数名をキーに、タグ（`{{STEPS}}` など）を値にした、フラットな JSON です。
キーの一覧は、ポーラーの許可リスト（[messages.py](/hyperpod/vams-poller/src/pask_vams_poller/messages.py) の `ALLOWED_TRAINING_PARAMS`）から写しました。
タグの既定値は、[slurm_train.sh](/samples/lerobot-openpi/training/slurm_train.sh) の既定値と同じにしています。
許可リストか `slurm_train.sh` の既定値を変えた場合は、`template.json` の `configBody` と `tagSchema` も同じように変えます。
許可リストに無いキーがテンプレートに残っていると、ポーラーがすべての実行を `PASK.InvalidRequest` で失敗にします。

`configBody` では、文字列のタグを引用符の中に書き、整数と真偽値のタグ（`{{STEPS}}`、`{{COMPILE_MODEL}}` など）は引用符無しで書きます。
VAMS は、引用符の無いタグを、タグの型に応じた JSON の整数と真偽値に置換します。
ポーラーは整数でない数値を受け付けないので、数値のタグの型は `number` ではなく `integer` にしています。
どのタグも既定値を持ち、`required` を `true` にしているので、VAMS が作る設定の JSON には常にすべてのキーが入ります。

ワークフローは、アセット全体の入力を許し（`wholeAssetAllowed: true`）、出力のアセットを実行のときに変えられるようにします（`outputTarget.allowOverride: true`）。

### 登録の手順

次のコマンドは、`hyperpod` ディレクトリで、VamsCLI を認証した状態で実行します。
パイプライン、テンプレート、ワークフローは、学習に使うアセットと同じ VAMS のデータベースに登録します。

1. 登録に使う値を変数に入れます。
    `DATABASE_ID` は、「データベースとアセットの用意」の章で作ったデータベースの ID です。

    ```bash
    DATABASE_ID="<VAMS のデータベースの ID>"
    PIPELINE_ID="pask-lerobot-openpi-training"
    WORKFLOW_ID="pask-lerobot-openpi-training"
    TEMPLATE_ID="$(jq -r .templateId vams/template.json)"
    QUEUE_URL="$(aws cloudformation describe-stacks --stack-name PASK \
      --query "Stacks[0].Outputs[?starts_with(OutputKey, 'VamsVamsQueueUrl')].OutputValue | [0]" \
      --output text)"
    ```

    `PIPELINE_ID` と `WORKFLOW_ID` は、英数字、`-`、`_` の 3〜63 文字であれば変えてかまいません。
    VAMS は manifest の `auxTempPrefix` にパイプラインの名前ではなく `PIPELINE_ID` を入れるので、MLflow のタグ `vams.pipeline_name` にはこの値が入ります。

2. パイプラインを登録します。

    ```bash
    vamscli pipeline create -d "${DATABASE_ID}" -p "${PIPELINE_ID}" \
      -n "$(jq -r .pipelineName vams/pipeline.json)" \
      --category "$(jq -r .category vams/pipeline.json)" \
      --description "$(jq -r .description vams/pipeline.json)" \
      --execution-config "$(jq -c --arg url "${QUEUE_URL}" '.executionConfig | .sqs.queueUrl = $url' vams/pipeline.json)" \
      --system-config "$(jq -c .systemConfig vams/pipeline.json)"
    ```

3. テンプレートを登録します。

    ```bash
    vamscli pipeline template create -d "${DATABASE_ID}" -p "${PIPELINE_ID}" -t "${TEMPLATE_ID}" \
      -n "$(jq -r .templateName vams/template.json)" \
      --description "$(jq -r .description vams/template.json)" \
      --config-format json \
      --config-body "$(jq -r .configBody vams/template.json)" \
      --tag-schema "$(jq -c .tagSchema vams/template.json)" \
      --input-instructions "$(jq -r .inputInstructions vams/template.json)" \
      --default
    ```

    `--allow-custom-edit` は付けません。
    付けると、実行のときに設定の JSON を書き換えられるようになります。

4. ワークフローを登録します。

    ```bash
    vamscli workflow create -d "${DATABASE_ID}" -w "${WORKFLOW_ID}" \
      -n "$(jq -r .workflowName vams/workflow.json)" \
      --category "$(jq -r .category vams/workflow.json)" \
      --description "$(jq -r .description vams/workflow.json)" \
      --pipeline "${DATABASE_ID}:${PIPELINE_ID}:${TEMPLATE_ID}" \
      --system-config "$(jq -c .systemConfig vams/workflow.json)"
    ```

5. ワークフローの `systemConfig` を確かめます。

    ```bash
    vamscli workflow get -d "${DATABASE_ID}" -w "${WORKFLOW_ID}" --json-output
    ```

    `systemConfig.assetScope.wholeAssetAllowed` と `systemConfig.outputTarget.allowOverride` が `true` であることを確かめます。
    `systemConfig` を付けずに作ったワークフローでは `wholeAssetAllowed` が `false` になることを確かめています。
    `false` の場合は、次のコマンドで更新します。

    ```bash
    vamscli workflow update -d "${DATABASE_ID}" -w "${WORKFLOW_ID}" \
      --system-config "$(jq -c .systemConfig vams/workflow.json)"
    ```

## 学習の実行

VAMS から学習を実行するときは、入力にデータセットのアセットか curation のビューのアセットの全体を選び、出力に学習済みモデルのアセットを選びます（「データベースとアセットの用意」の章）。
アセットのファイルを 1 つだけ選んだ実行と、フォルダを選んだ実行は、`PASK.InvalidRequest` で失敗します。
ビューを入力にした場合は、学習が終わるまで、元のアセットとビューが記録した版を消さないでください。

VamsCLI で実行する例です。
`templateTags` で、変えたい学習パラメータだけを指定します。
指定しなかったタグは、テンプレートの既定値になります。

```bash
vamscli workflow execute --workflow-database-id "${DATABASE_ID}" -w "${WORKFLOW_ID}" \
  --input-file "${DATABASE_ID}:<入力のアセットの ID>:/" \
  --output-asset-id "<出力のアセットの ID>" \
  --pipeline-parameters "$(jq -cn --arg pipeline "${PIPELINE_ID}" --arg template "${TEMPLATE_ID}" \
    '{($pipeline): {templateId: $template, templateTags: [{key: "STEPS", value: 1000}]}}')"
```

`--input-file` の最後の `/` が、アセットの全体を表します。
VAMS の画面から実行する場合は、入力のアセットの画面の「Execute Workflow」でワークフロー `pask-lerobot-openpi-training` を選び、入力にアセットの全体を、出力に出力のアセットを指定します。

実行の状態は、VAMS の実行の詳細（`vamscli execution details <実行の ID>`）で確かめます。
クラスタでは、学習ジョブは `vams-<pipelineExecutionId>` の名前で Slurm に投入されます。

```bash
squeue --name "vams-<pipelineExecutionId>"
```

同時にデータのコピー以降へ進める実行の数は `MaxConcurrentRuns` までです。
上限を超えた実行は、先の実行の後片付けが終わるまで待ちます。
待っているあいだも heartbeat を送るので、`taskTimeout` の中であれば失敗しません。

VAMS で実行を中断すると、ポーラーは猶予（`AbortGraceSeconds`）を過ぎた後に学習ジョブを `scancel` で止めます。

## 学習の出力

学習の出力（出力のアセットに入るファイル、results の `pask_training.json`、MLflow の記録）と、実行の後の削除は、[VAMS 連携の仕組み](/hyperpod/docs/ja/VAMS_INTEGRATION/ARCHITECTURE.md) の「利用者が指定するものと受け取るもの」と「全体の流れ」にあります。
書き戻しの規則は [VAMS 連携の仕様](/hyperpod/docs/ja/VAMS_INTEGRATION/SPEC.md) の「完了通知と書き戻し」、MLflow のタグの一覧は同じ文書の「MLflow に記録するもの」にあります。

## VAMS 側に期待する運用設定

VAMS の次の設定は、PASK からは変えられません。
VAMS を運用する側で、次のように設定しておきます。

1. ワークフローの `metadataInputs` で、アセットのメタデータ（`assetMetadata`）を有効にします。
    無効にすると、`metadata.json` にアセットの名前、説明、タグが入りません。
    MLflow の run にタグ `vams.input.asset_name` が付かず、dataset の名前が assetId になります。
    同梱の `pipeline.json` と `workflow.json` は有効にしています。
2. アセットバケットのバージョニングを有効にします。
    無効の場合も学習は行いますが、コピーした入力の版を固定できません。
    `input_manifest.json` と `vams_context.json` の `versioned` が `false` になり、`versionId` が `"null"` になります。
    バージョニングの状態は `aws s3api get-bucket-versioning --bucket <バケット名>` で確かめます。
3. パイプラインは、同梱のテンプレートとタグの定義で登録し、`allowCustomTemplateOverride` を `false` にします。
    `true` にすると、実行のときに任意の設定の JSON を渡せるようになります。
    ポーラーは許可リストに無いキーを拒否しますが、その場合の失敗は学習ジョブの投入の前ではなく、VAMS で実行した後にしか分かりません。

## DLQ の確認

ポーラーが受け付けられなかったメッセージは、`MaxReceiveCount`（5 回）受信された後に DLQ に入ります。
DLQ に入るのは、本文から TaskToken を読めないメッセージと、DynamoDB への記録に失敗し続けたメッセージです。
DLQ に入ったメッセージの実行は、PASK から結果が返らないので、VAMS 側では `taskTimeout`（同梱の定義では 5 日）まで待ち続けます。

DLQ のメッセージは、ローカル PC から、DLQ を読む権限のある AWS のクレデンシャルで確かめます。
login ノードのロールには、DLQ の権限がありません。

1. DLQ の URL を取り出します。

    ```bash
    DLQ_URL="$(aws cloudformation describe-stacks --stack-name PASK \
      --query "Stacks[0].Outputs[?starts_with(OutputKey, 'VamsVamsDeadLetterQueueUrl')].OutputValue | [0]" \
      --output text)"
    ```

2. メッセージの数を確かめます。

    ```bash
    aws sqs get-queue-attributes --queue-url "${DLQ_URL}" \
      --attribute-names ApproximateNumberOfMessages
    ```

3. メッセージの本文を確かめます。

    ```bash
    aws sqs receive-message --queue-url "${DLQ_URL}" \
      --max-number-of-messages 10 --visibility-timeout 60 \
      --attribute-names All
    ```

    本文の `body.workflowExecutionId` が、VAMS の実行の ID です。
    受け付けられなかった理由は、その時刻のポーラーのログ（login ノードの `journalctl -u pask-vams-poller`）で確かめます。
    本文には TaskToken が入っています。
    TaskToken と `states:SendTask*` の権限があれば VAMS のステップを終わらせられるので、本文をチケットやチャットに貼らないでください。

4. VAMS の実行を待たせておく必要が無ければ、VAMS で実行を中断し、DLQ のメッセージを削除します。

    ```bash
    vamscli execution abort "<workflowExecutionId>"
    aws sqs delete-message --queue-url "${DLQ_URL}" --receipt-handle "<ReceiptHandle>"
    ```

    `<ReceiptHandle>` は、手順 3 の出力の `ReceiptHandle` の値です。

受け付けた後の実行の状態は、DynamoDB のテーブルの item で確かめられます。

```bash
TABLE_NAME="$(aws cloudformation describe-stacks --stack-name PASK \
  --query "Stacks[0].Outputs[?starts_with(OutputKey, 'VamsVamsTableName')].OutputValue | [0]" \
  --output text)"
aws dynamodb get-item --table-name "${TABLE_NAME}" \
  --key '{"pipelineExecutionId": {"S": "<pipelineExecutionId>"}}'
```

`state` の値の意味は [VAMS 連携の仕様](/hyperpod/docs/ja/VAMS_INTEGRATION/SPEC.md) の「状態遷移」にあります。

## login ノードの権限の注意

login ノードには、ポーラーのほかに、人も SSH で入ります。
login ノードに SSH した人は、ポーラーと同じ権限を使えます。
権限の一覧と、`SeparateLoginRole` による違いは [VAMS 連携の仕組み](/hyperpod/docs/ja/VAMS_INTEGRATION/ARCHITECTURE.md) の「権限とセキュリティ」にあります。
