#!/bin/bash
# Import the Isaac Lab Newton Docker image from ECR to Enroot on HyperPod.
#
# Usage:
#   ./hyperpod_import_container.sh [IMAGE_TAG] [AWS_REGION] [AWS_ACCOUNT_ID]
#
# Environment variables:
#   ECR_REPOSITORY: ECR repository name (default: isaac-lab-newton)
#   IMAGE_TAG: Docker image tag to import (default: 3.0.0-beta1)
#   AWS_REGION: AWS region (default: auto-detect)
#   AWS_ACCOUNT_ID: AWS account ID (default: auto-detect)
#   ENROOT_CACHE_PATH: Enroot cache directory (default: /fsx/enroot)
#   ENROOT_DATA_PATH: Enroot data directory (default: /fsx/enroot/data)

set -euo pipefail

ECR_REPOSITORY="${ECR_REPOSITORY:-isaac-lab-newton}"
IMAGE_TAG="${1:-${IMAGE_TAG:-3.0.0-beta1}}"
AWS_REGION="${2:-${AWS_REGION:-}}"
AWS_ACCOUNT_ID="${3:-${AWS_ACCOUNT_ID:-}}"
ENROOT_CACHE_PATH="${ENROOT_CACHE_PATH:-/fsx/enroot}"
ENROOT_DATA_PATH="${ENROOT_DATA_PATH:-/fsx/enroot/data}"

if [ -z "${AWS_REGION}" ]; then
    TOKEN="$(curl -X PUT "http://169.254.169.254/latest/api/token" \
        -H "X-aws-ec2-metadata-token-ttl-seconds: 21600" -s 2>/dev/null || true)"
    AWS_REGION="$(curl -H "X-aws-ec2-metadata-token: ${TOKEN}" -s \
        http://169.254.169.254/latest/meta-data/placement/region 2>/dev/null || true)"
    AWS_REGION="${AWS_REGION:-$(aws configure get region 2>/dev/null || true)}"
    AWS_REGION="${AWS_REGION:-us-east-1}"
fi

if [ -z "${AWS_ACCOUNT_ID}" ]; then
    AWS_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text 2>/dev/null || true)"
fi

if [ -z "${AWS_ACCOUNT_ID}" ]; then
    echo "ERROR: Could not determine AWS account ID."
    echo "Set AWS_ACCOUNT_ID or pass it as the third argument."
    exit 1
fi

ECR_URI="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPOSITORY}:${IMAGE_TAG}"
CONTAINER_FILENAME="${ECR_REPOSITORY}+${IMAGE_TAG}.sqsh"
CONTAINER_PATH="${ENROOT_DATA_PATH}/${CONTAINER_FILENAME}"

echo "=================================================="
echo "Isaac Lab Newton - Enroot Import"
echo "=================================================="
echo "ECR image: ${ECR_URI}"
echo "Enroot cache: ${ENROOT_CACHE_PATH}"
echo "Enroot data: ${ENROOT_DATA_PATH}"
echo "Container path: ${CONTAINER_PATH}"
echo "=================================================="

mkdir -p "${ENROOT_CACHE_PATH}" "${ENROOT_DATA_PATH}"
export ENROOT_CACHE_PATH
export ENROOT_DATA_PATH

echo "[1/4] Checking local Docker cache..."
if docker image inspect "${ECR_URI}" >/dev/null 2>&1; then
    echo "  Image found locally"
else
    echo "  Pulling image from ECR"
    aws ecr get-login-password --region "${AWS_REGION}" | \
        docker login --username AWS --password-stdin "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
    docker pull "${ECR_URI}"
fi

echo "[2/4] Removing existing Enroot image if present..."
rm -f "${CONTAINER_PATH}"

echo "[3/4] Importing Docker image to Enroot..."
enroot import -o "${CONTAINER_PATH}" "dockerd://${ECR_URI}"

echo "[4/4] Verifying Enroot image..."
if [ ! -f "${CONTAINER_PATH}" ]; then
    echo "ERROR: Container file was not created: ${CONTAINER_PATH}"
    exit 1
fi

echo "=================================================="
echo "Container imported successfully"
echo "Container: ${CONTAINER_FILENAME}"
echo "Size: $(du -h "${CONTAINER_PATH}" | cut -f1)"
echo "=================================================="
