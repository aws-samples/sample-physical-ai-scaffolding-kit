#!/bin/bash
# Install Docker + NVIDIA Container Toolkit. Runs on all node types.
#
# Idempotent: each step checks whether its effect is already in place and
# skips the work if so. Safe to re-run. Restarts docker/containerd only if
# something actually changed.
set -exo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"

# Suppress debconf "unable to initialize frontend: Dialog/Readline/Teletype"
# warnings during apt installs run from non-interactive contexts (lifecycle).
export DEBIAN_FRONTEND=noninteractive
APT_OPTS=(-y -o DPkg::Lock::Timeout=120)
DOCKER_CHANGED=false
CONTAINERD_CHANGED=false

# ---------- Docker apt repo ----------
if [[ ! -s /etc/apt/keyrings/docker.gpg ]] || [[ ! -s /etc/apt/sources.list.d/docker.list ]]; then
    apt-get "${APT_OPTS[@]}" update
    apt-get "${APT_OPTS[@]}" install ca-certificates curl gnupg lsb-release
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --yes --dearmor -o /etc/apt/keyrings/docker.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
        > /etc/apt/sources.list.d/docker.list
    apt-get "${APT_OPTS[@]}" update
fi

# ---------- Docker packages ----------
# Install anything missing from the set. `dpkg -s` returns non-zero if the
# package isn't installed; if any are missing, we apt-install them all
# (apt is idempotent on already-installed packages so this is safe).
if ! dpkg -s docker-ce docker-ce-cli containerd.io docker-buildx-plugin >/dev/null 2>&1; then
    apt-get "${APT_OPTS[@]}" install docker-ce docker-ce-cli containerd.io docker-buildx-plugin
    DOCKER_CHANGED=true
    CONTAINERD_CHANGED=true
fi

# ---------- Docker group + ubuntu membership ----------
# groupadd -f is idempotent; usermod -aG is too, but we still flag CHANGED
# so the daemon picks up the group on restart if we had to create it.
if ! getent group docker >/dev/null; then
    groupadd docker
    DOCKER_CHANGED=true
fi
if ! id -nG ubuntu 2>/dev/null | grep -qw docker; then
    usermod -aG docker ubuntu
    # Note: existing sessions don't pick up new group membership until
    # re-login; no service restart needed for this.
fi

# ---------- NVIDIA Container Toolkit apt repo ----------
if [[ ! -s /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg ]] \
   || [[ ! -s /etc/apt/sources.list.d/nvidia-container-toolkit.list ]]; then
    curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
        | gpg --yes --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
    curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
        | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
        > /etc/apt/sources.list.d/nvidia-container-toolkit.list
    apt-get "${APT_OPTS[@]}" update
fi

# ---------- NVIDIA Container Toolkit package ----------
if ! dpkg -s nvidia-container-toolkit >/dev/null 2>&1; then
    apt-get "${APT_OPTS[@]}" install nvidia-container-toolkit
    DOCKER_CHANGED=true
fi

# ---------- NVMe storage root (if disk is attached) ----------
# Docker Engine 29+ uses containerd image store by default. data-root only
# controls Docker's own metadata — image layers are stored by containerd.
# Both must be configured separately.
if [[ -d /opt/dlami/nvme ]]; then
    STORAGE_ROOT="/opt/dlami/nvme/docker"

    # docker data-root (volumes, configs, etc.)
    NEW_DAEMON_JSON="{\"data-root\": \"$STORAGE_ROOT/data-root\"}"
    if [[ ! -f /etc/docker/daemon.json ]] \
       || [[ "$(cat /etc/docker/daemon.json)" != "$NEW_DAEMON_JSON" ]]; then
        mkdir -p /etc/docker "$STORAGE_ROOT/data-root"
        echo "$NEW_DAEMON_JSON" > /etc/docker/daemon.json
        DOCKER_CHANGED=true
    fi

    # containerd root (image layers, snapshots)
    if [[ -f /etc/containerd/config.toml ]] \
       && ! grep -qE "^root *= *\"$STORAGE_ROOT/containerd\"$" /etc/containerd/config.toml; then
        mkdir -p "$STORAGE_ROOT/containerd"
        sed -i -e "s|^#\?root *=.*|root = \"$STORAGE_ROOT/containerd\"|" /etc/containerd/config.toml
        CONTAINERD_CHANGED=true
    fi
fi

# ---------- Apply restarts only if something changed ----------
if $DOCKER_CHANGED || $CONTAINERD_CHANGED; then
    systemctl daemon-reload
fi
if $CONTAINERD_CHANGED; then
    systemctl restart containerd
fi
if $DOCKER_CHANGED; then
    systemctl restart docker
fi

if $DOCKER_CHANGED || $CONTAINERD_CHANGED; then
    echo "Docker installed/updated"
else
    echo "Docker already up-to-date, nothing to do"
fi
