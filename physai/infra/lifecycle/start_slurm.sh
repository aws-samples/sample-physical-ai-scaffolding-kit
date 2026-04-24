#!/bin/bash
# Start Slurm daemons based on node type.
# Usage: start_slurm.sh <node_type> <controller_ips>
#   node_type: controller, compute, or login
#   controller_ips: comma-separated controller IP addresses
set -ex

NODE_TYPE="$1"
CONTROLLER_IPS="$2"

if [[ "$NODE_TYPE" == "controller" ]]; then
  echo "Starting slurmctld (controller)..."
  systemctl enable --now slurmctld
  # Prevent slurmd from running on controller
  mv /etc/systemd/system/slurmd{,_disabled}.service 2>/dev/null || true

elif [[ "$NODE_TYPE" == "compute" || "$NODE_TYPE" == "login" ]]; then
  echo "Starting slurmd ($NODE_TYPE)..."
  # Point slurmd at the controller for config
  SLURMD_OPTIONS="--conf-server $CONTROLLER_IPS" envsubst < /etc/systemd/system/slurmd.service > /tmp/slurmd.service
  mv /tmp/slurmd.service /etc/systemd/system/slurmd.service
  systemctl daemon-reload
  systemctl enable --now slurmd
  # Prevent slurmctld from running on non-controller
  mv /etc/systemd/system/slurmctld{,_disabled}.service 2>/dev/null || true
fi

echo "Slurm started for $NODE_TYPE"
