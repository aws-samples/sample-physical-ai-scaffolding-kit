# Workstation Image Builder

日本語 | [English](/workstation-imagebuilder/README.md)

EC2 Image Builder でカスタム AMI を作成し、Lambda 経由でワークステーションを起動する汎用基盤です。ディレクトリにコンポーネント YAML を配置するだけで独自の AMI を定義できます。

同梱の `isaacsim6.0` / `isaacsim5.1` イメージ定義を使えば、NVIDIA Isaac Sim + DCV のワークステーションをすぐにデプロイできます。

## アーキテクチャ

```
┌─────────────────────────────────────────────────────────┐
│ CDK がデプロイするもの                                       │
├─────────────────────────────────────────────────────────┤
│ ImageBuilder スタック                                     │
│   Image Builder パイプライン × N (ImageNames の数だけ)       │
│                                                         │
│ WorkstationEnv スタック                                   │
│   VPC / Security Group / S3 Files                        │
│   Launch Lambda / Connect Lambda                         │
└─────────────────────────────────────────────────────────┘
         │
         ▼ Lambda invoke
┌─────────────────────────────────────────────────────────┐
│ EC2 インスタンス (手動ライフサイクル管理)                      │
│ - カスタム AMI                                           │
│ - Elastic IP 自動割当                                     │
│ - S3 Files マウント (UserData)                            │
└─────────────────────────────────────────────────────────┘
```

## 同梱イメージ定義

| イメージ名 | 内容 |
|-----------|------|
| [`isaacsim6.0`](lib/imagebuilder/isaacsim6.0/) | Isaac Sim 6.0.1 + Isaac Lab v3.0.0-beta2.patch1 + DCV + ROS2 Jazzy |
| [`isaacsim5.1`](lib/imagebuilder/isaacsim5.1/) | Isaac Sim 5.1.0 + Isaac Lab release/2.3.0 + DCV + ROS2 Jazzy |

Isaac Sim / Isaac Lab の起動方法や ROS2 連携については [docs/IsaacSim.ja.md](docs/IsaacSim.ja.md) を参照してください。

## 前提条件

- Node.js v20+
- AWS CLI 設定済み（AdministratorAccess 推奨）
- CDK Bootstrap 済み (`npx cdk bootstrap`)
- NICE DCV クライアントインストール済み（接続用）

## デプロイ手順

### 1. インフラのデプロイ

```bash
cd workstation-imagebuilder
npm install
npx cdk deploy --all --require-approval never
```

これにより以下がデプロイされます:

- `ImageBuilder` — AMI ビルドパイプライン（`ImageNames` で指定したイメージ分）
- `WorkstationEnv` — VPC、S3 Files、Launch Lambda、Connect Lambda

### 2. AMI のビルド

初回デプロイ時およびコンポーネント変更時に Image Builder パイプラインが自動実行されます。AMI ビルドには 30〜60 分程度かかります。

ビルド完了後、AMI ID は SSM Parameter Store (`/workstation-imagebuilder/ami-id/<image-name>`) に自動登録されます。手動操作は不要です。

ビルド状況は AWS コンソールの EC2 Image Builder > Image pipelines で確認できます。

### 3. ワークステーションの起動

Launch Lambda を呼び出してインスタンスを起動します:

```bash
aws lambda invoke \
  --function-name <WorkstationEnv.LaunchFunctionName> \
  --payload '{"imageName":"isaacsim6.0","instanceName":"my-workstation","instanceType":"g6e.4xlarge","diskSizeGb":200,"subnetId":"subnet-xxxxxxxx"}' \
  --cli-binary-format raw-in-base64-out \
  /dev/stdout
```

#### Launch Lambda パラメータ

| パラメータ | 必須 | デフォルト | 説明 |
|-----------|------|-----------|------|
| `imageName` | Yes | — | イメージ名（`cdk.json` の `ImageNames` に対応） |
| `instanceName` | Yes | — | インスタンスの Name タグ |
| `instanceType` | No | `g6e.4xlarge` | インスタンスタイプ |
| `diskSizeGb` | No | AMI のデフォルト値 | ルートボリュームサイズ (GiB)。指定時は AMI のデフォルトを上書き |
| `subnetId` | No | 自動選択 | 起動するサブネット |

#### Launch Lambda レスポンス

