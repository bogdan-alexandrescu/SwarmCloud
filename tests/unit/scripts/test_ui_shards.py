"""The swarm-ui component tests run in shards sized and split by measured weight.

OWNER REQUEST, 2026-11-03 (part of #2688). Eight pull request runs of
application.yml: vitest's own `--shard=k/4` splits by file count, and its
slowest shard took 15-25 minutes while the others took 10-14 -- eight
spacing.*.test.tsx slices weigh 259-485 s each against ~2.9 s for a typical
file, and the shard that drew several of them set the merge queue's pace.
scripts/lib/ui_shards.py does what scripts/lib/unit_shards.py does for the
Python unit tests (test_unit_shards.py): a count from the measured weight in
the `changes` job, and a deterministic longest-first split every shard computes
the same way.

What is asserted here, by running the script rather than matching its text:

  * the shards cover exactly the files vitest's `include` globs name, each in
    exactly one shard, balanced by tests/unit/scripts/ui_test_durations.json;
  * the split is deterministic, whatever order the files are found in;
  * a file with no recorded weight (a new one) runs, at the default weight;
  * files where one path contains another share a shard, because vitest reads
    a file argument as a substring filter;
  * the workflow computes the count once, in `changes`, and each shard hands
    vitest its own files and never an empty list.
"""

from __future__ import annotations

import importlib.util
import json
import math
import os
import random
import re
import subprocess
from pathlib import Path

import pytest
import yaml

from .test_fast_checks_on_lane_prs import without_lane_clause

REPO = Path(__file__).resolve().parents[3]
APPLICATION = REPO / ".github" / "workflows" / "application.yml"
UI = REPO / "apps" / "swarm-ui"
DURATIONS = REPO / "tests" / "unit" / "scripts" / "ui_test_durations.json"
UI_SHARDS = REPO / "scripts" / "lib" / "ui_shards.py"


def _module():
    spec = importlib.util.spec_from_file_location("ui_shards_under_test", UI_SHARDS)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _vitest_files(ui: Path = UI) -> set[str]:
    """What vitest runs, read independently: the include globs in the config."""
    text = (ui / "vitest.config.ts").read_text()
    line = next(l for l in text.splitlines() if l.strip().startswith("include:"))
    globs = [g.strip(" '\"") for g in line.split("[", 1)[1].rsplit("]", 1)[0].split(",") if g.strip()]
    assert globs, line
    return {p.relative_to(ui).as_posix() for g in globs for p in ui.glob(g)
            if "node_modules" not in p.parts}


def _env(tmp_path: Path, durations: Path, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k != "GITHUB_STEP_SUMMARY"}
    env.update(DURATIONS=str(durations), GITHUB_OUTPUT=str(tmp_path / "output"), **extra)
    return env


def _outputs(tmp_path: Path) -> dict[str, str]:
    path = tmp_path / "output"
    text = path.read_text() if path.exists() else ""
    return dict(l.split("=", 1) for l in text.splitlines() if "=" in l)


def _count(tmp_path: Path, *, cwd: Path = REPO, durations: Path = DURATIONS):
    tmp_path.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(["python3", str(UI_SHARDS), "count"], cwd=cwd, env=_env(tmp_path, durations),
                          capture_output=True, text=True, timeout=120)
    return proc, _outputs(tmp_path)


def _pick(tmp_path: Path, shard: int, shards: int, *, cwd: Path = REPO, durations: Path = DURATIONS):
    tmp_path.mkdir(parents=True, exist_ok=True)
    files = tmp_path / "shard-files"
    env = _env(tmp_path, durations, SHARD=str(shard), SHARDS=str(shards), SHARD_FILES=str(files))
    proc = subprocess.run(["python3", str(UI_SHARDS), "pick"], cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=120)
    picked = files.read_text().splitlines() if files.exists() else []
    return proc, picked, _outputs(tmp_path)


def _all_shards(tmp_path: Path, **kwargs) -> list[list[str]]:
    proc, outputs = _count(tmp_path / "count", **kwargs)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    shards = json.loads(outputs["shards"])
    assert shards == list(range(1, len(shards) + 1)), shards
    out = []
    for k in shards:
        proc, picked, outputs = _pick(tmp_path / f"k{k}", k, len(shards), **kwargs)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert outputs.get("count") == str(len(picked)), outputs
        out.append(picked)
    return out


def _load(shard: list[str], weights: dict[str, float], default: float) -> float:
    return sum(weights.get(f, default) for f in shard)


