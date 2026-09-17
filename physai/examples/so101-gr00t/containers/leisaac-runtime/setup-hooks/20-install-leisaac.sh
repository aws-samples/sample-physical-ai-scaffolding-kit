#!/bin/bash
# Install LeIsaac, IsaacSim (pip), and IsaacLab.
set -euo pipefail
export TERM=xterm

uv python install 3.12

git clone https://github.com/LightwheelAI/leisaac.git "$LEISAAC_DIR"
cd "$LEISAAC_DIR"
git checkout "$LEISAAC_REF"
git submodule update --init --recursive

# IsaacSim 6.0.0 dropped `omni.kit.property.physx` (a Kit UI property panel).
# LeIsaac's pinned IsaacLab 2.3.0 still declares it as a dependency in the
# rendering kit app, which fails dependency resolution on startup.
# IsaacLab 3.0.0 removes this line upstream; drop it in place so eval works.
sed -i '/"omni.kit.property.physx"/d' \
  dependencies/IsaacLab/apps/isaaclab.python.rendering.kit

uv venv --python 3.12 .venv
uv pip install pip

source .venv/bin/activate
# Ephemeral disk (/tmp/enroot/data on the compute node's root fs) is tight;
# the enroot data root is ~50 GB and this install alone consumes ~30 GB before
# pin/libpinocchio upgrade. Use --no-cache-dir throughout and purge the pip
# cache between the big installs so wheels aren't held twice on disk.
pip install --no-cache-dir torch==2.7.0 torchvision==0.22.0 --index-url https://download.pytorch.org/whl/cu128
pip cache purge || true
# IsaacSim 6.0.0.0 (Python 3.12) — required for compatibility with NVIDIA
# driver 595 series. IsaacSim 5.1.0 (Python 3.11) SEGVs in
# librtx.scenedb.plugin.so at carbOnPluginStartup on driver 595+.
pip install --no-cache-dir 'isaacsim[all,extscache]==6.0.0.0' --extra-index-url https://pypi.nvidia.com
pip cache purge || true
pip install --no-cache-dir -e source/leisaac
pip install --no-cache-dir --no-build-isolation flatdict==4.0.1
( cd dependencies/IsaacLab && ./isaaclab.sh --install none )
pip install --no-cache-dir msgpack msgpack-numpy pyzmq
pip cache purge || true

# Free up as much space as possible before the pin upgrade — the compute node's
# /tmp/enroot data root is ~33 GB and by this point we've consumed most of it.
# .git in LeIsaac + submodules (esp. IsaacLab) is ~1-2 GB and no longer needed.
git submodule foreach --recursive 'rm -rf .git' 2>/dev/null || true
rm -rf .git
pip cache purge || true

# IsaacLab 2.3.0 pins dex_retargeting==0.4.6 which requires only pin>=2.7.0.
# Pip's resolver picks pin 2.7.0, whose bundled pinocchio_pywrap.so is compiled
# against numpy 1.x — but IsaacSim 6.0's Kit prepends its own numpy 2.3.1 at
# startup, so pinocchio import aborts eval with the numpy 1.x/2.x ABI error.
# Force-upgrade pin (and libpinocchio) to 4.1.0 which is built against numpy 2.
# This also bumps numpy in the venv to 2.x, aligning it with Kit's numpy.
pip install --no-cache-dir --upgrade 'pin>=4.0'
pip cache purge || true

# IsaacLab 2.3.0 pins `dex_retargeting==0.4.6` whose bundled pinocchio C
# extension was compiled against numpy 1.x, but IsaacSim 6.0's Kit prepends
# its own numpy 2.3.1 at startup — the ABI mismatch aborts eval.
# Nothing in the SO101 eval path uses VR teleop retargeting, so guard the
# eager import so `isaaclab.devices` loads without pulling in pinocchio.
python - <<'PY'
import pathlib
p = pathlib.Path("dependencies/IsaacLab/source/isaaclab/isaaclab/devices/__init__.py")
s = p.read_text()
old = "from .teleop_device_factory import create_teleop_device"
new = (
    "try:\n"
    "    from .teleop_device_factory import create_teleop_device\n"
    "except Exception:  # dex_retargeting/pinocchio numpy 1.x ABI vs IsaacSim numpy 2.x\n"
    "    create_teleop_device = None"
)
assert old in s, "IsaacLab devices/__init__.py layout changed; update patch"
patched = s.replace(old, new)
p.write_text(patched)
assert "except Exception:" in p.read_text(), f"Patch did not persist to {p}"
print(f"[install-leisaac] Patched {p} (teleop_device_factory import guarded)")
PY

# pip install -e ... の途中で isaaclab.devices が import され、パッチ前のソースから
# __pycache__/*.pyc が作られる。Python は .py と .pyc の mtime が同じだと .pyc を
# 優先するため、後段のパッチが無視されてしまう。isaaclab 配下の .pyc を消して
# 実行時に patched .py から再コンパイルさせる。
find dependencies/IsaacLab -type d -name "__pycache__" -prune -exec rm -rf {} +
echo "[install-leisaac] Cleared __pycache__ under dependencies/IsaacLab"

deactivate

echo "LeIsaac installed"
