"""ci-gate judges a merge queue entry by its `merge_group` runs.

OWNER DECISION, 2026-10-06 (observer P26). main was red from 19:12 to 19:58Z
because two pull requests were each green on their own base and merged nine
seconds apart: the ruleset did not require a branch to be up to date, so
neither ran against the other. The owner chose a merge queue.

A queue builds a temporary `gh-readonly-queue/main/...` branch and waits for
the required checks to report on it, through the `merge_group` event. These
run scripts/ci-gate.sh over test_ci_gate's fixture workflows and fake GitHub:
on `merge_group` it expects every workflow it gates whatever changed, waits
for that event's runs and fails with them.

The tests that hold the workflows' own `on:` blocks and job conditions to that
(who can start them, what mints a cloud identity, which jobs report on a queue
entry) read the CI YAML and are kept; the ones that read the docs' prose or the
ruleset body quoted there were deleted by issue 71.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from .test_ci_gate import GATE_WORKFLOW, SHA, gate, tf_jobs, wait

REPO = Path(__file__).resolve().parents[3]
WORKFLOWS = REPO / ".github" / "workflows"

#: The required status checks of ruleset `main-protection`, read
#: with `gh api repos/<owner>/<repo>/rules/branches/main` on
#: 2026-10-01 and unchanged at 2026-10-06 (test_auto_merge_workflow.py's
#: RULESET_CHECKS is the same read). A context added to the ruleset must be
#: added here, or the queue can hold on it unnoticed.
REQUIRED_CONTEXTS = (
    "secret scan",
    "trivy (repo)",
    "checkov (terraform + kubernetes)",
    "platform policy assertions",
    # security.yml's always-run deployment-values job (part of archived issue 2688): the
    # ruleset bodies require it, as test_ci_gate holds them to that file.
    "deployment-values",
    "ci-gate",
)

#: The clause every job's `if:` in application.yml, security.yml and
#: terraform.yml ends with (the lane rule of 2026-11-12).
LANE_SKIP = ("!(github.event_name == 'pull_request' && startsWith(github.head_ref, 'swarm/') "
             "&& github.event.pull_request.head.repo.full_name == github.repository)")

_COMPARISON = re.compile(r"^github\.event_name\s*(==|!=)\s*'([A-Za-z_]+)'$")


def _load(path: Path) -> dict:
    data = yaml.safe_load(path.read_text())
    if True in data:
        data["on"] = data.pop(True)
    return data


def _all() -> dict[str, dict]:
    return {path.name: _load(path) for path in sorted(WORKFLOWS.glob("*.yml"))}


def _producers(context: str) -> list[tuple[str, str, dict]]:
    """(workflow file, job id, job) of every job whose check is `context`."""
    found = []
    for name, workflow in _all().items():
        for job_id, job in (workflow.get("jobs") or {}).items():
            if str(job.get("name") or job_id) == context:
                found.append((name, job_id, job))
    return found


def _ci_gate_waits_on(tmp_path: Path) -> list[str]:
    """What scripts/ci-gate.sh waits on, asked of the script itself."""
    proc = gate(tmp_path, "workflows")
    assert proc.returncode == 0, proc.stderr
    names = proc.stdout.split()
    assert names, "ci-gate waits on nothing: this test would check nothing"
    return names


def _assert_unfiltered_merge_group(name: str, workflow: dict) -> None:
    on = workflow.get("on")
    assert isinstance(on, dict) and "merge_group" in on, (
        f"{name} does not run on merge_group: its required check never reports "
        "on a queue entry, and the queue drops every entry at its timeout"
    )
    trigger = on["merge_group"] or {}
    assert trigger == {"types": ["checks_requested"]}, (
        f"{name}: merge_group must be exactly `types: [checks_requested]`, with no "
        f"filter that could skip it, not {trigger}"
    )


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


def _triggers(workflow: dict[Any, Any]) -> frozenset[str]:
    """The events a workflow runs on. PyYAML reads a bare `on:` key as True."""
    on = workflow.get("on", workflow.get(True))
    if isinstance(on, str):
        return frozenset({on})
    if isinstance(on, list):
        return frozenset(str(event) for event in on)
    return frozenset(str(event) for event in (on or {}))


def _events(condition: Any, universe: frozenset[str]) -> frozenset[str]:
    """The events on which `condition` can be true; all of them if it does not say."""
    if condition is None:
        return universe
    text = str(condition).strip()
    if text.startswith("${{") and text.endswith("}}"):
        text = text[3:-2].strip()
    # The lane clause every job of application.yml, security.yml and
    # terraform.yml ends with (test_fast_checks_on_lane_prs.py) only narrows a
    # pull request by its head, never rules an event out, so it is dropped:
    # the events read from what precedes it.
    if text == LANE_SKIP or text.endswith(" && " + LANE_SKIP):
        text = without_lane_clause(text)
    # A status check (!cancelled(), !failure(), success(), always()) says nothing
    # about which EVENT runs the job, so a conjunct of one is dropped before the
    # comparison is read (application.yml build, PR 1324).
    text = re.sub(r"!?\b(?:cancelled|failure|success|always)\(\)\s*&&\s*", "", text).strip()
    if "(" in text or "!" in text.replace("!=", ""):
        return universe
    allowed: frozenset[str] = frozenset()
    for disjunct in text.split("||"):
        events = universe
        for conjunct in disjunct.split("&&"):
            match = _COMPARISON.match(conjunct.strip())
            if not match:
                continue
            operator, event = match.groups()
            events = events & (frozenset({event}) if operator == "==" else universe - {event})
        allowed = allowed | events
    return allowed


# ---------------------------------------------------------------------------
# The workflows
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("context", REQUIRED_CONTEXTS)
def test_each_required_context_has_exactly_one_producing_job(context: str):
    """The control for the tests below: a context no job produces would make
    them pass over nothing (and would itself stall every pull request)."""
    producers = _producers(context)
    assert len(producers) == 1, (context, [(n, j) for n, j, _ in producers])


@pytest.mark.parametrize("context", REQUIRED_CONTEXTS)
def test_every_workflow_producing_a_required_context_triggers_on_merge_group(context: str):
    """MUTATION: drop `merge_group` from security.yml or ci-gate.yml, or give
    it a `branches:` or `paths:` filter."""
    ((name, _job_id, _job),) = _producers(context)
    _assert_unfiltered_merge_group(name, _all()[name])


@pytest.mark.parametrize("context", REQUIRED_CONTEXTS)
def test_no_job_producing_a_required_context_is_skipped_on_merge_group(context: str):
    """A skipped required check is green on a pull request, but a job whose
    `if:` never holds on merge_group reports nothing there.
    MUTATION: gate `secret scan` on `github.event_name == 'pull_request'`."""
    ((name, job_id, job),) = _producers(context)
    universe = _triggers(_all()[name])
    assert "merge_group" in _events(job.get("if"), universe), (name, job_id, job.get("if"))


def test_every_workflow_ci_gate_waits_on_triggers_on_merge_group(tmp_path):
    """ci-gate on a queue entry waits for these workflows' merge_group runs at
    the entry's commit. MUTATION: drop `merge_group` from terraform.yml."""
    workflows = _all()
    for name in _ci_gate_waits_on(tmp_path):
        _assert_unfiltered_merge_group(name, workflows[name])



