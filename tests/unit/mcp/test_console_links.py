"""Every answer about a task or a workflow carries the console link the API served.

Owner decision, 2026-10-01: the console link comes FROM THE API, which puts
`links.console` on every task (`<origin>/agents/live/<task_id>`) and every
workflow and workflow step (`<origin>/workflows/<workflow_id>`) it serves, or
null when the deployment has no console. The MCP tools, `swarm`/`sc` and the
plugin print what the API served and NEVER rebuild the host or the path: a host
spelled in five places is five places to drift. A null or missing link is
OMITTED -- never invented -- and the link appears whatever the task's state,
because a QUEUED task is exactly the one a person opens the console to watch.

The links below are deliberately on a host and path no code here could derive
(`console.example.test/served/...`): a consumer that built its own link instead
of copying the served one would print a different string and fail.
"""

from __future__ import annotations

import argparse
import io
import json
import re
from pathlib import Path

import pytest

from swarm_mcp import cli, compact, sc, server

from test_follow_cursor import NOW, World, swarm, world  # noqa: F401 - fixtures
from test_sc_config_and_debug import FakeTaskApi

_REPO = Path(__file__).resolve().parents[3]
_RUN_JS = _REPO / "plugin" / "workflows" / "run.js"
_STEP_MD = _REPO / "plugin" / "agents" / "step.md"


def _served(task_id: str) -> str:
    """A link only the API could have handed over: no consumer derives this host."""
    return f"https://console.example.test/served/agent/{task_id}?from=api"


def _served_workflow(workflow_id: str) -> str:
    return f"https://console.example.test/served/flow/{workflow_id}?from=api"


def _link_tasks(swarm, monkeypatch, *, link: bool) -> None:  # noqa: F811 - fixture name
    """Every task the real API serves to this client carries `links.console`."""
    original = swarm.task

    def task(task_id):
        doc = dict(original(task_id))
        doc["links"] = {"console": _served(task_id) if link else None}
        return doc

    monkeypatch.setattr(swarm, "task", task)


def _seed(world, state: str, task_id: str = "task_a") -> None:  # noqa: F811 - fixture name
    world.task(task_id, state=state)
    doc = world.db.docs[f"tasks/{task_id}"]
    doc["step_id"] = "review"
    if state not in ("QUEUED", "READY", "PARKED"):
        doc["started_at"] = NOW
        doc["attempt_count"] = 1
        doc["max_attempts"] = 3


_STATES = ["QUEUED", "RUNNING", "SUCCEEDED"]


# --------------------------------------------------------------------------
# swarm_status
# --------------------------------------------------------------------------


@pytest.mark.parametrize("state", _STATES)
def test_status_carries_the_served_console_link_in_every_state(swarm, world, monkeypatch, state):
    _seed(world, state)
    _link_tasks(swarm, monkeypatch, link=True)
    (row,) = json.loads(server._call(swarm, "swarm_status", {"task_ids": ["task_a"]}))
    assert row["console"] == _served("task_a")


def test_status_omits_a_null_console_link(swarm, world, monkeypatch):
    _seed(world, "QUEUED")
    _link_tasks(swarm, monkeypatch, link=False)
    (row,) = json.loads(server._call(swarm, "swarm_status", {"task_ids": ["task_a"]}))
    assert "console" not in row


# --------------------------------------------------------------------------
# swarm_follow, every format
# --------------------------------------------------------------------------


def _follow(swarm, fmt: str | None, **extra):  # noqa: F811 - fixture name
    args = {"task_ids": ["task_a"], **extra}
    if fmt is not None:
        args["format"] = fmt
    return json.loads(server._call(swarm, "swarm_follow", args))


@pytest.mark.parametrize("state", _STATES)
@pytest.mark.parametrize("fmt", [None, "json", "lines", "progress"])
def test_follow_carries_the_console_link_in_every_format_and_state(swarm, world, monkeypatch, fmt, state):
    _seed(world, state)
    _link_tasks(swarm, monkeypatch, link=True)
    reply = _follow(swarm, fmt)
    (row,) = reply["tasks"]
    assert row["console"] == _served("task_a"), row


@pytest.mark.parametrize("fmt", [None, "json", "lines", "progress"])
def test_follow_omits_a_null_console_link_in_every_format(swarm, world, monkeypatch, fmt):
    _seed(world, "RUNNING")
    _link_tasks(swarm, monkeypatch, link=False)
    text = json.dumps(_follow(swarm, fmt))
    reply = json.loads(text)
    assert "console" not in reply["tasks"][0]
    assert "console:" not in text, "a null link printed a console line"


