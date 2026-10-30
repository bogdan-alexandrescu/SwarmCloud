"""The merge step holds only for the owner questions the review marked blocking.

#2070, owner decision 2026-10-23 (option (c)). The review reads the workflow's
owner questions (`owner-questions.json`) and copies into `verdict.needs_owner`
each unanswered one that should hold the merge; the rest go in
`verdict.open_questions`. The merge step:

  * parks HUMAN_REQUIRED while any `needs_owner` question is still
    unanswered, naming each question's step and index -- re-reading the
    answers first, so one answered after the review counts as answered;
  * merges past open NON-blocking questions, and lists them on the pull
    request so nothing asked is invisible;
  * does exactly what it did before for a verdict that carries neither.

`merge_world.MergeWorld` is green in every respect; each test adds questions.
"""

from __future__ import annotations

import json
from typing import Any

from agent_worker import merge
from agent_worker.errors import ExitCode
from swarm_common.states import EventType, ParkReason, TaskState

from worker_seeds import seed_attempt

from merge_world import API, MERGED, NUMBER, OPENER, TASK, TENANT, WORKFLOW, MergeWorld

IMPLEMENT = "task_impl"


def _implement(world: MergeWorld, answers: list[dict[str, Any]] | None = None) -> None:
    world.docs[IMPLEMENT] = {
        "tenant_id": "eng", "workflow_id": WORKFLOW, "step_id": "implement",
        "metadata": {"owner_answers": list(answers or [])},
    }


def _ref(index: int, question: str) -> dict[str, Any]:
    return {"task_id": IMPLEMENT, "step_id": "implement", "question_index": index,
            "question": question}


def _comments(world: MergeWorld) -> list[str]:
    return [c.body["body"] for c in world.github.calls("POST", f"{API}/issues/{NUMBER}/comments")]


def test_a_blocking_question_parks_human_required_naming_its_step_and_index(tmp_path):
    world = MergeWorld(tmp_path)
    _implement(world)
    world.verdict = {"verdict": "MERGE", "findings": [],
                     "needs_owner": [_ref(1, "Does the route join UNCAPTURED_ROUTES?")]}

    outcome = merge.run_merge(world.context())

    # MUTATION: ignore `needs_owner` (the #2070 bug) and this merges.
    assert outcome.state is TaskState.PARKED, outcome.message
    assert outcome.exit_code == ExitCode.PARKED
    assert world.merge_calls() == []
    # MUTATION: park on CI_PENDING instead and `owner_wait` is None.
    assert outcome.owner_wait is not None
    assert outcome.ci_wait is None
    # MUTATION: drop the step or the index from the record and this fails.
    assert outcome.owner_wait["questions"] == [
        {"task_id": IMPLEMENT, "step_id": "implement", "question_index": 1,
         "question": "Does the route join UNCAPTURED_ROUTES?"}]
    assert "implement" in outcome.message and "question 1" in outcome.message
    # The hold is credential-free: no token was read to decide it.
    assert world.token_reads == 0


def test_an_answer_recorded_after_the_review_lets_the_merge_proceed(tmp_path):
    world = MergeWorld(tmp_path)
    _implement(world)
    world.verdict = {"verdict": "MERGE", "findings": [],
                     "needs_owner": [_ref(1, "Does the route join UNCAPTURED_ROUTES?")]}
    assert merge.run_merge(world.context()).state is TaskState.PARKED

    # The owner answers on the implement task (`swarm_task_answer`).
    world.docs[IMPLEMENT]["metadata"]["owner_answers"] = [
        {"question_index": 1, "choice": "yes", "text": None}]
    outcome = merge.run_merge(world.context())

    # MUTATION: trust the verdict's list without re-reading the answers and this parks.
    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert len(world.merge_calls()) == 1


def test_an_answer_to_another_index_does_not_release_the_hold(tmp_path):
    world = MergeWorld(tmp_path)
    _implement(world, answers=[{"question_index": 0, "choice": "per task", "text": None}])
    world.verdict = {"verdict": "MERGE", "findings": [],
                     "needs_owner": [_ref(1, "Does the route join UNCAPTURED_ROUTES?")]}

    outcome = merge.run_merge(world.context())

    # MUTATION: count any answer on the task as answering every question and this merges.
    assert outcome.state is TaskState.PARKED
    assert world.merge_calls() == []


def test_a_needs_owner_entry_of_another_workflow_or_unreadable_still_holds(tmp_path):
    world = MergeWorld(tmp_path)
    world.docs["task_elsewhere"] = {"tenant_id": "eng", "workflow_id": "wf_other",
                                    "metadata": {"owner_answers": [
                                        {"question_index": 0, "choice": "yes"}]}}
    world.verdict = {"verdict": "MERGE", "findings": [], "needs_owner": [
        {"task_id": "task_elsewhere", "step_id": "x", "question_index": 0},
        "not an entry at all",
    ]}

    outcome = merge.run_merge(world.context())

    # MUTATION: read another workflow's answer as this one's, or skip an entry the
    # merge cannot read, and this merges.
    assert outcome.state is TaskState.PARKED, outcome.message
    assert world.merge_calls() == []
    assert outcome.owner_wait["unreadable"] == 1


