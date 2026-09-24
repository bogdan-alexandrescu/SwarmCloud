"""The fencing generation and the current lease must reach the API.

CONTRACT invariant 5 is the platform's core safety mechanism: "Every attempt
carries a fencing generation; a stale worker exits WITHOUT running the agent and
without touching the lease." `Task` carries `current_generation` and
`current_lease_id` (models.py:176-177), `task_from_dict` reads both back
(codec.py:87-88) -- and `task_to_api` never emitted either, from the moment the
function was born in 08679f5 through to this file. No commit ever removed them,
and nothing anywhere records a reason to withhold them: the docstring's promise
is "no credential material and no backend spec", and a small integer and a lease
id are neither. The lease id in particular is already public -- `lease_to_api`
serves `lease_id` itself, plus that lease's own generation.

THE LIVE CASE THIS COMES FROM. Workflow wf_bcdc9180e4fb4a209f31, step
`research`, task_b5dc2568713a40158851: state DISPATCHED for twenty minutes, with
`current_generation=2` while `current_lease_id` named lease_723362a25968460aae35
-- a generation-1 lease, never released, never heartbeaten, whose attempt
att_f48ae8482e0a471a9748 never started and whose Cloud Run execution was already
dead. The lease was holding capacity for work that could never run, and the one
number that says so was not served. Every screen showed "dispatched".

HOW IT ENDED, read out of Firestore afterwards: the reconciler did reclaim that
lease (`released_at` 04:05:16Z, `release_reason` "reconciler:missing_execution"),
the retry ran, and the task is SUCCEEDED -- at `current_generation` 3 with
`attempt_count` 2. The generation standing above the attempt count is the
PERMANENT record that a stale worker was fenced on this task, and until this
change no API a person or a screen could call would show either number. The
stall was transient; the unreadable evidence was not.

THE SAME CLASS OF BUG AS test_attempt_spend_round_trip.py, and that file's
discipline is copied here deliberately: assert PRESENT-when-present, not only
absent-when-absent. The five spend fields had a test -- it asserted they were
None on an attempt with no spend, which passes whether the decoder reads them or
not. It was satisfied by the bug. So each field here is asserted present with a
value, independently, and a zero is asserted to survive as a zero.

`attempt_count` is the control: same document, same decoder, same serialiser,
and it round-tripped throughout. If it ever fails, the harness is wrong.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from swarm_api.codec import (
    attempt_from_dict,
    attempt_to_api,
    lease_from_dict,
    lease_to_api,
    task_from_dict,
    task_to_api,
)

from .conftest import auth_header, seed_task, seed_tenant

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)

#: The two keys. Parametrised so a serialiser that adds one of the pair fails.
FENCING = ("current_generation", "current_lease_id")


def _task_doc(**extra):
    base = {
        "id": "task_1",
        "tenant_id": "eng",
        "created_at": NOW,
        "updated_at": NOW,
        "state": "DISPATCHED",
        "runner_profile": "claude-code",
        "resource_class": "standard",
    }
    base.update(extra)
    return base


# --------------------------------------------------------------------------
# 1. The bug, stated directly
# --------------------------------------------------------------------------

def test_the_fencing_generation_and_lease_reach_the_api():
    api = task_to_api(
        task_from_dict(
            _task_doc(current_generation=2, current_lease_id="lease_723362a25968460aae35")
        )
    )
    assert api["current_generation"] == 2
    assert api["current_lease_id"] == "lease_723362a25968460aae35"


@pytest.mark.parametrize("field,value", [
    ("current_generation", 7),
    ("current_lease_id", "lease_abc"),
])
def test_each_fencing_field_independently(field, value):
    """Parametrised so a serialiser that adds one of the two still fails."""
    api = task_to_api(task_from_dict(_task_doc(**{field: value})))
    assert api[field] == value, f"{field} was dropped between Firestore and the API"


def test_attempt_count_is_the_control():
    """If this ever fails the harness is wrong, not the serialiser."""
    api = task_to_api(task_from_dict(_task_doc(attempt_count=3)))
    assert api["attempt_count"] == 3


# --------------------------------------------------------------------------
# 2. Zero and null are answers, and the keys must always be there
# --------------------------------------------------------------------------

@pytest.mark.parametrize("field", FENCING)
def test_the_key_is_present_even_on_a_task_that_never_ran(field):
    """A QUEUED task must still carry both KEYS.

    An absent key and a null value are different to every caller: `types.ts`
    treats a missing `dispatch` key as "an older API than this bundle" and a
    present null as an answer, and the same reading applies here. A serialiser
    that emitted these only when set would make "this task has never been
    admitted" indistinguishable from "this deployment cannot tell you".
    """
    api = task_to_api(task_from_dict(_task_doc(state="QUEUED")))
    assert field in api


def test_generation_zero_survives_as_zero():
    """The trap in the fix itself.

    `task.current_generation or None` would turn generation 0 back into "not
    reported" -- the same null-is-not-zero conflation this codebase has spent
    days removing, reintroduced in the line that fixes it. Zero is an ANSWER:
    it means the task has never been admitted, and it is what makes
    `current_generation > attempt_count` (a stale worker was fenced) readable
    on a task that has not run yet.
    """
    api = task_to_api(task_from_dict(_task_doc(state="QUEUED")))
    assert api["current_generation"] == 0
    assert api["current_generation"] is not None


def test_an_unadmitted_task_has_no_lease_rather_than_an_empty_string():
    """None, not "". `models.py` types it `str | None` and the worker's fencing
    check is `task.get("current_lease_id") not in (None, self.lease_id)`; an
    empty string would be a lease id that matches nothing."""
    api = task_to_api(task_from_dict(_task_doc(state="QUEUED")))
    assert api["current_lease_id"] is None


# --------------------------------------------------------------------------
# 3. The live stall, reconstructed from the API alone
# --------------------------------------------------------------------------
#
# The mismatch is only visible when both halves reach a caller, which is why the
# lease's and the attempt's generations are asserted here too rather than taken
# on trust: they are the other side of the comparison. Both already round-trip,
# so these are regression armour for the number that makes the task's own
# generation mean anything.

def _live_lease_doc():
    return {
        "lease_id": "lease_723362a25968460aae35",
        "task_id": "task_b5dc2568713a40158851",
        "attempt_id": "att_f48ae8482e0a471a9748",
        "tenant_id": "eng",
        "generation": 1,
        "pools": ["global", "tenant:eng", "runner:claude-code"],
        "units": 1,
        "state": "DISPATCHED",
        "created_at": NOW - timedelta(minutes=20),
        "dispatch_deadline": NOW - timedelta(minutes=15),
        "expires_at": NOW + timedelta(minutes=40),
        # Never beaten, never released: this is what "holding capacity for work
        # that will never run" looks like in the document.
        "heartbeat_at": None,
        "released_at": None,
    }


def test_the_twenty_minute_stall_is_diagnosable_from_the_api():
    task = task_to_api(
        task_from_dict(
            _task_doc(
                id="task_b5dc2568713a40158851",
                state="DISPATCHED",
                current_generation=2,
                current_lease_id="lease_723362a25968460aae35",
                attempt_count=1,
            )
        )
    )
    lease = lease_to_api(lease_from_dict(_live_lease_doc()))

    # The join: the task names the lease, so a caller can fetch it.
    assert task["current_lease_id"] == lease["lease_id"]
    # The finding: the task has moved on, the lease has not, and the lease is
    # still holding its slot. Every term of that sentence is now in the JSON.
    assert task["current_generation"] != lease["generation"]
    assert task["current_generation"] == 2
    assert lease["generation"] == 1
    assert lease["released"] is False
    assert lease["heartbeat_at"] is None
    # And the gap against attempt_count is the "a worker was fenced" signal
    # docs/web-ui/03-agents-and-workflows.md 1.6 describes.
    assert task["current_generation"] > task["attempt_count"]


def test_the_fence_is_still_readable_after_the_task_recovered():
    """The shape that outlives the incident, and the real end of this one.

    `control.finish` writes `current_lease_id: None` at terminal state but never
    resets the generation, so the permanent record of a fence is a generation
    standing above the attempt count with NO lease left to join to. That is
    task_b5dc2568713a40158851 as it sits now: SUCCEEDED, generation 3,
    attempt_count 2. A serialiser that only emitted the pair together -- or only
    while a lease was held -- would serve the live minutes and lose the history,
    which is the half anyone reviewing a slow week actually reads.
    """
    api = task_to_api(
        task_from_dict(
            _task_doc(
                id="task_b5dc2568713a40158851",
                state="SUCCEEDED",
                current_generation=3,
                current_lease_id=None,
                attempt_count=2,
                completed_at=NOW,
            )
        )
    )
    assert api["current_generation"] == 3
    assert api["current_lease_id"] is None
    assert api["current_generation"] > api["attempt_count"]


def test_the_attempt_carries_the_generation_it_was_minted_with():
    """The third leg. The attempt is generation 1 like the lease, and it never
    started -- so an operator can see the dead attempt is the one the stale
    lease belongs to, not the current generation's."""
    api = attempt_to_api(
        attempt_from_dict({
            "attempt_id": "att_f48ae8482e0a471a9748",
            "task_id": "task_b5dc2568713a40158851",
            "tenant_id": "eng",
            "generation": 1,
            "lease_id": "lease_723362a25968460aae35",
            "backend": "CLOUD_RUN_JOB",
            "created_at": NOW - timedelta(minutes=20),
            "execution_name": "swarm-job-eng-claude-code-sq2l6",
            "started_at": None,
        })
    )
    assert api["generation"] == 1
    assert api["lease_id"] == "lease_723362a25968460aae35"
    assert api["started_at"] is None


