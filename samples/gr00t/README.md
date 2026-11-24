[日本語](./README.ja.md) | English | [한국어](./README.ko.md)

# NVIDIA Isaac GR00T on Amazon SageMaker HyperPod

This directory contains sample scripts for running NVIDIA Isaac GR00T model training on Amazon SageMaker HyperPod.

## Documents

1. [Training](/samples/gr00t/docs/en/training.md): Guide for running fine-tuning in Docker containers using Slurm + Enroot on AWS SageMaker HyperPod

## Model License

The default base model of this sample, [`nvidia/GR00T-N1.6-3B`](https://huggingface.co/nvidia/GR00T-N1.6-3B), is licensed under the NVIDIA OneWay Noncommercial License on its model card, so its use is limited to non-commercial purposes.
Before you use it in production or for commercial purposes, consider moving to [`nvidia/GR00T-N1.7-3B`](https://huggingface.co/nvidia/GR00T-N1.7-3B), which is licensed under the NVIDIA Open Model License and allows commercial use.
This sample uses N1.6 by default and has not been tested with N1.7.
