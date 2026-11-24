"""Slice E1-1: the paths E1 uses under DATA_DIR (plan 9.1 with R1, D-143 and D-144).

The oracle is plan 9.1 and the owner's plan review: a stored path must resolve inside DATA_DIR; a payload is a
payloads/<32 hex> folder that E2 wrote, with one of five stored names; an attempt gets a folder that did not
exist before, 0700, of which only out/ (0777) is mounted. The stored names come from E2's own tables.
"""
from __future__ import annotations

import errno
import os
import stat
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.e1_execution import files as files_module
from app.e1_execution.files import (INPUT_FILE, SOURCES, BadPayload, CaseFiles, Files, Payload, RunFolder,
                                    UnsafePath)
from app.e2_scheduling import validation
from tests.helpers import link_folder, logged

HEX = "0123456789abcdef0123456789abcdef"
REF = f"payloads/{HEX}"
WINDOWS = sys.platform == "win32"


@pytest.fixture
def data(tmp_path: Path) -> Path:
    """DATA_DIR, with a secret beside it that nothing may reach."""
    root = tmp_path / "data"
    for name in ("payloads", "problems", "runs"):
        (root / name).mkdir(parents=True)
    for name in ("outside", "data2"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "secret").write_bytes(b"secret")
    return root


@pytest.fixture
def files(data: Path) -> Files:
    return Files(data)


def tree(root: Path) -> dict[str, bytes | None]:
    """Everything below root by its relative path: a file's bytes, None for a folder."""
    return {p.relative_to(root).as_posix(): p.read_bytes() if p.is_file() else None for p in root.rglob("*")}


# ---- safe_path ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("rel", ["problems/12/01.in", "payloads", "a/b/c.txt", "a/../problems"])
def test_a_path_inside_data_dir_is_resolved(files: Files, data: Path, rel: str):
    """It need not exist yet; what comes back is absolute, with every '..' and link resolved."""
    assert files.safe_path(rel) == (data.resolve() / rel.replace("a/../", ""))


@pytest.mark.parametrize("rel", [
    "..", "../outside/secret", "a/../../outside/secret", "problems/../../outside/secret", "../data2/secret",
    "/etc/passwd", "//server/share/x", "", ".", "a/..", "payloads/\0x", "\0",
    *(["C:\\x", "C:x", "\\x", "\\\\server\\share\\x", "..\\outside\\secret"] if WINDOWS else []),
])
def test_a_path_that_leaves_data_dir_is_refused(files: Files, rel: str):
    """Catches a missing check, one on the path as written ('..' never resolved), one by string prefix (data2
    starts with data), DATA_DIR itself taken for a path inside it, and a NUL that cuts the path short."""
    with pytest.raises(UnsafePath):
        files.safe_path(rel)


@pytest.mark.parametrize("where", ["outside", "inside"])
def test_an_absolute_path_is_refused_wherever_it_points(files: Files, data: Path, tmp_path: Path, where: str):
    """rel is relative to DATA_DIR: an absolute path is refused even when it points inside."""
    path = (data / "payloads" if where == "inside" else tmp_path / "outside" / "secret").resolve()
    with pytest.raises(UnsafePath):
        files.safe_path(str(path))


def test_a_folder_link_out_of_data_dir_is_refused_and_one_inside_is_followed(files: Files, data: Path,
                                                                             tmp_path: Path):
    """Catches a check made before links are resolved (a junction on Windows)."""
    link_folder(data / "problems" / "out", tmp_path / "outside")
    link_folder(data / "problems" / "in", data / "payloads")
    for rel in ("problems/out", "problems/out/secret", "problems/out/new"):
        with pytest.raises(UnsafePath):
            files.safe_path(rel)
    assert files.safe_path("problems/in/x") == data.resolve() / "payloads" / "x"


def test_no_link_is_followed_below_runs(files: Files, data: Path):
    """A program owns what lies in its out/, and a link it leaves there stays inside DATA_DIR: the expected outputs,
    another student's source, the server's log. Catches a path below runs/ that is handed on through one."""
    out = files.new_run(1042, 1).out
    (data / "problems" / "12").mkdir()
    (data / "problems" / "12" / "01.out").write_bytes(b"7\n")
    link_folder(out / "answers", data / "problems" / "12")
    (out / "plot.png").write_bytes(b"png")
    for rel in ("runs/1042/a1/out/answers", "runs/1042/a1/out/answers/01.out", "runs/./1042/a1/out/answers/01.out",
                "problems/../runs/1042/a1/out/answers/01.out"):
        with pytest.raises(UnsafePath):
            files.safe_path(rel)
    assert files.safe_path("runs/1042/a1/out/plot.png") == out / "plot.png"
    assert files.safe_path("problems/../runs/1042/a1/out/plot.png") == out / "plot.png"


