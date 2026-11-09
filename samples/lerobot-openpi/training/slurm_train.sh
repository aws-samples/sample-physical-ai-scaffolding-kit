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
# When MLFLOW_TRACKING_URI is set (setup.sh writes it to ~/.bashrc), training runs
# through training/mlflow_train.py and is recorded to the SageMaker MLflow App.
#
# The #SBATCH values above target ml.g6.2xlarge (1x NVIDIA L4 24 GB, 8 vCPU, 32 GiB).
# Override them on the sbatch command line for other instance types, e.g.:
#   sbatch --cpus-per-task=16 --mem=60G slurm_train.sh my_job
# --output and --error can be overridden the same way, e.g.:
#   sbatch --output=/path/to/slurm_%j.out --error=/path/to/slurm_%j.err slurm_train.sh my_job
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
#   MLFLOW_TRACKING_URI  MLflow App ARN. Unset disables MLflow tracking (default: from ~/.bashrc)
#   MLFLOW_EXPERIMENT_NAME  MLflow experiment name for a new run (default: lerobot-pi).
#                     A resumed MLflow run keeps the experiment in its train_config.json
#   MLFLOW_DISABLE_ARTIFACT  Skip uploading the final LoRA adapter: true / false (default: false)
#   MLFLOW_ADAPTER_MAX_MB  Maximum adapter size in MB to upload as an artifact (default: 200)
#   MLFLOW_SYSTEM_METRICS_SAMPLING_INTERVAL  System metrics interval in seconds (default: 10)
#
# Environment Variables set by the VAMS poller (leave unset for manual runs):
#   VAMS_RUN_DIR      Run directory of the VAMS execution. When set, this script writes
#                     the marker files below to it, and for POLICY_TYPE=pi05 recomputes the
#                     q01/q99 statistics of the dataset before training when they are missing.
#                     The recomputation is skipped when VAMS_VIEW_MANIFEST_FILE is set
#   VAMS_CONTEXT_FILE Path to vams_context.json, passed to mlflow_train.py to record the
#                     VAMS provenance. Requires MLFLOW_TRACKING_URI; the job fails without it
#   VAMS_VIEW_MANIFEST_FILE  Path to curation_manifest.json of a VAMS curation view. When
#                     set, the normalization statistics of the kept episodes are computed
#                     into ${DATASET_ROOT}/meta/stats.json before training, the pi05 quantile
#                     recomputation is skipped, and the variable is passed to the
#                     container. Requires VAMS_CONTEXT_FILE, from which mlflow_train.py
#                     selects the kept episodes
#
# Marker files written to ${VAMS_RUN_DIR} (JSON, written to a temporary file and renamed):
#   job_started.json  {"slurmJobId": "<id>", "startedAt": "<UTC ISO 8601>", "hostname": "<host>"}
#                     written before the environment check
#   job_exit.json     {"slurmJobId": "<id>", "exitCode": <int>, "finishedAt": "<UTC ISO 8601>"}
#                     written by an EXIT trap, also when the script stops on an error
#
# The learning rate is not passed; the LeRobot policy preset is used.
# ================================================

set -e

utc_now() {
    date -u +%Y-%m-%dT%H:%M:%SZ
}

# Writes $2 to $1 through a temporary file in the same directory, so that a
# reader never sees a partially written file. Every step is checked explicitly
# because errexit is ignored when this runs inside an if condition (the EXIT trap).
write_file_atomically() {
    local target_path="$1"
    local content="$2"
    local temp_path
    temp_path="$(mktemp "${target_path}.tmp.XXXXXX")" || return 1
    printf '%s\n' "${content}" > "${temp_path}" || { rm -f "${temp_path}"; return 1; }
    mv -f "${temp_path}" "${target_path}" || { rm -f "${temp_path}"; return 1; }
}

write_job_started_marker() {
    local marker_json
    marker_json="$(python3 -c '
import json, sys
print(json.dumps({"slurmJobId": sys.argv[1], "startedAt": sys.argv[2], "hostname": sys.argv[3]}))
' "${SLURM_JOB_ID}" "$(utc_now)" "$(hostname)")" || return 1
    write_file_atomically "${VAMS_RUN_DIR}/job_started.json" "${marker_json}"
}

