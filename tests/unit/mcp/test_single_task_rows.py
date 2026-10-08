"""A single dispatched task gets a row, as a workflow's steps do (#830).

Owner, 2026-10-07: "we need to be able to auto attach on single tasks too".
Five lanes went out that day as single `swarm_dispatch` tasks and none showed
in Claude Code: the SessionStart hook and `/sc attach --all` read only
`GET /v1/workflows`, and the `sc:step` row stopped on a task in no workflow
(`task ... is workflow step None`). Held here:

* THE LIST. `sc.running_tasks` -- the caller's unfinished tasks in no
  workflow, through the existing `GET /v1/tasks` filters (`state`,
  `submitted_by=me`) -- against the REAL swarm-api, because "only mine, only
  unfinished, only outside a workflow" is the route and the bridge together.
* THE HOOK'S CONTEXT names them beside the workflows, and each read fails alone.
* `/sc attach --all`: `swarm_workflow_launch {attach: "all"}` adds one
  `{attach_tasks}` copy, and run.js starts one `sc:task` row per task, titled
  from its label; launched by name, `{attach: "all"}` returns it as `task_call`.
* `swarm_dispatch`'s reply carries `rows`, the launch for the ids just sent.
* `sc:task` follows WITHOUT `step_id`.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import pytest

from swarm_mcp import launch, sc, server
from swarm_mcp.client import SwarmError

# The real application and SwarmClient's real transport into it.
from test_against_the_real_api import AUTH, DAG, api, db, swarm  # noqa: F401
from test_auto_attach import _BY_PROMPT, _listed
from test_plugin_agents_and_workflows import _RUN_JS, _load, _node, _step

_REPO = Path(__file__).resolve().parents[3]
_PLUGIN = _REPO / "plugin"
_TASK_MD = _PLUGIN / "agents" / "task.md"


def _dispatch(swarm, label: str | None = None, prompt: str = "do the thing") -> str:  # noqa: F811
    args = {"prompt": prompt, "profile": "mock"}
    if label:
        args["label"] = label
    return json.loads(server._call(swarm, "swarm_dispatch", args))["task_id"]


def _workflow(swarm) -> dict:  # noqa: F811
    return json.loads(server._call(swarm, "swarm_workflow", json.loads(json.dumps(DAG))))


# --------------------------------------------------------------------------
# The list, against the real API
# --------------------------------------------------------------------------


def test_the_list_holds_only_my_unfinished_tasks_outside_any_workflow(swarm, api, db):  # noqa: F811
    lane = _dispatch(swarm, label="lane-a", prompt="lane a")
    done = _dispatch(swarm, label="finished", prompt="lane b")
    theirs = _dispatch(swarm, label="someone else's", prompt="lane c")
    child = _dispatch(swarm, label="a child", prompt="lane d")
    created = _workflow(swarm)
    db.docs[f"tasks/{done}"]["state"] = "SUCCEEDED"
    db.docs[f"tasks/{theirs}"]["submitted_by"] = "bob@saga.xyz"
    db.docs[f"tasks/{child}"]["parent_task_id"] = lane

    listing = sc.running_tasks(swarm)

    ids = [t["task_id"] for t in listing["tasks"]]
    assert ids == [lane], ids
    steps = {s["task_id"] for s in created["steps"]}
    assert not steps & set(ids), "a workflow's step is listed as a single task"
    (entry,) = listing["tasks"]
    assert entry["label"] == "lane-a"
    assert entry["state"] not in {"SUCCEEDED", "FAILED", "CANCELLED", "DEAD_LETTERED"}
    assert isinstance(entry["age_seconds"], int)
    assert listing["count"] == 1 and listing["complete"] is True
    assert listing["tenant_id"] == "eng"


def test_the_list_asks_only_existing_filters_for_unfinished_states():
    """`state` (one value per request) and `submitted_by=me` are what
    `GET /v1/tasks` takes; nothing else is asked, and no terminal state."""
    seen: list[str] = []

    class _Client:
        def request(self, method, path, **_):
            seen.append(path)
            return {"tasks": [], "next_page_token": None, "tenant_id": "eng"}

    listing = sc.running_tasks(_Client())
    assert listing == {"tenant_id": "eng", "count": 0, "complete": True, "tasks": []}
    asked = set()
    for path in seen:
        assert path.startswith("/v1/tasks?")
        params = dict(part.split("=", 1) for part in path.split("?", 1)[1].split("&"))
        assert set(params) == {"state", "submitted_by", "limit"}, params
        assert params["submitted_by"] == "me"
        asked.add(params["state"])
    assert asked == {"SUBMITTED", "QUEUED", "PARKED", "READY", "LEASED", "DISPATCHED", "STARTING", "RUNNING"}


def test_an_api_that_ignores_a_filter_still_lists_only_single_unfinished_tasks():
    rows = [
        {"id": "task_step", "state": "RUNNING", "workflow_id": "wf_1", "step_id": "a"},
        {"id": "task_done", "state": "SUCCEEDED"},
        {"id": "task_live", "state": "RUNNING", "metadata": {"unit": "  my   lane "},
         "created_at": "2026-10-07T10:00:00Z", "links": {"console": "https://c.test/agents/task_live"}},
    ]

    class _Client:
        def request(self, method, path, **_):
            return {"tasks": rows, "next_page_token": None, "tenant_id": "eng"}

    listing = sc.running_tasks(_Client())
    (entry,) = listing["tasks"]
    assert entry["task_id"] == "task_live"
    assert entry["label"] == "my lane"
    assert entry["console"] == "https://c.test/agents/task_live"


def test_a_list_that_pages_past_its_cap_says_it_stopped_short():
    class _Client:
        def request(self, method, path, **_):
            return {"tasks": [], "next_page_token": "more", "tenant_id": "eng"}

    listing = sc.running_tasks(_Client(), pages=2)
    assert listing["complete"] is False
    assert "older running tasks" in listing["incomplete_because"]


def test_a_failed_read_is_an_error_not_an_empty_list():
    class _Client:
        def request(self, *_, **__):
            raise SwarmError("GET /v1/tasks -> 503: unavailable")

    with pytest.raises(SwarmError):
        sc.running_tasks(_Client())


# --------------------------------------------------------------------------
# The hook's context
# --------------------------------------------------------------------------


def _session_start(client) -> tuple[int, str]:
    import io

    out = io.StringIO()
    args = sc.build_parser().parse_args(["workflows", "--session-start"])
    return sc.cmd_workflows(client, args, out), out.getvalue()


def test_session_start_names_a_running_single_task(swarm, api, db):  # noqa: F811
    lane = _dispatch(swarm, label="lane-a")
    code, text = _session_start(swarm)
    assert code == 0
    context = json.loads(text)["hookSpecificOutput"]["additionalContext"]
    assert lane in context and '"lane-a"' in context
    assert "1 SwarmCloud single task you dispatched is running" in context
    assert "/sc attach --all" in context and "auto_attach" in context


def test_session_start_names_workflows_and_single_tasks_together(swarm, api, db):  # noqa: F811
    lane = _dispatch(swarm, label="lane-a")
    created = _workflow(swarm)
    context = json.loads(_session_start(swarm)[1])["hookSpecificOutput"]["additionalContext"]
    assert created["workflow_id"] in context and lane in context
    assert "1 SwarmCloud workflow is running" in context


def test_session_start_names_the_tasks_when_the_workflow_read_fails():
    """Each read fails alone: a deployment whose workflow route is broken
    still gets the caller's single tasks named."""

    class _Client:
        def workflows(self, **_):
            raise SwarmError("GET /v1/workflows -> 503")

        def request(self, method, path, **_):
            if path.startswith("/v1/tasks?") and "state=RUNNING" in path:
                return {"tasks": [{"id": "task_x", "state": "RUNNING", "metadata": {"unit": "x"}}],
                        "next_page_token": None, "tenant_id": "eng"}
            if path.startswith("/v1/tasks?"):
                return {"tasks": [], "next_page_token": None, "tenant_id": "eng"}
            raise SwarmError("GET /v1/workflows -> 503")

    code, text = _session_start(_Client())
    assert code == 0
    assert "task_x" in json.loads(text)["hookSpecificOutput"]["additionalContext"]


