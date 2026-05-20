#!/bin/bash
# /app/convert.sh — Container protocol entrypoint for HDF5 → LeRobot v2.1 conversion.
# Usage: /app/convert.sh <input_hdf5_dir> <output_dataset_dir>
set -e

INPUT_DIR="$1"
OUTPUT_DIR="$2"

if [[ -z "$INPUT_DIR" || -z "$OUTPUT_DIR" ]]; then
  echo "Usage: /app/convert.sh <input_hdf5_dir> <output_dataset_dir>"
  exit 1
fi

if [[ -z "${RUN_CONFIG:-}" ]]; then
  echo "ERROR: RUN_CONFIG env var not set"
  exit 1
fi

# Extract language_instruction from run_config.yaml (used as LeRobot task string).
LANGUAGE_INSTRUCTION=$(python3 -c '
import os, sys, yaml
with open(os.environ["RUN_CONFIG"]) as f:
    cfg = yaml.safe_load(f) or {}
instr = cfg.get("sim", {}).get("language_instruction")
if not instr:
    sys.exit("ERROR: sim.language_instruction missing in run_config")
print(instr)
')

# Atomic lock on the output dir: `mkdir` fails if it already exists, so two
# concurrent conversions targeting the same dataset name can't stomp each other.
if ! mkdir "$OUTPUT_DIR" 2>/dev/null; then
  echo "ERROR: $OUTPUT_DIR already exists (or another job is writing to it)"
  exit 1
fi

# Write to a temp dir alongside the output, then atomically replace on success.
# If the Python step fails, the trap removes both the temp dir AND the empty
# output dir lock so the user can retry.
TMP_DIR="${OUTPUT_DIR}.tmp-$$"
rm -rf "$TMP_DIR"
cleanup_on_fail() {
  local exit_code=$?
  if [[ $exit_code -ne 0 ]]; then
    rm -rf "$TMP_DIR" "$OUTPUT_DIR"
  fi
}
trap cleanup_on_fail EXIT

python3 /app/convert_hdf5_to_lerobot.py \
  --input-dir "$INPUT_DIR" \
  --output-dir "$TMP_DIR" \
  --robot-config /app/robot_configs/so101.yaml \
  --task "$LANGUAGE_INSTRUCTION"

# Replace the empty lock dir with the real output.
rmdir "$OUTPUT_DIR"
mv "$TMP_DIR" "$OUTPUT_DIR"
echo "Conversion complete: $OUTPUT_DIR"
