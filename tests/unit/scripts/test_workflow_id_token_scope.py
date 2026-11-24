"""The deployer's trust pin names no workflow that has an account of its own.

terraform/bootstrap/wif.tf pins the deployer service account to the workflow
files that may act as it (`deployer_workflows`). ci-fix.yml and accept.yml each
federate as their own account (ci_fix.tf, acceptance.tf), so neither may be in
that pin. Read from terraform/bootstrap alone.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
WIF_TF = REPO / "terraform" / "bootstrap" / "wif.tf"


def _deployer_workflows_in_terraform() -> set[str]:
    text = WIF_TF.read_text()
    match = re.search(r"^\s*deployer_workflows\s*=\s*\[(.*?)\]", text, re.M | re.S)
    assert match, f"{WIF_TF} declares no deployer_workflows list; the deployer is not pinned to workflow files"
    body = "\n".join(l.split("#", 1)[0] for l in match.group(1).splitlines())
    return set(re.findall(r'"([^"]+)"', body))


def test_the_ci_fixer_workflow_is_not_trusted_as_the_deployer():
    assert "ci-fix.yml" not in _deployer_workflows_in_terraform(), (
        "ci-fix.yml has its own account (terraform/bootstrap/ci_fix.tf); trusting it as the "
        "deployer hands a job that comments on pull requests projectIamAdmin on a shared project"
    )


def test_acceptance_federates_as_its_own_account_never_the_deployer():
    """accept.yml starts verification jobs and reads release records; it needs
    none of the deployer's run.admin, projectIamAdmin or state bucket. MUTATION:
    add accept.yml to deployer_workflows."""
    assert "accept.yml" not in _deployer_workflows_in_terraform()
    bound = (REPO / "terraform" / "bootstrap" / "acceptance.tf").read_text()
    assert '.github/workflows/accept.yml@refs/heads/main"' in bound
