"""A claude-code step never ends on "the suite is still running, I'll report".

Owner decision 2026-10-05 (lane review W1+G5). In 3 of 17 implement steps the
agent backgrounded its test run and ended its turn announcing a result it
would never deliver; in `--print` mode the session then ends and the worker
auto-commits an unverified tree. Two layers stop that:

  * the runner starts the CLI with background tasks switched off -- the
    CLI's own `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS`, and a PreToolUse hook in
    a generated `--settings` file that refuses any `run_in_background` call;
  * after the run, an answer that announces pending work (or a background
    shell left open) is resumed ONCE with `--resume <session_id>` and a
    finish prompt, within the step's remaining budget, and the result says
    `resumed_to_finish: true`.

These run the real claude-code runner against a fake CLI: a Python script
that prints a stream-json conversation and records how it was started.
"""

from __future__ import annotations

import json
import secrets as pysecrets
import subprocess
import time
from pathlib import Path

import pytest

from agent_worker.runners import claude_code, cliagent
from agent_worker.runners.base import RunnerContext, RunnerFailure
from agent_worker.runners.cliagent import (
    FINISH_PROMPT,
    announces_pending_work,
    open_background_shells,
)

SESSION = "sess-" + "0f1e2d3c4b5a6978"


def _key() -> str:
    # Built at runtime: no credential-shaped literal in this file.
    return "sk-ant-" + "api03-" + pysecrets.token_hex(16)


def _ctx(tmp_path: Path, payload: dict) -> RunnerContext:
    work = tmp_path / "work"
    artifacts = tmp_path / "artifacts"
    work.mkdir(parents=True, exist_ok=True)
    artifacts.mkdir(parents=True, exist_ok=True)
    return RunnerContext(
        work_dir=work,
        artifacts_dir=artifacts,
        input_path=work / "input.json",
        result_path=work / "result.json",
        quota_path=work / "quota.json",
        payload=payload,
    )


#: The fake CLI. Its plan is `$HOME/plan.json` (HOME is the work directory);
#: each start appends how it was started to `$HOME/runs.jsonl`.
FAKE_CLI = r'''#!/usr/bin/env python3
import json, os, pathlib, sys, time
argv = sys.argv[1:]
home = pathlib.Path(os.environ["HOME"])
plan = json.loads((home / "plan.json").read_text())
resume = argv[argv.index("--resume") + 1] if "--resume" in argv else None
settings = argv[argv.index("--settings") + 1] if "--settings" in argv else None
with (home / "runs.jsonl").open("a") as f:
    f.write(json.dumps({
        "resume": resume,
        "prompt": sys.stdin.read(),
        "settings": settings,
        "no_background": os.environ.get("CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"),
    }) + "\n")
runs = sum(1 for _ in (home / "runs.jsonl").open())
step = plan["runs"][min(runs - 1, len(plan["runs"]) - 1)]

def say(event):
    print(json.dumps(event), flush=True)

say({"type": "system", "subtype": "init", "session_id": plan["session"]})
for event in step.get("events", []):
    say(event)
time.sleep(step.get("sleep", 0))
say({"type": "result", "subtype": "success", "is_error": False,
     "session_id": plan["session"], "result": step["answer"],
     "total_cost_usd": step.get("cost", 0.5), "num_turns": step.get("turns", 3)})
'''


def _install(tmp_path: Path, monkeypatch, ctx: RunnerContext, runs: list[dict]) -> None:
    cli = tmp_path / "fake-claude"
    cli.write_text(FAKE_CLI)
    cli.chmod(0o755)
    (ctx.work_dir / "plan.json").write_text(json.dumps({"session": SESSION, "runs": runs}))
    monkeypatch.setenv("CLAUDE_CODE_BIN", str(cli))
    monkeypatch.setenv("ANTHROPIC_API_KEY", _key())
    for name in ("CLAUDE_CODE_ARGS", "CLAUDE_CODE_OAUTH_TOKEN", "MODEL",
                 cliagent.ACCOUNT_STREAM_ENV, cliagent.RESUME_SESSION_ENV):
        monkeypatch.delenv(name, raising=False)


def _runs(ctx: RunnerContext) -> list[dict]:
    path = ctx.work_dir / "runs.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


PENDING = "Pushed the fix. The test suite is still running; I'll report the result once it finishes."
DONE = "All done: 214 passed, 0 failed. Committed as abc1234."


# ---------------------------------------------------------------------------
# The phrase detector
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "answer",
    [
        PENDING,
        "The suite is still running, I'll report.",
        "Tests are still running in the background.",
        "I'm waiting on the integration run to complete.",
        "Still waiting on CI.",
        "I'll report back once it finishes.",
        "Started `uv run pytest` -- I will post the result once it finishes.",
        "Build kicked off. I'll report the outcome when it completes.",
    ],
)
def test_an_answer_announcing_pending_work_is_detected(answer):
    assert announces_pending_work(answer) is not None