def test_session_start_says_nothing_when_no_single_task_runs_either(swarm, api, db):  # noqa: F811
    done = _dispatch(swarm)
    db.docs[f"tasks/{done}"]["state"] = "CANCELLED"
    assert _session_start(swarm) == (0, "")


def test_sc_workflows_lists_my_single_tasks(swarm, api, db):  # noqa: F811
    import io

    lane = _dispatch(swarm, label="lane-a")
    out = io.StringIO()
    assert sc.cmd_workflows(swarm, sc.build_parser().parse_args(["workflows"]), out) == sc.EXIT_OK
    text = out.getvalue()
    assert "single tasks" in text.lower() and lane in text and "lane-a" in text
    out = io.StringIO()
    assert sc.cmd_workflows(swarm, sc.build_parser().parse_args(["workflows", "--json"]), out) == sc.EXIT_OK
    assert [t["task_id"] for t in json.loads(out.getvalue())["single_tasks"]["tasks"]] == [lane]


def test_the_mcp_list_tool_carries_my_single_tasks(swarm, api, db):  # noqa: F811
    lane = _dispatch(swarm, label="lane-a")
    reply = json.loads(server._call(swarm, "swarm_workflows", {}))
    assert [t["task_id"] for t in reply["single_tasks"]] == [lane]
    assert reply["single_tasks"][0]["label"] == "lane-a"
    assert reply["single_tasks_count"] == 1


