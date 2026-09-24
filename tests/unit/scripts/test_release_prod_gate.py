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

A RE-RUN IS THE THIRD WAY PAST IT. GitHub asks for an approval only for a job
that names a protected environment, and a partial re-run -- "Re-run failed
jobs", "Re-run this job" -- starts a job again together with every job that
depends on it and KEEPS the result and outputs of every other job (REST API
reference: "Re-run all of the failed jobs and their dependent jobs"; "Re-run
a job and its dependent jobs"). Re-running a failed `terraform apply (prod)`
leaves `approval` out, so `needs.approval.result` is still the 'success' of
the attempt the reviewer approved -- possibly days earlier, against a plan
nobody has seen. The re-run tests carry an approved first attempt into a
second one and hold every prod-facing job to not starting, and the re-run to
going red rather than green-having-done-nothing; a full re-run, which runs the
approval again, still reaches everything. A job output a job-level `if:` reads
is rendered from the job's `outputs:` at the attempt that job ran in (the
model renders only expressions over `github` and the inputs, and says so when
it meets anything else).

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


_NEEDS_OUTPUT = re.compile(r"\bneeds\.([\w-]+)\.outputs\.([\w-]+)\b")


def _read_outputs(jobs: dict) -> dict[str, set[str]]:
    """job id -> the names of its outputs that some job-level `if:` reads."""
    read: dict[str, set[str]] = {}
    for job in jobs.values():
        for owner, name in _NEEDS_OUTPUT.findall(str(job.get("if", ""))):
            read.setdefault(owner, set()).add(name)
    return read


def _outputs(jobs: dict, job_id: str, ctx: dict, attempt: int, *, started: bool) -> dict[str, str]:
    """The outputs of `job_id` that a job-level `if:` reads, as GitHub renders
    them at the end of `job_id` when it ran in `attempt`; '' for a job that
    never started. Only the outputs a condition reads are rendered, so an
    output built from a step (`steps.images.outputs.tag`) is never asked for."""
    declared = jobs[job_id].get("outputs") or {}
    out: dict[str, str] = {}
    for name in sorted(_read_outputs(jobs).get(job_id, set())):
        assert name in declared, (
            f"a job-level `if:` in release.yml reads needs.{job_id}.outputs.{name}, which {job_id!r} "
            "does not declare: it is always empty"
        )
        if not started:
            out[name] = ""
            continue
        try:
            out[name] = _render(declared[name], **{**ctx, "github.run_attempt": str(attempt)})
        except NameError:
            pytest.fail(
                f"release.yml's {job_id} job output {name!r} is {declared[name]!r}; this model renders "
                "only expressions over `github` and the inputs -- extend it before relying on it"
            )
    return out


def _runs(
    job_id: str, job: dict, results: dict, ctx: dict, *, outputs: dict | None = None, cancelled: bool = False
) -> bool:
    """Whether GitHub would start `job_id`, given how the jobs it needs ended,
    what they output, and whether the run has been cancelled. `ctx` carries
    `github.run_attempt`, the attempt being decided."""
    needs = _needs(job)
    # A cancelled run is not a successful one: GitHub's success() is false
    # once the run is cancelled, whatever the jobs before this one did.
    succeeded = all(results[n] == "success" for n in needs) and not cancelled
    text = _condition(f"{job_id} job", job.get("if"))
    values = {**ctx, "always()": True, "success()": succeeded, "cancelled()": cancelled}
    values.update({f"needs.{n}.result": results[n] for n in needs})
    for n in needs:
        values.update({f"needs.{n}.outputs.{k}": v for k, v in (outputs or {}).get(n, {}).items()})
    return bool(_evaluate("${{ " + text + " }}", **values))


