"""Parsing and validation of VAMS requests: the SQS body, the manifest, and the training config.

The shapes follow VAMS ``stepfunctions_builder.build_payload`` (the SQS message body) and
``executionRecords.build_manifest_envelope`` / ``build_metadata_envelope`` (the files the
message body points to).
"""

import json
import re
from dataclasses import dataclass
from typing import Any

from botocore.exceptions import ClientError

from pask_vams_poller.constants import PIPELINE_EXECUTION_ID_PATTERN, ErrorName
from pask_vams_poller.errors import PermanentFailure

S3_URI_SCHEME = "s3://"
PIPELINE_SEGMENT = ".pipeline."
WHOLE_ASSET_RELATIVE_PATH = "/"
AUX_TEMP_PREFIX_PATTERN = re.compile(r"^pipelines/(?P<name>[^/]+)/(?P<execution_id>[^/]+)/$")

POSITIVE_INTEGER_PATTERN = re.compile(r"^[1-9][0-9]{0,8}$")

ALLOWED_TRAINING_PARAMS: dict[str, re.Pattern[str]] = {
    "POLICY_TYPE": re.compile(r"^(pi05|pi0)$"),
    "PRETRAINED_PATH": re.compile(r"^(?!.*\.\.)[A-Za-z0-9._/-]{1,200}$"),
    "FINETUNE_MODE": re.compile(r"^(lora|expert_only|full)$"),
    "LORA_R": POSITIVE_INTEGER_PATTERN,
    "LORA_ALPHA": POSITIVE_INTEGER_PATTERN,
    "STEPS": POSITIVE_INTEGER_PATTERN,
    "BATCH_SIZE": POSITIVE_INTEGER_PATTERN,
    "SAVE_FREQ": POSITIVE_INTEGER_PATTERN,
    "LOG_FREQ": POSITIVE_INTEGER_PATTERN,
    "NUM_WORKERS": POSITIVE_INTEGER_PATTERN,
    "COMPILE_MODEL": re.compile(r"^(true|false)$"),
    "IMAGE_TAG": re.compile(r"^[A-Za-z0-9._-]{1,128}$"),
}

DENIED_TRAINING_PARAMS: frozenset[str] = frozenset(
    {
        "RESUME",
        "OUTPUT_DIR",
        "DATASET_ROOT",
        "DATASET_REPO_ID",
        "EXTRA_ARGS",
        "CONTAINER_IMAGE",
        "HF_TOKEN",
    }
)
DENIED_TRAINING_PARAM_PREFIXES: tuple[str, ...] = ("MLFLOW_", "VAMS_")

REQUIRED_BODY_FIELDS: dict[str, str] = {
    "workflow_database_id": "workflowDatabaseId",
    "workflow_id": "workflowId",
    "workflow_execution_id": "workflowExecutionId",
    "workflow_io_bucket": "workflowExecutionS3InputOutputBucket",
    "executing_user_name": "executingUserName",
    "input_manifest_s3_location": "inputManifestS3Location",
    "input_configuration_s3_location": "inputConfigurationS3Location",
}
S3_LOCATION_BODY_FIELDS: tuple[str, ...] = (
    "inputManifestS3Location",
    "inputConfigurationS3Location",
)
WORKFLOW_INPUTS_KEY_PREFIX = "pipelines/workflowExecutionInputs/"
OUTPUTS_FIELDS: tuple[str, ...] = ("bucket", "files", "previews", "metadata", "results")
WHOLE_ASSET_ENTRY_FIELDS: tuple[str, ...] = ("bucket", "assetRootS3Key", "databaseId", "assetId")
METADATA_STRING_FIELDS: tuple[str, ...] = ("assetName", "description")


class InvalidMessage(Exception):
    """The SQS message body is not a valid VAMS request.

    ``task_token`` holds the TaskToken when it could be read, so the caller can still
    answer the execution with ``SendTaskFailure``. It is None when the token is unreadable.
    """

    def __init__(self, reason: str, task_token: str | None) -> None:
        """Create the error.

        Args:
            reason: Human readable reason. It never contains field values.
            task_token: TaskToken read from the body, or None.
        """
        super().__init__(reason)
        self.reason = reason
        self.task_token = task_token


@dataclass(frozen=True)
class VamsRequest:
    """Fields of one VAMS pipeline request taken from the SQS message body."""

    task_token: str
    workflow_database_id: str
    workflow_id: str
    workflow_execution_id: str
    workflow_io_bucket: str
    executing_user_name: str
    input_manifest_s3_location: str
    input_configuration_s3_location: str


@dataclass
class Manifest:
    """The per-pipeline input manifest written by VAMS ``build_manifest_envelope``."""

    input_files: list[dict[str, Any]]
    input_metadata_s3_location: str
    outputs: dict[str, str]
    output_target: dict[str, Any]
    aux_bucket: str
    aux_temp_prefix: str
    system_config: dict[str, Any]


