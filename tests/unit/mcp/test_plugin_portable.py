"""The plugin runs without a checkout and names no live deployment.

DAYONE-PLUGIN-1 (plan review finding 17). The plugin is published to the
release bucket and installed by teammates who have no checkout of this
repository, so its skill, commands and workflow tell them to run the installed
`sc` and `swarm` commands, never `uv run …` (which works only inside a
checkout). And they are read by people on other deployments, so they name no
live project, domain, repository owner or tailnet.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
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

_CHECKOUT_HINT = re.compile(r"\buv (?:tool )?run\b")


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Registered first: a dataclass resolves its annotations through sys.modules.
    sys.modules.setdefault(name, module)
    spec.loader.exec_module(module)
    return module


#: What is live is read by the deployment-values guard, never spelled here
#: (docs/design/generic-repository.md sections 1.4 and 4.5; the same reading as
#: tests/unit/scripts/test_no_live_literals.py): its shape rules hold no value
#: and run offline, its name layer is the private pattern list.
guard = _load("swarm_guards_deployment_values", _REPO / "scripts" / "guards" / "deployment_values.py")


def _listed() -> list:
    """The pattern list's entries, read as the guard reads it; skips with no list."""
    path = os.environ.get(guard.LIST_FILE_VARIABLE)
    text = Path(path).read_text(encoding="utf-8") if path else os.environ.get(guard.LIST_VARIABLE)
    if not text or not text.strip():
        pytest.skip("no deployment pattern list (DEPLOYMENT_PATTERNS or DEPLOYMENT_PATTERNS_FILE): "
                    "nothing says what is live")
    return guard.parse_list(text).entries


def _findings(relative: str, entries: list) -> list[str]:
    """The guard's findings in one shipped file: `path:line category rule`, never the value."""
    path = _PLUGIN / relative
    source = guard.Source(path.relative_to(_REPO).as_posix(), path.read_text(encoding="utf-8"))
    allow = guard.Allow.load().screened(entries)
    return [str(f) for f in guard.scan([source], allow, entries, archive_refs=False,
                                       address=guard.repository_address(_REPO))]


@pytest.mark.parametrize("relative", SHIPPED)
def test_plugin_has_no_checkout_hint_or_deployment_shape(relative):
    """No `uv run`, and no project, host, address, tailnet or bucket outside the placeholder set."""
    text = (_PLUGIN / relative).read_text(encoding="utf-8")
    problems = [f"plugin/{relative}:{number}: checkout hint: {line.strip()}"
                for number, line in enumerate(text.splitlines(), 1) if _CHECKOUT_HINT.search(line)]
    problems += _findings(relative, [])
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("relative", SHIPPED)
def test_plugin_names_nothing_on_the_deployment_pattern_list(relative):
    """The live deployment's names: its domain, project, repository owner and tailnet."""
    findings = _findings(relative, _listed())
    assert not findings, "\n".join(findings)


def test_the_plugin_needs_one_value():
    manifest = json.loads((_PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    options = manifest["userConfig"]
    required = sorted(key for key, option in options.items() if option.get("required"))
    assert required == ["deployment_url"]
    for key in ("oauth_client_id", "oauth_client_secret"):
        assert "discovery" in options[key]["description"], key
    assert not _CHECKOUT_HINT.search(manifest["description"]), manifest["description"]
