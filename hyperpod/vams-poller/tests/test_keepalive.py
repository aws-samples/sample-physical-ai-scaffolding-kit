"""Tests for the background keepalive."""

import threading

import pytest

from pask_vams_poller.keepalive import Keepalive

INTERVAL_SECONDS = 0.01
WAIT_TIMEOUT_SECONDS = 5.0


def test_keepalive_calls_callback_until_stopped() -> None:
    call_count = 0
    called_three_times = threading.Event()

    def callback() -> bool:
        nonlocal call_count
        call_count += 1
        if call_count >= 3:
            called_three_times.set()
        return True

    with Keepalive(callback, INTERVAL_SECONDS) as keepalive:
        assert called_three_times.wait(WAIT_TIMEOUT_SECONDS)

    count_after_stop = call_count
    assert not keepalive.should_stop.is_set()
    assert keepalive.error is None
    assert not keepalive._thread.is_alive()
    threading.Event().wait(INTERVAL_SECONDS * 5)
    assert call_count == count_after_stop


def test_keepalive_sets_should_stop_when_callback_returns_false() -> None:
    call_count = 0

    def callback() -> bool:
        nonlocal call_count
        call_count += 1
        return False

    with Keepalive(callback, INTERVAL_SECONDS) as keepalive:
        assert keepalive.should_stop.wait(WAIT_TIMEOUT_SECONDS)
        keepalive._thread.join(WAIT_TIMEOUT_SECONDS)
        assert not keepalive._thread.is_alive()

    assert call_count == 1
    assert keepalive.error is None


def test_keepalive_records_callback_exception() -> None:
    failure = RuntimeError("heartbeat failed")

    def callback() -> bool:
        raise failure

    with Keepalive(callback, INTERVAL_SECONDS) as keepalive:
        assert keepalive.should_stop.wait(WAIT_TIMEOUT_SECONDS)

    assert keepalive.error is failure


def test_keepalive_does_not_call_before_first_interval() -> None:
    called = threading.Event()

    def callback() -> bool:
        called.set()
        return True

    with Keepalive(callback, 60.0):
        assert not called.wait(INTERVAL_SECONDS * 5)

    assert not called.is_set()


def test_keepalive_propagates_exception_from_block() -> None:
    with pytest.raises(ValueError, match="work failed"), Keepalive(lambda: True, INTERVAL_SECONDS):
        raise ValueError("work failed")


def test_keepalive_rejects_non_positive_interval() -> None:
    with pytest.raises(ValueError, match="interval_seconds"):
        Keepalive(lambda: True, 0)


def test_keepalive_cannot_start_twice() -> None:
    keepalive = Keepalive(lambda: True, INTERVAL_SECONDS)
    keepalive.start()
    try:
        with pytest.raises(RuntimeError, match="already started"):
            keepalive.start()
    finally:
        keepalive.stop()
