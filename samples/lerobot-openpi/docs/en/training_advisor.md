# Proposing the Next Training Parameters from MLflow Training Results

This document describes how the Claude Code project skill `pask-training-advisor` diagnoses training runs of this sample that were recorded to the MLflow App, proposes the parameters for the next training, and writes a report.
It explains the mechanism, the diagnosis criteria, the form and validation of proposals, and how to read the report.
For how to use the commands and the procedure, see the [skill's procedure document](../../../../.claude/skills/pask-training-advisor/SKILL.md).
For the meaning of the params, tags, and metrics recorded to MLflow, see [Training Tracking with MLflow](mlflow_tracking.md).

## Overview

The inputs for the diagnosis are the training results already recorded to MLflow (the history of metrics, params, and tags) and, when a connection to VAMS is available, the metadata of the datasets used for training (number of episodes, quality flags, and reviews).
The skill does not perform closed-loop evaluation in a simulation environment.
For this reason, the diagnosis shows the trend of the loss on the training data, not whether the robot's behavior is good or bad.

Proposals are output as a JSON file (`vams_parameters.json`) that can be passed as is to `pipelineExecutionParameters` (`templateId` and `templateTags`) of the VAMS execute API.
The skill does not run the proposed training.
A person reviews the proposal and the report, and after approving them, the person runs the training from VAMS.

For the datasets used for training, the report lists the episodes and datasets to exclude from the next training.
These are not proposals for deletion; they are proposals for a person to exclude them from the input of the next training in VAMS LeRobot Dataset Curation.
The skill does not delete VAMS assets, and it does not create views.

The report requires the MLflow training results.
If only VAMS is reachable, or if neither is reachable, the process stops without creating a report.
This is because the dataset used for training is identified by following the tags of the MLflow run, so it cannot be identified without MLflow.

The processing is divided between a Python tool that performs deterministic processing and Claude Code, which is responsible for judgment.
The tool checks the connection targets, fetches data from MLflow and VAMS, computes features and diagnosis labels, extracts exclusion candidates, validates proposals and converts them to the VAMS format, generates figures, and saves outputs as MLflow artifacts.
Claude Code decides which of the 12 items to change and how, writes the rationale, and writes the report.
The reason numerical computation and constraint validation are left to the tool is to prevent Claude Code from recomputing numbers or proposing values that VAMS would reject.

## Overall Flow

```mermaid
flowchart LR
  X[MLflow App and VAMS] -->|detect| Y{mode}
  Y -->|full / mlflow_only| A
  Y -.vams_only / none.-> Z[Stop]
  A[MLflow App<br/>params / metrics / tags] -->|fetch| B[snapshot.json]
  B -->|fetch-vams| V[datasets.json]
  B -->|analyze| C[diagnosis.json]
  V -->|analyze| C
  C --> D[Claude Code<br/>decides the next parameters]
  D --> E[proposal.json]
  E -->|validate-proposal| F[vams_parameters.json]
  B -->|plot| P[PNG figures]
  B --> R[Claude Code<br/>writes the report]
  C --> R
  F --> R
  P --> R
  R --> G[report.md]
  G -.optional.->|publish| M[MLflow artifacts]
  G -.after approval.-> H[A person runs it in VAMS]
```

1. `detect`: Checks whether MLflow and VAMS can be connected, and returns one of `full` (both), `mlflow_only`, `vams_only`, or `none`. With `vams_only` and `none`, the process does not proceed further.
2. `fetch`: Fetches the target run and the comparison runs from MLflow and saves them to `snapshot.json`.
3. `fetch-vams` (for `full`): Reads the metadata of the VAMS assets used by the runs with VamsCLI and saves it to `datasets.json`.
4. `analyze`: Computes features from `snapshot.json` and writes the diagnosis labels and findings to `diagnosis.json`. If `datasets.json` exists, it also writes the dataset diagnosis and exclusion candidates.
5. Claude Code reads `diagnosis.json` and the constraints, and writes the values and rationale for the following 12 items to `proposal.json`.
6. `validate-proposal`: Validates `proposal.json` and writes `vams_parameters.json` only if it passes. If it does not pass, Claude Code corrects `proposal.json` and repeats until it passes.
7. `plot`: Creates the learning curve figures from `snapshot.json`.
8. Claude Code reads the JSON and figures and writes `report.md`.
9. `publish` (optional): Saves `report.md`, the figures, and the JSON to the artifacts of the target run.

Among the subcommands from `analyze` onward, only `publish` connects to MLflow.
If `snapshot.json` and `datasets.json` exist, the diagnosis and report can be reproduced even in an environment that cannot connect to MLflow and VAMS.

Unless the user specifies them, the comparison runs are chosen from the experiments `vams-training` and `lerobot-pi`: up to 5 runs with status `FINISHED` that used the same dataset as the target run.
Whether the dataset is the same is determined by the tag `vams.dataset_digest`, and for runs without this tag, by the tag `lerobot.dataset_repo_id`.
The experiments and the number of runs can be changed by the user.

## Metrics Used

| Metrics | Use in the diagnosis |
|---|---|
| `train/loss` | Judging convergence, plateau, divergence, and noise |
| `train/grad_norm` | Judging divergence and spikes |
| `train/lr` | Progress of the learning rate schedule |
| `train/update_s`, `train/dataloading_s`, `train/samples_per_s` | Whether data loading limits the training speed |
| `train/gpu_mem_gb` | GPU memory usage |
| `train/epochs`, `train/samples`, `train/episodes` | How many passes over the dataset were made, and an estimate of the dataset size |
| `checkpoint_step` | Position of the saved checkpoints |
| `system/gpu_0_utilization_percentage`, `system/gpu_0_memory_usage_percentage` | GPU utilization and memory headroom |
| `eval/*` | Evaluation loss. Only for runs where it is recorded |

The 12 item values are restored from the params (`policy.type`, `steps`, `batch_size`, `peft.r`, and so on) and the tags (`lerobot.finetune_mode`, `lerobot.pretrained_path`, `pask.image_tag`, and so on).
For a resumed run, the params `steps` keeps its first value, so the planned total number of steps is treated as the larger of the params `steps` and the last step in the metrics, and this is noted in the `snapshot.json` annotations.

The number of frames and episodes in the dataset is not recorded in MLflow, so it is estimated from `train/samples / train/epochs` and `train/episodes / train/epochs`.

## Diagnosis Criteria

For each run, `analyze` attaches every one of the following labels that applies.
Curve-shape labels (`diverging`, `unstable`, `converging`, `plateau`, `overfit_risk`) are not attached to a run that has `insufficient_data`.
This is because features such as the tail slope have no meaning when there are not enough points to judge.
GPU memory labels (`gpu_memory_headroom`, `gpu_memory_tight`) are not attached to a run that has no recorded points for `train/loss`.
System metrics recorded before training starts do not represent the memory required for training.

| Label | Meaning | Main conditions (threshold key in parentheses) |
|---|---|---|
| `insufficient_data` | Not enough points to judge | Number of recorded points for `train/loss` is below the minimum (`minPoints`) |
| `failed_or_killed` | The run stopped partway | Status is `FAILED` or `KILLED`, or the completion ratio against the planned steps is below the minimum (`minCompletionRatio`) |
| `diverging` | Diverging | `train/loss` or `train/grad_norm` contains NaN or inf, or the slope of `log(loss)` over the tail exceeds the threshold and is positive, and the relative improvement over the tail is negative (`divergingTailSlopePer1k`) |
| `unstable` | Unstable | The number of spikes or the coefficient of variation over the tail exceeds the threshold (`spikeFactor`, `maxSpikes`, `maxTailCv`) |
| `converging` | Still decreasing | The slope over the tail is negative, and the relative improvement over the tail exceeds the threshold (`convergingTailImprovement`) |
| `plateau` | Has leveled off | Not `diverging`, and the relative improvement over the tail is below the threshold (`plateauTailImprovement`) |
| `overfit_risk` | Possible overfitting | `converging`, and the final `train/epochs` exceeds the threshold (`overfitEpochs`). If `eval/eval_loss` is recorded, it is also attached when the run is `converging` and the slope of the evaluation loss over the tail is positive |
| `dataloader_bound` | Data loading limits the training speed | The median of `train/dataloading_s / train/update_s` exceeds the threshold (`dataloaderBoundRatio`) |
| `gpu_memory_headroom` | GPU memory has headroom | The maximum of `system/gpu_0_memory_usage_percentage` is below the threshold (`gpuMemoryHeadroomPercent`) |
| `gpu_memory_tight` | GPU memory is tight | The maximum of `system/gpu_0_memory_usage_percentage` exceeds the threshold (`gpuMemoryTightPercent`) |

The tail slope and the relative improvement are computed over a window at the end of the recorded points (`tailWindowFraction`).
The slope is the value per 1000 steps (`tail_slope_per_1k`) obtained by linear regression of `log(loss)` against the step, and the relative improvement (`tail_improvement`) is obtained from the ratio of the medians of the first half and the second half of the tail window.

The thresholds are in [`diagnosis_thresholds.json`](../../../../.claude/skills/pask-training-advisor/scripts/config/diagnosis_thresholds.json), and the meaning of each key is written in the `_comments` of the same file.
The tail window is 25% of the recorded points, and the threshold of the tail relative improvement that separates `converging` from `plateau` is 0.01.
For a run with about 30 recorded points or fewer, the tail window is short and the curve-shape labels are more easily affected by noise.
You can change the thresholds by editing the file or by passing a different file to `analyze`.
`diagnosis.json` also keeps the thresholds used for the judgment, so the basis of the judgment can be checked later.

Overfitting cannot be judged directly unless `eval/*` is recorded.
`train/loss` is the loss on the training data, and it keeps decreasing even when the model is fitted too closely to the training data.
The default settings of this sample do not run evaluation, so `overfit_risk` is a proxy judgment based on the number of passes over the data (`train/epochs`).

## Diagnosis of the Datasets Used for Training

`fetch-vams` reads only the assets used by the target run and the comparison runs.
The targets are the run's input (tag `vams.input.asset_id`) and, when the input is a curation view, the original dataset of the view (tag `vams.source.asset_id`).
Other assets in the database are not read.
VamsCLI uses only read commands with the saved login, and the profile is specified with `--vams-profile` or the environment variable `VAMS_CLI_PROFILE`.

For each asset, `fetch-vams` saves the following information to `datasets.json`.

| Information | Source |
|---|---|
| Scale (number of episodes, number of frames, duration, fps, tasks) | Asset metadata `lerobot_total_*`, `lerobot_fps`, `lerobot_tasks` |
| Quality (number of episodes with flags and the breakdown of flags) | Asset metadata `lerobot_quality_*`. Written by the VAMS LeRobot Episode Quality pipeline |
| Review (judgment and exclusion for each episode) | Asset metadata `lerobot_review_episodes`. Written by the review on the VAMS screen |
| Statistics (presence of `meta/stats.json` and agreement of the frame count) | Asset metadata `lerobot_stats_*` |
| Episodes kept and excluded by the view | Run artifact `vams/curation_manifest.json` |
| Status of all runs that used the same asset | MLflow run search |

`analyze` writes the following candidates from `datasets.json` to `datasets` in `diagnosis.json`.

| Candidate | Reason | Rule |
|---|---|---|
| Dataset (do not use as input for the next training) | `only_failed_runs` | All runs that used the asset as input are `FAILED` or `KILLED` |
| | `not_lerobot` | It is neither a LeRobot dataset nor a view |
| | `stats_missing` | The normalization statistics are missing |
| | `stats_count_mismatch` | The frame count in the statistics does not match the actual frame count |
| | `few_episodes` | The number of episodes is at or below the threshold (`fewEpisodesMax`, default 1) |
| Episode (exclude from the episodes kept by the view) | `review_exclude` | The review has `exclude` |
| | `review_failure` | The review judgment is `failure` |

An episode is a candidate only if the run has not yet used it for training.
When the input is a view, episodes that the view has already excluded are not made candidates; they are kept separately in `alreadyExcludedEpisodes`.
The thresholds are in [`dataset_thresholds.json`](../../../../.claude/skills/pask-training-advisor/scripts/config/dataset_thresholds.json).

The quality flags are handled only as counts, and a flag alone does not make an episode a candidate.
The per-episode flags are in the asset's quality file (`meta/vams_episode_quality.parquet`), and the metadata has only counts.
Also, the flags are for triage and do not represent the success or failure of an episode.
When an episode is flagged and has no review, the report recommends reviewing it on the VAMS screen.

The dataset diagnosis is also used for the judgment of the next parameters.
For example, when `overfit_risk` is attached to an input with `few_episodes`, the proposal gives priority to changing the input to a dataset or view with more episodes rather than increasing `STEPS`.

## Proposal Format and Validation

The targets of the proposal are the following 12 items that can be passed to training from VAMS.

`POLICY_TYPE`, `PRETRAINED_PATH`, `FINETUNE_MODE`, `LORA_R`, `LORA_ALPHA`, `STEPS`, `BATCH_SIZE`, `SAVE_FREQ`, `LOG_FREQ`, `NUM_WORKERS`, `COMPILE_MODEL`, `IMAGE_TAG`

In `proposal.json`, Claude Code writes the values of all 12 items, the rationale and basis for each item changed from the previous run (the names of the diagnosis labels and the values of the features in `diagnosis.json`), the expected effect, the risks, and alternatives.
For a guide to the correspondence between labels and the 12 items, see [tuning-guide.md](../../../../.claude/skills/pask-training-advisor/references/tuning-guide.md).
The guide is a recommendation. Even when Claude Code makes a judgment that differs from the guide, the rationale and basis remain in `proposal.json`.

`validate-proposal` validates the proposal with two levels of constraints.

| Constraint | Content | Where it is defined |
|---|---|---|
| Format constraints | All 12 items are present; no keys outside the allowlist or in the deny list; values match the regular expressions of the poller's allowlist; types and enum values match `tagSchema` | `ALLOWED_TRAINING_PARAMS` of the poller (`hyperpod/vams-poller`), `tagSchema` in [`template.json`](../../../../hyperpod/vams/template.json) |
| Operational constraints | Value ranges; relationships between items (`SAVE_FREQ <= STEPS`, `LOG_FREQ <= SAVE_FREQ`, combinations of `POLICY_TYPE` and `PRETRAINED_PATH`); the maximum number of items changed at once; the maximum change factor for each item; items fixed to the previous run's values | [`proposal_constraints.json`](../../../../.claude/skills/pask-training-advisor/scripts/config/proposal_constraints.json) |

A proposal that does not satisfy the format constraints is rejected by the poller even when run in VAMS.
Because the validation imports the poller's allowlist rather than copying it, changing the allowlist changes the validation rules in the same way.

The operational constraints are there to keep proposals within a range that is reasonable for the environment of this sample.
With the current values, at most 3 items can be changed at once, integer items can change by at most 4 times, and `IMAGE_TAG`, `PRETRAINED_PATH`, and `POLICY_TYPE` are fixed to the values of the previous run.
A proposal with `FINETUNE_MODE` set to `expert_only` or `full` passes, but a warning is issued that it may not fit in a 24 GB GPU (see [GPU Memory and Finetuning Method Guide](lerobot_pi05_training.md#6-gpu-memory-guidance)).
Alternatives (`alternatives`) are also validated with the same two levels of constraints.
A violation in an alternative does not stop the proposal; it is issued as a warning.
The `FINETUNE_MODE` warning is also issued for alternatives in the same way.
The lower bounds of `STEPS` and `SAVE_FREQ` are set to 1, the same as in the poller's allowlist.
This is so that a proposal that keeps the previous run's value, or extends it within the change factor limit, also passes for a short run for operation checks such as `STEPS=20`.

Validation also checks that the list of changes written in `proposal.json` agrees with the actual differences in values.
Validation fails if the previous value written in the change list differs from the previous run's value, or if an item not in the list has changed from the previous run.

All 12 items, including the items that are not changed, are included in `templateTags` of `vams_parameters.json`.
In VAMS, a tag not specified in `templateTags` takes the template's default value ([Running Training in the VAMS Integration guide](../../../../hyperpod/docs/en/VAMS_INTEGRATION/OVERVIEW.md#running-training)).
If only the changed items are included, items that were set to a non-default value in the previous run would silently revert to the template defaults.

```json
{
  "<pipelineId>": {
    "templateId": "pask-lerobot-openpi-default",
    "templateTags": [{"key": "POLICY_TYPE", "value": "pi05"}, {"key": "STEPS", "value": 60000}]
  }
}
```

The example above abbreviates `templateTags` to 2 items.
The actual file contains all 12 items.
There is no default value for pipelineId; the user specifies it when running `validate-proposal`.

## How to Read the Report

`report.md` is written by Claude Code, and its language follows the language the user uses in the conversation.
Commands, parameter names, metric names, and diagnosis labels are written in their original form regardless of language.
The axis names and legends of the figures are in English.
Numbers are copied from the JSON values as they are; they are not recomputed by Claude Code.

The report consists of the following 8 sections.
The section names and the JSON items shown in each section are in [report-template.md](../../../../.claude/skills/pask-training-advisor/references/report-template.md).

| Section | What to look at |
|---|---|
| Summary of the target run | Run name, run ID, experiment, status, start and end times (UTC and JST), trigger (`manual` or `vams`), dataset, values of the 12 items, planned total number of steps, comparison conditions |
| Learning curve figures | Figures of `train/loss` (raw and smoothed values, log scale), `train/grad_norm`, `train/lr`, processing speed, and GPU memory, and a figure overlaying `train/loss` of the comparison runs. There are two kinds of comparison figures: one with the step on the x-axis and one with the number of processed samples on the x-axis |
| Diagnosis results | Labels, feature table, findings, and the thresholds related to the attached labels. For runs without `eval/*`, a note that overfitting is not directly judged |
| Proposal for the next parameters | For each changed item, the previous value, the proposed value, the rationale, and the basis. Expected effect, risks, warnings from `validate-proposal`, and alternatives |
| Comparison table | The values of the 12 items, loss, completion ratio, processing speed, and labels of the target run and the comparison runs. Items that differ from the target are shown in bold |
| Datasets used for training | Scale, quality flags, and reviews of each dataset, and the status of the runs that used them. Candidates for datasets and episodes to exclude from the next training, and the steps to exclude them in VAMS curation. If VAMS could not be connected, the reason |
| Steps to run | Contents of `vams_parameters.json` and an example of VamsCLI commands. The training is run by a person after approval |
| Findings outside the allowlist | Only when it is judged that an item not among the 12 items, such as the learning rate, should be changed |

When comparing the final loss in the comparison table, pay attention to differences in `BATCH_SIZE`.
If `BATCH_SIZE` differs, the number of processed samples differs even at the same number of steps, so compare using the processed sample count listed beneath the final loss and the figure whose x-axis is the number of processed samples.

The report does not include values containing AWS account IDs, such as the ARN of the MLflow App, and runs are identified by run ID and experiment name.

## Output Location

All output files (`snapshot.json`, `datasets.json`, `diagnosis.json`, `proposal.json`, `vams_parameters.json`, `report.md`, and the PNG figures) are placed in the same directory.
The default output location is `samples/lerobot-openpi/outputs/advisor/<UTC timestamp>/`, created by `fetch`.
The timestamp has the form `%Y%m%dT%H%M%SZ` (for example `20261019T010203Z`).
`samples/lerobot-openpi/outputs` is excluded by `.gitignore`, so the outputs are not managed by git.
The user can specify a different output location when running the skill.

Saving to MLflow is optional. Only when the user requests it does `publish` save to the artifact `advisor/<UTC timestamp>/` of the target run.
If the name of the output directory is a timestamp, that value is used; otherwise, the time of saving is used.
`publish` only adds artifacts and does not change params, metrics, or tags.
If `report.md` contains an ARN, `publish` fails without saving.

Examples of output are in the [output sample](../../../../hyperpod/docs/ja/VAMS_INTEGRATION/sample-report/README.md) (a run with MLflow only) and the [output sample of a run started from VAMS](../../../../hyperpod/docs/ja/VAMS_INTEGRATION/sample-report-vams/README.md) (including the dataset section).

## Required Permissions

`fetch` and `publish` connect to the MLflow App from the workstation.
The AWS profile on the workstation needs the permission `sagemaker:CallMlflowAppApi` to call the MLflow App API.
Without this permission, `fetch` and `publish` fail with a permission error.
The ARN of the MLflow App to connect to is passed in the environment variable `MLFLOW_TRACKING_URI`.
Pass the profile and region as described in the [skill's procedure document](../../../../.claude/skills/pask-training-advisor/SKILL.md).

`detect` and `fetch-vams` read VAMS through the saved login of VamsCLI.
Complete `vamscli auth login` with a VamsCLI profile for the VAMS connected to PASK.
If the login has expired, `detect` judges that VAMS cannot be used and proceeds as `mlflow_only`.

`analyze`, `validate-proposal`, and `plot` read only the files in the output location, so AWS authentication is not required.

## Limitations

Only the 12 items can be proposed.
The learning rate, the number of warmup steps, and the number of decay steps are not in the poller's allowlist or in `template.json`, so they cannot be included in proposals.
This sample does not pass the learning rate from `slurm_train.sh`, and the value from the LeRobot policy preset is used.
Even for symptoms that should be addressed with the learning rate, such as divergence or plateau, proposals are limited to indirect means such as `BATCH_SIZE`, `STEPS`, and `LORA_R`.
When Claude Code judges that the learning rate or similar should be changed, it only notes this as a reference in the "Findings outside the allowlist" section of the report.
To make these targets of proposals, changes to the poller's allowlist, `template.json`, and `slurm_train.sh` are required.

Changing `STEPS` also changes the preset's learning rate schedule (warmup and cosine decay).
The relationship between `STEPS` and the learning rate schedule is in [the "Learning rate schedule and STEPS" section of tuning-guide.md](../../../../.claude/skills/pask-training-advisor/references/tuning-guide.md#学習率のスケジュールと-stepslerobot-061).

Exclusion candidates for datasets are decided from the VAMS metadata and reviews.
Per-episode quality flags, video quality, and the content of the actions are not examined.
Whether to exclude a candidate is decided by a person after checking the episode on the VAMS screen.
