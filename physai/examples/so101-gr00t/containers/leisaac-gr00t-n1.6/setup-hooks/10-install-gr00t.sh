#!/bin/bash
# Install Isaac-GR00T in a separate venv.
set -euo pipefail
export TERM=xterm

git clone https://github.com/NVIDIA/Isaac-GR00T.git "$GR00T_DIR"
cd "$GR00T_DIR"
git checkout "$GR00T_REF"

uv sync
uv pip install -e .
uv pip install flash-attn --no-build-isolation

rm -rf .git

echo "GR00T installed"
