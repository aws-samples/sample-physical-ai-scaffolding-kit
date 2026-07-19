#!/bin/bash
# Shared helpers for lifecycle scripts.
#
# Every lifecycle script should source this file right after `set -...` and
# then call `require_node_type <type>` (or nothing, if the script is meant
# to run on all node types).
#
# Usage:
#     set -exo pipefail
#     . "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"
#     require_node_type controller
#
# After sourcing, the variable NODE_TYPE is set to one of:
#     controller | login | compute | unknown
#
# Detection reads /opt/ml/config/resource_config.json (or the path in
# $SAGEMAKER_RESOURCE_CONFIG_PATH) and matches this node's primary IP
# against the InstanceGroups. If the file is missing or no match is found,
# NODE_TYPE is set to "unknown" and guard calls will skip the script (exit 0).

# _detect_node_type: populate NODE_TYPE from resource_config.json.
# Group name "controller-machine" -> controller
# Group name "login-group"        -> login
# Any other group                 -> compute
# Missing file or unmatched IP    -> unknown
_detect_node_type() {
    local resource_config="${SAGEMAKER_RESOURCE_CONFIG_PATH:-/opt/ml/config/resource_config.json}"
    if [[ ! -f "$resource_config" ]]; then
        NODE_TYPE="unknown"
        return
    fi
    local my_ip my_group
    my_ip=$(hostname -I | awk '{print $1}')
    my_group=$(jq -r --arg ip "$my_ip" \
        '.InstanceGroups[] | select(.Instances[]?.CustomerIpAddress == $ip) | .Name' \
        "$resource_config" 2>/dev/null | head -n1)
    case "$my_group" in
        controller-machine) NODE_TYPE="controller" ;;
        login-group)        NODE_TYPE="login" ;;
        "")                 NODE_TYPE="unknown" ;;
        *)                  NODE_TYPE="compute" ;;
    esac
}

# require_node_type <type> [<type> ...]: skip the script (exit 0) unless this
# node's detected type matches one of the given types. Logs a clear message
# so multi-node runs (run-lifecycle.sh --all) show why a script was skipped.
# If NODE_TYPE is "unknown" (no resource_config.json, unmapped IP), the
# script is also skipped to avoid acting on an unexpected environment.
require_node_type() {
    local script_name
    script_name=$(basename "${BASH_SOURCE[1]:-unknown}")
    for allowed in "$@"; do
        if [[ "$NODE_TYPE" == "$allowed" ]]; then
            return 0
        fi
    done
    echo "$script_name skipped: this node is '$NODE_TYPE', need one of: $*"
    exit 0
}

# wait_for_slurmctld_ready: block until slurmctld answers RPCs.
# `systemctl enable --now slurmctld` returns when the unit has launched, not
# when the controller is answering. On a first boot slurmctld connects to a
# freshly-created RDS accounting DB and initializes its state before it
# responds — and it does so while install_docker.sh / install_enroot_pyxis.sh
# run concurrently on the same controller (a ~30s update-initramfs, apt, and a
# pyxis compile all competing for CPU/IO), so it can take several minutes to
# start answering. A `scontrol reconfigure` that races ahead of that fails with
# "Unable to contact slurm controller (connect failure)" and aborts the
# lifecycle. Call this immediately before such a reconfigure. Probe with
# `scontrol ping` (the reachability check reconfigure needs); abort early if
# the unit died, otherwise give it up to 300s (observed cold-start under
# contention was still not answering at ~168s).
wait_for_slurmctld_ready() {
    local attempt
    for attempt in $(seq 1 300); do
        if ! systemctl is-active --quiet slurmctld; then
            echo "ERROR: slurmctld is not active (attempt $attempt). Recent log:" >&2
            journalctl -u slurmctld -n 20 --no-pager >&2 || true
            return 1
        fi
        if scontrol ping >/dev/null 2>&1; then
            echo "slurmctld ready on attempt $attempt"
            return 0
        fi
        sleep 1
    done
    echo "ERROR: slurmctld did not start answering RPCs within 300s" >&2
    journalctl -u slurmctld -n 20 --no-pager >&2 || true
    return 1
}

