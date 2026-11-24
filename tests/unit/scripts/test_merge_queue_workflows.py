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
"""

from __future__ import annotations

from .test_ci_gate import SHA, gate, tf_jobs, wait


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
