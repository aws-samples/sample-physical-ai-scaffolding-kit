[日本語](./README.ja.md) | English | [한국어](./README.ko.md)

# LeRobot π0.5 on Amazon SageMaker HyperPod

This directory contains sample scripts for fine-tuning the π0.5 (and π0) policy of Hugging Face LeRobot on a single GPU of Amazon SageMaker HyperPod. The training config, metrics, and LoRA adapter can be recorded to an Amazon SageMaker AI MLflow App.

## Documentation

1. [π0.5 Fine-Tuning](docs/en/lerobot_pi05_training.md): Guide for running LeRobot π0.5 fine-tuning (LoRA by default) in Docker containers using Slurm + Enroot on AWS SageMaker HyperPod
2. [Training Advisor](docs/en/training_advisor.md): How the `pask-training-advisor` skill diagnoses training results recorded in MLflow and creates a proposal for the next training parameters and a report, and how to read the report
