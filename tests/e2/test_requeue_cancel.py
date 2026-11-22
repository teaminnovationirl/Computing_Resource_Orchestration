"""requeue, cancel_queued, pause and resume, set_policy, rebuild, the resync and queue_view (plan 8.4 and
8.5, D-104 to D-106, D-117, D-118). Real transition() calls write the job rows. StubE3 answers snapshot and
allocate unless the test uses e2_real; OffsetClock's time() starts at the database's now, its monotonic() at 0.
"""
from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar, cast

import psycopg
import pytest

import app.e2_scheduling.service as service
import app.e3_allocation as e3
from app.common.db import tx
from app.common.errors import IllegalTransition, NotFound
from app.common.types import Committed, QueueEntry, Released, Snapshot, SubmitRequest, User, Vector
from app.config import Config
from app.e2_scheduling.policy import Policy
from app.e2_scheduling.service import E2Service
from app.e4_monitoring.state import transition
from tests.helpers import (SMALL, OffsetClock, TempDb, assert_invariants, fail_at_end_of_body, job_history, job_state,
                           logged, lose_commit_ack, make_job, queue_entry, queued)
from tests.stubs import Stubs

STUDENT = User("01FE24BCS901", "student")
REQUEST = SubmitRequest("cpu_bound", 100, 64, 0, 5, source_name="prog.py")
LOST = "server closed the connection unexpectedly"  # a COMMIT whose acknowledgement never came
T = TypeVar("T")


def ids(entries: list[QueueEntry]) -> list[int]:
    return [e.job_id for e in entries]


def free(cpu: int) -> Snapshot:
    """S0's free RAM and GPUs with `cpu` millicores free."""
    return Snapshot(cpu_free=cpu, ram_free=117964, gpu_free={0: 14745, 1: 14745})


def allocate_calls(stubs: Stubs) -> list[object]:
    """The job ids StubE3.allocate() was called with, in order."""
    return [args[0] for name, args in stubs.e3.calls if name == "allocate"]


def admission_events(stubs: Stubs) -> list[dict[str, Any]]:
    return [e for e in stubs.hub.events if e["type"] == "admission"]


def job_events(stubs: Stubs, job_id: int) -> list[tuple[str, str]]:
    return [(e["state"], e["reason"]) for e in stubs.hub.events if e.get("job_id") == job_id]


def retry_count(job_id: int) -> int:
    with tx("E2") as t:
        row = t.execute("SELECT retry_count FROM jobs WHERE job_id = %s", (job_id,)).fetchone()
    assert row is not None, f"no job {job_id}"
    return row[0]


def allocations_of(job_id: int) -> int:
    with tx("E3") as t:
        row = t.execute("SELECT count(*) FROM allocations WHERE job_id = %s", (job_id,)).fetchone()
    assert row is not None
    return row[0]


def arrived(db: TempDb, seconds_ago: dict[int, int]) -> None:
    """Test data: each job's submitted_at moved to that many seconds before the database's now, so arrival
    order can differ from job_id order."""
    with psycopg.connect(db.owner_url, autocommit=True) as conn:
        for job_id, seconds in seconds_ago.items():
            conn.execute("UPDATE jobs SET submitted_at = clock_timestamp() - make_interval(secs => %s) "
                         "WHERE job_id = %s", (seconds, job_id))


def finish(job_id: int) -> None:
    """What E1 and E3 do once a dispatched attempt ends: started, completed after the confirmed stop, then
    release() (plan 9.7)."""
    for state in ("running", "completed"):
        with tx("E1") as t:
            transition(t, job_id, cast(Any, state), "E1", f"test: {state}")
    assert isinstance(e3.release(job_id), Released)


def on_another_thread(fn: Callable[[], T]) -> T:
    with ThreadPoolExecutor(1) as pool:
        return pool.submit(fn).result(timeout=10)


def lock_free_elsewhere(e2: E2Service) -> bool:
    """True when another thread could take E2's lock at once."""
    def probe() -> bool:
        if e2.lock.acquire(blocking=False):
            e2.lock.release()
            return True
        return False
    return on_another_thread(probe)


def resync_elsewhere(e2: E2Service) -> bool:
    """Run the resync on another thread if that thread can take E2's lock at once; True when it ran."""
    def attempt() -> bool:
        if not e2.lock.acquire(blocking=False):
            return False
        try:
            e2._resync_queue()
        finally:
            e2.lock.release()
        return True
    return on_another_thread(attempt)


