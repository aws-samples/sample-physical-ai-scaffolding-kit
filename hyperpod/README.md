[日本語](./README.ja.md) | English

# Building a Slurm Cluster with Amazon SageMaker HyperPod

This sample demonstrates how to build an environment using Amazon SageMaker HyperPod as a Slurm cluster.

## Architecture

[Amazon FSx for Lustre](https://aws.amazon.com/fsx/lustre/) is used as the shared storage for nodes. Lustre is [linked to an S3 bucket](https://docs.aws.amazon.com/fsx/latest/LustreGuide/create-dra-linked-data-repo.html), making it easy to access stored data.

![Architecture](/hyperpod/docs/architecture.png)

## Table of Contents

1. [Deployment](/hyperpod/docs/en/DEPLOYMENT.md)
1. [Cleanup](/hyperpod/docs/en/CLEANUP.md)
