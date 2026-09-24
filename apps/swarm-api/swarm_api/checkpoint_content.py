"""What is INSIDE a checkpoint: a file listing, one file, and the whole archive.

Owner decision 2026-09-24 on redesign-v2 S3 (item A3): a checkpoint gets all
three. Until now the checkpoint route (`GET /v1/tasks/{id}/checkpoints`) could
say a checkpoint existed, how big its archive was and whether a retry would
resume from it -- and nothing could say what was IN it. `types.ts` recorded the
reason in capitals: "CONTENTS ARE NOT RECORDED ANYWHERE". The manifest carries
a `file_count` and no names, so a listing means reading the tarball. This
module reads it.

THE KEY IS NEVER THE CALLER'S
-----------------------------
Three identifiers arrive from the request: the task id, the checkpoint id and,
optionally, the attempt id. None of them becomes part of an object key until:

  * `tenant_scope` has named the caller's tenant and `Store.get_task` has
    returned the task INSIDE that tenant -- the same 404 a missing task gets
    otherwise (`InspectionService._scoped`, called rather than copied, so the
    boundary this route enforces is the boundary the artifact route enforces);
  * each has passed `objects.safe_segment`, a single-segment check with no
    `/`, no `..` and no leading dot;
  * the checkpoint's own prefix has been LISTED and every key in the answer has
    been re-parsed by `inspect.parse_checkpoint_key`, which re-checks tenant
    and task on the key itself.

The archive key is then REBUILT from those parts -- `<prefix>/archive.tar.gz`
-- exactly as `CheckpointManager.create` builds it. The manifest's own
`archive_key` is evidence, compared and reported (`archive_key_agrees`), never
followed: a manifest is data a worker wrote into a bucket, and an address taken
from it would be an address an attacker who can write one object chooses.

THE PATH INSIDE THE ARCHIVE IS NOT A KEY EITHER. `{path}` in the per-file route
names a MEMBER of the tarball, matched for exact equality against the names in
it, and it never reaches the object store at all -- the only object read is the
archive the three checks above resolved. It is still refused if it is absolute,
contains `..`, `.`, an empty segment, a backslash or a control character
(`requested_path`), because "it cannot reach a key" is the argument for today's
code and the refusal is the argument for tomorrow's.

STREAMED, AND BOUNDED THREE WAYS
--------------------------------
A checkpoint archive may be 2 GiB (`CheckpointManager.max_bytes`). Nothing here
holds more than one read chunk of it: the archive is pulled through
`read_range` one `chunk_bytes` window at a time, inflated incrementally, and
walked with `tarfile`'s forward-only stream mode, which never seeks back. A tar
has no index, so a listing must inflate everything before the last header it
reports, and three budgets bound what one request may cost:

  * `max_entries`      -- how many members a listing returns (`entry_cap`);
  * `max_scan_bytes`   -- compressed bytes read from the store (`scan_budget`);
  * `max_inflate_bytes`-- bytes decompressed, which is what stops a small
                          archive that inflates to terabytes of zeros turning a
                          listing into a CPU denial of service (`inflate_budget`).

Hitting any of them is REPORTED -- `truncated: true` with the reason -- and
never silent. A listing that showed the first 5000 of 90000 files and a listing
that showed all of them are otherwise byte-identical.

THE ANSWERS STAY APART
----------------------
  * `status: ok`      -- the archive was read; `files` is what it holds, up to a
                         stated cut. `files: []` with `status: ok` and
                         `truncated: false` is a REAL empty workspace.
  * `status: absent`  -- the checkpoint exists and its archive object does not;
                         `files` is null, never `[]`.
  * `status: corrupt` -- the object is there and is not a readable tar.gz past
                         some point; `files` is what was read before it, and
                         `truncated` is true.
  * a failed READ     -- 503, with no `files` key at all. There is no honest
                         partial answer to "the store could not be read".

WHAT IS REDACTED AND WHAT IS NOT
--------------------------------
A single file is served as text through `swarm_api.redaction`, windowed and
whitespace-aligned by the same `_align` the artifact route uses, under the same
size cap. The WHOLE ARCHIVE is not, and cannot be: it is gzip, and no pattern
runs over compressed bytes. Serving it unredacted is the owner's decision of
2026-09-24 (redesign-v2 S3), made knowing that. The response says so in
`X-Swarm-Redaction: not-applied` rather than leaving a caller to assume the
per-file guarantee extends to it.

Nothing here writes. There is no upload, no delete and no copy on the reader
this module is given (`objects.ObjectReader`), and the IAM grant behind it is
`roles/storage.objectViewer`.
"""

from __future__ import annotations

import io
import json
import logging
import re
import tarfile
import zlib
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Iterator

from swarm_common.models import Task

from .errors import ApiError, NotFound, UpstreamUnavailable, ValidationFailed
from .inspect import (
    ARTIFACT_SNIFF_BYTES,
    CHECKPOINTS_SEGMENT,
    MANIFEST_MAX_BYTES,
    CheckpointRef,
    InspectionService,
    _align,
    _artifact_row,
    _as_int,
    _as_text,
    parse_checkpoint_key,
)
from .objects import ObjectAbsent, ObjectInfo, ObjectReader, ObjectSlice, ObjectUnreadable
from .redaction import redact, redact_detail

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Budgets. Every one of them is reported when it is hit.
# --------------------------------------------------------------------------

#: One ranged read from the store. 1 MiB is large enough that a 256 MiB scan is
#: 256 requests rather than the 26,000 `tarfile`'s own 10 KiB record size would
#: make, and small enough that the process never holds a meaningful fraction of
#: a checkpoint.
CHUNK_BYTES = 1024 * 1024

#: Compressed bytes one listing or one file read may pull from the store. A
#: checkpoint may be 2 GiB; this reads at most an eighth of one. NOT MEASURED:
#: every window is two SEQUENTIAL store calls (`GcsObjectReader.read_range`
#: looks the object up with `get_blob`, then downloads the range), so 256 MiB
#: is 256 windows and 512 round trips -- seconds to low tens of seconds at the
#: tens of milliseconds an in-region call takes, well inside the request
#: timeout, and slower than "interactive". Past it the listing says
#: `scan_budget` and the whole-archive download is the way to the rest.
MAX_SCAN_BYTES = 256 * 1024 * 1024

