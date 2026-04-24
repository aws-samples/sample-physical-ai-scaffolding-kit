#! /usr/bin/env bash

# Mount graphics config files into the container when graphics or display
# capabilities are requested. This fills a gap where nvidia-container-cli
# (called by 98-nvidia.sh) mounts the Vulkan/EGL libraries but not the
# ICD/config JSON manifests that the nvidia-container-toolkit normally
# provides for Docker.

set -euo pipefail
shopt -s lastpipe

tac "${ENROOT_ENVIRON}" | grep "^NVIDIA_" | while IFS='=' read -r key value; do
    [ -v "${key}" ] || export "${key}=${value}"
done || :

if [ "${NVIDIA_VISIBLE_DEVICES:-void}" = "void" ]; then
    exit 0
fi

needs_graphics=false

if [ -z "${NVIDIA_DRIVER_CAPABILITIES-}" ]; then
    NVIDIA_DRIVER_CAPABILITIES="utility"
fi
for cap in ${NVIDIA_DRIVER_CAPABILITIES//,/ }; do
    case "${cap}" in
    all|graphics|display)
        needs_graphics=true
        break
        ;;
    esac
done

if ! "${needs_graphics}"; then
    exit 0
fi

# Helper: bind-mount a file if it exists on the host.
mount_file() {
    local src="$1" dst="${2:-$1}"
    if [ -f "${src}" ]; then
        enroot-mount --root "${ENROOT_ROOTFS}" - <<< "${src} ${dst} none x-create=file,bind,ro,nosuid,noexec,private,nofail,silent"
    fi
}

# Vulkan ICD and layer configs
mount_file /etc/vulkan/icd.d/nvidia_icd.json
mount_file /etc/vulkan/implicit_layer.d/nvidia_layers.json

# EGL and GLVND configs
mount_file /usr/share/glvnd/egl_vendor.d/10_nvidia.json
mount_file /usr/share/egl/egl_external_platform.d/10_nvidia_wayland.json
mount_file /usr/share/egl/egl_external_platform.d/15_nvidia_gbm.json
