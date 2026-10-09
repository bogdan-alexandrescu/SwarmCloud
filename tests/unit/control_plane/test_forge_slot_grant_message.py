"""A slot swarm-api may not read names the grant that is missing, for the
slot it is (forge.SecretManagerForgeTokens.read_slot).

2026-10-07: a `-git-u-` user slot answered PermissionDenied, and the message
sent the reader to terraform/modules/secret_manager and a per-secret binding.
That module binds only the tenant's `-git` secret. A user slot is read
through a CONDITIONAL PROJECT binding, `google_project_iam_member.
forge_refresh_reader` in terraform/bootstrap/forge_user_slots.tf, titled
"swarm forge user slots api <tenant>" (a personal tenant's, "swarm forge
user slots personal"), matching on resource.name.startsWith.

Each case is paired: the -git secret must keep naming its own module and
must not name the user-slot binding, and the reverse.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from swarm_api import forge

from .test_issue_preview import FakeSecretClient

#: A user slot's provider: "git-u-" and sixteen hex digits, built here.
USER_SLOT = "git-u-" + "0123456789abcdef"
REPO_SLOT = "git-r-" + "fedcba9876543210"


def _tenant(tenant_id: str):
    from swarm_common.models import Tenant

    return Tenant(tenant_id=tenant_id, kind="user" if tenant_id.startswith("u-") else "group",
                  principal=f"{tenant_id}@saga.xyz", created_at=datetime.now(timezone.utc))


def _denied(tenant_id: str, provider: str) -> str:
    from google.api_core import exceptions as gexc

    reader = forge.SecretManagerForgeTokens(
        "p", client=FakeSecretClient(error=gexc.PermissionDenied("denied")))
    with pytest.raises(forge.IssueReadFailed) as raised:
        reader.read_slot(_tenant(tenant_id), provider)
    return str(raised.value)


def test_the_tenants_git_secret_names_its_per_secret_binding() -> None:
    text = _denied("eng", forge.GIT_PROVIDER)
    assert "swarm-tenant-eng-git" in text
    assert "terraform/modules/secret_manager" in text
    assert "forge_user_slots" not in text
    assert "forge_refresh_reader" not in text


def test_a_user_slot_names_the_conditional_project_binding() -> None:
    text = _denied("eng", USER_SLOT)
    assert f"swarm-tenant-eng-{USER_SLOT}" in text
    assert "terraform/bootstrap/forge_user_slots.tf" in text
    assert "google_project_iam_member.forge_refresh_reader" in text
    assert '"swarm forge user slots api eng"' in text
    assert "terraform/modules/secret_manager" not in text


def test_the_user_slot_message_names_the_callers_tenant_not_a_constant() -> None:
    text = _denied("ops", USER_SLOT)
    assert '"swarm forge user slots api ops"' in text
    assert '"swarm forge user slots api eng"' not in text


def test_a_personal_tenants_user_slot_names_the_personal_grant() -> None:
    text = _denied("u-dev-example-com", USER_SLOT)
    assert "terraform/bootstrap/forge_user_slots.tf" in text
    assert '"swarm forge user slots personal"' in text
    assert "swarm forge user slots api" not in text
    assert "terraform/modules/secret_manager" not in text


def test_a_repository_slot_does_not_claim_the_tenant_secrets_module() -> None:
    text = _denied("eng", REPO_SLOT)
    assert f"swarm-tenant-eng-{REPO_SLOT}" in text
    assert "terraform/modules/secret_manager" not in text
    assert "forge_refresh_reader" not in text
