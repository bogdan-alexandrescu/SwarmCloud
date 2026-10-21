"""The check, attach-all and the workflows list read EVERY running workflow
they are scoped to (CHECK-FILTER, #1749).

THE DEFECT (tenant eng, 2026-10-20 20:11Z, plugin sc-v0.5.45). `swarm_check`,
`swarm_workflow_launch {attach: "all"}` and `swarm_workflows` read at most 4
pages of 50 running workflows and said "stopped after 4 pages ... older ones
are not checked". A session's stuck work older than the tenant's newest 200
running workflows was never flagged and never given a row.

THE CHANGE (owner decision 2026-10-20: a server-side filter, not paging to the
end). The check and attach-all ask `GET /v1/workflows` for this session's
work (`claude_session=`), so their walk is this session's handful of rows; the
4-page cap is gone everywhere, and `incomplete_because` is said only when a
read actually fails. Held here against the real API over FakeFirestore.
"""

from __future__ import annotations

import ast
import json
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from swarm_api.codec import workflow_to_firestore
from swarm_common.models import Workflow, WorkflowStep
from swarm_common.states import TaskState
from swarm_mcp import check, launch, sc, server, session
from swarm_mcp.client import SwarmError

from test_against_the_real_api import api, db, swarm  # noqa: F401

_MCP = Path(sc.__file__).resolve().parent
_PLUGIN = Path(__file__).resolve().parents[3] / "plugin"
REPO = "https://github.com/acme/app"
TENANT_SIZE = 250


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    """Launch copies have somewhere to go, and the registry starts empty."""
    monkeypatch.setenv(launch.PLUGIN_ROOT_ENV, str(_PLUGIN))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    launch.forget_attached()
    yield
    launch.forget_attached()


def _running(db, workflow_id: str, at: datetime, sid: str) -> None:  # noqa: F811
    task_id = f"task_{workflow_id}"
    db.docs[f"tasks/{task_id}"] = {
        "id": task_id, "tenant_id": "eng", "state": "RUNNING",
        "created_at": at, "updated_at": at, "started_at": at, "runner_profile": "mock",
        "resource_class": "standard", "input": {}, "metadata": {"claude_session": sid},
        "repository_url": REPO, "submitted_by": "alice@example.com",
        "workflow_id": workflow_id, "step_id": "only",
    }
    workflow = Workflow(
        workflow_id=workflow_id, tenant_id="eng", created_at=at, updated_at=at,
        state=TaskState.RUNNING, submitted_by="alice@example.com",
        steps=[WorkflowStep(step_id="only", runner_profile="mock", input={},
                            depends_on=[], task_id=task_id)],
    )
    db.docs[f"workflows/{workflow_id}"] = workflow_to_firestore(workflow)


@pytest.fixture
def tenant(db, monkeypatch):  # noqa: F811
    """250 running workflows; this session owns 3, one of them the OLDEST of
    all, five hours quiet. The other 247 are another session's, all newer."""
    mine, theirs = str(uuid.uuid4()), str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    for i in range(TENANT_SIZE - 3):
        _running(db, f"wf_theirs_{i:03d}", now - timedelta(seconds=i), theirs)
    _running(db, "wf_mine_new", now - timedelta(seconds=10), mine)
    _running(db, "wf_mine_mid", now - timedelta(seconds=100), mine)
    _running(db, "wf_mine_oldest", now - timedelta(hours=5), mine)
    monkeypatch.setenv(session.SESSION_ENV, mine)
    return {"mine": mine, "theirs": theirs}


def test_the_check_flags_this_sessions_oldest_workflow_behind_200_newer_ones(swarm, tenant):  # noqa: F811
    result = check.run(swarm, repository="all", names=lambda _c: {})
    flagged = {i["workflow_id"] for i in result["flagged"] if i["rule"] == "no_heartbeat"}
    # MUTATION: restore the 4-page cap (or drop `claude_session` from the
    # check's listing) and the oldest workflow is past the newest 200: never
    # read, never flagged.
    assert "wf_mine_oldest" in flagged
    assert {w["workflow_id"] for w in result["running"]["workflows"]} == {
        "wf_mine_new", "wf_mine_mid", "wf_mine_oldest"}
    # MUTATION: keep the old sentence and it is said over a complete read.
    assert "running_incomplete_because" not in result
    assert "single_tasks_incomplete_because" not in result
    # The other session's work is still counted as left out, from the API's
    # own count. MUTATION: drop `filter.left_out` and this reads 0.
    assert result["session"]["left_out"]["workflows"] == {"other_session": TENANT_SIZE - 3, "untagged": 0}


