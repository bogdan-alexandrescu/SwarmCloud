"""Idempotent submission: one key, one submission, however often it is sent.

A caller -- the plugin bridge above all -- names an `idempotency_key` on
`POST /v1/tasks`, `POST /v1/tasks/batch` or `POST /v1/workflows`. A POST that
times out may still have been accepted, so the caller sends the SAME body with
the SAME key again; this module makes that second send answer the first
submission's ids, marked `idempotent_replay: true`, and create nothing.

WHERE A KEY LIVES. One document per (workspace, submitter, key) in
`submission_keys`, its id the SHA-256 of the three joined by NUL bytes
(`claim_id`), so the key itself is never stored and never served. The tenant
is the one `SubmissionService.tenant_for` resolved and checked for this
request and the submitter is the verified email, never a value from the
body: two workspaces, or two members of one, that pick the same key name two
different documents, and neither can read, replay or collide with the
other's submission. The document also stores both, and `find` refuses a
document whose stored scope is not the caller's, so even a hash collision
could not hand one scope another's ids.

WHY IT CANNOT DOUBLE-ADMIT. The claim is written with `create` (a
create-if-absent precondition) in the SAME Firestore write batch as the
submission's first chunk of tasks (`Store.create_tasks`); a batch commits
atomically. Two submits that both passed the read in `find` race to that
commit: exactly one creates the claim and its tasks; the other's whole batch
is refused with ALREADY_EXISTS, so none of its tasks is written, and it then
reads the winner's claim and answers the winner's ids (`Raced`). A submission
is at most `max_batch_size` (100) tasks, inside one 200-task chunk, so every
task of a batch is in that commit; a workflow's later chunks and its workflow
document follow, as they always have. Admission is untouched: the claim is
written beside QUEUED tasks, which hold no lease and no capacity.

THE PAYLOAD IS FINGERPRINTED, NOT STORED. `fingerprint` is the SHA-256 of the
body's canonical JSON without the key, and with every `mcp_credentials`
entry cut to its `expires_in`: a credential value, or a hash of one, is never
written (docs/design/registered-mcp-servers.md §4.3). The same key with a
different fingerprint, or on another route, is refused 409
`idempotency_key_reused`; nothing is created.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping, Sequence

from google.cloud import firestore

from swarm_common.admission import _snapshot

from .errors import Conflict, ValidationFailed

#: The collection of claims. Firestore deletes a claim once its `expires_at`
#: has passed (the TTL field in terraform/modules/firestore/indexes.tf); until
#: the TTL sweeps it, `find` treats an expired claim as absent itself.
SUBMISSION_KEYS = "submission_keys"

#: How long a key holds its submission: 24 hours. A bridge re-sends within
#: minutes of a timeout, but a session can be suspended with its laptop, or a
#: bridge restarted, and come back hours later to the same pending send; a day
#: covers a working day's gap and the next morning's resume, while a key that
#: is a day old is one no caller is still retrying. It is also the window
#: other idempotent APIs commonly hold. A claim is one ~400-byte document per
#: keyed submission, so a day of them costs next to nothing.
KEY_WINDOW = timedelta(hours=24)

#: What a key may be: 16 to 128 characters of a URL-safe alphabet. 16 at least
#: so a key is a random value and not a word two logical submissions of one
#: person could both pick (a UUID4 is 36, `secrets.token_urlsafe(24)` 32).
KEY_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{16,128}$")

#: Which route a claim was made on; a key is never replayed across routes.
KIND_TASK = "task"
KIND_BATCH = "batch"
KIND_WORKFLOW = "workflow"


class KeyReused(Conflict):
    """The same key, in the same scope, with a different payload or route."""

    code = "idempotency_key_reused"


class ReplayIncomplete(Conflict):
    """The claim exists, but a document it names is not readable yet: the
    first submission is still writing its later chunks. Retrying with the
    same key in a few seconds answers it."""

    code = "idempotency_replay_incomplete"


@dataclass(frozen=True)
class Claim:
    """One keyed submission: what a replay answers."""

    kind: str
    fingerprint: str
    task_ids: tuple[str, ...]
    workflow_id: str | None = None
    #: The workflow create reply's `dispatch` object, as first answered.
    dispatch: Mapping[str, Any] = field(default_factory=dict)


class Raced(Exception):
    """The claim's create lost to a concurrent submit with the same key: the
    write batch carrying it was refused whole, so nothing of this submission
    was written."""


def check_key(key: str | None) -> str | None:
    """The key, checked; None when the caller named none."""
    if key is None:
        return None
    if not isinstance(key, str) or not KEY_PATTERN.match(key):
        raise ValidationFailed(
            "idempotency_key is 16 to 128 characters of letters, digits and _ . : -; "
            "send a fresh random value per submission and the same one when re-sending it",
            detail={"field": "idempotency_key"},
        )
    return key


def claim_id(tenant_id: str, submitter: str, key: str) -> str:
    """The claim's document id: the scope and the key, hashed, so the key is
    not stored and no two scopes share a document."""
    joined = "\x00".join((tenant_id, submitter.strip().lower(), key))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _without_credential_values(value: Any) -> Any:
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for name, item in value.items():
            if name == "mcp_credentials" and isinstance(item, Mapping):
                out[name] = {
                    server: ({"expires_in": entry.get("expires_in")}
                             if isinstance(entry, Mapping) else None)
                    for server, entry in item.items()
                }
            else:
                out[name] = _without_credential_values(item)
        return out
    if isinstance(value, list):
        return [_without_credential_values(item) for item in value]
    return value


def fingerprint(kind: str, payload: Any) -> str:
    """SHA-256 of the route and the body's canonical JSON, the key left out
    and credential values cut to their `expires_in`."""
    body = _without_credential_values(payload)
    if isinstance(body, dict):
        body.pop("idempotency_key", None)
    canonical = json.dumps({"kind": kind, "body": body}, sort_keys=True,
                           separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class Keyed:
    """One request's key in its scope: built by the route's service call,
    read by `Store.create_tasks` to write the claim in its first batch."""

    tenant_id: str
    submitter: str
    kind: str
    fingerprint: str
    doc_id: str
    expires_at: datetime
    created_at: datetime
    #: Filled once the submission's ids are known, before the write.
    task_ids: Sequence[str] = ()
    workflow_id: str | None = None
    dispatch: Mapping[str, Any] = field(default_factory=dict)

    def document(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "submitted_by": self.submitter.strip().lower(),
            "kind": self.kind,
            "fingerprint": self.fingerprint,
            "task_ids": list(self.task_ids),
            "workflow_id": self.workflow_id,
            "dispatch": dict(self.dispatch),
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }


class SubmissionKeys:
    """Reads and clears claims; the create is `Store.create_tasks`'s."""

    def __init__(self, db: Any, *, now: Callable[[], datetime]) -> None:
        self._db = db
        self._now = now

    def ref(self, doc_id: str) -> Any:
        return self._db.collection(SUBMISSION_KEYS).document(doc_id)

    def keyed(self, *, tenant_id: str, submitter: str, key: str, kind: str,
              payload: Any) -> Keyed:
        now = self._now()
        return Keyed(
            tenant_id=tenant_id, submitter=submitter, kind=kind,
            fingerprint=fingerprint(kind, payload),
            doc_id=claim_id(tenant_id, submitter, key),
            created_at=now, expires_at=now + KEY_WINDOW,
        )

    def find(self, keyed: Keyed) -> Claim | None:
        """The live claim for this scope and key, checked against this
        request; None when there is none or it has expired (an expired one is
        deleted first, so the create that follows can succeed). Raises
        `KeyReused` when the claim was made with another payload or route."""
        ref = self.ref(keyed.doc_id)
        snap = ref.get()
        if not snap.exists:
            return None
        data = snap.to_dict() or {}
        if (data.get("tenant_id") != keyed.tenant_id
                or data.get("submitted_by") != keyed.submitter.strip().lower()):
            # Not this scope's document: never served, never replayed.
            raise KeyReused(
                "idempotency_key is already in use; send a fresh random key",
                detail={"field": "idempotency_key"},
            )
        expires_at = data.get("expires_at")
        if isinstance(expires_at, datetime) and expires_at <= self._now():
            self._delete_if_expired(ref)
            return None
        claim = Claim(
            kind=str(data.get("kind") or ""),
            fingerprint=str(data.get("fingerprint") or ""),
            task_ids=tuple(data.get("task_ids") or ()),
            workflow_id=data.get("workflow_id"),
            dispatch=dict(data.get("dispatch") or {}),
        )
        if claim.kind != keyed.kind or claim.fingerprint != keyed.fingerprint:
            raise KeyReused(
                "this idempotency_key was already used for a different submission "
                f"in the last {int(KEY_WINDOW.total_seconds() // 3600)} hours; a key names "
                "ONE submission: re-send that submission unchanged, or send this one "
                "with a fresh key. Nothing was created",
                detail={"field": "idempotency_key", "kind": claim.kind},
            )
        return claim

    def _delete_if_expired(self, ref: Any) -> None:
        """Delete an expired claim inside a transaction that re-reads it, so a
        fresh claim written a moment ago by a concurrent submit is never
        deleted."""
        transaction = self._db.transaction()
        now = self._now()

        @firestore.transactional
        def run(txn: Any) -> None:
            snap = _snapshot(txn.get(ref))
            if not snap.exists:
                return
            expires_at = (snap.to_dict() or {}).get("expires_at")
            if isinstance(expires_at, datetime) and expires_at <= now:
                txn.delete(ref)

        run(transaction)
