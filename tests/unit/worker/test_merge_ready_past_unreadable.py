"""A person's ready lets the merge go on past `needs_owner` entries it cannot read.

THE LIVE FINDING (10 November 2026). A review wrote its `needs_owner` as
free-text sentences, so the verdict reader counted two unreadable entries,
and the merge parked "2 entries the merge cannot read". The owner answered,
a follow-up built the answers, the checks were green, and a member called
human-ready twice: each attempt read the same verdict and parked again in
seconds, for eight hours. Nothing recorded that the person had said ready.

Now the park names what a ready acknowledges (the review task, the verdict's
digest, the head), swarm-api's `handoff.ready_writes` copies it onto the task
with who and when (`unreadable_acknowledgement`), and the next attempt goes on
past THAT verdict's unreadable entries -- and only those:

  * the same verdict at the same head merges;
  * a newer verdict (another digest) or a moved head parks again;
  * a readable unanswered `needs_owner` question still holds;
  * the review is shown a `needs_owner` example that reads as a reference.

No credentials, no network, no emulator.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from agent_worker import merge, owner_questions, security_review, verdict
from swarm_api import handoff
from swarm_common.states import TaskState

from merge_world import OPENER, TASK, WORKFLOW, MergeWorld

IMPLEMENT = "task_impl"
SECURITY = "task_sec"
NEW_HEAD = "0f" * 20
#: The live review's shape: sentences, not references.
SENTENCES = ["The owner must confirm the flag stays for one more release.",
             "The owner must say whether the route joins the capture list."]


def _implement(world: MergeWorld) -> None:
    world.docs[IMPLEMENT] = {"tenant_id": "eng", "workflow_id": WORKFLOW,
                             "step_id": "implement", "metadata": {"owner_answers": []}}


def _ready(world: MergeWorld, parked: Any, *, by: str = "member@example.com") -> None:
    """A member's human-ready on the park `parked` wrote: what `ready_writes` records."""
    blocked_by = [{"reason": "HUMAN_REQUIRED", "park_phase": "owner_questions",
                   "owner_questions": dict(parked.owner_wait)}]
    acknowledged = handoff.unreadable_acknowledgement(
        blocked_by, by=by, at=datetime(2026, 11, 10, 20, 0, tzinfo=timezone.utc))
    assert acknowledged is not None, parked.owner_wait
    source, record = acknowledged
    world.docs[TASK]["metadata"].setdefault(merge.OWNER_READY_METADATA_KEY, {})[source] = record


def _parked_on_sentences(world: MergeWorld) -> Any:
    world.verdict = {"verdict": "MERGE", "findings": [], "needs_owner": list(SENTENCES)}
    outcome = merge.run_merge(world.context())
    assert outcome.state is TaskState.PARKED, outcome.message
    assert outcome.owner_wait["unreadable"] == 2
    return outcome


def test_the_park_names_what_a_ready_on_it_acknowledges(tmp_path):
    world = MergeWorld(tmp_path)
    outcome = _parked_on_sentences(world)

    basis = outcome.owner_wait["acknowledges"]
    # MUTATION: park without the basis and the ready has nothing to record.
    assert basis["source"] == merge.OWNER_READY_REVIEW
    assert basis["review_task"] == world.target["review"]
    assert basis["verdict_digest"] and len(basis["verdict_digest"]) == 64
    assert basis["head"] == world.docs[OPENER]["result_summary"]["git"]["pushed_head"]
    assert basis["round_heads"] == []
    assert "a member's ready lets the merge go on past them" in outcome.message


def test_a_ready_for_the_same_verdict_and_head_goes_on_past_the_unreadable_entries(tmp_path):
    world = MergeWorld(tmp_path)
    parked = _parked_on_sentences(world)
    _ready(world, parked)

    outcome = merge.run_merge(world.context())

    # MUTATION: hold on `needs_owner_unreadable` on every attempt (the live
    # finding) and this parks again, for ever.
    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert len(world.merge_calls()) == 1
    record = outcome.summary[merge.OWNER_READY_SUMMARY_KEY][merge.OWNER_READY_REVIEW]
    assert record["by"] == "member@example.com" and record["unreadable"] == 2


def test_without_a_ready_the_next_attempt_parks_again(tmp_path):
    world = MergeWorld(tmp_path)
    _parked_on_sentences(world)

    outcome = merge.run_merge(world.context())

    # MUTATION: go on past unreadable entries with no ready recorded and this merges.
    assert outcome.state is TaskState.PARKED, outcome.message
    assert world.merge_calls() == []


def test_a_newer_verdict_parks_again_until_someone_says_ready_again(tmp_path):
    world = MergeWorld(tmp_path)
    parked = _parked_on_sentences(world)
    _ready(world, parked)
    # A re-review wrote another verdict file: another digest.
    world.verdict = {"verdict": "MERGE", "findings": [],
                     "needs_owner": [*SENTENCES, "One more sentence."]}

    outcome = merge.run_merge(world.context())

    # MUTATION: match the acknowledgement on the review task alone and this merges.
    assert outcome.state is TaskState.PARKED, outcome.message
    assert outcome.owner_wait["unreadable"] == 3
    assert world.merge_calls() == []

    _ready(world, outcome)
    assert merge.run_merge(world.context()).state is TaskState.SUCCEEDED


