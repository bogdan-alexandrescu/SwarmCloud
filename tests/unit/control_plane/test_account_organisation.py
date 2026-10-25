"""A pool account is an ORGANISATION, and one organisation is one account (#2222).

POOL-DUP-DETECT. On 2026-10-24 eng:team and eng:saga-personal read identical
figures: one subscription registered twice. eng:team was replaced by
eng:saga-team, and that read the same again. The cause: the claude.ai login
belonged to TWO organisations, "Saga" (Team) and "Personal" (Max), and the
sign-in authorised whichever was selected -- Personal, both times. So:

  * the broker records which organisation a credential runs on, at add and at
    every refresh, as an OPAQUE identity (never the provider's id, never the
    login's email), with the organisation's name and plan;
  * the add answer names the organisation and plan it registered, so a wrong
    pick shows at once;
  * an add on an organisation another account already runs on is refused,
    naming that account's label -- but one login with two organisations is
    two subscriptions and both are accepted;
  * two existing accounts on one organisation are both marked `duplicate_of`
    on refresh, and `choose()` hands out only the older registration.

AND ONE AGENT UNTIL MEASURED (BGD, 2026-10-24 ~10:55Z): a never-observed
account reads as fully free, so it won every pick until the poller read it.
It now takes one agent, and its first reading decides the rest.

Unit tests: the fake Firestore and fake endpoints. Every organisation id, login
and token is built at runtime.
"""

from __future__ import annotations

import json
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from quota_broker.accounts import (
    DEFAULT_HOLD_TTL,
    MAX_AGENTS_BEFORE_READING,
    Account,
    AccountState,
    Hold,
    Unavailable,
    WindowReading,
    account_id_for,
    choose,
    duplicates,
    eligibility,
)
from quota_broker.accountstore import AccountStore
from quota_broker.credentials import RefreshOutcome
from quota_broker.main import _sweep_account_pool, account_to_api, acquire_hold
from quota_broker.oauth import (
    Credential,
    organisation_from_payload,
    organisation_identity,
    read_organisation,
    refresh,
)

from .conftest import auth_header

ENG = "eng"
RESEARCH = "research"
NOW = datetime(2026, 10, 24, 11, 0, tzinfo=timezone.utc)


def _org_id() -> str:
    return str(uuid.uuid4())


def _login() -> dict:
    return {"uuid": str(uuid.uuid4()), "email_address": f"{secrets.token_hex(4)}@example.test"}


def _token_payload(org_id: str, *, name: str = "Personal", kind: str | None = "claude_max",
                   login: dict | None = None) -> dict:
    org = {"uuid": org_id, "name": name}
    if kind is not None:
        org["organization_type"] = kind
    return {
        "access_token": "at-" + secrets.token_hex(16),
        "refresh_token": "rt-" + secrets.token_hex(16),
        "expires_in": 28800,
        "organization": org,
        "account": login or _login(),
    }


# --------------------------------------------------------------------------
# What the provider says, and what is kept of it
# --------------------------------------------------------------------------


def test_the_identity_is_the_organisation_hashed_never_the_id_or_the_login():
    org_id = _org_id()
    login = _login()
    found = organisation_from_payload(_token_payload(org_id, login=login))
    assert found is not None
    assert found.identity == organisation_identity(org_id)
    # MUTATION: storing the id itself puts the provider's identifier in Firestore and on the page.
    assert org_id not in found.identity
    assert login["uuid"] not in found.identity and login["email_address"] not in repr(found)
    assert (found.name, found.plan) == ("Personal", "Max")
    # One login, another organisation: another identity.
    other = organisation_from_payload(_token_payload(_org_id(), name="Saga", kind="claude_team",
                                                     login=login))
    assert other.identity != found.identity and other.plan == "Team"


def test_an_answer_with_no_organisation_is_no_identity_and_never_the_login():
    payload = _token_payload(_org_id())
    del payload["organization"]
    assert organisation_from_payload(payload) is None
    assert organisation_from_payload({"organization": {"name": "x"}}) is None
    assert organisation_from_payload(None) is None


class _Profiled:
    def __init__(self, answer=None, error=None):
        self.answer, self.error, self.calls = answer, error, 0

    def profile(self, access_token):
        self.calls += 1
        if self.error:
            raise self.error
        return self.answer


def test_the_profile_fills_the_plan_the_token_response_left_out():
    org_id = _org_id()
    token = _token_payload(org_id, kind=None)
    endpoint = _Profiled({"organization": {"uuid": org_id, "name": "Personal",
                                           "organization_type": "claude_max"}})
    found = read_organisation(token, endpoint, token["access_token"])
    assert endpoint.calls == 1
    assert (found.identity, found.name, found.plan) == (
        organisation_identity(org_id), "Personal", "Max")


