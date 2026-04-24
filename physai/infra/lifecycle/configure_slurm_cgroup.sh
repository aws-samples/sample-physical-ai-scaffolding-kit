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
        for attempt in 1 2 3 4 5 6; do
            sleep 5
            if scontrol reconfigure 2>&1; then
                echo "Post-restart reconfigure OK on attempt $attempt"
                break
            fi
        done
        echo "Controller: cgroup config applied, slurmctld restarted and reconfigured"
    else
        echo "Controller: cgroup config already up-to-date, nothing to do"
    fi

elif [ "$NODE_TYPE" = "compute" ]; then
    # Compute nodes fetch cgroup.conf from the controller via configless. A
    # restart here is only needed if we think the cached config is stale. The
    # controller's reconfigure (above) triggers a SIGHUP push to all slurmd
    # instances, which is sufficient. Restart only if slurmd isn't running.
    if ! systemctl is-active --quiet slurmd; then
        systemctl start slurmd
        echo "Compute: slurmd started"
    else
        echo "Compute: slurmd already running, nothing to do"
    fi
fi
