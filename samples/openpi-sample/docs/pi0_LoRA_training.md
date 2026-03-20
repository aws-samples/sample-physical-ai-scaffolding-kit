# HyperPod + Slurm + Enroot での OpenPI LoRA トレーニング実行ガイド

AWS SageMaker HyperPod 上で Slurm + Enroot を使用して Docker コンテナで LoRA ファインチューニングを実行するガイドです。

## アーキテクチャ

```mermaid
flowchart TB
    local[Local / EC2<br/>Docker Build]
    S3[S3 Bucket<br/>Training Data, Code]
    ecr[Container Registry<br/>Amazon ECR]
    login[Login Node<br/>Slurm Master]
    fsx[FSx for Lustre<br/>/fsx shared]
    compute[Compute Node<br/>srun --container-image<br/>Pyxis + Enroot]

    local -->|Docker Build & Push| ecr
    local -->|Data Transfer| S3
    ecr -->|Enroot Import| login
    S3 -.->|shared storage| fsx
    fsx -.->|shared storage| login
    login -->|sbatch| compute

    style local fill:#e1f5ff
    style ecr fill:#fff4e1
    style login fill:#f0f0f0
    style fsx fill:#f0f0f0
    style compute fill:#d4f4dd
```

***

## 前提条件

### 環境準備

1. **Hyperpod クラスタ**: 本プロジェクト内の CDK を使って Hyperpod クラスタを AWS 上に構築している
	以下の手順では、CDK で構築されている Hyperpod を前提に記述しますが、コンソールなどから手動で作成した Hyperpod でも同様に学習実行は可能です。
2. **開発環境**: 以下の設定が必要になります。
	1. AWS 認証情報設定: ECR アクセス用
	2. Docker: pi0 学習用イメージのBuild用
