"""swarm-api provisions each browser-session secret and grants it per secret
(docs/design/live-browser.md §5.5.1, decision LB-7; lane LB-L8c).

Owner decision 2026-10-11, answering the question lane L8b asked: no worker
holds a project-level grant for a `-browser-` secret and no worker holds
`secretmanager.secrets.create`. Before the worker writes a session it asks
here for the SLOT:

    POST /v1/tasks/{task_id}/browser-session/slot   WORKER ONLY
        {tenant_id, attempt_id, lease_id, generation, secret_id}

and swarm-api

  1. authenticates the call as L9b's worker routes do (`BrowserLogins._prove`):
     the tenant's worker ID token, then the attempt key's signature over the
     raw body under `BROWSER_LOGIN_PURPOSE`;
  2. refuses any tenant that is not personal -- `kind = "user"`, an id
     starting `u-`, and a principal that derives to that id;
  3. refuses any `secret_id` outside that tenant's own
     `swarm-tenant-<tenant>-browser-wf-<this task's workflow>`,
     `swarm-tenant-<tenant>-browser-park-<this task>` (a park's browser, kept
     for the same task's resumed attempt; live-browser §5.5.2, issue #1362) or
     `swarm-tenant-<tenant>-browser-login-<name>`, BEFORE it calls Secret
     Manager. swarmBrowserSessionProvisioner's create permission is evaluated
     against the project and its IAM condition cannot narrow it
     (terraform/bootstrap/browser_logins.tf), so this check is the one that
     keeps a create inside the browser names;
  4. fences on the generation: the task must be STARTING or RUNNING under this
     attempt's live lease (`BrowserLogins._fenced_read`), so a stale worker is
     refused and nothing is created;
  5. for a login, requires the login record this workspace's person made:
     pending for this task (a save), or active and unexpired (a refresh). The
     expiry the secret is created with is that record's, never the caller's;
  6. creates the secret -- a carry or a park with the 24-hour TTL, a login with its
     record's expiry, labelled `managed-by=swarm-api`, `swarm-tenant`,
     `swarm-browser` -- and then sets THAT SECRET's IAM policy to exactly two
     bindings, `roles/secretmanager.secretAccessor` and
     `roles/secretmanager.secretVersionAdder`, each to
     `swarm-agent-worker-<tenant>` alone. Any other binding on the secret is
     dropped: "nobody else".

A SECRET THAT ALREADY EXISTS is re-granted, not re-created: a workflow's second
step, a refreshed login. Its `swarm-tenant` label must be the caller's tenant.
Without that check a tenant whose id is a prefix of another's (`u-a` and
`u-a-browser-login-x`) could name the other's secret inside its own prefix and
have its policy rewritten to itself; the label is set by swarm-api at create.

NO VALUE PASSES THROUGH HERE. The worker adds the version under its per-secret
grant; this route neither accepts nor returns a payload, and its body forbids
every field it does not name.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from google.cloud import firestore
from pydantic import Field

from swarm_common.admission import _snapshot
from swarm_common.identity import worker_service_account_id
from swarm_common.models import utcnow

from .browser_logins import (
    COLLECTION,
    NAME,
    BrowserLoginExpired,
    BrowserLoginPersonalOnlyHere,
    BrowserLoginUnknown,
    BrowserLoginVaultUnavailable,
    BrowserLogins,
    _doc_id,
    is_personal,
)
from .childkey import AttemptTuple
from .children import _AttemptFields, parse_body
from .errors import ApiError, ValidationFailed

log = logging.getLogger(__name__)

#: A personal tenant's id (`swarm_common.identity.tenant_id_for_user`).
PERSONAL_PREFIX = "u-"

#: The workflow carry's lifetime (§5.5.1): Secret Manager deletes it even if
#: swarm-api's terminal-state delete never ran. A park's secret has the same
#: lifetime, and it is its only delete: a hand-off park is dead-lettered after
#: `human_park_max_seconds` (24 hours by default), so a park secret outlives
#: no park that could still resume (§5.5.2).
CARRY_TTL_SECONDS = 24 * 3600

#: The two roles a slot's worker holds on its one secret, and nothing else.
ACCESSOR_ROLE = "roles/secretmanager.secretAccessor"
VERSION_ADDER_ROLE = "roles/secretmanager.secretVersionAdder"
SLOT_ROLES = (ACCESSOR_ROLE, VERSION_ADDER_ROLE)

#: Who the secret says made it. A label, so an operator listing secrets sees
#: swarm-api's and not a worker's.
MANAGED_BY = "swarm-api"

#: A workflow id as it may appear in a secret id: [A-Za-z0-9_-], as the
#: worker's `carry_secret_name` allows.
_WORKFLOW_PART = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

#: Every name this module will hand to Secret Manager, checked again at the
#: vault, after the route's own check: `swarm-tenant-u-<t>-browser-(wf|login)-<x>`.
BROWSER_SECRET_ID = re.compile(
    r"^swarm-tenant-u-[a-z0-9-]+-browser-(?:wf-[A-Za-z0-9_-]{1,128}|park-[A-Za-z0-9_-]{1,128}"
    r"|login-[a-z0-9-]{1,40})$"
)


class BrowserSlotOutsidePrefix(ApiError):
    """The secret id is not this tenant's own browser carry or login."""

    status_code = 403
    code = "browser_slot_outside_prefix"


