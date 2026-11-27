"""Tests for the ABORT_SUSPECTED handler."""

from collections.abc import Callable
from typing import Any

import pytest

from pask_vams_poller.abort import process_abort_suspected
from pask_vams_poller.config import PollerConfig
from pask_vams_poller.constants import PENDING_CLEANUP, State
from pask_vams_poller.context import PollerContext
from pask_vams_poller.errors import TransientError
from pask_vams_poller.sfn import CallResult
from pask_vams_poller.slurm import SqueueJob
from pask_vams_poller.store import REQUEST_IDENTITY_FIELDS, ExecutionItem, ExecutionStore

from .conftest import FIXED_EPOCH_SECONDS, TEST_REGION

OWNER = "login-node:invocation-a"
LEASE_SECONDS = 900
TTL_DAYS = 30
GRACE_SECONDS = 1800
PID = "exec_0001-a"
JOB_NAME = f"vams-{PID}"
TOKEN = "token-1"
NEW_TOKEN = "token-2"
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


class PollerStoppedError(Exception):
    """Raised by a test to stand for the poller process stopping."""


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


class FakeSfn:
    """Step Functions notifier whose heartbeat result is set per token."""

    def __init__(self) -> None:
        """Start with every token closed."""
        self.open_tokens: set[str] = set()
        self.heartbeat_error: Exception | None = None
        self.heartbeat_tokens: list[str] = []

    def heartbeat(self, token: str) -> CallResult:
        """Record the call and return ACCEPTED for open tokens, CLOSED otherwise."""
        self.heartbeat_tokens.append(token)
        if self.heartbeat_error is not None:
            raise self.heartbeat_error
        return CallResult.ACCEPTED if token in self.open_tokens else CallResult.CLOSED


class FakeSlurm:
    """Slurm client with an in-memory queue."""

    def __init__(self) -> None:
        """Start with an empty queue that scancel empties."""
        self.queue: dict[str, list[SqueueJob]] = {}
        self.cancel_removes_jobs = True
        self.cancelled_names: list[str] = []
        self.found_names: list[str] = []

    def cancel_by_name(self, job_name: str) -> None:
        """Record the call and drop the jobs when ``cancel_removes_jobs`` is set."""
        self.cancelled_names.append(job_name)
        if self.cancel_removes_jobs:
            self.queue.pop(job_name, None)

    def find_by_name(self, job_name: str) -> list[SqueueJob]:
        """Record the call and return the queued jobs with the name."""
        self.found_names.append(job_name)
        return list(self.queue.get(job_name, []))


@pytest.fixture
def clock() -> MutableClock:
    """Return a clock that starts at the shared fixed epoch seconds."""
    return MutableClock(FIXED_EPOCH_SECONDS)


@pytest.fixture
def store(dynamodb_client: Any, executions_table: str, clock: MutableClock) -> ExecutionStore:
    """Return a store owned by ``OWNER`` on the moto table."""
    return ExecutionStore(
        executions_table,
        region=TEST_REGION,
        owner_id=OWNER,
        lease_seconds=LEASE_SECONDS,
        ttl_days=TTL_DAYS,
        clock=clock,
        dynamodb_client=dynamodb_client,
    )


@pytest.fixture
def sfn() -> FakeSfn:
    """Return the fake Step Functions notifier."""
    return FakeSfn()


@pytest.fixture
def slurm() -> FakeSlurm:
    """Return the fake Slurm client."""
    return FakeSlurm()


@pytest.fixture
def ctx(
    poller_config: PollerConfig,
    store: ExecutionStore,
    sfn: FakeSfn,
    slurm: FakeSlurm,
    clock: MutableClock,
    training_env: dict[str, str],
) -> PollerContext:
    """Return a poller context with the moto store and the fakes."""
    assert poller_config.abort_grace_seconds == GRACE_SECONDS
    return PollerContext(
        config=poller_config,
        store=store,
        sfn=sfn,
        slurm=slurm,
        s3=None,
        sqs=None,
        training_env=training_env,
        owner_id=OWNER,
        clock=clock,
    )


def _received(store: ExecutionStore) -> ExecutionItem:
    """Create a RECEIVED execution and take its lease."""
    assert store.create_received(FIELDS, TOKEN, SENT_AT)
    leased = store.acquire_lease(PID)
    assert leased is not None
    return leased


