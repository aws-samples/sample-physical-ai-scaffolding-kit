#!/bin/bash
# Enable Slurm cgroup process tracking so scancel kills all child processes.
# Adapted from: https://github.com/awslabs/awsome-distributed-training/blob/main/
#   1.architectures/5.sagemaker-hyperpod/LifecycleScripts/base-config/utils/pam_adopt_cgroup_wheel.sh
#
# Usage: configure_slurm_cgroup.sh <node_type>
#   node_type: controller | login | compute
set -ex

NODE_TYPE="${1:?Usage: configure_slurm_cgroup.sh <node_type>}"

if [ "$NODE_TYPE" = "login" ]; then
    echo "Login node: nothing to do for cgroup config"
    exit 0
fi

SLURM_DIR="${SLURM_DIR:-/opt/slurm}"

# Update or append a key=value in a config file
set_conf() {
    local file="$3" key="$1" val="$2"
    if grep -q "^${key}=" "$file" 2>/dev/null; then
        sed -i "s|^${key}=.*|${key}=${val}|" "$file"
    else
        printf '\n%s=%s\n' "$key" "$val" >> "$file"
    fi
}

if [ "$NODE_TYPE" = "controller" ]; then
    SLURM_CONF="$SLURM_DIR/etc/slurm.conf"
    CGROUP_CONF="$SLURM_DIR/etc/cgroup.conf"

    if [ ! -f "$SLURM_CONF" ]; then
        echo "ERROR: $SLURM_CONF not found"
        exit 1
    fi

    cp "$SLURM_CONF" "${SLURM_CONF}.pre-cgroup"

    set_conf ProctrackType proctrack/cgroup "$SLURM_CONF"
    set_conf PrologFlags Contain "$SLURM_CONF"

    cat > "$CGROUP_CONF" <<EOF
CgroupPlugin=autodetect
ConstrainDevices=yes
ConstrainRAMSpace=yes
ConstrainSwapSpace=yes
SignalChildrenProcesses=yes
MaxRAMPercent=99
EOF

    systemctl restart slurmctld
    echo "Controller: cgroup config applied, slurmctld restarted"

elif [ "$NODE_TYPE" = "compute" ]; then
    systemctl restart slurmd
    echo "Compute: slurmd restarted to pick up cgroup config"
fi
