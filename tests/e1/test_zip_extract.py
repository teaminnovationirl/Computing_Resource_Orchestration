"""Slice E1-1: extract_zip(), which unpacks a python_ml payload.zip (PRODUCT_SPEC section 3, D-85, D-151).

The oracle is the owner's plan: the caps hold on the bytes written and on what is created, never on what the
archive declares; an absolute, '..' or link entry is refused on the name zipfile extracts to; every file is made
new; a failure leaves no partial folder and nothing outside it. E2 has already checked the declared sizes.
"""
from __future__ import annotations

import errno
import io
import os
import shutil
import stat
import struct
import sys
import traceback
import tracemalloc
import warnings
import zipfile
import zlib
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from app.e1_execution import files as files_module
from app.e1_execution.files import (DISK_RESERVE_BYTES, ZIP_MAX_DEPTH, ZIP_MAX_NAME_BYTES, ZIP_MAX_PART_BYTES,
                                    BadArchive, UnsafePath, extract_zip, remove_unpacked)
from tests.helpers import link_folder, logged, zip_of

WINDOWS = sys.platform == "win32"
Entries = Sequence[tuple[str, bytes]]


def zipped(entries: Entries, compression: int = zipfile.ZIP_STORED) -> bytes:
    """An archive of entries in this order, names exactly as given, two of one name included."""
    buffer = io.BytesIO()
    with warnings.catch_warnings(), zipfile.ZipFile(buffer, "w", compression=compression) as archive:
        warnings.simplefilter("ignore")  # zipfile warns of a duplicate name
        for name, data in entries:
            info = zipfile.ZipInfo("placeholder")
            info.filename = name
            info.compress_type = compression
            archive.writestr(info, data)
    return buffer.getvalue()


class Place:
    """Where one archive is extracted: target in its own folder, with a file beside it that must not change."""

    def __init__(self, tmp_path: Path) -> None:
        self.archive, self.target = tmp_path / "payload.zip", tmp_path / "run" / "job"
        self.outside = tmp_path / "run" / "outside"
        self.outside.mkdir(parents=True)
        (self.outside / "victim").write_bytes(b"untouched")

    def extract(self, data: bytes, max_entries: int = 100, max_bytes: int = 1000) -> dict[str, bytes | None]:
        """What was extracted, by relative path: a file's bytes, None for a folder."""
        self.archive.write_bytes(data)
        extract_zip(self.archive, self.target, max_entries=max_entries, max_bytes=max_bytes)
        return {p.relative_to(self.target).as_posix(): p.read_bytes() if p.is_file() else None
                for p in self.target.rglob("*")}

    def refuses(self, data: bytes, match: str, **caps: int) -> None:
        """BadArchive, with no target left and nothing made or changed beside it."""
        with pytest.raises(BadArchive, match=match):
            self.extract(data, **caps)
        self.nothing_left()

    def nothing_left(self) -> None:
        assert not os.path.lexists(self.target)
        assert [p.name for p in self.target.parent.rglob("*")] == ["outside", "victim"]
        assert (self.outside / "victim").read_bytes() == b"untouched"


@pytest.fixture
def place(tmp_path: Path) -> Place:
    return Place(tmp_path)


# ---- what is extracted ----------------------------------------------------------------------------------

@pytest.mark.parametrize("compression", [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED])
def test_an_archive_is_extracted_byte_for_byte(place: Place, compression: int):
    entries = [("main.py", b"import pkg.a\n"), ("pkg/a.py", b"\xff\x00" * 50), ("pkg/deep/er/data.bin", b""),
               ("empty/", b"")]
    assert place.extract(zipped(entries, compression)) == {
        "main.py": b"import pkg.a\n", "pkg": None, "pkg/a.py": b"\xff\x00" * 50, "pkg/deep": None,
        "pkg/deep/er": None, "pkg/deep/er/data.bin": b"", "empty": None}


@pytest.mark.parametrize("entries", [
    [("pkg/", b""), ("pkg/a.py", b"1"), ("main.py", b"2")],
    [("pkg/a.py", b"1"), ("pkg/", b""), ("main.py", b"2")],
    [("pkg/", b""), ("pkg/a.py", b"1"), ("main.py", b"2"), ("pkg/", b"")],
    [("pkg/", b""), ("pkg/", b""), ("pkg/a.py", b"1"), ("main.py", b"2")],
], ids=["before", "after", "around", "twice"])
def test_a_folders_own_entry_may_come_anywhere(place: Place, entries: Entries):
    """Catches a folder entry refused because a file inside it already made the folder: the zip format does not
    order its entries (plan review, point 1)."""
    assert place.extract(zipped(entries)) == {"pkg": None, "pkg/a.py": b"1", "main.py": b"2"}


