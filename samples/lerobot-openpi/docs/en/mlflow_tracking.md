# Training Tracking with MLflow

The training job of this sample records the training configuration, metrics, and LoRA adapter to an **MLflow App** in Amazon SageMaker AI.
This document describes how the MLflow App is set up, how the records are written, and what each run records.
For setting the environment variables, submitting jobs, and opening the MLflow UI, see [Training π0.5 with LeRobot](lerobot_pi05_training.md#mlflow-tracking).

## Setup

An MLflow App is an MLflow tracking server whose servers are managed by Amazon SageMaker AI.
The CDK in this repository (`hyperpod/lib/constructs/mlflow.ts`) creates the MLflow App and its related resources together with the HyperPod stack.

| Resource | Description |
|---|---|
| MLflow App | Named `pask-mlflow`. The MLflow version is not specified, so the App gets the default version of SageMaker AI at creation time (3.16 as of November 2026). The weekly maintenance starts on Sunday at 18:00 (UTC). Automatic model registration is disabled |
| S3 bucket for artifacts | Artifacts are stored under `s3://<bucket>/mlflow`. This bucket is separate from the Data bucket linked to FSx for Lustre. The bucket is retained when the stack is deleted |
| Service role of the MLflow App | The role that the MLflow App uses to read and write the artifact bucket |
| Permissions on the ExecutionRole | Permissions for the HyperPod nodes to call the MLflow App API (`sagemaker:CallMlflowAppApi`) and to read and write the artifact bucket |

The stack Outputs include the ARN of the MLflow App (`MlflowAppArn`) and the name of the artifact bucket (`MlflowArtifactBucketName`).
The HyperPod nodes connect to the public endpoint of the MLflow App over HTTPS through the existing NAT Gateway.
To use it without internet access, add the interface VPC endpoint `aws.sagemaker.<region>.mlflow` and the SageMaker API endpoint, and enable private DNS.

## How Records Are Written

LeRobot has no MLflow integration; it can only record to W&B (Weights & Biases).
This sample therefore runs training with the W&B logger replaced by a logger that records to MLflow.
LeRobot itself is not modified.

Records are written in the following order.

1. `setup.sh` reads the ARN of the MLflow App from the stack Outputs and writes it to `~/.bashrc` as `MLFLOW_TRACKING_URI`.
2. When `MLFLOW_TRACKING_URI` is set, `slurm_train.sh` starts `training/mlflow_train.py` instead of `lerobot-train`.
3. `mlflow_train.py` replaces LeRobot's `WandBLogger` with `MlflowLogger` and then calls the same code as `lerobot-train`.
4. `MlflowLogger` sends the values that the training loop passes to the W&B logger to the MLflow App.

The MLflow client in the container uses the `sagemaker-mlflow` plugin, which accepts the ARN of the MLflow App as the tracking URI and signs requests with SigV4.
The credentials used for signing come from the ExecutionRole of the HyperPod node.
No access key or token needs to be passed to the job.

The following table maps the logger methods called by the LeRobot training loop to what is recorded in MLflow.

| Method called by LeRobot | When it is called | What is recorded in MLflow |
|---|---|---|
| `log_dict` | Every `LOG_FREQ` steps, and after evaluation | Metrics `train/*` and `eval/*` |
| `log_policy` | When a checkpoint is saved | Metric `checkpoint_step`, tag `pask.last_checkpoint_dir`, and the LoRA adapter of the final step |
| `log_video` | When evaluation produces a video | Artifact `videos/eval/` |

## Experiments and Runs

An experiment groups runs.
New runs are created in the experiment named by `MLFLOW_EXPERIMENT_NAME` (default `lerobot-pi`).

A run corresponds to one training.
The run name is the job name (the first argument of `slurm_train.sh`).
Submitting with the same job name again still creates a run with a different run ID.

`mlflow_train.py` saves the run ID to `wandb.run_id` in the `train_config.json` of the checkpoint.
When training is resumed with `RESUME=true`, the run with this run ID is opened and recording continues.
As a result, metrics stay in one run even when training that stopped at the 48-hour time limit is resumed.

The state of the run depends on how the job ends.

| How the job ends | Run state |
|---|---|
| Training ran to the end | `FINISHED` |
| Training stopped with an error | `FAILED` |
| Stopped by `scancel` or the time limit | `KILLED` |

### Runs Started from VAMS

For jobs started from a VAMS workflow, the poller passes the path of `vams_context.json` in the environment variable `VAMS_CONTEXT_FILE`.
`mlflow_train.py` treats the job as a VAMS run when this variable is set, and as a manual run otherwise.
If the variable is set but the file cannot be read or contains invalid JSON, the job fails before training starts, without creating a run.
This prevents runs without a record of their input.

The experiment is the VAMS-specific experiment that the poller passes in `MLFLOW_EXPERIMENT_NAME` (default `vams-training`).

Only one run is created for one VAMS execution (`pipelineExecutionId`).
Before creating a new run, `mlflow_train.py` looks for a run in the experiment with the same tag `vams.pipeline_execution_id`.
If one is found, that run is opened and recording continues.
In this case the params are already recorded and cannot be changed, so params and `train_config.json` are not recorded.

When the run starts, a back link to the run is written to the file that `resultFile` in `vams_context.json` points to (`mlflow_run.json`).
After the job ends, the poller reads this file and puts the run ID into the result sent to VAMS.
The file is written to a temporary file and then renamed, so the poller never reads a partially written file.

```json
{"run_id": "...", "experiment_id": "...", "tracking_uri": "..."}
```

For the format of `vams_context.json` and what the poller does, see the [VAMS Integration Specification](../../../../hyperpod/docs/en/VAMS_INTEGRATION/SPEC.md#what-is-recorded-to-mlflow).

## What a Run Records

### Params

The whole training configuration is flattened into dot-separated keys and recorded as params (for example `dataset.repo_id`, `peft.r`, `optimizer.lr`, `steps`).
LoRA training of π0.5 records about 140 params.
Each value is limited to 6000 characters, and anything longer is truncated.

MLflow params cannot be changed after they are recorded.
Params are therefore recorded only when the run is created, not when training is resumed or when the run of the same VAMS execution is continued.
Even if `STEPS` extends the total number of steps on resume, the `steps` param keeps its first value.

### Metrics `train/*`

Every `LOG_FREQ` steps (default 100), the same values that LeRobot prints to the training log are recorded.
The x-axis is the training step.

| Metric | Description | Name in the training log |
|---|---|---|
| `train/loss` | Training loss | `loss` |
| `train/grad_norm` | Gradient norm | `grdn` |
| `train/lr` | Learning rate | `lr` |
| `train/update_s` | Seconds spent updating the weights in one step | `updt_s` |
| `train/dataloading_s` | Seconds spent loading data in one step | `data_s` |
| `train/samples_per_s` | Samples processed per second | `smp/s` |
| `train/gpu_mem_gb` | GPU memory usage (GB) | `mem_gb` |
| `train/steps` | Number of steps | `step` |
| `train/samples` | Samples processed since training started | `smpl` |
| `train/episodes` | Processed samples converted to dataset episodes | `ep` |
| `train/epochs` | Processed samples converted to passes over the whole dataset | `epch` |

To judge whether training is going well, first check that `train/loss` is decreasing.
If `train/dataloading_s` is large compared with `train/update_s`, data loading is limiting the training speed.
In that case, increasing `NUM_WORKERS` may make training faster.

### Metrics `eval/*`

Evaluation results are recorded to `eval/*` only when LeRobot evaluation is enabled through `EXTRA_ARGS`.
The default settings of this sample do not run evaluation, so nothing is recorded.

### Metric `checkpoint_step`

The step at which a checkpoint was saved is recorded as the value at that step.
One point is added every `SAVE_FREQ` steps (default 5000).
The directory of the last saved checkpoint is shown by the tag `pask.last_checkpoint_dir`.

### Metrics `system/*`

The system metrics feature of MLflow records node usage every `MLFLOW_SYSTEM_METRICS_SAMPLING_INTERVAL` seconds (default 10).
The x-axis is the number of system metrics samples, not the training step.

| Metric | Description |
|---|---|
| `system/gpu_0_utilization_percentage` | GPU utilization (%) |
| `system/gpu_0_memory_usage_megabytes` | GPU memory usage (MB) |
| `system/gpu_0_memory_usage_percentage` | GPU memory usage (%) |
| `system/gpu_0_power_usage_watts` | GPU power draw (W) |
| `system/gpu_0_power_usage_percentage` | GPU power draw relative to its limit (%) |
| `system/cpu_utilization_percentage` | CPU utilization (%) |
| `system/system_memory_usage_megabytes` | Node memory usage (MB) |
| `system/system_memory_usage_percentage` | Node memory usage (%) |
| `system/disk_usage_megabytes` | Disk usage (MB) |
| `system/disk_usage_percentage` | Disk usage (%) |
| `system/disk_available_megabytes` | Available disk space (MB) |
| `system/network_receive_megabytes` | Data received since recording started (MB) |
| `system/network_transmit_megabytes` | Data sent since recording started (MB) |

`gpu_0` in the GPU metrics is the GPU index.
Training in this sample uses a single GPU, so only `gpu_0` is recorded.
If `system/gpu_0_utilization_percentage` is low, check `train/dataloading_s` as well to see whether the GPU is waiting for data.

### Artifacts

| Artifact | Description | When it is recorded |
|---|---|---|
| `train_config.json` | The whole training configuration | When the run is created |
| `adapter/<step>/` | LoRA adapter (`adapter_model.safetensors`, `adapter_config.json`, `config.json`) | When the checkpoint of the final step (`STEPS`) is saved |
| `videos/eval/` | Evaluation videos | Only when evaluation is enabled |
| `vams/input_manifest.json` | List of input files (bucket, key, versionId, ETag, size). For a curation view, the list of files of the source asset and the information of the source asset (`source`) | When a run started from VAMS begins |
| `vams/curation_manifest.json` | Manifest of the curation view | When a run started from VAMS with a curation view begins |
| `vams/norm_stats.json` | Normalization statistics used for training: the `meta/stats.json` computed by `compute_view_stats.py` from the kept episodes | When a run started from VAMS with a curation view begins |

The LoRA adapter is recorded only when the three files together are at most `MLFLOW_ADAPTER_MAX_MB` (default 200) and `MLFLOW_DISABLE_ARTIFACT` is `false`.
An adapter with `LORA_R=16` is about 5 MB.
Checkpoints with `FINETUNE_MODE=expert_only` or `full` have no adapter, and their full weights are several GB, so they are not recorded to MLflow.
For these weights, refer to the checkpoint on FSx at the path in the tag `pask.last_checkpoint_dir`.

### Dataset

For runs started from VAMS, the input dataset is registered as the dataset of the run with `mlflow.log_input`.
Manual runs do not register a dataset.

| Field | Value |
|---|---|
| source | `s3://<input.bucket>/<input.assetRootS3Key>` (the root of the VAMS asset) |
| name | Asset name (`input.assetName`), or the asset ID (`input.assetId`) if there is no name |
| digest | The first 36 hexadecimal characters of `datasetDigest` |
| context | `training` |

When the input is a curation view, the source is the root of the source asset (`s3://<view.source.bucket>/<view.source.assetRootS3Key>`).
The view asset holds no files used for training; the files to train on are in the source asset.
The name stays the name of the view asset (or the ID of the view asset if there is no name).

MLflow limits the digest of a dataset to 36 characters, so a value of the form `sha256:<hex>` does not fit as is.
The dataset digest therefore uses the first 36 hexadecimal characters, and the full value is kept in the tag `vams.dataset_digest`.

If registering the dataset fails, a warning is logged and training continues.
The record of the input remains in the `vams.*` tags and the artifact `vams/input_manifest.json`.

### Tags

Each run carries tags that describe the training conditions and the environment.
Tags with empty values are not set.

| Tag | Value |
|---|---|
| `lerobot.version` | LeRobot version |
| `lerobot.policy_type` | `pi05` or `pi0` |
| `lerobot.finetune_mode` | One of `lora`, `expert_only`, or `full` |
| `lerobot.dataset_repo_id` | Repository ID of the dataset |
| `lerobot.pretrained_path` | Base weights. Recorded only when the run is created |
| `pask.output_dir` | Output directory |
| `pask.last_checkpoint_dir` | Directory of the last saved checkpoint |
| `pask.image_tag` | Tag of the container image (`IMAGE_TAG`) |
| `pask.cluster` | Name of the HyperPod cluster |
| `pask.run_type` | `train` |
| `pask.trigger` | `vams` for runs started from VAMS, `manual` for manual runs |
| `slurm.job_id` | ID of the last job that recorded to this run |
| `slurm.job_ids` | IDs of all jobs that recorded to this run (comma-separated) |

Runs started from VAMS also carry the following tags.
The values come from `vams_context.json`.

| Tag | Value |
|---|---|
| `vams.workflow_database_id` | Database ID of the workflow (`workflowDatabaseId`) |
| `vams.workflow_id` | ID of the workflow (`workflowId`) |
| `vams.workflow_execution_id` | ID of the workflow execution (`workflowExecutionId`) |
| `vams.pipeline_execution_id` | ID of the pipeline execution (`pipelineExecutionId`). Used to find the run of the same execution |
| `vams.pipeline_name` | Name of the pipeline (`pipelineName`) |
| `vams.executing_user_name` | Name of the user who ran the workflow (`executingUserName`) |
| `vams.input.database_id` | Database ID of the input asset (`input.databaseId`) |
| `vams.input.asset_id` | ID of the input asset (`input.assetId`) |
| `vams.input.asset_name` | Name of the input asset (`input.assetName`) |
| `vams.input.versioned` | Whether the versions of the input files could be pinned (`true` or `false`). `false` when versioning is disabled on the asset bucket |
| `vams.input.kind` | Kind of input (`input.kind`): `view` for a curation view, `dataset` for a regular dataset |
| `vams.output.asset_id` | ID of the asset that receives the trained model (`output.assetId`) |
| `vams.dataset_digest` | Digest computed from the versions of the input files (`sha256:<hex>`) |

When the input is a VAMS curation view, the `vams.input.*` tags hold the values of the view asset.
The source asset that the view points to and the kept episodes are recorded in the following tags.
The values come from `view` in `vams_context.json`.

| Tag | Value |
|---|---|
| `vams.source.database_id` | Database ID of the source asset (`view.source.databaseId`) |
| `vams.source.asset_id` | ID of the source asset (`view.source.assetId`) |
| `vams.source.asset_version` | Version of the source asset (`view.source.assetVersion`). Not set if there is none |
| `vams.view.episode_count` | Number of episodes used for training (`view.episodeCount`) |
| `vams.view.excluded_episode_count` | Number of episodes excluded by the view (`view.excludedEpisodeCount`) |
| `vams.view.review_updated_at` | `source.review_updated_at` of the view manifest (`view.reviewUpdatedAt`). Not set if there is none |

`lerobot.pretrained_path` is kept as a separate tag because LeRobot rewrites `pretrained_path` in the configuration to the checkpoint path on resume.
`slurm.job_ids` tells which job logs to check, including resumed jobs.

The experiment carries the tag `mlflow.experimentKind=custom_model_development`.
When checked with the MLflow 3.10 UI, this tag did not change the display.
The view for model training is selected with the UI switch.
