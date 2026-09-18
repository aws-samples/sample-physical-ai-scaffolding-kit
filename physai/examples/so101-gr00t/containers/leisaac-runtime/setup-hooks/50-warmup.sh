#!/bin/bash
# Warm up IsaacSim shader caches.
# Equivalent to https://github.com/isaac-sim/IsaacSim/blob/main/source/scripts/warmup.sh
# adapted for pip-installed IsaacSim.
set -eo pipefail

LEISAAC_PYTHON="${LEISAAC_DIR:?}/.venv/bin/python"
ISAACSIM_DIR=$($LEISAAC_PYTHON -c "import isaacsim, os; print(os.path.dirname(isaacsim.__file__))")
TASKING_THREAD_CNT=$(( $(nproc --all) / 2 ))

echo "IsaacSim dir: $ISAACSIM_DIR"

# Step 1: Python app shader warmup.
# IsaacSim 6.0 on driver 595 deadlocks the carb.tasking plugin ("carb.tasking is
# likely stuck. All threads are busy"): the SimulationApp process ends up in
# futex_wait_queue with 55 idle threads and never exits. Cap the whole warmup
# with a timeout so a hang can't block the container build indefinitely.
# The shader cache populated up to the timeout is still baked into the image;
# any missing shaders will just compile on first eval run.
WARMUP_TIMEOUT=${WARMUP_TIMEOUT:-15m}
echo "Warming up Python app shader cache (timeout=$WARMUP_TIMEOUT)..."
set +e
timeout --preserve-status "$WARMUP_TIMEOUT" \
  env PYTHONUNBUFFERED=1 $LEISAAC_PYTHON -c "
from isaacsim import SimulationApp
kit = SimulationApp()
for i in range(100):
    kit.update()
kit.close()
"
warmup_rc=$?
set -e
case $warmup_rc in
    0)   echo "Python app shader cache warmed up." ;;
    124) echo "Python app warmup timed out after $WARMUP_TIMEOUT (carb.tasking hang; partial cache retained)" ;;
    *)   echo "Python app warmup exited with rc=$warmup_rc (partial cache retained; ignoring shutdown-time assertion)" ;;
esac

# Step 2: Kit shader warmup
echo "Warming up Kit shader cache (timeout=$WARMUP_TIMEOUT)..."
set +e
timeout --preserve-status "$WARMUP_TIMEOUT" \
  $LEISAAC_PYTHON "$ISAACSIM_DIR/kit/kit_app.py" "$ISAACSIM_DIR/apps/isaacsim.exp.base.kit" \
    --no-window \
    --/persistent/renderer/startupMessageDisplayed=true \
    --ext-folder "$ISAACSIM_DIR/exts" \
    --ext-folder "$ISAACSIM_DIR/apps" \
    --/app/settings/persistent=0 \
    --/app/settings/loadUserConfig=0 \
    --/structuredLog/enable=0 \
    --/app/hangDetector/enabled=0 \
    --/crashreporter/skipOldDumpUpload=1 \
    --/app/content/emptyStageOnStart=1 \
    --/rtx/materialDb/syncLoads=1 \
    --/omni.kit.plugin/syncUsdLoads=1 \
    --/rtx/hydra/materialSyncLoads=1 \
    --/app/asyncRendering=0 \
    --/app/quitAfter=1000 \
    --/app/fastShutdown=1 \
    --/app/file/ignoreUnsavedOnExit=1 \
    --/app/warmupMode=1 \
    --/exts/omni.kit.registry.nucleus/registries/0/name=0 \
    --/plugins/carb.tasking.plugin/threadCount=$TASKING_THREAD_CNT
kit_rc=$?
set -e
case $kit_rc in
    0)   echo "Kit shader cache warmed up." ;;
    124) echo "Kit shader warmup timed out after $WARMUP_TIMEOUT (partial cache retained)" ;;
    *)   echo "Kit shader warmup exited with rc=$kit_rc (partial cache retained)" ;;
esac
