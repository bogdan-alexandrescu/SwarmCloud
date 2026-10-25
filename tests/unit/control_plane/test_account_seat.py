"""One subscription is one SEAT, not one organisation (#2282, POOL-SEAT-ID).

POOL-DUP-DETECT (#2222, PR 2247) keyed pool-account duplicates on the
provider organisation alone. That is right for a personal plan -- one
organisation per person -- and wrong for a Claude Team organisation, where
every seat has its own limits. Measured 2026-10-25 ~03:50Z: eng:saga-team
(7-day 61%), eng:aieng-claude1 (7-day 59%), eng:ai-eng-3 and eng:devops-main
were withheld as `same_subscription_as: ai-eng-2` (7-day 97%), with 104 tasks
queued behind 8 running agents. One subscription cannot read 61% and 97% in
the same week.

BGD, 2026-10-25, "Seat-level fix lane, top priority":

  * two pool accounts are one subscription only when they share the
    organisation AND the login seat, or -- for an account whose seat is not
    recorded yet -- when their readings are identical (`readings_identical`);
  * readings that differ are proof of two quotas, and never withhold;
  * a second Team seat registers; the same seat again is refused;
  * the seat identity is recorded from the sign-in, at add and at every
    refresh, opaque, and never logged.

Unit tests: the fake Firestore and fake endpoints. Every organisation id,
login and token is built at runtime.
"""

from __future__ import annotations

import json
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from quota_broker.accounts import (
    Account,
    WindowReading,
    account_id_for,
    choose,
    duplicates,
)
from quota_broker.accountstore import AccountStore
from quota_broker.main import _sweep_account_pool
from quota_broker.oauth import (
    Credential,
    Organisation,
    organisation_from_payload,
    organisation_identity,
    read_organisation,
    refresh,
    seat_identity,
)
from scheduler.loop import pool_blockers

ENG = "eng"
RESEARCH = "research"
NOW = datetime(2026, 10, 25, 4, 0, tzinfo=timezone.utc)
FIVE_RESET = NOW + timedelta(hours=2, minutes=13)
WEEK_RESET = NOW + timedelta(days=2, hours=5)


def _uuid() -> str:
    return str(uuid.uuid4())


def _login() -> dict:
    return {"uuid": _uuid(), "email_address": f"{secrets.token_hex(4)}@example.test"}


def _token_payload(org_id: str, *, login: dict | None = None, name: str = "Saga",
                   kind: str | None = "claude_team") -> dict:
    org = {"uuid": org_id, "name": name}
    if kind is not None:
        org["organization_type"] = kind
    payload = {
        "access_token": "at-" + secrets.token_hex(16),
        "refresh_token": "rt-" + secrets.token_hex(16),
        "expires_in": 28800,
        "organization": org,
    }
    if login is not None:
        payload["account"] = login
    return payload


def _acct(label, *, org, seat=None, registered, week=0.40, five=0.05,
          owner=ENG) -> Account:
    return Account(
        account_id=account_id_for(owner, label),
        owner_tenant=owner,
        label=label,
        windows={"five_hour": WindowReading(five, FIVE_RESET),
                 "seven_day": WindowReading(week, WEEK_RESET)},
        observed_at=NOW,
        organisation_id=org,
        seat_id=seat,
        registered_at=registered,
    )


# --------------------------------------------------------------------------
# What the provider says about the seat, and what is kept of it
# --------------------------------------------------------------------------


def test_the_seat_is_the_login_uuid_hashed_never_the_uuid_or_the_email():
    login = _login()
    found = organisation_from_payload(_token_payload(_uuid(), login=login))
    assert found is not None
    # MUTATION: leaving `account` unread records no seat, and every Team seat reads as one.
    assert found.seat == seat_identity(login["uuid"])
    assert login["uuid"] not in repr(found) and login["email_address"] not in repr(found)
    # A seat identity is never mistaken for an organisation identity of the same uuid.
    assert seat_identity(login["uuid"]) != organisation_identity(login["uuid"])
    # An answer with no login, or a login without a uuid, records no seat.
    assert organisation_from_payload(_token_payload(_uuid())).seat is None
    assert organisation_from_payload(
        _token_payload(_uuid(), login={"email_address": login["email_address"]})).seat is None


