"""The per-agent budget, enforced in the worker from the runner's own stream (BUDGET-CAP, part of #2459).

BGD, 2026-10-26: at 80% the agent is told to wrap up and publish and a
`budget_warning` event is recorded; at 100% it is stopped at its next turn
boundary, checkpointed, its work published as any step publishes, and the
step ends FAILED `budget_exceeded` with the numbers; it is never stopped
mid-turn unless that turn passes 120%. With no budget nothing changes.

What these hold, on replayed usage streams (no network, no cloud):

  * tokens are input + output + cache reads, the last copy of each message;
  * the guard warns once at 80% (the note for the hook, the event) and asks
    for the stop at 100% and the kill at 120%;
  * the runner's watcher stops only at a turn boundary for `stop`, at once
    for `kill`, and never calls an exit the CLI made itself a budget stop;
  * end to end, a fake claude-code stream is warned at 80% and stopped at
    100% at a turn boundary, the turns after it never printed;
  * the lifecycle publishes the stopped attempt's work and ends it FAILED
    with `budget_exceeded`, and a task without a budget runs as before.
"""

from __future__ import annotations

import json
import os
import secrets as pysecrets
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from agent_worker import budget as budget_mod
from agent_worker.budget import BudgetGuard, Limits, UsageMeter
from agent_worker.errors import ExitCode
from agent_worker.runners import budgetwatch, claude_code
from agent_worker.runners.base import RunnerContext, RunnerFailure
from agent_worker.runners.cliagent import CliAgentSpec, run_cli_agent
from swarm_common.states import EventType, TaskState

from worker_seeds import seed_attempt


class _Log:
    def __init__(self) -> None:
        self.lines: list[tuple[str, dict[str, Any]]] = []

    def info(self, message: str, **fields: Any) -> None:
        self.lines.append((message, fields))

    warning = info


def _assistant(mid: str, *, inp: int = 0, out: int = 0, read: int = 0, write: int = 0,
               model: str = "claude-opus-5-5") -> dict[str, Any]:
    return {"type": "assistant", "message": {"id": mid, "model": model, "usage": {
        "input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": read,
        "cache_creation_input_tokens": write}}}


BOUNDARY = {"type": "user", "message": {"content": [{"type": "tool_result"}]}}


def _guard(tmp_path: Path, limits: Limits, **kwargs: Any) -> tuple[BudgetGuard, list[dict]]:
    events: list[dict[str, Any]] = []
    private = tmp_path / "private"
    private.mkdir(exist_ok=True)
    guard = BudgetGuard(limits, private=private, emit=events.append, log=_Log(), **kwargs)
    return guard, events


def _report(guard: BudgetGuard, meter: UsageMeter) -> None:
    """What the runner's watcher writes, written by hand."""
    guard.channel.write_text(json.dumps({"usage_by_model": meter.usage_by_model()}))
    os.utime(guard.channel, ns=(time.time_ns(), time.time_ns()))


# --------------------------------------------------------------------------
# What is counted
# --------------------------------------------------------------------------


def test_tokens_are_input_output_and_cache_reads_of_each_messages_last_copy():
    meter = UsageMeter()
    # The stream repeats a message per content block, its output growing.
    meter.feed(_assistant("m1", inp=100, out=5, read=1_000, write=50_000))
    meter.feed(_assistant("m1", inp=100, out=40, read=1_000, write=50_000))
    # A subagent's message costs money too and is counted.
    meter.feed({**_assistant("m2", inp=10, out=10), "parent_tool_use_id": "tool-1"})
    # MUTATION: count cache writes and this is 50,000 more; count every copy and 105 more.
    assert meter.tokens() == 100 + 40 + 1_000 + 10 + 10
    assert meter.usage_by_model()["claude-opus-5-5"]["output_tokens"] == 50


def test_a_turn_boundary_is_a_top_level_user_event_or_a_codex_turn():
    meter = UsageMeter()
    assert meter.feed(BOUNDARY) is True
    assert meter.feed({**BOUNDARY, "parent_tool_use_id": "sub"}) is False
    assert meter.feed(_assistant("m")) is False
    codex = {"type": "turn.completed",
             "usage": {"input_tokens": 1_000, "cached_input_tokens": 600, "output_tokens": 50}}
    assert meter.feed(codex) is True
    # codex counts cached input inside its input: 400 + 600 + 50.
    assert meter.tokens() == 1_050
    assert meter.turns == 2


