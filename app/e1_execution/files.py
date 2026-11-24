"""The files E1 uses under DATA_DIR (plan 9.1, D-143, D-144): the payload a job brings, the test cases of its
problem, the folder of one attempt, and the unpacking of a python_ml archive (D-151).

runs/<job_id>/a<n>/ belongs to the server alone (0700) and is never mounted: spec.json, compile.txt, NN.txt,
stdout.txt, stderr.txt and result.json lie in it. Only its out/ (0777) is mounted, at /out, where the program may
do as it likes; the server never opens anything inside out/ while the program could still run.
"""
from __future__ import annotations

import errno
import os
import re
import shutil
import stat
import uuid
import zipfile
import zlib
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePath
from typing import Final

from app.common.logging import log

# The stored names of D-76 and D-96. E2 writes them; engines do not import each other, so a test compares them
# with E2's tables.
SOURCES: Final = ("prog.c", "prog.cpp", "Main.java", "prog.py", "payload.zip")
INPUT_FILE: Final = "input.txt"
_PAYLOAD_REF: Final = re.compile(r"payloads/[0-9a-f]{32}")  # jobs.payload_path (D-80)
RUN_FOLDER_MODE: Final = 0o700
OUT_MODE: Final = 0o777  # user 65534 of the sandbox writes there (plan 9.1); RUN_FOLDER_MODE keeps others out
# An unpacked archive, as E2's payload folder: read by the sandbox user through a read-only mount, never written.
JOB_FOLDER_MODE: Final = 0o755
JOB_FILE_MODE: Final = 0o644
ZIP_MAX_DEPTH = 100  # levels of a name (proposal, D-151): removing a deeper tree recurses past Python's limit
ZIP_MAX_NAME_BYTES = 4096  # PATH_MAX and NAME_MAX of Linux: a longer name could not be stored, and every folder in
ZIP_MAX_PART_BYTES = 255   # it would be kept in memory as a name of its own before the disk said so
DISK_RESERVE_BYTES: Final = 1 << 30  # stays free when an archive is unpacked (proposal, D-151)
_CHUNK: Final = 1 << 20
_ENCRYPTED: Final = 0x1  # the general purpose flag bit of an encrypted entry
_DRIVE: Final = re.compile(r"[A-Za-z]:")
_SEPARATORS: Final = re.compile(r"[\\/]")


class UnsafePath(ValueError):
    """A path that does not resolve inside DATA_DIR, or that a link leads to where none may."""


class BadPayload(ValueError):
    """Not a payload folder as E2 writes them."""


class BadArchive(ValueError):
    """An archive that is not unpacked: the student's error. Its text says why and repeats nothing of theirs."""


@dataclass(frozen=True)
class Payload:
    folder: Path         # mounted read-only at /job (a zip is extracted to RunFolder.job instead)
    source: str          # one of SOURCES
    stdin: Path | None   # input.txt: only a practice run has one (D-96)

    @property
    def path(self) -> Path:
        return self.folder / self.source


@dataclass(frozen=True)
class CaseFiles:
    """One test case. Both files stay on the host: input goes in as stdin, expected is compared there."""
    case_no: int
    input: Path
    expected: Path


@dataclass(frozen=True)
class RunFolder:
    """The folder of one attempt; the worker builds it again from its path."""
    folder: Path

    @property
    def out(self) -> Path:
        """Mounted read-write at /out; the program's own."""
        return self.folder / "out"

    @property
    def job(self) -> Path:
        """Where extract_zip() unpacks a payload.zip; mounted read-only at /job."""
        return self.folder / "job"

    @property
    def spec(self) -> Path:
        """What the worker is told (E1-2)."""
        return self.folder / "spec.json"

    @property
    def compile_log(self) -> Path:
        return self.folder / "compile.txt"

    @property
    def main_check(self) -> Path:
        """What the JVM launcher said to the check of class Main (D-146)."""
        return self.folder / "main.txt"

    @property
    def stdout(self) -> Path:
        """Of a run that has no test cases; stderr beside it."""
        return self.folder / "stdout.txt"

    @property
    def stderr(self) -> Path:
        return self.folder / "stderr.txt"

    @property
    def result(self) -> Path:
        """What a practice run shows (D-150)."""
        return self.folder / "result.json"

    def case_output(self, case_no: int) -> Path:
        return self.folder / f"{case_no:02d}.txt"


