"""Tests for parsing VAMS messages, manifests, metadata, and training parameters."""

import copy
import json
from typing import Any

import pytest
from botocore.exceptions import ClientError

from pask_vams_poller.constants import ErrorName
from pask_vams_poller.errors import PermanentFailure
from pask_vams_poller.messages import (
    ALLOWED_TRAINING_PARAMS,
    DENIED_TRAINING_PARAMS,
    InvalidMessage,
    Manifest,
    load_json_from_s3,
    parse_manifest,
    parse_message_body,
    parse_metadata,
    parse_s3_uri,
    pipeline_execution_id,
    pipeline_name,
    validate_training_params,
    whole_asset_input,
)

WORKFLOW_BUCKET = "vams-test-workflow"
WORKFLOW_INPUTS = f"s3://{WORKFLOW_BUCKET}/pipelines/workflowExecutionInputs"
ASSET_BUCKET = "vams-test-assets"
PIPELINE_EXECUTION_ID = "pexec_01-ABC"


def _message_body() -> dict[str, Any]:
    """Return a body in the shape of VAMS ``build_payload`` with a callback token."""
    return {
        "body": {
            "workflowDatabaseId": "db1",
            "workflowId": "wf1",
            "workflowExecutionId": "exec1",
            "workflowExecutionS3InputOutputBucket": WORKFLOW_BUCKET,
            "executingUserName": "alice",
            "executingRequestContext": "alice",
            "inputManifestS3Location": f"{WORKFLOW_INPUTS}/manifest.json",
            "inputConfigurationS3Location": f"{WORKFLOW_INPUTS}/config.json",
            "TaskToken": "token-1",
        }
    }


def _manifest_data() -> dict[str, Any]:
    """Return a manifest in the shape of VAMS ``build_manifest_envelope`` for a whole asset."""
    return {
        "schemaVersion": 1,
        "inputFiles": [
            {
                "relativePath": "/",
                "databaseId": "db1",
                "assetId": "asset1",
                "assetRootS3Key": "asset1/",
                "auxPreviewPrefix": "asset1/preview/",
                "bucket": ASSET_BUCKET,
                "key": "asset1/",
                "versionId": "",
            }
        ],
        "inputMetadataS3Location": f"{WORKFLOW_INPUTS}/metadata.json",
        "outputs": {
            "bucket": WORKFLOW_BUCKET,
            "files": "pipelines/train/exec1/output/files/",
            "previews": "pipelines/train/exec1/output/previews/",
            "metadata": "pipelines/train/exec1/output/metadata/",
            "results": "pipelines/train/exec1/output/results/",
        },
        "outputTarget": {
            "locationType": "asset",
            "assetId": "asset1",
            "databaseId": "db1",
            "fileBaseExecutionPathExtension": "/",
        },
        "auxBucket": "vams-test-aux",
        "auxTempPrefix": "pipelines/train-pi05/exec1/",
        "auxPreviewPipelineSuffix": "",
        "systemConfig": {
            "orchestrationBusArn": "arn:aws:events:us-west-2:<account-id>:event-bus/vams",
            "orchestrationEventPrefix": f"vams.execution.exec1.pipeline.{PIPELINE_EXECUTION_ID}",
        },
    }


def _assert_invalid_request(failure: pytest.ExceptionInfo[PermanentFailure]) -> None:
    """Assert that a failure carries ``PASK.InvalidRequest``."""
    assert failure.value.error_name is ErrorName.INVALID_REQUEST


def test_parse_message_body_reads_all_fields() -> None:
    request = parse_message_body(json.dumps(_message_body()))

    assert request.task_token == "token-1"
    assert request.workflow_database_id == "db1"
    assert request.workflow_id == "wf1"
    assert request.workflow_execution_id == "exec1"
    assert request.workflow_io_bucket == WORKFLOW_BUCKET
    assert request.executing_user_name == "alice"
    assert request.input_manifest_s3_location.endswith("/manifest.json")
    assert request.input_configuration_s3_location.endswith("/config.json")


def test_parse_message_body_rejects_malformed_json() -> None:
    with pytest.raises(InvalidMessage) as failure:
        parse_message_body("{not json")

    assert failure.value.task_token is None


def test_parse_message_body_rejects_missing_body() -> None:
    with pytest.raises(InvalidMessage) as failure:
        parse_message_body(json.dumps({"TaskToken": "token-1"}))

    assert failure.value.task_token is None


