"""A round added after a round-0 `review_unreadable` starts a re-review (issue 338).

docs/design/fix-loop.md §4.5: after `review_unreadable` the added round is a
re-review of the same head. Before issue 338 a park at round 0 recorded no
head, the added round stored `head: None`, and every drive failed
`resume_head_missing` while the route answered 200 with only `failed: 1`.
What is held here:

  1. a round-0 park carries the root's pushed head, and add-round starts
     `re-review:1` on it (`submitted: 1`, `failed: 0`), under `integrate` and
     under `direct-pr`; that re-review's MERGE ends the loop SUCCEEDED;
  2. a loop already stuck with a head-less resume and no rounds starts
     `re-review:1` from the root's pushed head on its next drive;
  3. with no head recorded anywhere, add-round is a 409 that says why and
     writes nothing;
  4. a failure the drive records reaches the reply's `report` as `failures`,
     task id and code;
  5. a drive that raises is served as `drive_failed` with `error`, the
     exception's class and redacted text, by add-round (not a 500) and by
     the `task_finished` push, where before it was only logged.

No credentials, no network, no emulator.
"""

from __future__ import annotations

import secrets

import pytest

from swarm_api import fixloop
from swarm_common.states import ParkReason

from .conftest import auth_header
from .test_fix_loop_drive import (  # noqa: F401 -- fixtures, used by name
    FIX_HEAD, IMPL_HEAD, MERGE, Loop, _members_hold_grants, clock, direct_spec, finish,
    github, integrate_spec, integrate_to_first_round, loop_client, loop_enabled,
    park_running, push, pushed, round_task,
)


def _rounds(client, loop: Loop, user: str = "alice", **body):
    return client.post(f"/v1/workflows/{loop.workflow_id}/fix-loop/rounds",
                       headers=auth_header(user), json=body)


def unreadable_at_round_zero(client, db, objects) -> Loop:
    """integrate: the first review's verdict is not JSON, the fix pushed FIX_HEAD."""
    loop = integrate_to_first_round(client, db, objects, review="not json")
    assert fixloop.park_phase_of(loop.doc()) == "review_unreadable"
    assert loop.round_steps() == {}
    return loop


def test_a_round_zero_park_carries_the_head_and_the_added_round_re_reviews_it(
        loop_client, db, objects):
    loop = unreadable_at_round_zero(loop_client, db, objects)
    assert fixloop.park_detail_of(loop.doc())["head"] == FIX_HEAD

    response = _rounds(loop_client, loop)

    assert response.status_code == 200, response.text
    report = response.json()["fix_loop"]["report"]
    assert report["submitted"] == 1 and report["failed"] == 0, report
    assert report["failures"] == []
    steps = loop.round_steps()
    assert list(steps) == ["re-review:1"], steps
    again = loop.doc(task_id=steps["re-review:1"]["task_id"])
    assert again["repository_ref"] == FIX_HEAD
    assert again["metadata"]["dispatch"]["fix_loop_round"]["head"] == FIX_HEAD
    doc = loop.doc()
    assert doc["state"] == "PARKED"
    assert doc["park_reason"] == ParkReason.FIX_ROUNDS_RUNNING.value


def test_the_added_re_reviews_merge_ends_the_loop(loop_client, db, objects):
    loop = unreadable_at_round_zero(loop_client, db, objects)
    assert _rounds(loop_client, loop).status_code == 200
    rereview = round_task(loop, "re-review:1")
    finish(db, objects, rereview, verdict=MERGE)

    push(loop_client, rereview)

    doc = loop.doc()
    assert doc["state"] == "SUCCEEDED", fixloop.park_detail_of(doc)
    assert doc["result_summary"]["fix_loop"]["verdict"] == "MERGE"


def test_under_direct_pr_the_added_round_re_reviews_the_implementers_head(
        loop_client, db, objects):
    loop = Loop(loop_client, db, direct_spec())
    finish(db, objects, loop.tasks["implement"],
           git=pushed(loop.tasks["implement"], IMPL_HEAD))
    finish(db, objects, loop.tasks["review"], verdict="not json")
    park_running(loop)
    push(loop_client, loop.tasks["review"])
    assert fixloop.park_phase_of(loop.doc()) == "review_unreadable"
    assert fixloop.park_detail_of(loop.doc())["head"] == IMPL_HEAD

    response = _rounds(loop_client, loop)

    assert response.status_code == 200, response.text
    report = response.json()["fix_loop"]["report"]
    assert report["submitted"] == 1 and report["failed"] == 0, report
    steps = loop.round_steps()
    assert list(steps) == ["re-review:1"], steps
    assert loop.doc(task_id=steps["re-review:1"]["task_id"])["repository_ref"] == IMPL_HEAD