#: Bytes decompressed per request. Gzip reaches ~1000:1 on zeros, so the
#: compressed budget alone admits a 256 MiB object that inflates to 256 GiB --
#: minutes of zlib CPU on a request thread. 1 GiB is a few seconds.
MAX_INFLATE_BYTES = 1024 * 1024 * 1024

#: Members one listing returns. The same order of magnitude as
#: `ApiSettings.object_scan_limit` (5000), for the same reason: a response a
#: browser can render and a person can scroll, with `truncated` past it.
MAX_ENTRIES = 5000

#: Largest piece handed back by one `decompress` call. It is what bounds memory
#: on a highly compressible archive, where one 1 MiB input chunk could otherwise
#: become one 1 GiB output string.
_INFLATE_PIECE = 256 * 1024

#: The archive's file name in the whole-archive download.
_DOWNLOAD_SUFFIX = ".tar.gz"

#: What a digest must look like to be put in a response HEADER. The manifest's
#: value is data a worker wrote into a bucket. Starlette encodes a header value
#: as latin-1 and uvicorn refuses one with a control character in it, and
#: either failure is a 500 for the whole download -- or, with CR/LF and a
#: server that did not check, a header the worker chose. Anything that is not
#: a digest is left out of the header rather than failing the download it
#: would have labelled; the listing still shows it, escaped.
_SHA256_HEX = re.compile(r"[0-9a-fA-F]{64}")

# --------------------------------------------------------------------------
# What may be served as text
# --------------------------------------------------------------------------

#: The content-type allowlist for a single member, by extension. A member whose
#: extension is not here is reported `binary` and NO byte of it is read -- the
#: allowlist is what the owner's decision asked for, and it is an allowlist
#: rather than a denylist because the set of binary formats is not ours to
#: enumerate. A member with NO extension (`Makefile`, `LICENSE`, `.env`,
#: `.gitignore` -- `PurePosixPath` gives a dotfile no suffix) is a text
#: CANDIDATE, decided by the NUL sniff below exactly as the artifact route
#: decides. The sniff runs on allowed extensions too: a `.txt` full of NULs is
#: not text because of its name.
#:
#: The value is the content type REPORTED in the payload. It is informational --
#: the bytes always travel as a JSON string, never as a response of that type,
#: so an `.html` member is shown as its source and never rendered.
TEXT_EXTENSIONS: dict[str, str] = {
    **dict.fromkeys((".md", ".markdown", ".mdx"), "text/markdown"),
    **dict.fromkeys(
        (".txt", ".log", ".text", ".rst", ".adoc", ".org", ".tex", ".bib",
         ".ini", ".cfg", ".conf", ".properties", ".env", ".lock", ".mod", ".sum",
         ".diff", ".patch", ".mk", ".cmake", ".gradle", ".dockerfile"),
        "text/plain",
    ),
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    **dict.fromkeys((".json", ".jsonc", ".json5", ".ipynb"), "application/json"),
    **dict.fromkeys((".jsonl", ".ndjson"), "application/x-ndjson"),
    **dict.fromkeys((".yaml", ".yml"), "application/yaml"),
    ".toml": "application/toml",
    ".xml": "application/xml",
    **dict.fromkeys((".html", ".htm"), "text/html"),
    ".css": "text/css",
    **dict.fromkeys((".js", ".mjs", ".cjs", ".jsx"), "text/javascript"),
    **dict.fromkeys((".ts", ".tsx", ".mts", ".cts"), "text/x-typescript"),
    **dict.fromkeys((".py", ".pyi"), "text/x-python"),
    **dict.fromkeys(
        (".rb", ".go", ".rs", ".java", ".kt", ".kts", ".scala", ".swift", ".c",
         ".h", ".cc", ".cpp", ".cxx", ".hpp", ".cs", ".m", ".php", ".pl", ".lua",
         ".r", ".sql", ".sh", ".bash", ".zsh", ".fish", ".ps1", ".tf", ".tfvars",
         ".hcl", ".proto", ".graphql", ".gql", ".vue", ".svelte", ".ex", ".exs",
         ".erl", ".hs", ".ml", ".clj", ".dart", ".zig", ".nix", ".sol"),
        "text/x-source",
    ),
}

#: What an extensionless member is reported as when the sniff says it is text.
_PLAIN = "text/plain"


def content_type_for(path: str) -> str | None:
    """The type a member would be served as, or None if it is not allowed.

    None is the allowlist refusing. It is decided from the NAME alone and
    before any byte is read, so a refused member costs no scan beyond finding
    its header.
    """
    suffix = PurePosixPath(path).suffix.lower()
    if suffix == "":
        return _PLAIN
    return TEXT_EXTENSIONS.get(suffix)


# --------------------------------------------------------------------------
# Paths inside the archive
# --------------------------------------------------------------------------

#: A request path longer than this is not a path anyone typed. POSIX PATH_MAX.
_MAX_PATH = 4096


def _has_control(text: str) -> bool:
    return any(ord(c) < 0x20 or ord(c) == 0x7F for c in text)


def requested_path(path: Any) -> str:
    """Validate the `{path}` a caller asked for, or refuse it with a 422.

    It never becomes an object key -- see the module docstring -- and it is
    refused anyway if it could mean "somewhere else" to ANY consumer: absolute,
    a `..` or `.` segment, an empty segment (`a//b`, a trailing `/`), a
    backslash (a separator on the platform a downloaded file may be unpacked
    on), a NUL or any other control character. Matching is exact, so a path
    that survives this addresses one member of one archive and nothing else.
    """
    if not isinstance(path, str) or not path:
        raise ValidationFailed("a path inside the checkpoint is required")
    if len(path) > _MAX_PATH:
        raise ValidationFailed("the path is longer than any path inside a checkpoint")
    if path.startswith("/"):
        raise ValidationFailed("the path must be relative to the checkpoint's root")
    if "\\" in path or _has_control(path):
        raise ValidationFailed("the path contains a character no checkpoint member may have")
    if any(segment in ("", ".", "..") for segment in path.split("/")):
        raise ValidationFailed(
            "the path contains an empty, '.' or '..' segment; name the file exactly "
            "as the listing spells it"
        )
    return path


