#!/bin/bash
#SBATCH --job-name=lerobot_pi_prepare
#SBATCH --nodes=1
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=4
#SBATCH --mem=16G
#SBATCH --time=4:00:00
#SBATCH --output=../logs/slurm_%j.out
#SBATCH --error=../logs/slurm_%j.err

# ================================================
# LeRobot pi Dataset Preparation - Slurm Job Script
# ================================================
# Pre-fetches the base weights, the PaliGemma tokenizer and the dataset into
# the FSx Hugging Face cache, optionally converts a v2.1 dataset to v3.0, and
# recomputes the quantile statistics required by pi0.5.
# This job does NOT require a GPU.
#
# Usage (run from the training/ directory):
#   sbatch slurm_prepare_dataset.sh [DATASET_REPO_ID] [PRETRAINED_PATH]
# Example:
#   sbatch slurm_prepare_dataset.sh lerobot/libero lerobot/pi05_base
#
# Environment Variables:
#   DATASET_REPO_ID (optional): dataset repo id (default: lerobot/libero)
#   PRETRAINED_PATH (optional): base weights repo id (default: lerobot/pi05_base)
#   POLICY_TYPE (optional): pi05 or pi0 (default: pi05)
#   TOKENIZER_REPO_ID (optional): tokenizer repo id (default: google/paligemma-3b-pt-224)
#   CONVERT_V21 (optional): true to convert a v2.1 dataset to v3.0 (default: false)
#   RECOMPUTE_STATS (optional): auto, true or false (default: auto)
#   CONTAINER_IMAGE (optional): path to the .sqsh image
#     (default: ${ENROOT_DATA_PATH}/lerobot-pi-train+${IMAGE_TAG:-latest}.sqsh)
# ================================================

set -e

DATASET_REPO_ID="${1:-${DATASET_REPO_ID:-lerobot/libero}}"
PRETRAINED_PATH="${2:-${PRETRAINED_PATH:-lerobot/pi05_base}}"
POLICY_TYPE="${POLICY_TYPE:-pi05}"
TOKENIZER_REPO_ID="${TOKENIZER_REPO_ID:-google/paligemma-3b-pt-224}"
CONVERT_V21="${CONVERT_V21:-false}"
RECOMPUTE_STATS="${RECOMPUTE_STATS:-auto}"

echo "=================================================="
echo "LeRobot pi Dataset Preparation"
echo "=================================================="
echo "Job ID: ${SLURM_JOB_ID}"
echo "Node: ${SLURM_NODELIST}"
echo "Dataset: ${DATASET_REPO_ID}"
echo "Pretrained: ${PRETRAINED_PATH}"
echo "Policy type: ${POLICY_TYPE}"
echo "Tokenizer: ${TOKENIZER_REPO_ID}"
echo "Convert v2.1: ${CONVERT_V21}"
echo "Recompute stats: ${RECOMPUTE_STATS}"
echo "Start Time: $(date)"
echo "=================================================="

if [ -z "${LEROBOT_PI_BASE_DIR}" ] || [ -z "${LEROBOT_PI_LOG_DIR}" ] || [ -z "${LEROBOT_PI_OUTPUT_DIR}" ] \
    || [ -z "${LEROBOT_PI_CACHE_DIR}" ] || [ -z "${LEROBOT_PI_HF_HOME}" ] || [ -z "${LEROBOT_PI_HF_LEROBOT_HOME}" ]; then
    echo "ERROR: Required environment variables not set"
    echo "Please run setup.sh first to configure the environment:"
    echo "  cd /fsx/ubuntu/sample-physical-ai-scaffolding-kit/samples/lerobot-openpi/training"
    echo "  ./setup.sh --hf-token \"your_hf_token\""
    echo ""
    echo "Or manually set the following in ~/.bashrc:"
    echo "  export LEROBOT_PI_BASE_DIR=<path_to_samples/lerobot-openpi>"
    echo "  export LEROBOT_PI_LOG_DIR=\${LEROBOT_PI_BASE_DIR}/logs"
    echo "  export LEROBOT_PI_OUTPUT_DIR=\${LEROBOT_PI_BASE_DIR}/outputs"
    echo "  export LEROBOT_PI_CACHE_DIR=\${LEROBOT_PI_BASE_DIR}/.cache"
    echo "  export LEROBOT_PI_HF_HOME=\${LEROBOT_PI_CACHE_DIR}/huggingface"
    echo "  export LEROBOT_PI_HF_LEROBOT_HOME=\${LEROBOT_PI_HF_HOME}/lerobot"
    echo "  export HF_TOKEN=<your_huggingface_token>"
    exit 1
