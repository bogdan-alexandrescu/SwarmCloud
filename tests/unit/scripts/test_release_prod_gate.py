"""A prod release waits for the owner's approval before anything prod-facing
happens, and :prod promotion sits behind that same approval.

Owner decision, 2026-09-24 (docs/ci.md, "A prod release waits for approval
before anything prod-facing"). Before it, release.yml's `images and promote`
job moved :prod to the new digests, and only THEN did `terraform apply (prod)`
wait for a reviewer. A prod release that was rejected, or simply left
waiting, had already pointed :prod at images prod does not run -- and a
`skip_build` redeploy of prod deploys whatever :prod points at.

release.yml runs only on main or by dispatch, so nothing on a pull request
executes it. This file reads the workflow and SCHEDULES it. For a prod
dispatch, with and without skip_build, it walks the job graph in dependency
order, evaluates each job-level `if:` against the results of the jobs that job
needs, and lets every job that runs end in each way a job can end -- except
the job that names the prod environment, which is made to be rejected
(failure), to time out or be cancelled (cancelled), or never to be reached
(skipped). No job holding a prod-facing step may run in any of those
schedules.

A step inside the prod-environment job itself counts as gated: GitHub holds
that whole job, before its first step, until a reviewer approves it.

`always()` is why this simulates rather than reading `needs:`. A job that
needs the approval but says `if: always() && ...` runs whatever the approval's
result was, unless its condition itself requires the approval to have
succeeded. A test that only checked `needs:` would pass that workflow.

THE MODEL, AND WHICH WAY IT ERRS. GitHub's job-level `success()` is false when
any job upstream failed, was cancelled or was skipped; the model reads it over
the DIRECT needs only, which lets more jobs run than GitHub would. So a job
the model says cannot run cannot run on GitHub either. `cancelled()` is
whether the RUN was cancelled: false in the schedules above, true in the
cancellation test below, where `success()` is false too, as GitHub has it.
`failure()` is not modelled: a job-level `if:` using it fails here with a
request to extend the model, rather than being guessed at.

CANCELLING IS THE OTHER WAY TO SAY NO. A reviewer who approves and then sees
the wrong commit cancels the run. GitHub still starts, after a cancel, any job
whose `if:` holds -- `always()` does, by definition -- so a prod-facing job
conditioned on `always()` survives the cancel that was meant to stop it, and
so does an `always()` step after the one a cancel lands on. The two
cancellation tests hold every prod-facing job, and every prod-facing step, to
not starting in a cancelled run, whatever ran before it.

WHAT THIS CANNOT PROVE: that the `prod` environment in repository settings has
a required reviewer. Measured 2026-09-24 with `gh api .../environments`:
reviewer bogdan-alexandrescu, deployment branch policy `main` only, and
`can_admins_bypass: true`. GitHub creates an environment a workflow names if
it does not exist, with no protection at all, so the gate is only as real as
that setting. Nor that a real prod release waits: the first prod dispatch
after this lands is the proof.
"""

from __future__ import annotations

import itertools
import re

import pytest

from .test_release_reuses_ci_images import _code, _evaluate, _needs, _render, _upstream, _workflow

PROD_RELEASES = {
    "prod": {
        "github.event_name": "workflow_dispatch",
        "github.event.inputs.environment": "prod",
        "github.event.inputs.skip_build": "false",
    },
    "prod-skip-build": {
        "github.event_name": "workflow_dispatch",
        "github.event.inputs.environment": "prod",
        "github.event.inputs.skip_build": "true",
    },
}
DEV_RELEASES = {
    # On a push, github.event.inputs is null.
    "push": {
        "github.event_name": "push",
        "github.event.inputs.environment": None,
        "github.event.inputs.skip_build": None,
    },
    "dev": {
        "github.event_name": "workflow_dispatch",
        "github.event.inputs.environment": "dev",
        "github.event.inputs.skip_build": "false",
    },
    "dev-skip-build": {
        "github.event_name": "workflow_dispatch",
        "github.event.inputs.environment": "dev",
        "github.event.inputs.skip_build": "true",
    },
}

_PUSH_IMAGES = re.compile(r"(^|[\s/])push-images\.sh(\s|$)")

