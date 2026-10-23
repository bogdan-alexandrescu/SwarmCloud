"""A watched CLI run is stopped at a turn boundary before its limit and resumed
once to wrap up (#2139, the optional part of its proposed fix).

Measured 2026-10-23 by the failure-review schedule: three claude-code agents
(f-003, f-007, f-015) were killed mid-`sleep` at the step's 7200 s limit (exit
137) with their work uncommitted. Telling the agent its limit (the
`time_limit_line` step) is the first half; this is the net under it. When less
than the wrap-up margin is left, `_run_watched` stops the CLI at the next turn
boundary -- the way an account swap does, `terminate(reason="wrap-up")`, never
`mark_timed_out` -- and `run_cli_agent` resumes the session once with
`WRAP_UP_PROMPT` inside what is left of the budget. A run that reaches no turn
boundary is killed at the hard deadline exactly as before.

These run the real runner against a fake CLI that records each start's argv
and stdin, with the limits shrunk to seconds (`WRAP_UP_SECONDS`,
`WRAP_UP_FRACTION` and `FINISH_MIN_SECONDS` patched) so the margin is reached.

MUTATIONS: never arm the soft deadline in `_run_watched` (the first two tests
go red: the run is killed at the deadline as a timeout); arm it again on the
wrap-up pass (the once test goes red: a third start); compute the finish
pass's budget from the last start alone instead of the step's whole
`duration_seconds` (the budget test goes red); drop the FINISH_MIN_SECONDS
floor on the margin (the too-little-budget test goes red).
"""

from __future__ import annotations

import json
import secrets as pysecrets
import sys
import time
from pathlib import Path

import pytest

from agent_worker import procman
from agent_worker.runners import cliagent
from agent_worker.runners.base import RunnerContext, RunnerFailure
from agent_worker.runners.cliagent import CliAgentSpec, run_cli_agent
from agent_worker.runners.limits import GRACE_ENV, TIMEOUT_ENV
from time_limit_helper import is_limit_line

SESSION = "sess-" + "c3d4e5f60718293a"
LIMIT = 6.0
GRACE = 1.0

#: One start of the fake CLI, told what to do by `key=value` arguments (the
#: runner hands the child no variable of the test's own). Every start appends
#: its argv, stdin and start time to `$HOME/runs.jsonl`. A resumed start
#: (`--resume`) answers at once -- the second start with `wrap=` (or, given
#: `loops`, by running turns until it is killed); a later one with "All
#: done.". A fresh start does what `mode=` says: `turns` emits a turn boundary
#: every 0.2 s and never ends by itself; `silent` sits in one long call with no
#: boundary; `short` emits turns for `seconds=` and then succeeds.
FAKE_CLI = r'''#!PYTHON
import json, os, pathlib, sys, time
home = pathlib.Path(os.environ["HOME"])
runs = home / "runs.jsonl"
index = len(runs.read_text().splitlines()) if runs.exists() else 0
stdin = sys.stdin.read()
opts = dict(arg.split("=", 1) for arg in sys.argv[1:] if "=" in arg)
with runs.open("a") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "stdin": stdin, "at": time.time()}) + "\n")

def emit(event):
    print(json.dumps(event), flush=True)

def turn():
    emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "working"}]}})
    emit({"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}})

def done(answer):
    emit({"type": "result", "subtype": "success", "is_error": False, "session_id": "SESSION",
          "result": answer, "total_cost_usd": 0.01, "num_turns": 1})
    sys.exit(0)

emit({"type": "system", "subtype": "init", "session_id": "SESSION"})
if "--resume" in sys.argv:
    if index == 1 and opts.get("wrap") == "loops":
        while True:
            turn()
            time.sleep(0.2)
    if index == 1:
        done(opts.get("wrap", "Committed; the pull request files are written."))
    done("All done.")
mode = opts["mode"]
if mode == "turns":
    while True:
        turn()
        time.sleep(0.2)
elif mode == "silent":
    time.sleep(120)
else:
    end = time.time() + float(opts["seconds"])
    while time.time() < end:
        turn()
        time.sleep(0.2)
    done("Finished on my own.")
'''.replace("#!PYTHON", "#!" + sys.executable).replace("SESSION", SESSION)