fi

case "${POLICY_TYPE}" in
    pi05|pi0) ;;
    *)
        echo "ERROR: POLICY_TYPE must be pi05 or pi0 (got: ${POLICY_TYPE})"
        exit 1
        ;;
esac

case "${CONVERT_V21}" in
    true|false) ;;
    *)
        echo "ERROR: CONVERT_V21 must be true or false (got: ${CONVERT_V21})"
        exit 1
        ;;
esac

case "${RECOMPUTE_STATS}" in
    auto|true|false) ;;
    *)
        echo "ERROR: RECOMPUTE_STATS must be auto, true or false (got: ${RECOMPUTE_STATS})"
        exit 1
        ;;
esac

# The CUDA base image bakes in NVIDIA_VISIBLE_DEVICES=all, which makes the
# enroot NVIDIA hook fail when no GPU is allocated. This job needs no GPU.
export NVIDIA_VISIBLE_DEVICES=void

mkdir -p "${LEROBOT_PI_LOG_DIR}"
LOG_FILE="${LEROBOT_PI_LOG_DIR}/prepare_${SLURM_JOB_ID}.log"
ERR_FILE="${LEROBOT_PI_LOG_DIR}/prepare_${SLURM_JOB_ID}.err"

exec 1> >(tee -a "${LOG_FILE}")
exec 2> >(tee -a "${ERR_FILE}" >&2)

echo "Logs will be written to:"
echo "  Output: ${LOG_FILE}"
echo "  Error: ${ERR_FILE}"
echo ""

if [ -z "${HF_TOKEN}" ]; then
    echo "ERROR: HF_TOKEN is not set"
    echo "Please run setup.sh with --hf-token on the login node before submitting the job:"
    echo "  cd /fsx/ubuntu/sample-physical-ai-scaffolding-kit/samples/lerobot-openpi/training"
    echo "  ./setup.sh --hf-token \"your_hf_token\""
    exit 1
fi

if [ -z "${CONTAINER_IMAGE}" ]; then
    if [ -z "${ENROOT_DATA_PATH}" ]; then
        echo "ERROR: ENROOT_DATA_PATH not set"
        echo "Please export ENROOT_DATA_PATH=/fsx/enroot/data in ~/.bashrc"
        exit 1
    fi
    CONTAINER_IMAGE="${ENROOT_DATA_PATH}/lerobot-pi-train+${IMAGE_TAG:-latest}.sqsh"
fi

if [ ! -f "${CONTAINER_IMAGE}" ]; then
    echo "ERROR: Container not found: ${CONTAINER_IMAGE}"
    echo "Please run hyperpod_import_container.sh first"
    echo ""
    echo "Available containers:"
    ls -lh "$(dirname "${CONTAINER_IMAGE}")"
    exit 1
fi

if [ ! -d "${LEROBOT_PI_BASE_DIR}" ]; then
    echo "ERROR: Base directory not found: ${LEROBOT_PI_BASE_DIR}"
    exit 1
fi

DATASET_ROOT="${LEROBOT_PI_HF_LEROBOT_HOME}/${DATASET_REPO_ID}"

# Hugging Face caches live on FSx and are set only for this job, so other
# samples sharing ~/.bashrc are not affected. srun propagates the exported
# variables (default --export=ALL); HF_TOKEN is never expanded into argv.
export HF_HOME="${LEROBOT_PI_HF_HOME}"
export HF_LEROBOT_HOME="${LEROBOT_PI_HF_LEROBOT_HOME}"
export HF_TOKEN
export DATASET_REPO_ID PRETRAINED_PATH POLICY_TYPE TOKENIZER_REPO_ID CONVERT_V21 RECOMPUTE_STATS DATASET_ROOT
CONTAINER_HOME="${LEROBOT_PI_CACHE_DIR}/home"

