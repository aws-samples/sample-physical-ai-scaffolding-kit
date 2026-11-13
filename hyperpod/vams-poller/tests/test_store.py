"""Tests for the DynamoDB execution store."""

from typing import Any

import pytest
from botocore.exceptions import ClientError

from pask_vams_poller.constants import (
    CONCURRENCY_ITEM_ID,
    MAX_CAUSE_LENGTH,
    PENDING_ACTIVE,
    PENDING_CLEANUP,
    ErrorName,
    State,
)
from pask_vams_poller.errors import RequestMismatchError, StaleItemError
from pask_vams_poller.store import REQUEST_IDENTITY_FIELDS, ExecutionItem, ExecutionStore

from .conftest import FIXED_EPOCH_SECONDS, TEST_REGION

OWNER = "login-node:invocation-a"
OTHER_OWNER = "login-node:invocation-b"
LEASE_SECONDS = 900
TTL_DAYS = 30
PID = "exec_0001-a"
TOKEN = "token-1"
SENT_AT = 1_789_999_000_000
FIELDS = {
    "pipelineExecutionId": PID,
    "workflowDatabaseId": "db1",
    "workflowId": "wf1",
    "workflowExecutionId": "wfx1",
    "inputManifestS3Location": "s3://workflow-bucket/manifest.json",
    "inputConfigurationS3Location": "s3://workflow-bucket/config.json",
    "inputMetadataS3Location": "s3://workflow-bucket/metadata.json",
    "executingUserName": "alice",
}
IDENTITY = {name: FIELDS[name] for name in REQUEST_IDENTITY_FIELDS}
FAILED_OUTCOME = {"status": "FAILED", "errorName": ErrorName.JOB_FAILED.value, "cause": "exit 1"}
SUCCEEDED_OUTCOME = {"status": "SUCCEEDED", "errorName": None, "cause": None}


class MutableClock:
    """Clock whose epoch seconds the test can move forward."""

    def __init__(self, now: float) -> None:
        """Start the clock at ``now`` epoch seconds."""
        self.now = now

    def __call__(self) -> float:
        """Return the current epoch seconds."""
        return self.now

    def advance(self, seconds: float) -> None:
        """Move the clock forward by ``seconds``."""
        self.now += seconds


@pytest.fixture
def clock() -> MutableClock:
    """Return a clock that starts at the shared fixed epoch seconds."""
    return MutableClock(FIXED_EPOCH_SECONDS)


def _make_store(client: Any, table: str, clock: MutableClock, owner: str) -> ExecutionStore:
    """Create a store for ``owner`` on the moto table."""
    return ExecutionStore(
        table,
        region=TEST_REGION,
        owner_id=owner,
        lease_seconds=LEASE_SECONDS,
        ttl_days=TTL_DAYS,
        clock=clock,
        dynamodb_client=client,
    )


@pytest.fixture
def store(dynamodb_client: Any, executions_table: str, clock: MutableClock) -> ExecutionStore:
    """Return a store owned by ``OWNER``."""
    return _make_store(dynamodb_client, executions_table, clock, OWNER)


@pytest.fixture
def other_store(dynamodb_client: Any, executions_table: str, clock: MutableClock) -> ExecutionStore:
    """Return a store owned by ``OTHER_OWNER`` on the same table and clock."""
    return _make_store(dynamodb_client, executions_table, clock, OTHER_OWNER)


def _received(store: ExecutionStore, pid: str = PID) -> ExecutionItem:
    """Create a RECEIVED execution and take its lease."""
    assert store.create_received({**FIELDS, "pipelineExecutionId": pid}, TOKEN, SENT_AT)
    leased = store.acquire_lease(pid)
    assert leased is not None
    return leased


def _staging(store: ExecutionStore, pid: str = PID) -> ExecutionItem:
    """Create an execution in STAGING that holds a concurrency slot."""
    item = _received(store, pid)
    assert store.start_staging(item, {"stagingPrefix": f"vams/{pid}/"}, max_concurrent=5)
    staged = store.get(pid)
    assert staged is not None
    return staged


