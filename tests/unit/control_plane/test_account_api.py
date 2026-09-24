"""swarm-api's account routes: a proxy with a tenant boundary on it.

These routes exist so a browser can manage the account pool, and they add
exactly one thing to the quota broker's own routes: the caller's tenant, taken
from the verified token. Everything worth testing here follows from that being
the ONLY thing they add.

WHAT THESE PIN

  * the tenant is the caller's, RESOLVED THROUGH `tenant_for` like every other
    tenant-scoped route -- so a tenant-id collision between two groups and an
    admin-disabled tenant are refused here too, and a body or query that says
    otherwise is ignored or refused -- never obeyed;
  * the pool holds Claude SUBSCRIPTIONS and nothing else: there is no API-key
    account and no credential-kind selector, so `provider` has exactly one
    accepted value;
  * a redirect is REFUSED rather than followed, because following one would
    hand this service's own ID token to whatever host the `Location` names;
  * a 401/403 from the broker is about THIS SERVICE's identity, never the
    human caller's permissions;
  * an account LENT to this tenant is visible and usable and is not theirs to
    pause, re-lend, refresh or delete;
  * a credential goes in and never comes back -- not in a response, not in a
    log line, not in an error message;
  * a broker that is unreachable is a 503 that says ACCOUNT POOL, with its own
    code, because the group-resolution 503 this API already returns has a
    completely different remedy;
  * "not configured" refuses. There is no local fallback and there must not be
    one: the broker is the platform's single writer for these credentials.

Nothing here needs a credential, a metadata server or an outbound network. The
broker client is injected, and the tests that exercise the real client inject a
transport instead -- except the redirect test, which binds two throwaway HTTP
servers on 127.0.0.1 because the whole point of it is that the REAL urllib path
never opens the second connection. Loopback only: nothing leaves the machine.
"""

from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from swarm_api.brokerclient import (
    METADATA_IDENTITY_URL,
    AccountPoolUnavailable,
    BrokerClient,
    BrokerRedirected,
    BrokerUnreachable,
    MetadataIdToken,
    StaticIdToken,
    urllib_transport,
)
from swarm_api.main import create_app

from .conftest import api_settings, auth_header, seed_tenant

#: What an operator pastes: Claude Code's keychain item, wrapper and all.
CREDENTIAL = json.dumps(
    {
        "claudeAiOauth": {
            "accessToken": "at-do-not-echo-this",
            "refreshToken": "rt-do-not-echo-this",
            "expiresAt": 1_800_000_000_000,
        }
    }
)


def account(
    owner_tenant: str,
    label: str,
    *,
    lend_to: tuple[str, ...] = (),
    state: str = "AVAILABLE",
    stale: bool = False,
) -> dict:
    """`account_to_api`'s exact shape. No key material, not even a length."""
    return {
        "account_id": f"{owner_tenant}:{label}",
        "owner_tenant": owner_tenant,
        "label": label,
        "provider": "anthropic",
        "state": state,
        "reason": "",
        "lend_to": list(lend_to),
        "assigned": 0,
        "windows": {
            "five_hour": {
                "utilization": 0.4,
                "resets_at": "2026-09-20T18:00:00+00:00",
                "reset": False,
            }
        },
        "observed_at": None if stale else "2026-09-20T17:00:00+00:00",
        "stale": stale,
    }


