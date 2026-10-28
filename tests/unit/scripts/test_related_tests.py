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
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "related-tests.sh"
SELECTOR = REPO / "scripts" / "lib" / "related_tests.py"
UI = "apps/swarm-ui"

PYTEST_CMD = "uv run pytest "
VITEST_CMD = f"(cd {UI} && npx vitest run "
NODE_CMD = f"(cd {UI} && npm run test:node)"


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
    out: dict[str, list[str]] = {"pytest": [], "vitest": [], "node": [], "note": []}
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
    # The --run logs go under SWARM_ARTIFACTS_DIR. Unless a test names its own,
    # it is a throwaway directory, never the caller's: a lane running this file
    # would otherwise collect related-tests.*.runN.log files from every test.
    with tempfile.TemporaryDirectory(prefix="related-tests-artifacts.") as scratch:
        if "SWARM_ARTIFACTS_DIR" not in (env_extra or {}):
            env["SWARM_ARTIFACTS_DIR"] = scratch
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
            args = [a for a in command[len(VITEST_CMD):].rstrip(")").split() if not a.startswith("-")]
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


def test_a_test_reading_a_doc_that_cites_the_changed_path_is_selected(tmp_path):
    """The same doc rule as pull-request CI and changed-guards.sh (#2299): a
    test that names a doc citing the changed path by its full path reaches the
    change through the doc."""
    _tree(tmp_path)
    _write(tmp_path, "docs/core.md", "`apps/foo/foo/lone.py` holds Y.\n")
    _write(tmp_path, "docs/elsewhere.md", "`apps/foo/foo/edge.py` holds nothing here.\n")
    _write(tmp_path, "tests/unit/foo/test_core_doc.py", 'DOC = REPO / "docs" / "core.md"\n')
    _write(tmp_path, "tests/unit/foo/test_elsewhere_doc.py", 'DOC = REPO / "docs" / "elsewhere.md"\n')
    got = _select(tmp_path, "apps/foo/foo/lone.py")
    # MUTATION: feed the text rule script_closure(changed) without doc_closure
    # -> test_core_doc.py is lost. The control: a doc citing another path does
    # not select its readers.
    assert _files(got["pytest"]) == ["tests/unit/foo/test_core_doc.py", "tests/unit/foo/test_lone.py"]


def test_a_deleted_file_selects_nothing_that_is_gone(tmp_path):
    _tree(tmp_path)
    got = _select(tmp_path, f"{UI}/src/__tests__/gone.test.tsx", "tests/unit/foo/test_gone.py")
    assert got["vitest"] == [] and got["pytest"] == []


def test_a_large_ui_selection_comes_back_as_a_few_bounded_runs(tmp_path):
    _tree(tmp_path, many=120)
    got = _select(tmp_path, f"{UI}/src/widget.ts")
    runs = got["vitest"]
    # 123 unrecorded files weigh 123 x 2.9 = 357 CI-seconds, one run by
    # weight: the file-count cap is what still cuts them, into ceil(123/50).
    # MUTATION: return the selection as one run -> len(runs) == 1; drop the
    # per-run file cap (cut by weight alone) -> one 123-file run.
    assert len(runs) == 3
    for run in runs:
        # MUTATION: open a new run only on weight -> a run over 50 files.
        assert 0 < len(run.split()) <= 50
    files = _files(runs)
    assert len(files) == len(set(files)) == 123


# --- vitest runs are cut by recorded CI-seconds (#2362) ----------------------
#
# Cut by file count alone, the nine spacing.*.test.tsx slices (252-485 s each,
# measured in agent logs 2026-10-25/26) landed in one 50-file run that could not
# finish inside related-tests.sh's RUN_SECONDS=540, and 40+ agents timed out.

HEAVY_SPACING = {
    "src/__tests__/spacing.accounts.test.tsx": 437,
    "src/__tests__/spacing.admin.test.tsx": 300,
    "src/__tests__/spacing.capacity.test.tsx": 252,
    "src/__tests__/spacing.config.test.tsx": 310,
    "src/__tests__/spacing.runs.test.tsx": 320,
    "src/__tests__/spacing.setup.test.tsx": 330,
    "src/__tests__/spacing.submit.test.tsx": 340,
    "src/__tests__/spacing.work.test.tsx": 353,
}
UI_WEIGHTS = "tests/unit/scripts/ui_test_durations.json"


