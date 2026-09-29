"""A dev release whose plan changes IAM stops for the owner; a routine one flows.

Owner decision, 2026-09-28 (#268; docs/ci.md, "A dev release that changes IAM
waits for the owner"). dev stays un-gated for what it releases every day --
new digests, a changed env var -- but a plan that creates, updates, replaces,
deletes or forgets a custom role or an IAM member, binding or policy is a
change to who may do what in a project shared with another team. Such a plan
is applied only in a job that names the `dev-iam` environment, whose required
reviewer is the owner.

The shape this file holds, in release.yml:

* `terraform apply` plans, runs the shared-project guard, and on dev
  classifies the plan's JSON with `plan-guard.sh --classify-iam`, which writes
  the IAM rows to the job summary. Its own apply step runs on prod always
  (prod is unchanged: its approval is `approval (prod)`), and on dev only when
  the classification said "false" -- not when it said "true", and not when it
  said nothing.
* On "true" that job holds the SAVED plan (its checksum is a job output) and
  the `dev-iam` job applies exactly that file after the owner approves. It
  never plans: a re-plan would apply something nobody reviewed.
* `deploy and smoke` waits for the `dev-iam` apply when there is one.

Scheduled with the model in test_release_prod_gate.py, which reads each
job-level `if:` and tries every value the classification can write.

WHAT THIS CANNOT PROVE: that the `dev-iam` environment has a required
reviewer. GitHub creates an environment a workflow names, with no protection,
if it does not exist; docs/ci.md has the `gh api` command that sets it, and
the owner runs it. Nor that Terraform refuses a saved plan whose state has
moved on ("Saved plan is stale") -- that is Terraform's documented behaviour,
not observed here.
"""

from __future__ import annotations

import re

import pytest

from .test_release_prod_gate import (
    DEV_IAM_ENVIRONMENT,
    DEV_RELEASES,
    PROD_FACING,
    PROD_RELEASES,
    STEP_OUTPUTS,
    _attempts,
    _condition,
    _environment,
    _holders,
    _shell,
)
from .test_release_reuses_ci_images import REPO, _evaluate, _needs, _upstream, _workflow

INFRA = "infrastructure"
_STEP_OUTPUT = re.compile(r"^\$\{\{\s*steps\.([\w-]+)\.outputs\.([\w-]+)\s*\}\}$")


def _jobs() -> dict:
    return _workflow("release.yml")["jobs"]


def _iam_job(jobs: dict) -> str:
    named = sorted(j for j, job in jobs.items() if _environment(job, DEV_RELEASES["push"]) == DEV_IAM_ENVIRONMENT)
    assert len(named) == 1, f"release.yml names the {DEV_IAM_ENVIRONMENT} environment on {named or 'no job'}; expected one"
    return named[0]


def _step_writing(job: dict, output: str) -> tuple[str, str]:
    """(step id, step output) behind a job output."""
    declared = (job.get("outputs") or {}).get(output)
    assert declared, f"the job declares no `{output}` output"
    match = _STEP_OUTPUT.match(str(declared).strip())
    assert match, f"the job's `{output}` output is {declared!r}, not one step's output"
    return match.group(1), match.group(2)


def _step(job: dict, step_id: str) -> tuple[int, dict]:
    for index, step in enumerate(job.get("steps") or []):
        if step.get("id") == step_id:
            return index, step
    pytest.fail(f"no step has id {step_id!r}")


def _applying_steps(job: dict) -> list[tuple[int, dict]]:
    return [(i, s) for i, s in enumerate(job.get("steps") or []) if PROD_FACING["applies"](_shell(s))]


def test_one_job_names_dev_iam_and_it_applies():
    jobs = _jobs()
    job_id = _iam_job(jobs)
    assert _applying_steps(jobs[job_id]), f"{job_id!r} names {DEV_IAM_ENVIRONMENT} and applies nothing"
    assert INFRA in _upstream(jobs, job_id), f"{job_id!r} does not need {INFRA!r}, whose plan it applies"
    for release, ctx in PROD_RELEASES.items():
        assert _environment(jobs[job_id], ctx) != "prod", f"{job_id!r} names prod on a {release} release"