class FakePool:
    """The broker's account routes, in memory, with the same method names.

    It implements the `AccountPool` protocol the routes depend on, so the routes
    under test are the shipped ones -- nothing is patched and no import is
    monkeyed.
    """

    def __init__(self, *accounts: dict, error: Exception | None = None) -> None:
        self.accounts = {a["account_id"]: dict(a) for a in accounts}
        self.calls: list[tuple] = []
        self.registered: list[dict] = []
        self._error = error

    def _maybe_fail(self) -> None:
        if self._error is not None:
            raise self._error

    def list_accounts(self, tenant_id: str) -> dict:
        self.calls.append(("list_accounts", tenant_id))
        self._maybe_fail()
        visible = [
            dict(a)
            for a in self.accounts.values()
            if a["owner_tenant"] == tenant_id or tenant_id in a["lend_to"]
        ]
        return {"accounts": visible, "tenant_id": tenant_id}

    def register(self, *, owner_tenant, label, provider, lend_to, credential) -> dict:
        self.calls.append(("register", owner_tenant, label, provider))
        self._maybe_fail()
        self.registered.append(
            {
                "owner_tenant": owner_tenant,
                "label": label,
                "provider": provider,
                "lend_to": list(lend_to),
                "credential": credential,
            }
        )
        made = account(owner_tenant, label, lend_to=tuple(lend_to))
        self.accounts[made["account_id"]] = made
        return {
            "account": made,
            "expires_at": "2026-09-21T01:00:00+00:00",
            "note": "stored write-only; no route in this service returns key material",
        }

    def set_lending(self, account_id: str, *, lend_to: list[str]) -> dict:
        self.calls.append(("set_lending", account_id, tuple(lend_to)))
        self._maybe_fail()
        self.accounts[account_id]["lend_to"] = list(lend_to)
        return {"account": dict(self.accounts[account_id])}

    def set_state(self, account_id: str, *, state: str, reason: str) -> dict:
        self.calls.append(("set_state", account_id, state, reason))
        self._maybe_fail()
        self.accounts[account_id]["state"] = state
        self.accounts[account_id]["reason"] = reason
        return {"account": dict(self.accounts[account_id])}

    def refresh(self, account_id: str) -> dict:
        self.calls.append(("refresh", account_id))
        self._maybe_fail()
        return {
            "refresh": {
                "tenant_id": self.accounts[account_id]["owner_tenant"],
                "provider": "anthropic",
                "refreshed": True,
                "reason": "rotated",
                "expires_at": "2026-09-21T01:00:00+00:00",
            },
            "account": dict(self.accounts[account_id]),
        }

    def remove(self, account_id: str) -> dict:
        self.calls.append(("remove", account_id))
        self._maybe_fail()
        del self.accounts[account_id]
        return {"removed": account_id, "secret": "retained"}

    @property
    def mutations(self) -> list[tuple]:
        """Every call that was not a read. The set that must stay empty on 404."""
        return [c for c in self.calls if c[0] != "list_accounts"]


@pytest.fixture
def pool() -> FakePool:
    # eng owns one; research owns one and lends it to eng. The lent account is
    # what separates "may run on" from "may administer".
    return FakePool(
        account("eng", "primary"),
        account("research", "shared", lend_to=("eng",)),
    )


@pytest.fixture
def client(api_context, pool) -> TestClient:
    app = create_app(api_context)
    app.state.account_pool = pool
    return TestClient(app, raise_server_exceptions=False)


# --------------------------------------------------------------------------
# The tenant is the caller's
# --------------------------------------------------------------------------


def test_list_is_scoped_to_the_caller_not_to_anything_in_the_request(client, pool):
    response = client.get(
        "/v1/accounts", params={"tenant_id": "research"}, headers=auth_header("alice")
    )

    assert response.status_code == 200
    body = response.json()
    # The query said "research". The token said eng, and the token wins -- the
    # query parameter is not a field this route has.
    assert ("list_accounts", "eng") in pool.calls
    assert ("list_accounts", "research") not in pool.calls
    assert body["tenant_id"] == "eng"
    assert {a["account_id"] for a in body["accounts"]} == {"eng:primary", "research:shared"}


def test_list_marks_a_stale_reading_rather_than_rendering_it_as_a_number(api_context):
    """`stale` survives the proxy. It is a different claim from "utilisation 0"."""
    stale_pool = FakePool(account("eng", "primary", stale=True))
    app = create_app(api_context)
    app.state.account_pool = stale_pool
    local = TestClient(app, raise_server_exceptions=False)

    body = local.get("/v1/accounts", headers=auth_header("alice")).json()

    assert body["accounts"][0]["stale"] is True
    assert body["accounts"][0]["observed_at"] is None


@pytest.mark.parametrize(
    "method,path,payload",
    [
        ("get", "/v1/accounts", None),
        ("post", "/v1/accounts/eng:primary/refresh", None),
        ("delete", "/v1/accounts/eng:primary", None),
        ("put", "/v1/accounts/eng:primary/lending", {"lend_to": ["research"]}),
        ("put", "/v1/accounts/eng:primary/state", {"state": "PAUSED", "reason": "x"}),
    ],
)
def test_a_tenant_id_collision_between_two_groups_is_refused_here_too(
    client, pool, db, method, path, payload
):
    """`eng@saga.xyz` and `eng@partner.com` both derive tenant `eng`.

    The frozen `tenant_id_for_group` slugs the local part only, so two
    different Google groups collide on one id. `Store.ensure_tenant` refuses
    the second with a 409 everywhere else in this API; without going through
    `tenant_for`, the account routes would be the one place it did not -- and
    the member of the losing group could list, re-lend, delete and REFRESH the
    winning group's account. A refresh REVOKES the token in flight, so that is
    not an information leak, it is every pod on that account failing at once.
    """
    seed_tenant(db, "eng")
    # The document belongs to a different group with the same slug. alice is in
    # eng@saga.xyz.
    db.docs["tenants/eng"]["principal"] = "eng@partner.com"

    kwargs = {"headers": auth_header("alice")}
    if payload is not None:
        kwargs["json"] = payload
    response = getattr(client, method)(path, **kwargs)

    assert response.status_code == 409, response.text
    body = response.json()
    assert body["code"] == "conflict"
    assert body["detail"]["registered_principal"] == "eng@partner.com"
    # Refused BEFORE the broker was asked anything at all, so the other group's
    # account was neither read nor touched.
    assert pool.calls == []
    assert "eng:primary" in pool.accounts


