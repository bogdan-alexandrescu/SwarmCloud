"""The coverage ratchet (issue 64): each package's line coverage, measured on
the unit shards that already run, against the floor coverage-floors.json
holds for it. scripts/lib/coverage_floors.py builds the per-package reports
and judges them; scripts/coverage-floors.sh is what the `coverage` job runs.

Held here: a package below its floor fails with one line naming it; a package
at its floor passes; a floored package missing from a report that ran fails;
a malformed floors file fails rather than passing on what it could read; a
package 2+ points above its floor passes with a hint to raise it; the shards'
reports combine as one run (python: coverage.py's JSON grouped by
`apps/<dir>/`; swarm-ui: istanbul's coverage-final.json summed statement by
statement); and the workflow wires the job into ci-gate.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "lib" / "coverage_floors.py"
WRAPPER = ROOT / "scripts" / "coverage-floors.sh"
FLOORS = ROOT / "coverage-floors.json"
WORKFLOW = ROOT / ".github" / "workflows" / "application.yml"

_spec = importlib.util.spec_from_file_location("coverage_floors", SCRIPT)
floors_lib = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(floors_lib)


def _floors(tmp_path: Path, packages: dict, **extra) -> Path:
    path = tmp_path / "floors.json"
    path.write_text(json.dumps({"about": "measured in a test", "packages": packages, **extra}))
    return path


def _report(tmp_path: Path, suite: str, packages: dict[str, tuple[int, int]]) -> Path:
    path = tmp_path / f"{suite}-report.json"
    path.write_text(json.dumps({"suite": suite, "packages": {
        name: {"covered": covered, "lines": lines} for name, (covered, lines) in packages.items()}}))
    return path


def _check(floors: Path, *reports: Path) -> subprocess.CompletedProcess:
    args = ["python3", str(SCRIPT), "check", "--floors", str(floors)]
    for report in reports:
        args += ["--report", str(report)]
    return subprocess.run(args, capture_output=True, text=True, timeout=60)


# --- the verdict --------------------------------------------------------------
def test_a_package_below_its_floor_fails_with_one_line_naming_it(tmp_path):
    """MUTATION: compare with <= instead of <, or report only the first."""
    floors = _floors(tmp_path, {"apps/a": {"suite": "python", "floor": 80},
                                "apps/b": {"suite": "python", "floor": 50},
                                "apps/c": {"suite": "python", "floor": 90}})
    report = _report(tmp_path, "python", {"apps/a": (799, 1000), "apps/b": (60, 100), "apps/c": (10, 100)})
    proc = _check(floors, report)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    below = [line for line in proc.stdout.splitlines() if line.startswith("BELOW")]
    assert len(below) == 2, proc.stdout
    assert any("apps/a" in line and "79.90%" in line and "floor 80" in line for line in below), below
    assert any("apps/c" in line for line in below), below
    assert not any("apps/b" in line for line in below), below


def test_a_package_at_its_floor_passes(tmp_path):
    """MUTATION: fail on percent <= floor."""
    floors = _floors(tmp_path, {"apps/a": {"suite": "python", "floor": 80}})
    proc = _check(floors, _report(tmp_path, "python", {"apps/a": (80, 100)}))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "BELOW" not in proc.stdout and "RAISE" not in proc.stdout, proc.stdout


def test_a_floored_package_missing_from_a_report_that_ran_fails(tmp_path):
    """A package whose suite ran but whose files no test loaded reads as
    deleted tests, not as nothing to judge. MUTATION: skip a missing package."""
    floors = _floors(tmp_path, {"apps/a": {"suite": "python", "floor": 80},
                                "apps/gone": {"suite": "python", "floor": 40}})
    proc = _check(floors, _report(tmp_path, "python", {"apps/a": (90, 100)}))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert any(line.startswith("MISSING") and "apps/gone" in line for line in proc.stdout.splitlines()), proc.stdout


def test_a_package_whose_suite_did_not_run_is_not_judged(tmp_path):
    """The swarm-ui shards skip on a change that does not reach the console;
    the python floors are still judged. MUTATION: fail every package of a
    suite whose report was not given."""
    floors = _floors(tmp_path, {"apps/a": {"suite": "python", "floor": 80},
                                "apps/swarm-ui": {"suite": "ui", "floor": 70}})
    proc = _check(floors, _report(tmp_path, "python", {"apps/a": (85, 100)}))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "apps/swarm-ui" in proc.stdout and "did not run" in proc.stdout, proc.stdout


def test_two_points_above_the_floor_passes_with_a_hint_to_raise_it(tmp_path):
    """THE RATCHET. MUTATION: drop the hint, or print it one point above."""
    floors = _floors(tmp_path, {"apps/a": {"suite": "python", "floor": 80},
                                "apps/b": {"suite": "python", "floor": 80}})
    proc = _check(floors, _report(tmp_path, "python", {"apps/a": (82, 100), "apps/b": (819, 1000)}))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    raise_lines = [line for line in proc.stdout.splitlines() if line.startswith("RAISE")]
    assert len(raise_lines) == 1 and "apps/a" in raise_lines[0] and "81" in raise_lines[0], raise_lines


def test_an_unmeasured_package_reports_its_number_and_does_not_fail(tmp_path):
    """A floor of null with a note is a package not measured yet: the run
    prints the number to set and fails nothing. MUTATION: fail on null."""
    floors = _floors(tmp_path, {"apps/a": {"suite": "python", "floor": None, "note": "not yet measured"}})
    proc = _check(floors, _report(tmp_path, "python", {"apps/a": (731, 1000)}))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "apps/a" in proc.stdout and "73.10%" in proc.stdout and "72" in proc.stdout, proc.stdout


def test_a_reported_package_with_no_floor_entry_is_named(tmp_path):
    """MUTATION: say nothing about a new package."""
    floors = _floors(tmp_path, {"apps/a": {"suite": "python", "floor": 10}})
    proc = _check(floors, _report(tmp_path, "python", {"apps/a": (50, 100), "apps/new": (5, 10)}))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert any(line.startswith("NO FLOOR") and "apps/new" in line for line in proc.stdout.splitlines()), proc.stdout


@pytest.mark.parametrize("text, why", [
    ("{not json", "not JSON"),
    (json.dumps([]), "not an object"),
    (json.dumps({"about": "x"}), "no packages"),
    (json.dumps({"about": "x", "packages": {}}), "empty packages"),
    (json.dumps({"packages": {"apps/a": {"suite": "python", "floor": 1}}}), "no about"),
    (json.dumps({"about": "x", "packages": {"apps/a": {"suite": "python", "floor": "80"}}}), "a string floor"),
    (json.dumps({"about": "x", "packages": {"apps/a": {"suite": "python", "floor": 101}}}), "above 100"),
    (json.dumps({"about": "x", "packages": {"apps/a": {"suite": "python", "floor": -1}}}), "below 0"),
    (json.dumps({"about": "x", "packages": {"apps/a": {"suite": "python", "floor": True}}}), "a boolean"),
    (json.dumps({"about": "x", "packages": {"apps/a": {"floor": 80}}}), "no suite"),
    (json.dumps({"about": "x", "packages": {"apps/a": {"suite": "go", "floor": 80}}}), "an unknown suite"),
    (json.dumps({"about": "x", "packages": {"apps/a": {"suite": "python"}}}), "no floor key"),
    (json.dumps({"about": "x", "packages": {"apps/a": {"suite": "python", "floor": None}}}), "null without a note"),
    (json.dumps({"about": "x", "packages": {"apps/a": {"suite": "python", "floor": 8, "flor": 9}}}), "a typo key"),
])
def test_a_malformed_floors_file_fails(tmp_path, text, why):
    """A floors file the script cannot read whole must fail the job, never
    pass on the entries it could read. MUTATION: skip a bad entry."""
    floors = tmp_path / "floors.json"
    floors.write_text(text)
    proc = _check(floors, _report(tmp_path, "python", {"apps/a": (90, 100)}))
    assert proc.returncode == 2, (why, proc.stdout, proc.stderr)
    assert "coverage-floors" in proc.stderr or "floors" in proc.stderr, (why, proc.stderr)


def test_a_malformed_report_fails(tmp_path):
    """MUTATION: read a report with more covered lines than lines."""
    floors = _floors(tmp_path, {"apps/a": {"suite": "python", "floor": 10}})
    proc = _check(floors, _report(tmp_path, "python", {"apps/a": (11, 10)}))
    assert proc.returncode == 2, proc.stdout + proc.stderr


def test_the_committed_floors_file_is_well_formed():
    """The file CI reads parses, and every floor sits below what it says
    was measured. MUTATION: hand-edit a floor above its measurement."""
    floors = floors_lib.load_floors(FLOORS)
    assert floors, "coverage-floors.json holds no package"
    for name, entry in floors.items():
        if entry["floor"] is not None and entry.get("measured") is not None:
            assert entry["floor"] <= entry["measured"], (name, entry)


# --- the reports --------------------------------------------------------------
def test_the_python_report_groups_coverage_py_files_by_package(tmp_path):
    """MUTATION: group by the module's top directory instead of apps/<dir>."""
    raw = {"files": {
        "apps/swarm-api/swarm_api/main.py": {"summary": {"covered_lines": 8, "num_statements": 10}},
        "apps/swarm-api/swarm_api/sub/x.py": {"summary": {"covered_lines": 2, "num_statements": 10}},
        "apps/common/swarm_common/states.py": {"summary": {"covered_lines": 5, "num_statements": 5}},
        "scripts/lib/other.py": {"summary": {"covered_lines": 1, "num_statements": 9}},
    }}
    source = tmp_path / "coverage.json"
    source.write_text(json.dumps(raw))
    out = tmp_path / "python.json"
    proc = subprocess.run(["python3", str(SCRIPT), "python-report", "--json", str(source), "--out", str(out)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    report = json.loads(out.read_text())
    assert report == {"suite": "python", "packages": {
        "apps/common": {"covered": 5, "lines": 5}, "apps/swarm-api": {"covered": 10, "lines": 20}}}, report


def _istanbul(path: str, statements: list[tuple[int, int]], counts: list[int]) -> dict:
    return {path: {
        "path": path,
        "statementMap": {str(i): {"start": {"line": line, "column": col}, "end": {"line": line, "column": col + 5}}
                         for i, (line, col) in enumerate(statements)},
        "s": {str(i): count for i, count in enumerate(counts)},
        "fnMap": {}, "f": {}, "branchMap": {}, "b": {},
    }}


def test_the_ui_report_sums_the_shards_statement_by_statement(tmp_path):
    """A statement one shard ran and another did not is covered, once; a
    line with two statements counts once. MUTATION: take the last shard's
    counts, or count statements rather than lines."""
    root = "/runner/work/repo/apps/swarm-ui/"
    one = _istanbul(root + "src/A.tsx", [(1, 0), (2, 0), (2, 10), (3, 0)], [1, 0, 0, 0])
    one.update(_istanbul(root + "src/B.ts", [(1, 0), (2, 0)], [0, 0]))
    two = _istanbul(root + "src/A.tsx", [(1, 0), (2, 0), (2, 10), (3, 0)], [0, 0, 4, 0])
    two.update(_istanbul(root + "src/B.ts", [(1, 0), (2, 0)], [0, 0]))
    shard1, shard2 = tmp_path / "s1.json", tmp_path / "s2.json"
    shard1.write_text(json.dumps(one))
    shard2.write_text(json.dumps(two))
    out = tmp_path / "ui.json"
    proc = subprocess.run(["python3", str(SCRIPT), "ui-report", "--out", str(out), str(shard1), str(shard2)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    # A.tsx: lines 1 and 2 covered of 3; B.ts: 0 of 2.
    assert json.loads(out.read_text()) == {"suite": "ui", "packages": {"apps/swarm-ui": {"covered": 2, "lines": 5}}}


def test_the_ui_report_refuses_no_shard(tmp_path):
    """MUTATION: write an empty report that every floor then reads as missing."""
    proc = subprocess.run(["python3", str(SCRIPT), "ui-report", "--out", str(tmp_path / "ui.json")],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode != 0


# --- the wrapper and the workflow --------------------------------------------
def test_the_wrapper_judges_the_ui_shards_end_to_end(tmp_path):
    """scripts/coverage-floors.sh with the swarm-ui data only: merges the
    shards, judges the floors, and fails naming the package below.
    MUTATION: let the wrapper exit 0 whatever the check said."""
    data = tmp_path / "ui-data"
    (data / "1").mkdir(parents=True)
    (data / "2").mkdir()
    (data / "1" / "coverage-final.json").write_text(json.dumps(_istanbul("/w/apps/swarm-ui/src/A.tsx", [(1, 0), (2, 0)], [1, 0])))
    (data / "2" / "coverage-final.json").write_text(json.dumps(_istanbul("/w/apps/swarm-ui/src/A.tsx", [(1, 0), (2, 0)], [0, 0])))
    env = {**os.environ, "SWARM_NO_PROJECT": "1"}
    low = _floors(tmp_path, {"apps/swarm-ui": {"suite": "ui", "floor": 60}})
    proc = subprocess.run(["bash", str(WRAPPER), "--floors", str(low), "--ui-data", str(data), "--expect-ui-shards", "2"],
                          capture_output=True, text=True, timeout=60, env=env, cwd=ROOT)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "BELOW" in proc.stdout and "apps/swarm-ui" in proc.stdout, proc.stdout
    ok = _floors(tmp_path, {"apps/swarm-ui": {"suite": "ui", "floor": 50}})
    proc = subprocess.run(["bash", str(WRAPPER), "--floors", str(ok), "--ui-data", str(data), "--expect-ui-shards", "2"],
                          capture_output=True, text=True, timeout=60, env=env, cwd=ROOT)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_the_wrapper_fails_when_a_shard_s_data_is_missing(tmp_path):
    """A shard that uploaded nothing would lower every package's number for
    a reason no floor explains. MUTATION: combine whatever arrived."""
    data = tmp_path / "ui-data"
    (data / "1").mkdir(parents=True)
    (data / "1" / "coverage-final.json").write_text(json.dumps(_istanbul("/w/apps/swarm-ui/src/A.tsx", [(1, 0)], [1])))
    floors = _floors(tmp_path, {"apps/swarm-ui": {"suite": "ui", "floor": 50}})
    proc = subprocess.run(["bash", str(WRAPPER), "--floors", str(floors), "--ui-data", str(data), "--expect-ui-shards", "3"],
                          capture_output=True, text=True, timeout=60, env={**os.environ, "SWARM_NO_PROJECT": "1"}, cwd=ROOT)
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "1 of 3" in proc.stdout + proc.stderr, proc.stdout + proc.stderr


def _jobs() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]


def test_ci_gate_needs_the_coverage_job():
    """MUTATION: drop `coverage` from ci-gate's needs."""
    jobs = _jobs()
    assert "coverage" in jobs, sorted(jobs)
    assert "coverage" in jobs["ci-gate"]["needs"], jobs["ci-gate"]["needs"]
    assert set(jobs["coverage"]["needs"]) == {"changes", "python-unit", "ui-tests"}, jobs["coverage"]["needs"]


def test_the_coverage_job_runs_the_wrapper_and_no_test():
    """The suite runs once, in the shards; this job only combines.
    MUTATION: run pytest or vitest here."""
    steps = _jobs()["coverage"]["steps"]
    runs = "\n".join(str(step.get("run", "")) for step in steps)
    assert "scripts/coverage-floors.sh" in runs, runs
    assert "pytest" not in runs and "vitest" not in runs and "test:components" not in runs, runs


def test_the_shards_measure_only_when_they_run_the_whole_suite():
    """A pull request's narrowed selection measures a fraction of each
    package; judged against a floor it would fail for no reason.
    MUTATION: measure on python=selected."""
    jobs = _jobs()
    unit = next(s for s in jobs["python-unit"]["steps"] if s.get("name") == "unit tests")
    assert unit["env"].get("MEASURE_COVERAGE") == "${{ needs.changes.outputs.python == 'all' }}", unit["env"]
    assert "--cov" in unit["run"] and "COVERAGE_FILE" in unit["env"], unit
    uploads = [s for s in jobs["python-unit"]["steps"] if "coverage" in str(s.get("with", {}).get("name", ""))]
    assert len(uploads) == 1 and "python == 'all'" in uploads[0]["if"], uploads
    ui_uploads = [s for s in jobs["ui-tests"]["steps"] if "coverage" in str(s.get("with", {}).get("name", ""))]
    assert len(ui_uploads) == 1, ui_uploads
    vitest = next(s for s in jobs["ui-tests"]["steps"] if "test:components" in str(s.get("run", "")))
    assert "SWARM_UI_COVERAGE_DIR" in vitest["env"], vitest["env"]


def test_pytest_cov_is_pinned():
    """MUTATION: a range, which a resolution could move past the quarantine's pick."""
    text = (ROOT / "pyproject.toml").read_text()
    assert '"pytest-cov==' in text, "pytest-cov is not pinned in pyproject.toml"
    package = json.loads((ROOT / "apps" / "swarm-ui" / "package.json").read_text())
    pinned = package["devDependencies"]["@vitest/coverage-v8"]
    assert pinned == package["devDependencies"]["vitest"], (pinned, package["devDependencies"]["vitest"])


def test_the_coverage_job_judges_only_what_ran_the_whole_suite():
    """MUTATION: drop `!cancelled()` (the job would skip whenever one shard
    job was skipped), or judge python on a narrowed selection."""
    condition = str(_jobs()["coverage"]["if"])
    assert condition.startswith("${{ !cancelled() && "), condition
    assert "(needs.changes.outputs.python == 'all' && needs.python-unit.result == 'success')" in condition, condition
    assert ("(needs.changes.outputs.ui == 'true' && needs.changes.outputs.lane != 'true' "
            "&& needs.ui-tests.result == 'success')") in condition, condition


def test_fill_sets_the_null_floors_from_a_report_and_leaves_the_rest(tmp_path):
    """The step that turns the first CI measurement into floors. MUTATION:
    overwrite a floor already set, or set it at the measurement itself."""
    floors = _floors(tmp_path, {"apps/a": {"suite": "python", "floor": None, "note": "not yet measured"},
                                "apps/b": {"suite": "python", "floor": 40},
                                "apps/swarm-ui": {"suite": "ui", "floor": None, "note": "not yet measured"}})
    report = _report(tmp_path, "python", {"apps/a": (7319, 10000), "apps/b": (90, 100), "apps/new": (1, 2)})
    proc = subprocess.run(["python3", str(SCRIPT), "fill", "--floors", str(floors), "--about", "CI run 1, 2026-11-24",
                           "--report", str(report)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    packages = floors_lib.load_floors(floors)
    assert packages["apps/a"] == {"suite": "python", "floor": 72, "measured": 73.19}, packages["apps/a"]
    assert packages["apps/b"]["floor"] == 40, packages["apps/b"]
    assert packages["apps/new"]["floor"] == 49, packages["apps/new"]
    assert packages["apps/swarm-ui"]["floor"] is None, packages["apps/swarm-ui"]
    assert json.loads(floors.read_text())["about"] == "CI run 1, 2026-11-24"
    # --raise lifts a floor 2+ points under the measurement: the ratchet.
    proc = subprocess.run(["python3", str(SCRIPT), "fill", "--floors", str(floors), "--about", "CI run 2",
                           "--report", str(report), "--raise"], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert floors_lib.load_floors(floors)["apps/b"]["floor"] == 89
