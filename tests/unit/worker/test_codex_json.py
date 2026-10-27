"""codex runs `exec --json`, renders its stream readable, and is metered by its budget (CODEX-JSON, part of #2459).

BGD, 2026-10-26 (BUDGET-CAP question 6, "Switch codex to --json"): the codex
profile's stdout carries `turn.completed` usage, so a codex step's budget is
enforced and not only pinned. These run the real codex runner against a fake
CLI that replays a recorded codex-cli 0.154.0 `exec --json` stream.

MUTATIONS: drop `--json` from `codex.SPEC.args_default` (the argv test);
skip `codex_events.finish_capture` in `codex.body` (the log, events and
transcript tests read raw JSON); stop treating a completed tool item as a
codex turn boundary in `budget.UsageMeter.feed` (the boundary tests run past
the stop); stop counting `turn.completed` usage (the meter tests read 0).
"""

from __future__ import annotations

import json
import os
import secrets as pysecrets
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from agent_worker import budget as budget_mod
from agent_worker.budget import BudgetGuard, Limits, UsageMeter
from agent_worker.runners import budgetwatch, cliagent, codex, codex_events
from agent_worker.runners.base import RunnerContext, RunnerFailure

SESSION = "01a17ac4-e427-7213-b3b6-75d33a4d17b7"


def _openai_key() -> str:
    # Built at runtime: no credential-shaped literal in this file.
    return "sk-" + "proj-" + pysecrets.token_hex(16)


def _usage(inp: int, cached: int, out: int) -> dict[str, int]:
    return {"input_tokens": inp, "cached_input_tokens": cached, "output_tokens": out}


def _command(item_id: str, command: str, *, output: str = "", exit_code: int | None = None,
             status: str = "in_progress") -> dict[str, Any]:
    return {"id": item_id, "type": "command_execution", "command": command,
            "aggregated_output": output, "exit_code": exit_code, "status": status}


#: One `codex exec --json` run, in codex-cli 0.154.0's shapes (the first two
#: lines and the error lines as the pinned CLI printed them here, 2026-10-27).
RECORDED: list[dict[str, Any]] = [
    {"type": "thread.started", "thread_id": SESSION},
    {"type": "turn.started"},
    {"type": "error", "message": "Reconnecting... 1/5 (stream disconnected before completion)"},
    {"type": "item.completed", "item": {"id": "item_0", "type": "reasoning",
                                        "text": "**Reading the repository layout**"}},
    {"type": "item.started", "item": _command("item_1", "bash -lc ls")},
    {"type": "item.completed", "item": _command("item_1", "bash -lc ls", output="README.md\napps\n",
                                                exit_code=0, status="completed")},
    {"type": "item.completed", "item": {"id": "item_2", "type": "file_change", "status": "completed",
                                        "changes": [{"path": "README.md", "kind": "update"}]}},
    {"type": "item.completed", "item": {"id": "item_3", "type": "mcp_tool_call", "server": "swarm-git",
                                        "tool": "git_fetch", "status": "completed"}},
    {"type": "item.started", "item": {"id": "item_4", "type": "todo_list",
                                      "items": [{"text": "edit README", "completed": False}]}},
    {"type": "item.updated", "item": {"id": "item_4", "type": "todo_list",
                                      "items": [{"text": "edit README", "completed": True}]}},
    {"type": "item.completed", "item": {"id": "item_4", "type": "todo_list",
                                        "items": [{"text": "edit README", "completed": True}]}},
    {"type": "item.completed", "item": {"id": "item_5", "type": "agent_message",
                                        "text": "Updated the README's first paragraph."}},
    {"type": "turn.completed", "usage": _usage(12_000, 9_000, 800)},
]


def _jsonl(events: list[dict[str, Any]]) -> str:
    return "".join(json.dumps(e) + "\n" for e in events)


#: A codex stand-in: records argv and stdin in `$HOME/runs.jsonl`, writes a
#: rollout naming the session, then prints `$HOME/stream.jsonl` line by line,
#: `$HOME/pause` seconds apart, appending each line's index to `$HOME/marks`.
FAKE_CODEX = r'''#!{python}
import json, os, pathlib, sys, time
home = pathlib.Path(os.environ["HOME"])
with (home / "runs.jsonl").open("a") as f:
    f.write(json.dumps({{"argv": sys.argv[1:], "stdin": sys.stdin.read()}}) + "\n")
pause = float((home / "pause").read_text()) if (home / "pause").exists() else 0.0
for index, line in enumerate((home / "stream.jsonl").read_text().splitlines()):
    with (home / "marks").open("a") as f:
        f.write(f"{{index}}\n")
    print(line, flush=True)
    time.sleep(pause)
'''


