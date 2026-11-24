"""The context ceiling: a session past 200k tokens or 150 tool calls goes on in a fresh one (TOKEN-CEILING, part of archived issue 2524).

the owner, 2026-10-27 (archived issue 2524 item 3): the runner stops the CLI at the turn boundary
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
#: from turn `hold_from` on. `creations`, when given, is each turn's
#: `cache_creation_input_tokens` (0 otherwise).
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
creations = step.get("creations") or [0] * len(contexts)
for turn, context in enumerate(contexts, 1):
    with open(os.path.join(home, "marks"), "a") as f:
        f.write(f"{{n}}:{{turn}}\n")
    content = [{{"type": "tool_use", "id": f"t{{n}}-{{turn}}-{{k}}", "name": "Bash",
                 "input": {{"command": "true"}}}} for k in range(step.get("tools", 1))]
    if turn == len(contexts):
        content = [{{"type": "text", "text": step.get("answer", "done")}}]
    print(json.dumps({{"type": "assistant", "session_id": sid, "message": {{
        "id": f"m{{n}}-{{turn}}", "content": content,
        "usage": {{"input_tokens": context, "cache_read_input_tokens": 0,
                   "cache_creation_input_tokens": creations[turn - 1], "output_tokens": 10}}}}}}),
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
    # Under the swap hand-off threshold too (`ceiling.SWAP_HANDOFF_CONTEXT_TOKENS`).
    session = _resume_from(ctx, monkeypatch, 60_000)
    _plan(ctx, SMALL)
    out = run_cli_agent(ctx, _spec())

    (only,) = _starts(ctx)
    assert only["argv"][-2:] == ["--resume", session] or session in only["argv"]
    assert "CONTEXT CEILING HANDOFF" not in only["stdin"]
    assert out["context_ceiling"]["stops"] == []


# A SWAP OF A LARGE SESSION GOES FRESH (the owner's "Shorter sessions" plan,
# finding 3, 2026-11-23). A swap's `--resume` writes the whole transcript to
# the new account's prompt cache, and the turn the session last ran may hold
# much of it as `cache_creation_input_tokens` -- the cold first turn on the
# account it was moved to, or the tool results its turn just added -- which
# `cache_read + input` does not count. Each test moves one session twice: the
# first restart resumes it (its recorded context is under the ceiling), its
# one turn on the new account writes `creation` tokens to cache, and the
# second restart is judged on what that turn left.


def _moved_twice(ctx: RunnerContext, monkeypatch, creation: int, *, fresh_sessions: int = 0,
                 then: dict[str, Any] = SMALL, recorded: int = 60_000) -> tuple[str, Any]:
    session = _resume_from(ctx, monkeypatch, recorded)
    if fresh_sessions:
        path = ctx.work_dir.parent / "private" / ceiling.STATE_NAME
        path.write_text(json.dumps({**json.loads(path.read_text()),
                                    "fresh_sessions": fresh_sessions}))
    _plan(ctx, {"contexts": [2_000], "creations": [creation]}, then)
    first = run_cli_agent(ctx, _spec())
    assert first["context_ceiling"]["stops"] == [], "the first move resumes"
    return session, first


def test_a_swap_whose_last_turn_wrote_its_context_to_cache_past_the_ceiling_is_fresh(
    ctx, monkeypatch
):
    session, _ = _moved_twice(ctx, monkeypatch, 228_000)
    out = run_cli_agent(ctx, _spec())

    first, second = _starts(ctx)
    assert first["argv"][first["argv"].index("--resume") + 1] == session
    # MUTATION: judge the resume on `cache_read + input` alone (2,000) and the
    # second move resumes, writing 230,000 tokens to the new account's cache.
    assert "--resume" not in second["argv"] and session not in second["argv"]
    assert second["stdin"].startswith("CONTEXT CEILING HANDOFF")
    assert "do the work" in second["stdin"], "the task prompt is in the handoff"
    (stop,) = out["context_ceiling"]["stops"]
    assert stop["at"] == ceiling.AT_RESUME and stop["why"] == ceiling.WHY_CONTEXT
    assert stop["resume_reason"] == "moved" and stop["context_tokens"] == 230_000
    assert out["context_ceiling"]["fresh_sessions"] == 1


def test_a_swap_whose_whole_prompt_is_under_the_ceiling_still_resumes(ctx, monkeypatch):
    # 62,000: under the ceiling and under the swap hand-off threshold.
    session, _ = _moved_twice(ctx, monkeypatch, 60_000)
    out = run_cli_agent(ctx, _spec())

    first, second = _starts(ctx)
    # MUTATION: count something larger than the turn's prompt (the session's
    # peak, the step's spend) and a session under the ceiling goes fresh.
    assert second["argv"][second["argv"].index("--resume") + 1] == session
    assert "CONTEXT CEILING HANDOFF" not in second["stdin"]
    assert out["context_ceiling"]["stops"] == []


def test_a_swap_judges_the_prompt_against_a_lowered_ceiling(ctx, monkeypatch):
    ctx.payload = {**ctx.payload, "context_ceiling": {"tokens": 100_000}}
    _moved_twice(ctx, monkeypatch, 110_000, recorded=50_000)
    out = run_cli_agent(ctx, _spec())

    _, second = _starts(ctx)
    # MUTATION: compare with CONTEXT_CEILING_TOKENS instead of the resolved
    # limits and 112,000 resumes.
    assert "--resume" not in second["argv"]
    (stop,) = out["context_ceiling"]["stops"]
    assert stop["context_tokens"] == 112_000


def test_a_swap_driven_handoff_counts_toward_the_fresh_sessions_bound(ctx, monkeypatch):
    # One fresh session left; the session the swap hands off to passes the
    # ceiling again and is NOT stopped, because the handoff spent the last one.
    over = {"contexts": [250_000] * 4, "pause": 0.5}
    _moved_twice(ctx, monkeypatch, 228_000, fresh_sessions=ceiling.MAX_FRESH_SESSIONS - 1,
                 then=over)
    out = run_cli_agent(ctx, _spec())

    starts = _starts(ctx)
    assert len(starts) == 2, "the handoff's session ran to its end: no fifth fresh session"
    assert starts[1]["stdin"].startswith("CONTEXT CEILING HANDOFF")
    (stop,) = out["context_ceiling"]["stops"]
    assert stop["at"] == ceiling.AT_RESUME
    assert out["context_ceiling"]["fresh_sessions"] == ceiling.MAX_FRESH_SESSIONS
    assert "1:4" in (ctx.work_dir / "marks").read_text().split()


def test_a_swap_over_the_ceiling_with_the_fresh_sessions_spent_resumes(ctx, monkeypatch):
    session, _ = _moved_twice(ctx, monkeypatch, 228_000,
                              fresh_sessions=ceiling.MAX_FRESH_SESSIONS)
    out = run_cli_agent(ctx, _spec())

    _, second = _starts(ctx)
    # The bound holds: once spent, the ceiling stops nothing more, a swap's
    # handoff included, and the session carries on where it was.
    assert second["argv"][second["argv"].index("--resume") + 1] == session
    assert out["context_ceiling"]["stops"] == []
    assert out["context_ceiling"]["fresh_sessions"] == ceiling.MAX_FRESH_SESSIONS


def test_a_resume_with_a_persons_answer_over_the_ceiling_still_resumes(ctx, monkeypatch):
    from agent_worker.runners.cliagent import QUESTION_ANSWER_FILE_ENV, RESUME_ANSWERED

    session, _ = _moved_twice(ctx, monkeypatch, 228_000)
    answer = ctx.work_dir.parent / "private" / "answer.txt"
    answer.write_text("Use the staging scope.")
    monkeypatch.setenv(RESUME_REASON_ENV, RESUME_ANSWERED)
    monkeypatch.setenv(QUESTION_ANSWER_FILE_ENV, str(answer))
    out = run_cli_agent(ctx, _spec())

    _, second = _starts(ctx)
    # The answered question is in that session alone: never a handoff.
    assert second["argv"][second["argv"].index("--resume") + 1] == session
    assert second["stdin"].startswith("Use the staging scope.")
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


# A SWAP OF A SESSION PAST THE HAND-OFF THRESHOLD GOES FRESH (wave 3, shorter
# sessions). A swap's `--resume` writes the whole prompt to the new account's
# cache; past `ceiling.SWAP_HANDOFF_CONTEXT_TOKENS` (half the ceiling) the
# runner starts a fresh session from the hand-off note instead, inside the
# same fresh-session cap, and records which path the swap took: in the
# ceiling's summary and on the account channel the worker reads after a swap.


def _channel(ctx: RunnerContext, monkeypatch) -> Path:
    channel = ctx.work_dir.parent / "private" / "channel.json"
    monkeypatch.setenv("SWARM_ACCOUNT_STREAM", str(channel))
    return channel


def _swap_path(channel: Path) -> Any:
    return json.loads(channel.read_text()).get("swap_path")


def _with_state(ctx: RunnerContext, **fields: Any) -> None:
    path = ctx.work_dir.parent / "private" / ceiling.STATE_NAME
    path.write_text(json.dumps({**json.loads(path.read_text()), **fields}))


def test_the_swap_threshold_is_half_the_ceiling_and_never_above_a_lowered_one():
    assert ceiling.SWAP_HANDOFF_CONTEXT_TOKENS == ceiling.CONTEXT_CEILING_TOKENS // 2
    assert ceiling.swap_threshold(ceiling.resolve_limits(None, None)) == 100_000
    lowered = ceiling.resolve_limits({"context_ceiling": {"tokens": 60_000}}, None)
    assert ceiling.swap_threshold(lowered) == 60_000


def test_a_swap_above_the_threshold_hands_off_from_the_note(ctx, monkeypatch):
    session = _resume_from(ctx, monkeypatch, 120_000)
    channel = _channel(ctx, monkeypatch)
    (ctx.artifacts_dir / ceiling.AGENT_HANDOFF_NAME).write_text("Done: the parser. Left: tests.")
    _plan(ctx, SMALL)
    out = run_cli_agent(ctx, _spec())

    (only,) = _starts(ctx)
    # MUTATION: judge a swap against the ceiling (200,000) and 120,000 resumes.
    assert "--resume" not in only["argv"] and session not in only["argv"]
    assert only["stdin"].startswith("CONTEXT CEILING HANDOFF")
    assert "Left: tests." in only["stdin"], "the fresh session starts from the note"
    (stop,) = out["context_ceiling"]["stops"]
    assert stop["at"] == ceiling.AT_RESUME and stop["why"] == ceiling.WHY_CONTEXT
    assert stop["context_tokens"] == 120_000 and stop["resume_reason"] == "moved"
    assert out["context_ceiling"]["fresh_sessions"] == 1
    assert out["context_ceiling"]["swap"] == {
        "path": ceiling.SWAP_HANDED_OFF, "context_tokens": 120_000, "threshold_tokens": 100_000}
    assert _swap_path(channel) == ceiling.SWAP_HANDED_OFF


def test_a_swap_below_the_threshold_resumes_as_today(ctx, monkeypatch):
    session = _resume_from(ctx, monkeypatch, 90_000)
    channel = _channel(ctx, monkeypatch)
    _plan(ctx, SMALL)
    out = run_cli_agent(ctx, _spec())

    (only,) = _starts(ctx)
    assert only["argv"][only["argv"].index("--resume") + 1] == session
    assert "CONTEXT CEILING HANDOFF" not in only["stdin"]
    assert out["context_ceiling"]["stops"] == []
    assert out["context_ceiling"]["swap"] == {
        "path": ceiling.SWAP_RESUMED, "context_tokens": 90_000, "threshold_tokens": 100_000}
    assert _swap_path(channel) == ceiling.SWAP_RESUMED


def test_a_credential_reload_above_the_swap_threshold_still_resumes(ctx, monkeypatch):
    # The same account, its cache warm: a reload is judged on the ceiling.
    session = _resume_from(ctx, monkeypatch, 120_000)
    monkeypatch.setenv(RESUME_REASON_ENV, "credential_reloaded")
    _plan(ctx, SMALL)
    out = run_cli_agent(ctx, _spec())

    (only,) = _starts(ctx)
    assert only["argv"][only["argv"].index("--resume") + 1] == session
    assert out["context_ceiling"]["stops"] == []
    assert out["context_ceiling"]["swap"] is None, "no swap happened"


def test_a_swap_above_the_threshold_with_the_fresh_sessions_spent_resumes(ctx, monkeypatch):
    session = _resume_from(ctx, monkeypatch, 120_000)
    _with_state(ctx, fresh_sessions=ceiling.MAX_FRESH_SESSIONS)
    channel = _channel(ctx, monkeypatch)
    _plan(ctx, SMALL)
    out = run_cli_agent(ctx, _spec())

    (only,) = _starts(ctx)
    assert only["argv"][only["argv"].index("--resume") + 1] == session
    assert out["context_ceiling"]["stops"] == []
    assert out["context_ceiling"]["fresh_sessions"] == ceiling.MAX_FRESH_SESSIONS
    assert out["context_ceiling"]["swap"]["path"] == ceiling.SWAP_RESUMED
    assert _swap_path(channel) == ceiling.SWAP_RESUMED


def test_a_swap_of_the_fresh_session_a_handoff_started_judges_that_session(ctx, monkeypatch):
    # A hand-off in progress: the session over the ceiling was ended and a
    # fresh one started from the note; the swap moved the fresh one, which is
    # small, so it is resumed and no second fresh session is spent.
    ended = _resume_from(ctx, monkeypatch, 230_000)
    fresh = "sess-fresh-" + pysecrets.token_hex(4)
    monkeypatch.setenv(RESUME_SESSION_ENV, fresh)
    _with_state(ctx, session_id=fresh, context_tokens=30_000, fresh_sessions=1,
                handed_off_sessions=[ended])
    _plan(ctx, SMALL)
    out = run_cli_agent(ctx, _spec())

    (only,) = _starts(ctx)
    assert only["argv"][only["argv"].index("--resume") + 1] == fresh
    assert out["context_ceiling"]["stops"] == []
    assert out["context_ceiling"]["fresh_sessions"] == 1
    assert out["context_ceiling"]["swap"]["path"] == ceiling.SWAP_RESUMED


def test_a_swap_naming_the_session_a_handoff_already_ended_never_resumes_it(ctx, monkeypatch):
    # The swap came before the fresh session named itself, so the worker
    # still holds the ended session's id: resuming it would re-read the very
    # context the hand-off was for. It is handed off again, inside the cap.
    ended = _resume_from(ctx, monkeypatch, 230_000)
    _with_state(ctx, session_id=None, context_tokens=0, tool_calls=0, fresh_sessions=1,
                handed_off_sessions=[ended])
    _plan(ctx, SMALL)
    out = run_cli_agent(ctx, _spec())

    (only,) = _starts(ctx)
    assert "--resume" not in only["argv"] and ended not in only["argv"]
    assert only["stdin"].startswith("CONTEXT CEILING HANDOFF")
    (stop,) = out["context_ceiling"]["stops"]
    assert stop["why"] == ceiling.WHY_HANDED_OFF
    assert out["context_ceiling"]["fresh_sessions"] == 2
    assert out["context_ceiling"]["swap"]["path"] == ceiling.SWAP_HANDED_OFF


def test_a_ceiling_stop_remembers_the_session_it_ended(ctx, monkeypatch):
    private = ctx.work_dir.parent / "private"
    private.mkdir()
    monkeypatch.setenv(SESSION_FILE_ENV, str(private / "session.json"))
    _plan(ctx, {"contexts": [50_000, 210_000, 220_000, 230_000], "pause": 0.5}, SMALL)
    out = run_cli_agent(ctx, _spec())
    assert len(out["context_ceiling"]["stops"]) == 1

    state = json.loads((private / ceiling.STATE_NAME).read_text())
    assert state["handed_off_sessions"] == ["sess-0000-abcdef"]
    assert state["session_id"] == "sess-0001-abcdef"


@pytest.mark.parametrize(("channel", "record"), [
    ({"swap_path": "resumed"}, {"swap_path": "resumed"}),
    ({"swap_path": "handed_off"}, {"swap_path": "handed_off"}),
    ({"swap_path": "anything else"}, {}),
    ({"swap_path": None}, {}),
    ({}, {}),
])
def test_the_worker_reads_the_swap_path_off_the_channel(channel, record):
    assert ceiling.swap_path_of(channel) == record
