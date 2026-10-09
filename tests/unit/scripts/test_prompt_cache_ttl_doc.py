"""docs/cost-control.md records the claude-code prompt-cache TTL, and the runner sets none (#323).

#323 measured the 1-hour versus 5-minute prompt-cache TTL over 47 claude-code
tasks, and the owner decided on 2026-09-29 to change nothing yet and to park
the controlled A/B. The finding, the decision and the knob live in the doc's
"Provider tokens: the prompt-cache TTL" section, which says plainly that no TTL
is set and Claude Code's default is in force.

That sentence is only true while the runner sets no TTL. There are three ways
it could start to: a `promptCacheTtl` key (or a TTL env var) in the settings
file `claude_code.write_headless_settings` writes, `CLAUDE_CODE_PROMPT_CACHE_TTL`
added to `cliagent`'s passthrough allow-list, or a deployment's
`CLAUDE_CODE_ARGS`. The first two are code and are held here; the third is
Terraform, and the doc names it so the person wiring it finds the section.
Whoever wires a TTL updates the doc in the same change and this test with it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
DOC = REPO / "docs" / "cost-control.md"

_STALE = (
    "docs/cost-control.md states that no prompt-cache TTL is set and Claude "
    "Code's default TTL is in force (#323); update the doc in the same change "
    "that wires a TTL, then this test"
)

#: The environment variables that choose a TTL for the main conversation.
_TTL_ENV = ("CLAUDE_CODE_PROMPT_CACHE_TTL", "FORCE_PROMPT_CACHING_5M", "ENABLE_PROMPT_CACHING_1H")


def _section() -> str:
    text = DOC.read_text(encoding="utf-8")
    match = re.search(r"^### [^\n]*prompt-cache TTL[^\n]*\n(.*?)(?=^#{1,3} |\Z)", text, re.M | re.S)
    assert match, f"docs/cost-control.md has no '### ... prompt-cache TTL' section; {_STALE}"
    return match.group(0)


def test_the_doc_records_the_ttl_finding_and_the_knob():
    section = _section()
    for name in (
        "promptCacheTtl",
        "CLAUDE_CODE_PROMPT_CACHE_TTL",
        "write_headless_settings",
        "_PLAIN_PASSTHROUGH",
        "CLAUDE_CODE_ARGS",
    ):
        assert name in section, f"the TTL section does not name {name}; {_STALE}"
    assert "https://github.com/bogdan-alexandrescu/SwarmCloud/issues/323" in section, (
        f"the TTL section does not link issue #323; {_STALE}"
    )
    assert "default is in force" in section, f"the TTL section no longer says so; {_STALE}"


def test_the_runner_passes_no_ttl_variable_through():
    from agent_worker.runners import cliagent

    passed = (*cliagent._PLAIN_PASSTHROUGH, *cliagent._SENSITIVE_PASSTHROUGH)
    for name in _TTL_ENV:
        assert name not in passed, f"cliagent now passes {name} through; {_STALE}"


def test_the_headless_settings_choose_no_ttl(tmp_path):
    from agent_worker.runners import claude_code

    settings = json.loads(claude_code.write_headless_settings(tmp_path / "settings-dir").read_text())
    assert "promptCacheTtl" not in settings, f"write_headless_settings now sets promptCacheTtl; {_STALE}"
    assert "subagentPromptCacheTtl" not in settings, (
        f"write_headless_settings now sets subagentPromptCacheTtl; {_STALE}"
    )
    for name in _TTL_ENV:
        assert name not in settings["env"], f"write_headless_settings now sets {name} in env; {_STALE}"