def _ctx(tmp_path: Path) -> RunnerContext:
    work, artifacts = tmp_path / "work", tmp_path / "artifacts"
    work.mkdir()
    artifacts.mkdir()
    return RunnerContext(work_dir=work, artifacts_dir=artifacts, input_path=work / "input.json",
                         result_path=work / "result.json", quota_path=work / "quota.json",
                         payload={"prompt": "edit the README"})


def _install(tmp_path: Path, monkeypatch, ctx: RunnerContext, stream: list[dict[str, Any]],
             *, pause: float = 0.0) -> None:
    cli = tmp_path / "fake-codex"
    cli.write_text(FAKE_CODEX.format(python=sys.executable))
    cli.chmod(0o755)
    monkeypatch.setenv("CODEX_BIN", str(cli))
    monkeypatch.setenv("OPENAI_API_KEY", _openai_key())
    for name in (
        "CODEX_ARGS", "MODEL", "SWARM_CHILDREN", "SWARM_GIT", "SWARM_GIT_SPOOL",
        cliagent.ACCOUNT_STREAM_ENV, cliagent.ACCOUNT_MOVE_ENV, cliagent.RESUME_SESSION_ENV,
        cliagent.RESUME_REASON_ENV, cliagent.SESSION_FILE_ENV,
        budget_mod.CHANNEL_ENV, budget_mod.SIGNAL_ENV, budget_mod.NOTE_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    (ctx.work_dir / "stream.jsonl").write_text(_jsonl(stream))
    (ctx.work_dir / "pause").write_text(str(pause))


def _runs(ctx: RunnerContext) -> list[dict[str, Any]]:
    path = ctx.work_dir / "runs.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# --------------------------------------------------------------------------
# 1. The argv: --json, the prompt on stdin
# --------------------------------------------------------------------------


def test_codex_runs_exec_json_with_the_prompt_on_stdin_never_in_argv(tmp_path, monkeypatch):
    assert codex.SPEC.args_default[:2] == ("exec", "--json")
    ctx = _ctx(tmp_path)
    _install(tmp_path, monkeypatch, ctx, RECORDED)
    codex.body(ctx)
    (run,) = _runs(ctx)
    # MUTATION: drop `--json` and codex prints prose, with no usage in it.
    assert run["argv"][:2] == ["exec", "--json"]
    assert run["argv"][-1] == "-"
    assert "edit the README" in run["stdin"]
    assert not any("edit the README" in arg for arg in run["argv"])


# --------------------------------------------------------------------------
# 2. The log: readable, the raw stream kept beside it
# --------------------------------------------------------------------------


def test_a_recorded_stream_renders_to_a_readable_log_and_keeps_the_raw_events(
    tmp_path, monkeypatch
):
    ctx = _ctx(tmp_path)
    _install(tmp_path, monkeypatch, ctx, RECORDED)
    out = codex.body(ctx)

    log = (ctx.artifacts_dir / "codex.stdout.log").read_text()
    # MUTATION: skip the rendering and every line of the Logs tab is JSON.
    assert not [line for line in log.splitlines() if line.lstrip().startswith("{")], log
    assert "Updated the README's first paragraph." in log
    assert "$ bash -lc ls" in log and "    README.md" in log and "exit 0" in log
    assert "!! error: Reconnecting... 1/5" in log
    assert "files (completed): update README.md" in log
    assert "tool swarm-git.git_fetch (completed)" in log
    assert "[x] edit README" in log
    assert "12,000 input tokens (9,000 cached), 800 output tokens" in log
    # The command is one block: its start is replaced by its completion.
    assert log.count("$ bash -lc ls") == 1 and "still running" not in log

    raw = (ctx.artifacts_dir / codex_events.EVENTS_NAME).read_text()
    assert [json.loads(line) for line in raw.splitlines()] == RECORDED

    document = json.loads((ctx.artifacts_dir / "codex-transcript.json").read_text())
    assert document["format"] == codex_events.TRANSCRIPT_FORMAT
    assert document["thread_id"] == SESSION
    assert document["final_message"] == "Updated the README's first paragraph."
    assert document["usage"] == _usage(12_000, 9_000, 800)
    kinds = [step["kind"] for step in document["steps"]]
    assert kinds == ["thread", "turn_started", "error", "reasoning", "command", "file_change",
                     "tool_call", "todo_list", "message", "turn_completed"]

    # The summary is the agent's answer, not the tail of a JSON stream.
    assert out["summary"] == "Updated the README's first paragraph."


def test_a_failed_run_is_rendered_too(tmp_path, monkeypatch):
    stream = [
        {"type": "thread.started", "thread_id": SESSION},
        {"type": "turn.started"},
        {"type": "item.started", "item": _command("item_0", "pytest -q")},
        {"type": "turn.failed", "error": {"message": "stream disconnected before completion"}},
    ]
    ctx = _ctx(tmp_path)
    _install(tmp_path, monkeypatch, ctx, stream)
    # The fake exits 0 after a failed turn; the CLI's own exit decides the
    # outcome, and what matters here is that the log reads either way.
    codex.body(ctx)
    log = (ctx.artifacts_dir / "codex.stdout.log").read_text()
    assert "!! turn failed: stream disconnected before completion" in log
    assert "$ pytest -q\n    (still running when the stream ended)" in log


def test_prose_from_a_cli_started_without_json_is_left_as_it_was(tmp_path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "codex.stdout.log").write_text("done\n")
    assert codex_events.finish_capture(artifacts, "codex.stdout.log", "codex-transcript.json",
                                       lambda s: s) is None
    assert (artifacts / "codex.stdout.log").read_text() == "done\n"
    assert not (artifacts / codex_events.EVENTS_NAME).exists()


def test_a_value_hidden_by_a_json_escape_is_redacted_once_decoded(tmp_path):
    secret = "k" + pysecrets.token_hex(12)
    escaped = "".join(f"\\u{ord(c):04x}" for c in secret)
    line = ('{"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", '
            f'"text": "the value is {escaped}"}}}}\n')
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "codex.stdout.log").write_text(line)
    (artifacts / "codex-transcript.json").write_text("[]")
    message = codex_events.finish_capture(
        artifacts, "codex.stdout.log", "codex-transcript.json",
        lambda s: s.replace(secret, "[REDACTED]"))
    assert message == "the value is [REDACTED]"
    for name in ("codex.stdout.log", "codex-transcript.json"):
        assert secret not in (artifacts / name).read_text()


