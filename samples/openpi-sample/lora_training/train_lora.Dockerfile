# Dockerfile for OpenPI LoRA Fine-tuning on HyperPod
# Based on serve_policy.Dockerfile with training-specific optimizations

FROM nvidia/cuda:12.2.2-cudnn8-devel-ubuntu22.04

# Copy uv package manager
COPY --from=ghcr.io/astral-sh/uv:0.5.1 /uv /uvx /bin/

WORKDIR /app

# Install system dependencies
# - git-lfs: Required by LeRobot datasets
# - build-essential: Required for JAX compilation
RUN apt-get update && apt-get install -y \
    git \
    git-lfs \
    build-essential \
    clang \
    curl \
    libgl1-mesa-glx \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# UV configuration
ENV UV_LINK_MODE=copy
ENV UV_PROJECT_ENVIRONMENT=/.venv

# Create virtual environment and install dependencies
RUN uv venv --python 3.11.9 $UV_PROJECT_ENVIRONMENT

# Install project dependencies using lockfile
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=packages/openpi-client/pyproject.toml,target=packages/openpi-client/pyproject.toml \
    --mount=type=bind,source=packages/openpi-client/src,target=packages/openpi-client/src \
    GIT_LFS_SKIP_SMUDGE=1 uv sync --frozen --no-install-project --no-dev

# Copy transformers_replace files (model architecture patches)
COPY src/openpi/models_pytorch/transformers_replace/ /tmp/transformers_replace/
RUN /.venv/bin/python -c "import transformers; print(transformers.__file__)" | \
    xargs dirname | \
    xargs -I{} cp -r /tmp/transformers_replace/* {} && \
    rm -rf /tmp/transformers_replace

# Set PYTHONPATH to include src directory
ENV PYTHONPATH=/app:/app/src

# Activate virtual environment by default
ENV PATH="/.venv/bin:$PATH"

# Training-specific environment variables
# XLA_PYTHON_CLIENT_MEM_FRACTION: Allocate 90% of GPU memory to JAX
ENV XLA_PYTHON_CLIENT_MEM_FRACTION=0.9
# XLA_FLAGS: Performance optimizations for multi-GPU
ENV XLA_FLAGS="--xla_gpu_enable_triton_softmax_fusion=true --xla_gpu_triton_gemm_any=True"

# Default command: bash shell
CMD ["/bin/bash"]