# shellcheck disable=SC2329 # Invoked from the EXIT trap handler.
write_job_exit_marker() {
    local exit_code="$1"
    local marker_json
    marker_json="$(python3 -c '
import json, sys
print(json.dumps({"slurmJobId": sys.argv[1], "exitCode": int(sys.argv[2]), "finishedAt": sys.argv[3]}))
' "${SLURM_JOB_ID}" "${exit_code}" "$(utc_now)")" || return 1
    write_file_atomically "${VAMS_RUN_DIR}/job_exit.json" "${marker_json}"
}

# EXIT trap: records the exit code of the script for the VAMS poller and keeps it
# as the exit code of the job.
# shellcheck disable=SC2329 # Invoked by the EXIT trap.
on_vams_job_exit() {
    local exit_code=$?
    if ! write_job_exit_marker "${exit_code}"; then
        echo "ERROR: Failed to write ${VAMS_RUN_DIR}/job_exit.json" >&2
    fi
    exit "${exit_code}"
}

if [ -n "${VAMS_RUN_DIR:-}" ]; then
    if [ ! -d "${VAMS_RUN_DIR}" ]; then
        echo "ERROR: VAMS_RUN_DIR is not a directory: ${VAMS_RUN_DIR}"
        exit 1
    fi
    trap on_vams_job_exit EXIT
    write_job_started_marker
fi

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
MLFLOW_TRACKING_URI="${MLFLOW_TRACKING_URI:-}"
MLFLOW_EXPERIMENT_NAME="${MLFLOW_EXPERIMENT_NAME:-lerobot-pi}"
MLFLOW_DISABLE_ARTIFACT="${MLFLOW_DISABLE_ARTIFACT:-false}"
MLFLOW_ADAPTER_MAX_MB="${MLFLOW_ADAPTER_MAX_MB:-200}"
MLFLOW_SYSTEM_METRICS_SAMPLING_INTERVAL="${MLFLOW_SYSTEM_METRICS_SAMPLING_INTERVAL:-10}"

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

# A VAMS run must be recorded to MLflow with its provenance, so it never falls
# back to an untracked run.
if [ -n "${VAMS_CONTEXT_FILE:-}" ]; then
    if [ -z "${MLFLOW_TRACKING_URI}" ]; then
        echo "ERROR: VAMS_CONTEXT_FILE is set but MLFLOW_TRACKING_URI is not set"
        echo "A VAMS run requires MLflow tracking. Run setup.sh without --no-mlflow and submit again"
        exit 1
    fi
    if [ ! -f "${VAMS_CONTEXT_FILE}" ]; then
        echo "ERROR: VAMS context file not found: ${VAMS_CONTEXT_FILE}"
        exit 1
    fi
fi

# A view selects its kept episodes through mlflow_train.py, which reads them from
# the view recorded in vams_context.json.
if [ -n "${VAMS_VIEW_MANIFEST_FILE:-}" ]; then
    if [ -z "${VAMS_CONTEXT_FILE:-}" ]; then
        echo "ERROR: VAMS_VIEW_MANIFEST_FILE is set but VAMS_CONTEXT_FILE is not set"
        echo "A VAMS curation view selects its episodes through the VAMS context of the run"
        exit 1
    fi
    if [ ! -f "${VAMS_VIEW_MANIFEST_FILE}" ]; then
        echo "ERROR: VAMS view manifest file not found: ${VAMS_VIEW_MANIFEST_FILE}"
        exit 1
    fi
    if [ ! -d "${DATASET_ROOT}" ]; then
        echo "ERROR: Dataset root of the VAMS view is not a directory: ${DATASET_ROOT}"
        exit 1
    fi
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
if [ -n "${MLFLOW_TRACKING_URI}" ]; then
    echo "  MLflow:          enabled (${MLFLOW_TRACKING_URI})"
    if [ "${RESUME}" = "true" ]; then
        echo "  MLflow experiment: from train_config.json, or ${MLFLOW_EXPERIMENT_NAME} if the run had no MLflow tracking"
    else
        echo "  MLflow experiment: ${MLFLOW_EXPERIMENT_NAME}"
    fi
