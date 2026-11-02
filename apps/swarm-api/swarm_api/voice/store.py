"""Voice it storage: projects, cards, turns, events and starts, and the
tenant's `tenants/<tenant>/voice/` prefix (docs/design/voice-it.md §3.7, §7
invariant 9, §10 phase 2).

THE DOCUMENTS sit beside the project and session documents the session
routes write (`sessions.py`), and every one carries the tenant and the person:

  voice_projects/{project}                              title, status, brief, references
  voice_projects/{project}/cards/{card}                 the card envelope and body, versioned
  voice_projects/{project}/turns/{turn}                 one planner turn
  voice_projects/{project}/sessions/{session}           sessions.py's
  voice_projects/{project}/sessions/{session}/events/{seq}
                                                        the stream's log; seq from the
                                                        session's `event_seq` counter
  voice_starts/{start}                                  one row per thing Voice it started

EVERY PATH IS BUILT FROM THE TENANT THE CALLER RESOLVES TO. Each method takes
`tenant_id` and `person` from the route's guards (`tenant_scope` and the
verified email), never from a request field, and reads the project first: a
project whose `tenant_id` or `person` is not the caller's is the same
`NotFound` as a missing one, and nothing under it is read or written.

THE BLOBS. Card bodies too large for a document and the redacted transcript
live under the tenant's prefix in the artifact bucket:

  tenants/<tenant>/voice/<project>/transcripts/<session>/<part>.jsonl
  tenants/<tenant>/voice/<project>/mockups/<card>/<version>.html
  tenants/<tenant>/voice/<project>/diagrams/<card>/<version>.mmd | .json
  tenants/<tenant>/voice/<project>/plans/<version>.md

`blob_key` is the only way a key is made, and `key_refusal` is what every
bucket call checks first: one tenant segment, `voice/`, one project segment,
then plain segments -- no `..`, no `.`, no empty segment, no absolute path, no
backslash, and never another tenant's prefix. The IAM grants in
terraform/infra/voice.tf (`api_voice_blob_writer`, `api_voice_blob_deleter`)
are conditioned on the same `tenants/<t>/voice/<p>/` shape; one swarm-api
serves every tenant, so the condition pins the shape and this module pins the
tenant.

CREATE, NEVER OVERWRITE. `create` uploads with `if_generation_match=0`, so an
existing name is a refusal; a new transcript part or card version is a new
name. The bucket grant is objectCreator plus a conditioned delete for
removing a project, and create-plus-delete would admit a replace, so the
precondition here is what keeps an object from being rewritten.

DELETING A PROJECT removes its objects first and its documents after, as the
history purge does (`purge.py`): an interrupted delete leaves the project
listed, so the person can retry it, and a retry deletes what is there and
skips what is not. A project with a live session is refused with 409 until the
session ends, so swarm-voice never writes under a project that is gone.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Protocol

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from swarm_common.admission import _snapshot

from ..errors import Conflict, NotFound, UpstreamUnavailable, ValidationFailed
from . import sessions as vs

log = logging.getLogger(__name__)

PROJECTS = vs.PROJECTS
SESSIONS = vs.SESSIONS
CARDS = "cards"
TURNS = "turns"
EVENTS = "events"
STARTS = "voice_starts"
AUDIT = vs.AUDIT

AUDIT_RENAMED = "voice_project_renamed"
AUDIT_DELETED = "voice_project_deleted"

#: Project statuses (§3.7).
ACTIVE = "active"
WAITING = "waiting"
SHIPPED = "shipped"
ARCHIVED = "archived"
STATUSES = (ACTIVE, WAITING, SHIPPED, ARCHIVED)

TITLE_MAX = 120
#: The planner keeps the brief at a few thousand tokens (§3.7); a document
#: holds 1 MiB, and a card body over CARD_BODY_MAX goes to the bucket.
BRIEF_MAX_BYTES = 64 * 1024
CARD_BODY_MAX_BYTES = 256 * 1024
#: The list route's page. Version one has one person, so one page is all of it.
LIST_LIMIT = 200
#: Firestore commits at most 500 writes in one batch.
_BATCH = 400

#: A Voice it id: `vp_`/`vs_`/... and hex, as sessions.py mints them.
_ID_RE = re.compile(r"^[a-z]{2}_[A-Za-z0-9]{1,61}$")
#: A tenant id as the registry names them; one path segment.
_TENANT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")
#: One segment below a project: plain names, no leading dot.
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
BLOB_ROOT = "voice"
#: The four kinds of thing kept under a project's prefix (§3.7).
BLOB_KINDS = ("transcripts", "mockups", "diagrams", "plans")
_KEY_MAX = 1024


class VoiceProjectLive(Conflict):
    pass


class VoiceBlobKeyRefused(ValueError):
    """A key outside the tenant's voice prefix. A bug in the caller, never input."""


