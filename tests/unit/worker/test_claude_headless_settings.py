"""The claude-code runner's `--settings` file turns attribution off and lifts
the Bash timeout (#735, observer P21).

ATTRIBUTION. Claude Code appends a "Generated with Claude Code" footer to the
pull request bodies it writes and a `Co-Authored-By: Claude` trailer to its
commits unless its settings say otherwise. The worker refused a `pr-body.md`
carrying that footer whole, so a body that said `Closes #N` published without
it (16 of 248 PRs in 7 days, 2026-10-06). The keys are the ones the pinned CLI
(`CLAUDE_CODE_VERSION` in images/agent-runtime-base/Dockerfile, 2.1.283)
documents in its settings schema: `attribution` as the object form
`{"commit": "", "pr": "", "sessionUrl": false}` -- the CLI's own spelling of
"hide all attribution" -- and the deprecated `includeCoAuthoredBy: false` for
a CLI that predates `attribution`.

BASH TIMEOUT. The CLI's default Bash timeout is 2 minutes; it killed a
foreground command in 3 of 19 chunk-2 steps, while the lane briefs allow any
command up to 10 minutes. `BASH_DEFAULT_TIMEOUT_MS` and `BASH_MAX_TIMEOUT_MS`
are environment variables the CLI reads, set through the settings' `env`.
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_worker.runners import claude_code


def _settings(tmp_path: Path) -> dict:
    return json.loads(claude_code.write_headless_settings(tmp_path / "settings-dir").read_text())


def test_the_headless_settings_hide_all_attribution(tmp_path):
    settings = _settings(tmp_path)
    assert settings["attribution"] == {"commit": "", "pr": "", "sessionUrl": False}
    assert settings["includeCoAuthoredBy"] is False


def test_the_headless_settings_give_bash_ten_minutes(tmp_path):
    env = _settings(tmp_path)["env"]
    # The settings' env values are strings, as the CLI's schema requires.
    assert env["BASH_DEFAULT_TIMEOUT_MS"] == "600000"
    assert env["BASH_MAX_TIMEOUT_MS"] == "600000"


def test_the_headless_settings_keep_the_background_refusal(tmp_path):
    settings = _settings(tmp_path)
    assert settings["env"][claude_code.NO_BACKGROUND_ENV] == "1"
    (entry,) = settings["hooks"]["PreToolUse"]
    assert "Bash" in entry["matcher"].split("|")
