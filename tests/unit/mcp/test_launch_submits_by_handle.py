"""A `/sc:swarmcloud` launch submits its spec by handle, so no agent retypes it (SC-LAUNCH-SPEC, #2034).

MEASURED, 2026-10-23 ~01:35Z, lane SCAN-FP (wf_053776f6-dcb): the launch
embedded the spec in its args and the `sc:workflow` agent retyped ~23 KB of it
into `swarm_workflow`. The only brief of five that night holding JavaScript
template-literal text -- backticks around `${...}` -- arrived altered, and the
digest check refused it: NOT_SUBMITTED. The check worked; the retyping was the
defect.

So `swarm_workflow_launch` writes the exact bytes it embeds to a handle file
beside the titled copy, run.js gives the submitting agent only that handle
(path and digest), and `swarm_workflow {spec_handle, spec_digest}` reads the
bytes back and checks them as it checks `spec_path`. A spec given to the
script only as text keeps the by-value path, still checked.
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

#: The text that came back altered: a template literal, as a brief quotes it.
_TEMPLATE = "`i-${x}`"
_ISSUE_TEXT = '`i-${mockUuid().replace(/-/g, "")}`'


def _spec() -> dict:
    return {
        "label": "scan-fp",
        "steps": [
            {"step_id": "scan", "prompt": f"Mock the id as {_TEMPLATE}, the way the test writes {_ISSUE_TEXT}."},
            {"step_id": "fix", "prompt": "Fix what `scan` found; keep ${HOME} and \\` as they are.",
             "depends_on": ["scan"]},
        ],
    }


@pytest.fixture
def _launching(monkeypatch, tmp_path):
    """A plugin root, and the session's project in `tmp_path`, so the copy
    and its handle land in `tmp_path/.sc-swarmcloud-runs/`."""
    monkeypatch.setenv("SWARM_SC_PLUGIN_ROOT", str(_PLUGIN))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    return tmp_path


def _launch(spec: dict) -> dict:
    return json.loads(server._call(_Recorder(), "swarm_workflow_launch", {"spec": spec}))


def test_a_launch_of_a_template_literal_brief_writes_its_handle_byte_identical(_launching):
    spec = _spec()
    reply = _launch(spec)
    handle = reply["args"]["spec_handle"]
    target = Path(handle["path"])
    # MUTATION: a launch that writes no handle (or puts it elsewhere) fails here.
    assert target.parent == Path(reply["script_path"]).parent == _launching / ".sc-swarmcloud-runs"
    # MUTATION: a handle written in any other form than the object embedded -- re-escaped,
    # retyped, ASCII-only -- fails here, byte for byte.
    assert target.read_bytes() == json.dumps(spec, indent=2, ensure_ascii=False).encode("utf-8")
    assert _TEMPLATE.encode() in target.read_bytes()
    assert _ISSUE_TEXT in json.loads(target.read_bytes())["steps"][0]["prompt"]
    # MUTATION: a handle whose digest is not the embedded spec's fails here.
    assert handle["digest"] == workflows.spec_digest(spec)
    # MUTATION: args that embed the spec beside the handle fail here (#2875:
    # a 90 KB spec made the reply too long to read).
    assert "spec" not in reply["args"]
    assert reply["args"]["titled_copy"] == reply["script_path"]


def test_the_submit_row_is_handed_the_handle_and_no_spec_text(_launching, tmp_path):
    spec = _spec()
    reply = _launch(spec)
    handle = reply["args"]["spec_handle"]
    read = json.loads(server._call(None, "swarm_workflow_spec",
                                   {"spec_handle": handle["path"], "spec_digest": handle["digest"]}))
    got = _run(tmp_path, reply["args"], {**_answers(spec), "READ SPEC": read}, script=Path(reply["script_path"]))
    (submit,) = [c for c in got["calls"] if c["prompt"].startswith("SUBMIT")]
    lines = submit["prompt"].split("\n")
    # MUTATION: a Submit phase that still hands over the spec by value (BEGIN SPEC) fails here.
    assert lines[:3] == ["SUBMIT", "spec_digest: " + handle["digest"], "spec_handle: " + handle["path"]]
    # MUTATION: a prompt that carries any of the spec's text beside the handle fails here.
    assert "BEGIN SPEC" not in submit["prompt"]
    assert "mockUuid" not in submit["prompt"] and "${" not in submit["prompt"]
    assert got["result"]["workflow_id"] == "wf_1"


def test_args_whose_embedded_spec_was_altered_still_submit_the_launch_bytes(_launching, tmp_path):
    """Args from before #2875 embed the spec beside the handle, and the session
    retyped them into the Workflow call. An altered copy there shapes rows
    only: the submission is the handle's bytes, and the run says the two
    differ."""
    spec = _spec()
    reply = _launch(spec)
    args = {**json.loads(json.dumps(reply["args"])), "spec": json.loads(json.dumps(spec))}
    args["spec"]["steps"][0]["prompt"] = args["spec"]["steps"][0]["prompt"].replace("${x}", "{x}")
    got = _run(tmp_path, args, _answers(spec), script=Path(reply["script_path"]))
    (submit,) = [c for c in got["calls"] if c["prompt"].startswith("SUBMIT")]
    # MUTATION: a script that digests the altered args spec instead of trusting the handle fails here.
    assert submit["prompt"].split("\n")[1] == "spec_digest: " + reply["args"]["spec_handle"]["digest"]
    # MUTATION: a mismatch the run does not mention fails here.
    assert any("submitting the launch's bytes" in line for line in got["logs"])


def test_swarm_workflow_submits_a_handle_exactly_as_the_launch_wrote_it(_launching):
    spec = _spec()
    handle = _launch(spec)["args"]["spec_handle"]
    recorder = _Recorder()
    arguments = {"spec_handle": handle["path"], "spec_digest": handle["digest"]}
    reply = json.loads(server._call(recorder, "swarm_workflow", arguments))
    # MUTATION: a bridge that ignores `spec_handle` (or reads anything but its bytes) fails here.
    assert [s["input"]["prompt"] for s in _submitted_steps(recorder)] == [s["prompt"] for s in spec["steps"]]
    assert reply["spec_digest"] == handle["digest"] and reply["spec_digest_checked"] is True


def test_a_handle_whose_bytes_changed_submits_nothing(_launching):
    spec = _spec()
    handle = _launch(spec)["args"]["spec_handle"]
    target = Path(handle["path"])
    target.write_text(target.read_text(encoding="utf-8").replace("${x}", "{x}"), encoding="utf-8")
    recorder = _Recorder()
    # MUTATION: a handle read that skips the digest check submits the changed spec and fails here.
    with pytest.raises(SwarmError, match="changed on its way here.*NOTHING was submitted"):
        server._call(recorder, "swarm_workflow", {"spec_handle": handle["path"], "spec_digest": handle["digest"]})
    assert recorder.sent == []


def test_a_handle_is_never_read_without_its_digest(_launching):
    handle = _launch(_spec())["args"]["spec_handle"]
    recorder = _Recorder()
    # MUTATION: a handle accepted with no digest submits an unchecked spec and fails here.
    with pytest.raises(SwarmError, match="spec_digest"):
        server._call(recorder, "swarm_workflow", {"spec_handle": handle["path"]})
    assert recorder.sent == []


def test_a_handle_outside_the_run_folder_is_refused_unread(_launching, monkeypatch):
    spec = _spec()
    monkeypatch.setenv("SWARM_CHECKOUT_DIR", str(_launching))
    elsewhere = _launching / "spec-00000000-000000000000.json"
    elsewhere.write_text(json.dumps(spec))
    recorder = _Recorder()
    # MUTATION: a handle read from anywhere but the launch's own folder fails here.
    with pytest.raises(SwarmError, match="is not a spec handle"):
        server._call(recorder, "swarm_workflow",
                     {"spec_handle": str(elsewhere), "spec_digest": workflows.spec_digest(spec)})
    assert recorder.sent == []


def test_a_spec_given_only_as_text_is_still_submitted_by_value_and_checked(tmp_path):
    spec = _spec()
    got = _run(tmp_path, json.dumps(spec), _answers(spec))
    (submit,) = [c for c in got["calls"] if c["prompt"].startswith("SUBMIT")]
    lines = submit["prompt"].split("\n")
    # MUTATION: a by-value submit that lost its digest or its spec fails here.
    assert lines[:3] == ["SUBMIT", "spec_digest: " + workflows.spec_digest(spec), "BEGIN SPEC"]
    assert "spec_handle" not in submit["prompt"]

    recorder = _Recorder()
    altered = json.loads(json.dumps(spec))
    altered["steps"][0]["prompt"] = altered["steps"][0]["prompt"].replace("${x}", "{x}")
    # MUTATION: a by-value path whose digest check was dropped submits the altered spec and fails here.
    with pytest.raises(SwarmError, match="NOTHING was submitted"):
        server._call(recorder, "swarm_workflow", {"spec": altered, "spec_digest": workflows.spec_digest(spec)})
    assert recorder.sent == []
    reply = json.loads(server._call(recorder, "swarm_workflow", {"spec": spec, "spec_digest": workflows.spec_digest(spec)}))
    assert reply["spec_digest_checked"] is True


def test_the_schema_and_the_agent_offer_the_handle():
    (tool,) = [t for t in server.TOOLS if t["name"] == "swarm_workflow"]
    # MUTATION: a schema without `spec_handle` makes the bridge drop the argument and fails here.
    assert "spec_handle" in tool["inputSchema"]["properties"]
    agent = (_PLUGIN / "agents" / "workflow.md").read_text()
    # MUTATION: an agent prompt that never says how to pass a handle fails here.
    assert '{"spec_handle": "<that path>", "spec_digest": "<that digest>", "infer": true}' in agent
