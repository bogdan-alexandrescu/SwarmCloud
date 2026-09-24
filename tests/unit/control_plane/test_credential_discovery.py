"""Finding the tenants whose credentials need refreshing, and surviving failure.

Two hazards live here, and neither shows up in the refresher's own tests
because both are in the seam between the refresher and Secret Manager:

  1. `swarm-tenant-u-bogdan-anthropic-refresh` does not split unambiguously.
     Personal tenants are `u-<user>`, so taking the name apart on dashes can
     produce tenant `u` / provider `bogdan-anthropic`, and the refreshed
     credential would then be published to a secret belonging to nobody -- or,
     worse, to somebody else.

  2. A permission error while listing secrets must not 500 the sweep. The sweep
     is also what un-parks quota-throttled tenants; coupling the two would let
     one broken credential stall every tenant's recovery.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from quota_broker.credentials import REFRESH_SUFFIX
from quota_broker.main import WorkerIdentity, create_app
from quota_broker.secretstore import SecretManagerStore, SecretMissing

from .conftest import PROJECT


class _Secret:
    def __init__(self, name: str, labels: dict[str, str] | None = None) -> None:
        self.name = f"projects/{PROJECT}/secrets/{name}"
        self.labels = labels or {}


class _Client:
    """Only the three Secret Manager calls this store makes."""

    def __init__(self, secrets=(), payloads=None) -> None:
        self.secrets = list(secrets)
        self.payloads = dict(payloads or {})
        self.added: list[tuple[str, str]] = []
        self.list_filters: list[str] = []

    def list_secrets(self, request):
        self.list_filters.append(request["filter"])
        return list(self.secrets)

    def access_secret_version(self, request):
        from google.api_core import exceptions as gexc

        name = request["name"].split("/secrets/")[1].split("/versions/")[0]
        if name not in self.payloads:
            raise gexc.NotFound(name)

        class _P:
            payload = type("_D", (), {"data": self.payloads[name].encode()})()

        return _P()

    def add_secret_version(self, request):
        from google.api_core import exceptions as gexc

        name = request["parent"].split("/secrets/")[1]
        if name not in self.payloads:
            raise gexc.NotFound(name)
        self.added.append((name, request["payload"].data.decode()))


def _store(client: _Client) -> SecretManagerStore:
    return SecretManagerStore(PROJECT, client=client)


# -- discovery -------------------------------------------------------------

def test_a_dashed_tenant_id_is_read_from_labels_not_parsed_from_the_name():
    """`u-bogdan` is the personal-tenant form, and it contains a dash."""
    client = _Client([
        _Secret(
            f"swarm-tenant-u-bogdan-anthropic{REFRESH_SUFFIX}",
            {"tenant": "u-bogdan", "provider": "anthropic"},
        )
    ])
    assert _store(client).subscription_tenants() == [("u-bogdan", "anthropic")]


def test_only_refresh_secrets_are_swept():
    """The short-lived half is written BY the sweep; sweeping it would mean
    trying to refresh an access token, which has no refresh token in it."""
    client = _Client([
        _Secret("swarm-tenant-eng-anthropic", {"tenant": "eng", "provider": "anthropic"}),
        _Secret(f"swarm-tenant-eng-anthropic{REFRESH_SUFFIX}",
                {"tenant": "eng", "provider": "anthropic"}),
    ])
    assert _store(client).subscription_tenants() == [("eng", "anthropic")]


def test_an_unlabelled_refresh_secret_is_skipped_rather_than_guessed_at():
    """Guessing publishes a refreshed credential under a name that may belong
    to another tenant. Skipping loses a refresh; guessing loses isolation."""
    client = _Client([
        _Secret(f"swarm-tenant-mystery{REFRESH_SUFFIX}"),
        _Secret(f"swarm-tenant-eng-anthropic{REFRESH_SUFFIX}",
                {"tenant": "eng", "provider": "anthropic"}),
    ])
    assert _store(client).subscription_tenants() == [("eng", "anthropic")]


def test_discovery_is_scoped_to_tenant_credentials():
    client = _Client([])
    _store(client).subscription_tenants()
    assert client.list_filters == ["labels.component=tenant-credential"]


def test_duplicates_are_collapsed_so_a_tenant_refreshes_once_per_sweep():
    """Two refreshes in one tick would rotate twice and strand the first."""
    labels = {"tenant": "eng", "provider": "anthropic"}
    client = _Client([
        _Secret(f"swarm-tenant-eng-anthropic{REFRESH_SUFFIX}", labels),
        _Secret(f"swarm-tenant-eng-anthropic{REFRESH_SUFFIX}", labels),
    ])
    assert _store(client).subscription_tenants() == [("eng", "anthropic")]


# -- missing vs broken -----------------------------------------------------

def test_a_missing_secret_raises_KeyError_so_the_refresher_treats_it_as_normal():
    """The refresher catches KeyError specifically: an API-key tenant has no
    refresh secret and that is not an incident."""
    with pytest.raises(KeyError):
        _store(_Client()).access("swarm-tenant-eng-anthropic-refresh")


def test_adding_a_version_to_a_secret_that_does_not_exist_is_refused():
    """Creating it here would produce a secret with no tenant labels and no
    accessor binding -- unreadable by the worker that needs it, and the failure
    would surface much later as an unexplained auth error inside a job."""
    with pytest.raises(SecretMissing):
        _store(_Client()).add_version("swarm-tenant-eng-anthropic", "token")


def test_a_write_reaches_secret_manager_under_the_expected_name():
    client = _Client(payloads={"swarm-tenant-eng-anthropic": "old"})
    _store(client).add_version("swarm-tenant-eng-anthropic", "new")
    assert client.added == [("swarm-tenant-eng-anthropic", "new")]


# -- the sweep endpoint ----------------------------------------------------

class _Refresher:
    def __init__(self, outcomes=(), raises=None):
        self.outcomes, self.raises, self.seen = list(outcomes), raises, []

    def sweep(self, pairs, keep_going=None):
        self.seen.append(pairs)
        if self.raises:
            raise self.raises
        return self.outcomes


def _client(broker, refresher, tenants):
    return TestClient(
        create_app(
            broker,
            identity=WorkerIdentity(required=False),
            credential_refresher=refresher,
            subscription_tenants=tenants,
        ),
        raise_server_exceptions=False,
    )


def test_a_failing_credential_sweep_does_not_break_the_quota_sweep(broker):
    """The quota sweep is what un-parks throttled tenants. If a Secret Manager
    permission error could 500 it, one broken credential would stall every
    tenant's quota recovery -- a blast radius far wider than its cause."""

    def _boom():
        raise PermissionError("caller lacks secretmanager.secrets.list")

    response = _client(broker, _Refresher(), _boom).post("/v1/quota/sweep")
    assert response.status_code == 200
    assert response.json()["credentials"] == {"error": "PermissionError"}


