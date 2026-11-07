"""CONTEXT.md and pack.json (§4.4), and writing them where no link can redirect them.

CONTEXT.md, in this order:
  1. the header: task type, freshness, Jev (only when it is on), the seeds,
     the summary;
  1a. THE GRAPH'S ANSWER for the brief (`answer.brief_answer`, BGD's pick of
     6 November 2026): the files swarm-graph `search` ranks most relevant,
     each with its reason, or why there are none -- sized last, to what the
     bound leaves after every never-cut row and the checklist, so it leads
     the file and still displaces nothing below;
  2. NEVER CUT: parallel work on this territory, and the paths this step cannot
     push -- before anything ranked, so the budget can never reach them;
  3. the checklist for the task type, and where to answer it;
  4. the ranked rows, as many as fit under `BUDGET_BYTES`;
  5. PAGED OUT: per kind, how many rows did not fit and which swarm-graph tool
     pages them. Never a silent truncation (graphmcp's rule).

pack.json carries every row, shown or not, so nothing is ever lost to the cap.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .answer import GraphAnswer
from .checklists import SELF_CHECK_FILE, checklist
from .layout import BUDGET_BYTES
from .rank import ordered
from .retrieve import Candidate

#: The swarm-graph tool that pages each kind past the budget (§4.4's table).
PAGED_BY = {"symbol": "context", "caller": "impact", "callee": "context",
            "test": "tests_for", "guard": "rules_for", "rule": "rules_for",
            "doc": "rules_for", "similar_pr": "similar_changes"}
#: The never-cut section may use at most this much of the budget; past it, its
#: rows are shown by reference only, and past that counted with pack.json named.
ALWAYS_MAX_BYTES = 10 * 1024
#: Room kept for the paged-out section and the closing line.
TAIL_RESERVE_BYTES = 768
_FIELD_CHARS = 200

UNKNOWN_IN_FLIGHT = (
    "UNKNOWN: the in-flight view was not staged for this step, so parallel work is not "
    "known to be absent. Check the open pull requests on these files before you edit them."
)


def _clip(text: Any, limit: int = _FIELD_CHARS) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _row_line(cand: Candidate) -> str:
    bits = [f"- **{cand.kind}** `{_clip(cand.ref)}`: {_clip(cand.why)}"]
    if cand.hops is not None and cand.kind in ("caller", "callee", "test"):
        bits.append(f"(hops {cand.hops})")
    if cand.extra.get("signature"):
        bits.append(f"`{_clip(cand.extra['signature'], 80)}`")
    if cand.stale:
        bits.append(f"STALE ({cand.stale})")
    return " ".join(bits)


def _size(lines: list[str]) -> int:
    return len("\n".join(lines).encode("utf-8")) + 1


def _always(rows: list[Candidate], in_flight: str | int) -> list[str]:
    flight = [c for c in rows if c.kind == "in_flight"]
    blocked = [c for c in rows if c.kind == "cannot_push"]
    lines = ["## Never cut", "", "### Parallel work on this territory"]
    if in_flight == "UNKNOWN":
        lines.append(UNKNOWN_IN_FLIGHT)
    elif not flight:
        lines.append("None of the in-flight work staged for this step touches its territory.")
    full = [f"- `{_clip(c.ref, 120)}`: {_clip(c.why)}" for c in flight]
    short = [f"- `{_clip(c.ref, 120)}`" for c in flight]
    walls = ["", "### Paths this step cannot push, or that a human must approve"]
    walls += [f"- `{_clip(c.ref)}`: {_clip(c.why)}" for c in blocked]
    shown = lines + full + walls
    if _size(shown) <= ALWAYS_MAX_BYTES:
        return shown
    shown = lines + short + walls
    if _size(shown) <= ALWAYS_MAX_BYTES:
        return shown
    # Pathological: more parallel work than the section can name. The first
    # ones are named, and the rest are counted with where they all are.
    kept: list[str] = []
    for line in short:
        if _size(lines + kept + [line, "x" * 120] + walls) > ALWAYS_MAX_BYTES:
            break
        kept.append(line)
    more = len(short) - len(kept)
    return lines + kept + [f"- … and {more} more in pack.json beside this file"] + walls


GRAPH_ANSWER_HEADING = "## The graph's answer for this brief"
GRAPH_ANSWER_LEAD = ("Files swarm-graph `search` ranks most relevant to the brief, best "
                     "first: open these before searching widely.")


def _graph_lines(answer: GraphAnswer | None, room: int) -> list[str]:
    """The answer's section within `room` bytes; sets `answer.shown`."""
    if answer is None:
        return []
    answer.shown = 0
    head = [GRAPH_ANSWER_HEADING, ""]
    if not answer.files:
        said = head + [f"None: {_clip(answer.reason or 'the graph gave no answer')}.", ""]
        return said if _size(said) <= room else []
    rows = [f"- `{_clip(f['path'], 160)}`: {_clip(f['why'])}" for f in answer.files]
    kept: list[str] = []
    for row in rows:
        more = len(rows) - len(kept) - 1
        tail = [f"- … and {more} more in pack.json beside this file"] if more else []
        if _size(head + [GRAPH_ANSWER_LEAD] + kept + [row] + tail + [""]) > room:
            break
        kept.append(row)
    more = len(rows) - len(kept)
    tail = [f"- … and {more} more in pack.json beside this file"] if more else []
    said = head + [GRAPH_ANSWER_LEAD] + kept + tail + [""]
    if _size(said) > room:
        # Pathological: not even one file fits beside the never-cut rows.
        said = head + [f"{len(rows)} files in pack.json beside this file.", ""]
        return said if _size(said) <= room else []
    answer.shown = len(kept)
    return said


