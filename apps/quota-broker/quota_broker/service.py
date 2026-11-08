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
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Callable

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from swarm_common.models import ProviderState, QuotaState, utcnow

from .aimd import (
    AimdConfig,
    # Re-exported: the route maps it to a 409 and imports it from here.
    TargetResetRefused as TargetResetRefused,
    ceiling_of,
    current_target,
    initial_state,
    quota_derived_limit_for,
    record_exhausted,
    record_rate_limit,
    record_success,
    recover_over_time,
    recovery_clock,
    refresh,
    reset_target,
)
from .settings import BrokerSettings
from .sweeplease import txn_snapshot

log = logging.getLogger(__name__)

QUOTA = "quota"
POOLS = "pools"
TENANTS = "tenants"
#: One append-only document per admin change to a quota document, written in
#: the change's own transaction (`_mutate(audit=...)`): a target reset
#: (`adaptive_target_reset`, QUOTA-RESET) and a configured-max change
#: (`hard_max_set`, ACCOUNT-429-FOLLOWUP). Every other write here is a
#: provider's signal rather than a person's decision -- except a time-based
#: climb (`adaptive_target_recovered`, owner 2026-11-02), recorded here too so
#: every rise of a target that is not a success run's has a record.
QUOTA_AUDIT = "quota_audit"

#: The quota document's field holding the time of the last cut or the last
#: climb, from which `aimd.recover_over_time` counts quiet intervals. Kept
#: beside the frozen `QuotaState` fields rather than in them: the clock is this
#: store's, and `quota_from_dict` and every other reader ignore it.
RECOVERY_CLOCK = "recovery_clock_at"

#: The quota document's field holding when `adaptive_target` last changed: a
#: cut, a climb (successes or quiet time), an admin override or a max's clamp
#: (owner, 2026-11-02). Written by `_mutate` only when the committed target
#: differs from the stored one, and carried forward unchanged otherwise, so a
#: success that leaves the target alone does not move it -- unlike
#: `updated_at`, which every write moves. Kept beside the frozen `QuotaState`
#: fields, as `RECOVERY_CLOCK` is; the readers that date a lowered target
#: (`scheduler.limits.lowered_by`, swarm-api's pool `lowered_by`) prefer it
#: and fall back to the cut or override for a document written before it.
TARGET_CHANGED_AT = "target_changed_at"

#: Every provider state that means "start nothing new right now".
_STOP_STATES = (ProviderState.EXHAUSTED, ProviderState.DISABLED, ProviderState.COOLDOWN)


def doc_id(provider: str, tenant_id: str) -> str:
    return f"{provider}:{tenant_id}"


def quota_to_firestore(state: QuotaState) -> dict[str, Any]:
    payload = asdict(state)
    payload["state"] = state.state.value
    return payload


