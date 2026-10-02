"""Running SwarmCloud workflows show in Claude Code automatically (owner decision, 2026-10-02).

Lane B45, functionality wave 3. Four pieces, each held here:

* THE LIST. `sc workflows` and the MCP tool `swarm_workflows` list the
  caller's tenant's NON-TERMINAL workflows -- id, label, current step(s) and
  state, age, and the console link the API served -- through
  `GET /v1/workflows?state=...`. Checked against the REAL swarm-api, because
  "only this tenant's, only the unfinished ones" is a property of the route
  and the bridge together, and a fake would agree with whoever wrote it.
* `/sc attach --all`. `/sc:swarmcloud {attach: "all"}` lists them once and
  attaches every one, up to a cap, in ONE run: the same slim `sc:step` row per
  unfinished step that a single attach starts. The rest are listed, not
  followed.
* THE SESSION-START HOOK. `plugin/hooks/session-start.sh` asks the bridge's CLI
  (read-only, short timeout) and hands the session `additionalContext` naming
  the running workflows and telling it to run `/sc attach --all` first. It is
  silent and exits 0 when none run, when anything fails, and when the plugin's
  `auto_attach` option is off.
* THE VERSION. A plugin change that does not move the version is never
  published; the hook reads the bridge pin out of plugin.json rather than
  restating it, so the pin still has one home.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest

from swarm_common.states import TERMINAL_STATES
from swarm_mcp import sc, server
from swarm_mcp.client import SwarmClient, SwarmError

# The real application and SwarmClient's real transport into it; imported so
# pytest finds the fixtures here too (`tests/unit` is on sys.path via conftest).
from test_against_the_real_api import AUTH, DAG, api, db, swarm  # noqa: F401
from test_plugin_agents_and_workflows import _HARNESS, _RUN_JS, _node, _step

_REPO = Path(__file__).resolve().parents[3]
_PLUGIN = _REPO / "plugin"
_MANIFEST = _PLUGIN / ".claude-plugin" / "plugin.json"
_HOOKS_JSON = _PLUGIN / "hooks" / "hooks.json"
_HOOK = _PLUGIN / "hooks" / "session-start.sh"

_TERMINAL = {s.value for s in TERMINAL_STATES}


# --------------------------------------------------------------------------
# The list, against the real API
# --------------------------------------------------------------------------


def _submit(swarm, label: str | None = None) -> dict:
    spec = json.loads(json.dumps(DAG))
    if label:
        spec["label"] = label
    return json.loads(server._call(swarm, "swarm_workflow", spec))


def _finish(db, created: dict, state: str = "SUCCEEDED") -> None:
    for step in created["steps"]:
        db.docs[f"tasks/{step['task_id']}"]["state"] = state


def _other_tenants_copy(db, workflow_id: str) -> str:
    """A running workflow that belongs to another tenant: the same document
    under a new id and another tenant_id, so only the tenant differs."""
    other = dict(db.docs[f"workflows/{workflow_id}"])
    other["workflow_id"] = "wf_othertenant000000000"
    other["tenant_id"] = "someone-else"
    db.docs[f"workflows/{other['workflow_id']}"] = other
    return other["workflow_id"]


def test_the_list_route_filters_by_the_derived_state(swarm, api, db):  # noqa: F811
    done = _submit(swarm)
    running = _submit(swarm)
    _finish(db, done)
    page = api.get("/v1/workflows?state=SUCCEEDED", headers=AUTH).json()
    assert [w["workflow_id"] for w in page["workflows"]] == [done["workflow_id"]]
    page = api.get("/v1/workflows?state=QUEUED&state=READY&state=RUNNING&state=PARKED", headers=AUTH).json()
    assert [w["workflow_id"] for w in page["workflows"]] == [running["workflow_id"]]
    # No filter is every workflow, as before.
    page = api.get("/v1/workflows", headers=AUTH).json()
    assert {w["workflow_id"] for w in page["workflows"]} == {done["workflow_id"], running["workflow_id"]}


def test_the_list_holds_only_the_callers_unfinished_workflows(swarm, api, db):  # noqa: F811
    done = _submit(swarm, label="finished-one")
    failed = _submit(swarm)
    running = _submit(swarm, label="nightly")
    _finish(db, done)
    _finish(db, failed, "FAILED")
    foreign = _other_tenants_copy(db, running["workflow_id"])

    listing = sc.running_workflows(swarm)

    ids = [w["workflow_id"] for w in listing["workflows"]]
    assert ids == [running["workflow_id"]], ids
    assert foreign not in ids and done["workflow_id"] not in ids
    assert listing["count"] == 1 and listing["complete"] is True
    assert listing["tenant_id"] == "eng"
    (entry,) = listing["workflows"]
    assert entry["state"] not in _TERMINAL
    assert entry["label"] == "nightly"
    assert isinstance(entry["age_seconds"], int) and entry["age_seconds"] >= 0
    # Its current steps: every step that has not finished, with its state.
    current = {s["step_id"]: s["state"] for s in entry["current_steps"]}
    assert set(current) == {"research", "draft"}
    assert current["draft"] == "PARKED"
    assert all(s["task_id"] for s in entry["current_steps"])


def test_a_finished_step_is_not_a_current_step(swarm, api, db):  # noqa: F811
    created = _submit(swarm)
    tasks = {s["step_id"]: s["task_id"] for s in created["steps"]}
    db.docs[f"tasks/{tasks['research']}"]["state"] = "SUCCEEDED"
    db.docs[f"tasks/{tasks['draft']}"]["state"] = "RUNNING"
    (entry,) = sc.running_workflows(swarm)["workflows"]
    assert [(s["step_id"], s["state"]) for s in entry["current_steps"]] == [("draft", "RUNNING")]
    assert entry["state"] == "RUNNING"


class _Fake:
    """A client whose transport is `request`; the shipped `workflows` method
    above it builds the query, so that is what the tests see."""

    workflows = SwarmClient.workflows


def test_the_list_asks_the_route_for_the_unfinished_states_only():
    """The filter goes to the API, so a tenant with hundreds of finished
    workflows is not paged through to find the three that run."""
    seen: list[str] = []

    class _Client(_Fake):
        def request(self, method, path, **_):
            seen.append(path)
            return {"workflows": [], "next_page_token": None, "tenant_id": "eng"}

    sc.running_workflows(_Client())
    (path,) = seen
    assert path.startswith("/v1/workflows?")
    asked = {part.split("=", 1)[1] for part in path.split("?", 1)[1].split("&") if part.startswith("state=")}
    assert asked and not asked & _TERMINAL
    assert {"QUEUED", "PARKED", "READY", "LEASED", "RUNNING", "UNKNOWN"} <= asked


def test_an_api_that_ignores_the_filter_still_lists_only_unfinished_workflows():
    """An older deployment drops an unknown query parameter and serves every
    workflow; the bridge filters again rather than attach a finished one."""

    class _Client(_Fake):
        def request(self, method, path, **_):
            if path.startswith("/v1/workflows?"):
                return {
                    "tenant_id": "eng",
                    "next_page_token": None,
                    "workflows": [
                        {"workflow_id": "wf_a", "state": "SUCCEEDED", "steps": []},
                        {"workflow_id": "wf_b", "state": "RUNNING", "steps": []},
                    ],
                }
            return {"workflow": {"workflow_id": "wf_b", "state": "RUNNING", "steps": []}, "tasks": []}

    listing = sc.running_workflows(_Client())
    assert [w["workflow_id"] for w in listing["workflows"]] == ["wf_b"]


def test_the_list_carries_the_console_link_the_api_served():
    link = "https://console.example.test/workflows/wf_b"

    class _Client(_Fake):
        def request(self, method, path, **_):
            if path.startswith("/v1/workflows?"):
                return {
                    "tenant_id": "eng", "next_page_token": None,
                    "workflows": [{"workflow_id": "wf_b", "state": "RUNNING", "steps": [],
                                   "links": {"console": link}}],
                }
            return {"workflow": {"workflow_id": "wf_b", "state": "RUNNING", "steps": [],
                                 "links": {"console": link}}, "tasks": []}

    (entry,) = sc.running_workflows(_Client())["workflows"]
    assert entry["console"] == link


def test_the_mcp_tool_lists_the_running_workflows(swarm, api, db):  # noqa: F811
    done = _submit(swarm)
    running = _submit(swarm, label="nightly")
    _finish(db, done)
    names = {t["name"] for t in server.TOOLS}
    assert "swarm_workflows" in names
    reply = json.loads(server._call(swarm, "swarm_workflows", {}))
    assert [w["workflow_id"] for w in reply["workflows"]] == [running["workflow_id"]]
    assert reply["count"] == 1
    assert "/sc attach --all" in reply["attach_all_with"]


def test_sc_workflows_prints_each_running_workflow(swarm, api, db):  # noqa: F811
    running = _submit(swarm, label="nightly")
    out = io.StringIO()
    args = sc.build_parser().parse_args(["workflows"])
    assert sc.cmd_workflows(swarm, args, out) == sc.EXIT_OK
    text = out.getvalue()
    assert running["workflow_id"] in text and "nightly" in text
    assert "draft" in text and "PARKED" in text

    out = io.StringIO()
    args = sc.build_parser().parse_args(["workflows", "--json"])
    assert sc.cmd_workflows(swarm, args, out) == sc.EXIT_OK
    assert [w["workflow_id"] for w in json.loads(out.getvalue())["workflows"]] == [running["workflow_id"]]


# --------------------------------------------------------------------------
# `sc workflows --session-start`: what the hook hands the session
# --------------------------------------------------------------------------


def _session_start(client) -> tuple[int, str]:
    out = io.StringIO()
    args = sc.build_parser().parse_args(["workflows", "--session-start"])
    code = sc.cmd_workflows(client, args, out)
    return code, out.getvalue()


def test_session_start_names_the_running_workflows_and_the_attach(swarm, api, db):  # noqa: F811
    running = _submit(swarm, label="nightly")
    other = _submit(swarm)
    code, text = _session_start(swarm)
    assert code == 0
    payload = json.loads(text)
    hook = payload["hookSpecificOutput"]
    assert hook["hookEventName"] == "SessionStart"
    context = hook["additionalContext"]
    assert running["workflow_id"] in context and other["workflow_id"] in context
    assert "nightly" in context
    assert "2 SwarmCloud workflows" in context
    assert "/sc attach --all" in context


def test_session_start_says_nothing_when_none_run(swarm, api, db):  # noqa: F811
    _finish(db, _submit(swarm))
    assert _session_start(swarm) == (0, "")


def test_session_start_says_nothing_when_the_api_fails():
    class _Broken(_Fake):
        def request(self, *_, **__):
            raise SwarmError("GET /v1/workflows -> 503: unavailable")

    assert _session_start(_Broken()) == (0, "")


# --------------------------------------------------------------------------
# `/sc attach --all` in run.js
# --------------------------------------------------------------------------


#: run.js's harness keys a reply by the prompt's first line; attach --all asks
#: the same first line (`ATTACH`) once per workflow, so a reply may also be
#: keyed by the whole prompt.
_BY_PROMPT = _HARNESS.replace(
    "const answer = fixture.answers[key]",
    "const answer = fixture.answers[prompt] !== undefined ? fixture.answers[prompt] : fixture.answers[key]",
)
assert _BY_PROMPT != _HARNESS, "the harness changed shape; re-point the per-prompt lookup"


def _run_all(tmp_path, answers) -> dict:
    harness = tmp_path / "harness.cjs"
    harness.write_text(_BY_PROMPT)
    fixture = tmp_path / "fixture.json"
    fixture.write_text(json.dumps({"args": {"attach": "all"}, "answers": answers}))
    done = subprocess.run(
        [_node(), str(harness), str(_RUN_JS), str(fixture)],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def _attached(workflow_id: str, steps: list[dict]) -> dict:
    return {"workflow_id": workflow_id, "console": None, "state": "RUNNING", "state_note": None,
            "bridge_version": "0.1.0", "follow_error": None, "error": None, "steps": steps}


def _listed(*ids: str) -> dict:
    return {
        "count": len(ids), "error": None,
        "workflows": [{"workflow_id": i, "label": f"label-{i}", "state": "RUNNING", "console": None} for i in ids],
    }


_TWO = {
    "LIST": _listed("wf_a", "wf_b"),
    "ATTACH\nworkflow_id: wf_a": _attached("wf_a", [
        {"step_id": "implement", "task_id": "ta_1", "depends_on": [], "state": "SUCCEEDED", "console": None},
        {"step_id": "review", "task_id": "ta_2", "depends_on": ["implement"], "state": "RUNNING", "console": None},
    ]),
    "ATTACH\nworkflow_id: wf_b": _attached("wf_b", [
        {"step_id": "scan", "task_id": "tb_1", "depends_on": [], "state": "RUNNING", "console": None},
        {"step_id": "fix", "task_id": "tb_2", "depends_on": ["scan"], "state": "PARKED", "console": None},
    ]),
    "step:review": _step("SUCCEEDED"),
    "step:scan": _step("SUCCEEDED"),
    "step:fix": _step("SUCCEEDED"),
    "STATUS": {"state": "SUCCEEDED", "state_note": None, "console": None, "steps": []},
}


def test_attach_all_lists_once_and_starts_one_row_per_unfinished_step_across_workflows(tmp_path):
    got = _run_all(tmp_path, _TWO)
    assert "error" not in got, got.get("error")
    first = [c["prompt"].split("\n")[0] for c in got["calls"]]
    assert first.count("LIST") == 1 and first[0] == "LIST"
    assert "SUBMIT" not in first and "READ SPEC" not in first
    attaches = [c["prompt"] for c in got["calls"] if c["prompt"].startswith("ATTACH")]
    assert attaches == ["ATTACH\nworkflow_id: wf_a", "ATTACH\nworkflow_id: wf_b"]
    rows = sorted(
        (line.split(": ", 1)[1] for c in got["calls"] if c["agentType"] == "sc:step"
         for line in c["prompt"].split("\n") if line.startswith("step_id: ")),
    )
    # `implement` had finished before the attach: no row for it.
    assert rows == ["fix", "review", "scan"]
    # Each row is named by its workflow's label.
    labels = [c["label"] for c in got["calls"] if c["agentType"] == "sc:step"]
    assert any("label-wf_a" in label for label in labels) and any("label-wf_b" in label for label in labels)
    assert got["result"]["state"] == "ATTACHED"
    assert [w["workflow_id"] for w in got["result"]["workflows"]] == ["wf_a", "wf_b"]
    assert got["result"]["not_followed"] == []


def test_attach_all_follows_at_most_the_cap_and_lists_the_rest(tmp_path):
    ids = [f"wf_{n:02d}" for n in range(12)]
    answers = {"LIST": _listed(*ids), "STATUS": _TWO["STATUS"], "step:work": _step("SUCCEEDED")}
    for workflow_id in ids:
        answers[f"ATTACH\nworkflow_id: {workflow_id}"] = _attached(workflow_id, [
            {"step_id": "work", "task_id": f"t_{workflow_id}", "depends_on": [], "state": "RUNNING", "console": None},
        ])
    got = _run_all(tmp_path, answers)
    assert "error" not in got, got.get("error")
    attached = [c["prompt"].split(": ", 1)[1] for c in got["calls"] if c["prompt"].startswith("ATTACH")]
    assert attached == ids[:10], "the newest ten, in the order the bridge listed them"
    assert len([c for c in got["calls"] if c["agentType"] == "sc:step"]) == 10
    assert [w["workflow_id"] for w in got["result"]["not_followed"]] == ids[10:]
    said = " ".join(got["logs"])
    for workflow_id in ids[10:]:
        assert workflow_id in said, f"{workflow_id} was neither followed nor listed"


def test_attach_all_with_nothing_running_starts_nothing(tmp_path):
    got = _run_all(tmp_path, {"LIST": _listed()})
    assert [c["prompt"] for c in got["calls"]] == ["LIST"]
    assert got["result"]["state"] == "NOTHING_RUNNING"


def test_attach_all_reports_a_failed_list_verbatim(tmp_path):
    failed = {"count": None, "workflows": [], "error": "GET /v1/workflows -> 503: unavailable"}
    got = _run_all(tmp_path, {"LIST": failed})
    assert got["result"]["state"] == "NOT_ATTACHED"
    assert got["result"]["error"] == failed["error"]


def test_one_workflow_that_cannot_be_attached_does_not_stop_the_others(tmp_path):
    answers = dict(_TWO)
    answers["ATTACH\nworkflow_id: wf_a"] = {**_attached("wf_a", []), "error": "GET /v1/workflows/wf_a -> 404"}
    got = _run_all(tmp_path, answers)
    by_id = {w["workflow_id"]: w for w in got["result"]["workflows"]}
    assert by_id["wf_a"]["state"] == "NOT_ATTACHED"
    assert by_id["wf_b"]["state"] == "SUCCEEDED"


def test_attach_all_sets_the_result_phase_once_after_every_workflow(tmp_path):
    got = _run_all(tmp_path, _TWO)
    assert got["phases"] == ["Attach", "Result"], got["phases"]
    results = [c for c in got["calls"] if c["prompt"].startswith("STATUS")]
    assert len(results) == 2 and all(c["phase"] == "Result" for c in results)


def test_attach_all_tells_apart_two_workflows_with_the_same_label(tmp_path):
    answers = dict(_TWO)
    listed = _listed("wf_aaaaaaaa1111", "wf_bbbbbbbb2222")
    for entry in listed["workflows"]:
        entry["label"] = "nightly scan"
    answers["LIST"] = listed
    answers["ATTACH\nworkflow_id: wf_aaaaaaaa1111"] = _TWO["ATTACH\nworkflow_id: wf_a"]
    answers["ATTACH\nworkflow_id: wf_bbbbbbbb2222"] = _TWO["ATTACH\nworkflow_id: wf_b"]
    got = _run_all(tmp_path, answers)
    labels = [c["label"] for c in got["calls"] if c["agentType"] == "sc:step"]
    assert len(labels) == len(set(labels)) == 3, labels
    assert any("aaaa1111" in label for label in labels) and any("bbbb2222" in label for label in labels)


def test_attach_all_keeps_a_unique_label_as_it_is(tmp_path):
    got = _run_all(tmp_path, _TWO)
    labels = [c["label"] for c in got["calls"] if c["agentType"] == "sc:step"]
    assert all("[SwarmCloud] label-wf_" in label for label in labels), labels


def test_the_cap_is_stated_with_its_reason():
    text = _RUN_JS.read_text()
    assert "const MAX_ATTACHED_WORKFLOWS = 10" in text
    at = text.index("const MAX_ATTACHED_WORKFLOWS")
    assert "row" in text[max(0, at - 1200):at].lower(), "the cap's reason must sit above it"


def test_the_list_relay_is_granted_the_list_tool():
    body = (_PLUGIN / "agents" / "workflow.md").read_text()
    assert "mcp__plugin_sc_swarmcloud__swarm_workflows" in body
    assert "## LIST" in body


# --------------------------------------------------------------------------
# The SessionStart hook
# --------------------------------------------------------------------------


_CONTEXT_REPLY = json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": "2 SwarmCloud workflows are running: wf_a, wf_b. Run `/sc attach --all` now.",
    }
})


def _fake_uv(bin_dir: Path, body: str) -> None:
    uv = bin_dir / "uv"
    uv.write_text("#!/usr/bin/env bash\n" + body + "\n")
    uv.chmod(uv.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _hook(tmp_path, uv_body: str, **env: str) -> subprocess.CompletedProcess:
    bash = shutil.which("bash")
    if bash is None:  # pragma: no cover
        pytest.skip("bash is not installed")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    _fake_uv(bin_dir, uv_body)
    base = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "CLAUDE_PLUGIN_ROOT": str(_PLUGIN),
        "CLAUDE_PLUGIN_OPTION_DEPLOYMENT_URL": "https://swarm.example.test",
        "SWARM_ARGS_LOG": str(tmp_path / "uv-args"),
    }
    base.update(env)
    return subprocess.run(
        [bash, str(_HOOK)],
        input=json.dumps({"hook_event_name": "SessionStart", "source": "startup", "cwd": str(tmp_path)}),
        capture_output=True, text=True, timeout=60, env=base, check=False,
    )


_RECORD = 'printf "%s\\n" "$@" > "$SWARM_ARGS_LOG"'


def test_the_hook_emits_the_bridges_additional_context(tmp_path):
    done = _hook(tmp_path, f"{_RECORD}\ncat <<'EOF'\n{_CONTEXT_REPLY}\nEOF")
    assert done.returncode == 0, done.stderr
    payload = json.loads(done.stdout)
    context = payload["hookSpecificOutput"]["additionalContext"]
    assert "wf_a" in context and "/sc attach --all" in context
    argv = (tmp_path / "uv-args").read_text().split("\n")
    assert argv[-4:-1] == ["sc", "workflows", "--session-start"], argv


def test_the_hook_runs_the_bridge_pinned_in_the_manifest(tmp_path):
    """The pin has ONE home, plugin.json; the hook reads it from there."""
    done = _hook(tmp_path, f"{_RECORD}\ncat <<'EOF'\n{_CONTEXT_REPLY}\nEOF")
    assert done.returncode == 0, done.stderr
    manifest = json.loads(_MANIFEST.read_text())
    version = manifest["version"]
    argv = (tmp_path / "uv-args").read_text().split("\n")
    spec = argv[argv.index("--from") + 1]
    assert spec.startswith("swarm-mcp @ git+https://github.com/")
    assert f"@sc-v{version}#subdirectory=apps/swarm-mcp" in spec
    assert f"sc-v{version}" not in _HOOK.read_text(), "the hook restates the pin"


def test_the_hook_honours_the_developer_override(tmp_path):
    done = _hook(tmp_path, f"{_RECORD}\ncat <<'EOF'\n{_CONTEXT_REPLY}\nEOF",
                 SWARM_MCP_FROM="/src/swarm/apps/swarm-mcp")
    assert done.returncode == 0, done.stderr
    argv = (tmp_path / "uv-args").read_text().split("\n")
    assert argv[argv.index("--from") + 1] == "/src/swarm/apps/swarm-mcp"


def test_the_hook_says_nothing_when_none_run(tmp_path):
    done = _hook(tmp_path, "exit 0")
    assert (done.returncode, done.stdout) == (0, "")


@pytest.mark.parametrize("failure", [
    "echo 'sc: GET /v1/workflows -> 503' >&2; exit 1",
    "echo 'error: failed to fetch git+https://github.com/...' >&2; exit 2",
    "echo 'not json at all'",
])
def test_the_hook_says_nothing_when_the_bridge_fails(tmp_path, failure):
    done = _hook(tmp_path, failure)
    assert (done.returncode, done.stdout) == (0, "")
    assert done.stderr == "", "a failure is silent: the session shows hook stderr"


def test_the_hook_gives_up_on_a_bridge_that_hangs(tmp_path):
    started = time.monotonic()
    done = _hook(tmp_path, f"sleep 30\ncat <<'EOF'\n{_CONTEXT_REPLY}\nEOF",
                 SWARM_SESSION_START_TIMEOUT="1")
    assert (done.returncode, done.stdout) == (0, "")
    assert time.monotonic() - started < 15


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        # A zombie is dead; only its parent has not read its status yet.
        return Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1][0] != "Z"
    except (OSError, IndexError):
        return True


def test_the_hook_kills_what_the_bridge_spawned_when_it_hangs(tmp_path):
    """uv runs the bridge as a child; a timeout must not leave that child running."""
    child = tmp_path / "child-pid"
    done = _hook(tmp_path, f'sleep 30 &\necho $! > "{child}"\nwait',
                 SWARM_SESSION_START_TIMEOUT="1")
    assert (done.returncode, done.stdout, done.stderr) == (0, "", "")
    pid = int(child.read_text())
    deadline = time.monotonic() + 5
    while _alive(pid) and time.monotonic() < deadline:
        time.sleep(0.1)
    alive = _alive(pid)
    if alive:
        os.kill(pid, 9)
    assert not alive, "the bridge's child outlived the hook"


@pytest.mark.parametrize("off", ["false", "False", "0", "off", "no"])
def test_the_hook_does_nothing_when_auto_attach_is_off(tmp_path, off):
    done = _hook(tmp_path, f"{_RECORD}\ncat <<'EOF'\n{_CONTEXT_REPLY}\nEOF",
                 CLAUDE_PLUGIN_OPTION_AUTO_ATTACH=off)
    assert (done.returncode, done.stdout) == (0, "")
    assert not (tmp_path / "uv-args").exists(), "the bridge was asked although auto_attach is off"


def test_the_hook_does_nothing_where_the_plugin_is_not_configured(tmp_path):
    done = _hook(tmp_path, f"{_RECORD}\ncat <<'EOF'\n{_CONTEXT_REPLY}\nEOF",
                 CLAUDE_PLUGIN_OPTION_DEPLOYMENT_URL="")
    assert (done.returncode, done.stdout) == (0, "")
    assert not (tmp_path / "uv-args").exists()


def test_the_hook_is_registered_for_session_start():
    hooks = json.loads(_HOOKS_JSON.read_text())["hooks"]
    (entry,) = hooks["SessionStart"]
    (command,) = entry["hooks"]
    assert command["type"] == "command"
    assert "${CLAUDE_PLUGIN_ROOT}/hooks/session-start.sh" in command["command"]
    assert 0 < command["timeout"] <= 30
    assert os.access(_HOOK, os.X_OK), "the hook script is not executable"
    # Claude Code loads hooks/hooks.json by itself; naming it in the manifest
    # too loads it twice and is refused as a duplicate.
    assert "hooks" not in json.loads(_MANIFEST.read_text())


def test_auto_attach_is_a_user_option_on_by_default():
    option = json.loads(_MANIFEST.read_text())["userConfig"]["auto_attach"]
    assert option["type"] == "boolean"
    assert option["default"] is True
    assert "/sc attach --all" in option["description"]
    assert "CLAUDE_PLUGIN_OPTION_AUTO_ATTACH" in _HOOK.read_text()


def test_the_hook_script_follows_the_house_shell_rules():
    lines = [line for line in _HOOK.read_text().splitlines()]
    assert lines[0] == "#!/usr/bin/env bash"
    effective = [line for line in lines[1:] if line.strip() and not line.lstrip().startswith("#")]
    assert effective[0] == "set -euo pipefail"


# --------------------------------------------------------------------------
# The version
# --------------------------------------------------------------------------


# The last version main published before this change (f8e16f4, #499). A plugin
# change that leaves the version at or below it is never published by the
# release's tag job (2026-10-02 lesson).
_PREVIOUS_RELEASED_VERSION = (0, 5, 10)


def test_the_plugin_version_moved_past_the_last_release_with_its_pin():
    manifest = json.loads(_MANIFEST.read_text())
    version = tuple(int(part) for part in manifest["version"].split("."))
    assert version > _PREVIOUS_RELEASED_VERSION
    pinned = manifest["mcpServers"]["swarmcloud"]["args"]
    assert any(f"@sc-v{manifest['version']}#" in arg for arg in pinned)