class VoiceBlobFailed(UpstreamUnavailable):
    code = "voice_storage_unavailable"


def check_id(value: Any, prefix: str, what: str) -> str:
    """`value` when it is a Voice it id with `prefix`, else a 422."""
    if not isinstance(value, str) or not value.startswith(prefix) or not _ID_RE.match(value):
        raise ValidationFailed(f"{what} is not a Voice it id", detail={"field": what})
    return value


# --------------------------------------------------------------------------
# The prefix
# --------------------------------------------------------------------------


def voice_prefix(tenant_id: str) -> str:
    """`tenants/<tenant>/voice/`, the tenant's one voice prefix."""
    if not isinstance(tenant_id, str) or not _TENANT_RE.match(tenant_id):
        raise VoiceBlobKeyRefused("the tenant id is not one path segment")
    return f"tenants/{tenant_id}/{BLOB_ROOT}/"


def project_prefix(tenant_id: str, project_id: str) -> str:
    """`tenants/<tenant>/voice/<project>/`. The trailing slash matters: without
    it `vp_1` would also match `vp_10`."""
    if not isinstance(project_id, str) or not _ID_RE.match(project_id):
        raise VoiceBlobKeyRefused("the project id is not a Voice it id")
    return voice_prefix(tenant_id) + project_id + "/"


def key_refusal(tenant_id: str, key: Any) -> str | None:
    """Why `key` is outside `tenant_id`'s voice prefix, or None when inside."""
    if not isinstance(key, str) or not key:
        return "the key is empty or not a string"
    if len(key) > _KEY_MAX:
        return "the key is too long"
    if key.startswith("/") or "\\" in key or "\x00" in key:
        return "the key is an absolute path or holds a backslash or NUL"
    try:
        prefix = voice_prefix(tenant_id)
    except VoiceBlobKeyRefused as exc:
        return str(exc)
    if not key.startswith(prefix):
        return "the key is not under this tenant's voice prefix"
    parts = key[len(prefix):].split("/")
    if len(parts) < 2:
        return "the key names no project or no object under it"
    if not _ID_RE.match(parts[0]):
        return "the key's project segment is not a Voice it id"
    for part in parts[1:]:
        if part in ("", ".", "..") or not _SEGMENT_RE.match(part):
            return "the key holds an empty, dot or unsafe segment"
    return None


def checked_key(tenant_id: str, key: str) -> str:
    refusal = key_refusal(tenant_id, key)
    if refusal is not None:
        raise VoiceBlobKeyRefused(refusal)
    return key


def blob_key(tenant_id: str, project_id: str, *parts: str) -> str:
    """The key of one object under the project's prefix, refused unless every
    part is one plain segment: `blob_key("eng", "vp_1", "plans", "3.md")`."""
    if not parts:
        raise VoiceBlobKeyRefused("a blob needs a name under the project")
    for part in parts:
        if not isinstance(part, str) or part in ("", ".", "..") or not _SEGMENT_RE.match(part):
            raise VoiceBlobKeyRefused("each part of a blob name is one plain segment")
    return checked_key(tenant_id, project_prefix(tenant_id, project_id) + "/".join(parts))


def transcript_key(tenant_id: str, project_id: str, session_id: str, part: int) -> str:
    check_id(session_id, "vs_", "session_id")
    if not isinstance(part, int) or isinstance(part, bool) or part < 0:
        raise VoiceBlobKeyRefused("a transcript part is a non-negative integer")
    return blob_key(tenant_id, project_id, "transcripts", session_id, f"{part:06d}.jsonl")


# --------------------------------------------------------------------------
# The bucket
# --------------------------------------------------------------------------


