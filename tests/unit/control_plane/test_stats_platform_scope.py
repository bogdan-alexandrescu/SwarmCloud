"""`GET /v1/stats` runs the platform half only when it is asked for (#2149).

The console shell polls `/v1/stats` every 30 s for its panel counts. When the
platform half rode along on every admin read, an open admin tab paid twelve
`count()`s over every tenant per poll, on top of the caller's own twelve --
and Platform counts' decided rule (owner ruling 2026-10-07, #138) is that the
platform count is a priced button and never auto-refreshes. So the platform
half is opt-in: `?scope=platform`, from an admin.

A non-admin who asks keeps today's rule: no error, the tenant half only, and
no `platform_tasks_by_state` key at all -- an absent key is not a platform with
no work, and a 403 would turn a shared client's one request into two.

Each test counts the platform `count_tasks_by_state(None)` calls the store
actually received, not the payload's keys alone: a payload that dropped the key
but still ran the twelve counts would pass a key check and still bill them.
"""

from __future__ import annotations

from typing import Any

import pytest

from .conftest import auth_header


@pytest.fixture
def scopes(api_context, monkeypatch) -> list[Any]:
    """Every tenant argument `count_tasks_by_state` was called with."""
    store = api_context.submissions._store
    real = store.count_tasks_by_state
    seen: list[Any] = []

    def spy(tenant_id=None):
        seen.append(tenant_id)
        return real(tenant_id)

    monkeypatch.setattr(store, "count_tasks_by_state", spy)
    return seen


def test_an_admins_stats_without_the_parameter_runs_no_platform_count(client, scopes):
    response = client.get("/v1/stats", headers=auth_header("root"))
    assert response.status_code == 200, response.text
    # MUTATION: run the platform half for every admin read again (drop the
    # `scope == "platform"` test in service.stats) and this sees a None.
    assert None not in scopes, "an admin's plain /v1/stats ran the platform-wide count()s"
    # MUTATION: serve the platform key without counting and this catches it.
    assert "platform_tasks_by_state" not in response.json()
    assert scopes, "the tenant half was never counted; the check above is vacuous"


def test_an_admins_stats_with_scope_platform_runs_the_platform_count(client, scopes):
    response = client.get("/v1/stats", params={"scope": "platform"}, headers=auth_header("root"))
    assert response.status_code == 200, response.text
    # MUTATION: ignore the parameter (never run the platform half) and this
    # sees no None, and Platform counts' Run button shows no platform card.
    assert scopes.count(None) == 1, f"the platform half ran {scopes.count(None)} times, not once"
    assert "platform_tasks_by_state" in response.json()
    assert "tasks_by_state" in response.json()


def test_a_non_admin_asking_for_the_platform_gets_the_tenant_half_only(client, scopes):
    response = client.get("/v1/stats", params={"scope": "platform"}, headers=auth_header("bob"))
    # Today's rule kept: served, not refused.
    # MUTATION: refuse a non-admin's scope=platform with 403 and this fails.
    assert response.status_code == 200, response.text
    body = response.json()
    # MUTATION: gate the platform half on the parameter alone (drop
    # `ctx.is_admin`) and a non-admin reads every tenant's counts.
    assert None not in scopes, "a non-admin's request ran the platform-wide count()s"
    assert "platform_tasks_by_state" not in body
    assert body["tenant_id"] == "research"


def test_an_unknown_scope_is_refused_rather_than_read_as_tenant(client):
    # A typo (`scope=platfrom`) answering the tenant half with a 200 would show
    # an admin no platform card and no reason; the route names the values.
    response = client.get("/v1/stats", params={"scope": "everyone"}, headers=auth_header("root"))
    # MUTATION: accept any string as `scope` and this sees a 200.
    assert response.status_code == 422, response.text
