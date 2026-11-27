[日本語](./README.ja.md) | [English](./README.md) | 한국어

# LeRobot π0.5 on Amazon SageMaker HyperPod

이 디렉터리는 Amazon SageMaker HyperPod에서 Hugging Face LeRobot의 PyTorch 버전 π0.5 (π0) policy를 fine-tuning하기 위한 sample scripts를 제공합니다. Container build, Enroot import, dataset 준비, 1 GPU training을 Slurm job으로 실행하며, 기본 finetuning 방식은 PEFT LoRA입니다. 학습 설정, metrics, LoRA adapter는 Amazon SageMaker AI MLflow App에 기록할 수 있습니다.

## Documentation

1. [π0.5 Training](docs/ko/lerobot_pi05_training.md): AWS SageMaker HyperPod에서 Slurm + Enroot로 LeRobot의 π0.5를 fine-tuning하는 가이드
2. [Training Advisor](docs/ko/training_advisor.md): MLflow에 기록된 학습 결과를 진단하고 다음 학습 파라미터의 제안과 report를 만드는 skill `pask-training-advisor`의 구조와 report 보는 법