else
    echo "  MLflow:          disabled (MLFLOW_TRACKING_URI is not set)"
fi
if [ -n "${VAMS_RUN_DIR:-}" ]; then
    echo "  VAMS run dir:    ${VAMS_RUN_DIR}"
    echo "  VAMS context:    ${VAMS_CONTEXT_FILE:-(not set)}"
    echo "  VAMS view:       ${VAMS_VIEW_MANIFEST_FILE:-(not set)}"
fi
echo ""

echo "[3/4] Building the lerobot-train command..."

# mlflow_train.py replaces the W&B logger of lerobot-train with an MLflow one, so
# the --wandb.* options select the MLflow experiment and the artifact upload.
if [ -n "${MLFLOW_TRACKING_URI}" ]; then
    TRAIN_ENTRYPOINT=(python training/mlflow_train.py)
    TRACKING_ARGS=(
        --wandb.enable=true
        "--wandb.project=${MLFLOW_EXPERIMENT_NAME}"
        "--wandb.disable_artifact=${MLFLOW_DISABLE_ARTIFACT}"
    )
else
    TRAIN_ENTRYPOINT=(lerobot-train)
    TRACKING_ARGS=(--wandb.enable=false)
fi

if [ "${RESUME}" = "true" ]; then
    TRAIN_CMD=(
        "${TRAIN_ENTRYPOINT[@]}"
        --resume=true
        "--config_path=${RESUME_CONFIG_PATH}"
    )
    if [ -n "${STEPS_OVERRIDE}" ]; then
        TRAIN_CMD+=("--steps=${STEPS_OVERRIDE}")
    fi
    # A run that already records to MLflow keeps wandb.project and wandb.run_id from
    # train_config.json so that it continues in the same experiment and run.
    # A run started without MLflow is recorded as a new MLflow run.
    if [ -n "${MLFLOW_TRACKING_URI}" ]; then
        RESUMED_WANDB_ENABLE="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["wandb"]["enable"])' "${RESUME_CONFIG_PATH}")"
        if [ "${RESUMED_WANDB_ENABLE}" != "True" ]; then
            TRAIN_CMD+=("${TRACKING_ARGS[@]}")
        fi
    else
        TRAIN_CMD+=("${TRACKING_ARGS[@]}")
    fi
