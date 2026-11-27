"""Slice E2-1: validate() and submit() (plan 8.1 and 8.5, context §12, D-76 to D-80), with every source passed
as a spooled file since E2-5 (D-90): the same checks and the same expected results as with bytes.

The oracle comes from the baseline and the seed, never from the code under test: S0's allocatable
maxima (28,800 millicores, 117,964 MiB, 14,745 MiB of VRAM), the seed's workload types, lab sessions 7
(exam) and 8 (lab), both open, problem 12, and the §12 sample. The first tests call validate() alone;
the rest run submit() on a seeded database with tmp_path as DATA_DIR.
"""
from __future__ import annotations

import dataclasses
import io
import json
import re
import struct
import sys
import threading
import zipfile
import zlib
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO

import psycopg
import pytest
from psycopg import errors

from app.common.db import Engine, Tx, in_transaction, tx
from app.common.errors import ApiError
from app.common.types import QueueEntry, SubmitRequest, SubmitResult, User, Vector
from app.config import Config
from app.e2_scheduling import E2Service, service, validation
from app.e2_scheduling.validation import Accepted, Refs, Session, WorkloadType, validate
from scripts.capacity import Row, apply
from tests.helpers import TempDb, assert_invariants, link_folder, spooled, zip_of

Spool = Callable[[bytes], Path]

STUDENT = User("01FE24BCS901", "student")
C_SOURCE = b'#include <stdio.h>\nint main(void) { printf("%d\\n", 42); return 0; }\n'
CPP_SOURCE = b"#include <iostream>\nint main() { std::cout << 42 << '\\n'; }\n"
JAVA_SOURCE = b'public class Main { public static void main(String[] a) { System.out.println(42); } }\n'
PY_SOURCE = "print('r\u00e9sultat', 42)\n".encode()
NOT_UTF8 = b"int main(void) { return 0; } /* \xff\xfe */\n"
SAMPLE = SubmitRequest("code_eval", 1000, 512, 0, 5, problem_id=12, lab_session_id=7, source_name="prog.c")
ML = SubmitRequest("python_ml", 2000, 4096, 1024, 600, source_name="model.zip")
ML_PY = dataclasses.replace(ML, source_name="train.py")
RUN = SubmitRequest("code_run", 1000, 512, 0, 10, source_name="main.py")
TYPES = {"code_eval": WorkloadType(2, 60), "code_run": WorkloadType(3, 10), "cpu_bound": WorkloadType(3, 600),
         "mem_bound": WorkloadType(3, 600), "gpu_bound": WorkloadType(3, 1800), "mixed": WorkloadType(3, 600),
         "python_ml": WorkloadType(3, 3600)}
S0 = Refs(types=TYPES, problem_found=True, session=Session("exam", True), cpu_max=28800, ram_max=117964,
          vram_max=14745)
DEFAULTS = Config()
CODE_TYPES = ["code_eval", "code_run", "cpu_bound", "mem_bound", "gpu_bound", "mixed"]
CODE_EXTENSIONS = "must end in .c, .cpp, .java or .py"
ONE_OF = "must be one of code_eval, code_run, cpu_bound, gpu_bound, mem_bound, mixed, python_ml"


def change(req: SubmitRequest = SAMPLE, /, **fields: Any) -> SubmitRequest:
    return dataclasses.replace(req, **fields)


def unicode_path_zip(stored: str, extracted: str) -> bytes:
    """main.py plus one entry stored as `stored` whose Info-ZIP Unicode Path extra field (0x7075) names
    `extracted`: zipfile extracts the entry under that second name when the field's CRC matches."""
    name = extracted.encode()
    info = zipfile.ZipInfo(stored)
    info.extra = struct.pack("<HHBL", 0x7075, 5 + len(name), 1, zlib.crc32(stored.encode())) + name
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("main.py", b"print(1)\n")
        archive.writestr(info, b"1")
    return buffer.getvalue()


ML_ZIP = zip_of({"main.py": b"print('trained')\n", "data/train.csv": b"x,y\n1,2\n"})


def refusal(req: SubmitRequest, source: Any, refs: Refs = S0, config: Config = DEFAULTS) -> tuple[str | None, str]:
    """The (field, error) of the 400 that validate() raises."""
    with pytest.raises(ApiError) as caught:
        validate(req, source, refs, config)
    assert caught.value.status == 400
    return caught.value.field, caught.value.error


# ---- validate() alone -------------------------------------------------------------------------------

def test_the_section_12_sample_is_accepted_with_need_class_1(spool: Spool):
    assert validate(SAMPLE, spool(C_SOURCE), S0, DEFAULTS) == Accepted(1, "prog.c")


@pytest.mark.parametrize(("field", "error"), [
    ("workload_type", "is required"), ("problem_id", "is required for code_eval"), ("time_limit_s", "is required"),
    ("ram_mib", "is required"), ("vram_mib", "is required"), ("cpu_millicores", "is required"),
    ("source_name", "is required")])
def test_a_missing_field_is_named(spool: Spool, field: str, error: str):
    """Catches a missing field that crashes (a TypeError, so a 500) or is reported under another name."""
    assert refusal(change(**{field: None}), spool(C_SOURCE)) == (field, error)


def test_an_empty_name_or_a_missing_source_is_named(spool: Spool):
    """An empty source_name is the default of SubmitRequest: the API did not pass the file's name."""
    assert refusal(change(source_name=""), spool(C_SOURCE)) == ("source_name", "is required")
    assert refusal(change(workload_type=""), spool(C_SOURCE)) == ("workload_type", "is required")
    assert refusal(SAMPLE, None) == ("source", "is required")


@pytest.mark.parametrize("field", ["problem_id", "lab_session_id", "time_limit_s", "ram_mib", "vram_mib",
                                   "cpu_millicores"])
@pytest.mark.parametrize("value", [True, 512.0, "512"], ids=["bool", "float", "text"])
def test_an_integer_field_refuses_bool_float_and_text(spool: Spool, field: str, value: object):
    """Catches isinstance(value, int), which lets True through as 1, and a float such as 512.0, which
    compares equal to 512 and would reach an INTEGER column (integer units only, invariant 7)."""
    assert refusal(change(**{field: value}), spool(C_SOURCE)) == (field, "must be an integer")


