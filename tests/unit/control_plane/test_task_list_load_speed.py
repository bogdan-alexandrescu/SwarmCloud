"""#2688 LOAD-SPEED: `GET /v1/tasks` serves the Agents page in fewer, overlapping reads.

Measured on the live deployment (2026-11-01): the Agents page's
`GET /v1/tasks?limit=200&view=summary` took 5.3 s cold and 1.1-2.1 s per
poll with swarm-api at 47-68% CPU, so the time was serial Firestore round
trips, not computation. This file holds the three server-side changes to it:

  * the page's enrichment reads (pools, accounts, leases, attempt totals,
    names) start together, so a request waits for the slowest, not the sum;
  * `view=summary` reads only `store.TASK_SUMMARY_FIELDS`, never the
    task's `result_summary`, and still masks with the input's literals;
  * `state=` takes several states in one `in` query, so the console reads the
    live and waiting states in one request instead of six.
"""

from __future__ import annotations

import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from swarm_api.store import STATE_IN_MAX, TASK_SUMMARY_FIELDS, TASK_SUMMARY_UNREAD

from .conftest import auth_header, seed_task, seed_tenant
from .fakes import FakeFirestore, FakeQuery

#: The six states the console's live-and-waiting list shows.
LIVE_AND_WAITING = ("QUEUED", "READY", "LEASED", "DISPATCHED", "STARTING", "RUNNING")

#: How long each fake round trip takes: long enough that two overlapping reads
#: are certain to be seen in flight together.
ROUND_TRIP_S = 0.02


