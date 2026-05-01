# Running the Sample Project

This document walks you through using a sample project to learn how to use the physai platform.

## Installing and Configuring the physai CLI

We recommend using Python in a virtual environment using tools like [uv venv](https://docs.astral.sh/uv/pip/environments/) rather than installing it in your system environment.

```bash
# cd into physai/ first:

pip install -e cli
```

Configure the CLI:

```bash
mkdir -p ~/.physai && cat > ~/.physai/config.yaml <<EOF
host: physai-login
model_config_roots:
  - $(pwd)/examples/so101-gr00t/model_configs
EOF
```

`~/.physai/config.yaml` is the configuration file referenced when using the `physai` command. Both values can be overridden with command-line arguments at runtime:

- `--host HOST` overrides `host`
- `--model-config-root PATH` prepends to `model_config_roots` (can be specified multiple times)

If you only use one host and one model-config root, setting them here eliminates the need for flags.

## Obtaining the Dataset

This sample uses the [Pick Orange](https://huggingface.co/datasets/LightwheelAI/leisaac-pick-orange) dataset published by Lightwheel AI alongside the [LeIsaac](https://github.com/lightwheelai/leisaac) simulation environment. Since it is already provided in LeRobot v2.1 format (60 episodes, approximately 36k frames, 698 MB), you can upload it to the cluster and run `train` + `eval` directly.

```bash
pip install -U huggingface_hub
hf download LightwheelAI/leisaac-pick-orange \
  --repo-type dataset --local-dir /tmp/leisaac-pick-orange
physai upload datasets /tmp/leisaac-pick-orange/
```

The `physai upload datasets` command places the dataset at `/fsx/datasets/leisaac-pick-orange/` on the cluster.

You can verify the stored datasets with the following command:

```bash
physai ls datasets
```

## Building Containers

Containers on the cluster are built and run using **Enroot** (a lightweight, rootless container runtime) and **Pyxis** (a Slurm plugin that enables Enroot via `srun --container-image=...`). Here are two key concepts:

- **Image** — A build artifact. A squashfs file located at `/fsx/enroot/<name>.sqsh`. Each `physai build` produces one image. Images are immutable, shared across jobs, and persist until you `--rebuild` or delete the file.
- **Container** — A live runtime instance of an image. Created on a worker node at job start and typically destroyed when the job ends. Containers may remain if a job terminates abnormally. Use `physai clean --enroot` to remove stale containers.

`physai build` generates images used in the pipeline. For this sample, you need to build three images. Image builds are executed as Slurm jobs. Paste all three commands and the jobs will run sequentially. It takes approximately 30 minutes for all three jobs to complete.

```bash
physai build -n examples/so101-gr00t/containers/leisaac-runtime
physai build -n examples/so101-gr00t/containers/leisaac-gr00t-n1.6
physai build -n examples/so101-gr00t/containers/gr00t-n1.6-trainer
```

Build logs are streamed to your terminal. You can detach with Ctrl-C (the build continues). Use `physai logs <job-id>` to check build logs.

You can view the history of executed jobs with the following command:

```bash
physai list

physai list
JOB_ID   TYPE    NAME                           STATE        SUBMIT (UTC)    START (UTC)     ELAPSED    COMMENT
3        build   leisaac-gr00t-n1.6             PENDING      04-28 09:40:55  N/A             0:00       base=/fsx/enroot/leisaac-runtime.sqsh
2        build   leisaac-runtime                RUNNING      04-28 09:39:35  04-28 09:39:35  2:50       base=nvcr.io/nvidia/cuda:12.8.1-cudnn-devel-ubuntu24.04
```

## Running the Pipeline

The pipeline definition is configured in a YAML file. The pipeline is executed based on the information in this configuration file: [examples/so101-gr00t/configs/so101_liftcube_gr00t-n1.6.yaml](/physai/examples/so101-gr00t/configs/so101_liftcube_gr00t-n1.6.yaml).

```yaml
pipeline:
  stages: [train, eval]

sim:
  platform: leisaac
  environment: LeIsaac-SO101-LiftCube-v0
  language_instruction: "Lift the red cube up"

model:
  name: gr00t-n1.6
  config_dir: model_configs/gr00t-n1.6/so101-singlecam

stages:
  train:
    partition: gpu
    gres: "gpu:1"
    constraint: l40s
    container: gr00t-n1.6-trainer
    max_steps: 10000
  eval:
    partition: gpu
    gres: "gpu:1"
    container: leisaac-gr00t-n1.6
    rounds: 20
```

- `pipeline.stages`: Stages to run by default.
- `stages.<name>`: Resource and parameter settings for each stage.
- `model.config_dir`: A relative name resolved against `model_config_roots` (see [`run_config.yaml` reference](#run_configyaml-reference)).

### Running All Default Stages

The following command executes the stages specified in the pipeline:

```bash
physai run -n --config examples/so101-gr00t/configs/so101_pickorange_gr00t-n1.6.yaml \
  --dataset leisaac-pick-orange
```

The executed pipeline runs as Slurm jobs. You can check the results with the following command:

```bash
physai list
JOB_ID   TYPE    NAME                           STATE        SUBMIT (UTC)    START (UTC)     ELAPSED    COMMENT
5        run     run-20260430-011618/eval       COMPLETED    04-30 01:16:34  04-30 02:50:05  00:44:07
4        run     run-20260430-011618/train      COMPLETED    04-30 01:16:29  04-30 01:16:30  01:33:35
```

This concludes the introduction to using the pipeline with the sample project. You can modify the sample scripts and configurations to suit your needs.
