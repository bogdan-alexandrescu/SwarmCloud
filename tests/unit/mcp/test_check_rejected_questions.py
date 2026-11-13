"""`swarm_check` and a row name a questions.json the platform could not take (#3086).

MEASURED, 2026-11-02/03: two integrate lanes' merge steps parked on
HUMAN_REQUIRED with `owner_questions: {code: owner_questions_open,
unreadable: 1, questions: []}`. `swarm_check` said only that the merge waited
on a person; nothing said WHICH file could not be read, from which step, or
why -- and the build step's own result said it asked no questions, because
the worker had counted its rejected `questions.json` as 0.

What these tests hold, with no credentials and no network:

* a merge parked on the owner-question hold whose park record carries
  `rejected_files` ({task_id, step_id, file, reason} each) is flagged by
  `swarm_check` with each file, its place and its reason (clipped), in the
  evidence and in the flag's sentence -- read from the park's `blocked_by`
  record, else from the API's `waiting.detail`;
* a hold without any rejected file carries no `rejected_files` key;
* a row that follows that merge ends with the same files in
  `waiting_on_person`;
* a task the worker counted 0 questions for because it rejected the file
  (`questions_rejected`) reads `[]` with `questions_unavailable_because`
  naming the file and the worker's reason -- never "asked none" -- and a
  row's result for it keeps that reason; a task that asked none reads
  nothing and says nothing.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from test_sc_rows_person_wait import Fake

from swarm_mcp import check, compact, progress

NOW = datetime(2026, 11, 3, 9, 0, tzinfo=timezone.utc)
PARKED_AT = NOW - timedelta(hours=8)
MERGE = "task_m3086"
BUILD = "task_b3086"
#: The worker's reason, longer than any clip, so a test sees it cut.
REASON = ("question 0 carries a key the validator does not know: 'touches' -- "
          + "the whole file was refused " * 20)


def _iso(at: datetime) -> str:
    return at.isoformat().replace("+00:00", "Z")


def _rejected() -> list[dict[str, Any]]:
    return [{"task_id": BUILD, "step_id": "implement", "file": "questions.json", "reason": REASON}]


def _held_merge(*, rejected: list[dict[str, Any]] | None, in_detail: bool = False) -> dict[str, Any]:
    """The merge step of the issue: PARKED HUMAN_REQUIRED on the owner-question
    hold, one entry it cannot read, none it can."""
    record: dict[str, Any] = {"code": "owner_questions_open", "unreadable": 1, "questions": []}
    if rejected is not None and not in_detail:
        record["rejected_files"] = rejected
    task: dict[str, Any] = {
        "id": MERGE, "task_id": MERGE, "workflow_id": "wf_3086", "step_id": "merge",
        "state": "PARKED", "park_reason": "HUMAN_REQUIRED",
        "created_at": _iso(PARKED_AT - timedelta(hours=1)), "updated_at": _iso(PARKED_AT),
        "runner_profile": "merge", "metadata": {},
        "blocked_by": [{"reason": "HUMAN_REQUIRED", "park_phase": "owner_questions",
                        "owner_questions": record}],
    }
    if in_detail:
        task["waiting"] = {"reason": "HUMAN_REQUIRED",
                           "summary": "the merge waits on 1 needs_owner entry it cannot read",
                           "detail": {"park_phase": "owner_questions", "questions": [],
                                      "rejected_files": rejected}}
    return task


class _Reads:
    def events(self, task_id: str) -> list[dict[str, Any]]:  # noqa: ARG002
        return [{"type": "parked", "at": _iso(PARKED_AT), "detail": {"reason": "HUMAN_REQUIRED"}}]

    def task(self, task_id: str) -> None:  # noqa: ARG002
        return None

    def capacity(self) -> tuple[None, str]:
        return None, "not read in this test"


def _flag(task: dict[str, Any]) -> dict[str, Any]:
    flag = check.classify_task(task, _Reads(), now=NOW, window=20)
    assert flag is not None and flag["kind"] == check.STUCK and flag["rule"] == "parked", flag
    return flag


def test_check_names_each_rejected_file_with_its_place_and_reason():
    flag = _flag(_held_merge(rejected=_rejected()))

    # MUTATION: no `rejected_files` in the evidence -- the issue's bare
    # "unreadable: 1", which says nothing a person can act on.
    (entry,) = flag["evidence"]["rejected_files"]
    assert {k: entry[k] for k in ("task_id", "step_id", "file")} == {
        "task_id": BUILD, "step_id": "implement", "file": "questions.json"}
    # MUTATION: the reason whole -- a row of the check is a line, not the file.
    assert entry["reason"].startswith("question 0 carries a key the validator does not know: 'touches'")
    assert len(entry["reason"]) <= progress.REJECTED_REASON_CHARS and entry["reason"].endswith("…")
    # The flag's own sentence names the file, its step and its task.
    assert "questions.json" in flag["reason"] and "implement" in flag["reason"] and BUILD in flag["reason"]
    assert "'touches'" in flag["reason"]


def test_check_reads_the_files_from_the_apis_waiting_detail_too():
    flag = _flag(_held_merge(rejected=_rejected(), in_detail=True))

    # MUTATION: reading the park's `blocked_by` record only.
    assert [e["task_id"] for e in flag["evidence"]["rejected_files"]] == [BUILD]
    assert flag["evidence"]["waiting_summary"].startswith("the merge waits on 1 needs_owner entry")


def test_a_hold_with_no_rejected_file_carries_no_key():
    for rejected in (None, [], [{"task_id": 7, "file": None}], "questions.json"):
        flag = _flag(_held_merge(rejected=rejected))
        # MUTATION: an empty list, or an entry made of nothing, for a hold
        # that named no file.
        assert "rejected_files" not in flag["evidence"], (rejected, flag["evidence"])
        assert "could not take" not in flag["reason"]


def test_a_row_following_the_held_merge_ends_with_the_rejected_files():
    fake = Fake()
    fake.docs[MERGE] = _held_merge(rejected=_rejected())

    got = compact.watch_progress(fake, [MERGE], step_id="merge", wait_seconds=1800,
                                 sleep=fake.sleep, clock=fake.clock, now=fake.wall)

    assert got["stop"] is True and got["stop_reason"] == "waiting_on_person"
    wait = got["result"]["waiting_on_person"]
    assert wait["kind"] == "owner_questions" and wait["questions"] == []
    # MUTATION (compact.person_wait): no `rejected_files` -- "answer the
    # owner questions its hold names" with none named.
    (entry,) = wait["rejected_files"]
    assert entry["task_id"] == BUILD and entry["step_id"] == "implement" and entry["file"] == "questions.json"
    assert len(entry["reason"]) <= progress.REJECTED_REASON_CHARS


class _Client:
    """The artifacts route; every read is recorded."""

    def __init__(self) -> None:
        self.reads: list[str] = []

    def artifact_content(self, task_id: str, name: str, *, limit_bytes: int = 0):  # noqa: ARG002
        self.reads.append(f"{task_id}/{name}")
        return {"status": "ok", "content": json.dumps([]), "truncated": False}


def test_a_rejected_file_is_never_read_as_asked_none():
    client = _Client()
    task = {"task_id": BUILD, "result_summary": {"questions": 0, "questions_rejected": REASON}}

    read = progress.owner_questions(client, task)

    assert read["questions"] == []
    # MUTATION (progress.owner_questions): the count-0 path answering `[]`
    # alone -- the 422 "asked no questions" of the issue, in the bridge.
    why = read["questions_unavailable_because"]
    assert "questions.json" in why and "'touches'" in why, why
    # Nothing to read: the worker uploaded no file it refused.
    assert client.reads == []


def test_a_task_that_asked_none_says_nothing():
    client = _Client()
    for summary in ({"questions": 0}, {}, {"questions": 0, "questions_rejected": ""},
                    {"questions": 0, "questions_rejected": None}):
        # MUTATION: a reason for a task whose agent wrote no file.
        assert progress.owner_questions(client, {"task_id": BUILD, "result_summary": summary}) == {"questions": []}
    assert client.reads == []


class _Finished(Fake):
    """`Fake`, with the answer route a finished task's outcome reads."""

    def answer(self, task_id: str) -> dict[str, Any]:  # noqa: ARG002
        return {"status": "ok", "content": "built it", "source": "result_event", "complete": True}


def test_a_rows_result_for_the_build_step_keeps_the_reason():
    fake = _Finished()
    fake.docs[BUILD] = {
        "task_id": BUILD, "id": BUILD, "workflow_id": "wf_3086", "step_id": "implement", "state": "SUCCEEDED",
        "created_at": _iso(PARKED_AT - timedelta(hours=2)), "updated_at": _iso(PARKED_AT - timedelta(hours=1)),
        "result_summary": {"questions": 0, "questions_rejected": REASON},
    }

    got = compact.watch_progress(fake, [BUILD], step_id="implement", wait_seconds=0,
                                 sleep=fake.sleep, clock=fake.clock, now=fake.wall)

    result = got["result"]
    assert result["questions"] == []
    # MUTATION (compact.step_result): dropping `questions_unavailable_because`
    # on the count-0 path.
    assert "questions.json" in result["questions_unavailable_because"], result
