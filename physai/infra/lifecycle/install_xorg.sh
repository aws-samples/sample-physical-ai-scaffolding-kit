#!/bin/bash
# install_xorg.sh — Install and start Xorg with NVIDIA driver on GPU worker nodes.
# Required for IsaacSim headless rendering (needs GLFW/GLX even in headless mode).
#
# Must pin xserver-xorg-video-nvidia to match the kernel module version to avoid
# API mismatch. The kernel module version comes from the HyperPod AMI.
set -e

# Get kernel module version
DRIVER_VERSION=$(cat /proc/driver/nvidia/version | grep "NVRM version" | grep -oP '\d+\.\d+\.\d+' | head -1)
if [[ -z "$DRIVER_VERSION" ]]; then
  echo "WARNING: Could not detect NVIDIA driver version, skipping Xorg install"
  exit 0
fi
echo "NVIDIA kernel module version: $DRIVER_VERSION"

# Extract major version (e.g., 580 from 580.126.09)
MAJOR=$(echo "$DRIVER_VERSION" | cut -d. -f1)

# Install Xorg + matching NVIDIA Xorg driver
# Must pin ALL nvidia packages to exact version to avoid API mismatch.
# --allow-downgrades needed because apt may try to resolve to a newer version.
V="${DRIVER_VERSION}-1ubuntu1"
apt-get update -qq
apt-get install -y --allow-downgrades --no-install-recommends \
  xserver-xorg-core \
  "xserver-xorg-video-nvidia-${MAJOR}=${V}" \
  "nvidia-persistenced=${V}" \
  "libnvidia-cfg1-${MAJOR}=${V}" \
  "libnvidia-common-${MAJOR}=${V}" \
  "libnvidia-compute-${MAJOR}=${V}" \
  "libnvidia-decode-${MAJOR}=${V}" \
  "libnvidia-gl-${MAJOR}=${V}" \
  "libnvidia-gpucomp-${MAJOR}=${V}"

# Symlink NVIDIA Xorg modules to where Xorg expects them
mkdir -p /usr/lib/xorg/modules/drivers /usr/lib/xorg/modules/extensions
ln -sf /usr/lib/x86_64-linux-gnu/nvidia/xorg/nvidia_drv.so /usr/lib/xorg/modules/drivers/nvidia_drv.so
ln -sf /usr/lib/x86_64-linux-gnu/nvidia/xorg/libglxserver_nvidia.so /usr/lib/xorg/modules/extensions/libglxserver_nvidia.so

# Start nvidia-persistenced
systemctl enable --now nvidia-persistenced

# Generate xorg.conf with correct BusID
BUSID_HEX=$(nvidia-smi --query-gpu=pci.bus_id --format=csv,noheader | head -1)
# Format: 00000000:BUS:DEVICE.FUNCTION -> PCI:bus:device:function (decimal)
BUS=$(echo "$BUSID_HEX" | cut -d: -f2)
DEV_FUNC=$(echo "$BUSID_HEX" | cut -d: -f3)
DEV=$(echo "$DEV_FUNC" | cut -d. -f1)
FUNC=$(echo "$DEV_FUNC" | cut -d. -f2)
BUS_DEC=$((16#$BUS))
DEV_DEC=$((16#$DEV))
FUNC_DEC=$((16#$FUNC))

cat > /etc/X11/xorg.conf << EOF
Section "ServerLayout"
    Identifier     "Layout0"
    Screen      0  "Screen0"
EndSection

Section "Device"
    Identifier     "Device0"
    Driver         "nvidia"
    BusID          "PCI:${BUS_DEC}:${DEV_DEC}:${FUNC_DEC}"
EndSection

Section "Screen"
    Identifier     "Screen0"
    Device         "Device0"
    DefaultDepth    24
    Option         "ConnectedMonitor" "DFP-0,DFP-1,DFP-2,DFP-3"
    SubSection     "Display"
        Depth       24
    EndSubSection
EndSection
EOF

# Start Xorg on display :0 via systemd
cat > /etc/systemd/system/xorg.service << EOF
[Unit]
Description=Xorg display server
After=nvidia-persistenced.service

[Service]
ExecStart=/usr/lib/xorg/Xorg :0
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now xorg.service

if systemctl is-active --quiet xorg.service; then
  echo "Xorg started on :0"
else
  echo "WARNING: Xorg failed to start, check journalctl -u xorg.service"
fi
