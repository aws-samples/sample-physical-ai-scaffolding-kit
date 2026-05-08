日本語 | [English](./README.md) | [한국어](./README.ko.md)

# Physical AI Scaffolding Kit

[フィジカル AI 開発支援プログラム by AWS ジャパン](https://aws.amazon.com/jp/blogs/news/aws-japan-physical-ai-development-support-program/) で紹介されているサンプルリポジトリです。

このリポジトリには以下のサンプルを含みます。

## 環境構築

ローカルの限られたリソースの中では、効率的に速くトレーニングを繰り返すことが課題になることがあります。
Amazon SageMaker HyperPod は、生成 AI モデルの構築に伴う、差別化につながらない手間のかかる作業をなくし、豊富なリソースを使いながら効率的にモデルの構築を進めることができます。

* [Amazon SageMaker HyperPodを使ったSlurmクラスタの構築](/hyperpod/README.ja.md)
  * 標準的なSlurmクラスタのみを構築したい場合は、このサンプルを参照してAmazon SageMaker HyperPodの環境を構築してください
* [Physical AI Pipeline Platform SDK](physai/README.ja.md)
  * Amazon SageMaker HyperPod 向けに開発されたSDKを利用したパイプラインを使いたい場合は、このサンプルを参照して環境を構築してください

## Amazon SageMaker HyperPod 向けのサンプル

1. [π0 サンプル](samples/openpi-sample/README.ja.md)
2. [NVIDIA Isaac GR00T サンプル](/samples/gr00t/README.ja.md)
3. [NVIDIA Isaac Lab Newton RL サンプル](/samples/newton-rl/README.ja.md)
