"""Slice E2-6: what validate() and extract_zip() say of one python_ml archive (D-151, D-153, D-154).

No engine imports another, so E2 holds its own copy of the rules that E1's extract_zip() reads from the central
directory. One table of archives runs through both: they refuse with one text, or both accept. The oracle is D-151
and D-153 with the texts of E1-1; every archive is written here, header by header.

E2 may refuse more than E1, never less. E2's own rules are the table OWN. What E2 leaves to extract_zip() is out of
scope here (D-154): the table ONLY_E1 (CRC, data cut short, what a local header says, and whether one is where the
directory says: every header offset is E1's, even one past the end of the archive, which the directory does show),
and what the archive alone does not decide: the bytes really written, the room on the disk, the length of the whole
path on the disk (the name under the server's folders), and two names that one file system holds for one (Windows).

The module needs no database and no test helper, so its tables also run under a plain Python (Linux, D-154).
"""
from __future__ import annotations

import ast
import builtins
import io
import random
import stat
import struct
import sys
import tracemalloc
import zipfile
import zlib
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from types import FrameType
from typing import Any, NamedTuple

import pytest

from app.common.errors import ApiError
from app.common.types import SubmitRequest
from app.config import Config
from app.e1_execution import files
from app.e2_scheduling import validation
from app.e2_scheduling.validation import Accepted, Refs, WorkloadType, validate

WINDOWS = sys.platform == "win32"
UNICODE_PATHS = sys.version_info >= (3, 12)  # zipfile reads the Info-ZIP Unicode Path field (0x7075)
ML = SubmitRequest("python_ml", 2000, 4096, 1024, 600, source_name="model.zip")
REFS = Refs(types={"python_ml": WorkloadType(3, 3600)}, problem_found=False, session=None, cpu_max=28800,
            ram_max=117964, vram_max=14745)
CAP = 1000           # entries, and folders
UNPACKED = 1 << 20   # bytes
LINK = stat.S_IFLNK | 0o777


# ---- archives, written header by header -----------------------------------------------------------------

@dataclass(frozen=True)
class Member:
    """One member: its local header, its data and its directory record. The fields after version make a header, the
    data or the record say what is not so."""
    name: str
    data: bytes = b"1"
    method: int = zipfile.ZIP_STORED
    flags: int = 0
    mode: int = 0                  # the Unix mode, in the record
    extra: bytes = b""             # of the record
    comment: bytes = b""           # of the record
    version: int = 20              # needed to extract
    compressed: int | None = None  # the size in the record
    unpacked: int | None = None    # the size in the record
    crc: int | None = None         # in the record
    local_name: str | None = None
    raw: bytes | None = None       # in the place of the data as the method packs it
    header_of: int | None = None   # nothing of its own: the record names the header of this member
    moved: int = 0                 # added to the header's offset in the record


def packed(m: Member) -> bytes:
    if m.raw is not None:
        return m.raw
    if m.method != zipfile.ZIP_DEFLATED:
        return m.data
    deflate = zlib.compressobj(wbits=-15)
    return deflate.compress(m.data) + deflate.flush()


def local(m: Member) -> bytes:
    """The local header of m and its data."""
    name, body = (m.name if m.local_name is None else m.local_name).encode(), packed(m)
    return struct.pack("<4s5H3L2H", b"PK\x03\x04", m.version, m.flags | 0x800, m.method, 0, 0x21, zlib.crc32(m.data),
                       len(body), len(m.data), len(name), 0) + name + body


def archive(*members: Member, prefix: bytes = b"") -> bytes:
    body, directory = b"", b""
    offsets: list[int] = []
    for m in members:
        if m.header_of is None:
            offsets.append(len(body))
            body += local(m)
        else:
            offsets.append(offsets[m.header_of])
        name = m.name.encode()
        directory += struct.pack(
            "<4s6H3L5H2L", b"PK\x01\x02", 0x0314, m.version, m.flags | 0x800, m.method, 0, 0x21,
            zlib.crc32(m.data) if m.crc is None else m.crc, len(packed(m)) if m.compressed is None else m.compressed,
            len(m.data) if m.unpacked is None else m.unpacked, len(name), len(m.extra), len(m.comment), 0, 0,
            m.mode << 16, offsets[-1] + m.moved) + name + m.extra + m.comment
    return (prefix + body + directory
            + struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, len(members), len(members), len(directory), len(body), 0))


def with_tail(tail: bytes) -> bytes:
    """An archive of main.py whose directory goes on with tail, which its end record counts in."""
    data = archive(MAIN)
    end = bytearray(data[-22:])
    struct.pack_into("<L", end, 12, struct.unpack_from("<L", end, 12)[0] + len(tail))
    return data[:-22] + tail + bytes(end)


def field(number: int, data: bytes = b"") -> bytes:
    """One extra field."""
    return struct.pack("<HH", number, len(data)) + data


