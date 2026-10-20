"""The QA identity, swarm-api's half (docs/design/qa-identity.md §4.1-§4.3, §4.9;
lane QI-API, part of #1649; owner decisions 2026-10-19 in §7).

Option B. A QA service account (`ApiSettings.qa_identity_console_account`,
`qa-console@` on dev) has its own service workspace. A live-browser attempt
of a GRANTED tenant asks swarm-api, as the worker, for a short-lived JWT signed
as that account; the gate adds it to the browser's requests to the console
host, IAP admits it (M1), and the agent sees the console as a member of the QA
workspace. The agent never holds the credential.

THE GRANT. `qa_identity_grants/{tenant_id}`, one document per consuming
tenant, written by a platform admin with a reason (Q2 (a)) and only here.
`state` is `granted` or `revoked`; a revoke keeps the document and a re-grant
overwrites it. Every change appends an `admin_audit` row IN THE SAME
TRANSACTION (`_audit`), as `swarm_api.admins` does, so a change without its
row cannot exist. The QA workspace's own tenant cannot be granted
(`qa_identity_self`): it would let the identity's own tasks sign in as it.

THE GRANT IS READ TWICE (invariant 9): at submission (`refuse_unless_granted`,
for a task, a batch task, a workflow step and a child), and again at every
signing, so a revoke takes effect on the attempt's next renewal.

THE SIGNING ROUTE, `POST /v1/attempts/{attempt_id}/qa-identity`, is proven and
fenced as the egress ticket's (`swarm_api.egress`): the tenant's worker ID
token, the attempt key's Ed25519 signature over the raw body under this
route's own purpose (QA_IDENTITY_PURPOSE), the task RUNNING under this
attempt's live lease with no cancel requested. Then the task's SIGNED input
must name the identity (contract request 106), the tenant must hold a
`granted` document, and the attempt is under its rate (12 an hour; a renewal
every 10 minutes needs 6). Only then is IAM Credentials asked to sign.

THE CREDENTIAL is a JWT whose `aud` is exactly `https://<console host>/*` --
the host `console_url` names, so it cannot drift from the one swarm-api is
deployed behind -- and whose life is `lifetime_seconds` (900 by default,
clamped 300-3600). IAP accepts it and nothing else does (M1: the same JWT is
401 at a Google API). It is in the response body, under `Cache-Control:
no-store`, and nowhere else: not a log line, not a document, not the audit
row, which carries its `jti`. The route is in `main.UNCAPTURED_ROUTES`.

THE HEADER IS `Authorization` (M3, measured 2026-10-20): Chromium refuses to
add `Proxy-Authorization` through `Fetch.continueRequest`, so the design's
example `"header": "Proxy-Authorization"` is not what the gate can send. M1
measured that IAP admits the JWT in either header.

EVERY REFUSAL is a fixed code and a fixed sentence: nothing from the request
is interpolated into a message.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlsplit

from google.cloud import firestore
from pydantic import Field

from swarm_common.admission import _snapshot
from swarm_common.identity import tenant_id_for_user
from swarm_common.models import new_id, utcnow
from swarm_common.states import TaskState

from .admins import AUDIT_COLLECTION
from .childkey import AttemptTuple, verify_proof
from .children import ChildService, _AttemptFields, _lease_is_live, parse_body
from .errors import ApiError, Forbidden, NotFound, RateLimited, ValidationFailed
from .handoff import LIVE_AUDIT
from .redaction import redact
from .store import LEASES, TASKS, TENANTS
from .verdictpublish import spec_refusal

log = logging.getLogger(__name__)

#: The input key the frozen catalogue declares on `claude-code-browser`
#: (contract request 106), and its one value.
QA_IDENTITY_INPUT_KEY = "qa_identity"
IDENTITY = "console"
IDENTITIES: tuple[str, ...] = (IDENTITY,)

GRANTS_COLLECTION = "qa_identity_grants"
STATE_GRANTED = "granted"
STATE_REVOKED = "revoked"
AUDIT_GRANTED = "qa_identity_granted"
AUDIT_REVOKED = "qa_identity_revoked"
#: The `live_audit` entry each credential issued appends (§4.2).
AUDIT_ISSUED = "qa_identity_issued"

#: The worker's signing purpose for this route; never the live session's,
#: the egress ticket's or a child's.
QA_IDENTITY_PURPOSE = "swarm-qa-identity-request/v1"
MAX_REQUEST_BYTES = 4096
REASON_MAX = 200

#: Q5 (a): 15 minutes, renewed at 10. The floor keeps a renewal from becoming
#: a hot loop on a misconfigured setting; IAP refuses a JWT whose life is over
#: an hour.
DEFAULT_LIFETIME_SECONDS = 900
MIN_LIFETIME_SECONDS = 300
MAX_LIFETIME_SECONDS = 3600
#: Per attempt per rolling hour. A renewal every 10 minutes needs 6, so 12
#: leaves room for a worker restart without letting one attempt mint freely.
ISSUES_PER_HOUR = 12
RATE_WINDOW = timedelta(hours=1)

#: What the gate adds the credential as (M3) and with which scheme.
CREDENTIAL_HEADER = "Authorization"
CREDENTIAL_SCHEME = "Bearer"


# --------------------------------------------------------------------------
# Refusals: a code and a fixed sentence each
# --------------------------------------------------------------------------


class QaIdentityNotGranted(Forbidden):
    code = "qa_identity_not_granted"


class QaIdentityUnconfigured(ValidationFailed):
    """422 at submission: the deployment offers no QA identity."""

    code = "qa_identity_unconfigured"


class QaIdentityUnavailable(ApiError):
    """503 at signing, under the same code as the submission's 422."""

    status_code = 503
    code = "qa_identity_unconfigured"