def test_the_classification_is_the_step_behind_the_iam_output():
    """The job output the gate reads, the step that writes it, and the plan it
    classifies are one chain. MUTATION: point the output at another step, or
    classify a different file from the one `terraform show -json` wrote."""
    job = _jobs()[INFRA]
    step_id, output = _step_writing(job, "iam")
    assert (INFRA, "iam") in STEP_OUTPUTS
    _, step = _step(job, step_id)
    code = _shell(step)
    assert re.search(r"plan-guard\.sh\s[^\n]*--classify-iam", code), f"step {step_id!r} does not classify: {code!r}"
    assert re.search(rf"\b{output}=", code), f"step {step_id!r} never writes `{output}=`"
    assert re.search(r"--plan\s+\"?\$\{TF_ROOT\}/plan\.json\"?", code), code
    assert '--summary "${GITHUB_STEP_SUMMARY}"' in code or '--summary "$GITHUB_STEP_SUMMARY"' in code, (
        "the IAM rows are not written to the job summary, where the reviewer reads them at the gate"
    )
    shows = [s for s in job["steps"] if re.search(r"terraform[^\n]*show -json plan\.tfplan > \"\$\{TF_ROOT\}/plan\.json\"", _shell(s))]
    assert shows, "the classified plan.json is not `terraform show -json` of plan.tfplan"


@pytest.mark.parametrize("environment", ("dev", "prod"))
@pytest.mark.parametrize("iam", ("true", "false", ""))
def test_terraform_apply_applies_in_job_only_a_plan_classified_free_of_iam(environment, iam):
    """On dev the in-job apply runs only on an explicit "false": "true" goes
    to dev-iam, and "" -- a classification that wrote nothing -- applies
    nowhere rather than un-gated. On prod it runs exactly as before.
    MUTATION: `steps.iam.outputs.iam != 'true'`."""
    job = _jobs()[INFRA]
    step_id, output = _step_writing(job, "iam")
    applying = _applying_steps(job)
    assert len(applying) == 1, f"{INFRA!r} applies in {len(applying)} steps"
    index, step = applying[0]
    text = _condition(f"{INFRA} job's apply", step.get("if"))
    values = {
        "env.ENVIRONMENT": environment,
        f"steps.{step_id}.outputs.{output}": iam,
        "always()": True,
        "success()": True,
        "cancelled()": False,
    }
    runs = bool(_evaluate("${{ " + text + " }}", **values))
    want = environment == "prod" or iam == "false"
    assert runs is want, f"on {environment} with iam={iam!r} the in-job apply {'runs' if runs else 'does not run'}: {text}"
    assert index > _step(job, step_id)[0], "the in-job apply runs before the classification"


@pytest.mark.parametrize("release", sorted(DEV_RELEASES))
def test_nothing_after_an_iam_plan_runs_until_dev_iam_approves(release):
    """Every way a dev release can unfold with the dev-iam job rejected,
    cancelled or unreached: when the plan changed IAM, no other job that
    applies or deploys starts. (`terraform apply` starts -- it plans and
    classifies -- and its apply step is held by the test above.)"""
    ctx = DEV_RELEASES[release]
    jobs = _jobs()
    gate = _iam_job(jobs)
    facing = set()
    for what in ("applies", "deploys"):
        facing |= set(_holders(jobs, what))
    facing -= {gate, INFRA}
    assert facing, "no other job applies or deploys, so this checked nothing"
    tried = 0
    for results, outs in _attempts(jobs, ctx, gate):
        if results[INFRA] != "success" or outs[INFRA].get("iam") != "true":
            continue
        tried += 1
        assert results[gate] in ("failure", "cancelled", "skipped"), results
        started = sorted(j for j in facing if results[j] != "skipped")
        assert not started, f"with an IAM plan and {gate!r} {results[gate]}, {started} started: {results}"
    assert tried, "no schedule classified the plan as IAM, so this checked nothing"