echo ""
echo "Environment:"
echo "  LEROBOT_PI_BASE_DIR: ${LEROBOT_PI_BASE_DIR}"
echo "  HF_HOME (container): ${HF_HOME}"
echo "  HF_LEROBOT_HOME (container): ${HF_LEROBOT_HOME}"
echo "  HOME (container): ${CONTAINER_HOME}"
echo "  Dataset root: ${DATASET_ROOT}"
echo "  Container: ${CONTAINER_IMAGE}"
echo ""

read -r -d '' CONTAINER_SCRIPT <<'EOF' || true
set -euo pipefail

mkdir -p "${HOME}" "${HF_HOME}" "${HF_LEROBOT_HOME}"
HF_OUTPUT_FILE="$(mktemp -p "${HOME}" hf_download.XXXXXX)"
trap 'rm -f "${HF_OUTPUT_FILE}"' EXIT

run_hf_download() {
    hf download "$@" 2>&1 | tee "${HF_OUTPUT_FILE}"
}

is_rate_limited() {
    grep -qiE '(^|[^0-9])429([^0-9]|$)|too many requests' "${HF_OUTPUT_FILE}"
}

is_access_denied() {
    grep -qiE '(^|[^0-9])40[13]([^0-9]|$)|gated|unauthorized|forbidden' "${HF_OUTPUT_FILE}"
}

report_hf_failure() {
    local repo_url="$1"
    if is_rate_limited; then
        echo "ERROR: Hugging Face rate limit reached (HTTP 429)"
        echo "Wait at least 5 minutes and resubmit this job; the download resumes from the files already fetched."
    elif is_access_denied; then
        echo "ERROR: Access to ${repo_url} was denied (HTTP 401/403)"
        echo "Check that HF_TOKEN is valid and that its account can access the repository."
    else
        echo "ERROR: hf download failed for ${repo_url}"
    fi
}

read_codebase_version() {
    python -c 'import json, sys; print(json.load(open(sys.argv[1])).get("codebase_version", "unknown"))' \
        "${DATASET_ROOT}/meta/info.json"
}

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

print_dataset_summary() {
    python -c '
import json, sys
from pathlib import Path

dataset_root = Path(sys.argv[1])
info = json.loads((dataset_root / "meta" / "info.json").read_text())
stats_path = dataset_root / "meta" / "stats.json"
stats = json.loads(stats_path.read_text()) if stats_path.is_file() else {}
codebase_version = info.get("codebase_version", "unknown")
total_episodes = info.get("total_episodes", "unknown")
total_frames = info.get("total_frames", "unknown")
print(f"  Dataset path: {dataset_root}")
print(f"  Codebase version: {codebase_version}")
print(f"  Episodes: {total_episodes}")
print(f"  Frames: {total_frames}")
for key in ("observation.state", "action"):
    feature_stats = stats.get(key, {})
    quantile_status = "present" if "q01" in feature_stats and "q99" in feature_stats else "missing"
    print(f"  {key} q01/q99: {quantile_status}")
' "${DATASET_ROOT}"
}

echo "Container environment setup complete"
echo "Working directory: $(pwd)"
echo "Python version: $(python --version)"
echo ""

echo "[1/4] Downloading base weights: ${PRETRAINED_PATH}"
if ! run_hf_download "${PRETRAINED_PATH}"; then
    report_hf_failure "https://huggingface.co/${PRETRAINED_PATH}"
    exit 1
fi
echo ""

echo "[2/4] Downloading tokenizer: ${TOKENIZER_REPO_ID}"
if ! run_hf_download "${TOKENIZER_REPO_ID}" \
        --include "tokenizer*" --include "*.json" --include "*.model"; then
    if ! is_rate_limited && is_access_denied; then
        echo "ERROR: Access to the gated tokenizer ${TOKENIZER_REPO_ID} was denied (HTTP 401/403)"
        echo "HF_TOKEN must belong to a Hugging Face account that has accepted the license at"
        echo "  https://huggingface.co/${TOKENIZER_REPO_ID}"
    else
        report_hf_failure "https://huggingface.co/${TOKENIZER_REPO_ID}"
    fi
    exit 1
fi
echo ""