# --------------------------------------------------------------------------
# The launches: /sc attach --all, and swarm_dispatch's own rows
# --------------------------------------------------------------------------


@pytest.fixture
def _plugin_root(monkeypatch, tmp_path):
    monkeypatch.setenv(launch.PLUGIN_ROOT_ENV, str(_PLUGIN))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))


def test_a_task_launch_titles_its_run_and_rows_from_the_labels(_plugin_root):
    out = launch.for_tasks([
        {"task_id": "task_a", "label": "lane-a", "console": "https://c.test/agents/task_a"},
        {"task_id": "task_b", "label": None},
    ])
    assert out["title"] == "SC · lane-a +1 · 2 tasks"
    assert out["args"] == {"attach_tasks": [
        {"task_id": "task_a", "title": "lane-a", "console": "https://c.test/agents/task_a"},
        {"task_id": "task_b", "title": "task_b"},
    ]}
    assert Path(out["script_path"]).is_file()
    assert launch.for_tasks([{"task_id": "task_a", "label": "lane-a"}])["title"] == "SC · lane-a · task"


def test_a_task_launch_follows_at_most_the_cap(_plugin_root):
    entries = [{"task_id": f"task_{n:02d}"} for n in range(launch.MAX_ATTACHED_TASKS + 2)]
    out = launch.for_tasks(entries)
    assert len(out["args"]["attach_tasks"]) == launch.MAX_ATTACHED_TASKS
    assert [e["task_id"] for e in out["not_followed"]] == ["task_10", "task_11"]


def test_attach_all_launches_one_more_run_for_my_single_tasks(_plugin_root, swarm, api, db):  # noqa: F811
    lane = _dispatch(swarm, label="lane-a")
    created = _workflow(swarm)
    reply = json.loads(server._call(swarm, "swarm_workflow_launch", {"attach": "all"}))
    by_args = [entry["args"] for entry in reply["launches"]]
    assert [a["attach"] for a in by_args if "attach" in a] == [created["workflow_id"]]
    (tasks,) = [a for a in by_args if "attach_tasks" in a]
    assert [t["task_id"] for t in tasks["attach_tasks"]] == [lane]
    assert tasks["attach_tasks"][0]["title"] == "lane-a"
    assert reply["single_tasks"] == 1


def test_a_dispatch_reply_starts_its_own_row(_plugin_root, swarm, api, db):  # noqa: F811
    payload = json.loads(server._call(swarm, "swarm_dispatch", {"prompt": "hi", "profile": "mock", "label": "lane-a"}))
    rows = payload["rows"]
    (sent,) = rows["args"]["attach_tasks"]
    assert (sent["task_id"], sent["title"]) == (payload["task_id"], "lane-a")
    assert sent.get("console") == payload.get("console"), "the row's link is the one the API served"
    assert Path(rows["script_path"]).is_file()
    assert "Workflow tool" in rows["start_now"]


def test_a_batch_dispatch_reply_starts_a_row_per_task(_plugin_root, swarm, api, db):  # noqa: F811
    payload = json.loads(server._call(swarm, "swarm_dispatch", {"tasks": [
        {"prompt": "one", "runner_profile": "mock", "label": "lane-1"},
        {"prompt": "two", "runner_profile": "mock"},
    ]}))
    sent = payload["rows"]["args"]["attach_tasks"]
    assert [t["task_id"] for t in sent] == payload["task_ids"]
    assert [t["title"] for t in sent] == ["lane-1", payload["task_ids"][1]]


def test_a_dispatch_outside_the_plugin_still_dispatches_and_says_how_to_get_a_row(swarm, api, db, monkeypatch):  # noqa: F811
    monkeypatch.delenv(launch.PLUGIN_ROOT_ENV, raising=False)
    payload = json.loads(server._call(swarm, "swarm_dispatch", {"prompt": "hi", "profile": "mock"}))
    assert payload["task_id"].startswith("task_")
    assert payload["rows"]["attach_with"] == "/sc attach --all"
    assert launch.PLUGIN_ROOT_ENV in payload["rows"]["error"]


