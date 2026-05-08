#!/bin/bash
# Bootstrap script for NVIDIA Isaac Sim Development Workstation AMI
# This AMI already includes: NVIDIA drivers, Desktop/GDM, NICE DCV, Isaac Sim, PyTorch
# This script configures: ROS2 Jazzy, S3 Files mount
#
# USAGE GUIDE (read this if something failed)
# ============================================================
# Where to look:
#   - Summary (quick, one line per step): /var/log/workstation-bootstrap.summary
#       Entries are prefixed with one of: STEP_OK, STEP_WARN, STEP_FAIL
#         * STEP_OK  : step completed successfully
#         * STEP_WARN: step failed but was non-fatal and intentionally ignored
#         * STEP_FAIL: critical step failed; see detailed log
#   - Detailed log: /var/log/workstation-bootstrap.log
#
# How to interpret and fix:
#   1) Session Manager into the instance and review the summary:
#        sudo cat /var/log/workstation-bootstrap.summary
#   2) For each STEP_FAIL, open the detailed log around the time it ran:
#        sudo less +G /var/log/workstation-bootstrap.log
#   3) Fix the underlying issue (e.g., networking, package mirror, permissions).
#
# Step-specific log viewing commands:
#   - View logs for a specific step:
#        sudo grep -A 50 "== START: <step-name> ==" /var/log/workstation-bootstrap.log
#   - View step completion status:
#        ls -la /var/lib/workstation-bootstrap/
#
# Re-running only the failed steps (idempotent):
#   - This script creates state markers in: /var/lib/workstation-bootstrap/<step-name>.done
#   - Re-running the entire script will SKIP steps already marked done.
#   - To force re-run a specific step, delete its marker and re-run the script:
#        sudo rm "/var/lib/workstation-bootstrap/<step-name>.done"
#        sudo bash /var/lib/cloud/instance/scripts/part-001
#
# Common checks:
#   - DCV server status:    sudo systemctl status dcvserver --no-pager
#   - DCV sessions:         sudo dcv list-sessions
#   - SSM Agent status:     sudo systemctl status amazon-ssm-agent --no-pager
#   - S3 Files mount:       mount | grep ' /mnt/s3files '
# ============================================================

set -Eeuo pipefail

LOG="/var/log/workstation-bootstrap.log"
SUMMARY="/var/log/workstation-bootstrap.summary"
STATE_DIR="/var/lib/workstation-bootstrap"
mkdir -p "$STATE_DIR"

# Timestamped logging to file and syslog
exec > >(awk '{ print strftime("[%Y-%m-%d %H:%M:%S]"), $0 }' | tee -a "$LOG" | logger -t user-data -s 2>/dev/null) 2>&1

CURRENT_STEP=""
FAILURES=0
export DEBIAN_FRONTEND=noninteractive

on_error() {
  local line="$1" cmd="$2" rc="$3"
  echo "ERROR: step='$CURRENT_STEP' line=$line rc=$rc cmd='$cmd'"
  echo "STEP_FAIL:${CURRENT_STEP}:line=${line}:rc=${rc}:cmd=${cmd}" >> "$SUMMARY"
}
trap 'on_error "$LINENO" "$BASH_COMMAND" "$?"' ERR

log() { echo "$*"; }
mark_done() { touch "${STATE_DIR}/$1.done"; }
is_done() { [[ -f "${STATE_DIR}/$1.done" ]]; }

retry() {
  local tries="${3:-5}" delay="${4:-5}"
  for ((i=1;i<=tries;i++)); do
    if eval "$1"; then return 0; fi
    echo "Retry $i/$tries for: $2"
    sleep "$delay"
  done
  return 1
}

must() {
  local desc="$1"; shift
  CURRENT_STEP="$desc"
  if is_done "$desc"; then
    log "SKIP (done): $desc"; return 0
  fi
  log "== START: $desc =="
  if ( set -e; eval "$@" ); then
    log "== OK: $desc =="
    echo "STEP_OK:${desc}" >> "$SUMMARY"
    mark_done "$desc"
    return 0
  else
    FAILURES=$((FAILURES+1))
    log "== FAIL: $desc =="
    return 1
  fi
}

apt_update() {
  retry "apt-get update -yq" "apt-get update" 6 8
}
apt_install() {
  local pkgs="$*"
  retry "apt-get install -yq --no-install-recommends $pkgs" "install: $pkgs" 6 8
}

# 0) install required library
must "install-basic-utils" '
    apt_update
    apt_install curl
'

# 1) amazon-efs-utils (critical for S3 Files mount)
must "install-efs-utils" '
  curl -fsSL https://amazon-efs-utils.aws.com/efs-utils-installer.sh | sh -s -- --install
'

# 2) S3 Files mount (non-fatal)
must "mount-s3files" '
  mkdir -p /mnt/s3files
  retry "mount -t s3files __S3FILES_FS_ID__ /mnt/s3files" "S3 Files mount" 10 10
  chown ubuntu:ubuntu /mnt/s3files || chmod 777 /mnt/s3files
'

# 3) ROS2 Jazzy
must "install-ros2-jazzy" '
  curl -sSL https://raw.githubusercontent.com/ros/rosdistro/master/ros.key -o /usr/share/keyrings/ros-archive-keyring.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/ros-archive-keyring.gpg] http://packages.ros.org/ros2/ubuntu $(. /etc/os-release && echo $UBUNTU_CODENAME) main" | tee /etc/apt/sources.list.d/ros2.list > /dev/null
  apt_update
  apt_install ros-jazzy-desktop ros-dev-tools
  apt_install ros-jazzy-rosbridge-suite ros-jazzy-rosbridge-server ros-jazzy-topic-tools
  if ! grep -q "source /opt/ros/jazzy/setup.bash" /home/ubuntu/.bashrc; then
    echo "source /opt/ros/jazzy/setup.bash" >> /home/ubuntu/.bashrc
  fi
'



# Final: summary and optional reboot
log "==== SUMMARY (also in $SUMMARY) ===="
cat "$SUMMARY" || true

if [[ $FAILURES -gt 0 ]]; then
  log "One or more critical steps failed ($FAILURES). Not rebooting automatically."
else
  log "All critical steps OK."
fi
