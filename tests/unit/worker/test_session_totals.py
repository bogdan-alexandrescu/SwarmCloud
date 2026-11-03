"""An attempt's cost, tokens and tool calls cover every session it ran (COST-ACROSS-SESSIONS, part of #2688).

Measured in experiment batches one and two: 17 steps hit the 200,000-token
context ceiling and restarted as a fresh session at 95 to 108 calls, and their
recorded cost left the stopped session out -- $17.04 recorded against about
$85 read off the streams on 8 steps whose task said the cost was complete; in
7 of 10 finished batch-two build steps the first session was missing. The
tool-call counts were per runner process too.

What these hold, on a fake claude-code (no network, no cloud): an attempt
with a ceiling restart reports the sum of both sessions and says it ran two;
a session whose usage was not read marks the cost incomplete rather than
reporting the others' figure as the whole; and a runner restarted inside the
attempt reports the tool calls of the runner before it as well as its own.
"""

from __future__ import annotations

import json
import secrets as pysecrets
import sys
from typing import Any

import pytest

from agent_worker.runners import ceiling, claude_code
from agent_worker.runners.base import RunnerContext
from agent_worker.runners.cliagent import (
    RESUME_REASON_ENV,
    RESUME_SESSION_ENV,
    SESSION_FILE_ENV,
    CliAgentSpec,
    run_cli_agent,
)

MODEL = "claude-opus-5-5"

#: A fake claude-code: each start plays the next entry of `$HOME/plan.json`,
#: one turn per `contexts` value (its `input_tokens`, every message without
#: usage when `usage` is false), a turn boundary after every turn but the
#: last (after `pause` seconds), and a `result` event reporting `cost`.
FAKE_CLI = r"""#!{python}
import json, os, sys, time
home = os.environ["HOME"]
sys.stdin.read()
starts = os.path.join(home, "starts.jsonl")
n = sum(1 for _ in open(starts)) if os.path.exists(starts) else 0
with open(starts, "a") as f:
    f.write(json.dumps({{"argv": sys.argv[1:]}}) + "\n")
plan = json.load(open(os.path.join(home, "plan.json")))
step = plan[min(n, len(plan) - 1)]
sid = f"sess-{{n:04d}}-abcdef"
print(json.dumps({{"type": "system", "subtype": "init", "session_id": sid}}), flush=True)
contexts = step["contexts"]
for turn, context in enumerate(contexts, 1):
    content = [{{"type": "tool_use", "id": f"t{{n}}-{{turn}}", "name": "Bash",
                 "input": {{"command": "true"}}}}]
    if turn == len(contexts):
        content = [{{"type": "text", "text": "done"}}]
    message = {{"id": f"m{{n}}-{{turn}}", "model": "{model}", "content": content}}
    if step.get("usage", True):
        message["usage"] = {{"input_tokens": context, "cache_read_input_tokens": 0,
                             "cache_creation_input_tokens": 0, "output_tokens": 10}}
    print(json.dumps({{"type": "assistant", "session_id": sid, "message": message}}), flush=True)
    if turn == len(contexts):
        break
    time.sleep(step.get("pause", 0.0))
    print(json.dumps({{"type": "user", "session_id": sid,
                      "message": {{"content": [{{"type": "tool_result"}}]}}}}), flush=True)
print(json.dumps({{"type": "result", "subtype": "success", "is_error": False,
                  "session_id": sid, "result": "done", "num_turns": len(contexts),
                  "duration_ms": 1000, "total_cost_usd": step["cost"],
                  "usage": {{"input_tokens": sum(contexts), "output_tokens": 10 * len(contexts)}}}}),
      flush=True)
"""

#: A session the context ceiling stops: one turn of 210,000 tokens, then the boundary.
STOPPED = {"contexts": [210_000, 1_000, 1_000], "pause": 1.0, "cost": 9.99}
#: A session the tool-call ceiling (lowered to 1 below) stops, whose stream
#: shows no usage at all: its cost is unknown.
UNREAD = {"contexts": [1_000, 1_000, 1_000, 1_000], "pause": 1.0, "usage": False, "cost": 9.99}
#: A session that runs to its end and reports $0.20.
LAST = {"contexts": [1_000, 2_000, 3_000], "cost": 0.20}

PER_INPUT_TOKEN = 1e-6


