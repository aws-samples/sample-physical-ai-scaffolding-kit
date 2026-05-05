#!/bin/bash
# register_slurm_features.sh — Install systemd service + path unit that
# self-registers this worker node's Slurm Feature (e.g. "l40s", "a10g") via
# `scontrol update`.
#
# Runs on compute/worker nodes only. Installs two systemd units:
#
#   register-slurm-features.service (oneshot)
#     - Detects instance type via IMDSv2 and maps it to a feature name
#     - Runs `scontrol update NodeName=<self> ActiveFeatures=X AvailableFeatures=X`
#       with retry (slurmd may not be registered with slurmctld yet)
#     - Runs at boot (WantedBy=multi-user.target, After=slurmd.service)
#
#   register-slurm-features.path
#     - Watches /var/spool/slurmd/conf-cache/slurm.conf
#     - Triggers the .service on every modification of that file
#     - In HyperPod's configless mode (slurmd --conf-server), slurmctld
#       pushes a fresh slurm.conf to this path on every `scontrol reconfigure`,
#       so the .path unit fires on every reconfigure.
#
# Why re-register on reconfigure: features set via `scontrol update` do NOT
# survive slurmctld reconfigure. They revert to whatever is in slurm.conf
# (which has no Feature= entries — HyperPod owns those lines).
#
# Why not ReloadPropagatedFrom=slurmd.service: slurmd receives SIGHUP via
# Slurm's internal RPC mechanism (slurmctld → slurmd reconfigure RPC), not
# via `systemctl reload`. systemd is unaware, so PropagatesReloadTo never
# fires. Verified empirically on a live cluster.
#
# Why worker-side and not controller-side: HyperPod's Slurm agent owns the
# NodeName lines in slurm.conf and rewrites them on cluster updates. The
# controller also provisions before workers exist, so a controller-side
# script can't see new nodes added via scaling.
#
# Usage: register_slurm_features.sh
set -exo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"
require_node_type compute

SCRIPT_PATH="/usr/local/sbin/register-slurm-features"
SERVICE_PATH="/etc/systemd/system/register-slurm-features.service"
PATH_UNIT_PATH="/etc/systemd/system/register-slurm-features.path"
# In HyperPod configless mode, slurmd caches configs here and rewrites them
# on every `scontrol reconfigure`.
SLURM_CONF_WATCH="/var/spool/slurmd/conf-cache/slurm.conf"

# Write the registration script.
cat > "$SCRIPT_PATH" << 'SCRIPT_EOF'
#!/bin/bash
# Self-register this node's Slurm Feature based on EC2 instance type.
# Installed by register_slurm_features.sh lifecycle script.
set -eo pipefail

# Resolve scontrol at runtime. Prefer /opt/slurm (HyperPod's symlink to the
# active install) so we never pick up a staged but inactive version like
# /opt/slurm-25.11 that would cause RPC version mismatches with slurmctld.
SLURM_BIN="/opt/slurm/bin/scontrol"
if [[ ! -x "$SLURM_BIN" ]]; then
    SLURM_BIN="$(command -v scontrol || true)"
fi
if [[ -z "$SLURM_BIN" ]]; then
    echo "scontrol not found, cannot register features" >&2
    exit 1
fi

# Get instance type via IMDSv2
TOKEN=$(curl -sfX PUT "http://169.254.169.254/latest/api/token" \
    -H "X-aws-ec2-metadata-token-ttl-seconds: 60")
ITYPE=$(curl -sf -H "X-aws-ec2-metadata-token: $TOKEN" \
    "http://169.254.169.254/latest/meta-data/instance-type")

# Map instance type to a Slurm feature name. Keep in sync with any partition
# constraints (e.g. `--constraint=l40s`) used in run_config.yaml files.
case "$ITYPE" in
    g6e.*)  FEATURE="l40s" ;;
    g6.*)   FEATURE="l4"   ;;
    g5.*)   FEATURE="a10g" ;;
    p3.*)   FEATURE="v100" ;;
    p4d.*)  FEATURE="a100" ;;
    p4de.*) FEATURE="a100" ;;
    p5.*)   FEATURE="h100" ;;
    *)
        echo "No feature mapping for instance type $ITYPE, skipping"
        exit 0
        ;;
esac

NODENAME=$(hostname -s)
echo "Registering $NODENAME ($ITYPE) with feature=$FEATURE"

# slurmd may have started but slurmctld may not have registered this node yet.
# Retry with backoff for up to ~60s.
for attempt in 1 2 3 4 5 6 7 8 9 10; do
    if "$SLURM_BIN" update "NodeName=$NODENAME" \
        "ActiveFeatures=$FEATURE" "AvailableFeatures=$FEATURE" 2>&1; then
        echo "Feature registered successfully"
        exit 0
    fi
    echo "Attempt $attempt failed, retrying in 6s..."
    sleep 6
done

echo "ERROR: Failed to register feature after 10 attempts" >&2
exit 1
SCRIPT_EOF

chmod 0755 "$SCRIPT_PATH"

# Write the .service unit.
#
# RemainAfterExit is NOT set (defaults to no): for the .path unit to
# re-trigger this service on subsequent file modifications, the service
# must be in an inactive state when the path event fires. With
# RemainAfterExit=yes, systemd would treat the oneshot as permanently
# active and the .path unit would skip re-triggering.
cat > "$SERVICE_PATH" << EOF
[Unit]
Description=Register this node's Slurm Feature based on instance type
After=slurmd.service
Requires=slurmd.service

[Service]
Type=oneshot
ExecStart=$SCRIPT_PATH

[Install]
WantedBy=multi-user.target
EOF

# Write the .path unit that watches slurm.conf. PathModified fires on any
# write+close to the file, which is what slurmd does when it pulls a new
# config via --conf-server on reconfigure.
cat > "$PATH_UNIT_PATH" << EOF
[Unit]
Description=Watch slurm.conf cache and re-register node feature on change
After=slurmd.service
Requires=slurmd.service

[Path]
PathModified=$SLURM_CONF_WATCH
Unit=register-slurm-features.service

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
# Enable + (re)start both units so that on re-runs of this script, any
# changes to the unit files or the registration script take effect
# immediately. `enable --now` alone is a no-op for already-active units,
# so we `enable` (idempotent) then `restart` (picks up new definitions).
systemctl enable register-slurm-features.service register-slurm-features.path
systemctl restart register-slurm-features.path
systemctl restart register-slurm-features.service

if systemctl is-active --quiet register-slurm-features.path; then
    echo "register-slurm-features.path is active"
else
    echo "WARNING: register-slurm-features.path not active; check journalctl -u register-slurm-features.path"
fi
