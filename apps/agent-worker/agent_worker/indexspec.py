"""`repo-index.json`, checked by the worker with promotion's own rules (lane INDEX-ROUTES, #1693).

Measured 2026-10-20 on ai-studio (repo_d754779d0cf31b44): 3 of 19 index runs
were refused at promotion (`routes.N.kind` missing, `routes.520.handler`
empty, `modules.4.purpose` over 300 characters). swarm-api checks the
document with `swarm_api.repoindex.RepoIndexSpec` only after the attempt has
ended, so a refusal cost the whole run -- extractor, language servers and
agent -- and the next run was given nothing to learn from.

So the worker checks it first, while the agent's session can still be
continued: the claude-code runner gives an index run ONE repair turn naming
exactly what is wrong (`runners.cliagent.INDEX_REPAIR_MAX_TURNS`), and a
document still refused after it fails the attempt with the same lines
(`lifecycle.Worker._index_after_agent`) before the graph write stores it.

A RESTATEMENT, NOT AN IMPORT. The worker image carries neither swarm_api nor
pydantic, so `RepoIndexSpec` is restated here as a table, and checked the way
pydantic checks it in lax Python mode -- `parse_index`'s `model_validate` over
`json.loads` -- with pydantic's own messages, in its order, as
`parse_index`'s `field: message` lines, the first 20. A worker that refused a
document promotion accepts would cost a turn for nothing; one that accepted a
document promotion refuses would cost the run, as before.
tests/unit/worker/test_index_output_repair.py holds the two equal, field for
field and line for line, over a corpus of every rule; change both together.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

#: `swarm_api.repoindex.SCHEMA`, `INDEX_FILE`, `MAX_INDEX_BYTES`, held equal by the test.
SCHEMA = "swarm.repo-index/v1"
INDEX_FILE = "repo-index.json"
MAX_INDEX_BYTES = 512 * 1024
#: `parse_index` names the first 20 problems; so does the repair prompt.
MAX_PROBLEMS = 20


# --------------------------------------------------------------------------
# the field types, as pydantic checks them
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class _Str:
    min_length: int | None = None
    max_length: int | None = None
    pattern: str | None = None
    #: A field validator run after the constraints: a message, or None.
    after: Callable[[str], str | None] | None = None


@dataclass(frozen=True)
class _Lit:
    values: tuple[str, ...]


@dataclass(frozen=True)
class _Int:
    """`StrictInt`: an int, never a bool, a float or a string."""

    ge: int | None = None


@dataclass(frozen=True)
class _Bool:
    """`StrictBool`."""


@dataclass(frozen=True)
class _Float:
    """`float` in lax mode: an int, a float, a bool or numeric text."""

    ge: float | None = None
    le: float | None = None


@dataclass(frozen=True)
class _List:
    item: Any
    max_length: int | None = None


@dataclass(frozen=True)
class _Model:
    name: str


@dataclass(frozen=True)
class _Opt:
    """`X | None`."""

    inner: Any


@dataclass(frozen=True)
class _Field:
    name: str
    kind: Any
    required: bool = True
    alias: str | None = None


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def _number(value: float) -> str:
    """A bound as pydantic prints it: 0.0 as 0, 0.5 as 0.5."""
    return str(int(value)) if float(value).is_integer() else str(value)


def _timestamp(value: str) -> str | None:
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return "built_at is an ISO 8601 timestamp"
    return None


_SHA = _Str(pattern=r"^[0-9a-f]{40}$")
_PATH = _Str(1, 400)
_NAME = _Str(1, 80)
_LINE = _Str(1, 300)
_COMMAND = _Str(1, 500)
_EVIDENCE = _Lit(("declared", "path-ref", "lsp", "ast", "co-change", "import", "naming"))
_TRUNCATABLE = _Lit((
    "modules", "entry_points", "routes", "test_layout", "test_map", "always_tests",
    "territory", "commands", "hot_spots", "notes", "languages", "graph",
))


def _f(name: str, kind: Any, *, required: bool = True, alias: str | None = None) -> _Field:
    return _Field(name, kind, required, alias)


def _o(name: str, kind: Any) -> _Field:
    """An optional field: absent, or None when `kind` is `_Opt`."""
    return _Field(name, kind, False)


# --------------------------------------------------------------------------
# RepoIndexSpec, section by section (swarm_api/repoindex.py, §2.1-§2.2)
# --------------------------------------------------------------------------

def _extractor_reason(value: Mapping[str, Any]) -> str | None:
    if not value.get("ran") and not value.get("reason"):
        return "an extractor that did not run says why, in reason"
    return None


def _coverage_adds_up(value: Mapping[str, Any]) -> str | None:
    if not value["source_modules_with_tests"] <= value["source_modules"] <= value["modules"]:
        return "source_modules_with_tests <= source_modules <= modules"
    return None


def _incremental_has_a_base(value: Mapping[str, Any]) -> str | None:
    if value.get("kind") == "incremental" and value.get("base_sha") is None:
        return "an incremental index names the base_sha it was built from"
    return None


_MODELS: dict[str, tuple[tuple[_Field, ...], Callable[[Mapping[str, Any]], str | None] | None]] = {
    "Module": ((
        _f("path", _PATH), _f("language", _NAME), _o("purpose", _Str(max_length=300)),
        _f("files", _Int(0)), _f("lines", _Int(0)), _o("commit_sha", _Opt(_SHA)),
    ), None),
    "EntryPoint": ((
        _f("path", _PATH), _f("kind", _NAME), _o("started_by", _Opt(_LINE)),
    ), None),
    "Route": ((
        _f("kind", _Lit(("http", "export", "mcp"))), _f("file", _PATH),
        _o("method", _Opt(_Str(1, 10))), _o("path", _Opt(_Str(1, 400))),
        _o("name", _Opt(_Str(1, 200))), _o("handler", _Opt(_Str(1, 200))),
    ), None),
    "TestSuite": ((
        _f("root", _PATH), _f("framework", _NAME), _f("command", _COMMAND),
        _o("needs", _List(_Lit(("emulator", "credentials", "network", "docker")), 4)),
        _o("covers", _List(_PATH, 50)),
    ), None),
    "TestEdge": ((
        _f("source", _PATH), _f("test", _PATH), _f("evidence", _EVIDENCE),
        _o("command", _Opt(_COMMAND)), _o("confidence", _Opt(_Float(0.0, 1.0))),
        _o("also_evidence", _List(_EVIDENCE, len(_EVIDENCE.values))),
    ), None),
    "AlwaysTest": ((
        _f("target", _PATH), _f("because", _LINE), _o("command", _Opt(_COMMAND)),
        _o("source", _Opt(_PATH)),
    ), None),
    "TerritoryRule": ((
        _f("path", _PATH), _f("rule", _LINE), _f("source", _PATH),
    ), None),
    "CommandEntry": ((
        _f("name", _NAME), _f("kind", _Lit(("build", "lint", "test", "ci", "other"))),
        _f("command", _COMMAND), _f("source", _PATH),
    ), None),
    "HotSpot": ((
        _f("path", _PATH), _f("changes", _Int(0)), _o("co_changed", _List(_PATH, 10)),
    ), None),
    "Note": ((
        _f("text", _LINE), _o("source", _Opt(_PATH)),
    ), None),
    "LanguageRow": ((
        _f("language", _NAME), _f("files", _Int(0)),
        _o("grammar", _Opt(_Str(max_length=80))), _o("server", _Opt(_Str(max_length=80))),
        _f("status", _Lit(("ok", "partial", "unsupported", "failing", "timed_out", "not_run"))),
        _o("files_covered", _Opt(_Int(0))), _o("fallback", _Opt(_LINE)),
    ), None),
    "TopSymbol": ((
        _f("id", _Str(1, 500)), _f("callers", _Int(0)),
    ), None),
    "GraphSummary": ((
        _o("manifest_digest", _Opt(_Str(pattern=r"^sha256:[0-9a-f]{64}$"))),
        _o("symbols", _Int(0)), _o("edges", _Int(0)),
        _o("top_symbols", _List(_Model("TopSymbol"), 100)),
    ), None),
    "History": ((
        _f("available", _Bool()), _o("reason", _Opt(_Str(max_length=500))),
        _o("window_days", _Opt(_Int(0))), _o("window_start", _Opt(_Str(max_length=40))),
        _o("window_end", _Opt(_Str(max_length=40))), _o("commits", _Opt(_Int(0))),
        _o("shallow", _Opt(_Bool())), _o("boundary_commits", _Opt(_Int(0))),
        _o("window_covered", _Opt(_Bool())),
    ), None),
    "Extractor": ((
        _f("ran", _Bool()), _o("command", _Opt(_Str(max_length=80))),
        _o("version", _Opt(_Str(max_length=80))), _o("reason", _Opt(_LINE)),
        _o("history", _Opt(_Model("History"))),
    ), _extractor_reason),
    "NotCounted": ((
        _f("path", _PATH), _f("reason", _Lit(("test", "build"))),
    ), None),
    "TestCoverage": ((
        _f("modules", _Int(0)), _f("source_modules", _Int(0)),
        _f("source_modules_with_tests", _Int(0)),
        _o("not_counted", _List(_Model("NotCounted"), 400)),
        _o("without_tests", _List(_PATH, 400)),
    ), _coverage_adds_up),
    "RepoIndexSpec": ((
        _f("schema_", _Lit((SCHEMA,)), alias="schema"),
        _f("commit_sha", _SHA), _f("branch", _Str(1, 255)),
        _f("built_at", _Str(1, 40, after=_timestamp)),
        _f("kind", _Lit(("full", "incremental"))), _o("base_sha", _Opt(_SHA)),
        _f("extractor", _Model("Extractor")),
        _o("modules", _List(_Model("Module"), 400)),
        _o("entry_points", _List(_Model("EntryPoint"), 200)),
        _o("routes", _List(_Model("Route"), 1000)),
        _o("test_layout", _List(_Model("TestSuite"), 50)),
        _o("test_map", _List(_Model("TestEdge"), 4000)),
        _o("always_tests", _List(_Model("AlwaysTest"), 50)),
        _o("territory", _List(_Model("TerritoryRule"), 200)),
        _o("commands", _List(_Model("CommandEntry"), 100)),
        _o("hot_spots", _List(_Model("HotSpot"), 50)),
        _o("notes", _List(_Model("Note"), 20)),
        _o("languages", _List(_Model("LanguageRow"), 50)),
        _o("graph", _Opt(_Model("GraphSummary"))),
        _o("test_coverage", _Opt(_Model("TestCoverage"))),
        _o("truncated", _List(_TRUNCATABLE, 12)),
    ), _incremental_has_a_base),
}


def required_fields(model: str) -> dict[str, bool]:
    """Each field of `model` by the key a document carries, and whether it is required."""
    fields, _validator = _MODELS[model]
    return {field.alias or field.name: field.required for field in fields}


# --------------------------------------------------------------------------
# the check
# --------------------------------------------------------------------------

Loc = tuple[Any, ...]


def _check(kind: Any, value: Any, loc: Loc, errors: list[tuple[Loc, str]]) -> None:
    """Append (loc, message) for every way `value` breaks `kind`, as pydantic would."""
    if isinstance(kind, _Opt):
        if value is not None:
            _check(kind.inner, value, loc, errors)
        return
    if isinstance(kind, _Str):
        if not isinstance(value, str):
            errors.append((loc, "Input should be a valid string"))
        elif kind.min_length is not None and len(value) < kind.min_length:
            errors.append((loc, f"String should have at least "
                                f"{_plural(kind.min_length, 'character')}"))
        elif kind.max_length is not None and len(value) > kind.max_length:
            errors.append((loc, f"String should have at most "
                                f"{_plural(kind.max_length, 'character')}"))
        elif kind.pattern is not None and re.search(kind.pattern, value) is None:
            errors.append((loc, f"String should match pattern '{kind.pattern}'"))
        elif kind.after is not None and (said := kind.after(value)) is not None:
            errors.append((loc, f"Value error, {said}"))
        return
    if isinstance(kind, _Lit):
        if not (isinstance(value, str) and value in kind.values):
            quoted = [f"'{v}'" for v in kind.values]
            listed = quoted[0] if len(quoted) == 1 else (
                ", ".join(quoted[:-1]) + " or " + quoted[-1])
            errors.append((loc, f"Input should be {listed}"))
        return
    if isinstance(kind, _Int):
        if not isinstance(value, int) or isinstance(value, bool):
            errors.append((loc, "Input should be a valid integer"))
        elif kind.ge is not None and value < kind.ge:
            errors.append((loc, f"Input should be greater than or equal to {kind.ge}"))
        return
    if isinstance(kind, _Bool):
        if not isinstance(value, bool):
            errors.append((loc, "Input should be a valid boolean"))
        return
    if isinstance(kind, _Float):
        number: float
        if isinstance(value, (int, float)):
            number = float(value)
        elif isinstance(value, str):
            try:
                number = float(value.strip())
            except ValueError:
                errors.append((loc, "Input should be a valid number, unable to parse "
                                    "string as a number"))
                return
        else:
            errors.append((loc, "Input should be a valid number"))
            return
        if kind.ge is not None and not number >= kind.ge:
            errors.append((loc, f"Input should be greater than or equal to {_number(kind.ge)}"))
        elif kind.le is not None and not number <= kind.le:
            errors.append((loc, f"Input should be less than or equal to {_number(kind.le)}"))
        return
    if isinstance(kind, _List):
        if not isinstance(value, list):
            errors.append((loc, "Input should be a valid list"))
            return
        before = len(errors)
        for n, item in enumerate(value):
            _check(kind.item, item, (*loc, n), errors)
        if len(errors) == before and kind.max_length is not None \
                and len(value) > kind.max_length:
            errors.append((loc, f"List should have at most {_plural(kind.max_length, 'item')} "
                                f"after validation, not {len(value)}"))
        return
    if isinstance(kind, _Model):
        _check_model(kind.name, value, loc, errors)
        return
    raise TypeError(f"no check for {kind!r}")


def _check_model(name: str, value: Any, loc: Loc, errors: list[tuple[Loc, str]]) -> None:
    if not isinstance(value, Mapping):
        errors.append((loc, f"Input should be a valid dictionary or instance of {name}"))
        return
    fields, validator = _MODELS[name]
    before = len(errors)
    used: set[str] = set()
    seen: dict[str, Any] = {}
    for field in fields:
        key = field.alias or field.name
        if key not in value and field.alias is not None and field.name in value:
            key = field.name  # `populate_by_name`: the field's own name, absent its alias
        if key not in value:
            if field.required:
                errors.append(((*loc, field.alias or field.name), "Field required"))
            continue
        used.add(key)
        seen[field.name] = value[key]
        _check(field.kind, value[key], (*loc, key), errors)
    for key in value:
        if key not in used:
            errors.append(((*loc, key), "Extra inputs are not permitted"))
    if validator is not None and len(errors) == before:
        said = validator(seen)
        if said is not None:
            errors.append((loc, f"Value error, {said}"))


def problems(text: str) -> list[str]:
    """What promotion would say of `text` (`parse_index`), as `field: message`
    lines, the first `MAX_PROBLEMS`; [] when it would accept it."""
    if len(text.encode("utf-8")) > MAX_INDEX_BYTES:
        return [f"{INDEX_FILE} is larger than {MAX_INDEX_BYTES} bytes"]
    try:
        value = json.loads(text)
    except ValueError:
        return [f"{INDEX_FILE} is not JSON"]
    if not isinstance(value, Mapping):
        return [f"{INDEX_FILE} is a JSON object"]
    errors: list[tuple[Loc, str]] = []
    _check_model("RepoIndexSpec", value, (), errors)
    return [f"{'.'.join(str(part) for part in where) or 'index'}: {message}"
            for where, message in errors[:MAX_PROBLEMS]]


def refusal(found: Sequence[str]) -> str | None:
    """The one line an attempt fails with, in `parse_index`'s own wording, for
    the lines `problems` gave; None when there are none."""
    if not found:
        return None
    if len(found) == 1 and found[0].startswith(INDEX_FILE):
        return found[0]
    return f"{INDEX_FILE} does not match {SCHEMA}: " + "; ".join(found)


def problems_of_file(path: Path | str) -> list[str] | None:
    """`problems` of the file at `path`; None when there is no regular file there."""
    target = Path(path)
    try:
        if not target.is_file() or target.is_symlink():
            return None
        data = target.read_bytes()
    except OSError:
        return None
    if len(data) > MAX_INDEX_BYTES:
        return [f"{INDEX_FILE} is larger than {MAX_INDEX_BYTES} bytes"]
    return problems(data.decode("utf-8", errors="replace"))


__all__: Sequence[str] = (
    "INDEX_FILE", "MAX_INDEX_BYTES", "MAX_PROBLEMS", "SCHEMA", "problems", "problems_of_file",
    "refusal", "required_fields",
)
