"""A MERGE verdict's pull request, opened by swarm-api without a worker (#748).

docs/workflows.md "What a MERGE verdict publishes". In an implement -> review
-> fix workflow under `integrate`, the fix step is the integrator and is
gated on `NOT_YET`. On MERGE its agent does not run, but until this module the
step still took a lease, started a Cloud Run execution and cloned the
repository, only to push a branch and open the one pull request. Measured on
2026-10-06 over three MERGE workflows: 116 s from the review's end to the
pull request, 57 s of it container start and 35 s egress wait and clone, and
one execution and one lease per workflow.

WHY SWARM-API CAN DO IT. With one contributor the integrator is cloned from
the implementer's pushed branch (`builds_on`), merges that same branch (an
ancestor: nothing to merge) and pushes it unchanged as `swarm/<fix task>`. So
the pull request it opens is the implementer's tip, and every byte of that
tip was already through the implementer's own worker's publish scan before
its push (`final_tree_leak`). Nothing new reaches GitHub but a branch name, a
title and a body. This module creates `swarm/<fix task>` at the commit the
implementer RECORDED it pushed (refused if the remote branch has since
moved), and opens the pull request from it, so every reader that finds the
integrator's pull request by its branch or its `result_summary.git` --
issueci, cifix, the merge step, the UI -- reads it exactly as a worker's.

WHAT IS PUBLISHED HERE, AND NOTHING ELSE. The step is published here only
when every one of these holds; otherwise it is DECLINED and goes the
worker's way, unchanged:

  * its dispatch block is `integrate`'s integrator, gated on a review it
    depends on, `builds_on` the implementer, and integrates exactly that one
    contributor, continuing nothing, with no `input.issue` (whose title the
    worker makes);
  * every step it depends on SUCCEEDED, in its own tenant and workflow;
  * the review's verdict file -- the `input_from` name the step stages from
    it -- reads as a verdict OUTSIDE the gate's `verdict_in`, and has no
    finding marked minor (filing those on the wave epic is the worker's,
    #638). A file that does not read as a verdict is declined, and the
    worker refuses it as before: an unreadable review is not a MERGE;
  * the implementer pushed a branch, did not end `no_change` or `skipped`,
    and wrote a usable `pr-title.txt`: one line, no control character, no
    attribution, no task id, nothing the API's redaction masks. Its
    `pr-body.md` is optional, has its attribution lines removed as the
    worker removes them (#735), and is declined, not masked, if redaction
    finds anything in it. Both are read through the same tenant-checked,
    redacted artifact read the API serves (`InspectionService.read_artifact`),
    at most 256 KiB each, and every mention in them is neutralised;
  * the tenant's `-git` token is readable, appears in neither text, and
    GitHub creates the branch and opens (or already has) the pull request.

A step declined here is ordinary: no write but the decline marker, and the
scheduler admits it on the wake this module rings.

THE TRIGGER, THE HOLD AND THE CLAIM. A `task_finished` wake -- the worker's,
on the scheduler's wake topic -- also reaches swarm-api, through its own push
subscription (`POST /v1/admin/tasks/finished`, the rollup sweeper's identity,
terraform/modules/scheduler/main.tf). For the finished task's dependants
that are PARKED on DEPENDENCY_INCOMPLETE and match `held_by_scheduler`, the
scheduler does NOT promote at once (`scheduler.loop.Scheduler.
_held_for_control_publish`): it waits up to its hold for
`metadata.control_publish`. This module CLAIMS the step there first, in a
transaction guarded on the step still being this tenant's, PARKED on
DEPENDENCY_INCOMPLETE and unclaimed, so two deliveries publish once. Then
either:

  * PUBLISHED: one transaction, guarded on the claim, ends the step
    SUCCEEDED with `result_summary.published_by: "control_plane"` and
    `verdict_gate.agent_ran: false`, and a `task_finished` wake for the step
    releases its own dependants (the merge step);
  * DECLINED: the marker says why, and a `task_finished` wake for the parent
    makes the scheduler resolve the step again, now unheld.

A lost wake, a swarm-api that is down, a claim whose holder died: the
scheduler's hold ends (`CONTROL_PUBLISH_HOLD_SECONDS` after the last parent
ended, `CLAIM_TIMEOUT_SECONDS` after a claim) and the step is promoted as
before. The worker then finds the pull request this module may already have
opened and adopts it (`forge.open_pull_request`'s 422 path).

INVARIANT 1. The step goes PARKED -> SUCCEEDED and never holds a lease, a
pool count or an execution. That transition is CONTRACT REQUEST 52
(docs/contract-change-requests.md): `swarm_common.states` does not allow it
today, so `contract_allows()` is false and both this module and the
scheduler's hold do nothing until it is applied. Nothing is changed in
`swarm_common` here.

A FORGED MARKER OR DISPATCH BLOCK IS THE TENANT'S OWN. Firestore has no
document-level IAM, and swarm-api does not verify the step-spec signature
the worker verifies. What a tenant can make this module do by writing its
own documents is open a pull request in its own repository, with its own
token, from a branch its own task pushed: what its own worker would do. A
forged decline costs the hold; a forged claim, the claim timeout.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from swarm_common.admission import _snapshot
from swarm_common.states import EventType, ParkReason, TaskState, can_transition

from .cifix import BRANCH_PREFIX
from .errors import ApiError
from .forgewrite import ForgeWriteError, GitHubWriter
from .issueci import NO_CHANGE_MARKER
from .issuecomments import neutralise_mentions
from .repositories import parse_repository
from .rollup import SKIPPED_SUMMARY_KEY
from .validation import (
    DISPATCH_METADATA_KEY,
    INPUT_FROM_METADATA_KEY,
    REVIEW_VERDICTS,
    IssueRef,
)
from .waker import TASK_FINISHED, ring

log = logging.getLogger(__name__)

TASKS = "tasks"

#: The marker on the gated step's `metadata`, beside `dispatch`, not in it:
#: the dispatch block is signed and this is not. Read by the scheduler's hold
#: (`scheduler.loop.CONTROL_PUBLISH_METADATA_KEY`), held equal by
#: tests/unit/control_plane/test_verdict_publish.py.
CONTROL_PUBLISH_METADATA_KEY = "control_publish"
CLAIMED = "claimed"
DECLINED = "declined"
PUBLISHED = "published"

#: How long the scheduler leaves a claimed step alone before it promotes it
#: anyway (`scheduler.loop.CONTROL_PUBLISH_CLAIM_SECONDS`). A publish is a
#: token read, four artifact reads and four GitHub calls, each bounded by the
#: forge client's own timeout; five minutes is far past all of them, and short
#: enough that a swarm-api that died mid-publish delays the step by no more
#: than the container start it was saving.
CLAIM_TIMEOUT_SECONDS = 300

#: `result_summary.published_by` on a step this module ended.
PUBLISHED_BY = "control_plane"

#: The worker's file names and bounds (`agent_worker.lifecycle.PR_TITLE_FILE`,
#: `PR_BODY_FILE`, `PR_TITLE_MAX_CHARS`, `PR_BODY_MAX_BYTES`,
#: `PR_READ_LIMIT_BYTES`; `agent_worker.verdict.MAX_VERDICT_BYTES`,
#: `MAX_FINDINGS`, `MAX_FINDING_CHARS`), restated because swarm-api does not
#: import the worker; test_verdict_publish.py holds each equal.
PR_TITLE_FILE = "pr-title.txt"
PR_BODY_FILE = "pr-body.md"
PR_TITLE_MAX_CHARS = 256
PR_BODY_MAX_BYTES = 60 * 1024
PR_READ_LIMIT_BYTES = 256 * 1024
MAX_VERDICT_BYTES = 256 * 1024
MAX_FINDINGS = 50
MAX_FINDING_CHARS = 1000

#: `agent_worker.gitops.ATTRIBUTION_MARKERS`, restated (held equal by the test).
ATTRIBUTION_MARKERS = (
    "co-authored-by",
    "generated with",
    "anthropic.com",
    "claude.com/claude-code",
    "claude.ai/code",
)
#: `agent_worker.lifecycle._ROBOT_FACE`: the "Generated with" footer opens with it.
_ROBOT_FACE = "\U0001f916"
#: `agent_worker.lifecycle._RETIRED_TITLE_RE`: the platform's old fallback title.
_RETIRED_TITLE_RE = re.compile(r"\[swarm\]\s*task_", re.IGNORECASE)
#: Any task id, so a title naming the implementer's, the review's or this
#: step's is refused alike (owner rule, 2026-09-28: no task id in a title).
_TASK_ID_IN_TEXT = re.compile(r"\btask_[0-9a-z]{6,}", re.IGNORECASE)
_SHA = re.compile(r"^[0-9a-f]{40}$")
_GITHUB_REPOSITORY = re.compile(
    r"^(?:https://(?:www\.)?github\.com/|git@github\.com:)([^/\s]+)/([^/\s]+?)(?:\.git)?/?$"
)


def contract_allows() -> bool:
    """Whether the frozen state machine lets a step end SUCCEEDED from PARKED.

    Contract request 52. False until it is applied, and then this module and
    the scheduler's hold (which asks the same question) start together.
    """
    return can_transition(TaskState.PARKED, TaskState.SUCCEEDED)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def held_by_scheduler(metadata: Any) -> bool:
    """The steps the scheduler holds for this module: `integrate`'s gated integrator.

    The scheduler restates this (`scheduler.loop.held_for_control_publish`),
    and test_verdict_publish.py holds the two equal over the same documents:
    a step one holds and the other never decides waits out the whole hold.
    Everything else this module checks, it checks after the claim, and
    declines.
    """
    block = _mapping(_mapping(metadata).get(DISPATCH_METADATA_KEY))
    return (
        block.get("strategy") == "integrate"
        and block.get("role") == "integrator"
        and isinstance(block.get("verdict_gate"), Mapping)
    )


@dataclass
class FinishReport:
    """One `task_finished` wake, as this module handled it."""

    task_id: str
    #: Why nothing was considered, when nothing was.
    skipped: str | None = None
    considered: int = 0
    published: list[str] = field(default_factory=list)
    declined: list[dict[str, str]] = field(default_factory=list)

    def to_api(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "skipped": self.skipped,
            "considered": self.considered,
            "published": list(self.published),
            "declined": [dict(d) for d in self.declined],
        }


class Decline(Exception):
    """This step is the worker's after all. `code` is stored; `why` is a sentence."""

    def __init__(self, code: str, why: str) -> None:
        super().__init__(why)
        self.code = code
        self.why = why


