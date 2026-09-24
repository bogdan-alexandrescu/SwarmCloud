"""Superseded secret versions are destroyed, and only ever this platform's own.

WHY THESE TESTS EXIST. `swarm-tenant-u-bogdan-anthropic` held 1,816 versions on
2026-09-22, every one of them ENABLED and none of them destroyed. Only `latest`
is ever read -- `agent_worker.secrets.SecretManagerClient.access` defaults to
it, `SecretManagerStore.access` pins it, and the Cloud Run mount the dispatcher
builds sets it -- so 1,815 past credentials had no consumer and stayed
retrievable by everything holding `secretAccessor`. A credential that rotates
and leaves its predecessor enabled has not rotated.

Two halves, and they fail differently:

  * doing too little is a slow security problem -- old credentials accumulate;
  * doing too much is an immediate one, and not only ours. `saga-agents-staging`
    is SHARED, the broker's `swarmSecretLister` grant is project-WIDE, and the
    one thing standing between a retention bug and another team's credentials
    is the name guard. The guard tests below are the ones in this file that
    matter most.

No cloud, no emulator: the store is driven through the same injected client
seam `tests/unit/control_plane/test_credential_discovery.py` uses.
"""

from __future__ import annotations

import pytest

from quota_broker import secretstore
from quota_broker.secretstore import (
    RETAINED_VERSIONS,
    SecretManagerStore,
    owned_by_this_platform,
)

from .conftest import PROJECT

SECRET = "swarm-tenant-u-bogdan-anthropic"


def _version_name(secret: str, number: int, *, project: str = PROJECT) -> str:
    return f"projects/{project}/secrets/{secret}/versions/{number}"


class _Version:
    def __init__(self, name: str, state: str = "ENABLED") -> None:
        self.name = name
        self.state = state


class _Client:
    """The four Secret Manager calls the store makes, and nothing else.

    Versions are numbered from a counter rather than read back off the names,
    the way Secret Manager numbers them: monotonically, never reused, and
    unaffected by what any one version happens to be called.
    """

    def __init__(
        self,
        secrets: dict[str, list[_Version]] | None = None,
        *,
        list_error: Exception | None = None,
        destroy_error: Exception | None = None,
    ) -> None:
        self.secrets = {k: list(v) for k, v in (secrets or {}).items()}
        self.counters = {k: len(v) for k, v in self.secrets.items()}
        self.list_error = list_error
        self.destroy_error = destroy_error
        self.listed: list[str] = []
        self.destroyed: list[str] = []

    # -- the calls the store makes -----------------------------------------

    def add_secret_version(self, request):
        from google.api_core import exceptions as gexc

        secret = request["parent"].split("/secrets/")[1]
        if secret not in self.secrets:
            raise gexc.NotFound(secret)
        self.counters[secret] += 1
        created = _Version(_version_name(secret, self.counters[secret]))
        self.secrets[secret].append(created)
        return created

    def list_secret_versions(self, request):
        self.listed.append(request["parent"])
        if self.list_error is not None:
            raise self.list_error
        return list(self.secrets.get(request["parent"].split("/secrets/")[1], []))

    def destroy_secret_version(self, request):
        if self.destroy_error is not None:
            raise self.destroy_error
        self.destroyed.append(request["name"])
        for versions in self.secrets.values():
            for version in versions:
                if version.name == request["name"]:
                    # Secret Manager with delayed destruction leaves the version
                    # listed and unreadable rather than removing the row.
                    version.state = "DESTROYED"

    # -- what the assertions read ------------------------------------------

    def state_of(self, name: str) -> str:
        for versions in self.secrets.values():
            for version in versions:
                if version.name == name:
                    return version.state
        raise AssertionError(f"no such version: {name}")

    def enabled(self, secret: str) -> list[str]:
        return [v.name for v in self.secrets[secret] if v.state == "ENABLED"]


def _store(client: _Client) -> SecretManagerStore:
    return SecretManagerStore(PROJECT, client=client)


def _with_versions(count: int, *, secret: str = SECRET) -> _Client:
    return _Client({secret: [_Version(_version_name(secret, n)) for n in range(1, count + 1)]})


# -- the retention rule ----------------------------------------------------


