"""Test helpers (plan 5.6): the TempDb handle that the db fixture yields, assert_invariants(), the
factories and the race tools of the E3 tests. Import this module as tests.helpers only: a second import
path would load a second copy.
"""
from __future__ import annotations

import io
import json
import secrets
import stat
import threading
import time
import zipfile
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Final, LiteralString, TypeVar

import psycopg
import pytest
from psycopg import errors

from app.common.clock import FakeClock
from app.common.db import Engine, Tx, tx
from app.common.types import Committed, QueueEntry, SubmitRequest, Vector
from app.e2_scheduling.queue import JobQueue
from app.e2_scheduling.service import insert_job
from app.e3_allocation import allocate, service
from app.e4_monitoring.state import JobState, transition
from scripts.capacity import apply, s0_rows
from tests.stubs import Stubs

ROOT = Path(__file__).resolve().parents[1]
INVARIANTS_SQL = ROOT / "db" / "invariants.sql"
PASSWORD_HASH = "$2b$12$" + "." * 53  # the format users requires; no password matches it

# The test oracle, written from the baseline and never computed from the code under test: the 8
# states and the 13 transitions of context §9, each with the engine that makes it (plan 7.2;
# running to failed is also E4's, when a worker is lost).
STATES: Final[tuple[JobState, ...]] = (
    "queued", "admitted", "allocated", "running", "completed", "failed", "cancelled", "released")
ACTOR: Final[dict[tuple[JobState, JobState], Engine]] = {
    ("queued", "admitted"): "E2", ("queued", "cancelled"): "E2",
    ("admitted", "allocated"): "E3", ("admitted", "queued"): "E3",
    ("allocated", "running"): "E1", ("allocated", "cancelled"): "E1",
    ("running", "completed"): "E1", ("running", "failed"): "E1", ("running", "cancelled"): "E1",
    ("completed", "released"): "E3", ("failed", "released"): "E3", ("cancelled", "released"): "E3",
    ("released", "queued"): "E2",
}
ALLOWED: Final = frozenset(ACTOR)
# make_job's way from queued to each state (D-63): cancelled while queued, released after a
# completed run, the order a real job takes.
PATH: Final[dict[JobState, tuple[JobState, ...]]] = {
    "queued": (),
    "admitted": ("admitted",),
    "allocated": ("admitted", "allocated"),
    "running": ("admitted", "allocated", "running"),
    "completed": ("admitted", "allocated", "running", "completed"),
    "failed": ("admitted", "allocated", "running", "failed"),
    "cancelled": ("cancelled",),
    "released": ("admitted", "allocated", "running", "completed", "released"),
}


@dataclass(frozen=True)
class TempDb:
    """One fresh database copied from orch_template; the pool of app.common.db is open on it."""
    name: str
    owner_url: str  # postgres, the owner: constraints apply, privileges do not
    app_url: str    # orch_app, the login of the pool
    stubs: Stubs


def invariant_queries() -> dict[str, bytes]:
    """The named queries of db/invariants.sql, split at their '-- name:' lines."""
    queries: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in INVARIANTS_SQL.read_text(encoding="utf-8").splitlines():
        if line.startswith("-- name:"):
            current = queries.setdefault(line.removeprefix("-- name:").strip(), [])
        elif current is not None:
            current.append(line)
    return {name: "\n".join(lines).encode() for name, lines in queries.items()}


def invariant_violations(settled: bool = False) -> list[tuple[object, ...]]:
    """Every row of the invariants query of db/invariants.sql on the database of the open pool and,
    once the run has settled, of the leaks query too, each led by its query's name. Every engine role
    may read every table; this one reads as E4."""
    queries = invariant_queries()
    found: list[tuple[object, ...]] = []
    with tx("E4") as cur:
        for name in ["invariants", "leaks"] if settled else ["invariants"]:
            found += [(name, *row) for row in cur.execute(queries[name]).fetchall()]
    return found


def assert_invariants(settled: bool = False) -> None:
    """Fail listing every violating row of invariant_violations(settled)."""
    found = invariant_violations(settled)
    assert not found, "db/invariants.sql found violations:\n" + "\n".join(map(str, found))


def make_user(role: str = "student") -> str:
    """A user with a fresh user_id, written by E4; returns the user_id."""
    user_id = f"{role[:3]}_{secrets.token_hex(4)}"
    with tx("E4") as cur:
        cur.execute("INSERT INTO users (user_id, role, password_hash) VALUES (%s, %s, %s)",
                    (user_id, role, PASSWORD_HASH))
    return user_id


