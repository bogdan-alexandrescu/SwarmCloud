"""How much one `GET /v1/notifications` costs (lane LOAD-SPEED-NOTIFICATIONS,
part of #2688; the owner's pick of 2026-11-01).

The feed ran its twelve sources one after another: 17-20 serial Firestore
round trips, up to ~800 task documents for a member, 6.2 s cold for an admin
(measured in the owner's browser). What is held here, each against the
in-memory Firestore, with no cloud credentials:

* the sources run CONCURRENTLY, and one that fails still fails alone;
* PARKED tasks are read ONCE per request and shared by the hand-off and
  waiting-step sources;
* every task read of the feed is one query, with the submitter IN the query
  when it narrows to one person (index tasks-tenant-submitter-state-created),
  projected to the fields the items use (never the prompt);
* the admin's platform-secrets count never holds the feed for Secret Manager:
  a stale count is served while one background read refreshes it, and the
  rows of that read are fetched concurrently.
"""

from __future__ import annotations

import dataclasses
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from swarm_api import notifications as notif
from swarm_api import platform_secrets as ps
from swarm_api.store import Store

from .conftest import seed_task, seed_tenant
from .fakes import FakeQuery

ALICE = "alice@example.com"
BOB = "bob@example.com"
ROOT = "root@example.com"


def _now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _tenants(db):
    seed_tenant(db, "eng")


@pytest.fixture
def task_queries(monkeypatch) -> list[tuple[tuple[str, str, Any], ...]]:
    """Every query the fake runs on `tasks`, by its filters: one entry per
    round trip."""
    seen: list[tuple[tuple[str, str, Any], ...]] = []
    lock = threading.Lock()
    rows = FakeQuery._rows

    def counted(self):
        if self._path == "tasks":
            with lock:
                seen.append(self._filters)
        return rows(self)

    monkeypatch.setattr(FakeQuery, "_rows", counted)
    return seen


@pytest.fixture
def projections(monkeypatch) -> list[tuple[str, ...]]:
    """`Query.select`, which the in-memory Firestore lacks: recorded, and
    applied as Firestore applies it (only the named fields come back)."""
    asked: list[tuple[str, ...]] = []
    lock = threading.Lock()
    rows = FakeQuery._rows

    def select(self, field_paths):
        fields = tuple(field_paths)
        with lock:
            asked.append(fields)
        query = self._with()
        query._projection = fields
        return query

    def projected(self):
        fields = getattr(self, "_projection", None)
        for snap in rows(self):
            if fields is not None:
                snap = dataclasses.replace(
                    snap, _data={k: v for k, v in snap._data.items() if k in fields})
            yield snap

    monkeypatch.setattr(FakeQuery, "select", select, raising=False)
    monkeypatch.setattr(FakeQuery, "_rows", projected)
    return asked


def _task(db, task_id: str, *, by: str = ALICE, state: str = "SUCCEEDED", **extra: Any):
    doc = seed_task(db, task_id=task_id, tenant_id="eng", state=state,
                    created_at=_now() - timedelta(minutes=5))
    doc["submitted_by"] = by
    if state in ("SUCCEEDED", "FAILED"):
        doc["completed_at"] = doc["updated_at"] = _now() - timedelta(minutes=1)
    doc.update(extra)
    return doc


def _await_step(db, task_id: str, *, by: str = ALICE) -> None:
    opened = _now() - timedelta(minutes=5)
    _task(db, task_id, by=by, state="PARKED", park_reason="AWAITING_INPUT",
          next_eligible_at=opened, workflow_id="wf_review", step_id="review",
          input={"prompt": "Read draft.md and approve it."},
          metadata={"dispatch": {"await_input": {"form": "verdict", "remind": [by],
                                                 "remind_after_seconds": 3600}},
                    "await_input": {"opened_at": opened,
                                    "expires_at": opened + timedelta(days=1), "answer": None}})


def _seed_parked(db) -> None:
    _await_step(db, "t_review")
    _task(db, "t_browser", state="PARKED", park_reason="HUMAN_REQUIRED", current_generation=2)
    _task(db, "t_bobs", by=BOB, state="PARKED", park_reason="HUMAN_REQUIRED")


def _parked_reads(queries) -> list[tuple[tuple[str, str, Any], ...]]:
    return [q for q in queries if ("state", "==", "PARKED") in q]


# --------------------------------------------------------------------------
# PARKED is read once, and its items are the same
# --------------------------------------------------------------------------