# requeue (message 11, FR-14)

def test_a_requeue_keeps_submitted_at_and_returns_the_job_to_its_arrival_place(e2_stubbed: E2Service, seeded: TempDb):
    """Catches a requeue that writes or pushes a new submitted_at (the job would queue behind younger jobs,
    invariant 8), skips retry_count or the history reason, or forgets the push, the event or the wake."""
    job = make_job("released", SMALL)
    arrival = queue_entry(job).submitted_at
    younger = [queued(e2_stubbed.queue) for _ in range(2)]
    e2_stubbed.wake.clear()
    assert e2_stubbed.requeue(job) is True
    assert ids(e2_stubbed.queue.in_fifo_order()) == [job, *younger]
    assert e2_stubbed.queue.fifo_head() == queue_entry(job)  # the row's entry: arrival, class and vector
    assert queue_entry(job).submitted_at == arrival
    assert (job_state(job), retry_count(job)) == ("queued", 1)
    assert job_history(job)[-1] == ("released", "queued", "E2", "requeue 1 of 2")
    assert job_events(seeded.stubs, job)[-1] == ("queued", "requeue 1 of 2")
    assert e2_stubbed.wake.is_set()
    assert_invariants()


def test_after_two_requeues_a_third_returns_false_and_changes_nothing(
        e2_real: E2Service, seeded: TempDb, json_log: Path):
    """Catches a third requeue (at most 2 retries, FR-14 and invariant 8; the retry_count CHECK would make
    it a raw error), a refusal that still moves, counts, pushes, publishes or wakes, and a job that will
    never run again logged only at INFO."""
    job = make_job("released", SMALL)
    for _ in range(2):
        assert e2_real.requeue(job) is True
        e2_real.run_cycle()  # the real allocate(), dispatched to the stub E1
        assert job_state(job) == "allocated"
        finish(job)
    history, events = job_history(job), len(seeded.stubs.hub.events)
    e2_real.wake.clear()
    assert e2_real.requeue(job) is False
    assert (job_state(job), retry_count(job), job in e2_real.queue) == ("released", 2, False)
    assert job_history(job) == history
    assert [reason for _, _, _, reason in history if reason.startswith("requeue")] == ["requeue 1 of 2",
                                                                                      "requeue 2 of 2"]
    assert len(seeded.stubs.hub.events) == events
    assert not e2_real.wake.is_set()
    line = logged(json_log, "requeue")[-1]
    assert (line["level"], line["outcome"], line["retry_count"]) == ("WARNING", "retries_used", 2)
    assert_invariants()


def test_a_requeue_whose_commit_fails_pushes_publishes_and_wakes_nothing(
        e2_stubbed: E2Service, seeded: TempDb, monkeypatch: pytest.MonkeyPatch):
    """Catches the push, the job_state event or the wake made inside the transaction instead of after COMMIT
    (invariant 3): the transaction fails at its end and rolls back, so none of them may be seen."""
    job = make_job("released", SMALL)
    events = len(seeded.stubs.hub.events)
    e2_stubbed.wake.clear()
    fail_at_end_of_body(monkeypatch, psycopg.OperationalError(LOST), module=service)
    with pytest.raises(psycopg.OperationalError):
        e2_stubbed.requeue(job)
    assert (job_state(job), retry_count(job), job in e2_stubbed.queue) == ("released", 0, False)
    assert len(seeded.stubs.hub.events) == events
    assert not e2_stubbed.wake.is_set()


def test_a_requeue_of_a_job_that_is_not_released_or_does_not_exist_raises_and_writes_nothing(
        e2_stubbed: E2Service):
    """Catches a requeue that answers False for a caller's mistake (False means only that the retries are
    used up, plan 8.4) or that writes before it is refused."""
    job = queued(e2_stubbed.queue)
    history = job_history(job)
    with pytest.raises(IllegalTransition, match="queued"):
        e2_stubbed.requeue(job)
    with pytest.raises(NotFound):
        e2_stubbed.requeue(999_999)
    assert (job_history(job), retry_count(job), ids(e2_stubbed.queue.in_fifo_order())) == (history, 0, [job])


# cancel_queued (message 11)

