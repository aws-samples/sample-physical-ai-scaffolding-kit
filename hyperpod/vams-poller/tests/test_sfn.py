"""Tests for the Step Functions task token callbacks."""

import json
from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from botocore.config import Config
from botocore.exceptions import ClientError
from botocore.stub import Stubber

from pask_vams_poller.constants import CLOSED_TOKEN_ERRORS, MAX_CAUSE_LENGTH, MAX_ERROR_LENGTH
from pask_vams_poller.errors import TransientError
from pask_vams_poller.sfn import CallResult, SfnNotifier, call_with_latest_token


@pytest.fixture
def sfn_client() -> Any:
    """Return a Step Functions client that does not retry, so each stub answers one call."""
    return boto3.client(
        "stepfunctions",
        region_name="us-west-2",
        config=Config(retries={"mode": "standard", "max_attempts": 1}),
    )


@pytest.fixture
def stubber(sfn_client: Any) -> Iterator[Stubber]:
    """Activate a stubber on the client and check that every stubbed call was used."""
    with Stubber(sfn_client) as active_stubber:
        yield active_stubber
        active_stubber.assert_no_pending_responses()


def test_heartbeat_accepted(sfn_client: Any, stubber: Stubber) -> None:
    stubber.add_response("send_task_heartbeat", {}, {"taskToken": "token-1"})

    assert SfnNotifier(sfn_client).heartbeat("token-1") is CallResult.ACCEPTED


def test_send_success_serializes_output(sfn_client: Any, stubber: Stubber) -> None:
    output = {"status": "SUCCEEDED", "slurmJobId": "42"}
    stubber.add_response(
        "send_task_success", {}, {"taskToken": "token-1", "output": json.dumps(output)}
    )

    assert SfnNotifier(sfn_client).send_success("token-1", output) is CallResult.ACCEPTED


def test_send_failure_truncates_error_and_cause(sfn_client: Any, stubber: Stubber) -> None:
    error = "E" * (MAX_ERROR_LENGTH + 10)
    cause = "C" * (MAX_CAUSE_LENGTH + 10)
    stubber.add_response(
        "send_task_failure",
        {},
        {
            "taskToken": "token-1",
            "error": "E" * MAX_ERROR_LENGTH,
            "cause": "C" * MAX_CAUSE_LENGTH,
        },
    )

    assert SfnNotifier(sfn_client).send_failure("token-1", error, cause) is CallResult.ACCEPTED


@pytest.mark.parametrize("code", sorted(CLOSED_TOKEN_ERRORS))
def test_closed_token_errors_return_closed(code: str, sfn_client: Any, stubber: Stubber) -> None:
    notifier = SfnNotifier(sfn_client)
    stubber.add_client_error("send_task_heartbeat", service_error_code=code, http_status_code=400)
    stubber.add_client_error("send_task_success", service_error_code=code, http_status_code=400)
    stubber.add_client_error("send_task_failure", service_error_code=code, http_status_code=400)

    assert notifier.heartbeat("token-1") is CallResult.CLOSED
    assert notifier.send_success("token-1", {}) is CallResult.CLOSED
    assert notifier.send_failure("token-1", "PASK.JobFailed", "exit 1") is CallResult.CLOSED


@pytest.mark.parametrize(
    ("code", "status"),
    [("ThrottlingException", 400), ("ServiceUnavailable", 503), ("InternalFailure", 500)],
)
def test_throttling_and_server_errors_raise_transient(
    code: str, status: int, sfn_client: Any, stubber: Stubber
) -> None:
    stubber.add_client_error(
        "send_task_heartbeat", service_error_code=code, http_status_code=status
    )

    with pytest.raises(TransientError):
        SfnNotifier(sfn_client).heartbeat("token-1")


def test_other_client_errors_propagate(sfn_client: Any, stubber: Stubber) -> None:
    stubber.add_client_error(
        "send_task_heartbeat", service_error_code="AccessDeniedException", http_status_code=400
    )

    with pytest.raises(ClientError) as failure:
        SfnNotifier(sfn_client).heartbeat("token-1")

    assert failure.value.response["Error"]["Code"] == "AccessDeniedException"


def test_call_with_latest_token_accepted_first_time() -> None:
    sent_tokens: list[str] = []

    def operation(token: str) -> CallResult:
        sent_tokens.append(token)
        return CallResult.ACCEPTED

    outcome = call_with_latest_token(operation, "token-1", lambda: pytest.fail("not read"))

    assert outcome.result is CallResult.ACCEPTED
    assert outcome.token == "token-1"
    assert outcome.replaced is False
    assert sent_tokens == ["token-1"]


def test_call_with_latest_token_resends_with_replaced_token(
    sfn_client: Any, stubber: Stubber
) -> None:
    stubber.add_client_error(
        "send_task_heartbeat",
        service_error_code="TaskTimedOut",
        http_status_code=400,
        expected_params={"taskToken": "token-1"},
    )
    stubber.add_response("send_task_heartbeat", {}, {"taskToken": "token-2"})

    outcome = call_with_latest_token(
        SfnNotifier(sfn_client).heartbeat, "token-1", lambda: "token-2"
    )

    assert outcome.result is CallResult.ACCEPTED
    assert outcome.token == "token-2"
    assert outcome.replaced is True


def test_call_with_latest_token_closed_when_token_unchanged(
    sfn_client: Any, stubber: Stubber
) -> None:
    stubber.add_client_error(
        "send_task_success", service_error_code="InvalidToken", http_status_code=400
    )
    notifier = SfnNotifier(sfn_client)

    outcome = call_with_latest_token(
        lambda token: notifier.send_success(token, {}), "token-1", lambda: "token-1"
    )

    assert outcome.result is CallResult.CLOSED
    assert outcome.token == "token-1"
    assert outcome.replaced is False


def test_call_with_latest_token_closed_when_no_token_is_stored() -> None:
    outcome = call_with_latest_token(lambda token: CallResult.CLOSED, "token-1", lambda: None)

    assert outcome.result is CallResult.CLOSED
    assert outcome.token == "token-1"


def test_call_with_latest_token_closed_after_replacement() -> None:
    latest_tokens = iter(["token-2", "token-2"])

    outcome = call_with_latest_token(
        lambda token: CallResult.CLOSED, "token-1", lambda: next(latest_tokens)
    )

    assert outcome.result is CallResult.CLOSED
    assert outcome.token == "token-2"
    assert outcome.replaced is True


def test_call_with_latest_token_gives_up_when_token_keeps_changing() -> None:
    latest_tokens = iter(["token-2", "token-3", "token-4"])
    sent_tokens: list[str] = []

    def operation(token: str) -> CallResult:
        sent_tokens.append(token)
        return CallResult.CLOSED

    with pytest.raises(TransientError):
        call_with_latest_token(operation, "token-1", lambda: next(latest_tokens), max_rounds=3)

    assert sent_tokens == ["token-1", "token-2", "token-3"]


def test_call_with_latest_token_rejects_zero_rounds() -> None:
    with pytest.raises(ValueError):
        call_with_latest_token(lambda token: CallResult.ACCEPTED, "token-1", lambda: None, 0)
