日本語 | [English](./README.md) | [한국어](./README.ko.md)

# NVIDIA Isaac Lab Newton RL on Amazon SageMaker HyperPod

本ディレクトリは、Amazon SageMaker HyperPod 上で NVIDIA Isaac Lab、Newton 物理バックエンド、RSL-RL を使った強化学習トレーニングを実行するためのサンプルスクリプトです。

デフォルトのワークロードは、`presets=newton` を使用し、`Isaac-Velocity-Flat-Anymal-D-v0` 上で PPO/RSL-RL による ANYmal-D の歩行ポリシーを学習します。

## デモ

以下のクリップは、初期チェックポイントと `NUM_ENVS=4096`、`MAX_ITERATIONS=1000` で学習したチェックポイントを比較したものです。

![Newton RL checkpoint comparison](docs/assets/newton-rl-demo.gif)

## ドキュメント一覧

1. [Training](docs/ja/training.md): AWS SageMaker HyperPod 上で Slurm + Enroot を使用して Isaac Lab Newton コンテナをビルドし、短い RSL-RL トレーニングジョブを実行するガイド