def _running(store: ExecutionStore) -> ExecutionItem:
    """Move an execution through STAGING, SUBMITTING, and SUBMITTED to RUNNING."""
    item = _staging(store)
    item = store.transition(item, State.SUBMITTING, set_attrs={"jobName": f"vams-{PID}"})
    item = store.transition(item, State.SUBMITTED, set_attrs={"slurmJobId": "42"})
    return store.transition(item, State.RUNNING)


def _terminal(store: ExecutionStore, outcome: dict[str, Any]) -> ExecutionItem:
    """Move an execution that holds a slot to the terminal state of ``outcome``."""
    finalizing = store.to_finalizing(_running(store), outcome)
    return store.to_notified_terminal(finalizing, "ACCEPTED")


def test_create_received_writes_initial_item(store: ExecutionStore) -> None:
    assert store.create_received(FIELDS, TOKEN, SENT_AT)

    item = store.get(PID)
    assert item is not None
    assert item.state is State.RECEIVED
    assert item.version == 1
    assert item.pending == PENDING_ACTIVE
    assert item.taskToken == TOKEN
    assert item.tokenSentAt == SENT_AT
    assert item.executingUserName == "alice"
    assert item.createdAt == "2026-09-21T14:13:20Z"
    assert item.updatedAt == item.createdAt
    assert item.holdsSlot is False
    assert item.resendPending is False
    assert item.leaseOwner is None


def test_create_received_returns_false_when_item_exists(store: ExecutionStore) -> None:
    assert store.create_received(FIELDS, TOKEN, SENT_AT)

    assert not store.create_received(FIELDS, "token-2", SENT_AT + 1)
    item = store.get(PID)
    assert item is not None
    assert item.taskToken == TOKEN
    assert item.version == 1


@pytest.mark.parametrize("pid", ["", "a/b", "../x", "#CONCURRENCY", "id with space"])
def test_create_received_rejects_unsafe_ids(store: ExecutionStore, pid: str) -> None:
    with pytest.raises(ValueError, match="Invalid pipelineExecutionId"):
        store.create_received({**FIELDS, "pipelineExecutionId": pid}, TOKEN, SENT_AT)


def test_create_received_rejects_unknown_fields(store: ExecutionStore) -> None:
    with pytest.raises(ValueError, match="Unknown fields"):
        store.create_received({**FIELDS, "state": "RUNNING"}, TOKEN, SENT_AT)


def test_item_round_trips_through_dynamodb_format() -> None:
    item = ExecutionItem(
        pipelineExecutionId=PID,
        state=State.ABORT_SUSPECTED,
        version=7,
        tokenSentAt=SENT_AT,
        outcome={"status": "FAILED", "errorName": "PASK.JobLost", "cause": "lost"},
        outputs=["a/b.bin", "results/pask_training.json"],
        prevState=State.RUNNING,
        suspectedAt=1_790_000_100,
        holdsSlot=True,
    )

    raw = item.to_item()

    assert raw["state"] == {"S": "ABORT_SUSPECTED"}
    assert raw["holdsSlot"] == {"BOOL": True}
    assert "resendPending" not in raw
    assert "taskToken" not in raw
    assert ExecutionItem.from_item(raw) == item


def test_item_from_dynamodb_fills_missing_outcome_entries() -> None:
    raw = ExecutionItem(pipelineExecutionId=PID, state=State.FINALIZING, version=3).to_item()
    raw["outcome"] = {"M": {"status": {"S": "SUCCEEDED"}}}

    item = ExecutionItem.from_item(raw)

    assert item.outcome == SUCCEEDED_OUTCOME


def test_replace_token_accepts_newer_message(store: ExecutionStore) -> None:
    item = _received(store)

    replaced = store.replace_token(PID, "token-2", SENT_AT + 1000, IDENTITY)

    assert replaced is not None
    assert replaced.taskToken == "token-2"
    assert replaced.tokenSentAt == SENT_AT + 1000
    assert replaced.version == item.version + 1
    assert replaced.state is State.RECEIVED
    assert replaced.resendPending is False
    with pytest.raises(StaleItemError):
        store.start_staging(item, {}, max_concurrent=1)


@pytest.mark.parametrize("sent_at", [SENT_AT, SENT_AT - 1])
def test_replace_token_rejects_same_or_older_message(store: ExecutionStore, sent_at: int) -> None:
    _received(store)

    assert store.replace_token(PID, "token-old", sent_at, IDENTITY) is None
    item = store.get(PID)
    assert item is not None
    assert item.taskToken == TOKEN
    assert item.version == 1