@pytest.mark.parametrize(
    "answer",
    [
        DONE,
        # The words, mentioned in a finished report.
        'Done. The detector now recognises "still running", "I\'ll report", '
        '"waiting on" and "once it finishes"; 18 tests pass.',
        "Fixed the banner that said `still running` after the job exited. All tests pass.",
        "Finished. The suite was still running at 14:02 when I first checked; it "
        "has since completed: 312 passed. Nothing is waiting on review.",
        "Implemented the 'once it finishes' callback and the 'I'll report' "
        "summary line. Tests: 40 passed.",
        "```\n$ pytest\nstill running... I'll report\n```\nResult: 12 passed, done.",
        "",
    ],
)
def test_a_finished_report_that_mentions_the_words_is_not_pending(answer):
    assert announces_pending_work(answer) is None


def _bash(tool_id: str, background: bool, name: str = "Bash") -> dict:
    tool_input = {"command": "uv run pytest -q"}
    if background:
        tool_input["run_in_background"] = True
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}]}}


def _tool_result(tool_id: str, error: bool) -> dict:
    return {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": tool_id, "is_error": error, "content": "x"}]}}


def test_a_background_shell_started_and_never_read_is_open():
    events = [_bash("t1", True), _tool_result("t1", False)]
    assert open_background_shells(events) == 1


def test_a_refused_or_collected_background_shell_is_not_open():
    refused = [_bash("t1", True), _tool_result("t1", True)]
    collected = [_bash("t1", True), _tool_result("t1", False),
                 _bash("t2", False, name="BashOutput"), _tool_result("t2", False)]
    foreground = [_bash("t1", False), _tool_result("t1", False)]
    assert open_background_shells(refused) == 0
    assert open_background_shells(collected) == 0
    assert open_background_shells(foreground) == 0
    assert open_background_shells(None) == 0


# ---------------------------------------------------------------------------
# Background Bash is switched off in the CLI it starts
# ---------------------------------------------------------------------------


def test_the_cli_is_started_with_background_tasks_off_and_the_refusing_hook(
    tmp_path, monkeypatch
):
    ctx = _ctx(tmp_path, {"prompt": "do it"})
    _install(tmp_path, monkeypatch, ctx, [{"answer": DONE}])
    claude_code.body(ctx)
    (run,) = _runs(ctx)
    assert run["no_background"] == "1"
    assert run["settings"], "the CLI was not given the generated settings"
    settings = json.loads(Path(run["settings"]).read_text())
    assert settings["env"]["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] == "1"
    (entry,) = settings["hooks"]["PreToolUse"]
    assert "Bash" in entry["matcher"].split("|")
    (hook,) = entry["hooks"]
    assert hook["type"] == "command"
    # The settings live in the work directory, never in the checkout or the
    # uploaded artifacts.
    assert Path(run["settings"]).resolve().is_relative_to(ctx.work_dir.resolve())


def _hook_command(tmp_path: Path) -> str:
    path = claude_code.write_headless_settings(tmp_path / "settings-dir")
    settings = json.loads(path.read_text())
    return settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]


def _call_hook(command: str, payload: dict) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S602 - the command the CLI itself would run
        command, shell=True, input=json.dumps(payload), capture_output=True,
        text=True, timeout=30, check=False,
    )


def test_the_hook_refuses_a_background_bash_call(tmp_path):
    done = _call_hook(_hook_command(tmp_path), {
        "hook_event_name": "PreToolUse", "tool_name": "Bash",
        "tool_input": {"command": "uv run pytest", "run_in_background": True},
    })
    # Exit 2 is the CLI's "block this tool call"; stderr is what the model reads.
    assert done.returncode == 2
    assert "foreground" in done.stderr


@pytest.mark.parametrize("tool_input", [
    {"command": "uv run pytest"},
    {"command": "ls", "run_in_background": False},
])
def test_the_hook_lets_a_foreground_call_through(tmp_path, tool_input):
    done = _call_hook(_hook_command(tmp_path), {
        "hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": tool_input,
    })
    assert done.returncode == 0, done.stderr


def test_the_hook_lets_unreadable_input_through_rather_than_wedging_the_agent(tmp_path):
    done = subprocess.run(  # noqa: S602
        _hook_command(tmp_path), shell=True, input="not json", capture_output=True,
        text=True, timeout=30, check=False,
    )
    assert done.returncode == 0


