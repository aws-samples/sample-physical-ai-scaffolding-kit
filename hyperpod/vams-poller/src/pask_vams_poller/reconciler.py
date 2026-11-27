"""Reconciler: lease each pending execution and run the handler of its current state.

Every ``reconcile_interval_seconds`` the reconciler lists the executions with
``pending=ACTIVE`` and ``pending=CLEANUP`` from the sparse ``pending-index``, takes the
lease of each one (which also reads the item with strong consistency), runs exactly one
handler chosen by the state, and releases the lease. An execution whose lease is held by
another poller is skipped. A failure of one execution is logged and does not stop the
others; the same handler runs again in the next cycle.

Every handler sends a heartbeat before it works. Without throttling, executions that wait
(RECEIVED without a free slot, SUBMITTED, RUNNING) would send one heartbeat per cycle.
``HeartbeatThrottle`` remembers when each token was last accepted and skips heartbeats
that come sooner than needed, so that heartbeats go out about every
``heartbeat_interval_seconds``. ABORT_SUSPECTED always uses the real notifier, because an
accepted heartbeat there means that the token was replaced and restores the execution.
"""

import dataclasses
import logging
import threading
from collections.abc import Callable
from time import monotonic
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from pask_vams_poller import abort, finalize, staging, submit
from pask_vams_poller.constants import (
    PENDING_ACTIVE,
    PENDING_CLEANUP,
    TERMINAL_STATES,
    State,
)
from pask_vams_poller.context import PollerContext
from pask_vams_poller.errors import StaleItemError, TransientError
from pask_vams_poller.sfn import CallResult
from pask_vams_poller.store import ExecutionItem

logger = logging.getLogger(__name__)

StateHandler = Callable[[ExecutionItem, PollerContext], None]

EXPECTED_HANDLER_ERRORS: tuple[type[Exception], ...] = (StaleItemError, TransientError)
LISTING_ERRORS: tuple[type[Exception], ...] = (ClientError, BotoCoreError)


class HeartbeatThrottle:
    """Step Functions notifier that skips heartbeats sent soon after an accepted one.

    ``send_success`` and ``send_failure`` are passed through unchanged. Only accepted
    heartbeats are remembered, so a token whose heartbeat failed or was CLOSED is sent
    again on the next call.
    """

    def __init__(
        self, notifier: Any, min_interval_seconds: float, clock: Callable[[], float]
    ) -> None:
        """Create the throttle.

        Args:
            notifier: The real ``SfnNotifier``.
            min_interval_seconds: A heartbeat for a token accepted less than this many
                seconds ago is skipped. Zero or less disables skipping.
            clock: Monotonic clock in seconds.
        """
        self._notifier = notifier
        self._min_interval_seconds = min_interval_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._accepted_at: dict[str, float] = {}

    def heartbeat(self, token: str) -> CallResult:
        """Send a heartbeat unless the same token was accepted recently.

        Args:
            token: Task token.

        Returns:
            ACCEPTED for a skipped or accepted heartbeat, or CLOSED when the token is closed.

        Raises:
            TransientError: Raised by the real notifier.
            ClientError: Raised by the real notifier.
        """
        now = self._clock()
        with self._lock:
            self._forget_older_than(now)
            if token in self._accepted_at:
                return CallResult.ACCEPTED
        result = self._notifier.heartbeat(token)
        with self._lock:
            if result is CallResult.ACCEPTED:
                self._accepted_at[token] = now
            else:
                self._accepted_at.pop(token, None)
        return result

    def send_success(self, token: str, output: dict[str, Any]) -> CallResult:
        """Report success through the real notifier.

        Args:
            token: Task token.
            output: JSON serializable task output.

        Returns:
            The result of the real notifier.
        """
        return self._notifier.send_success(token, output)

    def send_failure(self, token: str, error: str, cause: str) -> CallResult:
        """Report failure through the real notifier.

        Args:
            token: Task token.
            error: Error name.
            cause: Failure cause.

        Returns:
            The result of the real notifier.
        """
        return self._notifier.send_failure(token, error, cause)

    def _forget_older_than(self, now: float) -> None:
        """Drop the tokens whose last accepted heartbeat is old enough to send again.

        The caller holds ``self._lock``.

        Args:
            now: Current monotonic time in seconds.
        """
        expired_tokens = [
            token
            for token, accepted_at in self._accepted_at.items()
            if now - accepted_at >= self._min_interval_seconds
        ]
        for token in expired_tokens:
            del self._accepted_at[token]