class Files:
    """Every path under one DATA_DIR. Paths from the database or a request go through safe_path()."""

    def __init__(self, data_dir: Path) -> None:
        self._root = data_dir.resolve()

    def safe_path(self, rel: str) -> Path:
        """DATA_DIR / rel with every link and '..' resolved. UnsafePath unless that is inside DATA_DIR, or when
        rel is not relative; it need not exist. Below runs/, where programs leave what they like, no link is
        followed at all. A disk that cannot say is its OSError."""
        try:
            if "\0" in rel or PurePath(rel).anchor:
                raise ValueError("not a relative path")
            path = (self._root / rel).resolve()
            # A link loop: before Python 3.13 resolve() raises RuntimeError; since then it goes once around it on
            # Windows, so that resolving again leads elsewhere, and stops in it on Linux, where stat() says ELOOP.
            if path.resolve() != path:
                raise ValueError("a link loop")
            with suppress(FileNotFoundError, NotADirectoryError):
                path.stat()
        except (RuntimeError, ValueError) as exc:
            raise UnsafePath(f"{rel!r} is not a path inside DATA_DIR") from exc
        except OSError as exc:
            if exc.errno not in (errno.ELOOP, errno.ENAMETOOLONG, errno.EINVAL):  # EINVAL: what Windows says of both
                raise
            raise UnsafePath(f"{rel!r} is not a path inside DATA_DIR") from exc
        if path == self._root or not path.is_relative_to(self._root):
            raise UnsafePath(f"{rel!r} does not resolve inside DATA_DIR")
        written = PurePath(os.path.normpath(rel))
        if written.parts[0] == "runs" and path != self._root / written:
            raise UnsafePath(f"{rel!r} is reached through a link")
        return path

    def payload(self, file_ref: str, *, with_input: bool = False) -> Payload:
        """The payload that jobs.payload_path names: exactly payloads/<hex>, a folder no link leads to, with
        exactly one of SOURCES in it, a plain file. with_input is for code_run, whose payload always holds
        input.txt too (D-129); any other has no stdin. BadPayload or UnsafePath otherwise."""
        if _PAYLOAD_REF.fullmatch(file_ref) is None:
            raise BadPayload(f"{file_ref!r} is not a payloads/<hex> reference")
        folder = self._plain(file_ref)
        found = [name for name in SOURCES if _lstat(folder / name) is not None]
        if len(found) != 1 or not _is_plain_file(folder / found[0]):
            raise BadPayload(f"{file_ref} must hold exactly one plain file of {', '.join(SOURCES)}; found {found}")
        if not with_input:
            return Payload(folder, found[0], None)
        if not _is_plain_file(folder / INPUT_FILE):
            raise BadPayload(f"{file_ref} must hold {INPUT_FILE}, a plain file")
        return Payload(folder, found[0], folder / INPUT_FILE)

    def case(self, case_no: int, input_path: str, expected_path: str) -> CaseFiles:
        """One test_cases row with both of its paths resolved. UnsafePath, or FileNotFoundError when one is not
        a file."""
        paths = [self.safe_path(rel) for rel in (input_path, expected_path)]
        for path in paths:
            if not path.is_file():
                raise FileNotFoundError(f"case {case_no}: {path} is not a file")
        return CaseFiles(case_no, *paths)

    def new_run(self, job_id: int, attempt_no: int) -> RunFolder:
        """runs/<job_id>/a<attempt_no>, made new, with an empty out/. Whatever is already there (a database reset
        starts the job ids again and keeps DATA_DIR) is renamed to a<n>.stale-<hex> and never read: renamed, not
        deleted, because the server may not be able to delete what user 65534 left in it."""
        for value in (job_id, attempt_no):
            if type(value) is not int or value < 1:
                raise ValueError(f"job_id and attempt_no must be integers from 1, got {value!r}")
        parent = self._plain(f"runs/{job_id}")
        parent.mkdir(parents=True, exist_ok=True)
        parent.chmod(RUN_FOLDER_MODE)  # who may write to it can put an a<n> of their own there; not ours: EPERM
        folder = parent / f"a{attempt_no}"
        stale = _lstat(folder)
        if stale is not None:  # a link included: it is renamed itself, never followed
            aside = parent / f"a{attempt_no}.stale-{uuid.uuid4().hex}"
            os.rename(folder, aside)
            # How old it is tells a database reset from an attempt that was dispatched twice.
            log.warning("run_folder_stale", engine="E1", job_id=job_id, attempt_no=attempt_no, path=str(aside),
                        kind=_kind(stale), modified=datetime.fromtimestamp(stale.st_mtime, UTC).isoformat())
        folder.mkdir(mode=RUN_FOLDER_MODE)
        run = RunFolder(folder)
        run.out.mkdir()
        run.out.chmod(OUT_MODE)  # the umask took bits from mkdir's mode
        return run

    def _plain(self, rel: str) -> Path:
        """safe_path(rel), which no link may lead to, not even one that stays inside DATA_DIR."""
        path = self.safe_path(rel)
        if path != self._root / rel:
            raise UnsafePath(f"{rel!r} is reached through a link")
        return path


