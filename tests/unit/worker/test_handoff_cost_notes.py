"""A hand-off to a fresh session inside one attempt keeps its cost and its notes (HANDOFF-COST-NOTES, #2896, part of #2897).

Reported by the Product Oracle session, 2026-10-31 (wf_f27ece6a6d7c4171a9c2):

  * #2896: qa4-docs ran ONE 30-minute attempt across several sessions and
    read $0.20, its last 5-turn session alone. A session the context ceiling
    stops prints no `result` event, so only its tokens were carried, and the
    worker priced the stream only when NO session had reported a cost;
  * #2897: qa4-company kept its findings "in my context" and the session
    after the hand-off rebuilt them from screenshots: the hand-off carried the
    note file and nothing the session itself had said.

What these hold, on a fake claude-code (no network, no cloud): three sessions
of known costs sum to their total; the next session's prompt carries the
previous session's own last words, its todo list and its notes files, from
the attempt's own artifacts only; a single-session attempt is unchanged; and
no session is counted twice.
"""

from __future__ import annotations

import json
import secrets as pysecrets
import sys
from pathlib import Path
from typing import Any

import pytest

from agent_worker import lifecycle
from agent_worker.runners import ceiling
from agent_worker.runners.base import RunnerContext
from agent_worker.runners.cliagent import (
    RESUME_REASON_ENV,
    RESUME_SESSION_ENV,
    SESSION_FILE_ENV,
    CliAgentSpec,
    run_cli_agent,
)

MODEL = "claude-opus-5-5"

#: A fake claude-code. Each start appends its argv and stdin to
#: `$HOME/starts.jsonl` and plays the next entry of `$HOME/plan.json`: one
#: turn per `contexts` value (its `input_tokens`), the first turn's message
#: carrying `say` as text and `todos` as a TodoWrite call, a turn boundary
#: after every turn but the last (after `pause` seconds), and a `result`
#: event reporting `cost` when the start gets to its end.
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
sid = f"sess-{{n:04d}}-abcdef"
print(json.dumps({{"type": "system", "subtype": "init", "session_id": sid}}), flush=True)
contexts = step["contexts"]
for turn, context in enumerate(contexts, 1):
    content = [{{"type": "tool_use", "id": f"t{{n}}-{{turn}}", "name": "Bash",
                 "input": {{"command": "true"}}}}]
    if turn == 1 and step.get("say"):
        content.insert(0, {{"type": "text", "text": step["say"]}})
    if turn == 1 and step.get("todos"):
        content.append({{"type": "tool_use", "id": f"todo{{n}}", "name": "TodoWrite",
                         "input": {{"todos": step["todos"]}}}})
    if turn == len(contexts):
        content = [{{"type": "text", "text": "done"}}]
    print(json.dumps({{"type": "assistant", "session_id": sid, "message": {{
        "id": f"m{{n}}-{{turn}}", "model": "{model}", "content": content,
        "usage": {{"input_tokens": context, "cache_read_input_tokens": 0,
                  "cache_creation_input_tokens": 0, "output_tokens": 10}}}}}}), flush=True)
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

#: A session the ceiling stops: one turn of 210,000 tokens, then the boundary.
STOPPED = {"contexts": [210_000, 1_000, 1_000], "pause": 1.0, "cost": 9.99}
#: A session that runs to its end and reports $0.20 (qa4-docs's last session).
LAST = {"contexts": [1_000, 2_000, 3_000], "cost": 0.20}

#: swarm-api's price in this test: a dollar per million input tokens, so a
#: session's known cost is its input tokens / 1e6.
PER_INPUT_TOKEN = 1e-6


class TokenPricer:
    """swarm-api's `spend-estimate` at `PER_INPUT_TOKEN`, recording each ask."""

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


def _starts(ctx: RunnerContext) -> list[dict[str, Any]]:
    path = ctx.work_dir / "starts.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _worker_spend(worker_factory, output: dict[str, Any], *, times: int = 1):
    """The worker's spend after reading `output` as one runner's result.json,
    `times` times (the teardown backstop reads it again)."""
    from agent_worker import workspace as workspace_mod

    worker, config, _ = worker_factory()
    worker.ws = workspace_mod.create(config.workspace_root, config.attempt_id)
    pricer = TokenPricer()
    worker.spend_pricer = pricer
    worker._note_runner_spend_start(resumed=False)
    worker.ws.result_path.write_text(json.dumps({"output": output}))
    worker._spend_pending = True
    for _ in range(times):
        worker._collect_spend()
    return worker._spend, pricer


def test_three_sessions_of_known_costs_sum_to_their_total(ctx, worker_factory):
    _plan(ctx, STOPPED, STOPPED, LAST)
    out = run_cli_agent(ctx, _spec())

    assert len(_starts(ctx)) == 3
    assert out["context_ceiling"]["fresh_sessions"] == 2
    spend = out["structured_output"]
    # The two stopped sessions' counts ride out for the worker to price.
    unpriced = spend[ceiling.UNPRICED_KEY]
    assert list(unpriced) == [MODEL]
    assert unpriced[MODEL]["input_tokens"] == 2 * 210_000
    assert unpriced[MODEL]["prompt_buckets"] == [[210, 2 * 210_000, 20, 0, 0]]
    # Every session's wall time: the last reports 1,000 ms, the stopped two
    # are timed by the runner (each paused 1 s before its boundary).
    assert spend["duration_ms"] >= 1000 + 2 * 900

    total, pricer = _worker_spend(worker_factory, out)
    # $0.21 + $0.21 for the stopped sessions, $0.20 reported by the last.
    # MUTATION: drop the unpriced sessions and this reads $0.20, the defect.
    assert total["total_cost_usd"] == pytest.approx(0.21 + 0.21 + 0.20)
    assert total["cost_estimated"] is True
    assert pricer.asked == [unpriced]
    assert total["input_tokens"] == 2 * 210_000 + 6_000


