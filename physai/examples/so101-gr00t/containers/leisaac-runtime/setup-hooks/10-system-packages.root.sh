#!/bin/bash
# Install system packages and uv.
set -euo pipefail

apt-get update && apt-get install -y \
    build-essential git cmake unzip curl ffmpeg libegl1 \
    libxt6 libglu1-mesa libxext6 && \
    rm -rf /var/lib/apt/lists/*

curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh

echo "System packages installed"