def test_the_profile_fills_the_seat_the_token_response_left_out():
    org_id, login = _uuid(), _login()
    token = _token_payload(org_id)  # plan named, no login
    endpoint_answer = {"organization": {"uuid": org_id, "name": "Saga",
                                        "organization_type": "claude_team"},
                       "account": login}

    class _Profiled:
        calls = 0

        def profile(self, access_token):
            _Profiled.calls += 1
            return endpoint_answer

    found = read_organisation(token, _Profiled(), token["access_token"])
    # MUTATION: returning early once the plan is known never asks for the seat.
    assert _Profiled.calls == 1
    assert found.seat == seat_identity(login["uuid"])


def test_every_refresh_carries_the_seat_it_ran_on():
    org_id, login = _uuid(), _login()

    class _Exchange:
        def exchange(self, refresh_token):
            return _token_payload(org_id, login=login)

    fresh = refresh(Credential("a", "r", NOW), _Exchange(), now=NOW)
    assert fresh.organisation == Organisation(
        organisation_identity(org_id), "Saga", "Team", seat_identity(login["uuid"]))


# --------------------------------------------------------------------------
# Which accounts are one subscription
# --------------------------------------------------------------------------


def test_two_seats_of_one_organisation_with_different_readings_are_both_handed_out():
    """The 03:50Z reading had ai-eng-2 at 97% of its week, saga-team at 61%;
    here the older seat still has room, so `choose()` can offer both."""
    team = organisation_identity(_uuid())
    kept = _acct("ai-eng-2", org=team, seat=seat_identity(_uuid()),
                 registered=NOW - timedelta(days=5), week=0.70, five=0.41)
    seat = _acct("saga-team", org=team, seat=seat_identity(_uuid()),
                 registered=NOW - timedelta(days=1), week=0.61, five=0.0)
    # MUTATION: grouping on the organisation alone withholds saga-team.
    assert duplicates([kept, seat]) == {}
    first = choose([kept, seat], ENG, NOW)
    second = choose([kept, seat], ENG, NOW, exclude=[first.account_id])
    assert {first.label, second.label} == {"ai-eng-2", "saga-team"}


def test_two_seats_with_identical_readings_are_still_two_subscriptions():
    team = organisation_identity(_uuid())
    a = _acct("a", org=team, seat=seat_identity(_uuid()), registered=NOW - timedelta(days=2))
    b = _acct("b", org=team, seat=seat_identity(_uuid()), registered=NOW - timedelta(days=1))
    assert duplicates([a, b]) == {}


def test_one_seat_registered_twice_withholds_the_newer_whatever_its_readings():
    team, seat = organisation_identity(_uuid()), seat_identity(_uuid())
    older = _acct("older", org=team, seat=seat, registered=NOW - timedelta(days=3), week=0.70)
    newer = _acct("newer", org=team, seat=seat, registered=NOW - timedelta(hours=1), week=0.10)
    # The over-admission guard POOL-DUP-DETECT exists for: one seat, handed out once.
    assert duplicates([older, newer]) == {
        "eng:older": ("eng:newer", False), "eng:newer": ("eng:older", True)}
    assert choose([older, newer], ENG, NOW).label == "older"
    assert choose([older, newer], ENG, NOW, exclude=["eng:older"]) is None


def test_one_seat_on_two_organisations_is_two_subscriptions():
    seat = seat_identity(_uuid())
    a = _acct("personal", org=organisation_identity(_uuid()), seat=seat,
              registered=NOW - timedelta(days=2))
    b = _acct("team", org=organisation_identity(_uuid()), seat=seat,
              registered=NOW - timedelta(days=1))
    assert duplicates([a, b]) == {}


