"""Only the jobs that authenticate to Google can mint a Google identity token,
and only the workflow files the deployer trusts can become the deployer.

#457 (docs/merge-step.md M4, R4). #455 moved `id-token: write` to job level in
release.yml; application.yml, terraform.yml and security.yml still granted it
at workflow level and run on every push to main. Their test jobs run the
merged repository's own code -- `pytest tests/unit`, `pytest
tests/integration`, `terraform test` -- so a merged conftest could mint an
OIDC token and exchange it for the deployer, because the deployer's binding
accepted ANY workflow on main. Merging was code execution as the deployer.

Two controls close it, and each is held here:

* in every workflow below, the workflow-level `permissions:` carries no
  `id-token`, `id-token: write` sits on exactly the jobs that run
  `google-github-actions/auth`, and every such job declares it (a missing
  grant fails only on main, which no pull request exercises);
* `permissions:` is never a string. `write-all` grants id-token without the
  word appearing, so a check that reads the mapping would pass it;
* the deployer's workload identity binding (terraform/bootstrap/wif.tf,
  `deployer_workflows`) names exactly the workflow files that authenticate as
  GCP_DEPLOY_SA, by `attribute.job_workflow_ref`. A workflow added later, or a
  job in one of these files that is handed id-token, is the only way past it,
  and the first is refused by the binding while the second is refused here.
"""

from __future__ import annotations

import re

import pytest

from .test_release_reuses_ci_images import REPO, WORKFLOWS, _workflow

# Every workflow file whose jobs may hold id-token, with the jobs that
# authenticate to Google. release.yml has its own test
# (test_release_id_token_scope.py) and is included here for the string and
# trust-pin properties.
NEEDS_ID_TOKEN = {
    "application.yml": {"build"},
    "terraform.yml": {"plan"},
    "security.yml": {"images"},
    "iam-refusal-probe.yml": {"probe"},
    "release.yml": {"build", "promote", "infrastructure", "infrastructure-iam", "deploy", "acceptance"},
}

WIF_TF = REPO / "terraform" / "bootstrap" / "wif.tf"


def _auth_steps(job: dict):
    for step in job.get("steps") or []:
        if str(step.get("uses", "")).startswith("google-github-actions/auth@"):
            yield step


def _permissions(owner: str, block) -> dict:
    assert block is None or isinstance(block, dict), (
        f"{owner} sets permissions to {block!r}; a string such as write-all grants id-token "
        "to every job it covers without naming it. Spell the scopes out as a mapping."
    )
    return block or {}


@pytest.mark.parametrize("name", sorted(NEEDS_ID_TOKEN))
def test_permissions_are_never_a_string(name):
    workflow = _workflow(name)
    _permissions(f"{name} (workflow level)", workflow.get("permissions"))
    for job_id, job in workflow["jobs"].items():
        _permissions(f"{name} job {job_id}", job.get("permissions"))


@pytest.mark.parametrize("name", sorted(NEEDS_ID_TOKEN))
def test_no_workflow_level_id_token(name):
    granted = _permissions(name, _workflow(name).get("permissions"))
    assert "id-token" not in granted, f"{name} grants id-token to every job: {granted}"


@pytest.mark.parametrize("name", sorted(NEEDS_ID_TOKEN))
def test_id_token_is_on_exactly_the_jobs_that_authenticate_to_google(name):
    jobs = _workflow(name)["jobs"]
    have = {
        job_id
        for job_id, job in jobs.items()
        if _permissions(f"{name} job {job_id}", job.get("permissions")).get("id-token") == "write"
    }
    authenticating = {job_id for job_id, job in jobs.items() if any(_auth_steps(job))}
    assert authenticating == NEEDS_ID_TOKEN[name], (
        f"{name}: the jobs running google-github-actions/auth are {sorted(authenticating)}, "
        f"this test expects {sorted(NEEDS_ID_TOKEN[name])}. A new auth job is a new holder of "
        "a Google identity: add it here deliberately."
    )
    assert have == authenticating, (
        f"{name}: id-token: write on {sorted(have)}, but the jobs that authenticate are "
        f"{sorted(authenticating)}"
    )


@pytest.mark.parametrize("name", sorted(NEEDS_ID_TOKEN))
def test_every_job_using_workload_identity_keeps_contents(name):
    for job_id, job in _workflow(name)["jobs"].items():
        if any(_auth_steps(job)):
            assert _permissions(name, job.get("permissions")).get("contents"), (
                f"{name}'s {job_id} job's permissions block drops contents (a block replaces the workflow's)"
            )


def _deployer_workflows_in_terraform() -> set[str]:
    text = WIF_TF.read_text()
    match = re.search(r"^\s*deployer_workflows\s*=\s*\[(.*?)\]", text, re.M | re.S)
    assert match, f"{WIF_TF} declares no deployer_workflows list; the deployer is not pinned to workflow files"
    body = "\n".join(l.split("#", 1)[0] for l in match.group(1).splitlines())
    return set(re.findall(r'"([^"]+)"', body))


def _workflows_authenticating_as_the_deployer() -> set[str]:
    found = set()
    for path in sorted(WORKFLOWS.glob("*.yml")):
        workflow = _workflow(path.name)
        for job in (workflow.get("jobs") or {}).values():
            for step in _auth_steps(job):
                if "GCP_DEPLOY_SA" in str((step.get("with") or {}).get("service_account", "")):
                    found.add(path.name)
    return found


def test_the_deployer_trusts_exactly_the_workflows_that_authenticate_as_it():
    """The trust pin and the workflows are one list stated twice, so they are compared.

    A workflow missing from the pin fails its auth step on main with
    `Permission 'iam.serviceAccounts.getAccessToken' denied`; a file in the pin
    that no longer authenticates is standing trust nobody uses.
    """
    assert _deployer_workflows_in_terraform() == _workflows_authenticating_as_the_deployer()


def test_the_ci_fixer_workflow_is_not_trusted_as_the_deployer():
    assert "ci-fix.yml" not in _deployer_workflows_in_terraform(), (
        "ci-fix.yml has its own account (terraform/bootstrap/ci_fix.tf); trusting it as the "
        "deployer hands a job that comments on pull requests projectIamAdmin on a shared project"
    )
