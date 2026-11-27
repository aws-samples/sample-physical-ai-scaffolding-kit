"""Constants shared by the VAMS poller modules."""

import re
from enum import StrEnum


class State(StrEnum):
    """Execution states stored in the DynamoDB item attribute ``state``."""

    RECEIVED = "RECEIVED"
    STAGING = "STAGING"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    RUNNING = "RUNNING"
    FINALIZING = "FINALIZING"
    ABORT_SUSPECTED = "ABORT_SUSPECTED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    ABORTED = "ABORTED"


TERMINAL_STATES: frozenset[State] = frozenset({State.SUCCEEDED, State.FAILED, State.ABORTED})
NON_TERMINAL_STATES: frozenset[State] = frozenset(set(State) - TERMINAL_STATES)


class ErrorName(StrEnum):
    """Error names sent to Step Functions with ``SendTaskFailure``."""

    INVALID_REQUEST = "PASK.InvalidRequest"
    STAGING_FAILED = "PASK.StagingFailed"
    SUBMIT_FAILED = "PASK.SubmitFailed"
    JOB_FAILED = "PASK.JobFailed"
    JOB_LOST = "PASK.JobLost"
    OUTPUT_UPLOAD_FAILED = "PASK.OutputUploadFailed"
    ABORTED = "PASK.Aborted"


PENDING_ACTIVE = "ACTIVE"
PENDING_CLEANUP = "CLEANUP"
PENDING_INDEX = "pending-index"
CONCURRENCY_ITEM_ID = "#CONCURRENCY"

PIPELINE_EXECUTION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")

MAX_CAUSE_LENGTH = 32768
MAX_ERROR_LENGTH = 256
CLOSED_TOKEN_ERRORS: frozenset[str] = frozenset(
    {
        "TaskTimedOut",
        "TaskDoesNotExist",
        "InvalidToken",
    }
)

OUTPUT_FILES_LIMIT = 1000
RESULTS_FILE_MAX_BYTES = 380 * 1024

INPUT_MANIFEST_FILE = "input_manifest.json"
VAMS_CONTEXT_FILE = "vams_context.json"
JOB_STARTED_FILE = "job_started.json"
JOB_EXIT_FILE = "job_exit.json"
MLFLOW_RUN_FILE = "mlflow_run.json"
CURATION_MANIFEST_FILE = "curation_manifest.json"
VIEW_DATASET_DIR = "view_dataset"

CURATION_MANIFEST_VERSION = 2
INPUT_KIND_VIEW = "view"
INPUT_KIND_DATASET = "dataset"