def _ctx(tmp_path: Path) -> RunnerContext:
    work, artifacts = tmp_path / "work", tmp_path / "artifacts"
    work.mkdir(parents=True)
    artifacts.mkdir(parents=True)
    return RunnerContext(work_dir=work, artifacts_dir=artifacts, input_path=work / "input.json",
                         result_path=work / "result.json", quota_path=work / "quota.json",
                         payload={"prompt": "fix the thing"})


def _spec(*, finish_on_pending: bool = False) -> CliAgentSpec:
    return CliAgentSpec(name="fake", provider="anthropic", binary_env="FAKE_BIN",
                        binary_default="fake", args_env="FAKE_ARGS", args_default=(),
                        key_env="FAKE_KEY", model_flag=None, resume_flag="--resume",
                        finish_on_pending=finish_on_pending)


class _Spy:
    """What the runner did to its children: terminate reasons, timeouts
    marked, and the timeout each watched start was given."""

    def __init__(self, monkeypatch) -> None:
        self.reasons: list[str] = []
        self.timed_out = 0
        self.budgets: list[float] = []
        terminate = procman.ChildProcess.terminate
        mark = procman.ChildProcess.mark_timed_out
        watched = cliagent._run_watched

        def spy_terminate(child, grace_seconds, *, reason="terminated"):
            self.reasons.append(reason)
            return terminate(child, grace_seconds, reason=reason)

        def spy_mark(child):
            self.timed_out += 1
            return mark(child)

        def spy_watched(*args, **kwargs):
            self.budgets.append(kwargs["limits"].timeout_seconds)
            return watched(*args, **kwargs)

        monkeypatch.setattr(procman.ChildProcess, "terminate", spy_terminate)
        monkeypatch.setattr(procman.ChildProcess, "mark_timed_out", spy_mark)
        monkeypatch.setattr(cliagent, "_run_watched", spy_watched)


