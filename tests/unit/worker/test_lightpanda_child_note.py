"""A `claude-code-lightpanda` child is never told to submit a child
(docs/design/lightpanda-runtime.md §6.2; part of #2688).

A child attempt is never given the child path (`agent_worker.children`: a
child may not submit children), so `SWARM_CHILDREN` is unset in its shell. The
Lightpanda note told every attempt to hand a failing page to a
`claude-code-browser` child through `.swarm-children ($SWARM_CHILDREN)`, which a
child cannot do. The owner-side answer of 2026-10-31 ("variant"): an attempt
without the child path lists, in its answer, each page that failed and why, for
the parent to route to a live-browser child. Every other sentence of the note is
the same for both.

MUTATION: choose `LIGHTPANDA_NOTE` whatever the child path in
`claude_code.body`, or drop the list-the-pages sentence from
`lightpanda.LIGHTPANDA_CHILD_NOTE`, and a test below fails.
"""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

from agent_worker import browser_tools, children, lightpanda
from agent_worker.runners import claude_code
from agent_worker.runners.base import RunnerContext

LIST_THE_PAGES = "list in your answer each page that failed and why"


class _FakeSupervisor:
    def __init__(self, argv, env, *, log, **_kw):
        self.env = dict(env)

    def start(self):
        pass

    def stop(self):
        pass


def _prompt(tmp_path: Path, monkeypatch, *, spool: str | None) -> str:
    """The prompt `claude_code.body` hands the faked CLI on
    `claude-code-lightpanda`, with the runner's `SWARM_CHILDREN` set to `spool`
    (None: unset, as for a child attempt)."""
    for name in (claude_code.HANDOFF_ENV, *claude_code.AGENT_PASSTHROUGH_ENV,
                 claude_code.BROWSERS_PATH_ENV, claude_code.GIT_SPOOL_ENV,
                 claude_code.SEARCH_SPOOL_ENV, claude_code.STATE_SPOOL_ENV,
                 claude_code.MCP_RELAY_ENV, claude_code.CDP_ENDPOINT_ENV,
                 claude_code.CHILDREN_ENV):
        monkeypatch.delenv(name, raising=False)
    if spool is not None:
        monkeypatch.setenv(claude_code.CHILDREN_ENV, spool)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", secrets.token_hex(16))
    monkeypatch.setenv(claude_code.CACHE_HOME_ENV, str(tmp_path / "cache"))
    monkeypatch.setenv(lightpanda.PROFILE_ENV, lightpanda.PROFILE)
    monkeypatch.setattr(claude_code.lightpanda, "Supervisor", _FakeSupervisor)
    monkeypatch.setattr(browser_tools, "write_state", lambda *a, **k: None)
    seen: dict[str, Any] = {}

    def fake_run(run_ctx, spec, *, extra_args=(), extra_env=None):
        seen["prompt"] = run_ctx.payload["prompt"]
        return {"summary": "ok"}

    monkeypatch.setattr(claude_code, "run_cli_agent", fake_run)
    work = tmp_path / "work"
    work.mkdir(parents=True, exist_ok=True)
    (tmp_path / "artifacts").mkdir(exist_ok=True)
    claude_code.body(RunnerContext(
        work_dir=work, artifacts_dir=tmp_path / "artifacts", input_path=work / "input.json",
        result_path=work / "result.json", quota_path=work / "quota.json",
        payload={"prompt": "Read three pages."},
    ))
    return seen["prompt"]


def test_a_lightpanda_child_is_told_to_list_the_failed_pages_and_not_to_submit(tmp_path, monkeypatch):
    prompt = _prompt(tmp_path, monkeypatch, spool=None)
    assert LIST_THE_PAGES in prompt
    assert "submit a child agent" not in prompt
    assert children.SPOOL_DIR_NAME not in prompt
    assert f"${children.CHILDREN_ENV}" not in prompt
    assert lightpanda.LIGHTPANDA_NOTE not in prompt


def test_a_lightpanda_attempt_with_the_child_path_still_submits(tmp_path, monkeypatch):
    spool = str(children.ChildPath.spool(tmp_path / "work"))
    prompt = _prompt(tmp_path, monkeypatch, spool=spool)
    assert lightpanda.LIGHTPANDA_NOTE in prompt
    assert "submit a child agent on runner_profile `claude-code-browser`" in prompt
    assert LIST_THE_PAGES not in prompt


def test_the_two_notes_differ_only_in_the_failed_page_sentence():
    with_path = lightpanda.lightpanda_note(children=True)
    without = lightpanda.lightpanda_note(children=False)
    assert with_path == lightpanda.LIGHTPANDA_NOTE
    assert without == lightpanda.LIGHTPANDA_CHILD_NOTE
    head = with_path[:with_path.index("When a page fails here")]
    tail = with_path[with_path.index("On any one site"):]
    assert without.startswith(head) and without.endswith(tail)
    assert LIST_THE_PAGES in without
    assert "live-browser child" in without
