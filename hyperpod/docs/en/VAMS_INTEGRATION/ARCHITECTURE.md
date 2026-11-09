# How the VAMS Integration Works

This document summarizes the mechanism and constraints of the integration that runs LeRobot (pi0 and pi0.5) training on the PASK HyperPod cluster from VAMS (Visual Asset Management System) workflows.
The readers are users who run training from VAMS and developers who operate or modify the integration.
For installation steps and the list of configuration items, see [VAMS Integration Setup and Operation](/hyperpod/docs/en/VAMS_INTEGRATION/OVERVIEW.md). For details of the message and file formats, see [VAMS Integration Specification](/hyperpod/docs/en/VAMS_INTEGRATION/SPEC.md).

## Integration Scope

VAMS users select a LeRobot v3.0 dataset asset, run a workflow, and receive the trained model as a separate asset.
Training progress and results are recorded in the experiment `vams-training` of the PASK MLflow App.

All integration functionality is on the PASK side, and the VAMS code and deployment settings are not changed.
On the VAMS side, you only register the pipeline and workflow definitions bundled with PASK (`hyperpod/vams/`).
This integration assumes that VAMS and PASK are in the same AWS account and the same Region.
Configurations that split accounts or Regions, and training runs longer than one week, are out of scope.

## Overall Flow

VAMS calls PASK as a pipeline of the built-in SQS execution type (`waitForCallback=Enabled`).
A poller resident on the PASK login node (the systemd service `pask-vams-poller`) advances the execution in the following order.

1. VAMS Step Functions sends a message to the PASK SQS queue and waits until a TaskToken is returned.
2. The poller receives the message, records the execution in the PASK DynamoDB table, and then deletes the message.
3. The poller copies the input dataset from the VAMS asset bucket to `vams/<pipelineExecutionId>/` in the PASK data bucket.
   If the input is a curation view, it copies the files of the original asset that the view points to (see the section "How Curation Views Are Resolved").
   The copied files appear under `/fsx/s3link/vams/<pipelineExecutionId>/` through FSx for Lustre automatic import.
4. The poller submits `slurm_train.sh` as a Slurm job and monitors it until the job finishes.
5. When the job succeeds, the poller writes the trained model back to the VAMS outputs and returns `SendTaskSuccess`.
   VAMS imports the files in the outputs into the output asset.
6. The poller deletes the copy in the PASK data bucket, and the FSx execution directory and output directory.

The ownership of execution state is split between VAMS and PASK.
VAMS holds the state of the workflow and steps (running, succeeded, failed, aborted), and PASK only returns `SendTask*`.
The Slurm job ID, the TaskToken, and the PASK-side paths are held by the PASK DynamoDB table.
PASK records these itself because the hyperpod stack has no slurmdbd, and finished jobs disappear from `squeue` within a few minutes.

## What Users Specify and Receive

When running the workflow `pask-lerobot-openpi-training` in VAMS, users specify the following.

| Specified item | Description |
|---|---|
| Input (regular dataset) | The entire dataset asset. The LeRobot v3.0 dataset must be at the root of the asset, and `meta/info.json` must exist at the root |
| Input (curation view) | The entire asset of a view created by VAMS LeRobot Dataset Curation. `curation_manifest.json` must exist at the root, and its `mode` must be `view`. The view and the original asset must be in the same database |
| Output asset | The asset that receives the trained model. Use an asset separate from the dataset |
| Training parameters | Only the items listed in the template tags, such as `POLICY_TYPE`, `FINETUNE_MODE`, `STEPS`, and `SAVE_FREQ`. If there is a key that is not in the allowlist, the execution fails |

For either input, specifying a single file or a folder inside the asset is not accepted.

When the execution succeeds, users receive the following.

- The output asset contains the files under `checkpoints/last/pretrained_model` (weights, configuration, and preprocessing and postprocessing settings). The optimizer state is not included.
- The results of the VAMS execution details contain `pask_training.json` (Slurm job ID, training parameters, dataset digest, MLflow run ID, and input type). For a view, it also contains the original asset and the number of retained episodes.
- The MLflow run records the tags `pask.trigger=vams` and `vams.*` (the VAMS execution ID, the input and output assets, and so on), the input dataset, and the list of input files (artifact `vams/input_manifest.json`). For a view, it also records the tags of the original asset, the view manifest (artifact `vams/curation_manifest.json`), and the normalization statistics used for training (artifact `vams/norm_stats.json`).

When you abort an execution in VAMS, the poller detects the abort and `scancel`s the job.
VAMS does not send an abort notification, so the poller detects the abort when the heartbeat returns an error that the TaskToken has expired.
Because a retry of a VAMS send can deliver a new TaskToken, the poller waits for a grace period (30 minutes by default) before it decides that the execution is aborted.