def test_a_cancel_of_a_queued_job_writes_cancelled_with_no_allocation_and_takes_it_out_of_the_queue(
        e2_stubbed: E2Service, seeded: TempDb):
    """Plan 8.5. Catches a cancel that leaves the job in the queue (the next cycle would pick a cancelled
    job), writes the wrong move, actor or reason, or allocates or releases anything (nothing was held)."""
    job, other = queued(e2_stubbed.queue), queued(e2_stubbed.queue)
    e2_stubbed.wake.clear()
    assert e2_stubbed.cancel_queued(job, "FAC001") is True
    assert (job_state(job), ids(e2_stubbed.queue.in_fifo_order())) == ("cancelled", [other])
    assert job_history(job)[-1] == ("queued", "cancelled", "E2", "cancelled by FAC001")
    assert job_events(seeded.stubs, job)[-1] == ("cancelled", "cancelled by FAC001")
    assert allocations_of(job) == 0
    assert seeded.stubs.e3.calls == []
    assert e2_stubbed.wake.is_set()  # the next job may fit now that the head is gone
    assert_invariants()


def test_a_cancel_of_a_job_that_is_not_queued_returns_false_and_changes_nothing(
        e2_stubbed: E2Service, seeded: TempDb):
    """Plan 8.5. Catches a cancel that moves a job E2 does not hold: running to cancelled is a legal move,
    but E1's, after a stop (plan 8.4: E4 then tries E1). An unknown id is NotFound, not a quiet False."""
    job = make_job("running", SMALL)
    history, events = job_history(job), len(seeded.stubs.hub.events)
    assert e2_stubbed.cancel_queued(job, "FAC001") is False
    assert (job_state(job), job_history(job), len(seeded.stubs.hub.events)) == ("running", history, events)
    with pytest.raises(NotFound):
        e2_stubbed.cancel_queued(999_999, "FAC001")
    assert_invariants()


def test_a_cancel_of_a_job_queued_in_the_database_but_not_in_memory_cancels_it(e2_stubbed: E2Service):
    """Catches a cancel decided by queue membership: a job whose COMMIT acknowledgement was lost is queued in
    the database but missing from memory until the resync (D-105, D-106), and its owner sees it queued."""
    job = make_job("queued", SMALL)  # committed, never pushed
    assert e2_stubbed.cancel_queued(job, "01FE24BCS901") is True
    assert (job_state(job), job in e2_stubbed.queue) == ("cancelled", False)
    assert_invariants()


def test_a_cancel_of_the_job_in_doubt_says_try_again_and_leaves_it_to_the_next_cycle(
        e2_stubbed: E2Service, seeded: TempDb):
    """Catches a cancel that tries queued to cancelled for the job in doubt, drops it from the queue or the
    slot, or answers False, after which E1's cancel finds nothing to stop and the cancel is lost (D-122): it
    reads admitted (D-117), no engine can cancel it yet, and only allocate() can settle it."""
    job = queued(e2_stubbed.queue)
    seeded.stubs.e3.allocate_results = [psycopg.OperationalError(LOST)]
    with pytest.raises(psycopg.OperationalError):
        e2_stubbed.run_cycle()
    history = job_history(job)
    with pytest.raises(IllegalTransition, match="being admitted"):
        e2_stubbed.cancel_queued(job, "FAC001")
    assert (job_state(job), job_history(job), job in e2_stubbed.queue) == ("admitted", history, True)
    assert e2_stubbed._in_doubt is not None and e2_stubbed._in_doubt.job_id == job
    seeded.stubs.e3.allocate_results = [Committed(1, None)]
    e2_stubbed.run_cycle()
    assert allocate_calls(seeded.stubs) == [job, job]
    assert job not in e2_stubbed.queue