def _install(
    tmp_path: Path, monkeypatch, *args: str, finish_min: float = 1.0
) -> _Spy:
    cli = tmp_path / "fake-cli"
    cli.write_text(FAKE_CLI)
    cli.chmod(0o755)
    for name in ("MODEL", "SWARM_CHILDREN", cliagent.ACCOUNT_MOVE_ENV,
                 cliagent.RESUME_SESSION_ENV, cliagent.RESUME_REASON_ENV,
                 cliagent.SESSION_FILE_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FAKE_BIN", str(cli))
    monkeypatch.setenv("FAKE_KEY", "k-" + pysecrets.token_hex(8))
    monkeypatch.setenv("FAKE_ARGS", json.dumps(list(args)))
    # On a pool account: the stream is watched, so turn boundaries are seen.
    monkeypatch.setenv(cliagent.ACCOUNT_STREAM_ENV, str(tmp_path / "stream.json"))
    monkeypatch.setenv(TIMEOUT_ENV, str(LIMIT))
    monkeypatch.setenv(GRACE_ENV, str(GRACE))
    # The margin in seconds instead of minutes: half of the 6 s limit.
    monkeypatch.setattr(cliagent, "WRAP_UP_SECONDS", 3.0)
    monkeypatch.setattr(cliagent, "WRAP_UP_FRACTION", 0.5)
    monkeypatch.setattr(cliagent, "FINISH_MIN_SECONDS", finish_min)
    return _Spy(monkeypatch)


def _runs(ctx: RunnerContext) -> list[dict]:
    path = ctx.work_dir / "runs.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _is_wrap_up(stdin: str) -> bool:
    return stdin.startswith(f"{cliagent.WRAP_UP_PROMPT}\n\n")


def test_stops_at_a_turn_boundary_before_the_deadline_and_resumes_to_wrap_up(
    tmp_path, monkeypatch
):
    ctx = _ctx(tmp_path)
    spy = _install(tmp_path, monkeypatch, "mode=turns")
    began = time.time()

    run_cli_agent(ctx, _spec())

    first, wrap = _runs(ctx)
    # Stopped at a boundary past the soft deadline (6 s - 3 s), before the hard one.
    assert 3.0 <= wrap["at"] - began < LIMIT, wrap["at"] - began
    assert spy.reasons[0] == "wrap-up", spy.reasons
    assert spy.timed_out == 0, "a wrap-up stop is not a timeout"
    # Resumed once, the same session, told to wrap up and how long it has.
    assert wrap["argv"][-2:] == ["--resume", SESSION]
    assert _is_wrap_up(wrap["stdin"]), wrap["stdin"]
    assert is_limit_line(wrap["stdin"][len(cliagent.WRAP_UP_PROMPT) + 2 :].rstrip())
    # Inside what is left of the step's budget, never a fresh limit.
    assert spy.budgets[0] == LIMIT
    assert spy.budgets[1] <= LIMIT - 3.0, spy.budgets
    assert ctx.report.get("resumed_to_wrap_up") is True
    # A CLI that echoes the message is not misread as a 429 or a dead credential.
    said = cliagent.WRAP_UP_PROMPT.lower()
    markers = (*cliagent._RATE_LIMIT_MARKERS, *cliagent._CREDENTIAL_MARKERS)
    assert [m for m in markers if m in said] == []


def test_successful_wrap_up_is_not_a_timeout(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    _install(tmp_path, monkeypatch, "mode=turns")

    out = run_cli_agent(ctx, _spec())

    assert out["exit_code"] == 0
    assert out["resumed_to_wrap_up"] is True
    assert "Committed; the pull request files are written." in out["summary"]
    assert out["metrics"]["duration_seconds"] <= LIMIT + GRACE


def test_no_turn_boundary_is_still_killed_at_the_hard_deadline(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    spy = _install(tmp_path, monkeypatch, "mode=silent")

    with pytest.raises(RunnerFailure) as caught:
        run_cli_agent(ctx, _spec())

    assert str(caught.value) == f"fake timed out after {LIMIT:.0f}s"
    assert spy.reasons == ["timeout"] and spy.timed_out == 1
    assert len(_runs(ctx)) == 1, "no wrap-up pass after a hard timeout"
    assert not ctx.report.get("resumed_to_wrap_up")


def test_too_little_budget_skips_the_wrap_up(tmp_path, monkeypatch):
    # A 3 s margin is less than a 60 s finish floor: the soft deadline is
    # never armed, and the run goes on to end by itself as before.
    ctx = _ctx(tmp_path)
    spy = _install(tmp_path, monkeypatch, "mode=short", "seconds=4", finish_min=60.0)

    out = run_cli_agent(ctx, _spec())

    assert [_is_wrap_up(run["stdin"]) for run in _runs(ctx)] == [False]
    assert spy.reasons == [] and spy.timed_out == 0
    assert out["resumed_to_wrap_up"] is False
    assert "Finished on my own." in out["summary"]


def test_wrap_up_runs_once_and_never_past_the_budget(tmp_path, monkeypatch):
    # A wrap-up answer that announces pending work gets the finish pass --
    # inside what the first start AND the wrap-up pass left, not more.
    ctx = _ctx(tmp_path)
    spy = _install(tmp_path, monkeypatch, "mode=turns",
                   "wrap=The suite is still running in the background.")
    began = time.time()

    out = run_cli_agent(ctx, _spec(finish_on_pending=True))

    runs = _runs(ctx)
    assert [_is_wrap_up(run["stdin"]) for run in runs] == [False, True, False]
    assert runs[2]["stdin"] == cliagent.FINISH_PROMPT
    assert out["resumed_to_wrap_up"] is True and out["resumed_to_finish"] is True
    first_and_wrap = runs[2]["at"] - began
    assert spy.budgets[2] <= LIMIT - first_and_wrap + 0.5, (spy.budgets, first_and_wrap)
    assert time.time() - began <= LIMIT + GRACE + 1.0

    # And a wrap-up pass that keeps running turns is never stopped a second
    # time: it is killed at what is left of the hard deadline.
    ctx = _ctx(tmp_path / "again")
    spy = _install(tmp_path, monkeypatch, "mode=turns", "wrap=loops")
    began = time.time()

    with pytest.raises(RunnerFailure) as caught:
        run_cli_agent(ctx, _spec())

    assert "timed out" in str(caught.value)
    assert [_is_wrap_up(run["stdin"]) for run in _runs(ctx)] == [False, True]
    assert spy.reasons == ["wrap-up", "timeout"], spy.reasons
    assert time.time() - began <= LIMIT + GRACE + 1.0
