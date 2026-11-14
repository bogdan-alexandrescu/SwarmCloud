"""A lane's pull request runs no checks at all; any other pull request runs the
fast checks only; the full suite runs on the merge train's pull request, on
main and wherever `full-ci` asks for it.

THE LANE RULE, the owner's direction of 12 November 2026 ("no testing on lane
pull requests, only on the merge train's run on its way to main, with large
merge trains, to cut CI wait time"; docs/ci.md "A lane's pull request runs no
checks"). On a pull request whose head is a `swarm/` branch of this
repository, every job of application.yml, security.yml and terraform.yml is
skipped by its own `if:`, which ends `&& LANE_SKIP`, and ci-gate passes at
once: it skips every step but its lane note. Each job's `if:` is evaluated
here, the way GitHub does, on a lane's pull request (nothing runs), and on a
`train/` pull request, a push to main, a fork's pull request, another branch's
and a queue entry (exactly what the `if:` without the clause runs).

THE FAST-CHECKS RULE, below, still holds for every other pull request.

Owner decision 2026-11-11 ("skip pr testing and only run tests when merging to
main", "Fast checks only on PRs"; docs/ci.md "What runs where"). Every HEAVY
job of application.yml, terraform.yml and security.yml runs only when the run
is not a pull request, or the pull request's head is a `train/` branch
(scripts/merge-train.sh), or it carries the `full-ci` label.

The properties held here, against the real workflow files:

  * the rule is written ONCE, as the `changes` job's `full` output, and every
    heavy job that can run on a pull request reads it first in its `if:`;
  * a heavy job that never runs on any pull request (an image build, a
    Terraform plan, the published-image scan) keeps its own main-only `if:`;
  * no fast job reads it, and every job of the three files is classified, so a
    new job must be put on one side or the other;
  * the aggregate gates (`ci-gate`, `format / unit tests`,
    `swarm-ui typecheck / component tests`) read a heavy job skipped by the
    rule as a pass and a failed one as a failure, and ci-gate's reuse verdict
    (scripts/lib/ci_reuse.py) does not mistake the skip for an unsourced one.

The workflow lines ship as workflow-lines.patch, pushed by hand: until it is
applied the workflow assertions here fail with PATCH_NOTE.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from .test_ci_reuse import reuse as ci_reuse

ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = ROOT / ".github" / "workflows"

PATCH_NOTE = ("-- the workflow change of 'fast checks only on lane PRs' is not applied: it ships "
              "as workflow-lines.patch, which the operator pushes by hand")

FULL_OUTPUT = ("${{ (github.event_name != 'pull_request' || startsWith(github.head_ref, 'train/') "
               "|| contains(github.event.pull_request.labels.*.name, 'full-ci')) && 'true' || 'false' }}")
FULL_IF = "needs.changes.outputs.full == 'true' && "

#: Heavy jobs that run on a pull request, so they carry the rule.
HEAVY_GATED = {
    "application.yml": {
        "python-unit": "unit tests (${{ matrix.shard }}/${{ strategy.job-total }})",
        "ui-build": "swarm-ui typecheck / build",
        "ui-tests": "swarm-ui component tests (${{ matrix.shard }}/${{ strategy.job-total }})",
        "integration": "integration tests (emulator)",
        "build-check": "build changed images without pushing",
    },
    "terraform.yml": {},
    "security.yml": {},
}
#: Heavy jobs that already never run on any pull request: they need no rule.
HEAVY_MAIN_ONLY = {
    "application.yml": {"build"},
    "terraform.yml": {"plan"},
    "security.yml": {"images"},
}
#: Fast checks: they run on every pull request and never read the rule.
FAST = {
    "application.yml": {"changes", "shell", "workflows", "ruff", "python-checks", "quick-checks", "manifests"},
    "terraform.yml": {"offline", "validate", "test", "checkov", "plan-not-run"},
    "security.yml": {"filesystem", "checks", "secrets", "policy", "iac"},
}
#: Aggregates and bookkeeping: they read other jobs' results.
AGGREGATE = {
    "application.yml": {"python", "ui", "tested-tree", "ci-gate", "plugin-tag"},
    "terraform.yml": set(),
    "security.yml": set(),
}
# The event clauses a main-only job's every disjunct must carry one of.
NOT_A_PULL_REQUEST = ("github.event_name != 'pull_request'", "github.event_name == 'workflow_dispatch'",
                      "github.event_name == 'schedule'")


#: The clause every job's `if:` in the three files ends with (`&& LANE_SKIP`).
LANE_SKIP = ("!(github.event_name == 'pull_request' && startsWith(github.head_ref, 'swarm/') "
             "&& github.event.pull_request.head.repo.full_name == github.repository)")
#: The aggregate that still runs on a lane's pull request, so it reports.
LANE_GATE = ("application.yml", "ci-gate")
THIS_REPO = "example-org/example-repo"


def _jobs(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text())["jobs"]


def _if(job: dict) -> str:
    return str(job.get("if", "")).removeprefix("${{").removesuffix("}}").strip()


@pytest.mark.parametrize("name", sorted(FAST))
def test_every_job_is_classified(name):
    """MUTATION: add a job without saying whether it is heavy or fast."""
    classified = set(HEAVY_GATED[name]) | HEAVY_MAIN_ONLY[name] | FAST[name] | AGGREGATE[name]
    assert set(_jobs(name)) == classified, sorted(set(_jobs(name)) ^ classified)


def test_the_rule_is_written_once_as_the_changes_job_s_output():
    """MUTATION: drop `train/` or `full-ci` from it, or copy it into a job."""
    jobs = _jobs("application.yml")
    assert jobs["changes"]["outputs"].get("full") == FULL_OUTPUT, f"{jobs['changes']['outputs'].get('full')} {PATCH_NOTE}"
    for name in FAST:
        text = (WORKFLOWS / name).read_text()
        expected = 1 if name == "application.yml" else 0
        assert text.count("startsWith(github.head_ref, 'train/')") == expected, name
        assert text.count("labels.*.name, 'full-ci'") == expected, name


@pytest.mark.parametrize("name, job_id", [(n, j) for n, js in HEAVY_GATED.items() for j in js])
def test_each_heavy_job_reads_the_rule_first(name, job_id):
    """MUTATION: drop the rule from one heavy job's `if:`, or rename the job."""
    job = _jobs(name)[job_id]
    assert str(job["name"]) == HEAVY_GATED[name][job_id], job["name"]
    needs = job["needs"] if isinstance(job["needs"], list) else [job["needs"]]
    assert "changes" in needs, needs
    assert _if(job).startswith(FULL_IF), f"{job_id}: {_if(job)} {PATCH_NOTE}"
    assert _if(job).count("outputs.full") == 1, _if(job)


