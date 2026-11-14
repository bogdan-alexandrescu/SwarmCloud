"""Each platform instruction is in a first message once, and only where it applies (ONE-PLACE).

The owner asked on 3 November 2026 for prompts without bloat or repeated
paragraphs. Measured in experiment batches one and two: task text was 40 to
44% of a build step's first message and 30 to 33% of a rendered one; the
live-browser note alone was 4,754 characters; "related tests only" was said
four times; the publishing paragraph asked for pull request files in steps
that publish nothing (written in 88 of 90 steps); swarm-git and swarm-state
(22 tools) were registered in 91 steps and never called; Lightpanda's 29
tools were registered on build steps that never rendered; and 37 oversized
shell results the CLI had saved to files were read back whole (about 175,000
tokens).

So, held here against the real runner and a fake CLI that records its stdin:

* a step the worker says publishes nothing (`SWARM_STEP_PUBLISHES=0`) gets no
  PUBLISH_PARAGRAPH, and so no `pr-title.txt`; one that publishes, or a worker
  that does not say, keeps it, once;
* no sentence, and none of the instructions measured as repeated, appears
  twice in any step kind's first message: a build step, a review, a task with
  no repository, a rendered step's notes;
* the live-browser note is under 3,000 characters;
* swarm-git and swarm-state are registered only when the worker's per-step
  list names them (`SWARM_STEP_MCP_SERVERS`; swarm-git also on a live-browser
  step, whose screen review pushes first), every offered one when the worker
  sets no list, and Lightpanda's server only on a step that renders a page;
* every claude-code start is told to read a saved oversized output by window.

MUTATIONS: drop `step_publishes` from `run_cli_agent` (the publishes-nothing
test fails); restore "Commit your work and write the pull request's title and
body" or "the tests related to your change" in `time_limit_line`, "never
background a test run" in `budget_line`, or the path in
EXPLORATION_CHILDREN_CLAUSE (the once tests fail); register the git or state
shim whatever the worker's list says, or Lightpanda on every step of its profile (the
server tests fail); stop appending SAVED_OUTPUT_NOTE (its test fails).
"""

from __future__ import annotations

import json
import re
import secrets as pysecrets
from pathlib import Path
from typing import Any

import pytest

from agent_worker import browser_tools, lightpanda, stepservers
from agent_worker.runners import claude_code, cliagent
from agent_worker.runners.base import RunnerContext
from agent_worker.runners.limits import TIMEOUT_ENV

SESSION = "sess-" + "c3d4e5f607182930"

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

#: Phrases that each carry one instruction measured as repeated: each may be
#: in a first message at most once.
ONCE = (
    "pr-title.txt",
    "tests related to",
    "in the background",
    "sleep",
    "($SWARM_CHILDREN)",
    "Commit your work",
    "verification.md",
    "WebSearch",
    "get around",
    "$SWARM_ARTIFACTS_DIR/screenshots",
)


def _key() -> str:
    # Built at runtime: no credential-shaped literal in this file.
    return "sk-ant-" + "api03-" + pysecrets.token_hex(16)


def _ctx(tmp_path: Path, payload: dict[str, Any], *, repo: bool = True) -> RunnerContext:
    work = tmp_path / "work"
    artifacts = tmp_path / "artifacts"
    work.mkdir(parents=True, exist_ok=True)
    artifacts.mkdir(parents=True, exist_ok=True)
    checkout = None
    if repo:
        checkout = work / "repo"
        (checkout / "scripts").mkdir(parents=True)
        (checkout / claude_code.RELATED_TESTS_SCRIPT).write_text("#!/bin/sh\n")
    return RunnerContext(
        work_dir=work, artifacts_dir=artifacts, input_path=work / "input.json",
        result_path=work / "result.json", quota_path=work / "quota.json",
        payload=payload, repo_dir=checkout,
    )


