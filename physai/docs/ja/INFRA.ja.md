# インフラストラクチャ (CDK)

2つの CDK スタック (TypeScript) を使用して、physai プラットフォームを AWS 上にデプロイします。

## スタック概要

| スタック | 用途 | ステートフル | 削除保護 |
|-------|---------|----------|----------------------|
| **PhysaiInfraStack** | ネットワーク、ストレージ、データベース | はい | ON |
| **PhysaiClusterStack** | HyperPod クラスター、IAM、モニタリング | いいえ | OFF |

PhysaiInfraStack のリソースは長期的に保持され、クラスターの再構築をまたいで維持されます。PhysaiClusterStack はデータを失うことなく安全に削除・再作成できます。

## PhysaiInfraStack

### VPC

- CIDR: `10.0.0.0/16`
- 2 AZ (`maxAzs: 2`)、各 AZ にパブリックサブネットとプライベートサブネットを1つずつ配置します。HyperPod と FSx は単一 AZ (`privateSubnets[0]`) で稼働します。2つ目の AZ は RDS の DB サブネットグループが少なくとも2つの AZ にサブネットを必要とするためにのみ存在します。RDS 自体はシングル AZ (`multiAz: false`) です。
- インターネットゲートウェイ + NAT ゲートウェイ 1台 (プライベートサブネットのアウトバウンド用)
- セキュリティグループ: クラスターと FSx 間の通信のため、自己参照インバウンド (全プロトコル)
- プライベートルートテーブル上に S3 ゲートウェイ VPC エンドポイント

### S3

- **データバケット** (`<clusterName>-data-<account>-<region>`): 生データ、データセット、チェックポイント、結果の永続ストレージ

### FSx for Lustre

- デフォルト 1.2 TB (`fsxCapacityGiB` で設定可能)、PERSISTENT_2、SSD、125 MB/s/TiB スループット
- プライベートサブネットにデプロイされます
- Data Repository Association: `s3://<data-bucket>/raw/` から `/fsx/raw/` への自動インポート

### RDS (Slurm アカウンティング)

- `db.t4g.small` 上の MariaDB、シングル AZ、gp3 ストレージ
- プライベートサブネットグループ (パブリックアクセスなし)
- セキュリティグループ: クラスターセキュリティグループからの TCP 3306 インバウンドのみ
- データベース名: `slurm_acct_db`
- 認証情報は Secrets Manager に保存されます (パスワード自動生成、ローテーションはデフォルトで無効)
- HyperPod コントローラー上の `slurmdbd` が `sacct` ジョブ履歴のために使用します

### PhysaiClusterStack へのエクスポート

- VPC ID、プライベートサブネット ID、セキュリティグループ ID
- FSx DNS 名、マウント名
- データバケット名 / ARN
- RDS エンドポイント
- Secrets Manager シークレット ARN (DB パスワード用)

### 出力値

