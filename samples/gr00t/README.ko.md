[日本語](./README.ja.md) | [English](./README.md) | 한국어

# NVIDIA Isaac GR00T on Amazon SageMaker HyperPod

이 디렉터리는 Amazon SageMaker HyperPod에서 NVIDIA Isaac GR00T model training을 실행하기 위한 sample scripts를 제공합니다.

## Documents

1. [Training](/samples/gr00t/docs/ko/training.md): AWS SageMaker HyperPod에서 Slurm + Enroot로 Docker container fine-tuning을 실행하는 가이드

## 모델 라이선스

이 sample의 기본 base model인 [`nvidia/GR00T-N1.6-3B`](https://huggingface.co/nvidia/GR00T-N1.6-3B)는 model card에 NVIDIA OneWay Noncommercial License가 지정되어 있어, 비상업적 용도로만 사용할 수 있습니다.
Production 환경이나 상업적 용도로 사용하기 전에, 상업적 이용이 가능한 NVIDIA Open Model License의 [`nvidia/GR00T-N1.7-3B`](https://huggingface.co/nvidia/GR00T-N1.7-3B)로 전환하는 것을 검토합니다.
이 sample은 N1.6을 기본으로 하며, N1.7에서의 동작은 확인하지 않았습니다.
