"""Pool, tx(), after-commit hooks and deadlock retry (plan 5.1).

Every engine transaction runs as that engine's database role: `with tx("E3") as cur:`. Work that
leaves the process (dispatch, hub pushes, E2 notices) is registered with cur.after_commit() and
runs only after COMMIT, once the connection is back in the pool and no lock is held (invariant 3).
A database error the body must survive belongs in a savepoint, taken only with `with cur.savepoint():`
and never with connection.transaction() directly, so a rollback also drops its hooks (D-67); a caught
error without one makes tx() refuse to commit (D-48).
"""
from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Final, Literal, TypeVar

import psycopg
from psycopg import sql
from psycopg.abc import Params, QueryNoTemplate
from psycopg.pq import TransactionStatus
from psycopg.rows import TupleRow
from psycopg_pool import ConnectionPool

from app.common.logging import log

Engine = Literal["E1", "E2", "E3", "E4"]
T = TypeVar("T")

ROLE: Final[dict[str, str]] = {"E1": "e1_role", "E2": "e2_role", "E3": "e3_role", "E4": "e4_role"}
RETRYABLE: Final = frozenset({"40P01", "40001"})  # deadlock, serialization failure
MAX_TRIES: Final = 3  # CLAUDE.md and plan 5.1: 3 tries in all (D-55)
CONNECT_TIMEOUT_S: Final = 10  # D-45

_local = threading.local()
_pool: ConnectionPool[psycopg.Connection[TupleRow]] | None = None


def _open_flag() -> list[bool]:
    """This thread's nesting flag, kept in a cell: tx() clears the cell of the thread that opened
    the transaction, even when a generator suspended inside tx() is closed on another thread."""
    cell: list[bool] | None = getattr(_local, "cell", None)
    if cell is None:
        cell = [False]
        _local.cell = cell
    return cell


class Tx:
    """The cursor of one open transaction. Every call shares this cursor: fetch a result before
    the next execute()."""

    def __init__(self, cur: psycopg.Cursor[TupleRow]) -> None:
        self.cur = cur
        self.hooks: list[Callable[[], object]] = []
        self.finished = False
        self.failed: psycopg.Error | None = None  # the last error a statement raised through execute()

    def execute(self, query: QueryNoTemplate, params: Params | None = None) -> psycopg.Cursor[TupleRow]:
        try:
            return self.cur.execute(query, params)
        except psycopg.Error as exc:
            self.failed = exc
            raise

    def fetchone(self) -> TupleRow | None:
        return self.cur.fetchone()

    def fetchall(self) -> list[TupleRow]:
        return self.cur.fetchall()

    def after_commit(self, fn: Callable[[], object]) -> None:
        """Run fn after a successful COMMIT, in the order registered; never after a rollback."""
        if self.finished:
            raise RuntimeError("after_commit() on a finished transaction: the hook would never run")
        self.hooks.append(fn)

    @contextmanager
    def savepoint(self) -> Iterator[None]:
        """A savepoint inside this transaction, the only way to survive a database error in the body
        of tx() (D-48, D-67). When it rolls back (an exception, or psycopg.Rollback), the
        after_commit hooks registered inside it are dropped too, including those of inner
        savepoints that were already released, so nothing is pushed for work that was undone."""
        mark = len(self.hooks)
        released = False
        try:
            with self.cur.connection.transaction():
                yield
                released = True
        finally:
            if not released:
                del self.hooks[mark:]


def init_pool(dsn: str, min_size: int, max_size: int) -> None:
    """Open the process's pool (D-49). One probe connection comes first, so a wrong DSN fails at
    once with the server's own message, where a pool would only log it and time out after 30 s.
    The pool checks each connection before handing it out, so one the server dropped is replaced."""
    global _pool
    if _pool is not None:
        raise RuntimeError("init_pool(): a pool is already open; call close_pool() first")
    psycopg.connect(dsn, connect_timeout=CONNECT_TIMEOUT_S).close()
    pool: ConnectionPool[psycopg.Connection[TupleRow]] = ConnectionPool(
        dsn, min_size=min_size, max_size=max_size, open=False, check=ConnectionPool.check_connection,
        kwargs={"connect_timeout": CONNECT_TIMEOUT_S})
    try:
        pool.open(wait=True, timeout=CONNECT_TIMEOUT_S)
    except BaseException:
        pool.close()
        raise
    _pool = pool