# --------------------------------------------------------------------------
# 4. Through the real routes, which is where a UI reads them
# --------------------------------------------------------------------------

@pytest.mark.parametrize("field", FENCING)
def test_the_task_detail_route_serves_them(client, db, field):
    seed_tenant(db, "eng")
    doc = seed_task(db, task_id="task_live", tenant_id="eng", state="DISPATCHED")
    doc["current_generation"] = 2
    doc["current_lease_id"] = "lease_723362a25968460aae35"

    response = client.get("/v1/tasks/task_live", headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    body = response.json()["task"]
    assert field in body
    assert body["current_generation"] == 2
    assert body["current_lease_id"] == "lease_723362a25968460aae35"


def test_the_task_list_route_serves_them(client, db):
    """The agents table reads the LIST, not the detail.

    Asserted separately because "the detail view has it" is how a column that
    never populates gets signed off: the list is what the table renders, and a
    trimmed serialiser on either route empties the fencing column on its own.
    """
    seed_tenant(db, "eng")
    doc = seed_task(db, task_id="task_live", tenant_id="eng", state="DISPATCHED")
    doc["current_generation"] = 2
    doc["current_lease_id"] = "lease_723362a25968460aae35"

    response = client.get("/v1/tasks", headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    rows = {row["id"]: row for row in response.json()["tasks"]}
    assert rows["task_live"]["current_generation"] == 2
    assert rows["task_live"]["current_lease_id"] == "lease_723362a25968460aae35"


# --------------------------------------------------------------------------
# 5. The UI's typed surface, and the note that told it not to look
# --------------------------------------------------------------------------
#
# Read as text, like test_dispatch_ui_surface.py does, and skipped rather than
# failed when the UI is not checked out: this suite has to pass in a tree that
# holds only the Python services.

UI = Path(__file__).resolve().parents[3] / "apps/swarm-ui/src"


def _task_interface() -> str:
    source = (UI / "types.ts").read_text()
    match = re.search(r"export interface Task \{(.*?)\n\}", source, re.S)
    assert match is not None, "types.ts no longer declares `export interface Task`"
    return match.group(1)


@pytest.mark.skipif(not UI.is_dir(), reason="apps/swarm-ui/src is not present")
@pytest.mark.parametrize("field", FENCING)
def test_the_ui_declares_the_fencing_fields_on_task(field):
    assert re.search(rf"^\s*{field}:", _task_interface(), re.M), (
        f"apps/swarm-ui/src/types.ts does not type `{field}` on Task, so no "
        "screen can render the fencing generation even though the API sends it"
    )


@pytest.mark.skipif(not UI.is_dir(), reason="apps/swarm-ui/src is not present")
def test_the_ui_no_longer_records_the_fields_as_unavailable():
    """A comment that has stopped being true is worse than none.

    types.ts carried a block headed "NOT on the task: `current_generation`",
    ending "task_to_api does not send it, so no screen can show it and no
    screen should imply it". That is the same failure as the spend comment
    blaming a shipped worker fix: it was accurate when written, it stopped
    being accurate here, and while it stands it is the documented reason not
    to build the one indicator that explains a stalled task.
    """
    source = (UI / "types.ts").read_text()
    assert "NOT on the task" not in source, (
        "types.ts still records current_generation as absent from the task API; "
        "task_to_api now sends it and this note is why no screen renders it"
    )
