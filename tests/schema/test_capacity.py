"""scripts/capacity.py: the capacity profiles s0 and host (plan 6.5; decisions D-41 to D-44; slice A-3).

The rolled-back tests act as E3 the way the script does. The race test commits on connections of
its own and removes what it created.
"""
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from scripts import capacity

ROOT = Path(__file__).resolve().parents[2]
RACES = 100
# (resource_id, kind, gpu_index, total, reserved, free) of the S0 seed: 1 CPU, 2 RAM, 3 and 4 GPUs
S0 = [(1, "cpu", None, 32000, 3200, 28800), (2, "ram", None, 131072, 13108, 117964),
      (3, "gpu", 0, 16384, 1639, 14745), (4, "gpu", 1, 16384, 1639, 14745)]
ROWS = ("SELECT resource_id, kind, gpu_index, total, reserved, total - reserved - allocated "
        "FROM resources ORDER BY resource_id")
NEW_ALLOCATION = "INSERT INTO allocations (job_id) VALUES (%s) RETURNING allocation_id"
INVARIANTS = (ROOT / "db" / "invariants.sql").read_text(encoding="utf-8").split("-- name: leaks")[0]
WAITING_ON = "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE %s = ANY (pg_blocking_pids(pid)))"
MEMINFO = "MemTotal:       16333128 kB\nMemFree:         1048576 kB\nMemAvailable:    8388608 kB\n"
HOST_CPU_RAM = [("cpu", None, 8000, 800), ("ram", None, 15950, 1595)]  # 8 CPUs; 16,333,128 kB = 15,950 MiB


def _shape(rows) -> list[tuple]:
    return [(row.kind, row.gpu_index, row.total, row.reserved) for row in rows]


def _no_nvidia_smi() -> str:
    raise AssertionError("nvidia-smi may run only when GPU_MODE=nvidia")


def _raises(error: Exception):
    def run() -> str:
        raise error
    return run


@pytest.mark.parametrize("total, reserved", [(32000, 3200), (131072, 13108), (16384, 1639),
                                             (1, 1), (10, 1), (11, 2)])
def test_reserve_is_ceil_of_ten_percent(total, reserved):
    """Catches floor or round, which resources_reserve_ck refuses at run time (23514)."""
    assert capacity.reserve(total) == reserved


def test_s0_on_the_seed_changes_nothing(app_tx):
    """Catches S0 numbers drifting from db/seed.sql, and delete-and-reinsert renumbering the rows
    that fix the lock order (1 CPU, 2 RAM, 3 and 4 GPUs)."""
    app_tx.role("e3_role")
    assert capacity.apply(app_tx.conn, capacity.s0_rows()) == S0
    assert app_tx.all(ROWS) == S0


@pytest.mark.parametrize("gpu_mode, nvidia_output, gpus", [
    ("none", None, []),
    ("simulated", None, [("gpu", 0, 16384, 1639), ("gpu", 1, 16384, 1639)]),
    ("nvidia", "0, 49140\n1, 49140\n", [("gpu", 0, 49140, 4914), ("gpu", 1, 49140, 4914)]),
])
def test_host_rows_read_the_machine(tmp_path, gpu_mode, nvidia_output, gpus):
    """Catches kB taken for MiB, float amounts, the GPU modes mixed up, and nvidia-smi run
    outside GPU_MODE=nvidia."""
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(MEMINFO)
    smi = _no_nvidia_smi if nvidia_output is None else (lambda: nvidia_output)
    rows = capacity.host_rows(gpu_mode, cpu_count=8, meminfo_path=meminfo, nvidia_smi=smi)
    assert _shape(rows) == HOST_CPU_RAM + gpus
    assert {type(row.total) for row in rows} == {int}