# What "prod-facing" means here: a step that changes what prod is or runs, or
# acts against the running deployment. Reading (verify, obtaining and
# scanning images, `terraform plan`) is not in it.
#
# Matched against the step's `run:` AS TEXT, comments dropped, and erring
# toward a match: an `echo` that merely says "terraform apply" counts as an
# apply. That already happened once (the pre-approval summary), and the fix is
# to reword the prose, never to narrow these -- a narrower match is one that a
# real apply can slip past.
PROD_FACING = {
    # Moves a channel tag. push-images.sh does, in every mode but --scan-only
    # (test_push_images_scan_only.py holds it to moving nothing); so does
    # gcloud directly.
    "promotes": lambda code: bool(re.search(r"docker\s+tags\s+(add|delete)\b", code))
    or any("--scan-only" not in line for line in code.splitlines() if _PUSH_IMAGES.search(line)),
    "applies": lambda code: bool(re.search(r"\bterraform\b[^\n]*\s(apply|destroy|import)\b", code)),
    "deploys": lambda code: bool(re.search(r"(^|[\s/])(deploy|verify-remote)\.sh(\s|$)", code)),
}

_STATUS_CALL = re.compile(r"\b(always|success|failure|cancelled)\(\s*\)")
_ENDINGS = ("success", "failure", "cancelled")
_NOT_APPROVED = ("failure", "cancelled")  # rejected; timed out or cancelled


def _shell(step: dict) -> str:
    """The step's shell with comments dropped and continuation lines joined."""
    return re.sub(r"\\\n\s*", " ", _code(step.get("run", "")))


def _environment(job: dict, ctx: dict):
    named = job.get("environment")
    if isinstance(named, dict):
        named = named.get("name")
    return None if named is None else _render(named, **ctx)


def _order(jobs: dict) -> list[str]:
    done: set[str] = set()
    order: list[str] = []

    def visit(job_id: str, path: tuple[str, ...]) -> None:
        assert job_id not in path, f"release.yml's needs form a cycle: {' -> '.join(path + (job_id,))}"
        if job_id in done:
            return
        for need in _needs(jobs[job_id]):
            assert need in jobs, f"release.yml's {job_id} job needs {need!r}, which is not a job"
            visit(need, path + (job_id,))
        done.add(job_id)
        order.append(job_id)

    for job_id in jobs:
        visit(job_id, ())
    return order


def _condition(owner: str, condition) -> str:
    """An `if:` as the expression GitHub evaluates, status function included.
    `owner` names the job or step, for the failure message."""
    if condition is None:
        return "success()"
    if isinstance(condition, bool):
        # YAML read `if: true` as a bool. Python's literal, because this text
        # is only ever handed to _evaluate.
        return f"success() && {condition!r}"
    body = re.fullmatch(r"\s*\$\{\{(.*)\}\}\s*", str(condition), re.S)
    text = body.group(1) if body else str(condition)
    used = set(_STATUS_CALL.findall(text))
    modelled = {"always", "success", "cancelled"}
    assert used <= modelled, (
        f"release.yml's {owner} conditions on {sorted(used - modelled)}(), which this "
        "model does not evaluate; extend the model before relying on it"
    )
    if not used:
        # GitHub's rule: a condition with no status function runs only if
        # everything before it succeeded, as if it began `success() &&`.
        text = f"success() && ({text})"
    return text


def _runs(job_id: str, job: dict, results: dict, ctx: dict, *, cancelled: bool = False) -> bool:
    """Whether GitHub would start `job_id`, given how the jobs it needs ended
    and whether the run has been cancelled."""
    needs = _needs(job)
    # A cancelled run is not a successful one: GitHub's success() is false
    # once the run is cancelled, whatever the jobs before this one did.
    succeeded = all(results[n] == "success" for n in needs) and not cancelled
    text = _condition(f"{job_id} job", job.get("if"))
    values = {**ctx, "always()": True, "success()": succeeded, "cancelled()": cancelled}
    values.update({f"needs.{n}.result": results[n] for n in needs})
    return bool(_evaluate("${{ " + text + " }}", **values))


def _schedules(jobs: dict, ctx: dict, gate: str | None = None):
    """Every way a run under `ctx` can unfold, as {job id: result} with
    'skipped' for a job that never started. The job `gate`, when it starts,
    is never approved."""
    order = _order(jobs)

    def walk(i: int, results: dict):
        if i == len(order):
            yield dict(results)
            return
        job_id = order[i]
        if _runs(job_id, jobs[job_id], results, ctx):
            endings = _NOT_APPROVED if job_id == gate else _ENDINGS
        else:
            endings = ("skipped",)
        for ending in endings:
            results[job_id] = ending
            yield from walk(i + 1, results)
        del results[job_id]

    yield from walk(0, {})