def test_a_failed_profile_never_fails_the_add_or_refresh():
    org_id = _org_id()
    token = _token_payload(org_id, kind=None)
    found = read_organisation(token, _Profiled(error=RuntimeError("boom")), "at")
    assert found.identity == organisation_identity(org_id) and found.plan is None
    assert read_organisation(None, _Profiled(error=RuntimeError("boom")), "at") is None
    assert read_organisation(None, object(), "at") is None


def test_every_refresh_carries_the_organisation_it_ran_on():
    org_id = _org_id()

    class _Exchange:
        def exchange(self, refresh_token):
            return _token_payload(org_id, name="Saga", kind="claude_team")

    old = Credential("a", "r", NOW)
    fresh = refresh(old, _Exchange(), now=NOW)
    assert (fresh.organisation.identity, fresh.organisation.name, fresh.organisation.plan) == (
        organisation_identity(org_id), "Saga", "Team")


# --------------------------------------------------------------------------
# The add, over HTTP
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
    """The token endpoint, answering with whatever organisation is queued."""

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
    state = r.json()["state"]
    return client.post("/v1/accounts/exchange",
                       json={"state": state, "code": "code-" + secrets.token_hex(4),
                             "expected_owner": owner})


def test_the_exchange_answer_names_the_organisation_and_plan_it_registered(broker_app, broker_client):
    org_id = _org_id()
    r = _add(broker_client, broker_app, _token_payload(org_id), label="saga-team")
    assert r.status_code == 201, r.text
    body = r.json()
    # MUTATION: dropping `registered` hides that the sign-in authorised Personal, not Saga.
    assert body["registered"] == "registered eng:saga-team: organisation Personal (Max)"
    assert body["organisation"] == {"name": "Personal", "plan": "Max"}
    listed = broker_client.get("/v1/accounts").json()["accounts"][0]
    assert listed["organisation"] == {"name": "Personal", "plan": "Max"}
    assert listed["duplicate_of"] is None


def test_a_second_add_on_the_same_organisation_is_refused_naming_the_existing_label(
    broker_app, broker_client
):
    # One seat (#2282): a second seat of the organisation registers, see test_account_seat.
    org_id, login = _org_id(), _login()
    assert _add(broker_client, broker_app, _token_payload(org_id, login=login),
                label="saga-personal").status_code == 201
    secrets_before = dict(broker_app.state.secret_store.created)

    again = _add(broker_client, broker_app, _token_payload(org_id, login=login), label="saga-team")

    # MUTATION: removing `refuse_duplicate_organisation` registers the subscription twice.
    assert again.status_code == 422, again.text
    assert "saga-personal" in again.json()["message"]
    assert "Personal (Max)" in again.json()["message"]
    labels = [a["label"] for a in broker_client.get("/v1/accounts").json()["accounts"]]
    assert labels == ["saga-personal"]
    # Refused before any secret was created for the new label.
    assert broker_app.state.secret_store.created == secrets_before


def test_another_owner_on_the_same_organisation_is_refused_naming_nothing_of_theirs(
    broker_app, broker_client
):
    """BGD, 2026-10-24 17:26Z (Q2): "Name it only within the same tenant"."""
    org_id, login = _org_id(), _login()
    assert _add(broker_client, broker_app, _token_payload(org_id, login=login),
                label="mine").status_code == 201
    other = _add(broker_client, broker_app, _token_payload(org_id, login=login),
                 owner=RESEARCH, label="theirs")
    assert other.status_code == 422, other.text
    message = other.json()["message"]
    assert "already registered in another workspace" in message
    # MUTATION: naming `other.label` whatever its tenant tells research what eng's pool holds.
    for foreign in ("mine", "eng:", "eng ", organisation_identity(org_id), org_id, "Personal"):
        assert foreign not in other.text, foreign
    # The same add inside eng names the label, as before.
    same = _add(broker_client, broker_app, _token_payload(org_id, login=login), owner=ENG,
                label="again")
    assert same.status_code == 422 and "mine" in same.json()["message"]


def test_one_login_with_two_organisations_is_two_accounts_and_neither_is_flagged(
    broker_app, broker_client
):
    login = _login()
    personal = _add(broker_client, broker_app,
                    _token_payload(_org_id(), name="Personal", kind="claude_max", login=login),
                    label="saga-personal")
    team = _add(broker_client, broker_app,
                _token_payload(_org_id(), name="Saga", kind="claude_team", login=login),
                label="saga-team")
    # MUTATION: keying the identity on the login refuses the second, legitimate subscription.
    assert personal.status_code == 201, personal.text
    assert team.status_code == 201, team.text
    listed = broker_client.get("/v1/accounts").json()["accounts"]
    assert {a["label"]: a["duplicate_of"] for a in listed} == {
        "saga-personal": None, "saga-team": None}
    assert all(a["can_take_new_agent"] for a in listed)


