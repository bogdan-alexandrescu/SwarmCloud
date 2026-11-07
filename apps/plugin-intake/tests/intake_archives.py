"""Crafted archives, built in memory at test time: nothing is fetched."""

from __future__ import annotations

import gzip
import io
import stat
import tarfile
import zipfile
from dataclasses import dataclass, field


@dataclass
class Member:
    name: str
    data: bytes = b""
    kind: bytes = tarfile.REGTYPE
    mode: int = 0o644
    link: str = ""
    mtime: int = 1_700_000_000
    extra: dict = field(default_factory=dict)


def file(name: str, data: bytes | str = b"", mode: int = 0o644, mtime: int = 1_700_000_000) -> Member:
    return Member(name, data.encode() if isinstance(data, str) else data, tarfile.REGTYPE, mode, mtime=mtime)


def folder(name: str, mode: int = 0o755) -> Member:
    return Member(name, kind=tarfile.DIRTYPE, mode=mode)


def symlink(name: str, target: str) -> Member:
    return Member(name, kind=tarfile.SYMTYPE, mode=0o777, link=target)


def hardlink(name: str, target: str) -> Member:
    return Member(name, kind=tarfile.LNKTYPE, link=target)


def device(name: str) -> Member:
    return Member(name, kind=tarfile.CHRTYPE)


def fifo(name: str) -> Member:
    return Member(name, kind=tarfile.FIFOTYPE)


def tar_gz(members: list[Member], *, uid: int = 1000) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for member in members:
            info = tarfile.TarInfo(member.name)
            info.type = member.kind
            info.mode = member.mode
            info.mtime = member.mtime
            info.uid = info.gid = uid
            info.uname = info.gname = "someone"
            info.linkname = member.link
            if member.kind == tarfile.REGTYPE:
                info.size = len(member.data)
                tar.addfile(info, io.BytesIO(member.data))
            else:
                tar.addfile(info)
    return gzip.compress(raw.getvalue(), mtime=0)


def zip_archive(members: list[Member]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for member in members:
            info = zipfile.ZipInfo(member.name, date_time=(2024, 1, 2, 3, 4, 5))
            info.create_system = 3
            info.compress_type = zipfile.ZIP_DEFLATED
            if member.kind == tarfile.DIRTYPE:
                info.external_attr = (stat.S_IFDIR | member.mode) << 16
                archive.writestr(info, b"")
            elif member.kind == tarfile.SYMTYPE:
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(info, member.link.encode())
            elif member.kind == tarfile.CHRTYPE:
                info.external_attr = (stat.S_IFCHR | 0o644) << 16
                archive.writestr(info, b"")
            else:
                info.external_attr = (stat.S_IFREG | member.mode) << 16
                archive.writestr(info, member.data)
    return out.getvalue()


def zip_raw_name(name: str, data: bytes = b"x") -> bytes:
    """A zip whose one entry has exactly `name`, as a zip-slip archive is written."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr(zipfile.ZipInfo(name, date_time=(2024, 1, 1, 0, 0, 0)), data)
    return out.getvalue()
