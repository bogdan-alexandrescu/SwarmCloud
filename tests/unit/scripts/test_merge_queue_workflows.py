"""Every required check reports on a merge queue's `merge_group` ref.

OWNER DECISION, 2026-10-06 (observer P26). main was red from 19:12 to 19:58Z
because #726 and #727 were each green on their own base and merged nine
seconds apart: ruleset `main-protection` (24160219) has
`strict_required_status_checks_policy: false`, so neither ran against the
other. The owner chose a merge queue. The operator adds the `merge_queue` rule
after this lands (docs/ci.md, "Merging through the merge queue"); this file
holds everything that must already be true when they do.

A queue builds a temporary `gh-readonly-queue/main/...` branch and waits for
the ruleset's required checks to report on it, through the `merge_group`
event. A required check whose workflow does not run on `merge_group` -- or
runs with a filter that skips it, or whose job's `if:` skips it -- never
reports there, and the queue holds the entry until
`check_response_timeout_minutes` and then drops it. So:

  * every workflow that produces a required context triggers on
    `merge_group: types: [checks_requested]`, with no filter;
  * so does every workflow `ci-gate` waits on, or the gate waits for a run
    that never comes and fails the entry by name;
  * no job producing a required context is skipped on `merge_group`;
  * no job that mints a cloud identity runs there: the workload identity
    pool admits `refs/heads/main` only, and a red `build images` or `plan`
    would fail ci-gate on every queue entry;
  * release.yml and auto-merge.yml never run on it.

None of this changes a pull request or a push to main: `merge_group` is a new
trigger beside them, and while the queue is off GitHub never raises it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from .test_ci_gate import GATE_WORKFLOW, SHA, gate, tf_jobs, wait
from .test_workflow_step_reachability import _events, _triggers

REPO = Path(__file__).resolve().parents[3]
WORKFLOWS = REPO / ".github" / "workflows"
CI_DOC = REPO / "docs" / "ci.md"

#: The required status checks of ruleset `main-protection`, id 24160219, read
#: with `gh api repos/bogdan-alexandrescu/SwarmCloud/rules/branches/main` on
#: 2026-10-01 and unchanged at 2026-10-06 (test_auto_merge_workflow.py's
#: RULESET_CHECKS is the same read). A context added to the ruleset must be
#: added here, or the queue can hold on it unnoticed.
RULESET_ID = 24160219
REQUIRED_CONTEXTS = (
    "secret scan",
    "trivy (repo)",
    "checkov (terraform + kubernetes)",
    "platform policy assertions",
    "ci-gate",
)

#: The ruleset change docs/ci.md gives the operator, value by value.
QUEUE_PARAMETERS = {
    "merge_method": "MERGE",
    "grouping_strategy": "ALLGREEN",
    "max_entries_to_build": 5,
    # The group size the owner chose on 2026-10-08 (proposal G); the API
    # refuses a merge_queue rule without it (the 2026-10-07 attempt).
    "max_entries_to_merge": 5,
    "min_entries_to_merge": 1,
    "min_entries_to_merge_wait_minutes": 0,
    "check_response_timeout_minutes": 60,
}


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


def test_the_trigger_reads_as_all_to_ci_gate(tmp_path):
    """ci-gate decides which workflows a change triggers from their `on:`
    blocks. On merge_group every gated workflow is unfiltered, so each one is
    expected, whatever changed."""
    for name in _ci_gate_waits_on(tmp_path):
        proc = gate(tmp_path, "paths", "--workflow", str(WORKFLOWS / name), "--event", "merge_group")
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.split() == ["all"], (name, proc.stdout)
    listing = tmp_path / "changed.txt"
    listing.write_text("LICENSE\n")
    proc = gate(tmp_path, "expected", "--event", "merge_group", "--changed", str(listing))
    assert proc.returncode == 0, proc.stderr
    assert set(proc.stdout.split()) == set(_ci_gate_waits_on(tmp_path)), proc.stdout


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


# ---------------------------------------------------------------------------
# The ruleset change, in the docs
# ---------------------------------------------------------------------------
def _section() -> str:
    text = CI_DOC.read_text()
    start = text.index("## Merging through the merge queue")
    end = text.find("\n## ", start + 1)
    return text[start:] if end < 0 else text[start:end]


def _ruleset_body() -> dict:
    """The JSON body of the section's PUT."""
    section = _section()
    put = section.index(f"gh api -X PUT repos/bogdan-alexandrescu/SwarmCloud/rulesets/{RULESET_ID}")
    start = section.index("<<'JSON'\n", put) + len("<<'JSON'\n")
    end = section.index("\nJSON\n", start)
    return json.loads(section[start:end])


def test_docs_say_why_with_the_incident():
    section = _section()
    for fact in ("2026-10-06", "#726", "#727", "strict"):
        assert fact in section, fact


def test_docs_say_why_with_the_wait_it_removes():
    """Owner decision 2026-10-08 (observer proposal G): the strict policy's
    serial update-branch and re-run is what the queue replaces."""
    section = _section()
    for fact in ("2026-10-08", "p50 75 min", "p90 8.5 h", "update-branch",
                 "Only merge non-failing pull requests", "status check"):
        assert fact in section, fact


def test_docs_say_the_queue_is_refused_on_a_user_owned_repository():
    """The 2026-10-07 attempt was refused with HTTP 422: an operator must not
    read the command below as one that will work here as things stand."""
    section = _section()
    assert "Invalid rule 'merge_queue'" in section
    assert "owned by an organization" in section


def test_the_put_turns_strict_off_and_the_rollback_turns_it_back_on():
    """With strict on, every pull request still needs an update-branch and its
    own CI run before it can join the queue -- the wait the queue removes.
    Rolling back must restore strict, or main has neither protection.
    MUTATION: leave `strict_required_status_checks_policy: true` in the PUT."""
    (checks,) = [r for r in _ruleset_body()["rules"] if r["type"] == "required_status_checks"]
    assert checks["parameters"]["strict_required_status_checks_policy"] is False
    rollback = _section().split("### Rolling back", 1)[1]
    assert "`strict_required_status_checks_policy` back to `true`" in rollback


def test_docs_carry_the_exact_merge_queue_rule():
    rules = _ruleset_body()["rules"]
    (queue,) = [r for r in rules if r.get("type") == "merge_queue"]
    assert queue["parameters"] == QUEUE_PARAMETERS, queue


def test_the_put_keeps_every_rule_the_ruleset_has_today():
    """The PUT replaces the ruleset whole: a rule it leaves out is deleted."""
    body = _ruleset_body()
    assert body["name"] == "main-protection"
    types = [r["type"] for r in body["rules"]]
    assert types == ["deletion", "non_fast_forward", "pull_request", "required_status_checks",
                     "merge_queue"], types
    (checks,) = [r for r in body["rules"] if r["type"] == "required_status_checks"]
    contexts = [c["context"] for c in checks["parameters"]["required_status_checks"]]
    assert contexts == list(REQUIRED_CONTEXTS), contexts


def test_docs_say_how_to_roll_back():
    section = _section().lower()
    assert "roll back" in section or "rolling back" in section
    # Rolling back is the same PUT without the merge_queue rule: the ci-gate
    # section's body, which is this one's minus that rule.
    assert "without the `merge_queue` rule" in section
