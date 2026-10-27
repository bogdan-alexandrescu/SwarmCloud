"""An attempt's runner summary counts every agent run, and what a swap costs (SWAP-RUNS, #2457).

On 2026-10-26 two 7,200 s timeouts read "agent ran 4,600 s": an account swap
at minute 42 restarted the agent CLI and `duration_seconds` was the second
run's alone. These run the real worker and the real claude-code runner
against a fake CLI and a fake broker (as test_account_hot_swap.py does) and
pin:

  * a two-run attempt reports the summed duration, both runs, the swap, and
    the restarted run's re-read cost -- the seconds and tokens it spent
    before its first action;
  * a one-run attempt's summary carries none of the new keys and its
    duration is the run's, exactly as before;
  * which tool calls are reads (`cliagent.is_action`), and that the measure
    is carried across the runner's own restarts of the CLI.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from agent_worker.errors import ExitCode
from agent_worker.runners.cliagent import AccountStreamWatcher, is_action

from test_account_hot_swap import FIRST, SECOND, TENANT, FakeBroker, _Secrets, _token
from worker_seeds import seed_attempt, seed_tenant

#: The usage of the restarted run's two reading messages, and of its edit.
READ_ONE = {"input_tokens": 120, "output_tokens": 30,
            "cache_creation_input_tokens": 400, "cache_read_input_tokens": 9000}
READ_TWO = {"input_tokens": 80, "output_tokens": 25,
            "cache_creation_input_tokens": 100, "cache_read_input_tokens": 9500}
EDIT = {"input_tokens": 10, "output_tokens": 300,
        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 9600}

#: The fake CLI. `exhaust` works for a second, is refused by the provider and
#: is stopped at its turn boundary; `reread` reads twice, waits, then edits
#: and succeeds; `finish` succeeds at once.
FAKE_CLI = r'''#!/usr/bin/env python3
import json, os, pathlib, sys, time
argv = sys.argv[1:]
resume = argv[argv.index("--resume") + 1] if "--resume" in argv else None
plan_dir = pathlib.Path(PLAN_DIR)
plan = json.loads((plan_dir / "plan.json").read_text())
with (plan_dir / "runs.jsonl").open("a") as f:
    f.write(json.dumps({"resume": resume}) + "\n")
runs = sum(1 for _ in (plan_dir / "runs.jsonl").open())

def say(event):
    print(json.dumps(event), flush=True)

session = plan["sessions"][min(runs - 1, len(plan["sessions"]) - 1)]
say({"type": "system", "subtype": "init", "session_id": session})
mode = plan["modes"][min(runs - 1, len(plan["modes"]) - 1)]
reset = int(time.time()) + 3600

def turn(n, tool, usage, tool_input):
    say({"type": "assistant", "session_id": session, "message": {
        "id": "msg_%s_%d" % (mode, n), "model": "claude-test", "usage": usage,
        "content": [{"type": "tool_use", "id": "t%d" % n, "name": tool, "input": tool_input}]}})
    say({"type": "user", "session_id": session, "parent_tool_use_id": None,
         "message": {"content": [{"type": "tool_result", "tool_use_id": "t%d" % n, "content": "ok"}]}})

if mode == "exhaust":
    time.sleep(1.0)
    say({"type": "assistant", "session_id": session, "message": {"id": "msg_x", "content": [
        {"type": "tool_use", "id": "t0", "name": "Edit", "input": {}}]}})
    say({"type": "rate_limit_event", "session_id": session, "rate_limit_info": {
        "status": "rejected", "rateLimitType": "five_hour", "resetsAt": reset}})
    say({"type": "user", "session_id": session, "parent_tool_use_id": None,
         "message": {"content": [{"type": "tool_result", "tool_use_id": "t0", "content": "ok"}]}})
    time.sleep(30)
    sys.exit(0)
if mode == "reread":
    turn(1, "Read", plan["usage"][0], {"file_path": "a.py"})
    turn(2, "Bash", plan["usage"][1], {"command": "grep -n x a.py | head -5"})
    time.sleep(0.5)
    turn(3, "Edit", plan["usage"][2], {"file_path": "a.py"})
say({"type": "result", "subtype": "success", "is_error": False, "result": "done",
     "session_id": session})
sys.exit(0)
'''


@pytest.fixture
def cli(tmp_path, monkeypatch) -> Path:
    binary = tmp_path / "fake-claude"
    plan = tmp_path / "plan"
    plan.mkdir()
    binary.write_text(FAKE_CLI.replace("PLAN_DIR", repr(str(plan)), 1))
    binary.chmod(0o755)
    monkeypatch.setenv("CLAUDE_CODE_BIN", str(binary))
    monkeypatch.delenv("CLAUDE_CODE_ARGS", raising=False)
    return binary


def _run(db, worker_factory, tmp_path, broker, *, modes):
    tokens = {f"swarm-account-{TENANT}--{label}": _token()
              for label in ("first", "second", "third")}
    sessions = [str(uuid.uuid4()) for _ in modes]
    seed_attempt(db, runner_profile="claude-code", task_input={"prompt": "do the work"})
    seed_tenant(db, credentials=["anthropic"])
    worker, _config, _ = worker_factory(
        runner_profile="claude-code", secret_client=_Secrets(tokens), timeout_seconds=60,
    )
    worker._account_broker = broker
    (tmp_path / "plan" / "plan.json").write_text(json.dumps(
        {"modes": modes, "sessions": sessions, "usage": [READ_ONE, READ_TWO, EDIT]}))
    code = worker.run()
    return code, db.doc("tasks/task_1").get("result_summary") or {}


def _sum(*usages: dict[str, int]) -> dict[str, int]:
    return {key: sum(u[key] for u in usages) for key in READ_ONE}


def test_a_two_run_attempt_reports_the_summed_duration_both_runs_and_the_swap(
    db, worker_factory, tmp_path, cli, log_stream
):
    code, summary = _run(db, worker_factory, tmp_path, FakeBroker(), modes=["exhaust", "reread"])

    assert code == ExitCode.OK, log_stream.getvalue()[-3000:]
    runs = summary["runs"]
    assert [r["run"] for r in runs] == [1, 2]
    assert [r["account"] for r in runs] == [FIRST, SECOND]
    assert [r["after_swap"] for r in runs] == [False, True]
    assert [r["resumed"] for r in runs] == [False, True]
    assert all(r["started_at"] <= r["ended_at"] for r in runs)
    assert runs[1]["exit_code"] == 0
    # The first run worked a second before the swap; the duration counts it.
    assert runs[0]["duration_s"] >= 1.0
    assert summary["duration_seconds"] == pytest.approx(
        runs[0]["duration_s"] + runs[1]["duration_s"], abs=0.002)
    assert summary["last_run_duration_seconds"] == runs[1]["duration_s"]
    assert summary["duration_seconds"] >= summary["last_run_duration_seconds"] + 1.0

    [swap] = summary["swaps"]
    assert {k: swap[k] for k in ("from", "to", "reason", "after_run")} == {
        "from": FIRST, "to": SECOND, "reason": "exhausted", "after_run": 1}
    assert runs[0]["ended_at"] <= swap["at"] <= runs[1]["started_at"]

    # THE RE-READ COST: the two reads' tokens, not the edit's, and the half
    # second the restarted agent waited before it acted.
    reread = runs[1]["reread"]
    assert reread["reached"] is True
    assert reread["tool"] == "Edit"
    assert reread["tokens"] == _sum(READ_ONE, READ_TWO)
    assert reread["seconds"] >= 0.5
    assert "reread" not in runs[0], "the first run followed no swap"
    assert summary["swap_cost"] == {
        "reread_seconds": reread["seconds"],
        "reread_tokens": _sum(READ_ONE, READ_TWO),
        "measured_runs": 1,
        "reached_action": 1,
    }
    # Each run's tokens are its own: the second run's are the three messages.
    assert runs[1]["tokens"] == _sum(READ_ONE, READ_TWO, EDIT)


def test_a_one_run_attempt_reports_exactly_what_it_did_before(
    db, worker_factory, tmp_path, cli, log_stream
):
    broker = FakeBroker()
    code, summary = _run(db, worker_factory, tmp_path, broker, modes=["finish"])

    assert code == ExitCode.OK, log_stream.getvalue()[-3000:]
    assert broker.swaps == []
    for key in ("runs", "swaps", "swap_cost", "last_run_duration_seconds"):
        assert key not in summary, key
    assert isinstance(summary["duration_seconds"], float)
    assert summary["duration_seconds"] < 30


@pytest.mark.parametrize(
    ("block", "acts"),
    [
        ({"name": "Read", "input": {"file_path": "a"}}, False),
        ({"name": "Grep", "input": {}}, False),
        ({"name": "Task", "input": {}}, False),
        ({"name": "Bash", "input": {"command": "grep -n x a.py | head -5"}}, False),
        ({"name": "Bash", "input": {"command": "cd repo && git log --oneline -3; git diff"}}, False),
        ({"name": "Bash", "input": {"command": "sed -n 1,40p a.py"}}, False),
        ({"name": "Bash", "input": {"command": "sed -i s/a/b/ a.py"}}, True),
        ({"name": "Bash", "input": {"command": "echo x > a.py"}}, True),
        ({"name": "Bash", "input": {"command": "git commit -m x"}}, True),
        ({"name": "Bash", "input": {"command": "uv run pytest tests -q"}}, True),
        ({"name": "Bash", "input": {}}, True),
        ({"name": "Edit", "input": {}}, True),
        ({"name": "Write", "input": {}}, True),
        ({"name": "mcp__swarm-git__issue_view", "input": {}}, True),
        ({"input": {}}, False),
    ],
)
def test_which_tool_calls_are_the_agent_acting(block, acts):
    assert is_action({"type": "tool_use", **block}) is acts


def test_the_measure_carries_across_the_runners_own_restarts(tmp_path):
    stdout = tmp_path / "stdout.log"
    channel = tmp_path / "channel.json"

    def assistant(n, tool, usage):
        return json.dumps({"type": "assistant", "message": {
            "id": f"m{n}", "usage": usage,
            "content": [{"type": "tool_use", "id": f"t{n}", "name": tool, "input": {}}]}})

    stdout.write_text(assistant(1, "Read", READ_ONE) + "\n")
    first = AccountStreamWatcher(stdout, channel, None)
    first.poll()
    first.write()
    assert first.first_action is None
    assert json.loads(channel.read_text())["spent"]["tokens"] == READ_ONE

    # The next start of the CLI (a finish pass) gets a new watcher and a new
    # capture; what the first saw is still counted before the action.
    stdout.write_text(assistant(2, "Edit", EDIT) + "\n")
    second = AccountStreamWatcher(stdout, channel, None, carry=first)
    second.poll()
    second.write()
    written = json.loads(channel.read_text())["first_action"]
    assert written["tool"] == "Edit"
    assert written["tokens"] == READ_ONE
