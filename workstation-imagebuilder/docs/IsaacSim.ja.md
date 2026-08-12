# Isaac Sim / Isaac Lab イメージ

日本語 | [English](IsaacSim.md)

## 同梱イメージ定義

| イメージ名 | Isaac Sim | Isaac Lab | Python |
|-----------|-----------|-----------|--------|
| [`isaacsim6.0`](../lib/imagebuilder/isaacsim6.0/) | 6.0.1 | v3.0.0-beta2.patch1 | 3.12 |
| [`isaacsim5.1`](../lib/imagebuilder/isaacsim5.1/) | 5.1.0 | v2.3.2 | 3.11 |

### AMI に含まれるもの

- NVIDIA GPU ドライバ + CUDA Toolkit
- NICE DCV サーバ + 自動コンソールセッション
- NVIDIA Isaac Sim（pip パッケージ）
- NVIDIA Isaac Lab
- ROS2 Jazzy (Desktop + rosbridge)
- AWS SSM Agent
- Miniconda（Python 環境管理）

## Isaac Sim の起動

DCV でリモートデスクトップに接続した後、ターミナルを開きます。

### Isaac Sim 6.0

```bash
conda activate env_isaaclab
isaacsim
```

### Isaac Sim 5.1

```bash
conda activate env_isaaclab
isaacsim
```

初回の起動ではアセットの読み込みに数分かかります。

## Isaac Lab の実行

### Isaac Sim 6.0 (Isaac Lab v3.x)

Isaac Lab 3.x ではデフォルトがヘッドレスモードのため、GUI を表示するには `--viz kit` を指定します。

```bash
conda activate env_isaaclab
cd ~/IsaacLab

# チュートリアル: 空のシミュレーション環境を起動
python scripts/tutorials/00_sim/create_empty.py --viz kit

# 強化学習: Ant の歩行トレーニング
./isaaclab.sh train --rl_library rsl_rl --task Isaac-Ant-v0 --viz kit
```

### Isaac Sim 5.1 (Isaac Lab v2.x)

```bash
conda activate env_isaaclab
cd ~/IsaacLab

# チュートリアル: 空のシミュレーション環境を起動
python scripts/tutorials/00_sim/create_empty.py

# 強化学習: Ant の歩行トレーニング
python scripts/reinforcement_learning/rsl_rl/train.py --task=Isaac-Ant-v0
```

## ROS2 との連携

```bash
conda activate env_isaaclab
export ROS_DISTRO=jazzy
export RMW_IMPLEMENTATION=rmw_fastrtps_cpp
isaacsim
```

別のターミナルで:

```bash
source /opt/ros/jazzy/setup.bash
ros2 topic list
```
