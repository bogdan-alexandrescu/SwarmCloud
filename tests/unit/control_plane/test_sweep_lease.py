"""Only one quota sweep exchanges refresh tokens at a time.

docs/audits/2026-09-18/06-quota-broker-accounts.md §3. Cloud Scheduler retries a
tick it thinks timed out WITHOUT cancelling the first request, the broker takes
dozens of concurrent requests per instance, and it can run more than one
instance. Two sweeps then read the same refresh token; one exchanges it and the
provider rotates it; the other presents the token that was just rotated away
and is told `invalid_grant` -- about a perfectly healthy account.

`_sweep_account_pool` already confirms a dead credential before writing
REAUTH_REQUIRED, which makes the race harmless THERE. It did nothing about the
exchanges themselves: every account the losing sweep touched spent a call
against the token endpoint's shared rate limit to rediscover a rotation, and
the subscription-tenant sweep has no confirmation step at all.

The fix is a lease on the credential phase of the sweep, in Firestore, so it
holds across instances. The quota recompute and the hold backstop are NOT
under it: both are idempotent, and they are what un-parks throttled tenants, so
a stuck credential sweep must never be able to delay them.

The frozen `swarm_common.models.Lease` is not used and does not need changing:
it is a TASK's claim on capacity (task, attempt, pools, units, generation), and
writing a sweep lock into the `leases` collection would put a document the
reconciler reads as a task lease into its hands.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi.testclient import TestClient

from quota_broker.credentials import RefreshOutcome
from quota_broker.main import WorkerIdentity, create_app

#: Where the lease lives. Stated here the way every other test in this
#: directory states a document path, so a move is a visible, deliberate change.
LEASE_DOC = "sweep_leases/quota-sweep"


def _account(account_id: str):
    from quota_broker.accounts import Account

    owner, label = account_id.split(":", 1)
    return Account(account_id=account_id, owner_tenant=owner, label=label)


class _Store:
    """An account store with three registered accounts and nothing else."""

    def __init__(self, ids=("eng:a", "eng:b", "eng:c")) -> None:
        self._ids = ids

    def list(self):
        return [_account(i) for i in self._ids]

    def secret_for(self, account):
        return f"swarm-account-{account.account_id}"

    def get(self, account_id):
        return None

    def record_reading(self, *args, **kwargs):
        return None


class _Refresher:
    """Records every exchange. Honours `keep_going` the way the real one must."""

    def __init__(self, *, block: threading.Event | None = None,
                 entered: threading.Event | None = None,
                 on_account: Any = None) -> None:
        self.tenant_sweeps: list[Any] = []
        self.exchanged: list[str] = []
        self._block = block
        self._entered = entered
        self._on_account = on_account

    def sweep(self, pairs, keep_going=None):
        self.tenant_sweeps.append(list(pairs))
        if self._entered is not None:
            self._entered.set()
        if self._block is not None:
            self._block.wait(10)
        return []

    def sweep_accounts(self, secrets, keep_going=None):
        out = []
        for _base, label in secrets:
            if keep_going is not None and not keep_going():
                break
            self.exchanged.append(label)
            out.append(RefreshOutcome(label, "account", True, "refreshed"))
            if self._on_account is not None:
                self._on_account(label)
        return out

    def refresh_secret(self, base, *, label=""):
        return RefreshOutcome(label, "account", True, "refreshed")


def _client(broker, refresher, store=None) -> TestClient:
    app = create_app(
        broker,
        identity=WorkerIdentity(required=False),
        credential_refresher=refresher,
        subscription_tenants=lambda: [("eng", "anthropic")],
        account_store=store or _Store(),
    )
    # The usage poll reaches the network and is not what this file is about.
    app.state.usage_poller = None
    return TestClient(app, raise_server_exceptions=False)


def _held_elsewhere(db, *, expires_in: timedelta) -> None:
    now = datetime.now(timezone.utc)
    db.docs[LEASE_DOC] = {
        "holder": "another-broker-instance",
        "acquired_at": now - timedelta(seconds=10),
        "renewed_at": now - timedelta(seconds=10),
        "expires_at": now + expires_in,
        "released_at": None,
        "generation": 7,
    }


# ---------------------------------------------------------------------------
# the race, as the audit described it
# ---------------------------------------------------------------------------


def test_a_second_broker_does_not_exchange_while_the_first_is_sweeping(broker):
    """Two instances, one database, overlapping ticks. Name-independent: this
    asserts only on which refresher was called."""
    entered, release = threading.Event(), threading.Event()
    first = _Refresher(block=release, entered=entered)
    second = _Refresher()
    responses: dict[str, Any] = {}

    def run_first() -> None:
        responses["first"] = _client(broker, first).post("/v1/quota/sweep")

    thread = threading.Thread(target=run_first)
    thread.start()
    try:
        assert entered.wait(10), "the first sweep never reached its credential phase"
        responses["second"] = _client(broker, second).post("/v1/quota/sweep")
    finally:
        release.set()
        thread.join(15)

    assert responses["second"].status_code == 200
    assert second.tenant_sweeps == [], "the second sweep refreshed tenant credentials"
    assert second.exchanged == [], "the second sweep exchanged account refresh tokens"
    assert responses["second"].json()["sweep_lease"]["acquired"] is False

    assert responses["first"].status_code == 200
    assert first.exchanged == ["eng:a", "eng:b", "eng:c"]


def test_a_live_lease_held_elsewhere_skips_only_the_credential_phase(broker, db):
    """The quota recompute and the hold backstop still run: they un-park
    throttled tenants and must never wait on a credential sweep."""
    _held_elsewhere(db, expires_in=timedelta(seconds=90))
    refresher = _Refresher()

    response = _client(broker, refresher).post("/v1/quota/sweep")

    assert response.status_code == 200
    body = response.json()
    assert refresher.tenant_sweeps == [] and refresher.exchanged == []
    assert body["sweep_lease"]["acquired"] is False
    assert body["sweep_lease"]["held_by"] == "another-broker-instance"
    assert "examined" in body, "the quota recompute must still have run"
    assert "holds" in body, "the hold backstop must still have run"
    assert "credentials" not in body and "accounts" not in body


def test_a_sweep_that_loses_its_lease_stops_before_the_next_exchange(broker, db):
    """The lease is renewed before every exchange, and a renewal that finds
    somebody else holding it ends the sweep there. Taken over here by writing
    the document directly, which is what another instance does once this one's
    lease has expired."""
    def steal(_label: str) -> None:
        _held_elsewhere(db, expires_in=timedelta(seconds=90))

    refresher = _Refresher(on_account=steal)

    body = _client(broker, refresher).post("/v1/quota/sweep").json()

    assert refresher.exchanged == ["eng:a"], (
        "the sweep kept exchanging refresh tokens after another holder took the lease"
    )
    assert body["sweep_lease"]["lost"] is True
    # And it did not release a lease it no longer held.
    assert db.docs[LEASE_DOC]["holder"] == "another-broker-instance"


def test_a_lease_that_cannot_be_taken_fails_closed(broker, db, monkeypatch):
    """No proof of exclusion, no exchange. Skipping one tick costs five minutes
    of a token with hours left; an unexcluded exchange can cost the token."""
    def broken(**_kwargs: Any) -> Any:
        raise RuntimeError("firestore is unavailable")

    monkeypatch.setattr(db, "transaction", broken)
    refresher = _Refresher()

    response = _client(broker, refresher).post("/v1/quota/sweep")

    assert response.status_code == 200
    assert refresher.tenant_sweeps == [] and refresher.exchanged == []
    assert response.json()["sweep_lease"]["error"] == "RuntimeError"


# ---------------------------------------------------------------------------
# the lease must not become the outage
# ---------------------------------------------------------------------------
# These pass without the lease too. They are here because a lease that is
# never released, or never expires, turns "one sweep at a time" into "no
# sweep ever again", which is worse than the race it closes.


def test_the_lease_is_released_so_the_next_tick_sweeps(broker, db):
    refresher = _Refresher()
    client = _client(broker, refresher)

    client.post("/v1/quota/sweep")
    client.post("/v1/quota/sweep")

    assert refresher.exchanged == ["eng:a", "eng:b", "eng:c"] * 2
    assert len(refresher.tenant_sweeps) == 2


def test_an_expired_lease_left_by_a_dead_sweep_is_taken_over(broker, db):
    """An instance killed mid-sweep never releases. Its lease expires instead."""
    _held_elsewhere(db, expires_in=timedelta(seconds=-1))
    refresher = _Refresher()

    body = _client(broker, refresher).post("/v1/quota/sweep").json()

    assert refresher.exchanged == ["eng:a", "eng:b", "eng:c"]
    assert body["sweep_lease"]["acquired"] is True


def test_a_deployment_with_no_refresher_takes_no_lease(broker, db, monkeypatch):
    """Nothing to serialise, nothing written: API-key-only deployments keep the
    exact response shape they had."""
    monkeypatch.setenv("CREDENTIAL_REFRESH_ENABLED", "false")
    client = TestClient(
        create_app(broker, identity=WorkerIdentity(required=False)),
        raise_server_exceptions=False,
    )

    body = client.post("/v1/quota/sweep").json()

    assert "sweep_lease" not in body
    assert LEASE_DOC not in db.docs


# ---------------------------------------------------------------------------
# the real refresher and poller honour the stop
# ---------------------------------------------------------------------------


def test_the_real_refresher_exchanges_nothing_once_told_to_stop():
    from quota_broker.credentials import CredentialRefresher

    class _Secrets:
        def __init__(self) -> None:
            self.reads: list[str] = []

        def access(self, name: str) -> str:
            self.reads.append(name)
            raise KeyError(name)

        def add_version(self, name: str, payload: str) -> None:
            raise AssertionError("nothing may be written after the lease is lost")

    class _Endpoint:
        def exchange(self, *args: Any, **kwargs: Any) -> Any:
            raise AssertionError("no exchange may happen after the lease is lost")

    import logging

    secrets = _Secrets()
    refresher = CredentialRefresher(secrets, _Endpoint(), logger=logging.getLogger("t"))

    assert refresher.sweep_accounts([("s1", "eng:a"), ("s2", "eng:b")],
                                    keep_going=lambda: False) == []
    assert refresher.sweep([("eng", "anthropic")], keep_going=lambda: False) == []
    assert secrets.reads == []


def test_the_real_usage_poller_polls_nothing_once_told_to_stop():
    import logging

    from quota_broker.usagepoll import UsagePoller

    def fetch(_token: str) -> Any:
        raise AssertionError("no poll may happen after the lease is lost")

    class _Secrets:
        def access(self, name: str) -> str:
            raise AssertionError("no token may be read after the lease is lost")

    poller = UsagePoller(_Secrets(), _Store(), logger=logging.getLogger("t"), fetch=fetch)
    assert poller.poll_round([_account("eng:a")], keep_going=lambda: False) == []
