"""transition(), the only way to change jobs.state (plan 7, context §9; invariants 3 and 5).

Every test runs on its own copy of orch_template (fixture db: a pool of 1 to 4 as orch_app) with
the stub engines installed. Each docstring names the bug the test catches. The 13 legal pairs and
their engines come from tests.helpers.ACTOR, written from context §9 and plan 7.2, never from the
code under test.
"""
from __future__ import annotations

import json
import queue
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import psycopg
import pytest

from app.common.db import Engine, Tx, close_pool, init_pool, tx
from app.common.errors import IllegalTransition, NotFound
from app.common.types import Vector
from app.e4_monitoring.state import JobState, transition
from tests.helpers import ACTOR, ALLOWED, STATES, TempDb, assert_invariants, invariant_violations, make_job

RACES = 100
SMALL = Vector(100, 64)  # allocated and running jobs hold a real allocation: 100 of these fit S0 (D-73)
LEGAL = sorted(ALLOWED)
ILLEGAL = [(old, new) for old in STATES for new in STATES if (old, new) not in ALLOWED]
assert len(LEGAL) == 13 and len(ILLEGAL) == 51, "context §9 allows 13 of the 64 ordered pairs"
NO_SUCH_JOB = 999_999_999  # job ids start at 1042 (plan 4.3) and every test has a fresh database


def history(job_id: int) -> list[tuple[Any, ...]]:
    """The job's history rows in order, as (from_state, to_state, actor, reason)."""
    with tx("E4") as cur:
        return cur.execute("SELECT from_state, to_state, actor, reason FROM job_state_history "
                           "WHERE job_id = %s ORDER BY event_id", (job_id,)).fetchall()


def state_of(job_id: int) -> str:
    with tx("E4") as cur:
        row = cur.execute("SELECT state FROM jobs WHERE job_id = %s", (job_id,)).fetchone()
    assert row is not None
    return row[0]


def owner_of(job_id: int) -> str:
    with tx("E4") as cur:
        row = cur.execute("SELECT user_id FROM jobs WHERE job_id = %s", (job_id,)).fetchone()
    assert row is not None
    return row[0]


# --- the 64 ordered pairs -------------------------------------------------------------------

@pytest.mark.parametrize(("from_state", "to_state"), LEGAL)
def test_a_legal_move_writes_exactly_one_history_row(db: TempDb, from_state: JobState, to_state: JobState):
    """Catches an UPDATE that misses the job, settings the trigger cannot see (JT002) and a second
    writer of history next to the trigger: each of the 13 moves, run in the engine that makes it
    (plan 7.2), adds exactly one row carrying the arguments as passed. A bare admitted to allocated
    move skips allocate(), so the invariant query must report exactly that job's missing allocation."""
    job = make_job(from_state)
    before = history(job)
    engine = ACTOR[(from_state, to_state)]
    reason = f"test {from_state} to {to_state}"
    with tx(engine) as cur:
        transition(cur, job, to_state, engine, reason)
    assert history(job) == before + [(from_state, to_state, engine, reason)]
    assert state_of(job) == to_state
    bare = (from_state, to_state) == ("admitted", "allocated")
    assert invariant_violations() == ([("invariants", "missing_allocation", str(job), "allocated")] if bare else [])


@pytest.mark.parametrize(("from_state", "to_state"), ILLEGAL)
def test_an_illegal_move_raises_and_leaves_the_transaction_usable(db: TempDb, from_state: JobState,
                                                                  to_state: JobState):
    """Catches an UPDATE without a savepoint (the caller's transaction would fail and tx() would
    refuse to commit, D-48), a JT001 that reaches the caller as a raw database error, and a refused
    move that still changes the state, writes history or publishes an event."""
    job = make_job(from_state)
    before = history(job)
    published = len(db.stubs.hub.events)
    with tx("E4") as cur:
        with pytest.raises(IllegalTransition, match=f"{from_state} to {to_state}"):
            transition(cur, job, to_state, "E4", "refused")
        row = cur.execute("SELECT state, (SELECT count(*) FROM job_state_history WHERE job_id = %s) "
                          "FROM jobs WHERE job_id = %s", (job, job)).fetchone()
        assert row == (from_state, len(before))
    assert state_of(job) == from_state and history(job) == before
    assert len(db.stubs.hub.events) == published
    assert_invariants()


# --- app.actor and app.reason -----------------------------------------------------------------