class QaIdentityOrigin(Forbidden):
    code = "qa_identity_origin"


class QaIdentityNotRequested(ValidationFailed):
    code = "qa_identity_not_requested"


class QaIdentitySelf(Forbidden):
    code = "qa_identity_self"


class QaIdentitySignFailed(ApiError):
    status_code = 503
    code = "qa_identity_sign_failed"


class BadSignature(ApiError):
    status_code = 401
    code = "bad_signature"


class StaleGeneration(ApiError):
    status_code = 409
    code = "stale_generation"


class LeaseNotLive(ApiError):
    status_code = 409
    code = "lease_not_live"


class SpecUnverified(Forbidden):
    """The task document's signature does not verify: its input may have been
    rewritten after submission, so it is not read as a request."""

    code = "spec_unverified"


NOT_GRANTED_MESSAGE = (
    "the QA identity is granted per workspace by a platform admin, and this workspace "
    "has no grant; ask a platform admin to grant it in Admin › QA identity"
)
UNCONFIGURED_MESSAGE = "this deployment has no QA identity (QA_IDENTITY_CONSOLE_ACCOUNT is empty)"


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def account(settings: Any) -> str:
    return str(getattr(settings, "qa_identity_console_account", "") or "").strip().lower()


def console_host(settings: Any) -> str:
    """The host `console_url` names: what the JWT's audience and the Origin
    rule are bound to. "" when there is no console URL or it is not https."""
    raw = str(getattr(settings, "console_url", "") or "").strip()
    if not raw:
        return ""
    parts = urlsplit(raw)
    if parts.scheme != "https" or not parts.hostname:
        return ""
    return parts.netloc.lower()


def configured(settings: Any) -> bool:
    return bool(account(settings)) and bool(console_host(settings))


def console_origin(settings: Any) -> str:
    host = console_host(settings)
    return f"https://{host}" if host else ""


def lifetime_seconds(configured_seconds: Any) -> int:
    try:
        seconds = int(configured_seconds)
    except (TypeError, ValueError):
        seconds = DEFAULT_LIFETIME_SECONDS
    return max(MIN_LIFETIME_SECONDS, min(seconds, MAX_LIFETIME_SECONDS))


def own_tenant(settings: Any) -> str:
    """The QA workspace's tenant: the frozen slug of the account's address."""
    email = account(settings)
    return tenant_id_for_user(email) if email else ""


def names_identity(payload: Mapping[str, Any] | None) -> bool:
    return isinstance(payload, Mapping) and QA_IDENTITY_INPUT_KEY in payload


