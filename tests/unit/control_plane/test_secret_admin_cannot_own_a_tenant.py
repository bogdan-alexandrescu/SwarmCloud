"""A principal that administers every tenant's secrets may not own a tenant.

terraform/infra/variables.tf already refuses this shape, and states why at
length: secret_admin_members holds secretVersionAdder on EVERY tenant's
provider-key secrets. That cannot read a key in place, but it can REPLACE one
with a key pointing at attacker-controlled infrastructure, after which the
victim tenant's prompts, source and output all flow through it. The validation
caught exactly this once, when bogdan@saga.xyz was both the secret admin and
the principal of the u-bogdan fallback tenant.

That validation can only see tenants DECLARED in var.tenants.
`Store.ensure_tenant` creates a self-service tenant for any allowed-domain
caller on first sight, and those are invisible to terraform. On 2026-09-21
admin@saga.xyz -- the account the tfvars comment describes as "a platform
account that owns no tenant" -- opened the web UI, and tenant u-admin was
written with the platform's secret admin as its principal.

So the runtime now enforces what terraform enforces, on the path terraform
cannot see. These tests pin that, and pin that it does NOT over-reach: an
ordinary caller must still get a tenant on first sight, because that is how
every human on this platform currently resolves one.
"""

from __future__ import annotations

import pytest

from swarm_api.auth import AuthContext
from swarm_api.errors import Forbidden
from swarm_common.identity import Principal


class _Store:
    def __init__(self):
        self.created: list[str] = []

    def ensure_tenant(self, tenant_id, *, principal, **kw):
        self.created.append(principal)

        class _T:
            enabled = True
        t = _T()
        t.tenant_id = tenant_id
        t.principal = principal
        return t


class _Core:
    default_tenant_max_active = 20
    default_tenant_capacity_units = 40
    artifact_bucket = "b"


class _Settings:
    core = _Core()
    project_id = "p"
    tenant_service_account_prefix = "swarm-agent-worker"
    tenant_namespace_prefix = "swarm-tenant-"
    secret_admin_principals: tuple[str, ...] = ("admin@saga.xyz",)


def _service(store, settings=None):
    from swarm_api.service import SubmissionService

    return SubmissionService(
        settings=settings or _Settings(),
        store=store,
        waker=object(),
        metrics=object(),
    )


def _ctx(email, tenant_id, tenant_principal=""):
    return AuthContext(
        principal=Principal(email=email, subject="s", domain=email.rsplit("@", 1)[-1].lower(), groups=()),
        tenant_id=tenant_id,
        is_admin=False,
        tenant_principal=tenant_principal,
    )


def test_the_secret_admin_is_refused_a_tenant():
    store = _Store()
    with pytest.raises(Forbidden) as exc:
        _service(store).tenant_for(_ctx("admin@saga.xyz", "u-admin"))
    assert store.created == [], "the tenant was created before the check ran"
    assert "provider-key secrets" in str(exc.value)


def test_the_refusal_happens_before_the_tenant_is_created():
    """The whole point. ensure_tenant CREATES on first sight, so a check that
    ran afterwards would refuse the request and leave the document behind --
    which is the state this platform is already in for u-admin."""
    store = _Store()
    with pytest.raises(Forbidden):
        _service(store).tenant_for(_ctx("admin@saga.xyz", "u-admin"))
    assert store.created == []


def test_an_ordinary_caller_still_gets_a_tenant_on_first_sight():
    """The check must not break the path every human currently takes: group
    resolution is off for most callers, so they land on `u-<user>` and that
    document is created here."""
    store = _Store()
    tenant = _service(store).tenant_for(_ctx("bogdan@saga.xyz", "u-bogdan"))
    assert tenant.tenant_id == "u-bogdan"
    assert store.created == ["bogdan@saga.xyz"]


def test_matching_is_case_insensitive():
    """An address differing only in case is the same identity, and IAP has
    been observed returning either."""
    store = _Store()
    with pytest.raises(Forbidden):
        _service(store).tenant_for(_ctx("Admin@Saga.XYZ", "u-admin"))
    assert store.created == []


def test_a_group_tenant_is_matched_on_the_group_not_the_caller():
    """tenant_principal is the GROUP for a group tenant. A group named as a
    secret admin is the more dangerous case of the two, because every member
    of it inherits the reach."""
    class _GroupAdmin(_Settings):
        secret_admin_principals = ("platform-admins@saga.xyz",)

    store = _Store()
    with pytest.raises(Forbidden):
        _service(store, _GroupAdmin()).tenant_for(
            _ctx("someone@saga.xyz", "platform-admins", "platform-admins@saga.xyz")
        )
    assert store.created == []


def test_a_caller_in_a_secret_admin_group_but_resolving_elsewhere_is_allowed():
    """Scoped to the TENANT's principal, not the caller's address. A person who
    happens to be in the admin group but whose work files under their own
    tenant is not the dangerous shape -- the tenant is what owns the secrets."""
    class _GroupAdmin(_Settings):
        secret_admin_principals = ("platform-admins@saga.xyz",)

    store = _Store()
    tenant = _service(store, _GroupAdmin()).tenant_for(_ctx("someone@saga.xyz", "u-someone"))
    assert tenant.tenant_id == "u-someone"


def test_no_secret_admins_configured_changes_nothing():
    """The default deployment has an empty list, and an empty list must not
    accidentally match an empty principal."""
    class _None(_Settings):
        secret_admin_principals = ()

    store = _Store()
    assert _service(store, _None()).tenant_for(_ctx("x@saga.xyz", "u-x")).tenant_id == "u-x"


def test_blank_entries_do_not_match_an_empty_principal():
    class _Blank(_Settings):
        secret_admin_principals = ("", "   ")

    store = _Store()
    assert _service(store, _Blank()).tenant_for(_ctx("x@saga.xyz", "u-x")).tenant_id == "u-x"