EXITS: dict[str, tuple[Callable[[Tx, int], object], type[Exception] | None]] = {
    "ok": (lambda cur, job: transition(cur, job, "admitted", "E2", "admitted"), None),
    "illegal": (lambda cur, job: transition(cur, job, "running", "E2", "too early"), IllegalTransition),
    "missing": (lambda cur, job: transition(cur, NO_SUCH_JOB, "admitted", "E2", "no such job"), NotFound),
    "missing, expected": (lambda cur, job: transition(cur, NO_SUCH_JOB, "admitted", "E2", "no such job",
                                                      expected="queued"), NotFound),
    "moved": (lambda cur, job: transition(cur, job, "admitted", "E2", "stale", expected="released"),
              IllegalTransition),
}


@pytest.mark.parametrize("exit_name", EXITS)
def test_state_settings_are_empty_after_every_exit(db: TempDb, exit_name: str):
    """Catches a clear_actor skipped on one exit (plan 7.1 raised NotFound before clearing): a raw
    write of jobs.state later in the same transaction would pass the JT002 guard with the values of
    transition(), or with values the caller had set before it. Every exit leaves the transaction
    usable, so it commits afterwards."""
    call, raised = EXITS[exit_name]
    job = make_job("queued")
    with tx("E2") as cur:
        cur.execute("SELECT set_config('app.actor', 'stale', true), set_config('app.reason', 'stale', true)")
        if raised is None:
            call(cur, job)
        else:
            with pytest.raises(raised):
                call(cur, job)
        settings = cur.execute("SELECT current_setting('app.actor', true), "
                               "current_setting('app.reason', true)").fetchone()
        assert settings == ("", "")
        with pytest.raises(psycopg.Error) as refused:
            with cur.savepoint():
                cur.execute("UPDATE jobs SET state = 'cancelled' WHERE job_id = %s", (job,))
        assert refused.value.sqlstate == "JT002"


def test_state_settings_do_not_outlive_the_transaction(db: TempDb):
    """Catches set_config(..., false): the values would stay on the pooled connection after COMMIT,
    and the next transaction there could write jobs.state past the JT002 guard. A pool of one, so
    the check reads the connection transition() used."""
    close_pool()
    init_pool(db.app_url, min_size=1, max_size=1)
    job = make_job("admitted")
    with tx("E3") as cur:
        transition(cur, job, "queued", "E3", "refused: capacity taken first")
    with tx("E4") as cur:
        leftover = cur.execute("SELECT nullif(current_setting('app.actor', true), ''), "
                               "nullif(current_setting('app.reason', true), '')").fetchone()
    assert leftover == (None, None)
    assert_invariants()


# --- the job_state event (message 12) ---------------------------------------------------------

def test_one_event_is_published_after_commit_with_the_database_values(db: TempDb):
    """Catches a publish inside the transaction (invariant 3; the stub hub refuses one), an event
    that leaves out user_id, which the hub filters on, or carries the wrong state or time, and an
    extra key that replaces a core one."""
    job = make_job("running")
    owner = owner_of(job)
    published = len(db.stubs.hub.events)
    with tx("E1") as cur:
        at = transition(cur, job, "completed", "E1", "within limits",
                        {"verdict": "AC", "user_id": "intruder", "state": "failed"})
        assert len(db.stubs.hub.events) == published  # nothing leaves before COMMIT
    assert db.stubs.hub.events[published:] == [
        {"verdict": "AC", "type": "job_state", "job_id": job, "user_id": owner, "state": "completed",
         "reason": "within limits", "at": at.isoformat()}]
    with tx("E4") as cur:
        row = cur.execute("SELECT at FROM job_state_history WHERE job_id = %s "
                          "ORDER BY event_id DESC LIMIT 1", (job,)).fetchone()
    assert row is not None and at <= row[0]  # the history row is written a moment later (plan 7.1)


def test_a_failed_publish_is_logged_with_its_job(db: TempDb, json_log: Path, monkeypatch: pytest.MonkeyPatch):
    """Catches a hook that hides its job: when the push fails after COMMIT, tx() logs
    after_commit_failed with the hook's name (D-47), and an anonymous lambda would leave the
    on-call unable to tell whose live update was lost (E4-1 incident review)."""
    job = make_job("running")

    def hub_down(event: dict[str, Any]) -> None:
        raise ConnectionError("hub down")

    monkeypatch.setattr(db.stubs.e4, "publish", hub_down)
    with tx("E1") as cur:
        transition(cur, job, "completed", "E1", "within limits")
    records = [json.loads(line) for line in json_log.read_text(encoding="utf-8").splitlines()]
    failed = [r for r in records if r["event"] == "after_commit_failed"]
    assert len(failed) == 1 and f"'job_id': {job}" in failed[0]["hook"], failed
    assert state_of(job) == "completed"  # the commit stands (D-47)


