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
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

from swarm_common.models import Attempt, Task, utcnow

from . import agent_streams as agent_streams_mod
from .errors import NotFound, UpstreamUnavailable, ValidationFailed
from .objects import (
    ObjectAbsent,
    ObjectInfo,
    ObjectReader,
    ObjectReplaced,
    ObjectSlice,
    ObjectUnreadable,
    UnsafeKeySegment,
    safe_segment,
)
from .redaction import (
    PEM_BLOCK_MAX_CHARS,
    RULES as REDACTION_RULES,
    open_key_start,
    redact_detail,
    redact_lines,
)
from .store import Store
from .task_input import masking_for

# --------------------------------------------------------------------------
# The layout. Compared against the worker's own by the unit tests.
# --------------------------------------------------------------------------

#: Everything this platform writes lives under this root.
TENANTS_ROOT = "tenants/"
#: `agent_worker.checkpoint.checkpoint_prefix` -> `.../checkpoints/<id>`.
CHECKPOINTS_SEGMENT = "checkpoints"
#: `agent_worker.config.WorkerConfig.log_prefix` -> `.../logs`.
LOGS_SEGMENT = "logs"
#: `agent_worker.config.WorkerConfig.artifact_prefix` -> `.../artifacts`, and
#: `lifecycle._upload_outputs` writes each file at `<artifact_prefix>/<rel>`
#: where `rel` is the manifest entry's `name`. That one line is the whole key
#: layout, and `read_artifact` REBUILDS the key from it rather than trusting
#: the `uri` the manifest carries.
ARTIFACTS_SEGMENT = "artifacts"
#: `agent_worker.lifecycle._publish_live_logs` -> `.../logs/live/<stream>.tail.log`.
LIVE_SEGMENT = "live"
#: Written LAST by the worker, and therefore the commit marker: a checkpoint
#: prefix without one was never finished and no restore will ever select it.
MANIFEST_NAME = "manifest.json"
ARCHIVE_NAME = "archive.tar.gz"

#: What `GET /v1/tasks/{id}/logs` serves when no `stream` is named: the RUNNER
#: process's two streams, exactly as before #184 (RunFiles and `swarm tail`
#: pass no stream and must keep getting these two).
STREAMS = ("stdout", "stderr")
#: Every stream the route can name. `agent_stdout` / `agent_stderr` are the
#: agent CLI's own (see `swarm_api.agent_streams`).
LOG_STREAMS = STREAMS + agent_streams_mod.AGENT_STREAMS

#: `_publish_live_logs` prefixes each tail with this so a reader can tell a gap
#: from a continuation. It is metadata, not output, so it is parsed out of the
#: content and served as a structured field instead of being shown as a log line.
#: ` at=<RFC 3339>` -- when the worker cut the window -- was added in #184 and is
#: OPTIONAL: every tail published before it has no `at`, and still parses.
#: Matched on BYTES, so the header's length in the match is its length in the
#: object and the byte accounting after it stays exact.
_TAIL_HEADER = re.compile(rb"^#swarm-tail offset=(\d+) size=(\d+)(?: at=(\S+))?\n")

#: A manifest is a small JSON document. This cap is what stops a hand-written or
#: corrupted object at that key turning a list request into a large download.
MANIFEST_MAX_BYTES = 64 * 1024

#: How much of an artifact's HEAD decides whether it can be served as text.
#: A NUL byte is the standard "this is not text" signal -- it is what `git
#: diff` uses -- and it is checked over a fixed prefix rather than over the
#: window being served, so the answer for one artifact does not depend on
#: which page of it was asked for.
ARTIFACT_SNIFF_BYTES = 4096

#: How far before its own start a page that does not begin at 0 reads (#188
#: review). A private key printed as text spans lines, and a page in the
#: middle of one holds neither of its markers -- only a look-back that finds
#: the BEGIN, and no END after it, says the page begins inside a key
#: (`redaction.open_key_start`). A key's END is looked for this far after its
#: BEGIN, so a BEGIN further back than this does not open a key here either.
#: The byte before the page -- all `_align` needs -- is the look-back's last.
KEY_LOOKBACK_BYTES = PEM_BLOCK_MAX_CHARS