def unicode_path(stored: str, extracted: str) -> bytes:
    """The Info-ZIP Unicode Path field under which zipfile extracts the entry stored as `stored`."""
    return field(0x7075, struct.pack("<BL", 1, zlib.crc32(stored.encode())) + extracted.encode())


def named(*names: str) -> tuple[Member, ...]:
    """A member of each name; one that ends in / is a folder's own entry and holds nothing."""
    return tuple(Member(name, b"" if name.endswith("/") else b"1") for name in names)


MAIN = Member("main.py", b"print(1)\n")


# ---- the two engines ------------------------------------------------------------------------------------

def spooled(folder: Path, data: bytes) -> Path:
    source = folder / "upload"
    source.write_bytes(data)
    return source


def e2_says(folder: Path, data: bytes, cap: int = CAP) -> str | None:
    """validate()'s refusal, None when it accepts."""
    return refusal(spooled(folder, data), cap)


def refusal(source: Path, cap: int = CAP) -> str | None:
    try:
        assert validate(ML, source, REFS, Config(python_ml_max_entries=cap, python_ml_unzipped_max_bytes=UNPACKED)
                        ) == Accepted(3, "payload.zip")
    except ApiError as refused:
        assert (refused.status, refused.field) == (400, "source")
        return refused.error
    return None


def e1_says(folder: Path, data: bytes, cap: int = CAP) -> str | None:
    """extract_zip()'s refusal, None when it unpacks."""
    source, target = folder / "payload.zip", folder / "job"
    source.write_bytes(data)
    try:
        files.extract_zip(source, target, max_entries=cap, max_bytes=UNPACKED)
    except files.BadArchive as refused:
        return str(refused)
    files.remove_unpacked(target)
    return None


def both(folder: Path, data: bytes, cap: int = CAP) -> tuple[str | None, str | None]:
    return e2_says(folder, data, cap), e1_says(folder, data, cap)


# ---- the table: one fault each, beside a good main.py ---------------------------------------------------

class Row(NamedTuple):
    members: tuple[Member, ...]  # after main.py
    text: str | None             # what both engines say
    cap: int = CAP


METHOD = "must contain only stored or deflated entries"
ENCRYPTED = "must not contain an encrypted entry"
UNREADABLE = "must be a zip archive that can be unpacked"
TWO_FILES = "must not contain two files of one name"
FILE_AND_FOLDER = "must not contain a file and a folder of one name"
FIVE_FOLDERS = "must have at most 5 folders"
LEVELS = "must have names of at most 100 levels"
TOO_LONG = "must not contain a name too long to store"
MORE_DATA = "must not name more data than it holds"
TWICE = "must not name one member twice"
NO_NAME = "must not contain a file without a name"
ABSOLUTE = "must not contain an absolute path"
DOT_DOT = "must not contain a '..' part"
A_LINK = "must not contain a link"
EXTRA_FIELDS = "must have at most 16 extra fields in an entry"