class VoiceBlobs(Protocol):
    """The artifact bucket for Voice it. Every method checks the key against
    the tenant's prefix before it touches the bucket."""

    bucket: str

    def create(self, tenant_id: str, key: str, data: bytes, content_type: str) -> str: ...

    def read(self, tenant_id: str, key: str) -> bytes | None: ...

    def list_keys(self, tenant_id: str, prefix: str) -> list[str]: ...

    def delete_keys(self, tenant_id: str, keys: list[str]) -> None: ...


def _checked_prefix(tenant_id: str, prefix: str) -> str:
    # A listing prefix is a project's own, built by project_prefix: checked as
    # a key with a placeholder leaf, so the same rules hold.
    if not isinstance(prefix, str) or not prefix.endswith("/"):
        raise VoiceBlobKeyRefused("a listing prefix ends in /")
    checked_key(tenant_id, prefix + "x")
    return prefix


class GcsVoiceBlobs:
    """Google Cloud Storage. Holds no client until first used, so
    `create_app()` needs no credentials (objects.py, inputwriter.py)."""

    def __init__(self, bucket: str, project_id: str | None = None, client: Any = None, *,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        if not bucket:
            raise ValueError("an artifact bucket is required for Voice it's blobs")
        self.bucket = bucket
        self._project_id = project_id
        self._client = client
        self._now = now
        self._lock = threading.Lock()

    def _get_client(self) -> Any:
        with self._lock:
            if self._client is None:
                from google.cloud import storage  # lazy: create_app() needs no credentials

                self._client = storage.Client(project=self._project_id)
            return self._client

    def create(self, tenant_id: str, key: str, data: bytes, content_type: str) -> str:
        checked_key(tenant_id, key)
        try:
            blob = self._get_client().bucket(self.bucket).blob(key)
            blob.custom_time = self._now()
            blob.upload_from_string(data, content_type=content_type, if_generation_match=0)
        except Exception as exc:  # the client's type only: its text can name the key
            raise VoiceBlobFailed(
                f"Voice it's storage did not create the object ({type(exc).__name__})") from None
        return f"gs://{self.bucket}/{key}"

    def read(self, tenant_id: str, key: str) -> bytes | None:
        checked_key(tenant_id, key)
        from google.api_core import exceptions as gexc

        try:
            return self._get_client().bucket(self.bucket).blob(key).download_as_bytes()
        except gexc.NotFound:
            return None
        except Exception as exc:  # noqa: BLE001 - a failed read is never an empty one
            raise VoiceBlobFailed(
                f"Voice it's storage could not read the object ({type(exc).__name__})") from None

    def list_keys(self, tenant_id: str, prefix: str) -> list[str]:
        _checked_prefix(tenant_id, prefix)
        try:
            return [b.name for b in self._get_client().list_blobs(self.bucket, prefix=prefix)]
        except Exception as exc:  # noqa: BLE001 - a failed listing is never an empty one
            raise VoiceBlobFailed(
                f"Voice it's storage could not list the project ({type(exc).__name__})") from None

    def delete_keys(self, tenant_id: str, keys: list[str]) -> None:
        for key in keys:
            checked_key(tenant_id, key)
        if not keys:
            return
        try:
            bucket = self._get_client().bucket(self.bucket)
            # on_error is called for a 404 only: an object already gone is
            # what a retried delete finds. A 403 raises.
            bucket.delete_blobs([bucket.blob(k) for k in keys], on_error=lambda _blob: None)
        except Exception as exc:  # noqa: BLE001
            raise VoiceBlobFailed(
                f"Voice it's storage could not delete the project's objects ({type(exc).__name__})"
            ) from None


class InMemoryVoiceBlobs:
    """The same semantics over a dict, for tests and `RUN_MODE=local`."""

    def __init__(self, *, bucket: str = "swarm-artifacts-test") -> None:
        self.bucket = bucket
        self.objects: dict[str, bytes] = {}
        self.content_types: dict[str, str] = {}
        self.fail = False
        self._lock = threading.Lock()

    def _maybe_fail(self) -> None:
        if self.fail:
            raise VoiceBlobFailed("Voice it's storage is unavailable (injected)")

    def create(self, tenant_id: str, key: str, data: bytes, content_type: str) -> str:
        checked_key(tenant_id, key)
        self._maybe_fail()
        with self._lock:
            if key in self.objects:
                raise VoiceBlobFailed("the object already exists; Voice it never overwrites")
            self.objects[key] = bytes(data)
            self.content_types[key] = content_type
        return f"gs://{self.bucket}/{key}"

    def read(self, tenant_id: str, key: str) -> bytes | None:
        checked_key(tenant_id, key)
        self._maybe_fail()
        return self.objects.get(key)

    def list_keys(self, tenant_id: str, prefix: str) -> list[str]:
        _checked_prefix(tenant_id, prefix)
        self._maybe_fail()
        return sorted(k for k in self.objects if k.startswith(prefix))

    def delete_keys(self, tenant_id: str, keys: list[str]) -> None:
        for key in keys:
            checked_key(tenant_id, key)
        self._maybe_fail()
        with self._lock:
            for key in keys:
                self.objects.pop(key, None)
                self.content_types.pop(key, None)


def build_voice_blobs(*, bucket: str, project_id: str | None = None) -> VoiceBlobs | None:
    """The production store, or None when no bucket is configured (then no
    object can have been written, and a project delete has none to remove)."""
    if not bucket:
        return None
    return GcsVoiceBlobs(bucket=bucket, project_id=project_id)


# --------------------------------------------------------------------------
# The documents
# --------------------------------------------------------------------------


def check_title(value: Any) -> str:
    title = value.strip() if isinstance(value, str) else ""
    if not title or len(title) > TITLE_MAX or any(ord(c) < 32 for c in title):
        raise ValidationFailed(
            f"title is 1 to {TITLE_MAX} printable characters", detail={"field": "title"})
    return title


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def project_view(project_id: str, data: dict[str, Any]) -> dict[str, Any]:
    """What the routes answer: never the person's email or the tenant beyond
    what the caller already is."""
    return {
        "project_id": project_id,
        "title": data.get("title") or vs.UNTITLED,
        "status": data.get("status") or ACTIVE,
        "subject": data.get("subject"),
        "size": data.get("size"),
        "brief": data.get("brief") or {},
        "references": data.get("references") or {},
        "tag": f"voice:{project_id}",
        "created_at": _iso(data.get("created_at")),
        "updated_at": _iso(data.get("updated_at")),
    }


def _json_size(value: Any) -> int:
    return len(json.dumps(value, default=str).encode("utf-8"))


class VoiceStore:
    """One person's Voice it documents in one tenant."""

    def __init__(self, db: Any, *, now: Callable[[], datetime],
                 blobs: VoiceBlobs | None = None) -> None:
        self._db = db
        self._now = now
        self._blobs = blobs

    # ---- references ------------------------------------------------------

    def _project_ref(self, project_id: str) -> Any:
        return self._db.collection(PROJECTS).document(project_id)

    def _sub_ref(self, project_id: str, collection: str, doc_id: str) -> Any:
        return self._project_ref(project_id).collection(collection).document(doc_id)

    @staticmethod
    def _mine(data: dict[str, Any] | None, tenant_id: str, person: str) -> bool:
        return bool(data) and data.get("tenant_id") == tenant_id and data.get("person") == person

    def _owned(self, tenant_id: str, person: str, project_id: str) -> dict[str, Any]:
        """The project's data when it is this person's in this tenant; the same
        `NotFound` as a missing one otherwise (invariant 9)."""
        check_id(project_id, "vp_", "project_id")
        snap = self._project_ref(project_id).get()
        data = snap.to_dict() if snap.exists else None
        if not self._mine(data, tenant_id, person):
            raise NotFound(f"voice project {project_id!r} not found")
        return data or {}

    def _audit(self, writer: Any, *, action: str, tenant_id: str, person: str,
               project_id: str, detail: dict[str, Any]) -> None:
        writer.set(self._db.collection(AUDIT).document(), {
            "action": action, "tenant_id": tenant_id, "by": person, "at": self._now(),
            "project_id": project_id, "session_id": None, "detail": detail,
        })

    # ---- projects --------------------------------------------------------

    def list_projects(self, *, tenant_id: str, person: str) -> list[dict[str, Any]]:
        """Newest first. Served by voice-projects-tenant-person-updated."""
        rows = (self._db.collection(PROJECTS)
                .where(filter=FieldFilter("tenant_id", "==", tenant_id))
                .where(filter=FieldFilter("person", "==", person))
                .order_by("updated_at", direction=firestore.Query.DESCENDING)
                .limit(LIST_LIMIT)
                .stream())
        return [project_view(snap.id, snap.to_dict() or {}) for snap in rows
                if self._mine(snap.to_dict(), tenant_id, person)]

    def get_project(self, *, tenant_id: str, person: str, project_id: str) -> dict[str, Any]:
        return project_view(project_id, self._owned(tenant_id, person, project_id))

    def rename_project(self, *, tenant_id: str, person: str, project_id: str,
                       title: str) -> dict[str, Any]:
        check_id(project_id, "vp_", "project_id")
        title = check_title(title)
        ref = self._project_ref(project_id)

        @firestore.transactional
        def _apply(txn: Any) -> dict[str, Any]:
            snap = _snapshot(txn.get(ref))
            data = (snap.to_dict() or {}) if snap.exists else {}
            if not self._mine(data, tenant_id, person):
                raise NotFound(f"voice project {project_id!r} not found")
            before = data.get("title")
            now = self._now()
            txn.update(ref, {"title": title, "updated_at": now})
            self._audit(txn, action=AUDIT_RENAMED, tenant_id=tenant_id, person=person,
                        project_id=project_id, detail={"changed": before != title})
            return {**data, "title": title, "updated_at": now}

        return project_view(project_id, _apply(self._db.transaction()))

    def update_project(self, *, tenant_id: str, person: str, project_id: str,
                       brief: dict[str, Any] | None = None,
                       references: dict[str, Any] | None = None,
                       status: str | None = None, size: str | None = None,
                       subject: dict[str, Any] | None = None) -> dict[str, Any]:
        """The planner's after-turn write (§3.11): the brief, references,
        status, size and subject. Only what is given changes."""
        data = self._owned(tenant_id, person, project_id)
        changes: dict[str, Any] = {}
        if brief is not None:
            if not isinstance(brief, dict) or _json_size(brief) > BRIEF_MAX_BYTES:
                raise ValidationFailed("brief is an object of at most 64 KiB",
                                       detail={"field": "brief"})
            changes["brief"] = brief
        if references is not None:
            if not isinstance(references, dict):
                raise ValidationFailed("references is an object", detail={"field": "references"})
            changes["references"] = references
        if status is not None:
            if status not in STATUSES:
                raise ValidationFailed(f"status is one of {', '.join(STATUSES)}",
                                       detail={"field": "status"})
            changes["status"] = status
        if size is not None:
            changes["size"] = str(size)[:32]
        if subject is not None:
            if not isinstance(subject, dict):
                raise ValidationFailed("subject is an object", detail={"field": "subject"})
            changes["subject"] = subject
        changes["updated_at"] = self._now()
        self._project_ref(project_id).update(changes)
        return project_view(project_id, {**data, **changes})

    # ---- cards -----------------------------------------------------------

    def put_card(self, *, tenant_id: str, person: str, project_id: str, card_id: str,
                 kind: str, body: dict[str, Any], session_id: str | None = None,
                 turn_id: str | None = None) -> dict[str, Any]:
        """Write the card's next version. A body over CARD_BODY_MAX_BYTES is
        refused here: the caller puts it in the bucket (`create_blob`) and
        stores the key."""
        self._owned(tenant_id, person, project_id)
        check_id(card_id, "vc_", "card_id")
        if not isinstance(kind, str) or not kind.strip():
            raise ValidationFailed("kind is required", detail={"field": "kind"})
        if not isinstance(body, dict) or _json_size(body) > CARD_BODY_MAX_BYTES:
            raise ValidationFailed("a card body over 256 KiB goes to the bucket",
                                   detail={"field": "body"})
        ref = self._sub_ref(project_id, CARDS, card_id)

        @firestore.transactional
        def _apply(txn: Any) -> dict[str, Any]:
            snap = _snapshot(txn.get(ref))
            old = (snap.to_dict() or {}) if snap.exists else {}
            if snap.exists and not self._mine(old, tenant_id, person):
                raise NotFound(f"voice card {card_id!r} not found")
            now = self._now()
            doc = {
                "tenant_id": tenant_id, "person": person, "project_id": project_id,
                "kind": kind.strip(), "body": body, "version": int(old.get("version") or 0) + 1,
                "session_id": session_id, "turn_id": turn_id,
                "created_at": old.get("created_at") or now, "updated_at": now,
            }
            txn.set(ref, doc)
            return doc

        return _apply(self._db.transaction())

    def list_cards(self, *, tenant_id: str, person: str, project_id: str) -> list[dict[str, Any]]:
        self._owned(tenant_id, person, project_id)
        out = []
        for snap in self._project_ref(project_id).collection(CARDS).stream():
            data = snap.to_dict() or {}
            if self._mine(data, tenant_id, person):
                out.append({"card_id": snap.id, **data})
        return out

    # ---- turns -----------------------------------------------------------

    def record_turn(self, *, tenant_id: str, person: str, project_id: str, turn_id: str,
                    fields: dict[str, Any]) -> dict[str, Any]:
        """Create or merge one planner turn. A retried turn reads its own
        actions first (`get_turn`), so `actions` is merged by key, never lost."""
        self._owned(tenant_id, person, project_id)
        check_id(turn_id, "vt_", "turn_id")
        if not isinstance(fields, dict):
            raise ValidationFailed("a turn is an object", detail={"field": "turn"})
        ref = self._sub_ref(project_id, TURNS, turn_id)

        @firestore.transactional
        def _apply(txn: Any) -> dict[str, Any]:
            snap = _snapshot(txn.get(ref))
            old = (snap.to_dict() or {}) if snap.exists else {}
            if snap.exists and not self._mine(old, tenant_id, person):
                raise NotFound(f"voice turn {turn_id!r} not found")
            actions = dict(old.get("actions") or {})
            actions.update(fields.get("actions") or {})
            now = self._now()
            doc = {**old, **{k: v for k, v in fields.items() if k != "actions"},
                   "actions": actions, "tenant_id": tenant_id, "person": person,
                   "project_id": project_id, "created_at": old.get("created_at") or now,
                   "updated_at": now}
            txn.set(ref, doc)
            return doc

        return _apply(self._db.transaction())

    def get_turn(self, *, tenant_id: str, person: str, project_id: str,
                 turn_id: str) -> dict[str, Any] | None:
        self._owned(tenant_id, person, project_id)
        check_id(turn_id, "vt_", "turn_id")
        snap = self._sub_ref(project_id, TURNS, turn_id).get()
        data = snap.to_dict() if snap.exists else None
        return data if self._mine(data, tenant_id, person) else None

    # ---- events ----------------------------------------------------------

    def append_event(self, *, tenant_id: str, person: str, project_id: str,
                     session_id: str, event: dict[str, Any]) -> int:
        """Append one stream event; its sequence number. The counter is the
        session document's `event_seq`, read and written in the transaction
        that writes the event, so two writers never share a number."""
        self._owned(tenant_id, person, project_id)
        check_id(session_id, "vs_", "session_id")
        if not isinstance(event, dict):
            raise ValidationFailed("an event is an object", detail={"field": "event"})
        session_ref = self._sub_ref(project_id, SESSIONS, session_id)

        @firestore.transactional
        def _apply(txn: Any) -> int:
            snap = _snapshot(txn.get(session_ref))
            session = (snap.to_dict() or {}) if snap.exists else {}
            if not self._mine(session, tenant_id, person):
                raise NotFound(f"voice session {session_id!r} not found")
            seq = int(session.get("event_seq") or 0) + 1
            txn.update(session_ref, {"event_seq": seq})
            txn.set(session_ref.collection(EVENTS).document(f"{seq:012d}"), {
                **event, "seq": seq, "tenant_id": tenant_id, "person": person,
                "at": self._now()})
            return seq

        return _apply(self._db.transaction())

    def events_after(self, *, tenant_id: str, person: str, project_id: str,
                     session_id: str, after: int = 0) -> list[dict[str, Any]]:
        self._owned(tenant_id, person, project_id)
        check_id(session_id, "vs_", "session_id")
        rows = (self._sub_ref(project_id, SESSIONS, session_id).collection(EVENTS)
                .where(filter=FieldFilter("seq", ">", int(after)))
                .order_by("seq").stream())
        return [snap.to_dict() or {} for snap in rows
                if self._mine(snap.to_dict(), tenant_id, person)]

    # ---- starts ----------------------------------------------------------

    def record_start(self, *, tenant_id: str, person: str, project_id: str,
                     session_id: str, kind: str, target_id: str, day: str,
                     reservation: dict[str, Any] | None = None) -> str:
        """One row per thing Voice it started (§3.8's ledger reads these)."""
        self._owned(tenant_id, person, project_id)
        check_id(session_id, "vs_", "session_id")
        if kind not in ("agent", "workflow", "run"):
            raise ValidationFailed("kind is agent, workflow or run", detail={"field": "kind"})
        start_id = "vst_" + secrets.token_hex(16)
        self._db.collection(STARTS).document(start_id).set({
            "tenant_id": tenant_id, "person": person, "project_id": project_id,
            "session_id": session_id, "kind": kind, "target_id": str(target_id),
            "day": str(day), "reservation": reservation or {}, "at": self._now()})
        return start_id

    def starts_of(self, *, tenant_id: str, person: str, project_id: str) -> list[dict[str, Any]]:
        """Served by voice-starts-tenant-project."""
        self._owned(tenant_id, person, project_id)
        return [{"start_id": snap.id, **(snap.to_dict() or {})} for snap in self._starts(
            tenant_id, project_id) if self._mine(snap.to_dict(), tenant_id, person)]

    def _starts(self, tenant_id: str, project_id: str) -> Iterable[Any]:
        return (self._db.collection(STARTS)
                .where(filter=FieldFilter("tenant_id", "==", tenant_id))
                .where(filter=FieldFilter("project_id", "==", project_id))
                .stream())

    # ---- blobs -----------------------------------------------------------

    def create_blob(self, *, tenant_id: str, person: str, project_id: str,
                    parts: tuple[str, ...], data: bytes, content_type: str) -> str:
        """Create one object under the project's prefix; its key."""
        self._owned(tenant_id, person, project_id)
        if parts and parts[0] not in BLOB_KINDS:
            raise VoiceBlobKeyRefused(f"a blob is one of {', '.join(BLOB_KINDS)}")
        key = blob_key(tenant_id, project_id, *parts)
        if self._blobs is None:
            raise VoiceBlobFailed("Voice it's storage is not configured (no artifact bucket)")
        self._blobs.create(tenant_id, key, data, content_type)
        return key

    def read_blob(self, *, tenant_id: str, person: str, project_id: str,
                  key: str) -> bytes | None:
        self._owned(tenant_id, person, project_id)
        checked_key(tenant_id, key)
        if not key.startswith(project_prefix(tenant_id, project_id)):
            raise VoiceBlobKeyRefused("the key is not under this project's prefix")
        return None if self._blobs is None else self._blobs.read(tenant_id, key)

    # ---- delete ----------------------------------------------------------

    def delete_project(self, *, tenant_id: str, person: str, project_id: str) -> dict[str, Any]:
        """Remove the project's objects, then every document under it, then
        the project. 409 while one of its sessions is live."""
        data = self._owned(tenant_id, person, project_id)
        project_ref = self._project_ref(project_id)
        sessions = list(project_ref.collection(SESSIONS).stream())
        for snap in sessions:
            if (snap.to_dict() or {}).get("state") in vs.HOLDING:
                raise VoiceProjectLive(
                    "This project has a live Voice it session; end it before deleting the project.",
                    detail={"project_id": project_id, "session_id": snap.id})

        objects = 0
        if self._blobs is not None:
            keys = self._blobs.list_keys(tenant_id, project_prefix(tenant_id, project_id))
            self._blobs.delete_keys(tenant_id, keys)
            objects = len(keys)

        refs: list[Any] = []
        for snap in sessions:
            refs.extend(e.reference for e in snap.reference.collection(EVENTS).stream())
            refs.append(snap.reference)
        for collection in (CARDS, TURNS):
            refs.extend(s.reference for s in project_ref.collection(collection).stream())
        refs.extend(s.reference for s in self._starts(tenant_id, project_id)
                    if self._mine(s.to_dict(), tenant_id, person))
        documents = len(refs) + 1
        for i in range(0, len(refs), _BATCH):
            batch = self._db.batch()
            for ref in refs[i:i + _BATCH]:
                batch.delete(ref)
            batch.commit()
        # The project last, with its audit row: until here a retry finds it.
        batch = self._db.batch()
        batch.delete(project_ref)
        self._audit(batch, action=AUDIT_DELETED, tenant_id=tenant_id, person=person,
                    project_id=project_id,
                    detail={"documents": documents, "objects": objects,
                            "title": data.get("title") or vs.UNTITLED})
        batch.commit()
        log.info("voice project deleted tenant=%s project=%s documents=%d objects=%d",
                 tenant_id, project_id, documents, objects)
        return {"project_id": project_id, "deleted": True, "documents": documents,
                "objects": objects}
