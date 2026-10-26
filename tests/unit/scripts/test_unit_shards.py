"""The unit suite and the vitest suite run in shards, under the check names they had.

OWNER REQUEST, 2026-10-07. With strict up-to-date merging, one pull request
merges per CI cycle, and CI at a pull request's final head took a median 707 s
-- 82 % of the time between merges. Its two long poles were the Python unit
suite (`format / unit tests`, ~673 s) and the vitest suite (`swarm-ui
typecheck / component tests`, ~11 min). Both now run as parallel shards, and
the old names are kept by jobs that only read the shards' results.

What is asserted here, by running the steps rather than matching their text
where a step decides something:

  * every check name application.yml reported is still reported, and the
    required ones by exactly one job;
  * the Python shards cover every test file under tests/unit exactly once,
    balanced by the stored weights; a docs-only or UI-only change runs only the
    test files that read docs/ or apps/swarm-ui/ -- directly, through a helper
    they import, or under a conftest that reads it -- and a shard left with no
    file runs nothing (a bare `pytest` would run the whole suite);
  * the Python shard count is computed once, by scripts/lib/unit_shards.py
    in the `changes` job, from the measured weight of what the change runs
    (LANE CI-EARLY, part of #1694): a full run gets more shards, a small
    selection fewer, so every shard carries about the same measured time;
  * vitest is sharded with its own `--shard=k/N` over the same count, beside
    one job for the typecheck, the node:test runners and the build;
  * everything the old `python` job checked besides the unit suite still runs.

The aggregating jobs' judgement and ci-gate's are run in test_ci_gate.py; the
path-filter matrix in test_ui_changes_gate.py.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from .ci_fold_layout import check_steps

REPO = Path(__file__).resolve().parents[3]
APPLICATION = REPO / ".github" / "workflows" / "application.yml"
DURATIONS = REPO / "tests" / "unit" / "scripts" / "unit_test_durations.json"
UI_PACKAGE = REPO / "apps" / "swarm-ui" / "package.json"
UNIT_SHARDS = REPO / "scripts" / "lib" / "unit_shards.py"

#: Every check name application.yml reported on a pull request before the
#: shards (read from main at 8892e96). Each must still be reported.
NAMES_BEFORE = {
    "shellcheck",
    "release workflow wiring (actionlint)",
    "format / unit tests",
    "swarm-ui typecheck / component tests",
    "integration tests (emulator)",
    "kubernetes manifests",
    "build changed images without pushing",
    "build images",
}


def _jobs() -> dict:
    return yaml.safe_load(APPLICATION.read_text())["jobs"]


def _needs(job: dict) -> list[str]:
    needs = job.get("needs")
    return [needs] if isinstance(needs, str) else list(needs or [])


def _step(job_id: str, step_id: str) -> dict:
    found = [s for s in _jobs()[job_id]["steps"] if s.get("id") == step_id]
    assert len(found) == 1, f"{job_id} has no single step with id {step_id}"
    return found[0]


def _shards(job_id: str) -> list[int]:
    return _jobs()[job_id]["strategy"]["matrix"]["shard"]


def _test_files(root: Path = REPO) -> set[str]:
    """Every file pytest collects under tests/unit: its default python_files."""
    found = set()
    for path in (root / "tests" / "unit").rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        if path.name.startswith("test_") or path.name.endswith("_test.py"):
            found.add(str(path.relative_to(root)))
    return found


# ---------------------------------------------------------------------------
# The names.
# ---------------------------------------------------------------------------
def test_every_check_name_is_still_reported():
    """MUTATION: rename `python` or `ui`, or drop the job carrying a name."""
    names = {str(job.get("name") or job_id) for job_id, job in _jobs().items()}
    assert NAMES_BEFORE <= names, NAMES_BEFORE - names
    assert "ci-gate" in names
    for name in ("format / unit tests", "swarm-ui typecheck / component tests", "ci-gate"):
        assert sum(str(j.get("name")) == name for j in _jobs().values()) == 1, name


@pytest.mark.parametrize(
    "job_id, needs",
    [("python", {"changes", "python-checks", "python-unit"}), ("ui", {"changes", "ui-build", "ui-tests"})],
)
def test_the_kept_names_stand_for_their_shards(job_id, needs):
    """`always()`: a job skipped because a shard failed would report skipped,
    which reads as passed. MUTATION: drop a shard job from `needs`."""
    job = _jobs()[job_id]
    assert set(_needs(job)) == needs, _needs(job)
    assert job.get("if") == "always()", job.get("if")


def test_the_build_still_waits_for_the_unit_tests():
    """main's image build runs only after the free checks pass; `python` is
    their result now."""
    assert {"python", "shell", "manifests"} <= set(_needs(_jobs()["build"]))


def test_the_old_python_jobs_other_checks_still_run():
    """MUTATION: drop any step from python-checks."""
    # Its steps run in `quick-checks` since CI-FOLD (#1799).
    runs = "\n".join(str(s.get("run", "")) for s in check_steps("application.yml", "python-checks"))
    for needle in ("uv lock --check", "uv sync --frozen", "check-frozen-contract.sh", "compileall",
                   "test_plugin_bridge_install.py", "destroy.sh --self-test"):
        assert needle in runs, needle
    # The lock is checked before the frozen sync installs from it, in the job
    # that checks it (test_uv_lock_check_step.py); each shard syncs too.
    unit = "\n".join(str(s.get("run", "")) for s in _jobs()["python-unit"]["steps"])
    assert "uv sync --frozen" in unit and "uv lock --check" not in unit


# ---------------------------------------------------------------------------
# The Python shards.
# ---------------------------------------------------------------------------
def test_the_python_shard_count_is_written_once():
    """The `changes` job computes the count; the matrix reads it and the pick
    step reads `strategy.job-total`. MUTATION: write SHARDS: 4 in the step,
    or put a literal list back in the matrix."""
    job = _jobs()["python-unit"]
    assert job["strategy"]["matrix"]["shard"] == "${{ fromJSON(needs.changes.outputs.unit_shards) }}", job["strategy"]
    assert "changes" in _needs(job)
    changes = _jobs()["changes"]
    assert changes["outputs"].get("unit_shards") == "${{ steps.shards.outputs.shards }}", changes["outputs"]
    step = _step("changes", "shards")
    assert "scripts/lib/unit_shards.py count" in step["run"], step["run"]
    env = step.get("env") or {}
    assert env.get("PYTHON_MODE") == "${{ steps.select.outputs.python }}", env
    assert env.get("PYTHON_AREAS") == "${{ steps.select.outputs.areas }}", env
    assert env.get("PYTHON_TESTS") == "${{ steps.select.outputs.tests }}", env
    assert env.get("DURATIONS") == "tests/unit/scripts/unit_test_durations.json", env
    env = _step("python-unit", "pick").get("env") or {}
    assert env.get("SHARD") == "${{ matrix.shard }}", env
    assert env.get("SHARDS") == "${{ strategy.job-total }}", env
    pick = _step("python-unit", "pick")["run"]
    assert pick.strip().splitlines()[-1].strip() == "python3 scripts/lib/unit_shards.py pick", pick
    assert _jobs()["python-unit"]["strategy"].get("fail-fast") is False


def _count(tmp_path: Path, *, mode: str = "all", areas: str = "", tests: str = "",
           cwd: Path = REPO, durations: Path = DURATIONS) -> tuple[subprocess.CompletedProcess[str], dict]:
    output = tmp_path / "count-output"
    output.write_text("")
    # Never the job's own step summary: a count's coverage warning lands in
    # `count-summary`, where a test can read it.
    env = {**os.environ, "PYTHON_MODE": mode, "PYTHON_AREAS": areas, "PYTHON_TESTS": tests,
           "DURATIONS": str(durations), "GITHUB_OUTPUT": str(output),
           "GITHUB_STEP_SUMMARY": str(tmp_path / "count-summary")}
    proc = subprocess.run(["python3", str(UNIT_SHARDS), "count"], cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=120)
    outputs = dict(l.split("=", 1) for l in output.read_text().splitlines() if "=" in l)
    return proc, outputs


def _constants() -> tuple[float, int]:
    text = UNIT_SHARDS.read_text()
    target = re.search(r"^SHARD_TARGET_SECONDS = (\d+(?:\.\d+)?)\b", text, re.M)
    most = re.search(r"^MAX_SHARDS = (\d+)\b", text, re.M)
    assert target and most, "unit_shards.py no longer sets SHARD_TARGET_SECONDS and MAX_SHARDS on lines of their own"
    return float(target.group(1)), int(most.group(1))


def test_a_full_run_gets_enough_shards_that_none_carries_much_over_the_target(tmp_path):
    """THE TAIL (LANE CI-EARLY, 2026-10-20). The weights now sum to ~7,000 s,
    not the ~2,950 s the four fixed shards were sized for, so a full run's
    shard carried ~1,750 s: a 455-501 s p90 against a 152-188 s median, the
    median being pull requests' small selections. Sized by weight, a full
    run's shard carries at most ~1.5x what a selection's shard does.
    MUTATION: go back to a fixed [1, 2, 3, 4], or cap the count below the
    weight's need (MAX_SHARDS = 4)."""
    target, most = _constants()
    proc, outputs = _count(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    shards = json.loads(outputs["shards"])
    assert shards == list(range(1, len(shards) + 1)), shards
    assert 4 < len(shards) <= most, shards
    weights = json.loads(DURATIONS.read_text())
    median = sorted(weights.values())[len(weights) // 2]
    loads = [sum(weights.get(f, median) for f in shard) for shard in _all_shards(tmp_path)]
    assert len(loads) == len(shards)
    assert max(loads) <= 1.5 * target, (loads, target)


def test_a_small_selection_takes_one_runner_not_four(tmp_path):
    """Runners were the largest share of pull-request wall time (docs/ci.md,
    "Where pull-request CI time goes"): a selection that is one target's
    worth runs on one shard. MUTATION: hand every run MAX_SHARDS."""
    tests = "tests/unit/scripts/test_unit_shards.py tests/unit/scripts/test_ci_reuse.py"
    proc, outputs = _count(tmp_path, mode="selected", tests=tests)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads(outputs["shards"]) == [1], outputs


def test_a_selection_is_split_by_its_own_weight(tmp_path):
    """MUTATION: size every selected run by the whole suite's weight."""
    target, most = _constants()
    weights = json.loads(DURATIONS.read_text())
    heavy = sorted((f for f in weights if (REPO / f).is_file()), key=lambda f: -weights[f])
    chosen, total = [], 0.0
    for f in heavy:
        if total > 2.2 * target:
            break
        chosen.append(f)
        total += weights[f]
    proc, outputs = _count(tmp_path, mode="selected", tests=" ".join(chosen))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    n = len(json.loads(outputs["shards"]))
    assert 2 <= n < most, (n, total)


def test_a_subset_is_sized_by_the_files_that_read_the_area(tmp_path):
    """MUTATION: size a docs-only run as the whole suite."""
    whole = tmp_path / "all"
    whole.mkdir()
    proc, all_outputs = _count(whole)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    proc, outputs = _count(tmp_path, mode="subset", areas="docs")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert 1 <= len(json.loads(outputs["shards"])) < len(json.loads(all_outputs["shards"])), outputs


@pytest.mark.parametrize("mode, areas, tests", [
    ("none", "", ""), ("subset", "", ""), ("subset", "terraform", ""), ("bogus", "", ""), ("selected", "", ""),
])
def test_the_count_refuses_what_it_does_not_model(tmp_path, mode, areas, tests):
    """A broken gate is never "one shard of nothing". `none` never reaches the
    step's output either: the job is skipped. MUTATION: answer [1] to anything."""
    proc, outputs = _count(tmp_path, mode=mode, areas=areas, tests=tests)
    if mode == "none":
        assert proc.returncode == 0 and outputs.get("shards") == "[1]", (proc.stderr, outputs)
    else:
        assert proc.returncode != 0, proc.stdout
        assert "shards" not in outputs


def _pick(tmp_path: Path, shard: int, shards: int, *, mode: str = "all", areas: str = "",
          cwd: Path = REPO, durations: Path = DURATIONS) -> tuple[subprocess.CompletedProcess[str], list[str], dict]:
    # The workflow's pick step runs this script from the checkout
    # (test_the_python_shard_count_is_written_once holds that); a scratch tree
    # (`cwd`) has no scripts/, so the same script runs from here against it.
    files = tmp_path / f"shard-{shard}"
    output = tmp_path / f"output-{shard}"
    output.write_text("")
    env = {**os.environ, "SHARD": str(shard), "SHARDS": str(shards), "PYTHON_MODE": mode,
           "PYTHON_AREAS": areas, "DURATIONS": str(durations), "SHARD_FILES": str(files),
           "GITHUB_OUTPUT": str(output)}
    proc = subprocess.run(["python3", str(UNIT_SHARDS), "pick"], cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=120)
    picked = files.read_text().split() if files.exists() else []
    outputs = dict(l.split("=", 1) for l in output.read_text().splitlines() if "=" in l)
    return proc, picked, outputs


def _all_shards(tmp_path: Path, **kwargs) -> list[list[str]]:
    counted = tmp_path / "count"
    counted.mkdir()
    proc, outputs = _count(counted, **{k: v for k, v in kwargs.items() if k in ("mode", "areas", "cwd", "durations")})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    shards = len(json.loads(outputs["shards"]))
    out = []
    for k in range(1, shards + 1):
        sub = tmp_path / f"k{k}"
        sub.mkdir()
        proc, picked, outputs = _pick(sub, k, shards, **kwargs)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert outputs.get("count") == str(len(picked)), outputs
        out.append(picked)
    return out


def test_the_shards_cover_every_test_file_exactly_once(tmp_path):
    """MUTATION: pick by `k == shard` off by one, or drop files with no weight."""
    shards = _all_shards(tmp_path)
    every = [f for shard in shards for f in shard]
    assert len(every) == len(set(every)), "a file runs in two shards"
    assert set(every) == _test_files(), set(every) ^ _test_files()
    assert all(shards), "a shard was handed nothing on the full suite"


def test_the_shards_are_balanced_by_the_stored_weights(tmp_path):
    """~1/N of the measured time each, not ~1/N of the files: one file
    (test_offboard_tenant.py) is 12 % of the suite. MUTATION: split by hash or
    round-robin (measured 2026-10-07 at 1.20-1.59x the even share)."""
    weights = json.loads(DURATIONS.read_text())
    median = sorted(weights.values())[len(weights) // 2]
    loads = [sum(weights.get(f, median) for f in shard) for shard in _all_shards(tmp_path)]
    assert max(loads) <= 1.10 * (sum(loads) / len(loads)), loads


def test_the_weights_name_test_files():
    """The weights file is well-formed: every key a unit test path, every
    value a positive number. How MANY files it covers is not asserted here:
    stale weights unbalance the shards but never drop a file (an unknown file
    weighs the median), and an 80 % floor here turned python-unit red on main
    and every pull request whenever new tests outran a manual refresh
    (1069/1337 at 82638be8, #2364). The count reports it instead
    (test_a_quarter_unweighted_still_shards_under_budget_and_warns).
    MUTATION: weigh a file 0, or key one outside tests/unit."""
    weights = json.loads(DURATIONS.read_text())
    assert all(re.fullmatch(r"tests/unit/[\w/.-]+\.py", k) for k in weights), [k for k in weights][:5]
    assert all(isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 for v in weights.values())


#: changed-guards.sh holds each pytest call to this; a shard must fit in one.
CALL_BUDGET_SECONDS = 540


def _weighted_tree(root: Path, files: int, weighted: int) -> Path:
    _tree(root, {f"tests/unit/w/test_{i:02d}.py": "def test_x(): pass\n" for i in range(files)})
    durations = root / "durations.json"
    durations.write_text(json.dumps({f"tests/unit/w/test_{i:02d}.py": 4.0 + i % 3 for i in range(weighted)}))
    return durations


def test_a_quarter_unweighted_still_shards_under_budget_and_warns(tmp_path):
    """#2364. A quarter of the files with no weight is a stale weights file,
    not a broken gate: the count and every pick succeed, every file runs once,
    no shard is over budget, and the count says the weights want refreshing.
    MUTATION: fail the count below 80 %, or say nothing."""
    root = tmp_path / "tree"
    durations = _weighted_tree(root, 40, 30)
    counted = tmp_path / "counted"
    counted.mkdir()
    proc, outputs = _count(counted, cwd=root, durations=durations)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "::warning title=stale shard weights::30 of 40 test files weighted (75%)" in proc.stdout, proc.stdout
    assert "docs/ci.md#refreshing-the-shard-weights" in proc.stdout, proc.stdout
    assert "30 of 40 test files weighted (75%)" in (counted / "count-summary").read_text()
    picks = tmp_path / "picks"
    picks.mkdir()
    shards = _all_shards(picks, cwd=root, durations=durations)
    every = [f for shard in shards for f in shard]
    assert sorted(every) == sorted(_test_files(root)), every
    weights = json.loads(durations.read_text())
    median = sorted(weights.values())[len(weights) // 2]
    target, _ = _constants()
    loads = [sum(weights.get(f, median) for f in shard) for shard in shards]
    assert max(loads) <= min(CALL_BUDGET_SECONDS, 1.5 * target), loads


def test_well_weighted_tree_does_not_warn(tmp_path):
    """At 80 % coverage and above the count says nothing about the weights.
    MUTATION: warn on every run."""
    root = tmp_path / "tree"
    durations = _weighted_tree(root, 40, 32)
    proc, outputs = _count(tmp_path, cwd=root, durations=durations)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "::warning" not in proc.stdout, proc.stdout
    assert not (tmp_path / "count-summary").exists()


@pytest.mark.parametrize(
    "areas, present, absent",
    [
        ("docs", ["tests/unit/scripts/test_docs_describe_what_was_built.py",
                  "tests/unit/scripts/test_docs_spec_amendments.py",
                  "tests/unit/scripts/test_ci_gate.py"],
         ["tests/unit/scheduler/test_gke_browser_pod_dns.py"]),
        ("swarm-ui", ["tests/unit/control_plane/test_runtimes_screen.py",
                      "tests/unit/scripts/test_issue_forms.py",
                      "tests/unit/scripts/test_ui_node_line.py"],
         ["tests/unit/scheduler/test_gke_browser_pod_dns.py",
          "tests/unit/scripts/test_docs_spec_amendments.py"]),
    ],
)
def test_a_subset_runs_the_tests_that_read_the_area_and_only_those(tmp_path, areas, present, absent):
    """UI-only skips the Python unit suite EXCEPT the tests that read UI
    sources (and docs-only, those that read docs/). MUTATION: run nothing on a
    subset, or everything."""
    every = [f for shard in _all_shards(tmp_path, mode="subset", areas=areas) for f in shard]
    assert len(every) == len(set(every))
    for path in present:
        assert path in every, path
    for path in absent:
        assert path not in every, path
    assert 0 < len(every) < len(_test_files()) / 2, len(every)


def _tree(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def test_a_test_that_reads_the_area_through_a_helper_or_conftest_is_in_the_subset(tmp_path):
    """MUTATION: match test files' own text only, follow only non-test
    helpers, or match prose mentions or a dict key."""
    root = tmp_path / "tree"
    _tree(root, {
        "tests/unit/a/helper_reader.py": 'DOC = "docs/ci.md"\n',
        # Prose is not a read.
        "tests/unit/a/test_prose.py": '"""See docs/ci.md (and swarm-ui)."""\n',
        # A test module used as a helper carries its reads too.
        "tests/unit/d/test_reader.py": 'X = REPO / "docs"\n',
        "tests/unit/e/test_borrower.py": "from d.test_reader import X\n",
        "tests/unit/a/test_uses_helper.py": "from .helper_reader import DOC\n",
        "tests/unit/a/test_plain.py": "def test_x(): pass\n",
        "tests/unit/b/conftest.py": 'ROOT = REPO / "docs"\n',
        # A dict key is not a path.
        "tests/unit/f/test_firestore_fake.py": 'state = {"docs": {}}\n',
        "tests/unit/b/test_under_conftest.py": "def test_y(): pass\n",
        "tests/unit/c/test_direct.py": 'P = REPO / "docs" / "x.md"\n',
        "tests/unit/c/test_other.py": "def test_z(): pass\n",
    })
    durations = tmp_path / "durations.json"
    durations.write_text("{}")
    picked = []
    for k in (1, 2):
        sub = tmp_path / f"k{k}"
        sub.mkdir()
        proc, files, _ = _pick(sub, k, 2, mode="subset", areas="docs", cwd=root, durations=durations)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        picked += files
    assert sorted(picked) == [
        "tests/unit/a/test_uses_helper.py",
        "tests/unit/b/test_under_conftest.py",
        "tests/unit/c/test_direct.py",
        "tests/unit/d/test_reader.py",
        "tests/unit/e/test_borrower.py",
    ], picked


def test_a_shard_left_with_no_file_runs_no_pytest(tmp_path):
    """`pytest` with no file argument runs testpaths -- the whole suite. A
    subset with fewer files than shards leaves some empty; those must say
    count=0 and the pytest step must not run. MUTATION: drop the step's if."""
    root = tmp_path / "tree"
    _tree(root, {"tests/unit/a/test_one.py": 'X = "docs/"\n', "tests/unit/a/test_two.py": "pass\n"})
    durations = tmp_path / "durations.json"
    durations.write_text("{}")
    counts = []
    for k in (1, 2, 3):
        sub = tmp_path / f"k{k}"
        sub.mkdir()
        proc, files, outputs = _pick(sub, k, 3, mode="subset", areas="docs", cwd=root, durations=durations)
        assert proc.returncode == 0, proc.stderr
        counts.append(outputs["count"])
    assert sorted(counts) == ["0", "0", "1"], counts
    (unit,) = [s for s in _jobs()["python-unit"]["steps"] if "pytest" in str(s.get("run", ""))
               and "junit" in str(s.get("run", ""))]
    assert unit.get("if") == "steps.pick.outputs.count != '0'", unit.get("if")
    assert '"${files[@]}"' in unit["run"], unit["run"]
    assert (unit.get("env") or {}).get("SWARM_REQUIRE_REAL_REAP") == "1"


def test_a_shards_workers_steal_rather_than_split_up_front():
    """xdist's default `load` schedule left shard 3 ~45 % over its floor: its
    long sleep-bound tests landed on one worker (docs/ci.md, "Where
    pull-request CI time goes (2026-10-09)"). MUTATION: drop `--dist
    worksteal`, or let a second pytest run in the job without it.

    Two runs since 2026-10-14: the gating one and the quarantined tests'
    (tests/flaky.txt), which reports and never fails the gate."""
    runs = [str(s.get("run", "")) for s in _jobs()["python-unit"]["steps"]]
    pytests = [r for r in runs if "uv run pytest" in r]
    assert len(pytests) == 2, pytests
    for run in pytests:
        assert re.search(r"uv run pytest\b[^\n]*-n auto --dist worksteal\b", run), run


def test_a_hung_shard_fails_and_names_the_test_that_hung():
    """On 2026-10-14 eight unit shards sat "in progress" for 51-306 minutes, and
    `build images` (which needs this job) never started, so release.yml timed
    out waiting for it. MUTATION: drop the job's timeout-minutes, raise it past
    an hour, or drop faulthandler_timeout from either pytest step."""
    job = _jobs()["python-unit"]
    assert 0 < int(job.get("timeout-minutes", 0)) <= 60, job.get("timeout-minutes")
    runs = [str(s.get("run", "")) for s in job["steps"]]
    pytests = [r for r in runs if "uv run pytest" in r]
    assert len(pytests) == 2, pytests
    for run in pytests:
        m = re.search(r"-o faulthandler_timeout=(\d+)\b", run)
        assert m and int(m.group(1)) < int(job["timeout-minutes"]) * 60, run


@pytest.mark.parametrize("mode, areas", [("none", ""), ("subset", ""), ("subset", "terraform"), ("bogus", "")])
def test_the_pick_refuses_what_it_does_not_model(tmp_path, mode, areas):
    """A mode or area it does not know is a broken gate, never "run nothing"."""
    proc, picked, outputs = _pick(tmp_path, 1, 4, mode=mode, areas=areas)
    assert proc.returncode != 0, proc.stdout
    assert "count" not in outputs and not picked


def test_a_shard_that_does_not_exist_is_refused(tmp_path):
    proc, _, _ = _pick(tmp_path, 5, 4)
    assert proc.returncode != 0


# ---------------------------------------------------------------------------
# The vitest shards.
# ---------------------------------------------------------------------------
def test_vitest_is_sharded_over_the_matrix_by_vitest_itself():
    """vitest's own `--shard=k/N` runs every test file in exactly one shard.
    MUTATION: hard-code `/4`, or run `npm test` (all three runners) per shard."""
    shards = _shards("ui-tests")
    assert shards == list(range(1, len(shards) + 1)) and len(shards) >= 2, shards
    steps = [s for s in _jobs()["ui-tests"]["steps"] if "test:components" in str(s.get("run", ""))]
    assert len(steps) == 1, steps
    step = steps[0]
    assert step["run"].strip() == 'npm run test:components -- --shard="${SHARD}/${SHARDS}"', step["run"]
    assert step["env"] == {"SHARD": "${{ matrix.shard }}", "SHARDS": "${{ strategy.job-total }}"}, step["env"]
    assert step.get("working-directory") == "apps/swarm-ui"
    assert _jobs()["ui-tests"]["strategy"].get("fail-fast") is False


def test_the_npm_scripts_split_without_changing_npm_test():
    """`npm test` (scripts/ui-component-test.sh, a developer) still runs all
    three runners; CI runs vitest and the node runners as separate scripts so
    `--shard` reaches vitest alone. MUTATION: put a second runner in
    test:components -- `--` would hand the flag to the last one."""
    scripts = json.loads(UI_PACKAGE.read_text())["scripts"]
    assert scripts["test:components"] == "vitest run", scripts
    assert scripts["test"] == "npm run test:components && npm run test:node", scripts
    assert "vitest" not in scripts["test:node"] and "tests/run.mjs" in scripts["test:node"], scripts
    assert "test/*.test.mjs" in scripts["test:node"], scripts


def test_the_ui_build_job_runs_what_is_not_sharded():
    runs = [str(s.get("run", "")).strip() for s in _jobs()["ui-build"]["steps"]]
    for command in ("npm run typecheck", "npm run test:node", "npm run build"):
        assert command in runs, command
