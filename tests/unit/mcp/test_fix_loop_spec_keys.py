"""The sc plugin sends a workflow step's `fix_loop` block as given (docs/design/fix-loop.md §4.1).

MEASURED, 8 November: a lane ran one fix round, its re-review said NOT_YET,
and the workflow ended FAILED instead of running another round. The platform's
fix loop was merged and deployed, and swarm-api accepts a `fix-loop` step's
`fix_loop` block, but `workflows._STEP_KEYS` did not hold the key, so
`build_steps` refused every spec step that declared a loop: no spec submitted
through the plugin -- swarm_workflow, swarm_workflow_launch, /sc:swarmcloud,
the terminal's workflow command -- could use it.

Every path a spec takes through the bridge is held here: the request body, the
spec checker and its outline, the digest, the launch's handle, the CLI's help
and the tool's description. The block's fields are swarm-api's rules
(`validation.resolve_fix_loop`) and are not restated: the bridge checks that
it is an object and sends it byte for byte.
"""

from __future__ import annotations

import copy
import json
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest

from swarm_common.profiles import RUNNER_PROFILES
from swarm_mcp import cli, server, workflows
from swarm_mcp.client import SwarmError

from test_plugin_agents_and_workflows import _PLUGIN
from test_submit_by_reference import _Recorder, _submitted_steps

#: The block of docs/design/fix-loop.md §4.1, every optional field set.
_BLOCK = {
    "review": "review",
    "fix": "fix",
    "max_rounds": 3,
    "fix_round": {"runner_profile": "claude-code", "model_class": "opus",
                  "prompt": "You are a fix round: fix every blocker and major in verdict.json."},
    "re_review": {"runner_profile": "claude-code-browser", "model_class": "fable",
                  "runtime": "browser_if_ui",
                  "prompt": "You are a re-review: judge the fix's head and write verdict.json."},
}


def _spec() -> dict:
    """§5.1's lane -- implement, review, fix, loop -- with §4.1's loop step, which has no prompt."""
    return {
        "label": "loop-lane",
        "strategy": "integrate",
        "merge": "on_merge_verdict",
        "steps": [
            {"step_id": "implement", "runner_profile": "claude-code",
             "prompt": "Implement the change in the brief."},
            {"step_id": "review", "runner_profile": "claude-code",
             "depends_on": ["implement"], "builds_on": "implement",
             "input_from": {"implement": "swarm-work.patch"},
             "prompt": "Review swarm-work.patch. Write verdict.json."},
            {"step_id": "fix", "runner_profile": "claude-code",
             "depends_on": ["review"], "builds_on": "implement",
             "input_from": {"review": "verdict.json"},
             "when": {"step": "review", "verdict_in": ["NOT_YET"]},
             "prompt": "Fix every blocker and major in verdict.json."},
            {"step_id": "loop", "runner_profile": "fix-loop", "depends_on": ["fix"],
             "fix_loop": copy.deepcopy(_BLOCK)},
        ],
    }


@pytest.fixture(autouse=True)
def _loop_enabled(monkeypatch):
    """`fix-loop`, available for these tests only, as swarm-api's own tests make it."""
    monkeypatch.setitem(RUNNER_PROFILES, "fix-loop",
                        replace(RUNNER_PROFILES["fix-loop"], available=True))


def _loop_sent(recorder: _Recorder) -> dict:
    (loop,) = [s for s in _submitted_steps(recorder) if s["step_id"] == "loop"]
    return loop


def _assert_block_byte_for_byte(sent: dict) -> None:
    # MUTATION: a bridge that drops, reorders, renames or rewrites any field of the block fails here.
    assert json.dumps(sent["fix_loop"]) == json.dumps(_BLOCK)


def test_build_steps_sends_the_block_as_given_and_the_loop_needs_no_prompt():
    built = workflows.build_steps(_spec()["steps"])
    (loop,) = [s for s in built if s["step_id"] == "loop"]
    # MUTATION: `_STEP_KEYS` without "fix_loop" refuses the step before this line.
    _assert_block_byte_for_byte(loop)
    # §4.1's loop step carries no prompt: no agent runs it, and swarm-api takes
    # an empty `input`. Every other step still needs one.
    assert loop["input"] == {}
    assert loop["runner_profile"] == "fix-loop" and loop["depends_on"] == ["fix"]


def test_a_loop_step_that_gives_a_prompt_still_sends_it():
    step = {"step_id": "loop", "runner_profile": "fix-loop", "prompt": "the loop", "fix_loop": _BLOCK}
    (built,) = workflows.build_steps([step])
    assert built["input"] == {"prompt": "the loop"}


def test_any_other_step_without_a_prompt_is_still_refused():
    with pytest.raises(SwarmError, match="has no prompt"):
        workflows.build_steps([{"step_id": "fix", "runner_profile": "claude-code", "fix_loop": _BLOCK}])


