# プラットフォームアーキテクチャ

このドキュメントでは、physai プラットフォームの内部アーキテクチャ（ストレージ設計、ジョブオーケストレーション、インフラストラクチャ、コストモデル）について説明します。プラットフォームのメンテナーおよびオペレーター向けです。

独自のパイプラインの開発（コンテナ定義、設定フォーマット、エントリポイント仕様）については [PIPELINE_DEVELOP.ja.md](PIPELINE_DEVELOP.ja.md) を参照してください。CLI コマンドリファレンスは [PHYSAI_CLI.ja.md](PHYSAI_CLI.ja.md)、CDK スタックの詳細は [INFRA.ja.md](INFRA.ja.md) を参照してください。

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

2 層モデル：**S3** を永続ストア、**FSx for Lustre** を高速作業用ストレージとして使用します。

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

全クラスターノードで GB/s のスループットで共有されます。一時的なもので、各実行後にクリーンアップされます。

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

GPU ワーカーノード上の高速ローカルストレージ（`/opt/dlami/nvme`）。`/fsx` に書き込まない一時的な拡張 HDF5（600GB 以上）に使用します。

### データフロー

1. ユーザーが生のデモディレクトリを `s3://bucket/raw/<name>/` にアップロードすると、`/fsx/raw/<name>/` に自動インポートされます（初回アクセス時に遅延ロード）
2. パイプラインステージが `/fsx` 上で Lustre 速度で読み書きします
3. 登録ステージが `aws s3 cp` を明示的に実行して最終結果を S3 に公開します
4. 生の HDF5 は変換後に `/fsx/raw/` から削除されます。必要に応じて S3 から再インポートできます
5. 公開済みデータセットからの再学習時、`physai train` が S3 から `/fsx/datasets/` にステージングします

`/fsx/raw/` は `s3://bucket/raw/` にリンクされた Data Repository Association（自動インポートのみ）を持ちます。ユーザーがデモディレクトリ（各デモセットは HDF5 ファイルのディレクトリ）を S3 にアップロードすると、内容は初回アクセス時の遅延ロードで `/fsx/raw/<name>/` 配下に出現します。その他の `/fsx/` ディレクトリに S3 リンクはありません。登録ステージは `aws s3 cp` を明示的に実行し、`/fsx` から S3 に最終結果を公開します。

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
JOB1=$(sbatch --parsable --job-name=physai/run/$RUN_ID/convert  convert.sh)
JOB2=$(sbatch --parsable --job-name=physai/run/$RUN_ID/train    --dependency=afterok:$JOB1 train.sh)
JOB3=$(sbatch --parsable --job-name=physai/run/$RUN_ID/eval     --dependency=afterok:$JOB2 eval.sh)
```

すべてのジョブは run ID を共有します。いずれかのステップが失敗すると、下流のジョブはキャンセルされます。`physai cancel` でいずれかのジョブをキャンセルすると、同じ run ID を共有するすべてのジョブがキャンセルされます。

このドキュメント内の他の箇所で言及している `register` ステージは計画中で未実装です。現在のパイプラインは `eval` で終了します。

コンテナイメージが現在ビルド中 (`physai build` が進行中) の場合、パイプラインは自動的にビルドジョブを依存関係として追加します。`physai build` の開始直後に `physai run` を実行できます。

## 4. データ拡張

データ拡張が有効な場合、オーケストレーターは拡張と変換を同一 GPU ノード上の単一 Slurm ジョブとして実行します。拡張された HDF5 はローカル NVMe (`/fsx` ではなく) に書き込まれ、変換はローカル NVMe から読み取って `/fsx` に書き込みます。拡張された HDF5 (600GB 以上になる可能性あり) は共有ストレージに触れることなく、ジョブ終了時に自動的にクリーンアップされます。

## 5. DCV によるビジュアル評価

`physai eval --visual` は、NICE DCV を介して開発者のブラウザにレンダリングされたシミュレーションビューポートをストリーミングします：

```bash
$ physai eval --visual --config examples/so101-gr00t/configs/so101_pickorange_gr00t-n1.6.yaml \
  --checkpoint run-20260430-011618

Submitted 1 stage(s): eval
  Run ID:     run-20260515-030000
  Reconnect:  physai logs 123

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 Visual evaluation is ready on node ip-10-0-12-47.

 1) In a second terminal, open the SSM tunnel and KEEP IT RUNNING:

    aws ssm start-session \
      --target sagemaker-cluster:xxxxxxxxxxxx_gpu-workers-i-0123456789abcdef0 \
      --document-name AWS-StartPortForwardingSession \
      --parameters '{"portNumber":["8443"],"localPortNumber":["8443"]}' \
      --region us-west-2

 2) Open in your browser:

    https://localhost:8443/#console

 3) Accept the self-signed cert on first connect.

 4) Sign in with:

    Username: ubuntu
    Password: xK9mP2qL7nR4vT8w

 Session closes automatically when the job ends (`physai cancel 123`).
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

