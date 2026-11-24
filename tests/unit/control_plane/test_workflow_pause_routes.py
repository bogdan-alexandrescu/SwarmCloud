"""The workflow pause and resume routes (docs/design/workflow-pause.md section
3, lane P1; owner decisions of 2026-11-24 on issue 7: any member may pause and
resume, no cascade to child tasks).

What is held:

* pause records `paused`, `paused_at` and `paused_by` on the workflow document
  (the one authority the scheduler and the reconciler read by equality), and
  mirrors `pause_requested_at == paused_at` onto every non-terminal step with
  one PAUSE_REQUESTED event each; it never writes a step's state, generation
  or lease (invariants 2, 3 and 5);
* a repeat pause writes nothing, never moves `paused_at`, and answers
  `already_paused`;
* resume clears the pause and only a mirror that matches the pause it lifts,
  with one RESUME_REQUESTED each; a repeat resume writes nothing;
* a viewer is refused 403 `role_member_required` and nothing is written;
* another tenant's workflow is 404;
* a finished workflow refuses pause 409 `workflow_terminal`, a workflow
  being cancelled 409 `workflow_cancel_requested`.

No cloud and no emulator: the in-memory Firestore and the real app.
"""

from __future__ import annotations

import secrets
from typing import Any

import pytest

from swarm_api import refusals, roles
from swarm_api.auth import StaticTokenVerifier
from swarm_api.credentials import InMemoryCredentials
from swarm_api.deps import build_context
from swarm_api.metrics import ApiMetrics
from swarm_api.waker import NullWaker

from .conftest import (
    ENG_GROUP,
    NoForgeTokens,
    StaticGroups,
    api_settings,
    auth_header,
    seed_pool,
    seed_tenant,
)
from .test_roles_store import grant

MEMBER_SWITCH = refusals.env_name("role_member_required")
WORKFLOW = {
    "steps": [
        {"step_id": "a", "runner_profile": "mock"},
        {"step_id": "b", "runner_profile": "mock", "depends_on": ["a"]},
    ]
}


@pytest.fixture
def group_map(group_map) -> dict[str, tuple[str, ...]]:
    """conftest's people, and `vera`, an eng member a grant narrows to viewer."""
    return {**group_map, "vera@example.com": (ENG_GROUP,)}


@pytest.fixture
def api_context(db, tokens, group_map, objects):
    """conftest's context with a settings key, so a role grant can be signed."""
    return build_context(
        settings=api_settings(settings_key=secrets.token_hex(32)),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
        forge_tokens=NoForgeTokens(),
    )


def _create(client, db) -> tuple[str, dict[str, str]]:
    seed_tenant(db, "eng")
    seed_pool(db, "global", hard_limit=10)
    created = client.post("/v1/workflows", headers=auth_header("alice"), json=WORKFLOW)
    assert created.status_code in (200, 201), created.text
    workflow = created.json()["workflow"]
    return workflow["workflow_id"], {s["step_id"]: s["task_id"] for s in workflow["steps"]}


def _events(db, task_id: str, type_: str) -> list[dict[str, Any]]:
    prefix = f"tasks/{task_id}/events/"
    return [d for k, d in db.docs.items() if k.startswith(prefix) and d.get("type") == type_]


def _without_people(db) -> dict[str, dict[str, Any]]:
    return {k: v for k, v in db.dump().items() if not k.startswith("people/")}


def test_pause_records_the_workflow_and_mirrors_every_live_step(client, db):
    workflow_id, steps = _create(client, db)
    states_before = {t: db.docs[f"tasks/{t}"]["state"] for t in steps.values()}

    r = client.post(f"/v1/workflows/{workflow_id}/pause", headers=auth_header("alice"),
                    json={"reason": "reading step a's output first"})

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["workflow_id"] == workflow_id
    assert body["paused"] is True
    assert sorted(body["steps_signalled"]) == sorted(steps.values())
    assert body["steps_already_terminal"] == []
    doc = db.docs[f"workflows/{workflow_id}"]
    assert doc["paused"] is True
    assert doc["paused_by"] == "alice@example.com"
    assert doc["pause_reason"] == "reading step a's output first"
    paused_at = doc["paused_at"]
    assert paused_at is not None
    for task_id in steps.values():
        task = db.docs[f"tasks/{task_id}"]
        assert task["pause_requested_at"] == paused_at
        # The route signals; it never moves a step's state (the scheduler parks).
        assert task["state"] == states_before[task_id]
        events = _events(db, task_id, "pause_requested")
        assert len(events) == 1
        assert events[0]["detail"]["workflow_id"] == workflow_id
        assert events[0]["detail"]["requested_by"] == "alice@example.com"


def test_a_repeat_pause_keeps_paused_at_and_writes_nothing(client, db):
    workflow_id, steps = _create(client, db)
    first = client.post(f"/v1/workflows/{workflow_id}/pause", headers=auth_header("alice"))
    assert first.status_code == 200, first.text
    before = _without_people(db)

    again = client.post(f"/v1/workflows/{workflow_id}/pause", headers=auth_header("alice"))

    assert again.status_code == 200, again.text
    assert again.json()["already_paused"] is True
    assert _without_people(db) == before
    for task_id in steps.values():
        assert len(_events(db, task_id, "pause_requested")) == 1