def test_dot_and_empty_parts_are_dropped(place: Place):
    """As zipfile's own extract() does; the folder entry of the archive's root names nothing."""
    entries = [("./", b""), ("./main.py", b"1"), ("a//b.py", b"2"), ("a/./c.py", b"3")]
    assert place.extract(zipped(entries)) == {"main.py": b"1", "a": None, "a/b.py": b"2", "a/c.py": b"3"}


def test_a_backslash_separates_folders_on_windows_only(place: Place):
    found = place.extract(zipped([("a\\b.py", b"1")]))
    assert found == ({"a": None, "a/b.py": b"1"} if WINDOWS else {"a\\b.py": b"1"})


def test_a_name_is_cut_at_a_nul_as_zipfile_cuts_it(place: Place):
    """The name after the NUL is never used: this entry is main.py."""
    assert place.extract(zipped([("main.py\0/../../evil", b"1")])) == {"main.py": b"1"}


def test_the_content_of_a_folder_entry_is_ignored(place: Place):
    assert place.extract(zipped([("pkg/", b"not a file"), ("main.py", b"1")])) == {"pkg": None, "main.py": b"1"}


@pytest.mark.skipif(WINDOWS, reason="chmod sets only the read-only flag on Windows; C-4 runs this on Linux")
@pytest.mark.parametrize("umask", [0o022, 0o077, 0o000])
def test_the_sandbox_user_can_read_and_not_write(place: Place, umask: int):
    """As E2's payload folder (D-128): folders 0755 and files 0644 whatever the umask, never an archived mode."""
    info = zipfile.ZipInfo("pkg/tool.py")
    info.external_attr = (stat.S_IFREG | 0o4777) << 16
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(info, b"1")
    old = os.umask(umask)
    try:
        place.extract(buffer.getvalue())
    finally:
        os.umask(old)
    modes = {p.name: stat.S_IMODE(p.stat().st_mode) for p in [place.target, *place.target.rglob("*")]}
    assert modes == {"job": 0o755, "pkg": 0o755, "tool.py": 0o644}


# ---- names ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["/evil.py", "//server/evil.py", "\\evil.py", "C:\\evil.py", "C:/evil.py", "c:evil.py",
                                  "/", "/pkg/"])
def test_an_absolute_name_is_refused(place: Place, name: str):
    place.refuses(zipped([("main.py", b"1"), (name, b"2")]), "absolute path")


@pytest.mark.parametrize("name", ["../evil.py", "pkg/../../evil.py", "pkg/..", "..", "..\\evil.py",
                                  "pkg\\..\\..\\evil.py", "pkg/..\\../evil.py", "../", "pkg/../"])
def test_a_dot_dot_part_is_refused(place: Place, name: str):
    """Catches '..' dropped silently, as zipfile's extract() does, and a check on one separator alone."""
    place.refuses(zipped([("main.py", b"1"), (name, b"2")]), r"'\.\.' part")


def test_the_name_judged_is_the_one_zipfile_extracts_to(place: Place):
    """Catches a check of the name stored in the directory: an Info-ZIP Unicode Path field (0x7075) replaces
    main.py by ../evil.py, and zipfile opens that entry without a complaint."""
    unicode_name = b"../evil.py"
    info = zipfile.ZipInfo("main.py")
    info.extra = struct.pack("<HHBL", 0x7075, 5 + len(unicode_name), 1, zlib.crc32(b"main.py")) + unicode_name
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(info, b"1")
    with zipfile.ZipFile(io.BytesIO(buffer.getvalue())) as archive:
        read = archive.infolist()[0].filename
    if read == "main.py":  # Python 3.11 does not read the field, and neither does its zipfile extract by it
        assert place.extract(buffer.getvalue()) == {"main.py": b"1"}
    else:
        assert read == "../evil.py"
        place.refuses(buffer.getvalue(), r"'\.\.' part")


def test_a_dot_dot_before_a_nul_is_refused(place: Place):
    place.refuses(zipped([("../evil.py\0main.py", b"1")]), r"'\.\.' part")


@pytest.mark.parametrize("links", [("link",), ("pkg/link.py",), ("linked/",)])
def test_a_link_entry_is_refused(place: Place, links: tuple[str, ...]):
    """Catches a link extracted as a link, or as a file that holds its target's path."""
    place.refuses(zip_of({"main.py": b"1"}, links=links), "link")


@pytest.mark.parametrize("name", ["", ".", "./.", "\0main.py"])
def test_a_file_without_a_name_is_refused(place: Place, name: str):
    """Catches a name read by its last character (zipfile's is_dir() on Python 3.11: IndexError for an empty one)."""
    place.refuses(zipped([("main.py", b"1"), (name, b"2")]), "without a name")