def test_a_file_link_out_of_data_dir_is_refused(files: Files, data: Path, tmp_path: Path):
    """Skipped where the OS refuses to create a file link (Windows without the privilege)."""
    try:
        (data / "problems" / "01.in").symlink_to(tmp_path / "outside" / "secret")
    except OSError as exc:
        pytest.skip(f"this OS refuses a file symlink: {exc}")
    with pytest.raises(UnsafePath):
        files.safe_path("problems/01.in")


def test_a_link_loop_is_refused(files: Files, data: Path):
    """Catches a path that cannot be opened handed on as a good one: resolve() raises for a loop only before
    Python 3.13."""
    first, second = data / "problems" / "a", data / "problems" / "b"
    second.mkdir()  # a junction needs a target when it is made
    link_folder(first, second)
    second.rmdir()
    link_folder(second, first)
    for rel in ("problems/a", "problems/a/x", "problems/b/../a/x"):
        with pytest.raises(UnsafePath):
            files.safe_path(rel)


def failing_for(real: Callable[..., Any], name: str, number: int) -> Callable[..., Any]:
    """real, except that it fails with this errno for a path of this name."""
    def call(path: Any, *args: Any, **rest: Any) -> Any:
        if Path(path).name == name:
            raise OSError(number, os.strerror(number), str(path))
        return real(path, *args, **rest)

    return call


@pytest.mark.parametrize("number", [errno.EACCES, errno.EIO])
def test_a_disk_that_fails_is_no_path_attack(files: Files, data: Path, monkeypatch: pytest.MonkeyPatch, number: int):
    """Catches a wrong owner or a bad disk told as a path that leaves DATA_DIR: whoever is paged looks for an attack."""
    monkeypatch.setattr(Path, "stat", failing_for(Path.stat, "01.in", number))
    with pytest.raises(OSError) as caught:
        files.safe_path("problems/01.in")
    assert caught.value.errno == number and not isinstance(caught.value, ValueError)


def test_a_loop_that_only_stat_sees_is_refused(files: Files, data: Path, monkeypatch: pytest.MonkeyPatch):
    """What Linux says of a loop since Python 3.13. Catches exists() in place of stat(): it says False for a loop."""
    monkeypatch.setattr(Path, "stat", failing_for(Path.stat, "01.in", errno.ELOOP))
    with pytest.raises(UnsafePath):
        files.safe_path("problems/01.in")


def test_data_dir_may_itself_be_reached_through_a_link(data: Path, tmp_path: Path):
    link_folder(tmp_path / "linked", data)
    assert Files(tmp_path / "linked").safe_path("problems/x") == data.resolve() / "problems" / "x"


# ---- payload --------------------------------------------------------------------------------------------

def test_the_stored_names_are_e2s(files: Files):
    """Catches a name that only one engine knows: E1 may not import E2, so both write the names down."""
    assert set(SOURCES) == {*validation.CODE_FILES.values(), *validation.ML_FILES.values()}
    assert INPUT_FILE == validation.INPUT_FILE


@pytest.mark.parametrize("source", ["prog.c", "prog.cpp", "Main.java", "prog.py", "payload.zip"])
@pytest.mark.parametrize("stdin", [False, True])
def test_a_payload_holds_one_source_and_perhaps_an_input(files: Files, data: Path, source: str, stdin: bool):
    folder = data / REF
    folder.mkdir()
    (folder / source).write_bytes(b"source")
    if stdin:
        (folder / "input.txt").write_bytes(b"")
    folder = folder.resolve()
    assert files.payload(REF, with_input=stdin) == Payload(folder, source, folder / "input.txt" if stdin else None)
    assert files.payload(REF).path == folder / source


def test_the_input_is_the_callers_to_ask_for(files: Files, data: Path):
    """Every code_run payload holds input.txt, empty when none was given (D-129). Catches one that is missing run
    with an empty input, and an input handed to a run that has none."""
    folder = data / REF
    folder.mkdir()
    (folder / "prog.c").write_bytes(b"source")
    with pytest.raises(BadPayload, match="input.txt"):
        files.payload(REF, with_input=True)
    (folder / "input.txt").write_bytes(b"5")
    assert files.payload(REF).stdin is None
    assert files.payload(REF, with_input=True).stdin == folder.resolve() / "input.txt"


