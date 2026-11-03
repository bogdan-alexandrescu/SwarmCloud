"""A switched-on tool is told WHEN it helps, at the system prompt's weight (part of #2688).

Experiment batch two was stopped on 2 November 2026 ("Stop everything"): six
build steps with code intelligence on called the graph 1, 0, 0, 0, 0 and 0
times against 200-560 built-in calls each, and six with Jev on called
`jev_decide` 0 times. Their transcripts (batch three's too, after the fix that
loaded the tools up front) show every graph and Jev tool `connected` and
listed in the session's init, and so loaded and not chosen: the one line
naming them came after the whole brief, while CLAUDE.md, which the CLI loads
into the system context, says "`grep -n` or `rg` first" -- and the agents
quoted that rule to their helpers. Nothing named a moment for `jev_decide`.

So, in the Claude Code runner, a step with the switch on gets:

* `--append-system-prompt` carrying a short block per switched-on tool,
  naming the moments it beats a text search (code graph: before editing, the
  tests to run, before finishing) or a solo call (Jev: a choice, the
  acceptance check, a failure of unclear cause). The system prompt is carried
  into every session of the attempt: a resumed one and a fresh session after
  a context handoff, whose own prompt is only the handoff;
* the same tool descriptions, which now say when each tool is worth a call.

A step with both switches off gets no system-prompt block at all.

Offline: the CLI is never started; `run_cli_agent` is replaced.
"""

from __future__ import annotations

import pytest

from agent_worker import graphstage
from agent_worker.graphmcp import tools as graph_tools
from agent_worker.judge import mcp as judge_mcp
from agent_worker.runners import claude_code

from test_agents_use_tools import _graph_env, _judge_config, _run, _served_env

FLAG = "--append-system-prompt"


def _system(seen: dict) -> str | None:
    args = seen["args"]
    if FLAG not in args:
        return None
    assert args.count(FLAG) == 1
    return args[args.index(FLAG) + 1]


# --------------------------------------------------------------------------
# the code graph
# --------------------------------------------------------------------------

@pytest.mark.parametrize("env_of", [_graph_env, _served_env], ids=["staged", "served"])
def test_a_switched_on_graph_puts_its_moments_in_the_system_prompt(monkeypatch, tmp_path, env_of):
    seen = _run(monkeypatch, tmp_path, env_of(tmp_path))
    system = _system(seen)
    # MUTATION: leave the block in the user prompt only and it is weighed
    # below CLAUDE.md's grep-first rule again.
    assert system is not None and claude_code.GRAPH_SYSTEM_NOTE in system
    assert claude_code.JEV_SYSTEM_NOTE not in system


def test_the_graph_block_names_when_each_call_helps():
    note = claude_code.GRAPH_SYSTEM_NOTE
    for tool in ("`context`", "`impact`", "`tests_for`"):
        assert tool in note
    for moment in ("Before you edit", "Before you run tests", "Before you finish"):
        assert moment in note
    # It answers CLAUDE.md's rule rather than leaving the two to compete.
    assert "grep" in note and "by window" in note
    # It does not force calls that add nothing.
    assert "Skip" in note
    # Zero is unknown, not none (`tools.UNKNOWN_CALLERS`).
    assert "unknown" in note
    assert len(note) < 1600


def test_a_switched_off_step_gets_no_system_prompt_block(monkeypatch, tmp_path):
    seen = _run(monkeypatch, tmp_path, {})
    assert _system(seen) is None
    assert claude_code.GRAPH_SYSTEM_NOTE not in seen["prompt"]
    assert claude_code.JEV_SYSTEM_NOTE not in seen["prompt"]


def test_an_unstaged_graph_gets_no_graph_block(monkeypatch, tmp_path):
    seen = _run(monkeypatch, tmp_path, {graphstage.UNSTAGED_ENV: "the snapshot could not be read"})
    assert _system(seen) is None


# --------------------------------------------------------------------------
# Jev's decision tool
# --------------------------------------------------------------------------

def test_a_switched_on_jev_puts_its_moments_in_the_system_prompt(monkeypatch, tmp_path):
    seen = _run(monkeypatch, tmp_path, {claude_code.JUDGE_CONFIG_ENV: str(_judge_config(tmp_path))})
    system = _system(seen)
    assert system is not None and claude_code.JEV_SYSTEM_NOTE in system
    assert claude_code.GRAPH_SYSTEM_NOTE not in system


def test_a_jev_config_that_is_not_a_file_gets_no_block(monkeypatch, tmp_path):
    seen = _run(monkeypatch, tmp_path, {claude_code.JUDGE_CONFIG_ENV: str(tmp_path / "absent.json")})
    assert _system(seen) is None


def test_the_jev_block_names_when_a_call_helps():
    note = claude_code.JEV_SYSTEM_NOTE
    assert f"`{judge_mcp.TOOL_NAME}`" in note
    for moment in ("choice", "acceptance", "fails"):
        assert moment in note
    # Not for what the agent can check itself; advisory; the shared budget.
    assert "check yourself" in note and "advisory" in note and "budget" in note
    assert len(note) < 1200


def test_both_switches_on_get_both_blocks_in_one_flag(monkeypatch, tmp_path):
    env = {**_graph_env(tmp_path), claude_code.JUDGE_CONFIG_ENV: str(_judge_config(tmp_path))}
    system = _system(_run(monkeypatch, tmp_path, env))
    assert system is not None
    assert claude_code.GRAPH_SYSTEM_NOTE in system and claude_code.JEV_SYSTEM_NOTE in system


def test_system_note_is_none_with_both_off():
    assert claude_code.system_note(graph=False, jev=False) is None
    assert claude_code.system_note(graph=True, jev=False) == claude_code.GRAPH_SYSTEM_NOTE


# --------------------------------------------------------------------------
# the tool descriptions say when each tool is worth a call
# --------------------------------------------------------------------------

def _description(name: str) -> str:
    return next(t["description"] for t in graph_tools.TOOLS if t["name"] == name)


def test_the_graph_descriptions_say_when_they_beat_grep():
    assert "before editing" in _description("context")
    assert "before finishing" in _description("impact")
    # The two batch-three calls asked tests_for by bare or Class.method names
    # and got `unknown` and zero rows: files answer most fully.
    assert "pass files" in _description("tests_for")
    assert "grep" in _description("call_graph")
    assert "defined" in _description("search")


def test_the_decision_tool_description_says_when_to_call_it():
    description = judge_mcp.TOOL["description"]
    assert "Call it" in description
    for moment in ("choosing", "acceptance", "fails"):
        assert moment in description
