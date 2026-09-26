"""What an agent with no repository leaves in its working folder, uploaded (#184).

Found in the post-deploy QA of #184: `task_0e5b1f8b7bc1448fafdf`, a claude-code
task with no repository, wrote `answer.md` and `primes.txt` in its working
folder, and neither was uploaded. Only `$SWARM_ARTIFACTS_DIR` ever was
(`runners/base.py`: "files written here are uploaded when the attempt ends"),
so the task SUCCEEDED and its Artifacts tab showed the runner's logs and
nothing the agent made.

The owner decided three things on 2026-09-26, recorded on #184:

1. Every claude-code and codex prompt gets one line naming
   `$SWARM_ARTIFACTS_DIR`: files written there are uploaded and shown in
   Artifacts (`expected_outputs.deliverables_line`, appended by the one
   instruction builder both CLI runners share). It is information in the
   owner's words, not an order, because a repository task gets it too.
2. For a task with NO repository, the worker also uploads, when the attempt
   ends, the files the agent CREATED in its working folder. That is this
   module.
3. A repository task is unchanged: the diff or the pull request is its
   deliverable, plus the artifacts folder.

WHICH TASKS. A task with no repository URL, run by a runner whose child is a
provider's coding-agent CLI (`runners.streams.cli_agent_spec`: claude-code and
codex, the two runners the prompt line of decision 1 reaches). Not `mock`: it
writes `progress/` and `mock_state.json` into its working folder on every run,
and every smoke test would start uploading them. Not `generic`: its own module
calls it "the platform's escape valve for work that is not an agent". Not
`browser`: it drives Chromium in-process and writes its outputs into the
artifacts folder itself.

WHAT "CREATED" MEANS. A path under `work/` that the scan below finds when the
attempt ends and did not find just before the attempt's first runner started.
The worker takes that snapshot once per attempt, so an in-place restart (a
short provider wait, a reloaded credential) does not turn the first run's
files into "existing" ones. A file the agent only MODIFIED -- a staged input it
edited, say -- was there before, and is not uploaded.

A RESUMED ATTEMPT. A checkpoint restores `work/`, so the files an earlier
attempt's agent created are on disk before this attempt's runner starts. They
are still counted as created: the only files the platform itself puts in a
standalone task's `work/` are its control files (skipped by name) and the
inputs it stages (`metadata.input_from`, known by path), so anything else a
checkpoint brought back was written by this task's agent. Treating them as
"existing" would lose them for good, because the manifest a reader sees is
the FINAL attempt's, and a task that parked once would show only what its last
attempt wrote.

WHERE THEY GO: `workdir/<path under work>`, next to the artifacts, in the same
manifest (`result_summary.artifacts`), so the Artifacts tab lists and serves
them through the existing routes, with read-time redaction, and nothing new in
the API. The prefix is deliberate:

* a file of the same name in `$SWARM_ARTIFACTS_DIR` keeps its bare name, and
  the working-folder copy cannot replace it in the bucket;
* a dependant's `input_from` and the end-of-attempt check for
  `metadata.expected_outputs` both match by exact name, so a working-folder
  `scan-01.md` does NOT satisfy a later step that stages `scan-01.md`. The
  owner rejected exactly that on #149 (option (c)), and decision 2 is about
  what a reader sees, not what a dependant stages;
* the name says where the file was found.

If `$SWARM_ARTIFACTS_DIR` itself holds `workdir/<path>`, that file wins and
the working-folder copy is listed as not uploaded, with the reason.

CAPS: 50 files and 25 MiB in total per attempt (owner decision). Files are
taken shallowest first, then by path, so a top-level `answer.md` is not pushed
out by a generated tree. A file that does not fit is listed in the attempt's
result as "not uploaded: over cap" (`result_summary.workdir_outputs`), never
dropped silently.

NOT IN `artifacts_skipped` (#225 review). Every reader of that list calls its
names dropped at the artifacts folder's size cap: the Artifacts tab's "dropped
at the size cap", a dependant's "exceeded its artifact size cap"
(`inputs.py`). A working-folder file is dropped by a different cap, or for a
reason that is no cap at all, so it is listed only in
`workdir_outputs.not_uploaded`, with its reason, and the tab draws that list.

EVERY NAME IS IN THE LOG. `not_uploaded` holds the first 50 and
`not_uploaded_count` the whole number, because the summary is a Firestore
document with a 1 MiB limit. The worker's WARNING lines name every file it
did not upload, `LOG_BATCH` to a line, so a line stays well under Cloud
Logging's 256 KiB entry.

LISTED, NOT UPLOADED, with the reason (#225 review):

* **a name that is not UTF-8.** `os.walk` decodes such a name with
  `surrogateescape`, into a str holding lone surrogates. GCS names objects in
  UTF-8, a protobuf string field (so a Firestore write) cannot carry a lone
  surrogate, and the worker's log is UTF-8 on stdout. One such name in the
  summary made `finish` raise, and the next attempt restored the same file
  from the checkpoint and failed the same way. The name is listed as the
  bytes it was, `caf\\xe9.txt`, spelled as `ls -b` and the API's checkpoint
  listing (`swarm_api.checkpoint_content.displayable`) spell it. That is a
  display, and it is never used as a key.
* **a name too long to be an object's**: GCS allows 1,024 bytes of UTF-8, and
  the attempt's prefix comes first. It is listed with its name cut to
  `SHOWN_NAME_CHARS`, so 50 of them cannot take the summary near 1 MiB.
* **a core dump**: `core` or `core.<pid>`, wherever it is. A program the agent
  built and ran crashes with the working folder as its cwd, and its core holds
  its memory, environment block included, which inherited the CLI's
  credential. Never a deliverable, and listed so a reader learns something
  crashed.
* **a file holding a registered secret the worker could not redact.** For
  `$SWARM_ARTIFACTS_DIR` the trade is to upload a binary file as-is and say
  so: the agent chose to deliver it, and corrupting it to take a key out is
  worse. That trade does not carry over to a net under files nobody chose to
  deliver. A working-folder file whose raw bytes hold a registered value, or
  that could not be scanned at all, stays in the pod.

SKIPPED, by design and without a listing:

* every name that starts with a dot, folder or file. The owner named dot
  folders; dot FILES are skipped too, because the CLI runners set HOME to the
  working folder, and the CLIs write their own state there (`.claude.json`,
  `.claude/`, `.codex/`, `.npm/`). None of it is the agent's deliverable, and
  some of it describes the account the agent ran as;
* `node_modules` and `__pycache__`, and caches: a folder whose name ends in
  "cache" or "caches", or that holds a `CACHEDIR.TAG` (the Cache Directory
  Tagging convention pip, cargo and pytest follow);
* a Python virtual environment: `.venv` (a dot folder) and any folder holding
  `pyvenv.cfg`, whatever it is called;
* the control files the worker places in `work/` (`input.json`,
  `result.json`, `quota.json`, `credential.json`) and the worker's own
  `./artifacts` link, whose target is uploaded already.

SYMLINKS ARE NEVER FOLLOWED. The scan does not descend into a linked folder
and does not read a linked file, and it counts what it passed over. The copy
then opens each path component by component with `O_NOFOLLOW`, relative to
the folder above it (`open_without_following`), so a path swapped for a link
between the scan and the read -- an orphaned agent process can still be
running -- is refused rather than followed out of the workspace.
"""

