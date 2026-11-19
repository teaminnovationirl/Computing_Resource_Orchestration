"""E3-1 placement (plan 6.2, 6.6): the fit check and the best-fit GPU, as pure functions; no database.

Expected values are written by hand from the baseline: free = total - reserved - allocated, the reserve
is ceil(10% of total), and S0 is CPU 32,000 millicores, RAM 131,072 MiB and two GPUs of 16,384 MiB.
"""
from __future__ import annotations

import random
from collections.abc import Iterator

import pytest

from app.common.types import Snapshot, Vector
from app.e3_allocation.placement import Kind, Row, fit, snapshot_of

# The S0 rows of db/seed.sql: resource_id, kind, gpu_index, total, reserved, allocated.
CPU = Row(1, "cpu", None, 32000, 3200, 0)    # 28,800 millicores free
RAM = Row(2, "ram", None, 131072, 13108, 0)  # 117,964 MiB free
GPU0 = Row(3, "gpu", 0, 16384, 1639, 0)      # 14,745 MiB free
GPU1 = Row(4, "gpu", 1, 16384, 1639, 0)
S0 = [CPU, RAM, GPU0, GPU1]


def gpu(resource_id: int, gpu_index: int, free: int) -> Row:
    """A 16,384 MiB GPU row (reserve 1,639, so 14,745 allocatable) with free MiB left."""
    return Row(resource_id, "gpu", gpu_index, 16384, 1639, 14745 - free)


def test_the_reserve_is_never_offered() -> None:
    """Catches free() without the reserve, and an exact fit (need == free) refused."""
    assert fit(S0, Vector(28800, 117964)) == ([(1, 28800), (2, 117964)], None)
    assert fit(S0, Vector(28801, 64)) is None
    assert fit(S0, Vector(1000, 117965)) is None
    assert fit(S0, Vector(1000, 512, 14745)) == ([(1, 1000), (2, 512), (3, 14745)], 0)
    assert fit(S0, Vector(1000, 512, 14746)) is None


def test_no_gpu_when_vram_is_zero_even_with_every_gpu_full() -> None:
    """Catches a GPU item of 0 MiB (allocation_items.amount > 0 rejects it) and a refusal because no GPU
    has room."""
    rows = [CPU, RAM, gpu(3, 0, free=0), gpu(4, 1, free=0)]
    assert fit(rows, Vector(2000, 1024)) == ([(1, 2000), (2, 1024)], None)


def test_best_fit_takes_the_gpu_with_the_least_free_vram_that_fits() -> None:
    """Catches worst fit, first fit, the least free GPU that does not fit, and > for >= on VRAM."""
    rows = [CPU, RAM, gpu(3, 0, free=9000), gpu(4, 1, free=4000), gpu(5, 2, free=6000)]
    assert fit(rows, Vector(1000, 512, 5000)) == ([(1, 1000), (2, 512), (5, 5000)], 2)
    assert fit(rows, Vector(1000, 512, 6000)) == ([(1, 1000), (2, 512), (5, 6000)], 2)
    assert fit(rows, Vector(1000, 512, 3000)) == ([(1, 1000), (2, 512), (4, 3000)], 1)


def test_a_tie_goes_to_the_lower_resource_id() -> None:
    """Catches a tie broken by list order or by gpu_index: the higher resource_id comes first and holds
    the lower gpu_index."""
    rows = [CPU, RAM, gpu(6, 0, free=8000), gpu(3, 1, free=8000)]
    assert fit(rows, Vector(1000, 512, 4000)) == ([(1, 1000), (2, 512), (3, 4000)], 1)


def test_items_come_in_resource_id_order() -> None:
    """Catches a missing sort: here the GPU has a lower resource_id than the cpu and ram rows."""
    rows = [Row(5, "cpu", None, 32000, 3200, 0), Row(6, "ram", None, 131072, 13108, 0), gpu(2, 0, free=8000)]
    assert fit(rows, Vector(1000, 512, 100)) == ([(2, 100), (5, 1000), (6, 512)], 0)


