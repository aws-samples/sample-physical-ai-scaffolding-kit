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

# slurm_reconfigure_with_retry: wrap `scontrol reconfigure` in a retry loop.
# slurmctld can be transiently unreachable in the lifecycle pipeline (apt
# package processing, initramfs rebuilds, kernel-trigger work all happen on
# the controller during install_docker.sh / install_enroot_pyxis.sh and can
# briefly stall slurmctld). A single naive reconfigure that hits that window
# fails with "Unable to contact slurm controller (connect failure)" and
# aborts the lifecycle. Retry up to 6 times with 5s sleeps (~30s budget).
slurm_reconfigure_with_retry() {
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

_detect_node_type
export NODE_TYPE