def test_replacing_a_credential_on_its_own_organisation_is_not_a_duplicate(broker_app, broker_client):
    org_id = _org_id()
    assert _add(broker_client, broker_app, _token_payload(org_id), label="team").status_code == 201
    again = _add(broker_client, broker_app, _token_payload(org_id), label="team")
    assert again.status_code == 201, again.text


def test_a_re_sign_in_into_another_organisation_records_it(broker_app, broker_client):
    """The 10:35Z re-sign-in of eng:saga-team, this time with Saga selected."""
    assert _add(broker_client, broker_app, _token_payload(_org_id()), label="saga-team").status_code == 201
    r = _add(broker_client, broker_app, _token_payload(_org_id(), name="Saga", kind="claude_team"),
             label="saga-team")
    assert r.status_code == 201, r.text
    assert r.json()["registered"] == "registered eng:saga-team: organisation Saga (Team)"


def test_an_add_the_provider_says_nothing_about_is_accepted_and_says_so(broker_app, broker_client):
    payload = _token_payload(_org_id())
    del payload["organization"]
    r = _add(broker_client, broker_app, payload)
    assert r.status_code == 201, r.text
    assert "did not say which organisation" in r.json()["registered"]
    assert r.json()["organisation"] is None


def test_no_identity_or_login_is_logged_or_returned_in_clear(broker_app, broker_client, broker, caplog):
    org_id = _org_id()
    login = _login()
    payload = _token_payload(org_id, login=login)
    caplog.set_level(logging.DEBUG)
    texts = [_add(broker_client, broker_app, payload, label="one").text]
    texts.append(_add(broker_client, broker_app, dict(payload), label="two").text)
    texts.append(broker_client.get("/v1/accounts").text)
    # The pair, found by a sweep, logs its warning too.
    store = AccountStore(broker.db)
    store.register(ENG, "three")
    store.record_organisation(f"{ENG}:three", organisation_from_payload(payload))
    _sweep_account_pool(_NoRefresher(), store, NOW)
    texts.append("\n".join(r.getMessage() + json.dumps(r.__dict__, default=str)
                           for r in caplog.records))
    everything = "\n".join(texts)
    identity = organisation_identity(org_id)
    for secret_shaped in (org_id, identity, login["uuid"], login["email_address"],
                          payload["access_token"], payload["refresh_token"]):
        # MUTATION: returning `organisation_id` in `account_to_api` puts the identity on the page.
        assert secret_shaped not in everything, secret_shaped


# --------------------------------------------------------------------------
# Two existing accounts on one organisation
# --------------------------------------------------------------------------


class _NoRefresher:
    def sweep_accounts(self, secrets, *, keep_going=None, held=()):
        return []


class _Refresher:
    """Every account refreshes, and says it runs on `orgs[account_id]`."""

    def __init__(self, orgs):
        self.orgs = orgs

    def sweep_accounts(self, secrets, *, keep_going=None, held=()):
        return [
            RefreshOutcome(account_id, "account", True, "refreshed",
                           NOW + timedelta(hours=8), self.orgs.get(account_id))
            for _, account_id in secrets
        ]


def test_two_accounts_on_one_organisation_are_both_flagged_on_refresh(broker):
    clock = [NOW - timedelta(days=3), NOW - timedelta(hours=2)]
    store = AccountStore(broker.db, now=lambda: clock.pop(0) if clock else NOW)
    store.register(ENG, "saga-personal")
    store.register(ENG, "team")
    store.register(ENG, "devops-team")
    shared = organisation_from_payload(_token_payload(_org_id()))
    own = organisation_from_payload(_token_payload(_org_id(), name="Devops", kind="claude_max"))

    result = _sweep_account_pool(
        _Refresher({"eng:saga-personal": shared, "eng:team": shared, "eng:devops-team": own}),
        store, NOW)

    by_id = {a.account_id: a for a in store.list()}
    # MUTATION: not calling `mark_duplicates` after the sweep leaves both unmarked.
    assert by_id["eng:saga-personal"].duplicate_of == "eng:team"
    assert by_id["eng:team"].duplicate_of == "eng:saga-personal"
    assert by_id["eng:devops-team"].duplicate_of is None
    assert (by_id["eng:saga-personal"].duplicate_withheld, by_id["eng:team"].duplicate_withheld) == (
        False, True)
    assert sorted(result["duplicates"]) == ["eng:saga-personal", "eng:team"]

    listed = {a["label"]: a for a in (account_to_api(a, now=NOW) for a in by_id.values())}
    assert listed["team"]["duplicate_of"] == "eng:saga-personal"
    assert listed["team"]["can_take_new_agent"] is False
    assert {"duplicate_of": "eng:saga-personal",
            "reason": "registered on the same organisation as eng:saga-personal"} in listed[
        "team"]["blocked_by"]
    assert listed["saga-personal"]["duplicate_of"] == "eng:team"

    # choose() offers only the older registration, however the headroom reads.
    accounts = [a for a in by_id.values() if a.label != "devops-team"]
    assert choose(accounts, ENG, NOW).label == "saga-personal"
    paused = AccountStore(broker.db)
    paused.set_state("eng:saga-personal", AccountState.PAUSED)
    accounts = [a for a in paused.list() if a.label != "devops-team"]
    # MUTATION: dropping the withheld check from choose() hands out the newer one.
    assert choose(accounts, ENG, NOW) is None
    assert eligibility(accounts, ENG, NOW) == (Unavailable.POOL_PAUSED, None)

    # And a re-sign-in into its own organisation releases the newer one.
    store.record_organisation("eng:team", organisation_from_payload(
        _token_payload(_org_id(), name="Saga", kind="claude_team")))
    _sweep_account_pool(_NoRefresher(), store, NOW)
    assert {a.duplicate_of for a in store.list()} == {None}


