"""Who holds an account, as each viewer may see it: #379 parts 2 and 3.

The broker serves swarm-api every tenant's holds and spans on an account. This
file is the tenant boundary on that, and its central assertion is a TABLE over
the four viewers the approved design names:

    own tenant   on its own account: task links, attempt numbers, since when
    owner        of an account it lent: borrowers by tenant and count, no ids
    borrower     on a lent account: a count of everyone else's, no tenant name
    admin        `scope=platform` behind require_admin: everything

For every viewer, every response, every log line and every error is searched
for the task ids of tenants that viewer is not, and none may appear. A hold
that names a task its own tenant does not own is `verified: false` and carries
no id; a hold with no task is `recorded: false`. No response anywhere carries
an assignment id (it authorises a release) or a secret name.

The broker is a fake implementing the `AccountPool` protocol; the tasks and
attempts are real documents in the fake Firestore the real `Store` reads.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient

from swarm_api.main import create_app

from .conftest import auth_header, seed_task
from .test_log_redaction import _shape

NOW = datetime.now(timezone.utc)
SHARED = "eng:shared"
SOLO = "eng:solo"

#: Built at runtime so no credential-shaped literal exists in this source: the
#: key the broker's account record would carry, and a value shaped like one.
PLANTED_FIELD = _shape("sec", "ret_ref")
PLANTED_VALUE = _shape("swarm-acc", "ount-eng--shared")

ENG_TASKS = ("eng-task-1", "eng-task-2", "eng-task-3", "eng-task-solo")
RESEARCH_TASKS = ("research-task-1",)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _hold(assignment: str, tenant: str, task: str | None, attempt: str | None,
          *, minutes_ago: int, expired: bool = False) -> dict[str, Any]:
    at = NOW - timedelta(minutes=minutes_ago)
    return {
        "assignment_id": assignment,  # a broker that served it must not get it through
        PLANTED_FIELD: PLANTED_VALUE,
        "tenant_id": tenant,
        "task_id": task,
        "attempt_id": attempt,
        "assigned_at": _iso(at),
        "expires_at": _iso(NOW - timedelta(minutes=1) if expired else at + timedelta(hours=3)),
    }


HOLDS = {
    SHARED: [
        _hold("asg-e1", "eng", "eng-task-1", "att-e2", minutes_ago=38),
        _hold("asg-e0", "eng", None, None, minutes_ago=20),          # not recorded
        _hold("asg-r1", "research", "research-task-1", "att-r1", minutes_ago=10),
        # research's worker naming ENG's task: unverified, and eng's id must
        # reach research by no route.
        _hold("asg-r2", "research", "eng-task-2", None, minutes_ago=5),
        _hold("asg-dead", "eng", "eng-task-3", None, minutes_ago=200, expired=True),
    ],
    SOLO: [_hold("asg-s1", "eng", "eng-task-solo", "att-s1", minutes_ago=3)],
}


def _span(tenant: str, task: str | None, *, hours_ago: int, end: str | None,
          open_lapsed: bool = False) -> dict[str, Any]:
    at = NOW - timedelta(hours=hours_ago)
    return {
        "tenant_id": tenant,
        "task_id": task,
        "attempt_id": None,
        "assigned_at": _iso(at),
        "released_at": _iso(at + timedelta(minutes=30)) if end else None,
        "end": end,
        "hold_expires_at": _iso(NOW - timedelta(minutes=2) if open_lapsed else at + timedelta(hours=3)),
        "assignment_id": "asg-hist",
        PLANTED_FIELD: PLANTED_VALUE,
    }


SPANS = {
    SHARED: [
        _span("research", "research-task-1", hours_ago=1, end="released"),
        _span("research", "eng-task-2", hours_ago=2, end="unusable"),
        _span("eng", "eng-task-1", hours_ago=3, end="released"),
        _span("eng", "eng-task-3", hours_ago=4, end=None, open_lapsed=True),
        _span("eng", None, hours_ago=5, end="expired"),
    ],
    SOLO: [],
}


class FakeBroker:
    def __init__(self) -> None:
        self.accounts = {
            SHARED: {"account_id": SHARED, "owner_tenant": "eng", "lend_to": ["research"]},
            SOLO: {"account_id": SOLO, "owner_tenant": "eng", "lend_to": []},
        }
        self.calls: list[tuple] = []

    def list_accounts(self, tenant_id: str) -> dict:
        return {"accounts": [
            dict(a) for a in self.accounts.values()
            if a["owner_tenant"] == tenant_id or tenant_id in a["lend_to"]
        ]}

    def holds(self, account_id: str) -> dict:
        self.calls.append(("holds", account_id))
        return {"account_id": account_id, "owner_tenant": "eng",
                "holds": [dict(h) for h in HOLDS[account_id]]}

    def hold_history(self, account_id: str, *, start, end, cursor) -> dict:
        self.calls.append(("hold_history", account_id, start, end, cursor))
        return {"account_id": account_id, "from": start or _iso(NOW - timedelta(days=7)),
                "to": end or _iso(NOW), "spans": [dict(s) for s in SPANS[account_id]],
                "next_cursor": "2026-09-30T12:00:00+00:00|1"}


def _attempt(db, attempt_id: str, task_id: str, tenant: str, minutes_ago: int) -> None:
    created = NOW - timedelta(minutes=minutes_ago)
    db.docs[f"attempts/{attempt_id}"] = {
        "attempt_id": attempt_id, "task_id": task_id, "tenant_id": tenant,
        "generation": 1, "lease_id": f"lease_{attempt_id}", "backend": "CLOUD_RUN_JOB",
        "execution_name": None, "created_at": created, "started_at": None,
        "completed_at": None, "exit_code": None, "error": None,
        "peak_rss_bytes": None, "oom_near_miss": False, "checkpoints": [],
    }


@pytest.fixture
def broker() -> FakeBroker:
    return FakeBroker()


@pytest.fixture
def client(api_context, db, broker) -> TestClient:
    for task in ENG_TASKS:
        seed_task(db, task_id=task, tenant_id="eng", state="RUNNING")
    for task in RESEARCH_TASKS:
        seed_task(db, task_id=task, tenant_id="research", state="RUNNING")
    _attempt(db, "att-e1", "eng-task-1", "eng", 90)
    _attempt(db, "att-e2", "eng-task-1", "eng", 40)
    _attempt(db, "att-r1", "research-task-1", "research", 12)
    _attempt(db, "att-s1", "eng-task-solo", "eng", 4)
    app = create_app(api_context)
    app.state.account_pool = broker
    return TestClient(app, raise_server_exceptions=False)


def _foreign_ids(tenant: str | None) -> tuple[str, ...]:
    """Task ids that belong to a tenant other than `tenant`."""
    if tenant is None:
        return ()
    return RESEARCH_TASKS if tenant == "eng" else ENG_TASKS


# The table. (case, user, account, scope, viewer, tenant whose ids it may see)
VIEWERS = [
    ("own tenant", "alice", SOLO, None, "owner", "eng"),
    ("owner who lent", "alice", SHARED, None, "owner", "eng"),
    ("borrower", "bob", SHARED, None, "borrower", "research"),
    ("admin", "root", SHARED, "platform", "platform", None),
]


@pytest.mark.parametrize("path", ["holders", "history"])
@pytest.mark.parametrize(("case", "user", "account", "scope", "viewer", "tenant"), VIEWERS,
                         ids=[v[0] for v in VIEWERS])
def test_no_viewer_is_served_another_tenants_task_id(
    client, caplog, path, case, user, account, scope, viewer, tenant
):
    caplog.set_level(logging.DEBUG)
    params = {"scope": scope} if scope else {}

    response = client.get(f"/v1/accounts/{account}/{path}", params=params,
                          headers=auth_header(user))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["viewer"] == viewer
    served = json.dumps(body)
    for foreign in _foreign_ids(tenant):
        assert foreign not in served, f"{case}: {foreign} reached the response"
        assert foreign not in caplog.text, f"{case}: {foreign} reached a log line"
    for forbidden in ("asg-", "assignment", PLANTED_FIELD, PLANTED_VALUE, "secret"):
        assert forbidden not in served, f"{case}: {forbidden!r} was served"
    assert "eng-task-3" not in json.dumps(body.get("holders", [])), "an expired hold was served"


def test_the_owner_sees_its_own_work_and_borrowers_only_by_tenant(client):
    body = client.get(f"/v1/accounts/{SHARED}/holders", headers=auth_header("alice")).json()

    assert body["total"] == 4, "four live holds; the expired one is not counted"
    own = {h.get("task_id"): h for h in body["holders"]}
    assert own["eng-task-1"]["attempt"] == 2
    assert own["eng-task-1"]["verified"] is True
    assert own[None] == {"since": own[None]["since"], "recorded": False, "verified": False}
    assert body["others"] == 2
    assert body["by_tenant"] == [{"tenant": "research", "n": 2}]


def test_a_borrower_sees_a_count_and_no_tenant_name(client):
    body = client.get(f"/v1/accounts/{SHARED}/holders", headers=auth_header("bob")).json()

    assert body["others"] == 2
    assert "by_tenant" not in body
    assert all("tenant" not in h for h in body["holders"])
    mine = sorted(body["holders"], key=lambda h: h["since"])
    assert mine[0]["task_id"] == "research-task-1" and mine[0]["attempt"] == 1
    # Its own worker named eng's task. Shown as unverified, without the id.
    assert mine[1] == {"since": mine[1]["since"], "recorded": True, "verified": False}


def test_the_admin_sees_every_hold_with_the_unverified_claim_flagged(client):
    body = client.get(f"/v1/accounts/{SHARED}/holders", params={"scope": "platform"},
                      headers=auth_header("root")).json()

    claims = {(h["tenant"], h.get("task_id"), h["verified"]) for h in body["holders"]}
    assert claims == {
        ("eng", "eng-task-1", True), ("eng", None, False),
        ("research", "research-task-1", True), ("research", "eng-task-2", False),
    }


def test_history_anonymises_others_by_viewer(client):
    owner = client.get(f"/v1/accounts/{SHARED}/history", headers=auth_header("alice")).json()
    borrower = client.get(f"/v1/accounts/{SHARED}/history", headers=auth_header("bob")).json()

    theirs = [s for s in owner["spans"] if not s["mine"]]
    assert {s["tenant"] for s in theirs} == {"research"}
    assert all("task_id" not in s for s in theirs)
    others = [s for s in borrower["spans"] if not s["mine"]]
    assert others and all(set(s) == {"since", "until", "end", "mine"} for s in others)
    assert owner["next_cursor"] == "2026-09-30T12:00:00+00:00|1"


def test_an_open_span_past_its_deadline_is_served_as_expired(client):
    body = client.get(f"/v1/accounts/{SHARED}/history", headers=auth_header("alice")).json()

    lapsed = [s for s in body["spans"] if s.get("task_id") == "eng-task-3"]
    assert lapsed and lapsed[0]["end"] == "expired" and lapsed[0]["until"] is not None


def test_history_passes_the_window_through_to_the_broker(client, broker):
    client.get(f"/v1/accounts/{SHARED}/history", headers=auth_header("alice"),
               params={"from": "2026-09-29T00:00:00Z", "to": "2026-09-30T00:00:00Z",
                       "cursor": "c|1"})

    assert ("hold_history", SHARED, "2026-09-29T00:00:00Z",
            "2026-09-30T00:00:00Z", "c|1") in broker.calls


def test_an_empty_history_is_an_empty_list_not_an_error(client):
    body = client.get(f"/v1/accounts/{SOLO}/history", headers=auth_header("alice")).json()
    assert body["spans"] == []


@pytest.mark.parametrize("path", ["holders", "history"])
def test_a_tenant_the_account_is_not_lent_to_gets_a_404_and_no_broker_read(
    client, broker, caplog, path
):
    caplog.set_level(logging.DEBUG)

    response = client.get(f"/v1/accounts/{SHARED}/{path}", headers=auth_header("carol"))

    assert response.status_code == 404
    assert not [c for c in broker.calls if c[0] in ("holds", "hold_history")]
    for task in (*ENG_TASKS, *RESEARCH_TASKS):
        assert task not in response.text and task not in caplog.text


@pytest.mark.parametrize("path", ["holders", "history"])
def test_platform_scope_needs_an_admin(client, broker, path):
    response = client.get(f"/v1/accounts/{SHARED}/{path}", params={"scope": "platform"},
                          headers=auth_header("bob"))

    assert response.status_code == 403
    assert not [c for c in broker.calls if c[0] in ("holds", "hold_history")]
    for task in (*ENG_TASKS, *RESEARCH_TASKS):
        assert task not in response.text


def test_an_unknown_scope_is_refused(client):
    response = client.get(f"/v1/accounts/{SHARED}/holders", params={"scope": "everyone"},
                          headers=auth_header("alice"))
    assert response.status_code == 422