@pytest.mark.parametrize(
    "method,path,payload",
    [
        ("get", "/v1/accounts", None),
        ("post", "/v1/accounts", {"label": "second", "credential": CREDENTIAL}),
        ("post", "/v1/accounts/eng:primary/refresh", None),
        ("delete", "/v1/accounts/eng:primary", None),
    ],
)
def test_a_disabled_tenant_cannot_administer_accounts_either(
    client, pool, db, method, path, payload
):
    """An admin-disabled tenant is refused on every other route in this API.

    Reading `auth.tenant_id` directly would have left this surface -- the one
    that holds live subscription credentials -- serving a tenant an admin had
    switched off.
    """
    seed_tenant(db, "eng", enabled=False)

    kwargs = {"headers": auth_header("alice")}
    if payload is not None:
        kwargs["json"] = payload
    response = getattr(client, method)(path, **kwargs)

    assert response.status_code == 403, response.text
    assert response.json()["code"] == "forbidden"
    assert "disabled" in response.json()["message"]
    assert pool.calls == []
    # And no credential was forwarded on the way to finding out.
    assert pool.registered == []


def test_register_files_the_account_under_the_callers_tenant(client, pool):
    response = client.post(
        "/v1/accounts",
        headers=auth_header("alice"),
        json={"label": "second", "credential": CREDENTIAL, "lend_to": ["research"]},
    )

    assert response.status_code == 201
    assert pool.registered[0]["owner_tenant"] == "eng"
    assert pool.registered[0]["label"] == "second"
    assert pool.registered[0]["provider"] == "anthropic"
    assert pool.registered[0]["lend_to"] == ["research"]
    # Forwarded verbatim: parse_credential in the broker is the single reader of
    # this shape.
    assert pool.registered[0]["credential"] == CREDENTIAL


def test_an_owner_tenant_naming_someone_else_is_refused_not_silently_overridden(
    client, pool
):
    """A silent override would leave the operator believing they had registered
    an account into a pool they had not touched -- with a live credential."""
    response = client.post(
        "/v1/accounts",
        headers=auth_header("alice"),
        json={
            "owner_tenant": "research",
            "label": "stolen",
            "credential": CREDENTIAL,
        },
    )

    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"
    assert "owner_tenant" in response.json()["message"]
    # And nothing was written anywhere on the way to finding that out.
    assert pool.registered == []


def test_an_owner_tenant_agreeing_with_the_token_is_accepted_and_still_not_used(
    client, pool
):
    """The UI mirrors the broker's body shape, which carries the field.

    Accepting it costs nothing as long as it is COMPARED and never USED: the
    value written is `auth.tenant_id` on both paths.
    """
    response = client.post(
        "/v1/accounts",
        headers=auth_header("alice"),
        json={"owner_tenant": "eng", "label": "second", "credential": CREDENTIAL},
    )

    assert response.status_code == 201
    assert pool.registered[0]["owner_tenant"] == "eng"


def test_register_returns_no_key_material_and_says_so(client, pool):
    response = client.post(
        "/v1/accounts",
        headers=auth_header("alice"),
        json={"label": "second", "credential": CREDENTIAL},
    )

    body = response.json()
    assert body["note"] == (
        "stored write-only; no read path in this API can return key material"
    )
    raw = response.text
    for secret in ("at-do-not-echo-this", "rt-do-not-echo-this", CREDENTIAL):
        assert secret not in raw
    # Not even a length, which is a real hint about a secret.
    assert set(body["account"]) == {
        "account_id",
        "owner_tenant",
        "label",
        "provider",
        "state",
        "reason",
        "lend_to",
        "assigned",
        "windows",
        "observed_at",
        "stale",
    }


