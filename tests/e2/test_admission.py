"""Admission against the real E3 on S0 (plan 8.3, D-03, D-75, D-104): allocate() and release() are E3's
own, this E2Service is the installed e2, so every release() wakes it, and E1 and E4 stay stubs. The loop
runs on an OffsetClock that is never advanced: a cycle that runs was started by a wake.
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from typing import cast

import psycopg
import pytest

import app.e3_allocation as e3
from app.common.db import tx
from app.common.ports import E2Port, E3Port, Engines, install
from app.common.types import AlreadyReleased, Committed, Refused, Released, Snapshot, SubmitRequest, User, Vector
from app.config import Config
from app.e2_scheduling.loop import AdmissionLoop
from app.e2_scheduling.service import E2Service
from app.e4_monitoring.state import transition
from tests.helpers import (SMALL, OffsetClock, TempDb, assert_invariants, job_history, job_state, logged, make_job,
                           queued)
from tests.stubs import s0_free

STUDENT = User("01FE24BCS901", "student")
REQUEST = SubmitRequest("cpu_bound", 100, 64, 0, 5, source_name="prog.py")


def release_job(job_id: int) -> None:
    """What E1 does when a running job ends: the terminal move after the confirmed stop, then release()."""
    with tx("E1") as t:
        transition(t, job_id, "completed", "E1", "test: finished")
    e3.release(job_id)


def use_e3(e2: E2Service, seeded: TempDb, port: E3Port) -> None:
    install(Engines(e1=seeded.stubs.e1, e2=cast(E2Port, e2), e3=port, e4=seeded.stubs.e4))


class Counted:
    """The loop's view of an E2Service, counting cycles and the most that ran at the same time."""

    def __init__(self, e2: E2Service) -> None:
        self._e2 = e2
        self.cycles = 0
        self.active = 0
        self.most_at_once = 0
        self._cond = threading.Condition()

    @property
    def wake(self) -> threading.Event:
        return self._e2.wake

    def run_cycle(self) -> None:
        with self._cond:
            self.cycles += 1
            self.active += 1
            self.most_at_once = max(self.most_at_once, self.active)
        try:
            self._e2.run_cycle()
        finally:
            with self._cond:
                self.active -= 1
                self._cond.notify_all()

    def wait_until(self, done: Callable[[], bool]) -> None:
        with self._cond:
            assert self._cond.wait_for(lambda: self.active == 0 and done(), 15), (
                f"not done after {self.cycles} cycle(s)")


def test_a_queued_job_is_allocated_and_dispatched_once(e2_real: E2Service, seeded: TempDb):
    job = queued(e2_real.queue)
    e2_real.run_cycle()
    assert job_state(job) == "allocated"
    assert [args[0] for name, args in seeded.stubs.e1.calls if name == "dispatch"] == [job]
    assert len(e2_real.queue) == 0
    assert_invariants()


def test_a_refusal_by_e3_puts_the_job_back_to_queued_in_its_place(e2_real: E2Service, seeded: TempDb):
    make_job("running", Vector(28000, 512))  # 800 millicores left
    first, second = queued(e2_real.queue, Vector(1000, 64)), queued(e2_real.queue)
    tried: list[int] = []

    class Optimistic:
        """The real allocate() behind a snapshot that shows S0 empty, so E2 picks a job that cannot fit."""

        def snapshot(self) -> Snapshot:
            return s0_free()

        def allocate(self, job_id: int, vector: Vector) -> Committed | Refused:
            tried.append(job_id)
            return e3.allocate(job_id, vector)

        def release(self, job_id: int) -> Released | AlreadyReleased:
            return e3.release(job_id)

    use_e3(e2_real, seeded, Optimistic())
    e2_real.run_cycle()
    assert tried == [first]  # the refusal ended the cycle
    assert [move[:3] for move in job_history(first)] == [
        (None, "queued", "E2"), ("queued", "admitted", "E2"), ("admitted", "queued", "E3")]
    assert [e.job_id for e in e2_real.queue.in_fifo_order()] == [first, second]
    assert e2_real._in_doubt is None
    assert_invariants()