def test_replace_token_returns_none_for_missing_item(store: ExecutionStore) -> None:
    assert store.replace_token("missing", "token-2", SENT_AT, IDENTITY) is None


@pytest.mark.parametrize("field", REQUEST_IDENTITY_FIELDS)
def test_replace_token_rejects_another_request(store: ExecutionStore, field: str) -> None:
    store.create_received(FIELDS, TOKEN, SENT_AT)
    other_identity = {**IDENTITY, field: "other"}

    with pytest.raises(RequestMismatchError):
        store.replace_token(PID, "token-2", SENT_AT + 1000, other_identity)

    item = store.get(PID)
    assert item is not None
    assert item.taskToken == TOKEN
    assert item.version == 1


def test_replace_token_requires_all_identity_fields(store: ExecutionStore) -> None:
    store.create_received(FIELDS, TOKEN, SENT_AT)
    partial_identity = {"workflowId": FIELDS["workflowId"]}

    with pytest.raises(ValueError):
        store.replace_token(PID, "token-2", SENT_AT + 1000, partial_identity)


def test_replace_token_on_terminal_sets_resend_pending(store: ExecutionStore) -> None:
    terminal = _terminal(store, SUCCEEDED_OUTCOME)

    replaced = store.replace_token(PID, "token-2", SENT_AT + 1, IDENTITY)

    assert replaced is not None
    assert replaced.state is State.SUCCEEDED
    assert replaced.resendPending is True
    assert replaced.pending == PENDING_CLEANUP
    assert replaced.version == terminal.version + 1


def test_replace_token_on_cleaned_terminal_restores_cleanup(store: ExecutionStore) -> None:
    store.complete_cleanup(_terminal(store, FAILED_OUTCOME))
    cleaned = store.get(PID)
    assert cleaned is not None
    assert cleaned.pending is None

    replaced = store.replace_token(PID, "token-2", SENT_AT + 1, IDENTITY)

    assert replaced is not None
    assert replaced.pending == PENDING_CLEANUP
    assert replaced.resendPending is True
    assert PID in store.query_pending(PENDING_CLEANUP)


def test_acquire_lease_is_exclusive_until_expiry(
    store: ExecutionStore, other_store: ExecutionStore, clock: MutableClock
) -> None:
    item = _received(store)
    assert item.leaseOwner == OWNER
    assert item.leaseExpiresAt == int(FIXED_EPOCH_SECONDS) + LEASE_SECONDS
    assert item.version == 1

    assert other_store.acquire_lease(PID) is None
    assert store.acquire_lease(PID) is not None

    clock.advance(LEASE_SECONDS + 1)
    taken = other_store.acquire_lease(PID)

    assert taken is not None
    assert taken.leaseOwner == OTHER_OWNER
    assert taken.version == 1


def test_acquire_lease_returns_none_for_missing_item(store: ExecutionStore) -> None:
    assert store.acquire_lease("missing") is None
    assert store.get("missing") is None


def test_renew_and_release_lease_only_for_owner(
    store: ExecutionStore, other_store: ExecutionStore, clock: MutableClock
) -> None:
    item = _received(store)

    clock.advance(100)
    assert store.renew_lease(item)
    renewed = store.get(PID)
    assert renewed is not None
    assert renewed.leaseExpiresAt == int(FIXED_EPOCH_SECONDS) + 100 + LEASE_SECONDS

    assert not other_store.renew_lease(item)
    assert not other_store.release_lease(item)
    assert store.release_lease(item)
    released = store.get(PID)
    assert released is not None
    assert released.leaseOwner is None
    assert released.leaseExpiresAt is None
    assert other_store.acquire_lease(PID) is not None


def test_transition_increments_version_and_sets_attributes(
    store: ExecutionStore, clock: MutableClock
) -> None:
    item = _staging(store)
    clock.advance(60)

    moved = store.transition(item, State.SUBMITTING, set_attrs={"jobName": f"vams-{PID}"})

    assert moved.state is State.SUBMITTING
    assert moved.version == item.version + 1
    assert moved.jobName == f"vams-{PID}"
    assert moved.updatedAt == "2026-09-21T14:14:20Z"
    assert moved.holdsSlot is True


