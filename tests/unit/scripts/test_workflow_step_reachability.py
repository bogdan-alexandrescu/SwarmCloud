"""No workflow step is gated on an event its own job can never run on.

WHY THIS EXISTS. `terraform.yml`'s `plan` job is gated
`github.event_name != 'pull_request'` -- deliberately: a pull request cannot
mint the deployer's token, see the job's own header. Inside it sat a
"comment on the PR" step gated `github.event_name == 'pull_request'`. The two
conditions cannot both hold, so the step never ran, on any event, ever -- and it
read as a feature: the plan summary "is posted to the PR". It was also the only
reason the workflow asked for `pull-requests: write`, so a dead step kept a live
permission on every run. Found in a lane review on 2026-09-24.

A step like that is invisible in the checks list (a skipped step is green) and
invisible in review (each condition is correct on its own). So it is computed,
not read: for every job, the events it can run on; for every step, the events
its `if:` allows; a step whose set does not meet its job's set is dead.

WHAT IS MODELLED, AND WHAT IS NOT. Only comparisons of `github.event_name`
against a quoted literal, joined by `&&` and `||`. Anything else -- `always()`,
`needs.*.result`, a parenthesised expression, `!` -- is treated as "may be
true on any event". That can only make this test MISS a dead step, never
invent one, which is the right direction for a check that fails a build.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
WORKFLOWS = sorted((REPO / ".github" / "workflows").glob("*.yml"))

_COMPARISON = re.compile(r"^github\.event_name\s*(==|!=)\s*'([A-Za-z_]+)'$")


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


def test_the_workflows_were_found():
    """The test below is parametrised over this glob; an empty glob checks nothing."""
    names = {path.name for path in WORKFLOWS}
    assert {"application.yml", "terraform.yml", "security.yml", "release.yml"} <= names, names


def test_the_model_sees_the_exact_shape_that_was_dead():
    universe = frozenset({"push", "pull_request", "workflow_dispatch"})
    job = _events("github.event_name != 'pull_request'", universe)
    step = _events("github.event_name == 'pull_request'", universe)
    assert job == {"push", "workflow_dispatch"}
    assert step == {"pull_request"}
    assert not job & step
    # ...and does not call anything it cannot evaluate dead.
    assert _events("always() && steps.verify.outcome == 'success'", universe) == universe
    assert _events("github.event.inputs.skip_build != 'true'", universe) == universe
    assert _events(
        "github.event_name == 'schedule' || github.event_name == 'workflow_dispatch'",
        universe | {"schedule"},
    ) == {"schedule", "workflow_dispatch"}


@pytest.mark.parametrize("workflow_path", WORKFLOWS, ids=lambda path: path.name)
def test_no_step_is_gated_on_an_event_its_job_never_runs_on(workflow_path: Path):
    """MUTATION: re-add a step with `if: github.event_name == 'pull_request'` to
    terraform.yml's `plan` job (or any job gated `!= 'pull_request'`), or gate a
    release.yml job on `pull_request`, which release.yml is never triggered by."""
    workflow = yaml.safe_load(workflow_path.read_text())
    universe = _triggers(workflow)
    assert universe, f"{workflow_path.name}: read no triggers from `on:`"

    dead: list[str] = []
    visited = 0
    for job_id, job in (workflow.get("jobs") or {}).items():
        job_events = _events(job.get("if"), universe)
        if not job_events:
            dead.append(f"jobs.{job_id}: `if: {job.get('if')}` never holds on {sorted(universe)}")
            continue
        for index, step in enumerate(job.get("steps") or []):
            visited += 1
            if not _events(step.get("if"), universe) & job_events:
                label = step.get("name") or step.get("uses") or f"steps[{index}]"
                dead.append(
                    f"jobs.{job_id} step {label!r}: `if: {step.get('if')}` never holds "
                    f"where the job runs ({sorted(job_events)}, from `if: {job.get('if')}`)"
                )
    assert visited, f"{workflow_path.name}: visited no steps, so this checked nothing"
    assert not dead, (
        f"{workflow_path.name} has steps that can never run:\n  "
        + "\n  ".join(dead)
        + "\nA skipped step is green, so this reads as a working feature. Delete it, "
        "and any permission it was the only user of."
    )