def _acct(label, *, org=None, registered=None, holds=0, observed=NOW, windows=True,
          max_agents=6) -> Account:
    return Account(
        account_id=account_id_for(ENG, label),
        owner_tenant=ENG,
        label=label,
        windows=(
            {"five_hour": WindowReading(0.05, NOW + timedelta(hours=3)),
             "seven_day": WindowReading(0.40, NOW + timedelta(days=4))}
            if windows else {}
        ),
        observed_at=observed,
        holds=tuple(Hold(assignment_id=f"{label}-{i}", tenant_id=ENG,
                         expires_at=NOW + DEFAULT_HOLD_TTL, assigned_at=NOW)
                    for i in range(holds)),
        assigned=holds,
        organisation_id=org,
        registered_at=registered,
        max_agents=max_agents,
    )


def test_choose_hands_out_only_the_older_of_a_pair_the_sweep_has_not_marked_yet():
    shared = organisation_identity(_org_id())
    older = _acct("saga-personal", org=shared, registered=NOW - timedelta(days=9), holds=2)
    newer = _acct("team", org=shared, registered=NOW - timedelta(hours=1))
    # The newer has more room by every measure; only the pair rule skips it.
    assert choose([older, newer], ENG, NOW).label == "saga-personal"
    assert duplicates([older, newer]) == {
        "eng:saga-personal": ("eng:team", False), "eng:team": ("eng:saga-personal", True)}
    # A document registered before `registered_at` existed is the older one.
    legacy = _acct("legacy", org=shared, registered=None)
    assert duplicates([newer, legacy])["eng:legacy"] == ("eng:team", False)


def test_accounts_with_no_recorded_organisation_are_never_a_pair():
    assert duplicates([_acct("a"), _acct("b")]) == {}


# --------------------------------------------------------------------------
# One agent until measured
# --------------------------------------------------------------------------


def test_a_never_observed_account_holding_one_agent_is_skipped():
    new = _acct("saga-team", observed=None, windows=False, holds=1)
    measured = _acct("devops-team", holds=3)
    assert MAX_AGENTS_BEFORE_READING == 1
    assert new.headroom(NOW) > measured.headroom(NOW)
    # MUTATION: dropping `awaits_first_reading` from choose() hands it every agent until read.
    assert choose([new, measured], ENG, NOW).label == "devops-team"
    # With nothing held it is usable at once.
    assert choose([_acct("saga-team", observed=None, windows=False)], ENG, NOW).label == "saga-team"


def test_after_its_first_reading_it_is_offered_again_up_to_max_agents():
    read = _acct("saga-team", holds=1)
    assert choose([read], ENG, NOW).label == "saga-team"
    assert choose([_acct("saga-team", holds=5)], ENG, NOW).label == "saga-team"
    assert choose([_acct("saga-team", holds=6)], ENG, NOW) is None


def test_a_pool_whose_only_free_account_awaits_its_first_reading_waits_on_the_poll():
    new = _acct("saga-team", observed=None, windows=False, holds=1)
    full = _acct("devops-team", holds=6)
    assert choose([new, full], ENG, NOW) is None
    # MUTATION: without the first-reading branch this answers `accounts_full`.
    assert eligibility([new, full], ENG, NOW) == (Unavailable.NO_RECENT_READING, None)
    # Its cap of one is the first reading's, not a full account's.
    one = _acct("saga-team", observed=None, windows=False, holds=1, max_agents=1)
    assert eligibility([one], ENG, NOW)[0] is Unavailable.NO_RECENT_READING


def test_the_listing_says_the_first_reading_is_pending():
    shown = account_to_api(_acct("saga-team", observed=None, windows=False, holds=1), now=NOW)
    assert shown["can_take_new_agent"] is False
    assert shown["blocked_by"] == [
        {"first_reading": "pending", "assigned": 1, "max_before_reading": 1}]
    assert account_to_api(_acct("saga-team", observed=None, windows=False), now=NOW)[
        "can_take_new_agent"] is True