def test_transition_rejects_version_mismatch(store: ExecutionStore) -> None:
    item = _staging(store)
    store.transition(item, State.SUBMITTING)

    with pytest.raises(StaleItemError):
        store.transition(item, State.SUBMITTING)


def test_transition_rejects_wrong_source_state(store: ExecutionStore) -> None:
    item = _staging(store)
    wrong = ExecutionItem(**{**item.__dict__, "state": State.SUBMITTED})

    with pytest.raises(StaleItemError):
        store.transition(wrong, State.RUNNING)


def test_transition_rejects_expired_lease(store: ExecutionStore, clock: MutableClock) -> None:
    item = _staging(store)
    clock.advance(LEASE_SECONDS + 1)

    with pytest.raises(StaleItemError):
        store.transition(item, State.SUBMITTING)


def test_transition_rejects_lease_of_other_owner(
    store: ExecutionStore, other_store: ExecutionStore
) -> None:
    item = _staging(store)

    with pytest.raises(StaleItemError):
        other_store.transition(item, State.SUBMITTING)


def test_transition_applies_extra_condition(store: ExecutionStore) -> None:
    item = _staging(store)
    condition = ("#runDir = :runDir", {"#runDir": "runDir"}, {":runDir": "/other"})

    with pytest.raises(StaleItemError):
        store.transition(item, State.SUBMITTING, extra_condition=condition)


@pytest.mark.parametrize("attribute", ["state", "version", "leaseOwner", "pending", "holdsSlot"])
def test_transition_rejects_managed_attributes(store: ExecutionStore, attribute: str) -> None:
    item = _staging(store)

    with pytest.raises(ValueError, match="managed"):
        store.transition(item, State.SUBMITTING, set_attrs={attribute: "x"})


def test_transition_rejects_terminal_target_and_unmetered_staging(store: ExecutionStore) -> None:
    item = _received(store)

    with pytest.raises(ValueError, match="start_staging"):
        store.transition(item, State.STAGING)
    with pytest.raises(ValueError, match="dedicated"):
        store.transition(item, State.SUCCEEDED)


def test_start_staging_takes_slot(store: ExecutionStore) -> None:
    item = _received(store)

    assert store.start_staging(
        item, {"stagingPrefix": f"vams/{PID}/", "runDir": "/fsx/run"}, max_concurrent=1
    )

    staged = store.get(PID)
    assert staged is not None
    assert staged.state is State.STAGING
    assert staged.version == 2
    assert staged.holdsSlot is True
    assert staged.runDir == "/fsx/run"
    assert store.active_runs() == 1


def test_start_staging_returns_false_at_limit(store: ExecutionStore) -> None:
    first = _received(store, "exec-1")
    second = _received(store, "exec-2")
    assert store.start_staging(first, {}, max_concurrent=1)

    assert not store.start_staging(second, {}, max_concurrent=1)

    waiting = store.get("exec-2")
    assert waiting is not None
    assert waiting.state is State.RECEIVED
    assert waiting.version == second.version
    assert waiting.holdsSlot is False
    assert store.active_runs() == 1


def test_start_staging_stale_item_does_not_take_slot(store: ExecutionStore) -> None:
    item = _received(store)
    store.replace_token(PID, "token-2", SENT_AT + 1, IDENTITY)

    with pytest.raises(StaleItemError):
        store.start_staging(item, {}, max_concurrent=1)

    assert store.active_runs() == 0


def test_start_staging_rejects_non_positive_limit(store: ExecutionStore) -> None:
    with pytest.raises(ValueError, match="max_concurrent"):
        store.start_staging(_received(store), {}, max_concurrent=0)


def test_to_finalizing_from_each_allowed_state(store: ExecutionStore) -> None:
    received = _received(store, "from-received")
    finalizing = store.to_finalizing(received, FAILED_OUTCOME)
    assert finalizing.state is State.FINALIZING
    assert finalizing.outcome == FAILED_OUTCOME

    running = _running(store)
    finalizing = store.to_finalizing(
        running, SUCCEEDED_OUTCOME, set_attrs={"mlflowRunId": "run-1", "mlflowExperimentId": "7"}
    )
    assert finalizing.outcome == SUCCEEDED_OUTCOME
    assert finalizing.mlflowRunId == "run-1"
    assert finalizing.mlflowExperimentId == "7"


