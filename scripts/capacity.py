"""Write a capacity profile into the resources table (plan 6.5, slice A-3).

    python -m scripts.capacity [--profile s0|host]        (from the repository root)

s0 is the S0 benchmark machine: 32,000 millicores, 131,072 MiB of RAM and two GPUs of 16,384 MiB.
host reads this machine: os.cpu_count(), MemTotal from /proc/meminfo, and the GPUs that GPU_MODE
names (none; two simulated GPUs of 16,384 MiB; or nvidia-smi). The profile defaults to
CAPACITY_PROFILE. Every row gets reserved = ceil(10% of total), as resources_reserve_ck requires.

The script refuses to run while any allocation is open, and never removes a GPU row that allocation
history still names. Run it while the server is stopped: E3 caches the resource ids at startup.
"""
import argparse
import os
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

import psycopg
from psycopg.conninfo import conninfo_to_dict

from app.config import CHOICES, Config

S0_CPU_MILLICORES = 32000
S0_RAM_MIB = 131072
S0_VRAM_MIB = 16384          # each of the two S0 GPUs
SIMULATED_VRAM_MIB = 16384   # each of the two GPUs of GPU_MODE=simulated
NVIDIA_SMI = ["nvidia-smi", "--query-gpu=index,memory.total", "--format=csv,noheader,nounits"]
NVIDIA_SMI_TIMEOUT_S = 10

LOCK_ALL = "SELECT resource_id FROM resources ORDER BY resource_id FOR UPDATE"
ANY_OPEN = "SELECT EXISTS (SELECT 1 FROM allocations WHERE status = 'open')"
CURRENT = "SELECT resource_id, kind, gpu_index FROM resources"
USED = ("SELECT DISTINCT r.gpu_index FROM resources r JOIN allocation_items i USING (resource_id) "
        "WHERE r.resource_id = ANY(%s) ORDER BY r.gpu_index")
UPDATE = "UPDATE resources SET total = %s, reserved = %s WHERE resource_id = %s"
INSERT = "INSERT INTO resources (kind, gpu_index, total, reserved) VALUES (%s, %s, %s, %s)"
DELETE = "DELETE FROM resources WHERE resource_id = ANY(%s)"
FREE = ("SELECT resource_id, kind, gpu_index, total, reserved, total - reserved - allocated "
        "FROM resources ORDER BY resource_id")


class CapacityError(Exception):
    """The profile cannot be read or applied. Raised before any write, so nothing changes."""


def reserve(total: int) -> int:
    """ceil(10% of total) in integers, the rule resources_reserve_ck enforces (invariant 7)."""
    return (total + 9) // 10


@dataclass(frozen=True)
class Row:
    """One wanted resources row: cpu in millicores, ram and gpu in MiB."""
    kind: str
    gpu_index: int | None
    total: int

    @property
    def reserved(self) -> int:
        return reserve(self.total)


class Free(NamedTuple):
    """One resources row after the write; free = total - reserved - allocated."""
    resource_id: int
    kind: str
    gpu_index: int | None
    total: int
    reserved: int
    free: int


def s0_rows() -> list[Row]:
    return [Row("cpu", None, S0_CPU_MILLICORES), Row("ram", None, S0_RAM_MIB),
            Row("gpu", 0, S0_VRAM_MIB), Row("gpu", 1, S0_VRAM_MIB)]


def host_rows(gpu_mode: str, cpu_count: int | None = None,
              meminfo_path: Path = Path("/proc/meminfo"),
              nvidia_smi: Callable[[], str] | None = None) -> list[Row]:
    """The rows of this machine. Every outside call happens here, before any transaction."""
    cpus = os.cpu_count() if cpu_count is None else cpu_count
    if not cpus:
        raise CapacityError("the number of CPUs is unknown")
    rows = [Row("cpu", None, cpus * 1000), Row("ram", None, _mem_total_mib(meminfo_path))]
    if gpu_mode == "simulated":
        rows += [Row("gpu", index, SIMULATED_VRAM_MIB) for index in (0, 1)]
    elif gpu_mode == "nvidia":
        rows += _nvidia_gpus(nvidia_smi or _run_nvidia_smi)
    return rows


def _mem_total_mib(path: Path) -> int:
    try:
        lines = path.read_text(encoding="ascii").splitlines()
    except OSError as exc:
        raise CapacityError(f"cannot read {path} ({exc.strerror}); --profile host needs Linux") from exc
    for line in lines:
        name, _, value = line.partition(":")
        fields = value.split()
        if name == "MemTotal" and len(fields) == 2 and fields[1] == "kB" and fields[0].isdigit():
            return int(fields[0]) // 1024
    raise CapacityError(f"{path} has no MemTotal line in kB")


