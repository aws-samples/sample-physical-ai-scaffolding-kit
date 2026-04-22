#!/bin/bash
# Install system packages needed for GR00T training.
set -euo pipefail

apt-get update && apt-get install -y --no-install-recommends \
    build-essential git cmake curl ffmpeg libaio-dev && \
    rm -rf /var/lib/apt/lists/*

# Install uv
curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh

echo "System packages installed"
