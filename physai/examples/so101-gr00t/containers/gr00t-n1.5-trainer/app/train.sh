#!/bin/bash
# /app/train.sh — Container protocol entrypoint for GR00T N1.5 training.
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

DATA_CONFIG="$MODEL_CONFIG_DIR/data_config.py"
if [[ ! -f "$DATA_CONFIG" ]]; then
  echo "ERROR: data_config.py not found in $MODEL_CONFIG_DIR"
  exit 1
fi

# Copy modality.json into dataset meta/ if needed
if [[ -f "$MODEL_CONFIG_DIR/modality.json" && ! -f "$DATASET_DIR/meta/modality.json" ]]; then
  cp "$MODEL_CONFIG_DIR/modality.json" "$DATASET_DIR/meta/modality.json"
fi

# Derive the data config class name from data_config.py.
# Convention: the module must define exactly one BaseDataConfig subclass.
DATA_CONFIG_CLASS=$($GR00T_DIR/.venv/bin/python -c "
import ast, sys
with open('$DATA_CONFIG') as f:
    tree = ast.parse(f.read())
for node in ast.walk(tree):
    if isinstance(node, ast.ClassDef):
        print(node.name); sys.exit()
sys.exit('ERROR: no class found in data_config.py')
")
echo "Using data config: data_config:${DATA_CONFIG_CLASS}"

echo "Starting GR00T N1.5 training: max_steps=$MAX_STEPS"
TRAIN_WORK_DIR="${OUTPUT_DIR}/.work"
rm -rf "$TRAIN_WORK_DIR"
mkdir -p "$TRAIN_WORK_DIR"

PYTHONPATH="$MODEL_CONFIG_DIR:${PYTHONPATH:-}" \
$GR00T_DIR/.venv/bin/python $GR00T_DIR/scripts/gr00t_finetune.py \
  --dataset-path "$DATASET_DIR" \
  --data-config "data_config:${DATA_CONFIG_CLASS}" \
  --base-model-path nvidia/GR00T-N1.5-3B \
  --embodiment-tag new_embodiment \
  --num-gpus 1 \
  --output-dir "$TRAIN_WORK_DIR" \
  --max-steps "$MAX_STEPS" \
  --report-to tensorboard \
  --batch-size 32 \
  --save-steps "$MAX_STEPS" \
  --dataloader-num-workers 4

# GR00T trainer writes to $TRAIN_WORK_DIR/checkpoint-<step>/.
# Move the final checkpoint to $OUTPUT_DIR per the container protocol.
CKPT_DIR="$TRAIN_WORK_DIR/checkpoint-$MAX_STEPS"
if [[ -d "$CKPT_DIR" ]]; then
  echo "Publishing checkpoint to $OUTPUT_DIR"
  mv "$CKPT_DIR"/* "$OUTPUT_DIR"/
fi
rm -rf "$TRAIN_WORK_DIR"