def test_a_lost_acknowledgement_is_settled_by_the_next_cycle(e2_real: E2Service, seeded: TempDb, json_log: Path):
    job = queued(e2_real.queue)
    lost = [psycopg.OperationalError("server closed the connection unexpectedly")]

    class LosesOneAnswer:
        """The real E3, except that the first allocate() commits and then raises, as a lost COMMIT
        acknowledgement does."""

        def snapshot(self) -> Snapshot:
            return e3.snapshot()

        def allocate(self, job_id: int, vector: Vector) -> Committed | Refused:
            result = e3.allocate(job_id, vector)
            if lost:
                raise lost.pop()
            return result

        def release(self, job_id: int) -> Released | AlreadyReleased:
            return e3.release(job_id)

    use_e3(e2_real, seeded, LosesOneAnswer())
    with pytest.raises(psycopg.OperationalError):
        e2_real.run_cycle()
    assert job in e2_real.queue

    e2_real.run_cycle()
    assert job not in e2_real.queue
    assert job_state(job) == "allocated"
    [line] = logged(json_log, "allocate_ack_lost")
    assert line["job_id"] == job
    assert "may never have been dispatched (D-72)" in line["detail"]
    assert_invariants()


def test_a_release_wakes_the_loop_at_once(e2_real: E2Service, db_clock: OffsetClock):
    holder = make_job("running", Vector(28000, 512))
    job = queued(e2_real.queue, Vector(1000, 64))  # does not fit until holder is released
    counted = Counted(e2_real)
    loop = AdmissionLoop(counted, db_clock, Config())
    loop.start()
    try:
        db_clock.wait_for_waiters(1)
        release_job(holder)
        counted.wait_until(lambda: job not in e2_real.queue)
        assert job_state(job) == "allocated"
        assert db_clock.monotonic() == 0.0
    finally:
        assert loop.stop(timeout=10)
    assert_invariants()


def test_ten_releases_at_once_run_cycles_one_at_a_time_on_the_loop_thread(
        e2_real: E2Service, db_clock: OffsetClock, monkeypatch: pytest.MonkeyPatch):
    holders = [make_job("running", Vector(2800, 512)) for _ in range(10)]  # 28,000 of 28,800 millicores
    waiting = [queued(e2_real.queue, Vector(2800, 512)) for _ in range(10)]
    threads: set[int] = set()
    real_cycle = e2_real.run_cycle

    def cycle() -> None:  # every run_cycle() call, the loop's and any other caller's
        threads.add(threading.get_ident())
        real_cycle()

    monkeypatch.setattr(e2_real, "run_cycle", cycle)
    counted = Counted(e2_real)
    loop = AdmissionLoop(counted, db_clock, Config())
    loop.start()
    try:
        db_clock.wait_for_waiters(1)
        barrier = threading.Barrier(10)

        def release_at_once(job_id: int) -> None:
            barrier.wait(10)
            release_job(job_id)

        with ThreadPoolExecutor(10) as pool:
            for future in [pool.submit(release_at_once, holder) for holder in holders]:
                future.result(timeout=30)
        counted.wait_until(lambda: len(e2_real.queue) == 0)
        assert counted.most_at_once == 1
        assert threads == {loop.ident}
        assert [job_state(job) for job in waiting] == ["allocated"] * 10
    finally:
        assert loop.stop(timeout=10)
    assert_invariants()


def race_once(pool: ThreadPoolExecutor, e2: E2Service, watch: psycopg.Connection[tuple[object, ...]]) -> None:
    """One run: a job waits; then a cycle, a second submit and a release start together behind a Barrier."""
    holder = make_job("running", SMALL)
    e2.submit(STUDENT, REQUEST, b"print(1)\n")
    barrier = threading.Barrier(3)

    def at_once(call: Callable[[], object]) -> object:
        barrier.wait(10)
        return call()

    calls: list[Callable[[], object]] = [e2.run_cycle, partial(e2.submit, STUDENT, REQUEST, b"print(2)\n"),
                                         partial(release_job, holder)]
    for future in [pool.submit(at_once, call) for call in calls]:
        future.result(timeout=30)
    e2.run_cycle()  # admit what the race left queued
    queued_rows = {row[0] for row in watch.execute("SELECT job_id FROM jobs WHERE state = 'queued'")}
    assert {e.job_id for e in e2.queue.in_fifo_order()} == queued_rows
    assert watch.execute("SELECT job_id FROM job_state_history WHERE to_state = 'admitted' "
                         "GROUP BY job_id HAVING count(*) > 1").fetchall() == []
    assert watch.execute("SELECT job_id FROM allocations GROUP BY job_id HAVING count(*) > 1").fetchall() == []


def test_a_cycle_a_submit_and_a_release_at_once_100_times(e2_real: E2Service, seeded: TempDb):
    with psycopg.connect(seeded.owner_url, autocommit=True) as watch, ThreadPoolExecutor(3) as pool:
        for _ in range(100):
            race_once(pool, e2_real, watch)
    assert_invariants()
