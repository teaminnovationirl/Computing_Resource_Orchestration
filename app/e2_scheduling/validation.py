"""Request checks of submit() (plan 8.1 with D-76, D-79, D-90, D-96 and D-154). No database; the only file read
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
import struct
import zipfile
from collections.abc import Callable, Mapping, Sequence
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
ZIP_MAX_EXTRA_FIELDS: Final = 16  # of one directory record, a proposal (D-155): real archives have at most 2
# The end record (22 bytes), its comment (at most 65,535) and the zip64 locator and record (20 and 56) all sit
# in the last 65,633 bytes of an archive; the end record is parsed from a copy of the last 128 KiB.
_END_TAIL: Final = 1 << 17
_DRIVE: Final = re.compile(r"[A-Za-z]:")
# What E1's extract_zip() refuses and the directory shows (D-151, D-153). No engine imports another, so E2 has a copy
# of those rules, and tests/e2/test_zip_parity.py runs both engines on the same archives.
ZIP_MAX_DEPTH: Final = 100  # levels of a name
ZIP_MAX_NAME_BYTES: Final = 4096
ZIP_MAX_PART_BYTES: Final = 255
_METHODS: Final = (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
_ENCRYPTED: Final = 0x1  # a general purpose flag bit
_NOT_READ: Final = 0x20 | 0x40  # patched data and strong encryption: zipfile raises once it opens a file with one
# zipfile's own reader of the end of central directory record (zip64 included), so the bound in _zip()
# applies to exactly the directory that ZipFile() parses next. It is private but has been in zipfile
# since Python 2; it is looked up here, so a Python without it fails at start-up, not on a request.
_END_RECORD: Final = cast(Callable[[BinaryIO], "list[Any] | None"], getattr(zipfile, "_EndRecData"))  # noqa: B009 (not in typeshed, so pyright refuses the attribute)
_END_ENTRIES: Final[int] = getattr(zipfile, "_ECD_ENTRIES_TOTAL")  # noqa: B009 (not in typeshed)
_END_DIRECTORY_SIZE: Final[int] = getattr(zipfile, "_ECD_SIZE")  # noqa: B009 (not in typeshed)
_END_LOCATION: Final[int] = getattr(zipfile, "_ECD_LOCATION")  # noqa: B009 (not in typeshed)
_END_SIGNATURE: Final[int] = getattr(zipfile, "_ECD_SIGNATURE")  # noqa: B009 (not in typeshed)
_CENTRAL_HEADER: Final = b"PK\x01\x02"  # the signature that starts every central directory record
_ZIP64_END: Final = b"PK\x06\x06"  # the signature of the zip64 end record
_ZIP64_END_BYTES: Final = 56 + 20  # the zip64 end record and its locator
_RECORD_BYTES: Final = 46  # of a directory record, before its name, extra fields and comment
_RECORD_LENGTHS: Final = struct.Struct("<28x3H")  # the lengths of those three, in the record
_FIELD: Final = struct.Struct("<2xH")  # the header of one extra field: its number and the length of its data


class _EndCopy(io.BytesIO):
    """The last bytes of an archive, seen at their offsets in the file (D-132). Since the CVE-2025-8291 fix
    (CPython 3.12.12 and 3.13.9) zipfile's end record reader seeks to the absolute offset a zip64 locator
    gives; older readers seek from the end only, which this copy keeps as it was. An offset before the
    copy is a negative seek, so ValueError, and the archive is refused."""

    def __init__(self, data: bytes, start: int) -> None:
        super().__init__(data)
        self._start = start

    def seek(self, pos: int, whence: int = io.SEEK_SET, /) -> int:
        return super().seek(pos - self._start if whence == io.SEEK_SET else pos, whence) + self._start

    def tell(self) -> int:
        return super().tell() + self._start


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
    """The python_ml archive rules (D-79, D-153), read from its central directory without unpacking anything.
    Sizes are the ones the entries declare: E1 must hold the same caps while it extracts."""
    cap = config.python_ml_max_entries
    budget = cap * ZIP_DIRECTORY_BYTES_PER_ENTRY
    try:
        with open(path, "rb") as file:  # an OSError here is the disk's, so it reaches the caller as one
            size = os.fstat(file.fileno()).st_size
            start = max(size - _END_TAIL, 0)
            file.seek(start)
            tail = file.read()
            # Parsed from bytes: zipfile's reader seeks back from the end with no guard, so on a real file
            # shorter than the zip64 record a locator promises it raises OSError, where on bytes it stops at 0
            # and the archive is refused below as it was before D-90 (review of E2-5). The copy keeps the
            # file's offsets for the readers that seek to them (D-132).
            end = _END_RECORD(_EndCopy(tail, start))
            if end is None:
                raise zipfile.BadZipFile("no end of central directory record")
            # ZipFile() builds one object per record inside the directory size this record declares and
            # ignores the count it declares, so the size is bounded and the records are counted first: a
            # 1 GiB upload of tiny records would otherwise cost gigabytes (D-133). It reads the size bytes that
            # end at the reported location, except that for a zip64 archive a zipfile from before the
            # CVE-2025-8291 fix reports the classic end record, 76 bytes past the zip64 one: the signature
            # at that location tells which.
            if end[_END_ENTRIES] > cap:
                raise _bad("source", f"must have at most {cap} entries")
            if end[_END_DIRECTORY_SIZE] > budget:
                raise _bad("source", f"has a zip directory larger than {budget} bytes")
            directory_end = end[_END_LOCATION]
            if end[_END_SIGNATURE] == _ZIP64_END:
                file.seek(directory_end)
                if file.read(4) != _ZIP64_END:
                    directory_end -= _ZIP64_END_BYTES
            first = max(directory_end - end[_END_DIRECTORY_SIZE], 0)
            file.seek(first)
            _directory(file.read(max(directory_end - first, 0)), cap)
            # The same open file, read from disk: the directory parsed is the one counted, of the size above.
            with zipfile.ZipFile(file) as archive:
                entries = archive.infolist()
    except (zipfile.BadZipFile, ValueError, EOFError, NotImplementedError):
        # ValueError: a bad offset or name encoding; NotImplementedError: a zip version above 6.3
        raise _bad("source", "must be a zip archive") from None
    if len(entries) > cap:  # a backstop, should a zipfile read its directory from elsewhere
        raise _bad("source", f"must have at most {cap} entries")
    _entries(entries, size, config)


def _directory(records: bytes, cap: int) -> None:
    """The bounds that hold before ZipFile() parses these bytes: how many records there are, and the extra fields of
    each. zipfile slices what is left of a record's extra fields again for every one of them, so a directory of 10 MB
    that is nothing else took 4.5 s (D-155). The walk steps as zipfile does, and bytes that are not records to their end
    are refused here, as ZipFile() refuses them: should a zipfile read its directory from elsewhere, these bounds fail
    closed instead of passing bytes they never judged (security review of E2-6)."""
    # Every record starts with the signature, which cannot overlap itself, so there are at most that many (D-133).
    if records.count(_CENTRAL_HEADER) > cap:
        raise _bad("source", f"must have at most {cap} entries")
    at = 0
    while records.startswith(_CENTRAL_HEADER, at) and len(records) - at >= _RECORD_BYTES:
        name, extra, comment = _RECORD_LENGTHS.unpack_from(records, at)
        field = at + _RECORD_BYTES + name
        end = min(field + extra, len(records))
        fields = 0
        while end - field >= _FIELD.size:
            fields += 1
            if fields > ZIP_MAX_EXTRA_FIELDS:
                raise _bad("source", f"must have at most {ZIP_MAX_EXTRA_FIELDS} extra fields in an entry")
            field += _FIELD.size + _FIELD.unpack_from(records, field)[0]
        at += _RECORD_BYTES + name + extra + comment
    if at < len(records):  # no whole record where one must start: ZipFile() refuses such a directory too
        raise zipfile.BadZipFile("the directory is not records to its end")


def _entries(entries: Sequence[zipfile.ZipInfo], archive_bytes: int, config: Config) -> None:
    """The rules on the entries, in one pass over at most PYTHON_ML_MAX_ENTRIES of them; the first that fails
    decides (D-154). For each entry first D-79's own, then what E1's extract_zip() refuses and the directory shows, in
    E1's words and with the name judged that it judges, the one zipfile extracts to."""
    packed = unpacked = 0
    offsets: set[int] = set()
    names = _Names(config.python_ml_max_entries)
    has_main = False
    for entry in entries:
        # Both names: the one stored in the header, and the one zipfile extracts to, which an Info-ZIP
        # Unicode Path field (0x7075) replaces and a NUL cuts short; on Windows it also has / for \.
        for name in dict.fromkeys((entry.orig_filename, entry.filename)):  # one, when they are the same
            if name.startswith(("/", "\\")) or _DRIVE.match(name):
                raise _bad("source", "must not contain an absolute path")
            if "/../" in "/" + name.replace("\\", "/") + "/":  # searched, not split: no list of its parts (D-155)
                raise _bad("source", "must not contain a '..' part")
        if stat.S_ISLNK(entry.external_attr >> 16):
            raise _bad("source", "must not contain a link")
        if entry.flag_bits & _ENCRYPTED:
            raise _bad("source", "must not contain an encrypted entry")
        if entry.compress_type not in _METHODS:
            raise _bad("source", "must contain only stored or deflated entries")
        if entry.flag_bits & _NOT_READ and not entry.filename.endswith("/"):  # a folder's own entry is never opened
            raise _bad("source", "must be a zip archive that can be unpacked")
        packed += entry.compress_size
        if packed > archive_bytes:
            raise _bad("source", "must not name more data than it holds")
        if entry.header_offset in offsets:
            raise _bad("source", "must not name one member twice")
        offsets.add(entry.header_offset)
        if len(entry.orig_filename.encode()) > ZIP_MAX_NAME_BYTES:  # the name before a NUL cut it short
            raise _bad("source", "must not contain a name too long to store")
        names.add(entry.filename)
        unpacked += entry.file_size
        has_main = has_main or entry.filename == "main.py"
    if unpacked > config.python_ml_unzipped_max_bytes:
        raise _bad("source", f"must unpack to at most {config.python_ml_unzipped_max_bytes} bytes")
    if not has_main:
        raise _bad("source", "must have main.py at its top level")


