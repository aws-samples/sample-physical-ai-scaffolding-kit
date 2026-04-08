#!/bin/bash
#SBATCH --job-name=gr00t_finetune
#SBATCH --nodes=1
#SBATCH --output=/fsx/ubuntu/joblog/finetune_%j.out
#SBATCH --error=/fsx/ubuntu/joblog/finetune_%j.err

# ================================================
# GR00T Fine-tuning - Slurm Job Script (Container)
# ================================================
# Runs GR00T fine-tuning inside the enroot container
# built by build_and_push_ecr.sh and imported by
# hyperpod_import_container.sh.
#
# Prerequisites:
#   - Enroot container imported at /fsx/enroot/data/gr00t-train+latest.sqsh
#   - mkdir -p /fsx/ubuntu/joblog
#
# Usage:
#   sbatch slurm_finetune_container.sh
#
# Environment Variables (optional):
#   NUM_GPUS: Number of GPUs to use (default: 1)
#   MAX_STEPS: Maximum training steps (default: 2000)
#   SAVE_STEPS: Save checkpoint every N steps (default: 2000)
#   GLOBAL_BATCH_SIZE: Global batch size (default: 32)
#   OUTPUT_DIR: Output directory for checkpoints (default: /fsx/s3link/so100)
#   DATASET_PATH: Path to training dataset (default: ./demo_data/cube_to_bowl_5)
#   BASE_MODEL: Base model path (default: nvidia/GR00T-N1.6-3B)
# ================================================

set -e

# Configuration with defaults
GR00T_HOME="/workspace/gr00t"
NUM_GPUS="${NUM_GPUS:-1}"
MAX_STEPS="${MAX_STEPS:-2000}"
SAVE_STEPS="${SAVE_STEPS:-2000}"
SAVE_TOTAL_LIMIT="${SAVE_TOTAL_LIMIT:-5}"
GLOBAL_BATCH_SIZE="${GLOBAL_BATCH_SIZE:-32}"
OUTPUT_DIR="${OUTPUT_DIR:-/fsx/s3link/so100}"
DATASET_PATH="${DATASET_PATH:-./demo_data/cube_to_bowl_5}"
BASE_MODEL="${BASE_MODEL:-nvidia/GR00T-N1.6-3B}"
EMBODIMENT_TAG="${EMBODIMENT_TAG:-NEW_EMBODIMENT}"
MODALITY_CONFIG="${MODALITY_CONFIG:-examples/SO100/so100_config.py}"
DATALOADER_NUM_WORKERS="${DATALOADER_NUM_WORKERS:-4}"

echo "=================================================="
echo "GR00T Fine-tuning - Container Job"
echo "=================================================="
echo "Job ID: ${SLURM_JOB_ID}"
echo "Node: ${SLURM_NODELIST}"
echo "Start Time: $(date)"
echo "GR00T_HOME: ${GR00T_HOME}"
echo "Base Model: ${BASE_MODEL}"
echo "Dataset: ${DATASET_PATH}"
echo "Output Dir: ${OUTPUT_DIR}"
echo "Num GPUs: ${NUM_GPUS}"
echo "Max Steps: ${MAX_STEPS}"
echo "Batch Size: ${GLOBAL_BATCH_SIZE}"
echo "=================================================="

# Generate CUDA_VISIBLE_DEVICES based on NUM_GPUS (e.g., "0" for 1, "0,1" for 2)
if [ "${NUM_GPUS}" -eq 1 ]; then
    CUDA_VISIBLE_DEVICES="0"
else
    CUDA_VISIBLE_DEVICES=$(seq -s, 0 $((NUM_GPUS - 1)))
fi

# Set PyTorch distributed environment variables
export CUDA_VISIBLE_DEVICES
export MASTER_ADDR="${MASTER_ADDR:-localhost}"
export MASTER_PORT="${MASTER_PORT:-29500}"
export WORLD_SIZE="${NUM_GPUS}"
export RANK="${RANK:-0}"
export LOCAL_RANK="${LOCAL_RANK:-0}"

# Container configuration
CONTAINER_IMAGE="/fsx/enroot/data/gr00t-train+latest.sqsh"

# Use srun with Pyxis to execute inside the enroot container.
# Container options are passed to srun (not #SBATCH) so that the
# batch script itself runs on the host where srun is available.
srun --container-image="${CONTAINER_IMAGE}" \
    --container-mounts="/fsx:/fsx" \
    --container-workdir="${GR00T_HOME}" \
    "${GR00T_HOME}/.venv/bin/python" \
    gr00t/experiment/launch_finetune.py \
    --base-model-path "${BASE_MODEL}" \
    --dataset-path "${DATASET_PATH}" \
    --embodiment-tag "${EMBODIMENT_TAG}" \
    --modality-config-path "${MODALITY_CONFIG}" \
    --num-gpus "${NUM_GPUS}" \
    --output-dir "${OUTPUT_DIR}" \
    --save-total-limit "${SAVE_TOTAL_LIMIT}" \
    --save-steps "${SAVE_STEPS}" \
    --max-steps "${MAX_STEPS}" \
    --no-use-wandb \
    --global-batch-size "${GLOBAL_BATCH_SIZE}" \
    --color-jitter-params brightness 0.3 contrast 0.4 saturation 0.5 hue 0.08 \
    --dataloader-num-workers "${DATALOADER_NUM_WORKERS}"

EXIT_CODE=$?

echo ""
echo "=================================================="
if [ ${EXIT_CODE} -eq 0 ]; then
    echo "Fine-tuning completed successfully"
else
    echo "Fine-tuning failed with exit code: ${EXIT_CODE}"
fi
echo "End Time: $(date)"
echo "=================================================="

exit ${EXIT_CODE}
