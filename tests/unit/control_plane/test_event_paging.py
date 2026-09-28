"""GET /v1/tasks/{id}/events pages, in both directions, and loses nothing.

THE DEFECT. `Store.list_events` was `order_by("at", ASCENDING).limit(n)` with
no cursor, and the route took neither a `page_token` nor an `order`. `limit` is
clamped to `max_page_size`, so the HEAD of a task's history was always
reachable and the TAIL never was: a run that wrote more events than one page
lost its end -- which is the part an operator opens a timeline to read.
`swarm_mcp.follow` says so in its own docstring: "once a task has produced
more events than the route will return, the later ones are not reachable
through this API at all."

WHAT IS PINNED

  * following `next_page_token` from the first page reaches every event,
    exactly once, in order -- ascending and descending;
  * `order=desc` serves the END of a run on the first page;
  * the old default is unchanged: no `page_token` and no `order` means the
    oldest `limit` events, ascending, and row N is row N for every limit that
    reaches it. `swarm_mcp.follow` keeps a COUNT as its cursor and depends on
    exactly that;
  * events that share a timestamp across a page boundary are neither dropped
    nor repeated. That is why the token is (at, event_id) and not a time: a
    timestamp cursor either skips the rest of a tie or serves it twice;
  * a malformed token, or one minted for the other order or for another task,
    is a 422 -- never a silently different page.

Offline: the real routes over FakeFirestore. No credentials, no emulator.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from swarm_common.states import EventType

from .conftest import auth_header, seed_task, seed_tenant

T0 = datetime(2026, 9, 24, 9, 0, tzinfo=timezone.utc)


def _task(db, task_id: str = "task_long", tenant: str = "eng") -> str:
    seed_tenant(db, tenant)
    seed_task(db, task_id=task_id, tenant_id=tenant, state="RUNNING")
    return task_id


def _event(db, task_id: str, event_id: str, at: datetime, tenant: str = "eng") -> None:
    db.docs[f"tasks/{task_id}/events/{event_id}"] = {
        "event_id": event_id,
        "task_id": task_id,
        "tenant_id": tenant,
        "type": EventType.HEARTBEAT.value,
        "at": at,
        "attempt_id": None,
        "lease_id": None,
        "generation": None,
        "detail": {},
    }


def _history(db, task_id: str, count: int) -> list[str]:
    """`count` events a minute apart, written NEWEST FIRST.

    Reverse insertion order on purpose: the in-memory Firestore returns rows in
    insertion order wherever nothing orders them, so a reader that forgot to
    order would come back reversed here rather than accidentally right.
    """
    ids = [f"ev_{i:03d}" for i in range(count)]
    for i in reversed(range(count)):
        _event(db, task_id, ids[i], T0 + timedelta(minutes=i))
    return ids


def _get(client, task_id: str, user: str = "alice", **params: Any):
    return client.get(
        f"/v1/tasks/{task_id}/events", params=params, headers=auth_header(user)
    )


def _page(client, task_id: str, user: str = "alice", **params: Any) -> dict[str, Any]:
    response = _get(client, task_id, user, **params)
    assert response.status_code == 200, response.text
    body = response.json()
    assert "next_page_token" in body, (
        "the events page carries no next_page_token, so nothing past the first "
        "page of a task's history is reachable through this API"
    )
    return body


def _walk(client, task_id: str, *, limit: int, order: str | None = None) -> tuple[list[str], int]:
    """Follow next_page_token to the end. Returns (event ids, pages read)."""
    seen: list[str] = []
    token: str | None = None
    pages = 0
    while True:
        params: dict[str, Any] = {"limit": limit}
        if order is not None:
            params["order"] = order
        if token is not None:
            params["page_token"] = token
        body = _page(client, task_id, **params)
        seen.extend(e["event_id"] for e in body["events"])
        token = body["next_page_token"]
        pages += 1
        assert pages <= 200, "paging never terminated"
        if token is None:
            return seen, pages


# -- the tail is reachable ---------------------------------------------------


def test_following_the_token_reaches_every_event_in_order(client, db):
    task_id = _task(db)
    ids = _history(db, task_id, 7)

    seen, pages = _walk(client, task_id, limit=3)

    assert seen == ids, "ascending paging must reach the tail, once, in order"
    assert pages == 3


def test_a_history_that_fills_its_last_page_exactly_ends_without_an_empty_page(client, db):
    """6 events at limit 3 is two pages. A token on the second would send a
    caller for a third page that is always empty -- harmless once, and a
    poller that pages forever on every tick."""
    task_id = _task(db)
    ids = _history(db, task_id, 6)

    seen, pages = _walk(client, task_id, limit=3)

    assert seen == ids
    assert pages == 2


def test_order_desc_serves_the_end_of_a_run_first(client, db):
    task_id = _task(db)
    ids = _history(db, task_id, 7)

    first = _page(client, task_id, limit=3, order="desc")
    assert [e["event_id"] for e in first["events"]] == ids[::-1][:3], (
        "order=desc must put the NEWEST event first -- the end of the run is "
        "the reason it exists"
    )
    assert first["next_page_token"] is not None

    seen, _ = _walk(client, task_id, limit=3, order="desc")
    assert seen == ids[::-1]


# -- the old default is unchanged --------------------------------------------


def test_no_parameters_still_means_the_oldest_page_ascending(client, db):
    """Every existing caller -- the web UI and `swarm_mcp` -- passes neither
    `order` nor `page_token`. They must get exactly what they got before."""
    task_id = _task(db)
    ids = _history(db, task_id, 60)

    body = _page(client, task_id)

    # default_page_size is 50 in the test settings (conftest.api_settings).
    assert [e["event_id"] for e in body["events"]] == ids[:50]
    assert body["task_id"] == task_id
    assert body["next_page_token"] is not None, "60 events at 50 per page has a second page"


def test_row_n_is_row_n_for_every_limit_that_reaches_it(client, db):
    """`swarm_mcp.follow` keeps a COUNT as its cursor: it asks for
    delivered + page rows and slices off the ones it has shown. That is only
    sound while a longer head is an extension of a shorter one."""
    task_id = _task(db)
    ids = _history(db, task_id, 12)

    for limit in (1, 2, 5, 11, 12, 30):
        body = _page(client, task_id, limit=limit)
        assert [e["event_id"] for e in body["events"]] == ids[:limit], limit


def test_the_event_row_shape_is_unchanged(client, db):
    task_id = _task(db)
    _history(db, task_id, 2)
    row = _page(client, task_id)["events"][0]
    # `detail_redaction_count` is the one key added since (the PR #229 review):
    # every string in `detail` is masked by the task's masker, and this says
    # how many masks that took.
    assert set(row) == {
        "event_id", "task_id", "type", "at", "attempt_id", "lease_id",
        "generation", "detail", "detail_redaction_count",
    }


# -- ties at a page boundary -------------------------------------------------


def _tied_history(db, task_id: str) -> list[tuple[datetime, str]]:
    """Two before, FIVE at one instant, two after -- in (at, id) order.

    The tied five are written in REVERSE id order, so a reader that relied on
    whatever order the store returns them in, rather than ordering by id
    itself, puts them in the wrong place.
    """
    tie = T0 + timedelta(minutes=10)
    rows = [
        (T0 + timedelta(minutes=1), "ev_a"),
        (T0 + timedelta(minutes=2), "ev_b"),
        *[(tie, f"ev_t{i}") for i in range(5)],
        (T0 + timedelta(minutes=20), "ev_y"),
        (T0 + timedelta(minutes=21), "ev_z"),
    ]
    for at, event_id in sorted(rows, key=lambda r: r[1], reverse=True):
        _event(db, task_id, event_id, at)
    return sorted(rows)


def test_events_sharing_a_timestamp_are_neither_dropped_nor_repeated(client, db):
    """Every limit, both directions, over a tie wider than a page.

    A cursor that is only a time drops the rest of the tie (`at > t`) or
    serves it again (`at >= t`); limits 1-4 each put a page boundary inside
    the five tied events at a different offset.
    """
    task_id = _task(db)
    expected = [event_id for _, event_id in _tied_history(db, task_id)]

    for limit in (1, 2, 3, 4, 6):
        asc, _ = _walk(client, task_id, limit=limit)
        assert asc == expected, f"asc, limit={limit}: {asc}"
        desc, _ = _walk(client, task_id, limit=limit, order="desc")
        assert desc == expected[::-1], f"desc, limit={limit}: {desc}"


def test_a_first_page_that_ends_inside_a_tie_takes_the_lowest_ids(client, db):
    """The first page has no cursor, so it is the window read itself that
    cuts the tie. Which members land on it must be decided by id -- the same
    rule the next page's token continues from -- or the two pages disagree
    about who was already served."""
    task_id = _task(db)
    expected = [event_id for _, event_id in _tied_history(db, task_id)]

    body = _page(client, task_id, limit=4)
    assert [e["event_id"] for e in body["events"]] == expected[:4]


# -- the token is checked, not trusted ---------------------------------------


def test_a_malformed_token_is_a_422_not_a_first_page(client, db):
    task_id = _task(db)
    _history(db, task_id, 3)
    for token in ("not-a-token", "eyJub3QiOiAiYSBjdXJzb3IifQ==", "%%%"):
        response = _get(client, task_id, limit=1, page_token=token)
        assert response.status_code == 422, (token, response.status_code, response.text)


def test_a_token_minted_for_one_order_is_refused_for_the_other(client, db):
    """Continuing an ascending walk with order=desc would serve the rows BEFORE
    the cursor, newest first -- a page that looks right and is not the next
    one. Refused rather than reinterpreted."""
    task_id = _task(db)
    _history(db, task_id, 5)
    token = _page(client, task_id, limit=2)["next_page_token"]
    assert token is not None

    response = _get(client, task_id, limit=2, order="desc", page_token=token)
    assert response.status_code == 422, response.text


def test_a_token_from_another_task_is_refused(client, db):
    first = _task(db, "task_one")
    second = _task(db, "task_two")
    _history(db, first, 5)
    _history(db, second, 5)
    token = _page(client, first, limit=2)["next_page_token"]
    assert token is not None

    response = _get(client, second, limit=2, page_token=token)
    assert response.status_code == 422, response.text


def test_an_unknown_order_is_refused(client, db):
    task_id = _task(db)
    response = _get(client, task_id, order="sideways")
    assert response.status_code == 422, response.text


def test_a_valid_token_does_not_open_another_tenants_task(client, db):
    """The token carries a task id and a timestamp. Neither is a credential:
    the tenant check still runs first and a foreign task is still a 404."""
    task_id = _task(db)
    _history(db, task_id, 5)
    token = _page(client, task_id, limit=2)["next_page_token"]
    assert token is not None

    response = _get(client, task_id, user="bob", limit=2, page_token=token)
    assert response.status_code == 404, response.text