def test_parked_tasks_are_read_once_per_request(db, task_queries):
    _seed_parked(db)
    feed = notif.Notifications(db, Store(db), now=_now).feed(
        notif.Viewer(email=ALICE, tenant_id="eng"))
    ids = {i["id"] for i in feed["items"]}
    assert "task:handoff:t_browser:2" in ids
    assert "task:input:t_review:0" in ids
    assert not any("t_bobs" in i for i in ids)
    # MUTATION: let handoffs and awaiting_input each read PARKED -> 2.
    assert len(_parked_reads(task_queries)) == 1


def test_a_member_scoped_readers_task_reads_name_the_submitter_in_the_query(db, task_queries):
    _seed_parked(db)
    _task(db, "t_running", state="RUNNING")
    viewer = notif.Viewer(email=ALICE, tenant_id="eng", member_scope=True)
    feed = notif.Notifications(db, Store(db), now=_now).feed(viewer)
    assert {"task:handoff:t_browser:2", "task:input:t_review:0"} <= {i["id"] for i in feed["items"]}
    # Every task read of the feed narrows to the person IN the query, so a
    # page of 200 is 200 of theirs, never 200 of the tenant's filtered after.
    assert task_queries
    for filters in task_queries:
        assert ("submitted_by", "==", ALICE) in filters, filters


def test_the_feeds_task_reads_are_one_query_each(db, task_queries):
    """Own tasks, RUNNING and PARKED: three task queries for a whole feed, and
    no second read at a full window's edge (the paged listing's run read)."""
    for n in range(notif.SCAN + 5):
        _task(db, f"t_{n:03d}")
    _seed_parked(db)
    notif.Notifications(db, Store(db), now=_now).feed(notif.Viewer(email=ALICE, tenant_id="eng"))
    # MUTATION: read PARKED twice, or page own tasks through the keyset
    # listing (its run read at the window's edge) -> more than 3.
    assert len(task_queries) == 3, task_queries


def test_the_feeds_task_reads_project_only_the_fields_items_use(db, projections):
    _seed_parked(db)
    _task(db, "t_done", input={"prompt": "x" * 10_000})
    feed = notif.Notifications(db, Store(db), now=_now).feed(
        notif.Viewer(email=ALICE, tenant_id="eng"))
    ids = {i["id"] for i in feed["items"]}
    assert {"task:handoff:t_browser:2", "task:input:t_review:0",
            "finished:task:t_done:SUCCEEDED"} <= ids
    # MUTATION: read whole task documents -> no projection asked.
    assert projections, "the feed read whole task documents"
    for fields in projections:
        assert "input" not in fields
        assert {"id", "tenant_id", "state", "submitted_by", "metadata"} <= set(fields)


# --------------------------------------------------------------------------
# the sources run concurrently, and each fails alone
# --------------------------------------------------------------------------

def test_sources_run_concurrently_and_one_failing_leaves_the_others_items(db):
    meet = threading.Barrier(2, timeout=5)

    def first(reading):
        meet.wait()
        return [notif._item("one", notif.TASK, True, "one", None, "/", _now(), topic="question")]

    def second(reading):
        meet.wait()
        return [notif._item("two", notif.TASK, True, "two", None, "/", _now(), topic="question")]

    def broken(reading):
        raise RuntimeError("firestore said no")

    service = notif.Notifications(db, Store(db), now=_now,
                                  sources=(("first", first), ("broken", broken),
                                           ("second", second)))
    # MUTATION: run the sources one after another -> the barrier never
    # fills, both time out, and neither item is served.
    feed = service.feed(notif.Viewer(email=ALICE, tenant_id="eng"))
    assert {i["id"] for i in feed["items"]} == {"one", "two"}
    assert feed["partial"] is True


def test_a_failed_shared_read_fails_each_source_that_needs_it_and_is_tried_once(db):
    calls = []

    class Store_(Store):
        def notification_tasks(self, *args, **kwargs):
            calls.append(kwargs.get("state"))
            raise RuntimeError("firestore said no")

    _task(db, "t_done")
    service = notif.Notifications(db, Store_(db), now=_now)
    feed = service.feed(notif.Viewer(email=ALICE, tenant_id="eng"))
    assert feed["partial"] is True
    # One attempt per distinct read, never one per source that shares it.
    assert sorted(map(str, calls)) == sorted(map(str, set(calls)))


# --------------------------------------------------------------------------
# the platform-secrets count never holds the feed
# --------------------------------------------------------------------------

class _SlowMeta:
    def __init__(self, versions: dict[str, list]) -> None:
        self.versions = versions
        self.gate = threading.Event()
        self.gate.set()

    def enabled_versions(self, secret_id: str):
        assert self.gate.wait(10)
        return self.versions.get(secret_id, [_now()])