def test_text_and_file_fields_refuse_other_types(spool: Spool):
    """Also catches a source accepted in any form but the path of a spooled file: text, a bytearray, the
    bytes the port took before D-90, and a path written as text."""
    assert refusal(change(workload_type=5), spool(C_SOURCE)) == ("workload_type", ONE_OF)
    assert refusal(change(source_name=123), spool(C_SOURCE)) == ("source_name", CODE_EXTENSIONS)
    for source in ["int main(void) { return 0; }", bytearray(C_SOURCE), C_SOURCE, str(spool(C_SOURCE))]:
        assert refusal(SAMPLE, source) == ("source", "must be a file")


EDGES = [
    # field, request, lowest and highest accepted values, error one past either edge
    ("time_limit_s", SAMPLE, 1, 60, "must be 1 to 60 (maximum for code_eval)"),
    ("time_limit_s", ML_PY, 1, 3600, "must be 1 to 3600 (maximum for python_ml)"),
    ("time_limit_s", RUN, 1, 10, "must be 1 to 10 (maximum for code_run)"),
    ("ram_mib", SAMPLE, 64, 117964, "must be 64 to 117964 (allocatable maximum)"),
    ("vram_mib", SAMPLE, 0, 14745, "must be 0 to 14745 (allocatable maximum of the largest GPU)"),
    ("cpu_millicores", SAMPLE, 100, 28800, "must be 100 to 28800 (allocatable maximum)"),
]


@pytest.mark.parametrize(("field", "base", "low", "high", "error"), EDGES,
                         ids=["time code_eval", "time python_ml", "time code_run", "ram", "vram", "cpu"])
def test_both_edges_of_every_range(spool: Spool, field: str, base: SubmitRequest, low: int, high: int, error: str):
    """Catches < written for <= (or the reverse) at either end, a bound taken from the wrong type or
    row, and a huge integer that overflows instead of being refused."""
    source = spool(PY_SOURCE if base.workload_type == "python_ml" else C_SOURCE)
    for value in (low, high):
        validate(change(base, **{field: value}), source, S0, DEFAULTS)
    for value in (low - 1, high + 1, 10**30, -(10**30)):
        assert refusal(change(base, **{field: value}), source) == (field, error)


def test_vram_must_be_0_when_the_host_has_no_gpu(spool: Spool):
    no_gpu = dataclasses.replace(S0, vram_max=0)
    source = spool(C_SOURCE)
    assert validate(SAMPLE, source, no_gpu, DEFAULTS) == Accepted(1, "prog.c")
    assert refusal(change(vram_mib=1), source, refs=no_gpu) == ("vram_mib", "must be 0 (this host has no GPU)")


def test_the_first_failing_field_is_reported_in_plan_order(spool: Spool):
    """Catches checks run in another order: every field starts bad, and each fix moves the 400 on to
    the next field of plan 8.1, then stdin, a cheap check made before the file is read (D-129), and
    source_name and then source last (D-79)."""
    req = SubmitRequest("no_such_type", 99, 63, -1, 0, problem_id=999, lab_session_id=8, source_name="prog.h",
                        stdin=b"7\n")
    refs = dataclasses.replace(S0, problem_found=False, session=Session("lab", False))
    source = spool(b"")
    seen: list[str | None] = []
    for _ in range(10):
        field, _error = refusal(req, source, refs)
        seen.append(field)
        if field == "workload_type":
            req = change(req, workload_type="code_eval")
        elif field == "problem_id":
            refs = dataclasses.replace(refs, problem_found=True)
        elif field == "lab_session_id":
            refs = dataclasses.replace(refs, session=Session("lab", True))
        elif field == "time_limit_s":
            req = change(req, time_limit_s=5)
        elif field == "ram_mib":
            req = change(req, ram_mib=64)
        elif field == "vram_mib":
            req = change(req, vram_mib=0)
        elif field == "cpu_millicores":
            req = change(req, cpu_millicores=100)
        elif field == "stdin":
            req = change(req, stdin=None)
        elif field == "source_name":
            req = change(req, source_name="prog.c")
        else:
            source = spool(C_SOURCE)
    assert seen == ["workload_type", "problem_id", "lab_session_id", "time_limit_s", "ram_mib", "vram_mib",
                    "cpu_millicores", "stdin", "source_name", "source"]
    assert validate(req, source, refs, DEFAULTS) == Accepted(2, "prog.c")


def test_problem_id_is_required_for_code_eval_and_empty_for_every_other_type(spool: Spool):
    source = spool(C_SOURCE)
    assert refusal(SAMPLE, source, refs=dataclasses.replace(S0, problem_found=False)) == (
        "problem_id", "does not exist")
    cpu = change(workload_type="cpu_bound", problem_id=None, lab_session_id=None)
    assert validate(cpu, source, S0, DEFAULTS) == Accepted(3, "prog.c")
    assert refusal(change(cpu, problem_id=12), source) == ("problem_id", "must be empty for cpu_bound")
    assert refusal(change(ML, problem_id=12), spool(ML_ZIP)) == ("problem_id", "must be empty for python_ml")


def test_need_class_is_1_only_for_an_open_exam_session(spool: Spool):
    """Catches class 1 for a lab session or for no session, and a session that is closed or unknown
    taken as no session at all."""
    source, archive = spool(C_SOURCE), spool(ML_ZIP)
    assert validate(SAMPLE, source, S0, DEFAULTS).need_class == 1
    lab = dataclasses.replace(S0, session=Session("lab", True))
    assert validate(change(lab_session_id=8), source, lab, DEFAULTS).need_class == 2
    none = dataclasses.replace(S0, session=None)
    assert validate(change(lab_session_id=None), source, none, DEFAULTS).need_class == 2
    assert validate(ML, archive, none, DEFAULTS).need_class == 3
    assert validate(change(ML, lab_session_id=7), archive, S0, DEFAULTS).need_class == 1
    assert refusal(SAMPLE, source, refs=dataclasses.replace(S0, session=Session("exam", False))) == (
        "lab_session_id", "is not open")
    assert refusal(SAMPLE, source, refs=none) == ("lab_session_id", "does not exist")


CODE_NAMES = [("prog.c", "prog.c"), ("solution.cpp", "prog.cpp"), ("Solution.java", "Main.java"), ("main.py", "prog.py"),
              ("../../x.c", "prog.c"), ("..\\..\\x.cpp", "prog.cpp"), ("C:\\Users\\me\\prog.c", "prog.c")]
