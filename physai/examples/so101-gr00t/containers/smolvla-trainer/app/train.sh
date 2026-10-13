#!/bin/bash
# /app/train.sh — Container protocol entrypoint for SmolVLA fine-tuning.
# Usage: /app/train.sh <dataset_dir> <model_config_dir> <output_dir> <max_steps>
set -eo pipefail
SECONDS=0

DATASET_DIR="$1"
MODEL_CONFIG_DIR="$2"
OUTPUT_DIR="$3"
MAX_STEPS="${4:-30000}"

if [[ -z "$DATASET_DIR" || -z "$MODEL_CONFIG_DIR" || -z "$OUTPUT_DIR" ]]; then
  echo "Usage: /app/train.sh <dataset_dir> <model_config_dir> <output_dir> <max_steps>"
  exit 1
fi

if [[ -z "${RUN_CONFIG:-}" || ! -f "$RUN_CONFIG" ]]; then
  echo "ERROR: RUN_CONFIG env var not set or file missing"
  exit 1
fi

PY="$SMOLVLA_HOME/.venv/bin/python"
LEROBOT_TRAIN="$SMOLVLA_HOME/.venv/bin/lerobot-train"

# ── Resolve repo_id from run config; LeRobot loads datasets by repo_id under
#    HF_LEROBOT_HOME, so we build that layout as a symlink over $DATASET_DIR.
REPO_ID=$("$PY" - <<'PY'
import os, yaml, sys
cfg = yaml.safe_load(open(os.environ["RUN_CONFIG"])) or {}
repo_id = (cfg.get("dataset") or {}).get("repo_id")
if not repo_id:
    print("ERROR: dataset.repo_id missing from run config", file=sys.stderr)
    sys.exit(1)
print(repo_id)
PY
)
echo "dataset.repo_id: $REPO_ID"

export HF_LEROBOT_HOME="$OUTPUT_DIR/.lerobot_home"
LINK="$HF_LEROBOT_HOME/$REPO_ID"
rm -rf "$HF_LEROBOT_HOME"
mkdir -p "$(dirname "$LINK")"
ln -s "$DATASET_DIR" "$LINK"

# The rosbag converter emits LeRobotDataset v3.0 directly (matches lerobot 0.6+
# schema). If a user points this trainer at an older v2.1 dataset produced by a
# legacy converter, fail-fast with a clear message rather than silently
# upgrading in place.
CODEBASE_VER=$("$PY" -c "
import json, os
p = os.path.join('$DATASET_DIR', 'meta/info.json')
try:
    print(json.load(open(p)).get('codebase_version', ''))
except Exception:
    print('')
" || echo "")
echo "dataset codebase_version: $CODEBASE_VER"
if [[ "$CODEBASE_VER" == "v2.1" || "$CODEBASE_VER" == "2.1" ]]; then
  echo "ERROR: $DATASET_DIR is LeRobotDataset v2.1; lerobot 0.6 requires v3.0."
  echo "       Re-run convert with the current so101-rosbag-converter container,"
  echo "       or upgrade in place: lerobot-scripts convert_dataset_v21_to_v30 --repo-id=$REPO_ID"
  exit 1
fi

# ── Read per-run overrides (list of --key=value strings) ──
readarray -t EXTRA_ARGS < <("$PY" /app/read_overrides.py "$MODEL_CONFIG_DIR/train_overrides.yaml" || true)

# lerobot-train's config.validate() refuses to overwrite an existing
# output_dir when resume is False, so we just clear it and let lerobot
# create it fresh — DON'T mkdir here.
TRAIN_WORK_DIR="$OUTPUT_DIR/.work"
rm -rf "$TRAIN_WORK_DIR"

echo "Starting SmolVLA fine-tune: max_steps=$MAX_STEPS"
# lerobot 0.6+ ships an entrypoint script (was `python -m lerobot.scripts.train`
# in <=0.5). Use the script directly so the venv's Python is picked up.
"$LEROBOT_TRAIN" \
  --policy.path="$SMOLVLA_HOME/smolvla_base" \
  --policy.push_to_hub=false \
  --dataset.repo_id="$REPO_ID" \
  --batch_size=32 --steps="$MAX_STEPS" \
  --save_freq="$MAX_STEPS" \
  --output_dir="$TRAIN_WORK_DIR" \
  --wandb.enable=false \
  "${EXTRA_ARGS[@]}"

# ── Publish checkpoint per physai contract ──
# lerobot 0.6 writes to <output_dir>/checkpoints/<zero-padded step>/pretrained_model/
# (e.g. .work/checkpoints/000200/pretrained_model/). Pick the highest-numbered
# checkpoint on disk to be robust to save_freq quirks.
CKPT=$(find "$TRAIN_WORK_DIR" -type d -name pretrained_model | sort | tail -n 1)
if [[ -z "$CKPT" || ! -d "$CKPT" ]]; then
  echo "ERROR: no pretrained_model checkpoint produced under $TRAIN_WORK_DIR"
  exit 1
fi
echo "Publishing checkpoint from $CKPT to $OUTPUT_DIR"
mv "$CKPT"/* "$OUTPUT_DIR"/
rm -rf "$TRAIN_WORK_DIR" "$HF_LEROBOT_HOME"

echo "Training complete (${SECONDS}s)"
