#!/bin/bash
# Enable Slurm cgroup process tracking so scancel kills all child processes.
# Adapted from: https://github.com/awslabs/awsome-distributed-training/blob/main/
#   1.architectures/5.sagemaker-hyperpod/LifecycleScripts/base-config/utils/pam_adopt_cgroup_wheel.sh
#
# Usage: configure_slurm_cgroup.sh
set -ex
. "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"
# Login nodes don't need cgroup tracking (no slurmd manages user jobs there).
require_node_type controller compute

SLURM_DIR="${SLURM_DIR:-/opt/slurm}"

# Update or append a key=value in a config file. Sets CHANGED=true if the file
# was modified.
CHANGED=false
set_conf() {
    local key="$1" val="$2" file="$3"
    if grep -q "^${key}=${val}$" "$file" 2>/dev/null; then
        return  # already set to this value
    fi
    if grep -q "^${key}=" "$file" 2>/dev/null; then
        sed -i "s|^${key}=.*|${key}=${val}|" "$file"
    else
        printf '\n%s=%s\n' "$key" "$val" >> "$file"
    fi
    CHANGED=true
}

if [ "$NODE_TYPE" = "controller" ]; then
    SLURM_CONF="$SLURM_DIR/etc/slurm.conf"
    CGROUP_CONF="$SLURM_DIR/etc/cgroup.conf"

    if [ ! -f "$SLURM_CONF" ]; then
        echo "ERROR: $SLURM_CONF not found"
        exit 1
    fi

    set_conf ProctrackType proctrack/cgroup "$SLURM_CONF"
    set_conf PrologFlags Contain "$SLURM_CONF"

    NEW_CGROUP_CONF=$(cat <<EOF
CgroupPlugin=autodetect
ConstrainDevices=yes
ConstrainRAMSpace=yes
ConstrainSwapSpace=yes
SignalChildrenProcesses=yes
MaxRAMPercent=99
EOF
)
    if [ ! -f "$CGROUP_CONF" ] || ! diff -q <(echo "$NEW_CGROUP_CONF") "$CGROUP_CONF" >/dev/null 2>&1; then
        echo "$NEW_CGROUP_CONF" > "$CGROUP_CONF"
        CHANGED=true
    fi

    if $CHANGED; then
        # ProctrackType changes require a full restart; a reconfigure alone
        # won't pick them up.
        systemctl restart slurmctld
        # Wait for slurmctld to come back, then issue a final reconfigure to
        # push the complete config (including pyxis plugstack written by
        # install_enroot_pyxis.sh) to all registered workers. Without this,
        # the restart interrupts any in-flight reconfigure from earlier
        # scripts and Slurm (pre-25.11) does not auto-push on slurmctld
        # restart.
        sleep 5
        slurm_reconfigure_with_retry
        echo "Controller: cgroup config applied, slurmctld restarted and reconfigured"
    else
        echo "Controller: cgroup config already up-to-date, nothing to do"
    fi

else
    # Compute node. Fetches cgroup.conf from the controller via configless.
    # The controller's reconfigure (above) triggers a SIGHUP push to all
    # slurmd instances, which is sufficient — we only need to ensure slurmd
    # is running here.
    if ! systemctl is-active --quiet slurmd; then
        systemctl start slurmd
        echo "Compute: slurmd started"
    else
        echo "Compute: slurmd already running, nothing to do"
    fi
fi
