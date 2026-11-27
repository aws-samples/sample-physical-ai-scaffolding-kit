#!/bin/bash
# Setup OpenPI environment on HyperPod
# This script will:
#   1. Clone OpenPI repository if needed and pin it to OPENPI_COMMIT
#   2. Apply the MLflow tracking patch to OpenPI
#   3. Create required directories
#   4. Read MLflow / S3 link settings from the CloudFormation stack outputs
#   5. Prepare environment for Slurm jobs
#
# Usage: ./setup.sh [--hf-token "hf_xxxxx"] [--mlflow-tracking-uri <ARN>]
#                   [--stack-name <name>] [--region <region>] [--no-mlflow]
#
# Options:
#   --hf-token             Hugging Face token written to ~/.bashrc
#   --mlflow-tracking-uri  MLflow App ARN. Overrides the MlflowAppArn stack output
#   --stack-name           CloudFormation stack name (default: PASK)
#   --region               AWS region of the stack (default: EC2 metadata (IMDSv2))
#   --no-mlflow            Do not write MLflow settings to ~/.bashrc

set -e

USAGE='Usage: ./setup.sh [--hf-token "hf_xxxxx"] [--mlflow-tracking-uri <ARN>] [--stack-name <name>] [--region <region>] [--no-mlflow]'

OPENPI_COMMIT=215abfb217dbac7d5f1273282331b9b1866c0479
MLFLOW_MARKER="# OpenPI MLflow"

# Parse command line arguments
HF_TOKEN=""
MLFLOW_TRACKING_URI_OVERRIDE=""
STACK_NAME="PASK"
REGION=""
ENABLE_MLFLOW=true
while [[ $# -gt 0 ]]; do
    case $1 in
        --hf-token)
            HF_TOKEN="$2"
            shift 2
            ;;
        --mlflow-tracking-uri)
            MLFLOW_TRACKING_URI_OVERRIDE="$2"
            shift 2
            ;;
        --stack-name)
            STACK_NAME="$2"
            shift 2
            ;;
        --region)
            REGION="$2"
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
echo "OpenPI Environment Setup"
echo "=================================================="
echo ""

# Get the directory where this script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Calculate paths relative to script location
# Script is in: /fsx/ubuntu/openpi-sample/lora_training/
# Base dir is:  /fsx/ubuntu/openpi-sample/
BASE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
OPENPI_ROOT="${BASE_DIR}/openpi"

echo "Script location: ${SCRIPT_DIR}"
echo "Base directory: ${BASE_DIR}"
echo ""

# Clone OpenPI repository if it doesn't exist
if [ ! -d "${OPENPI_ROOT}" ]; then
    echo "📥 Cloning OpenPI repository..."
    cd "${BASE_DIR}"

    if [ -n "${HF_TOKEN}" ]; then
        echo "  Using provided HF_TOKEN for authentication"
        export HF_TOKEN="${HF_TOKEN}"
    fi

    git clone https://github.com/Physical-Intelligence/openpi.git

    if [ $? -eq 0 ]; then
        echo "  ✓ Successfully cloned OpenPI repository"
    else
        echo "  ❌ ERROR: Failed to clone OpenPI repository"
        exit 1
    fi

    git -C "${OPENPI_ROOT}" checkout --quiet "${OPENPI_COMMIT}"
    echo "  ✓ Checked out OpenPI commit ${OPENPI_COMMIT}"
    echo ""
else
    echo "✓ Found existing OpenPI repository at: ${OPENPI_ROOT}"
    CURRENT_COMMIT=$(git -C "${OPENPI_ROOT}" rev-parse HEAD)
    if [ "${CURRENT_COMMIT}" != "${OPENPI_COMMIT}" ]; then
        echo "  ⚠️  WARNING: OpenPI HEAD is ${CURRENT_COMMIT}, expected ${OPENPI_COMMIT}"
        echo "     The MLflow tracking patch is written for ${OPENPI_COMMIT}"
    fi
    echo ""
fi

# Apply the MLflow tracking patch
PATCH_FILE="${SCRIPT_DIR}/patches/mlflow_tracking.patch"
echo "Applying MLflow tracking patch..."
if git -C "${OPENPI_ROOT}" apply --reverse --check "${PATCH_FILE}" 2>/dev/null; then
    echo "  ✓ Patch already applied"
elif git -C "${OPENPI_ROOT}" apply --check "${PATCH_FILE}" 2>/dev/null; then
    git -C "${OPENPI_ROOT}" apply "${PATCH_FILE}"
    echo "  ✓ Patch applied"