def _running(store: ExecutionStore) -> ExecutionItem:
    """Move an execution through STAGING, SUBMITTING, and SUBMITTED to RUNNING."""
    item = _received(store)
    assert store.start_staging(item, {"stagingPrefix": f"vams/{PID}/"}, max_concurrent=1)
    staged = store.get(PID)
    assert staged is not None
    item = store.transition(staged, State.SUBMITTING, set_attrs={"jobName": JOB_NAME})
    item = store.transition(item, State.SUBMITTED, set_attrs={"slurmJobId": "42"})
    return store.transition(item, State.RUNNING)


def _suspect(store: ExecutionStore, make_item: Callable[[ExecutionStore], ExecutionItem]) -> None:
    """Create an execution with ``make_item`` and move it to ABORT_SUSPECTED with TOKEN."""
    suspected = store.to_abort_suspected(make_item(store), TOKEN)
    assert suspected.state is State.ABORT_SUSPECTED


def _leased_item(store: ExecutionStore) -> ExecutionItem:
    """Take the lease again at the current clock, as the reconciler does, and return the item."""
    item = store.acquire_lease(PID)
    assert item is not None
    return item


def _stored(store: ExecutionStore) -> ExecutionItem:
    """Read the stored execution."""
    item = store.get(PID)
    assert item is not None
    return item


@pytest.mark.parametrize(
    ("make_item", "previous_state"),
    [(_received, State.RECEIVED), (_running, State.RUNNING)],
)
def test_restores_previous_state_when_new_token_arrives_within_grace(
    ctx: PollerContext,
    store: ExecutionStore,
    sfn: FakeSfn,
    slurm: FakeSlurm,
    clock: MutableClock,
    make_item: Callable[[ExecutionStore], ExecutionItem],
    previous_state: State,
) -> None:
    _suspect(store, make_item)
    clock.advance(60)
    assert store.replace_token(PID, NEW_TOKEN, SENT_AT + 60_000, IDENTITY) is not None
    sfn.open_tokens.add(NEW_TOKEN)

    process_abort_suspected(_leased_item(store), ctx)

    restored = _stored(store)
    assert restored.state is previous_state
    assert restored.prevState is None
    assert restored.suspectedAt is None
    assert restored.taskToken == NEW_TOKEN
    assert sfn.heartbeat_tokens == [NEW_TOKEN]
    assert slurm.cancelled_names == []


def test_restores_when_token_is_replaced_during_the_heartbeat(
    ctx: PollerContext, store: ExecutionStore, sfn: FakeSfn, clock: MutableClock
) -> None:
    _suspect(store, _running)
    stale_item = _leased_item(store)
    assert store.replace_token(PID, NEW_TOKEN, SENT_AT + 60_000, IDENTITY) is not None
    sfn.open_tokens.add(NEW_TOKEN)

    process_abort_suspected(stale_item, ctx)

    assert _stored(store).state is State.RUNNING
    assert sfn.heartbeat_tokens == [TOKEN, NEW_TOKEN]


def test_does_nothing_within_grace_without_new_token(
    ctx: PollerContext, store: ExecutionStore, sfn: FakeSfn, slurm: FakeSlurm, clock: MutableClock
) -> None:
    _suspect(store, _running)
    slurm.queue[JOB_NAME] = [SqueueJob(job_id="42", state="RUNNING")]
    clock.advance(GRACE_SECONDS)
    item = _leased_item(store)

    process_abort_suspected(item, ctx)

    stored = _stored(store)
    assert stored.state is State.ABORT_SUSPECTED
    assert stored.version == item.version
    assert sfn.heartbeat_tokens == [TOKEN]
    assert slurm.cancelled_names == []


def test_aborts_after_grace_once_job_is_gone(
    ctx: PollerContext, store: ExecutionStore, slurm: FakeSlurm, clock: MutableClock
) -> None:
    _suspect(store, _running)
    slurm.queue[JOB_NAME] = [SqueueJob(job_id="42", state="RUNNING")]
    clock.advance(GRACE_SECONDS + 1)

    process_abort_suspected(_leased_item(store), ctx)

    aborted = _stored(store)
    assert aborted.state is State.ABORTED
    assert aborted.pending == PENDING_CLEANUP
    assert aborted.terminalAt is not None
    assert aborted.holdsSlot is True
    assert slurm.cancelled_names == [JOB_NAME]
    assert slurm.found_names == [JOB_NAME]


