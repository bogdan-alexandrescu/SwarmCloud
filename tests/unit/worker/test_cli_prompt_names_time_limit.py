"""A claude-code or codex step is told how long it has (#2139).

Measured 2026-10-23 by the failure-review schedule: three claude-code agents
(f-003, f-007, f-015) started the tenant's whole pytest suite in the
background and polled it with ~600 s `sleep` calls, and were killed mid-sleep
at the step's 7200 s limit (exit 137); one dependant review was cancelled
`failed_parent` (f-004). Nothing in the prompt named the limit:
`run_cli_agent` resolved it only after the prompt was built, and at the
deadline the watched loop terminates the CLI without a word to the agent.

So every prompt carries one platform line, built by `cliagent.time_limit_line`,
naming the limit in minutes and its end time in UTC, and telling the agent to
commit and write its pull request's title and body before then -- and not to
run the whole suite in the background and poll it with sleep. A fresh start
with a repository has it just before PUBLISH_PARAGRAPH, which still ends the
prompt; one without a repository has it last; a resumed session (an account
move, a credential reload) has it after its resume message, with that run's
own limit.

These run the real runner against a fake CLI that records what arrived on its
stdin.

MUTATIONS: drop the line from the fresh-start path in `run_cli_agent` (the
repository and no-repository tests go red); drop it from the resume path (the
resumed-session test goes red); compute it from the platform default instead
of `limits.timeout_seconds` (every "120 minutes" assertion goes red).
"""

from __future__ import annotations

import json
import re
import secrets as pysecrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_worker.runners import claude_code, cliagent
from agent_worker.runners.base import RunnerContext
from agent_worker.runners.limits import TIMEOUT_ENV

LIMIT_SECONDS = 7200
SESSION = "sess-" + "b2c3d4e5f6071829"
_END = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")


def _anthropic_key() -> str:
    # Built at runtime: no credential-shaped literal in this file.
    return "sk-ant-" + "api03-" + pysecrets.token_hex(16)


def _ctx(tmp_path: Path, payload: dict, *, repo: bool) -> RunnerContext:
    work = tmp_path / "work"
    artifacts = tmp_path / "artifacts"
    work.mkdir(parents=True, exist_ok=True)
    artifacts.mkdir(parents=True, exist_ok=True)
    checkout = None
    if repo:
        checkout = work / "repo"
        checkout.mkdir()
    return RunnerContext(
        work_dir=work,
        artifacts_dir=artifacts,
        input_path=work / "input.json",
        result_path=work / "result.json",
        quota_path=work / "quota.json",
        payload=payload,
        repo_dir=checkout,
    )


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


def _install(tmp_path: Path, monkeypatch) -> None:
    cli = tmp_path / "fake-cli"
    cli.write_text(FAKE_CLI)
    cli.chmod(0o755)
    monkeypatch.setenv("CLAUDE_CODE_BIN", str(cli))
    for name in (
        "CLAUDE_CODE_ARGS", "CLAUDE_CODE_OAUTH_TOKEN", "MODEL", "SWARM_CHILDREN", "SWARM_GIT",
        cliagent.ACCOUNT_STREAM_ENV, cliagent.RESUME_SESSION_ENV, cliagent.RESUME_REASON_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", _anthropic_key())
    # The step's limit, the way the worker hands it over: the platform ceiling
    # `resolve_limits` reads, exported from WorkerConfig.
    monkeypatch.setenv(TIMEOUT_ENV, str(LIMIT_SECONDS))


def _stdin(ctx: RunnerContext) -> str:
    path = ctx.work_dir / "runs.jsonl"
    (run,) = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return run["stdin"]


def _run(ctx: RunnerContext) -> tuple[str, float, float]:
    before = time.time()
    claude_code.body(ctx)
    return _stdin(ctx), before, time.time()


def _assert_is_the_limit_line(paragraph: str, before: float, after: float) -> None:
    """`paragraph` is time_limit_line's output for this run: 120 minutes, and
    an end time within a few seconds of now + 7200 s."""
    (end_text,) = _END.findall(paragraph)
    end = datetime.strptime(end_text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    assert before + LIMIT_SECONDS - 5 <= end.timestamp() <= after + LIMIT_SECONDS + 5, end_text
    assert "120 minutes" in paragraph, paragraph
    started = end - timedelta(seconds=LIMIT_SECONDS)
    assert paragraph == cliagent.time_limit_line(LIMIT_SECONDS, now=started)


def test_repository_prompt_names_the_limit_before_the_publish_paragraph(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, {"prompt": "fix it"}, repo=True)
    _install(tmp_path, monkeypatch)

    prompt, before, after = _run(ctx)

    paragraphs = prompt.rstrip().split("\n\n")
    assert paragraphs[-1] == cliagent.PUBLISH_PARAGRAPH, prompt[-800:]
    assert prompt.count(cliagent.PUBLISH_PARAGRAPH) == 1
    _assert_is_the_limit_line(paragraphs[-2], before, after)
    assert prompt.count("120 minutes") == 1, "one limit line, not one per paragraph"


def test_prompt_without_repository_names_the_limit(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, {"prompt": "summarise the notes"}, repo=False)
    _install(tmp_path, monkeypatch)

    prompt, before, after = _run(ctx)

    assert prompt.startswith("summarise the notes\n\n"), prompt[:200]
    assert cliagent.PUBLISH_PARAGRAPH not in prompt
    _assert_is_the_limit_line(prompt.rstrip().split("\n\n")[-1], before, after)


def test_resumed_session_is_told_its_own_limit(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, {"prompt": "the original prompt"}, repo=True)
    _install(tmp_path, monkeypatch)
    monkeypatch.setenv(cliagent.RESUME_SESSION_ENV, SESSION)
    monkeypatch.setenv(cliagent.RESUME_REASON_ENV, cliagent.RESUME_MOVED)

    message, before, after = _run(ctx)

    resume = cliagent.resume_prompt(cliagent.RESUME_MOVED)
    assert message.startswith(f"{resume}\n\n"), message[:400]
    _assert_is_the_limit_line(message[len(resume) + 2 :].rstrip(), before, after)
    assert "the original prompt" not in message, "the session already holds it"
    assert cliagent.PUBLISH_PARAGRAPH not in message, "the session already holds it"


def test_limit_line_forbids_background_suites_polled_with_sleep():
    now = datetime(2026, 10, 23, 18, 0, 0, tzinfo=timezone.utc)
    line = cliagent.time_limit_line(LIMIT_SECONDS, now=now)

    assert "120 minutes" in line, line
    assert "2026-10-23T20:00:00Z" in line, line
    # Minutes rounded down: 7259 s is still 120 whole minutes.
    assert "120 minutes" in cliagent.time_limit_line(7259, now=now)
    lowered = line.lower()
    for phrase in ("commit", "pull request's title and body", "before",
                   "whole test suite", "background", "sleep"):
        assert phrase in lowered, phrase
    # A CLI that echoes the line is not misread as a 429 or a dead credential,
    # at any limit a profile is likely to carry.
    markers = (*cliagent._RATE_LIMIT_MARKERS, *cliagent._CREDENTIAL_MARKERS)
    for seconds in (600, 1800, 3600, LIMIT_SECONDS, 4 * 3600, 24 * 3600):
        text = cliagent.time_limit_line(seconds, now=now).lower()
        assert [m for m in markers if m in text] == [], seconds
