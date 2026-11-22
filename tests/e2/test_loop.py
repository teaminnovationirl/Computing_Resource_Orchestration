"""The admission loop thread (plan 8.3 and 8.5, D-75): one cycle per admission_cycle_s, or at once after a
wake, never two at once, and no failing cycle ends the thread. FakeClock drives every wait, so nothing
sleeps; FakeService only counts cycles and how many ran at the same time.
"""
from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest
from psycopg import errors

from app.common.clock import FakeClock
from app.common.types import QueueEntry, Snapshot, Vector
from app.config import Config
from app.e2_scheduling.loop import AdmissionLoop, Admitter
from app.e2_scheduling.service import E2Service
from tests.helpers import TempDb, logged

CONFIG = Config()  # admission_cycle_s = 0.5


class FakeService:
    """An Admitter whose cycles do nothing but count, and note the threads that ran them. The first `gated`
    cycles block until gate is set."""

    def __init__(self, gated: int = 0) -> None:
        self.wake = threading.Event()
        self.gate = threading.Event()
        self.entered = threading.Event()
        self.gated = gated
        self.cycles = 0
        self.active = 0
        self.most_at_once = 0
        self.threads: set[int | None] = set()
        self.errors: list[Exception] = []  # raised by the next cycles, one each
        self._cond = threading.Condition()

    def run_cycle(self) -> None:
        with self._cond:
            self.cycles += 1
            number = self.cycles
            self.active += 1
            self.most_at_once = max(self.most_at_once, self.active)
            self.threads.add(threading.get_ident())
        try:
            if number <= self.gated:
                self.entered.set()
                assert self.gate.wait(10), "the gate never opened"
            if self.errors:
                raise self.errors.pop(0)
        finally:
            with self._cond:
                self.active -= 1
                self._cond.notify_all()

    def wait_for_cycles(self, count: int) -> None:
        with self._cond:
            assert self._cond.wait_for(lambda: self.cycles >= count and self.active == 0, 5), (
                f"expected {count} finished cycle(s), saw {self.cycles}")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@contextmanager
def running(service: Admitter, clock: FakeClock) -> Iterator[AdmissionLoop]:
    """The loop started and waiting for its first period; stopped afterwards."""
    loop = AdmissionLoop(service, clock, CONFIG)
    loop.start()
    try:
        clock.wait_for_waiters(1)
        yield loop
    finally:
        assert loop.stop(timeout=5), "the loop did not stop"


def test_a_cycle_runs_once_the_period_has_passed_and_not_before(clock: FakeClock):
    service = FakeService()
    with running(service, clock):
        clock.advance(0.4375)
        clock.wait_for_waiters(1)
        assert service.cycles == 0
        clock.advance(0.0625)
        service.wait_for_cycles(1)
        clock.wait_for_waiters(1)
        assert service.cycles == 1


def test_a_wake_runs_a_cycle_at_once_without_the_clock_moving(clock: FakeClock):
    service = FakeService()
    with running(service, clock):
        service.wake.set()
        service.wait_for_cycles(1)
        assert clock.monotonic() == 0.0


def test_1000_wakes_during_a_cycle_give_exactly_one_more_cycle_on_the_loop_thread(clock: FakeClock):
    service = FakeService(gated=1)
    with running(service, clock) as loop:
        service.wake.set()
        assert service.entered.wait(5)
        barrier = threading.Barrier(8)

        def wake_125_times() -> None:
            barrier.wait(5)
            for _ in range(125):
                service.wake.set()

        wakers = [threading.Thread(target=wake_125_times) for _ in range(8)]
        for t in wakers:
            t.start()
        for t in wakers:
            t.join(5)
        service.gate.set()
        clock.wait_for_waiters(1)  # cycle 1 ended, cycle 2 ran at once, and the loop waits again
        assert (service.cycles, service.most_at_once) == (2, 1)
        # Deterministic, unlike the overlap count: a cycle run off the loop thread may still happen to
        # start after cycle 1 has ended.
        assert service.threads == {loop.ident}


def test_stop_ends_a_waiting_loop_at_once_without_running_a_cycle(clock: FakeClock, json_log: Path):
    service = FakeService()
    loop = AdmissionLoop(service, clock, CONFIG)
    loop.start()
    clock.wait_for_waiters(1)
    assert loop.stop(timeout=5) is True
    assert not loop.is_alive()
    assert service.cycles == 0
    assert len(logged(json_log, "admission_loop_stopped")) == 1


def test_the_loop_outlives_snapshot_errors_and_logs_each_lasting_failure_once(
        seeded: TempDb, tmp_path: Path, json_log: Path, clock: FakeClock):
    e2 = E2Service(Config(data_dir=tmp_path), clock=clock)
    e2.queue.push(QueueEntry(1042, "01FE24BCS901", 3, datetime(2026, 11, 22, 9, 0, tzinfo=UTC), Vector(100, 64)))
    stub = seeded.stubs.e3
    stub.snapshot_error = psycopg.OperationalError("connection refused")
    with running(e2, clock) as loop:
        for _ in range(5):
            clock.advance(0.5)
            clock.wait_for_waiters(1)
        assert loop.is_alive()
        assert len(logged(json_log, "admission_cycle_failed")) == 1

        stub.snapshot_error = ValueError("no capacity loaded: resources has no cpu row")
        clock.advance(0.5)
        clock.wait_for_waiters(1)
        failed = logged(json_log, "admission_cycle_failed")
        assert [line["error"].split(":")[0] for line in failed] == ["OperationalError", "ValueError"]

        stub.snapshot_error = None
        stub.snapshot_value = Snapshot(cpu_free=0, ram_free=0, gpu_free={})  # blocked: it reads the maxima too
        clock.advance(0.5)
        clock.wait_for_waiters(1)
        recovered = logged(json_log, "admission_cycle_recovered")
        assert [line["failed_cycles"] for line in recovered] == [6]
        assert loop.is_alive()
    assert [name for name, _ in stub.calls] == ["snapshot"] * 7


def test_failures_with_one_sqlstate_are_one_failure_whatever_their_message(clock: FakeClock, json_log: Path):
    service = FakeService()
    service.errors = [errors.DeadlockDetected("process 101 waits for ShareLock; blocked by process 202"),
                      errors.DeadlockDetected("process 303 waits for ShareLock; blocked by process 404")]
    with running(service, clock):
        for _ in range(2):
            clock.advance(0.5)
            clock.wait_for_waiters(1)
    assert len(logged(json_log, "admission_cycle_failed")) == 1


def test_a_stop_that_times_out_says_so(clock: FakeClock, json_log: Path):
    service = FakeService(gated=1)
    loop = AdmissionLoop(service, clock, CONFIG)
    loop.start()
    clock.wait_for_waiters(1)
    service.wake.set()
    assert service.entered.wait(5)
    assert loop.stop(timeout=0) is False  # the gated cycle is still running
    assert [line["timeout_s"] for line in logged(json_log, "admission_loop_stop_timeout")] == [0]
    service.gate.set()
    assert loop.stop(timeout=5) is True