def test_parse_message_body_without_token_has_no_token() -> None:
    body = _message_body()
    del body["body"]["TaskToken"]

    with pytest.raises(InvalidMessage) as failure:
        parse_message_body(json.dumps(body))

    assert failure.value.task_token is None


@pytest.mark.parametrize(
    "field",
    [
        "workflowDatabaseId",
        "workflowId",
        "workflowExecutionId",
        "workflowExecutionS3InputOutputBucket",
        "executingUserName",
        "inputManifestS3Location",
        "inputConfigurationS3Location",
    ],
)
def test_parse_message_body_missing_field_keeps_token(field: str) -> None:
    body = _message_body()
    del body["body"][field]

    with pytest.raises(InvalidMessage) as failure:
        parse_message_body(json.dumps(body))

    assert failure.value.task_token == "token-1"
    assert field in failure.value.reason


def test_parse_message_body_rejects_non_s3_location() -> None:
    body = _message_body()
    body["body"]["inputManifestS3Location"] = "https://example.com/manifest.json"

    with pytest.raises(InvalidMessage) as failure:
        parse_message_body(json.dumps(body))

    assert failure.value.task_token == "token-1"


@pytest.mark.parametrize("field", ["inputManifestS3Location", "inputConfigurationS3Location"])
@pytest.mark.parametrize(
    "location",
    [
        f"s3://{ASSET_BUCKET}/pipelines/workflowExecutionInputs/manifest.json",
        f"s3://{WORKFLOW_BUCKET}/asset1/manifest.json",
        f"s3://{WORKFLOW_BUCKET}/workflowExecutionInputs/manifest.json",
        f"s3://{WORKFLOW_BUCKET}/pipelines/workflowExecutionInputsX/manifest.json",
    ],
)
def test_parse_message_body_rejects_location_outside_workflow_inputs(
    field: str, location: str
) -> None:
    body = _message_body()
    body["body"][field] = location

    with pytest.raises(InvalidMessage) as failure:
        parse_message_body(json.dumps(body))

    assert failure.value.task_token == "token-1"
    assert field in failure.value.reason


def test_parse_s3_uri_splits_bucket_and_key() -> None:
    assert parse_s3_uri("s3://bucket/a/b.json") == ("bucket", "a/b.json")


@pytest.mark.parametrize("uri", ["bucket/key", "s3://bucket", "s3://bucket/", "s3:///key"])
def test_parse_s3_uri_rejects_malformed(uri: str) -> None:
    with pytest.raises(PermanentFailure) as failure:
        parse_s3_uri(uri)

    _assert_invalid_request(failure)


def test_parse_manifest_reads_envelope() -> None:
    manifest = parse_manifest(_manifest_data())

    assert isinstance(manifest, Manifest)
    assert manifest.outputs["bucket"] == WORKFLOW_BUCKET
    assert manifest.outputs["results"] == "pipelines/train/exec1/output/results/"
    assert manifest.output_target["assetId"] == "asset1"
    assert manifest.aux_bucket == "vams-test-aux"
    assert pipeline_execution_id(manifest) == PIPELINE_EXECUTION_ID
    assert pipeline_name(manifest) == "train-pi05"
    entry = whole_asset_input(manifest)
    assert entry["bucket"] == ASSET_BUCKET
    assert entry["assetRootS3Key"] == "asset1/"
    assert entry["databaseId"] == "db1"
    assert entry["assetId"] == "asset1"


@pytest.mark.parametrize("field", ["inputFiles", "outputs", "systemConfig", "auxTempPrefix"])
def test_parse_manifest_rejects_missing_field(field: str) -> None:
    data = _manifest_data()
    del data[field]

    with pytest.raises(PermanentFailure) as failure:
        parse_manifest(data)

    _assert_invalid_request(failure)


def test_whole_asset_input_rejects_two_input_files() -> None:
    data = _manifest_data()
    second = copy.deepcopy(data["inputFiles"][0])
    second["assetId"] = "asset2"
    data["inputFiles"].append(second)

    with pytest.raises(PermanentFailure) as failure:
        whole_asset_input(parse_manifest(data))

    _assert_invalid_request(failure)


def test_whole_asset_input_rejects_single_file_selection() -> None:
    data = _manifest_data()
    data["inputFiles"][0]["relativePath"] = "/episode_0.mcap"
    data["inputFiles"][0]["key"] = "asset1/episode_0.mcap"

    with pytest.raises(PermanentFailure) as failure:
        whole_asset_input(parse_manifest(data))

    _assert_invalid_request(failure)


