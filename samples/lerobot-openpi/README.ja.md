日本語 | [English](./README.md) | [한국어](./README.ko.md)

# LeRobot π0.5 on Amazon SageMaker HyperPod

本ディレクトリには、Amazon SageMaker HyperPod 上で LeRobot の PyTorch 版 π0.5 を LoRA で finetuning するサンプルスクリプトがあります。
Slurm と Enroot を使い、`lerobot/libero` データセットを GPU 1 枚で学習します。

## ドキュメント一覧

1. [LeRobot π0.5 Training](docs/ja/lerobot_pi05_training.md): HyperPod 上で Slurm と Enroot を使い、Docker コンテナで π0.5 を LoRA で finetuning するガイド
