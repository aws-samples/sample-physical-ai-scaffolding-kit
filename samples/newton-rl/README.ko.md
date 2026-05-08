[日本語](./README.ja.md) | [English](./README.md) | 한국어

# NVIDIA Isaac Lab Newton RL on Amazon SageMaker HyperPod

이 디렉터리는 Amazon SageMaker HyperPod에서 NVIDIA Isaac Lab, Newton physics backend, RSL-RL을 사용해 RL training을 실행하기 위한 sample scripts를 제공합니다.

기본 workload는 `presets=newton`을 사용해 `Isaac-Velocity-Flat-Anymal-D-v0` task에서 PPO/RSL-RL 기반 ANYmal-D locomotion policy를 학습합니다.

## 데모

아래 클립은 initial checkpoint와 `NUM_ENVS=4096`, `MAX_ITERATIONS=1000`으로 학습한 checkpoint를 비교한 것입니다.

![Newton RL checkpoint comparison](docs/assets/newton-rl-demo.gif)

## 문서

1. [Training](docs/ko/training.md): AWS SageMaker HyperPod에서 Slurm + Enroot로 Isaac Lab Newton container를 build하고 짧은 RSL-RL training job을 실행하는 가이드
