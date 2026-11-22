"""Slice E2-5: submit() from a spooled file (D-90) and practice runs with stdin and the code_run type (D-96).

The oracle comes from the decisions and the seed, never from the code under test: an upload arrives as a
plain file directly in DATA_DIR/spool, is refused with ValueError anywhere else, and is moved (renamed, never
copied) into payloads/<hex>/<stored name>; a refused upload stays in the spool for E4-3 to delete. stdin is
at most 1 MiB (1,048,576 bytes), for code_run only, and is stored as input.txt beside the source. The
code_run seed row has need class 3 and max_time_limit_s 10.
"""
from __future__ import annotations

import dataclasses
import errno
import os
import stat
import tracemalloc
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import app.e2_scheduling.service as service
from app.common.db import tx
from app.common.errors import ApiError
from app.common.types import SubmitRequest, User
from app.config import Config
from app.e2_scheduling import E2Service
from app.e2_scheduling.validation import Accepted, Refs, WorkloadType, validate
from tests.helpers import TempDb, assert_invariants, link_folder, logged, zip_of

Spool = Callable[[bytes], Path]

STUDENT = User("01FE24BCS901", "student")
C_SOURCE = b'#include <stdio.h>\nint main(void) { int n; return scanf("%d", &n) != 1; }\n'
CPP_SOURCE = b"#include <iostream>\nint main() { int n; std::cin >> n; std::cout << n << '\\n'; }\n"
JAVA_SOURCE = b"public class Main { public static void main(String[] a) { System.out.println(42); } }\n"
PY_SOURCE = b"print(input())\n"
NOT_UTF8 = b"int main(void) { return 0; } /* \xff\xfe */\n"
RUN = SubmitRequest("code_run", 1000, 512, 0, 10, source_name="main.py")
CPU = SubmitRequest("cpu_bound", 1000, 512, 0, 5, source_name="prog.c")
ML = SubmitRequest("python_ml", 2000, 4096, 1024, 600, source_name="model.zip")
SPOOL_ONLY = "DATA_DIR/spool"  # the ValueError names the one place a source may come from
# db/seed.sql with the code_run row of D-96, and S0's allocatable maxima (context §3).
TYPES = {"code_eval": WorkloadType(2, 60), "code_run": WorkloadType(3, 10), "cpu_bound": WorkloadType(3, 600),
         "mem_bound": WorkloadType(3, 600), "gpu_bound": WorkloadType(3, 1800), "mixed": WorkloadType(3, 600),
         "python_ml": WorkloadType(3, 3600)}
S0 = Refs(types=TYPES, problem_found=True, session=None, cpu_max=28800, ram_max=117964, vram_max=14745)


def change(req: SubmitRequest, /, **fields: Any) -> SubmitRequest:
    return dataclasses.replace(req, **fields)


def job_count() -> int:
    with tx("E4") as cur:
        row = cur.execute("SELECT count(*) FROM jobs").fetchone()
    assert row is not None
    return row[0]


def job_row(job_id: int) -> tuple[Any, ...]:
    """(type_code, problem_id, need_class, time_limit_s, payload_path)."""
    with tx("E4") as cur:
        row = cur.execute("SELECT type_code, problem_id, need_class, time_limit_s, payload_path FROM jobs "
                          "WHERE job_id = %s", (job_id,)).fetchone()
    assert row is not None
    return tuple(row)


def files_under(root: Path) -> dict[str, bytes]:
    """Every file below root, by its path relative to root, with its bytes."""
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def refused_by_submit(e2: E2Service, req: SubmitRequest, source: Any) -> tuple[str | None, str]:
    with pytest.raises(ApiError) as caught:
        e2.submit(STUDENT, req, source)
    assert caught.value.status == 400
    return caught.value.field, caught.value.error


def nothing_queued(e2: E2Service, seeded: TempDb) -> None:
    assert job_count() == 0
    assert seeded.stubs.hub.events == [] and len(e2.queue) == 0


# ---- where a source may come from (D-90) ------------------------------------------------------------

@pytest.mark.parametrize("where", ["dot dot", "data dir", "payloads", "outside data dir", "spool folder",
                                   "spool subfolder", "missing"])
