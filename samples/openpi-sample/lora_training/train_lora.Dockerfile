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

# Install MLflow tracking dependencies on top of the locked environment
# - mlflow-skinny: Logs training runs to the SageMaker AI MLflow App
# - sagemaker-mlflow: SigV4 auth plugin for the MLflow App tracking server
# - nvidia-ml-py: Provides pynvml for MLflow GPU system metrics
# Installed with uv pip install so that openpi's pyproject.toml and uv.lock stay unchanged.
# nvidia-ml-py matches the version resolved in openpi's uv.lock; psutil is already an openpi dependency.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /.venv/bin/python \
    mlflow-skinny==3.10.1 \
    sagemaker-mlflow==0.5.0 \
    nvidia-ml-py==12.575.51

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

# Verify that the MLflow tracking dependencies are importable
RUN /.venv/bin/python -c "import mlflow, sagemaker_mlflow, psutil, pynvml; print(mlflow.__version__)"

# Training-specific environment variables
# XLA_PYTHON_CLIENT_MEM_FRACTION: Allocate 90% of GPU memory to JAX
ENV XLA_PYTHON_CLIENT_MEM_FRACTION=0.9
# XLA_FLAGS: Performance optimizations for multi-GPU
ENV XLA_FLAGS="--xla_gpu_enable_triton_softmax_fusion=true --xla_gpu_triton_gemm_any=True"

# Default command: bash shell
CMD ["/bin/bash"]