# --------------------------------------------------------------------------
# The grants
# --------------------------------------------------------------------------


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def grant_to_api(doc: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "tenant_id": doc.get("tenant_id"),
        "identity": doc.get("identity"),
        "state": doc.get("state"),
        "granted_by": doc.get("granted_by"),
        "granted_at": _iso(doc.get("granted_at")),
        "reason": doc.get("reason"),
        "revoked_by": doc.get("revoked_by"),
        "revoked_at": _iso(doc.get("revoked_at")),
    }


def _audit(txn: Any, db: Any, *, action: str, tenant_id: str, by: str, now: datetime,
           detail: Mapping[str, Any]) -> None:
    """One `admin_audit` row, inside `txn`: the only writer of this lane's rows,
    and it only ever creates."""
    txn.set(db.collection(AUDIT_COLLECTION).document(), {
        "action": action,
        "target_tenant_id": tenant_id,
        "by": by,
        "at": now,
        "detail": dict(detail),
    })


def check_reason(reason: Any) -> str:
    if not isinstance(reason, str) or not reason.strip():
        raise ValidationFailed("a grant needs a reason", detail={"field": "reason"})
    if len(reason) > REASON_MAX:
        raise ValidationFailed(f"a reason is at most {REASON_MAX} characters",
                               detail={"field": "reason"})
    # Redacted before it is written: a pasted token must not reach a document.
    return redact(reason.strip()).text[:REASON_MAX]


class QaIdentityGrants:
    """`qa_identity_grants/`: read by submission, signing and the member
    route; written by the admin routes only."""

    def __init__(self, db: Any, settings: Any, *, now: Callable[[], datetime] = utcnow) -> None:
        self._db = db
        self._settings = settings
        self._now = now

    def _ref(self, tenant_id: str) -> Any:
        return self._db.collection(GRANTS_COLLECTION).document(tenant_id)

    def read(self, tenant_id: str) -> dict[str, Any] | None:
        snap = self._ref(tenant_id).get()
        return (snap.to_dict() or {}) if snap.exists else None

    def is_granted(self, tenant_id: str) -> bool:
        doc = self.read(tenant_id)
        return bool(doc) and doc.get("state") == STATE_GRANTED and doc.get("identity") == IDENTITY \
            and doc.get("tenant_id") == tenant_id

    def list(self) -> list[dict[str, Any]]:
        docs = [snap.to_dict() or {} for snap in self._db.collection(GRANTS_COLLECTION).stream()]
        return sorted((grant_to_api(d) for d in docs), key=lambda g: str(g["tenant_id"] or ""))

    def _check_target(self, tenant_id: str) -> None:
        if tenant_id == own_tenant(self._settings):
            raise QaIdentitySelf("the QA workspace cannot be granted its own identity")
        snap = self._db.collection(TENANTS).document(tenant_id).get()
        if not snap.exists:
            raise NotFound("no such workspace")

    def grant(self, tenant_id: str, *, reason: Any, by: str) -> dict[str, Any]:
        reason = check_reason(reason)
        self._check_target(tenant_id)
        now = self._now()
        ref = self._ref(tenant_id)
        doc = {
            "tenant_id": tenant_id,
            "identity": IDENTITY,
            "state": STATE_GRANTED,
            "granted_by": by,
            "granted_at": now,
            "reason": reason,
            "revoked_by": None,
            "revoked_at": None,
        }
        transaction = self._db.transaction()

        @firestore.transactional
        def _apply(txn: Any) -> None:
            txn.set(ref, doc)
            _audit(txn, self._db, action=AUDIT_GRANTED, tenant_id=tenant_id, by=by, now=now,
                   detail={"identity": IDENTITY, "reason": reason})

        _apply(transaction)
        log.info("qa identity granted tenant=%s identity=%s", tenant_id, IDENTITY)
        return grant_to_api(doc)

    def revoke(self, tenant_id: str, *, by: str) -> dict[str, Any]:
        now = self._now()
        ref = self._ref(tenant_id)
        transaction = self._db.transaction()

        @firestore.transactional
        def _apply(txn: Any) -> dict[str, Any]:
            snap = _snapshot(txn.get(ref))
            if not snap.exists:
                raise NotFound("this workspace holds no QA identity grant")
            doc = {**(snap.to_dict() or {}), "state": STATE_REVOKED, "revoked_by": by,
                   "revoked_at": now}
            txn.set(ref, doc)
            _audit(txn, self._db, action=AUDIT_REVOKED, tenant_id=tenant_id, by=by, now=now,
                   detail={"identity": IDENTITY})
            return doc

        doc = _apply(transaction)
        log.info("qa identity revoked tenant=%s identity=%s", tenant_id, IDENTITY)
        return grant_to_api(doc)


