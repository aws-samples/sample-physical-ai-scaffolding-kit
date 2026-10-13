#!/bin/bash
# Remove build caches before squashfs export.
set -euo pipefail

rm -rf ~/.cache/uv ~/.cache/pip ~/.cache/huggingface/hub

echo "Cleanup complete"
