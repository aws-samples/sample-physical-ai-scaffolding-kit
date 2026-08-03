# Isaac Sim / Isaac Lab Image

[日本語](IsaacSim.ja.md) | English

## Included Image Definitions

| Image Name | Isaac Sim | Isaac Lab | Python |
|-----------|-----------|-----------|--------|
| [`isaacsim6.0`](../lib/imagebuilder/isaacsim6.0/) | 6.0.1 | v3.0.0-beta2.patch1 | 3.12 |
| [`isaacsim5.1`](../lib/imagebuilder/isaacsim5.1/) | 5.1.0 | release/2.3.0 | 3.11 |

### Included in the AMI

- NVIDIA GPU drivers + CUDA Toolkit
- NICE DCV server + automatic console session
- NVIDIA Isaac Sim (pip package)
- NVIDIA Isaac Lab
- ROS2 Jazzy (Desktop + rosbridge)
- AWS SSM Agent
- Miniconda (Python environment management)

## Launching Isaac Sim

After connecting to the remote desktop via DCV, open a terminal.

### Isaac Sim 6.0

```bash
conda activate env_isaaclab
export OMNI_KIT_ACCEPT_EULA=yes
isaacsim
```

### Isaac Sim 5.1

```bash
conda activate env_isaaclab
export OMNI_KIT_ACCEPT_EULA=yes
python -m isaacsim
```

First launch may take several minutes while assets are loaded.

## Running Isaac Lab

### Isaac Sim 6.0 (Isaac Lab v3.x)

Isaac Lab 3.x defaults to headless mode. Add `--viz kit` to display the GUI.

```bash
conda activate env_isaaclab
cd ~/IsaacLab

# Tutorial: Launch an empty simulation environment
python scripts/tutorials/00_sim/create_empty.py --viz kit

# Reinforcement learning: Ant locomotion training
./isaaclab.sh train --rl_library rsl_rl --task Isaac-Ant-v0 --viz kit
```

### Isaac Sim 5.1 (Isaac Lab v2.x)

```bash
conda activate env_isaaclab
cd ~/IsaacLab

# Tutorial: Launch an empty simulation environment
python scripts/tutorials/00_sim/create_empty.py

# Reinforcement learning: Ant locomotion training
python scripts/reinforcement_learning/rsl_rl/train.py --task=Isaac-Ant-v0
```

## ROS2 Integration

```bash
conda activate env_isaaclab
export OMNI_KIT_ACCEPT_EULA=yes
export ROS_DISTRO=jazzy
export RMW_IMPLEMENTATION=rmw_fastrtps_cpp
isaacsim
```

In a separate terminal:

```bash
source /opt/ros/jazzy/setup.bash
ros2 topic list
```
