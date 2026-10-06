"""A credential reload OFF the account pool continues the session too (#626).

On a held pool account the runner's channel names the session and a reload
resumes it (`test_account_hot_swap`). An attempt on its tenant's own credential
has no channel -- and the codex runner cannot hold a pool account at all
(`accountlease.ACCOUNT_TOKEN_ENV` is claude's) -- so before this, every reload
there restarted the CLI from the prompt and threw away the work it had done.

The worker now names a session file for a run of a CLI that can resume
(`cliagent.SESSION_FILE_ENV`); the runner writes the session there when the CLI
exits; a reload reads it and restarts with `--resume <id>` (claude-code) or
`exec resume <id>` (codex), and records `credential_reloaded` with
`resumed: true`. These run the real worker and runners against a fake CLI.

MUTATIONS: leave `_session_channel_env` out of `_build_child_env`'s tenant
branch (both resume assertions go red: the second run starts from the prompt);
skip `_read_session_file` in the reload branch (the same).
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from agent_worker.errors import ExitCode
from agent_worker.runners.cliagent import RELOAD_RESUME_PROMPT
from swarm_common.states import TaskState

from conftest import TENANT, seed_attempt, seed_tenant

#: Records each start in PLAN_DIR/runs.jsonl and behaves per `plan.json`:
#: `modes[i]` is "refused" (a revoked token) or "finish"; `sessions[i]` the
#: session start i names ("" names none). As codex (`exec` first) it writes
#: a rollout under $HOME/.codex and prints prose; as claude, stream-json.
FAKE_CLI = r'''#!/usr/bin/env python3
import json, os, pathlib, sys
argv = sys.argv[1:]
plan_dir = pathlib.Path(PLAN_DIR)
plan = json.loads((plan_dir / "plan.json").read_text())
codex = argv[:1] == ["exec"]
flag = "resume" if codex else "--resume"
resume = argv[argv.index(flag) + 1] if flag in argv else None
with (plan_dir / "runs.jsonl").open("a") as f:
    f.write(json.dumps({"resume": resume, "stdin": sys.stdin.read()}) + "\n")
runs = sum(1 for _ in (plan_dir / "runs.jsonl").open())
mode = plan["modes"][min(runs - 1, len(plan["modes"]) - 1)]
session = resume or plan["sessions"][min(runs - 1, len(plan["sessions"]) - 1)]
if codex:
    if session:
        day = pathlib.Path(os.environ["HOME"]) / ".codex" / "sessions" / "2026" / "10" / "06"
        day.mkdir(parents=True, exist_ok=True)
        with (day / ("rollout-2026-10-06T09-00-00-%s.jsonl" % session)).open("a") as f:
            f.write(json.dumps({"type": "session_meta", "payload": {"id": session}}) + "\n")
    if mode == "refused":
        print("ERROR: 401 Unauthorized: OAuth access token has been revoked", file=sys.stderr)
        sys.exit(1)
    print("done")
    sys.exit(0)
if session:
    print(json.dumps({"type": "system", "subtype": "init", "session_id": session}), flush=True)
if mode == "refused":
    print(json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True,
                      "result": "OAuth access token has been revoked"}))
    sys.exit(1)
out = {"type": "result", "subtype": "success", "is_error": False, "result": "done"}
if session:
    out["session_id"] = session
print(json.dumps(out))
'''


@pytest.fixture
def cli(tmp_path, monkeypatch) -> Path:
    binary = tmp_path / "fake-cli"
    plan = tmp_path / "plan"
    plan.mkdir()
    binary.write_text(FAKE_CLI.replace("PLAN_DIR", repr(str(plan)), 1))
    binary.chmod(0o755)
    for name in ("CLAUDE_CODE_BIN", "CODEX_BIN"):
        monkeypatch.setenv(name, str(binary))
    for name in ("CLAUDE_CODE_ARGS", "CODEX_ARGS"):
        monkeypatch.delenv(name, raising=False)
    return binary


def _run(db, worker_factory, tmp_path, *, profile, provider, modes, sessions):
    seed_attempt(db, runner_profile=profile, task_input={"prompt": "do the work"})
    seed_tenant(db, credentials=[provider])
    worker, _config, _ = worker_factory(runner_profile=profile)
    plan = tmp_path / "plan"
    (plan / "plan.json").write_text(json.dumps({"modes": modes, "sessions": sessions}))
    code = worker.run()
    runs_file = plan / "runs.jsonl"
    runs = [json.loads(line) for line in runs_file.read_text().splitlines()] if (
        runs_file.exists()) else []
    return code, worker, runs


def _reloads(db) -> list[dict]:
    return [e["detail"] for e in db.events("task_1")
            if (e.get("detail") or {}).get("cause") == "credential_reloaded"]


@pytest.mark.parametrize(
    ("profile", "provider"), [("claude-code", "anthropic"), ("codex", "openai")]
)
def test_a_reload_on_the_tenant_credential_resumes_the_session(
    db, worker_factory, tmp_path, cli, log_stream, profile, provider
):
    session = str(uuid.uuid4())
    code, worker, runs = _run(
        db, worker_factory, tmp_path, profile=profile, provider=provider,
        modes=["refused", "finish"], sessions=[session, ""],
    )

    assert code == ExitCode.OK, log_stream.getvalue()[-3000:]
    assert worker._account is None, "this test is about a run on no pool account"
    assert [r["resume"] for r in runs] == [None, session]
    assert runs[1]["stdin"] == RELOAD_RESUME_PROMPT
    reloads = _reloads(db)
    assert len(reloads) == 1
    assert reloads[0]["resumed"] is True
    # The session id is a secret: in no event and no log line.
    assert session not in json.dumps(db.events("task_1"), default=str)
    assert session not in log_stream.getvalue()
    assert db.doc("tasks/task_1")["state"] == TaskState.SUCCEEDED.value


def test_a_run_that_named_no_session_restarts_from_the_prompt_and_says_so(
    db, worker_factory, tmp_path, cli, log_stream
):
    code, _worker, runs = _run(
        db, worker_factory, tmp_path, profile="claude-code", provider="anthropic",
        modes=["refused", "finish"], sessions=["", ""],
    )

    assert code == ExitCode.OK, log_stream.getvalue()[-3000:]
    assert [r["resume"] for r in runs] == [None, None]
    assert runs[1]["stdin"].startswith("do the work")
    (reload,) = _reloads(db)
    assert reload["resumed"] is False


def test_a_second_reload_resumes_the_session_the_resumed_run_named(
    db, worker_factory, tmp_path, cli, log_stream
):
    session = str(uuid.uuid4())
    code, _worker, runs = _run(
        db, worker_factory, tmp_path, profile="claude-code", provider="anthropic",
        modes=["refused", "refused", "finish"], sessions=[session, "", ""],
    )

    assert code == ExitCode.OK, log_stream.getvalue()[-3000:]
    assert [r["resume"] for r in runs] == [None, session, session]
    assert [r["resumed"] for r in _reloads(db)] == [True, True]
