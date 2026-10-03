"""What an issue run writes on GitHub, as text: pure, no I/O (#454).

Three texts, each built from the run document alone:

  * THE PLAN COMMENT -- summary, mode, estimate, requirements, the overlaps
    the planner named (as links), each step with its files, tests and
    estimate, the plan's digest and revision, and who approved it or that
    approval is pending;
  * THE STATUS COMMENT -- one comment, edited in place as the run moves
    (planning -> awaiting approval -> running -> PR opened -> checks green ->
    merged, or failed), with the pull request and the console links;
  * THE KEYWORD BLOCK in the pull request's body -- `Closes #N` only when
    every planned requirement is delivered, `part of #N` naming what is left
    otherwise (CLAUDE.md, "`Closes #N` ONLY WHEN IT IS UNCONDITIONALLY TRUE").

EVERY TEXT SOMEONE ELSE AUTHORED IS NEUTRALISED (`neutral`). The plan is an
agent's output and the issue is anybody's, and the comment is posted with the
TENANT'S credential, so whatever it says, the tenant said:

  * masked by the API's redaction -- with the token as a known literal when
    the caller has it -- so a plan that quoted a credential does not publish it;
  * `<` and `>` escaped, so it cannot open an HTML comment that looks like our
    marker, a `<details>` that swallows the rest, or an image that pings a host;
  * every `@` that could mention someone broken with a zero-width joiner, by
    the worker's own rule (`agent_worker.lifecycle._neutralise_mentions`,
    restated here because swarm-api does not import the worker;
    tests/unit/control_plane/test_issue_writeback.py pins the two equal);
  * every closing keyword (`fixes #12`) turned into `refs`, so quoted text in
    a pull request body cannot close an issue the review did not confirm.

EVERY TEXT IS BOUNDED under GitHub's 65,536-character limit for a comment or
a body: each field by its own cap, and the whole by `MAX_BODY_CHARS`, cut at
the end so the marker and the headline -- the parts a retry and a reader need
-- always survive.

EACH COMMENT OPENS WITH A HIDDEN MARKER naming the run and the kind,
`<!-- swarmcloud-issue-run:<run_id>:<kind> -->`, on the first line. A retry
whose stored comment id was lost finds its own comment by it
(`GitHubWriter.find_comment`) and edits it instead of posting a second one.
"""

from __future__ import annotations

import hashlib
import re
from typing import Iterable, Sequence

from .codec import run_console_url, workflow_console_url
from .issueruns import AUTO_APPROVER, IssueRun, RunState
from .redaction import redact

#: GitHub refuses a comment or a body over 65,536 characters; this leaves room.
MAX_BODY_CHARS = 60_000
#: Per-field caps, so no one field can spend the whole body.
MAX_SUMMARY_CHARS = 4_000
MAX_LINE_CHARS = 500
MAX_PROMPT_CHARS = 3_000
MAX_ERROR_CHARS = 1_500

PLAN_KIND = "plan"
STATUS_KIND = "status"
KEYWORD_KIND = "keyword"

_RUN_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_TRUNCATED = "\n\n… (cut: GitHub's size limit for a comment)\n"


def marker(run_id: str, kind: str) -> str:
    """The hidden first line of one of this run's comments."""
    if not _RUN_ID.match(run_id):
        # A run id is `new_id("run")`; anything else would be spliced into HTML.
        raise ValueError("a run id is letters, digits, '_' and '-'")
    return f"<!-- swarmcloud-issue-run:{run_id}:{kind} -->"


def keyword_end(run_id: str) -> str:
    return f"<!-- /swarmcloud-issue-run:{run_id}:{KEYWORD_KIND} -->"