def test_a_cancel_of_a_job_whose_admit_move_lost_its_answer_says_try_again_and_the_next_picks_allocate_it(
        e2_stubbed: E2Service, seeded: TempDb, monkeypatch: pytest.MonkeyPatch):
    """Catches the second job that sits in the queue while the database says admitted: its admit move
    committed but the answer was lost, and E2 learns that only at its next pick. A cancel must leave it."""
    job = queued(e2_stubbed.queue)
    real_move, failures = service._move, [psycopg.OperationalError(LOST)]

    def move(job_id: int, to_state: service.JobState, reason: str) -> None:
        real_move(job_id, to_state, reason)  # commits
        if failures:
            raise failures.pop()

    monkeypatch.setattr(service, "_move", move)
    with pytest.raises(psycopg.OperationalError):
        e2_stubbed.run_cycle()
    assert (job_state(job), e2_stubbed._in_doubt) == ("admitted", None)
    with pytest.raises(IllegalTransition, match="being admitted"):
        e2_stubbed.cancel_queued(job, "FAC001")
    assert (job_state(job), job in e2_stubbed.queue) == ("admitted", True)
    seeded.stubs.e3.allocate_results = [Committed(1, None)]
    e2_stubbed.run_cycle()  # picked: the move is refused and the job reads admitted, so it is in doubt
    e2_stubbed.run_cycle()  # allocate()
    assert allocate_calls(seeded.stubs) == [job]
    assert job not in e2_stubbed.queue


def test_a_cancel_of_the_job_in_doubt_whose_row_reads_queued_cancels_it_and_clears_the_slot(
        e2_stubbed: E2Service, seeded: TempDb):
    """Catches a slot left pointing at a cancelled job (the next cycle would ask allocate() for it) and no event
    saying the halt is over: E3's refusal had committed, admitted back to queued, but its answer was lost."""
    job = queued(e2_stubbed.queue)
    seeded.stubs.e3.allocate_results = [psycopg.OperationalError(LOST)]
    with pytest.raises(psycopg.OperationalError):
        e2_stubbed.run_cycle()
    with tx("E3") as t:  # what E3's refusal wrote before its answer was lost
        transition(t, job, "queued", "E3", "refused: capacity taken first", expected="admitted")
    assert e2_stubbed.cancel_queued(job, "FAC001") is True
    assert (job_state(job), job in e2_stubbed.queue, e2_stubbed._in_doubt) == ("cancelled", False, None)
    assert [e["in_doubt"] for e in admission_events(seeded.stubs)] == [job, None]
    e2_stubbed.run_cycle()
    assert allocate_calls(seeded.stubs) == [job]  # only the call whose answer was lost


def test_a_cancel_whose_commit_acknowledgement_was_lost_is_dropped_by_the_next_resync(
        e2_stubbed: E2Service, seeded: TempDb, db_clock: OffsetClock, json_log: Path,
        monkeypatch: pytest.MonkeyPatch):
    """Catches the mirror of D-105's orphan: the cancel committed but its acknowledgement was lost, so its hooks
    never ran and the cancelled job stayed in memory, where as an FCFS head that does not fit it holds every job
    behind it. The resync drops it (only E2 cancels queued jobs, under its lock); the line says commit_unknown."""
    ghost, behind = queued(e2_stubbed.queue, Vector(2000, 64)), queued(e2_stubbed.queue)
    lose_commit_ack(monkeypatch, service, psycopg.OperationalError(LOST))
    with pytest.raises(psycopg.OperationalError):
        e2_stubbed.cancel_queued(ghost, "FAC001")
    assert (job_state(ghost), ghost in e2_stubbed.queue) == ("cancelled", True)
    seeded.stubs.e3.snapshot_value = free(cpu=1000)  # the ghost does not fit, so it holds the FCFS head
    e2_stubbed.run_cycle()
    assert allocate_calls(seeded.stubs) == []
    db_clock.advance(30)
    seeded.stubs.e3.allocate_results = [Committed(1, None)]
    e2_stubbed.run_cycle()  # the resync drops it, then the job behind it is admitted
    assert (ghost in e2_stubbed.queue, allocate_calls(seeded.stubs)) == (False, [behind])
    [line] = [line for line in logged(json_log, "queue_resync") if line["level"] == "WARNING"]
    assert (line["pushed"], line["dropped"]) == ([], [ghost])
    [cancel] = logged(json_log, "cancel_queued")
    assert (cancel["level"], cancel["outcome"], cancel["error"]) == ("ERROR", "commit_unknown", f"OperationalError: {LOST}")


# pause, resume, policy (message 11)