@pytest.mark.parametrize("gpu_mode, meminfo_text, smi", [
    pytest.param("none", None, _no_nvidia_smi, id="no-meminfo"),
    pytest.param("none", "MemFree: 1048576 kB\n", _no_nvidia_smi, id="no-memtotal"),
    pytest.param("nvidia", MEMINFO, _raises(FileNotFoundError(2, "No such file", "nvidia-smi")),
                 id="no-nvidia-smi"),
    pytest.param("nvidia", MEMINFO, _raises(subprocess.CalledProcessError(9, "nvidia-smi")),
                 id="nvidia-smi-failed"),
    pytest.param("nvidia", MEMINFO, lambda: "0, [N/A]\n", id="unreadable-vram"),
    pytest.param("nvidia", MEMINFO, lambda: "", id="no-gpu-listed"),  # D-44
])
def test_host_rows_refuse_what_they_cannot_read(tmp_path, gpu_mode, meminfo_text, smi):
    """Catches raw tracebacks, and an empty nvidia-smi list silently removing every GPU row (D-44)."""
    meminfo = tmp_path / "meminfo"
    if meminfo_text is not None:
        meminfo.write_text(meminfo_text)
    with pytest.raises(capacity.CapacityError):
        capacity.host_rows(gpu_mode, cpu_count=8, meminfo_path=meminfo, nvidia_smi=smi)


def test_extra_gpus_are_added_then_removed_and_ids_stay(app_tx):
    """Catches leftover or duplicate GPU rows. Adding rows as e3_role also proves that the
    identity column needs no sequence grant (D-40)."""
    app_tx.role("e3_role")
    extra = [capacity.Row("gpu", 2, 8192), capacity.Row("gpu", 3, 8192)]
    capacity.apply(app_tx.conn, capacity.s0_rows() + extra)
    rows = app_tx.all(ROWS)
    assert rows[:4] == S0
    assert [row[1:] for row in rows[4:]] == [("gpu", 2, 8192, 820, 7372), ("gpu", 3, 8192, 820, 7372)]
    assert all(row[0] > 4 for row in rows[4:])
    capacity.apply(app_tx.conn, capacity.s0_rows())
    assert app_tx.all(ROWS) == S0


def test_cpu_and_ram_follow_the_profile_and_gpus_go(app_tx):
    """Catches CPU or RAM left at the S0 amounts, and GPU rows kept when the profile has none."""
    app_tx.role("e3_role")
    host = [capacity.Row("cpu", None, 8000), capacity.Row("ram", None, 15950)]
    expected = [(1, "cpu", None, 8000, 800, 7200), (2, "ram", None, 15950, 1595, 14355)]
    assert capacity.apply(app_tx.conn, host) == expected
    assert app_tx.all(ROWS) == expected


def test_a_gpu_with_allocation_history_is_kept(owner_tx):
    """Catches a raw 23503 from the RESTRICT key, and CPU or RAM rewritten before the refusal."""
    job = owner_tx.new_job()
    allocation = owner_tx.one("INSERT INTO allocations (job_id, status, released_at) "
                              "VALUES (%s, 'released', clock_timestamp()) RETURNING allocation_id", (job,))[0]
    owner_tx.run("INSERT INTO allocation_items (allocation_id, resource_id, amount) VALUES (%s, 4, 1024)",
                 (allocation,))
    owner_tx.role("e3_role")
    host = [capacity.Row("cpu", None, 8000), capacity.Row("ram", None, 15950), capacity.Row("gpu", 0, 16384)]
    with pytest.raises(capacity.CapacityError, match="GPU 1"):
        capacity.apply(owner_tx.conn, host)
    assert owner_tx.all(ROWS) == S0


def test_refuses_while_an_allocation_is_open(owner_tx):
    """Catches capacity rewritten under a job that holds resources."""
    owner_tx.one(NEW_ALLOCATION, (owner_tx.new_job(),))
    owner_tx.role("e3_role")
    with pytest.raises(capacity.CapacityError, match="open"):
        capacity.apply(owner_tx.conn, [capacity.Row("cpu", None, 8000), capacity.Row("ram", None, 15950)])
    assert owner_tx.all(ROWS) == S0


def _committed_job(owner: psycopg.Connection) -> int:
    """A queued job, committed, inserted the way E2 submits one."""
    with owner.transaction():
        owner.execute("SELECT set_config('app.actor', 'E2', true), set_config('app.reason', 'submitted', true)")
        return owner.execute(
            "INSERT INTO jobs (user_id, type_code, cpu_millicores, ram_mib, vram_mib, time_limit_s, need_class, "
            "payload_path) VALUES ('01FE24BCS901', 'cpu_bound', 1000, 512, 0, 5, 3, 'payloads/test') "
            "RETURNING job_id").fetchone()[0]


