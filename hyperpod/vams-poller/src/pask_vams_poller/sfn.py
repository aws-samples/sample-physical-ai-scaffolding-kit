"""Step Functions task token callbacks: heartbeat, success, and failure."""

import json
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from botocore.exceptions import ClientError, ConnectionError, HTTPClientError

from pask_vams_poller.constants import CLOSED_TOKEN_ERRORS, MAX_CAUSE_LENGTH, MAX_ERROR_LENGTH
from pask_vams_poller.errors import TransientError

THROTTLING_ERROR_CODES: frozenset[str] = frozenset(
    {
        "ThrottlingException",
        "Throttling",
        "TooManyRequestsException",
        "RequestLimitExceeded",
    }
)
SERVER_ERROR_STATUS = 500


class CallResult(Enum):
    """Result of one task token call."""

    ACCEPTED = "ACCEPTED"
    CLOSED = "CLOSED"


@dataclass
class TokenCallOutcome:
    """Result of ``call_with_latest_token``.

    Attributes:
        result: ACCEPTED, or CLOSED when the latest token is closed.
        token: The token used by the last call.
        replaced: True when the token was replaced by a newer one on the way.
    """

    result: CallResult
    token: str
    replaced: bool


def _error_code(error: ClientError) -> str:
    """Read the error code of a ``ClientError``.

    Args:
        error: The error.

    Returns:
        The code, or an empty string.
    """
    return error.response.get("Error", {}).get("Code", "")


def _is_transient(error: ClientError) -> bool:
    """Tell whether a ``ClientError`` is throttling or a server side error.

    Args:
        error: The error left after botocore's own retries.

    Returns:
        True for throttling and HTTP 5xx.
    """
    status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0)
    return _error_code(error) in THROTTLING_ERROR_CODES or status >= SERVER_ERROR_STATUS


class SfnNotifier:
    """Send ``SendTaskHeartbeat``, ``SendTaskSuccess``, and ``SendTaskFailure``.

    Throttling and 5xx are retried by the client's botocore retry configuration (create
    the client with ``retries={"mode": "standard"}``). What is still left after those
    retries becomes ``TransientError``.
    """

    def __init__(self, client: Any) -> None:
        """Create the notifier.

        Args:
            client: boto3 Step Functions client.
        """
        self._client = client

    def heartbeat(self, token: str) -> CallResult:
        """Send a heartbeat.

        Args:
            token: Task token.

        Returns:
            ACCEPTED, or CLOSED when the token is closed.

        Raises:
            TransientError: Throttling, 5xx, or a connection error remained after retries.
            ClientError: Any other API error.
        """
        return self._call(self._client.send_task_heartbeat, taskToken=token)

    def send_success(self, token: str, output: dict[str, Any]) -> CallResult:
        """Report success.

        Args:
            token: Task token.
            output: JSON serializable task output.

        Returns:
            ACCEPTED, or CLOSED when the token is closed.

        Raises:
            TransientError: Throttling, 5xx, or a connection error remained after retries.
            ClientError: Any other API error.
        """
        return self._call(
            self._client.send_task_success, taskToken=token, output=json.dumps(output)
        )

    def send_failure(self, token: str, error: str, cause: str) -> CallResult:
        """Report failure.

        Args:
            token: Task token.
            error: Error name. Truncated to ``MAX_ERROR_LENGTH`` characters.
            cause: Failure cause. Truncated to ``MAX_CAUSE_LENGTH`` characters.

        Returns:
            ACCEPTED, or CLOSED when the token is closed.

        Raises:
            TransientError: Throttling, 5xx, or a connection error remained after retries.
            ClientError: Any other API error.
        """
        return self._call(
            self._client.send_task_failure,
            taskToken=token,
            error=error[:MAX_ERROR_LENGTH],
            cause=cause[:MAX_CAUSE_LENGTH],
        )

    @staticmethod
    def _call(operation: Callable[..., Any], **kwargs: Any) -> CallResult:
        """Call one Step Functions operation and classify its result.

        Args:
            operation: Bound client method.
            **kwargs: Arguments of the operation.

        Returns:
            ACCEPTED, or CLOSED for an error in ``CLOSED_TOKEN_ERRORS``.

        Raises:
            TransientError: Throttling, 5xx, or a connection error.
            ClientError: Any other API error.
        """
        try:
            operation(**kwargs)
        except ClientError as error:
            if _error_code(error) in CLOSED_TOKEN_ERRORS:
                return CallResult.CLOSED
            if _is_transient(error):
                raise TransientError(f"Step Functions call failed: {_error_code(error)}") from error
            raise
        except (ConnectionError, HTTPClientError) as error:
            raise TransientError(f"Step Functions call failed: {error}") from error
        return CallResult.ACCEPTED


def call_with_latest_token(
    op: Callable[[str], CallResult],
    used_token: str,
    read_latest_token: Callable[[], str | None],
    max_rounds: int = 3,
) -> TokenCallOutcome:
    """Call a task token operation and retry it with a newer token when the token is closed.

    VAMS may resend the same pipeline execution with a new TaskToken. When a call returns
    CLOSED, the latest token is read again. A different token is retried; the same token
    (or no token) means the execution is really closed.

    Args:
        op: Operation that takes a token, such as ``SfnNotifier.heartbeat``.
        used_token: Token to try first.
        read_latest_token: Reads the latest token with strong consistency, or None.
        max_rounds: Maximum number of calls. Must be at least 1.

    Returns:
        The outcome. CLOSED means the latest token is closed; the caller moves a heartbeat
        to ABORT_SUSPECTED and treats SendTask* as ALREADY_CLOSED.

    Raises:
        ValueError: ``max_rounds`` is less than 1.
        TransientError: The token kept changing for ``max_rounds`` calls, or ``op`` raised it.
    """
    if max_rounds < 1:
        raise ValueError(f"max_rounds must be at least 1, got {max_rounds}")
    token = used_token
    replaced = False
    for _ in range(max_rounds):
        if op(token) is CallResult.ACCEPTED:
            return TokenCallOutcome(result=CallResult.ACCEPTED, token=token, replaced=replaced)
        latest_token = read_latest_token()
        if latest_token is None or latest_token == token:
            return TokenCallOutcome(result=CallResult.CLOSED, token=token, replaced=replaced)
        token = latest_token
        replaced = True
    raise TransientError(f"Task token kept changing for {max_rounds} calls")