def _tree(root: Path, files: list[str], weights: dict[str, float]) -> Path:
    """A scratch checkout: apps/swarm-ui with the real config and these files."""
    ui = root / "apps" / "swarm-ui"
    ui.mkdir(parents=True)
    (ui / "vitest.config.ts").write_text((UI / "vitest.config.ts").read_text())
    for name in files:
        path = ui / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
    durations = root / "durations.json"
    durations.write_text(json.dumps(weights))
    return durations


# ---------------------------------------------------------------------------
# The real suite.
# ---------------------------------------------------------------------------
def test_the_shards_cover_every_vitest_file_exactly_once(tmp_path):
    """MUTATION: pick `parts[shard]` (off by one), drop a file with no weight,
    or list `*.test.ts` alone."""
    shards = _all_shards(tmp_path)
    every = [f for shard in shards for f in shard]
    assert len(every) == len(set(every)), "a file runs in two shards"
    assert set(every) == _vitest_files(), set(every) ^ _vitest_files()
    assert all(shards), "a shard was handed nothing: vitest would exit 'No test files found'"


def test_the_count_follows_the_weight_up_to_the_cap(tmp_path):
    """About SHARD_TARGET_SECONDS a shard, at most MAX_SHARDS. MUTATION: go
    back to a fixed four, or ignore the weights."""
    mod = _module()
    weights = json.loads(DURATIONS.read_text())
    total = sum(weights.get(f, mod.VITEST_DEFAULT_SECONDS) for f in _vitest_files())
    proc, outputs = _count(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    shards = json.loads(outputs["shards"])
    assert len(shards) == min(mod.MAX_SHARDS, math.ceil(total / mod.SHARD_TARGET_SECONDS)), (shards, total)
    assert mod.SHARD_TARGET_SECONDS == 600 and mod.MAX_SHARDS == 8


CI_DOC = REPO / "docs" / "ci.md"
CI_DOC_SECTION = "### The component-test shards are sized by measured weight"


def _ci_doc_shard_figure(name: str, unit: str = "") -> int:
    """The figure docs/ci.md's component-shard section gives `name`, found by
    the section's own wording ("`NAME` = <n>"), not by line number."""
    text = CI_DOC.read_text()
    assert CI_DOC_SECTION in text, f"{CI_DOC_SECTION!r} is gone from docs/ci.md"
    section = text.split(CI_DOC_SECTION, 1)[1]
    section = re.split(r"^#{1,3} ", section, maxsplit=1, flags=re.M)[0]
    found = re.findall(rf"`{name}`\s*=\s*(\d[\d,]*){unit}", " ".join(section.split()))
    assert len(found) == 1, f"docs/ci.md's component-shard section should state `{name}` = <n> once: {found}"
    return int(found[0].replace(",", ""))


def test_the_ci_doc_states_the_shard_target_and_cap_the_script_uses():
    """docs/ci.md says ~600 s a shard and at most eight shards; those are the
    script's figures. MUTATION: change SHARD_TARGET_SECONDS or MAX_SHARDS in
    ui_shards.py, or either figure in the doc."""
    mod = _module()
    assert _ci_doc_shard_figure("SHARD_TARGET_SECONDS", r"\s*s\b") == mod.SHARD_TARGET_SECONDS
    assert _ci_doc_shard_figure("MAX_SHARDS") == mod.MAX_SHARDS


def test_the_shards_are_balanced_by_the_stored_weights(tmp_path):
    """~1/N of the measured time each, not ~1/N of the files: the heaviest
    shard is the heavier of one indivisible file and 1.1x the mean. MUTATION:
    split round-robin, or by count as `--shard` did."""
    mod = _module()
    weights = json.loads(DURATIONS.read_text())
    loads = [_load(s, weights, mod.VITEST_DEFAULT_SECONDS) for s in _all_shards(tmp_path)]
    heaviest_file = max(weights.values())
    assert max(loads) <= max(heaviest_file, 1.10 * sum(loads) / len(loads)), loads


def test_the_heavy_slices_do_not_share_a_shard(tmp_path):
    """The defect measured on 2026-11-03: the shard that drew several
    spacing slices ran 15-25 minutes. With a shard for each, none doubles up."""
    weights = json.loads(DURATIONS.read_text())
    shards = _all_shards(tmp_path)
    heavy = sorted(weights, key=weights.get, reverse=True)[:len(shards)]
    holders = [next(k for k, s in enumerate(shards) if f in s) for f in heavy if f in _vitest_files()]
    assert len(holders) == len(set(holders)), dict(zip(heavy, holders))


def test_the_weights_name_vitest_files():
    """A recorded file that no longer exists weighs nothing anywhere; a key
    off apps/swarm-ui's relative paths would never match."""
    weights = json.loads(DURATIONS.read_text())
    assert weights and all(isinstance(v, (int, float)) and v > 0 for v in weights.values()), weights
    assert set(weights) <= _vitest_files(), set(weights) - _vitest_files()


# ---------------------------------------------------------------------------
# The split, on scratch trees.
# ---------------------------------------------------------------------------
def test_the_split_is_deterministic_whatever_order_the_files_come_in():
    """Every shard computes the split on its own runner: the same input must
    give the same parts. MUTATION: break ties by set order or by load alone."""
    mod = _module()
    files = [f"src/__tests__/f{i:03d}.test.tsx" for i in range(60)]
    weights = {f: float(i % 7 + 1) for i, f in enumerate(files)}
    first, load = mod.split(mod.units(files), 5, weights)
    for seed in range(5):
        shuffled = files[:]
        random.Random(seed).shuffle(shuffled)
        assert mod.split(mod.units(shuffled), 5, weights) == (first, load)


def test_a_new_file_with_no_weight_runs_in_exactly_one_shard(tmp_path):
    """A file added after the last weight refresh is placed at the default
    weight. MUTATION: drop files with no recorded weight."""
    heavy = {f"src/__tests__/heavy{i}.test.tsx": 300.0 for i in range(3)}
    light = [f"src/c{i}.test.ts" for i in range(30)]
    new = "src/__tests__/brand-new.test.tsx"
    durations = _tree(tmp_path / "tree", [*heavy, *light, new], heavy)
    shards = _all_shards(tmp_path / "run", cwd=tmp_path / "tree", durations=durations)
    assert sum(new in s for s in shards) == 1, shards
    assert len(shards) == 2, shards  # ~990 s at a 600 s target
    every = [f for s in shards for f in s]
    assert sorted(every) == sorted([*heavy, *light, new])


def test_files_one_path_contains_share_a_shard(tmp_path):
    """vitest's filter is a case-insensitive substring: `src/a.test.ts` also
    runs `src/a.test.tsx`, and `src/Nav.test.tsx` `src/nav.test.tsx`. Split
    apart, each would run twice. MUTATION: split file by file."""
    files = ["src/a.test.ts", "src/a.test.tsx", "src/Nav.test.tsx", "src/nav.test.tsx",
             *[f"src/z{i}.test.tsx" for i in range(8)]]
    weights = {f: 400.0 for f in files}
    durations = _tree(tmp_path / "tree", files, weights)
    shards = _all_shards(tmp_path / "run", cwd=tmp_path / "tree", durations=durations)
    assert len(shards) == 8, shards
    for pair in (("src/a.test.ts", "src/a.test.tsx"), ("src/Nav.test.tsx", "src/nav.test.tsx")):
        assert any(set(pair) <= set(s) for s in shards), shards
    for shard in shards:
        for f in shard:
            reached = [g for other in shards if other is not shard for g in other if f.lower() in g.lower()]
            assert not reached, (f, reached)


def test_a_small_suite_never_gets_a_shard_with_nothing(tmp_path):
    """Two files of 1,000 s want four shards by weight; a third and fourth
    shard would run nothing. MUTATION: drop the unit cap from the count."""
    files = ["src/one.test.tsx", "src/two.test.tsx"]
    durations = _tree(tmp_path / "tree", files, {f: 1000.0 for f in files})
    proc, outputs = _count(tmp_path / "count", cwd=tmp_path / "tree", durations=durations)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads(outputs["shards"]) == [1, 2], outputs
    proc, picked, _ = _pick(tmp_path / "pick", 3, 3, cwd=tmp_path / "tree", durations=durations)
    assert proc.returncode != 0 and not picked, proc.stdout + proc.stderr


def test_files_outside_the_include_globs_or_under_node_modules_do_not_run(tmp_path):
    files = ["src/a.test.tsx", "src/node_modules/x/b.test.tsx", "test/runner.test.mjs", "src/c.spec.tsx"]
    durations = _tree(tmp_path / "tree", files, {})
    shards = _all_shards(tmp_path / "run", cwd=tmp_path / "tree", durations=durations)
    assert shards == [["src/a.test.tsx"]], shards


@pytest.mark.parametrize("shard, shards", [(0, 4), (5, 4)])
def test_a_shard_that_does_not_exist_is_refused(tmp_path, shard, shards):
    proc, picked, _ = _pick(tmp_path, shard, shards)
    assert proc.returncode != 0 and not picked, proc.stdout + proc.stderr


# ---------------------------------------------------------------------------
# The workflow.
# ---------------------------------------------------------------------------
def _jobs() -> dict:
    return yaml.safe_load(APPLICATION.read_text())["jobs"]


def _step(job_id: str, step_id: str) -> dict:
    found = [s for s in _jobs()[job_id]["steps"] if s.get("id") == step_id]
    assert len(found) == 1, f"{job_id} has no single step with id {step_id}"
    return found[0]


def test_the_ui_shard_count_is_written_once_in_the_changes_job():
    """MUTATION: put `[1, 2, 3, 4]` back in the matrix, or write SHARDS: 4."""
    changes = _jobs()["changes"]
    assert changes["outputs"].get("ui_shards") == "${{ steps.ui_shards.outputs.shards }}", changes["outputs"]
    step = _step("changes", "ui_shards")
    assert step["run"].strip().splitlines()[-1].strip() == "python3 scripts/lib/ui_shards.py count", step["run"]
    assert (step.get("env") or {}).get("DURATIONS") == "tests/unit/scripts/ui_test_durations.json", step
    job = _jobs()["ui-tests"]
    assert job["strategy"]["matrix"]["shard"] == "${{ fromJSON(needs.changes.outputs.ui_shards) }}", job["strategy"]
    assert job["strategy"].get("fail-fast") is False
    # The reuse rule and the path gate are kept as they were, behind the
    # fast-checks-only rule (test_fast_checks_on_lane_prs.py).
    assert without_lane_clause(job["if"]) == ("needs.changes.outputs.full == 'true' && needs.changes.outputs.ui == 'true' && "
                         "!contains(fromJSON(needs.changes.outputs.reused || '[]'), 'ui-tests')"), job["if"]


def test_each_shard_hands_vitest_its_own_files():
    """MUTATION: keep `--shard`, read another file than the pick wrote, or run
    the vitest step before the pick."""
    pick = _step("ui-tests", "pick")
    env = pick.get("env") or {}
    assert env.get("SHARD") == "${{ matrix.shard }}" and env.get("SHARDS") == "${{ strategy.job-total }}", env
    assert env.get("DURATIONS") == "tests/unit/scripts/ui_test_durations.json", env
    assert pick["run"].strip().splitlines()[-1].strip() == "python3 scripts/lib/ui_shards.py pick", pick["run"]
    # A lane's pull request splits its selection the same way (ci-select.sh ui-pick).
    assert "./scripts/ci-select.sh ui-pick" in pick["run"], pick["run"]
    steps = [s for s in _jobs()["ui-tests"]["steps"] if "test:components" in str(s.get("run", ""))]
    assert len(steps) == 1, steps
    step = steps[0]
    assert step.get("working-directory") == "apps/swarm-ui"
    assert (step.get("env") or {}).get("SHARD_FILES") == env.get("SHARD_FILES"), (step.get("env"), env)
    assert step["run"].strip() == 'npm run test:components -- $(cat "${SHARD_FILES}")', step["run"]
    # Only after the pick passed, and never with no file: a lane's selection
    # that reaches no component test writes count=0, and `vitest run` with no
    # file argument would run the whole suite.
    assert step.get("if") == "steps.pick.outputs.count != '0'", step
    order = _jobs()["ui-tests"]["steps"]
    assert order.index(pick) < order.index(step)


def test_the_vitest_step_hands_npm_the_listed_files(tmp_path):
    """Run the step's script with a stand-in `npm` that records its arguments."""
    steps = [s for s in _jobs()["ui-tests"]["steps"] if "test:components" in str(s.get("run", ""))]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    record = tmp_path / "npm-args"
    npm = bin_dir / "npm"
    npm.write_text(f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "{record}"\n')
    npm.chmod(0o755)
    files = tmp_path / "shard-files"
    files.write_text("src/a.test.tsx\nsrc/__tests__/b.test.tsx\n")
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "SHARD_FILES": str(files)}
    proc = subprocess.run(["bash", "-e", "-c", steps[0]["run"]], cwd=tmp_path, env=env,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert record.read_text().splitlines() == ["run", "test:components", "--",
                                               "src/a.test.tsx", "src/__tests__/b.test.tsx"]


@pytest.mark.parametrize("name", ["src/a b.test.tsx", "src/[x].test.tsx", "src/*.test.tsx"])
def test_a_path_the_step_would_split_or_expand_is_refused(tmp_path, name):
    """The vitest step expands the list unquoted. MUTATION: drop UNSAFE."""
    durations = _tree(tmp_path / "tree", ["src/ok.test.tsx", name], {})
    proc, outputs = _count(tmp_path / "count", cwd=tmp_path / "tree", durations=durations)
    assert proc.returncode != 0 and "shards" not in outputs, proc.stdout + proc.stderr
