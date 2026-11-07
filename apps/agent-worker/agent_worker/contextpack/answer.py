"""The graph's answer for the step's brief: the files it ranks most relevant,
each with the one-line reason the graph gives (the owner's pick, 6 November 2026,
after the 48-hour code intelligence report; part of #2688).

WHY. Only 62 of 903 steps that had the graph ever called it, but when a step
did, the graph named a file the step then changed in 35 of 56 answers. So the
pack no longer waits to be asked: it leads with what `search` -- the
swarm-graph tool an agent would call first -- answers for the brief, grouped
by file, before the local scores (`render.render_markdown` places and sizes
it; never past the pack's bound, never over the never-cut rows).

THE REASON is the graph's own: the symbols in the file that matched the
brief's terms, their kind and line, and the file's summed BM25 score. Nothing
is inferred past what the index holds.

NOT DELIVERED is said, never filled: an empty brief, a graph without a term
index, a search the pack's deadline cut short, or a brief none of whose terms
the graph knows each leave the section with no file and its reason, and
pack.json records `carried: false` with that reason, so the next report can
tell an answer the agent ignored from one it never got.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..graphmcp.snapshot import Snapshot, SnapshotError, name_of, path_of
from .seeds import BRIEF_CHARS

#: Files the section names at most; the rest are in pack.json. Eight lines of
#: about 200 bytes: an eighth of the 16 KiB bound at most, less when the
#: never-cut rows need it (`render`).
GRAPH_FILES = 8
#: Symbols the search returns before they are grouped by file.
SEARCH_SYMBOLS = 40
#: Symbols named in a file's reason; the rest are counted.
REASON_SYMBOLS = 3


@dataclass
class GraphAnswer:
    """The ranked files (`path`, `score`, `why`), or none and `reason`.
    `shown` is how many of them CONTEXT.md carried, set by the renderer."""

    files: list[dict[str, Any]] = field(default_factory=list)
    reason: str | None = None
    shown: int = 0

    def record(self) -> dict[str, Any]:
        """pack.json's `graph_answer`: carried or not, how many, and why not."""
        return {"carried": self.shown > 0, "files": list(self.files), "shown": self.shown,
                "reason": self.reason}


def _why(symbols: list[tuple[str, float]], snap: Snapshot, score: float) -> str:
    named = []
    for symbol, _score in symbols[:REASON_SYMBOLS]:
        row = snap.symbol(symbol) or {}
        where = ", ".join(str(v) for v in (row.get("kind"),
                                           f"line {row['start_line']}"
                                           if row.get("start_line") else None) if v)
        named.append(f"`{name_of(symbol)}`" + (f" ({where})" if where else ""))
    more = len(symbols) - REASON_SYMBOLS
    tail = f" and {more} more" if more > 0 else ""
    return f"defines {', '.join(named)}{tail} matching the brief; BM25 {score:.2f}"


def brief_answer(snap: Snapshot, brief: str) -> GraphAnswer:
    """`search` over the brief, grouped by file, best first. Never raises: a
    graph that cannot answer is a `GraphAnswer` with no file and its reason."""
    if not brief.strip():
        return GraphAnswer(reason="the step's brief is empty")
    try:
        hits = snap.search(brief[:BRIEF_CHARS], SEARCH_SYMBOLS)
    except SnapshotError as exc:
        flat = " ".join(str(exc).split())[:200]
        return GraphAnswer(reason=f"the graph's search did not answer ({flat})")
    if not hits:
        return GraphAnswer(reason="the graph's search matched none of the brief's terms")
    by_file: dict[str, list[tuple[str, float]]] = {}
    for symbol, score in hits:
        by_file.setdefault(path_of(symbol), []).append((symbol, float(score)))
    ranked = sorted(by_file.items(),
                    key=lambda item: (-round(sum(s for _n, s in item[1]), 4), item[0]))
    files = []
    for path, symbols in ranked[:GRAPH_FILES]:
        score = round(sum(s for _n, s in symbols), 4)
        files.append({"path": path, "score": score, "why": _why(symbols, snap, score)})
    return GraphAnswer(files=files)
