#!/bin/bash
# Import Docker image from ECR to Enroot on HyperPod
# Run this script on the HyperPod controller node
#
# Prerequisites:
#   - AWS CLI configured with ECR access
#   - Enroot installed on HyperPod cluster
#   - FSx for Lustre mounted at /fsx
#
# Usage:
#   bash ./hyperpod_import_container.sh [IMAGE_TAG] [AWS_REGION] [AWS_ACCOUNT_ID]
#
#   Examples:
#     ./hyperpod_import_container.sh                                # Use EC2 metadata
#     ./hyperpod_import_container.sh latest                          # Specify image tag only
#     ./hyperpod_import_container.sh latest us-west-2                # Specify tag and region
#     ./hyperpod_import_container.sh latest us-west-2 123456789012   # Specify all
#
# Environment Variables:
#   IMAGE_TAG (optional): Docker image tag to import (default: latest)
#   AWS_REGION (optional): AWS region (default: auto-detect from EC2 metadata)
#   AWS_ACCOUNT_ID (optional): AWS account ID (default: auto-detect from STS)
#   ENROOT_CACHE_PATH (optional): Enroot cache directory (default: /fsx/enroot)
#   ENROOT_DATA_PATH (optional): Enroot data directory (default: /fsx/enroot/data)

set -e

# Configuration - Parse command line arguments
# Priority: 1. Command line args  2. Environment variables  3. Auto-detect  4. Fallback
IMAGE_TAG="${1:-${IMAGE_TAG:-latest}}"

# Get AWS Region
# Priority: 1. Argument $2  2. Environment variable  3. EC2 metadata (IMDSv2)  4. Fallback
if [ -n "$2" ]; then
    AWS_REGION="$2"
elif [ -z "${AWS_REGION}" ]; then
    # Try EC2 instance metadata (IMDSv2)
    TOKEN=$(curl -X PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 21600" -s 2>/dev/null)
    AWS_REGION=$(curl -H "X-aws-ec2-metadata-token: $TOKEN" -s http://169.254.169.254/latest/meta-data/placement/region 2>/dev/null)
    AWS_REGION="${AWS_REGION:-us-east-1}"  # Fallback if metadata unavailable
fi

# Get AWS Account ID
# Priority: 1. Argument $3  2. Environment variable  3. AWS STS  4. Error
if [ -n "$3" ]; then
    AWS_ACCOUNT_ID="$3"
elif [ -z "${AWS_ACCOUNT_ID}" ]; then
    AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text 2>/dev/null)
fi

# Validate AWS Account ID was retrieved
if [ -z "${AWS_ACCOUNT_ID}" ]; then
    echo "ERROR: Could not determine AWS Account ID"
    echo "Please specify as argument: ./hyperpod_import_container.sh [TAG] [REGION] [ACCOUNT_ID]"
    echo "Or set AWS_ACCOUNT_ID environment variable or configure AWS CLI"
    exit 1
fi

ECR_REPOSITORY="gr00t-train"
ECR_URI="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPOSITORY}:${IMAGE_TAG}"

# Enroot configuration
ENROOT_CACHE_PATH="${ENROOT_CACHE_PATH:-/fsx/enroot}"
ENROOT_DATA_PATH="${ENROOT_DATA_PATH:-/fsx/enroot/data}"

echo "=================================================="
echo "GR00T Training - Enroot Import Script"
echo "=================================================="
echo "ECR Image: ${ECR_URI}"
echo "Enroot Cache: ${ENROOT_CACHE_PATH}"
echo "Enroot Data: ${ENROOT_DATA_PATH}"
echo ""

# Step 1: Create enroot directories
echo "[1/5] Setting up Enroot directories..."
mkdir -p "${ENROOT_CACHE_PATH}"
mkdir -p "${ENROOT_DATA_PATH}"
export ENROOT_CACHE_PATH
export ENROOT_DATA_PATH
echo "  Done: Directories created"

# Step 2-3: Check local Docker cache, pull from ECR only if needed
echo "[2/5] Checking local Docker cache..."
if docker image inspect "${ECR_URI}" > /dev/null 2>&1; then
    echo "  Done: Image found in local Docker cache, skipping ECR pull"
else
    echo "  Image not found locally, pulling from ECR..."
    echo "[3/5] Authenticating to ECR..."
    aws ecr get-login-password --region "${AWS_REGION}" | \
        docker login --username AWS --password-stdin "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
    echo "  Done: Authentication successful"
    echo "[3/5] Pulling Docker image from ECR..."
    docker pull "${ECR_URI}"
    echo "  Done: Image pulled successfully"
fi

# Step 4: Import from local Docker daemon to Enroot
echo "[4/5] Importing container to Enroot from local Docker..."
# Use dockerd:// protocol to import from local Docker daemon
# This avoids ECR authentication issues with enroot
# Must use the full ECR URI as Docker stores it with the complete name
CONTAINER_FILENAME="${ECR_REPOSITORY}+${IMAGE_TAG}.sqsh"
CONTAINER_PATH="${ENROOT_DATA_PATH}/${CONTAINER_FILENAME}"
if [ -f "${CONTAINER_PATH}" ]; then
    echo "  Existing container found. Removing for re-import..."
    rm -f "${CONTAINER_PATH}"
fi
enroot import -o "${CONTAINER_PATH}" "dockerd://${ECR_URI}"
echo "  Done: Import successful"

# Step 5: Verify imported container
echo "[5/5] Verifying imported container..."
CONTAINER_PATH="${ENROOT_DATA_PATH}/${CONTAINER_FILENAME}"
if [ ! -f "${CONTAINER_PATH}" ]; then
    echo "  ERROR: Container file not found at: ${CONTAINER_PATH}"
    echo ""
    echo "Files in ${ENROOT_DATA_PATH}:"
    ls -lh "${ENROOT_DATA_PATH}/" 2>/dev/null || echo "  Directory is empty or does not exist"
    exit 1
fi
CONTAINER_SIZE=$(du -h "${CONTAINER_PATH}" | cut -f1)
echo "  Done: Container imported: ${CONTAINER_FILENAME}"
echo "  Container size: ${CONTAINER_SIZE}"

