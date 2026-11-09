#!/bin/bash
# Setup the LeRobot OpenPI (pi0 / pi0.5) environment on HyperPod
# This script will:
#   1. Create the required directories on FSx
#   2. Read the MLflow App ARN and the cluster name from the CloudFormation stack outputs
#   3. Add the LeRobot OpenPI configuration block to ~/.bashrc
#
# Usage: ./setup.sh [--hf-token "hf_xxxxx"] [--mlflow-tracking-uri <ARN>]
#                   [--stack-name <name>] [--region <region>] [--no-mlflow]
#
# Options:
#   --hf-token             Hugging Face token written to the block as HF_TOKEN
#   --mlflow-tracking-uri  MLflow App ARN. Overrides the MlflowAppArn stack output
#   --stack-name           CloudFormation stack name (default: PASK)
#   --region               Region of the stack (default: EC2 metadata via IMDSv2)
#   --no-mlflow            Do not write the MLflow settings to the block

set -e

USAGE='Usage: ./setup.sh [--hf-token "hf_xxxxx"] [--mlflow-tracking-uri <ARN>] [--stack-name <name>] [--region <region>] [--no-mlflow]'
BLOCK_START="# LeRobot OpenPI Configuration"
BLOCK_END="# End LeRobot OpenPI Configuration"
BASHRC="${HOME}/.bashrc"

HF_TOKEN_ARG=""
MLFLOW_TRACKING_URI_ARG=""
STACK_NAME="PASK"
REGION_ARG=""
ENABLE_MLFLOW=true

require_value() {
    if [[ -z "$2" ]]; then
        echo "ERROR: $1 requires a value"
        echo "${USAGE}"
        exit 1
    fi
}

while [[ $# -gt 0 ]]; do
    case $1 in
        --hf-token)
            require_value "$1" "$2"
            HF_TOKEN_ARG="$2"
            shift 2
            ;;
        --mlflow-tracking-uri)
            require_value "$1" "$2"
            MLFLOW_TRACKING_URI_ARG="$2"
            shift 2
            ;;
        --stack-name)
            require_value "$1" "$2"
            STACK_NAME="$2"
            shift 2
            ;;
        --region)
            require_value "$1" "$2"
            REGION_ARG="$2"
            shift 2
            ;;
        --no-mlflow)
            ENABLE_MLFLOW=false
            shift
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

echo "[1/3] Creating required directories..."

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

# Prints the value of the first stack output whose key starts with $1, or "None".
get_stack_output() {
    aws cloudformation describe-stacks \
        --stack-name "${STACK_NAME}" \
        --region "${STACK_REGION}" \
        --query "Stacks[0].Outputs[?starts_with(OutputKey, '$1')].OutputValue | [0]" \
        --output text
}

echo "[2/3] Resolving the MLflow settings..."

MLFLOW_TRACKING_URI_VALUE=""
MLFLOW_REGION=""
CLUSTER_NAME=""
if [ "${ENABLE_MLFLOW}" != true ]; then
    echo "  Skipped (--no-mlflow)"
else
    STACK_REGION="${REGION_ARG}"
    if [ -z "${STACK_REGION}" ]; then
        TOKEN=$(curl -X PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 21600" -s 2>/dev/null || true)
        STACK_REGION=$(curl -H "X-aws-ec2-metadata-token: ${TOKEN}" -s http://169.254.169.254/latest/meta-data/placement/region 2>/dev/null || true)
    fi

    if [ -z "${STACK_REGION}" ]; then
        echo "  WARNING: Could not determine the region from EC2 metadata. Pass --region to read the stack outputs"
    else
        echo "  Stack:  ${STACK_NAME} (${STACK_REGION})"
        CLUSTER_NAME=$(get_stack_output HyperPodClusterId) || CLUSTER_NAME=""
        if [ -z "${CLUSTER_NAME}" ] || [ "${CLUSTER_NAME}" = "None" ]; then
            echo "  WARNING: Could not read the HyperPodClusterId output of stack ${STACK_NAME}"
            CLUSTER_NAME=""
        fi
        if [ -z "${MLFLOW_TRACKING_URI_ARG}" ]; then
            MLFLOW_TRACKING_URI_VALUE=$(get_stack_output MlflowAppArn) || MLFLOW_TRACKING_URI_VALUE=""
            if [ -z "${MLFLOW_TRACKING_URI_VALUE}" ] || [ "${MLFLOW_TRACKING_URI_VALUE}" = "None" ]; then
                echo "  WARNING: Could not read the MlflowAppArn output of stack ${STACK_NAME}"
                MLFLOW_TRACKING_URI_VALUE=""
            fi
        fi
    fi

    if [ -n "${MLFLOW_TRACKING_URI_ARG}" ]; then
        MLFLOW_TRACKING_URI_VALUE="${MLFLOW_TRACKING_URI_ARG}"
    fi

    if [ -n "${MLFLOW_TRACKING_URI_VALUE}" ]; then
        # arn:aws:sagemaker:<region>:<account>:mlflow-app/<name>
        MLFLOW_REGION=$(echo "${MLFLOW_TRACKING_URI_VALUE}" | cut -d: -f4)
        if [ -z "${MLFLOW_REGION}" ]; then
            echo "  WARNING: Could not read the region from the MLflow App ARN: ${MLFLOW_TRACKING_URI_VALUE}"
            MLFLOW_TRACKING_URI_VALUE=""
        fi
    fi

    if [ -n "${MLFLOW_TRACKING_URI_VALUE}" ]; then
        echo "  MLflow App: ${MLFLOW_TRACKING_URI_VALUE}"
        echo "  Cluster:    ${CLUSTER_NAME:-(unknown)}"
    else
        echo "  WARNING: Continuing without MLflow. Training runs are not recorded"
    fi
fi
echo ""

echo "[3/3] Updating ${BASHRC}..."

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
    if [ -n "${MLFLOW_TRACKING_URI_VALUE}" ]; then
        printf 'export MLFLOW_TRACKING_URI=%q\n' "${MLFLOW_TRACKING_URI_VALUE}"
        printf 'export AWS_DEFAULT_REGION=%q\n' "${MLFLOW_REGION}"
        if [ -n "${CLUSTER_NAME}" ]; then
            printf 'export PASK_CLUSTER_NAME=%q\n' "${CLUSTER_NAME}"
        fi
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
if [ -n "${MLFLOW_TRACKING_URI_VALUE}" ]; then
    echo "  MLFLOW_TRACKING_URI:        ${MLFLOW_TRACKING_URI_VALUE}"
    echo "  AWS_DEFAULT_REGION:         ${MLFLOW_REGION}"
    echo "  PASK_CLUSTER_NAME:          ${CLUSTER_NAME:-(not set in this block)}"
else
    echo "  MLFLOW_TRACKING_URI:        (not set in this block; MLflow tracking is disabled)"
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
echo "      containing 'export HF_TOKEN=', 'ENROOT_CACHE_PATH' or 'ENROOT_DATA_PATH' from"
echo "      ~/.bashrc, including the ones in this block. Run this script again after that."
echo "      The MLFLOW_TRACKING_URI, AWS_DEFAULT_REGION and PASK_CLUSTER_NAME lines in this"
echo "      block are not removed by it."
