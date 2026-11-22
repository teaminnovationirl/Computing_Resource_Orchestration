"""Request checks of submit() (plan 8.1 with D-76, D-79, D-90 and D-96). No database; the only file read
is the source it is given, a chunk at a time.

validate() checks the fields in this order and raises ApiError(400, error, field) at the first that
fails: workload_type, problem_id, lab_session_id, time_limit_s, ram_mib, vram_mib, cpu_millicores,
stdin, source_name, source. stdin is cheap to check, so it comes before the file is read (D-129);
source_name comes before source because the name decides how the file is read. Error texts never
repeat what the user sent. The upper bounds come from Refs, which submit() reads from the database for
every request; the caps on the source come from Config.
"""
from __future__ import annotations

import codecs
import io
import os
import re
import stat
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Final, Literal, cast

from app.common.errors import ApiError
from app.common.types import SubmitRequest
from app.config import Config

FileName = Literal["prog.c", "prog.cpp", "Main.java", "prog.py", "payload.zip"]

ML: Final = "python_ml"
CODE_RUN: Final = "code_run"  # a practice run (D-96): one run with the user's stdin, no problem, no verdict
STDIN_MAX_BYTES: Final = 1 << 20  # 1 MiB, a proposal (D-96)
INPUT_FILE: Final = "input.txt"  # a practice run's stdin, stored beside the source (D-96)
TIME_MIN_S: Final = 1
RAM_MIN_MIB: Final = 64          # context §12 and the jobs CHECK
VRAM_MIN_MIB: Final = 0
CPU_MIN_MILLICORES: Final = 100  # plan 8.1 (proposal)
# The extension (after the last dot, lower case only) picks the stored name (D-76).
CODE_FILES: Final[dict[str, FileName]] = {"c": "prog.c", "cpp": "prog.cpp", "java": "Main.java", "py": "prog.py"}
ML_FILES: Final[dict[str, FileName]] = {"py": "prog.py", "zip": "payload.zip"}
UTF8_FILES: Final = frozenset({"prog.c", "prog.cpp", "prog.py"})  # Main.java: extension and size only
UTF8_CHUNK: Final = 1 << 20
ZIP_DIRECTORY_BYTES_PER_ENTRY: Final = 1024  # proposal (D-85): a directory record is 46 bytes plus its names
# The end record (22 bytes), its comment (at most 65,535) and the zip64 locator and record (20 and 56) all sit
# in the last 65,633 bytes of an archive; the end record is parsed from a copy of the last 128 KiB.
_END_TAIL: Final = 1 << 17
_DRIVE: Final = re.compile(r"[A-Za-z]:")
_SEPARATORS: Final = re.compile(r"[\\/]")
# zipfile's own reader of the end of central directory record (zip64 included), so the bound in _zip()
# applies to exactly the directory that ZipFile() parses next. It is private but has been in zipfile
# since Python 2; it is looked up here, so a Python without it fails at start-up, not on a request.
_END_RECORD: Final = cast(Callable[[BinaryIO], "list[Any] | None"], getattr(zipfile, "_EndRecData"))  # noqa: B009 (not in typeshed, so pyright refuses the attribute)
_END_ENTRIES: Final[int] = getattr(zipfile, "_ECD_ENTRIES_TOTAL")  # noqa: B009 (not in typeshed)
_END_DIRECTORY_SIZE: Final[int] = getattr(zipfile, "_ECD_SIZE")  # noqa: B009 (not in typeshed)


@dataclass(frozen=True)
class WorkloadType:
    """One workload_types row."""
    default_need_class: int
    max_time_limit_s: int


@dataclass(frozen=True)
class Session:
    """The lab session a request names."""
    kind: str      # "exam" or "lab"
    is_open: bool  # opens_at <= clock_timestamp() < closes_at, judged by the database


@dataclass(frozen=True)
class Refs:
    """What validate() needs from the database, read in one transaction by submit()."""
    types: Mapping[str, WorkloadType]  # every workload_types row by type_code
    problem_found: bool                # the integer problem_id names a problem
    session: Session | None            # the integer lab_session_id's session; None when there is none
    cpu_max: int                       # total - reserved of the cpu row
    ram_max: int                       # total - reserved of the ram row
    vram_max: int                      # the largest total - reserved of a GPU row; 0 with no GPU row


