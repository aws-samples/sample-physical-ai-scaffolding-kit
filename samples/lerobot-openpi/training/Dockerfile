# Dockerfile for LeRobot pi0.5 / pi0 fine-tuning on SageMaker HyperPod (Slurm + Enroot)
#
# Base: nvidia/cuda 12.8.1 on Ubuntu 24.04 (base flavor). The PyTorch cu128 wheels
# ship their own CUDA runtime, cuDNN and NCCL libraries, so the devel/runtime
# flavors are not needed. Only NPP is added from the CUDA apt repository, because
# the cu128 torchcodec wheel links against it and the PyTorch wheels do not ship it.
# Pinned versions: LeRobot (LEROBOT_VERSION), Python (PYTHON_VERSION), uv image tag,
# and the PyTorch CUDA backend (cu128).
#
# Build context: samples/lerobot-openpi/training/ (no files are copied into the image).

FROM nvidia/cuda:12.8.1-base-ubuntu24.04

ARG LEROBOT_VERSION=0.6.1
ARG PYTHON_VERSION=3.12
ARG DEBIAN_FRONTEND=noninteractive

COPY --from=ghcr.io/astral-sh/uv:0.12.23 /uv /uvx /bin/

# - ffmpeg: video decoding for LeRobotDataset (torchcodec)
# - libnpp-12-8: NPP libraries (libnppicc.so.12) required by the cu128 torchcodec build;
#   without them torchcodec fails to load and LeRobot falls back to the slower pyav decoder
# - libgl1, libglib2.0-0: runtime libraries for OpenCV
# - git, git-lfs: Hugging Face Hub repositories
# - build-essential: C compiler used by triton and packages built from source
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libnpp-12-8 \
    libgl1 \
    libglib2.0-0 \
    git \
    git-lfs \
    build-essential \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install the uv-managed Python under /opt instead of /root, because Enroot runs
# the container as the submitting user (ubuntu) who cannot read /root.
ENV UV_LINK_MODE=copy \
    UV_PYTHON_INSTALL_DIR=/opt/uv/python \
    UV_PYTHON_PREFERENCE=only-managed

WORKDIR /opt/lerobot

RUN uv venv --python ${PYTHON_VERSION} /opt/lerobot/.venv

ENV VIRTUAL_ENV=/opt/lerobot/.venv
ENV PATH=/opt/lerobot/.venv/bin:$PATH

RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install \
    --torch-backend=cu128 \
    "lerobot[pi,training,peft]==${LEROBOT_VERSION}"

# HOME, HF_HOME, HF_LEROBOT_HOME, TRITON_CACHE_DIR and TORCHINDUCTOR_CACHE_DIR are
# intentionally not set here. Enroot mounts the squashfs image read-only and runs it
# as the submitting user (ubuntu), so every writable location is passed in by the
# Slurm job scripts and points to /fsx.
# CUDA_VISIBLE_DEVICES is intentionally not set here either; GPU visibility is left
# to the Slurm GRES allocation.

# Fail the build early if dependency resolution produced a broken environment.
# This check must not touch the GPU because the build host may not have one.
RUN ldconfig -p | grep -q "libnppicc.so.12" \
    && python -c "import torch, transformers, peft, lerobot; print(lerobot.__version__, torch.__version__, torch.version.cuda)" \
    && lerobot-train --help > /dev/null \
    && lerobot-edit-dataset --help > /dev/null \
    && hf --help > /dev/null

CMD ["/bin/bash"]
