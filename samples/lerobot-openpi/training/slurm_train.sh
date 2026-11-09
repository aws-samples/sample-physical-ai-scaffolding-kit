#!/bin/bash
#SBATCH --job-name=lerobot_pi_train
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=8
#SBATCH --gres=gpu:1
#SBATCH --mem=28G
#SBATCH --time=48:00:00
#SBATCH --output=../logs/slurm_%j.out
#SBATCH --error=../logs/slurm_%j.err

# ================================================
# LeRobot pi0 / pi0.5 Fine-tuning - Slurm Job Script (1 GPU)
# ================================================
# Runs lerobot-train inside the lerobot-pi-train container on a single GPU.
#
# The #SBATCH values above target ml.g6.2xlarge (1x NVIDIA L4 24 GB, 8 vCPU, 32 GiB).
# Override them on the sbatch command line for other instance types, e.g.:
#   sbatch --cpus-per-task=16 --mem=60G slurm_train.sh my_job
#
# Usage (run from the training/ directory):
#   sbatch slurm_train.sh [JOB_NAME]
# Examples:
#   sbatch slurm_train.sh
#   FINETUNE_MODE=expert_only sbatch slurm_train.sh pi05_expert_run
#   RESUME=true sbatch slurm_train.sh pi05_lora_20261001_120000
#
# Arguments:
#   JOB_NAME (optional): $1, then the JOB_NAME env var (default: pi05_lora_<timestamp>)
#
# Environment Variables (optional):
#   POLICY_TYPE       Policy type: pi05 or pi0 (default: pi05)
#   PRETRAINED_PATH   Base weights (default: lerobot/pi05_base)
#   DATASET_REPO_ID   Dataset repo id (default: lerobot/libero)
#   DATASET_ROOT      Local dataset root (default: ${LEROBOT_PI_HF_LEROBOT_HOME}/${DATASET_REPO_ID})
#   FINETUNE_MODE     lora / expert_only / full (default: lora)
#   LORA_R            LoRA rank (default: 16)
#   LORA_ALPHA        LoRA alpha (default: 16)
#   STEPS             Training steps (default: 30000). With RESUME=true, it is passed
#                     only when set explicitly, to extend the total steps of the run
#   BATCH_SIZE        Batch size (default: 4)
#   SAVE_FREQ         Checkpoint interval in steps (default: 5000)
#   LOG_FREQ          Log interval in steps (default: 100)
#   NUM_WORKERS       DataLoader workers (default: 4)
#   COMPILE_MODEL     Enable torch.compile: true / false (default: false)
#   RESUME            Resume from ${OUTPUT_DIR}/checkpoints/last: true / false (default: false).
#                     The other settings come from the checkpoint's train_config.json
#   OUTPUT_DIR        Output directory (default: ${LEROBOT_PI_OUTPUT_DIR}/${JOB_NAME})
#   EXTRA_ARGS        Additional arguments appended to lerobot-train (default: empty)
#   IMAGE_TAG         Container image tag (default: latest)
#   CONTAINER_IMAGE   Full path to the .sqsh file (default: ${ENROOT_DATA_PATH}/lerobot-pi-train+${IMAGE_TAG}.sqsh)
#
# The learning rate is not passed; the LeRobot policy preset is used.
# ================================================

set -e

JOB_NAME="${1:-${JOB_NAME:-pi05_lora_$(date +%Y%m%d_%H%M%S)}}"

# Kept before the default is applied so that a resumed run changes its total
# steps only when STEPS is given explicitly.
STEPS_OVERRIDE="${STEPS:-}"

POLICY_TYPE="${POLICY_TYPE:-pi05}"
PRETRAINED_PATH="${PRETRAINED_PATH:-lerobot/pi05_base}"
DATASET_REPO_ID="${DATASET_REPO_ID:-lerobot/libero}"
FINETUNE_MODE="${FINETUNE_MODE:-lora}"
LORA_R="${LORA_R:-16}"
LORA_ALPHA="${LORA_ALPHA:-16}"
STEPS="${STEPS:-30000}"
BATCH_SIZE="${BATCH_SIZE:-4}"
SAVE_FREQ="${SAVE_FREQ:-5000}"
LOG_FREQ="${LOG_FREQ:-100}"
NUM_WORKERS="${NUM_WORKERS:-4}"
COMPILE_MODEL="${COMPILE_MODEL:-false}"
RESUME="${RESUME:-false}"
EXTRA_ARGS="${EXTRA_ARGS:-}"
IMAGE_TAG="${IMAGE_TAG:-latest}"

MIN_DRIVER_VERSION="570.86"

echo "=================================================="
echo "LeRobot pi Training Job"
echo "=================================================="
echo "Job ID: ${SLURM_JOB_ID}"
echo "Node: ${SLURM_NODELIST}"
echo "Job Name: ${JOB_NAME}"
echo "Start Time: $(date)"
echo "=================================================="

echo "[1/4] Checking environment..."

