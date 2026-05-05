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

`infra/lifecycle/` 配下のライフサイクルスクリプトは、HyperPod がノードを初めてプロビジョニングするときに実行されます。スクリプトを編集したり（上流の変更を取り込んだり）しても、**既存のノードは新しいスクリプトを自動的には取り込みません** — HyperPod は既に「初回プロビジョニング」ステップを通過しているためです。

対応したいことは次の 2 つです：

1. **既存のノードに今すぐ新しいスクリプトを適用する。**
2. **今後のノード置換やスケールアウトでも新しいスクリプトが使われるようにする。**

必要に応じてどちらか、あるいは両方を実施してください。

#### 既存ノードでスクリプトをその場で再実行する

`infra/scripts/run-lifecycle.sh` を使います。`infra/lifecycle/` をパッケージして SSM 経由で対象ノードに配信するため、Slurm、SSH、`/fsx` のいずれかが壊れていても動作します。

```bash
# 全ノード（controller、login、すべての worker）でライフサイクル全体を再実行
infra/scripts/run-lifecycle.sh --all

# 実行せずにターゲットだけプレビュー
infra/scripts/run-lifecycle.sh --all --dry-run

# 特定のインスタンスグループのみ
infra/scripts/run-lifecycle.sh --group gpu-workers

# 特定のノードで特定のスクリプトだけ実行（スクリプト編集中の最速イテレーションループ）
infra/scripts/run-lifecycle.sh --node ip-10-0-2-124 --script register_slurm_features.sh
```

各ライフサイクルスクリプトはノードタイプを自動検出し、適用可否を自動的に判定します。「全スクリプトを全ノードで実行」しても安全であり、対象外のノードでは exit 0 で終了し、「skipped」という明示的なメッセージを出力します。また、スクリプトは冪等であるため、再実行時には「インストール済み」のファストパスが適用され、通常の `--all` 実行も数秒で完了します。

ノードごとのログは `/tmp/physai-lifecycle-runs/<timestamp>/` 配下に記録されます。各実行の末尾の要約にそのパスが表示されます。

#### 将来の置換のために S3 も更新する

`run-lifecycle.sh` はローカルの `infra/lifecycle/` のコピーをノードに転送しますが、HyperPod が *新しい* ノードに使う S3 上のコピーは更新しません。変更内容に満足したら、次のコマンドも実行してください：

```bash
npx cdk deploy PhysaiClusterStack   # 新しいハッシュプレフィックスでスクリプトを S3 に再アップロード
```

これにより、ノードが後で置換されたとき（例えば `scontrol update node=<name> state=fail reason="Action:Replace"` を実行した、HyperPod が不健全なノードを自動置換した、スケールアップした等）、新しいノードが更新後のスクリプトでプロビジョニングされます。

#### その場で再実行する代わりにノードを置換する

HyperPod に新しいノードをゼロからプロビジョニングしてもらう方が好みの場合、置換ワークフローも引き続き使えます — ただし新しいノードに変更内容を反映させるため、先に `cdk deploy` が必要です：

```bash
npx cdk deploy PhysaiClusterStack
# login ノードから実行（Slurm 管理者権限が必要）:
scontrol update node=<node-name> state=fail reason="Action:Replace"
```

これは worker と login ノードで動作します。**controller はこの方法では置き換えられません** — その場合は `run-lifecycle.sh --group controller-machine` を使ってライフサイクルスクリプトをその場で再実行してください。

#### 最終手段: `PhysaiClusterStack` の破棄と再デプロイ

クラスターが `run-lifecycle.sh` でもノード置換でも良い状態に戻せないほど深刻に詰まっている場合 — あるいは `infra/lifecycle/` が SSM のペイロード上限を超えて `run-lifecycle.sh` が実行を拒否した場合 — `PhysaiClusterStack` を破棄して再デプロイします：

```bash
npx cdk destroy PhysaiClusterStack     # 約 10 分
npx cdk deploy PhysaiClusterStack      # 約 15 分
```

遅く（合計約 25 分）、実行中のジョブはすべて失われます。しかし安全です： `PhysaiClusterStack` は設計上ステートレス（IAM ロール、HyperPod クラスター、ライフサイクルバケット）で、永続状態をすべて保持する `PhysaiInfraStack`（FSx、RDS、S3 データバケット）は変更されません。

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
