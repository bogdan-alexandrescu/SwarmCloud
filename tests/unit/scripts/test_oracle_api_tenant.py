"""oracle-api submits work as a tenant of its own, u-or-9a0792 (#1568).

OWNER DECISION 2026-10-18. The orchestrating client application runs as the
service account `oracle-api@saga-agents-staging.iam.gserviceaccount.com` and is
admitted as a tenant of its own, the way swarm-verify is (u-sw-c90291), and
NEVER as a member of eng@saga.xyz: eng's tenant decides eng's worker account,
provider keys, GCS prefix and namespace (invariant 9), and a client
application's work has no business holding any of them.

Three places have to agree for that to hold, and each is checked here:

  * the front door: IAP admits the account (`frontend_iap_members` in
    terraform/bootstrap/terraform.tfvars, which the owner applies himself --
    the committed line is the record of that apply);
  * swarm-api: ALLOWED_USERS admits the account past the saga.xyz domain
    check (terraform/infra/locals.tf appends var.allowed_users, which
    dev.tfvars sets to the account since DAYONE-TF-1);
  * the tenant: dev.tfvars declares the id the frozen identity module gives
    the account, so the tenant has a worker account, a namespace and a prefix
    behind it -- and no group tenant lists the account.

The id is COMPUTED, never chosen: swarm_common.identity appends a digest
whenever the slug is lossy or too long, and this address is both. So the key
is recomputed from the frozen module here rather than trusted from the file.
tests/terraform/oracle_api_tenant.tftest.hcl holds the plan to the same id.
"""

from __future__ import annotations

import re

from swarm_common.identity import tenant_id_for_user

from .test_dev_tfvars_capacity import DEV_TFVARS, _block, _entries
from .test_verify_identity_grants import (
    BOOTSTRAP_TFVARS,
    LOCALS_TF,
    _string_list,
    _strip_comments,
    _verify_identity,
)

ORACLE_API = "oracle-api@saga-agents-staging.iam.gserviceaccount.com"
#: What tenant_id_for_user gave the account on main, 2026-10-18. Asserted
#: against the frozen module below, never used in place of it.
ORACLE_TENANT = "u-or-9a0792"


def _tenants() -> dict[str, dict[str, str]]:
    text = _strip_comments(DEV_TFVARS.read_text())
    return {name: _entries(body) for name, body in _entries(_block(text, "tenants")).items()}


def _strings(value: str) -> list[str]:
    return re.findall(r'"([^"]+)"', value)


def _unquote(value: str) -> str:
    return value.strip().strip('"')


def _allowed_users_expression() -> str:
    """The right-hand side of `ALLOWED_USERS = ...` in locals.tf, comments out."""
    text = _strip_comments(LOCALS_TF.read_text())
    m = re.search(r"^\s*ALLOWED_USERS\s*=\s*", text, re.M)
    assert m, "terraform/infra/locals.tf no longer sets ALLOWED_USERS"
    rest = text[m.end() :]
    if not rest.startswith("join("):
        return rest.splitlines()[0]
    depth = 0
    for i, ch in enumerate(rest):
        depth += {"(": 1, ")": -1}.get(ch, 0)
        if depth == 0 and ch == ")":
            return rest[: i + 1]
    raise AssertionError("ALLOWED_USERS' join( is not closed in locals.tf")


def test_dev_declares_the_tenant_the_frozen_identity_gives_the_account() -> None:
    derived = tenant_id_for_user(ORACLE_API)
    assert derived == ORACLE_TENANT, (
        f"swarm_common.identity now gives {ORACLE_API} {derived!r}, not "
        f"{ORACLE_TENANT!r}: dev.tfvars, locals.tf's comment, the docs and the "
        "operator steps all name the old id"
    )
    tenants = _tenants()
    assert derived in tenants, (
        f"dev.tfvars declares no tenant {derived!r}; a POST /v1/workflows as "
        f"{ORACLE_API} resolves there and finds no worker account to run as"
    )
    entry = tenants[derived]
    assert _unquote(entry["kind"]) == "user"
    assert _unquote(entry["principal"]) == ORACLE_API
    # Not a registered group: there is no group to ask Cloud Identity about,
    # and a directory_group lookup for it would 503 every caller below it.
    assert _unquote(entry["directory_group"]) == "false"