@pytest.mark.parametrize("provider", ["acme", "openai"])
def test_the_pool_takes_subscriptions_only_so_provider_is_not_a_choice(
    client, pool, provider
):
    """One kind of credential, so one accepted value -- `openai` included.

    An account here is an OAuth pair the broker rotates on a timer. There is no
    API-key account in the pool: a provider with no subscription to refresh
    would be a pool entry nothing in the platform could ever renew, which is
    the shape that looks healthy and dies at its first expiry.
    """
    response = client.post(
        "/v1/accounts",
        headers=auth_header("alice"),
        json={"label": "second", "provider": provider, "credential": CREDENTIAL},
    )

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_failed"
    assert body["detail"]["known_providers"] == ["anthropic"]
    assert pool.registered == []


# --------------------------------------------------------------------------
# Lending is a right to RUN, not a right to administer
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path,payload",
    [
        ("put", "/v1/accounts/research:shared/state", {"state": "PAUSED", "reason": "x"}),
        ("put", "/v1/accounts/research:shared/lending", {"lend_to": []}),
        ("post", "/v1/accounts/research:shared/refresh", None),
        ("delete", "/v1/accounts/research:shared", None),
    ],
)
def test_a_borrower_may_not_administer_the_lenders_account(
    client, pool, method, path, payload
):
    kwargs = {"headers": auth_header("alice")}
    if payload is not None:
        kwargs["json"] = payload

    response = getattr(client, method)(path, **kwargs)

    # 404, not 403: "not yours" and "does not exist" must read the same from
    # outside, exactly as another tenant's task id does.
    assert response.status_code == 404, response.text
    assert response.json()["code"] == "not_found"
    assert pool.mutations == []
    # Visible, though -- it is still an account this tenant may run on.
    listed = client.get("/v1/accounts", headers=auth_header("alice")).json()
    assert "research:shared" in {a["account_id"] for a in listed["accounts"]}


def test_another_tenants_account_is_not_reachable_at_all(client, pool):
    response = client.delete("/v1/accounts/eng:primary", headers=auth_header("bob"))

    assert response.status_code == 404
    assert pool.mutations == []
    assert "eng:primary" in pool.accounts


# --------------------------------------------------------------------------
# What the owner can do
# --------------------------------------------------------------------------


def test_owner_can_pause_relend_refresh_and_remove(client, pool):
    paused = client.put(
        "/v1/accounts/eng:primary/state",
        headers=auth_header("alice"),
        json={"state": "PAUSED", "reason": "operator paused"},
    )
    assert paused.status_code == 200
    assert paused.json()["account"]["state"] == "PAUSED"
    assert paused.json()["account"]["reason"] == "operator paused"

    lent = client.put(
        "/v1/accounts/eng:primary/lending",
        headers=auth_header("alice"),
        json={"lend_to": ["research"]},
    )
    assert lent.status_code == 200
    assert lent.json()["account"]["lend_to"] == ["research"]

    refreshed = client.post(
        "/v1/accounts/eng:primary/refresh", headers=auth_header("alice")
    )
    assert refreshed.status_code == 200
    # The refresh outcome carries tenant, provider, a boolean, a reason and an
    # expiry -- and no token.
    assert refreshed.json()["refresh"]["refreshed"] is True
    assert "at-do-not-echo-this" not in refreshed.text

    removed = client.delete("/v1/accounts/eng:primary", headers=auth_header("alice"))
    assert removed.status_code == 200
    assert removed.json() == {"removed": "eng:primary", "secret": "retained"}
    assert "eng:primary" not in pool.accounts


