#!/bin/bash
# /app/eval.sh — Container protocol entrypoint for GR00T N1.6 evaluation in LeIsaac.
# Usage: /app/eval.sh <checkpoint_dir> <model_config_dir> <output_dir> <eval_rounds>
# Reads sim.environment from $RUN_CONFIG for task selection.
set -eo pipefail
SECONDS=0

CHECKPOINT_DIR="$1"
MODEL_CONFIG_DIR="$2"
OUTPUT_DIR="$3"
EVAL_ROUNDS="${4:-20}"
LEISAAC_PYTHON="$LEISAAC_DIR/.venv/bin/python"
GR00T_PYTHON="$GR00T_DIR/.venv/bin/python"
PORT=$($LEISAAC_PYTHON -c "import socket; s=socket.socket(); s.bind(('',0)); print(s.getsockname()[1]); s.close()")

if [[ -z "$CHECKPOINT_DIR" || -z "$MODEL_CONFIG_DIR" || -z "$OUTPUT_DIR" ]]; then
  echo "Usage: /app/eval.sh <checkpoint_dir> <model_config_dir> <output_dir> <eval_rounds>"
  exit 1
fi

MODALITY_CONFIG="$MODEL_CONFIG_DIR/modality_config.py"
if [[ ! -f "$MODALITY_CONFIG" ]]; then
  echo "ERROR: modality_config.py not found in $MODEL_CONFIG_DIR"
  exit 1
fi

# Read task from RUN_CONFIG
if [[ -z "$RUN_CONFIG" || ! -f "$RUN_CONFIG" ]]; then
  echo "ERROR: RUN_CONFIG not set or file not found"
  exit 1
fi

TASK=$($LEISAAC_PYTHON -c "import yaml; print(yaml.safe_load(open('$RUN_CONFIG'))['sim']['environment'])")
LANGUAGE_INSTRUCTION=$($LEISAAC_PYTHON -c "import yaml; print(yaml.safe_load(open('$RUN_CONFIG'))['sim']['language_instruction'])")
echo "Task: $TASK"
echo "Language instruction: $LANGUAGE_INSTRUCTION"

mkdir -p "$OUTPUT_DIR"

# Cleanup server on exit
cleanup() { kill $SERVER_PID 2>/dev/null; wait $SERVER_PID 2>/dev/null; }
trap cleanup EXIT

# PyTorch distributed env (required under Slurm)
export MASTER_ADDR="${MASTER_ADDR:-localhost}"
export MASTER_PORT="${MASTER_PORT:-29500}"
export WORLD_SIZE="${WORLD_SIZE:-1}"
export RANK="${RANK:-0}"
export LOCAL_RANK="${LOCAL_RANK:-0}"

# ── Start GR00T policy server in background ──
T_SERVER_START=$SECONDS
echo "Starting GR00T N1.6 policy server on port $PORT..."
$GR00T_PYTHON "$GR00T_DIR/gr00t/eval/run_gr00t_server.py" \
  --model-path "$CHECKPOINT_DIR" \
  --embodiment-tag NEW_EMBODIMENT \
  --modality-config-path "$MODALITY_CONFIG" \
  --port $PORT &
SERVER_PID=$!

# Wait for server to be ready (poll with timeout)
echo "Waiting for policy server to be ready..."
SERVER_READY=false
for i in $(seq 1 60); do
  if ! kill -0 $SERVER_PID 2>/dev/null; then
    echo "ERROR: Policy server died during startup"
    exit 1
  fi
  if $GR00T_PYTHON -c "
from gr00t.policy.server_client import PolicyClient
client = PolicyClient(host='localhost', port=$PORT)
client.ping()
" 2>/dev/null; then
    SERVER_READY=true
    echo "Policy server ready on port $PORT ($(( SECONDS - T_SERVER_START ))s)"
    break
  fi
  sleep 2
done

if [[ "$SERVER_READY" != "true" ]]; then
  echo "ERROR: Policy server failed to start within 120s"
  exit 1
fi

# ── Run evaluation ──
# If $DISPLAY is set, render to display (visual mode via DCV); otherwise headless.
HEADLESS_FLAG=""
if [[ -z "$DISPLAY" ]]; then
  HEADLESS_FLAG="--headless"
fi

echo "[eval.sh] Starting policy_inference.py at $(date)"
T_EVAL_START=$SECONDS
cd "$LEISAAC_DIR"

PYTHONUNBUFFERED=1 $LEISAAC_PYTHON scripts/evaluation/policy_inference.py \
  --task="$TASK" \
  --eval_rounds="$EVAL_ROUNDS" \
  --policy_type=gr00tn1.6 \
  --policy_host=localhost --policy_port=$PORT \
  --policy_timeout_ms=5000 --policy_action_horizon=16 \
  --policy_language_instruction="$LANGUAGE_INSTRUCTION" \
  --device=cuda --enable_cameras $HEADLESS_FLAG 2>&1 | tee "$OUTPUT_DIR/eval.log"
EVAL_EXIT=${PIPESTATUS[0]}
echo "[eval.sh] policy_inference.py exited with code $EVAL_EXIT (eval: $(( SECONDS - T_EVAL_START ))s, total: ${SECONDS}s)"

# ── Parse results and write metrics ──
SUCCESS_RATE=$(grep -oP 'success_rate["\s:]+\K[0-9.]+' "$OUTPUT_DIR/eval.log" || echo "")
if [[ -z "$SUCCESS_RATE" ]]; then
  SUCCESS_COUNT=$(grep -oP '\b(\d+)\s*/\s*\d+' "$OUTPUT_DIR/eval.log" | tail -1 | grep -oP '^\d+')
  TOTAL_COUNT=$(grep -oP '\d+\s*/\s*(\d+)' "$OUTPUT_DIR/eval.log" | tail -1 | grep -oP '\d+$')
  if [[ -n "$SUCCESS_COUNT" && -n "$TOTAL_COUNT" && "$TOTAL_COUNT" -gt 0 ]]; then
    SUCCESS_RATE=$($LEISAAC_PYTHON -c "print($SUCCESS_COUNT / $TOTAL_COUNT)")
  else
    SUCCESS_RATE="0.0"
  fi
fi

cat > "$OUTPUT_DIR/metrics.json" << EOF
{
  "task": "$TASK",
  "eval_rounds": $EVAL_ROUNDS,
  "success_rate": $SUCCESS_RATE,
  "checkpoint": "$CHECKPOINT_DIR"
}
EOF

echo "Metrics written to $OUTPUT_DIR/metrics.json"
echo "Success rate: $SUCCESS_RATE"
echo "Total time: ${SECONDS}s (server: $(( T_EVAL_START - T_SERVER_START ))s, eval: $(( SECONDS - T_EVAL_START ))s)"