- `DataBucketName` — S3 データバケットです。ユーザーが S3 経由で生データをアップロードする際に参照します ([`PHYSAI_CLI.ja.md` の「生データと S3 自動インポート」](PHYSAI_CLI.ja.md#生データと-s3-自動インポート)を参照)。`${stackName}-DataBucketName` としてエクスポートされます。

## PhysaiClusterStack

PhysaiInfraStack に依存します。

### ライフサイクルスクリプト

- **ライフサイクルスクリプトバケット** (`<clusterName>-lifecycle-<account>-<region>`): `infra/lifecycle/` から `BucketDeployment` で配置されます
- CDK はデプロイ時に `physai-config.json` を生成し (PhysaiInfraStack からの RDS エンドポイント、Secrets Manager ARN を含む)、スクリプトと一緒にデプロイします
- HyperPod はノードプロビジョニング時にこのバケットからスクリプトをダウンロードします

### IAM

- **実行ロール** (`<clusterName>-ExecutionRole`): `sagemaker.amazonaws.com` が引き受けます
  - `AmazonSageMakerClusterInstanceRolePolicy` (マネージドポリシー)
  - EC2 ネットワーク権限 (ネットワークインターフェースの作成/削除)
  - データバケットおよびライフサイクルスクリプトバケットへの S3 アクセス
  - FSx describe
  - RDS シークレットの Secrets Manager 読み取り (コントローラーが起動時に DB パスワードを取得します)

### HyperPod クラスター

- オーケストレーター: `Slurm`、`SlurmConfigStrategy: Merge`
- **クラスター名**: `<baseClusterName>-<stackGuid8>`。`baseClusterName` は `cdk.json` context から取得します (デフォルト `physai-cluster`)、`stackGuid8` は PhysaiClusterStack の CloudFormation スタック GUID の先頭8文字です。GUID はスタック更新をまたいで安定していますが、削除+再デプロイ時に変更されます。そのため新規デプロイごとに Slurm アカウンティング上で新しい `ClusterName` が割り当てられ、ジョブ ID は1から始まり、`sacct` のデフォルト表示がクリーンになります。旧クラスターのアカウンティング履歴は以下で参照できます:

    ```bash
    sacctmgr list clusters
    sacct --clusters=<old-cluster-name>   # or --clusters=all
    ```

- 固定インスタンスグループ:
  - `controller-machine`: ml.c5.large x 1、NodeType: Controller
  - `login-group`: ml.c5.large x 1、NodeType: Login
- 設定可能なインスタンスグループ (CDK context で指定):
  - GPU ワーカー: 各グループに NodeType: Compute、PartitionNames: ["gpu"]
  - CPU ワーカー: NodeType: Compute、PartitionNames: ["cpu"]
- 全グループが `FsxLustreConfig` で FSx を `/fsx` にマウントします
- 全グループがライフサイクルスクリプトの S3 URI を使用します

### CloudWatch

- FSx の `FreeStorageCapacity` がしきい値を下回った場合のアラーム

### 出力値

- `ClusterName` — HyperPod クラスター名 (スタック GUID サフィックス付き) です。`setup-ssh.sh` やその他のツールがクラスターを特定するために使用します。`${stackName}-ClusterName` としてエクスポートされます。

## 設定

`cdk.json` context:

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

`gpuWorkers` を調整して GPU インスタンスグループを追加・削除できます。例:

```json
// 単一 GPU タイプ
"gpuWorkers": [
  { "name": "gpu-workers", "instanceType": "ml.g6e.2xlarge", "count": 2 }
]

// 複数 GPU タイプの混在
"gpuWorkers": [
  { "name": "gpu-workers-l40s", "instanceType": "ml.g6e.2xlarge", "count": 1 },
  { "name": "gpu-workers-a10g", "instanceType": "ml.g5.2xlarge", "count": 1 }
]

// 大規模学習
"gpuWorkers": [
  { "name": "gpu-workers-h100", "instanceType": "ml.p5.48xlarge", "count": 4 }
]
```

コントローラーおよびログインノードは ml.c5.large x 1 に固定されています。

## ライフサイクルスクリプト

`infra/lifecycle/` ディレクトリが `BucketDeployment` で S3 にデプロイされます。スクリプトはノードプロビジョニング時に実行されます。

ライフサイクルスクリプトはすべて `_lib.sh` を source します。`_lib.sh` は `/opt/ml/config/resource_config.json` からノードタイプを自動検出し（`$NODE_TYPE` を `controller` / `login` / `compute` に設定）、特定のノードタイプだけに適用するスクリプト向けに `require_node_type <type>` ガードを公開します。オーケストレーターはすべてのスクリプトをすべてのノードで実行し、各スクリプトが自身で適用可否を判定します。

| スクリプト | 実行対象 | 用途 |
|--------|---------|---------|
| `on_create.sh` | 全ノード | エントリポイント、`lifecycle_script.py` を呼び出します |
| `lifecycle_script.py` | 全ノード | オーケストレーター: ライフサイクルスクリプトを順番に実行します（各スクリプトがノードタイプに基づき自身で適用可否を判定） |
| `_lib.sh` | 全ノード | 共有ヘルパー: ノードタイプ検出と `require_node_type` ガード |
| `create_fsx_dirs.sh` | コントローラー | `/fsx/{raw,datasets,checkpoints,evaluations,physai}` ディレクトリを作成します |
| `start_slurm.sh` | 全ノード | `slurmctld`（コントローラー）または `slurmd`（compute/login）を起動し、他方は無効化します |
| `configure_slurm_accounting.sh` | コントローラー | `sacct` 用の `slurmdbd` + RDS 接続を設定します |
| `install_docker.sh` | 全ノード | NVMe 上に containerd を使用した Docker をインストールします |
| `install_enroot_pyxis.sh` | 全ノード | Enroot + Pyxis + Vulkan ICD フック + NGX パッチをインストールします |
| `configure_slurm_cgroup.sh` | コントローラー + Compute | `scancel` 用の cgroup プロセストラッキングを有効化します |
| `register_slurm_features.sh` | Compute | ノードの Slurm `Feature`（例: `l40s`）を `scontrol update` で自己登録する systemd `.service` + `.path` ユニットをインストールします。`.path` ユニットは `/var/spool/slurmd/conf-cache/slurm.conf`（configless モードで `scontrol reconfigure` ごとに slurmd が書き直す）を監視するため、features は reconfigure の後に再適用されます — `scontrol update` で設定した features は slurmctld のメモリ上で reconfigure を生き延びないため必要です。 |
| `install_xorg.sh` | Compute (GPU のみ) | IsaacSim ヘッドレスレンダリング用 Xorg をインストールします |

### ライフサイクルスクリプトの作成: HyperPod のタイミング上の注意点

compute ノードから `slurmctld` とやり取りするライフサイクルスクリプトを書く際には、HyperPod の挙動の非対称性に注意が必要です。

`slurmctld` は `slurm.conf` に `NodeName=...` 行として現れるノードのみを認識します。これらの行はコントローラー上で HyperPod が管理・書き込みますが、そのタイミングはクラスター作成時とインスタンスグループ追加時で異なります:

- **クラスター初回作成時.** 各ワーカーの `NodeName` 行は、そのワーカーのライフサイクルスクリプトが実行される前に書き込まれます。compute ノードのスクリプトはすぐに `scontrol update NodeName=<self> ...` を発行でき、コントローラーはそのノードを認識します。
- **UpdateCluster（稼働中のクラスターへの新しいインスタンスグループの追加）.** 新しいノードはまずブートし、ライフサイクルスクリプトを*先に*実行します。対応する `NodeName` 行は、ノードが `InService` に到達した数分*後*に HyperPod がコントローラー上で書き込みます。ライフサイクル実行中は `slurmctld` はそのノードの存在を知らず、そのノードを参照する `scontrol update` は拒否されます。

実務上のガイドライン:

1. **これでライフサイクルを失敗させない.** `slurmctld` にノードを認識させる必要のある compute ノードのスクリプトは、まず操作を試みるべきですが、短いリトライ期間内に成功しなかった場合は警告をログに出して `exit 0` してください。ライフサイクルを失敗させるとノードが失敗し、CloudFormation のロールバックが発生します。
2. **`slurm.conf` の書き込みを最終的整合性のシグナルとして使う.** configless モードでは、`slurmd` は `scontrol reconfigure` のたびに `/var/spool/slurmd/conf-cache/slurm.conf` を書き直します — HyperPod が新しい `NodeName` 行を書いた後に発行する reconfigure も含みます。このファイルを監視する systemd `.path` ユニットは、ノードが最終的に `slurmctld` に認識された時点で操作をリトライするための信頼できるトリガーになります。

`register_slurm_features.sh` はこの両方のルールに従っています — 同様に slurmctld に依存する手順を追加するときはこのパターンをコピーしてください。

### Slurm アカウンティングのセットアップ

`configure_slurm_accounting.sh` はコントローラー上で実行されます:

1. `physai-config.json` から RDS エンドポイントと Secrets Manager シークレット ARN を読み取ります (ライフサイクルスクリプトと一緒にデプロイされ、シークレットでない値のみを含みます)
2. AWS CLI で Secrets Manager から DB パスワードを取得します (パスワードはメモリ上のみで保持され、平文でディスクに保存されることはありません)
3. `/opt/slurm/etc/slurmdbd.conf` を書き込みます (chmod 600)
4. `slurm.conf` にアカウンティング設定を追記します (冪等)
5. `slurmdbd` を起動し、`scontrol reconfigure` を実行します
6. `sacctmgr` でクラスターを登録します

CDK はデプロイ時に `physai-config.json` を生成し、`BucketDeployment` でライフサイクルスクリプトバケットにアップロードします。このファイルには RDS エンドポイントとシークレット ARN が含まれますが、DB パスワードは AWS Well-Architected セキュリティベストプラクティスに従い、実行時に常に Secrets Manager から取得されます。

## デプロイ

```bash
cd infra
npm install
npx cdk bootstrap   # 初回のみ
npx cdk deploy --all
```

ワーカーノードおよびログインノードのライフサイクルスクリプトを既存ノードで**置換せずに**更新するには、`infra/scripts/run-lifecycle.sh` を使います:

```bash
# 全クラスターノードでライフサイクル全体を再実行
infra/scripts/run-lifecycle.sh --all

# 特定のノードタイプのみ
infra/scripts/run-lifecycle.sh --group gpu-workers

# 特定のノードで特定のスクリプトだけ実行
infra/scripts/run-lifecycle.sh --node ip-10-0-2-124 --script register_slurm_features.sh

# ドライラン
infra/scripts/run-lifecycle.sh --all --dry-run
```

このスクリプトは `infra/lifecycle/` を base64 tarball にパッケージして SSM（`AWS-StartNonInteractiveCommand`）で各ノードに配信するため、SSH、Slurm、`/fsx` のいずれかが壊れていても動作します。各ライフサイクルスクリプトはノードタイプに基づき自身で適用可否を判定するため、「全スクリプトを全ノードで実行」しても安全です — 適用外のノードでは exit 0 で終了し、「skipped」という明示的なメッセージを出力します。ノードごとのログは `/tmp/physai-lifecycle-runs/<timestamp>/` 配下に記録されます。

フル再デプロイを行う場合（例: 新しく置換されたノードが取り込めるよう S3 にライフサイクルスクリプトをアップロードしたい場合）:

```bash
npx cdk deploy PhysaiClusterStack   # スクリプトを新しいハッシュプレフィックスで S3 に再アップロードし、UpdateCluster を呼び出す
# その後、クラスター上で更新対象の各 compute/login ノードに対して:
# scontrol update node=<node> state=fail reason="Action:Replace"
```

ライフサイクルスクリプトはコンテンツハッシュ付きプレフィックス (例: `s3://bucket/lifecycle/<hash>/`) で S3 にデプロイされるため、スクリプトに変更があると `SourceS3Uri` が変わり、CloudFormation が `UpdateCluster` を呼び出します。これがないと、置換されたノードは以前のキャッシュ済みスクリプトを取得してしまいます。HyperPod はノード置換だけでは S3 から再取得を行いません。

**注意**: コントローラーノードは `scontrol update ... state=fail` で置換できません。コントローラー上でライフサイクルをその場で再実行するには `run-lifecycle.sh --node <controller-hostname>` を使用します。