class Meter:
    """Counts the fake's round trips (a query run or a `get_all`), and the most
    that were in flight at once."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reads = 0
        self.in_flight = 0
        self.peak = 0
        self.projections: list[tuple[str, tuple[str, ...] | None, tuple]] = []

    def __enter__(self) -> None:
        with self._lock:
            self.reads += 1
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)

    def __exit__(self, *exc) -> None:
        time.sleep(ROUND_TRIP_S)
        with self._lock:
            self.in_flight -= 1


@pytest.fixture
def meter(monkeypatch) -> Meter:
    m = Meter()
    stream, get_all = FakeQuery.stream, FakeFirestore.get_all

    def metered_stream(self, *args, **kwargs):
        with m:
            m.projections.append((self._path, self._projection, self._filters))
            return iter(list(stream(self, *args, **kwargs)))

    def metered_get_all(self, references, *args, **kwargs):
        refs = list(references)
        with m:
            first = refs[0].path.split("/", 1)[0] if refs else None
            m.projections.append((f"get_all:{first}", None, ()))
            return iter(list(get_all(self, refs, *args, **kwargs)))

    monkeypatch.setattr(FakeQuery, "stream", metered_stream)
    monkeypatch.setattr(FakeQuery, "get", lambda self, *a, **k: list(metered_stream(self)))
    monkeypatch.setattr(FakeFirestore, "get_all", metered_get_all)
    return m


def _seed_page(db, rows: int = 200) -> None:
    """`rows` tasks: READY ones (a pool read), RUNNING ones holding a lease (a
    lease read) and finished ones -- every one with attempts to total."""
    seed_tenant(db, "eng")
    base = datetime.now(timezone.utc) - timedelta(hours=3)
    for i in range(rows):
        state = ("READY", "RUNNING", "SUCCEEDED")[i % 3]
        doc = seed_task(db, task_id=f"task_{i:03d}", tenant_id="eng", state=state,
                        created_at=base + timedelta(seconds=i))
        doc["submitted_by"] = "alice@example.com"
        if state == "RUNNING":
            doc["current_lease_id"] = f"lease_{i:03d}"
            doc["current_generation"] = 1
            db.docs[f"leases/lease_{i:03d}"] = {
                "lease_id": f"lease_{i:03d}", "tenant_id": "eng", "task_id": doc["id"],
                "generation": 1, "heartbeat_at": base, "released_at": None,
            }
        db.docs[f"attempts/att_{i:03d}"] = {
            "attempt_id": f"att_{i:03d}", "tenant_id": "eng", "task_id": doc["id"],
            "generation": 1, "created_at": base, "started_at": base,
            "completed_at": base + timedelta(seconds=30), "cost_usd": 0.25,
        }


def _list(client, **params):
    return client.get("/v1/tasks", headers=auth_header("alice"), params=params)


# --------------------------------------------------------------------------
# The enrichment reads overlap
# --------------------------------------------------------------------------

@pytest.mark.parametrize("view", ["summary", "full"])
def test_a_200_row_page_runs_its_reads_at_once(client, db, meter, view):
    _seed_page(db)
    meter.reads = meter.peak = 0
    response = _list(client, limit=200, view=view)
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["tasks"]) == 200
    # Every row was enriched exactly as before.
    assert all(row["attempts"] == 1 and row["cost_usd_total"] == 0.25 for row in body["tasks"])
    assert sum(1 for row in body["tasks"] if row["heartbeat_at"] is not None) == 67

    # N reads for a 200-row page: the page itself, the attempt totals in
    # ceil(200 / 30) = 7 queries, the leases in one `get_all` per 100, and the
    # READY rows' pools in one `get_all`. No read per row.
    paths = [path for path, _, _ in meter.projections]
    assert paths.count("tasks") == 1
    assert paths.count("attempts") == 7
    assert paths.count("get_all:leases") == 1
    assert paths.count("get_all:pools") <= 1
    enrichment = [p for p in paths if p in ("attempts", "get_all:leases", "get_all:pools")]
    assert len(enrichment) <= 9, paths
    # ...and they overlap: the request no longer waits for their sum.
    assert meter.peak >= 2, "the page's enrichment reads ran one after another"


# --------------------------------------------------------------------------
# view=summary reads only the summary fields
# --------------------------------------------------------------------------

def test_the_summary_projection_leaves_result_summary_on_the_server():
    assert "result_summary" not in TASK_SUMMARY_FIELDS
    assert set(TASK_SUMMARY_UNREAD).isdisjoint(TASK_SUMMARY_FIELDS)
    # The masker learns the secret literals from the input, so it is read.
    for needed in ("id", "tenant_id", "state", "created_at", "input", "metadata",
                   "last_error", "repository_url", "latest_checkpoint", "end_cause"):
        assert needed in TASK_SUMMARY_FIELDS


def test_a_summary_page_is_read_projected_and_a_full_page_is_not(client, db, meter):
    _seed_page(db, rows=5)
    assert _list(client, view="summary").status_code == 200
    summary = [p for path, p, _ in meter.projections if path == "tasks"]
    assert summary and all(p == TASK_SUMMARY_FIELDS for p in summary), summary
    meter.projections.clear()
    assert _list(client).status_code == 200
    full = [p for path, p, _ in meter.projections if path == "tasks"]
    assert full and all(p is None for p in full), full


def _seed_rich(db) -> str:
    """One task with every summary-served field set, and a secret its input names."""
    seed_tenant(db, "eng")
    leaked = "s" + secrets.token_hex(16)
    doc = seed_task(db, task_id="task_r", tenant_id="eng", state="FAILED")
    doc.update({
        "input": {"prompt": "deploy", "api_token": leaked},
        "metadata": {"title": "Deploy the thing", "ticket": "T-1"},
        "last_error": f"auth failed with {leaked}",
        "result_summary": {"summary": "x" * 5000},
        "latest_checkpoint": "gs://bucket/ckpt/1",
        "end_cause": "agent_failed",
        "attempt_count": 2,
        "current_generation": 2,
        "repository_url": "https://github.com/acme/repo",
        "spec_signature": "c2lnbmF0dXJl",
    })
    return leaked


def test_the_projected_summary_row_is_the_full_row_less_the_dropped_keys(client, db):
    _seed_rich(db)
    summary = _list(client, view="summary").json()["tasks"][0]
    full = _list(client).json()["tasks"][0]
    for key in ("input", "input_redaction_count", "result_summary",
                "result_summary_redaction_count"):
        assert key not in summary
    for key, value in full.items():
        if key in summary and key not in ("metadata", "metadata_redaction_count"):
            assert summary[key] == value, key
    assert set(full) - set(summary) == {"input", "input_redaction_count", "result_summary",
                                        "result_summary_redaction_count"}


def test_the_projected_summary_still_masks_what_the_input_names(client, db):
    leaked = _seed_rich(db)
    row = _list(client, view="summary").json()["tasks"][0]
    assert leaked not in row["last_error"]
    assert row["last_error_redaction_count"] == 1


# --------------------------------------------------------------------------
# state= takes several states in one query
# --------------------------------------------------------------------------

def _seed_states(db) -> None:
    seed_tenant(db, "eng")
    base = datetime.now(timezone.utc) - timedelta(hours=1)
    states = LIVE_AND_WAITING + ("PARKED", "SUCCEEDED", "FAILED", "CANCELLED")
    for i, state in enumerate(states):
        seed_task(db, task_id=f"task_{state.lower()}", tenant_id="eng", state=state,
                  created_at=base + timedelta(minutes=i))


@pytest.mark.parametrize("params", [
    [("state", s) for s in LIVE_AND_WAITING],
    [("state", ",".join(LIVE_AND_WAITING))],
    [("state", "QUEUED,READY"), ("state", "LEASED,DISPATCHED,STARTING"), ("state", "RUNNING")],
])
def test_six_states_are_one_request_and_one_query(client, db, meter, params):
    _seed_states(db)
    response = client.get("/v1/tasks", headers=auth_header("alice"),
                          params=params + [("view", "summary")])
    assert response.status_code == 200, response.text
    served = {row["state"] for row in response.json()["tasks"]}
    assert served == set(LIVE_AND_WAITING)
    pages = [f for path, _, f in meter.projections if path == "tasks"]
    assert len(pages) == 1
    assert ("state", "in", list(LIVE_AND_WAITING)) in pages[0]


def test_one_state_is_still_an_equality(client, db, meter):
    _seed_states(db)
    response = _list(client, state="RUNNING")
    assert [row["state"] for row in response.json()["tasks"]] == ["RUNNING"]
    pages = [f for path, _, f in meter.projections if path == "tasks"]
    assert ("state", "==", "RUNNING") in pages[0]


def test_a_repeated_state_is_read_once(client, db, meter):
    _seed_states(db)
    response = _list(client, state="RUNNING,RUNNING")
    assert [row["state"] for row in response.json()["tasks"]] == ["RUNNING"]
    pages = [f for path, _, f in meter.projections if path == "tasks"]
    assert ("state", "==", "RUNNING") in pages[0]


def test_an_unknown_state_in_the_list_is_a_422_naming_the_known_ones(client, db):
    _seed_states(db)
    response = client.get("/v1/tasks", headers=auth_header("alice"),
                          params=[("state", "RUNNING,SLEEPING")])
    assert response.status_code == 422, response.text
    assert "SLEEPING" in response.text and "QUEUED" in response.text


def test_the_state_list_cap_is_firestores_in_cap():
    assert STATE_IN_MAX == 30


def test_a_state_list_pages_with_the_owner_filter_after_the_cursor(client, db):
    _seed_states(db)
    db.docs["tasks/task_running"]["submitted_by"] = "alice@example.com"
    response = client.get("/v1/tasks", headers=auth_header("alice"),
                          params=[("state", "RUNNING,READY"), ("submitted_by", "me")])
    assert response.status_code == 200, response.text
    assert [row["id"] for row in response.json()["tasks"]] == ["task_running"]


# --------------------------------------------------------------------------
# The attempt totals' own chunks overlap too
# --------------------------------------------------------------------------

def test_a_200_task_totals_read_runs_its_seven_chunks_at_once(db, meter):
    from swarm_api.attempt_totals import totals_for

    _seed_page(db)
    meter.reads = meter.peak = 0
    totals = totals_for(db, "eng", [f"task_{i:03d}" for i in range(200)])
    assert meter.reads == 7
    assert meter.peak >= 2, "the attempt-total chunks ran one after another"
    assert all(t["attempts"] == 1 and t["attempts_read"] == "ok" for t in totals.values())
    assert len(totals) == 200


def test_one_failed_totals_chunk_marks_only_its_own_tasks_unread(db, monkeypatch):
    from swarm_api.attempt_totals import IN_LIMIT, totals_for

    _seed_page(db, rows=60)
    stream = FakeQuery.stream

    def flaky(self, *args, **kwargs):
        if self._path == "attempts" and "task_000" in dict((f, v) for f, _, v in self._filters)["task_id"]:
            raise RuntimeError("unavailable")
        return stream(self, *args, **kwargs)

    monkeypatch.setattr(FakeQuery, "stream", flaky)
    ids = [f"task_{i:03d}" for i in range(60)]
    totals = totals_for(db, "eng", ids)
    assert [totals[t]["attempts_read"] for t in ids[:IN_LIMIT]] == ["failed"] * IN_LIMIT
    assert [totals[t]["attempts_read"] for t in ids[IN_LIMIT:]] == ["ok"] * (60 - IN_LIMIT)


class _EventsDb(FakeFirestore):
    """The fake with `collection_group("events")` over top-level `events/*`."""

    def collection_group(self, path: str) -> FakeQuery:
        return FakeQuery(self, path)


def _seed_account_events(db, ids) -> None:
    at = datetime.now(timezone.utc)
    for i, task_id in enumerate(ids):
        db.docs[f"events/ev_{i:03d}"] = {
            "event_id": f"ev_{i:03d}", "tenant_id": "eng", "task_id": task_id, "at": at,
            "generation": 1, "attempt_id": f"att_{i:03d}",
            "detail": {"cause": "account_assigned", "account_id": f"acct_{i % 3}",
                       "provider": "anthropic"},
        }


def test_the_account_reads_chunks_run_at_once_and_fold_in_order(meter):
    from swarm_api.task_accounts import read_account_events

    db = _EventsDb()
    ids = [f"task_{i:03d}" for i in range(90)]
    _seed_account_events(db, ids)
    metered: list[int] = []
    events, complete = read_account_events(db, "eng", ids, meter=metered.append)
    assert meter.reads == 3
    assert meter.peak >= 2, "the account-event chunks ran one after another"
    assert metered == [30, 30, 30]
    assert complete == set(ids)
    assert all(len(events[t]) == 1 for t in ids)


def test_a_strict_account_read_still_raises_a_failed_chunk(monkeypatch):
    from swarm_api.task_accounts import read_account_events

    db = _EventsDb()
    ids = [f"task_{i:03d}" for i in range(60)]
    _seed_account_events(db, ids)
    stream = FakeQuery.stream

    def flaky(self, *args, **kwargs):
        if "task_059" in dict((f, v) for f, _, v in self._filters)["task_id"]:
            raise RuntimeError("unavailable")
        return stream(self, *args, **kwargs)

    monkeypatch.setattr(FakeQuery, "stream", flaky)
    with pytest.raises(RuntimeError):
        read_account_events(db, "eng", ids, strict=True)
    events, complete = read_account_events(db, "eng", ids)
    assert complete == set(ids[:30])