def test_no_event_is_published_after_a_rollback(db: TempDb):
    """Catches an event for a move that was rolled back: transition() leaves the push to tx()'s
    after_commit hooks, which a full rollback drops. This guards the pairing of the two; a publish
    inside the transaction is caught by test_one_event_is_published_after_commit_with_the_database_values."""
    class Abort(Exception):
        pass

    job = make_job("queued")
    before = history(job)
    published = len(db.stubs.hub.events)
    with pytest.raises(Abort):
        with tx("E2") as cur:
            transition(cur, job, "admitted", "E2", "admitted, then rolled back")
            raise Abort
    assert len(db.stubs.hub.events) == published
    assert state_of(job) == "queued" and history(job) == before


SAVEPOINT_OUTCOMES: dict[str, list[str]] = {  # outcome of the caller's savepoint -> states published
    "released": ["admitted"],
    "rolled back": [],
    "inner released, outer rolled back": [],
}


@pytest.mark.parametrize("outcome", SAVEPOINT_OUTCOMES)
def test_the_event_follows_the_callers_savepoint(db: TempDb, outcome: str):
    """Catches an event for a move that a caller's savepoint undid (E4-1 review, D-67): the outer
    transaction commits either way, yet the pages may show the move only if every savepoint around
    it was released, and then exactly once."""
    class Undo(Exception):
        pass

    job = make_job("queued")
    before = history(job)
    published = len(db.stubs.hub.events)
    with tx("E2") as cur:
        try:
            with cur.savepoint():
                if outcome == "inner released, outer rolled back":
                    with cur.savepoint():
                        transition(cur, job, "admitted", "E2", "admitted")
                else:
                    transition(cur, job, "admitted", "E2", "admitted")
                if outcome != "released":
                    raise Undo
        except Undo:
            pass
    assert [event["state"] for event in db.stubs.hub.events[published:]] == SAVEPOINT_OUTCOMES[outcome]
    if outcome == "released":
        assert state_of(job) == "admitted" and len(history(job)) == len(before) + 1
    else:
        assert state_of(job) == "queued" and history(job) == before


# --- the error paths --------------------------------------------------------------------------

def test_an_empty_actor_stays_a_database_error(db: TempDb):
    """Catches an except that turns every database error into IllegalTransition: an empty actor is
    a programming error, which the trigger reports as JT002, and no caller may take it for a
    refused move."""
    job = make_job("queued")
    with pytest.raises(psycopg.Error) as failed:
        with tx("E2") as cur:
            transition(cur, job, "admitted", "", "no actor")  # pyright: ignore[reportArgumentType]  # on purpose
    assert failed.value.sqlstate == "JT002"
    assert state_of(job) == "queued"


REJECTIONS: dict[str, tuple[Callable[[Tx, int], object], JobState]] = {
    "illegal": (lambda cur, job: transition(cur, job, "running", "E4", "too early"), "running"),
    "moved": (lambda cur, job: transition(cur, job, "admitted", "E4", "stale", expected="released"),
              "admitted"),
}


@pytest.mark.parametrize("rejection", REJECTIONS)
def test_every_rejected_move_is_logged(db: TempDb, json_log: Path, rejection: str):
    """Catches a refusal that leaves no log line: a rejected transition must be logged (context §7,
    plan 5.5), both when the trigger refuses it and when the expected state is stale."""
    call, to_state = REJECTIONS[rejection]
    job = make_job("queued")
    with tx("E4") as cur:
        with pytest.raises(IllegalTransition):
            call(cur, job)
    records = [json.loads(line) for line in json_log.read_text(encoding="utf-8").splitlines()]
    refused = [(r["level"], r["job_id"], r["to"], r["actor"]) for r in records if r["event"] == "illegal_transition"]
    assert refused == [("WARNING", job, to_state, "E4")]