def body_digest(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# neutralising text someone else wrote
# --------------------------------------------------------------------------

#: An `@` that could start a mention, in every spelling GitHub decodes to one:
#: `@`, the fullwidth `＠`, `&commat;`, and decimal or hex character
#: references. Not after an ASCII word character, so `ops@example.com` is
#: left alone exactly as GitHub leaves it. The worker's `_MENTION_AT_RE`,
#: character for character (the test pins it).
_MENTION_AT_RE = re.compile(
    r"(?<![A-Za-z0-9_])"
    r"(@|\uff20|&commat;|&#0*64(?![0-9]);?|&#[xX]0*40(?![0-9A-Fa-f]);?)"
    r"(?=[A-Za-z0-9_&-])"
)

#: U+200D ZERO WIDTH JOINER: invisible, and not a character a mention can
#: contain, so `@<U+200D>octocat` pages no one. The worker's `MENTION_BREAK`.
MENTION_BREAK = "‍"

#: GitHub's closing keywords, followed by an issue reference: `#N`,
#: `owner/repo#N`, or an issue URL.
_CLOSING_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b(\s*:?\s+)"
    r"((?:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?#[0-9]+|https?://github\.com/[^\s/]+/[^\s/]+/issues/[0-9]+)",
    re.IGNORECASE,
)


def neutralise_mentions(text: str) -> str:
    return _MENTION_AT_RE.sub(lambda match: match.group(1) + MENTION_BREAK, text)


def neutralise_closing_keywords(text: str) -> str:
    """`fixes #12` -> `refs #12`: only the keyword block may close an issue."""
    return _CLOSING_RE.sub(lambda match: "refs" + match.group(1) + match.group(2), text)


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def neutral(text: str, limit: int, *, literals: Iterable[str] = (), one_line: bool = False) -> str:
    """Text someone else wrote, made safe to post with the tenant's credential."""
    masked = redact(str(text), extra=tuple(literals)).text
    masked = masked.replace("\x00", "")
    if one_line:
        masked = " ".join(masked.split())
    masked = masked.replace("<", "&lt;").replace(">", "&gt;")
    masked = neutralise_closing_keywords(neutralise_mentions(masked))
    return _cut(masked, limit)


def bounded(body: str, limit: int = MAX_BODY_CHARS) -> str:
    """`body` under `limit`, cut at the end so its marker and headline survive."""
    if len(body) <= limit:
        return body
    return body[: limit - len(_TRUNCATED)] + _TRUNCATED


def _person(email: str | None, *, literals: Iterable[str] = ()) -> str:
    """Who acted, as a public comment names them: the address's local part.

    The issue may be on a public repository, and a tenant member's full
    address there is a disclosure the run does not need to make; the console
    shows the whole address to the tenant.
    """
    if not email:
        return "someone"
    if email == AUTO_APPROVER:
        return "automatic approval (`plan_approval: auto`)"
    return neutral(email.split("@", 1)[0], 100, literals=literals, one_line=True)


def _iso(moment: object) -> str:
    text = moment.isoformat() if hasattr(moment, "isoformat") else str(moment or "")
    return text.split(".")[0].replace("+00:00", "") + (" UTC" if text else "")


def _console_lines(run: IssueRun, origin: str | None) -> list[str]:
    lines = []
    run_url = run_console_url(origin, run.id)
    if run_url:
        lines.append(f"- run: {run_url}")
    workflow_url = workflow_console_url(origin, run.workflow_id)
    if workflow_url:
        lines.append(f"- workflow: {workflow_url}")
    return lines


# --------------------------------------------------------------------------
# the plan comment
# --------------------------------------------------------------------------

def _approval_line(run: IssueRun, literals: Sequence[str]) -> str:
    if run.state == RunState.PLANNED:
        return (
            f"**Approval: pending.** Approve, edit or reject it in the console or with "
            f"`sc plan approve|edit|reject {run.id}`. Nothing runs, and no capacity is "
            "held, until it is approved."
        )
    if run.state == RunState.REJECTED:
        reason = (
            f": {neutral(run.rejection_reason, MAX_LINE_CHARS, literals=literals, one_line=True)}"
            if run.rejection_reason else ""
        )
        return f"**Rejected** by {_person(run.rejected_by, literals=literals)}{reason}"
    if run.approved_by:
        when = f" at {_iso(run.approved_at)}" if run.approved_at else ""
        return f"**Approved** by {_person(run.approved_by, literals=literals)}{when}."
    if run.state == RunState.CANCELLED:
        return "**Not approved:** the run was cancelled."
    return "**Approval: pending.**"


