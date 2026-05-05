# プラットフォームアーキテクチャ

このドキュメントでは、physai プラットフォームの内部アーキテクチャ（ストレージ設計、ジョブオーケストレーション、インフラストラクチャ、コストモデル）について説明します。プラットフォームのメンテナーおよびオペレーター向けです。

独自のパイプラインの開発（コンテナ定義、設定フォーマット、エントリポイント仕様）については [PIPELINE_DEVELOP.ja.md](PIPELINE_DEVELOP.ja.md) を参照してください。CLI コマンドリファレンスは [PHYSAI_CLI.ja.md](../ja/PHYSAI_CLI.ja.md)、CDK スタックの詳細は [INFRA.ja.md](../ja/INFRA.ja.md) を参照してください。

## 1. システム概要

```
┌──────────────────────────────────────────────────────────────────┐
│  開発者マシン                                                      │
│    └── physai CLI (SSH 経由でオーケストレーション)                    │
└──────────────────────────────────────────────────────────────────┘
         │ SSH
         ▼
┌──────────────────────────────────────────────────────────────────┐
│                    SageMaker HyperPod クラスター                   │
│                                                                  │
│  ログインノード (ml.c5.large)                                      │
│    ├── 開発者向け SSH エントリポイント                                │
│    └── MLflow クライアント (実験ログ)                                │
│                                                                  │
│  コントローラーノード (ml.c5.large)                                  │
│    └── Slurm スケジューラー                                        │
│                                                                  │
│  ワーカーパーティション: "gpu" (固定数、cdk.json で設定)               │
│    → データ拡張、学習、評価                                         │
│                                                                  │
│  ワーカーパーティション: "cpu" (固定数、cdk.json で設定)               │
│    → フォーマット変換、バリデーション、登録                            │
│                                                                  │
│  全ノードがマウント: /fsx (FSx for Lustre)                          │
└──────────────────────────────────────────────────────────────────┘
         │
         ▼
┌──────────────────────┐   ┌──────────────────────┐
│  S3 (永続)            │   │  SageMaker MLflow    │
│  ├── raw/            │   │  (トラッキングサーバー)  │
│  ├── datasets/       │   └──────────────────────┘
│  ├── checkpoints/    │
│  └── results/        │
│                      │
│  FSx (作業用)         │
│  /fsx/               │
│  ├── raw/  ←DRA──S3  │
│  ├── datasets/       │
│  ├── checkpoints/    │
│  ├── evaluations/    │
│  ├── enroot/         │
│  └── physai/         │
└──────────────────────┘
```

CLI はセッション開始時に単一の SSH ControlMaster 接続を確立し、以降のすべてのコマンドをその接続上で多重化します。

## 2. ストレージアーキテクチャ

2 層モデルです：**S3** が永続ストア、**FSx for Lustre** が高速作業用ストレージです。

### S3 (永続)

パイプラインのすべての入出力がここに永続的に保存されます。

```
s3://<bucket>/
├── raw/                    # ユーザーがアップロードした HDF5 デモデータ
├── datasets/               # 公開済み LeRobot v2.1 データセット
├── checkpoints/            # 公開済みモデルチェックポイント
└── results/                # 公開済み評価メトリクスおよび動画
```

### FSx for Lustre (作業用)

全クラスターノードで GB/s スループットで共有されます。一時的なもので、各実行後にクリーンアップされます。

```
/fsx/
├── raw/                    # S3 からの DRA 自動インポート (読み取り専用リンク)
├── datasets/               # 変換済み LeRobot データセット (S3 からステージングまたはコンバーターが書き込み)
├── checkpoints/            # 学習チェックポイント (登録ステージで S3 に公開)
├── evaluations/            # 評価ログとメトリクス (登録ステージで S3 に公開)
├── enroot/                 # コンテナ squashfs イメージ
└── physai/                 # CLI 作業状態
    ├── logs/               # ジョブログ: <job-id>.out
    ├── builds/             # ビルド作業ディレクトリ
    └── sync/               # rsync された設定ファイルとモデル設定
```

### ローカル NVMe

GPU ワーカーノード上の高速ローカルストレージ (`/opt/dlami/nvme`) です。`/fsx` に書き込まない一時的な拡張 HDF5 (600GB 以上) に使用されます。

### データフロー

