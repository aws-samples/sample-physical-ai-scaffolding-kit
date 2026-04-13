#!/bin/bash
#SBATCH --partition gpu
#SBATCH --gres=gpu:1
#SBATCH --job-name=build-leisaac
#SBATCH --output=/fsx/build/logs/build-leisaac-%j.out
#SBATCH --chdir=/tmp
set -euo pipefail

BUILD_DIR="${1:?Usage: sbatch build.sbatch <build-dir> [--clean]}"
PYXIS_NAME="pyxis_${CONTAINER_NAME}"

# Check for leftover container from a previous build
if srun enroot list | grep -q "^${PYXIS_NAME}$"; then
    if [ "${2:-}" = "--clean" ]; then
        echo "Removing existing container: $PYXIS_NAME"
        srun enroot remove -f "$PYXIS_NAME"
    else
        echo "ERROR: Container '$PYXIS_NAME' already exists."
        echo "To remove it:  srun --partition gpu enroot remove $PYXIS_NAME"
        echo "Or resubmit with --clean:  ./build.sh --clean"
        exit 1
    fi
fi

echo "Building on node: $(hostname), build dir: $BUILD_DIR"

# Step 1: Root setup (system packages, env vars)
srun --container-image=nvcr.io/nvidia/pytorch:25.04-py3 \
     --container-name="$CONTAINER_NAME" \
     --container-mounts=/fsx:/fsx \
     --container-remap-root \
     bash "$BUILD_DIR/setup-root.sh"

# Step 2: User setup (LeIsaac, GR00T, assets)
srun --container-name="$CONTAINER_NAME" \
     --container-mounts=/fsx:/fsx \
     bash "$BUILD_DIR/setup-user.sh"

# Step 3: Export to squashfs
if [ -f "$SQSH" ]; then
    echo "ERROR: $SQSH already exists. Remove it, then export manually:"
    echo "  srun --partition gpu --nodelist=$(hostname) enroot export -o $SQSH $PYXIS_NAME"
    exit 1
fi
srun enroot export -o "$SQSH" "$PYXIS_NAME"

echo "Build complete: $SQSH"
