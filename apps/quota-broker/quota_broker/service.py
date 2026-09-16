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
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import datetime
from typing import Any, Callable

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

log = logging.getLogger(__name__)

QUOTA = "quota"
POOLS = "pools"

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

    def _persist(self, state: QuotaState) -> QuotaState:
        self._db.collection(QUOTA).document(doc_id(state.provider, state.tenant_id)).set(
            quota_to_firestore(state)
        )
        self.apply_to_pools(state)
        return state

    def set_hard_max(self, provider: str, tenant_id: str, hard_max: int) -> QuotaState:
        """An admin's ceiling. Clamps the adaptive target down immediately.

        Lowering the hard max below the current adaptive target must take effect
        at once, or the invariant `adaptive_target <= configured_hard_max` would
        be violated for however long it took the next 429 to arrive.
        """
        if hard_max < 0:
            raise ValueError("configured_hard_max cannot be negative")
        state = self.get(provider, tenant_id)
        state.configured_hard_max = int(hard_max)
        if state.adaptive_target is not None:
            state.adaptive_target = min(state.adaptive_target, int(hard_max))
        state.updated_at = self._now()
        return self._persist(state)

    def observe_success(
        self,
        provider: str,
        tenant_id: str,
        *,
        requests_remaining: int | None = None,
        tokens_remaining: int | None = None,
        reset_at: datetime | None = None,
    ) -> QuotaState:
        state = record_success(
            self.current(provider, tenant_id),
            now=self._now(),
            config=self._config,
            requests_remaining=requests_remaining,
            tokens_remaining=tokens_remaining,
            reset_at=reset_at,
        )
        return self._persist(state)

    def observe_rate_limit(
        self,
        provider: str,
        tenant_id: str,
        *,
        retry_after_seconds: int | None = None,
        reset_at: datetime | None = None,
    ) -> QuotaState:
        state = record_rate_limit(
            self.get(provider, tenant_id),
            now=self._now(),
            config=self._config,
            retry_after_seconds=retry_after_seconds,
            reset_at=reset_at,
        )
        return self._persist(state)

    def observe_exhausted(
        self,
        provider: str,
        tenant_id: str,
        *,
        retry_after_seconds: int | None = None,
        reset_at: datetime | None = None,
    ) -> QuotaState:
        state = record_exhausted(
            self.get(provider, tenant_id),
            now=self._now(),
            config=self._config,
            retry_after_seconds=retry_after_seconds,
            reset_at=reset_at,
        )
        return self._persist(state)

    def sweep(self, limit: int = 500) -> dict[str, int]:
        """Retire expired cooldowns.

        Necessary because the recovery signal is the absence of traffic: while a
        pool's quota-derived limit is 0 no work runs, so no success can arrive to
        clear the state. Something has to notice that the window reopened, and
        this is it. Cloud Scheduler calls it on a tick.
        """
        examined = 0
        recovered = 0
        for snap in self._db.collection(QUOTA).limit(limit).stream():
            stored = quota_from_dict(snap.to_dict())
            examined += 1
            refreshed = refresh(stored, now=self._now(), config=self._config)
            if (
                refreshed.state is not stored.state
                or refreshed.quota_derived_limit != stored.quota_derived_limit
            ):
                self._persist(refreshed)
                recovered += 1
            else:
                # Nothing expired, but re-asserting the pool cap is cheap and
                # repairs a pool an operator edited by hand.
                self.apply_to_pools(stored)
        return {"examined": examined, "recovered": recovered}

    # -- pool bridge ------------------------------------------------------

    def apply_to_pools(self, state: QuotaState) -> dict[str, int | None]:
        """Write the quota-derived cap onto the pools the scheduler reads.

        Two pools are involved:

          provider:<p>:tenant:<t>  the per-tenant provider pool. Takes this
                                   tenant's derived limit directly, so an
                                   EXHAUSTED provider for THIS tenant drives
                                   that pool's effective limit to 0.
          provider:<p>             the shared provider pool. Only capped when
                                   EVERY tenant on the provider is stopped,
                                   because one tenant's spent quota says nothing
                                   about another tenant's key.
        """
        derived = quota_derived_limit_for(state, self._config)
        tenant_pool = f"provider:{state.provider}:tenant:{state.tenant_id}"
        self._write_pool(
            tenant_pool,
            quota_derived_limit=derived,
            adaptive_target=state.adaptive_target,
            default_hard_limit=state.configured_hard_max,
        )
        provider_limit = self._recompute_provider_pool(state.provider)
        return {tenant_pool: derived, f"provider:{state.provider}": provider_limit}

    def _recompute_provider_pool(self, provider: str) -> int | None:
        states = self.list(provider=provider)
        if not states:
            return None
        if all(s.state in _STOP_STATES for s in states):
            limit: int | None = 0
        else:
            # No platform-wide cap: the per-tenant pools carry the real limits.
            limit = None
        self._write_pool(f"provider:{provider}", quota_derived_limit=limit)
        return limit

    def _write_pool(
        self,
        name: str,
        *,
        quota_derived_limit: int | None,
        adaptive_target: int | None = None,
        default_hard_limit: int | None = None,
    ) -> None:
        ref = self._db.collection(POOLS).document(name)
        snap = ref.get()
        now = self._now()
        if not snap.exists:
            ref.set(
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
                }
            )
            return
        patch: dict[str, Any] = {
            "quota_derived_limit": quota_derived_limit,
            "updated_at": now,
        }
        if adaptive_target is not None:
            patch["adaptive_target"] = adaptive_target
        ref.update(patch)
