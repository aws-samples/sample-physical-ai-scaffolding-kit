#!/bin/bash
# Step 2: User setup for leisaac-runtime container.
# Run with: srun --partition gpu --container-name=leisaac-runtime \
#                bash /fsx/build/leisaac-runtime/setup-user.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/pins.env"
export TERM=xterm

# ── LeIsaac + IsaacSim + IsaacLab ──

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

# Clean git data
git submodule foreach --recursive 'rm -rf .git' 2>/dev/null || true
rm -rf .git

# ── Isaac-GR00T (separate venv) ──

git clone https://github.com/NVIDIA/Isaac-GR00T.git "$GR00T_DIR"
cd "$GR00T_DIR"
git checkout "$GR00T_REF"

uv sync
uv pip install -e .
uv pip install flash-attn --no-build-isolation

rm -rf .git

# ── Assets ──

mkdir -p "$LEISAAC_DIR/assets/robots"
curl -L -o "$LEISAAC_DIR/assets/robots/so101_follower.usd" \
    https://github.com/LightwheelAI/leisaac/releases/download/v0.1.0/so101_follower.usd

cd "$LEISAAC_DIR/assets"
curl -L -o table_with_cube.zip \
    https://github.com/LightwheelAI/leisaac/releases/download/v0.1.2/table_with_cube.zip
unzip -q table_with_cube.zip -d scenes && rm table_with_cube.zip

curl -L -o kitchen_with_orange.zip \
    https://github.com/LightwheelAI/leisaac/releases/download/v0.1.0/kitchen_with_orange.zip
unzip -q kitchen_with_orange.zip -d scenes && rm kitchen_with_orange.zip

# ── Copy eval.sh entrypoint ──

cp "$SCRIPT_DIR/eval.sh" /app/eval.sh
chmod +x /app/eval.sh

# ── Warm up shader caches ──

"$SCRIPT_DIR/warmup.sh"

echo "User setup complete"
