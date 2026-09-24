"""`GET /v1/attempts`: every attempt of the caller's tenant, across tasks.

THE GAP. `Store.list_attempts` accepted `task_id=None` from the day it was
written and the `attempts-tenant-created` index was declared for it, but the
only route that called it passed a task id. So "what did this tenant's agents
cost this week" had no answer short of one request per task -- the N+1 loop
docs/web-ui/ui-audit-and-build-prompt.md §B9.S3 warns will otherwise be
written in the browser. That section specifies the shape:

    GET /v1/attempts?since=&until=&limit=&page_token=
     -> {"attempts": [...], "next_page_token": str | null,
         "coverage": {"attempts": int, "with_spend": int}}

COVERAGE IS THE POINT. A sum over attempts whose cost was never reported is
not a cost; it is a lower bound that looks like one. `coverage` counts, over
the rows on THIS page, how many there are and how many carry a measured
`cost_usd`, so a figure can be drawn as "measured on 2 of 7". Zero dollars is
a measurement and counts; `None` is "not reported" and does not.

TENANT-SCOPED (invariant 9). The tenant comes from `tenant_scope`, never from
the request, and the query starts from an equality filter on it.

Offline: the real routes over FakeFirestore.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .conftest import auth_header, seed_tenant

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)


def _attempt(
    db,
    attempt_id: str,
    *,
    tenant: str = "eng",
    task_id: str = "task_a",
    minutes_ago: float,
    cost_usd: float | None = None,
    input_tokens: int | None = None,
) -> None:
    created = NOW - timedelta(minutes=minutes_ago)
    doc: dict[str, Any] = {
        "attempt_id": attempt_id,
        "task_id": task_id,
        "tenant_id": tenant,
        "generation": 1,
        "lease_id": f"lease_{attempt_id}",
        "backend": "CLOUD_RUN_JOB",
        "execution_name": None,
        "created_at": created,
        "started_at": created + timedelta(seconds=5),
        "completed_at": None,
        "exit_code": None,
        "error": None,
        "peak_rss_bytes": None,
        "oom_near_miss": False,
        "checkpoints": [],
    }
    # Written the way `control.record_spend` writes them: a field the runner
    # did not report is ABSENT, never zero.
    if cost_usd is not None:
        doc["cost_usd"] = cost_usd
    if input_tokens is not None:
        doc["input_tokens"] = input_tokens
    db.docs[f"attempts/{attempt_id}"] = doc


def _get(client, user: str = "alice", **params: Any):
    return client.get("/v1/attempts", params=params, headers=auth_header(user))


def _page(client, user: str = "alice", **params: Any) -> dict[str, Any]:
    response = _get(client, user, **params)
    assert response.status_code == 200, f"/v1/attempts -> {response.status_code}: {response.text}"
    return response.json()


def test_every_attempt_across_every_task_newest_first(client, db):
    seed_tenant(db, "eng")
    _attempt(db, "att_1", task_id="task_a", minutes_ago=50)
    _attempt(db, "att_2", task_id="task_b", minutes_ago=40)
    _attempt(db, "att_3", task_id="task_a", minutes_ago=30)
    _attempt(db, "att_4", task_id="task_c", minutes_ago=20)

    body = _page(client)

    assert [a["attempt_id"] for a in body["attempts"]] == ["att_4", "att_3", "att_2", "att_1"]
    assert {a["task_id"] for a in body["attempts"]} == {"task_a", "task_b", "task_c"}
    assert body["next_page_token"] is None


def test_the_rows_are_the_same_shape_the_per_task_route_serves(client, db):
    """One attempt, one JSON shape. A second serialiser would be a second
    place for the five spend fields to go missing."""
    seed_tenant(db, "eng")
    _attempt(db, "att_1", task_id="task_a", minutes_ago=5, cost_usd=0.25, input_tokens=100)
    from .conftest import seed_task

    seed_task(db, task_id="task_a", tenant_id="eng")

    across = _page(client)["attempts"][0]
    per_task = client.get("/v1/tasks/task_a/attempts", headers=auth_header("alice")).json()
    assert across == per_task["attempts"][0]
    assert across["cost_usd"] == 0.25


def test_another_tenants_attempts_never_appear(client, db):
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    _attempt(db, "att_eng", tenant="eng", minutes_ago=10, cost_usd=1.0)
    _attempt(db, "att_res", tenant="research", minutes_ago=5, cost_usd=9.0)

    alice = _page(client, "alice")
    assert [a["attempt_id"] for a in alice["attempts"]] == ["att_eng"]
    assert alice["coverage"] == {"attempts": 1, "with_spend": 1}

    bob = _page(client, "bob")
    assert [a["attempt_id"] for a in bob["attempts"]] == ["att_res"]


def test_a_tenant_named_in_the_query_string_is_not_obeyed(client, db):
    """The tenant is derived from the verified identity. A caller who names
    another one gets their own rows, not research's."""
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    _attempt(db, "att_eng", tenant="eng", minutes_ago=10)
    _attempt(db, "att_res", tenant="research", minutes_ago=5)

    body = _page(client, "alice", tenant_id="research")
    assert [a["attempt_id"] for a in body["attempts"]] == ["att_eng"]


