"""The context ceiling: a session past 200k tokens or 150 tool calls goes on in a fresh one (TOKEN-CEILING, part of #2524).

BGD, 2026-10-27 (#2524 item 3): the runner stops the CLI at the turn boundary
after a turn whose `cache_read + input` passes 200,000 tokens, or once the
session has made more than 150 tool calls, commits the tree, writes a handoff
and starts a NEW session with it as the prompt's prefix -- never `--resume`.
A resume the worker asks for, or a finish pass, of a session that ended over
the ceiling does the same. The fresh sessions per attempt are bounded.

What these hold, on a fake claude-code replaying a stream (no network, no
cloud): the stop at that boundary and the fresh start with its handoff; the
same at 151 tool calls; a resume and a finish pass over the ceiling become a
fresh session; the bound; a session under both limits runs exactly as before;
and `result_summary` gets each stop's turn, tokens, tool calls and why.
"""

from __future__ import annotations

import json
import secrets as pysecrets
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from agent_worker.runners import ceiling, claude_code
from agent_worker.runners.base import RunnerContext
from agent_worker.runners.cliagent import (
    FINISH_PROMPT,
    RESUME_REASON_ENV,
    RESUME_SESSION_ENV,
    SESSION_FILE_ENV,
    CliAgentSpec,
    run_cli_agent,
)
from test_cli_prompt_names_time_limit import without_limit_line

#: A fake claude-code. Each start appends its argv and stdin to
#: `$HOME/starts.jsonl` and plays the next entry of `$HOME/plan.json`: one
#: turn per `contexts` value (that turn's `input_tokens`), each with `tools`
#: tool calls, a turn boundary after every turn but the last, and a marker per
#: turn it got to print. `pause` is slept before each boundary, `hold` instead
#: from turn `hold_from` on.
FAKE_CLI = r"""#!{python}
import json, os, sys, time
home = os.environ["HOME"]
stdin = sys.stdin.read()
starts = os.path.join(home, "starts.jsonl")
n = sum(1 for _ in open(starts)) if os.path.exists(starts) else 0
with open(starts, "a") as f:
    f.write(json.dumps({{"argv": sys.argv[1:], "stdin": stdin}}) + "\n")
plan = json.load(open(os.path.join(home, "plan.json")))
step = plan[min(n, len(plan) - 1)]
sid = sys.argv[sys.argv.index("--resume") + 1] if "--resume" in sys.argv else f"sess-{{n:04d}}-abcdef"
print(json.dumps({{"type": "system", "subtype": "init", "session_id": sid}}), flush=True)
contexts = step["contexts"]
for turn, context in enumerate(contexts, 1):
    with open(os.path.join(home, "marks"), "a") as f:
        f.write(f"{{n}}:{{turn}}\n")
    content = [{{"type": "tool_use", "id": f"t{{n}}-{{turn}}-{{k}}", "name": "Bash",
                 "input": {{"command": "true"}}}} for k in range(step.get("tools", 1))]
    if turn == len(contexts):
        content = [{{"type": "text", "text": step.get("answer", "done")}}]
    print(json.dumps({{"type": "assistant", "session_id": sid, "message": {{
        "id": f"m{{n}}-{{turn}}", "content": content,
        "usage": {{"input_tokens": context, "cache_read_input_tokens": 0, "output_tokens": 10}}}}}}),
        flush=True)
    if turn == len(contexts):
        break
    held = step.get("hold_from") and turn >= step["hold_from"]
    time.sleep(step["hold"] if held else step.get("pause", 0.0))
    print(json.dumps({{"type": "user", "session_id": sid,
                      "message": {{"content": [{{"type": "tool_result"}}]}}}}), flush=True)
print(json.dumps({{"type": "result", "subtype": "success", "is_error": False,
                  "session_id": sid, "result": step.get("answer", "done"),
                  "usage": {{"input_tokens": sum(contexts)}}}}), flush=True)
"""