def test_the_hold_transaction_refuses_a_second_agent_before_the_first_reading(broker):
    store = AccountStore(broker.db)
    store.register(ENG, "saga-team")
    assert acquire_hold(broker.db, "eng:saga-team", tenant_id=ENG, now=NOW) is not None
    # MUTATION: dropping the observed_at cap from `_raw_has_room` lets a racing assign take a second.
    assert acquire_hold(broker.db, "eng:saga-team", tenant_id=ENG, now=NOW) is None
    store.record_reading("eng:saga-team", {
        "five_hour": WindowReading(0.05, NOW + timedelta(hours=3))}, NOW)
    assert acquire_hold(broker.db, "eng:saga-team", tenant_id=ENG, now=NOW) is not None


# --------------------------------------------------------------------------
# swarm-api passes the organisation through
# --------------------------------------------------------------------------


class _Bridge:
    def __init__(self, broker_client):
        self._c = broker_client

    def __call__(self, method, url, *, headers, body=None, timeout=30.0):
        parts = urlsplit(url)
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        r = self._c.request(method, path, content=body, headers=dict(headers))
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, r.text


class _Token:
    def token(self, audience: str) -> str:
        return "t"


def test_swarm_api_names_the_organisation_and_carries_the_refusal(
    api_context, broker_app, broker_client
):
    from swarm_api.brokerclient import BrokerClient
    from swarm_api.main import create_app as create_api

    app = create_api(api_context)
    app.state.account_pool = BrokerClient(
        base_url="https://broker.invalid", audience="https://broker.invalid",
        transport=_Bridge(broker_client), tokens=_Token())
    api = TestClient(app, raise_server_exceptions=False)
    org_id, login = _org_id(), _login()

    def add(label):
        broker_app.state.token_endpoint.next = _token_payload(org_id, login=login)
        r = api.post("/v1/accounts/authorize", headers=auth_header("alice"), json={"label": label})
        assert r.status_code == 200, r.text
        return api.post("/v1/accounts/exchange", headers=auth_header("alice"),
                        json={"state": r.json()["state"], "code": "code-x"})

    first = add("saga-personal")
    assert first.status_code == 201, first.text
    assert first.json()["registered"] == "registered eng:saga-personal: organisation Personal (Max)"
    assert first.json()["organisation"] == {"name": "Personal", "plan": "Max"}
    assert first.json()["account"]["organisation"] == {"name": "Personal", "plan": "Max"}

    second = add("saga-team")
    assert second.status_code == 422, second.text
    assert "saga-personal" in second.text
    assert org_id not in second.text + first.text


# --------------------------------------------------------------------------
# Another workspace's account is never named (BGD, Q2)
# --------------------------------------------------------------------------
#
# "Name it only within the same tenant": a refusal, the listing's
# `duplicate_of`, `blocked_by` and `possible_duplicate`, and swarm-api's proxy
# name the other side of a pair only when the reader's tenant owns it. A
# platform reader of the platform-wide listing sees both sides.


class _TenantIdentity:
    """A caller that IS a tenant, not the platform speaking for one."""

    def __init__(self, tenant):
        self.tenant = tenant

    def resolve(self, authorization):
        return self.tenant, False


def _cross_tenant_pair(broker):
    """eng:mine (older) and research:theirs (newer) on one organisation, marked."""
    from quota_broker.main import mark_duplicates

    clock = [NOW - timedelta(days=3), NOW - timedelta(hours=2)]
    store = AccountStore(broker.db, now=lambda: clock.pop(0) if clock else NOW)
    store.register(ENG, "mine")
    store.register(RESEARCH, "theirs")
    org_id = _org_id()
    shared = organisation_from_payload(_token_payload(org_id))
    store.record_organisation("eng:mine", shared)
    store.record_organisation("research:theirs", shared)
    assert sorted(mark_duplicates(store)) == ["eng:mine", "research:theirs"]
    return store, org_id


def _foreign_free(text, *names):
    for name in names:
        assert name not in text, name