3. **Hugging Face トークン**: サンプル学習データセットダウンロード用 (`HF_TOKEN`)
	1. [Hugging Face](https://huggingface.co/settings/tokens) での事前 Sign Up とToken の払い出しが必要です。独自の学習データを使用する場合は不要です。

***

## 実行手順

### ローカル環境でのイメージビルド & ECR プッシュ

#### Docker イメージのビルドと ECR への Push

```bash
cd samples/openpi-sample/

# openpi を Clone
git clone https://github.com/Physical-Intelligence/openpi.git

cd lora_training/

# ECR にビルド & プッシュ（AWS CLI のデフォルトリージョンを使用）
./build_and_push_ecr.sh

# リージョンとアカウントIDの両方を指定する場合 
./build_and_push_ecr.sh us-west-2 123456789012

# 特定のタグを指定する場合
IMAGE_TAG=v1.0.0 ./build_and_push_ecr.sh
```

**環境情報の取得方法（ローカルPC）**:

スクリプトは以下の優先順位で環境情報を取得します：

1. **コマンドライン引数**（最優先）
2. **環境変数** (`AWS_REGION`, `AWS_ACCOUNT_ID`)
3. **AWS CLI設定**
   * リージョン: `aws configure get region`
   * アカウントID: `aws sts get-caller-identity --query Account --output text`

**実行内容**:
* ECR リポジトリ `openpi-lora-train` の作成（存在しない場合）
* Docker イメージのビルド（`train_lora.Dockerfile` を使用）
* ECR へのプッシュ

**出力例**:

```
✅ Docker image successfully pushed to ECR
Image URI: 123456789012.dkr.ecr.us-west-2.amazonaws.com/openpi-lora-train:latest
```

***

### HyperPod Login Node での準備

#### HyperPod への SSH 接続

[DEPLOYMENT.md](../../../hyperpod/docs/DEPLOYMENT.md) の HyperPod への SSH 接続方法を参考に接続。
```
ssh pask-cluster
```

#### プロジェクトのセットアップ

```bash
# コード一式を PASK リポジトリのclone
cd
git clone git@github.com:aws-samples/sample-physical-ai-scaffolding-kit.git

# セットアップを実行。パラメータについて以下を参照
cd samples/openpi-sample/lora_training
./setup.sh --hf-token "hf_xxxxx"

# 環境変数を反映
source ~/.bashrc
```

**パラメータについて**
* hf-token（オプション）:  "hf\_" から始まる Hugging Face のTokenを指定
	* [Hugging Face](https://huggingface.co/settings/tokens) での事前 Sign Up とToken の払い出しが必要です。

**実行内容について**

1. OpenPI リポジトリのクローン
	- /fsx/ubuntu/samples/openpi-sample/openpi/ が存在しない場合
	- GitHub から git clone <https://github.com/Physical-Intelligence/openpi.git> を実行
2. ディレクトリ構造の作成
	- /fsx/ubuntu/samples/openpi-sample/logs/
	- /fsx/ubuntu/samples/openpi-sample/.cache/
	- /fsx/ubuntu/samples/openpi-sample/openpi/assets/physical-intelligence/libero/
3. 環境変数を \~/.bashrc に設定 🆕
	- 既存の OpenPI/Enroot 設定があれば削除（バックアップ作成）
	- 以下の環境変数を追記：**!重要**これらの環境変数は、すべての Slurm ジョブスクリプトで使用されます。
		* export OPENPI\_BASE\_DIR=/fsx/ubuntu/samples/openpi-sample
		* export OPENPI\_PROJECT\_ROOT=\${OPENPI\_BASE\_DIR}/openpi
		* export OPENPI\_DATA\_HOME=\${OPENPI\_BASE\_DIR}/.cache
		* export OPENPI\_LOG\_DIR=\${OPENPI\_BASE\_DIR}/logs
		* export HF\_TOKEN=<引数で指定した値 or 空>
		* export ENROOT\_CACHE\_PATH=/fsx/enroot
		* export ENROOT\_DATA\_PATH=/fsx/enroot/data

**Weights & Biases (wandb) について**:
* デフォルトのスクリプトでは wandb を無効化しています (`--no-wandb-enabled`)
* wandb でトレーニングをトラッキングしたい場合:
  1. [wandb.ai](https://wandb.ai) でアカウントを作成
  2. API key を取得して `~/.bashrc` に追加: `export WANDB_API_KEY=your_key_here`
  3. スクリプトから `--no-wandb-enabled` を削除（または `--wandb-enabled` に変更）

---
#### Enroot で Docker イメージをインポート

```bash
# ECR イメージを Enroot 形式に変換
cd samples/openpi-sample/lora_training

# EC2 メタデータから自動取得
./hyperpod_import_container.sh

# イメージタグを指定
./hyperpod_import_container.sh v1.0.0

# リージョンを指定
./hyperpod_import_container.sh latest us-west-2
```

**環境情報の取得方法（HyperPod Cluster）**:

スクリプトは以下の優先順位で環境情報を取得します。Hyperpod 内で特にコマンドライン引数や環境変数設定なしに実行した場合、EC2インスタンスメタデータから取得することになります。：

1. **コマンドライン引数**（最優先）

   ```bash
   ./hyperpod_import_container.sh [IMAGE_TAG] [AWS_REGION] [AWS_ACCOUNT_ID]
   ```

2. **環境変数**

   ```bash
   export AWS_REGION=us-west-2
   export AWS_ACCOUNT_ID=123456789012
   ./hyperpod_import_container.sh
   ```

3. **自動検出**

   * **リージョン**: EC2インスタンスメタデータ（IMDSv2）

     ```bash
     TOKEN=$(curl -X PUT "http://169.254.169.254/latest/api/token" \
       -H "X-aws-ec2-metadata-token-ttl-seconds: 21600" -s)
     curl -H "X-aws-ec2-metadata-token: $TOKEN" -s \
       http://169.254.169.254/latest/meta-data/placement/region
     ```

   * **アカウントID**: AWS STS

     ```bash
     aws sts get-caller-identity --query Account --output text
     ```

4. **フォールバック**: リージョンは `us-east-1`

**実行内容**:
* ECR から Docker イメージを Pull
* SquashFS 形式 (`.sqsh`) に変換
* `/fsx/enroot/data/` に保存

**出力例**:

```
✅ Container ready for Slurm execution
Container Name: openpi-lora-train+latest.sqsh
```

**確認**:

```bash
# インポートされたコンテナを確認
enroot list

# 出力例:
# openpi-lora-train+latest.sqsh
```


***

### Slurm ジョブの実行

#### 正規化統計の計算（初回のみ）

```bash
cd samples/openpi-sample/lora_training

# Slurm ジョブとして投入
sbatch ./slurm_compute_norm_stats.sh pi0_libero_low_mem_finetune

# ジョブ ID が返される（例: Submitted batch job 1234）
```

**進捗確認**:

```bash
# ジョブ状態確認
squeue -u ubuntu

# リアルタイムログ監視
tail -f ${OPENPI_LOG_DIR}/slurm_<JOB_ID>.out

# エラーログ確認
tail -f ${OPENPI_LOG_DIR}/slurm_<JOB_ID>.err
```


##### 実行エラーについて
**Hugging Face Quota エラー**:
サンプルの学習データを使用する場合、Hugging Face からのダウンロード時に以下のような Quota エラーが発生することがあります。
この場合は、5分以上時間をあけてから再度 `./slurm_compute_norm_stats.sh` を実行してください。
ダウンロードの途中から再開されるため、2回目では  Quota エラーなく処理が完了します。

```
huggingface_hub.errors.HfHubHTTPError: 429 Client Error: Too Many Requests for url: https://huggingface.co/api/datasets/physical-intelligence/
We had to rate limit you, you hit the quota of 1000 api requests per 5 minutes period. Upgrade to a PRO user or Team/Enterprise organization account (https://hf.co/pricing) to get higher limits. See https://huggingface.co/docs/hub/rate-limits
```


***

#### LoRA ファインチューニングの実行（GPU ジョブ）

```bash
cd samples/openpi-sample/lora_training

# LoRA トレーニングを投入
sbatch ./slurm_train_lora.sh pi0_libero_low_mem_finetune my_lora_run

# カスタム実験名で実行
sbatch ./slurm_train_lora.sh pi0_libero_low_mem_finetune experiment_$(date +%Y%m%d)
```

**進捗確認**:

```bash
# ジョブ状態確認
squeue -u ubuntu

# GPU 使用状況（compute node で）
srun --jobid=<JOB_ID> nvidia-smi

# リアルタイムログ監視
tail -f ${OPENPI_LOG_DIR}/train_<JOB_ID>.out

# エラーログ確認
tail -f ${OPENPI_LOG_DIR}/train_<JOB_ID>.err
```

**トレーニング中の典型的なログ**:

```
[1000/30000] loss=0.234 lr=1e-4 step_time=1.2s
[2000/30000] loss=0.189 lr=9e-5 step_time=1.1s
Saving checkpoint to /fsx/ubuntu/openpi_test/openpi/checkpoints/pi0_libero_low_mem_finetune/my_lora_run/2000
```

**完了確認**:

```bash
# ジョブ完了状態
sacct -j <JOB_ID> --format=JobID,State,ExitCode

# チェックポイントの確認
ls -lh ${OPENPI_PROJECT_ROOT}/checkpoints/pi0_libero_low_mem_finetune/my_lora_run/

# 出力例:
# drwxr-xr-x  1000/
# drwxr-xr-x  2000/
# drwxr-xr-x  5000/
# drwxr-xr-x  30000/  ← 最終チェックポイント
```

***

## Slurm ジョブ管理コマンド

### ジョブの確認

```bash
# 自分のジョブ一覧
squeue -u ubuntu

# 詳細情報
squeue -u ubuntu -o "%.18i %.9P %.30j %.8u %.2t %.10M %.6D %R"

# すべてのジョブ（クラスター全体）
squeue
```

### ジョブのキャンセル

```bash
# 特定のジョブをキャンセル
scancel <JOB_ID>

# 自分のすべてのジョブをキャンセル
scancel -u ubuntu

# 特定の名前のジョブをキャンセル
scancel --name=openpi_lora_train
```

***

## 参考リソース

### ドキュメント

* [AWS HyperPod ドキュメント](https://docs.aws.amazon.com/sagemaker/latest/dg/sagemaker-hyperpod.html)
* [Enroot ドキュメント](https://github.com/NVIDIA/enroot)
* [Slurm ドキュメント](https://slurm.schedmd.com/documentation.html)
