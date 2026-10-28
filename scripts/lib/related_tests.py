#!/usr/bin/env python3
"""The test files a change reaches, cut into runs of named files
(RELATED-TESTS, part of #1694, owner decision 2026-10-20).

    related_tests.py --changed FILE [--root DIR]

WHY. Agents ran whole suites and lost hours: 36 vitest runs with no file
filter at about 22 min each (2.05 h), 8 pytest runs over whole tests/unit
directories (0.73 h), and both out-of-memory kills of 2026-10-19/20 came from
four vitest shards started in the background. scripts/related-tests.sh asks
this for the tests related to a change and prints (or runs) them as named
files. It never answers with a directory or the whole suite: where CI's own
selector (select_unit_tests.py) falls back to "all", this keeps the files it
can name and leaves the rest to CI, which is the gate.

WHAT IS RELATED:

  * Python (tests/unit): the change's own test files; every test that imports
    a changed module through any chain of imports; every test whose text names
    a changed path (or a script that names one) -- select_unit_tests.py's
    rules, imported rather than restated, so the two cannot drift;
  * swarm-ui (vitest): the change's own test files, and every test file that
    reaches a changed module through relative imports -- `import ... from`,
    `export ... from`, `import '...'`, `import('...')` and `vi.mock('...')` --
    transitively. That is what `vitest related` follows, computed here from
    the source so it needs no node_modules and can be cut into runs. A change
    to what every test runs under (RUNNER_WIDE) relates to no file by import:
    it is named in a note, and CI runs the suite;
  * a change to a swarm-ui stylesheet (`apps/swarm-ui/**.css`) also selects
    the stylesheet guards (STYLESHEET_GUARDS), which read every sheet from
    disk and so are reached by no import. changed-guards.sh reads the same
    list (`--stylesheet-guards`) and runs them before a lane finishes.
  * a change to a swarm-ui stylesheet also selects every vitest file that
    names that sheet in a quoted string -- reads it from disk by its path
    relative to src or by its file name -- since no import reaches those
    either (the named-sheet readers, beside STYLESHEET_GUARDS);
  * except the spacing slices (SPACING-NARROW, #2362): a spacing.<group>.test.tsx
    the import graph reaches is kept only when the change reaches a module one
    of its SPACING_GROUPS routes' SectionBody cases renders, or reaches it
    other than through an App.tsx -> screen import (App.tsx, the shell, the
    sweep, spaceprobe). A changed src/styles sheet, or an App.tsx or sweep that
    does not parse, keeps every slice; spacing.test.tsx is kept with any slice.
  * swarm-ui (node:test): any change under apps/swarm-ui/ also names the
    package's `npm run test:node` (once), when its package.json declares that
    script. test/*.test.mjs and tests/run.mjs run under node, not vitest, and
    read the source tree from disk -- no-company-name.test.mjs greps every
    file for the company's name -- so no import reaches them. CI's ui-build
    runs them; four lanes that never did went red there (#2455).

OUTPUT, one line each, tab-separated:

    pytest<TAB><file> <file> ...     repo-relative, at most ~PYTEST_RUN_WEIGHT CI-seconds
    vitest<TAB><file> <file> ...     relative to apps/swarm-ui, at most ~VITEST_RUN_WEIGHT CI-seconds
                                     and VITEST_FILES_PER_RUN files (a file heavier alone runs alone)
    node<TAB><package dir>           run `npm run test:node` there; after the vitest runs
    note<TAB><text>

Pure standard library, like select_unit_tests.py.
"""

from __future__ import annotations

import argparse
import json
import math
import posixpath
import re
import sys
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import select_unit_tests as sut  # noqa: E402

UI = "apps/swarm-ui"
DURATIONS = "tests/unit/scripts/unit_test_durations.json"
# Per-file vitest CI-seconds, keyed relative to apps/swarm-ui. Only the files
# far over the default are recorded; scripts/refresh-ui-weights.sh rewrites it
# from the logs of main's latest green application.yml run.
UI_DURATIONS = "tests/unit/scripts/ui_test_durations.json"