def test_the_listing_names_a_duplicate_only_to_its_own_tenant(broker_app, broker_client, broker):
    _store, org_id = _cross_tenant_pair(broker)

    research = broker_client.get("/v1/accounts", params={"tenant_id": RESEARCH})
    assert research.status_code == 200, research.text
    (theirs,) = research.json()["accounts"]
    # MUTATION: serving `duplicate_of` unredacted names eng's account to research.
    assert theirs["duplicate_of"] is None
    assert theirs["duplicate_reason"] == (
        "this subscription is already registered in another workspace")
    assert theirs["can_take_new_agent"] is False
    assert {"duplicate_of": None,
            "reason": "this subscription is already registered in another workspace"} in theirs[
        "blocked_by"]
    _foreign_free(research.text, "mine", "eng:", organisation_identity(org_id), org_id)

    # And the other way round: eng learns nothing of research's account either.
    eng = broker_client.get("/v1/accounts", params={"tenant_id": ENG})
    (mine,) = eng.json()["accounts"]
    assert mine["duplicate_of"] is None and "another workspace" in mine["duplicate_reason"]
    _foreign_free(eng.text, "theirs", "research")

    # The platform-wide listing is the platform's, and names both sides.
    everything = {a["account_id"]: a for a in broker_client.get("/v1/accounts").json()["accounts"]}
    assert everything["research:theirs"]["duplicate_of"] == "eng:mine"
    assert everything["eng:mine"]["duplicate_of"] == "research:theirs"


def test_a_tenant_caller_reads_its_own_view_whatever_it_asks_for(broker_app, broker_client, broker):
    _cross_tenant_pair(broker)
    broker_app.state.identity = _TenantIdentity(RESEARCH)
    # A tenant cannot widen its view by naming the platform-wide scope or eng.
    for params in ({}, {"tenant_id": ENG}):
        r = broker_client.get("/v1/accounts", params=params)
        assert r.status_code == 200, r.text
        assert [a["account_id"] for a in r.json()["accounts"]] == ["research:theirs"]
        _foreign_free(r.text, "mine", "eng:")


def test_a_lent_account_names_its_pair_only_to_the_tenant_that_owns_the_pair(broker):
    from quota_broker.main import mark_duplicates

    store = AccountStore(broker.db)
    store.register(ENG, "mine", lend_to=[RESEARCH])
    store.register(ENG, "copy")
    shared = organisation_from_payload(_token_payload(_org_id()))
    store.record_organisation("eng:mine", shared)
    store.record_organisation("eng:copy", shared)
    mark_duplicates(store)
    lent = store.get("eng:mine")
    # Research may run on eng:mine and sees it; it does not learn of eng:copy.
    shown = account_to_api(lent, now=NOW, viewer=RESEARCH)
    assert shown["duplicate_of"] is None
    assert "copy" not in json.dumps(shown)
    # Its owner does.
    assert account_to_api(lent, now=NOW)["duplicate_of"] == "eng:copy"
    assert account_to_api(lent, now=NOW, viewer=ENG)["duplicate_of"] == "eng:copy"
    assert account_to_api(lent, now=NOW, platform_wide=True)["duplicate_of"] == "eng:copy"


def test_swarm_api_shows_another_tenants_caller_no_label_even_from_an_unredacted_broker(
    api_context
):
    """swarm-api's proxy is a second line: an older broker that sends the
    other side's id unredacted still reaches bob (research) without it."""
    from swarm_api.main import create_app as create_api

    class _Pool:
        def list_accounts(self, tenant_id):
            return {"accounts": [{
                "account_id": "research:theirs", "owner_tenant": RESEARCH, "label": "theirs",
                "duplicate_of": "eng:mine",
                "duplicate_reason": "registered on the same organisation as eng:mine",
                "possible_duplicate": {"of": "eng:other", "reason": "identical readings on two polls"},
                "blocked_by": [
                    {"duplicate_of": "eng:mine",
                     "reason": "registered on the same organisation as eng:mine"},
                    {"possible_duplicate": "eng:other", "advisory": True,
                     "reason": "identical readings on two polls with eng:other"},
                    {"cap": 6, "max_agents": 6},
                ],
            }, {
                "account_id": "research:own", "owner_tenant": RESEARCH, "label": "own",
                "duplicate_of": "research:theirs",
                "duplicate_reason": "registered on the same organisation as research:theirs",
                "possible_duplicate": None,
                "blocked_by": [{"duplicate_of": "research:theirs",
                                "reason": "registered on the same organisation as research:theirs"}],
            }]}

    app = create_api(api_context)
    app.state.account_pool = _Pool()
    r = TestClient(app, raise_server_exceptions=False).get(
        "/v1/accounts", headers=auth_header("bob"))
    assert r.status_code == 200, r.text
    theirs, own = r.json()["accounts"]
    # MUTATION: forwarding `accounts` verbatim hands bob eng's labels.
    _foreign_free(r.text, "eng:", "mine", "eng:other")
    assert theirs["duplicate_of"] is None
    assert theirs["duplicate_reason"] == (
        "this subscription is already registered in another workspace")
    assert theirs["possible_duplicate"] == {
        "of": None,
        "reason": "identical readings on two polls with an account in another workspace"}
    assert theirs["blocked_by"][0] == {
        "duplicate_of": None,
        "reason": "this subscription is already registered in another workspace"}
    assert theirs["blocked_by"][1]["possible_duplicate"] is None
    assert theirs["blocked_by"][2] == {"cap": 6, "max_agents": 6}
    # Its own tenant's pair is named, untouched.
    assert own["duplicate_of"] == "research:theirs"
    assert own["blocked_by"][0]["duplicate_of"] == "research:theirs"