def test_pause_stops_admission_until_resume_which_wakes_the_loop(e2_stubbed: E2Service, seeded: TempDb):
    """Plan 8.5's "pause stops admission" (moved here by D-118). Catches a pause the next cycle ignores, a
    resume that does not wake the loop (the job would wait up to a period), and missing or misordered events."""
    job = queued(e2_stubbed.queue)
    e2_stubbed.pause_admission("host unhealthy")
    e2_stubbed.run_cycle()
    assert (allocate_calls(seeded.stubs), job in e2_stubbed.queue) == ([], True)
    e2_stubbed.wake.clear()
    e2_stubbed.resume_admission()
    assert e2_stubbed.wake.is_set()
    seeded.stubs.e3.allocate_results = [Committed(1, None)]
    e2_stubbed.run_cycle()
    assert allocate_calls(seeded.stubs) == [job]
    events = admission_events(seeded.stubs)
    assert [(e["paused"], e["reason"], e["policy"], e["in_doubt"]) for e in events] == [
        (True, "host unhealthy", "fcfs", None), (False, None, "fcfs", None)]
    assert all(datetime.fromisoformat(e["at"]).utcoffset() is not None for e in events)


def test_set_policy_switches_the_next_pick_and_an_unknown_name_changes_nothing(
        e2_stubbed: E2Service, seeded: TempDb):
    """Catches a policy kept only for display (the cycle would still pick FCFS), a missing event or wake, and
    an unknown name that falls back silently or half-applies."""
    low, high = queued(e2_stubbed.queue, need_class=3), queued(e2_stubbed.queue, need_class=1)
    e2_stubbed.wake.clear()
    with pytest.raises(ValueError, match="lifo"):
        e2_stubbed.set_policy(cast(Policy, "lifo"))
    assert (ids(e2_stubbed.queue_view()), admission_events(seeded.stubs), e2_stubbed.wake.is_set()) == (
        [low, high], [], False)
    e2_stubbed.set_policy("need")
    assert e2_stubbed.wake.is_set()
    seeded.stubs.e3.allocate_results = [Committed(1, None), Committed(2, None)]
    e2_stubbed.run_cycle()
    assert allocate_calls(seeded.stubs) == [high, low]
    assert [e["policy"] for e in admission_events(seeded.stubs)] == ["need"]


def test_a_failed_admission_event_is_sent_again_logged_once_and_never_hides_a_cycles_error(
        e2_stubbed: E2Service, seeded: TempDb, json_log: Path, monkeypatch: pytest.MonkeyPatch):
    """Catches an event push that raises out of pause_admission() (the pause stands, and the admin would
    retry a change already made) or out of run_cycle()'s finally, where it would replace the cycle's error;
    a failed event counted as sent (no page could learn the state: nothing else serves it); and a log line
    per cycle while the hub is down."""
    real_publish = seeded.stubs.e4.publish

    def down(event: dict[str, Any]) -> None:
        raise RuntimeError("hub down")

    monkeypatch.setattr(seeded.stubs.e4, "publish", down)
    job = queued(e2_stubbed.queue)
    e2_stubbed.pause_admission("maintenance")
    e2_stubbed.run_cycle()  # paused; the same event fails again and is not logged again
    assert allocate_calls(seeded.stubs) == []
    monkeypatch.setattr(seeded.stubs.e4, "publish", real_publish)
    e2_stubbed.run_cycle()  # the hub is back: the cycle's end sends the state the pages missed
    assert [(e["paused"], e["reason"]) for e in admission_events(seeded.stubs)] == [(True, "maintenance")]
    monkeypatch.setattr(seeded.stubs.e4, "publish", down)
    e2_stubbed.resume_admission()
    seeded.stubs.e3.allocate_results = [psycopg.OperationalError(LOST)]
    with pytest.raises(psycopg.OperationalError):
        e2_stubbed.run_cycle()  # the job goes in doubt; its admission event fails too
    assert e2_stubbed._in_doubt is not None and e2_stubbed._in_doubt.job_id == job
    lines = logged(json_log, "admission_event_failed")
    assert [(line["level"], line["paused"], line["in_doubt"]) for line in lines] == [
        ("ERROR", True, None), ("ERROR", False, None), ("ERROR", False, job)]
    assert all(line["error"] == "RuntimeError: hub down" and line["traceback"] for line in lines)


# rebuild and the resync (D-105, D-106)

