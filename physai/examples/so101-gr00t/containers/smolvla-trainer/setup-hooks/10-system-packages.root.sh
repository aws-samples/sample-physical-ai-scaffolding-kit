#!/bin/bash
# Install system packages needed for SmolVLA training.
set -euo pipefail

apt-get update && apt-get install -y --no-install-recommends \
    build-essential git curl ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# Install uv (matches other physai trainer containers)
curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh

echo "System packages installed"