def refuse_unless_granted(settings: Any, db: Any, tenant_id: str, payload: Mapping[str, Any],
                          *, step_id: str | None = None) -> None:
    """§4.3 at submission: a task, batch task, workflow step or child whose
    input names `qa_identity` needs this deployment's account and its tenant's
    `granted` document. NOT a `refusals` switch: see the entry in
    tests/unit/control_plane/test_new_refusals_ship_off.py."""
    if not names_identity(payload):
        return
    detail: dict[str, Any] = {"key": QA_IDENTITY_INPUT_KEY}
    prefix = ""
    if step_id is not None:
        detail["step_id"] = step_id
        prefix = f"step {step_id!r}: "
    if not configured(settings):
        raise QaIdentityUnconfigured(prefix + UNCONFIGURED_MESSAGE, detail=detail)
    if not QaIdentityGrants(db, settings).is_granted(tenant_id):
        raise QaIdentityNotGranted(prefix + NOT_GRANTED_MESSAGE, detail=detail)


# --------------------------------------------------------------------------
# The signer: IAM Credentials' signJwt, as the QA account
# --------------------------------------------------------------------------


class JwtSigner(Protocol):
    def sign_jwt(self, account: str, claims: Mapping[str, Any]) -> str:
        """The JWT Google signs for `account` over `claims`. Raises on any
        refusal; the message never carries the JWT."""


class IamJwtSigner:
    """`iamcredentials.googleapis.com ... :signJwt` with swarm-api's own
    credentials, which hold `swarmQaJwtSigner` on the QA account alone
    (terraform/bootstrap/qa_identity.tf). The session is built on first use,
    so building the app opens no connection and needs no credentials."""

    ROOT = "https://iamcredentials.googleapis.com/v1"
    SCOPE = "https://www.googleapis.com/auth/cloud-platform"

    def __init__(self, session_factory: Callable[[], Any] | None = None) -> None:
        self._session_factory = session_factory
        self._session: Any = None

    def _authorized(self) -> Any:
        if self._session is None:
            if self._session_factory is not None:
                self._session = self._session_factory()
            else:
                import google.auth
                from google.auth.transport.requests import AuthorizedSession

                credentials, _ = google.auth.default(scopes=[self.SCOPE])
                self._session = AuthorizedSession(credentials)
        return self._session

    def sign_jwt(self, account: str, claims: Mapping[str, Any]) -> str:
        import json

        url = f"{self.ROOT}/projects/-/serviceAccounts/{account}:signJwt"
        response = self._authorized().post(url, json={"payload": json.dumps(dict(claims))},
                                           timeout=10)
        if response.status_code != 200:
            # The status only: the body is Google's and is not needed to act.
            raise RuntimeError(f"signJwt answered {response.status_code}")
        signed = (response.json() or {}).get("signedJwt")
        if not isinstance(signed, str) or not signed:
            raise RuntimeError("signJwt answered no JWT")
        return signed


def build_claims(*, account: str, host: str, issued_at: int, lifetime: int,
                 jti: str) -> dict[str, Any]:
    """The payload IAP verifies for a service account's own JWT (M1): issuer
    and subject the account, audience the console host's every path."""
    return {
        "iss": account,
        "sub": account,
        "aud": f"https://{host}/*",
        "iat": issued_at,
        "exp": issued_at + lifetime,
        "jti": jti,
    }


# --------------------------------------------------------------------------
# The worker's route
# --------------------------------------------------------------------------


class QaIdentityRequest(_AttemptFields):
    task_id: str = Field(min_length=1, max_length=128)
    identity: str = Field(min_length=1, max_length=32)


