"""The standing platform paragraphs ride the system prompt, said once per session.

The owner's "Shorter sessions" plan, finding 1 (2026-11-23): 30 days of task
records to that day show agents re-reading a median of 54,000 tokens of
context on every turn, 27 turns a task. The worker appended about ten
standing paragraphs -- tests, scratch area, saved outputs, the graph and Jev
lines, the context-ceiling handoff note, publishing -- to the USER prompt of
every session, so every fresh session's first message restated text that
never varies per task. They now go in the one `--append-system-prompt` the
claude-code runner already passes, and the user prompt keeps what varies per
task: the brief, the deliverables line, the child path, the time limit.

Held here against the real runner and a fake CLI that records its argv and
its stdin:

* every paragraph that existed before is delivered EXACTLY once per session,
  system prompt and user prompt together, on a first start and on a resumed
  session alike, word for word;
* the standing ones are in the system prompt, the per-task ones in the user
  prompt and never in the system prompt;
* the one flag is passed once: a second `--append-system-prompt` would
  replace the first.

MUTATIONS: append any standing paragraph to the prompt again (the once test
fails); move the time-limit or deliverables line into the system prompt (the
per-task test fails); pass the platform paragraphs in a second flag (the
one-flag test fails).
"""

from __future__ import annotations

import json
import secrets as pysecrets
from pathlib import Path

import pytest

from agent_worker import cachedirs, children, expected_outputs, lightpanda, stepservers
from agent_worker.runners import ceiling, claude_code, cliagent
from agent_worker.runners.base import RunnerContext
from agent_worker.runners.limits import TIMEOUT_ENV
from test_cli_prompt_names_time_limit import is_limit_line

SESSION = "sess-" + "a1b2c3d4e5f60718"

#: Records each start in `$HOME/runs.jsonl`: its argv and its whole stdin.
FAKE_CLI = r'''#!/usr/bin/env python3
import json, os, pathlib, sys
home = pathlib.Path(os.environ["HOME"])
stdin = sys.stdin.read()
with (home / "runs.jsonl").open("a") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "stdin": stdin}) + "\n")
print(json.dumps({"type": "system", "subtype": "init", "session_id": "SESSION"}), flush=True)
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "session_id": "SESSION", "result": "All done.", "total_cost_usd": 0.1,
                  "num_turns": 1}), flush=True)
'''.replace("SESSION", SESSION)

BRIEF = "Change the parser so that it accepts tabs."
TIMEOUT = 7200


def _key() -> str:
    # Built at runtime: no credential-shaped literal in this file.
    return "sk-ant-" + "api03-" + pysecrets.token_hex(16)


def _ctx(tmp_path: Path) -> RunnerContext:
    work = tmp_path / "work"
    artifacts = tmp_path / "artifacts"
    checkout = work / "repo"
    (checkout / "scripts").mkdir(parents=True)
    (checkout / claude_code.RELATED_TESTS_SCRIPT).write_text("#!/bin/sh\n")
    artifacts.mkdir(parents=True)
    return RunnerContext(
        work_dir=work, artifacts_dir=artifacts, input_path=work / "input.json",
        result_path=work / "result.json", quota_path=work / "quota.json",
        payload={"prompt": BRIEF}, repo_dir=checkout,
    )


