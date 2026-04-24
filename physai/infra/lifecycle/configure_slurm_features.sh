#!/bin/bash
# Configure Slurm node Features based on instance type.
# Runs on the CONTROLLER node only. Patches slurm.conf and reconfigures.
# Usage: configure_slurm_features.sh
set -euo pipefail

SLURM_DIR="${SLURM_DIR:-/opt/slurm}"
SLURM_CONF="$SLURM_DIR/etc/slurm.conf"
RESOURCE_CONFIG="/opt/ml/config/resource_config.json"

if [[ ! -f "$RESOURCE_CONFIG" ]]; then
    echo "No resource_config.json found, skipping feature configuration"
    exit 0
fi

if [[ ! -f "$SLURM_CONF" ]]; then
    echo "No slurm.conf found at $SLURM_CONF, skipping"
    exit 0
fi

# Map instance type prefix to Slurm feature name
instance_type_to_feature() {
    local itype="$1"
    case "$itype" in
        ml.g6e.*)  echo "l40s" ;;
        ml.g6.*)   echo "l4"   ;;
        ml.g5.*)   echo "a10g" ;;
        ml.p3.*)   echo "v100" ;;
        ml.p4d.*)  echo "a100" ;;
        ml.p4de.*) echo "a100" ;;
        ml.p5.*)   echo "h100" ;;
        *)         echo ""     ;;
    esac
}

cp "$SLURM_CONF" "$SLURM_CONF.bak-features"

changed=false

# For each instance group, find its instance type and nodes, then patch slurm.conf
for group in $(jq -r '.InstanceGroups[].Name' "$RESOURCE_CONFIG"); do
    itype=$(jq -r --arg g "$group" '.InstanceGroups[] | select(.Name==$g) | .InstanceType' "$RESOURCE_CONFIG")
    feature=$(instance_type_to_feature "$itype")

    if [[ -z "$feature" ]]; then
        continue
    fi

    # Get hostnames for this group
    for hostname in $(jq -r --arg g "$group" '.InstanceGroups[] | select(.Name==$g) | .Instances[]? | .CustomerIpAddress' "$RESOURCE_CONFIG"); do
        # slurm.conf uses the hostname form (ip-10-0-1-98), not the IP
        nodename=$(echo "$hostname" | sed 's/\./-/g; s/^/ip-/')

        # Add Feature= to the NodeName line if not already present
        if grep -q "^NodeName=$nodename " "$SLURM_CONF" && ! grep -q "^NodeName=$nodename .*Feature=" "$SLURM_CONF"; then
            sed -i "s/^NodeName=$nodename .*/& Feature=$feature/" "$SLURM_CONF"
            echo "Added Feature=$feature to $nodename ($group, $itype)"
            changed=true
        elif grep -q "^NodeName=$nodename .*Feature=" "$SLURM_CONF"; then
            echo "Feature already set for $nodename, skipping"
        fi
    done
done

if $changed; then
    scontrol reconfigure
    echo "Slurm reconfigured with node features"
else
    echo "No feature changes needed"
fi