def test_a_local_header_with_another_name_is_refused(place: Place):
    """The directory says main.py and the file's own header says evil.py (SUMMARY gotcha)."""
    raw = bytearray(zipped([("main.py", b"1")]))
    start = raw.index(b"main.py")  # the local header comes first
    raw[start:start + 7] = b"evil.py"
    place.refuses(bytes(raw), "zip archive")


# ---- two entries of one name ----------------------------------------------------------------------------

@pytest.mark.parametrize("entries", [
    [("a.py", b"1"), ("a.py", b"2")],
    [("a.py", b"1"), ("./a.py", b"2")],
    [("pkg/a.py", b"1"), ("pkg//a.py", b"2")],
], ids=["plain", "dot", "double slash"])
def test_a_second_file_of_one_name_is_refused(place: Place, entries: Entries):
    """Catches the last entry winning, and names compared before they are normalised."""
    place.refuses(zipped(entries), "two files")


@pytest.mark.parametrize("entries", [
    [("a", b"1"), ("a/", b"")], [("a/", b""), ("a", b"1")],
    [("a", b"1"), ("a/b.py", b"2")], [("a/b.py", b"2"), ("a", b"1")],
    [("a/b", b"1"), ("a/b/c.py", b"2")], [("a/b/c.py", b"2"), ("a/b", b"1")],
])
def test_a_file_and_a_folder_of_one_name_are_refused_in_either_order(place: Place, entries: Entries):
    place.refuses(zipped(entries), "a file and a folder")


# ---- the caps, on what is written and made --------------------------------------------------------------

def test_the_byte_cap_holds_over_the_whole_archive(place: Place):
    """Catches a cap per file, and one off by one at the limit."""
    entries = [("a", b"x" * 60), ("b", b"y" * 40)]
    assert place.extract(zipped(entries), max_bytes=100) == {"a": b"x" * 60, "b": b"y" * 40}
    shutil.rmtree(place.target)
    place.refuses(zipped([*entries, ("c", b"z")]), "at most 100 bytes", max_bytes=100)
    place.refuses(zipped([("a", b"x" * 101)], zipfile.ZIP_DEFLATED), "at most 100 bytes", max_bytes=100)


class Lying(io.RawIOBase):
    """A zip member that yields far more than its entry declares, and watches the disk while it does."""

    def __init__(self, target: Path, total: int) -> None:
        self._target, self._left, self.most_on_disk = target, total, 0

    def read(self, size: int = -1, /) -> bytes:
        self.most_on_disk = max(self.most_on_disk, sum(p.stat().st_size for p in self._target.rglob("*") if p.is_file()))
        chunk = b"x" * min(40, self._left)
        self._left -= len(chunk)
        return chunk


def test_the_byte_cap_counts_what_is_written_not_what_is_declared(place: Place, monkeypatch: pytest.MonkeyPatch):
    """Catches a cap on the declared sizes (5 bytes here, as a lying archive can say), and one checked only after
    the bytes are on disk. zipfile itself stops at the declared size, so the member is replaced by one that
    does not."""
    data, member = zipped([("a", b"12345")]), Lying(place.target, total=10_000)
    monkeypatch.setattr(zipfile.ZipFile, "open", lambda self, *args, **rest: member)
    monkeypatch.setattr(files_module, "open", lambda path, mode: open(path, mode, buffering=0), raising=False)
    place.refuses(data, "at most 100 bytes", max_bytes=100)
    assert member.most_on_disk == 80  # two chunks were written; the third would have passed the cap


def with_copies(data: bytes, names: Sequence[str]) -> bytes:
    """data with one more directory record per name for its one local header, named by a Unicode Path field."""
    start, end = data.index(b"PK\x01\x02"), data.rindex(b"PK\x05\x06")
    record = data[start:end]
    stored = record[46:46 + struct.unpack_from("<H", record, 28)[0]]
    directory = record
    for name in names:
        field = struct.pack("<HHBL", 0x7075, 5 + len(name.encode()), 1, zlib.crc32(stored)) + name.encode()
        header = bytearray(record[:46])
        struct.pack_into("<H", header, 30, len(field))
        directory += bytes(header) + stored + field
    tail = bytearray(data[end:])
    struct.pack_into("<HHL", tail, 8, 1 + len(names), 1 + len(names), len(directory))
    return data[:start] + directory + bytes(tail)