def undecodable(text: str) -> bool:
    """Whether `text` holds a lone surrogate, which no JSON response can carry.

    `tarfile` decodes names with `errors="surrogateescape"`, so a name whose
    bytes are not UTF-8 -- a Latin-1 fixture in a cloned repository, which the
    worker archives exactly as `Path.rglob` hands it over -- comes back holding
    `\\udc80`-`\\udcff`. `json.loads` returns any lone surrogate from a `\\ud800`
    escape. Starlette's `JSONResponse` encodes with `encode("utf-8")`, which
    raises on both, AFTER the route has returned: an unhandled 500 with no
    reason, on every request that touches the string.
    """
    return any(0xD800 <= ord(c) <= 0xDFFF for c in text)


def _escape_surrogate(c: str) -> str:
    code = ord(c)
    if 0xDC80 <= code <= 0xDCFF:
        # A byte surrogateescape carried: shown as the byte it was.
        return f"\\x{code - 0xDC00:02x}"
    return f"\\u{code:04x}"


def displayable(text: str) -> str:
    """`text` as a response can carry it: every lone surrogate escaped.

    `caf\\udce9.txt` is served as `caf\\xe9.txt` -- the bytes the archive
    holds, spelled the way Python and `ls -b` spell them. The result is a
    DISPLAY and never an address: it contains a backslash, which
    `requested_path` refuses, so the escaped form cannot be sent back to name
    the member and nothing has to decide which of two members it meant.
    """
    if not undecodable(text):
        return text
    return "".join(
        _escape_surrogate(c) if 0xD800 <= ord(c) <= 0xDFFF else c for c in text
    )


def _shown(value: str | None) -> str | None:
    return None if value is None else displayable(value)


def member_path(name: str) -> tuple[str | None, bool]:
    """How a tar member's name is shown, and whether it is UNSAFE.

    `(None, False)` is the archive's own root (`.` or `./`), which `tar -C work
    -czf x .` writes and `CheckpointManager._write_archive` does not; it is not
    a file anyone can open and it is left out of the listing.

    UNSAFE members are LISTED, flagged, and never readable through the per-file
    route. Hiding them would hide the finding: a member named `../../etc/x` is
    exactly what `checkpoint._safe_members` refuses on restore, so an archive
    carrying one would fail to resume, and a person looking at the checkpoint
    is the person who needs to see why.

    `unsafe` is decided on the name AS DECODED and the name is escaped only
    afterwards (`displayable`): the escape introduces a backslash, and a
    Latin-1 name is not an escape from the archive -- a restore unpacks it
    without complaint. `undecodable` is the separate fact for that.
    """
    shown = name
    while shown.startswith("./"):
        shown = shown[2:]
    shown = displayable(shown.rstrip("/"))
    if shown in ("", "."):
        return None, False
    unsafe = (
        shown.startswith("/")
        or "\\" in shown
        or _has_control(shown)
        or len(shown) > _MAX_PATH
        or any(segment in ("", ".", "..") for segment in shown.split("/"))
    )
    return displayable(shown), unsafe


def member_type(member: tarfile.TarInfo) -> str:
    if member.isreg():
        return "file"
    if member.isdir():
        return "dir"
    if member.issym():
        return "symlink"
    if member.islnk():
        return "hardlink"
    if member.isfifo():
        return "fifo"
    if member.ischr():
        return "chardev"
    if member.isblk():
        return "blockdev"
    return "other"


def member_row(member: tarfile.TarInfo) -> dict[str, Any] | None:
    """One listing row -- `{path, size, mode, type}` plus `link`, `unsafe` and
    `undecodable`.

    The shape is CONSTANT: `link` is null rather than missing for a member that
    is not a link, so a client never has to tell "no link" from "the field was
    dropped".

    `link` is run through the redaction filter. A symlink target is a string
    an agent wrote, which is the same class of text as log content; paths are
    not, and are served as the archive spells them.

    `undecodable` says the `path` or the `link` shown here is an ESCAPED
    rendering of bytes that are not UTF-8 (`displayable`). Such a row is listed
    and cannot be opened by name: the escaped form is refused as a path.
    """
    path, unsafe = member_path(member.name)
    if path is None:
        return None
    kind = member_type(member)
    escaped = undecodable(member.name)
    link = None
    if kind in ("symlink", "hardlink"):
        target = member.linkname or ""
        escaped = escaped or undecodable(target)
        link = redact(displayable(target)).text
    return {
        "path": path,
        "size": int(member.size or 0),
        "mode": int(member.mode or 0) & 0o7777,
        "type": kind,
        "link": link,
        "unsafe": unsafe,
        "undecodable": escaped,
    }


# --------------------------------------------------------------------------
# The stream: one object, read forward, in bounded windows
# --------------------------------------------------------------------------

class _BudgetExceeded(Exception):
    """A per-request budget was reached. `reason` is the payload's word for it."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _Corrupt(Exception):
    """The object is there and is not a readable tar.gz past some point."""


class _Changed(Exception):
    """The object's size changed between two windows of one read."""


class DownloadAborted(Exception):
    """A whole-archive stream that could not be finished after its status was sent.

    Raised with a REDACTED message in place of the store's own exception: the
    ASGI server logs whatever ends a response, and a storage client's error can
    quote the request it failed on, signed URL and all.
    """


class _ObjectStream(io.RawIOBase):
    """One object, read sequentially through `read_range`, one window at a time.

    Holds at most one window (`chunk_bytes`). `consumed` is how much of the
    object has been pulled from the store, which is what `max_scan_bytes`
    bounds and what the listing reports as `scanned_bytes`.

    The object's size is taken from the FIRST window and every later window is
    required to agree: a checkpoint is write-once, so a size that moves means
    the object was replaced mid-read, and splicing two objects' bytes into one
    listing would report a tarball that never existed.
    """

    def __init__(self, reader: ObjectReader, key: str, *, chunk_bytes: int, budget: int) -> None:
        super().__init__()
        self._reader = reader
        self._key = key
        self._chunk = max(1, chunk_bytes)
        self._budget = budget
        self._window = b""
        self._at = 0
        self.consumed = 0
        self.total: int | None = None

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        if self._at >= len(self._window):
            if self.total is not None and self.consumed >= self.total:
                return 0
            if self.consumed >= self._budget:
                raise _BudgetExceeded("scan_budget")
            length = min(self._chunk, self._budget - self.consumed)
            piece = self._reader.read_range(self._key, offset=self.consumed, length=length)
            if self.total is None:
                self.total = piece.total_bytes
            elif piece.total_bytes != self.total:
                raise _Changed(
                    f"the archive was {self.total} bytes and is now {piece.total_bytes}"
                )
            if not piece.data:
                return 0
            self._window = piece.data
            self._at = 0
            self.consumed += len(piece.data)
        n = min(len(buffer), len(self._window) - self._at)
        buffer[:n] = self._window[self._at : self._at + n]
        self._at += n
        return n


