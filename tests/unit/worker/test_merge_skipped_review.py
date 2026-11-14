"""A review the size gate skipped says SKIPPED, and the merge step decides (#4101).

A peer project reported that a review under the gate's line threshold ended
with `verdict.json` saying MERGE beside a `skipped` field, so anything that
read `verdict` alone -- a person, a script -- took an unreviewed change for
a reviewed approval. The gate now writes `{"verdict": "SKIPPED", "skipped":
"<reason>"}`. What a skip lets through is unchanged, and decided by the
reader from the skip, never from a MERGE nobody wrote:

  * the merge step merges a skipped-review change once CI is green, by its
    own rule (`merge.SKIPPED_REVIEW_MERGES`), and records the skip;
  * a verdict gate compares a skip as MERGE (`verdict.gate_reading`), so a
    gate on NOT_YET stays shut and the integrator publishes as before;
  * a review that RAN may not write SKIPPED.

No credentials, no network: GitHub is `merge_world`'s fake.
"""

from __future__ import annotations

import json

import pytest

from agent_worker import expected_outputs, merge
from agent_worker import verdict as verdict_mod
from agent_worker.errors import InputUnavailable
from swarm_common.models import EndCause
from swarm_common.states import TaskState

from merge_world import MergeWorld
from test_merge_review_not_yet import NOT_YET, _serve_hold

REASON = ("review skipped: 12 changed lines (the gate runs a review from 300), "
          "no risk path touched")
SKIPPED = {"verdict": "SKIPPED", "findings": [], "skipped": REASON}


def _skipped_world(tmp_path, *, labelled: bool = False) -> MergeWorld:
    world = MergeWorld(tmp_path)
    world.verdict = dict(SKIPPED)
    world.pr["labels"] = [{"name": "review-not-yet"}] if labelled else []
    _serve_hold(world)
    return world


# ---------------------------------------------------------------------------
# The merge step: today's behaviour, decided from the skip
# ---------------------------------------------------------------------------


def test_the_merge_step_still_merges_a_skipped_review_once_ci_is_green(tmp_path):
    """MUTATION: drop the skip from `approved` in `run_merge` and this is refused
    `verdict_not_merge`: the change under the gate would never merge."""
    world = _skipped_world(tmp_path)

    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert len(world.merge_calls()) == 1
    # The summary says what happened, not a MERGE nobody wrote.
    assert outcome.summary["verdict"] == "SKIPPED"
    assert outcome.summary[merge.SKIPPED_REVIEW_KEY] == REASON


def test_a_skipped_review_clears_review_not_yet_as_a_merge_does(tmp_path):
    world = _skipped_world(tmp_path, labelled=True)

    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    (removed,) = world.github.calls("DELETE", NOT_YET)
    (merged,) = world.merge_calls()
    assert world.github.seen.index(removed) < world.github.seen.index(merged)


def test_whether_a_skip_merges_is_the_merge_steps_own_rule(tmp_path, monkeypatch):
    """The decision is `SKIPPED_REVIEW_MERGES`, read from the skip: with it off,
    the same verdict is refused, and nothing merges."""
    monkeypatch.setattr(merge, "SKIPPED_REVIEW_MERGES", False)
    world = _skipped_world(tmp_path)

    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.FAILED
    assert outcome.end_cause is EndCause.MERGE_REFUSED
    assert outcome.summary["refusal"]["code"] == "verdict_not_merge"
    assert "SKIPPED, not MERGE" in outcome.summary["refusal"]["message"]
    assert world.merge_calls() == []


def test_a_skipped_verdict_that_names_no_reason_merges_nothing(tmp_path):
    world = _skipped_world(tmp_path)
    world.verdict = {"verdict": "SKIPPED", "findings": []}

    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.FAILED
    assert outcome.summary["refusal"]["code"] == "verdict_unreadable"
    assert world.merge_calls() == []


# ---------------------------------------------------------------------------
# The reader and the gate
# ---------------------------------------------------------------------------


def test_a_skipped_reviews_verdict_reads_as_skipped_not_merge():
    read = verdict_mod.read_verdict_bytes(json.dumps(SKIPPED).encode(), task_id="task_r",
                                          filename="verdict.json")

    assert read.verdict == verdict_mod.SKIPPED_VERDICT == "SKIPPED"
    assert read.verdict != "MERGE"
    assert read.skipped == REASON
    assert verdict_mod.SKIPPED_VERDICT not in verdict_mod.REVIEW_VERDICTS


def test_a_gate_compares_a_skip_as_merge_so_a_not_yet_gate_stays_shut():
    """The integrator's gate (on NOT_YET) stays shut and it publishes, as it
    did when the file said MERGE; a gate on MERGE opens."""
    read = verdict_mod.read_verdict_bytes(json.dumps(SKIPPED).encode(), task_id="task_r",
                                          filename="verdict.json")
    shut = verdict_mod.gate_from_dispatch(
        {"verdict_gate": {"task_id": "task_r", "verdict_in": ["NOT_YET"]}})
    opens = verdict_mod.gate_from_dispatch(
        {"verdict_gate": {"task_id": "task_r", "verdict_in": ["MERGE"]}})

    assert verdict_mod.gate_reading(read) == "MERGE"
    assert verdict_mod.gate_reading(read) not in shut.verdict_in
    assert verdict_mod.gate_reading(read) in opens.verdict_in


def test_a_reviews_own_verdict_reads_through_the_gate_unchanged():
    read = verdict_mod.verdict_of_document({"verdict": "not_yet", "findings": ["x"]})
    assert read is not None and read.skipped is None
    assert verdict_mod.gate_reading(read) == "NOT_YET"


@pytest.mark.parametrize("document", [
    {"verdict": "SKIPPED", "findings": []},
    {"verdict": "SKIPPED", "findings": [], "skipped": "   "},
    {"verdict": "SKIPPED", "findings": [], "skipped": 3},
])
def test_a_skip_that_names_no_reason_is_no_verdict(document):
    assert verdict_mod.verdict_of_document(document) is None
    with pytest.raises(InputUnavailable, match="has `verdict` 'SKIPPED'"):
        verdict_mod.read_verdict_bytes(json.dumps(document).encode(), task_id="task_r",
                                       filename="verdict.json")


def test_a_review_that_ran_may_not_write_skipped(tmp_path):
    """SKIPPED is the size gate's word for a review that never ran."""
    (tmp_path / "verdict.json").write_text(json.dumps(SKIPPED))

    problem = expected_outputs.verdict_problem(tmp_path)

    assert problem is not None and "'SKIPPED'" in problem
    (tmp_path / "verdict.json").write_text(json.dumps({"verdict": "MERGE", "findings": []}))
    assert expected_outputs.verdict_problem(tmp_path) is None
