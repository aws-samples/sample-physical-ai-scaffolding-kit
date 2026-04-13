#!/bin/bash
# Mount FSx for Lustre at the specified mount point.
# Usage: mount_fsx.sh <fsx_dns_name> <fsx_mountname> <mount_point>
set -eux

FSX_DNS="$1"
FSX_MOUNT="$2"
MOUNT_POINT="$3"

# Load Lustre kernel modules
modprobe lnet
modprobe lustre
lctl network up

# Create mount point
mkdir -p "$MOUNT_POINT"

# Mount with retry
for attempt in $(seq 1 5); do
  if mount -t lustre -o noatime,flock "${FSX_DNS}@tcp:/${FSX_MOUNT}" "$MOUNT_POINT"; then
    echo "FSx mounted at $MOUNT_POINT"
    break
  fi
  echo "Mount attempt $attempt failed, retrying in 5s..."
  sleep 5
done

# Verify
mountpoint "$MOUNT_POINT"
touch "$MOUNT_POINT/.mount_test_$(hostname)" && rm "$MOUNT_POINT/.mount_test_$(hostname)"

# Add to fstab for persistence
if ! grep -q "$FSX_DNS" /etc/fstab; then
  echo "${FSX_DNS}@tcp:/${FSX_MOUNT} ${MOUNT_POINT} lustre noatime,flock,_netdev,x-systemd.automount,x-systemd.requires=network-online.target 0 0" >> /etc/fstab
fi

# Tell systemd about the new fstab entry and activate the automount unit
systemctl daemon-reload
systemctl restart remote-fs.target

# Verify
systemctl status "$(systemd-escape -p --suffix=automount "${MOUNT_POINT}")" || true
mountpoint -q "$MOUNT_POINT" && echo "FSx mount verified: $MOUNT_POINT" || { echo "ERROR: $MOUNT_POINT not mounted"; exit 1; }