class _Gunzip(io.RawIOBase):
    """Incremental gzip inflation over `_ObjectStream`, with an output budget.

    Not `tarfile`'s own `r|gz`, for two reasons that are both about honesty:

      * `tarfile` stops quietly at a truncated stream -- a gzip cut short
        yields the members before the cut and then looks like the end of the
        archive. `finish()` below requires the gzip stream's own end marker
        (and zlib checks its CRC on the way), so a short object is `corrupt`
        rather than a shorter listing;
      * `tarfile` has no output budget, and a bomb is the case that needs one.
    """

    def __init__(self, raw: _ObjectStream, *, budget: int) -> None:
        super().__init__()
        self._raw = raw
        self._z = zlib.decompressobj(16 + zlib.MAX_WBITS)
        self._out = b""
        self._at = 0
        self._budget = budget
        self.inflated = 0

    def readable(self) -> bool:
        return True

    def _fill(self) -> bool:
        while True:
            if self._z.eof:
                return False
            source = self._z.unconsumed_tail
            drained = False
            if not source:
                source = self._raw.read(CHUNK_BYTES)
                # An empty read is NOT yet a truncation: zlib may still hold
                # output it could not return under the piece limit, and a
                # `decompress(b"")` is what flushes it. Only a flush that
                # produces nothing, with no end marker seen, is a short object.
                drained = not source
            try:
                out = self._z.decompress(source, _INFLATE_PIECE)
            except zlib.error as exc:
                raise _Corrupt(f"the archive is not a readable gzip stream: {exc}") from None
            if out:
                self.inflated += len(out)
                if self.inflated > self._budget:
                    raise _BudgetExceeded("inflate_budget")
                self._out = out
                self._at = 0
                return True
            if drained and not self._z.eof:
                raise _Corrupt(
                    "the archive ends before its compressed stream does; the object "
                    "is truncated"
                )

    def readinto(self, buffer: Any) -> int:
        if self._at >= len(self._out) and not self._fill():
            return 0
        n = min(len(buffer), len(self._out) - self._at)
        buffer[:n] = self._out[self._at : self._at + n]
        self._at += n
        return n

    def finish(self) -> None:
        """Read to the end of the gzip stream, or say why it has none.

        After the tar's end-of-archive blocks there is normally only padding
        left inside the gzip member, so this is cheap on a good archive and it
        is what turns a truncated object into `corrupt` instead of into a
        listing that silently stops early.
        """
        while self._fill():
            self._at = len(self._out)


@dataclass
class _Scan:
    """One forward pass over one archive, and what it cost."""

    raw: _ObjectStream
    gunzip: _Gunzip
    tar: tarfile.TarFile

    @property
    def scanned_bytes(self) -> int:
        return self.raw.consumed

    @property
    def inflated_bytes(self) -> int:
        return self.gunzip.inflated


# --------------------------------------------------------------------------
# Errors a caller can act on
# --------------------------------------------------------------------------

class ScanBudgetExceeded(ApiError):
    """The member lies beyond what one request may inflate. Not a 404: it may
    well be there. The whole-archive download is the way to it."""

    status_code = 413
    code = "checkpoint_scan_budget_exceeded"


class ArchiveCorrupt(ApiError):
    """The archive stops being a readable tar.gz before the member was reached."""

    status_code = 422
    code = "checkpoint_archive_corrupt"


# --------------------------------------------------------------------------
# The service
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ArchiveDownload:
    """A whole archive, ready to stream: the chunks and the headers that go with them."""

    chunks: Iterator[bytes]
    total_bytes: int
    filename: str
    headers: dict[str, str]


