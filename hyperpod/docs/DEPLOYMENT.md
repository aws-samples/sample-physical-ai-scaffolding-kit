# デプロイについて

このサンプルは CDK を利用して AWS のリソースを構築します。CDK でリソースをデプロイする場合は、十分な権限を持つ AWS のクレデンシャルを用意する必要があります。

## 1.環境要件

以下の環境での動作を確認しております。

- Node.js v22.18.0
- npm 10.9.3
- aws-cli 2.32.21
- Python 3.14(Lambda)
- Docker 25.0.8

## 2.デプロイ

cdkでのデプロイには AWS のクレデンシャルやリージョンの指定を以下の環境変数で指定してください。このサンプルはus-east-1, us-west-2で動作確認を行なっています。`AWS_DEFAULT_REGION` には実際に利用するリージョンを指定してます。

```bash
export AWS_ACCESS_KEY_ID=
export AWS_SECRET_ACCESS_KEY=
export AWS_DEFAULT_REGION=us-east-1
```

### 2.1.CDK のセットアップ

cdkがインストールされていない場合は [AWS CDK の開始方法](https://docs.aws.amazon.com/ja_jp/cdk/v2/guide/getting-started.html) を参考にcdkのインストールを行なってください。

### 2.2.サンプルの展開

Githubよりサンプルコードをcloneします。

```bash
git clone git@github.com:aws-samples/sample-physical-ai-scaffolding-kit.git
cd sample-physical-ai-scaffolding-kit/hyperpod
```

### 2.3.関連 node module のインストール

この CDK スタックで必要となるライブラリをインストールします。

```bash
npm install
```

### 2.4.CDK の Bootstrap

cdkのデプロイに必要な環境を準備します。既に同じリージョンで 1度でも実行済みであれば実行不要です。

```bash
cdk bootstrap
```

### 2.5.環境の設定

CDK でデプロイされるアーキテクチャについて幾つかの設定を行うことができます。設定ファイルは [hyperpod/cdk.json](/hyperpod/cdk.json) に存在しており、以下の部分が設定内容になります。
初期設定では worker group の設定は含まれていません。worker groupにはGPUを持つインスタンスを指定することが多いことから、初回デプロイでは設定せずにデプロイすることで、インスタンスの割り当てができない場合に全体が失敗することを防ぐことができます。後の手順で worker group のデプロイを行います。

変更する場合、`vim cdk.json` と入力し、コマンドラインのエディタを利用して書き換えてください。

```json
"config": {
  "StackName": "PASK",  // CloudFormationのStack名を変える場合は変更してください
  "Cluster": {
    "Name": "pask-cluster",  // Amazon SageMaker HyperPodのクラスタ名
    "ControllerGroup": {
      "Name": "controller-group",
      "Count": 1,
      "InstanceType": "ml.c5.large"
    },
    "LoginGroup": {
      "Name": "login-group",
      "Count": 1,
      "InstanceType": "ml.c5.large"
    },
    "WorkerGroup": []
  },
  "ClusterVPC": {
    "SubnetAZ": "",  // 事前に特定のAZを指定する場合に入力
    "UseFlowLog": false  // VPC Flow logsを有効にしたい場合は trueに設定
  },
  "Lustre": {
    "S3Prefix": "",  // Lustreのリンク先S3バケットのpathを指定する場合に指定
    "FileSystemPath": "/s3link"  // node上のパスは `/fsx/ここで指定した値` になります
  }
}
```

#### 2.5.1.クラスターのAZの指定について

デフォルトでは、g5,g6,g6e,g7e が利用できるAZが自動的に選ばれます。

[AZ選択用のカスタムリソース](/hyperpod/lib/lambda/custom-resources/subnet-selector/index.py)

その他の特定のインスタンスタイプを使う場合は、以下のコマンドで調べたAZを cdk.json の ClusterVPC.SubnetAZ に指定してください。

```bash
aws ec2 describe-instance-type-offerings --location-type availability-zone --filters Name=instance-type,Values=p4d.24xlarge
```

### 2.6.スタックのデプロイ

以下のコマンドを実行して、環境を構築します。途中で権限の確認がありますので `y` と入力して進めます。このコマンドですべての環境を一気に作成するため、完了まで数十分かかります。

```sh
cdk deploy
```

デプロイが成功すると以下の様な出力が確認できます。この出力結果はAWSのマネージメントコンソールでCloudFormationを開くと `PASK` というスタックがありますので、スタックの `Outputs` タブで確認できます

```bash
Outputs:
PASK.BucketDataBucketName = データを保存するS3バケット名
PASK.ClusterAZNameParameterStore = クラスタがデプロイされているAZ名が保存されているパラメータストア名
PASK.ClusterClusterAvailabilityZone = クラスタがデプロイされているAZ名
PASK.HyperPodClusterExecutionRoleARN = クラスタのnodeのRole arn
PASK.HyperPodClusterId = クラスタのID
PASK.HyperPodClusterSecurityGroup = クラスタで利用されるセキュリティーグループ
PASK.HyperPodClusterSubnet = クラスタがデプロイされたサブネットID
PASK.HyperPodLifeCycleScriptBucketName = ライフサイクルスクリプトの保存先S3バケット
PASK.HyperPodLoginGroupName = クラスタのログイングループ名
PASK.Region = リソースがデプロイされているリージョン
```

クラスタ上でこの情報を見たい場合は、以下のコマンドで取得することが可能です。(リージョンとStack名は実際に利用したたいを指定してください)

```bash
export AWS_DEFAULT_REGION=us-east-1
aws cloudformation describe-stacks --stack-name PASK --query "Stacks[0].Outputs"
```

これで、一通りのリソースが準備できました。

### 2.7.クラスタの初期設定

デプロイでHyperPod上にクラスタが準備できました。この手順では、クラスタにsshするための手順を説明します。

まずは、[sshのセットアップスクリプト](https://docs.aws.amazon.com/ja_jp/sagemaker/latest/dg/sagemaker-hyperpod-run-jobs-slurm-access-nodes.html)を利用して、ローカル PC から ssh コマンドでアクセスできるようにセットアップします。

```bash
wget https://raw.githubusercontent.com/awslabs/awsome-distributed-training/refs/heads/main/1.architectures/5.sagemaker-hyperpod/easy-ssh.sh -O easy-ssh.sh
chmod +x easy-ssh.sh
```

**注意** `easy-ssh.sh` を実行するにはAWSのクレデンシャルを指定する必要があります。

途中で `~/.ssh/config` に追加するか、ssh用の鍵を作成するか聞いてきますので `yes` で回答すると次回からのログインが簡単になります。

`easy-ssh.sh` は引数として `PASK.HyperPodLoginGroupNameの値` と `PASK.HyperPodClusterIdの値` を指定します。デフォルトの設定でデプロイした場合、コマンドは以下となります。

```bash
./easy-ssh.sh -c login-group pask-cluster
```

sshが成功すると以下のような出力がされます。

```bash
Now you can run:

$ ssh pask-cluster

Starting session with SessionId: xxxxxxxx
#
```

これは、login nodeにrootでsshした状態ですので、 `exit` で一度抜けてください。

抜けたら、コマンドの実行中に表示された `ssh pask-cluster` コマンドでsshを行います。

**注意** `easy-ssh.sh` で `~/.ssh/config` に追加されるsshの設定は以下のようにProxyCommandを利用し、[AWS Systems Manager Session Manager](https://docs.aws.amazon.com/ja_jp/systems-manager/latest/userguide/session-manager.html)を使ってsshを行います。そのため **sshする際にはAWSのクレデンシャルを指定する必要があります** のでご注意ください。

```bash
Host pask-cluster
    User ubuntu
    ProxyCommand sh -c "aws ssm start-session --target sagemaker-cluster:xxxxxxx_controller-group-i-xxxxxxxx --document-name AWS-StartSSHSession --parameters 'portNumber=%p'"
```

Session Managerを使ったセッションは何も操作がない場合20分(デフォルト)でセッションが切れます。タイムアウトの時間を伸ばしたい場合は、[AWS Systems Managerのドキュメント](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-preferences-timeout.html)を参考に設定を変更してください。

正常にsshができると以下の様にlogin nodeにubuntuユーザーとして入ることができます。

```bash

〜〜省略〜〜

You're on the login
Controller Node IP: 10.1.xxx.xxx
Login Node IP: 10.1.xxx.xxx
Instance Type: ml.c5.large
ubuntu@ip-10-1-155-217:~$
```

LustreのストレージにリンクされているS3バケットのディレクトリの権限をubuntuユーザーでも書き込める様にします。

```bash
sudo chmod -R 777 /fsx/s3link
```

試しにファイルを書き込んでみて、S3に反映されることを確認します。

```bash
touch /fsx/s3link/test.txt
```

[マネージメントコンソール](https://console.aws.amazon.com/s3/buckets) を開き、`pask-bucketdata` で始まるバケットを選択し、先ほど作成したファイルが存在することを確認してください。

確認ができたら、login nodeから `exit` で抜けます。

以降の作業はローカルPCのターミナルでで行います。

### 2.8.worker groupを追加

worker groupを追加する場合は、cdk.jsonを編集し、再度デプロイする必要があります。
追加のworker groupを用意する場合も、以下の手順を進めてください。

#### instanceの起動数制限を上限緩和申請とインスタンスの確保

使いたいインスタンスの上限緩和申請が事前に必要となる場合があります。実際に利用するインスタンスの上限が必要と想定されている数に設定されているかを確認し、足りない場合はインスタンスの数を上限緩和申請してください。 **インスタンス数や種類によっては承認まで時間がかかる** 場合があります。

制限の申請は以下の順番で行います。**かならず、利用するAWSアカウントでサインインしいることを確認してください。**

1. <https://console.aws.amazon.com/servicequotas/> にアクセス
1. 左のメニューで `AWS services` を選択
1. `Amazon SageMaker` を検索して、選択
1. `for cluster usage` と検索欄に入力し検索結果に表示される利用したいインスタンスタイプを選択します
1. `Request increase at account level` のボタンを選択
1. `Increase quota value` に値を入力して `Request` をクリックすると反映されます

**注意** これは上限緩和の申請であって、この数が必ず確保されるというものではありません。

worker groupで利用するインスタンスの確保ができない場合はデプロイに失敗します。特にGPUを利用するインスタンスタイプでは、[Amazon SageMaker HyperPod flexible training plans](https://aws.amazon.com/jp/blogs/news/meet-your-training-timelines-and-budgets-with-new-amazon-sagemaker-hyperpod-flexible-training-plans/) を利用して、使いたいインスタンスを確保する必要があります。確保する方法は [Amazon SageMaker HyperPod flexible training plans](https://aws.amazon.com/jp/blogs/aws/meet-your-training-timelines-and-budgets-with-new-amazon-sagemaker-hyperpod-flexible-training-plans/) に書かれている手順を参考に進めてください。

**注意** Training plans では誤って購入した場合キャンセルができません。TargetやInstance Typeなどに間違いがない様に十分注意してください。

**Amazon SageMaker HyperPod flexible training plans** を利用する場合は上限緩和のページで `reserved capacity across training plans per Region` を検索し、利用するインスタンスが該当する場合、合わせて上限の緩和を行ってください。

### 2.9.cdk デプロイして worker groupを作成

`cdk.json` に以下のようにworker groupの設定を追加します。実際に利用するインスタンスタイプを指定してください。また、複数のworker groupを追加する場合は、複数追加してください。

```json
"Cluster": {
  "Name": "pask-cluster",
  "ControllerGroup": {
    "Name": "controller-group",
    "Count": 1,
    "InstanceType": "ml.c5.large"
  },
  "LoginGroup": {
    "Name": "login-group",
    "Count": 1,
    "InstanceType": "ml.c5.large"
  },
  "WorkerGroup": [
    {
      "Name": "worker-group-1",
      "Count": 1,
      "InstanceType": "ml.g6e.2xlarge"
    }
  ]
},
```

cdk.json の編集が終わったら、以下のコマンドで再度デプロイを行います。

```bash
cdk deploy
```

デプロイが成功すると worker node が利用できるようになります。

デプロイが失敗し、以下の様なエラーが表示されあ場合は、インスタンスの確保ができなかったことを意味します。しばらくしてから再度デプロイするか、インスタンスを確保してから実行してください。

```bash
❌  PASK failed: ToolkitError: The stack named PASK failed to deploy: UPDATE_ROLLBACK_COMPLETE: Resource handler returned message: "Resource of type 'AWS::SageMaker::Cluster' with identifier 'Operation [UPDATE] on [arn:aws:sagemaker:us-east-1:0000000000:cluster/xxxxxxx] failed with status [InService] and error [We currently do not have sufficient capacity to launch new ml.g6e.2xlarge instances. Please try again.]' did not stabilize." (RequestToken: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxx, HandlerErrorCode: NotStabilized)
```

---

以上でAmazon SageMaker HyperPodの基本的な環境の準備ができました。
作成されたHyperPodのクラスタをマネージメントコンソールで確認する場合は、 [https://console.aws.amazon.com/sagemaker/home#/cluster-management](https://console.aws.amazon.com/sagemaker/home#/cluster-management) にアクセスしてください。(表示されない場合は、デプロイしたリージョンを選択してください)

HyperPod 上の node は共通の Lustre ストレージを `/fsx` にマウントしています。デフォルトのユーザー ubuntu のホームディレクトリは Lustre 上の `/fsx/ubuntu` が設定されます。Lustre 上の `/fsx/s3link` は `PASK.BucketDataBucketName` に表示された [S3バケットにリンク](https://docs.aws.amazon.com/ja_jp/fsx/latest/LustreGuide/create-dra-linked-data-repo.html)されていますので、大量のデータはこのS3バケットにアップロードしてクラスタから利用すると、各ノードはこの Lustre を通して S3 のデータにアクセスできますので、データの利用が簡単になります。
