#!/usr/bin/env python3
"""Which unit test files a pull request's changed paths reach (B2, owner
decision 2026-10-14, docs/ci.md "A pull request runs the unit tests its change
reaches").

    select_unit_tests.py --changed FILE [--root DIR] [--github-output FILE]
                         [--summary FILE] [--guards FILE]

WHY. A pull request's unit shards ran all ~550 files whatever it changed, and
application.yml's pull-request run took a median 7.6 min (last 150 PRs,
measured 2026-10-14). Main's push run and a merge-train run keep the whole
suite, so a file this misses is caught there, one merge later: the cost of a
miss is a red main, never an untested release.

WHAT IT SELECTS, for a change it can reason about:

  * the repository-wide guard set, ALWAYS, whatever changed: changed-guards.sh's
    GUARD_TESTS array, read from that script (guard_tests) so the list lives
    once. Whole-tree tests no changed path names went unrun on pull requests
    and turned main red (test_collection_stays_cheap for 19 commits,
    test_awaiting_input_readers for 6; CI-GUARDS-ALWAYS, part of #1822);
  * everything `scripts/changed-guards.sh --list` selects for the same paths:
    the guard set and every test whose text names a changed path (--guards:
    that list, read from a file the caller wrote);
  * every test file that imports, directly or through any chain of other
    modules under apps/, scripts/ and tests/unit, a changed Python module;
  * every test whose text names a changed path the way a test builds one: its
    full path, its basename (a generic basename -- Dockerfile, __init__.py --
    only together with one of its directories' names), after first adding
    every file under scripts/ that names a changed script, transitively: a
    change to scripts/lib/common.sh reaches every script that sources it, and
    so every test that runs one of those;
  * `areas` -- docs and swarm-ui -- for the pick step's own directory-reader
    selection, when the change touches docs/ or apps/swarm-ui/.

WHEN IT RUNS EVERYTHING (python=all), naming why, because a selection it
cannot vouch for is worth less than the minutes it saves:

  * a workflow, a composite action, a conftest.py, a pyproject.toml, uv.lock,
    tests/flaky.txt or the quarantine module changed -- each changes how every
    test runs, not what one test reads;
  * the frozen contract (apps/common/swarm_common/) changed -- every
    component imports it;
  * more than MAX_CHANGED paths changed;
  * the selection already covers at least FULL_FRACTION of the suite: then the
    whole suite costs almost nothing more, and a run of the whole suite is one
    main's push run can reuse at an identical tree (B1, scripts/lib/ci_reuse.py).

Pure standard library: the `changes` job runs it with the runner's python3,
before anything is synced. tests/unit/scripts/test_select_unit_tests.py holds
each rule.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

# Above this many changed paths the selection is not worth its doubt: a change
# that wide reaches most of the suite anyway. Of the last 150 merged pull
# requests (measured 2026-10-14) the median changed 7 files, p95 23, and one
# more than 40 -- so the cap catches the rare sweep, not ordinary work.
MAX_CHANGED = 40
# A selection this large runs everything instead (see the module docstring).
FULL_FRACTION = 0.5

# Each changes how every test runs. Prefix match on the repo-relative path.
FALLBACK_PREFIXES = (
    ".github/workflows/",
    ".github/actions/",
    "apps/common/swarm_common/",
)
FALLBACK_EXACT = {
    "uv.lock",
    "tests/flaky.txt",
    "tests/unit/flaky_quarantine.py",
}
FALLBACK_NAMES = {"conftest.py", "pyproject.toml"}

# Names shared by many files: a test naming one alone is not reading THIS one.
# The same idea as changed-guards.sh's GENERIC_BASENAMES, which it also uses.
GENERIC = {
    "__init__.py", "conftest.py", "README.md", "main.py", "app.py", "config.py", "models.py",
    "utils.py", "index.ts", "index.tsx", "types.ts", "package.json", "pyproject.toml",
    "Makefile", "Dockerfile", "cloudbuild.yaml", "entrypoint.sh", "selftest.py",
}

SKIP_PARTS = {"node_modules", ".venv", "__pycache__", "build", "dist", ".git"}


def fallback_reason(changed: list[str]) -> str | None:
    """Why this change runs the whole suite, or None when it can be selected."""
    if len(changed) > MAX_CHANGED:
        return f"{len(changed)} changed paths, more than {MAX_CHANGED}"
    for path in changed:
        name = path.rsplit("/", 1)[-1]
        if path in FALLBACK_EXACT or name in FALLBACK_NAMES or path.startswith(FALLBACK_PREFIXES):
            return f"{path} changes how every test runs"
    return None


def _python_files(root: Path, top: str) -> list[Path]:
    base = root / top
    if not base.is_dir():
        return []
    return sorted(p for p in base.rglob("*.py") if not SKIP_PARTS.intersection(p.relative_to(root).parts))


def module_name(rel: str) -> str:
    """The repo-rooted dotted name of a .py path: apps/a/b/__init__.py -> apps.a.b."""
    parts = rel[:-3].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def imported_names(rel: str, text: str) -> set[str]:
    """Every dotted name a module imports, with each of its prefixes: `import
    a.b.c` runs a/__init__ and a/b/__init__ too. Relative imports are resolved
    to repo-rooted names. Text that does not parse is read with a regex."""
    names: set[str] = set()
    package = module_name(rel).split(".")
    if not rel.endswith("/__init__.py"):
        package = package[:-1]

    def add(name: str) -> None:
        bits = name.split(".")
        for i in range(1, len(bits) + 1):
            names.add(".".join(bits[:i]))

    def relative(level: int, module: str | None) -> str:
        base = package[: len(package) - (level - 1)] if level > 1 else package
        return ".".join([*base, *([module] if module else [])])

    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        for m in re.finditer(r"^\s*(?:from\s+([\w.]+)\s+import\s+([\w, ]+)|import\s+([\w., ]+))", text, re.M):
            if m.group(1):
                add(m.group(1))
                for alias in m.group(2).split(","):
                    if alias.strip():
                        add(f"{m.group(1)}.{alias.split()[0]}")
            else:
                for alias in m.group(3).split(","):
                    if alias.strip():
                        add(alias.split()[0])
        return names
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            base = relative(node.level, node.module) if node.level else (node.module or "")
            if base:
                add(base)
                for alias in node.names:
                    if alias.name != "*":
                        add(f"{base}.{alias.name}")
    return names


def _suffixes(module: str) -> set[str]:
    """Every name an absolute import could reach `module` by: a suffix of its
    repo-rooted name, since sys.path roots are apps/<app>, tests/unit,
    scripts ... (`scheduler.dispatch` is apps.scheduler.scheduler.dispatch).
    A resolved relative import names the whole of it, which is a suffix too."""
    parts = module.split(".")
    return {".".join(parts[i:]) for i in range(len(parts))}


def import_closure(root: Path, changed_py: list[str]) -> set[str]:
    """Every module under apps/, scripts/ and tests/unit that imports a changed
    module, through any chain of imports."""
    files = [*_python_files(root, "apps"), *_python_files(root, "scripts"), *_python_files(root, "tests/unit")]
    rels = [str(p.relative_to(root)) for p in files]
    importers: dict[str, set[str]] = {}
    for rel in rels:
        for name in imported_names(rel, (root / rel).read_text(errors="replace")):
            importers.setdefault(name, set()).add(rel)
    reached: set[str] = set()
    todo = [module_name(p) for p in changed_py]
    seen: set[str] = set()
    while todo:
        module = todo.pop()
        if module in seen:
            continue
        seen.add(module)
        for suffix in _suffixes(module):
            for rel in importers.get(suffix, ()):
                if rel not in reached:
                    reached.add(rel)
                    todo.append(module_name(rel))
    return reached


def _quoted(word: str) -> re.Pattern[str]:
    # A path segment as a test builds it: "word", 'word', /word/, word/ ...
    return re.compile(r"(?:[\"'/])" + re.escape(word) + r"(?:[\"'/])")


def names_path(text: str, path: str) -> bool:
    """`text` names `path` the way a test would: by its whole path, by a
    distinctive basename, or -- a generic basename -- with one of its
    directories' names beside it (`REPO / "images" / "x" / "Dockerfile"`)."""
    if path in text:
        return True
    name = path.rsplit("/", 1)[-1]
    if name not in text:
        return False
    if name not in GENERIC:
        return True
    dirs = path.split("/")[:-1]
    return any(_quoted(d).search(text) for d in dirs)


