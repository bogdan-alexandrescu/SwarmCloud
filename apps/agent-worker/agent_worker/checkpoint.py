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

Restore searches across ALL attempts of the task, because a resume is by
definition a new attempt with a new id, and the checkpoint worth restoring was
written by the attempt that died.

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
import stat
import tarfile
import tempfile
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .errors import CheckpointError
from .objectstore import ObjectStore
from .workspace import Workspace

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
            target = _archive_parts(member.linkname)
            if target is None or target not in files:
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
        """Archive `work/`, upload it, then commit with the manifest."""
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
        with tempfile.TemporaryDirectory(prefix="swarm-ckpt-") as tmpdir:
            archive_path = Path(tmpdir) / ARCHIVE_NAME
            file_count = self._write_archive(ws.work, archive_path, skip=skip)
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
            )
            # Commit marker. Nothing before this line is discoverable.
            self._store.upload_bytes(
                manifest_key,
                json.dumps({**asdict(record), "label": label}, indent=2).encode("utf-8"),
                content_type="application/json",
            )
            self._written += 1

        self._log.info(
            "checkpoint written",
            checkpoint_id=checkpoint_id,
            bytes=size,
            files=file_count,
            label=label,
        )
        return record

    def _write_archive(
        self, source: Path, archive_path: Path, *, skip: frozenset[str] = frozenset()
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
        tree's depth, and a deep tree cannot raise `RecursionError` here.
        """
        try:
            root_fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError as exc:
            raise CheckpointError(
                f"refusing to checkpoint {source}: it is a link or not a directory "
                f"({exc.strerror}), and archiving it would archive whatever it "
                f"points at"
            ) from exc
        count = 0
        stack: list[tuple[int, str, list[str]]] = []
        try:
            stack.append((root_fd, "", sorted(os.listdir(root_fd), reverse=True)))
        except BaseException:
            os.close(root_fd)
            raise
        try:
            with _CappedFile(archive_path, self._max_bytes) as raw, tarfile.open(
                fileobj=raw, mode="w:gz"
            ) as tar:
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
                        count += self._add_entry(tar, stack, dir_fd, name, rel)
                    except (FileNotFoundError, NotADirectoryError):
                        continue  # gone, or no longer a directory
                    except OSError as exc:
                        if exc.errno == errno.ELOOP:
                            continue  # became a link after it was looked at
                        raise
        finally:
            for dir_fd, _, _ in stack:
                os.close(dir_fd)
        return count

    @staticmethod
    def _add_entry(
        tar: tarfile.TarFile,
        stack: list[tuple[int, str, list[str]]],
        dir_fd: int,
        name: str,
        rel: str,
    ) -> int:
        """Add one entry of the directory open as `dir_fd`; 1 when it counts.

        A directory is pushed onto `stack` open, for the walk to descend into.
        """
        st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        if stat.S_ISLNK(st.st_mode):
            info = _tarinfo(rel, st, tarfile.SYMTYPE)
            info.linkname = os.readlink(name, dir_fd=dir_fd)
            tar.addfile(info)
            return 1
        if stat.S_ISDIR(st.st_mode):
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
            try:
                info = _tarinfo(rel, os.fstat(child), tarfile.DIRTYPE)
                names = sorted(os.listdir(child), reverse=True)
            except BaseException:
                os.close(child)
                raise
            stack.append((child, rel + "/", names))
            tar.addfile(info)
            return 0
        if not stat.S_ISREG(st.st_mode):
            return 0  # sockets and FIFOs are not state worth carrying
        # O_NONBLOCK so a FIFO swapped in after the stat cannot hang the open;
        # the type is checked again on what was actually opened.
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
        with os.fdopen(fd, "rb") as handle:
            # `gettarinfo` over the open file: its fstat, and tarfile's own
            # hard-link bookkeeping, as `tar.add` had.
            info = tar.gettarinfo(arcname=rel, fileobj=handle)
            if info is None or not (info.isreg() or info.islnk()):
                return 0
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
        """Newest committed checkpoint for this TASK, across every attempt."""
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
        same tenant's -- resolves to nothing and the worker falls back to
        `find_latest`, which searches that prefix and no other.
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
        except Exception:
            return None
        return self._accept(CheckpointRecord.from_dict(data), key)

    # -- restore -----------------------------------------------------------
    def restore(self, record: CheckpointRecord, ws: Workspace) -> int:
        """Restore into a FRESH workspace. Refuses a non-empty `work/`.

        The refusal is the point: restoring over an existing tree produces a
        workspace that matches no checkpoint, which is worse than failing.
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
        archive_path = ws.restore / ARCHIVE_NAME
        self._store.download_file(record.archive_key, archive_path)

        digest = _sha256(archive_path)
        if digest != record.archive_sha256:
            raise CheckpointError(
                f"checkpoint {record.checkpoint_id} failed integrity check: "
                f"expected {record.archive_sha256}, got {digest}"
            )

        # Every refusal of an inconsistent archive is raised by `_safe_members`
        # BEFORE the first member is extracted, so a refused archive leaves
        # `work/` empty and the resume falls back as it always has on a
        # `CheckpointError` here.
        skipped: list[tuple[str, str]] = []

        def skip(name: str, link: str) -> None:
            skipped.append((name, link))

        with tarfile.open(archive_path, "r:gz") as tar:
            members = _safe_members(tar, ws.work, on_skip=skip)
            try:
                tar.extractall(path=ws.work, members=members, filter=_restore_filter(skip))
            except tarfile.FilterError as exc:
                raise CheckpointError(
                    f"checkpoint {record.checkpoint_id} holds a member the restore "
                    f"refuses: {type(exc).__name__}"
                ) from exc
        archive_path.unlink(missing_ok=True)
        _unmake_escaped_links(ws.work, members, skip)
        self._log_skipped_links(record, skipped)

        # The ids continue from the restored checkpoint (#174), so they stay
        # monotonic along the chain of attempts a person reads in the bucket
        # and on the attempt documents. Each attempt's ids still sit under its
        # own prefix, so continuing from an OLDER checkpoint than the newest
        # (a pointer can name one) collides with nothing. `seq`, the
        # heartbeat's count, is not moved: it stays this attempt's.
        #
        # The manifest is data read from a bucket, so its `seq` is used only
        # when it is a non-negative integer. Anything else would make every
        # later `create` raise formatting the id, and invariant 8 says this
        # attempt must keep checkpointing; its ids start again at 1 instead.
        restored_seq = record.seq
        is_count = isinstance(restored_seq, int) and not isinstance(restored_seq, bool)
        if is_count and restored_seq >= 0:
            self._seq = max(self._seq, restored_seq)
        else:
            self._log.warning(
                "the restored checkpoint's seq is not a count; this attempt's ids start at 1",
                checkpoint_id=record.checkpoint_id,
                seq_type=type(restored_seq).__name__,
            )
        restored = sum(1 for _ in ws.work.rglob("*") if _.is_file())
        self._log.info(
            "checkpoint restored",
            checkpoint_id=record.checkpoint_id,
            from_attempt=record.attempt_id,
            files=restored,
        )
        return restored

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