@pytest.mark.parametrize("release", sorted(DEV_RELEASES))
def test_an_approved_iam_plan_is_applied_and_deployed(release):
    ctx = DEV_RELEASES[release]
    jobs = _jobs()
    gate = _iam_job(jobs)
    deploys = sorted(_holders(jobs, "deploys"))
    runs = [
        results
        for results, outs in _attempts(jobs, ctx, endings=("success",))
        if outs[INFRA].get("iam") == "true"
    ]
    assert runs, "no schedule classified the plan as IAM"
    for results in runs:
        assert results[gate] == "success", results
        assert all(results[j] == "success" for j in deploys), results


def test_a_skip_build_dev_release_with_an_iam_plan_still_reaches_dev_iam():
    """PR #274, security review MAJOR 1 (release.yml:626): `infrastructure-iam`
    names no status function in its `if:`, so GitHub reads it as `success() &&
    ...`. `success()` is not scoped to `infrastructure-iam`'s own `needs:`
    (just `infrastructure`, which handles a skipped `promote` explicitly in
    ITS `if:`) -- it looks past that to every job upstream, transitively,
    including `build` and `promote`, both of which a `skip_build` dispatch
    skips. The run then ends green with the IAM plan held but never applied,
    and nobody asked.

    `test_an_approved_iam_plan_is_applied_and_deployed` above already covers
    `dev-skip-build` as one of `DEV_RELEASES`; this names that one schedule on
    its own so the defect this PR fixes has a test that fails on exactly it.

    MUTATION: revert `_runs()` in test_release_prod_gate.py to check direct
    `needs` only (the model before this PR), or drop `!cancelled()` from
    `infrastructure-iam`'s `if:` in release.yml -- either turns this red."""
    ctx = DEV_RELEASES["dev-skip-build"]
    jobs = _jobs()
    gate = _iam_job(jobs)
    assert INFRA in _upstream(jobs, gate), f"{gate!r} does not need {INFRA!r} at all; this checked nothing"
    reached = 0
    for results, outs in _attempts(jobs, ctx, endings=("success",)):
        if outs[INFRA].get("iam") != "true":
            continue
        reached += 1
        assert results[INFRA] == "success", results
        assert results[gate] != "skipped", (
            f"a skip_build dev release with an IAM plan skips {gate!r} entirely, so it is applied nowhere: "
            f"{results}"
        )
    assert reached, "no skip_build schedule classified the plan as IAM, so this checked nothing"


@pytest.mark.parametrize("release", sorted(DEV_RELEASES))
def test_a_routine_dev_release_flows_without_dev_iam(release):
    """A plan free of IAM applies in `terraform apply`, names no dev-iam job,
    and deploys -- the dev release as it was before #268."""
    ctx = DEV_RELEASES[release]
    jobs = _jobs()
    gate = _iam_job(jobs)
    deploys = sorted(_holders(jobs, "deploys"))
    runs = [
        results
        for results, outs in _attempts(jobs, ctx, endings=("success",))
        if outs[INFRA].get("iam") == "false"
    ]
    assert runs, "no schedule classified the plan free of IAM"
    for results in runs:
        assert results[gate] == "skipped", f"a routine dev release started {gate!r}: {results}"
        assert results[INFRA] == "success" and all(results[j] == "success" for j in deploys), results


@pytest.mark.parametrize("release", sorted(DEV_RELEASES))
def test_a_dev_classification_that_wrote_nothing_deploys_nothing(release):
    """Fail closed: '' applied nowhere (above), so nothing may deploy either."""
    ctx = DEV_RELEASES[release]
    jobs = _jobs()
    gate = _iam_job(jobs)
    deploys = sorted(_holders(jobs, "deploys"))
    for results, outs in _attempts(jobs, ctx, endings=("success",)):
        if outs[INFRA].get("iam") == "":
            assert results[gate] == "skipped", results
            assert all(results[j] == "skipped" for j in deploys), results


