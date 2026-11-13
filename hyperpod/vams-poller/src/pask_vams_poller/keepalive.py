"""Background keepalive that sends heartbeats and extends the lease during long work."""

import logging
import threading
from collections.abc import Callable
from types import TracebackType
from typing import final

logger = logging.getLogger(__name__)


@final
class Keepalive:
    """Call a callback periodically from a background thread.

    The first call happens ``interval_seconds`` after the thread starts, so the caller
    sends the first heartbeat itself before entering. When the callback returns False or
    raises, ``should_stop`` is set and the thread ends. The long running work checks
    ``should_stop`` at safe points and stops there. The raised exception is kept in
    ``error`` so that the caller can report it.
    """

    def __init__(self, callback: Callable[[], bool], interval_seconds: float) -> None:
        """Create the keepalive without starting it.

        Args:
            callback: Function that returns True to keep going and False to stop.
            interval_seconds: Seconds between calls. Must be positive.

        Raises:
            ValueError: ``interval_seconds`` is not positive.
        """
        if interval_seconds <= 0:
            raise ValueError(f"interval_seconds must be positive, got {interval_seconds}")
        self._callback = callback
        self._interval_seconds = interval_seconds
        self._stop_requested = threading.Event()
        self._thread: threading.Thread | None = None
        self.should_stop = threading.Event()
        self.error: Exception | None = None

    def start(self) -> None:
        """Start the background thread.

        Raises:
            RuntimeError: The keepalive was already started.
        """
        if self._thread is not None:
            raise RuntimeError("Keepalive was already started")
        self._thread = threading.Thread(target=self._run, name="pask-vams-keepalive", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the background thread and wait for it to end."""
        self._stop_requested.set()
        if self._thread is not None:
            self._thread.join()

    def _run(self) -> None:
        """Call the callback every interval until stopped or until it fails."""
        while not self._stop_requested.wait(self._interval_seconds):
            if not self._call_once():
                self.should_stop.set()
                return

    def _call_once(self) -> bool:
        """Call the callback once and record an exception if it raises.

        The exception is caught here because it cannot propagate out of the thread. It
        is logged with its traceback, stored in ``error``, and reported as a stop.

        Returns:
            True to keep going, False to stop.
        """
        try:
            return bool(self._callback())
        except Exception as error:
            logger.exception("Keepalive callback failed")
            self.error = error
            return False

    def __enter__(self) -> "Keepalive":
        """Start the keepalive.

        Returns:
            This keepalive.
        """
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Stop the keepalive. Exceptions raised in the block propagate unchanged."""
        self.stop()
