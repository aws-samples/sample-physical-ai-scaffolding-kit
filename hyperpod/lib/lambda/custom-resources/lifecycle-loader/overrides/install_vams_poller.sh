#!/bin/bash
# Install the VAMS poller as the systemd service pask-vams-poller on the login node.
# Executed as root. Called from on_init_complete.sh on every node, and can be run by
# hand on an existing login node with `sudo bash install_vams_poller.sh` from a copy of
# the lifecycle script bucket. Running it again gives the same result.
#
# The script does nothing unless the VAMS integration is enabled and this node belongs
# to the login group. It then:
#   1. copies the poller sources from vams_poller/ next to this script to
#      /opt/pask/vams-poller/ (the lifecycle directory is removed on reboot),
#   2. creates a venv there and installs requirements.txt and the poller package,
#   3. writes the poller settings to /etc/pask/vams-poller.env,
#   4. writes, enables and restarts pask-vams-poller.service.
#
# The placeholders below are replaced by the lifecycle-loader lambda when it uploads
# this file. Do not reference a placeholder anywhere else in this file: every
# occurrence gets substituted, so a comparison against it would silently compare the
# value with itself. Unsubstituted placeholders fail the checks below because they are
# neither "true" nor a real group name.

set -euo pipefail

VAMS_ENABLED="__VAMS_ENABLED__"
LOGIN_GROUP_NAME="__LOGIN_GROUP_NAME__"
VAMS_QUEUE_URL="__VAMS_QUEUE_URL__"
VAMS_TABLE_NAME="__VAMS_TABLE_NAME__"
VAMS_DATA_BUCKET="__VAMS_DATA_BUCKET__"
LUSTRE_S3_PREFIX="__LUSTRE_S3_PREFIX__"
LUSTRE_FILESYSTEM_PATH="__LUSTRE_FILESYSTEM_PATH__"
VAMS_REGION="__VAMS_REGION__"
VAMS_CONFIG_NAME="__VAMS_CONFIG_NAME__"
VAMS_VISIBILITY_TIMEOUT_SECONDS="__VAMS_VISIBILITY_TIMEOUT_SECONDS__"
VAMS_HEARTBEAT_INTERVAL_SECONDS="__VAMS_HEARTBEAT_INTERVAL_SECONDS__"
VAMS_ABORT_GRACE_SECONDS="__VAMS_ABORT_GRACE_SECONDS__"
VAMS_LEASE_SECONDS="__VAMS_LEASE_SECONDS__"
VAMS_ITEM_TTL_DAYS="__VAMS_ITEM_TTL_DAYS__"
VAMS_INGEST_WAIT_SECONDS="__VAMS_INGEST_WAIT_SECONDS__"
VAMS_MAX_CONCURRENT_RUNS="__VAMS_MAX_CONCURRENT_RUNS__"
VAMS_SLURM_PARTITION="__VAMS_SLURM_PARTITION__"
VAMS_MLFLOW_EXPERIMENT_NAME="__VAMS_MLFLOW_EXPERIMENT_NAME__"

SERVICE_USER="ubuntu"
SERVICE_NAME="pask-vams-poller.service"
INSTALL_DIR="/opt/pask/vams-poller"
VENV_DIR="${INSTALL_DIR}/.venv"
CONFIG_DIR="/etc/pask"
CONFIG_FILE="${CONFIG_DIR}/vams-poller.env"
UNIT_FILE="/etc/systemd/system/${SERVICE_NAME}"
BASE_SERVICE_PATH="/opt/slurm/bin:/usr/local/bin:/usr/bin:/bin"
RESOURCE_CONFIG="${SAGEMAKER_RESOURCE_CONFIG_PATH:-/opt/ml/config/resource_config.json}"
APT_LOCK_TIMEOUT_SECONDS=600
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DIR="${SCRIPT_DIR}/vams_poller"
# The home directory of the service user is not always /home/<user>: the HyperPod
# base config moves it to the FSx file system when one is mounted.
SERVICE_HOME="$(getent passwd "${SERVICE_USER}" | cut -d: -f6)"
TRAINING_ENV_FILE="${SERVICE_HOME}/.config/pask/lerobot-openpi.env"

log() {
  echo "[install_vams_poller] $*"
}

fail() {
  echo "[install_vams_poller] ERROR: $*" >&2
  exit 1
}

