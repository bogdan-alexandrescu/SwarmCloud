"""Named browser logins and the workflow carry, swarm-api's half
(docs/design/live-browser.md §5.5.1, decision LB-7).

Lane LB-L9b. Read §5.5.1 before changing anything here. In short:

  * A signed-in session is kept ONLY in a personal tenant (`u-<user>`). Every
    worker of a group tenant runs as one service account, so no IAM grant can
    let one member's agents read a session while refusing another's; a
    personal tenant's worker identity is that person's alone (lane L8b holds
    the grants). So a group tenant has no logins, a group tenant's task that
    names `input.browser_login` is refused at submission with
    `browser_login_personal_only` (`refuse_unless_personal`), and a group
    tenant's worker has no login to use.
  * THE VALUE NEVER PASSES THROUGH HERE. The jar moves gate to gate; the
    worker writes it to Secret Manager under its own identity
    (`swarm-tenant-u-<user>-browser-login-<name>`, lane L4c). swarm-api keeps
    only the metadata -- site, saved at, expires, last used -- in its own
    collection `browser_logins`, which belongs to swarm-api as `live_sessions`
    does, not to the frozen models. No body here accepts a value (every model
    forbids the fields it does not name) and no answer carries one.
  * SAVE is the person's, after Return control on a task in their own
    workspace: a name and an expiry (default 14 days, at most 30). It records
    a PENDING login; the worker reads it (`pending`), exports the jar on the
    gate's pipe before the wipe, stores it, and reports `stored`.
  * USE is the worker's: `input.browser_login` names a login, never cookies
    (invariant 10). The worker asks here before the agent runs; an unknown,
    pending, expired or revoked name is refused, and the worker fails the task.
  * REVOKE is the person's, and destroys every secret version before the
    record goes, so a failure leaves the record for a retry rather than a
    secret nobody lists.
  * THE CARRY. A workflow step's session is carried in
    `swarm-tenant-u-<user>-browser-wf-<workflow>` (24-hour TTL, written by the
    worker). `CarryReaper` deletes it once the workflow is terminal; the TTL
    is the backstop.
  * THE AUDIT records `browser_login_saved`, `_used`, `_refreshed` and
    `_revoked` in `live_audit` (§5.8), in the same transaction as the change,
    with the login's NAME and nothing else in `detail`.

WHO IS "THE WORKSPACE'S OWN PERSON". The caller's tenant is a personal tenant
(kind `user`, whose principal derives to its id) and the caller is that
principal. Anyone else -- a member of a group tenant, an admin, another
person -- is told there is nothing here (404), the same answer as a name that
does not exist, so a foreign login is not confirmed.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Literal, Mapping, Protocol

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from pydantic import Field

from swarm_common.admission import _snapshot
from swarm_common.identity import tenant_id_for_user
from swarm_common.models import new_id, utcnow
from swarm_common.profiles import RUNNER_PROFILES
from swarm_common.states import TERMINAL_STATES, TaskState

from .childkey import AttemptTuple, verify_proof
from .children import ChildService, _AttemptFields, _lease_is_live, _Strict, parse_body
from .errors import ApiError, NotFound, ValidationFailed
from .handoff import LIVE_AUDIT, expect_refusal
from .store import LEASES, TASKS, WORKFLOWS

log = logging.getLogger(__name__)

#: swarm-api's own collection (§5.5.1), keyed `<tenant_id>:<name>`.
COLLECTION = "browser_logins"
#: One marker per workflow whose carry was deleted, so a sweep asks once.
CARRY_REAPED = "browser_carry_reaped"
TENANTS = "tenants"

#: The signing purpose of the worker's routes (§5.4.1): its own, so neither a
#: hand-off nor a child-request signature verifies here, nor the reverse.
BROWSER_LOGIN_PURPOSE = "swarm-browser-login/v1"

#: The task input that names a login (§5.5.1). A name, never cookies.
BROWSER_LOGIN_INPUT_KEY = "browser_login"

#: §5.5.1: "an expiry (default 14 days, at most 30)".
DEFAULT_EXPIRY_DAYS = 14
MAX_EXPIRY_DAYS = 30

#: A login's name: lower-case letters, digits and inner hyphens, at most 40,
#: so `swarm-tenant-u-<user>-browser-login-<name>` stays a valid secret id and
#: a name can never spell a path or another secret's suffix.
NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$")

#: How far back the carry sweep looks, by workflow creation. A carry is
#: written with a 24-hour TTL, so anything older than the window has already
#: expired unless its workflow ran for most of a week; the TTL is the backstop
#: for that case, as §5.5.1 says it is.
CARRY_LOOKBACK = timedelta(days=7)

#: The audit entries this module writes (§5.8). Names only in `detail`.
AUDIT_ENTRIES = (
    "browser_login_saved",
    "browser_login_used",
    "browser_login_refreshed",
    "browser_login_revoked",
)

#: The refusal code of a login asked for outside a personal tenant.
BROWSER_LOGIN_PERSONAL_ONLY = "browser_login_personal_only"

_TERMINAL = sorted(state.value for state in TERMINAL_STATES)


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------


class BrowserLoginPersonalOnly(ValidationFailed):
    """`input.browser_login` in a group tenant: refused at submission (§5.5.1).

    A ValidationFailed, so the submission paths count it as every other
    refusal; 422 there, as an input the tenant cannot use."""

    code = BROWSER_LOGIN_PERSONAL_ONLY


class BrowserLoginPersonalOnlyHere(ApiError):
    """The same rule on a route that is not a submission: a save on a group
    tenant's task, or a group tenant's worker asking for a login."""

    status_code = 403
    code = BROWSER_LOGIN_PERSONAL_ONLY


class BrowserLoginNotLive(ApiError):
    status_code = 409
    code = "browser_login_not_live"


class BrowserLoginUnknown(ApiError):
    status_code = 404
    code = "browser_login_unknown"


class BrowserLoginExpired(ApiError):
    status_code = 410
    code = "browser_login_expired"


class BrowserLoginVaultUnavailable(ApiError):
    status_code = 503
    code = "browser_login_vault_unavailable"


class BrowserLoginUnavailable(ApiError):
    status_code = 503
    code = "browser_login_unavailable"


class BrowserLoginUnauthenticated(ApiError):
    status_code = 403
    code = "browser_login_unauthenticated"


class BrowserLoginUnproven(ApiError):
    status_code = 403
    code = "browser_login_unproven"


class BrowserLoginFenced(ApiError):
    status_code = 409
    code = "browser_login_fenced"


# --------------------------------------------------------------------------
# names
# --------------------------------------------------------------------------


def login_secret_id(tenant_id: str, name: str) -> str:
    """The named login's secret (§5.5.1), in `Tenant.secret_name`'s shape."""
    return f"swarm-tenant-{tenant_id}-browser-login-{name}"


def carry_secret_id(tenant_id: str, workflow_id: str) -> str:
    """The workflow carry's secret (§5.5.1)."""
    return f"swarm-tenant-{tenant_id}-browser-wf-{workflow_id}"