class BrowserSlotTaken(ApiError):
    """A secret by that name exists and is labelled another tenant's."""

    status_code = 409
    code = "browser_slot_taken"


class SlotRequest(_AttemptFields):
    secret_id: str = Field(min_length=1, max_length=255)


@dataclass(frozen=True)
class SlotName:
    kind: str  # "carry", "park" or "login"
    suffix: str  # the workflow id, the task id, or the login's name


def personal_tenant_id(tenant_id: str) -> bool:
    """The id half of "personal": `u-` and something after it."""
    return isinstance(tenant_id, str) and tenant_id.startswith(PERSONAL_PREFIX) and len(tenant_id) > len(PERSONAL_PREFIX)


def slot_name(tenant_id: str, secret_id: str) -> SlotName | None:
    """`secret_id` read as this tenant's carry or login, or None when it is neither.

    Pure, so a refusal is decided before anything is read or created."""
    if not personal_tenant_id(tenant_id) or not isinstance(secret_id, str):
        return None
    prefix = f"swarm-tenant-{tenant_id}-browser-"
    if not secret_id.startswith(prefix) or not BROWSER_SECRET_ID.fullmatch(secret_id):
        return None
    rest = secret_id[len(prefix):]
    if rest.startswith("wf-") and _WORKFLOW_PART.fullmatch(rest[3:]):
        return SlotName("carry", rest[3:])
    if rest.startswith("park-") and _WORKFLOW_PART.fullmatch(rest[5:]):
        return SlotName("park", rest[5:])
    if rest.startswith("login-") and NAME.fullmatch(rest[6:]):
        return SlotName("login", rest[6:])
    return None


def worker_member(tenant_id: str, project_id: str) -> str:
    return f"serviceAccount:{worker_service_account_id(tenant_id)}@{project_id}.iam.gserviceaccount.com"


# --------------------------------------------------------------------------
# the vault: create, then the secret's own policy
# --------------------------------------------------------------------------


class SlotVault(Protocol):
    def provision(
        self,
        secret_id: str,
        *,
        tenant_id: str,
        member: str,
        labels: Mapping[str, str],
        ttl_seconds: int | None = None,
        expire_time: datetime | None = None,
    ) -> bool:
        """Create `secret_id` (or find it, labelled `tenant_id`'s) and bind
        exactly `member` to `SLOT_ROLES` on it. True when it was created."""


def refuse_unless_browser_secret(secret_id: str) -> None:
    if not isinstance(secret_id, str) or not BROWSER_SECRET_ID.fullmatch(secret_id):
        raise ValueError("refusing to provision a secret that is not a personal browser session")


class SecretManagerSlotVault:
    """Secret Manager, reached lazily so building the app builds no client."""

    def __init__(self, project_id: str, client: Any = None) -> None:
        self._project = project_id
        self._client = client

    def _secret_client(self) -> Any:
        if self._client is None:
            from google.cloud import secretmanager

            self._client = secretmanager.SecretManagerServiceClient()
        return self._client

    def provision(
        self,
        secret_id: str,
        *,
        tenant_id: str,
        member: str,
        labels: Mapping[str, str],
        ttl_seconds: int | None = None,
        expire_time: datetime | None = None,
    ) -> bool:
        from google.api_core import exceptions as gexc

        # Before any call: create is not narrowed by IAM (the module docstring).
        refuse_unless_browser_secret(secret_id)
        client = self._secret_client()
        name = f"projects/{self._project}/secrets/{secret_id}"
        secret: dict[str, Any] = {"replication": {"automatic": {}}, "labels": dict(labels)}
        if ttl_seconds is not None:
            secret["ttl"] = {"seconds": int(ttl_seconds)}
        elif expire_time is not None:
            secret["expire_time"] = expire_time
        created = False
        try:
            client.create_secret(
                request={"parent": f"projects/{self._project}", "secret_id": secret_id, "secret": secret}
            )
            created = True
        except gexc.AlreadyExists:
            existing = client.get_secret(request={"name": name})
            if dict(getattr(existing, "labels", {}) or {}).get("swarm-tenant") != tenant_id:
                raise BrowserSlotTaken(
                    "a secret by that name exists and is not this workspace's; nothing was granted"
                ) from None
        self._bind_only(client, name, member, gexc)
        return created

    @staticmethod
    def _bind_only(client: Any, name: str, member: str, gexc: Any) -> None:
        """The secret's policy becomes exactly `member` on `SLOT_ROLES`, under
        the policy's etag; an ABORTED (the etag moved) is re-read once."""
        for _ in range(2):
            policy = client.get_iam_policy(request={"resource": name})
            del policy.bindings[:]
            for role in SLOT_ROLES:
                binding = policy.bindings.add()
                binding.role = role
                binding.members.append(member)
            try:
                client.set_iam_policy(request={"resource": name, "policy": policy})
                return
            except gexc.Aborted:
                continue
        raise RuntimeError("the secret's IAM policy kept changing under the slot grant")