@pytest.mark.parametrize("name", ["prog.c", "input.txt"])
@pytest.mark.parametrize("number", [errno.EACCES, errno.EIO])
def test_a_payload_that_cannot_be_read_is_the_disks_error(files: Files, data: Path, monkeypatch: pytest.MonkeyPatch,
                                                          name: str, number: int):
    """Catches a file that cannot be looked at taken for one that is not there: a practice run would run with an
    empty input, and a payload would be called empty with no cause (os.path.lexists says False for any error)."""
    folder = data / REF
    folder.mkdir()
    for file in ("prog.c", "input.txt"):
        (folder / file).write_bytes(b"source")
    monkeypatch.setattr(files_module.os, "lstat", failing_for(os.lstat, name, number))
    with pytest.raises(OSError) as caught:
        files.payload(REF, with_input=True)
    assert caught.value.errno == number and not isinstance(caught.value, ValueError)


@pytest.mark.parametrize("ref", [
    "", "payloads", "payloads/", "payloads/test", f"payloads/{HEX[:-1]}", f"payloads/{HEX}0", f"payloads/{HEX.upper()}",
    f"payloads/{HEX}\n", f"payloads/{HEX}/", f"payloads/{HEX}/prog.c", f"runs/{HEX}", f"./payloads/{HEX}",
    f"payloads/../payloads/{HEX}", f"payloads//{HEX}", HEX, f"/payloads/{HEX}", f"xpayloads/{HEX}",
])
def test_a_reference_of_another_shape_is_refused(files: Files, data: Path, ref: str):
    """Catches a check that accepts a prefix or a suffix (a newline before the end included), or anything that
    resolves to the folder: jobs.payload_path is exactly payloads/<uuid4 hex> (D-80)."""
    folder = data / REF
    folder.mkdir()
    (folder / "prog.c").write_bytes(b"source")
    with pytest.raises(BadPayload):
        files.payload(ref)


@pytest.mark.parametrize("holds", ["nothing", "unknown", "two", "folder", "input folder"])
def test_a_payload_that_e2_did_not_write_is_refused(files: Files, data: Path, holds: str):
    """Catches the first of two sources picked, and a folder taken for a source."""
    folder = data / REF
    folder.mkdir()
    if holds == "unknown":
        (folder / "main.c").write_bytes(b"source")
    if holds == "folder":
        (folder / "prog.c").mkdir()
    if holds in ("two", "input folder"):
        (folder / "prog.c").write_bytes(b"source")
    if holds == "two":
        (folder / "Main.java").write_bytes(b"source")
    if holds == "input folder":
        (folder / "input.txt").mkdir()
    with pytest.raises(BadPayload):
        files.payload(REF, with_input=holds == "input folder")


@pytest.mark.parametrize("missing", ["folder", "file"])
def test_a_payload_that_is_no_folder_is_refused(files: Files, data: Path, missing: str):
    if missing == "file":
        (data / REF).write_bytes(b"source")
    with pytest.raises(BadPayload):
        files.payload(REF)


@pytest.mark.parametrize("target", ["outside", "inside"])
def test_a_payload_reached_through_a_link_is_refused(files: Files, data: Path, tmp_path: Path, target: str):
    """Catches a payload folder that is a link, also to another payload inside DATA_DIR: E2 never makes one."""
    real = tmp_path / "outside" / "payload" if target == "outside" else data / "payloads" / ("f" * 32)
    real.mkdir()
    (real / "prog.c").write_bytes(b"source")
    link_folder(data / REF, real)
    with pytest.raises((UnsafePath, BadPayload)):
        files.payload(REF)


@pytest.mark.parametrize("name", ["prog.c", "input.txt"])
def test_a_link_inside_a_payload_is_refused(files: Files, data: Path, tmp_path: Path, name: str):
    """Skipped where the OS refuses to create a file link (Windows without the privilege)."""
    folder = data / REF
    folder.mkdir()
    if name != "prog.c":
        (folder / "prog.c").write_bytes(b"source")
    try:
        (folder / name).symlink_to(tmp_path / "outside" / "secret")
    except OSError as exc:
        pytest.skip(f"this OS refuses a file symlink: {exc}")
    with pytest.raises(BadPayload):
        files.payload(REF, with_input=True)


# ---- case -----------------------------------------------------------------------------------------------