RULES: dict[str, Row] = {
    **{f"method {name}": Row((Member("a", method=number),), METHOD)
       for name, number in {"deflate64": 9, "bzip2": 12, "lzma": 14, "zstandard": 93, "unknown": 99}.items()},
    "method of a folder's own entry": Row((Member("pkg/", b"", method=14),), METHOD),
    "encrypted": Row((Member("a", flags=1),), ENCRYPTED),
    "encrypted folder's own entry": Row((Member("pkg/", b"", flags=1),), ENCRYPTED),
    "encrypted with AES": Row((Member("a", flags=1, method=99),), ENCRYPTED),
    "flag of patched data": Row((Member("a", flags=0x20),), UNREADABLE),
    "flag of strong encryption": Row((Member("a", flags=0x40),), UNREADABLE),
    "two files": Row(named("a.py", "a.py"), TWO_FILES),
    "two files, main.py": Row(named("main.py"), TWO_FILES),
    "two files, a dot": Row(named("a.py", "./a.py"), TWO_FILES),
    "two files, two slashes": Row(named("pkg/a.py", "pkg//a.py"), TWO_FILES),
    "two files, a last dot": Row(named("a", "a/."), TWO_FILES),
    "two files, one far down": Row(named("a/b/c/d.py", "a/b/c2/d.py", "a/b/c/./d.py"), TWO_FILES),
    **{f"by a Unicode Path field, {fault}": Row((Member("u.py", extra=unicode_path("u.py", name)),),
                                                text if UNICODE_PATHS else None)
       for fault, name, text in (
           ("two files", "main.py", TWO_FILES), ("a file and a folder", "main.py/", FILE_AND_FOLDER),
           ("a part of 256 bytes", "x" * 256, TOO_LONG), ("4,220 bytes", "/".join(["x" * 200] * 21), TOO_LONG),
           ("levels", "a/" * 101, LEVELS), ("no name", ".", NO_NAME), ("dot dot", "../evil.py", DOT_DOT),
           ("absolute", "/evil.py", ABSOLUTE))},
    "a file, then a folder": Row(named("a", "a/"), FILE_AND_FOLDER),
    "a folder, then a file": Row(named("a/", "a"), FILE_AND_FOLDER),
    "a file, then a file in it": Row(named("a", "a/b.py"), FILE_AND_FOLDER),
    "a file in it, then the file": Row(named("a/b.py", "a"), FILE_AND_FOLDER),
    "a file, then a file in it, a level down": Row(named("a/b", "a/b/c.py"), FILE_AND_FOLDER),
    "a file in it, then the file, a level down": Row(named("a/b/c.py", "a/b"), FILE_AND_FOLDER),
    "a file, then a file in it, with a dot": Row(named("a", "./a/./b.py"), FILE_AND_FOLDER),
    "main.py as a folder": Row(named("main.py/a.py"), FILE_AND_FOLDER),
    "folders no entry names": Row(named("a/b/c/d/e/f/x"), FIVE_FOLDERS, cap=5),
    "folders of two names": Row(named("a/b/c/x", "d/e/f/x"), FIVE_FOLDERS, cap=5),
    "folders with entries of their own": Row(named("a/", "b/", "c/", "d/e/f/"), FIVE_FOLDERS, cap=5),
    "levels of a file": Row(named("a/" * 100 + "x"), LEVELS),
    "levels of a folder": Row(named("a/" * 101), LEVELS),
    "a part of 256 bytes": Row(named("x" * 256), TOO_LONG),
    "a part of 256 bytes, 128 characters": Row(named("\u00e9" * 128), TOO_LONG),
    "a folder of 256 bytes": Row(named("x" * 256 + "/a.py"), TOO_LONG),
    "a folder's own entry of 256 bytes": Row(named("x" * 256 + "/"), TOO_LONG),
    "a name of 4,220 bytes": Row(named("/".join(["x" * 200] * 21)), TOO_LONG),
    "a stored name that a NUL cuts short": Row(named("b.py\0" + "x" * 4096), TOO_LONG),
    "one size over the archive's": Row((Member("a", compressed=1 << 20),), MORE_DATA),
    "sizes that only together pass the archive's": Row(
        (Member("a", compressed=200), Member("b", compressed=200)), MORE_DATA),
    "the size of a folder's own entry over the archive's": Row((Member("pkg/", b"", compressed=1 << 20),), MORE_DATA),
    "two records of one member": Row((Member("a", b""), Member("b", b"", header_of=1)), TWICE),
    "two records of main.py": Row((Member("b", b"", header_of=0),), TWICE),
    "two records of one member, a folder's own entry": Row((Member("pkg/", b"", header_of=0),), TWICE),
    **{f"no name: {name!r}": Row(named(name), NO_NAME) for name in ("", ".", "./.", "\0x")},
    **{f"absolute: {name!r}": Row(named(name), ABSOLUTE) for name in (
        "/evil.py", "//server/evil.py", "\\evil.py", "C:\\evil.py", "C:/evil.py", "c:evil.py", "/", "/pkg/")},
    **{f"dot dot: {name!r}": Row(named(name), DOT_DOT) for name in (
        "../evil.py", "pkg/../../evil.py", "pkg/..", "..", "..\\evil.py", "pkg\\..\\..\\evil.py",
        "pkg/..\\../evil.py", "../", "pkg/../", "../evil.py\0a.py")},
    "a link": Row((Member("link", b"main.py", mode=LINK),), A_LINK),
    "a link named as a folder": Row((Member("linked/", b"", mode=LINK),), A_LINK),
    "entries": Row(named("a", "b", "c"), "must have at most 3 entries", cap=3),
}

