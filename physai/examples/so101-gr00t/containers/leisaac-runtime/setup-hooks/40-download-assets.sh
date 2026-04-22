#!/bin/bash
# Download robot USD and scene assets for LeIsaac.
set -euo pipefail

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

echo "Assets downloaded"