def test_transition_refuses_to_run_outside_the_callers_transaction(db: TempDb):
    """Catches a missing guard: after a COMMIT sent by hand, psycopg's transaction() would BEGIN a
    transaction of its own (as orch_app, without the engine role) and COMMIT it, outside the
    caller's tx() (D-48)."""
    job = make_job("queued")
    before = history(job)
    with pytest.raises(RuntimeError, match="ended inside the block"):
        with tx("E2") as cur:
            cur.execute("COMMIT")
            with pytest.raises(RuntimeError, match="open transaction"):
                transition(cur, job, "admitted", "E2", "after a hand-sent COMMIT")
    assert state_of(job) == "queued" and history(job) == before


def test_transition_refuses_a_finished_tx_whose_connection_serves_another(db: TempDb):
    """Catches the guard reading the pooled connection instead of the caller's Tx (Q1-06): once tx() has
    ended its connection goes back to the pool, and while another tx() holds it the status reads open, so
    a Tx kept past its with block (or used in a hook) moved the job inside that other transaction, as its
    role, and emptied its app.actor and app.reason (D-135)."""
    job = make_job("queued")
    before = history(job)
    close_pool()
    init_pool(db.app_url, min_size=1, max_size=1)  # one connection: the next tx() gets the finished one's
    with tx("E2") as finished:
        pass
    with tx("E4"):
        with pytest.raises(RuntimeError, match="open transaction"):
            transition(finished, job, "admitted", "E2", "a finished Tx")
    assert state_of(job) == "queued" and history(job) == before


def test_a_caller_can_move_each_row_of_its_own_query(db: TempDb):
    """Catches transition() running its statements on the caller's cursor: a sweep that iterates
    its own SELECT and moves each job (E4's liveness sweep, startup reconciliation) would read the
    results of transition()'s statements as its next rows (E4-1 incident review)."""
    jobs = [make_job("running") for _ in range(3)]
    with tx("E4") as cur:
        for (job,) in cur.execute("SELECT job_id FROM jobs WHERE state = 'running' ORDER BY job_id"):
            transition(cur, job, "failed", "E4", "worker lost")
    assert [state_of(job) for job in jobs] == ["failed", "failed", "failed"]
    assert_invariants()


# --- expected (change 1 of the E4-1 review) ---------------------------------------------------

def test_expected_state_refuses_a_job_that_has_moved(db: TempDb):
    """Catches an expected state that is ignored: E4 decided to cancel while the job was allocated,
    E1 has started it since, and running to cancelled is legal, so the job would be cancelled
    without the stop and confirm that context §9 requires."""
    job = make_job("running")
    before = history(job)
    published = len(db.stubs.hub.events)
    with tx("E4") as cur:
        with pytest.raises(IllegalTransition) as refused:
            transition(cur, job, "cancelled", "E4", "cancel before start", expected="allocated")
        assert "running" in str(refused.value) and "allocated" in str(refused.value)
        row = cur.execute("SELECT state FROM jobs WHERE job_id = %s", (job,)).fetchone()
        assert row == ("running",)
    assert history(job) == before and len(db.stubs.hub.events) == published
    assert_invariants()


# --- concurrency: separate connections that commit, a Barrier, 100 runs -------------------------

def race(pool: ThreadPoolExecutor, job: int, moves: list[tuple[Engine, JobState]],
         expected: JobState | None = None) -> list[str]:
    """Run the moves at once, each in its own transaction that commits, released together by one
    Barrier. Returns each move's to_state when it won, or "refused" when it raised
    IllegalTransition; a refused move runs one more statement before its COMMIT."""
    barrier = threading.Barrier(len(moves))

    def move(engine: Engine, to_state: JobState) -> str:
        with tx(engine) as cur:
            barrier.wait(timeout=10)  # every transaction is open here
            try:
                transition(cur, job, to_state, engine, "race", expected=expected)
            except IllegalTransition:
                cur.execute("SELECT 1")
                return "refused"
        return to_state

    runs = [pool.submit(move, engine, to_state) for engine, to_state in moves]
    return [run.result(timeout=30) for run in runs]


def test_two_conflicting_moves_at_once_exactly_one_wins(db: TempDb):
    """Catches a lost update and a loser left with a failed transaction: E1 ends a job within its
    limits while E4 marks its worker lost (plan 7.2). One move wins; the other raises
    IllegalTransition and still commits; history gains exactly one row, from running."""
    actor = {"completed": "E1", "failed": "E4"}
    with ThreadPoolExecutor(max_workers=2) as pool:
        for _ in range(RACES):
            job = make_job("running", SMALL)
            before = history(job)
            outcomes = race(pool, job, [("E1", "completed"), ("E4", "failed")])
            assert sorted(outcomes) in (["completed", "refused"], ["failed", "refused"])
            winner = outcomes[0] if outcomes[0] != "refused" else outcomes[1]
            assert state_of(job) == winner
            assert history(job)[len(before):] == [("running", winner, actor[winner], "race")]
    assert_invariants()


