"""`POST /v1/admin/history:purge`: the audited purge of failed history.

Owner request 2026-10-11 (swarm_api/purge.py). Every test runs the shipped
route over the in-memory Firestore and an in-memory bucket; nothing here
needs credentials. Each one fails without the purge: the route did not exist,
so every request below was a 404.
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from swarm_api.auth import POOL_ADMIN_ROUTES, StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.groups import StaticGroups
from swarm_api.main import create_app
from swarm_api.metrics import ApiMetrics
from swarm_api.purge import PurgeFailed
from swarm_api.waker import NullWaker

from .conftest import NoForgeTokens, api_settings, auth_header, seed_pool, seed_task, seed_tenant

ROUTE = "/v1/admin/history:purge"
OLD = datetime.now(timezone.utc) - timedelta(days=10)


class MemoryPurger:
    """The bucket, in memory, recording every call in order with Firestore's
    audit writes (`journal`), so a test can see what happened first."""

    def __init__(self, journal: list[str]) -> None:
        self.bucket = "swarm-artifacts-test"
        self.objects: dict[str, bytes] = {}
        self.deleted: list[str] = []
        self.listed: list[str] = []
        self.journal = journal
        self.fail_delete = False

    def put(self, key: str) -> None:
        self.objects[key] = b"x"

    def list_keys(self, prefix: str) -> list[str]:
        self.listed.append(prefix)
        return sorted(k for k in self.objects if k.startswith(prefix))

    def delete_keys(self, keys: list[str]) -> None:
        if self.fail_delete:
            raise PurgeFailed("403 the API may not delete objects")
        for key in keys:
            self.journal.append(f"gcs:{key}")
            self.deleted.append(key)
            self.objects.pop(key, None)


@pytest.fixture
def journal() -> list[str]:
    return []


@pytest.fixture
def purger(journal) -> MemoryPurger:
    return MemoryPurger(journal)


@pytest.fixture
def client(api_context, purger, db, journal) -> TestClient:
    api_context.artifact_purger = purger
    # Every document write and delete, in order, into the same journal.
    original_set = type(db.collection("x").document("y")).set
    original_delete = type(db.collection("x").document("y")).delete

    def set_(ref, data, merge=False, **kw):
        journal.append(f"set:{ref.path}")
        return original_set(ref, data, merge=merge, **kw)

    def delete_(ref, **kw):
        journal.append(f"delete:{ref.path}")
        return original_delete(ref, **kw)

    ref_type = type(db.collection("x").document("y"))
    mp = pytest.MonkeyPatch()
    mp.setattr(ref_type, "set", set_)
    mp.setattr(ref_type, "delete", delete_)
    yield TestClient(create_app(api_context), raise_server_exceptions=False)
    mp.undo()


def _task(db, task_id: str, *, tenant: str = "eng", state: str = "FAILED",
          workflow_id: str | None = None, updated: datetime = OLD) -> dict:
    doc = seed_task(db, task_id=task_id, tenant_id=tenant, state=state,
                    workflow_id=workflow_id, created_at=updated)
    return doc


def _attempt(db, task_id: str, attempt_id: str, tenant: str = "eng") -> None:
    db.docs[f"attempts/{attempt_id}"] = {"attempt_id": attempt_id, "task_id": task_id,
                                         "tenant_id": tenant, "created_at": OLD}


def _lease(db, task_id: str, lease_id: str, *, released: bool = True) -> None:
    db.docs[f"leases/{lease_id}"] = {
        "lease_id": lease_id, "task_id": task_id, "tenant_id": "eng", "units": 1,
        "pools": ["global"], "released_at": OLD if released else None,
    }


def _event(db, task_id: str, event_id: str) -> None:
    db.docs[f"tasks/{task_id}/events/{event_id}"] = {"event_id": event_id, "type": "failed"}


def _workflow(db, workflow_id: str, steps: list[str], *, tenant: str = "eng",
              state: str = "FAILED", step_states: dict[str, str] | None = None) -> None:
    step_states = step_states or {}
    for i, task_id in enumerate(steps):
        _task(db, task_id, tenant=tenant, state=step_states.get(task_id, "FAILED"),
              workflow_id=workflow_id)
        db.docs[f"tasks/{task_id}"]["step_id"] = f"s{i}"
    db.docs[f"workflows/{workflow_id}"] = {
        "workflow_id": workflow_id, "tenant_id": tenant, "state": state,
        "created_at": OLD, "updated_at": OLD, "submitted_by": "seed@eng",
        "steps": [{"step_id": f"s{i}", "runner_profile": "mock", "input": {},
                   "task_id": t} for i, t in enumerate(steps)],
    }


def _capacity(db) -> dict:
    seed_pool(db, "global", hard_limit=10, active=2)
    db.docs["quota/anthropic"] = {"provider": "anthropic", "state": "OPEN"}
    db.docs["accounts/acc_1"] = {"account_id": "acc_1", "state": "ACTIVE"}
    return {p: copy.deepcopy(d) for p, d in db.docs.items()
            if p.split("/")[0] in ("pools", "quota", "accounts", "tenants")}


def _seed(db, purger) -> None:
    seed_tenant(db, "eng")
    seed_tenant(db, "research")
    _workflow(db, "wf_dead", ["t_a", "t_b"])
    _attempt(db, "t_a", "att_a1")
    _lease(db, "t_a", "lease_a1")
    _event(db, "t_a", "ev_1")
    purger.put("tenants/eng/tasks/t_a/attempts/att_a1/artifacts/out.md")
    purger.put("tenants/eng/tasks/t_b/attempts/att_b1/logs/stdout")
    _task(db, "t_alone", state="DEAD_LETTERED")
    purger.put("tenants/eng/tasks/t_alone/attempts/x/checkpoints/c1/manifest.json")
    # The neighbours that must survive: a task whose id is a prefix extension,
    # and another tenant's object under the same task id.
    purger.put("tenants/eng/tasks/t_a0/attempts/z/logs/stdout")
    purger.put("tenants/research/tasks/t_alone/attempts/y/logs/stdout")


def _purge(client, **body):
    body.setdefault("states", ["FAILED", "CANCELLED", "DEAD_LETTERED"])
    return client.post(ROUTE, json=body, headers=auth_header("root"))


# -- authorisation --------------------------------------------------------

def test_a_tenant_member_is_refused(client, db, purger):
    _seed(db, purger)
    before = copy.deepcopy(db.docs)
    response = client.post(ROUTE, json={"states": ["FAILED"], "dry_run": False,
                                         "confirm": "purge"}, headers=auth_header("alice"))
    assert response.status_code == 403, response.text
    assert db.docs == before and purger.deleted == []


def test_the_pool_admin_capability_is_refused(db, tokens, group_map, objects, purger):
    _seed(db, purger)
    gate = "gate@verify-project.iam.gserviceaccount.com"
    tokens = dict(tokens, **{"token-gate": {"email": gate, "email_verified": True,
                                            "sub": "sub-gate"}})
    ctx = build_context(
        settings=api_settings(allowed_users=(gate,), admin_pool_users=(gate,)),
        db=db, verifier=StaticTokenVerifier(tokens), groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(), waker=NullWaker(), metrics=ApiMetrics(),
        objects=objects, forge_tokens=NoForgeTokens(), artifact_purger=purger,
    )
    response = TestClient(create_app(ctx), raise_server_exceptions=False).post(
        ROUTE, json={"states": ["FAILED"], "dry_run": False, "confirm": "purge"},
        headers={"Authorization": "Bearer token-gate"})
    assert response.status_code == 403, response.text
    assert "tasks/t_a" in {p.rsplit("/events", 1)[0] for p in db.docs}
    assert not any("purge" in path for _method, path in POOL_ADMIN_ROUTES)


# -- the request ----------------------------------------------------------

@pytest.mark.parametrize("states", [["SUCCEEDED"], ["RUNNING"], ["READY", "FAILED"], []])
def test_only_failed_cancelled_or_dead_lettered_can_be_asked_for(client, db, purger, states):
    _seed(db, purger)
    response = client.post(ROUTE, json={"states": states}, headers=auth_header("root"))
    assert response.status_code == 422, response.text


def test_a_delete_without_the_typed_word_is_refused(client, db, purger):
    _seed(db, purger)
    before = copy.deepcopy(db.docs)
    for confirm in (None, "yes", "PURGE"):
        response = _purge(client, dry_run=False, confirm=confirm)
        assert response.status_code == 422, response.text
    assert db.docs == before and purger.deleted == []


# -- dry run --------------------------------------------------------------

def test_a_dry_run_lists_everything_and_deletes_nothing(client, db, purger, journal):
    _seed(db, purger)
    before = copy.deepcopy(db.docs)
    response = _purge(client)  # dry_run defaults to true
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["dry_run"] is True
    assert [w["id"] for w in body["workflows"]] == ["wf_dead"]
    assert body["workflows"][0]["task_ids"] == ["t_a", "t_b"]
    assert [t["id"] for t in body["tasks"]] == ["t_alone"]
    assert body["counts"] == {
        "workflows": 1, "standalone_tasks": 1, "task_documents": 3, "attempts": 1,
        "leases": 1, "events": 1, "artifact_objects": 3, "skipped": 0, "failed": 0,
    }
    assert db.docs == before
    assert purger.deleted == []
    assert not [entry for entry in journal if entry.startswith(("set:", "delete:"))]


# -- the purge ------------------------------------------------------------

def test_a_purge_deletes_the_workflow_its_steps_and_their_records(client, db, purger):
    _seed(db, purger)
    response = _purge(client, dry_run=False, confirm="purge")
    assert response.status_code == 200, response.text
    for path in ("workflows/wf_dead", "tasks/t_a", "tasks/t_b", "tasks/t_a/events/ev_1",
                 "attempts/att_a1", "leases/lease_a1", "tasks/t_alone"):
        assert path not in db.docs, path
    assert sorted(purger.deleted) == [
        "tenants/eng/tasks/t_a/attempts/att_a1/artifacts/out.md",
        "tenants/eng/tasks/t_alone/attempts/x/checkpoints/c1/manifest.json",
        "tenants/eng/tasks/t_b/attempts/att_b1/logs/stdout",
    ]


def test_capacity_documents_are_untouched(client, db, purger):
    _seed(db, purger)
    _lease(db, "t_alone", "lease_alone")
    capacity = _capacity(db)
    assert _purge(client, dry_run=False, confirm="purge").status_code == 200
    after = {p: d for p, d in db.docs.items()
             if p.split("/")[0] in ("pools", "quota", "accounts", "tenants")}
    assert after == capacity


def test_artifacts_go_only_under_the_items_stored_tenant_prefix(client, db, purger):
    _seed(db, purger)
    # The request names research; the task is stored under eng. The request
    # narrows the selection and never re-roots the prefix.
    response = _purge(client, tenant_id="research", dry_run=False, confirm="purge")
    assert response.status_code == 200, response.text
    assert response.json()["counts"]["workflows"] == 0
    assert purger.deleted == [] and "tasks/t_alone" in db.docs
    response = _purge(client, tenant_id="eng", dry_run=False, confirm="purge")
    assert response.status_code == 200, response.text
    assert all(key.startswith("tenants/eng/tasks/t_") for key in purger.deleted)
    assert set(purger.listed) == {"tenants/eng/tasks/t_a/", "tenants/eng/tasks/t_b/",
                                  "tenants/eng/tasks/t_alone/"}
    # The neighbours survive: another tenant's same task id, and t_a0.
    assert "tenants/research/tasks/t_alone/attempts/y/logs/stdout" in purger.objects
    assert "tenants/eng/tasks/t_a0/attempts/z/logs/stdout" in purger.objects


def test_the_audit_is_written_before_anything_is_deleted(client, db, purger, journal):
    _seed(db, purger)
    assert _purge(client, dry_run=False, confirm="purge").status_code == 200
    audits = [p for p in db.docs if p.startswith("admin_audit/")]
    assert len(audits) == 2
    entries = sorted((db.docs[p] for p in audits), key=lambda e: e["target_id"])
    assert [(e["action"], e["target_kind"], e["target_id"]) for e in entries] == [
        ("history_purged", "task", "t_alone"), ("history_purged", "workflow", "wf_dead")]
    wf = entries[1]
    assert wf["by"] == "root@saga.xyz" and isinstance(wf["at"], datetime)
    assert wf["detail"]["tenant_id"] == "eng" and wf["detail"]["state"] == "FAILED"
    assert wf["detail"]["task_ids"] == ["t_a", "t_b"]
    assert wf["detail"]["artifact_objects"] == 2
    # Ordering: each item's audit entry is written before its first object goes.
    for entry_path in audits:
        written = journal.index(f"set:{entry_path}")
        task = "t_a" if db.docs[entry_path]["target_id"] == "wf_dead" else "t_alone"
        deletes = [i for i, j in enumerate(journal)
                   if j.startswith("gcs:") and f"/tasks/{task}/" in j]
        assert deletes and written < min(deletes), entry_path


def test_a_failed_bucket_delete_keeps_the_documents_and_is_audited(client, db, purger):
    _seed(db, purger)
    purger.fail_delete = True
    body = _purge(client, dry_run=False, confirm="purge").json()
    assert {f["id"] for f in body["failed"]} == {"wf_dead", "t_alone"}
    assert "workflows/wf_dead" in db.docs and "tasks/t_a" in db.docs
    actions = sorted(d["action"] for p, d in db.docs.items() if p.startswith("admin_audit/"))
    assert actions == ["history_purge_failed"] * 2 + ["history_purged"] * 2


# -- what is never purged -------------------------------------------------

def test_a_workflow_with_a_live_step_is_never_purged(client, db, purger):
    seed_tenant(db, "eng")
    _workflow(db, "wf_live", ["s_done", "s_running"], step_states={"s_running": "RUNNING"})
    purger.put("tenants/eng/tasks/s_done/attempts/a/logs/stdout")
    body = _purge(client, dry_run=False, confirm="purge").json()
    assert body["workflows"] == [] and body["tasks"] == []
    assert [(s["id"], s["reason"]) for s in body["skipped"]] == [("wf_live", "live_step")]
    assert "workflows/wf_live" in db.docs and "tasks/s_done" in db.docs
    assert purger.deleted == []


def test_a_task_with_a_live_lease_is_never_purged(client, db, purger):
    seed_tenant(db, "eng")
    _task(db, "t_leaky")
    _lease(db, "t_leaky", "lease_live", released=False)
    body = _purge(client, dry_run=False, confirm="purge").json()
    assert [(s["id"], s["reason"]) for s in body["skipped"]] == [("t_leaky", "live_lease")]
    assert "tasks/t_leaky" in db.docs and "leases/lease_live" in db.docs


def test_a_live_task_is_never_selected(client, db, purger):
    seed_tenant(db, "eng")
    for state in ("READY", "RUNNING", "PARKED", "LEASED", "SUCCEEDED"):
        _task(db, f"t_{state.lower()}", state=state)
    body = _purge(client, dry_run=False, confirm="purge").json()
    assert body["tasks"] == [] and body["scanned"] == 0
    assert len([p for p in db.docs if p.startswith("tasks/")]) == 5


def test_what_a_live_issue_run_references_is_never_purged(client, db, purger):
    _seed(db, purger)
    db.docs["issue_runs/run_1"] = {"id": "run_1", "state": "FIXING", "workflow_id": "wf_dead",
                                   "planner_task_id": "p"}
    db.docs["issue_runs/run_2"] = {"id": "run_2", "state": "PLANNING",
                                   "planner_task_id": "t_alone"}
    body = _purge(client, dry_run=False, confirm="purge").json()
    assert {(s["id"], s["reason"]) for s in body["skipped"]} == {
        ("wf_dead", "issue_run"), ("t_alone", "issue_run")}
    assert "workflows/wf_dead" in db.docs and "tasks/t_alone" in db.docs
    # An ENDED run's reference does not hold its workflow.
    db.docs["issue_runs/run_1"]["state"] = "FAILED"
    body = _purge(client, dry_run=False, confirm="purge").json()
    assert [w["id"] for w in body["workflows"]] == ["wf_dead"]


def test_exclude_ids_are_kept_and_a_named_step_keeps_its_workflow(client, db, purger):
    _seed(db, purger)
    _task(db, "t_other", state="CANCELLED")
    body = _purge(client, dry_run=False, confirm="purge",
                  exclude_ids=["t_b", "t_alone"]).json()
    assert {(s["id"], s["reason"]) for s in body["skipped"]} == {
        ("wf_dead", "excluded"), ("t_alone", "excluded")}
    assert [t["id"] for t in body["tasks"]] == ["t_other"]
    for path in ("workflows/wf_dead", "tasks/t_a", "tasks/t_b", "tasks/t_alone"):
        assert path in db.docs, path
    assert "tasks/t_other" not in db.docs


def test_a_workflow_named_in_exclude_ids_is_kept_whole(client, db, purger):
    _seed(db, purger)
    body = _purge(client, dry_run=False, confirm="purge", exclude_ids=["wf_dead"]).json()
    assert [(s["id"], s["reason"]) for s in body["skipped"]] == [("wf_dead", "excluded")]
    assert [t["id"] for t in body["tasks"]] == ["t_alone"]
    for path in ("workflows/wf_dead", "tasks/t_a", "tasks/t_b", "attempts/att_a1"):
        assert path in db.docs, path
    assert not [k for k in purger.deleted if "/t_a/" in k or "/t_b/" in k]


def test_states_and_before_narrow_the_selection(client, db, purger):
    seed_tenant(db, "eng")
    _task(db, "t_failed", state="FAILED")
    _task(db, "t_cancelled", state="CANCELLED")
    _task(db, "t_recent", state="FAILED", updated=datetime.now(timezone.utc))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    body = _purge(client, states=["FAILED"], before=cutoff).json()
    assert [t["id"] for t in body["tasks"]] == ["t_failed"]


# -- re-runs and paging ---------------------------------------------------

def test_a_rerun_is_idempotent_and_finishes_a_partial_purge(client, db, purger):
    _seed(db, purger)
    # A purge that died after the bucket and one step: t_a and its records
    # are gone, the workflow document and t_b remain.
    for path in ("tasks/t_a", "tasks/t_a/events/ev_1", "attempts/att_a1", "leases/lease_a1"):
        db.docs.pop(path)
    purger.objects.pop("tenants/eng/tasks/t_a/attempts/att_a1/artifacts/out.md")
    first = _purge(client, dry_run=False, confirm="purge")
    assert first.status_code == 200, first.text
    assert first.json()["workflows"][0]["task_ids"] == ["t_b"]
    assert "workflows/wf_dead" not in db.docs and "tasks/t_b" not in db.docs
    second = _purge(client, dry_run=False, confirm="purge").json()
    assert second["workflows"] == [] and second["tasks"] == [] and second["failed"] == []


def test_a_page_limit_bounds_one_call_and_the_token_resumes(client, db, purger):
    seed_tenant(db, "eng")
    for i in range(5):
        _task(db, f"t_{i}")
    seen: list[str] = []
    token = None
    for _ in range(10):
        body = _purge(client, limit=2, page_token=token).json()
        assert len(body["tasks"]) <= 2
        seen += [t["id"] for t in body["tasks"]]
        token = body["next_page_token"]
        if not token:
            break
    assert seen == [f"t_{i}" for i in range(5)]