def test_every_gated_workflow_is_expected_on_merge_group_whatever_changed(tmp_path):
    waits_on = gate(tmp_path, "workflows")
    assert waits_on.returncode == 0, waits_on.stderr
    names = set(waits_on.stdout.split())
    assert names, "ci-gate waits on nothing: this test would check nothing"
    listing = tmp_path / "changed.txt"
    listing.write_text("LICENSE\n")
    proc = gate(tmp_path, "expected", "--event", "merge_group", "--changed", str(listing))
    assert proc.returncode == 0, proc.stderr
    assert set(proc.stdout.split()) == names, proc.stdout


def test_ci_gate_waits_for_the_merge_group_runs_and_passes_when_they_pass(tmp_path):
    """MUTATION: keep `--event must be pull_request or push` in ci-gate.sh;
    the gate then fails every queue entry before it reads a run."""
    world = {"runs": [
        {"id": 2, "workflow": "terraform.yml", "head_sha": SHA, "event": "merge_group",
         "jobs": tf_jobs()},
    ]}
    proc, events = wait(tmp_path, world, ["LICENSE"], event="merge_group")
    assert proc.returncode == 0, proc.stderr
    queried = [e["query"].get("event") for e in events if e["event"] == "runs"]
    assert queried and set(queried) == {"merge_group"}, queried


