#!/bin/bash
# /app/convert.sh — Container protocol entrypoint for rosbag2 → LeRobot v2.1 conversion.
# Usage: /app/convert.sh <input_rosbag_dir> <output_dataset_dir>
set -e

INPUT_DIR="$1"
OUTPUT_DIR="$2"

if [[ -z "$INPUT_DIR" || -z "$OUTPUT_DIR" ]]; then
  echo "Usage: /app/convert.sh <input_rosbag_dir> <output_dataset_dir>"
  exit 1
fi

if [[ -z "${RUN_CONFIG:-}" ]]; then
  echo "ERROR: RUN_CONFIG env var not set"
  exit 1
fi

# Fallback task label. Per-episode metadata.custom_data.task is preferred by
# the converter; this run_config value is used only when metadata omits it.
LANGUAGE_INSTRUCTION=$(python3 -c '
import os, yaml
with open(os.environ["RUN_CONFIG"]) as f:
    cfg = yaml.safe_load(f) or {}
instr = cfg.get("rosbag", {}).get("language_instruction") \
     or cfg.get("sim", {}).get("language_instruction") \
     or ""
print(instr)
')

if ! mkdir "$OUTPUT_DIR" 2>/dev/null; then
  echo "ERROR: $OUTPUT_DIR already exists (or another job is writing to it)"
  exit 1
fi

TMP_DIR="${OUTPUT_DIR}.tmp-$$"
rm -rf "$TMP_DIR"
cleanup_on_fail() {
  local exit_code=$?
  if [[ $exit_code -ne 0 ]]; then
    rm -rf "$TMP_DIR" "$OUTPUT_DIR"
  fi
}
trap cleanup_on_fail EXIT

python3 /app/convert_rosbag_to_lerobot.py \
  --input-dir "$INPUT_DIR" \
  --output-dir "$TMP_DIR" \
  --robot-config /app/robot_configs/so101.yaml \
  ${LANGUAGE_INSTRUCTION:+--task "$LANGUAGE_INSTRUCTION"}

rmdir "$OUTPUT_DIR"
mv "$TMP_DIR" "$OUTPUT_DIR"
echo "Conversion complete: $OUTPUT_DIR"
