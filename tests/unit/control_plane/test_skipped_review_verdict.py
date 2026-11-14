"""A review the size gate skipped says SKIPPED, read by each control-plane reader (#4101).

The gate's verdict.json was `{"verdict": "MERGE", "skipped": ...}`: a reader
of `verdict` alone took an unreviewed change for an approval. It is now
`{"verdict": "SKIPPED", "skipped": "<reason>"}`, and every control-plane
reader keeps today's outcome by reading the skip under its own named rule:

  * swarm-api's publish of the integrator compares a skip as MERGE at the
    gate (`verdictpublish.gate_reading`) and records both words;
  * the fix loop ends on a skip as on a MERGE (`fixloop.Verdict.ends_loop`);
  * an issue run's merge goes on for it (`issueci._merges_on`);
  * the scheduler reads the gate's recorded reading, not the review's word.

The worker half is tests/unit/worker/test_merge_skipped_review.py. No
credentials, no network, no emulator.
"""

from __future__ import annotations

import json

import pytest

from scheduler import runtime
from swarm_api import fixloop, issueci, reviewgate, verdictpublish

REASON = "review skipped: 12 changed lines (the gate runs a review from 300)"


def _skip_text(reason: str = REASON) -> str:
    return reviewgate.verdict_text(reason)


def test_the_gate_writes_skipped_never_merge():
    """MUTATION: write MERGE in `verdict_text` and a reader of `verdict` alone
    takes the skip for a reviewed approval again."""
    document = json.loads(_skip_text())

    assert document == {"verdict": "SKIPPED", "findings": [], "skipped": REASON}
    assert document["verdict"] != "MERGE"


def test_the_requirements_check_fold_keeps_the_skipped_verdict():
    requirements = [{"index": 0, "met": True, "note": "done"}]
    document = json.loads(reviewgate.verdict_text(REASON, requirements))
    assert document["verdict"] == "SKIPPED" and document["requirements"] == requirements


def test_a_skip_summarys_inline_verdict_says_skipped():
    decision = reviewgate.Decision(False, reviewgate.REASON_SMALL, changed_lines=12)
    (entry,) = reviewgate.skip_summary(decision)["artifacts"]
    assert json.loads(entry["inline"])["verdict"] == "SKIPPED"


# ---------------------------------------------------------------------------
# swarm-api's parser and its gate
# ---------------------------------------------------------------------------


def test_swarm_api_reads_a_skip_as_skipped_and_its_gate_as_merge():
    read = verdictpublish.parse_verdict(_skip_text())

    assert read.verdict == "SKIPPED" and read.skipped == REASON
    assert read.findings == () and read.minors == 0
    # The integrator's gate is on NOT_YET: a skip keeps it shut, so swarm-api
    # publishes the integrator without a fix agent, as it did before.
    assert verdictpublish.gate_reading(read) == "MERGE"
    assert verdictpublish.noop_verdict_reason(read) is None


def test_a_review_that_ran_reads_through_swarm_apis_gate_unchanged():
    read = verdictpublish.parse_verdict(json.dumps({"verdict": "NOT_YET", "findings": ["x"]}))
    assert read.skipped is None and verdictpublish.gate_reading(read) == "NOT_YET"


@pytest.mark.parametrize("document", [
    {"verdict": "SKIPPED", "findings": []},
    {"verdict": "SKIPPED", "findings": [], "skipped": ""},
])
def test_swarm_api_declines_a_skip_that_names_no_reason(document):
    with pytest.raises(verdictpublish.Decline):
        verdictpublish.parse_verdict(json.dumps(document))


# ---------------------------------------------------------------------------
# The fix loop, an issue run's merge, the scheduler
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("verdict", "skipped", "ends"), [
    ("MERGE", None, True),
    ("SKIPPED", REASON, True),
    ("NOT_YET", None, False),
])
def test_the_fix_loop_ends_on_a_skip_as_on_a_merge(verdict, skipped, ends):
    read = fixloop.Verdict(verdict=verdict, findings=(), minors=0, skipped=skipped)
    assert read.ends_loop is ends


def test_an_issue_runs_merge_goes_on_for_a_skipped_review():
    verdict, why = issueci._verdict_of(_skip_text(), None, what="review", name="verdict.json")

    assert (verdict, why) == ("SKIPPED", "")
    assert issueci._merges_on(verdict) is True
    assert issueci._merges_on("MERGE") is True
    assert issueci._merges_on("NOT_YET") is False
    assert issueci._merges_on(None) is False


def test_an_issue_run_reads_a_skip_with_no_reason_as_no_verdict():
    text = json.dumps({"verdict": "SKIPPED", "findings": []})
    verdict, why = issueci._verdict_of(text, None, what="review", name="verdict.json")
    assert verdict is None and "names no reason" in why


def _integrator(record: dict) -> dict:
    return {"state": "SUCCEEDED", "result_summary": {"verdict_gate": record}}


def test_the_scheduler_reads_the_gates_reading_of_a_skip():
    """A re-review gated on NOT_YET behind an integrator whose gate read a skip
    as MERGE is shut, as it was when the file said MERGE."""
    record = {"task_id": "task_r", "verdict": "SKIPPED", "gate_reading": "MERGE",
              "verdict_in": ["NOT_YET"]}
    dispatch = {"verdict_gate": {"task_id": "task_r", "verdict_in": ["NOT_YET"]},
                "builds_on": "task_fix"}

    shut = runtime.republishes(["task_fix"], "task_r", dispatch,
                               lambda _tid: _integrator(record))

    assert shut is not None and shut["verdict"] == "MERGE"