from __future__ import annotations

import errno
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterable, Mapping

#: The manifest prefix every working-folder file is uploaded under. See the
#: module docstring for why it is not the bare path.
PREFIX = "workdir"

#: The owner's caps, 2026-09-26 on #184: at most this many files, and this many
#: bytes in total, per attempt. Defaults for `WorkerConfig.max_workdir_output_*`.
MAX_FILES = 50
MAX_BYTES = 25 * 1024 * 1024

#: Folder names skipped wherever they appear. `.venv` is here for the reader;
#: the dot rule already skips it.
SKIPPED_DIRECTORY_NAMES = frozenset({"node_modules", "__pycache__", ".venv"})

#: A folder holding one of these is a cache or a virtual environment,
#: whatever it is called.
SKIPPED_DIRECTORY_MARKERS = ("CACHEDIR.TAG", "pyvenv.cfg")

#: GCS's limit on an object's name, in bytes of UTF-8. The whole key counts,
#: the attempt's `tenants/<t>/tasks/<id>/attempts/<a>/artifacts/` prefix too.
MAX_OBJECT_NAME_BYTES = 1024

#: How much of a name that could not be uploaded is kept in the summary. A
#: Linux path runs to 4,096 bytes; 50 of those in `not_uploaded` would be a
#: fifth of the 1 MiB Firestore allows the task document.
SHOWN_NAME_CHARS = 256

#: Names per WARNING line when the worker logs every file it did not upload:
#: at most about 100 KiB of names per line, under Cloud Logging's 256 KiB.
LOG_BATCH = 100

