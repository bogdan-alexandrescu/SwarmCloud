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
    it is named in a note, and CI runs the suite.

OUTPUT, one line each, tab-separated:

    pytest<TAB><file> <file> ...     repo-relative, at most ~PYTEST_RUN_WEIGHT CI-seconds
    vitest<TAB><file> <file> ...     relative to apps/swarm-ui, at most VITEST_FILES_PER_RUN
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

sys.path.insert(0, str(Path(__file__).resolve().parent))

import select_unit_tests as sut  # noqa: E402

UI = "apps/swarm-ui"
DURATIONS = "tests/unit/scripts/unit_test_durations.json"

# One pytest run's weight in CI-seconds. The same 540 as changed-guards.sh's
# SHARD_HINT_WEIGHT: about 400 s on a 6-core container, under the 540 s a
# command there is held to and the 10 minutes an agent's command may take.
PYTEST_RUN_WEIGHT = 540
# One vitest run's files. The whole UI suite (463 files) took ~22 min on 4
# cores, ~2.9 s a file, so 50 is about 2.5 min: well under the command limit,
# and few enough runs that a change to types.ts stays "a few".
VITEST_FILES_PER_RUN = 50

# swarm-ui files every component test runs under. No test imports them, so
# the import graph relates them to nothing; naming the whole suite instead is
# the run this exists to stop.
RUNNER_WIDE = {
    "vitest.config.ts", "vite.config.ts", "tsconfig.json", "package.json",
    "package-lock.json", "src/__tests__/setup.ts",
}

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
    for rel in files:
        if not rel.endswith(UI_SOURCE_EXTS):
            continue
        text = (ui_root / rel).read_text(errors="replace")
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
    return sorted(r for r in reached if is_vitest_file(r))


def python_related(root: Path, changed: list[str]) -> list[str]:
    """tests/unit files related to the change, by select_unit_tests.py's rules
    without its fall-back to the whole suite."""
    all_tests = sorted(str(p.relative_to(root)) for p in sut._python_files(root, "tests/unit")
                       if sut.is_test(str(p.relative_to(root))))
    known = set(all_tests)
    chosen = {c for c in changed if c in known}
    changed_py = [c for c in changed if c.endswith(".py") and c.startswith(("apps/", "scripts/", "tests/unit/"))]
    chosen.update(r for r in sut.import_closure(root, changed_py) if r in known)
    virtual = sut.script_closure(root, changed)
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


def vitest_runs(tests: list[str]) -> list[list[str]]:
    if not tests:
        return []
    count = math.ceil(len(tests) / VITEST_FILES_PER_RUN)
    size = math.ceil(len(tests) / count)
    return [tests[i:i + size] for i in range(0, len(tests), size)]


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--changed", required=True, help="file of changed paths, one a line")
    ap.add_argument("--root", default=".")
    args = ap.parse_args(argv)
    root = Path(args.root).resolve()
    changed = sorted({c.strip() for c in Path(args.changed).read_text().splitlines() if c.strip()})
    changed_ui = [c[len(UI) + 1:] for c in changed if c.startswith(f"{UI}/")]
    for rel in changed_ui:
        if rel in RUNNER_WIDE:
            print(f"note\t{UI}/{rel} changes how every component test runs; no test imports it, "
                  "so none is named for it here and CI runs the whole UI suite")
    lines = []
    for run in pytest_runs(root, python_related(root, changed)):
        lines.append("pytest\t" + " ".join(run))
    for run in vitest_runs(ui_related(root / UI, changed_ui)):
        lines.append("vitest\t" + " ".join(run))
    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