def test_one_member_is_not_read_again_and_again(place: Place):
    """Catches a bomb of the server's time: many records that name one member (zipfile only warns when their header
    offsets are equal), whose data unpacks to nothing, so no cap on what is written ever stops it. An honest
    archive holds the data of every entry, so their compressed sizes never add up to more than its own."""
    blocks = bytes([0, 0, 0, 255, 255]) * 2000 + bytes([1, 0, 0, 255, 255])  # empty deflate blocks, the last one final
    raw = bytearray(zipped([("main.py", blocks)]))  # stored, and then said to be deflated
    for signature, method, crc, unpacked in ((b"PK\x03\x04", 8, 14, 22), (b"PK\x01\x02", 10, 16, 24)):
        at = raw.index(signature)
        struct.pack_into("<H", raw, at + method, zipfile.ZIP_DEFLATED)
        struct.pack_into("<L", raw, at + crc, 0)
        struct.pack_into("<L", raw, at + unpacked, 0)
    assert place.extract(bytes(raw)) == {"main.py": b""}  # once is an honest, odd archive
    shutil.rmtree(place.target)
    place.refuses(with_copies(bytes(raw), ["a.py", "b.py"]), "more data than it holds")


def test_two_records_of_one_member_are_refused(place: Place):
    """Catches records that share a header whose data is empty, so that no size gives them away: zipfile warns of
    each with a name of up to 64 KB in the text, keeps every such text for good and prints it."""
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        place.refuses(with_copies(zipped([("main.py", b"")]), ["a.py"]), "one member twice")
    assert seen == []


def test_the_entry_cap(place: Place):
    entries = [(f"f{n}", b"1") for n in range(5)]
    assert len(place.extract(zipped(entries), max_entries=5)) == 5
    shutil.rmtree(place.target)
    place.refuses(zipped([*entries, ("f5", b"1")]), "at most 5 entries", max_entries=5)


def test_folders_no_entry_names_count_too(place: Place):
    """Catches a folder bomb: one entry implies a folder per part of its name, which no cap on entries or bytes
    sees (a 120 KB archive with one entry implies 30,000)."""
    assert place.extract(zipped([("a/b/c/d/e/x", b"1")]), max_entries=5).keys() >= {"a/b/c/d/e"}
    shutil.rmtree(place.target)
    place.refuses(zipped([("a/b/c/d/e/f/x", b"1")]), "at most 5 folders", max_entries=5)
    place.refuses(zipped([("a/b/c/x", b"1"), ("d/e/f/x", b"1")]), "at most 5 folders", max_entries=5)


def test_shared_folders_count_once(place: Place):
    """Catches folders counted per entry: an honest archive of many files in few folders."""
    entries = [(f"a/b/f{n}", b"1") for n in range(5)]
    assert len(place.extract(zipped(entries), max_entries=5)) == 7


def test_depth_is_limited(place: Place, monkeypatch: pytest.MonkeyPatch):
    """Catches a name so deep that removing it again recurses past Python's limit."""
    assert ZIP_MAX_DEPTH == 100
    place.refuses(zipped([("a/" * 100 + "x", b"1")]), "at most 100 levels", max_entries=10_000)
    place.refuses(zipped([("a/" * 101, b"")]), "at most 100 levels", max_entries=10_000)
    monkeypatch.setattr(files_module, "ZIP_MAX_DEPTH", 3)
    assert place.extract(zipped([("a/b/x", b"1"), ("c/d/e/", b"")])) == {
        "a": None, "a/b": None, "a/b/x": b"1", "c": None, "c/d": None, "c/d/e": None}


def test_names_have_the_lengths_a_disk_can_hold(place: Place, monkeypatch: pytest.MonkeyPatch):
    """Catches a name that costs memory before any file system refuses it: every folder in it is kept as a name of
    its own, 100 of them in a name of 64 KB. Bytes count, not characters."""
    assert (ZIP_MAX_NAME_BYTES, ZIP_MAX_PART_BYTES) == (4096, 255)
    place.refuses(zipped([("x" * 256, b"1")]), "too long")
    place.refuses(zipped([("\u00e9" * 128, b"1")]), "too long")
    place.refuses(zipped([("x" * 256 + "/", b"")]), "too long")
    place.refuses(zipped([("main.py\0" + "x" * 4096, b"1")]), "too long")  # what zipfile's messages quote
    place.refuses(zipped([("/".join(["x" * 200] * 21), b"1")]), "too long")
    monkeypatch.setattr(files_module, "ZIP_MAX_PART_BYTES", 4)
    monkeypatch.setattr(files_module, "ZIP_MAX_NAME_BYTES", 9)
    assert place.extract(zipped([("abcd/\u00e9\u00e9", b"1")])) == {"abcd": None, "abcd/\u00e9\u00e9": b"1"}
    shutil.rmtree(place.target)
    for name in ("abcd/\u00e9\u00e9\u00e9", "abcde/fg", "a/b/c/d/ef"):
        place.refuses(zipped([(name, b"1")]), "too long")


