"""Tests for the reconciler: dispatch by state, leases, error isolation, and heartbeat pacing."""

import dataclasses
import json
import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from pask_vams_poller import abort, finalize, reconciler, staging, submit
from pask_vams_poller.config import PollerConfig
from pask_vams_poller.constants import PENDING_ACTIVE, PENDING_CLEANUP, State
from pask_vams_poller.context import PollerContext
from pask_vams_poller.errors import StaleItemError, TransientError
from pask_vams_poller.intake import IntakeLoop
from pask_vams_poller.reconciler import HeartbeatThrottle, Reconciler
from pask_vams_poller.sfn import CallResult
from pask_vams_poller.slurm import SlurmClient
from pask_vams_poller.store import ExecutionItem, ExecutionStore

from .conftest import FIXED_EPOCH_SECONDS, TEST_REGION
from .test_staging import (
    ASSET_BUCKET,
    DEFAULT_TRAINING_CONFIG,
    WORKFLOW_BUCKET,
    WORKFLOW_INPUTS_PREFIX,
    FakeSfn,
    create_bucket,
    put_asset_files,
    put_request_files,
)

OWNER = "login-node:invocation-a"
OTHER_OWNER = "login-node:invocation-b"
TOKEN = "token-1"
SENT_AT = 1_789_999_000_000
UPDATED_AT = "2026-10-16T00:00:00Z"

HANDLER_ATTRIBUTES: dict[State, tuple[Any, str]] = {
    State.RECEIVED: (staging, "process_received"),
    State.STAGING: (staging, "process_staging"),
    State.SUBMITTING: (submit, "process_submitting"),
    State.SUBMITTED: (finalize, "process_submitted"),
    State.RUNNING: (finalize, "process_running"),
    State.FINALIZING: (finalize, "process_finalizing"),
    State.ABORT_SUSPECTED: (abort, "process_abort_suspected"),
}
CLEANUP_HANDLER = "process_cleanup"


class HandlerRecorder:
    """Replacement of the state handlers that records which one was called."""

    def __init__(self) -> None:
        """Create an empty recorder."""
        self.calls: list[tuple[str, str, PollerContext]] = []
        self.errors: dict[str, Exception] = {}

    def handler(self, name: str) -> Callable[[ExecutionItem, PollerContext], None]:
        """Return a fake handler that records its calls under ``name``."""

        def fake_handler(item: ExecutionItem, ctx: PollerContext) -> None:
            self.calls.append((name, item.pipelineExecutionId, ctx))
            error = self.errors.get(item.pipelineExecutionId)
            if error is not None:
                raise error

        return fake_handler

    def called(self) -> list[tuple[str, str]]:
        """Return the handler name and execution id of every call."""
        return [(name, pid) for name, pid, _ in self.calls]


class CountingNotifier:
    """Step Functions notifier that returns queued heartbeat results and counts calls."""

    def __init__(self, results: list[CallResult]) -> None:
        """Create the fake with the results of the next heartbeats."""
        self._results = list(results)
        self.heartbeats: list[str] = []
        self.successes: list[tuple[str, dict[str, Any]]] = []
        self.failures: list[tuple[str, str, str]] = []

    def heartbeat(self, token: str) -> CallResult:
        """Record the heartbeat and return the next result."""
        self.heartbeats.append(token)
        return self._results.pop(0)

    def send_success(self, token: str, output: dict[str, Any]) -> CallResult:
        """Record the success."""
        self.successes.append((token, output))
        return CallResult.ACCEPTED

    def send_failure(self, token: str, error: str, cause: str) -> CallResult:
        """Record the failure."""
        self.failures.append((token, error, cause))
        return CallResult.ACCEPTED


class ManualClock:
    """Clock that only moves when the test advances it."""

    def __init__(self) -> None:
        """Start at zero."""
        self.now = 0.0

    def __call__(self) -> float:
        """Return the current time."""
        return self.now


