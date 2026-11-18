"""The one source of time for timers and loop pacing (plan 5.2).

Deadlines and timers use monotonic(); time() is wall time, for the 60 s guard only; database times
come from clock_timestamp(). Loops pace themselves with wait(event, period), so tests drive them
with FakeClock.advance() and never sleep. A plain sleep is wait(threading.Event(), seconds).
"""
from __future__ import annotations

import threading
import time
from typing import Final, Protocol


class Clock(Protocol):
    def monotonic(self) -> float:
        """Seconds for deadlines and timers."""
        ...

    def time(self) -> float:
        """Wall seconds since the epoch; the 60 s guard only."""
        ...

    def wait(self, event: threading.Event, timeout: float) -> bool:
        """Block until event is set (True) or timeout seconds pass (False)."""
        ...


class RealClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def time(self) -> float:
        return time.time()

    def wait(self, event: threading.Event, timeout: float) -> bool:
        return event.wait(timeout)


class FakeClock:
    """Time that moves only when a test calls advance(); monotonic() and time() both read it.

    wait() returns True as soon as its event is set, and False once advance() has passed its
    deadline. Between checks of the fake time it waits on the event for at most POLL_S real
    seconds, so a fake timeout never costs real time (D-50). A test that compares time() with
    database times (the 60 s guard reads submitted_at) starts the clock at the database's now.
    """

    POLL_S: Final = 0.005

    def __init__(self, start: float = 0.0) -> None:
        self._now = start
        self._cond = threading.Condition()
        self._waiters: dict[object, list[float]] = {}  # per blocked wait(): [deadline, fake time last seen]

    def monotonic(self) -> float:
        with self._cond:
            return self._now

    def time(self) -> float:
        return self.monotonic()

    def wait(self, event: threading.Event, timeout: float) -> bool:
        if event.is_set() or timeout <= 0:
            return event.is_set()
        token = object()
        with self._cond:
            deadline = self._now + timeout
            self._waiters[token] = [deadline, self._now]
            self._cond.notify_all()
        try:
            while not event.wait(self.POLL_S):
                with self._cond:
                    self._waiters[token][1] = self._now
                    if self._now >= deadline:
                        return event.is_set()
                    self._cond.notify_all()
            return True
        finally:
            with self._cond:
                del self._waiters[token]
                self._cond.notify_all()

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("FakeClock: time only moves forward")
        with self._cond:
            self._now += seconds
            self._cond.notify_all()

    def wait_for_waiters(self, count: int, timeout: float = 5.0) -> None:
        """Block until exactly count threads wait, each has looked at the current fake time and
        kept waiting: every thread that advance() released has run on and waits again, or has
        left. timeout is in real seconds and only bounds a failing test."""
        def settled() -> bool:
            return len(self._waiters) == count and all(
                deadline > self._now and seen == self._now for deadline, seen in self._waiters.values())

        with self._cond:
            if not self._cond.wait_for(settled, timeout):
                raise AssertionError(f"FakeClock: expected {count} waiting thread(s) that saw the current time, "
                                     f"found {len(self._waiters)} waiting")