def test_open_non_blocking_questions_merge_and_are_listed_on_the_pull_request(tmp_path):
    world = MergeWorld(tmp_path)
    _implement(world)
    world.verdict = {"verdict": "MERGE", "findings": [],
                     "needs_owner": [],
                     "open_questions": [_ref(0, "Per task or per workflow?")]}

    outcome = merge.run_merge(world.context())

    # MUTATION: hold on every open question (option (a)) and this parks.
    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert len(world.merge_calls()) == 1
    # MUTATION: drop the listing and no comment names the question.
    listed = [body for body in _comments(world) if "Per task or per workflow?" in body]
    assert listed, _comments(world)
    assert "implement" in listed[0]
    assert outcome.summary["open_questions"][0]["question_index"] == 0


def test_the_listing_quotes_the_asking_task_not_the_reviews_paraphrase(tmp_path):
    world = MergeWorld(tmp_path)
    asked = [{"question": "Per task or per workflow?",
              "options": [{"label": "per task", "description": "one each"},
                          {"label": "per workflow", "description": "one shared"}],
              "recommended": "per task", "context": "the API counts both"}]
    key = f"tenants/{TENANT}/tasks/{IMPLEMENT}/attempts/att_i/artifacts/questions.json"
    data = json.dumps(asked).encode("utf-8")
    world.store.upload_bytes(key, data)
    world.docs[IMPLEMENT] = {
        "tenant_id": TENANT, "workflow_id": WORKFLOW, "step_id": "implement",
        "state": TaskState.SUCCEEDED.value,
        "result_summary": {"questions": 1, "artifacts": [
            {"name": "questions.json", "uri": world.store.uri(key), "bytes": len(data)}]},
        "metadata": {"owner_answers": []},
    }
    world.verdict = {"verdict": "MERGE", "findings": [],
                     "open_questions": [_ref(0, "Shall we count once?")]}

    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    # MUTATION: render the review's copy of the question and the paraphrase is listed.
    assert any("Per task or per workflow?" in body for body in _comments(world)), \
        _comments(world)
    assert not any("Shall we count once?" in body for body in _comments(world))


def test_an_open_question_answered_before_the_merge_is_not_listed(tmp_path):
    world = MergeWorld(tmp_path)
    _implement(world, answers=[{"question_index": 0, "choice": "per task", "text": None}])
    world.verdict = {"verdict": "MERGE", "findings": [],
                     "open_questions": [_ref(0, "Per task or per workflow?")]}

    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    # MUTATION: list without re-reading the answers and this names the question.
    assert not any("Per task or per workflow?" in body for body in _comments(world))
    assert "open_questions" not in outcome.summary


def test_a_verdict_with_no_questions_merges_exactly_as_today(tmp_path):
    world = MergeWorld(tmp_path)
    outcome = merge.run_merge(world.context())

    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert outcome.owner_wait is None
    assert len(world.merge_calls()) == 1
    # MUTATION: post an empty questions section on every merge and this is two comments.
    assert len(_comments(world)) == 1
    assert "open_questions" not in outcome.summary


# -- the park itself, through the production worker over in-memory Firestore --


def test_the_owner_wait_parks_human_required_releases_the_lease_and_names_the_question(
    db, worker_factory, monkeypatch
):
    from agent_worker import lifecycle
    from fakes import ExplodingChildProcess

    monkeypatch.setattr(lifecycle, "ChildProcess", ExplodingChildProcess)
    seed_attempt(db, runner_profile="merge", task_input={"prompt": "merge"})
    doc = db.doc("tasks/task_1")
    doc["workflow_id"] = "wf_1"
    doc["metadata"] = {"dispatch": {"strategy": "integrate", "carrier": "checkpoints",
                                    "merge_target": {"pull_request": OPENER}}}
    held = [{"task_id": IMPLEMENT, "step_id": "implement", "question_index": 1,
             "question": "Does the route join UNCAPTURED_ROUTES?"}]

    def run(ctx):
        return merge._Run(ctx, {"action": "merge", "merged_by_this_task": False}).wait_for_owner(
            held, unreadable=0)

    monkeypatch.setattr(merge, "run_merge", run)
    worker, _, _ = worker_factory(runner_profile="merge")

    assert worker.run() == ExitCode.PARKED

    task = db.doc("tasks/task_1")
    # MUTATION: end the action FAILED, or park CI_PENDING, and this fails.
    assert task["state"] == TaskState.PARKED.value
    assert task["park_reason"] == ParkReason.HUMAN_REQUIRED.value
    assert task["current_lease_id"] is None
    assert db.doc("leases/lease_1")["released_at"] is not None
    (blocked,) = task["blocked_by"]
    # MUTATION: drop the questions from the park record and nobody can see what it waits on.
    assert blocked["owner_questions"]["questions"] == held
    parked = [e for e in db.events("task_1") if e["type"] == EventType.PARKED.value]
    assert parked and parked[-1]["detail"]["reason"] == ParkReason.HUMAN_REQUIRED.value