# Print the instance group name of this node, or nothing when it cannot be found.
# Same lookup as _detect_node_type in physai/infra/lifecycle/_lib.sh: match the
# primary IP of this node against the instances of resource_config.json.
detect_group_name() {
  if [[ ! -f "${RESOURCE_CONFIG}" ]]; then
    return 0
  fi
  command -v jq >/dev/null 2>&1 || fail "jq is required to read ${RESOURCE_CONFIG}"
  local my_ip
  my_ip="$(hostname -I | awk '{print $1}')"
  jq -r --arg ip "${my_ip}" \
    'first(.InstanceGroups[] | select(any(.Instances[]?; .CustomerIpAddress == $ip)) | .Name) // empty' \
    "${RESOURCE_CONFIG}"
}

# Fail when a required setting is empty.
require_setting() {
  local name="$1"
  local value="$2"
  [[ -n "${value}" ]] || fail "${name} is empty; redeploy the stack with config.Vams set"
}

# Install python3-venv when the system python cannot create a venv with pip.
# Ubuntu ships ensurepip only with the python3-venv package.
ensure_python_venv() {
  command -v python3 >/dev/null 2>&1 || fail "python3 is not installed"
  if python3 -c 'import ensurepip' >/dev/null 2>&1; then
    return 0
  fi
  log "Installing python3-venv"
  apt-get -o DPkg::Lock::Timeout="${APT_LOCK_TIMEOUT_SECONDS}" update
  DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout="${APT_LOCK_TIMEOUT_SECONDS}" \
    install -y python3-venv
}

# Replace the installed sources and the venv with fresh ones.
install_poller_package() {
  [[ -f "${SOURCE_DIR}/pyproject.toml" ]] || fail "poller sources not found in ${SOURCE_DIR}"
  [[ -f "${SOURCE_DIR}/requirements.txt" ]] || fail "requirements.txt not found in ${SOURCE_DIR}"

  if [[ -f "${UNIT_FILE}" ]]; then
    log "Stopping ${SERVICE_NAME} before replacing ${INSTALL_DIR}"
    systemctl stop "${SERVICE_NAME}"
  fi

  log "Copying the poller sources to ${INSTALL_DIR}"
  rm -rf "${INSTALL_DIR}"
  mkdir -p "${INSTALL_DIR}"
  cp -r "${SOURCE_DIR}/." "${INSTALL_DIR}/"
  chown -R root:root "${INSTALL_DIR}"
  chmod -R u=rwX,go=rX "${INSTALL_DIR}"

  log "Creating the venv in ${VENV_DIR}"
  python3 -m venv "${VENV_DIR}"
  "${VENV_DIR}/bin/pip" install --no-cache-dir -r "${INSTALL_DIR}/requirements.txt"
  "${VENV_DIR}/bin/pip" install --no-cache-dir --no-deps "${INSTALL_DIR}"
}

# Write the PASK_VAMS_* settings read by pask_vams_poller.config.PollerConfig.
write_config_file() {
  mkdir -p "${CONFIG_DIR}"
  chmod 0755 "${CONFIG_DIR}"
  local tmp_file
  tmp_file="$(mktemp "${CONFIG_DIR}/.vams-poller.env.XXXXXX")"
  cat > "${tmp_file}" << EOF
PASK_VAMS_QUEUE_URL=${VAMS_QUEUE_URL}
PASK_VAMS_TABLE_NAME=${VAMS_TABLE_NAME}
PASK_VAMS_DATA_BUCKET=${VAMS_DATA_BUCKET}
PASK_VAMS_LUSTRE_S3_PREFIX=${LUSTRE_S3_PREFIX}
PASK_VAMS_LUSTRE_FS_PATH=${LUSTRE_FILESYSTEM_PATH}
PASK_VAMS_REGION=${VAMS_REGION}
PASK_VAMS_CONFIG_NAME=${VAMS_CONFIG_NAME}
PASK_VAMS_VISIBILITY_TIMEOUT_SECONDS=${VAMS_VISIBILITY_TIMEOUT_SECONDS}
PASK_VAMS_HEARTBEAT_INTERVAL_SECONDS=${VAMS_HEARTBEAT_INTERVAL_SECONDS}
PASK_VAMS_ABORT_GRACE_SECONDS=${VAMS_ABORT_GRACE_SECONDS}
PASK_VAMS_LEASE_SECONDS=${VAMS_LEASE_SECONDS}
PASK_VAMS_ITEM_TTL_DAYS=${VAMS_ITEM_TTL_DAYS}
PASK_VAMS_INGEST_WAIT_SECONDS=${VAMS_INGEST_WAIT_SECONDS}
PASK_VAMS_MAX_CONCURRENT_RUNS=${VAMS_MAX_CONCURRENT_RUNS}
PASK_VAMS_SLURM_PARTITION=${VAMS_SLURM_PARTITION}
PASK_VAMS_MLFLOW_EXPERIMENT_NAME=${VAMS_MLFLOW_EXPERIMENT_NAME}
PASK_VAMS_TRAINING_ENV_FILE=${TRAINING_ENV_FILE}
EOF
  chown "root:${SERVICE_USER}" "${tmp_file}"
  chmod 0640 "${tmp_file}"
  mv -f "${tmp_file}" "${CONFIG_FILE}"
  log "Wrote ${CONFIG_FILE}"
}