VALID: dict[str, Row] = {
    "main.py alone": Row((), None),
    "deflated": Row((Member("a.csv", b"1,2\n" * 100, method=zipfile.ZIP_DEFLATED),), None),
    "a folder's own entry before its files": Row(named("pkg/", "pkg/a.py"), None),
    "a folder's own entry after its files": Row(named("pkg/a.py", "pkg/"), None),
    "a folder's own entry around its files": Row(named("pkg/", "pkg/a.py", "pkg/"), None),
    "a folder's own entry twice": Row(named("pkg/", "pkg/", "pkg/a.py"), None),
    "a folder's own entry, a level down": Row(named("pkg/sub/a.py", "pkg/sub/", "pkg/"), None),
    "dot and empty parts": Row(named("./", "./b.py", "a//c.py", "a/./d.py"), None),
    "a last part that is a dot": Row(named("a/."), None),
    "a name that a NUL cuts": Row(named("b.py\0junk"), None),
    "names that are not ASCII": Row(named("donn\u00e9es/\u00e9.csv", "\u0c95/\U0001f600.txt"), None),
    "one name in several folders": Row(named("a/x.py", "b/x.py", "a/b/x.py", "x.py"), None),
    "names that only look alike": Row(named("a/b", "ab", "a.b/c", "a/bc/d"), None),
    "names with dots": Row(named("...", "..a/b..", ".hidden/..."), None),
    "data in a folder's own entry": Row((Member("pkg/", b"not a file"),), None),
    "flags of a folder's own entry": Row((Member("pkg/", b"", flags=0x60),), None),
    "data descriptors": Row((Member("a", flags=0x8),), None),
    "zip64 sizes": Row((Member("a", b"12345", compressed=0xFFFFFFFF, unpacked=0xFFFFFFFF,
                               extra=field(1, struct.pack("<QQ", 5, 5))),), None),
    "16 extra fields": Row((Member("a", extra=field(0xFFFF) * 16), Member("b", extra=field(0xFFFF) * 16)), None),
    "too few bytes for an extra field": Row((Member("a", extra=b"123"),), None),
    "a file's mode": Row((Member("a", mode=stat.S_IFREG | 0o4777),), None),
    "folders at the cap": Row(named("a/b/c/d/e/x"), None, cap=5),
    "folders at the cap, with entries of their own": Row(named("a/", "b/", "c/", "d/e/"), None, cap=5),
    "folders at the cap, shared": Row(named("a/b/x", "a/b/y", "a/b/c/d/e/"), None, cap=5),
    "entries at the cap": Row(named("a", "b"), None, cap=3),
}

if not WINDOWS:  # Windows drops a dot or a space at the end of a name
    VALID["names that end in a dot"] = Row(named("a.", "a..", "a /b."), None)
else:
    del VALID["names with dots"]


def rows(table: dict[str, Any]) -> list[Any]:
    return [pytest.param(row, id=name) for name, row in table.items()]


@pytest.mark.parametrize("row", rows(RULES))
def test_both_engines_refuse_it_with_one_text(tmp_path: Path, row: Row):
    """Catches a rule of extract_zip() that validate() does not have, or has otherwise: such an archive waited in the
    queue and took an allocation before it failed (D-153)."""
    assert both(tmp_path, archive(MAIN, *row.members), row.cap) == (row.text, row.text)


@pytest.mark.parametrize("row", rows(VALID))
def test_both_engines_accept_it(tmp_path: Path, row: Row):
    """Catches a rule drawn too tight: what E1 unpacks, E2 takes (its own rules excepted, OWN)."""
    assert both(tmp_path, archive(MAIN, *row.members), row.cap) == (None, None)


def test_an_archive_with_something_before_it_is_accepted_by_both(tmp_path: Path):
    """A self-extracting archive: its offsets count from where the zip data starts."""
    assert both(tmp_path, archive(MAIN, *named("pkg/a.py"), prefix=b"#!/bin/sh\n" * 100)) == (None, None)


def test_a_backslash_is_read_alike(tmp_path: Path):
    """On Linux it is part of a name, on Windows zipfile makes it a slash: the two engines must agree on either.
    Catches names split on both separators, which only the '..' rule does, and a file taken for a folder."""
    two = archive(MAIN, *named("pkg\\a.py", "pkg/a.py"))
    assert both(tmp_path, two) == ((TWO_FILES, TWO_FILES) if WINDOWS else (None, None))
    long = archive(MAIN, *named("x" * 200 + "\\" + "y" * 100))
    assert both(tmp_path, long) == ((None, None) if WINDOWS else (TOO_LONG, TOO_LONG))
    last = archive(MAIN, *named("pkg\\", "pkg\\"))
    assert both(tmp_path, last) == ((None, None) if WINDOWS else (TWO_FILES, TWO_FILES))


@pytest.mark.parametrize("data", [
    b"PK\x03\x04 not a zip", archive(MAIN)[:-10], archive(MAIN, replace(MAIN, name="a", version=64)),
    archive(MAIN).replace(b"PK\x01\x02", b"PK\x01\x00"), archive(MAIN, Member("a", extra=struct.pack("<HH", 9, 100))),
    *(with_tail(b"PK\x01\x02" + bytes(size - 4)) for size in (10, 20, 33, 45)),
], ids=["no archive", "cut", "a version above 6.3", "a bad record", "an extra field past its record",
        "10 bytes of a record", "20 bytes of a record", "33 bytes of a record", "45 bytes of a record"])
def test_what_zipfile_cannot_read_is_refused_by_both(tmp_path: Path, data: bytes):
    assert both(tmp_path, data) == ("must be a zip archive", UNREADABLE)


# ---- at the limits --------------------------------------------------------------------------------------

def test_the_limits_are_e1s():
    """Catches a limit that only one engine knows: E2 may not import E1, so both write the numbers down."""
    assert (validation.ZIP_MAX_DEPTH, validation.ZIP_MAX_NAME_BYTES, validation.ZIP_MAX_PART_BYTES) == (
        files.ZIP_MAX_DEPTH, files.ZIP_MAX_NAME_BYTES, files.ZIP_MAX_PART_BYTES)


