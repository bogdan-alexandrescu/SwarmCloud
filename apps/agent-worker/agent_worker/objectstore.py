"""Artifact and checkpoint storage.

Every object this platform writes lives at a path derived only from identifiers
the control plane already knows:

    tenants/<tenant>/tasks/<task>/attempts/<attempt>/checkpoints/<id>/...
    tenants/<tenant>/tasks/<task>/attempts/<attempt>/artifacts/...
    tenants/<tenant>/tasks/<task>/attempts/<attempt>/logs/...

Deterministic paths are what make a resumed worker able to find the previous
attempt's checkpoint without a catalogue, and what make the tenant prefix a real
isolation boundary: a per-tenant IAM condition on `tenants/<tenant>/` is
sufficient, because nothing is ever written outside it.

Two implementations, one interface. `GcsObjectStore` is production;
`LocalObjectStore` is a filesystem-backed store used by the unit tests and by
`RUN_MODE=local` smoke runs. The local one is not a mock -- it implements the
same semantics, including the prefix listing order the checkpoint code relies
on, so a checkpoint round-trip test exercises the real code path.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Iterable, Protocol, runtime_checkable


class ObjectStoreError(RuntimeError):
    pass


def validate_key(key: str) -> str:
    """Reject anything that could escape the tenant prefix.

    A single trailing slash is allowed: a checkpoint's canonical identity is the
    prefix `.../checkpoints/<id>/`, and that is the string recorded in
    `task.latest_checkpoint`.
    """
    if not key or key.startswith("/"):
        raise ObjectStoreError(f"object key must be relative and non-empty: {key!r}")
    parts = key.rstrip("/").split("/")
    if not parts or any(p in ("", ".", "..") for p in parts):
        raise ObjectStoreError(f"object key contains an unsafe path segment: {key!r}")
    return key


@runtime_checkable
class ObjectStore(Protocol):
    bucket: str

    def uri(self, key: str) -> str: ...
    def upload_file(self, key: str, source: Path, content_type: str | None = None) -> int: ...
    def upload_bytes(self, key: str, data: bytes, content_type: str | None = None) -> int: ...
    def download_file(self, key: str, destination: Path) -> int: ...
    def download_bytes(self, key: str) -> bytes: ...
    def list_keys(self, prefix: str) -> list[str]: ...
    def exists(self, key: str) -> bool: ...
    def delete(self, key: str) -> None: ...


class LocalObjectStore:
    """Filesystem-backed object store with GCS-compatible key semantics."""

    def __init__(self, root: Path, bucket: str = "local") -> None:
        self.root = Path(root)
        self.bucket = bucket
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.root / validate_key(key)

    def uri(self, key: str) -> str:
        return f"file://{self._path(key)}"

    def upload_file(self, key: str, source: Path, content_type: str | None = None) -> int:
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        return target.stat().st_size

    def upload_bytes(self, key: str, data: bytes, content_type: str | None = None) -> int:
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return len(data)

    def download_file(self, key: str, destination: Path) -> int:
        source = self._path(key)
        if not source.exists():
            raise ObjectStoreError(f"object not found: {self.uri(key)}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        return destination.stat().st_size

    def download_bytes(self, key: str) -> bytes:
        source = self._path(key)
        if not source.exists():
            raise ObjectStoreError(f"object not found: {self.uri(key)}")
        return source.read_bytes()

    def list_keys(self, prefix: str) -> list[str]:
        base = self.root
        found: list[str] = []
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(base).as_posix()
            if rel.startswith(prefix):
                found.append(rel)
        return sorted(found)

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def delete(self, key: str) -> None:
        path = self._path(key)
        if path.exists():
            path.unlink()


class GcsObjectStore:
    """Google Cloud Storage. The client is imported lazily and reused.

    Lazy import is deliberate: the unit tests drive the entire worker lifecycle
    and must never pull in grpc or authenticate to anything.
    """

    def __init__(self, bucket: str, project_id: str | None = None, client: object | None = None):
        if not bucket:
            raise ObjectStoreError("ARTIFACT_BUCKET is required for GCS storage")
        self.bucket = bucket
        self._project_id = project_id
        self._client = client
        self._bucket_obj = None

    def _get_bucket(self):
        if self._bucket_obj is None:
            if self._client is None:
                from google.cloud import storage  # lazy: keeps unit tests grpc-free

                self._client = storage.Client(project=self._project_id)
            self._bucket_obj = self._client.bucket(self.bucket)
        return self._bucket_obj

    def uri(self, key: str) -> str:
        return f"gs://{self.bucket}/{validate_key(key)}"

    def upload_file(self, key: str, source: Path, content_type: str | None = None) -> int:
        blob = self._get_bucket().blob(validate_key(key))
        blob.upload_from_filename(str(source), content_type=content_type)
        return Path(source).stat().st_size

    def upload_bytes(self, key: str, data: bytes, content_type: str | None = None) -> int:
        blob = self._get_bucket().blob(validate_key(key))
        blob.upload_from_string(data, content_type=content_type or "application/octet-stream")
        return len(data)

    def download_file(self, key: str, destination: Path) -> int:
        blob = self._get_bucket().blob(validate_key(key))
        destination.parent.mkdir(parents=True, exist_ok=True)
        blob.download_to_filename(str(destination))
        return destination.stat().st_size

    def download_bytes(self, key: str) -> bytes:
        blob = self._get_bucket().blob(validate_key(key))
        return blob.download_as_bytes()

    def list_keys(self, prefix: str) -> list[str]:
        client = self._get_bucket().client
        return sorted(b.name for b in client.list_blobs(self.bucket, prefix=prefix))

    def exists(self, key: str) -> bool:
        return bool(self._get_bucket().blob(validate_key(key)).exists())

    def delete(self, key: str) -> None:
        blob = self._get_bucket().blob(validate_key(key))
        if blob.exists():
            blob.delete()


def build_object_store(
    *, bucket: str, project_id: str | None = None, local_root: Path | None = None
) -> ObjectStore:
    """Pick a store. A local root is only ever used when explicitly configured."""
    if local_root is not None:
        return LocalObjectStore(local_root, bucket=bucket or "local")
    return GcsObjectStore(bucket=bucket, project_id=project_id)


def total_size(store: ObjectStore, keys: Iterable[str]) -> int:
    return sum(len(store.download_bytes(k)) for k in keys)
