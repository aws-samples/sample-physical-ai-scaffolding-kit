"""Exception types that drive the state handlers of the VAMS poller."""

from pask_vams_poller.constants import ErrorName


class PermanentFailure(Exception):
    """A failure that retrying cannot fix.

    A state handler raises it to finish the execution with ``outcome=FAILED``.
    """

    def __init__(self, error_name: ErrorName, cause: str) -> None:
        """Create the failure.

        Args:
            error_name: Error name sent to Step Functions with ``SendTaskFailure``.
            cause: Human readable reason sent as the ``cause`` of ``SendTaskFailure``.
        """
        super().__init__(f"{error_name.value}: {cause}")
        self.error_name = error_name
        self.cause = cause


class TransientError(Exception):
    """A failure that a later retry of the same state handler can fix."""


class StaleItemError(Exception):
    """A conditional update failed, so the caller must read the item again."""


class RequestMismatchError(Exception):
    """A message carries a stored pipelineExecutionId but belongs to another workflow request."""