#: The reasons a created file is listed as not uploaded. "over cap" is the
#: owner's wording ("not uploaded: over cap").
OVER_CAP = "over cap"
NAME_TAKEN = "name taken by a file in $SWARM_ARTIFACTS_DIR"
SYMLINK_REFUSED = "refused: a symlink or not a regular file when it was read"
UNREADABLE = "unreadable"
UPLOAD_FAILED = "upload failed"
NOT_UTF8 = "name is not valid UTF-8"
NAME_TOO_LONG = f"name is longer than a storage object's {MAX_OBJECT_NAME_BYTES} bytes"
CORE_DUMP = "a core dump"
HOLDS_SECRET = "holds a registered secret that could not be redacted"
UNEXAMINED = "could be neither redacted nor scanned for a registered secret"

_CORE_DUMP_NAME = re.compile(r"core(\.[0-9]+)?")


def manifest_name(relative: str) -> str:
    """The manifest name of `work/<relative>`: `workdir/<relative>`."""
    return f"{PREFIX}/{relative}"


def storable(name: str) -> bool:
    """True when `name` is valid UTF-8: an object name, a Firestore string, a log field.

    False for a str holding a lone surrogate, which is what `os.walk` makes of
    a file name whose bytes are not UTF-8.
    """
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def displayable(text: str) -> str:
    """`text` with every lone surrogate escaped, so any UTF-8 reader can carry it.

    A byte `surrogateescape` carried is shown as that byte (`\\xe9`), any other
    lone surrogate as `\\udxxx`. The same spelling as
    `swarm_api.checkpoint_content.displayable`, the API's own listing of the
    names a checkpoint holds; restated here because the worker does not import
    the API, and held to it by a parity test
    (`tests/unit/worker/test_standalone_outputs.py`), because a restated rule
    is the one that drifts. The result is for reading, never an address.
    """
    if storable(text):
        return text
    out = []
    for char in text:
        code = ord(char)
        if 0xDC80 <= code <= 0xDCFF:
            out.append(f"\\x{code - 0xDC00:02x}")
        elif 0xD800 <= code <= 0xDFFF:
            out.append(f"\\u{code:04x}")
        else:
            out.append(char)
    return "".join(out)


def shown(name: str) -> str:
    """`name` as the summary lists a file it could not upload: escaped, and cut short.

    Cut with three ASCII dots, so what is left is still plainly a prefix.
    """
    text = displayable(name)
    if len(text) > SHOWN_NAME_CHARS:
        return text[:SHOWN_NAME_CHARS] + "..."
    return text


def is_core_dump(relative: str) -> bool:
    """True for `core` or `core.<pid>` anywhere under the working folder."""
    return _CORE_DUMP_NAME.fullmatch(relative.rsplit("/", 1)[-1]) is not None


def is_hidden(name: str) -> bool:
    return name.startswith(".")


def skipped_directory(path: Path) -> bool:
    """True for a folder whose contents are never uploaded (see the docstring).

    `path` is a real folder, not a link: the caller has already passed over
    links. A marker that cannot be checked counts as absent, so a folder the
    worker cannot stat is walked, and the files in it are judged one by one.
    """
    name = path.name
    lowered = name.lower()
    if is_hidden(name) or name in SKIPPED_DIRECTORY_NAMES:
        return True
    if lowered.endswith("cache") or lowered.endswith("caches"):
        return True
    for marker in SKIPPED_DIRECTORY_MARKERS:
        try:
            if os.path.lexists(path / marker):
                return True
        except OSError:
            continue
    return False


@dataclass(frozen=True)
class Scan:
    """What a scan of `work/` found.

    `files` maps each candidate's path under `work/` (POSIX, relative) to its
    size as `lstat` saw it. `symlinks` are the links it passed over without
    following, by the same kind of path.
    """

    files: Mapping[str, int]
    symlinks: tuple[str, ...] = ()