def test_rebuild_restores_the_exact_order_and_pushes_in_arrival_order(
        seeded: TempDb, tmp_path: Path, db_clock: OffsetClock, monkeypatch: pytest.MonkeyPatch):
    """Plan 8.5. Catches a rebuild that misses a queued row, takes one that is not queued, or pushes by job_id:
    JobQueue sorts on push anyway, so the push order itself is checked (each push an O(1) append, SUMMARY);
    a second run, the resync's code, pushes nothing again."""
    a, b, c, d = (make_job("queued", SMALL, need_class=n) for n in (3, 1, 3, 2))
    make_job("running", SMALL)
    make_job("cancelled")
    arrived(seeded, {d: 40, b: 30, a: 20, c: 10})  # arrival order d, b, a, c
    e2 = E2Service(Config(data_dir=tmp_path), clock=db_clock)
    pushed: list[int] = []
    real_push = e2.queue.push

    def push(entry: QueueEntry) -> None:
        pushed.append(entry.job_id)
        real_push(entry)

    monkeypatch.setattr(e2.queue, "push", push)
    e2.wake.clear()
    assert e2.rebuild() == 4
    assert pushed == [d, b, a, c]
    assert e2.queue.in_fifo_order() == [queue_entry(job) for job in (d, b, a, c)]
    assert ids(e2.queue.in_need_order()) == [b, d, a, c]
    assert e2.wake.is_set()
    assert e2.rebuild() == 0
    assert pushed == [d, b, a, c]


def test_the_resync_pushes_a_job_missing_from_the_queue_once_every_30_s(
        e2_stubbed: E2Service, seeded: TempDb, db_clock: OffsetClock, json_log: Path):
    """D-106. Catches an orphan (its push lost with its COMMIT acknowledgement) that waits for the next start,
    a resync on every cycle (a read of every queued row twice a second) or one whose timer never restarts,
    and a resync that pushes a job twice (the push would raise)."""
    orphan = make_job("queued", SMALL)
    db_clock.advance(29)
    e2_stubbed.run_cycle()
    assert (orphan in e2_stubbed.queue, allocate_calls(seeded.stubs)) == (False, [])
    db_clock.advance(1)
    seeded.stubs.e3.allocate_results = [Committed(1, None)]
    e2_stubbed.run_cycle()  # 30 s: pushed by the resync, admitted in the same cycle
    assert allocate_calls(seeded.stubs) == [orphan]

    second = make_job("queued", SMALL)
    db_clock.advance(29)
    e2_stubbed.run_cycle()  # 59 s: the timer restarted at 30 s
    assert second not in e2_stubbed.queue
    seeded.stubs.e3.snapshot_value = free(cpu=50)  # nothing fits from here on, so it stays queued
    db_clock.advance(1)
    e2_stubbed.run_cycle()  # 60 s
    db_clock.advance(30)
    e2_stubbed.run_cycle()  # 90 s: due again, and the job already queued is not pushed again
    assert ids(e2_stubbed.queue.in_fifo_order()) == [second]
    lines = [line for line in logged(json_log, "queue_resync") if line["level"] == "WARNING"]
    assert [(line["pushed"], line["dropped"]) for line in lines] == [([orphan], []), ([second], [])]


def test_the_resync_runs_while_admission_is_paused_so_the_view_shows_an_orphan(
        e2_stubbed: E2Service, seeded: TempDb, db_clock: OffsetClock):
    """Catches a resync skipped while paused: an orphan would be missing from the queue view for the whole
    pause (it is not admission, so the pause does not stop it)."""
    e2_stubbed.pause_admission("host unhealthy")
    orphan = make_job("queued", SMALL)
    db_clock.advance(30)
    e2_stubbed.run_cycle()
    assert (ids(e2_stubbed.queue_view()), allocate_calls(seeded.stubs)) == ([orphan], [])


def test_a_requeue_from_inside_an_admission_cycle_on_its_thread_is_refused_loudly(
        e2_stubbed: E2Service, seeded: TempDb, monkeypatch: pytest.MonkeyPatch):
    """Catches the silent re-entry of E2's RLock (D-104): a dispatch hook that requeued synchronously inside the
    cycle would push a job the cycle still holds (a ValueError in a hook) and the cycle would then drop it."""
    job, released = queued(e2_stubbed.queue), make_job("released", SMALL)
    refused: list[str] = []

    def allocate(job_id: int, vector: Vector) -> Committed:
        try:
            e2_stubbed.requeue(released)
        except RuntimeError as exc:
            refused.append(str(exc))
        return Committed(1, None)

    monkeypatch.setattr(seeded.stubs.e3, "allocate", allocate)
    e2_stubbed.run_cycle()
    assert len(refused) == 1 and "inside an admission cycle" in refused[0]
    assert (job_state(released), retry_count(released), job in e2_stubbed.queue) == ("released", 0, False)
    assert e2_stubbed.requeue(released) is True  # after the cycle the same call works