def test_a_publish_destroys_every_version_older_than_the_newest_three():
    """The steady state is three versions, not 1,816.

    Five existed, the publish makes six, and the three oldest go. Three is
    argued beside `RETAINED_VERSIONS`: one because nothing ever reads an older
    version, one so a bad publish can be rolled back, one for the operator
    rotation that lands a second before a refresh.
    """
    client = _with_versions(5)
    _store(client).add_version(SECRET, "token")

    assert client.destroyed == [
        _version_name(SECRET, 3),
        _version_name(SECRET, 2),
        _version_name(SECRET, 1),
    ]
    assert client.enabled(SECRET) == [_version_name(SECRET, n) for n in (4, 5, 6)]
    assert len(client.enabled(SECRET)) == RETAINED_VERSIONS


def test_the_version_just_written_is_never_destroyed():
    """The credential that just landed is the one thing retention may not take.

    Asserted from both sides on purpose. That the new version survives is not
    enough by itself -- a pass that destroyed nothing at all would satisfy it
    too -- so this also pins that the pass RAN, and spared the version the
    publish had just created while taking the three it superseded.
    """
    client = _with_versions(5)
    _store(client).add_version(SECRET, "token")

    newest = _version_name(SECRET, 6)
    assert newest not in client.destroyed
    assert client.state_of(newest) == "ENABLED"
    assert client.destroyed == [
        _version_name(SECRET, 3),
        _version_name(SECRET, 2),
        _version_name(SECRET, 1),
    ]


def test_a_secret_with_three_or_fewer_versions_loses_nothing():
    """Onboarding must not be a destroy. Two versions plus the publish is three."""
    client = _with_versions(2)
    _store(client).add_version(SECRET, "token")

    assert client.destroyed == []
    assert len(client.enabled(SECRET)) == 3


def test_versions_already_destroyed_do_not_shield_live_ones():
    """Retention counts what is READABLE, not what is listed.

    A destroyed version is already unreadable, so it is neither a risk worth
    expiring nor a survivor worth counting. Counting it would let three dead
    rows hold the retained set open and leave live credentials behind -- the
    same leak, with a tidier listing.
    """
    client = _with_versions(3)
    for version in client.secrets[SECRET][1:]:
        version.state = "DESTROYED"

    store = _store(client)
    store.add_version(SECRET, "token")  # v4; enabled {1, 4}
    assert client.destroyed == []

    store.add_version(SECRET, "token")  # v5; enabled {1, 4, 5}
    assert client.destroyed == []

    store.add_version(SECRET, "token")  # v6; enabled {1, 4, 5, 6} -- one too many
    assert client.destroyed == [_version_name(SECRET, 1)]


def test_retention_never_leaves_a_secret_with_no_enabled_version(monkeypatch):
    """The floor is checked in code, not inferred from the constant.

    Driven by lowering `RETAINED_VERSIONS` to zero, which is the only way this
    branch is reachable today -- and exactly the shape a careless future edit
    to that constant would take. Nothing is destroyed, so the worker keeps a
    credential it can still read.
    """
    monkeypatch.setattr(secretstore, "RETAINED_VERSIONS", 0)
    client = _with_versions(4)
    _store(client).add_version(SECRET, "token")

    assert client.destroyed == []
    assert client.enabled(SECRET)


# -- the name guard --------------------------------------------------------


def test_a_secret_outside_the_platform_naming_scheme_is_never_touched():
    """saga-agents-staging is SHARED, and this is what keeps retention out of it.

    Nothing in this platform calls `add_version` with a name like this, and the
    point of the test is that the guard does not depend on that: the broker's
    `swarmSecretLister` grant is project-wide, so Secret Manager would list and
    destroy another team's versions if it were ever asked. It is never asked --
    the refusal comes before the listing call, which is why `listed` is
    asserted empty and not merely `destroyed`.
    """
    other = "agents-staging-postgres"
    client = _Client({other: [_Version(_version_name(other, n)) for n in range(1, 11)]})

    _store(client).add_version(other, "token")

    assert client.destroyed == []
    assert client.listed == []
    assert len(client.enabled(other)) == 11


