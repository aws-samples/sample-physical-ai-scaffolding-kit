# デプロイ手順

## 前提条件

- 認証情報が設定済みの AWS アカウント（AWS SSO や `~/.aws/config` の管理者プロファイルなど）
- ターゲットリージョンで十分なサービスクォータが確保されていること:
  - HyperPod クラスターおよび使用予定のインスタンスタイプ（controller + login は `ml.c5.large`、GPU のデフォルトは `ml.g6e.2xlarge`、CPU は `ml.m5.2xlarge`）
  - VPC クォータ: スタックは NAT ゲートウェイと複数のサブネットを含む VPC を1つ作成します
- ローカル環境のツール:
  - Node.js 20+ および `npm`（CDK 用）
  - Python 3.12+ および `pip`
  - AWS CLI v2
  - [Session Manager plugin for AWS CLI](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html)
  - `rsync` および `ssh`（macOS/Linux にはプリインストール済み）

## 設定

`physai/infra/cdk.json` の context でサイジングを設定します:

```json
{
  "context": {
    "clusterName": "physai-cluster",
    "fsxCapacityGiB": 1200,
    "gpuWorkers": [
      { "name": "gpu-workers", "instanceType": "ml.g6e.2xlarge", "count": 1 }
    ],
    "cpuWorkerType": "ml.m5.2xlarge",
    "cpuWorkerCount": 1
  }
}
```

`gpuWorkers` はリストで指定します。エントリを追加することで、デフォルトとは異なる GPU タイプを同時に利用できます。各エントリは個別の Slurm インスタンスグループとなり、Slurm の constraint でグループ名を指定してターゲットできます。2種類の GPU を使う例:

```json
"gpuWorkers": [
  { "name": "gpu-workers-l40s", "instanceType": "ml.g6e.2xlarge", "count": 2 },
  { "name": "gpu-workers-h100", "instanceType": "ml.p5.48xlarge", "count": 1 }
]
```

`cpuWorkerType` / `cpuWorkerCount` は単一の CPU インスタンスグループを設定します。controller ノードと login ノードは `ml.c5.large x 1` で固定されており、ここでは変更できません。

### instanceの起動数制限を上限緩和申請とインスタンスの確保

使いたいインスタンスの上限緩和申請が事前に必要となる場合があります。実際に利用するインスタンスの上限が必要と想定されている数に設定されているかを確認し、足りない場合はインスタンスの数を上限緩和申請してください。 **インスタンス数や種類によっては承認まで時間がかかる** 場合があります。

制限の申請は以下の順番で行います。**かならず、利用するAWSアカウントでサインインしいることを確認してください。**

1. <https://console.aws.amazon.com/servicequotas/> にアクセス
1. 左のメニューで `AWS services` を選択
1. `Amazon SageMaker` を検索して、選択
1. `for cluster usage` と検索欄に入力し検索結果に表示される利用したいインスタンスタイプを選択します
1. `Request increase at account level` のボタンを選択
1. `Increase quota value` に値を入力して `Request` をクリックすると反映されます

**注意** これは上限緩和の申請であって、この数が必ず確保されるというものではありません。

## デプロイ

デプロイの完了には約20分かかります。

```bash
cd physai/infra
npm install
npx cdk bootstrap
npx cdk deploy --all --require-approval never
```

**2つの CloudFormation スタックが作成されます:**

| スタック | 内容 | 削除保護 |
|----------|------|----------|
| `PhysaiInfraStack` | VPC、S3 データバケット、FSx、RDS（アカウンティング）、Secrets Manager | ON |
| `PhysaiClusterStack` | HyperPod クラスター、IAM 実行ロール、ライフサイクルスクリプトバケット | OFF |

個別にデプロイする場合:

```bash
npx cdk deploy PhysaiInfraStack
npx cdk deploy PhysaiClusterStack
```

### デプロイされるリソース

