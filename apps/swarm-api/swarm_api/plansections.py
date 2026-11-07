"""A plan's optional approval sections: what it does, why, what changes where,
its risk, how it is validated, and the change graph (BGD, 6 November 2026).

BGD asked, on the issue-run approvals page, to see "super quickly what the
plan wants to do, why, whats going to change, where, and risk profiles plus
test and qa plans for the validation part and diagrams or code graphs". A
plan carried a summary, requirements, steps and overlaps; nothing said why,
how risky, or how it would be checked. The planner now writes these beside
its plan:

    purpose      one sentence: what the change does
    why          the problem, with the issue's evidence, two sentences at most
    changes      [{file, area, kind add|modify|delete|test, what}]
    risk         {level low|medium|high, factors, blast_radius, invariants,
                  mitigations, rollback}
    validation   {tests [{file, proves, requirements}], qa [{check, where, when}]}
    graph        {nodes [{id, label, kind changed|caller|test|route, file}],
                  edges [{from, to, kind calls|imports|tests}]}

Every one is OPTIONAL, and a plan without them -- an older plan, an older
planner -- reads, compiles and digests exactly as it did. They are advisory:
nothing compiles from them, so a malformed one never fails the plan. It is
dropped and a line saying why is kept in the plan's `section_notes`, which
the run read serves with the plan. The graph is the one section that is cut
rather than dropped when it is too big: its changed nodes are kept first.

`split_sections` takes them off a plan before `PlanSpec` checks the rest
(which still refuses any key it does not know), and `parse_plan` puts the
ones that passed back on the plan it returns.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

#: The plan's keys this module owns, in the order the planner is shown them.
SECTION_KEYS = ("purpose", "why", "changes", "risk", "validation", "graph")
#: Why a section was dropped, or the graph cut: the plan's own key, so a plan
#: read back keeps its notes (and its digest).
NOTES_KEY = "section_notes"

MAX_PURPOSE_CHARS = 300
MAX_WHY_CHARS = 600
#: "two sentences at most" (the brief); the design doc holds the longer why.
MAX_WHY_SENTENCES = 2
#: As many as a plan's step files allow, so a change list is never the cap.
MAX_CHANGES = 60
MAX_RISK_ITEMS = 8
#: CONTRACT.md numbers its invariants 1 to 10.
MAX_INVARIANT = 10
MAX_VALIDATION_TESTS = 30
MAX_QA_CHECKS = 20
#: The most requirement ids one test names; ids are the plan's own
#: requirement numbers, 1 up (`PlanSpec.requirements`).
MAX_TEST_REQUIREMENTS = 40
#: The change graph's caps: a page draws it beside the plan, so a graph past
#: these is a picture nobody reads. Over them it is cut, never refused.
MAX_GRAPH_NODES = 40
MAX_GRAPH_EDGES = 80
MAX_NOTES = 20
MAX_NOTE_CHARS = 300
#: What each overlap may say about the other pull request's change.
MAX_THEIRS_CHARS = 300

#: The order nodes are kept in when a graph is cut: what changes first.
_NODE_PRIORITY = {"changed": 0, "test": 1, "caller": 2, "route": 3}
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=\S)")


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


_Path = Annotated[str, Field(min_length=1, max_length=300)]
_Short = Annotated[str, Field(min_length=1, max_length=200)]
_Name = Annotated[str, Field(min_length=1, max_length=120)]


#: One line: no line break anywhere in it.
_ONE_LINE = r"^[^\r\n]*$"


class _Purpose(_Section):
    value: str = Field(min_length=1, max_length=MAX_PURPOSE_CHARS, pattern=_ONE_LINE)


class _Why(_Section):
    value: str = Field(min_length=1, max_length=MAX_WHY_CHARS)

    @field_validator("value")
    @classmethod
    def _two_sentences(cls, value: str) -> str:
        count = len(_SENTENCE_END.split(value.strip()))
        if count > MAX_WHY_SENTENCES:
            raise ValueError(
                f"{count} sentences, over {MAX_WHY_SENTENCES}: the longer why goes in the "
                "design doc"
            )
        return value


class Change(_Section):
    file: _Path
    area: _Name
    kind: Literal["add", "modify", "delete", "test"]
    what: str = Field(min_length=1, max_length=200, pattern=_ONE_LINE)


class _Changes(_Section):
    value: list[Change] = Field(min_length=1)


class Risk(_Section):
    level: Literal["low", "medium", "high"]
    factors: list[_Short] | None = Field(default=None, max_length=MAX_RISK_ITEMS)
    blast_radius: _Short | None = None
    #: CONTRACT.md invariant numbers the change touches; none is an empty list.
    invariants: list[Annotated[int, Field(ge=1, le=MAX_INVARIANT)]] | None = Field(
        default=None, max_length=MAX_INVARIANT)
    mitigations: list[_Short] | None = Field(default=None, max_length=MAX_RISK_ITEMS)
    rollback: _Short | None = None

    @field_validator("invariants")
    @classmethod
    def _once(cls, value: list[int] | None) -> list[int] | None:
        return sorted(set(value)) if value is not None else None


class ValidationTest(_Section):
    file: _Path
    proves: _Short
    #: The plan's requirement numbers this test proves, drawn as chips.
    requirements: list[Annotated[int, Field(ge=1)]] | None = Field(
        default=None, max_length=MAX_TEST_REQUIREMENTS)


class QaCheck(_Section):
    check: _Short
    where: _Name
    when: _Name


class Validation(_Section):
    tests: list[ValidationTest] | None = Field(default=None, max_length=MAX_VALIDATION_TESTS)
    qa: list[QaCheck] | None = Field(default=None, max_length=MAX_QA_CHECKS)


class GraphNode(_Section):
    id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_.:/-]+$")
    label: _Name
    kind: Literal["changed", "caller", "test", "route"]
    file: _Path | None = None


class GraphEdge(_Section):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, populate_by_name=True)

    source: str = Field(alias="from", min_length=1, max_length=80)
    target: str = Field(alias="to", min_length=1, max_length=80)
    kind: Literal["calls", "imports", "tests"]


class Graph(_Section):
    nodes: list[GraphNode] = Field(min_length=1)
    edges: list[GraphEdge] = Field(default_factory=list)

    @field_validator("nodes")
    @classmethod
    def _unique(cls, nodes: list[GraphNode]) -> list[GraphNode]:
        ids = [node.id for node in nodes]
        if len(set(ids)) != len(ids):
            raise ValueError("every node id must be unique")
        return nodes


def _problems(exc: ValidationError) -> str:
    found = []
    for error in exc.errors()[:3]:
        where = ".".join(str(part) for part in error.get("loc") or () if part != "value")
        found.append(f"{where}: {error.get('msg')}" if where else str(error.get("msg")))
    return "; ".join(found)


def _note(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= MAX_NOTE_CHARS else text[: MAX_NOTE_CHARS - 1] + "…"


def _cut_graph(graph: Graph, notes: list[str]) -> dict[str, Any]:
    """The graph within its caps: changed nodes first, then tests, callers and
    routes, each in the planner's order; edges only between kept nodes."""
    nodes = graph.nodes
    if len(nodes) > MAX_GRAPH_NODES:
        ranked = sorted(range(len(nodes)), key=lambda i: (_NODE_PRIORITY[nodes[i].kind], i))
        keep = set(ranked[:MAX_GRAPH_NODES])
        nodes = [node for i, node in enumerate(nodes) if i in keep]
    ids = {node.id for node in nodes}
    edges = [edge for edge in graph.edges if edge.source in ids and edge.target in ids]
    dangling = len(graph.edges) - len(edges)
    if len(edges) > MAX_GRAPH_EDGES:
        edges = edges[:MAX_GRAPH_EDGES]
    if len(nodes) < len(graph.nodes) or len(edges) + dangling < len(graph.edges):
        notes.append(_note(
            f"graph: cut to {len(nodes)} of {len(graph.nodes)} nodes and {len(edges)} of "
            f"{len(graph.edges)} edges (at most {MAX_GRAPH_NODES} nodes and "
            f"{MAX_GRAPH_EDGES} edges)"
        ))
    elif dangling:
        notes.append(_note(
            f"graph: {dangling} edge{'' if dangling == 1 else 's'} naming no node dropped"
        ))
    return {
        "nodes": [node.model_dump(exclude_none=True) for node in nodes],
        "edges": [edge.model_dump(by_alias=True) for edge in edges],
    }


