[日本語](./README.ja.md) | [English](./README.md) | 한국어

# LeRobot π0.5 on Amazon SageMaker HyperPod

이 디렉터리는 Amazon SageMaker HyperPod에서 Hugging Face LeRobot의 PyTorch 버전 π0.5 (π0) policy를 fine-tuning하기 위한 sample scripts를 제공합니다. Container build, Enroot import, dataset 준비, 1 GPU training을 Slurm job으로 실행하며, 기본 finetuning 방식은 PEFT LoRA입니다. 학습 설정, metrics, LoRA adapter는 Amazon SageMaker AI MLflow App에 기록할 수 있습니다.

## Documentation

1. [π0.5 Training](docs/ko/lerobot_pi05_training.md): AWS SageMaker HyperPod에서 Slurm + Enroot로 LeRobot의 π0.5를 fine-tuning하는 가이드

## 모델 라이선스

π0 / π0.5의 weights는 PaliGemma를 기반으로 하며, [Gemma Terms of Use](https://gemma.ai/resources/terms/)의 대상입니다.
Gemma Terms of Use의 이용 제한은 finetuning한 model에도 그대로 적용됩니다.
학습한 model을 사용하거나 배포하기 전에, Gemma Terms of Use와 그 안에서 참조하는 금지 사용 정책을 확인해 주십시오.