When a failure occurs, the following error names are shown in the VAMS execution details.

| Error name | Cause |
|---|---|
| `PASK.InvalidRequest` | The input specification or the training parameters do not meet the conditions. For the input, this is the case when: neither `meta/info.json` nor `curation_manifest.json` exists at the root; `curation_manifest_version` is not 2; the view manifest is invalid (a required item is missing, an episode number is invalid, and so on); the manifest points to a different database or bucket; or the version recorded in the manifest does not exist in the original asset, or its size differs from the record |
| `PASK.StagingFailed` | Copying the data failed, the free space of FSx is insufficient, or the import did not finish within the wait limit (3600 seconds by default). For a view, this also includes the case where the poller cannot read the original asset (403) |
| `PASK.SubmitFailed`, `PASK.JobLost` | The job could not be submitted, or its completion could not be confirmed |
| `PASK.JobFailed` | The training job failed. This includes a failure to compute the normalization statistics of a view |
| `PASK.OutputUploadFailed` | The trained model could not be written back to the outputs |

## Why Data Is Copied Separately from the VAMS Bucket

The training input does not connect the VAMS asset bucket directly to FSx. Instead, the data is copied to the PASK data bucket for each execution.
If VAMS and FSx both synchronize the same S3 data, the file contents and the version records of VAMS can diverge, which may corrupt the data.

The FSx of PASK has a data repository association with the data bucket, and it automatically reflects creation, modification, and deletion in both directions.
If the VAMS asset bucket is connected through this association, the data and records of VAMS diverge in the following two cases.

- If a file is rewritten on FSx (for example, when the training preprocessing regenerates `meta/stats.json`), the change is automatically exported to the VAMS bucket. VAMS does not record a change that does not go through its own API as a version, so the asset versions managed by VAMS no longer match the contents in S3. A deletion on FSx also deletes the original VAMS data.
- If the asset is updated on the VAMS side during training, the change is imported into FSx. The training job then reads data that switched to a file of a different version partway through the read.

In addition, the FSx automatic import configures S3 event notifications on the target bucket, which overlaps with the notifications that VAMS configures on the asset bucket.

The copy specifies the VersionId at the time the input was listed.
Therefore, even if the asset is updated in VAMS during execution, training reads the version as of the time of the listing.
The copy destination is separated for each execution and is deleted when the execution finishes, so changes on the PASK side never reach the VAMS data.
The poller is not granted permission to delete from the VAMS bucket.

We also do not adopt a method that mounts the VAMS asset bucket directly on each node with Mountpoint for Amazon S3 or similar and reads it without copying.
With this method, the training job reads the latest version in the asset bucket, because Mountpoint for Amazon S3 cannot read a specified version.
Training runs for hours to days. If an asset is updated or deleted in VAMS during that time, the job reads data from a different version partway through training, so a data inconsistency can occur.
Mounting read-only does not solve this problem.
Preventing it requires various measures, such as pinning versions or detecting changes in the input before and after training.
For this reason, the data as of the execution time is copied at present, and the copy is fixed and used for the duration of training.

## How Curation Views Are Resolved

The view method of VAMS LeRobot Dataset Curation keeps the result of selecting episodes as a view asset, without copying the data.
The view asset contains only `curation_manifest.json` and `meta/stats.json` computed over the retained episodes.
The data and videos remain in the original asset, and the manifest points to the original files by their S3 versions (VersionId).
PASK resolves the original files from this manifest and trains.
The format of the messages from VAMS to PASK is the same as for a regular dataset.

The poller and the training job resolve a view in the following order.

1. In STAGING, the poller lists the latest version at the root of the input asset. If `curation_manifest.json` exists at the root, it reads the version that it listed. If `mode` is `view`, it treats the input as a view and validates the manifest.
2. For each file in `source.files` of the manifest, the poller issues `HeadObject` with the recorded version (VersionId) and confirms that the version exists and that the size matches the record. If the version does not exist, it fails the execution with the reason.
3. The poller copies the recorded versions to the bucket for each execution (the staging prefix of the PASK data bucket). It does not copy the files of the view itself. Because the copy specifies the versions, training reads the versions as of the time the view was created, even if the original asset is updated after the view is created.
4. When the import finishes, the poller writes the manifest to the execution directory as `curation_manifest.json`, and creates `view_dataset/`, the dataset root used for training. `view_dataset/meta/` is a real directory. Each file under it, and `data`, `videos`, and the other entries directly under the root, are symbolic links to the files imported into FSx. The files imported into FSx are owned by root and the training job cannot write to them, so the poller prepares a location in the execution directory where the training job can write `meta/stats.json`.
5. Before training, the training job computes the normalization statistics of the retained episodes with `compute_view_stats.py` and writes them to `view_dataset/meta/stats.json`. The numeric features (such as `observation.state` and `action`) are computed from the rows of the retained episodes with LeRobot's `get_feature_stats`. For the image and video features, the per-episode statistics in `meta/episodes` are aggregated with LeRobot's `aggregate_stats`.
6. `mlflow_train.py` passes `episodes` of the manifest to `lerobot-train` as `--dataset.episodes`. The episode numbers remain the `episode_index` of the original dataset.

