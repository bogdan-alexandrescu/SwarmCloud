"""pyright (basic) gates NEW type errors over a per-file baseline (TYPECHECK).

Audit "add a type checker (pyright basic) with a baseline", part of #55.
scripts/typecheck.sh runs the exactly pinned pyright with [tool.pyright] from
the root pyproject.toml and scripts/lib/pyright_baseline.py compares its
report with scripts/lib/pyright-baseline.json by (file, rule). These tests
hold the configuration, the pin and the baseline's shape, prove the
comparison on fabricated reports, and run the real script twice: red on a
temporary file with a type error, green on a file the baseline covers.
"""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "typecheck.sh"
BASELINE = REPO / "scripts" / "lib" / "pyright-baseline.json"
COMPARE = REPO / "scripts" / "lib" / "pyright_baseline.py"

# The baseline's error count when it was generated (2026-11-22, pyright
# 1.1.414). It may only shrink: lower this when errors are fixed, never raise it.
BASELINE_CEILING = 1245

_spec = importlib.util.spec_from_file_location("pyright_baseline", COMPARE)
assert _spec and _spec.loader
pb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pb)


def _script_pin() -> str:
    m = re.search(r'^PYRIGHT_VERSION="([^"]*)"$', SCRIPT.read_text(), re.M)
    assert m, "scripts/typecheck.sh must set PYRIGHT_VERSION=\"X.Y.Z\""
    return m.group(1)


def _baseline() -> dict:
    return json.loads(BASELINE.read_text())


def test_pyright_is_configured_once_in_basic_mode_for_the_python_apps():
    pyright = tomllib.loads((REPO / "pyproject.toml").read_text())["tool"]["pyright"]
    assert pyright["typeCheckingMode"] == "basic"
    assert pyright["pythonVersion"] == "3.11"
    assert pyright["include"] == ["apps"]
    assert "apps/swarm-ui" in pyright["exclude"]
    assert not (REPO / "pyrightconfig.json").exists(), (
        "one config: a pyrightconfig.json silently wins over [tool.pyright]"
    )


def test_pyright_is_pinned_exactly_and_the_baseline_was_written_by_that_version():
    pin = _script_pin()
    assert re.fullmatch(r"\d+\.\d+\.\d+", pin), f"pin pyright exactly, not {pin!r}"
    assert _baseline()["pyright"] == pin, (
        "a pyright bump reports different errors: regenerate the baseline with "
        "scripts/typecheck.sh --write-baseline in the same change"
    )
    assert '"pyright[nodejs]==${PYRIGHT_VERSION}"' in SCRIPT.read_text()


def test_the_baseline_names_existing_files_and_only_shrinks():
    files = _baseline()["files"]
    total = sum(n for rules in files.values() for n in rules.values())
    assert 0 < total <= BASELINE_CEILING, (
        f"the pyright baseline holds {total} errors, over its ceiling {BASELINE_CEILING}: "
        "fix the new errors instead of baselining them"
    )
    missing = [f for f in files if not (REPO / f).is_file()]
    assert not missing, f"baseline entries for files that no longer exist: {missing}"
    assert all(f.startswith("apps/") and not f.startswith("apps/swarm-ui/") for f in files)
    assert all(n > 0 for rules in files.values() for n in rules.values())


def test_the_script_is_executable_strict_and_sources_common():
    text = SCRIPT.read_text()
    assert SCRIPT.stat().st_mode & 0o111
    first = next(
        line for line in text.splitlines()[1:] if line.strip() and not line.startswith("#")
    )
    assert first == "set -euo pipefail"
    assert "source=lib/common.sh" in text and "/lib/common.sh\"" in text


def _report(*diags: tuple[str, str | None, str], analyzed: int = 3) -> dict:
    return {
        "version": "test",
        "summary": {"filesAnalyzed": analyzed, "errorCount": len(diags)},
        "generalDiagnostics": [
            {
                "file": str(REPO / f),
                "severity": sev,
                "message": "msg",
                "range": {"start": {"line": 9, "character": 4}},
                **({"rule": rule} if rule else {}),
            }
            for f, rule, sev in diags
        ],
    }


def _compare(tmp_path: Path, report: dict, files: dict, *extra: str) -> subprocess.CompletedProcess:
    (tmp_path / "r.json").write_text(json.dumps(report))
    (tmp_path / "b.json").write_text(json.dumps({"pyright": "x", "files": files}))
    return subprocess.run(
        ["python3", str(COMPARE), "compare", "--root", str(REPO),
         "--report", str(tmp_path / "r.json"), "--baseline", str(tmp_path / "b.json"), *extra],
        capture_output=True, text=True, timeout=60,
    )


