#!/bin/bash
# Build the LeRobot pi0 / pi0.5 training Docker image and push it to ECR
#
# This script can run both on a local machine and on a HyperPod worker node.
#
# Usage:
#   Local:
#     ./build_and_push_ecr.sh [AWS_REGION] [AWS_ACCOUNT_ID]
#
#     Examples:
#       ./build_and_push_ecr.sh                           # Auto-detect region and account
#       ./build_and_push_ecr.sh us-west-2                 # Specify region only
#       ./build_and_push_ecr.sh us-west-2 123456789012    # Specify both
#
#   HyperPod (build on a worker node via Slurm, run from the training/ directory):
#     sbatch slurm_build_docker.sh
#
# Environment Variables:
#   AWS_REGION (optional): AWS region
#     (resolution order: $1, environment variable, aws configure, EC2 metadata (IMDSv2), us-east-1)
#   AWS_ACCOUNT_ID (optional): AWS account ID
#     (resolution order: $2, environment variable, aws sts get-caller-identity)
#   ECR_REPOSITORY (optional): ECR repository name (default: lerobot-pi-train)
#   IMAGE_TAG (optional): Docker image tag (default: latest)
#   LEROBOT_VERSION (optional): lerobot version installed from PyPI (default: 0.6.1)
#   DOCKERFILE_PATH (optional): Path to Dockerfile (default: Dockerfile in this script's directory)

set -e

IMDS_ENDPOINT="http://169.254.169.254/latest"
IMDS_TIMEOUT_SECONDS=2

# Print the region of the current EC2 / HyperPod instance via IMDSv2, or nothing if unavailable.
detect_region_from_imds() {
    local imds_token
    imds_token=$(curl -s -X PUT "${IMDS_ENDPOINT}/api/token" \
        -H "X-aws-ec2-metadata-token-ttl-seconds: 21600" \
        --connect-timeout "${IMDS_TIMEOUT_SECONDS}" 2>/dev/null || true)
    if [ -z "${imds_token}" ]; then
        return 0
    fi
    curl -s -H "X-aws-ec2-metadata-token: ${imds_token}" \
        --connect-timeout "${IMDS_TIMEOUT_SECONDS}" \
        "${IMDS_ENDPOINT}/meta-data/placement/region" 2>/dev/null || true
}

# Print the AWS region resolved from the CLI configuration, IMDSv2, or the fallback region.
detect_region() {
    local detected_region
    detected_region=$(aws configure get region 2>/dev/null || true)
    if [ -z "${detected_region}" ]; then
        detected_region=$(detect_region_from_imds)
    fi
    echo "${detected_region:-us-east-1}"
}

# Configuration - Parse command line arguments
AWS_REGION="${1:-${AWS_REGION:-}}"
if [ -z "${AWS_REGION}" ]; then
    AWS_REGION=$(detect_region)
fi

AWS_ACCOUNT_ID="${2:-${AWS_ACCOUNT_ID:-}}"
if [ -z "${AWS_ACCOUNT_ID}" ]; then
    AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text 2>/dev/null || true)
fi

ECR_REPOSITORY="${ECR_REPOSITORY:-lerobot-pi-train}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
LEROBOT_VERSION="${LEROBOT_VERSION:-0.6.1}"

if [ -z "${AWS_REGION}" ]; then
    echo "ERROR: Could not determine AWS region"
    echo "Please specify as argument: ./build_and_push_ecr.sh [REGION] [ACCOUNT_ID]"
    echo "Or set the AWS_REGION environment variable"
    exit 1
fi

if [ -z "${AWS_ACCOUNT_ID}" ]; then
    echo "ERROR: Could not determine AWS Account ID"
    echo "Please specify as argument: ./build_and_push_ecr.sh [REGION] [ACCOUNT_ID]"
    echo "Or set the AWS_ACCOUNT_ID environment variable, or configure AWS CLI: aws configure"
    exit 1
fi

ECR_REGISTRY="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
ECR_URI="${ECR_REGISTRY}/${ECR_REPOSITORY}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOCKERFILE_PATH="${DOCKERFILE_PATH:-${SCRIPT_DIR}/Dockerfile}"

echo "=================================================="
echo "LeRobot pi Training - ECR Build & Push"
echo "=================================================="
echo "AWS Region: ${AWS_REGION}"
echo "ECR Repository: ${ECR_URI}"
echo "Image Tag: ${IMAGE_TAG}"
echo "LeRobot Version: ${LEROBOT_VERSION}"
echo "Dockerfile: ${DOCKERFILE_PATH}"
echo "Build Context: ${SCRIPT_DIR}"
echo ""

if [ ! -f "${DOCKERFILE_PATH}" ]; then
    echo "ERROR: Dockerfile not found at ${DOCKERFILE_PATH}"
    echo "Set DOCKERFILE_PATH or place the Dockerfile next to this script"
    exit 1
fi

# Step 1: Check if ECR repository exists, create if not
echo "[1/4] Checking ECR repository..."
if ! aws ecr describe-repositories --repository-names "${ECR_REPOSITORY}" --region "${AWS_REGION}" > /dev/null 2>&1; then
    echo "  Creating ECR repository: ${ECR_REPOSITORY}"
    aws ecr create-repository \
        --repository-name "${ECR_REPOSITORY}" \
        --region "${AWS_REGION}" \
        --image-scanning-configuration scanOnPush=true
    echo "  Repository created"
else
    echo "  Repository exists"
fi

# Step 2: Authenticate Docker to ECR
echo "[2/4] Authenticating to ECR..."
aws ecr get-login-password --region "${AWS_REGION}" | \
    docker login --username AWS --password-stdin "${ECR_REGISTRY}"
echo "  Authentication successful"

# Step 3: Build Docker image
echo "[3/4] Building Docker image for linux/amd64..."
docker build \
    --platform linux/amd64 \
    --build-arg "LEROBOT_VERSION=${LEROBOT_VERSION}" \
    -t "${ECR_REPOSITORY}:${IMAGE_TAG}" \
    -t "${ECR_URI}:${IMAGE_TAG}" \
    -f "${DOCKERFILE_PATH}" \
    "${SCRIPT_DIR}"
echo "  Build complete"

# Step 4: Push to ECR
echo "[4/4] Pushing image to ECR..."
docker push "${ECR_URI}:${IMAGE_TAG}"
echo "  Push complete"

echo ""
echo "=================================================="
echo "Docker image successfully pushed to ECR"
echo "=================================================="
echo "Image URI: ${ECR_URI}:${IMAGE_TAG}"