@dataclass(frozen=True)
class Accepted:
    need_class: int
    file_name: FileName


def validate(req: SubmitRequest, source: object, refs: Refs, config: Config) -> Accepted:
    """Accepted(need_class, file_name) for a request that passes every rule, or ApiError(400, error,
    field) naming the first field that does not. source is the path of the spooled upload (D-90), read
    only once every other field has passed; any value that is not a Path is refused. need_class is 1 for
    an open exam session and the workload type's default otherwise; the user's role is never read
    (FR-01)."""
    type_code, workload = _workload_type(req.workload_type, refs)
    _problem(type_code, req.problem_id, refs)
    session = _session(req.lab_session_id, refs)
    _in_range("time_limit_s", req.time_limit_s, TIME_MIN_S, workload.max_time_limit_s, f"maximum for {type_code}")
    _in_range("ram_mib", req.ram_mib, RAM_MIN_MIB, refs.ram_max, "allocatable maximum")
    if refs.vram_max == 0:
        _in_range("vram_mib", req.vram_mib, VRAM_MIN_MIB, 0, "", text="must be 0 (this host has no GPU)")
    else:
        _in_range("vram_mib", req.vram_mib, VRAM_MIN_MIB, refs.vram_max, "allocatable maximum of the largest GPU")
    _in_range("cpu_millicores", req.cpu_millicores, CPU_MIN_MILLICORES, refs.cpu_max, "allocatable maximum")
    _stdin(type_code, req.stdin)
    file_name = _file_name(type_code, req.source_name)
    _source(source, file_name, type_code == ML, config)
    need_class = 1 if session is not None and session.kind == "exam" else workload.default_need_class
    return Accepted(need_class, file_name)


def _bad(field: str, error: str) -> ApiError:
    return ApiError(400, error, field)


def _workload_type(value: object, refs: Refs) -> tuple[str, WorkloadType]:
    if value is None or value == "":
        raise _bad("workload_type", "is required")
    workload = refs.types.get(value) if isinstance(value, str) else None
    if not isinstance(value, str) or workload is None:
        raise _bad("workload_type", "must be one of " + ", ".join(sorted(refs.types)))
    return value, workload


def _problem(type_code: str, value: object, refs: Refs) -> None:
    if type_code != "code_eval":
        if value is not None:
            raise _bad("problem_id", f"must be empty for {type_code}")
        return
    if value is None:
        raise _bad("problem_id", "is required for code_eval")
    if type(value) is not int:
        raise _bad("problem_id", "must be an integer")
    if not refs.problem_found:
        raise _bad("problem_id", "does not exist")


def _session(value: object, refs: Refs) -> Session | None:
    if value is None:
        return None
    if type(value) is not int:
        raise _bad("lab_session_id", "must be an integer")
    if refs.session is None:
        raise _bad("lab_session_id", "does not exist")
    if not refs.session.is_open:
        raise _bad("lab_session_id", "is not open")
    return refs.session


def _in_range(field: str, value: object, low: int, high: int, why: str, text: str | None = None) -> None:
    """An int, never a bool or a float (integer units only, invariant 7), from low to high."""
    if value is None:
        raise _bad(field, "is required")
    if type(value) is not int:
        raise _bad(field, "must be an integer")
    if not low <= value <= high:
        raise _bad(field, text or f"must be {low} to {high} ({why})")


def _file_name(type_code: str, name: object) -> FileName:
    """Only the extension matters; the name itself is never stored or used in a path (D-76)."""
    if name is None or name == "":
        raise _bad("source_name", "is required")
    table = ML_FILES if type_code == ML else CODE_FILES
    found = table.get(name.rpartition(".")[2]) if isinstance(name, str) and "." in name else None
    if found is None:
        raise _bad("source_name", "must end in .py or .zip" if type_code == ML else "must end in .c, .cpp, .java or .py")
    return found