def _doc_id(tenant_id: str, name: str) -> str:
    return f"{tenant_id}:{name}"


def browser_login_message(method: str, path: str, body: bytes, timestamp: str) -> bytes:
    """What the worker signs for one browser-login call: the hand-off's shape
    under its own purpose."""
    digest = hashlib.sha256(body).hexdigest()
    return f"{BROWSER_LOGIN_PURPOSE}\n{method.upper()}\n{path}\n{digest}\n{timestamp}".encode("utf-8")


def name_refusal(name: Any) -> str:
    """Why `name` is not a login name, or "" when it is."""
    if not isinstance(name, str) or not NAME.fullmatch(name):
        return ("a browser login's name is 1-40 lower-case letters, digits and inner "
                "hyphens, for example github-qa")
    return ""


def _get(tenant: Any, key: str) -> Any:
    if isinstance(tenant, Mapping):
        return tenant.get(key)
    return getattr(tenant, key, None)


def is_personal(tenant: Any) -> bool:
    """A personal tenant: kind `user`, whose principal derives to its id.

    Both, because the id alone is not proof: the frozen slug can make a
    group's id start `u-` (`deps.tenant_scope`)."""
    if tenant is None or _get(tenant, "kind") != "user":
        return False
    principal = str(_get(tenant, "principal") or "").strip().lower()
    return bool(principal) and tenant_id_for_user(principal) == _get(tenant, "tenant_id")