def _install(tmp_path: Path, monkeypatch, *, publishes: str | None,
             servers: str | None = stepservers.NONE) -> None:
    cli = tmp_path / "fake-cli"
    cli.write_text(FAKE_CLI)
    cli.chmod(0o755)
    monkeypatch.setenv("CLAUDE_CODE_BIN", str(cli))
    for name in ("CLAUDE_CODE_ARGS", "CLAUDE_CODE_OAUTH_TOKEN", "MODEL",
                 cliagent.ACCOUNT_STREAM_ENV, cliagent.RESUME_SESSION_ENV,
                 cliagent.RESUME_REASON_ENV, claude_code.HANDOFF_ENV,
                 *claude_code.AGENT_PASSTHROUGH_ENV, claude_code.BROWSERS_PATH_ENV,
                 claude_code.CDP_ENDPOINT_ENV, claude_code.SEARCH_SPOOL_ENV,
                 claude_code.MCP_RELAY_ENV, lightpanda.PROFILE_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", _key())
    monkeypatch.setenv(TIMEOUT_ENV, "7200")
    # What a SwarmCloud attempt has: the child path, the git tools and the
    # state reads offered, and the cache volume's TMPDIR.
    work = tmp_path / "work"
    monkeypatch.setenv(claude_code.CHILDREN_ENV, str(work / ".swarm-children"))
    monkeypatch.setenv(claude_code.GIT_SPOOL_ENV, str(work / ".swarm-git"))
    monkeypatch.setenv(claude_code.STATE_SPOOL_ENV, str(work / ".swarm-state"))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "cache"))
    monkeypatch.setenv("SWARM_WORKSPACE", str(tmp_path))
    if publishes is None:
        monkeypatch.delenv(cliagent.STEP_PUBLISHES_ENV, raising=False)
    else:
        monkeypatch.setenv(cliagent.STEP_PUBLISHES_ENV, publishes)
    if servers is None:
        monkeypatch.delenv(stepservers.ENV, raising=False)
    else:
        monkeypatch.setenv(stepservers.ENV, servers)


def _first_start(ctx: RunnerContext) -> dict[str, Any]:
    claude_code.body(ctx)
    runs = [json.loads(line) for line in
            (ctx.work_dir / "runs.jsonl").read_text().splitlines() if line.strip()]
    return runs[0]


def _first_message(tmp_path, monkeypatch, payload, *, repo=True, publishes=None) -> str:
    _install(tmp_path, monkeypatch, publishes=publishes)
    return _first_start(_ctx(tmp_path, payload, repo=repo))["stdin"]


def _servers(run: dict[str, Any]) -> dict[str, Any]:
    argv = run["argv"]
    if "--mcp-config" not in argv:
        return {}
    return json.loads(Path(argv[argv.index("--mcp-config") + 1]).read_text())["mcpServers"]


def _sentences(text: str) -> list[str]:
    """The message's sentences, normalised, with paths and times left out of
    what is compared, so one sentence said twice is one string twice."""
    pieces = re.split(r"(?<=[.;:])\s+|\n+", text)
    return [re.sub(r"\s+", " ", p).strip().lower() for p in pieces if len(p.strip()) > 24]


def _assert_said_once(text: str) -> None:
    sentences = _sentences(text)
    twice = sorted({s for s in sentences if sentences.count(s) > 1})
    assert twice == [], twice
    for phrase in ONCE:
        assert text.count(phrase) <= 1, (phrase, text.count(phrase))


# --- the publishing paragraph, only where the step publishes ----------------

def test_a_step_that_publishes_nothing_gets_no_publishing_paragraph(tmp_path, monkeypatch):
    prompt = _first_message(tmp_path, monkeypatch, {"prompt": BRIEF}, publishes="0")
    assert prompt.startswith(BRIEF)
    assert cliagent.PUBLISH_PARAGRAPH not in prompt, prompt[-1200:]
    assert "pr-title.txt" not in prompt and "pr-body.md" not in prompt
    assert "pull request" not in prompt, prompt[-1200:]