def test_no_seat_recorded_and_different_readings_withholds_neither():
    """Accounts registered before the seat was stored, until their next refresh."""
    team = organisation_identity(_uuid())
    a = _acct("ai-eng-2", org=team, registered=NOW - timedelta(days=5), week=0.97)
    b = _acct("aieng-claude1", org=team, registered=NOW - timedelta(days=1), week=0.59)
    # MUTATION: pairing seatless accounts on the organisation alone withholds aieng-claude1.
    assert duplicates([a, b]) == {}


def test_no_seat_recorded_and_identical_readings_withholds_the_newer():
    """DUP-1's fallback (BGD 2026-10-24), unchanged."""
    team = organisation_identity(_uuid())
    a = _acct("saga-personal", org=team, registered=NOW - timedelta(days=5))
    b = _acct("team", org=team, registered=NOW - timedelta(days=1))
    assert duplicates([a, b]) == {
        "eng:saga-personal": ("eng:team", False), "eng:team": ("eng:saga-personal", True)}
    # One side's seat recorded, the other not yet: the readings still decide.
    c = _acct("team", org=team, seat=seat_identity(_uuid()), registered=NOW - timedelta(days=1))
    assert duplicates([a, c])["eng:team"] == ("eng:saga-personal", True)
    d = _acct("team", org=team, seat=seat_identity(_uuid()), registered=NOW - timedelta(days=1),
              week=0.12)
    assert duplicates([a, d]) == {}


def test_a_seat_registered_again_behind_a_readings_pair_is_withheld_against_its_root():
    """Over-admission guard: K (no seat) pairs with B (seat s1) by readings; a
    later C on seat s1 reads differently from K, and is still one subscription
    with B, so it is withheld against K, the one handed out."""
    team, seat = organisation_identity(_uuid()), seat_identity(_uuid())
    k = _acct("k", org=team, registered=NOW - timedelta(days=3))
    b = _acct("b", org=team, seat=seat, registered=NOW - timedelta(days=2))
    c = _acct("c", org=team, seat=seat, registered=NOW - timedelta(days=1), week=0.12)
    # MUTATION: comparing a newer account only with kept ones hands out K and C both.
    assert duplicates([k, b, c]) == {
        "eng:k": ("eng:b", False), "eng:b": ("eng:k", True), "eng:c": ("eng:k", True)}
    assert choose([k, b, c], ENG, NOW, exclude=["eng:k"]) is None


def test_two_idle_seatless_accounts_on_one_organisation_are_not_a_pair():
    team = organisation_identity(_uuid())
    a = _acct("a", org=team, registered=NOW - timedelta(days=2), week=0.0, five=0.0)
    b = _acct("b", org=team, registered=NOW - timedelta(days=1), week=0.0, five=0.0)
    assert duplicates([a, b]) == {}


def test_pool_blockers_stops_naming_an_unpaired_seat_withheld():
    team = organisation_identity(_uuid())
    kept = _acct("ai-eng-2", org=team, seat=seat_identity(_uuid()),
                 registered=NOW - timedelta(days=5), week=0.97)
    seat = _acct("saga-team", org=team, seat=seat_identity(_uuid()),
                 registered=NOW - timedelta(days=1), week=0.61)
    blockers = {b["account"]: b for b in pool_blockers([kept, seat], NOW, ENG)}
    # MUTATION: the organisation key names saga-team `same_subscription_as: ai-eng-2`.
    assert "withheld" not in blockers["saga-team"]
    assert "withheld" not in blockers["ai-eng-2"]
    # A real pair is still named, as today.
    again = _acct("again", org=team, seat=kept.seat_id, registered=NOW - timedelta(hours=1))
    blockers = {b["account"]: b for b in pool_blockers([kept, again], NOW, ENG)}
    assert blockers["again"]["withheld"] == {"same_subscription_as": "ai-eng-2"}


# --------------------------------------------------------------------------
# The store and the sweep
# --------------------------------------------------------------------------


class _NoRefresher:
    def sweep_accounts(self, secrets, *, keep_going=None, held=()):
        return []


