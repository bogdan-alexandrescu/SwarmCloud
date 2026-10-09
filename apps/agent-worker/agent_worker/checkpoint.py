"""Mandatory periodic checkpointing.

Cloud Run's ephemeral (second-generation) disk is a Preview feature, and turning
it on **disables live migration**. That single fact is why this module is not
best-effort: without live migration, an infrastructure event during a two-hour
agent run destroys the sandbox outright. A checkpoint every
`checkpoint_interval_seconds` is what turns that from "the attempt is lost" into
"the attempt loses a couple of minutes".

Layout, derived entirely from identifiers the control plane already has:

    tenants/<tenant>/tasks/<task>/attempts/<attempt>/checkpoints/<id>/archive.tar.gz
    tenants/<tenant>/tasks/<task>/attempts/<attempt>/checkpoints/<id>/manifest.json

The manifest is uploaded **last, and only after the archive**, so its presence is
the commit marker: a checkpoint interrupted halfway leaves an orphan archive that
no restore will ever select, rather than a manifest pointing at a truncated one.

A restore reads a checkpoint written by an EARLIER attempt of the task,
because a resume is by definition a new attempt with a new id. It restores
only the one that attempt recorded -- `task.latest_checkpoint`, bound to the
attempt document that lists it and its archive digest -- and never on a
task's first attempt (#347, `Worker._recorded_checkpoint`). It does not list
the prefix to pick one: every agent of the tenant can write under
`tenants/<tenant>/`, so "the newest manifest under this task's prefix" is
whatever another step of the tenant last put there, `.claude/` hooks
included.

**A checkpoint is only ever restored into the task it belongs to.** The pointer
in `task.latest_checkpoint` is a Firestore field, and Firestore has no
document-level IAM, so it must be treated as data rather than as an
instruction: `find_by_uri` resolves it only inside THIS task's own prefix, and
`restore` re-checks the manifest's `tenant_id` and `task_id` before it unpacks a
byte. The GCS IAM condition on `tenants/<tenant>/` denies the cross-tenant case
in production; these checks are what make the same true under
`LOCAL_ARTIFACT_ROOT`, and what stop a pointer to another TASK of the same
tenant from delivering the wrong working tree.
"""

from __future__ import annotations

import errno
import hashlib
import io
import json
import os
import re
import shutil
import stat
import struct
import sys
import tarfile
import tempfile
import time
import zlib
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .errors import CheckpointError
from .objectstore import ObjectStore
from .workspace import Workspace, walk_tree


class RestoreResourceError(CheckpointError):
    """A restore that failed for want of LOCAL room, not for anything in the archive.

    ENOSPC on the memory-backed workspace, EIO, out of file handles or memory
    (`_LOCAL_RESOURCE_ERRNOS`). Every other `CheckpointError` out of `restore`
    is a refusal of the checkpoint, and the worker starts clean on it (#346);
    this one says nothing against the checkpoint, so the attempt fails as it
    always did and a retry resumes the same one. Starting clean on it would
    let the attempt's first checkpoint move `latest_checkpoint` off the
    earlier attempt's work for good.
    """


#: What makes an extraction failure the worker's, not the archive's.
_LOCAL_RESOURCE_ERRNOS = frozenset(
    {errno.ENOSPC, errno.EDQUOT, errno.EIO, errno.EMFILE, errno.ENFILE, errno.ENOMEM}
)

#: How many folders deep a checkpoint archives. The archive walk holds one
#: open descriptor per level (it opens each child relative to its parent so
#: that no link is followed, #227), so an unbounded depth is an unbounded
#: number of descriptors: a tree 1,000 levels deep met a 1,024-descriptor
#: limit before it met anything else. A folder this deep is NOT archived, nor
#: anything below it, and the checkpoint is still written (owner decision,
#: 2026-09-28): invariant 8 says checkpointing is mandatory, and a refusal
#: would fail every checkpoint for the rest of the attempt. How many folders
#: were left out is logged, so the loss is never silent.
CHECKPOINT_MAX_DEPTH = 512

#: How many bytes of FILE CONTENT a checkpoint archives, as a multiple of its
#: compressed cap (`max_checkpoint_bytes`, 2 GiB by default, so 16 GiB). The
#: compressed cap is enforced while the archive is written (`_CappedFile`),
#: but a highly compressible tree -- gigabytes of zeros, a log of one repeated
#: line -- stays under it while gzip still reads and compresses every byte,
#: on the heartbeat's clock (#227). Source text compresses to about a quarter
#: under gzip -9, so a real tree under the compressed cap is well under this
#: one; and `work/` is memory-backed tmpfs, so 16 GiB of content is half the
#: memory of the largest resource class (32 GiB) before the agent uses any.
#: Past it the checkpoint is refused, as past the compressed cap.
CHECKPOINT_EXPANSION_RATIO = 8

#: How many bytes of file content a RESTORE writes, summed over every layer
#: of the chain, as a multiple of the compressed cap (32 GiB by default). A
#: small archive of zeros expands without bound, and it expands into `work/`,
#: which is memory: this is what stops a planted or corrupt chain from
#: filling the container's memory before the agent starts (#227). Twice the
#: per-archive bound, because a chain is a full archive and the incrementals
#: on it, and it is rebased once the incrementals weigh as much as the full
#: (`_create`); every layer this platform wrote is under
#: `CHECKPOINT_EXPANSION_RATIO` on its own. It is the largest resource
#: class's whole memory, so a chain past it could not be restored anyway.
RESTORE_EXPANSION_RATIO = 2 * CHECKPOINT_EXPANSION_RATIO

#: The largest `seq` a restored manifest may carry for this attempt's ids to
#: continue from it (#174). The manifest is data read from a bucket, and its
#: `seq` becomes every later checkpoint's id and so part of its object key:
#: a planted seq of 1,100 digits made every key longer than GCS's 1,024
#: bytes, failing every later checkpoint (#227). A billion checkpoints is one
#: a minute for nineteen centuries, so no real chain of attempts reaches it,
#: and its ten digits keep the key short. Past it the ids start again at 1.
RESTORED_SEQ_MAX = 10**9

def _require_data_filter(module: Any = tarfile) -> None:
    """Refuse to run on a Python whose `tarfile` has no extraction filters.

    `restore` extracts through `tarfile.data_filter` (`_restore_filter`),
    which CPython has from 3.11.4. Without it the restore would fail on the
    first resume with a TypeError nobody connects to the interpreter, or --
    worse, a change that dropped the filter argument to "fix" that would
    extract with no filter at all. So the worker does not start: this runs
    at import, and the image build imports this module (and asserts the
    version itself, `images/agent-runtime-base/Dockerfile`).
    """
    if not callable(getattr(module, "data_filter", None)) or not hasattr(module, "FilterError"):
        raise ImportError(
            "agent_worker.checkpoint needs tarfile.data_filter (Python 3.11.4 or later) "
            f"to restore a checkpoint safely; this is Python {sys.version.split()[0]}"
        )


_require_data_filter()

ARCHIVE_NAME = "archive.tar.gz"
MANIFEST_NAME = "manifest.json"

#: Tool caches under the agent's HOME, which is `work/` (`Workspace.child_env`),
#: left out of every checkpoint (#286). Since #261 an agent runs the offline
#: tests in its container, so uv, pip, npm, pnpm and yarn fill these. They are
#: REBUILDABLE -- the next `uv run` or `npm ci` fetches them again -- and they
#: are the bulk of the bytes, which lengthens every checkpoint and counts
#: toward its cap. And they hold absolute links (uv's build environment's
#: `.cache/uv/builds-v0/.tmpX/bin/python -> /usr/local/bin/python3.11`), which
#: a restore can never make.
#:
#: Each entry is a path relative to HOME, except `**/node_modules`: a
#: `node_modules` directory ANYWHERE in `work/`, the repository checkout
#: (`work/repo`) included. The owner's decision of 2026-09-28 (PR #288): it is
#: rebuildable by `npm ci` and never the agent's work, wherever it sits. The
#: other entries are HOME's, so a `.cache` inside the checkout is kept. What
#: stays in regardless: the CLIs' session transcripts (`.claude/`, `.codex/`),
#: which are not caches.
TOOL_CACHES: tuple[str, ...] = (
    ".cache",
    ".npm",
    ".local/share/uv",
    ".local/share/pnpm",
    ".yarn/cache",
    "**/node_modules",
)


def _is_tool_cache(rel: str) -> bool:
    """True when `rel`, relative to `work/`, is one of `TOOL_CACHES`."""
    for entry in TOOL_CACHES:
        if entry.startswith("**/"):
            if rel.rsplit("/", 1)[-1] == entry[3:]:
                return True
        elif rel == entry:
            return True
    return False


