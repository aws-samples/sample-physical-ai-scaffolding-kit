[日本語](./README.ja.md) | English | [한국어](./README.ko.md)

# Building a Slurm Cluster with Amazon SageMaker HyperPod

This sample demonstrates how to build an environment using Amazon SageMaker HyperPod as a Slurm cluster.

## Architecture

[Amazon FSx for Lustre](https://aws.amazon.com/fsx/lustre/) is used as the shared storage for nodes. Lustre is [linked to an S3 bucket](https://docs.aws.amazon.com/fsx/latest/LustreGuide/create-dra-linked-data-repo.html), making it easy to access stored data.

The stack also creates an MLflow App (`pask-mlflow`) in Amazon SageMaker AI to track training runs.
The MLflow App is serverless, so the App itself is not billed while idle.
Storage and request charges for the S3 bucket that holds the artifacts are billed separately.
For the steps to track training in MLflow, see the [pi0 LoRA training](/samples/openpi-sample/docs/en/pi0_LoRA_training.md) and [pi0.5 training with LeRobot](/samples/lerobot-openpi/docs/en/lerobot_pi05_training.md) guides.

![Architecture](/hyperpod/docs/architecture.png)

## Table of Contents

1. [Deployment](/hyperpod/docs/en/DEPLOYMENT.md)
1. [Cleanup](/hyperpod/docs/en/CLEANUP.md)