# Print the PATH of the service. The directory of sbatch, when found, comes first, because
# the Slurm install location cannot be fixed in advance.
service_path() {
  local sbatch_path
  sbatch_path="$(PATH="${PATH}:${BASE_SERVICE_PATH}"; command -v sbatch || true)"
  if [[ -z "${sbatch_path}" ]]; then
    echo "[install_vams_poller] WARNING: sbatch not found; the poller cannot submit jobs until Slurm is installed" >&2
    echo "${BASE_SERVICE_PATH}"
    return 0
  fi
  local sbatch_dir
  sbatch_dir="$(dirname "${sbatch_path}")"
  case ":${BASE_SERVICE_PATH}:" in
    *":${sbatch_dir}:"*) echo "${BASE_SERVICE_PATH}" ;;
    *) echo "${sbatch_dir}:${BASE_SERVICE_PATH}" ;;
  esac
}

# Write the unit. Ordering after munge and sackd is a no-op when a unit does not exist on
# this node, so it covers the Slurm client setups of both older and newer HyperPod images.
# StartLimitIntervalSec=0 keeps systemd restarting the poller while it waits for the
# training environment file, instead of giving up after a burst of failures.
write_unit_file() {
  local path_value
  path_value="$(service_path)"
  cat > "${UNIT_FILE}" << EOF
[Unit]
Description=PASK VAMS training request poller
Wants=network-online.target
After=network-online.target munge.service sackd.service
StartLimitIntervalSec=0

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
EnvironmentFile=${CONFIG_FILE}
Environment=PATH=${path_value}
Environment=PYTHONUNBUFFERED=1
ExecStart=${VENV_DIR}/bin/python -m pask_vams_poller.main
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
  chmod 0644 "${UNIT_FILE}"
  log "Wrote ${UNIT_FILE} (PATH=${path_value})"
}

main() {
  if [[ "${VAMS_ENABLED}" != "true" ]]; then
    log "VAMS integration is not enabled; nothing to do"
    exit 0
  fi
  local group_name
  group_name="$(detect_group_name)"
  if [[ -z "${group_name}" || "${group_name}" != "${LOGIN_GROUP_NAME}" ]]; then
    log "This node is not in the login group (group: '${group_name}'); nothing to do"
    exit 0
  fi
  log "Installing the VAMS poller on login group ${group_name}"

  require_setting "VAMS queue URL" "${VAMS_QUEUE_URL}"
  require_setting "VAMS table name" "${VAMS_TABLE_NAME}"
  require_setting "PASK data bucket" "${VAMS_DATA_BUCKET}"
  require_setting "Lustre file system path" "${LUSTRE_FILESYSTEM_PATH}"
  require_setting "region" "${VAMS_REGION}"
  require_setting "VAMS config name" "${VAMS_CONFIG_NAME}"
  id "${SERVICE_USER}" >/dev/null 2>&1 || fail "user ${SERVICE_USER} does not exist"
  [[ -n "${SERVICE_HOME}" ]] || fail "cannot resolve the home directory of ${SERVICE_USER}"

  ensure_python_venv
  install_poller_package
  write_config_file
  write_unit_file

  systemctl daemon-reload
  systemctl enable "${SERVICE_NAME}"
  systemctl restart "${SERVICE_NAME}"

  if [[ -f "${TRAINING_ENV_FILE}" ]]; then
    log "${SERVICE_NAME} restarted; check it with: journalctl -u ${SERVICE_NAME} -f"
  else
    log "${TRAINING_ENV_FILE} does not exist yet. ${SERVICE_NAME} keeps restarting until it does."
    log "As ${SERVICE_USER}, run samples/lerobot-openpi/training/setup.sh --poller-env to create it."
  fi
}

main "$@"
