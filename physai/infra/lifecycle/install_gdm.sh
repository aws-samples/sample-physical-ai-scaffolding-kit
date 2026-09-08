#!/bin/bash
# install_gdm.sh — Install GNOME/GDM3 with auto-login on GPU worker nodes.
# DCV console sessions need a real graphical login session to attach to.
# GDM3 boots Xorg (with the NVIDIA driver) under ubuntu's PAM session, and
# dcvsessionlauncher hooks dcvagent into that session at start-up.
#
# NVIDIA userspace is NOT installed here: the HyperPod GPU AMI ships the driver
# via NVIDIA's runfile installer (kernel module + libnvidia-* + nvidia_drv.so all
# at the same version, e.g. 595.91.07). apt's libnvidia-*-<major> debs are (a) a
# different version (e.g. 595.84), which fails Xorg's userspace/kernel version
# check, and (b) file-conflict with the runfile-installed .so files, which fails
# dpkg. So we only install ubuntu-desktop-minimal + gdm3 and rely on the AMI's
# pre-installed NVIDIA Xorg driver.
set -e
# shellcheck source=_lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/_lib.sh"
require_node_type compute

# Skip non-GPU compute nodes. The HyperPod compute AMI can ship `nvidia-smi`
# on CPU-only instances too, so binary presence alone is not sufficient — we
# must confirm at least one GPU is actually enumerable.
if ! command -v nvidia-smi >/dev/null 2>&1; then
    echo "nvidia-smi not present — not a GPU node, skipping GDM install"
    exit 0
fi
if ! nvidia-smi -L 2>/dev/null | grep -q '^GPU '; then
    echo "nvidia-smi -L reports no GPUs — not a GPU node, skipping GDM install"
    exit 0
fi

DRIVER_VERSION=$(grep "NVRM version" /proc/driver/nvidia/version | grep -oP '\d+\.\d+\.\d+' | head -1)
echo "NVIDIA kernel module version: ${DRIVER_VERSION:-unknown} (using AMI-provided userspace)"

# Sanity check: the runfile installer should have placed these on the AMI. If
# they are missing, the AMI's driver provisioning is broken and Xorg will not
# start regardless of what we do here — surface it early.
for f in \
    /usr/lib/x86_64-linux-gnu/nvidia/xorg/nvidia_drv.so \
    /usr/lib/x86_64-linux-gnu/nvidia/xorg/libglxserver_nvidia.so; do
    if [[ ! -e "$f" ]]; then
        echo "WARNING: $f not found — expected from HyperPod AMI's runfile installer"
    fi
done

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq

# Only the desktop shell — no NVIDIA packages (see file header for why).
apt-get install -y -qq --no-install-recommends \
  ubuntu-desktop-minimal \
  gdm3

# NVIDIA Xorg modules live under /usr/lib/x86_64-linux-gnu/nvidia/xorg/ on
# Ubuntu; OutputClass in /usr/share/X11/xorg.conf.d/10-nvidia.conf normally
# picks them up, but symlink anyway as belt-and-braces for environments where
# the OutputClass file is missing.
mkdir -p /usr/lib/xorg/modules/drivers /usr/lib/xorg/modules/extensions
ln -sf /usr/lib/x86_64-linux-gnu/nvidia/xorg/nvidia_drv.so \
       /usr/lib/xorg/modules/drivers/nvidia_drv.so
ln -sf /usr/lib/x86_64-linux-gnu/nvidia/xorg/libglxserver_nvidia.so \
       /usr/lib/xorg/modules/extensions/libglxserver_nvidia.so

# nvidia-persistenced is optional. HyperPod DLAMIs typically ship the unit via
# the runfile installer; if it is absent we do not fail — the driver still
# works, we just lose the "keep GPU state warm across process boundaries"
# optimisation, which is not required for the Xorg-based DCV path.
if systemctl list-unit-files nvidia-persistenced.service >/dev/null 2>&1 \
    && systemctl list-unit-files nvidia-persistenced.service | grep -q '^nvidia-persistenced\.service'; then
    systemctl enable --now nvidia-persistenced
else
    echo "nvidia-persistenced.service not present on this AMI — skipping enable"
fi

# Generate xorg.conf via nvidia-xconfig — the supported recipe for headless
# data-center GPUs (a virtual DFP display head instead of a physical monitor)
# per AWS DCV TAM runbook. --preserve-busid pins the BusID to the detected
# GPU so the driver binds correctly across reboots.
nvidia-xconfig \
  --preserve-busid \
  --enable-all-gpus \
  --connected-monitor=DFP-0

# Auto-login ubuntu so GDM brings up Xorg + a graphical session at boot,
# without a human at the (non-existent) console. dcvsessionlauncher hooks
# dcvagent into that session for frame capture.
mkdir -p /etc/gdm3
cat > /etc/gdm3/custom.conf << 'EOF'
[daemon]
WaylandEnable = false
AutomaticLoginEnable = true
AutomaticLogin = ubuntu

[security]

[xdmcp]

[chooser]

[debug]
EOF

# Disable GNOME screen lock and idle blanking system-wide. Without this the
# auto-logged-in ubuntu desktop locks itself after a few minutes of no input
# and DCV viewers get stuck on the lock screen — there is no human at the
# console to type ubuntu's PAM password (which is the per-job OTP, already
# rotated out by then anyway).
mkdir -p /etc/dconf/db/local.d /etc/dconf/profile
cat > /etc/dconf/profile/user << 'EOF'
user-db:user
system-db:local
EOF
cat > /etc/dconf/db/local.d/00-physai-no-lock << 'EOF'
[org/gnome/desktop/screensaver]
lock-enabled=false
idle-activation-enabled=false

[org/gnome/desktop/session]
idle-delay=uint32 0

[org/gnome/settings-daemon/plugins/power]
sleep-inactive-ac-type='nothing'
sleep-inactive-battery-type='nothing'
EOF
dconf update

# Tear down the obsolete raw-Xorg unit if we're upgrading from a node that
# was previously bootstrapped with install_xorg.sh.
if [[ -f /etc/systemd/system/xorg.service ]]; then
    systemctl disable --now xorg.service 2>/dev/null || true
    rm -f /etc/systemd/system/xorg.service
    systemctl daemon-reload
fi

# nvidia-xconfig leaves /etc/X11/xorg.conf.{backup,nvidia-xconfig-original}
# on its first run. Drop them so a freshly-bootstrapped node and a migrated
# node end up with identical /etc/X11/ contents.
rm -f /etc/X11/xorg.conf.backup /etc/X11/xorg.conf.nvidia-xconfig-original

# Boot to graphical.target so GDM starts on every boot.
systemctl set-default graphical.target

systemctl enable gdm3
# Restart unconditionally so dconf/custom.conf changes take effect on re-runs.
# Lifecycle scripts are a planned maintenance operation — operators accept
# that this kills any in-progress visual eval session.
systemctl restart gdm3

sleep 3
if systemctl is-active --quiet gdm3 && ls /tmp/.X11-unix/X* >/dev/null 2>&1; then
    echo "GDM3 running, X display socket present: $(ls /tmp/.X11-unix/)"
else
    echo "WARNING: GDM3 or Xorg not up yet; check 'journalctl -u gdm3'"
fi