def _attempts(
    jobs: dict,
    ctx: dict,
    gate: str | None = None,
    *,
    attempt: int = 1,
    carried: dict | None = None,
    endings: tuple[str, ...] = _ENDINGS,
):
    """Every way one attempt of a run under `ctx` can unfold, as
    ({job id: result}, {job id: outputs}), with 'skipped' for a job that
    never started.

    The job `gate`, when it starts, is never approved. `carried` makes the
    attempt a partial re-run: {job id: (result, outputs)} from the attempt
    before, for every job this one does not start again. GitHub keeps those
    jobs' results and outputs and decides only the rest afresh."""
    order = _order(jobs)
    carried = carried or {}
    here = {**ctx, "github.run_attempt": str(attempt)}

    def walk(i: int, results: dict, outs: dict):
        if i == len(order):
            yield dict(results), dict(outs)
            return
        job_id = order[i]
        if job_id in carried:
            options = [carried[job_id]]
        elif _runs(job_id, jobs[job_id], results, here, outputs=outs):
            made = _outputs(jobs, job_id, ctx, attempt, started=True)
            options = [(e, made) for e in (_NOT_APPROVED if job_id == gate else endings)]
        else:
            options = [("skipped", _outputs(jobs, job_id, ctx, attempt, started=False))]
        for result, out in options:
            results[job_id], outs[job_id] = result, out
            yield from walk(i + 1, results, outs)
        del results[job_id], outs[job_id]

    yield from walk(0, {}, {})


def _schedules(jobs: dict, ctx: dict, gate: str | None = None):
    """Every way a first attempt under `ctx` can unfold, as {job id: result}.
    The job `gate`, when it starts, is never approved."""
    for results, _ in _attempts(jobs, ctx, gate):
        yield results


def _downstream(jobs: dict, job_id: str) -> set[str]:
    """Every job that needs `job_id`, directly or through another job."""
    return {j for j in jobs if job_id in _upstream(jobs, j)}


def _partial_reruns(jobs: dict, keep: str) -> list[frozenset[str]]:
    """Every set of jobs one partial re-run can start again while `keep`
    keeps its earlier result.

    GitHub re-runs a job together with every job that depends on it -- "Re-run
    failed jobs" re-runs "all of the failed jobs and their dependent jobs",
    "Re-run this job" "a job and its dependent jobs" -- so each set is a union
    of such closures. Any job may be the one picked, not only a failed one:
    the permissive reading again."""
    closures = {j: frozenset({j} | _downstream(jobs, j)) for j in jobs}
    ids = sorted(jobs)
    found: set[frozenset[str]] = set()
    for picked in itertools.product((False, True), repeat=len(ids)):
        union = frozenset().union(*(closures[j] for j, p in zip(ids, picked) if p))
        if union and keep not in union:
            found.add(union)
    return sorted(found, key=sorted)


def _holders(jobs: dict, what: str) -> dict[str, list[str]]:
    """job id -> the names of its steps that do `what`."""
    out: dict[str, list[str]] = {}
    for job_id, job in jobs.items():
        for step in job.get("steps") or []:
            if PROD_FACING[what](_shell(step)):
                out.setdefault(job_id, []).append(step.get("name") or step.get("run", "").strip()[:60])
    return out


def _facing(jobs: dict) -> dict[str, list[str]]:
    """job id -> '<what>: <step>' for every prod-facing step the job holds."""
    out: dict[str, list[str]] = {}
    for what in sorted(PROD_FACING):
        for job_id, steps in _holders(jobs, what).items():
            out.setdefault(job_id, []).extend(f"{what}: {s}" for s in steps)
    return out


def _prod_gate(jobs: dict, ctx: dict) -> str:
    gates = sorted(j for j, job in jobs.items() if _environment(job, ctx) == "prod")
    assert len(gates) == 1, f"release.yml names the prod environment on {gates or 'no job'}; expected exactly one"
    return gates[0]


_EXITS_NONZERO = re.compile(r"(^|\n)\s*exit\s+[1-9][0-9]*\s*$")


def _fails_on_purpose(job: dict) -> bool:
    """A job that exists to go red: nothing prod-facing, and a step with no
    `if:` whose shell ends in `exit <non-zero>`."""
    steps = job.get("steps") or []
    if any(PROD_FACING[w](_shell(s)) for s in steps for w in PROD_FACING):
        return False
    return any(s.get("if") is None and _EXITS_NONZERO.search(_shell(s).rstrip()) for s in steps)


