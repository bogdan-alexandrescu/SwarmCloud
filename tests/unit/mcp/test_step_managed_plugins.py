"""A step's managed plugins travel from the sc plugin, and swarm_result shows what they ran.

swarm-api's task create and workflow step take `plugins` (`{install_id:
{overrides?}}` for a managed install, `{name: {mode, skills}}` for a built-in)
and `plugins_off` (a list of names), but a spec step refused both keys and
swarm_dispatch had no such field, so on 11 November 2026 the end-to-end test
of docs/design/managed-plugins.md section 16 posted its task to the API by
hand; and swarm_result did not show the attempt summary's `plugins` record.

These pin that both fields are sent as given, top-level on the step or task;
that a malformed one is refused before anything is sent; that a spec or a
dispatch naming neither sends exactly today's body; and that swarm_result
shows the record's names, versions, digests and counts, and nothing else.
"""

from __future__ import annotations

import copy
import json
import secrets
from typing import Any

import pytest

from swarm_mcp import server, workflows
from swarm_mcp.client import SwarmClient, SwarmError

from test_follow_cursor import NOW, World, swarm, world  # noqa: F401 - fixtures

REPO = "https://github.com/example/widgets"
INSTALL = "inst_belay"
PLUGINS = {INSTALL: {"overrides": {"CHECK_MODE": "strict"}}, "gstack": {"mode": "on", "skills": ["qa"]}}
PLUGINS_OFF = ["inst_enforce"]


