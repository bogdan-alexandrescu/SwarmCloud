"""An account's 429 is the account's, and every account keeps a health record (#2302).

Measured 2026-10-25: tenant eng's anthropic adaptive target went 26 -> 14 on
ONE pool account's 429 at 04:40:57Z, and the pool sat full with 100+ queued
while the other accounts had room. The worker reported per (provider, tenant)
only, and AIMD halved the tenant on every report.

Pinned here, through the broker's real app over the in-memory Firestore:

  * a 429 that names a pool account the caller HOLDS leaves the tenant's
    `adaptive_target` alone and lands on that account's health record;
  * a 429 that names no account halves the tenant, as before;
  * a 429 that names an account the caller does not hold halves the tenant
    and writes nothing on the account (no dodging the halving, no writing
    into somebody else's record);
  * when every account the tenant may draw from -- its own AND the ones lent
    to it -- is limited at once, the next account 429 halves the tenant;
  * the record is bounded (51 events keep 50) and carries no credential;
  * `GET /v1/accounts/{id}/health` serves 1 h / 24 h / 7 d counts and is
    refused to anyone but the platform; the account list carries the 24 h
    counts and the status;
  * the worker sends the held account (with the hold's proof) and sends none
    without one.
"""

from __future__ import annotations

import io
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

import pytest
from fastapi.testclient import TestClient

from agent_worker.accountlease import Assignment
from agent_worker.control import ControlPlane
from agent_worker.logs import build_logger
from swarm_common.models import ProviderState
from swarm_common.states import TaskState

from quota_broker import accountstore
from quota_broker.accounts import WindowReading
from quota_broker.accountstore import AccountStore
from quota_broker.main import create_app
from quota_broker.service import QuotaBroker
from quota_broker.settings import BrokerSettings

from .conftest import core_settings
from .fakes import FakeFirestore

ENG = "eng"
RESEARCH = "research"
PROVIDER = "anthropic"
FIRST = f"{ENG}:first"
SECOND = f"{ENG}:second"
LENT = f"{RESEARCH}:lent"
HARD_MAX = 40


def _token() -> str:
    """A credential-shaped value, built at runtime (CLAUDE.md: no literal)."""
    return "-".join(("sk", "ant", "oat01", secrets.token_hex(24)))


class _Identity:
    """Tenant eng's worker by default; `platform` makes it swarm-api."""

    def __init__(self) -> None:
        self.tenant: str | None = ENG
        self.platform = False

    def resolve(self, authorization):  # noqa: ANN001 - the broker's duck type
        return (None, True) if self.platform else (self.tenant, False)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _read(store: AccountStore, account_id: str, used: float = 0.2) -> None:
    """Give an account a fresh reading, so `choose()` hands it more than one agent."""
    later = _now() + timedelta(hours=3)
    store.record_reading(
        account_id,
        {"five_hour": WindowReading(used, later), "seven_day": WindowReading(used, later + timedelta(days=3))},
        _now(),
    )


@pytest.fixture()
def db() -> FakeFirestore:
    return FakeFirestore()


@pytest.fixture()
def store(db) -> AccountStore:
    s = AccountStore(db)
    s.register(ENG, "first")
    s.register(ENG, "second")
    _read(s, FIRST)
    _read(s, SECOND)
    return s


@pytest.fixture()
def broker(db) -> QuotaBroker:
    return QuotaBroker(
        db, settings=BrokerSettings(core=core_settings(), default_hard_max=HARD_MAX)
    )


@pytest.fixture()
def client(broker, store):
    identity = _Identity()
    c = TestClient(
        create_app(broker, identity=identity, account_store=store),
        raise_server_exceptions=False,
    )
    c.identity = identity
    return c


def _hold(client, task: str, *, exclude: tuple[str, ...] = ()) -> dict[str, Any]:
    r = client.post(
        "/v1/accounts/assign",
        json={"provider": PROVIDER, "task_id": task, "attempt_id": f"att-{task}",
              "exclude": list(exclude)},
    )
    assert r.status_code == 200 and r.json()["account_id"], r.text
    return {
        "account_id": r.json()["account_id"],
        "assignment_id": r.json()["assignment_id"],
        "task_id": task,
        "attempt_id": f"att-{task}",
    }