@pytest.mark.parametrize(("name", "text"), [
    ("a/b/x", None), ("a/b/c/", None), ("a/b/c/x", "must have names of at most 3 levels"),
    ("a/b/c/d/", "must have names of at most 3 levels"), ("./a//b/./x", None),
    ("abcdefg", None), ("abcdefgh", TOO_LONG), ("\u00e9\u00e9\u00e9a", None), ("\u00e9\u00e9\u00e9\u00e9", TOO_LONG),
    ("abcdefg/x", None), ("abcdefgh/x", TOO_LONG), ("abcdefgh/", TOO_LONG),
    ("abcdef/abcdefg", None), ("abcdefg/abcdefg", TOO_LONG), ("abcdefg/abcdef/", TOO_LONG),
    ("b.py\0" + "x" * 9, None), ("b.py\0" + "x" * 10, TOO_LONG)])
def test_both_engines_draw_the_limits_at_one_place(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str,
                                                   text: str | None):
    """Small limits, since a path of Windows ends at 260 characters: 3 levels, 7 bytes a part, 14 bytes a name.
    Catches each off by one and characters counted for bytes."""
    for module in (validation, files):
        monkeypatch.setattr(module, "ZIP_MAX_DEPTH", 3)
        monkeypatch.setattr(module, "ZIP_MAX_PART_BYTES", 7)
        monkeypatch.setattr(module, "ZIP_MAX_NAME_BYTES", 14)
    assert both(tmp_path, archive(MAIN, *named(name))) == (text, text)


@pytest.mark.parametrize(("name", "text"), [
    ("a/" * 99 + "x", None), ("a/" * 100, None), ("a/" * 100 + "x", LEVELS), ("a/" * 101, LEVELS),
    ("x" * 255, None), ("x" * 256, TOO_LONG), ("\u00e9" * 127 + "x", None), ("\u00e9" * 128, TOO_LONG),
    ("/".join(["x" * 255] * 15 + ["y" * 128, "z" * 127]), None),
    ("/".join(["x" * 255] * 15 + ["y" * 128, "z" * 128]), TOO_LONG),
    ("b.py\0" + "x" * 4091, None), ("b.py\0" + "x" * 4092, TOO_LONG),
    ("b.py\0x" + "\u00e9" * 2045, None), ("b.py\0" + "\u00e9" * 2046, TOO_LONG),
], ids=["100 levels", "100 levels, folder", "101 levels", "101 levels, folder", "255 bytes", "256 bytes",
        "255 bytes, 128 characters", "256 bytes, 128 characters", "4,096 bytes", "4,097 bytes", "4,096 bytes stored",
        "4,097 bytes stored", "4,096 bytes stored, 2,051 characters", "4,097 bytes stored, 2,051 characters"])
def test_the_limits_of_e2_are_100_levels_255_and_4096_bytes(tmp_path: Path, name: str, text: str | None):
    """The numbers of D-151 at both edges; E1 alone could not make all of these on every disk."""
    assert e2_says(tmp_path, archive(MAIN, *named(name))) == text


@pytest.mark.parametrize(("size", "text"), [(0, None), (1, MORE_DATA)])
def test_the_sizes_may_add_up_to_the_archive_and_no_further(tmp_path: Path, size: int, text: str | None):
    """The edge of D-151: not more than the archive holds. E1 judges the sum alike, and then cannot read the member."""
    length = len(archive(MAIN, Member("a")))
    data = archive(MAIN, Member("a", compressed=length - len(MAIN.data) + size))
    assert len(data) == length
    assert both(tmp_path, data) == (text, text or UNREADABLE)


# ---- what only one of the two refuses ---------------------------------------------------------------------

OWN: dict[str, tuple[bytes, str, int]] = {
    "17 extra fields": (archive(MAIN, Member("a", extra=field(0xFFFF) * 17)), EXTRA_FIELDS, CAP),
    "the stored name has a dot dot": (archive(MAIN, *named("b.py\0/../../evil")), DOT_DOT, CAP),
    "no main.py": (archive(Member("./main.py")), "must have main.py at its top level", CAP),
    "declared sizes": (archive(MAIN, Member("a", b"12345", unpacked=UNPACKED)),
                       f"must unpack to at most {UNPACKED} bytes", CAP),
    "directory size": (archive(MAIN, Member("a", comment=b"x" * 4000)),
                       "has a zip directory larger than 3072 bytes", 3),
}
if UNICODE_PATHS:
    OWN["the stored name is absolute"] = (
        archive(MAIN, Member("/b.py", extra=unicode_path("/b.py", "b.py"))), ABSOLUTE, CAP)