def test_the_tenant_follows_swarm_verify_s_providers_and_limits() -> None:
    """No providers and 4/8, as u-sw-c90291: a Claude account is LENT to the
    tenant from the pool, so it holds no key of its own, and declaring
    anthropic would also lift provider:anthropic's floor to 4 x
    provider_tenant, above dev's pool, and fail the plan."""
    tenants = _tenants()
    verify = tenants[tenant_id_for_user(_verify_identity())]
    oracle = tenants[tenant_id_for_user(ORACLE_API)]
    for field in ("providers", "max_active", "capacity_units"):
        assert oracle[field].replace(" ", "") == verify[field].replace(" ", ""), (
            f"u-or-9a0792's {field} is {oracle[field]}, swarm-verify's is {verify[field]}"
        )
    assert _strings(oracle["providers"]) == []
    assert not _strings(oracle.get("service_accounts", "[]"))


def test_the_account_is_in_no_group_tenant_and_holds_no_admin_reach() -> None:
    """Exactly one tenant names the account, and it is its own user tenant.

    A `service_accounts` listing under eng (or smoke) would file its work in
    that group's tenant -- eng's keys, prefix and namespace -- which is the one
    thing the owner ruled out. The admin lists are checked too: any of them
    would give a client application reach over other tenants."""
    naming = {
        name: entry
        for name, entry in _tenants().items()
        if ORACLE_API in {_unquote(entry.get("principal", ""))}
        | {s.lower() for s in _strings(entry.get("service_accounts", "[]"))}
    }
    assert list(naming) == [tenant_id_for_user(ORACLE_API)], (
        f"the tenants naming {ORACLE_API} are {sorted(naming)}; it must be "
        f"exactly its own, {ORACLE_TENANT}"
    )
    eng = _tenants()["eng"]
    assert _unquote(eng["kind"]) == "group" and _unquote(eng["principal"]) == "eng@saga.xyz"
    for listing in ("admin_users", "admin_pool_users", "secret_admin_members"):
        members = {m.lower().split(":", 1)[-1] for m in _string_list(DEV_TFVARS, listing)}
        assert ORACLE_API not in members, f"{listing} names {ORACLE_API}"


def test_the_bootstrap_admits_the_account_at_the_front_door() -> None:
    members = _string_list(BOOTSTRAP_TFVARS, "frontend_iap_members")
    assert f"serviceAccount:{ORACLE_API}" in members, (
        f"frontend_iap_members is {members}; IAP answers 403 'Access denied. "
        f"For user {ORACLE_API}' before swarm-api ever sees the request"
    )
    # Adding the client must not have replaced anybody already admitted.
    assert "domain:saga.xyz" in members
    assert f"serviceAccount:{_verify_identity()}" in members


def test_swarm_api_admits_the_account_beside_swarm_verify() -> None:
    expression = _allowed_users_expression()
    assert "google_service_account.verify.email" in expression, expression
    # Since DAYONE-TF-1 the address is an install input, var.allowed_users,
    # rather than a literal in locals.tf: the expression must append it, and
    # dev.tfvars must name the account there.
    assert re.search(r"\bvar\.allowed_users\b", expression), (
        f"ALLOWED_USERS is `{expression}`; it no longer appends var.allowed_users"
    )
    allowed = _string_list(DEV_TFVARS, "allowed_users")
    assert ORACLE_API in allowed, (
        f"dev.tfvars allowed_users is {allowed}; swarm-api answers {ORACLE_API} "
        "403 on the saga.xyz domain check until it is listed"
    )
    # The account alone: nothing else rides in with it.
    assert allowed == [ORACLE_API], allowed
    # No literal address left in the expression itself.
    assert set(_strings(expression)) <= {","}, expression
