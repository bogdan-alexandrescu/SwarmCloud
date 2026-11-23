"""Every request path the bridge package builds, read out of its source.

`test_against_the_real_api.py` compares these against the real API's route
table. The scan used to read four named modules with one regex, so a call
added to any other module of `apps/swarm-mcp/swarm_mcp/` -- `schedules`,
`secrets`, `keys`, `awaitinput`, ... -- was first checked on the merge
train, or never. This reads EVERY module the package directory holds, found
by walking the directory, so a module added tomorrow is read without an edit
here.

How a call is found, on the syntax tree rather than the text:

* the transport: a call to any method whose parameters open with
  `(self, method, path)` -- `SwarmClient.request` and the methods under it,
  found from the package, not named here;
* a request built by hand on the client's `base_url` with `Request(...,
  method=...)` (the multipart answer post);
* a wrapper: a function that hands its own parameters to either of the above
  (`schedules._call`), read at each of ITS call sites, wrappers of wrappers
  included.

A path is read through string constants, f-strings, `+`, conditional
expressions, `str.format`, local and module constants (also another module's,
`render.JEV_USAGE_ROUTE`), helpers that return a path (`input_path(...)`)
and a `method, path = TABLE[key]` row of a module-level table. A call none of
that can read is reported as UNREAD rather than dropped, so a call the scan
cannot see fails the test instead of passing it by silence.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

_HOLE = re.compile(r"\{[^{}]*\}")
_MARK = "\0"  # stands for a parameter the enclosing function was handed


def shape(path: str) -> str:
    """The route a path names: no query string, every parameter one `{}`."""
    return _HOLE.sub("{}", path.split("?", 1)[0])


class _Module:
    def __init__(self, name: str, tree: ast.Module) -> None:
        self.name, self.tree = name, tree
        self.constants: dict = {}
        self.functions: dict = {}
        self.modules: dict = {}
        self.names: dict = {}
        for node in tree.body:
            targets = node.targets if isinstance(node, ast.Assign) else (
                [node.target] if isinstance(node, ast.AnnAssign) and node.value else [])
            for t in targets:
                if isinstance(t, ast.Name):
                    self.constants[t.id] = node.value
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.functions.setdefault(node.name, node)
            elif isinstance(node, ast.ImportFrom) and node.level == 1:
                for a in node.names:
                    if node.module is None:
                        self.modules[a.asname or a.name] = a.name
                    else:
                        self.names[a.asname or a.name] = (node.module, a.name)


class _Scope:
    """What a name means inside one function: its assignments and its parameters."""

    def __init__(self, mod: _Module, func, bindings: dict | None = None) -> None:
        self.mod, self.func = mod, func
        self.assigns: dict = {}
        self.params: list = []
        if func is not None:
            a = func.args
            self.params = [p.arg for p in a.posonlyargs + a.args + a.kwonlyargs]
            for node in ast.walk(func):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        self._bind(target, node.value)
                elif isinstance(node, ast.AnnAssign) and node.value is not None:
                    self._bind(node.target, node.value)
        # A parameter is either bound to the caller's argument, or the mark.
        self.bindings = bindings if bindings is not None else {p: _MARK for p in self.params}

    def _bind(self, target, value) -> None:
        if isinstance(target, ast.Name):
            self.assigns.setdefault(target.id, []).append(value)
        elif isinstance(target, ast.Tuple):
            for i, elt in enumerate(target.elts):
                if isinstance(elt, ast.Name):
                    self.assigns.setdefault(elt.id, []).append((value, i))


@dataclass
class Scan:
    modules: list  # every module read
    building: list  # the modules that build at least one request
    calls: dict  # (METHOD, shape) -> {"module.py:function", ...}
    unread: dict  # "module.py:function" -> the call that could not be read


class _Scanner:
    def __init__(self, package: Path) -> None:
        self.mods: dict = {}
        for path in sorted(package.rglob("*.py")):
            name = path.relative_to(package).with_suffix("").as_posix().replace("/", ".")
            self.mods[name] = _Module(name, ast.parse(path.read_text(), filename=str(path)))
        self.methods: dict = {}
        self.transport: set = set()
        for mod in self.mods.values():
            for node in ast.walk(mod.tree):
                if isinstance(node, ast.ClassDef):
                    for item in node.body:
                        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            self.methods.setdefault(item.name, (mod, item))
                            params = [a.arg for a in item.args.args]
                            if params[:3] == ["self", "method", "path"]:
                                # `SwarmClient.request` and the transport under it.
                                self.transport.add(item.name)
        self.wrappers: dict = {}  # (module, function) -> the request site inside it

    # -- what a string expression can be -----------------------------------
    def strings(self, expr, scope: _Scope, depth: int = 0):
        """Every string `expr` can be, or None when one of them cannot be read."""
        if depth > 16 or expr is None:
            return None
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            return [expr.value]
        if isinstance(expr, ast.JoinedStr):
            out = [""]
            for i, part in enumerate(expr.values):
                if isinstance(part, ast.Constant):
                    out = [o + part.value for o in out]
                    continue
                # A hole that opens the string is a path being extended
                # (`f"{path}/end"`); anywhere else it is a path parameter.
                inner = self.strings(part.value, scope, depth + 1) if i == 0 else None
                out = [o + "{}" for o in out] if inner is None else [o + s for o in out for s in inner]
            return out
        if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
            left = self.strings(expr.left, scope, depth + 1)
            right = self.strings(expr.right, scope, depth + 1)
            return None if left is None or right is None else [a + b for a in left for b in right]
        if isinstance(expr, ast.IfExp):
            a = self.strings(expr.body, scope, depth + 1)
            b = self.strings(expr.orelse, scope, depth + 1)
            return None if a is None or b is None else a + b
        if isinstance(expr, ast.Call):
            if isinstance(expr.func, ast.Attribute) and expr.func.attr == "format":
                base = self.strings(expr.func.value, scope, depth + 1)
                return None if base is None else [_HOLE.sub("{}", b) for b in base]
            target = self.function(expr.func, scope.mod)
            if target is None:
                return None
            # A helper that returns a path (`input_path(...)`): what it returns.
            inner = self.bind(target, expr, scope)
            out = []
            for node in ast.walk(target[1]):
                if isinstance(node, ast.Return) and node.value is not None:
                    got = self.strings(node.value, inner, depth + 1)
                    if got is None:
                        return None
                    out += got
            return out or None
        if isinstance(expr, ast.Name):
            return self.name(expr.id, scope, depth)
        if isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name):
            other = self.mods.get(scope.mod.modules.get(expr.value.id))
            if other is not None and expr.attr in other.constants:
                return self.strings(other.constants[expr.attr], _Scope(other, None), depth + 1)
        return None

    def name(self, ident: str, scope: _Scope, depth: int):
        if ident in scope.assigns:
            out = []
            for value in scope.assigns[ident]:
                got = None if isinstance(value, tuple) else self.strings(value, scope, depth + 1)
                if got is None:
                    return None
                out += got
            return out
        if ident in scope.bindings:
            bound = scope.bindings[ident]
            return [_MARK] if bound == _MARK else self.strings(bound[0], bound[1], depth + 1)
        if ident in scope.params:
            return None  # a parameter the caller did not pass: its default is not a route
        mod = scope.mod
        if ident in mod.constants:
            return self.strings(mod.constants[ident], _Scope(mod, None), depth + 1)
        if ident in mod.names:
            other, real = mod.names[ident]
            if other in self.mods and real in self.mods[other].constants:
                return self.strings(self.mods[other].constants[real], _Scope(self.mods[other], None), depth + 1)
        return None

    # -- which function a call reaches -------------------------------------
    def function(self, func, mod: _Module):
        if isinstance(func, ast.Name):
            if func.id in mod.functions:
                return mod, mod.functions[func.id]
            other, real = mod.names.get(func.id, (None, None))
            if other in self.mods and real in self.mods[other].functions:
                return self.mods[other], self.mods[other].functions[real]
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            other = self.mods.get(mod.modules.get(func.value.id))
            if other is not None and func.attr in other.functions:
                return other, other.functions[func.attr]
            if func.value.id == "self" and func.attr in mod.functions:
                return mod, mod.functions[func.attr]
        return None

    def bind(self, target, call: ast.Call, caller: _Scope) -> _Scope:
        mod, fn = target
        params = [p.arg for p in fn.args.posonlyargs + fn.args.args]
        if params and params[0] in ("self", "cls") and isinstance(call.func, ast.Attribute):
            params = params[1:]
        bindings = {p: (a, caller) for p, a in zip(params, call.args)}
        bindings.update({k.arg: (k.value, caller) for k in call.keywords if k.arg})
        return _Scope(mod, fn, bindings)

    # -- the requests ------------------------------------------------------
    def sites(self, mod: _Module):
        """(function, call, method expr, path expr, wrapper) for every request built in `mod`."""
        owner: dict = {}
        for node in ast.walk(mod.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for inner in ast.walk(node):
                    owner[id(inner)] = node  # walk is breadth first: the innermost def wins
        for node in ast.walk(mod.tree):
            if not isinstance(node, ast.Call):
                continue
            fn, f = owner.get(id(node)), node.func
            if isinstance(f, ast.Attribute) and f.attr in self.transport and len(node.args) >= 2:
                if fn is None or fn.name not in self.transport:  # the transport handing on its own call
                    yield fn, node, node.args[0], node.args[1], None
                continue
            url = node.args[0] if node.args else None
            if (isinstance(f, (ast.Attribute, ast.Name)) and getattr(f, "attr", getattr(f, "id", "")) == "Request"
                    and isinstance(url, ast.JoinedStr) and url.values
                    and isinstance(url.values[0], ast.FormattedValue)
                    and isinstance(url.values[0].value, ast.Attribute)
                    and url.values[0].value.attr == "base_url"):
                # A request built by hand on the client's own base URL.
                method = next((k.value for k in node.keywords if k.arg == "method"), None)
                yield fn, node, method, ast.JoinedStr(values=url.values[1:]), None
                continue
            target = self.function(f, mod)
            if target is None and isinstance(f, ast.Attribute):
                target = self.methods.get(f.attr)
            if target is not None and (target[0].name, target[1].name) in self.wrappers:
                yield fn, node, None, None, target

    def pairs(self, site, scope: _Scope, depth: int = 0):
        """Every (method, path) one request site can send, or None."""
        _fn, node, method_expr, path_expr, target = site
        if target is not None:
            if depth > 8:
                return None
            return self.pairs(self.wrappers[(target[0].name, target[1].name)],
                              self.bind(target, node, scope), depth + 1)
        table = self.table(method_expr, path_expr, scope)
        if table is not None:
            return table
        methods = self.strings(method_expr, scope)
        paths = self.strings(path_expr, scope)
        return None if methods is None or paths is None else [(m, p) for m in methods for p in paths]

    def table(self, method_expr, path_expr, scope: _Scope):
        """`method, path = ROWS[key]`: every row of the table, each pair kept together."""
        if not (isinstance(method_expr, ast.Name) and isinstance(path_expr, ast.Name)):
            return None
        m, p = scope.assigns.get(method_expr.id, []), scope.assigns.get(path_expr.id, [])
        if not (len(m) == len(p) == 1 and isinstance(m[0], tuple) and isinstance(p[0], tuple)
                and m[0][0] is p[0][0] and isinstance(m[0][0], ast.Subscript)
                and isinstance(m[0][0].value, ast.Name)):
            return None
        rows = scope.mod.constants.get(m[0][0].value.id)
        if not isinstance(rows, ast.Dict):
            return None
        out = []
        for row in rows.values:
            if not isinstance(row, ast.Tuple):
                return None
            methods = self.strings(row.elts[m[0][1]], scope)
            paths = self.strings(row.elts[p[0][1]], scope)
            if methods is None or paths is None:
                return None
            out += [(a, b) for a in methods for b in paths]
        return out

    def run(self) -> Scan:
        # A function that hands its own parameters to a request is a wrapper;
        # its callers are read through it. Wrappers of wrappers too, so this
        # repeats until no new wrapper turns up.
        while True:
            calls, unread, building, grew = {}, {}, set(), False
            for mod in self.mods.values():
                for site in list(self.sites(mod)):
                    fn = site[0]
                    where = f"{mod.name}.py:{fn.name if fn else '<module>'}"
                    pairs = self.pairs(site, _Scope(mod, fn))
                    if pairs is None:
                        unread[where] = ast.unparse(site[1])
                        continue
                    if any(_MARK in m or _MARK in p for m, p in pairs):
                        if fn is not None and (mod.name, fn.name) not in self.wrappers:
                            self.wrappers[(mod.name, fn.name)] = site
                            grew = True
                        continue
                    building.add(mod.name)
                    for m, p in pairs:
                        calls.setdefault((m, shape(p)), set()).add(where)
            if not grew:
                return Scan(sorted(self.mods), sorted(building), calls, unread)


def scan(package: Path) -> Scan:
    return _Scanner(package).run()