def test_attach_all_attaches_this_sessions_oldest_workflow(plugin, swarm, tenant):  # noqa: F811
    reply = json.loads(server._call(swarm, "swarm_workflow_launch", {"attach": "all"}))
    attached = {l["workflow_id"] for l in reply["launches"] if l.get("workflow_id")}
    # MUTATION: restore the cap and wf_mine_oldest gets no row.
    assert attached == {"wf_mine_new", "wf_mine_mid", "wf_mine_oldest"}
    assert "incomplete_because" not in json.dumps(reply)


def test_the_workflows_list_names_every_running_workflow_of_the_tenant(swarm, tenant):  # noqa: F811
    listing = json.loads(server._call(swarm, "swarm_workflows", {}))
    # MUTATION: restore the 4-page cap and this is 200, `complete: false`.
    assert listing["count"] == TENANT_SIZE
    assert listing["complete"] is True and "incomplete_because" not in listing


def test_the_check_asks_the_api_for_this_sessions_work(swarm, tenant, monkeypatch):  # noqa: F811
    asked: list[str] = []
    real = type(swarm).request

    def recording(self, method, path, **kwargs):
        if path.startswith("/v1/workflows?") and "active=true" in path:
            asked.append(path)
        return real(self, method, path, **kwargs)

    monkeypatch.setattr(type(swarm), "request", recording)
    check.run(swarm, repository="all", names=lambda _c: {})
    # MUTATION: list without the session and the walk is 5 pages of everyone's.
    assert len(asked) == 1, asked
    assert f"claude_session={tenant['mine']}" in asked[0]


def test_a_read_that_fails_mid_walk_is_said_and_keeps_what_was_read():
    pages = iter([
        {"workflows": [{"workflow_id": "wf_1", "state": "RUNNING", "steps": []}],
         "next_page_token": "p2", "tenant_id": "eng", "filter": {"stored_states": ["RUNNING"]}},
    ])

    class _Client:
        def workflows(self, **_):
            try:
                return next(pages)
            except StopIteration:
                raise SwarmError("GET /v1/workflows -> 503: unavailable") from None

    listing = sc.running_workflows(_Client(), detail_reads=0)
    assert [w["workflow_id"] for w in listing["workflows"]] == ["wf_1"]
    # MUTATION: swallow the failure and the list reads as complete.
    assert listing["complete"] is False
    assert "503" in listing["incomplete_because"]


def test_a_page_token_handed_back_unchanged_stops_the_walk_and_says_so():
    seen: list[str | None] = []

    class _Client:
        def workflows(self, *, page_token=None, **_):
            seen.append(page_token)
            return {"workflows": [], "next_page_token": "same", "tenant_id": "eng"}

    listing = sc.running_workflows(_Client(), detail_reads=0)
    # MUTATION: follow the token blindly and this never returns.
    assert seen == [None, "same"]
    assert listing["complete"] is False and "same page token" in listing["incomplete_because"]


def _assigned(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else (
            [node.target] if isinstance(node, ast.AnnAssign) else [])
        names |= {t.id for t in targets if isinstance(t, ast.Name)}
    return names


def test_the_workflow_page_constants_are_stated_once():
    in_check, in_sc = _assigned(_MCP / "check.py"), _assigned(_MCP / "sc.py")
    # MUTATION: restate `WORKFLOW_PAGE_SIZE = 50` in check.py and it is twice.
    assert not {n for n in in_check & in_sc if "PAGE" in n}, in_check & in_sc
    # MUTATION: bring a page cap back in either module.
    for gone in ("WORKFLOW_LIST_PAGES", "SINGLE_TASK_PAGES"):
        assert gone not in in_check | in_sc, gone
    assert check.WORKFLOW_PAGE_SIZE is sc.WORKFLOW_PAGE_SIZE