def _approved_then_rerun(jobs: dict, ctx: dict, gate: str):
    """(re-run set, second attempt) for every partial re-run that leaves
    `gate` out, from every first attempt in which `gate` was approved -- the
    first attempts reduced to what the re-run keeps, so each distinct starting
    point is tried once."""
    first = [(r, o) for r, o in _attempts(jobs, ctx) if r[gate] == "success"]
    assert first, f"no first attempt of release.yml gets {gate!r} approved; the re-run tests would check nothing"
    for rerun in _partial_reruns(jobs, gate):
        starts: dict[str, dict] = {}
        for results, outs in first:
            carried = {j: (results[j], outs[j]) for j in sorted(jobs) if j not in rerun}
            starts.setdefault(repr(carried), carried)
        for carried in starts.values():
            for second, _ in _attempts(jobs, ctx, attempt=2, carried=carried):
                yield rerun, second


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
    here = {**ctx, "github.run_attempt": "1"}
    for job_id, steps in sorted(holders.items()):
        needs = _needs(jobs[job_id])
        # Outputs as the jobs needed would write them in this very attempt:
        # an approval that is current, the permissive reading.
        outs = {n: _outputs(jobs, n, ctx, 1, started=True) for n in needs}
        for ended in itertools.product(("success", "failure", "cancelled", "skipped"), repeat=len(needs)):
            results = dict(zip(needs, ended))
            tried += 1
            assert not _runs(job_id, jobs[job_id], results, here, outputs=outs, cancelled=True), (
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
                values = {**ctx, "github.run_attempt": "1", "always()": True, "success()": False, "cancelled()": True}
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


def _expected(ctx: dict) -> list[str]:
    """What a release under `ctx` must be able to do. A `skip_build` release
    promotes nothing by design -- it redeploys what the channel holds -- but
    still applies and deploys."""
    return [w for w in sorted(PROD_FACING) if not (w == "promotes" and ctx["github.event.inputs.skip_build"] == "true")]


@pytest.mark.parametrize("attempt", (1, 2), ids=("first-run", "rerun-all-jobs"))
@pytest.mark.parametrize("release", sorted(PROD_RELEASES) + sorted(DEV_RELEASES))
def test_an_approved_release_still_reaches_every_prod_facing_step(release, attempt):
    """The other half. A workflow whose promotion or apply can never run at
    all would pass the test above; this one fails it.

    Also as "Re-run all jobs" (attempt 2, nothing carried): that is the one
    re-run that runs the approval again, so it is the way to retry a prod
    release, and it must still get all the way through. An approval that
    recorded a constant, not the attempt it cleared, would pass the first
    run and fail here.

    And a release in which every job succeeds must not start a job that
    exists to fail, or every release goes red."""
    ctx = {**PROD_RELEASES, **DEV_RELEASES}[release]
    jobs = _workflow("release.yml")["jobs"]
    schedules = [results for results, _ in _attempts(jobs, ctx, attempt=attempt)]
    for what in _expected(ctx):
        for job_id in _holders(jobs, what):
            assert any(s[job_id] != "skipped" for s in schedules), (
                f"on a {release} release (attempt {attempt}), release.yml's {job_id!r} job ({what}) can "
                "never run, even when everything before it succeeds"
            )
    clean, _ = next(_attempts(jobs, ctx, attempt=attempt, endings=("success",)))
    loud = sorted(j for j, r in clean.items() if r != "skipped" and _fails_on_purpose(jobs[j]))
    assert not loud, (
        f"a {release} release (attempt {attempt}) in which every job succeeds still starts {loud}, which "
        f"exists to fail: every such release goes red. The run: {clean}"
    )


@pytest.mark.parametrize("release", sorted(PROD_RELEASES))
def test_a_partial_rerun_of_an_approved_prod_release_starts_nothing_prod_facing(release):
    """The owner approves a prod dispatch; `terraform apply (prod)` then fails
    -- the state lock, a transient 409. Days later anyone with write access
    clicks "Re-run failed jobs" (GitHub allows it for 30 days). The re-run
    starts the apply and the deploy again but not `approval`, which keeps its
    'success', and a job that names no environment is not held for a
    reviewer: the apply would re-plan against that day's state of the shared
    project, apply it, and dispatch the smoke suite, with nobody having
    approved that plan. Before the approval moved to its own job, the apply
    and the deploy named prod themselves and every re-run asked again.

    Every partial re-run that leaves the approval out, from every first
    attempt in which it was approved, and every way the re-run jobs can end:
    no job holding a prod-facing step may start."""
    ctx = PROD_RELEASES[release]
    jobs = _workflow("release.yml")["jobs"]
    gate = _prod_gate(jobs, ctx)
    facing = _facing(jobs)
    assert facing, "no step of release.yml is prod-facing; this test would pass without checking anything"

    tried = 0
    for rerun, second in _approved_then_rerun(jobs, ctx, gate):
        restarted = sorted(set(rerun) & set(facing))
        if not restarted:
            continue
        tried += 1
        started = {j: facing[j] for j in restarted if second[j] != "skipped"}
        assert not started, (
            f"re-running {sorted(rerun)} of a {release} release whose {gate!r} job was approved in the attempt "
            f"before starts {started} WITHOUT asking anyone: {gate!r} is not re-run, so "
            f"`needs.{gate}.result` is still that attempt's 'success', and GitHub holds only a job that names "
            f"the prod environment. Tie the job to the attempt that was approved. The re-run: {second}"
        )
    assert tried, "no partial re-run restarted a prod-facing job, so this checked nothing"


@pytest.mark.parametrize("release", sorted(PROD_RELEASES))
def test_a_partial_rerun_of_an_approved_prod_release_goes_red_saying_so(release):
    """Skipping is not enough. A re-run whose every job is skipped ends GREEN,
    so an operator who re-ran a failed `terraform apply (prod)` would read
    success for a release that changed nothing. Every partial re-run that
    restarts a prod-facing job without the approval must start a job that
    exists to fail -- nothing prod-facing, a step with no `if:` ending in
    `exit 1` -- whose error says to re-run all jobs instead."""
    ctx = PROD_RELEASES[release]
    jobs = _workflow("release.yml")["jobs"]
    gate = _prod_gate(jobs, ctx)
    facing = set(_facing(jobs))

    tried = 0
    for rerun, second in _approved_then_rerun(jobs, ctx, gate):
        if not set(rerun) & facing:
            continue
        tried += 1
        loud = [j for j in sorted(rerun) if second[j] != "skipped" and _fails_on_purpose(jobs[j])]
        assert loud, (
            f"re-running {sorted(rerun)} of a {release} release whose {gate!r} job was approved in the attempt "
            f"before starts no job that fails, so the re-run ends green whatever it did or skipped. "
            f"The re-run: {second}"
        )
    assert tried, "no partial re-run restarted a prod-facing job, so this checked nothing"


@pytest.mark.parametrize("release", sorted(DEV_RELEASES))
def test_a_partial_rerun_of_a_dev_release_still_runs(release):
    """dev stays un-gated (owner, 2026-09-24), and that includes re-running
    it. A dev apply that failed on the state lock is retried with "Re-run
    failed jobs"; that must promote, apply and deploy again -- not skip, and
    not go red asking for an approval dev does not have. Each prod-facing job
    is re-run on its own, with everything before it carried from a first
    attempt that succeeded."""
    ctx = DEV_RELEASES[release]
    jobs = _workflow("release.yml")["jobs"]
    first, first_outs = next(_attempts(jobs, ctx, endings=("success",)))

    checked = 0
    for what in _expected(ctx):
        for job_id in sorted(_holders(jobs, what)):
            rerun = {job_id} | _downstream(jobs, job_id)
            carried = {j: (first[j], first_outs[j]) for j in sorted(jobs) if j not in rerun}
            second, _ = next(_attempts(jobs, ctx, attempt=2, carried=carried, endings=("success",)))
            checked += 1
            assert second[job_id] == "success", (
                f"re-running {job_id!r} ({what}) of a {release} release does not run it: dev has no reviewer, "
                f"so nothing about a re-run should stop it. The re-run: {second}"
            )
            loud = sorted(j for j in rerun if second[j] != "skipped" and _fails_on_purpose(jobs[j]))
            assert not loud, (
                f"re-running {job_id!r} of a {release} release starts {loud}, which exists to fail: a dev "
                f"re-run goes red. The re-run: {second}"
            )
    assert checked, f"no prod-facing job of a {release} release was re-run, so this checked nothing"


@pytest.mark.parametrize("release", sorted(DEV_RELEASES))
def test_a_dev_release_never_names_the_prod_environment(release):
    """dev stays un-gated (owner, 2026-09-24): its release goes through the
    same jobs, and the one that names an environment names `dev`, which has no
    protection rule (measured 2026-09-24), so it passes straight on."""
    ctx = DEV_RELEASES[release]
    jobs = _workflow("release.yml")["jobs"]
    named = {j: _environment(job, ctx) for j, job in jobs.items() if job.get("environment") is not None}
    assert set(named.values()) <= {"dev"}, f"a {release} release names {named}"
