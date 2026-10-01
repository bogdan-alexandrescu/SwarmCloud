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
        #: When set, `hold_history` answers from it by the cursor asked for
        #: (None for the first page) instead of from SPANS.
        self.history_script: dict[str | None, dict] | None = None

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
        if self.history_script is not None:
            page = self.history_script[cursor]
            return {"account_id": account_id, "from": start or "", "to": end or "",
                    "spans": [dict(s) for s in page["spans"]],
                    "next_cursor": page["next_cursor"]}
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


def test_history_owner_keeps_per_span_detail_with_the_borrowing_tenant(client):
    owner = client.get(f"/v1/accounts/{SHARED}/history", headers=auth_header("alice")).json()

    theirs = [s for s in owner["spans"] if not s["mine"]]
    assert {s["tenant"] for s in theirs} == {"research"}
    assert all("task_id" not in s for s in theirs)
    assert all(s["since"] and "until" in s and "end" in s for s in theirs)
    assert {s["end"] for s in theirs} == {"released", "unusable"}
    assert owner["next_cursor"] == "2026-09-30T12:00:00+00:00|1"


def _row(tenant: str, task: str, *, hours_ago: int, end: str | None = "released") -> dict:
    return _span(tenant, task, hours_ago=hours_ago, end=end)


def _everything_foreign_to_research(rows: list[dict]) -> list[str]:
    """Every timestamp string another tenant's rows carry."""
    out: list[str] = []
    for r in rows:
        if r["tenant_id"] != "research":
            out += [r[k] for k in ("assigned_at", "released_at", "hold_expires_at") if r[k]]
    return out


def test_a_borrower_is_served_none_of_another_tenants_times_or_ends(client, broker):
    rows = [_row("research", "research-task-1", hours_ago=1),
            _row("eng", "eng-task-1", hours_ago=3),
            _row("eng", "eng-task-2", hours_ago=4, end="unusable")]
    # The broker's cursor points at the LAST row of the page: an eng row.
    last = rows[-1]["assigned_at"]
    broker.history_script = {None: {"spans": rows, "next_cursor": f"{last}|1"}}

    body = client.get(f"/v1/accounts/{SHARED}/history", headers=auth_header("bob")).json()

    text = json.dumps(body)
    for stamp in _everything_foreign_to_research(rows):
        assert stamp not in text, f"another tenant's timestamp {stamp} reached the borrower"
    assert "unusable" not in text
    assert [s["mine"] for s in body["spans"]] == [True]
    assert all(set(s) <= {"since", "until", "end", "mine", "recorded", "verified",
                          "task_id", "attempt"} for s in body["spans"])
    assert body["others"] == 0


def test_a_borrowers_cursor_points_only_at_its_own_row(client, broker):
    rows = [_row("research", "research-task-1", hours_ago=1),
            _row("eng", "eng-task-1", hours_ago=3)]
    broker.history_script = {None: {"spans": rows, "next_cursor": f"{rows[-1]['assigned_at']}|1"}}

    body = client.get(f"/v1/accounts/{SHARED}/history", headers=auth_header("bob")).json()

    assert body["next_cursor"] == f"{rows[0]['assigned_at']}|1"


def test_a_borrower_counts_the_others_in_the_window_and_pages_past_them(client, broker):
    page1 = [_row("eng", "eng-task-1", hours_ago=1), _row("eng", "eng-task-2", hours_ago=2)]
    page2 = [_row("eng", "eng-task-3", hours_ago=3, end="unusable"),
             _row("research", "research-task-1", hours_ago=4)]
    broker.history_script = {
        None: {"spans": page1, "next_cursor": f"{page1[-1]['assigned_at']}|1"},
        f"{page1[-1]['assigned_at']}|1": {"spans": page2, "next_cursor": None},
    }

    body = client.get(f"/v1/accounts/{SHARED}/history", headers=auth_header("bob")).json()

    assert len(body["spans"]) == 1 and body["spans"][0]["mine"] is True
    assert body["others"] == 3
    assert body["next_cursor"] is None
    text = json.dumps(body)
    for stamp in _everything_foreign_to_research(page1 + page2):
        assert stamp not in text


def _eng_pages(n_pages: int, per_page: int) -> tuple[dict, int]:
    """A broker history of `n_pages` pages of eng rows only, each with a next cursor."""
    script: dict = {}
    count = 0
    for i in range(n_pages):
        rows = [_row("eng", f"eng-task-{i}-{j}", hours_ago=i * per_page + j + 1)
                for j in range(per_page)]
        count += len(rows)
        key = None if i == 0 else f"page-{i}"
        script[key] = {"spans": rows, "next_cursor": f"page-{i + 1}"}
    return script, count


def test_a_borrower_scan_with_no_own_row_in_20_pages_stops_limited_with_no_cursor(client, broker):
    script, count = _eng_pages(20, 2)
    broker.history_script = script

    body = client.get(f"/v1/accounts/{SHARED}/history", headers=auth_header("bob")).json()

    assert body["spans"] == []
    assert body["scan_limited"] is True
    assert body.get("next_cursor") is None
    assert body["others"] == count == 40
    assert "task_id" not in json.dumps(body)


def test_own_page_stops_at_20_pages_and_marks_the_scan_limited():
    from swarm_api.accountholds import own_page

    script, count = _eng_pages(25, 3)
    fetched: list[str | None] = []

    def fetch(cursor: str | None) -> dict:
        fetched.append(cursor)
        return script[cursor]

    served = own_page(script[None], tenant_id="research", cursor=None, fetch=fetch)

    assert len(fetched) == 19  # the first page was already in hand: 20 pages in all
    assert served["scan_limited"] is True
    assert served["next_cursor"] is None
    assert len(served["spans"]) == 20 * 3
    assert 20 * 3 < count