# One pytest run's weight in CI-seconds. The same 540 as changed-guards.sh's
# SHARD_HINT_WEIGHT: about 400 s on a 6-core container, under the 540 s a
# command there is held to and the 10 minutes an agent's command may take.
PYTEST_RUN_WEIGHT = 540
# One vitest run's weight in CI-seconds: the same 540 as PYTEST_RUN_WEIGHT and
# related-tests.sh's RUN_SECONDS, the limit each run is held to with --run.
# Cutting by file count alone put all nine spacing.*.test.tsx slices (252-485 s
# each) in one 50-file run that could not finish inside it: 40+ agents timed
# out re-running them in 2026-10-25/26 (#2362).
VITEST_RUN_WEIGHT = 540
# The weight of a file ui_test_durations.json does not record. The whole UI
# suite (463 files) took ~22 min on 4 cores, ~2.9 s a file.
VITEST_DEFAULT_SECONDS = 2.9
# A cap on one vitest run's files whatever their weight, so a run of many
# unrecorded files stays about 2.5 min (50 x 2.9 s) and a change to types.ts
# stays "a few" runs.
VITEST_FILES_PER_RUN = 50

# swarm-ui files every component test runs under. No test imports them, so
# the import graph relates them to nothing; naming the whole suite instead is
# the run this exists to stop.
RUNNER_WIDE = {
    "vitest.config.ts", "vite.config.ts", "tsconfig.json", "package.json",
    "package-lock.json", "src/__tests__/setup.ts",
}

# THE STYLESHEET GUARDS (RELATED-CSS-GUARDS, part of #1914): source scans
# that read EVERY sheet (`import.meta.glob('../styles/*.css')`, directly or
# through sheets.ts) and assert over the text, so any line of any .css can turn
# them red. No import connects them to the sheet a change edits, so the graph
# above never selects them: measured 2026-10-22 on PR 1905, whose duplicate
# `.rn-filters > .c-seg > button` in styles/intake.css CI's gate and sections
# guards refused after its implement, fix and re-review agents had run this
# selection and changed-guards.sh without either. Any swarm-ui .css change
# selects all of them, once. THE ONE LIST: changed-guards.sh asks for it with
# `--stylesheet-guards`. Only no-render scans of every sheet belong here; the
# ~170 screen tests that glob the sheets to compute the cascade (marks.ts,
# spacingsweep.tsx, phone.chrome) are behaviour tests CI runs on a CSS change.
STYLESHEET_GUARDS = (
    "src/__tests__/stylesheet.gate.test.ts",      # duplicate rules, keyframes, lost comment openers
    "src/__tests__/sections.stylesheet.test.ts",  # each src/styles/*.css parses, no duplicate rules
    "src/__tests__/letterspacing.test.ts",        # no tracking in any sheet
    "src/__tests__/typescale.test.ts",            # no raw font sizes or all-caps in any sheet
)

# THE NAMED-SHEET READERS (NAMED-SHEETS, part of #1914, owner decision
# 2026-10-22: "Yes, a name-keyed rule"). Beside the guards above, which read
# every sheet, some tests read ONE sheet from disk by name --
# `readFileSync(resolve(__dirname, '../styles/repograph.css'))`,
# `join(__dirname, '..', 'styles', 'theater.css')`, a helper's
# `css('theater.css')` -- and no import connects them to a change of that
# sheet either (found by RELATED-CSS-GUARDS, PR 1921: browser.badges,
# repograph.vqa-l08, repositories.c6j, repositories.graph, theater.followup,
# theater.v2, theater.take). So a changed swarm-ui sheet also selects every
# vitest file holding a quoted string that IS the sheet: its file name alone
# (`'theater.css'`), or a path ending in `/` + its path relative to src
# (`'../styles/theater.css'`). A bare word is not a name: `theater` in a test
# title, or `'styles/'` for a change to styles.css, selects nothing. Only
# quoted strings count, so a sheet named in a comment's backticks does not.
_QUOTED_SHEET = re.compile(r"""(['"])([^'"\n]*?\.css)(?:\?[^'"\n]*)?\1""")

UI_SOURCE_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs")
UI_RESOLVE_EXTS = ("", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".d.ts")
UI_SKIP = {"node_modules", "dist", "build", "coverage"}