# --------------------------------------------------------------------------
# the service
# --------------------------------------------------------------------------


class BrowserSlots:
    """The slot route's logic. Authentication and fencing are L9b's
    (`BrowserLogins`), so there is one worker-proof path for browser sessions."""

    def __init__(
        self,
        *,
        settings: Any,
        db: Any,
        logins: BrowserLogins,
        vault: SlotVault,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self._settings = settings
        self._db = db
        self._logins = logins
        self._vault = vault
        self._now = now

    def slot(self, authorization: str | None, task_id: str, raw: bytes, *, path: str,
             proof: str | None, timestamp: str | None) -> dict[str, Any]:
        if len(raw) > 4096:
            raise ValidationFailed("a browser-session slot call is at most 4096 bytes")
        body: SlotRequest = parse_body(SlotRequest, raw)
        attempt = AttemptTuple(
            tenant_id=body.tenant_id, task_id=task_id, attempt_id=body.attempt_id,
            lease_id=body.lease_id, generation=body.generation,
        )
        self._logins._prove(authorization, attempt, path=path, body=raw, proof=proof,
                            timestamp=timestamp)
        tenant = self._logins._tenant(attempt.tenant_id)
        if (
            not personal_tenant_id(attempt.tenant_id)
            or tenant is None
            or tenant.get("kind") != "user"
            or not is_personal(tenant)
        ):
            raise BrowserLoginPersonalOnlyHere(
                "a browser session is stored only in a personal workspace: every worker of a "
                "group tenant shares one identity"
            )
        wanted = slot_name(attempt.tenant_id, body.secret_id)
        if wanted is None:
            raise BrowserSlotOutsidePrefix(
                "a slot is this workspace's own swarm-tenant-<tenant>-browser-wf-<workflow>, "
                "-browser-park-<task> or -browser-login-<name>, and nothing else"
            )
        now = self._now()
        login_ref = self._db.collection(COLLECTION).document(_doc_id(attempt.tenant_id, wanted.suffix))

        @firestore.transactional
        def _read(txn: Any) -> tuple[dict[str, Any], dict[str, Any] | None]:
            task = self._logins._fenced_read(txn, attempt, now)
            login = None
            if wanted.kind == "login":
                snap = _snapshot(txn.get(login_ref))
                login = (snap.to_dict() or None) if snap.exists else None
            return task, login

        task, login = _read(self._db.transaction())

        ttl_seconds: int | None = None
        expire_time: datetime | None = None
        if wanted.kind == "carry":
            if task.get("workflow_id") != wanted.suffix:
                raise BrowserSlotOutsidePrefix(
                    "a carry slot is this task's own workflow's, and this task is not a step of it"
                )
            ttl_seconds = CARRY_TTL_SECONDS
        elif wanted.kind == "park":
            if wanted.suffix != task_id:
                raise BrowserSlotOutsidePrefix("a park slot is this task's own, and no other task's")
            ttl_seconds = CARRY_TTL_SECONDS
        else:
            if login is None or login.get("tenant_id") != attempt.tenant_id:
                raise BrowserLoginUnknown(f"no browser login named {wanted.suffix!r} in this workspace")
            saving = login.get("pending_task_id") == task_id
            if not saving and login.get("state") != "active":
                raise BrowserLoginUnknown(
                    f"browser login {wanted.suffix!r} was never stored; save it again"
                )
            expires = login.get("expires_at")
            if not isinstance(expires, datetime) or expires <= now:
                raise BrowserLoginExpired(
                    f"browser login {wanted.suffix!r} expired; sign in and save it again"
                )
            expire_time = expires

        member = worker_member(attempt.tenant_id, self._settings.project_id)
        labels = {"managed-by": MANAGED_BY, "swarm-tenant": attempt.tenant_id,
                  "swarm-browser": wanted.kind}
        try:
            created = self._vault.provision(
                body.secret_id, tenant_id=attempt.tenant_id, member=member, labels=labels,
                ttl_seconds=ttl_seconds, expire_time=expire_time,
            )
        except ApiError:
            raise
        except Exception:
            log.warning("browser session slot could not reach Secret Manager", exc_info=False)
            raise BrowserLoginVaultUnavailable(
                "the session's secret could not be provisioned; nothing was stored, and asking "
                "again is safe"
            ) from None
        return {
            "secret_id": body.secret_id,
            "kind": wanted.kind,
            "created": created,
            "member": member,
            "roles": list(SLOT_ROLES),
        }
