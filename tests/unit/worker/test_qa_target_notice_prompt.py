"""The claude-code runner puts the QA target's notice in the agent's prompt
(VQ4-WIRE, part of #1933; BGD, 2026-10-22: "Wire it + prompt notice").

The worker writes `$SWARM_HANDOFF/qa-target.md` (`qa_target.NOTICE_NAME`)
when the gate could not sign the agent in to its QA target: a pending host,
a challenge, a credential not set. The runner reads it into the prompt the
way it reads `egress.md`, so the agent learns at start why it is signed
out. The notice names a cause, never the credential, and the runner reads
only the spool file, never the secret.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_worker import handoff, qa_target
from agent_worker.runners import claude_code
from agent_worker.runners.base import RunnerContext


def _prompt(monkeypatch, tmp_path: Path, spool: Path) -> str:
    seen: dict[str, Any] = {}

    def fake_run(ctx, spec, *, extra_args=(), extra_env=None):
        seen["prompt"] = ctx.payload["prompt"]
        return {}

    monkeypatch.setattr(claude_code, "run_cli_agent", fake_run)
    monkeypatch.setenv(handoff.HANDOFF_ENV, str(spool))
    # A live-browser attempt reaches the runner only with its browser (BROWSER-START).
    monkeypatch.setenv(claude_code.CDP_ENDPOINT_ENV, "http://127.0.0.1:9223")
    monkeypatch.setenv(claude_code.BROWSERS_PATH_ENV, "/opt/playwright")
    ctx = RunnerContext(
        work_dir=tmp_path, artifacts_dir=tmp_path / "artifacts", input_path=tmp_path / "input.json",
        result_path=tmp_path / "result.json", quota_path=tmp_path / "quota.json",
        payload={"prompt": "Check the account page."},
    )
    claude_code.body(ctx)
    return seen["prompt"]


def test_the_qa_target_notice_is_in_the_prompt_when_the_spool_has_it(monkeypatch, tmp_path):
    spool = tmp_path / handoff.SPOOL_DIR_NAME
    spool.mkdir()
    text = qa_target.notice(qa_target.CHALLENGE)
    (spool / qa_target.NOTICE_NAME).write_text(text)
    prompt = _prompt(monkeypatch, tmp_path, spool)
    assert prompt.startswith("Check the account page.")
    # MUTATION: drop the `qa_target_notice` block from `body` -> the notice
    # is not in the prompt.
    assert text.strip() in prompt
    # MUTATION: read `egress.md` in `qa_target_notice` -> None here.
    assert claude_code.qa_target_notice(str(spool)) == text.strip()


def test_no_notice_in_the_spool_puts_nothing_in_the_prompt(monkeypatch, tmp_path):
    spool = tmp_path / handoff.SPOOL_DIR_NAME
    spool.mkdir()
    prompt = _prompt(monkeypatch, tmp_path, spool)
    # MUTATION: append a fixed "signed out" sentence whatever the spool holds
    # -> its words are in the prompt.
    assert "QA target" not in prompt and qa_target.NOTICE_NAME not in prompt
    assert claude_code.qa_target_notice(str(spool)) is None
    # MUTATION: drop `_spool_text`'s empty-spool check -> reads `./qa-target.md`.
    assert claude_code.qa_target_notice("") is None