def _requirement_ids(validation: dict[str, Any], count: int, notes: list[str]) -> None:
    """Drop the requirement ids a test names that the plan does not list."""
    unknown: set[int] = set()
    for test in validation.get("tests") or []:
        ids = test.get("requirements")
        if ids is None:
            continue
        kept = list(dict.fromkeys(n for n in ids if n <= count))
        unknown.update(n for n in ids if n > count)
        test["requirements"] = kept
    if unknown:
        named = ", ".join(str(n) for n in sorted(unknown))
        notes.append(_note(
            f"validation: requirement ids {named} dropped: the plan lists {count} "
            f"requirement{'' if count == 1 else 's'}"
        ))


def _read(key: str, value: Any, requirement_count: int, notes: list[str]) -> Any:
    """One section checked: its normalised value, or None with a note."""
    try:
        if key == "purpose":
            return _Purpose(value=value).value
        if key == "why":
            return _Why(value=value).value
        if key == "changes":
            parsed = _Changes(value=value).value
            if len(parsed) > MAX_CHANGES:
                notes.append(_note(f"changes: cut to the first {MAX_CHANGES} of {len(parsed)}"))
            return [change.model_dump() for change in parsed[:MAX_CHANGES]]
        if key == "risk":
            return Risk.model_validate(value).model_dump(exclude_none=True)
        if key == "validation":
            found = Validation.model_validate(value).model_dump(exclude_none=True)
            _requirement_ids(found, requirement_count, notes)
            return found
        graph = Graph.model_validate(value)
        return _cut_graph(graph, notes)
    except ValidationError as exc:
        notes.append(_note(f"{key}: dropped: {_problems(exc)}"))
        return None