INNER = Member("b")
ONLY_E1: dict[str, bytes] = {
    "a wrong CRC": archive(MAIN, Member("a", crc=1)),
    "data cut short": archive(MAIN, Member("a", b"x" * 100, compressed=50)),
    "data that is not deflated": archive(MAIN, Member("a", method=zipfile.ZIP_DEFLATED, raw=b"\xff\xff\xff\xff")),
    "another name in the local header": archive(MAIN, Member("a", local_name="b")),
    "an offset where no header is": archive(MAIN, Member("a", moved=1)),
    "an offset past the end": archive(MAIN, Member("a", moved=10_000)),
    "a member inside another's data": archive(
        MAIN, Member("a", local(INNER)), replace(INNER, header_of=1, moved=len(local(Member("a", b""))))),
}


@pytest.mark.parametrize("row", rows(OWN))
def test_e2s_own_rules(tmp_path: Path, row: tuple[bytes, str, int]):
    """E2 refuses more than E1 (D-79, D-85, D-154). Catches one of its own rules lost in the copy of E1's."""
    data, text, cap = row
    assert both(tmp_path, data, cap) == (text, None)


@pytest.mark.parametrize("data", rows(ONLY_E1))
def test_what_the_directory_cannot_show_is_e1s(tmp_path: Path, data: bytes):
    """The scope of this file: E2 reads no local header and no data."""
    assert both(tmp_path, data) == (None, UNREADABLE)


def test_neither_engine_imports_the_other():
    """Catches the copy replaced by an import: engines call each other through app/common/ports.py only."""
    for package, other in (("e1_execution", "e2_scheduling"), ("e2_scheduling", "e1_execution")):
        sources = list(Path(validation.__file__).parents[1].joinpath(package).rglob("*.py"))
        assert sources
        for source in sources:
            for node in ast.walk(ast.parse(source.read_bytes())):
                names = ([alias.name for alias in node.names] if isinstance(node, ast.Import) else
                         [f"{node.module}.{alias.name}" for alias in node.names] if isinstance(node, ast.ImportFrom)
                         else [])
                assert not [name for name in names if other in name], source


# ---- archives nobody wrote down ---------------------------------------------------------------------------

def random_member(rng: random.Random, index: int) -> Member:
    name = "/".join(rng.choices(["a", "b", "c", "a.py", "main.py", ".", "", "\u00e9", "a\\b"], k=rng.randint(1, 4)))
    name += rng.choice(["", "", "/"])
    member = Member(name, rng.choice([b"", b"12345"]), rng.choice([0, 8]))
    fault = rng.randrange(80)
    if fault == 0:
        return replace(member, name="../" + name)
    if fault == 1:
        return replace(member, name=rng.choice(["/", "c:", "\\"]) + name)
    if fault == 2:
        return replace(member, flags=rng.choice([0x1, 0x20, 0x40, 0x8]))
    if fault == 3:
        return replace(member, method=rng.choice([9, 12, 14, 99]))
    if fault == 4:
        return replace(member, mode=LINK)
    if fault == 5:
        return replace(member, data=b"", method=0, header_of=rng.randrange(index))
    if fault == 6:
        return replace(member, compressed=5000)
    return member


def test_300_random_archives_are_judged_alike(tmp_path: Path):
    """Catches what the table forgot: names, flags, methods and sizes drawn by chance, at caps that are often met.
    The texts may differ when an archive has several faults, since each engine reports the first it meets."""
    rng = random.Random(153)
    outcomes: list[bool] = []
    for _ in range(300):
        members = [MAIN]
        for index in range(1, rng.randint(1, 5) + 1):
            members.append(random_member(rng, index))
        data, cap = archive(*members), rng.choice([6, 7, 8, 100])
        said = both(tmp_path, data, cap)
        assert (said[0] is None) == (said[1] is None), (members, cap, said)
        outcomes.append(said[0] is None)
    assert 75 <= sum(outcomes) <= 225  # both verdicts are met often


# ---- the order of the checks ------------------------------------------------------------------------------