@pytest.mark.parametrize("state", _STATES)
def test_the_progress_formats_first_line_carries_the_console_link(swarm, world, monkeypatch, state):
    """The row's FIRST progress line names where to watch it, from the moment
    the task id is known -- a QUEUED step included."""
    _seed(world, state)
    _link_tasks(swarm, monkeypatch, link=True)
    reply = _follow(swarm, "progress", step_id="review")
    (line,) = reply["progress"]
    assert line.endswith("console: " + _served("task_a")), line


def test_the_progress_formats_final_line_keeps_the_console_link(swarm, world, monkeypatch):
    _seed(world, "RUNNING")
    _link_tasks(swarm, monkeypatch, link=True)
    first = compact.watch_progress(swarm, ["task_a"], step_id="review")
    world.set_state("task_a", "SUCCEEDED")
    world.db.docs["tasks/task_a"]["completed_at"] = NOW
    last = compact.watch_progress(swarm, ["task_a"], since=first["since"], step_id="review")
    assert last["stop"] is True
    (line,) = last["progress"]
    assert "SUCCEEDED" in line and line.endswith("console: " + _served("task_a")), line
    assert last["tasks"][0]["outcome"]["console"] == _served("task_a")


def test_the_lines_format_says_the_console_link_once(swarm, world, monkeypatch):
    _seed(world, "RUNNING")
    _link_tasks(swarm, monkeypatch, link=True)
    first = _follow(swarm, "lines")
    said = [line for line in first["lines"] if "console: " in line]
    assert said == ["[review task_a] console: " + _served("task_a")], first["lines"]
    again = _follow(swarm, "lines", since=first["since"])
    assert not [line for line in again["lines"] if "console: " in line], again["lines"]


# --------------------------------------------------------------------------
# swarm_result
# --------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["QUEUED", "SUCCEEDED"])
def test_result_carries_the_console_link(swarm, world, monkeypatch, state):
    _seed(world, state)
    _link_tasks(swarm, monkeypatch, link=True)
    body = json.loads(server._call(swarm, "swarm_result", {"task_id": "task_a"}))
    assert body["console"] == _served("task_a")


def test_result_omits_a_null_console_link(swarm, world, monkeypatch):
    _seed(world, "SUCCEEDED")
    _link_tasks(swarm, monkeypatch, link=False)
    body = json.loads(server._call(swarm, "swarm_result", {"task_id": "task_a"}))
    assert "console" not in body


# --------------------------------------------------------------------------
# swarm_dispatch, single and batch
# --------------------------------------------------------------------------


class _DispatchApi:
    """Answers a dispatch the way `SwarmClient` hands it back, with the link."""

    def __init__(self, *, link: bool) -> None:
        self.link = link
        self.count = 0

    def _created(self) -> dict:
        self.count += 1
        task_id = f"task_{self.count}"
        return {"id": task_id, "state": "QUEUED",
                "links": {"console": _served(task_id) if self.link else None}}

    def dispatch(self, **kwargs):  # noqa: ARG002
        return self._created()

    def dispatch_batch(self, tasks):
        return [self._created() for _ in tasks]

    def request(self, method, path, *, payload=None, timeout=60):  # noqa: ARG002
        return {"limits": {"max_batch_size": 50}}


@pytest.mark.parametrize("link", [True, False])
def test_dispatch_answers_the_served_console_link_or_none(link):
    body = json.loads(server._call(_DispatchApi(link=link), "swarm_dispatch", {"prompt": "console link probe"}))
    if link:
        assert body["console"] == _served("task_1")
    else:
        assert "console" not in body


@pytest.mark.parametrize("link", [True, False])
def test_a_dispatched_batch_carries_each_tasks_console_link(link):
    tasks = [{"prompt": "console link probe one"}, {"prompt": "console link probe two"}]
    body = json.loads(server._call(_DispatchApi(link=link), "swarm_dispatch", {"tasks": tasks}))
    for row in body["tasks"]:
        if link:
            assert row["console"] == _served(row["task_id"])
        else:
            assert "console" not in row


# --------------------------------------------------------------------------
# swarm_workflow and swarm_workflow_status
# --------------------------------------------------------------------------