def _invalid_request(cause: str) -> PermanentFailure:
    """Build the failure raised for a request that does not have the expected shape.

    Args:
        cause: Reason sent as the ``cause`` of ``SendTaskFailure``.

    Returns:
        A ``PermanentFailure`` with ``PASK.InvalidRequest``.
    """
    return PermanentFailure(ErrorName.INVALID_REQUEST, cause)


def _is_non_empty_string(value: Any) -> bool:
    """Tell whether a JSON value is a string with at least one character.

    Args:
        value: Any decoded JSON value.

    Returns:
        True for a non-empty ``str``.
    """
    return isinstance(value, str) and value != ""


def _read_task_token(body: dict[str, Any]) -> str | None:
    """Read the TaskToken from a decoded message body.

    Args:
        body: The ``body`` object of the SQS message.

    Returns:
        The token, or None when it is missing or not a non-empty string.
    """
    token = body.get("TaskToken")
    return token if _is_non_empty_string(token) else None


def parse_message_body(text: str) -> VamsRequest:
    """Parse the SQS message body that the VAMS state machine sends.

    Args:
        text: The raw SQS ``Body``, a JSON object with a ``body`` object inside.

    Returns:
        The request fields.

    Raises:
        InvalidMessage: The JSON is malformed, ``body`` is missing, a required field is
            missing or empty, or an S3 location is not an ``s3://bucket/key`` URI under
            ``pipelines/workflowExecutionInputs/`` of the workflow input/output bucket.
    """
    try:
        document = json.loads(text)
    except json.JSONDecodeError as error:
        raise InvalidMessage("message body is not valid JSON", None) from error
    if not isinstance(document, dict) or not isinstance(document.get("body"), dict):
        raise InvalidMessage("message body has no 'body' object", None)

    body = document["body"]
    task_token = _read_task_token(body)
    if task_token is None:
        raise InvalidMessage("message body has no TaskToken", None)

    missing_fields = [
        body_key
        for body_key in REQUIRED_BODY_FIELDS.values()
        if not _is_non_empty_string(body.get(body_key))
    ]
    if missing_fields:
        raise InvalidMessage(
            f"message body is missing required fields: {', '.join(missing_fields)}", task_token
        )
    workflow_io_bucket = body[REQUIRED_BODY_FIELDS["workflow_io_bucket"]]
    for body_key in S3_LOCATION_BODY_FIELDS:
        try:
            bucket, key = parse_s3_uri(body[body_key])
        except PermanentFailure as error:
            raise InvalidMessage(f"{body_key} is not an S3 URI", task_token) from error
        if bucket != workflow_io_bucket or not key.startswith(WORKFLOW_INPUTS_KEY_PREFIX):
            raise InvalidMessage(
                f"{body_key} is not under {WORKFLOW_INPUTS_KEY_PREFIX} of the workflow "
                "input/output bucket",
                task_token,
            )

    fields = {name: body[body_key] for name, body_key in REQUIRED_BODY_FIELDS.items()}
    return VamsRequest(task_token=task_token, **fields)


def parse_s3_uri(uri: str) -> tuple[str, str]:
    """Split an ``s3://bucket/key`` URI.

    Args:
        uri: The URI. Both the bucket and the key must be non-empty.

    Returns:
        ``(bucket, key)``.

    Raises:
        PermanentFailure: ``PASK.InvalidRequest`` when the URI does not have that form.
    """
    if not isinstance(uri, str) or not uri.startswith(S3_URI_SCHEME):
        raise _invalid_request("S3 location does not start with s3://")
    bucket, separator, key = uri[len(S3_URI_SCHEME) :].partition("/")
    if not bucket or not separator or not key:
        raise _invalid_request("S3 location must name a bucket and a key")
    return bucket, key


def _require_dict(data: dict[str, Any], key: str) -> dict[str, Any]:
    """Read a manifest field that must be a JSON object.

    Args:
        data: The decoded manifest.
        key: Field name.

    Returns:
        The object.

    Raises:
        PermanentFailure: ``PASK.InvalidRequest`` when the field is missing or not an object.
    """
    value = data.get(key)
    if not isinstance(value, dict):
        raise _invalid_request(f"manifest field {key} must be an object")
    return value


def _require_string(data: dict[str, Any], key: str, owner: str) -> str:
    """Read a field that must be a string, possibly empty.

    Args:
        data: The object that holds the field.
        key: Field name.
        owner: Name of the object, used in the failure cause.

    Returns:
        The string.

    Raises:
        PermanentFailure: ``PASK.InvalidRequest`` when the field is missing or not a string.
    """
    value = data.get(key)
    if not isinstance(value, str):
        raise _invalid_request(f"{owner} field {key} must be a string")
    return value


