"""The verdict gate: run a step's agent only when a review says so (#264).

The workflow shape this serves is implement, then review, then fix-if-needed:

    implement ──> review ──> fix (when review's verdict is NOT_YET)

The review stages the implementer's patch through `input_from` and writes a
verdict file into `$SWARM_ARTIFACTS_DIR`:

    {"verdict": "MERGE" | "NOT_YET", "findings": ["...", {"summary": "..."}]}

The fix stages that file through its own `input_from`, and swarm-api records
the gate in `task.metadata["dispatch"]["verdict_gate"]` as
`{"task_id": <review task>, "verdict_in": ["NOT_YET"]}`. This module reads the
two; `lifecycle.Worker` decides what to do with them.

A finding that is an object marked `"severity": "minor"` is also read out as a
`MinorFinding`, which the gated step files on the tenant's wave epic rather
than leaving in this file (#638, `agent_worker.findings_epic`).

A finding object may also say WHERE it is: `"file"` (repository-relative),
`"line"` (a positive int) and `"side"` (`"old"` or `"new"`, which half of the
patch the line number counts in). Those are kept, validated, as
`FindingLocation`s beside the text, so the console can pin the finding beside
its line (docs/design/diff-viewer.md, variant 3). The path is DISPLAYED, never
opened: validation is what keeps a hostile one (`../../etc/passwd`, an
absolute path, a control character) out of the record, and nothing here or
downstream reads a file by it. A location that fails validation is dropped and
its finding is kept, because a pin on the wrong line is worse than none and a
lost finding is worse than both. A verdict with no location reads exactly as
it did before, and its record carries no new key.

WHY THE GATE IS IN THE WORKER AND NOT THE SCHEDULER. The gated step is the
one that publishes, so it has to run whatever the verdict: on NOT_YET its
agent fixes the findings and then it publishes, on MERGE it publishes without
an agent. Skipping it in the scheduler would skip the pull request. The
scheduler also reads no artifact, and would need a GCS read on its drain path
to learn the verdict. The worker already has the file on disk, staged.

A SHUT GATE PASSES THE VERDICT ON (2026-10-10, part of #295). A gated step
whose agent does not run re-publishes the verdict file it staged, byte for
byte, under its `input_from` name, as its own artifact
(`lifecycle.Worker._republish_staged_verdict`). That is what lets a step be
gated AND be staged from: the re-review `on_merge_verdict` appends is gated on
the first review's NOT_YET, and on MERGE the merge step reads the first
review's verdict through it. A step that stages any OTHER file from a gated
step is still refused at submission: only the verdict is passed on.

JEV'S FINDING TRIAGE IS AN ANNOTATION, NEVER A VERDICT (lane CTXQ-JW, J3 of
docs/design/context-quality.md §5.2). When the review step's workspace opted
in to Jev, the review's post-step asks, per finding, "is this a real defect in
this diff, as stated?" and `annotate_findings` writes the answer onto the
finding as `p_real`. A finding under `FINDING_VERIFY_BELOW` also carries
`"triage": VERIFY_BEFORE_ACTING`, and its text reads with that mark, so it
reaches the fixer marked -- and is NEVER dropped. `verdict` is not read,
written or reordered by any of it: the gate, auto-merge and the requirements
decide exactly as they would without Jev.

THE OWNER QUESTIONS THAT HOLD THE MERGE (#2070, owner decision 2026-10-23,
option (c)). A review is given the workflow's owner questions
(`owner_questions`, `owner-questions.json`) and writes, beside `verdict`:

    "needs_owner":    [{"task_id", "step_id", "question_index", "question"}, ...]
    "open_questions": [the same shape]

`needs_owner` holds the unanswered questions that must hold the merge;
`open_questions` the others, which the merge lists on the pull request.
Both are read here as `OwnerQuestionRef`s, bounded. An entry that names no
task id or no question index is not dropped silently: `needs_owner_unreadable`
counts it, and the merge holds for it as for an unanswered one, because a
hold the merge cannot read is still a hold. Neither key changes the gate:
`verdict` alone decides who runs.

AN UNREADABLE VERDICT FAILS THE ATTEMPT. It is not read as MERGE, which would
publish unreviewed work, and not as NOT_YET, which would run a fixer against
findings that are not there. Every refusal is `InputUnavailable`, so the
agent never starts and the task ends with `INPUTS_UNAVAILABLE`, naming the
upstream task and the file.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import InputUnavailable

#: The verdicts a review writes, in the order every message lists them.
#: swarm-api's `validation.REVIEW_VERDICTS`, restated because the worker must
#: not import the control plane; tests/unit/worker/test_verdict_gate.py holds
#: the two equal.
REVIEW_VERDICTS = ("MERGE", "NOT_YET")

#: The most of a verdict file the worker reads. A verdict and a page of
#: findings is a few KiB; a file far larger than that is not a verdict, and
#: reading it whole into this process's memory is not free.
MAX_VERDICT_BYTES = 256 * 1024

#: How many findings are kept, and how long each may be. They are rendered on
#: the pull request and stored in `result_summary`, a Firestore field bounded
#: at 1 MiB with everything else in it.
MAX_FINDINGS = 50
MAX_FINDING_CHARS = 1000

#: J3 (§5.2): a finding Jev puts under this reaches the fixer marked, never
#: dropped. `judge.shapes.FINDING_VERIFY_BELOW`, restated so the verdict reader
#: imports nothing of the Jev client; test_jev_wiring.py holds the two equal.
FINDING_VERIFY_BELOW = 0.15
VERIFY_BEFORE_ACTING = "verify before acting"
#: The annotation's keys on a finding object.
P_REAL_KEY = "p_real"
TRIAGE_KEY = "triage"

_EXPECTED_SHAPE = (
    '{"verdict": "MERGE" | "NOT_YET", "findings": [...]}'
)


@dataclass(frozen=True)
class VerdictGate:
    """`metadata.dispatch.verdict_gate`, checked."""

    task_id: str
    verdict_in: tuple[str, ...]


#: The severity that sends a finding to the tenant's wave epic instead of the
#: fix step (#638). Blockers and majors are what the fix step fixes; a string
#: finding has no severity and is read as the convention's blocker.
SEVERITY_MINOR = "minor"

#: How many minors one verdict files, and how long a file or call site may be.
#: Each is one comment on a public issue, posted one request at a time.
MAX_MINORS = MAX_FINDINGS
MAX_WHERE_CHARS = 300


@dataclass(frozen=True)
class MinorFinding:
    """One finding the review marked `"severity": "minor"` (#638)."""

    text: str
    file: str = ""
    call_site: str = ""
    #: How the review found it, in its own words, when it said.
    evidence: str = ""


#: The two halves of a patch a finding's `line` can count in: the file before
#: the change (`old`, a removed line) and after it (`new`).
FINDING_SIDES = ("old", "new")

#: The longest path and the highest line a location may name. Both are far
#: past anything real; they exist so a hostile verdict cannot put a megabyte
#: of "path" into `result_summary`, which is bounded at 1 MiB in all.
MAX_LOCATION_PATH_CHARS = 1024
MAX_LOCATION_LINE = 10_000_000

#: What a staged input the review READ is called when it is a patch: the
#: digest of each is recorded beside the locations, so the console can tell a
#: pin made against one patch from the patch it is showing (lines drift after
#: a fix round). `swarm-work.patch` is the name every step's diff is published
#: under; `*.patch` keeps a hand-written review over another patch covered.
PATCH_SUFFIX = ".patch"

_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class FindingLocation:
    """Where the review said finding number `finding` is (variant 3).

    `finding` indexes `Verdict.findings`. `line` and `side` are both set or
    both None: a line number without the half of the patch it counts in
    cannot be placed, and a placement is never guessed.
    """

    finding: int
    file: str
    line: int | None = None
    side: str | None = None

    def as_dict(self) -> dict[str, Any]:
        record: dict[str, Any] = {"finding": self.finding, "file": self.file}
        if self.line is not None and self.side is not None:
            record["line"] = self.line
            record["side"] = self.side
        return record


#: The verdict keys the review writes its owner questions under
#: (`owner_questions.NEEDS_OWNER_KEY`, `OPEN_QUESTIONS_KEY`).
NEEDS_OWNER_KEY = "needs_owner"
OPEN_QUESTIONS_KEY = "open_questions"
#: How many of each are kept: `questions.MAX_QUESTIONS` per step, a few steps.
MAX_OWNER_QUESTIONS = 50
#: The longest question text kept for a pull request comment or a park record.
MAX_OWNER_QUESTION_CHARS = 500
_TASK_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


@dataclass(frozen=True)
class OwnerQuestionRef:
    """One owner question a verdict names: whose, and which."""

    task_id: str
    question_index: int
    step_id: str = ""
    question: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"task_id": self.task_id, "step_id": self.step_id,
                "question_index": self.question_index, "question": self.question}


