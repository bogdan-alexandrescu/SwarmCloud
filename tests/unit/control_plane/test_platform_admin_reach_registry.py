"""A platform admin's reach covers the `ready` GROUP workspaces the workspace
job creates, exactly as it covers Terraform groups (docs/design/workspace-
roles.md §4.2.2; owner decision 2026-10-19 on the Q5 follow-up; lane
WK-REACH, part of #847).

Tenant isolation (invariant 9) is the risk, so the cases are the edges: a
non-admin gets no reach, a job-made SERVICE workspace is never reached, and a
group record that is not `ready` is in nobody's reach.

`root` is a platform admin (in `swarm-admins`) and in no tenant group;
`alice` is a non-admin in eng. eng is the only Terraform group; research is
registered only by its job-made record.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from swarm_api.auth import Authenticator, StaticTokenVerifier, TenantNotMember
from swarm_api.groups import StaticGroups
from swarm_api.registry import WorkspaceRegistry
from swarm_common.identity import tenant_id_for_user

from .conftest import ADMIN_GROUP, ENG_GROUP, PROJECT, RESEARCH_GROUP, api_settings
from .fakes import FakeFirestore
from .test_workspace_registry import Clock, at, seed_group, seed_service

TERRAFORM_GROUPS = (ENG_GROUP,)
SERVICE = f"ci-bot@{PROJECT}.iam.gserviceaccount.com"

MEMBERS = {
    "root@saga.xyz": (ADMIN_GROUP,),
    "alice@saga.xyz": (ENG_GROUP,),
}


def authenticator(db: FakeFirestore, *, reach: bool = True) -> Authenticator:
    built = api_settings(tenant_groups=TERRAFORM_GROUPS, platform_admin_reach=reach)
    return Authenticator(
        built,
        StaticTokenVerifier({
            f"t-{email.split('@')[0]}": {"email": email, "email_verified": True,
                                          "sub": f"s-{email}"}
            for email in MEMBERS
        }),
        StaticGroups(MEMBERS),
        registry=WorkspaceRegistry.from_settings(db, built, clock=Clock()),
    )


def tenant(tenant_id: str, kind: str, principal: str) -> SimpleNamespace:
    return SimpleNamespace(tenant_id=tenant_id, kind=kind, principal=principal)


def reach_ids(ctx: Any) -> list[str]:
    return [tenant_id for tenant_id, _ in ctx.platform_reach]


@pytest.fixture
def db() -> FakeFirestore:
    db = FakeFirestore()
    seed_group(db, RESEARCH_GROUP, ready_at=at(9))
    return db


# -- a ready job-made group is reached as a Terraform group is -------------------

def test_a_platform_admin_not_in_a_ready_job_made_group_gets_it_as_reach(db):
    auth = authenticator(db)
    ctx = auth.authenticate("Bearer t-root")

    assert ctx.is_admin is True
    # Terraform groups first, then the registry's, the same walk as the
    # memberships; the admin is in neither, so both are reach.
    assert ctx.platform_reach == (("eng", ENG_GROUP), ("research", RESEARCH_GROUP))
    assert "research" not in [t for t, _ in ctx.tenant_choices]

    selected = auth.authenticate("Bearer t-root", None, tenant="research")
    assert selected.tenant_id == "research"
    assert selected.tenant_principal == RESEARCH_GROUP
    assert selected.tenant_via == "platform_admin"


def test_a_platform_admin_may_act_in_a_ready_job_made_group(db):
    auth = authenticator(db)
    assert auth.may_act_in("root@saga.xyz",
                           tenant("research", "group", RESEARCH_GROUP)) == "platform_admin"
    # The same answer a Terraform group gets.
    assert auth.may_act_in("root@saga.xyz", tenant("eng", "group", ENG_GROUP)) == "platform_admin"


def test_the_job_made_reach_is_off_with_the_reach_switch(db):
    auth = authenticator(db, reach=False)
    assert auth.authenticate("Bearer t-root").platform_reach == ()
    assert auth.may_act_in("root@saga.xyz", tenant("research", "group", RESEARCH_GROUP)) == ""


# -- a non-admin is not reached in ----------------------------------------------

def test_a_non_admin_gets_no_reach_into_a_job_made_group(db):
    auth = authenticator(db)
    ctx = auth.authenticate("Bearer t-alice")

    assert ctx.is_admin is False
    assert ctx.platform_reach == ()
    with pytest.raises(TenantNotMember):
        auth.authenticate("Bearer t-alice", None, tenant="research")
    assert auth.may_act_in("alice@saga.xyz", tenant("research", "group", RESEARCH_GROUP)) == ""


# -- a job-made service workspace is never reached --------------------------------

def test_a_ready_job_made_service_workspace_is_never_reached(db):
    seed_service(db)
    service_id = tenant_id_for_user(SERVICE)
    auth = authenticator(db)

    ctx = auth.authenticate("Bearer t-root")
    assert service_id not in reach_ids(ctx)
    with pytest.raises(TenantNotMember):
        auth.authenticate("Bearer t-root", None, tenant=service_id)
    # Neither as the service tenant it is, nor dressed as a group tenant.
    assert auth.may_act_in("root@saga.xyz", tenant(service_id, "service", SERVICE)) == ""
    assert auth.may_act_in("root@saga.xyz", tenant(service_id, "user", SERVICE)) == ""
    assert auth.may_act_in("root@saga.xyz", tenant(service_id, "group", SERVICE)) == ""


# -- a group record that is not ready is in nobody's reach --------------------------

@pytest.mark.parametrize("state", ["requested", "approved", "applying", "needs_owner", "failed",
                                   "denied", "deprovisioned"])
def test_a_job_made_group_that_is_not_ready_is_not_reached(state):
    db = FakeFirestore()
    seed_group(db, RESEARCH_GROUP, state=state)
    auth = authenticator(db)

    ctx = auth.authenticate("Bearer t-root")
    assert reach_ids(ctx) == ["eng"]
    with pytest.raises(TenantNotMember):
        auth.authenticate("Bearer t-root", None, tenant="research")
    assert auth.may_act_in("root@saga.xyz", tenant("research", "group", RESEARCH_GROUP)) == ""


# -- the registry decides a job-made group's reach, and only that ----------------

class Down:
    """A Firestore that never answers: the registry has never been read."""

    def collection(self, path: str):
        raise RuntimeError("firestore unavailable")


def test_may_act_in_a_job_made_group_is_503_when_the_registry_cannot_be_read():
    from swarm_api.registry import RegistryUnavailable

    auth = authenticator(Down())  # type: ignore[arg-type]
    with pytest.raises(RegistryUnavailable):
        auth.may_act_in("root@saga.xyz", tenant("research", "group", RESEARCH_GROUP))


def test_may_act_in_a_terraform_group_never_waits_on_the_registry():
    auth = authenticator(Down())  # type: ignore[arg-type]
    assert auth.may_act_in("root@saga.xyz", tenant("eng", "group", ENG_GROUP)) == "platform_admin"
    # A job-made principal under a Terraform tenant's id is not that tenant.
    assert auth.may_act_in("root@saga.xyz", tenant("eng", "group", RESEARCH_GROUP)) == ""
