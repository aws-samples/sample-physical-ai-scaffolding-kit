#!/bin/bash
# /app/train.sh — Container protocol entrypoint for GR00T N1.6 training.
# Usage: /app/train.sh <dataset_dir> <model_config_dir> <output_dir> <max_steps>
set -e

DATASET_DIR="$1"
MODEL_CONFIG_DIR="$2"
OUTPUT_DIR="$3"
MAX_STEPS="${4:-10000}"

if [[ -z "$DATASET_DIR" || -z "$MODEL_CONFIG_DIR" || -z "$OUTPUT_DIR" ]]; then
  echo "Usage: /app/train.sh <dataset_dir> <model_config_dir> <output_dir> <max_steps>"
  exit 1
fi

MODALITY_CONFIG="$MODEL_CONFIG_DIR/modality_config.py"
if [[ ! -f "$MODALITY_CONFIG" ]]; then
  echo "ERROR: modality_config.py not found in $MODEL_CONFIG_DIR"
  exit 1
fi

# Copy modality.json into dataset meta/ if not already there
if [[ -f "$MODEL_CONFIG_DIR/modality.json" && ! -f "$DATASET_DIR/meta/modality.json" ]]; then
  cp "$MODEL_CONFIG_DIR/modality.json" "$DATASET_DIR/meta/modality.json"
fi

# PyTorch distributed env (required even for single-GPU)
export MASTER_ADDR="${MASTER_ADDR:-localhost}"
export MASTER_PORT="${MASTER_PORT:-29500}"
export WORLD_SIZE="${WORLD_SIZE:-1}"
export RANK="${RANK:-0}"
export LOCAL_RANK="${LOCAL_RANK:-0}"

echo "Starting GR00T N1.6 training: max_steps=$MAX_STEPS"
/workspace/gr00t/.venv/bin/python gr00t/experiment/launch_finetune.py \
  --base-model-path nvidia/GR00T-N1.6-3B \
  --dataset-path "$DATASET_DIR" \
  --embodiment-tag NEW_EMBODIMENT \
  --modality-config-path "$MODALITY_CONFIG" \
  --num-gpus 1 \
  --output-dir "$OUTPUT_DIR" \
  --max-steps "$MAX_STEPS" \
  --no-use-wandb \
  --global-batch-size 12 \
  --save-steps 5000 \
  --save-total-limit 3 \
  --dataloader-num-workers 4