class _Names:
    """The files, and the folders (at most cap of them), that extract_zip() would make: '.' and empty parts of a name
    are dropped, and a name that ends in / is a folder. Each is kept under the number of the folder it is in, never by
    its whole path, so what it costs does not grow with its depth."""

    def __init__(self, cap: int) -> None:
        self._cap = cap
        self._folders: dict[tuple[int, bytes], int] = {}  # number 0 is the top level
        self._files: set[tuple[int, bytes]] = set()

    def add(self, name: str) -> None:
        raw = name.encode()
        parts = [part for part in raw.split(b"/") if part not in (b"", b".")]
        if len(raw) > ZIP_MAX_NAME_BYTES or any(len(part) > ZIP_MAX_PART_BYTES for part in parts):
            raise _bad("source", "must not contain a name too long to store")
        if len(parts) > ZIP_MAX_DEPTH:
            raise _bad("source", f"must have names of at most {ZIP_MAX_DEPTH} levels")
        is_folder = raw.endswith(b"/")
        inside = 0
        for part in parts if is_folder else parts[:-1]:
            inside = self._folder((inside, part))
        if is_folder:
            return
        if not parts:
            raise _bad("source", "must not contain a file without a name")
        file = (inside, parts[-1])
        if file in self._files:
            raise _bad("source", "must not contain two files of one name")
        if file in self._folders:
            raise _bad("source", "must not contain a file and a folder of one name")
        self._files.add(file)

    def _folder(self, folder: tuple[int, bytes]) -> int:
        """The number of a folder, which is new when no entry has named it before."""
        number = self._folders.get(folder)
        if number is None:
            if folder in self._files:
                raise _bad("source", "must not contain a file and a folder of one name")
            if len(self._folders) == self._cap:
                raise _bad("source", f"must have at most {self._cap} folders")
            number = self._folders[folder] = len(self._folders) + 1
        return number
