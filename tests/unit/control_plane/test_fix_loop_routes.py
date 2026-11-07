"""A person resumes or stops a fix loop parked for them (FL-DRIVE, part of #2509).

docs/design/fix-loop.md §4.5 and BGD's Q4 (a) (2026-10-27):

    POST /v1/workflows/{workflow_id}/fix-loop/rounds   {rounds?, note?}
    POST /v1/workflows/{workflow_id}/fix-loop/end      {outcome: "stop"}

What is held here:

  1. any full member of the loop's tenant may add rounds -- one by default --
     and the tenant is the token's, never the body's; a viewer, another
     tenant's member and an unknown workflow are refused and nothing moves;
  2. the add is recorded (who, when, how many, the redacted note) in a
     transaction guarded on the park, and runs the decision table again:
     after `fix_loop_exhausted` the added round is a fix whose prompt carries
     the note fenced as data; after `review_unreadable` it is a re-review of
     the same head;
  3. no add raises the loop past FIX_LOOP_ROUNDS_CEILING = 10; a note over
     4 KiB is refused; a loop that is not parked for a person is a 409;
  4. `end` with `stop` ends the loop FAILED `fix_loop_exhausted`;
  5. `human-ready` refuses a `fix-loop` agent with 409, as a READY
     control-plane agent would only be parked again.

No credentials, no network, no emulator. Every token is built at runtime.
"""

from __future__ import annotations

import re
import secrets

from swarm_api import fixloop
from swarm_common.models import EndCause
from swarm_common.states import ParkReason

from .conftest import auth_header
from .test_fix_loop_drive import (  # noqa: F401 -- fixtures, used by name
    FIX_HEAD, NOT_YET, Loop, _members_hold_grants, clock, finish, github,
    integrate_spec, integrate_to_first_round, loop_client, loop_enabled, push, round_task,
)


def exhausted(client, db, objects) -> Loop:
    """A loop of one round whose re-review said NOT_YET: parked for a person."""
    loop = integrate_to_first_round(client, db, objects, max_rounds=1)
    rereview = round_task(loop, "re-review:1")
    finish(db, objects, rereview, verdict=NOT_YET)
    push(client, rereview)
    assert fixloop.park_phase_of(loop.doc()) == "fix_loop_exhausted"
    return loop


def _rounds(client, loop: Loop, user: str = "alice", **body):
    return client.post(f"/v1/workflows/{loop.workflow_id}/fix-loop/rounds",
                       headers=auth_header(user), json=body)


def _end(client, loop: Loop, user: str = "alice", **body):
    return client.post(f"/v1/workflows/{loop.workflow_id}/fix-loop/end",
                       headers=auth_header(user), json=body or {"outcome": "stop"})


def test_a_member_adds_one_round_and_the_fix_carries_the_note(loop_client, db, objects):
    loop = exhausted(loop_client, db, objects)
    # Built at runtime: a secret-shaped value in the note is redacted.
    planted = "ghp_" + secrets.token_hex(18)
    note = f"the TTL belongs in settings, not a constant. token {planted}"

    response = _rounds(loop_client, loop, user="root", note=note)

    assert response.status_code == 200, response.text
    doc = loop.doc()
    assert doc["state"] == "PARKED"
    assert doc["park_reason"] == ParkReason.FIX_ROUNDS_RUNNING.value
    (added,) = loop.state()["added"]
    assert added["by"] == "root@example.com" and added["rounds"] == 1
    assert planted not in added["note"]
    steps = loop.round_steps()
    assert list(steps) == ["re-review:1", "review-fix:2"], steps
    prompt = loop.doc(task_id=steps["review-fix:2"]["task_id"])["input"]["prompt"]
    assert "the TTL belongs in settings" in prompt and planted not in prompt
    assert "review-fix round 2 of at most 2" in prompt
    fences = re.findall(r"^=== PERSON'S GUIDANCE ([0-9a-f]{16}) ===$", prompt, re.M)
    assert len(fences) == 2 and fences[0] == fences[1]
    assert response.json()["fix_loop"]["max_rounds"] == 2


