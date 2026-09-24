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
     what that cost -- and `quota_broker.publishledger` for why the replacement
     had to be durable rather than a dict on this object.

     The rule the whole of `_base_state` exists to enforce: A WRITE NEEDS
     POSITIVE EVIDENCE THAT IT IS NEEDED. A read that failed is not evidence of
     anything. There are exactly two admissible sources -- a successful
     comparison, or a durable record of what this platform last published --
     and when neither can answer, nothing is written and the sweep says so.

     That rule applies to the BASE secret only, and deliberately. The
     `-refresh` half changes on every single exchange -- measured, see the
     comment beside its `add_version` below -- so there is no unchanged write
     to suppress there and a digest comparison over it could never match. Its
     growth is bounded by retention in `quota_broker.secretstore`, not by this.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Protocol

from .oauth import (
    Credential,
    CredentialError,
    ReauthRequired,
    TokenEndpoint,
    parse_credential,
    refresh,
    serialise,
)
from .publishledger import (
    InMemoryPublishLedger,
    LedgerUnavailable,
    PublishLedger,
    fingerprint,
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

    Four values, and every one of them is a separate decision the caller has to
    make. The check used to be a bool, and a bool cannot say "I could not
    look" -- so a read that FAILED returned the same False as a read that
    succeeded and found a different token, and the caller published either way.

    That is indistinguishable-guard-failure, and it cost 1,741 identical secret
    versions on `swarm-account-u-bogdan-devops-main` and 1,814 on
    `swarm-tenant-u-bogdan-anthropic` in saga-agents-staging before anyone
    noticed. The broker holds `secretmanager.versions.add` on the worker-facing
    secret but NOT `secretmanager.versions.access` -- verified in the live IAM
    policy on 2026-09-22, where `roles/secretmanager.secretAccessor` on those
    secrets names the WORKER's service account and the broker appears only
    under `roles/secretmanager.secretVersionAdder` -- so `access()` raises
    PermissionDenied every single time. The bool turned that permanent,
    structural fact into "the secret is stale", and the sweep republished an
    identical value every five minutes from 2026-09-18T07:26 onward without one
    line of log to say so.

    Replacing the bool with a three-valued state and an in-process note reduced
    that to one write per PROCESS rather than per tick, which on a
    scale-to-zero service redeployed several times a day is a slower leak, not
    a closed one -- measured, four deploys and four writes, in
    `quota_broker.publishledger`. The fourth value is what closes it: when the
    read is refused, the answer comes from a durable record or it does not come
    at all.
    """

    #: The read succeeded and the secret already holds this token, or the read
    #: was refused and the durable ledger says this platform put exactly this
    #: token there. Either way: nothing to do.
    CURRENT = "current"
    #: A DEFINITE answer that a write is needed: the read succeeded and the
    #: payload differs, or there is no enabled version at all.
    NEEDS_PUBLISH = "needs_publish"
    #: The read was refused, and the ledger answered that this platform has
    #: never published this token into this secret. Not a comparison -- but a
    #: positive record, and one that can only ever be true once per token, so
    #: it cannot produce a cadence.
    UNVERIFIED_NEEDED = "unverified_needed"
    #: The read failed AND the ledger could not be consulted. NOTHING here knows
    #: anything about the secret's contents, so nothing here may write to it.
    UNKNOWN = "unknown"


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
        ledger: PublishLedger | None = None,
    ) -> None:
        self._store = store
        self._endpoint = endpoint
        self._log = logger
        self._window = window
        self._now = now or (lambda: datetime.now(timezone.utc))
        #: The durable record of what this platform last published into each
        #: worker-facing secret. It is the ONLY evidence admissible when the
        #: read-back is refused -- see `quota_broker.publishledger`, and
        #: `_BaseState` for what an in-process note cost instead.
        #:
        #: Defaulted rather than required so a deployment with no Firestore
        #: handle still gets within-process deduplication; `create_app` passes
        #: the durable one, and that is what production runs on.
        self._ledger: PublishLedger = ledger or InMemoryPublishLedger()
        #: What the ledger is believed to hold, so an unchanged credential
        #: re-reads it but never re-writes it. Purely an optimisation: every
        #: decision is made from `_ledger`, never from this.
        self._ledger_cache: dict[str, str] = {}
        #: secret name -> the digest the "published unchecked" warning was last
        #: emitted for. The warning is worth saying once per token and worth
        #: nothing 288 times a day.
        self._warned: dict[str, str] = {}

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
                # Covers both "the comparison succeeded and matched" and "the
                # comparison was refused, and the ledger says this platform put
                # exactly this token there". Writing again could not change
                # what the secret holds; it could only add another identical
                # version, which is the leak.
                #
                # What the second case gives up: it no longer heals a base
                # version somebody disabled or destroyed out of band, because
                # it cannot see that happen. The refresh path publishes
                # unconditionally, so that self-heals within one token lifetime
                # (five hours here) instead of five minutes. Five minutes of
                # healing is not worth 288 unverified writes a day, and the
                # real fix for both is the accessor grant named below.
                self._remember_published(base, credential.access_token)
                return RefreshOutcome(
                    tenant_id, provider, False, "still_valid", credential.expires_at
                )

            if state is _BaseState.UNKNOWN:
                # NEITHER SOURCE OF EVIDENCE COULD ANSWER, so this does not
                # write. That is the entire lesson of the 1,741-version leak:
                # a failed read is not a negative answer, and publishing on one
                # turns a permanent misconfiguration into a permanent write
                # cadence. Louder than the blind-publish case below because
                # two things are broken at once, and the credential may now be
                # going stale unnoticed.
                self._log.error(
                    "could not read the worker-facing secret AND could not "
                    "read the record of what was last published to it, so "
                    "nothing was written; grant the broker "
                    "roles/secretmanager.secretAccessor on it and check "
                    "Firestore, or this credential will only be republished "
                    "when it next actually rotates",
                    extra={
                        "tenant_id": tenant_id,
                        "provider": provider,
                        "error": detail,
                    },
                )
                return RefreshOutcome(
                    tenant_id,
                    provider,
                    False,
                    "unverified_skipped",
                    credential.expires_at,
                )

            if state is _BaseState.UNVERIFIED_NEEDED:
                # Said ONCE PER TOKEN, not once per sweep, and now that is true
                # across restarts too: the ledger answers CURRENT on every
                # later tick of this token's life, so this branch is reached at
                # most once per (secret, token) for the life of the record.
                # Loud because a permanently blind guard is a misconfiguration
                # somebody has to fix, and the only thing that ever reported it
                # was the secret-version count.
                self._warn_unverified(base, credential.access_token, tenant_id, provider, detail)
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
        #
        # AND THE TOKEN DOES ROTATE, EVERY TIME -- so there is nothing here for
        # a ledger to suppress, and the `-refresh` half will never get the
        # deduplication the base half got. Measured on 2026-09-22 from this
        # deployment's own logs, over the 14 days to that date: 76 exchanges
        # logged "subscription credential refreshed" and 76 logged "refresh
        # token rotated and persisted", with the same per-account distribution
        # (20 / 15 / 15 / 14 / 4 / 4 / 4). That log line fires only on
        # `fresh.refresh_token != credential.refresh_token`, so every exchange
        # returned a NEW refresh token. `oauth.refresh` still carries the
        # presented token forward when the endpoint omits one, because that
        # costs nothing and losing it strands the account -- but no exchange
        # has yet omitted one.
        #
        # The consequence is worth stating plainly, because it is the reason
        # the retention rule exists: every legitimate refresh MUST append a
        # version here, ~4.8 a day per account, forever. The waste was never
        # the write; it was that nothing ever expired what the write
        # superseded. See `RETAINED_VERSIONS` in quota_broker.secretstore.
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
        it: `PermissionDenied` and `ServiceUnavailable` both fail the read and
        want completely different things done about them, and which one it was
        is the entire diagnosis.

        `KeyError` is the store's contract for "no such secret, or no enabled
        version" -- see `SecretStore.access`. That is a DEFINITE answer and
        exactly the onboarding case this path exists for, so it is
        NEEDS_PUBLISH. Collapsing it into the unreadable case would be the same
        mistake in the other direction: a real outage would read as an empty
        secret and be written to on every tick.

        WHEN THE READ FAILS, THIS DOES NOT GUESS. It asks the ledger, which
        answers one of three things, and each maps to a different state:

          * the same digest      -> CURRENT. Positive evidence; no write.
          * a different digest,
            or no record at all  -> UNVERIFIED_NEEDED. Positive evidence that
                                    this platform has not put this token here.
                                    True at most once per (secret, token), so
                                    it cannot produce a cadence.
          * it could not tell    -> UNKNOWN. No evidence. No write.

        The middle case is the only one that writes without a comparison, and
        it is bounded: the write records the digest, and every later tick of
        that token's life reads it back as CURRENT. That bound is what makes
        the difference between one version per credential and the 1,741 this
        path actually produced.
        """
        try:
            stored = self._store.access(base)
        except KeyError:
            return _BaseState.NEEDS_PUBLISH, "no_version"
        except Exception as exc:
            read_error = type(exc).__name__
        else:
            return (
                _BaseState.CURRENT if stored == access_token else _BaseState.NEEDS_PUBLISH
            ), "compared"

        try:
            known = self._ledger.published_digest(base)
        except LedgerUnavailable as exc:
            return _BaseState.UNKNOWN, f"{read_error}/ledger:{exc}"
        except Exception as exc:
            # A ledger that raises something other than its own contract type
            # is still a ledger that did not answer. Anything else here would
            # be the swallowed exception this whole module exists to remove.
            return _BaseState.UNKNOWN, f"{read_error}/ledger:{type(exc).__name__}"

        if known is not None:
            self._ledger_cache[base] = known
            if known == fingerprint(access_token):
                return _BaseState.CURRENT, f"{read_error}/ledger_match"
        return _BaseState.UNVERIFIED_NEEDED, read_error

    def _remember_published(self, base: str, access_token: str) -> None:
        """Record what is now in the secret. Called only after a write returned.

        Skipped when the ledger is already believed to hold this digest, so an
        unchanged credential costs a Firestore READ per tick and never a write.
        A ledger write that fails is logged and NOT cached: the cost of losing
        it is one redundant secret version on a later tick, and pretending it
        landed would cost a write the secret genuinely needs.
        """
        digest = fingerprint(access_token)
        if self._ledger_cache.get(base) == digest:
            return
        try:
            self._ledger.record(base, digest)
        except Exception as exc:
            self._log.warning(
                "could not record which access token was published, so the "
                "next sweep may publish an identical version before it can "
                "tell that it should not",
                extra={"secret": base, "error": type(exc).__name__},
            )
            return
        self._ledger_cache[base] = digest

    def _warn_unverified(
        self, base: str, access_token: str, tenant_id: str, provider: str, detail: str
    ) -> None:
        """Say once per token that a write went out without being checked."""
        digest = fingerprint(access_token)
        if self._warned.get(base) == digest:
            return
        self._warned[base] = digest
        self._log.warning(
            "could not read the worker-facing secret to see whether it "
            "already holds this access token, so it was published "
            "unchecked; grant the broker "
            "roles/secretmanager.secretAccessor on it, or every new "
            "token is written without anything confirming it was needed",
            extra={"tenant_id": tenant_id, "provider": provider, "error": detail},
        )

    def sweep_accounts(
        self,
        secrets: list[tuple[str, str]],
        *,
        keep_going: Callable[[], bool] | None = None,
    ) -> list[RefreshOutcome]:
        """Refresh every account given, INCLUDING ones nobody is using.

        The omission of any "in use" filter is the feature. A refresh token that
        is never exchanged expires, so a pool where two accounts are busy and
        three are idle is a pool where the idle three rot until the day they are
        wanted -- which is the day someone needed capacity in a hurry.

        One account's failure never stops the sweep: the others are exactly what
        the fleet falls back on when one goes bad.

        `keep_going` DOES stop it. It is the sweep lease's fence, asked before
        every account; False means another sweep holds the lease now, and
        exchanging one more refresh token is exactly the race the lease exists
        to prevent. The accounts not reached are the next tick's, and are not
        reported as outcomes -- nothing happened to them.
        """
        outcomes: list[RefreshOutcome] = []
        for secret_base, label in secrets:
            if keep_going is not None and not keep_going():
                break
            try:
                outcomes.append(self.refresh_secret(secret_base, label=label))
            except Exception as exc:
                self._log.error(
                    "account refresh raised; continuing with the rest of the pool",
                    extra={"account": label, "error": type(exc).__name__},
                )
                outcomes.append(RefreshOutcome(label, "account", False, "error"))
        return outcomes

    def sweep(
        self,
        tenants: list[tuple[str, str]],
        *,
        keep_going: Callable[[], bool] | None = None,
    ) -> list[RefreshOutcome]:
        """Refresh every (tenant, provider) pair that is due.

        `keep_going` is the sweep lease's fence, as for `sweep_accounts`.
        """
        outcomes: list[RefreshOutcome] = []
        for tenant_id, provider in tenants:
            if keep_going is not None and not keep_going():
                break
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
