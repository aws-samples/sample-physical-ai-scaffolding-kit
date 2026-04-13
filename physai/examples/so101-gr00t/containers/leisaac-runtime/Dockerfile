FROM nvcr.io/nvidia/pytorch:25.04-py3

ENV NVIDIA_DRIVER_CAPABILITIES=graphics,utility,compute
ENV DEBIAN_FRONTEND=noninteractive
ENV OMNI_KIT_ACCEPT_EULA=YES

# System deps
RUN apt-get update && apt-get install -y \
    build-essential git cmake unzip curl ffmpeg libegl1 \
    libxt6 libglu1-mesa && \
    rm -rf /var/lib/apt/lists/*

# Install uv
RUN pip install --upgrade pip setuptools && pip install uv

# ── LeIsaac + IsaacSim + IsaacLab (Python 3.11 venv) ──

ARG GR00T_REF
ARG LEISAAC_REF
RUN git clone --recursive https://github.com/LightwheelAI/leisaac.git /workspace/leisaac && \
    cd /workspace/leisaac && git checkout ${LEISAAC_REF}

WORKDIR /workspace/leisaac

# Create Python 3.11 venv (IsaacSim 5.1 requires 3.11)
RUN uv venv --python 3.11 .venv
RUN uv pip install pip

# Install PyTorch + IsaacSim + LeIsaac into the venv
RUN uv pip install torch==2.7.0 torchvision==0.22.0 --index-url https://download.pytorch.org/whl/cu128
RUN uv pip install 'isaacsim[all,extscache]==5.1.0' --extra-index-url https://pypi.nvidia.com
RUN uv pip install -e source/leisaac

# Install IsaacLab (flatdict needs --no-build-isolation)
ENV PATH="/workspace/leisaac/.venv/bin:$PATH"
ENV VIRTUAL_ENV=/workspace/leisaac/.venv
ENV PIP_CONSTRAINT=""
RUN pip install --no-build-isolation flatdict==4.0.1
RUN cd dependencies/IsaacLab && TERM=xterm ./isaaclab.sh --install none
RUN pip install click==8.1.7 msgpack msgpack-numpy pyzmq

# ── Isaac-GR00T (policy server, separate venv) ──

RUN git clone https://github.com/NVIDIA/Isaac-GR00T.git /workspace/gr00t && \
    cd /workspace/gr00t && git checkout ${GR00T_REF}

# Unset LeIsaac venv so GR00T creates its own
ENV PATH="/usr/local/bin:/usr/bin:/bin"
ENV VIRTUAL_ENV=""
RUN cd /workspace/gr00t && uv sync && uv pip install -e . && \
    uv pip install flash-attn --no-build-isolation

# ── Assets (baked in) ──

RUN mkdir -p /workspace/leisaac/assets/robots && \
    curl -L -o /workspace/leisaac/assets/robots/so101_follower.usd \
    https://github.com/LightwheelAI/leisaac/releases/download/v0.1.0/so101_follower.usd

RUN cd /workspace/leisaac/assets && \
    curl -L -o table_with_cube.zip \
    https://github.com/LightwheelAI/leisaac/releases/download/v0.1.2/table_with_cube.zip && \
    unzip -q table_with_cube.zip -d scenes && rm table_with_cube.zip

RUN cd /workspace/leisaac/assets && \
    curl -L -o kitchen_with_orange.zip \
    https://github.com/LightwheelAI/leisaac/releases/download/v0.1.0/kitchen_with_orange.zip && \
    unzip -q kitchen_with_orange.zip -d scenes && rm kitchen_with_orange.zip

# Vulkan ICD + EGL vendor config (required for Enroot — nvidia-container-cli
# mounts the Vulkan libraries but not the ICD JSON files)
# Place ICD in a custom path to avoid conflicts with docker's bind-mount to /etc/vulkan/icd.d/
RUN mkdir -p /opt/vulkan/icd.d && \
    echo '{"file_format_version":"1.0.0","ICD":{"library_path":"libGLX_nvidia.so.0","api_version":"1.3.0"}}' \
    > /opt/vulkan/icd.d/nvidia_icd.json
RUN mkdir -p /usr/share/glvnd/egl_vendor.d && \
    echo '{"file_format_version":"1.0.0","ICD":{"library_path":"libEGL_nvidia.so.0"}}' \
    > /usr/share/glvnd/egl_vendor.d/10_nvidia.json
ENV VK_ICD_FILENAMES=/opt/vulkan/icd.d/nvidia_icd.json

# Container protocol entrypoints
COPY eval.sh /app/eval.sh
RUN chmod +x /app/eval.sh

# Do NOT set global VIRTUAL_ENV — eval.sh uses explicit paths:
#   LeIsaac client: /workspace/leisaac/.venv/bin/python
#   GR00T server:   /workspace/gr00t/.venv/bin/python
ENV PATH="/usr/local/bin:/usr/bin:/bin"
ENV VIRTUAL_ENV=""

WORKDIR /workspace/leisaac
CMD ["/bin/bash"]
