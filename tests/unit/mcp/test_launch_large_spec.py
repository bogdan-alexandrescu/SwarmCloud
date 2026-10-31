"""A spec of any size launches through `swarm_workflow_launch` (LAUNCH-LARGE-SPEC, #2875).

MEASURED, 2026-10-31 (product-oracle-2f): a 24-step spec with about 90 KB of
prompts made `swarm_workflow_launch {spec_path}` answer 94,498 characters, over
Claude Code's tool-output limit, because its `args` embedded the whole spec
beside `spec_handle`; the documented fallback, the titled script given the
spec file's path, had `swarm_workflow_spec` answer 86,506 characters, the whole
spec again, and the run ended NOT_SUBMITTED.

So the launch's `args` carry only `{spec_handle, details, rows, titled_copy}`;
run.js has one row read the handle's OUTLINE with `swarm_workflow_spec
{spec_handle, spec_digest}` -- the digest checked where the bytes are read --
and submits by the handle; `swarm_workflow_spec` answers an outline, the
digest and a ref, and one step's prompt only when asked, a window at a time.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from swarm_mcp import server, workflows
from swarm_mcp.client import SwarmError

from test_plugin_agents_and_workflows import _PLUGIN
from test_plugin_rows_match_console import _answers, _run
from test_submit_by_reference import _Recorder, _submitted_steps

#: Claude Code's tool output is cut well before this; the issue's replies were
#: 94,498 and 86,506 characters.
_REPLY_LIMIT = 20 * 1024


def _large_spec() -> dict:
    """24 steps, about 90 KB of prompts, each step depending on the one before."""
    filler = "Read the module, write the change and its tests, then say what you verified. " * 48
    steps = []
    for index in range(24):
        step = {"step_id": f"s{index:02d}", "prompt": f"Step {index}: backlog item {index}.\n{filler}",
                "runner_profile": "claude-code"}
        if index:
            step["depends_on"] = [f"s{index - 1:02d}"]
        steps.append(step)
    spec = {"label": "oracle-backlog-107", "title": "Oracle backlog 107", "steps": steps}
    assert 85 * 1024 < sum(len(s["prompt"]) for s in steps) < 100 * 1024
    return spec


@pytest.fixture
def _launching(monkeypatch, tmp_path):
    """A plugin root, and the session's project in `tmp_path`, so the copy
    and its handle land in `tmp_path/.sc-swarmcloud-runs/`."""
    monkeypatch.setenv("SWARM_SC_PLUGIN_ROOT", str(_PLUGIN))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("SWARM_CHECKOUT_DIR", str(tmp_path))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    return tmp_path


def _launch_text(spec: dict) -> str:
    return server._call(_Recorder(), "swarm_workflow_launch", {"spec": spec})


def _read(handle: dict, **extra) -> dict:
    return json.loads(server._call(None, "swarm_workflow_spec",
                                   {"spec_handle": handle["path"], "spec_digest": handle["digest"], **extra}))


def test_a_large_specs_launch_reply_carries_the_handle_and_never_the_spec(_launching):
    spec = _large_spec()
    text = _launch_text(spec)
    reply = json.loads(text)
    # MUTATION: a launch whose args embed the spec again answers ~95 KB and fails here.
    assert len(text) < _REPLY_LIMIT, len(text)
    assert set(reply["args"]) == {"spec_handle", "details", "rows", "titled_copy"}
    assert spec["steps"][5]["prompt"] not in text
    handle = reply["args"]["spec_handle"]
    assert handle["digest"] == workflows.spec_digest(spec)
    assert json.loads(Path(handle["path"]).read_text(encoding="utf-8")) == spec
    assert "Never add the spec" in reply["launch_with"]


def test_swarm_workflow_spec_answers_an_outline_never_every_prompt(_launching):
    spec = _large_spec()
    handle = json.loads(_launch_text(spec))["args"]["spec_handle"]
    text = server._call(None, "swarm_workflow_spec", {"spec_handle": handle["path"], "spec_digest": handle["digest"]})
    read = json.loads(text)
    # MUTATION: a reply that carries the spec (or every prompt) answers ~90 KB and fails here.
    assert len(text) < _REPLY_LIMIT, len(text)
    assert "spec" not in read
    assert read["spec_digest"] == handle["digest"] and read["spec_ref"].startswith("spec_")
    steps = read["outline"]["steps"]
    assert [s["step_id"] for s in steps] == [s["step_id"] for s in spec["steps"]]
    assert steps[3]["depends_on"] == ["s02"] and steps[3]["runner_profile"] == "claude-code"
    assert steps[3]["prompt_chars"] == len(spec["steps"][3]["prompt"])
    assert steps[3]["prompt_first_line"] == "Step 3: backlog item 3."

    # The same by path, from the checkout.
    (_launching / "oracle.json").write_text(json.dumps(spec), encoding="utf-8")
    by_path = server._call(None, "swarm_workflow_spec", {"path": "oracle.json"})
    assert len(by_path) < _REPLY_LIMIT and "spec" not in json.loads(by_path)


def test_one_steps_prompt_is_read_only_on_request_and_windowed(_launching):
    spec = _large_spec()
    handle = json.loads(_launch_text(spec))["args"]["spec_handle"]
    prompt = spec["steps"][7]["prompt"]
    first = _read(handle, step_id="s07", limit=1000)["prompt"]
    assert first == {"step_id": "s07", "offset": 0, "text": prompt[:1000], "chars": len(prompt), "next_offset": 1000}
    rest = _read(handle, step_id="s07", offset=first["next_offset"], limit=10 ** 6)["prompt"]
    # MUTATION: a window that is not held to its cap, or misses the tail, fails here.
    assert rest["text"] == prompt[1000:1000 + server._PROMPT_WINDOW_MAX] and rest["next_offset"] is None
    with pytest.raises(SwarmError, match="no step 'nope'"):
        _read(handle, step_id="nope")


def test_the_titled_script_submits_a_large_spec_from_its_handle(_launching, tmp_path):
    spec = _large_spec()
    reply = json.loads(_launch_text(spec))
    handle = reply["args"]["spec_handle"]
    got = _run(tmp_path, reply["args"], {**_answers(spec), "READ SPEC": _read(handle)},
               script=Path(reply["script_path"]))
    read_row, submit = [c for c in got["calls"] if c["prompt"].startswith(("READ SPEC", "SUBMIT"))]
    # MUTATION: a script that cannot run from the handle alone (it wanted `spec`) fails here.
    assert read_row["prompt"] == "READ SPEC\nspec_handle: " + handle["path"] + "\nspec_digest: " + handle["digest"]
    assert submit["prompt"].split("\n")[:3] == ["SUBMIT", "spec_digest: " + handle["digest"], "spec_handle: " + handle["path"]]
    assert "Read the module" not in submit["prompt"]
    assert got["result"]["workflow_id"] == "wf_1"

    # The submit row's call, as the agent makes it: the launch's bytes, every prompt intact.
    recorder = _Recorder()
    server._call(recorder, "swarm_workflow", {"spec_handle": handle["path"], "spec_digest": handle["digest"]})
    assert [s["input"]["prompt"] for s in _submitted_steps(recorder)] == [s["prompt"] for s in spec["steps"]]


def test_a_changed_handle_is_refused_where_it_is_read(_launching):
    spec = _large_spec()
    handle = json.loads(_launch_text(spec))["args"]["spec_handle"]
    target = Path(handle["path"])
    target.write_text(target.read_text(encoding="utf-8").replace("backlog item 9.", "backlog item 9!"), encoding="utf-8")
    # MUTATION: a handle read that skips the digest hands out a ref to the changed spec and fails here.
    with pytest.raises(SwarmError, match="changed after its digest was taken"):
        _read(handle)
    recorder = _Recorder()
    with pytest.raises(SwarmError, match="NOTHING was submitted"):
        server._call(recorder, "swarm_workflow", {"spec_handle": handle["path"], "spec_digest": handle["digest"]})
    assert recorder.sent == []
    # MUTATION: a handle read with no digest at all fails here.
    with pytest.raises(SwarmError, match="spec_digest"):
        server._call(None, "swarm_workflow_spec", {"spec_handle": handle["path"]})


def test_the_script_submits_nothing_when_the_read_row_names_another_digest(_launching, tmp_path):
    spec = _large_spec()
    reply = json.loads(_launch_text(spec))
    read = _read(reply["args"]["spec_handle"])
    read["spec_digest"] = "fnv1a32:00000000"
    got = _run(tmp_path, reply["args"], {**_answers(spec), "READ SPEC": read}, script=Path(reply["script_path"]))
    # MUTATION: a script that trusts the relayed digest over the launch's submits and fails here.
    assert [c["prompt"].split("\n")[0] for c in got["calls"]] == ["READ SPEC"]
    assert got["result"]["state"] == "NOT_SUBMITTED"


def test_the_agent_and_the_skill_say_how_to_read_a_handle():
    agent = (_PLUGIN / "agents" / "workflow.md").read_text()
    assert '{"spec_handle": "<that path>", "spec_digest": "<that digest>"}' in agent
    skill = (_PLUGIN / "skills" / "sc" / "SKILL.md").read_text()
    # MUTATION: a skill that still tells the session to pass `{spec, spec_handle, ...}` fails here.
    assert "`{spec,\n   spec_handle" not in skill and "never add `spec` beside it" in skill
