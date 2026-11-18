"""Fixtures for the schema tests (slice A-2).

Each test session rebuilds the database orch_test from db/roles.sql, db/schema.sql and
db/seed.sql on the server of ADMIN_DATABASE_URL (the docker compose service db by default).
owner_tx and app_tx give a test one transaction that is rolled back afterwards; the
last-unit race test commits on its own connections and removes what it created.
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from app.config import Config

ROOT = Path(__file__).resolve().parents[2]
TEST_DB = "orch_test"


@dataclass(frozen=True)
class Urls:
    owner: str  # the database owner (postgres superuser): constraints apply, privileges do not
    app: str    # orch_app, the application login: it writes only after SET ROLE eN_role


class Tx:
    """One open transaction on orch_test."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self.conn = conn

    def run(self, query, params=None) -> None:
        self.conn.execute(query, params)

    def one(self, query, params=None):
        return self.conn.execute(query, params).fetchone()

    def all(self, query, params=None) -> list:
        return self.conn.execute(query, params).fetchall()

    def role(self, role: str) -> None:
        """Act as one engine for the rest of the transaction, as every engine transaction does."""
        self.run(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))

    def actor(self, actor: str | None, reason: str | None = "test") -> None:
        """Set the two settings the state trigger requires; None leaves a setting empty."""
        self.run("SELECT set_config('app.actor', %s, true), set_config('app.reason', %s, true)",
                 (actor or "", reason or ""))

    def new_job(self, **cols) -> int:
        """Insert a job the way E2 submits one (actor E2, reason 'submitted'); returns job_id."""
        row = {"user_id": "01FE24BCS901", "type_code": "cpu_bound", "cpu_millicores": 1000,
               "ram_mib": 512, "vram_mib": 0, "time_limit_s": 5, "need_class": 3,
               "payload_path": "payloads/test"} | cols
        self.actor("E2", "submitted")
        query = sql.SQL("INSERT INTO jobs ({}) VALUES ({}) RETURNING job_id").format(
            sql.SQL(", ").join(map(sql.Identifier, row)),
            sql.SQL(", ").join([sql.Placeholder()] * len(row)))
        return self.one(query, list(row.values()))[0]

    def move(self, job_id: int, *states: str, actor: str = "E4") -> None:
        """Change the job's state to each of states in turn, with the trigger's settings set."""
        for state in states:
            self.actor(actor, f"to {state}")
            self.run("UPDATE jobs SET state = %s WHERE job_id = %s", (state, job_id))

    @contextlib.contextmanager
    def refused(self, sqlstate: str, constraint: str | None = None):
        """The block must fail with exactly this SQLSTATE, and this constraint when given.
        It runs in a savepoint, so the test's transaction stays usable afterwards."""
        with pytest.raises(psycopg.Error) as info:
            with self.conn.transaction():
                yield
        error = info.value
        assert error.sqlstate == sqlstate, f"expected {sqlstate}, got {error.sqlstate}: {error}"
        if constraint is not None:
            assert error.diag.constraint_name == constraint, f"expected {constraint}: {error}"


def _run_file(conn: psycopg.Connection, name: str) -> None:
    conn.execute((ROOT / "db" / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def urls() -> Urls:
    cfg = Config.from_env()
    try:
        admin = psycopg.connect(cfg.admin_database_url, autocommit=True, connect_timeout=5)
    except psycopg.OperationalError as exc:
        pytest.fail(f"PostgreSQL is not reachable at ADMIN_DATABASE_URL: {exc}\n"
                    'Start it with "docker compose up -d db", then run the tests again.',
                    pytrace=False)
    with admin:
        _run_file(admin, "roles.sql")
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(TEST_DB)))
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(TEST_DB)))
    owner = make_conninfo(cfg.admin_database_url, dbname=TEST_DB)
    with psycopg.connect(owner, autocommit=True) as conn:
        _run_file(conn, "schema.sql")
        _run_file(conn, "seed.sql")
    return Urls(owner=owner, app=make_conninfo(cfg.database_url, dbname=TEST_DB))


def _open_tx(url: str):
    with psycopg.connect(url) as conn:
        conn.execute("SELECT 1")  # opens the transaction, so refused() blocks become savepoints
        yield Tx(conn)
        conn.rollback()


@pytest.fixture
def owner_tx(urls: Urls):
    """A transaction as the database owner, rolled back after the test."""
    yield from _open_tx(urls.owner)


@pytest.fixture
def app_tx(urls: Urls):
    """A transaction as orch_app, rolled back after the test; switch engines with tx.role()."""
    yield from _open_tx(urls.app)