def qa_identity_message(method: str, path: str, body: bytes, timestamp: str) -> bytes:
    """What the worker signs to ask for a QA credential: the live-session shape
    under this route's own purpose."""
    digest = hashlib.sha256(body).hexdigest()
    return f"{QA_IDENTITY_PURPOSE}\n{method.upper()}\n{path}\n{digest}\n{timestamp}".encode()


class QaIdentityService:
    def __init__(self, *, settings: Any, db: Any, children: ChildService | None,
                 signer: JwtSigner | None, now: Callable[[], datetime] = utcnow,
                 clock: Callable[[], float] = time.time) -> None:
        self._settings = settings
        self._db = db
        self._children = children
        self._signer = signer
        self._now = now
        self._clock = clock

    def _prove(self, authorization: str | None, attempt: AttemptTuple, *, path: str,
               body: bytes, proof: str | None, timestamp: str | None) -> None:
        children = self._children
        if children is None or not children._keys.configured:
            raise QaIdentityUnavailable(
                "this deployment has no swarm-child-key, so no attempt key can be proven")
        try:
            children.authenticate_worker(authorization, attempt.tenant_id)
        except ApiError as exc:
            if exc.status_code == 429:
                raise
            raise BadSignature("only this tenant's worker may ask for a QA credential") from None
        unproven = BadSignature("the attempt proof did not verify for this attempt")
        if not proof or not timestamp or not re.fullmatch(r"\d{1,12}", timestamp):
            raise unproven
        skew = int(getattr(self._settings, "child_proof_skew_seconds", 120))
        if abs(self._clock() - int(timestamp)) > skew:
            raise unproven
        registration = children._registration(attempt)
        if registration is None:
            raise unproven
        if not verify_proof(registration["public_key"], proof,
                            qa_identity_message("POST", path, body, timestamp)):
            raise unproven

    def _fence(self, attempt: AttemptTuple) -> dict[str, Any]:
        task_snap = self._db.collection(TASKS).document(attempt.task_id).get()
        lease_snap = self._db.collection(LEASES).document(attempt.lease_id).get()
        task = task_snap.to_dict() if task_snap.exists else None
        lease = lease_snap.to_dict() if lease_snap.exists else None
        if task is None or task.get("tenant_id") != attempt.tenant_id:
            raise NotFound("no such task")
        if int(task.get("current_generation") or 0) != attempt.generation:
            raise StaleGeneration("this attempt's generation is not the task's live one")
        if (
            task.get("state") != TaskState.RUNNING.value
            or task.get("cancel_requested")
            or not _lease_is_live(task, lease, attempt, self._now())
        ):
            raise LeaseNotLive(
                "the task is not RUNNING under this attempt's live lease, or a cancel was requested")
        return task

    def _under_rate(self, attempt: AttemptTuple, now: datetime) -> None:
        from google.cloud.firestore_v1.base_query import FieldFilter

        rows = self._db.collection(LIVE_AUDIT).where(
            filter=FieldFilter("attempt_id", "==", attempt.attempt_id)).stream()
        since = now - RATE_WINDOW
        recent = [
            d for d in (snap.to_dict() or {} for snap in rows)
            if d.get("entry") == AUDIT_ISSUED and isinstance(d.get("at"), datetime)
            and d["at"] > since
        ]
        if len(recent) >= ISSUES_PER_HOUR:
            oldest = min(d["at"] for d in recent)
            raise RateLimited(
                f"at most {ISSUES_PER_HOUR} QA credentials an hour per attempt",
                retry_after_seconds=max(1.0, (oldest + RATE_WINDOW - now).total_seconds()),
            )

    def issue(self, authorization: str | None, attempt_id: str, raw: bytes, *, path: str,
              proof: str | None, timestamp: str | None) -> dict[str, Any]:
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValidationFailed(f"a QA identity request is at most {MAX_REQUEST_BYTES} bytes")
        body: QaIdentityRequest = parse_body(QaIdentityRequest, raw)
        if body.attempt_id != attempt_id:
            raise ValidationFailed("the body's attempt_id is not the path's")
        attempt = AttemptTuple(body.tenant_id, body.task_id, body.attempt_id, body.lease_id,
                               body.generation)
        self._prove(authorization, attempt, path=path, body=raw, proof=proof,
                    timestamp=timestamp)
        task = self._fence(attempt)
        if spec_refusal(self._settings, task, attempt.task_id):
            raise SpecUnverified("the task's signed spec does not verify")
        requested = (task.get("input") or {}).get(QA_IDENTITY_INPUT_KEY)
        if body.identity not in IDENTITIES or requested != body.identity:
            raise QaIdentityNotRequested("the task's signed input does not name this QA identity")
        if not configured(self._settings):
            raise QaIdentityUnavailable(UNCONFIGURED_MESSAGE)
        # Re-read at every signing, so a revoke takes effect on the next renewal.
        if not QaIdentityGrants(self._db, self._settings).is_granted(attempt.tenant_id):
            raise QaIdentityNotGranted(NOT_GRANTED_MESSAGE)
        now = self._now()
        self._under_rate(attempt, now)

        email = account(self._settings)
        host = console_host(self._settings)
        lifetime = lifetime_seconds(getattr(self._settings, "qa_identity_lifetime_seconds",
                                            DEFAULT_LIFETIME_SECONDS))
        jti = secrets.token_hex(16)
        issued_at = int(self._clock())
        claims = build_claims(account=email, host=host, issued_at=issued_at, lifetime=lifetime,
                              jti=jti)
        if self._signer is None:
            raise QaIdentitySignFailed("this deployment has no signer for the QA identity")
        try:
            credential = self._signer.sign_jwt(email, claims)
        except Exception as exc:  # noqa: BLE001 - IAM refused or did not answer
            log.warning("qa identity sign failed tenant=%s attempt=%s (%s)", attempt.tenant_id,
                        attempt.attempt_id, type(exc).__name__)
            raise QaIdentitySignFailed("IAM Credentials did not sign the QA credential") from None

        entry_id = new_id("la")
        self._db.collection(LIVE_AUDIT).document(entry_id).set({
            "id": entry_id,
            "entry": AUDIT_ISSUED,
            "task_id": attempt.task_id,
            "tenant_id": attempt.tenant_id,
            "attempt_id": attempt.attempt_id,
            "generation": attempt.generation,
            "email": None,
            "at": now,
            "detail": {"identity": body.identity, "jti": jti,
                       "expires_at": (now + timedelta(seconds=lifetime)).isoformat()},
        })
        log.info("qa identity issued tenant=%s task=%s attempt=%s generation=%d jti=%s "
                 "expires_in=%d", attempt.tenant_id, attempt.task_id, attempt.attempt_id,
                 attempt.generation, jti, lifetime)
        return {
            "identity": body.identity,
            "host": host,
            "header": CREDENTIAL_HEADER,
            "scheme": CREDENTIAL_SCHEME,
            "expires_in": lifetime,
            "jti": jti,
            "credential": credential,
        }


