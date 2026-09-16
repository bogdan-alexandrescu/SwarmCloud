"""Cloud Identity group resolution, pinned to what the project actually allows.

CONTRACT.md records a live finding: `groups/-/memberships:searchTransitiveGroups`
403s in saga-agents-staging, while `groups:lookup` and
`memberships:checkTransitiveMembership` work -- and every call needs
`x-goog-user-project`, or it fails with SERVICE_DISABLED naming gcloud's shared
client project.

Those are exactly the kind of facts that get re-broken by a well-meaning
refactor six months later, so they are asserted here: the header, the query
shape, the absence of any enumeration call, and the caching.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from swarm_common.identity import Principal, resolve_tenant, tenant_id_for_group

from swarm_api.groups import CLOUD_IDENTITY_ROOT, CloudIdentityGroups, GroupLookupError

PROJECT = "saga-agents-staging"
T0 = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


class FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self) -> dict:
        return self._payload


class FakeSession:
    """Records every Cloud Identity call the resolver makes."""

    def __init__(self, memberships: dict[str, set[str]], fail_lookup: set[str] = frozenset()):
        self.memberships = memberships
        self.fail_lookup = set(fail_lookup)
        self.calls: list[dict] = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": dict(params or {}),
                           "headers": dict(headers or {}), "timeout": timeout})
        if url.endswith("/groups:lookup"):
            group = params["groupKey.id"]
            if group in self.fail_lookup:
                return FakeResponse({"error": "not found"}, status_code=404)
            return FakeResponse({"name": f"groups/id-of-{group.split('@')[0]}"})
        if url.endswith(":checkTransitiveMembership"):
            group_id = url.split("/groups/")[1].split("/")[0]
            group = group_id.replace("id-of-", "") + "@saga.xyz"
            query = params["query"]
            member = query.split("'")[1]
            return FakeResponse({"hasMembership": member in self.memberships.get(group, set())})
        raise AssertionError(f"unexpected Cloud Identity call: {url}")


@pytest.fixture
def session() -> FakeSession:
    return FakeSession(
        {
            "eng@saga.xyz": {"alice@saga.xyz", "root@saga.xyz"},
            "research@saga.xyz": {"bob@saga.xyz"},
        }
    )


def resolver(session: FakeSession, **kwargs) -> CloudIdentityGroups:
    return CloudIdentityGroups(PROJECT, session=session, **kwargs)


# -- the verified operational constraint ----------------------------------

def test_every_call_carries_the_billing_project_header(session):
    resolver(session).is_member("alice@saga.xyz", "eng@saga.xyz")
    assert session.calls, "no call was made"
    for call in session.calls:
        assert call["headers"]["x-goog-user-project"] == PROJECT, (
            "without x-goog-user-project the call 403s with SERVICE_DISABLED"
        )


def test_membership_is_checked_per_group_never_enumerated(session):
    resolver(session).groups_for("alice@saga.xyz", ("eng@saga.xyz", "research@saga.xyz"))
    urls = [call["url"] for call in session.calls]
    assert all("searchTransitiveGroups" not in url for url in urls), (
        "searchTransitiveGroups 403s in this project and is more privilege than needed"
    )
    assert any(url.endswith("/groups:lookup") for url in urls)
    assert any(url.endswith(":checkTransitiveMembership") for url in urls)


def test_the_query_is_an_expression_not_a_bare_value(session):
    resolver(session).is_member("alice@saga.xyz", "eng@saga.xyz")
    check = next(c for c in session.calls if c["url"].endswith(":checkTransitiveMembership"))
    assert check["params"]["query"] == "member_key_id == 'alice@saga.xyz'"


def test_lookup_uses_the_group_email_as_group_key(session):
    name = resolver(session).group_resource_name("eng@saga.xyz")
    lookup = next(c for c in session.calls if c["url"].endswith("/groups:lookup"))
    assert lookup["params"] == {"groupKey.id": "eng@saga.xyz"}
    assert lookup["url"] == f"{CLOUD_IDENTITY_ROOT}/groups:lookup"
    assert name == "groups/id-of-eng"


# -- behaviour -------------------------------------------------------------

def test_only_groups_the_caller_belongs_to_come_back(session):
    found = resolver(session).groups_for(
        "bob@saga.xyz", ("eng@saga.xyz", "research@saga.xyz")
    )
    assert found == ("research@saga.xyz",)


def test_results_are_cached_per_caller_and_group(session):
    clock = {"now": T0}
    resolved = resolver(session, ttl_seconds=60, now=lambda: clock["now"])

    resolved.is_member("alice@saga.xyz", "eng@saga.xyz")
    first = len(session.calls)
    resolved.is_member("alice@saga.xyz", "eng@saga.xyz")
    assert len(session.calls) == first, "a cached answer must not hit the network"

    clock["now"] = T0 + timedelta(seconds=61)
    resolved.is_member("alice@saga.xyz", "eng@saga.xyz")
    assert len(session.calls) > first, "the cache must expire"


def test_a_lookup_failure_degrades_to_the_personal_tenant(session):
    session.fail_lookup = {"eng@saga.xyz"}
    resolved = resolver(session)

    with pytest.raises(GroupLookupError):
        resolved.group_resource_name("eng@saga.xyz")

    # groups_for swallows it: one flaky group must not take down the API.
    assert resolved.groups_for("alice@saga.xyz", ("eng@saga.xyz",)) == ()
    principal = Principal(email="alice@saga.xyz", subject="s", domain="saga.xyz", groups=())
    assert resolve_tenant(principal, ("eng@saga.xyz",)) == "u-alice"


def test_group_priority_is_deterministic_for_multi_group_members(session):
    session.memberships["research@saga.xyz"].add("root@saga.xyz")
    resolved = resolver(session)
    groups = resolved.groups_for("root@saga.xyz", ("research@saga.xyz", "eng@saga.xyz"))
    principal = Principal(email="root@saga.xyz", subject="s", domain="saga.xyz", groups=groups)

    # First match in the ADMIN-ORDERED list wins, not first match in `groups`.
    assert resolve_tenant(principal, ("eng@saga.xyz", "research@saga.xyz")) == "eng"
    assert resolve_tenant(principal, ("research@saga.xyz", "eng@saga.xyz")) == "research"
    assert tenant_id_for_group("eng@saga.xyz") == "eng"


def test_a_project_id_is_required():
    with pytest.raises(ValueError):
        CloudIdentityGroups("", session=FakeSession({}))
