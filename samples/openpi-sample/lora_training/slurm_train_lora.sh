#!/bin/bash
#SBATCH --job-name=openpi_lora_train
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=8
#SBATCH --gres=gpu:1
#SBATCH --mem=28G
#SBATCH --time=48:00:00
#SBATCH --output=../logs/slurm_%j.out
#SBATCH --error=../logs/slurm_%j.err

# Note: Slurm output/error files go to ../../logs/ (relative to tools/docker/)
# Additional log files (.log, .err) are managed dynamically via OPENPI_LOG_DIR

# ================================================
# OpenPI LoRA Fine-tuning - Slurm Job Script
# ================================================
# Usage:
#   sbatch /path/to/slurm_train_lora.sh [CONFIG_NAME] [EXP_NAME]
# Example:
#   sbatch slurm_train_lora.sh pi0_libero_low_mem_finetune my_lora_run
# ================================================

set -e

# Parse arguments
CONFIG_NAME="${1:-pi0_libero_low_mem_finetune}"
EXP_NAME="${2:-lora_run_$(date +%Y%m%d_%H%M%S)}"

echo "=================================================="
echo "OpenPI LoRA Training Job"
echo "=================================================="
echo "Job ID: ${SLURM_JOB_ID}"
echo "Node: ${SLURM_NODELIST}"
echo "Config: ${CONFIG_NAME}"
echo "Experiment: ${EXP_NAME}"
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

# Setup log redirection
mkdir -p "${OPENPI_LOG_DIR}"
LOG_FILE="${OPENPI_LOG_DIR}/train_${SLURM_JOB_ID}.log"
ERR_FILE="${OPENPI_LOG_DIR}/train_${SLURM_JOB_ID}.err"

# Redirect stdout and stderr to log files
exec 1> >(tee -a "${LOG_FILE}")
exec 2> >(tee -a "${ERR_FILE}" >&2)

echo "Logs will be written to:"
echo "  Output: ${LOG_FILE}"
echo "  Error: ${ERR_FILE}"
echo ""

# Environment setup
export HF_TOKEN="${HF_TOKEN:-}"  # Inherit from login node

# JAX/XLA configuration for A10G / L4 / L40S GPUs
export XLA_PYTHON_CLIENT_MEM_FRACTION=0.9
export XLA_FLAGS="--xla_gpu_triton_gemm_any=True"

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
echo "  GPU: $(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null || echo 'Not available on controller node')"
echo ""

# Verify project directory exists
if [ ! -d "${OPENPI_PROJECT_ROOT}" ]; then
    echo "ERROR: Project directory not found: ${OPENPI_PROJECT_ROOT}"
    exit 1
fi

# Change to project directory
cd "${OPENPI_PROJECT_ROOT}"

# Run training with Pyxis (Slurm container integration)
echo "Starting training..."
srun --container-image="${CONTAINER_IMAGE}" \
     --container-mounts="/fsx:/fsx" \
     --container-workdir="${OPENPI_PROJECT_ROOT}" \
     bash -c "
         set -e
         export OPENPI_DATA_HOME=${OPENPI_DATA_HOME}
         export HF_TOKEN=${HF_TOKEN}
         export XLA_PYTHON_CLIENT_MEM_FRACTION=${XLA_PYTHON_CLIENT_MEM_FRACTION}
         export XLA_FLAGS='${XLA_FLAGS}'
         export PYTHONPATH=${OPENPI_PROJECT_ROOT}:${OPENPI_PROJECT_ROOT}/src

         echo 'Container environment setup complete'
         echo 'Python version:' \$(python --version)
         echo 'JAX version:' \$(python -c 'import jax; print(jax.__version__)')
         echo 'GPU devices:' \$(python -c 'import jax; print(jax.devices())')
         echo ''

         # Run training
         # Note: Config name is passed as first positional argument (tyro convention)
         # Note: For boolean flags, use --no-FLAG to set False (tyro convention)
         uv run scripts/train.py \\
             ${CONFIG_NAME} \\
             --exp-name ${EXP_NAME} \\
             --overwrite \\
             --no-wandb-enabled
     "

EXIT_CODE=$?

echo ""
echo "=================================================="
if [ ${EXIT_CODE} -eq 0 ]; then
    echo "✅ Training completed successfully"
    echo "Checkpoints saved to: ${OPENPI_PROJECT_ROOT}/checkpoints/${CONFIG_NAME}/${EXP_NAME}/"
else
    echo "❌ Training failed with exit code: ${EXIT_CODE}"
fi
echo "End Time: $(date)"
echo "=================================================="

exit ${EXIT_CODE}
