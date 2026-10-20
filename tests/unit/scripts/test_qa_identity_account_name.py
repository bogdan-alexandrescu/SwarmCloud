"""The QA account is not one of the platform's own accounts (#1649).

The QA identity needs a service workspace of its own (docs/design/qa-identity.md
§2.2, invariant 9: it sees only what a member of the QA workspace sees). The
workspace flow refuses every account whose address starts with the platform's
prefix (`swarm_api.workspaces.PLATFORM_SA_PREFIX`, WORKSPACE_SERVICE_PLATFORM).
On 2026-10-20 the account bootstrap first made, `swarm-qa`, was refused there,
so the account is named without the prefix. This reads the name Terraform
creates and holds it to the rule swarm-api applies, so the two cannot drift.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]


def _qa_account_id() -> str:
    tf = (REPO / "terraform" / "bootstrap" / "qa_identity.tf").read_text()
    found = re.search(r'^\s*qa_account_id\s*=\s*"([^"]+)"', tf, flags=re.M)
    assert found, "terraform/bootstrap/qa_identity.tf no longer sets qa_account_id"
    return found.group(1)


def test_the_qa_account_can_own_a_service_workspace() -> None:
    # Imported here, not at module top: swarm_api pulls in google, grpc and
    # pydantic, and test_collection_stays_cheap holds tests/unit/scripts to a
    # collection that does not pay for them.
    from swarm_api.workspaces import PLATFORM_SA_PREFIX

    address = f"{_qa_account_id()}@saga-agents-staging.iam.gserviceaccount.com"
    assert not address.startswith(PLATFORM_SA_PREFIX), (
        f"{address} would be refused by the workspace flow (WORKSPACE_SERVICE_PLATFORM), "
        "and the QA identity could never have the workspace its isolation rests on")


def test_bootstrap_admits_the_same_account_at_iap() -> None:
    tfvars = (REPO / "terraform" / "bootstrap" / "terraform.tfvars").read_text()
    member = f'"serviceAccount:{_qa_account_id()}@saga-agents-staging.iam.gserviceaccount.com"'
    assert member in tfvars, "frontend_iap_members does not admit the account qa_identity.tf creates"