class _Workflows:
    """The workflow route's wire: every body as sent."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def request(self, method, path, payload=None, **_):
        self.sent.append(copy.deepcopy(payload))
        return {"workflow": {"workflow_id": "wf_1", "steps": []}}


def _submit(steps: list[dict[str, Any]]) -> dict[str, Any]:
    wire = _Workflows()
    spec = workflows.read_spec({"steps": steps, "strategy": "collect"})
    workflows.submit(wire, **spec)
    (body,) = wire.sent
    return body


def _spec_steps(**build: Any) -> list[dict[str, Any]]:
    return [
        {"step_id": "build", "prompt": "sort the widgets", "runner_profile": "claude-code", **build},
        {"step_id": "review", "prompt": "review it", "depends_on": ["build"]},
    ]


# -- a workflow spec's step ---------------------------------------------------


def test_a_spec_step_with_plugins_and_plugins_off_is_sent_with_both():
    body = _submit(_spec_steps(plugins=PLUGINS, plugins_off=PLUGINS_OFF))
    by_id = {s["step_id"]: s for s in body["steps"]}
    # MUTATION: leave either key out of `_STEP_KEYS` and the spec is refused;
    # leave it out of `build_steps` and the step goes without it.
    assert by_id["build"]["plugins"] == PLUGINS
    assert by_id["build"]["plugins_off"] == PLUGINS_OFF
    assert "plugins" not in by_id["review"] and "plugins_off" not in by_id["review"]
    assert "plugins" not in (by_id["build"].get("metadata") or {})
    assert "plugins" not in by_id["build"]["input"]


def test_an_empty_plugins_off_is_sent_as_given():
    body = _submit(_spec_steps(plugins_off=[]))
    assert body["steps"][0]["plugins_off"] == []


@pytest.mark.parametrize("field, value", [
    ("plugins", ["inst_belay"]),
    ("plugins", "inst_belay"),
    ("plugins", {"inst_belay": "on"}),
    ("plugins", {"inst_belay": ["overrides"]}),
    ("plugins", {"": {}}),
    ("plugins", None),
    ("plugins_off", "inst_enforce"),
    ("plugins_off", {"inst_enforce": {}}),
    ("plugins_off", ["inst_enforce", 3]),
    ("plugins_off", [" "]),
    ("plugins_off", None),
])
def test_a_malformed_one_is_refused_before_anything_is_sent(field, value):
    wire = _Workflows()
    with pytest.raises(SwarmError) as refused:
        spec = workflows.read_spec({"steps": _spec_steps(**{field: value}), "strategy": "collect"})
        workflows.submit(wire, **spec)
    assert wire.sent == []
    assert "step 'build'" in str(refused.value) and field in str(refused.value)


#: What a spec naming no plugins sent before the fields existed, byte for byte.
TODAY = (
    '{"steps": [{"step_id": "build", "runner_profile": "claude-code", "input": {"prompt": '
    '"sort the widgets"}, "depends_on": [], "input_from": {}}, {"step_id": "review", '
    '"runner_profile": "claude-code", "input": {"prompt": "review it"}, "depends_on": '
    '["build"], "input_from": {}}]'
)


def test_a_spec_without_them_sends_todays_steps_byte_for_byte():
    body = _submit(_spec_steps())
    assert json.dumps({"steps": body["steps"]}).startswith(TODAY)
    assert json.dumps({"steps": body["steps"]}) == TODAY + "}"


def test_the_terminal_lists_the_two_keys_as_a_steps_own():
    assert {"plugins", "plugins_off"} <= workflows._STEP_KEYS


# -- swarm_dispatch -------------------------------------------------------------


class _Wire:
    """`SwarmClient.dispatch` over a recorded transport."""

    dispatch = SwarmClient.dispatch

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, Any]] = []

    def request(self, method: str, path: str, payload: Any = None, **_: Any) -> dict:
        self.sent.append((method, path, copy.deepcopy(payload)))
        if path == "/v1/stats":
            return {"limits": {"max_batch_size": 5}}
        return {"task": {"id": f"task_{len(self.sent)}", "state": "QUEUED"}}

    def dispatch_batch(self, payloads):
        self.sent.append(("POST", "/v1/tasks/batch", copy.deepcopy(payloads)))
        return [{"id": f"task_b{i}", "state": "QUEUED"} for i, _ in enumerate(payloads)]


def _posts(wire: _Wire) -> list[tuple[str, Any]]:
    return [(path, payload) for method, path, payload in wire.sent if method == "POST"]


def _prompt() -> str:
    # A fresh prompt each time, so the session's repeat guard never refuses.
    return f"sort the widgets {secrets.token_hex(4)}"


def test_dispatch_sends_both_top_level_when_given():
    wire = _Wire()
    server._call(wire, "swarm_dispatch", {"prompt": _prompt(), "repo": REPO,
                                          "plugins": PLUGINS, "plugins_off": PLUGINS_OFF})
    ((path, body),) = _posts(wire)
    assert path == "/v1/tasks"
    # MUTATION: drop either from `task_payload` and this is a KeyError.
    assert body["plugins"] == PLUGINS
    assert body["plugins_off"] == PLUGINS_OFF
    assert "plugins" not in body["input"] and "plugins" not in body["metadata"]


def test_dispatch_without_them_sends_todays_body():
    wire = _Wire()
    prompt = _prompt()
    server._call(wire, "swarm_dispatch", {"prompt": prompt, "repo": REPO})
    ((_, body),) = _posts(wire)
    assert set(body) == {"runner_profile", "input", "metadata", "repository_url"}
    assert body["input"] == {"prompt": prompt}


def test_each_batch_task_sends_them_only_when_given():
    wire = _Wire()
    server._call(wire, "swarm_dispatch", {"tasks": [
        {"prompt": _prompt(), "plugins": PLUGINS},
        {"prompt": _prompt(), "plugins_off": PLUGINS_OFF},
        {"prompt": _prompt()},
    ]})
    ((path, bodies),) = [(p, b) for p, b in _posts(wire) if p == "/v1/tasks/batch"]
    assert bodies[0]["plugins"] == PLUGINS and "plugins_off" not in bodies[0]
    assert bodies[1]["plugins_off"] == PLUGINS_OFF and "plugins" not in bodies[1]
    assert "plugins" not in bodies[2] and "plugins_off" not in bodies[2]


@pytest.mark.parametrize("args", [
    {"plugins": ["inst_belay"]},
    {"plugins": {"inst_belay": "on"}},
    {"plugins_off": "inst_enforce"},
])
def test_dispatch_refuses_a_malformed_one_before_anything_is_sent(args):
    wire = _Wire()
    with pytest.raises(SwarmError):
        server._call(wire, "swarm_dispatch", {"prompt": _prompt(), **args})
    assert _posts(wire) == []
    batch = _Wire()
    with pytest.raises(SwarmError):
        server._call(batch, "swarm_dispatch", {"tasks": [{"prompt": _prompt(), **args}]})
    assert _posts(batch) == []


def test_the_same_prompt_with_other_plugins_is_not_a_repeat():
    wire = _Wire()
    prompt = _prompt()
    server._call(wire, "swarm_dispatch", {"prompt": prompt, "repo": REPO})
    server._call(wire, "swarm_dispatch", {"prompt": prompt, "repo": REPO, "plugins": PLUGINS})
    assert len(_posts(wire)) == 2


def test_the_tool_descriptions_name_both_fields():
    tools = {t["name"]: t for t in server.TOOLS}
    dispatch = tools["swarm_dispatch"]["inputSchema"]["properties"]
    assert {"plugins", "plugins_off"} <= set(dispatch)
    assert {"plugins", "plugins_off"} <= set(dispatch["tasks"]["items"]["properties"])
    step = tools["swarm_workflow"]["inputSchema"]["properties"]["steps"]["items"]["properties"]
    assert {"plugins", "plugins_off"} <= set(step)


# -- swarm_result ---------------------------------------------------------------


def _digest() -> str:
    return secrets.token_hex(32)


def _finish(world: World, task_id: str, summary: dict[str, Any]) -> None:
    world.task(task_id, state="SUCCEEDED")
    doc = world.db.docs[f"tasks/{task_id}"]
    doc["runner_profile"] = "claude-code"
    doc["started_at"] = NOW
    doc["completed_at"] = NOW.replace(minute=4)
    doc["result_summary"] = {"runner": {"summary": "done"}, "artifacts": [], "logs": {}, **summary}


def _result(swarm, task_id: str) -> dict[str, Any]:
    return json.loads(server._call(swarm, "swarm_result", {"task_id": task_id}))


def test_swarm_result_shows_a_served_plugins_record(swarm, world):
    digest = _digest()
    record = {
        INSTALL: {
            "managed": True, "name": "jev-belay", "scope": "platform", "version_id": "0.2.0",
            "runnable_sha256": digest,
            "hooks": {"Stop": {"runs": 2, "blocks": 1, "errors": 0}},
            "mcp_servers_started": ["typesafe"], "skill_runs": {"check": 3},
            "secrets_missing": 1, "adapter": "typesafe",
        },
        "gstack": {"version": "1.4.0", "mode": "on", "skills": ["qa"], "skill_runs": {"qa": 1}},
    }
    _finish(world, "task_p", {"plugins": record})
    shown = _result(swarm, "task_p")
    # MUTATION: leave the record out of `_result_of` and this is a KeyError.
    assert shown["plugins"] == {
        INSTALL: {
            "name": "jev-belay", "version_id": "0.2.0", "runnable_sha256": digest,
            "hooks": {"Stop": {"runs": 2, "blocks": 1, "errors": 0}},
            "mcp_servers_started": ["typesafe"], "skill_runs": {"check": 3},
        },
        "gstack": {"version": "1.4.0", "skill_runs": {"qa": 1}},
    }


def test_swarm_result_says_nothing_of_plugins_when_there_is_none(swarm, world):
    _finish(world, "task_none", {})
    # MUTATION: show an empty record for every task and this key exists.
    assert "plugins" not in _result(swarm, "task_none")
    _finish(world, "task_empty", {"plugins": {}})
    assert "plugins" not in _result(swarm, "task_empty")
