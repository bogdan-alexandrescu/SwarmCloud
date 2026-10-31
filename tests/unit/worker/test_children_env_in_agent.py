"""The child-task spool reaches the agent's shell, and the notes name it
(docs/design/lightpanda-runtime.md §6.2, docs/design/child-tasks.md; part of
#2688).

Measured 2026-10-31 on the Lightpanda acceptance run: an agent on
`claude-code-lightpanda` found `SWARM_CHILDREN` empty in its own shell, while
`LIGHTPANDA_NOTE` and the exploration clause both told it to submit children
through `$SWARM_CHILDREN`. The worker sets the variable on the runner only for
an attempt with the child path (`children.ChildPath.prepare`), and the
claude-code runner never handed it to the CLI. It now does, for that attempt
only: a child is never given the variable, because a child may not submit
children (`agent_worker.children`).

MUTATION: drop the `CHILDREN_ENV` pass-through in `claude_code.body`, or the
spool directory's name from either note, and a test below fails.
"""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

import pytest

from agent_worker import browser_tools, children, lightpanda
from agent_worker.runners import claude_code
from agent_worker.runners.base import RunnerContext


class _FakeSupervisor:
    def __init__(self, argv, env, *, log, **_kw):
        self.env = dict(env)

    def start(self):
        pass

    def stop(self):
        pass


def _run(tmp_path: Path, monkeypatch, profile: str, *, spool: str | None) -> dict[str, Any]:
    """`claude_code.body` on `profile`, the CLI faked, with the runner's
    `SWARM_CHILDREN` set to `spool` (None: unset, as for a child); what the
    CLI was handed."""
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
    monkeypatch.setenv(lightpanda.PROFILE_ENV, profile)
    monkeypatch.setattr(claude_code.lightpanda, "Supervisor", _FakeSupervisor)
    monkeypatch.setattr(browser_tools, "write_state", lambda *a, **k: None)
    seen: dict[str, Any] = {}

    def fake_run(run_ctx, spec, *, extra_args=(), extra_env=None):
        seen["prompt"] = run_ctx.payload["prompt"]
        seen["env"] = dict(extra_env or {})
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
    return seen


def _spool(tmp_path: Path) -> str:
    return str(children.ChildPath.spool(tmp_path / "work"))


@pytest.mark.parametrize("profile", ["claude-code", lightpanda.PROFILE])
def test_an_attempt_with_the_child_path_hands_the_cli_its_spool(tmp_path, monkeypatch, profile):
    spool = _spool(tmp_path)
    without = _run(tmp_path, monkeypatch, profile, spool=None)["env"]
    seen = _run(tmp_path, monkeypatch, profile, spool=spool)
    assert seen["env"][children.CHILDREN_ENV] == spool
    # Every other variable of the CLI's environment is unchanged.
    rest = {k: v for k, v in seen["env"].items() if k != children.CHILDREN_ENV}
    assert rest == without


@pytest.mark.parametrize("profile", ["claude-code", lightpanda.PROFILE])
def test_a_child_attempts_cli_has_no_children_spool(tmp_path, monkeypatch, profile):
    seen = _run(tmp_path, monkeypatch, profile, spool=None)
    assert children.CHILDREN_ENV not in seen["env"]
    assert claude_code.EXPLORATION_CHILDREN_CLAUSE not in seen["prompt"]


def test_a_blank_spool_is_not_handed_on(tmp_path, monkeypatch):
    seen = _run(tmp_path, monkeypatch, "claude-code", spool="  ")
    assert children.CHILDREN_ENV not in seen["env"]


@pytest.mark.parametrize("profile", ["claude-code", lightpanda.PROFILE])
def test_every_note_naming_the_variable_names_what_the_agent_has(tmp_path, monkeypatch, profile):
    """A note that sends the agent to `$SWARM_CHILDREN` does so only when the
    CLI's environment carries it, and also names the spool directory itself,
    which is what the prompt's child-tasks line points at."""
    seen = _run(tmp_path, monkeypatch, profile, spool=_spool(tmp_path))
    notes = [claude_code.EXPLORATION_CHILDREN_CLAUSE]
    if profile == lightpanda.PROFILE:
        notes.append(lightpanda.LIGHTPANDA_NOTE)
    for note in notes:
        assert note in seen["prompt"]
        assert f"${children.CHILDREN_ENV}" in note
        assert children.SPOOL_DIR_NAME in note
    assert seen["env"].get(children.CHILDREN_ENV)
