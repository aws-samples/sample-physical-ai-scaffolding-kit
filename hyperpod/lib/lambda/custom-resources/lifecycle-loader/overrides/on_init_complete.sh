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

log() {
  echo "[on_init_complete] $*"
}

# An absolute path is the only valid form. This also catches the case where the
# substitution did not happen, since the raw placeholder does not start with "/".
if [[ "${LUSTRE_FILESYSTEM_PATH}" != /* ]]; then
  log "Lustre file system path is not a valid absolute path; nothing to do"
  exit 0
fi

relative_path="${LUSTRE_FILESYSTEM_PATH}"
while [[ "${relative_path}" == */ ]]; do
  relative_path="${relative_path%/}"
done

# A path of "/" would hand the whole file system to a single user.
if [[ -z "${relative_path}" ]]; then
  log "Refusing to change ownership of the mount point ${LUSTRE_MOUNT}"
  exit 0
fi

target="${LUSTRE_MOUNT}${relative_path}"

if [[ ! -d "${target}" ]]; then
  log "${target} does not exist; skipping"
  exit 0
fi

log "Setting ${target} to ${OWNER} ${MODE}"
chown "${OWNER}" "${target}"
chmod "${MODE}" "${target}"
ls -ld "${target}"
