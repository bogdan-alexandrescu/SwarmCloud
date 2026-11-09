"""Every rule `swarm_check` can return has a row in the /sc check rule table.

The `check` section of plugin/skills/sc/SKILL.md tells a session, rule by
rule, what is flagged and what is not (`| `rule` | flags | is NOT flagged |`).
`plugin_files_missing` and `deploy_behind` merged on 2026-11-09 without a row,
so a session reporting them had nothing to say what they mean. The rules are
read from the bridge's own source -- every `"rule": ...` in a dict literal of
swarm_mcp/check.py, a string or a module-level name -- so the next rule cannot
merge without its row.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from swarm_mcp import check

_REPO = Path(__file__).resolve().parents[3]
_SKILL = _REPO / "plugin" / "skills" / "sc" / "SKILL.md"
_HEADER = "| `rule` | flags | is NOT flagged |"
_ROW = re.compile(r"^\|\s*`([a-z_]+)`\s*\|")


def _returned_rules() -> set[str]:
    tree = ast.parse(Path(check.__file__).read_text(encoding="utf-8"))
    rules: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if not (isinstance(key, ast.Constant) and key.value == "rule"):
                continue
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                rules.add(value.value)
            elif isinstance(value, ast.Name) and isinstance(getattr(check, value.id, None), str):
                rules.add(getattr(check, value.id))
    return rules


def _table_rows() -> list[str]:
    lines = [line.strip() for line in _SKILL.read_text(encoding="utf-8").splitlines()]
    assert lines.count(_HEADER) == 1, f"SKILL.md holds {lines.count(_HEADER)} rule tables, not one"
    rows = []
    for line in lines[lines.index(_HEADER) + 2:]:
        match = _ROW.match(line)
        if match is None:
            break
        rows.append(match.group(1))
    return rows


def test_the_rules_are_read_from_the_bridge():
    rules = _returned_rules()
    # Read, not empty: the rules of every age of the check are found.
    assert {"no_heartbeat", "parked", "deploy_behind", "plugin_files_missing"} <= rules, rules
    assert set(check._RULE_ORDER) <= rules


def test_every_rule_swarm_check_returns_has_a_row():
    rows = _table_rows()
    missing = sorted(_returned_rules() - set(rows))
    assert not missing, f"the /sc check rule table in {_SKILL.relative_to(_REPO)} has no row for {missing}"


def test_every_row_names_a_rule_the_bridge_returns_once():
    rows = _table_rows()
    assert len(rows) == len(set(rows)), rows
    assert set(rows) <= _returned_rules(), sorted(set(rows) - _returned_rules())


def test_the_two_new_rows_say_what_the_owner_accepted():
    text = _SKILL.read_text(encoding="utf-8")
    deploy = next(line for line in text.splitlines() if line.strip().startswith("| `deploy_behind` |"))
    assert "more than 60 min" in deploy and "60 min or less" in deploy
    assert "not read: " in deploy and "release run" in deploy
    plugin = next(line for line in text.splitlines() if line.strip().startswith("| `plugin_files_missing` |"))
    assert "type /reload-plugins in this session" in plugin and "run.js" in plugin
