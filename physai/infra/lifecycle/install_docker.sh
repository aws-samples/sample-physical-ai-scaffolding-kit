#!/bin/bash
# Install Docker + NVIDIA Container Toolkit.
set -exo pipefail

if command -v docker &>/dev/null; then
  echo "Docker already installed, skipping."
  exit 0
fi

# Install Docker
apt-get -y -o DPkg::Lock::Timeout=120 update
apt-get -y -o DPkg::Lock::Timeout=120 install ca-certificates curl gnupg lsb-release
mkdir -m 0755 -p /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get -y -o DPkg::Lock::Timeout=120 update
apt-get -y -o DPkg::Lock::Timeout=120 install docker-ce docker-ce-cli containerd.io docker-buildx-plugin

# Docker group permissions
groupadd -f docker
usermod -aG docker ubuntu

# NVIDIA Container Toolkit
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | gpg --yes --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  > /etc/apt/sources.list.d/nvidia-container-toolkit.list
apt-get -y -o DPkg::Lock::Timeout=120 update
apt-get -y -o DPkg::Lock::Timeout=120 install nvidia-container-toolkit

# Use NVMe for Docker + containerd data if available (avoids filling 100GB root volume)
# Docker Engine 29+ uses containerd image store by default. data-root only controls Docker's
# own metadata — image layers are stored by containerd. Both must be configured separately.
if [[ -d /opt/dlami/nvme ]]; then
  STORAGE_ROOT="/opt/dlami/nvme/docker"
else
  STORAGE_ROOT=""
fi

if [[ -n "$STORAGE_ROOT" ]]; then
  # Docker data-root (volumes, configs, etc.)
  mkdir -p "$STORAGE_ROOT/data-root"
  cat > /etc/docker/daemon.json <<EOF
{"data-root": "$STORAGE_ROOT/data-root"}
EOF

  # Containerd root (image layers, snapshots)
  mkdir -p "$STORAGE_ROOT/containerd"
  sed -i -e "s|^#\?root *=.*|root = \"$STORAGE_ROOT/containerd\"|" /etc/containerd/config.toml
  systemctl restart containerd
fi

systemctl daemon-reload
systemctl restart docker
echo "Docker installed"
