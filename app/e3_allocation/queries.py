"""Every SQL statement of E3 (plan 3.4). Each function runs one statement on the caller's Tx, inside a
tx("E3") the caller owns, and returns typed rows.
"""
from __future__ import annotations

from app.common.db import Tx
from app.e3_allocation.placement import Row


def read_resources(t: Tx) -> list[Row]:
    """Every resources row in resource_id order, without a lock (D-68). One statement, so all rows come
    from one committed state under READ COMMITTED. No LIMIT: the table holds one cpu row, one ram row and
    one row per GPU, and a row left out would hide capacity. Inside allocate() these rows only name the
    ids to lock: fit() decides on the rows the lock statement returns (invariant 2)."""
    t.execute("SELECT resource_id, kind, gpu_index, total, reserved, allocated "
              "FROM resources ORDER BY resource_id")
    return [Row(*r) for r in t.fetchall()]
