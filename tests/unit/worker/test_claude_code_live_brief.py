"""A claude-code-browser agent works IN the live browser, and asks a person at a wall.

BROWSER-BRIEF (part of #1030). MEASURED 2026-10-13 on task_7cda0d5335f049b4b442
(attempt att_53c1e5962b094cd4900a): asked to open ten Google results in tabs,
the agent wrote Playwright that called `p.chromium.launch(headless=True, ...)`
-- its own hidden browser -- so the live view the person watched stayed blank;
plain `python3` had no Playwright (only /opt/venv/bin/python did, and the
CLI's PATH did not name it); and when Google answered with its CAPTCHA the
agent silently switched to Bing. docs/design/live-browser.md §3.2 said "the
profile's agent instructions point Playwright at the CDP endpoint"; nothing
did. The runner now appends those instructions to the system prompt for a
profile whose `live_browser` flag is set, and only for it, and puts the
image's venv first on the agent's PATH.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_worker.runners import claude_code, cliagent
from agent_worker.runners.base import RunnerContext

REPO = Path(__file__).resolve().parents[3]
DESIGN = REPO / "docs" / "design" / "live-browser.md"
LIVE = "claude-code-browser"


def _ctx(tmp_path: Path) -> RunnerContext:
    return RunnerContext(
        work_dir=tmp_path,
        artifacts_dir=tmp_path / "artifacts",
        input_path=tmp_path / "input.json",
        result_path=tmp_path / "result.json",
        quota_path=tmp_path / "quota.json",
        payload={"prompt": "Open the top 10 results, each in its own tab."},
    )


def _body(monkeypatch, tmp_path, profile: str | None, browsers: str | None = "/opt/playwright"):
    seen: dict[str, Any] = {}

    def fake_run(ctx, spec, *, extra_args=(), extra_env=None, path_prefix=()):
        seen["args"] = list(extra_args)
        seen["prompt"] = ctx.payload["prompt"]
        seen["path_prefix"] = tuple(path_prefix)
        return {}

    monkeypatch.setattr(claude_code, "run_cli_agent", fake_run)
    if profile is None:
        monkeypatch.delenv(claude_code.PROFILE_ENV, raising=False)
    else:
        monkeypatch.setenv(claude_code.PROFILE_ENV, profile)
    if browsers is None:
        monkeypatch.delenv(claude_code.BROWSERS_PATH_ENV, raising=False)
    else:
        monkeypatch.setenv(claude_code.BROWSERS_PATH_ENV, browsers)
    claude_code.body(_ctx(tmp_path))
    return seen


def _appended(args: list[str]) -> str | None:
    if "--append-system-prompt" not in args:
        return None
    return args[args.index("--append-system-prompt") + 1]


def test_the_live_profile_is_the_one_with_the_flag():
    from swarm_common.profiles import RUNNER_PROFILES

    assert RUNNER_PROFILES[LIVE].live_browser is True
    assert RUNNER_PROFILES["claude-code"].live_browser is False


def test_a_live_browser_agent_is_told_to_work_in_the_watched_browser(monkeypatch, tmp_path):
    seen = _body(monkeypatch, tmp_path, LIVE)
    brief = _appended(seen["args"])
    assert brief == claude_code.LIVE_BROWSER_BRIEF
    # Connect to the running browser, in its live profile, one visible tab each.
    for needed in (
        'connect_over_cdp(os.environ["SWARM_CDP_ENDPOINT"])',
        "browser.contexts[0]",
        "new_page()",
        "bring_to_front()",
        "python3",
    ):
        assert needed in brief, needed
    # Never its own browser.
    assert "launch()" in brief and "launch_persistent_context()" in brief
    assert "never" in brief.lower()
    # A wall is a person's job, and a silent switch of site is not allowed.
    assert "swarm-handoff request --reason" in brief
    assert "--expect" in brief
    assert "swarm-handoff wait" in brief
    assert "CAPTCHA" in brief
    assert "different site" in brief


def test_the_live_agent_is_not_also_told_to_launch_a_headless_browser(monkeypatch, tmp_path):
    """The browser image's headless note says `p.chromium.launch(...)` -- the
    exact call that left the live view blank. A live agent must not read both."""
    seen = _body(monkeypatch, tmp_path, LIVE)
    assert claude_code.BROWSER_NOTE not in seen["prompt"]
    assert "chromium.launch(" not in seen["prompt"]
    assert seen["prompt"] == "Open the top 10 results, each in its own tab."


@pytest.mark.parametrize("profile", ["claude-code", None])
def test_a_browserless_profile_gets_no_live_brief(monkeypatch, tmp_path, profile):
    seen = _body(monkeypatch, tmp_path, profile, browsers=None)
    assert _appended(seen["args"]) is None
    assert not any("SWARM_CDP_ENDPOINT" in arg for arg in seen["args"])
    assert seen["path_prefix"] == ()


def test_the_brief_stays_short():
    """It is in the system prompt, so every turn pays for it."""
    assert len(claude_code.LIVE_BROWSER_BRIEF) < 1800


def test_the_brief_comes_after_the_platform_flags_and_survives_a_replaced_flag_set(
    monkeypatch, tmp_path
):
    monkeypatch.setenv(claude_code.SPEC.args_env, "--print --output-format stream-json --verbose")
    seen = _body(monkeypatch, tmp_path, LIVE)
    assert _appended(seen["args"]) == claude_code.LIVE_BROWSER_BRIEF


# --- PATH ----------------------------------------------------------------------


def _launched_env(monkeypatch, tmp_path, profile: str) -> dict[str, str]:
    monkeypatch.setenv(claude_code.PROFILE_ENV, profile)
    monkeypatch.setenv(claude_code.SPEC.key_env, "sk-ant-" + "x" * 40)
    launched: dict[str, Any] = {}

    class Stop(Exception):
        pass

    def fake_child(argv, *, env, **kwargs):
        launched["argv"] = list(argv)
        launched.update(env)
        raise Stop

    monkeypatch.setattr(cliagent, "run_child", fake_child)
    binary = tmp_path / "claude"
    binary.write_text("")
    monkeypatch.setenv(claude_code.SPEC.binary_env, str(binary))
    (tmp_path / "artifacts").mkdir()
    with pytest.raises(Stop):
        claude_code.body(_ctx(tmp_path))
    return launched


def test_the_live_agents_python3_is_the_venv_that_has_playwright(monkeypatch, tmp_path):
    env = _launched_env(monkeypatch, tmp_path, LIVE)
    entries = env["PATH"].split(":")
    assert entries[0] == "/opt/venv/bin"
    assert entries[1:] == cliagent.BASE_PATH.split(":")
    # And the brief reaches the CLI's own argv.
    assert claude_code.LIVE_BROWSER_BRIEF in env["argv"]


def test_a_browserless_agents_path_is_unchanged(monkeypatch, tmp_path):
    env = _launched_env(monkeypatch, tmp_path, "claude-code")
    assert env["PATH"] == cliagent.BASE_PATH
    assert "--append-system-prompt" not in env["argv"]


# --- the design doc ------------------------------------------------------------


def test_the_design_names_what_was_built():
    text = DESIGN.read_text(encoding="utf-8")
    section = text.split("### 3.2", 1)[1].split("### 3.3", 1)[0]
    assert "LIVE_BROWSER_BRIEF" in section
    assert "apps/agent-worker/agent_worker/runners/claude_code.py" in section
    assert "--append-system-prompt" in section
    assert "/opt/venv/bin" in section
