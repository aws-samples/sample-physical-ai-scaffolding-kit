#!/bin/bash
# Configure Slurm accounting (slurmdbd + RDS) so sacct works.
# Runs on the CONTROLLER node only.
# Reads RDS endpoint and Secrets Manager ARN from physai-config.json (deployed
# alongside lifecycle scripts). Fetches DB password from Secrets Manager.
# Usage: configure_slurm_accounting.sh
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"
require_node_type controller

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

# Ensure this controller's hostname resolves locally before slurmdbd/slurmctld
# start. slurmdbd binds to DbdHost (the controller's hostname) and slurmctld
# reads AccountingStorageHost from slurm.conf — both call getaddrinfo on that
# name. HyperPod nodes don't always have working VPC DNS for their own
# hostname at first-boot time, so we add a self-entry to /etc/hosts. The
# value goes in slurm.conf, which is fetched by every login/compute node, so
# it must be a name they can also resolve — VPC DNS handles that for them.
HOST=$(hostname)
PRIMARY_IP=$(hostname -I | awk '{print $1}')
if [[ -n "$PRIMARY_IP" ]] && ! grep -Eq "^[^#]*[[:space:]]${HOST}([[:space:]]|$)" /etc/hosts; then
    echo "${PRIMARY_IP} ${HOST}" >> /etc/hosts
fi

# Install mariadb client if missing. slurmdbd itself ships with the HyperPod
# AMI (the systemd unit is preinstalled and `systemctl enable slurmdbd`
# succeeds without us touching apt) — don't try to install it from apt, the
# Ubuntu package isn't in the available repos and it just produces a noisy
# "E: Unable to locate package slurmdbd" log line.
export DEBIAN_FRONTEND=noninteractive
APT_OPTS=(-o 'Dpkg::Options::=--force-confold' -o 'Dpkg::Options::=--force-confdef')
if ! command -v mysql >/dev/null 2>&1; then
    apt-get update -qq && apt-get install -y -qq "${APT_OPTS[@]}" mariadb-client
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
DbdHost=${HOST}
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
# Set Key=Value, replacing any existing line for the same key (idempotent
# across re-runs even if a previous version wrote a different value, e.g.
# AccountingStorageHost=ip-10-0-2-227 vs =ip-10-0-1-58 on a controller
# replacement). Slurm would otherwise honor the last occurrence, but the
# stale line is confusing and brittle.
set_kv() {
    local kv="$1" key="${1%%=*}"
    if grep -Eq "^${key}=" "$SLURM_CONF"; then
        if ! grep -Fqx "$kv" "$SLURM_CONF"; then
            sed -i "s|^${key}=.*|${kv}|" "$SLURM_CONF"
            slurm_conf_changed=true
        fi
    else
        echo "$kv" >> "$SLURM_CONF"
        slurm_conf_changed=true
    fi
}
set_kv "AccountingStorageType=accounting_storage/slurmdbd"
# AccountingStorageHost is the controller hostname — slurmdbd runs only here,
# but slurm.conf is read by every login/compute node, and they call sacct /
# sacctmgr against this address. localhost would only work on the controller
# itself; other nodes would try to connect to themselves where no slurmdbd
# listens. VPC DNS resolves the short hostname for non-controller nodes;
# /etc/hosts above guarantees it resolves on the controller too.
set_kv "AccountingStorageHost=${HOST}"
set_kv "AccountingStoragePort=6819"

# Start slurmdbd (enable is idempotent; restart only if config changed or
# slurmdbd isn't running).
mkdir -p /var/log/slurm
systemctl enable slurmdbd
if $slurmdbd_changed || ! systemctl is-active --quiet slurmdbd; then
    systemctl restart slurmdbd
    sleep 3
fi

# If slurmdbd config changed (i.e. we may now be pointing at a different DBD,
# or starting one for the first time), the cached cluster_id in slurmctld's
# state file may no longer match what the (new) DBD will hand back. Delete
# the cache and restart slurmctld so it re-fetches a fresh cluster_id from
# DBD on next start. This is exactly the override the slurmctld safety check
# documents ("Remove .../clustername to override this safety check"). On a
# fresh bootstrap (slurmctld not yet running), the rm is a no-op and there's
# nothing to restart — start_slurm.sh starts slurmctld next, with the new
# accounting config already in slurm.conf.
if $slurmdbd_changed; then
    state_save_location=$(awk -F= '/^StateSaveLocation=/{print $2; exit}' "$SLURM_CONF" | tr -d '[:space:]')
    rm -f "${state_save_location:-/var/spool/slurmctld}/clustername"
    if systemctl is-active --quiet slurmctld; then
        systemctl restart slurmctld
        sleep 3
    fi
fi

# Reload slurmctld only if slurm.conf changed AND slurmctld is already
# running. On fresh bootstrap, slurmctld isn't started yet (start_slurm.sh
# runs after this script) — it'll read the new slurm.conf at startup. On
# in-place re-runs, the retry-with-backoff covers transient unreachability
# from concurrent apt/kernel work elsewhere in the lifecycle.
if $slurm_conf_changed && systemctl is-active --quiet slurmctld; then
    slurm_reconfigure_with_retry
fi

# Register the cluster in the accounting DB. `sacctmgr add cluster` returns
# exit 1 if the cluster is already registered, so check first and only add
# if missing — this surfaces real errors (slurmdbd unreachable, permission
# denied, etc.) instead of swallowing them all.
CLUSTER_NAME=$(awk -F= '/^ClusterName=/{print $2}' "$SLURM_CONF" | tail -n1 | tr -d '[:space:]')
if [[ -n "$CLUSTER_NAME" ]]; then
    if sacctmgr -n -P list cluster "$CLUSTER_NAME" format=Cluster | grep -Fxq "$CLUSTER_NAME"; then
        echo "Cluster $CLUSTER_NAME already registered in accounting DB"
    else
        sacctmgr -i add cluster "$CLUSTER_NAME"
    fi
fi

echo "Slurm accounting configured (RDS: $RDS_ENDPOINT)"