def _overlap_line(item: dict, literals: Sequence[str]) -> str:
    ref = str(item.get("ref") or "")
    kind = "pull request" if item.get("kind") == "pull_request" else "issue"
    owner_repo, _, number = ref.partition("#")
    # PlanOverlap's pattern admitted `ref`, so it is `owner/repo#N` and safe in
    # a URL; it is checked again here because a link is built from it.
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+", owner_repo) and number.isdigit():
        path = "pull" if kind == "pull request" else "issues"
        label = f"[{ref}](https://github.com/{owner_repo}/{path}/{number})"
    else:
        label = neutral(ref, 160, literals=literals, one_line=True)
    note = neutral(item.get("note") or "", MAX_LINE_CHARS, literals=literals, one_line=True)
    return f"- {label} ({kind}): {note}"


def render_plan_comment(
    run: IssueRun, *, console_origin: str | None = None, literals: Sequence[str] = ()
) -> str:
    """The plan as a comment on the issue. `run.plan` must be set."""
    plan = run.plan or {}
    out: list[str] = [
        marker(run.id, PLAN_KIND),
        f"### SwarmCloud plan for #{run.issue.number}",
        "",
        _approval_line(run, literals),
        "",
    ]
    facts = []
    if plan.get("mode"):
        facts.append(f"**Mode:** {'one agent' if plan['mode'] == 'single' else 'workflow'}")
    if plan.get("estimate"):
        facts.append(
            "**Estimate:** " + neutral(plan["estimate"], 200, literals=literals, one_line=True)
        )
    facts.append(f"**Revision:** {run.plan_revision}")
    facts.append(f"**Digest:** `{run.plan_digest or 'none'}`")
    out += [" · ".join(facts), ""]
    out += [neutral(plan.get("summary") or "", MAX_SUMMARY_CHARS, literals=literals), ""]

    requirements = plan.get("requirements") or []
    out.append("#### Requirements")
    if requirements:
        out += [
            f"{n}. {neutral(item, MAX_LINE_CHARS, literals=literals, one_line=True)}"
            for n, item in enumerate(requirements, start=1)
        ]
    else:
        out.append("_The plan lists none, so the pull request will say `part of`, not `Closes`._")
    out.append("")

    out.append("#### Overlaps with open work")
    overlaps = plan.get("overlaps")
    if overlaps:
        out += [_overlap_line(item, literals) for item in overlaps]
    elif overlaps is not None:
        out.append("None found in the repository's open issues and pull requests.")
    else:
        out.append("_The plan does not say._")
    out.append("")

    out.append("#### Steps")
    for n, step in enumerate(plan.get("steps") or [], start=1):
        title = neutral(step.get("title") or "", 200, literals=literals, one_line=True)
        line = f"{n}. **{title}** (`{step.get('step_id')}`)"
        if step.get("estimate"):
            line += " — " + neutral(step["estimate"], 100, literals=literals, one_line=True)
        out.append(line)
        if step.get("files"):
            out.append("   - files: " + ", ".join(
                f"`{neutral(path, 300, literals=literals, one_line=True).replace('`', '')}`"
                for path in step["files"]
            ))
        if step.get("tests"):
            out.append("   - tests, written first:")
            out += [
                f"     - {neutral(test, MAX_LINE_CHARS, literals=literals, one_line=True)}"
                for test in step["tests"]
            ]
        prompt = neutral(step.get("prompt") or "", MAX_PROMPT_CHARS, literals=literals)
        out += [
            "",
            "   <details><summary>Instructions</summary>",
            "",
            *("   " + line if line else "" for line in prompt.splitlines()),
            "",
            "   </details>",
            "",
        ]
    console = _console_lines(run, console_origin)
    if console:
        out += ["Console:", *console, ""]
    out.append(
        "_Written by SwarmCloud for its run "
        f"`{run.id}`, and edited in place when the plan changes._"
    )
    return bounded("\n".join(out) + "\n")


# --------------------------------------------------------------------------
# the status comment
# --------------------------------------------------------------------------

def status_phase(run: IssueRun) -> str:
    """Where the run is, in the words the issue's readers see."""
    pull = run.pull_request or {}
    checks = pull.get("checks")
    if run.state == RunState.PLANNING:
        return "planning"
    if run.state == RunState.PLANNED:
        return "plan awaiting approval"
    if run.state == RunState.APPROVED:
        return "approved, starting"
    if run.state == RunState.RUNNING:
        if not pull:
            return "running"
        if checks == "green":
            return "checks green"
        if checks == "red" or run.ci_fix_round:
            return "CI red, fixing"
        return "pull request opened"
    if run.state == RunState.DONE:
        if pull.get("merged"):
            return "merged"
        if checks == "green":
            return "checks green, ready to merge"
        return "done"
    if run.state == RunState.FAILED:
        return "failed"
    if run.state == RunState.REJECTED:
        return "plan rejected"
    return "cancelled"