def close_pool() -> None:
    global _pool
    pool, _pool = _pool, None
    if pool is not None:
        pool.close()


def in_transaction() -> bool:
    return _open_flag()[0]


def assert_outside_tx(what: str) -> None:
    """Called by dispatch, hub.publish, capacity_changed and every docker call (invariant 3)."""
    if in_transaction():
        raise RuntimeError(f"{what} called inside an open transaction")


@contextmanager
def tx(engine: Engine) -> Iterator[Tx]:
    """One transaction as one engine's database role (invariant 6). COMMIT on a clean exit; on any
    exception ROLLBACK, then the same exception is re-raised. Nested tx() on one thread is refused,
    so an engine call that opens its own transaction never runs while the caller holds locks.
    Hooks run after COMMIT, once the connection is back in the pool; a failing hook is logged and
    the next one still runs, because the commit stands (D-47)."""
    if engine not in ROLE:
        raise ValueError(f"unknown engine {engine!r}; expected one of {', '.join(ROLE)}")
    pool = _pool
    if pool is None:
        raise RuntimeError("tx(): no pool; call init_pool() first")
    flag = _open_flag()
    if flag[0]:
        raise RuntimeError("nested tx(): call other engines after COMMIT")
    flag[0] = True
    hooks: list[Callable[[], object]] = []
    t: Tx | None = None
    try:
        with pool.connection() as conn:  # an exception leaving this block rolls back
            with conn.cursor() as cur:
                cur.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(ROLE[engine])))
                t = Tx(cur)
                yield t
            _refuse_unless_open(conn, engine, t)
            conn.commit()
            hooks = t.hooks
    finally:
        flag[0] = False
        if t is not None:
            t.finished = True
    for fn in hooks:  # COMMIT done and the connection back in the pool: no lock is held
        try:
            fn()
        except Exception:  # noqa: BLE001 (D-47: a failing hook is logged and the next one still runs)
            log.exception("after_commit_failed", engine=engine, hook=getattr(fn, "__qualname__", repr(fn)))


def _refuse_unless_open(conn: psycopg.Connection[TupleRow], engine: str, t: Tx) -> None:
    """Commit only the transaction tx() opened (D-48). After an error the body caught, psycopg would
    send COMMIT and the server would roll back without an error; after a COMMIT or ROLLBACK the
    body sent itself, COMMIT would do nothing. Either way the hooks would run as if committed."""
    status = conn.info.transaction_status
    if status == TransactionStatus.INERROR:
        code = f" ({t.failed.sqlstate})" if t.failed is not None and t.failed.sqlstate else ""
        raise RuntimeError(f"tx({engine}): a statement failed{code} and its error was caught inside the "
                           "transaction; rolled back instead of committing") from t.failed
    if status != TransactionStatus.INTRANS:
        raise RuntimeError(f"tx({engine}): the transaction was ended inside the block (a COMMIT or "
                           "ROLLBACK sent by hand), so its hooks are skipped")


def retrying(fn: Callable[[], T], attempts: int = 3) -> T:
    """Run fn, which runs one whole tx(), again after a deadlock or a serialization failure (40P01,
    40001): attempts tries in all, at most 3 (D-55), then the last error is raised unchanged. Any
    other error is raised at once. A retry replays all of fn, so fn runs exactly one transaction,
    and retrying() never starts inside one."""
    assert_outside_tx("retrying()")
    if not 1 <= attempts <= MAX_TRIES:
        raise ValueError(f"retrying(): attempts must be 1 to {MAX_TRIES}, not {attempts}")
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except psycopg.Error as exc:
            if exc.sqlstate not in RETRYABLE:
                raise
            if attempt == attempts:
                log.error("tx_retry_exhausted", sqlstate=exc.sqlstate, attempts=attempts)
                raise
            log.info("tx_retry", sqlstate=exc.sqlstate, attempt=attempt, attempts=attempts)
    raise AssertionError("unreachable")