# -- a NOT_YET verdict that names needs_owner holds too (#2764) --


def test_a_not_yet_verdict_whose_needs_owner_is_unanswered_parks_for_the_owner(tmp_path):
    world = MergeWorld(tmp_path)
    _implement(world)
    world.verdict = {"verdict": "NOT_YET", "findings": ["waits on the owner"],
                     "needs_owner": [_ref(1, "Does the route join UNCAPTURED_ROUTES?")]}

    outcome = merge.run_merge(world.context())

    # MUTATION: refuse every non-MERGE verdict first (the #2764 bug) and this is
    # FAILED verdict_not_merge, a lane dead on an answer the owner has not given.
    assert outcome.state is TaskState.PARKED, outcome.message
    assert outcome.exit_code == ExitCode.PARKED
    assert "refusal" not in outcome.summary
    assert world.merge_calls() == []
    assert outcome.owner_wait is not None and outcome.ci_wait is None
    # The same hold as a MERGE verdict's: swarm-api's answer route reads it.
    assert outcome.owner_wait["questions"] == [
        {"task_id": IMPLEMENT, "step_id": "implement", "question_index": 1,
         "question": "Does the route join UNCAPTURED_ROUTES?"}]
    assert "implement" in outcome.message and "question 1" in outcome.message
    assert world.token_reads == 0


def test_a_not_yet_verdict_with_an_unreadable_needs_owner_entry_holds(tmp_path):
    world = MergeWorld(tmp_path)
    world.verdict = {"verdict": "NOT_YET", "findings": ["waits on the owner"],
                     "needs_owner": ["not an entry at all"]}

    outcome = merge.run_merge(world.context())

    # MUTATION: hold a NOT_YET only for readable entries and this refuses.
    assert outcome.state is TaskState.PARKED, outcome.message
    assert outcome.owner_wait["unreadable"] == 1
    assert world.merge_calls() == []


def test_a_not_yet_verdict_answered_with_the_built_pick_still_refuses_verdict_not_merge(
        tmp_path):
    world = MergeWorld(tmp_path)
    _implement(world, answers=[{"question_index": 1, "choice": "yes", "text": None}])
    world.verdict = {"verdict": "NOT_YET", "findings": ["waits on the owner"],
                     "needs_owner": [_ref(1, "Does the route join UNCAPTURED_ROUTES?")]}

    outcome = merge.run_merge(world.context())

    # MUTATION: treat an answered NOT_YET as a MERGE and this merges a head no
    # review passed.
    assert outcome.state is TaskState.FAILED, outcome.message
    assert outcome.summary["refusal"]["code"] == "verdict_not_merge"
    assert outcome.owner_wait is None
    assert world.merge_calls() == []
    message = outcome.summary["refusal"]["message"]
    assert "NOT_YET" in message and "1 finding(s)" in message
    assert "answered" in message and "re-review" in message


def test_a_not_yet_hold_whose_pull_request_merged_outside_ends(tmp_path):
    world = MergeWorld(tmp_path)
    _implement(world)
    world.verdict = {"verdict": "NOT_YET", "findings": ["waits on the owner"],
                     "needs_owner": [_ref(1, "Does the route join UNCAPTURED_ROUTES?")]}
    hold: dict[str, Any] = {"parked_at": "2026-10-26T01:52:00+00:00", "pull_request": 41,
                            "wake_requested_at": "2026-10-26T07:26:00+00:00",
                            "wake_reason": merge.PULL_REQUEST_MERGED}
    world.docs[TASK]["metadata"] = {"merge_wait": {
        "wake_requested_at": hold["wake_requested_at"], "wake_reason": hold["wake_reason"],
        merge.OWNER_HOLD_KEY: hold}}
    world.pr.update(state="closed", merged=True, merge_commit_sha=MERGED,
                    merged_by={"login": "bogdan-alexandrescu"},
                    merged_at="2026-10-26T07:25:00Z")

    outcome = merge.run_merge(world.context())

    # MUTATION: refuse a NOT_YET before `_ended_outside` and this is FAILED.
    assert outcome.state is TaskState.SUCCEEDED, outcome.message
    assert outcome.summary["merged_outside_workflow"] is True
    assert world.merge_calls() == []