def test_no_placement_when_no_single_gpu_fits() -> None:
    """Catches VRAM summed across GPUs, and min() over no GPU rows (GPU_MODE=none)."""
    split = [CPU, RAM, gpu(3, 0, free=3000), gpu(4, 1, free=3000)]
    assert fit(split, Vector(1000, 512, 5000)) is None
    assert fit([CPU, RAM], Vector(1000, 512, 1)) is None


@pytest.mark.parametrize(("rows", "kind"), [([RAM, GPU0], "cpu"), ([CPU, GPU0], "ram")])
def test_a_missing_cpu_or_ram_row_raises(rows: list[Row], kind: str) -> None:
    """Catches a StopIteration, and a zero capacity that would stall admission without a word."""
    with pytest.raises(ValueError, match=kind):
        fit(rows, Vector(1000, 512))
    with pytest.raises(ValueError, match=kind):
        snapshot_of(rows)


def test_snapshot_of_keys_each_gpu_by_its_gpu_index() -> None:
    """Catches gpu_free keyed by resource_id or by position: here resource_id order is opposite to
    gpu_index order."""
    rows = [CPU, RAM, gpu(3, 1, free=5000), gpu(6, 0, free=8000)]
    assert snapshot_of(rows) == Snapshot(cpu_free=28800, ram_free=117964, gpu_free={0: 8000, 1: 5000})


def _row(rng: random.Random, resource_id: int, kind: Kind, gpu_index: int | None, top: int) -> Row:
    """A row that passes every resources CHECK: empty, full, or anywhere between."""
    total = rng.randint(1024, top)
    reserved = (total + 9) // 10
    room = total - reserved
    allocated = rng.choice([0, room, rng.randint(0, room), rng.randint(0, room)])
    return Row(resource_id, kind, gpu_index, total, reserved, allocated)


def _cases(rng: random.Random, count: int) -> Iterator[tuple[list[Row], Vector]]:
    """Shuffled rows and a need that passes the jobs CHECKs. Each case puts one need (CPU, RAM or VRAM)
    on the edge of its free capacity, at free - 1, free or free + 1, and the others inside theirs, so
    each comparison decides a share of the cases."""
    for _ in range(count):
        rows = [_row(rng, 1, "cpu", None, 64000), _row(rng, 2, "ram", None, 262144)]
        rows += [_row(rng, 3 + i, "gpu", i, 32768) for i in range(rng.randint(0, 3))]
        rng.shuffle(rows)
        free = {r.resource_id: r.total - r.reserved - r.allocated for r in rows}
        gpus = [free[r.resource_id] for r in rows if r.kind == "gpu"]
        top = max(gpus, default=0)  # the largest free VRAM, the edge of "some GPU fits"
        edge = rng.choice(["cpu", "ram", "vram"])
        step = rng.choice([-1, 0, 1])
        cpu = free[1] + step if edge == "cpu" else rng.randint(1, max(1, free[1]))
        ram = free[2] + step if edge == "ram" else rng.randint(64, max(64, free[2]))
        if edge == "vram":
            vram = rng.choice([top, rng.choice(gpus)]) + step if gpus else rng.randint(1, 20000)
        else:
            vram = rng.choice([0, rng.randint(1, top)]) if top else 0
        yield rows, Vector(max(1, cpu), max(64, ram), max(0, vram))


def test_fit_agrees_with_the_snapshot_e2_admits_by() -> None:
    """E2 admits with snapshot().fits(v) and E3 re-checks with fit(). Catches the two disagreeing on the
    same rows: E2 would admit jobs that E3 refuses, or never admit jobs that E3 would place."""
    for rows, need in _cases(random.Random(311), 1000):
        assert (fit(rows, need) is not None) == snapshot_of(rows).fits(need), (rows, need)