@pytest.mark.parametrize(
    "name",
    [
        "swarm-tenant-eng-anthropic",
        "swarm-tenant-u-bogdan-anthropic-refresh",
        "swarm-account-eng--team",
        "swarm-account-u-bogdan--devops-main-refresh",
    ],
)
def test_the_guard_admits_the_names_this_platform_creates(name):
    """Both shapes, both halves. A guard that refused these would be the leak."""
    assert owned_by_this_platform(name)


@pytest.mark.parametrize(
    "name",
    [
        "agents-staging-postgres",
        "agents-staging-swarm-tenant-eng-anthropic",  # why the leading anchor
        "swarm-tenant-eng-anthropic\nagents-staging-postgres",  # why \Z and not $
        "swarm-tenant-eng-anthropic/../agents-staging-postgres",
        "swarm-other-eng-anthropic",
        "swarm-tenant-",
        "swarm-tenant",
        "",
        " swarm-tenant-eng-anthropic",
    ],
)
def test_the_guard_refuses_everything_else(name):
    """Each of these is a different way out of the names this platform owns."""
    assert not owned_by_this_platform(name)


def test_a_version_listed_under_another_secret_is_refused():
    """The ownership check is re-derived from the API's own answer.

    A wrong `parent`, a paging bug or a client that returned somebody else's
    rows would otherwise arrive as a list of resource names this code destroys
    without looking at them. It looks.
    """
    client = _with_versions(5)
    client.secrets[SECRET][0] = _Version(_version_name("agents-staging-postgres", 1))

    _store(client).add_version(SECRET, "token")

    assert client.destroyed == []


def test_a_version_naming_another_project_is_refused():
    """The same check, on the project half of the resource name."""
    client = _with_versions(5)
    client.secrets[SECRET][0] = _Version(_version_name(SECRET, 1, project="some-other-project"))

    _store(client).add_version(SECRET, "token")

    assert client.destroyed == []


def test_an_unparseable_version_name_is_refused():
    """Not being able to read a name is not permission to destroy the others."""
    client = _with_versions(5)
    client.secrets[SECRET][0] = _Version("not-a-resource-name")

    _store(client).add_version(SECRET, "token")

    assert client.destroyed == []


# -- failure is housekeeping, never the publish ----------------------------


def test_a_destroy_failure_does_not_fail_the_publish():
    """The credential landing is the point; expiring its predecessors is not.

    This is the broker's situation TODAY: it holds `versions.add` and neither
    `versions.list` nor `versions.destroy`, so every retention pass raises
    PermissionDenied. If that propagated, the refresher would report the tenant
    as unrefreshed and publish the same token again on the next sweep -- the
    version leak, rebuilt one layer up by the code meant to end it.
    """
    from google.api_core import exceptions as gexc

    client = _with_versions(5)
    client.destroy_error = gexc.PermissionDenied("secretmanager.versions.destroy")

    _store(client).add_version(SECRET, "token")  # must not raise

    assert client.destroyed == []
    assert client.state_of(_version_name(SECRET, 6)) == "ENABLED"


def test_a_listing_failure_does_not_fail_the_publish():
    """`versions.list` is the permission the broker is missing first."""
    from google.api_core import exceptions as gexc

    client = _with_versions(5)
    client.list_error = gexc.PermissionDenied("secretmanager.versions.list")

    _store(client).add_version(SECRET, "token")  # must not raise

    assert client.destroyed == []
    assert client.state_of(_version_name(SECRET, 6)) == "ENABLED"


def test_the_next_publish_retries_a_destroy_that_failed():
    """Nothing records that a secret was pruned, so nothing suppresses a retry.

    A transient failure must not leave the secret growing forever, and what
    guarantees that is the retained set being recomputed from a live listing on
    every publish rather than read from a marker.
    """
    from google.api_core import exceptions as gexc

    client = _with_versions(5)
    client.destroy_error = gexc.ServiceUnavailable("try again")
    store = _store(client)

    store.add_version(SECRET, "token")
    assert client.destroyed == []

    client.destroy_error = None
    store.add_version(SECRET, "token")

    # Everything the failed pass should have taken, plus the version the first
    # publish added -- caught up in one pass, by the same store instance.
    assert client.destroyed == [
        _version_name(SECRET, 4),
        _version_name(SECRET, 3),
        _version_name(SECRET, 2),
        _version_name(SECRET, 1),
    ]
    assert client.enabled(SECRET) == [_version_name(SECRET, n) for n in (5, 6, 7)]
