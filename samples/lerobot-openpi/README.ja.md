日本語 | [English](./README.md) | [한국어](./README.ko.md)

# LeRobot π0.5 on Amazon SageMaker HyperPod

本ディレクトリには、Amazon SageMaker HyperPod 上で LeRobot の PyTorch 版 π0.5 を LoRA で finetuning するサンプルスクリプトがあります。
Slurm と Enroot を使い、`lerobot/libero` データセットを GPU 1 枚で学習します。
学習の設定、metrics、LoRA のアダプタは Amazon SageMaker AI の MLflow App に記録できます。

## ドキュメント一覧

1. [LeRobot π0.5 Training](docs/ja/lerobot_pi05_training.md): HyperPod 上で Slurm と Enroot を使い、Docker コンテナで π0.5 を LoRA で finetuning するガイド
2. [Training Advisor](docs/ja/training_advisor.md): MLflow に記録された学習の結果を診断し、次の学習パラメータの提案とレポートを作る skill `pask-training-advisor` の仕組みとレポートの見方
