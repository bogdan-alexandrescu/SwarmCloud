"""Jev's judgment of a step's work contract, served where the person deciding reads it.

The owner's picks on PR #3255 (jev-contract.md sections 5.4 and 8.2):

  * `GET /v1/approvals/{id}` serves the step's record: a step waiting on its
    owner is projected into the inbox as `contract:<task_id>`, whose detail
    carries `contract_acceptance`; and `GET /v1/tasks/{id}` carries it too.

Each assertion names the MUTATION it catches. Offline: the in-memory
Firestore, no credentials, no emulator.
"""

from __future__ import annotations

import secrets
from datetime import timedelta

import pytest

from swarm_api import approvals
from swarm_api import contractjudge as cj
from swarm_api import contractreask as cr

from .conftest import auth_header, seed_task
from .test_approvals import (  # noqa: F401 -- fixtures, used by name
    _grants,
    api_context,
    client,
    clock,
    group_map,
    pr_files,
    writes,
)
from .test_contract_verdict import COMMIT

HUNK = "@@ -1,2 +1,3 @@\n def form():\n-    return None\n+    return Field('email')"


# --- served on the approval detail and the task detail ---------------------------


def _owner_waiting(db, clock, task_id: str, *, tenant_id: str = "eng", waiting_on: str = cr.WAITING_OWNER,
                   generation: int = 1, current: int = 1) -> dict:
    doc = seed_task(db, task_id=task_id, tenant_id=tenant_id, state="SUCCEEDED",
                    created_at=clock.at - timedelta(hours=2))
    doc.update({"submitted_by": "alice@example.com", "step_id": "implement",
                "input": {"prompt": "Release the lease on cancel."},
                "current_generation": current})
    doc[cr.ACCEPTANCE_FIELD] = {
        "status": cj.JUDGMENT_PENDING, "waiting_on": waiting_on,
        "owner_reason": cr.OWNER_SEND_BACK_CAP, "attempt_id": "att_1", "lease_id": "lease_1",
        "generation": generation, "contract_id": "wc-lease", "revision": 2, "digest": "9d" * 32,
        "commit": COMMIT, "base": None, "outcome": "judged", "reason": None, "call_id": "jc2-1",
        "state_sha256": "e" * 64,
        "verdicts": [{"requirement_id": "REQ-1", "verdict": "unmet", "reason": "partially_done",
                      "evidence_item": "i0", "p": 0.8}],
        "changed": ["apps/x.py"], cr.HUNKS_FIELD: {"i0": HUNK}, "mechanical": [],
        "reask": None, "at": clock.at - timedelta(hours=1),
    }
    return doc


def _inbox(client, user: str = "alice", **params) -> list[dict]:
    query = "&".join(f"{k}={v}" for k, v in params.items())
    response = client.get("/v1/approvals" + (f"?{query}" if query else ""), headers=auth_header(user))
    assert response.status_code == 200, response.text
    return response.json()["approvals"]


def test_a_step_waiting_on_its_owner_is_an_inbox_item_and_no_other_is(client, db, clock):
    _owner_waiting(db, clock, "t_owner")
    _owner_waiting(db, clock, "t_reask", waiting_on=cr.WAITING_REASK)
    _owner_waiting(db, clock, "t_theirs", tenant_id="research")
    _owner_waiting(db, clock, "t_stale", generation=1, current=2)
    items = [i for i in _inbox(client) if i["kind"] == approvals.CONTRACT]
    # MUTATION: not projecting the step; projecting one still re-asked, another
    # tenant's (invariant 9), or one a newer attempt superseded.
    assert [i["approval_id"] for i in items] == ["contract:t_owner"]
    item = items[0]
    assert item["subject"]["task_id"] == "t_owner" and item["subject"]["owner_reason"] == "send_back_cap"
    # MUTATION: naming the platform owner, or nobody, as whose step it is.
    assert item["approvers"] == ["alice@example.com"]
    # Evidence, never a decision here: no verb is offered.
    assert "may_decide" not in item
    assert [i["approval_id"] for i in _inbox(client, kind="contract")] == ["contract:t_owner"]