def _spec() -> CliAgentSpec:
    return CliAgentSpec(name="fake", provider="anthropic", binary_env="FAKE_BIN",
                        binary_default="fake", args_env="FAKE_ARGS", args_default=(),
                        key_env="FAKE_KEY", model_flag=None, resume_flag="--resume",
                        finish_on_pending=True, context_ceiling=True)


@pytest.fixture
def ctx(tmp_path, monkeypatch) -> RunnerContext:
    cli = tmp_path / "fake-claude"
    cli.write_text(FAKE_CLI.format(python=sys.executable))
    cli.chmod(0o755)
    monkeypatch.setenv("FAKE_BIN", str(cli))
    monkeypatch.setenv("FAKE_KEY", "k-" + pysecrets.token_hex(8))
    for name in (RESUME_SESSION_ENV, RESUME_REASON_ENV, SESSION_FILE_ENV, "SWARM_ACCOUNT_STREAM",
                 "SWARM_ACCOUNT_MOVE", "SWARM_CLONE_BASE"):
        monkeypatch.delenv(name, raising=False)
    work, artifacts = tmp_path / "work", tmp_path / "artifacts"
    work.mkdir()
    artifacts.mkdir()
    return RunnerContext(work_dir=work, artifacts_dir=artifacts, input_path=work / "input.json",
                         result_path=work / "result.json", quota_path=work / "quota.json",
                         payload={"prompt": "do the work"})


def _plan(ctx: RunnerContext, *starts: dict[str, Any]) -> None:
    (ctx.work_dir / "plan.json").write_text(json.dumps(list(starts)))


def _starts(ctx: RunnerContext) -> list[dict[str, Any]]:
    path = ctx.work_dir / "starts.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


SMALL = {"contexts": [1_000, 2_000, 3_000]}


def test_a_turn_past_200k_stops_at_that_boundary_and_a_fresh_session_follows(ctx):
    _plan(ctx, {"contexts": [50_000, 150_000, 210_000, 220_000, 230_000, 240_000], "pause": 0.5},
          SMALL)
    out = run_cli_agent(ctx, _spec())

    first, second = _starts(ctx)
    assert "--resume" not in first["argv"]
    # MUTATION: resume the stopped session and `--resume` is in the argv.
    assert "--resume" not in second["argv"]
    assert second["stdin"].startswith("CONTEXT CEILING HANDOFF")
    assert "do the work" in second["stdin"], "the task prompt is in the handoff"
    # Stopped at the boundary after turn 3, long before the stream's turn 6.
    marks = (ctx.work_dir / "marks").read_text().split()
    assert "0:6" not in marks and "1:3" in marks
    (stop,) = out["context_ceiling"]["stops"]
    assert stop["why"] == ceiling.WHY_CONTEXT and stop["at"] == ceiling.AT_TURN_BOUNDARY
    assert stop["turn"] == 3 and stop["context_tokens"] == 210_000
    assert out["context_ceiling"]["fresh_sessions"] == 1
    assert ctx.report["context_ceiling"]["stops"] == [stop]
    # The stopped session printed no `result` event; its tokens still count,
    # added to the fresh session's (1,000 + 2,000 + 3,000).
    # MUTATION: keep only the `result` event's spend and this reads 6,000.
    assert out["structured_output"]["usage"]["input_tokens"] == 410_000 + 6_000


def test_151_tool_calls_stop_the_session_the_same_way(ctx):
    # Quick turns, then a held boundary from turn 150 on, so the CLI is
    # still running when the watcher reads the crossing.
    _plan(ctx, {"contexts": [1_000] * 170, "pause": 0.0, "hold_from": 150, "hold": 1.0}, SMALL)
    out = run_cli_agent(ctx, _spec())

    first, second = _starts(ctx)
    assert "--resume" not in second["argv"]
    assert second["stdin"].startswith("CONTEXT CEILING HANDOFF")
    (stop,) = out["context_ceiling"]["stops"]
    assert stop["why"] == ceiling.WHY_TOOL_CALLS
    assert stop["tool_calls"] == 151 and stop["turn"] == 151


