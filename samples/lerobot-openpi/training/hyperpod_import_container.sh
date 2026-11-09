#!/bin/bash
# Import the LeRobot OpenPI (pi0.5) training image from ECR to Enroot on HyperPod
# Run this script on the HyperPod login node
#
# Prerequisites:
#   - AWS CLI configured with ECR access
#   - Docker and Enroot installed on the node
#   - FSx for Lustre mounted at /fsx
#
# Usage:
#   ./hyperpod_import_container.sh [IMAGE_TAG] [AWS_REGION] [AWS_ACCOUNT_ID]
#
#   Examples:
#     ./hyperpod_import_container.sh                                # Use EC2 metadata
#     ./hyperpod_import_container.sh latest                          # Specify image tag only
#     ./hyperpod_import_container.sh latest us-west-2                # Specify tag and region
#     ./hyperpod_import_container.sh latest us-west-2 123456789012   # Specify all
#     FORCE=true ./hyperpod_import_container.sh                      # Overwrite an existing .sqsh
#
# Environment Variables:
#   IMAGE_TAG (optional): Docker image tag to import (default: latest)
#   AWS_REGION (optional): AWS region (default: auto-detect from EC2 metadata)
#   AWS_ACCOUNT_ID (optional): AWS account ID (default: auto-detect from STS)
#   ECR_REPOSITORY (optional): ECR repository name (default: lerobot-pi-train)
#   ENROOT_CACHE_PATH (optional): Enroot cache directory (default: /fsx/enroot)
#   ENROOT_DATA_PATH (optional): Enroot data directory (default: /fsx/enroot/data)
#   ENROOT_TEMP_PATH (optional): Enroot temporary directory (default: /fsx/enroot/tmp)
#   FORCE (optional): Set to "true" to delete an existing .sqsh before importing (default: unset)

set -e

# Priority: 1. Command line args  2. Environment variables  3. Auto-detect  4. Fallback
IMAGE_TAG="${1:-${IMAGE_TAG:-latest}}"

# Priority: 1. Argument $2  2. Environment variable  3. EC2 metadata (IMDSv2)  4. Fallback
if [ -n "$2" ]; then
    AWS_REGION="$2"
elif [ -z "${AWS_REGION}" ]; then
    TOKEN=$(curl -X PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 21600" -s 2>/dev/null)
    AWS_REGION=$(curl -H "X-aws-ec2-metadata-token: $TOKEN" -s http://169.254.169.254/latest/meta-data/placement/region 2>/dev/null)
    AWS_REGION="${AWS_REGION:-us-east-1}"
fi

# Priority: 1. Argument $3  2. Environment variable  3. AWS STS  4. Error
if [ -n "$3" ]; then
    AWS_ACCOUNT_ID="$3"
elif [ -z "${AWS_ACCOUNT_ID}" ]; then
    AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text 2>/dev/null)
fi

if [ -z "${AWS_ACCOUNT_ID}" ]; then
    echo "ERROR: Could not determine AWS Account ID"
    echo "Please specify as argument: ./hyperpod_import_container.sh [TAG] [REGION] [ACCOUNT_ID]"
    echo "Or set AWS_ACCOUNT_ID environment variable or configure AWS CLI"
    exit 1
fi

ECR_REPOSITORY="${ECR_REPOSITORY:-lerobot-pi-train}"
ECR_URI="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPOSITORY}:${IMAGE_TAG}"

ENROOT_CACHE_PATH="${ENROOT_CACHE_PATH:-/fsx/enroot}"
ENROOT_DATA_PATH="${ENROOT_DATA_PATH:-/fsx/enroot/data}"
# The login node root volume is small, so keep Enroot temporary files on FSx
ENROOT_TEMP_PATH="${ENROOT_TEMP_PATH:-/fsx/enroot/tmp}"

CONTAINER_FILENAME="${ECR_REPOSITORY}+${IMAGE_TAG}.sqsh"
CONTAINER_PATH="${ENROOT_DATA_PATH}/${CONTAINER_FILENAME}"

echo "=================================================="
echo "LeRobot OpenPI (pi0.5) - Enroot Import Script"
echo "=================================================="
echo "ECR Image: ${ECR_URI}"
echo "Enroot Cache: ${ENROOT_CACHE_PATH}"
echo "Enroot Data: ${ENROOT_DATA_PATH}"
echo "Enroot Temp: ${ENROOT_TEMP_PATH}"
echo "Output: ${CONTAINER_PATH}"
echo ""

# enroot import fails when the output file already exists, so check before pulling
if [ -e "${CONTAINER_PATH}" ]; then
    if [ "${FORCE}" = "true" ]; then
        echo "FORCE=true: Deleting existing container file: ${CONTAINER_PATH}"
        rm -f "${CONTAINER_PATH}"
        echo ""
    else
        echo "ERROR: Container file already exists: ${CONTAINER_PATH}"
        echo "enroot import cannot overwrite an existing file."
        echo "Re-run with FORCE=true to delete it and import again:"
        echo "  FORCE=true ./hyperpod_import_container.sh ${IMAGE_TAG} ${AWS_REGION} ${AWS_ACCOUNT_ID}"
        exit 1
    fi
fi

echo "[1/5] Setting up Enroot directories..."
mkdir -p "${ENROOT_CACHE_PATH}"
mkdir -p "${ENROOT_DATA_PATH}"
mkdir -p "${ENROOT_TEMP_PATH}"
export ENROOT_CACHE_PATH
export ENROOT_DATA_PATH
export ENROOT_TEMP_PATH
echo "  Directories created"

echo "[2/5] Authenticating to ECR..."
aws ecr get-login-password --region "${AWS_REGION}" | \
    docker login --username AWS --password-stdin "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
echo "  Authentication successful"

echo "[3/5] Pulling Docker image from ECR..."
docker pull "${ECR_URI}"
echo "  Image pulled successfully"

echo "[4/5] Importing container to Enroot from local Docker..."
# dockerd:// imports from the local Docker daemon, which avoids ECR authentication in enroot.
# Docker stores the image under the full ECR URI, so the same URI is used here.
enroot import -o "${CONTAINER_PATH}" "dockerd://${ECR_URI}"
echo "  Import successful"

echo "[5/5] Verifying imported container..."
if [ ! -f "${CONTAINER_PATH}" ]; then
    echo "  ERROR: Container file not found at: ${CONTAINER_PATH}"
    echo ""
    echo "Files in ${ENROOT_DATA_PATH}:"
    ls -lh "${ENROOT_DATA_PATH}/" 2>/dev/null || echo "  Directory is empty or does not exist"
    exit 1
fi
CONTAINER_SIZE=$(du -h "${CONTAINER_PATH}" | cut -f1)
echo "  Container imported: ${CONTAINER_FILENAME}"
echo "  Container size: ${CONTAINER_SIZE}"

echo ""
echo "=================================================="
echo "Container ready for Slurm execution"
echo "=================================================="
echo "Container Name: ${CONTAINER_FILENAME}"
echo "Container Path: ${CONTAINER_PATH}"
echo ""