#: Dependency and build directories left out of every checkpoint, wherever in
#: `work/` they sit (#637, owner decision 2026-10-05). The history analysis of
#: 2026-10-05 measured 247 GB of checkpoints uploaded, a median of 66 MB every
#: 120 s, and only 1.25% of those bytes ever restored: virtualenvs, bytecode,
#: bundles, test builds and coverage reports were most of it. Each is rebuilt
#: by the command that made it (docs/checkpointing.md lists them):
#:
#:   .venv        `uv sync` (or `uv run`, which syncs first)
#:   __pycache__  rewritten by the interpreter on the next import
#:   dist         the project's build (`npm run build`, `uv build`)
#:   .test-build  the UI's typecheck build (`npm run typecheck`)
#:   coverage     the next test run with coverage on
#:
#: A DIRECTORY ONLY, AND NOT ONE A GIT CHECKOUT TRACKS. A file named `dist`
#: is kept, and so is a `dist/` with even one tracked file under it
#: (`_BuildDirFilter`): left out, the restore would hand the agent a checkout
#: in which those committed files read as deleted, and a publish could carry
#: the deletion. When the checkout's index cannot be read, the directory is
#: kept: a few megabytes too many is the cheap side of that mistake.
BUILD_DIRS: tuple[str, ...] = (".venv", "__pycache__", "dist", ".test-build", "coverage")

#: How many incremental archives follow a full one before the next full one.
#: A restore downloads and replays the whole chain, so the chain is bounded:
#: at the 120 s interval this is a full archive at least every 48 minutes of
#: steady change. A chain is also rebased once its incremental archives
#: together outweigh the full archive they sit on (`CheckpointManager._create`).
CHECKPOINT_CHAIN_MAX = 24

#: The longest a PERIODIC checkpoint waits while the tree has not changed:
#: 2 -> 4 -> 8 -> 10 minutes at the default 120 s interval (#637, owner
#: decision 2026-10-05). A tree that changes resets it to the base interval.
#: This is the wait to the next LOOK, so work done just after an unchanged
#: look is exposed for up to this long before the next one takes it; the
#: final, park, cancellation and interruption checkpoints are never skipped
#: (invariant 8). `WorkerConfig.checkpoint_max_interval_seconds` sets it.
CHECKPOINT_BACKOFF_CAP_SECONDS = 600

#: How an INCREMENTAL archive names its base and its deletions: the gzip
#: header's comment (RFC 1952 FCOMMENT), this prefix and then JSON,
#: `{"base": {"checkpoint_id", "archive_sha256"}, "deleted": [paths]}`. It is
#: in the archive's own bytes, so the archive's digest -- which the attempt
#: document binds (`Worker._recorded_checkpoint`) -- binds it too: the base
#: cannot be swapped by rewriting a manifest or an object in the bucket. Not a
#: tar member, so it can never collide with a file of the agent's, and not a
#: pax global header, which `tarfile` refuses to read when no member follows
#: it (an incremental archive of an unchanged tree has none). Every gzip
#: reader skips the comment. A full archive has none.
CHAIN_COMMENT_PREFIX = b"swarm-checkpoint "

#: The longest chain a restore replays. Far past what `create` writes
#: (`CHECKPOINT_CHAIN_MAX`); it bounds what a forged header could ask for.
RESTORE_CHAIN_MAX = 200

#: An entry whose mtime or ctime is this close to (or after) the start of the
#: walk that read it is RACY: a write in the same clock tick, after the stat,
#: leaves size, mtime, ctime and inode as they were, and would never be seen
#: as a change. Its signature is recorded so that it never matches, and the
#: next checkpoint archives it again -- git's racy-clean rule, for the same
#: reason. File times come from the kernel's coarse clock, a tick or so
#: behind `time.time_ns()`; a second is far past that on tmpfs.
RACY_WINDOW_NS = 1_000_000_000

#: A checkpoint id as `create` writes it; a base named in a header is used to
#: build a key only when it is one.
_CHECKPOINT_ID_RE = re.compile(r"ckpt-[0-9]{5,}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")

#: The largest git index `_BuildDirFilter` reads. Past it the checkout's build
#: directories are kept rather than parsed for.
_GIT_INDEX_MAX_BYTES = 256 * 1024 * 1024


def _git_index_paths(git_dir: Path) -> list[bytes] | None:
    """The paths a git index tracks; [] when there is no index; None when unreadable.

    Read by hand rather than by running git: the checkout is the agent's, and
    git run in it reads the agent's config (`core.fsmonitor` runs a command).
    Versions 2, 3 and 4 of the index format, SHA-1 or SHA-256 object names.
    Nothing read here leaves the worker: it decides only whether a build
    directory is archived. The last component is opened `O_NOFOLLOW`.
    """
    try:
        fd = os.open(git_dir / "index", os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return []
    except OSError:
        return None
    try:
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                return None
            data = handle.read(_GIT_INDEX_MAX_BYTES + 1)
    except OSError:
        return None
    if len(data) > _GIT_INDEX_MAX_BYTES or len(data) < 12 or data[:4] != b"DIRC":
        return None
    hash_len = 20
    try:
        cfd = os.open(git_dir / "config", os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(cfd, "rb") as handle:
            if re.search(rb"objectformat\s*=\s*sha256", handle.read(64 * 1024), re.I):
                hash_len = 32
    except OSError:
        pass
    version, count = struct.unpack(">II", data[4:12])
    if version not in (2, 3, 4):
        return None
    paths: list[bytes] = []
    previous = b""
    pos = 12
    try:
        for _ in range(count):
            start = pos
            pos += 40 + hash_len
            (flags,) = struct.unpack(">H", data[pos : pos + 2])
            pos += 2
            if version >= 3 and flags & 0x4000:
                pos += 2
            if version == 4:
                byte = data[pos]
                pos += 1
                strip = byte & 0x7F
                while byte & 0x80:
                    byte = data[pos]
                    pos += 1
                    strip = ((strip + 1) << 7) | (byte & 0x7F)
                end = data.index(b"\0", pos)
                if strip > len(previous):
                    return None
                name = previous[: len(previous) - strip] + data[pos:end]
                pos = end + 1
            else:
                end = data.index(b"\0", pos)
                name = data[pos:end]
                pos = start + ((end - start + 8) & ~7)
            if not name or name.startswith(b"/") or pos > len(data):
                return None
            paths.append(name)
            previous = name
    except (IndexError, ValueError, struct.error):
        return None
    return paths


class _BuildDirFilter:
    """Decides, per checkpoint, whether a directory named in `BUILD_DIRS` is left out.

    Out unless a git checkout enclosing it tracks a file under it. The nearest
    enclosing checkout is the nearest ancestor (`work/` included) holding a
    real `.git` directory; its index is read once per checkpoint.
    """

    def __init__(self, source: Path) -> None:
        self._source = source
        self._tracked: dict[str, frozenset[str] | None] = {}

    def excludes(self, rel: str) -> bool:
        parts = rel.split("/")
        if parts[-1] not in BUILD_DIRS:
            return False
        for depth in range(len(parts) - 1, -1, -1):
            root = "/".join(parts[:depth])
            git_dir = self._source / root / ".git" if root else self._source / ".git"
            try:
                if not stat.S_ISDIR(os.lstat(git_dir).st_mode):
                    return False  # a `.git` file or link: no index to read here
            except OSError:
                continue
            tracked = self._tracked_dirs(root, git_dir)
            if tracked is None:
                return False  # an index that cannot be read keeps the directory
            return "/".join(parts[depth:]) not in tracked
        return True

    def _tracked_dirs(self, root: str, git_dir: Path) -> frozenset[str] | None:
        if root not in self._tracked:
            paths = _git_index_paths(git_dir)
            if paths is None:
                self._tracked[root] = None
            else:
                dirs: set[str] = set()
                for raw in paths:
                    parts = raw.decode("utf-8", "surrogateescape").rstrip("/").split("/")
                    for index, part in enumerate(parts):
                        if part in BUILD_DIRS:
                            dirs.add("/".join(parts[: index + 1]))
                self._tracked[root] = frozenset(dirs)
        return self._tracked[root]


class CheckpointBackoff:
    """The wait to the next PERIODIC checkpoint: doubled while unchanged, reset on change.

    Pure, so the interval rule is tested without a clock. The cap never
    shortens the base: a profile whose interval is already past the cap does
    not back off at all.
    """

    def __init__(
        self, *, base_seconds: float, cap_seconds: float = CHECKPOINT_BACKOFF_CAP_SECONDS
    ) -> None:
        self._base = base_seconds
        self._cap = max(base_seconds, cap_seconds)
        self.current = base_seconds

    def next_interval(self, *, changed: bool) -> float:
        self.current = self._base if changed else min(self.current * 2, self._cap)
        return self.current


def _signature(st: os.stat_result, linkname: str | None = None) -> tuple[Any, ...]:
    """What `create` compares to decide that an entry changed since the last checkpoint.

    A file's inode and ctime as well as its size and mtime: an editor's atomic
    save is a new inode, and a write that restores the mtime still moves the
    ctime. A directory changes only with its mode; what is in it is compared
    entry by entry.
    """
    if stat.S_ISDIR(st.st_mode):
        return ("d", stat.S_IMODE(st.st_mode))
    if stat.S_ISLNK(st.st_mode):
        return ("l", linkname)
    return ("f", st.st_mode, st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino)


@dataclass(frozen=True)
class CheckpointRecord:
    checkpoint_id: str
    seq: int
    task_id: str
    attempt_id: str
    tenant_id: str
    generation: int
    created_at: str
    archive_key: str
    manifest_key: str
    archive_bytes: int
    archive_sha256: str
    file_count: int
    uri: str
    #: The commit the attempt's clone landed on, as the WORKER knew it, or
    #: `gitops.EMPTY_CLONE_BASE` for an empty repository; None when there was
    #: no clone, or the manifest predates this field. It is here, and not only
    #: in `work/.swarm/clone-base`, because that file is in the agent's working
    #: directory: the publish decides which commits it replaces from this base,
    #: and an agent that could move it could keep its own commits out of the
    #: fold. The manifest is written by the worker, outside the archived tree.
    clone_base: str | None = None
    #: The checkpoint this one's archive holds the changes since, and that
    #: archive's digest; None for a full archive (#637). For the reader only:
    #: a restore takes the base from the archive's own header (`PAX_BASE`),
    #: which the attempt document's digest binds, and never from here.
    base_checkpoint_id: str | None = None
    base_archive_sha256: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CheckpointRecord":
        return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})

    @property
    def sort_key(self) -> tuple[str, int]:
        return (self.created_at, self.seq)