def _state_handlers() -> dict[State, StateHandler]:
    """Return the handler of each non terminal state.

    The handlers are read from their modules when the reconciler is created, so that
    tests can replace them.

    Returns:
        Mapping from state to ``process_<state>`` function.
    """
    return {
        State.RECEIVED: staging.process_received,
        State.STAGING: staging.process_staging,
        State.SUBMITTING: submit.process_submitting,
        State.SUBMITTED: finalize.process_submitted,
        State.RUNNING: finalize.process_running,
        State.FINALIZING: finalize.process_finalizing,
        State.ABORT_SUSPECTED: abort.process_abort_suspected,
    }


def _ordered_unique(pids: list[str]) -> list[str]:
    """Remove repeated ids while keeping the first occurrence of each.

    Args:
        pids: Pipeline execution ids, possibly repeated.

    Returns:
        The ids in their first order without repeats.
    """
    return list(dict.fromkeys(pids))


class Reconciler:
    """Run the state handlers of every pending execution, one handler per execution."""

    def __init__(self, ctx: PollerContext) -> None:
        """Create the reconciler.

        Args:
            ctx: Poller context. ``config.reconcile_interval_seconds`` and
                ``config.heartbeat_interval_seconds`` set the cycle and the heartbeat
                interval.
        """
        self._ctx = ctx
        throttle = HeartbeatThrottle(
            ctx.sfn,
            ctx.config.heartbeat_interval_seconds - ctx.config.reconcile_interval_seconds,
            monotonic,
        )
        self._throttled_ctx = dataclasses.replace(ctx, sfn=throttle)
        self._handlers = _state_handlers()
        self._cleanup_handler: StateHandler = finalize.process_cleanup

    def run(self, stop_event: threading.Event) -> None:
        """Call ``run_once`` every ``reconcile_interval_seconds`` until ``stop_event`` is set.

        A failure to list the pending executions is logged and retried in the next cycle.

        Args:
            stop_event: Set by the caller to stop after the current execution.
        """
        interval_seconds = self._ctx.config.reconcile_interval_seconds
        while not stop_event.is_set():
            try:
                self.run_once(stop_event)
            except LISTING_ERRORS:
                logger.exception("Listing pending executions failed; retrying next cycle")
            stop_event.wait(interval_seconds)

    def run_once(self, stop_event: threading.Event | None = None) -> None:
        """Handle every pending execution once.

        Args:
            stop_event: When given and set, the cycle stops before the next execution.

        Raises:
            ClientError: Querying ``pending-index`` failed.
            BotoCoreError: DynamoDB could not be reached while querying.
        """
        store = self._ctx.store
        pids = _ordered_unique(
            store.query_pending(PENDING_ACTIVE) + store.query_pending(PENDING_CLEANUP)
        )
        for pid in pids:
            if stop_event is not None and stop_event.is_set():
                logger.info("Stop requested; leaving the reconcile cycle")
                return
            self._reconcile_safely(pid)

    def _reconcile_safely(self, pid: str) -> None:
        """Handle one execution and log its failure without stopping the cycle.

        Stale items and transient errors are expected and logged as warnings. Any other
        exception is logged with its traceback. In both cases the execution keeps its
        state and is handled again in the next cycle.

        Args:
            pid: The pipelineExecutionId.
        """
        try:
            self._reconcile(pid)
        except EXPECTED_HANDLER_ERRORS as error:
            logger.warning("Handling %s did not finish: %s: %s", pid, type(error).__name__, error)
        except Exception:
            logger.exception("Handling %s failed", pid)

    def _reconcile(self, pid: str) -> None:
        """Lease one execution, run the handler of its state, and release the lease.

        Args:
            pid: The pipelineExecutionId.
        """
        item = self._ctx.store.acquire_lease(pid)
        if item is None:
            logger.debug("Skipping %s; another poller holds its lease", pid)
            return
        try:
            self._dispatch(item)
        finally:
            if not self._ctx.store.release_lease(item):
                logger.warning("Lease of %s was taken over before it was released", pid)

    def _dispatch(self, item: ExecutionItem) -> None:
        """Call the one handler that matches the state of a leased execution.

        Args:
            item: Execution as read when the lease was taken.
        """
        pid = item.pipelineExecutionId
        if item.state in TERMINAL_STATES:
            if item.pending != PENDING_CLEANUP:
                logger.debug("Skipping %s; it is %s with nothing pending", pid, item.state.value)
                return
            logger.debug("Cleaning up %s (%s)", pid, item.state.value)
            self._cleanup_handler(item, self._throttled_ctx)
            return
        handler = self._handlers[item.state]
        handler_ctx = self._ctx if item.state is State.ABORT_SUSPECTED else self._throttled_ctx
        logger.debug("Handling %s in %s", pid, item.state.value)
        handler(item, handler_ctx)
