"""`git-merge` and `git-review` are never registrable through the API (#364, contract request 33's amendment).

Contract requests 33 and 35 put the two providers in the catalogue. Both
credential routes -- `POST /v1/tenants/me/credentials` and the admin routes
that check a provider name -- accept exactly `known_providers()`, and a
credential registered through them is granted to the tenant's ORDINARY worker
account (`credentials._grant_accessor`). Any agent of the tenant can mint that
account's token, so a merge or review App key registered that way is an App
key every agent can read (merge-step.md §7, T3). The amendment accepted on
2026-10-01 is that the two are left out of `known_providers()`, so neither
route can bind them, and neither route file is edited to get there.

FAILS WITHOUT THE CHANGE: with the catalogue entries in and `known_providers()`
unchanged, `git-merge` and `git-review` are in the set and the route accepts
them with 201.
"""

from __future__ import annotations

import pytest

from swarm_common.profiles import RUNNER_PROFILES

from swarm_api.errors import ValidationFailed
from swarm_api.routes import admin as admin_routes
from swarm_api.validation import known_providers

from .conftest import auth_header

APP_PROVIDERS = ("git-merge", "git-review")


def test_the_catalogue_names_git_review_and_git_merge_is_retired_but_still_excluded():
    # The premise: without this the exclusion below would pass vacuously.
    # Contract request 47 (2026-10-04) moved the merge onto the tenant's `-git`
    # token, so the catalogue names `git-review` and no longer `git-merge`;
    # a `-git-merge` secret that exists is still an App key, so it stays out.
    from swarm_api.validation import APP_CREDENTIAL_PROVIDERS, RETIRED_APP_CREDENTIAL_PROVIDERS

    named = {p.provider for p in RUNNER_PROFILES.values()}
    assert "git-review" in named and "git-merge" not in named, named
    assert RETIRED_APP_CREDENTIAL_PROVIDERS == {"git-merge"}
    assert set(APP_PROVIDERS) <= APP_CREDENTIAL_PROVIDERS


def test_the_forge_token_the_merge_reads_is_not_registrable_through_a_route():
    """Contract request 47: `merge` names `git`, the tenant's forge token. It
    is registered by register-tenant.sh and stored by create-secrets.sh
    --stdin only (owner rule, 2026-09-25), and is not an App key either."""
    from swarm_api.validation import APP_CREDENTIAL_PROVIDERS

    assert RUNNER_PROFILES["merge"].provider == "git"
    assert "git" not in known_providers()
    assert "git" not in APP_CREDENTIAL_PROVIDERS, "register-tenant.sh would refuse the worker -git"


@pytest.mark.parametrize("provider", APP_PROVIDERS)
def test_known_providers_leaves_out_the_app_providers(provider):
    assert provider not in known_providers()


def test_known_providers_still_offers_every_other_catalogue_provider():
    expected = sorted(
        {p.provider for p in RUNNER_PROFILES.values() if p.provider} - set(APP_PROVIDERS) - {"git"}
    )
    assert list(known_providers()) == expected
    assert "anthropic" in known_providers() and "openai" in known_providers()


@pytest.mark.parametrize("provider", APP_PROVIDERS)
def test_the_tenant_route_refuses_to_register_an_app_provider(client, provider):
    response = client.post(
        "/v1/tenants/me/credentials",
        headers=auth_header("alice"),
        json={"provider": provider, "api_key": "x" * 20},
    )
    assert response.status_code == 422, response.text
    assert provider not in response.json()["detail"]["known_providers"]
    me = client.get("/v1/tenants/me", headers=auth_header("alice"))
    assert provider not in me.json()["tenant"]["credentials"]


@pytest.mark.parametrize("provider", APP_PROVIDERS)
def test_the_admin_routes_provider_check_refuses_an_app_provider(provider):
    with pytest.raises(ValidationFailed):
        admin_routes._check_provider(provider)