else
    TRAIN_CMD=(
        "${TRAIN_ENTRYPOINT[@]}"
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
        "${TRACKING_ARGS[@]}"
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
if [ -n "${MLFLOW_TRACKING_URI}" ]; then
    export MLFLOW_TRACKING_URI
    export AWS_DEFAULT_REGION
    export MLFLOW_SYSTEM_METRICS_SAMPLING_INTERVAL
    export MLFLOW_ADAPTER_MAX_MB
    export IMAGE_TAG
    export PASK_CLUSTER_NAME
    if [ -n "${VAMS_CONTEXT_FILE:-}" ]; then
        export VAMS_CONTEXT_FILE
    fi
fi
# A view is never fetched from the Hugging Face Hub even without HF_HUB_OFFLINE: its
# dataset repo_id vams/<databaseId>/<assetId> has three segments, so huggingface_hub
# rejects it with HFValidationError before any network request. HF_HUB_OFFLINE is not
# set so that the pretrained weights and tokenizer can still be downloaded.
if [ -n "${VAMS_VIEW_MANIFEST_FILE:-}" ]; then
    export VAMS_VIEW_MANIFEST_FILE
fi

# The statistics of a view are computed from the rows of the kept episodes only.
# lerobot-edit-dataset recompute_stats is not used for a view because it also reads
# the rows of the excluded episodes that share a data file with the kept ones.
if [ -n "${VAMS_VIEW_MANIFEST_FILE:-}" ]; then
    echo "Computing normalization statistics of the kept episodes (VAMS view)..."
    VIEW_STATS_EXIT_CODE=0
    srun --container-image="${CONTAINER_IMAGE}" \
         --container-mounts="/fsx:/fsx" \
         --container-workdir="${LEROBOT_PI_BASE_DIR}" \
         --export="ALL,HOME=${CONTAINER_HOME}" \
         python training/compute_view_stats.py \
         --root "${DATASET_ROOT}" \
         --manifest "${VAMS_VIEW_MANIFEST_FILE}" \
         || VIEW_STATS_EXIT_CODE=$?
    if [ ${VIEW_STATS_EXIT_CODE} -ne 0 ]; then
        echo "ERROR: Computing the statistics of the VAMS view failed with exit code: ${VIEW_STATS_EXIT_CODE}"
        exit ${VIEW_STATS_EXIT_CODE}
    fi
    echo ""
fi

# pi0.5 normalizes observation.state and action with the q01/q99 quantiles. A VAMS
# dataset is not prepared by slurm_prepare_dataset.sh, so the same check and
# recomputation run here in the training container before training.
# DATASET_ROOT of a VAMS run is the staged copy under /fsx/s3link/vams/<id>/. The
# recomputed meta/stats.json is exported only to vams/<id>/ in the PASK data bucket
# by the FSx automatic export; the dataset in the VAMS asset bucket is not changed.
# A view has its statistics computed above, so it is skipped here.
if [ -n "${VAMS_RUN_DIR:-}" ] && [ -z "${VAMS_VIEW_MANIFEST_FILE:-}" ] && [ "${POLICY_TYPE}" = "pi05" ]; then
    echo "Checking quantile statistics (VAMS run, policy: ${POLICY_TYPE})..."

    read -r -d '' QUANTILE_STATS_SCRIPT <<'EOF' || true
set -euo pipefail
DATASET_REPO_ID="$1"
DATASET_ROOT="$2"

has_quantile_stats() {
    python -c '
import json, sys
from pathlib import Path

stats_path = Path(sys.argv[1])
if not stats_path.is_file():
    sys.exit(1)
stats = json.loads(stats_path.read_text())
required_keys = ("observation.state", "action")
has_quantiles = all("q01" in stats.get(key, {}) and "q99" in stats.get(key, {}) for key in required_keys)
sys.exit(0 if has_quantiles else 1)
' "${DATASET_ROOT}/meta/stats.json"
}

if has_quantile_stats; then
    echo "  meta/stats.json already has q01/q99 for observation.state and action"
    exit 0
fi

echo "  meta/stats.json is missing q01/q99 for observation.state or action"
echo "  Recomputing statistics in place: ${DATASET_ROOT}/meta/stats.json"
lerobot-edit-dataset \
    --repo_id="${DATASET_REPO_ID}" \
    --root="${DATASET_ROOT}" \
    --new_root="${DATASET_ROOT}" \
    --operation.type=recompute_stats \
    --operation.overwrite=true
if ! has_quantile_stats; then
    echo "ERROR: q01/q99 are still missing in ${DATASET_ROOT}/meta/stats.json after recomputation"
    exit 1
fi
EOF

    QUANTILE_STATS_EXIT_CODE=0
    srun --container-image="${CONTAINER_IMAGE}" \
         --container-mounts="/fsx:/fsx" \
         --container-workdir="${LEROBOT_PI_BASE_DIR}" \
         --export="ALL,HOME=${CONTAINER_HOME}" \
         bash -c "${QUANTILE_STATS_SCRIPT}" container_quantile_stats "${DATASET_REPO_ID}" "${DATASET_ROOT}" \
         || QUANTILE_STATS_EXIT_CODE=$?
    if [ ${QUANTILE_STATS_EXIT_CODE} -ne 0 ]; then
        echo "ERROR: Quantile statistics check failed with exit code: ${QUANTILE_STATS_EXIT_CODE}"
        exit ${QUANTILE_STATS_EXIT_CODE}
    fi
    echo ""
fi

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
