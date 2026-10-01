"""The broker's quota and pool writes lose nothing to a concurrent writer.

Gap audit D2 (2026-10-01). `QuotaBroker._persist`, the `observe_*` methods and
`_write_pool` were get-then-set. Three interleavings, each driven below
through the shipped methods:

  1. TWO REPORTS AT ONCE. Two workers on one tenant meet a 429 together. Both
     read the same state, both halve it, both write: the target is halved once
     and the run counts one 429. The second halving -- the one AIMD exists to
     apply fast -- is lost, and the exhaustion threshold needs more 429s than
     it says.
  2. A POOL CREATED UNDER THE SCHEDULER. The broker read the tenant pool as
     absent, the scheduler created it and admitted against it, and the
     broker's `set` then replaced the document with `active: 0`. Capacity
     accounting (invariant 3) lost every slot in use.
  3. THE SWEEP OVER A FRESH 429. The sweep listed an expired cooldown, a new
     429 committed a fresh one, and the sweep wrote its stale AVAILABLE over
     it -- re-opening a tenant the provider had just closed.

WHAT THE FAKE MODELS. `ContendedFirestore` is the shared `FakeFirestore` with
two additions local to this file, the shape test_request_cancel_is_transactional
uses: a one-shot hook that runs a concurrent writer right after a read, and a
transaction commit that raises `Aborted` when anything the transaction read --
a document, or the rows of a query -- changed since. The REAL
`firestore.transactional` decorator catches that and re-runs the body. A plain
read outside a transaction fires the hook too, which is what makes each test
red on the get-then-set code for the right reason.
"""

from __future__ import annotations

import copy
import uuid
from datetime import timedelta
from typing import Any, Callable, Iterator

from google.api_core import exceptions as gexc

from swarm_common.models import ProviderState, utcnow

from quota_broker.service import QuotaBroker
from quota_broker.settings import BrokerSettings

from .conftest import core_settings
from .fakes import (
    FakeCollectionRef,
    FakeDocumentRef,
    FakeFirestore,
    FakeQuery,
    FakeSnapshot,
    FakeTransaction,
)

TENANT = "eng"
PROVIDER = "anthropic"
DOC = f"quota/{PROVIDER}:{TENANT}"
TENANT_POOL = f"pools/provider:{PROVIDER}:tenant:{TENANT}"


class _WatchedRef(FakeDocumentRef):
    def get(self, *args: Any, **kwargs: Any) -> FakeSnapshot:
        snap = super().get(*args, **kwargs)
        self._db.after_read(self.path)
        return snap


class _WatchedQuery(FakeQuery):
    def _with(self, **kwargs: Any) -> FakeQuery:
        base = super()._with(**kwargs)
        query = _WatchedQuery(self._db, self._path)
        query.__dict__.update(base.__dict__)
        return query

    def stream(self, *args: Any, **kwargs: Any) -> Iterator[FakeSnapshot]:
        rows = list(super().stream(*args, **kwargs))
        self._db.after_read(f"stream:{self._path}")
        return iter(rows)


class _WatchedCollection(FakeCollectionRef, _WatchedQuery):
    def document(self, doc_id: str | None = None) -> FakeDocumentRef:
        return _WatchedRef(self._db, f"{self._path}/{doc_id or uuid.uuid4().hex}")


class ContendedTransaction(FakeTransaction):
    """`FakeTransaction`, plus: a commit aborts if anything it read changed."""

    def __init__(self, db: "ContendedFirestore") -> None:
        super().__init__(db)
        self._read_set: dict[str, Any] = {}
        self._queries: list[tuple[Any, list[tuple[str, Any]]]] = []

    def _clean_up(self) -> None:
        super()._clean_up()
        self._read_set = {}
        self._queries = []

    def get(self, ref: Any, **kwargs: Any) -> Any:
        if isinstance(ref, FakeDocumentRef):
            result = super().get(ref, **kwargs)
            self._read_set[ref.path] = copy.deepcopy(result.to_dict())
            return result
        rows = list(super().get(ref, **kwargs))
        self._queries.append((ref, [(r.reference.path, copy.deepcopy(r.to_dict())) for r in rows]))
        return iter(rows)

    def _commit(self) -> list[Any]:
        changed = [p for p, seen in self._read_set.items() if self._db.docs.get(p) != seen]
        for query, seen_rows in self._queries:
            now_rows = [(r.reference.path, r.to_dict()) for r in FakeQuery.stream(query)]
            if now_rows != seen_rows:
                changed.append(f"query:{query._path}")
        if changed:
            self._db.aborts.extend(changed)
            self._buffer = []
            raise gexc.Aborted(f"{changed} changed after this transaction read it")
        return super()._commit()


class ContendedFirestore(FakeFirestore):
    def __init__(self) -> None:
        super().__init__()
        self.aborts: list[str] = []
        self._after_read: dict[str, Callable[[], None]] = {}

    def collection(self, path: str) -> FakeCollectionRef:
        return _WatchedCollection(self, path)

    def document(self, path: str) -> FakeDocumentRef:
        return _WatchedRef(self, path)

    def transaction(self, **kwargs: Any) -> ContendedTransaction:
        return ContendedTransaction(self)

    def interleave(self, key: str, writer: Callable[[], None]) -> None:
        """Run `writer` once, right after the next read of `key` returns."""
        self._after_read[key] = writer

    def after_read(self, key: str) -> None:
        writer = self._after_read.pop(key, None)
        if writer is not None:
            writer()