def make_problem(cases: int = 10) -> int:
    """A problem of a new faculty member (FA001) with cases 1 to cases, written by E1; returns its id."""
    owner = make_user("faculty")
    with tx("E1") as cur:
        row = cur.execute("INSERT INTO problems (created_by, title, time_limit_ms) VALUES (%s, %s, 1000) "
                          "RETURNING problem_id", (owner, "Test problem")).fetchone()
        assert row is not None
        problem_id: int = row[0]
        for case_no in range(1, cases + 1):
            cur.execute("INSERT INTO test_cases (problem_id, case_no, input_path, expected_path) "
                        "VALUES (%s, %s, %s, %s)",
                        (problem_id, case_no, f"problems/{problem_id}/{case_no:02d}.in",
                         f"problems/{problem_id}/{case_no:02d}.out"))
    return problem_id


def zip_of(files: dict[str, bytes], links: tuple[str, ...] = (), compression: int = zipfile.ZIP_STORED) -> bytes:
    """A zip archive holding files and symbolic links (E2 tests). Each name is stored exactly as given:
    ZipInfo would turn a backslash into a slash on Windows, so the name is set after it is built."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=compression) as archive:
        for name, data in files.items():
            info = zipfile.ZipInfo("placeholder")
            info.filename = name
            info.compress_type = compression
            archive.writestr(info, data)
        for name in links:
            info = zipfile.ZipInfo(name)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, "main.py")
    return buffer.getvalue()


DEFAULT_JOB_VECTOR = Vector(1000, 512)  # make_job()'s default; a call in the signature trips B008


def make_job(state: JobState = "queued", vector: Vector = DEFAULT_JOB_VECTOR, need_class: int = 3) -> int:
    """A cpu_bound job of a new student in the given state; returns its job_id. E2 inserts it with
    insert_job(), the INSERT that submit() runs, which sets app.actor and app.reason for it (D-81).
    It skips submit()'s validation and payload, so a test may ask allocate() for any vector and needs
    no capacity loaded: the job is queued with need_class (3 unless given) and payload_path payloads/test. Each step
    of PATH[state] then runs in the engine that makes it (plan 7.2), so the trigger checks the test
    data too, and each step publishes one job_state event to the stub hub. A job that ends allocated or running gets
    its allocation of vector from e3 allocate(), which also dispatches it to the stub E1 and
    publishes a capacity event; the S0 capacity is loaded first when resources is empty. Every
    other step runs through transition() in its own tx(), so completed, failed and released jobs
    hold nothing, as after a release (D-73)."""
    steps = PATH[state]  # an unknown state fails here, before anything is written
    user_id = make_user("student")
    with tx("E2") as cur:
        cur.execute("INSERT INTO workload_types (type_code, default_need_class, max_time_limit_s) "
                    "VALUES ('cpu_bound', 3, 600) ON CONFLICT DO NOTHING")
        request = SubmitRequest("cpu_bound", vector.cpu_millicores, vector.ram_mib, vector.vram_mib, 5)
        job_id, _ = insert_job(cur, user_id, request, need_class, "payloads/test")
    current: JobState = "queued"
    for step in steps:
        engine = ACTOR[(current, step)]
        if step == "allocated" and state in ("allocated", "running"):
            _allocate(job_id, vector)
        else:
            with tx(engine) as cur:
                transition(cur, job_id, step, engine, f"test: {step}")
        current = step
    return job_id


def _allocate(job_id: int, vector: Vector) -> None:
    """Move the admitted job to allocated with a real allocation, on the S0 capacity when none is loaded."""
    with tx("E3") as cur:
        row = cur.execute("SELECT count(*) FROM resources").fetchone()
        if row is not None and row[0] == 0:
            apply(cur, s0_rows())
    result = allocate(job_id, vector)
    assert isinstance(result, Committed), f"make_job: allocate() refused job {job_id}: {result}"


# Races (E3-2, E3-3). Real threads call the engine on separate pooled connections; a gate holds the CPU
# row, the first resource row every allocate() and release() locks (every job holds CPU), so each
# interleaving happens every run, never by luck.
CPU: Final = 1  # the cpu row of S0 (db/seed.sql)
T = TypeVar("T")


@contextmanager
def gate(db: TempDb, resource_id: int = CPU) -> Iterator[None]:
    """Hold resource_id as allocate() and release() do (FOR UPDATE) until the block ends."""
    with psycopg.connect(db.owner_url) as conn:
        conn.execute("SELECT 1 FROM resources WHERE resource_id = %s FOR UPDATE", (resource_id,))
        yield
        conn.rollback()


def wait_until_waiting(db: TempDb, count: int, futures: Sequence[Future[Any]]) -> None:
    """Return once count sessions of this database wait on a lock, or as soon as a racer has ended
    without waiting (its result then fails the test). Every waiting session counts, not only those
    waiting on the gate: the first racer to queue for a row waits on its holder, and each later one
    on the tuple lock of the racer ahead of it, which pg_blocking_pids names instead of the holder.
    Each poll is a transaction of its own: pg_stat_activity stays fixed for the rest of a transaction."""
    deadline = time.monotonic() + 10
    with psycopg.connect(db.owner_url, autocommit=True) as watch:
        while not any(future.done() for future in futures):
            row = watch.execute("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                                "AND cardinality(pg_blocking_pids(pid)) > 0").fetchone()
            if row is not None and row[0] >= count:
                return
            assert time.monotonic() < deadline, f"{count} racers never waited on a lock"


def race(db: TempDb, pool: ThreadPoolExecutor, calls: Sequence[Callable[[], T]]) -> list[T | BaseException]:
    """Every call at once, released by one Barrier while the gate holds the CPU row; the gate opens
    once every racer waits on a lock. Each outcome is the call's result or the exception it raised."""
    barrier = threading.Barrier(len(calls))

    def run(call: Callable[[], T]) -> T:
        barrier.wait(timeout=10)
        return call()

    with gate(db):
        futures = [pool.submit(run, call) for call in calls]
        wait_until_waiting(db, len(calls), futures)
    return [future.exception(timeout=30) or future.result() for future in futures]


