"""The pre-publish gate's budget ends at the worker's kill, and a clamped step timeout is recorded (#2981).

WHY THE REVIEW WAS KILLED (task_2518ef3904ed47d0ac52, att_9b61ee8533b1450db91d).
The attempt started at 16:23:12Z; the runner's CLI child started at 16:41:07Z;
the gate's first round ended at 17:10:30Z and then ran the guard script as 11
parts; the worker killed the runner at its `_runner_deadline`, 18:23:48Z
("runner exceeded its 7200s timeout and was killed"). The runner's gate
measured what was left as `limits.timeout_seconds - duration_seconds`, where
`duration_seconds` sums only the CLI's own starts: the ~18 minutes between the
worker building SWARM_CHILD_TIMEOUT_SECONDS and the CLI starting were counted
nowhere, so the gate believed it had that much more than the worker gave it
and kept starting parts. Now:

1. the worker exports its deadline as an instant (`RUNNER_DEADLINE_ENV`);
2. the runner's gate budget is never more than what is left of it;
3. the runner hands the gate its staged input patches;
4. a spec's `timeout_seconds` the API lowered to the profile's cap is
   recorded in `limits.clamped` (`TIMEOUT_ASKED_ENV`, from the task document's
   `timeout_seconds_asked`).
"""

from __future__ import annotations

import secrets as pysecrets
import time
from pathlib import Path
from typing import Any

from agent_worker import lifecycle, prepublish
from agent_worker import workspace as workspace_mod
from agent_worker.runners import claude_code, cliagent
from agent_worker.runners.base import RunnerContext
from agent_worker.runners.limits import TIMEOUT_ENV

from worker_seeds import TENANT, seed_attempt

SESSION = "sess-" + "c3d4e5f60718293a"
LIMIT_SECONDS = 7200

FAKE_CLI = r'''#!/usr/bin/env python3
import json, sys
sys.stdin.read()
print(json.dumps({"type": "system", "subtype": "init", "session_id": "SESSION"}), flush=True)
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "session_id": "SESSION", "result": "All done.", "total_cost_usd": 0.1,
                  "num_turns": 1}), flush=True)
'''.replace("SESSION", SESSION)


def test_the_worker_and_the_runner_name_the_same_variables():
    assert lifecycle.RUNNER_DEADLINE_ENV == prepublish.RUNNER_DEADLINE_ENV
    assert lifecycle.TIMEOUT_ASKED_ENV == cliagent.TIMEOUT_ASKED_ENV


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------


def _ctx(tmp_path: Path, monkeypatch, *, deadline_in: float | None,
         asked: int | None = None) -> RunnerContext:
    work, artifacts = tmp_path / "work", tmp_path / "artifacts"
    (work / "repo").mkdir(parents=True)
    artifacts.mkdir()
    (work / "swarm-work.patch").write_text("diff --git a/x b/x\n")
    (work / "notes.md").write_text("a staged report, never applied\n")
    cli = tmp_path / "fake-cli"
    cli.write_text(FAKE_CLI)
    cli.chmod(0o755)
    monkeypatch.setenv("CLAUDE_CODE_BIN", str(cli))
    for name in ("CLAUDE_CODE_ARGS", "CLAUDE_CODE_OAUTH_TOKEN", "MODEL", "SWARM_CHILDREN",
                 "SWARM_GIT", cliagent.ACCOUNT_STREAM_ENV, cliagent.RESUME_SESSION_ENV,
                 cliagent.RESUME_REASON_ENV, prepublish.RUNNER_DEADLINE_ENV,
                 cliagent.TIMEOUT_ASKED_ENV):
        monkeypatch.delenv(name, raising=False)
    # Built at runtime: no credential-shaped literal in this file.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-" + "api03-" + pysecrets.token_hex(16))
    monkeypatch.setenv(TIMEOUT_ENV, str(LIMIT_SECONDS))
    if deadline_in is not None:
        monkeypatch.setenv(prepublish.RUNNER_DEADLINE_ENV, f"{time.time() + deadline_in:.3f}")
    if asked is not None:
        monkeypatch.setenv(cliagent.TIMEOUT_ASKED_ENV, str(asked))
    return RunnerContext(
        work_dir=work, artifacts_dir=artifacts, input_path=work / "input.json",
        result_path=work / "result.json", quota_path=work / "quota.json",
        payload={"prompt": "review the change", "staged_inputs": [
            {"path": "swarm-work.patch"}, {"path": "notes.md"}]},
        repo_dir=work / "repo",
    )


def _captured_gate(monkeypatch) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    def gate(ctx, *, remaining, staged_patches=(), **kwargs):
        seen["remaining"] = remaining()
        seen["staged_patches"] = list(staged_patches)
        return {"rounds": []}

    monkeypatch.setattr(cliagent, "run_prepublish_gate", gate)
    return seen