```json
{
  "instanceId": "i-0abc123def456789",
  "publicIp": "54.x.x.x",
  "waitCommand": "aws ec2 wait instance-status-ok --instance-ids i-0abc123def456789 --region ap-northeast-1",
  "connectCommand": "aws lambda invoke --function-name <connect-fn> --payload '{\"instanceId\":\"i-0abc123def456789\"}' ..."
}
```

### 4. インスタンスへの接続

DCV セッショントークンによる認証を使用します。パスワードの設定は不要です。

```bash
# 1. Status Check 完了を待つ（起動後 3〜5 分）
aws ec2 wait instance-status-ok --instance-ids <instanceId>

# 2. Connect Lambda で DCV 接続 URL を取得
aws lambda invoke \
  --function-name <WorkstationEnv.ConnectFunctionName> \
  --payload '{"instanceId":"<instanceId>"}' \
  --cli-binary-format raw-in-base64-out \
  /dev/stdout
```

#### Connect Lambda レスポンス

```json
{
  "instanceId": "i-0abc123def456789",
  "publicIp": "54.x.x.x",
  "dcvWebUrl": "https://54.x.x.x:8443/#console?authToken=<token>",
  "dcvAppUrl": "dcv://54.x.x.x:8443/console#<token>",
  "ssmCommand": "aws ssm start-session --target i-0abc123def456789"
}
```

ブラウザで `dcvWebUrl` を開くだけで接続できます（ユーザー名・パスワード入力不要）。

トークンは発行から 10 分以内に接続を開始する必要があります。接続後のセッションには時間制限はありません。期限切れの場合は Connect Lambda を再実行してください。

#### 再接続

DCV クライアントを閉じた場合でも、サーバー側のセッションは維持されます（開いているウィンドウやプロセスはそのまま残ります）。再接続するには Connect Lambda を再実行して新しいトークンを取得し、URL を開いてください。

## 設定

`cdk.json` の `config` セクション:

| パラメータ | デフォルト | 説明 |
|-----------|-----------|------|
| `ImageNames` | `["isaacsim6.0", "isaacsim5.1"]` | ビルドするイメージ名の配列 |
| `VpcId` | `""` (新規作成) | 既存 VPC を使う場合に指定 |
| `DefaultInstanceType` | `g6e.4xlarge` | Launch Lambda のデフォルトインスタンスタイプ |

## 独自イメージの追加方法

独自の AMI を作成するには、以下の手順でイメージ定義ディレクトリを追加します。

### 1. ディレクトリの作成

`lib/imagebuilder/` 配下に任意の名前でディレクトリを作成します:

```bash
mkdir lib/imagebuilder/my-custom-image
```

### 2. config.json の作成

```json
{
  "baseImage": "ubuntu-server-24-lts-x86",
  "buildInstanceType": "m5.xlarge",
  "diskSizeGb": 50,
  "parameters": {}
}
```

| フィールド | 説明 |
|-----------|------|
| `baseImage` | EC2 Image Builder のベースイメージ名 |
| `buildInstanceType` | AMI ビルド時のインスタンスタイプ（GPU が必要なら `g6e.4xlarge` 等） |
| `diskSizeGb` | ルートボリュームサイズ (GiB) |
| `parameters` | コンポーネントに渡すパラメータ（キーはファイル名） |

### 3. コンポーネント YAML の配置

ファイル名のプレフィックス番号で実行順序を制御します:

```
lib/imagebuilder/my-custom-image/
├── config.json
├── 01-ssm-agent.yml    ← 必須
├── 02-my-setup.yml
└── 03-my-app.yml
```