if [ -z "${LEROBOT_PI_BASE_DIR}" ] || [ -z "${LEROBOT_PI_LOG_DIR}" ] || [ -z "${LEROBOT_PI_OUTPUT_DIR}" ] \
    || [ -z "${LEROBOT_PI_CACHE_DIR}" ] || [ -z "${LEROBOT_PI_HF_HOME}" ] || [ -z "${LEROBOT_PI_HF_LEROBOT_HOME}" ]; then
    echo "ERROR: Required environment variables not set"
    echo "Please run setup.sh first to configure the environment:"
    echo "  cd /fsx/ubuntu/sample-physical-ai-scaffolding-kit/samples/lerobot-openpi/training"
    echo "  ./setup.sh --hf-token \"your_hf_token\""
    echo "  source ~/.bashrc"
    exit 1
fi

mkdir -p "${LEROBOT_PI_LOG_DIR}"
LOG_FILE="${LEROBOT_PI_LOG_DIR}/train_${SLURM_JOB_ID}.log"
ERR_FILE="${LEROBOT_PI_LOG_DIR}/train_${SLURM_JOB_ID}.err"

exec 1> >(tee -a "${LOG_FILE}")
exec 2> >(tee -a "${ERR_FILE}" >&2)

echo "  Logs will be written to:"
echo "    Output: ${LOG_FILE}"
echo "    Error:  ${ERR_FILE}"

if [ -z "${HF_TOKEN}" ]; then
    echo "ERROR: HF_TOKEN is not set"
    echo "Please run setup.sh with --hf-token, or export HF_TOKEN on the login node before submitting the job"
    exit 1
fi

if [ -z "${ENROOT_DATA_PATH}" ]; then
    echo "ERROR: ENROOT_DATA_PATH not set"
    echo "Please run setup.sh first, or export ENROOT_DATA_PATH=/fsx/enroot/data in ~/.bashrc"
    exit 1
fi

CONTAINER_IMAGE="${CONTAINER_IMAGE:-${ENROOT_DATA_PATH}/lerobot-pi-train+${IMAGE_TAG}.sqsh}"

if [ ! -f "${CONTAINER_IMAGE}" ]; then
    echo "ERROR: Container not found: ${CONTAINER_IMAGE}"
    echo "Please run hyperpod_import_container.sh first"
    echo ""
    echo "Available containers:"
    ls -lh "${ENROOT_DATA_PATH}"
    exit 1
fi

echo "  Container: ${CONTAINER_IMAGE}"
echo "  GPU: $(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null || echo 'Not available on this node')"
echo ""

echo "[2/4] Validating training options..."

DATASET_ROOT="${DATASET_ROOT:-${LEROBOT_PI_HF_LEROBOT_HOME}/${DATASET_REPO_ID}}"
OUTPUT_DIR="${OUTPUT_DIR:-${LEROBOT_PI_OUTPUT_DIR}/${JOB_NAME}}"
RESUME_CONFIG_PATH="${OUTPUT_DIR}/checkpoints/last/pretrained_model/train_config.json"

case "${FINETUNE_MODE}" in
    lora|expert_only|full)
        ;;
    *)
        echo "ERROR: Invalid FINETUNE_MODE: ${FINETUNE_MODE}"
        echo "Valid values: lora, expert_only, full"
        exit 1
        ;;
esac

if [ "${RESUME}" = "true" ]; then
    if [ ! -f "${RESUME_CONFIG_PATH}" ]; then
        echo "ERROR: Checkpoint config for resuming not found: ${RESUME_CONFIG_PATH}"
        echo "Please check JOB_NAME / OUTPUT_DIR of the run to resume"
        exit 1
    fi
elif [ -e "${OUTPUT_DIR}" ]; then
    echo "ERROR: Output directory already exists: ${OUTPUT_DIR}"
    echo "lerobot-train refuses to overwrite it. Use a different JOB_NAME, or set RESUME=true to resume:"
    echo "  RESUME=true sbatch ./slurm_train.sh ${JOB_NAME}"
    exit 1
fi

if [ "${RESUME}" = "true" ]; then
    echo "  Settings:        from ${RESUME_CONFIG_PATH}"
    echo "  Steps:           ${STEPS_OVERRIDE:-from train_config.json}"
else
    echo "  Policy:          ${POLICY_TYPE} (${PRETRAINED_PATH})"
    echo "  Dataset:         ${DATASET_REPO_ID}"
    echo "  Dataset root:    ${DATASET_ROOT}"
    echo "  Finetune mode:   ${FINETUNE_MODE}"
    echo "  Steps:           ${STEPS} (batch size ${BATCH_SIZE}, save every ${SAVE_FREQ})"
fi
echo "  Output dir:      ${OUTPUT_DIR}"
echo "  Resume:          ${RESUME}"
echo ""

echo "[3/4] Building the lerobot-train command..."

if [ "${RESUME}" = "true" ]; then
    TRAIN_CMD=(
        lerobot-train
        --resume=true
        "--config_path=${RESUME_CONFIG_PATH}"
    )
    if [ -n "${STEPS_OVERRIDE}" ]; then
        TRAIN_CMD+=("--steps=${STEPS_OVERRIDE}")
    fi
