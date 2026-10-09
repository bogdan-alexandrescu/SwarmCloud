"""The broker's swarmSecretLister grant waits for terraform/infra (#69).

`terraform/bootstrap/platform_roles.tf` adopts the platform's custom roles and
swarm-quota-broker's swarmSecretLister grant from terraform/infra. Both carry a
lifecycle precondition, `length(local.infra_states_holding_platform_roles) ==
0`, so the owner cannot adopt either while an infra state still manages it:
adopting first leaves the grant managed from two states, and infra's next apply
could remove the binding bootstrap just imported.

`terraform test` cannot reach the grant's precondition. The grant names its
role through `google_project_iam_custom_role.platform["secret_lister"]`, so
while infra still holds the roles the role's precondition fails first and the
grant's is never evaluated; once infra has let go, both pass. The run
`adoption_waits_until_terraform_infra_has_let_go` in
`tests/terraform/platform_roles.tftest.hcl` therefore expects only the role to
fail, and would pass unchanged with the grant's precondition gone. This file
holds the grant's precondition to the role's instead, by reading the source.

MUTATION it catches: replace the grant's condition with `true`, or delete the
grant's precondition block. Either leaves every terraform test green.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
PLATFORM_ROLES_TF = REPO / "terraform" / "bootstrap" / "platform_roles.tf"

GATE_LOCAL = "local.infra_states_holding_platform_roles"


def _block(text: str, header: str) -> str:
    """The body of the block opened by `header`, found by brace matching.

    Braces inside a string (an error_message's `${...}`) balance, so plain
    counting finds the block's own closing brace.
    """
    start = text.find(header)
    assert start != -1, f"platform_roles.tf no longer declares {header}"
    open_at = text.index("{", start + len(header) - 1)
    depth = 0
    for i in range(open_at, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[open_at + 1 : i]
    raise AssertionError(f"{header} is never closed")


def _preconditions(resource_body: str) -> list[str]:
    """Every precondition's condition expression, whitespace-normalised."""
    conditions = []
    for match in re.finditer(r"\bprecondition\s*\{", resource_body):
        body = _block(resource_body[match.start() :], match.group(0))
        cond = re.search(r"^\s*condition\s*=\s*(.+)$", body, re.MULTILINE)
        assert cond, "a precondition without a condition line"
        conditions.append(" ".join(cond.group(1).split()))
    return conditions


def test_the_broker_grant_waits_for_infra_like_the_roles_do() -> None:
    text = PLATFORM_ROLES_TF.read_text()

    role = _block(text, 'resource "google_project_iam_custom_role" "platform" {')
    grant = _block(text, 'resource "google_project_iam_member" "broker_secret_lister" {')

    # Controls: each block is the resource itself, not a neighbour the brace
    # matching ran into.
    assert "permissions = each.value.permissions" in role, "the role block found is not the custom role"
    assert 'secret_lister"].role_id' in grant, "the grant block found does not name the secret_lister role"

    role_conditions = _preconditions(role)
    grant_conditions = _preconditions(grant)
    assert len(role_conditions) == 1, f"the custom role has {len(role_conditions)} preconditions, expected exactly one"
    assert len(grant_conditions) == 1, (
        f"the broker's swarmSecretLister grant has {len(grant_conditions)} preconditions, expected exactly one: "
        "without it the owner can adopt the grant while terraform/infra still manages it"
    )

    assert GATE_LOCAL in role_conditions[0], f"the role's precondition no longer reads {GATE_LOCAL}: {role_conditions[0]}"
    assert grant_conditions[0] == role_conditions[0], (
        "the broker's swarmSecretLister grant does not wait for terraform/infra the way the roles do: "
        f"grant {grant_conditions[0]!r}, role {role_conditions[0]!r}"
    )
