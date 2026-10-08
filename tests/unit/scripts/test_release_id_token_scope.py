"""`id-token: write` in release.yml is a job-level grant, never a workflow-level one.

Epic #352 item 9 (docs/merge-step.md M4, R4). With the grant at workflow level
every job in a release run could mint a deploy-scoped Google identity token --
including `verify`, which executes the merged repository's own tests. Merging
to main was therefore code execution as the deployer. The property held here:

* the workflow's own `permissions:` carries no `id-token`;
* `id-token: write` is on exactly the jobs that authenticate to Google, and on
  no other job;
* every job that authenticates to Google through workload identity has it
  (without it the auth step fails at run time, which no pull request exercises
  because release.yml runs only on main);
* `permissions:` is never a string, at workflow or job level. `write-all`
  grants id-token without the word appearing, so the checks above, which read
  the mapping, would pass it (the gap the #455 review found).

This closed the exposure for release.yml only. #457 closes it for every
workflow: application.yml, terraform.yml and security.yml lose their
workflow-level grant (tests/unit/scripts/test_workflow_id_token_scope.py), and
the deployer's workload identity binding is pinned to the workflow files that
authenticate as it by `attribute.job_workflow_ref` (terraform/bootstrap/wif.tf).
The pin holds once the bootstrap root is applied; until then any workflow on
main that holds id-token can still present a token the deployer accepts.
"""

from __future__ import annotations

from .test_release_reuses_ci_images import _workflow
from .test_workflow_id_token_scope import ID_TOKEN_REQUEST_VARS, _auth_steps

NEEDS_ID_TOKEN = {"build", "promote", "infrastructure", "infrastructure-iam", "deploy", "acceptance"}


def _jobs() -> dict:
    return _workflow("release.yml")["jobs"]


def test_no_workflow_level_id_token():
    granted = _workflow("release.yml").get("permissions") or {}
    assert "id-token" not in granted, f"release.yml grants id-token to every job: {granted}"


def test_id_token_is_on_exactly_the_jobs_that_authenticate_to_google():
    have = {
        job_id
        for job_id, job in _jobs().items()
        if ((job.get("permissions") or {}).get("id-token")) == "write"
    }
    assert have == NEEDS_ID_TOKEN, f"id-token: write on {sorted(have)}, expected {sorted(NEEDS_ID_TOKEN)}"


def _undeclared_auth(jobs: dict) -> list[str]:
    """Jobs that authenticate to Google but do not hold id-token: write and contents.

    "Authenticate" is `_auth_steps`: the google-github-actions/auth action, or a
    `run:` (or `with:`/`env:`) step that reads ACTIONS_ID_TOKEN_REQUEST_URL or
    _TOKEN and mints the OIDC token by hand. Matching only the action let a
    hand-rolled `curl` job through unchecked (#453, the #455 review).
    """
    problems = []
    for job_id, job in jobs.items():
        if not any(_auth_steps(job)):
            continue
        permissions = job.get("permissions") or {}
        if permissions.get("id-token") != "write":
            problems.append(f"{job_id} authenticates to Google without declaring id-token: write")
        if not permissions.get("contents"):
            problems.append(f"{job_id}'s permissions block drops contents (a block replaces the workflow's)")
    return problems


def test_every_job_using_workload_identity_declares_id_token():
    problems = _undeclared_auth(_jobs())
    assert not problems, f"release.yml: {problems}"


def test_a_run_step_minting_the_token_by_hand_must_declare_id_token():
    """The check above reads `run:` steps, not only the auth action."""
    url, tok = ID_TOKEN_REQUEST_VARS
    by_hand = {"run": f'curl -sH "Authorization: bearer ${tok}" "${url}&audience=x"'}
    undeclared = {"steps": [by_hand], "permissions": {"contents": "read"}}
    declared = {"steps": [by_hand], "permissions": {"contents": "read", "id-token": "write"}}
    plain = {"steps": [{"run": "pytest tests/unit"}], "permissions": {"contents": "read"}}
    assert _undeclared_auth({"mint": undeclared}) == [
        "mint authenticates to Google without declaring id-token: write"
    ]
    assert _undeclared_auth({"mint": declared, "test": plain}) == []


def test_permissions_are_never_a_string():
    workflow = _workflow("release.yml")
    blocks = [("workflow level", workflow.get("permissions"))]
    blocks += [(f"job {job_id}", job.get("permissions")) for job_id, job in _jobs().items()]
    for owner, block in blocks:
        assert block is None or isinstance(block, dict), (
            f"release.yml {owner} sets permissions to {block!r}; a string such as write-all "
            "grants id-token without naming it"
        )