def test_ci_reuse_knows_every_gated_reusable_job():
    """ci-gate's reuse verdict reads REACH; a heavy job it does not know as
    heavy would fail the gate on every lane pull request. MUTATION: drop a key
    from ci_reuse.HEAVY."""
    gated = set(HEAVY_GATED["application.yml"])
    assert set(ci_reuse.HEAVY) == gated & set(ci_reuse.REUSABLE)
    assert ci_reuse.FULL_IF + " && " == FULL_IF


@pytest.mark.parametrize("name, job_id", [(n, j) for n, js in HEAVY_MAIN_ONLY.items() for j in sorted(js)])
def test_a_main_only_heavy_job_never_runs_on_a_pull_request(name, job_id):
    """MUTATION: let `build`, `plan` or the published-image scan run on a pull request."""
    condition = _if(_jobs(name)[job_id])
    assert condition, job_id
    for disjunct in condition.split("||"):
        assert any(clause in disjunct for clause in NOT_A_PULL_REQUEST), (job_id, disjunct)
    assert "outputs.full" not in condition, condition


@pytest.mark.parametrize("name, job_id", [(n, j) for n, js in FAST.items() for j in sorted(js)]
                         + [(n, j) for n, js in AGGREGATE.items() for j in sorted(js)])
def test_no_fast_or_aggregate_job_reads_the_rule(name, job_id):
    """MUTATION: put the rule on ruff, the secret scan or ci-gate."""
    condition = _if(_jobs(name)[job_id])
    for marker in ("outputs.full", "full-ci", "train/"):
        assert marker not in condition, (job_id, condition)


# --- the gates ----------------------------------------------------------------
def _needs(job_ids, *, full: str, failed: str = "") -> dict:
    outputs = {"app": "true", "python": "all", "ui": "true", "full": full, "reused": "[]", "reused_from": ""}
    needs = {"changes": {"result": "success", "outputs": outputs}}
    for job_id in job_ids:
        if job_id == "changes":
            continue
        heavy = job_id in HEAVY_GATED["application.yml"]
        result = "skipped" if heavy and full == "false" else "success"
        needs[job_id] = {"result": "failure" if job_id == failed else result, "outputs": {}}
    return needs