1. ユーザーが生の HDF5 を `s3://bucket/raw/` にアップロードすると、`/fsx/raw/` に自動インポートされます（初回アクセス時に遅延ロード）
2. パイプラインステージが `/fsx` 上で Lustre 速度で読み書きします
3. 登録ステージが最終結果を明示的な `aws s3 cp` で S3 に公開します
4. 生の HDF5 は変換後に `/fsx/raw/` から削除されます。必要に応じて S3 から再インポートできます
5. 公開済みデータセットからの再学習時、`physai train` が S3 から `/fsx/datasets/` にステージングします

`/fsx/raw/` は `s3://bucket/raw/` にリンクされた Data Repository Association (自動インポートのみ) を持ちます。ユーザーが S3 に HDF5 をアップロードすると、初回アクセス時の遅延ロードにより `/fsx/raw/` に出現します。その他の `/fsx/` ディレクトリには S3 リンクはありません。登録ステージが `/fsx` から S3 へ明示的な `aws s3 cp` で最終結果を公開します。

### 実行ごとのストレージ予算

| データ | サイズ | ライフサイクル |
|--------|--------|----------------|
| 生の HDF5 (100 エピソード、デュアルカメラ) | 約 600GB | 変換後に削除 |
| LeRobot データセット (H.264 圧縮) | 約 5-10GB | S3 に公開後に削除 |
| チェックポイント (3B モデル、3 回保存) | 約 10-15GB | S3 に公開後に削除 |
| 評価ログ + メトリクス | 約 1GB | S3 に公開後に削除 |
| コンテナ squashfs イメージ | 約 40GB | `/fsx` 上に永続 |

FSx は 1.2TB で開始します。2.4TB 単位でのライブ容量増加をサポートします (ダウンタイムなし、増加のみ)。`FreeStorageCapacity` の CloudWatch アラームが容量不足前に警告します。

## 3. Slurm ジョブチェーン

`physai run` はステージごとに 1 つの Slurm ジョブを投入し、`--dependency=afterok` で連結します：

```bash
RUN_ID=run-20260415-155400
JOB1=$(sbatch --parsable --job-name=physai/run/$RUN_ID/train    train.sh)
JOB2=$(sbatch --parsable --job-name=physai/run/$RUN_ID/eval     --dependency=afterok:$JOB1 eval.sh)
JOB3=$(sbatch --parsable --job-name=physai/run/$RUN_ID/register --dependency=afterok:$JOB2 register.sh)
```

すべてのジョブは run ID を共有します。いずれかのステップが失敗すると、下流のジョブはキャンセルされます。`physai cancel` でいずれかのジョブをキャンセルすると、同じ run ID を共有するすべてのジョブがキャンセルされます。

コンテナイメージが現在ビルド中 (`physai build` が進行中) の場合、パイプラインは自動的にビルドジョブを依存関係として追加します。`physai build` の開始直後に `physai run` を実行できます。

## 4. データ拡張

データ拡張が有効な場合、オーケストレーターは拡張と変換を同一 GPU ノード上の単一 Slurm ジョブとして実行します。拡張された HDF5 はローカル NVMe (`/fsx` ではなく) に書き込まれ、変換はローカル NVMe から読み取って `/fsx` に書き込みます。拡張された HDF5 (600GB 以上になる可能性あり) は共有ストレージに触れることなく、ジョブ終了時に自動的にクリーンアップされます。

## 5. DCV によるビジュアル評価 — 計画中、未実装

`physai eval --visual` は、NICE DCV を介して開発者のブラウザにレンダリングされたシミュレーションビューポートをストリーミングします：

```bash
$ physai eval --visual --config examples/so101-gr00t/configs/so101_pickorange_gr00t-n1.6.yaml \
  --checkpoint run-20260430-011618

Submitted job 456
Allocating GPU node...          gpu-worker-3 (i-0abc123def)
Starting DCV session...         physai-eval-456

Connect to the DCV session:
  aws ssm start-session --target i-0abc123def \
    --document-name AWS-StartPortForwardingSession \
    --parameters '{"portNumber":["8443"],"localPortNumber":["8443"]}'

Then open: https://localhost:8443
Username: ubuntu          Password: xxxxxxx

Streaming eval log (Ctrl-C to detach)...
```

パイプラインは `--gres=gpu:1,dcv:1` で Slurm ジョブを投入し、DCV セッションを作成、SSM ポートフォワーディングコマンドを出力し、`eval.sh` を `--visual` 付きで実行します。DCV サーバーは HyperPod ライフサイクルスクリプトを通じて GPU ワーカーにインストールされます。SSM ポートフォワーディングはセキュリティグループの変更を必要としません。

## 6. 実験トラッキング (MLflow) — 計画中、未実装

実装後、各完了した実行は SageMaker MLflow にログされます：

