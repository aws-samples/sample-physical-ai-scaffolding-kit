日本語 | [English](./README.md) | [한국어](./README.ko.md)

# NVIDIA Isaac GR00T on Amazon SageMaker HyperPod

本ディレクトリは、Amazon SageMaker HyperPod上でNVIDIA Isaac GR00Tのモデルトレーニングを行う際のサンプルスクリプトになります。

## ドキュメント一覧

1. [Training](/samples/gr00t/docs/ja/training.md): AWS SageMaker HyperPod 上で Slurm + Enroot を使用して Docker コンテナでファインチューニングを実行するガイド

## モデルのライセンス

このサンプルの既定のベースモデル [`nvidia/GR00T-N1.6-3B`](https://huggingface.co/nvidia/GR00T-N1.6-3B) は、モデルカードで NVIDIA OneWay Noncommercial License が指定されており、非商用の利用に限られます。
本番環境や商用の用途で使う前に、商用利用できる NVIDIA Open Model License の [`nvidia/GR00T-N1.7-3B`](https://huggingface.co/nvidia/GR00T-N1.7-3B) への移行を検討します。
このサンプルは N1.6 を既定にしており、N1.7 での動作は確かめていません。
