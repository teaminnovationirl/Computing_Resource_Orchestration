"""Test helpers (plan 5.6): the TempDb handle that the db fixture yields, assert_invariants() and the
factories. Import this module as tests.helpers only: a second import path would load a second copy.
"""
from __future__ import annotations

import secrets
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from app.common.db import Engine, tx
from app.common.types import Vector
from app.e4_monitoring.state import JobState, set_actor, transition
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


def assert_invariants(settled: bool = False, unallocated: Collection[int] = ()) -> None:
    """Run the invariants query of db/invariants.sql on the database of the open pool and, once the
    run has settled, the leaks query too; fail listing every violating row. Every engine role may
    read every table; this one reads as E4. unallocated: jobs that make_job left allocated or
    running, which cannot hold an allocation before E3-2; their missing_allocation rows, and no
    other row, are excused (D-63)."""
    queries = invariant_queries()
    excused = {str(job_id) for job_id in unallocated}
    found: list[tuple[object, ...]] = []
    with tx("E4") as cur:
        for name in ["invariants", "leaks"] if settled else ["invariants"]:
            found += [(name, *row) for row in cur.execute(queries[name]).fetchall()
                      if not (name == "invariants" and row[0] == "missing_allocation" and row[1] in excused)]
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


def make_job(state: JobState = "queued", vector: Vector = Vector(1000, 512)) -> int:
    """A cpu_bound job of a new student in the given state; returns its job_id. E2 inserts it the
    way submit() will: the state trigger demands app.actor and app.reason on the INSERT too
    (JT002), so they are set for that one transaction only (D-51). Each step of PATH[state] then
    runs through transition() in its own tx() of the engine that makes it (plan 7.2), so the
    trigger checks the test data too, and each step publishes one job_state event to the stub hub.
    An allocated or running job has no allocation until E3-2: pass it to
    assert_invariants(unallocated=...)."""
    steps = PATH[state]  # an unknown state fails here, before anything is written
    user_id = make_user("student")
    with tx("E2") as cur:
        cur.execute("INSERT INTO workload_types (type_code, default_need_class, max_time_limit_s) "
                    "VALUES ('cpu_bound', 3, 600) ON CONFLICT DO NOTHING")
        set_actor(cur, "E2", "submitted")
        row = cur.execute("INSERT INTO jobs (user_id, type_code, cpu_millicores, ram_mib, vram_mib, time_limit_s, "
                          "need_class, payload_path) VALUES (%s, 'cpu_bound', %s, %s, %s, 5, 3, 'payloads/test') "
                          "RETURNING job_id",
                          (user_id, vector.cpu_millicores, vector.ram_mib, vector.vram_mib)).fetchone()
    assert row is not None
    job_id: int = row[0]
    current: JobState = "queued"
    for step in steps:
        engine = ACTOR[(current, step)]
        with tx(engine) as cur:
            transition(cur, job_id, step, engine, f"test: {step}")
        current = step
    return job_id
