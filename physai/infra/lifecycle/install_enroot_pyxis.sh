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

# Configure Pyxis plugin for Slurm.
# Only meaningful on the controller: HyperPod uses Slurm configless mode, where
# workers fetch plugstack.conf from the controller and cache it under
# /var/spool/slurmd/conf-cache/. Modifying a worker's $SLURM_DIR/etc has no
# runtime effect. The controller writes the authoritative copy and pushes it to
# workers via `scontrol reconfigure`.
#
# The conventional pattern is to `include $SLURM_DIR/etc/plugstack.conf.d/*.conf`
# from plugstack.conf so each plugin ships its own drop-in file — useful when
# multiple packages (Pyxis, MPI wrappers, site plugins) manage independent
# configs. We don't use it here because configless only pushes plugstack.conf
# itself, not the files it includes. The include path (`$SLURM_DIR/etc/...`)
# must exist on every node, but workers don't have it — so Pyxis silently fails
# to load. Inlining the `required` line in plugstack.conf keeps the whole config
# self-contained and correctly propagated to workers.
if [[ "$NODE_TYPE" == "controller" ]]; then
    PYXIS_LINE="required /usr/local/lib/slurm/spank_pyxis.so use_enroot_load=1"
    # Remove any existing spank_pyxis entry (regardless of path or flags) so we
    # don't accumulate duplicates or stale lines with different options. Then
    # append the canonical line. Skip the subsequent reconfigure if the file
    # already matched.
    before=$(md5sum "$SLURM_DIR/etc/plugstack.conf" | cut -d' ' -f1)
    sed -i '/spank_pyxis\.so/d' "$SLURM_DIR/etc/plugstack.conf"
    # Ensure trailing newline before appending (HyperPod's plugstack.conf may lack one)
    [[ -s "$SLURM_DIR/etc/plugstack.conf" && -n "$(tail -c 1 "$SLURM_DIR/etc/plugstack.conf")" ]] && echo "" >> "$SLURM_DIR/etc/plugstack.conf"
    echo "$PYXIS_LINE" >> "$SLURM_DIR/etc/plugstack.conf"
    after=$(md5sum "$SLURM_DIR/etc/plugstack.conf" | cut -d' ' -f1)

    # GPU cgroup constraint (required for correct GPU ID mapping with Pyxis).
    # cgroup.conf is also pushed to workers via configless (like slurm.conf and
    # plugstack.conf), so editing it only on the controller is sufficient.
    cgroup_changed=false
    if [[ -f "$SLURM_DIR/etc/cgroup.conf" ]] && ! grep -q "^ConstrainDevices" "$SLURM_DIR/etc/cgroup.conf"; then
      echo "ConstrainDevices=yes" >> "$SLURM_DIR/etc/cgroup.conf"
      cgroup_changed=true
    fi

    # Push updated plugstack to workers via the configless mechanism only if
    # something actually changed. slurmctld is already running so a single
    # reconfigure is sufficient.
    if [[ "$before" != "$after" ]] || $cgroup_changed; then
        scontrol reconfigure
    fi
else
    # On workers (compute/login), restart slurmd so it re-fetches the latest
    # config from the controller. Together with the controller's `scontrol
    # reconfigure` above, this covers both orderings:
    #   - Controller writes pyxis BEFORE the worker restarts here:
    #     the restart fetches the pyxis-included config.
    #   - Controller writes pyxis AFTER the worker restarts here:
    #     the worker is already registered, so the controller's reconfigure
    #     pushes the pyxis-included config to it.
    # Skip the restart if the cached config already has pyxis.
    if systemctl is-active --quiet slurmd \
       && ! grep -q 'spank_pyxis' /var/spool/slurmd/conf-cache/plugstack.conf 2>/dev/null; then
        systemctl restart slurmd
    fi
fi

echo "Enroot + Pyxis installed"
