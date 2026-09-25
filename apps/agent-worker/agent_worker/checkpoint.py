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

import hashlib
import json
import os
import tarfile
import tempfile
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import CheckpointError
from .objectstore import ObjectStore
from .workspace import Workspace

ARCHIVE_NAME = "archive.tar.gz"
MANIFEST_NAME = "manifest.json"


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


def _safe_members(tar: tarfile.TarFile, destination: Path) -> list[tarfile.TarInfo]:
    """Reject anything that would write outside the destination.

    A checkpoint archive is written by this platform, but it contains a tenant's
    working tree, and a tenant's agent can create any file it likes inside it --
    including a symlink to /etc. Path traversal is checked on the way out, not
    trusted on the way in.
    """
    resolved_dest = destination.resolve()
    members: list[tarfile.TarInfo] = []
    for member in tar.getmembers():
        name = member.name
        if name.startswith("/") or os.path.isabs(name):
            raise CheckpointError(f"checkpoint archive contains an absolute path: {name}")
        target = (resolved_dest / name).resolve()
        if not str(target).startswith(str(resolved_dest) + os.sep) and target != resolved_dest:
            raise CheckpointError(f"checkpoint archive escapes the workspace: {name}")
        if member.issym() or member.islnk():
            link = member.linkname
            link_target = (target.parent / link).resolve() if not os.path.isabs(link) else Path(link)
            # The separator is required here for the same reason it is required
            # on the path check above: without it, `/w/workspace/att/work` would
            # accept a link resolving to `/w/workspace/att/work-secrets`, a
            # sibling that merely shares the string prefix.
            if not (
                str(link_target).startswith(str(resolved_dest) + os.sep)
                or link_target == resolved_dest
            ):
                raise CheckpointError(
                    f"checkpoint archive contains a link escaping the workspace: {name} -> {link}"
                )
        elif not (member.isfile() or member.isdir()):
            # Sockets, FIFOs and devices are never restored; an agent that left
            # one behind gets a workspace without it rather than a failed resume.
            continue
        members.append(member)
    return members


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
        self._seq = 0

    @property
    def seq(self) -> int:
        return self._seq

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
        # Archived, it would break every resume: it resolves outside `work/`,
        # and `_safe_members` refuses an archive holding such a link, so the
        # restore would fail and the attempt with it. It names this attempt's
        # directory besides, and a resumed attempt makes its own.
        #
        # What is BEHIND the link is not archived either, and nothing has to
        # be done for that: `Path.rglob` does not descend into a symlinked
        # directory (3.11 checks `is_dir(follow_symlinks=False)`), so
        # `_write_archive` meets the link as one entry and never its contents.
        # Those files are the artifacts, which are uploaded on their own.
        skip = frozenset(
            path for path in (ws.artifacts_link(),) if ws.is_artifacts_link(path)
        )
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
            )
            # Commit marker. Nothing before this line is discoverable.
            self._store.upload_bytes(
                manifest_key,
                json.dumps({**asdict(record), "label": label}, indent=2).encode("utf-8"),
                content_type="application/json",
            )

        self._log.info(
            "checkpoint written",
            checkpoint_id=checkpoint_id,
            bytes=size,
            files=file_count,
            label=label,
        )
        return record

    def _write_archive(
        self, source: Path, archive_path: Path, *, skip: frozenset[Path] = frozenset()
    ) -> int:
        count = 0
        with tarfile.open(archive_path, "w:gz") as tar:
            for entry in sorted(Path(source).rglob("*")):
                if entry in skip:
                    continue
                if entry.is_symlink():
                    tar.add(entry, arcname=str(entry.relative_to(source)), recursive=False)
                    count += 1
                    continue
                if entry.is_dir():
                    tar.add(entry, arcname=str(entry.relative_to(source)), recursive=False)
                    continue
                if not entry.is_file():
                    continue  # sockets and FIFOs are not state worth carrying
                tar.add(entry, arcname=str(entry.relative_to(source)), recursive=False)
                count += 1
        return count

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

        with tarfile.open(archive_path, "r:gz") as tar:
            members = _safe_members(tar, ws.work)
            tar.extractall(path=ws.work, members=members)
        archive_path.unlink(missing_ok=True)

        # The sequence continues from the restored checkpoint so checkpoint ids
        # stay monotonic for a human reading the bucket.
        self._seq = max(self._seq, 0)
        restored = sum(1 for _ in ws.work.rglob("*") if _.is_file())
        self._log.info(
            "checkpoint restored",
            checkpoint_id=record.checkpoint_id,
            from_attempt=record.attempt_id,
            files=restored,
        )
        return restored