def _rate_limit(client, account: dict[str, Any] | None = None, **body: Any):
    if account is not None:
        body["account"] = account
    return client.post(f"/v1/quota/{PROVIDER}/{ENG}/rate-limit", json=body)


def _target(broker) -> int | None:
    return broker.get(PROVIDER, ENG).adaptive_target


# -- the tenant's target ---------------------------------------------------------


def test_a_429_naming_a_held_account_leaves_the_tenant_target_and_counts_on_the_account(
    client, broker, store
):
    held = _hold(client, "task_1")

    r = _rate_limit(client, held, retry_after_seconds=600, message="claude-code reported a provider rate limit")

    assert r.status_code == 200, r.text
    assert r.json()["scope"] == "account"
    assert r.json()["tenant_target_decreased"] is False
    assert _target(broker) == HARD_MAX, "an account's 429 must not halve the tenant"
    assert broker.get(PROVIDER, ENG).state is ProviderState.AVAILABLE, (
        "and must not put the whole tenant in a cooldown either"
    )
    events = store.health(held["account_id"])["events"]
    assert len(events) == 1
    assert events[0]["kind"] == "rate_limit"
    assert events[0]["tenant_id"] == ENG
    assert events[0]["task_id"] == "task_1"
    assert events[0]["message"] == "claude-code reported a provider rate limit"
    other = SECOND if held["account_id"] == FIRST else FIRST
    assert store.health(other) == {}, "the other account was not refused"


def test_a_429_naming_no_account_halves_the_tenant_as_before(client, broker, store):
    r = _rate_limit(client, retry_after_seconds=600)

    assert r.status_code == 200, r.text
    assert r.json()["scope"] == "tenant"
    assert _target(broker) == HARD_MAX // 2
    assert store.health(FIRST) == {} and store.health(SECOND) == {}


def test_a_429_naming_an_account_the_caller_does_not_hold_halves_and_records_nothing(
    client, broker, store
):
    held = _hold(client, "task_1")
    forged = {**held, "assignment_id": "asg-" + secrets.token_hex(8)}

    r = _rate_limit(client, forged, retry_after_seconds=600)

    assert r.status_code == 200, r.text
    assert r.json()["scope"] == "not_held"
    assert _target(broker) == HARD_MAX // 2, "naming an account is no way to dodge the halving"
    assert store.health(held["account_id"]) == {}, "and no way into an account's record"


def test_another_tenants_worker_cannot_name_eng_s_held_account(client, broker, store):
    held = _hold(client, "task_1")
    client.identity.tenant = RESEARCH

    r = client.post(f"/v1/quota/{PROVIDER}/{RESEARCH}/rate-limit",
                    json={"account": held, "retry_after_seconds": 60})

    assert r.status_code == 200, r.text
    assert r.json()["scope"] == "not_held"
    assert broker.get(PROVIDER, RESEARCH).adaptive_target == HARD_MAX // 2
    assert store.health(held["account_id"]) == {}


def test_when_every_account_the_tenant_may_use_is_limited_the_next_account_429_halves(
    client, broker
):
    first = _hold(client, "task_1")
    assert _rate_limit(client, first, retry_after_seconds=600).json()["scope"] == "account"
    assert _target(broker) == HARD_MAX

    second = _hold(client, "task_2", exclude=(first["account_id"],))
    assert second["account_id"] != first["account_id"]
    r = _rate_limit(client, second, retry_after_seconds=600)

    assert r.json()["scope"] == "pool_exhausted", r.text
    assert _target(broker) == HARD_MAX // 2, "the pool as a whole is spent: that is the tenant's limit"