def test_to_finalizing_rejects_other_states(store: ExecutionStore) -> None:
    finalizing = store.to_finalizing(_running(store), SUCCEEDED_OUTCOME)

    with pytest.raises(ValueError, match="FINALIZING"):
        store.to_finalizing(finalizing, FAILED_OUTCOME)


def test_to_finalizing_truncates_cause_and_validates_outcome(store: ExecutionStore) -> None:
    item = _received(store)
    with pytest.raises(ValueError, match="errorName and cause"):
        store.to_finalizing(item, {"status": "FAILED", "errorName": None, "cause": None})
    with pytest.raises(ValueError, match="status"):
        store.to_finalizing(item, {"status": "DONE", "errorName": None, "cause": None})

    long_cause = "x" * (MAX_CAUSE_LENGTH + 10)
    finalizing = store.to_finalizing(
        item, {"status": "FAILED", "errorName": "PASK.InvalidRequest", "cause": long_cause}
    )

    assert finalizing.outcome is not None
    assert len(finalizing.outcome["cause"] or "") == MAX_CAUSE_LENGTH


def test_record_outputs_and_rewrite_outcome_stay_in_finalizing(store: ExecutionStore) -> None:
    finalizing = store.to_finalizing(_running(store), SUCCEEDED_OUTCOME)

    recorded = store.record_outputs(finalizing, ["out/model.safetensors"])
    assert recorded.state is State.FINALIZING
    assert recorded.version == finalizing.version + 1
    assert recorded.outputs == ["out/model.safetensors"]

    rewritten = store.rewrite_outcome_failed(
        recorded, ErrorName.OUTPUT_UPLOAD_FAILED, "too many files"
    )
    assert rewritten.state is State.FINALIZING
    assert rewritten.version == recorded.version + 1
    assert rewritten.outcome == {
        "status": "FAILED",
        "errorName": "PASK.OutputUploadFailed",
        "cause": "too many files",
    }

    with pytest.raises(StaleItemError):
        store.to_notified_terminal(recorded, "ACCEPTED")
    failed = store.to_notified_terminal(rewritten, "ACCEPTED")
    assert failed.state is State.FAILED


def test_record_outputs_requires_finalizing(store: ExecutionStore) -> None:
    with pytest.raises(ValueError, match="FINALIZING"):
        store.record_outputs(_staging(store), ["a"])


def test_to_notified_terminal_sets_ttl_and_cleanup(
    store: ExecutionStore, clock: MutableClock
) -> None:
    finalizing = store.to_finalizing(_running(store), SUCCEEDED_OUTCOME)
    clock.advance(600)

    terminal = store.to_notified_terminal(finalizing, "ALREADY_CLOSED")

    terminal_epoch = int(FIXED_EPOCH_SECONDS) + 600
    assert terminal.state is State.SUCCEEDED
    assert terminal.notifyResult == "ALREADY_CLOSED"
    assert terminal.terminalAt == "2026-09-21T14:23:20Z"
    assert terminal.ttl == terminal_epoch + TTL_DAYS * 86400
    assert terminal.pending == PENDING_CLEANUP


def test_to_notified_terminal_rejects_unknown_notify_result(store: ExecutionStore) -> None:
    finalizing = store.to_finalizing(_running(store), SUCCEEDED_OUTCOME)

    with pytest.raises(ValueError, match="notify result"):
        store.to_notified_terminal(finalizing, "MAYBE")


def test_abort_suspected_and_restore(store: ExecutionStore, clock: MutableClock) -> None:
    running = _running(store)

    with pytest.raises(StaleItemError):
        store.to_abort_suspected(running, "not-the-latest-token")
    suspected = store.to_abort_suspected(running, TOKEN)
    assert suspected.state is State.ABORT_SUSPECTED
    assert suspected.prevState is State.RUNNING
    assert suspected.suspectedAt == int(FIXED_EPOCH_SECONDS)

    with pytest.raises(StaleItemError):
        store.restore_from_abort(suspected, TOKEN)

    replaced = store.replace_token(PID, "token-2", SENT_AT + 1, IDENTITY)
    assert replaced is not None
    restored = store.restore_from_abort(replaced, TOKEN)
    assert restored.state is State.RUNNING
    assert restored.prevState is None
    assert restored.suspectedAt is None
    assert restored.holdsSlot is True