class TokenPricer:
    """swarm-api's `spend-estimate` at a dollar per million input tokens."""

    def __init__(self) -> None:
        self.asked: list[dict[str, Any]] = []

    def estimate(self, usage_by_model: dict[str, Any]) -> tuple[float | None, bool]:
        self.asked.append(usage_by_model)
        return sum(c["input_tokens"] for c in usage_by_model.values()) * PER_INPUT_TOKEN, False


def _spec() -> CliAgentSpec:
    return CliAgentSpec(name="fake", provider="anthropic", binary_env="FAKE_BIN",
                        binary_default="fake", args_env="FAKE_ARGS", args_default=(),
                        key_env="FAKE_KEY", model_flag=None, resume_flag="--resume",
                        finish_on_pending=True, context_ceiling=True)


@pytest.fixture
def ctx(tmp_path, monkeypatch) -> RunnerContext:
    cli = tmp_path / "fake-claude"
    cli.write_text(FAKE_CLI.format(python=sys.executable, model=MODEL))
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


def _worker_spend(worker_factory, output: dict[str, Any]) -> dict[str, Any]:
    """The worker's spend after reading `output` as one runner's result.json."""
    from agent_worker import workspace as workspace_mod

    worker, config, _ = worker_factory()
    worker.ws = workspace_mod.create(config.workspace_root, config.attempt_id)
    worker.spend_pricer = TokenPricer()
    worker._note_runner_spend_start(resumed=False)
    worker.ws.result_path.write_text(json.dumps({"output": output}))
    worker._spend_pending = True
    worker._collect_spend()
    return worker._spend


def test_a_ceiling_restart_reports_the_sum_of_both_sessions(ctx, worker_factory):
    _plan(ctx, STOPPED, LAST)
    out = run_cli_agent(ctx, _spec())

    # MUTATION: count only the session that printed a result and this reads 1.
    assert out["metrics"][ceiling.SESSIONS_METRIC] == {
        "count": 2, "usage_unread": [], "cost_complete": True}
    assert out["context_ceiling"]["count"] == 2
    spend = out["structured_output"]
    assert ceiling.COST_INCOMPLETE_KEY not in spend
    total = _worker_spend(worker_factory, out)
    # $0.21 for the stopped session, priced from its stream, $0.20 reported.
    assert total["total_cost_usd"] == pytest.approx(0.21 + 0.20)
    assert total["input_tokens"] == 210_000 + 6_000


def test_a_session_whose_usage_is_missing_marks_the_cost_incomplete(ctx, worker_factory):
    ctx.payload[ceiling.PAYLOAD_KEY] = {"tool_calls": 1}
    _plan(ctx, UNREAD, LAST)
    out = run_cli_agent(ctx, _spec())

    assert out["context_ceiling"]["fresh_sessions"] == 1
    assert out["metrics"][ceiling.SESSIONS_METRIC] == {
        "count": 2, "usage_unread": [1], "cost_complete": False}
    spend = out["structured_output"]
    # MUTATION: keep the last session's $0.20 and the attempt reads as if the
    # first session were free -- the measured defect.
    assert "total_cost_usd" not in spend
    assert spend[ceiling.COST_INCOMPLETE_KEY] is True
    total = _worker_spend(worker_factory, out)
    assert total.get("total_cost_usd") != pytest.approx(0.20) or total.get("cost_estimated") is True


