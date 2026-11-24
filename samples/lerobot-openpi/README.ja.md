日本語 | [English](./README.md) | [한국어](./README.ko.md)

# LeRobot π0.5 on Amazon SageMaker HyperPod

本ディレクトリには、Amazon SageMaker HyperPod 上で LeRobot の PyTorch 版 π0.5 を LoRA で finetuning するサンプルスクリプトがあります。
Slurm と Enroot を使い、`lerobot/libero` データセットを GPU 1 枚で学習します。

## ドキュメント一覧

1. [LeRobot π0.5 Training](docs/ja/lerobot_pi05_training.md): HyperPod 上で Slurm と Enroot を使い、Docker コンテナで π0.5 を LoRA で finetuning するガイド

## モデルのライセンス

π0 / π0.5 の重みは PaliGemma をもとにしており、[Gemma Terms of Use](https://gemma.ai/resources/terms/) の対象です。
Gemma Terms of Use の利用制限は、ファインチューニングしたモデルにも引き継がれます。
学習したモデルを使う、配布する前に、Gemma Terms of Use と、そこから参照される利用禁止のポリシーを確かめます。
