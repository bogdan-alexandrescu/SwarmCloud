"""Checkpoint and log inspection: the read models behind two routes.

This is the server-side seam that did not exist. The worker has written
checkpoints and logs into the artifact bucket since the first attempt ever ran
(`agent_worker.checkpoint.CheckpointManager.create`,
`agent_worker.lifecycle._upload_outputs`, `_publish_live_logs`) and nothing
served either, so "why did this agent fail" was answerable only by someone with
a gcloud session and the key layout memorised.

THE LAYOUT IS RESTATED HERE, NOT IMPORTED
-----------------------------------------
`images/swarm-api/Dockerfile` installs `swarm-api` and `swarm-common` and
nothing else, so `agent_worker` is not importable in the deployed API and an
import of it would fail at runtime in the one environment that matters.
`reconciler.checkpoints` restated the same layout for the same reason and
documented it; this module follows that precedent, and
`tests/unit/control_plane/test_checkpoint_read_path.py` compares these
constants against `agent_worker.checkpoint.checkpoint_prefix` directly, because
every rule restated twice in this repository has since drifted.

THE TENANT BOUNDARY (invariant 9)
---------------------------------
Every prefix built here starts from `tenant_id` as supplied by the route's
`tenant_scope` dependency and from a `task_id` that `Store.get_task` has
already confirmed belongs to that tenant. Nothing a caller sends contributes to
the prefix above the attempt segment, and the segments that ARE caller-supplied
go through `objects.safe_segment` before they are interpolated. There is no
parameter -- task id, attempt id, page token, stream name -- that moves the
prefix out of `tenants/<caller's tenant>/`.

THREE ANSWERS, NEVER TWO
------------------------
Everything here distinguishes:

  * present   -- we read it, here it is (possibly zero bytes, which is present);
  * absent    -- we looked and it is not there;
  * unreadable-- we could not look, and nothing may be concluded.

A prefix listing that fails raises through to a 503, because there is no
partial answer to give and `{"checkpoints": []}` with a 200 is the exact shape
this repository has spent two days removing. A single object that fails inside
an otherwise successful listing is reported per-item, because the rest of the
answer is real.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from typing import Any

from swarm_common.models import Attempt, Task

from .errors import UpstreamUnavailable, ValidationFailed
from .objects import (
    ObjectAbsent,
    ObjectInfo,
    ObjectReader,
    ObjectUnreadable,
    UnsafeKeySegment,
    safe_segment,
)
from .redaction import RULES as REDACTION_RULES, redact, redact_detail
from .store import Store

# --------------------------------------------------------------------------
# The layout. Compared against the worker's own by the unit tests.
# --------------------------------------------------------------------------

#: Everything this platform writes lives under this root.
TENANTS_ROOT = "tenants/"
#: `agent_worker.checkpoint.checkpoint_prefix` -> `.../checkpoints/<id>`.
CHECKPOINTS_SEGMENT = "checkpoints"
#: `agent_worker.config.WorkerConfig.log_prefix` -> `.../logs`.
LOGS_SEGMENT = "logs"
#: `agent_worker.lifecycle._publish_live_logs` -> `.../logs/live/<stream>.tail.log`.
LIVE_SEGMENT = "live"
#: Written LAST by the worker, and therefore the commit marker: a checkpoint
#: prefix without one was never finished and no restore will ever select it.
MANIFEST_NAME = "manifest.json"
ARCHIVE_NAME = "archive.tar.gz"

STREAMS = ("stdout", "stderr")

#: `_publish_live_logs` prefixes each tail with this so a reader can tell a gap
#: from a continuation. It is metadata, not output, so it is parsed out of the
#: content and served as a structured field instead of being shown as a log line.
_TAIL_HEADER = re.compile(r"^#swarm-tail offset=(\d+) size=(\d+)\n")

#: A manifest is a small JSON document. This cap is what stops a hand-written or
#: corrupted object at that key turning a list request into a large download.
MANIFEST_MAX_BYTES = 64 * 1024


def attempts_prefix(*, tenant_id: str, task_id: str) -> str:
    """The ONLY prefix a checkpoint of this task can live under.

    Identical to `agent_worker.checkpoint.attempts_prefix`, which is what
    `CheckpointManager.find_latest` scans -- so "what this route lists" and
    "what a resuming worker would consider" are the same set by construction.
    """
    return f"{TENANTS_ROOT}{tenant_id}/tasks/{task_id}/attempts/"


def attempt_prefix(*, tenant_id: str, task_id: str, attempt_id: str) -> str:
    return f"{attempts_prefix(tenant_id=tenant_id, task_id=task_id)}{attempt_id}"


def pointer_to_prefix(pointer: Any) -> str | None:
    """Normalise `task.latest_checkpoint` to a bucket-relative checkpoint prefix.

    The pointer is a Firestore field, so it is DATA and not an instruction --
    the same reading `CheckpointManager.find_by_uri` and
    `reconciler.checkpoints.pointer_to_prefix` take. It is written as a store
    URI (`gs://bucket/tenants/...`, or `file:///...` under
    `LOCAL_ARTIFACT_ROOT`), so slicing from `tenants/` yields the key whatever
    wrote it.

    The caller compares the result as a WHOLE prefix and never as a substring:
    `ckpt-00001` and `ckpt-000010` share a string prefix, and a pointer test
    matching on that would mark the wrong checkpoint as the current one.
    """
    if not isinstance(pointer, str) or not pointer:
        return None
    marker = pointer.find(TENANTS_ROOT)
    if marker < 0:
        return None
    candidate = pointer[marker:].rstrip("/")
    if candidate.endswith(f"/{MANIFEST_NAME}"):
        candidate = candidate[: -len(MANIFEST_NAME) - 1]
    return candidate or None


@dataclass(frozen=True)
class CheckpointRef:
    """One checkpoint, identified entirely by where it sits in the bucket."""

    tenant_id: str
    task_id: str
    attempt_id: str
    checkpoint_id: str
    #: `tenants/<t>/tasks/<task>/attempts/<attempt>/checkpoints/<id>`, no slash.
    prefix: str

    @property
    def manifest_key(self) -> str:
        return f"{self.prefix}/{MANIFEST_NAME}"

    @property
    def archive_key(self) -> str:
        return f"{self.prefix}/{ARCHIVE_NAME}"


def parse_checkpoint_key(key: str, *, tenant_id: str, task_id: str) -> CheckpointRef | None:
    """Recover the checkpoint an object key belongs to, or None.

    None means "not a checkpoint object" -- an artifact, a log, or a layout
    this function does not recognise -- and the caller ignores such keys
    entirely. A parser that guessed would put a tenant's artifacts on a
    checkpoint screen the day the layout gains a segment.

    The tenant and task are CHECKED here rather than merely parsed. The listing
    was made under a prefix that already pins both, so a key that disagrees
    cannot arise from a correct store; refusing it anyway is what makes the
    boundary hold even if the store is wrong, which is the only kind of check
    worth having on an isolation boundary.
    """
    parts = key.split("/")
    # tenants/<t>/tasks/<task>/attempts/<attempt>/checkpoints/<id>/<object...>
    if len(parts) < 9:
        return None
    if (
        parts[0] != "tenants"
        or parts[2] != "tasks"
        or parts[4] != "attempts"
        or parts[6] != CHECKPOINTS_SEGMENT
    ):
        return None
    if any(not part for part in parts[1:8]):
        return None
    if parts[1] != tenant_id or parts[3] != task_id:
        return None
    return CheckpointRef(
        tenant_id=parts[1],
        task_id=parts[3],
        attempt_id=parts[5],
        checkpoint_id=parts[7],
        prefix="/".join(parts[:8]),
    )


# --------------------------------------------------------------------------
# Paging
# --------------------------------------------------------------------------

#: The ordering key: (attempt created_at, attempt id, checkpoint id), compared
#: as a TUPLE and never as a joined string. A joined string needs a separator,
#: and every separator that is printable sorts somewhere among the characters
#: an ISO timestamp is made of -- `"|"` is above every digit, so an attempt with
#: no timestamp sorted FIRST under a descending sort instead of last. A tuple
#: has no separator to get wrong.
SortKey = tuple[str, str, str]


def _encode_cursor(sort_key: SortKey) -> str:
    raw = json.dumps(list(sort_key), separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(token: str | None) -> SortKey | None:
    """Decode a page token, or refuse it.

    A token that cannot be decoded is REFUSED rather than treated as absent.
    Silently restarting from the first page on a malformed cursor is how a
    client that mangles a token gets an infinite list of the same rows and
    never learns why.
    """
    if token is None or token == "":
        return None
    padded = token + "=" * (-len(token) % 4)
    try:
        parts = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        if not isinstance(parts, list) or len(parts) != 3:
            raise ValueError("a cursor is a three-part ordering key")
        return (str(parts[0]), str(parts[1]), str(parts[2]))
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise ValidationFailed("page_token is not a token this endpoint issued") from None


# --------------------------------------------------------------------------
# The service
# --------------------------------------------------------------------------

class InspectionService:
    """Read models over the artifact bucket, scoped to one tenant per call.

    Constructed once in `deps.build_context`. It holds no client: the reader it
    is given builds its Google client lazily on first use, so this class is
    importable and constructible with no credentials.
    """

    def __init__(
        self,
        *,
        store: Store,
        objects: ObjectReader | None,
        scan_limit: int = 5000,
        max_log_bytes: int = 1024 * 1024,
        default_log_bytes: int = 64 * 1024,
        min_log_bytes: int = 4 * 1024,
    ) -> None:
        self._store = store
        self._objects = objects
        self._scan_limit = scan_limit
        self._max_log_bytes = max_log_bytes
        self._default_log_bytes = default_log_bytes
        self._min_log_bytes = min(min_log_bytes, max_log_bytes)

    # -- shared ------------------------------------------------------------
    def _reader(self) -> ObjectReader:
        if self._objects is None:
            # Not "the read failed": nothing was ever configured to read from.
            # The two need different sentences because they have different
            # fixes -- one is a deployment variable, the other is IAM.
            raise UpstreamUnavailable(
                "no artifact store is configured for this deployment, so "
                "checkpoints and logs cannot be served; set ARTIFACT_BUCKET"
            )
        return self._objects

    @staticmethod
    def _segment(value: str, *, what: str) -> str:
        try:
            return safe_segment(value, what=what)
        except UnsafeKeySegment as exc:
            raise ValidationFailed(str(exc)) from None

    def _scoped(self, tenant_id: str, task_id: str) -> tuple[Task, str]:
        """Resolve the task inside the caller's tenant and return its prefix.

        `get_task` raises the SAME 404 for a task in another tenant as for one
        that does not exist, deliberately: a different status would confirm the
        id exists elsewhere. Because it runs first, every key built below is
        already inside a tenant the caller belongs to.

        The ids are re-validated as path segments afterwards even though one
        came from Firestore. A document id cannot contain a slash today; the
        check costs nothing and is the difference between a boundary that holds
        by construction and one that holds by a property of another system.
        """
        task = self._store.get_task(tenant_id, task_id)
        tenant = self._segment(task.tenant_id, what="tenant id")
        task_key = self._segment(task.id, what="task id")
        return task, attempts_prefix(tenant_id=tenant, task_id=task_key)

    def _list(self, prefix: str):
        try:
            return self._reader().list_objects(prefix, limit=self._scan_limit)
        except ObjectUnreadable as exc:
            # There is no honest partial answer to a failed listing, so this
            # becomes a 503 rather than an empty array with a 200.
            raise UpstreamUnavailable(
                "the artifact store could not be listed, so it is not known "
                "what this task has written: " + redact_detail(exc.reason)
            ) from None

    def _attempt_order(self, tenant_id: str, task_id: str) -> dict[str, Attempt]:
        """Attempt documents by id, for ordering and for `attempt_known`.

        Ordering checkpoints chronologically ACROSS attempts would otherwise
        need every manifest downloaded, because the key carries no timestamp
        and attempt ids are random. The attempt documents already hold
        `created_at`, and the API can already read them, so the order comes
        from there and the manifests are only fetched for the page actually
        being returned.
        """
        try:
            rows = self._store.list_attempts(tenant_id, task_id, limit=self._scan_limit)
        except Exception as exc:  # noqa: BLE001
            raise UpstreamUnavailable(
                "the attempt documents could not be read, so checkpoints "
                "cannot be ordered: " + redact_detail(f"{type(exc).__name__}: {exc}")
            ) from None
        return {a.attempt_id: a for a in rows}

    # -- checkpoints -------------------------------------------------------
    def list_checkpoints(
        self,
        tenant_id: str,
        task_id: str,
        *,
        attempt_id: str | None = None,
        limit: int,
        page_token: str | None = None,
    ) -> dict[str, Any]:
        """Every checkpoint of one task, newest attempt first.

        ACROSS ATTEMPTS, not just the current one, because that is what a
        resume actually considers: `CheckpointManager.find_latest` scans the
        whole `.../attempts/` prefix and takes the newest committed manifest,
        so a checkpoint written by the attempt that died is the one a retry
        restores from. A screen that showed only the live attempt's checkpoints
        would omit the only one that matters after a crash.

        WHAT `resumable` MEANS. Exactly what `CheckpointManager._owns` plus
        `restore` would accept: a manifest exists and parses, it names THIS
        tenant and THIS task, both of its keys sit under this task's own
        prefix, and the archive it points at is present at the size it claims.
        It is `null`, never `false`, when the manifest could not be read --
        "cannot resume" and "cannot tell" are different answers and the UI
        prints different sentences for them.

        WHAT IS NOT DONE HERE: the archive is never opened. `file_count` and
        `archive_bytes` come from the manifest and the object listing, which is
        what lets a UI say what a checkpoint contains without downloading a
        gigabyte to find out. Verifying `archive_sha256` would mean downloading
        it, so the digest is served for a caller who wants to check it and is
        not checked here.
        """
        task, prefix = self._scoped(tenant_id, task_id)
        scope = prefix
        if attempt_id is not None:
            # Narrowing only. The attempt segment cannot widen the prefix: it
            # is appended to a prefix that already pins tenant and task, and it
            # is validated as a single segment first.
            scope = prefix + self._segment(attempt_id, what="attempt_id") + "/"

        listing = self._list(scope)
        cursor = _decode_cursor(page_token)
        attempts = self._attempt_order(tenant_id, task_id)

        by_prefix: dict[str, list[ObjectInfo]] = {}
        refs: dict[str, CheckpointRef] = {}
        for info in listing.objects:
            ref = parse_checkpoint_key(info.key, tenant_id=task.tenant_id, task_id=task.id)
            if ref is None:
                continue  # an artifact, a log, or a layout this parser does not own
            by_prefix.setdefault(ref.prefix, []).append(info)
            refs[ref.prefix] = ref

        def sort_key(ref: CheckpointRef) -> SortKey:
            attempt = attempts.get(ref.attempt_id)
            stamp = attempt.created_at.isoformat() if attempt is not None else ""
            # Unknown attempts get an empty stamp, which sorts lowest and so
            # lands LAST under a descending sort -- visible, at the end, rather
            # than dropped or silently first.
            return (stamp, ref.attempt_id, ref.checkpoint_id)

        ordered = sorted(refs.values(), key=sort_key, reverse=True)

        pointer_prefix = pointer_to_prefix(task.latest_checkpoint)
        pointer_status = self._pointer_status(task, pointer_prefix, prefix, set(refs))

        if cursor is not None:
            ordered = [r for r in ordered if sort_key(r) < cursor]
        page = ordered[:limit]
        next_token = _encode_cursor(sort_key(page[-1])) if len(ordered) > limit and page else None

        reader = self._reader()
        rows = [
            self._checkpoint_row(
                ref,
                objects=sorted(by_prefix[ref.prefix], key=lambda o: o.key),
                attempt=attempts.get(ref.attempt_id),
                own_prefix=prefix,
                pointer_prefix=pointer_prefix,
                reader=reader,
            )
            for ref in page
        ]

        return {
            "task_id": task.id,
            "tenant_id": task.tenant_id,
            "prefix": reader.uri(scope),
            "checkpoints": rows,
            "count": len(rows),
            "total_found": len(refs),
            "next_page_token": next_token,
            # The listing SUCCEEDED -- this is what makes `checkpoints: []`
            # readable as "this task has written none" rather than as a failure
            # that happened to serialise as an empty array.
            "listed": True,
            # True when the scan limit cut the prefix short. A screen showing
            # the first N of M and a screen showing all of them are otherwise
            # identical.
            "truncated": listing.truncated,
            "latest_checkpoint": pointer_status,
        }

    def _pointer_status(
        self,
        task: Task,
        pointer_prefix: str | None,
        own_prefix: str,
        known: set[str],
    ) -> dict[str, Any]:
        """What `task.latest_checkpoint` currently names.

        Four outcomes, and they are not interchangeable. `outside_this_task` in
        particular is a real finding rather than a formatting detail: it is a
        pointer a resuming worker would REFUSE (`find_by_uri` resolves only
        inside this task's own prefix and falls back to `find_latest`), so a
        UI that rendered it as the current checkpoint would show a restore
        source that will never be used.
        """
        raw = task.latest_checkpoint
        if not raw:
            return {"pointer": None, "status": "unset", "checkpoint_id": None}
        if pointer_prefix is None or not pointer_prefix.startswith(own_prefix):
            return {
                "pointer": raw,
                "status": "outside_this_task",
                "checkpoint_id": None,
                "detail": (
                    "the pointer does not name a checkpoint under this task's own "
                    "prefix, so a resuming worker would ignore it and fall back to "
                    "the newest committed checkpoint it can find"
                ),
            }
        if pointer_prefix not in known:
            return {
                "pointer": raw,
                "status": "missing",
                "checkpoint_id": pointer_prefix.rsplit("/", 1)[-1],
                "detail": (
                    "the pointer names a checkpoint of this task that is no longer "
                    "in the bucket; it may have been reclaimed"
                ),
            }
        return {
            "pointer": raw,
            "status": "present",
            "checkpoint_id": pointer_prefix.rsplit("/", 1)[-1],
        }

    def _checkpoint_row(
        self,
        ref: CheckpointRef,
        *,
        objects: list[ObjectInfo],
        attempt: Attempt | None,
        own_prefix: str,
        pointer_prefix: str | None,
        reader: ObjectReader,
    ) -> dict[str, Any]:
        contents = [{"name": o.name, "key": o.key, "bytes": o.size} for o in objects]
        sizes = {o.key: o.size for o in objects}
        row: dict[str, Any] = {
            "checkpoint_id": ref.checkpoint_id,
            "attempt_id": ref.attempt_id,
            # False does NOT mean the attempt never happened -- it means no
            # attempt document was found, which after a purge or a partial
            # write is a different thing from an attempt that did not run.
            "attempt_known": attempt is not None,
            "attempt_created_at": attempt.created_at if attempt is not None else None,
            "attempt_completed_at": attempt.completed_at if attempt is not None else None,
            "prefix": ref.prefix,
            "uri": reader.uri(ref.prefix + "/"),
            "is_latest_pointer": pointer_prefix == ref.prefix,
            "objects": contents,
            "stored_bytes": sum(sizes.values()),
            # Filled in below, or left as the unknown they are.
            "manifest": "unreadable",
            "manifest_detail": None,
            "created_at": None,
            "seq": None,
            "generation": None,
            "label": None,
            "archive_bytes": None,
            "archive_sha256": None,
            "file_count": None,
            "resumable": None,
            "resumable_detail": None,
        }

        if ref.manifest_key not in sizes:
            # The manifest is the commit marker and is uploaded LAST, so a
            # prefix without one was never a finished checkpoint. This is an
            # ABSENCE we can prove, not a read we failed.
            row["manifest"] = "absent"
            row["manifest_detail"] = (
                "no manifest.json: this checkpoint was never committed, so no "
                "restore would select it"
            )
            row["resumable"] = False
            row["resumable_detail"] = "the checkpoint was never committed"
            return row

        try:
            chunk = reader.read_range(ref.manifest_key, offset=0, length=MANIFEST_MAX_BYTES)
        except ObjectAbsent:
            # It was in the listing a moment ago. Concurrent reclamation is the
            # ordinary explanation, and it is still an absence rather than a
            # failure.
            row["manifest"] = "absent"
            row["manifest_detail"] = "the manifest disappeared between the listing and the read"
            row["resumable"] = False
            row["resumable_detail"] = "the manifest is gone"
            return row
        except ObjectUnreadable as exc:
            row["manifest_detail"] = redact_detail(exc.reason)
            row["resumable"] = None  # NOT False: we could not look.
            row["resumable_detail"] = "the manifest could not be read, so nothing is known"
            return row

        try:
            data = json.loads(chunk.data.decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("a manifest must be a JSON object")
        except (UnicodeDecodeError, ValueError) as exc:
            row["manifest_detail"] = f"the manifest is not readable JSON: {redact_detail(exc)}"
            row["resumable"] = None
            row["resumable_detail"] = "the manifest could not be parsed, so nothing is known"
            return row

        row["manifest"] = "present"
        row["created_at"] = _as_text(data.get("created_at"))
        row["seq"] = _as_int(data.get("seq"))
        row["generation"] = _as_int(data.get("generation"))
        row["label"] = _as_text(data.get("label"))
        row["archive_bytes"] = _as_int(data.get("archive_bytes"))
        row["archive_sha256"] = _as_text(data.get("archive_sha256"))
        row["file_count"] = _as_int(data.get("file_count"))

        ok, why = _resumable(
            data,
            ref=ref,
            own_prefix=own_prefix,
            archive_size=sizes.get(ref.archive_key),
        )
        row["resumable"] = ok
        row["resumable_detail"] = why
        return row

    # -- logs --------------------------------------------------------------
    def read_logs(
        self,
        tenant_id: str,
        task_id: str,
        *,
        attempt_id: str | None = None,
        stream: str | None = None,
        source: str = "auto",
        offset: int = 0,
        limit_bytes: int | None = None,
    ) -> dict[str, Any]:
        """A window of an attempt's captured output, REDACTED at read time.

        WHICH OBJECT. The worker writes each stream twice and they are not the
        same thing:

          * `.../logs/<stream>.log` is the complete record, uploaded once by
            `_upload_outputs` when the attempt finishes. It does not exist for a
            run that is still going, and it never exists for one that was killed
            before it could upload.
          * `.../logs/live/<stream>.tail.log` is a bounded window republished
            every `live_log_interval_seconds` while the agent runs. It is the
            only thing that exists mid-run and the only thing that survives a
            worker that never reached its upload.

        `source=auto` prefers the complete record and falls back to the tail. It
        does NOT fall back when the complete record is UNREADABLE -- only when
        it is absent -- because falling back on a failure would serve a partial
        window while reporting success, which is the same class of lie as an
        empty list.

        REDACTION. Unconditional, on every byte, whatever happened at write
        time; see `swarm_api.redaction` for why the worker's pass cannot be
        relied on. Two consequences worth knowing:

          * the byte accounting (`offset`, `next_offset`, `total_bytes`) refers
            to the RAW object, not to the redacted text, because the redacted
            text is shorter and an offset derived from it would desynchronise
            paging;
          * a window never contains a PARTIAL TOKEN at either end -- see
            `_align`. A credential cut in half by paging matches no pattern in
            either half, so the boundaries are moved to whitespace before
            anything is served. `limit_bytes` is clamped UP to
            `min_log_bytes` for the same reason: a window too small to hold a
            line can only ever be withheld.
        """
        task, prefix = self._scoped(tenant_id, task_id)
        if offset < 0:
            raise ValidationFailed("offset must not be negative")
        if source not in ("auto", "final", "live"):
            raise ValidationFailed("source must be one of: auto, final, live")
        if stream is not None and stream not in STREAMS:
            raise ValidationFailed(f"stream must be one of: {', '.join(STREAMS)}")
        window = self._default_log_bytes if limit_bytes is None else limit_bytes
        if window < 1:
            raise ValidationFailed("limit_bytes must be at least 1")
        window = min(max(window, self._min_log_bytes), self._max_log_bytes)

        attempts = self._attempt_order(tenant_id, task_id)
        chosen, attempt_status = self._choose_attempt(attempts, attempt_id)

        wanted = STREAMS if stream is None else (stream,)
        reader = self._reader()
        entries: list[dict[str, Any]] = []
        if chosen is None:
            for name in wanted:
                entries.append(_no_attempt_entry(name, attempt_status))
        else:
            base = attempt_prefix(
                tenant_id=task.tenant_id, task_id=task.id, attempt_id=chosen
            )
            for name in wanted:
                entries.append(
                    self._stream_entry(
                        base=base,
                        stream=name,
                        source=source,
                        offset=offset,
                        window=window,
                        reader=reader,
                    )
                )

        record = attempts.get(chosen) if chosen else None
        return {
            "task_id": task.id,
            "tenant_id": task.tenant_id,
            "attempt_id": chosen,
            "attempt": {
                "status": attempt_status,
                "known": record is not None,
                "generation": record.generation if record is not None else None,
                "created_at": record.created_at if record is not None else None,
                "completed_at": record.completed_at if record is not None else None,
                "exit_code": record.exit_code if record is not None else None,
            },
            "streams": entries,
            "prefix": reader.uri(prefix),
            "redaction": {
                # Stated in the payload so a reader never has to assume it, and
                # so a deployment where it somehow stopped is visible.
                "applied_at_read_time": True,
                "rules": len(REDACTION_RULES),
            },
        }

    def _choose_attempt(
        self, attempts: dict[str, Attempt], requested: str | None
    ) -> tuple[str | None, str]:
        """Which attempt's logs to read, and how that was decided.

        A requested id is NOT required to have an attempt document. The prefix
        it builds is `tenants/<caller's tenant>/tasks/<their task>/attempts/<id>`
        whatever the id is, so an unknown one can only ever address an empty
        corner of the caller's own prefix -- and refusing it would make the
        logs of an attempt whose document was purged permanently unreachable
        while the objects are still there. It is reported as `unknown_attempt`
        instead, so the caller knows the control plane has no record of it.
        """
        if requested is not None:
            self._segment(requested, what="attempt_id")
            return requested, ("requested" if requested in attempts else "unknown_attempt")
        if not attempts:
            return None, "no_attempt_yet"
        newest = max(attempts.values(), key=lambda a: (a.created_at, a.attempt_id))
        return newest.attempt_id, "latest"

    def _stream_entry(
        self,
        *,
        base: str,
        stream: str,
        source: str,
        offset: int,
        window: int,
        reader: ObjectReader,
    ) -> dict[str, Any]:
        final_key = f"{base}/{LOGS_SEGMENT}/{stream}.log"
        live_key = f"{base}/{LOGS_SEGMENT}/{LIVE_SEGMENT}/{stream}.tail.log"

        order: list[tuple[str, str]]
        if source == "final":
            order = [("final", final_key)]
        elif source == "live":
            order = [("live", live_key)]
        else:
            order = [("final", final_key), ("live", live_key)]

        # ONE BYTE OF OVERLAP when the caller is not starting at the beginning.
        # It is what tells `_align` whether `offset` lands on a token boundary
        # (the previous byte is whitespace) or inside a token. Without it the
        # head rule has to assume the worst on every page and would eat the
        # first token of every window after the first, losing data.
        probe = 1 if offset > 0 else 0

        last_absent: dict[str, Any] | None = None
        for label, key in order:
            try:
                chunk = reader.read_range(key, offset=offset - probe, length=window + probe)
            except ObjectAbsent:
                last_absent = _absent_entry(stream, label, key, reader.uri(key))
                continue  # try the next source, if `auto` left one
            except ObjectUnreadable as exc:
                # Deliberately NOT falling through to the live tail. A failure
                # is an answer, and substituting a different object for it
                # would report success over a window that is not the one asked
                # for.
                return _unreadable_entry(stream, label, key, reader.uri(key), exc.reason)
            return self._served(
                stream, label, key, reader.uri(key), chunk, offset=offset, probe=probe
            )

        assert last_absent is not None
        if source == "auto":
            last_absent["detail"] = (
                "neither the completed log nor a live tail exists for this attempt; "
                "nothing has been written yet, or the objects have been removed"
            )
        return last_absent

    def _served(
        self,
        stream: str,
        label: str,
        key: str,
        uri: str,
        chunk: Any,
        *,
        offset: int,
        probe: int,
    ) -> dict[str, Any]:
        raw_full = chunk.data
        if not raw_full:
            # Paged past the end, or a zero-byte object. Both are an object
            # that EXISTS, which is a different answer from absent.
            row = _entry(stream, label, "ok")
            row.update(
                key=key, uri=uri, content="", total_bytes=chunk.total_bytes,
                offset=min(offset, chunk.total_bytes),
            )
            return row

        previous = raw_full[:probe]
        raw = raw_full[probe:]
        start = chunk.offset + probe

        tail_window = None
        if label == "live" and start == 0:
            match = _TAIL_HEADER.match(raw.decode("utf-8", errors="replace"))
            if match is not None:
                tail_window = {
                    "object_offset": int(match.group(1)),
                    "stream_size": int(match.group(2)),
                }
                raw = raw[match.end() :]
                start += match.end()

        at_eof = chunk.end >= chunk.total_bytes
        raw, start, end, withheld = _align(
            raw, start=start, previous=previous, at_eof=at_eof, read_end=chunk.end
        )

        scrubbed = redact(raw.decode("utf-8", errors="replace"))
        complete = end >= chunk.total_bytes
        row = _entry(stream, label, "ok")
        row.update(
            key=key,
            uri=uri,
            content=scrubbed.text,
            # Byte accounting is over the RAW object. The redacted string is
            # shorter, and paging on its length would drift.
            total_bytes=chunk.total_bytes,
            offset=start,
            returned_bytes=len(raw),
            next_offset=None if complete else end,
            truncated=not complete,
            redacted=scrubbed.any,
            redaction_count=scrubbed.count,
            tail_window=tail_window,
        )
        if withheld is not None:
            row["detail"] = withheld
        return row


# --------------------------------------------------------------------------
# Window alignment -- the rule that stops paging splitting a credential
# --------------------------------------------------------------------------

#: Everything a token can be bounded by. A credential is a whitespace-delimited
#: run of characters, so moving both ends of a window to whitespace is exactly
#: what guarantees no PARTIAL credential is ever served.
_WHITESPACE = b" \t\r\n\f\v"


def _last_boundary(data: bytes) -> int:
    """Index of the byte to cut AFTER: the last newline, else the last space.

    A newline is preferred so an ordinary log pages line by line, which is what
    a reader wants. Any whitespace will do for safety, and is the fallback for
    a stream that writes very long lines.
    """
    cut = data.rfind(b"\n")
    if cut >= 0:
        return cut
    return max(data.rfind(bytes([c])) for c in _WHITESPACE)


def _align(
    raw: bytes, *, start: int, previous: bytes, at_eof: bool, read_end: int
) -> tuple[bytes, int, int, str | None]:
    """Move both ends of the window onto token boundaries.

    THE ATTACK THIS CLOSES. `redact` is a set of patterns over the text it is
    given. A token cut in half by paging matches nothing in either half --
    `sk-proj-Aa1Bb2Cc3` and `Dd4Ee5Ff6Gg7Hh8` are, to every rule in the table,
    ordinary words. Redaction at read time is therefore only worth as much as
    the guarantee that it sees whole tokens, and this function is that
    guarantee. It was found by a test that swept every window size over a log
    containing a key, which is the only way this class of hole shows up.

    THE HEAD. `previous` is the single byte before `start`, read as overlap. If
    it is whitespace the window already begins at a token boundary and nothing
    is dropped -- which is what makes paging LOSSLESS, because the previous
    window ended just after that same whitespace. If it is not whitespace the
    caller chose an offset inside a token, and everything up to the next
    whitespace is dropped.

    THE TAIL. Unless the read reached the end of the object, the window is cut
    after the last boundary byte, so the trailing partial token is left for the
    next window. The boundary byte is KEPT, so concatenating every window
    reproduces the object exactly.

    WHEN THERE IS NO BOUNDARY AT ALL -- a single token longer than the window --
    nothing can be served safely, so nothing is: empty content, the offset
    advanced past what was read, and a `detail` saying why. It always makes
    progress, and it says what it did rather than returning a fragment.

    Returns `(bytes, start, end, detail)` where `start` and `end` are absolute
    offsets into the object.
    """
    detail = None
    if previous and previous not in _WHITESPACE:
        head = min(
            (i for i, byte in enumerate(raw) if bytes([byte]) in _WHITESPACE),
            default=-1,
        )
        if head < 0:
            return b"", start, read_end, (
                "this window began inside a token that is longer than the window, so "
                "nothing could be served without splitting it; raise limit_bytes"
            )
        raw = raw[head + 1 :]
        start += head + 1

    if at_eof:
        return raw, start, start + len(raw), detail

    cut = _last_boundary(raw)
    if cut < 0:
        return b"", start, read_end, (
            "this window fell inside a token longer than the window, so nothing "
            "could be served without splitting it; raise limit_bytes"
        )
    raw = raw[: cut + 1]
    return raw, start, start + len(raw), detail


# --------------------------------------------------------------------------
# Row helpers
# --------------------------------------------------------------------------

def _entry(stream: str, source: str | None, status: str) -> dict[str, Any]:
    """The constant shape every stream entry has, whatever happened.

    `content` is None -- never `""` -- in every non-ok case, so a client that
    renders `content` without reading `status` shows nothing rather than an
    empty log that looks like a successful read of a silent agent.
    """
    return {
        "stream": stream,
        "source": source,
        "status": status,
        "detail": None,
        "key": None,
        "uri": None,
        "content": None,
        "total_bytes": None,
        "offset": 0,
        "returned_bytes": 0,
        "next_offset": None,
        "truncated": False,
        "redacted": False,
        "redaction_count": 0,
        "tail_window": None,
    }


def _no_attempt_entry(stream: str, attempt_status: str) -> dict[str, Any]:
    row = _entry(stream, None, "absent")
    row["detail"] = (
        "this task has no attempt yet, so no log object can exist"
        if attempt_status == "no_attempt_yet"
        else "no attempt could be identified for this task"
    )
    return row


def _absent_entry(stream: str, source: str, key: str, uri: str) -> dict[str, Any]:
    row = _entry(stream, source, "absent")
    row["key"] = key
    row["uri"] = uri
    row["detail"] = "this object does not exist"
    return row


def _unreadable_entry(
    stream: str, source: str, key: str, uri: str, reason: str
) -> dict[str, Any]:
    row = _entry(stream, source, "unreadable")
    row["key"] = key
    row["uri"] = uri
    # Redacted like log content itself: a storage client's error string can
    # quote the request it failed on, and a signed URL is a credential.
    row["detail"] = redact_detail(reason)
    return row


def _as_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _as_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _resumable(
    manifest: dict[str, Any],
    *,
    ref: CheckpointRef,
    own_prefix: str,
    archive_size: int | None,
) -> tuple[bool, str]:
    """Would `CheckpointManager` restore from this, if a retry asked it to?

    Mirrors `_owns` and the first half of `restore`. The manifest was found in
    a bucket, which is not evidence of who wrote it, so both identifiers are
    compared and both keys are required to sit under this task's own prefix --
    a manifest naming the right task but pointing its archive elsewhere is
    refused here exactly as the worker refuses it.

    The digest is NOT verified: that needs the archive downloaded, which is the
    one thing a list endpoint must not do. `archive_sha256` is served so a
    caller who does download it can check.
    """
    if manifest.get("tenant_id") != ref.tenant_id or manifest.get("task_id") != ref.task_id:
        return False, (
            "the manifest names "
            f"{manifest.get('tenant_id')}/{manifest.get('task_id')}, not this task; "
            "a worker would refuse to restore it"
        )
    archive_key = manifest.get("archive_key")
    manifest_key = manifest.get("manifest_key")
    for label, value in (("archive_key", archive_key), ("manifest_key", manifest_key)):
        if not isinstance(value, str) or not value.startswith(own_prefix):
            return False, f"the manifest's {label} points outside this task's own prefix"
    if archive_size is None:
        return False, "the archive named by the manifest is not in the bucket"
    declared = _as_int(manifest.get("archive_bytes"))
    if declared is not None and declared != archive_size:
        return False, (
            f"the archive is {archive_size} bytes but the manifest declares "
            f"{declared}; it was truncated or replaced"
        )
    return True, "committed, owned by this task, and its archive is present at the declared size"
