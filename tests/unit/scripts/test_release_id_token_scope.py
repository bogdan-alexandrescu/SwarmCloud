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
  because release.yml runs only on main).
"""

from __future__ import annotations

from .test_release_reuses_ci_images import _workflow

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


def test_every_job_using_workload_identity_declares_id_token():
    for job_id, job in _jobs().items():
        uses_auth = any(
            str(step.get("uses", "")).startswith("google-github-actions/auth@")
            for step in job.get("steps") or []
        )
        if uses_auth:
            assert (job.get("permissions") or {}).get("id-token") == "write", (
                f"release.yml's {job_id} job authenticates to Google without declaring id-token: write"
            )
            assert (job.get("permissions") or {}).get("contents"), (
                f"release.yml's {job_id} job's permissions block drops contents (a block replaces the workflow's)"
            )
