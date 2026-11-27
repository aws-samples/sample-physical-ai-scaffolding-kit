"""Intake loop: receive VAMS requests from SQS and record them in DynamoDB.

The loop only writes execution items. It never copies data, submits jobs, or reports
results, so a message can be received again at any time without side effects. A message
is deleted only after its DynamoDB write is settled (created, token replaced, or no
replacement needed). Malformed requests whose TaskToken is readable are answered with
``SendTaskFailure`` (``PASK.InvalidRequest``) and deleted; requests without a readable
TaskToken are left in the queue so that they move to the dead letter queue.
"""

import logging
import threading
from dataclasses import dataclass
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from pask_vams_poller.constants import ErrorName
from pask_vams_poller.context import PollerContext
from pask_vams_poller.errors import (
    PermanentFailure,
    RequestMismatchError,
    StaleItemError,
    TransientError,
)
from pask_vams_poller.messages import (
    InvalidMessage,
    Manifest,
    VamsRequest,
    load_json_from_s3,
    parse_manifest,
    parse_message_body,
    pipeline_execution_id,
)
from pask_vams_poller.store import REQUEST_IDENTITY_FIELDS

logger = logging.getLogger(__name__)

RECEIVE_WAIT_TIME_SECONDS = 20
RECEIVE_MAX_MESSAGES = 10
RECEIVE_SYSTEM_ATTRIBUTES: tuple[str, ...] = ("SentTimestamp", "ApproximateReceiveCount")
ERROR_BACKOFF_SECONDS = 5.0
INVALID_REQUEST_CAUSE_PREFIX = "Invalid VAMS request: "

MESSAGE_ERRORS: tuple[type[Exception], ...] = (
    ClientError,
    BotoCoreError,
    TransientError,
    StaleItemError,
)


@dataclass(frozen=True)
class _ReceivedMessage:
    """The SQS fields of one received message that the intake loop uses.

    Attributes:
        message_id: SQS ``MessageId``, used in log lines.
        receipt_handle: Handle for ``DeleteMessage``.
        receive_count: ``ApproximateReceiveCount`` as sent by SQS, used in log lines.
        sent_timestamp: ``SentTimestamp`` in epoch milliseconds.
        body: Raw message body.
    """

    message_id: str
    receipt_handle: str
    receive_count: str
    sent_timestamp: int
    body: str


def _to_received_message(message: dict[str, Any]) -> _ReceivedMessage:
    """Take the fields the intake loop needs from a ``ReceiveMessage`` entry.

    Args:
        message: One entry of the ``Messages`` list.

    Returns:
        The message fields.

    Raises:
        ValueError: ``SentTimestamp`` is missing or not an integer. SQS always returns it
            when it is requested, so this means the request was built wrongly.
    """
    attributes = message.get("Attributes", {})
    raw_sent_timestamp = attributes.get("SentTimestamp")
    if raw_sent_timestamp is None or not raw_sent_timestamp.isdigit():
        raise ValueError(f"Message {message['MessageId']} has no valid SentTimestamp")
    return _ReceivedMessage(
        message_id=message["MessageId"],
        receipt_handle=message["ReceiptHandle"],
        receive_count=attributes.get("ApproximateReceiveCount", "unknown"),
        sent_timestamp=int(raw_sent_timestamp),
        body=message["Body"],
    )


def _received_fields(request: VamsRequest, manifest: Manifest, pid: str) -> dict[str, str]:
    """Build the input fields stored on a new RECEIVED execution.

    Args:
        request: Fields of the SQS message body.
        manifest: The parsed manifest.
        pid: The pipelineExecutionId taken from the manifest.

    Returns:
        The fields for ``ExecutionStore.create_received``.
    """
    return {
        "pipelineExecutionId": pid,
        "workflowDatabaseId": request.workflow_database_id,
        "workflowId": request.workflow_id,
        "workflowExecutionId": request.workflow_execution_id,
        "inputManifestS3Location": request.input_manifest_s3_location,
        "inputConfigurationS3Location": request.input_configuration_s3_location,
        "inputMetadataS3Location": manifest.input_metadata_s3_location,
        "executingUserName": request.executing_user_name,
    }