def _remove_job(owner: psycopg.Connection, job: int) -> None:
    """Test cleanup only: job_state_history is append-only (AO001), so this one transaction runs as
    the owner with the triggers off."""
    with owner.transaction():
        owner.execute("SET LOCAL session_replication_role = replica")
        for table in ("allocations", "job_state_history", "jobs"):
            owner.execute(sql.SQL("DELETE FROM {} WHERE job_id = %s").format(sql.Identifier(table)), (job,))


def _allocate_once_capacity_waits(conn, monitor, job, barrier, capacity_done) -> int:
    """Lock CPU and RAM as allocate() does and add an open allocation; commit only once the capacity
    run waits on these locks, or has finished without waiting."""
    with conn.transaction():
        conn.execute("SET LOCAL ROLE e3_role")
        conn.execute("SELECT resource_id FROM resources WHERE resource_id = ANY(%s) "
                     "ORDER BY resource_id FOR UPDATE", ([1, 2],))
        allocation = conn.execute(NEW_ALLOCATION, (job,)).fetchone()[0]
        barrier.wait(timeout=10)
        deadline = time.monotonic() + 10
        while not capacity_done.is_set():
            if monitor.execute(WAITING_ON, (conn.info.backend_pid,)).fetchone()[0]:
                break
            assert time.monotonic() < deadline, "the capacity run neither waited for the locks nor finished"
    return allocation


def _capacity_run(url: str, barrier, done) -> str:
    barrier.wait(timeout=10)
    try:
        capacity.write(url, capacity.s0_rows())
        return "written"
    except capacity.CapacityError as exc:
        return f"refused: {exc}"
    finally:
        done.set()


def test_refusal_holds_when_an_allocation_commits_during_the_lock_wait(urls):
    """Catches check-then-lock: unless the open-allocation check comes after the row locks, an
    allocation that commits while capacity.py waits slips through. 100 runs, separate connections."""
    with (psycopg.connect(urls.owner, autocommit=True) as owner,
          psycopg.connect(urls.owner, autocommit=True) as monitor,
          psycopg.connect(urls.app, autocommit=True) as allocator):
        job = _committed_job(owner)
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                for _ in range(RACES):
                    barrier, done = threading.Barrier(2), threading.Event()
                    held = pool.submit(_allocate_once_capacity_waits, allocator, monitor, job, barrier, done)
                    run = pool.submit(_capacity_run, urls.app, barrier, done)
                    allocation, outcome = held.result(timeout=30), run.result(timeout=30)
                    owner.execute("DELETE FROM allocations WHERE allocation_id = %s", (allocation,))
                    assert outcome.startswith("refused") and "open" in outcome, outcome
        finally:
            _remove_job(owner, job)
        assert owner.execute(ROWS).fetchall() == S0
        assert owner.execute(INVARIANTS).fetchall() == []


def test_cli_writes_through_orch_app_as_e3(urls, capsys):
    """Catches a missing SET LOCAL ROLE e3_role: orch_app alone cannot lock or write resources (42501)."""
    assert capacity.main(["--profile", "s0"], env={"DATABASE_URL": urls.app}) == 0
    out = capsys.readouterr().out
    assert "orch_test" in out
    assert all(str(free) in out for *_, free in S0)


def test_cli_rejects_an_unknown_profile():
    """Catches a mistyped profile falling back to a default instead of stopping."""
    with pytest.raises(SystemExit) as stop:
        capacity.main(["--profile", "s1"], env={})
    assert stop.value.code == 2


def test_write_keeps_nothing_when_it_fails_after_writing(urls, monkeypatch):
    """Catches autocommit, a commit in a finally, or a statement outside the one transaction (D-41)."""
    real_apply = capacity.apply

    def apply_then_fail(conn, rows):
        real_apply(conn, rows)
        raise RuntimeError("forced failure after the writes")

    monkeypatch.setattr(capacity, "apply", apply_then_fail)
    try:
        with pytest.raises(RuntimeError, match="forced failure"):
            capacity.write(urls.app, capacity.s0_rows() + [capacity.Row("gpu", 2, 8192)])
        with psycopg.connect(urls.owner) as conn:
            assert conn.execute(ROWS).fetchall() == S0
    finally:
        with psycopg.connect(urls.owner, autocommit=True) as conn:
            conn.execute("DELETE FROM resources WHERE kind = 'gpu' AND gpu_index = 2")