def test_ci_gate_fails_a_merge_group_whose_workflow_failed(tmp_path):
    """The control for the pass above: the gate judges the merge_group runs."""
    world = {"runs": [
        {"id": 2, "workflow": "terraform.yml", "head_sha": SHA, "event": "merge_group",
         "jobs": tf_jobs(**{"terraform test": "failure"}), "conclusion": "failure"},
    ]}
    proc, _events_seen = wait(tmp_path, world, ["LICENSE"], event="merge_group")
    assert proc.returncode != 0
    assert "terraform.yml" in proc.stderr


def test_ci_gate_hands_the_queue_entrys_commits_to_the_script():
    """On merge_group, `github.sha` is the entry's head and the base is
    `merge_group.base_sha`; `pull_request.*` is empty there."""
    job = _load(GATE_WORKFLOW)["jobs"]["ci-gate"]
    (step,) = [s for s in job["steps"] if "ci-gate.sh wait" in str(s.get("run", ""))]
    env = step.get("env") or {}
    assert "github.sha" in str(env.get("HEAD_SHA")), env
    assert "github.event.merge_group.base_sha" in str(env.get("BASE_SHA")), env
    assert env.get("EVENT") == "${{ github.event_name }}", env


def test_nothing_that_mints_a_cloud_identity_runs_on_merge_group():
    """The pool admits `refs/heads/main` only (terraform/bootstrap/wif.tf). A
    `build images` or `plan` on a queue entry would fail auth, fail its run,
    and fail ci-gate on every entry -- and the pressure to "fix" that is the
    pressure to widen the pin, which must never be done.
    MUTATION: put `build images` back to `if: github.event_name != 'pull_request'`."""
    checked = 0
    for name, workflow in _all().items():
        universe = _triggers(workflow)
        if "merge_group" not in universe:
            continue
        for job_id, job in (workflow.get("jobs") or {}).items():
            permissions = job.get("permissions") or {}
            mints = isinstance(permissions, dict) and permissions.get("id-token") == "write"
            if not mints:
                continue
            checked += 1
            assert "merge_group" not in _events(job.get("if"), universe), (
                f"{name} job {job_id} holds id-token: write and runs on merge_group"
            )
    # application.yml's build, terraform.yml's plan: both are on merge_group
    # workflows now, so an empty sweep means this checked nothing.
    assert checked >= 2, checked


@pytest.mark.parametrize("name", ["release.yml", "auto-merge.yml"])
def test_release_and_auto_merge_never_run_on_merge_group(name: str):
    """A release is for a commit on main, which a queue entry is not yet."""
    assert "merge_group" not in _triggers(_all()[name])


def test_no_workflow_but_the_required_ones_and_ci_gates_runs_on_merge_group(tmp_path):
    """Each extra workflow on merge_group is runner time per queue entry for
    a check nothing requires."""
    expected = {name for context in REQUIRED_CONTEXTS for name, _j, _job in _producers(context)}
    expected |= set(_ci_gate_waits_on(tmp_path))
    on_queue = {name for name, wf in _all().items() if "merge_group" in _triggers(wf)}
    assert on_queue == expected, on_queue ^ expected
