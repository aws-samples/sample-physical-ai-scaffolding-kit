"""Tests for the intake loop with moto SQS, S3, and DynamoDB and a stubbed Step Functions."""

import dataclasses
import json
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import boto3
import pytest
from botocore.stub import ANY, Stubber

from pask_vams_poller.config import PollerConfig
from pask_vams_poller.constants import PENDING_CLEANUP, State
from pask_vams_poller.context import PollerContext
from pask_vams_poller.intake import IntakeLoop
from pask_vams_poller.sfn import SfnNotifier
from pask_vams_poller.store import ExecutionItem, ExecutionStore

from .conftest import TEST_REGION

WORKFLOW_BUCKET = "vams-test-workflow"
MANIFEST_KEY = "pipelines/workflowExecutionInputs/manifest.json"
MANIFEST_URI = f"s3://{WORKFLOW_BUCKET}/{MANIFEST_KEY}"
CONFIG_URI = f"s3://{WORKFLOW_BUCKET}/pipelines/workflowExecutionInputs/config.json"
METADATA_URI = f"s3://{WORKFLOW_BUCKET}/pipelines/workflowExecutionInputs/metadata.json"
PID = "pexec_01-ABC"
TOKEN = "token-new"
OLD_TOKEN = "token-old"
STORED_TOKEN = "token-stored"
OWNER = "login-1:invocation-a"
ONE_MINUTE_MS = 60_000


@pytest.fixture
def workflow_bucket(s3_client: Any) -> str:
    """Create the VAMS workflow bucket that holds the manifest."""
    s3_client.create_bucket(
        Bucket=WORKFLOW_BUCKET,
        CreateBucketConfiguration={"LocationConstraint": TEST_REGION},
    )
    return WORKFLOW_BUCKET


@pytest.fixture
def store(
    dynamodb_client: Any, executions_table: str, fixed_clock: Callable[[], float]
) -> ExecutionStore:
    """Return a store on the moto executions table."""
    return ExecutionStore(
        executions_table,
        region=TEST_REGION,
        owner_id=OWNER,
        lease_seconds=900,
        ttl_days=30,
        clock=fixed_clock,
        dynamodb_client=dynamodb_client,
    )


@pytest.fixture
def sfn_stubber() -> Iterator[tuple[Any, Stubber]]:
    """Return a Step Functions client with an active Stubber, checked at teardown."""
    client = boto3.client("stepfunctions", region_name=TEST_REGION)
    with Stubber(client) as stubber:
        yield client, stubber
        stubber.assert_no_pending_responses()


def _make_context(
    poller_config: PollerConfig,
    queue_url: str,
    store: ExecutionStore,
    sfn_client: Any,
    s3_client: Any,
    sqs_client: Any,
    training_env: dict[str, str],
) -> PollerContext:
    """Build a context that points the intake loop at the moto queue."""
    return PollerContext(
        config=dataclasses.replace(poller_config, queue_url=queue_url),
        store=store,
        sfn=SfnNotifier(sfn_client),
        slurm=None,
        s3=s3_client,
        sqs=sqs_client,
        training_env=training_env,
        owner_id=OWNER,
    )


@pytest.fixture
def intake(
    poller_config: PollerConfig,
    queue_url: str,
    store: ExecutionStore,
    sfn_stubber: tuple[Any, Stubber],
    s3_client: Any,
    sqs_client: Any,
    workflow_bucket: str,
    training_env: dict[str, str],
) -> IntakeLoop:
    """Return an intake loop wired to moto and the stubbed Step Functions client."""
    context = _make_context(
        poller_config, queue_url, store, sfn_stubber[0], s3_client, sqs_client, training_env
    )
    return IntakeLoop(context)


def _body(token: str | None = TOKEN, **overrides: Any) -> str:
    """Return a message body in the shape that the VAMS state machine sends."""
    body: dict[str, Any] = {
        "workflowDatabaseId": "db1",
        "workflowId": "wf1",
        "workflowExecutionId": "exec1",
        "workflowExecutionS3InputOutputBucket": WORKFLOW_BUCKET,
        "executingUserName": "alice",
        "executingRequestContext": "alice",
        "inputManifestS3Location": MANIFEST_URI,
        "inputConfigurationS3Location": CONFIG_URI,
    }
    if token is not None:
        body["TaskToken"] = token
    body.update(overrides)
    return json.dumps({"body": body})