@pytest.mark.parametrize("publishes", [None, "1"])
def test_a_publishing_step_keeps_the_paragraph_once(tmp_path, monkeypatch, publishes):
    prompt = _first_message(tmp_path, monkeypatch, {"prompt": BRIEF}, publishes=publishes)
    assert prompt.rstrip().endswith(cliagent.PUBLISH_PARAGRAPH)
    assert prompt.count("pr-title.txt") == 1


def test_step_publishes_is_false_only_for_zero():
    assert cliagent.step_publishes({}) is True
    assert cliagent.step_publishes({cliagent.STEP_PUBLISHES_ENV: "1"}) is True
    assert cliagent.step_publishes({cliagent.STEP_PUBLISHES_ENV: " 0 "}) is False


# --- no instruction twice, in any step kind ---------------------------------

@pytest.mark.parametrize("kind", ["build", "review", "no-repository"])
def test_no_instruction_appears_twice_in_a_first_message(tmp_path, monkeypatch, kind):
    payload: dict[str, Any] = {"prompt": BRIEF}
    if kind == "review":
        payload["expected_outputs"] = ["verdict.json"]
    prompt = _first_message(tmp_path, monkeypatch, payload, repo=kind != "no-repository",
                            publishes="0" if kind == "review" else None)
    _assert_said_once(prompt[len(BRIEF):])
    # The Jev lines only when Jev is on: this attempt has no judge config.
    assert "Jev" not in prompt
    if kind != "no-repository":
        assert prompt.count(claude_code.RELATED_TESTS_NOTE) == 1


@pytest.mark.parametrize("selected", sorted(browser_tools.SELECTIONS))
@pytest.mark.parametrize("search", [False, True])
def test_no_instruction_appears_twice_in_a_rendered_steps_notes(selected, search):
    choice = browser_tools.choose(selected, jev_installed=True, jev_key=True)
    note = claude_code.live_browser_note(choice, search=search)
    rendered = "\n\n".join((claude_code.RELATED_TESTS_NOTE, claude_code.SAVED_OUTPUT_NOTE,
                            note, cliagent.time_limit_line(3600), cliagent.PUBLISH_PARAGRAPH))
    _assert_said_once(rendered)


def test_the_live_browser_note_says_the_dev_server_shares_the_workers_memory():
    # #3104: a live-browser worker running the CLI, `npm ci` and a dev server
    # was OOM-killed, the worker with it. The browser has its own container;
    # the dev server does not. MUTATION: drop the sentence and this fails.
    note = claude_code.LIVE_BROWSER_NOTE
    assert "dev server" in note
    assert "shares its memory limit" in note
    assert "NODE_OPTIONS=--max-old-space-size=" in note


def test_the_live_browser_note_is_short():
    # Was 3,874 characters alone, 4,754 with its lead, strengths and test note.
    assert len(claude_code.LIVE_BROWSER_NOTE) < 3000, len(claude_code.LIVE_BROWSER_NOTE)


# --- saved oversized outputs are read by window -----------------------------

def test_every_start_is_told_to_read_a_saved_output_by_window(tmp_path, monkeypatch):
    prompt = _first_message(tmp_path, monkeypatch, {"prompt": BRIEF})
    assert prompt.count(claude_code.SAVED_OUTPUT_NOTE) == 1
    assert "by window" in claude_code.SAVED_OUTPUT_NOTE
    assert "offset" in claude_code.SAVED_OUTPUT_NOTE and "never whole" in claude_code.SAVED_OUTPUT_NOTE


# --- servers only for the steps that use them -------------------------------

def test_a_build_step_registers_neither_the_git_nor_the_state_tools(tmp_path, monkeypatch):
    _install(tmp_path, monkeypatch, publishes=None)
    run = _first_start(_ctx(tmp_path, {"prompt": BRIEF}))
    servers = _servers(run)
    assert claude_code.GIT_MCP_SERVER_NAME not in servers
    assert claude_code.STATE_MCP_SERVER_NAME not in servers


