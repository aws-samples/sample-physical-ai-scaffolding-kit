# Setting Up and Operating the VAMS Integration

This document describes how to introduce and operate the integration that lets VAMS workflows run `samples/lerobot-openpi` training jobs on the PASK HyperPod cluster.
It assumes readers who have never used VAMS, and starts from deploying VAMS to an AWS environment.

The integration design, the reasons for copying data, the guideline for data volume and alternatives, and permissions are described in [How the VAMS Integration Works](/hyperpod/docs/en/VAMS_INTEGRATION/ARCHITECTURE.md).
The formats of messages, files, and states, and the processing rules of the poller are described in [VAMS Integration Specification](/hyperpod/docs/en/VAMS_INTEGRATION/SPEC.md).

## About VAMS

**VAMS** (Visual Asset Management System) is an open-source solution for storing and versioning files such as 3D models, point clouds, videos, and robot training data within an AWS account, and for browsing them in a web browser.
It is published at [awslabs/visual-asset-management-system](https://github.com/awslabs/visual-asset-management-system) and is deployed to the user's account with AWS CDK.
Processing of files is registered as pipelines and workflows, and run from the screen or the CLI.

The VAMS terms used in this document are as follows.

| Term | Meaning |
|---|---|
| Database | A unit that groups assets. Pipelines and workflows are also registered per database |
| Asset | A collection of files. In this integration, one LeRobot dataset and one trained model each become one asset |
| Pipeline | The definition of one processing step. In this integration, it sends a message to a PASK SQS queue and waits for a completion notification from PASK |
| Template | A skeleton of the settings passed to a pipeline. In this integration, it defines the default values of the training parameters and the items that can be changed at execution time |
| Workflow | A set of pipelines arranged together, and the unit that users execute |

All functions of the integration are on the PASK side, and the VAMS code and deployment settings are not changed.
On the VAMS side, you only register the pipeline and workflow definitions bundled with PASK (`hyperpod/vams/`).

## Installation Flow

Install in the following order.

1. Confirm that the prerequisites are met (see the "Prerequisites" section).
2. Deploy VAMS (see the "Deploying VAMS" section).
3. Prepare VamsCLI (see the "Preparing VamsCLI" section).
4. Check the VAMS values used for the integration settings (see the "Checking the VAMS Values" section).
5. Set `Vams` in `cdk.json` (see the "Configuration" section).
6. Deploy the PASKStack (see the "Deployment" section).
7. Install the poller on the login node (see the "Installing the Poller on the Login Node" section).
8. Prepare a database and the input and output assets in VAMS (see the "Preparing the Database and Assets" section).
9. Register the pipeline and workflow in VAMS (see the "Registering the Pipeline" section).
10. Run training from VAMS (see the "Running Training" section).

If VAMS is already deployed, skip step 2.

## Prerequisites

On the PASK HyperPod cluster, make sure that you can run [Training pi0.5 with LeRobot](/samples/lerobot-openpi/docs/en/lerobot_pi05_training.md) manually.
Training from VAMS uses the same `slurm_train.sh` and the same container image (`lerobot-pi-train+<IMAGE_TAG>.sqsh`).
If the container image is missing, the training job fails at the moment it starts.

Training is recorded in MLflow, so set `Mlflow.Enabled` in `cdk.json` to `true` and make sure that the MLflow App of PASKStack is also available.

Deploy VAMS to the same AWS account and the same region as PASK.
The integration is designed so that the VAMS workflow role sends messages directly to the PASK queue, and it has no settings that cross accounts or regions.

On the PC you work from, prepare the following tools in addition to the tools used to deploy PASK.

| Tool | Use |
|---|---|
| Python 3.13 or later | Runs VamsCLI |
| Docker or a compatible container engine | Builds the container image in `cdk deploy` of VAMS |
| Node.js 22 or later and npm | Used for building the VAMS screen and for CDK |
| `jq` | Extracts values from definition files in the steps of this document |

## Deploying VAMS

For details on deploying VAMS, see the VAMS [Deployment Guide](https://awslabs.github.io/visual-asset-management-system/deployment/deploy-the-solution).
This section shows the minimum steps required for the integration and the values to decide for the integration.
The initial deployment takes about 30 to 45 minutes.

1. Get the repository and switch to a stable release tag.

    ```bash
    git clone https://github.com/awslabs/visual-asset-management-system.git
    cd visual-asset-management-system
    git fetch --all --tags
    git checkout tags/<release tag>
    ```

    Check the release tags on [Releases](https://github.com/awslabs/visual-asset-management-system/releases).
    The steps in this document were verified with VAMS 2.6.2 and VamsCLI 2.6.2.

2. Build the screen and install the CDK dependencies.

    ```bash
    cd web && nvm use && npm install && npm run build
    cd ../infra && npm install
    ```

3. If you have not used CDK with this account and region before, bootstrap it.

    ```bash
    cdk bootstrap aws://<account ID>/<same region as PASK>
    ```

4. Create the configuration file from the template and change the following items.

    ```bash
    cp config/config.template.commercial.json config/config.json
    ```

    | Item | Value |
    |---|---|
    | `name` | Keep the `vams` in the template. This value becomes `Vams.ConfigName` in PASK |
    | `env.region` | Same region as PASK |
    | `app.adminEmailAddress` | Email address of the first administrator. The temporary password is sent here |
    | `app.adminUserId` | User name of the first administrator |
    | `app.baseStackName` | Environment name included in the stack names (for example, `dev`) |

5. Deploy.

    ```bash
    cdk deploy --all --require-approval never
    ```

    Docker (or the container engine you configured) must be running.

6. Open the URL shown in the output `WebAppCloudFrontDistributionDomainName` in a browser, sign in with the temporary password sent by email, and set a password.

## Preparing VamsCLI

For registration and execution of the integration, use VamsCLI (`vamscli`), the command-line tool for VAMS.
VamsCLI is in `tools/VamsCLI` of the VAMS repository obtained in step 1.

1. Install VamsCLI in a virtual environment.

    ```bash
    cd visual-asset-management-system/tools/VamsCLI
    python3 -m venv .venv
    source .venv/bin/activate
    pip install -e .
    vamscli --version
    ```

2. Set the VAMS URL and sign in as an administrator.

    ```bash
    vamscli setup https://<VAMS URL>
    vamscli auth login -u <administrator user name>
    ```

For details on how to use VamsCLI, see the VAMS [CLI Reference](https://awslabs.github.io/visual-asset-management-system/cli/getting-started).

## Checking the VAMS Values

Check the VAMS values to write in `cdk.json` of PASK.

1. Check `name` in the VAMS `config.json`.
    If the template is unchanged, it is `vams`.
    Write this value in `Vams.ConfigName`.

2. List the VAMS asset buckets.

    ```bash
    vamscli database list-buckets
    ```

    Write the names of all the buckets shown in `Vams.AssetBucketNames`.
    Write the name of the default bucket (the one whose `isDefault` is `true`) in `Vams.WorkflowBucketName`.
    VAMS places the input and output of pipeline executions in `pipelines/` in the default bucket.

3. Check the encryption and versioning of the bucket.

    ```bash
    aws s3api get-bucket-encryption --bucket <bucket name>
    aws s3api get-bucket-versioning --bucket <bucket name>
    ```

    Only when the encryption is SSE-KMS, write the key ARN in `Vams.KmsKeyArns`.
    For the effect of versioning not being enabled, see the "Operational Settings Expected on the VAMS Side" section.

## Configuration

Write the VAMS integration settings in `config.Vams` of [hyperpod/cdk.json](/hyperpod/cdk.json).
If `Enabled` is `false` or if `Vams` is missing, no integration resources are created, and the stack template is the same as when there is no integration.

When `Enabled` is `true`, `ConfigName`, `AssetBucketNames`, `WorkflowBucketName`, `MlflowExperimentName`, and the numeric items cannot be omitted.
`Mlflow.Enabled` must also be `true`; otherwise `cdk synth` fails.
If a value is empty or an item is omitted, `cdk synth` and `cdk deploy` fail.
`KmsKeyArns` and `SlurmPartition` can be omitted.

| Item | `cdk.json` value | Description and how to decide the value |
|---|---|---|
| `Enabled` | `false` | Set to `true` to create the integration resources |
| `ConfigName` | `vams` | The `config.name` of the VAMS deployment. It is included in the queue name (`pask-<ConfigName>-training`). The VAMS workflow role can send messages only to queues whose names include `config.name`, so use the same value as VAMS. Only alphanumerics, `-`, and `_` can be used |
| `AssetBucketNames` | `[]` | The names of all the VAMS asset buckets, including external asset buckets. Check with `vamscli database list-buckets` |
| `WorkflowBucketName` | `""` | The bucket where VAMS places the execution manifest and outputs (`workflowExecutionS3InputOutputBucket` in the message). This is the default VAMS asset bucket |
| `SeparateLoginRole` | `true` | With `true`, the role of the login group is separated from the roles of the controller and worker groups. For a cluster that has already been created, set `false` (see the "Deployment" section). If omitted, it is the same as `true` |
| `KmsKeyArns` | `[]` | Write the key ARN only when the VAMS buckets are encrypted with SSE-KMS. Check the encryption method with `aws s3api get-bucket-encryption --bucket <bucket name>` |
| `QueueVisibilityTimeoutSeconds` | `300` | Visibility timeout of the queue (seconds). Set it to a length that covers the intake processing (reading the manifest and recording to DynamoDB) |
| `MaxReceiveCount` | `5` | The number of receives before a message that could not be accepted is moved to the DLQ |
| `HeartbeatIntervalSeconds` | `300` | The interval (seconds) for sending heartbeats to the VAMS Step Functions |
| `AbortGraceSeconds` | `1800` | The grace period (seconds) after heartbeat errors before deciding that the execution is aborted. Set it longer than the time until a new token arrives through the VAMS send retries (within 1 minute) |
| `LeaseSeconds` | `900` | The length (seconds) of the lease of the poller that processes an execution. Set it to three times `HeartbeatIntervalSeconds` |
| `ItemTtlDays` | `30` | The number of days to keep DynamoDB items of finished executions. Set it longer than the sum of the SQS retention period (14 days) and the upper limit of the pipeline `taskTimeout` (7 days) |
| `StagingExpirationDays` | `10` | The number of days after which objects under `<S3Prefix>vams/` in the PASK data bucket expire. This removes copies that were not removed by cleanup. Set it longer than the upper limit of `taskTimeout` (7 days) |
| `IngestWaitSeconds` | `3600` | The upper limit (seconds) for waiting until the copied dataset appears on FSx |
| `MaxConcurrentRuns` | `1` | The number of executions that can proceed to data copy and later at the same time. The FSx capacity is fixed at 1200 GiB, so set it to a number that fits the dataset and checkpoints |
| `SlurmPartition` | `""` | The Slurm partition to which training jobs are submitted. If empty, the Slurm default partition is used |
| `MlflowExperimentName` | `vams-training` | The MLflow experiment that records training from VAMS. Keep it separate from the experiment of manual training (`lerobot-pi`) |

`QueueVisibilityTimeoutSeconds` and `MaxReceiveCount` are set on the queue, and `StagingExpirationDays` is set on the bucket lifecycle rule.
The values other than `MaxReceiveCount` and `StagingExpirationDays` are also written as `PASK_VAMS_*` environment variables in the poller configuration file `/etc/pask/vams-poller.env` on the login node.
This configuration file is written only when the poller is installed.
After you change a value and deploy, reinstall by following the steps in the "Installing the Poller on the Login Node" section.

The following is an example of the `cdk.json` settings.
Replace `<...>` with actual values.

```json
"Vams": {
  "Enabled": true,
  "ConfigName": "<VAMS config.name>",
  "AssetBucketNames": ["<VAMS asset bucket>"],
  "WorkflowBucketName": "<VAMS default asset bucket>",
  "SeparateLoginRole": true,
  "KmsKeyArns": [],
  "QueueVisibilityTimeoutSeconds": 300,
  "MaxReceiveCount": 5,
  "HeartbeatIntervalSeconds": 300,
  "AbortGraceSeconds": 1800,
  "LeaseSeconds": 900,
  "ItemTtlDays": 30,
  "StagingExpirationDays": 10,
  "IngestWaitSeconds": 3600,
  "MaxConcurrentRuns": 1,
  "SlurmPartition": "",
  "MlflowExperimentName": "vams-training"
}
```

## Deployment

Follow the steps in [About Deployment](/hyperpod/docs/en/DEPLOYMENT.md) to deploy. Before `cdk deploy`, set `Vams.Enabled` and `Mlflow.Enabled` in `cdk.json` to `true` and write the other items.
When `Vams.Enabled` is `true` and `Vams.SeparateLoginRole` is `true`, the role of the login group is separated from the roles of the controller and worker groups.
This is so that only the poller on the login node has permissions for the VAMS buckets and Step Functions.

HyperPod does not accept changes to the ExecutionRole of an instance group that has already been created.
If you deploy with `SeparateLoginRole` set to `true` on a cluster that has already been created, `cdk diff` shows an update of `AWS::SageMaker::Cluster`, but the deployment fails with `Updating fields ExecutionRoleArn on an InstanceGroup not supported`, and the stack is rolled back.
To add the integration to a cluster that has already been created, set `SeparateLoginRole` to `false`.
In this case, the login role is not separated, all groups use the common role, and the poller's permissions are attached to that role (see "Permissions and Security" in [How the VAMS Integration Works](/hyperpod/docs/en/VAMS_INTEGRATION/ARCHITECTURE.md)).
`SeparateLoginRole` can be set to `true` only when you create a new cluster.

Before deploying, use `cdk diff` to confirm that `ExecutionRole` in `InstanceGroups` of `AWS::SageMaker::Cluster` does not change.

```bash
cdk diff
```

If `(requires replacement)` or `(may cause replacement)` appears on an `AWS::SageMaker::Cluster` resource, do not deploy.
If the cluster is recreated, the local state of the nodes and the running jobs are lost.
When you change `Vams.Enabled` from `true` back to `false`, also confirm in the same way that the role assignment does not change.

After you confirm, deploy.

```bash
cdk deploy
```

On successful deployment, the following Outputs are output in addition to the Outputs in [About Deployment](/hyperpod/docs/en/DEPLOYMENT.md).

```bash
Outputs:
PASK.HyperPodLoginExecutionRoleARN = Role ARN of the nodes in the login group
PASK.VamsVamsDeadLetterQueueUrl = URL of the DLQ where messages that could not be accepted are stored
PASK.VamsVamsQueueArn = ARN of the SQS queue to which the VAMS workflow sends messages
PASK.VamsVamsQueueUrl = URL of the SQS queue to which the VAMS workflow sends messages (the value used to register the pipeline)
PASK.VamsVamsTableName = Name of the DynamoDB table that records the execution state
```

The CloudFormation Outputs keys have an 8-character alphanumeric suffix after the names above.
The commands in this document retrieve values by matching the beginning of the key.

The DynamoDB table is not deleted even when the stack is deleted (`RemovalPolicy.RETAIN`).
If it is no longer needed, delete it manually.

## Installing the Poller on the Login Node

The poller (the systemd service `pask-vams-poller`) is installed by the login node lifecycle script `on_init_complete.sh`.
Lifecycle scripts run only when a node is created, so for the login node of a cluster that has already been created, install the poller manually.
Even for a newly created cluster, step 2, which creates the env file for training, is required.

While the env file for training `~/.config/pask/lerobot-openpi.env` does not exist, the poller fails to start and keeps restarting.
Because `~/.bashrc` is not read by systemd services, the poller reads the environment variables for the training job from this file.

Run the following steps by connecting to the login node via SSH as the ubuntu user (see "Initial Cluster Setup" in [About Deployment](/hyperpod/docs/en/DEPLOYMENT.md)).

1. Check the role of the login node.

    ```bash
    aws sts get-caller-identity --query Arn --output text
    ```

    If `SeparateLoginRole` is `true`, confirm that the role name shown is the same as the role name of `HyperPodLoginExecutionRoleARN` in the Outputs.
    If it is `false`, confirm that it is the same as the role name of `HyperPodClusterExecutionRoleARN`.

2. Run `setup.sh` with `--poller-env` to create the env file for training.

    ```bash
    cd ~/sample-physical-ai-scaffolding-kit/samples/lerobot-openpi/training
    bash setup.sh --hf-token "hf_xxxxx" --poller-env
    source ~/.bashrc
    ```

    `setup.sh` expands the same values that it writes to `~/.bashrc`, and then writes them to `~/.config/pask/lerobot-openpi.env` (permission 0600).
    Other options such as `--hf-token` are the same as in [Training pi0.5 with LeRobot](/samples/lerobot-openpi/docs/en/lerobot_pi05_training.md).

3. Get the installation script and the poller source from the lifecycle script bucket, and run them.

    ```bash
    LIFECYCLE_BUCKET="$(aws cloudformation describe-stacks --stack-name PASK \
      --query "Stacks[0].Outputs[?starts_with(OutputKey, 'HyperPodLifeCycleScriptBucketName')].OutputValue | [0]" \
      --output text)"
    WORK_DIR="$(mktemp -d)"
    aws s3 cp "s3://${LIFECYCLE_BUCKET}/install_vams_poller.sh" "${WORK_DIR}/"
    aws s3 cp --recursive "s3://${LIFECYCLE_BUCKET}/vams_poller/" "${WORK_DIR}/vams_poller/"
    sudo bash "${WORK_DIR}/install_vams_poller.sh"
    ```

    `install_vams_poller.sh` assumes that `vams_poller/` is in the same directory.
    The configuration values in the script (such as the queue URL) are embedded at deployment time.
    The script performs the following processing.

    1. Copy the poller source to `/opt/pask/vams-poller/`, create a venv, and install it.
       The `python3` of the HyperPod image is older than the poller supports, so a pinned uv installs CPython 3.14 to `/opt/pask/python/` and the venv is created with that Python. uv and CPython are downloaded from GitHub.
    2. Write the configuration file `/etc/pask/vams-poller.env`.
    3. Write `pask-vams-poller.service`, enable it, and restart it.

    If it is run on a node other than the login group, or if `Vams.Enabled` is not `true`, it does nothing.
    The result is the same no matter how many times it is run.

4. Confirm that the poller is running.

    ```bash
    systemctl status pask-vams-poller
    journalctl -u pask-vams-poller -f
    ```

    If `systemctl status` shows `active (running)` and `journalctl` shows no continuing errors, the installation is complete.
    If startup fails, check the cause with `journalctl -u pask-vams-poller`, fix it, and then redo step 3.

When you recreate the login node, the lifecycle script installs the poller.
The env file for training is in the home directory on FSx, so it remains even after recreation.
Even if the installation by the lifecycle script fails, node creation does not fail.
If the poller is not running on a new login node, redo steps 3 and 4.

## Preparing the Database and Assets

Training uses one VAMS database and two assets in it.
Pipelines and workflows are also registered in the same database, so complete this section first.

1. Create a database.

    ```bash
    DATABASE_ID="<database ID (for example, robot-datasets)>"
    vamscli database create -d "${DATABASE_ID}" --description "<description>"
    ```

    If you are asked for the bucket in which to place the assets, choose the default bucket.

2. Create an input asset and upload a LeRobot v3.0 dataset.

    ```bash
    vamscli assets create -d "${DATABASE_ID}" --name "<dataset name>" --description "<description>"
    vamscli file upload -d "${DATABASE_ID}" -a "<input asset ID>" --directory <dataset directory> --recursive
    ```

    The asset ID is shown in the output of `assets create`.
    For `--directory`, specify the directory that directly contains `meta/info.json`.

3. Create an output asset into which the trained model is placed.
    Leave its contents empty.

    ```bash
    vamscli assets create -d "${DATABASE_ID}" --name "<model name>" --description "<description>"
    ```

The input asset is an asset whose root contains a LeRobot dataset.
The root contains `meta/info.json`, and `meta/`, `data/`, `videos/`, and so on are under it.
The poller copies the entire asset to the PASK data bucket and passes its root to the training job as `DATASET_ROOT`.
For an asset whose dataset is in a subdirectory of the asset (for example, `lerobot/<dataset name>/`), the training job fails because it cannot read the dataset.
If the asset contains files other than the dataset (such as MCAP files before conversion), they are also copied and use the FSx capacity.
VAMS folder markers (keys ending in `/`) and preview files (keys containing `.previewFile.`) are not copied.

If VAMS has the LeRobot Dataset Curation pipeline, a view asset created with it can also be used as the input. This pipeline is not included in some VAMS versions.
A view asset has `curation_manifest.json` (with `mode` set to `view`) at its root, and it has no data or videos.
The poller copies the file versions of the original asset recorded in the manifest, and trains only on the episodes in the manifest's `episodes`.
When you use a view as the input, observe the following two points.

- Place the view and the original asset in the same database. If the manifest points to a database or bucket that is different from the input asset, the poller fails with `PASK.InvalidRequest`.
- Do not delete the original asset or the versions recorded by the view until training is complete. If a recorded version does not exist, the poller fails with `PASK.InvalidRequest`.

The flow for resolving a view is described in the "How Curation Views Are Resolved" section of [How the VAMS Integration Works](/hyperpod/docs/en/VAMS_INTEGRATION/ARCHITECTURE.md).

The output asset must be a different asset from the input.
The poller writes the trained model files back to the root of the output asset.
If the output asset is the same as the input, the model files are placed at the dataset root, and the next training run on the same asset copies them as part of the dataset.

## Registering the Pipeline

To register with VAMS, use the following definitions bundled with PASK.

| File | Content |
|---|---|
| [hyperpod/vams/pipeline.json](/hyperpod/vams/pipeline.json) | Pipeline name, `executionConfig`, `systemConfig` |
| [hyperpod/vams/template.json](/hyperpod/vams/template.json) | Template of the training parameters (`configBody`) and tag definitions (`tagSchema`) |
| [hyperpod/vams/workflow.json](/hyperpod/vams/workflow.json) | Workflow name and `systemConfig` |

VamsCLI commands take values for each item, such as `executionConfig` and `systemConfig`, not the whole file.
In the following steps, each item is extracted with `jq` and passed.

### Definition Contents

The pipeline uses the SQS execution type (`executionType: SQS`) and waits until a TaskToken is returned (`waitForCallback: Enabled`).
`sqs.queueUrl` is left empty in the file, and the value of `VamsVamsQueueUrl` in the Outputs is entered at registration.
If you register it while it is empty, VAMS rejects it with `sqs.queueUrl is required`.

`taskHeartbeatTimeout` is left empty.
If a value is set, heartbeats stop while the poller is stopped, and the VAMS step fails even though training continues.

`taskTimeout` is set to `432000` seconds (5 days).
After this time, VAMS fails the step, and the poller stops the training job as an abort from the next heartbeat error.
Heartbeats do not extend `taskTimeout`, so it must be longer than the total time that one execution takes.
The total includes the following times.

| Time | Estimated length |
|---|---|
| Training | Up to 48 hours (`#SBATCH --time=48:00:00` in `slurm_train.sh`). Recalculation of the quantiles is also done inside the job, so it is included in this time |
| Data copy and ingestion into FSx | The upper limit of the wait for ingestion is 1 hour (`IngestWaitSeconds`) |
| Write-back | It depends on the size of the checkpoints, but it is expected to fit within 1 hour |
| Time waiting for the end of a preceding execution | If `MaxConcurrentRuns` is 1, up to 50 hours for one preceding execution |
| Waiting in the Slurm queue | The time until a worker node becomes free |

Training, copying, and write-back take about 50 hours in total. Adding the time to wait for one preceding execution makes about 100 hours, and the remaining about 20 hours is allocated to waiting in the Slurm queue.
The VAMS upper limit is 604800 seconds (7 days).
In operations that submit three or more executions at the same time, the waiting time increases, so increase the value within the upper limit.
However, if `taskTimeout` is made longer, the executions of messages that entered the DLQ also keep waiting on the VAMS side for that time.

With `inputFileArity: one` and `assetScope` in `systemConfig`, the input is limited to the whole of one asset.
`metadataInputs.assetMetadata` is set to `true` so that asset names and so on are recorded in MLflow.
`requireTemplate` is set to `true` and `allowCustomTemplateOverride` is set to `false`, so the training parameters are received only through the template tags.

The `configBody` of the template is flat JSON in which the environment variable names of the training parameters are the keys and the tags (such as `{{STEPS}}`) are the values.
The list of keys was copied from the poller's allowlist (`ALLOWED_TRAINING_PARAMS` in [messages.py](/hyperpod/vams-poller/src/pask_vams_poller/messages.py)).
The default values of the tags are the same as the defaults in [slurm_train.sh](/samples/lerobot-openpi/training/slurm_train.sh).
If you change the allowlist or the defaults in `slurm_train.sh`, change `configBody` and `tagSchema` of `template.json` in the same way.
If a key that is not in the allowlist remains in the template, the poller fails all executions with `PASK.InvalidRequest`.

In `configBody`, write string tags inside quotation marks, and write integer and boolean tags (such as `{{STEPS}}` and `{{COMPILE_MODEL}}`) without quotation marks.
VAMS replaces unquoted tags with JSON integers and booleans according to the tag type.
The poller does not accept numbers that are not integers, so the type of numeric tags is `integer`, not `number`.
Every tag has a default value and `required` is set to `true`, so the settings JSON that VAMS creates always contains all the keys.

The workflow allows the input of the whole asset (`wholeAssetAllowed: true`), and allows the output asset to be changed at execution time (`outputTarget.allowOverride: true`).

### Registration Steps

Run the following commands in the `hyperpod` directory, with VamsCLI authenticated.
Register the pipeline, template, and workflow in the same VAMS database as the assets used for training.

1. Set the values used for registration in variables.
    `DATABASE_ID` is the ID of the database created in the "Preparing the Database and Assets" section.

    ```bash
    DATABASE_ID="<VAMS database ID>"
    PIPELINE_ID="pask-lerobot-openpi-training"
    WORKFLOW_ID="pask-lerobot-openpi-training"
    TEMPLATE_ID="$(jq -r .templateId vams/template.json)"
    QUEUE_URL="$(aws cloudformation describe-stacks --stack-name PASK \
      --query "Stacks[0].Outputs[?starts_with(OutputKey, 'VamsVamsQueueUrl')].OutputValue | [0]" \
      --output text)"
    ```

    `PIPELINE_ID` and `WORKFLOW_ID` may be changed if they are 3 to 63 characters of alphanumerics, `-`, and `_`.
    VAMS puts `PIPELINE_ID`, not the pipeline name, in `auxTempPrefix` of the manifest, so this value goes into the MLflow tag `vams.pipeline_name`.

2. Register the pipeline.

    ```bash
    vamscli pipeline create -d "${DATABASE_ID}" -p "${PIPELINE_ID}" \
      -n "$(jq -r .pipelineName vams/pipeline.json)" \
      --category "$(jq -r .category vams/pipeline.json)" \
      --description "$(jq -r .description vams/pipeline.json)" \
      --execution-config "$(jq -c --arg url "${QUEUE_URL}" '.executionConfig | .sqs.queueUrl = $url' vams/pipeline.json)" \
      --system-config "$(jq -c .systemConfig vams/pipeline.json)"
    ```

3. Register the template.

    ```bash
    vamscli pipeline template create -d "${DATABASE_ID}" -p "${PIPELINE_ID}" -t "${TEMPLATE_ID}" \
      -n "$(jq -r .templateName vams/template.json)" \
      --description "$(jq -r .description vams/template.json)" \
      --config-format json \
      --config-body "$(jq -r .configBody vams/template.json)" \
      --tag-schema "$(jq -c .tagSchema vams/template.json)" \
      --input-instructions "$(jq -r .inputInstructions vams/template.json)" \
      --default
    ```

    Do not add `--allow-custom-edit`.
    If you add it, the settings JSON can be rewritten at execution time.

4. Register the workflow.

    ```bash
    vamscli workflow create -d "${DATABASE_ID}" -w "${WORKFLOW_ID}" \
      -n "$(jq -r .workflowName vams/workflow.json)" \
      --category "$(jq -r .category vams/workflow.json)" \
      --description "$(jq -r .description vams/workflow.json)" \
      --pipeline "${DATABASE_ID}:${PIPELINE_ID}:${TEMPLATE_ID}" \
      --system-config "$(jq -c .systemConfig vams/workflow.json)"
    ```

5. Check the `systemConfig` of the workflow.

    ```bash
    vamscli workflow get -d "${DATABASE_ID}" -w "${WORKFLOW_ID}" --json-output
    ```

    Confirm that `systemConfig.assetScope.wholeAssetAllowed` and `systemConfig.outputTarget.allowOverride` are `true`.
    It has been verified that a workflow created without `systemConfig` has `wholeAssetAllowed` set to `false`.
    If it is `false`, update it with the following command.

    ```bash
    vamscli workflow update -d "${DATABASE_ID}" -w "${WORKFLOW_ID}" \
      --system-config "$(jq -c .systemConfig vams/workflow.json)"
    ```

## Running Training

When running training from VAMS, select the whole dataset asset or the whole curation view asset as the input, and select the trained model asset as the output (see the "Preparing the Database and Assets" section).
An execution that selects only one file of an asset, or an execution that selects a folder, fails with `PASK.InvalidRequest`.
If a view is the input, do not delete the original asset or the versions recorded by the view until training is complete.

The following is an example of execution with VamsCLI.
Specify only the training parameters you want to change in `templateTags`.
Tags that are not specified take the template defaults.

```bash
vamscli workflow execute --workflow-database-id "${DATABASE_ID}" -w "${WORKFLOW_ID}" \
  --input-file "${DATABASE_ID}:<input asset ID>:/" \
  --output-asset-id "<output asset ID>" \
  --pipeline-parameters "$(jq -cn --arg pipeline "${PIPELINE_ID}" --arg template "${TEMPLATE_ID}" \
    '{($pipeline): {templateId: $template, templateTags: [{key: "STEPS", value: 1000}]}}')"
```

The trailing `/` in `--input-file` represents the whole asset.
To run from the VAMS screen, on the input asset's screen, select the workflow `pask-lerobot-openpi-training` under "Execute Workflow", and specify the whole asset as the input and the output asset as the output.

Check the execution state with the VAMS execution details (`vamscli execution details <execution ID>`).
On the cluster, the training job is submitted to Slurm with the name `vams-<pipelineExecutionId>`.

```bash
squeue --name "vams-<pipelineExecutionId>"
```

The number of executions that can proceed at the same time to data copy and later is up to `MaxConcurrentRuns`.
An execution beyond the limit waits until the cleanup of the preceding executions is complete.
Heartbeats continue to be sent while waiting, so the execution does not fail as long as it is within `taskTimeout`.

If you abort an execution in VAMS, the poller stops the training job with `scancel` after the grace period (`AbortGraceSeconds`) has passed.

## Training Output

For the training output (the files placed in the output asset, `pask_training.json` in results, and the MLflow records) and for deletion after the execution, see the "What Users Specify and Receive" and "Overall Flow" sections of [How the VAMS Integration Works](/hyperpod/docs/en/VAMS_INTEGRATION/ARCHITECTURE.md).
The write-back rules are in the "Completion Notification and Write-Back" section of [VAMS Integration Specification](/hyperpod/docs/en/VAMS_INTEGRATION/SPEC.md), and the list of MLflow tags is in the "What Is Recorded to MLflow" section of the same document.

## Operational Settings Expected on the VAMS Side

The following VAMS settings cannot be changed from PASK.
Configure them as follows on the side that operates VAMS.

1. Enable asset metadata (`assetMetadata`) in the workflow's `metadataInputs`.
    If it is disabled, the asset name, description, and tags are not included in `metadata.json`.
    The tag `vams.input.asset_name` is not attached to the MLflow run, and the dataset name becomes the assetId.
    The bundled `pipeline.json` and `workflow.json` have it enabled.
2. Enable versioning on the asset bucket.
    If it is disabled, training still runs, but the version of the copied input cannot be fixed.
    `versioned` in `input_manifest.json` and `vams_context.json` becomes `false`, and `versionId` becomes `"null"`.
    Check the versioning state with `aws s3api get-bucket-versioning --bucket <bucket name>`.
3. Register the pipeline with the bundled template and tag definitions, and set `allowCustomTemplateOverride` to `false`.
    If it is set to `true`, an arbitrary settings JSON can be passed at execution time.
    The poller rejects keys that are not in the allowlist, but in that case the failure is known only after the execution in VAMS, not before the training job is submitted.

## Checking the DLQ

Messages that the poller could not accept enter the DLQ after they have been received `MaxReceiveCount` (5) times.
The messages that enter the DLQ are those whose TaskToken cannot be read from the body, and those that keep failing to be recorded in DynamoDB.
For an execution whose message is in the DLQ, no result is returned from PASK, so VAMS keeps waiting until `taskTimeout` (5 days in the bundled definitions).

Check the DLQ messages from a local PC, using AWS credentials that have permission to read the DLQ.
The login node role does not have DLQ permissions.

1. Get the DLQ URL.

    ```bash
    DLQ_URL="$(aws cloudformation describe-stacks --stack-name PASK \
      --query "Stacks[0].Outputs[?starts_with(OutputKey, 'VamsVamsDeadLetterQueueUrl')].OutputValue | [0]" \
      --output text)"
    ```

2. Check the number of messages.

    ```bash
    aws sqs get-queue-attributes --queue-url "${DLQ_URL}" \
      --attribute-names ApproximateNumberOfMessages
    ```

3. Check the message body.

    ```bash
    aws sqs receive-message --queue-url "${DLQ_URL}" \
      --max-number-of-messages 10 --visibility-timeout 60 \
      --attribute-names All
    ```

    `body.workflowExecutionId` in the body is the ID of the VAMS execution.
    Check the reason the message was not accepted in the poller log at that time (`journalctl -u pask-vams-poller` on the login node).
    The body contains a TaskToken.
    With a TaskToken and the `states:SendTask*` permission, the VAMS step can be ended, so do not paste the body into tickets or chats.

4. If you do not need the VAMS execution to keep waiting, abort the execution in VAMS and delete the DLQ message.

    ```bash
    vamscli execution abort "<workflowExecutionId>"
    aws sqs delete-message --queue-url "${DLQ_URL}" --receipt-handle "<ReceiptHandle>"
    ```

    `<ReceiptHandle>` is the value of `ReceiptHandle` in the output of step 3.

The state of an execution after it has been accepted can be checked with the item in the DynamoDB table.

```bash
TABLE_NAME="$(aws cloudformation describe-stacks --stack-name PASK \
  --query "Stacks[0].Outputs[?starts_with(OutputKey, 'VamsVamsTableName')].OutputValue | [0]" \
  --output text)"
aws dynamodb get-item --table-name "${TABLE_NAME}" \
  --key '{"pipelineExecutionId": {"S": "<pipelineExecutionId>"}}'
```

The meaning of the `state` values is in the "State Transitions" section of [VAMS Integration Specification](/hyperpod/docs/en/VAMS_INTEGRATION/SPEC.md).

## Notes on Login Node Permissions

Besides the poller, people also SSH into the login node.
Anyone who has SSHed into the login node can use the same permissions as the poller.
The list of permissions and the differences due to `SeparateLoginRole` are described in the "Permissions and Security" section of [How the VAMS Integration Works](/hyperpod/docs/en/VAMS_INTEGRATION/ARCHITECTURE.md).