def test_abort_suspected_rejects_terminal_and_suspected_states(store: ExecutionStore) -> None:
    suspected = store.to_abort_suspected(_received(store), TOKEN)

    with pytest.raises(ValueError, match="ABORT_SUSPECTED"):
        store.to_abort_suspected(suspected, TOKEN)


def test_to_aborted_waits_for_grace_period(store: ExecutionStore, clock: MutableClock) -> None:
    grace_seconds = 1800
    suspected = store.to_abort_suspected(_running(store), TOKEN)
    assert suspected.suspectedAt is not None

    with pytest.raises(StaleItemError):
        store.to_aborted(suspected, TOKEN, grace_limit=suspected.suspectedAt)

    clock.advance(grace_seconds + 1)
    store.renew_lease(suspected)
    grace_limit = int(clock()) - grace_seconds
    with pytest.raises(StaleItemError):
        store.to_aborted(suspected, "other-token", grace_limit=grace_limit)
    aborted = store.to_aborted(suspected, TOKEN, grace_limit=grace_limit)

    assert aborted.state is State.ABORTED
    assert aborted.pending == PENDING_CLEANUP
    assert aborted.ttl == int(clock()) + TTL_DAYS * 86400
    assert aborted.terminalAt == "2026-09-21T14:43:21Z"


def test_update_notify_result_removes_resend_pending(store: ExecutionStore) -> None:
    _terminal(store, FAILED_OUTCOME)
    replaced = store.replace_token(PID, "token-2", SENT_AT + 1, IDENTITY)
    assert replaced is not None

    with pytest.raises(StaleItemError):
        store.complete_cleanup(replaced)
    updated = store.update_notify_result(replaced, "ACCEPTED")

    assert updated.state is State.FAILED
    assert updated.resendPending is False
    assert updated.notifyResult == "ACCEPTED"
    assert updated.version == replaced.version + 1


def test_update_notify_result_requires_terminal_state(store: ExecutionStore) -> None:
    with pytest.raises(ValueError, match="not terminal"):
        store.update_notify_result(_staging(store), "ACCEPTED")


def test_complete_cleanup_releases_slot_once(store: ExecutionStore) -> None:
    terminal = _terminal(store, SUCCEEDED_OUTCOME)
    assert store.active_runs() == 1

    store.complete_cleanup(terminal)

    cleaned = store.get(PID)
    assert cleaned is not None
    assert cleaned.pending is None
    assert cleaned.holdsSlot is False
    assert store.active_runs() == 0
    with pytest.raises(StaleItemError):
        store.complete_cleanup(terminal)
    with pytest.raises(StaleItemError):
        store.complete_cleanup(cleaned)
    assert store.active_runs() == 0


def test_complete_cleanup_without_slot(store: ExecutionStore) -> None:
    received = _received(store)
    finalizing = store.to_finalizing(received, FAILED_OUTCOME)
    terminal = store.to_notified_terminal(finalizing, "ACCEPTED")
    assert terminal.holdsSlot is False

    store.complete_cleanup(terminal)

    cleaned = store.get(PID)
    assert cleaned is not None
    assert cleaned.pending is None
    assert store.active_runs() == 0


def test_complete_cleanup_rejects_non_terminal(store: ExecutionStore) -> None:
    item = _staging(store)

    with pytest.raises(StaleItemError):
        store.complete_cleanup(item)
    assert store.active_runs() == 1


def test_complete_cleanup_raises_when_counter_is_already_zero(
    store: ExecutionStore, dynamodb_client: Any, executions_table: str
) -> None:
    terminal = _terminal(store, SUCCEEDED_OUTCOME)
    dynamodb_client.put_item(
        TableName=executions_table,
        Item={"pipelineExecutionId": {"S": CONCURRENCY_ITEM_ID}, "active": {"N": "0"}},
    )

    with pytest.raises(RuntimeError, match="counter is 0"):
        store.complete_cleanup(terminal)

    unchanged = store.get(PID)
    assert unchanged is not None
    assert unchanged.pending == PENDING_CLEANUP
    assert unchanged.holdsSlot is True