@pytest.mark.parametrize("release", sorted(PROD_RELEASES))
def test_the_dev_iam_job_never_starts_on_prod(release):
    """Prod keeps its one approval, `approval (prod)`; whatever `iam` reads."""
    ctx = PROD_RELEASES[release]
    jobs = _jobs()
    gate = _iam_job(jobs)
    tried = 0
    for results, _ in _attempts(jobs, ctx):
        tried += 1
        assert results[gate] == "skipped", f"{gate!r} started on a {release} release: {results}"
    assert tried


def test_the_dev_iam_job_applies_the_saved_plan_it_was_shown_and_never_plans():
    """The owner approves the rows in the summary, which came from one saved
    plan. The job must apply that FILE, checked against the checksum
    `terraform apply` took of it, and never plan. MUTATION: add a `terraform
    plan` step, apply without the checksum check, or check it after the apply."""
    jobs = _jobs()
    job_id = _iam_job(jobs)
    job = jobs[job_id]
    steps = job.get("steps") or []
    for step in steps:
        # The subcommand is the first word after terraform's own flags;
        # `${{ }}` is flattened first because it holds spaces.
        code = re.sub(r"\$\{\{.*?\}\}", "X", _shell(step))
        assert not re.search(r"\bterraform\b(?:\s+-\S+)*\s+(plan|refresh)(\s|$)", code, re.M), (
            f"{job_id!r} plans or refreshes ({step.get('name')}): it must apply the reviewed plan, not a new one"
        )
    applying = _applying_steps(job)
    assert len(applying) == 1, f"{job_id!r} applies in {len(applying)} steps"
    apply_index, apply_step = applying[0]
    assert re.search(r"\bapply\b[^\n]*\splan\.tfplan\b", _shell(apply_step)), _shell(apply_step)
    assert "-var" not in _shell(apply_step), "a saved plan carries its variables; passing any is a new plan"

    checks = [i for i, s in enumerate(steps) if re.search(r"sha256sum\s[^\n]*(-c|--check)\b", _shell(s))]
    assert checks and min(checks) < apply_index, f"{job_id!r} does not verify the plan's checksum before applying"
    env = {**(job.get("env") or {}), **(steps[min(checks)].get("env") or {})}
    assert any(f"needs.{INFRA}.outputs.plan_sha256" in str(v) for v in env.values()), (
        f"the checksum {job_id!r} verifies is not the one {INFRA!r} recorded"
    )
    assert INFRA in _needs(job)


def test_the_checksum_is_taken_of_the_plan_that_was_classified():
    """`terraform apply` records the checksum of plan.tfplan -- the file
    `terraform show -json` read -- and holds that file in the state bucket,
    never as a workflow artifact: this repository is public, and a saved plan
    holds every planned value in plaintext."""
    job = _jobs()[INFRA]
    step_id, output = _step_writing(job, "plan_sha256")
    iam_step, iam_output = _step_writing(job, "iam")
    index, step = _step(job, step_id)
    code = _shell(step)
    assert re.search(r"sha256sum\s+\"?\$\{TF_ROOT\}/plan\.tfplan\"?", code), code
    assert re.search(rf"\b{output}=", code), code
    assert re.search(r"gcloud storage cp\s[^\n]*\$\{TF_ROOT\}/plan\.tfplan", code), code
    assert f"steps.{iam_step}.outputs.{iam_output} == 'true'" in str(step.get("if")), step.get("if")
    assert index > _step(job, iam_step)[0]
    for s in job.get("steps") or []:
        if str(s.get("uses", "")).startswith("actions/upload-artifact"):
            assert "tfplan" not in str((s.get("with") or {}).get("path")), "the saved plan is uploaded as an artifact"


def test_the_owner_has_the_command_that_protects_dev_iam():
    """The environment is a repository setting the owner creates."""
    doc = (REPO / "docs" / "ci.md").read_text()
    assert "repos/bogdan-alexandrescu/SwarmCloud/environments/dev-iam" in doc
    assert re.search(r"gh api[^\n]*-X PUT[^\n]*environments/dev-iam", doc) or re.search(
        r"gh api --method PUT[^\n]*\n?[^\n]*environments/dev-iam", doc
    ), "docs/ci.md has no `gh api` PUT for the dev-iam environment"
    assert "reviewers" in doc