```
┌──────────────────────────────────────────────────────────────────────┐
│  PhysaiInfraStack (ステートフル; スタック削除時も保持)                │
│                                                                      │
│   VPC  (10.0.0.0/16, 2 AZs)                                         │
│   ├── Public subnets + NAT gateway + Internet gateway                │
│   ├── Private subnets                                                │
│   └── S3 gateway VPC endpoint                                        │
│                                                                      │
│   S3 data bucket     s3://<clusterName>-data-<account>               │
│   FSx for Lustre     1.2 TB PERSISTENT_2, DRA → s3://.../raw/        │
│   RDS MariaDB        db.t4g.small  (Slurm accounting)                │
│   Secrets Manager    DB credentials                                  │
└──────────────────────────────────────────────────────────────────────┘
          │ Exports VPC / subnets / SG / FSx / RDS / Secret
          ▼
┌──────────────────────────────────────────────────────────────────────┐
│  PhysaiClusterStack (ステートレス; 破棄・再作成可能)                  │
│                                                                      │
│   S3 lifecycle scripts bucket                                        │
│   IAM execution role                                                 │
│   SageMaker HyperPod cluster                                         │
│   ├── controller-machine  ml.c5.large × 1  (Slurm scheduler)        │
│   ├── login-group         ml.c5.large × 1  (SSH entry point)        │
│   ├── gpu-workers         ml.g6e.2xlarge   (configurable)            │
│   └── cpu-workers         ml.m5.2xlarge    (configurable)            │
│   All nodes mount /fsx                                               │
│                                                                      │
│   CloudWatch alarm   FSx FreeStorageCapacity                         │
└──────────────────────────────────────────────────────────────────────┘
```

パイプラインで使用する S3 レイアウト:

```
s3://<clusterName>-data-<account>/
└── raw/        # 生の HDF5 デモデータ。DRA によりアクセス時に /fsx/raw/ へ自動インポートされる。
```

FSx レイアウト（全クラスターノードで `/fsx/` にマウント）:

```
/fsx/
├── raw/            # DRA 経由で s3://.../raw/ から遅延ロード
├── datasets/       # LeRobot datasets
├── checkpoints/    # 学習チェックポイント
├── evaluations/    # 評価出力
├── enroot/         # コンテナ .sqsh イメージ
└── physai/         # CLI の作業領域 (builds, logs, sync directories)
```

### 稼働中のクラスターへのライフサイクルスクリプト変更の適用（上級者向け）

`npx cdk deploy PhysaiClusterStack` を実行すると、`infra/lifecycle/` 配下の編集内容が S3 にアップロードされます。ライフサイクルスクリプトはノードの初回作成時にのみ実行されるため、既存のノードにはアップロードしただけでは反映されません。worker ノードや login ノードに新しいスクリプトを適用するには、ノードを置き換えます:

```bash
# login ノードから実行（Slurm 管理者権限が必要）
scontrol update node=<node-name> state=fail reason="Action:Replace"
```

HyperPod が該当ノードを破棄し、更新されたライフサイクルスクリプトを実行する新しいインスタンスをプロビジョニングします。

controller ノードはこの方法では置き換えられません。controller にライフサイクルスクリプトの変更を適用するには、SSH/SSM で controller に接続し、該当スクリプトを手動で再実行します（スクリプトは冪等であり再実行しても安全です）。それが不可能な場合は、最終手段として `PhysaiClusterStack` を破棄して再デプロイします。

## クラスターへのアクセス

login ノードにはパブリック IP がありません。AWS SSM を経由した SSH トンネルでアクセスします。

```bash
# まず physai/ ディレクトリに移動:
# オプションなしの場合、~/.ssh/id_rsa.pub, id_ed25519.pub, id_ecdsa.pub のいずれかを使用
# オプション: infra/scripts/setup-ssh.sh --key ~/.ssh/mykey.pub --profile myprofile --region us-west-2

infra/scripts/setup-ssh.sh
```

このスクリプトが行うこと:

1. `PhysaiClusterStack` からクラスター名を取得します
2. login ノードのインスタンスを検索します
3. SSM 経由で公開鍵を `/home/ubuntu/.ssh/authorized_keys` にアップロードします
4. `~/.ssh/config` に追加する SSH config スニペットを表示します

表示されたスニペットを `~/.ssh/config` に追加してテストします:

```bash
ssh physai-login
```

初回接続時にホストキーの確認を求められます。ProxyCommand が SSM 経由でトンネルするため、セキュリティグループの変更は不要です。

## 環境の削除

```bash
infra/scripts/cleanup.sh             # コマンドを表示するだけ; 確認してから各コマンドを実行する
```

このスクリプトは、解決済みのリソース ID を含む具体的なコマンドを正しい順序で表示します:

1. `cdk destroy PhysaiClusterStack` -- クラスターの ENI を解放します。
2. FSx、RDS を削除します（先に RDS の削除保護を無効化します）。
3. S3 データバケットを削除します（先にバケットを空にします）。
4. `PhysaiInfraStack` の削除保護を無効化します。
5. `cdk destroy PhysaiInfraStack`。
6. オプション: Secrets Manager のシークレットを即時削除します（回復期間をスキップする場合）。

各コマンドは実行前に内容を確認してください。スクリプト自体は何も実行しません。