# slurm_reconfigure_with_retry: wait for slurmctld to be reachable, then run
# `scontrol reconfigure` with retries.
#
# Every lifecycle reconfigure funnels through here, so the readiness gate
# lives here rather than at each call site: slurmctld may still be doing its
# (minutes-long, first-boot) cold start when a reconfigure fires — either
# because it was only just `systemctl enable --now`'d, or because a sibling
# script restarted it — and a reconfigure that races that cold start fails
# with "Unable to contact slurm controller (connect failure)" and aborts the
# lifecycle. wait_for_slurmctld_ready blocks until it answers (or the unit
# dies). The retry loop then still covers a brief mid-reconfigure stall from
# concurrent apt/kernel work elsewhere in the pipeline.
slurm_reconfigure_with_retry() {
    wait_for_slurmctld_ready || return 1
    local attempt
    for attempt in 1 2 3 4 5 6; do
        if scontrol reconfigure 2>&1; then
            echo "scontrol reconfigure OK on attempt $attempt"
            return 0
        fi
        echo "scontrol reconfigure attempt $attempt failed; retrying in 5s"
        sleep 5
    done
    echo "ERROR: scontrol reconfigure failed after 6 attempts" >&2
    return 1
}

# sanitize_slurm_topology: work around a HyperPod Slurm 25.11 topology config
# that crashes slurmctld on the controller.
#
# Observed on HyperPod clusters on the Slurm 25.11 AMI: $SLURM_DIR/etc contains
# an empty topology.yaml ("[]") and empty topology.conf, and the GPU
# PartitionName carries a `Topology=tree` tag. With that config slurmctld
# crash-loops on SIGSEGV and the controller never stays up (the visible
# "No Assoc usage file" fatal is a downstream effect of that crash). We do not
# know what makes HyperPod generate this config; a 24.11 cluster on the same
# hardware had none of these lines and was healthy.
#
# Removing the empty topology files and stripping the Topology= token (i.e.
# restoring the topology-free shape) is verified to let slurmctld start cleanly.
# That is what this does.
#
# Idempotent. Returns 0 if it changed something (caller may then reconfigure),
# 1 if nothing needed changing or real topology is present.
sanitize_slurm_topology() {
    local etc conf yaml tconf changed f yaml_stripped
    etc="${SLURM_DIR:-/opt/slurm}/etc"
    conf="$etc/slurm.conf"
    yaml="$etc/topology.yaml"
    tconf="$etc/topology.conf"
    changed=false

    if [[ ! -f "$conf" ]]; then
        echo "sanitize_slurm_topology: $conf not found, skipping"
        return 1
    fi

    # Safety guard: only act on HyperPod's EMPTY/placeholder topology artifacts.
    # If real topology is defined (a topology.conf with SwitchName=/BlockName=
    # entries, or a non-empty topology.yaml), this cluster legitimately uses
    # topology-aware scheduling (e.g. p5) — leave everything untouched so the
    # workaround can never disable a real feature.
    if [[ -f "$tconf" ]] && grep -qiE '^[[:space:]]*(SwitchName|BlockName)=' "$tconf"; then
        echo "sanitize_slurm_topology: $tconf defines real switches/blocks — not modifying"
        return 1
    fi
    if [[ -f "$yaml" ]]; then
        yaml_stripped=$(tr -d '[:space:]' < "$yaml")
        if [[ -n "$yaml_stripped" && "$yaml_stripped" != "[]" ]]; then
            echo "sanitize_slurm_topology: $yaml is non-empty — not modifying"
            return 1
        fi
    fi

    # Remove the empty topology files.
    for f in "$yaml" "$tconf"; do
        if [[ -e "$f" ]]; then
            echo "sanitize_slurm_topology: removing empty $f"
            rm -f "$f"
            changed=true
        fi
    done

    # Strip the orphaned `Topology=<x>` token from PartitionName lines.
    if grep -Eq '^PartitionName=.*[[:space:]]Topology=' "$conf"; then
        echo "sanitize_slurm_topology: stripping Topology= from PartitionName lines"
        sed -i -E '/^PartitionName=/ s/[[:space:]]+Topology=[^[:space:]]+//g' "$conf"
        changed=true
    fi

    if $changed; then
        echo "sanitize_slurm_topology: applied HyperPod 25.11 topology workaround"
        return 0
    fi
    return 1
}

_detect_node_type
export NODE_TYPE
