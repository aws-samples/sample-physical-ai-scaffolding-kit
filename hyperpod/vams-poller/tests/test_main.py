"""Tests for the poller entry point: stop on signals, exit on loop failures, and wiring."""

import os
import signal
import socket
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from pask_vams_poller import main
from pask_vams_poller.sfn import SfnNotifier
from pask_vams_poller.slurm import SlurmClient
from pask_vams_poller.store import ExecutionStore

from .conftest import TEST_REGION, TEST_TABLE_NAME

THREAD_JOIN_TIMEOUT_SECONDS = 10


class WaitingLoop:
    """Loop that runs until the stop event is set and records that it started and ended."""

    def __init__(self) -> None:
        """Create the loop."""
        self.started = threading.Event()
        self.finished = threading.Event()

    def __call__(self, stop_event: threading.Event) -> None:
        """Wait for the stop event."""
        self.started.set()
        stop_event.wait()
        self.finished.set()


@pytest.fixture
def restore_signal_handlers() -> Iterator[None]:
    """Put back the SIGTERM and SIGINT handlers that the test replaced."""
    saved = {stop_signal: signal.getsignal(stop_signal) for stop_signal in main.STOP_SIGNALS}
    yield
    for stop_signal, handler in saved.items():
        signal.signal(stop_signal, handler)


@pytest.mark.parametrize("stop_signal", [signal.SIGTERM, signal.SIGINT])
def test_signal_stops_both_loops_and_exits_with_zero(
    stop_signal: signal.Signals, restore_signal_handlers: None
) -> None:
    intake_loop = WaitingLoop()
    reconcile_loop = WaitingLoop()
    stop_event = threading.Event()
    main.install_stop_signal_handlers(stop_event)

    def send_signal_when_started() -> None:
        intake_loop.started.wait(THREAD_JOIN_TIMEOUT_SECONDS)
        reconcile_loop.started.wait(THREAD_JOIN_TIMEOUT_SECONDS)
        os.kill(os.getpid(), stop_signal)

    sender = threading.Thread(target=send_signal_when_started)
    sender.start()
    exit_code = main.run_loops({"intake": intake_loop, "reconciler": reconcile_loop}, stop_event)
    sender.join()

    assert exit_code == main.EXIT_OK
    assert intake_loop.finished.is_set()
    assert reconcile_loop.finished.is_set()


def test_a_crashing_loop_stops_the_other_and_exits_with_failure() -> None:
    reconcile_loop = WaitingLoop()

    def crashing_loop(stop_event: threading.Event) -> None:
        reconcile_loop.started.wait(THREAD_JOIN_TIMEOUT_SECONDS)
        raise RuntimeError("boom")

    exit_code = main.run_loops(
        {"intake": crashing_loop, "reconciler": reconcile_loop}, threading.Event()
    )

    assert exit_code == main.EXIT_LOOP_FAILED
    assert reconcile_loop.finished.is_set()


def test_a_loop_that_returns_early_exits_with_failure() -> None:
    reconcile_loop = WaitingLoop()

    exit_code = main.run_loops(
        {"intake": lambda stop_event: None, "reconciler": reconcile_loop}, threading.Event()
    )

    assert exit_code == main.EXIT_LOOP_FAILED
    assert reconcile_loop.finished.is_set()


def test_build_context_wires_settings_and_clients(
    poller_env: dict[str, str], tmp_path: Path
) -> None:
    training_env_file = tmp_path / "lerobot-openpi.env"
    training_env_file.write_text(
        "LEROBOT_PI_BASE_DIR=/fsx/ubuntu/lerobot-openpi\nHF_TOKEN=hf_test\n", encoding="utf-8"
    )
    training_env_file.chmod(0o600)
    environ = {
        **poller_env,
        "PASK_VAMS_TRAINING_ENV_FILE": str(training_env_file),
        "PASK_VAMS_LEASE_SECONDS": "600",
        "INVOCATION_ID": "invocation-1",
    }

    ctx = main.build_context(environ)

    assert ctx.owner_id == f"{socket.gethostname()}:invocation-1"
    assert ctx.training_env == {
        "LEROBOT_PI_BASE_DIR": "/fsx/ubuntu/lerobot-openpi",
        "HF_TOKEN": "hf_test",
    }
    assert isinstance(ctx.store, ExecutionStore)
    assert ctx.store.table_name == TEST_TABLE_NAME
    assert ctx.store.owner_id == ctx.owner_id
    assert ctx.store.lease_seconds == 600
    assert ctx.store.ttl_days == ctx.config.item_ttl_days
    assert isinstance(ctx.sfn, SfnNotifier)
    assert isinstance(ctx.slurm, SlurmClient)
    for client in (ctx.s3, ctx.sqs, ctx.store._client):
        assert client.meta.region_name == TEST_REGION
        assert client.meta.config.retries["mode"] == "standard"


def test_build_context_uses_the_process_id_outside_systemd(
    poller_env: dict[str, str], tmp_path: Path
) -> None:
    training_env_file = tmp_path / "lerobot-openpi.env"
    training_env_file.write_text("LEROBOT_PI_BASE_DIR=/fsx/ubuntu\n", encoding="utf-8")
    training_env_file.chmod(0o600)
    environ = {**poller_env, "PASK_VAMS_TRAINING_ENV_FILE": str(training_env_file)}

    ctx = main.build_context(environ)

    assert ctx.owner_id == f"{socket.gethostname()}:{os.getpid()}"


def test_build_context_rejects_a_training_env_file_readable_by_others(
    poller_env: dict[str, str], tmp_path: Path
) -> None:
    training_env_file = tmp_path / "lerobot-openpi.env"
    training_env_file.write_text("LEROBOT_PI_BASE_DIR=/fsx/ubuntu\n", encoding="utf-8")
    training_env_file.chmod(0o644)
    environ = {**poller_env, "PASK_VAMS_TRAINING_ENV_FILE": str(training_env_file)}

    with pytest.raises(ValueError, match="group or others"):
        main.build_context(environ)
