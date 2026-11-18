"""Every task and workflow the API serves carries its console link.

Owner decision 2026-10-01: the console link comes FROM THE API, one source.
The plugin, the sc CLI, the MCP answers and the web UI print the
`links.console` served here and never rebuild the host. So:

  * the origin is ONE setting, `ApiSettings.console_url` (SWARM_CONSOLE_URL);
  * unset means NO link -- the field is null, never a run.app guess;
  * the link is the same whatever the task's state, QUEUED to terminal;
  * the PATH is the console's own route, read out of
    apps/swarm-ui/src/paths.ts here, so a route renamed in the UI fails this
    file instead of silently breaking every link the API serves.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from swarm_api.codec import (
    CONSOLE_AGENT_TAB,
    agent_console_url,
    task_from_dict,
    task_to_api,
    workflow_console_url,
    workflow_from_dict,
    workflow_to_api,
)
from swarm_api.main import create_app
from swarm_api.settings import ApiSettings
from swarm_common.states import TaskState

from .conftest import auth_header, seed_task

ORIGIN = "https://swarm.example.test"
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
REPO = Path(__file__).resolve().parents[3]
PATHS_TS = REPO / "apps" / "swarm-ui" / "src" / "paths.ts"
AGENTLIST_TS = REPO / "apps" / "swarm-ui" / "src" / "agentlist.ts"


def _task(state: str, task_id: str = "task_abc123") -> object:
    return task_from_dict(
        {
            "id": task_id,
            "tenant_id": "eng",
            "created_at": NOW,
            "updated_at": NOW,
            "state": state,
            "runner_profile": "mock",
            "resource_class": "standard",
        }
    )


# --------------------------------------------------------------------------
# The path shapes are paths.ts's
# --------------------------------------------------------------------------

def test_agent_link_path_is_the_ui_agent_route():
    """`addressToPath('work/task/<id>')` is `/agents/${agentTab}/<id>`, default `live`."""
    source = PATHS_TS.read_text(encoding="utf-8")
    assert "return `/agents/${agentTab}/${seg.slice(2).join('/')}`" in source, (
        "paths.ts no longer spells one agent as /agents/<tab>/<id>: "
        "swarm_api.codec.agent_console_url must follow it"
    )
    default = re.search(r"addressToPath\(address: string, agentTab: AgentTab = '(\w+)'\)", source)
    assert default is not None, "paths.ts addressToPath lost its default agent tab"
    assert default.group(1) == CONSOLE_AGENT_TAB
    tabs = re.search(r"AGENT_TABS = \[([^\]]*)\]", AGENTLIST_TS.read_text(encoding="utf-8"))
    assert tabs is not None and f"'{CONSOLE_AGENT_TAB}'" in tabs.group(1)
    assert agent_console_url(ORIGIN, "task_1") == f"{ORIGIN}/agents/{default.group(1)}/task_1"


def test_workflow_link_path_is_the_ui_workflow_route():
    """`/workflows/${encodeURIComponent(wf)}` in paths.ts, and its reverse."""
    source = PATHS_TS.read_text(encoding="utf-8")
    assert "`/workflows/${encodeURIComponent(wf)}${pane}`" in source, (
        "paths.ts no longer spells one workflow as /workflows/<id>: "
        "swarm_api.codec.workflow_console_url must follow it"
    )
    assert "if (seg[0] === 'workflows' && seg.length >= 2)" in source
    assert workflow_console_url(ORIGIN, "wf_1") == f"{ORIGIN}/workflows/wf_1"
    # encodeURIComponent's spelling, not Python's default quote().
    assert workflow_console_url(ORIGIN, "a b/c(d)!") == f"{ORIGIN}/workflows/a%20b%2Fc(d)!"


@pytest.mark.parametrize("origin", ["", "   ", None])
def test_no_origin_means_no_link(origin):
    assert agent_console_url(origin, "task_1") is None
    assert workflow_console_url(origin, "wf_1") is None


def test_a_trailing_slash_on_the_origin_is_not_doubled():
    assert agent_console_url(ORIGIN + "/", "task_1") == f"{ORIGIN}/agents/live/task_1"


# --------------------------------------------------------------------------
# The codec
# --------------------------------------------------------------------------

@pytest.mark.parametrize("state", [s.value for s in TaskState])
def test_a_task_in_every_state_carries_the_same_link(state):
    served = task_to_api(_task(state), console_url=ORIGIN)
    assert served["links"] == {"console": f"{ORIGIN}/agents/live/task_abc123"}


@pytest.mark.parametrize("state", [s.value for s in TaskState])
def test_an_unset_console_url_serves_null_in_every_state(state):
    assert task_to_api(_task(state))["links"] == {"console": None}
    assert task_to_api(_task(state), console_url="")["links"] == {"console": None}


def _workflow(with_task_ids: bool = True):
    return workflow_from_dict(
        {
            "workflow_id": "wf_xyz",
            "tenant_id": "eng",
            "created_at": NOW,
            "updated_at": NOW,
            "state": "RUNNING",
            "steps": [
                {"step_id": "a", "runner_profile": "mock", "task_id": "task_a" if with_task_ids else None},
                {"step_id": "b", "runner_profile": "mock", "task_id": None},
            ],
        }
    )


def test_a_workflow_carries_its_link_and_each_step_its_agent_link():
    served = workflow_to_api(_workflow(), console_url=ORIGIN)
    assert served["links"] == {"console": f"{ORIGIN}/workflows/wf_xyz"}
    by_step = {s["step_id"]: s for s in served["steps"]}
    assert by_step["a"]["links"] == {"console": f"{ORIGIN}/agents/live/task_a"}
    # A step with no task yet has no agent to open.
    assert by_step["b"]["links"] == {"console": None}


def test_a_workflow_with_no_console_url_serves_null_links():
    served = workflow_to_api(_workflow())
    assert served["links"] == {"console": None}
    assert all(s["links"] == {"console": None} for s in served["steps"])


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

@pytest.fixture
def api_env(monkeypatch):
    monkeypatch.setenv("PROJECT_ID", "swarm-test-project")
    monkeypatch.setenv("ENVIRONMENT", "test")


def test_settings_read_swarm_console_url(api_env, monkeypatch):
    monkeypatch.setenv("SWARM_CONSOLE_URL", ORIGIN)
    assert ApiSettings.from_env().console_url == ORIGIN


def test_settings_default_to_no_console(api_env, monkeypatch):
    monkeypatch.delenv("SWARM_CONSOLE_URL", raising=False)
    assert ApiSettings.from_env().console_url == ""


# --------------------------------------------------------------------------
# Every route that serves a task or a workflow
# --------------------------------------------------------------------------

@pytest.fixture
def linked_client(db, tokens, group_map, objects):
    from swarm_api.auth import StaticTokenVerifier
    from swarm_api.credentials import InMemoryCredentials
    from swarm_api.deps import build_context
    from swarm_api.groups import StaticGroups
    from swarm_api.metrics import ApiMetrics
    from swarm_api.waker import NullWaker

    from .conftest import api_settings

    context = build_context(
        settings=api_settings(console_url=ORIGIN),
        db=db,
        verifier=StaticTokenVerifier(tokens),
        groups=StaticGroups(group_map),
        credentials=InMemoryCredentials(),
        waker=NullWaker(),
        metrics=ApiMetrics(),
        objects=objects,
    )
    return TestClient(create_app(context), raise_server_exceptions=False)


def _agent(task_id: str) -> dict:
    return {"console": f"{ORIGIN}/agents/live/{task_id}"}


def test_task_routes_carry_the_link(linked_client, db):
    alice = auth_header("alice")
    created = linked_client.post("/v1/tasks", headers=alice, json={"runner_profile": "mock"})
    assert created.status_code == 201, created.text
    task = created.json()["task"]
    assert task["links"] == _agent(task["id"])

    batch = linked_client.post(
        "/v1/tasks/batch", headers=alice, json={"tasks": [{"runner_profile": "mock"}]}
    )
    assert batch.status_code == 201, batch.text
    assert all(t["links"] == _agent(t["id"]) for t in batch.json()["tasks"])

    for state in ("QUEUED", "RUNNING", "PARKED", "SUCCEEDED", "FAILED"):
        seed_task(db, task_id=f"task_{state.lower()}", tenant_id="eng", state=state)

    listed = linked_client.get("/v1/tasks", headers=alice)
    assert listed.status_code == 200, listed.text
    rows = listed.json()["tasks"]
    assert rows and all(t["links"] == _agent(t["id"]) for t in rows)

    for state in ("QUEUED", "RUNNING", "PARKED", "SUCCEEDED", "FAILED"):
        got = linked_client.get(f"/v1/tasks/task_{state.lower()}", headers=alice)
        assert got.status_code == 200, got.text
        assert got.json()["task"]["links"] == _agent(f"task_{state.lower()}")

    cancelled = linked_client.post("/v1/tasks/task_queued/cancel", headers=alice)
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["task"]["links"] == _agent("task_queued")


def test_workflow_routes_carry_the_link(linked_client):
    alice = auth_header("alice")
    created = linked_client.post(
        "/v1/workflows",
        headers=alice,
        json={
            "steps": [
                {"step_id": "build", "runner_profile": "mock"},
                {"step_id": "test", "runner_profile": "mock", "depends_on": ["build"]},
            ]
        },
    )
    assert created.status_code == 201, created.text
    workflow = created.json()["workflow"]
    wf_id = workflow["workflow_id"]
    assert workflow["links"] == {"console": f"{ORIGIN}/workflows/{wf_id}"}
    assert all(s["links"] == _agent(s["task_id"]) for s in workflow["steps"])

    got = linked_client.get(f"/v1/workflows/{wf_id}", headers=alice)
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["workflow"]["links"] == {"console": f"{ORIGIN}/workflows/{wf_id}"}
    assert all(s["links"] == _agent(s["task_id"]) for s in body["workflow"]["steps"])
    assert body["tasks"] and all(t["links"] == _agent(t["id"]) for t in body["tasks"])

    listed = linked_client.get("/v1/workflows", headers=alice)
    assert listed.status_code == 200, listed.text
    rows = listed.json()["workflows"]
    assert rows and all(w["links"] == {"console": f"{ORIGIN}/workflows/{w['workflow_id']}"} for w in rows)


def test_the_default_deployment_serves_null_on_the_routes(client, db):
    """`api_settings()` sets no console: every route serves null, not a guess."""
    alice = auth_header("alice")
    created = client.post("/v1/tasks", headers=alice, json={"runner_profile": "mock"})
    assert created.status_code == 201, created.text
    assert created.json()["task"]["links"] == {"console": None}
    wf = client.post("/v1/workflows", headers=alice, json={"steps": [{"step_id": "a", "runner_profile": "mock"}]})
    assert wf.status_code == 201, wf.text
    assert wf.json()["workflow"]["links"] == {"console": None}
    assert wf.json()["workflow"]["steps"][0]["links"] == {"console": None}