def checkpoint_prefix(*, tenant_id: str, task_id: str, attempt_id: str, checkpoint_id: str) -> str:
    return (
        f"tenants/{tenant_id}/tasks/{task_id}/attempts/{attempt_id}"
        f"/checkpoints/{checkpoint_id}"
    )


def attempts_prefix(*, tenant_id: str, task_id: str) -> str:
    return f"tenants/{tenant_id}/tasks/{task_id}/attempts/"


#: How many skipped links a restore names in its log line; the rest are
#: counted. A tree can hold thousands of such links (a virtualenv per tool),
#: and one log line naming them all would be the largest line the worker
#: writes, for no more information than the first twenty and a count give.
SKIPPED_LINKS_NAMED = 20


def _archive_parts(name: str) -> tuple[str, ...] | None:
    """`name`, a path relative to the archive root, as its components.

    None when it cannot be one: absolute, empty, or holding `..`. This
    platform's archiver writes plain relative names and never a `..`, so a name
    with one is evidence of a tampered archive -- and a `..` after a symlink
    component resolves on the filesystem to somewhere a lexical reading of the
    name does not predict.
    """
    if name.startswith("/") or os.path.isabs(name):
        return None
    parts = tuple(part for part in name.split("/") if part not in ("", "."))
    if not parts or ".." in parts:
        return None
    return parts


def _link_leaves(parts: tuple[str, ...], linkname: str) -> bool:
    """True when a symlink at `parts` to `linkname` leaves the root, read lexically.

    Only the first of two checks: `tarfile.data_filter` repeats it at
    extraction against the real filesystem, where a link through an earlier
    link (`d -> .`, then `c -> d/..`) is resolved as the kernel would.

    IT OVER-SKIPS, KNOWINGLY (#227). The reading is lexical, so `..` undoes
    the component before it whatever that component is: `x -> d/../../y` is
    judged to leave, even when `d` is a link to a directory two levels down
    (`d -> sub/inner`) and the kernel would resolve it to `y`, inside. Such a
    link is skipped and named in the restore's warning rather than made.
    Accepted: a skip loses one link, never a byte outside `work/`, and
    `data_filter` re-checks every link this lets through, so being exact here
    would buy nothing the filter does not already guarantee.
    """
    if not linkname or os.path.isabs(linkname):
        return True
    stack = list(parts[:-1])
    for part in linkname.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not stack:
                return True
            stack.pop()
        else:
            stack.append(part)
    return False


def _safe_members(
    tar: tarfile.TarFile,
    destination: Path,
    *,
    on_skip: Callable[[str, str], None] | None = None,
) -> list[tarfile.TarInfo]:
    """The members a restore extracts, or `CheckpointError` refusing the archive.

    A checkpoint archive is written by this platform, but it contains a tenant's
    working tree, and a tenant's agent can create any file it likes inside it --
    including a symlink to /etc. Path traversal is checked on the way out, not
    trusted on the way in. This is the first pass, over the whole member list
    before a byte is written; `restore` then extracts through
    `tarfile.data_filter` (`_restore_filter`), which checks each member again
    against the filesystem as it is at that moment.

    A SYMLINK THAT ESCAPES IS SKIPPED, NOT A REASON TO REFUSE THE ARCHIVE (#286).
    It is never created -- that is the check -- but one such link used to fail
    the whole restore, and with it every later resume of the task: uv's build
    environment leaves `bin/python -> /usr/local/bin/python3.11` behind as a
    matter of course. `on_skip(name, linkname)` is called for each one so the
    caller can name it.

    AN ARCHIVE INCONSISTENT WITH ITSELF IS REFUSED WHOLE (the PR #288 review).
    This platform's archiver follows no link and writes each name once, so
    none of these comes from an agent's ordinary tree:

    * a member whose own path is absolute, holds `..`, or appears twice;
    * a member whose path passes through a SYMLINK member, skipped or not --
      `d -> .`, `c -> d/..`, then `c/x` writes `x` beside `work/`;
    * a HARD link whose target is not an earlier regular-file member this
      pass accepted. The target is named relative to the archive root, so
      `../private/secret.txt` names a file outside, and a regular member of
      the link's name then writes into that file. And a hard link to a member
      that was skipped or is not a file is exactly what tarfile's fallback
      turns into a copy of the TARGET member (the CVE-2025-4330 class):
      refused here, whatever the Python version, so nothing rests on it.

    Sockets, FIFOs and devices are left out, as before: an agent that left
    one behind gets a workspace without it rather than a failed resume.
    `destination` is not read; the checks are over the archive's own names.
    """
    del destination  # every check here is relative to the archive root
    members = tar.getmembers()
    all_parts: list[tuple[str, ...]] = []
    symlinks: set[tuple[str, ...]] = set()
    for member in members:
        parts = _archive_parts(member.name)
        if parts is None:
            raise CheckpointError(
                f"checkpoint archive holds a path outside the workspace: {member.name}"
            )
        all_parts.append(parts)
        if member.issym():
            symlinks.add(parts)

    seen: set[tuple[str, ...]] = set()
    files: set[tuple[str, ...]] = set()
    accepted: list[tarfile.TarInfo] = []
    for member, parts in zip(members, all_parts):
        name = "/".join(parts)
        if parts in seen:
            raise CheckpointError(f"checkpoint archive holds {name} more than once")
        seen.add(parts)
        for depth in range(1, len(parts)):
            if parts[:depth] in symlinks:
                raise CheckpointError(
                    f"checkpoint archive writes {name} through its own link "
                    f"{'/'.join(parts[:depth])}"
                )
        if member.islnk():
            # A trailing slash names the target as a DIRECTORY, which no
            # hard link this archiver writes can have; `_archive_parts` drops
            # the empty part, so it would pass as the file, and the link then
            # fails at extraction with an OSError rather than a refusal.
            target = _archive_parts(member.linkname)
            if member.linkname.endswith("/") or target is None or target not in files:
                raise CheckpointError(
                    f"checkpoint archive holds a hard link, {name}, that is not to "
                    f"an earlier file of its own"
                )
            accepted.append(member)
        elif member.issym():
            if _link_leaves(parts, member.linkname):
                if on_skip is not None:
                    on_skip(name, member.linkname)
                continue
            accepted.append(member)
        elif member.isfile():
            files.add(parts)
            accepted.append(member)
        elif member.isdir():
            accepted.append(member)
    return accepted