CODE_REFUSED = ["prog.C", "prog.CPP", "prog.Py", "Main.JAVA", "prog.h", "prog", "prog.c.exe", "prog.c\x00",
                "prog.c ", "model.zip"]


@pytest.mark.parametrize("type_code", CODE_TYPES)
def test_code_types_take_c_cpp_java_and_py_in_lower_case_only(spool: Spool, type_code: str):
    """Catches a case-insensitive match (gcc compiles x.C as C++), a substring match (prog.c.exe), a zip
    outside python_ml, and a name used as a path: only the extension picks the stored name (D-76).
    code_run takes the same four languages (D-96)."""
    base = change(workload_type=type_code, problem_id=12 if type_code == "code_eval" else None)
    source = spool(C_SOURCE)
    for name, stored in CODE_NAMES:
        assert validate(change(base, source_name=name), source, S0, DEFAULTS).file_name == stored
    for name in CODE_REFUSED:
        assert refusal(change(base, source_name=name), source) == ("source_name", CODE_EXTENSIONS)


def test_python_ml_takes_py_and_zip_only(spool: Spool):
    archive = spool(ML_ZIP)
    assert validate(ML_PY, spool(PY_SOURCE), S0, DEFAULTS) == Accepted(3, "prog.py")
    assert validate(ML, archive, S0, DEFAULTS) == Accepted(3, "payload.zip")
    for name in ["prog.c", "x.cpp", "Main.java", "model.ZIP", "train.PY", "model.tar.gz", "model"]:
        assert refusal(change(ML, source_name=name), archive) == ("source_name", "must end in .py or .zip")


def test_source_size_caps_come_from_config(spool: Spool):
    """Catches a cap written into the code: code types use SOURCE_MAX_BYTES (65,536 by default) and
    python_ml uses PYTHON_ML_UPLOAD_MAX_BYTES; both edges of each, then smaller caps set in Config."""
    assert validate(SAMPLE, spool(b"x"), S0, DEFAULTS).file_name == "prog.c"
    assert validate(SAMPLE, spool(b"x" * 65536), S0, DEFAULTS).file_name == "prog.c"
    assert refusal(SAMPLE, spool(b"")) == ("source", "must be 1 to 65536 bytes")
    assert refusal(SAMPLE, spool(b"x" * 65537)) == ("source", "must be 1 to 65536 bytes")
    small = Config(source_max_bytes=100, python_ml_upload_max_bytes=200)
    assert validate(SAMPLE, spool(b"x" * 100), S0, small).file_name == "prog.c"
    assert refusal(SAMPLE, spool(b"x" * 101), config=small) == ("source", "must be 1 to 100 bytes")
    assert validate(ML_PY, spool(b"#" * 200), S0, small).file_name == "prog.py"
    assert refusal(ML_PY, spool(b"#" * 201), config=small) == ("source", "must be 1 to 200 bytes")


def test_c_cpp_and_py_sources_must_be_utf8_while_java_is_checked_for_size_only(spool: Spool):
    """Catches a missing UTF-8 check on text sources, and any content check on .java, which E2 does
    not make (D-79): a file without a runnable class Main is the judge's compile error (E1-1)."""
    not_utf8 = spool(NOT_UTF8)
    for name in ["prog.c", "prog.cpp", "prog.py"]:
        assert refusal(change(source_name=name), not_utf8) == ("source", "must be UTF-8 text")
    assert refusal(ML_PY, not_utf8) == ("source", "must be UTF-8 text")
    for content in [NOT_UTF8, b"public final class Main {}", b"class\tSolution {}", JAVA_SOURCE]:
        assert validate(change(source_name="Main.java"), spool(content), S0, DEFAULTS).file_name == "Main.java"
    assert refusal(change(source_name="Main.java"), spool(b"x" * 65537)) == ("source", "must be 1 to 65536 bytes")


def test_a_large_utf8_source_is_accepted_whatever_falls_on_a_chunk_boundary(spool: Spool):
    """Catches a chunked UTF-8 check that decodes each chunk on its own: the euro sign takes 3 bytes,
    so one of them straddles every boundary at a power of two."""
    big = ("\u20ac" * 700_000).encode()
    assert len(big) == 2_100_000
    assert validate(ML_PY, spool(big), S0, DEFAULTS) == Accepted(3, "prog.py")
    assert refusal(ML_PY, spool(big + b"\xe2\x82")) == ("source", "must be UTF-8 text")


@pytest.mark.parametrize(("files", "links", "error"), [
    ({"train.py": b"print(1)\n"}, (), "must have main.py at its top level"),
    ({"pkg/main.py": b"print(1)\n"}, (), "must have main.py at its top level"),
    ({}, (), "must have main.py at its top level"),
    ({"main.py": b"1", "/etc/cron.d/x": b"1"}, (), "must not contain an absolute path"),
    ({"main.py": b"1", "C:\\Windows\\x.dll": b"1"}, (), "must not contain an absolute path"),
    ({"main.py": b"1", "\\\\server\\share\\x": b"1"}, (), "must not contain an absolute path"),
    ({"main.py": b"1", "../x.py": b"1"}, (), "must not contain a '..' part"),
    ({"main.py": b"1", "data/../../x.py": b"1"}, (), "must not contain a '..' part"),
    ({"main.py": b"1", "data\\..\\..\\x.py": b"1"}, (), "must not contain a '..' part"),
    ({"main.py": b"1", "a/..\x00junk": b"1"}, (), "must not contain a '..' part"),
    ({"main.py": b"1"}, ("data/link",), "must not contain a link"),
], ids=["no main.py", "main.py in a folder", "empty", "absolute", "drive letter", "unc", "dot dot",
        "dot dot inside", "dot dot backslash", "dot dot before a nul", "symlink"])
def test_an_unsafe_or_incomplete_zip_is_refused(spool: Spool, files: dict[str, bytes], links: tuple[str, ...],
                                                error: str):
    """Catches each zip rule left out (D-79), including '..' written with backslashes, which Linux keeps
    in the entry name and Windows tools produce."""
    assert refusal(ML, spool(zip_of(files, links))) == ("source", error)


