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
  * the shard count is written once, in the matrix;
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

REPO = Path(__file__).resolve().parents[3]
APPLICATION = REPO / ".github" / "workflows" / "application.yml"
DURATIONS = REPO / "tests" / "unit" / "scripts" / "unit_test_durations.json"
UI_PACKAGE = REPO / "apps" / "swarm-ui" / "package.json"

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
    runs = "\n".join(str(s.get("run", "")) for s in _jobs()["python-checks"]["steps"])
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
    """The step reads `strategy.job-total`, so the matrix is the one place.
    MUTATION: write SHARDS: 4 in the step."""
    shards = _shards("python-unit")
    assert shards == list(range(1, len(shards) + 1)) and len(shards) >= 2, shards
    env = _step("python-unit", "pick").get("env") or {}
    assert env.get("SHARD") == "${{ matrix.shard }}", env
    assert env.get("SHARDS") == "${{ strategy.job-total }}", env
    assert _jobs()["python-unit"]["strategy"].get("fail-fast") is False


def _pick(tmp_path: Path, shard: int, shards: int, *, mode: str = "all", areas: str = "",
          cwd: Path = REPO, durations: Path = DURATIONS) -> tuple[subprocess.CompletedProcess[str], list[str], dict]:
    step = _step("python-unit", "pick")
    body = tmp_path / "pick.sh"
    body.write_text(step["run"])
    files = tmp_path / f"shard-{shard}"
    output = tmp_path / f"output-{shard}"
    output.write_text("")
    env = {**os.environ, "SHARD": str(shard), "SHARDS": str(shards), "PYTHON_MODE": mode,
           "PYTHON_AREAS": areas, "DURATIONS": str(durations), "SHARD_FILES": str(files),
           "GITHUB_OUTPUT": str(output)}
    proc = subprocess.run(["bash", "-e", str(body)], cwd=cwd, env=env, capture_output=True, text=True, timeout=120)
    picked = files.read_text().split() if files.exists() else []
    outputs = dict(l.split("=", 1) for l in output.read_text().splitlines() if "=" in l)
    return proc, picked, outputs


def _all_shards(tmp_path: Path, **kwargs) -> list[list[str]]:
    shards = len(_shards("python-unit"))
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


def test_the_weights_name_test_files_and_cover_most_of_them():
    """Stale weights unbalance the shards but never drop a file (an unknown
    file weighs the median). Below 80 % coverage they are worth refreshing
    (docs/ci.md, "Refreshing the shard weights")."""
    weights = json.loads(DURATIONS.read_text())
    assert all(re.fullmatch(r"tests/unit/[\w/.-]+\.py", k) for k in weights), [k for k in weights][:5]
    assert all(isinstance(v, (int, float)) and v > 0 for v in weights.values())
    files = _test_files()
    assert len(files & set(weights)) >= 0.8 * len(files), len(files & set(weights))


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
    worksteal`, or let a second pytest run in the job without it."""
    runs = [str(s.get("run", "")) for s in _jobs()["python-unit"]["steps"]]
    pytests = [r for r in runs if "uv run pytest" in r]
    assert len(pytests) == 1, pytests
    assert re.search(r"uv run pytest\b[^\n]*-n auto --dist worksteal\b", pytests[0]), pytests[0]


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
