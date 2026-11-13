#!/bin/bash
#SBATCH --job-name=lerobot_pi_docker_build
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --output=../logs/docker_build_%j.out
#SBATCH --error=../logs/docker_build_%j.err

# ================================================
# LeRobot pi Docker Build - Slurm Job Script
# ================================================
# Builds the LeRobot pi training Docker image on a worker node and pushes it to ECR.
#
# Usage (run from the training/ directory):
#   sbatch slurm_build_docker.sh
#
# Prerequisites:
#   - setup.sh has been run (it creates the ../logs directory)
#   - Docker and AWS CLI are available on the worker node
#
# Environment Variables:
#   AWS_REGION / AWS_ACCOUNT_ID (optional): auto-detected by build_and_push_ecr.sh when unset
#   ECR_REPOSITORY, IMAGE_TAG, LEROBOT_VERSION, DOCKERFILE_PATH (optional):
#     passed through to build_and_push_ecr.sh
# ================================================

set -e

echo "=================================================="
echo "LeRobot pi Docker Build - Slurm Job"
echo "=================================================="
echo "Job ID: ${SLURM_JOB_ID}"
echo "Node: ${SLURM_NODELIST}"
echo "Start Time: $(date)"
echo "=================================================="

# SLURM_SUBMIT_DIR points to the directory where sbatch was invoked.
# BASH_SOURCE[0] resolves to the spool copy, not the original location.
SCRIPT_DIR="${SLURM_SUBMIT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"

EXIT_CODE=0
bash "${SCRIPT_DIR}/build_and_push_ecr.sh" || EXIT_CODE=$?

echo ""
echo "=================================================="
if [ ${EXIT_CODE} -eq 0 ]; then
    echo "Docker build and push completed successfully"
else
    echo "Docker build failed with exit code: ${EXIT_CODE}"
fi
echo "End Time: $(date)"
echo "=================================================="

exit ${EXIT_CODE}