| カテゴリ | ログされる内容 |
|----------|----------------|
| パラメータ | model, dataset, max_steps, batch_size, augmentation config |
| メトリクス | 学習損失 (ステップごと), 評価成功率 |
| アーティファクト | チェックポイントパス (S3), 評価動画 (S3), run_config.yaml |
| タグ | Run ID, モデルタイプ, タスク名, ロボット |

## 7. HyperPod クラスター

| ノード | インスタンス | 役割 |
|--------|-------------|------|
| ログイン | ml.c5.large | SSH エントリ、MLflow クライアント |
| コントローラー | ml.c5.large | Slurm スケジューラー |
| GPU ワーカー | ml.g6e.2xlarge (1x L40S 48GB) | データ拡張、学習、評価 |
| CPU ワーカー | ml.m5.2xlarge | 変換、バリデーション、登録 |

GPU および CPU パーティションは `infra/cdk.json` で設定された固定ワーカー数で動作します ([INFRA.ja.md](../ja/INFRA.ja.md) 参照)。HyperPod はオートスケールしません。ワーカーの追加・削除はワーカー数を変更して `PhysaiClusterStack` を再デプロイしてください。

**実行中のノードにシステムレベルの変更を適用する手順**：ライフサイクルスクリプトはノードの初回プロビジョニング時にのみ実行されるため、既存のノードは `infra/lifecycle/` の編集を自動的には取り込みません。影響度の小さい順に 3 つの選択肢があります：

- **その場で再実行**: `infra/scripts/run-lifecycle.sh --all` が更新されたスクリプトをパッケージし、controller を含む全ノードへ SSM 経由で配信します。各スクリプトはノードタイプに基づき自身で適用可否を判定し、冪等に動作します。多くの場合はこの方法で対応でき、また controller（置換不可）に変更を適用する唯一の方法です。
- **置換**: worker／login ノードのみ対象。`npx cdk deploy PhysaiClusterStack`（新しいスクリプトを S3 にアップロード）の後、login ノードで `scontrol update node=X state=fail reason="Action:Replace"` を実行すると、HyperPod が新しいスクリプトでノードを再プロビジョニングします。
- **クラスタースタック全体の再デプロイ**（最終手段）: `npx cdk destroy PhysaiClusterStack && npx cdk deploy PhysaiClusterStack`。遅く（約 25 分）、実行中のジョブは失われますが、安全です — `PhysaiClusterStack` は設計上ステートレスで、`PhysaiInfraStack`（FSx、RDS、S3 データバケット）は変更されません。上記 2 つでは回復できないほどクラスターが詰まっている場合や、ライフサイクルの tarball が `run-lifecycle.sh` が依存する SSM サイズ上限を超えた場合に使用します。

`UpdateClusterSoftware` は AMI が変更された場合にのみ再プロビジョニングし、既存の AMI 上でライフサイクルスクリプトの再実行を強制するためには使用できません。詳細なワークフローは [DEPLOYMENT.ja.md](DEPLOYMENT.ja.md#稼働中のクラスターへのライフサイクルスクリプト変更の適用上級者向け) を参照してください。

## 8. コストモデル

すべてのクラスターノードは 24 時間 365 日稼働します — HyperPod はアイドル状態のインスタンスを停止しません。デフォルトデプロイメント (GPU ワーカー 1 台、CPU ワーカー 1 台、両方常時稼働) のコストは us-west-2 で約 **$2,700/月** で、GPU ワーカーが大部分を占めます (`ml.g6e.2xlarge` 1 台で約 $2,000/月)。

`infra/cdk.json` でワーカー数を設定してコストをスケーリングできます：

- アイドル (ワーカーなし): 約 $310/月 (コントローラー + ログイン + FSx + RDS + NAT + 小規模サービス)
- `ml.g6e.2xlarge` GPU ワーカー 1 台追加ごと: 約 $2,000/月
- `ml.m5.2xlarge` CPU ワーカー 1 台追加ごと: 約 $340/月

## 9. 参考資料

- [AWS Sample: Embodied AI Platform](https://github.com/aws-samples/sample-embodied-ai-platform)
- [AWS Sample: Physical AI Scaffolding Kit](https://github.com/aws-samples/sample-physical-ai-scaffolding-kit)
- [SageMaker HyperPod Documentation](https://docs.aws.amazon.com/sagemaker/latest/dg/sagemaker-hyperpod.html)
- [LeRobot Dataset Format](https://huggingface.co/docs/lerobot/lerobot-dataset-v3)