def _holders(jobs: dict, what: str) -> dict[str, list[str]]:
    """job id -> the names of its steps that do `what`."""
    out: dict[str, list[str]] = {}
    for job_id, job in jobs.items():
        for step in job.get("steps") or []:
            if PROD_FACING[what](_shell(step)):
                out.setdefault(job_id, []).append(step.get("name") or step.get("run", "").strip()[:60])
    return out


@pytest.mark.parametrize("what", sorted(PROD_FACING))
@pytest.mark.parametrize("release", sorted(PROD_RELEASES))
def test_nothing_prod_facing_runs_before_the_prod_approval(release, what):
    ctx = PROD_RELEASES[release]
    jobs = _workflow("release.yml")["jobs"]
    gates = sorted(j for j, job in jobs.items() if _environment(job, ctx) == "prod")
    assert gates, "no job of release.yml names the prod environment on a prod release: nothing waits for a reviewer"
    holders = _holders(jobs, what)
    assert holders, f"no step of release.yml {what}; this test would pass without checking anything"

    for job_id, steps in sorted(holders.items()):
        if job_id in gates:
            continue  # GitHub holds the whole job until a reviewer approves it
        guarding = [g for g in gates if g in _upstream(jobs, job_id)]
        assert guarding, (
            f"release.yml's {job_id!r} job {what} for prod ({'; '.join(steps)}) and does not need, "
            f"directly or through another job, the job that names the prod environment ({', '.join(gates)}): "
            "it runs before anyone approves the release"
        )
        leaks = {}
        for gate in guarding:
            leak = next((s for s in _schedules(jobs, ctx, gate) if s[job_id] != "skipped"), None)
            if leak is None:
                break
            leaks[gate] = leak
        else:
            gate, leak = next(iter(leaks.items()))
            pytest.fail(
                f"release.yml's {job_id!r} job {what} for prod ({'; '.join(steps)}) even when {gate!r} "
                f"is not approved -- its condition does not require the approval to have succeeded. "
                f"One such run: {leak}"
            )


@pytest.mark.parametrize("what", sorted(PROD_FACING))
@pytest.mark.parametrize("release", sorted(PROD_RELEASES))
def test_cancelling_a_prod_release_starts_nothing_prod_facing(release, what):
    """A cancel must stop a prod release after its approval as well as before.

    The window this closes: on a `skip_build` redeploy `promote` is skipped
    the moment `approval` succeeds, so the apply is the very next job. A
    reviewer who approved and cancelled a second later still got an apply if
    the apply's condition was `always() && ...`: GitHub evaluates a job's
    `if:` after a cancel and starts it when it holds. `!cancelled()` gives the
    same "run past a skipped job" that the apply needs and stops on a cancel.

    Every combination of how the jobs a prod-facing job needs ended is tried,
    not only the combinations a real run can reach -- the permissive reading
    again -- so "does not start" here means it cannot start on GitHub."""
    ctx = PROD_RELEASES[release]
    jobs = _workflow("release.yml")["jobs"]
    holders = _holders(jobs, what)
    assert holders, f"no step of release.yml {what}; this test would pass without checking anything"

    tried = 0
    for job_id, steps in sorted(holders.items()):
        needs = _needs(jobs[job_id])
        for ended in itertools.product(("success", "failure", "cancelled", "skipped"), repeat=len(needs)):
            results = dict(zip(needs, ended))
            tried += 1
            assert not _runs(job_id, jobs[job_id], results, ctx, cancelled=True), (
                f"release.yml's {job_id!r} job {what} for prod ({'; '.join(steps)}) and still starts after "
                f"the run is CANCELLED, when the jobs it needs ended {results}: its `if:` holds on a "
                "cancelled run (`always()` does). Condition it on `!cancelled()` instead"
            )
    assert tried, "no combination was tried, so this checked nothing"


_STEP_RESULT = re.compile(r"\bsteps\.([\w-]+)\.(outcome|conclusion)\b")