def test_an_account_lent_to_the_tenant_with_room_keeps_the_target(db, broker):
    """`choose()`'s set is owned AND lent-to, so a loan with room is room."""
    store = AccountStore(db)
    store.register(ENG, "first")
    store.register(RESEARCH, "lent", lend_to=(ENG,))
    _read(store, FIRST)
    _read(store, LENT)
    identity = _Identity()
    client = TestClient(create_app(broker, identity=identity, account_store=store),
                        raise_server_exceptions=False)
    own = _hold(client, "task_1", exclude=(LENT,))
    assert own["account_id"] == FIRST

    assert _rate_limit(client, own, retry_after_seconds=600).json()["scope"] == "account"
    assert _target(broker) == HARD_MAX

    # The loan's readings now block it (S15): every account eng may use is limited.
    _read(store, LENT, used=0.99)
    r = _rate_limit(client, own, retry_after_seconds=600)
    assert r.json()["scope"] == "pool_exhausted", r.text
    assert _target(broker) == HARD_MAX // 2


def test_successes_still_climb_the_target_after_an_account_429(db, store):
    broker = QuotaBroker(
        db,
        settings=BrokerSettings(core=core_settings(), default_hard_max=HARD_MAX),
    )
    broker.set_hard_max(PROVIDER, ENG, HARD_MAX)
    broker.reset_adaptive_target(PROVIDER, ENG, target=10, reason="test", requested_by="",
                                 caller="", clear_cooldown=False)
    client = TestClient(create_app(broker, identity=_Identity(), account_store=store),
                        raise_server_exceptions=False)
    held = _hold(client, "task_1")
    assert _rate_limit(client, held, retry_after_seconds=60).json()["scope"] == "account"
    for _ in range(broker.config.success_threshold):
        assert client.post(f"/v1/quota/{PROVIDER}/{ENG}/success", json={}).status_code == 200

    assert _target(broker) == 11, "the success run was not broken by the account's 429"
    assert _target(broker) <= HARD_MAX


# -- the record ------------------------------------------------------------------


def test_the_record_keeps_the_newest_50_events(store):
    for n in range(accountstore.HEALTH_EVENTS_MAX + 1):
        assert store.record_health(FIRST, "network", tenant_id=ENG, task_id=f"task_{n}")

    events = store.health(FIRST)["events"]
    assert len(events) == accountstore.HEALTH_EVENTS_MAX
    assert events[0]["task_id"] == "task_1", "the oldest (task_0) was dropped"
    assert events[-1]["task_id"] == f"task_{accountstore.HEALTH_EVENTS_MAX}"
    hourly = store.health(FIRST)["hourly"]
    assert sum(c.get("network", 0) for c in hourly.values()) == accountstore.HEALTH_EVENTS_MAX + 1, (
        "the hourly counts still count every event"
    )


def test_a_message_is_one_short_line(store):
    store.record_health(FIRST, "auth", message="first\nsecond\x00third " + "x" * 500)

    message = store.health(FIRST)["events"][0]["message"]
    assert "\n" not in message and "\x00" not in message
    assert len(message) == accountstore.HEALTH_MESSAGE_MAX


def test_an_unknown_kind_is_not_recorded(store):
    assert store.record_health(FIRST, "mystery") is False
    assert store.health(FIRST) == {}


def test_the_worker_redacts_the_message_before_the_broker_sees_it(client, db, store):
    held = _hold(client, "task_1")
    token = _token()
    transport = _BrokerOverHttp(client)
    worker = _worker(db, transport, task_id="task_1", attempt_id="att-task_1")

    assert worker.update_quota_state(
        provider=PROVIDER,
        state=ProviderState.EXHAUSTED,
        retry_after_seconds=600,
        account=_assignment(held),
        message=f"refused with Authorization: Bearer {token}",
    ) is True

    sent = transport.bodies[-1]
    assert token not in str(sent)
    stored = store.health(held["account_id"])["events"][0]["message"]
    assert token not in stored
    assert stored, "a redacted message is still a message"


