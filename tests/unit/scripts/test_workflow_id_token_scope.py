"""The deployer's trust pin names no workflow that has an account of its own.

terraform/bootstrap/wif.tf pins the deployer service account to the workflow
files that may act as it (`deployer_workflows`). ci-fix.yml and accept.yml each
federate as their own account (ci_fix.tf, acceptance.tf), so neither may be in
that pin. Read from terraform/bootstrap alone.
"""

from __future__ import annotations

import re
from pathlib import Path

from .test_release_reuses_ci_images import _code

REPO = Path(__file__).resolve().parents[3]
WIF_TF = REPO / "terraform" / "bootstrap" / "wif.tf"

# For test_release_id_token_scope.py, a CI security guard the owner kept on
# 2026-11-25 (scripts/check_test_reads.py, SECURITY_GUARDS): what an "auth
# step" is. Nothing here reads a workflow; the guard hands in the job.

# The runner variables an OIDC token is requested with. A step that reads
# either can mint the job's identity token without any action.
ID_TOKEN_REQUEST_VARS = ("ACTIONS_ID_TOKEN_REQUEST_URL", "ACTIONS_ID_TOKEN_REQUEST_TOKEN")


def _reads_id_token_request(step: dict) -> bool:
    """A step whose script, env or inputs name the OIDC request variables.

    Comment lines in `run:` are dropped: a comment explaining the variable does
    not read it.
    """
    text = "\n".join([
        _code(str(step.get("run") or "")),
        *(str(v) for v in (step.get("env") or {}).values()),
        *(str(v) for v in (step.get("with") or {}).values()),
    ])
    return any(var in text for var in ID_TOKEN_REQUEST_VARS)


def _auth_steps(job: dict):
    for step in job.get("steps") or []:
        if str(step.get("uses", "")).startswith("google-github-actions/auth@") or _reads_id_token_request(step):
            yield step


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
