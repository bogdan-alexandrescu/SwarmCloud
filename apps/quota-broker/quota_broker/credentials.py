"""The one writer of subscription credentials.

Reads a tenant's refresh token, exchanges it when the access token is close to
expiry, and republishes. Nothing else in the platform refreshes -- see the
module docstring in oauth.py for why concurrent refreshers corrupt a rotating
credential.

Two invariants, both about not locking a tenant out:

  1. The rotated REFRESH token is persisted BEFORE the access token is
     published. If the process dies between the two, the tenant has a valid
     refresh token and a stale access token -- recoverable on the next sweep. The
     other order loses the means of getting any further credentials.

  2. `invalid_grant` stops immediately. It means the refresh token is gone, and
     retrying cannot bring it back; spinning would only burn the endpoint's rate
     limit while a human is the only possible fix. The tenant's provider is
     marked so admission parks its work rather than dispatching attempts that
     are certain to fail authentication.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from .oauth import (
    Credential,
    CredentialError,
    ReauthRequired,
    TokenEndpoint,
    parse_credential,
    refresh,
    serialise,
)

#: Suffix for the long-lived half. The short-lived half keeps the original name
#: so the worker's mount is unchanged.
REFRESH_SUFFIX = "-refresh"


class SecretStore(Protocol):
    def access(self, name: str) -> str: ...
    def add_version(self, name: str, payload: str) -> None: ...


@dataclass(frozen=True)
class RefreshOutcome:
    tenant_id: str
    provider: str
    refreshed: bool
    reason: str
    expires_at: datetime | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "provider": self.provider,
            "refreshed": self.refreshed,
            "reason": self.reason,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }


class CredentialRefresher:
    def __init__(
        self,
        store: SecretStore,
        endpoint: TokenEndpoint,
        *,
        logger: Any,
        window: timedelta = timedelta(hours=3),
        now: Any = None,
    ) -> None:
        self._store = store
        self._endpoint = endpoint
        self._log = logger
        self._window = window
        self._now = now or (lambda: datetime.now(timezone.utc))

    def refresh_tenant(self, tenant_id: str, provider: str) -> RefreshOutcome:
        now = self._now()
        base = f"swarm-tenant-{tenant_id}-{provider}"
        refresh_secret = f"{base}{REFRESH_SUFFIX}"

        try:
            stored = self._store.access(refresh_secret)
        except Exception:
            # No refresh secret at all is the NORMAL case for a tenant using a
            # static API key or a setup-token. Not an error, and not something to
            # log noisily on every sweep.
            return RefreshOutcome(tenant_id, provider, False, "no_refresh_credential")

        try:
            credential = parse_credential(stored, now=now)
        except CredentialError as exc:
            self._log.warning(
                "stored subscription credential is unusable",
                tenant_id=tenant_id, provider=provider, error=str(exc),
            )
            return RefreshOutcome(tenant_id, provider, False, "unreadable")

        if not credential.needs_refresh(now, self._window):
            return RefreshOutcome(
                tenant_id, provider, False, "still_valid", credential.expires_at
            )

        try:
            fresh = refresh(credential, self._endpoint, now=now)
        except ReauthRequired as exc:
            # Terminal. Say so once, loudly, and stop.
            self._log.error(
                "subscription credential needs a human; tasks will park",
                tenant_id=tenant_id, provider=provider, error=str(exc),
            )
            return RefreshOutcome(tenant_id, provider, False, "reauth_required")
        except CredentialError as exc:
            self._log.warning(
                "credential refresh failed; will retry on the next sweep",
                tenant_id=tenant_id, provider=provider, error=str(exc),
            )
            return RefreshOutcome(tenant_id, provider, False, "refresh_failed")

        # ORDER IS THE SAFETY PROPERTY. Persist the means of getting more
        # credentials before the credentials themselves.
        if fresh.refresh_token != credential.refresh_token:
            self._store.add_version(refresh_secret, serialise(fresh))
            self._log.info(
                "refresh token rotated and persisted",
                tenant_id=tenant_id, provider=provider,
            )
        else:
            self._store.add_version(refresh_secret, serialise(fresh))

        # Only now the short-lived half the worker actually mounts.
        self._store.add_version(base, fresh.access_token)

        self._log.info(
            "subscription credential refreshed",
            tenant_id=tenant_id, provider=provider, **fresh.redacted(),
        )
        return RefreshOutcome(tenant_id, provider, True, "refreshed", fresh.expires_at)

    def sweep(self, tenants: list[tuple[str, str]]) -> list[RefreshOutcome]:
        """Refresh every (tenant, provider) pair that is due."""
        outcomes: list[RefreshOutcome] = []
        for tenant_id, provider in tenants:
            try:
                outcomes.append(self.refresh_tenant(tenant_id, provider))
            except Exception as exc:  # one tenant must not stop the sweep
                self._log.error(
                    "credential sweep raised for a tenant",
                    tenant_id=tenant_id, provider=provider, error=type(exc).__name__,
                )
                outcomes.append(RefreshOutcome(tenant_id, provider, False, "error"))
        return outcomes
