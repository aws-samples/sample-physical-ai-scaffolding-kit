日本語 | [English](./README.md) | [한국어](./README.ko.md)

# OpenPI on Amazon SageMaker HyperPod

本ディレクトリは、Amazon SageMaker HyperPod上でOpenPI（Physical Intelligence）のモデルトレーニングや推論実行サンプルスクリプトになります。

## ドキュメント一覧

1. [LoRA Training](docs/ja/pi0_LoRA_training.md): AWS SageMaker HyperPod 上で Slurm + Enroot を使用して Docker コンテナで LoRA ファインチューニングを実行するガイド

## モデルのライセンス

π0 / π0.5 の重みは PaliGemma をもとにしており、[Gemma Terms of Use](https://gemma.ai/resources/terms/) の対象です。
Gemma Terms of Use の利用制限は、ファインチューニングしたモデルにも引き継がれます。
学習したモデルを使う、配布する前に、Gemma Terms of Use と、そこから参照される利用禁止のポリシーを確かめます。
