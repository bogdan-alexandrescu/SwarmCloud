"""READ-ONLY access to the artifact bucket, for the inspection routes.

The worker writes checkpoints, artifacts and logs to

    tenants/<tenant>/tasks/<task>/attempts/<attempt>/checkpoints/<id>/...
    tenants/<tenant>/tasks/<task>/attempts/<attempt>/artifacts/...
    tenants/<tenant>/tasks/<task>/attempts/<attempt>/logs/...

and until now nothing served any of it. This module is how the API reads it,
and it deliberately exposes THREE operations and no more: list a prefix, stat
one object, read a byte range of one object. There is no upload, no delete and
no copy, because the API's IAM grant is `roles/storage.objectViewer`
(terraform/modules/iam/bindings.tf, `api_reader`) and a method this class does
not have is a method no refactor can accidentally call.

ABSENT AND UNREADABLE ARE TWO DIFFERENT ANSWERS, AND THEY DO NOT SHARE A BASE
-----------------------------------------------------------------------------
`ObjectAbsent` and `ObjectUnreadable` both derive from `Exception` and NOT from
a common ancestor. That is the whole design of this module, and it is a
deliberate inconvenience: a caller must name both to catch both, so there is no
`except ObjectError` that quietly turns "the bucket 403'd" into "there are no
logs". This repository has shipped that bug three times in a week -- a gate
that read a live database as absent because the lookup failed, a guard that
said "stale" when it meant "I cannot read this", a destroy check that reported
another team's cluster as deleted when it could not look -- and every one of
them was a shared exception base or a bare `except`.

The Google client is imported lazily, inside the method that needs it, for the
same reason `deps.build_firestore` is: `create_app()` must construct with no
credentials and no network, or every unit test that imports the app needs a
service account. The swarm-api image's build-time import check
(images/swarm-api/Dockerfile) runs exactly that and would fail otherwise.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

#: A single path segment in an object key. Used to validate every identifier
#: that is about to be interpolated into a key -- see `safe_segment`.
_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class UnsafeKeySegment(ValueError):
    """An identifier that must never reach an object key."""


def safe_segment(value: str, *, what: str) -> str:
    """Return `value` if it is a single, traversal-free path segment.

    EVERY identifier that ends up in an object key goes through this, including
    ones that came from Firestore rather than from the caller. The tenant id in
    the prefix is the isolation boundary (invariant 9), and a boundary built by
    string concatenation is only as strong as the weakest segment in it: an
    `attempt_id` of `../../../other-tenant` would walk straight out of it in any
    store that normalises paths, and a `tenant_id` containing a slash would
    silently re-root the prefix in any store at all.

    GCS itself does not normalise `..`, so in production the escape does not
    work -- but "the current storage backend happens not to normalise" is not
    an access-control argument, and `LocalObjectReader`-shaped stores in tests
    and local runs do normalise.
    """
    if not isinstance(value, str) or not value or not _SEGMENT.match(value):
        raise UnsafeKeySegment(f"{what} is not a usable path segment")
    if value in (".", ".."):
        raise UnsafeKeySegment(f"{what} is not a usable path segment")
    return value


class ObjectAbsent(Exception):
    """The object is not there. A fact about the world.

    Does NOT share a base with `ObjectUnreadable`; see the module docstring.
    """

    def __init__(self, key: str) -> None:
        super().__init__(f"object not found: {key}")
        self.key = key


class ObjectUnreadable(Exception):
    """The read did not complete. A fact about US, not about the world.

    Carries `reason` unredacted for the log and `key` for the operator. The
    route is what redacts it before it reaches a response body -- a GCS error
    can quote the failed request, and a signed URL is a credential.
    """

    def __init__(self, key: str, reason: str) -> None:
        super().__init__(f"could not read {key}: {reason}")
        self.key = key
        self.reason = reason


@dataclass(frozen=True)
class ObjectInfo:
    """What a listing knows about one object, without downloading it."""

    key: str
    size: int
    updated: datetime | None = None
    content_type: str | None = None

    @property
    def name(self) -> str:
        return self.key.rsplit("/", 1)[-1]


@dataclass(frozen=True)
class ObjectListing:
    """A prefix listing that never hides having been cut short.

    `truncated` exists because a listing that stopped at the scan limit and a
    listing that reached the end of the prefix are otherwise identical, and a
    checkpoint screen that silently shows the first N of M is the same lie as
    an empty one.
    """

    prefix: str
    objects: tuple[ObjectInfo, ...]
    truncated: bool = False


@dataclass(frozen=True)
class ObjectSlice:
    """A byte range of one object, plus the size of the whole.

    `total_bytes` is the object's real size, so a caller can tell a short read
    (there is more) from a complete one (that was all of it) without a second
    request. `data` may legitimately be empty for a zero-byte object -- which is
    an object that EXISTS and is empty, a third thing again from absent and from
    unreadable.
    """

    key: str
    offset: int
    data: bytes
    total_bytes: int

    @property
    def end(self) -> int:
        return self.offset + len(self.data)

    @property
    def complete(self) -> bool:
        return self.end >= self.total_bytes


@runtime_checkable
class ObjectReader(Protocol):
    """The read-only surface the inspection routes are written against."""

    bucket: str

    def uri(self, key: str) -> str: ...

    def list_objects(self, prefix: str, *, limit: int) -> ObjectListing: ...

    def read_range(self, key: str, *, offset: int, length: int) -> ObjectSlice: ...


def _uri(bucket: str, key: str) -> str:
    return f"gs://{bucket}/{key}"


class GcsObjectReader:
    """Google Cloud Storage, read-only.

    Holds no client until the first call. `google.cloud.storage` is imported
    inside the accessor so importing this module -- which `create_app` does --
    pulls in neither grpc nor google.auth.
    """

    def __init__(self, bucket: str, project_id: str | None = None, client: Any = None) -> None:
        if not bucket:
            raise ValueError("an artifact bucket is required to read objects")
        self.bucket = bucket
        self._project_id = project_id
        self._client = client

    def _get_client(self) -> Any:
        if self._client is None:
            from google.cloud import storage  # lazy: create_app() must need no credentials

            self._client = storage.Client(project=self._project_id)
        return self._client

    def uri(self, key: str) -> str:
        return _uri(self.bucket, key)

    def list_objects(self, prefix: str, *, limit: int) -> ObjectListing:
        try:
            client = self._get_client()
            # One over the limit, so "there was more" is observed rather than
            # inferred from a full page -- a full page is ambiguous.
            blobs = list(client.list_blobs(self.bucket, prefix=prefix, max_results=limit + 1))
        except Exception as exc:  # noqa: BLE001 - every failure is the same answer: we cannot look
            raise ObjectUnreadable(prefix, f"{type(exc).__name__}: {exc}") from None
        truncated = len(blobs) > limit
        rows = tuple(
            ObjectInfo(
                key=b.name,
                size=int(b.size or 0),
                updated=b.updated,
                content_type=b.content_type,
            )
            for b in blobs[:limit]
        )
        return ObjectListing(prefix=prefix, objects=rows, truncated=truncated)

    def read_range(self, key: str, *, offset: int, length: int) -> ObjectSlice:
        if offset < 0 or length < 0:
            raise ValueError("offset and length must not be negative")
        try:
            # `get_blob` returns None for a missing object and raises for a
            # failed lookup, which is exactly the distinction this module is
            # built on -- `blob.exists()` collapses both into False.
            blob = self._get_client().bucket(self.bucket).get_blob(key)
        except Exception as exc:  # noqa: BLE001
            raise ObjectUnreadable(key, f"{type(exc).__name__}: {exc}") from None
        if blob is None:
            raise ObjectAbsent(key)
        total = int(blob.size or 0)
        if length == 0 or offset >= total:
            return ObjectSlice(key=key, offset=min(offset, total), data=b"", total_bytes=total)
        try:
            data = blob.download_as_bytes(start=offset, end=offset + length - 1)
        except Exception as exc:  # noqa: BLE001
            raise ObjectUnreadable(key, f"{type(exc).__name__}: {exc}") from None
        return ObjectSlice(key=key, offset=offset, data=bytes(data), total_bytes=total)


class InMemoryObjectReader:
    """A real reader over a dict, for tests and `RUN_MODE=local`.

    Not a mock: it implements the same semantics the routes depend on,
    including the sorted prefix listing and the absent/unreadable split, so a
    test drives the shipped code path rather than a stand-in for it.

    `fail_prefixes` is the point of it. Proving that a FAILED read is reported
    as a failed read needs a store that can fail on demand, and a store that
    can only be empty can only ever prove the easy half.
    """

    def __init__(
        self,
        objects: dict[str, bytes] | None = None,
        *,
        bucket: str = "swarm-artifacts-test",
        fail_prefixes: tuple[str, ...] = (),
        updated: datetime | None = None,
    ) -> None:
        self.bucket = bucket
        self.objects: dict[str, bytes] = dict(objects or {})
        self.fail_prefixes = tuple(fail_prefixes)
        self.updated = updated

    # -- test helpers ------------------------------------------------------
    def put(self, key: str, data: bytes | str) -> None:
        self.objects[key] = data.encode("utf-8") if isinstance(data, str) else data

    def fail_on(self, *prefixes: str) -> None:
        self.fail_prefixes = self.fail_prefixes + prefixes

    def _guard(self, key: str) -> None:
        for prefix in self.fail_prefixes:
            if key.startswith(prefix):
                raise ObjectUnreadable(key, "injected store failure")

    # -- reader ------------------------------------------------------------
    def uri(self, key: str) -> str:
        return _uri(self.bucket, key)

    def list_objects(self, prefix: str, *, limit: int) -> ObjectListing:
        self._guard(prefix)
        keys = sorted(k for k in self.objects if k.startswith(prefix))
        truncated = len(keys) > limit
        rows = tuple(
            ObjectInfo(
                key=k,
                size=len(self.objects[k]),
                updated=self.updated,
                content_type=None,
            )
            for k in keys[:limit]
        )
        return ObjectListing(prefix=prefix, objects=rows, truncated=truncated)

    def read_range(self, key: str, *, offset: int, length: int) -> ObjectSlice:
        if offset < 0 or length < 0:
            raise ValueError("offset and length must not be negative")
        self._guard(key)
        if key not in self.objects:
            raise ObjectAbsent(key)
        blob = self.objects[key]
        total = len(blob)
        if length == 0 or offset >= total:
            return ObjectSlice(key=key, offset=min(offset, total), data=b"", total_bytes=total)
        return ObjectSlice(
            key=key, offset=offset, data=blob[offset : offset + length], total_bytes=total
        )


def build_object_reader(*, bucket: str, project_id: str | None = None) -> ObjectReader | None:
    """The production reader, or None when no bucket is configured.

    None is returned rather than a reader that fails on use, so the routes can
    answer "the artifact store is not configured" -- a deployment problem with
    a named fix -- instead of "the read failed", which sends an operator
    looking at IAM.
    """
    if not bucket:
        return None
    return GcsObjectReader(bucket=bucket, project_id=project_id)