def _install(tmp_path: Path, monkeypatch, *, resume: bool = False) -> None:
    cli = tmp_path / "fake-cli"
    cli.write_text(FAKE_CLI)
    cli.chmod(0o755)
    monkeypatch.setenv("CLAUDE_CODE_BIN", str(cli))
    for name in ("CLAUDE_CODE_ARGS", "CLAUDE_CODE_OAUTH_TOKEN", "MODEL",
                 cliagent.ACCOUNT_STREAM_ENV, cliagent.RESUME_SESSION_ENV,
                 cliagent.RESUME_REASON_ENV, cliagent.STEP_PUBLISHES_ENV,
                 claude_code.HANDOFF_ENV, *claude_code.AGENT_PASSTHROUGH_ENV,
                 claude_code.BROWSERS_PATH_ENV, claude_code.CDP_ENDPOINT_ENV,
                 claude_code.SEARCH_SPOOL_ENV, claude_code.MCP_RELAY_ENV,
                 lightpanda.PROFILE_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", _key())
    monkeypatch.setenv(TIMEOUT_ENV, str(TIMEOUT))
    work = tmp_path / "work"
    monkeypatch.setenv(claude_code.CHILDREN_ENV, str(work / ".swarm-children"))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "cache"))
    monkeypatch.setenv("SWARM_WORKSPACE", str(tmp_path))
    monkeypatch.setenv(stepservers.ENV, stepservers.NONE)
    # Jev opted in: the worker wrote the judge config (`judge.attempt.setup`).
    judge = tmp_path / "judge.json"
    judge.write_text("{}")
    monkeypatch.setenv(claude_code.JUDGE_CONFIG_ENV, str(judge))
    if resume:
        monkeypatch.setenv(cliagent.RESUME_SESSION_ENV, SESSION)


def _run(tmp_path: Path, monkeypatch, *, resume: bool = False) -> tuple[str, str, list[str]]:
    """(system prompt, user prompt, argv) of the one start."""
    _install(tmp_path, monkeypatch, resume=resume)
    ctx = _ctx(tmp_path)
    claude_code.body(ctx)
    runs = [json.loads(line) for line in
            (ctx.work_dir / "runs.jsonl").read_text().splitlines() if line.strip()]
    argv = runs[0]["argv"]
    flag = claude_code.APPEND_SYSTEM_PROMPT_FLAG
    system = argv[argv.index(flag) + 1] if flag in argv else ""
    return system, runs[0]["stdin"], argv


def _standing() -> list[str]:
    """Every paragraph that never varies per task, as it was word for word."""
    return [
        claude_code.RELATED_TESTS_NOTE,
        cachedirs.scratch_note(own_volume=False),  # TMPDIR is under SWARM_WORKSPACE here
        claude_code.SAVED_OUTPUT_NOTE,
        claude_code.JEV_DECIDE_NOTE,
        ceiling.HANDOFF_NOTE,
        cliagent.PUBLISH_PARAGRAPH,
    ]


@pytest.mark.parametrize("resume", [False, True])
def test_every_standing_paragraph_is_delivered_once_per_session(tmp_path, monkeypatch, resume):
    system, user, _ = _run(tmp_path, monkeypatch, resume=resume)
    for paragraph in _standing():
        assert (system + user).count(paragraph) == 1, (paragraph[:60], resume)


def test_the_standing_paragraphs_are_in_the_system_prompt(tmp_path, monkeypatch):
    system, user, _ = _run(tmp_path, monkeypatch)
    for paragraph in _standing():
        assert paragraph in system, paragraph[:60]
        assert paragraph not in user, paragraph[:60]
    # The tool moments the flag already carried stay there.
    assert claude_code.JEV_SYSTEM_NOTE in system


def test_the_per_task_paragraphs_stay_in_the_user_prompt(tmp_path, monkeypatch):
    system, user, _ = _run(tmp_path, monkeypatch)
    ctx_artifacts = tmp_path / "artifacts"
    per_task = [
        BRIEF,
        expected_outputs.deliverables_line(ctx_artifacts),
        children.prompt_line(str(tmp_path / "work" / ".swarm-children")),
    ]
    for paragraph in per_task:
        assert user.count(paragraph) == 1, paragraph[:60]
        assert paragraph not in system, paragraph[:60]
    assert user.startswith(BRIEF)
    # The time-limit line names an end time that moves with the clock, so it
    # is read back out (`is_limit_line`) rather than rebuilt here: rebuilt, a
    # second ticking between the runner and this line failed the test.
    last = user.rstrip().rpartition("\n\n")[2]
    assert is_limit_line(last), user[-600:]
    assert f"{TIMEOUT // 60} minutes" in last
    assert user.count("wall-clock limit") == 1
    assert "wall-clock limit" not in system


def test_the_system_prompt_flag_is_passed_once(tmp_path, monkeypatch):
    _, _, argv = _run(tmp_path, monkeypatch)
    assert argv.count(claude_code.APPEND_SYSTEM_PROMPT_FLAG) == 1
