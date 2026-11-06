"""The sc plugin warns on a spec step that asks for claude-code-large (part of #2688).

BGD, 6 November 2026, after the 7-day load analysis: every large request came
from a spec sent through the plugin, and measured without page cache 96% of
the large attempts fit 6 CPU and 16 GiB. The bridge still submits the step as
written -- a warning, never a refusal -- and its answer names the medium
profile and the measured share, so the person picks the size next time.
"""

from __future__ import annotations

import copy
import json
import re

from swarm_common.profiles import RUNNER_PROFILES
from swarm_mcp import server, workflows

from test_plugin_agents_and_workflows import _ANSWERS, _RUN_JS, _SPEC, _run
from test_plugin_rows_match_console import _FIXTURE, _Recorder


def _spec(*profiles: str | None) -> dict:
    steps = []
    for i, profile in enumerate(profiles):
        step = {"step_id": f"s{i}", "prompt": "do it"}
        if profile is not None:
            step["runner_profile"] = profile
        steps.append(step)
    return {**_FIXTURE["chain"], "steps": steps}


def test_a_large_step_is_submitted_and_answered_with_a_warning_naming_medium():
    recorder = _Recorder()
    reply = json.loads(server._call(recorder, "swarm_workflow", {"spec": _spec("claude-code-large", None)}))
    # MUTATION: refuse the step instead of warning and nothing is sent.
    ((_, _, payload),) = recorder.sent
    assert payload["steps"][0]["runner_profile"] == "claude-code-large"
    # MUTATION: drop the warning from the reply and this is a KeyError.
    (warning,) = reply["warnings"]
    assert "s0" in warning
    assert "claude-code-medium" in warning
    assert "96%" in warning
    assert "claude-code-large" in warning


def test_a_spec_with_no_large_step_carries_no_warnings_key():
    reply = json.loads(server._call(_Recorder(), "swarm_workflow", {"spec": _spec("claude-code", "claude-code-medium")}))
    # MUTATION: always add `warnings` and every answer grows an empty list.
    assert "warnings" not in reply


def test_the_warning_is_one_line_per_large_step_in_spec_order():
    steps = workflows.build_steps(_spec("claude-code-large", "claude-code", "claude-code-large")["steps"])
    lines = workflows.large_step_warnings(steps)
    assert [line.split(" ", 2)[1] for line in lines] == ["s0", "s2"]
    # MUTATION: point the warning at a profile that does not exist.
    assert workflows.MEDIUM_PROFILE in RUNNER_PROFILES
    assert RUNNER_PROFILES[workflows.MEDIUM_PROFILE].resource_class == "medium"


# --- /sc:swarmcloud's run.js says the same before it submits -----------------


def test_run_js_restates_the_bridges_names_and_share():
    source = _RUN_JS.read_text(encoding="utf-8")
    for name in ("LARGE_PROFILE", "MEDIUM_PROFILE", "LARGE_FIT_MEDIUM_SHARE"):
        found = re.search(rf"^const {name} = '([^']*)'$", source, re.MULTILINE)
        # MUTATION: change the share in one place only and the two disagree.
        assert found and found.group(1) == getattr(workflows, name), name


def test_run_js_logs_the_warning_and_still_submits_a_large_step(tmp_path):
    spec = copy.deepcopy(_SPEC)
    spec["steps"][2]["runner_profile"] = "claude-code-large"
    answers = copy.deepcopy(_ANSWERS)
    answers["SUBMIT"]["spec_digest"] = workflows.spec_digest(spec)
    got = _run(tmp_path, spec, answers)
    # MUTATION: return before the submit row on a large step and no SUBMIT call is made.
    assert got["calls"][0]["prompt"].startswith("SUBMIT")
    warned = [line for line in got["logs"] if "asks for claude-code-large" in line]
    assert warned == [
        "step join asks for claude-code-large; 96% of large attempts over 7 days fit "
        "claude-code-medium: name it unless this step was OOM-killed on it"
    ], got["logs"]


def test_run_js_logs_no_warning_without_a_large_step(tmp_path):
    got = _run(tmp_path, _SPEC, _ANSWERS)
    assert not [line for line in got["logs"] if "claude-code-large" in line]