else
    TRAIN_CMD=(
        lerobot-train
        "--dataset.repo_id=${DATASET_REPO_ID}"
        "--dataset.root=${DATASET_ROOT}"
        "--policy.type=${POLICY_TYPE}"
        "--policy.pretrained_path=${PRETRAINED_PATH}"
        --policy.push_to_hub=false
        --policy.device=cuda
        --policy.dtype=bfloat16
        --policy.gradient_checkpointing=true
        "--policy.compile_model=${COMPILE_MODEL}"
        "--output_dir=${OUTPUT_DIR}"
        "--job_name=${JOB_NAME}"
        "--steps=${STEPS}"
        "--batch_size=${BATCH_SIZE}"
        "--save_freq=${SAVE_FREQ}"
        "--log_freq=${LOG_FREQ}"
        "--num_workers=${NUM_WORKERS}"
        --wandb.enable=false
    )
    case "${FINETUNE_MODE}" in
        lora)
            TRAIN_CMD+=(
                --peft.method_type=LORA
                "--peft.r=${LORA_R}"
                "--peft.lora_alpha=${LORA_ALPHA}"
            )
            ;;
        expert_only)
            TRAIN_CMD+=(--policy.train_expert_only=true)
            ;;
        full)
            ;;
    esac
fi

if [ -n "${EXTRA_ARGS}" ]; then
    read -r -a EXTRA_ARGS_ARRAY <<< "${EXTRA_ARGS}"
    TRAIN_CMD+=("${EXTRA_ARGS_ARRAY[@]}")
fi

echo "  Command:"
printf '    %s\n' "${TRAIN_CMD[@]}"
echo ""

mkdir -p "${LEROBOT_PI_OUTPUT_DIR}" \
    "${LEROBOT_PI_CACHE_DIR}/home" \
    "${LEROBOT_PI_CACHE_DIR}/triton" \
    "${LEROBOT_PI_CACHE_DIR}/torchinductor" \
    "${LEROBOT_PI_HF_HOME}" \
    "${LEROBOT_PI_HF_LEROBOT_HOME}"

# These variables reach the container through srun's --export=ALL,
# so HF_TOKEN never appears in a command-line string. HOME is overridden
# only for the container (via --export) so the batch shell keeps its own HOME.
CONTAINER_HOME="${LEROBOT_PI_CACHE_DIR}/home"
export HF_HOME="${LEROBOT_PI_HF_HOME}"
export HF_LEROBOT_HOME="${LEROBOT_PI_HF_LEROBOT_HOME}"
export TRITON_CACHE_DIR="${LEROBOT_PI_CACHE_DIR}/triton"
export TORCHINDUCTOR_CACHE_DIR="${LEROBOT_PI_CACHE_DIR}/torchinductor"
export HF_TOKEN

echo "[4/4] Running training..."

EXIT_CODE=0
# The container script is single-quoted. The minimum driver version ($1) and the
# training command ("$@" after shift) are passed as positional arguments so that
# no value is re-parsed by the shell.
# shellcheck disable=SC2016 # Expansion must happen inside the container, not in this shell.
srun --container-image="${CONTAINER_IMAGE}" \
     --container-mounts="/fsx:/fsx" \
     --container-workdir="${LEROBOT_PI_BASE_DIR}" \
     --export="ALL,HOME=${CONTAINER_HOME}" \
     bash -c '
         set -e
         MIN_DRIVER_VERSION="$1"
         shift
         echo "Container environment:"
         echo "  torch / CUDA available / device:"
         python -c "import torch; print(torch.__version__, torch.cuda.is_available(), torch.cuda.get_device_name(0))"
         nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv
         DRIVER_VERSION="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -n1)"
         LOWEST_VERSION="$(printf "%s\n%s\n" "${DRIVER_VERSION}" "${MIN_DRIVER_VERSION}" | sort -V | head -n1)"
         if [ "${LOWEST_VERSION}" != "${MIN_DRIVER_VERSION}" ]; then
             echo "WARNING: NVIDIA driver ${DRIVER_VERSION} is older than ${MIN_DRIVER_VERSION} required by CUDA 12.8"
         fi
         echo ""
         exec "$@"
     ' container_train "${MIN_DRIVER_VERSION}" "${TRAIN_CMD[@]}" || EXIT_CODE=$?

CHECKPOINT_DIR="${OUTPUT_DIR}/checkpoints/last/pretrained_model"

echo ""
echo "=================================================="
if [ ${EXIT_CODE} -eq 0 ]; then
    echo "Training completed successfully"
    if [ -d "${CHECKPOINT_DIR}" ]; then
        echo "Latest checkpoint: ${CHECKPOINT_DIR}"
        ls -lh "${CHECKPOINT_DIR}"
        echo ""
        echo "To resume this run:"
        echo "  RESUME=true sbatch ./slurm_train.sh ${JOB_NAME}"
    else
        echo "WARNING: Checkpoint directory not found: ${CHECKPOINT_DIR}"
    fi
else
    echo "Training failed with exit code: ${EXIT_CODE}"
fi
echo "End Time: $(date)"
echo "=================================================="

exit ${EXIT_CODE}