def test_query_pending_lists_active_and_cleanup_with_paging(
    store: ExecutionStore, dynamodb_client: Any, executions_table: str
) -> None:
    active_ids = [f"exec-{index:03d}" for index in range(30)]
    for pid in active_ids:
        assert store.create_received({**FIELDS, "pipelineExecutionId": pid}, TOKEN, SENT_AT)
    _terminal(store, SUCCEEDED_OUTCOME)

    paginator_calls: list[dict[str, Any]] = []
    original_paginator = dynamodb_client.get_paginator

    def small_page_paginator(operation: str) -> Any:
        paginator = original_paginator(operation)
        original_paginate = paginator.paginate

        def paginate(**kwargs: Any) -> Any:
            paginator_calls.append(kwargs)
            return original_paginate(**kwargs, PaginationConfig={"PageSize": 7})

        paginator.paginate = paginate
        return paginator

    dynamodb_client.get_paginator = small_page_paginator

    assert sorted(store.query_pending(PENDING_ACTIVE)) == active_ids
    assert store.query_pending(PENDING_CLEANUP) == [PID]
    assert paginator_calls
    assert CONCURRENCY_ITEM_ID not in store.query_pending(PENDING_ACTIVE)


class FailingUpdateClient:
    """DynamoDB client wrapper that raises once on the next write after arming."""

    def __init__(self, client: Any) -> None:
        """Wrap ``client``."""
        self._client = client
        self.fail_next_write = False

    def __getattr__(self, name: str) -> Any:
        """Delegate other calls to the wrapped client."""
        return getattr(self._client, name)

    def update_item(self, **kwargs: Any) -> Any:
        """Fail once when armed, otherwise update."""
        self._raise_if_armed("UpdateItem")
        return self._client.update_item(**kwargs)

    def transact_write_items(self, **kwargs: Any) -> Any:
        """Fail once when armed, otherwise run the transaction."""
        self._raise_if_armed("TransactWriteItems")
        return self._client.transact_write_items(**kwargs)

    def _raise_if_armed(self, operation: str) -> None:
        """Raise a service error before the write when armed."""
        if self.fail_next_write:
            self.fail_next_write = False
            raise ClientError(
                {"Error": {"Code": "InternalServerError", "Message": "injected"}}, operation
            )


def test_failed_write_leaves_state_and_counter_intact_for_retry(
    dynamodb_client: Any, executions_table: str, clock: MutableClock
) -> None:
    failing = FailingUpdateClient(dynamodb_client)
    store = _make_store(failing, executions_table, clock, OWNER)
    item = _received(store)

    failing.fail_next_write = True
    with pytest.raises(ClientError, match="injected"):
        store.start_staging(item, {"stagingPrefix": f"vams/{PID}/"}, max_concurrent=1)
    assert store.active_runs() == 0
    unchanged = store.get(PID)
    assert unchanged == item

    assert store.start_staging(item, {"stagingPrefix": f"vams/{PID}/"}, max_concurrent=1)
    staged = store.get(PID)
    assert staged is not None

    failing.fail_next_write = True
    with pytest.raises(ClientError, match="injected"):
        store.transition(staged, State.SUBMITTING)
    assert store.get(PID) == staged
    assert store.transition(staged, State.SUBMITTING).state is State.SUBMITTING
    assert store.active_runs() == 1


def test_crashed_owner_is_taken_over_after_lease_expiry(
    store: ExecutionStore, other_store: ExecutionStore, clock: MutableClock
) -> None:
    staged = _staging(store)

    clock.advance(LEASE_SECONDS + 1)
    taken = other_store.acquire_lease(PID)
    assert taken is not None
    submitting = other_store.transition(taken, State.SUBMITTING)

    with pytest.raises(StaleItemError):
        store.transition(staged, State.SUBMITTING)
    current = store.get(PID)
    assert current == submitting
    assert current.leaseOwner == OTHER_OWNER
    assert store.active_runs() == 1
