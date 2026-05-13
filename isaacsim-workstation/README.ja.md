# NVIDIA IsaacSim Development Workstation

日本語 | [English](/isaacsim-workstation/README.md)

NVIDIA Isaac Sim Development Workstation AMI を使用した GPU インスタンスを CDK でデプロイし、NICE DCV 経由でリモートデスクトップ接続できるワークステーションを構築します。

## アーキテクチャ

- **GPU EC2 インスタンス**: NVIDIA Isaac Sim AMI (Ubuntu 24.04 ベース) + NICE DCV
- **VPC**: 新規作成 or 既存 VPC のインポート（GPU インスタンス対応 AZ を自動選択）
- **S3 Files**: EFS 互換の S3 バックエンドファイルシステム（NFS マウント）
- **UserData**: ROS2 Jazzy + S3 Files マウントを自動セットアップ

### AMI に含まれるもの（インストール不要）

<https://aws.amazon.com/marketplace/pp/prodview-bl35herdyozhw>

- NVIDIA ドライバ
- NICE DCV サーバ + 自動セッション
- NVIDIA Isaac Sim
- PyTorch
- SSM Agent

### UserData でセットアップされるもの

- ROS2 Jazzy (Desktop + rosbridge)
- S3 Files (EFS) マウント (`/mnt/s3files`)

## 前提条件

- Node.js v20+
- AWS CLI 設定済み（AdministratorAccess 推奨）
- CDK Bootstrap 済み (`npx cdk bootstrap`)
- NICE DCV クライアントインストール済み（接続用）
- **AWS Marketplace で NVIDIA Isaac Sim AMI をサブスクライブ済みであること**
  - サブスクライブページ: <https://aws.amazon.com/marketplace/pp/prodview-bl35herdyozhw>
  - サブスクライブしていない場合、デプロイ時に AMI の参照が失敗します

## デプロイ

```bash
cd cdk
npm install
cdk deploy
```

### 設定のカスタマイズ

`cdk.json` の `config` セクションで設定を変更できます:

| パラメータ | デフォルト | 説明 |
|-----------|-----------|------|
| `StackPrefix` | `Dev` | スタック名サフィックス（`DevWorkstation`） |
| `VpcId` | `""` (新規作成) | 既存 VPC を使う場合に指定 |
| `SubnetAZ` | `""` (自動選択) | AZ を明示的に指定する場合 |
| `InstanceType` | `g6e.8xlarge` | GPU インスタンスタイプ |

コマンドラインで指定する場合:

```bash
cdk deploy -c VpcId=vpc-xxxxxxxx -c SubnetAZ=subnet-xxxxxxxx
```

### スタック出力一覧

cdk deployが終わると以下の情報が出力されます。

| 出力名 | 説明 |
|--------|------|
| `DevWorkstation.SimulatorAZNameParameterStore` | VPC Idが格納されたParameter store名 |
| `DevWorkstation.SimulatorClusterAvailabilityZone` | EC2インスタンスが起動している AZ Id |
| `DevWorkstation.WorkstationDCVApp` | DCV Native App 用 URL |
| `DevWorkstation.WorkstationDCVWebURL` | DCV Web クライアント URL |
| `DevWorkstation.WorkstationInstancePublicIP` | インスタンスの Elastic IP |
| `DevWorkstation.WorkstationS3FilesBucketName` | S3 Files 用のバケット名 |
| `DevWorkstation.WorkstationS3FilesFileSystemId` | S3 Files ファイルシステム ID |
| `DevWorkstation.WorkstationSSMSessionCommand` | Session Manager 接続コマンド(ssh) |
| `DevWorkstation.WorkstationSetPasswordCommand` | ubuntu パスワード設定コマンド |
| `DevWorkstation.WorkstationWaitForInstanceCommand` | Status Check 完了待ちコマンド |

あとでこの情報を見たい場合は、マネージメントコンソールでCloudFormationのスタックの情報を確認するか、以下のコマンドで取得することが可能です。(リージョンとStack名は実際に利用したたいを指定してください)

```bash
export AWS_DEFAULT_REGION=us-east-1
aws cloudformation describe-stacks --stack-name <your stack name> --query "Stacks[0].Outputs"
```

