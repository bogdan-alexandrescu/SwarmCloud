"""`GET /v1/tasks?claude_session=` serves only that session's tasks.

THE DEFECT (#1749, lane CHECK-FILTER-TASKS). CHECK-FILTER taught
`GET /v1/workflows` to filter by the Claude Code session on the server; the
single-task read did not get the same. The sc plugin's check read every page
of the caller's live tasks (`submitted_by=me`, one walk per live state) and
dropped other sessions' rows only after reading them, so a tenant's other
sessions cost the check pages.

The session is a task's `metadata.claude_session` (`sessiontag`). No index
pairs it with the state and submitter filters the bridge sends, so the route
reads store pages, keeps the rows tagged with the session, and pages on
inside the request -- with a token that continues strictly past the last row
it served.

Offline: the real routes over FakeFirestore.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from swarm_api.routes import tasks as route

from .conftest import auth_header, seed_task, seed_tenant

T0 = datetime(2026, 10, 21, 9, 0, tzinfo=timezone.utc)
MINE = "0f0e0d0c-aaaa-4bbb-8ccc-0000000000a1"
THEIRS = "0f0e0d0c-aaaa-4bbb-8ccc-0000000000a2"
TENANT_SIZE = 250
OWN = ["task_mine_new", "task_mine_mid", "task_mine_oldest"]


def _task(db, task_id: str, *, created_at: datetime, sid: str | None, **extra) -> None:
    doc = seed_task(db, task_id=task_id, tenant_id="eng", state="RUNNING", created_at=created_at)
    doc["metadata"] = {"claude_session": sid} if sid else {}
    doc["submitted_by"] = "alice@example.com"
    doc.update(extra)
    db.docs[f"tasks/{task_id}"] = doc


def _tenant(db) -> None:
    """250 live tasks of the caller's: this session owns 3 -- one of them the
    OLDEST of all, behind every other row -- two are untagged, and the other
    245 are another session's. task_mine_new and task_mine_mid each share
    their instant with one of those, so a token that names only an instant
    skips or repeats a row."""
    seed_tenant(db, "eng")
    for i in range(TENANT_SIZE - 5):
        _task(db, f"task_theirs_{i:03d}", created_at=T0 - timedelta(seconds=i), sid=THEIRS)
    _task(db, "task_untagged_a", created_at=T0 - timedelta(seconds=3, milliseconds=500), sid=None)
    _task(db, "task_untagged_b", created_at=T0 - timedelta(seconds=150, milliseconds=500), sid=None,
          workflow_id="wf_step")
    _task(db, "task_mine_new", created_at=T0 - timedelta(seconds=10), sid=MINE)
    _task(db, "task_mine_mid", created_at=T0 - timedelta(seconds=100), sid=MINE)
    _task(db, "task_mine_oldest", created_at=T0 - timedelta(hours=5), sid=MINE)


def _list(client, **params) -> dict:
    response = client.get("/v1/tasks", params={"state": "RUNNING", "submitted_by": "me", **params},
                          headers=auth_header("alice"))
    assert response.status_code == 200, response.text
    return response.json()


def _walk(client, **params) -> tuple[list[str], list[dict]]:
    ids: list[str] = []
    bodies: list[dict] = []
    token = None
    while True:
        body = _list(client, **params, **({"page_token": token} if token else {}))
        bodies.append(body)
        ids += [t["id"] for t in body["tasks"]]
        token = body["next_page_token"]
        if token is None:
            return ids, bodies
        # MUTATION: a token that never ends (the route re-serving its own)
        # would loop here; 30 pages is far past any honest walk.
        assert len(bodies) < 30, "the filtered listing never ended"


def _summed(bodies: list[dict], key: str) -> dict[str, int]:
    total: dict[str, int] = {}
    for body in bodies:
        for reason, count in body["filter"][key].items():
            total[reason] = total.get(reason, 0) + count
    return total


@pytest.mark.parametrize("limit", [1, 2, 50])
def test_the_session_filter_serves_exactly_the_sessions_tasks_page_by_page(db, client, limit):
    _tenant(db)
    ids, bodies = _walk(client, claude_session=MINE, limit=limit)
    # MUTATION: drop the session filter in `list_tasks` and the walk is the
    # whole tenant's 250. MUTATION: hand back the store page's token after
    # `limit` matches inside it and task_mine_mid (in the same store page as
    # task_mine_new) is skipped at limit=1.
    assert ids == OWN
    # MUTATION: mint the token on the row BEFORE the last one served and a
    # row comes back twice.
    assert len(set(ids)) == len(ids)
    assert {t["claude_session"] for b in bodies for t in b["tasks"]} == {MINE}


def test_the_first_page_holds_the_oldest_when_one_request_can_read_to_it(db, client):
    _tenant(db)
    body = _list(client, claude_session=MINE, limit=50)
    # MUTATION: read one store page per request and the oldest -- behind 247
    # newer rows -- is not on the first answer.
    assert [t["id"] for t in body["tasks"]] == OWN
    assert body["next_page_token"] is None


def test_the_filter_block_echoes_the_session_and_counts_what_it_left_out(db, client):
    _tenant(db)
    body = _list(client, claude_session=MINE, limit=50)
    # MUTATION: echo nothing and a bridge cannot tell this deployment
    # filtered from one that ignored the parameter.
    assert body["filter"]["claude_session"] == MINE
    # MUTATION: count an untagged row as another session's (or the reverse)
    # and these swap.
    assert body["filter"]["left_out"] == {"other_session": TENANT_SIZE - 5, "untagged": 2}
    # The rows a single-task reader would have counted itself: none of a
    # workflow step. MUTATION: count task_untagged_b (a workflow step) here
    # and the bridge reports a single task that is not one.
    assert body["filter"]["left_out_single"] == {"other_session": TENANT_SIZE - 5, "untagged": 1}


@pytest.mark.parametrize("limit", [1, 2])
def test_a_walk_counts_each_left_out_row_once(db, client, limit):
    _tenant(db)
    _, bodies = _walk(client, claude_session=MINE, limit=limit)
    # MUTATION: count the rows after the last one served in a page and the
    # next request reads them again -- counted twice over the walk.
    assert _summed(bodies, "left_out") == {"other_session": TENANT_SIZE - 5, "untagged": 2}


def test_the_route_scans_a_bounded_number_of_store_pages_per_request(db, client, monkeypatch):
    _tenant(db)
    monkeypatch.setattr(route, "MAX_FILTER_PAGES", 1)
    monkeypatch.setattr(route, "FILTER_PAGE_SIZE", 50)
    first = _list(client, claude_session=MINE, limit=50)
    # MUTATION: ignore the bound and the first answer holds all three.
    assert [t["id"] for t in first["tasks"]] == ["task_mine_new"]
    assert first["next_page_token"] is not None
    ids, bodies = _walk(client, claude_session=MINE, limit=50)
    # MUTATION: answer the bound with the store page's token minus one row,
    # or none at all, and the walk repeats or loses a row.
    assert ids == OWN
    assert _summed(bodies, "left_out") == {"other_session": TENANT_SIZE - 5, "untagged": 2}


def test_without_the_parameter_the_listing_is_unchanged(db, client):
    _tenant(db)
    ids, bodies = _walk(client, limit=200)
    # MUTATION: make the filter the default and this is 3.
    assert len(ids) == TENANT_SIZE
    assert "filter" not in bodies[0]


def test_a_session_that_names_nothing_is_refused(db, client):
    seed_tenant(db, "eng")
    response = client.get("/v1/tasks", params={"claude_session": "not a session"},
                          headers=auth_header("alice"))
    # MUTATION: accept it and an unscoped page is served as though it matched.
    assert response.status_code == 422, response.text
