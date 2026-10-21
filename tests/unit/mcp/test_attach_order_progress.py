"""`/sc attach --all` launches closest to finish first, and a cap leaves out the furthest (BOARD-ORDER).

Owner decisions, 2026-10-16 (#1437): `/sc attach --all` -- and `/sc check`'s
attach, which goes through the same `launch.for_all` -- launches in the board
order (steps done / total, then the current step's age, then the id, in
workspace blocks), so the runs start top to bottom in that order and a cap
leaves out the ones furthest from finishing. The single-task launch follows
the same rule by its own state age. The cap is 100 unless set, 1-200.

THE MUTATIONS THESE CATCH: launch in the order the listing was read (newest
first) instead of the board order, or cap from the end (the launch tests go
red); leave the single tasks in creation order (the task test goes red); put
the default back to 30 or the limit to 50 (the cap test goes red).
"""

from __future__ import annotations

import pytest

from swarm_mcp import config, launch, sc

SESSION = "11111111-2222-4333-8444-555555555555"


@pytest.fixture(autouse=True)
def _plugin_root(monkeypatch, tmp_path):
    from test_plugin_agents_and_workflows import _PLUGIN

    monkeypatch.setenv("SWARM_SC_PLUGIN_ROOT", str(_PLUGIN))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION)
    monkeypatch.setattr(launch.tempfile, "tempdir", str(tmp_path))
    for name in ("SWARM_PLUGIN_MAX_LIVE_ROWS", "CLAUDE_PLUGIN_OPTION_MAX_LIVE_ROWS"):
        monkeypatch.delenv(name, raising=False)


class _NoTenants:
    def tenants_mine(self):
        raise RuntimeError("no /v1/tenants/mine here")

    def request(self, method, path, **_):
        raise RuntimeError("no reads here")


def _wf(wid: str, done: int, total: int, age: int, workspace: str = "eng") -> dict:
    return {"workflow_id": wid, "workspace": workspace, "title": wid, "label": None, "steps_total": total,
            "console": None, "claude_session": SESSION, "state": "RUNNING",
            "progress": {"done": done, "total": total, "age_seconds": age}}


#: Read newest first, as the list route serves them.
_LISTED = [
    _wf("wf_newest", 0, 4, 30),
    _wf("wf_nearly", 3, 4, 600),
    _wf("wf_half", 2, 4, 120),
    _wf("wf_half_longer", 2, 4, 900),
    _wf("wf_oldest", 1, 4, 60),
]


def test_launches_run_closest_to_finish_first_and_the_cap_leaves_out_the_furthest(monkeypatch):
    monkeypatch.setenv("SWARM_PLUGIN_MAX_LIVE_ROWS", "3")
    monkeypatch.setattr(sc, "running_workflows", lambda client, **_: {"count": 5, "complete": True,
                                                                 "workflows": [dict(e) for e in _LISTED]})
    monkeypatch.setattr(sc, "running_tasks", lambda client: {"count": 0, "tasks": []})
    out = launch.for_all(_NoTenants())
    assert [l["workflow_id"] for l in out["launches"]] == ["wf_nearly", "wf_half_longer", "wf_half"]
    assert [n["workflow_id"] for n in out["not_followed"]] == ["wf_oldest", "wf_newest"]


def test_the_workspace_blocks_follow_their_closest_workflow(monkeypatch):
    listed = [_wf("wf_e1", 0, 2, 10, "eng"), _wf("wf_u1", 1, 2, 10, "u-bogdan"), _wf("wf_e2", 1, 2, 5, "eng")]
    monkeypatch.setattr(sc, "running_workflows", lambda client, **_: {"count": 3, "complete": True, "workflows": listed})
    monkeypatch.setattr(sc, "running_tasks", lambda client: {"count": 0, "tasks": []})
    out = launch.for_all(_NoTenants())
    assert [l["workflow_id"] for l in out["launches"]] == ["wf_u1", "wf_e2", "wf_e1"]


def test_the_single_task_launch_runs_the_longest_running_task_first(monkeypatch):
    monkeypatch.setenv("SWARM_PLUGIN_MAX_LIVE_ROWS", "2")
    monkeypatch.setattr(sc, "running_workflows", lambda client, **_: {"count": 0, "complete": True, "workflows": []})
    monkeypatch.setattr(sc, "running_tasks", lambda client: {"count": 3, "tasks": [
        {"task_id": "task_new", "label": "new", "workspace": "eng", "claude_session": SESSION,
         "state_age_seconds": 20, "age_seconds": 20},
        {"task_id": "task_old", "label": "old", "workspace": "eng", "claude_session": SESSION,
         "state_age_seconds": 3000, "age_seconds": 3600},
        {"task_id": "task_mid", "label": "mid", "workspace": "eng", "claude_session": SESSION,
         "state_age_seconds": 400, "age_seconds": 4000},
    ]})
    out = launch.for_all(_NoTenants())
    (task_launch,) = [l for l in out["launches"] if "task_ids" in l]
    assert task_launch["task_ids"] == ["task_old", "task_mid"]
    assert [n["task_id"] for n in out["not_followed"]] == ["task_new"]


def test_a_single_tasks_state_age_runs_from_its_start():
    from datetime import datetime, timezone

    now = datetime(2026, 10, 16, 20, 0, tzinfo=timezone.utc)
    entry = sc._single_task_entry({"id": "task_a", "state": "RUNNING", "created_at": "2026-10-16T19:00:00Z",
                                   "started_at": "2026-10-16T19:50:00Z"}, now)
    assert entry["state_age_seconds"] == 600 and entry["age_seconds"] == 3600


def test_the_cap_is_100_unless_set_and_held_to_1_through_200():
    assert config.max_live_rows({}) == config.DEFAULT_MAX_LIVE_ROWS == 100
    assert config.MAX_LIVE_ROWS_LIMIT == 200
    assert config.max_live_rows({"SWARM_PLUGIN_MAX_LIVE_ROWS": "150"}) == 150
    assert config.max_live_rows({"SWARM_PLUGIN_MAX_LIVE_ROWS": "201"}) == 200
    assert config.max_live_rows({"SWARM_PLUGIN_MAX_LIVE_ROWS": "0"}) == 1