def test_the_sweep_reports_which_tenants_need_a_human(broker):
    from quota_broker.credentials import RefreshOutcome

    refresher = _Refresher([
        RefreshOutcome("eng", "anthropic", True, "refreshed"),
        RefreshOutcome("u-bogdan", "anthropic", False, "reauth_required"),
        RefreshOutcome("research", "anthropic", False, "still_valid"),
    ])
    body = _client(broker, refresher, lambda: [("eng", "anthropic")]).post(
        "/v1/quota/sweep"
    ).json()

    assert body["credentials"]["examined"] == 3
    assert body["credentials"]["refreshed"] == 1
    assert body["credentials"]["reauth_required"] == ["u-bogdan"]
    assert refresher.seen == [[("eng", "anthropic")]]


def test_a_deployment_with_no_refresher_sweeps_quota_as_before(broker):
    """API-key-only deployments must not grow a credentials key they never had."""
    import os

    os.environ["CREDENTIAL_REFRESH_ENABLED"] = "false"
    try:
        client = TestClient(
            create_app(broker, identity=WorkerIdentity(required=False)),
            raise_server_exceptions=False,
        )
        response = client.post("/v1/quota/sweep")
    finally:
        del os.environ["CREDENTIAL_REFRESH_ENABLED"]

    assert response.status_code == 200
    assert "credentials" not in response.json()


# -- the account pool shares the tick ---------------------------------------

def test_the_account_sweep_and_the_credential_sweep_are_reported_separately(broker):
    """They shared one try block once, and the first thing that hid was a
    missing method on the account path masking a perfectly good credential
    result. "Credentials are broken" and "the pool is broken" want different
    people to do different things."""
    from quota_broker.credentials import RefreshOutcome

    class _Refresher:
        def sweep(self, pairs, keep_going=None):
            return [RefreshOutcome("eng", "anthropic", True, "refreshed")]

        def sweep_accounts(self, secrets, keep_going=None):
            raise RuntimeError("the pool is unreachable")

    class _Store:
        def list(self):
            return []

        def secret_for(self, account):
            return "unused"

    client = TestClient(
        create_app(
            broker,
            identity=WorkerIdentity(required=False),
            credential_refresher=_Refresher(),
            subscription_tenants=lambda: [("eng", "anthropic")],
            account_store=_Store(),
        ),
        raise_server_exceptions=False,
    )
    body = client.post("/v1/quota/sweep").json()

    # The credential sweep still reports its real result...
    assert body["credentials"]["refreshed"] == 1
    # ...and the account failure is visible rather than merged into it.
    assert body["accounts"] == {"error": "RuntimeError"}


def test_a_broken_account_pool_does_not_break_the_quota_sweep(broker):
    """The tick is also what un-parks throttled tenants."""
    class _Store:
        def list(self):
            raise PermissionError("caller lacks datastore.entities.list")

        def secret_for(self, account):
            return "unused"

    class _Refresher:
        def sweep(self, pairs, keep_going=None):
            return []

        def sweep_accounts(self, secrets, keep_going=None):
            return []

    response = TestClient(
        create_app(
            broker,
            identity=WorkerIdentity(required=False),
            credential_refresher=_Refresher(),
            subscription_tenants=lambda: [],
            account_store=_Store(),
        ),
        raise_server_exceptions=False,
    ).post("/v1/quota/sweep")

    assert response.status_code == 200
    assert response.json()["accounts"] == {"error": "PermissionError"}
