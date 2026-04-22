#!/bin/bash
# Configure Slurm accounting (slurmdbd + RDS) so sacct works.
# Runs on the CONTROLLER node only.
# Reads RDS endpoint and Secrets Manager ARN from physai-config.json (deployed
# alongside lifecycle scripts). Fetches DB password from Secrets Manager.
# Usage: configure_slurm_accounting.sh
set -euo pipefail

SLURM_DIR="${SLURM_DIR:-/opt/slurm}"
SLURM_CONF="$SLURM_DIR/etc/slurm.conf"
SLURMDBD_CONF="$SLURM_DIR/etc/slurmdbd.conf"
CONFIG_JSON="$(dirname "$0")/physai-config.json"

if [[ ! -f "$CONFIG_JSON" ]]; then
    echo "No physai-config.json found at $CONFIG_JSON, skipping accounting setup"
    exit 0
fi

if [[ ! -f "$SLURM_CONF" ]]; then
    echo "No slurm.conf found at $SLURM_CONF, skipping"
    exit 0
fi

RDS_ENDPOINT=$(jq -r '.rds_endpoint' "$CONFIG_JSON")
RDS_PORT=$(jq -r '.rds_port' "$CONFIG_JSON")
RDS_DATABASE=$(jq -r '.rds_database' "$CONFIG_JSON")
SECRET_ARN=$(jq -r '.secret_arn' "$CONFIG_JSON")

if [[ -z "$RDS_ENDPOINT" || -z "$SECRET_ARN" ]]; then
    echo "Missing rds_endpoint or secret_arn in $CONFIG_JSON, skipping"
    exit 0
fi

# Resolve region from IMDSv2
TOKEN=$(curl -sX PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 60")
REGION=$(curl -sH "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/placement/region)

# Install mariadb client and slurmdbd if missing
export DEBIAN_FRONTEND=noninteractive
APT_OPTS=(-o 'Dpkg::Options::=--force-confold' -o 'Dpkg::Options::=--force-confdef')
if ! command -v mysql >/dev/null 2>&1; then
    apt-get update -qq && apt-get install -y -qq "${APT_OPTS[@]}" mariadb-client
fi
if ! systemctl list-unit-files | grep -q '^slurmdbd\.service'; then
    apt-get install -y -qq "${APT_OPTS[@]}" slurmdbd || true
fi

# Fetch DB credentials from Secrets Manager
SECRET=$(aws secretsmanager get-secret-value --region "$REGION" --secret-id "$SECRET_ARN" --query SecretString --output text)
DB_USER=$(echo "$SECRET" | jq -r '.username')
DB_PASS=$(echo "$SECRET" | jq -r '.password')

# Write slurmdbd.conf (chmod 600, password in memory until write). Skip the
# write and slurmdbd restart if the content is already identical — avoids a
# pointless ~5s sacct interruption when re-running manually.
umask 077
NEW_SLURMDBD_CONF=$(cat <<EOF
AuthType=auth/munge
DbdHost=localhost
DbdPort=6819
SlurmUser=slurm
StorageType=accounting_storage/mysql
StorageHost=${RDS_ENDPOINT}
StoragePort=${RDS_PORT}
StorageLoc=${RDS_DATABASE}
StorageUser=${DB_USER}
StoragePass=${DB_PASS}
LogFile=/var/log/slurm/slurmdbd.log
PidFile=/var/run/slurmdbd.pid
EOF
)
slurmdbd_changed=false
if [[ ! -f "$SLURMDBD_CONF" ]] || ! diff -q <(echo "$NEW_SLURMDBD_CONF") "$SLURMDBD_CONF" >/dev/null 2>&1; then
    echo "$NEW_SLURMDBD_CONF" > "$SLURMDBD_CONF"
    chown slurm:slurm "$SLURMDBD_CONF" 2>/dev/null || true
    slurmdbd_changed=true
fi

# Patch slurm.conf (idempotent). Ensure the file ends with a newline before
# appending — HyperPod's slurm.conf may not have a trailing newline.
[[ -n "$(tail -c 1 "$SLURM_CONF")" ]] && echo "" >> "$SLURM_CONF"
slurm_conf_changed=false
add_if_missing() {
    local line="$1"
    if ! grep -Fqx "$line" "$SLURM_CONF"; then
        echo "$line" >> "$SLURM_CONF"
        slurm_conf_changed=true
    fi
}
add_if_missing "AccountingStorageType=accounting_storage/slurmdbd"
add_if_missing "AccountingStorageHost=$(hostname)"
add_if_missing "AccountingStoragePort=6819"

# Start slurmdbd (enable is idempotent; restart only if config changed or
# slurmdbd isn't running).
mkdir -p /var/log/slurm
systemctl enable slurmdbd
if $slurmdbd_changed || ! systemctl is-active --quiet slurmdbd; then
    systemctl restart slurmdbd
    sleep 3
fi

# Reload slurmctld only if slurm.conf changed. slurmctld is already running at
# this point (not restarting), so a single reconfigure call is sufficient.
if $slurm_conf_changed; then
    scontrol reconfigure
fi

# Register the cluster in the accounting DB
CLUSTER_NAME=$(awk -F= '/^ClusterName=/{print $2}' "$SLURM_CONF" | tail -n1 | tr -d '[:space:]')
if [[ -n "$CLUSTER_NAME" ]]; then
    sacctmgr -i add cluster "$CLUSTER_NAME" 2>/dev/null || true
fi

echo "Slurm accounting configured (RDS: $RDS_ENDPOINT)"