_IMPORT_PATTERNS = (
    re.compile(r"""\b(?:import|export)\s[^'";]*?\bfrom\s*['"]([^'"]+)['"]"""),
    re.compile(r"""\bimport\s*['"]([^'"]+)['"]"""),
    re.compile(r"""\bimport\s*\(\s*['"]([^'"]+)['"]\s*\)"""),
    re.compile(r"""\bvi\.(?:mock|doMock|importActual|importMock)\s*\(\s*['"]([^'"]+)['"]"""),
)


def is_vitest_file(rel: str) -> bool:
    """vitest.config.ts's `include`: src/**/*.test.ts(x) and test/**/*.test.tsx."""
    if rel.startswith("src/"):
        return rel.endswith((".test.ts", ".test.tsx"))
    return rel.startswith("test/") and rel.endswith(".test.tsx")


def _ui_files(ui_root: Path) -> list[str]:
    out = []
    for top in ("src", "test"):
        base = ui_root / top
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            rel = path.relative_to(ui_root)
            if path.is_file() and not UI_SKIP.intersection(rel.parts):
                out.append(rel.as_posix())
    return sorted(out)


def _resolve(importer: str, spec: str, known: set[str]) -> str | None:
    if not spec.startswith("."):
        return None
    spec = spec.split("?", 1)[0]
    base = posixpath.normpath(posixpath.join(posixpath.dirname(importer), spec))
    for candidate in (*(base + e for e in UI_RESOLVE_EXTS),
                      *(f"{base}/index{e}" for e in UI_SOURCE_EXTS)):
        if candidate in known:
            return candidate
    return None


def ui_related(ui_root: Path, changed_ui: list[str]) -> list[str]:
    """Vitest files (relative to ui_root) that are, or import, a changed file."""
    files = _ui_files(ui_root)
    known = set(files)
    importers: dict[str, set[str]] = {}
    sheets_named: dict[str, set[str]] = {}
    for rel in files:
        if not rel.endswith(UI_SOURCE_EXTS):
            continue
        text = (ui_root / rel).read_text(errors="replace")
        if is_vitest_file(rel):
            sheets_named[rel] = {m.group(2) for m in _QUOTED_SHEET.finditer(text)}
        for pattern in _IMPORT_PATTERNS:
            for spec in pattern.findall(text):
                target = _resolve(rel, spec, known)
                if target and target != rel:
                    importers.setdefault(target, set()).add(rel)
    reached: set[str] = set()
    todo = [c for c in changed_ui if c in known]
    while todo:
        rel = todo.pop()
        if rel in reached:
            continue
        reached.add(rel)
        todo.extend(importers.get(rel, ()))
    changed_sheets = [c for c in changed_ui if c.endswith(".css")]
    if changed_sheets:
        reached.update(stylesheet_guards(known))
        reached.update(t for t, names in sheets_named.items()
                       if any(names_sheet(n, s) for n in names for s in changed_sheets))
        # The sweep reads every one of these into every route's cascade, by
        # `?raw` import and import.meta.glob, which no edge above follows.
        if any(s == "src/styles.css" or _SWEPT_SHEET.fullmatch(s) for s in changed_sheets):
            reached.update(k for k in known if _SLICE.fullmatch(k) or k == SLICE_CHECK)
    reached -= spacing_dropped(ui_root, known, importers, reached, changed_ui)
    return sorted(r for r in reached if is_vitest_file(r))