def test_the_approval_detail_carries_the_steps_contract_acceptance(client, db, clock):
    _owner_waiting(db, clock, "t_owner")
    response = client.get("/v1/approvals/contract:t_owner", headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    out = response.json()
    assert out["approval"]["approval_id"] == "contract:t_owner"
    acc = out["contract_acceptance"]
    # MUTATION: serving the detail without the record, or without its hunks.
    assert acc["status"] == cj.JUDGMENT_PENDING and acc["waiting_on"] == cr.WAITING_OWNER
    assert acc["verdicts"][0]["evidence_item"] == "i0" and acc[cr.HUNKS_FIELD] == {"i0": HUNK}
    assert isinstance(acc["at"], str)
    # The control plane's own keys stay behind.
    assert "lease_id" not in acc and "state_sha256" not in acc


@pytest.mark.parametrize("task_id", ["t_reask", "t_theirs", "t_missing", ""])
def test_a_step_not_waiting_on_this_tenants_owner_is_404(client, db, clock, task_id):
    _owner_waiting(db, clock, "t_reask", waiting_on=cr.WAITING_REASK)
    _owner_waiting(db, clock, "t_theirs", tenant_id="research")
    response = client.get(f"/v1/approvals/contract:{task_id}", headers=auth_header("alice"))
    # MUTATION: serving another tenant's step, or one not waiting on its owner.
    assert response.status_code == 404


@pytest.mark.parametrize("verb, payload", [(":approve", {"digest": "x"}), (":reject", {"reason": "no"})])
def test_the_contract_item_is_never_decided_through_the_inbox(client, db, clock, verb, payload):
    _owner_waiting(db, clock, "t_owner")
    response = client.post(f"/v1/approvals/contract:t_owner{verb}", json=payload,
                           headers=auth_header("alice"))
    # MUTATION: a decide path that takes the projected id and moves the step.
    assert response.status_code == 404
    assert db.docs["tasks/t_owner"][cr.ACCEPTANCE_FIELD]["waiting_on"] == cr.WAITING_OWNER


def test_the_task_detail_carries_contract_acceptance_when_the_step_has_one(client, db, clock):
    _owner_waiting(db, clock, "t_owner")
    seed_task(db, task_id="t_plain", tenant_id="eng", state="SUCCEEDED")
    served = client.get("/v1/tasks/t_owner", headers=auth_header("alice"))
    assert served.status_code == 200, served.text
    acc = served.json()["task"]["contract_acceptance"]
    # MUTATION: the task detail not serving the record, or serving it raw.
    assert acc["verdicts"][0]["verdict"] == "unmet" and acc[cr.HUNKS_FIELD] == {"i0": HUNK}
    assert "lease_id" not in acc
    plain = client.get("/v1/tasks/t_plain", headers=auth_header("alice"))
    assert plain.status_code == 200 and "contract_acceptance" not in plain.json()["task"]


def test_a_stored_hunk_is_masked_again_wherever_it_is_served(client, db, clock):
    leaked = "ghp_" + secrets.token_hex(18)
    # A record an older build stored before its hunks were sanitized.
    _owner_waiting(db, clock, "t_owner")[cr.ACCEPTANCE_FIELD][cr.HUNKS_FIELD] = {
        "i0": f"+TOKEN = '{leaked}'\n"}
    detail = client.get("/v1/approvals/contract:t_owner", headers=auth_header("alice"))
    task = client.get("/v1/tasks/t_owner", headers=auth_header("alice"))
    assert detail.status_code == 200 and task.status_code == 200
    # MUTATION: serving the stored text without the redaction every served
    # text goes through.
    for acc in (detail.json()["contract_acceptance"], task.json()["task"]["contract_acceptance"]):
        assert leaked not in acc[cr.HUNKS_FIELD]["i0"] and "TOKEN" in acc[cr.HUNKS_FIELD]["i0"]