def _parse_outputs(data: dict[str, Any]) -> dict[str, str]:
    """Read the ``outputs`` block of the manifest.

    Args:
        data: The decoded manifest.

    Returns:
        ``bucket``, ``files``, ``previews``, ``metadata``, and ``results`` as strings.

    Raises:
        PermanentFailure: ``PASK.InvalidRequest`` when a field is missing or not a string.
    """
    outputs = _require_dict(data, "outputs")
    return {field: _require_string(outputs, field, "outputs") for field in OUTPUTS_FIELDS}


def parse_manifest(data: dict[str, Any]) -> Manifest:
    """Read the manifest envelope written by VAMS.

    Args:
        data: The decoded ``inputManifestS3Location`` JSON.

    Returns:
        The manifest.

    Raises:
        PermanentFailure: ``PASK.InvalidRequest`` when a field is missing or has the wrong type.
    """
    if not isinstance(data, dict):
        raise _invalid_request("manifest must be a JSON object")
    input_files = data.get("inputFiles")
    if not isinstance(input_files, list) or not all(
        isinstance(entry, dict) for entry in input_files
    ):
        raise _invalid_request("manifest field inputFiles must be a list of objects")
    return Manifest(
        input_files=input_files,
        input_metadata_s3_location=_require_string(data, "inputMetadataS3Location", "manifest"),
        outputs=_parse_outputs(data),
        output_target=_require_dict(data, "outputTarget"),
        aux_bucket=_require_string(data, "auxBucket", "manifest"),
        aux_temp_prefix=_require_string(data, "auxTempPrefix", "manifest"),
        system_config=_require_dict(data, "systemConfig"),
    )


def pipeline_execution_id(manifest: Manifest) -> str:
    """Take the pipelineExecutionId from ``systemConfig.orchestrationEventPrefix``.

    VAMS builds the prefix as
    ``<eventSourcePrefix>.execution.<executionId>.pipeline.<pipelineExecutionId>``.

    Args:
        manifest: The parsed manifest.

    Returns:
        The id. It matches ``PIPELINE_EXECUTION_ID_PATTERN``.

    Raises:
        PermanentFailure: ``PASK.InvalidRequest`` when the prefix has no ``.pipeline.<id>``
            suffix or the id has characters outside the pattern.
    """
    prefix = manifest.system_config.get("orchestrationEventPrefix")
    if not isinstance(prefix, str) or PIPELINE_SEGMENT not in prefix:
        raise _invalid_request("systemConfig.orchestrationEventPrefix has no .pipeline.<id>")
    execution_id = prefix.rpartition(PIPELINE_SEGMENT)[2]
    if not PIPELINE_EXECUTION_ID_PATTERN.fullmatch(execution_id):
        raise _invalid_request("pipelineExecutionId has characters outside [A-Za-z0-9_-]")
    return execution_id


def pipeline_name(manifest: Manifest) -> str:
    """Take the pipeline name from ``auxTempPrefix`` (``pipelines/{name}/{executionId}/``).

    Args:
        manifest: The parsed manifest.

    Returns:
        The pipeline name.

    Raises:
        PermanentFailure: ``PASK.InvalidRequest`` when the prefix does not have that form.
    """
    match = AUX_TEMP_PREFIX_PATTERN.fullmatch(manifest.aux_temp_prefix)
    if match is None:
        raise _invalid_request("auxTempPrefix is not pipelines/<name>/<executionId>/")
    return match.group("name")


def whole_asset_input(manifest: Manifest) -> dict[str, Any]:
    """Return the single input entry that selects a whole asset.

    Args:
        manifest: The parsed manifest.

    Returns:
        The ``inputFiles`` entry. ``bucket``, ``assetRootS3Key``, ``databaseId``, and
        ``assetId`` are non-empty strings.

    Raises:
        PermanentFailure: ``PASK.InvalidRequest`` unless ``inputFiles`` has exactly one entry
            whose ``relativePath`` is ``/`` and whose identity fields are set.
    """
    if len(manifest.input_files) != 1:
        raise _invalid_request(
            f"inputFiles must have exactly one entry, got {len(manifest.input_files)}"
        )
    entry = manifest.input_files[0]
    if entry.get("relativePath") != WHOLE_ASSET_RELATIVE_PATH:
        raise _invalid_request("inputFiles must select the whole asset (relativePath '/')")
    missing_fields = [
        field for field in WHOLE_ASSET_ENTRY_FIELDS if not _is_non_empty_string(entry.get(field))
    ]
    if missing_fields:
        raise _invalid_request(f"inputFiles entry is missing: {', '.join(missing_fields)}")
    return entry