@pytest.fixture
def catalogue(monkeypatch):
    monkeypatch.setattr(notif, "PLATFORM_SECRET_CATALOGUE", tuple(
        ps.PlatformSecret(i, ps.TERRAFORM, "scripts/create-secrets.sh")
        for i in ("swarm-a-key", "swarm-b-key")))


def test_a_stale_platform_secrets_count_is_served_while_it_refreshes_in_the_background(
        db, catalogue):
    meta = _SlowMeta({"swarm-a-key": []})
    clock = [_now()]
    service = notif.Notifications(db, Store(db), now=lambda: clock[0], secret_metadata=meta)
    admin = notif.Viewer(email=ROOT, tenant_id="eng", is_admin=True)
    assert service.feed(admin)["platform_secrets_unset"] == 1

    clock[0] += notif.PLATFORM_SECRETS_TTL + timedelta(seconds=1)
    meta.versions["swarm-b-key"] = []
    meta.gate.clear()  # Secret Manager is slow now
    served: list[Any] = []
    reader = threading.Thread(target=lambda: served.append(service.feed(admin)))
    reader.start()
    reader.join(3)
    # MUTATION: read the count inline once it is stale -> the feed waits for
    # Secret Manager and nothing is served within the 3 s.
    alive = reader.is_alive()
    meta.gate.set()
    reader.join(10)
    assert not alive, "the feed waited for Secret Manager"
    assert served[0]["platform_secrets_unset"] == 1  # the last count, while it refreshes
    assert notif.wait_platform_secrets_refresh(meta, timeout=10)
    assert service.feed(admin)["platform_secrets_unset"] == 2


def test_the_platform_secret_rows_are_read_concurrently(db):
    meet = threading.Barrier(2, timeout=5)

    class Meta:
        def enabled_versions(self, secret_id):
            meet.wait()
            return []

    catalogue = tuple(ps.PlatformSecret(i, ps.TERRAFORM, "scripts/create-secrets.sh")
                      for i in ("swarm-a-key", "swarm-b-key"))
    started = time.monotonic()
    status = ps.platform_secret_status(Meta(), db, catalogue=catalogue)
    # MUTATION: read the rows one after another -> the barrier breaks and
    # both rows raise BrokenBarrierError out of the status read.
    assert [r["secret_id"] for r in status["secrets"]] == ["swarm-a-key", "swarm-b-key"]
    assert [r["status"] for r in status["secrets"]] == [ps.NOT_SET, ps.NOT_SET]
    assert time.monotonic() - started < 5


# --------------------------------------------------------------------------
# the index the in-query submitter filter needs
# --------------------------------------------------------------------------

def test_the_submitter_state_index_is_declared():
    repo = Path(__file__).resolve().parents[3]
    text = (repo / "terraform/modules/firestore/indexes.tf").read_text()
    match = re.search(r'"tasks-tenant-submitter-state-created"\s*=\s*\{(.*?)\n    \}', text, re.S)
    assert match, "tasks-tenant-submitter-state-created is not declared in indexes.tf"
    block = match.group(1)
    assert 'collection  = "tasks"' in block
    assert 'query_scope = "COLLECTION"' in block
    assert re.findall(r'field_path = "([^"]+)", order = "([A-Z]+)"', block) == [
        ("tenant_id", "ASCENDING"), ("submitted_by", "ASCENDING"),
        ("state", "ASCENDING"), ("created_at", "DESCENDING"),
    ]


def test_until_the_index_is_built_the_submitter_is_filtered_after_the_read(db, monkeypatch):
    """Firestore refuses a query whose composite index is still building
    (FailedPrecondition). A deploy that lands before the index serves the
    member's bell from the tenant-wide read, narrowed after, never partial."""
    from google.api_core import exceptions as gexc

    rows = FakeQuery._rows

    def building(self):
        names = {f for f, _op, _v in self._filters}
        if self._path == "tasks" and {"submitted_by", "state"} <= names:
            raise gexc.FailedPrecondition("The query requires an index.")
        return rows(self)

    monkeypatch.setattr(FakeQuery, "_rows", building)
    _seed_parked(db)
    viewer = notif.Viewer(email=ALICE, tenant_id="eng", member_scope=True)
    feed = notif.Notifications(db, Store(db), now=_now).feed(viewer)
    # MUTATION: let FailedPrecondition through -> both sources unread, partial.
    assert feed["partial"] is False
    ids = {i["id"] for i in feed["items"]}
    assert {"task:handoff:t_browser:2", "task:input:t_review:0"} <= ids
    assert not any("t_bobs" in i for i in ids)
