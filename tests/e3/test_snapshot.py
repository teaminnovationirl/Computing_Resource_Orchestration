"""E3-1 snapshot() (plan 6.1, 6.6) on real PostgreSQL: free = total - reserved - allocated per row, read in
one tx("E3") without a lock. The S0 seed gives 28,800 millicores, 117,964 MiB and 14,745 MiB per GPU.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait

import psycopg
import pytest

from app.common.db import tx
from app.common.types import Snapshot
from app.e3_allocation import snapshot
from tests.helpers import ROOT, TempDb, assert_invariants
from tests.stubs import s0_free

S0_FREE = Snapshot(cpu_free=28800, ram_free=117964, gpu_free={0: 14745, 1: 14745})


@pytest.fixture
def s0(db: TempDb) -> TempDb:
    """A fresh database with db/seed.sql loaded: resources 1 cpu, 2 ram, 3 gpu 0 and 4 gpu 1 (S0)."""
    with psycopg.connect(db.owner_url, autocommit=True) as conn:
        conn.execute((ROOT / "db" / "seed.sql").read_bytes())
    return db


def _set_allocated(amounts: dict[int, int]) -> None:
    with tx("E3") as cur:
        for resource_id, amount in amounts.items():
            cur.execute("UPDATE resources SET allocated = %s WHERE resource_id = %s", (amount, resource_id))


def test_the_s0_seed_gives_the_baseline_free_capacity(s0: TempDb) -> None:
    """Catches GPUs keyed by resource_id, a wrong column, or a row left out. The stub that E2's tests
    use must give the same numbers."""
    assert snapshot() == S0_FREE == s0_free()
    assert_invariants()


def test_free_subtracts_what_is_allocated_on_each_row(s0: TempDb) -> None:
    """Catches allocated left out, and VRAM held on one GPU charged to the other."""
    _set_allocated({1: 1500, 2: 1000, 4: 2000})  # CPU, RAM and the GPU with gpu_index 1
    assert snapshot() == Snapshot(cpu_free=27300, ram_free=116964, gpu_free={0: 14745, 1: 12745})
    _set_allocated({1: 0, 2: 0, 4: 0})  # no allocation holds these amounts
    assert_invariants()


def test_snapshot_takes_no_lock_and_sees_only_committed_rows(s0: TempDb) -> None:
    """Catches any row lock in the snapshot query, FOR KEY SHARE included: E2's admission loop would
    then queue behind every allocate() that holds the row. The holder takes allocate()'s FOR UPDATE,
    which conflicts with every row lock mode."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        with psycopg.connect(s0.owner_url) as holder:
            holder.execute("SELECT 1 FROM resources WHERE resource_id = 1 FOR UPDATE")
            holder.execute("UPDATE resources SET allocated = 1000 WHERE resource_id = 1")  # not committed
            future = pool.submit(snapshot)
            done, _ = wait([future], timeout=10)  # milliseconds, unless snapshot() waits for the lock
            holder.rollback()  # lets a waiting snapshot() finish
    assert done, "snapshot() waited for the row lock of an uncommitted UPDATE"
    assert future.result() == S0_FREE  # the uncommitted 1,000 is not seen
    assert_invariants()


def test_no_capacity_loaded_raises(db: TempDb) -> None:
    """Catches a snapshot() that reports zero capacity for an empty resources table: E2 would then hold
    every job without a word (D-69)."""
    with pytest.raises(ValueError, match="cpu"):
        snapshot()
