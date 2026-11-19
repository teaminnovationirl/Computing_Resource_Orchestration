"""Clock, RealClock and FakeClock (plan 5.2). Each docstring names the bug the test catches."""
from __future__ import annotations

import threading
import time

import pytest

from app.common.clock import Clock, FakeClock, RealClock


def _wait_in_thread(clock: Clock, event: threading.Event, timeout: float) -> tuple[threading.Thread, list[bool]]:
    result: list[bool] = []
    thread = threading.Thread(target=lambda: result.append(clock.wait(event, timeout)), daemon=True)
    thread.start()
    return thread, result


def test_fake_clock_times_out_only_when_advanced():
    """Catches a fake wait that sleeps for real, or returns before advance() passes its deadline.
    A one-hour timeout must cost no real hour."""
    clock = FakeClock()
    started = time.monotonic()
    thread, result = _wait_in_thread(clock, threading.Event(), 3600)
    clock.wait_for_waiters(1)
    clock.advance(3599.5)
    clock.wait_for_waiters(1)  # still blocked: its deadline is ahead
    assert result == []
    clock.advance(0.5)
    thread.join(timeout=2)
    assert result == [False]
    assert clock.monotonic() == 3600 and clock.time() == 3600
    assert time.monotonic() - started < 1
    with pytest.raises(ValueError):
        clock.advance(-1)


def test_fake_clock_wakes_at_once_when_the_event_is_set():
    """Catches a lost wake-up: an event set by another thread must end the wait without advance()."""
    clock = FakeClock()
    event = threading.Event()
    started = time.monotonic()
    thread, result = _wait_in_thread(clock, event, 3600)
    clock.wait_for_waiters(1)
    event.set()
    thread.join(timeout=2)
    assert result == [True] and clock.monotonic() == 0
    assert time.monotonic() - started < 1


def test_wait_for_waiters_fails_without_its_waiters():
    """Catches a sync helper that returns although nobody waits, which would make loop tests racy."""
    with pytest.raises(AssertionError, match="expected 1 waiting"):
        FakeClock().wait_for_waiters(1, timeout=0.05)


def test_real_clock_waits_on_the_event():
    """Catches a RealClock that ignores the event or the timeout."""
    clock = RealClock()
    before = clock.monotonic()
    assert clock.wait(threading.Event(), 0.01) is False
    event = threading.Event()
    event.set()
    assert clock.wait(event, 10) is True
    assert clock.monotonic() >= before and clock.time() > 1_000_000_000
