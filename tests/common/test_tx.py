"""tx(), after-commit hooks, retrying() and the pool (plan 5.1; the six checks of plan 5.6).

Every database test runs on its own copy of orch_template (fixture db: a pool of 1 to 4 as
orch_app). Each docstring names the bug the test catches.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, LiteralString

import psycopg
import pytest
from psycopg import errors, sql
from psycopg.conninfo import make_conninfo

import app.common.db as common_db
from app.common.db import Engine, assert_outside_tx, close_pool, in_transaction, init_pool, retrying, tx
from tests.helpers import TempDb, assert_invariants

RACES = 100
NEW_TYPE = "INSERT INTO workload_types (type_code, default_need_class, max_time_limit_s) VALUES (%s, 3, 600)"
COUNT_TYPE = "SELECT count(*) FROM workload_types WHERE type_code = %s"
NEW_GPU = "INSERT INTO resources (kind, gpu_index, total, reserved) VALUES ('gpu', %s, 1000, 100) RETURNING resource_id"
BUMP = "UPDATE resources SET total = total + 10, reserved = reserved + 1 WHERE resource_id = %s"


def _committed(url: str, type_code: str) -> int:
    """How many workload_types rows named type_code a connection of its own sees."""
    with psycopg.connect(url) as conn:
        row = conn.execute(COUNT_TYPE, (type_code,)).fetchone()
    assert row is not None
    return row[0]


def _reopen(db: TempDb, size: int) -> None:
    """Replace the fixture's pool with one of exactly size connections."""
    close_pool()
    init_pool(db.app_url, min_size=size, max_size=size)


def _pool_idle() -> bool:
    """True when every connection of the pool is back in it."""
    pool = common_db._pool
    assert pool is not None
    stats = pool.get_stats()
    return stats["pool_available"] == stats["pool_size"]


def test_an_exception_rolls_back_and_skips_the_hooks(db: TempDb):
    """Catches a commit or a hook after an error, and an exception that is wrapped or replaced."""
    ran: list[str] = []
    error = ValueError("forced")
    with pytest.raises(ValueError) as raised:
        with tx("E2") as cur:
            cur.execute(NEW_TYPE, ("rolled_back",))
            cur.after_commit(lambda: ran.append("hook"))
            raise error
    assert raised.value is error
    assert _committed(db.owner_url, "rolled_back") == 0
    assert ran == [] and not in_transaction()


def test_hooks_run_in_order_after_commit_without_locks(db: TempDb):
    """Catches hooks that run before COMMIT, while the row locks are still held, or before the
    connection is back in the pool."""
    with tx("E3") as cur:
        cur.execute(NEW_GPU, (0,))
    seen: list[tuple[int, bool, bool, bool]] = []

    def hook(n: int) -> None:
        with psycopg.connect(db.owner_url, autocommit=True) as other:
            visible = other.execute("SELECT count(*) FROM resources WHERE gpu_index = 1").fetchone() == (1,)
            try:
                other.execute("SELECT 1 FROM resources WHERE gpu_index = 0 FOR UPDATE NOWAIT")
                unlocked = True
            except errors.LockNotAvailable:
                unlocked = False
        seen.append((n, visible, unlocked, _pool_idle()))

    with tx("E3") as cur:
        cur.execute("SELECT 1 FROM resources WHERE gpu_index = 0 FOR UPDATE")
        cur.execute(NEW_GPU, (1,))
        for n in (1, 2, 3):
            cur.after_commit(lambda n=n: hook(n))
    assert seen == [(1, True, True, True), (2, True, True, True), (3, True, True, True)]


def test_a_caught_sql_error_is_not_committed(db: TempDb):
    """Catches psycopg's silent COMMIT of a failed transaction: the server answers ROLLBACK without
    an error, and the hooks would run as if the work had been committed."""
    ran: list[str] = []
    caught: list[psycopg.Error] = []
    with pytest.raises(RuntimeError, match="caught inside the transaction") as raised:
        with tx("E2") as cur:
            cur.execute(NEW_TYPE, ("swallowed",))
            cur.after_commit(lambda: ran.append("hook"))
            try:
                cur.execute(NEW_TYPE, ("swallowed",))  # 23505, caught right here
            except errors.UniqueViolation as exc:
                caught.append(exc)
    assert _committed(db.owner_url, "swallowed") == 0
    assert ran == []
    assert raised.value.__cause__ is caught[0] and "23505" in str(raised.value)