def test_after_an_unreadable_review_the_added_round_is_a_re_review(loop_client, db, objects):
    loop = integrate_to_first_round(loop_client, db, objects)
    rereview = round_task(loop, "re-review:1")
    finish(db, objects, rereview, verdict="not json")
    push(loop_client, rereview)
    assert fixloop.park_phase_of(loop.doc()) == "review_unreadable"

    response = _rounds(loop_client, loop)

    assert response.status_code == 200, response.text
    # The added round is round 2, a re-review of the head round 1's could not
    # judge: no fix runs on findings nobody could read.
    steps = loop.round_steps()
    assert list(steps) == ["re-review:1", "re-review:2"], steps
    again = loop.doc(task_id=steps["re-review:2"]["task_id"])
    assert again["repository_ref"] == FIX_HEAD


def test_rounds_never_pass_the_ceiling(loop_client, db, objects):
    loop = exhausted(loop_client, db, objects)

    response = _rounds(loop_client, loop, rounds=fixloop.FIX_LOOP_ROUNDS_CEILING + 5)

    assert fixloop.FIX_LOOP_ROUNDS_CEILING == 10
    assert response.status_code == 200, response.text
    assert response.json()["fix_loop"]["max_rounds"] == fixloop.FIX_LOOP_ROUNDS_CEILING


def test_a_loop_at_the_ceiling_is_refused(loop_client, db, objects):
    loop = exhausted(loop_client, db, objects)
    loop.doc()["metadata"][fixloop.STATE_KEY]["added"] = [
        {"by": "alice@example.com", "rounds": fixloop.FIX_LOOP_ROUNDS_CEILING}]

    response = _rounds(loop_client, loop)

    assert response.status_code == 409, response.text
    assert fixloop.park_phase_of(loop.doc()) == "fix_loop_exhausted"


def test_a_note_over_4_kib_is_refused(loop_client, db, objects):
    loop = exhausted(loop_client, db, objects)

    response = _rounds(loop_client, loop, note="x" * (fixloop.NOTE_MAX_BYTES + 1))

    assert response.status_code == 422, response.text
    assert "added" not in loop.state()


def test_a_loop_not_parked_for_a_person_is_a_409(loop_client, db, objects):
    loop = integrate_to_first_round(loop_client, db, objects)

    response = _rounds(loop_client, loop)

    assert response.status_code == 409, response.text
    assert "added" not in loop.state()


def test_another_tenants_member_is_refused(loop_client, db, objects):
    loop = exhausted(loop_client, db, objects)

    response = _rounds(loop_client, loop, user="bob")

    assert response.status_code == 404, response.text
    assert fixloop.park_phase_of(loop.doc()) == "fix_loop_exhausted"


def test_a_workflow_with_no_loop_is_not_found(loop_client, db, objects):
    response = loop_client.post("/v1/workflows", headers=auth_header("alice"), json={
        "strategy": "direct-pr", "repository_url": "https://github.com/example-org/widgets",
        "steps": [{"step_id": "a", "runner_profile": "mock", "input": {"prompt": "p"}}]})
    assert response.status_code == 201, response.text
    workflow_id = response.json()["workflow"]["workflow_id"]

    refused = loop_client.post(f"/v1/workflows/{workflow_id}/fix-loop/rounds",
                               headers=auth_header("alice"), json={})

    assert refused.status_code == 404, refused.text


def test_stop_ends_the_loop_failed_fix_loop_exhausted(loop_client, db, objects):
    loop = exhausted(loop_client, db, objects)

    response = _end(loop_client, loop)

    assert response.status_code == 200, response.text
    doc = loop.doc()
    assert doc["state"] == "FAILED"
    assert doc["end_cause"] == EndCause.FIX_LOOP_EXHAUSTED.value
    assert doc["result_summary"]["fix_loop"]["phase"] == "fix_loop_exhausted"


def test_end_takes_only_stop(loop_client, db, objects):
    loop = exhausted(loop_client, db, objects)

    response = _end(loop_client, loop, outcome="merge")

    assert response.status_code == 422, response.text
    assert loop.doc()["state"] == "PARKED"


def test_end_of_a_loop_with_a_round_running_is_a_409(loop_client, db, objects):
    loop = integrate_to_first_round(loop_client, db, objects)

    response = _end(loop_client, loop)

    assert response.status_code == 409, response.text
    assert loop.doc()["state"] == "PARKED"


def test_human_ready_refuses_a_fix_loop_agent(loop_client, db, objects):
    loop = exhausted(loop_client, db, objects)

    response = loop_client.post(f"/v1/tasks/{loop.loop}/human-ready",
                                headers=auth_header("alice"))

    assert response.status_code == 409, response.text
    assert loop.doc()["state"] == "PARKED"