def _put_manifest(s3_client: Any, orchestration_prefix: str) -> None:
    """Write a whole-asset manifest whose orchestration prefix carries the pipeline id."""
    manifest = {
        "schemaVersion": 1,
        "inputFiles": [
            {
                "relativePath": "/",
                "databaseId": "db1",
                "assetId": "asset1",
                "assetRootS3Key": "asset1/",
                "bucket": "vams-test-assets",
                "key": "asset1/",
                "versionId": "",
            }
        ],
        "inputMetadataS3Location": METADATA_URI,
        "outputs": {
            "bucket": WORKFLOW_BUCKET,
            "files": "pipelines/train/exec1/output/files/",
            "previews": "pipelines/train/exec1/output/previews/",
            "metadata": "pipelines/train/exec1/output/metadata/",
            "results": "pipelines/train/exec1/output/results/",
        },
        "outputTarget": {"locationType": "asset", "assetId": "asset1", "databaseId": "db1"},
        "auxBucket": "vams-test-aux",
        "auxTempPrefix": "pipelines/train/exec1/",
        "systemConfig": {"orchestrationEventPrefix": orchestration_prefix},
    }
    s3_client.put_object(Bucket=WORKFLOW_BUCKET, Key=MANIFEST_KEY, Body=json.dumps(manifest))


def _put_valid_manifest(s3_client: Any) -> None:
    """Write a manifest whose pipeline id is ``PID``."""
    _put_manifest(s3_client, f"vams.execution.exec1.pipeline.{PID}")


def _send(sqs_client: Any, queue_url: str, body: str) -> int:
    """Send a message and return a timestamp in epoch milliseconds taken before sending."""
    sent_before_ms = int(time.time() * 1000)
    sqs_client.send_message(QueueUrl=queue_url, MessageBody=body)
    return sent_before_ms


def _queued_message_count(sqs_client: Any, queue_url: str) -> int:
    """Count the visible and in-flight messages of the queue."""
    attributes = sqs_client.get_queue_attributes(
        QueueUrl=queue_url,
        AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
    )["Attributes"]
    return int(attributes["ApproximateNumberOfMessages"]) + int(
        attributes["ApproximateNumberOfMessagesNotVisible"]
    )


def _peek_sent_timestamp(sqs_client: Any, queue_url: str) -> int:
    """Read the SentTimestamp of the queued message and make it visible again."""
    message = sqs_client.receive_message(
        QueueUrl=queue_url, MessageSystemAttributeNames=["SentTimestamp"]
    )["Messages"][0]
    sqs_client.change_message_visibility(
        QueueUrl=queue_url, ReceiptHandle=message["ReceiptHandle"], VisibilityTimeout=0
    )
    return int(message["Attributes"]["SentTimestamp"])


def _create_existing(store: ExecutionStore, token: str, token_sent_at: int) -> None:
    """Create the execution ``PID`` as an earlier message would have."""
    fields = {
        "pipelineExecutionId": PID,
        "workflowDatabaseId": "db1",
        "workflowId": "wf1",
        "workflowExecutionId": "exec1",
        "inputManifestS3Location": MANIFEST_URI,
        "inputConfigurationS3Location": CONFIG_URI,
        "inputMetadataS3Location": METADATA_URI,
        "executingUserName": "alice",
    }
    assert store.create_received(fields, token, token_sent_at)


def _stored(store: ExecutionStore) -> ExecutionItem:
    """Read the execution ``PID``, which must exist."""
    item = store.get(PID)
    assert item is not None
    return item


def _expect_invalid_request_failure(stubber: Stubber, token: str) -> None:
    """Expect one ``SendTaskFailure`` with ``PASK.InvalidRequest`` for ``token``."""
    stubber.add_response(
        "send_task_failure",
        {},
        {"taskToken": token, "error": "PASK.InvalidRequest", "cause": ANY},
    )


