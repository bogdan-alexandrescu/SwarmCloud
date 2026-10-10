"""Where a clone bundle lives in the tenant's own prefix, and its store operations (#940).

A step whose commit is already known (a workflow step's base pin, a carried
parent head) need not ask GitHub for it: some earlier step in the same tenant
cloned that sha and left a one-commit git bundle of it in the bucket. This
module is that bundle's place and its four store operations. Nothing here runs
git; the clone path decides what to bundle and what to do with one it reads.

THE LAYOUT, from the worker's own `cfg.tenant_id` and the registration's
repo_id (`indexrun.target`, the registration's recipe, so no URL text is ever
in a key):

    tenants/<tenant>/bundles/<repo_id>/<sha>.swarm-clone.bundle
    tenants/<tenant>/bundles/<repo_id>/heads/<sha256(ref)[:32]>.swarm-clone.head

A head object holds the last bundled sha of one branch, for a branch-tip step
that seeds from it and fetches only the delta from the forge.

ISOLATION (invariant 9). The tenant segment is the worker's own, never the
task's input, and `tenants/<tenant>/` is already the tenant's service
account's read/write grant and nobody else's, so a bundle is read only by the
tenant that wrote it. Two tenants cloning one repository get two repo_ids and
two objects. No IAM change is needed.

EXPIRY. Bundles are cache, not record: they are deliberately NOT under
`repos/` (`objectstore.is_repos_key`), which is off the expiry clock, so
every upload is stamped with customTime like any artifact. The bucket's
lifecycle rule that expires them sooner keys on BUNDLE_SUFFIX and HEAD_SUFFIX,
which are distinctive on purpose: the rule is bucket-wide (personal `u-<slug>`
tenants are in no Terraform map) and must never catch an agent artifact that
happens to be named `*.bundle`. Terraform holds the same two strings, and a
parity test holds them equal.

WRITE ONCE. "Written once per sha, by whoever clones first" is the
`ifGenerationMatch=0` precondition of `upload_file_if_absent`: a second writer
is told False and the first object stands. A head pointer is the one object
that is overwritten, because a branch moves.

NO TOKEN. A bundle's object carries a content type and nothing else -- no
custom metadata -- so no forge token can reach its metadata. What goes into
the bundle file itself is the clone path's to keep clean (a bundle holds
objects and refs, never the remote's URL or config).

NOTHING HERE FAILS A STEP. A key that cannot be built is a `NoKey` with a
reason; an absent, oversized or unreadable bundle is a logged miss (None); a
publish that cannot happen is False. The caller falls back to today's clone.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .indexrun import target
from .objectstore import ObjectStore

#: The suffixes the bucket's bundle lifecycle rule matches. Held equal to
#: Terraform's by a parity test; change both or neither.
BUNDLE_SUFFIX = ".swarm-clone.bundle"
HEAD_SUFFIX = ".swarm-clone.head"

#: The tenant's own segment that holds every bundle: `tenants/<t>/bundles/`.
BUNDLES_SEGMENT = "bundles"
HEADS_SEGMENT = "heads"

#: The one piece of object metadata a bundle carries.
BUNDLE_CONTENT_TYPE = "application/x-git-bundle"
HEAD_CONTENT_TYPE = "text/plain"

#: How many hex characters of sha256(ref) name a head object: 128 bits, so
#: two branches of one repository never share a pointer.
HEAD_NAME_HEX = 32

_SHA = re.compile(r"^[0-9a-f]{40}$")
_TENANT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
#: A head object is one sha and a newline; anything longer is not ours.
_HEAD_MAX_BYTES = 128


@dataclass(frozen=True)
class NoKey:
    """Why there is no bundle key: a reason, never an exception."""

    reason: str


def _prefix(tenant_id: str, repository_url: str | None) -> str | NoKey:
    if not _TENANT.match(tenant_id or ""):
        return NoKey("the worker's tenant id is not a single path segment")
    where = target(tenant_id, repository_url)
    if isinstance(where, str):
        return NoKey(where)
    return f"tenants/{tenant_id}/{BUNDLES_SEGMENT}/{where.repo_id}"


def bundle_key(tenant_id: str, repository_url: str | None, sha: str | None) -> str | NoKey:
    """`tenants/<tenant>/bundles/<repo_id>/<sha>.swarm-clone.bundle`, or why not."""
    if not _SHA.match(sha or ""):
        return NoKey("the commit is not a 40-hex sha")
    prefix = _prefix(tenant_id, repository_url)
    if isinstance(prefix, NoKey):
        return prefix
    return f"{prefix}/{sha}{BUNDLE_SUFFIX}"


def head_key(tenant_id: str, repository_url: str | None, ref: str | None) -> str | NoKey:
    """`tenants/<tenant>/bundles/<repo_id>/heads/<sha256(ref)[:32]>.swarm-clone.head`,
    or why not. The ref is hashed, so a branch name never reaches a key."""
    if not ref:
        return NoKey("the step names no branch")
    prefix = _prefix(tenant_id, repository_url)
    if isinstance(prefix, NoKey):
        return prefix
    name = hashlib.sha256(ref.encode("utf-8")).hexdigest()[:HEAD_NAME_HEX]
    return f"{prefix}/{HEADS_SEGMENT}/{name}{HEAD_SUFFIX}"


def _note(log: Any, message: str, **fields: Any) -> None:
    if log is not None:
        log.info(message, **fields)


def fetch_bundle(
    store: ObjectStore, key: str, destination: Path, max_bytes: int, log: Any = None
) -> int | None:
    """Download the bundle at `key` to `destination`: its size, or None on a miss.

    Absent, larger than `max_bytes`, or any store error is a miss, logged,
    with nothing left at `destination`. Never raises: the step clones from
    the forge instead. The size is checked after the download because the
    store has no stat; `publish_bundle` refuses anything over the same cap,
    so an oversized bundle exists only if the cap was lowered since.
    """
    if not key.endswith(BUNDLE_SUFFIX):
        _note(log, "clone bundle miss", key=key, reason="not a bundle key")
        return None
    try:
        size = store.download_file(key, destination)
    except Exception as exc:  # noqa: BLE001 -- every failure is a miss
        destination.unlink(missing_ok=True)
        _note(log, "clone bundle miss", key=key, reason=type(exc).__name__, error=str(exc))
        return None
    if size > max_bytes:
        destination.unlink(missing_ok=True)
        _note(log, "clone bundle miss", key=key, reason="over the size cap",
              size_bytes=size, max_bytes=max_bytes)
        return None
    return size


def read_head(store: ObjectStore, key: str, log: Any = None) -> str | None:
    """The last bundled sha of the branch at `key`, or None (absent, garbled, error)."""
    try:
        raw = store.download_bytes(key)
    except Exception as exc:  # noqa: BLE001 -- every failure is a miss
        _note(log, "clone bundle head miss", key=key, reason=type(exc).__name__)
        return None
    if len(raw) > _HEAD_MAX_BYTES:
        return None
    sha = raw.decode("ascii", errors="replace").strip()
    return sha if _SHA.match(sha) else None


def publish_bundle(
    store: ObjectStore,
    key: str,
    source: Path,
    max_bytes: int | None = None,
    log: Any = None,
) -> bool:
    """Write the bundle once. True when this call wrote it; False when one was
    already there, it is over `max_bytes`, or the write failed (logged)."""
    if not key.endswith(BUNDLE_SUFFIX):
        return False
    try:
        size = Path(source).stat().st_size
        if max_bytes is not None and size > max_bytes:
            _note(log, "clone bundle not published", key=key, reason="over the size cap",
                  size_bytes=size, max_bytes=max_bytes)
            return False
        return bool(store.upload_file_if_absent(key, source, BUNDLE_CONTENT_TYPE))
    except Exception as exc:  # noqa: BLE001 -- a cache write never fails the step
        _note(log, "clone bundle not published", key=key, reason=type(exc).__name__,
              error=str(exc))
        return False


def publish_head(store: ObjectStore, key: str, sha: str, log: Any = None) -> bool:
    """Point the branch's head at `sha` (overwritten: the branch moves). False
    for a sha that is not 40 hex or a failed write (logged)."""
    if not key.endswith(HEAD_SUFFIX) or not _SHA.match(sha or ""):
        return False
    try:
        store.upload_bytes(key, f"{sha}\n".encode("ascii"), HEAD_CONTENT_TYPE)
    except Exception as exc:  # noqa: BLE001 -- a cache write never fails the step
        _note(log, "clone bundle head not published", key=key, reason=type(exc).__name__)
        return False
    return True
