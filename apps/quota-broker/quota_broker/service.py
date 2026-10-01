"""Quota state persistence and the bridge onto slot pools.

The broker owns `quota/{provider}:{tenant_id}` documents and is the ONLY writer
of `quota_derived_limit` on the provider pools. That separation is what keeps
the admission path honest: the scheduler never asks a provider how it feels, it
just reads a pool, and the pool already carries the answer.

Why the state is keyed by (provider, tenant) and not by provider alone: tenants
bring their own API keys, so one tenant burning through their Anthropic quota
says nothing about another tenant's. A provider-wide throttle on a per-tenant
429 would let one tenant halt everyone else's work, which is exactly the
cross-tenant blast radius the platform exists to prevent.

EVERY READ-MODIFY-WRITE HERE IS A FIRESTORE TRANSACTION. Workers on every
tenant report outcomes concurrently, the sweep retires cooldowns on its own
tick, and the scheduler increments `active` on the very pool documents this
module caps. A get-then-set loses whichever write lands first: two 429s
arriving together halved the target once and counted one, a sweep that had
read a document before a fresh 429 committed wrote AVAILABLE over it, and a
pool created here from a stale "absent" read reset `active` to 0 under the
scheduler's admissions. The quota document and its per-tenant pool are
written in ONE transaction (`_mutate`), so the cap the scheduler reads always
matches the state it was derived from; the shared provider pool is recomputed
in a second one that reads every tenant's document it depends on
(`_recompute_provider_pool`), so a concurrent report retries it rather than
being overwritten by an older view. The same `firestore.transactional`
pattern the scheduler's admission and the account holds use.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import datetime
from typing import Any, Callable

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from swarm_common.models import ProviderState, QuotaState, utcnow

from .aimd import (
    AimdConfig,
    initial_state,
    quota_derived_limit_for,
    record_exhausted,
    record_rate_limit,
    record_success,
    refresh,
)
from .settings import BrokerSettings
from .sweeplease import txn_snapshot

log = logging.getLogger(__name__)

QUOTA = "quota"
POOLS = "pools"
TENANTS = "tenants"

#: Every provider state that means "start nothing new right now".
_STOP_STATES = (ProviderState.EXHAUSTED, ProviderState.DISABLED, ProviderState.COOLDOWN)


def doc_id(provider: str, tenant_id: str) -> str:
    return f"{provider}:{tenant_id}"


def quota_to_firestore(state: QuotaState) -> dict[str, Any]:
    payload = asdict(state)
    payload["state"] = state.state.value
    return payload


def quota_from_dict(data: dict[str, Any]) -> QuotaState:
    def _dt(value: Any) -> datetime | None:
        if value is None or isinstance(value, datetime):
            return value
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed

    return QuotaState(
        provider=data["provider"],
        tenant_id=data["tenant_id"],
        state=ProviderState(data.get("state", ProviderState.UNKNOWN.value)),
        updated_at=_dt(data.get("updated_at")) or utcnow(),
        configured_hard_max=int(data.get("configured_hard_max", 50)),
        adaptive_target=data.get("adaptive_target"),
        quota_derived_limit=data.get("quota_derived_limit"),
        requests_remaining=data.get("requests_remaining"),
        tokens_remaining=data.get("tokens_remaining"),
        reset_at=_dt(data.get("reset_at")),
        cooldown_until=_dt(data.get("cooldown_until")),
        last_429_at=_dt(data.get("last_429_at")),
        retry_after_seconds=data.get("retry_after_seconds"),
        success_count=int(data.get("success_count", 0)),
        rate_limit_count=int(data.get("rate_limit_count", 0)),
    )


class QuotaBroker:
    def __init__(
        self,
        db: Any,
        *,
        settings: BrokerSettings,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self._db = db
        self._settings = settings
        self._config: AimdConfig = settings.aimd
        self._now = now

    @property
    def db(self) -> Any:
        """The Firestore client this broker was built with.

        Exposed because the account pool lives in the same database and must be
        built from the same client -- not a second one with its own settings.
        Reaching for `_db` from outside would work and would also be the kind of
        coupling that breaks quietly when the attribute is renamed.
        """
        return self._db

    @property
    def config(self) -> AimdConfig:
        return self._config

    # -- reads ------------------------------------------------------------

    def get(self, provider: str, tenant_id: str) -> QuotaState:
        snap = self._db.collection(QUOTA).document(doc_id(provider, tenant_id)).get()
        if not snap.exists:
            return initial_state(
                provider,
                tenant_id,
                configured_hard_max=self._settings.default_hard_max,
                now=self._now(),
            )
        return quota_from_dict(snap.to_dict())

    def current(self, provider: str, tenant_id: str) -> QuotaState:
        """State with expired cooldowns already retired."""
        state = refresh(self.get(provider, tenant_id), now=self._now(), config=self._config)
        return state

    def list(self, tenant_id: str | None = None, provider: str | None = None,
             limit: int = 500) -> list[QuotaState]:
        query: Any = self._db.collection(QUOTA)
        if tenant_id is not None:
            query = query.where(filter=FieldFilter("tenant_id", "==", tenant_id))
        if provider is not None:
            query = query.where(filter=FieldFilter("provider", "==", provider))
        query = query.limit(limit)
        return [
            refresh(quota_from_dict(snap.to_dict()), now=self._now(), config=self._config)
            for snap in query.stream()
        ]

    # -- writes -----------------------------------------------------------

    def _mutate(
        self,
        provider: str,
        tenant_id: str,
        change: Callable[[QuotaState], QuotaState | None],
        *,
        reassert_pool: bool = False,
        recompute_shared: bool = True,
    ) -> QuotaState:
        """Apply `change` to the stored state inside one transaction.

        The quota document and the tenant's provider pool are read and written
        together, so the pool's cap cannot be left derived from a state that a
        concurrent report has since replaced. `change` sees the COMMITTED
        state and may be run more than once: Firestore re-runs the body when
        another writer touched what it read. Returning None means "nothing to
        write", which the sweep uses so an unchanged document is not rewritten;
        with `reassert_pool` the tenant pool's cap is still written, from the
        committed state.

        The shared provider pool follows in its own transaction once this one
        has committed (`_recompute_provider_pool`), unless `recompute_shared`
        is False because the caller recomputes it once for many documents.
        """
        ref = self._db.collection(QUOTA).document(doc_id(provider, tenant_id))
        pool_name = f"provider:{provider}:tenant:{tenant_id}"
        pool_ref = self._db.collection(POOLS).document(pool_name)
        transaction = self._db.transaction()

        @firestore.transactional
        def _apply(txn: Any) -> tuple[QuotaState, bool]:
            # Every read before any write: Firestore refuses a read after a
            # write inside one transaction.
            snap = txn_snapshot(txn.get(ref))
            pool_snap = txn_snapshot(txn.get(pool_ref))
            stored = (
                quota_from_dict(snap.to_dict() or {})
                if getattr(snap, "exists", False)
                else initial_state(
                    provider,
                    tenant_id,
                    configured_hard_max=self._settings.default_hard_max,
                    now=self._now(),
                )
            )
            changed = change(stored)
            if changed is None:
                if reassert_pool and getattr(snap, "exists", False):
                    self._pool_write(
                        txn,
                        pool_ref,
                        pool_snap,
                        pool_name,
                        quota_derived_limit=quota_derived_limit_for(stored, self._config),
                        adaptive_target=stored.adaptive_target,
                        default_hard_limit=stored.configured_hard_max,
                    )
                return stored, False
            txn.set(ref, quota_to_firestore(changed))
            self._pool_write(
                txn,
                pool_ref,
                pool_snap,
                pool_name,
                quota_derived_limit=quota_derived_limit_for(changed, self._config),
                adaptive_target=changed.adaptive_target,
                default_hard_limit=changed.configured_hard_max,
            )
            return changed, True

        state, written = _apply(transaction)
        if written and recompute_shared:
            self._recompute_provider_pool(provider)
        return state

    def set_hard_max(self, provider: str, tenant_id: str, hard_max: int) -> QuotaState:
        """An admin's ceiling. Clamps the adaptive target down immediately.

        Lowering the hard max below the current adaptive target must take effect
        at once, or the invariant `adaptive_target <= configured_hard_max` would
        be violated for however long it took the next 429 to arrive.
        """
        if hard_max < 0:
            raise ValueError("configured_hard_max cannot be negative")

        def _change(state: QuotaState) -> QuotaState:
            state.configured_hard_max = int(hard_max)
            if state.adaptive_target is not None:
                state.adaptive_target = min(state.adaptive_target, int(hard_max))
            state.updated_at = self._now()
            return state

        return self._mutate(provider, tenant_id, _change)

    def observe_success(
        self,
        provider: str,
        tenant_id: str,
        *,
        requests_remaining: int | None = None,
        tokens_remaining: int | None = None,
        reset_at: datetime | None = None,
    ) -> QuotaState:
        def _change(state: QuotaState) -> QuotaState:
            return record_success(
                refresh(state, now=self._now(), config=self._config),
                now=self._now(),
                config=self._config,
                requests_remaining=requests_remaining,
                tokens_remaining=tokens_remaining,
                reset_at=reset_at,
            )

        return self._mutate(provider, tenant_id, _change)

    def observe_rate_limit(
        self,
        provider: str,
        tenant_id: str,
        *,
        retry_after_seconds: int | None = None,
        reset_at: datetime | None = None,
    ) -> QuotaState:
        def _change(state: QuotaState) -> QuotaState:
            return record_rate_limit(
                state,
                now=self._now(),
                config=self._config,
                retry_after_seconds=retry_after_seconds,
                reset_at=reset_at,
            )

        return self._mutate(provider, tenant_id, _change)

    def observe_exhausted(
        self,
        provider: str,
        tenant_id: str,
        *,
        retry_after_seconds: int | None = None,
        reset_at: datetime | None = None,
    ) -> QuotaState:
        def _change(state: QuotaState) -> QuotaState:
            return record_exhausted(
                state,
                now=self._now(),
                config=self._config,
                retry_after_seconds=retry_after_seconds,
                reset_at=reset_at,
            )

        return self._mutate(provider, tenant_id, _change)

    def sweep(self, limit: int = 500) -> dict[str, int]:
        """Retire expired cooldowns.

        Necessary because the recovery signal is the absence of traffic: while a
        pool's quota-derived limit is 0 no work runs, so no success can arrive to
        clear the state. Something has to notice that the window reopened, and
        this is it. Cloud Scheduler calls it on a tick.

        The listing only says WHICH documents to visit. Whether a cooldown has
        expired is decided again inside each document's own transaction, on the
        committed state: a 429 that lands between the listing and the write
        starts a fresh cooldown, and retiring the listed one would write
        AVAILABLE over it.
        """
        examined = 0
        recovered = 0
        providers: set[str] = set()
        for snap in self._db.collection(QUOTA).limit(limit).stream():
            listed = quota_from_dict(snap.to_dict())
            examined += 1
            providers.add(listed.provider)
            retired: list[bool] = []

            def _change(state: QuotaState) -> QuotaState | None:
                retired.clear()
                refreshed = refresh(state, now=self._now(), config=self._config)
                if (
                    refreshed.state is state.state
                    and refreshed.quota_derived_limit == state.quota_derived_limit
                ):
                    # Nothing expired. The tenant pool is still re-asserted
                    # from this state (`_mutate`), which is cheap and repairs a
                    # pool an operator edited by hand.
                    return None
                retired.append(True)
                return refreshed

            self._mutate(
                listed.provider,
                listed.tenant_id,
                _change,
                reassert_pool=True,
                recompute_shared=False,
            )
            if retired:
                recovered += 1
        # Once per provider rather than once per document: each recompute
        # reads every tenant's document for the provider.
        for provider in sorted(providers):
            self._recompute_provider_pool(provider)
        return {"examined": examined, "recovered": recovered}

    # -- pool bridge ------------------------------------------------------
    #
    # Two pools are involved:
    #
    #   provider:<p>:tenant:<t>  the per-tenant provider pool. Takes this
    #                            tenant's derived limit directly, so an
    #                            EXHAUSTED provider for THIS tenant drives
    #                            that pool's effective limit to 0. Written in
    #                            the quota document's own transaction
    #                            (`_mutate`).
    #   provider:<p>             the shared provider pool. Only capped when
    #                            EVERY tenant on the provider is stopped,
    #                            because one tenant's spent quota says nothing
    #                            about another tenant's key.

    def _recompute_provider_pool(self, provider: str) -> int | None:
        """Cap the SHARED provider pool only when genuinely every tenant is stopped.

        `self.list(provider=...)` sees only tenants that already have a quota
        DOCUMENT, and a tenant that has never reported has none. Reading `all()`
        over that subset meant one tenant hitting its own 429 threshold could
        drive the shared pool -- which `pool_names_for` puts in EVERY tenant's
        admission list -- to an absolute stop, halting tenants that had never
        touched the provider. It was also self-sustaining: with the shared pool
        at 0 no work runs, so no success can arrive to clear it, and the only
        other way out is the platform-only sweep.

        So the roster is consulted, and only when the cheap check has already
        passed: any enabled tenant with no document for this provider means the
        evidence is incomplete and the shared pool stays uncapped. The per-tenant
        pools are unaffected either way, and they are what actually stops the
        tenant that is out of quota.

        ONE TRANSACTION over everything it reads: every quota document for the
        provider, the roster when it is consulted, and the pool. Two reports
        for different tenants committing together each recompute; whichever
        commits second has read the first one's document, or is re-run.
        """
        query: Any = self._db.collection(QUOTA).where(
            filter=FieldFilter("provider", "==", provider)
        ).limit(500)
        pool_name = f"provider:{provider}"
        pool_ref = self._db.collection(POOLS).document(pool_name)
        transaction = self._db.transaction()

        @firestore.transactional
        def _apply(txn: Any) -> tuple[bool, int | None]:
            states = [
                refresh(quota_from_dict(snap.to_dict()), now=self._now(), config=self._config)
                for snap in txn.get(query)
            ]
            if not states:
                return False, None
            limit: int | None = None
            if all(s.state in _STOP_STATES for s in states):
                reported = {s.tenant_id for s in states}
                unreported = [
                    t for t in self._enabled_tenant_ids(txn) if t not in reported
                ]
                if unreported:
                    log.info(
                        "provider %s is stopped for every tenant that has reported, but "
                        "%d enabled tenant(s) have no quota document; leaving the shared "
                        "pool uncapped rather than halting them",
                        provider,
                        len(unreported),
                    )
                else:
                    limit = 0
            pool_snap = txn_snapshot(txn.get(pool_ref))
            self._pool_write(txn, pool_ref, pool_snap, pool_name, quota_derived_limit=limit)
            return True, limit

        _written, limit = _apply(transaction)
        return limit

    def _enabled_tenant_ids(self, txn: Any, limit: int = 500) -> list[str]:
        """Tenants that may currently be admitted, read in the caller's transaction.

        Read only when the subset check above has already said "all stopped",
        which is rare, so the ordinary report path still costs one query.
        """
        out: list[str] = []
        for snap in txn.get(self._db.collection(TENANTS).limit(limit)):
            data = snap.to_dict() or {}
            if data.get("enabled", True):
                out.append(str(data.get("tenant_id") or snap.id))
        return out

    def _pool_write(
        self,
        txn: Any,
        ref: Any,
        snap: Any,
        name: str,
        *,
        quota_derived_limit: int | None,
        adaptive_target: int | None = None,
        default_hard_limit: int | None = None,
    ) -> None:
        """Write the cap onto one pool, IN the caller's transaction.

        `snap` is the pool as that transaction read it. Creating the pool from
        an "absent" read outside a transaction is how `active` got reset to 0:
        the scheduler could create and admit against the pool between the read
        and the `set`, and the `set` replaced its count. Inside the transaction
        that interleaving re-runs the body, which then sees the pool and only
        patches the two fields this module owns.
        """
        now = self._now()
        if not getattr(snap, "exists", False):
            txn.set(
                ref,
                {
                    "name": name,
                    "hard_limit": int(
                        default_hard_limit
                        if default_hard_limit is not None
                        else self._settings.default_hard_max
                    ),
                    "adaptive_target": adaptive_target,
                    "quota_derived_limit": quota_derived_limit,
                    "active": 0,
                    "enabled": True,
                    "updated_at": now,
                },
            )
            return
        patch: dict[str, Any] = {
            "quota_derived_limit": quota_derived_limit,
            "updated_at": now,
        }
        if adaptive_target is not None:
            patch["adaptive_target"] = adaptive_target
        txn.update(ref, patch)
