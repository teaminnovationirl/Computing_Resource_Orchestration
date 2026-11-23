"""Shared database fixtures (plan 5.6; the A-2 fixtures moved here from tests/schema, D-42).

Each session runs db/roles.sql once on the server of ADMIN_DATABASE_URL (the docker compose
service db by default), drops the t_* databases a crashed session left, and builds:
- orch_test: schema and seed. owner_tx and app_tx give a schema test one transaction that is
  rolled back afterwards; the race tests commit on connections of their own and clean up.
- orch_template: schema only. The db fixture copies it into a fresh t_<random> database for each
  test, opens the pool of app.common.db on it as orch_app and installs the stubs.
Never run two test sessions against one server at once: both rebuild these databases.
"""
from __future__ import annotations

import contextlib
import logging
import secrets
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from app.common import ports
from app.common.db import close_pool, init_pool
from app.common.logging import LIBRARY, LOGGER, configure
from app.config import Config
from tests.helpers import TempDb
from tests.stubs import install_stubs

ROOT = Path(__file__).resolve().parents[1]
TEST_DB = "orch_test"
TEMPLATE_DB = "orch_template"
LEFTOVER = r"^t_[0-9a-f]{12}$"  # the names the db fixture gives


@dataclass(frozen=True)
class Urls:
    owner: str  # the database owner (postgres superuser): constraints apply, privileges do not
    app: str    # orch_app, the application login: it writes only after SET ROLE eN_role


class OpenTx:
    """One open transaction on orch_test (not app.common.db.Tx: no engine role, no hooks)."""

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
        inserted = self.one(query, list(row.values()))
        assert inserted is not None
        return inserted[0]

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


def _run_file(conn: psycopg.Connection[Any], name: str) -> None:
    conn.execute((ROOT / "db" / name).read_bytes())


def _admin_execute(admin_url: str, statement: sql.Composed) -> None:
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(statement)


@pytest.fixture(scope="session")
def cfg() -> Config:
    return Config.from_env()


@pytest.fixture(scope="session")
def admin_url(cfg: Config) -> str:
    """The server answers, the roles exist (db/roles.sql), and no t_* database of a crashed session
    is left."""
    try:
        admin = psycopg.connect(cfg.admin_database_url, autocommit=True, connect_timeout=5)
    except psycopg.OperationalError as exc:
        pytest.fail(f"PostgreSQL is not reachable at ADMIN_DATABASE_URL: {exc}\n"
                    'Start it with "docker compose up -d db", then run the tests again.',
                    pytrace=False)
    with admin:
        _run_file(admin, "roles.sql")
        for (name,) in admin.execute("SELECT datname FROM pg_database WHERE datname ~ %s", (LEFTOVER,)).fetchall():
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
    return cfg.admin_database_url


@pytest.fixture(scope="session")
def urls(admin_url: str, cfg: Config) -> Urls:
    """orch_test from db/schema.sql and db/seed.sql, rebuilt once per session."""
    _admin_execute(admin_url, sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(TEST_DB)))
    _admin_execute(admin_url, sql.SQL("CREATE DATABASE {}").format(sql.Identifier(TEST_DB)))
    owner = make_conninfo(admin_url, dbname=TEST_DB)
    with psycopg.connect(owner, autocommit=True) as conn:
        _run_file(conn, "schema.sql")
        _run_file(conn, "seed.sql")
    return Urls(owner=owner, app=make_conninfo(cfg.database_url, dbname=TEST_DB))


@pytest.fixture(scope="session")
def template(admin_url: str) -> str:
    """orch_template from db/schema.sql alone, closed to connections so that CREATE DATABASE can
    always copy it."""
    name = sql.Identifier(TEMPLATE_DB)
    _admin_execute(admin_url, sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(name))
    _admin_execute(admin_url, sql.SQL("CREATE DATABASE {}").format(name))
    with psycopg.connect(make_conninfo(admin_url, dbname=TEMPLATE_DB), autocommit=True) as conn:
        _run_file(conn, "schema.sql")
    _admin_execute(admin_url, sql.SQL("ALTER DATABASE {} WITH ALLOW_CONNECTIONS false").format(name))
    return TEMPLATE_DB


@pytest.fixture
def db(admin_url: str, template: str, cfg: Config, monkeypatch: pytest.MonkeyPatch) -> Iterator[TempDb]:
    """A fresh copy of orch_template for one test, the pool open on it as orch_app (1 to 4
    connections) and fresh stubs installed; afterwards the stubs are uninstalled, so a later test
    gets no stale engines, and the database is dropped."""
    monkeypatch.setattr(ports, "_installed", None)
    name = f"t_{secrets.token_hex(6)}"
    _admin_execute(admin_url, sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(sql.Identifier(name),
                                                                              sql.Identifier(template)))
    temp = TempDb(name=name, owner_url=make_conninfo(admin_url, dbname=name),
                  app_url=make_conninfo(cfg.database_url, dbname=name), stubs=install_stubs())
    try:
        init_pool(temp.app_url, min_size=1, max_size=4)
        yield temp
    finally:
        close_pool()
        _admin_execute(admin_url, sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


def _open_tx(url: str) -> Iterator[OpenTx]:
    with psycopg.connect(url) as conn:
        conn.execute("SELECT 1")  # opens the transaction, so refused() blocks become savepoints
        yield OpenTx(conn)
        conn.rollback()


@pytest.fixture
def owner_tx(urls: Urls) -> Iterator[OpenTx]:
    """A transaction as the database owner, rolled back after the test."""
    yield from _open_tx(urls.owner)


@pytest.fixture
def app_tx(urls: Urls) -> Iterator[OpenTx]:
    """A transaction as orch_app, rolled back after the test; switch engines with tx.role()."""
    yield from _open_tx(urls.app)


@pytest.fixture
def json_log(tmp_path: Path) -> Iterator[Path]:
    """Logging as main.py configures it, into tmp_path/logs/server.log; undone afterwards."""
    configure(tmp_path)
    yield tmp_path / "logs" / "server.log"
    for logger in (LOGGER, LIBRARY):
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
            handler.close()
        logger.propagate = True
    LOGGER.setLevel(logging.NOTSET)