def test_a_refusal_at_assignment_is_recorded_without_the_secret_manager_text(client, store):
    held = _hold(client, "task_1")
    detail = "PermissionDenied: projects/p/secrets/swarm-account-eng--first"

    r = client.post(f"/v1/accounts/{held['account_id']}/release",
                    json={"assignment_id": held["assignment_id"], "unusable": detail})

    assert r.status_code == 200, r.text
    event = store.health(held["account_id"])["events"][0]
    assert event["kind"] == "assign_refused"
    assert event["tenant_id"] == ENG
    assert "swarm-account" not in event["message"]


def test_a_re_auth_mark_and_a_failed_refresh_are_recorded(store):
    store.record_refresh(FIRST, at=_now(), reason="refresh_failed", expires_at=None)
    store.mark_reauth_required(FIRST, "refresh failed: reauth_required")

    kinds = [e["kind"] for e in store.health(FIRST)["events"]]
    assert kinds == ["refresh_failed", "reauth"]
    assert accountstore.health_status(store.get(FIRST), store.health(FIRST), _now()) == "reauth_needed"


def test_three_errors_in_an_hour_make_an_account_erroring(store):
    for _ in range(2):
        store.record_health(FIRST, "overloaded", tenant_id=ENG)
    assert accountstore.health_status(store.get(FIRST), store.health(FIRST), _now()) == "healthy"

    store.record_health(FIRST, "server_error", tenant_id=ENG)
    assert accountstore.health_status(store.get(FIRST), store.health(FIRST), _now()) == "erroring"


def test_a_provider_error_report_is_health_only(client, broker, store):
    held = _hold(client, "task_1")

    r = client.post(f"/v1/quota/{PROVIDER}/{ENG}/account-error",
                    json={"account": held, "kind": "auth", "message": "credential refused"})

    assert r.status_code == 200, r.text
    assert store.health(held["account_id"])["events"][0]["kind"] == "auth"
    assert _target(broker) == HARD_MAX


def test_a_provider_error_report_on_an_account_not_held_is_refused(client, store):
    held = _hold(client, "task_1")
    forged = {**held, "attempt_id": "att-other"}

    r = client.post(f"/v1/quota/{PROVIDER}/{ENG}/account-error",
                    json={"account": forged, "kind": "network"})

    assert r.status_code == 403, r.text
    assert store.health(held["account_id"]) == {}


def test_a_provider_error_of_an_unknown_kind_is_refused(client):
    held = _hold(client, "task_1")

    r = client.post(f"/v1/quota/{PROVIDER}/{ENG}/account-error",
                    json={"account": held, "kind": "rate_limit"})

    assert r.status_code == 422, r.text


# -- serving it ------------------------------------------------------------------


def test_the_health_route_serves_counts_by_window_for_the_platform(client, store):
    held = _hold(client, "task_1")
    _rate_limit(client, held, retry_after_seconds=600)
    store.record_health(held["account_id"], "network", tenant_id=ENG, task_id="task_1")
    client.identity.platform = True

    r = client.get(f"/v1/accounts/{held['account_id']}/health")

    assert r.status_code == 200, r.text
    body = r.json()
    for span in ("last_1h", "last_24h", "last_7d"):
        assert body["counts"][span]["rate_limit"] == 1
        assert body["counts"][span]["network"] == 1
    assert body["status"] == "limited"
    assert len(body["hourly"]) == 7 * 24
    assert body["hourly"][-1]["total"] == 2
    assert [e["kind"] for e in body["events"]] == ["network", "rate_limit"], "newest first"


def test_the_health_route_refuses_a_tenant_caller(client):
    held = _hold(client, "task_1")

    r = client.get(f"/v1/accounts/{held['account_id']}/health")

    assert r.status_code == 403, r.text


def test_the_account_list_carries_the_24h_counts_and_the_status(client, store):
    held = _hold(client, "task_1")
    _rate_limit(client, held, retry_after_seconds=600)

    rows = {a["account_id"]: a for a in client.get("/v1/accounts").json()["accounts"]}

    assert rows[held["account_id"]]["health_24h"]["rate_limit"] == 1
    assert rows[held["account_id"]]["health_status"] == "limited"
    other = SECOND if held["account_id"] == FIRST else FIRST
    assert rows[other]["health_24h"]["rate_limit"] == 0
    assert rows[other]["health_status"] == "healthy"
    assert "events" not in rows[other], "a row carries counts, never an event"