def test_a_task_without_a_budget_block_has_no_budget():
    assert Limits.from_dispatch({"strategy": "collect"}) is None
    assert Limits.from_dispatch(None) is None
    assert Limits.from_dispatch({"budget": {"usd": 0, "tokens": "5"}}) is None
    assert Limits.from_dispatch({"budget": {"usd": 1, "from": {}}}) == Limits(usd=1.0)


# --------------------------------------------------------------------------
# The guard: 80%, 100%, 120%
# --------------------------------------------------------------------------


def test_the_guard_warns_once_at_80_and_asks_for_the_stop_at_100(tmp_path):
    guard, events = _guard(tmp_path, Limits(tokens=1_000), can_tell=True)
    meter = UsageMeter()
    meter.feed(_assistant("a", inp=700))
    _report(guard, meter)
    assert guard.watch() is None
    assert not guard.note.exists() and events == []

    meter.feed(_assistant("b", inp=100))
    _report(guard, meter)
    # MUTATION: warn at 100% instead and this is None.
    assert guard.watch() == "warn"
    (event,) = events
    assert event["cause"] == budget_mod.WARNING_CAUSE and event["told"] is True
    assert event["used"]["tokens"] == 800 and event["limit"] == {"tokens": 1_000}
    assert "Wrap up now" in json.loads(guard.note.read_text())["message"]
    assert not guard.signal.exists()
    assert guard.watch() is None, "warned once"

    meter.feed(_assistant("c", inp=200))
    _report(guard, meter)
    assert guard.watch() == budget_mod.ACTION_STOP
    assert json.loads(guard.signal.read_text()) == {"action": "stop"}
    assert guard.watch() is None, "asked once"
    assert len(events) == 1


def test_a_turn_past_120_percent_is_killed(tmp_path):
    guard, _ = _guard(tmp_path, Limits(tokens=1_000))
    meter = UsageMeter()
    meter.feed(_assistant("a", out=1_100))
    _report(guard, meter)
    assert guard.watch() == budget_mod.ACTION_STOP
    meter.feed(_assistant("a", out=1_250))
    _report(guard, meter)
    # MUTATION: never escalate past the stop and this is None.
    assert guard.watch() == budget_mod.ACTION_KILL
    assert json.loads(guard.signal.read_text()) == {"action": "kill"}


def test_dollars_are_priced_by_swarm_api_and_throttled(tmp_path):
    priced: list[dict] = []
    now = [100.0]

    def pricer(usage):
        priced.append(usage)
        return usage["claude-opus-5-5"]["output_tokens"] / 1_000

    guard, events = _guard(tmp_path, Limits(usd=1.0), pricer=pricer, clock=lambda: now[0])
    meter = UsageMeter()
    meter.feed(_assistant("a", out=500))
    _report(guard, meter)
    assert guard.watch() is None and guard.usd == 0.5
    meter.feed(_assistant("b", out=400))
    _report(guard, meter)
    # Inside the throttle: not asked again, so no warning yet.
    assert guard.watch() is None and len(priced) == 1
    now[0] += budget_mod.PRICE_INTERVAL_SECONDS
    assert guard.watch() == "warn" and guard.usd == 0.9
    assert events[0]["used"]["usd"] == 0.9
    # codex has no hook: the agent is not told, and the event says so.
    assert events[0]["told"] is False and not guard.note.exists()


def test_no_dollar_limit_asks_swarm_api_nothing(tmp_path):
    def pricer(usage):
        raise AssertionError("priced a budget that has no dollar limit")

    guard, _ = _guard(tmp_path, Limits(tokens=10), pricer=pricer)
    meter = UsageMeter()
    meter.feed(_assistant("a", out=5))
    _report(guard, meter)
    assert guard.watch() is None


# --------------------------------------------------------------------------
# The runner's watcher
# --------------------------------------------------------------------------


def _lines(*events: dict) -> bytes:
    return b"".join(json.dumps(e).encode() + b"\n" for e in events)


