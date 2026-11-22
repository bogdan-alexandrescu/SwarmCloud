"""The claude-code runner sets no prompt-cache TTL; Claude Code's default is in force.

The owner decided on 2026-09-29 to change nothing yet after the 1-hour versus
5-minute measurement. There are three ways a TTL could start to be set: a
`promptCacheTtl` key (or a TTL env var) in the settings file
`claude_code.write_headless_settings` writes, a TTL variable added to
`cliagent`'s passthrough allow-list, or a deployment's `CLAUDE_CODE_ARGS`. The
first two are code and are held here; the third is Terraform. Whoever wires a
TTL changes this test in the same change.
"""

from __future__ import annotations

import json

_STALE = "a prompt-cache TTL is now set; change this test in the same change that wires it"

#: The environment variables that choose a TTL for the main conversation.
_TTL_ENV = ("CLAUDE_CODE_PROMPT_CACHE_TTL", "FORCE_PROMPT_CACHING_5M", "ENABLE_PROMPT_CACHING_1H")

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
