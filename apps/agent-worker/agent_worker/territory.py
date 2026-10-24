"""A lane's declared territory, and the changed paths that fall outside it.

Lane TERRITORY-WARN, part of #1468. OWNER DECISION, 2026-10-19: a lane whose
diff touches paths outside its declared territory is PUBLISHED WITH A LOUD
WARNING, not refused. The pull request still opens; its body starts with
`OUTSIDE_HEADER` and the list of paths, and it carries the label
`OUT_OF_TERRITORY_LABEL`. Merging still needs the review verdict and green CI.
A push onto a pull request already open (a CI-fix round) gets the label too,
and the header, in its continuation form, as ONE comment on the pull request;
the body section the push appends only points at it (CIFIX-TERRITORY-2).

WHERE THE TERRITORY COMES FROM. `inputs.territory` on the step, a list of
repo-relative paths and globs, written by whoever submitted the task and
stored in the task document -- the signed spec covers `input` -- so the
worker reads it from `task["input"]` and never from anything the agent can
write. A step that declares none is published exactly as before.

WHAT AN ENTRY MEANS. A POSIX path relative to the repository root, matched
against each changed path, together with everything below it, so `apps/x`
and `apps/x/` both hold `apps/x/a.py`. Inside a segment `*` is any run of
characters and `?` one character, neither crossing `/`; `[abc]`, `[a-z]` and
`[!abc]` are classes; `**` as a whole segment is any number of segments,
none included. Nothing else is special: there is no brace expansion, so a
`{` is refused rather than read literally by one tool and expanded by the
next.

THE SAME RULE IS IN swarm-api (`swarm_api.laneterritory`), which refuses a
malformed entry at submit with a 422. The two images copy only their own app
and `apps/common/`, and `apps/common/swarm_common/` is frozen, so the rule is
written twice; tests/unit/worker/test_territory_warn.py holds the two to the
same answers over one corpus.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Sequence

#: The key of `input` a step declares its territory under.
TERRITORY_INPUT_KEY = "territory"

#: The label a pull request whose diff leaves its territory carries. Listed in
#: .github/labels.yml.
OUT_OF_TERRITORY_LABEL = "out-of-territory"

#: The first line of such a pull request's body, exactly as the owner wrote it.
OUTSIDE_HEADER = "⚠ Outside this lane's territory:"

#: Bounds on a declaration. A brief names a handful of paths; 200 is room for
#: a generated one and still a body the API stores without thinking about it.
MAX_ENTRIES = 200
MAX_ENTRY_CHARS = 400

#: What a continuation's appended body section says instead of repeating the
#: header, which goes in a comment of its own (CIFIX-TERRITORY-2).
COMMENTED_NOTE = (
    "⚠ This push leaves the lane's territory: the out-of-territory warning was "
    "posted as a comment on this pull request."
)

#: The most paths the header lists by name; the rest are counted. GitHub caps
#: a body at 65,536 characters and the agent's own text has to fit after it.
MAX_LISTED = 100

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


def declared(task_input: Any) -> list[re.Pattern[str]] | None:
    """The territory a task's stored `input` declares, or None when it declares
    none. Raises TerritoryError for a declaration the API should have refused."""
    if not isinstance(task_input, dict) or TERRITORY_INPUT_KEY not in task_input:
        return None
    return parse(task_input[TERRITORY_INPUT_KEY])


def outside(paths: Iterable[str], territory: Sequence[re.Pattern[str]]) -> list[str]:
    """Each of `paths` that no entry of `territory` holds, sorted, once each."""
    return sorted({p for p in paths if not any(rule.fullmatch(p) for rule in territory)})


def warning_header(paths: Sequence[str], *, continuation: bool = False) -> str:
    """The block a pull request's body starts with when `paths` left the territory.

    `continuation` is the form for a push onto a pull request that was already
    open -- a CI-fix round, or a later attempt -- which the worker posts as a
    comment on it (CIFIX-TERRITORY-2, part of #1499). That push opened nothing,
    so "the pull request opened anyway" would be false there; the default form
    is word for word what a newly opened pull request has always said.
    """
    shown = list(paths[:MAX_LISTED])
    lines = [OUTSIDE_HEADER, ""]
    # A backtick would end the code span early; a path rarely has one.
    lines += ["- `" + p.replace("`", "'") + "`" for p in shown]
    if len(paths) > len(shown):
        lines.append(f"- ... and {len(paths) - len(shown)} more")
    which = "the path above matches" if len(paths) == 1 else f"the {len(paths)} paths above match"
    published = (
        "This push onto an open pull request was published anyway"
        if continuation
        else "The pull request opened anyway"
    )
    lines += [
        "",
        f"This step declared its territory in `inputs.{TERRITORY_INPUT_KEY}`, and {which} "
        f"none of its entries. {published} (owner decision, "
        "2026-10-19); merging still needs the review verdict and green CI.",
    ]
    return "\n".join(lines)