def _workflow_envelope(*, link: bool) -> dict:
    def served(url):
        return {"console": url if link else None}

    return {
        "workflow": {
            "workflow_id": "wf_x",
            "state": "RUNNING",
            "state_source": "derived",
            "cancel_requested": False,
            "on_step_failure": "fail_workflow",
            "links": served(_served_workflow("wf_x")),
            "steps": [
                {"step_id": "a", "task_id": "task_a", "runner_profile": "mock",
                 "depends_on": [], "input_from": {}, "links": served(_served("task_a"))},
                {"step_id": "b", "task_id": "task_b", "runner_profile": "mock",
                 "depends_on": ["a"], "input_from": {}, "links": served(_served("task_b"))},
                # A step with no task yet: the API serves a null link for it.
                {"step_id": "c", "task_id": None, "runner_profile": "mock",
                 "depends_on": ["b"], "input_from": {}, "links": {"console": None}},
            ],
        },
        "tasks": [
            {"id": "task_a", "state": "SUCCEEDED", "links": served(_served("task_a"))},
            {"id": "task_b", "state": "QUEUED", "links": served(_served("task_b"))},
        ],
        "dispatch": {"strategy": "collect"},
    }


class _WorkflowApi:
    def __init__(self, *, link: bool) -> None:
        self.envelope = _workflow_envelope(link=link)

    def request(self, method, path, *, payload=None, timeout=60):  # noqa: ARG002
        return self.envelope


def _steps_by_id(body) -> dict:
    return {row["step_id"]: row for row in body["steps"]}


def test_workflow_submit_carries_the_workflow_and_each_steps_console_link():
    body = json.loads(server._call(_WorkflowApi(link=True), "swarm_workflow",
                                   {"steps": [{"step_id": "a", "prompt": "console link probe"}]}))
    assert body["console"] == _served_workflow("wf_x")
    steps = _steps_by_id(body)
    assert steps["a"]["console"] == _served("task_a")
    assert steps["b"]["console"] == _served("task_b")
    assert "console" not in steps["c"], "a step with no task has no link, and none is invented"


@pytest.mark.parametrize("tool", ["swarm_workflow_status", "swarm_workflow_result"])
def test_workflow_status_carries_the_workflow_and_each_steps_console_link(tool):
    body = json.loads(server._call(_WorkflowApi(link=True), tool, {"workflow_id": "wf_x"}))
    assert body["console"] == _served_workflow("wf_x")
    steps = _steps_by_id(body)
    assert steps["a"]["console"] == _served("task_a")
    assert steps["b"]["console"] == _served("task_b")
    assert "console" not in steps["c"]


@pytest.mark.parametrize("tool", ["swarm_workflow", "swarm_workflow_status"])
def test_workflow_tools_omit_null_console_links(tool):
    args = ({"steps": [{"step_id": "a", "prompt": "console link probe"}]}
            if tool == "swarm_workflow" else {"workflow_id": "wf_x"})
    body = json.loads(server._call(_WorkflowApi(link=False), tool, args))
    assert "console" not in body
    assert all("console" not in row for row in body["steps"])


# --------------------------------------------------------------------------
# swarm status / swarm follow / sc debug
# --------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["QUEUED", "SUCCEEDED"])
def test_swarm_status_prints_the_console_link(swarm, world, monkeypatch, capsys, state):
    _seed(world, state)
    _link_tasks(swarm, monkeypatch, link=True)
    cli.cmd_status(swarm, argparse.Namespace(task_ids=["task_a"]))
    printed = capsys.readouterr().out
    assert "console: " + _served("task_a") in printed.splitlines()[1], printed


def test_swarm_status_prints_no_console_line_for_a_null_link(swarm, world, monkeypatch, capsys):
    _seed(world, "QUEUED")
    _link_tasks(swarm, monkeypatch, link=False)
    cli.cmd_status(swarm, argparse.Namespace(task_ids=["task_a"]))
    assert "console" not in capsys.readouterr().out


def _follow_args():
    return type("A", (), {"task_ids": ["task_a"], "interval": 0.0, "once": True,
                          "max_log_bytes": 20_000, "verbose": False})()


@pytest.mark.parametrize("link", [True, False])
def test_swarm_follow_prints_the_console_link_or_nothing(swarm, world, monkeypatch, capsys, link):
    _seed(world, "QUEUED")
    _link_tasks(swarm, monkeypatch, link=link)
    cli.cmd_follow(swarm, _follow_args())
    printed = capsys.readouterr().out
    if link:
        assert "[review task_a] console: " + _served("task_a") in printed.splitlines(), printed
    else:
        assert "console" not in printed


class _LinkedTaskApi(FakeTaskApi):
    def __init__(self, link):
        super().__init__()
        self.link = link

    def task(self, task_id):
        return {**super().task(task_id), "links": {"console": _served(task_id) if self.link else None}}


