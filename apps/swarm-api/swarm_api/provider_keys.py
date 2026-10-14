"""Provider keys a person or a tenant admin sets: TypeSafe first (KEYS-API).

Owner request 2026-10-14: a way to set the TypeSafe API key (the Jev judgment
models' provider, docs.typesafe.ai) in the console and in the /sc plugin,
safely. Owner decisions, recorded in docs/provider-keys.md:

  * SCOPE = BOTH, the person's key wins. A person's own key lives in their
    personal tenant `u-<user>` (`swarm-tenant-u-<user>-typesafe`): secrets
    are bound to a tenant's worker account, and a group tenant's members
    share one, so a key only that person's work may use cannot live in the
    group's tenant. Else the tenant-wide key an ADMIN set
    (`swarm-tenant-<tenant>-typesafe`). Else none.
  * SETTING = NEVER THROUGH CLAUDE. The key is accepted on exactly two routes
    (`PUT .../user`, `PUT .../tenant`), which the console form and a terminal
    hidden prompt call. No MCP tool takes it; nothing here returns it.

What this module adds over `swarm_api.credentials`, which still does every
Secret Manager write (resource-level accessor grant, no read path):

  * STATUS without the value. `provider_keys/{secret_id}` records that a key
    is set, when, by whom and its version name -- never a payload, a length,
    a prefix or a suffix -- and every set and delete is an `admin_audit`
    entry in the same transaction as that record.
  * ONE-TIME LINKS. `provider_key_links/{sha256(token)}` binds a token to
    (person, scope, provider, tenant) for `KEY_LINK_TTL`, single use. The
    token is never stored, never logged; only its digest is a document id.
    It proves the write came through the /sc flow and grants NOTHING: every
    check a write without a link meets runs before the link is looked at.
  * RESOLUTION. `resolve_provider_key` says which secret a task would use.
    Nothing consumes it yet -- there is no Jev runner (owner: later, gated) --
    and no secret here is mounted into any runner.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from collections.abc import Collection, Mapping
from datetime import datetime, timedelta
from typing import Any

from google.cloud import firestore

from swarm_common.admission import _snapshot
from swarm_common.models import Tenant

from . import refusals
from .admins import AUDIT_COLLECTION
from .auth import AuthContext, PersonalTenants
from .codec import console_origin
from .errors import KeyLinkInvalid, NotFound, ValidationFailed
from .gittokens import secret_name_for
from .store import Store
from .validation import PROVIDER_KEY_PROVIDERS, TYPESAFE_PROVIDER
from .workspaces import is_service_identity, personal_tenant_id

log = logging.getLogger(__name__)

TYPESAFE = TYPESAFE_PROVIDER

#: One document per secret, keyed by its secret id: whether a key is set,
#: when, by whom, its version name. Never the key.
KEYS_COLLECTION = "provider_keys"
#: One document per minted link, keyed by sha256(token). Never the token.
LINKS_COLLECTION = "provider_key_links"

#: Ten minutes (owner brief): long enough to switch from the terminal to the
#: browser and paste, short enough that a link found in a scrollback or a
#: chat log later is dead.
KEY_LINK_TTL = timedelta(minutes=10)

USER = "user"
TENANT = "tenant"
SCOPES = (USER, TENANT)

AUDIT_KEY_SET = "provider_key_set"
AUDIT_KEY_DELETED = "provider_key_deleted"


def check_provider(provider: str) -> str:
    name = (provider or "").strip().lower()
    if name not in PROVIDER_KEY_PROVIDERS:
        raise ValidationFailed(
            f"unknown provider {provider!r}",
            detail={"provider_key_providers": sorted(PROVIDER_KEY_PROVIDERS)},
        )
    return name


def check_scope(scope: str) -> str:
    if scope not in SCOPES:
        raise ValidationFailed(f"unknown scope {scope!r}", detail={"scopes": list(SCOPES)})
    return scope


def resolve_provider_key(
    task_tenant_id: str, submitter_tenant_id: str | None, provider: str,
    present: Collection[str],
) -> str | None:
    """Which secret a task in `task_tenant_id` would use.

    `submitter_tenant_id` is the submitter's CONFIRMED personal tenant --
    `own_personal_tenant(...).tenant_id`, or None when they have none, or are
    a service identity. Confirmed, never derived here: the frozen slug keeps
    only the local part, so `alice@partner.com` derives `u-alice` as
    `alice@saga.xyz` does, and only the tenant document's principal says whose
    it is. A resolver that derived it from the email would hand one person's
    key to their namesake. `present` is the secret ids that hold a key now.

    The submitter's own key first, then the task tenant's, else None. Pure,
    so every combination is a unit test.
    """
    if provider not in PROVIDER_KEY_PROVIDERS:
        raise ValueError(f"{provider!r} is not a provider-key provider")
    if submitter_tenant_id:
        own = secret_name_for(submitter_tenant_id, provider)
        if own in present:
            return own
    tenant = secret_name_for(task_tenant_id, provider)
    return tenant if tenant in present else None


def own_personal_tenant(store: Store, db: Any, auth: AuthContext, *,
                        require_ready: bool = True) -> Tenant:
    """The caller's OWN ready personal tenant, or 404.

    Derived from the verified email, never from the request, and confirmed by
    `auth.PersonalTenants` -- the same check that offers it as the caller's
    default tenant (#1200): `tenants/u-<caller>` names THIS email, since the
    frozen slug drops the domain, and its workspace is `ready`, so its worker
    account exists to bind the secret to. Read uncached: a write must not act
    on a 30-second-old answer.

    `require_ready=False` is for DELETE alone: a person whose workspace left
    `ready` must still be able to destroy their own key, so it needs only the
    tenant document to be `kind: user` and to name this email.
    """
    email = (auth.email or "").strip().lower()
    tenant_id = personal_tenant_id(email) if email else ""
    ours = bool(email) and not is_service_identity(email)
    if ours and require_ready:
        ours = PersonalTenants(db, cache_seconds=0).ready(email)
    tenant = store.get_tenant(tenant_id) if ours else None
    if tenant is not None and (
            tenant.kind != "user" or (tenant.principal or "").strip().lower() != email):
        tenant = None
    if tenant is None:
        raise NotFound(
            "you have no ready personal workspace, which is where your own key is "
            "kept; request one in the console (People), or ask an admin to set a "
            "tenant key",
            detail={"tenant_id": tenant_id or None},
        )
    return tenant


def _status(data: Mapping[str, Any] | None, secret_id: str | None, *,
            available: bool = True) -> dict[str, Any]:
    data = data or {}
    is_set = bool(data.get("set")) and available
    return {
        "available": available,
        "set": is_set,
        "set_at": _iso(data.get("set_at")) if is_set else None,
        "set_by": data.get("set_by") if is_set else None,
        "version": data.get("version") if is_set else None,
        "secret_id": secret_id,
    }


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


class ProviderKeys:
    """`provider_keys/` and `provider_key_links/`: status, audit, links."""

    def __init__(self, db: Any, *, now: Any) -> None:
        self._db = db
        self._now = now

    # -- status --------------------------------------------------------------

    def _read(self, secret_id: str) -> dict[str, Any] | None:
        snap = self._db.collection(KEYS_COLLECTION).document(secret_id).get()
        return (snap.to_dict() or {}) if snap.exists else None

    def status(self, tenant_id: str, provider: str) -> dict[str, Any]:
        secret_id = secret_name_for(tenant_id, provider)
        return _status(self._read(secret_id), secret_id)

    def unavailable(self) -> dict[str, Any]:
        return _status(None, None, available=False)

    # -- set and delete, each with its audit entry -----------------------------

    def record(self, *, tenant_id: str, provider: str, scope: str, by: str,
               version: str | None, via_link: bool) -> dict[str, Any]:
        """Record a set (`version` given) or a delete (`version` None), with
        its `admin_audit` entry, in one transaction."""
        secret_id = secret_name_for(tenant_id, provider)
        ref = self._db.collection(KEYS_COLLECTION).document(secret_id)
        audit = self._db.collection(AUDIT_COLLECTION).document()
        now = self._now()
        is_set = version is not None
        doc = {
            "tenant_id": tenant_id, "provider": provider, "scope": scope,
            "set": is_set, "updated_at": now, "updated_by": by,
        }
        if is_set:
            doc.update(set_at=now, set_by=by, version=version)
        else:
            doc.update(set_at=None, set_by=None, version=None)
        transaction = self._db.transaction()

        @firestore.transactional
        def _write(txn: Any) -> None:
            txn.set(ref, doc, merge=True)
            txn.set(audit, {
                "action": AUDIT_KEY_SET if is_set else AUDIT_KEY_DELETED,
                "target_secret_id": secret_id,
                "by": by,
                "at": now,
                "detail": {"tenant_id": tenant_id, "provider": provider, "scope": scope,
                           "version": version, "via_link": via_link},
            })

        _write(transaction)
        return _status(doc, secret_id)

    # -- one-time links --------------------------------------------------------

    def mint_link(self, *, email: str, scope: str, provider: str, tenant_id: str,
                  console: str | None) -> dict[str, Any]:
        token = secrets.token_urlsafe(32)
        now = self._now()
        expires_at = now + KEY_LINK_TTL
        self._db.collection(LINKS_COLLECTION).document(_digest(token)).set({
            "email": email, "scope": scope, "provider": provider, "tenant_id": tenant_id,
            "created_at": now, "expires_at": expires_at, "used_at": None,
        })
        path = f"/settings/keys/{provider}?scope={scope}&t={token}"
        base = console_origin(console)
        log.info("minted a key-form link provider=%s scope=%s tenant=%s",
                 provider, scope, tenant_id)
        return {
            "url": f"{base}{path}" if base else None,
            "path": path,
            "scope": scope,
            "provider": provider,
            "expires_at": expires_at.isoformat(),
        }

    def consume_link(self, token: str, *, email: str, scope: str, provider: str,
                     tenant_id: str) -> bool:
        """Mark the link used if it is this caller's, for this scope, provider
        and tenant, unused and unexpired; False otherwise, and then nothing is
        written -- someone else presenting a leaked link does not burn it."""
        if not token or len(token) > 256:
            return False
        ref = self._db.collection(LINKS_COLLECTION).document(_digest(token))
        now = self._now()
        transaction = self._db.transaction()

        @firestore.transactional
        def _consume(txn: Any) -> bool:
            snap = _snapshot(txn.get(ref))
            link = (snap.to_dict() or {}) if snap.exists else None
            if not link or link.get("used_at") is not None:
                return False
            expires_at = link.get("expires_at")
            if not isinstance(expires_at, datetime) or expires_at <= now:
                return False
            if (link.get("email"), link.get("scope"), link.get("provider"),
                    link.get("tenant_id")) != (email, scope, provider, tenant_id):
                return False
            txn.update(ref, {"used_at": now})
            return True

        return _consume(transaction)

    def check_link(self, token: str | None, *, email: str, scope: str, provider: str,
                   tenant_id: str) -> bool:
        """True when a valid link was presented and consumed. A bad one is
        refused through the KEY_LINK_INVALID switch; off, the write goes on as
        one without a link. The message never carries the token."""
        if token is None:
            return False
        if self.consume_link(token, email=email, scope=scope, provider=provider,
                             tenant_id=tenant_id):
            return True
        refusals.refuse(KeyLinkInvalid(
            "this key-form link is unknown, already used, expired, or was minted for "
            "someone else or another scope; mint a new one with /sc or open the "
            "settings page directly"
        ))
        return False


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