class CheckpointContent:
    """File listing, single-file read and whole-archive download for one checkpoint.

    Built AROUND the `InspectionService` rather than beside it. The tenant
    scoping (`_scoped`), the segment validation (`_segment`), the "no artifact
    store is configured" answer (`_reader`) and the artifact route's size caps
    are all taken from the one instance `deps.build_context` constructs, so a
    change to any of them changes this route in the same commit rather than
    leaving a second copy behind to drift. That is also why this class holds no
    reader of its own.
    """

    def __init__(
        self,
        inspection: InspectionService,
        *,
        chunk_bytes: int = CHUNK_BYTES,
        max_scan_bytes: int = MAX_SCAN_BYTES,
        max_inflate_bytes: int = MAX_INFLATE_BYTES,
        max_entries: int = MAX_ENTRIES,
    ) -> None:
        self._inspection = inspection
        self._chunk = chunk_bytes
        self._max_scan = max_scan_bytes
        self._max_inflate = max_inflate_bytes
        self._max_entries = max_entries

    # -- the caps the artifact-content route applies -----------------------
    @property
    def max_file_bytes(self) -> int:
        return self._inspection._max_artifact_bytes

    @property
    def default_file_bytes(self) -> int:
        return self._inspection._default_artifact_bytes

    @property
    def min_file_bytes(self) -> int:
        return self._inspection._min_artifact_bytes

    # -- resolution ----------------------------------------------------------
    def _locate(
        self,
        task: Task,
        attempts_root: str,
        *,
        checkpoint_id: str,
        attempt_id: str | None,
        reader: ObjectReader,
    ) -> tuple[CheckpointRef, dict[str, ObjectInfo]]:
        """The checkpoint the request names, and the objects under its prefix.

        With `attempt_id`, one prefix is listed: `.../attempts/<a>/checkpoints/<c>/`.
        Without it the task's whole attempts prefix is scanned for a checkpoint
        of that id, because ids restart per attempt (`ckpt-00001` exists once
        for EVERY attempt that checkpointed) and guessing which one was meant
        would serve another attempt's working tree under the right name. More
        than one match is a 422 naming the candidates; a scan cut short by the
        scan limit that found none is a 422 too, because "not found" cannot be
        concluded from a listing that did not finish.

        The trailing `/` on every prefix is load-bearing: `ckpt-00001` and
        `ckpt-000010` share a string prefix, and a listing without it would
        fold the second into the first.
        """
        segment = self._inspection._segment
        wanted = segment(checkpoint_id, what="checkpoint id")
        if attempt_id is not None:
            attempt = segment(attempt_id, what="attempt_id")
            scope = f"{attempts_root}{attempt}/{CHECKPOINTS_SEGMENT}/{wanted}/"
            limit = 16
        else:
            scope = attempts_root
            limit = self._inspection._scan_limit
        try:
            listing = reader.list_objects(scope, limit=limit)
        except ObjectUnreadable as exc:
            raise UpstreamUnavailable(
                "the artifact store could not be listed, so it is not known whether "
                "this checkpoint exists: " + redact_detail(exc.reason)
            ) from None

        found: dict[str, tuple[CheckpointRef, dict[str, ObjectInfo]]] = {}
        for info in listing.objects:
            ref = parse_checkpoint_key(info.key, tenant_id=task.tenant_id, task_id=task.id)
            if ref is None or ref.checkpoint_id != wanted:
                continue
            if attempt_id is not None and ref.attempt_id != attempt_id:
                continue
            found.setdefault(ref.prefix, (ref, {}))[1][info.key] = info

        if len(found) == 1:
            return next(iter(found.values()))
        if len(found) > 1:
            attempts = sorted(ref.attempt_id for ref, _ in found.values())
            raise ValidationFailed(
                f"{len(attempts)} attempts of this task wrote a checkpoint named "
                f"{wanted!r}; pass attempt_id to say which",
                detail={"checkpoint_id": wanted, "attempt_ids": attempts},
            )
        if listing.truncated:
            raise ValidationFailed(
                f"the scan limit cut this task's listing short before a checkpoint "
                f"named {wanted!r} was found; pass attempt_id to look in one attempt",
                detail={"checkpoint_id": wanted},
            )
        raise NotFound(
            f"task {task.id!r} has no checkpoint {wanted!r}"
            + (f" in attempt {attempt_id!r}" if attempt_id is not None else ""),
            detail={"task_id": task.id, "checkpoint_id": wanted, "attempt_id": attempt_id},
        )

    def _resolve(
        self,
        tenant_id: str,
        task_id: str,
        *,
        checkpoint_id: str,
        attempt_id: str | None,
    ) -> tuple[Task, CheckpointRef, dict[str, ObjectInfo], ObjectReader]:
        task, attempts_root = self._inspection._scoped(tenant_id, task_id)
        reader = self._inspection._reader()
        ref, objects = self._locate(
            task,
            attempts_root,
            checkpoint_id=checkpoint_id,
            attempt_id=attempt_id,
            reader=reader,
        )
        return task, ref, objects, reader

    def _manifest(
        self, reader: ObjectReader, ref: CheckpointRef, *, listed: bool
    ) -> dict[str, Any]:
        """What the commit marker says, in the three answers.

        `archive_key_agrees` is the manifest's `archive_key` compared with the
        key this route REBUILT. The rebuilt one is what is read either way;
        the comparison is served because a disagreement is a finding about how
        the checkpoint was written, not a detail to resolve quietly.
        """
        block: dict[str, Any] = {
            "status": "absent",
            "detail": None,
            "file_count": None,
            "archive_bytes": None,
            "archive_sha256": None,
            "created_at": None,
            "label": None,
            "archive_key_agrees": None,
        }
        if not listed:
            block["detail"] = (
                "no manifest.json: this checkpoint was never committed, so no restore "
                "would select it"
            )
            return block
        try:
            chunk = reader.read_range(ref.manifest_key, offset=0, length=MANIFEST_MAX_BYTES)
        except ObjectAbsent:
            block["detail"] = "the manifest disappeared between the listing and the read"
            return block
        except ObjectUnreadable as exc:
            block.update(status="unreadable", detail=redact_detail(exc.reason))
            return block
        try:
            data = json.loads(chunk.data.decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("a manifest must be a JSON object")
        except (UnicodeDecodeError, ValueError) as exc:
            block.update(
                status="unreadable",
                detail=f"the manifest is not readable JSON: {redact_detail(exc)}",
            )
            return block
        # `_shown`: `json.loads` returns a lone surrogate from a `\ud800`
        # escape, and a manifest is data a worker wrote into a bucket.
        block.update(
            status="present",
            file_count=_as_int(data.get("file_count")),
            archive_bytes=_as_int(data.get("archive_bytes")),
            archive_sha256=_shown(_as_text(data.get("archive_sha256"))),
            created_at=_shown(_as_text(data.get("created_at"))),
            label=_shown(_as_text(data.get("label"))),
            archive_key_agrees=data.get("archive_key") == ref.archive_key,
        )
        return block

    def _open(self, reader: ObjectReader, key: str) -> _Scan:
        raw = _ObjectStream(reader, key, chunk_bytes=self._chunk, budget=self._max_scan)
        gunzip = _Gunzip(raw, budget=self._max_inflate)
        # `r|` -- a plain, FORWARD-ONLY tar stream over the inflated bytes. The
        # 64 KiB buffer is tarfile's read size into `_Gunzip`, not a size of
        # anything held: the window and the inflate piece bound that.
        tar = tarfile.open(fileobj=gunzip, mode="r|", bufsize=64 * 1024)
        return _Scan(raw=raw, gunzip=gunzip, tar=tar)

    # -- 1. the listing ------------------------------------------------------
    def list_files(
        self,
        tenant_id: str,
        task_id: str,
        *,
        checkpoint_id: str,
        attempt_id: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Every member of the checkpoint's archive, in archive order, bounded.

        `GET /v1/tasks/{id}/checkpoints/{n}/files`. See the module docstring for
        the answers this keeps apart; what is added here is the cross-check
        against the manifest: `file_count_agrees` compares the members this
        listing counted the way `CheckpointManager._write_archive` counts them
        (every member that is not a directory) with the manifest's own
        `file_count`. It is null whenever either side is not a complete fact --
        a cut listing, an unreadable manifest -- because a mismatch between a
        partial count and a real one proves nothing.

        WHAT THE CROSS-CHECK IS FOR. A truncated OBJECT is caught by the gzip
        layer (`_Gunzip.finish` requires the stream's own end marker). A tar
        that is malformed INSIDE a valid gzip stream is not: `tarfile` ends
        iteration at an invalid header past the first one exactly as it ends it
        at the real end-of-archive block, and says nothing. The worker's own
        writer does not produce that, so it takes a hand-built archive -- and
        `file_count_agrees: false` is what makes such a listing visibly short
        rather than quietly so.
        """
        task, ref, objects, reader = self._resolve(
            tenant_id, task_id, checkpoint_id=checkpoint_id, attempt_id=attempt_id
        )
        if limit is not None and limit < 1:
            raise ValidationFailed("limit must be at least 1")
        cap = self._max_entries if limit is None else min(limit, self._max_entries)

        archive = objects.get(ref.archive_key)
        manifest = self._manifest(reader, ref, listed=ref.manifest_key in objects)
        body: dict[str, Any] = {
            "task_id": task.id,
            "tenant_id": task.tenant_id,
            "attempt_id": ref.attempt_id,
            "checkpoint_id": ref.checkpoint_id,
            "prefix": ref.prefix,
            "archive": {
                "key": ref.archive_key,
                "uri": reader.uri(ref.archive_key),
                "bytes": archive.size if archive is not None else None,
            },
            "manifest": manifest,
            "status": "ok",
            "detail": None,
            "files": None,
            "count": 0,
            "truncated": False,
            "truncated_reason": None,
            "file_count_agrees": None,
            "scanned_bytes": 0,
            "inflated_bytes": 0,
            "limits": {
                "entries": cap,
                "scan_bytes": self._max_scan,
                "inflate_bytes": self._max_inflate,
            },
        }

        if archive is None:
            body.update(
                status="absent",
                detail=(
                    "this checkpoint has no archive object, so there is nothing to list; "
                    "it was reclaimed, or its upload never completed"
                ),
            )
            return body

        files: list[dict[str, Any]] = []
        scan: _Scan | None = None
        try:
            scan = self._open(reader, ref.archive_key)
            finished = True
            for member in scan.tar:
                row = member_row(member)
                if row is None:
                    continue
                if len(files) >= cap:
                    body.update(truncated=True, truncated_reason="entry_cap")
                    finished = False
                    break
                files.append(row)
            if finished:
                scan.gunzip.finish()
        except _BudgetExceeded as exc:
            body.update(truncated=True, truncated_reason=exc.reason)
        except (_Corrupt, tarfile.TarError, EOFError) as exc:
            body.update(
                status="corrupt",
                truncated=True,
                truncated_reason="corrupt",
                detail=(
                    "the archive stops being a readable tar.gz here, so the files "
                    "below are the ones before that point and no more: "
                    + redact_detail(str(exc) or type(exc).__name__)
                ),
            )
        except ObjectAbsent:
            body.update(
                status="absent",
                detail="the archive disappeared while it was being read; it may have been reclaimed",
            )
            return body
        except ObjectUnreadable as exc:
            raise UpstreamUnavailable(
                "the checkpoint archive could not be read, so what it contains is not "
                "known: " + redact_detail(exc.reason)
            ) from None
        except _Changed as exc:
            raise UpstreamUnavailable(
                f"the checkpoint archive changed while it was being read ({exc}); "
                "nothing read from it can be trusted"
            ) from None
        finally:
            if scan is not None:
                body["scanned_bytes"] = scan.scanned_bytes
                body["inflated_bytes"] = scan.inflated_bytes

        body["files"] = files
        body["count"] = len(files)
        declared = manifest["file_count"]
        if body["status"] == "ok" and not body["truncated"] and isinstance(declared, int):
            body["file_count_agrees"] = (
                sum(1 for f in files if f["type"] != "dir") == declared
            )
        return body

    # -- 2. one file -----------------------------------------------------------
    def read_file(
        self,
        tenant_id: str,
        task_id: str,
        *,
        checkpoint_id: str,
        path: str,
        attempt_id: str | None = None,
        offset: int = 0,
        limit_bytes: int | None = None,
    ) -> dict[str, Any]:
        """One member's CONTENT, as text, redacted, in a bounded window.

        `GET /v1/tasks/{id}/checkpoints/{n}/files/{path}`. The response is the
        artifact-content route's shape field for field (`_artifact_row`), so
        `ArtifactViewer` renders it without a second code path: `status` is
        `ok`, `absent`, `unreadable` or `binary`, `content` is null for every
        non-`ok` status and `''` only for a real empty file, `truncated` and
        `next_offset` are on every window, and the window is cut on whitespace
        by the same `_align` -- a credential split across two pages matches no
        redaction rule in either half.

        Added: `checkpoint_id`, `path`, `content_type` (the allowlist's answer,
        or null when refused) and `member` (`type`, `mode`, `size`). `uri` is
        the ARCHIVE's: a member has no object of its own.

        Refusals, in order: another tenant's task is the 404 a missing task
        gets; a hostile `path` is a 422 before any object is read; a member
        that is a directory or a link is a 422 naming what it is; a member not
        in a fully read archive is a 404; one beyond the scan budget is a 413,
        because it may be there.
        """
        task, attempts_root = self._inspection._scoped(tenant_id, task_id)
        wanted = requested_path(path)
        if offset < 0:
            raise ValidationFailed("offset must not be negative")
        window = self.default_file_bytes if limit_bytes is None else limit_bytes
        if window < 1:
            raise ValidationFailed("limit_bytes must be at least 1")
        window = min(max(window, self.min_file_bytes), self.max_file_bytes)

        reader = self._inspection._reader()
        ref, objects = self._locate(
            task,
            attempts_root,
            checkpoint_id=checkpoint_id,
            attempt_id=attempt_id,
            reader=reader,
        )
        archive_uri = reader.uri(ref.archive_key)
        allowed = content_type_for(wanted)
        row = _artifact_row(
            task=task,
            entry={"name": wanted, "bytes": None, "uri": archive_uri},
            attempt_id=ref.attempt_id,
        )
        row.update(
            key=ref.archive_key,
            uri=archive_uri,
            checkpoint_id=ref.checkpoint_id,
            path=wanted,
            content_type=allowed,
            member=None,
        )

        if ref.archive_key not in objects:
            row.update(
                status="absent",
                detail=(
                    "this checkpoint has no archive object, so none of its files can be "
                    "read; it was reclaimed, or its upload never completed"
                ),
            )
            return row

        try:
            scan = self._open(reader, ref.archive_key)
            for member in scan.tar:
                shown, unsafe = member_path(member.name)
                # An undecodable name's escaped form carries a backslash that
                # `requested_path` already refused, so it cannot equal `wanted`;
                # skipped by name as well, so that stays true if either changes.
                if shown != wanted or unsafe or undecodable(member.name):
                    continue
                return self._serve_member(
                    scan, member, row, allowed=allowed, offset=offset, window=window
                )
            scan.gunzip.finish()
        except _BudgetExceeded as exc:
            raise ScanBudgetExceeded(
                f"{wanted!r} could not be read within this request's "
                f"{'inflate' if exc.reason == 'inflate_budget' else 'scan'} budget. It "
                "may well be in the archive; download the whole checkpoint to read it.",
                detail={"path": wanted, "reason": exc.reason},
            ) from None
        except (_Corrupt, tarfile.TarError, EOFError) as exc:
            raise ArchiveCorrupt(
                f"the archive stops being a readable tar.gz before {wanted!r} could be "
                "read, so that file's content is not known: "
                + redact_detail(str(exc) or type(exc).__name__),
                detail={"path": wanted},
            ) from None
        except ObjectAbsent:
            row.update(
                status="absent",
                detail="the archive disappeared while it was being read; it may have been reclaimed",
            )
            return row
        except ObjectUnreadable as exc:
            row.update(
                status="unreadable",
                detail=(
                    "the artifact store could not be read, so nothing may be concluded "
                    "about this file's content: " + redact_detail(exc.reason)
                ),
            )
            return row
        except _Changed as exc:
            raise UpstreamUnavailable(
                f"the checkpoint archive changed while it was being read ({exc}); "
                "nothing read from it can be trusted"
            ) from None

        raise NotFound(
            f"checkpoint {ref.checkpoint_id!r} of attempt {ref.attempt_id!r} has no "
            f"file {wanted!r}",
            detail={
                "task_id": task.id,
                "checkpoint_id": ref.checkpoint_id,
                "attempt_id": ref.attempt_id,
                "path": wanted,
            },
        )

    def _serve_member(
        self,
        scan: _Scan,
        member: tarfile.TarInfo,
        row: dict[str, Any],
        *,
        allowed: str | None,
        offset: int,
        window: int,
    ) -> dict[str, Any]:
        kind = member_type(member)
        size = int(member.size or 0)
        row["member"] = {"type": kind, "mode": int(member.mode or 0) & 0o7777, "size": size}
        if kind != "file":
            link = (
                redact(member.linkname or "").text
                if kind in ("symlink", "hardlink")
                else None
            )
            raise ValidationFailed(
                f"{row['path']!r} is a {kind} in this checkpoint, not a regular file"
                + (f"; it points at {link!r}" if link else ""),
                detail={"path": row["path"], "type": kind, "link": link},
            )
        row["artifact"]["bytes"] = size
        row["total_bytes"] = size

        if allowed is None:
            # Refused by NAME, before a byte of it is read.
            row.update(
                status="binary",
                offset=0,
                detail=(
                    "this file's type is not on the text allowlist, so its bytes are not "
                    "served here: nothing could scan them for a credential before they "
                    "left. Download the whole checkpoint to read it."
                ),
            )
            return row

        handle = scan.tar.extractfile(member)
        if handle is None:  # pragma: no cover - isreg() guarantees a handle
            raise ArchiveCorrupt(f"{row['path']!r} could not be opened inside the archive")
        head = handle.read(min(ARTIFACT_SNIFF_BYTES, size))
        if b"\x00" in head:
            row.update(
                status="binary",
                offset=0,
                detail=(
                    "this file is not text, so its bytes are not served here: nothing can "
                    "scan them for a credential before they leave, and a redaction that "
                    "cannot run is not a redaction. Download the whole checkpoint to read it."
                ),
            )
            return row

        # ONE BYTE OF OVERLAP, exactly as `read_artifact` takes it: the byte
        # before `offset` tells `_align` whether the window starts on a token
        # boundary or inside one.
        probe = 1 if offset > 0 else 0
        start = min(offset - probe, size)
        end = min(size, offset + window)
        data = _span(handle, head, start=start, end=end, chunk=self._chunk)
        chunk = ObjectSlice(key=row["key"], offset=start, data=data, total_bytes=size)

        if not chunk.data:
            row.update(status="ok", content="", offset=min(offset, size), truncated=False)
            return row

        previous = chunk.data[:probe]
        raw = chunk.data[probe:]
        begin = chunk.offset + probe
        raw, begin, stop, withheld = _align(
            raw,
            start=begin,
            previous=previous,
            at_eof=chunk.end >= chunk.total_bytes,
            read_end=chunk.end,
        )
        scrubbed = redact(raw.decode("utf-8", errors="replace"))
        complete = stop >= size
        row.update(
            status="ok",
            content=scrubbed.text,
            offset=begin,
            returned_bytes=len(raw),
            next_offset=None if complete else stop,
            truncated=not complete,
            redacted=scrubbed.any,
            redaction_count=scrubbed.count,
        )
        if withheld is not None:
            row["detail"] = withheld
        elif not complete:
            row["detail"] = (
                f"{stop} of {size} bytes are shown. This is a window, not the whole "
                "file -- continue from next_offset, or download the whole checkpoint."
            )
        return row

    # -- 3. the whole archive --------------------------------------------------
    def download(
        self,
        tenant_id: str,
        task_id: str,
        *,
        checkpoint_id: str,
        attempt_id: str | None = None,
    ) -> ArchiveDownload:
        """The archive object, byte for byte, as a stream of bounded windows.

        `GET /v1/tasks/{id}/checkpoints/{n}/content`. The FIRST window is read
        before anything is returned, so an absent archive is a 404 and an
        unreadable one a 503 -- never a 200 with an empty body, which a browser
        would save as a zero-byte `.tar.gz` that looks like a download.

        NO `Content-Length`, ON PURPOSE. swarm-api is uvicorn, which speaks
        HTTP/1 only, behind a Cloud Run port that is not h2c, and Cloud Run
        caps an HTTP/1 response at 32 MiB "if not using Transfer-Encoding:
        chunked or streaming" (docs.cloud.google.com/run/quotas). uvicorn
        chunks a response exactly when the application declares no length, so
        declaring one would put every archive over 32 MiB -- the ones a cut
        listing sends people here for -- over that cap. The size travels as
        `X-Checkpoint-Bytes` instead, where no server or proxy acts on it.

        A failure AFTER the first window cannot change a status that has
        already been sent. The stream then ends early, and that is still
        detectable: on the wire the chunked body never gets its terminating
        chunk, which a browser reports as a failed download, and a client
        counting bytes receives fewer than `X-Checkpoint-Bytes` promised (and,
        when the manifest has one, a body that fails `X-Checkpoint-Sha256`).
        The failure is logged here, redacted.

        THE REQUEST TIMEOUT BOUNDS THE SIZE THAT CAN BE DOWNLOADED, and it is
        not raised for this. swarm-api runs on the Cloud Run module's default
        `request_timeout` of 300 s (terraform/modules/cloud_run/variables.tf;
        terraform/infra/main.tf does not override it for swarm-api), and Cloud
        Run ends the request there -- mid-body, which the client sees as the
        cut above, never as a complete file. The time taken is at most
        `windows x (store time per window) + bytes / the client's throughput`:
        the next window is read only once the previous one has been handed to
        the socket, and each window is two sequential store calls
        (`get_blob`, then the ranged read). With an ASSUMED 50 ms per window --
        not measured -- the largest archive that finishes in 300 s is about
        2.9 GiB at 20 MiB/s to the client (so every checkpoint, whose cap is
        2 GiB), 1.2 GiB at 5 MiB/s, and 545 MiB at 2 MiB/s. Raising
        swarm-api's timeout (Cloud Run allows 60 minutes) is a service-wide
        change and is left to the owner; see redesign-v2.md S3.

        NOT REDACTED, and the headers say so -- see the module docstring. The
        manifest's digest travels as `X-Checkpoint-Sha256` when there is one
        and it is a digest, so the caller can verify what they received;
        `X-Checkpoint-Manifest` says whether there was a commit marker at all.
        """
        task, ref, objects, reader = self._resolve(
            tenant_id, task_id, checkpoint_id=checkpoint_id, attempt_id=attempt_id
        )
        key = ref.archive_key
        manifest = self._manifest(reader, ref, listed=ref.manifest_key in objects)
        try:
            first = reader.read_range(key, offset=0, length=self._chunk)
        except ObjectAbsent:
            raise NotFound(
                f"checkpoint {ref.checkpoint_id!r} of attempt {ref.attempt_id!r} has no "
                "archive in the bucket; it was reclaimed, or its upload never completed",
                detail={
                    "checkpoint_id": ref.checkpoint_id,
                    "attempt_id": ref.attempt_id,
                    "manifest": manifest["status"],
                },
            ) from None
        except ObjectUnreadable as exc:
            raise UpstreamUnavailable(
                "the checkpoint archive could not be read: " + redact_detail(exc.reason)
            ) from None

        total = first.total_bytes
        filename = f"{task.id}-{ref.attempt_id}-{ref.checkpoint_id}{_DOWNLOAD_SUFFIX}"
        headers = {
            # Every part of the name passed `safe_segment` ([A-Za-z0-9_.-]), so
            # nothing in it can close the quoted string or start a parameter.
            "Content-Disposition": f'attachment; filename="{filename}"',
            # NOT `Content-Length` -- see the docstring: declaring the length
            # is what makes uvicorn send the body unchunked, and Cloud Run
            # refuses an unchunked HTTP/1 response over 32 MiB.
            "X-Checkpoint-Bytes": str(total),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Swarm-Redaction": "not-applied",
            "X-Checkpoint-Manifest": str(manifest["status"]),
        }
        digest = manifest["archive_sha256"]
        if digest:
            headers["X-Checkpoint-Sha256"] = str(digest)

        def chunks() -> Iterator[bytes]:
            if first.data:
                yield first.data
            at = first.end
            while at < total:
                try:
                    piece = reader.read_range(key, offset=at, length=self._chunk)
                except (ObjectAbsent, ObjectUnreadable) as exc:
                    reason = redact_detail(
                        exc.reason if isinstance(exc, ObjectUnreadable) else "the object is gone"
                    )
                    log.error(
                        "checkpoint download of %s ended at byte %d of %d: %s",
                        key, at, total, reason,
                    )
                    raise DownloadAborted(
                        f"checkpoint download ended at byte {at} of {total}: {reason}"
                    ) from None
                if piece.total_bytes != total or not piece.data:
                    log.error(
                        "checkpoint download of %s ended at byte %d of %d: the object "
                        "changed size or returned nothing", key, at, total,
                    )
                    raise DownloadAborted(
                        f"checkpoint download ended at byte {at} of {total}: the "
                        "archive changed while it was being sent"
                    )
                yield piece.data
                at = piece.end

        return ArchiveDownload(
            chunks=chunks(), total_bytes=total, filename=filename, headers=headers
        )


def _span(handle: Any, head: bytes, *, start: int, end: int, chunk: int) -> bytes:
    """Bytes `[start, end)` of a member, read forward from where `head` stopped.

    `head` is the sniff already taken from the member's start; a stream-mode
    member cannot be re-read, so it is reused rather than read again. Skipping
    to `start` is done in bounded pieces and discarded, so a window late in a
    large file costs inflation, not memory.
    """
    if end <= start:
        return b""
    out = bytearray()
    at = len(head)
    if start < at:
        out += head[start:min(end, at)]
    while at < start:
        piece = handle.read(min(chunk, start - at))
        if not piece:
            return bytes(out)
        at += len(piece)
    while at < end:
        piece = handle.read(min(chunk, end - at))
        if not piece:
            break
        out += piece
        at += len(piece)
    return bytes(out)
