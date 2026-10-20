"""QI-INSTRUCT (docs/design/qa-identity.md §4.7, §6 row 4; part of #1649).

A `claude-code-browser` task whose input names the QA identity has a live
browser that the gate signs in to the console as the QA service account. The
agent is told so in one paragraph after QA-KIT's device and theme lines. The
paragraph says the console is already signed in. It says a hand-off there is
refused (`qa_identity_host`, owner decision Q7, 2026-10-19), and what
`$SWARM_HANDOFF/qa-identity.md` means when it appears. A task that names no
identity, or one the frozen catalogue does not offer, is told nothing of it:
its console is not signed in, and a note saying otherwise would be false.

The design's row names `images/agent-runtime-browser/swarm_browser.py`, but
QA-KIT put the device and theme commands in the claude-code runner's
`LIVE_BROWSER_NOTE`. §4.7 says the text goes where QA-KIT put them, so the
runner is what these tests read. No CLI, no Chromium: the runner's CLI is
replaced, as in test_qa_kit.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_worker import qa_identity
from agent_worker.runners import claude_code
from agent_worker.runners.base import RunnerContext

ENDPOINT = "http://127.0.0.1:9222"


def _prompt(monkeypatch, tmp_path: Path, payload: dict[str, Any], *, live: bool = True) -> str:
    seen: dict[str, Any] = {}

    def fake_run(ctx, spec, *, extra_args=(), extra_env=None):
        seen["prompt"] = ctx.payload["prompt"]
        return {"summary": ""}

    monkeypatch.setattr(claude_code, "run_cli_agent", fake_run)
    for name in (claude_code.GIT_SPOOL_ENV, claude_code.SEARCH_SPOOL_ENV, claude_code.HANDOFF_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(claude_code.BROWSERS_PATH_ENV, "/opt/playwright")
    if live:
        monkeypatch.setenv(claude_code.CDP_ENDPOINT_ENV, ENDPOINT)
    else:
        monkeypatch.delenv(claude_code.CDP_ENDPOINT_ENV, raising=False)
    ctx = RunnerContext(
        work_dir=tmp_path,
        artifacts_dir=tmp_path / "artifacts",
        input_path=tmp_path / "input.json",
        result_path=tmp_path / "result.json",
        quota_path=tmp_path / "quota.json",
        payload={"prompt": "Sweep the Overview at 390 and 1440, light and dark.", **payload},
    )
    claude_code.body(ctx)
    return seen["prompt"]


def test_the_catalogue_offers_the_console_identity_the_note_is_written_for():
    assert qa_identity.IDENTITIES == ("console",)


def test_the_note_says_the_console_is_signed_in_as_the_qa_identity():
    note = claude_code.QA_IDENTITY_NOTE
    assert "already signed in to the SwarmCloud console as its QA identity" in note
    assert "Google sign-in" in note


def test_the_note_forbids_a_hand_off_on_the_console_and_names_the_refusal():
    note = claude_code.QA_IDENTITY_NOTE
    assert "Do not run `swarm-handoff request` for a console page" in note
    assert "qa_identity_host" in note
    assert "never type a password" in note


def test_the_note_says_what_the_notice_means_when_it_appears():
    note = claude_code.QA_IDENTITY_NOTE
    assert f"$SWARM_HANDOFF/{qa_identity.NOTICE_NAME}" in note
    assert "signed out" in note


def test_a_task_naming_the_identity_is_told_after_the_device_and_theme_lines(monkeypatch, tmp_path):
    prompt = _prompt(monkeypatch, tmp_path, {qa_identity.INPUT_KEY: "console"})
    assert claude_code.LIVE_BROWSER_NOTE in prompt
    assert claude_code.QA_IDENTITY_NOTE in prompt
    assert prompt.index(claude_code.QA_IDENTITY_NOTE) > prompt.index("swarm-handoff theme dark|light|system")


@pytest.mark.parametrize("payload", [
    {},
    {qa_identity.INPUT_KEY: "admin"},
    {qa_identity.INPUT_KEY: ""},
    {qa_identity.INPUT_KEY: ["console"]},
], ids=["absent", "off-the-catalogue", "empty", "not-a-string"])
def test_a_task_that_names_no_offered_identity_is_not_told_its_console_is_signed_in(
        monkeypatch, tmp_path, payload):
    prompt = _prompt(monkeypatch, tmp_path, payload)
    assert claude_code.LIVE_BROWSER_NOTE in prompt
    assert claude_code.QA_IDENTITY_NOTE not in prompt
    assert "QA identity" not in prompt


def test_the_always_shown_live_browser_note_claims_no_sign_in():
    assert "QA identity" not in claude_code.LIVE_BROWSER_NOTE
    assert "signed in" not in claude_code.LIVE_BROWSER_NOTE


def test_without_the_live_browser_the_note_is_not_given(monkeypatch, tmp_path):
    prompt = _prompt(monkeypatch, tmp_path, {qa_identity.INPUT_KEY: "console"}, live=False)
    assert "QA identity" not in prompt


def test_qa_kits_device_and_theme_lines_are_unchanged():
    note = claude_code.LIVE_BROWSER_NOTE
    assert "swarm-handoff device <preset>" in note
    assert "swarm-handoff theme dark|light|system" in note
    assert "browser_resize is refused" in note


def test_the_note_is_one_paragraph():
    assert "\n" not in claude_code.QA_IDENTITY_NOTE