# THE SPACING SLICES A CHANGE REACHES (SPACING-NARROW, part of #2362).
# spacingsweep.tsx imports {App, SECTIONS} from '../App', so by import every
# module App.tsx reaches reached all nine spacing.<group>.test.tsx slices, at
# 252-437 CI-seconds each: 40+ agents timed out re-running them on 2026-10-25/26
# for changes to one screen. A slice renders App at its SPACING_GROUPS routes
# only, so it is kept when the change reaches a module one of its routes'
# SectionBody case renders, OR reaches the slice by a path that avoids every
# App -> screen import -- the shell (Spine, a hook App calls), the sweep or
# spaceprobe, which render on every route. An App import counts as a screen's
# only when every name it brings in appears in App.tsx inside SectionBody's
# cases and nowhere else; a module App also imports a card or hook from stays
# a shell path. A changed sheet keeps every slice: the sweep globs every
# src/styles/*.css into every route's cascade. Whatever does not parse -- no
# SPACING_GROUPS, no SectionBody, a route with no case or a tag App neither
# imports nor defines -- keeps every slice, so a reshaped App.tsx costs time,
# never a missed slice. spacingsweep.tsx and App.tsx are read, never edited.
APP = "src/App.tsx"
SWEEP = "src/__tests__/spacingsweep.tsx"
SLICE_CHECK = "src/__tests__/spacing.test.tsx"
_SLICE = re.compile(r"src/__tests__/spacing\.([^./]+)\.test\.tsx")
_SWEPT_SHEET = re.compile(r"src/styles/[^/]+\.css")
_GROUPS = re.compile(r"\bexport\s+const\s+SPACING_GROUPS\b[^=]*=\s*\[(.*?)^\]", re.S | re.M)
_GROUP = re.compile(r"\bname:\s*'([^']+)'\s*,\s*routes:\s*\[([^\]]*)\]")
_CASE = re.compile(r"^(\s*)case\s+'([^']+)'\s*:", re.M)
_TAG = re.compile(r"<([A-Z][A-Za-z0-9_]*)")
_COMMENT = re.compile(r"/\*.*?\*/|//[^\n]*", re.S)
_IMPORT = re.compile(r"^import\s+(?!type\s)([^'\";]*?)\s+from\s*['\"]([^'\"]+)['\"]", re.M)


class SpacingPlan(NamedTuple):
    groups: dict[str, list[str]]   # slice name -> its routes
    routes: dict[str, set[str]]    # route -> the modules its case renders (APP for a local one)
    cut: set[str]                  # modules App imports only for SectionBody's cases


def _imported_names(clause: str) -> list[str]:
    """Local names of `Default, { a, b as c, type T }`."""
    names = []
    head, _, rest = clause.partition("{")
    for part in [head, *rest.rstrip("} \n").split(",")]:
        part = part.strip().strip(",").strip()
        if not part or part.startswith(("type ", "* as")):
            continue
        names.append(part.split(" as ")[-1].strip())
    return [n for n in names if n.isidentifier()]


def spacing_plan(ui_root: Path, known: set[str]) -> SpacingPlan | None:
    """SPACING_GROUPS' routes and the modules App.tsx's SectionBody case for
    each renders, or None when any of it does not parse."""
    if APP not in known or SWEEP not in known:
        return None
    sweep = (ui_root / SWEEP).read_text(errors="replace")
    block = _GROUPS.search(sweep)
    if not block:
        return None
    groups = {m.group(1): re.findall(r"'([^']+)'", m.group(2)) for m in _GROUP.finditer(block.group(1))}
    if not groups or len(groups) != len(re.findall(r"\bname:\s*'", block.group(1))):
        return None
    app = (ui_root / APP).read_text(errors="replace")
    start = app.find("function SectionBody")
    if start < 0:
        return None
    end = re.compile(r"^(?:export\s+)?(?:function|const|class)\s", re.M).search(app, start + 1)
    body = app[start:end.start() if end else len(app)]
    # Each case's text runs to the next label; fall-through labels share the
    # next label's return.
    marks = [*_CASE.finditer(body), *re.finditer(r"^\s*default\s*:", body, re.M)]
    marks.sort(key=lambda m: m.start())
    raw: dict[str, str] = {}
    rest = app[:start] + body[:marks[0].start()] if marks else app
    for i, mark in enumerate(marks):
        stop = marks[i + 1].start() if i + 1 < len(marks) else len(body)
        if mark.re is _CASE:
            raw[mark.group(2)] = body[mark.end():stop]
        else:
            rest += body[mark.start():stop]
    rest += app[start + len(body):]
    segments: dict[str, str] = {}
    shared = ""
    for route in reversed(list(raw)):
        text = _COMMENT.sub("", raw[route])
        shared = segments[route] = text if text.strip() else shared
    imports: dict[str, str] = {}
    for m in _IMPORT.finditer(app):
        target = _resolve(APP, m.group(2), known)
        if target:
            imports.update(dict.fromkeys(_imported_names(m.group(1)), target))
    local = set(re.findall(r"^(?:export\s+)?(?:function|const|class)\s+([A-Z]\w*)", app, re.M))
    routes: dict[str, set[str]] = {}
    for route in (r for rs in groups.values() for r in rs):
        tags = set(_TAG.findall(segments.get(route, "")))
        if not tags or not tags <= set(imports) | local:
            return None
        routes[route] = {imports.get(t, APP) for t in tags}
    # App's import of a module is a screen's when each name it brings in is
    # used inside SectionBody's cases and nowhere else in App.tsx.
    rest = _IMPORT.sub("", _COMMENT.sub("", rest))
    in_cases = "\n".join(segments.values())
    by_module: dict[str, set[str]] = {}
    for name, target in imports.items():
        by_module.setdefault(target, set()).add(name)
    bare = {t for spec in re.findall(r"^import\s*['\"]([^'\"]+)['\"]", app, re.M)
            if (t := _resolve(APP, spec, known))}

    def used(name: str, text: str) -> bool:
        return re.search(rf"\b{re.escape(name)}\b", text) is not None

    cut = {mod for mod, names in by_module.items()
           if mod not in bare and all(used(n, in_cases) and not used(n, rest) for n in names)}
    return SpacingPlan(groups, routes, cut)


