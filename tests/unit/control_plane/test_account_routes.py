"""The account pool over HTTP: registering, reading and refreshing a pool.

These routes live on the quota broker and not on swarm-api, and that is not a
layering preference. Refreshing an OAuth credential REVOKES the previous one,
so two components writing the same account brick it. The broker is the
platform's single writer; swarm-api proxies. "Single" has to mean one
implementation as well as one process, which is why the on-demand refresh here
goes through the same CredentialRefresher the sweep uses instead of exchanging
a token inline.

What these pin:

  * a credential with no refresh token is refused AT PASTE TIME, because the
    whole promise is that the operator logs in once -- discovering it at the
    next sweep would be long afterwards with nothing pointing at the cause;
  * the credential is split across two secrets, so a pod holds something that
    expires rather than something that can mint successors forever;
  * no route returns key material, not even its length.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import pytest

PROJECT = "test-project"
TENANT = "eng"


def _credential(expires_in_hours: int = 8, refresh: str = "rt-abcdefgh") -> str:
    """Claude Code's keychain shape, which is what an operator actually pastes."""
    expires = datetime.now(timezone.utc) + timedelta(hours=expires_in_hours)
    return json.dumps(
        {
            "claudeAiOauth": {
                "accessToken": "at-abcdefghijklmnop",
                "refreshToken": refresh,
                "expiresAt": int(expires.timestamp() * 1000),
            }
        }
    )


class _Secrets:
    """A Secret Manager double that records every write.

    Models the real store's REFUSAL to invent a secret: `add_version` on a
    secret that was never created raises, exactly as SecretManagerStore does
    ("a secret this service invents has no tenant labels and no accessor
    binding"). Without that refusal here, a test would pass against a double
    that is more forgiving than production -- which is how the ordering bug
    reached a deployed service.
    """

    def __init__(self) -> None:
        self.versions: dict[str, list[str]] = {}
        self.created: dict[str, dict[str, str]] = {}
        self.accessors: dict[str, list[str]] = {}
        self.fail_on: str | None = None

    def access(self, name: str) -> str:
        if name not in self.versions:
            raise KeyError(name)
        return self.versions[name][-1]

    def ensure_secret(self, name, *, labels, accessors, region):
        if self.fail_on and self.fail_on in name:
            raise RuntimeError("secret manager said no")
        created = name not in self.created
        if created:
            self.created[name] = dict(labels)
            self.region = region
        self.accessors.setdefault(name, [])
        for m in accessors:
            if m not in self.accessors[name]:
                self.accessors[name].append(m)
        return created

    def add_version(self, name: str, payload: str) -> None:
        if name not in self.created:
            raise KeyError(f"{name} was never created")
        self.versions.setdefault(name, []).append(payload)


@pytest.fixture()
def client(broker):
    from fastapi.testclient import TestClient

    from quota_broker.accountstore import AccountStore
    from quota_broker.main import WorkerIdentity, create_app

    class PlatformIdentity(WorkerIdentity):
        def resolve(self, authorization):
            return None, True             # the platform, so every tenant is in scope

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("REGION", "us-central1")
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.setenv("BROKER_SERVICE_ACCOUNT", "swarm-quota-broker@p.iam.gserviceaccount.com")
    monkeypatch.setenv(
        "WORKER_SERVICE_ACCOUNT_TEMPLATE",
        "swarm-agent-worker-{tenant}@p.iam.gserviceaccount.com",
    )
    app = create_app(
        broker,
        identity=PlatformIdentity(project_id=PROJECT),
        account_store=AccountStore(broker.db),
    )
    secrets = _Secrets()
    app.state.secret_store = secrets
    c = TestClient(app, raise_server_exceptions=False)
    c.secrets = secrets
    c.app_ref = app
    return c


# -- registration ----------------------------------------------------------