def test_the_watcher_stops_only_at_the_next_turn_boundary(tmp_path):
    channel, signal = tmp_path / "chan.json", tmp_path / "signal.json"
    watcher = budgetwatch.BudgetStreamWatcher(channel, signal)
    feed = watcher.tap()
    feed(_lines(_assistant("a", inp=10), BOUNDARY))
    assert watcher.poll() is None
    signal.write_text(json.dumps({"action": "stop"}))
    feed(_lines(_assistant("b", inp=10)))
    # MUTATION: stop on the signal alone and this is mid-turn.
    assert watcher.poll() is None, "not mid-turn"
    feed(_lines(BOUNDARY, _assistant("c", inp=99_999)))
    assert watcher.poll() == budget_mod.ACTION_STOP
    data = json.loads(channel.read_text())
    assert data["stopped_for"] == budget_mod.STOPPED_AT_BOUNDARY
    assert data["tokens"] == 20, "nothing after the boundary it stopped at is read"


def test_the_watcher_kills_at_once_and_never_after_the_cli_exited(tmp_path):
    channel, signal = tmp_path / "chan.json", tmp_path / "signal.json"
    watcher = budgetwatch.BudgetStreamWatcher(channel, signal)
    feed = watcher.tap()
    signal.write_text(json.dumps({"action": "kill"}))
    feed(_lines(BOUNDARY))
    assert watcher.poll(honour=False) is None
    assert json.loads(channel.read_text())["stopped_for"] is None
    feed(_lines(_assistant("a", out=5)))
    assert watcher.poll() == budget_mod.ACTION_KILL
    assert json.loads(channel.read_text())["stopped_for"] == budget_mod.STOPPED_MID_TURN


def test_without_the_workers_files_there_is_no_watcher(monkeypatch):
    monkeypatch.delenv(budget_mod.CHANNEL_ENV, raising=False)
    monkeypatch.delenv(budget_mod.SIGNAL_ENV, raising=False)
    assert budgetwatch.budget_watcher() is None


# --------------------------------------------------------------------------
# End to end in the runner: a replayed stream, warned, then stopped
# --------------------------------------------------------------------------

#: A fake claude-code: ten turns of 1,000 tokens each, a pause before each
#: turn boundary, and a marker for every turn it got to print.
FAKE_STREAM_CLI = r"""#!{python}
import json, os, sys, time
sys.stdin.read()
marks = os.path.join(os.environ["HOME"], "marks")
print(json.dumps({{"type": "system", "subtype": "init", "session_id": "sess-0123456789"}}), flush=True)
for turn in range(1, 11):
    with open(marks, "a") as f:
        f.write(f"{{turn}}\n")
    print(json.dumps({{"type": "assistant", "message": {{"id": f"m{{turn}}", "model": "claude-opus-5-5",
        "usage": {{"input_tokens": 600, "output_tokens": 400, "cache_read_input_tokens": 0}}}}}}), flush=True)
    time.sleep({pause})
    print(json.dumps({{"type": "user", "message": {{"content": [{{"type": "tool_result"}}]}}}}), flush=True)
print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "result": "all ten"}}), flush=True)
"""


def _fake_cli(tmp_path: Path, pause: float) -> Path:
    cli = tmp_path / "fake-claude"
    cli.write_text(FAKE_STREAM_CLI.format(python=sys.executable, pause=pause))
    cli.chmod(0o755)
    return cli


def _runner_ctx(tmp_path: Path) -> RunnerContext:
    work, artifacts = tmp_path / "work", tmp_path / "artifacts"
    work.mkdir()
    artifacts.mkdir()
    return RunnerContext(work_dir=work, artifacts_dir=artifacts, input_path=work / "input.json",
                         result_path=work / "result.json", quota_path=work / "quota.json",
                         payload={"prompt": "do the work"})


def _spec() -> CliAgentSpec:
    return CliAgentSpec(name="fake", provider="anthropic", binary_env="FAKE_BIN",
                        binary_default="fake", args_env="FAKE_ARGS", args_default=(),
                        key_env="FAKE_KEY", model_flag=None, resume_flag="--resume")


def _drive(guard: BudgetGuard, done: threading.Event) -> threading.Thread:
    """The lifecycle's supervision loop, as far as the budget goes."""
    def loop() -> None:
        while not done.is_set():
            guard.watch()
            time.sleep(0.05)
    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    return thread