The training job recomputes the normalization statistics because `lerobot-train` of LeRobot 0.6.1 uses `meta/stats.json` as is for normalization, even when episodes are specified.
The `meta/stats.json` of the view is treated only as a value for display and reference, and is not used for training.
We also do not use the statistics recomputation of LeRobot's `lerobot-edit-dataset`.
In LeRobot v3.0, a data file holds the rows of multiple episodes, so this recomputation also includes the rows of the excluded episodes in the same file in the statistics.
We also do not use openpi's `compute_norm_stats`.
It writes `norm_stats.json` for openpi training, and the training of the PASK VAMS integration (pi0 and pi0.5 by `lerobot-train` of LeRobot) does not read this file.

The episode list is not put on the command line of `slurm_train.sh`. Instead, `mlflow_train.py` adds it to the arguments inside the process, because on Linux a single argument cannot exceed 128 KiB.
With tens of thousands of episodes, the list may not fit in the arguments of `srun`.

The training job for a view is not given `HF_HUB_OFFLINE=1`.
Even if dataset files are missing, the dataset is never fetched from the Hugging Face Hub. The `repo_id` of the view dataset (`vams/<databaseId>/<assetId>`) is split into three parts, so `huggingface_hub` stops with `HFValidationError` before it communicates with the Hub.
The pretrained weights and the tokenizer are fetched from the Hub if they are not in the `LEROBOT_PI_HF_HOME` cache of the cluster, the same as in regular training.

## Data Volume Guidelines and Alternatives for Larger Inputs

The current implementation has the poller perform server-side S3 copies per object in a single process (8 objects are copied in parallel, and objects larger than 5 GiB are copied with multipart copy).
We have only verified the operation with a dataset of about 100 MB and 11 files.
If the data of a single training run becomes large, the execution may be delayed or fail for the following reasons.

- The copy time grows in proportion to the number of objects and their total size. This time is included, together with the Slurm wait and the training time, in the `taskTimeout` of the VAMS pipeline (24 hours by default, 7 days at most).
- If the poller stops during a copy, the restarted poller copies all the objects again. If it stops near the end of a long copy, all the time up to that point is wasted.
- The poller processes one execution at a time, so while a copy is in progress, the post-acceptance processing of other executions waits.
- The FSx automatic import imports only the file metadata, and the contents are read from S3 when the training job first reads them. With large data, the first epoch is slow.
- The capacity of FSx is fixed at 1200 GiB. Before copying, the poller checks whether the total of the dataset and the expected checkpoints (about 40 GiB each when `FINETUNE_MODE` is `full`) fits in the free space. If it does not fit, the poller fails the execution with `PASK.StagingFailed`.

When the input of a single training run falls into any of the following, review the copy method.
The values are guideline estimates based on the reasons above, not values measured and confirmed.

| Review guideline | Reason |
|---|---|
| The total exceeds 100 GiB | The copy and the reading of the first epoch take tens of minutes to hours, and redoing the work after an interruption costs more |
| More than 10,000 objects | Copying each object and checking the import (checking every 10 seconds whether any files are not yet imported, and their sizes) become the bottleneck |
| The total of the dataset and checkpoints approaches the free space of FSx (several hundred GiB) | The capacity check fails, or checkpoints can no longer be written during training |

The alternatives for review are as follows.
They are listed from those that require small changes.

1. Copy only the dataset folder: add a relative path inside the asset to the training parameters, and copy only what is under it. Files that training does not use (such as the original rosbags) are no longer copied.
2. Skip the objects that are already copied: if an object with the same size and ETag exists at the destination, skip the copy, so that the copy can resume after a stop.
3. Copy with S3 Batch Operations: the poller only builds a manifest from a listing, starts the job, and monitors its completion. It can handle millions of objects, and retries and the completion report are left to S3. The per-object size limit must be checked before adoption.
4. Copy with AWS DataSync: used for data at the TB scale when transfer verification and differential transfer are required.
5. Finish reading FSx before training: after the copy, load the contents with `lfs hsm_restore` or a data repository task, so that the first-epoch delay is excluded from the training time.
6. Increase the capacity of FSx, or prepare an FSx for each execution: consider this when the dataset and checkpoints do not fit in the fixed 1200 GiB.