def scan(work: Path, *, reserved: Iterable[str] = ()) -> Scan:
    """Every regular file under `work/` that could be a deliverable.

    `reserved` names entries at the TOP of `work/` that are the worker's own
    and are skipped whatever they are: the control files, and the
    `./artifacts` link when the worker made it. Never follows a link, never
    raises for one file it could not stat.
    """
    root = Path(work)
    top = frozenset(reserved)
    files: dict[str, int] = {}
    links: list[str] = []
    # `os.walk` lists the folder it is GIVEN even when that is a link, so a
    # working folder an agent replaced with a link to `/` would be walked as
    # `/`. Nothing is uploaded from it then, and the caller says so.
    if root.is_symlink() or not root.is_dir():
        return Scan(files={})
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        at_top = here == root
        keep: list[str] = []
        for name in sorted(dirnames):
            if is_hidden(name) or (at_top and name in top):
                continue
            path = here / name
            if path.is_symlink():
                links.append(path.relative_to(root).as_posix())
                continue
            if skipped_directory(path):
                continue
            keep.append(name)
        # Pruned in place, which is how `os.walk` is told not to descend.
        dirnames[:] = keep
        for name in sorted(filenames):
            if is_hidden(name) or (at_top and name in top):
                continue
            path = here / name
            try:
                info = os.lstat(path)
            except OSError:
                continue
            relative = path.relative_to(root).as_posix()
            if stat.S_ISLNK(info.st_mode):
                links.append(relative)
                continue
            if not stat.S_ISREG(info.st_mode):
                # A FIFO, a socket or a device: never a deliverable, and
                # opening a FIFO blocks.
                continue
            files[relative] = info.st_size
    return Scan(files=files, symlinks=tuple(sorted(links)))


def created(found: Scan, baseline: Iterable[str]) -> dict[str, int]:
    """The files in `found` that are not in `baseline`, with their sizes."""
    before = frozenset(baseline)
    return {path: size for path, size in found.files.items() if path not in before}


def upload_order(files: Mapping[str, int]) -> list[tuple[str, int]]:
    """Shallowest first, then by path: the order the caps are applied in."""
    return sorted(files.items(), key=lambda item: (item[0].count("/"), item[0]))


class Refused(OSError):
    """A path component was a symlink, or the file was not a regular file."""


class OverCap(Exception):
    """The file was larger than the bytes left under the cap when it was read."""


def open_without_following(work: Path, relative: str) -> int:
    """A read-only descriptor for `work/<relative>`, with no symlink followed.

    Each folder on the way is opened with `O_NOFOLLOW | O_DIRECTORY` relative
    to the one above it, and the file itself with `O_NOFOLLOW | O_NONBLOCK`,
    so a link anywhere in the path raises `Refused` instead of being followed,
    and a FIFO put in a file's place cannot block the worker. The result is
    checked to be a regular file.
    """
    parts = relative.split("/")
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise Refused(errno.EINVAL, f"not a path inside the working folder: {relative!r}")
    cloexec = getattr(os, "O_CLOEXEC", 0)
    folder_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | cloexec
    try:
        # The working folder itself too: an agent can replace it with a link.
        fd = os.open(os.fspath(work), folder_flags)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise Refused(exc.errno, "the working folder is a symlink") from exc
        raise
    try:
        for part in parts[:-1]:
            try:
                deeper = os.open(part, folder_flags, dir_fd=fd)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise Refused(exc.errno, f"{relative!r}: {part!r} is a symlink") from exc
                raise
            os.close(fd)
            fd = deeper
        try:
            leaf = os.open(
                parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | cloexec, dir_fd=fd
            )
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise Refused(exc.errno, f"{relative!r} is a symlink") from exc
            raise
    finally:
        os.close(fd)
    try:
        info = os.fstat(leaf)
    except OSError:
        os.close(leaf)
        raise
    if not stat.S_ISREG(info.st_mode):
        os.close(leaf)
        raise Refused(errno.EINVAL, f"{relative!r} is not a regular file")
    return leaf


def copy_without_following(work: Path, relative: str, destination: Path, *, limit: int) -> int:
    """Copy `work/<relative>` to `destination`; return the bytes copied.

    Raises `Refused` for a symlink anywhere in the path or a file that is not
    regular, and `OverCap` when more than `limit` bytes are there to copy. The
    partial copy is removed in either case. `destination` is the WORKER's own
    scratch file, never a path the agent chose.
    """
    fd = open_without_following(work, relative)
    try:
        source = os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise
    with source:
        # Created fresh and never through a link: whatever is at the scratch
        # path is removed first, and O_EXCL | O_NOFOLLOW refuses anything that
        # appears there in between.
        destination.unlink(missing_ok=True)
        out = os.open(
            os.fspath(destination),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        try:
            with os.fdopen(out, "wb") as sink:
                return _copy(source, sink, limit=limit)
        except BaseException:
            destination.unlink(missing_ok=True)
            raise


def _copy(source: BinaryIO, sink: BinaryIO, *, limit: int) -> int:
    copied = 0
    while True:
        chunk = source.read(1024 * 1024)
        if not chunk:
            return copied
        copied += len(chunk)
        if copied > limit:
            raise OverCap(f"more than {limit} bytes")
        sink.write(chunk)