def _selector_module():
    spec = importlib.util.spec_from_file_location("related_tests_weights", SELECTOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _spacing_tree(root: Path, weights: dict[str, float], light: int = 41) -> None:
    """A sweep module every slice imports, the slices, spacing.test.tsx and
    `light` other tests of the same module; `weights` recorded for the UI."""
    _write(root, f"{UI}/src/App.tsx", "export const App = () => null\n")
    _write(root, f"{UI}/src/__tests__/spacingsweep.tsx", "import { App } from '../App'\n")
    for path in [*HEAVY_SPACING, "src/__tests__/spacing.test.tsx"]:
        _write(root, f"{UI}/{path}", "import { sweep } from './spacingsweep'\n")
    for i in range(light):
        _write(root, f"{UI}/src/__tests__/light{i:02d}.test.tsx", "import { App } from '../App'\n")
    _write(root, UI_WEIGHTS, json.dumps(weights))


def _estimate(run: str, weights: dict[str, float]) -> float:
    return sum(weights.get(f, 2.9) for f in run.split())


def _assert_heavy_apart_and_bounded(runs: list[str], weights: dict[str, float]) -> None:
    for run in runs:
        heavy = [f for f in run.split() if f in HEAVY_SPACING]
        # MUTATION: cut by file count (today's one 50-file run) -> all eight
        # heavy slices in one run.
        assert len(heavy) <= 1, run
        files = run.split()
        # MUTATION: open no new run when a file would take every run over
        # VITEST_RUN_WEIGHT -> a run estimated over 540 CI-seconds.
        assert _estimate(run, weights) <= 540 or len(files) == 1, (run, _estimate(run, weights))


def test_heavy_spacing_slices_never_share_a_vitest_run(tmp_path):
    _spacing_tree(tmp_path, HEAVY_SPACING)
    got = _select(tmp_path, f"{UI}/src/App.tsx")
    files = _files(got["vitest"])
    # MUTATION: drop or duplicate a file while cutting -> not 50 distinct files.
    assert len(files) == len(set(files)) == 50
    _assert_heavy_apart_and_bounded(got["vitest"], HEAVY_SPACING)
    # Eight heavy files, each in a run of its own.
    assert len(got["vitest"]) >= 8


def test_the_checked_in_weights_keep_every_spacing_slice_apart(tmp_path):
    recorded = json.loads((REPO / UI_WEIGHTS).read_text())
    # MUTATION: drop a slice from ui_test_durations.json -> it weighs 2.9 s
    # and rejoins another slice's run.
    assert set(HEAVY_SPACING) <= set(recorded), sorted(set(HEAVY_SPACING) - set(recorded))
    _spacing_tree(tmp_path, recorded)
    got = _select(tmp_path, f"{UI}/src/App.tsx")
    _assert_heavy_apart_and_bounded(got["vitest"], recorded)


def test_no_vitest_run_is_estimated_over_540_seconds(tmp_path):
    weights = {
        "src/__tests__/huge.test.tsx": 700,
        "src/__tests__/big0.test.tsx": 400,
        "src/__tests__/big1.test.tsx": 200,
        "src/__tests__/big2.test.tsx": 150,
        "src/__tests__/mid0.test.tsx": 90,
        "src/__tests__/mid1.test.tsx": 60,
    }
    _write(tmp_path, f"{UI}/src/widget.ts", "export const w = 1\n")
    for path in weights:
        _write(tmp_path, f"{UI}/{path}", "import { w } from '../widget'\n")
    for i in range(70):
        _write(tmp_path, f"{UI}/src/__tests__/l{i:02d}.test.tsx", "import { w } from '../widget'\n")
    _write(tmp_path, UI_WEIGHTS, json.dumps(weights))
    got = _select(tmp_path, f"{UI}/src/widget.ts")
    runs = got["vitest"]
    assert len(_files(runs)) == 76
    for run in runs:
        # MUTATION: ignore ui_test_durations.json, or let a heavy file join a
        # run it takes over 540 -> a run estimated over 540 CI-seconds.
        assert _estimate(run, weights) <= 540 or run.split() == ["src/__tests__/huge.test.tsx"], run
        assert len(run.split()) <= 50, run
    # MUTATION: put another file beside one heavier than the limit -> the
    # 700 s file does not run alone.
    assert "src/__tests__/huge.test.tsx" in runs


def test_an_unrecorded_vitest_file_weighs_the_default(tmp_path):
    module = _selector_module()
    # MUTATION: change the default without its measurement -> red here first.
    assert module.VITEST_DEFAULT_SECONDS == 2.9
    heavy = "src/__tests__/heavy.test.tsx"
    _write(tmp_path, UI_WEIGHTS, json.dumps({heavy: 530.5}))
    light = [f"src/__tests__/u{i}.test.tsx" for i in range(4)]
    # 530.5 + 3 x 2.9 = 539.2 fits one run; a 4th unrecorded file (542.1) does not.
    # MUTATION: weigh an unrecorded file 0 (or anything under ~2.4 s) -> the
    # four light files join the heavy one.
    # MUTATION: weigh it the recorded median, as pytest_runs does -> 530.5 s
    # each, and the three light files do not fit beside the heavy one.
    assert module.vitest_runs(tmp_path, [heavy, *light[:3]]) == [sorted([heavy, *light[:3]])]
    runs = module.vitest_runs(tmp_path, [heavy, *light])
    assert sorted(runs) == sorted([[heavy], sorted(light)]), runs
    # A missing durations file weighs every file the default: one bounded run.
    assert module.vitest_runs(tmp_path / "nowhere", light) == [sorted(light)]


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


def _fake_tools(bin_dir: Path, log: Path) -> None:
    """uv, npx and npm that only log the directory they ran in and their
    arguments: --run is held without running a real test runner."""
    for tool in ("uv", "npx", "npm"):
        fake = bin_dir / tool
        fake.write_text(f'#!/usr/bin/env bash\nprintf "%s|%s\\n" "${{PWD##*/}}" "{tool} $*" >>"{log}"\n')
        fake.chmod(0o755)


def test_run_executes_each_named_run_in_turn_in_the_foreground(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    _fake_tools(bin_dir, log)
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
    # The UI change also runs the node runners (one more call than the
    # file runs; see the node-runner tests below).
    assert len(calls) == len(_commands(result.stdout)) + result.stdout.splitlines().count(NODE_CMD)


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


# --- the spacing slices a screen change reaches (#2362, part 2) --------------
#
# spacingsweep.tsx imports {App, SECTIONS} from '../App', so by import every
# module App.tsx reaches reached all nine spacing.<group>.test.tsx slices, at
# 252-437 CI-seconds each: 40+ agents timed out re-running them (#2362). A
# slice renders App at its own routes only, so a change to one route's screen
# selects the slices that walk that route, and a change to anything else App
# renders -- the shell, the sweep, a sheet -- still selects all of them.

SLICES = ("src/__tests__/spacing.capacity.test.tsx", "src/__tests__/spacing.admin.test.tsx")
SLICE_CHECK = "src/__tests__/spacing.test.tsx"

APP_TSX = """\
import { AScreen } from './A'
import { BScreen } from './B'
import { Spine } from './Spine'

export function App() {
  return <Spine><SectionBody id="capacity/pools" /></Spine>
}

function SectionBody({ id }: { id: string }) {
  switch (id) {
    case 'capacity/pools':
      return <AScreen />
    case 'admin/limits':
      return <BScreen />
    default:
      return null
  }
}
"""

SWEEP_TSX = """\
import { App, SECTIONS } from '../App'
import { probe } from './spaceprobe'

export const SPACING_GROUPS: readonly SpacingGroup[] = [
  { name: 'capacity', routes: ['capacity/pools'], floor: 1 },
  {
    name: 'admin', routes: ['admin/limits'], floor: 1,
    centred: ['span.x [left] width 4px, centred'],
  },
]
"""


def _sweep_tree(root: Path, *, app: str = APP_TSX, sweep: str = SWEEP_TSX) -> None:
    _write(root, f"{UI}/src/App.tsx", app)
    _write(root, f"{UI}/src/A.tsx", "import { onlyA } from './onlyA'\nexport const AScreen = () => onlyA\n")
    _write(root, f"{UI}/src/onlyA.ts", "export const onlyA = 1\n")
    _write(root, f"{UI}/src/B.tsx", "export const BScreen = () => null\n")
    _write(root, f"{UI}/src/Spine.tsx", "export const Spine = () => null\n")
    _write(root, f"{UI}/src/styles/capacity.css", ".a { margin: 0 }\n")
    _write(root, f"{UI}/src/__tests__/spaceprobe.ts", "export const probe = 1\n")
    _write(root, f"{UI}/src/__tests__/spacingsweep.tsx", sweep)
    for slice_ in SLICES:
        _write(root, f"{UI}/{slice_}", "import { spacingGroup } from './spacingsweep'\n")
    _write(root, f"{UI}/{SLICE_CHECK}", "import { SPACING_GROUPS } from './spacingsweep'\n")
    _write(root, f"{UI}/src/__tests__/a.test.tsx", "import { AScreen } from '../A'\n")
    _write(root, f"{UI}/src/__tests__/app.test.tsx", "import { App } from '../App'\n")


def _spacing_selected(root: Path, *changed: str) -> list[str]:
    files = _files(_select(root, *(f"{UI}/{c}" for c in changed))["vitest"])
    return [f for f in files if "/spacing." in f]


def test_a_screen_change_selects_only_the_spacing_slice_of_its_routes(tmp_path):
    _sweep_tree(tmp_path)
    for changed in ("src/A.tsx", "src/onlyA.ts"):
        # MUTATION: leave the selection to the import graph -> A reaches App,
        # App reaches the sweep, and spacing.admin is selected too.
        assert _spacing_selected(tmp_path, changed) == sorted(
            ["src/__tests__/spacing.capacity.test.tsx", SLICE_CHECK]), changed
    assert _spacing_selected(tmp_path, "src/B.tsx") == sorted(
        ["src/__tests__/spacing.admin.test.tsx", SLICE_CHECK])


def test_an_app_shell_or_sweep_or_sheet_change_selects_every_spacing_slice(tmp_path):
    _sweep_tree(tmp_path)
    every = sorted([*SLICES, SLICE_CHECK])
    for changed in ("src/App.tsx", "src/Spine.tsx", "src/__tests__/spacingsweep.tsx",
                    "src/__tests__/spaceprobe.ts", "src/styles/capacity.css"):
        # MUTATION: treat Spine as a screen, or select slices by screen alone
        # -> a shell, sweep or sheet change drops a slice it changes.
        assert _spacing_selected(tmp_path, changed) == every, changed


def test_a_screen_module_app_also_imports_for_the_shell_selects_every_slice(tmp_path):
    # A wrapper's module or a name App uses outside SectionBody's cases (a
    # card, a hook) is rendered on every route, so its module is not a screen.
    app = APP_TSX.replace("import { AScreen } from './A'", "import { AScreen, useA } from './A'")
    app = app.replace("export function App() {\n", "export function App() {\n  useA()\n")
    _sweep_tree(tmp_path, app=app)
    # MUTATION: cut every App import of a case's screen name -> only capacity.
    assert _spacing_selected(tmp_path, "src/A.tsx") == sorted([*SLICES, SLICE_CHECK])


def test_an_unparseable_app_or_sweep_selects_every_spacing_slice(tmp_path):
    every = sorted([*SLICES, SLICE_CHECK])
    broken = {
        "no SPACING_GROUPS": {"sweep": "import { App } from '../App'\nexport const GROUPS = []\n"},
        "no case lines": {"app": APP_TSX.replace("case ", "when ")},
        "an unresolvable screen": {"app": APP_TSX.replace("import { AScreen } from './A'\n", "")},
        "a route with no case": {"sweep": SWEEP_TSX.replace("'capacity/pools'", "'capacity/gone'")},
    }
    for why, parts in broken.items():
        root = tmp_path / why.replace(" ", "-")
        _sweep_tree(root, **parts)
        # MUTATION: answer "no slice" when the parse fails -> a screen change
        # selects nothing and the sweep goes unrun until CI.
        assert _spacing_selected(root, "src/B.tsx") == every, why


def test_non_spacing_selection_is_unchanged(tmp_path):
    _sweep_tree(tmp_path)
    got = [f for f in _files(_select(tmp_path, f"{UI}/src/A.tsx")["vitest"]) if "/spacing." not in f]
    # MUTATION: drop the screen's importers outside the sweep as well -> a.test
    # or app.test goes missing.
    assert got == ["src/__tests__/a.test.tsx", "src/__tests__/app.test.tsx"]


def test_every_real_spacing_route_resolves_to_a_screen_module():
    module = _selector_module()
    ui_root = REPO / UI
    known = set(module._ui_files(ui_root))
    plan = module.spacing_plan(ui_root, known)
    # MUTATION: rename SPACING_GROUPS, a SectionBody case or a screen's import
    # -> the plan is None and every UI change selects all nine slices again.
    assert plan is not None
    slices = sorted(k for k in known if re.fullmatch(r"src/__tests__/spacing\.[^.]+\.test\.tsx", k))
    assert sorted(f"src/__tests__/spacing.{g}.test.tsx" for g in plan.groups) == slices
    for group, routes in plan.groups.items():
        for route in routes:
            modules = plan.routes[route]
            assert modules, (group, route)
            assert all(m in known for m in modules), (route, modules)
    # And the narrowing is not empty: some screen's change selects fewer than
    # every slice. (Screens import from each other -- Overview's cards come
    # from several -- so one screen often reaches two or three slices.)
    for screen in sorted(plan.cut):
        got = [f for f in module.ui_related(ui_root, [screen]) if module._SLICE.fullmatch(f)]
        if 0 < len(got) < len(slices):
            break
    else:
        raise AssertionError(f"no screen of {sorted(plan.cut)} narrows the spacing slices")


# --- the node:test runners (#2455) -------------------------------------------
#
# apps/swarm-ui/test/*.test.mjs (no-company-name.test.mjs among them) and
# tests/run.mjs run under node, not vitest, through `npm run test:node`, which
# CI's ui-build runs. No import from a vitest file reaches them, so the
# selector named none of them and four lanes (PRs #2429, #2430, #2433, #2437)
# published UI code naming the company and went red in ui-build.

NODE_SCRIPT = "node tests/run.mjs && node --test test/*.test.mjs"


def _with_package(root: Path, scripts: dict[str, str]) -> None:
    _write(root, f"{UI}/package.json", json.dumps({"name": "swarm-ui", "scripts": scripts}))


def test_a_ui_change_names_the_node_test_runners(tmp_path):
    _tree(tmp_path)
    _with_package(tmp_path, {"test:node": NODE_SCRIPT, "test:components": "vitest run"})
    _write(tmp_path, f"{UI}/src/Foo.tsx", "export const Foo = () => null\n")
    got = _select(tmp_path, f"{UI}/src/Foo.tsx")
    # MUTATION: leave related_tests.py printing pytest and vitest runs only
    # (the #2455 gap) -> no node run, and this is red.
    assert got["node"] == [UI], got
    got = _select(tmp_path, f"{UI}/src/widget.ts", "apps/foo/foo/core.py")
    # MUTATION: print the node run once per changed UI file -> two or more.
    assert got["node"] == [UI], got


def test_no_node_run_without_a_ui_change_or_a_test_node_script(tmp_path):
    _tree(tmp_path)
    _with_package(tmp_path, {"test:node": NODE_SCRIPT})
    # MUTATION: print the node run for every change -> a Python-only change
    # (or a path that only starts like apps/swarm-ui) gets one.
    assert _select(tmp_path, "apps/foo/foo/core.py")["node"] == []
    assert _select(tmp_path, "apps/swarm-ui-old/src/x.ts")["node"] == []
    # MUTATION: skip the package.json check -> a package with no test:node
    # script (or no package.json at all) is told to run one.
    _with_package(tmp_path, {"test:components": "vitest run"})
    assert _select(tmp_path, f"{UI}/src/widget.ts")["node"] == []
    (tmp_path / UI / "package.json").unlink()
    assert _select(tmp_path, f"{UI}/src/widget.ts")["node"] == []


def test_the_node_run_is_rendered_and_run_in_the_ui_package(tmp_path):
    # This checkout's apps/swarm-ui/package.json declares test:node.
    assert "test:node" in json.loads((REPO / UI / "package.json").read_text())["scripts"]
    diff = tmp_path / "diff.txt"
    diff.write_text(f"{UI}/src/vitestWorkers.ts\n")
    result = _run("--files", str(diff))
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    # MUTATION: leave related-tests.sh rendering pytest and vitest only (or
    # drop the node line) -> the command is never printed.
    assert lines.count(NODE_CMD) == 1, lines
    last_vitest = max(i for i, line in enumerate(lines) if line.startswith(VITEST_CMD))
    assert lines.index(NODE_CMD) > last_vitest, lines

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    _fake_tools(bin_dir, log)
    result = _run("--run", "--files", str(diff),
                  env_extra={"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"})
    assert result.returncode == 0, result.stderr
    calls = log.read_text().splitlines()
    # MUTATION: print the node run without running it, run it from the
    # repository root, or before the vitest runs -> red.
    assert calls.count("swarm-ui|npm run test:node") == 1, calls
    assert calls[-1] == "swarm-ui|npm run test:node", calls
    assert any(c.startswith("swarm-ui|npx vitest run ") for c in calls), calls


def test_a_red_node_run_stops_run_with_exit_1(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    _fake_tools(bin_dir, log)
    (bin_dir / "npm").write_text("#!/usr/bin/env bash\nexit 3\n")
    diff = tmp_path / "diff.txt"
    diff.write_text(f"{UI}/src/vitestWorkers.ts\n")
    result = _run("--run", "--files", str(diff),
                  env_extra={"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"})
    # MUTATION: ignore the node run's exit status -> exit 0 over a red
    # no-company-name.test.mjs.
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "failed (exit 3)" in result.stderr


# --- the output of --run (TEST-OUTPUT, TOKEN-A lane-caps, part of #2524) ------
#
# Measured 2026-10-20..27: 97% of the pool's tokens were cache reads, and a
# whole pytest or vitest transcript printed into a Bash result is re-read on
# every later turn. So each run's full output goes to a log (under
# $SWARM_ARTIFACTS_DIR in a lane), and the agent reads only the tail and, when
# it failed, the failure blocks.


def _loud_tools(bin_dir: Path, *, fail: bool) -> None:
    """uv, npx and npm that print 400 numbered lines -- with a failure block
    in the middle when `fail` -- and exit 1 when `fail`."""
    block = (
        'echo "=================================== FAILURES ==================================="\n'
        'echo "___ test_the_thing ___"\n'
        'echo "E       assert 1 == 2"\n'
        if fail else ""
    )
    for tool in ("uv", "npx", "npm"):
        fake = bin_dir / tool
        fake.write_text(
            "#!/usr/bin/env bash\n"
            f'printf "%s\\n" "{tool} $*" >>"{bin_dir}/args.log"\n'
            'for i in $(seq 1 200); do echo "noise line $i of the run"; done\n'
            + block +
            'for i in $(seq 201 400); do echo "noise line $i of the run"; done\n'
            + ('echo "FAILED tests/test_x.py::test_the_thing"\nexit 1\n' if fail
               else 'echo "400 passed in 1.00s"\n')
        )
        fake.chmod(0o755)


def _run_loud(tmp_path: Path, *, fail: bool, changed: str) -> tuple[subprocess.CompletedProcess[str], Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _loud_tools(bin_dir, fail=fail)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    diff = tmp_path / "diff.txt"
    diff.write_text(changed + "\n")
    result = _run("--run", "--files", str(diff), env_extra={
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "SWARM_ARTIFACTS_DIR": str(artifacts),
    })
    return result, artifacts


def test_a_passing_run_prints_its_tail_not_its_whole_output(tmp_path):
    result, artifacts = _run_loud(tmp_path, fail=False, changed="scripts/related-tests.sh")
    assert result.returncode == 0, result.stderr
    out = result.stdout + result.stderr
    # MUTATION: let the runner write straight to the terminal -> line 1 is
    # printed and this fails.
    assert "noise line 1 of the run" not in out
    assert "400 passed" in out
    # The whole output is kept, in a log under the lane's artifacts.
    logs = sorted(artifacts.glob("related-tests*"))
    assert logs, list(artifacts.iterdir())
    assert any("noise line 1 of the run\n" in p.read_text() for p in logs)


def test_the_kept_log_is_redacted_like_the_printed_tail(tmp_path):
    """The log under SWARM_ARTIFACTS_DIR outlives the lane; it goes through
    common.sh's redact, not only the tail printed from it."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # Built at runtime: nothing in this file may look like a credential.
    value = "q" * 8 + "Z" * 24
    for tool in ("uv", "npx", "npm"):
        fake = bin_dir / tool
        fake.write_text(
            "#!/usr/bin/env bash\n"
            f'echo "api_key={value}"\n'
            'for i in $(seq 1 200); do echo "noise line $i of the run"; done\n'
            'echo "400 passed in 1.00s"\n'
        )
        fake.chmod(0o755)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    diff = tmp_path / "diff.txt"
    diff.write_text("scripts/related-tests.sh\n")
    result = _run("--run", "--files", str(diff), env_extra={
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "SWARM_ARTIFACTS_DIR": str(artifacts),
    })
    assert result.returncode == 0, result.stderr
    logs = sorted(artifacts.glob("related-tests*.log"))
    assert logs, list(artifacts.iterdir())
    text = "".join(p.read_text() for p in logs)
    # The log was written (the control), and the value is not in it.
    # MUTATION: write the run straight to ${log} -> the value is kept.
    assert "noise line 1 of the run" in text
    assert "api_key" in text
    assert value not in text
    assert value not in result.stdout + result.stderr


def test_run_keeps_its_logs_out_of_the_callers_artifacts(tmp_path, monkeypatch):
    """`_run` without an explicit SWARM_ARTIFACTS_DIR does not write into the
    one the caller (a lane) has set."""
    callers = tmp_path / "callers-artifacts"
    callers.mkdir()
    monkeypatch.setenv("SWARM_ARTIFACTS_DIR", str(callers))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _loud_tools(bin_dir, fail=False)
    diff = tmp_path / "diff.txt"
    diff.write_text("scripts/related-tests.sh\n")
    result = _run("--run", "--files", str(diff),
                  env_extra={"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"})
    assert result.returncode == 0, result.stderr
    # The run did write a log (the control) -- just not here.
    assert "full output:" in result.stderr
    assert str(callers) not in result.stdout + result.stderr
    assert list(callers.iterdir()) == []


def test_a_failing_run_prints_the_failure_block_and_the_tail(tmp_path):
    result, artifacts = _run_loud(tmp_path, fail=True, changed="scripts/related-tests.sh")
    assert result.returncode == 1
    out = result.stdout + result.stderr
    # MUTATION: print only the tail -> the assertion in the middle of the
    # output is lost and this fails.
    assert "E       assert 1 == 2" in out
    assert "FAILED tests/test_x.py::test_the_thing" in out
    assert "noise line 1 of the run" not in out
    # The log is named, so the rest is one read away.
    assert str(artifacts) in out


def test_vitest_runs_with_the_dot_reporter(tmp_path):
    result, _ = _run_loud(tmp_path, fail=False, changed=f"{UI}/src/vitestWorkers.ts")
    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "bin" / "args.log").read_text().splitlines()
    vitest = [c for c in calls if c.startswith("npx vitest run")]
    # MUTATION: drop --reporter=dot -> vitest's default reporter prints a
    # line per test file and this fails.
    assert vitest and all("--reporter=dot" in c for c in vitest), calls
    printed = [c for c in _commands(result.stdout) if c.startswith(VITEST_CMD)]
    assert printed and all("--reporter=dot" in c for c in printed)
