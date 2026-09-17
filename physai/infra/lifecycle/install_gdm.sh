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

# Separate "no NVIDIA hardware" from "NVIDIA hardware but no working driver".
# The HyperPod compute AMI can ship the nvidia-smi binary even on CPU-only
# instances, so binary presence alone is not sufficient. Use lspci for the
# hardware check (works even when the kernel driver isn't loaded) and
# nvidia-smi -L to confirm the driver actually enumerates the GPUs.
if ! command -v lspci >/dev/null 2>&1 || ! lspci 2>/dev/null | grep -qi 'nvidia'; then
    echo "No NVIDIA GPU hardware detected (lspci) — skipping GDM install"
    exit 0
fi
if ! command -v nvidia-smi >/dev/null 2>&1 \
   || ! nvidia-smi -L 2>/dev/null | grep -q '^GPU '; then
    echo "ERROR: NVIDIA GPU hardware present but nvidia-smi cannot enumerate it." >&2
    echo "       The kernel driver is not loaded or is malfunctioning; the AMI's" >&2
    echo "       NVIDIA runfile installation is broken. Fix the AMI before rerunning." >&2
    exit 1
fi

DRIVER_VERSION=$(grep "NVRM version" /proc/driver/nvidia/version | grep -oP '\d+\.\d+\.\d+' | head -1)
DRIVER_MAJOR="${DRIVER_VERSION%%.*}"
echo "NVIDIA kernel module version: ${DRIVER_VERSION:-unknown} (using AMI-provided userspace)"

# Cleanup: purge apt-installed NVIDIA display-driver userspace debs whose
# version does not match the runfile-installed kernel driver. Prior versions
# of this script attempted to install these on runfile-based AMIs, where they
# end up as stale packages that conflict with the runfile's .so files.
#
# Scope carefully: match ONLY packages whose name ends with the driver major
# (e.g. libnvidia-common-595, libnvidia-compute-595, xserver-xorg-video-nvidia-595).
# Do NOT touch libnvidia-container-tools / libnvidia-container1 — those are the
# NVIDIA Container Toolkit CLI that enroot's 98-nvidia.sh hook depends on;
# their versioning (e.g. 1.17.0-1) has nothing to do with the display driver
# version and blindly matching `libnvidia-*` would purge them.
if [[ -n "$DRIVER_VERSION" && -n "$DRIVER_MAJOR" ]]; then
    STALE=$(dpkg-query -W -f='${Package} ${Version}\n' \
                'libnvidia-*' 'xserver-xorg-video-nvidia-*' 2>/dev/null \
                | awk -v v="$DRIVER_VERSION" -v m="$DRIVER_MAJOR" '
                    $1 ~ ("-" m "$") && $2 !~ ("^" v) { print $1 }
                  ')
    if [[ -n "$STALE" ]]; then
        echo "Purging stale NVIDIA display-driver apt packages (not matching runfile driver $DRIVER_VERSION):"
        # shellcheck disable=SC2086
        printf '  %s\n' $STALE
        export DEBIAN_FRONTEND=noninteractive
        # shellcheck disable=SC2086
        apt-get -y purge $STALE || true
    fi
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq

# Only the desktop shell — no NVIDIA packages (see file header for why).
apt-get install -y -qq --no-install-recommends \
  ubuntu-desktop-minimal \
  gdm3

# NVIDIA Xorg modules live under /usr/lib/x86_64-linux-gnu/nvidia/xorg/ on
# apt-managed Ubuntu installations; symlink into /usr/lib/xorg/modules/ as
# belt-and-braces for environments where /usr/share/X11/xorg.conf.d/10-nvidia.conf
# OutputClass file is missing. The HyperPod runfile installer instead writes
# nvidia_drv.so / libglxserver_nvidia.so directly under /usr/lib/xorg/modules/,
# so if the apt-style source path is absent we deliberately skip — creating a
# dangling symlink here would clobber the real runfile-installed file and
# prevent Xorg from loading the nvidia module.
mkdir -p /usr/lib/xorg/modules/drivers /usr/lib/xorg/modules/extensions
for pair in \
    "/usr/lib/x86_64-linux-gnu/nvidia/xorg/nvidia_drv.so:/usr/lib/xorg/modules/drivers/nvidia_drv.so" \
    "/usr/lib/x86_64-linux-gnu/nvidia/xorg/libglxserver_nvidia.so:/usr/lib/xorg/modules/extensions/libglxserver_nvidia.so"; do
    src="${pair%%:*}"
    dst="${pair##*:}"
    if [[ -e "$src" ]]; then
        ln -sf "$src" "$dst"
    else
        echo "Skipping symlink: $src not present (AMI provides driver at $dst directly)"
    fi
done

# nvidia-persistenced is optional. HyperPod DLAMIs typically ship the unit via
# the runfile installer; if it is absent we do not fail — the driver still
# works, we just lose the "keep GPU state warm across process boundaries"
# optimisation, which is not required for the Xorg-based DCV path.
# (systemctl list-unit-files exits 0 even when nothing matches, so the grep
# check alone is authoritative.)
if systemctl list-unit-files nvidia-persistenced.service 2>/dev/null \
    | grep -q '^nvidia-persistenced\.service'; then
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
