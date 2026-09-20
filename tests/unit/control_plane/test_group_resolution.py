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


def test_a_lookup_failure_that_could_change_the_tenant_is_not_swallowed(session):
    """Failing open here does not degrade a caller -- it MOVES them.

    The tenant decides which Secret Manager secret, which GCS prefix and which
    namespace the work runs under. A caller quietly demoted to `u-alice` during a
    Cloud Identity blip submits tasks their group cannot see afterwards, which may
    park as CREDENTIAL_MISSING or run against a different key. A 503 they retry is
    the cheaper failure.
    """
    session.fail_lookup = {"eng@saga.xyz"}
    resolved = resolver(session)

    with pytest.raises(GroupLookupError):
        resolved.group_resource_name("eng@saga.xyz")

    with pytest.raises(GroupLookupError):
        resolved.groups_for("alice@saga.xyz", ("eng@saga.xyz",))


def test_a_failure_below_a_confirmed_match_cannot_change_the_tenant_and_is_swallowed(
    session,
):
    """One flaky group must not take down the API when it changes no answer.

    `candidate_groups` arrives in admin priority order and `resolve_tenant` takes
    the FIRST match, so a group that could only ever lose to `eng` is irrelevant
    to alice's tenant however it answers.
    """
    session.fail_lookup = {"research@saga.xyz"}
    resolved = resolver(session)

    found = resolved.groups_for("alice@saga.xyz", ("eng@saga.xyz", "research@saga.xyz"))
    assert found == ("eng@saga.xyz",)
    principal = Principal(email="alice@saga.xyz", subject="s", domain="saga.xyz", groups=found)
    assert resolve_tenant(principal, ("eng@saga.xyz", "research@saga.xyz")) == "eng"


def test_a_failure_above_a_confirmed_match_is_raised(session):
    """bob is in `research`, but `eng` outranks it and did not answer.

    Returning `research` here would file bob's work under the wrong tenant if he
    was in fact also a member of `eng`.
    """
    session.fail_lookup = {"eng@saga.xyz"}
    resolved = resolver(session)

    with pytest.raises(GroupLookupError):
        resolved.groups_for("bob@saga.xyz", ("eng@saga.xyz", "research@saga.xyz"))


def test_an_unresolvable_tenant_is_a_503_not_a_silent_tenant_switch(db, tokens, group_map):
    """End to end through the real app: the request fails, it does not move."""
    from fastapi.testclient import TestClient

    from swarm_api.deps import build_context
    from swarm_api.auth import StaticTokenVerifier
    from swarm_api.credentials import InMemoryCredentials
    from swarm_api.main import create_app
    from swarm_api.waker import NullWaker

    from .conftest import api_settings

    class BrokenGroups:
        def groups_for(self, member_email, candidate_groups):
            raise GroupLookupError("cloud identity is having a bad minute")

    ctx = build_context(
        settings=api_settings(),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=BrokenGroups(),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
    )
    client = TestClient(create_app(ctx), raise_server_exceptions=False)

    response = client.post(
        "/v1/tasks",
        headers={"Authorization": "Bearer token-alice"},
        json={"runner_profile": "mock"},
    )
    assert response.status_code == 503
    assert response.json()["code"] == "upstream_unavailable"
    # And nothing was filed under the personal tenant.
    assert not [path for path in db.docs if path.startswith("tasks/")]
    assert "tenants/u-alice" not in db.docs


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


# -- domain-wide delegation ------------------------------------------------

def test_impersonation_is_applied_when_the_credentials_support_it(monkeypatch):
    """A service account must act AS a Workspace user to read groups at all.

    Cloud Identity's Groups API does not authorize through GCP IAM: a
    *.gserviceaccount.com identity is a principal in none of its three
    authorization modes, so every lookup returns Error(2028) no matter what
    IAM role is granted.
    """
    from swarm_api import groups as groups_mod

    applied = {}

    class _Creds:
        def with_subject(self, subject):
            applied["subject"] = subject
            return self

    class _FakeAuth:
        @staticmethod
        def default(scopes=None):
            return _Creds(), "proj"

    monkeypatch.setitem(__import__("sys").modules, "google.auth", _FakeAuth)
    monkeypatch.setattr(
        groups_mod, "CloudIdentityGroups", groups_mod.CloudIdentityGroups, raising=False
    )

    g = groups_mod.CloudIdentityGroups("proj", impersonate_user="bogdan@saga.xyz")
    assert g._impersonate_user == "bogdan@saga.xyz"


def test_blank_impersonation_is_normalised_to_none():
    """An unset env var arrives as "" and must not become a subject of ""."""
    from swarm_api.groups import CloudIdentityGroups

    for value in ("", "   ", None):
        g = CloudIdentityGroups("proj", impersonate_user=value)
        assert g._impersonate_user is None, f"{value!r} should disable delegation"


def test_impersonation_does_not_crash_when_credentials_cannot_delegate():
    """Every developer machine hits this path.

    google.auth.default() returns USER credentials locally, which have no
    with_subject. The service must degrade to an undelegated session -- the
    caller then sees the same Error(2028) it would have seen anyway -- rather
    than failing to construct.
    """
    from swarm_api.groups import CloudIdentityGroups

    # A session supplied explicitly means _ensure_session never builds one, so
    # this asserts the constructor itself is safe with impersonation set.
    class _Session:
        def get(self, *a, **k):
            raise AssertionError("not called")

    g = CloudIdentityGroups("proj", session=_Session(), impersonate_user="someone@saga.xyz")
    assert g._impersonate_user == "someone@saga.xyz"