## デプロイ後に実行すること

### インスタンスの Status Check 完了を待つ

デプロイ完了後、インスタンスの初期化（UserData の実行含む）が完了するまで待ちます。EC2 インスタンスの Status Check が `3/3 checks passed` になるまでパスワード設定や DCV 接続を行わないでください。スタック出力の `WaitForInstanceCommand` を実行します:

```bash
aws ec2 wait instance-status-ok --instance-ids <instance-id>
```

### Session Manager で接続（sshアクセス）

スタック出力の WorkstationSSMSessionCommand を実行します

```bash
aws ssm start-session --target <instance-id> --region <region>
```

rootユーザーになっているので、以下のコマンドでubuntuユーザーに切り替えます

```bash
sudo su - ubuntu
```

#### UserData ログ確認

```bash
cat /var/log/workstation-bootstrap.summary
```

失敗していたら、以下のコマンドでログを確認してください。

```bash
tail -n 20 /var/log/workstation-bootstrap.log
```

### ubuntu ユーザーのパスワード設定

cdk deployを実行したPCから、DCV ログイン用のパスワードを設定します。スタック出力の `SetPasswordCommand` を使用します:

```bash
export UBUNTU_PW="your-password-here"

# スタック出力の SetPasswordCommand をそのまま実行
aws ssm send-command --instance-ids i-0986d7fe5a672b6f5 --document-name "AWS-RunShellScript" --parameters "commands=[\"HASHED=\$(openssl passwd -6 '${UBUNTU_PW}') && sudo usermod --password \\\"\$HASHED\\\" ubuntu\"]" --region us-east-1 --output text --query "Command.CommandId"
```

### Amazon DCV で接続