# --------------------------------------------------------------------------
# 3. The meter: turn.completed usage, warned at 80%, stopped at 100%
# --------------------------------------------------------------------------


class _Log:
    def __init__(self) -> None:
        self.lines: list[tuple[str, dict[str, Any]]] = []

    def info(self, message: str, **fields: Any) -> None:
        self.lines.append((message, fields))

    warning = info


def _guard(tmp_path: Path, limits: Limits) -> tuple[BudgetGuard, list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []
    private = tmp_path / "private"
    private.mkdir(exist_ok=True)
    # can_tell False: `codex exec` takes nothing once started (lifecycle).
    return BudgetGuard(limits, private=private, emit=events.append, log=_Log()), events


def _turn(inp: int, cached: int, out: int) -> list[dict[str, Any]]:
    return [{"type": "turn.started"},
            {"type": "item.completed", "item": {"id": "m", "type": "agent_message", "text": "ok"}},
            {"type": "turn.completed", "usage": _usage(inp, cached, out)}]


def test_codex_usage_drives_the_guard_to_warn_at_80_and_stop_at_100(tmp_path):
    guard, events = _guard(tmp_path, Limits(tokens=10_000))
    watcher = budgetwatch.BudgetStreamWatcher(guard.channel, guard.signal)
    feed = watcher.tap()

    # 7,000 tokens: input 6,000 (4,000 of them cached) + output 1,000.
    feed(_jsonl(_turn(6_000, 4_000, 1_000)).encode())
    assert watcher.poll() is None
    # MUTATION: stop counting turn.completed and nothing is ever seen.
    assert guard.watch() is None and guard.tokens == 7_000 and events == []

    feed(_jsonl(_turn(1_000, 0, 500)).encode())
    watcher.poll()
    assert guard.watch() == "warn"
    (event,) = events
    assert event["cause"] == budget_mod.WARNING_CAUSE and event["percent"] == 85
    assert event["told"] is False, "codex exec takes no note mid-run"

    feed(_jsonl(_turn(1_000, 0, 500)).encode())
    watcher.poll()
    assert guard.watch() == budget_mod.ACTION_STOP and guard.tokens == 10_000
    assert guard.usage() == {budget_mod.UNNAMED_MODEL: {
        "input_tokens": 4_000, "output_tokens": 2_000,
        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 4_000}}


def test_a_completed_codex_tool_item_is_a_turn_boundary_and_a_message_is_not():
    meter = UsageMeter()
    started = {"type": "item.started", "item": _command("c", "ls")}
    done = {"type": "item.completed", "item": _command("c", "ls", exit_code=0, status="completed")}
    message = {"type": "item.completed", "item": {"id": "m", "type": "agent_message", "text": "x"}}
    assert meter.feed(started) is False
    assert meter.feed(message) is False
    # MUTATION: count only turn.completed and codex is never stopped before its end.
    assert meter.feed(done) is True
    for kind in ("file_change", "mcp_tool_call", "web_search"):
        assert meter.feed({"type": "item.completed", "item": {"id": kind, "type": kind}}) is True
    assert meter.feed({"type": "item.completed", "item": {"id": "r", "type": "reasoning"}}) is False
    assert meter.tokens() == 0


def test_a_codex_run_asked_to_stop_stops_at_its_next_tool_boundary(tmp_path, monkeypatch):
    """The budget spent by the time this start runs (an earlier start's usage,
    folded in by the guard): the runner stops codex once the next tool's
    result is in, before the model answers it, and the work so far is
    rendered."""
    ctx = _ctx(tmp_path)
    stream = [
        {"type": "thread.started", "thread_id": SESSION},
        {"type": "turn.started"},
        {"type": "item.started", "item": _command("item_0", "make build")},
        {"type": "item.completed", "item": _command("item_0", "make build", output="ok\n",
                                                    exit_code=0, status="completed")},
        {"type": "item.started", "item": _command("item_1", "make test")},
        {"type": "item.completed", "item": _command("item_1", "make test", exit_code=0,
                                                    status="completed")},
        {"type": "item.completed", "item": {"id": "item_2", "type": "agent_message", "text": "all done"}},
        {"type": "turn.completed", "usage": _usage(50_000, 0, 5_000)},
    ]
    _install(tmp_path, monkeypatch, ctx, stream, pause=0.3)
    guard, _events = _guard(tmp_path, Limits(tokens=1_000))
    for name, value in guard.env().items():
        monkeypatch.setenv(name, value)
    guard.signal.write_text(json.dumps({"action": budget_mod.ACTION_STOP}))

    with pytest.raises(RunnerFailure, match="stopped by its budget"):
        codex.body(ctx)
    guard.runner_ended()
    assert guard.exceeded and guard.stopped_for == budget_mod.STOPPED_AT_BOUNDARY
    marks = (ctx.work_dir / "marks").read_text().split()
    # Stopped after line 3 (the first completed command), long before the
    # second command or the answer.
    assert int(marks[-1]) <= 4, marks
    log = (ctx.artifacts_dir / "codex.stdout.log").read_text()
    assert "$ make build" in log and "all done" not in log
    assert not [line for line in log.splitlines() if line.lstrip().startswith("{")]


def test_a_replayed_codex_stream_is_warned_then_stopped_by_the_worker(tmp_path, monkeypatch):
    """The two processes end to end: the runner's watcher reports codex's
    turn usage, the worker's guard warns at 80% and asks the stop at 100%,
    and the runner honours it at the next boundary. The fake prints several
    turns in one start so one runner shows the whole sequence; a real `codex
    exec` start is one turn, whose usage arrives at its end."""
    ctx = _ctx(tmp_path)
    stream: list[dict[str, Any]] = [{"type": "thread.started", "thread_id": SESSION}]
    for turn in range(10):
        stream += [{"type": "turn.started"},
                   {"type": "item.completed", "item": _command(f"c{turn}", f"step {turn}",
                                                               exit_code=0, status="completed")},
                   {"type": "turn.completed", "usage": _usage(800, 200, 200)}]
    _install(tmp_path, monkeypatch, ctx, stream, pause=0.15)
    guard, events = _guard(tmp_path, Limits(tokens=5_000))
    for name, value in guard.env().items():
        monkeypatch.setenv(name, value)

    done = threading.Event()

    def loop() -> None:
        while not done.is_set():
            guard.watch()
            time.sleep(0.03)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    try:
        with pytest.raises(RunnerFailure, match="stopped by its budget"):
            codex.body(ctx)
    finally:
        done.set()
        thread.join(5)
    guard.runner_ended()

    (event,) = [e for e in events if e["cause"] == budget_mod.WARNING_CAUSE]
    assert event["used"]["tokens"] == 4_000 and event["told"] is False
    assert guard.exceeded and guard.stopped_for == budget_mod.STOPPED_AT_BOUNDARY
    assert 5_000 <= guard.tokens <= 6_000, guard.tokens
    printed = [int(m) for m in (ctx.work_dir / "marks").read_text().split()]
    # MUTATION: ignore the signal and all 31 lines are printed.
    assert printed[-1] < len(stream) - 3, printed
    assert guard.summary()["end_cause"] == "budget_exceeded"
    assert os.path.exists(ctx.artifacts_dir / codex_events.EVENTS_NAME)