def test_new_request_is_recorded_and_deleted(
    intake: IntakeLoop, store: ExecutionStore, s3_client: Any, sqs_client: Any, queue_url: str
) -> None:
    _put_valid_manifest(s3_client)
    sent_before_ms = _send(sqs_client, queue_url, _body())

    assert intake.poll_once() == 1

    item = _stored(store)
    assert item.state is State.RECEIVED
    assert item.version == 1
    assert item.taskToken == TOKEN
    assert item.tokenSentAt is not None and item.tokenSentAt >= sent_before_ms
    assert item.workflowDatabaseId == "db1"
    assert item.workflowId == "wf1"
    assert item.workflowExecutionId == "exec1"
    assert item.inputManifestS3Location == MANIFEST_URI
    assert item.inputConfigurationS3Location == CONFIG_URI
    assert item.inputMetadataS3Location == METADATA_URI
    assert item.executingUserName == "alice"
    assert _queued_message_count(sqs_client, queue_url) == 0


def test_receive_uses_configured_visibility_timeout(
    intake: IntakeLoop,
    poller_config: PollerConfig,
    s3_client: Any,
    sqs_client: Any,
    queue_url: str,
) -> None:
    receive_params: list[dict[str, Any]] = []

    def capture_receive_params(params: dict[str, Any], **_: Any) -> None:
        receive_params.append(dict(params))

    sqs_client.meta.events.register(
        "provide-client-params.sqs.ReceiveMessage", capture_receive_params
    )
    _put_valid_manifest(s3_client)
    _send(sqs_client, queue_url, _body())

    assert intake.poll_once() == 1

    assert receive_params[0]["VisibilityTimeout"] == poller_config.visibility_timeout_seconds


def test_redelivered_message_keeps_token_and_is_deleted(
    intake: IntakeLoop, store: ExecutionStore, s3_client: Any, sqs_client: Any, queue_url: str
) -> None:
    _put_valid_manifest(s3_client)
    _send(sqs_client, queue_url, _body())
    _create_existing(store, TOKEN, _peek_sent_timestamp(sqs_client, queue_url))

    assert intake.poll_once() == 1

    item = _stored(store)
    assert item.taskToken == TOKEN
    assert item.version == 1
    assert _queued_message_count(sqs_client, queue_url) == 0


def test_newer_token_replaces_stored_token(
    intake: IntakeLoop, store: ExecutionStore, s3_client: Any, sqs_client: Any, queue_url: str
) -> None:
    _put_valid_manifest(s3_client)
    sent_before_ms = _send(sqs_client, queue_url, _body())
    _create_existing(store, OLD_TOKEN, sent_before_ms - ONE_MINUTE_MS)

    intake.poll_once()

    item = _stored(store)
    assert item.taskToken == TOKEN
    assert item.tokenSentAt is not None and item.tokenSentAt >= sent_before_ms
    assert item.version == 2
    assert item.state is State.RECEIVED
    assert item.resendPending is False
    assert _queued_message_count(sqs_client, queue_url) == 0


def test_older_token_does_not_replace_stored_token(
    intake: IntakeLoop, store: ExecutionStore, s3_client: Any, sqs_client: Any, queue_url: str
) -> None:
    _put_valid_manifest(s3_client)
    sent_before_ms = _send(sqs_client, queue_url, _body(token=OLD_TOKEN))
    newer_sent_at = sent_before_ms + ONE_MINUTE_MS
    _create_existing(store, STORED_TOKEN, newer_sent_at)

    intake.poll_once()

    item = _stored(store)
    assert item.taskToken == STORED_TOKEN
    assert item.tokenSentAt == newer_sent_at
    assert item.version == 1
    assert _queued_message_count(sqs_client, queue_url) == 0