def test_a_moved_head_parks_again(tmp_path):
    world = MergeWorld(tmp_path)
    parked = _parked_on_sentences(world)
    _ready(world, parked)
    world.docs[OPENER]["result_summary"]["git"]["pushed_head"] = NEW_HEAD

    outcome = merge.run_merge(world.context())

    # MUTATION: leave the head out of the comparison and this goes on.
    assert outcome.state is TaskState.PARKED, outcome.message
    assert outcome.owner_wait["unreadable"] == 2
    assert outcome.owner_wait["acknowledges"]["head"] == NEW_HEAD
    assert world.merge_calls() == []


def test_a_readable_unanswered_question_still_holds_after_a_ready(tmp_path):
    world = MergeWorld(tmp_path)
    _implement(world)
    world.verdict = {"verdict": "MERGE", "findings": [], "needs_owner": [
        {"task_id": IMPLEMENT, "step_id": "implement", "question_index": 1,
         "question": "Does the route join the capture list?"},
        SENTENCES[0],
    ]}
    parked = merge.run_merge(world.context())
    assert parked.state is TaskState.PARKED and parked.owner_wait["unreadable"] == 1
    _ready(world, parked)

    outcome = merge.run_merge(world.context())

    # MUTATION: let the ready clear every hold rather than the unreadable
    # entries alone and this merges with the question unanswered.
    assert outcome.state is TaskState.PARKED, outcome.message
    assert [q["question_index"] for q in outcome.owner_wait["questions"]] == [1]
    assert outcome.owner_wait["unreadable"] == 0
    assert world.merge_calls() == []

    world.docs[IMPLEMENT]["metadata"]["owner_answers"] = [
        {"question_index": 1, "choice": "yes", "text": None}]
    assert merge.run_merge(world.context()).state is TaskState.SUCCEEDED


def test_the_metadata_key_is_swarm_apis():
    # MUTATION: rename either side and the ready records what the merge never reads.
    assert merge.OWNER_READY_METADATA_KEY == handoff.OWNER_READY_METADATA_KEY
    assert (merge.OWNER_READY_REVIEW, merge.OWNER_READY_SECURITY) == handoff.OWNER_READY_SOURCES
    assert "owner_questions" == handoff.OWNER_QUESTIONS_PHASE


def test_the_reviews_needs_owner_example_is_a_reference_the_merge_reads(tmp_path):
    text = owner_questions.guidance(tmp_path / owner_questions.NAME,
                                    [{"answer": None}])
    assert json.dumps(verdict.NEEDS_OWNER_EXAMPLE) in text
    # MUTATION: show an example the reader cannot read (a sentence, or no
    # task id) and this counts it unreadable.
    refs, unreadable = verdict.owner_question_refs([verdict.NEEDS_OWNER_EXAMPLE])
    assert unreadable == 0 and len(refs) == 1
    assert "note" in verdict.NEEDS_OWNER_EXAMPLE
    # Old free-text entries still read, as unreadable: nothing written breaks.
    assert verdict.owner_question_refs(list(SENTENCES)) == ((), 2)


def test_a_ready_on_a_security_reviews_unreadable_entries_refuses_rather_than_merges(tmp_path):
    world = MergeWorld(tmp_path)
    world.docs[SECURITY] = {"tenant_id": "eng", "workflow_id": WORKFLOW,
                            "step_id": "security-review", "state": "SUCCEEDED",
                            "metadata": {"owner_answers": []}}
    path = tmp_path / security_review.VERDICT_FILE
    path.write_text(json.dumps({
        "verdict": "NOT_YET",
        "findings": [{"summary": "[high] SQL injection (apps/api/q.py:3)", "severity": "high"}],
        "needs_owner": ["The owner must accept the injection risk."]}))

    def context() -> Any:
        ctx = world.context()
        ctx.staged = {**ctx.staged, security_review.VERDICT_FILE: path}
        return ctx

    parked = merge.run_merge(context())
    assert parked.state is TaskState.PARKED, parked.message
    assert parked.owner_wait["acknowledges"]["source"] == merge.OWNER_READY_SECURITY
    _ready(world, parked)

    outcome = merge.run_merge(context())

    # MUTATION: let the ready clear the security review's unreadable entries
    # and go on, and a confirmed high merges with no answer accepting it.
    assert outcome.state is not TaskState.SUCCEEDED, outcome.message
    assert outcome.state is not TaskState.PARKED, outcome.message
    assert outcome.summary["refusal"]["code"] == merge.SECURITY_VERDICT_NOT_MERGE
    assert world.merge_calls() == []
