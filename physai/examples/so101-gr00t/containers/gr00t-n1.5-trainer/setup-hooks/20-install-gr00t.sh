#!/bin/bash
# Install Isaac-GR00T N1.5 for fine-tuning.
set -euo pipefail

git clone https://github.com/NVIDIA/Isaac-GR00T.git "$GR00T_DIR"
cd "$GR00T_DIR"
git checkout "$GR00T_N15_REF"

uv venv --python 3.11
uv pip install -e ".[base]"
uv pip install flash-attn --no-build-isolation
# hf_xet (Rust-based HF download backend, auto-pulled by huggingface_hub
# >=0.32) deadlocks on long-running model downloads — sockets end up in
# CLOSE-WAIT while worker threads sleep on futexes indefinitely. Uninstall
# it so hf_hub falls back to its urllib3 HTTP path.
uv pip uninstall hf_xet

rm -rf .git

echo "GR00T N1.5 installed"
