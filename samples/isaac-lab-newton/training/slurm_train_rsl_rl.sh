#!/bin/bash
#SBATCH --job-name=isaac_newton_rl
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=8
#SBATCH --gres=gpu:1
#SBATCH --mem=48G
#SBATCH --time=02:00:00
#SBATCH --output=/fsx/ubuntu/isaac-lab-newton/logs/train_%j.out
#SBATCH --error=/fsx/ubuntu/isaac-lab-newton/logs/train_%j.err

# Runs RSL-RL training in Isaac Lab with the Newton physics backend.
#
# Smoke test:
#   mkdir -p /fsx/ubuntu/isaac-lab-newton/logs
#   ACCEPT_EULA=Y PRIVACY_CONSENT=Y NUM_ENVS=128 MAX_ITERATIONS=2 sbatch slurm_train_rsl_rl.sh

set -euo pipefail

if [ "${ACCEPT_EULA:-}" != "Y" ]; then
    echo "ERROR: ACCEPT_EULA=Y is required to run the Isaac Lab container."
    echo "By running this image, you acknowledge the NVIDIA Isaac Lab container license terms."
    exit 1
fi

PRIVACY_CONSENT="${PRIVACY_CONSENT:-Y}"
ISAAC_NEWTON_BASE_DIR="${ISAAC_NEWTON_BASE_DIR:-/fsx/ubuntu/isaac-lab-newton}"
LOG_DIR="${LOG_DIR:-${ISAAC_NEWTON_BASE_DIR}/logs}"
CACHE_DIR="${CACHE_DIR:-${ISAAC_NEWTON_BASE_DIR}/cache}"
DATA_DIR="${DATA_DIR:-${ISAAC_NEWTON_BASE_DIR}/data}"
DOCUMENTS_DIR="${DOCUMENTS_DIR:-${ISAAC_NEWTON_BASE_DIR}/documents}"
OMNI_LOG_DIR="${OMNI_LOG_DIR:-${ISAAC_NEWTON_BASE_DIR}/omni_logs}"
ECR_REPOSITORY="${ECR_REPOSITORY:-isaac-lab-newton}"
IMAGE_TAG="${IMAGE_TAG:-3.0.0-beta1}"
ENROOT_DATA_PATH="${ENROOT_DATA_PATH:-/fsx/enroot/data}"
CONTAINER_IMAGE="${CONTAINER_IMAGE:-${ENROOT_DATA_PATH}/${ECR_REPOSITORY}+${IMAGE_TAG}.sqsh}"
TASK="${TASK:-Isaac-Velocity-Flat-Anymal-D-v0}"
NUM_ENVS="${NUM_ENVS:-4096}"
MAX_ITERATIONS="${MAX_ITERATIONS:-100}"
EXPERIMENT_NAME="${EXPERIMENT_NAME:-anymal_d_newton}"
RUN_NAME="${RUN_NAME:-run_${SLURM_JOB_ID:-manual}}"

mkdir -p \
    "${LOG_DIR}" \
    "${LOG_DIR}/isaaclab" \
    "${CACHE_DIR}/kit" \
    "${CACHE_DIR}/ov" \
    "${CACHE_DIR}/pip" \
    "${CACHE_DIR}/glcache" \
    "${CACHE_DIR}/computecache" \
    "${DATA_DIR}" \
    "${DOCUMENTS_DIR}" \
    "${OMNI_LOG_DIR}"

LOG_FILE="${LOG_DIR}/train_${SLURM_JOB_ID:-manual}.log"
ERR_FILE="${LOG_DIR}/train_${SLURM_JOB_ID:-manual}.err"
exec 1> >(tee -a "${LOG_FILE}")
exec 2> >(tee -a "${ERR_FILE}" >&2)

if [ ! -f "${CONTAINER_IMAGE}" ]; then
    echo "ERROR: Container not found: ${CONTAINER_IMAGE}"
    echo "Run hyperpod_import_container.sh before submitting the training job."
    exit 1
fi

CONTAINER_MOUNTS="/fsx:/fsx"
CONTAINER_MOUNTS+=",${CACHE_DIR}/kit:/isaac-sim/kit/cache"
CONTAINER_MOUNTS+=",${CACHE_DIR}/ov:/root/.cache/ov"
CONTAINER_MOUNTS+=",${CACHE_DIR}/pip:/root/.cache/pip"
CONTAINER_MOUNTS+=",${CACHE_DIR}/glcache:/root/.cache/nvidia/GLCache"
CONTAINER_MOUNTS+=",${CACHE_DIR}/computecache:/root/.nv/ComputeCache"
CONTAINER_MOUNTS+=",${OMNI_LOG_DIR}:/root/.nvidia-omniverse/logs"
CONTAINER_MOUNTS+=",${DATA_DIR}:/root/.local/share/ov/data"
CONTAINER_MOUNTS+=",${DOCUMENTS_DIR}:/root/Documents"
CONTAINER_MOUNTS+=",${LOG_DIR}/isaaclab:/workspace/IsaacLab/logs"

echo "=================================================="
echo "Isaac Lab Newton RSL-RL Training"
echo "=================================================="
echo "Job ID: ${SLURM_JOB_ID}"
echo "Node: ${SLURM_NODELIST}"
echo "Container: ${CONTAINER_IMAGE}"
echo "Task: ${TASK}"
echo "Num envs: ${NUM_ENVS}"
echo "Max iterations: ${MAX_ITERATIONS}"
echo "Experiment: ${EXPERIMENT_NAME}"
echo "Run name: ${RUN_NAME}"
echo "Logs: ${LOG_DIR}"
echo "Start Time: $(date)"
echo "=================================================="

srun --container-image="${CONTAINER_IMAGE}" \
    --container-mounts="${CONTAINER_MOUNTS}" \
    --container-workdir="/workspace/IsaacLab" \
    bash -lc "
        set -euo pipefail
        export ACCEPT_EULA='${ACCEPT_EULA}'
        export PRIVACY_CONSENT='${PRIVACY_CONSENT}'
        export OMNI_KIT_ALLOW_ROOT=1

        ./isaaclab.sh -p scripts/reinforcement_learning/rsl_rl/train.py \
            --task '${TASK}' \
            --num_envs '${NUM_ENVS}' \
            --max_iterations '${MAX_ITERATIONS}' \
            --headless \
            --experiment_name '${EXPERIMENT_NAME}' \
            --run_name '${RUN_NAME}' \
            --logger tensorboard \
            presets=newton
    "

echo "=================================================="
echo "Training completed successfully"
echo "Isaac Lab logs: ${LOG_DIR}/isaaclab"
echo "End Time: $(date)"
echo "=================================================="
