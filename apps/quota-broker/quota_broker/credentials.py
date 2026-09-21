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

And one about not writing for the sake of writing:

  3. An UNCHANGED credential adds no secret version. A sweep runs every five
     minutes and a token lives eight hours, so anything this module writes per
     tick it writes ~1,700 times per token. See `_BaseState` for the bool that
     could not tell "the secret differs" from "I was not allowed to look", and
     what that cost.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
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
    def access(self, name: str) -> str:
        """Return the latest version's payload.

        Raises `KeyError` -- and only `KeyError` -- when the secret does not
        exist or holds no enabled version. Every other failure must raise
        something else, because the two mean opposite things here: a missing
        refresh secret is an ordinary tenant using a static API key, while a
        Secret Manager outage is an incident. If both arrived as the same
        exception, an outage would read as "no subscription tenants" and the
        sweep would report success while refreshing nothing.
        """

    def add_version(self, name: str, payload: str) -> None: ...


class _BaseState(Enum):
    """What is KNOWN about the worker-facing secret's current contents.

    Three values rather than two, and that is the whole point of this type.
    The check used to be a bool, and a bool cannot say "I could not look" --
    so a read that FAILED returned the same False as a read that succeeded and
    found a different token, and the caller published either way.

    That is indistinguishable-guard-failure, and it cost 1,665 identical
    secret versions per credential in saga-agents-staging before anyone
    noticed. The broker holds `secretmanager.secretVersionAdder` on the
    worker-facing secret but NOT `secretAccessor`, so `access()` raises
    PermissionDenied every single time; the bool turned that permanent,
    structural fact into "the secret is stale", and the sweep republished an
    identical value every five minutes from 2026-09-18T07:26 onward without
    one line of log to say so.
    """

    #: The read succeeded and the secret already holds this token. Nothing to do.
    CURRENT = "current"
    #: A DEFINITE answer that a write is needed: the read succeeded and the
    #: payload differs, or there is no enabled version at all.
    NEEDS_PUBLISH = "needs_publish"
    #: The read itself failed. NOT evidence about the contents, in either
    #: direction.
    UNREADABLE = "unreadable"


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
        #: secret name -> SHA-256 of the access token THIS refresher last wrote
        #: into it, and only ever recorded after the write returned.
        #:
        #: It exists for one case: the read-back is refused, so the only
        #: evidence about what the worker-facing secret holds is what this
        #: process put there. A digest rather than the token because this dict
        #: outlives the request that filled it, and a long-lived map of live
        #: access tokens is a thing to leak; the comparison only needs equality.
        #:
        #: Bounded by the number of credentials in the deployment (tens), so it
        #: is not evicted. An account that is removed leaves one dead 64-byte
        #: entry behind until the next deploy.
        self._published: dict[str, str] = {}

    def refresh_tenant(self, tenant_id: str, provider: str) -> RefreshOutcome:
        return self._refresh(
            f"swarm-tenant-{tenant_id}-{provider}",
            tenant_id=tenant_id,
            provider=provider,
        )

    def _refresh(self, base: str, *, tenant_id: str, provider: str) -> RefreshOutcome:
        now = self._now()
        refresh_secret = f"{base}{REFRESH_SUFFIX}"

        try:
            stored = self._store.access(refresh_secret)
        except KeyError:
            # No refresh secret at all is the NORMAL case for a tenant using a
            # static API key or a setup-token. Not an error, and not something to
            # log noisily on every sweep.
            return RefreshOutcome(tenant_id, provider, False, "no_refresh_credential")
        except Exception as exc:
            # Anything else is the store failing, not the tenant lacking a
            # credential. Reported separately so a broken sweep is visible.
            self._log.warning(
                "could not read the refresh credential",
                extra={
                    "tenant_id": tenant_id,
                    "provider": provider,
                    "error": type(exc).__name__,
                },
            )
            return RefreshOutcome(tenant_id, provider, False, "store_unavailable")

        try:
            credential = parse_credential(stored, now=now)
        except CredentialError as exc:
            self._log.warning(
                "stored subscription credential is unusable",
                extra={"tenant_id": tenant_id, "provider": provider, "error": str(exc)},
            )
            return RefreshOutcome(tenant_id, provider, False, "unreadable")

        if not credential.needs_refresh(now, self._window):
            # STILL VALID IS NOT THE SAME AS ALREADY PUBLISHED.
            #
            # An operator onboards a credential minted minutes ago, so its access
            # token has hours left and this path is taken immediately. Returning
            # here without publishing leaves the base secret -- the one the
            # worker actually mounts -- with no version at all, for as long as
            # the token remains fresh. Every task for that tenant fails on a
            # missing secret, and the cause is a credential that is working
            # perfectly.
            #
            # So the access token is published when the base secret does not
            # already carry it. Compared rather than written blindly: writing
            # every sweep would add a secret version every five minutes, which
            # is 288 a day of identical values.
            state, detail = self._base_state(base, credential.access_token)
            if state is _BaseState.CURRENT:
                self._remember_published(base, credential.access_token)
                return RefreshOutcome(
                    tenant_id, provider, False, "still_valid", credential.expires_at
                )

            if state is _BaseState.UNREADABLE and self._already_published(
                base, credential.access_token
            ):
                # THE READ IS REFUSED AND THIS PROCESS ALREADY WROTE THIS EXACT
                # TOKEN HERE. Writing it again cannot change what the secret
                # holds; it can only add another identical version, which is
                # the leak. Its own successful `add_version` is better evidence
                # than the read it is not allowed to make.
                #
                # What this gives up: it no longer heals a base version that
                # somebody disabled or destroyed out of band, because it cannot
                # see that happen. The refresh path publishes unconditionally,
                # so that self-heals within one token lifetime (five hours
                # here) instead of five minutes. Five minutes of healing is not
                # worth 288 unverified writes a day, and the real fix for both
                # is the accessor grant named in the warning below.
                return RefreshOutcome(
                    tenant_id, provider, False, "still_valid", credential.expires_at
                )

            if state is _BaseState.UNREADABLE:
                # Said ONCE PER TOKEN, not once per sweep: the branch above
                # swallows the other 59 ticks of each token's life. Loud
                # because a permanently blind guard is a misconfiguration
                # somebody has to fix, and the only thing that ever reported it
                # was the secret-version count.
                self._log.warning(
                    "could not read the worker-facing secret to see whether it "
                    "already holds this access token, so it was published "
                    "unchecked; grant the broker "
                    "roles/secretmanager.secretAccessor on it, or every new "
                    "token is written without anything confirming it was needed",
                    extra={
                        "tenant_id": tenant_id,
                        "provider": provider,
                        "error": detail,
                    },
                )
            try:
                self._store.add_version(base, credential.access_token)
            except Exception as exc:
                self._log.warning(
                    "could not publish a still-valid access token",
                    extra={
                        "tenant_id": tenant_id,
                        "provider": provider,
                        "error": type(exc).__name__,
                    },
                )
                return RefreshOutcome(tenant_id, provider, False, "publish_failed")
            # AFTER the write returned, never before. A note that we published
            # something we did not is how the branch above would suppress a
            # write the secret actually needs.
            self._remember_published(base, credential.access_token)
            self._log.info(
                "published an access token that was already valid",
                extra={"tenant_id": tenant_id, "provider": provider},
            )
            return RefreshOutcome(
                tenant_id,
                provider,
                True,
                # Distinct from `published` so the sweep response and the
                # Settings screen say which of the two happened: a write the
                # broker CHECKED was needed, or one it could not check.
                "published" if state is _BaseState.NEEDS_PUBLISH else "published_unverified",
                credential.expires_at,
            )

        try:
            fresh = refresh(credential, self._endpoint, now=now)
        except ReauthRequired as exc:
            # Terminal. Say so once, loudly, and stop.
            self._log.error(
                "subscription credential needs a human; tasks will park",
                extra={"tenant_id": tenant_id, "provider": provider, "error": str(exc)},
            )
            return RefreshOutcome(tenant_id, provider, False, "reauth_required")
        except CredentialError as exc:
            self._log.warning(
                "credential refresh failed; will retry on the next sweep",
                extra={"tenant_id": tenant_id, "provider": provider, "error": str(exc)},
            )
            return RefreshOutcome(tenant_id, provider, False, "refresh_failed")

        # ORDER IS THE SAFETY PROPERTY. Persist the means of getting more
        # credentials before the credentials themselves.
        # Written on EVERY refresh, not only when the token rotates: this
        # payload also carries the new expiry, and the next sweep reads its
        # expiry to decide whether to refresh. Skipping the write when the
        # endpoint returns the same refresh token would leave a stale expiry
        # behind and refresh again on every tick, forever.
        self._store.add_version(refresh_secret, serialise(fresh))
        if fresh.refresh_token != credential.refresh_token:
            self._log.info(
                "refresh token rotated and persisted",
                extra={"tenant_id": tenant_id, "provider": provider},
            )

        # Only now the short-lived half the worker actually mounts.
        self._store.add_version(base, fresh.access_token)
        # Recorded here too, and this is what makes the steady state cost
        # nothing even where the read-back is refused: the next ~59 sweeps in
        # this token's life find the digest of the token they were about to
        # write and stop. Without this line the blind-publish branch would fire
        # once per tick again, because nothing else ever tells it what is in
        # there.
        self._remember_published(base, fresh.access_token)

        self._log.info(
            "subscription credential refreshed",
            extra={"tenant_id": tenant_id, "provider": provider, **fresh.redacted()},
        )
        return RefreshOutcome(tenant_id, provider, True, "refreshed", fresh.expires_at)

    def refresh_secret(self, secret_base: str, *, label: str = "") -> RefreshOutcome:
        """Refresh one named credential pair, whatever it belongs to.

        `refresh_tenant` builds its secret name from a tenant and a provider,
        which is right for v1's one-credential-per-tenant model and wrong for an
        account pool, where several credentials belong to the same tenant. This
        takes the base name directly so the two callers share one implementation
        rather than drifting into two that must be kept in step.
        """
        return self._refresh(secret_base, tenant_id=label or secret_base, provider="account")

    def _base_state(self, base: str, access_token: str) -> tuple[_BaseState, str]:
        """What the worker-facing secret holds, and how confident that is.

        Returns the reason string alongside the state because the caller logs
        it: `PermissionDenied` and `ServiceUnavailable` both arrive here as
        UNREADABLE and want completely different things done about them, and
        which one it was is the entire diagnosis.

        `KeyError` is the store's contract for "no such secret, or no enabled
        version" -- see `SecretStore.access`. That is a DEFINITE answer and
        exactly the onboarding case this path exists for, so it is
        NEEDS_PUBLISH rather than UNREADABLE. Collapsing the two would be the
        same mistake in the other direction: a real outage would read as an
        empty secret and be written to on every tick.
        """
        try:
            stored = self._store.access(base)
        except KeyError:
            return _BaseState.NEEDS_PUBLISH, "no_version"
        except Exception as exc:
            return _BaseState.UNREADABLE, type(exc).__name__
        return (
            _BaseState.CURRENT if stored == access_token else _BaseState.NEEDS_PUBLISH
        ), "compared"

    @staticmethod
    def _fingerprint(access_token: str) -> str:
        return hashlib.sha256(access_token.encode("utf-8")).hexdigest()

    def _already_published(self, base: str, access_token: str) -> bool:
        return self._published.get(base) == self._fingerprint(access_token)

    def _remember_published(self, base: str, access_token: str) -> None:
        self._published[base] = self._fingerprint(access_token)

    def sweep_accounts(self, secrets: list[tuple[str, str]]) -> list[RefreshOutcome]:
        """Refresh every account given, INCLUDING ones nobody is using.

        The omission of any "in use" filter is the feature. A refresh token that
        is never exchanged expires, so a pool where two accounts are busy and
        three are idle is a pool where the idle three rot until the day they are
        wanted -- which is the day someone needed capacity in a hurry.

        One account's failure never stops the sweep: the others are exactly what
        the fleet falls back on when one goes bad.
        """
        outcomes: list[RefreshOutcome] = []
        for secret_base, label in secrets:
            try:
                outcomes.append(self.refresh_secret(secret_base, label=label))
            except Exception as exc:
                self._log.error(
                    "account refresh raised; continuing with the rest of the pool",
                    extra={"account": label, "error": type(exc).__name__},
                )
                outcomes.append(RefreshOutcome(label, "account", False, "error"))
        return outcomes

    def sweep(self, tenants: list[tuple[str, str]]) -> list[RefreshOutcome]:
        """Refresh every (tenant, provider) pair that is due."""
        outcomes: list[RefreshOutcome] = []
        for tenant_id, provider in tenants:
            try:
                outcomes.append(self.refresh_tenant(tenant_id, provider))
            except Exception as exc:  # one tenant must not stop the sweep
                self._log.error(
                    "credential sweep raised for a tenant",
                    extra={
                        "tenant_id": tenant_id,
                        "provider": provider,
                        "error": type(exc).__name__,
                    },
                )
                outcomes.append(RefreshOutcome(tenant_id, provider, False, "error"))
        return outcomes