def test_keeps_suspected_while_job_is_still_listed(
    ctx: PollerContext, store: ExecutionStore, slurm: FakeSlurm, clock: MutableClock
) -> None:
    _suspect(store, _running)
    slurm.queue[JOB_NAME] = [SqueueJob(job_id="42", state="COMPLETING")]
    slurm.cancel_removes_jobs = False
    clock.advance(GRACE_SECONDS + 1)

    process_abort_suspected(_leased_item(store), ctx)

    assert _stored(store).state is State.ABORT_SUSPECTED
    assert slurm.cancelled_names == [JOB_NAME]

    slurm.queue.pop(JOB_NAME)
    clock.advance(30)
    process_abort_suspected(_leased_item(store), ctx)

    assert _stored(store).state is State.ABORTED
    assert slurm.cancelled_names == [JOB_NAME, JOB_NAME]


def test_retries_scancel_after_stop_before_writing_aborted(
    ctx: PollerContext,
    store: ExecutionStore,
    slurm: FakeSlurm,
    clock: MutableClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _suspect(store, _running)
    slurm.queue[JOB_NAME] = [SqueueJob(job_id="42", state="RUNNING")]
    clock.advance(GRACE_SECONDS + 1)

    def stop_before_aborted(*args: Any, **kwargs: Any) -> ExecutionItem:
        """Simulate the poller stopping after scancel and before ABORTED is written."""
        raise PollerStoppedError("poller stopped")

    with monkeypatch.context() as patch:
        patch.setattr(store, "to_aborted", stop_before_aborted)
        with pytest.raises(PollerStoppedError):
            process_abort_suspected(_leased_item(store), ctx)

    assert _stored(store).state is State.ABORT_SUSPECTED
    assert slurm.cancelled_names == [JOB_NAME]

    clock.advance(LEASE_SECONDS + 1)
    process_abort_suspected(_leased_item(store), ctx)

    assert _stored(store).state is State.ABORTED
    assert slurm.cancelled_names == [JOB_NAME, JOB_NAME]


def test_aborts_without_scancel_when_no_job_name(
    ctx: PollerContext, store: ExecutionStore, slurm: FakeSlurm, clock: MutableClock
) -> None:
    _suspect(store, _received)
    clock.advance(GRACE_SECONDS + 1)

    process_abort_suspected(_leased_item(store), ctx)

    aborted = _stored(store)
    assert aborted.state is State.ABORTED
    assert aborted.pending == PENDING_CLEANUP
    assert slurm.cancelled_names == []
    assert slurm.found_names == []


def test_aborts_when_replaced_token_is_also_closed(
    ctx: PollerContext, store: ExecutionStore, sfn: FakeSfn, clock: MutableClock
) -> None:
    _suspect(store, _running)
    clock.advance(GRACE_SECONDS + 1)
    stale_item = _leased_item(store)
    assert store.replace_token(PID, NEW_TOKEN, SENT_AT + 60_000, IDENTITY) is not None

    process_abort_suspected(stale_item, ctx)

    aborted = _stored(store)
    assert aborted.state is State.ABORTED
    assert aborted.taskToken == NEW_TOKEN
    assert sfn.heartbeat_tokens == [TOKEN, NEW_TOKEN]


def test_transient_heartbeat_error_leaves_state_unchanged(
    ctx: PollerContext, store: ExecutionStore, sfn: FakeSfn, slurm: FakeSlurm, clock: MutableClock
) -> None:
    _suspect(store, _running)
    clock.advance(GRACE_SECONDS + 1)
    item = _leased_item(store)
    sfn.heartbeat_error = TransientError("throttled")

    with pytest.raises(TransientError):
        process_abort_suspected(item, ctx)

    stored = _stored(store)
    assert stored.state is State.ABORT_SUSPECTED
    assert stored.version == item.version
    assert slurm.cancelled_names == []


def test_rejects_execution_in_other_state(ctx: PollerContext, store: ExecutionStore) -> None:
    with pytest.raises(ValueError, match="ABORT_SUSPECTED"):
        process_abort_suspected(_running(store), ctx)
