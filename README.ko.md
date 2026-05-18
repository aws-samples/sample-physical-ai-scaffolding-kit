[日本語](./README.ja.md) | [English](./README.md) | 한국어

# Physical AI Scaffolding Kit

이 repository는 [Physical AI Development Support Program by AWS Japan](https://aws.amazon.com/jp/blogs/news/aws-japan-physical-ai-development-support-program/)에서 소개된 sample repository입니다.

이 repository에는 다음 sample이 포함되어 있습니다.

## Environment Setup

로컬 resource가 제한된 환경에서는 training을 빠르게 반복하는 일이 어려울 수 있습니다.
Amazon SageMaker HyperPod는 generative AI model 구축에 필요한 undifferentiated heavy lifting을 줄이고, 충분한 resource를 사용해 model을 효율적으로 구축할 수 있게 해줍니다.

* [Amazon SageMaker HyperPod로 Slurm Cluster 구축하기](/hyperpod/README.ko.md)
  * 표준 Slurm cluster만 구축하려는 경우, 이 sample을 참고하여 Amazon SageMaker HyperPod 환경을 구축하세요
* [Physical AI Pipeline Platform SDK](physai/README.md)
  * Amazon SageMaker HyperPod용으로 개발된 SDK를 활용한 pipeline을 사용하려는 경우, 이 sample을 참고하여 환경을 구축하세요
* [NVIDIA IsaacSim Development Workstation](/isaacsim-workstation/README.ko.md)
  * EC2 instance에서 NVIDIA Isaac Sim을 사용하기 위한 sample입니다
  * NVIDIA Isaac Sim / Lab 환경을 이용할 수 있습니다

## Samples for Amazon SageMaker HyperPod

1. [π0 Sample](samples/openpi-sample/README.ko.md)
2. [NVIDIA Isaac GR00T Sample](/samples/gr00t/README.ko.md)
3. [NVIDIA Isaac Lab Newton RL Sample](/samples/newton-rl/README.ko.md)
