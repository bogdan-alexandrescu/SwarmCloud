"""The Timeline reads exactly what `GET /v1/outcomes` sends, and asks only what it accepts (#185).

WHY THIS FILE. The route (#196) and the page (#197) were written in two lanes at
once, against a contract neither could run. `test_ui_api_field_contract.py`
holds `types.ts` to the payloads it was written for; the ledger's types are in
`apps/swarm-ui/src/outcomes.ts` and nothing held them. A renamed or retyped
field does not fail to compile in the browser -- it renders as `undefined`,
which the page draws as an em dash or, worse, as the absence-means-zero the
honesty rules forbid.

WHAT IS CHECKED, from the real route over FakeFirestore, never a mock:

  * EVERY FIELD, BOTH DIRECTIONS, AT EVERY DEPTH. Each object the payload
    carries is checked against the TypeScript type that declares it: every
    required field is sent, every sent field is declared, and each value is of
    the declared type -- string, number, boolean, null, a literal, an array, a
    `Record` over a literal union, a union or an intersection. So a state the
    route sends and the page's union lacks (`BucketState`, `UnreadReason`, a
    failure class) fails here, and so does a nullable field typed as always
    present.
  * NOTHING VACUOUS. Every array the types declare must have been checked
    with at least one element, and every nullable object at least once not
    null, across the payloads below -- a type checked only against `[]` or
    `null` would pass whatever it declared.
  * THE QUERY. Every parameter `outcomesQuery` can set is one the route
    declares, and the page's span, bucket, kind and group choices, its default
    span and its restated limits equal the route's.

The payloads are #196's own fixture week (`test_outcomes_route.seed_week`),
imported rather than copied: tenant scope with every filter the page sends and
`compare=previous`; platform scope grouped by tenant with an exclusion, and
with an include list; and a read past its derive budget, whose buckets are
unread.

The client source is read as TEXT, as the route-level seam test reads
`api.ts`: this is a check on the shipped declarations.

UNTIL #196 IS MERGED this file fails on its first line, exactly as
`test_every_v1_path_the_ui_calls_is_served_by_this_api` does, and for the same
reason: the page calls a route this branch does not serve. It FAILS rather
than skips, because a skip reads as a pass in CI output. The import is inside
the fixtures, so the rest of the suite still collects and runs.

Offline: FakeFirestore, StaticTokenVerifier, StaticGroups, a fixed clock.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[3]
OUTCOMES_TS = REPO / "apps/swarm-ui/src/outcomes.ts"


# --------------------------------------------------------------------------
# The route, which is #196's
# --------------------------------------------------------------------------

def _route_side():
    """(swarm_api.outcomes, swarm_api.routes.outcomes, #196's route tests), or a failure that says why."""
    try:
        from swarm_api import outcomes as module
        from swarm_api.routes import outcomes as routes

        from . import test_outcomes_route as route_tests
    except ImportError as exc:  # before #196 is merged
        pytest.fail(
            f"GET /v1/outcomes is not on this branch ({exc}). outcomes.ts reads a route "
            "nothing here serves; this goes green when #196 is merged, with the seam test."
        )
    return module, routes, route_tests


# --------------------------------------------------------------------------
# Reading outcomes.ts
# --------------------------------------------------------------------------

def _source() -> str:
    # A missing client file FAILS: a skip is indistinguishable from a pass.
    assert OUTCOMES_TS.is_file(), f"{OUTCOMES_TS} is not present; this test would check nothing"
    text = OUTCOMES_TS.read_text()
    assert text.strip(), f"{OUTCOMES_TS} is empty; this test would check nothing"
    return text


def _strip_comments(text: str) -> str:
    """Block comments, and line comments that start a line or follow whitespace (never `https://`)."""
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    return re.sub(r"(^|\s)//[^\n]*", r"\1", text)


def _braced(text: str, open_at: int) -> str:
    """The text between the brace at `open_at` and its partner, braces counted."""
    assert text[open_at] == "{"
    depth = 0
    for i in range(open_at, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[open_at : i + 1]
    raise AssertionError("an interface in outcomes.ts is not closed")


def _declarations() -> dict[str, str]:
    """Every exported interface and type alias in outcomes.ts, as type text."""
    source = _strip_comments(_source())
    out: dict[str, str] = {}
    for m in re.finditer(r"^export interface (\w+) \{", source, flags=re.MULTILINE):
        out[m.group(1)] = _braced(source, m.end() - 1)
    for m in re.finditer(r"^export type (\w+) =", source, flags=re.MULTILINE):
        rest = source[m.end():]
        # An alias runs until the next line that starts at column 0.
        end = re.search(r"\n(?=\S)", rest)
        out[m.group(1)] = (rest[: end.start()] if end else rest).strip()
    assert "Outcomes" in out, "interface Outcomes is not in outcomes.ts; this check would be vacuous"
    return out


_TOKEN = re.compile(r"\s*(?:('[^']*')|(\w+)|(\S))")


def _tokens(text: str) -> list[str]:
    out = []
    pos = 0
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if m is None or m.end() == pos:
            break
        tok = m.group(1) or m.group(2) or m.group(3)
        if tok is not None:
            out.append(tok)
        pos = m.end()
    return out


class _Parser:
    """A TypeScript type expression, for the forms outcomes.ts uses in its response types.

    union (`A | B`, a leading `|` allowed), intersection (`A & B`), `T[]`,
    `Array<T>`, `Record<K, V>`, object literals (`{ a: T; b?: U }`, `;` or a
    new line between fields), string literals, `(...)`, primitives, and names
    of other declarations. Anything else raises, so an unread form cannot pass
    as checked.
    """

    def __init__(self, text: str) -> None:
        self.toks = _tokens(text)
        self.i = 0

    def peek(self) -> str | None:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def take(self, expected: str | None = None) -> str:
        tok = self.peek()
        if tok is None or (expected is not None and tok != expected):
            raise AssertionError(f"outcomes.ts type: expected {expected!r}, found {tok!r} in {self.toks}")
        self.i += 1
        return tok

    def parse(self) -> tuple:
        t = self.union()
        if self.peek() is not None:
            raise AssertionError(f"outcomes.ts type: unread tail {self.toks[self.i:]}")
        return t

    def union(self) -> tuple:
        if self.peek() == "|":
            self.take("|")
        members = [self.inter()]
        while self.peek() == "|":
            self.take("|")
            members.append(self.inter())
        return members[0] if len(members) == 1 else ("or", members)

    def inter(self) -> tuple:
        members = [self.postfix()]
        while self.peek() == "&":
            self.take("&")
            members.append(self.postfix())
        return members[0] if len(members) == 1 else ("and", members)

    def postfix(self) -> tuple:
        t = self.primary()
        while self.peek() == "[" and self.toks[self.i + 1 : self.i + 2] == ["]"]:
            self.take("[")
            self.take("]")
            t = ("arr", t)
        return t

    def primary(self) -> tuple:
        tok = self.peek()
        if tok == "{":
            return self.obj()
        if tok == "(":
            self.take("(")
            t = self.union()
            self.take(")")
            return t
        if tok is not None and tok.startswith("'"):
            self.take()
            return ("lit", tok[1:-1])
        name = self.take()
        if not re.fullmatch(r"\w+", name):
            raise AssertionError(f"outcomes.ts type: unexpected {name!r} in {self.toks}")
        if self.peek() == "<":
            self.take("<")
            args = [self.union()]
            while self.peek() == ",":
                self.take(",")
                args.append(self.union())
            self.take(">")
            if name == "Array" and len(args) == 1:
                return ("arr", args[0])
            if name == "Record" and len(args) == 2:
                return ("record", args[0], args[1])
            raise AssertionError(f"outcomes.ts type: {name}<...> is not a form this check reads")
        if name in ("string", "number", "boolean", "null"):
            return ("prim", name)
        return ("ref", name)

    def obj(self) -> tuple:
        self.take("{")
        fields: dict[str, tuple[bool, tuple]] = {}
        while self.peek() != "}":
            name = self.take()
            optional = False
            if self.peek() == "?":
                self.take("?")
                optional = True
            self.take(":")
            fields[name] = (optional, self.union())
            if self.peek() in (";", ","):
                self.take()
        self.take("}")
        return ("obj", fields)


class _Checker:
    """Checks a JSON value against a parsed type, and records what it actually exercised."""

    def __init__(self, decls: dict[str, str]) -> None:
        self.decls = decls
        self.parsed: dict[str, tuple] = {}
        #: array paths checked with at least one element
        self.arrays_seen: set[str] = set()
        #: nullable structured paths seen not null
        self.nonnull_seen: set[str] = set()

    def resolve(self, t: tuple) -> tuple:
        while t[0] == "ref":
            name = t[1]
            assert name in self.decls, f"outcomes.ts names a type it does not declare: {name}"
            if name not in self.parsed:
                self.parsed[name] = _Parser(self.decls[name]).parse()
            t = self.parsed[name]
        return t

    def literals(self, t: tuple) -> set[str] | None:
        t = self.resolve(t)
        if t[0] == "lit":
            return {t[1]}
        if t[0] == "or":
            out: set[str] = set()
            for m in t[1]:
                sub = self.literals(m)
                if sub is None:
                    return None
                out |= sub
            return out
        return None

    def fields(self, t: tuple) -> dict[str, tuple[bool, tuple]] | None:
        """The fields of an object or an intersection of objects; None for anything else."""
        t = self.resolve(t)
        if t[0] == "obj":
            return dict(t[1])
        if t[0] == "and":
            out: dict[str, tuple[bool, tuple]] = {}
            for m in t[1]:
                sub = self.fields(m)
                if sub is None:
                    return None
                out.update(sub)
            return out
        return None

    @staticmethod
    def structured(t: tuple) -> bool:
        return t[0] in ("obj", "and", "arr", "record")

    def check(self, value: Any, t: tuple, path: str, seen: tuple[set[str], set[str]]) -> list[str]:
        t = self.resolve(t)
        kind = t[0]
        if kind == "prim":
            ok = {
                "string": isinstance(value, str),
                "number": isinstance(value, (int, float)) and not isinstance(value, bool),
                "boolean": isinstance(value, bool),
                "null": value is None,
            }[t[1]]
            return [] if ok else [f"{path}: {value!r} is not {t[1]}"]
        if kind == "lit":
            return [] if value == t[1] else [f"{path}: {value!r} is not '{t[1]}'"]
        if kind == "or":
            failures = []
            for member in t[1]:
                trial: tuple[set[str], set[str]] = (set(), set())
                errors = self.check(value, member, path, trial)
                if not errors:
                    seen[0].update(trial[0])
                    seen[1].update(trial[1])
                    if value is not None and self.structured(self.resolve(member)):
                        seen[1].add(path)
                    return []
                failures.append(errors)
            closest = min(failures, key=len)
            return [f"{path}: {value!r:.120} matches no member of its union; closest: {closest[:3]}"]
        if kind == "arr":
            if not isinstance(value, list):
                return [f"{path}: {value!r:.80} is not an array"]
            if value:
                seen[0].add(path + "[]")
            errors = []
            for item in value:
                errors += self.check(item, t[1], path + "[]", seen)
            return errors
        if kind == "record":
            if not isinstance(value, dict):
                return [f"{path}: {value!r:.80} is not an object"]
            errors = []
            keys = self.literals(t[1])
            if keys is not None and set(value) != keys:
                errors.append(
                    f"{path}: keys {sorted(value)} are not the declared {sorted(keys)} "
                    f"(missing {sorted(keys - set(value))}, undeclared {sorted(set(value) - keys)})"
                )
            for key, item in value.items():
                errors += self.check(item, t[2], f"{path}.{key}", seen)
            return errors
        fields = self.fields(t)
        if fields is None:
            raise AssertionError(f"{path}: a type form this check does not read: {t}")
        if not isinstance(value, dict):
            return [f"{path}: {value!r:.80} is not an object"]
        errors = []
        for name, (optional, _) in fields.items():
            if not optional and name not in value:
                errors.append(f"{path}.{name}: declared in outcomes.ts and not sent by the route")
        for name in value:
            if name not in fields:
                errors.append(f"{path}.{name}: sent by the route and not declared in outcomes.ts")
        for name, (_, ft) in fields.items():
            if name in value:
                errors += self.check(value[name], ft, f"{path}.{name}", seen)
        return errors

    def expected(self, t: tuple, path: str, arrays: set[str], nullables: set[str], depth: int = 0) -> None:
        """Every array path and nullable structured path the declared type has, walked without a value."""
        assert depth < 40, f"{path}: the type is too deep to walk; is it recursive?"
        t = self.resolve(t)
        kind = t[0]
        if kind == "or":
            members = [self.resolve(m) for m in t[1]]
            if any(m == ("prim", "null") for m in members) and any(self.structured(m) for m in members):
                nullables.add(path)
            for m in members:
                self.expected(m, path, arrays, nullables, depth + 1)
        elif kind == "arr":
            arrays.add(path + "[]")
            self.expected(t[1], path + "[]", arrays, nullables, depth + 1)
        elif kind == "record":
            return
        elif kind in ("obj", "and"):
            for name, (_, ft) in (self.fields(t) or {}).items():
                self.expected(ft, f"{path}.{name}", arrays, nullables, depth + 1)


# --------------------------------------------------------------------------
# The payloads: #196's fixture week, through the real route
# --------------------------------------------------------------------------

@pytest.fixture
def payloads(db, tokens, group_map) -> dict[str, dict[str, Any]]:
    module, _, route_tests = _route_side()
    from swarm_api.groups import StaticGroups
    from swarm_api.main import create_app

    clock = route_tests.Clock()
    route_tests.seed_week(db)
    ctx = route_tests._context(db, tokens, StaticGroups(group_map), clock)
    api = TestClient(create_app(ctx), raise_server_exceptions=False)
    week = dict(route_tests.WEEK)

    out = {
        # Tenant scope with every filter the page sends, and the previous span.
        "tenant": route_tests.ok(api, "alice", compare="previous", group="submitted_by", **week),
        "filtered": route_tests.ok(
            api, "alice", profile=["mock", "claude-code"], submitted_by="alice@saga.xyz",
            kind="all", bucket="day", **week,
        ),
        # Platform scope, both ways the page asks for it.
        "platform_excluding": route_tests.ok(
            api, "root", exclude_tenant="research", group="tenant_id", compare="previous", **week,
        ),
        "platform_including": route_tests.ok(api, "root", tenant=["research", "eng"], **week),
        # An explicit range, as the from-to inputs and a zoom send it.
        "range": route_tests.ok(api, "alice", tz="UTC", since="2026-09-20", until="2026-09-24"),
        # kind=standalone: `workflows_failed` does not apply, and its counts are
        # null rather than 0 (the review of #196), which outcomes.ts must allow.
        "standalone": route_tests.ok(api, "alice", kind="standalone", **week),
    }

    # A read past its derive budget: unread buckets, their reasons, coverage.unread.
    fresh = type(db)()
    route_tests.seed_week(fresh)
    starved = route_tests._context(fresh, tokens, StaticGroups(group_map), clock)
    starved.outcomes = module.Outcomes(
        store=starved.store, rollups=starved.rollups, metrics=starved.metrics, now=clock,
        derive_read_budget=1,
    )
    out["unread"] = route_tests.ok(
        TestClient(create_app(starved), raise_server_exceptions=False), "alice", **week,
    )
    assert out["unread"]["totals"]["complete"] is False, "the budget did not leave a bucket unread"
    return out


# --------------------------------------------------------------------------
# Every field, both directions
# --------------------------------------------------------------------------

def test_every_payload_is_exactly_the_shape_outcomes_ts_declares(payloads):
    checker = _Checker(_declarations())
    seen: tuple[set[str], set[str]] = (set(), set())
    errors: list[str] = []
    for name, body in payloads.items():
        errors += [f"[{name}] {e}" for e in checker.check(body, ("ref", "Outcomes"), "Outcomes", seen)]
    assert not errors, "outcomes.ts and GET /v1/outcomes disagree:\n  " + "\n  ".join(errors[:40])

    arrays: set[str] = set()
    nullables: set[str] = set()
    checker.expected(("ref", "Outcomes"), "Outcomes", arrays, nullables)
    assert arrays and nullables, "no arrays or nullable objects were found in Outcomes; this check is vacuous"
    unchecked_arrays = sorted(arrays - seen[0])
    assert not unchecked_arrays, (
        f"these arrays were never checked with an element, so their element type is unproven: "
        f"{unchecked_arrays}"
    )
    never_present = sorted(nullables - seen[1])
    assert not never_present, (
        f"these nullable objects were null in every payload, so their shape is unproven: {never_present}"
    )


def test_the_unions_the_page_switches_on_are_exactly_the_routes_values():
    """A value the route sends and the page's union lacks is drawn as nothing; one it never sends is dead code."""
    module, _, _ = _route_side()
    checker = _Checker(_declarations())

    def lit(name: str) -> set[str] | None:
        return checker.literals(("ref", name))

    assert lit("FailureClassKey") == set(module.FAILURE_KEYS)
    assert lit("CancelCauseKey") == set(module.CANCEL_KEYS)
    assert lit("LedgerBucketSize") == set(module.BUCKETS)
    assert lit("GroupBy") == set(module.GROUPS)
    assert lit("UnreadReason") == set(module._REASON_ORDER)
    # The bucket states are written inline in `fold`; the payloads above hold
    # them, and these three are the ones the page draws.
    assert lit("BucketState") == {"sealed", "open", "unread"}


# --------------------------------------------------------------------------
# The query
# --------------------------------------------------------------------------

def _function_body(source: str, name: str) -> str:
    m = re.search(rf"export function {name}\([^)]*\)[^{{]*\{{", source)
    assert m, f"function {name} is not in outcomes.ts; this check would be vacuous"
    return _braced(source, m.end() - 1)


def _const_list(source: str, name: str) -> list[str]:
    m = re.search(rf"export const {name}(?::[^=]+)? = \[([^\]]*)\]", source)
    assert m, f"const {name} is not in outcomes.ts; this check would be vacuous"
    return re.findall(r"'([^']*)'", m.group(1))


def _const_scalar(source: str, name: str) -> str:
    m = re.search(rf"export const {name}(?::[^=]+)? = ('[^']*'|[\d_]+)\s*$", source, flags=re.MULTILINE)
    assert m, f"const {name} is not in outcomes.ts; this check would be vacuous"
    return m.group(1).strip("'").replace("_", "")


def test_every_parameter_the_page_sends_is_one_the_route_declares():
    _, routes, _ = _route_side()
    from fastapi.params import Query

    declared = {
        name
        for name, p in inspect.signature(routes.get_outcomes).parameters.items()
        if isinstance(p.default, Query)
    }
    assert declared, "no query parameters were read off the route; this check would be vacuous"
    body = _function_body(_strip_comments(_source()), "outcomesQuery")
    sent = set(re.findall(r"q\.(?:set|append)\('(\w+)'", body))
    assert sent, "no parameters were read out of outcomesQuery; this check would be vacuous"
    assert sent <= declared, f"outcomesQuery sends parameters the route does not declare: {sorted(sent - declared)}"
    assert "tz" in sent, "the route requires tz and the page does not send it"


def test_the_pages_choices_defaults_and_limits_are_the_routes():
    module, _, _ = _route_side()
    source = _strip_comments(_source())
    assert _const_list(source, "SPANS") == list(module.SPANS)
    assert _const_scalar(source, "DEFAULT_SPAN") == module.DEFAULT_SPAN
    assert _const_list(source, "BUCKET_CHOICES") == ["auto", *module.BUCKETS]
    assert _const_list(source, "KINDS") == list(module.KINDS)
    assert _const_list(source, "GROUPS") == list(module.GROUPS)
    for name in ("MAX_BUCKETS", "MONTH_MIN_DAYS", "MAX_SPAN_DAYS", "OUTCOMES_CACHE_S"):
        assert int(_const_scalar(source, name)) == getattr(module, name), (
            f"outcomes.ts restates {name} as {_const_scalar(source, name)}; the route's is "
            f"{getattr(module, name)}"
        )