def test_a_rollback_inside_the_block_skips_the_hooks(db: TempDb):
    """Catches hooks that run after a ROLLBACK the body sent itself: COMMIT then finds no
    transaction and does nothing, and the hooks would run for work that was rolled back."""
    ran: list[str] = []
    with pytest.raises(RuntimeError, match="ended inside the block"):
        with tx("E2") as cur:
            cur.execute(NEW_TYPE, ("rolled_back_by_hand",))
            cur.after_commit(lambda: ran.append("hook"))
            cur.execute("ROLLBACK")
    assert _committed(db.owner_url, "rolled_back_by_hand") == 0
    assert ran == []


def test_a_failing_hook_is_logged_and_the_commit_stands(db: TempDb, json_log: Path):
    """Catches a hook error that undoes or hides the commit, skips the later hooks, or reaches the
    caller, who would then redo work that is already committed."""
    ran: list[str] = []

    def broken() -> None:
        raise ConnectionError("dispatch failed")

    with tx("E2") as cur:
        cur.execute(NEW_TYPE, ("kept",))
        cur.after_commit(broken)
        cur.after_commit(lambda: ran.append("second"))
    assert _committed(db.owner_url, "kept") == 1
    assert ran == ["second"]
    records = [json.loads(line) for line in json_log.read_text(encoding="utf-8").splitlines()]
    failed = [record for record in records if record["event"] == "after_commit_failed"]
    assert len(failed) == 1 and failed[0]["engine"] == "E2" and failed[0]["hook"].endswith("broken")
    assert "dispatch failed" in failed[0]["error"] and "Traceback" in failed[0]["traceback"]


def test_a_hook_can_open_its_own_transaction_on_a_pool_of_one(db: TempDb):
    """Catches hooks that run before the connection is back in the pool: E3's dispatch hook calls
    E1, which opens its own tx() (plan 6.3, 12.2 step 2); with one connection it could not get one."""
    _reopen(db, 1)
    done: list[str] = []

    def dispatch() -> None:
        with tx("E2") as inner:
            inner.execute(NEW_TYPE, ("from_hook",))
        done.append("dispatched")

    with tx("E3") as cur:
        cur.execute(NEW_GPU, (0,))
        cur.after_commit(dispatch)
    assert done == ["dispatched"] and _committed(db.owner_url, "from_hook") == 1


def test_nested_tx_is_refused_and_assert_outside_tx_raises_inside(db: TempDb):
    """Catches an engine call that opens its own transaction while the caller holds locks, and
    outside work started inside a transaction (invariant 3)."""
    checked: list[str] = []
    with tx("E3") as cur:
        with pytest.raises(RuntimeError, match="nested tx"):
            with tx("E2"):
                pass
        with pytest.raises(RuntimeError, match="dispatch called inside an open transaction"):
            assert_outside_tx("dispatch")
        cur.after_commit(lambda: (assert_outside_tx("dispatch"), checked.append("hook")))
    assert_outside_tx("dispatch")
    assert checked == ["hook"]


def test_transactions_on_two_threads_are_not_nested(db: TempDb):
    """Catches a process-wide nesting flag: a second thread's tx() would be refused while the
    first one is open. Two threads, a Barrier, 100 runs."""
    def hold(barrier: threading.Barrier) -> str:
        with tx("E2") as cur:
            cur.execute("SELECT 1")
            barrier.wait(timeout=10)  # both transactions are open here
            assert in_transaction()
        return "committed"

    with ThreadPoolExecutor(max_workers=2) as pool:
        for _ in range(RACES):
            barrier = threading.Barrier(2)
            runs = [pool.submit(hold, barrier) for _ in range(2)]
            assert [run.result(timeout=30) for run in runs] == ["committed", "committed"]
    assert not in_transaction()


@pytest.mark.parametrize("engine, own, other", [
    ("E1", "DELETE FROM attempts WHERE false",
     "INSERT INTO resources (kind, gpu_index, total, reserved) VALUES ('gpu', 7, 1000, 100)"),
    ("E2", "DELETE FROM jobs WHERE false",
     "INSERT INTO users (user_id, role, password_hash) VALUES ('intruder', 'student', '$2b$12$x')"),
    ("E3", "DELETE FROM resources WHERE false",
     "INSERT INTO workload_types (type_code, default_need_class, max_time_limit_s) VALUES ('intruder', 3, 600)"),
    ("E4", "DELETE FROM users WHERE false",
     "INSERT INTO problems (created_by, title, time_limit_ms) VALUES ('nobody', 'p', 1000)"),
])
def test_an_engine_cannot_write_another_engines_table(db: TempDb, engine: Engine, own: LiteralString,
                                                      other: LiteralString):
    """Catches a missing or wrong SET LOCAL ROLE (invariant 6). The write to the engine's own table
    must work, or the 42501 would prove nothing."""
    with tx(engine) as cur:
        cur.execute(own)
    with pytest.raises(errors.InsufficientPrivilege) as refused:
        with tx(engine) as cur:
            cur.execute(other)
    assert refused.value.sqlstate == "42501"