def owner_question_refs(value: Any) -> tuple[tuple[OwnerQuestionRef, ...], int]:
    """`(refs, unreadable)` for a verdict's `needs_owner` or `open_questions`.

    Absent or null is none. A value that is not a list is one unreadable
    entry; so is every entry that is not an object with a task id and a
    non-negative integer `question_index`. A repeated question is kept once.
    Past `MAX_OWNER_QUESTIONS` the rest count as unreadable: the merge then
    holds, rather than merging past a question it did not look at.
    """
    if value is None:
        return (), 0
    if not isinstance(value, list):
        return (), 1
    refs: list[OwnerQuestionRef] = []
    unreadable = 0
    seen: set[tuple[str, int]] = set()
    for item in value:
        task_id = item.get("task_id") if isinstance(item, dict) else None
        index = item.get("question_index") if isinstance(item, dict) else None
        if (not isinstance(task_id, str) or not _TASK_ID.match(task_id.strip())
                or type(index) is not int or index < 0):
            unreadable += 1
            continue
        key = (task_id.strip(), index)
        if key in seen:
            continue
        seen.add(key)
        if len(refs) >= MAX_OWNER_QUESTIONS:
            unreadable += 1
            continue
        assert isinstance(item, dict)
        refs.append(OwnerQuestionRef(
            task_id=key[0], question_index=index,
            step_id=_one_line(item.get("step_id"), MAX_WHERE_CHARS),
            question=_one_line(item.get("question"), MAX_OWNER_QUESTION_CHARS),
        ))
    return tuple(refs), unreadable


