[日本語](./README.ja.md) | English

# NVIDIA Isaac Lab Newton RL on Amazon SageMaker HyperPod

This directory contains sample scripts for running reinforcement learning training with NVIDIA Isaac Lab, the Newton physics backend, and RSL-RL on Amazon SageMaker HyperPod.

The default workload trains an ANYmal-D locomotion policy with PPO/RSL-RL on `Isaac-Velocity-Flat-Anymal-D-v0` using `presets=newton`.

## Demo

The clip below compares the initial checkpoint with a checkpoint trained using `NUM_ENVS=4096` and `MAX_ITERATIONS=1000`.

![Newton RL checkpoint comparison](docs/assets/newton-rl-demo.gif)

## Documentation

1. [Training](docs/en/training.md): Guide for building the Isaac Lab Newton container and running a short RSL-RL training job with Slurm + Enroot on AWS SageMaker HyperPod