def test_a_state_the_broker_rejects_comes_back_as_its_own_422(api_context):
    """The broker owns the state machine; this API does not keep a second copy."""
    from swarm_api.errors import ValidationFailed

    broken = FakePool(account("eng", "primary"))

    def refuse(account_id, *, state, reason):
        raise ValidationFailed(
            f"unknown account state {state!r}; known: AVAILABLE, PAUSED, "
            "DRAINING, REAUTH_REQUIRED"
        )

    broken.set_state = refuse  # type: ignore[method-assign]
    app = create_app(api_context)
    app.state.account_pool = broken
    local = TestClient(app, raise_server_exceptions=False)

    response = local.put(
        "/v1/accounts/eng:primary/state",
        headers=auth_header("alice"),
        json={"state": "SLEEPY", "reason": ""},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "validation_failed"
    assert "REAUTH_REQUIRED" in response.json()["message"]


# --------------------------------------------------------------------------
# The 503 that says ACCOUNT POOL
# --------------------------------------------------------------------------


def test_an_unreachable_broker_is_a_503_naming_the_account_pool(api_context):
    down = FakePool(
        error=AccountPoolUnavailable(
            "the account pool is unavailable: could not reach the quota broker "
            "at quota-broker.example (Connection refused)"
        )
    )
    app = create_app(api_context)
    app.state.account_pool = down
    local = TestClient(app, raise_server_exceptions=False)

    response = local.get("/v1/accounts", headers=auth_header("alice"))

    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "account_pool_unavailable"
    assert "account pool" in body["message"]


def test_the_two_503s_this_api_can_return_are_distinguishable(db, tokens, api_context):
    """Same status, different remedies: one is the broker, one is Cloud Identity.

    An operator who cannot tell them apart from the response spends the incident
    in the wrong system, which is the entire reason `account_pool_unavailable`
    exists rather than a second use of `upstream_unavailable`.
    """
    from swarm_api.auth import StaticTokenVerifier
    from swarm_api.credentials import InMemoryCredentials
    from swarm_api.deps import build_context
    from swarm_api.groups import GroupLookupError
    from swarm_api.waker import NullWaker

    class BrokenGroups:
        def groups_for(self, member_email, candidate_groups):
            raise GroupLookupError("cloud identity is having a bad minute")

    groups_down = TestClient(
        create_app(
            build_context(
                settings=api_settings(),
                db=db,
                verifier=StaticTokenVerifier(tokens),
                groups=BrokenGroups(),
                credentials=InMemoryCredentials(),
                waker=NullWaker(),
            )
        ),
        raise_server_exceptions=False,
    )
    broker_down_app = create_app(api_context)
    broker_down_app.state.account_pool = FakePool(
        error=AccountPoolUnavailable("the account pool is unavailable: broker down")
    )
    broker_down = TestClient(broker_down_app, raise_server_exceptions=False)

    from_groups = groups_down.get("/v1/accounts", headers=auth_header("alice"))
    from_broker = broker_down.get("/v1/accounts", headers=auth_header("alice"))

    assert from_groups.status_code == from_broker.status_code == 503
    assert from_groups.json()["code"] == "upstream_unavailable"
    assert from_broker.json()["code"] == "account_pool_unavailable"
    assert from_groups.json()["code"] != from_broker.json()["code"]


def test_no_broker_configured_refuses_rather_than_accepting_anything(db, tokens):
    """No QUOTA_BROKER_URL means refuse. There is no local path to fall back to.

    This one deliberately does NOT inject a pool: it exercises the real
    dependency, which builds the real client from settings and never leaves the
    process because the base URL is empty.
    """
    from swarm_api.auth import StaticTokenVerifier
    from swarm_api.credentials import InMemoryCredentials
    from swarm_api.deps import build_context
    from swarm_api.groups import StaticGroups
    from swarm_api.waker import NullWaker

    ctx = build_context(
        settings=api_settings(quota_broker_url=""),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups({"alice@saga.xyz": ("eng@saga.xyz",)}),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
    )
    local = TestClient(create_app(ctx), raise_server_exceptions=False)

    response = local.post(
        "/v1/accounts",
        headers=auth_header("alice"),
        json={"label": "primary", "credential": CREDENTIAL},
    )

    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "account_pool_unavailable"
    assert "QUOTA_BROKER_URL" in body["message"]


# --------------------------------------------------------------------------
# The client itself: error mapping and the service-to-service identity
# --------------------------------------------------------------------------


def _recording_transport(status: int, payload):
    seen: list[dict] = []

    def transport(method, url, *, headers, body=None, timeout=15.0):
        seen.append(
            {"method": method, "url": url, "headers": dict(headers), "body": body}
        )
        return status, payload

    return transport, seen


def test_client_sends_an_id_token_minted_for_the_configured_audience():
    """The audience is NOT the URL. terraform pins a custom one on the broker."""
    transport, seen = _recording_transport(200, {"accounts": [], "tenant_id": "eng"})
    client = BrokerClient(
        base_url="https://swarm-quota-broker-abc-uc.a.run.app",
        audience="https://swarm-quota-broker.dev.swarm.internal",
        tokens=StaticIdToken("id-token-for-the-custom-audience"),
        transport=transport,
    )

    client.list_accounts("eng")

    assert seen[0]["url"] == (
        "https://swarm-quota-broker-abc-uc.a.run.app/v1/accounts?tenant_id=eng"
    )
    assert seen[0]["headers"]["Authorization"] == "Bearer id-token-for-the-custom-audience"


def test_a_brokers_json_403_is_this_services_identity_not_the_callers_fault():
    """The broker cannot send this service a tenant-level 403, in either shape.

    swarm-api authenticates to the broker as a PLATFORM caller, and the
    broker's `_authorize` returns immediately for one -- so every 401/403 it
    can answer with is about swarm-api's OWN service account: not listed in
    PLATFORM_SERVICE_ACCOUNTS, token verification failed, no bearer token. Two
    of those render as JSON `{"code": "forbidden"}` from the broker's own
    handler, and it is exactly the state a deployment is in on day one before
    the service account is added to that list.

    Re-raising it as the caller's `Forbidden` puts "you are not permitted" on
    an operator's account screen for a platform IAM problem no human permission
    could fix, and sends them into the wrong system. It is a 503 that names the
    grants instead.
    """
    from swarm_api.errors import Forbidden

    forbidden, _ = _recording_transport(
        403,
        {
            "code": "forbidden",
            "message": "caller is not a swarm worker service account",
        },
    )

    with pytest.raises(AccountPoolUnavailable) as exc:
        BrokerClient(
            base_url="https://broker.example",
            tokens=StaticIdToken("t"),
            transport=forbidden,
        ).refresh("eng:primary")

    assert exc.value.status_code == 503
    assert exc.value.code == "account_pool_unavailable"
    assert not isinstance(exc.value, Forbidden)
    # The broker's own words are kept -- they are token-free by contract and
    # they say which of the three identity failures it was -- and the remedy
    # is named alongside them.
    assert "caller is not a swarm worker service account" in exc.value.message
    assert "PLATFORM_SERVICE_ACCOUNTS" in exc.value.message


def test_client_maps_the_brokers_validation_error_to_422():
    from swarm_api.errors import ValidationFailed

    invalid, _ = _recording_transport(
        422, {"code": "validation_failed", "message": "no account 'eng:nope'"}
    )

    with pytest.raises(ValidationFailed) as validation_error:
        BrokerClient(
            base_url="https://broker.example",
            tokens=StaticIdToken("t"),
            transport=invalid,
        ).remove("eng:nope")
    assert validation_error.value.status_code == 422
    assert "eng:nope" in validation_error.value.message


def test_an_iam_rejection_is_not_reported_as_the_callers_fault():
    """Cloud Run's IAM check answers 403 with HTML, before the app is reached.

    Reporting that as the caller's 403 would send an operator looking for a
    permission the human does not need. It is this service's own identity that
    was refused, so it is a 503 that says which grant is missing.
    """
    transport, _ = _recording_transport(403, "<html>Forbidden</html>")

    with pytest.raises(AccountPoolUnavailable) as exc:
        BrokerClient(
            base_url="https://broker.example",
            tokens=StaticIdToken("t"),
            transport=transport,
        ).list_accounts("eng")

    assert exc.value.status_code == 503
    assert exc.value.code == "account_pool_unavailable"
    assert "run.invoker" in exc.value.message


def test_a_transport_failure_never_carries_the_request_body():
    """The register body is a pasted OAuth credential. It must not reach a message."""

    def transport(method, url, *, headers, body=None, timeout=15.0):
        raise BrokerUnreachable("Connection refused")

    with pytest.raises(AccountPoolUnavailable) as exc:
        BrokerClient(
            base_url="https://broker.example",
            tokens=StaticIdToken("t"),
            transport=transport,
        ).register(
            owner_tenant="eng",
            label="primary",
            provider="anthropic",
            lend_to=[],
            credential=CREDENTIAL,
        )

    message = exc.value.message
    assert exc.value.status_code == 503
    assert "broker.example" in message
    assert "rt-do-not-echo-this" not in message
    assert "at-do-not-echo-this" not in message


def _jwt(exp: int) -> str:
    def part(payload: dict) -> str:
        raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
        return raw.rstrip("=")

    return f"{part({'alg': 'RS256'})}.{part({'exp': exp, 'aud': 'broker'})}.sig"


def test_metadata_token_is_asked_for_per_audience_and_cached_until_expiry():
    clock = {"now": 1_000.0}
    minted: list[str] = []

    def transport(method, url, *, headers, body=None, timeout=15.0):
        minted.append(url)
        assert headers == {"Metadata-Flavor": "Google"}
        return 200, _jwt(exp=int(clock["now"]) + 3600)

    source = MetadataIdToken(transport=transport, now=lambda: clock["now"])

    first = source.token("https://swarm-quota-broker.dev.swarm.internal")
    second = source.token("https://swarm-quota-broker.dev.swarm.internal")

    assert first == second
    assert len(minted) == 1, "a cached token must not cost a metadata round trip"
    assert minted[0].startswith(METADATA_IDENTITY_URL + "?")
    assert "audience=https%3A%2F%2Fswarm-quota-broker.dev.swarm.internal" in minted[0]

    # Past the skew window, it is minted again rather than sent expired.
    clock["now"] += 3600
    source.token("https://swarm-quota-broker.dev.swarm.internal")
    assert len(minted) == 2


def test_metadata_failures_refuse_and_never_echo_a_token():
    def unreachable(method, url, *, headers, body=None, timeout=15.0):
        raise BrokerUnreachable("Name or service not known")

    with pytest.raises(AccountPoolUnavailable) as exc:
        MetadataIdToken(transport=unreachable).token("https://broker.example")
    assert "metadata server" in exc.value.message

    def refused(method, url, *, headers, body=None, timeout=15.0):
        return 404, "no identity"

    with pytest.raises(AccountPoolUnavailable) as exc:
        MetadataIdToken(transport=refused).token("https://broker.example")
    assert "HTTP 404" in exc.value.message

    # And an unconfigured audience refuses rather than minting an unusable
    # token: an ID token with no `aud` is exactly what a service that forgot to
    # pin one would accept.
    with pytest.raises(AccountPoolUnavailable) as exc:
        MetadataIdToken(transport=refused).token("")
    assert "audience" in exc.value.message


# --------------------------------------------------------------------------
# "Not configured" refuses: the audience
# --------------------------------------------------------------------------


class RecordingTokens:
    """An IdTokenSource that remembers which audience it was asked for."""

    def __init__(self) -> None:
        self.audiences: list[str] = []

    def token(self, audience: str) -> str:
        self.audiences.append(audience)
        return "id-token"


def test_the_audience_falls_back_to_the_url_only_where_that_is_correct():
    """Local development only. Anywhere else an unset audience REFUSES.

    Terraform pins a CUSTOM audience on the broker
    (`https://swarm-quota-broker.<env>.swarm.internal`), which is not its
    service URL, and the broker verifies `aud` against exactly that string. So
    falling back to the URL in a deployed environment does not "probably work"
    -- it mints a token the broker is guaranteed to reject, and the rejection
    arrives as an identity 403 that reads like the operator's fault.
    """
    transport, _ = _recording_transport(200, {"accounts": [], "tenant_id": "eng"})
    tokens = RecordingTokens()

    # A local broker: the URL genuinely is the audience.
    BrokerClient(
        base_url="http://localhost:8081", tokens=tokens, transport=transport
    ).list_accounts("eng")
    assert tokens.audiences == ["http://localhost:8081"]

    # A deployed one with no audience configured: refused at construction, so
    # no token is ever minted for the wrong value.
    with pytest.raises(AccountPoolUnavailable) as exc:
        BrokerClient(
            base_url="https://swarm-quota-broker-abc-uc.a.run.app",
            require_audience=True,
            tokens=tokens,
            transport=transport,
        )
    assert "QUOTA_BROKER_AUDIENCE" in exc.value.message
    assert tokens.audiences == ["http://localhost:8081"]

    # Configured, and it is the value that gets used.
    BrokerClient(
        base_url="https://swarm-quota-broker-abc-uc.a.run.app",
        audience="https://swarm-quota-broker.dev.swarm.internal",
        require_audience=True,
        tokens=tokens,
        transport=transport,
    ).list_accounts("eng")
    assert tokens.audiences[-1] == "https://swarm-quota-broker.dev.swarm.internal"


def test_a_deployed_environment_with_no_audience_is_a_503_naming_the_variable(
    db, tokens
):
    """The shipped path, not a hand-built client: settings -> dependency -> 503.

    `hardened` is what turns the requirement on, the same switch that makes the
    human-facing token audience mandatory in `deps.build_context`.
    """
    from swarm_api.auth import StaticTokenVerifier
    from swarm_api.credentials import InMemoryCredentials
    from swarm_api.deps import build_context
    from swarm_api.groups import StaticGroups
    from swarm_api.waker import NullWaker

    ctx = build_context(
        settings=api_settings(
            core_overrides={"environment": "staging"},
            quota_broker_url="https://swarm-quota-broker-abc-uc.a.run.app",
            quota_broker_audience="",
        ),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups({"alice@saga.xyz": ("eng@saga.xyz",)}),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
    )
    local = TestClient(create_app(ctx), raise_server_exceptions=False)

    response = local.get("/v1/accounts", headers=auth_header("alice"))

    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "account_pool_unavailable"
    assert "QUOTA_BROKER_AUDIENCE" in body["message"]


# --------------------------------------------------------------------------
# A redirect is refused, not followed
# --------------------------------------------------------------------------


def _loopback_pair():
    """One host that 302s to a second host that records what it received.

    Two throwaway servers on 127.0.0.1. Nothing leaves the machine, and the
    point of the second one is to stay EMPTY: `urllib.request.urlopen` follows
    a redirect and re-attaches every header it was given, including
    `Authorization`, so with the stock opener this recorder would hold this
    service's ID token.
    """
    received: list[dict] = []

    class Elsewhere(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's spelling
            received.append(dict(self.headers))
            body = b'{"accounts": [], "tenant_id": "eng"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # noqa: D102 - silence the test output
            pass

    elsewhere = ThreadingHTTPServer(("127.0.0.1", 0), Elsewhere)
    target = f"http://127.0.0.1:{elsewhere.server_address[1]}/v1/accounts"

    class Redirector(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(302)
            self.send_header("Location", target)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):  # noqa: D102
            pass

    redirector = ThreadingHTTPServer(("127.0.0.1", 0), Redirector)
    for server in (elsewhere, redirector):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{redirector.server_address[1]}"
    return base, received, (elsewhere, redirector)


def test_a_302_does_not_carry_the_service_identity_token_onward():
    """The transport refuses the redirect, and the new host is never contacted.

    The header on these requests is a full impersonation of swarm-api to the
    quota broker -- the single writer of every subscription credential in the
    platform. `QUOTA_BROKER_URL` pointing at anything that redirects (a bare
    domain, an apex-to-www rule, a load balancer in front of the wrong backend)
    would otherwise deliver that identity to whatever host the `Location`
    names, and nothing in the logs would say so.
    """
    base, received, servers = _loopback_pair()
    token = "service-id-token-must-not-be-forwarded"
    try:
        # The transport itself: refused, and refused as its own error type.
        with pytest.raises(BrokerRedirected) as raw:
            urllib_transport(
                "GET",
                f"{base}/v1/accounts",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                },
                timeout=5.0,
            )

        assert isinstance(raw.value, BrokerUnreachable)
        assert "redirect" in str(raw.value)
        assert received == [], "the redirect target must never be contacted"

        # And through the shipped client, which uses that transport by default:
        # a 503 that names the account pool and the URL, with no token in it.
        with pytest.raises(AccountPoolUnavailable) as exc:
            BrokerClient(
                base_url=base,
                audience="https://swarm-quota-broker.dev.swarm.internal",
                tokens=StaticIdToken(token),
                timeout=5.0,
            ).list_accounts("eng")

        assert exc.value.status_code == 503
        assert exc.value.code == "account_pool_unavailable"
        assert "redirect" in exc.value.message
        assert "QUOTA_BROKER_URL" in exc.value.message
        assert token not in exc.value.message
        assert received == [], "the redirect target must never be contacted"
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


# -- the seam itself -------------------------------------------------------


def test_every_path_the_ui_calls_exists_on_this_api():
    """THE TEST FOR THE FAILURE THAT HAPPENED THREE TIMES IN ONE DAY.

    Twice a component gained a route, another component gained the call, and
    nothing was built in between: swarm-api had no broker URL, the worker had
    no broker URL, and then the browser asked swarm-api for /v1/accounts/authorize
    -- a path swarm-api did not have. Each time both ends were written and the
    seam was not, and each time it was found by a probe against the deployed
    service rather than by a test.

    This reads the paths the UI actually calls out of api.ts and asserts this
    router serves every one. It is deliberately a string comparison against the
    real client: a mock would agree with whatever the server happens to do.
    """
    import re
    from pathlib import Path

    from swarm_api.routes.accounts import router

    api_ts = Path(__file__).resolve().parents[3] / "apps/swarm-ui/src/api.ts"
    if not api_ts.exists():  # pragma: no cover - the UI is not always checked out
        pytest.skip("apps/swarm-ui/src/api.ts is not present")

    called = set()
    for raw in re.findall(r"['\"`](/v1/accounts[^'\"`]*)['\"`]", api_ts.read_text()):
        # Template holes become the path parameter this router declares, so the
        # comparison is about the SHAPE of the route rather than one instance.
        called.add(re.sub(r"\$\{[^}]*\}", "{account_id}", raw))

    served = {r.path for r in router.routes}
    missing = sorted(called - served)

    assert not missing, (
        "the UI calls paths this API does not serve: "
        f"{missing}. Both ends exist and the seam does not."
    )
