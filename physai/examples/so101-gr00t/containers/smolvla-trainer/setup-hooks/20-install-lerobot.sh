#!/bin/bash
# Install LeRobot with the SmolVLA extras and pre-download smolvla_base.
#
# CPU build: the trainer container is built on the cpu partition (no GPU
# needed for pip installs or hf downloads). torch is pulled with the cu128
# wheels so runtime works on GPU nodes.
set -euo pipefail

mkdir -p "$SMOLVLA_HOME"

git clone "$LEROBOT_REPO" "$SMOLVLA_HOME/lerobot"
cd "$SMOLVLA_HOME/lerobot"
git checkout "$LEROBOT_REF"

# lerobot 0.6+ requires Python >= 3.12
uv venv --python 3.12 "$SMOLVLA_HOME/.venv"
# shellcheck disable=SC1091
source "$SMOLVLA_HOME/.venv/bin/activate"

# CUDA-enabled torch — matches leisaac-runtime's driver expectation (cu128).
uv pip install --no-cache-dir torch==2.7.0 torchvision==0.22.0 \
    --index-url https://download.pytorch.org/whl/cu128
uv cache prune || true

# lerobot 0.6 requires the `training` meta-extra for `lerobot-train`
# (bundles dataset + accelerate + wandb per the official docstring), plus
# the `smolvla` extra for the policy itself.
uv pip install --no-cache-dir -e ".[smolvla,training]"
uv cache prune || true

# Pre-download smolvla_base into the container so training doesn't need
# outbound HTTPS on the compute node. (`huggingface-cli` was deprecated in
# huggingface_hub 0.36; the replacement CLI is `hf`.)
"$SMOLVLA_HOME/.venv/bin/hf" download \
    "$SMOLVLA_MODEL" --local-dir "$SMOLVLA_HOME/smolvla_base"

deactivate

rm -rf "$SMOLVLA_HOME/lerobot/.git"

echo "SmolVLA installed at $SMOLVLA_HOME"
