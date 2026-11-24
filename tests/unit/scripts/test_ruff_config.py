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

# Issue 54: the broad/silent-except rules. Issue 185: every existing site
# carries its own inline noqa marker; no per-file ignore names these rules.
SILENT_EXCEPT_RULES = ("BLE001", "S110", "S112")
# The number of BLE001/S110/S112 sites silenced by an inline noqa marker when
# issue 185 landed. It may only go DOWN: fix a handler, delete its marker,
# lower this number. Never raise it.
SUPPRESSED_SITE_CEILING = 1050


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


def test_no_per_file_ignore_names_a_silent_except_rule():
    # A per-file ignore silences the rule for the whole file, so a NEW broad
    # except there would pass (issue 185): each site is marked inline instead.
    ignores = _ruff()["lint"].get("per-file-ignores", {})
    named = {k: v for k, v in ignores.items() if set(v) & set(SILENT_EXCEPT_RULES)}
    assert not named, f"per-file ignores naming a silent-except rule: {named}"
    assert "BLE001/S110/S112 BASELINE" not in PYPROJECT.read_text()


@pytest.mark.parametrize(
    "path",
    [
        # Formerly baselined for BLE001 (issue 54): no longer quiet as a file.
        "apps/agent-worker/agent_worker/lifecycle.py",
        "apps/swarm-api/swarm_api/attemptstate.py",
        "scripts/lib/ci_reuse.py",
        # Brand-new paths: every path behaves the same.
        "apps/agent-worker/agent_worker/brand_new_module.py",
        "scripts/brand_new_tool.py",
    ],
)
def test_a_new_silent_broad_except_in_a_formerly_baselined_file_fails(path):
    codes = sorted(
        f["code"] for f in _ruff_check("--stdin-filename", path, "-", stdin=NEW_SILENT_EXCEPTS)
    )
    assert codes == ["BLE001", "BLE001", "BLE001", "S110", "S112"], codes


def test_the_tree_is_clean_and_the_suppressed_site_count_only_goes_down():
    rules = ",".join(SILENT_EXCEPT_RULES)
    unmarked = _ruff_check("--select", rules, ".")
    assert unmarked == [], (
        "a BLE001/S110/S112 site without an inline marker -- narrow the handler: "
        + ", ".join(f"{f['filename']}:{f['location']['row']} {f['code']}" for f in unmarked)
    )
    # Counting ruff's own findings with the markers ignored, not a regex over
    # comments: a regex would count markers inside strings and miss a blanket
    # noqa marker.
    count = len(_ruff_check("--select", rules, "--ignore-noqa", "."))
    assert count <= SUPPRESSED_SITE_CEILING, (
        f"{count} BLE001/S110/S112 sites carry a `# noqa`, over SUPPRESSED_SITE_CEILING "
        f"{SUPPRESSED_SITE_CEILING}: it may only go down -- fix the new handler instead"
    )
    assert count >= SUPPRESSED_SITE_CEILING, (
        f"only {count} BLE001/S110/S112 sites carry a `# noqa` now: lower "
        f"SUPPRESSED_SITE_CEILING in {Path(__file__).name} to {count}"
    )


_UNUSED = re.compile(r"(?:\(|; )unused: ([^;)]*)")


def test_no_silent_except_marker_suppresses_nothing():
    # RUF100 runs here scoped to the three rules, NOT in `lint.select`:
    # selected repository-wide (measured for issue 185) it reported 920
    # findings in 309 files -- about 570 markers for rules this repository
    # never selected (ANN001, SLF001, ARG001 ...), 310 `F811` markers under
    # tests/, and one inside the FROZEN apps/common/swarm_common/. Only the
    # `unused:` part of a message counts; `non-enabled:` lists codes never
    # selected here.
    stale = []
    for f in _ruff_check("--select", ",".join((*SILENT_EXCEPT_RULES, "RUF100")), "."):
        if f["code"] != "RUF100":
            continue
        m = _UNUSED.search(f["message"])
        unused = {c.strip(" `") for c in m.group(1).split(",")} if m else set()
        if unused & set(SILENT_EXCEPT_RULES):
            stale.append(f"{f['filename']}:{f['location']['row']} {f['message']}")
    assert not stale, "markers that suppress nothing -- delete the code from them: " + "; ".join(
        stale
    )


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
