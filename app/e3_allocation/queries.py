"""Every SQL statement of E3 (plan 3.4). Each function runs one statement on the caller's Tx, inside a
tx("E3") the caller owns, and returns typed rows.
"""
from __future__ import annotations

from typing import Final, Literal, NamedTuple

from app.common.db import Tx
from app.e3_allocation.placement import Row

# Row(*r) maps the columns by position, so every query that returns resources rows starts with this.
_ROWS: Final = "SELECT resource_id, kind, gpu_index, total, reserved, allocated FROM resources"


class Job(NamedTuple):
    """What allocate() and release() read of their job: the state each requires and what E1 needs for
    the dispatch."""
    state: str
    time_limit_s: int
    payload_path: str


def read_resources(t: Tx) -> list[Row]:
    """Every resources row in resource_id order, without a lock (D-68). One statement, so all rows come
    from one committed state under READ COMMITTED. No LIMIT: the table holds one cpu row, one ram row and
    one row per GPU, and a row left out would hide capacity. Inside allocate() these rows only name the
    ids to lock: fit() decides on the rows the lock statement returns (invariant 2)."""
    t.execute(_ROWS + " ORDER BY resource_id")
    return [Row(*r) for r in t.fetchall()]


def lock_resources(t: Tx, ids: list[int]) -> list[Row]:
    """The one lock statement of allocate() (invariant 2): FOR UPDATE on the rows that ids names. The
    locking clause applies after ORDER BY, so the rows are locked in resource_id order and two callers
    never wait on each other in a cycle. Under READ COMMITTED each row comes back in its latest
    committed version once its lock is held; a row deleted since the caller read the ids is not
    returned. No LIMIT: every named row must be locked."""
    t.execute(_ROWS + " WHERE resource_id = ANY(%s) ORDER BY resource_id FOR UPDATE", (ids,))
    return [Row(*r) for r in t.fetchall()]


def read_job(t: Tx, job_id: int) -> Job | None:
    """The job's state, time limit and payload path, read without a lock: transition() locks the job
    row later, after the resource rows, or concurrent callers could deadlock."""
    t.execute("SELECT state, time_limit_s, payload_path FROM jobs WHERE job_id = %s", (job_id,))
    row = t.fetchone()
    return None if row is None else Job(*row)


def add_allocated(t: Tx, items: list[tuple[int, int]]) -> None:
    """Charge every (resource_id, amount) to resources.allocated in one statement. The caller holds the
    row locks; resources_allocated_ck refuses anything past total - reserved."""
    t.execute("UPDATE resources AS r SET allocated = r.allocated + i.amount "
              "FROM unnest(%s::int[], %s::int[]) AS i (resource_id, amount) WHERE r.resource_id = i.resource_id",
              ([resource_id for resource_id, _ in items], [amount for _, amount in items]))


def insert_allocation(t: Tx, job_id: int) -> int:
    """A new open allocation of job_id; returns its allocation_id. allocations_one_open_uk allows one
    open allocation per job."""
    t.execute("INSERT INTO allocations (job_id) VALUES (%s) RETURNING allocation_id", (job_id,))
    row = t.fetchone()
    if row is None:  # RETURNING always gives the inserted row
        raise RuntimeError(f"INSERT INTO allocations returned no row for job {job_id}")
    return row[0]


def insert_items(t: Tx, allocation_id: int, items: list[tuple[int, int]]) -> None:
    """One allocation_items row per (resource_id, amount), in one statement."""
    t.execute("INSERT INTO allocation_items (allocation_id, resource_id, amount) "
              "SELECT %s, i.resource_id, i.amount FROM unnest(%s::int[], %s::int[]) AS i (resource_id, amount)",
              (allocation_id, [resource_id for resource_id, _ in items], [amount for _, amount in items]))


def insert_audit(t: Tx, job_id: int, allocation_id: int | None, outcome: Literal["committed", "refused"]) -> None:
    """The allocate audit row (FR-06): committed names its allocation, refused names none
    (audit_refused_ck). The table is append-only (AO001)."""
    t.execute("INSERT INTO allocation_audit (job_id, allocation_id, action, outcome) "
              "VALUES (%s, %s, 'allocate', %s)", (job_id, allocation_id, outcome))


def lock_open_allocation(t: Tx, job_id: int) -> int | None:
    """Lock 1 of release() (invariant 2): the job's open allocation, FOR UPDATE, found through
    allocations_one_open_uk; None when the job holds none. A second release() of the job waits here
    until the first commits and then, under READ COMMITTED, finds the allocation closed: the open
    allocation is the key that makes release() idempotent (context §7)."""
    t.execute("SELECT allocation_id FROM allocations WHERE job_id = %s AND status = 'open' FOR UPDATE", (job_id,))
    row = t.fetchone()
    return None if row is None else row[0]


def read_items(t: Tx, allocation_id: int) -> list[tuple[int, int]]:
    """The allocation's (resource_id, amount) items in resource_id order, by primary key. Items never
    change once written, and the caller holds the allocation's lock."""
    t.execute("SELECT resource_id, amount FROM allocation_items WHERE allocation_id = %s ORDER BY resource_id",
              (allocation_id,))
    return [(resource_id, amount) for resource_id, amount in t.fetchall()]


def subtract_allocated(t: Tx, items: list[tuple[int, int]]) -> None:
    """Give every (resource_id, amount) back to resources.allocated in one statement. The caller holds
    the row locks; resources_allocated_ck refuses a result below 0, so nothing is given back twice
    unnoticed."""
    t.execute("UPDATE resources AS r SET allocated = r.allocated - i.amount "
              "FROM unnest(%s::int[], %s::int[]) AS i (resource_id, amount) WHERE r.resource_id = i.resource_id",
              ([resource_id for resource_id, _ in items], [amount for _, amount in items]))


def close_allocation(t: Tx, allocation_id: int) -> None:
    """Mark the allocation released at the database time; allocations_released_ck pairs the two."""
    t.execute("UPDATE allocations SET status = 'released', released_at = clock_timestamp() "
              "WHERE allocation_id = %s", (allocation_id,))


def insert_release_audit(t: Tx, job_id: int, allocation_id: int) -> None:
    """The release audit row (FR-06): always committed and naming its allocation (audit_refused_ck,
    audit_refused_allocate_ck). The table is append-only (AO001)."""
    t.execute("INSERT INTO allocation_audit (job_id, allocation_id, action, outcome) "
              "VALUES (%s, %s, 'release', 'committed')", (job_id, allocation_id))