def spacing_dropped(ui_root: Path, known: set[str], importers: dict[str, set[str]],
                    reached: set[str], changed_ui: list[str]) -> set[str]:
    """The reached spacing slices the change cannot alter (SPACING-NARROW)."""
    slices = {r: m.group(1) for r in reached if (m := _SLICE.fullmatch(r))}
    if not slices or any(c.endswith(".css") for c in changed_ui):
        return set()
    plan = spacing_plan(ui_root, known)
    if plan is None:
        return set()
    # Reachability with every App -> screen edge cut.
    kept: set[str] = set()
    todo = [c for c in changed_ui if c in known]
    while todo:
        rel = todo.pop()
        if rel in kept:
            continue
        kept.add(rel)
        todo.extend(i for i in importers.get(rel, ()) if not (i == APP and rel in plan.cut))
    dropped = set()
    for path, name in slices.items():
        if name not in plan.groups:
            continue
        screens = set().union(*(plan.routes[r] for r in plan.groups[name])) - {APP}
        if path not in kept and not screens & reached:
            dropped.add(path)
    if SLICE_CHECK in reached and SLICE_CHECK not in kept and set(slices) <= dropped:
        dropped.add(SLICE_CHECK)
    return dropped


def names_sheet(literal: str, sheet: str) -> bool:
    """Whether a quoted string in a test names `sheet` (relative to ui_root):
    its file name alone, or a path ending in its path relative to src."""
    rel = sheet[len("src/"):] if sheet.startswith("src/") else sheet
    return literal == posixpath.basename(sheet) or literal == rel or literal.endswith("/" + rel)


def stylesheet_guards(known: set[str]) -> list[str]:
    """STYLESHEET_GUARDS that exist in this tree (a gone file is never named)."""
    return [g for g in STYLESHEET_GUARDS if g in known]


def python_related(root: Path, changed: list[str]) -> list[str]:
    """tests/unit files related to the change, by select_unit_tests.py's rules
    without its fall-back to the whole suite."""
    all_tests = sorted(str(p.relative_to(root)) for p in sut._python_files(root, "tests/unit")
                       if sut.is_test(str(p.relative_to(root))))
    known = set(all_tests)
    chosen = {c for c in changed if c in known}
    changed_py = [c for c in changed if c.endswith(".py") and c.startswith(("apps/", "scripts/", "tests/unit/"))]
    # The closure parses every module in the repository (~10 s here): only
    # worth it when a Python file changed.
    if changed_py:
        chosen.update(r for r in sut.import_closure(root, changed_py) if r in known)
    # The same text rule as CI and changed-guards.sh (#2299): a test naming a
    # doc that cites a changed path reaches the change through the doc.
    virtual = sut.script_closure(root, sut.doc_closure(root, changed))
    for test in all_tests:
        if test not in chosen:
            text = (root / test).read_text(errors="replace")
            if any(sut.names_path(text, p) for p in virtual):
                chosen.add(test)
    return sorted(chosen)