def test_a_replayed_stream_is_warned_at_80_and_stopped_at_100_at_a_turn_boundary(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("FAKE_BIN", str(_fake_cli(tmp_path, pause=0.6)))
    monkeypatch.setenv("FAKE_KEY", "k-" + pysecrets.token_hex(8))
    guard, events = _guard(tmp_path, Limits(tokens=5_000), can_tell=True)
    for name, value in guard.env().items():
        monkeypatch.setenv(name, value)
    ctx = _runner_ctx(tmp_path)
    marks = ctx.work_dir / "marks"

    done = threading.Event()
    thread = _drive(guard, done)
    try:
        with pytest.raises(RunnerFailure) as caught:
            run_cli_agent(ctx, _spec())
    finally:
        done.set()
        thread.join(5)
    guard.runner_ended()

    assert "stopped by its budget" in str(caught.value)
    # Warned at 4,000 tokens, before the stop.
    (event,) = [e for e in events if e["cause"] == budget_mod.WARNING_CAUSE]
    assert event["used"]["tokens"] == 4_000 and event["told"] is True
    assert guard.note.exists(), "the 80% note is there for the hook"
    # Stopped at 5,000, at the turn boundary that ended turn 5 -- not mid-turn,
    # and long before turn 10.
    assert guard.exceeded and guard.stopped_for == budget_mod.STOPPED_AT_BOUNDARY
    assert guard.tokens == 5_000
    printed = marks.read_text().split()
    # MUTATION: ignore the signal and all ten turns are printed.
    assert printed[-1] in ("5", "6"), printed
    summary = guard.summary()
    assert summary["end_cause"] == "budget_exceeded" and summary["stopped"] == "turn_boundary"
    assert summary["used"] == {"tokens": 5_000} and summary["limit"] == {"tokens": 5_000}
    assert guard.error().startswith("budget_exceeded: ")


def test_with_no_budget_the_same_stream_runs_to_its_end(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_BIN", str(_fake_cli(tmp_path, pause=0.01)))
    monkeypatch.setenv("FAKE_KEY", "k-" + pysecrets.token_hex(8))
    for name in (budget_mod.CHANNEL_ENV, budget_mod.SIGNAL_ENV, budget_mod.NOTE_ENV):
        monkeypatch.delenv(name, raising=False)
    ctx = _runner_ctx(tmp_path)
    out = run_cli_agent(ctx, _spec())
    assert (ctx.work_dir / "marks").read_text().split()[-1] == "10"
    assert "all ten" in json.dumps(out)


# --------------------------------------------------------------------------
# claude-code: the hook that tells the note once
# --------------------------------------------------------------------------


def test_the_hook_tells_the_note_once(tmp_path):
    note = tmp_path / "budget-note.json"
    note.write_text(json.dumps({"message": "Wrap up now."}))
    hook = [sys.executable, "-I", str(claude_code.BUDGET_NOTE_HOOK), str(note)]
    first = subprocess.run(hook, input="{}", capture_output=True, text=True, check=True)
    told = json.loads(first.stdout)["hookSpecificOutput"]
    assert told == {"hookEventName": "PostToolUse", "additionalContext": "Wrap up now."}
    second = subprocess.run(hook, input="{}", capture_output=True, text=True, check=True)
    # MUTATION: read the note in place instead of renaming it, and it is told every call.
    assert second.stdout == ""


def test_the_settings_register_the_hook_only_with_a_budget(tmp_path, monkeypatch):
    monkeypatch.delenv(claude_code.HANDOFF_ENV, raising=False)
    monkeypatch.delenv(budget_mod.NOTE_ENV, raising=False)
    plain = json.loads(claude_code.write_headless_settings(tmp_path / "a").read_text())
    # With no budget the settings are what they were: no PostToolUse at all.
    assert "PostToolUse" not in plain["hooks"]
    monkeypatch.setenv(budget_mod.NOTE_ENV, str(tmp_path / "note.json"))
    budgeted = json.loads(claude_code.write_headless_settings(tmp_path / "b").read_text())
    (entry,) = budgeted["hooks"]["PostToolUse"]
    assert str(claude_code.BUDGET_NOTE_HOOK) in entry["hooks"][0]["command"]


# --------------------------------------------------------------------------
# The lifecycle: published, then FAILED budget_exceeded; or nothing at all
# --------------------------------------------------------------------------

PUBLISHED = {
    "base": "a" * 40, "head": "b" * 40, "commit_count": 1, "dirty_count": 0,
    "patch": None, "patch_bytes": 0, "patch_omitted": False,
    "published": False, "publish_reason": "deferred",
}


def _seed(db: Any, *, budget: dict[str, Any] | None) -> None:
    seed_attempt(db, task_input={"prompt": "do the work", "steps": 1, "sleep_seconds": 0.01})
    dispatch: dict[str, Any] = {"strategy": "direct-pr"}
    if budget is not None:
        dispatch["budget"] = budget
    db.doc("tasks/task_1")["metadata"] = {"dispatch": dispatch}


def _publishes(worker: Any, pushes: list[dict[str, Any]]) -> None:
    """The harvest defers its publish; the real `_publish_checked` makes it."""

    def harvest(**_kwargs: Any) -> dict[str, Any]:
        worker._deferred_publish = {"repo": worker.ws.work, "work_head": "b" * 40,
                                    "publish_repo": None}
        return dict(PUBLISHED)

    def publish_git(**kwargs: Any) -> dict[str, Any]:
        pushes.append(kwargs)
        return {"published": kwargs["publish"], "branch": "swarm/task_1",
                "publish_reason": "pushed"}

    worker._harvest_git = harvest  # type: ignore[method-assign]
    worker._publish_git = publish_git  # type: ignore[method-assign]


def test_a_budget_stop_is_published_then_failed_budget_exceeded(db, worker_factory):
    _seed(db, budget={"usd": 1.0, "from": {"step": {"usd": 1.0}}})
    worker, _, _ = worker_factory()
    pushes: list[dict[str, Any]] = []
    _publishes(worker, pushes)
    # The guard as the supervision left it: the runner stopped the agent at
    # a turn boundary at $1.02 of $1.00 (the mock runner has no stream).
    guard = BudgetGuard(Limits(usd=1.0), private=worker.cfg.workspace_root, emit=lambda d: None,
                        log=_Log())
    guard.usd, guard.warned, guard.stopped_for = 1.02, True, budget_mod.STOPPED_AT_BOUNDARY
    guard.runner_ended = lambda: None  # type: ignore[method-assign]
    worker.budget = guard

    assert worker.run() == ExitCode.FAILED

    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.FAILED.value, task.get("last_error")
    # MUTATION: drop the budget branch from `_record_finalised` and this is SUCCEEDED.
    assert task["last_error"].startswith("budget_exceeded: "), task["last_error"]
    assert "$1.02" in task["last_error"] and "$1.00" in task["last_error"]
    # No frozen cause yet (contract request 118): null, the contract's own rule.
    assert task.get("end_cause") is None
    numbers = task["result_summary"]["budget"]
    assert numbers["end_cause"] == "budget_exceeded" and numbers["stopped"] == "turn_boundary"
    assert numbers["used"]["usd"] == 1.02 and numbers["limit"] == {"usd": 1.0}
    # Published as any step publishes, not withheld.
    (push,) = pushes
    assert push["publish"] is True
    assert task["result_summary"]["git"]["published"] is True
    failed = [e for e in db.events("task_1") if e["type"] == EventType.FAILED.value]
    assert failed, "the end is recorded"


def test_with_no_budget_the_child_environment_and_the_end_are_unchanged(db, worker_factory):
    _seed(db, budget=None)
    worker, _, _ = worker_factory()
    assert worker.run() == ExitCode.OK
    task = db.doc("tasks/task_1")
    assert task["state"] == TaskState.SUCCEEDED.value, task.get("last_error")
    # MUTATION: build a guard for every task and the summary grows a block.
    assert "budget" not in task["result_summary"]
    assert worker.budget is None


def test_only_a_budgeted_cli_task_gets_the_budget_files(db, worker_factory):
    _seed(db, budget={"tokens": 15_000_000})
    worker, _, _ = worker_factory(runner_profile="claude-code")
    worker._task = db.doc("tasks/task_1")
    from agent_worker import workspace as workspace_mod

    worker.ws = workspace_mod.create(worker.cfg.workspace_root, worker.cfg.attempt_id)
    env = worker._budget_env()
    assert set(env) == {budget_mod.CHANNEL_ENV, budget_mod.SIGNAL_ENV, budget_mod.NOTE_ENV}
    assert all(str(worker.ws.private) in path for path in env.values()), "never in work/"
    assert worker.budget is not None and worker.budget.limits == Limits(tokens=15_000_000)

    mock, _, _ = worker_factory(runner_profile="mock", attempt_id="att_2")
    mock._task = db.doc("tasks/task_1")
    mock.ws = workspace_mod.create(mock.cfg.workspace_root, mock.cfg.attempt_id)
    # A profile with no CLI stream has nothing to enforce from.
    assert mock._budget_env() == {} and mock.budget is None