def test_the_first_failing_check_decides(tmp_path: Path):
    """Catches checks run in another order than D-79's with the new ones where D-154 puts them: one record has every
    fault it can have at once, and each fix moves the refusal on to the next. Five folders are there before it, which
    is the cap, and no main.py until the end."""
    deep = "a/" * 101
    bad = Member("/../" + deep + "x" * 4096, mode=LINK, flags=0x61, method=14, compressed=1 << 20,
                 unpacked=UNPACKED + 1, header_of=0, extra=field(0xFFFF) * 17)
    first = named("app.py", "pkg/b/c/d/e/x")
    steps: list[tuple[str, dict[str, Any]]] = [
        (EXTRA_FIELDS, {"extra": b""}), (ABSOLUTE, {"name": "../" + deep + "x" * 4096}),
        (DOT_DOT, {"name": deep + "x" * 4096}), (A_LINK, {"mode": 0}), (ENCRYPTED, {"flags": 0x60}),
        (METHOD, {"method": 0}), (UNREADABLE, {"flags": 0}), (MORE_DATA, {"compressed": 0}),
        (TWICE, {"header_of": None}), (TOO_LONG, {"name": deep + "x" * 256}), (TOO_LONG, {"name": deep}),
        (LEVELS, {"name": "app.py/x"}),
        (FILE_AND_FOLDER, {"name": "pkg/new/x"}), (FIVE_FOLDERS, {"name": "."}), (NO_NAME, {"name": "app.py"}),
        (TWO_FILES, {"name": "pkg"}), (FILE_AND_FOLDER, {"name": "ok.py"}),
        (f"must unpack to at most {UNPACKED} bytes", {"unpacked": None}),
        ("must have main.py at its top level", {"name": "main.py"})]
    for text, fix in steps:
        assert e2_says(tmp_path, archive(*first, bad), cap=5) == text
        bad = replace(bad, **fix)
    assert e2_says(tmp_path, archive(*first, bad), cap=5) is None


def test_an_earlier_record_decides_before_a_later_one(tmp_path: Path):
    """One pass in directory order: catches a second pass that asks D-79's questions of every record first."""
    method, unsafe = Member("a", method=14), Member("../evil.py")
    assert e2_says(tmp_path, archive(MAIN, method, unsafe)) == METHOD
    assert e2_says(tmp_path, archive(MAIN, unsafe, method)) == DOT_DOT
    assert e2_says(tmp_path, archive(method, Member("b", unpacked=UNPACKED + 1))) == METHOD


# ---- the budget of a hostile archive (D-155, D-156): operations and bytes are counted, never seconds ------

@contextmanager
def operations() -> Iterator[Counter[str]]:
    """How often each function, of Python or of C, is called in the block, by its name."""
    calls: Counter[str] = Counter()

    def profile(frame: FrameType, event: str, arg: Any) -> None:
        if event == "call":
            calls[frame.f_code.co_name] += 1
        elif event == "c_call":
            calls[getattr(arg, "__name__", "?")] += 1

    sys.setprofile(profile)
    try:
        yield calls
    finally:
        sys.setprofile(None)


def test_extra_fields_past_the_cap_cost_nothing(tmp_path: Path):
    """Catches zipfile's decoder run on a record of many extra fields. It slices what is left again for each field, so
    a directory of 10 MB took 4.5 s of the server, or 4.8 s with a warning for each empty Unicode Path field: 17 fields
    and 16,000 must cost the same, and no field is decoded."""
    cost: list[int] = []
    for number in (17, 16_000):
        source = spooled(tmp_path, archive(MAIN, Member("a", extra=field(0xFFFF) * number)))
        with operations() as calls:
            said = refusal(source)
        assert said == EXTRA_FIELDS and calls["_decodeExtra"] == 0
        cost.append(sum(calls.values()))
    assert cost[1] <= cost[0]


def test_every_record_is_held_to_the_extra_fields(tmp_path: Path):
    """Catches a walk over the records that loses its place: names, extra fields and comments of other lengths lie
    before the record with 17 fields, and a whole record with 17 lies in a comment, which is no record."""
    odd = Member("pkg/a", extra=field(1) + field(0x5455, b"12345"), comment=b"c" * 31)
    many = Member("b", extra=field(0xFFFF) * 17)
    assert e2_says(tmp_path, archive(MAIN, odd, many)) == EXTRA_FIELDS
    fake = archive(many)[len(local(many)):-22]
    assert fake.startswith(b"PK\x01\x02")
    assert both(tmp_path, archive(MAIN, replace(odd, comment=fake), Member("c"))) == (None, None)


@pytest.mark.parametrize(("number", "text"), [(16, None), (17, EXTRA_FIELDS)])
def test_a_last_record_that_claims_more_extra_than_there_is(tmp_path: Path, number: int, text: str | None):
    """zipfile decodes what there is. Catches a walk past the end of the directory, which is a server error."""
    data = bytearray(archive(MAIN, Member("a", extra=field(0xFFFF) * number)))
    struct.pack_into("<H", data, data.rindex(b"PK\x01\x02") + 30, 0xFFFF)
    assert both(tmp_path, bytes(data)) == (text, None)