def test_resume_lifts_the_pause_and_clears_only_the_matching_mirror(client, db):
    workflow_id, steps = _create(client, db)
    assert client.post(f"/v1/workflows/{workflow_id}/pause",
                       headers=auth_header("alice")).status_code == 200
    # Step b's mirror belongs to some other pause: resume must leave it alone.
    db.docs[f"tasks/{steps['b']}"]["pause_requested_at"] = "a-later-pause"

    r = client.post(f"/v1/workflows/{workflow_id}/resume", headers=auth_header("alice"), json={})

    assert r.status_code == 200, r.text
    doc = db.docs[f"workflows/{workflow_id}"]
    assert doc["paused"] is False
    assert doc["paused_at"] is None
    assert doc["resumed_by"] == "alice@example.com"
    assert doc["resumed_at"] is not None
    assert db.docs[f"tasks/{steps['a']}"].get("pause_requested_at") is None
    assert len(_events(db, steps["a"], "resume_requested")) == 1
    assert db.docs[f"tasks/{steps['b']}"]["pause_requested_at"] == "a-later-pause"
    assert _events(db, steps["b"], "resume_requested") == []


def test_a_repeat_resume_writes_nothing(client, db):
    workflow_id, steps = _create(client, db)
    client.post(f"/v1/workflows/{workflow_id}/pause", headers=auth_header("alice"))
    assert client.post(f"/v1/workflows/{workflow_id}/resume",
                       headers=auth_header("alice")).status_code == 200
    before = _without_people(db)

    again = client.post(f"/v1/workflows/{workflow_id}/resume", headers=auth_header("alice"))

    assert again.status_code == 200, again.text
    assert again.json()["already_running"] is True
    assert _without_people(db) == before


def test_resume_of_a_workflow_never_paused_is_already_running(client, db):
    workflow_id, _steps = _create(client, db)
    r = client.post(f"/v1/workflows/{workflow_id}/resume", headers=auth_header("alice"))
    assert r.status_code == 200, r.text
    assert r.json()["already_running"] is True


@pytest.mark.parametrize("action", ["pause", "resume"])
def test_a_viewer_is_refused_and_nothing_is_written(client, db, api_context, monkeypatch, action):
    workflow_id, _steps = _create(client, db)
    if action == "resume":
        client.post(f"/v1/workflows/{workflow_id}/pause", headers=auth_header("alice"))
    grant(api_context.roles, "vera@example.com", roles.VIEWER)
    monkeypatch.setenv(MEMBER_SWITCH, "on")
    before = _without_people(db)

    r = client.post(f"/v1/workflows/{workflow_id}/{action}", headers=auth_header("vera"))

    assert r.status_code == 403, r.text
    assert r.json()["code"] == "role_member_required"
    after = {k: v for k, v in _without_people(db).items()
             if not k.startswith(f"{roles.GRANTS_COLLECTION}/")}
    assert after == {k: v for k, v in before.items()
                     if not k.startswith(f"{roles.GRANTS_COLLECTION}/")}


@pytest.mark.parametrize("action", ["pause", "resume"])
def test_another_tenants_workflow_is_not_found(client, db, action):
    workflow_id, _steps = _create(client, db)
    seed_tenant(db, "research")
    before = db.docs[f"workflows/{workflow_id}"].copy()

    r = client.post(f"/v1/workflows/{workflow_id}/{action}", headers=auth_header("bob"))

    assert r.status_code == 404, r.text
    # The store's tenant check, not a route that does not exist: FastAPI's own
    # 404 carries no `code`.
    assert r.json()["code"] in ("not_found", "workflow_not_in_tenant")
    assert db.docs[f"workflows/{workflow_id}"] == before


@pytest.mark.parametrize("state", ["SUCCEEDED", "FAILED", "CANCELLED"])
def test_a_finished_workflow_refuses_pause(client, db, state):
    workflow_id, _steps = _create(client, db)
    db.docs[f"workflows/{workflow_id}"]["state"] = state

    r = client.post(f"/v1/workflows/{workflow_id}/pause", headers=auth_header("alice"))

    assert r.status_code == 409, r.text
    assert r.json()["code"] == "workflow_terminal"
    assert not db.docs[f"workflows/{workflow_id}"].get("paused")


def test_a_cancelled_workflow_refuses_pause(client, db):
    workflow_id, steps = _create(client, db)
    assert client.post(f"/v1/workflows/{workflow_id}/cancel",
                       headers=auth_header("alice")).status_code == 200

    r = client.post(f"/v1/workflows/{workflow_id}/pause", headers=auth_header("alice"))

    assert r.status_code == 409, r.text
    assert r.json()["code"] in ("workflow_cancel_requested", "workflow_terminal")
    assert not db.docs[f"workflows/{workflow_id}"].get("paused")
    for task_id in steps.values():
        assert "pause_requested_at" not in db.docs[f"tasks/{task_id}"]