def test_a_path_outside_the_spool_is_refused_before_anything_is_read(
        seeded: TempDb, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, where: str):
    """Catches a missing containment check, one made on the path as written ('..' collapsed into the spool),
    and a check made after validate() has read the file: each path is refused with ValueError before
    validation, and nothing anywhere is written, moved or deleted. Only a plain file directly in
    DATA_DIR/spool is taken (D-90)."""
    data = tmp_path / "data"
    for path in (data / "x.c", data / "payloads" / "x.c", tmp_path / "x.c", data / "spool" / "sub" / "x.c"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(C_SOURCE)
    source = {"dot dot": data / "spool" / ".." / "x.c", "data dir": data / "x.c", "payloads": data / "payloads" / "x.c",
              "outside data dir": tmp_path / "x.c", "spool folder": data / "spool",
              "spool subfolder": data / "spool" / "sub" / "x.c", "missing": data / "spool" / "missing.c"}[where]
    before = files_under(tmp_path)

    def must_not_run(*args: object) -> None:
        raise AssertionError("validate() ran before the spool check")

    e2 = E2Service(Config(data_dir=data))
    monkeypatch.setattr(service, "validate", must_not_run)
    with pytest.raises(ValueError, match=SPOOL_ONLY):
        e2.submit(STUDENT, CPU, source)
    assert files_under(tmp_path) == before
    assert [p.name for p in (data / "payloads").iterdir()] == ["x.c"]  # no job folder beside it
    nothing_queued(e2, seeded)
    assert_invariants(settled=True)


@pytest.mark.parametrize("link", ["spool folder out", "folder out", "folder in", "file out", "file in", "hard link"])
def test_a_link_in_the_spool_is_refused_and_nothing_moves(seeded: TempDb, tmp_path: Path, link: str):
    """Catches links judged on the path as written and a link accepted or moved: E4-3 writes only plain files
    with one name directly in the spool. A spool folder that is itself a link out of DATA_DIR, a folder link
    (a junction on Windows) to a folder outside or to another spool folder, a file symlink to a file outside
    or to another spool file, and a second name (a hard link) of another spool file, through which the
    payload could change after validation, are each refused with ValueError, and the file they lead to stays
    where it is, byte for byte. The file symlinks are skipped where the OS refuses to create one (Windows
    without the privilege; C-4 runs them on Linux)."""
    data, outside = tmp_path / "data", tmp_path / "outside"
    spool_dir = data / "spool"
    outside.mkdir()
    if link == "spool folder out":
        data.mkdir()
        link_folder(spool_dir, outside)  # DATA_DIR/spool leads out of DATA_DIR
    else:
        (spool_dir / "inner").mkdir(parents=True)
    folder = {"spool folder out": outside, "folder out": outside, "folder in": spool_dir / "inner",
              "file out": outside, "file in": spool_dir, "hard link": spool_dir}[link]
    target = folder / "x.c"
    target.write_bytes(C_SOURCE)
    if link == "spool folder out":
        source = spool_dir / "x.c"  # directly in the spool as written, outside DATA_DIR on disk
    elif link == "hard link":
        source = spool_dir / "a.c"
        os.link(target, source)  # no privilege needed, on NTFS as on Linux
    elif link.startswith("folder"):
        link_folder(spool_dir / "linked", folder)
        source = spool_dir / "linked" / "x.c"
    else:
        source = spool_dir / "a.c"
        try:
            os.symlink(target, source)
        except OSError as exc:  # Windows without the symbolic link privilege (WinError 1314)
            pytest.skip(f"this OS refuses to create a file symlink here: {exc}")
    e2 = E2Service(Config(data_dir=data))
    with pytest.raises(ValueError, match=SPOOL_ONLY):
        e2.submit(STUDENT, CPU, source)
    assert target.read_bytes() == C_SOURCE
    assert os.path.lexists(source)
    assert not (data / "payloads").exists()
    nothing_queued(e2, seeded)
    assert_invariants(settled=True)


@pytest.mark.parametrize(("source", "expected"), [
    (None, ("source", "is required")), (C_SOURCE, ("source", "must be a file")),
    ("spool/x.c", ("source", "must be a file"))], ids=["none", "bytes", "text path"])
def test_a_source_that_is_not_a_path_gets_the_400_of_validation(e2: E2Service, seeded: TempDb, tmp_path: Path,
                                                                source: Any, expected: tuple[str, str]):
    """Catches the spool check turning a missing source into a server error (V1 sends None and expects a 400
    on source), and bytes, the old form of the port, still accepted after the change of D-90."""
    assert refused_by_submit(e2, CPU, source) == expected
    assert list(tmp_path.iterdir()) == []
    nothing_queued(e2, seeded)


# ---- the move into payloads (D-90) -------------------------------------------------------------------

def test_an_accepted_upload_is_moved_into_its_payload_folder_not_copied(e2: E2Service, seeded: TempDb,
                                                                        spool: Spool, tmp_path: Path):
    """Catches a copy (the spool file still there) and a copy then delete (a new file): the payload is the
    very file E4-3 spooled, renamed (os.path.samestat), byte for byte, and nothing else is left under
    DATA_DIR."""
    path = spool(C_SOURCE)
    before = os.stat(path)
    payload_path = job_row(e2.submit(STUDENT, CPU, path).job_id)[4]
    assert not os.path.lexists(path)
    assert os.path.samestat(before, os.stat(tmp_path / payload_path / "prog.c"))
    assert files_under(tmp_path) == {f"{payload_path}/prog.c": C_SOURCE}
    assert_invariants(settled=True)


@pytest.mark.parametrize(("req", "source", "expected"), [
    (CPU, NOT_UTF8, ("source", "must be UTF-8 text")),
    (ML, zip_of({"main.py": b"1", "../evil.py": b"1"}), ("source", "must not contain a '..' part")),
    (change(CPU, time_limit_s=601), C_SOURCE, ("time_limit_s", "must be 1 to 600 (maximum for cpu_bound)")),
    (change(CPU, stdin=b"7\n"), C_SOURCE, ("stdin", "must be empty for cpu_bound"))],
    ids=["bad utf-8", "unsafe zip", "bad field", "stdin for cpu_bound"])
def test_a_refused_upload_stays_in_the_spool_untouched(e2: E2Service, seeded: TempDb, spool: Spool, tmp_path: Path,
                                                       req: SubmitRequest, source: bytes, expected: tuple[str, str]):
    """Catches submit() deleting, moving or changing an upload it refuses: E4-3 deletes what is left after
    submit() returns or raises (D-90), so a refused file stays where it was, the same file byte for byte,
    and no payload folder is made."""
    path = spool(source)
    before = os.stat(path)
    assert refused_by_submit(e2, req, path) == expected
    assert path.read_bytes() == source and os.path.samestat(before, os.stat(path))
    assert not (tmp_path / "payloads").exists()
    nothing_queued(e2, seeded)
    assert_invariants(settled=True)


def test_a_failed_move_removes_the_payload_folder_and_keeps_the_spool_file(
        e2: E2Service, seeded: TempDb, spool: Spool, tmp_path: Path, json_log: Path, monkeypatch: pytest.MonkeyPatch):
    """Catches a half-made payload folder left behind, a copy as the fallback for another drive, and the
    error swallowed or left unlogged: os.replace fails as it does across drives (EXDEV), the OSError comes
    out of submit(), submit_failed logs it with its traceback, the new folder and its input.txt are removed,
    no job exists, and the spool file stays for E4-3."""
    path = spool(PY_SOURCE)
    real_replace = os.replace

    def across_drives(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
        if Path(src).name == path.name:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", across_drives)
    with pytest.raises(OSError) as caught:
        e2.submit(STUDENT, change(RUN, stdin=b"7\n"), path)
    assert caught.value.errno == errno.EXDEV
    assert path.read_bytes() == PY_SOURCE
    assert list((tmp_path / "payloads").iterdir()) == []
    assert [(line["level"], line["user_id"], line["payload_path"], "traceback" in line)
            for line in logged(json_log, "submit_failed")] == [("ERROR", STUDENT.user_id, None, True)]
    nothing_queued(e2, seeded)
    assert_invariants(settled=True)


def test_the_payload_is_readable_by_the_sandbox_whatever_the_spool_file_mode(e2: E2Service, seeded: TempDb,
                                                                              spool: Spool, tmp_path: Path):
    """Catches the payload keeping the spool file's mode: os.replace keeps the file, so one that E4-3 made
    with mkstemp (0600) would stay unreadable for the sandbox user 65534 behind the read-only mount (plan
    9.3). The source and input.txt end 0644 (on Windows, which keeps only a read-only flag: not read-only)."""
    path = spool(PY_SOURCE)
    os.chmod(path, stat.S_IRUSR)  # 0400; on Windows the read-only flag
    payload_path = job_row(e2.submit(STUDENT, change(RUN, stdin=b"7\n"), path).job_id)[4]
    expected = 0o644 if os.name == "posix" else 0o666
    assert [stat.S_IMODE((tmp_path / payload_path / name).stat().st_mode)
            for name in ("prog.py", "input.txt")] == [expected, expected]
    assert_invariants(settled=True)


@pytest.mark.skipif(os.name != "posix", reason="POSIX modes; Windows has ACLs (C-4 runs this on Linux)")
def test_the_payload_folder_and_files_are_readable_under_a_strict_umask(e2: E2Service, seeded: TempDb,
                                                                        spool: Spool, tmp_path: Path):
    """Catches modes left to the umask: under 077 (systemd's default for many services) the folder would be
    0700 and input.txt 0600, closed to the sandbox user; they end 0755 and 0644."""
    old = os.umask(0o077)
    try:
        payload_path = job_row(e2.submit(STUDENT, change(RUN, stdin=b"7\n"), spool(PY_SOURCE)).job_id)[4]
    finally:
        os.umask(old)
    folder = tmp_path / payload_path
    assert [stat.S_IMODE(path.stat().st_mode) for path in (folder, folder / "prog.py", folder / "input.txt")] == [
        0o755, 0o644, 0o644]
    assert_invariants(settled=True)


def test_validation_never_holds_a_large_upload_in_memory(spool: Spool):
    """Catches a source read whole (D-90: a python_ml upload of up to 1 GiB must never sit in memory): a 16 MiB
    .py and a zip holding one stored 16 MiB entry are each validated with under 6 MiB of Python memory at the
    peak (the UTF-8 check reads 1 MiB at a time; the zip check reads only its end and its directory)."""
    big_py = spool(b"#" * (16 << 20))
    big_zip = spool(zip_of({"main.py": b"#" * (16 << 20)}))
    tracemalloc.start()
    try:
        assert validate(change(ML, source_name="train.py"), big_py, S0, Config()) == Accepted(3, "prog.py")
        assert validate(ML, big_zip, S0, Config()) == Accepted(3, "payload.zip")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 6 << 20


# ---- practice runs: stdin and code_run (D-96) ---------------------------------------------------------

@pytest.mark.parametrize("req", [
    SubmitRequest("code_eval", 1000, 512, 0, 5, problem_id=12, source_name="prog.c"),
    SubmitRequest("cpu_bound", 1000, 512, 0, 5, source_name="prog.c"),
    SubmitRequest("mem_bound", 1000, 512, 0, 5, source_name="prog.c"),
    SubmitRequest("gpu_bound", 1000, 512, 0, 5, source_name="prog.c"),
    SubmitRequest("mixed", 1000, 512, 0, 5, source_name="prog.c"),
    SubmitRequest("python_ml", 1000, 512, 0, 5, source_name="train.py")], ids=lambda req: req.workload_type)
def test_stdin_is_refused_for_every_type_but_code_run(spool: Spool, req: SubmitRequest):
    """Catches stdin accepted outside a practice run (D-96): every other type refuses it by name, while
    code_run takes the same bytes. An empty stdin is no stdin (as an empty field is one not given, D-79), so
    "must be empty" is never said of an empty value."""
    source = spool(PY_SOURCE)
    with pytest.raises(ApiError) as caught:
        validate(change(req, stdin=b"7\n"), source, S0, Config())
    assert (caught.value.status, caught.value.field, caught.value.error) == (
        400, "stdin", f"must be empty for {req.workload_type}")
    assert validate(change(req, stdin=b""), source, S0, Config()) == validate(req, source, S0, Config())
    assert validate(change(RUN, stdin=b"7\n"), source, S0, Config()) == Accepted(3, "prog.py")


def test_a_stdin_of_1_mib_is_stored_byte_for_byte_and_one_byte_more_is_refused(e2: E2Service, seeded: TempDb,
                                                                                spool: Spool, tmp_path: Path):
    """Catches an off-by-one at the cap, a missing cap or type check, and stdin changed on its way to disk:
    exactly 1,048,576 bytes, every byte value included, are stored as input.txt beside the source; one byte
    more, text instead of bytes, or a bytearray, is refused by name and makes no job."""
    stdin = bytes(range(256)) * 4096
    assert len(stdin) == 1048576
    payload_path = job_row(e2.submit(STUDENT, change(RUN, stdin=stdin), spool(PY_SOURCE)).job_id)[4]
    assert files_under(tmp_path / payload_path) == {"prog.py": PY_SOURCE, "input.txt": stdin}
    for value, error in [(stdin + b"x", "must be at most 1048576 bytes"), ("7\n", "must be bytes"),
                         (bytearray(b"7\n"), "must be bytes")]:
        assert refused_by_submit(e2, change(RUN, stdin=value), spool(PY_SOURCE)) == ("stdin", error)
    assert job_count() == 1
    assert_invariants(settled=True)


LANGUAGES = [("main.c", "prog.c", C_SOURCE), ("main.cpp", "prog.cpp", CPP_SOURCE),
             ("Main.java", "Main.java", JAVA_SOURCE), ("main.py", "prog.py", PY_SOURCE)]


def test_a_practice_run_holds_its_source_and_an_input_txt_in_each_language(e2: E2Service, seeded: TempDb,
                                                                           spool: Spool, tmp_path: Path):
    """Catches a missing or wrong code_run seed row (need class 3, 10 s), a language E2 does not store for a
    practice run, and a payload without input.txt: E1-1 feeds input.txt to the program, so a run with no
    stdin still gets one, empty (D-96)."""
    for name, stored, source in LANGUAGES:
        result = e2.submit(STUDENT, change(RUN, source_name=name), spool(source))
        *row, payload_path = job_row(result.job_id)
        assert (result.need_class, row) == (3, ["code_run", None, 3, 10])
        assert files_under(tmp_path / payload_path) == {stored: source, "input.txt": b""}
    assert_invariants(settled=True)


def test_a_practice_run_takes_no_problem_no_zip_and_10_s_at_most_and_keeps_the_exam_class(
        e2: E2Service, seeded: TempDb, spool: Spool):
    """Catches a problem or a zip accepted for code_run (a practice run has no problem and no verdict,
    PRODUCT_SPEC section 7), a limit other than the seed's 10 s, and the session ignored: an open exam
    session (7) gives class 1, as for every type (FR-01)."""
    assert e2.submit(STUDENT, change(RUN, lab_session_id=7), spool(PY_SOURCE)).need_class == 1
    cases = [(change(RUN, time_limit_s=11), ("time_limit_s", "must be 1 to 10 (maximum for code_run)")),
             (change(RUN, problem_id=12), ("problem_id", "must be empty for code_run")),
             (change(RUN, source_name="model.zip"), ("source_name", "must end in .c, .cpp, .java or .py"))]
    for req, expected in cases:
        assert refused_by_submit(e2, req, spool(PY_SOURCE)) == expected
    assert job_count() == 1
    assert_invariants(settled=True)


def test_stdin_never_shows_in_a_repr_or_a_log_line(e2: E2Service, seeded: TempDb, spool: Spool, json_log: Path):
    """Catches stdin in the dataclass repr (a log line or a traceback that prints the request would carry
    the user's input) and in the submit or submit_refused line (D-96, like source_name under D-76)."""
    secret = b"TOP-SECRET-INPUT 1042"
    req = change(RUN, stdin=secret)
    assert "TOP-SECRET" not in repr(req)
    e2.submit(STUDENT, req, spool(PY_SOURCE))
    refused_by_submit(e2, change(CPU, stdin=secret), spool(C_SOURCE))
    assert [line["type_code"] for line in logged(json_log, "submit")] == ["code_run"]
    assert [line["field"] for line in logged(json_log, "submit_refused")] == ["stdin"]
    assert "TOP-SECRET" not in json_log.read_text(encoding="utf-8")
    assert_invariants(settled=True)
