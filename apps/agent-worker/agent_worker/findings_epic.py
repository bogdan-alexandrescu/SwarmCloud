"""A review's MINOR findings, filed on the tenant's wave epic (#638).

WHY. CLAUDE.md's rule is that minors and out-of-territory findings go to a
wave epic as one comment per finding. Until this existed that rule depended on
an operator copying them out of `verdict.json` by hand, and the 2026-10-05
history analysis found 530 minors across 122 reviews that never left the file.

WHO FILES THEM. The step gated on the review's verdict (`when`, #264): it is
the one step that reads `verdict.json`, it runs whatever the verdict (on MERGE
with no agent), and its worker already holds the tenant's git token for the
publish. The comments are posted by the WORKER after the agent has ended,
never by the agent, with the tenant's own `-git` token (invariant 9).

WHERE. The issue named by `dispatch.findings_epic`, an issue number in the
step's own repository. swarm-api copies it from the tenant's `findings_epic`
setting into the gated step's signed dispatch block at submission, so no
agent of the tenant can point the worker at another issue. Absent, nothing is
posted and the step's `result_summary.findings_epic` says so.

WHAT. Each finding the review marked `"severity": "minor"`
(`verdict.minor_findings`), as ONE comment in the epic shape:

    - [ ] **<the defect>** · `<file>` `<call site>` · found by review `<task>` ...

Blockers and majors are not filed: they are the fix step's.

ONCE. Every comment already on the epic is read first, and a finding whose
(text, file, call site) is already there -- filed by an earlier run, or by a
person in the same shape -- is not posted again. A list that could not be read
whole files nothing: a partial read taken as whole is a duplicate.

NOTHING HERE FAILS THE STEP. The step's own work (its pull request) is done
by the time this runs; a forge that refuses or is down is recorded in the
result, and the next run's dedup files what this one could not.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Mapping

from .verdict import MinorFinding

#: The signed dispatch key, and the `result_summary` key the outcome is under.
EPIC_FIELD = "findings_epic"
SUMMARY_KEY = "findings_epic"

#: U+200D, which `lifecycle._neutralise_mentions` inserts; ignored by the key.
_ZWJ = "‍"

_LINE = re.compile(r"^\s*[-*]\s+\[[ xX]\]\s+\*\*(?P<text>.+?)\*\*(?P<rest>.*)$")
_TICKED = re.compile(r"`([^`]*)`")


def epic_from_dispatch(block: Any) -> int | None:
    """The epic's issue number, or None when none is configured.

    A value that is present and not a positive issue number raises
    `ValueError`, so the result can say it was malformed rather than unset.
    """
    raw = block.get(EPIC_FIELD) if isinstance(block, Mapping) else None
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        raise ValueError(
            f"this step's dispatch.{EPIC_FIELD} is not an issue number, so no minor "
            "finding was posted"
        )
    return raw


def _norm(text: str) -> str:
    return " ".join(text.replace(_ZWJ, "").replace("\\", "").split()).casefold()


def finding_key(text: str, file: str, call_site: str) -> tuple[str, str, str]:
    """What makes two findings the same one: (text, file, call site), compared
    without case, runs of spaces, escapes or the mention break."""
    return (_norm(text), _norm(file), _norm(call_site))


def key_of(finding: MinorFinding) -> tuple[str, str, str]:
    """The key of `finding` as `render_comment` writes it."""
    return finding_key(_bold(finding.text), _ticked(finding.file), _ticked(finding.call_site))


def keys_in(body: str) -> set[tuple[str, str, str]]:
    """The finding keys of every epic-shaped line in a comment's body."""
    keys = set()
    for line in body.splitlines():
        match = _LINE.match(line)
        if match is None:
            continue
        rest = match.group("rest").split("·")
        where = _TICKED.findall(rest[1]) if len(rest) > 1 else []
        keys.add(finding_key(
            match.group("text"),
            where[0] if where else "",
            where[1] if len(where) > 1 else "",
        ))
    return keys


def _bold(text: str) -> str:
    # A `**` in the agent's text would end the bold early and change the key
    # read back; escaped, it renders as two asterisks.
    return " ".join(text.split()).replace("*", "\\*")


def _ticked(text: str) -> str:
    return " ".join(text.split()).replace("`", "'")


def render_comment(finding: MinorFinding, *, found_by: str, clean: Callable[[str], str]) -> str:
    """The one line a finding is filed as. `clean` scrubs registered secrets and
    breaks mentions: the text is an agent's, posted on a page others read."""
    file = _ticked(finding.file)
    call_site = _ticked(finding.call_site)
    if file:
        where = f"`{file}`" + (f" `{call_site}`" if call_site else " (no call site named)")
    else:
        where = "(no file named)"
    how = found_by + (f"; {' '.join(finding.evidence.split())}" if finding.evidence else "")
    return clean(f"- [ ] **{_bold(finding.text)}** · {where} · {how}")


def not_filed(epic: int | None, minors: int, reason: str, **extra: Any) -> dict[str, Any]:
    return {"epic": epic, "minors": minors, "filed": [], "already_filed": 0,
            "not_filed": reason, **extra}


def file_minors(
    *,
    minors: tuple[MinorFinding, ...],
    epic: int,
    ref: Any,
    list_comments: Callable[[int], list[str]],
    post_comment: Callable[[int, str], int],
    found_by: str,
    clean: Callable[[str], str],
) -> dict[str, Any]:
    """Post each minor not already on the epic, once. What was filed, and why not.

    `list_comments` and `post_comment` hold the token; nothing here sees it,
    and every message recorded is passed through `clean`.
    """
    from .forge import ForgeError

    result: dict[str, Any] = {
        "epic": epic, "repository": ref.full_name, "minors": len(minors),
        "filed": [], "already_filed": 0,
    }
    try:
        seen: set[tuple[str, str, str]] = set()
        for body in list_comments(epic):
            seen |= keys_in(body)
    except ForgeError as exc:
        result["not_filed"] = clean(
            f"the epic's comments could not be read, so nothing was posted: {str(exc)[:300]}"
        )
        return result
    for finding in minors:
        line = render_comment(finding, found_by=found_by, clean=clean)
        key = next(iter(keys_in(line)), key_of(finding))
        if key in seen:
            result["already_filed"] += 1
            continue
        try:
            comment_id = post_comment(epic, line)
        except ForgeError as exc:
            result["not_filed"] = clean(
                f"posting stopped after {len(result['filed'])} of {len(minors)}: "
                f"{str(exc)[:300]}"
            )
            return result
        seen.add(key)
        result["filed"].append({
            "text": clean(finding.text), "file": clean(finding.file),
            "call_site": clean(finding.call_site), "comment_id": comment_id,
        })
    return result