@pytest.fixture
def store(dynamodb_client: Any, executions_table: str, fixed_clock: Any) -> ExecutionStore:
    """Return a store owned by ``OWNER`` on the moto table."""
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
def recorder(monkeypatch: pytest.MonkeyPatch) -> HandlerRecorder:
    """Replace every state handler with a recording fake."""
    handler_recorder = HandlerRecorder()
    for module, attribute in HANDLER_ATTRIBUTES.values():
        monkeypatch.setattr(module, attribute, handler_recorder.handler(attribute))
    monkeypatch.setattr(finalize, CLEANUP_HANDLER, handler_recorder.handler(CLEANUP_HANDLER))
    return handler_recorder


@pytest.fixture
def ctx(poller_config: PollerConfig, store: ExecutionStore, fixed_clock: Any) -> PollerContext:
    """Return a context whose notifier is a counting fake."""
    return PollerContext(
        config=poller_config,
        store=store,
        sfn=CountingNotifier([]),
        slurm=None,
        s3=None,
        sqs=None,
        training_env={},
        owner_id=OWNER,
        clock=fixed_clock,
    )


def put_execution(
    dynamodb_client: Any,
    table_name: str,
    pid: str,
    state: State,
    pending: str,
    **attributes: Any,
) -> None:
    """Write an execution item directly, so that any state can be set up."""
    item = ExecutionItem(
        pipelineExecutionId=pid,
        state=state,
        version=1,
        taskToken=TOKEN,
        tokenSentAt=SENT_AT,
        createdAt=UPDATED_AT,
        updatedAt=UPDATED_AT,
        pending=pending,
        **attributes,
    )
    dynamodb_client.put_item(TableName=table_name, Item=item.to_item())


@pytest.mark.parametrize("state", list(HANDLER_ATTRIBUTES))
def test_run_once_calls_the_handler_of_the_state(
    state: State,
    recorder: HandlerRecorder,
    ctx: PollerContext,
    dynamodb_client: Any,
    executions_table: str,
    store: ExecutionStore,
) -> None:
    put_execution(dynamodb_client, executions_table, "exec-1", state, PENDING_ACTIVE)

    Reconciler(ctx).run_once()

    assert recorder.called() == [(HANDLER_ATTRIBUTES[state][1], "exec-1")]
    stored = store.get("exec-1")
    assert stored.leaseOwner is None
    assert stored.leaseExpiresAt is None


@pytest.mark.parametrize("state", [State.SUCCEEDED, State.FAILED, State.ABORTED])
def test_run_once_cleans_up_terminal_executions(
    state: State,
    recorder: HandlerRecorder,
    ctx: PollerContext,
    dynamodb_client: Any,
    executions_table: str,
) -> None:
    put_execution(dynamodb_client, executions_table, "exec-1", state, PENDING_CLEANUP)

    Reconciler(ctx).run_once()

    assert recorder.called() == [(CLEANUP_HANDLER, "exec-1")]


def test_handler_receives_the_leased_item_read_with_strong_consistency(
    monkeypatch: pytest.MonkeyPatch,
    ctx: PollerContext,
    dynamodb_client: Any,
    executions_table: str,
) -> None:
    put_execution(dynamodb_client, executions_table, "exec-1", State.RUNNING, PENDING_ACTIVE)
    received: list[ExecutionItem] = []
    monkeypatch.setattr(
        finalize, "process_running", lambda item, handler_ctx: received.append(item)
    )

    Reconciler(ctx).run_once()

    assert len(received) == 1
    assert received[0].leaseOwner == OWNER
    assert received[0].leaseExpiresAt == int(FIXED_EPOCH_SECONDS) + 900