# -- the worker ------------------------------------------------------------------


class _BrokerOverHttp:
    """`control.QuotaReporter`, sending each report to the broker's real app."""

    def __init__(self, client: TestClient) -> None:
        self._client = client
        self.bodies: list[dict[str, Any]] = []
        self.routes: list[str] = []

    def report(
        self, *, provider: str, tenant_id: str, route: str, body: Mapping[str, Any]
    ) -> None:
        self.bodies.append(dict(body))
        self.routes.append(route)
        response = self._client.post(f"/v1/quota/{provider}/{tenant_id}/{route}", json=dict(body))
        if response.status_code != 200:
            raise RuntimeError(f"broker answered {response.status_code}: {response.text}")


def _worker(db, transport, *, task_id: str, attempt_id: str) -> ControlPlane:
    lease_id = f"lease-{attempt_id}"
    db.document(f"tasks/{task_id}").set(
        {"tenant_id": ENG, "state": TaskState.RUNNING.value, "current_generation": 1}
    )
    db.document(f"leases/{lease_id}").set({"tenant_id": ENG, "released_at": None})
    return ControlPlane(
        db,
        task_id=task_id,
        attempt_id=attempt_id,
        lease_id=lease_id,
        tenant_id=ENG,
        generation=1,
        logger=build_logger(task_id=task_id, attempt_id=attempt_id, tenant_id=ENG,
                            generation=1, runner_profile="claude-code", stream=io.StringIO()),
        quota_reporter=transport,
    )


def _assignment(held: dict[str, Any]) -> Assignment:
    return Assignment(account_id=held["account_id"], secret="swarm-account-name",
                      assignment_id=held["assignment_id"])


def test_the_worker_sends_the_held_account_and_its_proof(client, db, broker, store):
    held = _hold(client, "task_1")
    transport = _BrokerOverHttp(client)
    worker = _worker(db, transport, task_id="task_1", attempt_id="att-task_1")

    assert worker.update_quota_state(
        provider=PROVIDER, state=ProviderState.EXHAUSTED, retry_after_seconds=600,
        account=_assignment(held),
    ) is True

    assert transport.bodies[-1]["account"] == held
    assert _target(broker) == HARD_MAX
    assert store.health(held["account_id"])["events"][0]["task_id"] == "task_1"


def test_the_worker_sends_no_account_without_one(client, db, broker):
    transport = _BrokerOverHttp(client)
    worker = _worker(db, transport, task_id="task_1", attempt_id="att-task_1")

    assert worker.update_quota_state(
        provider=PROVIDER, state=ProviderState.EXHAUSTED, retry_after_seconds=600,
    ) is True

    assert "account" not in transport.bodies[-1]
    assert "message" not in transport.bodies[-1]
    assert _target(broker) == HARD_MAX // 2


def test_a_success_never_names_an_account(client, db):
    held = _hold(client, "task_1")
    transport = _BrokerOverHttp(client)
    worker = _worker(db, transport, task_id="task_1", attempt_id="att-task_1")

    worker.update_quota_state(provider=PROVIDER, state=ProviderState.AVAILABLE,
                              account=_assignment(held))

    assert transport.routes == ["success"]
    assert transport.bodies[-1] == {}


def test_the_worker_reports_an_auth_error_on_the_held_account(client, db, store):
    held = _hold(client, "task_1")
    transport = _BrokerOverHttp(client)
    worker = _worker(db, transport, task_id="task_1", attempt_id="att-task_1")

    assert worker.report_account_error(
        provider=PROVIDER, account=_assignment(held), kind="auth",
        message="the provider refused the account's credential",
    ) is True

    assert transport.routes == ["account-error"]
    assert store.health(held["account_id"])["events"][0]["kind"] == "auth"