def test_the_owner_is_still_served_borrower_spans_with_the_tenant_name(client, broker):
    rows = [_row("research", "research-task-1", hours_ago=1, end="unusable"),
            _row("eng", "eng-task-1", hours_ago=3)]
    broker.history_script = {None: {"spans": rows, "next_cursor": None}}

    body = client.get(f"/v1/accounts/{SHARED}/history", headers=auth_header("alice")).json()

    theirs = [s for s in body["spans"] if not s["mine"]]
    assert [(s["tenant"], s["end"]) for s in theirs] == [("research", "unusable")]
    assert theirs[0]["since"] and theirs[0]["until"]


def test_an_oversized_cursor_is_a_422_before_the_broker_is_asked(client, broker):
    response = client.get(f"/v1/accounts/{SHARED}/history", headers=auth_header("alice"),
                          params={"cursor": "2026-09-30T12:00:00+00:00|" + "9" * 50})

    assert response.status_code == 422
    assert not [c for c in broker.calls if c[0] == "hold_history"]


def test_an_open_span_past_its_deadline_is_served_as_expired(client):
    body = client.get(f"/v1/accounts/{SHARED}/history", headers=auth_header("alice")).json()

    lapsed = [s for s in body["spans"] if s.get("task_id") == "eng-task-3"]
    assert lapsed and lapsed[0]["end"] == "expired" and lapsed[0]["until"] is not None


def test_history_passes_the_window_through_to_the_broker(client, broker):
    client.get(f"/v1/accounts/{SHARED}/history", headers=auth_header("alice"),
               params={"from": "2026-09-29T00:00:00Z", "to": "2026-09-30T00:00:00Z",
                       "cursor": "2026-09-30T00:00:00Z|1"})

    assert ("hold_history", SHARED, "2026-09-29T00:00:00Z",
            "2026-09-30T00:00:00Z", "2026-09-30T00:00:00Z|1") in broker.calls


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


_OUT_OF_RANGE = [
    pytest.param({"from": "0001-01-01T00:00:00+01:00", "to": "2026-09-30T00:00:00Z"}, id="from-underflows-in-utc"),
    pytest.param({"to": "9999-12-31T23:59:59-01:00"}, id="to-overflows-in-utc"),
    pytest.param({"to": "0001-01-05T00:00:00Z"}, id="default-span-underflows"),
    pytest.param({"cursor": "0001-01-01T00:00:00+01:00|0"}, id="cursor-underflows-in-utc"),
    pytest.param({"cursor": "2020-01-01T00:00:00+00:00|0"}, id="cursor-older-than-retention"),
    pytest.param({"to": "2999-01-01T00:00:00+00:00"}, id="to-in-the-far-future"),
]


@pytest.mark.parametrize("user", ["alice", "bob"])
@pytest.mark.parametrize("params", _OUT_OF_RANGE)
def test_an_instant_outside_the_retained_range_is_a_422_before_the_broker_is_asked(
    client, broker, user, params
):
    """Not an AccountPoolUnavailable (503) made of the broker's own 500."""
    response = client.get(f"/v1/accounts/{SHARED}/history", headers=auth_header(user), params=params)

    assert response.status_code == 422, (params, response.status_code, response.text[:200])
    assert not [c for c in broker.calls if c[0] == "hold_history"]


def test_a_borrower_cursor_built_from_a_foreign_span_is_a_422_and_costs_one_lookup(client, broker):
    foreign = SPANS[SHARED][2]["assigned_at"]  # an eng span; bob is research

    response = client.get(f"/v1/accounts/{SHARED}/history", headers=auth_header("bob"),
                          params={"cursor": f"{foreign}|0"})

    assert response.status_code == 422, response.text[:200]
    # The one bounded lookup that refused it, and no page read after.
    assert [c for c in broker.calls if c[0] == "hold_history" and c[4] == f"{foreign}|0"] == []
    assert len([c for c in broker.calls if c[0] == "hold_history"]) <= 1


def test_a_borrower_cursor_at_an_arbitrary_instant_is_a_422_and_reads_no_page(client, broker):
    arbitrary = _iso(NOW - timedelta(minutes=17, seconds=3))

    response = client.get(f"/v1/accounts/{SHARED}/history", headers=auth_header("bob"),
                          params={"cursor": f"{arbitrary}|0"})

    assert response.status_code == 422, response.text[:200]
    assert [c for c in broker.calls if c[0] == "hold_history" and c[4] is not None] == []


def test_a_borrower_cursor_this_service_issued_pages_normally(client, broker):
    own = SPANS[SHARED][0]["assigned_at"]  # research's own span

    response = client.get(f"/v1/accounts/{SHARED}/history", headers=auth_header("bob"),
                          params={"cursor": f"{own}|1"})

    assert response.status_code == 200, response.text[:200]
    assert any(c[0] == "hold_history" and c[4] == f"{own}|1" for c in broker.calls)


def test_an_owner_and_a_platform_cursor_are_not_checked_against_spans(client, broker):
    arbitrary = _iso(NOW - timedelta(minutes=17, seconds=3))

    owner = client.get(f"/v1/accounts/{SHARED}/history", headers=auth_header("alice"),
                       params={"cursor": f"{arbitrary}|0"})
    admin = client.get(f"/v1/accounts/{SHARED}/history", headers=auth_header("root"),
                       params={"cursor": f"{arbitrary}|0", "scope": "platform"})

    assert owner.status_code == 200 and admin.status_code == 200