The alternative of connecting the VAMS asset bucket directly to FSx to remove the copy is not adopted, because of the problem described in the section "Why Data Is Copied Separately from the VAMS Bucket" above.

## Permissions and Security

The poller accesses AWS with the instance role of the login node.
In a cluster that has already been created, HyperPod does not accept changes to the roles of the instance groups. Therefore, set `Vams.SeparateLoginRole` to `false`, and grant the poller's permissions through the role shared by all groups.
A separate login role can only be used when a cluster is newly created.

Besides the poller, people also SSH into the login node.
A person who SSHes into the login node can use the same permissions as the poller through the instance role.

- Read the VAMS asset bucket, and write to the outputs prefix (`*pipelines/*/output/*`) of the execution bucket
- `SendTaskSuccess`, `SendTaskFailure`, and `SendTaskHeartbeat` to the Step Functions state machines whose names start with `vams-` or match `*<ConfigName>*`
- Receive and delete messages in the PASK queue, and read and write the execution state table
- Read, write, and delete under `<S3Prefix>vams/` in the PASK data bucket

Limit the people who can SSH into the login node (the people who are allowed `ssm:StartSession` to the login node) to those who should have these permissions.
When `SeparateLoginRole` is `true`, the roles of the controller and worker nodes do not have these permissions.
Because the training job runs on the worker nodes, the training code cannot access the VAMS bucket.

When `SeparateLoginRole` is `false`, all groups use the shared role, so the controller and worker nodes, and the training code that runs on them, can also use the permissions above.
Limit the people who can SSH into any node of the cluster to those who should have the permissions above.

No deletion permission (`s3:DeleteObject`) is granted on any VAMS bucket.
The poller can delete only under `<S3Prefix>vams/` in the PASK data bucket.

In an execution whose input is a view, the poller reads the files of the original asset by specifying the recorded versions.
The permission required for reading a specified version is `s3:GetObjectVersion`.
The role of the poller has this permission on all the buckets in `Vams.AssetBucketNames`, so there is no need to change IAM for views.

PASK does not evaluate the VAMS permissions (such as the Tier 2 constraints).
It assumes that the view and the original asset are in the same database, and that the VAMS permissions apply under the same conditions.
The manifest of a view is a file of the view asset, so a person who can write to that asset can place a manifest that points to an asset of a different database in the same asset bucket.
Because the role of the poller can read the entire asset bucket, IAM alone cannot prevent this specification.
Therefore, the poller checks that `source.storage.bucket` of the manifest is the same as the bucket of the input asset, and that `source.database_id` is the same as the database of the input asset.
If they differ, the poller fails the execution with `PASK.InvalidRequest`.

The training parameters are validated with an allowlist and regular expressions, and are passed to the training job through the arguments and environment variables of `subprocess`, without going through a shell.
The pipelineExecutionId is validated for its character set (`^[A-Za-z0-9_-]+$`) before it is used in job names, S3 keys, and FSx paths.

The configuration file of the poller, `/etc/pask/vams-poller.env`, is owned by root, its group is ubuntu, and its mode is 0640.
The env file for training, `~/.config/pask/lerobot-openpi.env`, which holds `HF_TOKEN`, is readable only by ubuntu (0600).

## Current Implementation Constraints

- One execution is processed at a time (`Vams.MaxConcurrentRuns`). An execution that arrives later waits in the accepted state until the earlier execution finishes.
- The dataset must be placed at the root of the asset. An asset whose dataset is in a subfolder cannot be used for training as it is.
- Files imported into FSx are owned by root, so the training job cannot write into the dataset. A dataset whose `meta/stats.json` does not contain q01 and q99 may fail, because the quantiles cannot be computed. The output of the conversion from VAMS rosbags to LeRobot includes q01 and q99.
- The trained model is placed at the root of the output asset.
- A view points to the files of the original asset by the recorded versions. If the original asset is deleted, or the recorded version is deleted, training on that view is no longer possible (`PASK.InvalidRequest`). While training on a view is in progress, do not delete the original asset or its versions.
- The format of the view manifest matches the contract of `curation_manifest_version` 2. If VAMS changes the format of the manifest, the validation of the poller must be updated to match.
- A view accepts only datasets whose LeRobot `codebase_version` is `v3.0`. Other versions fail in the computation of the normalization statistics (`PASK.JobFailed`).
- If the files of the view dataset are missing from FSx, the training fails while loading the dataset (`PASK.JobFailed`). Because the `repo_id` is split into three parts, it stops with `HFValidationError` before it communicates with the Hugging Face Hub.
