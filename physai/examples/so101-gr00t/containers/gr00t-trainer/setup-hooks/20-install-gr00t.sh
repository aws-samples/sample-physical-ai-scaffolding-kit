#!/bin/bash
# Install Isaac-GR00T following official dGPU install procedure.
set -euo pipefail

git clone https://github.com/NVIDIA/Isaac-GR00T.git "$GR00T_DIR"
cd "$GR00T_DIR"
git checkout "$GR00T_REF"

uv sync
uv pip install -e .

rm -rf .git

echo "GR00T installed"