def test_nvidia_smi_failure_keeps_its_own_message(tmp_path):
    """Catches a refusal that drops what nvidia-smi said, such as a driver it cannot reach."""
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(MEMINFO)
    said = "NVIDIA-SMI has failed because it couldn't communicate with the NVIDIA driver."
    failed = _raises(subprocess.CalledProcessError(9, "nvidia-smi", output=said + "\n", stderr=""))
    with pytest.raises(capacity.CapacityError, match="couldn't communicate with the NVIDIA driver"):
        capacity.host_rows("nvidia", cpu_count=8, meminfo_path=meminfo, nvidia_smi=failed)


def test_cli_reports_a_bad_setting_in_one_line(capsys):
    """Catches a traceback for a bad GPU_MODE, and --help failing because of it."""
    with pytest.raises(SystemExit) as stop:
        capacity.main(["--help"], env={"GPU_MODE": "bogus"})
    assert stop.value.code == 0
    assert capacity.main(["--profile", "s0"], env={"GPU_MODE": "bogus"}) == 2
    err = capsys.readouterr().err
    assert "GPU_MODE" in err and err.count("\n") == 1, err


def test_cli_reports_a_refused_connection_in_one_line(urls, capsys):
    """Catches a psycopg traceback when the database refuses the connection (here a wrong password)."""
    refused = make_conninfo(urls.app, password="wrong")
    assert capacity.main(["--profile", "s0"], env={"DATABASE_URL": refused}) == 1
    err = capsys.readouterr().err
    assert "database orch_test" in err and err.count("\n") == 1, err


def _apply_once_the_other_run_waits(conn, monitor, rows, barrier, other_done) -> None:
    """A first capacity run: apply rows, then commit only once the second run waits on the locks,
    or has finished without waiting."""
    with conn.transaction():
        conn.execute("SET LOCAL ROLE e3_role")
        capacity.apply(conn, rows)
        barrier.wait(timeout=10)
        deadline = time.monotonic() + 10
        while not other_done.is_set():
            if monitor.execute(WAITING_ON, (conn.info.backend_pid,)).fetchone()[0]:
                break
            assert time.monotonic() < deadline, "the second run neither waited for the locks nor finished"


def _second_run(url: str, rows, barrier, done) -> list:
    barrier.wait(timeout=10)
    try:
        return capacity.write(url, rows)
    finally:
        done.set()


def test_a_run_that_waited_updates_the_gpu_rows_the_first_run_added(urls):
    """Catches matching GPUs against the rows the lock returned: a run that waited would miss the
    rows the first run added and insert them again (23505). 100 runs, separate connections."""
    rows = capacity.s0_rows() + [capacity.Row("gpu", 2, 8192), capacity.Row("gpu", 3, 8192)]
    expected = [row[1:] for row in S0] + [("gpu", 2, 8192, 820, 7372), ("gpu", 3, 8192, 820, 7372)]
    remove_extra = "DELETE FROM resources WHERE kind = 'gpu' AND gpu_index IN (2, 3)"
    with (psycopg.connect(urls.owner, autocommit=True) as owner,
          psycopg.connect(urls.owner, autocommit=True) as monitor,
          psycopg.connect(urls.app, autocommit=True) as first):
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                for _ in range(RACES):
                    barrier, done = threading.Barrier(2), threading.Event()
                    held = pool.submit(_apply_once_the_other_run_waits, first, monitor, rows, barrier, done)
                    second = pool.submit(_second_run, urls.app, rows, barrier, done)
                    held.result(timeout=30)
                    assert [row[1:] for row in second.result(timeout=30)] == expected
                    owner.execute(remove_extra)
        finally:
            owner.execute(remove_extra)
        assert owner.execute(ROWS).fetchall() == S0
        assert owner.execute(INVARIANTS).fetchall() == []
