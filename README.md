[日本語](./README.ja.md) | English

# Physical AI Scaffolding Kit

This is a sample repository introduced in the [Physical AI Development Support Program by AWS Japan](https://aws.amazon.com/jp/blogs/news/aws-japan-physical-ai-development-support-program/).

This repository contains the following samples.

## Environment Setup

When working with limited local resources, efficiently iterating on training can become a challenge.
Amazon SageMaker HyperPod eliminates the undifferentiated heavy lifting associated with building generative AI models, enabling you to build models efficiently with abundant resources.

* [Building a Slurm Cluster with Amazon SageMaker HyperPod](/hyperpod/README.md)
  * If you only want to build a standard Slurm cluster, please refer to this sample to set up your Amazon SageMaker HyperPod environment
* [Physical AI Pipeline Platform SDK](physai/README.md)
  * If you want to use a pipeline that utilizes the SDK developed for Amazon SageMaker HyperPod, please refer to this sample to set up your environment

## Samples for Amazon SageMaker HyperPod

1. [π0 Sample](samples/openpi-sample/README.md)
2. [NVIDIA Isaac GR00T Sample](/samples/gr00t/README.md)
3. [NVIDIA Isaac Lab Newton RL Sample](/samples/newton-rl/README.md)