def test_a_workflow_being_cancelled_refuses_pause_and_resume(client, db):
    workflow_id, _steps = _create(client, db)
    client.post(f"/v1/workflows/{workflow_id}/pause", headers=auth_header("alice"))
    db.docs[f"workflows/{workflow_id}"]["cancel_requested"] = True

    paused = client.post(f"/v1/workflows/{workflow_id}/pause", headers=auth_header("alice"))
    resumed = client.post(f"/v1/workflows/{workflow_id}/resume", headers=auth_header("alice"))

    assert paused.status_code == 409 and paused.json()["code"] == "workflow_cancel_requested"
    assert resumed.status_code == 409 and resumed.json()["code"] == "workflow_cancel_requested"


def test_pause_writes_no_state_generation_or_lease_on_a_live_step(client, db, make_scheduler):
    workflow_id, steps = _create(client, db)
    make_scheduler().drain()
    live = db.docs[f"tasks/{steps['a']}"]
    assert live["state"] == "DISPATCHED", "nothing was admitted"
    kept = {k: live[k] for k in ("state", "current_generation", "current_lease_id")}
    pools = {k: v.get("active") for k, v in db.docs.items() if k.startswith("pools/")}

    r = client.post(f"/v1/workflows/{workflow_id}/pause", headers=auth_header("alice"))

    assert r.status_code == 200, r.text
    after = db.docs[f"tasks/{steps['a']}"]
    assert {k: after[k] for k in kept} == kept
    assert after["pause_requested_at"] == db.docs[f"workflows/{workflow_id}"]["paused_at"]
    assert {k: v.get("active") for k, v in db.docs.items() if k.startswith("pools/")} == pools


@pytest.mark.parametrize("body", [
    {"reason": "x" * 501},
    {"reason": 7},
    {"runner_profile": "mock"},
    {"image": "anything"},
])
def test_the_pause_body_takes_only_a_bounded_reason(client, db, body):
    workflow_id, _steps = _create(client, db)
    r = client.post(f"/v1/workflows/{workflow_id}/pause", headers=auth_header("alice"), json=body)
    assert r.status_code == 422, r.text
    assert not db.docs[f"workflows/{workflow_id}"].get("paused")


def test_the_resume_body_takes_nothing(client, db):
    workflow_id, _steps = _create(client, db)
    client.post(f"/v1/workflows/{workflow_id}/pause", headers=auth_header("alice"))
    r = client.post(f"/v1/workflows/{workflow_id}/resume", headers=auth_header("alice"),
                    json={"reason": "no"})
    assert r.status_code == 422, r.text
    assert db.docs[f"workflows/{workflow_id}"]["paused"] is True


@pytest.mark.parametrize("action", ["pause", "resume"])
def test_a_contended_step_answers_503_and_a_retry_finishes_it(
    client, db, api_context, monkeypatch, action
):
    from swarm_api import store as store_mod

    workflow_id, steps = _create(client, db)
    if action == "resume":
        assert client.post(f"/v1/workflows/{workflow_id}/pause",
                           headers=auth_header("alice")).status_code == 200
    method = "_signal_step_pause" if action == "pause" else "_clear_step_pause"
    store = api_context.store
    real = getattr(store, method)
    stuck = {steps["b"]}

    def contended(tenant_id, task_id, **kwargs):
        if task_id in stuck:
            raise store_mod._StepContended(task_id)
        return real(tenant_id, task_id, **kwargs)

    monkeypatch.setattr(store, method, contended)
    first = client.post(f"/v1/workflows/{workflow_id}/{action}", headers=auth_header("alice"))
    assert first.status_code == 503, first.text
    assert first.headers.get("Retry-After")
    assert first.json()["code"] == "pause_contended"
    assert first.json()["detail"]["steps_contended"] == [steps["b"]]
    paused_at = db.docs[f"workflows/{workflow_id}"].get("paused_at")

    stuck.clear()
    second = client.post(f"/v1/workflows/{workflow_id}/{action}", headers=auth_header("alice"))

    assert second.status_code == 200, second.text
    # The retry moved no clock and finished the step the first call missed.
    assert db.docs[f"workflows/{workflow_id}"].get("paused_at") == paused_at
    mirror = db.docs[f"tasks/{steps['b']}"].get("pause_requested_at")
    if action == "pause":
        assert mirror == paused_at
        assert len(_events(db, steps["b"], "pause_requested")) == 1
    else:
        assert mirror is None
        assert len(_events(db, steps["b"], "resume_requested")) == 1


def test_cancel_lifts_a_pause_so_the_workflow_leaves_the_paused_query(client, db):
    workflow_id, _steps = _create(client, db)
    client.post(f"/v1/workflows/{workflow_id}/pause", headers=auth_header("alice"))

    r = client.post(f"/v1/workflows/{workflow_id}/cancel", headers=auth_header("alice"))

    assert r.status_code == 200, r.text
    assert db.docs[f"workflows/{workflow_id}"]["paused"] is False
