#!/bin/bash
# HyperPod lifecycle entrypoint — called by SageMaker on each node at creation.
set -ex

LOG_FILE="/var/log/provision/provisioning.log"
mkdir -p /var/log/provision
exec > >(tee -a "$LOG_FILE") 2>&1

# Wait for DNS to be ready (HyperPod reboots systemd-resolved during setup)
sleep 30

RESOURCE_CONFIG="${SAGEMAKER_RESOURCE_CONFIG_PATH:-/opt/ml/config/resource_config.json}"

if [[ ! -f "$RESOURCE_CONFIG" ]]; then
  echo "No resource config found at $RESOURCE_CONFIG. Exiting."
  exit 0
fi

python3 -u lifecycle_script.py \
  --resource-config "$RESOURCE_CONFIG"
