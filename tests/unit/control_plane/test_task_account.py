"""Which subscription ACCOUNT an agent runs on, read off its own events (#379).

The worker records the account it leased as a `running` event with
`detail.cause == "account_assigned"`, once per attempt, and a swap as a
`retrying` event with `detail.cause == "account_unreadable"`
(`agent_worker.lifecycle._lease_account` / `_reject_account`). The API derives
`task.account` from those events at read time (`swarm_api.task_accounts`).

What these tests hold:

  * the derivation -- assigned, swapped, none yet, a profile that calls no
    model, a profile that never asks the pool;
  * only the task's OWN events, in its OWN tenant, are read;
  * the list route reads with ONE bounded collection-group query per 30 rows
    (Firestore's `in` cap) -- two for 35 rows and two for 50 -- never per row;
  * a FINISHED task's account is cached per process; a rerun is read afresh,
    and a failed or cut-short read is never cached;
  * the composite index the query needs is declared in Terraform;
  * no credential-shaped value in an event's detail ever reaches the payload.

Offline: FakeFirestore with a collection-group query added here.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from google.api_core import exceptions as gexc

from swarm_api import task_accounts

from .conftest import auth_header, seed_task, seed_tenant
from .fakes import IN_MAX_VALUES, FakeFirestore, FakeQuery, FakeSnapshot, FakeDocumentRef, _compare, _field

REPO = Path(__file__).resolve().parents[3]
INDEXES_TF = REPO / "terraform/modules/firestore/indexes.tf"

T0 = datetime(2026, 9, 30, 9, 0, tzinfo=timezone.utc)


def _shape(*parts: str) -> str:
    """Join fragments at runtime, so no credential-shaped literal is in this file.

    The same helper as test_log_redaction.py, for the same reason: the worker
    refuses to publish a tree whose diff contains a string the redaction rules
    match, and so does the repository's own secret scan.
    """
    return "".join(parts)


# --------------------------------------------------------------------------
# A collection-group query for the fake, recording every query it runs
# --------------------------------------------------------------------------

class _GroupQuery(FakeQuery):
    """`Client.collection_group(name)`: every collection called `name`, at any depth."""

    def _with(self, **kwargs: Any) -> "_GroupQuery":
        return _GroupQuery(
            self._db,
            self._path,
            filters=kwargs.get("filters", self._filters),
            orders=kwargs.get("orders", self._orders),
            limit_n=kwargs.get("limit_n", self._limit),
        )

    def _rows(self):
        db = self._db
        db.group_queries.append({"collection": self._path, "filters": self._filters, "limit": self._limit})
        if db.fail_next_group_query:
            db.fail_next_group_query -= 1
            raise gexc.ServiceUnavailable("injected group query failure")
        disjunctions = 1
        for _field_path, op, value in self._filters:
            if op == "in":
                if len(value) > IN_MAX_VALUES:
                    raise gexc.InvalidArgument(f"'IN' supports up to {IN_MAX_VALUES} comparison values.")
                disjunctions *= len(value)
        # Firestore's cap on a query's disjunctive normal form: the product of
        # its `in` sizes. A range filter adds no disjunction.
        if disjunctions > IN_MAX_VALUES:
            raise gexc.InvalidArgument("query has too many disjunctions")
        matches = []
        for path, data in db.docs.items():
            parts = path.split("/")
            if len(parts) < 2 or parts[-2] != self._path:
                continue
            if all(_compare(_field(data, f), op, v) for f, op, v in self._filters):
                matches.append((path, data))
        rows = [
            FakeSnapshot(id=p.rsplit("/", 1)[1], path=p, _data=dict(d), reference=FakeDocumentRef(db, p))
            for p, d in matches
        ]
        if self._limit is not None:
            rows = rows[: self._limit]
        return iter(rows)


class GroupFirestore(FakeFirestore):
    def __init__(self) -> None:
        super().__init__()
        self.group_queries: list[dict[str, Any]] = []
        self.fail_next_group_query = 0

    def collection_group(self, name: str) -> _GroupQuery:
        return _GroupQuery(self, name)


@pytest.fixture
def db() -> GroupFirestore:
    return GroupFirestore()


@pytest.fixture(autouse=True)
def _fresh_cache():
    task_accounts.FINISHED.clear()
    yield
    task_accounts.FINISHED.clear()


def _running_task(db, task_id: str, *, tenant: str = "eng", state: str = "RUNNING",
                  profile: str = "claude-code", generation: int = 1, **over: Any) -> dict:
    doc = seed_task(db, task_id=task_id, tenant_id=tenant, state=state,
                    runner_profile=profile, provider="anthropic" if profile != "mock" else None)
    doc.update(
        current_generation=generation,
        attempt_count=generation,
        started_at=T0,
        completed_at=T0 + timedelta(minutes=5) if state in {"SUCCEEDED", "FAILED", "CANCELLED"} else None,
        **over,
    )
    return doc


_SEQ = iter(range(1, 10_000_000))


def _event(db, task_id: str, cause: str, account_id: str, *, tenant: str = "eng",
           generation: int = 1, attempt_id: str | None = None, minute: float = 0,
           extra: dict | None = None) -> str:
    n = next(_SEQ)
    event_id = f"ev_{n:020d}"
    kind = "running" if cause == "account_assigned" else "retrying"
    detail = {"cause": cause, "account_id": account_id, "provider": "anthropic", **(extra or {})}
    db.docs[f"tasks/{task_id}/events/{event_id}"] = {
        "event_id": event_id,
        "task_id": task_id,
        "tenant_id": tenant,
        "type": kind,
        "at": T0 + timedelta(minutes=minute),
        "attempt_id": attempt_id or f"att_{task_id}_{generation}",
        "lease_id": f"lease_{task_id}_{generation}",
        "generation": generation,
        "detail": detail,
    }
    return event_id


def _get(client, task_id: str, user: str = "alice") -> dict:
    r = client.get(f"/v1/tasks/{task_id}", headers=auth_header(user))
    assert r.status_code == 200, r.text
    return r.json()["task"]


# --------------------------------------------------------------------------
# The derivation
# --------------------------------------------------------------------------

def test_an_assigned_account_is_served_for_the_latest_attempt(client, db):
    seed_tenant(db, "eng")
    _running_task(db, "task_a", generation=2)
    _event(db, "task_a", "account_assigned", "acct-eng-01", generation=1, minute=0)
    _event(db, "task_a", "account_assigned", "acct-eng-03", generation=2, minute=10)

    account = _get(client, "task_a")["account"]

    assert account["status"] == "assigned"
    assert account["account_id"] == "acct-eng-03"
    assert account["generation"] == 2
    assert account["attempt_id"] == "att_task_a_2"
    assert account["provider"] == "anthropic"
    assert account["swapped_from"] is None
    assert account["swaps"] == []


def test_a_swap_names_the_account_it_left_and_why(client, db):
    seed_tenant(db, "eng")
    _running_task(db, "task_s")
    _event(db, "task_s", "account_assigned", "acct-eng-01", minute=0)
    _event(db, "task_s", "account_unreadable", "acct-eng-01", minute=1)
    _event(db, "task_s", "account_assigned", "acct-eng-02", minute=2)

    account = _get(client, "task_s")["account"]

    assert account["status"] == "assigned"
    assert account["account_id"] == "acct-eng-02"
    assert account["swapped_from"] == "acct-eng-01"
    assert account["swaps"] == [{"account_id": "acct-eng-01", "cause": "unreadable"}]


def test_a_live_attempt_with_no_assignment_yet_says_so_and_names_no_older_account(client, db):
    seed_tenant(db, "eng")
    # Attempt 1 ran on acct-eng-01; attempt 2 has not been assigned anything.
    _running_task(db, "task_n", state="STARTING", generation=2)
    _event(db, "task_n", "account_assigned", "acct-eng-01", generation=1)

    account = _get(client, "task_n")["account"]

    assert account["status"] == "not_assigned_yet"
    assert account["account_id"] is None


def test_a_finished_task_that_never_took_an_account_reads_not_assigned(client, db):
    seed_tenant(db, "eng")
    _running_task(db, "task_f", state="CANCELLED")

    account = _get(client, "task_f")["account"]

    assert account["status"] == "not_assigned"
    assert account["account_id"] is None


def test_every_offered_account_unreadable_leaves_no_account_and_lists_the_swaps(client, db):
    seed_tenant(db, "eng")
    _running_task(db, "task_u", state="PARKED")
    _event(db, "task_u", "account_assigned", "acct-eng-01", minute=0)
    _event(db, "task_u", "account_unreadable", "acct-eng-01", minute=1)

    account = _get(client, "task_u")["account"]

    assert account["status"] == "not_assigned_yet"
    assert account["account_id"] is None
    assert account["swaps"] == [{"account_id": "acct-eng-01", "cause": "unreadable"}]


def test_a_profile_that_calls_no_model_says_so_without_reading_events(client, db):
    seed_tenant(db, "eng")
    _running_task(db, "task_m", profile="mock")

    account = _get(client, "task_m")["account"]

    assert account["status"] == "no_model_call"
    assert account["account_id"] is None
    assert db.group_queries == []


def test_a_profile_that_takes_no_subscription_is_not_on_the_pool(client, db):
    seed_tenant(db, "eng")
    _running_task(db, "task_b", profile="browser")

    account = _get(client, "task_b")["account"]

    assert account["status"] == "not_pooled"
    assert db.group_queries == []


def test_a_task_never_admitted_is_not_assigned_yet_and_costs_no_read(client, db):
    seed_tenant(db, "eng")
    seed_task(db, task_id="task_q", tenant_id="eng", state="READY", runner_profile="claude-code",
              provider="anthropic")

    account = _get(client, "task_q")["account"]

    assert account["status"] == "not_assigned_yet"
    assert db.group_queries == []


def test_only_the_tasks_own_events_in_its_own_tenant_are_read(client, db):
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    _running_task(db, "task_own")
    _running_task(db, "task_other")
    # The same task id, another tenant's event: never read.
    _event(db, "task_own", "account_assigned", "acct-research-09", tenant="research", minute=5)
    # Another task of the same tenant: never read into this one.
    _event(db, "task_other", "account_assigned", "acct-eng-07", minute=6)
    _event(db, "task_own", "account_assigned", "acct-eng-01", minute=0)

    account = _get(client, "task_own")["account"]

    assert account["account_id"] == "acct-eng-01"
    [query] = db.group_queries
    filters = {(f, op): v for f, op, v in query["filters"]}
    assert filters[("tenant_id", "==")] == "eng"
    assert filters[("task_id", "in")] == ["task_own"]


def test_no_credential_shaped_detail_reaches_the_payload(client, db):
    seed_tenant(db, "eng")
    _running_task(db, "task_c")
    planted = _shape("sk-", "ant-", "api03-", "Q" * 40)
    _event(db, "task_c", "account_assigned", "acct-eng-01",
           extra={_shape("sec", "ret"): planted, "token_name": planted})

    body = client.get("/v1/tasks/task_c", headers=auth_header("alice")).text

    assert planted not in body
    account = _get(client, "task_c")["account"]
    assert set(account) == {
        "status", "account_id", "provider", "attempt_id", "generation", "swapped_from", "swaps",
    }


# --------------------------------------------------------------------------
# The list route: one bounded query per 30 rows
# --------------------------------------------------------------------------

def _seed_page(db, n: int) -> None:
    seed_tenant(db, "eng")
    for i in range(n):
        tid = f"task_{i:03d}"
        _running_task(db, tid)
        db.docs[f"tasks/{tid}"]["created_at"] = T0 + timedelta(seconds=i)
        _event(db, tid, "account_assigned", f"acct-eng-{i % 4:02d}")


def _list(client, limit: int) -> list[dict]:
    r = client.get(f"/v1/tasks?limit={limit}", headers=auth_header("alice"))
    assert r.status_code == 200, r.text
    return r.json()["tasks"]


def _assert_bounded(query: dict, ids: int) -> None:
    filters = {(f, op): v for f, op, v in query["filters"]}
    assert query["collection"] == "events"
    assert filters[("tenant_id", "==")] == "eng"
    assert len(filters[("task_id", "in")]) == ids
    assert filters[("detail.cause", ">=")] == "account_"
    assert ("detail.cause", "<") in filters
    assert query["limit"] == task_accounts.PER_TASK_CAP * ids


def test_a_page_of_twenty_is_one_bounded_query(client, db):
    _seed_page(db, 20)

    tasks = _list(client, 20)

    assert len(tasks) == 20
    assert all(t["account"]["status"] == "assigned" for t in tasks)
    [query] = db.group_queries
    _assert_bounded(query, 20)


def test_thirty_five_rows_are_two_queries(client, db):
    _seed_page(db, 35)

    tasks = _list(client, 35)

    assert len(tasks) == 35
    assert all(t["account"]["status"] == "assigned" for t in tasks)
    assert len(db.group_queries) == 2
    sizes = sorted(len(dict(((f, op), v) for f, op, v in q["filters"])[("task_id", "in")])
                   for q in db.group_queries)
    assert sizes == [5, 30]
    for q in db.group_queries:
        _assert_bounded(q, len(dict(((f, op), v) for f, op, v in q["filters"])[("task_id", "in")]))


def test_fifty_rows_are_at_most_two_queries(client, db):
    _seed_page(db, 50)

    tasks = _list(client, 50)

    assert len(tasks) == 50
    assert len(db.group_queries) == 2
    by_id = {t["id"]: t["account"]["account_id"] for t in tasks}
    assert by_id["task_007"] == "acct-eng-03"


# --------------------------------------------------------------------------
# The cache
# --------------------------------------------------------------------------

def test_a_finished_tasks_account_is_read_once_per_process(client, db):
    seed_tenant(db, "eng")
    _running_task(db, "task_done", state="SUCCEEDED")
    _event(db, "task_done", "account_assigned", "acct-eng-01")

    assert _get(client, "task_done")["account"]["account_id"] == "acct-eng-01"
    assert _get(client, "task_done")["account"]["account_id"] == "acct-eng-01"

    assert len(db.group_queries) == 1


def test_a_live_tasks_account_is_read_on_every_request(client, db):
    seed_tenant(db, "eng")
    _running_task(db, "task_live")
    _event(db, "task_live", "account_assigned", "acct-eng-01")

    _get(client, "task_live")
    _get(client, "task_live")

    assert len(db.group_queries) == 2


def test_a_rerun_task_is_read_afresh(client, db):
    seed_tenant(db, "eng")
    _running_task(db, "task_rr", state="FAILED")
    _event(db, "task_rr", "account_assigned", "acct-eng-01", generation=1)
    assert _get(client, "task_rr")["account"]["account_id"] == "acct-eng-01"

    # Run again: a new generation, on a new account, and finished again.
    db.docs["tasks/task_rr"].update(current_generation=2, attempt_count=2, state="SUCCEEDED",
                                    completed_at=T0 + timedelta(hours=1))
    _event(db, "task_rr", "account_assigned", "acct-eng-02", generation=2, minute=30)

    assert _get(client, "task_rr")["account"]["account_id"] == "acct-eng-02"
    assert len(db.group_queries) == 2


def test_a_failed_read_says_unread_and_is_not_cached(client, db):
    seed_tenant(db, "eng")
    _running_task(db, "task_fr", state="SUCCEEDED")
    _event(db, "task_fr", "account_assigned", "acct-eng-01")
    db.fail_next_group_query = 1

    first = _get(client, "task_fr")["account"]
    second = _get(client, "task_fr")["account"]

    assert first["status"] == "unread"
    assert first["account_id"] is None
    assert second["status"] == "assigned"
    assert len(db.group_queries) == 2


def test_a_cut_short_read_says_unread_and_is_not_cached(client, db):
    seed_tenant(db, "eng")
    _running_task(db, "task_cs", state="SUCCEEDED")
    for i in range(task_accounts.PER_TASK_CAP + 1):
        _event(db, "task_cs", "account_assigned", f"acct-eng-{i:02d}", minute=i)

    first = _get(client, "task_cs")["account"]
    _get(client, "task_cs")

    assert first["status"] == "unread"
    assert len(db.group_queries) == 2


# --------------------------------------------------------------------------
# The other routes that serve a task
# --------------------------------------------------------------------------

def test_the_cancel_route_returns_the_account(client, db):
    seed_tenant(db, "eng")
    _running_task(db, "task_cx")
    _event(db, "task_cx", "account_assigned", "acct-eng-05")

    r = client.post("/v1/tasks/task_cx/cancel", headers=auth_header("alice"))

    assert r.status_code == 200, r.text
    assert r.json()["task"]["account"]["account_id"] == "acct-eng-05"


def test_the_workflow_detail_route_returns_each_steps_account(client, db):
    seed_tenant(db, "eng")
    db.docs["workflows/wf_1"] = {
        "workflow_id": "wf_1",
        "tenant_id": "eng",
        "created_at": T0,
        "updated_at": T0,
        "state": "RUNNING",
        "submitted_by": "alice@saga.xyz",
        "steps": [
            {"step_id": "a", "runner_profile": "claude-code", "resource_class": "standard",
             "input": {}, "depends_on": [], "task_id": "task_w1"},
            {"step_id": "b", "runner_profile": "mock", "resource_class": "standard",
             "input": {}, "depends_on": ["a"], "task_id": "task_w2"},
        ],
    }
    _running_task(db, "task_w1", workflow_id="wf_1", step_id="a")
    _running_task(db, "task_w2", profile="mock", state="READY", generation=0,
                  workflow_id="wf_1", step_id="b")
    _event(db, "task_w1", "account_assigned", "acct-eng-02")

    r = client.get("/v1/workflows/wf_1", headers=auth_header("alice"))

    assert r.status_code == 200, r.text
    accounts = {t["id"]: t["account"] for t in r.json()["tasks"]}
    assert accounts["task_w1"]["account_id"] == "acct-eng-02"
    assert accounts["task_w2"]["status"] == "no_model_call"
    assert len(db.group_queries) == 1


def test_each_attempt_carries_its_own_account_and_swaps(client, db):
    seed_tenant(db, "eng")
    _running_task(db, "task_at", generation=2)
    for gen in (1, 2):
        db.docs[f"attempts/att_task_at_{gen}"] = {
            "attempt_id": f"att_task_at_{gen}",
            "task_id": "task_at",
            "tenant_id": "eng",
            "generation": gen,
            "lease_id": f"lease_task_at_{gen}",
            "backend": "CLOUD_RUN_JOB",
            "created_at": T0 + timedelta(minutes=gen),
            "started_at": T0 + timedelta(minutes=gen),
            "completed_at": T0 + timedelta(minutes=gen + 1) if gen == 1 else None,
        }
    _event(db, "task_at", "account_assigned", "acct-eng-01", generation=1, minute=0)
    _event(db, "task_at", "account_unreadable", "acct-eng-01", generation=1, minute=1)
    _event(db, "task_at", "account_assigned", "acct-eng-02", generation=1, minute=2)
    _event(db, "task_at", "account_assigned", "acct-eng-03", generation=2, minute=20)

    r = client.get("/v1/tasks/task_at/attempts", headers=auth_header("alice"))

    assert r.status_code == 200, r.text
    body = r.json()
    by_gen = {a["generation"]: body["accounts_by_attempt"][a["attempt_id"]] for a in body["attempts"]}
    assert by_gen[1]["account_id"] == "acct-eng-02"
    assert by_gen[1]["swapped_from"] == "acct-eng-01"
    assert by_gen[1]["swaps"] == [{"account_id": "acct-eng-01", "cause": "unreadable"}]
    assert by_gen[2]["account_id"] == "acct-eng-03"
    assert by_gen[2]["swaps"] == []


# --------------------------------------------------------------------------
# The index the query needs
# --------------------------------------------------------------------------

def test_the_composite_index_the_account_query_needs_is_declared():
    text = INDEXES_TF.read_text()
    match = re.search(r'"events-tenant-task-cause"\s*=\s*\{(.*?)\n    \}', text, re.S)
    assert match, "events-tenant-task-cause is not declared in indexes.tf"
    block = match.group(1)
    assert 'collection  = "events"' in block
    assert 'query_scope = "COLLECTION_GROUP"' in block
    fields = re.findall(r'field_path = "([^"]+)"', block)
    # Equality, then the `in`, then the RANGE field last: Firestore serves an
    # inequality only from an index whose last field it is.
    assert fields == ["tenant_id", "task_id", "detail.cause"]