def refuse_unless_personal(tenant: Any, payload: Mapping[str, Any], *,
                           step_id: str | None = None) -> None:
    """§5.5.1: a group tenant's task that names `input.browser_login` is
    refused at submission. Called for a task, each task of a batch and each
    step of a workflow, BEFORE the catalogue's input check, so a group tenant
    is told the rule rather than that the key is undeclared."""
    if BROWSER_LOGIN_INPUT_KEY not in payload or is_personal(tenant):
        return
    message = ("input 'browser_login' is offered only in a personal workspace: every "
               "worker of a group tenant shares one identity, so no session is stored "
               "or carried there and every browser step signs in fresh")
    detail: dict[str, Any] = {"key": BROWSER_LOGIN_INPUT_KEY}
    if step_id is not None:
        detail["step_id"] = step_id
        message = f"step {step_id!r}: {message}"
    raise BrowserLoginPersonalOnly(message, detail=detail)


# --------------------------------------------------------------------------
# the vault: destroying a secret, which is all swarm-api does to one
# --------------------------------------------------------------------------


class BrowserLoginVault(Protocol):
    def destroy(self, secret_id: str) -> int:
        """Destroy every version of `secret_id`, then the secret. The count of
        versions destroyed; 0, not an error, when there is no such secret."""


class SecretManagerVault:
    """Secret Manager, reached lazily so building the app builds no client.

    Every version is destroyed by name before the secret is deleted: deleting
    a secret destroys its versions too, but the explicit pass is what makes
    "revoke destroys every version" true even if the delete is refused.
    Nothing here reads a version's payload."""

    def __init__(self, project_id: str, client: Any = None) -> None:
        self._project = project_id
        self._client = client

    def _secret_client(self) -> Any:
        if self._client is None:
            from google.cloud import secretmanager

            self._client = secretmanager.SecretManagerServiceClient()
        return self._client

    def destroy(self, secret_id: str) -> int:
        from google.api_core import exceptions as gexc

        client = self._secret_client()
        name = f"projects/{self._project}/secrets/{secret_id}"
        destroyed = 0
        try:
            for version in client.list_secret_versions(request={"parent": name}):
                if getattr(version.state, "name", str(version.state)) == "DESTROYED":
                    continue
                try:
                    client.destroy_secret_version(request={"name": version.name})
                except gexc.FailedPrecondition:
                    continue  # destroyed between the list and the call
                destroyed += 1
            client.delete_secret(request={"name": name})
        except gexc.NotFound:
            pass
        return destroyed


# --------------------------------------------------------------------------
# request bodies: each forbids every field it does not name
# --------------------------------------------------------------------------


class SaveLogin(_Strict):
    name: str = Field(max_length=64)
    expires_in_days: int = Field(default=DEFAULT_EXPIRY_DAYS, ge=1, le=MAX_EXPIRY_DAYS)


class WorkerPending(_AttemptFields):
    pass


class WorkerReport(_AttemptFields):
    event: Literal["stored", "used", "refreshed"]
    name: str = Field(max_length=64)
    site: str | None = Field(default=None, max_length=253)


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def login_to_api(doc: Mapping[str, Any]) -> dict[str, Any]:
    """What a person is shown of a login. A closed set: never a value, never
    the secret's id."""
    return {
        "name": doc.get("name"),
        "site": doc.get("site"),
        "state": doc.get("state"),
        "saved_at": _iso(doc.get("saved_at")),
        "expires_at": _iso(doc.get("expires_at")),
        "last_used_at": _iso(doc.get("last_used_at")),
        "refreshed_at": _iso(doc.get("refreshed_at")),
    }


def _audit(txn: Any, db: Any, *, entry: str, tenant_id: str, name: str, now: datetime,
           email: str | None, task_id: str | None = None,
           attempt: AttemptTuple | None = None) -> None:
    """One `live_audit` entry (§5.8). `detail` is the login's name, only."""
    entry_id = new_id("la")
    txn.set(
        db.collection(LIVE_AUDIT).document(entry_id),
        {
            "id": entry_id,
            "entry": entry,
            "task_id": task_id,
            "tenant_id": tenant_id,
            "attempt_id": attempt.attempt_id if attempt else None,
            "generation": attempt.generation if attempt else None,
            "email": email,
            "at": now,
            "detail": {"name": name},
        },
    )


# --------------------------------------------------------------------------
# the service
# --------------------------------------------------------------------------