# --------------------------------------------------------------------------
# The member's read and the admin's list
# --------------------------------------------------------------------------


def mine(settings: Any, db: Any, tenant_id: str) -> dict[str, Any]:
    """`GET /v1/qa-identities/mine`: this tenant's grant, and nothing else --
    no account, host, reason or other tenant's grant."""
    return {"identities": [{
        "identity": IDENTITY,
        "configured": configured(settings),
        "granted": QaIdentityGrants(db, settings).is_granted(tenant_id),
    }]}


def admin_list(settings: Any, db: Any) -> dict[str, Any]:
    return {"identities": [{
        "identity": IDENTITY,
        "host": console_host(settings),
        "account": account(settings),
        "configured": configured(settings),
        "grants": [
            {k: g[k] for k in ("tenant_id", "state", "granted_by", "granted_at", "reason")}
            for g in QaIdentityGrants(db, settings).list()
        ],
    }]}


__all__ = [
    "GRANTS_COLLECTION",
    "IDENTITY",
    "QA_IDENTITY_INPUT_KEY",
    "QA_IDENTITY_PURPOSE",
    "IamJwtSigner",
    "QaIdentityGrants",
    "QaIdentityService",
    "admin_list",
    "console_host",
    "console_origin",
    "lifetime_seconds",
    "mine",
    "qa_identity_message",
    "refuse_unless_granted",
]