def test_sc_never_prints_a_duplicate_field():
    """`sc` shows accounts through an allow-list (`render.public_account`)
    that carries none of these fields, so it cannot name another tenant's
    account; this holds it there."""
    from swarm_mcp.render import public_account

    shown = public_account({"account_id": "research:theirs", "label": "theirs",
                            "duplicate_of": "eng:mine",
                            "possible_duplicate": {"of": "eng:mine"},
                            "duplicate_reason": "registered on the same organisation as eng:mine",
                            "blocked_by": [{"duplicate_of": "eng:mine"}]})
    assert "eng:mine" not in json.dumps(shown)


# --------------------------------------------------------------------------
# Identical readings on two polls: a possible duplicate (BGD, Q1)
# --------------------------------------------------------------------------
#
# "Build it as a safety net": nothing has yet confirmed the provider returns
# an organisation id, and without it the organisation check is inert. This
# morning's real pair read identical figures with resets one second apart.


FIVE_RESET = NOW + timedelta(hours=2, minutes=13)
WEEK_RESET = NOW + timedelta(days=2, hours=5)


def _reading(store, account_id, at, *, five=0.42, week=0.61, skew=timedelta(0)):
    store.record_reading(account_id, {
        "five_hour": WindowReading(five, FIVE_RESET + skew),
        "seven_day": WindowReading(week, WEEK_RESET + skew),
    }, at)


def _poll(store, at, *, a=None, b=None):
    """One poll of both accounts at `at`, then the broker's pass over them."""
    from quota_broker.main import mark_possible_duplicates

    _reading(store, "eng:team", at, **(a or {}))
    _reading(store, "eng:saga-personal", at + timedelta(seconds=20), **(b or {}))
    return mark_possible_duplicates(store)


def _two(broker):
    store = AccountStore(broker.db)
    store.register(ENG, "saga-personal")
    store.register(ENG, "team")
    return store


def _flags(store):
    return {a.account_id: a.possible_duplicate_of for a in store.list()}


def test_identical_readings_on_two_polls_mark_a_possible_duplicate(broker):
    store = _two(broker)
    # Resets one second apart, as measured on 2026-10-24.
    second = {"skew": timedelta(seconds=1)}
    # MUTATION: flagging on the first match calls one coincidence a duplicate.
    assert _poll(store, NOW, b=second) == []
    assert _flags(store) == {"eng:saga-personal": None, "eng:team": None}

    flagged = _poll(store, NOW + timedelta(minutes=5), b=second)

    assert sorted(flagged) == ["eng:saga-personal", "eng:team"]
    assert _flags(store) == {"eng:saga-personal": "eng:team", "eng:team": "eng:saga-personal"}
    # A possible duplicate is not the organisation-proven kind.
    assert {a.duplicate_of for a in store.list()} == {None}

    shown = account_to_api(store.get("eng:team"), now=NOW + timedelta(minutes=6))
    assert shown["possible_duplicate"] == {
        "of": "eng:saga-personal", "reason": "identical readings on two polls"}
    assert shown["duplicate_of"] is None
    assert {"possible_duplicate": "eng:saga-personal", "advisory": True,
            "reason": "identical readings on two polls with eng:saga-personal"} in shown[
        "blocked_by"]
    # FLAGGED, NOT WITHHELD: readings can coincide, and an inference must not
    # take a real subscription out of the pool.
    assert shown["can_take_new_agent"] is True
    assert choose(store.list(), ENG, NOW + timedelta(minutes=6)) is not None
    assert len({choose([a], ENG, NOW + timedelta(minutes=6)).account_id
                for a in store.list()}) == 2


def test_one_reading_counted_twice_is_still_one_poll(broker):
    store = _two(broker)
    _poll(store, NOW)
    from quota_broker.main import mark_possible_duplicates

    # MUTATION: counting passes rather than new readings flags on one poll.
    assert mark_possible_duplicates(store) == []
    assert mark_possible_duplicates(store) == []
    # A new reading of ONE side is not a second poll of the pair either.
    _reading(store, "eng:team", NOW + timedelta(minutes=5))
    assert mark_possible_duplicates(store) == []
    _reading(store, "eng:saga-personal", NOW + timedelta(minutes=6))
    assert sorted(mark_possible_duplicates(store)) == ["eng:saga-personal", "eng:team"]