else
    echo "  ❌ ERROR: Failed to apply ${PATCH_FILE}"
    echo "  The patch requires OpenPI commit ${OPENPI_COMMIT} without local changes."
    echo "  Move the existing clone aside and run this script again to clone a pinned copy:"
    echo "    mv ${OPENPI_ROOT} ${OPENPI_ROOT}.old"
    echo "    ./setup.sh [options]"
    exit 1
fi
echo ""

# Create required directories
echo "Creating required directories..."

DIRS=(
    "${BASE_DIR}/logs"
    "${BASE_DIR}/.cache"
    "${OPENPI_ROOT}/assets/physical-intelligence/libero"
)

for dir in "${DIRS[@]}"; do
    if [ ! -d "$dir" ]; then
        mkdir -p "$dir"
        echo "  ✓ Created: $dir"
    else
        echo "  ✓ Exists: $dir"
    fi
done

echo ""

# Read MLflow / S3 link settings from the CloudFormation stack outputs.
# Values that cannot be read are left empty and their ~/.bashrc lines are skipped.
MLFLOW_TRACKING_URI_VALUE=""
MLFLOW_REGION=""
PASK_CLUSTER_NAME_VALUE=""
PASK_S3LINK_DIR_VALUE=""
PASK_S3LINK_URI_VALUE=""

# Prints the value of a stack output selected by a JMESPath filter, or nothing
# if the output does not exist.
stack_output() {
    local filter="$1"
    local value
    value=$(aws cloudformation describe-stacks \
        --stack-name "${STACK_NAME}" \
        --region "${REGION}" \
        --query "Stacks[0].Outputs[?${filter}].OutputValue | [0]" \
        --output text)
    if [ "${value}" != "None" ]; then
        echo "${value}"
    fi
}