def _theirs(overlaps: Any, notes: list[str]) -> tuple[Any, dict[int, str]]:
    """The overlaps without `theirs`, and each valid `theirs` by index."""
    if not isinstance(overlaps, list):
        return overlaps, {}
    found: dict[int, str] = {}
    bare = []
    for index, item in enumerate(overlaps):
        if isinstance(item, Mapping) and "theirs" in item:
            value = item["theirs"]
            text = value.strip() if isinstance(value, str) else None
            if text and len(text) <= MAX_THEIRS_CHARS and "\n" not in text and "\r" not in text:
                found[index] = text
            elif value is not None:
                notes.append(_note(
                    f"overlaps.{index}.theirs: dropped: one line of at most "
                    f"{MAX_THEIRS_CHARS} characters"
                ))
            item = {k: v for k, v in item.items() if k != "theirs"}
        bare.append(item)
    return bare, found


def split_sections(value: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """`value` without the sections, and what to put back on the parsed plan.

    The second is every section that passed, normalised, `section_notes` when
    any note was made or carried, and `_theirs`, each overlap's valid
    `theirs` by its index (`merge_sections` places them).
    """
    rest = {k: v for k, v in value.items() if k not in SECTION_KEYS and k != NOTES_KEY}
    notes: list[str] = []
    carried = value.get(NOTES_KEY)
    if isinstance(carried, list):
        notes += [_note(n) for n in carried if isinstance(n, str) and n.strip()]
    requirements = value.get("requirements")
    count = len(requirements) if isinstance(requirements, list) else 0
    out: dict[str, Any] = {}
    for key in SECTION_KEYS:
        if value.get(key) is None:
            continue
        parsed = _read(key, value[key], count, notes)
        if parsed is not None:
            out[key] = parsed
    if "overlaps" in rest:
        rest["overlaps"], theirs = _theirs(rest["overlaps"], notes)
        if theirs:
            out["_theirs"] = theirs
    notes = list(dict.fromkeys(notes))[:MAX_NOTES]
    if notes:
        out[NOTES_KEY] = notes
    return rest, out


def merge_sections(plan: dict[str, Any], sections: Mapping[str, Any]) -> dict[str, Any]:
    """`plan` (as `PlanSpec` dumped it) with the sections `split_sections` kept."""
    theirs = sections.get("_theirs") or {}
    overlaps = plan.get("overlaps")
    if theirs and isinstance(overlaps, list):
        for index, text in theirs.items():
            if index < len(overlaps):
                overlaps[index]["theirs"] = text
    for key in (*SECTION_KEYS, NOTES_KEY):
        if key in sections:
            plan[key] = sections[key]
    return plan


def size_line(plan: Mapping[str, Any]) -> str:
    """`2 steps · 5 files (2 added, 2 modified, 1 test)`: how big the change is, from
    its `changes` when it has them, else from its steps' files."""
    steps = len(plan.get("steps") or [])
    text = f"{steps} step{'' if steps == 1 else 's'}"
    changes = plan.get("changes")
    if changes:
        files = len({change["file"] for change in changes})
        counts: dict[str, int] = {}
        for change in changes:
            counts[change["kind"]] = counts.get(change["kind"], 0) + 1
        words = {"add": "added", "modify": "modified", "delete": "deleted", "test": "test"}
        parts = [f"{counts[k]} {words[k]}{'s' if k == 'test' and counts[k] > 1 else ''}"
                 for k in words if counts.get(k)]
        return text + f" · {files} file{'' if files == 1 else 's'} (" + ", ".join(parts) + ")"
    files = {path for step in plan.get("steps") or [] for path in step.get("files") or []}
    if files:
        text += f" · {len(files)} file{'' if len(files) == 1 else 's'}"
    return text