def test_run_once_skips_an_execution_leased_by_another_poller(
    recorder: HandlerRecorder,
    ctx: PollerContext,
    dynamodb_client: Any,
    executions_table: str,
    store: ExecutionStore,
) -> None:
    lease_expires_at = int(FIXED_EPOCH_SECONDS) + 100
    put_execution(
        dynamodb_client,
        executions_table,
        "exec-leased",
        State.RUNNING,
        PENDING_ACTIVE,
        leaseOwner=OTHER_OWNER,
        leaseExpiresAt=lease_expires_at,
    )
    put_execution(dynamodb_client, executions_table, "exec-free", State.RUNNING, PENDING_ACTIVE)

    Reconciler(ctx).run_once()

    assert recorder.called() == [("process_running", "exec-free")]
    leased = store.get("exec-leased")
    assert leased.leaseOwner == OTHER_OWNER
    assert leased.leaseExpiresAt == lease_expires_at


def test_run_once_takes_over_an_expired_lease(
    recorder: HandlerRecorder,
    ctx: PollerContext,
    dynamodb_client: Any,
    executions_table: str,
) -> None:
    put_execution(
        dynamodb_client,
        executions_table,
        "exec-1",
        State.STAGING,
        PENDING_ACTIVE,
        leaseOwner=OTHER_OWNER,
        leaseExpiresAt=int(FIXED_EPOCH_SECONDS) - 1,
    )

    Reconciler(ctx).run_once()

    assert recorder.called() == [("process_staging", "exec-1")]