def test_a_large_member_is_streamed(place: Place):
    """Catches a member held whole in memory: 32 MiB of zeros, 32 KiB in the archive."""
    data = zipped([("big", bytes(32 << 20))], zipfile.ZIP_DEFLATED)
    place.archive.write_bytes(data)
    tracemalloc.start()
    try:
        extract_zip(place.archive, place.target, max_entries=1, max_bytes=32 << 20)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert (place.target / "big").stat().st_size == 32 << 20
    assert peak < 8 << 20


# ---- archives that cannot be unpacked -------------------------------------------------------------------

def patched(data: bytes, offset_local: int, offset_directory: int, value: int) -> bytes:
    """data with one 16 bit field changed in its first local header and its first directory record."""
    raw = bytearray(data)
    struct.pack_into("<H", raw, raw.index(b"PK\x03\x04") + offset_local, value)
    struct.pack_into("<H", raw, raw.index(b"PK\x01\x02") + offset_directory, value)
    return bytes(raw)


@pytest.mark.parametrize("compression", [zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA])
def test_only_stored_and_deflated_entries_are_unpacked(place: Place, compression: int):
    """An LZMA entry declares the dictionary its decoder allocates, up to 4 GiB of the server's memory."""
    place.refuses(zipped([("main.py", b"1" * 100)], compression), "stored or deflated")


def test_an_unknown_compression_method_is_refused(place: Place):
    place.refuses(patched(zipped([("main.py", b"1")]), 8, 10, 99), "stored or deflated")


def test_an_encrypted_entry_is_refused(place: Place):
    place.refuses(patched(zipped([("main.py", b"1")]), 6, 8, 1), "encrypted")


@pytest.mark.parametrize("damage", ["crc", "cut", "not a zip", "empty", "deflate data"])
def test_a_damaged_archive_is_refused(place: Place, damage: str):
    """Catches zipfile's own errors reaching the caller as anything but a refused archive."""
    good = zipped([("a", b"first file"), ("b", b"0123456789" * 20)], zipfile.ZIP_DEFLATED)
    raw = bytearray(good)
    if damage == "crc":
        struct.pack_into("<L", raw, raw.rindex(b"PK\x01\x02") + 16, 1)
    if damage == "deflate data":
        start = raw.rindex(b"PK\x03\x04") + 30 + 1
        raw[start:start + 4] = b"\xff\xff\xff\xff"
    data = {"cut": good[:-30], "not a zip": b"PK but no archive", "empty": b""}.get(damage, bytes(raw))
    place.refuses(data, "zip archive")


@pytest.mark.parametrize("flag", [0x20, 0x40], ids=["patched data", "strong encryption"])
def test_a_flag_zipfile_cannot_read_is_the_archives_error(place: Place, flag: int):
    """zipfile raises NotImplementedError for these two, once the member is opened."""
    place.refuses(patched(zipped([("main.py", b"1")]), 6, 8, flag), "zip archive")


def test_an_offset_past_every_file_is_the_archives_error(place: Place):
    """A zip64 header offset of 2**63: zipfile seeks there, which is a plain ValueError."""
    raw = zipped([("main.py", b"1")])
    start, end = raw.index(b"PK\x01\x02"), raw.rindex(b"PK\x05\x06")
    record = bytearray(raw[start:end])
    struct.pack_into("<L", record, 42, 0xFFFFFFFF)
    struct.pack_into("<H", record, 30, 12)
    record += struct.pack("<HHQ", 0x0001, 8, 1 << 63)
    tail = bytearray(raw[end:])
    struct.pack_into("<L", tail, 12, len(record))
    place.refuses(raw[:start] + bytes(record) + bytes(tail), "zip archive")


@pytest.mark.parametrize("error", [zipfile.BadZipFile("x"), zlib.error("x"), EOFError("x"), NotImplementedError("x"),
                                   ValueError("x")], ids=lambda error: type(error).__name__)
def test_every_error_of_the_reader_is_the_archives(place: Place, monkeypatch: pytest.MonkeyPatch, error: Exception):
    """Which archive raises which of them differs between Python versions; each is a refused archive."""
    data = zipped([("pkg/main.py", b"1")])

    def fail(*args: Any, **rest: Any) -> Any:
        raise error

    monkeypatch.setattr(zipfile.ZipFile, "open", fail)
    place.refuses(data, "zip archive")


@pytest.mark.parametrize("shift", [5, -5])
def test_a_directory_offset_that_lies_is_the_archives_error(place: Place, shift: int):
    """Catches a crafted archive reported as the server's disk error: with an offset 5 too high zipfile seeks to a
    negative position, which is a plain OSError (EINVAL), not BadZipFile. E2 accepts this archive: it never opens
    a member."""
    raw = bytearray(zipped([("main.py", b"1")]))
    end = raw.rindex(b"PK\x05\x06")
    struct.pack_into("<L", raw, end + 16, struct.unpack_from("<L", raw, end + 16)[0] + shift)
    place.refuses(bytes(raw), "zip archive")


