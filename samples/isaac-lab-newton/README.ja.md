日本語 | [English](./README.md)

# NVIDIA Isaac Lab Newton RL on Amazon SageMaker HyperPod

本ディレクトリは、Amazon SageMaker HyperPod 上で NVIDIA Isaac Lab、Newton 物理バックエンド、RSL-RL を使った強化学習トレーニングを実行するためのサンプルスクリプトです。

デフォルトのワークロードは、`presets=newton` を使用し、`Isaac-Velocity-Flat-Anymal-D-v0` 上で PPO/RSL-RL による ANYmal-D の歩行ポリシーを学習します。

## ドキュメント一覧

1. [Training](docs/ja/training.md): AWS SageMaker HyperPod 上で Slurm + Enroot を使用して Isaac Lab Newton コンテナをビルドし、短い RSL-RL トレーニングジョブを実行するガイド
