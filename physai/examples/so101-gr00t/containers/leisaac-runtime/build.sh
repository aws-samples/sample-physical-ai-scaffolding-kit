#!/bin/sh
# Deploy build files to /fsx and submit the build job.
# Usage: ./build.sh [--clean]
set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CONTAINER_NAME=leisaac-runtime
BUILD_DIR="/fsx/build/${CONTAINER_NAME}-$(date +%Y%m%d-%H%M%S)"
SQSH="/fsx/enroot/${CONTAINER_NAME}.sqsh"

if [ -f "$SQSH" ]; then
    echo "ERROR: $SQSH already exists. Remove it before building:"
    echo "  rm $SQSH"
    exit 1
fi

mkdir -p "$BUILD_DIR" /fsx/build/logs
cp "$SCRIPT_DIR"/setup-root.sh "$SCRIPT_DIR"/setup-user.sh "$SCRIPT_DIR"/build.sbatch "$SCRIPT_DIR"/eval.sh "$SCRIPT_DIR"/warmup.sh "$BUILD_DIR/"
cp "$SCRIPT_DIR/../pins.env" "$BUILD_DIR/"

echo "Build dir: $BUILD_DIR"
JOB_ID=$(sbatch --parsable --export=ALL,CONTAINER_NAME="$CONTAINER_NAME",SQSH="$SQSH" "$BUILD_DIR/build.sbatch" "$BUILD_DIR" "$@")
echo "Submitted job $JOB_ID"
echo "Tail log: tail -f /fsx/build/logs/build-leisaac-${JOB_ID}.out"