def _verdict(job_id: str, needs: dict) -> subprocess.CompletedProcess:
    step = _jobs("application.yml")[job_id]["steps"][0]
    assert step["env"]["NEEDS"] == "${{ toJSON(needs) }}", step["env"]
    return subprocess.run(["bash", "-c", step["run"]], env={**os.environ, "NEEDS": json.dumps(needs)},
                          capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize("job_id", ["ci-gate", "python", "ui"])
def test_an_aggregate_gate_passes_a_heavy_job_the_rule_skipped(job_id):
    """MUTATION: count `skipped` as a failure in the gate's first step."""
    needs = _needs(_jobs("application.yml")[job_id]["needs"], full="false")
    proc = _verdict(job_id, needs)
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.parametrize("job_id, failed", [("ci-gate", "python-unit"), ("ci-gate", "integration"),
                                             ("ci-gate", "build-check"), ("python", "python-unit"),
                                             ("ui", "ui-tests")])
def test_an_aggregate_gate_still_fails_a_heavy_job_that_ran_and_failed(job_id, failed):
    """MUTATION: pass every result the gate reads."""
    needs = _needs(_jobs("application.yml")[job_id]["needs"], full="true", failed=failed)
    proc = _verdict(job_id, needs)
    assert proc.returncode == 1 and failed in proc.stdout, proc.stdout + proc.stderr


def test_the_reuse_verdict_passes_a_heavy_skip_only_when_the_rule_is_off():
    """With `full` false a skipped heavy job was not reached; with `full`
    true (or absent) the same skip has no source and fails the gate.
    MUTATION: read every heavy skip as a pass, or ignore `full`."""
    jobs = _jobs("application.yml")["ci-gate"]["needs"]
    problems, _ = ci_reuse.gate(_needs(jobs, full="false"), "pull_request")
    assert problems == [], problems
    for full in ("true", ""):
        needs = _needs(jobs, full=full)
        needs["python-unit"]["result"] = "skipped"
        if not full:
            del needs["changes"]["outputs"]["full"]
        problems, _ = ci_reuse.gate(needs, "pull_request")
        assert len(problems) == 1 and "python-unit" in problems[0], (full, problems)


def test_docs_say_what_runs_where():
    """MUTATION: drop the section, or the label's name from it."""
    text = (ROOT / "docs" / "ci.md").read_text()
    start = text.index("## What runs where")
    section = text[start:text.index("\n## ", start + 1)]
    for needle in ("full-ci", "train/", "lane's pull request", "main"):
        assert needle in section, needle


# --- a lane's pull request runs no checks (owner, 2026-11-12) ---------------
_PATH = re.compile(r"\b(?:github|needs|vars|inputs|matrix|steps|runner)(?:\.[A-Za-z0-9_*-]+)+")


def _evaluate(expression: str, values: dict) -> bool:
    """A job's or a step's `if:` as GitHub reads it, for the operators and
    functions these three files use. A status function is the run's state
    (nothing failed or was cancelled); a condition with none of them gets
    GitHub's implicit `success()`. MUTATION: none -- it is the instrument."""
    text = expression.strip().removeprefix("${{").removesuffix("}}").strip()
    names: dict[str, object] = {}

    def bind(match: re.Match) -> str:
        path = match.group(0)
        assert path in values, f"_evaluate() does not model {path!r} in {text}"
        names[f"_v{len(names)}"] = values[path]
        return f"_v{len(names) - 1}"

    py = _PATH.sub(bind, text)
    py = py.replace("&&", " and ").replace("||", " or ")
    py = re.sub(r"!(?!=)", " not ", py)
    py = re.sub(r"\b(startsWith|contains|fromJSON|always|cancelled|failure|success)\(", r"_\1(", py)
    helpers = {
        "_startsWith": lambda value, prefix: str(value or "").lower().startswith(str(prefix).lower()),
        "_contains": lambda haystack, needle: (needle in haystack if isinstance(haystack, list)
                                               else str(needle).lower() in str(haystack or "").lower()),
        "_fromJSON": json.loads,
        "_always": lambda: True, "_cancelled": lambda: False, "_failure": lambda: False,
        "_success": lambda: True,
    }
    value = bool(eval(py, {"__builtins__": {}}, {**names, **helpers}))  # noqa: S307 -- the repo's own workflow
    if not re.search(r"\b(?:always|cancelled|failure|success)\(", text):
        value = value and True  # the implicit success(): nothing failed
    return value


#: name -> (event_name, head_ref, head repository, ref).
EVENTS = {
    "lane": ("pull_request", "swarm/task_lane", THIS_REPO, "refs/pull/7/merge"),
    "train": ("pull_request", "train/41-1", THIS_REPO, "refs/pull/8/merge"),
    "main": ("push", "", None, "refs/heads/main"),
    "queue": ("merge_group", "", None, "refs/heads/gh-readonly-queue/main/pr-7-abc"),
    "fork": ("pull_request", "swarm/task_lane", "someone/example-repo", "refs/pull/9/merge"),
    "other branch": ("pull_request", "fix/thing", THIS_REPO, "refs/pull/10/merge"),
    "schedule": ("schedule", "", None, "refs/heads/main"),
}


def _values(event: str) -> dict:
    """Every value the three files' job and step conditions read, for one
    event, with every job before it passed and `changes` saying the change
    reaches everything (full suite)."""
    event_name, head_ref, head_repo, ref = EVENTS[event]
    is_pr = event_name == "pull_request"
    full = "true" if (not is_pr or head_ref.startswith("train/")) else "false"
    values = {
        "github.event_name": event_name,
        "github.head_ref": head_ref,
        "github.ref": ref,
        "github.repository": THIS_REPO,
        "github.event.pull_request.head.repo.full_name": head_repo if is_pr else None,
        "github.event.pull_request.labels.*.name": [] if is_pr else None,
    }
    outputs = {"app": "true", "python": "all", "ui": "true", "full": full, "reused": "[]", "images": "true"}
    for key, value in outputs.items():
        values[f"needs.changes.outputs.{key}"] = value
    for job in ("changes", "python", "offline", "checks"):
        values[f"needs.{job}.result"] = "success"
    return values


def _all_jobs():
    for name in sorted(FAST):
        for job_id, job in _jobs(name).items():
            yield name, job_id, job


def _without_the_clause(condition: str) -> str:
    """The job's `if:` before the lane clause, unwrapped: what it ran before
    2026-11-12."""
    inner = condition.strip().removeprefix("${{").removesuffix("}}").strip()
    if inner == LANE_SKIP:
        return ""
    assert inner.endswith(" && " + LANE_SKIP), inner
    before = inner[: -len(" && " + LANE_SKIP)]
    if before.startswith("(") and before.endswith(")") and _balanced(before[1:-1]):
        before = before[1:-1]
    return before


def without_lane_clause(condition: object, *, required: bool = True) -> str:
    """A job's `if:` as it was written before the lane clause, `${{ }}` and
    all, for the tests that pin a job's own condition (test_ci_fold.py,
    test_build_images_local.py). Fails when the clause is missing, unless
    `required` is false (a sweep over every job, ci-gate included)."""
    text = str(condition or "").strip()
    inner = text.removeprefix("${{").removesuffix("}}").strip()
    if not required and not (inner == LANE_SKIP or inner.endswith(" && " + LANE_SKIP)):
        return text
    before = _without_the_clause(text)
    return "${{ " + before + " }}" if text.startswith("${{") and before else before


def _balanced(text: str) -> bool:
    depth = 0
    for char in re.sub(r"'[^']*'", "", text):
        depth += {"(": 1, ")": -1}.get(char, 0)
        if depth < 0:
            return False
    return depth == 0


@pytest.mark.parametrize("name, job_id", [(n, j) for n, j, _ in _all_jobs() if (n, j) != LANE_GATE])
def test_every_job_ends_its_if_with_the_lane_clause(name, job_id):
    """MUTATION: drop the clause from one job, add a job without it, or put it
    inside an `||` where it does not bind the whole condition."""
    condition = str(_jobs(name)[job_id].get("if", ""))
    assert condition, f"{name} {job_id} has no `if:`, so it runs on a lane's pull request"
    assert condition.count(LANE_SKIP) == 1, condition
    _without_the_clause(condition)
    inner = condition.strip().removeprefix("${{").removesuffix("}}").strip()
    before = inner[: -len(LANE_SKIP)].removesuffix(" && ")
    # The clause binds the whole condition: no `||` outside parentheses before it.
    depth = 0
    stripped = re.sub(r"'[^']*'", "''", before)
    for i, char in enumerate(stripped):
        depth += {"(": 1, ")": -1}.get(char, 0)
        assert not (depth == 0 and stripped.startswith("||", i)), f"{job_id}: an unparenthesised || in {before}"


@pytest.mark.parametrize("name, job_id", [(n, j) for n, j, _ in _all_jobs() if (n, j) != LANE_GATE])
def test_a_lane_s_pull_request_runs_no_job(name, job_id):
    """MUTATION: drop the clause from any job, or match `swarm/` from a fork."""
    assert _evaluate(str(_jobs(name)[job_id].get("if", "true")), _values("lane")) is False, job_id


@pytest.mark.parametrize("event", ["train", "main", "queue", "fork", "other branch", "schedule"])
def test_every_other_event_runs_exactly_what_it_ran_before(event):
    """The clause is false on every event but a lane's pull request, so each
    job runs exactly when its `if:` without the clause says. MUTATION: write
    the clause so that it also matches `train/`, a push or a fork."""
    values = _values(event)
    for name, job_id, job in _all_jobs():
        if (name, job_id) == LANE_GATE:
            continue
        condition = str(job.get("if", ""))
        before = _without_the_clause(condition)
        expected = _evaluate(before, values) if before else True
        assert _evaluate(condition, values) is expected, (event, name, job_id)


@pytest.mark.parametrize("event", ["train", "main"])
def test_a_train_s_pull_request_and_a_push_to_main_run_the_full_suite(event):
    """MUTATION: skip a heavy job on the train, or on main."""
    values = _values(event)
    ran = {(n, j) for n, j, job in _all_jobs() if (n, j) != LANE_GATE and _evaluate(str(job.get("if", "true")), values)}
    heavy = {("application.yml", j) for j in HEAVY_GATED["application.yml"]}
    if event == "main":
        heavy -= {("application.yml", "build-check")}  # a pull request's image check
        heavy |= {("application.yml", "build"), ("terraform.yml", "plan")}
    fast = {(n, j) for n, js in FAST.items() for j in js} - {("terraform.yml", "plan-not-run")}
    assert heavy <= ran, sorted(heavy - ran)
    assert fast <= ran, sorted(fast - ran)


def test_ci_gate_runs_on_a_lane_s_pull_request_and_judges_nothing():
    """ci-gate still reports (it is a required check), starts as soon as the
    skipped jobs it needs are done, and every step but the lane note is
    skipped -- nothing to judge, no checkout, no wait for terraform.yml.
    MUTATION: let the gate's judgement, checkout, reuse or wait run on a lane."""
    gate = _jobs("application.yml")["ci-gate"]
    lane = _values("lane")
    assert _evaluate(str(gate["if"]), lane) is True
    assert gate["name"] == "ci-gate"
    steps = gate["steps"]
    running = [step.get("name") or step.get("uses") for step in steps if _evaluate(str(step.get("if", "true")), lane)]
    assert running == ["a lane's pull request runs no checks"], running
    assert "scripts/ci-gate.sh" not in steps[-1]["run"] and "sleep" not in steps[-1]["run"]


@pytest.mark.parametrize("event", ["train", "main", "queue", "fork", "other branch"])
def test_ci_gate_judges_everything_else_as_before(event):
    """MUTATION: skip the gate's judgement or its wait on a train or main."""
    steps = _jobs("application.yml")["ci-gate"]["steps"]
    values = _values(event)
    running = [step.get("name") or step.get("uses") for step in steps
               if _evaluate(str(step.get("if", "true")), values)]
    assert "a lane's pull request runs no checks" not in running
    assert running[0] == "every job of this workflow passed, or was skipped by its own if"
    assert "wait for terraform.yml at this commit" in running
    assert "a job skipped by reuse passes only when the reuse step listed it" in running


def test_the_lane_gate_s_first_step_is_still_the_needs_judgement():
    """ci-gate's first step is the one test_ci_gate.py and the tests above run
    as its verdict; the lane note comes last."""
    steps = _jobs("application.yml")["ci-gate"]["steps"]
    assert steps[0]["env"]["NEEDS"] == "${{ toJSON(needs) }}"
    assert steps[0]["if"] == "${{ " + LANE_SKIP + " }}"
    assert steps[-1]["if"] == "${{ " + LANE_SKIP[2:-1] + " }}"


def test_docs_say_a_lane_s_pull_request_runs_no_checks_and_why():
    """MUTATION: drop the subsection, its reason, or the gather it relies on."""
    text = (ROOT / "docs" / "ci.md").read_text()
    start = text.index("## What runs where")
    section = text[start:text.index("\n## ", start + 1)]
    assert "### A lane's pull request runs no checks" in section
    for needle in ("swarm/", "CI wait time", "owner's direction", "ci-gate", "skipped", "train/",
                   "8 pull requests", "15 minutes", "up to 40", LANE_SKIP):
        assert needle in section, needle
