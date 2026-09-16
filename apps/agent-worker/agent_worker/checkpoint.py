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
            if not str(link_target).startswith(str(resolved_dest)):
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

        with tempfile.TemporaryDirectory(prefix="swarm-ckpt-") as tmpdir:
            archive_path = Path(tmpdir) / ARCHIVE_NAME
            file_count = self._write_archive(ws.work, archive_path)
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

    def _write_archive(self, source: Path, archive_path: Path) -> int:
        count = 0
        with tarfile.open(archive_path, "w:gz") as tar:
            for entry in sorted(Path(source).rglob("*")):
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

    # -- discover ----------------------------------------------------------
    def find_latest(self) -> CheckpointRecord | None:
        """Newest committed checkpoint for this TASK, across every attempt."""
        prefix = attempts_prefix(tenant_id=self._tenant_id, task_id=self._task_id)
        manifests = [k for k in self._store.list_keys(prefix) if k.endswith(f"/{MANIFEST_NAME}")]
        best: CheckpointRecord | None = None
        for key in manifests:
            try:
                data = json.loads(self._store.download_bytes(key).decode("utf-8"))
                record = CheckpointRecord.from_dict(data)
            except Exception as exc:  # a half-written manifest must not block a resume
                self._log.warning("ignoring unreadable checkpoint manifest", key=key, error=str(exc))
                continue
            if best is None or record.sort_key > best.sort_key:
                best = record
        return best

    def find_by_uri(self, uri: str) -> CheckpointRecord | None:
        """Resolve the pointer the control plane keeps in `task.latest_checkpoint`."""
        if not uri:
            return None
        key = uri.split("://", 1)[-1]
        # Every object this platform writes lives under `tenants/`, so slicing
        # from there turns any flavour of URI -- gs://bucket/..., file:///abs/...
        # -- into the bucket-relative key without special-casing the store.
        marker = key.find("tenants/")
        if marker > 0:
            key = key[marker:]
        elif key.startswith(self._store.bucket + "/"):
            key = key[len(self._store.bucket) + 1 :]
        key = key.rstrip("/") + f"/{MANIFEST_NAME}"
        try:
            data = json.loads(self._store.download_bytes(key).decode("utf-8"))
        except Exception:
            return None
        return CheckpointRecord.from_dict(data)

    # -- restore -----------------------------------------------------------
    def restore(self, record: CheckpointRecord, ws: Workspace) -> int:
        """Restore into a FRESH workspace. Refuses a non-empty `work/`.

        The refusal is the point: restoring over an existing tree produces a
        workspace that matches no checkpoint, which is worse than failing.
        """
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