def _stdin(type_code: str, value: object) -> None:
    """A practice run's input (D-96): None or b"" is no input (an empty field is one not given, D-79);
    anything else is for code_run only, must be bytes and holds at most STDIN_MAX_BYTES. The input is never
    repeated in an error or a log line."""
    if value is None or value == b"":
        return
    if type_code != CODE_RUN:
        raise _bad("stdin", f"must be empty for {type_code}")
    if type(value) is not bytes:
        raise _bad("stdin", "must be bytes")
    if len(value) > STDIN_MAX_BYTES:
        raise _bad("stdin", f"must be at most {STDIN_MAX_BYTES} bytes")


def _source(source: object, file_name: FileName, ml: bool, config: Config) -> None:
    """The spooled upload (D-90), sized by stat and read a chunk at a time: never held whole in memory."""
    if source is None:
        raise _bad("source", "is required")
    if not isinstance(source, Path):
        raise _bad("source", "must be a file")
    cap = config.python_ml_upload_max_bytes if ml else config.source_max_bytes
    if not 1 <= source.stat().st_size <= cap:
        raise _bad("source", f"must be 1 to {cap} bytes")
    if file_name == "payload.zip":
        _zip(source, config)
    elif file_name in UTF8_FILES and not _is_utf8(source):
        raise _bad("source", "must be UTF-8 text")


def _is_utf8(path: Path) -> bool:
    """Strict UTF-8, read and decoded UTF8_CHUNK bytes at a time; the incremental decoder carries a
    character that straddles two chunks over to the next one, and final=True refuses one cut off at the
    end."""
    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        with open(path, "rb") as file:
            while chunk := file.read(UTF8_CHUNK):
                decoder.decode(chunk)
        decoder.decode(b"", final=True)
    except UnicodeDecodeError:
        return False
    return True


def _zip(path: Path, config: Config) -> None:
    """The python_ml archive rules (D-79), read from its central directory without unpacking anything.
    Sizes are the ones the entries declare: E1 must hold the same caps while it extracts."""
    cap = config.python_ml_max_entries
    budget = cap * ZIP_DIRECTORY_BYTES_PER_ENTRY
    try:
        with open(path, "rb") as file:  # an OSError here is the disk's, so it reaches the caller as one
            file.seek(max(os.fstat(file.fileno()).st_size - _END_TAIL, 0))
            tail = file.read()
        # Parsed from bytes: zipfile's reader seeks back from the end with no guard, so on a real file shorter
        # than the zip64 record a locator promises it raises OSError, where on bytes it stops at 0 and the
        # archive is refused below as it was before D-90 (review of E2-5).
        end = _END_RECORD(io.BytesIO(tail))
        if end is None:
            raise zipfile.BadZipFile("no end of central directory record")
        # ZipFile() builds one object per record inside the directory size this record declares, so
        # both are bounded first: a 1 GiB upload of tiny records would otherwise cost gigabytes.
        if end[_END_ENTRIES] > cap:
            raise _bad("source", f"must have at most {cap} entries")
        if end[_END_DIRECTORY_SIZE] > budget:
            raise _bad("source", f"has a zip directory larger than {budget} bytes")
        with zipfile.ZipFile(path) as archive:  # by path: zipfile reads the directory from disk
            entries = archive.infolist()
    except (zipfile.BadZipFile, ValueError, EOFError, NotImplementedError):
        # ValueError: a bad offset or name encoding; NotImplementedError: a zip version above 6.3
        raise _bad("source", "must be a zip archive") from None
    if len(entries) > cap:
        raise _bad("source", f"must have at most {cap} entries")
    for entry in entries:
        # Both names: the one stored in the header, and the one zipfile extracts to, which an Info-ZIP
        # Unicode Path field (0x7075) replaces and a NUL cuts short; on Windows it also has / for \.
        for name in (entry.orig_filename, entry.filename):
            if name.startswith(("/", "\\")) or _DRIVE.match(name):
                raise _bad("source", "must not contain an absolute path")
            if ".." in _SEPARATORS.split(name):
                raise _bad("source", "must not contain a '..' part")
        if stat.S_ISLNK(entry.external_attr >> 16):
            raise _bad("source", "must not contain a link")
    if sum(entry.file_size for entry in entries) > config.python_ml_unzipped_max_bytes:
        raise _bad("source", f"must unpack to at most {config.python_ml_unzipped_max_bytes} bytes")
    if not any(entry.filename == "main.py" for entry in entries):
        raise _bad("source", "must have main.py at its top level")
