"""Every rule of source_text_rules/control_plane_am.py still covers the files it
was written for.

A rule whose root is renamed covers nothing and passes, where the test it
replaced failed on an empty walk or a missing file (`found only N modules`,
`is not the apps tree`). test_check_source_text.py runs each rule's samples;
this holds each rule's roots against the tracked files.

MUTATION: root `git-slot-read-through-the-resolver` at a directory that does
not exist, and its case is red.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def _load():
    name = "check_source_text"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # its dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


cst = _load()
cst.discover_rules()
RULES = sys.modules["source_text_rules.control_plane_am"].RULES

#: The fewest files a rule over a whole package must cover; a rule over named
#: modules covers exactly those. swarm_api's is the count the slot scan held.
FLOORS = {
    ("apps",): 200,
    ("apps/swarm-api/swarm_api",): 50,
    ("apps/swarm-mcp/swarm_mcp",): 20,
    ("apps/swarm-install/swarm_install",): 5,
}


@pytest.fixture(scope="module")
def files() -> list[str]:
    return cst.git_files(ROOT)


@pytest.mark.parametrize("rule", RULES, ids=lambda r: r.id)
def test_the_rule_covers_the_files_it_was_written_for(rule, files):
    covered = [path for path in files if rule.covers(path)]
    if rule.roots in FLOORS:
        assert len(covered) >= FLOORS[rule.roots], (rule.roots, len(covered))
    else:
        assert sorted(covered) == sorted(rule.roots)