def test_no_resync_can_run_between_a_submits_commit_and_its_push(
        e2_stubbed: E2Service, json_log: Path, monkeypatch: pytest.MonkeyPatch, spool: Callable[[bytes], Path]):
    """Catches the race of D-106: submit()'s first hook after COMMIT runs with E2's lock still held, so a resync
    (or a cancel, or a cycle) tried there cannot push the job first, which would make submit's own push raise
    ValueError. Also catches the lock taken before validate() or before the move of the spooled upload and the
    write of input.txt: a large upload would hold every submit and the admission loop (it is taken only around
    the insert, D-125). The submit line tells lock waits from database time."""
    tried: list[tuple[str, bool]] = []
    real_validate, real_log = service.validate, service._log_submit
    real_write = E2Service._write_payload  # pyright: ignore[reportPrivateUsage]

    def validate(*args: Any) -> Any:
        tried.append(("validate", lock_free_elsewhere(e2_stubbed)))
        return real_validate(*args)

    def write_payload(self: E2Service, *args: Any) -> Any:
        tried.append(("move", lock_free_elsewhere(e2_stubbed)))
        return real_write(self, *args)

    def log_submit(*args: Any) -> None:
        tried.append(("committed", resync_elsewhere(e2_stubbed)))
        real_log(*args)

    monkeypatch.setattr(service, "validate", validate)
    monkeypatch.setattr(E2Service, "_write_payload", write_payload)
    monkeypatch.setattr(service, "_log_submit", log_submit)
    result = e2_stubbed.submit(STUDENT, REQUEST, spool(b"print(1)\n"))
    assert tried == [("validate", True), ("move", True), ("committed", False)]
    assert ids(e2_stubbed.queue.in_fifo_order()) == [result.job_id]
    assert logged(json_log, "after_commit_failed") == []
    [line] = logged(json_log, "submit")
    assert 0 <= line["waited_ms"] and 0 <= line["held_ms"] <= line["ms"]


# queue_view

def test_queue_view_follows_the_active_policy_and_its_60_s_rule(e2_stubbed: E2Service, seeded: TempDb):
    """Catches a view in another order than the cycle admits by: arrival order under fcfs; under need, the jobs
    that have waited 60 s first, in arrival order (none may be overtaken), then need order."""
    a, d, b, c = (make_job("queued", SMALL, need_class=n) for n in (3, 2, 3, 1))
    arrived(seeded, {a: 70, d: 65, b: 20, c: 10})
    for job in (c, b, d, a):
        e2_stubbed.queue.push(queue_entry(job))
    assert ids(e2_stubbed.queue_view()) == [a, d, b, c]
    e2_stubbed.set_policy("need")
    assert ids(e2_stubbed.queue_view()) == [a, d, c, b]
    assert e2_stubbed.queue_view()[0] == queue_entry(a)


def test_queue_view_shows_the_job_in_doubt_first_and_doubt_publishes_when_it_starts_and_settles(
        seeded: TempDb, tmp_path: Path, db_clock: OffsetClock):
    """Catches a view that hides or misplaces the job admission is halted behind (it is retried before any
    other, D-117), and a halt the admin cannot see: an admission event when the doubt starts and when it ends."""
    e2 = E2Service(Config(data_dir=tmp_path, default_policy="need"), clock=db_clock)
    big = queued(e2.queue, Vector(2000, 64), need_class=1)
    small = queued(e2.queue, SMALL, need_class=3)
    seeded.stubs.e3.snapshot_value = free(cpu=1000)  # only small fits, so first fit picks it
    seeded.stubs.e3.allocate_results = [psycopg.OperationalError(LOST)]
    with pytest.raises(psycopg.OperationalError):
        e2.run_cycle()
    assert ids(e2.queue_view()) == [small, big]
    assert [e["in_doubt"] for e in admission_events(seeded.stubs)] == [small]
    seeded.stubs.e3.allocate_results = [Committed(1, None)]
    e2.run_cycle()  # allocate() settles small; big still does not fit
    assert ids(e2.queue_view()) == [big]
    assert [(e["in_doubt"], e["policy"]) for e in admission_events(seeded.stubs)] == [(small, "need"), (None, "need")]
