#!/bin/bash
# Install system packages needed for rosbag2 → LeRobot conversion.
set -euo pipefail

apt-get update && apt-get install -y --no-install-recommends \
    build-essential pkg-config ffmpeg && \
    rm -rf /var/lib/apt/lists/*

echo "System packages installed"