def test_the_dispatch_tool_tells_the_session_to_start_the_rows():
    (tool,) = [t for t in server.TOOLS if t["name"] == "swarm_dispatch"]
    assert "`rows`" in tool["description"] and "Workflow tool" in tool["description"]


# --------------------------------------------------------------------------
# run.js
# --------------------------------------------------------------------------


def _run(tmp_path, args, answers) -> dict:
    harness = tmp_path / "harness.cjs"
    harness.write_text(_BY_PROMPT)
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps({"args": args, "answers": answers}))
    done = subprocess.run(
        [_node(), str(harness), str(_RUN_JS), str(fixture)],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_attach_tasks_starts_one_task_row_per_task_titled_from_its_label(tmp_path):
    link = "https://c.test/agents/task_a"
    got = _run(tmp_path, {"attach_tasks": [
        {"task_id": "task_a", "title": "lane-a", "console": link},
        {"task_id": "task_b", "title": "lane-b"},
        {"task_id": "task_a", "title": "again"},
    ]}, {
        "task_id: task_a": _step("SUCCEEDED", cost_usd=0.2),
        "task_id: task_b": _step("FAILED", last_error="boom"),
    })
    assert "error" not in got, got.get("error")
    calls = got["calls"]
    assert [c["agentType"] for c in calls] == ["sc:task", "sc:task"], "one row per task, the duplicate dropped"
    assert calls[0]["label"] == f"[SwarmCloud] lane-a · task · {link}"
    assert calls[1]["label"] == "[SwarmCloud] lane-b · task"
    assert calls[0]["prompt"] == f"task_id: task_a\nconsole: {link}"
    assert calls[1]["prompt"] == "task_id: task_b\nconsole: none"
    assert all("step_id" not in c["prompt"] for c in calls)
    assert got["phases"] == ["Tasks"]
    result = got["result"]
    assert result["state"] == "FOLLOWED"
    assert [(t["task_id"], t["state"]) for t in result["tasks"]] == [("task_a", "SUCCEEDED"), ("task_b", "FAILED")]


def test_attach_tasks_naming_no_task_is_refused(tmp_path):
    got = _run(tmp_path, {"attach_tasks": []}, {})
    assert "attach_tasks" in got["error"] and got["calls"] == []


def test_attach_all_by_name_returns_the_call_for_my_single_tasks(tmp_path):
    listed = _listed("wf_a")
    listed["single_tasks"] = [
        {"task_id": "task_a", "label": "lane-a", "state": "RUNNING", "console": None},
        {"task_id": "task_b", "label": None, "state": "QUEUED", "console": "https://c.test/agents/task_b"},
    ]
    listed["single_tasks_error"] = None
    got = _run(tmp_path, {"attach": "all"}, {"LIST": listed})
    assert "error" not in got, got.get("error")
    result = got["result"]
    assert result["state"] == "LISTED"
    assert result["attach_calls"] == [{"attach": "wf_a", "title": "label-wf_a"}]
    assert result["task_call"] == {"attach_tasks": [
        {"task_id": "task_a", "title": "lane-a"},
        {"task_id": "task_b", "title": "task_b", "console": "https://c.test/agents/task_b"},
    ]}


def test_attach_all_with_only_single_tasks_running_is_not_nothing_running(tmp_path):
    listed = _listed()
    listed["single_tasks"] = [{"task_id": "task_a", "label": "lane-a", "state": "RUNNING", "console": None}]
    got = _run(tmp_path, {"attach": "all"}, {"LIST": listed})
    assert got["result"]["state"] == "LISTED"
    assert got["result"]["attach_calls"] == []
    assert got["result"]["task_call"]["attach_tasks"][0]["task_id"] == "task_a"


def test_the_run_scripts_task_cap_is_the_bridges():
    assert f"const MAX_ATTACHED_TASKS = {launch.MAX_ATTACHED_TASKS}" in _RUN_JS.read_text()


# --------------------------------------------------------------------------
# sc:task follows without step_id
# --------------------------------------------------------------------------


def test_the_task_row_never_passes_a_step_id_or_parents():
    fields, body = _load(_TASK_MD)
    flat = " ".join(body.split())
    assert "NEVER pass `step_id` or `parents`" in flat
    assert '`step_id: "<step_id>"`' not in flat, "sc:task was told to pass a step_id"
    assert "parents: [" not in flat
    assert '`format: "progress"`' in flat
    assert fields["tools"] == ["mcp__plugin_sc_swarmcloud__swarm_follow", "StructuredOutput"]


def test_the_list_relay_relays_the_single_tasks():
    _, body = _load(_PLUGIN / "agents" / "workflow.md")
    assert "`single_tasks`" in body and "`single_tasks_error`" in body