def _lstat(path: Path) -> os.stat_result | None:
    """What is at path, a link not followed; None when nothing is. Any other error is the disk's, where
    os.path.lexists would say that nothing is there."""
    try:
        return os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None


def _is_plain_file(path: Path) -> bool:
    found = _lstat(path)
    return found is not None and stat.S_ISREG(found.st_mode)


def _kind(found: os.stat_result) -> str:
    if stat.S_ISLNK(found.st_mode) or getattr(found, "st_reparse_tag", 0):  # the tag: a junction of Windows
        return "link"
    return "folder" if stat.S_ISDIR(found.st_mode) else "file"


def extract_zip(archive: Path, target: Path, *, max_entries: int, max_bytes: int) -> None:
    """Unpack a python_ml payload.zip into the new folder target (D-151). BadArchive when it is refused; any other
    error is the disk's or a bug. Whatever fails, target is removed again; a target that is already there is
    FileExistsError and is left alone.

    Names are judged as zipfile would extract them (ZipInfo.filename: a Unicode Path field read, cut at a NUL).
    max_bytes holds on the bytes written, over the whole archive; max_entries holds on the entries and on the
    folders, those no entry names included. Only stored and deflated entries are read: an LZMA entry declares
    the memory its decoder takes. The order of the entries does not matter. The caller removes target with
    remove_unpacked() once the sandbox that mounts it is gone."""
    target.mkdir()
    try:
        target.chmod(JOB_FOLDER_MODE)
        _unpack(archive, target.resolve(), max_entries, max_bytes)
    except BaseException:
        remove_unpacked(target)
        raise


def remove_unpacked(folder: Path) -> None:
    """Remove what extract_zip() made, or nothing when it is gone. Never raises for a file that cannot be deleted: it
    goes on past it and logs the first one left, with any error being handled in the line's traceback."""
    shutil.rmtree(folder, ignore_errors=True)
    try:
        with suppress(FileNotFoundError):  # all is gone
            shutil.rmtree(folder)
    except OSError:
        log.exception("archive_cleanup_failed", engine="E1", path=str(folder))


def _unpack(archive: Path, target: Path, max_entries: int, max_bytes: int) -> None:
    with _reading():
        opened = zipfile.ZipFile(archive)
    with opened:
        folders, members = _plan(opened.infolist(), max_entries, archive.stat().st_size)
        # zipfile never yields more of a member than it declares, so this is all that can be written.
        needed = min(sum(entry.file_size for entry in members.values()), max_bytes) + DISK_RESERVE_BYTES
        if shutil.disk_usage(target).free < needed:
            raise OSError(errno.ENOSPC, "no room to unpack an archive and keep the reserve", str(target))
        made = {"": _identity(target)}
        try:
            for name in folders:  # sorted, so a folder comes before what is in it
                path = _child(target, name, made)
                path.mkdir()  # new: a link that is there is never followed
                path.chmod(JOB_FOLDER_MODE)
                made[name] = _identity(path)
            left = max_bytes
            for name, entry in members.items():
                left = _write_member(opened, entry, target, name, made, left, max_bytes)
        except FileExistsError:  # two names that Windows holds for one, or a link that was already there
            raise BadArchive("must not contain two entries of one name") from None
        except OSError as exc:
            if exc.errno != errno.ENAMETOOLONG:
                raise
            raise BadArchive("must not contain a name too long to store") from None


@contextmanager
def _reading() -> Iterator[None]:
    """zipfile reading an archive that may be crafted: what it raises for one is BadArchive, without the cause, since
    zipfile quotes the names it read, up to 64 KB of the student's text. A disk that fails stays its OSError, except
    EINVAL, which here is a seek to an offset that the archive made up."""
    try:
        yield
    except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, ValueError):
        # ValueError: a bad offset or name encoding; NotImplementedError: a zip version or flag zipfile cannot read
        raise BadArchive("must be a zip archive that can be unpacked") from None
    except OSError as exc:
        if exc.errno != errno.EINVAL:
            raise
        raise BadArchive("must be a zip archive that can be unpacked") from None


