"""The join-request and membership invalid-record counters are ApiMetrics'
own, declared beside `swarm_api_role_grant_invalid_total`, not registered
lazily by the first store (owner decision 2026-10-25 23:34Z; part of #2325).

A counter declared up front is on `/metrics` from the first scrape, so an
alert on it reads zero rather than absent before any store exists; and two
stores sharing one ApiMetrics count into the same counter instead of racing
to register it.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone

from swarm_api import joinrequests, memberships
from swarm_api.joinrequests import JoinStore
from swarm_api.memberships import MembershipStore
from swarm_api.metrics import ApiMetrics

from .conftest import ENG_GROUP, api_settings
from .fakes import FakeFirestore

T0 = datetime(2026, 10, 25, 6, 41, tzinfo=timezone.utc)
LEE = "lee@example.com"
ALEX = "alex@example.com"
REGISTERED = {"eng": ENG_GROUP}
MEMBERSHIP = "swarm_api_membership_invalid_total"
JOIN_REQUEST = "swarm_api_join_request_invalid_total"


def settings() -> object:
    return api_settings(settings_key=secrets.token_hex(32), role_cache_seconds=15.0)


def count(metrics: ApiMetrics, name: str, reason: str) -> float:
    return metrics.registry.get_sample_value(name, {"reason": reason}) or 0.0


def families(metrics: ApiMetrics) -> dict[str, object]:
    return {family.name: family for family in metrics.registry.collect()}


def test_a_fresh_api_metrics_declares_both_counters_with_no_samples():
    metrics = ApiMetrics()
    found = families(metrics)
    # prometheus_client names a counter's family without its _total suffix.
    for name in (MEMBERSHIP, JOIN_REQUEST):
        family = found[name.removesuffix("_total")]
        assert family.type == "counter"
        assert [s for s in family.samples if s.name == name] == []
    assert metrics.membership_invalid._labelnames == ("reason",)
    assert metrics.join_request_invalid._labelnames == ("reason",)
    assert metrics.role_grant_invalid._labelnames == ("reason",)


def test_a_forged_membership_counts_on_the_api_metrics_counter():
    metrics, db = ApiMetrics(), FakeFirestore()
    store = MembershipStore(db, settings(), metrics=metrics, now=lambda: T0)
    FakeFirestore.collection(db, memberships.COLLECTION).document().set(
        {"tenant_id": "eng", "group": ENG_GROUP, "subject": LEE, "state": "member",
         "by": ALEX, "at": "2026-10-25T06:41:00.000000Z", "via": "join_request"})
    assert store.is_member(LEE, "eng", REGISTERED, fresh=True) is False
    assert count(metrics, MEMBERSHIP, "mac") == 1.0
    metrics.membership_invalid.labels(reason="mac").inc()
    assert count(metrics, MEMBERSHIP, "mac") == 2.0


def test_a_tampered_join_request_counts_on_the_api_metrics_counter():
    metrics, db = ApiMetrics(), FakeFirestore()
    store = JoinStore(db, settings(), metrics=metrics, now=lambda: T0)
    view, _created = store.ask(tenant_id="eng", group=ENG_GROUP, requester=LEE, message=None)
    (path,) = [p for p, d in db.dump(f"{joinrequests.REQUESTS}/").items()
               if p.count("/") == 1 and d.get("ref") == view.request.ref]
    db.document(path).update({"requester": "mallory@example.com"})
    assert store.open_for(["eng"]) == []
    seen = count(metrics, JOIN_REQUEST, "mac")
    assert seen >= 1.0
    metrics.join_request_invalid.labels(reason="mac").inc()
    assert count(metrics, JOIN_REQUEST, "mac") == seen + 1.0


def test_stores_sharing_one_api_metrics_register_nothing_twice():
    metrics, db = ApiMetrics(), FakeFirestore()
    first = MembershipStore(db, settings(), metrics=metrics)
    second = MembershipStore(db, settings(), metrics=metrics)
    JoinStore(db, settings(), metrics=metrics)
    JoinStore(db, settings(), metrics=metrics)
    assert first._invalid is second._invalid is metrics.membership_invalid
    names = [f.name for f in metrics.registry.collect()]
    assert len(names) == len(set(names))