class BrowserLogins:
    """The browser-login routes' logic. Built per request from the app context."""

    def __init__(
        self,
        *,
        settings: Any,
        db: Any,
        store: Any,
        children: ChildService,
        vault: BrowserLoginVault,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self._settings = settings
        self._db = db
        self._store = store
        self._children = children
        self._vault = vault
        self._now = now

    # -- whose workspace ---------------------------------------------------

    def _tenant(self, tenant_id: str) -> dict[str, Any] | None:
        snap = self._db.collection(TENANTS).document(tenant_id).get()
        return (snap.to_dict() or None) if snap.exists else None

    def _own_workspace(self, tenant_id: str, email: str, member_scope: str = "") -> None:
        """The caller is the person whose personal tenant this is, or 404."""
        tenant = self._tenant(tenant_id)
        caller = (email or "").strip().lower()
        if (
            member_scope
            or not is_personal(tenant)
            or str(tenant.get("principal") or "").strip().lower() != caller
        ):
            raise NotFound("there are no browser logins here")

    def _ref(self, tenant_id: str, name: str) -> Any:
        return self._db.collection(COLLECTION).document(_doc_id(tenant_id, name))

    # -- the person's routes -----------------------------------------------

    def list(self, tenant_id: str, *, email: str, member_scope: str = "") -> list[dict[str, Any]]:
        self._own_workspace(tenant_id, email, member_scope)
        query = self._db.collection(COLLECTION).where(
            filter=FieldFilter("tenant_id", "==", tenant_id)
        )
        docs = [snap.to_dict() or {} for snap in query.stream()]
        # The tenant filter again, on the documents: the id is the key's half.
        rows = [d for d in docs if d.get("tenant_id") == tenant_id]
        return [login_to_api(d) for d in sorted(rows, key=lambda d: str(d.get("name")))]

    def save(self, tenant_id: str, task_id: str, body: SaveLogin, *, email: str,
             member_scope: str = "", submitted_by: str | None = None) -> dict[str, Any]:
        """§5.5.1 "Save as login": a PENDING login the worker stores."""
        # The task first, through the tenant filter: another workspace's task
        # is a 404, as it is on every task route (§5.2 step 2).
        task = self._store.get_task(tenant_id, task_id, submitted_by=submitted_by)
        tenant = self._tenant(tenant_id)
        if not is_personal(tenant):
            raise BrowserLoginPersonalOnlyHere(
                "Save as login is offered only on a task in your own personal workspace; "
                "a group tenant never stores a session"
            )
        self._own_workspace(tenant_id, email, member_scope)
        why = name_refusal(body.name)
        if why:
            raise ValidationFailed(why, detail={"field": "name"})
        profile = RUNNER_PROFILES.get(task.runner_profile)
        if profile is None or not profile.live_browser:
            raise BrowserLoginNotLive(
                "this task has no live browser, so it has no session to save"
            )
        if task.state is not TaskState.RUNNING:
            raise BrowserLoginNotLive(
                "a session is saved from a running task, before its browser is wiped; "
                f"this task is {task.state.value}"
            )
        now = self._now()
        ref = self._ref(tenant_id, body.name)
        caller = email.strip().lower()

        @firestore.transactional
        def _apply(txn: Any) -> dict[str, Any]:
            snap = _snapshot(txn.get(ref))
            existing = (snap.to_dict() or {}) if snap.exists else {}
            doc = {
                **existing,
                "tenant_id": tenant_id,
                "name": body.name,
                # A saved login keeps its site and history while the new
                # version is written; a new one starts pending.
                "state": existing.get("state") if existing.get("state") == "active" else "pending",
                "site": existing.get("site"),
                "saved_at": now,
                "saved_by": caller,
                "expires_at": now + timedelta(days=body.expires_in_days),
                "last_used_at": existing.get("last_used_at"),
                "refreshed_at": existing.get("refreshed_at"),
                "pending_task_id": task_id,
                "secret_id": login_secret_id(tenant_id, body.name),
                "versions": int(existing.get("versions") or 0),
            }
            txn.set(ref, doc)
            _audit(txn, self._db, entry="browser_login_saved", tenant_id=tenant_id,
                   name=body.name, now=now, email=caller, task_id=task_id)
            return doc

        doc = _apply(self._db.transaction())
        return {**login_to_api(doc), "state": "pending"}

    def revoke(self, tenant_id: str, name: str, *, email: str,
               member_scope: str = "") -> dict[str, Any]:
        """§5.5.1: Revoke destroys every secret version, then the record."""
        self._own_workspace(tenant_id, email, member_scope)
        if name_refusal(name):
            raise NotFound(f"no browser login named {name!r}")
        ref = self._ref(tenant_id, name)
        snap = ref.get()
        if not snap.exists or (snap.to_dict() or {}).get("tenant_id") != tenant_id:
            raise NotFound(f"no browser login named {name!r}")
        try:
            destroyed = self._vault.destroy(login_secret_id(tenant_id, name))
        except Exception:
            log.warning("browser login revoke could not reach Secret Manager", exc_info=False)
            raise BrowserLoginVaultUnavailable(
                "the login's secret could not be destroyed, so it was not revoked; "
                "nothing was changed, and revoking again is safe"
            ) from None
        now = self._now()
        caller = email.strip().lower()

        @firestore.transactional
        def _apply(txn: Any) -> None:
            _snapshot(txn.get(ref))
            txn.delete(ref)
            _audit(txn, self._db, entry="browser_login_revoked", tenant_id=tenant_id,
                   name=name, now=now, email=caller)

        _apply(self._db.transaction())
        return {"name": name, "revoked": True, "versions_destroyed": destroyed}

    # -- the worker's routes -----------------------------------------------

    def _prove(self, authorization: str | None, attempt: AttemptTuple, *, path: str,
               body: bytes, proof: str | None, timestamp: str | None) -> None:
        """§5.4.1, as the hand-off routes: the tenant's worker identity, then
        the attempt key's signature over the raw body. Every failure of the
        signature is the same 403."""
        if not self._children._keys.configured:
            raise BrowserLoginUnavailable(
                "this deployment has no swarm-child-key, so it offers no browser-login path"
            )
        try:
            self._children.authenticate_worker(authorization, attempt.tenant_id)
        except ApiError as exc:
            if exc.status_code == 429:
                raise
            raise BrowserLoginUnauthenticated(
                "only this tenant's worker service account may call this route"
            ) from None
        unproven = BrowserLoginUnproven("the attempt proof did not verify for this attempt")
        if not proof or not timestamp or not re.fullmatch(r"\d{1,12}", timestamp):
            raise unproven
        skew = int(getattr(self._settings, "child_proof_skew_seconds", 120))
        if abs(self._children._clock() - int(timestamp)) > skew:
            raise unproven
        registration = self._children._registration(attempt)
        if registration is None or not verify_proof(
            registration["public_key"], proof,
            browser_login_message("POST", path, body, timestamp),
        ):
            raise unproven

    def _fenced_read(self, txn: Any, attempt: AttemptTuple, now: datetime) -> dict[str, Any]:
        task_ref = self._db.collection(TASKS).document(attempt.task_id)
        lease_ref = self._db.collection(LEASES).document(attempt.lease_id)
        task_snap = _snapshot(txn.get(task_ref))
        lease_snap = _snapshot(txn.get(lease_ref))
        task = task_snap.to_dict() if task_snap.exists else None
        lease = lease_snap.to_dict() if lease_snap.exists else None
        if (
            task is None
            or task.get("state") not in (TaskState.STARTING.value, TaskState.RUNNING.value)
            or not _lease_is_live(task, lease, attempt, now)
        ):
            raise BrowserLoginFenced(
                "the task is not running under this attempt's live lease; nothing was recorded"
            )
        return task

    def _worker(self, authorization: str | None, task_id: str, raw: bytes, model: Any, *,
                path: str, proof: str | None, timestamp: str | None) -> tuple[Any, AttemptTuple]:
        if len(raw) > 4096:
            raise ValidationFailed("a browser-login call is at most 4096 bytes")
        body = parse_body(model, raw)
        attempt = AttemptTuple(
            tenant_id=body.tenant_id, task_id=task_id, attempt_id=body.attempt_id,
            lease_id=body.lease_id, generation=body.generation,
        )
        self._prove(authorization, attempt, path=path, body=raw, proof=proof, timestamp=timestamp)
        if not is_personal(self._tenant(attempt.tenant_id)):
            raise BrowserLoginPersonalOnlyHere(
                "a group tenant's worker has no browser login to store or use"
            )
        return body, attempt

    def pending(self, authorization: str | None, task_id: str, raw: bytes, *, path: str,
                proof: str | None, timestamp: str | None) -> list[dict[str, Any]]:
        """What this task's person asked to save, for the worker to store
        before the wipe. Names, secret ids and expiries: nothing else exists."""
        _, attempt = self._worker(authorization, task_id, raw, WorkerPending, path=path,
                                  proof=proof, timestamp=timestamp)
        now = self._now()

        @firestore.transactional
        def _apply(txn: Any) -> None:
            self._fenced_read(txn, attempt, now)

        _apply(self._db.transaction())
        query = (
            self._db.collection(COLLECTION)
            .where(filter=FieldFilter("tenant_id", "==", attempt.tenant_id))
            .where(filter=FieldFilter("pending_task_id", "==", task_id))
        )
        items = []
        for snap in query.stream():
            doc = snap.to_dict() or {}
            if doc.get("tenant_id") != attempt.tenant_id or doc.get("pending_task_id") != task_id:
                continue
            items.append({
                "name": doc.get("name"),
                "secret_id": login_secret_id(attempt.tenant_id, str(doc.get("name"))),
                "expire_time": _iso(doc.get("expires_at")),
            })
        return sorted(items, key=lambda i: str(i["name"]))

    def report(self, authorization: str | None, task_id: str, raw: bytes, *, path: str,
               proof: str | None, timestamp: str | None) -> dict[str, Any]:
        """The worker stored a pending login, used one, or refreshed one."""
        body, attempt = self._worker(authorization, task_id, raw, WorkerReport, path=path,
                                     proof=proof, timestamp=timestamp)
        why = name_refusal(body.name)
        if why:
            raise ValidationFailed(why, detail={"field": "name"})
        site = body.site.strip().lower() if body.site else None
        if site:
            refused = expect_refusal(site)
            if refused:
                raise ValidationFailed(f"site is not a host a login may name: {refused}")
        ref = self._ref(attempt.tenant_id, body.name)
        now = self._now()

        @firestore.transactional
        def _apply(txn: Any) -> dict[str, Any]:
            self._fenced_read(txn, attempt, now)
            snap = _snapshot(txn.get(ref))
            doc = (snap.to_dict() or {}) if snap.exists else None
            if doc is None or doc.get("tenant_id") != attempt.tenant_id:
                raise BrowserLoginUnknown(f"no browser login named {body.name!r} in this workspace")
            expires = doc.get("expires_at")
            if body.event == "stored":
                if doc.get("pending_task_id") != task_id:
                    raise BrowserLoginUnknown(
                        f"no save of {body.name!r} is pending for this task"
                    )
                update = {
                    "state": "active",
                    "pending_task_id": None,
                    "site": site or doc.get("site"),
                    "versions": int(doc.get("versions") or 0) + 1,
                }
                txn.update(ref, update)
                return {**doc, **update}
            if doc.get("state") != "active":
                raise BrowserLoginUnknown(
                    f"browser login {body.name!r} was never stored; save it again"
                )
            if isinstance(expires, datetime) and expires <= now:
                raise BrowserLoginExpired(
                    f"browser login {body.name!r} expired; sign in and save it again"
                )
            if body.event == "used":
                update = {"last_used_at": now}
                entry = "browser_login_used"
            else:
                update = {
                    "refreshed_at": now,
                    "site": site or doc.get("site"),
                    "versions": int(doc.get("versions") or 0) + 1,
                }
                entry = "browser_login_refreshed"
            txn.update(ref, update)
            _audit(txn, self._db, entry=entry, tenant_id=attempt.tenant_id, name=body.name,
                   now=now, email=None, task_id=task_id, attempt=attempt)
            return {**doc, **update}

        doc = _apply(self._db.transaction())
        return {
            "name": body.name,
            "event": body.event,
            "secret_id": login_secret_id(attempt.tenant_id, body.name),
            "expire_time": _iso(doc.get("expires_at")),
        }


# --------------------------------------------------------------------------
# the carry: deleted when its workflow is terminal
# --------------------------------------------------------------------------


@dataclass
class CarryReport:
    tenants: int = 0
    workflows: int = 0
    reaped: int = 0
    versions_destroyed: int = 0
    failed: list[str] = field(default_factory=list)

    def to_api(self) -> dict[str, Any]:
        return {
            "tenants": self.tenants,
            "workflows": self.workflows,
            "reaped": self.reaped,
            "versions_destroyed": self.versions_destroyed,
            "failed": list(self.failed),
        }


class CarryReaper:
    """§5.5.1: "swarm-api deletes the secret when the workflow reaches a
    terminal state. The TTL is the backstop."

    Two callers. `on_task_finished`, on the `task_finished` push that already
    arrives as every step ends, is the one that makes it "at" the terminal
    state. `sweep` (POST /v1/admin/browser-logins/carry-sweep, an admin's) is
    the backfill for a push that was lost, over the stored state.

    Only personal tenants are read: a group tenant never carries, so its
    workflows are never named to Secret Manager at all. A workflow is reaped
    once; its marker in `browser_carry_reaped` is what a later sweep skips. A
    destroy that fails leaves no marker, so the next sweep tries again."""

    def __init__(self, *, db: Any, vault: BrowserLoginVault,
                 now: Callable[[], datetime] = utcnow) -> None:
        self._db = db
        self._vault = vault
        self._now = now

    def _reap(self, tenant_id: str, workflow_id: str, state: Any) -> int | None:
        """Destroy one workflow's carry and mark it. None when it was already
        reaped; raises when Secret Manager did not answer (no marker)."""
        marker = self._db.collection(CARRY_REAPED).document(workflow_id)
        if marker.get().exists:
            return None
        destroyed = self._vault.destroy(carry_secret_id(tenant_id, workflow_id))
        marker.set({
            "workflow_id": workflow_id,
            "tenant_id": tenant_id,
            "state": state,
            "reaped_at": self._now(),
            "versions_destroyed": destroyed,
        })
        return destroyed

    def on_task_finished(self, task_id: str, *, store: Any, rollups: Any) -> bool:
        """The moment a workflow ends: called on the `task_finished` push
        (routes/admin.py), which arrives as each step's task finishes. True
        when this call deleted the carry.

        The workflow's state is DERIVED from its steps (`rollup.effective_state`),
        not read from the stored cache, which can lag them; the derivation is
        written back as every read does. Never raises: a push is a doorbell,
        and the sweep and the TTL are behind it."""
        try:
            snap = self._db.collection(TASKS).document(task_id).get()
            task = (snap.to_dict() or {}) if snap.exists else {}
            tenant_id, workflow_id = task.get("tenant_id"), task.get("workflow_id")
            if not tenant_id or not workflow_id or task.get("state") not in _TERMINAL:
                return False
            if not is_personal(self._tenant(tenant_id)):
                return False
            from .rollup import effective_state

            workflow = store.get_workflow(tenant_id, workflow_id, submitted_by=None)
            results, _ = rollups.for_workflows(tenant_id, [workflow])
            state = effective_state(results[0].rollup) if results else None
            if state not in _TERMINAL:
                return False
            return bool(self._reap(tenant_id, workflow_id, state) is not None)
        except Exception:
            log.warning("browser carry for task %s was not reaped on its finish; the sweep "
                        "and the TTL remain", task_id, exc_info=False)
            return False

    def _tenant(self, tenant_id: str) -> dict[str, Any] | None:
        snap = self._db.collection(TENANTS).document(tenant_id).get()
        return (snap.to_dict() or None) if snap.exists else None

    def sweep(self, *, limit: int = 500) -> CarryReport:
        report = CarryReport()
        cutoff = self._now() - CARRY_LOOKBACK
        tenants = self._db.collection(TENANTS).where(filter=FieldFilter("kind", "==", "user"))
        for tenant_snap in tenants.stream():
            tenant = tenant_snap.to_dict() or {}
            if not is_personal(tenant):
                continue
            report.tenants += 1
            tenant_id = tenant["tenant_id"]
            query = (
                self._db.collection(WORKFLOWS)
                .where(filter=FieldFilter("tenant_id", "==", tenant_id))
                .where(filter=FieldFilter("state", "in", _TERMINAL))
                .where(filter=FieldFilter("created_at", ">=", cutoff))
            )
            for wf_snap in query.stream():
                if report.reaped + len(report.failed) >= limit:
                    return report
                workflow = wf_snap.to_dict() or {}
                workflow_id = workflow.get("workflow_id") or wf_snap.id
                if workflow.get("tenant_id") != tenant_id or workflow.get("state") not in _TERMINAL:
                    continue
                report.workflows += 1
                try:
                    destroyed = self._reap(tenant_id, workflow_id, workflow.get("state"))
                except Exception:
                    log.warning("browser carry for workflow %s could not be destroyed",
                                workflow_id, exc_info=False)
                    report.failed.append(workflow_id)
                    continue
                if destroyed is None:
                    continue
                report.reaped += 1
                report.versions_destroyed += destroyed
        return report