@pytest.mark.parametrize("block", [["review"], "review", 3])
def test_a_block_that_is_not_an_object_is_refused_before_anything_is_sent(block):
    recorder = _Recorder()
    spec = _spec()
    spec["steps"][3]["fix_loop"] = block
    with pytest.raises(SwarmError, match="fix_loop must be an object"):
        server._call(recorder, "swarm_workflow", {"spec": spec})
    assert recorder.sent == []


def test_swarm_workflow_sends_the_block_in_the_request_body_from_a_spec():
    recorder = _Recorder()
    server._call(recorder, "swarm_workflow", {"spec": _spec()})
    _assert_block_byte_for_byte(_loop_sent(recorder))


def test_swarm_workflow_sends_the_block_from_steps_too():
    """`steps` beside the parameters pass `_STEP_KEYS_HELD_HERE`, which must not hold the block."""
    assert "fix_loop" not in server._STEP_KEYS_HELD_HERE
    recorder = _Recorder()
    spec = _spec()
    server._call(recorder, "swarm_workflow", {"steps": spec["steps"], "strategy": "integrate"})
    _assert_block_byte_for_byte(_loop_sent(recorder))


def test_the_digest_covers_the_block_and_a_spec_path_with_it_submits(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARM_CHECKOUT_DIR", str(tmp_path))
    spec = _spec()
    changed = _spec()
    changed["steps"][3]["fix_loop"]["max_rounds"] = 5
    # MUTATION: a digest that skipped the block would let a relay change the loop unseen.
    assert workflows.spec_digest(spec) != workflows.spec_digest(changed)
    target = tmp_path / "loop.json"
    target.write_text(json.dumps(spec, indent=2))
    recorder = _Recorder()
    reply = json.loads(server._call(recorder, "swarm_workflow",
                                    {"spec_path": str(target), "spec_digest": workflows.spec_digest(spec)}))
    assert reply["spec_digest_checked"] is True
    _assert_block_byte_for_byte(_loop_sent(recorder))


def test_swarm_workflow_spec_accepts_the_loop_and_reports_it(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARM_CHECKOUT_DIR", str(tmp_path))
    (tmp_path / "loop.json").write_text(json.dumps(_spec()))
    read = json.loads(server._call(None, "swarm_workflow_spec", {"path": "loop.json"}))
    assert read["spec_digest"] == workflows.spec_digest(_spec())
    steps = {entry["step_id"]: entry for entry in read["outline"]["steps"]}
    # MUTATION: an outline that drops the block, or names it on a step without one, fails here.
    assert steps["loop"]["runner_profile"] == "fix-loop"
    assert json.dumps(steps["loop"]["fix_loop"]) == json.dumps(_BLOCK)
    assert steps["loop"]["prompt_chars"] == 0
    assert all("fix_loop" not in steps[s] for s in ("implement", "review", "fix"))

    # The held spec submits the block unchanged.
    recorder = _Recorder()
    server._call(recorder, "swarm_workflow", {"spec_ref": read["spec_ref"], "spec_digest": read["spec_digest"]})
    _assert_block_byte_for_byte(_loop_sent(recorder))


def test_a_launch_keeps_the_block_in_its_handle_and_submits_it(monkeypatch, tmp_path):
    monkeypatch.setenv("SWARM_SC_PLUGIN_ROOT", str(_PLUGIN))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    spec = _spec()
    reply = json.loads(server._call(_Recorder(), "swarm_workflow_launch", {"spec": spec}))
    handle = reply["args"]["spec_handle"]
    target = Path(handle["path"])
    # MUTATION: a launch that refuses the step, or writes the handle without the block, fails here.
    assert target.read_bytes() == json.dumps(spec, indent=2, ensure_ascii=False).encode("utf-8")
    assert handle["digest"] == workflows.spec_digest(spec)

    read = json.loads(server._call(None, "swarm_workflow_spec",
                                   {"spec_handle": handle["path"], "spec_digest": handle["digest"]}))
    assert json.dumps({e["step_id"]: e for e in read["outline"]["steps"]}["loop"]["fix_loop"]) == json.dumps(_BLOCK)
    recorder = _Recorder()
    server._call(recorder, "swarm_workflow", {"spec_handle": handle["path"], "spec_digest": handle["digest"]})
    _assert_block_byte_for_byte(_loop_sent(recorder))


def test_the_cli_help_offers_the_key():
    assert "fix_loop" in cli._workflow_epilog().replace("\n", " ")


def test_the_tool_description_names_the_key_among_a_steps_keys():
    (tool,) = [t for t in server.TOOLS if t["name"] == "swarm_workflow"]
    assert "`fix_loop`" in tool["description"]