def test_an_account_is_registered_from_the_keychain_item_verbatim(client):
    """`parse_credential` already unwraps `claudeAiOauth`, so the operator can
    paste what they have instead of taking it apart first."""
    r = client.post(
        "/v1/accounts",
        json={"owner_tenant": TENANT, "label": "personal", "credential": _credential()},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["account"]["label"] == "personal"
    assert body["account"]["owner_tenant"] == TENANT
    assert body["account"]["state"] == "AVAILABLE"


def test_the_credential_is_split_across_two_secrets(client):
    """The security boundary. `-refresh` holds the pair and only the broker
    reads it; the base holds ONLY the access token and is what the tenant's pod
    reads -- so a compromised pod has something that expires, not something
    that can mint successors forever."""
    client.post(
        "/v1/accounts",
        json={"owner_tenant": TENANT, "label": "personal", "credential": _credential()},
    )
    written = client.secrets.versions
    base = f"swarm-account-{TENANT}--personal"

    assert base in written and f"{base}-refresh" in written
    assert written[base][-1] == "at-abcdefghijklmnop"
    assert "refreshToken" not in written[base][-1]
    assert "rt-abcdefgh" in written[f"{base}-refresh"][-1]


def test_a_credential_with_no_refresh_token_is_refused_at_paste_time(client):
    """A `claude setup-token` value cannot be kept alive. Accepting it would
    give the operator a pool entry that looks healthy and dies silently at its
    first expiry -- the opposite of "you only log in once"."""
    r = client.post(
        "/v1/accounts",
        json={
            "owner_tenant": TENANT,
            "label": "setup-token",
            "credential": json.dumps({"accessToken": "sk-ant-oat01-longvalue"}),
        },
    )
    assert r.status_code == 422
    assert "refresh token" in r.json()["message"]
    assert client.secrets.versions == {}, "nothing may be written when the paste is refused"


def test_a_credential_that_is_not_json_says_what_was_expected(client):
    r = client.post(
        "/v1/accounts",
        json={"owner_tenant": TENANT, "label": "junk", "credential": "not json at all"},
    )
    assert r.status_code == 422
    assert "accessToken" in r.json()["message"]


def test_no_route_returns_key_material(client):
    """Not even its length. A length is a real hint about a secret and nobody
    reading an account listing needs it."""
    client.post(
        "/v1/accounts",
        json={"owner_tenant": TENANT, "label": "personal", "credential": _credential()},
    )
    blob = client.get("/v1/accounts").text
    for forbidden in ("at-abcdefghijklmnop", "rt-abcdefgh", "access_token_len"):
        assert forbidden not in blob, forbidden


# -- reading ---------------------------------------------------------------


def test_the_listing_carries_the_columns_cs_status_shows(client, broker):
    """An operator who reads `cs status` on their laptop should not have to
    learn a second vocabulary for the same facts about the same accounts."""
    from quota_broker.accounts import WindowReading
    from quota_broker.accountstore import AccountStore

    client.post(
        "/v1/accounts",
        json={"owner_tenant": TENANT, "label": "personal", "credential": _credential()},
    )
    store = AccountStore(broker.db)
    now = datetime.now(timezone.utc)
    store.record_reading(
        f"{TENANT}:personal",
        {
            "five_hour": WindowReading(utilization=0.01, resets_at=now + timedelta(hours=4)),
            "seven_day": WindowReading(utilization=0.97, resets_at=now + timedelta(days=2)),
        },
        observed_at=now,
    )

    account = client.get("/v1/accounts").json()["accounts"][0]
    assert set(account["windows"]) == {"five_hour", "seven_day"}
    assert account["windows"]["seven_day"]["utilization"] == 0.97
    assert account["windows"]["five_hour"]["resets_at"]
    assert account["stale"] is False
    for column in ("label", "state", "assigned", "observed_at"):
        assert column in account


def test_an_account_with_no_reading_is_stale_not_zero(client):
    """"Nothing has been observed" and "utilisation is zero" are different
    claims. `cs status` marks a projected figure with `~` for this reason; a
    number shown without that mark asserts it is current."""
    client.post(
        "/v1/accounts",
        json={"owner_tenant": TENANT, "label": "fresh", "credential": _credential()},
    )
    account = client.get("/v1/accounts").json()["accounts"][0]
    assert account["stale"] is True
    assert account["windows"] == {}


# -- on-demand refresh -----------------------------------------------------


class _Refresher:
    def __init__(self, reason="refreshed", changed=True):
        self.calls: list[tuple[str, str]] = []
        self._reason = reason
        self._changed = changed

    def refresh_secret(self, base, *, label=""):
        self.calls.append((base, label))

        class _Outcome:
            def as_dict(_self):
                return {
                    "tenant_id": label,
                    "provider": "account",
                    "changed": self._changed,
                    "reason": self._reason,
                }

        return _Outcome()


def test_refreshing_on_demand_reports_back(client):
    """The sweep already refreshes on a timer. This exists because a timer
    gives an operator no way to answer "did that work?"."""
    client.post(
        "/v1/accounts",
        json={"owner_tenant": TENANT, "label": "personal", "credential": _credential()},
    )
    client.app_ref.state.credential_refresher = _Refresher()

    r = client.post(f"/v1/accounts/{TENANT}:personal/refresh")
    assert r.status_code == 200, r.text
    assert r.json()["refresh"]["reason"] == "refreshed"
    assert r.json()["account"]["label"] == "personal"


def test_refreshing_goes_through_the_same_refresher_as_the_sweep(client):
    """Two code paths that both rotate a credential is precisely how a rotating
    credential gets corrupted."""
    client.post(
        "/v1/accounts",
        json={"owner_tenant": TENANT, "label": "personal", "credential": _credential()},
    )
    refresher = _Refresher()
    client.app_ref.state.credential_refresher = refresher

    client.post(f"/v1/accounts/{TENANT}:personal/refresh")
    assert refresher.calls == [(f"swarm-account-{TENANT}--personal", "personal")]


def test_a_dead_refresh_token_marks_the_account_rather_than_retrying_forever(client):
    """Only a human can fix a revoked refresh token, so the broker must stop
    trying rather than spend the endpoint's rate limit rediscovering it."""
    client.post(
        "/v1/accounts",
        json={"owner_tenant": TENANT, "label": "personal", "credential": _credential()},
    )
    client.app_ref.state.credential_refresher = _Refresher(reason="reauth_required", changed=False)

    r = client.post(f"/v1/accounts/{TENANT}:personal/refresh")
    assert r.json()["account"]["state"] == "REAUTH_REQUIRED"
    assert "reauth_required" in r.json()["account"]["reason"]


def test_refreshing_an_account_that_does_not_exist_says_so(client):
    r = client.post("/v1/accounts/eng:nope/refresh")
    assert r.status_code == 422
    assert "no account" in r.json()["message"]


# -- removal ---------------------------------------------------------------


def test_removing_an_account_keeps_its_secret(client):
    """Deleting a Secret Manager secret is irreversible and takes its version
    history with it, so an account removed by mistake would be unrecoverable
    rather than re-registerable."""
    client.post(
        "/v1/accounts",
        json={"owner_tenant": TENANT, "label": "personal", "credential": _credential()},
    )
    r = client.delete(f"/v1/accounts/{TENANT}:personal")

    assert r.status_code == 200
    assert r.json()["secret"] == "retained"
    assert client.get("/v1/accounts").json()["accounts"] == []
    assert f"swarm-account-{TENANT}--personal-refresh" in client.secrets.versions


# -- provisioning, and the ordering a live probe exposed --------------------


def test_registering_creates_both_secrets_with_the_labels_that_identify_them(client):
    """The secrets cannot be declared in terraform: their names contain a LABEL
    the operator chooses at registration time. So this service makes them, and
    it must apply the same labels the rest of the platform is found by."""
    client.post(
        "/v1/accounts",
        json={"owner_tenant": TENANT, "label": "personal", "credential": _credential()},
    )
    base = f"swarm-account-{TENANT}--personal"

    assert set(client.secrets.created) == {base, f"{base}-refresh"}
    for name, labels in client.secrets.created.items():
        assert labels["managed-by"] == "swarm-secrets", name
        assert labels["component"] == "swarm-account", name
        assert labels["tenant"] == TENANT and labels["account"] == "personal", name
        assert labels["environment"] == "dev", name


def test_only_the_broker_may_read_the_pair_and_the_worker_only_the_token(client):
    """The two-secret split is worthless if both are bound to the same readers.
    A pod that could read the refresh token could mint successors forever,
    which is the blast radius the split exists to prevent."""
    client.post(
        "/v1/accounts",
        json={"owner_tenant": TENANT, "label": "personal", "credential": _credential()},
    )
    base = f"swarm-account-{TENANT}--personal"

    pair_readers = client.secrets.accessors[f"{base}-refresh"]
    token_readers = client.secrets.accessors[base]

    assert any("quota-broker" in m for m in pair_readers)
    assert not any("agent-worker" in m for m in pair_readers), (
        "the tenant's pod must never be able to read the refresh token"
    )
    assert any(f"agent-worker-{TENANT}" in m for m in token_readers)


def test_a_failed_secret_write_leaves_no_account_behind(client):
    """THE ORDERING BUG, proved against the deployed service before it was
    fixed. `add_version` refuses to invent a secret, so registering a new label
    raised AFTER the document had been written -- leaving an account the pool
    would assign to an agent that then cannot authenticate. The orphan had to
    be deleted by hand.

    A secret with no document is invisible and harmless. A document with no
    secret is a live trap. So every fallible step must happen before the one
    that publishes the account.
    """
    client.secrets.fail_on = "-refresh"

    r = client.post(
        "/v1/accounts",
        json={"owner_tenant": TENANT, "label": "doomed", "credential": _credential()},
    )

    assert r.status_code == 422
    assert "No account was registered" in r.json()["message"]
    assert client.get("/v1/accounts").json()["accounts"] == [], "an orphan was left behind"


def test_re_registering_a_label_replaces_the_credential_without_duplicating(client):
    """The supported way to rotate a credential by hand. It must adopt the
    existing secrets rather than fail on AlreadyExists."""
    for token in ("first", "second"):
        r = client.post(
            "/v1/accounts",
            json={
                "owner_tenant": TENANT,
                "label": "personal",
                "credential": _credential(refresh=f"rt-{token}-value"),
            },
        )
        assert r.status_code == 201, r.text

    assert len(client.get("/v1/accounts").json()["accounts"]) == 1
    base = f"swarm-account-{TENANT}--personal"
    assert len(client.secrets.versions[f"{base}-refresh"]) == 2
    assert "rt-second-value" in client.secrets.versions[f"{base}-refresh"][-1]


def test_an_unset_worker_template_refuses_rather_than_binding_nobody(broker, monkeypatch):
    """"Not configured means refuse." A secret created with no worker accessor
    is one the tenant's pod cannot read, and that failure does not surface
    here -- it surfaces inside a job as an unexplained auth error."""
    from fastapi.testclient import TestClient

    from quota_broker.accountstore import AccountStore
    from quota_broker.main import WorkerIdentity, create_app

    monkeypatch.setenv("REGION", "us-central1")
    monkeypatch.delenv("WORKER_SERVICE_ACCOUNT_TEMPLATE", raising=False)

    class PlatformIdentity(WorkerIdentity):
        def resolve(self, authorization):
            return None, True

    app = create_app(
        broker,
        identity=PlatformIdentity(project_id=PROJECT),
        account_store=AccountStore(broker.db),
    )
    app.state.secret_store = _Secrets()
    c = TestClient(app, raise_server_exceptions=False)

    r = c.post(
        "/v1/accounts",
        json={"owner_tenant": TENANT, "label": "orphaned", "credential": _credential()},
    )

    assert r.status_code == 422
    assert "WORKER_SERVICE_ACCOUNT_TEMPLATE" in r.json()["message"]
    assert c.get("/v1/accounts").json()["accounts"] == []


# -- what the sweep writes down --------------------------------------------
#
# The sweep detected a dead refresh token, logged it, counted it into a
# response body Cloud Scheduler discards, and left the account document at
# AVAILABLE with an empty reason. Every consequence of that was invisible: new
# agents were still assigned an account that cannot authenticate, the dead
# token was presented to the endpoint every five minutes forever, and an
# operator looking at the pool saw nothing wrong with it. These pin the write
# and, just as importantly, the way back out of it.


class _SweepRefresher:
    """Answers the sweep, recording exactly what it was handed.

    Returns real `RefreshOutcome`s rather than a stand-in. The write-back reads
    `.reason` and `.tenant_id` off them, and a looser double would let the
    label-for-account-id confusion these tests exist to pin slip through.
    """

    def __init__(self, reasons=None, on_second=None):
        self.reasons = dict(reasons or {})
        # What a SECOND attempt on the same account answers, when it differs.
        # The sweep confirms a dead credential before writing a state only a
        # person can leave, so this is how a test drives the confirmation.
        self.on_second = dict(on_second or {})
        self.seen: list[list[tuple[str, str]]] = []
        self.confirmations: list[str] = []

    def sweep(self, tenants):
        return []

    def refresh_secret(self, base, *, label=""):
        from quota_broker.credentials import RefreshOutcome

        self.confirmations.append(label)
        reason = self.on_second.get(label, self.reasons.get(label, "refreshed"))
        return RefreshOutcome(label, "account", reason == "refreshed", reason)

    def sweep_accounts(self, secrets):
        from quota_broker.credentials import RefreshOutcome

        self.seen.append(list(secrets))
        out = []
        for _base, key in secrets:
            reason = self.reasons.get(key, "refreshed")
            out.append(RefreshOutcome(key, "account", reason == "refreshed", reason))
        return out

    def swept_keys(self):
        return [key for _base, key in self.seen[-1]]


def _sweeping(client, refresher):
    client.app_ref.state.credential_refresher = refresher
    # The usage poll is a separate concern and would reach the network.
    client.app_ref.state.usage_poller = None
    return client.post("/v1/quota/sweep")


def _add(client, tenant, label):
    r = client.post(
        "/v1/accounts",
        json={"owner_tenant": tenant, "label": label, "credential": _credential()},
    )
    assert r.status_code == 201, r.text


def _account(client, account_id):
    accounts = client.get("/v1/accounts").json()["accounts"]
    return next(a for a in accounts if a["account_id"] == account_id)


def test_the_sweep_points_an_operator_at_the_account_that_is_actually_broken(client):
    """The failure had no other symptom. A revoked refresh token was detected
    on every tick and the listing went on saying AVAILABLE with no reason, so
    nothing anywhere named the account a person had to go and fix."""
    _add(client, TENANT, "personal")

    r = _sweeping(client, _SweepRefresher({f"{TENANT}:personal": "reauth_required"}))

    assert r.status_code == 200, r.text
    account = _account(client, f"{TENANT}:personal")
    assert account["state"] == "REAUTH_REQUIRED"
    assert "reauth_required" in account["reason"]
    assert r.json()["accounts"]["marked_reauth_required"] == [f"{TENANT}:personal"]


def test_a_credential_that_cannot_be_parsed_is_as_dead_as_a_revoked_one(client):
    """`unreadable` means there is nothing to present to the token endpoint.
    Retrying the same bytes every five minutes cannot change the answer, and
    the on-demand route has always treated the two the same."""
    _add(client, TENANT, "personal")

    _sweeping(client, _SweepRefresher({f"{TENANT}:personal": "unreadable"}))

    assert _account(client, f"{TENANT}:personal")["state"] == "REAUTH_REQUIRED"


def test_the_write_back_is_keyed_on_the_account_id_not_the_label(client):
    """A label is unique only WITHIN a tenant -- two tenants may both have one
    called `personal`. Keyed on the label, the sweep marks whichever it finds
    first, so a healthy account is taken out of service and the dead one is
    left in it."""
    _add(client, "eng", "personal")
    _add(client, "research", "personal")

    refresher = _SweepRefresher({"research:personal": "reauth_required"})
    _sweeping(client, refresher)

    assert _account(client, "research:personal")["state"] == "REAUTH_REQUIRED"
    assert _account(client, "eng:personal")["state"] == "AVAILABLE"
    assert sorted(refresher.swept_keys()) == ["eng:personal", "research:personal"]


def test_a_marked_account_stops_being_presented_to_the_token_endpoint(client):
    """The whole reason the state exists. Only a human can fix a revoked token,
    so the broker must stop spending a shared rate limit rediscovering it."""
    _add(client, TENANT, "personal")
    dead = f"{TENANT}:personal"

    first = _SweepRefresher({dead: "reauth_required"})
    _sweeping(client, first)
    assert first.swept_keys() == [dead]

    second = _SweepRefresher()
    _sweeping(client, second)
    assert second.swept_keys() == []


def test_signing_in_again_gives_the_account_back(client):
    """Marking without an exit replaces a silent failure with a permanent one:
    `due_for_refresh` skips a marked account, so the sweep can never undo the
    state it wrote."""
    _add(client, TENANT, "personal")
    _sweeping(client, _SweepRefresher({f"{TENANT}:personal": "reauth_required"}))
    assert _account(client, f"{TENANT}:personal")["state"] == "REAUTH_REQUIRED"

    _add(client, TENANT, "personal")

    account = _account(client, f"{TENANT}:personal")
    assert account["state"] == "AVAILABLE"
    assert account["reason"] == "", "a reason that is no longer true is worse than none"


def test_recovery_gives_a_paused_account_its_pause_back(client):
    """A background sweep marked it and a person's credential unmarked it. If
    recovery always chose AVAILABLE, an account an operator had deliberately
    taken out of service would come back in it with nothing saying so."""
    _add(client, TENANT, "personal")
    client.put(
        f"/v1/accounts/{TENANT}:personal/state",
        json={"state": "PAUSED", "reason": "spending review"},
    )

    _sweeping(client, _SweepRefresher({f"{TENANT}:personal": "reauth_required"}))
    assert _account(client, f"{TENANT}:personal")["state"] == "REAUTH_REQUIRED"

    _add(client, TENANT, "personal")
    assert _account(client, f"{TENANT}:personal")["state"] == "PAUSED"


def test_an_exchange_that_worked_clears_the_mark_and_a_still_valid_one_does_not(client):
    """`refreshed` presented the refresh token and got a new one, which is the
    only thing the state ever claimed was false. `still_valid` never presents
    it -- it says the stored ACCESS token has hours left, and is silent about
    the half that died."""
    _add(client, TENANT, "personal")
    _sweeping(client, _SweepRefresher({f"{TENANT}:personal": "reauth_required"}))

    client.app_ref.state.credential_refresher = _Refresher(reason="still_valid")
    client.post(f"/v1/accounts/{TENANT}:personal/refresh")
    assert _account(client, f"{TENANT}:personal")["state"] == "REAUTH_REQUIRED"

    client.app_ref.state.credential_refresher = _Refresher(reason="refreshed")
    r = client.post(f"/v1/accounts/{TENANT}:personal/refresh")
    assert r.json()["account"]["state"] == "AVAILABLE"
    assert r.json()["account"]["reason"] == ""


def test_an_account_removed_mid_sweep_does_not_stall_the_tick(client):
    """The sweep is what un-parks throttled tenants. A refresh outcome for a
    document that is no longer there must cost one log line, not the tick."""
    _add(client, TENANT, "personal")
    client.delete(f"/v1/accounts/{TENANT}:personal")

    from quota_broker.credentials import RefreshOutcome

    refresher = _SweepRefresher()
    refresher.sweep_accounts = lambda secrets: [
        RefreshOutcome(f"{TENANT}:gone", "account", False, "reauth_required")
    ]

    r = _sweeping(client, refresher)

    assert r.status_code == 200, r.text
    assert r.json()["accounts"]["reauth_required"] == [f"{TENANT}:gone"]
    assert r.json()["accounts"]["marked_reauth_required"] == []


def test_a_credential_that_fails_once_and_then_works_is_not_taken_out_of_service(client):
    """REAUTH_REQUIRED is a state only a person can leave -- `due_for_refresh`
    skips it, so the sweep can never lift what it writes. A false one therefore
    costs an operator a sign-in they did not need and the pool an account that
    was never broken.

    This is the shape audit finding 3 produces and which is still unfixed:
    nothing serialises the sweep, so two ticks can read the same refresh token,
    one exchanges it, and the other is told `invalid_grant` for a token that
    was merely rotated away from it."""
    _add(client, TENANT, "personal")
    dead = f"{TENANT}:personal"

    refresher = _SweepRefresher(
        {dead: "reauth_required"}, on_second={dead: "still_valid"}
    )
    r = _sweeping(client, refresher)

    assert refresher.confirmations == [dead], "the mark must be confirmed first"
    assert _account(client, dead)["state"] == "AVAILABLE"
    assert r.json()["accounts"]["reauth_required"] == [dead]
    assert r.json()["accounts"]["marked_reauth_required"] == []


def test_a_genuinely_dead_credential_is_still_marked_after_the_second_attempt(client):
    """The confirmation must not become a way for a dead account to stay
    invisible, which is the failure the whole write-back exists to end."""
    _add(client, TENANT, "personal")
    dead = f"{TENANT}:personal"

    refresher = _SweepRefresher({dead: "reauth_required"})
    _sweeping(client, refresher)

    assert refresher.confirmations == [dead]
    assert _account(client, dead)["state"] == "REAUTH_REQUIRED"


def test_registration_records_what_it_published_so_the_next_sweep_repeats_nothing(
    client, db
):
    """REGISTRATION IS A WRITER TOO, and it owes the ledger a record.

    The broker cannot read the base secret it just wrote -- it holds
    `secretmanager.versions.add` on it and not `versions.access` -- so the very
    next sweep would find a secret it cannot read and no record of what is in
    it, and publish one more identical version. That is the 1,741-version leak
    restarted by the one code path that already knew the answer, five minutes
    after every registration.

    The digest, never the token: this collection is readable by anything with
    `roles/datastore.user` on the project, which is a far wider circle than the
    secret's accessor binding.
    """
    from quota_broker.accounts import secret_name
    from quota_broker.publishledger import COLLECTION, fingerprint

    r = client.post(
        "/v1/accounts",
        json={"owner_tenant": TENANT, "label": "personal", "credential": _credential()},
    )
    assert r.status_code == 201, r.text

    base = secret_name(TENANT, "personal")
    record = db.docs[f"{COLLECTION}/{base}"]
    assert record["digest"] == fingerprint("at-abcdefghijklmnop")
    assert "at-abcdefghijklmnop" not in json.dumps(record), (
        "the ledger stores a digest so that reading it cannot hand anyone a "
        "live access token"
    )


def test_the_sweep_after_a_registration_adds_no_second_version(client, db):
    """End to end over the seam the two writers share.

    Registration publishes the access token and records it; the sweep that
    follows cannot read the secret back, finds the record, and writes nothing.
    Before the record was durable this cost one identical version per broker
    process -- four deploys, four rounds of them, on 2026-09-22.
    """
    from quota_broker.accounts import secret_name
    from quota_broker.credentials import CredentialRefresher

    r = client.post(
        "/v1/accounts",
        json={"owner_tenant": TENANT, "label": "personal", "credential": _credential()},
    )
    assert r.status_code == 201, r.text

    base = secret_name(TENANT, "personal")
    secrets = client.secrets
    before = len(secrets.versions[base])

    class _Unreadable:
        """The live IAM shape: writes accepted, reads refused."""

        def access(self, name):
            if name == base:
                raise PermissionError("permission denied")
            return secrets.access(name)

        def add_version(self, name, payload):
            secrets.add_version(name, payload)

    class _Endpoint:
        def exchange(self, refresh_token):
            raise AssertionError("a valid token must not be spent")

    for _ in range(3):   # three broker processes, i.e. three deploys
        out = CredentialRefresher(
            _Unreadable(),
            _Endpoint(),
            logger=logging.getLogger("test.sweep-after-registration"),
            ledger=client.app_ref.state.publish_ledger,
        ).refresh_secret(base, label="eng:personal")

    assert out.reason == "still_valid"
    assert len(secrets.versions[base]) == before, (
        "the sweep republished a token registration had already published and "
        "recorded"
    )
