"""The claude-code runner tells the agent to run only the tests its change
reaches (RELATED-TESTS, part of #1694, owner decision 2026-10-20).

Measured 2026-10-19/20: 36 vitest runs with no file filter (~22 min each,
2.05 h), 8 pytest runs over whole tests/unit directories (0.73 h), and both
out-of-memory kills from `npx vitest run --shard=$i/4 &` in a loop. So when
the checkout carries `scripts/related-tests.sh`, the runner adds the standing
rule to the agent's prompt: related tests through that script, never vitest
without file arguments, never pytest over a directory or `-n auto` across the
suite, never parallel shards or background batches polled with sleep. A
checkout without the script (another tenant's repository) gets no note naming
a script it does not have.

The guards (owner decision 2026-11-01, #2981). The note once ended "run
scripts/changed-guards.sh once at the end". This runner has the pre-publish
gate (`prepublish=True`), which runs that script after the agent's turn and
hands back what fails in a fix turn; on a large patch the agent's own run hit
its 10-minute command limit (about ten minutes a step) and the gate ran the
same guards again. So the note now says the worker runs it, and no longer
tells the agent to.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_worker.runners import claude_code
from agent_worker.runners.base import RunnerContext


def _ctx(root: Path, *, helper: bool) -> RunnerContext:
    work = root / "work"
    artifacts = root / "artifacts"
    checkout = work / "repo"
    (checkout / "scripts").mkdir(parents=True, exist_ok=True)
    artifacts.mkdir(parents=True, exist_ok=True)
    if helper:
        (checkout / claude_code.RELATED_TESTS_SCRIPT).write_text("#!/usr/bin/env bash\n")
    return RunnerContext(
        work_dir=work,
        artifacts_dir=artifacts,
        input_path=work / "input.json",
        result_path=work / "result.json",
        quota_path=work / "quota.json",
        payload={"prompt": "Fix the thing."},
        repo_dir=checkout,
    )


def _prompt_given_to_the_cli(ctx: RunnerContext, monkeypatch) -> str:
    seen: dict[str, str] = {}

    def fake_run(run_ctx, spec, *, extra_args=(), extra_env=None):
        seen["prompt"] = run_ctx.payload["prompt"]
        return {"summary": "ok"}

    monkeypatch.setattr(claude_code, "run_cli_agent", fake_run)
    for name in (claude_code.HANDOFF_ENV, *claude_code.AGENT_PASSTHROUGH_ENV,
                 claude_code.BROWSERS_PATH_ENV, claude_code.CDP_ENDPOINT_ENV):
        monkeypatch.delenv(name, raising=False)
    claude_code.body(ctx)
    return seen["prompt"]


@pytest.mark.parametrize("rule", [
    "scripts/related-tests.sh",
    "never run vitest without file arguments",
    "never run pytest over a whole directory",
    "-n auto",
    "never start parallel shards",
    "sleep",
    "Do not run scripts/changed-guards.sh yourself: the worker's pre-publish "
    "gate runs it after your turn and hands you back what fails",
])
def test_the_rule_text_is_staged_in_the_agents_prompt(tmp_path, monkeypatch, rule):
    prompt = _prompt_given_to_the_cli(_ctx(tmp_path, helper=True), monkeypatch)
    # MUTATION: stop appending RELATED_TESTS_NOTE in `body`, or drop this
    # clause from the note -> this fails.
    assert prompt.startswith("Fix the thing.")
    assert rule in prompt


def test_the_note_no_longer_tells_the_agent_to_run_the_guards(tmp_path, monkeypatch):
    prompt = _prompt_given_to_the_cli(_ctx(tmp_path, helper=True), monkeypatch)
    # MUTATION: restore "Run scripts/changed-guards.sh once at the end" -> this
    # fails (#2981: the gate runs it after the turn; the agent's own run was
    # a second, ten-minute run of the same guards).
    assert "changed-guards.sh once at the end" not in prompt
    assert "Run scripts/changed-guards.sh" not in prompt


def test_the_note_names_the_helper_only_where_the_checkout_has_it(tmp_path, monkeypatch):
    prompt = _prompt_given_to_the_cli(_ctx(tmp_path, helper=False), monkeypatch)
    # MUTATION: append the note whatever the checkout holds -> this fails.
    assert "related-tests.sh" not in prompt
    assert claude_code.related_tests_note(None) is None


def test_the_note_is_added_once(tmp_path, monkeypatch):
    prompt = _prompt_given_to_the_cli(_ctx(tmp_path, helper=True), monkeypatch)
    assert prompt.count(claude_code.RELATED_TESTS_NOTE) == 1