def _restore_filter(
    on_skip: Callable[[str, str], None],
) -> Callable[[tarfile.TarInfo, str], tarfile.TarInfo | None]:
    """`tarfile.data_filter`, with an escaping SYMLINK skipped rather than fatal.

    The filter runs as each member is extracted, so it resolves a link against
    the links already made: `c -> d/..` after `d -> .` is caught here where the
    lexical check in `_safe_members` passes it. Any other refusal -- a path
    outside, a hard link outside, a special file -- raises, and `restore`
    refuses the archive.
    """

    def restore_filter(member: tarfile.TarInfo, path: str) -> tarfile.TarInfo | None:
        try:
            return tarfile.data_filter(member, path)
        except (tarfile.AbsoluteLinkError, tarfile.LinkOutsideDestinationError):
            if not member.issym():
                raise
            on_skip(member.name, member.linkname)
            return None

    return restore_filter


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tarinfo(arcname: str, st: os.stat_result, kind: bytes) -> tarfile.TarInfo:
    """A directory or symlink member from a stat already taken without following."""
    info = tarfile.TarInfo(arcname)
    info.type = kind
    info.mode = stat.S_IMODE(st.st_mode)
    info.uid = st.st_uid
    info.gid = st.st_gid
    info.mtime = int(st.st_mtime)
    return info


class _CappedFile(io.FileIO):
    """The archive file, refusing to grow past `cap` bytes WHILE it is written.

    The cap was checked on the finished archive, so a tree far over it was
    compressed whole before being refused -- minutes of gzip on a large tree,
    for a checkpoint that was never going to be uploaded (PR #288 review).
    The first write past the cap raises `CheckpointError` out of the walk;
    what the archive's close writes after that is discarded, so the close does
    not raise over the refusal.
    """

    def __init__(self, path: Path, cap: int) -> None:
        super().__init__(path, "wb")
        self._cap = cap
        self._size = 0
        self._tripped = False

    def write(self, data: Any) -> int:
        length = memoryview(data).nbytes
        if self._tripped:
            return length
        if self._size + length > self._cap:
            self._tripped = True
            raise CheckpointError(
                f"the checkpoint passed its {self._cap} byte cap while being written; "
                f"refusing it"
            )
        written = super().write(data)
        self._size += written or 0
        return written or 0


class _GzipWriter(io.RawIOBase):
    """gzip (RFC 1952) over `raw`, with an optional header comment.

    What `tarfile`'s `w:gz` wrote, at the same level 9, plus the FCOMMENT
    field the gzip module cannot write: `CHAIN_COMMENT_PREFIX`. `tell` is
    the uncompressed offset, which is all `tarfile` asks of it.
    """

    def __init__(self, raw: Any, *, comment: bytes | None) -> None:
        super().__init__()
        self._raw = raw
        self._deflate = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS)
        self._crc = 0
        self._size = 0
        flags = 0x10 if comment is not None else 0
        # magic, deflate, flags, mtime 0 (as `tarfile` wrote no name or
        # time that meant anything), "maximum compression", unknown OS.
        raw.write(b"\x1f\x8b\x08" + bytes([flags]) + b"\0\0\0\0\x02\xff")
        if comment is not None:
            if b"\0" in comment:
                raise CheckpointError("a checkpoint's gzip comment cannot hold a NUL")
            raw.write(comment + b"\0")

    def writable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._size

    def write(self, data: Any) -> int:
        view = memoryview(data).cast("B")
        self._crc = zlib.crc32(view, self._crc)
        self._size += len(view)
        self._raw.write(self._deflate.compress(view))
        return len(view)

    def close(self) -> None:
        if not self.closed:
            try:
                self._raw.write(self._deflate.flush())
                self._raw.write(struct.pack("<II", self._crc, self._size & 0xFFFFFFFF))
            finally:
                super().close()


def _gzip_comment(path: Path, *, limit: int = 64 * 1024 * 1024) -> bytes | None:
    """The FCOMMENT of the gzip file at `path`, or None when it has none."""
    with path.open("rb") as handle:
        head = handle.read(10)
        if len(head) < 10 or head[:3] != b"\x1f\x8b\x08":
            raise CheckpointError("a checkpoint archive is not a gzip stream")
        flags = head[3]
        if not flags & 0x10:
            return None
        if flags & 0x04:  # FEXTRA
            (length,) = struct.unpack("<H", handle.read(2))
            handle.read(length)
        if flags & 0x08:  # FNAME
            while handle.read(1) not in (b"\0", b""):
                pass
        comment = bytearray()
        while True:
            byte = handle.read(1)
            if byte in (b"\0", b""):
                return bytes(comment)
            comment += byte
            if len(comment) > limit:
                raise CheckpointError("a checkpoint archive's gzip comment is too long")


