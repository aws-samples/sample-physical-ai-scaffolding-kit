[日本語](./README.ja.md) | [English](./README.md) | 한국어

# Amazon SageMaker HyperPod로 Slurm Cluster 구축하기

이 sample은 Amazon SageMaker HyperPod를 Slurm cluster로 사용하는 environment를 구축하는 방법을 보여줍니다.

## Architecture

Node 간 shared storage로 [Amazon FSx for Lustre](https://aws.amazon.com/ko/fsx/lustre/)를 사용합니다. Lustre는 [S3 bucket과 linked data repository로 연결](https://docs.aws.amazon.com/fsx/latest/LustreGuide/create-dra-linked-data-repo.html)되어 저장된 data에 쉽게 접근할 수 있습니다.

![Architecture](/hyperpod/docs/architecture.png)

## Table of Contents

1. [Deployment](/hyperpod/docs/ko/DEPLOYMENT.md)
1. [Cleanup](/hyperpod/docs/ko/CLEANUP.md)