class IntakeLoop:
    """Receive VAMS requests from the queue and record them as execution items."""

    def __init__(self, ctx: PollerContext) -> None:
        """Create the loop.

        Args:
            ctx: Poller context. ``config.queue_url``, ``sqs``, ``s3``, ``store``, and
                ``sfn`` are used.
        """
        self._ctx = ctx

    def run(self, stop_event: threading.Event) -> None:
        """Call ``poll_once`` until ``stop_event`` is set.

        AWS errors of ``ReceiveMessage`` are logged, and the loop waits
        ``ERROR_BACKOFF_SECONDS`` (or until the stop event) before it polls again.

        Args:
            stop_event: Set by the caller to stop the loop after the current poll.
        """
        while not stop_event.is_set():
            try:
                self.poll_once()
            except (ClientError, BotoCoreError):
                logger.exception("Receiving VAMS requests failed; retrying")
                stop_event.wait(ERROR_BACKOFF_SECONDS)

    def poll_once(self) -> int:
        """Receive one batch of messages with long polling and handle each of them.

        A message whose handling fails with an AWS or DynamoDB condition error is left in
        the queue, and the next messages of the batch are still handled.

        Returns:
            The number of messages received and handled in this batch.

        Raises:
            ClientError: ``ReceiveMessage`` failed.
            BotoCoreError: ``ReceiveMessage`` could not reach SQS.
        """
        response = self._ctx.sqs.receive_message(
            QueueUrl=self._ctx.config.queue_url,
            WaitTimeSeconds=RECEIVE_WAIT_TIME_SECONDS,
            VisibilityTimeout=self._ctx.config.visibility_timeout_seconds,
            MaxNumberOfMessages=RECEIVE_MAX_MESSAGES,
            MessageSystemAttributeNames=list(RECEIVE_SYSTEM_ATTRIBUTES),
        )
        messages = response.get("Messages", [])
        for message in messages:
            self._handle_safely(_to_received_message(message))
        return len(messages)

    def _handle_safely(self, message: _ReceivedMessage) -> None:
        """Handle one message and leave it in the queue when an AWS call fails.

        Args:
            message: The received message.
        """
        try:
            self._handle(message)
        except MESSAGE_ERRORS:
            logger.exception(
                "Handling a VAMS request failed; leaving it for redelivery "
                "message_id=%s receive_count=%s",
                message.message_id,
                message.receive_count,
            )

    def _handle(self, message: _ReceivedMessage) -> None:
        """Validate one message, record it, and delete it once the record is settled.

        Args:
            message: The received message.

        Raises:
            ClientError: An S3, DynamoDB, Step Functions, or SQS call failed. S3 errors
                other than a missing object are not treated as a malformed request.
            BotoCoreError: An AWS endpoint could not be reached.
            TransientError: ``SendTaskFailure`` was throttled or the item disappeared.
            StaleItemError: Concurrent updates kept changing the item.
        """
        try:
            request = parse_message_body(message.body)
        except InvalidMessage as error:
            self._handle_invalid_body(message, error)
            return
        try:
            manifest = parse_manifest(
                load_json_from_s3(self._ctx.s3, request.input_manifest_s3_location)
            )
            pid = pipeline_execution_id(manifest)
        except PermanentFailure as failure:
            self._reject(message, request.task_token, failure.cause)
            return
        try:
            self._record(message, request, manifest, pid)
        except RequestMismatchError:
            self._reject(
                message,
                request.task_token,
                f"pipelineExecutionId {pid} is already recorded for another workflow request",
            )
            return
        self._delete(message, pid)

    def _handle_invalid_body(self, message: _ReceivedMessage, error: InvalidMessage) -> None:
        """Answer a malformed body when its TaskToken is readable, otherwise keep it.

        Args:
            message: The received message.
            error: Why the body is not a valid request.
        """
        if error.task_token is None:
            logger.error(
                "VAMS request has no readable TaskToken; leaving it for the dead letter queue "
                "message_id=%s receive_count=%s reason=%s",
                message.message_id,
                message.receive_count,
                error.reason,
            )
            return
        self._reject(message, error.task_token, error.reason)

    def _reject(self, message: _ReceivedMessage, task_token: str, reason: str) -> None:
        """Send ``SendTaskFailure`` with ``PASK.InvalidRequest`` and delete the message.

        The message is deleted also when the token is already closed.

        Args:
            message: The received message.
            task_token: TaskToken read from the body. It is never logged.
            reason: Why the request is invalid. It must not contain the token.
        """
        result = self._ctx.sfn.send_failure(
            task_token, ErrorName.INVALID_REQUEST.value, INVALID_REQUEST_CAUSE_PREFIX + reason
        )
        logger.warning(
            "Rejected an invalid VAMS request message_id=%s receive_count=%s "
            "send_task_failure=%s reason=%s",
            message.message_id,
            message.receive_count,
            result.value,
            reason,
        )
        self._delete(message, None)

    def _record(
        self, message: _ReceivedMessage, request: VamsRequest, manifest: Manifest, pid: str
    ) -> None:
        """Create the RECEIVED execution, or replace its token with a newer one.

        Args:
            message: The received message.
            request: Fields of the message body.
            manifest: The parsed manifest.
            pid: The pipelineExecutionId.

        Raises:
            TransientError: The item existed when it was created but is gone now.
            RequestMismatchError: The stored execution with this pipelineExecutionId has
                other workflow identifiers or another manifest location.
        """
        store = self._ctx.store
        fields = _received_fields(request, manifest, pid)
        if store.create_received(fields, request.task_token, message.sent_timestamp):
            self._log_recorded("Recorded a new VAMS request", message, pid)
            return
        request_identity = {name: fields[name] for name in REQUEST_IDENTITY_FIELDS}
        replaced = store.replace_token(
            pid, request.task_token, message.sent_timestamp, request_identity
        )
        if replaced is not None:
            self._log_recorded(
                f"Replaced the task token state={replaced.state.value} "
                f"resend_pending={replaced.resendPending}",
                message,
                pid,
            )
            return
        if store.get(pid) is None:
            raise TransientError(f"Execution {pid} disappeared while recording a message")
        self._log_recorded(
            "Kept the stored task token of the same or a newer message", message, pid
        )

    def _delete(self, message: _ReceivedMessage, pid: str | None) -> None:
        """Delete a message whose handling is settled.

        Args:
            message: The received message.
            pid: The pipelineExecutionId, or None when it is unknown.
        """
        self._ctx.sqs.delete_message(
            QueueUrl=self._ctx.config.queue_url, ReceiptHandle=message.receipt_handle
        )
        self._log_recorded("Deleted the VAMS request message", message, pid)

    @staticmethod
    def _log_recorded(event: str, message: _ReceivedMessage, pid: str | None) -> None:
        """Log one intake step with the identifiers of the message.

        Args:
            event: What happened.
            message: The received message.
            pid: The pipelineExecutionId, or None when it is unknown.
        """
        logger.info(
            "%s pid=%s message_id=%s receive_count=%s",
            event,
            pid,
            message.message_id,
            message.receive_count,
        )