def _run_nvidia_smi() -> str:
    return subprocess.run(NVIDIA_SMI, capture_output=True, text=True, check=True,
                          timeout=NVIDIA_SMI_TIMEOUT_S).stdout


def _output_of(exc: Exception) -> str:
    """What a failed nvidia-smi printed, on one line. A timeout keeps bytes even with text=True."""
    streams = (getattr(exc, "stdout", None), getattr(exc, "stderr", None))
    text = " ".join(s.decode(errors="replace") if isinstance(s, bytes) else s for s in streams if s)
    return " ".join(text.split())


def _nvidia_gpus(run: Callable[[], str]) -> list[Row]:
    try:
        output = run()
    except (OSError, subprocess.SubprocessError) as exc:
        said = _output_of(exc)
        raise CapacityError(f"GPU_MODE is nvidia, but nvidia-smi failed: {exc}"
                            + (f" It said: {said}" if said else "")) from exc
    gpus = []
    for line in filter(str.strip, output.splitlines()):
        try:
            index, vram_mib = (int(field) for field in line.split(","))
        except ValueError:
            raise CapacityError(f"cannot read this nvidia-smi line: {line!r}") from None
        gpus.append(Row("gpu", index, vram_mib))
    if not gpus:
        raise CapacityError("GPU_MODE is nvidia, but nvidia-smi listed no GPU (D-44); "
                            "on a machine without GPUs use GPU_MODE=none")
    return gpus


def apply(conn: psycopg.Connection, rows: list[Row]) -> list[Free]:
    """Make the resources rows match rows, inside the caller's transaction, which already acts as
    e3_role. Every resources row is locked first, in resource_id order (invariant 2), so no
    allocation can start or commit between the checks and the writes. Rows are matched by kind and
    gpu_index and updated in place, so resource ids never change. Raises CapacityError before
    any write."""
    conn.execute(LOCK_ALL)
    if conn.execute(ANY_OPEN).fetchone()[0]:
        raise CapacityError("an allocation is open; let every job end, then stop the server")
    # Read again after the lock: this also sees rows that another run added while this one waited.
    current = {(kind, gpu_index): rid for rid, kind, gpu_index in conn.execute(CURRENT)}
    wanted = {(row.kind, row.gpu_index): row for row in rows}
    gone = [rid for key, rid in current.items() if key[0] == "gpu" and key not in wanted]
    used = [str(gpu) for (gpu,) in conn.execute(USED, (gone,))] if gone else []
    if used:
        raise CapacityError(f"cannot remove GPU {', '.join(used)}: allocation_items still name the row; "
                            "keep it in the profile")
    for key, row in wanted.items():
        if key in current:
            conn.execute(UPDATE, (row.total, row.reserved, current[key]))
        else:
            conn.execute(INSERT, (row.kind, row.gpu_index, row.total, row.reserved))
    if gone:
        conn.execute(DELETE, (gone,))
    return [Free(*row) for row in conn.execute(FREE)]


def write(database_url: str, rows: list[Row]) -> list[Free]:
    """Apply rows in one transaction of its own as e3_role (D-41). The transaction rolls back on any
    exception, and nothing runs outside it. Slice A-4 replaces this connection with tx("E3")."""
    with psycopg.connect(database_url, autocommit=True, connect_timeout=10) as conn:
        with conn.transaction():
            conn.execute("SET LOCAL ROLE e3_role")
            return apply(conn, rows)


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.capacity",
                                     description="Write a capacity profile into the resources table.")
    parser.add_argument("--profile", choices=CHOICES["capacity_profile"],
                        help="s0: the S0 benchmark machine; host: this machine (default: CAPACITY_PROFILE)")
    args = parser.parse_args(argv)
    try:
        cfg = Config.from_env() if env is None else Config.from_env(env)
    except ValueError as exc:  # a bad setting is a usage error, like a bad argument
        print(f"capacity.py: {exc}", file=sys.stderr)
        return 2
    profile = args.profile or cfg.capacity_profile
    database = conninfo_to_dict(cfg.database_url).get("dbname")
    try:
        rows = s0_rows() if profile == "s0" else host_rows(cfg.gpu_mode)
        free = write(cfg.database_url, rows)
    except CapacityError as exc:
        print(f"capacity.py: {exc}. Nothing was changed.", file=sys.stderr)
        return 1
    except psycopg.OperationalError as exc:
        print(f"capacity.py: database {database}: {' '.join(str(exc).split())}", file=sys.stderr)
        return 1
    print(f"Capacity profile {profile} written to the database {database}:")
    for row in free:
        name = row.kind if row.gpu_index is None else f"gpu {row.gpu_index}"
        unit = "millicores" if row.kind == "cpu" else "MiB"
        print(f"  resource {row.resource_id} {name}: total {row.total}, reserved {row.reserved}, "
              f"free {row.free} {unit}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
