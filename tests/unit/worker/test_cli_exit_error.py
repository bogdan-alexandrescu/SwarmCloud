"""What the worker records when the agent CLI exits non-zero.

Owner decision 2026-10-06 (lane ERR). C1A's review (task_cb50036f4e264168b39e)
was killed by SIGTERM, exit 143, and recorded

    claude-code exited 143: Ignoring 23 permissions.allow entries from
    .claude/settings.json: this workspace has not been trusted. Run Claude
    Code interactively...

-- a warning every headless run prints, which made a kill read as a
permissions problem. The runner now drops known-benign stderr lines before it
chooses what to record, and names a signal exit plainly.

The first three tests run the real claude-code runner against a fake CLI that
writes the given stderr and exits with the given code; the rest pin the pure
composition in `cliagent.exit_error`.

MUTATIONS: empty `claude_code.SPEC.benign_stderr` (the trust-line tests go
red); make `exit_error` return the old `exited N: <tail>` for every code (the
SIGTERM/SIGKILL tests go red).
"""

from __future__ import annotations

import secrets as pysecrets
from pathlib import Path

import pytest

from agent_worker.runners import claude_code, cliagent
from agent_worker.runners.base import RunnerContext, RunnerFailure

TRUST_LINE = (
    "Ignoring 23 permissions.allow entries from .claude/settings.json: this workspace "
    "has not been trusted. Run Claude Code interactively in this directory to trust it."
)


def _anthropic_key() -> str:
    # Built at runtime: no credential-shaped literal in this file.
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
        payload={"prompt": "Review the change."},
    )


FAKE_CLI = r'''#!/usr/bin/env python3
import sys
sys.stdin.read()
sys.stderr.write(STDERR)
sys.stderr.flush()
sys.exit(CODE)
'''


def _run(tmp_path: Path, monkeypatch, stderr: str, code: int) -> str:
    ctx = _ctx(tmp_path)
    cli = tmp_path / "fake-cli"
    cli.write_text(FAKE_CLI.replace("STDERR", repr(stderr)).replace("CODE", str(code)))
    cli.chmod(0o755)
    monkeypatch.setenv("CLAUDE_CODE_BIN", str(cli))
    for name in (
        "CLAUDE_CODE_ARGS", "CLAUDE_CODE_OAUTH_TOKEN", "MODEL", "SWARM_CHILDREN",
        cliagent.ACCOUNT_STREAM_ENV, cliagent.RESUME_SESSION_ENV, cliagent.RESUME_REASON_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", _anthropic_key())
    with pytest.raises(RunnerFailure) as caught:
        claude_code.body(ctx)
    return str(caught.value)


def test_a_sigterm_exit_with_only_the_trust_line_records_the_signal(tmp_path, monkeypatch):
    error = _run(tmp_path, monkeypatch, TRUST_LINE + "\n", 143)
    assert error == "claude-code terminated by SIGTERM (exit 143)", error
    assert "trusted" not in error
    assert "permissions" not in error


def test_a_real_error_after_the_trust_line_is_kept(tmp_path, monkeypatch):
    real = "Error: ENOSPC: no space left on device, write"
    error = _run(tmp_path, monkeypatch, f"{TRUST_LINE}\n{real}\n", 1)
    assert error == f"claude-code exited 1: {real}", error


def test_exit_1_with_a_real_message_is_recorded_as_before(tmp_path, monkeypatch):
    real = "Error: the model returned an invalid response\n    at handler (cli.js:12:3)"
    error = _run(tmp_path, monkeypatch, real + "\n", 1)
    assert error == f"claude-code exited 1: {real}", error


def test_a_sigkill_exit_is_named_and_keeps_the_most_informative_line():
    stderr = f"{TRUST_LINE}\nfetching session\nFatal: heap out of memory\n\n"
    error = cliagent.exit_error("claude-code", 137, stderr, claude_code.SPEC.benign_stderr)
    assert error == "claude-code killed by SIGKILL (exit 137): Fatal: heap out of memory"


def test_a_signal_exit_without_an_error_word_keeps_the_last_line():
    stderr = f"{TRUST_LINE}\nstarting turn 4\nwriting checkpoint\n"
    error = cliagent.exit_error("claude-code", 143, stderr, claude_code.SPEC.benign_stderr)
    assert error == "claude-code terminated by SIGTERM (exit 143): writing checkpoint"


@pytest.mark.parametrize(
    ("code", "sentence"),
    [
        (130, "terminated by SIGINT (exit 130)"),
        (129, "terminated by SIGHUP (exit 129)"),
        (134, "terminated by SIGABRT (exit 134)"),
        (-15, "terminated by SIGTERM (exit -15)"),
        (-9, "killed by SIGKILL (exit -9)"),
    ],
)
def test_other_signal_exits_are_named(code, sentence):
    assert cliagent.exit_error("claude-code", code, "", ()) == f"claude-code {sentence}"


def test_a_code_above_128_that_is_no_signal_reads_as_an_exit():
    assert cliagent.exit_error("claude-code", 250, "boom\n", ()) == "claude-code exited 250: boom"


def test_codex_keeps_lines_claude_code_calls_benign():
    # The filter is per CLI: codex declares no benign lines, so nothing of its
    # stderr is dropped on claude-code's account.
    error = cliagent.exit_error("codex", 1, TRUST_LINE + "\n", ())
    assert error == f"codex exited 1: {TRUST_LINE}"


def test_capture_notices_are_never_the_line_kept():
    notice = cliagent._CAPTURE_MARK + " 4096 bytes dropped"
    error = cliagent.exit_error("claude-code", 143, f"real failure\n{notice}\n", ())
    assert error == "claude-code terminated by SIGTERM (exit 143): real failure"