@pytest.mark.parametrize("handed, git, state", [
    ("swarm-git", True, False),
    ("swarm-state", False, True),
    ("swarm-git,swarm-state", True, True),
])
def test_the_workers_list_registers_what_it_names(tmp_path, monkeypatch, handed, git, state):
    # The brief names neither: the worker's list, not the brief, decides.
    _install(tmp_path, monkeypatch, publishes=None, servers=handed)
    servers = _servers(_first_start(_ctx(tmp_path, {"prompt": BRIEF})))
    assert (claude_code.GIT_MCP_SERVER_NAME in servers) is git
    assert (claude_code.STATE_MCP_SERVER_NAME in servers) is state


def test_a_brief_that_names_the_tools_does_not_register_them(tmp_path, monkeypatch):
    _install(tmp_path, monkeypatch, publishes=None)
    servers = _servers(_first_start(_ctx(tmp_path, {"prompt": "Use swarm-state and pr_checks."})))
    assert claude_code.GIT_MCP_SERVER_NAME not in servers
    assert claude_code.STATE_MCP_SERVER_NAME not in servers


def test_no_list_keeps_every_offered_server(tmp_path, monkeypatch):
    # A worker that predates the variable: the attempt keeps what it was offered.
    _install(tmp_path, monkeypatch, publishes=None, servers=None)
    servers = _servers(_first_start(_ctx(tmp_path, {"prompt": BRIEF})))
    assert claude_code.GIT_MCP_SERVER_NAME in servers
    assert claude_code.STATE_MCP_SERVER_NAME in servers


def test_the_spools_stay_exported_for_the_bash_tool(tmp_path, monkeypatch):
    seen: dict[str, Any] = {}

    def fake_run(ctx, spec, *, extra_args=(), extra_env=None):
        seen["env"] = dict(extra_env or {})
        return {}

    _install(tmp_path, monkeypatch, publishes=None)
    monkeypatch.setattr(claude_code, "run_cli_agent", fake_run)
    claude_code.body(_ctx(tmp_path, {"prompt": BRIEF}))
    assert seen["env"][claude_code.GIT_SPOOL_ENV].endswith(".swarm-git")
    assert seen["env"][claude_code.STATE_SPOOL_ENV].endswith(".swarm-state")


class _Supervisor:
    def __init__(self, *_a, **_k):
        pass

    def start(self):
        pass

    def stop(self):
        pass


@pytest.mark.parametrize("brief, renders", [
    (BRIEF, False),
    ("Open https://example.org/docs and list its sections.", True),
    ("Check the rendered settings screen at both themes.", True),
])
def test_lightpanda_is_registered_only_on_a_step_that_renders(tmp_path, monkeypatch, brief,
                                                              renders):
    seen: dict[str, Any] = {}

    def fake_run(ctx, spec, *, extra_args=(), extra_env=None):
        seen["prompt"] = ctx.payload["prompt"]
        seen["argv"] = list(extra_args)
        return {}

    _install(tmp_path, monkeypatch, publishes=None)
    monkeypatch.setenv(lightpanda.PROFILE_ENV, lightpanda.PROFILE)
    monkeypatch.setenv(claude_code.CACHE_HOME_ENV, str(tmp_path / "xdg"))
    monkeypatch.setattr(claude_code.lightpanda, "Supervisor", _Supervisor)
    monkeypatch.setattr(claude_code, "run_cli_agent", fake_run)
    claude_code.body(_ctx(tmp_path, {"prompt": brief}))
    servers = _servers({"argv": seen["argv"]})
    assert (lightpanda.MCP_SERVER_NAME in servers) is renders
    assert ("Lightpanda" in seen["prompt"]) is renders
