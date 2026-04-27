#!/bin/bash
# Install system packages needed for HDF5 → LeRobot conversion.
set -euo pipefail

apt-get update && apt-get install -y --no-install-recommends \
    build-essential pkg-config ffmpeg libhdf5-dev && \
    rm -rf /var/lib/apt/lists/*

echo "System packages installed"