def test_a_session_under_both_limits_is_unchanged(ctx):
    _plan(ctx, {"contexts": [1_000] * 20 + [199_000], "pause": 0.0})
    out = run_cli_agent(ctx, _spec())

    (only,) = _starts(ctx)
    assert "CONTEXT CEILING HANDOFF" not in only["stdin"]
    # The platform's note: the agent may keep a handoff note.
    assert ceiling.HANDOFF_NOTE in only["stdin"]
    assert (ctx.work_dir / "marks").read_text().split()[-1] == "0:21"
    assert out["context_ceiling"]["stops"] == []


def test_the_fresh_sessions_are_bounded(ctx):
    over = {"contexts": [250_000] * 6, "pause": 1.0}
    _plan(ctx, over)
    out = run_cli_agent(ctx, _spec())

    starts = _starts(ctx)
    # MUTATION: no bound and the lane restarts until the step's timeout.
    assert len(starts) == 1 + ceiling.MAX_FRESH_SESSIONS
    assert len(out["context_ceiling"]["stops"]) == ceiling.MAX_FRESH_SESSIONS
    # The last one ran to its end.
    assert f"{ceiling.MAX_FRESH_SESSIONS}:6" in (ctx.work_dir / "marks").read_text().split()


def _resume_from(ctx: RunnerContext, monkeypatch, context_tokens: int) -> str:
    private = ctx.work_dir.parent / "private"
    private.mkdir()
    session = "sess-resumed-" + pysecrets.token_hex(4)
    monkeypatch.setenv(SESSION_FILE_ENV, str(private / "session.json"))
    monkeypatch.setenv(RESUME_SESSION_ENV, session)
    monkeypatch.setenv(RESUME_REASON_ENV, "moved")
    (private / ceiling.STATE_NAME).write_text(json.dumps(
        {"session_id": session, "context_tokens": context_tokens, "tool_calls": 40,
         "turns": 40, "fresh_sessions": 0}))
    return session


def test_a_resume_of_a_session_over_the_ceiling_is_a_fresh_session(ctx, monkeypatch):
    session = _resume_from(ctx, monkeypatch, 452_706)
    _plan(ctx, SMALL)
    out = run_cli_agent(ctx, _spec())

    (only,) = _starts(ctx)
    assert "--resume" not in only["argv"] and session not in only["argv"]
    assert only["stdin"].startswith("CONTEXT CEILING HANDOFF")
    assert "do the work" in only["stdin"]
    (stop,) = out["context_ceiling"]["stops"]
    assert stop["at"] == ceiling.AT_RESUME and stop["context_tokens"] == 452_706
    assert stop["resume_reason"] == "moved"


def test_a_resume_under_the_ceiling_still_resumes(ctx, monkeypatch):
    session = _resume_from(ctx, monkeypatch, 120_000)
    _plan(ctx, SMALL)
    out = run_cli_agent(ctx, _spec())

    (only,) = _starts(ctx)
    assert only["argv"][-2:] == ["--resume", session] or session in only["argv"]
    assert "CONTEXT CEILING HANDOFF" not in only["stdin"]
    assert out["context_ceiling"]["stops"] == []


def test_a_finish_pass_over_the_ceiling_is_a_fresh_session(ctx):
    _plan(ctx, {"contexts": [1_000, 260_000], "answer": "The suite is still running in the background."},
          SMALL)
    out = run_cli_agent(ctx, _spec())

    first, second = _starts(ctx)
    assert "--resume" not in second["argv"]
    assert second["stdin"].startswith("CONTEXT CEILING HANDOFF")
    # MUTATION: drop the finish pass's limit line in run_cli_agent -> red.
    assert without_limit_line(second["stdin"]).endswith(FINISH_PROMPT)
    (stop,) = out["context_ceiling"]["stops"]
    assert stop["at"] == ceiling.AT_RESUME and stop["resume_reason"] == "finish"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                          text=True).stdout.strip()