echo "[3/4] Downloading dataset: ${DATASET_REPO_ID}"
if [ "${CONVERT_V21}" = "true" ] && [ -f "${DATASET_ROOT}/meta/info.json" ] \
        && [ "$(read_codebase_version)" = "v3.0" ]; then
    echo "Dataset at ${DATASET_ROOT} is already v3.0; skipping download and conversion"
else
    if ! run_hf_download --repo-type dataset "${DATASET_REPO_ID}" --local-dir "${DATASET_ROOT}"; then
        report_hf_failure "https://huggingface.co/datasets/${DATASET_REPO_ID}"
        exit 1
    fi

    CODEBASE_VERSION="$(read_codebase_version)"
    echo "Dataset codebase version: ${CODEBASE_VERSION}"
    if [ "${CONVERT_V21}" = "true" ] && [ "${CODEBASE_VERSION}" = "v3.0" ]; then
        echo "Dataset is already v3.0; skipping conversion"
    elif [ "${CONVERT_V21}" = "true" ]; then
        echo "Converting ${DATASET_REPO_ID} from v2.1 to v3.0 in place"
        python -m lerobot.scripts.convert_dataset_v21_to_v30 \
            --repo-id="${DATASET_REPO_ID}" \
            --root="${DATASET_ROOT}" \
            --push-to-hub=false
    elif [ "${CODEBASE_VERSION}" != "v3.0" ]; then
        echo "ERROR: Dataset codebase version is ${CODEBASE_VERSION}, but LeRobot training requires v3.0"
        echo "Resubmit with CONVERT_V21=true to convert the dataset to v3.0"
        exit 1
    fi
fi
echo ""

echo "[4/4] Checking quantile statistics (policy: ${POLICY_TYPE}, mode: ${RECOMPUTE_STATS})"
SHOULD_RECOMPUTE=false
if [ "${RECOMPUTE_STATS}" = "true" ]; then
    SHOULD_RECOMPUTE=true
elif [ "${RECOMPUTE_STATS}" = "auto" ] && [ "${POLICY_TYPE}" = "pi05" ]; then
    if has_quantile_stats; then
        echo "meta/stats.json already has q01/q99 for observation.state and action"
    else
        echo "meta/stats.json is missing q01/q99 for observation.state or action"
        SHOULD_RECOMPUTE=true
    fi
else
    echo "Skipping quantile check"
fi

if [ "${SHOULD_RECOMPUTE}" = "true" ]; then
    echo "Recomputing statistics in place: ${DATASET_ROOT}/meta/stats.json"
    lerobot-edit-dataset \
        --repo_id="${DATASET_REPO_ID}" \
        --root="${DATASET_ROOT}" \
        --new_root="${DATASET_ROOT}" \
        --operation.type=recompute_stats \
        --operation.overwrite=true
    if [ "${POLICY_TYPE}" = "pi05" ] && ! has_quantile_stats; then
        echo "ERROR: q01/q99 are still missing in ${DATASET_ROOT}/meta/stats.json after recomputation"
        exit 1
    fi
fi
echo ""

echo "Dataset summary:"
print_dataset_summary
EOF

echo "Starting dataset preparation..."
EXIT_CODE=0
srun --container-image="${CONTAINER_IMAGE}" \
     --container-mounts="/fsx:/fsx" \
     --container-workdir="${LEROBOT_PI_BASE_DIR}" \
     --export="ALL,HOME=${CONTAINER_HOME}" \
     bash -c "${CONTAINER_SCRIPT}" || EXIT_CODE=$?

echo ""
echo "=================================================="
if [ ${EXIT_CODE} -eq 0 ]; then
    echo "Dataset preparation completed successfully"
    echo "Dataset root: ${DATASET_ROOT}"
    echo ""
    echo "Next step (run from the training/ directory):"
    echo "  sbatch ./slurm_train.sh <JOB_NAME>"
    if [ "${DATASET_REPO_ID}" != "lerobot/libero" ]; then
        echo "  (export DATASET_REPO_ID=${DATASET_REPO_ID} before sbatch so the training job uses this dataset)"
    fi
else
    echo "Dataset preparation failed with exit code: ${EXIT_CODE}"
fi
echo "End Time: $(date)"
echo "=================================================="

exit ${EXIT_CODE}
