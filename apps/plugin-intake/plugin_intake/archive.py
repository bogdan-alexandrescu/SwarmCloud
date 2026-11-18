"""Read one quarantined archive into memory, checking every entry as it comes.

Nothing is written to any disk: each entry's header is checked before a byte of
its data is read, and data is counted as it is actually decompressed, never
taken from a header (§2.3 steps 1-3). Nothing from the archive is ever executed.
"""

from __future__ import annotations

import gzip
import io
import stat
import tarfile
import unicodedata
import zipfile
import zlib
from dataclasses import dataclass

from . import limits
from .refusals import Refusal

FILE = "file"
DIR = "dir"
SYMLINK = "symlink"
HARDLINK = "hardlink"

_CHUNK = 1024 * 1024


@dataclass
class Entry:
    """One archive member: `path` is its normalised segments, `index` its place in the archive."""

    index: int
    path: tuple[str, ...]
    kind: str
    data: bytes = b""
    executable: bool = False
    #: A symbolic link's target as written; a hard link's target as segments.
    link: str | tuple[str, ...] | None = None


def split_path(raw: str, index: int | None) -> tuple[str, ...]:
    """A member name as segments, or a refusal: relative, no '..', no NUL, no backslash."""
    if "\x00" in raw or "\\" in raw:
        raise Refusal("entry_path_invalid", index)
    try:
        raw.encode("utf-8")
    except UnicodeEncodeError:
        # tarfile decodes undecodable bytes as lone surrogates.
        raise Refusal("entry_path_invalid", index) from None
    if raw.startswith("/"):
        raise Refusal("entry_path_absolute", index)
    if len(raw) >= 2 and raw[1] == ":" and raw[0].isascii() and raw[0].isalpha():
        raise Refusal("entry_path_invalid", index)
    segments = []
    for segment in raw.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            raise Refusal("entry_path_parent", index)
        if len(segment.encode("utf-8")) > limits.MAX_SEGMENT_BYTES:
            raise Refusal("entry_path_segment_too_long", index)
        segments.append(segment)
    if len(segments) > limits.MAX_SEGMENTS:
        raise Refusal("entry_path_too_deep", index)
    return tuple(segments)


def fold(path: tuple[str, ...]) -> tuple[str, ...]:
    """The key two paths collide on: NFC, then case folded (a case-insensitive checkout)."""
    return tuple(unicodedata.normalize("NFC", s).casefold() for s in path)


class _Budget:
    """File bytes and file counts, from bytes actually read."""

    def __init__(self) -> None:
        self.bytes = 0
        self.files = 0
        self.entries = 0

    def entry(self, index: int, counts_as_file: bool) -> None:
        self.entries += 1
        if counts_as_file:
            self.files += 1
        if self.files > limits.MAX_FILES or self.entries > limits.MAX_ENTRIES:
            raise Refusal("too_many_files", index)

    def add(self, n: int, index: int) -> None:
        self.bytes += n
        if self.bytes > limits.MAX_UNPACKED_BYTES:
            raise Refusal("unpacked_too_large", index)


class _CountingGzip(io.RawIOBase):
    """A gzip stream that refuses once it has produced too much, or too much per packed byte."""

    def __init__(self, packed: bytes) -> None:
        self._gz = gzip.GzipFile(fileobj=io.BytesIO(packed), mode="rb")
        self._packed = len(packed)
        self.produced = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:  # noqa: ANN001 -- io's own signature
        data = self._gz.read(len(buffer))
        n = len(data)
        buffer[:n] = data
        self.produced += n
        if self.produced > limits.MAX_UNPACKED_BYTES + limits.TAR_OVERHEAD_BYTES:
            raise Refusal("unpacked_too_large")
        if (
            self.produced > limits.RATIO_FLOOR_BYTES
            and self.produced > limits.MAX_EXPANSION_RATIO * self._packed
        ):
            raise Refusal("expansion_ratio")
        return n


def read_archive(packed: bytes) -> list[Entry]:
    """Every member of a .tar.gz or a .zip, checked; a `Refusal` on the first bad one."""
    if len(packed) > limits.MAX_PACKED_BYTES:
        raise Refusal("archive_too_large")
    if packed[:2] == b"\x1f\x8b":
        entries = _read_tar_gz(packed)
    elif packed[:4] in (b"PK\x03\x04", b"PK\x05\x06"):
        entries = _read_zip(packed)
    else:
        raise Refusal("archive_format_unknown")
    _check_duplicates(entries)
    if not any(e.kind != DIR for e in entries):
        raise Refusal("archive_empty")
    return entries


def _check_mode(mode: int, index: int) -> None:
    if mode & (stat.S_ISUID | stat.S_ISGID):
        raise Refusal("entry_setuid", index)


def _read_tar_gz(packed: bytes) -> list[Entry]:
    budget = _Budget()
    entries: list[Entry] = []
    stream = _CountingGzip(packed)
    try:
        with tarfile.open(fileobj=io.BufferedReader(stream, _CHUNK), mode="r|", encoding="utf-8") as tar:
            for index, member in enumerate(tar):
                entries.append(_tar_entry(tar, member, index, budget))
    except Refusal:
        raise
    except (tarfile.TarError, OSError, EOFError, zlib.error, ValueError):
        raise Refusal("archive_corrupt") from None
    return entries


