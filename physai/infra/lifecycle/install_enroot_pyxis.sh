#!/bin/bash
# Install Enroot + Pyxis for Slurm container support.
# Requires Docker to be installed first.
# Usage: install_enroot_pyxis.sh <node_type>
set -ex

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NODE_TYPE="$1"
# ENROOT_VERSION=3.4.1
ENROOT_VERSION=4.1.2
# PYXIS_VERSION=v0.19.0
PYXIS_VERSION=v0.23.0
ARCH=$(dpkg --print-architecture)
SLURM_DIR="${SLURM_DIR:-/opt/slurm}"

# Enroot dependencies
apt-get -y -o DPkg::Lock::Timeout=120 install squashfs-tools parallel fuse-overlayfs squashfuse

# Install Enroot (skip if already installed at correct version)
if ! enroot version 2>/dev/null | grep -q "^${ENROOT_VERSION}$"; then
    cd /tmp
    curl -fSsL -O "https://github.com/NVIDIA/enroot/releases/download/v${ENROOT_VERSION}/enroot_${ENROOT_VERSION}-1_${ARCH}.deb"
    curl -fSsL -O "https://github.com/NVIDIA/enroot/releases/download/v${ENROOT_VERSION}/enroot+caps_${ENROOT_VERSION}-1_${ARCH}.deb"
    apt-get -y -o DPkg::Lock::Timeout=120 install "./enroot_${ENROOT_VERSION}-1_${ARCH}.deb"
    apt-get -y -o DPkg::Lock::Timeout=120 install "./enroot+caps_${ENROOT_VERSION}-1_${ARCH}.deb"
    rm -f enroot_*.deb enroot+caps_*.deb
else
    echo "Enroot ${ENROOT_VERSION} already installed, skipping"
fi

# Configure Enroot paths — use NVMe for runtime, /fsx for shared cache
ENROOT_RUNTIME_PATH_BASE=/tmp/enroot
ENROOT_DATA_PATH_BASE=/tmp/enroot/data
ENROOT_CACHE_PATH=/tmp/enroot
ENROOT_TEMP_PATH=/tmp

# Use /opt/dlami/nvme if mounted
if [[ -d /opt/dlami/nvme ]]; then
    ENROOT_RUNTIME_PATH_BASE=/opt/dlami/nvme/tmp/enroot
    ENROOT_DATA_PATH_BASE=/opt/dlami/nvme/tmp/enroot/data
    ENROOT_CACHE_PATH=/opt/dlami/nvme/enroot
    ENROOT_TEMP_PATH=/opt/dlami/nvme/tmp
    
    mkdir -p /opt/dlami/nvme/tmp/enroot/data /opt/dlami/nvme/enroot
    chmod 1777 /opt/dlami/nvme/tmp /opt/dlami/nvme/tmp/enroot /opt/dlami/nvme/tmp/enroot/data /opt/dlami/nvme/enroot
fi

# Use /fsx for enroot cache if mounted (shared across nodes)
if mountpoint -q /fsx 2>/dev/null; then
    ENROOT_CACHE_PATH=/fsx/enroot
    mkdir -p /fsx/enroot
    chmod 1777 /fsx/enroot
fi

cat > /etc/enroot/enroot.conf <<EOF
ENROOT_RUNTIME_PATH ${ENROOT_RUNTIME_PATH_BASE}\$(id -u)
ENROOT_DATA_PATH    ${ENROOT_DATA_PATH_BASE}/user-\$(id -u)
ENROOT_CACHE_PATH   ${ENROOT_CACHE_PATH}
ENROOT_TEMP_PATH    ${ENROOT_TEMP_PATH}
ENROOT_SQUASH_OPTIONS -comp zstd -Xcompression-level 3 -b 1M -exit-on-error
ENROOT_MOUNT_HOME n
ENROOT_RESTRICT_DEV y
ENROOT_ROOTFS_WRITABLE y
EOF

# Fix enroot hooks for Nvidia
# Mount ICD file
cp "$SCRIPT_DIR/97-vulkan-icd.sh" /etc/enroot/hooks.d/
chmod +x /etc/enroot/hooks.d/97-vulkan-icd.sh

# Patch 98-nvidia.sh for NGX support (idempotent)
if patch --reverse --dry-run /etc/enroot/hooks.d/98-nvidia.sh "$SCRIPT_DIR/98-nvidia-add-ngx-capability.patch" &>/dev/null; then
    echo "98-nvidia.sh patch already applied, skipping"
else
    patch --forward /etc/enroot/hooks.d/98-nvidia.sh "$SCRIPT_DIR/98-nvidia-add-ngx-capability.patch"
fi

# Install Pyxis (Slurm plugin for container support) — skip if already built at correct version
PYXIS_MARKER="$SLURM_DIR/pyxis/.version-${PYXIS_VERSION}"
if [[ ! -f "$PYXIS_MARKER" ]]; then
    rm -rf "$SLURM_DIR/pyxis"
    mkdir -p "$SLURM_DIR/pyxis"
    git clone --depth 1 --branch "$PYXIS_VERSION" https://github.com/NVIDIA/pyxis.git "$SLURM_DIR/pyxis"
    cd "$SLURM_DIR/pyxis"
    CPPFLAGS="-I $SLURM_DIR/include/" make -j "$(nproc)"
    CPPFLAGS="-I $SLURM_DIR/include/" make install
    touch "$PYXIS_MARKER"
else
    echo "Pyxis ${PYXIS_VERSION} already installed, skipping"
fi

# Configure Pyxis plugin for Slurm
mkdir -p "$SLURM_DIR/etc/plugstack.conf.d"
if ! grep -q "plugstack.conf.d/pyxis.conf" "$SLURM_DIR/etc/plugstack.conf" 2>/dev/null; then
  echo "include $SLURM_DIR/etc/plugstack.conf.d/pyxis.conf" >> "$SLURM_DIR/etc/plugstack.conf"
fi
cat > "$SLURM_DIR/etc/plugstack.conf.d/pyxis.conf" <<EOF
required /usr/local/lib/slurm/spank_pyxis.so use_enroot_load=1
EOF

# GPU cgroup constraint (required for correct GPU ID mapping with Pyxis)
if [[ -f "$SLURM_DIR/etc/cgroup.conf" ]]; then
  grep -q "^ConstrainDevices" "$SLURM_DIR/etc/cgroup.conf" || echo "ConstrainDevices=yes" >> "$SLURM_DIR/etc/cgroup.conf"
fi

# Restart Slurm to pick up Pyxis plugin
systemctl is-active --quiet slurmctld && systemctl restart slurmctld || true
systemctl is-active --quiet slurmd && systemctl restart slurmd || true

echo "Enroot + Pyxis installed"
