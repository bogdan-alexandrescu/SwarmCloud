"""The shape of a step's `inputs.territory`, refused at submit when it is malformed.

Lane TERRITORY-WARN, part of #1468. A step may declare `inputs.territory`, a
list of repo-relative paths and globs naming the only files its lane may
edit. At publish the worker compares the diff's changed paths with it, and a
pull request whose diff leaves it opens with a warning header and the label
`out-of-territory` (owner decision, 2026-10-19: a warning, never a refusal --
`agent_worker.territory`). A malformed entry would match nothing the
submitter meant and flag every path, after the lane has spent its compute, so
it is refused HERE, as a 422 `invalid_input` naming the entry.

WHY IT IS NOT A CATALOGUE DECLARATION. Every other key of `input` is what
`RunnerProfile.inputs` declares in the frozen contract. `territory` is not
read by any runner; like `prompt`, it is the platform's, taken by every
profile, and `validation.validate_runner_input` takes it out before the
catalogue's check. Declaring it in the catalogue instead is a contract
request (see this lane's PR).

THE RULE IS THE WORKER'S, WRITTEN TWICE: the two images copy only their own
app and `apps/common/`, and `apps/common/swarm_common/` is frozen.
tests/unit/worker/test_territory_warn.py holds the two to the same answers.
What an entry means is documented in `agent_worker.territory`.
"""

from __future__ import annotations

import re
from typing import Any

#: The key of `input` a step declares its territory under.
TERRITORY_INPUT_KEY = "territory"

#: Bounds on a declaration; `agent_worker.territory` says why.
MAX_ENTRIES = 200
MAX_ENTRY_CHARS = 400

_FORBIDDEN_CHARS = re.compile(r"[\x00-\x1f\x7f\\{}]")


class TerritoryError(ValueError):
    """An entry, or the declaration, is not a territory. The message names the
    entry by position, and quotes it only when it is short and printable."""


def _named(index: int, entry: str) -> str:
    shown = entry if len(entry) <= 80 and not _FORBIDDEN_CHARS.search(entry) else None
    return f"territory[{index}]" + (f" {shown!r}" if shown is not None else "")


def _class_end(segment: str, start: int) -> int:
    """The index of the `]` closing the class opened at `start`, or -1."""
    i = start + 1
    if i < len(segment) and segment[i] == "!":
        i += 1
    if i < len(segment) and segment[i] == "]":
        # `[]]` and `[!]]`: a leading `]` is a member, as in fnmatch.
        i += 1
    while i < len(segment):
        if segment[i] == "]":
            return i
        i += 1
    return -1


def _segment_regex(segment: str, where: str) -> str:
    """One segment other than a lone `**`, as a regular expression."""
    if "**" in segment:
        raise TerritoryError(f"{where}: `**` must be a whole path segment")
    out: list[str] = []
    i = 0
    while i < len(segment):
        ch = segment[i]
        if ch == "*":
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        elif ch == "[":
            end = _class_end(segment, i)
            if end < 0:
                raise TerritoryError(f"{where}: a `[` class is never closed")
            body = segment[i + 1:end]
            negate = body.startswith("!")
            if negate:
                body = body[1:]
            if not body:
                raise TerritoryError(f"{where}: a `[]` class names no character")
            members = re.escape(body).replace("\\-", "-")
            if members.startswith("-") or members.endswith("-"):
                members = members.strip("-") + "\\-"
            try:
                re.compile(f"[{members}]")
            except re.error:
                raise TerritoryError(f"{where}: a `[` class has a backwards range") from None
            out.append(f"[^/{members}]" if negate else f"[{members}]")
            i = end
        elif ch == "]":
            raise TerritoryError(f"{where}: a `]` closes no class")
        else:
            out.append(re.escape(ch))
        i += 1
    return "".join(out)


def compile_entry(entry: Any, index: int = 0) -> re.Pattern[str]:
    """One entry, as the pattern a changed path is matched against whole.
    Raises TerritoryError for anything that is not a well-formed entry."""
    if not isinstance(entry, str):
        raise TerritoryError(f"territory[{index}] is a {type(entry).__name__}, not a path")
    where = _named(index, entry)
    if not entry.strip() or entry != entry.strip():
        raise TerritoryError(f"{where}: blank, or padded with whitespace")
    if len(entry) > MAX_ENTRY_CHARS:
        raise TerritoryError(f"{where}: longer than {MAX_ENTRY_CHARS} characters")
    if _FORBIDDEN_CHARS.search(entry):
        raise TerritoryError(
            f"{where}: a control character, a backslash or a brace; an entry is a "
            "POSIX path with `*`, `?`, `[...]` and `**` only"
        )
    if entry.startswith("/"):
        raise TerritoryError(f"{where}: absolute; an entry is relative to the repository root")
    body = entry[:-1] if entry.endswith("/") else entry
    segments = body.split("/")
    if any(s == "" for s in segments):
        raise TerritoryError(f"{where}: an empty path segment")
    if any(s in (".", "..") for s in segments):
        raise TerritoryError(f"{where}: a `.` or `..` segment")
    parts: list[str] = []
    for n, segment in enumerate(segments):
        last = n == len(segments) - 1
        if segment == "**":
            # Any number of whole segments, none included.
            parts.append("(?:.*)" if last else "(?:[^/]+/)*")
            continue
        parts.append(_segment_regex(segment, where) + ("" if last else "/"))
    pattern = "".join(parts)
    # The entry, and everything below it.
    return re.compile(f"(?:{pattern})(?:/.*)?", re.DOTALL)


def parse(raw: Any) -> list[re.Pattern[str]]:
    """A declaration -- a list of entries -- compiled. Raises TerritoryError."""
    if not isinstance(raw, list):
        raise TerritoryError(
            f"territory is a {type(raw).__name__}; it is a list of repo-relative paths and globs"
        )
    if not raw:
        raise TerritoryError("territory is empty; declare at least one path, or leave it out")
    if len(raw) > MAX_ENTRIES:
        raise TerritoryError(f"territory has {len(raw)} entries; at most {MAX_ENTRIES}")
    return [compile_entry(entry, index) for index, entry in enumerate(raw)]
