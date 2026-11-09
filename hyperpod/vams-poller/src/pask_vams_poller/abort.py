"""ABORT_SUSPECTED handling: restore on a new token, or scancel and ABORTED after the grace.

A heartbeat that fails with a closed token error on the latest token moves an execution
to ABORT_SUSPECTED instead of cancelling it at once, because a VAMS retry closes the old
token and delivers a new one shortly after. This module decides what happens next:

1. A heartbeat with the latest stored token is accepted: the intake loop delivered a new
   token, so the execution returns to ``prevState``.
2. The latest token is still closed and the grace period has not passed: nothing changes.
3. The grace period has passed: the Slurm job is cancelled and, once ``squeue`` no longer
   shows it, the execution becomes ABORTED. The cleanup of the staging prefix and the FSx
   directories is left to the cleanup handler, which picks up ``pending=CLEANUP``.

``ExecutionStore.to_abort_suspected`` does not store the token of the failed heartbeat,
so a replaced token cannot be told apart by comparing tokens. Instead, every call sends a
heartbeat with the latest stored token. A closed token never reopens (``TaskTimedOut``
was returned after an abort, after a retry, and after the task timeout in the
pre-verification), so an accepted heartbeat proves that the token was replaced.
"""

import logging

from pask_vams_poller.constants import State
from pask_vams_poller.context import PollerContext
from pask_vams_poller.errors import StaleItemError
from pask_vams_poller.sfn import CallResult, TokenCallOutcome, call_with_latest_token
from pask_vams_poller.store import ExecutionItem

logger = logging.getLogger(__name__)

NO_KNOWN_CLOSED_TOKEN = ""
"""Value passed as the suspected token when restoring from ABORT_SUSPECTED.

The store keeps no copy of the token whose heartbeat failed. ``restore_from_abort`` also
checks ``C(ABORT_SUSPECTED)``, whose version condition fails when the token changes, so
the stored token is the one whose heartbeat was just accepted and thus not a closed one.
An empty string never equals a Step Functions task token, so the extra
``taskToken <> :suspectedToken`` condition adds nothing beyond that guarantee.
"""


def process_abort_suspected(item: ExecutionItem, ctx: PollerContext) -> None:
    """Handle one execution in ABORT_SUSPECTED.

    The caller holds the lease of the execution and passes the item as last read.

    Args:
        item: Execution in ABORT_SUSPECTED with ``taskToken`` and ``suspectedAt``.
        ctx: Poller context.

    Raises:
        ValueError: The execution is not in ABORT_SUSPECTED or lacks the token or
            ``suspectedAt``.
        TransientError: The heartbeat failed temporarily or the token kept changing. The
            state is unchanged, so the next reconcile cycle retries.
        SlurmCommandError: scancel or squeue failed. The state is unchanged.
        StaleItemError: The item changed while it was handled. The caller reads it again.
    """
    _validate_suspected_item(item)
    heartbeat = _heartbeat_latest_token(item, ctx)
    current = _item_for_token(item, heartbeat, ctx)
    if heartbeat.result is CallResult.ACCEPTED:
        restored = ctx.store.restore_from_abort(current, NO_KNOWN_CLOSED_TOKEN)
        logger.info(
            "Restored %s from ABORT_SUSPECTED to %s after a heartbeat with a new token",
            current.pipelineExecutionId,
            restored.state.value,
        )
        return

    grace_limit = int(ctx.clock()) - ctx.config.abort_grace_seconds
    if current.suspectedAt >= grace_limit:
        return
    if not _cancel_job_and_confirm_gone(current, ctx):
        return
    ctx.store.to_aborted(current, heartbeat.token, grace_limit=grace_limit)
    logger.info("Moved %s to ABORTED", current.pipelineExecutionId)


def _validate_suspected_item(item: ExecutionItem) -> None:
    """Check that an execution is in ABORT_SUSPECTED with the attributes the handler needs.

    Args:
        item: Execution to check.

    Raises:
        ValueError: The state is not ABORT_SUSPECTED, or ``taskToken`` or ``suspectedAt``
            is missing.
    """
    if item.state is not State.ABORT_SUSPECTED:
        raise ValueError(
            f"{item.pipelineExecutionId} must be {State.ABORT_SUSPECTED.value}, "
            f"got {item.state.value}"
        )
    if not item.taskToken:
        raise ValueError(f"{item.pipelineExecutionId} has no taskToken")
    if item.suspectedAt is None:
        raise ValueError(f"{item.pipelineExecutionId} has no suspectedAt")


def _heartbeat_latest_token(item: ExecutionItem, ctx: PollerContext) -> TokenCallOutcome:
    """Send a heartbeat with the latest stored token, following replacements.

    Args:
        item: Execution in ABORT_SUSPECTED.
        ctx: Poller context.

    Returns:
        ACCEPTED when a token is open, or CLOSED when the latest token is closed. ``token``
        is the token used by the last call.

    Raises:
        TransientError: The heartbeat failed temporarily or the token kept changing.
    """
    pipeline_execution_id = item.pipelineExecutionId

    def read_latest_token() -> str | None:
        """Read the latest task token with a strongly consistent read."""
        latest = ctx.store.get(pipeline_execution_id)
        return None if latest is None else latest.taskToken

    return call_with_latest_token(ctx.sfn.heartbeat, item.taskToken, read_latest_token)


def _item_for_token(
    item: ExecutionItem, heartbeat: TokenCallOutcome, ctx: PollerContext
) -> ExecutionItem:
    """Return the execution whose stored token is the one the last heartbeat used.

    When the heartbeat followed a replaced token, the version of ``item`` is outdated, so
    the item is read again for the conditional update that follows.

    Args:
        item: Execution as passed to the handler.
        heartbeat: Result of the heartbeat.
        ctx: Poller context.

    Returns:
        ``item`` itself, or the item read again.

    Raises:
        StaleItemError: The item is gone, left ABORT_SUSPECTED, or its token changed again.
    """
    if not heartbeat.replaced:
        return item
    current = ctx.store.get(item.pipelineExecutionId)
    if (
        current is None
        or current.state is not State.ABORT_SUSPECTED
        or current.taskToken != heartbeat.token
    ):
        raise StaleItemError(f"{item.pipelineExecutionId} changed while sending a heartbeat")
    _validate_suspected_item(current)
    return current


def _cancel_job_and_confirm_gone(item: ExecutionItem, ctx: PollerContext) -> bool:
    """Cancel the Slurm job of an execution and check that squeue no longer shows it.

    Executions that never reached SUBMITTING have no ``jobName`` and no job, so nothing is
    cancelled. scancel is safe to repeat, so a job that is still listed is cancelled again
    on the next reconcile cycle.

    Args:
        item: Execution in ABORT_SUSPECTED after the grace period.
        ctx: Poller context.

    Returns:
        True when no job with the name is queued or running.

    Raises:
        SlurmCommandError: scancel or squeue failed.
    """
    if not item.jobName:
        return True
    ctx.slurm.cancel_by_name(item.jobName)
    remaining_jobs = ctx.slurm.find_by_name(item.jobName)
    if remaining_jobs:
        logger.info(
            "Job %s of %s is still listed after scancel: %s",
            item.jobName,
            item.pipelineExecutionId,
            ", ".join(f"{job.job_id}={job.state}" for job in remaining_jobs),
        )
        return False
    return True
