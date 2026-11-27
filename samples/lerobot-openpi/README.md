[日本語](./README.ja.md) | English | [한국어](./README.ko.md)

# LeRobot π0.5 on Amazon SageMaker HyperPod

This directory contains sample scripts for fine-tuning the π0.5 (and π0) policy of Hugging Face LeRobot on a single GPU of Amazon SageMaker HyperPod. The training config, metrics, and LoRA adapter can be recorded to an Amazon SageMaker AI MLflow App.

## Documentation

1. [π0.5 Fine-Tuning](docs/en/lerobot_pi05_training.md): Guide for running LeRobot π0.5 fine-tuning (LoRA by default) in Docker containers using Slurm + Enroot on AWS SageMaker HyperPod

## Model License

The π0 / π0.5 weights are based on PaliGemma and are subject to the [Gemma Terms of Use](https://gemma.ai/resources/terms/).
The use restrictions of the Gemma Terms of Use also apply to fine-tuned models.
Before you use or distribute a trained model, review the Gemma Terms of Use and the prohibited use policy that it references.
