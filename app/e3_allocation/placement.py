"""Placement (plan 6.2): the fit check and the best-fit GPU, as pure functions over resources rows.

Free capacity is total - reserved - allocated in integer units (millicores, MiB), so the reserve of
ceil(10% of total) is never offered. fit() decides on the rows allocate() has locked (E3-2), and
snapshot_of() builds the Snapshot that E2 pre-checks with Snapshot.fits(). Both use free(); the
comparisons are written twice, in fit() and in Snapshot.fits(), and tests/e3/test_placement.py checks
that the two agree on the same rows (D-69).
"""
from __future__ import annotations

from typing import Literal, NamedTuple

from app.common.types import Snapshot, Vector

Kind = Literal["cpu", "ram", "gpu"]


class Row(NamedTuple):
    """One resources row, in the column order of every E3 query."""
    resource_id: int
    kind: Kind
    gpu_index: int | None  # set on GPU rows only (resources_gpu_index_ck)
    total: int
    reserved: int
    allocated: int


def free(r: Row) -> int:
    return r.total - r.reserved - r.allocated


def _one(rows: list[Row], kind: Kind) -> Row:
    """The cpu or the ram row; the schema allows at most one of each (resources_kind_gpu_uk)."""
    for r in rows:
        if r.kind == kind:
            return r
    raise ValueError(f"resources has no {kind} row: load the capacity with db/seed.sql or scripts/capacity.py")


def fit(rows: list[Row], need: Vector) -> tuple[list[tuple[int, int]], int | None] | None:
    """Where need goes on rows: ([(resource_id, amount)] in resource_id order, gpu_index), or None when
    it does not fit. All of need.vram_mib goes on one GPU: the one with the least free VRAM that still
    fits it (best fit), a tie going to the lower resource_id. No GPU when need.vram_mib is 0."""
    cpu, ram = _one(rows, "cpu"), _one(rows, "ram")
    if need.cpu_millicores > free(cpu) or need.ram_mib > free(ram):
        return None
    items = [(cpu.resource_id, need.cpu_millicores), (ram.resource_id, need.ram_mib)]
    gpu_index = None
    if need.vram_mib > 0:
        fitting = [r for r in rows if r.kind == "gpu" and free(r) >= need.vram_mib]
        if not fitting:
            return None
        best = min(fitting, key=lambda r: (free(r), r.resource_id))
        items.append((best.resource_id, need.vram_mib))
        gpu_index = best.gpu_index
    return sorted(items), gpu_index


def snapshot_of(rows: list[Row]) -> Snapshot:
    """Free capacity per resource: cpu_free, ram_free, and gpu_free keyed by gpu_index."""
    return Snapshot(cpu_free=free(_one(rows, "cpu")), ram_free=free(_one(rows, "ram")),
                    gpu_free={r.gpu_index: free(r) for r in rows if r.gpu_index is not None})