def pytest_runs(root: Path, tests: list[str]) -> list[list[str]]:
    """Longest file first onto the lightest run, as CI cuts its unit shards;
    a file with no recorded weight counts the median."""
    if not tests:
        return []
    try:
        weights = json.loads((root / DURATIONS).read_text())
    except (OSError, ValueError):
        weights = {}
    values = sorted(weights.values())
    median = values[len(values) // 2] if values else 1
    weighted = sorted(((weights.get(t, median), t) for t in tests), key=lambda x: (-x[0], x[1]))
    count = max(1, math.ceil(sum(w for w, _ in weighted) / PYTEST_RUN_WEIGHT))
    runs: list[tuple[float, list[str]]] = [(0, []) for _ in range(min(count, len(tests)))]
    for weight, test in weighted:
        i = min(range(len(runs)), key=lambda j: runs[j][0])
        runs[i] = (runs[i][0] + weight, [*runs[i][1], test])
    return [sorted(files) for _, files in runs if files]


def vitest_weights(root: Path) -> dict[str, float]:
    """ui_test_durations.json, or nothing when it is missing or unreadable."""
    try:
        weights = json.loads((root / UI_DURATIONS).read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(weights, dict):
        return {}
    return {k: float(v) for k, v in weights.items() if isinstance(v, (int, float))}


def vitest_runs(root: Path, tests: list[str]) -> list[list[str]]:
    """Heaviest file first onto the lightest run it fits, as pytest_runs cuts
    pytest files; a run that would go over VITEST_RUN_WEIGHT CI-seconds or
    VITEST_FILES_PER_RUN files takes no more, and a file heavier than the
    weight alone runs alone."""
    if not tests:
        return []
    weights = vitest_weights(root)
    weighted = sorted(((weights.get(t, VITEST_DEFAULT_SECONDS), t) for t in tests),
                      key=lambda x: (-x[0], x[1]))
    total = sum(w for w, _ in weighted)
    count = max(1, math.ceil(total / VITEST_RUN_WEIGHT), math.ceil(len(tests) / VITEST_FILES_PER_RUN))
    runs: list[tuple[float, list[str]]] = [(0.0, []) for _ in range(min(count, len(tests)))]
    for weight, test in weighted:
        fits = [j for j, (w, files) in enumerate(runs)
                if len(files) < VITEST_FILES_PER_RUN and (not files or w + weight <= VITEST_RUN_WEIGHT)]
        if fits:
            i = min(fits, key=lambda j: runs[j][0])
        else:
            runs.append((0.0, []))
            i = len(runs) - 1
        runs[i] = (runs[i][0] + weight, [*runs[i][1], test])
    return [sorted(files) for _, files in runs if files]


def declares_test_node(package_dir: Path) -> bool:
    """Whether the package.json in package_dir has a `test:node` script."""
    try:
        scripts = json.loads((package_dir / "package.json").read_text()).get("scripts")
    except (OSError, ValueError, AttributeError):
        return False
    return isinstance(scripts, dict) and "test:node" in scripts


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--changed", required=True, help="file of changed paths, one a line")
    ap.add_argument("--root", default=".")
    ap.add_argument("--stylesheet-guards", action="store_true",
                    help="print only the stylesheet guards the change selects, one a line, "
                         "relative to apps/swarm-ui (changed-guards.sh)")
    args = ap.parse_args(argv)
    root = Path(args.root).resolve()
    changed = sorted({c.strip() for c in Path(args.changed).read_text().splitlines() if c.strip()})
    changed_ui = [c[len(UI) + 1:] for c in changed if c.startswith(f"{UI}/")]
    if args.stylesheet_guards:
        if any(c.endswith(".css") for c in changed_ui):
            for guard in stylesheet_guards(set(_ui_files(root / UI))):
                print(guard)
        return 0
    for rel in changed_ui:
        if rel in RUNNER_WIDE:
            print(f"note\t{UI}/{rel} changes how every component test runs; no test imports it, "
                  "so none is named for it here and CI runs the whole UI suite")
    lines = []
    for run in pytest_runs(root, python_related(root, changed)):
        lines.append("pytest\t" + " ".join(run))
    for run in vitest_runs(root, ui_related(root / UI, changed_ui)):
        lines.append("vitest\t" + " ".join(run))
    # The node:test runners read the tree from disk, so any UI change reaches
    # them (#2455); one run, whichever and however many files changed.
    if changed_ui and declares_test_node(root / UI):
        lines.append(f"node\t{UI}")
    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
