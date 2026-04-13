# as root
apt-get update && apt-get install -y \
                          build-essential git cmake unzip curl ffmpeg libegl1 \
                          libxt6 libglu1-mesa libxext6 libegl1
pip install --upgrade pip setuptools && pip install uv

# update /etc/environment for these variables
# - OMNI_KIT_ACCEPT_EULA=YES
# - PIP_CONSTRAINT=""
# - LEISAAC_DIR=/workspace/leisaac
# - NVIDIA_VISIBLE_DEVICES=all
# - NVIDIA_DRIVER_CAPABILITIES=all

# as normal user
uv python install 3.11

(
    git clone https://github.com/LightwheelAI/leisaac.git "${LEISAAC_DIR}" && cd "${LEISAAC_DIR}" && git checkout "${LEISAAC_REF}" && git submodule update --init --recursive
    uv venv --python 3.11 .venv
    uv pip install pip
    
    source .venv/bin/activate
    pip install torch==2.7.0 torchvision==0.22.0 --index-url https://download.pytorch.org/whl/cu128
    pip install 'isaacsim[all,extscache]==5.1.0' --extra-index-url https://pypi.nvidia.com
    pip install -e source/leisaac

    # flatdict 4.0.1 uses pkg_resources in setup.py; pip's build isolation
    # creates a clean env without setuptools, so install it separately first
    pip install --no-build-isolation flatdict==4.0.1
    ( cd dependencies/IsaacLab && ./isaaclab.sh --install none )
    pip install msgpack msgpack-numpy pyzmq

    # Remove git stuff to save space
    git submodule foreach --recursive 'rm -rf .git' 2>/dev/null || true
    rm -rf .git
)



# pip constraints, these packages installed by isaaclab.sh have conflicts with the constrants
# torch
# torchvision
# protobuf
# pillow
# onnx

