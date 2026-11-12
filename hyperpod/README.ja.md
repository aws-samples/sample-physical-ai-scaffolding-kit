日本語 | [English](./README.md) | [한국어](./README.ko.md)

# Amazon SageMaker HyperPodを使ったSlurmクラスタの構築

このサンプルではSlurmクラスタとして Amazon SageMaker HyperPod を利用した環境を構築する方法について紹介します。

## 構築されるアーキテクチャ

node の共有ストレージには [Amazon FSx for Lustre](https://aws.amazon.com/jp/fsx/lustre/) を利用します。Lustre では [S3のバケットとリンク](https://docs.aws.amazon.com/ja_jp/fsx/latest/LustreGuide/create-dra-linked-data-repo.html)しており、蓄積したデータを簡単に利用でき様になっています。

`cdk.json` の `Mlflow.Enabled` を `true` にすると、学習の記録先として Amazon SageMaker AI の MLflow App（`pask-mlflow`）も構築します。既定では構築しません。
MLflow App はサーバーレスで、使っていない間は App の料金がかかりません。
artifact を保存する S3 バケットのストレージとリクエストの料金は別にかかります。
学習を MLflow に記録する手順は、[pi0 の LoRA 学習](/samples/openpi-sample/docs/ja/pi0_LoRA_training.md)と [LeRobot での pi0.5 の学習](/samples/lerobot-openpi/docs/ja/lerobot_pi05_training.md)の手順書を参照してください。

VAMS（Visual Asset Management System）のワークフローから、このクラスタで LeRobot の学習を実行する連携も構築できます。
cdk.json の `Vams.Enabled` を `true` にすると、SQS キュー、DLQ、DynamoDB テーブルを作り、Outputs に `VamsQueueUrl`、`VamsQueueArn`、`VamsDeadLetterQueueUrl`、`VamsTableName` が加わります。`Vams.SeparateLoginRole` が `true` の場合は login group 専用のロールも作り、Outputs に `LoginExecutionRoleARN` が加わります。作ってあるクラスタでは login group のロールを変えられないため、`false` にします。
手順は [VAMS 連携の導入と運用](/hyperpod/docs/ja/VAMS_INTEGRATION/OVERVIEW.md) を参照してください。

![Architecture](/hyperpod/docs/architecture.png)

## 目次

1. [デプロイについて](/hyperpod/docs/ja/DEPLOYMENT.md)
1. [VAMS 連携の導入と運用](/hyperpod/docs/ja/VAMS_INTEGRATION/OVERVIEW.md)
1. [VAMS 連携の仕組み](/hyperpod/docs/ja/VAMS_INTEGRATION/ARCHITECTURE.md)
1. [VAMS 連携の仕様](/hyperpod/docs/ja/VAMS_INTEGRATION/SPEC.md)
1. [後片付け](/hyperpod/docs/ja/CLEANUP.md)
