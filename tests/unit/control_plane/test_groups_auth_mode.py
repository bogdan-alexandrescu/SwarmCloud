"""`GROUPS_AUTH`: how swarm-api is authorised to read Cloud Identity groups
(docs/design/day-one-install.md §8, BGD's Q2 answer in §14; DAYONE-IDENTITY-1,
part of #2688).

`admin_role`: the Workspace "Groups Reader" admin role is assigned to
swarm-api's own service account, so it calls the Groups API as itself and
`delegation.delegate()` is never reached. `delegation` (the default, and the
live deployment's mode until it is flipped): the service account acts as
`GROUPS_IMPERSONATE_USER` through domain-wide delegation. Anything else is
refused at start, never discovered as an Error(2028) on the first request.

Every Google client here is a fake: no credentials, no network.
"""

from __future__ import annotations

import pytest

from swarm_api import delegation
from swarm_api import groups as groups_mod
from swarm_api.groups import CLOUD_IDENTITY_ROOT, CloudIdentityGroups
from swarm_api.settings import ApiSettings

from .test_group_resolution import FakeSession

PROJECT = "test-project"
SUBJECT = "groups-reader@example.com"


class _OwnCredentials:
    """swarm-api's own (metadata-server) credentials: no `with_subject`."""


@pytest.fixture
def google_clients(monkeypatch):
    """Fake `google.auth.default`, `AuthorizedSession` and `delegate`, and
    record which credentials the session was built with."""
    import google.auth
    import google.auth.transport.requests as transport

    own = _OwnCredentials()
    seen: dict = {"delegated": [], "session_credentials": None}
    fake = FakeSession({"eng@example.com": {"alice@example.com"}})

    def default(scopes=None):
        seen["scopes"] = scopes
        return own, PROJECT

    def authorized_session(credentials):
        seen["session_credentials"] = credentials
        return fake

    def delegate(credentials, *, subject, scopes):
        seen["delegated"].append(subject)
        return object()

    monkeypatch.setattr(google.auth, "default", default)
    monkeypatch.setattr(transport, "AuthorizedSession", authorized_session)
    monkeypatch.setattr(delegation, "delegate", delegate)
    seen["own"] = own
    seen["session"] = fake
    return seen


def _from_env(monkeypatch, **env: str) -> ApiSettings:
    monkeypatch.setenv("PROJECT_ID", PROJECT)
    monkeypatch.setenv("ENVIRONMENT", "dev")
    monkeypatch.delenv("GROUPS_AUTH", raising=False)
    monkeypatch.delenv("GROUPS_IMPERSONATE_USER", raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return ApiSettings.from_env()


def _as_deps_builds_it(settings: ApiSettings) -> CloudIdentityGroups:
    """The resolver exactly as `swarm_api.deps` builds it (deps.py:214-218)."""
    return CloudIdentityGroups(
        settings.project_id,
        impersonate_user=settings.groups_impersonate_user,
        ttl_seconds=settings.group_cache_ttl_seconds,
    )


def test_admin_role_skips_delegation(monkeypatch, google_clients):
    """With the admin role, `checkTransitiveMembership` goes out on swarm-api's
    own credentials, even when GROUPS_IMPERSONATE_USER is still rendered."""
    settings = _from_env(monkeypatch, GROUPS_AUTH="admin_role",
                         GROUPS_IMPERSONATE_USER=SUBJECT)
    assert settings.groups_auth == "admin_role"

    resolver = _as_deps_builds_it(settings)
    assert resolver.is_member("alice@example.com", "eng@example.com") is True

    assert google_clients["delegated"] == [], "admin_role must never call delegation.delegate()"
    assert google_clients["session_credentials"] is google_clients["own"]
    assert any(
        call["url"].startswith(CLOUD_IDENTITY_ROOT)
        and call["url"].endswith(":checkTransitiveMembership")
        for call in google_clients["session"].calls
    )

    # The resolver's own switch says the same, whoever builds it.
    direct = CloudIdentityGroups(PROJECT, impersonate_user=SUBJECT, auth_mode="admin_role")
    assert direct.is_member("alice@example.com", "eng@example.com") is True
    assert google_clients["delegated"] == []


def test_delegation_is_the_default_and_still_delegates(monkeypatch, google_clients):
    """The fallback, and the live deployment's mode until it is flipped."""
    settings = _from_env(monkeypatch, GROUPS_IMPERSONATE_USER=SUBJECT)
    assert settings.groups_auth == "delegation"
    assert settings.groups_impersonate_user == SUBJECT

    _as_deps_builds_it(settings).is_member("alice@example.com", "eng@example.com")
    assert google_clients["delegated"] == [SUBJECT]

    google_clients["delegated"].clear()
    explicit = _from_env(monkeypatch, GROUPS_AUTH=" Delegation ", GROUPS_IMPERSONATE_USER=SUBJECT)
    assert explicit.groups_auth == "delegation"
    _as_deps_builds_it(explicit).is_member("alice@example.com", "eng@example.com")
    assert google_clients["delegated"] == [SUBJECT]


@pytest.mark.parametrize("value", ["admin", "dwd", "adminrole", "admin_role,delegation"])
def test_unknown_mode_refused_at_start(monkeypatch, value):
    with pytest.raises(ValueError, match="GROUPS_AUTH"):
        _from_env(monkeypatch, GROUPS_AUTH=value)
    with pytest.raises(ValueError, match="GROUPS_AUTH"):
        CloudIdentityGroups(PROJECT, auth_mode=value)


def test_the_modes_are_named_once():
    assert groups_mod.GROUPS_AUTH_MODES == ("admin_role", "delegation")
    assert ApiSettings.__dataclass_fields__["groups_auth"].default == "delegation"
