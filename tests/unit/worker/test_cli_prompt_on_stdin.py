"""The agent CLI is given its prompt on stdin, never in its argv.

Owner decision 2026-10-06 (observer P9). Measured on task_cb50036f4e264168b39e,
a review step: after a pytest timed out the agent ran

    ps aux | grep "[p]ytest tests/unit/scripts" | awk '{print $2}' | xargs -r kill

The worker passed the whole prompt as the CLI's LAST ARGUMENT, the brief
contained that very text, so the grep matched the claude CLI's own command
line and the agent killed its own CLI (exit 143). Any process listing an
agent runs sees every process's argv; the prompt is the one thing in it that
an agent's own pattern can match. And one argv string is capped at 128 KiB
(MAX_ARG_STRLEN) on Linux, so a long brief could not start at all.

So the prompt -- the first pass's, a resumed session's and every finish or
repair pass's -- is written to the CLI's stdin: `claude -p` reads it from
there when no prompt argument is given (checked against the pinned 2.1.283),
and `codex exec -` reads stdin by its documented `-`.

These run the real runners against a fake CLI that records its argv and what
arrived on its stdin.

MUTATIONS: append the prompt to the argv again in `run_cli_agent` (every
"not in argv" assertion goes red, and the >128 KiB start fails with E2BIG);
start the child with `stdin=DEVNULL` again in `procman.ChildProcess.start`
(every "stdin" assertion goes red); drop `stdin_arg` from codex's SPEC (the
codex argv assertion goes red).
"""

from __future__ import annotations

import json
import secrets as pysecrets
from pathlib import Path

import pytest

from agent_worker.runners import claude_code, cliagent, codex
from agent_worker.runners.base import RunnerContext

#: A distinctive brief, carrying the very pipeline that killed the CLI.
MARKER = "quokka-lantern-" + "7f3e91"
PROMPT = (
    f"Review the {MARKER} change. If a test hangs, run "
    "ps aux | grep \"[p]ytest tests/unit/scripts\" | awk '{print $2}' | xargs -r kill"
)
SESSION = "sess-" + "a1b2c3d4e5f60718"


def _anthropic_key() -> str:
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


#: Records each start in `$HOME/runs.jsonl`: its argv and its whole stdin.
#: Answers with the plan's next answer, as a stream-json conversation.
FAKE_CLI = r'''#!/usr/bin/env python3
import json, os, pathlib, sys
home = pathlib.Path(os.environ["HOME"])
stdin = sys.stdin.read()
with (home / "runs.jsonl").open("a") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "stdin": stdin}) + "\n")
runs = sum(1 for _ in (home / "runs.jsonl").open())
plan_file = home / "plan.json"
answers = json.loads(plan_file.read_text()) if plan_file.exists() else ["done"]
answer = answers[min(runs - 1, len(answers) - 1)]
print(json.dumps({"type": "system", "subtype": "init", "session_id": "SESSION"}), flush=True)
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "session_id": "SESSION", "result": answer, "total_cost_usd": 0.1,
                  "num_turns": 1}), flush=True)
'''.replace("SESSION", SESSION)


def _install(tmp_path: Path, monkeypatch, ctx: RunnerContext, bin_env: str) -> None:
    cli = tmp_path / "fake-cli"
    cli.write_text(FAKE_CLI)
    cli.chmod(0o755)
    monkeypatch.setenv(bin_env, str(cli))
    for name in (
        "CLAUDE_CODE_ARGS", "CODEX_ARGS", "CLAUDE_CODE_OAUTH_TOKEN", "MODEL",
        "SWARM_CHILDREN", cliagent.ACCOUNT_STREAM_ENV, cliagent.RESUME_SESSION_ENV,
        cliagent.RESUME_REASON_ENV,
    ):
        monkeypatch.delenv(name, raising=False)


def _runs(ctx: RunnerContext) -> list[dict]:
    path = ctx.work_dir / "runs.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _assert_prompt_not_in(argv: list[str], text: str) -> None:
    joined = "\n".join(argv)
    assert MARKER not in joined, argv
    assert "[p]ytest" not in joined, argv
    # No fragment of the message rides in the argv either. (The platform's
    # instructions after a caller's prompt name the artifacts directory, a
    # path the argv may share a prefix with, so callers pass the message.)
    for start in range(0, max(len(text) - 24, 1), 12):
        assert text[start : start + 24] not in joined, (text[start : start + 24], argv)


def test_claude_code_gets_its_prompt_on_stdin_and_none_of_it_in_argv(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, {"prompt": PROMPT})
    _install(tmp_path, monkeypatch, ctx, "CLAUDE_CODE_BIN")
    monkeypatch.setenv("ANTHROPIC_API_KEY", _anthropic_key())

    claude_code.body(ctx)

    (run,) = _runs(ctx)
    assert run["stdin"].startswith(PROMPT), run["stdin"][:200]
    _assert_prompt_not_in(run["argv"], PROMPT)
    # Everything else is as before: print mode, streamed JSON, the settings.
    assert run["argv"][:5] == list(claude_code.SPEC.args_default)
    assert "--settings" in run["argv"]


