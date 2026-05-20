#!/bin/bash
# Install Python deps for HDF5 → LeRobot conversion (CPU-only, no Isaac Lab).
# lerobot pulls in compatible torch/torchvision/torchcodec/numpy transitively;
# we only pin the packages we directly touch. --extra-index-url resolves
# torch/torchvision/torchcodec to their +cpu wheels.
set -euo pipefail

pip install --no-cache-dir \
    --extra-index-url https://download.pytorch.org/whl/cpu \
    "lerobot==${LEROBOT_VERSION}" \
    "h5py" \
    "tqdm" \
    "pyyaml"

echo "Python deps installed"