#: One byte that is not part of any UTF-8 sequence, as `surrogateescape` decodes it.
_UNDECODABLE = re.compile("[\udc80-\udcff]")


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
        # An artifact is the thing a person actually asked the agent to
        # produce, so the default window is larger than a log's: a
        # `synthesis.md` arriving in four pages would read as four documents.
        # The ceiling is what stops one request becoming an unbounded
        # download, and it is REPORTED on every response rather than applied
        # quietly -- a silent truncation reads as "that was the whole output".
        max_artifact_bytes: int = 4 * 1024 * 1024,
        default_artifact_bytes: int = 512 * 1024,
        min_artifact_bytes: int = 4 * 1024,
        # The server clock every `read_at` and `age_seconds` is measured on.
        # Injected so a test can pin it; `deps.build_context` passes the
        # context's own.
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self._store = store
        self._objects = objects
        self._now = now
        self._scan_limit = scan_limit
        self._max_log_bytes = max_log_bytes
        self._default_log_bytes = default_log_bytes
        self._min_log_bytes = min(min_log_bytes, max_log_bytes)
        self._max_artifact_bytes = max_artifact_bytes
        self._default_artifact_bytes = min(default_artifact_bytes, max_artifact_bytes)
        self._min_artifact_bytes = min(min_artifact_bytes, max_artifact_bytes)

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
        stream: str | Sequence[str] | None = None,
        source: str = "auto",
        offset: int = 0,
        limit_bytes: int | None = None,
    ) -> dict[str, Any]:
        """A window of an attempt's captured output, REDACTED at read time.

        WHICH STREAMS (#184). `stream` may be named more than once. `stdout`
        and `stderr` are the RUNNER process's streams; `agent_stdout` and
        `agent_stderr` are the agent CLI's own (claude-code's stream-json
        transcript is `agent_stdout`). Naming none serves `stdout` and
        `stderr`, exactly as before. An agent stream of a runner with no agent
        child is `not_applicable`, a fourth answer beside ok, absent and
        unreadable. An agent stream of an attempt that ran before #184 has no
        `logs/` object of its own; when both are absent the manifest entry the
        runner wrote it to is served instead, as `source: "artifact"` (see
        `_open_stream`).

        AGE. `read_at` is this server's clock when the read began; each stream
        carries its object's `object_updated_at` (GCS `updated`) and
        `age_seconds` between the two, so a live tail says how old it is on
        the server's clock rather than the viewer's.

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
        wanted = _wanted_streams(stream)
        window = self._default_log_bytes if limit_bytes is None else limit_bytes
        if window < 1:
            raise ValidationFailed("limit_bytes must be at least 1")
        window = min(max(window, self._min_log_bytes), self._max_log_bytes)

        attempts = self._attempt_order(tenant_id, task_id)
        chosen, attempt_status = self._choose_attempt(attempts, attempt_id)
        # What the task's input and metadata named as secret, masked in every
        # window too (the PR #229 review): the runner's `child started` line
        # logged the prompt inside argv until it logged its length, and an
        # agent can print anything it was given.
        literals = masking_for(task).literals

        read_at = self._now()
        reader = self._reader()
        entries: list[dict[str, Any]] = []
        if chosen is None:
            for name in wanted:
                entries.append(_no_attempt_entry(name, attempt_status))
        else:
            base = attempt_prefix(
                tenant_id=task.tenant_id, task_id=task.id, attempt_id=chosen
            )
            # OVERLAP when the caller is not starting at the beginning. Its last
            # byte tells `_align` whether `offset` lands on a token boundary
            # (the previous byte is whitespace) or inside a token; without it
            # the head rule has to assume the worst on every page and would
            # eat the first token of every window after the first, losing
            # data. The rest of it -- up to `KEY_LOOKBACK_BYTES` -- is what
            # tells the page whether it begins inside a private key (#188
            # review), which nothing inside the page can.
            probe = min(offset, KEY_LOOKBACK_BYTES)
            for name in wanted:
                opened = self._open_stream(
                    task=task,
                    base=base,
                    attempt_id=chosen,
                    stream=name,
                    source=source,
                    offset=offset - probe,
                    length=window + probe,
                    reader=reader,
                )
                entries.append(
                    self._served(
                        opened, offset=offset, probe=probe, read_at=read_at, literals=literals
                    )
                    if opened.status == "ok"
                    else opened.entry()
                )

        return {
            "task_id": task.id,
            "tenant_id": task.tenant_id,
            "attempt_id": chosen,
            "attempt": self._attempt_block(attempts, chosen, attempt_status),
            "read_at": _iso(read_at),
            "streams": entries,
            "prefix": reader.uri(prefix),
            "redaction": {
                # Stated in the payload so a reader never has to assume it, and
                # so a deployment where it somehow stopped is visible.
                "applied_at_read_time": True,
                "rules": len(REDACTION_RULES),
            },
        }

    @staticmethod
    def _attempt_block(
        attempts: dict[str, Attempt], chosen: str | None, attempt_status: str
    ) -> dict[str, Any]:
        """The `attempt` block every per-attempt read carries, in one shape."""
        record = attempts.get(chosen) if chosen else None
        return {
            "status": attempt_status,
            "known": record is not None,
            "generation": record.generation if record is not None else None,
            "created_at": record.created_at if record is not None else None,
            "completed_at": record.completed_at if record is not None else None,
            "exit_code": record.exit_code if record is not None else None,
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

    def _open_stream(
        self,
        *,
        task: Task,
        base: str,
        attempt_id: str,
        stream: str,
        source: str,
        offset: int,
        length: int,
        reader: ObjectReader,
        order: Sequence[str] | None = None,
    ) -> "OpenedStream":
        """Choose the object one stream of one attempt is read from, and read a window.

        `offset` and `length` are the RAW read, probe byte included; the
        caller decides how to align what comes back (`_served` cuts on
        whitespace for a log, the transcript parser cuts on newlines). One
        chooser for every reader, so `/logs?stream=agent_stdout`,
        `/transcript` and `/answer` never disagree about which object is the
        agent's output.

        THE ORDER. `source=final`: the complete record. `live`: the tail.
        `auto`: the record, then the tail -- and the tail only when the record
        is ABSENT, never when it is unreadable, because serving a different
        object in place of a failed read reports success over the wrong
        window. For an AGENT stream one more candidate follows when every
        other is absent: the manifest entry the runner wrote it to
        (`source: "artifact"`), for an attempt that ran before #184 and so has
        no `logs/agent_*` object of its own. Only for the attempt the manifest
        describes -- the manifest is the FINAL attempt's -- and never for
        `source=live`.

        `order` overrides the sequence for a caller with its own rule
        (`/answer` tries final, then the artifact, then live).
        """
        if stream in agent_streams_mod.AGENT_STREAMS:
            declared = agent_streams_mod.declared_streams(task.result_summary)
            if agent_streams_mod.has_no_agent(task.runner_profile, declared):
                return OpenedStream(
                    stream=stream,
                    source=None,
                    status="not_applicable",
                    detail=(
                        f"the {task.runner_profile!r} runner starts no agent CLI, so there "
                        "is no agent stream to read; its own output is `stdout` and `stderr`"
                    ),
                )
        if order is None:
            order = {"final": ("final",), "live": ("live",)}.get(source, ("final", "live"))
            if stream in agent_streams_mod.AGENT_STREAMS and source != "live":
                order = (*order, "artifact")

        last_absent: OpenedStream | None = None
        for label in order:
            if label == "artifact":
                located = self._stream_artifact(task, stream=stream, attempt_id=attempt_id)
                if located is None:
                    continue
                if isinstance(located, OpenedStream):
                    return located  # the manifest and the layout disagree: unreadable
                key = located
            elif label == "live":
                key = f"{base}/{LOGS_SEGMENT}/{LIVE_SEGMENT}/{stream}.tail.log"
            else:
                key = f"{base}/{LOGS_SEGMENT}/{stream}.log"
            uri = reader.uri(key)
            try:
                if label == "live":
                    chunk = _read_live(reader, key, offset=offset, length=length)
                else:
                    chunk = reader.read_range(key, offset=offset, length=length)
            except ObjectAbsent:
                last_absent = OpenedStream(
                    stream=stream, source=label, status="absent", key=key, uri=uri,
                    detail="this object does not exist",
                )
                continue  # try the next source, if the order left one
            except ObjectReplaced as exc:
                return OpenedStream(
                    stream=stream, source=label, status="unreadable", key=key, uri=uri,
                    detail=(
                        "the live object was replaced while it was read, twice; poll again"
                        if label == "live"
                        else redact_detail(exc.reason)
                    ),
                )
            except ObjectUnreadable as exc:
                # Deliberately NOT falling through to the next source. A
                # failure is an answer, and substituting a different object
                # for it would report success over a window that is not the
                # one asked for.
                return OpenedStream(
                    stream=stream, source=label, status="unreadable", key=key, uri=uri,
                    detail=redact_detail(exc.reason),
                )
            return OpenedStream(
                stream=stream, source=label, status="ok", key=key, uri=uri, chunk=chunk
            )

        if last_absent is None:
            # Only the artifact candidate was left and it was not eligible.
            return OpenedStream(
                stream=stream, source=None, status="absent",
                detail="no object for this stream exists for this attempt",
            )
        if source == "auto" and len(order) > 1:
            last_absent.detail = (
                "neither the completed log nor a live tail exists for this attempt; "
                "nothing has been written yet, or the objects have been removed"
            )
        return last_absent

    def _stream_artifact(
        self, task: Task, *, stream: str, attempt_id: str
    ) -> "str | OpenedStream | None":
        """The artifact key holding an agent stream of a pre-#184 attempt, or None.

        None -- not eligible -- when the attempt is not the one the manifest
        describes, when nothing names the stream's artifact, or when the
        manifest does not list it. The key is REBUILT from the task document's
        own tenant and id, exactly as `read_artifact` rebuilds one, and the
        stored `uri` is only compared: a disagreement is reported unreadable
        rather than followed.
        """
        if attempt_id != manifest_attempt(task):
            return None
        declared = agent_streams_mod.declared_streams(task.result_summary)
        name = agent_streams_mod.stream_artifacts(task.runner_profile, declared).get(stream)
        if not name:
            return None
        entry = self._store.artifact_manifest(task).find(name)
        if entry is None:
            return None
        try:
            key = self._artifact_key(task=task, attempt_id=attempt_id, name=name)
        except ValidationFailed:
            return None
        if self._reader().uri(key) != entry.get("uri"):
            return OpenedStream(
                stream=stream, source="artifact", status="unreadable", key=key,
                uri=self._reader().uri(key),
                detail=(
                    f"the recorded location of {name!r} does not match where this "
                    "platform writes it, so nothing was read"
                ),
            )
        return key

    def _served(
        self,
        opened: "OpenedStream",
        *,
        offset: int,
        probe: int,
        read_at: datetime,
        literals: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        stream, label, key, uri = opened.stream, opened.source, opened.key, opened.uri
        chunk = opened.chunk
        assert chunk is not None
        ages = _ages(chunk, read_at)
        raw_full = chunk.data
        if len(raw_full) <= probe:
            # Paged past the end, or a zero-byte object. Both are an object
            # that EXISTS, which is a different answer from absent.
            row = _entry(stream, label, "ok")
            row.update(
                key=key, uri=uri, content="", total_bytes=chunk.total_bytes,
                offset=min(offset, chunk.total_bytes), invalid_utf8_bytes=0, **ages,
            )
            return row

        previous = raw_full[probe - 1 : probe] if probe else b""
        raw = raw_full[probe:]
        start = chunk.offset + probe

        tail_window = None
        if label == "live" and start == 0:
            tail_window, header_end = parse_tail_header(raw)
            raw = raw[header_end:]
            start += header_end

        at_eof = chunk.end >= chunk.total_bytes
        raw, start, end, withheld = _align(
            raw, start=start, previous=previous, at_eof=at_eof, read_end=chunk.end
        )
        raw, start, inside_key, key_withheld = _enter_key(
            raw_full, raw=raw, start=start, end=end, base=chunk.offset
        )
        withheld = withheld or key_withheld

        text, undecodable = _decode_window(raw)
        # A line that is a JSON document -- every line of a stream-json
        # transcript -- is masked by its structure; the rest by the rules over
        # its text (`redaction.redact_lines`, the PR #229 review of #221).
        scrubbed = redact_lines(text, inside_key=inside_key, literals=literals)
        complete = end >= chunk.total_bytes
        row = _entry(stream, label, "ok")
        row.update(ages)
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
            invalid_utf8_bytes=undecodable,
        )
        if withheld is not None:
            row["detail"] = withheld
        if undecodable:
            row["detail"] = _with_sentence(
                row["detail"],
                f"{undecodable} bytes of this window are not UTF-8 and are shown as "
                "U+FFFD; the object at uri holds them exactly",
            )
        return row

    # -- artifacts ---------------------------------------------------------
    def read_artifact(
        self,
        tenant_id: str,
        task_id: str,
        *,
        name: str,
        offset: int = 0,
        limit_bytes: int | None = None,
    ) -> dict[str, Any]:
        """One artifact's CONTENT. The server picks the object; the caller names it.

        THE ROUTE TAKES A NAME, NEVER A PATH, AND THAT IS THE WHOLE SECURITY
        ARGUMENT. `name` is matched for EXACT equality against the manifest the
        worker wrote into this task's own `result_summary` -- a name that is
        not in that list resolves to nothing at all, so there is no value of it
        that addresses an object. The key is then REBUILT from three things
        that did not come from the request:

          * `task.tenant_id` and `task.id`, off the document `Store.get_task`
            already refused to return for another tenant;
          * the attempt segment, taken from the manifest entry and validated by
            `safe_segment`;
          * the manifest entry's own `name`.

        A path the caller supplies therefore cannot reach the key even in
        principle, which is a stronger statement than "we sanitise it" -- the
        sanitising is still there, on every segment, but nothing depends on it
        being complete.

        THE STORED `uri` IS EVIDENCE, NOT AN ADDRESS. It is Firestore data
        written by a worker, so it is compared against the rebuilt key and a
        disagreement is REFUSED (`manifest_inconsistent`) rather than
        followed. A manifest that pointed at another tenant's prefix -- through
        corruption or through anything else -- would be reported, not proxied.

        BOUNDED, AND IT SAYS SO. A transcript is routinely megabytes.
        `truncated` and `next_offset` are on every successful response, and
        when the window was cut the response also carries a `detail` sentence,
        because the failure mode being closed here is a reader concluding that
        a truncated artifact was the whole output.

        REDACTED AT READ TIME, unconditionally, exactly as `read_logs` is, and
        for exactly the same reason. The worker's pass is over REGISTERED
        LITERAL VALUES only; `lifecycle._redact_before_upload` short-circuits
        entirely on `if not self.log.has_secrets`, and `redact.scrub_file`
        skips any file that is binary, a symlink, or over 64 MiB. So an agent
        that printed an `Authorization:` header, minted its own GitHub token or
        catted an `.env` out of a cloned repository put those bytes in the
        bucket untouched. `swarm_api.redaction` runs over every byte served
        here and the response states that it did.

        NOT TEXT IS AN ANSWER. An artifact whose head carries a NUL byte is
        reported `binary` with its size and its `gs://` reference and NO
        content. Serving it base64-encoded would hand out bytes that no
        redaction rule can scan, which is the one thing the paragraph above
        exists to prevent; the listing route's position -- the caller reads GCS
        with their own credentials -- is the honest one for those.
        """
        task, _prefix = self._scoped(tenant_id, task_id)
        entry, attempt_id, key, reader = self._resolve_artifact(task, name)

        window = self._default_artifact_bytes if limit_bytes is None else limit_bytes
        if offset < 0:
            raise ValidationFailed("offset must not be negative")
        if window < 1:
            raise ValidationFailed("limit_bytes must be at least 1")
        window = min(max(window, self._min_artifact_bytes), self._max_artifact_bytes)

        row = _artifact_row(task=task, entry=entry, attempt_id=attempt_id)
        row.update(key=key, uri=reader.uri(key))

        # The look-back `read_logs` takes, for the same two reasons: the byte
        # before `offset` for `_align`, and whether the window begins inside
        # a private key.
        probe = min(offset, KEY_LOOKBACK_BYTES)
        try:
            chunk = reader.read_range(key, offset=offset - probe, length=window + probe)
        except ObjectAbsent:
            row.update(
                status="absent",
                detail=(
                    "the manifest records this artifact but the object is not in the "
                    "bucket; it was removed, or the upload the manifest records did "
                    "not complete"
                ),
            )
            return row
        except ObjectUnreadable as exc:
            row.update(
                status="unreadable",
                detail=(
                    "the artifact store could not be read, so nothing may be concluded "
                    "about this artifact's content: " + redact_detail(exc.reason)
                ),
            )
            return row

        row["total_bytes"] = chunk.total_bytes
        if self._looks_binary(reader, key, chunk, offset=offset, probe=probe):
            row.update(
                status="binary",
                offset=0,
                detail=(
                    "this artifact is not text, so its bytes are not served here: "
                    "nothing can scan them for a credential before they leave, and a "
                    "redaction that cannot run is not a redaction. Read it from the "
                    "uri above with your own credentials."
                ),
            )
            return row

        if len(chunk.data) <= probe:
            # Paged past the end, or a zero-byte artifact. Both are an object
            # that EXISTS and is empty, which is not absent and not a failure.
            row.update(
                status="ok",
                content="",
                offset=min(offset, chunk.total_bytes),
                truncated=False,
                invalid_utf8_bytes=0,
            )
            return row

        previous = chunk.data[probe - 1 : probe] if probe else b""
        raw = chunk.data[probe:]
        start = chunk.offset + probe
        raw, start, end, withheld = _align(
            raw,
            start=start,
            previous=previous,
            at_eof=chunk.end >= chunk.total_bytes,
            read_end=chunk.end,
        )
        raw, start, inside_key, key_withheld = _enter_key(
            chunk.data, raw=raw, start=start, end=end, base=chunk.offset
        )
        withheld = withheld or key_withheld
        text, undecodable = _decode_window(raw)
        # As `/logs` masks a window: a JSON line by its structure, the rest by
        # the rules, and the task's literals in both (the PR #229 review). The
        # agent's stream-json stdout is also one of its artifacts.
        scrubbed = redact_lines(
            text, inside_key=inside_key, literals=masking_for(task).literals
        )
        complete = end >= chunk.total_bytes
        row.update(
            status="ok",
            content=scrubbed.text,
            # Byte accounting is over the RAW object, never over the redacted
            # string: the redacted text is shorter and paging on its length
            # would drift away from the object.
            offset=start,
            returned_bytes=len(raw),
            next_offset=None if complete else end,
            truncated=not complete,
            redacted=scrubbed.any,
            redaction_count=scrubbed.count,
            invalid_utf8_bytes=undecodable,
        )
        if withheld is not None:
            row["detail"] = withheld
        elif not complete:
            row["detail"] = (
                f"{end} of {chunk.total_bytes} bytes are shown. This is a window, not "
                "the whole artifact -- continue from next_offset, or read the object "
                "from its uri."
            )
        if undecodable:
            row["detail"] = _with_sentence(
                row["detail"],
                f"{undecodable} bytes of this window are not UTF-8 and are shown as "
                "U+FFFD; the raw route (/artifacts/raw) serves them exactly",
            )
        return row

    def _resolve_artifact(
        self, task: Task, name: str
    ) -> tuple[dict[str, Any], str, str, ObjectReader]:
        """A manifest NAME to `(entry, attempt_id, key, reader)`, or the refusal.

        The resolution every artifact route shares -- the content window, the
        raw download (#184) and the legacy agent-stream fallback -- so they
        cannot disagree about which object a name means. The rules are
        `read_artifact`'s docstring's: exact-name match against this task's
        own manifest, the key REBUILT from the task document and the entry's
        attempt segment, the stored `uri` compared and a disagreement refused.
        """
        manifest = self._store.artifact_manifest(task)

        if not isinstance(name, str) or not name:
            raise ValidationFailed("an artifact name is required")
        entry = manifest.find(name)
        if entry is None:
            # The SAME 404 whether the task produced nothing, has not finished,
            # or produced something under a different name. `complete` is
            # carried so the caller can still tell "not yet" from "not ever"
            # without a second request -- that distinction is the artifact
            # listing's whole job and it would be lost here otherwise.
            raise NotFound(
                f"task {task.id!r} lists no artifact named {name!r}",
                detail={
                    "task_id": task.id,
                    "complete": manifest.complete,
                    "known_artifacts": [
                        str(a.get("name")) for a in manifest.artifacts if a.get("name")
                    ],
                },
            )

        stored_uri = entry.get("uri")
        attempt_id = _attempt_of_artifact_uri(
            stored_uri, tenant_id=task.tenant_id, task_id=task.id
        )
        if attempt_id is None:
            raise UpstreamUnavailable(
                f"the manifest entry for {name!r} does not record a usable location "
                "inside this task's own prefix, so its content cannot be served"
            )
        key = self._artifact_key(task=task, attempt_id=attempt_id, name=name)
        reader = self._reader()
        if reader.uri(key) != stored_uri:
            # Two records of one fact disagreeing. `_upload_outputs` writes the
            # key and the uri from the same expression, so they cannot differ
            # in a run this platform produced -- which is exactly why a
            # difference is refused rather than resolved in either direction.
            raise UpstreamUnavailable(
                f"the recorded location of {name!r} does not match where this "
                "platform writes it, so nothing was read; the manifest and the "
                "object layout disagree"
            )
        return entry, attempt_id, key, reader

    def _artifact_key(self, *, task: Task, attempt_id: str, name: str) -> str:
        """`tenants/<t>/tasks/<id>/attempts/<a>/artifacts/<name>`, rebuilt.

        Every segment is validated even though every segment came from the
        control plane rather than from the request. `safe_segment`'s own
        docstring makes the argument: a boundary built by concatenation is only
        as strong as its weakest segment, and "the store we happen to use does
        not normalise `..`" is not an access-control argument.

        `name` may legitimately contain slashes -- `_upload_outputs` walks
        `artifacts/` recursively and records `rel` as a POSIX relative path --
        so it is validated segment by segment rather than as one segment.
        """
        base = attempt_prefix(
            tenant_id=self._segment(task.tenant_id, what="tenant id"),
            task_id=self._segment(task.id, what="task id"),
            attempt_id=self._segment(attempt_id, what="attempt id"),
        )
        parts = name.split("/")
        safe = "/".join(
            self._segment(part, what="artifact name segment") for part in parts
        )
        return f"{base}/{ARTIFACTS_SEGMENT}/{safe}"

    def _looks_binary(
        self,
        reader: ObjectReader,
        key: str,
        chunk: Any,
        *,
        offset: int,
        probe: int,
    ) -> bool:
        """Whether this artifact is text, decided ONCE over its head.

        Deciding per window would make the same artifact text on page one and
        binary on page four, which is not a property an artifact has. When the
        caller asked for the head the window already contains it; otherwise the
        head is read separately, and a head that cannot be read is treated as
        NOT binary -- the content path below then serves it with replacement
        characters, which is a visibly mangled document rather than a silent
        refusal.
        """
        if offset == 0:
            return b"\x00" in chunk.data[:ARTIFACT_SNIFF_BYTES]
        try:
            head = reader.read_range(key, offset=0, length=ARTIFACT_SNIFF_BYTES)
        except (ObjectAbsent, ObjectUnreadable):
            return False
        return b"\x00" in head.data


def _attempt_of_artifact_uri(uri: Any, *, tenant_id: str, task_id: str) -> str | None:
    """The attempt segment of a manifest `uri`, or None if it is not ours.

    The ONLY thing taken from the stored uri, and it is taken only after the
    uri has been shown to sit under this task's own attempts prefix. Anything
    else -- a uri for another tenant, another task, a relative path, a value
    that is not a string -- yields None and the caller refuses.

    Sliced from `tenants/` rather than parsed as a URL so it works for the
    `gs://` form the worker writes in production and for the `file:///` form a
    `LOCAL_ARTIFACT_ROOT` run writes, which is the same reading
    `pointer_to_prefix` takes of the checkpoint pointer.
    """
    return _attempt_of_uri(uri, tenant_id=tenant_id, task_id=task_id, segment=ARTIFACTS_SEGMENT)


def _attempt_of_uri(uri: Any, *, tenant_id: str, task_id: str, segment: str) -> str | None:
    """The attempt segment of a stored uri under `.../attempts/<a>/<segment>/`, or None."""
    if not isinstance(uri, str) or not uri:
        return None
    marker = uri.find(TENANTS_ROOT)
    if marker < 0:
        return None
    key = uri[marker:]
    root = attempts_prefix(tenant_id=tenant_id, task_id=task_id)
    if not key.startswith(root):
        return None
    rest = key[len(root) :]
    attempt, _, remainder = rest.partition("/")
    if not attempt or not remainder.startswith(f"{segment}/"):
        return None
    return attempt


def manifest_attempt(task: Task) -> str | None:
    """The attempt `result_summary` describes, or None when nothing says.

    `result_summary` is written once, by `control.finish()`, and describes the
    FINAL attempt only; it names no attempt id, so the id is read off the
    locations it records -- the artifacts first, then the log objects -- each
    through `_attempt_of_uri`, which accepts only a location under THIS task's
    own prefix. Every per-attempt reader that falls back on the manifest (the
    legacy agent streams, the runner summary behind `/answer`) checks it is
    reading the manifest's attempt before it does, so an earlier attempt is
    never shown the final attempt's output.
    """
    summary = task.result_summary
    if not isinstance(summary, dict):
        return None
    entries = summary.get("artifacts")
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, dict):
            found = _attempt_of_artifact_uri(
                entry.get("uri"), tenant_id=task.tenant_id, task_id=task.id
            )
            if found:
                return found
    logs = summary.get("logs")
    if isinstance(logs, dict):
        for uri in logs.values():
            found = _attempt_of_uri(
                uri, tenant_id=task.tenant_id, task_id=task.id, segment=LOGS_SEGMENT
            )
            if found:
                return found
    return None


@dataclass
class OpenedStream:
    """Which object one stream was read from, and what the read gave.

    `chunk` is the raw window when `status` is `ok`, and None otherwise.
    """

    stream: str
    source: str | None
    status: str
    key: str | None = None
    uri: str | None = None
    detail: str | None = None
    chunk: ObjectSlice | None = None

    def entry(self) -> dict[str, Any]:
        """The log route's row for a stream that was NOT read (absent, unreadable, n/a)."""
        row = _entry(self.stream, self.source, self.status)
        row["key"] = self.key
        row["uri"] = self.uri
        row["detail"] = self.detail
        return row


def _read_live(reader: ObjectReader, key: str, *, offset: int, length: int) -> ObjectSlice:
    """Read a live tail, once more if it was republished mid-read.

    The worker rewrites every tail each `live_log_interval_seconds`, so a read
    that straddles a republish is ordinary, not a fault; a second read is
    normally a clean read of the new object. A second replacement propagates
    as `ObjectReplaced` and the stream is reported unreadable -- never a window
    spliced from two objects.
    """
    try:
        return reader.read_range(key, offset=offset, length=length)
    except ObjectReplaced:
        return reader.read_range(key, offset=offset, length=length)


def parse_tail_header(raw: bytes) -> tuple[dict[str, Any] | None, int]:
    """`(tail_window, header_length)` for a live tail's first bytes; `(None, 0)` if absent.

    `published_at` is the header's `at=` when present and parseable, else
    null -- a tail published before #184 has none, and a value that does not
    parse is not passed on as though it were a time.
    """
    match = _TAIL_HEADER.match(raw[:512])
    if match is None:
        return None, 0
    published = None
    if match.group(3) is not None:
        text = match.group(3).decode("ascii", errors="replace")
        if _parse_instant(text) is not None:
            published = text
    return (
        {
            "object_offset": int(match.group(1)),
            "stream_size": int(match.group(2)),
            "published_at": published,
        },
        match.end(),
    )


def _parse_instant(text: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _utc(moment: datetime) -> datetime:
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _iso(moment: datetime | None) -> str | None:
    return None if moment is None else _utc(moment).astimezone(timezone.utc).isoformat()


def _ages(chunk: ObjectSlice, read_at: datetime) -> dict[str, Any]:
    """`object_updated_at` and `age_seconds` for one read, both on the server's clock.

    Null when the store reported no time -- never an age of zero, which would
    draw a tail of unknown age as a fresh one.
    """
    if chunk.updated is None:
        return {"object_updated_at": None, "age_seconds": None}
    updated = _utc(chunk.updated)
    return {
        "object_updated_at": _iso(updated),
        "age_seconds": round((_utc(read_at) - updated).total_seconds(), 3),
    }


def _wanted_streams(stream: str | Sequence[str] | None) -> tuple[str, ...]:
    """The streams one log read serves, in the order asked, each once.

    None, or an empty list, is the runner's two streams -- the route's answer
    since before `stream` was repeatable, which RunFiles and `swarm tail` rely
    on. A name outside `LOG_STREAMS` is a 422 rather than an empty row.
    """
    if stream is None:
        return STREAMS
    names = [stream] if isinstance(stream, str) else list(stream)
    if not names:
        return STREAMS
    out: list[str] = []
    for name in names:
        if name not in LOG_STREAMS:
            raise ValidationFailed(f"stream must be one of: {', '.join(LOG_STREAMS)}")
        if name not in out:
            out.append(name)
    return tuple(out)


def _artifact_row(*, task: Task, entry: dict[str, Any], attempt_id: str) -> dict[str, Any]:
    """The constant shape every artifact response has, whatever happened.

    `content` is None -- never `""` -- in every non-ok case, for the reason
    `_entry` gives about log streams: a client that renders content without
    reading status must show nothing rather than an empty document that looks
    like a successful read of an artifact the agent left blank.
    """
    return {
        "task_id": task.id,
        "tenant_id": task.tenant_id,
        "attempt_id": attempt_id,
        "artifact": {
            "name": entry.get("name"),
            "bytes": entry.get("bytes"),
            "uri": entry.get("uri"),
        },
        "status": "ok",
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
        # Bytes of the served window that are not UTF-8, shown as U+FFFD. Null,
        # never 0, when no window was read.
        "invalid_utf8_bytes": None,
        "redaction": {
            # Stated in the payload so a reader never has to assume it, and so
            # a deployment where it somehow stopped is visible from outside.
            "applied_at_read_time": True,
            "rules": len(REDACTION_RULES),
        },
    }


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


def _enter_key(
    data: bytes, *, raw: bytes, start: int, end: int, base: int
) -> tuple[bytes, int, bool, str | None]:
    """Whether an aligned window begins inside a private key; if so, on a whole line.

    `data` is everything the read returned -- the look-back, then the window
    -- starting at object offset `base`; `raw` is the window as `_align` left
    it, starting at `start`. Returns `(raw, start, inside_key, detail)`.

    A key's lines hold no whitespace, so `_align` can only leave a window
    starting mid-line inside a key on its BEGIN line (the marker holds
    spaces) or on a JSON line holding a whole key written with `\\n`
    escapes. The rest of that line is key material either way, so it is
    skipped too and the window starts on the next line, where
    `redact(inside_key=True)` masks the body. Bytes skipped are not served;
    `offset` says where the window starts.
    """
    at = start - base
    if not raw or open_key_start(data, at) is None:
        return raw, start, False, None
    if at <= 0 or data[at - 1 : at] == b"\n":
        return raw, start, True, None
    newline = raw.find(b"\n")
    if newline < 0:
        return b"", end, True, (
            "this window lies inside a private key and holds no line break, so "
            "nothing of it was served; continue from next_offset"
        )
    return raw[newline + 1 :], start + newline + 1, True, None


def _decode_window(raw: bytes) -> tuple[str, int]:
    """`raw` as text for a JSON body, and how many of its bytes are not UTF-8.

    Each such byte is shown as U+FFFD -- a JSON string cannot carry it -- and
    COUNTED (#188 review), so a window of a Latin-1 file is never taken for
    the agent's own text. It used to be `errors="replace"`, silently. The raw
    download serves the exact bytes.
    """
    text = raw.decode("utf-8", errors="surrogateescape")
    return _UNDECODABLE.subn("\N{REPLACEMENT CHARACTER}", text)


def _with_sentence(detail: str | None, sentence: str) -> str:
    return sentence if not detail else f"{detail.rstrip('.')}. {sentence}"


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
        # The object's GCS `updated`, and the server-clock seconds between it
        # and the read's `read_at`. Null, never 0, when there was no object or
        # the store reported no time.
        "object_updated_at": None,
        "age_seconds": None,
        # Bytes of the served window that are not UTF-8, shown as U+FFFD. Null,
        # never 0, when nothing was read.
        "invalid_utf8_bytes": None,
    }


def _no_attempt_entry(stream: str, attempt_status: str) -> dict[str, Any]:
    row = _entry(stream, None, "absent")
    row["detail"] = (
        "this task has no attempt yet, so no log object can exist"
        if attempt_status == "no_attempt_yet"
        else "no attempt could be identified for this task"
    )
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