# --------------------------------------------------------------------------
# the texts: the worker's rules, restated
# --------------------------------------------------------------------------

def _carries_attribution(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in ATTRIBUTION_MARKERS)


def strip_attribution(text: str) -> str:
    """`agent_worker.lifecycle.strip_attribution`, restated (held equal by the test).

    `text` without its attribution lines; `text` itself when it has none;
    "" when nothing meaningful is left.
    """
    lines = text.split("\n")
    kept = [
        line for line in lines
        if not (_carries_attribution(line) or line.lstrip().startswith(_ROBOT_FACE))
    ]
    if len(kept) == len(lines):
        return text
    collapsed: list[str] = []
    for line in kept:
        if line.strip() or (collapsed and collapsed[-1].strip()):
            collapsed.append(line)
    cleaned = "\n".join(collapsed).strip()
    if not any(char.isalnum() for char in cleaned):
        return ""
    return cleaned


def title_refusal(text: str) -> str | None:
    """Why `text` cannot title the pull request, or None when it can.

    The worker's `_agent_title` checks, in its order, plus any task id at all
    (the worker checks its own; the title here was written by another task,
    so any id in it is one the owner's rule forbids).
    """
    text = text.strip()
    if not text:
        return "blank"
    if "\n" in text or "\r" in text:
        return "more than one line"
    if any(ord(char) < 32 and char != "\t" for char in text):
        return "holds control characters"
    if _carries_attribution(text):
        return "carries attribution"
    if _RETIRED_TITLE_RE.search(text) or _TASK_ID_IN_TEXT.search(text):
        return "names a task id"
    return None