@pytest.mark.parametrize("what", sorted(PROD_FACING))
@pytest.mark.parametrize("release", sorted(PROD_RELEASES))
def test_cancelling_a_prod_release_mid_job_starts_no_prod_facing_step(release, what):
    """The same, one level down. A cancel that lands while a job is running
    stops the step it lands on, and GitHub then still runs every later step
    of that job whose `if:` holds on a cancelled run -- `always()` does. So a
    step that promotes, applies or deploys must not be conditioned on
    `always()`: `deploy and smoke`'s GKE proof, which dispatches a browser
    task into the environment, ran after a cancel that landed on the smoke
    test. A step with no `if:` is `success()`, false once the run is
    cancelled.

    Every combination of how the steps a condition reads ended is tried."""
    ctx = PROD_RELEASES[release]
    jobs = _workflow("release.yml")["jobs"]
    visited = tried = 0
    for job_id, job in sorted(jobs.items()):
        for index, step in enumerate(job.get("steps") or []):
            if not PROD_FACING[what](_shell(step)):
                continue
            visited += 1
            label = step.get("name") or f"steps[{index}]"
            text = _condition(f"{job_id} job's step {label!r}", step.get("if"))
            read = sorted(set(_STEP_RESULT.findall(text)))
            for ended in itertools.product(("success", "failure", "cancelled", "skipped"), repeat=len(read)):
                values = {**ctx, "always()": True, "success()": False, "cancelled()": True}
                values.update({f"steps.{s}.{field}": e for (s, field), e in zip(read, ended)})
                tried += 1
                assert not _evaluate("${{ " + text + " }}", **values), (
                    f"release.yml's {job_id!r} job's step {label!r} {what} for prod and still runs after the "
                    f"run is CANCELLED part-way through the job (earlier steps ended {dict(zip(read, ended))}): "
                    f"`if: {step.get('if')}` holds on a cancelled run. Condition it on `!cancelled()` instead"
                )
    assert visited, f"no step of release.yml {what}; this test would pass without checking anything"
    assert tried, "no condition was evaluated, so this checked nothing"


@pytest.mark.parametrize("release", sorted(PROD_RELEASES))
def test_a_prod_release_asks_for_approval_once(release):
    """GitHub holds EVERY job that names a protected environment, each for its
    own approval. The owner's decision puts promotion behind "that same
    approval" as the apply; naming prod on the promotion, the apply and the
    deploy separately would ask the reviewer once per job instead."""
    ctx = PROD_RELEASES[release]
    jobs = _workflow("release.yml")["jobs"]
    gates = sorted(j for j, job in jobs.items() if _environment(job, ctx) == "prod")
    assert len(gates) == 1, (
        f"{len(gates)} jobs of release.yml name the prod environment ({', '.join(gates) or 'none'}); "
        f"GitHub holds each for its own approval, so one prod release asks the reviewer {len(gates)} times"
    )


@pytest.mark.parametrize("release", sorted(PROD_RELEASES) + sorted(DEV_RELEASES))
def test_an_approved_release_still_reaches_every_prod_facing_step(release):
    """The other half. A workflow whose promotion or apply can never run at
    all would pass the test above; this one fails it. A `skip_build` release
    promotes nothing by design -- it redeploys what the channel holds -- but
    still applies and deploys."""
    ctx = {**PROD_RELEASES, **DEV_RELEASES}[release]
    jobs = _workflow("release.yml")["jobs"]
    schedules = list(_schedules(jobs, ctx))
    expected = [w for w in sorted(PROD_FACING) if not (w == "promotes" and ctx["github.event.inputs.skip_build"] == "true")]
    for what in expected:
        for job_id in _holders(jobs, what):
            assert any(s[job_id] != "skipped" for s in schedules), (
                f"on a {release} release, release.yml's {job_id!r} job ({what}) can never run, "
                "even when everything before it succeeds"
            )


@pytest.mark.parametrize("release", sorted(DEV_RELEASES))
def test_a_dev_release_never_names_the_prod_environment(release):
    """dev stays un-gated (owner, 2026-09-24): its release goes through the
    same jobs, and the one that names an environment names `dev`, which has no
    protection rule (measured 2026-09-24), so it passes straight on."""
    ctx = DEV_RELEASES[release]
    jobs = _workflow("release.yml")["jobs"]
    named = {j: _environment(job, ctx) for j, job in jobs.items() if job.get("environment") is not None}
    assert set(named.values()) <= {"dev"}, f"a {release} release names {named}"
