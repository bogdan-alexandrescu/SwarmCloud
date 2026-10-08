"""/v1/providers counts an account the tenant is LENT, the way admission does (#76).

THE DEFECT. `SubmissionService.providers()` answered `credential_registered`
with `name in tenant.credentials` alone. A keyless tenant served by an account
another tenant lent it read "no credential" for `anthropic`, while admission
(`scheduler.credentials.credential_for`) admitted its tasks on the pool and
they ran. The two disagreed about the same tenant.

WHAT THESE PIN, one case each: own key; lent account only; neither; a loan
that was withdrawn. Plus the edges that keep the answer admission's: a key is
reported before an account, the pool runs only the profiles that take a
subscription token, no broker means the pool serves nobody, and a broker that
cannot answer degrades the page instead of failing it.

Offline: FakeFirestore, StaticTokenVerifier, an in-memory pool. No
credentials, no emulator, no network.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from swarm_api.brokerclient import AccountPoolUnavailable
from swarm_api.main import create_app
from swarm_common.profiles import RUNNER_PROFILES

from .conftest import auth_header, seed_tenant

#: The anthropic profiles a subscription account can run: those whose secrets
#: take the subscription token. Read from the catalogue, not restated.
ON_ACCOUNT = sorted(
    p.name
    for p in RUNNER_PROFILES.values()
    if p.provider == "anthropic" and "CLAUDE_CODE_OAUTH_TOKEN" in (p.secrets or ())
)
ANTHROPIC = sorted(p.name for p in RUNNER_PROFILES.values() if p.provider == "anthropic")


def _account(owner: str, label: str, *, lend_to: tuple[str, ...] = (), provider: str = "anthropic") -> dict:
    """`account_to_api`'s fields that matter here. No key material."""
    return {
        "account_id": f"{owner}:{label}",
        "owner_tenant": owner,
        "label": label,
        "provider": provider,
        "state": "AVAILABLE",
        "lend_to": list(lend_to),
    }


class Pool:
    """The broker's listing, scoped the broker's way (`may_serve`)."""

    def __init__(self, *accounts: dict, error: Exception | None = None) -> None:
        self.accounts = list(accounts)
        self.error = error
        self.calls: list[str] = []

    def list_accounts(self, tenant_id: str) -> dict:
        self.calls.append(tenant_id)
        if self.error is not None:
            raise self.error
        return {
            "accounts": [
                dict(a)
                for a in self.accounts
                if a["owner_tenant"] == tenant_id or tenant_id in a["lend_to"]
            ],
            "tenant_id": tenant_id,
        }


def _client(api_context, pool: Pool | None) -> TestClient:
    app = create_app(api_context)
    if pool is not None:
        app.state.account_pool = pool
    return TestClient(app, raise_server_exceptions=False)


def _read(client: TestClient) -> tuple[dict, dict]:
    response = client.get("/v1/providers", headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    body = response.json()
    entry = next(p for p in body["providers"] if p["provider"] == "anthropic")
    return body, entry


def test_own_key_is_registered_as_the_tenant_key(api_context, db):
    seed_tenant(db, "eng", credentials=("anthropic",))
    pool = Pool(_account("research", "shared", lend_to=("eng",)))
    body, entry = _read(_client(api_context, pool))
    assert entry["credential_registered"] is True
    assert entry["credential_source"] == "tenant_key"
    # A key runs every profile of its provider, `browser` included.
    assert entry["runnable_profiles"] == ANTHROPIC


def test_a_lent_account_alone_counts_as_the_account_pool(api_context, db):
    seed_tenant(db, "eng", credentials=())
    pool = Pool(_account("research", "shared", lend_to=("eng",)))
    body, entry = _read(_client(api_context, pool))
    assert entry["credential_registered"] is True
    assert entry["credential_source"] == "account_pool"
    assert body["account_pool"] == "read"
    # Only the profiles that take a subscription token; never one that does not.
    assert ON_ACCOUNT, "no anthropic profile takes a subscription token; the case is vacuous"
    assert entry["runnable_profiles"] == ON_ACCOUNT
    # The account's provider only: a lent anthropic account serves no openai.
    openai = next(p for p in body["providers"] if p["provider"] == "openai")
    assert openai["credential_registered"] is False
    assert openai["credential_source"] is None


def test_neither_a_key_nor_an_account_is_no_credential(api_context, db):
    seed_tenant(db, "eng", credentials=())
    # research's account exists but is lent to nobody.
    pool = Pool(_account("research", "private"))
    body, entry = _read(_client(api_context, pool))
    assert entry["credential_registered"] is False
    assert entry["credential_source"] is None
    assert entry["runnable_profiles"] == []
    assert body["account_pool"] == "read"


def test_a_withdrawn_loan_counts_for_nothing(api_context, db):
    seed_tenant(db, "eng", credentials=())
    lent = _account("research", "shared", lend_to=("eng",))
    pool = Pool(lent)
    client = _client(api_context, pool)
    _, before = _read(client)
    assert before["credential_source"] == "account_pool"

    # research withdraws the loan: the account no longer names eng.
    lent["lend_to"] = []
    _, after = _read(client)
    assert after["credential_registered"] is False
    assert after["credential_source"] is None


def test_a_listing_that_still_carries_a_withdrawn_loan_is_not_trusted(api_context, db):
    seed_tenant(db, "eng", credentials=())

    class Unscoped(Pool):
        def list_accounts(self, tenant_id: str) -> dict:
            return {"accounts": [dict(a) for a in self.accounts], "tenant_id": tenant_id}

    _, entry = _read(_client(api_context, Unscoped(_account("research", "shared"))))
    assert entry["credential_registered"] is False


def test_an_account_the_tenant_owns_counts_too(api_context, db):
    seed_tenant(db, "eng", credentials=())
    _, entry = _read(_client(api_context, Pool(_account("eng", "primary"))))
    assert entry["credential_source"] == "account_pool"


def test_a_keyed_tenant_reads_no_pool(api_context, db):
    seed_tenant(db, "eng", credentials=("anthropic", "git", "openai"))
    pool = Pool(_account("research", "shared", lend_to=("eng",)))
    body, _ = _read(_client(api_context, pool))
    assert pool.calls == []
    assert body["account_pool"] == "not_asked"


def test_no_broker_means_the_pool_serves_nobody(api_context, db):
    seed_tenant(db, "eng", credentials=())
    # No pool injected and QUOTA_BROKER_URL unset, as admission's
    # `AccountPool.configured` reads it.
    assert not api_context.settings.quota_broker_url
    body, entry = _read(_client(api_context, None))
    assert body["account_pool"] == "not_configured"
    assert entry["credential_registered"] is False


def test_a_broker_that_cannot_answer_degrades_the_page(api_context, db):
    seed_tenant(db, "eng", credentials=())
    pool = Pool(error=AccountPoolUnavailable("the account pool is unavailable"))
    body, entry = _read(_client(api_context, pool))
    assert body["account_pool"] == "unreadable"
    assert entry["credential_registered"] is False
    assert entry["credential_source"] is None


@pytest.mark.parametrize("credentials", [(), ("anthropic",)])
def test_every_entry_carries_the_additive_fields(api_context, db, credentials):
    seed_tenant(db, "eng", credentials=credentials)
    body, _ = _read(_client(api_context, Pool()))
    for entry in body["providers"]:
        assert {"credential_registered", "credential_source", "runnable_profiles"} <= set(entry)