[eval] round 1/20 starting...
```

### 仕組み

DCV の `console` セッションは永続的です — `dcvserver` が起動時に自動作成し、GDM3 が `ubuntu` ユーザーのグラフィカル PAM セッション（自動ログイン）として立ち上げる Xorg にアタッチされます。ジョブごとのセットアップでは、`ubuntu` の PAM パスワードを新しい OTP にローテートして接続情報を表示するだけです。

1. eval ステージの sbatch は `--constraint` に `dcv` を追加します。これにより、Slurm は DCV が構成された GPU ノードにのみジョブをスケジュールします（`dcv` フィーチャは GPU タイプ（例: `l40s`）と並んで `register_slurm_features.sh` が登録します）。
2. `srun` の前に、sbatch は `/fsx/physai/dcv-claims/<host>.lock` に POSIX `flock` を取得します（1 ノードあたり 1 ビジュアルセッション）。同じノードで 2 つ目の `--visual` ジョブが投入されると、最初のジョブが解放するまで（または `--visual-timeout`、デフォルト 1 時間まで）flock を待ちます。
3. sbatch が `dcv_session_setup.sh` を source します:
   - `/opt/ml/config/resource_config.json` と IMDSv2 から `sagemaker-cluster:<cluster-id>_<group>-<instance-id>` の SSM ターゲットを解決します。
   - ワンタイムパスワードを生成し、`chpasswd` で `ubuntu` アカウントに設定します。
   - 接続情報のフルブロック（SSM トンネルコマンド、ブラウザ URL、認証情報）を出力します。
4. `srun --container-image=... eval.sh --visual` が IsaacSim を `--headless` なしで実行し、Xorg `:0` にレンダリングします。DCV が `:0` をキャプチャしてポート 8443 に配信します。
5. 開発者は別のターミナルで SSM トンネルを実行し、URL を開いて自己署名証明書を承認、サインインします。
6. ジョブ終了時（正常終了または `physai cancel`）、sbatch の `EXIT TERM` トラップが `dcv_session_teardown.sh` を実行し、`ubuntu` のパスワードをランダムな推測不可能な値にローテートします。`console` セッション自体は次のジョブのために起動したままで、新規ログインのみブロックされます。（既に接続済みのブラウザタブはユーザーが閉じるまでストリーミングを続けます。）sbatch が終了すると、カーネルが flock を自動的に解放します。

### インフラストラクチャ

- GPU ワーカーは **GDM3 + GNOME** を実行します（ライフサイクル: `install_gdm.sh`）。`ubuntu` で自動ログインし、画面ロックは dconf で無効化されています。GDM が NVIDIA ドライバとヘッドレス向けの `DFP-{0..3}` 仮想ディスプレイヘッドで Xorg を所有します — これはデータセンター GPU 向けにサポートされた構成です（AWS NICE DCV TAM Runbook 準拠）。IsaacSim はこの Xorg セッションにレンダリングします。
- `dcvserver` は常駐 systemd サービスとして実行されます（ライフサイクル: `install_dcv.sh`）。`gdm3` の後に順序付けられ、起動時に `ubuntu` 所有の `console` セッションを自動作成します。`nice-dcv-gl` は **インストールされません** — その GL インターセプト層は IsaacSim の CUDA/Vulkan パスと競合します。コンソールセッションには不要です。
- Slurm の `dcv` フィーチャは GPU ノードで `register_slurm_features.sh` が登録します（GPU タイプフィーチャ `l40s` や `h100` などと並列に）。`--visual` 指定時、パイプラインは eval ステージの `--constraint` に `&dcv` を追加します。
- DCV の排他制御は POSIX `flock(2)` advisory ロックを FSx Lustre 上のファイル（`/fsx/physai/dcv-claims/<host>.lock`）に取得して行います。ロックは sbatch 内でジョブのライフタイム中保持され、終了時にカーネルが解放するため、古いクレームのクリーンアップは不要です。FSx は `flock` でマウントしています（`localflock` ではありません）。
- IAM ポリシーは EC2 自動ライセンスのために `arn:aws:s3:::dcv-license.<region>/*` への `s3:GetObject` を許可しています。
- セキュリティグループの変更は不要です — SSM ポートフォワーディングはインバウンドルールを必要としません。

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

GPU および CPU パーティションは `infra/cdk.json` で設定された固定ワーカー数で動作します ([INFRA.ja.md](INFRA.ja.md) 参照)。HyperPod はオートスケールしません。ワーカーの追加・削除はワーカー数を変更して `PhysaiClusterStack` を再デプロイしてください。

**実行中のノードにシステムレベルの変更を適用する手順**：ライフサイクルスクリプトはノードの初回プロビジョニング時にのみ実行されるため、既存のノードは `infra/lifecycle/` の編集を自動的には取り込みません。影響度の小さい順に 3 つの選択肢があります：

- **その場で再実行**: `infra/scripts/run-lifecycle.sh --all` が更新されたスクリプトをパッケージし、controller を含む全ノードへ SSM 経由で配信します。各スクリプトはノードタイプに基づき自身で適用可否を判定し、冪等に動作します。多くの場合はこの方法で対応でき、また controller（置換不可）に変更を適用する唯一の方法です。
- **置換**: worker／login ノードのみ対象。`npx cdk deploy PhysaiClusterStack`（新しいスクリプトを S3 にアップロード）の後、login ノードで `scontrol update node=X state=fail reason="Action:Replace"` を実行すると、HyperPod が新しいスクリプトでノードを再プロビジョニングします。
- **クラスタースタック全体の再デプロイ**（最終手段）: `npx cdk destroy PhysaiClusterStack && npx cdk deploy PhysaiClusterStack`。遅く（約 25 分）、実行中のジョブは失われますが、安全です — `PhysaiClusterStack` は設計上ステートレスで、`PhysaiInfraStack`（FSx、RDS、S3 データバケット）は変更されません。上記 2 つでは回復できないほどクラスターが詰まっている場合や、ライフサイクルの tarball が `run-lifecycle.sh` が依存する SSM サイズ上限を超えた場合に使用します。

`UpdateClusterSoftware` は AMI が変更された場合にのみ再プロビジョニングし、既存の AMI 上でライフサイクルスクリプトの再実行を強制するためには使用できません。詳細なワークフローは [DEPLOYMENT.ja.md](DEPLOYMENT.ja.md#稼働中のクラスターへのライフサイクルスクリプト変更の適用) を参照してください。

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
