"""A merge woken by the owner's answers merges only the option its pull request built.

ANSWER-CONTENT-CHECK (part of #2441, finding 5; owner decision 2026-10-26).
The merge step held for the `needs_owner` questions and, once each was
answered, merged -- checking THAT each was answered, never WHAT was picked.
On 2026-10-26 the owner picked an option the branch did not build on three
lanes (VQ-CAPACITY, VQ-CONFIG-B, BROWSER-POOL-100), and each workflow had to be
cancelled by hand before it merged the opposite of the owner's choice.

The asking agent now writes, per question in `questions.json`, `built`: the
option label its branch implements, or null when the question changes nothing
the branch builds. The merge compares each answered `needs_owner` question's
pick with it:

  * a pick equal to `built` merges;
  * a different pick does not merge: it parks for a pick-fix round
    (PICK-FIX-ROUND, test_merge_pick_fix_round.py), naming the question, the
    pick and the build;
  * free text that names no option does not merge;
  * `built: null` passes as before;
  * a file with no `built` key at all (written before this) passes as before,
    so a lane parked on its answers today is not stranded.

`merge_world.MergeWorld` is green in every respect; each test adds the asking
step's questions and answers.
"""

from __future__ import annotations

import json
from typing import Any

from agent_worker import merge
from swarm_common.states import TaskState

from merge_world import TENANT, WORKFLOW, MergeWorld

IMPLEMENT = "task_impl"

#: The park code of an unbuilt pick (PICK-FIX-ROUND). Spelled out, not imported.
PICK_FIX = "owner_pick_fix_pending"


def _question(**extra: Any) -> dict[str, Any]:
    return {
        "question": "Which tile does a full pool show?",
        "options": [{"label": "Needs a look", "description": "amber"},
                    {"label": "Needs action", "description": "red"}],
        "recommended": "Needs action",
        "context": "the tile reads the pool's ceiling",
        **extra,
    }


def _asked(world: MergeWorld, question: dict[str, Any], answer: dict[str, Any]) -> None:
    """The implement step asked `question` in its questions.json, and the owner gave `answer`."""
    key = f"tenants/{TENANT}/tasks/{IMPLEMENT}/attempts/att_i/artifacts/questions.json"
    data = json.dumps([question]).encode("utf-8")
    world.store.upload_bytes(key, data)
    world.docs[IMPLEMENT] = {
        "tenant_id": TENANT, "workflow_id": WORKFLOW, "step_id": "implement",
        "state": TaskState.SUCCEEDED.value,
        "result_summary": {"questions": 1, "artifacts": [
            {"name": "questions.json", "uri": world.store.uri(key), "bytes": len(data)}]},
        "metadata": {"owner_answers": [{"question_index": 0, **answer}]},
    }
    world.verdict = {"verdict": "MERGE", "findings": [], "needs_owner": [
        {"task_id": IMPLEMENT, "step_id": "implement", "question_index": 0,
         "question": question["question"]}]}


def test_a_pick_equal_to_what_the_branch_built_merges(tmp_path):
    world = MergeWorld(tmp_path)
    _asked(world, _question(built="Needs action"), {"choice": "Needs action", "text": None})

    outcome = merge.run_merge(world.context())

    # MUTATION: refuse every answered question that names `built` and this fails.
    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert len(world.merge_calls()) == 1


def test_a_pick_the_branch_did_not_build_does_not_merge_and_names_the_mismatch(tmp_path):
    world = MergeWorld(tmp_path)
    _asked(world, _question(built="Needs action"), {"choice": "Needs a look", "text": None})

    outcome = merge.run_merge(world.context())

    # MUTATION: check only THAT the question is answered (the 2026-10-26 bug) and this merges.
    assert outcome.state is TaskState.PARKED, outcome.message
    assert world.merge_calls() == []
    # PICK-FIX-ROUND: parked for a round that builds the pick, not refused.
    assert outcome.ci_wait["code"] == PICK_FIX
    message = outcome.summary["wait"]["message"]
    # MUTATION: drop the question, the pick or the build from the reason and the
    # operator cannot tell what to build.
    assert "does not build" in message
    assert "step implement question 0" in message
    assert "'Needs a look'" in message and "'Needs action'" in message
    (row,) = outcome.summary["unbuilt_picks"]
    assert row["question_index"] == 0
    assert row["picked"] == "Needs a look" and row["built"] == "Needs action"
    # Decided before any credential, as the hold is.
    assert world.token_reads == 0


def test_free_text_that_names_no_option_does_not_merge(tmp_path):
    world = MergeWorld(tmp_path)
    _asked(world, _question(built="Needs action"),
           {"choice": None, "text": "neither; make it a third tile"})

    outcome = merge.run_merge(world.context())

    # MUTATION: read a missing choice as agreeing with the build and this merges.
    assert outcome.state is TaskState.PARKED, outcome.message
    assert world.merge_calls() == []
    assert outcome.ci_wait["code"] == PICK_FIX
    assert "free text" in outcome.summary["wait"]["message"]
    assert outcome.summary["unbuilt_picks"][0]["picked"] is None


def test_a_question_that_changes_nothing_the_branch_builds_passes(tmp_path):
    world = MergeWorld(tmp_path)
    _asked(world, _question(built=None), {"choice": "Needs a look", "text": None})

    outcome = merge.run_merge(world.context())

    # MUTATION: compare against a null `built` and every pick is a mismatch.
    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert len(world.merge_calls()) == 1


def test_a_questions_file_written_before_built_existed_merges_as_today(tmp_path):
    world = MergeWorld(tmp_path)
    _asked(world, _question(), {"choice": "Needs a look", "text": None})

    outcome = merge.run_merge(world.context())

    # MUTATION: read a missing `built` as null-means-mismatch and every lane parked
    # on its answers today is stranded.
    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert len(world.merge_calls()) == 1
    assert "unbuilt_picks" not in outcome.summary
