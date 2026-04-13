#!/bin/bash
# Step 1: Root setup for leisaac-runtime container.
# Run with: srun --partition gpu --container-image=nvcr.io/nvidia/pytorch:25.04-py3 \
#                --container-name=leisaac-runtime --container-remap-root bash /fsx/build/leisaac-runtime/setup-root.sh
set -euo pipefail

apt-get update && apt-get install -y \
    build-essential git cmake unzip curl ffmpeg libegl1 \
    libxt6 libglu1-mesa libxext6 && \
    rm -rf /var/lib/apt/lists/*

pip install --upgrade pip setuptools && pip install uv

# Persist env vars for subsequent container runs
set_env() { grep -q "^$1=" /etc/environment && sed -i "s|^$1=.*|$1=$2|" /etc/environment || echo "$1=$2" >> /etc/environment; }
set_env OMNI_KIT_ACCEPT_EULA YES
set_env PIP_CONSTRAINT ""
set_env NVIDIA_VISIBLE_DEVICES all
set_env NVIDIA_DRIVER_CAPABILITIES all
set_env LEISAAC_DIR /workspace/leisaac
set_env GR00T_DIR /workspace/gr00t

mkdir -p /app

echo "Root setup complete"
