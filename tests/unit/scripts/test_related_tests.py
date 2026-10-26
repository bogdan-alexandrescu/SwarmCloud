"""`scripts/related-tests.sh` names the tests a change reaches, and only those
(RELATED-TESTS, part of #1694, owner decision 2026-10-20).

Measured 2026-10-19/20: 36 vitest runs with no file filter at about 22 min
each (2.05 h), 8 pytest runs over whole tests/unit directories (0.73 h), and
both out-of-memory kills from four vitest shards started in the background.
The helper prints the related test files and the commands that run them --
named files only, a large selection as a few runs one after another, never a
whole directory, a shard or a background batch.

The selector (scripts/lib/related_tests.py) is held against a synthetic tree
in tmp_path, so its answers are exact; the script is held end to end against
this checkout with `--files`, which reads a fixture diff instead of git.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "related-tests.sh"
SELECTOR = REPO / "scripts" / "lib" / "related_tests.py"
UI = "apps/swarm-ui"

PYTEST_CMD = "uv run pytest "
VITEST_CMD = f"(cd {UI} && npx vitest run "


def _first_effective_line(text: str) -> str:
    for line in text.splitlines()[1:]:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped
    return ""


def _write(root: Path, rel: str, text: str = "") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _tree(root: Path, *, many: int = 0) -> None:
    """A small repository: two UI modules with tests, two Python modules with
    tests, and a test that reads a module as text."""
    _write(root, f"{UI}/src/widget.ts", "export const w = 1\n")
    _write(root, f"{UI}/src/panel.tsx", "import { w } from './widget'\nexport const P = () => w\n")
    _write(root, f"{UI}/src/other.ts", "export const o = 2\n")
    _write(root, f"{UI}/src/__tests__/panel.test.tsx",
           "import { describe } from 'vitest'\nimport {\n  P,\n} from '../panel'\n")
    _write(root, f"{UI}/src/__tests__/other.test.tsx", "import { o } from '../other'\n")
    _write(root, f"{UI}/src/__tests__/mocked.test.ts", "vi.mock('../widget')\n")
    _write(root, f"{UI}/test/Live.test.tsx", "import '../src/widget.ts'\n")
    for i in range(many):
        _write(root, f"{UI}/src/__tests__/bulk{i:03d}.test.tsx", "import { w } from '../widget'\n")
    _write(root, "apps/foo/foo/__init__.py")
    _write(root, "apps/foo/foo/core.py", "X = 1\n")
    _write(root, "apps/foo/foo/edge.py", "from foo.core import X\n")
    _write(root, "apps/foo/foo/lone.py", "Y = 2\n")
    _write(root, "tests/unit/foo/test_core.py", "from foo.core import X\n")
    _write(root, "tests/unit/foo/test_edge.py", "from foo import edge\n")
    _write(root, "tests/unit/foo/test_lone.py", "from foo.lone import Y\n")
    _write(root, "tests/unit/foo/test_reads.py", 'TEXT = "apps/foo/foo/core.py"\n')
    _write(root, "tests/unit/control_plane/test_panel_text.py", 'SRC = "src/panel.tsx"\n')


def _select(root: Path, *changed: str) -> dict[str, list[str]]:
    listing = root / "changed.txt"
    listing.write_text("".join(f"{c}\n" for c in changed))
    result = subprocess.run(
        [sys.executable, str(SELECTOR), "--root", str(root), "--changed", str(listing)],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    out: dict[str, list[str]] = {"pytest": [], "vitest": [], "note": []}
    for line in result.stdout.splitlines():
        kind, _, rest = line.partition("\t")
        out[kind].append(rest)
    return out


def _files(runs: list[str]) -> list[str]:
    return sorted(f for run in runs for f in run.split())


def _run(*args: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "SWARM_CLONE_BASE"}
    env["NO_COLOR"] = "1"
    env.update(env_extra or {})
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=300,
    )


def _commands(out: str) -> list[str]:
    return [line for line in out.splitlines() if line.startswith((PYTEST_CMD, VITEST_CMD))]


def _listed(out: str) -> list[str]:
    return [line for line in out.splitlines() if line.startswith(("tests/", f"{UI}/"))]


def _assert_every_command_names_files(commands: list[str]) -> None:
    """The rule itself: no whole-suite, whole-directory, shard or background run."""
    assert commands
    for command in commands:
        # MUTATION: emit `npx vitest run` or `pytest tests/unit` with no file
        # arguments, a directory, `--shard` or a trailing `&` -> this fails.
        assert "--shard" not in command, command
        assert not command.rstrip().endswith("&"), command
        assert " & " not in command and "sleep" not in command, command
        if command.startswith(PYTEST_CMD):
            args = [a for a in command[len(PYTEST_CMD):].split() if not a.startswith("-") and a != "auto"]
            assert args, command
            for arg in args:
                assert arg.endswith(".py") and (REPO / arg).is_file(), (arg, command)
        else:
            args = command[len(VITEST_CMD):].rstrip(")").split()
            assert args, command
            for arg in args:
                assert re.search(r"\.test\.tsx?$", arg) and (REPO / UI / arg).is_file(), (arg, command)


# --- the script's shape ------------------------------------------------------


def test_the_script_has_the_house_shape():
    # MUTATION: delete scripts/related-tests.sh, drop `set -euo pipefail`, or
    # stop sourcing lib/common.sh -> this fails.
    text = SCRIPT.read_text()
    assert text.startswith("#!/usr/bin/env bash\n")
    assert _first_effective_line(text) == "set -euo pipefail"
    assert os.access(SCRIPT, os.X_OK)
    assert re.search(r'^source .*/lib/common\.sh"$', text, re.MULTILINE)
    assert "related_tests.py" in text


# --- the selector, on a synthetic tree ---------------------------------------


def test_a_ui_only_change_selects_the_vitest_files_that_import_it_directly_or_not(tmp_path):
    _tree(tmp_path)
    got = _select(tmp_path, f"{UI}/src/widget.ts")
    # MUTATION: follow only direct imports (no closure) -> panel.test.tsx is
    # lost; drop the `vi.mock` or side-effect `import '...'` patterns ->
    # mocked.test.ts or test/Live.test.tsx is lost; select every *.test.* ->
    # other.test.tsx appears.
    assert _files(got["vitest"]) == sorted([
        "src/__tests__/mocked.test.ts",
        "src/__tests__/panel.test.tsx",
        "test/Live.test.tsx",
    ])
    assert got["pytest"] == []


def test_a_ui_change_also_selects_the_python_tests_that_read_it_as_text(tmp_path):
    _tree(tmp_path)
    got = _select(tmp_path, f"{UI}/src/panel.tsx")
    assert _files(got["vitest"]) == ["src/__tests__/panel.test.tsx"]
    # MUTATION: drop the text-reader rule (names_path) -> this is empty.
    assert _files(got["pytest"]) == ["tests/unit/control_plane/test_panel_text.py"]


def test_a_python_only_change_selects_its_importers_and_readers_and_no_vitest(tmp_path):
    _tree(tmp_path)
    got = _select(tmp_path, "apps/foo/foo/core.py")
    # MUTATION: drop the import closure -> test_core.py and test_edge.py
    # (which reaches core through edge.py) are lost; select the whole of
    # tests/unit/foo -> test_lone.py appears.
    assert _files(got["pytest"]) == [
        "tests/unit/foo/test_core.py",
        "tests/unit/foo/test_edge.py",
        "tests/unit/foo/test_reads.py",
    ]
    assert got["vitest"] == []


def test_a_mixed_change_selects_both_sides_and_a_changed_test_selects_itself(tmp_path):
    _tree(tmp_path)
    got = _select(tmp_path, "apps/foo/foo/lone.py", f"{UI}/src/other.ts",
                  "tests/unit/foo/test_reads.py", f"{UI}/src/__tests__/panel.test.tsx")
    # MUTATION: drop either half, or the "a changed test file is related to
    # itself" rule -> one of these is missing.
    assert _files(got["pytest"]) == ["tests/unit/foo/test_lone.py", "tests/unit/foo/test_reads.py"]
    assert _files(got["vitest"]) == ["src/__tests__/other.test.tsx", "src/__tests__/panel.test.tsx"]


def test_a_deleted_file_selects_nothing_that_is_gone(tmp_path):
    _tree(tmp_path)
    got = _select(tmp_path, f"{UI}/src/__tests__/gone.test.tsx", "tests/unit/foo/test_gone.py")
    assert got["vitest"] == [] and got["pytest"] == []


def test_a_large_ui_selection_comes_back_as_a_few_bounded_runs(tmp_path):
    _tree(tmp_path, many=120)
    got = _select(tmp_path, f"{UI}/src/widget.ts")
    runs = got["vitest"]
    # 123 unrecorded files weigh 123 x 2.9 = ~357 s, inside one run's 540 s
    # budget: only the file cap cuts them, into ceil(123 / 50) = 3 runs.
    # MUTATION: return the selection as one run, or weigh runs by duration
    # alone and drop the file cap -> len(runs) == 1; open a run per file ->
    # len(runs) == 123.
    assert 3 <= len(runs) <= 5
    for run in runs:
        # MUTATION: drop the per-run cap -> a run holds more than
        # VITEST_FILES_PER_RUN files.
        assert 0 < len(run.split()) <= 50
    files = _files(runs)
    assert len(files) == len(set(files)) == 123


def _selector_module():
    """scripts/lib/related_tests.py, imported (it puts its own directory on
    sys.path for select_unit_tests)."""
    spec = importlib.util.spec_from_file_location("related_tests_under_test", SELECTOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


UI_DURATIONS = "tests/unit/scripts/ui_test_durations.json"
SPACING_SLICES = ("accounts", "admin", "capacity", "config", "runs", "setup", "submit", "work")
# The weights issue #2362 measured or bounded: 252-353 s a slice, and the
# lightest two measured (capacity 252.7 s, accounts 324.3 s on its quicker run)
# already add up past 540. These keep that property -- any two exceed 540 --
# so no two can share a run without one going over the budget.
SPACING_SECONDS = {"accounts": 353, "admin": 340, "capacity": 252, "config": 330,
                   "runs": 300, "setup": 295, "submit": 310, "work": 353}


def _spacing_selection(root: Path, extra_weights: dict[str, float] | None = None) -> list[str]:
    """The 9 spacing files (8 heavy slices plus the light partition test) and
    41 light files, 20 of them recorded at a second and 21 unrecorded; the
    weights written where vitest_runs reads them."""
    heavy = {f"src/__tests__/spacing.{g}.test.tsx": SPACING_SECONDS[g] for g in SPACING_SLICES}
    light = [f"src/__tests__/light{i:02d}.test.tsx" for i in range(41)]
    weights: dict[str, float] = {**heavy, **{t: 1.0 for t in light[:20]}, **(extra_weights or {})}
    _write(root, UI_DURATIONS, json.dumps(weights))
    return sorted([*heavy, "src/__tests__/spacing.test.tsx", *light, *(extra_weights or {})])


def test_heavy_spacing_slices_never_share_a_vitest_run(tmp_path):
    """Issue #2362: the 9 spacing files plus 41 light ones were one 50-file
    run, ~45 min of work under a 540 s command limit; 31 agents timed out."""
    module = _selector_module()
    tests = _spacing_selection(tmp_path)
    runs = module.vitest_runs(tmp_path, tests)
    heavy = {f"src/__tests__/spacing.{g}.test.tsx" for g in SPACING_SLICES}
    for run in runs:
        # MUTATION: cut runs by file count alone (VITEST_FILES_PER_RUN = 50,
        # today's rule) -> all eight slices share the one run.
        assert len(heavy & set(run)) <= 1, run
    # MUTATION: drop or duplicate a file while placing it -> red.
    placed = [t for run in runs for t in run]
    assert sorted(placed) == tests
    # MUTATION: stop returning each run's files sorted -> red.
    assert all(run == sorted(run) for run in runs)


def test_no_vitest_run_is_estimated_over_540_seconds(tmp_path):
    module = _selector_module()
    # One file heavier than the whole budget: it runs, alone.
    giant = "src/__tests__/giant.test.tsx"
    tests = _spacing_selection(tmp_path, {giant: 700})
    weights = json.loads((tmp_path / UI_DURATIONS).read_text())
    # MUTATION: a run budget other than 540 (RUN_SECONDS in related-tests.sh,
    # PYTEST_RUN_WEIGHT here) -> red.
    assert module.VITEST_RUN_WEIGHT == 540 == module.PYTEST_RUN_WEIGHT
    runs = module.vitest_runs(tmp_path, tests)
    for run in runs:
        estimate = sum(weights.get(t, module.VITEST_DEFAULT_SECONDS) for t in run)
        if giant in run:
            # MUTATION: let the lightest run take more files after a file that
            # alone is over the budget -> the giant shares its run.
            assert run == [giant], run
        else:
            # MUTATION: place each file on the lightest run without opening a
            # new one when it would go over 540 -> a run estimated over 540 s.
            assert estimate <= 540, (estimate, run)
    assert sorted(t for run in runs for t in run) == tests


def test_an_unrecorded_vitest_file_weighs_the_default(tmp_path):
    module = _selector_module()
    # MUTATION: change the default from the measured ~2.9 s a file -> red.
    assert module.VITEST_DEFAULT_SECONDS == 2.9
    recorded = "src/__tests__/recorded.test.tsx"
    unrecorded = "src/__tests__/unrecorded.test.tsx"
    # 537.0 + 2.9 = 539.9 fits one 540 s run; 537.2 + 2.9 = 540.1 does not.
    # So the pair's split shows the unrecorded file weighs 2.9, give or take
    # 0.1 s, through vitest_runs itself rather than a constant.
    _write(tmp_path, UI_DURATIONS, json.dumps({recorded: 537.0}))
    # MUTATION: weigh an unrecorded file 0 or the median of the recorded ones
    # (pytest_runs' rule) instead of 2.9 -> the next assertion's split moves.
    assert module.vitest_runs(tmp_path, [recorded, unrecorded]) == [[recorded, unrecorded]]
    _write(tmp_path, UI_DURATIONS, json.dumps({recorded: 537.2}))
    assert sorted(module.vitest_runs(tmp_path, [recorded, unrecorded])) == [[recorded], [unrecorded]]
    # No durations file at all: every file weighs the default, and runs.
    (tmp_path / UI_DURATIONS).unlink()
    # MUTATION: raise on a missing or unreadable durations file -> red.
    assert module.vitest_runs(tmp_path, [recorded, unrecorded]) == [[recorded, unrecorded]]
    (tmp_path / UI_DURATIONS).write_text("not json")
    assert module.vitest_runs(tmp_path, [recorded, unrecorded]) == [[recorded, unrecorded]]


def test_the_checked_in_ui_durations_are_keyed_like_the_vitest_selection():
    """The weights are keyed by path relative to apps/swarm-ui, as vitest_runs
    is handed them, so a key in another shape would weigh nothing."""
    module = _selector_module()
    weights = json.loads((REPO / UI_DURATIONS).read_text())
    # MUTATION: key a slice repo-relative (apps/swarm-ui/src/...) or seed
    # fewer than the eight slices -> red.
    for group in SPACING_SLICES:
        key = f"src/__tests__/spacing.{group}.test.tsx"
        assert (REPO / UI / key).is_file(), key
        assert 250 <= weights[key] <= 360, (key, weights[key])
    for key, seconds in weights.items():
        # A key for a test since deleted is harmless (it is never looked up),
        # so only the shape is held, not that the file still exists.
        assert module.is_vitest_file(key), key
        assert isinstance(seconds, (int, float)) and seconds > 0, (key, seconds)


def test_a_heavy_pytest_selection_splits_by_ci_weight(tmp_path):
    _tree(tmp_path)
    for i in range(6):
        _write(tmp_path, f"tests/unit/foo/test_heavy{i}.py", "from foo.core import X\n")
    weights = {f"tests/unit/foo/test_heavy{i}.py": 300 for i in range(6)}
    _write(tmp_path, "tests/unit/scripts/unit_test_durations.json", json.dumps(weights))
    got = _select(tmp_path, "apps/foo/foo/core.py")
    # MUTATION: ignore unit_test_durations.json -> one 1,800 CI-second run.
    assert len(got["pytest"]) >= 3
    assert len(_files(got["pytest"])) == 9


def test_a_runner_wide_ui_change_selects_no_suite_and_says_ci_runs_it(tmp_path):
    _tree(tmp_path)
    got = _select(tmp_path, f"{UI}/vitest.config.ts")
    # MUTATION: answer a config change with the whole suite -> vitest is not
    # empty (and no file is related to the config by import).
    assert got["vitest"] == []
    assert any("vitest.config.ts" in note and "CI" in note for note in got["note"])


# --- the script, end to end on this checkout ---------------------------------


def test_the_script_selects_a_ui_only_change(tmp_path):
    diff = tmp_path / "diff.txt"
    diff.write_text(f"{UI}/src/vitestWorkers.ts\n")
    result = _run("--files", str(diff))
    assert result.returncode == 0, result.stderr
    listed = _listed(result.stdout)
    assert f"{UI}/src/__tests__/vitestWorkers.test.ts" in listed
    commands = _commands(result.stdout)
    assert any(c.startswith(VITEST_CMD) for c in commands)
    _assert_every_command_names_files(commands)


def test_the_script_selects_a_python_only_change(tmp_path):
    diff = tmp_path / "diff.txt"
    diff.write_text("scripts/lib/select_unit_tests.py\n")
    result = _run("--files", str(diff))
    assert result.returncode == 0, result.stderr
    assert "tests/unit/scripts/test_select_unit_tests.py" in _listed(result.stdout)
    commands = _commands(result.stdout)
    assert not any(c.startswith(VITEST_CMD) for c in commands)
    _assert_every_command_names_files(commands)


def test_the_script_selects_a_mixed_change_and_a_wide_one_as_bounded_runs(tmp_path):
    diff = tmp_path / "diff.txt"
    # types.ts reaches most of the UI suite: it must still come back as named
    # files, in several runs.
    # scripts/related-tests.sh is not Python, so this case skips the import
    # scan and stays cheap; test_related_tests.py reads it by name.
    diff.write_text(f"{UI}/src/types.ts\nscripts/related-tests.sh\n")
    result = _run("--files", str(diff))
    assert result.returncode == 0, result.stderr
    commands = _commands(result.stdout)
    vitest = [c for c in commands if c.startswith(VITEST_CMD)]
    assert len(vitest) >= 2
    assert any(c.startswith(PYTEST_CMD) for c in commands)
    _assert_every_command_names_files(commands)


def test_an_empty_diff_runs_nothing_and_says_so(tmp_path):
    diff = tmp_path / "diff.txt"
    diff.write_text("")
    result = _run("--files", str(diff))
    assert result.returncode == 0
    assert _commands(result.stdout) == []
    assert "nothing changed" in result.stderr


def test_an_unresolvable_base_is_a_usage_error_not_an_empty_diff():
    result = _run("--base", "no-such-rev-" + "0" * 8)
    # MUTATION: read a git failure as an empty diff -> exit 0, "nothing changed".
    assert result.returncode == 2
    assert "does not resolve" in result.stderr


def test_run_executes_each_named_run_in_turn_in_the_foreground(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    for tool in ("uv", "npx"):
        fake = bin_dir / tool
        fake.write_text(f'#!/usr/bin/env bash\nprintf "%s|%s\\n" "${{PWD##*/}}" "{tool} $*" >>"{log}"\n')
        fake.chmod(0o755)
    diff = tmp_path / "diff.txt"
    diff.write_text(f"{UI}/src/vitestWorkers.ts\nscripts/related-tests.sh\n")
    result = _run("--run", "--files", str(diff),
                  env_extra={"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"})
    assert result.returncode == 0, result.stderr
    calls = log.read_text().splitlines()
    # MUTATION: make --run print without running, or run vitest from the
    # repository root -> these fail.
    assert any(c.startswith("swarm-ui|npx vitest run ") and "src/__tests__/vitestWorkers.test.ts" in c
               for c in calls), calls
    assert any(c.startswith("uv run pytest ") or "|uv run pytest " in c for c in calls), calls
    assert len(calls) == len(_commands(result.stdout))


# --- the stylesheet guards (RELATED-CSS-GUARDS, part of #1914) ---------------
#
# Measured 2026-10-22 on PR 1905: it changed apps/swarm-ui/src/styles/
# intake.css and declared `.rn-filters > .c-seg > button` twice in one media
# block. CI's stylesheet.gate.test.ts and sections.stylesheet.test.ts refused
# it; the lane's implement, fix and re-review agents ran this script and
# changed-guards.sh and none of them ran those two files. Both read every
# sheet from disk (`import.meta.glob`), so no import connects them to a
# changed .css file and the import graph never selects them.

GATE = "src/__tests__/stylesheet.gate.test.ts"
SECTIONS = "src/__tests__/sections.stylesheet.test.ts"
INTAKE = f"{UI}/src/styles/intake.css"


def _with_stylesheet_guards(root: Path) -> None:
    """The synthetic tree, plus the stylesheet guards as the checkout writes
    them: they glob the sheets and import none of them."""
    _tree(root)
    _write(root, f"{UI}/src/styles/intake.css", ".rn-filters { gap: 4px }\n")
    _write(root, f"{UI}/src/styles/capacity.css", ".cap { gap: 4px }\n")
    _write(root, f"{UI}/src/styles.css", ":root { --x: 1 }\n")
    for guard in (GATE, SECTIONS):
        _write(root, f"{UI}/{guard}",
               "const SHEETS = import.meta.glob<string>('../styles/*.css', "
               "{ query: '?raw', import: 'default', eager: true })\n")


def test_a_ui_stylesheet_change_selects_both_stylesheet_guards(tmp_path):
    _with_stylesheet_guards(tmp_path)
    got = _select(tmp_path, INTAKE)
    vitest = _files(got["vitest"])
    # MUTATION: drop the stylesheet-guard rule from related_tests.py (leave
    # only the import graph) -> neither guard is selected and this is red.
    assert GATE in vitest, vitest
    assert SECTIONS in vitest, vitest


def test_a_change_with_no_stylesheet_selects_no_stylesheet_guard(tmp_path):
    _with_stylesheet_guards(tmp_path)
    got = _select(tmp_path, f"{UI}/src/widget.ts", "apps/foo/foo/core.py")
    vitest = _files(got["vitest"])
    # MUTATION: add the stylesheet guards for every UI change (drop the
    # `.css` test) -> both appear here and this is red.
    assert GATE not in vitest and SECTIONS not in vitest, vitest


def test_a_stylesheet_outside_swarm_ui_selects_no_stylesheet_guard(tmp_path):
    _with_stylesheet_guards(tmp_path)
    got = _select(tmp_path, "docs/assets/site.css", "apps/other/static/intake.css")
    # MUTATION: match any path ending in .css instead of a .css under
    # apps/swarm-ui/ -> the guards are selected here and this is red.
    assert got["vitest"] == []


def test_several_stylesheets_select_each_stylesheet_guard_once(tmp_path):
    _with_stylesheet_guards(tmp_path)
    got = _select(tmp_path, INTAKE, f"{UI}/src/styles/capacity.css", f"{UI}/src/styles.css")
    flat = [f for run in got["vitest"] for f in run.split()]
    # MUTATION: append the guards once per changed sheet (no de-duplication)
    # -> each appears three times and this is red.
    assert flat.count(GATE) == 1, flat
    assert flat.count(SECTIONS) == 1, flat


def test_every_stylesheet_guard_named_is_a_test_in_this_checkout():
    # The selector imports select_unit_tests from its own directory, which it
    # puts on sys.path itself.
    spec = importlib.util.spec_from_file_location("related_tests_under_test", SELECTOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    guards = tuple(module.STYLESHEET_GUARDS)
    # MUTATION: drop either guard from STYLESHEET_GUARDS -> red.
    assert GATE in guards and SECTIONS in guards, guards
    # MUTATION: rename or delete a listed guard without the list following
    # (the selector would silently skip a file that is gone) -> red.
    for guard in guards:
        assert (REPO / UI / guard).is_file(), guard
        assert module.is_vitest_file(guard), guard


def test_the_script_selects_the_stylesheet_guards_for_intake_css_once(tmp_path):
    diff = tmp_path / "diff.txt"
    diff.write_text(f"{INTAKE}\n")
    result = _run("--files", str(diff))
    assert result.returncode == 0, result.stderr
    listed = _listed(result.stdout)
    # MUTATION: leave the selection to the import graph alone -> neither
    # guard is listed for PR 1905's change, the measured miss.
    assert listed.count(f"{UI}/{GATE}") == 1, listed
    assert listed.count(f"{UI}/{SECTIONS}") == 1, listed
    commands = _commands(result.stdout)
    # MUTATION: list the guards but leave them out of the runs -> red.
    assert sum(c.count(f" {GATE}") for c in commands) == 1, commands
    assert sum(c.count(f" {SECTIONS}") for c in commands) == 1, commands
    _assert_every_command_names_files(commands)


# --- the named-sheet readers (NAMED-SHEETS, part of #1914) -------------------
#
# Found by RELATED-CSS-GUARDS (PR 1921): some vitest files read ONE named
# sheet from disk with readFileSync -- `'../styles/repograph.css'`,
# `join(__dirname, '..', 'styles', 'theater.css')`, `css('theater.css')` --
# so no import connects them to a change of that sheet. Owner decision
# 2026-10-22: "Yes, a name-keyed rule".

THEATER = f"{UI}/src/styles/theater.css"
LIVE = f"{UI}/src/styles/live.css"
STYLES = f"{UI}/src/styles.css"
READS_THEATER = "src/__tests__/theater.reads.test.tsx"
READS_BY_NAME = "src/__tests__/theater.byname.test.tsx"
IMPORTS_PANEL = "src/__tests__/theater.panel.test.tsx"
READS_WIDGET = "src/__tests__/widget.text.test.ts"
MENTIONS = "src/__tests__/theater.mentions.test.tsx"
READS_STYLES = "src/__tests__/styles.reads.test.tsx"
READS_LIVE = "src/__tests__/live.reads.test.tsx"
READS_BOTH = "src/__tests__/both.reads.test.tsx"

# The seven files PR 1921 found reading one named sheet from disk.
NAMED_READERS = {
    "src/__tests__/browser.badges.test.tsx": "styles/live.css",
    "src/__tests__/repograph.vqa-l08.test.ts": "styles/repograph.css",
    "src/__tests__/repositories.c6j.test.tsx": "styles/repositories.css",
    "src/__tests__/repositories.graph.test.tsx": "styles/repograph.css",
    "src/__tests__/theater.followup.test.tsx": "styles/theater.css",
    "src/__tests__/theater.v2.test.tsx": "styles/theater.css",
    "src/__tests__/theater.take.test.tsx": "styles/theater.css",
}


def _with_named_sheets(root: Path) -> None:
    """The synthetic tree, plus sheets and tests that read one of them by
    name, import a component, or only say the word."""
    _tree(root)
    for sheet in (THEATER, LIVE, STYLES):
        _write(root, sheet, ".x { gap: 4px }\n")
    _write(root, f"{UI}/{READS_THEATER}",
           "import { readFileSync } from 'node:fs'\n"
           "const CSS = readFileSync(resolve(__dirname, '../styles/theater.css'), 'utf8')\n")
    _write(root, f"{UI}/{READS_BY_NAME}",
           "const css = (f: string) => readFileSync(join(STYLES, f), 'utf8')\n"
           "const sheet = css('theater.css')\n")
    _write(root, f"{UI}/{IMPORTS_PANEL}", "import { o } from '../other'\n")
    _write(root, f"{UI}/{READS_WIDGET}", "const W = readFileSync(join(__dirname, '../widget.ts'), 'utf8')\n")
    _write(root, f"{UI}/{MENTIONS}", "describe('the theater', () => { it('opens theater', () => {}) })\n")
    _write(root, f"{UI}/{READS_STYLES}",
           "const S = readFileSync(join(__dirname, '..', 'styles.css'), 'utf8')\n")
    _write(root, f"{UI}/{READS_LIVE}",
           "const S = readFileSync(join(__dirname, '..', 'styles', 'live.css'), 'utf8')\n"
           "const DIR = 'styles/'\n")
    _write(root, f"{UI}/{READS_BOTH}",
           "const A = readFileSync(join(__dirname, '../styles/theater.css'), 'utf8')\n"
           "const B = readFileSync(join(__dirname, '../styles/live.css'), 'utf8')\n")


def test_a_sheet_change_selects_the_tests_that_read_it_by_path_or_name(tmp_path):
    _with_named_sheets(tmp_path)
    vitest = _files(_select(tmp_path, THEATER)["vitest"])
    # MUTATION: drop the named-sheet rule from ui_related (leave the import
    # graph and STYLESHEET_GUARDS alone) -> neither reader is selected.
    assert READS_THEATER in vitest, vitest
    # MUTATION: match only the path relative to src (`styles/theater.css`),
    # never the file name read alone -> css('theater.css') is missed.
    assert READS_BY_NAME in vitest, vitest
    assert READS_BOTH in vitest, vitest


def test_a_sheet_change_does_not_select_an_importer_or_a_bare_word(tmp_path):
    _with_named_sheets(tmp_path)
    vitest = _files(_select(tmp_path, THEATER)["vitest"])
    # MUTATION: match the sheet's stem (`theater`) as a bare word instead of
    # the quoted path or file name -> theater.mentions is selected.
    assert MENTIONS not in vitest, vitest
    # MUTATION: select every vitest file on a .css change -> the component
    # importer (and every other test) is selected.
    assert IMPORTS_PANEL not in vitest, vitest
    assert READS_LIVE not in vitest and READS_STYLES not in vitest, vitest


def test_styles_css_selects_its_reader_and_not_a_reader_of_styles_dir(tmp_path):
    _with_named_sheets(tmp_path)
    vitest = _files(_select(tmp_path, STYLES)["vitest"])
    # MUTATION: drop the named-sheet rule -> the reader of styles.css is missed.
    assert READS_STYLES in vitest, vitest
    # MUTATION: match `styles.css` as a substring, or `styles` as a word ->
    # the reader of styles/live.css (which says 'styles/') is selected.
    assert READS_LIVE not in vitest, vitest
    assert READS_THEATER not in vitest and READS_BOTH not in vitest, vitest


def test_two_changed_sheets_named_by_one_test_select_it_once(tmp_path):
    _with_named_sheets(tmp_path)
    got = _select(tmp_path, THEATER, LIVE)
    flat = [f for run in got["vitest"] for f in run.split()]
    # MUTATION: append the readers once per changed sheet (a list, not a
    # set) -> both.reads appears twice and this is red.
    assert flat.count(READS_BOTH) == 1, flat
    assert flat.count(READS_LIVE) == 1 and flat.count(READS_THEATER) == 1, flat


def test_a_change_with_no_stylesheet_adds_no_named_sheet_reader(tmp_path):
    _with_named_sheets(tmp_path)
    vitest = _files(_select(tmp_path, f"{UI}/src/widget.ts", "apps/foo/foo/core.py")["vitest"])
    # MUTATION: run the named-path rule for every changed UI file (drop the
    # `.css` test) -> widget.text, which reads '../widget.ts' as text, is
    # selected and this is red.
    assert READS_WIDGET not in vitest, vitest
    # MUTATION: add any reader for a non-sheet change -> the answer is more
    # than the import graph's three files.
    assert vitest == sorted(["src/__tests__/mocked.test.ts", "src/__tests__/panel.test.tsx",
                             "test/Live.test.tsx"]), vitest


def test_the_script_selects_the_seven_named_sheet_readers_once(tmp_path):
    sheets = sorted({f"{UI}/src/{s}" for s in NAMED_READERS.values()})
    diff = tmp_path / "diff.txt"
    diff.write_text("".join(f"{s}\n" for s in sheets))
    result = _run("--files", str(diff))
    assert result.returncode == 0, result.stderr
    listed = _listed(result.stdout)
    commands = _commands(result.stdout)
    for reader in NAMED_READERS:
        # MUTATION: leave the selection to the import graph and the
        # stylesheet guards -> none of the seven is listed (PR 1921's gap).
        assert listed.count(f"{UI}/{reader}") == 1, (reader, listed)
        # MUTATION: list the readers but leave them out of the runs -> red.
        assert sum(c.count(f" {reader}") for c in commands) == 1, (reader, commands)
    _assert_every_command_names_files(commands)
