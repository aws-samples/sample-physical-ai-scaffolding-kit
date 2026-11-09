#!/bin/bash
#SBATCH --job-name=openpi_norm_stats
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=8
#SBATCH --mem=16G
#SBATCH --time=4:00:00
#SBATCH --output=../logs/slurm_%j.out
#SBATCH --error=../logs/slurm_%j.err

# Note: Slurm output file goes to ../../logs/ (relative to tools/docker/)
# Additional log files (.log, .err) are managed dynamically via OPENPI_LOG_DIR

# ================================================
# OpenPI Normalization Statistics - Slurm Job Script
# ================================================
# Compute normalization statistics for dataset
# This job does NOT require GPU
# ================================================
# Usage:
#   sbatch /path/to/slurm_compute_norm_stats.sh [CONFIG_NAME]
# Example:
#   sbatch slurm_compute_norm_stats.sh pi0_libero_low_mem_finetune
#
# Environment Variables:
#   MLFLOW_TRACKING_URI (optional): SageMaker MLflow App ARN. MLflow tracking is
#     disabled when unset (written to ~/.bashrc by setup.sh)
#   MLFLOW_EXPERIMENT_NAME (optional): MLflow experiment name
#     (default: project_name of the training config)
# ================================================

set -e

# Parse arguments
CONFIG_NAME="${1:-pi0_libero_low_mem_finetune}"

echo "=================================================="
echo "OpenPI Normalization Statistics Computation"
echo "=================================================="
echo "Job ID: ${SLURM_JOB_ID}"
echo "Node: ${SLURM_NODELIST}"
echo "Config: ${CONFIG_NAME}"
echo "Start Time: $(date)"
echo "=================================================="

# Verify required environment variables are set
if [ -z "${OPENPI_PROJECT_ROOT}" ] || [ -z "${OPENPI_DATA_HOME}" ] || [ -z "${OPENPI_LOG_DIR}" ]; then
    echo "ERROR: Required environment variables not set"
    echo "Please run setup.sh first to configure the environment:"
    echo "  cd /fsx/ubuntu/samples/openpi-sample/lora_training"
    echo "  ./setup.sh --hf-token \"your_hf_token\""
    echo ""
    echo "Or manually set the following in ~/.bashrc:"
    echo "  export OPENPI_BASE_DIR=<your_base_directory>"
    echo "  export OPENPI_PROJECT_ROOT=\${OPENPI_BASE_DIR}/openpi"
    echo "  export OPENPI_DATA_HOME=\${OPENPI_BASE_DIR}/.cache"
    echo "  export OPENPI_LOG_DIR=\${OPENPI_BASE_DIR}/logs"
    echo "  export HF_TOKEN=<your_huggingface_token>"
    exit 1
fi

# Environment setup
export HF_TOKEN="${HF_TOKEN:-}"  # Inherit from login node

# The CUDA base image bakes in NVIDIA_VISIBLE_DEVICES=all, which makes the
# enroot NVIDIA hook fail on CPU-only nodes. This job needs no GPU, so disable it.
export NVIDIA_VISIBLE_DEVICES=void

# Setup log redirection
mkdir -p "${OPENPI_LOG_DIR}"
LOG_FILE="${OPENPI_LOG_DIR}/norm_stats_${SLURM_JOB_ID}.log"
ERR_FILE="${OPENPI_LOG_DIR}/norm_stats_${SLURM_JOB_ID}.err"

# Redirect stdout and stderr to log files
exec 1> >(tee -a "${LOG_FILE}")
exec 2> >(tee -a "${ERR_FILE}" >&2)

echo "Logs will be written to:"
echo "  Output: ${LOG_FILE}"
echo "  Error: ${ERR_FILE}"
echo ""

# Verify HF_TOKEN is set
if [ -z "${HF_TOKEN}" ]; then
    echo "ERROR: HF_TOKEN is not set"
    echo "Please export HF_TOKEN on the login node before submitting the job"
    exit 1
fi

# Verify ENROOT_DATA_PATH is set
if [ -z "${ENROOT_DATA_PATH}" ]; then
    echo "ERROR: ENROOT_DATA_PATH not set"
    echo "Please export ENROOT_DATA_PATH=/fsx/enroot/data in ~/.bashrc"
    exit 1
fi

# Container configuration
CONTAINER_NAME="openpi-lora-train+latest.sqsh"
CONTAINER_IMAGE="${ENROOT_DATA_PATH}/${CONTAINER_NAME}"

# Verify container exists
if [ ! -f "${CONTAINER_IMAGE}" ]; then
    echo "ERROR: Container not found: ${CONTAINER_IMAGE}"
    echo "Please run hyperpod_import_container.sh first"
    echo ""
    echo "Available containers:"
    ls -lh "${ENROOT_DATA_PATH}"
    exit 1
