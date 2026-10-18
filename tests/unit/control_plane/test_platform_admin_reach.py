"""A platform admin reaches every registered GROUP workspace, and no one
else's personal workspace (docs/design/workspace-roles.md §4.2, owner answer
Q5, 2026-10-18; lane ROLES-REACH).

Tenant isolation (invariant 9) is the risk, so every case is measured against
the real `CloudIdentityGroups` over a fake HTTP session: the reach must ask
Cloud Identity nothing it did not already ask, a non-admin must still be
checked per registered group with `x-goog-user-project`, and with
`platform_admin_reach` off every case must answer exactly as before roles.

`root` is a platform admin (in `swarm-admins`) who is a member of research
only; `alice` is a non-admin in research only; `carol` is in nothing. The
registered groups are eng, research and `u-carol@saga.xyz` -- a group whose
frozen slug lands in the personal namespace, which no reach may ever offer.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

from swarm_api.admins import ROLES_COLLECTION, AdminRoles
from swarm_api.auth import Authenticator, StaticTokenVerifier, TenantNotMember
from swarm_api.errors import UpstreamUnavailable
from swarm_api.groups import CloudIdentityGroups
from swarm_api.settings import ApiSettings

from .conftest import ADMIN_GROUP, ENG_GROUP, PROJECT, RESEARCH_GROUP, api_settings
from .fakes import FakeFirestore
from .test_group_resolution import FakeResponse, FakeSession

CAROL_GROUP = "u-carol@saga.xyz"
REGISTERED = (ENG_GROUP, RESEARCH_GROUP, CAROL_GROUP)

#: `TenantNotMember`'s message today, which a reach refusal must not change.
NOT_MEMBER_MESSAGE = (
    "X-Swarm-Tenant names a tenant you are not a verified member of. "
    "It can only select one of the tenants GET /v1/tenants/mine lists; "
    "send no X-Swarm-Tenant to use your default tenant."
)

MEMBERSHIPS = {
    ADMIN_GROUP: {"root@saga.xyz"},
    RESEARCH_GROUP: {"root@saga.xyz", "alice@saga.xyz"},
    ENG_GROUP: set(),
    CAROL_GROUP: set(),
}


class FailingAdminGroup(FakeSession):
    """Cloud Identity answers every group but the admin group."""

    def get(self, url, params=None, headers=None, timeout=None):
        if url.endswith("/groups:lookup") and params["groupKey.id"] == ADMIN_GROUP:
            self.calls.append({"url": url, "params": dict(params), "headers": dict(headers or {})})
            return FakeResponse({"error": "unavailable"}, status_code=503)
        return super().get(url, params=params, headers=headers, timeout=timeout)


class Ready:
    """`PersonalTenants`: whose own personal workspace is ready."""

    def __init__(self, *emails: str) -> None:
        self.emails = set(emails)
        self.asked: list[str] = []

    def ready(self, email: str) -> bool:
        self.asked.append(email)
        return email in self.emails


def claims() -> dict[str, dict[str, Any]]:
    return {
        f"t-{user}": {"email": f"{user}@saga.xyz", "email_verified": True, "sub": f"s-{user}"}
        for user in ("root", "alice", "carol")
    }


def authenticator(*, reach: bool = True, session: FakeSession | None = None,
                  personal: Any = None, admin_roles: Any = None,
                  **settings: Any) -> tuple[Authenticator, FakeSession]:
    session = session if session is not None else FakeSession(
        {g: set(m) for g, m in MEMBERSHIPS.items()})
    auth = Authenticator(
        api_settings(tenant_groups=REGISTERED, platform_admin_reach=reach, **settings),
        StaticTokenVerifier(claims()),
        CloudIdentityGroups(PROJECT, session=session),
        personal=personal,
        admin_roles=admin_roles,
    )
    return auth, session


def checks(session: FakeSession) -> list[dict]:
    return [c for c in session.calls if c["url"].endswith(":checkTransitiveMembership")]


def checked_group(call: dict) -> str:
    return call["url"].split("/groups/")[1].split("/")[0].replace("id-of-", "") + "@saga.xyz"


# -- (1) a platform admin selects a group workspace they are not in ----------

def test_a_platform_admin_selects_a_group_workspace_they_are_not_in():
    baseline_auth, baseline = authenticator()
    baseline_auth.authenticate("Bearer t-root")

    auth, session = authenticator()
    ctx = auth.authenticate("Bearer t-root", None, tenant="eng")

    assert ctx.is_admin is True
    assert ctx.tenant_id == "eng"
    assert ctx.tenant_principal == ENG_GROUP
    assert ctx.tenant_via == "platform_admin"
    # Cloud Identity said no for eng@, and the reach asked it nothing more:
    # the calls are exactly the ones a headerless request makes.
    assert session.calls == baseline.calls
    assert ENG_GROUP not in ctx.principal.groups


def test_the_reach_is_a_separate_list_from_the_memberships():
    auth, _ = authenticator()
    ctx = auth.authenticate("Bearer t-root")
    assert ctx.tenant_choices == (("research", RESEARCH_GROUP),)
    assert ctx.platform_reach == (("eng", ENG_GROUP),)
    assert ctx.tenant_via == ""


def test_a_membership_is_selected_as_a_membership_not_through_the_reach():
    auth, _ = authenticator()
    ctx = auth.authenticate("Bearer t-root", None, tenant="research")
    assert ctx.tenant_id == "research"
    assert ctx.tenant_principal == RESEARCH_GROUP
    assert ctx.tenant_via == ""


# -- (2) refused every personal workspace but their own ----------------------

def test_a_platform_admin_is_refused_someone_elses_personal_workspace():
    auth, _ = authenticator(personal=Ready("root@saga.xyz", "carol@saga.xyz"))
    with pytest.raises(TenantNotMember) as refused:
        auth.authenticate("Bearer t-root", None, tenant="u-carol")
    assert refused.value.code == "tenant_not_member"
    assert str(refused.value) == NOT_MEMBER_MESSAGE


def test_a_registered_group_in_the_personal_namespace_is_never_reached():
    auth, _ = authenticator()
    ctx = auth.authenticate("Bearer t-root")
    assert CAROL_GROUP in auth._settings.tenant_groups
    assert "u-carol" not in {t for t, _ in ctx.platform_reach}
    assert CAROL_GROUP not in {g for _, g in ctx.platform_reach}


def test_a_platform_admins_own_personal_workspace_still_selects():
    personal = Ready("root@saga.xyz")
    auth, _ = authenticator(personal=personal)
    ctx = auth.authenticate("Bearer t-root", None, tenant="u-root")
    assert ctx.tenant_id == "u-root"
    assert ctx.tenant_principal == "root@saga.xyz"
    assert ctx.tenant_via == ""


def test_a_reach_request_keeps_the_own_workspace_among_the_choices():
    """§4.2.2 step 2: a reach id is treated like a membership by
    `_with_own_workspace`, so the switcher still offers the own entry."""
    personal = Ready("root@saga.xyz")
    auth, _ = authenticator(personal=personal)
    ctx = auth.authenticate("Bearer t-root", None, tenant="eng")
    assert ctx.tenant_id == "eng"
    assert ctx.tenant_via == "platform_admin"
    assert ctx.tenant_choices[0] == ("u-root", "root@saga.xyz")


# -- (3) a non-admin is still membership-checked -----------------------------

def test_a_non_admin_is_still_checked_per_registered_group():
    auth, session = authenticator()
    with pytest.raises(TenantNotMember) as refused:
        auth.authenticate("Bearer t-alice", None, tenant="eng")
    assert str(refused.value) == NOT_MEMBER_MESSAGE

    ctx = authenticator()[0].authenticate("Bearer t-alice")
    assert ctx.is_admin is False
    assert ctx.platform_reach == ()

    asked = [checked_group(c) for c in checks(session)]
    # Exactly one checkTransitiveMembership per registered group (and the
    # admin group, which decides `is_admin`), never an enumeration.
    assert sorted(asked) == sorted((*REGISTERED, ADMIN_GROUP))
    assert all("searchTransitiveGroups" not in c["url"] for c in session.calls)
    for call in session.calls:
        assert call["headers"]["x-goog-user-project"] == PROJECT


def test_a_non_admin_in_nothing_is_refused_a_group():
    auth, _ = authenticator()
    with pytest.raises(TenantNotMember):
        auth.authenticate("Bearer t-carol", None, tenant="eng")


def test_a_pool_admin_gets_no_reach():
    auth, _ = authenticator(admin_pool_users=("alice@saga.xyz",))
    ctx = auth.authenticate("Bearer t-alice")
    assert ctx.is_pool_admin is True
    assert ctx.platform_reach == ()
    with pytest.raises(TenantNotMember):
        auth.authenticate("Bearer t-alice", None, tenant="eng")


# -- the default and the admin lookup ----------------------------------------

def test_a_headerless_request_lands_in_the_same_default_as_before():
    on, _ = authenticator(reach=True)
    off, _ = authenticator(reach=False)
    with_reach = on.authenticate("Bearer t-root")
    without = off.authenticate("Bearer t-root")
    assert with_reach.tenant_id == without.tenant_id == "research"
    assert with_reach.tenant_principal == without.tenant_principal == RESEARCH_GROUP
    assert with_reach.principal == without.principal
    assert with_reach.tenant_choices == without.tenant_choices
    # The reach is the one field that differs.
    assert replace(with_reach, platform_reach=()) == without


def test_an_unresolved_admin_lookup_with_a_reach_header_is_503_not_403():
    auth, _ = authenticator(session=FailingAdminGroup({g: set(m) for g, m in MEMBERSHIPS.items()}))
    assert auth.authenticate("Bearer t-alice").admin_unresolved is True
    with pytest.raises(UpstreamUnavailable):
        auth.authenticate("Bearer t-alice", None, tenant="eng")
    # An id the reach could never have selected is still today's 403.
    with pytest.raises(TenantNotMember):
        auth.authenticate("Bearer t-alice", None, tenant="u-carol")
    with pytest.raises(TenantNotMember):
        auth.authenticate("Bearer t-alice", None, tenant="no-such-tenant")


# -- with the setting off, every case is today's -----------------------------

def test_with_the_reach_off_a_platform_admin_is_refused_a_group_they_are_not_in():
    auth, _ = authenticator(reach=False)
    ctx = auth.authenticate("Bearer t-root")
    assert ctx.platform_reach == ()
    with pytest.raises(TenantNotMember) as refused:
        auth.authenticate("Bearer t-root", None, tenant="eng")
    assert str(refused.value) == NOT_MEMBER_MESSAGE


def test_with_the_reach_off_the_other_cases_are_todays():
    auth, _ = authenticator(reach=False, personal=Ready("root@saga.xyz"))
    with pytest.raises(TenantNotMember):
        auth.authenticate("Bearer t-root", None, tenant="u-carol")
    assert auth.authenticate("Bearer t-root", None, tenant="u-root").tenant_via == ""
    with pytest.raises(TenantNotMember):
        auth.authenticate("Bearer t-alice", None, tenant="eng")
    unresolved, _ = authenticator(
        reach=False, session=FailingAdminGroup({g: set(m) for g, m in MEMBERSHIPS.items()}))
    with pytest.raises(TenantNotMember):
        unresolved.authenticate("Bearer t-alice", None, tenant="eng")


def test_the_setting_defaults_off():
    assert ApiSettings.__dataclass_fields__["platform_admin_reach"].default is False
    assert ApiSettings.__dataclass_fields__["role_cache_seconds"].default == 15.0


# -- may_act_in: the re-checks with nobody on the call -----------------------

def tenant(tenant_id: str, kind: str, principal: str) -> SimpleNamespace:
    return SimpleNamespace(tenant_id=tenant_id, kind=kind, principal=principal)


def test_may_act_in_answers_platform_admin_for_a_group_tenant():
    auth, _ = authenticator()
    assert auth.may_act_in("root@saga.xyz", tenant("eng", "group", ENG_GROUP)) == "platform_admin"
    assert auth.may_act_in("root@saga.xyz", tenant("research", "group", RESEARCH_GROUP)) == "member"
    assert auth.may_act_in("alice@saga.xyz", tenant("eng", "group", ENG_GROUP)) == ""


def test_may_act_in_answers_nothing_for_a_personal_tenant():
    auth, _ = authenticator()
    assert auth.may_act_in("root@saga.xyz", tenant("u-carol", "user", "carol@saga.xyz")) == ""
    assert auth.may_act_in("root@saga.xyz", tenant("u-carol", "group", CAROL_GROUP)) == ""
    assert auth.may_act_in("root@saga.xyz", tenant("u-root", "user", "root@saga.xyz")) == "member"


def test_may_act_in_never_reaches_an_unregistered_group():
    auth, _ = authenticator()
    assert auth.may_act_in("root@saga.xyz", tenant("ops", "group", "ops@saga.xyz")) == ""
    # Same id, another principal: the collision the principal exists to catch.
    assert auth.may_act_in("root@saga.xyz", tenant("eng", "group", "eng@partner.com")) == ""


def test_may_act_in_with_the_reach_off_is_membership_only():
    auth, _ = authenticator(reach=False)
    assert auth.may_act_in("root@saga.xyz", tenant("eng", "group", ENG_GROUP)) == ""
    assert auth.may_act_in("root@saga.xyz", tenant("research", "group", RESEARCH_GROUP)) == "member"


def test_may_act_in_asks_the_admin_group_past_the_cache():
    auth, session = authenticator()
    auth.authenticate("Bearer t-root")  # primes root's swarm-admins answer
    session.memberships[ADMIN_GROUP] = set()  # removed a moment ago
    assert auth.may_act_in("root@saga.xyz", tenant("eng", "group", ENG_GROUP)) == ""


def test_may_act_in_is_503_when_the_admin_lookup_fails():
    auth, _ = authenticator(session=FailingAdminGroup({g: set(m) for g, m in MEMBERSHIPS.items()}))
    with pytest.raises(UpstreamUnavailable):
        auth.may_act_in("root@saga.xyz", tenant("eng", "group", ENG_GROUP))


def test_may_act_in_reads_the_admin_role_document_fresh():
    db = FakeFirestore()
    roles = AdminRoles(db)
    db.docs[f"{ROLES_COLLECTION}/dana@saga.xyz"] = {"role": "admin"}
    auth, _ = authenticator(admin_roles=roles)
    assert roles.holds_admin("dana@saga.xyz") is True  # cached
    assert auth.may_act_in("dana@saga.xyz", tenant("eng", "group", ENG_GROUP)) == "platform_admin"
    del db.docs[f"{ROLES_COLLECTION}/dana@saga.xyz"]
    assert roles.holds_admin("dana@saga.xyz") is True  # still the cached answer
    assert auth.may_act_in("dana@saga.xyz", tenant("eng", "group", ENG_GROUP)) == ""


def test_may_act_in_gives_a_service_identity_no_reach():
    auth, _ = authenticator(admin_users=("ci@saga-agents.iam.gserviceaccount.com",))
    assert auth.may_act_in(
        "ci@saga-agents.iam.gserviceaccount.com", tenant("eng", "group", ENG_GROUP)) == ""


# -- holds_admin(fresh=True) -------------------------------------------------

def test_holds_admin_fresh_reads_past_the_cache():
    db = FakeFirestore()
    roles = AdminRoles(db)
    db.docs[f"{ROLES_COLLECTION}/dana@saga.xyz"] = {"role": "admin"}
    assert roles.holds_admin("dana@saga.xyz") is True
    del db.docs[f"{ROLES_COLLECTION}/dana@saga.xyz"]
    assert roles.holds_admin("dana@saga.xyz") is True
    assert roles.holds_admin("dana@saga.xyz", fresh=True) is False
    # The fresh read refreshed the cache for everyone after it.
    assert roles.holds_admin("dana@saga.xyz") is False
