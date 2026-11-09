[日本語](./README.ja.md) | English | [한국어](./README.ko.md)

# Building a Slurm Cluster with Amazon SageMaker HyperPod

This sample demonstrates how to build an environment using Amazon SageMaker HyperPod as a Slurm cluster.

## Architecture

[Amazon FSx for Lustre](https://aws.amazon.com/fsx/lustre/) is used as the shared storage for nodes. Lustre is [linked to an S3 bucket](https://docs.aws.amazon.com/fsx/latest/LustreGuide/create-dra-linked-data-repo.html), making it easy to access stored data.

The stack also creates an MLflow App (`pask-mlflow`) in Amazon SageMaker AI to track training runs.
The MLflow App is serverless, so the App itself is not billed while idle.
Storage and request charges for the S3 bucket that holds the artifacts are billed separately.
For the steps to track training in MLflow, see the [pi0 LoRA training](/samples/openpi-sample/docs/en/pi0_LoRA_training.md) and [pi0.5 training with LeRobot](/samples/lerobot-openpi/docs/en/lerobot_pi05_training.md) guides.

You can also connect this cluster with VAMS (Visual Asset Management System) so that VAMS workflows run LeRobot training on it.
Setting `Vams.Enabled` to `true` in cdk.json creates an SQS queue, a DLQ, and a DynamoDB table, and adds `VamsQueueUrl`, `VamsQueueArn`, `VamsDeadLetterQueueUrl`, and `VamsTableName` to the Outputs. When `Vams.SeparateLoginRole` is `true`, a dedicated role for the login group is also created and `LoginExecutionRoleARN` is added to the Outputs. The role of the login group cannot be changed on an existing cluster, so set it to `false` in that case.
For the steps, see [Setting Up and Operating the VAMS Integration](/hyperpod/docs/en/VAMS_INTEGRATION/OVERVIEW.md).

![Architecture](/hyperpod/docs/architecture.png)

## Table of Contents

1. [Deployment](/hyperpod/docs/en/DEPLOYMENT.md)
1. [Setting Up and Operating the VAMS Integration](/hyperpod/docs/en/VAMS_INTEGRATION/OVERVIEW.md)
1. [How the VAMS Integration Works](/hyperpod/docs/en/VAMS_INTEGRATION/ARCHITECTURE.md)
1. [VAMS Integration Specification](/hyperpod/docs/en/VAMS_INTEGRATION/SPEC.md)
1. [Cleanup](/hyperpod/docs/en/CLEANUP.md)
