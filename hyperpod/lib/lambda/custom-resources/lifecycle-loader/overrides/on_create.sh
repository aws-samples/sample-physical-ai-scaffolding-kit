#!/bin/bash

set -ex

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="/var/log/provision/provisioning.log"
mkdir -p "/var/log/provision"
touch "$LOG_FILE"

logger() {
  echo "$@" | tee -a "$LOG_FILE"
}

PROVISIONING_PARAMETERS_PATH="${SCRIPT_DIR}/provisioning_parameters.json"

# Give systemd-resolved enough time to restart after HostAgent changes.
sleep 30

if [[ -z "${SAGEMAKER_RESOURCE_CONFIG_PATH:-}" ]]; then
  logger "Env var SAGEMAKER_RESOURCE_CONFIG_PATH is unset, trying to read from default location path"
  SAGEMAKER_RESOURCE_CONFIG_PATH="/opt/ml/config/resource_config.json"

  if [[ ! -f "$SAGEMAKER_RESOURCE_CONFIG_PATH" ]]; then
    logger "Env var SAGEMAKER_RESOURCE_CONFIG_PATH is unset and file does not exist: $SAGEMAKER_RESOURCE_CONFIG_PATH"
    logger "Assume vanilla cluster setup, no scripts to run. Exiting."
    exit 0
  fi
else
  logger "env var SAGEMAKER_RESOURCE_CONFIG_PATH is set to: $SAGEMAKER_RESOURCE_CONFIG_PATH"
  if [[ ! -f "$SAGEMAKER_RESOURCE_CONFIG_PATH" ]]; then
    logger "Env var SAGEMAKER_RESOURCE_CONFIG_PATH is set and file does not exist: $SAGEMAKER_RESOURCE_CONFIG_PATH"
    exit 1
  fi
fi

logger "Running lifecycle_script.py with resourceConfig: $SAGEMAKER_RESOURCE_CONFIG_PATH, provisioning_parameters: $PROVISIONING_PARAMETERS_PATH"

python3 -u "${SCRIPT_DIR}/lifecycle_script.py" \
  -rc "$SAGEMAKER_RESOURCE_CONFIG_PATH" \
  -pp "$PROVISIONING_PARAMETERS_PATH" > >(tee -a "$LOG_FILE") 2>&1

exit_code=$?

if [[ $exit_code -eq 0 ]]; then
  logger "[INFO] Installing Slurm topology guard"
  bash "${SCRIPT_DIR}/ensure_topology_conf.sh" 2>&1 | tee -a "$LOG_FILE"
fi

exit $exit_code
