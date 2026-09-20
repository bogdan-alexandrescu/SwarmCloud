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
    """A Secret Manager double that records every write."""

    def __init__(self) -> None:
        self.versions: dict[str, list[str]] = {}

    def access(self, name: str) -> str:
        if name not in self.versions:
            raise KeyError(name)
        return self.versions[name][-1]

    def add_version(self, name: str, payload: str) -> None:
        self.versions.setdefault(name, []).append(payload)


@pytest.fixture()
def client(broker):
    from fastapi.testclient import TestClient

    from quota_broker.accountstore import AccountStore
    from quota_broker.main import WorkerIdentity, create_app

    class PlatformIdentity(WorkerIdentity):
        def resolve(self, authorization):
            return None, True             # the platform, so every tenant is in scope

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
    base = f"swarm-account-{TENANT}-personal"

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
    assert refresher.calls == [(f"swarm-account-{TENANT}-personal", "personal")]


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
    assert f"swarm-account-{TENANT}-personal-refresh" in client.secrets.versions