def test_whole_asset_input_rejects_missing_asset_root() -> None:
    data = _manifest_data()
    data["inputFiles"][0]["assetRootS3Key"] = ""

    with pytest.raises(PermanentFailure) as failure:
        whole_asset_input(parse_manifest(data))

    _assert_invalid_request(failure)


@pytest.mark.parametrize(
    "prefix",
    [
        "vams.execution.exec1.pipeline.bad/id",
        "vams.execution.exec1.pipeline.bad.id",
        "vams.execution.exec1.pipeline.",
        "vams.execution.exec1.pipeline.id\n",
        "vams.execution.exec1",
    ],
)
def test_pipeline_execution_id_rejects_bad_characters(prefix: str) -> None:
    data = _manifest_data()
    data["systemConfig"]["orchestrationEventPrefix"] = prefix

    with pytest.raises(PermanentFailure) as failure:
        pipeline_execution_id(parse_manifest(data))

    _assert_invalid_request(failure)


@pytest.mark.parametrize("prefix", ["pipelines/train/", "temp/train/exec1/", "pipelines//exec1/"])
def test_pipeline_name_rejects_unexpected_prefix(prefix: str) -> None:
    data = _manifest_data()
    data["auxTempPrefix"] = prefix

    with pytest.raises(PermanentFailure) as failure:
        pipeline_name(parse_manifest(data))

    _assert_invalid_request(failure)


def test_validate_training_params_converts_numbers_and_booleans() -> None:
    params = validate_training_params(
        {
            "POLICY_TYPE": "pi05",
            "PRETRAINED_PATH": "lerobot/pi05_base",
            "FINETUNE_MODE": "lora",
            "LORA_R": 16,
            "LORA_ALPHA": "32",
            "STEPS": 3000,
            "BATCH_SIZE": 8,
            "SAVE_FREQ": 1000,
            "LOG_FREQ": 50,
            "NUM_WORKERS": 4,
            "COMPILE_MODEL": False,
            "IMAGE_TAG": "v1.2.3",
        }
    )

    assert params["LORA_R"] == "16"
    assert params["LORA_ALPHA"] == "32"
    assert params["STEPS"] == "3000"
    assert params["COMPILE_MODEL"] == "false"
    assert validate_training_params({"COMPILE_MODEL": True}) == {"COMPILE_MODEL": "true"}
    assert all(isinstance(value, str) for value in params.values())


def test_validate_training_params_accepts_empty_config() -> None:
    assert validate_training_params({}) == {}


@pytest.mark.parametrize(
    "key",
    sorted(DENIED_TRAINING_PARAMS) + ["MLFLOW_TRACKING_URI", "VAMS_RUN_DIR"],
)
def test_validate_training_params_rejects_denied_keys(key: str) -> None:
    with pytest.raises(PermanentFailure) as failure:
        validate_training_params({key: "secret-value"})

    _assert_invalid_request(failure)
    assert key in failure.value.cause
    assert "secret-value" not in failure.value.cause


def test_validate_training_params_rejects_unknown_key() -> None:
    with pytest.raises(PermanentFailure) as failure:
        validate_training_params({"LEARNING_RATE": "0.1"})

    _assert_invalid_request(failure)
    assert "LEARNING_RATE" in failure.value.cause


INVALID_VALUES: dict[str, list[Any]] = {
    "POLICY_TYPE": ["pi1", "PI05", "pi05\n"],
    "PRETRAINED_PATH": ["../etc/passwd", "a/../b", "a b", "x" * 201, "$(id)"],
    "FINETUNE_MODE": ["partial", ""],
    "LORA_R": [0, -1, "1.5", 1.5, "abc"],
    "LORA_ALPHA": [0, "07"],
    "STEPS": [0, "1e3"],
    "BATCH_SIZE": [-8],
    "SAVE_FREQ": [0],
    "LOG_FREQ": ["ten"],
    "NUM_WORKERS": [True],
    "COMPILE_MODEL": ["True", "yes", 1],
    "IMAGE_TAG": ["latest;rm", "a/b", "x" * 129],
}


def test_invalid_values_cover_every_allowed_key() -> None:
    assert set(INVALID_VALUES) == set(ALLOWED_TRAINING_PARAMS)