def test_the_gate_budget_ends_at_the_workers_kill(tmp_path, monkeypatch):
    # The worker's kill is 300 s away; the runner's own count says ~7,200.
    ctx = _ctx(tmp_path, monkeypatch, deadline_in=300.0)
    seen = _captured_gate(monkeypatch)
    claude_code.body(ctx)
    # MUTATION: dropping the `deadline_left` bound in `gate_remaining` gives
    # the gate ~7,200 s it does not have (task_2518ef3904ed47d0ac52) and
    # turns this red.
    assert 0 < seen["remaining"] <= 300.0, seen


def test_without_a_named_deadline_the_runner_counts_as_before(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch, deadline_in=None)
    seen = _captured_gate(monkeypatch)
    claude_code.body(ctx)
    assert LIMIT_SECONDS - 60 < seen["remaining"] <= LIMIT_SECONDS, seen


def test_the_gate_is_handed_the_staged_patches_and_nothing_else(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch, deadline_in=3600.0)
    seen = _captured_gate(monkeypatch)
    claude_code.body(ctx)
    # MUTATION: not passing `staged_patches` leaves the gate unable to see a
    # review's applied patch, and turns this red.
    assert seen["staged_patches"] == [str(ctx.work_dir / "swarm-work.patch")]


def test_a_clamped_step_timeout_is_recorded(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch, deadline_in=3600.0, asked=10_800)
    _captured_gate(monkeypatch)
    out = claude_code.body(ctx)
    # MUTATION: dropping the `step_timeout_asked` clamp leaves `clamped`
    # empty, as on #2981 (10,800 asked, 7,200 given), and turns this red.
    assert "timeout_seconds" in out["limits"]["clamped"]
    assert out["limits"]["timeout_seconds_asked"] == 10_800
    assert out["limits"]["timeout_seconds"] == LIMIT_SECONDS


def test_no_ask_no_clamp(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, monkeypatch, deadline_in=3600.0)
    _captured_gate(monkeypatch)
    out = claude_code.body(ctx)
    assert out["limits"]["clamped"] == [] and "timeout_seconds_asked" not in out["limits"]


def test_deadline_left_reads_the_instant_and_ignores_anything_else():
    env = {prepublish.RUNNER_DEADLINE_ENV: "1000.5"}
    assert prepublish.deadline_left(env, now=lambda: 900.0) == 100.5
    for raw in ("", "soon", "nan", "inf"):
        assert prepublish.deadline_left({prepublish.RUNNER_DEADLINE_ENV: raw}) is None
    assert prepublish.deadline_left({}) is None


# ---------------------------------------------------------------------------
# The worker
# ---------------------------------------------------------------------------


class _Clock:
    def __init__(self) -> None:
        self.now = 50_000.0

    def __call__(self) -> float:
        return self.now


def _child_env(db, worker_factory, tmp_path, monkeypatch, *, advance: float,
               task: dict[str, Any] | None = None, **kwargs) -> dict[str, str]:
    seed_attempt(db, runner_profile="mock")
    db.doc(f"tenants/{TENANT}")["credentials"] = ["anthropic"]
    clock = _Clock()
    with monkeypatch.context() as patch:
        patch.setattr(lifecycle.time, "monotonic", clock)
        worker, _, _ = worker_factory(**kwargs)
        worker.ws = workspace_mod.create(tmp_path / "ws", "att_1")
        if task is not None:
            worker._task = task
        clock.now += advance
        return worker._build_child_env()


def test_the_worker_exports_its_kill_as_an_instant(db, worker_factory, tmp_path, monkeypatch):
    before = time.time()
    env = _child_env(db, worker_factory, tmp_path, monkeypatch, advance=600,
                     timeout_seconds=3600)
    after = time.time()
    # MUTATION: not exporting RUNNER_DEADLINE_ENV turns this red (KeyError).
    instant = float(env[lifecycle.RUNNER_DEADLINE_ENV])
    assert before + 3000 - 1 <= instant <= after + 3000 + 1, (before, instant)


def test_the_worker_exports_a_clamped_ask_and_nothing_else(db, worker_factory, tmp_path,
                                                           monkeypatch):
    env = _child_env(db, worker_factory, tmp_path, monkeypatch, advance=0, timeout_seconds=7200,
                     task={lifecycle.TIMEOUT_ASKED_FIELD: 10_800})
    assert env[lifecycle.TIMEOUT_ASKED_ENV] == "10800"
    for task in ({}, {lifecycle.TIMEOUT_ASKED_FIELD: 3600}, {lifecycle.TIMEOUT_ASKED_FIELD: "x"},
                 {lifecycle.TIMEOUT_ASKED_FIELD: True}):
        env = _child_env(db, worker_factory, tmp_path / str(len(task)), monkeypatch, advance=0,
                         timeout_seconds=7200, task=task)
        assert lifecycle.TIMEOUT_ASKED_ENV not in env, task
