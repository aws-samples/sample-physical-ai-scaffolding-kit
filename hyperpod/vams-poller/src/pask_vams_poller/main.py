"""Entry point of the VAMS poller service: run the intake loop and the reconciler together.

systemd starts ``python -m pask_vams_poller.main`` with ``/etc/pask/vams-poller.env``.
SIGTERM and SIGINT set a stop event; both loops stop at the end of their current step
and the process exits with 0. When either loop ends unexpectedly the other one is asked
to stop and the process exits with a non zero status, so that ``Restart=always`` starts a
new poller. Logs go to standard output, which journald collects.
"""

import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Callable, Mapping
from types import FrameType

import boto3
from botocore.config import Config

from pask_vams_poller.config import PollerConfig, load_training_env
from pask_vams_poller.context import PollerContext, build_owner_id
from pask_vams_poller.intake import IntakeLoop
from pask_vams_poller.reconciler import Reconciler
from pask_vams_poller.sfn import SfnNotifier
from pask_vams_poller.slurm import SlurmClient
from pask_vams_poller.store import ExecutionStore

logger = logging.getLogger(__name__)

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
QUIET_LOGGERS: tuple[str, ...] = ("boto3", "botocore", "urllib3")
STOP_SIGNALS: tuple[signal.Signals, ...] = (signal.SIGTERM, signal.SIGINT)
SUPERVISE_POLL_SECONDS = 1.0
EXIT_OK = 0
EXIT_LOOP_FAILED = 1

Loop = Callable[[threading.Event], None]


class LoopThread:
    """Run one loop in a thread and record whether it ended unexpectedly."""

    def __init__(self, name: str, loop: Loop, stop_event: threading.Event) -> None:
        """Create the thread without starting it.

        Args:
            name: Name used in the thread name and in the logs.
            loop: Function that runs until the stop event is set.
            stop_event: Shared stop event. It is set when this loop ends unexpectedly.
        """
        self.name = name
        self.failed = False
        self._loop = loop
        self._stop_event = stop_event
        self._thread = threading.Thread(target=self._run, name=f"pask-vams-{name}")

    def start(self) -> None:
        """Start the thread."""
        self._thread.start()

    def join(self) -> None:
        """Wait for the thread to end."""
        self._thread.join()

    def is_alive(self) -> bool:
        """Tell whether the thread is still running.

        Returns:
            True while the loop runs.
        """
        return self._thread.is_alive()

    def _run(self) -> None:
        """Run the loop, and stop the process when it raises or returns before a stop.

        The exception is caught here because it cannot propagate out of the thread. It
        is logged with its traceback and turned into a non zero exit status.
        """
        try:
            self._loop(self._stop_event)
        except Exception:
            logger.exception("The %s loop failed; stopping the poller", self.name)
            self._fail()
            return
        if not self._stop_event.is_set():
            logger.error("The %s loop returned without a stop request; stopping", self.name)
            self._fail()

    def _fail(self) -> None:
        """Record the failure and ask the other loops to stop."""
        self.failed = True
        self._stop_event.set()


def run_loops(loops: Mapping[str, Loop], stop_event: threading.Event) -> int:
    """Run the loops in parallel threads until the stop event is set, then wait for them.

    Args:
        loops: Loop functions by name.
        stop_event: Set by a signal handler or by a failed loop.

    Returns:
        ``EXIT_OK`` after a requested stop, or ``EXIT_LOOP_FAILED`` when a loop failed.
    """
    threads = [LoopThread(name, loop, stop_event) for name, loop in loops.items()]
    for thread in threads:
        thread.start()
    while not stop_event.wait(SUPERVISE_POLL_SECONDS):
        pass
    logger.info("Stopping; waiting for the loops to finish their current step")
    for thread in threads:
        thread.join()
    failed_names = [thread.name for thread in threads if thread.failed]
    if failed_names:
        logger.error("Poller stopped because these loops failed: %s", ", ".join(failed_names))
        return EXIT_LOOP_FAILED
    logger.info("Poller stopped")
    return EXIT_OK


def install_stop_signal_handlers(stop_event: threading.Event) -> None:
    """Set the stop event on SIGTERM and SIGINT.

    Must be called from the main thread.

    Args:
        stop_event: Event to set.
    """

    def request_stop(signal_number: int, frame: FrameType | None) -> None:
        """Log the signal and set the stop event."""
        logger.info("Received %s", signal.Signals(signal_number).name)
        stop_event.set()

    for stop_signal in STOP_SIGNALS:
        signal.signal(stop_signal, request_stop)


def configure_logging() -> None:
    """Write INFO and higher to standard output with UTC time, level, and module."""
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter(LOG_FORMAT, LOG_DATE_FORMAT)
    formatter.converter = time.gmtime
    handler.setFormatter(formatter)
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)
    root_logger.setLevel(logging.INFO)
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def build_context(environ: Mapping[str, str]) -> PollerContext:
    """Read the settings and create the AWS clients, the store, and the poller context.

    Args:
        environ: Process environment with the ``PASK_VAMS_*`` variables.

    Returns:
        The poller context.

    Raises:
        ValueError: A setting or the training environment file is invalid.
    """
    poller_config = PollerConfig.from_env(environ)
    training_env = load_training_env(poller_config.training_env_file)
    owner_id = build_owner_id(environ)
    session = boto3.session.Session(region_name=poller_config.region)
    client_config = Config(retries={"mode": "standard"})
    store = ExecutionStore(
        poller_config.table_name,
        region=poller_config.region,
        owner_id=owner_id,
        lease_seconds=poller_config.lease_seconds,
        ttl_days=poller_config.item_ttl_days,
        dynamodb_client=session.client("dynamodb", config=client_config),
    )
    return PollerContext(
        config=poller_config,
        store=store,
        sfn=SfnNotifier(session.client("stepfunctions", config=client_config)),
        slurm=SlurmClient(),
        s3=session.client("s3", config=client_config),
        sqs=session.client("sqs", config=client_config),
        training_env=training_env,
        owner_id=owner_id,
    )


def main() -> None:
    """Start the poller and exit with the status of ``run_loops``.

    Raises:
        SystemExit: Always, with ``EXIT_OK`` or ``EXIT_LOOP_FAILED``.
    """
    configure_logging()
    ctx = build_context(os.environ)
    logger.info("Starting the VAMS poller owner=%s table=%s", ctx.owner_id, ctx.config.table_name)
    stop_event = threading.Event()
    install_stop_signal_handlers(stop_event)
    loops: dict[str, Loop] = {
        "intake": IntakeLoop(ctx).run,
        "reconciler": Reconciler(ctx).run,
    }
    sys.exit(run_loops(loops, stop_event))


if __name__ == "__main__":
    main()
