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
    modules under apps/, scripts/ and tests/unit, a changed Python module --
    as Python resolves it (an absolute name only through a directory that can
    stand on sys.path), a lazy import only into what calls its function, a
    mounted router only into what requests its URLs or walks every route --
    and the tests under a changed or reached conftest.py (import_closure;
    RELATED-NARROW, part of #2688: one swarm-api route reached 1,428 of 1,707
    test files before);
  * every test whose text names a changed path the way a test builds one: its
    full path, its basename (a generic basename -- Dockerfile, __init__.py --
    only together with one of its directories' names), after first adding
    every file under scripts/ that names a changed script, transitively: a
    change to scripts/lib/common.sh reaches every script that sources it, and
    so every test that runs one of those;
  * every test whose text names a doc that cites a changed path (doc_closure):
    a docs/**/*.md naming a changed non-doc path by its FULL repo-relative
    path counts as changed, one level deep. PR #2267 moved PushTestCell out of
    Access.tsx while docs/onboarding.md still cited it there; the tests that
    resolve the citation name only onboarding.md, so nothing selected them and
    main's push went red (#2299);
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


def import_edges(rel: str, text: str) -> dict[str, frozenset[str] | None]:
    """Every dotted name a module imports, with each of its prefixes: `import
    a.b.c` runs a/__init__ and a/b/__init__ too. Relative imports are resolved
    to repo-rooted names. Text that does not parse is read with a regex.

    Each name maps to None when the module imports it as it loads (at module
    level, under an `if` or a `try` too), else to the top-level functions and
    classes whose bodies import it: a lazy import, which runs only when one of
    them is called. swarm_api/__init__.py imports .main -- every route -- only
    inside create_app(), so `from swarm_api import forgeapp` loads no route
    (RELATED-NARROW, part of #2688)."""
    edges: dict[str, frozenset[str] | None] = {}
    package = module_name(rel).split(".")
    if not rel.endswith("/__init__.py"):
        package = package[:-1]

    def add(name: str, owner: str | None) -> None:
        bits = name.split(".")
        for i in range(1, len(bits) + 1):
            key = ".".join(bits[:i])
            if owner is None or (key in edges and edges[key] is None):
                edges[key] = None
            else:
                edges[key] = frozenset({*(edges.get(key) or ()), owner})

    def relative(level: int, module: str | None) -> str:
        base = package[: len(package) - (level - 1)] if level > 1 else package
        return ".".join([*base, *([module] if module else [])])

    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        for m in re.finditer(r"^\s*(?:from\s+([\w.]+)\s+import\s+([\w, ]+)|import\s+([\w., ]+))", text, re.M):
            if m.group(1):
                add(m.group(1), None)
                for alias in m.group(2).split(","):
                    if alias.strip():
                        add(f"{m.group(1)}.{alias.split()[0]}", None)
            else:
                for alias in m.group(3).split(","):
                    if alias.strip():
                        add(alias.split()[0], None)
        return edges

    def record(node: ast.AST, owner: str | None) -> None:
        if isinstance(node, ast.Import):
            for alias in node.names:
                add(alias.name, owner)
        elif isinstance(node, ast.ImportFrom):
            base = relative(node.level, node.module) if node.level else (node.module or "")
            if base:
                add(base, owner)
                for alias in node.names:
                    if alias.name != "*":
                        add(f"{base}.{alias.name}", owner)

    def walk(node: ast.AST, owner: str | None, cls: str | None) -> None:
        # A function body runs when it is called, a class body as the module
        # loads. The owner is the top-level name an importer has to reach the
        # function by: its own, or for a method its class's.
        for child in ast.iter_child_nodes(node):
            record(child, owner)
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                walk(child, owner if owner is not None else (cls or child.name), None)
            elif isinstance(child, ast.ClassDef):
                walk(child, owner, cls if cls is not None or owner is not None else child.name)
            else:
                walk(child, owner, cls)

    walk(tree, None, None)
    return edges


def imported_names(rel: str, text: str) -> set[str]:
    """Every dotted name a module imports, eagerly or lazily (import_edges)."""
    return set(import_edges(rel, text))


def _suffixes(root: Path, rel: str) -> set[str]:
    """Every name an absolute import could reach the module at `rel` by: a
    suffix of its repo-rooted name whose stripped prefix is a directory that
    can stand on sys.path -- the repository root, or a directory that is not a
    package (apps/<app>, tests/unit, scripts/lib ...; `scheduler.dispatch` is
    apps.scheduler.scheduler.dispatch). A resolved relative import names the
    whole of it, the root's suffix.

    WHY NOT EVERY SUFFIX (RELATED-NARROW, part of #2688). Counting a package's
    directory as a root made a module's bare name match every import of that
    name: apps/agent-worker/agent_worker/startup.py's `import platform` (the
    standard library) read as an import of swarm_api/routes/platform.py, and
    `main`, `config`, `errors` and `models` likewise, so a change to one
    swarm-api route reached 1,428 of 1,707 test files, the worker's among them
    (measured on main at 0409b8d, 2026-11-03). Python finds `platform` there
    only if swarm_api/routes itself is on sys.path, and nothing puts a package
    directory there."""
    parts = module_name(rel).split(".")
    dirs = rel.split("/")[:-1]
    out = {".".join(parts)}
    for i in range(1, len(parts)):
        if not (root.joinpath(*dirs[:i]) / "__init__.py").is_file():
            out.add(".".join(parts[i:]))
    return out


# How much of a module a change reaches: all of it (None); only what calls one
# of these top-level names -- a lazy import (CALL, names); or only what
# requests one of these URLs -- an app that mounts a changed router (URL,
# patterns). Two different partial reaches meet as the whole module.
CALL = "call"
URL = "url"
Reach = "tuple[str, frozenset[str]] | None"


def _meet(a: tuple[str, frozenset[str]] | None,
          b: tuple[str, frozenset[str]] | None) -> tuple[str, frozenset[str]] | None:
    if a is None or b is None or a[0] != b[0]:
        return None
    return (a[0], a[1] | b[1])


_VERBS = {"get", "post", "put", "patch", "delete", "head", "options", "trace", "api_route", "websocket"}


def _static(node: ast.AST, consts: dict[str, str]) -> str | None:
    """A string expression's value when the module alone fixes it: a literal,
    a module-level constant, an f-string or `+` of those; else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return consts.get(node.id)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _static(node.left, consts), _static(node.right, consts)
        return None if left is None or right is None else left + right
    if isinstance(node, ast.JoinedStr):
        out = []
        for part in node.values:
            if isinstance(part, ast.FormattedValue):
                if part.conversion != -1 or part.format_spec is not None:
                    return None
                value = _static(part.value, consts)
            else:
                value = _static(part, consts)
            if value is None:
                return None
            out.append(value)
        return "".join(out)
    return None


def route_urls(text: str) -> list[str]:
    """Every URL path a FastAPI router module serves, its APIRouter prefix
    included -- or [] when it is not one, or serves a path this cannot read
    (a computed path, add_api_route, a root path): a partial list would gate
    out the tests of the paths it missed, so then the module is not gated."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    consts: dict[str, str] = {}
    prefixes: dict[str, str] = {}
    for node in tree.body:
        target, value = None, None
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target, value = node.targets[0].id, node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            target, value = node.target.id, node.value
        if target is None or value is None:
            continue
        if isinstance(value, ast.Call) and getattr(value.func, "id", getattr(value.func, "attr", "")) == "APIRouter":
            prefix = ""
            for kw in value.keywords:
                if kw.arg == "prefix":
                    found = _static(kw.value, consts)
                    if found is None:
                        return []
                    prefix = found
            prefixes[target] = prefix
            continue
        found = _static(value, consts)
        if found is not None:
            consts[target] = found
    if not prefixes:
        return []
    paths: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr in {"add_api_route", "add_api_websocket_route", "include_router", "mount"}:
            return []
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for deco in node.decorator_list:
            if not (isinstance(deco, ast.Call) and isinstance(deco.func, ast.Attribute)
                    and isinstance(deco.func.value, ast.Name) and deco.func.value.id in prefixes):
                continue
            if deco.func.attr not in _VERBS:
                continue
            arg = deco.args[0] if deco.args else next((k.value for k in deco.keywords if k.arg == "path"), None)
            path = None if arg is None else _static(arg, consts)
            if path is None:
                return []
            full = prefixes[deco.func.value.id] + path
            if not full.strip("/"):
                return []
            paths.append(full)
    return sorted(set(paths))


def url_pattern(path: str) -> str:
    """A regex for `path` as a test requests it: each `{param}` any one segment
    -- a literal, or an f-string's `{TENANT}` -- and nothing after it but a
    quote, a query or the end, so /v1/budget is not /v1/budget/x."""
    parts = re.split(r"\{[^}]*\}", path)
    return r"[^/\s\"'?#]+".join(re.escape(part) for part in parts) + r"""(?=["'?#\s]|$)"""


# A test that walks every route an app mounts -- the credential and admin
# surface guards (test_no_route_serves_credentials.py ...) -- reads a changed
# router whichever URL it serves.
_ENUMERATES_ROUTES = re.compile(r"\bapp\.routes\b|\bapp\.router\.routes\b|\.openapi\(\)|\bopenapi_schema\b|\.url_path_for\(")


def _mounts(text: str, rel: str) -> bool:
    """Whether a module includes the router module at `rel` in an app, by its
    module name (`app.include_router(budget_settings.router)`)."""
    stem = rel.rsplit("/", 1)[-1][:-3]
    return "include_router(" in text and re.search(rf"\b{re.escape(stem)}\.router\b", text) is not None


def import_closure(root: Path, changed_py: list[str]) -> set[str]:
    """Every module under apps/, scripts/ and tests/unit that imports a changed
    module, through any chain of imports, and every test a reached conftest.py
    reaches.

    Not every chain carries the whole change (RELATED-NARROW, part of #2688):

    * a module reached only through a lazy import (import_edges) changes only
      when a function holding that import is called, so it passes the change
      on only to importers whose code names one of those functions;
    * an app that mounts a changed router (swarm_api/main.py's
      `include_router(budget_settings.router)`) changes only at that router's
      URLs, so what is reached through it is kept -- a test, or the tests
      under a conftest.py -- only when its text requests one of them or it
      walks every route the app mounts (_ENUMERATES_ROUTES);
    * a conftest.py is imported by no test, yet runs for every test under its
      directory: a changed or reached one reaches those tests (a reached one
      under the same terms).

    Measured on main at 0409b8d (2026-11-03): one swarm-api route reached
    1,428 of 1,707 test files before these rules."""
    files = [*_python_files(root, "apps"), *_python_files(root, "scripts"), *_python_files(root, "tests/unit")]
    rels = [str(p.relative_to(root)) for p in files]
    text = {rel: (root / rel).read_text(errors="replace") for rel in rels}
    names: dict[str, frozenset[str]] = {}

    def used(rel: str) -> frozenset[str]:
        if rel not in names:
            names[rel] = identifiers(text.get(rel) or (root / rel).read_text(errors="replace"))
        return names[rel]

    importers: dict[str, dict[str, frozenset[str] | None]] = {}
    for rel in rels:
        for name, owners in import_edges(rel, text[rel]).items():
            importers.setdefault(name, {})[rel] = owners
    state: dict[str, tuple[str, frozenset[str]] | None] = dict.fromkeys(changed_py)
    reached: dict[str, tuple[str, frozenset[str]] | None] = {}

    def reach(rel: str, now: tuple[str, frozenset[str]] | None) -> bool:
        """Record `now` for rel; True when that reaches more of it than before."""
        before = reached.get(rel, ("", frozenset())) if rel in reached else False
        merged = now if before is False else _meet(before, now)
        if before is not False and before == merged:
            return False
        reached[rel] = merged
        return True

    todo = list(changed_py)
    while todo:
        path = todo.pop()
        through = state[path]
        src = text.get(path) or ""
        urls = route_urls(src) if through is None else []
        for suffix in _suffixes(root, path):
            for rel, owners in importers.get(suffix, {}).items():
                if through is not None and through[0] == CALL and not through[1] & used(rel):
                    continue
                if through is not None and through[0] == URL:
                    now = through
                elif urls and owners is None and _mounts(text[rel], path):
                    now = (URL, frozenset(url_pattern(u) for u in urls))
                else:
                    now = None if owners is None else (CALL, owners)
                if not reach(rel, now):
                    continue
                if rel in changed_py:
                    continue
                state[rel] = reached[rel]
                todo.append(rel)
    # The tests under each changed or reached conftest.py.
    tests = [rel for rel in rels if rel.startswith("tests/unit/") and is_test(rel)]
    confs = {r: reached[r] for r in reached if r.rsplit("/", 1)[-1] == "conftest.py"}
    confs.update((c, None) for c in changed_py if c.rsplit("/", 1)[-1] == "conftest.py")
    for conf, how in confs.items():
        under = conf[: -len("conftest.py")]
        for test in tests:
            if test.startswith(under):
                reach(test, how if how is not None and how[0] == URL else None)
    out: set[str] = set()
    for rel, how in reached.items():
        if how is not None and how[0] == URL and is_test(rel):
            body = text.get(rel) or ""
            if not _ENUMERATES_ROUTES.search(body) and not any(re.search(p, body) for p in how[1]):
                continue
        out.add(rel)
    return out


def identifiers(text: str) -> frozenset[str]:
    """The names a module's code uses -- variables, attributes, the names it
    imports -- not its strings, comments or docstrings: forge.py's docstring
    saying `create_app()` builds no client calls no create_app. Text that does
    not parse counts every word in it."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return frozenset(re.findall(r"\w+", text))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.Attribute):
            out.add(node.attr)
        elif isinstance(node, ast.alias):
            out.add((node.asname or node.name).split(".")[0])
            out.add(node.name.rsplit(".", 1)[-1])
    return frozenset(out)


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


def doc_closure(root: Path, changed: list[str]) -> list[str]:
    """`changed` plus every docs/**/*.md whose text cites a changed non-doc
    path by its full repo-relative path (#2299). Only the full path: a basename
    -- App.tsx, main.py -- is shared by many files. One level deep, and a
    changed doc cites nothing in: 55 docs cite CLAUDE.md, and following them
    would make a docs-only change select hundreds of files."""
    out = list(dict.fromkeys(changed))
    cited = [c for c in out if not c.startswith("docs/") and not c.endswith(".md")]
    base = root / "docs"
    if not cited or not base.is_dir():
        return out
    for p in sorted(base.rglob("*.md")):
        rel = str(p.relative_to(root))
        if rel in out or SKIP_PARTS.intersection(p.relative_to(root).parts) or not p.is_file():
            continue
        text = p.read_text(errors="replace")
        if any(c in text for c in cited):
            out.append(rel)
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
    # Text: a test that names a changed path, a script that names one, or a
    # doc that cites one by its full path.
    virtual = script_closure(root, doc_closure(root, changed))
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