def _chain_header(path: Path, checkpoint_id: str) -> tuple[tuple[str, str] | None, list[str]]:
    """The base `(checkpoint_id, archive_sha256)` and deleted paths of the archive at `path`.

    `(None, [])` for a full archive. Anything not exactly what `create`
    writes refuses the archive: the header decides which object is fetched
    next and which paths are removed.
    """
    comment = _gzip_comment(path)
    if comment is None:
        return None, []
    refused = CheckpointError(
        f"checkpoint {checkpoint_id} names its base in a form the restore refuses"
    )
    if not comment.startswith(CHAIN_COMMENT_PREFIX):
        raise refused
    try:
        data = json.loads(comment[len(CHAIN_COMMENT_PREFIX) :].decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        raise refused from None
    base = data.get("base") if isinstance(data, dict) else None
    deleted = data.get("deleted") if isinstance(data, dict) else None
    if (
        not isinstance(base, dict)
        or not isinstance(base.get("checkpoint_id"), str)
        or not _CHECKPOINT_ID_RE.fullmatch(base["checkpoint_id"])
        or not isinstance(base.get("archive_sha256"), str)
        or not _SHA256_RE.fullmatch(base["archive_sha256"])
        or not isinstance(deleted, list)
        or not all(isinstance(rel, str) and _archive_parts(rel) is not None for rel in deleted)
    ):
        raise refused
    return (base["checkpoint_id"], base["archive_sha256"]), deleted


def _deleted_since(
    previous: dict[str, tuple[Any, ...]], index: dict[str, tuple[Any, ...]]
) -> list[str]:
    """What `previous` held that `index` does not, or holds as another kind.

    Outermost only: a removed directory is named, and nothing under it. A
    path whose kind changed (a file that became a directory, a directory that
    became a link) is named too, so the restore clears it before the new
    entry is extracted in its place.
    """
    gone = sorted(
        rel for rel, sig in previous.items() if rel not in index or index[rel][0] != sig[0]
    )
    named: set[str] = set()
    out: list[str] = []
    for rel in gone:
        parts = rel.split("/")
        if any("/".join(parts[:depth]) in named for depth in range(1, len(parts))):
            continue
        named.add(rel)
        out.append(rel)
    return out


class CheckpointManager:
    """Creates, uploads, discovers and restores workspace checkpoints."""

    def __init__(
        self,
        *,
        store: ObjectStore,
        tenant_id: str,
        task_id: str,
        attempt_id: str,
        generation: int,
        logger: Any,
        max_bytes: int = 2 * 1024 * 1024 * 1024,
    ) -> None:
        self._store = store
        self._tenant_id = tenant_id
        self._task_id = task_id
        self._attempt_id = attempt_id
        self._generation = generation
        self._log = logger
        self._max_bytes = max_bytes
        #: The id sequence: the last `ckpt-NNNNN` this attempt used, continued
        #: from the checkpoint a resumed attempt restored (#174).
        self._seq = 0
        #: How many checkpoints THIS attempt committed. Not the same number
        #: once a restore has continued the ids; see `seq`.
        self._written = 0
        #: Written into every manifest `create` uploads. The lifecycle sets it
        #: once the clone has landed, or once it has been read back from the
        #: checkpoint a resumed attempt restored, so it carries forward.
        self.clone_base: str | None = None
        #: THE CHAIN (#637). What the last committed checkpoint of this
        #: attempt archived, entry by entry (`_signature`), and its id and
        #: digest: the next one archives only what differs. None until this
        #: attempt has committed a checkpoint, so every attempt's first one is
        #: full and a chain never crosses an attempt -- a restore, which
        #: starts a new attempt, starts a new chain.
        self._index: dict[str, tuple[Any, ...]] | None = None
        self._base: tuple[str, str] | None = None
        self._chain_length = 0
        self._chain_bytes = 0
        self._full_bytes = 0
        #: True when the last `create_if_changed` found nothing to write.
        self.last_unchanged = False

    @property
    def seq(self) -> int:
        """Checkpoints THIS attempt committed -- not the id sequence.

        The heartbeat sends this as "checkpoints" (`lifecycle._progress`), and
        #174 asked that it keep meaning what it meant before ids continued
        across attempts: an attempt that restored ckpt-00007 and has written
        one reports 1, not 8. The id is on the record (`CheckpointRecord.seq`).
        """
        return self._written

    # -- create ------------------------------------------------------------
    def create(self, ws: Workspace, *, label: str = "periodic") -> CheckpointRecord:
        """Archive `work/`, upload it, then commit with the manifest. Always writes.

        Every checkpoint but the periodic one comes through here -- final,
        park, cancellation, interruption, outage -- and is written whether or
        not the tree changed since the last (invariant 8): an unchanged tree
        writes an incremental archive with nothing in it, which is a few
        hundred bytes, and the checkpoint the next attempt restores is the
        one this exit recorded.
        """
        record = self._create(ws, label=label, only_if_changed=False, before_upload=None)
        if record is None:  # `only_if_changed=False` always writes
            raise CheckpointError(f"checkpoint ({label}) wrote nothing")
        return record

    def create_if_changed(
        self,
        ws: Workspace,
        *,
        label: str = "periodic",
        before_upload: Callable[[], None] | None = None,
    ) -> CheckpointRecord | None:
        """`create`, or None -- nothing uploaded -- when nothing changed since the last.

        For the PERIODIC checkpoint only (#637): the tree already equals the
        checkpoint this attempt last committed, so writing it again restores
        nothing more. `last_unchanged` says which it was, and the lifecycle
        backs the interval off on it (`CheckpointBackoff`). `before_upload`
        runs once the walk has found a change and before the first byte is
        uploaded; whatever it raises propagates, and nothing is uploaded.
        """
        return self._create(ws, label=label, only_if_changed=True, before_upload=before_upload)

    def reset_chain(self) -> None:
        """Make the next checkpoint a full archive."""
        self._index = None
        self._base = None

    def _create(
        self,
        ws: Workspace,
        *,
        label: str,
        only_if_changed: bool,
        before_upload: Callable[[], None] | None,
    ) -> CheckpointRecord | None:
        # THE ARTIFACTS LINK IS LEFT OUT, knowingly (#149). `work/artifacts` is
        # a link the worker makes to `artifacts/` (`workspace.link_artifacts`).
        # Archived, it broke every resume: it resolves outside `work/`, and
        # `_safe_members` refused an archive holding such a link (it skips
        # one now, #286, but a link restore can never make has no business in
        # the archive). It names this attempt's directory besides, and a
        # resumed attempt makes its own.
        #
        # What is BEHIND the link is not archived either: `_write_archive`
        # follows no link, so it meets the link as one entry and never its
        # contents. Those files are the artifacts, uploaded on their own.
        #
        # The checkout's link too (#226): with a repository attached the agent
        # starts in `work/repo`, so the lifecycle links `work/repo/artifacts`
        # as well, and it breaks a resume in exactly the same way.
        #
        # AND SO IS `input.json` (the PR #229 review). It is the task's whole
        # input, written by `AgentLifecycle._prepare`, and archived it was
        # served back out of every checkpoint -- as bytes by the archive
        # download, and by the file view through the text rules only -- while
        # every task route serves the input masked. Nothing needs it restored:
        # `_prepare` writes it from the task document at every attempt, AFTER
        # the restore (STEP 4) and before the runner starts, so a resumed
        # attempt reads the one it wrote, never an archived one.
        #
        # The tool caches under HOME (`TOOL_CACHES`, #286) are left out by
        # `_write_archive` itself, which matches them as it walks.
        skip = frozenset(
            path.relative_to(ws.work).as_posix()
            for path in (ws.artifacts_link(), ws.artifacts_link(ws.checkout()))
            if ws.is_artifacts_link(path)
        ) | {ws.input_path.relative_to(ws.work).as_posix()}
        self.last_unchanged = False
        build_dirs = _BuildDirFilter(ws.work)
        racy_after = time.time_ns() - RACY_WINDOW_NS
        previous = self._index
        base = self._base
        if previous is not None and (
            self._chain_length >= CHECKPOINT_CHAIN_MAX or self._chain_bytes >= self._full_bytes
        ):
            # Rebase: a restore replays the whole chain, so it is kept short
            # and never heavier than the full archive under it.
            previous, base = None, None
        index: dict[str, tuple[Any, ...]] = {}
        deleted: list[str] = []
        if previous is not None:
            # Two walks. The first only stats, so an unchanged tree costs no
            # archive at all, and the deletions are known before the archive
            # opens -- they travel in its gzip header, where the digest
            # covers them. The second archives what differs from `previous`.
            index = self._scan(ws.work, skip=skip, build_dirs=build_dirs)
            deleted = _deleted_since(previous, index)
            unchanged = not deleted and index == previous
            if only_if_changed and unchanged:
                self.last_unchanged = True
                self._log.info(
                    "checkpoint unchanged; nothing written", label=label, entries=len(index)
                )
                return None
        if before_upload is not None:
            before_upload()

        self._seq += 1
        checkpoint_id = f"ckpt-{self._seq:05d}"
        prefix = checkpoint_prefix(
            tenant_id=self._tenant_id,
            task_id=self._task_id,
            attempt_id=self._attempt_id,
            checkpoint_id=checkpoint_id,
        )
        archive_key = f"{prefix}/{ARCHIVE_NAME}"
        manifest_key = f"{prefix}/{MANIFEST_NAME}"
        comment: bytes | None = None
        if base is not None:
            comment = CHAIN_COMMENT_PREFIX + json.dumps(
                {
                    "base": {"checkpoint_id": base[0], "archive_sha256": base[1]},
                    "deleted": deleted,
                },
                sort_keys=True,
            ).encode("ascii")

        with tempfile.TemporaryDirectory(prefix="swarm-ckpt-") as tmpdir:
            archive_path = Path(tmpdir) / ARCHIVE_NAME
            archived: dict[str, tuple[Any, ...]] = {}
            file_count = self._write_archive(
                ws.work,
                archive_path,
                skip=skip,
                build_dirs=build_dirs,
                previous=previous if base is not None else None,
                index=archived,
                comment=comment,
            )
            # What the next checkpoint compares against: the first walk's
            # reading, overwritten by what the second actually archived. An
            # entry that vanished between the walks stays in, so the next
            # checkpoint names it deleted rather than forgetting it.
            index.update(archived)
            for rel, signature in index.items():
                if signature[0] == "f" and max(signature[3], signature[4]) >= racy_after:
                    index[rel] = signature + ("racy",)
            size = archive_path.stat().st_size
            if size > self._max_bytes:
                raise CheckpointError(
                    f"checkpoint {checkpoint_id} is {size} bytes, over the "
                    f"{self._max_bytes} byte cap; refusing to upload"
                )
            digest = _sha256(archive_path)
            self._store.upload_file(archive_key, archive_path, content_type="application/gzip")

            record = CheckpointRecord(
                checkpoint_id=checkpoint_id,
                seq=self._seq,
                task_id=self._task_id,
                attempt_id=self._attempt_id,
                tenant_id=self._tenant_id,
                generation=self._generation,
                created_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                archive_key=archive_key,
                manifest_key=manifest_key,
                archive_bytes=size,
                archive_sha256=digest,
                file_count=file_count,
                uri=self._store.uri(f"{prefix}/"),
                clone_base=self.clone_base,
                base_checkpoint_id=base[0] if base is not None else None,
                base_archive_sha256=base[1] if base is not None else None,
            )
            # Commit marker. Nothing before this line is discoverable.
            self._store.upload_bytes(
                manifest_key,
                json.dumps(
                    {
                        **asdict(record),
                        "label": label,
                        "kind": "incremental" if base is not None else "full",
                        "deleted": len(deleted),
                    },
                    indent=2,
                ).encode("utf-8"),
                content_type="application/json",
            )
            self._written += 1

        # The chain moves only once the manifest is up: a checkpoint that
        # failed anywhere before leaves the next one based on the last that
        # committed.
        if base is None:
            self._chain_length, self._chain_bytes, self._full_bytes = 0, 0, size
        else:
            self._chain_length += 1
            self._chain_bytes += size
        self._index = index
        self._base = (checkpoint_id, digest)

        self._log.info(
            "checkpoint written",
            checkpoint_id=checkpoint_id,
            bytes=size,
            files=file_count,
            label=label,
            kind="incremental" if base is not None else "full",
            base=base[0] if base is not None else None,
            deleted=len(deleted),
        )
        return record

    def _scan(
        self, source: Path, *, skip: frozenset[str], build_dirs: _BuildDirFilter
    ) -> dict[str, tuple[Any, ...]]:
        """Every entry `_write_archive` would archive, by `_signature`; nothing is read.

        The same walk, by descriptor and following no link, with no archive:
        the first of an incremental checkpoint's two walks.
        """
        index: dict[str, tuple[Any, ...]] = {}
        self._walk(source, None, skip=skip, build_dirs=build_dirs, previous=None, index=index)
        return index

    def _write_archive(
        self,
        source: Path,
        archive_path: Path,
        *,
        skip: frozenset[str] = frozenset(),
        build_dirs: _BuildDirFilter | None = None,
        previous: dict[str, tuple[Any, ...]] | None = None,
        index: dict[str, tuple[Any, ...]] | None = None,
        comment: bytes | None = None,
    ) -> int:
        """Archive the tree under `source`, following NO link, the root included.

        Returns the members that are not directories, which is how the API's
        listing counts them (`checkpoint_content`). `skip` holds names relative
        to `source`; a skipped directory is not descended into. Neither is a
        tool cache (`TOOL_CACHES`), which is never archived.

        A LINKED ROOT IS REFUSED (#227). `work/` is in the agent's reach, and
        `Path.rglob` -- what this walked before -- lists the TARGET of a linked
        root while declining links below it, so an agent that swapped `work/`
        for a link had whatever it pointed at uploaded as the newest checkpoint.
        Refused rather than archived empty: an empty checkpoint would commit as
        the newest and a resume would restore nothing, where a refusal leaves
        the last real checkpoint the newest. The lifecycle logs the refusal as
        CHECKPOINT FAILED with this error's text.

        BY DIRECTORY DESCRIPTOR, NOT BY PATH. A check that `work/` is not a link
        followed by a walk of the path is the same race #227's other boxes
        describe: the link swapped in between is followed. So the root is
        opened with `O_NOFOLLOW`, and every entry below it is looked up, opened
        or read relative to its parent's descriptor, the last component again
        `O_NOFOLLOW`. An entry that vanishes or changes type mid-walk is left
        out rather than failing the checkpoint; a live tree is never archived
        atomically, and the next checkpoint takes it.

        An explicit stack, not recursion: open descriptors are bounded by the
        tree's depth, and a deep tree cannot raise `RecursionError` here. The
        depth itself is bounded by `CHECKPOINT_MAX_DEPTH`: below it nothing is
        archived, the count left out is logged, and the checkpoint is written.

        INCREMENTAL (#637). With `previous`, an entry whose `_signature`
        equals the one recorded there is walked and not archived; `comment`
        goes into the gzip header (`CHAIN_COMMENT_PREFIX`). `index` collects the signature of every entry walked.
        A directory in `BUILD_DIRS` that `build_dirs` excludes is neither
        archived nor entered.
        """
        if build_dirs is None:
            build_dirs = _BuildDirFilter(source)
        if index is None:
            index = {}
        with _CappedFile(archive_path, self._max_bytes) as raw, _GzipWriter(
            raw, comment=comment
        ) as gz, tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar:
            return self._walk(
                source, tar, skip=skip, build_dirs=build_dirs, previous=previous, index=index
            )

    def _walk(
        self,
        source: Path,
        tar: tarfile.TarFile | None,
        *,
        skip: frozenset[str],
        build_dirs: _BuildDirFilter,
        previous: dict[str, tuple[Any, ...]] | None,
        index: dict[str, tuple[Any, ...]],
    ) -> int:
        """`_write_archive`'s walk, into `tar`, or only into `index` when `tar` is None.

        AN UNREADABLE ENTRY IS LEFT OUT, NOT A REASON TO FAIL (#227). A folder
        or file the worker may not open (`EACCES`/`EPERM`: an agent's
        `chmod 000`) failed the whole checkpoint, and every later one, where
        `Path.rglob` used to pass it by. It is skipped, the walk goes on, and
        one warning names the skipped paths and counts them, as `too_deep`.
        """
        try:
            root_fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError as exc:
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise CheckpointError(
                    f"refusing to checkpoint {source}: it is a link or not a directory "
                    f"({exc.strerror}), and archiving it would archive whatever it "
                    f"points at"
                ) from exc
            # Any other failure is not the link case, and saying it was would
            # send whoever reads the log looking for a link that is not there.
            code = errno.errorcode.get(exc.errno or 0, str(exc.errno))
            raise CheckpointError(
                f"refusing to checkpoint {source}: it could not be opened "
                f"({code}: {exc.strerror})"
            ) from exc
        count = 0
        too_deep: list[str] = []
        unreadable: list[str] = []
        #: File content archived so far, against `CHECKPOINT_EXPANSION_RATIO`.
        expanded = [0]
        stack: list[tuple[int, str, list[str]]] = []
        try:
            stack.append((root_fd, "", sorted(os.listdir(root_fd), reverse=True)))
        except BaseException:
            os.close(root_fd)
            raise
        try:
            while stack:
                dir_fd, prefix, names = stack[-1]
                if not names:
                    stack.pop()
                    os.close(dir_fd)
                    continue
                name = names.pop()  # reverse-sorted, so this is the smallest
                rel = prefix + name
                if rel in skip or _is_tool_cache(rel):
                    continue
                try:
                    # `too_deep` by keyword, so the entry's own path stays
                    # the last positional argument (#288's test records it).
                    count += self._add_entry(
                        tar,
                        stack,
                        dir_fd,
                        name,
                        rel,
                        too_deep=too_deep,
                        build_dirs=build_dirs,
                        previous=previous,
                        index=index,
                        expanded=expanded,
                        expanded_cap=self._max_bytes * CHECKPOINT_EXPANSION_RATIO,
                    )
                except (FileNotFoundError, NotADirectoryError):
                    continue  # gone, or no longer a directory
                except OSError as exc:
                    if exc.errno == errno.ELOOP:
                        continue  # became a link after it was looked at
                    if exc.errno in (errno.EACCES, errno.EPERM):
                        unreadable.append(rel)
                        continue
                    raise
        finally:
            for dir_fd, _, _ in stack:
                os.close(dir_fd)
        if too_deep and tar is not None:
            # Counted by path, with no descriptor held (`walk_tree`), and no
            # link followed: a count, not an archive, so a race here costs a
            # wrong number in a log line and nothing else.
            folders = 0
            for rel in too_deep:
                for _ in walk_tree(source / rel):
                    folders += 1
            self._log.warning(
                "the working tree is deeper than a checkpoint archives; the "
                "folders below the bound were not archived",
                max_depth=CHECKPOINT_MAX_DEPTH,
                folders_not_archived=folders,
            )
        if unreadable and tar is not None:
            # The path only, never the OS error's text, and through the
            # worker's scrub: a path is the agent's text, as a link's is
            # (`_log_skipped_links`).
            scrub: Callable[[str], str] = getattr(self._log, "scrub_text", None) or (
                lambda text: text
            )
            self._log.warning(
                "entries of the working tree could not be read; they were not archived",
                unreadable=len(unreadable),
                named=[scrub(rel) for rel in unreadable[:SKIPPED_LINKS_NAMED]],
                not_named=max(0, len(unreadable) - SKIPPED_LINKS_NAMED),
            )
        return count

    @staticmethod
    def _add_entry(
        tar: tarfile.TarFile | None,
        stack: list[tuple[int, str, list[str]]],
        dir_fd: int,
        name: str,
        rel: str,
        too_deep: list[str],
        build_dirs: _BuildDirFilter | None = None,
        previous: dict[str, tuple[Any, ...]] | None = None,
        index: dict[str, tuple[Any, ...]] | None = None,
        expanded: list[int] | None = None,
        expanded_cap: int | None = None,
    ) -> int:
        """Add one entry of the directory open as `dir_fd`; 1 when it counts.

        A directory is pushed onto `stack` open, for the walk to descend into
        -- unless it is `CHECKPOINT_MAX_DEPTH` levels down, when it is named in
        `too_deep` and neither archived nor entered, or it is a build
        directory `build_dirs` leaves out (`BUILD_DIRS`).

        The entry's `_signature` goes into `index`. It is archived only when
        `tar` is given and the signature differs from the one in `previous`
        (always, with no `previous`); a directory is entered either way, since
        what changed may be below it.

        `expanded[0]` sums the size of every file archived; past
        `expanded_cap` the checkpoint is refused before the file's bytes are
        read (`CHECKPOINT_EXPANSION_RATIO`).

        A HARD LINK IS ARCHIVED AS THE FILE IT NAMES, KNOWINGLY (#227). A link
        whose other name is outside `work/` -- or is `input.json`, which is
        never archived -- is archived as a regular file of its content under
        the name it has here: exactly what the agent copying that file into
        `work/` would produce, and the agent can do that with any file its uid
        reads. Accepted: it publishes nothing a copy would not, and refusing
        it would leave the copy archived anyway. Only a second name of an
        inode this walk already archived becomes a hard-link member.
        """
        st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)

        def changed(signature: tuple[Any, ...]) -> bool:
            if index is not None:
                index[rel] = signature
            return tar is not None and (previous is None or previous.get(rel) != signature)

        if stat.S_ISLNK(st.st_mode):
            linkname = os.readlink(name, dir_fd=dir_fd)
            if not changed(_signature(st, linkname)) or tar is None:
                return 0
            info = _tarinfo(rel, st, tarfile.SYMTYPE)
            info.linkname = linkname
            tar.addfile(info)
            return 1
        if stat.S_ISDIR(st.st_mode):
            # `stack` holds the root and every open ancestor: its length is
            # this directory's depth. See `CHECKPOINT_MAX_DEPTH`.
            if len(stack) >= CHECKPOINT_MAX_DEPTH:
                too_deep.append(rel)
                return 0
            if build_dirs is not None and build_dirs.excludes(rel):
                return 0
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
            try:
                child_st = os.fstat(child)
                names = sorted(os.listdir(child), reverse=True)
            except BaseException:
                os.close(child)
                raise
            stack.append((child, rel + "/", names))
            if changed(_signature(child_st)) and tar is not None:
                tar.addfile(_tarinfo(rel, child_st, tarfile.DIRTYPE))
            return 0
        if not stat.S_ISREG(st.st_mode):
            return 0  # sockets and FIFOs are not state worth carrying
        if not changed(_signature(st)) or tar is None:
            return 0
        # O_NONBLOCK so a FIFO swapped in after the stat cannot hang the open;
        # the type is checked again on what was actually opened.
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
        with os.fdopen(fd, "rb") as handle:
            # `gettarinfo` over the open file: its fstat, and tarfile's own
            # hard-link bookkeeping, as `tar.add` had.
            info = tar.gettarinfo(arcname=rel, fileobj=handle)
            if info is None or not (info.isreg() or info.islnk()):
                return 0
            if expanded is not None and expanded_cap is not None and info.isreg():
                expanded[0] += info.size
                if expanded[0] > expanded_cap:
                    raise CheckpointError(
                        f"the checkpoint's files passed {expanded_cap} bytes before "
                        f"compression; refusing it"
                    )
            tar.addfile(info, handle if info.isreg() else None)
        return 1

    # -- ownership ---------------------------------------------------------
    @property
    def own_prefix(self) -> str:
        """The only prefix this manager will ever read a checkpoint from."""
        return attempts_prefix(tenant_id=self._tenant_id, task_id=self._task_id)

    def _owns(self, record: CheckpointRecord) -> bool:
        """True when the manifest describes a checkpoint of THIS task.

        The manifest was found in a bucket; that is not evidence of who wrote
        it. Both identifiers are compared, and the archive key is required to
        sit under this task's prefix, so a manifest that names the right task
        but points its archive somewhere else is refused too.
        """
        return (
            record.tenant_id == self._tenant_id
            and record.task_id == self._task_id
            and record.archive_key.startswith(self.own_prefix)
            and record.manifest_key.startswith(self.own_prefix)
        )

    def _accept(self, record: CheckpointRecord, key: str) -> CheckpointRecord | None:
        if self._owns(record):
            return record
        self._log.error(
            "refusing a checkpoint manifest that belongs to another task",
            key=key,
            manifest_tenant=record.tenant_id,
            manifest_task=record.task_id,
            expected_tenant=self._tenant_id,
            expected_task=self._task_id,
        )
        return None

    # -- discover ----------------------------------------------------------
    def find_latest(self) -> CheckpointRecord | None:
        """Newest committed checkpoint for this TASK, across every attempt.

        NEVER USED TO CHOOSE WHAT TO RESTORE (#347). It lists the prefix, and
        every agent of the tenant can write there, so what it finds is not
        evidence of what this task's attempts wrote. The worker restores only
        what `Worker._recorded_checkpoint` accepts. This stays for the tests
        and tooling that read back what a run wrote.
        """
        prefix = self.own_prefix
        manifests = [k for k in self._store.list_keys(prefix) if k.endswith(f"/{MANIFEST_NAME}")]
        best: CheckpointRecord | None = None
        for key in manifests:
            try:
                data = json.loads(self._store.download_bytes(key).decode("utf-8"))
                record = CheckpointRecord.from_dict(data)
            except Exception as exc:  # a half-written manifest must not block a resume
                self._log.warning("ignoring unreadable checkpoint manifest", key=key, error=str(exc))
                continue
            if self._accept(record, key) is None:
                continue
            if best is None or record.sort_key > best.sort_key:
                best = record
        return best

    def find_by_uri(self, uri: str) -> CheckpointRecord | None:
        """Resolve the pointer the control plane keeps in `task.latest_checkpoint`.

        The pointer is a Firestore field and therefore untrusted input. It is
        resolved ONLY within this task's own prefix: a pointer that does not name
        a checkpoint of this task -- another tenant's, or another task of the
        same tenant's -- resolves to nothing, and the attempt starts from an
        empty workspace. There is no fallback to `find_latest` (#347): a
        checkpoint no attempt of this task recorded is not restored.
        """
        if not uri:
            return None
        key = uri.split("://", 1)[-1]
        # Every object this platform writes lives under `tenants/`, so slicing
        # from there turns any flavour of URI -- gs://bucket/..., file:///abs/...
        # -- into the bucket-relative key without special-casing the store.
        marker = key.find(self.own_prefix)
        if marker > 0:
            key = key[marker:]
        elif key.startswith(self._store.bucket + "/"):
            key = key[len(self._store.bucket) + 1 :]
        key = key.rstrip("/") + f"/{MANIFEST_NAME}"
        if not key.startswith(self.own_prefix):
            self._log.error(
                "refusing a checkpoint pointer outside this task's own prefix",
                pointer=uri,
                expected_prefix=self.own_prefix,
            )
            return None
        try:
            data = json.loads(self._store.download_bytes(key).decode("utf-8"))
            record = CheckpointRecord.from_dict(data)
        except Exception:
            # A manifest is bucket data: one that is not an object, or lacks
            # a field, resolves to nothing rather than failing the attempt.
            return None
        return self._accept(record, key)

    # -- restore -----------------------------------------------------------
    def restore(self, record: CheckpointRecord, ws: Workspace) -> int:
        """Restore into a FRESH workspace. Refuses a non-empty `work/`.

        The refusal is the point: restoring over an existing tree produces a
        workspace that matches no checkpoint, which is worse than failing.

        Every refusal raises `CheckpointError` with `work/` left empty, and
        the caller (`Worker._restore_checkpoint`) starts the attempt from that
        empty workspace rather than failing it (#346): an archive whose bytes
        no longer match the recorded digest is a checkpoint not to trust, not
        a reason to burn a retry. The one exception is `RestoreResourceError`,
        an extraction that ran out of local room: it fails the attempt.
        """
        if not self._owns(record):
            raise CheckpointError(
                f"checkpoint {record.checkpoint_id} belongs to "
                f"{record.tenant_id}/{record.task_id}, not "
                f"{self._tenant_id}/{self._task_id}; refusing to restore it"
            )
        if any(ws.work.iterdir()):
            raise CheckpointError(
                "refusing to restore a checkpoint into a non-empty workspace; "
                "a resumed attempt must start from a fresh ephemeral runtime"
            )
        # Every refusal of an inconsistent archive is raised by `_safe_members`
        # BEFORE the first member of that archive is extracted. A chain
        # refused part-way through has its earlier layers in `work/`, so on
        # any refusal `work/` is emptied again: a refused restore leaves it
        # empty and the resume falls back as it always has on a
        # `CheckpointError` here.
        skipped: list[tuple[str, str]] = []

        def skip(name: str, link: str) -> None:
            skipped.append((name, link))

        layers: list[tuple[str, Path, list[str]]] = []
        #: File content written so far, over every layer; see
        #: `RESTORE_EXPANSION_RATIO`.
        expanded = [0]
        try:
            self._download_chain(record, ws, layers)
            # Oldest first: the full archive, then each change in order.
            for position, (checkpoint_id, path, deleted) in enumerate(reversed(layers)):
                self._apply_layer(
                    checkpoint_id,
                    path,
                    ws.work,
                    deleted,
                    incremental=position > 0,
                    on_skip=skip,
                    expanded=expanded,
                )
                path.unlink(missing_ok=True)
        except CheckpointError:
            _empty_directory(ws.work)
            raise
        finally:
            for _, path, _ in layers:
                path.unlink(missing_ok=True)
        self._log_skipped_links(record, skipped)

        # The ids continue from the restored checkpoint (#174), so they stay
        # monotonic along the chain of attempts a person reads in the bucket
        # and on the attempt documents. Each attempt's ids still sit under its
        # own prefix, so continuing from an OLDER checkpoint than the newest
        # (a pointer can name one) collides with nothing. `seq`, the
        # heartbeat's count, is not moved: it stays this attempt's.
        #
        # The manifest is data read from a bucket, so its `seq` is used only
        # when it is a non-negative integer no larger than `RESTORED_SEQ_MAX`.
        # Anything else would make every later `create` raise formatting the
        # id, or name an object key longer than GCS accepts, and invariant 8
        # says this attempt must keep checkpointing; its ids start again at 1
        # instead.
        restored_seq = record.seq
        is_count = isinstance(restored_seq, int) and not isinstance(restored_seq, bool)
        if is_count and 0 <= restored_seq <= RESTORED_SEQ_MAX:
            self._seq = max(self._seq, restored_seq)
        else:
            self._log.warning(
                "the restored checkpoint's seq is not a count; this attempt's ids start at 1",
                checkpoint_id=record.checkpoint_id,
                seq_type=type(restored_seq).__name__,
            )
        # `walk_tree`, not `Path.rglob`, which recurses on Python 3.11: a
        # restored tree 1,000 levels deep raised `RecursionError` here, after
        # the restore had succeeded (#259). Regular files only, no link
        # followed -- the archive may carry links, and a link is not a file
        # this checkpoint restored.
        restored = 0
        for dirpath, _dirs, filenames in walk_tree(ws.work):
            for name in filenames:
                try:
                    if stat.S_ISREG(os.lstat(dirpath / name).st_mode):
                        restored += 1
                except OSError:
                    continue
        self._log.info(
            "checkpoint restored",
            checkpoint_id=record.checkpoint_id,
            from_attempt=record.attempt_id,
            files=restored,
        )
        return restored

    def _download_chain(
        self, record: CheckpointRecord, ws: Workspace, layers: list[tuple[str, Path, list[str]]]
    ) -> None:
        """Download `record`'s archive and every base it names, newest first, into `layers`.

        Each archive is checked against the digest that names it before its
        header is read: the head's against the record (which the attempt
        document binds), every base's against the header of the archive
        after it. So the whole chain is bound to the one digest the attempt
        recorded. A base is looked for only beside the head, under the same
        attempt's `checkpoints/`: a chain never crosses an attempt.
        """
        checkpoint_id = record.checkpoint_id
        key = record.archive_key
        expected = record.archive_sha256
        directory = record.archive_key.rsplit("/", 2)[0]
        seen: set[str] = set()
        while True:
            name = ARCHIVE_NAME if not layers else f"base-{len(layers):03d}.tar.gz"
            path = ws.restore / name
            layers.append((checkpoint_id, path, []))
            self._store.download_file(key, path)
            digest = _sha256(path)
            if digest != expected:
                raise CheckpointError(
                    f"checkpoint {checkpoint_id} failed integrity check: "
                    f"expected {expected}, got {digest}"
                )
            seen.add(checkpoint_id)
            base, deleted = _chain_header(path, checkpoint_id)
            layers[-1] = (checkpoint_id, path, deleted)
            if base is None:
                return
            if base[0] in seen or len(layers) >= RESTORE_CHAIN_MAX:
                raise CheckpointError(
                    f"checkpoint {record.checkpoint_id}'s chain of bases does not end "
                    f"in a full archive within {RESTORE_CHAIN_MAX}"
                )
            checkpoint_id, expected = base
            key = f"{directory}/{checkpoint_id}/{ARCHIVE_NAME}"

    def _apply_layer(
        self,
        checkpoint_id: str,
        path: Path,
        work: Path,
        deleted: list[str],
        *,
        incremental: bool,
        on_skip: Callable[[str, str], None],
        expanded: list[int] | None = None,
    ) -> None:
        """Extract one archive of a chain into `work/`, after its deletions.

        An incremental archive lands on the tree its bases made. What it
        names deleted is removed first; then whatever stands where one of its
        members goes is removed unless both are directories, so a changed
        file is replaced rather than written through -- a regular member over
        an existing LINK would otherwise be written to the link's target.
        A member under a path that is a link on disk is refused: this
        platform's archiver never writes one, any more than within one
        archive (`_safe_members`).

        `expanded[0]` sums the size of every file member accepted, over every
        layer `restore` applies; past `RESTORE_EXPANSION_RATIO` times the cap
        the archive is refused before this layer writes a byte. And an
        `OSError` out of the extraction is a `CheckpointError`, so `restore`
        empties `work/` for it as for any refusal.
        """
        with tarfile.open(path, "r:gz") as tar:
            members = _safe_members(tar, work, on_skip=on_skip)
            if expanded is not None:
                expanded[0] += sum(member.size for member in members if member.isfile())
                cap = self._max_bytes * RESTORE_EXPANSION_RATIO
                if expanded[0] > cap:
                    raise CheckpointError(
                        f"checkpoint {checkpoint_id} expands past {cap} bytes; "
                        f"refusing to restore it"
                    )
            if incremental:
                for rel in deleted:
                    _remove_within(work, rel, checkpoint_id, keep_directory=False)
                for member in members:
                    _remove_within(work, member.name, checkpoint_id, keep_directory=member.isdir())
            try:
                tar.extractall(path=work, members=members, filter=_restore_filter(on_skip))
            except tarfile.FilterError as exc:
                raise CheckpointError(
                    f"checkpoint {checkpoint_id} holds a member the restore "
                    f"refuses: {type(exc).__name__}"
                ) from exc
            except OSError as exc:
                error = (
                    RestoreResourceError
                    if exc.errno in _LOCAL_RESOURCE_ERRNOS
                    else CheckpointError
                )
                raise error(
                    f"checkpoint {checkpoint_id} could not be extracted: {type(exc).__name__}"
                ) from exc
        _unmake_escaped_links(work, members, on_skip)

    def _log_skipped_links(self, record: CheckpointRecord, skipped: list[tuple[str, str]]) -> None:
        """One warning naming the first `SKIPPED_LINKS_NAMED` skipped links, and the count.

        A link's name and target are the agent's text, so they go through the
        worker's scrub before they are logged, as every field of a log line
        does: an agent that names a link after a key it was given does not get
        that key into Cloud Logging by it.
        """
        if not skipped:
            return
        scrub: Callable[[str], str] = getattr(self._log, "scrub_text", None) or (lambda text: text)
        named = [
            {"member": scrub(name), "link": scrub(link)}
            for name, link in skipped[:SKIPPED_LINKS_NAMED]
        ]
        self._log.warning(
            "checkpoint restore skipped links escaping the workspace; the rest is restored",
            checkpoint_id=record.checkpoint_id,
            skipped=len(skipped),
            named=named,
            not_named=max(0, len(skipped) - SKIPPED_LINKS_NAMED),
        )


