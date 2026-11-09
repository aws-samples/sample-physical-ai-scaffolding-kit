#!/bin/bash
# Runs on every cluster node after HyperPod finishes its default configuration
# (LifeCycleConfig.OnInitComplete). Executed as root.
#
# FSx for Lustre creates the data repository association directory as root:root,
# and CreateDataRepositoryAssociation has no parameter for POSIX ownership, so the
# only place to hand it to ubuntu is here. Without this, writing to the S3-linked
# directory requires a manual chown after every deploy.
#
# The placeholder below is replaced with config.Lustre.FileSystemPath by the
# lifecycle-loader lambda when it uploads this file. Do not reference the
# placeholder anywhere else in this file: every occurrence gets substituted, so a
# comparison against it would silently compare the value with itself.

set -euo pipefail

LUSTRE_FILESYSTEM_PATH="__LUSTRE_FILESYSTEM_PATH__"
LUSTRE_MOUNT="/fsx"
OWNER="ubuntu:ubuntu"
MODE="755"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log() {
  echo "[on_init_complete] $*"
}

# Hand the S3-linked directory on Lustre to ubuntu.
chown_lustre_link_directory() {
  # An absolute path is the only valid form. This also catches the case where the
  # substitution did not happen, since the raw placeholder does not start with "/".
  if [[ "${LUSTRE_FILESYSTEM_PATH}" != /* ]]; then
    log "Lustre file system path is not a valid absolute path; nothing to do"
    return 0
  fi

  local relative_path="${LUSTRE_FILESYSTEM_PATH}"
  while [[ "${relative_path}" == */ ]]; do
    relative_path="${relative_path%/}"
  done

  # A path of "/" would hand the whole file system to a single user.
  if [[ -z "${relative_path}" ]]; then
    log "Refusing to change ownership of the mount point ${LUSTRE_MOUNT}"
    return 0
  fi

  local target="${LUSTRE_MOUNT}${relative_path}"

  if [[ ! -d "${target}" ]]; then
    log "${target} does not exist; skipping"
    return 0
  fi

  log "Setting ${target} to ${OWNER} ${MODE}"
  chown "${OWNER}" "${target}"
  chmod "${MODE}" "${target}"
  ls -ld "${target}"
}

# Install the VAMS poller. The script itself does nothing unless the VAMS integration
# is enabled and this is a login node.
#
# A failure is logged and does not fail this script. A failed OnInitComplete fails the
# node creation, which would take the whole cluster down for an optional add-on: the
# cluster stays usable for manual training without the poller, and the poller can be
# installed again by hand with `sudo bash install_vams_poller.sh` once the cause is fixed.
install_vams_poller() {
  if bash "${SCRIPT_DIR}/install_vams_poller.sh"; then
    return 0
  fi
  log "ERROR: install_vams_poller.sh failed; the VAMS poller is not running on this node." >&2
  log "Fix the cause shown above and run: sudo bash ${SCRIPT_DIR}/install_vams_poller.sh" >&2
}

chown_lustre_link_directory
install_vams_poller
