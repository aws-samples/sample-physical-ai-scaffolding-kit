# Amazon SageMaker HyperPodを使ったSlurmクラスタの構築

このサンプルではSlurmクラスタとして Amazon SageMaker HyperPod を利用した環境を構築する方法について紹介します。

## 構築されるアーキテクチャ

node の共有ストレージには [Amazon FSx for Lustre](https://aws.amazon.com/jp/fsx/lustre/) を利用します。Lustre では [S3のバケットとリンク](https://docs.aws.amazon.com/ja_jp/fsx/latest/LustreGuide/create-dra-linked-data-repo.html)しており、蓄積したデータを簡単に利用でき様になっています。

![Architecture](/hyperpod/docs/architecture.png)

## 目次

1. [デプロイについて](/hyperpod/docs/DEPLOYMENT.md)
1. [後片付け](/hyperpod/docs/CLEANUP.md)