@pytest.mark.parametrize(
    ("key", "value"),
    [(key, value) for key, values in INVALID_VALUES.items() for value in values],
)
def test_validate_training_params_rejects_invalid_values(key: str, value: Any) -> None:
    with pytest.raises(PermanentFailure) as failure:
        validate_training_params({key: value})

    _assert_invalid_request(failure)
    assert key in failure.value.cause
    if isinstance(value, str) and value:
        assert value not in failure.value.cause


@pytest.mark.parametrize("value", [{"nested": "1"}, ["1"], None])
def test_validate_training_params_rejects_non_scalar_values(value: Any) -> None:
    with pytest.raises(PermanentFailure) as failure:
        validate_training_params({"STEPS": value})

    _assert_invalid_request(failure)


def test_validate_training_params_rejects_non_object() -> None:
    with pytest.raises(PermanentFailure) as failure:
        validate_training_params(["STEPS"])  # type: ignore[arg-type]

    _assert_invalid_request(failure)


def test_parse_metadata_reads_v1_envelope() -> None:
    data = {
        "schemaVersion": 1,
        "metadata": {
            "assetName": "pick-and-place",
            "description": "50 episodes",
            "tags": ["robot", "so101"],
            "other": "ignored",
        },
    }

    assert parse_metadata(data) == {
        "assetName": "pick-and-place",
        "description": "50 episodes",
        "tags": ["robot", "so101"],
    }


def test_parse_metadata_keeps_only_present_fields() -> None:
    data = {"schemaVersion": 1, "metadata": {"assetName": "pick", "description": ""}}

    assert parse_metadata(data) == {"assetName": "pick"}


def test_parse_metadata_reads_v2_grouped_envelope() -> None:
    data = {
        "schemaVersion": 2,
        "assets": [
            {
                "databaseId": "db1",
                "assetId": "asset1",
                "assetData": {"assetName": "pick", "description": "", "tags": ["robot"]},
                "files": [{"fileKey": "/", "metadata": {}}],
            }
        ],
    }

    assert parse_metadata(data) == {"assetName": "pick", "tags": ["robot"]}


def test_parse_metadata_without_asset_fields_is_empty() -> None:
    assert parse_metadata({"schemaVersion": 1, "metadata": {}}) == {}


def test_parse_metadata_rejects_wrong_types() -> None:
    with pytest.raises(PermanentFailure) as failure:
        parse_metadata({"schemaVersion": 1, "metadata": {"tags": "robot"}})

    _assert_invalid_request(failure)


def test_load_json_from_s3_reads_object(s3_client: Any) -> None:
    s3_client.create_bucket(
        Bucket=WORKFLOW_BUCKET, CreateBucketConfiguration={"LocationConstraint": "us-west-2"}
    )
    s3_client.put_object(Bucket=WORKFLOW_BUCKET, Key="config.json", Body=b'{"STEPS": 10}')

    assert load_json_from_s3(s3_client, f"s3://{WORKFLOW_BUCKET}/config.json") == {"STEPS": 10}


@pytest.mark.parametrize("content", [b"{broken", b"\xff\xfe", b"[1, 2]"])
def test_load_json_from_s3_rejects_invalid_json(s3_client: Any, content: bytes) -> None:
    s3_client.create_bucket(
        Bucket=WORKFLOW_BUCKET, CreateBucketConfiguration={"LocationConstraint": "us-west-2"}
    )
    s3_client.put_object(Bucket=WORKFLOW_BUCKET, Key="config.json", Body=content)

    with pytest.raises(PermanentFailure) as failure:
        load_json_from_s3(s3_client, f"s3://{WORKFLOW_BUCKET}/config.json")

    _assert_invalid_request(failure)


def test_load_json_from_s3_missing_key_is_invalid_request(s3_client: Any) -> None:
    s3_client.create_bucket(
        Bucket=WORKFLOW_BUCKET, CreateBucketConfiguration={"LocationConstraint": "us-west-2"}
    )

    with pytest.raises(PermanentFailure) as failure:
        load_json_from_s3(s3_client, f"s3://{WORKFLOW_BUCKET}/missing.json")

    _assert_invalid_request(failure)


def test_load_json_from_s3_raises_other_client_errors(s3_client: Any) -> None:
    with pytest.raises(ClientError) as failure:
        load_json_from_s3(s3_client, "s3://bucket-that-does-not-exist/config.json")

    assert failure.value.response["Error"]["Code"] == "NoSuchBucket"