Amazon DCVを利用したリモートデスクトップを利用するには2つの方法があります。Webブラウザで専用のURLでアクセスするか、[DCV クライアント](https://www.amazondcv.com/)で専用のDCV Viewerクライアントをダウンロードして、DCV Viewerでアクセスする方法があります。

#### Web ブラウザでアクセス

1. スタック出力の `WorkstationDCVWebURL`（`https://<EIP>:8443`）をブラウザで開きます
1. ブラウザの場合に証明書の警告が出たら「信頼して接続」を選択
1. ユーザー名: `ubuntu`、パスワード: 上で設定したパスワードでログイン

#### Native App でアクセス

1. スタック出力の `WorkstationDCVApp`（`dcv://<EIP>:8443`）をブラウザで開きます
1. ユーザー名: `ubuntu`、パスワード: 上で設定したパスワードでログイン

## Isaac Sim / Isaac Lab の起動

DCV でEC2のリモートデスクトップに接続した後、ターミナルを開いて以下の手順でアプリケーションを起動します。

### Isaac Sim の起動

<https://docs.isaacsim.omniverse.nvidia.com/5.1.0/index.html>

初回の起動では、様々なアセットの読み込みが行われるため、画面が固まっている様に見えたり、「応答なし」の様なメッセージが表示されます。数分程度待っているとIsaacSimが起動されますので、しばらく待ってください。

```bash
cd ~
./IsaacSim/isaac-sim.sh
```

#### サンプルの実行

NVIDIA Isaac Sim のチュートリアルを試してみましょう。
<https://docs.isaacsim.omniverse.nvidia.com/5.1.0/introduction/quickstart_isaacsim_robot.html>

### Isaac Lab の実行

<https://isaac-sim.github.io/IsaacLab/main/index.html>

次はIsaacLabのサンプルを動かしてみましょう。IsaacSimが起動している場合は閉じてから作業してください。

```bash
cd ~/IsaacLab
conda activate env_isaaclab
```

IsaacLabを終了する場合は、起動したターミナルで `Control + C` で終了させます。

初回の起動では、様々なアセットの読み込みが行われるため、画面が固まっている様に見えたり、「応答なし」の様なメッセージが表示されます。数分程度待っているとIsaacSimが起動されますので、しばらく待ってください。

```bash
# チュートリアル: 空のシミュレーション環境を起動
./isaaclab.sh -p scripts/tutorials/00_sim/create_empty.py
```

[サンプルで用意されているロボットのトレーニング](https://isaac-sim.github.io/IsaacLab/main/source/setup/installation/source_installation.html#train-a-robot)は以下のコマンドで実行できます。

```bash
# 強化学習: Ant の歩行トレーニング
./isaaclab.sh -p scripts/reinforcement_learning/rsl_rl/train.py --task=Isaac-Ant-v0
```

### ROS2 との連携

<https://docs.isaacsim.omniverse.nvidia.com/5.1.0/installation/install_ros.html>

Isaac Sim と ROS2 を連携させるには、Isaac Sim 起動前に環境変数を設定する必要があります。IsaacSimが起動している場合は閉じてから、IsaacLabのconda環境が有効になっている場合は `conda deactivate` してから作業してください。

```bash
export ROS_DISTRO=jazzy
export RMW_IMPLEMENTATION=rmw_fastrtps_cpp
export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:/opt/IsaacSim/exts/isaacsim.ros2.bridge/jazzy/lib
```

上記を設定した状態で Isaac Sim を起動します。これらの環境変数が未設定だと ROS2 Bridge の有効化に失敗します。

```bash
~/IsaacSim/isaac-sim.sh
```

#### ROS2 ブリッジの動作確認

Isaac Sim 起動後、別のターミナルを開いて 環境変数を設定してROS2 のトピックが見えることを確認します。

```bash
ros2 topic list
```

#### チュートリアルの実行

[IsaacSim ROS2 チュートリアル](https://docs.isaacsim.omniverse.nvidia.com/5.1.0/installation/install_ros.html#setting-up-workspaces)で紹介されている ROS2 ワークスペースのサンプルが `~/IsaacSim-ros_workspaces` にビルド済みで用意されています。

TurtleBot3 のサンプルなど、詳細な手順は以下を参照してください。

<https://docs.isaacsim.omniverse.nvidia.com/5.1.0/ros2_tutorials/index.html>

## EC2インスタンスのトラブルシューティング

### Cloud init ログ確認

```bash
sudo tail -n 20 /var/log/cloud-init-output.log
```

### 各サービスの状態確認

```bash
sudo systemctl status dcvserver --no-pager       # DCV サーバ
sudo dcv list-sessions                           # DCV セッション
snap services amazon-ssm-agent                   # SSM Agent
mount | grep s3files                             # S3 Files マウント
```

### UserData の再実行

失敗したステップのみ再実行できます（冪等性あり）:

```bash
sudo ls -la /var/lib/workstation-bootstrap/
# 特定ステップのマーカーを削除して再実行
sudo rm /var/lib/dcv-bootstrap/install-ros2-jazzy.done
sudo bash /var/lib/cloud/instance/scripts/part-001
```

## コスト（us-east-1）

| リソース | 料金 | 備考 |
|---------|------|------|
| g6e.8xlarge (EC2) | $4.52856/時間 | オンデマンド料金 |
| NVIDIA Isaac Sim AMI | $0.00/時間 | ソフトウェア利用料無料 |
| EBS (gp3, 512 GiB) | ~$40.96/月 | $0.08/GiB/月 |
| Elastic IP (稼働中) | $0.005/時間 | インスタンス起動中 |
| Elastic IP (停止中) | $0.005/時間 | インスタンス停止中も課金 |
| S3 Files | S3 + EFS 料金 | 使用量に応じた従量課金 |

### コスト目安（g6e.8xlarge の場合）

- **1時間あたり**: 約 $4.53
- **8時間/日 × 5日**: 約 $181/週
- **24時間稼働/月**: 約 $3,302/月（EBS込み）

### コスト削減のヒント

- 使わない時はインスタンスを停止する（EBS 料金のみ発生）
- デモ終了後は `npx cdk destroy` でスタック全体を削除する
- 小さいインスタンスタイプ (`g6e.4xlarge`) で十分な場合はそちらを使用する

## 削除

```bash
cdk destroy
```

全リソースに `RemovalPolicy.DESTROY` が設定されているため、S3 バケット等も含めて完全に削除されます。
