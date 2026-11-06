"""One unreadable group fails only its own members
(docs/design/day-one-install.md §8, "the 503-everyone trap"; DAYONE-IDENTITY-1,
part of #2688).

Before this, a registered group Cloud Identity stopped answering for raised for
every caller it ranked above, so with it first in `TENANT_GROUPS` every request
was a 503 (`docs/multi-tenancy.md:167-171`). Now the resolver keeps each
caller's last answer for each group for a bounded window: a caller it last
saw OUTSIDE the failing group is resolved from the groups that do answer; a
caller it last saw inside it, or has never asked about it, still gets the 503,
because for them the failing group could change the tenant (invariant 9: a
misfiled task runs under another tenant's secret, prefix and namespace).

An unreadable workspace registry is still a 503 for everyone (kept).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from swarm_api.auth import Authenticator, StaticTokenVerifier
from swarm_api.errors import UpstreamUnavailable
from swarm_api.groups import CloudIdentityGroups, GroupLookupError, check_group_readable
from swarm_api.registry import RegistryUnavailable, WorkspaceRegistry

from .conftest import api_settings
from .fakes import FakeFirestore
from .test_group_resolution import FakeSession
from .test_workspace_registry import Clock, Flaky

T0 = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)
ENG = "eng@example.com"
RESEARCH = "research@example.com"
ORDER = (ENG, RESEARCH)
USERS = ("alice", "bob", "carol", "dave")


def _session() -> FakeSession:
    # alice: eng; bob: research; carol: neither; dave: never asked before.
    return FakeSession({ENG: {"alice@example.com"}, RESEARCH: {"bob@example.com"}})


def _resolver(session: FakeSession, clock: dict) -> CloudIdentityGroups:
    return CloudIdentityGroups("test-project", session=session, ttl_seconds=60,
                               now=lambda: clock["now"])


def test_one_unreadable_group_fails_only_its_members():
    session, clock = _session(), {"now": T0}
    resolver = _resolver(session, clock)
    assert resolver.groups_for("alice@example.com", ORDER) == (ENG,)
    assert resolver.groups_for("bob@example.com", ORDER) == (RESEARCH,)
    assert resolver.groups_for("carol@example.com", ORDER) == ()

    session.fail_lookup = {ENG}
    clock["now"] = T0 + timedelta(minutes=5)  # past the 60 s cache: eng is asked again

    # bob and carol were last seen outside eng: eng cannot change their tenant.
    assert resolver.groups_for("bob@example.com", ORDER) == (RESEARCH,)
    assert resolver.groups_for("carol@example.com", ORDER) == ()
    # alice is eng's member: her tenant is unknown, so she gets the 503.
    with pytest.raises(GroupLookupError):
        resolver.groups_for("alice@example.com", ORDER)
    # dave was never asked about eng: he could be its member.
    with pytest.raises(GroupLookupError):
        resolver.groups_for("dave@example.com", ORDER)
    # A fresh question (an await-input answer, a directory check) never leans
    # on an old answer.
    with pytest.raises(GroupLookupError):
        resolver.groups_for("bob@example.com", ORDER, fresh=True)
    with pytest.raises(GroupLookupError):
        check_group_readable(resolver, "carol@example.com", ENG)

    # Through the front door: bob is served, alice is a 503.
    authenticator = Authenticator(
        api_settings(tenant_groups=ORDER, admin_groups=()),
        StaticTokenVerifier({
            f"t-{u}": {"email": f"{u}@example.com", "email_verified": True, "sub": f"s-{u}"}
            for u in USERS
        }),
        resolver,
        registry=WorkspaceRegistry.from_settings(
            FakeFirestore(), api_settings(tenant_groups=ORDER), clock=Clock()),
    )
    assert authenticator.authenticate("Bearer t-bob").tenant_id == "research"
    with pytest.raises(UpstreamUnavailable):
        authenticator.authenticate("Bearer t-alice")


def test_an_old_answer_is_not_trusted_forever():
    """Past the window, the failing group could matter again: the 503 returns."""
    session, clock = _session(), {"now": T0}
    resolver = _resolver(session, clock)
    assert resolver.groups_for("bob@example.com", ORDER) == (RESEARCH,)

    session.fail_lookup = {ENG}
    clock["now"] = T0 + timedelta(hours=23)
    assert resolver.groups_for("bob@example.com", ORDER) == (RESEARCH,)
    clock["now"] = T0 + timedelta(hours=25)
    with pytest.raises(GroupLookupError):
        resolver.groups_for("bob@example.com", ORDER)


def test_unreadable_registry_still_503s():
    """Kept: no good read of the workspace registry is a 503 for every caller,
    however readable the groups are."""
    db = Flaky(FakeFirestore())
    db.down = True
    settings = api_settings(tenant_groups=ORDER, admin_groups=())
    authenticator = Authenticator(
        settings,
        StaticTokenVerifier({
            f"t-{u}": {"email": f"{u}@example.com", "email_verified": True, "sub": f"s-{u}"}
            for u in USERS
        }),
        _resolver(_session(), {"now": T0}),
        registry=WorkspaceRegistry.from_settings(db, settings, clock=Clock()),
    )
    for user in USERS:
        with pytest.raises(RegistryUnavailable) as refused:
            authenticator.authenticate(f"Bearer t-{user}")
        assert (refused.value.status_code, refused.value.code) == (503, "registry_unavailable")