def test_an_error_over_its_files_allowance_for_that_rule_fails(tmp_path: Path):
    files = {"apps/a.py": {"reportArgumentType": 1}}
    ok = _compare(tmp_path, _report(("apps/a.py", "reportArgumentType", "error")), files)
    assert ok.returncode == 0 and ok.stdout == "", ok
    # One more of the same rule in the same file: both are listed, the file fails.
    over = _compare(tmp_path, _report(("apps/a.py", "reportArgumentType", "error"),
                                      ("apps/a.py", "reportArgumentType", "error")), files)
    assert over.returncode == 1
    assert over.stdout.splitlines() == ["apps/a.py:10:5: reportArgumentType: msg"] * 2
    # A rule the file had none of, and a file the baseline does not name.
    other_rule = _compare(tmp_path, _report(("apps/a.py", "reportReturnType", "error")), files)
    assert other_rule.returncode == 1 and "reportReturnType" in other_rule.stdout
    new_file = _compare(tmp_path, _report(("apps/b.py", None, "error")), files)
    assert new_file.returncode == 1 and new_file.stdout.startswith("apps/b.py:10:5: error:")
    # Warnings are not gated.
    warn = _compare(tmp_path, _report(("apps/b.py", "reportUnusedVariable", "warning")), files)
    assert warn.returncode == 0


def test_a_run_that_checked_nothing_is_not_a_pass(tmp_path: Path):
    proc = _compare(tmp_path, _report(analyzed=0), {})
    assert proc.returncode == 2 and "analyzed no file" in proc.stderr


def test_a_full_run_names_entries_that_can_shrink_and_a_partial_one_does_not(tmp_path: Path):
    files = {"apps/a.py": {"reportArgumentType": 2}}
    report = _report(("apps/a.py", "reportArgumentType", "error"))
    full = _compare(tmp_path, report, files)
    assert full.returncode == 0 and "apps/a.py reportArgumentType: baseline 2, found 1" in full.stderr
    partial = _compare(tmp_path, report, files, "--partial")
    assert partial.returncode == 0 and "can shrink" not in partial.stderr


def test_write_records_counts_per_file_and_rule(tmp_path: Path):
    (tmp_path / "r.json").write_text(json.dumps(_report(
        ("apps/a.py", "reportArgumentType", "error"), ("apps/a.py", "reportArgumentType", "error"),
        ("apps/b.py", "reportCallIssue", "error"), ("apps/b.py", "reportCallIssue", "warning"))))
    out = tmp_path / "b.json"
    subprocess.run(
        ["python3", str(COMPARE), "write", "--root", str(REPO), "--report", str(tmp_path / "r.json"),
         "--baseline", str(out), "--version", "9.9.9"],
        check=True, capture_output=True, text=True, timeout=60,
    )
    body = json.loads(out.read_text())
    assert body["pyright"] == "9.9.9"
    assert body["files"] == {"apps/a.py": {"reportArgumentType": 2},
                             "apps/b.py": {"reportCallIssue": 1}}


# --- the real script, the real pyright -----------------------------------------
# These two download the pinned pyright through `uv tool run` on first use.

needs_uv = pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not installed")


def _typecheck(*files: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT), *files], cwd=REPO, capture_output=True, text=True, timeout=600,
    )


@needs_uv
def test_the_script_fails_on_a_new_type_error(tmp_path: Path):
    bad = tmp_path / "new_type_error.py"
    bad.write_text("def label(count: int) -> str:\n    return count\n")
    proc = _typecheck(str(bad))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert re.search(r"new_type_error\.py:2:\d+: reportReturnType: ", proc.stdout), proc.stdout
    assert "1 in a (file, rule) over its allowance" in proc.stderr


@needs_uv
def test_the_script_passes_on_a_file_the_baseline_covers():
    files = _baseline()["files"]
    # The baselined file with the most errors: green only because the baseline allows them.
    covered = max(files, key=lambda f: sum(files[f].values()))
    proc = _typecheck(covered)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout == ""
    m = re.search(r"1 files analyzed, (\d+) errors", proc.stderr)
    assert m and int(m.group(1)) == sum(files[covered].values()), proc.stderr