def script_closure(root: Path, changed: list[str]) -> list[str]:
    """`changed` plus every file under scripts/ that names a changed one by its
    basename, transitively: what sources common.sh changes with it."""
    base = root / "scripts"
    if not base.is_dir():
        return list(changed)
    scripts = sorted(str(p.relative_to(root)) for p in base.rglob("*")
                     if p.is_file() and not SKIP_PARTS.intersection(p.relative_to(root).parts))
    text = {s: (root / s).read_text(errors="replace") for s in scripts}
    out = list(dict.fromkeys(changed))
    todo = [p for p in out if p.startswith("scripts/")]
    while todo:
        path = todo.pop()
        name = path.rsplit("/", 1)[-1]
        if name in GENERIC:
            continue
        for s in scripts:
            if s not in out and name in text[s]:
                out.append(s)
                todo.append(s)
    return out


# Where the one guard list lives (CI-GUARDS-ALWAYS). A bash array of one
# path a line; lanes run it through changed-guards.sh, pull requests here.
GUARD_SCRIPT = "scripts/changed-guards.sh"


def guard_tests(root: Path, array: str = "GUARD_TESTS") -> list[str] | None:
    """The test files in GUARD_SCRIPT's `array`: [] when the tree has no such
    script, None when it has one this cannot read the array from."""
    path = root / GUARD_SCRIPT
    if not path.is_file():
        return []
    found = re.search(rf"^{re.escape(array)}=\(\n(.*?)^\)", path.read_text(errors="replace"), re.M | re.S)
    if not found:
        return None
    names = [line.split("#", 1)[0].strip() for line in found.group(1).splitlines()]
    return [n for n in names if n]


