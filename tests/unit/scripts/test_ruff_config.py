"""ruff is the house linter, configured once in the root pyproject (RUFF).

Owner decision 2026-10-14, "Adopt ruff". The pre-publish gate
(`apps/agent-worker/agent_worker/prepublish.py` `_configures_ruff`) runs
`ruff check` and `ruff format --check` over a lane's changed Python only where
the repository configures ruff, through `uv run ruff` -- so the config has to
exist where the gate looks, the version has to be the one uv.lock installs,
and formatting, which was NOT adopted, has to pass for every file the gate
names, or every lane touching Python would be sent to reformat what it touched.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
PYPROJECT = REPO / "pyproject.toml"
LOCK = REPO / "uv.lock"


def _project() -> dict:
    return tomllib.loads(PYPROJECT.read_text())


def _ruff() -> dict:
    ruff = _project().get("tool", {}).get("ruff")
    assert ruff is not None, "pyproject.toml has no [tool.ruff]: the pre-publish gate skips ruff"
    return ruff


def _dev_pin() -> str:
    pins = [d for d in _project()["dependency-groups"]["dev"] if re.match(r"ruff\b", d)]
    assert len(pins) == 1, f"the dev group must name ruff exactly once, found {pins}"
    m = re.fullmatch(r"ruff==(\d+\.\d+\.\d+)", pins[0])
    assert m, f"ruff must be pinned exactly (ruff==X.Y.Z), not {pins[0]!r}"
    return m.group(1)


def test_the_gate_finds_a_ruff_config_where_it_looks():
    # The same test `_configures_ruff` makes, so the two cannot disagree.
    assert "[tool.ruff" in PYPROJECT.read_text()
    assert not (REPO / "ruff.toml").exists() and not (REPO / ".ruff.toml").exists(), (
        "one config: a ruff.toml beside [tool.ruff] silently wins over the pyproject"
    )


def test_ruff_is_an_exact_dev_pin_held_equal_to_required_version_and_the_lock():
    version = _dev_pin()
    assert _ruff().get("required-version") == f"=={version}", (
        "required-version must equal the dev pin, so a different ruff refuses to lint"
    )
    lock = LOCK.read_text()
    m = re.search(r'\[\[package\]\]\nname = "ruff"\nversion = "([^"]+)"', lock)
    assert m, "uv.lock has no ruff package: run `uv lock`"
    assert m.group(1) == version


def test_the_rule_set_is_the_conservative_one():
    ruff = _ruff()
    assert ruff.get("target-version") == "py311"
    assert ruff.get("line-length") == 100
    assert ruff["lint"]["select"] == ["E4", "E7", "E9", "F"]


def test_formatting_is_not_adopted_even_for_files_named_explicitly():
    # The gate passes files by name; only `force-exclude` makes an exclude
    # apply to them, and `format.exclude = ["*"]` makes `ruff format --check`
    # a no-op rather than a demand to reformat whatever a lane touched.
    ruff = _ruff()
    assert ruff.get("force-exclude") is True
    assert ruff.get("format", {}).get("exclude") == ["*"]


def test_the_frozen_contract_is_never_fixed_only_ignored():
    # CLAUDE.md rule 1: apps/common/swarm_common/ is not edited for lint.
    ignores = _ruff()["lint"].get("per-file-ignores", {})
    assert "F401" in ignores.get("apps/common/swarm_common/**", [])