def render_status_comment(
    run: IssueRun, *, console_origin: str | None = None, literals: Sequence[str] = ()
) -> str:
    """The ONE status comment. Carries no time, so an unchanged run renders unchanged."""
    out = [
        marker(run.id, STATUS_KIND),
        f"### SwarmCloud: {status_phase(run)}",
        "",
        f"- **Run:** `{run.id}` ({run.state.value})",
    ]
    pull = run.pull_request or {}
    if pull.get("url") and pull.get("number"):
        sha = str(pull.get("head_sha") or "")[:12]
        out.append(
            f"- **Pull request:** #{int(pull['number'])}"
            + (f" at `{neutral(sha, 12, one_line=True)}`" if sha else "")
        )
    if run.ci_fix_round:
        out.append(f"- **CI fix round:** {run.ci_fix_round} of {run.fix_rounds}")
    if run.state == RunState.PLANNED and run.plan_digest:
        out.append(f"- **Plan:** revision {run.plan_revision}, `{run.plan_digest}`")
    if run.error and run.state in (RunState.FAILED, RunState.CANCELLED):
        out += [
            "",
            "**Why:**",
            "",
            "```text",
            neutral(run.error, MAX_ERROR_CHARS, literals=literals).replace("```", "'''"),
            "```",
        ]
    console = _console_lines(run, console_origin)
    if console:
        out += ["", "Console:", *console]
    out += [
        "",
        "_SwarmCloud edits this comment in place as the run moves._",
    ]
    return bounded("\n".join(out) + "\n")


# --------------------------------------------------------------------------
# the pull request body's keyword block
# --------------------------------------------------------------------------

def keyword_block(
    run: IssueRun, *, closes: bool, unmet: Sequence[str] = (), literals: Sequence[str] = ()
) -> str:
    """`Closes #N`, or `part of #N` and what is left, between this run's markers.

    `closes` is the caller's finding that the review confirmed EVERY planned
    requirement; it is refused (rendered `part of`) when the plan lists no
    requirements to confirm, or when anything is named as unmet.
    """
    number = int(run.issue.number)
    requirements = (run.plan or {}).get("requirements") or []
    lines = [marker(run.id, KEYWORD_KIND)]
    if closes and requirements and not unmet:
        lines.append(f"Closes #{number}")
    else:
        lines.append(f"part of #{number}")
        if unmet:
            lines += ["", "Not yet delivered:"]
            lines += [
                f"- {neutral(item, MAX_LINE_CHARS, literals=literals, one_line=True)}"
                for item in list(unmet)[:60]
            ]
        elif not requirements:
            lines += ["", "The plan listed no requirements, so none could be confirmed."]
        else:
            lines += ["", "The review did not confirm every planned requirement."]
    lines.append(keyword_end(run.id))
    return "\n".join(lines)


def apply_keyword_block(body: str, run: IssueRun, block: str) -> str:
    """`body` with this run's block replaced (or appended), and no other closing keyword.

    The block is the one place that may close the issue: any closing keyword
    elsewhere in the body -- an agent's "Fixes #42" -- becomes `refs`, so a
    `part of` block cannot be contradicted by a line above it.
    """
    start, end = marker(run.id, KEYWORD_KIND), keyword_end(run.id)
    text = body or ""
    head, sep, rest = text.partition(start)
    if sep:
        _, sep_end, tail = rest.partition(end)
        text = head.rstrip() + (tail if sep_end else "")
    text = neutralise_closing_keywords(text).rstrip()
    joined = (text + "\n\n" if text else "") + block + "\n"
    if len(joined) > MAX_BODY_CHARS:
        room = MAX_BODY_CHARS - len(block) - len(_TRUNCATED) - 2
        joined = text[: max(room, 0)] + _TRUNCATED + "\n" + block + "\n"
    return joined
