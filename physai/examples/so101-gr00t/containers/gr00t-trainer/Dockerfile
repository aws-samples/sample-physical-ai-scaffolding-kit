FROM nvcr.io/nvidia/pytorch:25.04-py3

ENV NVIDIA_DRIVER_CAPABILITIES=graphics,utility,compute
ENV DEBIAN_FRONTEND=noninteractive

# System deps (from official Dockerfile)
RUN apt-get update && apt-get install -y \
    build-essential yasm cmake libtool git pkg-config \
    libass-dev libfreetype6-dev libvorbis-dev \
    autoconf automake texinfo ffmpeg libegl1 && \
    rm -rf /var/lib/apt/lists/*

# Install uv + fix NGC pip constraints
RUN pip install --upgrade pip setuptools && pip install uv
RUN sed -i s/dill==0.3.9/dill==0.3.8/g /etc/pip/constraint.txt
RUN sed -i s/scipy==1.15.2/scipy==1.15.3/g /etc/pip/constraint.txt

# Pre-install heavy deps into system Python (from official Dockerfile)
RUN pip install imageio h5py boto3 transformers[torch] deepspeed timm peft \
    diffusers wandb tianshou dm_tree openai \
    albumentations==1.4.18 decord==0.6.0 torchcodec==0.4.0
RUN cd /opt && git clone https://github.com/facebookresearch/pytorch3d.git && \
    cd pytorch3d && pip install .

# Clone Isaac-GR00T
ARG GR00T_REF
RUN git clone https://github.com/NVIDIA/Isaac-GR00T.git /workspace/gr00t && \
    cd /workspace/gr00t && git checkout ${GR00T_REF}

# Install GR00T + flash-attn in one step (scaffolding kit pattern)
# --no-build-isolation builds flash-attn from source against the installed torch
RUN cd /workspace/gr00t && uv sync && uv pip install -e . && \
    uv pip install flash-attn --no-build-isolation

WORKDIR /workspace/gr00t
ENV VIRTUAL_ENV=/workspace/gr00t/.venv
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

# EGL/Vulkan config for rendering (from official Dockerfile)
RUN mkdir -p /usr/share/glvnd/egl_vendor.d && \
    echo '{"file_format_version":"1.0.0","ICD":{"library_path":"libEGL_nvidia.so.0"}}' \
    > /usr/share/glvnd/egl_vendor.d/10_nvidia.json
RUN mkdir -p /usr/share/vulkan/icd.d && \
    echo '{"file_format_version":"1.0.0","ICD":{"library_path":"libGLX_nvidia.so.0","api_version":"1.2.140"}}' \
    > /usr/share/vulkan/icd.d/nvidia_icd.json

# Container protocol entrypoint
COPY train.sh /app/train.sh
RUN chmod +x /app/train.sh

CMD ["/bin/bash"]