def test_a_case_resolves_both_of_its_paths(files: Files, data: Path):
    (data / "problems" / "12").mkdir()
    for name in ("03.in", "03.out"):
        (data / "problems" / "12" / name).write_bytes(b"7\n")
    folder = data.resolve() / "problems" / "12"
    assert files.case(3, "problems/12/03.in", "problems/12/03.out") == CaseFiles(3, folder / "03.in", folder / "03.out")


@pytest.mark.parametrize("which", ["input", "expected"])
def test_a_case_with_a_path_outside_or_missing_is_refused(files: Files, data: Path, which: str):
    """Catches one of the two paths left unchecked."""
    (data / "problems" / "ok").write_bytes(b"7\n")

    def case(bad: str) -> CaseFiles:
        return files.case(1, *((bad, "problems/ok") if which == "input" else ("problems/ok", bad)))

    with pytest.raises(UnsafePath):
        case("../outside/secret")
    with pytest.raises(FileNotFoundError):
        case("problems/missing")
    with pytest.raises(FileNotFoundError):
        case("problems")  # a folder


# ---- the run folder -------------------------------------------------------------------------------------

def test_a_new_run_folder_holds_only_an_empty_out(files: Files, data: Path):
    run = files.new_run(1042, 1)
    folder = data.resolve() / "runs" / "1042" / "a1"
    assert run == RunFolder(folder) and tree(data / "runs") == {"1042": None, "1042/a1": None, "1042/a1/out": None}


def test_the_host_keeps_its_files_outside_the_mounted_folder(files: Files):
    """R1: only out/ is mounted (and job/, read-only, for an extracted zip); everything the server writes or
    reads for an attempt lies beside it."""
    run = files.new_run(1042, 1)
    ours = [run.spec, run.compile_log, run.main_check, run.case_output(3), run.case_output(100), run.stdout, run.stderr,
            run.result]
    assert [path.name for path in ours] == ["spec.json", "compile.txt", "main.txt", "03.txt", "100.txt", "stdout.txt",
                                            "stderr.txt", "result.json"]
    assert all(path.parent == run.folder for path in [*ours, run.out, run.job])
    assert len({*ours, run.out, run.job}) == 10 and run.out.name == "out" and run.job.name == "job"


def test_each_attempt_has_a_folder_of_its_own(files: Files):
    first = files.new_run(1042, 1)
    first.compile_log.write_bytes(b"first")
    second = files.new_run(1042, 2)
    other = files.new_run(1043, 1)
    assert len({first.folder, second.folder, other.folder}) == 3
    assert first.compile_log.read_bytes() == b"first" and not second.compile_log.exists()


def test_a_stale_run_folder_is_moved_aside(files: Files, data: Path, json_log: Path):
    """Catches a folder of an older database used again (its binary, outputs and result file would speak for
    this attempt), and one deleted: the server may not be able to delete what user 65534 left, so it is renamed
    and stays whole."""
    stale = {"01.txt": b"7\n", "result.json": b"{}", "spec.json": b"{}", "out": None, "out/prog": b"old binary",
             "out/Main.class": b"old class"}
    old = data / "runs" / "1042" / "a1"
    (old / "out").mkdir(parents=True)
    for name, content in stale.items():
        if content is not None:
            (old / name).write_bytes(content)
    run = files.new_run(1042, 1)
    assert tree(run.folder) == {"out": None}
    aside = [p for p in (data / "runs" / "1042").iterdir() if p.name != "a1"]
    assert len(aside) == 1 and aside[0].name.startswith("a1.stale-") and tree(aside[0]) == stale
    line, = logged(json_log, "run_folder_stale")
    assert line["level"] == "WARNING" and line["job_id"] == 1042 and line["attempt_no"] == 1
    assert Path(line["path"]).name == aside[0].name and line["kind"] == "folder"


@pytest.mark.parametrize("kind", ["folder", "file", "link"])
def test_the_log_says_what_was_there_and_how_old_it_is(files: Files, data: Path, tmp_path: Path, json_log: Path,
                                                       kind: str):
    """A folder of last week is an older database's; one of a second ago is an attempt that was dispatched twice."""
    (data / "runs" / "1042").mkdir()
    old = data / "runs" / "1042" / "a1"
    if kind == "link":
        link_folder(old, tmp_path / "outside")
    elif kind == "file":
        old.write_bytes(b"1")
    else:
        old.mkdir()
    if kind != "link":
        os.utime(old, (1_700_000_000, 1_700_000_000))
    files.new_run(1042, 1)
    line, = logged(json_log, "run_folder_stale")
    assert line["kind"] == kind
    modified = datetime.fromisoformat(line["modified"])
    assert modified.tzinfo is not None
    if kind != "link":
        assert modified == datetime.fromtimestamp(1_700_000_000, UTC)