def test_start_and_cancel_at_once_with_an_expected_state_exactly_one_wins(db: TempDb):
    """Catches an expected state that is ignored under a race: E1 starts the job while E4 cancels
    it before start (plan 7.2), both decided on allocated. Exactly one wins, and history never
    shows running to cancelled, which would cancel a running job without stop and confirm."""
    actor = {"running": "E1", "cancelled": "E4"}
    with ThreadPoolExecutor(max_workers=2) as pool:
        for _ in range(RACES):
            job = make_job("allocated", SMALL)
            before = history(job)
            outcomes = race(pool, job, [("E1", "running"), ("E4", "cancelled")], expected="allocated")
            assert sorted(outcomes) in (["refused", "running"], ["cancelled", "refused"])
            winner = outcomes[0] if outcomes[0] != "refused" else outcomes[1]
            assert history(job)[len(before):] == [("allocated", winner, actor[winner], "race")]
    assert_invariants()


def waiting(cur: Tx, pid: int) -> bool:
    """True once the server process pid waits on a lock another session holds."""
    row = cur.execute("SELECT cardinality(pg_blocking_pids(%s)) > 0", (pid,)).fetchone()
    assert row is not None
    return bool(row[0])


def test_the_loser_waits_for_the_winner_then_raises(db: TempDb):
    """Catches a waiting loser left with a failed transaction, for example by a check-first read
    without a row lock in place of the savepoint: the move would read running, pass its own check,
    then fail inside the trigger. The winner commits only once pg_blocking_pids shows the loser
    waiting on it, so that path runs every time. A Python copy of the 13 pairs kept beside the
    savepoint would still pass here; invariant 5 and D-61 forbid it, and only review catches it."""
    job = make_job("running")
    before = history(job)
    pids: queue.Queue[int] = queue.Queue()

    def lose() -> str:
        with tx("E4") as cur:
            row = cur.execute("SELECT pg_backend_pid()").fetchone()
            assert row is not None
            pids.put(row[0])
            try:
                transition(cur, job, "failed", "E4", "worker lost")
            except IllegalTransition:
                cur.execute("SELECT 1")
                return "refused"
        return "failed"

    with ThreadPoolExecutor(max_workers=1) as pool:
        with tx("E1") as cur:
            transition(cur, job, "completed", "E1", "within limits")  # holds the row lock until COMMIT
            loser = pool.submit(lose)
            pid = pids.get(timeout=10)
            deadline = time.monotonic() + 10
            while not waiting(cur, pid):
                assert time.monotonic() < deadline, "the second move never waited on the row lock"
        assert loser.result(timeout=30) == "refused"
    assert state_of(job) == "completed"
    assert history(job)[len(before):] == [("running", "completed", "E1", "within limits")]


def test_a_lost_connection_reaches_the_caller_unchanged(db: TempDb):
    """Catches clean-up that hides the real error: when the session dies inside transition()
    (here pg_terminate_backend while the move waits on the row lock), the caller must see that
    error (SQLSTATE 57P01), not "the connection is closed" from clearing the settings afterwards
    (E4-1 review)."""
    job = make_job("running")
    pids: queue.Queue[int] = queue.Queue()

    def killed() -> psycopg.Error | None:
        try:
            with tx("E4") as cur:
                row = cur.execute("SELECT pg_backend_pid()").fetchone()
                assert row is not None
                pids.put(row[0])
                transition(cur, job, "failed", "E4", "worker lost")
        except psycopg.Error as e:
            return e
        return None

    with ThreadPoolExecutor(max_workers=1) as pool:
        with tx("E1") as cur:
            transition(cur, job, "completed", "E1", "within limits")  # holds the row lock until COMMIT
            victim = pool.submit(killed)
            pid = pids.get(timeout=10)
            deadline = time.monotonic() + 10
            while not waiting(cur, pid):
                assert time.monotonic() < deadline, "the second move never waited on the row lock"
            with psycopg.connect(db.owner_url, autocommit=True) as owner:
                owner.execute("SELECT pg_terminate_backend(%s)", (pid,))
        error = victim.result(timeout=30)
    assert error is not None and error.sqlstate == "57P01", repr(error)
    assert state_of(job) == "completed"