def test_the_handoff_commits_the_tree_and_carries_the_branch_and_the_agents_note(
    ctx, monkeypatch
):
    repo = ctx.work_dir / "repo"
    repo.mkdir()
    for name, value in (("GIT_AUTHOR_NAME", "Lane"), ("GIT_AUTHOR_EMAIL", "lane@example.invalid"),
                        ("GIT_COMMITTER_NAME", "Lane"),
                        ("GIT_COMMITTER_EMAIL", "lane@example.invalid")):
        monkeypatch.setenv(name, value)
    _git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("a\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    monkeypatch.setenv("SWARM_CLONE_BASE", base)
    (repo / "b.txt").write_text("work in progress\n")
    (ctx.artifacts_dir / ceiling.AGENT_HANDOFF_NAME).write_text("Left: the docs section.")
    ctx.repo_dir = repo
    _plan(ctx, {"contexts": [210_000, 1_000, 1_000], "pause": 1.0}, SMALL)
    out = run_cli_agent(ctx, _spec())

    _, second = _starts(ctx)
    assert "b.txt" in second["stdin"], "the diff stat against the base"
    assert "Left: the docs section." in second["stdin"]
    assert "b.txt" in _git(repo, "show", "--stat", "HEAD")
    (stop,) = out["context_ceiling"]["stops"]
    assert stop["wip"]["status"] == "committed"


def test_a_profile_or_step_lowers_the_ceiling_and_never_raises_it():
    assert ceiling.resolve_limits({}, None) == ceiling.CeilingLimits(200_000, 150)
    lowered = ceiling.resolve_limits({"context_ceiling": {"tokens": 50_000, "tool_calls": 20}}, None)
    assert lowered == ceiling.CeilingLimits(50_000, 20)
    raised = ceiling.resolve_limits({"context_ceiling": {"tokens": 10**9, "tool_calls": 10**6}}, None)
    assert raised == ceiling.CeilingLimits(ceiling.CONTEXT_CEILING_HARD_CAP,
                                           ceiling.TOOL_CALL_HARD_CAP)
    assert ceiling.resolve_limits({"context_ceiling": {"tokens": True, "tool_calls": -1}}, None) \
        == ceiling.CeilingLimits()


def test_claude_code_watches_the_ceiling_and_tells_the_agent_about_the_handoff_note():
    assert claude_code.SPEC.context_ceiling is True
    assert "$SWARM_ARTIFACTS_DIR/handoff.md" in ceiling.HANDOFF_NOTE


def test_a_subagents_context_is_not_the_sessions():
    watcher = ceiling.CeilingWatcher(ceiling.CeilingLimits())
    watcher.begin(fresh=True)
    feed = watcher.tap()
    sub = {"type": "assistant", "parent_tool_use_id": "t1", "message": {
        "id": "s1", "usage": {"input_tokens": 300_000}}}
    feed((json.dumps(sub) + "\n" + json.dumps(
        {"type": "user", "message": {"content": []}}) + "\n").encode())
    assert watcher.poll() is False and watcher.context_tokens == 0


def test_the_stream_is_read_from_the_pipe_not_assumed_whole():
    watcher = ceiling.CeilingWatcher(ceiling.CeilingLimits(tokens=10))
    watcher.begin(fresh=True)
    feed = watcher.tap()
    line = json.dumps({"type": "assistant", "message": {"id": "m", "usage": {"input_tokens": 11}}})
    feed(line[:10].encode())
    assert watcher.poll() is False
    feed((line[10:] + "\n").encode())
    assert watcher.poll() is False and watcher.passed == ceiling.WHY_CONTEXT
    feed(b'{"type": "user"}\n')
    assert watcher.poll() is True and watcher.stopped_for == ceiling.WHY_CONTEXT