@pytest.mark.parametrize("second", [
    {"skew": timedelta(seconds=30)},     # resets apart: two subscriptions
    {"five": 0.43},                      # one window's figure differs
    {"week": 0.60},                      # the other's
])
def test_readings_that_differ_anywhere_are_never_a_possible_duplicate(broker, second):
    store = _two(broker)
    _poll(store, NOW, b=second)
    # MUTATION: comparing one window, or resets without a tolerance, flags these.
    assert _poll(store, NOW + timedelta(minutes=5), b=second) == []
    assert _poll(store, NOW + timedelta(minutes=10), b=second) == []
    assert _flags(store) == {"eng:saga-personal": None, "eng:team": None}


def test_a_match_broken_by_a_differing_poll_starts_counting_again(broker):
    store = _two(broker)
    _poll(store, NOW)
    _poll(store, NOW + timedelta(minutes=5), b={"five": 0.5})
    assert _poll(store, NOW + timedelta(minutes=10)) == []
    assert len(_poll(store, NOW + timedelta(minutes=15))) == 2
    # And a later differing poll clears the flag.
    _poll(store, NOW + timedelta(minutes=20), b={"five": 0.9})
    assert _flags(store) == {"eng:saga-personal": None, "eng:team": None}


def test_two_idle_accounts_are_not_a_pair(broker):
    """Two unused accounts read 0% everywhere, and a provider that dates an
    unused window from the poll would give them resets seconds apart."""
    store = _two(broker)
    idle = {"five": 0.0, "week": 0.0}
    _poll(store, NOW, a=idle, b=idle)
    assert _poll(store, NOW + timedelta(minutes=5), a=idle, b=idle) == []


def test_one_recorded_organisation_does_not_stop_the_fallback(broker):
    store = _two(broker)
    store.record_organisation("eng:team", organisation_from_payload(_token_payload(_org_id())))
    _poll(store, NOW)
    assert len(_poll(store, NOW + timedelta(minutes=5))) == 2


def test_a_differing_recorded_organisation_wins_over_identical_readings(broker):
    store = _two(broker)
    _poll(store, NOW)
    assert len(_poll(store, NOW + timedelta(minutes=5))) == 2
    store.record_organisation("eng:team", organisation_from_payload(_token_payload(_org_id())))
    store.record_organisation("eng:saga-personal", organisation_from_payload(
        _token_payload(_org_id(), name="Saga", kind="claude_team")))
    # MUTATION: ignoring the organisations keeps two known subscriptions flagged.
    assert _poll(store, NOW + timedelta(minutes=10)) == []
    assert _flags(store) == {"eng:saga-personal": None, "eng:team": None}


def test_a_possible_duplicate_in_another_workspace_is_not_named(broker):
    store = AccountStore(broker.db)
    store.register(ENG, "team")
    store.register(RESEARCH, "theirs")
    from quota_broker.main import mark_possible_duplicates

    for minutes in (0, 5):
        _reading(store, "eng:team", NOW + timedelta(minutes=minutes))
        _reading(store, "research:theirs", NOW + timedelta(minutes=minutes, seconds=3))
        mark_possible_duplicates(store)
    theirs = store.get("research:theirs")
    assert theirs.possible_duplicate_of == "eng:team"
    shown = account_to_api(theirs, now=NOW + timedelta(minutes=6))
    # MUTATION: naming `possible_duplicate_of` whatever its tenant.
    assert shown["possible_duplicate"] == {
        "of": None,
        "reason": "identical readings on two polls with an account in another workspace"}
    assert "eng" not in json.dumps(shown["blocked_by"])
    _foreign_free(json.dumps(shown), "eng:team")
    assert account_to_api(theirs, now=NOW, platform_wide=True)["possible_duplicate"]["of"] == (
        "eng:team")


def test_the_sweep_marks_possible_duplicates_after_its_usage_poll(broker):
    """Wired into `/v1/quota/sweep`, after the poll, so the poll's readings
    are the ones compared."""
    from quota_broker.main import WorkerIdentity, create_app

    store = _two(broker)
    polls = [NOW, NOW + timedelta(minutes=5)]

    class _Poller:
        def poll_round(self, accounts, keep_going=None):
            at = polls.pop(0)
            _reading(store, "eng:team", at)
            _reading(store, "eng:saga-personal", at + timedelta(seconds=4))
            return []

    class _Refresher(_NoRefresher):
        def sweep(self, pairs, keep_going=None):
            return []

    app = create_app(broker, identity=WorkerIdentity(required=False),
                     credential_refresher=_Refresher(),
                     subscription_tenants=lambda: [], account_store=store)
    app.state.usage_poller = _Poller()
    client = TestClient(app, raise_server_exceptions=False)
    first = client.post("/v1/quota/sweep")
    assert first.status_code == 200, first.text
    second = client.post("/v1/quota/sweep").json()
    # MUTATION: not calling `mark_possible_duplicates` in the sweep leaves both unflagged.
    assert sorted(second["possible_duplicates"]) == ["eng:saga-personal", "eng:team"]
    assert _flags(store) == {"eng:saga-personal": "eng:team", "eng:team": "eng:saga-personal"}