@pytest.mark.parametrize(("extracted", "error"), [
    ("../../x.py", "must not contain a '..' part"),
    ("/etc/cron.d/x", "must not contain an absolute path"),
    ("C:\\Windows\\x.dll", "must not contain an absolute path"),
], ids=["dot dot", "absolute", "drive letter"])
def test_a_unicode_path_field_cannot_hide_an_unsafe_name(spool: Spool, extracted: str, error: str):
    """Catches the name rules applied to the stored name only: zipfile extracts an entry stored as
    ok.txt under the name in its Unicode Path field, which here leaves the job folder (review of E2-1).
    Python 3.11 does not read the field and extracts the entry as ok.txt, so there it is accepted."""
    data = unicode_path_zip("ok.txt", extracted)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entry = archive.infolist()[1]
    assert entry.orig_filename == "ok.txt"
    assert (entry.filename != "ok.txt") == (sys.version_info >= (3, 12))
    if entry.filename == "ok.txt":
        assert validate(ML, spool(data), S0, DEFAULTS) == Accepted(3, "payload.zip")
    else:
        assert refusal(ML, spool(data)) == ("source", error)


def test_bytes_that_are_not_a_zip_archive_are_refused(spool: Spool):
    """Catches a reader error escaping as a 500, among them the NotImplementedError zipfile raises for
    an entry that needs a zip version above 6.3, and the OSError its end record reader raises on a file
    too short for the zip64 record that a locator promises: on a real file it seeks before the start,
    where on bytes it stopped at 0 (review of E2-5)."""
    too_new = zipfile.ZipInfo("main.py")
    too_new.extract_version = 64
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(too_new, b"print(1)\n")
    short_zip64 = (struct.pack("<4sLQL", b"PK\x06\x07", 0, 0, 1)  # a zip64 locator, 20 bytes
                   + struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, 0, 0, 0, 0, 0))  # an empty end record, 22 bytes
    for data in [b"PK\x03\x04 not a zip", ML_ZIP[: len(ML_ZIP) // 2], b"\x00" * 100, buffer.getvalue(), short_zip64]:
        assert refusal(ML, spool(data)) == ("source", "must be a zip archive")


def test_zip_caps_come_from_config(spool: Spool):
    """Catches a hard-coded entry or size cap, and compressed sizes summed instead of the sizes the
    entries unpack to: 3 entries and 100 declared bytes pass, one more of either fails."""
    caps = Config(python_ml_max_entries=3, python_ml_unzipped_max_bytes=100)
    three = zip_of({"main.py": b"x" * 50, "a.csv": b"x" * 25, "b.csv": b"x" * 25})
    assert validate(ML, spool(three), S0, caps) == Accepted(3, "payload.zip")
    four = zip_of({"main.py": b"x", "a": b"x", "b": b"x", "c": b"x"})
    assert refusal(ML, spool(four), config=caps) == ("source", "must have at most 3 entries")
    over = zip_of({"main.py": b"x" * 50, "a.csv": b"x" * 51}, compression=zipfile.ZIP_DEFLATED)
    assert len(over) < 300  # deflated: far fewer bytes than the 101 they unpack to plus the headers
    assert refusal(ML, spool(over), config=caps) == ("source", "must unpack to at most 100 bytes")


def test_a_zip_directory_too_large_for_the_entry_cap_is_refused_before_it_is_parsed(
        spool: Spool, monkeypatch: pytest.MonkeyPatch):
    """Catches the entry cap checked only after zipfile has built one object per directory record: a
    1 GiB upload of tiny records would cost gigabytes first. The end record alone decides: too many
    entries, or a directory larger than PYTHON_ML_MAX_ENTRIES times 1 KiB (D-85)."""
    caps = Config(python_ml_max_entries=3)
    many = spool(zip_of({"main.py": b"1", **{str(i): b"" for i in range(1000)}}))
    long_names = spool(zip_of({"main.py": b"1", "n" * 3100: b""}))

    def parsed(*args: object, **kwargs: object) -> None:
        raise AssertionError("zipfile parsed the whole directory")

    monkeypatch.setattr(validation.zipfile, "ZipFile", parsed)
    assert refusal(ML, many, config=caps) == ("source", "must have at most 3 entries")
    assert refusal(ML, long_names, config=caps) == ("source", "has a zip directory larger than 3072 bytes")


def test_an_end_record_that_understates_its_entries_is_refused_before_the_directory_is_parsed(
        spool: Spool, monkeypatch: pytest.MonkeyPatch):
    """Catches the entry cap taken from the end record's count alone: zipfile parses every record inside
    the declared directory size and ignores the count, so a 10 MB upload declaring 1 entry made it build
    222,608 objects (3.4 s, 83 MiB) before the refusal (Q1-14, Q1-16). Here 10 records say they are 1."""
    caps = Config(python_ml_max_entries=3)
    ten = zip_of({"main.py": b"1", **{f"f{i}": b"" for i in range(9)}})
    end = len(ten) - 22
    assert ten[end:end + 4] == b"PK\x05\x06"
    one = ten[:end + 8] + struct.pack("<2H", 1, 1) + ten[end + 12:]  # both entry counts of the end record

    def parsed(*args: object, **kwargs: object) -> None:
        raise AssertionError("zipfile parsed the whole directory")

    monkeypatch.setattr(validation.zipfile, "ZipFile", parsed)
    assert refusal(ML, spool(one), config=caps) == ("source", "must have at most 3 entries")


@pytest.mark.parametrize("zip64", [False, True], ids=["classic", "zip64"])
def test_a_zip_stored_as_the_last_entry_adds_no_count(spool: Spool, monkeypatch: pytest.MonkeyPatch, zip64: bool):
    """Catches the record count reaching past the directory (review of Q-1): an archive stored last (an .npz,
    .pt or .whl is a zip file) ends with directory records of its own just before the outer directory, so
    main.py and data.npz were refused under a cap of 2. Run it on 3.12.15 too: a zipfile with the
    CVE-2025-8291 fix reports a zip64 directory as ending at the zip64 record (D-133)."""
    inner = zip_of({"a": b"1"})
    with monkeypatch.context() as m:
        if zip64:
            m.setattr(zipfile, "ZIP_FILECOUNT_LIMIT", 0)
        data = zip_of({"main.py": b"print(1)\n", "data.npz": inner})
    assert data.count(b"PK\x01\x02") == 3 and (b"PK\x06\x06" in data) == zip64  # the inner record and the two
    assert validate(ML, spool(data), S0, Config(python_ml_max_entries=2)) == Accepted(3, "payload.zip")


def test_a_zip64_archive_larger_than_the_end_copy_is_accepted(spool: Spool, monkeypatch: pytest.MonkeyPatch):
    """Catches the end record read from a copy of the archive's last 128 KiB that loses the file's
    offsets: since the CVE-2025-8291 fix (CPython 3.12.12 and 3.13.9) zipfile seeks to the absolute offset
    the zip64 locator gives, and every valid zip64 archive over 128 KiB was refused there (Q1-07). The
    3.13.5 venv predates the fix, so this test fails without it only on a fixed Python (3.12.15, D-132).
    zipfile writes zip64 end records only past 65,535 entries, so the limit is lowered while it writes,
    as writers that always use zip64 do."""
    with monkeypatch.context() as m:
        m.setattr(zipfile, "ZIP_FILECOUNT_LIMIT", 0)
        data = zip_of({"main.py": b"print(1)\n", "weights.bin": bytes(300_000)})
    assert b"PK\x06\x06" in data and b"PK\x06\x07" in data  # a zip64 end record and its locator
    assert len(data) > 131072
    assert validate(ML, spool(data), S0, DEFAULTS) == Accepted(3, "payload.zip")


def test_the_end_record_reader_finds_the_archive_at_its_file_offsets(spool: Spool, monkeypatch: pytest.MonkeyPatch):
    """Catches the end copy losing the file's offsets on any Python (review of Q-1): the 3.13.5 reader never
    seeks to one, so the zip64 test above goes red only on a Python with the CVE-2025-8291 fix. Here the
    real reader is handed the copy after it is asked for the end record at its offset in the file (D-132)."""
    data = zip_of({"main.py": b"print(1)\n", "weights.bin": bytes(300_000)})
    seen: list[bytes] = []
    real = validation._END_RECORD

    def reader(copy: BinaryIO) -> list[Any] | None:
        copy.seek(len(data) - 22)
        seen.append(copy.read(4))
        return real(copy)

    monkeypatch.setattr(validation, "_END_RECORD", reader)
    assert validate(ML, spool(data), S0, DEFAULTS) == Accepted(3, "payload.zip")
    assert seen == [b"PK\x05\x06"]


# ---- submit() on a seeded database ------------------------------------------------------------------

def files_under(root: Path) -> set[str]:
    return {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}


def job_row(job_id: int) -> tuple[Any, ...]:
    """(user_id, type_code, lab_session_id, problem_id, cpu_millicores, ram_mib, vram_mib, time_limit_s,
    need_class, state, retry_count, payload_path, submitted_at)."""
    with tx("E4") as cur:
        row = cur.execute("SELECT user_id, type_code, lab_session_id, problem_id, cpu_millicores, ram_mib, vram_mib, "
                          "time_limit_s, need_class, state, retry_count, payload_path, submitted_at FROM jobs "
                          "WHERE job_id = %s", (job_id,)).fetchone()
    assert row is not None
    return tuple(row)


def history(job_id: int) -> list[tuple[Any, ...]]:
    with tx("E4") as cur:
        return [tuple(row) for row in cur.execute(
            "SELECT from_state, to_state, actor, reason FROM job_state_history WHERE job_id = %s ORDER BY event_id",
            (job_id,)).fetchall()]


def counts() -> tuple[int, int]:
    """(jobs, history rows)."""
    with tx("E4") as cur:
        row = cur.execute("SELECT (SELECT count(*) FROM jobs), (SELECT count(*) FROM job_state_history)").fetchone()
    assert row is not None
    return row[0], row[1]


def refused_by_submit(e2: E2Service, req: SubmitRequest, source: Path) -> tuple[str | None, str]:
    with pytest.raises(ApiError) as caught:
        e2.submit(STUDENT, req, source)
    assert caught.value.status == 400
    return caught.value.field, caught.value.error


def new_session(kind: str, opens_in: str, closes_in: str) -> int:
    """A lab session of FAC001, its window relative to the database clock."""
    with tx("E2") as cur:
        row = cur.execute("INSERT INTO lab_sessions (faculty_id, kind, opens_at, closes_at) VALUES ('FAC001', %s, "
                          "clock_timestamp() + %s::interval, clock_timestamp() + %s::interval) "
                          "RETURNING lab_session_id", (kind, opens_in, closes_in)).fetchone()
    assert row is not None
    return row[0]


def test_the_section_12_sample_queues_job_1042_with_its_payload_history_and_event(
        e2: E2Service, seeded: TempDb, tmp_path: Path, spool: Spool):
    """Catches a wrong column, a missing payload or history row, and the job_state event sent before
    COMMIT (the stub hub refuses a push inside a transaction) or with an at other than submitted_at."""
    assert e2.submit(STUDENT, SAMPLE, spool(C_SOURCE)) == SubmitResult(1042, "queued", 1)
    *row, payload_path, submitted_at = job_row(1042)
    assert row == ["01FE24BCS901", "code_eval", 7, 12, 1000, 512, 0, 5, 1, "queued", 0]
    assert re.fullmatch(r"payloads/[0-9a-f]{32}", payload_path)
    assert files_under(tmp_path) == {f"{payload_path}/prog.c"}
    assert (tmp_path / payload_path / "prog.c").read_bytes() == C_SOURCE
    assert history(1042) == [(None, "queued", "E2", "submitted")]
    assert seeded.stubs.hub.events == [{"type": "job_state", "job_id": 1042, "user_id": "01FE24BCS901",
                                        "state": "queued", "reason": "submitted", "at": submitted_at.isoformat()}]
    assert_invariants(settled=True)


def test_a_committed_submit_is_queued_after_commit_and_before_its_job_state_event(
        e2: E2Service, seeded: TempDb, monkeypatch: pytest.MonkeyPatch, spool: Spool):
    """Catches the push left out, made inside the transaction (before COMMIT), made after the job_state
    event (a page that refetches the queue on that event would miss the job), or made with the type's
    class or Python's clock instead of the accepted class and the row's submitted_at (plan 8.1 step 4),
    or with a lock of its own instead of E2's (D-104)."""
    real_push, real_publish = e2.queue.push, service._publish
    seen: list[tuple[str, bool]] = []

    def push(entry: QueueEntry) -> None:
        seen.append(("push", in_transaction()))
        real_push(entry)

    def publish(event: dict[str, Any]) -> None:
        seen.append(("publish", event["job_id"] in e2.queue))
        real_publish(event)

    monkeypatch.setattr(e2.queue, "push", push)
    monkeypatch.setattr(service, "_publish", publish)
    assert e2.submit(STUDENT, SAMPLE, spool(C_SOURCE)) == SubmitResult(1042, "queued", 1)
    assert seen == [("push", False), ("publish", True)]
    submitted_at = job_row(1042)[12]
    assert e2.queue.in_fifo_order() == [QueueEntry(1042, "01FE24BCS901", 1, submitted_at, Vector(1000, 512, 0))]
    assert e2.queue._lock is e2.lock                         # E2's one lock (D-104), not a second one
    assert_invariants(settled=True)


def test_the_bounds_follow_the_capacity_loaded_in_resources(e2: E2Service, seeded: TempDb, spool: Spool):
    """Catches S0's maxima written into the code or read once at start-up (SUMMARY gotcha): on S0 RAM
    stops at 117,964 MiB with the §12 text; after capacity.apply of a 4-core, 8 GiB host with no GPU,
    the same E2Service stops at 3,600 millicores and 7,372 MiB, and VRAM must be 0."""
    req = change(lab_session_id=None)
    e2.submit(STUDENT, change(req, ram_mib=117964), spool(C_SOURCE))
    assert refused_by_submit(e2, change(req, ram_mib=117965), spool(C_SOURCE)) == (
        "ram_mib", "must be 64 to 117964 (allocatable maximum)")
    with tx("E3") as cur:
        apply(cur, [Row("cpu", None, 4000), Row("ram", None, 8192)])
    e2.submit(STUDENT, change(req, cpu_millicores=3600, ram_mib=7372), spool(C_SOURCE))
    assert refused_by_submit(e2, change(req, ram_mib=7373), spool(C_SOURCE)) == (
        "ram_mib", "must be 64 to 7372 (allocatable maximum)")
    assert refused_by_submit(e2, change(req, cpu_millicores=3601), spool(C_SOURCE)) == (
        "cpu_millicores", "must be 100 to 3600 (allocatable maximum)")
    assert refused_by_submit(e2, change(req, vram_mib=1), spool(C_SOURCE)) == (
        "vram_mib", "must be 0 (this host has no GPU)")
    assert counts() == (2, 2)
    assert_invariants(settled=True)


@pytest.mark.parametrize("user", [User("01FE24BCS902", "student"), User("FAC001", "faculty"),
                                  User("ADM001", "administrator")], ids=["student", "faculty", "administrator"])
def test_need_class_comes_from_the_session_and_the_type_never_from_the_role(e2: E2Service, seeded: TempDb,
                                                                           spool: Spool, user: User):
    """Catches a class taken from the role, from a lab session, or recomputed after the INSERT: the
    reply and the stored row must both carry the baseline's class."""
    cpu = change(workload_type="cpu_bound", problem_id=None)
    cases = [(change(lab_session_id=7), C_SOURCE, 1), (change(lab_session_id=8), C_SOURCE, 2),
             (change(lab_session_id=None), C_SOURCE, 2), (cpu, C_SOURCE, 1),
             (change(cpu, lab_session_id=None), C_SOURCE, 3), (change(ML_PY, lab_session_id=7), PY_SOURCE, 1),
             (ML_PY, PY_SOURCE, 3)]
    for req, source, expected in cases:
        result = e2.submit(user, req, spool(source))
        assert (result.need_class, job_row(result.job_id)[8]) == (expected, expected)
    assert_invariants(settled=True)


def test_every_refusal_writes_no_job_no_folder_and_no_event(e2: E2Service, seeded: TempDb, tmp_path: Path,
                                                            spool: Spool):
    """Catches a payload written before validation ends, a job inserted or queued for a refused request,
    a session that closed an hour ago or opens in an hour taken as open, and a refused upload consumed:
    each stays in the spool, untouched, for E4-3 to delete (D-90)."""
    closed = new_session("exam", "-2 hours", "-1 hour")
    future = new_session("exam", "1 hour", "2 hours")
    cases = [(change(workload_type="unknown"), C_SOURCE, ("workload_type", ONE_OF)),
             (change(problem_id=999999), C_SOURCE, ("problem_id", "does not exist")),
             (change(lab_session_id=closed), C_SOURCE, ("lab_session_id", "is not open")),
             (change(lab_session_id=future), C_SOURCE, ("lab_session_id", "is not open")),
             (change(lab_session_id=999999), C_SOURCE, ("lab_session_id", "does not exist")),
             (change(time_limit_s=61), C_SOURCE, ("time_limit_s", "must be 1 to 60 (maximum for code_eval)")),
             (change(vram_mib=14746), C_SOURCE, ("vram_mib", "must be 0 to 14745 (allocatable maximum of the "
                                                 "largest GPU)")),
             (change(source_name="prog.C"), C_SOURCE, ("source_name", CODE_EXTENSIONS)),
             (SAMPLE, NOT_UTF8, ("source", "must be UTF-8 text")),
             (ML, zip_of({"main.py": b"1", "../evil.py": b"1"}), ("source", "must not contain a '..' part")),
             (ML, zip_of({"main.py": b"1"}, compression=zipfile.ZIP_BZIP2), (
                 "source", "must contain only stored or deflated entries")),
             (ML, zip_of({"main.py": b"1", "pkg": b"1", "pkg/a.py": b"1"}), (
                 "source", "must not contain a file and a folder of one name"))]
    left: dict[Path, bytes] = {}
    for req, source, expected in cases:
        path = spool(source)
        left[path] = source
        assert refused_by_submit(e2, req, path) == expected
    assert counts() == (0, 0)
    assert list(tmp_path.iterdir()) == [tmp_path / "spool"]
    assert {path: path.read_bytes() for path in (tmp_path / "spool").iterdir()} == left
    assert seeded.stubs.hub.events == [] and len(e2.queue) == 0
    assert_invariants(settled=True)


def logged(json_log: Path, event: str) -> list[dict[str, Any]]:
    return [line for line in map(json.loads, json_log.read_text(encoding="utf-8").splitlines())
            if line["event"] == event]


def test_a_failed_insert_leaves_no_job_and_removes_the_payload_folder(e2: E2Service, seeded: TempDb,
                                                                       tmp_path: Path, json_log: Path, spool: Spool):
    """Catches the payload folder left behind when the INSERT fails before COMMIT, and a failure that E2
    does not log: a user_id that names nobody breaks the jobs foreign key. The upload had already moved, so
    nothing is left anywhere, and submit_failed names the user and the folder, with the traceback."""
    with pytest.raises(errors.ForeignKeyViolation):
        e2.submit(User("nobody_here", "student"), SAMPLE, spool(C_SOURCE))
    assert counts() == (0, 0)
    assert list((tmp_path / "payloads").iterdir()) == []
    assert list((tmp_path / "spool").iterdir()) == []
    assert seeded.stubs.hub.events == [] and len(e2.queue) == 0
    [line] = logged(json_log, "submit_failed")
    assert (line["level"], line["user_id"], line["error"].split(":")[0]) == ("ERROR", "nobody_here",
                                                                            "ForeignKeyViolation")
    assert re.fullmatch(r"payloads/[0-9a-f]{32}", line["payload_path"]) and "traceback" in line
    assert_invariants(settled=True)


def test_a_failure_at_commit_keeps_the_payload_of_the_job_that_may_exist(
        e2: E2Service, seeded: TempDb, tmp_path: Path, json_log: Path, monkeypatch: pytest.MonkeyPatch,
        spool: Spool):
    """Catches the payload removed when COMMIT may have succeeded: here COMMIT succeeds and its
    acknowledgement is lost, so tx() raises before any hook runs, as after a real failed COMMIT.
    Deleting the folder would leave a queued job with no source: it stays, submit_commit_unknown names
    it, and neither a job_state event nor a queue push goes out: the job waits for rebuild() at the next
    start (E2-4)."""
    real_tx, real_commit = service.tx, psycopg.Connection.commit
    armed: list[bool] = []

    @contextmanager
    def watch(engine: Engine) -> Iterator[Tx]:
        with real_tx(engine) as t:
            yield t
            if t.hooks:  # the INSERT body is done; its COMMIT comes next
                armed.append(True)

    def commit_then_lose_the_acknowledgement(self: psycopg.Connection[Any]) -> None:
        real_commit(self)
        if armed:
            armed.clear()
            raise psycopg.OperationalError("server closed the connection unexpectedly")

    source = spool(C_SOURCE)
    monkeypatch.setattr(service, "tx", watch)
    monkeypatch.setattr(psycopg.Connection, "commit", commit_then_lose_the_acknowledgement)
    with pytest.raises(psycopg.OperationalError):
        e2.submit(STUDENT, SAMPLE, source)
    payload_path = job_row(1042)[11]
    assert counts() == (1, 1)
    assert (tmp_path / payload_path / "prog.c").read_bytes() == C_SOURCE
    assert [(line["level"], line["payload_path"], "traceback" in line)
            for line in logged(json_log, "submit_commit_unknown")] == [("ERROR", payload_path, True)]
    assert logged(json_log, "submit_failed") == []  # the job may exist: not a failure
    assert seeded.stubs.hub.events == [] and logged(json_log, "submit") == [] and len(e2.queue) == 0
    assert_invariants(settled=True)


def test_a_deadlock_on_the_insert_replays_only_the_insert(e2: E2Service, seeded: TempDb, tmp_path: Path,
                                                          monkeypatch: pytest.MonkeyPatch, spool: Spool):
    """Catches the payload written inside the retried transaction (one folder per attempt) and an event
    or a job left by the attempt that rolled back: the first INSERT dies with 40P01, retrying() runs the
    transaction again, and the job, its folder, its event and its queue entry each exist once."""
    real_insert = service.insert_job
    deadlocks = [errors.DeadlockDetected("deadlock detected")]

    def deadlock_once(*args: Any) -> Any:
        if deadlocks:
            raise deadlocks.pop()
        return real_insert(*args)

    monkeypatch.setattr(service, "insert_job", deadlock_once)
    result = e2.submit(STUDENT, SAMPLE, spool(C_SOURCE))
    assert counts() == (1, 1)
    assert len(list((tmp_path / "payloads").iterdir())) == 1
    assert [event["job_id"] for event in seeded.stubs.hub.events] == [result.job_id]
    assert [entry.job_id for entry in e2.queue.in_fifo_order()] == [result.job_id]
    assert_invariants(settled=True)


def test_retries_used_up_remove_the_payload_folder(e2: E2Service, seeded: TempDb, tmp_path: Path,
                                                   monkeypatch: pytest.MonkeyPatch, spool: Spool):
    """Catches the folder kept after the third deadlock: nothing committed, so nothing may stay."""
    def always_deadlock(*args: Any) -> Any:
        raise errors.DeadlockDetected("deadlock detected")

    monkeypatch.setattr(service, "insert_job", always_deadlock)
    with pytest.raises(errors.DeadlockDetected):
        e2.submit(STUDENT, SAMPLE, spool(C_SOURCE))
    assert counts() == (0, 0)
    assert list((tmp_path / "payloads").iterdir()) == []
    assert seeded.stubs.hub.events == [] and len(e2.queue) == 0
    assert_invariants(settled=True)


def test_the_need_class_is_fixed_when_the_request_is_read(e2: E2Service, seeded: TempDb,
                                                          monkeypatch: pytest.MonkeyPatch, spool: Spool):
    """Pins D-80: a session judged open when submit() reads it still gives class 1 if it closes while
    the payload is written, because the request arrived while it was open; the next request is refused."""
    real_write = E2Service._write_payload  # pyright: ignore[reportPrivateUsage]

    def close_then_write(self: E2Service, *args: Any) -> Any:
        with tx("E2") as cur:
            cur.execute("UPDATE lab_sessions SET closes_at = clock_timestamp() WHERE lab_session_id = 7")
        return real_write(self, *args)

    with monkeypatch.context() as patch:  # never monkeypatch.undo(): it would uninstall the stubs too
        patch.setattr(E2Service, "_write_payload", close_then_write)
        result = e2.submit(STUDENT, SAMPLE, spool(C_SOURCE))
    assert (result.need_class, job_row(result.job_id)[8]) == (1, 1)
    assert refused_by_submit(e2, SAMPLE, spool(C_SOURCE)) == ("lab_session_id", "is not open")
    assert_invariants(settled=True)


def test_the_log_names_the_job_and_the_refusal_but_never_the_file_name(e2: E2Service, seeded: TempDb,
                                                                       json_log: Path, spool: Spool):
    """Catches a log line missing a field an operator needs, and the raw file name written to the log,
    which D-76 forbids: only the stored name (prog.c) may appear."""
    result = e2.submit(STUDENT, change(source_name="..\\top secret plan.c"), spool(C_SOURCE))
    assert refused_by_submit(e2, change(source_name="../../still secret.h"), spool(C_SOURCE))[0] == "source_name"
    assert "secret" not in json_log.read_text(encoding="utf-8")
    assert [(line["job_id"], line["user_id"], line["type_code"], line["need_class"], line["file"])
            for line in logged(json_log, "submit")] == [(result.job_id, "01FE24BCS901", "code_eval", 1, "prog.c")]
    assert [(line["user_id"], line["field"], line["error"]) for line in logged(json_log, "submit_refused")] == [
        ("01FE24BCS901", "source_name", CODE_EXTENSIONS)]
    assert logged(json_log, "submit_failed") == []  # a refusal is the user's, not a failure of E2's
    assert_invariants(settled=True)


def test_a_payloads_folder_that_leads_outside_data_dir_is_refused(seeded: TempDb, tmp_path: Path):
    """Catches a payload written wherever DATA_DIR/payloads points (CLAUDE.md: every path resolves
    inside DATA_DIR): when it links to another folder, submit() fails as a server error, writes nothing
    there, inserts no job and leaves the upload in the spool for E4-3."""
    data, outside = tmp_path / "data", tmp_path / "outside"
    data.mkdir()
    outside.mkdir()
    link_folder(data / "payloads", outside)
    source = spooled(data, C_SOURCE)
    with pytest.raises(RuntimeError, match="resolves outside DATA_DIR"):
        E2Service(Config(data_dir=data)).submit(STUDENT, SAMPLE, source)
    assert list(outside.iterdir()) == []
    assert source.read_bytes() == C_SOURCE
    assert counts() == (0, 0)
    assert_invariants(settled=True)


@pytest.mark.parametrize("name", ["../../x.c", "..\\..\\x.c"], ids=["slashes", "backslashes"])
def test_a_path_like_name_writes_only_prog_c_inside_the_job_folder(e2: E2Service, seeded: TempDb, tmp_path: Path,
                                                                   spool: Spool, name: str):
    """Catches the user's file name used as a path: the request is accepted (only the extension
    counts), the one new file under DATA_DIR is payloads/<hex>/prog.c, and nothing appears where the
    name points, from the job folder, from DATA_DIR or from the working directory."""
    targets = [tmp_path / "x.c", tmp_path.parent / "x.c", tmp_path / "payloads" / "x.c", Path.cwd().parent.parent / "x.c"]
    before = [target.exists() for target in targets]
    result = e2.submit(STUDENT, change(source_name=name), spool(C_SOURCE))
    payload_path = job_row(result.job_id)[11]
    assert files_under(tmp_path) == {f"{payload_path}/prog.c"}
    assert (tmp_path / payload_path / "prog.c").read_bytes() == C_SOURCE
    assert [target.exists() for target in targets] == before
    assert_invariants(settled=True)


def test_each_language_is_stored_under_its_fixed_name_byte_for_byte(e2: E2Service, seeded: TempDb, tmp_path: Path,
                                                                    spool: Spool):
    """Catches a wrong entry of the extension table or a source changed on its way to disk (a zip
    extracted or re-encoded, text decoded and written again)."""
    cases = [(change(source_name="Solution.java"), JAVA_SOURCE, "Main.java"),
             (change(source_name="solution.cpp"), CPP_SOURCE, "prog.cpp"),
             (change(source_name="solve.py"), PY_SOURCE, "prog.py"),
             (ML, ML_ZIP, "payload.zip"), (ML_PY, PY_SOURCE, "prog.py")]
    for req, source, stored in cases:
        folder = tmp_path / job_row(e2.submit(STUDENT, req, spool(source)).job_id)[11]
        assert [path.name for path in folder.iterdir()] == [stored]
        assert (folder / stored).read_bytes() == source
    assert_invariants(settled=True)


def test_with_no_capacity_loaded_submit_fails_loudly_and_writes_nothing(e2: E2Service, seeded: TempDb,
                                                                         tmp_path: Path, spool: Spool):
    """Catches an empty resources table taken as zero capacity: every request would get a 400 for
    cpu_millicores instead of the operator hearing that capacity.py never ran. The upload stays in the
    spool for E4-3."""
    with psycopg.connect(seeded.owner_url, autocommit=True) as owner:
        owner.execute("DELETE FROM resources")
    source = spool(C_SOURCE)
    with pytest.raises(RuntimeError, match="no capacity loaded"):
        e2.submit(STUDENT, SAMPLE, source)
    assert counts() == (0, 0)
    assert files_under(tmp_path) == {f"spool/{source.name}"}
    assert_invariants(settled=True)


RUNS = 100


def test_two_concurrent_submits_get_distinct_job_ids_and_folders(e2: E2Service, seeded: TempDb, tmp_path: Path,
                                                                 spool: Spool):
    """Catches shared state in E2Service or an id drawn in Python: two threads submit at the same moment
    (a Barrier), 100 times; every job gets its own id, history row and payload folder, and the queue holds
    all of them in the database's (submitted_at, job_id) order, whichever INSERT commits first."""
    results: list[SubmitResult] = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        for _ in range(RUNS):
            barrier = threading.Barrier(2)

            def submit_now(barrier: threading.Barrier = barrier) -> SubmitResult:
                source = spool(C_SOURCE)
                barrier.wait(timeout=30)
                return e2.submit(STUDENT, SAMPLE, source)

            futures = [pool.submit(submit_now) for _ in range(2)]
            results += [future.result(timeout=60) for future in futures]
    assert len({result.job_id for result in results}) == 2 * RUNS
    assert counts() == (2 * RUNS, 2 * RUNS)
    assert len(list((tmp_path / "payloads").glob("*/prog.c"))) == 2 * RUNS
    assert list((tmp_path / "spool").iterdir()) == []
    with tx("E4") as cur:
        arrival = [row[0] for row in cur.execute("SELECT job_id FROM jobs ORDER BY submitted_at, job_id")]
    assert [entry.job_id for entry in e2.queue.in_fifo_order()] == arrival
    assert_invariants(settled=True)
