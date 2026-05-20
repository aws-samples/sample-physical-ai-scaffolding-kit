# Physical AI パイプラインプラットフォーム

ロボット学習ワークフロー（生のデモデータから評価済みポリシーまで）のための、AWS 上のクラウドネイティブパイプラインプラットフォームです。開発者はラップトップから `physai` CLI でパイプラインジョブを投入し、SageMaker HyperPod Slurm クラスター上でコンテナ化されたワークロードをオーケストレーションします。

## physai の概要

physai はラップトップで動作する CLI です。データ変換、バリデーション、学習、評価といったコンテナ化されたロボット学習パイプラインを、SSH 経由で SageMaker HyperPod Slurm クラスターに投入します。クラスターは永続的な共有ストレージ (FSx for Lustre) と長期のアカウンティング履歴 (RDS) を保持します。デフォルトでは投入したジョブのログがターミナルにストリーミングされ、Ctrl-C しても生き残ります (リモートジョブは実行を継続し、再接続可能)。`-n` / `--no-stream` を渡すと投入後すぐに戻ります。

## サンプル

- ロボット: SO-101
- シミュレーション環境: LeIsaac (PickOrange, LiftCube)
- モデル: GR00T N1.6

## アーキテクチャ概要

```
開発者ラップトップ                    AWS
────────────────                      ────────────────────────────────
physai CLI ───── SSH (SSM) ─────────► HyperPod クラスター (ログインノード)
                                      ├── コントローラー (Slurm スケジューラー + slurmdbd)
                                      ├── GPU ワーカー  (g6e / L40S デフォルト)
                                      └── CPU ワーカー  (m5 デフォルト)
                                      ──────────────────────────────
                                      FSx for Lustre (/fsx) — 作業用ストレージ
                                      S3 データバケット        — 永続ストア
                                      RDS MariaDB           — Slurm アカウンティング
```

詳細は以下のデザインドキュメントを参照してください。

## ディレクトリ構成

```
physai/
├── README.md                     # 英語版 README
├── README.ja.md                  # このファイル
├── docs/
│   ├── en/                       # 英語ドキュメント
│   └── ja/                       # 日本語ドキュメント
├── infra/                        # CDK プロジェクト
│   ├── bin/app.ts                # エントリポイント
│   ├── lib/
│   │   ├── infra-stack.ts        # VPC, S3, FSx, RDS, Secrets Manager
│   │   └── cluster-stack.ts      # HyperPod クラスター, IAM, ライフサイクルバケット
│   ├── lifecycle/                # ノードプロビジョニングスクリプト (HyperPod ノード上で実行)
│   ├── scripts/
│   │   ├── setup-ssh.sh          # SSM 経由でログインノードに SSH 鍵をアップロード
│   │   ├── cleanup.sh            # 削除コマンドを出力 (手動レビュー)
│   │   └── cleanup-failed-stacks.sh   # 作成に失敗したスタックをクリーンアップ
│   └── cdk.json
├── cli/                          # physai CLI (Python, ローカルにインストール)
│   └── physai/
├── examples/
│   └── so101-gr00t/              # Phase 1: LeIsaac + SO-101 + GR00T N1.6
│       ├── project.yaml          # コンテナ共有設定
│       ├── containers/
│       │   ├── leisaac-runtime/       # ベース: IsaacSim + LeIsaac (GR00T なし)
│       │   ├── leisaac-gr00t-n1.6/    # 評価ランタイム: leisaac-runtime + GR00T N1.6
│       │   └── gr00t-n1.6-trainer/    # GR00T N1.6 ファインチューニング
│       ├── configs/              # run_config.yaml ファイル (タスクごと)
│       └── model_configs/        # モデル・ロボット別設定ファイル
```

## ドキュメント

| ドキュメント | 対象 |
|-------------|------|
| [docs/ja/DEPLOYMENT.ja.md](docs/ja/DEPLOYMENT.ja.md) | デプロイ手順 |
| [docs/ja/RUN_SAMPLE.ja.md](docs/ja/RUN_SAMPLE.ja.md) | サンプルプロジェクトの実行 |
| [docs/ja/PHYSAI_CLI.ja.md](docs/ja/PHYSAI_CLI.ja.md) | モデル開発者向け — CLI リファレンス |
| [docs/ja/PIPELINE_DEVELOP.ja.md](docs/ja/PIPELINE_DEVELOP.ja.md) | モデル開発者向け — 独自パイプラインの構築 |
| [docs/ja/PIPELINE_DESIGN.ja.md](docs/ja/PIPELINE_DESIGN.ja.md) | プラットフォーム開発者向け — パイプラインアーキテクチャ |
| [docs/ja/INFRA.ja.md](docs/ja/INFRA.ja.md) | プラットフォーム開発者向け — CDK スタックとライフサイクルスクリプト |
| [docs/ja/STATUS.ja.md](docs/ja/STATUS.ja.md) | Phase 1 スコープ (LeIsaac + SO-101 + GR00T N1.6) と実装状況 |

## コスト

デフォルトデプロイメントでは以下のリソースが 24 時間 365 日稼働します — SageMaker HyperPod はアイドル状態のノードを停止しません。料金は **us-west-2** のオンデマンド料金、月 730 時間で算出しています。

| リソース | 構成 | 月額 |
|----------|------|------|
| HyperPod コントローラー | 1x `ml.c5.large` | 約 $74 |
| HyperPod ログイン | 1x `ml.c5.large` | 約 $74 |
| HyperPod GPU ワーカー | 1x `ml.g6e.2xlarge` (L40S) | 約 $2,044 |
| HyperPod CPU ワーカー | 1x `ml.m5.2xlarge` | 約 $337 |
| FSx for Lustre | 1.2 TB PERSISTENT_2 SSD, 125 MB/s/TiB | 約 $174 |
| RDS MariaDB | `db.t4g.small` + 20 GiB gp3 | 約 $26 |
| NAT Gateway | 1x (時間課金、データ転送なし) | 約 $33 |
| Secrets Manager, CloudWatch alarm | — | 約 $1 |
| **合計 (常時稼働)** | | **約 $2,700** |

GPU ワーカーがコストの大部分を占めます。クラスターを破棄せずにコンピューティングを一時停止するには、`infra/cdk.json` で `cpuWorkerCount` や `gpuWorkers[*].count` を `0` に設定し `PhysaiClusterStack` を再デプロイしてください。復帰時はカウントを元に戻して再デプロイします。

各リージョンの最新料金は以下で確認してください:
[SageMaker](https://aws.amazon.com/sagemaker/ai/pricing/) ·
[FSx Lustre](https://aws.amazon.com/fsx/lustre/pricing/) ·
[RDS MariaDB](https://aws.amazon.com/rds/mariadb/pricing/) ·
[VPC](https://aws.amazon.com/vpc/pricing/)