> **Important**: `01-ssm-agent.yml` は必ず含めてください。[SSM Agent](https://docs.aws.amazon.com/systems-manager/latest/userguide/ssm-agent.html) がないと Connect Lambda によるセッショントークン発行や SSM Session Manager 経由の接続ができません。同梱イメージの `01-ssm-agent.yml` をそのままコピーして使用できます。

コンポーネント YAML は [EC2 Image Builder のコンポーネントドキュメント形式](https://docs.aws.amazon.com/imagebuilder/latest/userguide/toe-use-documents.html) に従います。

### 4. cdk.json に追加

```json
{
  "config": {
    "ImageNames": ["isaacsim6.0", "my-custom-image"]
  }
}
```

### 5. デプロイ

```bash
npx cdk deploy --all
```

## インスタンスの管理

起動したインスタンスは手動で管理します:

```bash
# 停止（EBS 料金のみ発生）
aws ec2 stop-instances --instance-ids <instance-id>

# 再開
aws ec2 start-instances --instance-ids <instance-id>

# 削除
aws ec2 terminate-instances --instance-ids <instance-id>

# Elastic IP の解放（インスタンス削除後）
aws ec2 release-address --allocation-id <allocation-id>

# 起動中のワークステーション一覧
aws ec2 describe-instances \
  --filters "Name=tag:ManagedBy,Values=workstation-imagebuilder" \
            "Name=instance-state-name,Values=running,stopped" \
  --query "Reservations[].Instances[].[Tags[?Key=='Name'].Value|[0],InstanceId,State.Name,PublicIpAddress]" \
  --output table
```

### 停止したインスタンスで作業を再開したい場合

```bash
# 1. インスタンスを再開
aws ec2 start-instances --instance-ids <instance-id>

# 2. Status Check 完了を待つ（3〜5 分）
aws ec2 wait instance-status-ok --instance-ids <instance-id>

# 3. Connect Lambda で DCV 接続 URL を取得
aws lambda invoke \
  --function-name <WorkstationEnv.ConnectFunctionName> \
  --payload '{"instanceId":"<instance-id>"}' \
  --cli-binary-format raw-in-base64-out \
  /dev/stdout
```

Elastic IP が割り当て済みのため、再開後も IP アドレスは変わりません。DCV サーバーはインスタンス起動時に自動起動します。

> **Note**: インスタンスの停止は OS のシャットダウンに相当するため、停止前に開いていたウィンドウや実行中のプロセスは復元されません。ディスク上のファイルはそのまま残ります。

## データの利用

[Amazon S3 Files](https://docs.aws.amazon.com/AmazonS3/latest/userguide/s3-files.html) が `/mnt/s3files` にマウントされます。S3 バケットにデータをアップロードすれば、すべてのワークステーションから共有できます。

バケット名はスタック出力の `WorkstationEnv.S3FilesBucketName` で確認できます。

## トラブルシューティング

### UserData ログ確認

```bash
cat /var/log/workstation-bootstrap.summary
tail -n 30 /var/log/workstation-bootstrap.log
```

### 各サービスの状態確認

```bash
sudo systemctl status dcvserver --no-pager       # DCV サーバ
sudo dcv list-sessions                           # DCV セッション
snap services amazon-ssm-agent                   # SSM Agent
mount | grep s3files                             # S3 Files マウント
```

### AMI の再ビルド

1. コンポーネント YAML を修正
2. `npx cdk deploy --all` — コンポーネント変更を検知してパイプラインが自動実行される
3. ビルド完了後、SSM Parameter Store の AMI ID が自動更新される
4. 新しいインスタンスを起動（既存インスタンスは影響なし）

## コスト（us-east-1）

| リソース | 料金 | 備考 |
|---------|------|------|
| EC2 インスタンス | インスタンスタイプによる | オンデマンド料金、起動中のみ課金 |
| EBS (gp3, 512 GiB) | ~$40.96/月 | $0.08/GiB/月、停止中も課金 |
| Elastic IP | $0.005/時間 | 起動中・停止中ともに課金 |
| S3 Files | S3 + EFS 料金 | 使用量に応じた従量課金 |
| Lambda | ほぼ無料 | 起動・接続時のみ実行 |
| Image Builder | EC2 ビルド時間のみ | パイプライン自体は無料 |

### コスト削減のヒント

- 使わない時はインスタンスを停止する（EBS + EIP 料金のみ）
- 不要なインスタンスは terminate + EIP 解放
- 用途に応じて小さいインスタンスタイプを選択する

## 削除

```bash
# インフラ全体を削除（起動済みインスタンスは影響なし）
npx cdk destroy --all
```

起動済みのインスタンスは手動で terminate してください:

```bash
# ランチャーが起動したインスタンスを全て終了
aws ec2 describe-instances \
  --filters "Name=tag:ManagedBy,Values=workstation-imagebuilder" \
  --query "Reservations[].Instances[].InstanceId" \
  --output text | xargs -n1 aws ec2 terminate-instances --instance-ids
```