def _dt(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed


def recovery_clock_of(data: dict[str, Any] | None) -> datetime | None:
    """The stored recovery clock, or None for a document written before it."""
    return _dt((data or {}).get(RECOVERY_CLOCK))


def pool_hard_limit_of(snap: Any) -> int | None:
    """The per-tenant pool's `hard_limit`, or None when there is no pool or no
    usable number on it -- then the quota document's maximum is the ceiling."""
    if not getattr(snap, "exists", False):
        return None
    value = (snap.to_dict() or {}).get("hard_limit")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(0, int(value))


def quota_from_dict(data: dict[str, Any]) -> QuotaState:

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


@dataclass
class TargetReset:
    """What `reset_adaptive_target` committed: the state, and its audit record."""

    state: QuotaState
    audit: dict[str, Any] = field(default_factory=dict)


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
        """State with expired cooldowns retired and the time-based climb applied.

        Read-only: the climb shown here is the one the next write of the
        document (a report, or the sweep's tick) commits and audits.
        """
        snap = self._db.collection(QUOTA).document(doc_id(provider, tenant_id)).get()
        if not snap.exists:
            return self.get(provider, tenant_id)
        data = snap.to_dict() or {}
        return self._as_read(quota_from_dict(data), recovery_clock_of(data))

    def list(self, tenant_id: str | None = None, provider: str | None = None,
             limit: int = 500) -> list[QuotaState]:
        query: Any = self._db.collection(QUOTA)
        if tenant_id is not None:
            query = query.where(filter=FieldFilter("tenant_id", "==", tenant_id))
        if provider is not None:
            query = query.where(filter=FieldFilter("provider", "==", provider))
        query = query.limit(limit)
        out: list[QuotaState] = []
        for snap in query.stream():
            data = snap.to_dict() or {}
            out.append(self._as_read(quota_from_dict(data), recovery_clock_of(data)))
        return out

    def _as_read(self, state: QuotaState, clock_at: datetime | None) -> QuotaState:
        """`refresh`, then `recover_over_time` against the tenant pool's ceiling."""
        pool_limit = pool_hard_limit_of(
            self._db.collection(POOLS)
            .document(f"provider:{state.provider}:tenant:{state.tenant_id}")
            .get()
        )
        recovered, _clock, _spent = recover_over_time(
            state,
            clock_at=clock_at,
            now=self._now(),
            config=self._config,
            pool_hard_limit=pool_limit,
        )
        return refresh(
            recovered, now=self._now(), config=self._config, pool_hard_limit=pool_limit
        )

    # -- writes -----------------------------------------------------------

    def _mutate(
        self,
        provider: str,
        tenant_id: str,
        change: Callable[[QuotaState, int | None], QuotaState | None],
        *,
        reassert_pool: bool = False,
        recompute_shared: bool = True,
        audit: Callable[[QuotaState, QuotaState], dict[str, Any]] | None = None,
        set_pool_hard_limit: int | None = None,
    ) -> QuotaState:
        """Apply `change` to the stored state inside one transaction.

        The quota document and the tenant's provider pool are read and written
        together, so the pool's cap cannot be left derived from a state that a
        concurrent report has since replaced. `change` sees the COMMITTED
        state and the pool's `hard_limit` (None when there is no pool), and
        may be run more than once: Firestore re-runs the body when another
        writer touched what it read. Returning None means "nothing to write",
        which the sweep uses so an unchanged document is not rewritten; with
        `reassert_pool` the tenant pool's cap is still written, from the
        committed state.

        THE TIME-BASED CLIMB IS APPLIED FIRST (owner, 2026-11-02). The stored
        state goes through `aimd.recover_over_time` before `change` sees it,
        so a 429 halves from the target the quiet time has earned, and a
        climb is committed even when `change` has nothing to write -- the
        sweep's tick is what carries a quiet workspace back up. The clock is
        written with the document (`RECOVERY_CLOCK`), restarted by any cut or
        climb (`aimd.recovery_clock`), and each time-based climb gets its own
        `adaptive_target_recovered` record in `quota_audit`, in this
        transaction.

        `TARGET_CHANGED_AT` is written only when the committed target differs
        from the stored one (a cut, a climb, an override, the clamp), and is
        otherwise carried forward as it was.

        `set_pool_hard_limit` writes the pool's `hard_limit` in the same
        transaction, and the pool's cap is then derived against it: the one
        caller is `set_hard_max`, which keeps the two ceilings equal.

        The shared provider pool follows in its own transaction once this one
        has committed (`_recompute_provider_pool`), unless `recompute_shared`
        is False because the caller recomputes it once for many documents.

        `audit`, when given, builds a record from (stored, changed) that is
        written to `quota_audit` IN THIS TRANSACTION, so a change and the
        record of who made it commit together or not at all. Its document id
        is chosen once, outside the body, so a re-run overwrites its own
        record rather than adding a second.
        """
        ref = self._db.collection(QUOTA).document(doc_id(provider, tenant_id))
        pool_name = f"provider:{provider}:tenant:{tenant_id}"
        pool_ref = self._db.collection(POOLS).document(pool_name)
        audit_ref = self._db.collection(QUOTA_AUDIT).document() if audit else None
        recovery_ref = self._db.collection(QUOTA_AUDIT).document()
        transaction = self._db.transaction()

        @firestore.transactional
        def _apply(txn: Any) -> tuple[QuotaState, bool]:
            # Every read before any write: Firestore refuses a read after a
            # write inside one transaction.
            snap = txn_snapshot(txn.get(ref))
            pool_snap = txn_snapshot(txn.get(pool_ref))
            exists = bool(getattr(snap, "exists", False))
            data = (snap.to_dict() or {}) if exists else {}
            stored = (
                quota_from_dict(data)
                if exists
                else initial_state(
                    provider,
                    tenant_id,
                    configured_hard_max=self._settings.default_hard_max,
                    now=self._now(),
                )
            )
            pool_limit = pool_hard_limit_of(pool_snap)
            stored_clock = recovery_clock_of(data)
            recovered, clock, spent = recover_over_time(
                stored,
                clock_at=stored_clock,
                now=self._now(),
                config=self._config,
                pool_hard_limit=pool_limit,
            )
            # Where the quiet time was counted from: the clock before it moved.
            since = (
                clock - timedelta(seconds=spent * self._config.recovery_interval_seconds)
                if spent and clock is not None
                else stored_clock
            )
            changed = change(recovered, pool_limit)
            if changed is None and spent:
                changed = recovered
            if set_pool_hard_limit is not None:
                pool_limit = max(0, int(set_pool_hard_limit))
            if changed is None:
                if reassert_pool and exists:
                    self._pool_write(
                        txn,
                        pool_ref,
                        pool_snap,
                        pool_name,
                        quota_derived_limit=quota_derived_limit_for(
                            stored, self._config, pool_hard_limit=pool_limit
                        ),
                        adaptive_target=stored.adaptive_target,
                        default_hard_limit=stored.configured_hard_max,
                    )
                return stored, False
            clock = recovery_clock(
                recovered,
                changed,
                clock_at=clock,
                now=self._now(),
                config=self._config,
                pool_hard_limit=pool_limit,
            )
            payload = quota_to_firestore(changed)
            payload[RECOVERY_CLOCK] = clock
            # Against the STORED target, before the time-based climb: a climb
            # is a change too. `txn.set` replaces the whole document, so an
            # unchanged target keeps the instant it already had.
            if changed.adaptive_target != stored.adaptive_target:
                payload[TARGET_CHANGED_AT] = changed.updated_at
            elif data.get(TARGET_CHANGED_AT) is not None:
                payload[TARGET_CHANGED_AT] = data[TARGET_CHANGED_AT]
            txn.set(ref, payload)
            if audit is not None and audit_ref is not None:
                txn.set(audit_ref, audit(recovered, changed))
            if spent:
                txn.set(
                    recovery_ref,
                    self._recovery_record(
                        stored, recovered, spent, since=since,
                        pool_limit=pool_hard_limit_of(pool_snap),
                    ),
                )
            self._pool_write(
                txn,
                pool_ref,
                pool_snap,
                pool_name,
                quota_derived_limit=quota_derived_limit_for(
                    changed, self._config, pool_hard_limit=pool_limit
                ),
                adaptive_target=changed.adaptive_target,
                default_hard_limit=changed.configured_hard_max,
                hard_limit=set_pool_hard_limit,
            )
            return changed, True

        state, written = _apply(transaction)
        if written and recompute_shared:
            # The tenant's state is already durable. Raising here would tell a
            # worker its report failed, and its retry would apply the same 429
            # twice; the sweep recomputes the shared pool on its next tick.
            try:
                self._recompute_provider_pool(provider)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "shared pool recompute for %s failed after the tenant write "
                    "committed; the sweep will redo it: %s",
                    provider,
                    str(exc)[:300],
                )
        return state

    def _recovery_record(
        self,
        stored: QuotaState,
        recovered: QuotaState,
        intervals: int,
        *,
        since: datetime | None,
        pool_limit: int | None,
    ) -> dict[str, Any]:
        """The `quota_audit` record of one time-based climb.

        Shaped like a target reset's, so one reader of `quota_audit` reads
        the target and the ceiling the same way from both; no person asked
        for it, so `requested_by` and `caller` are None and `reason` says
        what earned it.
        """
        interval = int(self._config.recovery_interval_seconds)
        from_target = current_target(stored, self._config, pool_hard_limit=pool_limit)
        log.info(
            "adaptive target for %s:%s recovered from %s to %s after %d quiet interval(s)",
            stored.provider,
            stored.tenant_id,
            from_target,
            recovered.adaptive_target,
            intervals,
        )
        return {
            "action": "adaptive_target_recovered",
            "provider": stored.provider,
            "tenant_id": stored.tenant_id,
            "requested_by": None,
            "caller": None,
            "reason": (
                f"{intervals} interval(s) of {interval} s with no workspace-wide refusal"
            ),
            "from_target": from_target,
            "to_target": recovered.adaptive_target,
            "intervals": intervals,
            "interval_seconds": interval,
            "since": since,
            "configured_hard_max": recovered.configured_hard_max,
            "pool_hard_limit": pool_limit,
            "ceiling": ceiling_of(recovered, pool_limit),
            "at": self._now(),
        }

    def set_hard_max(
        self,
        provider: str,
        tenant_id: str,
        hard_max: int,
        *,
        requested_by: str = "",
        reason: str = "",
        caller: str = "",
    ) -> QuotaState:
        """An admin's ceiling. Clamps the adaptive target down immediately.

        Lowering the hard max below the current adaptive target must take effect
        at once, or the invariant `adaptive_target <= configured_hard_max` would
        be violated for however long it took the next 429 to arrive.

        AUDITED in `quota_audit` in the change's own transaction, as a target
        reset is (the owner, 2026-10-25, "Yes, follow-up in the broker"): who asked
        (`requested_by`, the person the platform caller acts for; `caller`,
        the identity the broker verified), why, and the max and target before
        and after. swarm-api keeps its own `admin_audit` entry as well; this
        one is the record that holds for any platform caller, not only the
        console.
        """
        if hard_max < 0:
            raise ValueError("configured_hard_max cannot be negative")

        # The values before are taken here, on each run of the transaction,
        # together with the pool's own hard limit, which this change moves to
        # the same number (owner, 2026-11-02: the two ceilings must not
        # disagree, or halving starts from one the console does not show).
        before: dict[str, Any] = {}

        def _change(state: QuotaState, pool_limit: int | None) -> QuotaState:
            before["hard_max"] = state.configured_hard_max
            before["target"] = state.adaptive_target
            before["pool_hard_limit"] = pool_limit
            target = state.adaptive_target
            if target is not None:
                target = min(target, int(hard_max))
            return replace(
                state,
                configured_hard_max=int(hard_max),
                adaptive_target=target,
                updated_at=self._now(),
            )

        def _audit(stored: QuotaState, changed: QuotaState) -> dict[str, Any]:
            return {
                "action": "hard_max_set",
                "provider": provider,
                "tenant_id": tenant_id,
                "requested_by": requested_by or None,
                "caller": caller or None,
                "reason": (reason or "").strip()[:500] or None,
                "from_hard_max": before.get("hard_max"),
                "to_hard_max": changed.configured_hard_max,
                # The name a target reset's record uses, so one reader of
                # `quota_audit` reads the ceiling the same way from both.
                "configured_hard_max": changed.configured_hard_max,
                "from_target": before.get("target"),
                "to_target": changed.adaptive_target,
                "from_pool_hard_limit": before.get("pool_hard_limit"),
                "to_pool_hard_limit": int(hard_max),
                "at": self._now(),
            }

        state = self._mutate(
            provider, tenant_id, _change, audit=_audit, set_pool_hard_limit=int(hard_max)
        )
        log.info(
            "configured hard max for %s:%s set to %s by %s",
            provider,
            tenant_id,
            int(hard_max),
            requested_by or caller or "an unnamed platform caller",
        )
        return state

    def reset_adaptive_target(
        self,
        provider: str,
        tenant_id: str,
        *,
        target: int,
        reason: str,
        requested_by: str = "",
        caller: str = "",
        clear_cooldown: bool = False,
    ) -> TargetReset:
        """A platform admin's reset of a throttled tenant's AIMD target.

        The rules are `aimd.reset_target`'s, applied to the COMMITTED state
        inside `_mutate`, so the quota document, the per-tenant pool's
        `adaptive_target` / `quota_derived_limit` and the audit record land in
        one transaction -- the same one every other signal uses. A refusal
        (EXHAUSTED without `clear_cooldown`) raises out of that transaction
        and writes nothing.

        `requested_by` is the person the platform caller says it acts for
        (swarm-api forwards its admin's verified email); `caller` is the
        identity the broker itself verified. Both are recorded, because the
        broker can vouch only for the second.
        """
        why = (reason or "").strip()
        if not why:
            raise ValueError("a reason is required to reset an adaptive target")
        recorded: list[dict[str, Any]] = []

        def _change(state: QuotaState, pool_limit: int | None) -> QuotaState:
            return reset_target(
                refresh(
                    state, now=self._now(), config=self._config, pool_hard_limit=pool_limit
                ),
                target,
                now=self._now(),
                config=self._config,
                clear_cooldown=clear_cooldown,
                pool_hard_limit=pool_limit,
            )

        def _audit(stored: QuotaState, changed: QuotaState) -> dict[str, Any]:
            record = {
                "action": "adaptive_target_reset",
                "provider": provider,
                "tenant_id": tenant_id,
                "requested_by": requested_by or None,
                "caller": caller or None,
                "reason": why[:500],
                "requested_target": int(target),
                "from_target": current_target(stored, self._config),
                "to_target": changed.adaptive_target,
                "clamped": changed.adaptive_target != int(target),
                "configured_hard_max": changed.configured_hard_max,
                "from_state": stored.state.value,
                "to_state": changed.state.value,
                "cleared_cooldown": bool(clear_cooldown),
                "from_effective_limit": quota_derived_limit_for(
                    stored, self._config, now=self._now()
                ),
                "to_effective_limit": quota_derived_limit_for(
                    changed, self._config, now=self._now()
                ),
                "at": self._now(),
            }
            recorded.clear()
            recorded.append(record)
            return record

        state = self._mutate(provider, tenant_id, _change, audit=_audit)
        record = recorded[-1] if recorded else {}
        log.info(
            "adaptive target reset for %s:%s from %s to %s by %s",
            provider,
            tenant_id,
            record.get("from_target"),
            record.get("to_target"),
            requested_by or caller or "an unnamed platform caller",
            extra={"quota_audit": {k: v for k, v in record.items() if k != "at"}},
        )
        return TargetReset(state=state, audit=record)

    def observe_success(
        self,
        provider: str,
        tenant_id: str,
        *,
        requests_remaining: int | None = None,
        tokens_remaining: int | None = None,
        reset_at: datetime | None = None,
    ) -> QuotaState:
        def _change(state: QuotaState, pool_limit: int | None) -> QuotaState:
            return record_success(
                refresh(
                    state, now=self._now(), config=self._config, pool_hard_limit=pool_limit
                ),
                now=self._now(),
                config=self._config,
                pool_hard_limit=pool_limit,
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
        def _change(state: QuotaState, pool_limit: int | None) -> QuotaState:
            return record_rate_limit(
                state,
                now=self._now(),
                config=self._config,
                pool_hard_limit=pool_limit,
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
        def _change(state: QuotaState, pool_limit: int | None) -> QuotaState:
            return record_exhausted(
                state,
                now=self._now(),
                config=self._config,
                pool_hard_limit=pool_limit,
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

            def _change(state: QuotaState, pool_limit: int | None) -> QuotaState | None:
                retired.clear()
                refreshed = refresh(
                    state, now=self._now(), config=self._config, pool_hard_limit=pool_limit
                )
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
        hard_limit: int | None = None,
    ) -> None:
        """Write the cap onto one pool, IN the caller's transaction.

        `snap` is the pool as that transaction read it. Creating the pool from
        an "absent" read outside a transaction is how `active` got reset to 0:
        the scheduler could create and admit against the pool between the read
        and the `set`, and the `set` replaced its count. Inside the transaction
        that interleaving re-runs the body, which then sees the pool and only
        patches the fields this module owns.
        """
        now = self._now()
        if not getattr(snap, "exists", False):
            txn.set(
                ref,
                {
                    "name": name,
                    "hard_limit": int(
                        hard_limit
                        if hard_limit is not None
                        else default_hard_limit
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
        if hard_limit is not None:
            # `set_hard_max` only: the pool's ceiling moved with the quota
            # document's, in the same transaction.
            patch["hard_limit"] = int(hard_limit)
        txn.update(ref, patch)
