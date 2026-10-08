"""A codex run names its session, and a reloaded one continues it (#626).

The credential-reload path resumes the CLI's session instead of starting it
again from the prompt (26 restarts, ~169 agent-minutes lost on 2026-10-05, 8 of
them codex). claude-code names its session on every stream-json event; codex
`exec` prints prose and names its session only in the rollout it writes under
`$HOME/.codex/sessions/YYYY/MM/DD/rollout-<time>-<uuid>.jsonl`. So:

  * `codex.latest_session` finds the newest rollout this run wrote, and never
    one an earlier run left in the restored workspace;
  * a codex run started with `SWARM_RESUME_SESSION` continues it with
    `codex exec ... resume <id> -`, the reload's message on stdin;
  * a run on NO pool account told `SWARM_SESSION_FILE` writes its session there
    when the CLI exits -- refused or not -- for claude-code from its stream and
    for codex from its rollout.

These run the real runners against a fake CLI that records its argv and stdin.

MUTATIONS: drop `resume_flag` from codex's SPEC (the resume test raises
RunnerFailure); drop `session_locator` (the codex session-file test reads
None); drop the `since` filter in `latest_session` (the stale-rollout test
returns the old session); stop writing the session file in `run_cli_agent`
(both session-file tests read nothing).
"""

from __future__ import annotations

import json
import os
import secrets as pysecrets
import time
import uuid
from pathlib import Path

import pytest

from agent_worker.runners import claude_code, cliagent, codex
from agent_worker.runners.base import CredentialRevokedSignal, RunnerContext, RunnerFailure


def _openai_key() -> str:
    # Built at runtime: no credential-shaped literal in this file.
    return "sk-" + "proj-" + pysecrets.token_hex(16)


def _anthropic_key() -> str:
    return "sk-ant-" + "api03-" + pysecrets.token_hex(16)


def _ctx(tmp_path: Path) -> RunnerContext:
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
        payload={"prompt": "do the work"},
    )


def _rollout(home: Path, session: str, *, mtime: float | None = None) -> Path:
    day = home / ".codex" / "sessions" / "2026" / "10" / "06"
    day.mkdir(parents=True, exist_ok=True)
    path = day / f"rollout-2026-10-06T08-15-02-{session}.jsonl"
    path.write_text(json.dumps({"type": "session_meta", "payload": {"id": session}}) + "\n")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


#: A codex/claude stand-in. Records argv and stdin in `$HOME/runs.jsonl`. As
#: codex it writes a rollout naming SESSION and prints prose; as claude it
#: prints stream-json naming SESSION. `$HOME/mode` = "refused" fails it with
#: a revoked-token message.
FAKE_CLI = r'''#!/usr/bin/env python3
import json, os, pathlib, sys
home = pathlib.Path(os.environ["HOME"])
with (home / "runs.jsonl").open("a") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "stdin": sys.stdin.read()}) + "\n")
mode = (home / "mode").read_text().strip() if (home / "mode").exists() else "ok"
session = (home / "session").read_text().strip()
if sys.argv[1:2] == ["exec"]:
    day = home / ".codex" / "sessions" / "2026" / "10" / "06"
    day.mkdir(parents=True, exist_ok=True)
    rollout = day / ("rollout-2026-10-06T09-00-00-%s.jsonl" % session)
    with rollout.open("a") as f:
        f.write(json.dumps({"type": "session_meta", "payload": {"id": session}}) + "\n")
    if mode == "refused":
        print("ERROR: unexpected status 401 Unauthorized: OAuth access token has been revoked",
              file=sys.stderr)
        sys.exit(1)
    print("done")
    sys.exit(0)
print(json.dumps({"type": "system", "subtype": "init", "session_id": session}), flush=True)
if mode == "refused":
    print(json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True,
                      "session_id": session, "result": "OAuth access token has been revoked"}))
    sys.exit(1)
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "session_id": session, "result": "done"}))
'''


def _install(tmp_path: Path, monkeypatch, ctx: RunnerContext, bin_env: str, session: str,
             mode: str = "ok") -> None:
    cli = tmp_path / "fake-cli"
    cli.write_text(FAKE_CLI)
    cli.chmod(0o755)
    monkeypatch.setenv(bin_env, str(cli))
    (ctx.work_dir / "session").write_text(session)
    (ctx.work_dir / "mode").write_text(mode)
    for name in (
        "CLAUDE_CODE_ARGS", "CODEX_ARGS", "CLAUDE_CODE_OAUTH_TOKEN", "MODEL",
        "SWARM_CHILDREN", cliagent.ACCOUNT_STREAM_ENV, cliagent.ACCOUNT_MOVE_ENV,
        cliagent.RESUME_SESSION_ENV, cliagent.RESUME_REASON_ENV, cliagent.SESSION_FILE_ENV,
    ):
        monkeypatch.delenv(name, raising=False)


def _runs(ctx: RunnerContext) -> list[dict]:
    path = ctx.work_dir / "runs.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# -- codex.latest_session -------------------------------------------------------


