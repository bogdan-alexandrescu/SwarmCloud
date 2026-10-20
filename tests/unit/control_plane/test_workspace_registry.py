"""Job-made group and service workspaces in tenant resolution
(docs/design/workspace-kinds.md §4.4, §6 row WK-RESOLVE; part of #847).

A `ready` record of kind `group` registers its group AFTER every
`TENANT_GROUPS` entry; a `ready` record of kind `service` admits its account
at the front door. Tenant isolation (invariant 9) is the risk, so the group
cases run against the real `CloudIdentityGroups` over a fake HTTP session:
membership is asked one registered group at a time, with
`x-goog-user-project`, and nothing ever lists a caller's groups.

The owner's answers bind it (2026-10-19): a job-made group is a member's
default when no Terraform group is above it (Q4 (a)), and a failed lookup of
one is fatal for a caller it could have changed the answer for (Q5 (a)).

`alice` is in eng (Terraform); `bob` is in research (job-made); `dave` is in
both; `carol` is in neither.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import pytest

from swarm_api.auth import Authenticator, StaticTokenVerifier, TenantNotMember
from swarm_api.errors import Forbidden, UpstreamUnavailable
from swarm_api.groups import CloudIdentityGroups, StaticGroups
from swarm_api.registry import RegistryUnavailable, WorkspaceRegistry
from swarm_common.identity import tenant_id_for_user

from .conftest import ADMIN_GROUP, ENG_GROUP, PROJECT, RESEARCH_GROUP, api_settings
from .fakes import FakeFirestore
from .test_group_resolution import FakeSession

#: research is NOT in TENANT_GROUPS here: only its `ready` record registers it.
TERRAFORM_GROUPS = (ENG_GROUP,)

SERVICE = f"ci-bot@{PROJECT}.iam.gserviceaccount.com"
USERS = ("alice", "bob", "carol", "dave")

MEMBERSHIPS = {
    ENG_GROUP: {"alice@example.com", "dave@example.com"},
    RESEARCH_GROUP: {"bob@example.com", "dave@example.com"},
    ADMIN_GROUP: set(),
}


def at(hour: int) -> datetime:
    return datetime(2026, 10, 19, hour, tzinfo=timezone.utc)


def seed_group(db: FakeFirestore, group: str, *, state: str = "ready",
               tenant_id: str | None = None, ready_at: datetime | None = None) -> None:
    tenant_id = tenant_id or group.split("@", 1)[0]
    db.document(f"workspaces/{tenant_id}").set({
        "kind": "group", "principal": group, "state": state,
        "workspace_id": "w-0000a1", "ready_at": ready_at if state == "ready" else None,
    })


def seed_service(db: FakeFirestore, account: str = SERVICE, *, state: str = "ready",
                 tenant_id: str | None = None, unique_id: str | None = None) -> None:
    tenant_id = tenant_id or tenant_id_for_user(account)
    db.document(f"workspaces/{tenant_id}").set({
        "kind": "service", "principal": account, "state": state,
        "workspace_id": "w-0000b2", "ready_at": at(9) if state == "ready" else None,
        # S1's read of the account (§4.1): what its token's subject must be.
        "service": {"unique_id": unique_id if unique_id is not None else f"s-{account}"},
    })


def claims(*extra: str, verified: Any = True) -> dict[str, dict[str, Any]]:
    out = {
        f"t-{user}": {"email": f"{user}@example.com", "email_verified": True, "sub": f"s-{user}"}
        for user in USERS
    }
    for email in extra:
        out[f"t-{email.split('@')[0]}"] = {"email": email, "email_verified": verified,
                                           "sub": f"s-{email}"}
    return out


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class Flaky:
    """A Firestore that can be taken down, and counts what is asked of it."""

    def __init__(self, db: FakeFirestore) -> None:
        self.db = db
        self.down = False
        self.reads: list[str] = []

    def collection(self, path: str):
        self.reads.append(path)
        if self.down:
            raise RuntimeError("firestore unavailable")
        return self.db.collection(path)


def registry(db: Any, clock: Clock | None = None, **settings: Any) -> WorkspaceRegistry:
    return WorkspaceRegistry.from_settings(
        db, api_settings(tenant_groups=TERRAFORM_GROUPS, **settings),
        clock=clock or Clock())


def authenticator(db: Any, *, groups: Any = None, extra: tuple[str, ...] = (),
                  verified: Any = True, clock: Clock | None = None,
                  reg: Any = "build", **settings: Any) -> Authenticator:
    settings.setdefault("tenant_groups", TERRAFORM_GROUPS)
    built = api_settings(**settings)
    return Authenticator(
        built,
        StaticTokenVerifier(claims(*extra, verified=verified)),
        groups if groups is not None else StaticGroups({
            f"{u}@example.com": tuple(g for g, m in MEMBERSHIPS.items() if f"{u}@example.com" in m)
            for u in USERS
        }),
        registry=WorkspaceRegistry.from_settings(db, built, clock=clock or Clock())
        if reg == "build" else reg,
    )


def ids(ctx: Any) -> list[str]:
    return [tenant_id for tenant_id, _ in ctx.tenant_choices]


# -- a ready group is a registered group ----------------------------------------

def test_a_confirmed_member_of_a_ready_job_made_group_gets_it_in_tenant_choices():
    db = FakeFirestore()
    seed_group(db, RESEARCH_GROUP, ready_at=at(8))
    session = FakeSession({g: set(m) for g, m in MEMBERSHIPS.items()})
    auth = authenticator(db, groups=CloudIdentityGroups(PROJECT, session=session))

    bob = auth.authenticate("Bearer t-bob")
    assert ids(bob) == ["research"]
    assert bob.tenant_id == "research"           # Q4 (a): the default, no Terraform group above
    assert bob.tenant_principal == RESEARCH_GROUP

    # One membership question per registered group, each with the billing
    # header, and no call that lists the caller's groups.
    checks = [c for c in session.calls if c["url"].endswith(":checkTransitiveMembership")]
    asked = sorted(c["url"].split("/groups/")[1].split("/")[0] for c in checks)
    assert asked == ["id-of-eng", "id-of-research", "id-of-swarm-admins"]
    assert all(c["headers"].get("x-goog-user-project") == PROJECT for c in session.calls)
    assert not [c for c in session.calls if "searchTransitive" in c["url"] or ":search" in c["url"]]


def test_an_applying_group_record_is_in_nobodys_choices():
    db = FakeFirestore()
    seed_group(db, RESEARCH_GROUP, state="applying")
    auth = authenticator(db)
    for user in USERS:
        ctx = auth.authenticate(f"Bearer t-{user}")
        assert "research" not in ids(ctx), user
    assert auth.authenticate("Bearer t-bob").tenant_id == "u-bob"
    with pytest.raises(TenantNotMember):
        auth.authenticate("Bearer t-bob", None, tenant="research")


@pytest.mark.parametrize("state", ["requested", "approved", "needs_owner", "failed",
                                   "denied", "deprovisioned"])
def test_no_state_but_ready_registers_a_group(state):
    db = FakeFirestore()
    seed_group(db, RESEARCH_GROUP, state=state)
    assert authenticator(db).authenticate("Bearer t-bob").tenant_id == "u-bob"


def test_a_member_of_eng_keeps_eng_as_default_terraform_groups_first():
    db = FakeFirestore()
    seed_group(db, RESEARCH_GROUP, ready_at=at(8))
    dave = authenticator(db).authenticate("Bearer t-dave")
    assert dave.tenant_id == "eng"
    assert ids(dave) == ["eng", "research"]
    chosen = authenticator(db).authenticate("Bearer t-dave", None, tenant="research")
    assert (chosen.tenant_id, chosen.tenant_principal) == ("research", RESEARCH_GROUP)


def test_registered_groups_follow_ready_at_order():
    db = FakeFirestore()
    seed_group(db, RESEARCH_GROUP, ready_at=at(10))
    seed_group(db, "ops@example.com", ready_at=at(8))
    assert registry(db).registered_groups() == ("ops@example.com", RESEARCH_GROUP)


def test_a_failed_lookup_of_a_job_made_group_is_503_not_a_personal_fallback():
    db = FakeFirestore()
    seed_group(db, RESEARCH_GROUP, ready_at=at(8))
    session = FakeSession({g: set(m) for g, m in MEMBERSHIPS.items()},
                          fail_lookup={RESEARCH_GROUP})
    auth = authenticator(db, groups=CloudIdentityGroups(PROJECT, session=session))
    with pytest.raises(UpstreamUnavailable):
        auth.authenticate("Bearer t-bob")
    # Control: below a confirmed Terraform group the failure cannot change
    # the answer, so alice still resolves, exactly as today's groups do.
    assert auth.authenticate("Bearer t-alice").tenant_id == "eng"


# -- records that must never register a group -------------------------------

@pytest.mark.parametrize("group,tenant_id", [
    ("u-bob@example.com", "u-bob"),           # would act in bob's personal tenant
    (ADMIN_GROUP, "swarm-admins"),         # the admin lookup is kept apart
    ("eng@other.example", "eng"),          # a Terraform group's id
    ("design@other.example", "design"),    # outside the allowed domains
    (f"design-bot@{PROJECT}.iam.gserviceaccount.com", "design-bot"),  # an account, not a group
    (RESEARCH_GROUP, "elsewhere"),         # keyed under an id it does not derive
])
def test_a_ready_group_record_that_breaks_a_request_rule_is_ignored(group, tenant_id):
    db = FakeFirestore()
    seed_group(db, group, tenant_id=tenant_id, ready_at=at(8))
    members = {"bob@example.com": (group,)}
    auth = authenticator(db, groups=StaticGroups(members))
    bob = auth.authenticate("Bearer t-bob")
    assert bob.tenant_id == "u-bob"
    assert bob.tenant_choices == ()
    assert registry(db).registered_groups() == ()


def test_personal_records_register_nothing():
    db = FakeFirestore()
    db.document("workspaces/u-bob").set({"principal": "bob@example.com", "state": "ready"})
    db.document("workspaces/u-carol").set({"kind": "personal", "principal": "carol@example.com",
                                          "state": "ready"})
    reg = registry(db)
    assert reg.registered_groups() == ()
    assert reg.admitted_services() == frozenset()


# -- a ready service record admits its account ---------------------------------

def test_a_ready_service_record_admits_its_account_into_its_own_tenant():
    db = FakeFirestore()
    seed_service(db)
    ctx = authenticator(db, extra=(SERVICE,)).authenticate("Bearer t-ci-bot")
    assert ctx.email == SERVICE
    assert ctx.tenant_id == tenant_id_for_user(SERVICE)
    assert ctx.principal.domain == f"{PROJECT}.iam.gserviceaccount.com"
    assert ctx.is_admin is False


def test_without_a_record_the_account_is_refused_as_today():
    with pytest.raises(Forbidden) as refused:
        authenticator(FakeFirestore(), extra=(SERVICE,)).authenticate("Bearer t-ci-bot")
    assert refused.value.code == "forbidden"


def test_an_applying_service_record_is_403_workspace_not_ready():
    db = FakeFirestore()
    seed_service(db, state="applying")
    with pytest.raises(Forbidden) as refused:
        authenticator(db, extra=(SERVICE,)).authenticate("Bearer t-ci-bot")
    assert refused.value.status_code == 403
    assert refused.value.code == "WORKSPACE_NOT_READY"
    assert refused.value.detail == {"workspace_id": "w-0000b2", "state": "applying"}
    assert SERVICE not in refused.value.message


def test_a_deprovisioned_service_record_drops_its_admission():
    db = FakeFirestore()
    seed_service(db, state="deprovisioned")
    with pytest.raises(Forbidden) as refused:
        authenticator(db, extra=(SERVICE,)).authenticate("Bearer t-ci-bot")
    assert refused.value.code == "forbidden"


def test_the_admitted_account_cannot_select_a_group_tenant():
    db = FakeFirestore()
    seed_service(db)
    with pytest.raises(TenantNotMember) as refused:
        authenticator(db, extra=(SERVICE,)).authenticate("Bearer t-ci-bot", None, tenant="eng")
    assert refused.value.code == "tenant_not_member"


def test_the_admitted_account_needs_an_explicitly_verified_email():
    db = FakeFirestore()
    seed_service(db)
    with pytest.raises(Exception) as refused:
        authenticator(db, extra=(SERVICE,), verified=None).authenticate("Bearer t-ci-bot")
    assert getattr(refused.value, "status_code", None) in (401, 403)


def test_the_admission_is_pinned_to_the_records_unique_id():
    """A deleted account recreated under the same address has a new unique id,
    so its token's subject no longer matches: it is not the account the
    owner approved, and it is refused as if there were no record."""
    db = FakeFirestore()
    seed_service(db, unique_id="1" + "0" * 20)
    with pytest.raises(Forbidden) as refused:
        authenticator(db, extra=(SERVICE,)).authenticate("Bearer t-ci-bot")
    assert refused.value.code == "forbidden"


def test_a_ready_service_record_without_a_unique_id_admits_nobody():
    db = FakeFirestore()
    seed_service(db, unique_id="")
    assert registry(db).admitted_services() == frozenset()
    with pytest.raises(Forbidden):
        authenticator(db, extra=(SERVICE,)).authenticate("Bearer t-ci-bot")


@pytest.mark.parametrize("account", [
    "mallory@gmail.com",                                     # a person, past the domain check
    "ci-bot@other-project.iam.gserviceaccount.com",          # another project's account
    f"secrets-admin@{PROJECT}.iam.gserviceaccount.com",      # SECRET_ADMIN_PRINCIPALS
    f"swarm-api@{PROJECT}.iam.gserviceaccount.com",          # a platform account
    f"swarm-agent-worker-eng@{PROJECT}.iam.gserviceaccount.com",
])
def test_a_ready_service_record_for_a_forbidden_principal_admits_nobody(account):
    db = FakeFirestore()
    seed_service(db, account)
    admins = {"secret_admin_principals": (f"secrets-admin@{PROJECT}.iam.gserviceaccount.com",)}
    assert account not in registry(db, **admins).admitted_services()
    with pytest.raises(Forbidden):
        authenticator(db, extra=(account,), **admins).authenticate(
            f"Bearer t-{account.split('@')[0]}")


def test_a_service_record_under_another_id_admits_nobody():
    db = FakeFirestore()
    seed_service(db, tenant_id="u-someone-else")
    assert registry(db).admitted_services() == frozenset()


# -- a failing registry ------------------------------------------------------------

def test_a_failing_read_after_a_good_one_serves_the_cached_set_and_logs_registry_stale(caplog):
    inner = FakeFirestore()
    seed_group(inner, RESEARCH_GROUP, ready_at=at(8))
    db, clock = Flaky(inner), Clock()
    auth = authenticator(db, clock=clock)
    assert ids(auth.authenticate("Bearer t-bob")) == ["research"]

    db.down = True
    clock.now = 61.0     # past the 60 s cache, so the registry is read again
    with caplog.at_level(logging.WARNING, logger="swarm_api.registry"):
        assert ids(auth.authenticate("Bearer t-bob")) == ["research"]
    assert "registry_stale" in caplog.text

    clock.now = 601.0    # past the 10-minute bound: no longer served
    with pytest.raises(RegistryUnavailable) as refused:
        auth.authenticate("Bearer t-bob")
    assert (refused.value.status_code, refused.value.code) == (503, "registry_unavailable")


def test_with_no_good_read_an_authenticated_request_is_503_registry_unavailable():
    db = Flaky(FakeFirestore())
    db.down = True
    auth = authenticator(db)
    for user in USERS:
        with pytest.raises(RegistryUnavailable) as refused:
            auth.authenticate(f"Bearer t-{user}")
        assert (refused.value.status_code, refused.value.code) == (503, "registry_unavailable")


def test_the_registry_is_read_once_per_cache_window():
    db, clock = Flaky(FakeFirestore()), Clock()
    auth = authenticator(db, clock=clock)
    for user in USERS:
        auth.authenticate(f"Bearer t-{user}")
    first = len(db.reads)
    assert first and set(db.reads) == {"workspaces"}
    clock.now = 59.0
    auth.authenticate("Bearer t-bob")
    assert len(db.reads) == first
    clock.now = 61.0
    auth.authenticate("Bearer t-bob")
    assert len(db.reads) == 2 * first


# -- an empty registry changes nothing ---------------------------------------------

def test_an_empty_registry_leaves_every_context_exactly_as_without_one():
    settings = {"tenant_groups": (ENG_GROUP, RESEARCH_GROUP)}
    with_registry = authenticator(FakeFirestore(), **settings)
    without = authenticator(FakeFirestore(), reg=None, **settings)
    for user in USERS:
        for tenant in (None, "eng", "research"):
            try:
                expected = without.authenticate(f"Bearer t-{user}", None, tenant=tenant)
            except Forbidden as exc:
                with pytest.raises(type(exc)):
                    with_registry.authenticate(f"Bearer t-{user}", None, tenant=tenant)
                continue
            assert with_registry.authenticate(f"Bearer t-{user}", None, tenant=tenant) \
                == expected, (user, tenant)


# -- the deployed app reads the registry --------------------------------------------

def test_the_app_built_by_deps_lists_a_ready_job_made_group_in_tenants_mine():
    from fastapi.testclient import TestClient

    from swarm_api.credentials import InMemoryCredentials
    from swarm_api.deps import build_context
    from swarm_api.main import create_app
    from swarm_api.metrics import ApiMetrics
    from swarm_api.objects import InMemoryObjectReader
    from swarm_api.waker import NullWaker

    from .conftest import NoForgeTokens

    db = FakeFirestore()
    seed_group(db, RESEARCH_GROUP, ready_at=at(9))
    context = build_context(
        settings=api_settings(tenant_groups=TERRAFORM_GROUPS),
        db=db,
        verifier=StaticTokenVerifier(claims()),
        groups=StaticGroups({"bob@example.com": (RESEARCH_GROUP,)}),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=InMemoryObjectReader(bucket=f"swarm-artifacts-{PROJECT}"),
        forge_tokens=NoForgeTokens(),
    )
    client = TestClient(create_app(context), raise_server_exceptions=False)
    response = client.get("/v1/tenants/mine", headers={"Authorization": "Bearer t-bob"})
    assert response.status_code == 200, response.text
    assert "research" in [entry["tenant_id"] for entry in response.json()]
