# Dockerfile for Isaac Lab Newton + RSL-RL on HyperPod.
# Based on the pinned NVIDIA Isaac Lab pre-built container.

ARG BASE_IMAGE=nvcr.io/nvidia/isaac-lab:3.0.0-beta1
FROM ${BASE_IMAGE}

SHELL ["/bin/bash", "-c"]

ARG PRIVACY_CONSENT=Y

ENV ISAACLAB_PATH=/workspace/IsaacLab \
    PYTHONUNBUFFERED=1

USER root
WORKDIR ${ISAACLAB_PATH}

# Install the RSL-RL extra into the pre-built Isaac Lab container.
# The build script requires ACCEPT_EULA=Y before building this image.
RUN ACCEPT_EULA=Y PRIVACY_CONSENT=${PRIVACY_CONSENT} ./isaaclab.sh -i rsl_rl

CMD ["/bin/bash"]