def test_the_walk_fails_closed_where_zipfile_reads_another_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Catches the bounds before the parse passing in silence when the bytes they were given are not the directory that
    ZipFile() parses: zipfile's end record reader changed its meaning once already (D-132), and 16,000 extra fields
    were then decoded again. Bytes that do not walk as records to their end, which ZipFile() refuses too, are refused
    (security review of E2-6)."""
    real = validation._END_RECORD

    def moved(fp: Any) -> Any:
        end = real(fp)
        if end is not None:
            end[validation._END_LOCATION] -= 1  # as a reader one byte off would report it
        return end

    monkeypatch.setattr(validation, "_END_RECORD", moved)
    source = spooled(tmp_path, archive(MAIN, Member("a", extra=field(0xFFFF) * 16_000)))
    with operations() as calls:
        said = refusal(source)
    assert said == "must be a zip archive" and calls["_decodeExtra"] == 0


def test_the_directory_is_counted_and_parsed_from_one_open_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Catches the count, the walk and the archive's size taken from one open of the spool file and the parse from a
    second: a file put in its place in between would be parsed without having been counted (security review)."""
    source = spooled(tmp_path, archive(MAIN, *named("pkg/a.py")))
    real, opened = io.open, list[str]()

    def counting(file: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(file, str | Path) and Path(file) == source:
            opened.append(str(file))
        return real(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", counting)
    monkeypatch.setattr(io, "open", counting)
    assert refusal(source) is None
    assert len(opened) == 1


def test_a_name_is_taken_apart_once(tmp_path: Path):
    """Catches a list of parts made for every rule and for each of two equal names: 10 MB of slashes took 2.65 s of the
    server for the '..' rule alone. The name is searched for that rule, and split once to walk its folders."""
    source = spooled(tmp_path, archive(MAIN, *named(*(f"a{'/' * 200}{n}" for n in range(50)))))
    with operations() as calls:
        assert refusal(source) is None
    assert calls["split"] + calls["findall"] <= 51


def test_the_work_grows_with_the_records_and_no_faster(tmp_path: Path):
    """Catches a pass inside the pass that calls a function for each record it goes over: twice as many records, each
    99 folders down with 16 extra fields, cost at most twice as many calls. Work that makes no call, such as `in` on a
    list, is not counted here."""
    cost: list[int] = []
    for number in (100, 200):
        source = spooled(tmp_path, archive(MAIN, *(
            Member("a/" * 99 + str(n), extra=field(0xFFFF) * 16) for n in range(number))))
        with operations() as calls:
            assert refusal(source) is None
        cost.append(sum(calls.values()))
    assert cost[1] <= 2 * cost[0]


def test_deep_folders_are_not_kept_by_their_whole_paths(tmp_path: Path):
    """Catches memory that a directory only claims: ten names of 4 KB, one character of each from outside the basic
    plane, imply 990 folders, whose paths take 14 MB (10,000 of them 141 MiB, from 0.4 MB). Nothing stays afterwards,
    accepted or refused (D-157)."""
    long = "/".join(["x" * 250] * 13 + ["a"] * 85 + ["f"])
    members = named(*(f"\U0001f600{n:03}/{long}" for n in range(10)))
    for last, text in (("ok.py", None), ("../evil.py", DOT_DOT)):
        source = spooled(tmp_path, archive(MAIN, *members, Member(last)))
        tracemalloc.start()
        try:
            said = refusal(source, cap=1000)
            kept, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert said == text
        assert peak < 2 << 20 and kept < 100_000


class Watched(list[zipfile.ZipInfo]):
    """The records of a directory: how often they are gone through, and how many of them are looked at."""

    def __init__(self, records: list[zipfile.ZipInfo]) -> None:
        super().__init__(records)
        self.passes = self.seen = 0

    def __iter__(self) -> Iterator[zipfile.ZipInfo]:
        self.passes += 1
        for record in super().__iter__():
            self.seen += 1
            yield record


@pytest.mark.parametrize(("members", "text", "seen"), [
    (named("a", "b", "c", "d"), None, 5),
    (named("a/b/c/x", "d/e/f/x", "1", "2"), FIVE_FOLDERS, 3),
    ((Member("a", compressed=1 << 20), *named("1", "2", "3")), MORE_DATA, 2),
    ((Member("a", b"", header_of=0), *named("1", "2", "3")), TWICE, 2),
    ((Member("a", method=14), *named("1", "2", "3")), METHOD, 2),
], ids=["accepted", "folders", "data", "one member twice", "method"])
def test_one_pass_that_stops_at_the_first_fault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                members: tuple[Member, ...], text: str | None, seen: int):
    """Catches a second pass, and a count that goes on past its cap."""
    real, watched = zipfile.ZipFile.infolist, list[Watched]()

    def infolist(self: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
        watched.append(Watched(real(self)))
        return watched[-1]

    monkeypatch.setattr(zipfile.ZipFile, "infolist", infolist)
    assert e2_says(tmp_path, archive(MAIN, *members), cap=5) == text
    assert [(records.passes, records.seen) for records in watched] == [(1, seen)]


def test_no_more_records_than_the_cap_are_looked_at(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The backstop of D-133: should a zipfile find more records than were counted, none of them is judged."""
    records = Watched([zipfile.ZipInfo(f"f{n}") for n in range(6)])
    monkeypatch.setattr(zipfile.ZipFile, "infolist", lambda self: records)
    assert e2_says(tmp_path, archive(MAIN), cap=5) == "must have at most 5 entries"
    assert records.seen == 0
