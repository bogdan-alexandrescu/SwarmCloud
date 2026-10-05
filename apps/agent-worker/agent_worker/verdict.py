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

WHY THE GATE IS IN THE WORKER AND NOT THE SCHEDULER. The gated step is the
one that publishes, so it has to run whatever the verdict: on NOT_YET its
agent fixes the findings and then it publishes, on MERGE it publishes without
an agent. Skipping it in the scheduler would skip the pull request. The
scheduler also reads no artifact, and would need a GCS read on its drain path
to learn the verdict. The worker already has the file on disk, staged.

AN UNREADABLE VERDICT FAILS THE ATTEMPT. It is not read as MERGE, which would
publish unreviewed work, and not as NOT_YET, which would run a fixer against
findings that are not there. Every refusal is `InputUnavailable`, so the
agent never starts and the task ends with `INPUTS_UNAVAILABLE`, naming the
upstream task and the file.
"""

from __future__ import annotations

import json
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


@dataclass(frozen=True)
class Verdict:
    """A verdict file, read and bounded."""

    verdict: str
    findings: tuple[str, ...] = field(default=())
    findings_dropped: int = 0
    minors: tuple[MinorFinding, ...] = field(default=())
    minors_dropped: int = 0


def _normalise(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip().upper()
    return text if text in REVIEW_VERDICTS else None


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
    texts = [t for t in (_finding_text(item) for item in items) if t]
    minors, minors_dropped = minor_findings(items)
    return Verdict(
        verdict=verdict,
        findings=tuple(texts[:MAX_FINDINGS]),
        findings_dropped=max(len(texts) - MAX_FINDINGS, 0),
        minors=minors,
        minors_dropped=minors_dropped,
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