def render_markdown(header: dict[str, Any], rows: list[Candidate], ranked: list[Candidate],
                    in_flight: str | int, *, answer: GraphAnswer | None = None,
                    ) -> tuple[str, set[tuple[str, str]], dict[str, int]]:
    """(the text, the keys of the ranked rows shown, paged-out counts by kind).

    `answer`, when given, leads after the header (`_graph_lines`); how many
    of its files fit is set on it as `shown`."""
    fresh = header["freshness"]
    lines = [f"# Repository context: {header['task_type']} step", ""]
    stale_note = " · rows marked STALE changed after the index" if any(
        c.stale for c in rows) else ""
    lines.append(
        f"Index `{str(fresh.get('index_sha') or '')[:12]}` · base "
        f"`{str(fresh.get('base_sha') or 'unknown')[:12]}` · behind_by "
        f"{fresh.get('behind_by') if fresh.get('behind_by') is not None else 'unknown'} · "
        f"changed since the index: {fresh.get('changed_since_index')}{stale_note}")
    # Jev only when it is on for this step (owner's pick, 2026-11-03): an off
    # line tells the agent nothing it can act on. pack.json keeps the state
    # and why either way.
    jev = header["jev"]
    if jev.get("state") == "on":
        lines.append(f"Jev: on ({_clip(jev['reason'], 160)})")
    seeds = header["seeds"]
    named = [*seeds.get("files", []), *seeds.get("tests", []), *seeds.get("symbols", [])]
    lines.append("Seeds: " + (", ".join(f"`{_clip(s, 120)}`" for s in named[:12])
                              or "none the graph knows") +
                 (f" ({seeds['from']})" if seeds.get("from") else ""))
    if seeds.get("unknown"):
        lines.append("Not in the index (new since it, or misspelt): " + ", ".join(
            f"`{_clip(s, 120)}`" for s in seeds["unknown"][:8]))
    lines.append("")
    rest = _always(rows, in_flight)
    rest += ["", f"## Checklist: {header['task_type']} step", ""]
    rest += [f"- [ ] {item}" for item in checklist(header["task_type"])]
    rest += ["", f"Answer it in `$SWARM_ARTIFACTS_DIR/{SELF_CHECK_FILE}`: one line per item, "
                 "done or not done, with the evidence.", ""]
    # The graph's answer leads, in what the never-cut rows, the checklist, the
    # ranked heading and the tail leave: it can shorten, they cannot.
    fixed = _size(lines + rest + ["## Ranked context (000 of 000 rows)", ""])
    lines += _graph_lines(answer, BUDGET_BYTES - TAIL_RESERVE_BYTES - fixed)
    lines += rest
    head_size = _size(lines)
    total = {}
    for cand in rows:
        if cand.kind in PAGED_BY:
            total[cand.kind] = total.get(cand.kind, 0) + 1
    budget = BUDGET_BYTES - TAIL_RESERVE_BYTES
    shown: list[Candidate] = []
    size = head_size + _size(["## Ranked context (000 of 000 rows)", ""])
    for cand in ranked:
        line = _row_line(cand)
        piece = len(line.encode("utf-8")) + 1
        if size + piece > budget:
            continue
        shown.append(cand)
        size += piece
    body = [_row_line(c) for c in ordered(shown)]
    lines += [f"## Ranked context ({len(shown)} of {sum(total.values())} rows)", ""] + body
    counts: dict[str, int] = {}
    for cand in shown:
        counts[cand.kind] = counts.get(cand.kind, 0) + 1
    paged = {kind: total[kind] - counts.get(kind, 0) for kind in sorted(total)
             if total[kind] - counts.get(kind, 0) > 0}
    lines += ["", "## Paged out", ""]
    if paged:
        lines += [f"- {n} more {kind} rows: swarm-graph `{PAGED_BY[kind]}`"
                  for kind, n in paged.items()]
    else:
        lines.append("Nothing: every row is shown above.")
    lines.append("Every row, shown or not, is in pack.json beside this file.")
    text = "\n".join(lines) + "\n"
    if len(text.encode("utf-8")) > BUDGET_BYTES:  # the reserve is sized so this cannot
        raise AssertionError(f"CONTEXT.md is {len(text.encode())} bytes")
    return text, {c.key() for c in shown}, paged


def write_atomically(path: Path, data: bytes) -> None:
    """Write `path` by rename: a link planted at its name is replaced, never followed."""
    directory = path.parent
    if directory.is_symlink():
        raise OSError(f"{directory} is a link; the pack is not written through it")
    directory.mkdir(parents=True, exist_ok=True)
    handle, temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=directory)
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(data)
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def dumps(pack: dict[str, Any]) -> bytes:
    return (json.dumps(pack, indent=1, sort_keys=True, ensure_ascii=False) + "\n").encode()
