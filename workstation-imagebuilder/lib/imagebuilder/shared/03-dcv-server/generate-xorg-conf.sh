#!/bin/bash
BUSID=$(nvidia-smi --query-gpu=pci.bus_id --format=csv,noheader | head -1 | sed 's/00000000://' | awk -F'[:\\.]' '{printf "%d:%d:%d", strtonum("0x"$1), strtonum("0x"$2), $3}')
if [ -z "$BUSID" ]; then
  exit 1
fi
cat > /etc/X11/xorg.conf <<XORGEOF
Section "ServerLayout"
    Identifier "Layout0"
    Screen 0 "Screen0"
EndSection

Section "Device"
    Identifier "Device0"
    Driver "nvidia"
    BusID "PCI:${BUSID}"
    Option "AllowEmptyInitialConfiguration" "true"
    Option "ConnectedMonitor" "DFP-0"
EndSection

Section "Screen"
    Identifier "Screen0"
    Device "Device0"
    DefaultDepth 24
    SubSection "Display"
        Depth 24
        Virtual 3840 2160
    EndSubSection
EndSection
XORGEOF