def _cap_title(text: str) -> str:
    text = neutralise_mentions(text.strip())
    if len(text) > PR_TITLE_MAX_CHARS:
        text = text[: PR_TITLE_MAX_CHARS - 3].rstrip() + "..."
    return text


def _cap_body(text: str) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= PR_BODY_MAX_BYTES:
        return text
    return (
        encoded[:PR_BODY_MAX_BYTES].decode("utf-8", errors="ignore").rstrip()
        + f"\n\n_[cut at {PR_BODY_MAX_BYTES} bytes by swarm-api]_"
    )


def _normalise_verdict(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip().upper()
    return text if text in REVIEW_VERDICTS else None


def _finding_text(item: Any) -> str | None:
    """`agent_worker.verdict._finding_text`, restated."""
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


def _is_minor(item: Any) -> bool:
    severity = item.get("severity") if isinstance(item, dict) else None
    return isinstance(severity, str) and severity.strip().lower() == "minor"


@dataclass(frozen=True)
class VerdictRead:
    verdict: str
    findings: tuple[str, ...]
    findings_dropped: int
    minors: int


def parse_verdict(text: str) -> VerdictRead:
    """A verdict file's text, read by the worker's rules (`agent_worker.verdict.read_verdict`).

    Raises `Decline` for anything the worker would refuse: this module never
    reads an unreadable review as MERGE, and declining hands it to the
    worker, which refuses it by name.
    """
    try:
        document = json.loads(text)
    except ValueError:
        raise Decline("verdict_unreadable", "the verdict file is not JSON") from None
    if not isinstance(document, dict):
        raise Decline("verdict_unreadable", "the verdict file is not a JSON object")
    verdict = _normalise_verdict(document.get("verdict"))
    if verdict is None:
        raise Decline("verdict_unreadable", "the verdict file names no verdict this platform reads")
    raw = document.get("findings")
    items = raw if isinstance(raw, list) else ([raw] if raw not in (None, "") else [])
    texts = [t for t in (_finding_text(item) for item in items) if t]
    return VerdictRead(
        verdict=verdict,
        findings=tuple(texts[:MAX_FINDINGS]),
        findings_dropped=max(len(texts) - MAX_FINDINGS, 0),
        minors=sum(1 for item in items if _is_minor(item)),
    )


def verdict_lines(record: Mapping[str, Any]) -> list[str]:
    """`agent_worker.verdict.pull_request_lines` for a gate that stayed shut."""
    lines = [
        "",
        f"Review verdict: **{record.get('verdict')}** "
        f"(from `{record.get('file')}`, task `{record.get('task_id')}`).",
        "The verdict did not name this step, so the fix agent did not run: "
        "this branch is the reviewed work as it was.",
    ]
    findings = [str(f) for f in record.get("findings") or ()]
    if findings:
        lines += ["", "Findings, as the review wrote them:", "", "```text"]
        lines += [f"- {' '.join(f.split()).replace('```', 'ʼʼʼ')}" for f in findings]
        lines.append("```")
    dropped = int(record.get("findings_dropped") or 0)
    if dropped:
        lines.append(f"{dropped} more finding(s) are in the verdict file and not shown here.")
    return lines


# --------------------------------------------------------------------------
# Firestore: the claim and the two endings
# --------------------------------------------------------------------------

def _claimable(data: Mapping[str, Any] | None, tenant_id: str) -> bool:
    return (
        data is not None
        and data.get("tenant_id") == tenant_id
        and data.get("state") == TaskState.PARKED.value
        and data.get("park_reason") == ParkReason.DEPENDENCY_INCOMPLETE.value
    )


def _claim(db: Any, tenant_id: str, task_id: str, claim_id: str, now: datetime) -> bool:
    """Claim the step for this module; True only if this call claimed it."""
    ref = db.collection(TASKS).document(task_id)
    transaction = db.transaction()

    @firestore.transactional
    def _apply(txn: Any) -> bool:
        snap = _snapshot(txn.get(ref))
        data = snap.to_dict() if snap.exists else None
        if not _claimable(data, tenant_id):
            return False
        metadata = dict(_mapping(data.get("metadata")))
        if CONTROL_PUBLISH_METADATA_KEY in metadata or not held_by_scheduler(metadata):
            return False
        metadata[CONTROL_PUBLISH_METADATA_KEY] = {
            "state": CLAIMED, "claim_id": claim_id, "claimed_at": now,
        }
        txn.update(ref, {"metadata": metadata})
        return True

    return bool(_apply(transaction))


def _ours(data: Mapping[str, Any] | None, tenant_id: str, claim_id: str) -> dict[str, Any] | None:
    """The step's metadata, when it is still PARKED under this module's claim."""
    if not _claimable(data, tenant_id):
        return None
    metadata = dict(_mapping(data.get("metadata")))
    marker = _mapping(metadata.get(CONTROL_PUBLISH_METADATA_KEY))
    if marker.get("state") != CLAIMED or marker.get("claim_id") != claim_id:
        return None
    return metadata


def _decline(db: Any, tenant_id: str, task_id: str, claim_id: str, now: datetime,
             code: str, why: str) -> bool:
    """Hand the claimed step back to the scheduler, saying why."""
    ref = db.collection(TASKS).document(task_id)
    transaction = db.transaction()

    @firestore.transactional
    def _apply(txn: Any) -> bool:
        snap = _snapshot(txn.get(ref))
        metadata = _ours(snap.to_dict() if snap.exists else None, tenant_id, claim_id)
        if metadata is None:
            return False
        metadata[CONTROL_PUBLISH_METADATA_KEY] = {
            "state": DECLINED, "claim_id": claim_id,
            "claimed_at": _mapping(metadata.get(CONTROL_PUBLISH_METADATA_KEY)).get("claimed_at"),
            "decided_at": now, "code": code, "reason": why[:500],
        }
        txn.update(ref, {"metadata": metadata, "updated_at": now})
        return True

    return bool(_apply(transaction))


def _succeed(db: Any, tenant_id: str, task_id: str, claim_id: str, now: datetime,
             summary: dict[str, Any]) -> bool:
    """End the claimed step SUCCEEDED (contract request 52's PARKED -> SUCCEEDED)."""
    ref = db.collection(TASKS).document(task_id)
    transaction = db.transaction()

    @firestore.transactional
    def _apply(txn: Any) -> bool:
        snap = _snapshot(txn.get(ref))
        metadata = _ours(snap.to_dict() if snap.exists else None, tenant_id, claim_id)
        if metadata is None or not contract_allows():
            return False
        metadata[CONTROL_PUBLISH_METADATA_KEY] = {
            "state": PUBLISHED, "claim_id": claim_id,
            "claimed_at": _mapping(metadata.get(CONTROL_PUBLISH_METADATA_KEY)).get("claimed_at"),
            "decided_at": now,
        }
        txn.update(ref, {
            "state": TaskState.SUCCEEDED.value,
            "updated_at": now,
            "completed_at": now,
            "park_reason": None,
            "next_eligible_at": None,
            "current_lease_id": None,
            "last_error": None,
            "end_cause": None,
            "result_summary": summary,
            "metadata": metadata,
        })
        return True

    return bool(_apply(transaction))


# --------------------------------------------------------------------------
# the publish
# --------------------------------------------------------------------------

def _repository(url: Any) -> IssueRef:
    matched = _GITHUB_REPOSITORY.match(url) if isinstance(url, str) else None
    if matched is None:
        raise Decline("not_github", "the step's repository is not on github.com")
    try:
        owner, repo = parse_repository(f"{matched.group(1)}/{matched.group(2)}")
    except ValueError:
        raise Decline("not_github", "the step's repository is not an owner/repo on github.com") from None
    return IssueRef(owner=owner, repo=repo, number=0)


def _read_text(ctx: Any, tenant_id: str, task_id: str, name: str, *, limit: int,
               required: bool, what: str) -> str | None:
    """One artifact's whole text through the API's own tenant-checked, redacted read.

    None for an artifact the task's manifest does not list (when not
    `required`). Declined when it is listed and is not whole, not text, over
    `limit`, or held anything the redaction masked: this module publishes
    nothing it had to mask, it hands the step to the worker instead.
    """
    try:
        row = ctx.inspection.read_artifact(
            tenant_id, task_id, submitted_by=None, name=name, limit_bytes=limit,
        )
    except ApiError:
        if required:
            raise Decline(f"{what}_absent", f"task {task_id} uploaded no {name}") from None
        return None
    total = row.get("total_bytes")
    if row.get("status") != "ok":
        raise Decline(f"{what}_unreadable", f"{name} of task {task_id} could not be read whole")
    if isinstance(total, int) and total > limit:
        raise Decline(f"{what}_too_large", f"{name} of task {task_id} is over {limit} bytes")
    if row.get("truncated") or row.get("invalid_utf8_bytes"):
        raise Decline(f"{what}_unreadable", f"{name} of task {task_id} could not be read whole")
    if row.get("redacted"):
        raise Decline("credential", f"{name} of task {task_id} holds text the redaction masks")
    content = row.get("content")
    return content if isinstance(content, str) else ""


def _dispatch_facts(doc: Mapping[str, Any]) -> tuple[str, tuple[str, ...], str]:
    """(the review task, the verdicts that run the agent, the implementer task)."""
    metadata = _mapping(doc.get("metadata"))
    block = _mapping(metadata.get(DISPATCH_METADATA_KEY))
    gate = _mapping(block.get("verdict_gate"))
    review = gate.get("task_id")
    verdicts = gate.get("verdict_in")
    builds_on = block.get("builds_on")
    integrates = block.get("integrates")
    depends_on = doc.get("depends_on") or []
    if not isinstance(review, str) or review not in depends_on:
        raise Decline("gate", "the verdict gate names no task this step depends on")
    normalised = (
        tuple(v for v in (_normalise_verdict(x) for x in verdicts) if v)
        if isinstance(verdicts, list) else ()
    )
    if not normalised or len(normalised) != len(verdicts):
        raise Decline("gate", "the verdict gate names no verdict this platform reads")
    if not isinstance(builds_on, str) or builds_on not in depends_on:
        raise Decline("contributors", "the step builds on no upstream branch")
    if not isinstance(integrates, list) or integrates != [builds_on]:
        count = len(integrates) if isinstance(integrates, list) else 0
        raise Decline(
            "contributors",
            f"the integrator merges {count} contributor branch(es), not exactly the one it builds on",
        )
    if block.get("continues"):
        raise Decline("continuation", "the step continues another task's branch")
    if _mapping(doc.get("input")).get("issue") not in (None, ""):
        raise Decline("issue_input", "the step is titled from its issue input, which the worker reads")
    if doc.get("cancel_requested"):
        raise Decline("cancel_requested", "the step's cancellation was requested")
    return review, normalised, builds_on


def _implementer_head(implementer: Mapping[str, Any], builds_on: str) -> str:
    summary = _mapping(implementer.get("result_summary"))
    if isinstance(summary.get(SKIPPED_SUMMARY_KEY), Mapping) or summary.get(NO_CHANGE_MARKER) is True:
        raise Decline("implementer_no_change", "the implementer changed nothing")
    git = _mapping(summary.get("git"))
    head = git.get("pushed_head")
    if git.get("branch") != f"{BRANCH_PREFIX}{builds_on}" or not isinstance(head, str) \
            or not _SHA.fullmatch(head):
        raise Decline("implementer_unpushed", "the implementer recorded no pushed branch")
    return head


def _publish(ctx: Any, tenant: Any, doc: Mapping[str, Any],
             parents: Mapping[str, Mapping[str, Any]], started: float) -> dict[str, Any]:
    """Open the step's pull request; its `result_summary`, or `Decline`."""
    tenant_id = tenant.tenant_id
    task_id = str(doc.get("id"))
    review, verdict_in, builds_on = _dispatch_facts(doc)
    ref = _repository(doc.get("repository_url"))
    workflow_id = doc.get("workflow_id")
    for parent in (review, builds_on):
        if parents[parent].get("workflow_id") != workflow_id:
            raise Decline("workflow", f"task {parent} is not a step of this step's workflow")

    staged = _mapping(_mapping(doc.get("metadata")).get(INPUT_FROM_METADATA_KEY)).get(review)
    if not isinstance(staged, str) or not staged:
        raise Decline("gate", "the step stages no verdict file from its review")
    text = _read_text(ctx, tenant_id, review, staged, limit=MAX_VERDICT_BYTES,
                      required=True, what="verdict")
    read = parse_verdict(text or "")
    if read.verdict in verdict_in:
        raise Decline("agent_runs", f"the review's verdict is {read.verdict}, so the fix agent runs")
    if read.minors:
        raise Decline("minor_findings",
                      f"the verdict has {read.minors} minor finding(s), which the worker files")

    head = _implementer_head(parents[builds_on], builds_on)
    raw_title = _read_text(ctx, tenant_id, builds_on, PR_TITLE_FILE, limit=PR_READ_LIMIT_BYTES,
                           required=True, what="title")
    refused = title_refusal(raw_title or "")
    if refused:
        raise Decline("title_unusable", f"the implementer's {PR_TITLE_FILE} is refused: {refused}")
    raw_body = _read_text(ctx, tenant_id, builds_on, PR_BODY_FILE, limit=PR_READ_LIMIT_BYTES,
                          required=False, what="body")
    agent_body = None
    if raw_body is not None and raw_body.strip():
        stripped = strip_attribution(raw_body.strip())
        agent_body = neutralise_mentions(stripped) if stripped else None

    try:
        token = ctx.forge_tokens.token_for(tenant)
    except Exception as exc:
        code = exc.code if isinstance(exc, ApiError) else type(exc).__name__
        raise Decline("no_forge_token", f"the tenant's git token could not be read ({code})") from None
    try:
        if not token or token in (raw_title or "") or token in (raw_body or ""):
            raise Decline("credential", "the implementer's pull request text holds the tenant's token")
        writer: GitHubWriter = ctx.forge_writer
        branch = f"{BRANCH_PREFIX}{task_id}"
        upstream = f"{BRANCH_PREFIX}{builds_on}"
        try:
            base = writer.default_branch(ref, token)
            if writer.branch_head(ref, upstream, token) != head:
                raise Decline("implementer_branch_moved",
                              f"{upstream} is not at the commit the implementer pushed")
            writer.create_branch(ref, branch, head, token)
            title = _cap_title(raw_title or "")
            verdict_record = {
                "task_id": review, "file": staged, "verdict": read.verdict,
                "verdict_in": list(verdict_in), "agent_ran": False,
                "findings": list(read.findings), "findings_dropped": read.findings_dropped,
            }
            generated = "\n".join([
                "Opened by SwarmCloud's control plane. This branch is written only by this task.",
                "",
                f"- task: `{task_id}`",
                f"- tenant: `{tenant_id}`",
                f"- branch: `{branch}`",
                f"- head commit: `{head}`",
                "",
                "No worker ran for this step: the review's verdict did not name it, so "
                "there was no agent to run, and the branch is the implementer's as it was "
                "pushed (and scanned) by the implementer's own worker.",
                "",
                "Integrates 1 contributor branch(es):",
                f"- merged: `{upstream}`",
                *verdict_lines(verdict_record),
            ])
            body = _cap_body(f"{agent_body}\n\n---\n\n{generated}" if agent_body else generated)
            pull, created = writer.open_pull(
                ref, head=branch, base=base, title=title, body=body, token=token,
            )
        except ForgeWriteError as exc:
            raise Decline("forge", f"GitHub refused the publish ({exc.code})") from None
    finally:
        token = ""
    if pull.head_sha and pull.head_sha != head:
        # An open pull request from this branch at another commit: someone
        # else's. Left for the worker, which owns this branch.
        raise Decline("pull_request_elsewhere", f"an open pull request from {branch} is at another commit")
    return {
        "published_by": PUBLISHED_BY,
        "verdict_gate": verdict_record,
        "skipped_agent": f"review verdict {read.verdict}",
        "pull_request_text_from": {"title": "implementer",
                                   "body": "implementer" if agent_body else None},
        "branch": {"name": branch, "head": head, "head_sha": head},
        "git": {
            "repository": ref.repository_url,
            "default_branch": base,
            "role": "integrator",
            "integrated": {"merged": [upstream], "conflicted": [], "missing": [], "complete": True},
            "branch": branch,
            "pushed_head": head,
            "pull_request_text": {"title": "agent", "body": "agent" if agent_body else "platform"},
            "published": True,
            "pull_request": {"number": pull.number, "url": pull.url, "state": pull.state,
                             "created": created, "updated": False},
            "publish_reason": "opened" if created
            else "an open pull request already existed and was reused",
        },
        "artifacts": [],
        "duration_seconds": round(time.monotonic() - started, 3),
    }


def _consider(ctx: Any, tenant: Any, doc: Mapping[str, Any], report: FinishReport,
              parent_id: str) -> None:
    db = ctx.store.db
    tenant_id = tenant.tenant_id
    task_id = str(doc.get("id") or "")
    if CONTROL_PUBLISH_METADATA_KEY in _mapping(doc.get("metadata")):
        return
    parents: dict[str, Mapping[str, Any]] = {}
    for upstream in doc.get("depends_on") or []:
        snap = db.collection(TASKS).document(str(upstream)).get()
        data = snap.to_dict() if snap.exists else None
        if (data is None or data.get("tenant_id") != tenant_id
                or data.get("state") != TaskState.SUCCEEDED.value):
            # Not every parent has succeeded: the scheduler holds nothing
            # yet, and cancels the step if one failed.
            return
        parents[str(upstream)] = data
    report.considered += 1
    claim_id = uuid.uuid4().hex
    if not _claim(db, tenant_id, task_id, claim_id, ctx.now()):
        return
    started = time.monotonic()
    try:
        summary = _publish(ctx, tenant, doc, parents, started)
    except Decline as declined:
        code, why = declined.code, declined.why
    except Exception as exc:  # never a reason to keep the step from its worker
        code = exc.code if isinstance(exc, ApiError) else type(exc).__name__
        why = f"the control-plane publish failed ({code}); the worker publishes instead"
        log.exception("verdict publish %s tenant=%s failed", task_id, tenant_id)
    else:
        if _succeed(db, tenant_id, task_id, claim_id, ctx.now(), summary):
            report.published.append(task_id)
            ctx.store.append_event(
                task_id=task_id, tenant_id=tenant_id, type=EventType.SUCCEEDED,
                detail={"published_by": PUBLISHED_BY,
                        "pull_request": summary["git"]["pull_request"]["url"]},
            )
            log.info("verdict publish %s tenant=%s: opened %s", task_id, tenant_id,
                     summary["git"]["pull_request"]["url"])
            ring(ctx.waker, ctx.metrics, TASK_FINISHED, task_id=task_id, tenant_id=tenant_id,
                 state=TaskState.SUCCEEDED.value)
        else:
            # The pull request is open and the step is not ours any more (a
            # cancel, or a claim older than the scheduler's timeout). Its
            # worker adopts the pull request (`forge.open_pull_request`).
            log.warning("verdict publish %s tenant=%s: published, but the claim was lost",
                        task_id, tenant_id)
        return
    report.declined.append({"task_id": task_id, "code": code})
    log.info("verdict publish %s tenant=%s declined: %s (%s)", task_id, tenant_id, code, why)
    if _decline(db, tenant_id, task_id, claim_id, ctx.now(), code, why):
        # The scheduler resolves the parent's dependants again, now unheld.
        ring(ctx.waker, ctx.metrics, TASK_FINISHED, task_id=parent_id, tenant_id=tenant_id,
             state=TaskState.SUCCEEDED.value)


#: How many dependants one wake considers: a workflow's step limit bounds
#: how many tasks name one parent, and this is past it.
MAX_DEPENDANTS = 100


def on_task_finished(ctx: Any, task_id: str, *, tenant_id: str | None = None) -> FinishReport:
    """A task ended: publish its dependants' MERGE pull requests that need no worker.

    A doorbell, as the scheduler's `release_dependants` is: everything is
    read again, the attributes are trusted for nothing but a cross-check, and
    a task that is missing, not SUCCEEDED, or of another tenant than the wake
    said is a no-op.
    """
    report = FinishReport(task_id=task_id)
    if not contract_allows():
        report.skipped = "contract_request_52"
        return report
    db = ctx.store.db
    snap = db.collection(TASKS).document(task_id).get()
    parent = snap.to_dict() if snap.exists else None
    if parent is None or parent.get("state") != TaskState.SUCCEEDED.value:
        report.skipped = "not_succeeded"
        return report
    owner = str(parent.get("tenant_id") or "")
    if not owner or (tenant_id and tenant_id != owner):
        report.skipped = "tenant_mismatch"
        return report
    tenant = ctx.store.get_tenant(owner)
    if tenant is None or not getattr(tenant, "enabled", True):
        report.skipped = "tenant_unavailable"
        return report
    query = (
        db.collection(TASKS)
        .where(filter=FieldFilter("depends_on", "array_contains", task_id))
        .limit(MAX_DEPENDANTS)
    )
    for dependant in query.stream():
        doc = dependant.to_dict() or {}
        # Tenant, state and park reason checked here, as the scheduler's
        # `dependants_waiting_on` checks them (invariant 9).
        if not _claimable(doc, owner) or not held_by_scheduler(doc.get("metadata")):
            continue
        _consider(ctx, tenant, doc, report, task_id)
    return report

