#!/bin/bash
# Build the Isaac Lab Newton Docker image and push it to ECR.
#
# Usage:
#   ACCEPT_EULA=Y ./build_and_push_ecr.sh [AWS_REGION] [AWS_ACCOUNT_ID]
#
# Environment variables:
#   ACCEPT_EULA: Must be Y to acknowledge the NVIDIA Isaac Lab container EULA
#   PRIVACY_CONSENT: NVIDIA privacy consent flag passed during build (default: Y)
#   ECR_REPOSITORY: ECR repository name (default: isaac-lab-newton)
#   IMAGE_TAG: Docker image tag (default: 3.0.0-beta1)
#   BASE_IMAGE: Base image (default: nvcr.io/nvidia/isaac-lab:3.0.0-beta1)
#   NO_CACHE: Set to 1 to disable Docker build cache

set -euo pipefail

if [ "${ACCEPT_EULA:-}" != "Y" ]; then
    echo "ERROR: ACCEPT_EULA=Y is required to build the Isaac Lab container."
    echo "By building/running this image, you acknowledge the NVIDIA Isaac Lab container license terms."
    exit 1
fi

PRIVACY_CONSENT="${PRIVACY_CONSENT:-Y}"
ECR_REPOSITORY="${ECR_REPOSITORY:-isaac-lab-newton}"
IMAGE_TAG="${IMAGE_TAG:-3.0.0-beta1}"
BASE_IMAGE="${BASE_IMAGE:-nvcr.io/nvidia/isaac-lab:3.0.0-beta1}"
AWS_REGION="${1:-${AWS_REGION:-}}"
AWS_ACCOUNT_ID="${2:-${AWS_ACCOUNT_ID:-}}"

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
    echo "Set AWS_ACCOUNT_ID or pass it as the second argument."
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOCKERFILE_PATH="${SCRIPT_DIR}/Dockerfile"
ECR_URI="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPOSITORY}"
BUILD_FLAGS=()

if [ "${NO_CACHE:-0}" = "1" ]; then
    BUILD_FLAGS+=(--no-cache)
fi

echo "=================================================="
echo "Isaac Lab Newton Docker Build & ECR Push"
echo "=================================================="
echo "Base image: ${BASE_IMAGE}"
echo "ECR image: ${ECR_URI}:${IMAGE_TAG}"
echo "Dockerfile: ${DOCKERFILE_PATH}"
echo "Region: ${AWS_REGION}"
echo "Privacy consent: ${PRIVACY_CONSENT}"
echo "=================================================="

echo "[1/4] Checking ECR repository..."
if ! aws ecr describe-repositories --repository-names "${ECR_REPOSITORY}" --region "${AWS_REGION}" >/dev/null 2>&1; then
    aws ecr create-repository \
        --repository-name "${ECR_REPOSITORY}" \
        --region "${AWS_REGION}" \
        --image-scanning-configuration scanOnPush=true >/dev/null
    echo "  Created ${ECR_REPOSITORY}"
else
    echo "  Repository exists"
fi

echo "[2/4] Authenticating Docker to ECR..."
aws ecr get-login-password --region "${AWS_REGION}" | \
    docker login --username AWS --password-stdin "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

echo "[3/4] Building Docker image..."
docker build \
    --platform linux/amd64 \
    --build-arg BASE_IMAGE="${BASE_IMAGE}" \
    --build-arg PRIVACY_CONSENT="${PRIVACY_CONSENT}" \
    "${BUILD_FLAGS[@]}" \
    -t "${ECR_REPOSITORY}:${IMAGE_TAG}" \
    -t "${ECR_URI}:${IMAGE_TAG}" \
    -f "${DOCKERFILE_PATH}" \
    "${SCRIPT_DIR}"

echo "[4/4] Pushing Docker image..."
docker push "${ECR_URI}:${IMAGE_TAG}"

echo "=================================================="
echo "Docker image pushed successfully"
echo "Image URI: ${ECR_URI}:${IMAGE_TAG}"
echo "=================================================="