def test_codex_gets_its_prompt_on_stdin_by_its_dash_argument(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, {"prompt": PROMPT})
    _install(tmp_path, monkeypatch, ctx, "CODEX_BIN")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-" + "proj-" + pysecrets.token_hex(16))

    codex.body(ctx)

    (run,) = _runs(ctx)
    assert run["stdin"].startswith(PROMPT), run["stdin"][:200]
    _assert_prompt_not_in(run["argv"], PROMPT)
    assert run["argv"] == [*codex.SPEC.args_default, "-"], run["argv"]


def test_a_resumed_session_gets_its_message_on_stdin(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, {"prompt": PROMPT})
    _install(tmp_path, monkeypatch, ctx, "CLAUDE_CODE_BIN")
    monkeypatch.setenv("ANTHROPIC_API_KEY", _anthropic_key())
    monkeypatch.setenv(cliagent.RESUME_SESSION_ENV, SESSION)
    monkeypatch.setenv(cliagent.RESUME_REASON_ENV, cliagent.RESUME_MOVED)

    claude_code.body(ctx)

    (run,) = _runs(ctx)
    assert run["stdin"] == cliagent.RESUME_PROMPT
    argv = run["argv"]
    assert argv[argv.index("--resume") + 1] == SESSION
    assert argv[-1] == SESSION, "nothing follows the session id: the message is on stdin"
    _assert_prompt_not_in(argv, cliagent.RESUME_PROMPT)


def test_a_finish_pass_gets_its_prompt_on_stdin(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, {"prompt": PROMPT})
    _install(tmp_path, monkeypatch, ctx, "CLAUDE_CODE_BIN")
    monkeypatch.setenv("ANTHROPIC_API_KEY", _anthropic_key())
    (ctx.work_dir / "plan.json").write_text(json.dumps([
        "The suite is still running; I'll report the result once it finishes.",
        "All done: 12 passed.",
    ]))

    out = claude_code.body(ctx)

    first, finish = _runs(ctx)
    assert out.get("resumed_to_finish") is True, out
    assert first["stdin"].startswith(PROMPT)
    assert finish["stdin"] == cliagent.FINISH_PROMPT
    argv = finish["argv"]
    assert argv[argv.index("--resume") + 1] == SESSION
    assert argv[-1] == SESSION
    _assert_prompt_not_in(argv, cliagent.FINISH_PROMPT)
    _assert_prompt_not_in(argv, PROMPT)


def test_a_prompt_over_128_kib_reaches_the_cli_whole(tmp_path, monkeypatch):
    # One argv string over MAX_ARG_STRLEN (128 KiB on Linux) fails execve with
    # E2BIG; on stdin there is no such limit.
    big = PROMPT + "\n" + ("context line for the reviewer\n" * 8000)
    assert len(big.encode()) > 200 * 1024
    ctx = _ctx(tmp_path, {"prompt": big})
    _install(tmp_path, monkeypatch, ctx, "CLAUDE_CODE_BIN")
    monkeypatch.setenv("ANTHROPIC_API_KEY", _anthropic_key())

    claude_code.body(ctx)

    (run,) = _runs(ctx)
    assert run["stdin"].startswith(big), len(run["stdin"])
    assert MARKER not in "\n".join(run["argv"])


def test_the_start_line_names_the_prompt_length_and_not_the_prompt(
    tmp_path, monkeypatch, capsys
):
    ctx = _ctx(tmp_path, {"prompt": PROMPT})
    _install(tmp_path, monkeypatch, ctx, "CLAUDE_CODE_BIN")
    monkeypatch.setenv("ANTHROPIC_API_KEY", _anthropic_key())

    claude_code.body(ctx)
    err = capsys.readouterr().err

    (run,) = _runs(ctx)
    started = [
        json.loads(line) for line in err.splitlines()
        if line.startswith("{") and "child started" in line
    ]
    assert len(started) == 1, err
    assert MARKER not in err
    # The argv logged is the argv started, binary first: nothing is masked
    # because nothing caller-written is in it.
    assert started[0]["argv"][1:] == run["argv"]
    assert started[0]["stdin_bytes"] == len(run["stdin"].encode())


@pytest.mark.parametrize("prompt", ["", "   "])
def test_an_empty_prompt_is_still_refused(tmp_path, monkeypatch, prompt):
    ctx = _ctx(tmp_path, {"prompt": prompt})
    _install(tmp_path, monkeypatch, ctx, "CLAUDE_CODE_BIN")
    monkeypatch.setenv("ANTHROPIC_API_KEY", _anthropic_key())
    with pytest.raises(cliagent.RunnerFailure):
        claude_code.body(ctx)