def lockable(probe: psycopg.Connection[Any], query: LiteralString, key: int) -> bool:
    """True when query, a FOR UPDATE NOWAIT on one row, gets its lock; autocommit lets go at once."""
    try:
        probe.execute(query, (key,))
    except errors.LockNotAvailable:
        return False
    return True


def fail_at_end_of_body(monkeypatch: pytest.MonkeyPatch, *failures: Exception, module: ModuleType = service) -> None:
    """Make the transactions of module (app.e3_allocation.service unless given) raise each failure in turn
    once the body has run, that is after every write and every after_commit registration, inside the real
    tx(), which then rolls back. A fault at the transaction boundary: the code under test runs unchanged."""
    real = module.tx
    pending = list(failures)

    @contextmanager
    def failing_tx(engine: Engine) -> Iterator[Tx]:
        with real(engine) as t:
            yield t
            if pending:
                raise pending.pop(0)

    monkeypatch.setattr(module, "tx", failing_tx)


def lose_commit_ack(monkeypatch: pytest.MonkeyPatch, module: ModuleType, *failures: Exception) -> None:
    """Make the next transactions of module commit and then raise each failure in turn instead of running their
    hooks: a lost COMMIT acknowledgement (D-105), the work committed and every after_commit hook skipped."""
    real = module.tx
    pending = list(failures)

    @contextmanager
    def ack_lost(engine: Engine) -> Iterator[Tx]:
        failure = pending.pop(0) if pending else None
        with real(engine) as t:
            yield t
            if failure is not None:
                t.hooks.clear()
        if failure is not None:
            raise failure

    monkeypatch.setattr(module, "tx", ack_lost)


def logged(path: Path, event: str) -> list[dict[str, Any]]:
    """The JSON log records of one event, in order, from the file the json_log fixture names."""
    return [record for record in map(json.loads, path.read_text(encoding="utf-8").splitlines())
            if record["event"] == event]


def queue_entry(job_id: int) -> QueueEntry:
    """The QueueEntry that submit() pushes for job_id, built from its row (make_job pushes nothing)."""
    with tx("E2") as cur:
        row = cur.execute("SELECT user_id, need_class, submitted_at, cpu_millicores, ram_mib, vram_mib "
                          "FROM jobs WHERE job_id = %s", (job_id,)).fetchone()
    assert row is not None, f"no job {job_id}"
    return QueueEntry(job_id, row[0], row[1], row[2], Vector(row[3], row[4], row[5]))


# Admission (E2-3): jobs in an E2Service queue, what the database says about them, and a clock.
SMALL: Final = Vector(100, 64)  # the smallest vector submit() accepts (plan 8.1)


def queued(queue: JobQueue, vector: Vector = SMALL, need_class: int = 3) -> int:
    """make_job("queued") pushed into queue as submit() would push it; returns the job_id."""
    job_id = make_job("queued", vector, need_class)
    queue.push(queue_entry(job_id))
    return job_id


def job_state(job_id: int) -> str:
    with tx("E2") as cur:
        row = cur.execute("SELECT state FROM jobs WHERE job_id = %s", (job_id,)).fetchone()
    assert row is not None, f"no job {job_id}"
    return row[0]


def job_history(job_id: int) -> list[tuple[str | None, str, str, str]]:
    """(from_state, to_state, actor, reason) of every move of job_id, oldest first."""
    with tx("E2") as cur:
        rows = cur.execute("SELECT from_state, to_state, actor, reason FROM job_state_history "
                           "WHERE job_id = %s ORDER BY event_id", (job_id,)).fetchall()
    return [(row[0], row[1], row[2], row[3]) for row in rows]


class OffsetClock(FakeClock):
    """A FakeClock whose time() runs a fixed offset ahead of monotonic(): monotonic() starts at 0 and time()
    at wall_start, so code that reads one where it needs the other fails its test instead of passing by
    luck (with a plain FakeClock both return the same value)."""

    def __init__(self, wall_start: float) -> None:
        super().__init__(0.0)
        self._offset = wall_start

    def time(self) -> float:
        return self.monotonic() + self._offset