def _summary_after(worker_factory, result: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """The worker's spend and the summary's top level after reading `result`
    as one runner's whole result.json (output and metrics)."""
    from agent_worker import workspace as workspace_mod

    worker, config, _ = worker_factory()
    worker.ws = workspace_mod.create(config.workspace_root, config.attempt_id)
    worker.spend_pricer = TokenPricer()
    worker._note_runner_spend_start(resumed=False)
    worker.ws.result_path.write_text(json.dumps(result))
    worker._spend_pending = True
    worker._collect_spend()
    summary: dict[str, Any] = {}
    worker._add_runner_block(summary)
    return worker._spend, summary


def test_the_worker_says_the_attempts_cost_is_incomplete(ctx, worker_factory):
    ctx.payload[ceiling.PAYLOAD_KEY] = {"tool_calls": 1}
    _plan(ctx, UNREAD, LAST)
    out = run_cli_agent(ctx, _spec())

    spend, summary = _summary_after(worker_factory, {"status": "succeeded", "output": out})
    # MUTATION: leave `cost_incomplete` to the runner's output and the
    # attempt's summary says nothing of the unread session.
    assert spend["cost_incomplete"] is True
    assert summary["cost_incomplete"] is True


def test_the_sessions_metric_alone_marks_the_cost_incomplete(worker_factory):
    # A runner whose spend lost the flag still said so in its metrics.
    result = {"status": "succeeded",
              "output": {"structured_output": {"total_cost_usd": 0.2}},
              "metrics": {ceiling.SESSIONS_METRIC: {
                  "count": 2, "usage_unread": [1], "cost_complete": False}}}
    spend, summary = _summary_after(worker_factory, result)
    assert spend["cost_incomplete"] is True
    assert summary["cost_incomplete"] is True


def test_a_complete_cost_is_not_marked_incomplete(ctx, worker_factory):
    _plan(ctx, STOPPED, LAST)
    out = run_cli_agent(ctx, _spec())

    spend, summary = _summary_after(worker_factory, {"status": "succeeded", "output": out})
    assert "cost_incomplete" not in spend
    assert "cost_incomplete" not in summary


def test_a_single_session_attempt_says_it_ran_one(ctx):
    _plan(ctx, LAST)
    out = run_cli_agent(ctx, _spec())

    assert out["metrics"][ceiling.SESSIONS_METRIC] == {
        "count": 1, "usage_unread": [], "cost_complete": True}
    assert out["structured_output"]["total_cost_usd"] == 0.20


def test_a_restarted_runner_carries_the_session_count(ctx, tmp_path, monkeypatch):
    # The worker's session file names the attempt's state directory: the
    # second runner process of the attempt (a swap) reads what the first left.
    monkeypatch.setenv(SESSION_FILE_ENV, str(tmp_path / "session.json"))
    _plan(ctx, STOPPED, LAST, LAST)
    run_cli_agent(ctx, _spec())
    state = ceiling.read_state(tmp_path / ceiling.STATE_NAME)
    assert state is not None and state["sessions"] == 2

    out = run_cli_agent(ctx, _spec())
    # MUTATION: start the count from zero in each runner and this reads 1.
    assert out["metrics"][ceiling.SESSIONS_METRIC]["count"] == 3


def test_a_restarted_runner_reports_the_earlier_runners_tool_calls(tmp_path):
    state = tmp_path / ceiling.STATE_NAME
    first, second = tmp_path / "first.log", tmp_path / "second.log"

    def stream(path, names):
        path.write_text("".join(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": f"t{k}", "name": name}]}}) + "\n"
            for k, name in enumerate(names)))

    stream(first, ["Bash", "Read", "mcp__playwright__browser_click"])
    stream(second, ["Bash"])
    ceiling.write_state(state, {"sessions": 2, "fresh_sessions": 1})
    one = claude_code.attempt_tool_use(first, state)
    assert one["families"]["built_in"] == 2
    two = claude_code.attempt_tool_use(second, state)
    # MUTATION: report the last runner's capture alone and this reads 1.
    assert two["families"]["built_in"] == 3
    assert two["families"]["playwright"] == 1
    # The rest of the state is kept for the ceiling.
    assert ceiling.read_state(state)["sessions"] == 2


def test_an_unread_capture_carries_the_earlier_counts_unchanged(tmp_path):
    state = tmp_path / ceiling.STATE_NAME
    ceiling.write_state(state, {ceiling.EARLIER_TOOL_CALLS_KEY: {"built_in": 4}})
    out = claude_code.attempt_tool_use(tmp_path / "missing.log", state)
    assert out["read"] is False and out["families"] is None
    assert ceiling.read_state(state)[ceiling.EARLIER_TOOL_CALLS_KEY] == {"built_in": 4}


def test_the_watcher_keeps_the_carried_counts_in_its_state():
    watcher = ceiling.CeilingWatcher(ceiling.CeilingLimits())
    watcher.carry({"sessions": 3, "unread_sessions": [2],
                   ceiling.EARLIER_TOOL_CALLS_KEY: {"built_in": 5}})
    watcher.begin(fresh=True)
    state = watcher.state()
    assert state["sessions"] == 4
    assert state["unread_sessions"] == [2]
    assert state[ceiling.EARLIER_TOOL_CALLS_KEY] == {"built_in": 5}
    assert watcher.sessions_record()["cost_complete"] is False