def _unmake_escaped_links(
    destination: Path, members: list[tarfile.TarInfo], on_skip: Callable[[str, str], None]
) -> None:
    """Remove a restored symlink that resolves outside `destination` once all are made.

    The filter judged each link against the links made BEFORE it. A link made
    earlier can come to leave only through one made later -- `y -> z/..`
    judged while `z` did not exist, then `z -> .` -- and nothing was written
    through it (`_safe_members` refuses that), but it would be left in the
    workspace pointing out. Each is checked once more against the finished
    tree, and one that now leaves is unlinked and named as skipped.
    """
    root = os.path.realpath(destination)
    for member in members:
        if not member.issym():
            continue
        path = os.path.join(root, member.name)
        if not os.path.islink(path):
            continue  # the filter skipped it
        resolved = os.path.realpath(path)
        if resolved == root or resolved.startswith(root + os.sep):
            continue
        os.unlink(path)
        on_skip(member.name, member.linkname)


def _remove_within(work: Path, rel: str, checkpoint_id: str, *, keep_directory: bool) -> None:
    """Remove what stands at `rel` under `work`, following no link; absent is fine.

    By descriptor from `work/`, every parent opened `O_NOFOLLOW`: a parent
    that is a link refuses the archive (`CheckpointError`), and nothing is
    removed outside `work/`. A directory is removed whole, by descriptor
    (`shutil.rmtree` with `dir_fd`), unless `keep_directory` and it is one.
    `rmtree` recurses a frame per level; a restored tree is at most
    `CHECKPOINT_MAX_DEPTH` deep, because nothing deeper is ever archived.
    """
    parts = _archive_parts(rel)
    if parts is None:
        raise CheckpointError(f"checkpoint {checkpoint_id} names a path outside the workspace")
    fds = [os.open(work, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)]
    try:
        for part in parts[:-1]:
            try:
                fds.append(
                    os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fds[-1])
                )
            except FileNotFoundError:
                return
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise CheckpointError(
                        f"checkpoint {checkpoint_id} writes {rel} through a link or a "
                        f"file in the restored tree"
                    ) from exc
                raise
        try:
            st = os.stat(parts[-1], dir_fd=fds[-1], follow_symlinks=False)
        except FileNotFoundError:
            return
        if stat.S_ISDIR(st.st_mode):
            if not keep_directory:
                shutil.rmtree(parts[-1], dir_fd=fds[-1])
        else:
            os.unlink(parts[-1], dir_fd=fds[-1])
    finally:
        for fd in fds:
            os.close(fd)


def _empty_directory(work: Path) -> None:
    """Remove everything under `work`, following no link."""
    fd = os.open(work, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for name in os.listdir(fd):
            if stat.S_ISDIR(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode):
                shutil.rmtree(name, dir_fd=fd)
            else:
                os.unlink(name, dir_fd=fd)
    finally:
        os.close(fd)