if [ "${ENABLE_MLFLOW}" = true ]; then
    echo "Reading MLflow settings from stack ${STACK_NAME}..."

    if [ -z "${REGION}" ]; then
        TOKEN=$(curl -X PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 21600" -s 2>/dev/null)
        REGION=$(curl -H "X-aws-ec2-metadata-token: $TOKEN" -s http://169.254.169.254/latest/meta-data/placement/region 2>/dev/null)
    fi

    if [ -z "${REGION}" ]; then
        echo "  ⚠️  WARNING: Could not determine the AWS region. Pass --region to read the stack outputs"
    elif aws cloudformation describe-stacks --stack-name "${STACK_NAME}" --region "${REGION}" > /dev/null; then
        MLFLOW_TRACKING_URI_VALUE=$(stack_output "starts_with(OutputKey, 'MlflowAppArn')")
        PASK_S3LINK_URI_VALUE=$(stack_output "starts_with(OutputKey, 'S3LinkUri')")
        PASK_S3LINK_DIR_VALUE=$(stack_output "starts_with(OutputKey, 'S3LinkMountPath')")
        PASK_CLUSTER_NAME_VALUE=$(stack_output "starts_with(OutputKey, 'HyperPodClusterId')")
    else
        echo "  ⚠️  WARNING: Could not read stack ${STACK_NAME} in ${REGION}"
    fi

    if [ -n "${MLFLOW_TRACKING_URI_OVERRIDE}" ]; then
        MLFLOW_TRACKING_URI_VALUE="${MLFLOW_TRACKING_URI_OVERRIDE}"
    fi

    if [ -n "${MLFLOW_TRACKING_URI_VALUE}" ]; then
        MLFLOW_REGION=$(echo "${MLFLOW_TRACKING_URI_VALUE}" | cut -d: -f4)
    fi

    if [ -z "${MLFLOW_TRACKING_URI_VALUE}" ] || [ -z "${MLFLOW_REGION}" ]; then
        echo "  ⚠️  WARNING: MLflow App ARN not found. MLflow tracking will be disabled"
        echo "     Pass --mlflow-tracking-uri <ARN> or check the MlflowAppArn output of stack ${STACK_NAME}"
        MLFLOW_TRACKING_URI_VALUE=""
        MLFLOW_REGION=""
    else
        echo "  ✓ MLflow App: ${MLFLOW_TRACKING_URI_VALUE}"
    fi
    if [ -z "${PASK_S3LINK_DIR_VALUE}" ] || [ -z "${PASK_S3LINK_URI_VALUE}" ]; then
        echo "  ⚠️  WARNING: S3 link outputs not found. Final checkpoints will not be exported to S3"
        PASK_S3LINK_DIR_VALUE=""
        PASK_S3LINK_URI_VALUE=""
    fi
    echo ""
fi

# Setup environment variables in ~/.bashrc
echo "Setting up environment variables..."
echo ""

# Remove existing OpenPI and Enroot Configuration if it exists
if grep -q "# OpenPI Configuration" ~/.bashrc; then
    echo "  ⚠️  Removing existing OpenPI/Enroot Configuration from ~/.bashrc"
    # Create backup
    cp ~/.bashrc ~/.bashrc.backup
    # Remove entire OpenPI and Enroot configuration block
    grep -v "# OpenPI Configuration" ~/.bashrc | \
    grep -v "${MLFLOW_MARKER}" | \
    grep -v "# Enroot Configuration" | \
    grep -v "OPENPI_BASE_DIR" | \
    grep -v "OPENPI_PROJECT_ROOT" | \
    grep -v "OPENPI_DATA_HOME" | \
    grep -v "OPENPI_LOG_DIR" | \
    grep -v "export HF_TOKEN=" | \
    grep -v "ENROOT_CACHE_PATH" | \
    grep -v "ENROOT_DATA_PATH" > ~/.bashrc.tmp
    mv ~/.bashrc.tmp ~/.bashrc
fi

# Append new configuration
cat >> ~/.bashrc << EOF
# OpenPI Configuration
export OPENPI_BASE_DIR=${BASE_DIR}
export OPENPI_PROJECT_ROOT=\${OPENPI_BASE_DIR}/openpi
export OPENPI_DATA_HOME=\${OPENPI_BASE_DIR}/.cache
export OPENPI_LOG_DIR=\${OPENPI_BASE_DIR}/logs
EOF

# Add HF_TOKEN if provided
if [ -n "${HF_TOKEN}" ]; then
    echo "export HF_TOKEN=${HF_TOKEN}" >> ~/.bashrc
else
    echo "export HF_TOKEN=" >> ~/.bashrc
fi

# Add MLflow and S3 link settings. Each line carries MLFLOW_MARKER so that the
# next run removes only these lines and leaves other samples' blocks intact.
if [ -n "${MLFLOW_TRACKING_URI_VALUE}" ]; then
    echo "export MLFLOW_TRACKING_URI=${MLFLOW_TRACKING_URI_VALUE} ${MLFLOW_MARKER}" >> ~/.bashrc
    echo "export AWS_DEFAULT_REGION=${MLFLOW_REGION} ${MLFLOW_MARKER}" >> ~/.bashrc
fi
if [ -n "${PASK_CLUSTER_NAME_VALUE}" ]; then
    echo "export PASK_CLUSTER_NAME=${PASK_CLUSTER_NAME_VALUE} ${MLFLOW_MARKER}" >> ~/.bashrc
fi
if [ -n "${PASK_S3LINK_DIR_VALUE}" ]; then
    echo "export PASK_S3LINK_DIR=${PASK_S3LINK_DIR_VALUE} ${MLFLOW_MARKER}" >> ~/.bashrc
    echo "export PASK_S3LINK_URI=${PASK_S3LINK_URI_VALUE} ${MLFLOW_MARKER}" >> ~/.bashrc
fi

# Add Enroot Configuration
cat >> ~/.bashrc << 'EOF'

# Enroot Configuration
export ENROOT_CACHE_PATH=/fsx/enroot
export ENROOT_DATA_PATH=/fsx/enroot/data
EOF

echo "  ✓ Environment variables added to ~/.bashrc"
echo ""

# Display configuration
echo "=================================================="
echo "✅ Setup complete"
echo "=================================================="
echo ""
echo "Environment variables added to ~/.bashrc:"
echo "  OPENPI_BASE_DIR: ${BASE_DIR}"
echo "  OPENPI_PROJECT_ROOT: ${BASE_DIR}/openpi"
echo "  OPENPI_DATA_HOME: ${BASE_DIR}/.cache"
echo "  OPENPI_LOG_DIR: ${BASE_DIR}/logs"
if [ -n "${HF_TOKEN}" ]; then
    echo "  HF_TOKEN: ${HF_TOKEN:0:10}... (set)"
else
    echo "  HF_TOKEN: (not set)"
fi
echo "  MLFLOW_TRACKING_URI: ${MLFLOW_TRACKING_URI_VALUE:-(not set)}"
echo "  AWS_DEFAULT_REGION: ${MLFLOW_REGION:-(not set)}"
echo "  PASK_CLUSTER_NAME: ${PASK_CLUSTER_NAME_VALUE:-(not set)}"
echo "  PASK_S3LINK_DIR: ${PASK_S3LINK_DIR_VALUE:-(not set)}"
echo "  PASK_S3LINK_URI: ${PASK_S3LINK_URI_VALUE:-(not set)}"
echo "  ENROOT_CACHE_PATH: /fsx/enroot"
echo "  ENROOT_DATA_PATH: /fsx/enroot/data"