def test_the_sweep_clears_a_withheld_mark_the_seats_no_longer_support(broker):
    clock = [NOW - timedelta(days=5), NOW - timedelta(days=1)]
    store = AccountStore(broker.db, now=lambda: clock.pop(0) if clock else NOW)
    store.register(ENG, "ai-eng-2")
    store.register(ENG, "saga-team")
    org_id = _uuid()
    store.record_organisation("eng:ai-eng-2",
                              organisation_from_payload(_token_payload(org_id, login=_login())))
    store.record_organisation("eng:saga-team",
                              organisation_from_payload(_token_payload(org_id, login=_login())))
    # The marks POOL-DUP-DETECT's sweep wrote on the organisation alone.
    store.set_duplicate("eng:ai-eng-2", "eng:saga-team", withheld=False)
    store.set_duplicate("eng:saga-team", "eng:ai-eng-2", withheld=True)

    result = _sweep_account_pool(_NoRefresher(), store, NOW)

    by_id = {a.account_id: a for a in store.list()}
    # MUTATION: keeping the organisation key leaves saga-team withheld.
    assert {(a.duplicate_of, a.duplicate_withheld) for a in by_id.values()} == {(None, False)}
    assert result["duplicates"] == []


def test_a_refresh_records_the_seat_and_one_that_cannot_say_never_erases_it(broker):
    store = AccountStore(broker.db)
    store.register(ENG, "saga-team")
    org_id, login = _uuid(), _login()
    store.record_refresh("eng:saga-team", at=NOW, reason="refreshed", expires_at=None,
                         organisation=organisation_from_payload(
                             _token_payload(org_id, login=login)))
    assert store.get("eng:saga-team").seat_id == seat_identity(login["uuid"])
    store.record_refresh("eng:saga-team", at=NOW, reason="refreshed", expires_at=None,
                         organisation=organisation_from_payload(_token_payload(org_id)))
    assert store.get("eng:saga-team").seat_id == seat_identity(login["uuid"])
    # Re-registering (a change of loans) carries the seat over.
    store.register(ENG, "saga-team", lend_to=[RESEARCH])
    assert store.get("eng:saga-team").seat_id == seat_identity(login["uuid"])


# --------------------------------------------------------------------------
# Registration, over HTTP
# --------------------------------------------------------------------------


class _Secrets:
    def __init__(self):
        self.versions, self.created = {}, {}

    def ensure_secret(self, name, *, labels, accessors, region):
        self.created[name] = dict(labels)
        return True

    def set_worker_readers(self, name, *, readers, manages, revoke=True):
        return [], []

    def add_version(self, name, payload):
        if name not in self.created:
            raise KeyError(name)
        self.versions.setdefault(name, []).append(payload)


class _Endpoint:
    def __init__(self):
        self.next = None

    def redeem(self, *, code, verifier, redirect_uri, state=""):
        return dict(self.next)


class _Identity:
    def resolve(self, authorization):
        return None, True


@pytest.fixture()
def broker_app(broker, monkeypatch):
    from quota_broker.main import create_app

    monkeypatch.setenv("REGION", "us-central1")
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv("BROKER_SERVICE_ACCOUNT", "swarm-quota-broker@p.iam.gserviceaccount.com")
    monkeypatch.setenv("WORKER_SERVICE_ACCOUNT_TEMPLATE",
                       "swarm-agent-worker-{tenant}@p.iam.gserviceaccount.com")
    app = create_app(broker, identity=_Identity(), account_store=AccountStore(broker.db))
    app.state.secret_store = _Secrets()
    app.state.token_endpoint = _Endpoint()
    return app


@pytest.fixture()
def broker_client(broker_app):
    return TestClient(broker_app, raise_server_exceptions=False)


def _add(client, app, payload, *, owner=ENG, label="team"):
    app.state.token_endpoint.next = payload
    r = client.post("/v1/accounts/authorize", json={"owner_tenant": owner, "label": label})
    assert r.status_code == 200, r.text
    return client.post("/v1/accounts/exchange",
                       json={"state": r.json()["state"], "code": "code-" + secrets.token_hex(4),
                             "expected_owner": owner})


