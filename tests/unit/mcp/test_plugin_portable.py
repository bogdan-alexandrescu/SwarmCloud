"""The plugin runs without a checkout and names no live deployment.

DAYONE-PLUGIN-1 (plan review finding 17). The plugin is published to the
release bucket and installed by teammates who have no checkout of this
repository, so its skill, commands and workflow tell them to run the installed
`sc` and `swarm` commands, never `uv run …` (which works only inside a
checkout). And they are read by people on other deployments, so they name no
live project, domain, repository owner or tailnet.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_PLUGIN = _REPO / "plugin"

#: The files the plugin bundle ships that a person or a model reads.
SHIPPED = (
    "skills/sc/SKILL.md",
    "commands/sc.md",
    "commands/setup.md",
    "workflows/run.js",
)

#: The live deployment's literals (plan appendix A; `test_no_live_literals.py`).
LIVE = ("saga.xyz", "saga-agents-staging", "bogdan-alexandrescu", "taile0bda9")

_CHECKOUT_HINT = re.compile(r"\buv (?:tool )?run\b")


@pytest.mark.parametrize("relative", SHIPPED)
def test_plugin_has_no_checkout_hint_or_live_literal(relative):
    text = (_PLUGIN / relative).read_text(encoding="utf-8")
    problems = []
    for number, line in enumerate(text.splitlines(), 1):
        if _CHECKOUT_HINT.search(line):
            problems.append(f"plugin/{relative}:{number}: checkout hint: {line.strip()}")
        for literal in LIVE:
            if literal in line:
                problems.append(f"plugin/{relative}:{number}: live literal {literal!r}: {line.strip()}")
    assert not problems, "\n".join(problems)


def test_the_plugin_needs_one_value():
    manifest = json.loads((_PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    options = manifest["userConfig"]
    required = sorted(key for key, option in options.items() if option.get("required"))
    assert required == ["deployment_url"]
    for key in ("oauth_client_id", "oauth_client_secret"):
        assert "discovery" in options[key]["description"], key
    assert not _CHECKOUT_HINT.search(manifest["description"]), manifest["description"]
