"""The only way to change jobs.state (plan 7.1; invariant 5).

transition() changes the state inside the caller's transaction. The state trigger of db/schema.sql
checks the move against the 13 transitions of context §9 and writes the history row, so no
application code does either. The live job_state event (message 12) is registered with
after_commit() and leaves the process only after COMMIT (invariant 3, D-32).
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from functools import partial
from typing import Any, Final, Literal

import psycopg
from psycopg.pq import TransactionStatus
from psycopg.rows import TupleRow

from app.common.db import Engine, Tx
from app.common.errors import IllegalTransition, NotFound
from app.common.logging import log
from app.common.ports import engines

JobState = Literal["queued", "admitted", "allocated", "running", "completed", "failed", "cancelled", "released"]

_MOVE: Final = "UPDATE jobs SET state = %s WHERE job_id = %s RETURNING user_id, clock_timestamp()"
_MOVE_IF: Final = ("UPDATE jobs SET state = %s WHERE job_id = %s AND state = %s "
                   "RETURNING user_id, clock_timestamp()")


def set_actor(t: Tx, actor: Engine, reason: str) -> None:
    """Set the two settings the state trigger requires, for the rest of this transaction only
    (set_config(..., true), never the session). Used by E2's INSERT of a new job, which writes its
    first history row, and by transition()."""
    t.execute("SELECT set_config('app.actor', %s, true), set_config('app.reason', %s, true)", (actor, reason))


def clear_actor(t: Tx) -> None:
    """Empty both settings, so a later raw write of jobs.state in this transaction fails with JT002
    instead of passing the guard with stale values."""
    t.execute("SELECT set_config('app.actor', '', true), set_config('app.reason', '', true)")


def transition(t: Tx, job_id: int, to_state: JobState, actor: Engine, reason: str,
               extra: Mapping[str, object] | None = None, *, expected: JobState | None = None) -> datetime:
    """Move job job_id to to_state inside the caller's open transaction t and return the database
    time of the move. It never opens or commits a transaction and never writes history.

    expected: pass the state the caller's decision was based on; the move then happens only if
    the job is still in it (D-65).

    Each refusal leaves t usable, with nothing written and app.actor and app.reason empty:
    IllegalTransition when the trigger refuses the move (SQLSTATE JT001) or the job has left
    expected, logged as illegal_transition because a refused move is the caller's case, not a
    fault; NotFound when there is no such job. RuntimeError, before anything runs, when t is not
    inside an open transaction. Every other database error is re-raised unchanged (JT002 for an
    empty actor or reason; a deadlock, which retrying() repeats).

    On success the job_state event is published once the transaction commits, never after a
    rollback. extra adds fields to it but never replaces type, job_id, user_id, state, reason or at
    (D-62)."""
    conn = t.cur.connection
    if conn.info.transaction_status != TransactionStatus.INTRANS:
        raise RuntimeError(f"transition(): job {job_id} needs the caller's open transaction")
    with conn.cursor() as cur:  # its own cursor: a caller iterating its own result loses nothing
        row = _move(t, Tx(cur), job_id, to_state, actor, reason, expected)
    user_id: str = row[0]
    at: datetime = row[1]
    event: dict[str, Any] = {**(extra or {}), "type": "job_state", "job_id": job_id, "user_id": user_id,
                             "state": to_state, "reason": reason, "at": at.isoformat()}
    # Message 12, only once COMMIT is done. A partial, not a lambda: if the push fails, tx() logs
    # the hook's repr, which then names the job (D-47).
    t.after_commit(partial(_publish, event))
    return at


def _publish(event: dict[str, Any]) -> None:
    engines().e4.publish(event)


def _move(t: Tx, own: Tx, job_id: int, to_state: JobState, actor: Engine, reason: str,
          expected: JobState | None) -> TupleRow:
    """The UPDATE of transition() on its own cursor own, inside the caller's transaction t;
    returns (user_id, database time) or raises as transition() documents."""
    conn = t.cur.connection
    try:
        with t.savepoint():  # an illegal move leaves the transaction usable (D-48, D-67)
            set_actor(own, actor, reason)
            if expected is None:
                row = own.execute(_MOVE, (to_state, job_id)).fetchone()
            else:
                row = own.execute(_MOVE_IF, (to_state, job_id, expected)).fetchone()
    except psycopg.Error as e:
        if e.sqlstate != "JT001":
            raise
        raise _refused(job_id, to_state, actor, e.diag.message_primary or str(e)) from e
    finally:
        # The savepoint is released or rolled back by now, so this runs on every path that leaves
        # the transaction usable (D-61). On a lost connection the settings died with the session,
        # and clearing them would only replace the real error.
        if conn.info.transaction_status == TransactionStatus.INTRANS:
            clear_actor(own)
    if row is None:
        found = None if expected is None else own.execute(
            "SELECT state FROM jobs WHERE job_id = %s", (job_id,)).fetchone()
        if found is None:
            raise NotFound(f"job {job_id}")
        raise _refused(job_id, to_state, actor,
                       f"job {job_id} was not {expected} when the move to {to_state} ran (it is {found[0]} now)")
    return row


def _refused(job_id: int, to_state: str, actor: str, message: str) -> IllegalTransition:
    log.warning("illegal_transition", job_id=job_id, to=to_state, actor=actor, error=message)
    return IllegalTransition(message)