def test_set_local_role_does_not_leak_to_the_pooled_connection(db: TempDb):
    """Catches SET ROLE (for the whole session) in place of SET LOCAL ROLE: the next user of the
    pooled connection would act as that engine. A pool of one, so every step uses one backend."""
    _reopen(db, 1)
    pool = common_db._pool
    assert pool is not None

    def outside() -> tuple[Any, ...]:
        with pool.connection() as conn:
            row = conn.execute("SELECT pg_backend_pid(), current_user, session_user").fetchone()
        assert row is not None
        return row

    with tx("E3") as cur:
        inside = cur.execute("SELECT pg_backend_pid(), current_user").fetchone()
    assert inside is not None and inside[1] == "e3_role"
    assert outside() == (inside[0], "orch_app", "orch_app")
    with pytest.raises(ValueError):
        with tx("E1") as cur:
            cur.execute("SELECT 1")
            raise ValueError("forced")
    assert outside() == (inside[0], "orch_app", "orch_app")
    with tx("E4") as cur:
        assert cur.execute("SELECT pg_backend_pid(), current_user").fetchone() == (inside[0], "e4_role")


def test_retrying_gives_up_after_three_tries(caplog: pytest.LogCaptureFixture):
    """Catches endless retries, fewer than plan 5.1's 3 tries (40P01 retried twice, then raised),
    and a give-up that leaves no log line."""
    caplog.set_level(logging.INFO, logger="orch")
    calls: list[int] = []
    last = errors.DeadlockDetected("forced deadlock")

    def deadlocked() -> None:
        calls.append(1)
        raise last

    with pytest.raises(errors.DeadlockDetected) as raised:
        retrying(deadlocked)
    assert len(calls) == 3 and raised.value is last
    assert [record.getMessage() for record in caplog.records] == ["tx_retry", "tx_retry", "tx_retry_exhausted"]


def test_retrying_allows_at_most_three_tries():
    """Catches a caller raising the limit past the 3 tries of CLAUDE.md and plan 5.1."""
    for attempts in (0, 4):
        with pytest.raises(ValueError, match="1 to 3"):
            retrying(lambda: None, attempts=attempts)


def test_retrying_returns_the_result_after_a_deadlock():
    """Catches a retry that loses the transaction function's result."""
    calls: list[int] = []

    def once_deadlocked() -> str:
        calls.append(1)
        if len(calls) == 1:
            raise errors.DeadlockDetected("forced deadlock")
        return "done"

    assert retrying(once_deadlocked) == "done" and len(calls) == 2


@pytest.mark.parametrize("error, tries", [
    (errors.UniqueViolation("duplicate key"), 1),
    (RuntimeError("not a database error"), 1),
    (errors.SerializationFailure("could not serialize"), 3),
])
def test_retrying_retries_only_deadlocks_and_serialization_failures(error: Exception, tries: int):
    """Catches retrying every error, which would replay a constraint violation or a bug three times."""
    calls: list[int] = []

    def failing() -> None:
        calls.append(1)
        raise error

    with pytest.raises(type(error)):
        retrying(failing)
    assert len(calls) == tries


