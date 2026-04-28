#!/bin/bash
# Install Isaac-GR00T N1.5 in a separate venv.
set -euo pipefail
export TERM=xterm

git clone https://github.com/NVIDIA/Isaac-GR00T.git "$GR00T_DIR"
cd "$GR00T_DIR"
git checkout "$GR00T_N15_REF"

uv venv --python 3.11
uv pip install -e ".[base]"
uv pip install flash-attn --no-build-isolation

rm -rf .git

echo "GR00T N1.5 installed"