def test_coverage_counts_measured_cost_and_zero_is_a_measurement(client, db):
    seed_tenant(db, "eng")
    _attempt(db, "att_paid", minutes_ago=40, cost_usd=0.1234)
    _attempt(db, "att_free", minutes_ago=30, cost_usd=0.0)       # a mock run: measured, $0
    _attempt(db, "att_unknown", minutes_ago=20)                  # never reported
    _attempt(db, "att_tokens_only", minutes_ago=10, input_tokens=500)

    body = _page(client)

    assert body["coverage"] == {"attempts": 4, "with_spend": 2}, (
        "with_spend counts attempts whose cost_usd was REPORTED: $0.00 is a "
        "measurement, an absent cost is not, and tokens without a cost do not "
        "make a cost figure measured"
    )
    costs = {a["attempt_id"]: a["cost_usd"] for a in body["attempts"]}
    assert costs["att_free"] == 0.0
    assert costs["att_unknown"] is None


def test_an_empty_answer_says_zero_of_zero(client, db):
    seed_tenant(db, "eng")
    body = _page(client)
    assert body["attempts"] == []
    assert body["coverage"] == {"attempts": 0, "with_spend": 0}
    assert body["next_page_token"] is None


def test_paging_reaches_every_attempt_once_and_coverage_describes_its_own_page(client, db):
    seed_tenant(db, "eng")
    for i in range(5):
        _attempt(db, f"att_{i}", minutes_ago=50 - i, cost_usd=(1.0 if i % 2 == 0 else None))

    seen: list[str] = []
    measured = 0
    token = None
    pages = 0
    while True:
        params: dict[str, Any] = {"limit": 2}
        if token:
            params["page_token"] = token
        body = _page(client, **params)
        assert body["coverage"]["attempts"] == len(body["attempts"]), (
            "coverage must describe the rows it arrived with"
        )
        seen.extend(a["attempt_id"] for a in body["attempts"])
        measured += body["coverage"]["with_spend"]
        token = body["next_page_token"]
        pages += 1
        assert pages < 20
        if token is None:
            break

    assert seen == [f"att_{i}" for i in reversed(range(5))]
    assert pages == 3
    assert measured == 3


def test_attempts_created_in_the_same_instant_are_all_served(client, db):
    """The scheduler stamps `created_at` per attempt; two in one microsecond is
    rare and must still not drop one at a page boundary."""
    seed_tenant(db, "eng")
    for suffix in ("e", "d", "c", "b", "a"):
        _attempt(db, f"att_{suffix}", minutes_ago=10)
    _attempt(db, "att_older", minutes_ago=20)

    seen: list[str] = []
    token = None
    for _ in range(10):
        params: dict[str, Any] = {"limit": 2}
        if token:
            params["page_token"] = token
        body = _page(client, **params)
        seen.extend(a["attempt_id"] for a in body["attempts"])
        token = body["next_page_token"]
        if token is None:
            break

    assert seen == ["att_e", "att_d", "att_c", "att_b", "att_a", "att_older"]


def test_since_and_until_bound_the_window(client, db):
    """since is inclusive, until exclusive -- so adjacent windows tile."""
    seed_tenant(db, "eng")
    _attempt(db, "att_3h", minutes_ago=180)
    _attempt(db, "att_2h", minutes_ago=120)
    _attempt(db, "att_1h", minutes_ago=60)
    _attempt(db, "att_now", minutes_ago=0)

    since = (NOW - timedelta(minutes=120)).isoformat()
    until = (NOW - timedelta(minutes=0)).isoformat()
    body = _page(client, since=since, until=until)
    assert [a["attempt_id"] for a in body["attempts"]] == ["att_1h", "att_2h"]

    only_since = _page(client, since=(NOW - timedelta(minutes=90)).isoformat())
    assert [a["attempt_id"] for a in only_since["attempts"]] == ["att_now", "att_1h"]


def test_a_window_that_ends_before_it_starts_is_a_422(client, db):
    seed_tenant(db, "eng")
    response = _get(
        client,
        since=NOW.isoformat(),
        until=(NOW - timedelta(hours=1)).isoformat(),
    )
    assert response.status_code == 422, response.text


def test_a_malformed_page_token_is_a_422(client, db):
    seed_tenant(db, "eng")
    _attempt(db, "att_1", minutes_ago=5)
    response = _get(client, page_token="not-a-token")
    assert response.status_code == 422, response.text


def test_an_events_token_is_not_an_attempts_token(client, db):
    """Both routes mint (timestamp, id) tokens. One is not the other."""
    from .conftest import seed_task

    seed_tenant(db, "eng")
    seed_task(db, task_id="task_a", tenant_id="eng")
    for i in range(3):
        db.docs[f"tasks/task_a/events/ev_{i}"] = {
            "event_id": f"ev_{i}", "task_id": "task_a", "tenant_id": "eng",
            "type": "heartbeat", "at": NOW - timedelta(minutes=10 - i),
            "attempt_id": None, "lease_id": None, "generation": None, "detail": {},
        }
    events = client.get(
        "/v1/tasks/task_a/events", params={"limit": 1}, headers=auth_header("alice")
    ).json()
    assert events.get("next_page_token"), "the events route did not mint a token to reuse"

    response = _get(client, page_token=events["next_page_token"])
    assert response.status_code == 422, response.text


def test_unauthenticated_is_refused(client):
    assert client.get("/v1/attempts").status_code == 401