def is_test(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


def select(root: Path, changed: list[str], guards: list[str] | None = None) -> dict:
    """{python, areas, tests, why}: python is `all` or `selected`."""
    changed = sorted({c.strip() for c in changed if c.strip()})
    areas = []
    if any(c.startswith("docs/") for c in changed):
        areas.append("docs")
    if any(c.startswith("apps/swarm-ui/") for c in changed):
        areas.append("swarm-ui")
    reason = fallback_reason(changed)
    if reason:
        return {"python": "all", "areas": "", "tests": [], "why": reason}

    all_tests = sorted(str(p.relative_to(root)) for p in _python_files(root, "tests/unit") if is_test(str(p.relative_to(root))))
    if not all_tests:
        return {"python": "all", "areas": "", "tests": [], "why": "no test file under tests/unit: the checkout is not the repository"}
    always = guard_tests(root)
    if always is None:
        return {"python": "all", "areas": "", "tests": [],
                "why": f"no GUARD_TESTS array could be read from {GUARD_SCRIPT}: the guard set is unknown"}
    chosen: set[str] = set()
    # The change's own test files.
    chosen.update(c for c in changed if c in all_tests)
    # Imports.
    changed_py = [c for c in changed if c.endswith(".py") and c.startswith(("apps/", "scripts/", "tests/unit/"))]
    chosen.update(r for r in import_closure(root, changed_py) if is_test(r) and r in all_tests)
    # Text: a test that names a changed path, or a script that names one.
    virtual = script_closure(root, changed)
    for t in all_tests:
        if t in chosen:
            continue
        text = (root / t).read_text(errors="replace")
        if any(names_path(text, p) for p in virtual):
            chosen.add(t)
    # The guard set, always, and changed-guards.sh's selection: its guard set
    # and its own text readers.
    unknown = []
    for g in [*always, *(guards or [])]:
        if g in all_tests:
            chosen.add(g)
        else:
            unknown.append(g)
    if unknown:
        return {"python": "all", "areas": "", "tests": [],
                "why": f"changed-guards.sh names {len(unknown)} file(s) that are not unit tests here: {unknown[0]}"}
    if len(chosen) >= FULL_FRACTION * len(all_tests):
        return {"python": "all", "areas": "", "tests": [],
                "why": f"the change reaches {len(chosen)} of {len(all_tests)} test files; running all of them costs little more"}
    return {"python": "selected", "areas": " ".join(areas), "tests": sorted(chosen),
            "why": f"{len(changed)} changed path(s) reach {len(chosen)} of {len(all_tests)} test files"}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--changed", required=True, help="file of changed paths, one a line")
    ap.add_argument("--root", default=".")
    ap.add_argument("--guards", help="file of changed-guards.sh --list's test files")
    ap.add_argument("--github-output")
    ap.add_argument("--summary")
    args = ap.parse_args(argv)
    root = Path(args.root).resolve()
    changed = Path(args.changed).read_text().splitlines()
    guards = None
    if args.guards:
        guards = [g.strip() for g in Path(args.guards).read_text().splitlines()
                  if g.strip().startswith("tests/") and g.strip().endswith(".py")]
    result = select(root, changed, guards)
    print(f"python={result['python']} areas={result['areas'] or '(none)'}: {result['why']}")
    for t in result["tests"]:
        print(f"  {t}")
    if args.github_output:
        with open(args.github_output, "a") as out:
            out.write(f"python={result['python']}\nareas={result['areas']}\n")
            out.write(f"tests={' '.join(result['tests'])}\n")
            out.write(f"why={result['why']}\n")
    if args.summary:
        with open(args.summary, "a") as out:
            out.write("### Unit tests this pull request runs\n\n")
            if result["python"] == "all":
                out.write(f"**The whole suite:** {result['why']}.\n\n")
            else:
                out.write(f"**{len(result['tests'])} file(s):** {result['why']}. "
                          "Main's push run and a merge-train run run the whole suite.\n\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