@pytest.mark.parametrize(
    ("error", "level", "has_traceback"),
    [
        (RuntimeError("boom"), logging.ERROR, True),
        (StaleItemError("changed"), logging.WARNING, False),
        (TransientError("throttled"), logging.WARNING, False),
    ],
)
def test_one_failing_execution_does_not_stop_the_others(
    error: Exception,
    level: int,
    has_traceback: bool,
    recorder: HandlerRecorder,
    ctx: PollerContext,
    dynamodb_client: Any,
    executions_table: str,
    store: ExecutionStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    put_execution(dynamodb_client, executions_table, "exec-1", State.RUNNING, PENDING_ACTIVE)
    put_execution(dynamodb_client, executions_table, "exec-2", State.RUNNING, PENDING_ACTIVE)
    recorder.errors["exec-1"] = error

    with caplog.at_level(logging.WARNING, logger=reconciler.__name__):
        Reconciler(ctx).run_once()

    assert sorted(recorder.called()) == [
        ("process_running", "exec-1"),
        ("process_running", "exec-2"),
    ]
    assert store.get("exec-1").leaseOwner is None
    assert store.get("exec-2").leaseOwner is None
    records = [record for record in caplog.records if "exec-1" in record.getMessage()]
    assert [record.levelno for record in records] == [level]
    assert (records[0].exc_info is not None) is has_traceback


def test_abort_suspected_uses_the_real_notifier_and_others_the_throttle(
    recorder: HandlerRecorder,
    ctx: PollerContext,
    dynamodb_client: Any,
    executions_table: str,
) -> None:
    put_execution(
        dynamodb_client,
        executions_table,
        "exec-abort",
        State.ABORT_SUSPECTED,
        PENDING_ACTIVE,
        prevState=State.RUNNING,
        suspectedAt=int(FIXED_EPOCH_SECONDS),
    )
    put_execution(dynamodb_client, executions_table, "exec-run", State.RUNNING, PENDING_ACTIVE)

    Reconciler(ctx).run_once()

    notifiers = {pid: handler_ctx.sfn for _, pid, handler_ctx in recorder.calls}
    assert notifiers["exec-abort"] is ctx.sfn
    assert isinstance(notifiers["exec-run"], HeartbeatThrottle)


def test_run_once_stops_before_the_next_execution_when_stop_is_set(
    recorder: HandlerRecorder,
    ctx: PollerContext,
    dynamodb_client: Any,
    executions_table: str,
) -> None:
    put_execution(dynamodb_client, executions_table, "exec-1", State.RUNNING, PENDING_ACTIVE)
    stop_event = threading.Event()
    stop_event.set()

    Reconciler(ctx).run_once(stop_event)

    assert recorder.called() == []


def test_run_returns_after_stop_event(
    recorder: HandlerRecorder,
    ctx: PollerContext,
    dynamodb_client: Any,
    executions_table: str,
) -> None:
    put_execution(dynamodb_client, executions_table, "exec-1", State.RUNNING, PENDING_ACTIVE)
    stop_event = threading.Event()
    recorder_handler = recorder.handler("process_running")

    def handler_that_requests_stop(item: ExecutionItem, handler_ctx: PollerContext) -> None:
        recorder_handler(item, handler_ctx)
        stop_event.set()

    rec = Reconciler(ctx)
    rec._handlers[State.RUNNING] = handler_that_requests_stop
    thread = threading.Thread(target=rec.run, args=(stop_event,))
    thread.start()
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert recorder.called() == [("process_running", "exec-1")]


def test_throttle_skips_heartbeats_within_the_interval() -> None:
    notifier = CountingNotifier([CallResult.ACCEPTED, CallResult.ACCEPTED])
    clock = ManualClock()
    throttle = HeartbeatThrottle(notifier, 270, clock)

    assert throttle.heartbeat(TOKEN) is CallResult.ACCEPTED
    clock.now = 269
    assert throttle.heartbeat(TOKEN) is CallResult.ACCEPTED
    assert notifier.heartbeats == [TOKEN]

    clock.now = 270
    assert throttle.heartbeat(TOKEN) is CallResult.ACCEPTED
    assert notifier.heartbeats == [TOKEN, TOKEN]


def test_throttle_sends_a_new_token_at_once() -> None:
    notifier = CountingNotifier([CallResult.ACCEPTED, CallResult.ACCEPTED])
    throttle = HeartbeatThrottle(notifier, 270, ManualClock())

    throttle.heartbeat(TOKEN)
    throttle.heartbeat("token-2")

    assert notifier.heartbeats == [TOKEN, "token-2"]


def test_throttle_does_not_remember_a_closed_token() -> None:
    notifier = CountingNotifier([CallResult.CLOSED, CallResult.CLOSED])
    throttle = HeartbeatThrottle(notifier, 270, ManualClock())

    assert throttle.heartbeat(TOKEN) is CallResult.CLOSED
    assert throttle.heartbeat(TOKEN) is CallResult.CLOSED
    assert notifier.heartbeats == [TOKEN, TOKEN]


def test_throttle_does_not_remember_a_failed_heartbeat() -> None:
    notifier = CountingNotifier([CallResult.ACCEPTED])
    throttle = HeartbeatThrottle(notifier, 270, ManualClock())

    def failing_heartbeat(token: str) -> CallResult:
        raise TransientError("throttled")

    real_heartbeat = notifier.heartbeat
    notifier.heartbeat = failing_heartbeat
    with pytest.raises(TransientError):
        throttle.heartbeat(TOKEN)
    notifier.heartbeat = real_heartbeat

    assert throttle.heartbeat(TOKEN) is CallResult.ACCEPTED
    assert notifier.heartbeats == [TOKEN]


def test_throttle_without_room_sends_every_heartbeat() -> None:
    notifier = CountingNotifier([CallResult.ACCEPTED, CallResult.ACCEPTED])
    throttle = HeartbeatThrottle(notifier, 0, ManualClock())

    throttle.heartbeat(TOKEN)
    throttle.heartbeat(TOKEN)

    assert notifier.heartbeats == [TOKEN, TOKEN]


def test_throttle_passes_success_and_failure_through() -> None:
    notifier = CountingNotifier([])
    throttle = HeartbeatThrottle(notifier, 270, ManualClock())

    throttle.send_success(TOKEN, {"status": "SUCCEEDED"})
    throttle.send_failure(TOKEN, "PASK.JobFailed", "exit 1")

    assert notifier.successes == [(TOKEN, {"status": "SUCCEEDED"})]
    assert notifier.failures == [(TOKEN, "PASK.JobFailed", "exit 1")]


def test_reconciler_heartbeats_once_per_interval_for_a_waiting_execution(
    monkeypatch: pytest.MonkeyPatch,
    ctx: PollerContext,
    dynamodb_client: Any,
    executions_table: str,
) -> None:
    put_execution(dynamodb_client, executions_table, "exec-1", State.RUNNING, PENDING_ACTIVE)
    notifier = CountingNotifier([CallResult.ACCEPTED, CallResult.ACCEPTED])
    clock = ManualClock()
    monkeypatch.setattr(reconciler, "monotonic", clock)
    monkeypatch.setattr(
        finalize,
        "process_running",
        lambda item, handler_ctx: handler_ctx.sfn.heartbeat(item.taskToken),
    )
    rec = Reconciler(dataclasses.replace(ctx, sfn=notifier))

    interval = ctx.config.reconcile_interval_seconds
    cycles_within_interval = ctx.config.heartbeat_interval_seconds // interval - 1
    for cycle in range(cycles_within_interval):
        clock.now = cycle * interval
        rec.run_once()

    assert len(notifier.heartbeats) == 1
    clock.now = cycles_within_interval * interval
    rec.run_once()
    assert len(notifier.heartbeats) == 2


def test_intake_and_reconciler_move_a_request_to_staging(
    poller_config: PollerConfig,
    store: ExecutionStore,
    s3_client: Any,
    sqs_client: Any,
    queue_url: str,
    data_bucket: str,
    fixed_clock: Any,
    tmp_path: Path,
) -> None:
    pid = "exec_0001-a"
    create_bucket(s3_client, WORKFLOW_BUCKET, versioned=False)
    create_bucket(s3_client, ASSET_BUCKET, versioned=True)
    put_asset_files(s3_client)
    put_request_files(s3_client, pid, DEFAULT_TRAINING_CONFIG)
    body = {
        "TaskToken": TOKEN,
        "workflowDatabaseId": "db1",
        "workflowId": "wf1",
        "workflowExecutionId": "wfx1",
        "workflowExecutionS3InputOutputBucket": WORKFLOW_BUCKET,
        "executingUserName": "alice",
        "inputManifestS3Location": f"s3://{WORKFLOW_BUCKET}/{WORKFLOW_INPUTS_PREFIX}{pid}/manifest.json",
        "inputConfigurationS3Location": f"s3://{WORKFLOW_BUCKET}/{WORKFLOW_INPUTS_PREFIX}{pid}/config.json",
    }
    sqs_client.send_message(QueueUrl=queue_url, MessageBody=json.dumps({"body": body}))
    base_dir = tmp_path / "lerobot-openpi"
    sfn = FakeSfn(None)
    ctx = PollerContext(
        config=dataclasses.replace(poller_config, queue_url=queue_url),
        store=store,
        sfn=sfn,
        slurm=SlurmClient(runner=None),
        s3=s3_client,
        sqs=sqs_client,
        training_env={
            "LEROBOT_PI_BASE_DIR": str(base_dir),
            "LEROBOT_PI_OUTPUT_DIR": str(base_dir / "outputs"),
        },
        owner_id=OWNER,
        clock=fixed_clock,
    )

    assert IntakeLoop(ctx).poll_once() == 1
    received = store.get(pid)
    assert received.state is State.RECEIVED
    assert received.executingUserName == "alice"

    Reconciler(ctx).run_once()

    stored = store.get(pid)
    assert stored.state is State.STAGING
    assert stored.holdsSlot is True
    assert stored.stagingPrefix == f"vams/{pid}/"
    assert stored.runDir == f"{base_dir}/vams-runs/{pid}"
    assert stored.leaseOwner is None
    assert sfn.tokens == [TOKEN]
    assert store.active_runs() == 1
    remaining = sqs_client.receive_message(QueueUrl=queue_url).get("Messages", [])
    assert remaining == []