@pytest.mark.parametrize("link", [True, False])
def test_sc_debug_prints_the_console_link_or_nothing(link):
    out = io.StringIO()
    args = sc.build_parser().parse_args(["debug", "task_1"])
    args.func(_LinkedTaskApi(link), args, out)
    text = out.getvalue()
    if link:
        assert "  console: " + _served("task_1") in text.splitlines(), text
    else:
        assert "console" not in text
    body = json.loads(server._call(_LinkedTaskApi(link), "swarm_debug", {"task_id": "task_1"}))
    assert body["task"]["value"].get("console") == (_served("task_1") if link else None)


# --------------------------------------------------------------------------
# The plugin: the step row and run.js print what the bridge served
# --------------------------------------------------------------------------


def test_step_md_prints_the_served_console_link_first_and_last():
    flat = " ".join(_STEP_MD.read_text().split())
    first, _, rest = flat.partition("## 2. Follow it until it stops")
    assert "console" in first, "the row's first line must carry the step's console link"
    answer = rest.partition("## 3. Answer")[2]
    assert "`console`" in answer and "tasks[0].console" in answer, "the row's answer carries the link"
    assert "never build" in flat.lower(), "step.md must say the link is copied, not built"
    assert "/agents/" not in flat


def test_run_js_never_builds_a_console_link():
    source = _RUN_JS.read_text()
    assert "/agents/live/" not in source and "'/agents/" not in source
    assert "'/workflows/'" not in source
    assert not re.search(r"['\"]https?://", source), "run.js must not spell a host"


def _plugin_harness():
    import test_plugin_agents_and_workflows as plugin

    return plugin


def test_run_js_prints_the_console_links_on_submit_step_and_result_lines(tmp_path):
    plugin = _plugin_harness()
    submitted = {
        **plugin._SUBMITTED,
        "console": _served_workflow("wf_1"),
        "steps": [{**s, "console": _served(s["task_id"])} for s in plugin._SUBMITTED["steps"]],
    }
    answers = {
        **plugin._ANSWERS,
        "SUBMIT": submitted,
        "step:scan-01": {**plugin._ANSWERS["step:scan-01"], "console": _served("task_1")},
        "STATUS": {**plugin._ANSWERS["STATUS"], "console": _served_workflow("wf_1")},
    }
    got = plugin._run(tmp_path, plugin._SPEC, answers)
    assert "error" not in got, got.get("error")
    logs = got["logs"]
    submit_line = next(line for line in logs if " submitted · " in line)
    assert submit_line.endswith("console: " + _served_workflow("wf_1")), submit_line
    assert logs[-1] == "wf_1 FAILED · console: " + _served_workflow("wf_1"), logs
    # The step's own final line: from its row's answer...
    scan = next(line for line in logs if line.startswith("scan-01 "))
    assert scan.endswith("console: " + _served("task_1")), scan
    # ...and, for a row that answered none (or failed), from the submit reply.
    join = next(line for line in logs if line.startswith("join "))
    assert join.endswith("console: " + _served("task_3")), join
    schemas = {c["agentType"] + ":" + c["prompt"].split("\n")[0]: c["schema"] for c in got["calls"]}
    assert "console" in schemas["sc:workflow:SUBMIT"] and "console" in schemas["sc:workflow:STATUS"]
    # The row answers `{state, result}` (B2, 2026-10-05); the link is in `result`.
    assert all(c["schema"] == ["state", "result"] for c in got["calls"] if c["agentType"] == "sc:step")
    source = plugin._RUN_JS.read_text()
    assert "    console: { type: 'string' }," in source[source.index("const STEP_FIELDS = {"):source.index("const STEP_RESULT = {")]


def test_run_js_prints_no_console_link_when_none_was_served(tmp_path):
    plugin = _plugin_harness()
    got = plugin._run(tmp_path, plugin._SPEC, plugin._ANSWERS)
    assert "error" not in got, got.get("error")
    assert not [line for line in got["logs"] if "console" in line], got["logs"]


def test_the_workflow_relay_is_told_to_copy_the_console_link_and_never_build_one():
    """run.js's SUBMIT and STATUS schemas require `console`; the relay that
    answers them (plugin/agents/workflow.md) must be told where it comes from,
    or a required field it was never told about is one it fills in itself."""
    body = (Path(__file__).resolve().parents[3] / "plugin" / "agents" / "workflow.md").read_text()
    submit = " ".join(body.split("## SUBMIT", 1)[1].split("## STATUS", 1)[0].split())
    status = " ".join(body.split("## STATUS", 1)[1].split())
    for section in (submit, status):
        assert "`console` — the reply's `console`, copied character for character" in section
        assert "never build one" in section.lower()
    assert "and its `console`" in submit, "each submitted step's link must be relayed too"
