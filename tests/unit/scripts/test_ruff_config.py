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

import json
import re
import subprocess
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
PYPROJECT = REPO / "pyproject.toml"
LOCK = REPO / "uv.lock"

# Issue 54: the broad/silent-except rules, selected with a per-file baseline.
SILENT_EXCEPT_RULES = ("BLE001", "S110", "S112")
# The baseline's size when the rules were selected (2026-11-21). It may only
# shrink: lower this number when a line is deleted, never raise it.
BASELINE_CEILING = 146


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


def _baseline() -> dict[str, list[str]]:
    # The per-file-ignores entries naming any of the silent-except rules.
    ignores = _ruff()["lint"].get("per-file-ignores", {})
    return {k: v for k, v in ignores.items() if set(v) & set(SILENT_EXCEPT_RULES)}


def _ruff_check(*args: str, stdin: str | None = None) -> list[dict]:
    proc = subprocess.run(
        ["uv", "run", "--frozen", "--only-dev", "ruff", "check", "--output-format", "json",
         *args],
        cwd=REPO, input=stdin, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode in (0, 1), proc.stderr
    return json.loads(proc.stdout)


NEW_SILENT_EXCEPTS = """\
def swallow(fetch):
    try:
        return fetch()
    except Exception:
        pass


def skip(items, fetch):
    for item in items:
        try:
            fetch(item)
        except BaseException:
            continue


def default(fetch):
    try:
        return fetch()
    except Exception:
        return None
"""


def test_the_rule_set_is_the_conservative_one_plus_the_silent_except_rules():
    ruff = _ruff()
    assert ruff.get("target-version") == "py311"
    assert ruff.get("line-length") == 100
    assert ruff["lint"]["select"] == ["E4", "E7", "E9", "F", *SILENT_EXCEPT_RULES]


def test_the_silent_except_baseline_names_exact_existing_files_and_only_shrinks():
    baseline = _baseline()
    assert 0 < len(baseline) <= BASELINE_CEILING, (
        f"the BLE001/S110/S112 baseline has {len(baseline)} files, over its ceiling "
        f"{BASELINE_CEILING}: it may only shrink -- fix the new handler instead"
    )
    for path, rules in baseline.items():
        assert not set(path) & set("*?[]{}"), f"{path}: a baseline entry is one file, never a glob"
        assert (REPO / path).is_file(), f"{path}: baselined but gone -- delete its line"
        assert set(rules) <= set(SILENT_EXCEPT_RULES), f"{path}: {rules}"
        assert len(set(rules)) == len(rules), f"{path}: {rules}"
        assert not path.startswith("apps/common/swarm_common/"), path


def test_every_baselined_rule_still_suppresses_something():
    # A file whose handlers were fixed must leave the baseline, so it shrinks.
    baseline = _baseline()
    found = _ruff_check(
        "--select", ",".join(SILENT_EXCEPT_RULES),
        "--config", "lint.per-file-ignores = {}",
        *sorted(baseline),
    )
    live: dict[str, set[str]] = {}
    for f in found:
        live.setdefault(Path(f["filename"]).resolve().relative_to(REPO).as_posix(), set()).add(
            f["code"]
        )
    stale = {p: sorted(set(r) - live.get(p, set())) for p, r in baseline.items()}
    stale = {p: r for p, r in stale.items() if r}
    assert not stale, f"baseline lines that suppress nothing any more -- delete them: {stale}"


@pytest.mark.parametrize(
    "path",
    ["apps/agent-worker/agent_worker/brand_new_module.py", "scripts/brand_new_tool.py"],
)
def test_a_new_silent_broad_except_outside_the_baseline_fails(path):
    assert path not in _baseline()
    codes = sorted(
        f["code"] for f in _ruff_check("--stdin-filename", path, "-", stdin=NEW_SILENT_EXCEPTS)
    )
    assert codes == ["BLE001", "BLE001", "BLE001", "S110", "S112"], codes


def test_a_baselined_file_is_quiet_only_for_its_own_rules():
    baseline = _baseline()
    path = next(p for p, r in sorted(baseline.items()) if r == ["BLE001"])
    codes = sorted(
        f["code"] for f in _ruff_check("--stdin-filename", path, "-", stdin=NEW_SILENT_EXCEPTS)
    )
    assert codes == ["S110", "S112"], codes


def test_an_inline_noqa_ble001_marker_still_suppresses():
    source = NEW_SILENT_EXCEPTS.replace(
        "    except Exception:\n        return None",
        "    except Exception:  # noqa: BLE001\n        return None",
    )
    assert source != NEW_SILENT_EXCEPTS
    codes = sorted(
        f["code"]
        for f in _ruff_check(
            "--stdin-filename", "apps/agent-worker/agent_worker/brand_new_module.py", "-",
            stdin=source,
        )
    )
    assert codes == ["BLE001", "BLE001", "S110", "S112"], codes


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