def test_a_second_team_seat_registers_and_is_handed_out(broker_app, broker_client, broker):
    org_id = _uuid()
    assert _add(broker_client, broker_app, _token_payload(org_id, login=_login()),
                label="ai-eng-2").status_code == 201
    second = _add(broker_client, broker_app, _token_payload(org_id, login=_login()),
                  label="saga-team")
    # MUTATION: refusing on the organisation alone turns away a seat with its own quota.
    assert second.status_code == 201, second.text
    listed = broker_client.get("/v1/accounts").json()["accounts"]
    assert {a["label"]: a["duplicate_of"] for a in listed} == {
        "ai-eng-2": None, "saga-team": None}
    assert not any(a.duplicate_withheld for a in AccountStore(broker.db).list())


def test_the_same_seat_again_is_refused_and_a_re_sign_in_onto_its_own_account_is_not(
    broker_app, broker_client
):
    org_id, login = _uuid(), _login()
    assert _add(broker_client, broker_app, _token_payload(org_id, login=login),
                label="ai-eng-2").status_code == 201
    secrets_before = dict(broker_app.state.secret_store.created)

    again = _add(broker_client, broker_app, _token_payload(org_id, login=login), label="ai-eng-3")
    assert again.status_code == 422, again.text
    assert "ai-eng-2" in again.json()["message"]
    assert broker_app.state.secret_store.created == secrets_before

    replaced = _add(broker_client, broker_app, _token_payload(org_id, login=login),
                    label="ai-eng-2")
    assert replaced.status_code == 201, replaced.text


def test_the_same_seat_in_another_workspace_is_refused_naming_nothing_of_theirs(
    broker_app, broker_client
):
    org_id, login = _uuid(), _login()
    assert _add(broker_client, broker_app, _token_payload(org_id, login=login),
                label="mine").status_code == 201
    other = _add(broker_client, broker_app, _token_payload(org_id, login=login),
                 owner=RESEARCH, label="theirs")
    assert other.status_code == 422, other.text
    assert "already registered in another workspace" in other.json()["message"]
    for foreign in ("mine", "eng:", login["uuid"], login["email_address"],
                    seat_identity(login["uuid"]), org_id, "Saga"):
        assert foreign not in other.text, foreign
    # Another seat of that organisation, from the other workspace, is not refused:
    # a refusal answers only for the seat the caller itself signed in as.
    assert _add(broker_client, broker_app, _token_payload(org_id, login=_login()),
                owner=RESEARCH, label="theirs").status_code == 201


def test_a_sign_in_whose_seat_cannot_be_read_is_not_refused_on_organisation_alone(
    broker_app, broker_client
):
    org_id = _uuid()
    assert _add(broker_client, broker_app, _token_payload(org_id, login=_login()),
                label="ai-eng-2").status_code == 201
    unread = _add(broker_client, broker_app, _token_payload(org_id), label="devops-main")
    assert unread.status_code == 201, unread.text


def test_the_seat_is_recorded_from_the_token_answer_and_never_logged_or_returned(
    broker_app, broker_client, broker, caplog
):
    org_id, login = _uuid(), _login()
    payload = _token_payload(org_id, login=login)
    caplog.set_level(logging.DEBUG)
    texts = [_add(broker_client, broker_app, payload, label="one").text]
    texts.append(_add(broker_client, broker_app, dict(payload), label="two").text)
    store = AccountStore(broker.db)
    assert store.get("eng:one").seat_id == seat_identity(login["uuid"])
    # A pair the sweep finds logs its warning too.
    store.register(ENG, "three")
    store.record_organisation("eng:three", organisation_from_payload(payload))
    _sweep_account_pool(_NoRefresher(), store, NOW)
    assert store.get("eng:three").duplicate_withheld is True
    texts.append(broker_client.get("/v1/accounts").text)
    texts.append("\n".join(r.getMessage() + json.dumps(r.__dict__, default=str)
                           for r in caplog.records))
    everything = "\n".join(texts)
    for secret_shaped in (login["uuid"], login["email_address"], seat_identity(login["uuid"])):
        # MUTATION: returning `seat_id` in `account_to_api` puts the identity on the page.
        assert secret_shaped not in everything, secret_shaped
