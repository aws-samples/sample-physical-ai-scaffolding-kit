#!/bin/bash
# Install Python deps for rosbag2 → LeRobot conversion (CPU-only).
# rosbags provides pure-Python rosbag2 reading without requiring a full ROS2
# installation; mcap storage support is bundled with rosbags itself from
# 0.10 onwards (no separate rosbags-mcap package needed / it does not exist
# on PyPI). Pillow decodes the JPEG payload of sensor_msgs/CompressedImage.
set -euo pipefail

pip install --no-cache-dir \
    --extra-index-url https://download.pytorch.org/whl/cpu \
    "lerobot==${LEROBOT_VERSION}" \
    "rosbags>=0.10" \
    "Pillow" \
    "pyyaml"

echo "Python deps installed"
