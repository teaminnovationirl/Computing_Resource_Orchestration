"""Fixtures of the E1 tests (plan 9.9): no database and no Docker, on any OS."""
from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any

import pytest

from app.common.clock import FakeClock
from app.e1_execution.fake_runner import FakeRunner


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def runner(clock: FakeClock) -> FakeRunner:
    return FakeRunner(clock)


@pytest.fixture
def advancing(clock: FakeClock) -> Callable[..., Any]:
    """advancing(call, *steps): runs call in a thread and, each time it waits on the clock, advances the clock by
    the next step, or calls it when it is a function; returns what call returned. No real time passes. A call that
    still waits after the last step fails the test: the thread is a daemon, so it holds up nothing."""
    def run(call: Callable[[], Any], *steps: float | Callable[[], object]) -> Any:
        future: Future[Any] = Future()

        def target() -> None:
            try:
                future.set_result(call())
            except BaseException as error:  # noqa: BLE001 (raised again in the test's thread by result())
                future.set_exception(error)

        threading.Thread(target=target, daemon=True).start()
        for step in steps:
            clock.wait_for_waiters(1)
            if callable(step):
                step()
            else:
                clock.advance(step)
        return future.result(timeout=10)

    return run
