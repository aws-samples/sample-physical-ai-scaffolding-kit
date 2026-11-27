#!/bin/bash
# Setup the LeRobot OpenPI (pi0 / pi0.5) environment on HyperPod
# This script will:
#   1. Create the required directories on FSx
#   2. Add the LeRobot OpenPI configuration block to ~/.bashrc
#
# Usage: ./setup.sh [--hf-token "hf_xxxxx"]

set -e

USAGE='Usage: ./setup.sh [--hf-token "hf_xxxxx"]'
BLOCK_START="# LeRobot OpenPI Configuration"
BLOCK_END="# End LeRobot OpenPI Configuration"
BASHRC="${HOME}/.bashrc"

HF_TOKEN_ARG=""
while [[ $# -gt 0 ]]; do
    case $1 in
        --hf-token)
            if [[ -z "$2" ]]; then
                echo "ERROR: --hf-token requires a value"
                echo "${USAGE}"
                exit 1
            fi
            HF_TOKEN_ARG="$2"
            shift 2
            ;;
        *)
            echo "Unknown option: $1"
            echo "${USAGE}"
            exit 1
            ;;
    esac
done

echo "=================================================="
echo "LeRobot OpenPI Environment Setup"
echo "=================================================="
echo ""

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Script is in samples/lerobot-openpi/training/, base dir is samples/lerobot-openpi/
BASE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

echo "Script location: ${SCRIPT_DIR}"
echo "Base directory:  ${BASE_DIR}"
echo ""

echo "[1/2] Creating required directories..."

DIRS=(
    "${BASE_DIR}/logs"
    "${BASE_DIR}/outputs"
    "${BASE_DIR}/.cache/huggingface"
    "${BASE_DIR}/.cache/huggingface/lerobot"
    "${BASE_DIR}/.cache/triton"
    "${BASE_DIR}/.cache/torchinductor"
    "${BASE_DIR}/.cache/home"
    "/fsx/enroot/data"
    "/fsx/enroot/tmp"
)

for dir in "${DIRS[@]}"; do
    if [ ! -d "${dir}" ]; then
        mkdir -p "${dir}"
        echo "  Created: ${dir}"
    else
        echo "  Exists:  ${dir}"
    fi
done
echo ""

echo "[2/2] Updating ${BASHRC}..."

touch "${BASHRC}"

if grep -qx "${BLOCK_START}" "${BASHRC}"; then
    echo "  Removing the existing LeRobot OpenPI block (backup: ${BASHRC}.backup)"
    cp "${BASHRC}" "${BASHRC}.backup"
    sed -i "/^${BLOCK_START}\$/,/^${BLOCK_END}\$/d" "${BASHRC}"
fi

WRITE_ENROOT_DATA=false
if ! grep -q "export ENROOT_DATA_PATH=" "${BASHRC}"; then
    WRITE_ENROOT_DATA=true
fi

WRITE_ENROOT_TEMP=false
if ! grep -q "export ENROOT_TEMP_PATH=" "${BASHRC}"; then
    WRITE_ENROOT_TEMP=true
fi

{
    echo "${BLOCK_START}"
    echo "export LEROBOT_PI_BASE_DIR=${BASE_DIR}"
    cat << 'EOF'
export LEROBOT_PI_LOG_DIR=${LEROBOT_PI_BASE_DIR}/logs
export LEROBOT_PI_OUTPUT_DIR=${LEROBOT_PI_BASE_DIR}/outputs
export LEROBOT_PI_CACHE_DIR=${LEROBOT_PI_BASE_DIR}/.cache
export LEROBOT_PI_HF_HOME=${LEROBOT_PI_CACHE_DIR}/huggingface
export LEROBOT_PI_HF_LEROBOT_HOME=${LEROBOT_PI_HF_HOME}/lerobot
EOF
    if [ -n "${HF_TOKEN_ARG}" ]; then
        printf 'export HF_TOKEN=%q\n' "${HF_TOKEN_ARG}"
    fi
    if [ "${WRITE_ENROOT_DATA}" = true ]; then
        echo "export ENROOT_CACHE_PATH=/fsx/enroot"
        echo "export ENROOT_DATA_PATH=/fsx/enroot/data"
    fi
    if [ "${WRITE_ENROOT_TEMP}" = true ]; then
        echo "export ENROOT_TEMP_PATH=/fsx/enroot/tmp"
    fi
    echo "${BLOCK_END}"
} >> "${BASHRC}"

echo "  Added the LeRobot OpenPI block to ${BASHRC}"
echo ""

echo "=================================================="
echo "Setup complete"
echo "=================================================="
echo ""
echo "Environment variables in the LeRobot OpenPI block:"
echo "  LEROBOT_PI_BASE_DIR:        ${BASE_DIR}"
echo "  LEROBOT_PI_LOG_DIR:         ${BASE_DIR}/logs"
echo "  LEROBOT_PI_OUTPUT_DIR:      ${BASE_DIR}/outputs"
echo "  LEROBOT_PI_CACHE_DIR:       ${BASE_DIR}/.cache"
echo "  LEROBOT_PI_HF_HOME:         ${BASE_DIR}/.cache/huggingface"
echo "  LEROBOT_PI_HF_LEROBOT_HOME: ${BASE_DIR}/.cache/huggingface/lerobot"
if [ -n "${HF_TOKEN_ARG}" ]; then
    echo "  HF_TOKEN:                   ${HF_TOKEN_ARG:0:10}... (set)"
else
    echo "  HF_TOKEN:                   (not set in this block)"
fi
if [ "${WRITE_ENROOT_DATA}" = true ]; then
    echo "  ENROOT_CACHE_PATH:          /fsx/enroot"
    echo "  ENROOT_DATA_PATH:           /fsx/enroot/data"
else
    echo "  ENROOT_CACHE_PATH / ENROOT_DATA_PATH: already defined outside this block"
fi
if [ "${WRITE_ENROOT_TEMP}" = true ]; then
    echo "  ENROOT_TEMP_PATH:           /fsx/enroot/tmp"
else
    echo "  ENROOT_TEMP_PATH:           already defined outside this block"
fi
echo ""
echo "Next steps:"
echo "  1. source ~/.bashrc"
echo "  2. sbatch slurm_build_docker.sh"
echo "  3. ./hyperpod_import_container.sh"
echo ""
echo "Note: running samples/openpi-sample/lora_training/setup.sh later removes every line"
echo "      containing 'export HF_TOKEN=' or 'ENROOT_' from ~/.bashrc, including the ones"
echo "      in this block. Run this script again after that."