fi

echo ""
echo "Environment:"
echo "  OPENPI_PROJECT_ROOT: ${OPENPI_PROJECT_ROOT}"
echo "  OPENPI_DATA_HOME: ${OPENPI_DATA_HOME}"
echo "  Container: ${CONTAINER_IMAGE}"
echo ""

# Verify project directory exists
if [ ! -d "${OPENPI_PROJECT_ROOT}" ]; then
    echo "ERROR: Project directory not found: ${OPENPI_PROJECT_ROOT}"
    exit 1
fi

# Change to project directory
cd "${OPENPI_PROJECT_ROOT}"

# MLflow settings passed into the container. When MLflow is disabled,
# MLFLOW_TRACKING_URI is unset because bash under srun reads ~/.bashrc, where
# setup.sh writes it, so that the computation behaves exactly as without MLflow.
# This job has no GPU and does not log system metrics.
MLFLOW_EXPORTS="
         unset MLFLOW_TRACKING_URI"
if [ -n "${MLFLOW_TRACKING_URI}" ]; then
    echo "MLflow: enabled (${MLFLOW_TRACKING_URI})"
    OPENPI_GIT_COMMIT=$(git -C "${OPENPI_PROJECT_ROOT}" rev-parse --short HEAD)
    MLFLOW_EXPORTS="
         export MLFLOW_TRACKING_URI='${MLFLOW_TRACKING_URI}'
         export AWS_DEFAULT_REGION='${AWS_DEFAULT_REGION:-$(echo "${MLFLOW_TRACKING_URI}" | cut -d: -f4)}'
         export OPENPI_GIT_COMMIT='${OPENPI_GIT_COMMIT}'
         export PASK_CLUSTER_NAME='${PASK_CLUSTER_NAME}'
         export SLURM_JOB_ID='${SLURM_JOB_ID}'"
    if [ -n "${MLFLOW_EXPERIMENT_NAME}" ]; then
        MLFLOW_EXPORTS="${MLFLOW_EXPORTS}
         export MLFLOW_EXPERIMENT_NAME='${MLFLOW_EXPERIMENT_NAME}'"
    fi
else
    echo "MLflow: disabled (MLFLOW_TRACKING_URI is not set)"
fi

# Run normalization statistics computation
echo "Starting normalization statistics computation..."
srun --container-image="${CONTAINER_IMAGE}" \
     --container-mounts="/fsx:/fsx" \
     --container-workdir="${OPENPI_PROJECT_ROOT}" \
     bash -c "
         set -e
         export OPENPI_DATA_HOME=${OPENPI_DATA_HOME}
         export HF_TOKEN=${HF_TOKEN}
         export PYTHONPATH=${OPENPI_PROJECT_ROOT}:${OPENPI_PROJECT_ROOT}/src:${OPENPI_PROJECT_ROOT}/packages/openpi-client/src
         ${MLFLOW_EXPORTS}

         # Clear problematic XLA flags
         unset XLA_FLAGS
         export XLA_FLAGS=''

         # The GPU is hidden above (NVIDIA_VISIBLE_DEVICES=void), so keep JAX
         # from initializing its CUDA backend, which fails without a GPU.
         export JAX_PLATFORMS=cpu

         echo 'Container environment setup complete'
         echo 'Working directory:' \$(pwd)
         echo 'Python version:' \$(python --version)
         echo ''

         # Create cache directory
         mkdir -p ${OPENPI_DATA_HOME}

         # Run norm stats computation
         # Note: Config name is passed as --config-name argument (tyro convention)
         # Note: the container venv is on a read-only squashfs mount, so uv run
         # cannot re-sync the project. The interpreter on PATH is that venv.
         # Note: exec replaces this shell so that SIGTERM from Slurm reaches Python.
         exec python scripts/compute_norm_stats.py --config-name ${CONFIG_NAME}
     "

EXIT_CODE=$?

echo ""
echo "=================================================="
if [ ${EXIT_CODE} -eq 0 ]; then
    echo "✅ Normalization statistics computed successfully"
    OUTPUT_FILE="${OPENPI_PROJECT_ROOT}/assets/physical-intelligence/libero/norm_stats.json"
    echo "Output file: ${OUTPUT_FILE}"

    # Verify output file
    if [ -f "${OUTPUT_FILE}" ]; then
        echo "File size: $(du -h ${OUTPUT_FILE} | cut -f1)"
        echo "Content preview:"
        head -20 "${OUTPUT_FILE}"
    else
        echo "⚠ Warning: Output file not found at expected location"
    fi
else
    echo "❌ Computation failed with exit code: ${EXIT_CODE}"
fi
echo "End Time: $(date)"
echo "=================================================="

exit ${EXIT_CODE}