def _is_denied_training_param(key: str) -> bool:
    """Tell whether a training parameter is one the poller sets itself or never accepts.

    Args:
        key: Parameter name.

    Returns:
        True for a denied name.
    """
    return key in DENIED_TRAINING_PARAMS or key.startswith(DENIED_TRAINING_PARAM_PREFIXES)


def _training_param_to_string(key: str, value: Any) -> str:
    """Convert a scalar training parameter to the string passed in the job environment.

    Args:
        key: Parameter name, used in the failure cause.
        value: Decoded JSON value.

    Returns:
        The string. Booleans become ``true`` or ``false``.

    Raises:
        PermanentFailure: ``PASK.InvalidRequest`` for values other than strings, integers,
            and booleans.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return value
    raise _invalid_request(f"training parameter {key} must be a string, integer, or boolean")


def validate_training_params(config: dict[str, Any]) -> dict[str, str]:
    """Check the training parameters against the allow list.

    Args:
        config: The decoded ``inputConfigurationS3Location`` JSON. It must be a flat object.

    Returns:
        The parameters as strings, ready to pass in the job environment.

    Raises:
        PermanentFailure: ``PASK.InvalidRequest`` for a non-object config, a denied or
            unknown key, a non-scalar value, or a value that does not match the pattern.
            The cause names the key and never the value.
    """
    if not isinstance(config, dict):
        raise _invalid_request("training configuration must be a JSON object")
    validated: dict[str, str] = {}
    for key, value in config.items():
        if _is_denied_training_param(key):
            raise _invalid_request(f"training parameter {key} is not allowed")
        pattern = ALLOWED_TRAINING_PARAMS.get(key)
        if pattern is None:
            raise _invalid_request(f"training parameter {key} is unknown")
        text = _training_param_to_string(key, value)
        if not pattern.fullmatch(text):
            raise _invalid_request(f"training parameter {key} has an invalid value")
        validated[key] = text
    return validated


def _asset_data(data: dict[str, Any]) -> dict[str, Any]:
    """Find the asset fields in a metadata envelope.

    The v1 envelope keeps them under ``metadata``. The v2 grouped envelope keeps them under
    ``assets[].assetData``; it is read only when it has exactly one asset group, which is
    the case for a whole-asset input.

    Args:
        data: The decoded ``inputMetadataS3Location`` JSON.

    Returns:
        The object that may hold ``assetName``, ``description``, and ``tags``, or an empty
        dict when the envelope has none.
    """
    metadata = data.get("metadata")
    if isinstance(metadata, dict):
        return metadata
    assets = data.get("assets")
    if isinstance(assets, list) and len(assets) == 1 and isinstance(assets[0], dict):
        asset_data = assets[0].get("assetData")
        if isinstance(asset_data, dict):
            return asset_data
    return {}


def parse_metadata(data: dict[str, Any]) -> dict[str, Any]:
    """Take ``assetName``, ``description``, and ``tags`` from the metadata envelope.

    Args:
        data: The decoded ``inputMetadataS3Location`` JSON.

    Returns:
        Only the fields that are present and not empty.

    Raises:
        PermanentFailure: ``PASK.InvalidRequest`` when a present field has the wrong type.
    """
    if not isinstance(data, dict):
        raise _invalid_request("metadata must be a JSON object")
    asset_data = _asset_data(data)
    result: dict[str, Any] = {}
    for field in METADATA_STRING_FIELDS:
        value = asset_data.get(field)
        if value is None or value == "":
            continue
        if not isinstance(value, str):
            raise _invalid_request(f"metadata field {field} must be a string")
        result[field] = value
    tags = asset_data.get("tags")
    if tags:
        if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
            raise _invalid_request("metadata field tags must be a list of strings")
        result["tags"] = list(tags)
    return result


def load_json_from_s3(s3: Any, uri: str) -> dict[str, Any]:
    """Read a JSON object from S3.

    Args:
        s3: boto3 S3 client.
        uri: ``s3://bucket/key`` of the object.

    Returns:
        The decoded object.

    Raises:
        PermanentFailure: ``PASK.InvalidRequest`` when the URI is malformed, the object does
            not exist, or it is not a UTF-8 JSON object.
        ClientError: Any other S3 error, unchanged.
    """
    bucket, key = parse_s3_uri(uri)
    try:
        response = s3.get_object(Bucket=bucket, Key=key)
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "NoSuchKey":
            raise _invalid_request(f"S3 object does not exist: {uri}") from error
        raise
    try:
        document = json.loads(response["Body"].read().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _invalid_request(f"S3 object is not valid JSON: {uri}") from error
    if not isinstance(document, dict):
        raise _invalid_request(f"S3 object is not a JSON object: {uri}")
    return document
