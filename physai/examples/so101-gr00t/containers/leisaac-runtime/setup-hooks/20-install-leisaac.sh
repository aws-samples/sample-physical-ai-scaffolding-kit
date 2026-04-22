#!/bin/bash
# Install LeIsaac, IsaacSim (pip), and IsaacLab.
set -euo pipefail
export TERM=xterm

uv python install 3.11

git clone https://github.com/LightwheelAI/leisaac.git "$LEISAAC_DIR"
cd "$LEISAAC_DIR"
git checkout "$LEISAAC_REF"
git submodule update --init --recursive

uv venv --python 3.11 .venv
uv pip install pip

source .venv/bin/activate
pip install torch==2.7.0 torchvision==0.22.0 --index-url https://download.pytorch.org/whl/cu128
pip install 'isaacsim[all,extscache]==5.1.0' --extra-index-url https://pypi.nvidia.com
pip install -e source/leisaac
pip install --no-build-isolation flatdict==4.0.1
( cd dependencies/IsaacLab && ./isaaclab.sh --install none )
pip install msgpack msgpack-numpy pyzmq
deactivate

git submodule foreach --recursive 'rm -rf .git' 2>/dev/null || true
rm -rf .git

echo "LeIsaac installed"
