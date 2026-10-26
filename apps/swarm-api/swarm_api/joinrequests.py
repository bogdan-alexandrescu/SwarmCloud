"""Join requests: a person asks to join a group workspace, and that
workspace's admins or a platform admin decide (docs/design/join-requests.md
§4.1-§4.3, §4.7; lane JOIN-STORE, part of #2325; owner's answers to Q1-Q6,
2026-10-25).

Three records, each signed with the settings key under its own domain
string, so none can ever verify as another or as a role grant or settings
block (`memberships.sign_record`):

* a REQUEST, `join_requests/{auto-id}`, `swarm-join-request/v1\\n`, over
  (ref, tenant_id, group, requester, requested_at, expires_at). It admits
  nobody. It is signed anyway because a worker that could change
  `requester` between the ask and the approval would have an admin approve
  someone they never saw: approval re-reads it and refuses one that fails
  its MAC (`join_request_invalid`), and no list shows one.
* a DECISION, `join_requests/{auto-id}/decisions/{auto-id}`,
  `swarm-join-decision/v1\\n`, append-only. A request's state is DERIVED,
  never stored: the newest valid decision by `at`, else `expired` once
  `expires_at` has passed, else `requested`. A forged "approved" does not
  verify and is skipped -- and even a verified one admits nobody: only the
  membership grant (`memberships.py`) admits, written in the decision's own
  transaction (`decide(also=...)`).
* a JOIN LINK, `join_links/{tenant_id}`, `swarm-join-link/v1\\n`: how a
  person finds a workspace without learning the others exist (§4.2, Q2 (a)).
  Off until an admin turns it on. Only the code's SHA-256 is stored, so a
  worker that reads the document learns no code; one that overwrites it
  fails the MAC and the link reads OFF -- denial of service, never
  admission. An unknown, rotated, switched-off or re-grouped code all get
  the one `join_link_unknown`, so probing codes learns nothing.

The `ref` ("j-" + 12 hex) is the handle every route uses. The document id is
a Firestore auto-id that no route serves and no audit row names: it is what
an overwrite would need.

How long a request lives (Q5 (a)): open 14 days, kept 90 days after it was
decided or expired, then pruned (`prune`, run by the ten-minute sweep). The
`admin_audit` rows are the permanent record, so pruning loses nothing an
audit needs. The rate limits of §4.1 are checked in the transaction that
writes the request.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from . import roles
from .admins import AUDIT_COLLECTION
from .errors import (
    ApiError,
    JoinDeniedRecently,
    JoinLinkUnknown,
    JoinRequestDecided,
    JoinRequestInvalid,
    JoinRequestsDailyLimit,
    JoinRequestsOpenLimit,
    JoinRequestUnknown,
    JoinRoleNotAllowed,
    JoinWorkspaceQueueFull,
    UpstreamUnavailable,
    ValidationFailed,
)
from .judgment import settings_keys
from .memberships import (
    at_text,
    clean_person,
    clean_text,
    counter,
    is_person,
    offered_tenant,
    parse_at,
    record_verifies,
    sign_record,
)
from .metrics import ApiMetrics

log = logging.getLogger(__name__)

REQUESTS = "join_requests"
DECISIONS = "decisions"
LINKS = "join_links"

REQUEST_DOMAIN = b"swarm-join-request/v1\n"
DECISION_DOMAIN = b"swarm-join-decision/v1\n"
LINK_DOMAIN = b"swarm-join-link/v1\n"
REQUEST_FIELDS: tuple[str, ...] = (
    "ref", "tenant_id", "group", "requester", "requested_at", "expires_at")
#: The reason is under the MAC too: it is shown to the person, and a worker
#: must not be able to put words in the deciding admin's mouth.
DECISION_FIELDS: tuple[str, ...] = (
    "ref", "tenant_id", "requester", "state", "by", "at", "via", "role", "reason")
LINK_FIELDS: tuple[str, ...] = ("tenant_id", "group", "code_sha256", "on", "set_by", "set_at")

REQUESTED = "requested"
APPROVED = "approved"
DENIED = "denied"
WITHDRAWN = "withdrawn"
EXPIRED = "expired"
DECISION_STATES = frozenset({APPROVED, DENIED, WITHDRAWN})

VIA_WORKSPACE_ADMIN = "workspace_admin"
VIA_PLATFORM_ADMIN = "platform_admin"
VIA_REQUESTER = "requester"
VIAS = frozenset({VIA_WORKSPACE_ADMIN, VIA_PLATFORM_ADMIN, VIA_REQUESTER})

#: The roles an approval may give (§4.3 step 4, Q4 (a)): the workspace
#: default, which the approving admin may lower to viewer. Never admin: an
#: admin is named person by person, after joining.
OFFERED_ROLES = frozenset({roles.VIEWER, roles.MEMBER})

# -- how long (Q5 (a)) ---------------------------------------------------------

#: Open for 14 days, then `expired`: a queue nobody owns must not grow
#: forever, and two weeks covers an admin's holiday.
OPEN_DAYS = 14
#: Kept 90 days after its decision or expiry, so the person and the admin can
#: still see what happened; the `admin_audit` row is permanent.
KEEP_DAYS = 90

# -- the rate limits (§4.1) ----------------------------------------------------

#: Open requests per person across workspaces: a person joining a team needs
#: one or two at a time; more is someone trawling for codes that work.
OPEN_PER_PERSON = 3
#: Requests per person per 24 hours, withdrawn ones included: asking and
#: withdrawing in a loop must not ring an admin's bell all day.
DAILY_PER_PERSON = 10
#: After a denial, a new request to the same workspace waits 7 days, so a
#: denial is not answered by asking again a minute later.
DENIED_HOLD_DAYS = 7
#: Open requests per workspace: more than an admin will read. Shown only to
#: someone holding a working code, so it reveals nothing to anyone else.
OPEN_PER_WORKSPACE = 50

MESSAGE_MAX = 200
REASON_MAX = 200
#: Bounded reads. A person's window covers every request a denial in the
#: last 7 days could belong to (14 + 7 days); the workspace's covers every
#: request that can still be open.
PERSON_SCAN = 200
WORKSPACE_SCAN = 500
DECISION_SCAN = 20
MINE_SHOWN = 50
PRUNE_BATCH = 200
#: A join code is `secrets.token_urlsafe(16)`, 22 characters; anything far
#: longer is hashed as the empty string, with the same work done.
CODE_MAX = 64

#: `admin_audit` actions (§4.7). Expiry writes none: it is derived.
AUDIT_REQUESTED = "join_requested"
AUDIT_APPROVED = "join_approved"
AUDIT_DENIED = "join_denied"
AUDIT_WITHDRAWN = "join_withdrawn"
AUDIT_LINK_CHANGED = "join_link_changed"

#: One sentence for every code that does not work (§4.2).
LINK_UNKNOWN_MESSAGE = "this join link does not work; ask the workspace's admin for a new one"
REQUEST_UNKNOWN_MESSAGE = "no join request has that reference"


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------


def sign_request(keys: tuple[str, ...], fields: Mapping[str, Any]) -> dict[str, Any]:
    return sign_record(keys, REQUEST_DOMAIN, fields, REQUEST_FIELDS)


def request_verifies(keys: tuple[str, ...], doc: Mapping[str, Any]) -> bool:
    return record_verifies(keys, REQUEST_DOMAIN, doc, REQUEST_FIELDS)


def sign_decision(keys: tuple[str, ...], fields: Mapping[str, Any]) -> dict[str, Any]:
    return sign_record(keys, DECISION_DOMAIN, fields, DECISION_FIELDS)


def decision_verifies(keys: tuple[str, ...], doc: Mapping[str, Any]) -> bool:
    return record_verifies(keys, DECISION_DOMAIN, doc, DECISION_FIELDS)


def sign_link(keys: tuple[str, ...], fields: Mapping[str, Any]) -> dict[str, Any]:
    return sign_record(keys, LINK_DOMAIN, fields, LINK_FIELDS)


def link_verifies(keys: tuple[str, ...], doc: Mapping[str, Any]) -> bool:
    return record_verifies(keys, LINK_DOMAIN, doc, LINK_FIELDS)


def code_hash(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JoinRequest:
    #: Firestore's auto-id. NEVER served: it is what an overwrite needs.
    doc_id: str
    ref: str
    tenant_id: str
    group: str
    requester: str
    requested_at: str
    expires_at: str
    message: str = ""


@dataclass(frozen=True)
class Decision:
    ref: str
    tenant_id: str
    requester: str
    state: str
    by: str
    at: str
    via: str
    role: str | None = None
    reason: str = ""


@dataclass(frozen=True)
class RequestView:
    """A request and its derived state."""

    request: JoinRequest
    state: str
    decision: Decision | None = None

    def to_api(self) -> dict[str, Any]:
        """The person's own view of their request. No document id, no
        tenant id and no group: the route adds the workspace's display name,
        which is all a working code ever shows (§4.2)."""
        out: dict[str, Any] = {"ref": self.request.ref, "state": self.state,
                               "requested_at": self.request.requested_at,
                               "expires_at": self.request.expires_at}
        if self.decision is not None:
            out["decided_at"] = self.decision.at
            if self.decision.state == DENIED:
                out["reason"] = self.decision.reason
        return out


@dataclass(frozen=True)
class LinkState:
    on: bool
    set_by: str = ""
    set_at: str = ""

    def to_api(self) -> dict[str, Any]:
        return {"on": self.on, "set_by": self.set_by, "set_at": self.set_at}


def state_of(request: JoinRequest, decisions: Iterable[Decision],
             now: datetime) -> tuple[str, Decision | None]:
    """The derived state (§4.1): the newest valid decision by `at` wins; with
    none, `expired` once `expires_at` has passed, else `requested`."""
    newest = max(decisions, key=lambda d: d.at, default=None)
    if newest is not None:
        return newest.state, newest
    expires = parse_at(request.expires_at)
    if expires is None or now >= expires:
        return EXPIRED, None
    return REQUESTED, None


def _request_shape(doc: Mapping[str, Any]) -> str | None:
    ref = doc.get("ref")
    if not isinstance(ref, str) or not ref.startswith("j-"):
        return "shape"
    tenant_id = doc.get("tenant_id")
    if not isinstance(tenant_id, str) or not tenant_id or tenant_id.startswith("u-"):
        return "shape"
    if not is_person(doc.get("requester")) or not isinstance(doc.get("group"), str):
        return "shape"
    if parse_at(doc.get("requested_at")) is None or parse_at(doc.get("expires_at")) is None:
        return "shape"
    return None


def _decision_shape(doc: Mapping[str, Any], request: JoinRequest) -> str | None:
    if (doc.get("ref"), doc.get("tenant_id"), doc.get("requester")) != (
            request.ref, request.tenant_id, request.requester):
        return "decision_shape"
    if doc.get("state") not in DECISION_STATES or doc.get("via") not in VIAS:
        return "decision_shape"
    if not isinstance(doc.get("by"), str) or parse_at(doc.get("at")) is None:
        return "decision_shape"
    return None


Reader = Callable[[Any], list[Any]]


def _stream(query: Any) -> list[Any]:
    return list(query.stream())


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------


class JoinStore:
    """Reads, verifies and writes join requests, their decisions and join
    links. Holds no cache: every decision path reads fresh (§4.4)."""

    def __init__(self, db: Any, settings: Any, *, metrics: ApiMetrics | None = None,
                 now: Callable[[], datetime] | None = None) -> None:
        self._db = db
        self._settings = settings
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._invalid = counter(
            metrics or ApiMetrics(), "join_request_invalid", "swarm_api_join_request_invalid_total",
            "Join requests, decisions and links the reader skipped, by reason (mac, shape, "
            "decision_mac, decision_shape, link_mac, link_shape).")

    def _keys(self) -> tuple[str, ...]:
        return settings_keys(getattr(self._settings, "settings_key", ""),
                             getattr(self._settings, "settings_key_previous", ""))

    def _requests(self) -> Any:
        return self._db.collection(REQUESTS)

    def _skip(self, kind: str, reason: str) -> None:
        log.warning("join %s invalid reason=%s", kind, reason)
        self._invalid.labels(reason=reason).inc()

    # -- reading --------------------------------------------------------------

    def _verified(self, snap: Any) -> JoinRequest | None:
        doc = snap.to_dict() or {}
        reason = _request_shape(doc)
        if reason is None and not request_verifies(self._keys(), doc):
            reason = "mac"
        if reason is not None:
            self._skip("request", reason)
            return None
        return JoinRequest(
            doc_id=snap.id, ref=doc["ref"], tenant_id=doc["tenant_id"], group=doc["group"],
            requester=doc["requester"], requested_at=doc["requested_at"],
            expires_at=doc["expires_at"], message=str(doc.get("message") or ""))

    def _decisions(self, request: JoinRequest, read: Reader) -> list[Decision]:
        query = (self._requests().document(request.doc_id).collection(DECISIONS)
                 .order_by("at", direction=firestore.Query.DESCENDING).limit(DECISION_SCAN))
        keys = self._keys()
        found: list[Decision] = []
        for snap in read(query):
            doc = snap.to_dict() or {}
            reason = _decision_shape(doc, request)
            if reason is None and not decision_verifies(keys, doc):
                reason = "decision_mac"
            if reason is not None:
                self._skip("decision", reason)
                continue
            found.append(Decision(
                ref=doc["ref"], tenant_id=doc["tenant_id"], requester=doc["requester"],
                state=doc["state"], by=doc["by"], at=doc["at"], via=doc["via"],
                role=doc.get("role"), reason=str(doc.get("reason") or "")))
        return found

    def _view(self, request: JoinRequest, read: Reader, now: datetime) -> RequestView:
        state, decision = state_of(request, self._decisions(request, read), now)
        return RequestView(request=request, state=state, decision=decision)

    def _views(self, query: Any, read: Reader, now: datetime) -> list[RequestView]:
        views = []
        for snap in read(query):
            request = self._verified(snap)
            if request is not None:
                views.append(self._view(request, read, now))
        return views

    def _person_window(self, requester: str, now: datetime, read: Reader) -> list[RequestView]:
        since = now - timedelta(days=OPEN_DAYS + DENIED_HOLD_DAYS)
        query = (self._requests()
                 .where(filter=FieldFilter("requester", "==", requester))
                 .where(filter=FieldFilter("requested_at", ">=", at_text(since)))
                 .order_by("requested_at", direction=firestore.Query.DESCENDING)
                 .limit(PERSON_SCAN))
        return [v for v in self._views(query, read, now) if v.request.requester == requester]

    def _open_query(self, tenant_id: str | None, now: datetime) -> Any:
        # Open means now < requested_at + OPEN_DAYS: strictly after the cutoff.
        since = at_text(now - timedelta(days=OPEN_DAYS))
        query = self._requests()
        if tenant_id is not None:
            query = query.where(filter=FieldFilter("tenant_id", "==", tenant_id))
        return (query.where(filter=FieldFilter("requested_at", ">", since))
                .order_by("requested_at", direction=firestore.Query.DESCENDING)
                .limit(WORKSPACE_SCAN))

    def _open_in(self, tenant_id: str | None, now: datetime, read: Reader) -> list[RequestView]:
        return [v for v in self._views(self._open_query(tenant_id, now), read, now)
                if v.state == REQUESTED
                and (tenant_id is None or v.request.tenant_id == tenant_id)]

    def _read(self, what: Callable[[], Any]) -> Any:
        try:
            return what()
        except ApiError:
            raise
        except Exception as exc:  # noqa: BLE001 - any Firestore failure is a retry
            log.warning("join requests unreadable (%s)", type(exc).__name__)
            raise UpstreamUnavailable("join requests could not be read; retry shortly") from None

    def view(self, ref: str) -> RequestView:
        """One request by its `ref`, read fresh."""
        now = self._now()
        return self._read(lambda: self._view(self._find(ref, _stream), _stream, now))

    def mine(self, requester: str) -> list[RequestView]:
        """The person's own requests, newest first, every state (§4.8)."""
        requester = (requester or "").strip().lower()
        now = self._now()
        query = (self._requests()
                 .where(filter=FieldFilter("requester", "==", requester))
                 .order_by("requested_at", direction=firestore.Query.DESCENDING)
                 .limit(MINE_SHOWN))
        return self._read(lambda: [v for v in self._views(query, _stream, now)
                                   if v.request.requester == requester])

    def open_for(self, tenant_ids: Iterable[str] | None) -> list[RequestView]:
        """The open requests a decider sees, newest first: those of
        `tenant_ids` (the workspaces a workspace admin administers), or of
        every workspace for None (a platform admin)."""
        now = self._now()
        if tenant_ids is None:
            return self._read(lambda: self._open_in(None, now, _stream))
        out: list[RequestView] = []
        for tenant_id in sorted(set(tenant_ids)):
            out.extend(self._read(lambda t=tenant_id: self._open_in(t, now, _stream)))
        return sorted(out, key=lambda v: v.request.requested_at, reverse=True)

    def _find(self, ref: Any, read: Reader) -> JoinRequest:
        if not isinstance(ref, str) or not ref.startswith("j-") or len(ref) > 32:
            raise JoinRequestUnknown(REQUEST_UNKNOWN_MESSAGE)
        snaps = read(self._requests().where(filter=FieldFilter("ref", "==", ref)).limit(5))
        if not snaps:
            raise JoinRequestUnknown(REQUEST_UNKNOWN_MESSAGE)
        for snap in snaps:
            request = self._verified(snap)
            if request is not None:
                return request
        log.warning("join request failed verification on a decision")
        raise JoinRequestInvalid(
            "this join request was changed after it was made, so it cannot be decided; an "
            "operator has been alerted. The person can ask again.")

    # -- asking ----------------------------------------------------------------

    def ask(self, *, tenant_id: str, group: str, requester: str,
            message: str | None = None) -> tuple[RequestView, bool]:
        """Make a request, or return the person's open one for that workspace
        (False: nothing was written). Who may ask -- not a member already,
        not a platform admin, a working code -- is the route's to check;
        this checks the record's own rules and the limits of §4.1, in the
        transaction that writes it."""
        tenant_id = offered_tenant(tenant_id)
        group = clean_person(group, "group")
        requester = clean_person(requester, "requester")
        message = clean_text(message, "message", MESSAGE_MAX)
        keys = self._keys()
        now = self._now()
        ref = "j-" + secrets.token_hex(6)
        requested_at = at_text(now)
        fields = {"ref": ref, "tenant_id": tenant_id, "group": group, "requester": requester,
                  "requested_at": requested_at,
                  "expires_at": at_text(now + timedelta(days=OPEN_DAYS))}
        doc = dict(fields, message=message, mac=sign_request(keys, fields))
        row = {"action": AUDIT_REQUESTED, "target_email": requester,
               "target_tenant_id": tenant_id, "by": requester, "at": now,
               "detail": {"ref": ref, "via": "console"}}
        transaction = self._db.transaction()

        @firestore.transactional
        def _apply(txn: Any) -> tuple[RequestView, bool]:
            def read(query: Any) -> list[Any]:
                return list(txn.get(query))

            mine = self._person_window(requester, now, read)
            for view in mine:
                if view.request.tenant_id == tenant_id and view.state == REQUESTED:
                    return view, False
            self._check_person_limits(mine, tenant_id, now)
            queue = self._open_in(tenant_id, now, read)
            if len(queue) >= OPEN_PER_WORKSPACE:
                raise JoinWorkspaceQueueFull(
                    "this workspace has as many open join requests as its admins can read; "
                    "ask again once they have answered some",
                    _seconds_until(min(v.request.expires_at for v in queue), now))
            ref_doc = self._requests().document()
            txn.set(ref_doc, doc)
            txn.set(self._db.collection(AUDIT_COLLECTION).document(), row)
            request = JoinRequest(doc_id=ref_doc.id, message=message, **fields)
            return RequestView(request=request, state=REQUESTED), True

        view, created = _apply(transaction)
        if created:
            log.info("join request made tenant=%s", tenant_id)
        return view, created

    @staticmethod
    def _check_person_limits(mine: list[RequestView], tenant_id: str, now: datetime) -> None:
        open_mine = [v for v in mine if v.state == REQUESTED]
        if len(open_mine) >= OPEN_PER_PERSON:
            raise JoinRequestsOpenLimit(
                f"you have {len(open_mine)} open join requests, the most at once; withdraw "
                "one or wait for an answer",
                _seconds_until(min(v.request.expires_at for v in open_mine), now))
        day_ago = now - timedelta(days=1)
        today = [v for v in mine if (parse_at(v.request.requested_at) or now) > day_ago]
        if len(today) >= DAILY_PER_PERSON:
            oldest = min(parse_at(v.request.requested_at) or now for v in today)
            raise JoinRequestsDailyLimit(
                f"you have asked to join {len(today)} times in the last day, the most "
                "allowed; ask again tomorrow",
                (oldest + timedelta(days=1) - now).total_seconds())
        denials = [parse_at(v.decision.at) for v in mine
                   if v.request.tenant_id == tenant_id and v.state == DENIED
                   and v.decision is not None]
        latest = max((d for d in denials if d is not None), default=None)
        if latest is not None and now < latest + timedelta(days=DENIED_HOLD_DAYS):
            opens = latest + timedelta(days=DENIED_HOLD_DAYS)
            raise JoinDeniedRecently(
                f"this workspace's admin answered your last request {DENIED_HOLD_DAYS} days "
                f"ago or less; you can ask again on {opens.date().isoformat()}",
                (opens - now).total_seconds(), opens_on=opens.date().isoformat())

    # -- deciding --------------------------------------------------------------

    def decide(self, ref: str, *, state: str, by: str, via: str, role: str | None = None,
               reason: str | None = None, admitted: bool = False,
               also: Callable[[Any, JoinRequest, Decision], None] | None = None,
               ) -> tuple[RequestView, Decision]:
        """Approve, deny or withdraw an open request, in one transaction that
        re-reads it fresh, verifies its MAC and its state, and writes the
        decision and its audit row. `also(writer, request, decision)` runs
        inside the same transaction, after both are set: approval's
        membership and role-override writes go there, so the decision and
        what it does land together or not at all (§4.3). Who may decide is
        the route's to check (§4.6, `join_scope`); a withdrawal is checked
        here, because only the request names its requester."""
        if state not in DECISION_STATES:
            raise ValidationFailed("state is approved, denied or withdrawn",
                                   detail={"field": "state"})
        if via not in VIAS or (via == VIA_REQUESTER) != (state == WITHDRAWN):
            raise ValidationFailed("a withdrawal is the requester's, and a decision an admin's",
                                   detail={"field": "via"})
        if state == APPROVED:
            if role is None:
                raise ValidationFailed("an approval names the role it gives",
                                       detail={"field": "role"})
            role = roles.check_role(role)
            if role not in OFFERED_ROLES:
                raise JoinRoleNotAllowed(
                    "an approval gives viewer or member; name an admin after they have joined")
        else:
            role = None
        reason = clean_text(reason, "reason", REASON_MAX, required=state == DENIED)
        by = (by or "").strip().lower()
        keys = self._keys()
        now = self._now()
        transaction = self._db.transaction()

        @firestore.transactional
        def _apply(txn: Any) -> tuple[RequestView, Decision]:
            def read(query: Any) -> list[Any]:
                return list(txn.get(query))

            request = self._find(ref, read)
            if state == WITHDRAWN and by != request.requester:
                raise JoinRequestUnknown(REQUEST_UNKNOWN_MESSAGE)
            current = self._view(request, read, now)
            if current.state != REQUESTED:
                raise JoinRequestDecided(f"this join request is already {current.state}")
            fields = {"ref": request.ref, "tenant_id": request.tenant_id,
                      "requester": request.requester, "state": state, "by": by,
                      "at": at_text(now), "via": via, "role": role, "reason": reason}
            decision = Decision(**fields)
            txn.set(self._requests().document(request.doc_id).collection(DECISIONS).document(),
                    dict(fields, mac=sign_decision(keys, fields)))
            txn.set(self._db.collection(AUDIT_COLLECTION).document(),
                    _decision_row(request, decision, admitted, now))
            if also is not None:
                also(txn, request, decision)
            return RequestView(request=request, state=state, decision=decision), decision

        view, decision = _apply(transaction)
        log.info("join request decided tenant=%s state=%s via=%s",
                 view.request.tenant_id, state, via)
        return view, decision

    # -- pruning (Q5 (a)) -------------------------------------------------------

    def prune(self) -> int:
        """Delete requests decided or expired more than KEEP_DAYS ago, with
        their decisions; at most PRUNE_BATCH per call. A record that does not
        verify and is older than any keep window is deleted too: no reader
        would ever show it. Returns how many requests were deleted."""
        now = self._now()
        cutoff = at_text(now - timedelta(days=KEEP_DAYS))
        query = (self._requests()
                 .where(filter=FieldFilter("requested_at", "<", cutoff))
                 .order_by("requested_at").limit(PRUNE_BATCH))
        deleted = 0
        for snap in self._read(lambda: _stream(query)):
            request = self._verified(snap)
            if request is not None:
                view = self._view(request, _stream, now)
                if view.state == REQUESTED:
                    continue
                ended = parse_at(view.decision.at if view.decision else request.expires_at)
                if ended is not None and now < ended + timedelta(days=KEEP_DAYS):
                    continue
            batch = self._db.batch()
            for child in snap.reference.collection(DECISIONS).limit(DECISION_SCAN * 5).stream():
                batch.delete(child.reference)
            batch.delete(snap.reference)
            batch.commit()
            deleted += 1
        if deleted:
            log.info("join requests pruned count=%d", deleted)
        return deleted

    # -- the join link (§4.2) ----------------------------------------------------

    def _link_doc(self, tenant_id: str) -> Mapping[str, Any] | None:
        snap = self._db.collection(LINKS).document(tenant_id).get()
        if not snap.exists:
            return None
        doc = snap.to_dict() or {}
        reason = None
        if doc.get("tenant_id") != tenant_id or not isinstance(doc.get("group"), str) \
                or not isinstance(doc.get("on"), bool):
            reason = "link_shape"
        elif not link_verifies(self._keys(), doc):
            reason = "link_mac"
        if reason is not None:
            self._skip("link", reason)
            return None
        return doc

    def link(self, tenant_id: str) -> LinkState:
        """The workspace's link as its admin sees it. A link that does not
        verify reads off."""
        doc = self._read(lambda: self._link_doc(offered_tenant(tenant_id)))
        if doc is None:
            return LinkState(on=False)
        return LinkState(on=doc["on"], set_by=str(doc.get("set_by") or ""),
                         set_at=str(doc.get("set_at") or ""))

    def set_link(self, *, tenant_id: str, group: str, on: bool, rotate: bool,
                 by: str) -> tuple[LinkState, str | None]:
        """Turn the link on or off, or rotate its code. Returns the code only
        when one was just made -- the one time it is ever shown. Turning a
        link on that has no code, or whose workspace's group changed, makes
        one."""
        tenant_id = offered_tenant(tenant_id)
        group = clean_person(group, "group")
        by = (by or "").strip().lower()
        keys = self._keys()
        current = self._read(lambda: self._link_doc(tenant_id))
        kept = None
        if current is not None and current.get("group") == group:
            kept = current.get("code_sha256") if isinstance(current.get("code_sha256"), str) \
                else None
        made = bool(rotate) or (bool(on) and kept is None)
        code = secrets.token_urlsafe(16) if made else None
        now = self._now()
        fields = {"tenant_id": tenant_id, "group": group,
                  "code_sha256": code_hash(code) if code is not None else kept,
                  "on": bool(on), "set_by": by, "set_at": at_text(now)}
        batch = self._db.batch()
        batch.set(self._db.collection(LINKS).document(tenant_id),
                  dict(fields, mac=sign_link(keys, fields)))
        # Never the code or its hash (§4.7).
        batch.set(self._db.collection(AUDIT_COLLECTION).document(), {
            "action": AUDIT_LINK_CHANGED, "target_tenant_id": tenant_id, "by": by, "at": now,
            "detail": {"on": bool(on), "rotated": made}})
        batch.commit()
        log.info("join link changed tenant=%s on=%s rotated=%s", tenant_id, bool(on), made)
        return LinkState(on=bool(on), set_by=by, set_at=fields["set_at"]), code

    def lookup(self, code: Any, registered: Mapping[str, str]) -> tuple[str, str]:
        """(tenant_id, group) for a working code: on, signed, for a group
        workspace whose id still selects the group the link names. Anything
        else is one `join_link_unknown`, with the same work done."""
        text = code if isinstance(code, str) and len(code) <= CODE_MAX else ""
        digest = code_hash(text)
        query = self._db.collection(LINKS).where(
            filter=FieldFilter("code_sha256", "==", digest)).limit(5)
        snaps = self._read(lambda: _stream(query))
        keys = self._keys()
        found: tuple[str, str] | None = None
        for snap in snaps:
            doc = snap.to_dict() or {}
            tenant_id = doc.get("tenant_id")
            registered_group = registered.get(tenant_id) if isinstance(tenant_id, str) else None
            works = (link_verifies(keys, doc) and doc.get("on") is True
                     and tenant_id == snap.id and not tenant_id.startswith("u-")
                     and isinstance(registered_group, str)
                     and registered_group.strip().lower() == doc.get("group"))
            if works and found is None:
                found = (tenant_id, doc["group"])
        if found is None or not text:
            raise JoinLinkUnknown(LINK_UNKNOWN_MESSAGE)
        return found


def _seconds_until(at: str, now: datetime) -> float:
    when = parse_at(at) or now
    return max(0.0, (when - now).total_seconds())


def _decision_row(request: JoinRequest, decision: Decision, admitted: bool,
                  now: datetime) -> dict[str, Any]:
    """The decision's `admin_audit` row (§4.7). A denial's reason stays in
    the decision, redacted; it is not copied here."""
    if decision.state == APPROVED:
        action = AUDIT_APPROVED
        detail: dict[str, Any] = {"ref": request.ref, "role": decision.role,
                                  "admitted": bool(admitted), "via": decision.via}
    elif decision.state == DENIED:
        action = AUDIT_DENIED
        detail = {"ref": request.ref, "via": decision.via}
    else:
        action = AUDIT_WITHDRAWN
        detail = {"ref": request.ref}
    return {"action": action, "target_email": request.requester,
            "target_tenant_id": request.tenant_id, "by": decision.by, "at": now,
            "detail": detail}