@dataclass(frozen=True)
class Verdict:
    """A verdict file, read and bounded."""

    verdict: str
    findings: tuple[str, ...] = field(default=())
    findings_dropped: int = 0
    minors: tuple[MinorFinding, ...] = field(default=())
    minors_dropped: int = 0
    #: Only for kept findings that said where they are, in finding order.
    locations: tuple[FindingLocation, ...] = field(default=())
    #: Findings whose `file`, `line` or `side` was offered and not kept.
    locations_dropped: int = 0
    #: The unanswered owner questions the review said hold the merge (#2070).
    needs_owner: tuple[OwnerQuestionRef, ...] = field(default=())
    #: `needs_owner` entries that could not be read; the merge holds for them.
    needs_owner_unreadable: int = 0
    #: The other unanswered owner questions, listed on the pull request.
    open_questions: tuple[OwnerQuestionRef, ...] = field(default=())


def _normalise(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip().upper()
    return text if text in REVIEW_VERDICTS else None


def annotate_findings(document: dict[str, Any], p_real: dict[int, float]) -> dict[str, Any]:
    """`document` with J3's answers on its findings: `p_real` on each finding
    `p_real` names (by its index in `findings`), and the verify mark under
    `FINDING_VERIFY_BELOW`. A string finding becomes `{"summary": <it>, ...}`,
    which every reader reads as the same text. Nothing is dropped or
    reordered, and no other key -- `verdict` above all -- is touched."""
    raw = document.get("findings")
    if not isinstance(raw, list) or not p_real:
        return document
    findings: list[Any] = []
    for index, item in enumerate(raw):
        p = p_real.get(index)
        if p is None or not isinstance(item, (str, dict)):
            findings.append(item)
            continue
        out = dict(item) if isinstance(item, dict) else {"summary": item}
        out[P_REAL_KEY] = round(float(p), 4)
        if p < FINDING_VERIFY_BELOW:
            out[TRIAGE_KEY] = VERIFY_BEFORE_ACTING
        findings.append(out)
    return {**document, "findings": findings}


def gate_from_dispatch(block: Any) -> VerdictGate | None:
    """The gate in a dispatch block, None when there is none.

    A gate that is present and malformed is REFUSED rather than read as
    absent: absent means "run the agent", and a caller who asked for the agent
    to run only on NOT_YET would get it on every verdict. swarm-api validates
    what it writes, so a malformed gate means a newer control plane or a hand-
    edited document, and either way nobody has said what this step should do.
    """
    if not isinstance(block, dict) or "verdict_gate" not in block:
        return None
    raw = block.get("verdict_gate")
    problem = None
    task_id = raw.get("task_id") if isinstance(raw, dict) else None
    verdicts = raw.get("verdict_in") if isinstance(raw, dict) else None
    if not isinstance(raw, dict):
        problem = "it is not an object"
    elif not isinstance(task_id, str) or not task_id.strip():
        problem = "it names no upstream task"
    elif not isinstance(verdicts, list) or not verdicts:
        problem = "it names no verdicts"
    else:
        normalised = [_normalise(v) for v in verdicts]
        if any(v is None for v in normalised):
            problem = "it names a verdict outside " + ", ".join(REVIEW_VERDICTS)
        else:
            return VerdictGate(
                task_id=task_id.strip(),
                verdict_in=tuple(dict.fromkeys(v for v in normalised if v)),
            )
    raise InputUnavailable(
        f"this step's verdict gate cannot be read: {problem}. A gate is "
        '{"task_id": <upstream task>, "verdict_in": [' + ", ".join(
            f'"{v}"' for v in REVIEW_VERDICTS
        ) + "]}; the agent was not started and nothing was published"
    )


def _finding_text(item: Any) -> str | None:
    if isinstance(item, str):
        text = item
    elif isinstance(item, dict):
        text = next(
            (item[key] for key in ("summary", "title", "message") if isinstance(item.get(key), str)),
            None,
        )
        if text is None:
            text = json.dumps(item, sort_keys=True, default=str)
    else:
        return None
    text = " ".join(text.split())
    if not text:
        return None
    if isinstance(item, dict) and item.get(TRIAGE_KEY) == VERIFY_BEFORE_ACTING:
        text = f"[{VERIFY_BEFORE_ACTING}: Jev judged this unlikely to be real] {text}"
    if len(text) > MAX_FINDING_CHARS:
        text = text[: MAX_FINDING_CHARS - 1] + "…"
    return text


def _one_line(value: Any, bound: int) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    return text if len(text) <= bound else text[: bound - 1] + "…"


#: What a minor says its defect is, in the order read: the `summary` shape
#: first, then the review briefs' `problem` shape (#638).
_MINOR_TEXT_FIELDS = ("summary", "title", "message", "problem", "what")


def _first_text(item: dict[str, Any], names: tuple[str, ...]) -> str:
    """The first of `names` that holds a non-blank string, "" when none does."""
    for name in names:
        value = item.get(name)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def minor_findings(items: Any) -> tuple[tuple[MinorFinding, ...], int]:
    """The findings marked minor, bounded, and how many past the bound were left.

    Only an OBJECT whose `severity` is `minor` (any case) is one, and only
    when it says what the defect is (`summary`, `title`, `message`,
    `problem` or `what`). A string finding, or an object with any other
    severity or none, is the fix step's and never filed (#638).

    Both shapes are read. The review steps' briefs prescribe
    `{"severity", "file", "where", "problem", "fix"}`, which is the shape of
    the minors #638 counted, so `where` is the call site when `call_site` is
    absent, and the suggested `fix` trails the comment when the review gave no
    `evidence`. Reading only `summary`/`call_site` filed nothing on the
    verdicts that exist.
    """
    if not isinstance(items, list):
        items = [items] if items not in (None, "") else []
    minors: list[MinorFinding] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        severity = item.get("severity")
        if not isinstance(severity, str) or severity.strip().lower() != SEVERITY_MINOR:
            continue
        text = _one_line(_first_text(item, _MINOR_TEXT_FIELDS), MAX_FINDING_CHARS)
        if not text:
            continue
        evidence = _first_text(item, ("evidence",))
        if not evidence:
            fix = _first_text(item, ("fix",))
            evidence = f"suggested fix: {fix}" if fix else ""
        minors.append(MinorFinding(
            text=text,
            file=_one_line(item.get("file"), MAX_WHERE_CHARS),
            call_site=_one_line(_first_text(item, ("call_site", "where")), MAX_WHERE_CHARS),
            evidence=_one_line(evidence, MAX_WHERE_CHARS),
        ))
    return tuple(minors[:MAX_MINORS]), max(len(minors) - MAX_MINORS, 0)


def _repository_path(value: Any) -> str | None:
    """`value` as a repository-relative path to display, None when it is not one.

    Refused: anything but a non-blank string; one over the bound; a control
    character or a backslash anywhere; an absolute path (`/x`, `C:x`, `~/x`);
    and an empty, `.` or `..` segment, so the path names exactly one file
    inside the checkout and reads the same to every renderer.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > MAX_LOCATION_PATH_CHARS:
        return None
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text) or "\\" in text:
        return None
    if text.startswith(("/", "~")) or re.match(r"^[A-Za-z]:", text):
        return None
    if any(part in ("", ".", "..") for part in text.split("/")):
        return None
    return text


def _line_number(value: Any) -> int | None:
    # `type() is int`: a bool is an int to isinstance, and `true` is not line 1.
    if type(value) is int and 1 <= value <= MAX_LOCATION_LINE:
        return value
    return None


def _side(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    return text if text in FINDING_SIDES else None


def finding_location(item: Any, index: int) -> tuple[FindingLocation | None, bool]:
    """`(location, dropped)` for finding `index`: what is kept, and whether any was not.

    A string finding, or an object that names none of `file`, `line` and
    `side`, has no location and drops nothing. Otherwise the path must pass
    `_repository_path` or the whole location goes (a line in no file is no
    place); a valid path is kept with its line and side only when BOTH pass,
    and is kept alone, as "this file", when either does not.
    """
    if not isinstance(item, dict):
        return None, False
    offered = [key for key in ("file", "line", "side") if item.get(key) not in (None, "")]
    if not offered:
        return None, False
    path = _repository_path(item.get("file"))
    if path is None:
        return None, True
    line, side = _line_number(item.get("line")), _side(item.get("side"))
    if line is not None and side is not None:
        return FindingLocation(finding=index, file=path, line=line, side=side), False
    return FindingLocation(finding=index, file=path), offered != ["file"]


def reviewed_patches(review_task: Any) -> list[dict[str, str]]:
    """The patches the review step read, each with its digest, from its task document.

    Read from the review's `result_summary.staged_inputs`, which its own
    worker wrote with the sha256 of each file as staged
    (`inputs.StagedInput.sha256`): the worker's measurement, not the agent's
    claim, so a verdict cannot vouch for a patch it never read. An input
    without a well-formed digest (staged before digests were kept) is left
    out, and the console treats a finding with no digest to compare as not
    placed.
    """
    summary = review_task.get("result_summary") if isinstance(review_task, dict) else None
    staged = summary.get("staged_inputs") if isinstance(summary, dict) else None
    patches: list[dict[str, str]] = []
    for entry in staged if isinstance(staged, list) else ():
        if not isinstance(entry, dict):
            continue
        filename, digest = entry.get("filename"), entry.get("sha256")
        upstream = entry.get("task_id")
        if (
            isinstance(filename, str) and filename.endswith(PATCH_SUFFIX)
            and isinstance(digest, str) and _SHA256_HEX.fullmatch(digest)
            and isinstance(upstream, str)
        ):
            patches.append({"task_id": upstream, "filename": filename, "sha256": digest})
    return patches


def read_verdict(path: Path, *, task_id: str, filename: str) -> Verdict:
    """Read a staged verdict file, or refuse it by name."""
    where = f"the verdict file {filename!r} staged from task {task_id}"
    accepted = " or ".join(REVIEW_VERDICTS)

    def refuse(problem: str) -> InputUnavailable:
        return InputUnavailable(
            f"{where} {problem}. A verdict file is {_EXPECTED_SHAPE}, with "
            f"`verdict` {accepted}; the agent was not started and nothing was "
            "published, because an unreadable review is not a MERGE"
        )

    try:
        size = path.stat().st_size
    except OSError as exc:
        raise refuse(f"could not be read ({type(exc).__name__})") from exc
    if size > MAX_VERDICT_BYTES:
        raise refuse(f"is {size} bytes, over the {MAX_VERDICT_BYTES}-byte bound")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise refuse(f"is not JSON ({type(exc).__name__})") from exc
    if not isinstance(document, dict):
        raise refuse("is not a JSON object")
    verdict = _normalise(document.get("verdict"))
    if verdict is None:
        raise refuse(f"has `verdict` {document.get('verdict')!r}")

    raw_findings = document.get("findings")
    items = raw_findings if isinstance(raw_findings, list) else (
        [raw_findings] if raw_findings not in (None, "") else []
    )
    kept = [(item, t) for item, t in ((item, _finding_text(item)) for item in items) if t]
    texts = [t for _item, t in kept]
    locations: list[FindingLocation] = []
    locations_dropped = 0
    for index, (item, _text) in enumerate(kept[:MAX_FINDINGS]):
        location, dropped = finding_location(item, index)
        if location is not None:
            locations.append(location)
        locations_dropped += dropped
    minors, minors_dropped = minor_findings(items)
    needs_owner, needs_owner_unreadable = owner_question_refs(document.get(NEEDS_OWNER_KEY))
    open_questions, _ = owner_question_refs(document.get(OPEN_QUESTIONS_KEY))
    return Verdict(
        verdict=verdict,
        findings=tuple(texts[:MAX_FINDINGS]),
        findings_dropped=max(len(texts) - MAX_FINDINGS, 0),
        minors=minors,
        minors_dropped=minors_dropped,
        locations=tuple(locations),
        locations_dropped=locations_dropped,
        needs_owner=needs_owner,
        needs_owner_unreadable=needs_owner_unreadable,
        open_questions=open_questions,
    )


def pull_request_lines(record: dict[str, Any]) -> list[str]:
    """The verdict section of the pull request body.

    The findings are an agent's text, UNTRUSTED, on a page anyone with read
    access sees. They go inside one fenced block, with every run of three
    backticks broken, so no finding can close the fence and render as
    markdown: a link, an image, an @-mention.
    """
    lines = [
        "",
        f"Review verdict: **{record.get('verdict')}** "
        f"(from `{record.get('file')}`, task `{record.get('task_id')}`).",
    ]
    if record.get("agent_ran"):
        lines.append(
            "The verdict named this step, so the fix step ran and this branch "
            "carries its changes."
        )
    else:
        lines.append(
            "The verdict did not name this step, so the fix agent did not run: "
            "this branch is the reviewed work as it was."
        )
    findings = [str(f) for f in record.get("findings") or ()]
    if findings:
        lines += ["", "Findings, as the review wrote them:", "", "```text"]
        lines += [f"- {' '.join(f.split()).replace('```', 'ʼʼʼ')}" for f in findings]
        lines.append("```")
    dropped = int(record.get("findings_dropped") or 0)
    if dropped:
        lines.append(f"{dropped} more finding(s) are in the verdict file and not shown here.")
    return lines


# ---------------------------------------------------------------------------
# A UI lane's MERGE counts only from a review that saw the pages (#2184)
# ---------------------------------------------------------------------------
#
# BGD's decision, 2026-10-24 (~00:30Z). On 2026-10-23 ten mock-up lanes'
# review steps ran on `claude-code`, with no browser, and gave MERGE; two
# said "No browser was available here, so I reviewed the static HTML". The
# pages had clipped frames, crushed text and contradictory counts, found only
# when they were rendered (#2157, #2149). So a review of a patch that touches
# a UI path is UI-GATED: its MERGE counts only when the step ran on
# `RENDERED_REVIEW_PROFILE` and left `RENDERED_REVIEW_REPORT` and at least one
# image under `RENDERED_REVIEW_SHOTS/` in its artifacts. Otherwise the review's
# own worker rewrites its verdict.json to NOT_YET, with the reason, before the
# upload, so the step a `verdict_in: [NOT_YET]` gate holds runs (or the lane
# fails where everyone sees it). A patch that touches no UI path, and a
# NOT_YET, are left exactly as the review wrote them.
#
# WHAT THE PATCH TOUCHES IS THE WORKER'S MEASUREMENT, read before the agent
# starts: the `*.patch` files staged through `input_from`, and the `builds_on`
# step's `result_summary.git.files`. A file list that was cut at its bound
# with no UI path in what it kept cannot say the patch touches none, and is
# read as touching one: a review that could not be told is not one that saw.

#: The repository paths whose change makes a review UI-gated.
UI_PATH_PREFIXES = ("apps/swarm-ui/", "docs/web-ui/")
#: The one profile that renders pages (contract request 108's browser runner).
RENDERED_REVIEW_PROFILE = "claude-code-browser"
#: What a rendered review leaves in its artifacts directory.
RENDERED_REVIEW_REPORT = "review.md"
RENDERED_REVIEW_SHOTS = "shots"
#: What counts as an image under `shots/`, by suffix (any case).
SHOT_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif")
#: The words every downgraded verdict's reason starts with.
NOT_REVIEWED_RENDERED = "not reviewed rendered"
#: Where the rewrite is recorded, in verdict.json and `result_summary`.
RENDERED_REVIEW_KEY = "rendered_review"
#: How many of the UI paths found are kept in a record or a prompt.
MAX_UI_PATHS_SHOWN = 20
#: How deep and how wide `shots/` is looked through for one image.
MAX_SHOTS_WALKED = 2000

_PATCH_HEADER = re.compile(
    r"^(?:diff --git |--- |\+\+\+ |rename from |rename to |copy from |copy to )(.*)$"
)


def _patch_paths(header: str) -> list[str]:
    """The repository paths one patch header line names (`a/`, `b/` dropped)."""
    text = header.strip()
    if text.startswith('"') or " " not in text:
        parts = [text]
    else:
        # `diff --git a/x b/x`: split at the " b/" that starts the second path.
        cut = text.find(" b/")
        parts = [text[:cut], text[cut + 1:]] if cut > 0 else [text]
    paths = []
    for part in parts:
        part = part.strip().split("\t", 1)[0].strip('"')
        if part == "/dev/null" or not part:
            continue
        if part.startswith(("a/", "b/")):
            part = part[2:]
        paths.append(part)
    return paths


def is_ui_path(path: str) -> bool:
    """Whether a repository-relative path is one a rendered review must see."""
    return isinstance(path, str) and path.lstrip("/").startswith(UI_PATH_PREFIXES)


def ui_paths_in_patch(text: str) -> list[str]:
    """The UI paths a unified diff's headers name, in first-seen order."""
    found: dict[str, None] = {}
    for line in text.splitlines():
        match = _PATCH_HEADER.match(line)
        if match is None:
            continue
        for path in _patch_paths(match.group(1)):
            if is_ui_path(path):
                found.setdefault(path.lstrip("/"), None)
    return list(found)


def ui_paths_in_files(summary: Any) -> tuple[list[str], bool]:
    """`(UI paths, unknown)` from a step's `result_summary.git` record.

    `unknown` is True when its file list was cut at its bound and the part
    kept names no UI path: the rest may, and nobody measured it.
    """
    git = summary.get("git") if isinstance(summary, dict) else None
    rows = git.get("files") if isinstance(git, dict) else None
    found: dict[str, None] = {}
    for row in rows if isinstance(rows, list) else ():
        if not isinstance(row, dict):
            continue
        for key in ("path", "old_path"):
            value = row.get(key)
            if isinstance(value, str) and is_ui_path(value):
                found.setdefault(value.lstrip("/"), None)
    truncated = isinstance(git, dict) and git.get("files_truncated") is True
    return list(found), truncated and not found


def _has_shot(shots: Path) -> bool:
    """At least one non-empty regular image file under `shots/`, links not followed."""
    if shots.is_symlink() or not shots.is_dir():
        return False
    stack, walked = [shots], 0
    while stack and walked < MAX_SHOTS_WALKED:
        folder = stack.pop()
        try:
            entries = list(folder.iterdir())
        except OSError:
            continue
        for entry in entries:
            walked += 1
            if entry.is_symlink():
                continue
            if entry.is_dir():
                stack.append(entry)
            elif (entry.is_file() and entry.suffix.lower() in SHOT_SUFFIXES
                  and entry.stat().st_size > 0):
                return True
    return False


def rendered_review_missing(*, runner_profile: str, artifacts: Path) -> list[str]:
    """What a UI-gated review lacks for its MERGE to count; [] when nothing."""
    missing: list[str] = []
    if runner_profile != RENDERED_REVIEW_PROFILE:
        missing.append(f"ran on runner profile {runner_profile!r}, not {RENDERED_REVIEW_PROFILE!r}")
    report = artifacts / RENDERED_REVIEW_REPORT
    if report.is_symlink() or not report.is_file() or report.stat().st_size == 0:
        missing.append(f"no {RENDERED_REVIEW_REPORT} in its artifacts")
    if not _has_shot(artifacts / RENDERED_REVIEW_SHOTS):
        missing.append(f"no image under {RENDERED_REVIEW_SHOTS}/ in its artifacts")
    return missing


def rendered_review_guidance(ui_paths: list[str], runner_profile: str) -> str:
    """The review step's own instructions on the rule, appended to its prompt."""
    shown = ", ".join(ui_paths[:MAX_UI_PATHS_SHOWN]) or "a UI path its file list did not name"
    more = len(ui_paths) - MAX_UI_PATHS_SHOWN
    return (
        f"\n\nTHIS LANE CHANGES WHAT PEOPLE SEE ({shown}"
        + (f", and {more} more" if more > 0 else "")
        + "). A MERGE in verdict.json counts only from a review that saw the pages "
        f"rendered: this step must run on runner profile {RENDERED_REVIEW_PROFILE} and "
        f"leave {RENDERED_REVIEW_REPORT} and at least one screenshot under "
        f"{RENDERED_REVIEW_SHOTS}/ in the artifacts directory. This step runs on "
        f"{runner_profile}. Without all three the worker records your verdict as "
        f'NOT_YET with the reason "{NOT_REVIEWED_RENDERED}: <what was missing>", and '
        "the fix step runs. Never give MERGE on pages you only read as HTML."
    )


def gate_rendered_review(
    path: Path, *, ui_paths: list[str], runner_profile: str, artifacts: Path
) -> dict[str, Any] | None:
    """Rewrite a UI-gated MERGE in `path` to NOT_YET when the review did not render.

    None when nothing was looked at: no verdict file, or one that is not a
    readable verdict (`read_verdict` refuses that downstream, by name). Else
    the record kept in `result_summary`: `{"ui_paths", "verdict_given",
    "verdict", ...}`, with `missing` and `reason` when it was rewritten. The
    reason is also the verdict's first finding, so the fix step reads it.
    """
    if not path.is_file() or path.is_symlink():
        return None
    try:
        if path.stat().st_size > MAX_VERDICT_BYTES:
            return None
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    if not isinstance(document, dict):
        return None
    given = _normalise(document.get("verdict"))
    if given is None:
        return None
    record: dict[str, Any] = {
        "ui_paths": ui_paths[:MAX_UI_PATHS_SHOWN],
        "verdict_given": given,
        "verdict": given,
    }
    if given != "MERGE":
        return record
    missing = rendered_review_missing(runner_profile=runner_profile, artifacts=artifacts)
    if not missing:
        return record
    reason = f"{NOT_REVIEWED_RENDERED}: {'; '.join(missing)}"
    record.update({"verdict": "NOT_YET", "missing": missing, "reason": reason})
    findings = document.get("findings")
    findings = list(findings) if isinstance(findings, list) else (
        [findings] if findings not in (None, "") else []
    )
    document["verdict"] = "NOT_YET"
    document["findings"] = [reason, *findings]
    document[RENDERED_REVIEW_KEY] = record
    staging = path.with_name(f".{path.name}.rendered")
    staging.write_text(json.dumps(document, indent=2), encoding="utf-8")
    staging.replace(path)
    return record
