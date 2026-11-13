[日本語](./README.ja.md) | [English](./README.md) | 한국어

# Amazon SageMaker HyperPod로 Slurm Cluster 구축하기

이 sample은 Amazon SageMaker HyperPod를 Slurm cluster로 사용하는 environment를 구축하는 방법을 보여줍니다.

## Architecture

Node 간 shared storage로 [Amazon FSx for Lustre](https://aws.amazon.com/ko/fsx/lustre/)를 사용합니다. Lustre는 [S3 bucket과 linked data repository로 연결](https://docs.aws.amazon.com/fsx/latest/LustreGuide/create-dra-linked-data-repo.html)되어 저장된 data에 쉽게 접근할 수 있습니다.

`cdk.json`의 `Mlflow.Enabled`를 `true`로 하면 training 기록 대상으로 Amazon SageMaker AI의 MLflow App(`pask-mlflow`)도 구축합니다. 기본값으로는 구축하지 않습니다.
MLflow App은 serverless이므로 사용하지 않는 동안에는 App 요금이 발생하지 않습니다.
Artifact를 저장하는 S3 bucket의 storage 및 request 요금은 별도로 발생합니다.
Training을 MLflow에 기록하는 절차는 [pi0 LoRA training](/samples/openpi-sample/docs/ko/pi0_LoRA_training.md)과 [LeRobot을 사용한 pi0.5 training](/samples/lerobot-openpi/docs/ko/lerobot_pi05_training.md) 문서를 참고하세요.

![Architecture](/hyperpod/docs/architecture.png)

## Table of Contents

1. [Deployment](/hyperpod/docs/ko/DEPLOYMENT.md)
1. [Cleanup](/hyperpod/docs/ko/CLEANUP.md)