def _broker(db: FakeFirestore, now: Callable[[], Any] = utcnow) -> QuotaBroker:
    return QuotaBroker(
        db, settings=BrokerSettings(core=core_settings(), default_hard_max=40), now=now
    )


# -- 1. two reports at once -------------------------------------------------------


def test_two_concurrent_429s_both_apply():
    db = ContendedFirestore()
    broker = _broker(db)
    broker.observe_success(PROVIDER, TENANT)          # the document exists, target 40

    db.interleave(DOC, lambda: _broker(db).observe_rate_limit(PROVIDER, TENANT))
    broker.observe_rate_limit(PROVIDER, TENANT)

    stored = db.docs[DOC]
    assert stored["rate_limit_count"] == 2, "one of two concurrent 429s was lost"
    assert stored["adaptive_target"] == 10, "40 halved twice is 10; once is a lost update"
    assert db.docs[TENANT_POOL]["adaptive_target"] == 10
    assert DOC in db.aborts, "the race was never exercised"


def test_a_success_and_a_429_at_once_both_apply():
    db = ContendedFirestore()
    broker = _broker(db)
    broker.observe_success(PROVIDER, TENANT)

    db.interleave(DOC, lambda: _broker(db).observe_rate_limit(PROVIDER, TENANT))
    broker.observe_success(PROVIDER, TENANT)

    stored = db.docs[DOC]
    # The 429 committed first; the success then ran on top of it.
    assert stored["adaptive_target"] == 20
    assert stored["success_count"] == 1
    assert stored["last_429_at"] is not None


# -- 2. a pool created under the scheduler -----------------------------------------


def test_creating_a_pool_never_resets_a_concurrent_admission():
    db = ContendedFirestore()
    broker = _broker(db)

    def scheduler_creates_and_admits() -> None:
        FakeDocumentRef(db, TENANT_POOL).set(
            {"name": TENANT_POOL.split("/", 1)[1], "hard_limit": 40, "active": 3,
             "enabled": True}
        )

    db.interleave(TENANT_POOL, scheduler_creates_and_admits)
    broker.observe_rate_limit(PROVIDER, TENANT)

    pool = db.docs[TENANT_POOL]
    assert pool["active"] == 3, "the broker's create replaced the scheduler's admissions"
    assert pool["quota_derived_limit"] == 0
    assert TENANT_POOL in db.aborts


def test_the_shared_pool_is_recomputed_from_every_tenant_it_read():
    """Two tenants stop at once. Whichever recompute commits last must have
    seen both, or the shared pool stays open on a stale view."""
    db = ContendedFirestore()
    broker = _broker(db)
    for tenant in ("eng", "research"):
        db.docs[f"tenants/{tenant}"] = {"tenant_id": tenant, "enabled": True}
    for _ in range(broker.config.exhaustion_threshold - 1):
        broker.observe_rate_limit(PROVIDER, "eng")
        broker.observe_rate_limit(PROVIDER, "research")

    def research_exhausts() -> None:
        _broker(db).observe_rate_limit(PROVIDER, "research")

    # Fires as eng's recompute streams the provider's documents.
    db.interleave("stream:quota", research_exhausts)
    broker.observe_rate_limit(PROVIDER, "eng")

    for tenant in ("eng", "research"):
        assert db.docs[f"quota/{PROVIDER}:{tenant}"]["state"] == ProviderState.EXHAUSTED.value
    assert db.docs[f"pools/provider:{PROVIDER}"]["quota_derived_limit"] == 0


# -- 3. the sweep over a fresh 429 ---------------------------------------------------


def test_the_sweep_never_retires_a_cooldown_a_concurrent_429_just_set():
    db = ContendedFirestore()
    clock = {"now": utcnow()}
    broker = _broker(db, now=lambda: clock["now"])
    broker.observe_rate_limit(PROVIDER, TENANT, retry_after_seconds=60)
    clock["now"] += timedelta(seconds=120)           # that cooldown has expired

    def fresh_429() -> None:
        _broker(db, now=lambda: clock["now"]).observe_rate_limit(
            PROVIDER, TENANT, retry_after_seconds=600
        )

    # Fires after the sweep has listed the expired document.
    db.interleave("stream:quota", fresh_429)
    report = broker.sweep()

    stored = db.docs[DOC]
    assert stored["state"] == ProviderState.THROTTLED.value, (
        "the sweep wrote AVAILABLE over a 429 that arrived after it listed"
    )
    assert stored["rate_limit_count"] == 2
    assert db.docs[TENANT_POOL]["quota_derived_limit"] == 0
    assert report["recovered"] == 0


# -- the shared recompute failing after the tenant write --------------------------


def test_a_failed_shared_recompute_does_not_fail_a_committed_report(monkeypatch):
    # The tenant's 429 is durable once its transaction commits. If the shared
    # pool's recompute then exhausts its retries and the report raised, the
    # worker would send the same 429 again and it would be halved twice.
    db = ContendedFirestore()
    broker = _broker(db)
    broker.observe_success(PROVIDER, TENANT)          # target 40

    def _contended(_provider: str) -> None:
        raise gexc.Aborted("too much contention")

    monkeypatch.setattr(broker, "_recompute_provider_pool", _contended)
    state = broker.observe_rate_limit(PROVIDER, TENANT)

    assert state.adaptive_target == 20
    assert db.docs[DOC]["adaptive_target"] == 20
    assert db.docs[DOC]["rate_limit_count"] == 1