# ---------------------------------------------------------------------------
# One resume to finish
# ---------------------------------------------------------------------------


def test_a_pending_work_answer_is_resumed_exactly_once(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, {"prompt": "fix it"})
    _install(tmp_path, monkeypatch, ctx, [
        {"answer": PENDING, "cost": 0.5, "turns": 3},
        {"answer": DONE, "cost": 0.25, "turns": 2},
    ])
    out = claude_code.body(ctx)
    runs = _runs(ctx)
    assert len(runs) == 2
    assert runs[0]["resume"] is None
    assert runs[1]["resume"] == SESSION
    assert runs[1]["prompt"] == FINISH_PROMPT
    assert FINISH_PROMPT == (
        "Finish: wait for every command you started, report its result, then end."
    )
    assert out["resumed_to_finish"] is True
    assert out["summary"] == DONE
    # Both invocations are in the stdout capture, and the spend covers both.
    stdout = (ctx.artifacts_dir / "claude-code.stdout.log").read_text()
    assert PENDING in stdout and DONE in stdout
    assert out["structured_output"]["total_cost_usd"] == pytest.approx(0.75)
    assert out["structured_output"]["num_turns"] == 5


def test_a_still_pending_answer_after_the_resume_is_not_resumed_again(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, {"prompt": "fix it"})
    _install(tmp_path, monkeypatch, ctx, [{"answer": PENDING}])
    out = claude_code.body(ctx)
    assert len(_runs(ctx)) == 2
    assert out["resumed_to_finish"] is True


def test_a_normal_answer_is_not_resumed(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, {"prompt": "fix it"})
    _install(tmp_path, monkeypatch, ctx, [{"answer": DONE}])
    out = claude_code.body(ctx)
    assert len(_runs(ctx)) == 1
    assert out["resumed_to_finish"] is False
    assert out["summary"] == DONE


def test_an_open_background_shell_is_resumed_even_under_a_plain_answer(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, {"prompt": "fix it"})
    _install(tmp_path, monkeypatch, ctx, [
        {"answer": "Pushed the fix.",
         "events": [_bash("t1", True), _tool_result("t1", False)]},
        {"answer": DONE},
    ])
    out = claude_code.body(ctx)
    assert [run["resume"] for run in _runs(ctx)] == [None, SESSION]
    assert out["resumed_to_finish"] is True


def test_no_resume_when_the_remaining_budget_is_below_the_minimum(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, {"prompt": "fix it", "timeout_seconds": 20})
    _install(tmp_path, monkeypatch, ctx, [{"answer": PENDING}, {"answer": DONE}])
    assert cliagent.FINISH_MIN_SECONDS > 20
    out = claude_code.body(ctx)
    assert len(_runs(ctx)) == 1
    assert out["resumed_to_finish"] is False
    assert out["finish_skipped"] == "budget"


def test_the_resumed_run_gets_only_the_remaining_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(cliagent, "FINISH_MIN_SECONDS", 1.0)
    ctx = _ctx(tmp_path, {"prompt": "fix it", "timeout_seconds": 4, "grace_seconds": 1})
    _install(tmp_path, monkeypatch, ctx, [
        {"answer": PENDING},
        {"answer": DONE, "sleep": 60},
    ])
    started = time.monotonic()
    with pytest.raises(RunnerFailure, match="timed out"):
        claude_code.body(ctx)
    # Killed at the step's deadline, not after a fresh full timeout.
    assert time.monotonic() - started < 15
    assert len(_runs(ctx)) == 2


def test_no_resume_without_a_session_id(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, {"prompt": "fix it"})
    _install(tmp_path, monkeypatch, ctx, [{"answer": PENDING}])
    plan = json.loads((ctx.work_dir / "plan.json").read_text())
    plan["session"] = "bad id!"
    (ctx.work_dir / "plan.json").write_text(json.dumps(plan))
    out = claude_code.body(ctx)
    assert len(_runs(ctx)) == 1
    assert out["resumed_to_finish"] is False
    assert out["finish_skipped"] == "no_session"


def test_the_session_id_is_masked_in_the_resumed_start_line(tmp_path, monkeypatch, capfd):
    ctx = _ctx(tmp_path, {"prompt": "fix it"})
    _install(tmp_path, monkeypatch, ctx, [{"answer": PENDING}, {"answer": DONE}])
    claude_code.body(ctx)
    err = capfd.readouterr().err
    assert SESSION not in err
    assert "resuming the session once" in err


def test_codex_never_resumes_to_finish(tmp_path, monkeypatch):
    from agent_worker.runners import codex

    assert codex.SPEC.finish_on_pending is False
    assert claude_code.SPEC.finish_on_pending is True
