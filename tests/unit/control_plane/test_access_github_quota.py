"""GET /v1/access serves `github_quota`: each credential the caller may see, as GitHub last reported it (#1483, QUOTA-THROTTLE).

What these tests hold:

  1. One entry per credential `forge_limits.QUOTAS` has read that the caller
     may see -- their own connection (named by its GitHub login), their
     tenant's `-git` slot, and the installations their tenant enabled -- each
     {kind, name, limit, remaining, reset_at, resource, observed_at, low}.
  2. Another tenant's credentials and another person's connection are never
     served (invariant 9).
  3. `low` is under `forge_quota_defer_percent`, and only until the reset.
  4. Nothing token-shaped is served: counts and times only.

No credentials, no network, no emulator. Fake values are built at runtime.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from swarm_api import access, forgeapp
from swarm_api.forge_limits import (
    QUOTAS, connection_credential, installation_credential, tenant_credential,
)

from .conftest import auth_header
from .test_access_api import (  # noqa: F401  (fixtures)
    LOGIN,
    ORG,
    _connect,
    _enable,
    api,
    clock,
    github,
    issues_transport,
    slots,
    tenant_tokens,
)

FIELDS = {"kind", "name", "limit", "remaining", "reset_at", "resource", "observed_at", "low"}


def _limits(remaining: int, *, hours: float = 1, limit: int = 5000) -> dict[str, str]:
    reset = datetime.now(timezone.utc) + timedelta(hours=hours)
    return {"X-RateLimit-Limit": str(limit), "X-RateLimit-Remaining": str(remaining),
            "X-RateLimit-Reset": str(int(reset.timestamp())), "X-RateLimit-Resource": "core"}


def _quota(api, user: str = "alice") -> list[dict]:
    answer = api.get("/v1/access", headers=auth_header(user))
    assert answer.status_code == 200, answer.text
    return answer.json()["github_quota"]


def test_each_credential_the_caller_may_see_is_served_with_its_quota(api, github, db):
    _connect(api, github)
    _enable(api)
    installation = db.docs[f"forge_orgs/{access.org_id_for('eng', 'alice@example.com', ORG)}"][
        "installation_id"]
    QUOTAS.observe(connection_credential(forgeapp.connection_id_for("eng", "alice@example.com")),
                   _limits(712))
    QUOTAS.observe(tenant_credential("eng"), _limits(4100))
    QUOTAS.observe(installation_credential(installation), _limits(14000, limit=15000))
    # Another tenant's and another person's: never this caller's to see.
    QUOTAS.observe(tenant_credential("research"), _limits(1))
    QUOTAS.observe(connection_credential(forgeapp.connection_id_for("eng", "bob@example.com")),
                   _limits(2))
    QUOTAS.observe(installation_credential(installation + 1), _limits(3))

    served = _quota(api)

    by_kind = {entry["kind"]: entry for entry in served}
    assert len(served) == 3 and set(by_kind) == {"connection", "tenant", "installation"}
    assert all(set(entry) == FIELDS for entry in served)
    you = by_kind["connection"]
    assert (you["name"], you["remaining"], you["limit"], you["low"]) == (LOGIN, 712, 5000, True)
    assert (by_kind["tenant"]["name"], by_kind["tenant"]["low"]) == ("eng", False)
    assert by_kind["installation"]["name"].lower() == ORG.lower()
    assert by_kind["installation"]["remaining"] == 14000
    assert {entry["remaining"] for entry in served}.isdisjoint({1, 2, 3})


def test_nothing_read_yet_is_served_as_nothing_not_as_full(api, github):
    assert _quota(api) == []


def test_a_low_quota_past_its_reset_is_not_low(api, github):
    QUOTAS.observe(tenant_credential("eng"), _limits(5, hours=-1))
    (entry,) = _quota(api)
    assert entry["remaining"] == 5 and entry["low"] is False
