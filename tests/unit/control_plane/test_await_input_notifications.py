"""Await-input steps in the bell and the inbox (docs/design/human-steps.md §4.5, lane HW-NOTIFY).

A workflow step of the `await-input` profile, PARKED(AWAITING_INPUT), waits
for any member of its tenant to answer (Q2). Two surfaces show it, both
DERIVED on read from the task and its `metadata.dispatch.await_input`, so
neither stores a copy and neither can outlive the wait:

* the bell (`notifications.awaiting_input`): one "needs you" item per waiting
  step, for a reader in the step's tenant whose address is on its `remind`
  list, and nobody else. Its id is `task:input:<task>:<k>`, `k` the number of
  whole `remind_after_seconds` periods since the park, so each reminder is a
  new, unread item (Q7), and reading the feed writes nothing;
* the approvals inbox (`approvals.inbox`): `input:<task>` for every member of
  the tenant, kind `input`. The inbox never decides it -- the answer route is
  the step's one writer -- so approve and reject on that id are 404.

Once the step is answered (it leaves PARKED) both are gone.

The people (conftest): `alice` and `root` are in eng, `bob` is in research.
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from swarm_api import approvals, notifications as notif
from swarm_api.store import Store

from .conftest import auth_header, seed_task, seed_tenant

ALICE = "alice@saga.xyz"
BOB = "bob@saga.xyz"
ROOT = "root@saga.xyz"
REMIND = 14_400


def _now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _tenants(db):
    seed_tenant(db, "eng")
    seed_tenant(db, "research")


def _await_step(db, task_id: str, *, tenant_id: str = "eng", by: str = ALICE,
                remind: list[str] | None = None, remind_after: int | None = REMIND,
                opened: datetime | None = None, state: str = "PARKED",
                park_reason: str | None = "AWAITING_INPUT", form: str = "verdict",
                answer: dict[str, Any] | None = None) -> dict[str, Any]:
    opened = opened or _now() - timedelta(minutes=5)
    doc = seed_task(db, task_id=task_id, tenant_id=tenant_id, state=state,
                    runner_profile="await-input", park_reason=park_reason,
                    next_eligible_at=opened, workflow_id="wf_review",
                    created_at=opened - timedelta(minutes=30))
    doc["submitted_by"] = by
    doc["step_id"] = "review"
    doc["input"] = {"prompt": "Read draft.md. Approve it, or list what must change."}
    doc["metadata"] = {
        "dispatch": {"await_input": {
            "form": form, "artifact": "review.json", "max_files": 10,
            "expires_after_seconds": 86_400, "on_expire": "fail", "default": None,
            "remind_after_seconds": remind_after,
            "remind": [by] if remind is None else remind,
        }},
        "await_input": {"opened_at": opened, "expires_at": opened + timedelta(seconds=86_400),
                        "answer": answer},
    }
    return doc


def _bell(db, email: str, tenant_id: str = "eng", *, now: datetime | None = None) -> list[str]:
    viewer = notif.Viewer(email=email, tenant_id=tenant_id)
    reading = notif.Reading(db=db, store=Store(db), viewer=viewer, now=now or _now())
    return [i["id"] for i in notif.awaiting_input(reading)]


def _inbox(client, user: str = "alice", **params: str) -> list[dict[str, Any]]:
    query = "&".join(f"{k}={v}" for k, v in params.items())
    response = client.get("/v1/approvals" + (f"?{query}" if query else ""), headers=auth_header(user))
    assert response.status_code == 200, response.text
    return response.json()["approvals"]


# --------------------------------------------------------------------------
# the bell: `remind`, in the step's tenant only
# --------------------------------------------------------------------------

def test_a_reader_on_remind_gets_the_item_and_another_member_does_not(db):
    _await_step(db, "t_review", by=ALICE, remind=[ALICE])

    assert _bell(db, ALICE) == ["task:input:t_review:0"]
    # root is a member of eng and could answer (Q2), but is not on `remind`.
    assert _bell(db, ROOT) == []


def test_remind_names_whom_to_ring_not_only_the_submitter(db):
    _await_step(db, "t_review", by=ALICE, remind=[ROOT.upper()])

    assert _bell(db, ROOT) == ["task:input:t_review:0"]
    assert _bell(db, ALICE) == []


def test_a_reader_in_another_tenant_does_not_get_the_item(db):
    # bob is named on the step's `remind`, but the step is eng's and bob reads
    # research: the bell is read per tenant, so naming him rings nobody.
    _await_step(db, "t_review", tenant_id="eng", remind=[ALICE, BOB])

    assert _bell(db, BOB, "research") == []
    # The same person acting in another tenant does not see it either.
    assert _bell(db, ALICE, "research") == []
    assert _bell(db, ALICE, "eng") == ["task:input:t_review:0"]


def test_the_item_id_advances_once_per_remind_after_seconds(db):
    opened = _now() - timedelta(days=1)
    _await_step(db, "t_review", opened=opened, remind_after=REMIND)

    def at(seconds: float) -> list[str]:
        return _bell(db, ALICE, now=opened + timedelta(seconds=seconds))

    assert at(0) == ["task:input:t_review:0"]
    assert at(REMIND - 1) == ["task:input:t_review:0"]
    assert at(REMIND) == ["task:input:t_review:1"]
    assert at(2 * REMIND + REMIND / 2) == ["task:input:t_review:2"]
    assert at(5 * REMIND) == ["task:input:t_review:5"]


def test_with_no_remind_after_seconds_the_id_never_advances(db):
    opened = _now() - timedelta(days=1)
    _await_step(db, "t_review", opened=opened, remind_after=None)

    assert _bell(db, ALICE, now=opened + timedelta(hours=23)) == ["task:input:t_review:0"]


def test_reading_the_bell_writes_nothing_to_firestore(client, db):
    _await_step(db, "t_review", opened=_now() - timedelta(seconds=REMIND + 60))
    before = copy.deepcopy(db.docs)

    response = client.get("/v1/notifications", headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    items = [i for i in response.json()["items"] if i["id"].startswith("task:input:")]
    assert [i["id"] for i in items] == ["task:input:t_review:1"]
    assert items[0]["needs_you"] is True and items[0]["group"] == notif.TASK
    assert items[0]["read"] is False
    assert db.docs == before


def test_the_item_says_what_it_waits_for_and_links_to_the_workflow(db):
    _await_step(db, "t_review", form="verdict")
    viewer = notif.Viewer(email=ALICE, tenant_id="eng")
    (item,) = notif.awaiting_input(notif.Reading(db=db, store=Store(db), viewer=viewer, now=_now()))

    assert item["title"] == "A workflow is waiting for a review"
    assert item["href"] == "/workflows/wf_review"
    assert "review" in item["detail"] and "wf_review" in item["detail"]


def test_the_bell_derives_nothing_once_answered_expired_or_parked_otherwise(db):
    _await_step(db, "t_answered", state="SUCCEEDED", park_reason=None)
    _await_step(db, "t_recorded", answer={"answer_id": "ans_1", "answered_by": ROOT})
    _await_step(db, "t_expired", opened=_now() - timedelta(hours=25))
    _await_step(db, "t_human", park_reason="HUMAN_REQUIRED")
    cancelled = _await_step(db, "t_cancelled")
    cancelled["cancel_requested"] = True

    assert _bell(db, ALICE) == []


def test_the_source_is_in_the_feed(client, db):
    assert ("awaiting_input", notif.awaiting_input) in notif.SOURCES


# --------------------------------------------------------------------------
# the inbox: `input:<task>` for every member, never decided there
# --------------------------------------------------------------------------

def test_the_inbox_projects_the_step_for_every_member_of_its_tenant(client, db):
    _await_step(db, "t_review", by=ALICE, remind=[ALICE])

    for user in ("alice", "root"):
        (item,) = _inbox(client, user)
        assert item["approval_id"] == "input:t_review"
        assert item["kind"] == approvals.INPUT
        assert item["tenant_id"] == "eng"
        assert item["state"] == approvals.PENDING
        assert item["projected"] is True
        assert item["subject"]["task_id"] == "t_review"
        assert item["subject"]["workflow_id"] == "wf_review"
        assert item["subject"]["form"] == "verdict"
    assert _inbox(client, "bob") == []
    assert [i["approval_id"] for i in _inbox(client, "alice", kind="input")] == ["input:t_review"]
    assert _inbox(client, "alice", kind="run") == []


def test_another_tenants_step_is_not_in_the_inbox(client, db):
    _await_step(db, "t_research", tenant_id="research", by=BOB)

    assert _inbox(client, "alice") == []
    assert [i["approval_id"] for i in _inbox(client, "bob")] == ["input:t_research"]


def test_the_inbox_has_no_decide_path_for_an_input_item(client, db):
    _await_step(db, "t_review")
    before = copy.deepcopy(db.docs)

    for verb, body in (("approve", {"digest": "MERGE"}), ("reject", {"reason": "no"})):
        response = client.post(f"/v1/approvals/input:t_review:{verb}",
                               headers=auth_header("alice"), json=body)
        assert response.status_code == 404, (verb, response.text)
    assert client.get("/v1/approvals/input:t_review",
                      headers=auth_header("alice")).status_code == 404
    assert db.docs == before
    assert db.docs["tasks/t_review"]["state"] == "PARKED"


def test_no_approval_record_of_kind_input_can_be_made(db):
    kind = approvals.INPUT
    assert kind in approvals.KINDS  # `GET /v1/approvals?kind=input` is a valid filter
    with pytest.raises(ValueError, match="projected"):
        approvals.request(db, _now(), tenant_id="eng", kind=kind, key="t_review",
                          subject={"task_id": "t_review"}, digest=None, summary="x",
                          approvers="members", ttl=24)
    assert not any(k.startswith(f"{approvals.COLLECTION}/") for k in db.docs)


def test_the_inbox_item_and_the_bell_item_are_gone_once_answered(client, db):
    doc = _await_step(db, "t_review")
    assert [i["approval_id"] for i in _inbox(client)] == ["input:t_review"]
    assert _bell(db, ALICE) == ["task:input:t_review:0"]

    doc["state"] = "SUCCEEDED"
    doc["park_reason"] = None
    doc["completed_at"] = _now()
    doc["metadata"]["await_input"]["answer"] = {"answer_id": "ans_1", "answered_by": ROOT}

    assert _inbox(client) == []
    assert _bell(db, ALICE) == []


def test_a_recorded_answer_or_a_passed_deadline_leaves_the_inbox(client, db):
    _await_step(db, "t_recorded", answer={"answer_id": "ans_1", "answered_by": ROOT})
    _await_step(db, "t_expired", opened=_now() - timedelta(hours=25))
    _await_step(db, "t_human", park_reason="HUMAN_REQUIRED")

    assert _inbox(client) == []