def test_a_stale_file_is_moved_aside(files: Files, data: Path):
    (data / "runs" / "1042").mkdir()
    (data / "runs" / "1042" / "a1").write_bytes(b"a file")
    assert tree(files.new_run(1042, 1).folder) == {"out": None}
    aside, = (p for p in (data / "runs" / "1042").iterdir() if p.name != "a1")
    assert aside.read_bytes() == b"a file"


def test_a_stale_link_is_moved_aside_and_never_followed(files: Files, data: Path, tmp_path: Path):
    """Catches a link at the attempt's path that is followed, emptied or deleted through (a junction on Windows)."""
    (data / "runs" / "1042").mkdir()
    link_folder(data / "runs" / "1042" / "a1", tmp_path / "outside")
    run = files.new_run(1042, 1)
    assert tree(run.folder) == {"out": None}
    assert run.folder.resolve() == data.resolve() / "runs" / "1042" / "a1"
    assert tree(tmp_path / "outside") == {"secret": b"secret"}


def test_a_stale_link_to_nothing_is_moved_aside(files: Files, data: Path, tmp_path: Path):
    """Catches a check with exists(), which a link that leads nowhere passes: the folder could not be made."""
    (data / "runs" / "1042").mkdir()
    gone = tmp_path / "gone"
    gone.mkdir()
    link_folder(data / "runs" / "1042" / "a1", gone)
    gone.rmdir()
    assert tree(files.new_run(1042, 1).folder) == {"out": None} and not gone.exists()


def test_moving_aside_twice_keeps_both(files: Files, data: Path):
    """Catches a fixed name for what was moved aside: the second move would fail or overwrite the first."""
    for _ in range(3):
        files.new_run(1042, 1)
    assert len(list((data / "runs" / "1042").iterdir())) == 3


@pytest.mark.parametrize("which", ["runs", "job"])
def test_a_run_folder_is_never_made_through_a_link(data: Path, tmp_path: Path, which: str):
    """Catches a runs folder, or a job's folder in it, that leads out of DATA_DIR."""
    if which == "runs":
        (data / "runs").rmdir()
        link_folder(data / "runs", tmp_path / "outside")
    else:
        link_folder(data / "runs" / "1042", tmp_path / "outside")
    with pytest.raises(UnsafePath):
        Files(data).new_run(1042, 1)
    assert tree(tmp_path / "outside") == {"secret": b"secret"}


def test_a_link_to_another_jobs_folder_is_refused(files: Files, data: Path):
    """Catches a check for "inside DATA_DIR" alone: job 1042 must not run in job 7's folder."""
    (data / "runs" / "7").mkdir()
    link_folder(data / "runs" / "1042", data / "runs" / "7")
    with pytest.raises(UnsafePath):
        files.new_run(1042, 1)
    assert tree(data / "runs" / "7") == {}


@pytest.mark.parametrize("bad", [0, -1, True, "1042", "1042/../7", "..", None, 1.0])
def test_ids_must_be_positive_integers(files: Files, data: Path, bad: Any):
    """Catches an id that reaches a path as text."""
    for job_id, attempt_no in ((bad, 1), (1042, bad)):
        with pytest.raises(ValueError):
            files.new_run(job_id, attempt_no)
    assert tree(data / "runs") == {}


@pytest.mark.skipif(WINDOWS, reason="chmod sets only the read-only flag on Windows; C-4 runs this on Linux")
@pytest.mark.parametrize("umask", [0o022, 0o077, 0o000])
def test_only_the_sandbox_folder_is_open_to_others(files: Files, umask: int):
    """Plan 9.1 and R1: the sandbox user 65534 writes to out/ through its mount, and no other local account
    reaches it, because the attempt's folder is the server's alone, whatever the umask."""
    old = os.umask(umask)
    try:
        runs = [files.new_run(1042, 1)]
        runs[0].folder.parent.chmod(0o777)  # as an older server may have left it
        runs.append(files.new_run(1042, 2))
    finally:
        os.umask(old)
    for run in runs:  # the job's folder too: whoever may write to it can put an a1 of their own there
        assert [stat.S_IMODE(p.stat().st_mode) for p in (run.folder.parent, run.folder, run.out)] == [0o700, 0o700,
                                                                                                    0o777]
