#!/usr/bin/env python3
"""List every test that reads docs prose, CI YAML, a root *.md or the Makefile
(part of issue 71).

    check_test_reads.py [--root DIR] [--list-files] [FILE ...]

WHY. A unit test that opens docs/ci.md, a workflow under .github/, CLAUDE.md
or the Makefile turns red on an edit to a tree it does not test: a docs pull
request breaks the unit tests of code nobody touched, and a code pull request
inherits a red it did not cause. Issue 71 deletes those pins; this scanner is
its guard: it holds the whole tree with no baseline, so a read anywhere
under tests/ or in a JS/TS test is red (the rule:
docs/ci.md#a-test-reads-only-its-own-tree).

AN OUT-OF-TREE READ is an expression in a .py file under tests/ (test
modules, conftest.py, helper modules) that builds a path of THIS repository,
anchored on the repository root, to

  * anything under docs/ but a .json data file (prose: .md, .html, .txt,
    images; the docs/ directory itself, listed or globbed);
  * anything under .github/;
  * a root-level *.md (CLAUDE.md, CONTRACT.md, README.md, SECURITY.md, ...);
  * the Makefile.

The root is an expression that resolves from `__file__`: Path(__file__) (or
os.path.abspath/dirname of it), .resolve(), .absolute(), .parent,
.parents[n]; the depth is worked out from the file's own place, so parents[3]
is the root in tests/unit/scripts/ and parents[2] is tests/ there. Paths are
built with `/`, Path(X, ...), X.joinpath(...), os.path.join(X, ...),
X.glob/rglob(pattern) and an f-string that starts with X. A name is followed
through simple assignments at module, class and function level (DOCS = ROOT /
"docs"; DOCS / "ci.md"), through a string constant (CI = "docs/ci.md"; ROOT /
CI), through `from helper import NAME` and `import helper` of a module under
tests/ (a sibling such as tests/unit/scripts/ci_run_blocks.py, found
beside the file or in a directory above it, or by its dotted path), and
through a function that takes a relative path and joins it to a path (def
read(rel): return (ROOT / rel).read_text(); read("docs/ci.md"), or one that
hands its parameter on to such a function), and through a call of a function
whose own body reads (def changes_job(): ... APPLICATION.read_text()). A
loop, comprehension or @pytest.mark.parametrize over a tuple or list of
constants is read once per constant; `.name` and `.stem` of a known path are
its strings, and a path used only for its name (DOC.name in a message) reads
nothing.

The finding is reported where the path is used, not where a name is assigned
it: `CI = ROOT / "docs/ci.md"` alone reads nothing, `CI.read_text()` does.

IT IS A LOWER BOUND. It does not see: a root from `git rev-parse
--show-toplevel`, Path.cwd() or a relative "docs/x.md" opened from the
working directory; a root handed in by a pytest fixture or a parameter; a path
held in a container (dict, list) and read later; a path built in a loop over
names it cannot resolve; an attribute of an object (self.root); a helper
module outside tests/; a subprocess (`grep`, `git show`) that reads the file;
a read through a function of another module whose body builds the path. A
clean run means no read it can see, not no read at all.

A JS/TS TEST (a .js/.mjs/.cjs/.jsx/.ts/.mts/.cts/.tsx file under a tests/,
test/ or __tests__/ directory, or named *.test.* or *.spec.*, anywhere but
node_modules: apps/swarm-ui's vitest and node:test files, tests/browser)
is held to the same trees. Its paths are evaluated from __dirname,
import.meta.dirname/url, dirname(fileURLToPath(...)), new URL(rel, ...),
join/resolve with string parts, a `${NAME}/...` template and a name bound by
const/let/var to one of those, and a path is reported where it is built
(JsScan, below, says what it does not follow).

NOT A READ: a file the test writes under tmp_path; "docs/x.md" as data; a
read of apps/, scripts/, terraform/, kubernetes/, plugin/, install/ or
tests/; running `make` as a subprocess from the root.

  --list-files        print the distinct offending files instead of the reads.

  exit 0  no read
  exit 1  a read, each printed as `path:line: text`
  exit 2  usage error
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath


# ---------------------------------------------------------------------------
# Values. A Rel is a path relative to the repository root: its known parts
# and whether unknown parts follow (`open`). A Str is a string constant. A
# Param is a function's parameter while its body is read. A Joiner is a
# function that joins one of its parameters onto a path. A Module is an
# imported helper module's names. Anything else is None.


class Rel:
    __slots__ = ("parts", "open", "param")

    def __init__(self, parts: tuple[str, ...], open_: bool = False, param: str | None = None):
        self.parts = parts
        self.open = open_
        self.param = param


class Str:
    __slots__ = ("value",)

    def __init__(self, value: str):
        self.value = value


class Param:
    __slots__ = ("name",)

    def __init__(self, name: str):
        self.name = name


class Joiner:
    __slots__ = ("params", "param", "base")

    def __init__(self, params: list[str], param: str, base: tuple[str, ...]):
        self.params = params
        self.param = param
        self.base = base


class Items:
    """A tuple, list or set of constants (strings, paths or tuples of them)."""
    __slots__ = ("items",)

    def __init__(self, items: list):
        self.items = items


class Reader:
    """A function whose body reads one of the trees (head: what it reads)."""
    __slots__ = ("head",)

    def __init__(self, head: str):
        self.head = head


class Module:
    __slots__ = ("env",)

    def __init__(self, env: dict):
        self.env = env


class FileRel(Rel):
    """A path derived from __file__ that is not yet known to be a directory
    above the repository root (parents beyond the root are None)."""


def split(text: str) -> tuple[tuple[str, ...], bool]:
    """Parts of a relative path string, and whether it is unusable (absolute,
    or climbs with ..)."""
    if text.startswith("/"):
        return (), True
    parts = []
    for part in PurePosixPath(text).parts:
        if part == "..":
            return (), True
        if part not in (".", ""):
            parts.append(part)
    return tuple(parts), False


def in_scope(value) -> bool:
    if not isinstance(value, Rel):
        return False
    parts = value.parts
    if not parts:
        return False
    head = parts[0]
    if head == ".github":
        return True
    if head == "docs":
        if len(parts) == 1 or value.open:
            return True
        return not parts[-1].endswith(".json")
    if len(parts) == 1 and not value.open:
        return head == "Makefile" or head.endswith(".md")
    return False


def append(base, extra) -> Rel | None:
    """base / extra, extra a Str, a Param, an unresolved value or a glob."""
    if not isinstance(base, Rel):
        return None
    if base.open:
        return Rel(base.parts, True, base.param)
    if isinstance(extra, Str):
        parts, bad = split(extra.value)
        if bad:
            return None
        return Rel(base.parts + parts)
    if isinstance(extra, Param):
        return Rel(base.parts, True, extra.name)
    return Rel(base.parts, True)


# ---------------------------------------------------------------------------

PATH_CTORS = {"Path", "PurePath", "PosixPath", "PurePosixPath", "pathlib.Path",
              "pathlib.PurePath", "pathlib.PosixPath", "pathlib.PurePosixPath"}
SAME_VALUE_CALLS = {"str", "os.fspath", "os.path.abspath", "os.path.realpath",
                    "os.path.normpath", "fspath", "abspath", "realpath", "normpath"}
DIRNAME_CALLS = {"os.path.dirname", "dirname"}
JOIN_CALLS = {"os.path.join", "join", "posixpath.join"}
SAME_VALUE_METHODS = {"resolve", "absolute", "expanduser"}
GLOB_METHODS = {"glob", "rglob"}
# A loop, comprehension or parametrize over more constants than this is read
# once, its variable unknown: it bounds the work on a long table of data.
MAX_ITEMS = 64
# A path's name, stem or suffix, or its place relative to another, reads
# nothing (a message that names the doc it checked).
NAME_ONLY = frozenset({"name", "stem", "suffix", "suffixes", "relative_to", "parts"})
# The expressions that can hold a path; the rest are only walked through.
PATH_NODES = (ast.Name, ast.Attribute, ast.Subscript, ast.BinOp, ast.Call, ast.JoinedStr)
# A file whose text holds no string that starts with a tree's name or ends in
# .md, and that imports no helper under tests/ that does, builds no path to
# the trees: it is not parsed.
TOKENS = re.compile(r"""["'/](?:docs|\.github|Makefile)\b|\.md["']""")
ANCHOR = re.compile(r"__file__")
IMPORT_LINE = re.compile(r"^[ \t]*(?:from[ \t]+([\w.]+)[ \t]+import[ \t]+\(?([^)\n]*)|import[ \t]+([\w.]+))",
                         re.M)


def dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        inner = dotted(node.value)
        return f"{inner}.{node.attr}" if inner else None
    return None


class Scanner:
    def __init__(self, root: Path):
        self.root = root
        self.modules: dict[str, dict] = {}
        self.busy: set[str] = set()
        self.found_modules: dict = {}
        self.heads: dict[str, frozenset[str]] = {}
        self.tainted: dict[tuple, bool] = {}

    def may_read(self, rel: str, text: str) -> bool:
        """Whether a file can hold a read at all: it (or a helper module
        under tests/ it imports, transitively) reaches `__file__`, and it or
        such a helper names one of the trees. Most test files do not, and are
        not parsed: it keeps the whole-tree run in a guard's budget."""
        return self.reaches(rel, text, ANCHOR) and self.reaches(rel, text, TOKENS)

    def reaches(self, rel: str, text: str, tokens: re.Pattern) -> bool:
        key = (rel, tokens.pattern)
        if key in self.tainted:
            return self.tainted[key]
        self.tainted[key] = False
        result = bool(tokens.search(text))
        if not result:
            for found in self.helpers(rel, text):
                try:
                    other = (self.root / found).read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                if self.reaches(found, other, tokens):
                    result = True
                    break
        self.tainted[key] = result
        return result

    def helpers(self, rel: str, text: str) -> list[str]:
        """The modules under tests/ a file imports, read from its import lines."""
        found: list[str] = []
        for match in IMPORT_LINE.finditer(text):
            module = match.group(1) or match.group(3)
            level = len(module) - len(module.lstrip("."))
            names = [module.lstrip(".")]
            if match.group(2):
                names += [f"{names[0]}.{n.strip().split()[0]}".lstrip(".")
                          for n in match.group(2).split(",") if n.strip().split()]
            for name in names:
                if not name and not level:
                    continue
                module_rel = self.find_module(rel, name, level)
                if module_rel and module_rel != rel:
                    found.append(module_rel)
        return found

    # -- module resolution ------------------------------------------------
    def find_module(self, rel: str, name: str, level: int) -> str | None:
        key = (str(PurePosixPath(rel).parent), name, level)
        if key not in self.found_modules:
            self.found_modules[key] = self._find_module(rel, name, level)
        return self.found_modules[key]

    def _find_module(self, rel: str, name: str, level: int) -> str | None:
        here = PurePosixPath(rel).parent
        dotted_path = PurePosixPath(*name.split(".")) if name else None
        candidates: list[PurePosixPath] = []
        if level:
            base = here
            for _ in range(level - 1):
                base = base.parent
            candidates.append(base / dotted_path if dotted_path else base)
        elif dotted_path:
            directory = here
            while True:
                candidates.append(directory / dotted_path)
                if str(directory) in (".", ""):
                    break
                directory = directory.parent
        for cand in candidates:
            for file in (f"{cand}.py", f"{cand}/__init__.py"):
                if file.startswith("tests/") and (self.root / file).is_file():
                    return file
        return None

    def module_env(self, rel: str) -> dict:
        if rel in self.modules:
            return self.modules[rel]
        try:
            text = (self.root / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            text = ""
        if not self.reaches(rel, text, ANCHOR):
            # A helper that never reaches __file__ holds no path of the tree.
            self.modules[rel] = {}
            return {}
        if rel in self.busy:
            return {}
        self.busy.add(rel)
        try:
            tree = ast.parse((self.root / rel).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, SyntaxError, ValueError):
            tree = None
        env: dict = {}
        if tree is not None:
            FileScan(self, rel, tree, report=False).run(env)
        self.busy.discard(rel)
        self.modules[rel] = env
        return env

    # -- a file -------------------------------------------------------------
    def scan(self, rel: str) -> list[tuple[int, str]]:
        try:
            text = (self.root / rel).read_text(encoding="utf-8")
            if not self.may_read(rel, text):
                return []
            tree = ast.parse(text)
        except (OSError, UnicodeDecodeError, SyntaxError, ValueError):
            return []
        scan = FileScan(self, rel, tree, report=True)
        env: dict = {}
        scan.run(env)
        self.heads[rel] = frozenset(scan.heads)
        self.modules.setdefault(rel, env)
        lines = text.splitlines()
        return sorted({(n, lines[n - 1].strip() if n <= len(lines) else "") for n in scan.found})


class FileScan:
    def __init__(self, scanner: Scanner, rel: str, tree: ast.Module, report: bool):
        self.scanner = scanner
        self.rel = rel
        self.tree = tree
        self.report = report
        self.found: set[int] = set()
        # The first part of each path found: docs, .github, a root file.
        self.heads: set[str] = set()
        # The head of every finding in order, so a function knows its own.
        self.reads: list[str] = []
        # Built on the first finding: most files parsed have none.
        self._parents: dict[ast.AST, ast.AST] | None = None
        # Expressions whose value is assigned to a single name: reported where used.
        self.assigned: set[ast.AST] = set()
        self.joiner_hits: list[tuple[str, tuple[str, ...]]] = []
        # Values of expressions, per environment, while one statement is read.
        self.cache: dict = {}
        self.alive: list[dict] = []

    @property
    def parents(self) -> dict[ast.AST, ast.AST]:
        if self._parents is None:
            self._parents = {}
            for node in ast.walk(self.tree):
                for child in ast.iter_child_nodes(node):
                    self._parents[child] = node
        return self._parents

    def run(self, env: dict) -> None:
        self.block(self.tree.body, env)

    # -- statements ---------------------------------------------------------
    def block(self, body: list[ast.stmt], env: dict) -> None:
        for stmt in body:
            self.stmt(stmt, env)

    def stmt(self, stmt: ast.stmt, env: dict) -> None:
        self.cache.clear()
        self.alive.clear()
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            self.imports(stmt, env)
            return
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for deco in stmt.decorator_list:
                self.expressions(deco, env)
            args = stmt.args
            for default in args.defaults + [d for d in args.kw_defaults if d is not None]:
                self.expressions(default, env)
            params = [a.arg for a in args.posonlyargs + args.args + args.kwonlyargs]
            local = dict(env)
            for name in params:
                local[name] = Param(name)
            before = len(self.joiner_hits)
            reads_before = len(self.reads)
            self.block(stmt.body, local)
            for case in self.parametrized(stmt, env):
                self.block(stmt.body, {**local, **case})
            hits = self.joiner_hits[before:]
            del self.joiner_hits[before:]
            joiner = None
            for entry in hits:
                param, base = entry
                if param in params:
                    joiner = Joiner(params, param, base)
                    break
            if joiner is None and len(self.reads) > reads_before:
                # A function whose body reads: calling it reads too.
                joiner = Reader(self.reads[reads_before])
            env[stmt.name] = joiner
            return
        if isinstance(stmt, ast.ClassDef):
            for deco in stmt.decorator_list:
                self.expressions(deco, env)
            self.block(stmt.body, dict(env))
            env[stmt.name] = None
            return
        if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            value = stmt.value
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            if value is None:
                return
            if len(targets) == 1 and isinstance(targets[0], ast.Name):
                self.assigned.add(value)
            self.expressions(value, env)
            resolved = self.eval(value, env)
            for target in targets:
                self.bind(target, resolved, env)
            return
        if isinstance(stmt, (ast.For, ast.AsyncFor)):
            self.expressions(stmt.iter, env)
            items = self.constant_items(stmt.iter, env)
            if isinstance(stmt.target, ast.Name):
                env[stmt.target.id] = items[0] if len(items) == 1 else None
                if len(items) > 1:
                    # Read the body once per constant: each value is a path.
                    for item in items:
                        local = dict(env)
                        local[stmt.target.id] = item
                        self.block(stmt.body, local)
                    self.block(stmt.orelse, env)
                    return
            else:
                self.bind(stmt.target, None, env)
            self.block(stmt.body, env)
            self.block(stmt.orelse, env)
            return
        if isinstance(stmt, (ast.With, ast.AsyncWith)):
            for item in stmt.items:
                self.expressions(item.context_expr, env)
                if item.optional_vars is not None:
                    self.bind(item.optional_vars, None, env)
            self.block(stmt.body, env)
            return
        if isinstance(stmt, ast.Try) or type(stmt).__name__ == "TryStar":
            self.block(stmt.body, env)
            for handler in stmt.handlers:
                if handler.name:
                    env[handler.name] = None
                self.block(handler.body, env)
            self.block(stmt.orelse, env)
            self.block(stmt.finalbody, env)
            return
        if isinstance(stmt, (ast.If, ast.While)):
            self.expressions(stmt.test, env)
            self.block(stmt.body, env)
            self.block(stmt.orelse, env)
            return
        if type(stmt).__name__ == "Match":
            self.expressions(stmt.subject, env)
            for case in stmt.cases:
                self.block(case.body, env)
            return
        for child in ast.iter_child_nodes(stmt):
            if isinstance(child, ast.expr):
                self.expressions(child, env)
            elif isinstance(child, ast.stmt):
                self.stmt(child, env)
        if isinstance(stmt, ast.AugAssign) and isinstance(stmt.target, ast.Name):
            env[stmt.target.id] = None

    def bind(self, target: ast.AST, value, env: dict) -> None:
        if isinstance(target, ast.Name):
            env[target.id] = value
        elif isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                self.bind(elt, None, env)
        elif isinstance(target, ast.Starred):
            self.bind(target.value, None, env)

    def imports(self, stmt: ast.stmt, env: dict) -> None:
        if isinstance(stmt, ast.Import):
            for alias in stmt.names:
                found = self.scanner.find_module(self.rel, alias.name, 0)
                if alias.asname:
                    env[alias.asname] = Module(self.scanner.module_env(found)) if found else None
                else:
                    head = alias.name.split(".")[0]
                    if found and "." not in alias.name:
                        env[head] = Module(self.scanner.module_env(found))
                    else:
                        env.setdefault(head, None)
            return
        assert isinstance(stmt, ast.ImportFrom)
        found = self.scanner.find_module(self.rel, stmt.module or "", stmt.level)
        source = self.scanner.module_env(found) if found else {}
        for alias in stmt.names:
            if alias.name == "*":
                env.update(source)
                continue
            name = alias.asname or alias.name
            if alias.name in source:
                env[name] = source[alias.name]
                continue
            sub = None
            if found:
                prefix = (stmt.module + ".") if stmt.module else ""
                sub = self.scanner.find_module(self.rel, prefix + alias.name, stmt.level)
            env[name] = Module(self.scanner.module_env(sub)) if sub else None

    def constant_items(self, node: ast.AST, env: dict) -> list:
        value = self.eval(node, env)
        if isinstance(value, Items) and len(value.items) <= MAX_ITEMS:
            return value.items
        return []

    def parametrized(self, stmt: ast.FunctionDef | ast.AsyncFunctionDef, env: dict) -> list[dict]:
        """One binding of the parameters per case of each
        @pytest.mark.parametrize whose names and constant values resolve."""
        cases: list[dict] = [{}]
        for deco in stmt.decorator_list:
            if not (isinstance(deco, ast.Call) and (dotted(deco.func) or "").endswith("parametrize")
                    and len(deco.args) >= 2):
                continue
            names = self.eval(deco.args[0], env)
            if isinstance(names, Str):
                names_list = [n.strip() for n in names.value.split(",") if n.strip()]
            elif isinstance(names, Items) and all(isinstance(n, Str) for n in names.items):
                names_list = [n.value for n in names.items]
            else:
                continue
            bindings: list[dict] = []
            for item in self.constant_items(deco.args[1], env):
                if len(names_list) == 1:
                    bindings.append({names_list[0]: item})
                elif isinstance(item, Items) and len(item.items) == len(names_list):
                    bindings.append(dict(zip(names_list, item.items)))
            if bindings and len(cases) * len(bindings) <= MAX_ITEMS:
                cases = [{**case, **b} for case in cases for b in bindings]
        return cases if cases != [{}] else []

    # -- expressions --------------------------------------------------------
    def expressions(self, node: ast.AST, env: dict) -> None:
        """Report every maximal in-scope path expression under node."""
        self.cache.clear()
        self.alive.clear()
        self.visit(node, env)

    def visit(self, node: ast.AST, env: dict) -> None:
        if isinstance(node, ast.Lambda):
            local = dict(env)
            for arg in node.args.posonlyargs + node.args.args + node.args.kwonlyargs:
                local[arg.arg] = None
            self.alive.append(local)
            self.visit(node.body, local)
            return
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            self.comprehension(node, env)
            return
        if isinstance(node, PATH_NODES):
            self.check(node, env)
        for child in ast.iter_child_nodes(node):
            self.visit(child, env)

    def comprehension(self, node, env: dict) -> None:
        local = dict(env)
        cases: list[dict] = []
        for number, gen in enumerate(node.generators):
            self.visit(gen.iter, local)
            items = self.constant_items(gen.iter, local) if number == 0 else []
            if items and isinstance(gen.target, ast.Name):
                cases = [{gen.target.id: item} for item in items]
            self.bind(gen.target, None, local)
        parts = [node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]
        parts += [cond for gen in node.generators for cond in gen.ifs]
        for case in [{}] + cases:
            scope = {**local, **case}
            # Held while the cache is: a freed dict's id could name the next.
            self.alive.append(scope)
            for part in parts:
                self.visit(part, scope)

    def check(self, node: ast.expr, env: dict) -> None:
        value = self.eval(node, env)
        if isinstance(value, Rel) and value.param is not None:
            self.joiner_hits.append((value.param, value.parts))
        if node in self.assigned:
            return
        if in_scope(value) and not self.absorbed(node, env):
            parent = self.parents.get(node)
            if isinstance(parent, ast.Attribute) and parent.attr in NAME_ONLY:
                return
            self.found.add(node.lineno)
            self.heads.add(value.parts[0])
            self.reads.append(value.parts[0])

    def absorbed(self, node: ast.AST, env: dict) -> bool:
        """Whether an enclosing expression carries this path further: only
        the outermost path expression is reported."""
        child = node
        parent = self.parents.get(child)
        while isinstance(parent, (ast.expr, ast.keyword)):
            # A path carried into a longer path is judged there (ROOT / "docs"
            # inside ROOT / "docs" / "data.json" reads no prose).
            if isinstance(parent, ast.expr) and isinstance(self.eval(parent, env), Rel):
                return True
            if isinstance(parent, ast.Call) and child is not parent.func:
                # An argument of a call that is no path: the read is here.
                return False
            child, parent = parent, self.parents.get(parent)
        return False

    def eval(self, node: ast.AST, env: dict):
        key = (node, id(env))
        if key in self.cache:
            return self.cache[key]
        try:
            value = self._eval(node, env)
        except RecursionError:
            value = None
        self.cache[key] = value
        return value

    def _eval(self, node: ast.AST, env: dict):
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            items = [self.eval(elt, env) for elt in node.elts]
            if items and all(isinstance(i, (Str, Rel, Items)) for i in items):
                return Items(items)
            return None
        if isinstance(node, ast.Constant):
            return Str(node.value) if isinstance(node.value, str) else None
        if isinstance(node, ast.Name):
            if node.id == "__file__":
                return FileRel(tuple(PurePosixPath(self.rel).parts))
            return env.get(node.id)
        if isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and isinstance(env.get(node.value.id), Module):
                return env[node.value.id].env.get(node.attr)
            if node.attr == "parent":
                return self.up(self.eval(node.value, env), 1)
            if node.attr in ("name", "stem"):
                value = self.eval(node.value, env)
                if isinstance(value, Rel) and value.parts and not value.open:
                    name = value.parts[-1]
                    return Str(name if node.attr == "name" or "." not in name else name.rsplit(".", 1)[0])
            return None
        if isinstance(node, ast.Subscript):
            if isinstance(node.value, ast.Attribute) and node.value.attr == "parents":
                index = node.slice
                if isinstance(index, ast.Constant) and isinstance(index.value, int):
                    return self.up(self.eval(node.value.value, env), index.value + 1)
            return None
        if isinstance(node, ast.BinOp):
            if isinstance(node.op, ast.Div):
                return self.join(self.eval(node.left, env), self.eval(node.right, env))
            if isinstance(node.op, ast.Add):
                left = self.eval(node.left, env)
                right = self.eval(node.right, env)
                if isinstance(left, Str) and isinstance(right, Str):
                    return Str(left.value + right.value)
                if isinstance(left, Items) and isinstance(right, Items):
                    return Items(left.items + right.items)
                if isinstance(left, Rel) and isinstance(right, Str) and right.value.startswith(("/", "\\")):
                    return self.join(left, Str(right.value.lstrip("/")))
            return None
        if isinstance(node, ast.JoinedStr):
            return self.fstring(node, env)
        if isinstance(node, ast.Call):
            return self.call(node, env)
        return None

    def up(self, value, levels: int):
        if not isinstance(value, Rel) or value.open:
            return None
        if levels > len(value.parts):
            return None
        parts = value.parts[: len(value.parts) - levels]
        return Rel(parts)

    def join(self, base, extra):
        if isinstance(base, Rel):
            if isinstance(extra, Rel):
                return None
            return append(base, extra)
        return None

    def fstring(self, node: ast.JoinedStr, env: dict):
        values = node.values
        if all(isinstance(v, ast.Constant) for v in values):
            return Str("".join(str(v.value) for v in values))
        first = values[0]
        if not isinstance(first, ast.FormattedValue) or first.format_spec is not None:
            return None
        base = self.eval(first.value, env)
        if not isinstance(base, Rel):
            return None
        text = ""
        complete = True
        for value in values[1:]:
            if not isinstance(value, ast.Constant):
                complete = False
                break
            text += str(value.value)
        if not text.startswith("/"):
            return None
        text = text.lstrip("/")
        if complete:
            return append(base, Str(text))
        # The last part is cut by a placeholder: keep only the whole parts.
        head = append(base, Str(text.rsplit("/", 1)[0])) if "/" in text else base
        return Rel(head.parts, True) if isinstance(head, Rel) else None

    def call(self, node: ast.Call, env: dict):
        name = dotted(node.func)
        args = [self.eval(a, env) for a in node.args]
        if name in PATH_CTORS or name in JOIN_CALLS:
            if not args:
                return None
            value = args[0]
            if isinstance(value, Str):
                return None
            for extra in args[1:]:
                value = self.join(value, extra)
            return value
        if name in SAME_VALUE_CALLS and len(args) == 1:
            return args[0] if isinstance(args[0], Rel) else None
        if name in DIRNAME_CALLS and len(args) == 1:
            return self.up(args[0], 1)
        if isinstance(node.func, ast.Attribute):
            method = node.func.attr
            if method in SAME_VALUE_METHODS:
                return self.eval(node.func.value, env)
            if method == "joinpath":
                value = self.eval(node.func.value, env)
                for extra in args:
                    value = self.join(value, extra)
                return value
            if method in GLOB_METHODS and len(args) == 1:
                base = self.eval(node.func.value, env)
                if isinstance(base, Rel) and isinstance(args[0], Str):
                    parts, bad = split(args[0].value)
                    if bad or not parts:
                        return None
                    if base.open:
                        return None
                    # A wildcard in the last part only names files of a known
                    # directory (docs/*.json, *.md at the root); one earlier
                    # leaves the rest unknown.
                    literal = []
                    for part in parts[:-1]:
                        if any(c in part for c in "*?["):
                            return Rel(base.parts + tuple(literal), True)
                        literal.append(part)
                    return Rel(base.parts + parts)
                return None
            if method == "with_name" and args and isinstance(args[0], Str):
                value = self.up(self.eval(node.func.value, env), 1)
                return append(value, args[0]) if value is not None else None
        target = self.eval(node.func, env)
        if isinstance(target, Reader):
            return Rel((target.head,), target.head in ("docs", ".github"))
        if isinstance(target, Joiner):
            arg = None
            index = target.params.index(target.param)
            if index < len(node.args):
                arg = args[index]
            for kw in node.keywords:
                if kw.arg == target.param:
                    arg = self.eval(kw.value, env)
            if isinstance(arg, Str):
                return append(Rel(target.base), arg)
            if isinstance(arg, Param):
                # A helper that hands its own parameter on is a joiner too.
                return Rel(target.base, True, arg.name)
            if isinstance(arg, Rel) and not arg.parts:
                return None
        return None


# ---------------------------------------------------------------------------
# JavaScript and TypeScript tests. The same rule holds a console test
# (apps/swarm-ui's vitest and node:test files, tests/**/*.mjs): it may not
# build a path of this repository to docs prose, .github/, a root *.md or the
# Makefile. A JS/TS file is tokenized (strings, templates, regex literals and
# comments told apart) and its path expressions are evaluated from the file's
# own place: __dirname, import.meta.dirname, import.meta.url (and
# dirname(fileURLToPath(import.meta.url)), new URL(rel, import.meta.url)),
# join/resolve (bare, path. or posix.) of those with string parts, a template
# that starts with ${NAME}, and a name bound once by const/let/var to such an
# expression. A JS/TS path is reported where it is BUILT, not where it is read:
# a test that names a doc's path in a message spells it as a string instead.
# It is a lower bound too: an imported name, process.cwd() or a path handed
# in by a fixture is not followed.

JS_SUFFIXES = (".js", ".mjs", ".cjs", ".jsx", ".ts", ".mts", ".cts", ".tsx")
JS_TEST_DIRS = frozenset({"tests", "test", "__tests__"})
JS_PATH_CALLS = {"join", "resolve", "path.join", "path.resolve", "posix.join", "posix.resolve",
                 "path.posix.join", "path.posix.resolve", "pathJoin", "pathResolve"}
JS_DIRNAME_CALLS = {"dirname", "path.dirname", "posix.dirname", "path.posix.dirname"}
JS_SAME_CALLS = {"fileURLToPath", "url.fileURLToPath", "normalize", "path.normalize", "String"}
JS_SAME_ATTRS = {"pathname", "href"}
# A '/' after one of these starts a regex literal, not a division.
JS_REGEX_AFTER_WORDS = frozenset({"return", "typeof", "case", "do", "else", "in", "of", "new",
                                  "delete", "void", "throw", "instanceof", "yield", "await"})
# A JS/TS file that names none of the trees and no root anchor is not tokenized.
JS_TOKENS = re.compile(r"""docs|\.github|Makefile|\.md\b""")
JS_ANCHOR = re.compile(r"__dirname|__filename|import\.meta")
JS_ID_START = re.compile(r"[A-Za-z_$]")
JS_ID = re.compile(r"[A-Za-z0-9_$]*")


def is_js_test(rel: str) -> bool:
    """A JS/TS test file: under a tests/, test/ or __tests__/ directory, or
    named *.test.* / *.spec.*, and not under node_modules."""
    if not rel.endswith(JS_SUFFIXES):
        return False
    parts = PurePosixPath(rel).parts
    if "node_modules" in parts:
        return False
    name = parts[-1]
    return bool(JS_TEST_DIRS.intersection(parts[:-1])) or ".test." in name or ".spec." in name


class JsPath:
    """A path of the repository: its parts from the root, whether unknown
    parts follow (`open`) and whether it names a directory (`dir`, for a URL
    that a further `new URL(rel, it)` resolves against)."""
    __slots__ = ("parts", "open", "dir")

    def __init__(self, parts: tuple[str, ...], open_: bool = False, dir_: bool = True):
        self.parts = parts
        self.open = open_
        self.dir = dir_


def js_tokens(text: str) -> list[tuple[str, str, int]]:
    """(kind, value, line) for each token: id, str (its text, escapes kept),
    tpl (its raw text between the backticks), num, re and p (punctuation)."""
    out: list[tuple[str, str, int]] = []
    i, n, line = 0, len(text), 1

    def regex_ok() -> bool:
        if not out:
            return True
        kind, value, _ = out[-1]
        if kind in ("str", "tpl", "num", "re"):
            return False
        if kind == "id":
            return value in JS_REGEX_AFTER_WORDS
        return value not in (")", "]", "}")

    while i < n:
        c = text[i]
        if c == "\n":
            line += 1
            i += 1
        elif c in " \t\r\f\v":
            i += 1
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end < 0 else end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            end = n if end < 0 else end + 2
            line += text.count("\n", i, end)
            i = end
        elif c in "'\"":
            start, j = line, i + 1
            while j < n and text[j] != c and text[j] != "\n":
                j += 2 if text[j] == "\\" else 1
            out.append(("str", text[i + 1:j], start))
            i = j + 1
        elif c == "`":
            start, j, depth = line, i + 1, 0
            while j < n:
                ch = text[j]
                if ch == "\\":
                    j += 2
                    continue
                if depth == 0 and ch == "`":
                    break
                if text.startswith("${", j):
                    depth += 1
                    j += 2
                    continue
                if depth and ch == "}":
                    depth -= 1
                elif depth and ch == "{":
                    depth += 1
                j += 1
            raw = text[i + 1:j]
            line += raw.count("\n")
            out.append(("tpl", raw, start))
            i = j + 1
        elif c == "/" and regex_ok():
            j, in_class = i + 1, False
            while j < n and text[j] != "\n":
                ch = text[j]
                if ch == "\\":
                    j += 2
                    continue
                if ch == "[":
                    in_class = True
                elif ch == "]":
                    in_class = False
                elif ch == "/" and not in_class:
                    break
                j += 1
            j += 1
            while j < n and text[j].isalpha():
                j += 1
            out.append(("re", text[i:j], line))
            i = j
        elif JS_ID_START.match(c):
            m = JS_ID.match(text, i + 1)
            end = m.end() if m else i + 1
            out.append(("id", text[i:end], line))
            i = end
        elif c.isdigit():
            j = i + 1
            while j < n and (text[j].isalnum() or text[j] in "._"):
                j += 1
            out.append(("num", text[i:j], line))
            i = j
        elif text.startswith("?.", i) and not text[i + 2:i + 3].isdigit():
            out.append(("p", ".", line))
            i += 2
        elif text.startswith("...", i):
            out.append(("p", "...", line))
            i += 3
        else:
            out.append(("p", c, line))
            i += 1
    return out


class JsScan:
    def __init__(self, rel: str, text: str):
        self.toks = js_tokens(text)
        here = PurePosixPath(rel)
        self.file = JsPath(tuple(here.parts), dir_=False)
        self.dir = JsPath(tuple(here.parent.parts))
        self.env: dict[str, object] = {}
        self.found: set[int] = set()

    # -- values ---------------------------------------------------------------
    @staticmethod
    def walk(base, text: str):
        """base joined with the relative path text; None above the root."""
        if not isinstance(base, JsPath) or text.startswith("/"):
            return None
        if base.open:
            return JsPath(base.parts, True)
        parts = list(base.parts)
        for part in text.split("/"):
            if part in ("", "."):
                continue
            if part == "..":
                if not parts:
                    return None
                parts.pop()
            else:
                parts.append(part)
        return JsPath(tuple(parts), dir_=text.endswith("/") or text in ("", ".", ".."))

    def dotted(self, i: int) -> tuple[str, int]:
        """The dotted name starting at token i, and the index after it."""
        names = [self.toks[i][1]]
        j = i + 1
        while (j + 1 < len(self.toks) and self.toks[j] == ("p", ".", self.toks[j][2])
               and self.toks[j + 1][0] == "id"):
            names.append(self.toks[j + 1][1])
            j += 2
        return ".".join(names), j

    def args(self, i: int) -> tuple[list[list[int]], int]:
        """Token index ranges of the call's top-level arguments, the '(' at
        i; and the index after the ')'."""
        depth, j, start, ranges = 0, i, i + 1, []
        while j < len(self.toks):
            kind, value, _ = self.toks[j]
            if kind == "p" and value in "([{":
                depth += 1
            elif kind == "p" and value in ")]}":
                depth -= 1
                if depth == 0:
                    if j > start:
                        ranges.append([start, j])
                    return ranges, j + 1
            elif kind == "p" and value == "," and depth == 1:
                ranges.append([start, j])
                start = j + 1
            j += 1
        return ranges, j

    def arg_value(self, span: list[int]):
        value, end = self.primary(span[0])
        return value if end == span[1] else None

    def template(self, raw: str):
        if "${" not in raw:
            return raw
        if not raw.startswith("${"):
            return None
        close = raw.find("}")
        name = raw[2:close].strip()
        rest = raw[close + 1:].lstrip("/")
        base = self.env.get(name) if re.fullmatch(r"[A-Za-z_$][\w$]*", name) else None
        if not isinstance(base, JsPath):
            return None
        if "${" in rest:
            tail = rest[:rest.find("${")]
            known = self.walk(base, tail.rsplit("/", 1)[0]) if "/" in tail else base
            return JsPath(known.parts, True) if isinstance(known, JsPath) else None
        return self.walk(base, rest)

    def call(self, name: str, values: list):
        if name in JS_PATH_CALLS:
            if not values or not isinstance(values[0], JsPath):
                return None
            path = values[0]
            for value in values[1:]:
                if isinstance(value, JsPath) and name.endswith("resolve"):
                    path = value
                elif isinstance(value, str):
                    path = self.walk(path, value)
                    if path is None:
                        return None
                    path = JsPath(path.parts, path.open)
                else:
                    return JsPath(path.parts, True)
            return path
        if name in JS_DIRNAME_CALLS:
            value = values[0] if values else None
            if isinstance(value, JsPath) and value.parts and not value.open:
                return JsPath(value.parts[:-1])
            return None
        if name in JS_SAME_CALLS:
            return values[0] if values else None
        return None

    def primary(self, i: int):
        """The value of the expression starting at token i, and the index
        after it (i + 1 for anything it does not know)."""
        if i >= len(self.toks):
            return None, i + 1
        kind, value, _ = self.toks[i]
        if kind == "str":
            return value, i + 1
        if kind == "tpl":
            return self.template(value), i + 1
        if kind != "id":
            return None, i + 1
        if value == "new" and i + 1 < len(self.toks) and self.toks[i + 1][1] in ("URL", "url.URL"):
            name, j = self.dotted(i + 1)
            if name == "URL" and j < len(self.toks) and self.toks[j][1] == "(":
                spans, end = self.args(j)
                values = [self.arg_value(s) for s in spans]
                result = None
                if len(values) == 2 and isinstance(values[0], str) and isinstance(values[1], JsPath):
                    base = values[1] if values[1].dir else JsPath(values[1].parts[:-1])
                    result = self.walk(base, values[0])
                return self.suffix(result, end)
        name, j = self.dotted(i)
        if j < len(self.toks) and self.toks[j][1] == "(":
            spans, end = self.args(j)
            values = [self.arg_value(s) for s in spans]
            return self.suffix(self.call(name, values), end)
        if name in ("__dirname", "import.meta.dirname"):
            return self.dir, j
        if name in ("__filename", "import.meta.url", "import.meta.filename"):
            return self.file, j
        head, _, attr = name.rpartition(".")
        if head and attr in JS_SAME_ATTRS:
            value = self.env.get(head)
            return (value if isinstance(value, JsPath) else None), j
        return self.env.get(name), j

    def suffix(self, value, j: int):
        """A trailing .pathname or .href keeps a URL's path."""
        while (j + 1 < len(self.toks) and self.toks[j][1] == "." and self.toks[j + 1][1] in JS_SAME_ATTRS):
            j += 2
        return value, j

    # -- a file ---------------------------------------------------------------
    def run(self) -> set[int]:
        toks = self.toks
        for i, (kind, value, line) in enumerate(toks):
            if kind == "id" and value in ("const", "let", "var") and i + 3 < len(toks) \
                    and toks[i + 1][0] == "id" and toks[i + 2][1] == "=":
                bound, end = self.primary(i + 3)
                follows = toks[end][1] if end < len(toks) else ";"
                if follows in (";", ")", "}", ",") or (end < len(toks) and toks[end][0] == "id"):
                    self.env[toks[i + 1][1]] = bound
                else:
                    self.env.pop(toks[i + 1][1], None)
            if kind == "tpl" or (kind == "id" and (value == "new" or (
                    i + 1 < len(toks) and toks[i + 1][1] in ("(", ".")))):
                built, _ = self.primary(i)
                if isinstance(built, JsPath) and in_scope(Rel(built.parts, built.open)):
                    self.found.add(line)
        return self.found


def js_scan(root: Path, rel: str) -> list[tuple[int, str]]:
    try:
        text = (root / rel).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    if not (JS_TOKENS.search(text) and JS_ANCHOR.search(text)):
        return []
    lines = text.splitlines()
    return sorted({(n, lines[n - 1].strip() if n <= len(lines) else "") for n in JsScan(rel, text).run()})


# ---------------------------------------------------------------------------


def wanted(rel: str) -> bool:
    """A .py file under tests/, or a JS/TS test file anywhere."""
    return (rel.startswith("tests/") and rel.endswith(".py")) or is_js_test(rel)


def test_files(root: Path) -> list[str]:
    """Tracked and untracked-but-not-ignored .py files under tests/ and JS/TS
    test files anywhere (the gate runs before the publish's `git add --all`);
    outside a git work tree, the same files found by walking the tree."""
    names: list[str] = []
    try:
        for extra in ((), ("--others", "--exclude-standard")):
            out = subprocess.run(["git", "-C", str(root), "ls-files", "-z", *extra],
                                 check=True, capture_output=True).stdout.decode()
            names.extend(p for p in out.split("\0") if p)
    except (OSError, subprocess.CalledProcessError):
        names = [p.relative_to(root).as_posix() for p in root.rglob("*")
                 if ".git" not in p.relative_to(root).parts]
    return sorted({n for n in names if wanted(n) and (root / n).is_file()})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--root", default=".", help="repository root (default: .)")
    parser.add_argument("--list-files", action="store_true", help="print the distinct offending files")
    parser.add_argument("files", nargs="*",
                        help="repo-relative .py files under tests/ or JS/TS test files (default: all)")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    files = sorted(f for f in args.files if wanted(f)) if args.files else test_files(root)
    scanner = Scanner(root)
    bad = 0
    for rel in files:
        found = scanner.scan(rel) if rel.endswith(".py") else js_scan(root, rel)
        if not found:
            continue
        bad += 1
        if args.list_files:
            print(rel)
        else:
            for line, text in found:
                print(f"{rel}:{line}: {text}")
    if bad:
        print(f"{bad} test file(s) read docs prose, CI YAML, a root *.md or the "
              "Makefile: test the code, not the prose (issue 71; scripts/check_test_reads.py)",
              file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