# ---- the target folder ----------------------------------------------------------------------------------

@pytest.mark.parametrize("there", ["folder", "file", "link"])
def test_an_existing_target_is_refused_and_left_alone(place: Place, there: str):
    """Catches a target that is reused, emptied or removed: extract_zip() removes only a folder it made."""
    if there == "folder":
        place.target.mkdir()
        (place.target / "keep").write_bytes(b"1")
    elif there == "file":
        place.target.write_bytes(b"1")
    else:
        link_folder(place.target, place.outside)
    with pytest.raises(FileExistsError):
        place.extract(zipped([("main.py", b"1")]))
    assert os.path.lexists(place.target) and (place.outside / "victim").read_bytes() == b"untouched"
    assert sorted(p.name for p in place.outside.iterdir()) == ["victim"]
    if there == "folder":
        assert [p.name for p in place.target.iterdir()] == ["keep"]


def plant(monkeypatch: pytest.MonkeyPatch, plant_it: Callable[[], object]) -> None:
    """Run plant_it once the target folder exists and before anything is unpacked into it."""
    real = zipfile.ZipFile.infolist

    def infolist(self: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
        plant_it()
        return real(self)

    monkeypatch.setattr(zipfile.ZipFile, "infolist", infolist)


def test_a_file_is_never_written_through_one_that_is_already_there(place: Place, monkeypatch: pytest.MonkeyPatch):
    """Catches "wb": a second name (a hard link) of a file outside, planted in the target, must not be opened."""
    plant(monkeypatch, lambda: os.link(place.outside / "victim", place.target / "main.py"))
    place.refuses(zipped([("main.py", b"overwritten")]), "one name")


def test_a_folder_link_out_of_the_target_is_never_followed(place: Place, monkeypatch: pytest.MonkeyPatch):
    """Catches mkdir with exist_ok: a link planted where the archive has a folder leads outside, and neither the link
    nor what it leads to is touched."""
    plant(monkeypatch, lambda: link_folder(place.target / "pkg", place.outside))
    place.refuses(zipped([("main.py", b"1"), ("pkg/evil.py", b"2")]), "one name")


def test_a_folder_link_inside_the_target_is_never_followed(place: Place, monkeypatch: pytest.MonkeyPatch):
    """The same for a link that stays inside the target, which a check of where the path leads would let pass."""
    def plant_it() -> None:
        (place.target / "elsewhere").mkdir()
        link_folder(place.target / "pkg", place.target / "elsewhere")

    plant(monkeypatch, plant_it)
    place.refuses(zipped([("main.py", b"1"), ("pkg/evil.py", b"2")]), "one name")


@pytest.mark.parametrize("where", ["outside", "inside", "above"])
def test_a_folder_swapped_for_a_link_is_never_written_through(place: Place, monkeypatch: pytest.MonkeyPatch, where: str):
    """Catches files written where their folder was when it was made: before anything goes into a folder, it must
    still be the one that was made, wherever on its path the link was put and wherever it leads."""
    data = zipped([("pkg/sub/evil.py", b"1"), ("other/sub/x.py", b"2")])
    real = zipfile.ZipFile.open

    def swap(self: zipfile.ZipFile, entry: Any, *args: Any, **rest: Any) -> Any:
        opened = real(self, entry, *args, **rest)
        if entry.filename == "pkg/sub/evil.py":
            (place.target / "pkg" / "sub").rmdir()
            if where == "above":
                (place.target / "pkg").rmdir()
                link_folder(place.target / "pkg", place.target / "other")
            else:
                link_folder(place.target / "pkg" / "sub",
                            place.outside if where == "outside" else place.target / "other" / "sub")
        return opened

    monkeypatch.setattr(zipfile.ZipFile, "open", swap)
    with pytest.raises(UnsafePath):  # the server's: no archive can do this
        place.extract(data)
    place.nothing_left()


def test_a_folder_swapped_while_folders_are_made_is_never_entered(place: Place, monkeypatch: pytest.MonkeyPatch):
    """The same while the folders are made: a becomes a link before a/sub is made in it."""
    real = Path.mkdir

    def mkdir(self: Path, *args: Any, **rest: Any) -> None:
        real(self, *args, **rest)
        if self.name == "a-":  # made after a and before a/sub, since names are sorted
            (place.target / "a").rmdir()
            link_folder(place.target / "a", place.outside)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    with pytest.raises(UnsafePath):
        place.extract(zipped([("a/sub/", b""), ("a-/", b"")]))
    place.nothing_left()


def test_the_cost_of_a_file_does_not_grow_with_its_depth(place: Place, monkeypatch: pytest.MonkeyPatch):
    """Catches a path that is resolved again for every file, part by part: 9,900 folders below 98 levels (a 5 MB
    archive) took 32 s of the server on Linux, and 10,000 honest files 1 s."""
    real, calls = os.path.realpath, 0

    def realpath(*args: Any, **rest: Any) -> Any:
        nonlocal calls
        calls += 1
        return real(*args, **rest)

    monkeypatch.setattr(os.path, "realpath", realpath)
    assert len(place.extract(zipped([(f"a/b/c/{n}/x", b"1") for n in range(20)]))) == 43
    assert calls <= 5


# ---- failures that are the server's ---------------------------------------------------------------------

def failing_open(monkeypatch: pytest.MonkeyPatch, error: BaseException) -> None:
    def fail(*args: Any, **rest: Any) -> Any:
        raise error

    monkeypatch.setattr(files_module, "open", fail, raising=False)


class Failing:
    """A new file that takes one chunk and fails on the next, or fails when it is closed."""

    def __init__(self, path: Path, mode: str, error: OSError, when: str) -> None:
        self._file, self._error, self._when, self._chunks = open(path, mode), error, when, 0

    def write(self, chunk: bytes) -> int:
        self._chunks += 1
        if self._when == "write" and self._chunks == 2:
            raise self._error
        return self._file.write(chunk)

    def __enter__(self) -> Failing:
        return self

    def __exit__(self, *args: object) -> None:
        self._file.close()
        if self._when == "close" and args[0] is None:
            raise self._error


@pytest.mark.parametrize("when", ["mkdir", "open", "write", "close"])
@pytest.mark.parametrize("number", [errno.ENOSPC, errno.EIO, errno.EACCES, errno.EINVAL])
def test_a_disk_that_fails_is_the_servers_error(place: Place, monkeypatch: pytest.MonkeyPatch, when: str, number: int):
    """Catches a disk error reported as the student's bad archive, at every step of writing one, and a file that was
    only partly written taken for the whole one. EINVAL too: only where zipfile reads is it the archive's."""
    error = OSError(number, os.strerror(number))
    data = zipped([("pkg/main.py", bytes(3 << 20))])
    if when == "open":
        failing_open(monkeypatch, error)
    elif when == "mkdir":
        real = Path.mkdir

        def mkdir(self: Path, *args: Any, **rest: Any) -> None:
            if self.name == "pkg":
                raise error
            real(self, *args, **rest)

        monkeypatch.setattr(Path, "mkdir", mkdir)
    else:
        monkeypatch.setattr(files_module, "open", lambda path, mode: Failing(path, mode, error, when), raising=False)
    with pytest.raises(OSError) as caught:
        place.extract(data, max_bytes=4 << 20)
    assert caught.value.errno == number and not isinstance(caught.value, ValueError)
    place.nothing_left()


def test_an_archive_that_cannot_be_read_is_the_servers_error(place: Place, monkeypatch: pytest.MonkeyPatch):
    """The other half of the same line: a disk that fails while zipfile reads is not the archive's fault."""
    data = zipped([("main.py", b"1")])

    def fail(*args: Any, **rest: Any) -> Any:
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(zipfile.ZipFile, "open", fail)
    with pytest.raises(OSError) as caught:
        place.extract(data)
    assert caught.value.errno == errno.EIO and not isinstance(caught.value, ValueError)
    place.nothing_left()


def test_a_name_the_disk_cannot_hold_is_the_archives_error(place: Place, monkeypatch: pytest.MonkeyPatch):
    failing_open(monkeypatch, OSError(errno.ENAMETOOLONG, "File name too long"))
    place.refuses(zipped([("pkg/main.py", b"1")]), "too long")


def test_an_interrupt_leaves_nothing(place: Place, monkeypatch: pytest.MonkeyPatch):
    """Catches a cleanup for Exception alone."""
    failing_open(monkeypatch, KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        place.extract(zipped([("pkg/main.py", b"1")]))
    place.nothing_left()


@pytest.mark.parametrize("free, fits", [(100, True), (99, False)])
def test_a_disk_without_room_is_the_servers_error(place: Place, monkeypatch: pytest.MonkeyPatch, free: int, fits: bool):
    """Catches a disk filled to its last byte by one archive after another, when PostgreSQL and every other job fail:
    nothing is written unless the reserve stays free."""
    assert DISK_RESERVE_BYTES == 1 << 30
    usage = shutil.disk_usage(place.target.parent)
    monkeypatch.setattr(files_module.shutil, "disk_usage", lambda path: usage._replace(free=DISK_RESERVE_BYTES + free))
    data = zipped([("a", b"x" * 60), ("b", b"y" * 40)])
    if fits:
        assert len(place.extract(data)) == 2
        return
    with pytest.raises(OSError) as caught:
        place.extract(data)
    assert caught.value.errno == errno.ENOSPC and not isinstance(caught.value, ValueError)
    place.nothing_left()


def test_room_is_asked_for_up_to_the_cap_only(place: Place, monkeypatch: pytest.MonkeyPatch):
    """Catches a size that only the archive claims turned into a full disk: what is written stops at the cap."""
    raw = bytearray(zipped([("a", b"12345")]))
    struct.pack_into("<L", raw, raw.index(b"PK\x01\x02") + 24, 0xFFFFFFF0)
    usage = shutil.disk_usage(place.target.parent)
    monkeypatch.setattr(files_module.shutil, "disk_usage", lambda path: usage._replace(free=DISK_RESERVE_BYTES + 1000))
    assert place.extract(bytes(raw), max_bytes=1000) == {"a": b"12345"}


def test_an_unpacked_archive_can_be_removed_again(place: Place, json_log: Path, monkeypatch: pytest.MonkeyPatch):
    """Up to 4 GiB lie there when the attempt is over. Catches a removal that raises: a file that cannot be deleted is
    logged, and never keeps an attempt from ending."""
    place.extract(zipped([("main.py", b"1"), ("pkg/a.py", b"2")]))
    remove_unpacked(place.target)
    place.nothing_left()
    remove_unpacked(place.target)  # nothing there
    assert logged(json_log, "archive_cleanup_failed") == []
    place.extract(zipped([("main.py", b"1"), ("pkg/a.py", b"2")]))
    real = os.unlink

    def unlink(path: Any, *args: Any, **rest: Any) -> None:
        if Path(path).name == "main.py":
            raise PermissionError(13, "in use", str(path))
        real(path, *args, **rest)

    monkeypatch.setattr(os, "unlink", unlink)
    remove_unpacked(place.target)
    line, = logged(json_log, "archive_cleanup_failed")
    assert "in use" in line["error"] and [p.name for p in place.target.iterdir()] == ["main.py"]


@pytest.mark.parametrize("how", ["reader", "one name", "too long"])
def test_a_refusal_repeats_nothing_of_the_students(place: Place, monkeypatch: pytest.MonkeyPatch, how: str):
    """Catches student text in a traceback: zipfile quotes names of up to 64 KB, an OSError the path, and a log line
    with the chain of errors would hold them all."""
    word = "".join(["sec", "ret"])
    data = bytearray(zipped([(f"{word}.py", b"1")]))
    if how == "reader":
        start = data.index(word.encode())  # the local header: zipfile says that the names differ, and quotes them
        data[start:start + 3] = b"SEC"
    elif how == "one name":
        plant(monkeypatch, lambda: (place.target / f"{word}.py").write_bytes(b"there"))
    else:
        failing_open(monkeypatch, OSError(errno.ENAMETOOLONG, "File name too long", f"{word}.py"))
    with pytest.raises(BadArchive) as caught:
        place.extract(bytes(data))
    assert word not in "".join(traceback.format_exception(caught.value)).lower()


def test_a_missing_archive_leaves_nothing(place: Place):
    with pytest.raises(FileNotFoundError):
        extract_zip(place.archive, place.target, max_entries=1, max_bytes=1)
    place.nothing_left()


def test_a_cleanup_that_fails_is_logged_and_hides_nothing(place: Place, monkeypatch: pytest.MonkeyPatch,
                                                          json_log: Path):
    """Catches the cleanup's own error replacing the first one, a folder left behind with no trace, and a cleanup that
    stops at the first file it cannot delete and leaves gigabytes beside it."""
    stuck = "b.py"
    real = os.unlink

    def unlink(path: Any, *args: Any, **rest: Any) -> None:
        if Path(path).name == stuck:
            raise PermissionError(13, "in use", str(path))
        real(path, *args, **rest)

    monkeypatch.setattr(os, "unlink", unlink)
    with pytest.raises(BadArchive, match="at most 100 bytes"):
        place.extract(zipped([("a.py", b"1"), (stuck, b"2"), ("c.py", b"3"), ("d.py", b"x" * 200)]), max_bytes=100)
    assert [p.name for p in place.target.iterdir()] == [stuck]
    line, = logged(json_log, "archive_cleanup_failed")
    assert line["level"] == "ERROR" and line["path"] == str(place.target)
    assert "in use" in line["error"] and stuck in line["error"] and "at most 100 bytes" in line["traceback"]