def _tar_entry(tar: tarfile.TarFile, member: tarfile.TarInfo, index: int, budget: _Budget) -> Entry:
    path = split_path(member.name, index)
    _check_mode(member.mode, index)
    if member.ischr() or member.isblk() or member.isfifo():
        raise Refusal("entry_special_file", index)
    if member.issparse():
        raise Refusal("entry_type_unsupported", index)
    if member.isdir():
        budget.entry(index, counts_as_file=False)
        return Entry(index, path, DIR)
    if member.issym():
        budget.entry(index, counts_as_file=True)
        target = member.linkname
        if len(target.encode("utf-8", "surrogatepass")) > limits.MAX_LINK_TARGET_BYTES:
            raise Refusal("link_escapes_root", index)
        return Entry(index, path, SYMLINK, link=target)
    if member.islnk():
        budget.entry(index, counts_as_file=True)
        return Entry(index, path, HARDLINK, link=split_path(member.linkname, index))
    if not member.isreg():
        raise Refusal("entry_type_unsupported", index)
    budget.entry(index, counts_as_file=True)
    if member.size > limits.MAX_UNPACKED_BYTES - budget.bytes:
        raise Refusal("unpacked_too_large", index)
    source = tar.extractfile(member)
    chunks = []
    while True:
        chunk = source.read(_CHUNK)
        if not chunk:
            break
        budget.add(len(chunk), index)
        chunks.append(chunk)
    return Entry(index, path, FILE, b"".join(chunks), executable=bool(member.mode & 0o111))


def _read_zip(packed: bytes) -> list[Entry]:
    budget = _Budget()
    entries: list[Entry] = []
    try:
        with zipfile.ZipFile(io.BytesIO(packed)) as archive:
            infos = archive.infolist()
            # The index first (§2.3 step 2), before any byte is decompressed.
            shapes = [_zip_shape(info, index) for index, info in enumerate(infos)]
            for index, (info, (path, kind, mode)) in enumerate(zip(infos, shapes)):
                budget.entry(index, counts_as_file=kind != DIR)
                if kind == DIR:
                    entries.append(Entry(index, path, DIR))
                    continue
                cap = limits.MAX_LINK_TARGET_BYTES if kind == SYMLINK else limits.MAX_UNPACKED_BYTES - budget.bytes
                if info.file_size > cap:
                    raise Refusal("link_escapes_root" if kind == SYMLINK else "unpacked_too_large", index)
                data = _read_zip_member(archive, info, index, budget)
                if kind == SYMLINK:
                    try:
                        target = data.decode("utf-8")
                    except UnicodeDecodeError:
                        raise Refusal("entry_path_invalid", index) from None
                    entries.append(Entry(index, path, SYMLINK, link=target))
                else:
                    entries.append(Entry(index, path, FILE, data, executable=bool(mode & 0o111)))
    except Refusal:
        raise
    # RuntimeError: zipfile raises it for an encrypted member and its subclass
    # for a compression method or flag bit the stdlib cannot read; both are
    # refused, never passed through.
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, EOFError, zlib.error, ValueError, RuntimeError):
        raise Refusal("archive_corrupt") from None
    return entries


def _zip_shape(info: zipfile.ZipInfo, index: int) -> tuple[tuple[str, ...], str, int]:
    path = split_path(info.filename, index)
    if info.flag_bits & 0x1:
        raise Refusal("entry_encrypted", index)
    mode = info.external_attr >> 16 if info.create_system == 3 else 0
    _check_mode(mode, index)
    kind_bits = stat.S_IFMT(mode)
    if kind_bits in (stat.S_IFCHR, stat.S_IFBLK, stat.S_IFIFO, stat.S_IFSOCK):
        raise Refusal("entry_special_file", index)
    if kind_bits == stat.S_IFLNK:
        return path, SYMLINK, mode
    if info.is_dir() or kind_bits == stat.S_IFDIR:
        return path, DIR, mode
    if kind_bits not in (0, stat.S_IFREG):
        raise Refusal("entry_type_unsupported", index)
    return path, FILE, mode


def _read_zip_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo, index: int, budget: _Budget) -> bytes:
    chunks = []
    written = 0
    with archive.open(info) as source:
        while True:
            chunk = source.read(_CHUNK)
            if not chunk:
                break
            written += len(chunk)
            budget.add(len(chunk), index)
            if written > limits.RATIO_FLOOR_BYTES and written > limits.MAX_EXPANSION_RATIO * max(info.compress_size, 1):
                raise Refusal("expansion_ratio", index)
            chunks.append(chunk)
    return b"".join(chunks)


def _check_duplicates(entries: list[Entry]) -> None:
    """No two members on one path once folded; a repeated directory header is harmless."""
    seen: dict[tuple[str, ...], str] = {}
    for entry in entries:
        if not entry.path:
            continue
        key = fold(entry.path)
        before = seen.get(key)
        if before is not None and not (before == DIR and entry.kind == DIR):
            raise Refusal("entry_duplicate", entry.index)
        seen[key] = entry.kind
    # A file and a directory on one path ('a' a file, 'a/b' an entry).
    for entry in entries:
        for depth in range(1, len(entry.path)):
            parent = seen.get(fold(entry.path[:depth]))
            if parent in (FILE, HARDLINK):
                raise Refusal("entry_duplicate", entry.index)