def test_the_newest_rollout_written_since_the_start_names_the_session(tmp_path):
    started = time.time()
    older, newer = str(uuid.uuid4()), str(uuid.uuid4())
    _rollout(tmp_path, older, mtime=started + 1)
    _rollout(tmp_path, newer, mtime=started + 2)

    assert codex.latest_session(tmp_path, started) == newer


def test_a_rollout_an_earlier_run_left_behind_is_not_this_runs_session(tmp_path):
    # A checkpoint restores `.codex/` with the workspace: the previous
    # attempt's session is on disk before this run writes anything.
    _rollout(tmp_path, str(uuid.uuid4()), mtime=time.time() - 600)

    assert codex.latest_session(tmp_path, time.time()) is None


def test_no_sessions_directory_and_misnamed_files_name_no_session(tmp_path):
    started = time.time() - 5
    assert codex.latest_session(tmp_path, started) is None
    day = tmp_path / ".codex" / "sessions" / "2026" / "10" / "06"
    day.mkdir(parents=True)
    (day / "rollout-not-a-uuid.jsonl").write_text("{}\n")
    (day / "notes.txt").write_text("x")
    assert codex.latest_session(tmp_path, started) is None


def test_a_linked_rollout_is_not_followed(tmp_path):
    started = time.time() - 5
    target = tmp_path / "elsewhere.jsonl"
    target.write_text("{}\n")
    day = tmp_path / ".codex" / "sessions" / "2026" / "10" / "06"
    day.mkdir(parents=True)
    (day / f"rollout-2026-10-06T08-15-02-{uuid.uuid4()}.jsonl").symlink_to(target)

    assert codex.latest_session(tmp_path, started) is None


# -- a reloaded codex run continues its session ----------------------------------


def test_a_reloaded_codex_run_continues_its_session_with_exec_resume(tmp_path, monkeypatch):
    session = str(uuid.uuid4())
    ctx = _ctx(tmp_path)
    _install(tmp_path, monkeypatch, ctx, "CODEX_BIN", session)
    monkeypatch.setenv("OPENAI_API_KEY", _openai_key())
    monkeypatch.setenv(cliagent.RESUME_SESSION_ENV, session)
    monkeypatch.setenv(cliagent.RESUME_REASON_ENV, cliagent.RESUME_RELOADED)

    codex.body(ctx)

    (run,) = _runs(ctx)
    # `codex exec [flags] resume <id> -`: the session continues, the message
    # is on stdin, and the original prompt is not sent again.
    assert run["argv"] == [*codex.SPEC.args_default, "resume", session, "-"], run["argv"]
    assert run["stdin"] == cliagent.RELOAD_RESUME_PROMPT


def test_a_codex_session_id_of_the_wrong_shape_is_refused(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    _install(tmp_path, monkeypatch, ctx, "CODEX_BIN", str(uuid.uuid4()))
    monkeypatch.setenv("OPENAI_API_KEY", _openai_key())
    monkeypatch.setenv(cliagent.RESUME_SESSION_ENV, "--last")

    with pytest.raises(RunnerFailure):
        codex.body(ctx)
    assert not (ctx.work_dir / "runs.jsonl").exists()


# -- a run on no account writes its session for the worker -----------------------


def _session_file(tmp_path: Path, monkeypatch) -> Path:
    private = tmp_path / "private"
    private.mkdir()
    path = private / "session.json"
    monkeypatch.setenv(cliagent.SESSION_FILE_ENV, str(path))
    return path


def test_a_refused_codex_run_writes_the_session_its_rollout_names(tmp_path, monkeypatch):
    session = str(uuid.uuid4())
    ctx = _ctx(tmp_path)
    _install(tmp_path, monkeypatch, ctx, "CODEX_BIN", session, mode="refused")
    monkeypatch.setenv("OPENAI_API_KEY", _openai_key())
    # The previous attempt's session, restored with the workspace.
    _rollout(ctx.work_dir, str(uuid.uuid4()), mtime=time.time() - 600)
    target = _session_file(tmp_path, monkeypatch)

    with pytest.raises(CredentialRevokedSignal):
        codex.body(ctx)

    assert json.loads(target.read_text()) == {"session_id": session}
    assert oct(target.stat().st_mode & 0o777) == oct(0o600)


def test_a_refused_claude_run_writes_the_session_its_stream_names(tmp_path, monkeypatch):
    session = str(uuid.uuid4())
    ctx = _ctx(tmp_path)
    _install(tmp_path, monkeypatch, ctx, "CLAUDE_CODE_BIN", session, mode="refused")
    monkeypatch.setenv("ANTHROPIC_API_KEY", _anthropic_key())
    target = _session_file(tmp_path, monkeypatch)

    with pytest.raises(CredentialRevokedSignal):
        claude_code.body(ctx)

    assert json.loads(target.read_text()) == {"session_id": session}


def test_no_session_file_is_written_when_the_worker_named_none(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    _install(tmp_path, monkeypatch, ctx, "CLAUDE_CODE_BIN", str(uuid.uuid4()))
    monkeypatch.setenv("ANTHROPIC_API_KEY", _anthropic_key())

    claude_code.body(ctx)

    assert not any(p.name == "session.json" for p in tmp_path.rglob("session.json"))