def test_a_loop_stuck_with_a_head_less_resume_starts_its_re_review(loop_client, db, objects):
    # What the two live loops hold: an added round whose resume has no head.
    loop = unreadable_at_round_zero(loop_client, db, objects)
    doc = loop.doc()
    state = doc["metadata"].setdefault(fixloop.STATE_KEY, {})
    state["resume"] = {"kind": "review", "head": None, "findings": [], "seen": 0}
    state["added"] = [{"by": "alice@example.com", "rounds": 1}]
    doc.update(state="PARKED", park_reason=ParkReason.FIX_ROUNDS_RUNNING.value,
               blocked_by=[], next_eligible_at=None)

    body = push(loop_client, loop.tasks["fix"])

    steps = loop.round_steps()
    assert list(steps) == ["re-review:1"], (body, fixloop.park_detail_of(loop.doc()))
    assert loop.doc(task_id=steps["re-review:1"]["task_id"])["repository_ref"] == FIX_HEAD
    assert "resume_head_missing" not in str(body)


def test_with_no_head_recorded_add_round_is_a_409_and_writes_nothing(loop_client, db, objects):
    loop = Loop(loop_client, db, integrate_spec())
    finish(db, objects, loop.tasks["implement"])
    finish(db, objects, loop.tasks["review"], verdict="not json")
    finish(db, objects, loop.tasks["fix"])
    park_running(loop)
    push(loop_client, loop.tasks["fix"])
    assert fixloop.park_phase_of(loop.doc()) == "review_unreadable"
    assert "head" not in fixloop.park_detail_of(loop.doc())
    before = loop.doc()
    phase, reason = fixloop.park_phase_of(before), before["park_reason"]
    state = dict(before["metadata"].get(fixloop.STATE_KEY) or {})

    response = _rounds(loop_client, loop)

    assert response.status_code == 409, response.text
    body = response.json()
    assert body["code"] == "conflict"
    assert "no head" in body["message"]
    assert body["detail"] == {"reason": "no_head", "park_phase": "review_unreadable"}
    after = loop.doc()
    assert after["state"] == "PARKED" and after["park_reason"] == reason
    assert fixloop.park_phase_of(after) == phase
    after_state = after["metadata"].get(fixloop.STATE_KEY) or {}
    assert after_state.get("added") == state.get("added")
    assert "resume" not in after_state
    assert loop.round_steps() == {}


def test_a_failure_the_drive_records_is_in_the_reply(loop_client, db, objects):
    loop = integrate_to_first_round(loop_client, db, objects)
    rereview = round_task(loop, "re-review:1")
    finish(db, objects, rereview, verdict="not json")
    push(loop_client, rereview)
    assert fixloop.park_phase_of(loop.doc()) == "review_unreadable"
    # The review the loop's signed block names is gone: the drive cannot read it.
    del db.docs[f"tasks/{loop.tasks['review']}"]

    response = _rounds(loop_client, loop)

    assert response.status_code == 200, response.text
    report = response.json()["fix_loop"]["report"]
    assert report["failed"] == 1
    assert report["failures"] == [{"task_id": loop.loop, "code": "review_missing"}]


def _raising_drive(monkeypatch) -> str:
    """Every drive raises with a token-shaped value in its text; the value."""
    leaked = "ghp_" + secrets.token_hex(18)

    def explode(*_args, **_kwargs):
        raise RuntimeError(f"upstream refused with {leaked}")

    monkeypatch.setattr(fixloop, "_drive_doc", explode)
    return leaked


def _drive_failed(failures, loop: Loop, leaked: str) -> None:
    assert len(failures) == 1, failures
    failure = failures[0]
    assert failure["task_id"] == loop.loop and failure["code"] == "drive_failed"
    assert failure["error"].startswith("RuntimeError: upstream refused with ")
    assert leaked not in failure["error"]


def test_a_drive_that_raises_serves_its_redacted_text_in_the_add_round_reply(
        loop_client, db, objects, monkeypatch: pytest.MonkeyPatch):
    loop = unreadable_at_round_zero(loop_client, db, objects)
    leaked = _raising_drive(monkeypatch)

    response = _rounds(loop_client, loop)

    assert response.status_code == 200, response.text
    assert leaked not in response.text
    report = response.json()["fix_loop"]["report"]
    assert report["failed"] == 1
    _drive_failed(report["failures"], loop, leaked)


def test_a_drive_that_raises_serves_its_redacted_text_on_the_push(
        loop_client, db, objects, monkeypatch: pytest.MonkeyPatch):
    loop = integrate_to_first_round(loop_client, db, objects)
    rereview = round_task(loop, "re-review:1")
    finish(db, objects, rereview, verdict="not json")
    leaked = _raising_drive(monkeypatch)

    body = push(loop_client, rereview)

    assert leaked not in str(body)
    assert body["fix_loop"]["failed"] == 1
    _drive_failed(body["fix_loop"]["failures"], loop, leaked)