@pytest.mark.parametrize(
    "override",
    [
        {"workflowDatabaseId": "db2"},
        {"workflowId": "wf2"},
        {"workflowExecutionId": "exec2"},
        {
            "inputManifestS3Location": (
                f"s3://{WORKFLOW_BUCKET}/pipelines/workflowExecutionInputs/other.json"
            )
        },
    ],
)
def test_token_of_another_request_with_same_pid_is_rejected(
    intake: IntakeLoop,
    store: ExecutionStore,
    sfn_stubber: tuple[Any, Stubber],
    s3_client: Any,
    sqs_client: Any,
    queue_url: str,
    override: dict[str, str],
) -> None:
    _put_valid_manifest(s3_client)
    manifest = s3_client.get_object(Bucket=WORKFLOW_BUCKET, Key=MANIFEST_KEY)["Body"].read()
    s3_client.put_object(
        Bucket=WORKFLOW_BUCKET, Key="pipelines/workflowExecutionInputs/other.json", Body=manifest
    )
    sent_before_ms = _send(sqs_client, queue_url, _body(**override))
    _create_existing(store, OLD_TOKEN, sent_before_ms - ONE_MINUTE_MS)
    _expect_invalid_request_failure(sfn_stubber[1], TOKEN)

    assert intake.poll_once() == 1

    item = _stored(store)
    assert item.taskToken == OLD_TOKEN
    assert item.version == 1
    assert _queued_message_count(sqs_client, queue_url) == 0


def test_new_token_for_terminal_execution_sets_resend_pending(
    intake: IntakeLoop,
    store: ExecutionStore,
    dynamodb_client: Any,
    executions_table: str,
    s3_client: Any,
    sqs_client: Any,
    queue_url: str,
) -> None:
    _put_valid_manifest(s3_client)
    sent_before_ms = _send(sqs_client, queue_url, _body())
    terminal = ExecutionItem(
        pipelineExecutionId=PID,
        state=State.SUCCEEDED,
        version=7,
        taskToken=OLD_TOKEN,
        tokenSentAt=sent_before_ms - ONE_MINUTE_MS,
        workflowDatabaseId="db1",
        workflowId="wf1",
        workflowExecutionId="exec1",
        inputManifestS3Location=MANIFEST_URI,
        outcome={"status": "SUCCEEDED", "errorName": None, "cause": None},
        notifyResult="ACCEPTED",
        createdAt="2026-10-16T00:00:00Z",
        updatedAt="2026-10-16T01:00:00Z",
        terminalAt="2026-10-16T01:00:00Z",
    )
    dynamodb_client.put_item(TableName=executions_table, Item=terminal.to_item())

    intake.poll_once()

    item = _stored(store)
    assert item.state is State.SUCCEEDED
    assert item.taskToken == TOKEN
    assert item.resendPending is True
    assert item.pending == PENDING_CLEANUP
    assert item.version == 8
    assert PID in store.query_pending(PENDING_CLEANUP)
    assert _queued_message_count(sqs_client, queue_url) == 0


def test_body_missing_fields_with_token_sends_failure_and_deletes(
    intake: IntakeLoop,
    store: ExecutionStore,
    sfn_stubber: tuple[Any, Stubber],
    sqs_client: Any,
    queue_url: str,
) -> None:
    _expect_invalid_request_failure(sfn_stubber[1], TOKEN)
    _send(sqs_client, queue_url, _body(workflowId=""))

    assert intake.poll_once() == 1

    assert store.get(PID) is None
    assert _queued_message_count(sqs_client, queue_url) == 0


def test_invalid_pipeline_id_sends_failure_without_token_in_cause(
    intake: IntakeLoop,
    store: ExecutionStore,
    sfn_stubber: tuple[Any, Stubber],
    s3_client: Any,
    sqs_client: Any,
    queue_url: str,
) -> None:
    _, stubber = sfn_stubber
    captured: dict[str, Any] = {}
    stubber.add_response("send_task_failure", {})
    sfn_stubber[0].meta.events.register(
        "provide-client-params.states.SendTaskFailure",
        lambda params, **_: captured.update(params),
    )
    _put_manifest(s3_client, "vams.execution.exec1.pipeline.bad/id")
    _send(sqs_client, queue_url, _body())

    intake.poll_once()

    assert captured["taskToken"] == TOKEN
    assert captured["error"] == "PASK.InvalidRequest"
    assert TOKEN not in captured["cause"]
    assert _queued_message_count(sqs_client, queue_url) == 0


