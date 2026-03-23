#!/bin/bash
# Build GR00T Docker image on a worker node and push to ECR
#
# Prerequisites:
#   - GR00T_HOME environment variable must be set (path to Isaac-GR00T repository)
#   - Docker must be available on the worker node
#   - AWS CLI configured with ECR access
#
# Usage:
#   Via Slurm:
#     sbatch slurm_build_docker.sh
#
# Environment Variables:
#   GR00T_HOME (required): Path to Isaac-GR00T repository
#   AWS_REGION (optional): AWS region (default: auto-detect from EC2 metadata)
#   AWS_ACCOUNT_ID (optional): AWS account ID (default: auto-detect from STS)
#   ECR_REPOSITORY (optional): ECR repository name (default: gr00t-train)
#   IMAGE_TAG (optional): Docker image tag (default: latest)

set -e

# Configuration
ECR_REPOSITORY="${ECR_REPOSITORY:-gr00t-train}"
IMAGE_TAG="${IMAGE_TAG:-latest}"

# Get AWS Region
# Priority: 1. Environment variable  2. EC2 metadata (IMDSv2)  3. Fallback
if [ -z "${AWS_REGION}" ]; then
    TOKEN=$(curl -X PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 21600" -s 2>/dev/null)
    AWS_REGION=$(curl -H "X-aws-ec2-metadata-token: $TOKEN" -s http://169.254.169.254/latest/meta-data/placement/region 2>/dev/null)
    AWS_REGION="${AWS_REGION:-us-east-1}"  # Fallback if metadata unavailable
fi

# Get AWS Account ID
# Priority: 1. Environment variable  2. AWS STS  3. Error
if [ -z "${AWS_ACCOUNT_ID}" ]; then
    AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text 2>/dev/null)
fi

# Validate AWS Account ID was retrieved
if [ -z "${AWS_ACCOUNT_ID}" ]; then
    echo "ERROR: Could not determine AWS Account ID"
    echo "Set AWS_ACCOUNT_ID environment variable or configure AWS CLI"
    exit 1
fi

ECR_URI="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPOSITORY}"

# Build ID for temporary directory isolation
# Priority: 1. JOB_ID (set by Slurm wrapper)  2. PID + timestamp
JOB_ID="${JOB_ID:-$$_$(date +%s)}"

# Verify GR00T_HOME is set
if [ -z "${GR00T_HOME}" ]; then
    echo "ERROR: GR00T_HOME environment variable is not set"
    echo "Please set it to the Isaac-GR00T repository path:"
    echo "  export GR00T_HOME=/fsx/ubuntu/Isaac-GR00T"
    exit 1
fi

# Resolve absolute path
GR00T_HOME="$(cd "${GR00T_HOME}" && pwd)"
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
DOCKERFILE_PATH="${SCRIPT_DIR}/Dockerfile"


echo "=================================================="
echo "GR00T Docker Image - Worker Node Build & ECR Push"
echo "=================================================="
echo "ECR Repository: ${ECR_URI}"
echo "Image Tag: ${IMAGE_TAG}"
echo "Dockerfile: ${DOCKERFILE_PATH}"
echo "GR00T_HOME: ${GR00T_HOME}"
echo ""

# Verify Script directory structure
if [ ! -f "${DOCKERFILE_PATH}" ]; then
    echo "ERROR: Dockerfile not found at ${DOCKERFILE_PATH}"
    echo "Please verify ${DOCKERFILE_PATH} exists"
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
    docker login --username AWS --password-stdin "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
echo "  Authentication successful"

# Step 3: Build Docker image
echo "[3/4] Building Docker image..."
cd ${GR00T_HOME}
docker build \
    --no-cache \
    --platform linux/amd64 \
    -t "${ECR_REPOSITORY}:${IMAGE_TAG}" \
    -t "${ECR_URI}:${IMAGE_TAG}" -f "${DOCKERFILE_PATH}" .
echo "  Build complete"

# Step 4: Push to ECR
echo "[4/4] Pushing image to ECR..."
docker push "${ECR_URI}:${IMAGE_TAG}"
echo "  Push complete"
