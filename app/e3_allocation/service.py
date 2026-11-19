"""E3 Transaction-Safe Resource Allocation (plan 6): snapshot() from slice E3-1; allocate() and release()
follow in E3-2 and E3-3.
"""
from __future__ import annotations

from app.common.db import tx
from app.common.types import Snapshot
from app.e3_allocation.placement import snapshot_of
from app.e3_allocation.queries import read_resources


def snapshot() -> Snapshot:
    """Free capacity now (message 3, E3Port.snapshot): one read without a lock in one tx("E3"). It can be
    stale by the time E2 uses it; allocate() re-checks inside its row locks, which makes that harmless.
    Raises ValueError when the cpu or the ram row is missing, that is when no capacity is loaded."""
    with tx("E3") as t:
        rows = read_resources(t)
    return snapshot_of(rows)