def test_missing_manifest_sends_failure_and_deletes(
    intake: IntakeLoop, sfn_stubber: tuple[Any, Stubber], sqs_client: Any, queue_url: str
) -> None:
    _expect_invalid_request_failure(sfn_stubber[1], TOKEN)
    _send(sqs_client, queue_url, _body())

    intake.poll_once()

    assert _queued_message_count(sqs_client, queue_url) == 0


def test_closed_token_of_invalid_request_still_deletes(
    intake: IntakeLoop, sfn_stubber: tuple[Any, Stubber], sqs_client: Any, queue_url: str
) -> None:
    sfn_stubber[1].add_client_error(
        "send_task_failure", service_error_code="TaskTimedOut", http_status_code=400
    )
    _send(sqs_client, queue_url, _body(workflowId=""))

    intake.poll_once()

    assert _queued_message_count(sqs_client, queue_url) == 0


def test_throttled_failure_report_keeps_message(
    intake: IntakeLoop, sfn_stubber: tuple[Any, Stubber], sqs_client: Any, queue_url: str
) -> None:
    sfn_stubber[1].add_client_error(
        "send_task_failure", service_error_code="ThrottlingException", http_status_code=400
    )
    _send(sqs_client, queue_url, _body(workflowId=""))

    intake.poll_once()

    assert _queued_message_count(sqs_client, queue_url) == 1


@pytest.mark.parametrize("body", ["not json", json.dumps({"body": {"workflowId": "wf1"}})])
def test_message_without_token_is_not_deleted(
    intake: IntakeLoop, store: ExecutionStore, sqs_client: Any, queue_url: str, body: str
) -> None:
    _send(sqs_client, queue_url, body)

    assert intake.poll_once() == 1

    assert store.get(PID) is None
    assert _queued_message_count(sqs_client, queue_url) == 1


def test_manifest_s3_error_keeps_message(
    intake: IntakeLoop, store: ExecutionStore, sqs_client: Any, queue_url: str
) -> None:
    _send(
        sqs_client,
        queue_url,
        _body(inputManifestS3Location="s3://vams-test-missing-bucket/manifest.json"),
    )

    intake.poll_once()

    assert store.get(PID) is None
    assert _queued_message_count(sqs_client, queue_url) == 1


def test_dynamodb_failure_keeps_message(
    poller_config: PollerConfig,
    queue_url: str,
    dynamodb_client: Any,
    sfn_stubber: tuple[Any, Stubber],
    s3_client: Any,
    sqs_client: Any,
    workflow_bucket: str,
    training_env: dict[str, str],
    fixed_clock: Callable[[], float],
) -> None:
    missing_table_store = ExecutionStore(
        "pask-vams-test-missing-table",
        region=TEST_REGION,
        owner_id=OWNER,
        lease_seconds=900,
        ttl_days=30,
        clock=fixed_clock,
        dynamodb_client=dynamodb_client,
    )
    context = _make_context(
        poller_config,
        queue_url,
        missing_table_store,
        sfn_stubber[0],
        s3_client,
        sqs_client,
        training_env,
    )
    _put_valid_manifest(s3_client)
    _send(sqs_client, queue_url, _body())

    assert IntakeLoop(context).poll_once() == 1

    assert _queued_message_count(sqs_client, queue_url) == 1


def test_batch_continues_after_a_failed_message(
    intake: IntakeLoop, store: ExecutionStore, s3_client: Any, sqs_client: Any, queue_url: str
) -> None:
    _put_valid_manifest(s3_client)
    _send(sqs_client, queue_url, "not json")
    _send(sqs_client, queue_url, _body())

    received = 0
    while received < 2:
        received += intake.poll_once()

    assert _stored(store).taskToken == TOKEN
    assert _queued_message_count(sqs_client, queue_url) == 1


def test_run_stops_when_stop_event_is_set(intake: IntakeLoop) -> None:
    stop_event = threading.Event()
    stop_event.set()

    intake.run(stop_event)