def test_a_sessions_result_read_twice_is_counted_once(ctx, worker_factory):
    _plan(ctx, STOPPED, LAST)
    out = run_cli_agent(ctx, _spec())

    total, pricer = _worker_spend(worker_factory, out, times=2)
    assert total["total_cost_usd"] == pytest.approx(0.21 + 0.20)
    assert len(pricer.asked) == 1


def test_an_attempt_with_no_reported_cost_is_not_priced_twice(worker_factory):
    # Every session stopped: no `total_cost_usd` at all. The worker prices the
    # whole stream then (#627), which covers the stopped sessions already, so
    # their own counts are not priced on top.
    counts = {"input_tokens": 210_000, "output_tokens": 10, "cache_read_input_tokens": 0,
              "cache_creation_input_tokens": 0, "prompt_buckets": [[210, 210_000, 10, 0, 0]]}
    output = {"structured_output": {"usage": {"input_tokens": 210_000},
                                    ceiling.UNPRICED_KEY: {MODEL: counts}}}
    total, pricer = _worker_spend(worker_factory, output)
    assert {MODEL: counts} not in pricer.asked
    assert "total_cost_usd" not in total


def test_a_single_session_attempt_is_unchanged(ctx, worker_factory):
    _plan(ctx, LAST)
    out = run_cli_agent(ctx, _spec())

    (only,) = _starts(ctx)
    assert "CONTEXT CEILING HANDOFF" not in only["stdin"]
    spend = out["structured_output"]
    assert ceiling.UNPRICED_KEY not in spend
    assert spend["total_cost_usd"] == 0.20 and spend["duration_ms"] == 1000
    total, pricer = _worker_spend(worker_factory, out)
    assert total["total_cost_usd"] == pytest.approx(0.20)
    assert pricer.asked == [] and "cost_estimated" not in total


def test_the_handoff_carries_the_previous_sessions_notes(ctx, tmp_path):
    finding = "FINDING F-7: the pricing tab answers 500 on save " + pysecrets.token_hex(4)
    todos = [{"content": "walk the market page", "status": "completed"},
             {"content": "write findings.md", "status": "pending"}]
    (ctx.artifacts_dir / "findings.md").write_text("# Findings\n")
    (ctx.artifacts_dir / "shots").mkdir()
    (ctx.artifacts_dir / "shots" / "measure.json").write_text("{}")
    (ctx.artifacts_dir / "shots" / "page.png").write_bytes(b"\x89PNG")
    # A file outside the attempt's artifacts, reachable only through a link:
    # never named (tenant isolation).
    elsewhere = tmp_path / "another-attempt"
    elsewhere.mkdir()
    (elsewhere / "secret-notes.md").write_text("not this attempt's")
    (ctx.artifacts_dir / "linked.md").symlink_to(elsewhere / "secret-notes.md")
    (ctx.artifacts_dir / "linked-dir").symlink_to(elsewhere)
    _plan(ctx, {**STOPPED, "say": finding, "todos": todos}, LAST)
    run_cli_agent(ctx, _spec())

    _, second = _starts(ctx)
    prompt = second["stdin"]
    assert prompt.startswith("CONTEXT CEILING HANDOFF")
    # MUTATION: leave the session's last words out and the finding is lost.
    assert finding in prompt
    assert "- [completed] walk the market page" in prompt
    assert "- [pending] write findings.md" in prompt
    assert "findings.md (11 bytes)" in prompt
    assert "shots/measure.json (2 bytes)" in prompt
    assert "page.png" not in prompt
    assert "secret-notes" not in prompt and "linked" not in prompt


def test_the_agent_is_told_to_keep_its_notes_in_files():
    assert "never only in your context" in ceiling.HANDOFF_NOTE


def test_the_session_tail_is_capped_and_keeps_the_newest_words():
    watcher = ceiling.CeilingWatcher(ceiling.CeilingLimits())
    watcher.begin(fresh=True)
    feed = watcher.tap()
    for k in range(40):
        event = {"type": "assistant", "message": {"id": f"m{k}", "content": [
            {"type": "text", "text": f"note {k} " + "x" * 1000}]}}
        feed((json.dumps(event) + "\n").encode())
    watcher.poll()
    tail = watcher.session_tail()
    assert tail is not None and len(tail) <= ceiling.SESSION_TAIL_MAX_CHARS + 40
    assert "note 39 " in tail and "note 0 " not in tail
    watcher.begin(fresh=True)
    assert watcher.session_tail() is None


def test_the_prompt_buckets_width_is_the_workers():
    assert ceiling.PROMPT_BUCKET_TOKENS == lifecycle._PROMPT_BUCKET_TOKENS
    assert ceiling.UNNAMED_MODEL == lifecycle._UNNAMED_MODEL