def _plan(entries: Sequence[zipfile.ZipInfo], max_entries: int,
          archive_bytes: int) -> tuple[list[str], dict[str, zipfile.ZipInfo]]:
    """The folders to make, parents first, and the files to write, by name; BadArchive for an archive that the
    central directory alone already shows to be unacceptable."""
    if len(entries) > max_entries:
        raise BadArchive(f"must have at most {max_entries} entries")
    # Many records may name one member, or overlapping ones, which would be read again for each: a member that
    # unpacks to nothing never meets the cap on what is written. zipfile only warns when header offsets are equal,
    # and keeps and prints each warning, a name of up to 64 KB in it.
    if sum(entry.compress_size for entry in entries) > archive_bytes:
        raise BadArchive("must not name more data than it holds")
    if len({entry.header_offset for entry in entries}) < len(entries):
        raise BadArchive("must not name one member twice")
    folders: set[str] = set()
    members: dict[str, zipfile.ZipInfo] = {}
    for entry in entries:
        if len(entry.orig_filename.encode()) > ZIP_MAX_NAME_BYTES:  # the name before a NUL cut it short
            raise BadArchive("must not contain a name too long to store")
        if stat.S_ISLNK(entry.external_attr >> 16):
            raise BadArchive("must not contain a link")
        if entry.flag_bits & _ENCRYPTED:
            raise BadArchive("must not contain an encrypted entry")
        if entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise BadArchive("must contain only stored or deflated entries")
        parts = _parts(entry.filename)
        if len(parts) > ZIP_MAX_DEPTH:
            raise BadArchive(f"must have names of at most {ZIP_MAX_DEPTH} levels")
        is_folder = entry.filename.endswith("/")
        above = parts if is_folder else parts[:-1]
        folders.update("/".join(above[:end]) for end in range(1, len(above) + 1))
        if len(folders) > max_entries:
            raise BadArchive(f"must have at most {max_entries} folders")
        if not is_folder:
            name = "/".join(parts)
            if not name:
                raise BadArchive("must not contain a file without a name")
            if name in members:
                raise BadArchive("must not contain two files of one name")
            members[name] = entry
    if not folders.isdisjoint(members):
        raise BadArchive("must not contain a file and a folder of one name")
    return sorted(folders), members


def _parts(name: str) -> list[str]:
    """The parts of an entry's name, '.' and empty ones dropped."""
    if name.startswith(("/", "\\")) or _DRIVE.match(name):
        raise BadArchive("must not contain an absolute path")
    if ".." in _SEPARATORS.split(name):
        raise BadArchive("must not contain a '..' part")
    parts = [part for part in name.split("/") if part not in ("", ".")]
    if (len(name.encode()) > ZIP_MAX_NAME_BYTES
            or any(len(part.encode()) > ZIP_MAX_PART_BYTES for part in parts)):
        raise BadArchive("must not contain a name too long to store")
    return parts


def _identity(folder: Path) -> tuple[int, int]:
    found = os.stat(folder)
    return found.st_dev, found.st_ino


def _child(target: Path, name: str, made: Mapping[str, tuple[int, int]]) -> Path:
    """target / name, where the folder it goes into is still the one that was made there. One stat(), however deep
    it lies: resolving the path again for every entry costs one call per level, 30 s for a hostile archive."""
    parent = name.rpartition("/")[0]
    if _identity(target / parent) != made[parent]:
        raise UnsafePath(f"{target}: a folder was replaced while the archive was unpacked")
    return target / name


def _write_member(opened: zipfile.ZipFile, entry: zipfile.ZipInfo, target: Path, name: str,
                  made: Mapping[str, tuple[int, int]], left: int, max_bytes: int) -> int:
    """Write one file, made new, a chunk at a time, and return how many of max_bytes are left after it."""
    with _reading():
        source = opened.open(entry)
    with source:
        path = _child(target, name, made)
        with open(path, "xb") as out:
            while True:
                with _reading():
                    chunk = source.read(_CHUNK)
                if not chunk:
                    break
                left -= len(chunk)
                if left < 0:  # before it is written
                    raise BadArchive(f"must unpack to at most {max_bytes} bytes")
                out.write(chunk)
        path.chmod(JOB_FILE_MODE)
    return left
