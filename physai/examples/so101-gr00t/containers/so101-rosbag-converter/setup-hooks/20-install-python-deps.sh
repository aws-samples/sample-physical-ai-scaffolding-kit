#!/bin/bash
# Install Python deps for rosbag2 → LeRobot v3.0 conversion (CPU-only).
# rosbags provides pure-Python rosbag2 reading without requiring a full ROS2
# installation; mcap storage support is bundled with rosbags itself from
# 0.10 onwards (no separate rosbags-mcap package needed / it does not exist
# on PyPI). Pillow decodes the JPEG payload of sensor_msgs/CompressedImage.
#
# lerobot 0.6+ requires Python 3.12 (which this container already ships) and
# the `[dataset]` extra for the writer path (LeRobotDataset.create → add_frame
# → save_episode → finalize). torch is pulled in transitively; force CPU
# wheels since the converter runs on the cpu partition.
set -euo pipefail

pip install --no-cache-dir \
    --extra-index-url https://download.pytorch.org/whl/cpu \
    "lerobot[dataset]==${LEROBOT_VERSION}" \
    "rosbags>=0.10" \
    "Pillow" \
    "pyyaml"

echo "Python deps installed"