def test_a_real_deadlock_is_retried_and_succeeds(db: TempDb):
    """Catches a retry that resumes a broken transaction instead of replaying it, and hooks of the
    failed try. Two threads update two rows in opposite orders after a Barrier; PostgreSQL aborts
    one with 40P01 and retrying() replays it. Separate connections that commit, 100 runs;
    deadlock_timeout is 50 ms on this test database only, so the runs take seconds."""
    with psycopg.connect(db.owner_url, autocommit=True) as owner:
        owner.execute(sql.SQL("ALTER DATABASE {} SET deadlock_timeout = '50ms'").format(sql.Identifier(db.name)))
    _reopen(db, 2)  # new sessions read the setting
    with tx("E3") as cur:
        first = cur.execute(NEW_GPU, (0,)).fetchone()
        second = cur.execute(NEW_GPU, (1,)).fetchone()
    assert first is not None and second is not None
    a, b = first[0], second[0]
    tries: dict[str, int] = {}
    hooks: list[str] = []

    def update_both(name: str, one: int, two: int, barrier: threading.Barrier) -> None:
        def attempt() -> None:
            tries[name] = tries.get(name, 0) + 1
            with tx("E3") as cur:
                cur.execute(BUMP, (one,))
                cur.after_commit(lambda: hooks.append(name))  # before the deadlock: the failed try has one too
                if tries[name] == 1:
                    barrier.wait(timeout=10)  # each thread holds its first row now
                cur.execute(BUMP, (two,))
        retrying(attempt)

    with ThreadPoolExecutor(max_workers=2) as pool:
        for run in range(RACES):
            barrier = threading.Barrier(2)
            tries.clear()
            hooks.clear()
            runs = [pool.submit(update_both, "ab", a, b, barrier), pool.submit(update_both, "ba", b, a, barrier)]
            for finished in runs:
                finished.result(timeout=30)
            assert sorted(tries.values()) == [1, 2], (run, tries)
            assert sorted(hooks) == ["ab", "ba"], (run, hooks)
    with tx("E3") as cur:
        rows = cur.execute("SELECT total, reserved FROM resources ORDER BY resource_id").fetchall()
    assert rows == [(1000 + RACES * 20, 100 + RACES * 2)] * 2
    assert_invariants()


def test_a_dropped_pooled_connection_is_replaced(db: TempDb):
    """Catches the pool handing out a connection the server has dropped (a database restart, a
    killed backend): the next tx() would fail with 57P01. A pool of one; the server ends its backend
    and waits until it is gone."""
    _reopen(db, 1)
    with tx("E3") as cur:
        first = cur.execute("SELECT pg_backend_pid()").fetchone()
    assert first is not None
    with psycopg.connect(db.owner_url, autocommit=True) as owner:
        assert owner.execute("SELECT pg_terminate_backend(%s, 5000)", (first[0],)).fetchone() == (True,)
    with tx("E3") as cur:
        second = cur.execute("SELECT pg_backend_pid()").fetchone()
    assert second is not None and second[0] != first[0]


def test_a_transaction_closed_on_another_thread_clears_its_own_threads_flag(db: TempDb):
    """Catches the nesting flag cleared on the wrong thread when a generator suspended inside tx()
    is closed elsewhere (Starlette runs sync iterators on pool threads): the opening thread would
    then refuse every later tx() as nested."""
    def rows() -> Generator[int, None, None]:
        with tx("E2") as cur:
            cur.execute("SELECT 1")
            yield 1

    suspended = rows()
    assert next(suspended) == 1 and in_transaction()
    closer = threading.Thread(target=suspended.close)
    closer.start()
    closer.join(timeout=10)
    assert not in_transaction()
    with tx("E2") as cur:
        assert cur.execute("SELECT current_user").fetchone() == ("e2_role",)


def test_init_pool_fails_fast_and_keeps_one_pool(db: TempDb):
    """Catches a bad DATABASE_URL hidden behind a 30 s pool timeout without its cause, a second
    pool silently replacing the first, and tx() without a pool."""
    close_pool()
    with pytest.raises(RuntimeError, match="init_pool"):
        with tx("E3"):
            pass
    started = time.monotonic()
    with pytest.raises(psycopg.OperationalError, match="password authentication failed"):
        init_pool(make_conninfo(db.app_url, password="wrong"), min_size=1, max_size=1)
    assert time.monotonic() - started < 5
    init_pool(db.app_url, min_size=1, max_size=1)
    with pytest.raises(RuntimeError, match="already open"):
        init_pool(db.app_url, min_size=1, max_size=1)
    with tx("E3") as cur:
        assert cur.execute("SELECT current_user").fetchone() == ("e3_role",)


def test_misuse_fails_loudly(db: TempDb):
    """Catches a mistyped engine that would take a connection first, a hook added after the
    transaction ended (it would never run), and retrying() inside a transaction (a retry cannot
    replay the caller's work)."""
    with pytest.raises(ValueError, match="unknown engine"):
        with tx("E9"):  # pyright: ignore[reportArgumentType]
            pass
    with tx("E2") as cur:
        pass
    with pytest.raises(RuntimeError, match="finished"):
        cur.after_commit(lambda: None)
    with tx("E2"):
        with pytest.raises(RuntimeError, match="retrying"):
            retrying(lambda: None)
