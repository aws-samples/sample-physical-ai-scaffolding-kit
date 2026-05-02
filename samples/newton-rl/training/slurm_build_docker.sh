#!/bin/bash
#SBATCH --job-name=isaac_newton_build
#SBATCH --nodes=1
#SBATCH --output=/fsx/ubuntu/isaac-lab-newton/logs/docker_build_%j.out
#SBATCH --error=/fsx/ubuntu/isaac-lab-newton/logs/docker_build_%j.err

# Builds the Isaac Lab Newton Docker image on a HyperPod node and pushes it to ECR.
#
# Usage:
#   mkdir -p /fsx/ubuntu/isaac-lab-newton/logs
#   ACCEPT_EULA=Y sbatch slurm_build_docker.sh

set -euo pipefail

echo "=================================================="
echo "Isaac Lab Newton Docker Build - Slurm Job"
echo "=================================================="
echo "Job ID: ${SLURM_JOB_ID}"
echo "Node: ${SLURM_NODELIST}"
echo "Start Time: $(date)"
echo "=================================================="

SCRIPT_DIR="${SLURM_SUBMIT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"

bash "${SCRIPT_DIR}/build_and_push_ecr.sh"

echo "=================================================="
echo "Docker build and push completed successfully"
echo "End Time: $(date)"
echo "=================================================="
