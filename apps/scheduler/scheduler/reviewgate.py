"""Two skips decided as a step is readied, so no pod starts for work that buys nothing (#2524).

The token analysis of the week 2026-10-20..27 (#2524) found every change got
a review, whatever it touched and however small, and that fix rounds were
started for pull requests that had already merged. BGD decided on
2026-10-27 which skips to build and with what values.

1. THE REVIEW GATE (REVIEW-GATE, BGD: "Risk paths + over 300 lines; reuse
renders"). A workflow step may carry `metadata.review_gate = {"min_lines":
300, "risk_paths": [...]}` (`swarm_api.validation.resolve_review_gate`, a
422 for an unknown key or a bad type); the issue run's compiled review
carries it by default (`swarm_api.issueruns.review_gate_block`). As the
scheduler readies such a step -- in `Scheduler._promote_dependant`, beside
the `browser_if_ui` resolution (`scheduler.runtime`), every parent SUCCEEDED
so what they changed is final -- `resolve` reads the parents' changed paths
and changed-line counts, the record `runtime` reads, and:

  * the review RUNS when that touches any risk path (`DEFAULT_RISK_PATHS`
    when the block names none: credentials and the git token path, tenant
    isolation, redaction, IAM, the frozen contract) or changes more than
    `min_lines` lines in total;
  * the review RUNS when what was changed cannot be told: a parent that
    cannot be read, has no git record, has no line counts, or whose file
    list was cut with no risk path in what it kept. A gate that could not
    see the diff is not one that may wave it through;
  * otherwise the step ends SUCCEEDED, PARKED -> SUCCEEDED in one guarded
    write, holding nothing at any point (invariants 1-3): no lease, no
    attempt, no pod.

WHAT A SKIPPED REVIEW LEAVES. Everything downstream reads a review through
its `verdict.json`: the integrator's `when` gate, the re-review the merge's
`on_merge_verdict` appends (which passes the verdict on), the merge step.
The scheduler holds no bucket credential and must not gain one on its drain
path (`agent_worker.verdict`'s docstring says why), so the verdict is
written INLINE: the `result_summary.artifacts` entry for `verdict.json`
carries its text under `inline` and no `uri`, and the worker stages it from
there exactly as it would download it (`agent_worker.inputs`). The text is

    {"verdict": "MERGE", "findings": [], "skipped": "<reason>"}

and `result_summary.review_skipped` is the same reason, so the step reads as
a MERGE to every gate and as a skip to every person. `result_summary.skipped`
is deliberately NOT written: that key means "this step did not run and left
nothing", which makes a dependant that stages anything from it skip too
(`agent_worker.expected_outputs.nothing_to_work_on`) -- the integrator would
then never publish.

RENDERS PUBLISHED. The parents' `result_summary.artifacts` are read for an
image under `shots/` -- the directory the browser runner's rendered-review
rule names (`agent_worker.verdict.RENDERED_REVIEW_SHOTS`); no compare-report
manifest is written by any runner today, so the listing is the check. What
was found is recorded on the step (`renders_published`); it does not move
the review off the browser, for the reason in docs/workflows.md "Reviews
only where they matter".

2. THE NO-OP PRECHECK (NOOP-PRECHECK). A merge step's appended CI-fix,
conflict-fix and pick-fix rounds (`swarm_api.mergewake`, metadata
`merge_fix_round`, or `merge_conflict_round` for a conflict round) are checked at admission, before a lease: when the merge
task they serve records that its pull request was merged or closed, the
round ends without a lease, CANCELLED with `result_summary.skipped.reason`
(READY -> SUCCEEDED is not a transition the frozen contract has). What is
read is only what mergewake's tick already wrote -- `merge_wait.wake_reason`
from its pull request read, cached per `issueci.CI_READ_SECONDS` -- or the
merge task's own SUCCEEDED; no GitHub call is added, and the scheduler holds
no forge token.

THE PLATFORM'S WORDS ARE RESTATED, NOT SHARED, for the reason `runtime` says:
the scheduler image carries `swarm_common` and `quota_broker` only.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Callable, Mapping

log = logging.getLogger(__name__)

#: The task metadata key the gate lives under.
#: `swarm_api.validation.REVIEW_GATE_METADATA_KEY`, restated.
REVIEW_GATE_METADATA_KEY = "review_gate"
#: BGD's value, 2026-10-27. `swarm_api.validation.REVIEW_GATE_DEFAULT_MIN_LINES`.
DEFAULT_MIN_LINES = 300

#: The paths whose change always gets a review, BGD's list (#2524): a glob
#: per entry, `**` crossing directories and `*` not; an entry ending in `/`
#: is everything under that directory.
DEFAULT_RISK_PATHS: tuple[str, ...] = (
    # The frozen contract every component was written against.
    "apps/common/swarm_common/",
    # Redaction: the shared package and each component's own pass.
    "apps/redaction/",
    "apps/**/redact*.py",
    "apps/**/redaction*.py",
    # Credentials and the git token path.
    "apps/**/*secret*.py",
    "apps/**/*credential*.py",
    "apps/**/*token*.py",
    "apps/**/forge*.py",
    "apps/**/oauth*.py",
    "apps/agent-worker/agent_worker/specverify.py",
    "apps/swarm-api/swarm_api/specsigning.py",
    "scripts/create-secrets.sh",
    "scripts/lib/forge-slot.sh",
    "scripts/lib/auth-guard.sh",
    # Tenant isolation.
    "apps/**/auth.py",
    "apps/**/*tenant*.py",
    "kubernetes/namespaces/",
    "scripts/**/*tenant*",
    "terraform/**/*tenant*",
    # IAM.
    "terraform/**/iam*",
    "terraform/bootstrap/",
    "scripts/iam-*",
)

#: The artifact a review writes and every dependant reads.
VERDICT_FILE = "verdict.json"
#: `result_summary` keys a skipped review carries.
REVIEW_SKIPPED_KEY = "review_skipped"
#: The manifest entry's key for text the platform wrote in place of an
#: upload. `agent_worker.inputs.INLINE_ARTIFACT_KEY`, held equal by the test.
INLINE_ARTIFACT_KEY = "inline"
#: Who wrote an inline entry, on the entry.
INLINE_WRITER = "scheduler"

#: Where the rendered-review rule's screenshots live, and what is an image
#: there. `agent_worker.verdict.RENDERED_REVIEW_SHOTS` / `SHOT_SUFFIXES`.
SHOTS_DIRECTORY = "shots/"
SHOT_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif")

#: Task documents read per decision, as in `runtime.MAX_DOCUMENTS_READ`.
MAX_DOCUMENTS_READ = 40
#: Paths kept in the record and the event.
MAX_PATHS_SHOWN = 20

#: Why a step decided as it did.
REASON_RISK = "risk_paths"
REASON_OVER = "over_min_lines"
REASON_SMALL = "small_change"
REASON_UNREADABLE = "change_unreadable"
REASON_CUT = "file_list_cut"
REASON_NO_PARENTS = "no_parent_changes"

#: The merge task's recorded pull-request endings. `swarm_api.mergewake`'s
#: `_Reading("merged"/"closed")` and `PULL_REQUEST_MERGED` / `_CLOSED`.
MERGE_FIX_ROUND_KEY = "merge_fix_round"
#: A conflict-fix round's block. `swarm_api.validation.CONFLICT_ROUND_METADATA_KEY`.
CONFLICT_ROUND_KEY = "merge_conflict_round"
ROUND_KEYS = (MERGE_FIX_ROUND_KEY, CONFLICT_ROUND_KEY)
MERGE_WAIT_KEY = "merge_wait"
PR_ENDED_REASONS: Mapping[str, str] = {
    "merged": "merged",
    "closed": "closed",
    "pull_request_merged": "merged",
    "pull_request_closed": "closed",
}
#: `result_summary.skipped`, the worker's own key for a step that did not run
#: (`agent_worker.expected_outputs.SKIPPED_SUMMARY_KEY`).
SKIPPED_SUMMARY_KEY = "skipped"


@lru_cache(maxsize=256)
def _pattern(glob: str) -> re.Pattern[str]:
    out: list[str] = []
    i = 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif glob.startswith("**", i):
            out.append(".*")
            i += 2
        elif glob[i] == "*":
            out.append("[^/]*")
            i += 1
        elif glob[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(glob[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def matches(path: str, glob: str) -> bool:
    """Whether a repository path is under one risk-path entry."""
    text = path.lstrip("/")
    entry = glob.lstrip("/")
    if entry.endswith("/"):
        return text.startswith(entry)
    return _pattern(entry).match(text) is not None


@dataclass(frozen=True)
class Gate:
    """A step's checked `review_gate`."""

    min_lines: int
    risk_paths: tuple[str, ...]


def policy_of(metadata: Any) -> Gate | None:
    """The step's gate, or None for a step with none.

    The document is tenant-writable, so a block this reader cannot make sense
    of is no gate at all: the review runs, as it did before gates existed.
    """
    block = metadata.get(REVIEW_GATE_METADATA_KEY) if isinstance(metadata, Mapping) else None
    if not isinstance(block, Mapping) or "resolved" in block:
        return None
    min_lines = block.get("min_lines", DEFAULT_MIN_LINES)
    if isinstance(min_lines, bool) or not isinstance(min_lines, int) or min_lines < 0:
        return None
    paths = block.get("risk_paths")
    if paths is None:
        risk = DEFAULT_RISK_PATHS
    elif isinstance(paths, list) and paths and all(isinstance(p, str) and p for p in paths):
        risk = tuple(paths)
    else:
        return None
    return Gate(min_lines, risk)


@dataclass(frozen=True)
class Decision:
    """Whether a gated review runs, and why."""

    run: bool
    reason: str
    changed_lines: int = 0
    min_lines: int = DEFAULT_MIN_LINES
    risk_hits: tuple[str, ...] = ()
    read: tuple[str, ...] = ()
    unreadable: tuple[str, ...] = ()
    shots: tuple[str, ...] = field(default=())

    @property
    def text(self) -> str:
        """The one sentence a person reads: on the verdict, the summary and the event."""
        if self.reason == REASON_SMALL:
            return (f"review skipped: {self.changed_lines} changed lines (the gate runs a review "
                    f"over {self.min_lines}) and no risk path touched")
        if self.reason == REASON_RISK:
            return "review runs: the change touches a risk path (" + ", ".join(
                self.risk_hits[:MAX_PATHS_SHOWN]) + ")"
        if self.reason == REASON_OVER:
            return (f"review runs: {self.changed_lines} changed lines, over the gate's "
                    f"{self.min_lines}")
        if self.reason == REASON_CUT:
            return "review runs: a parent's file list was cut, so the change cannot be told"
        if self.reason == REASON_NO_PARENTS:
            return "review runs: no parent change was read"
        return "review runs: what the parents changed cannot be read"

    def to_detail(self) -> dict[str, Any]:
        return {
            "run": self.run,
            "reason": self.reason,
            "text": self.text,
            "changed_lines": self.changed_lines,
            "min_lines": self.min_lines,
            "risk_paths_touched": list(self.risk_hits[:MAX_PATHS_SHOWN]),
            "parents_read": list(self.read),
            "unreadable": list(self.unreadable),
            "renders_published": bool(self.shots),
            "shots": list(self.shots[:MAX_PATHS_SHOWN]),
            "shots_count": len(self.shots),
        }


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def change_of(document: Any) -> tuple[list[str], int | None, bool]:
    """`(paths, changed lines, file list cut)` from one task's `result_summary.git`.

    Paths are `own_files` when the worker recorded them (a merge of main left
    out, #2337), else every `files` row's `path` and `old_path`. Lines are
    `insertions + deletions`, None when either is not a count.
    """
    git = _mapping(_mapping(_mapping(document).get("result_summary")).get("git"))
    lines: int | None = None
    insertions, deletions = _count(git.get("insertions")), _count(git.get("deletions"))
    if insertions is not None and deletions is not None:
        lines = insertions + deletions
    own = git.get("own_files")
    if isinstance(own, list):
        return ([p for p in own if isinstance(p, str)], lines,
                git.get("own_files_truncated") is True)
    rows = git.get("files")
    if not isinstance(rows, list):
        return [], None, False
    paths: list[str] = []
    for row in rows:
        for key in ("path", "old_path"):
            value = _mapping(row).get(key)
            if isinstance(value, str) and value:
                paths.append(value)
    return paths, lines, git.get("files_truncated") is True


def shots_of(document: Any) -> list[str]:
    """The images a parent published under `shots/`, by artifact name."""
    entries = _mapping(_mapping(document).get("result_summary")).get("artifacts")
    found: list[str] = []
    for entry in entries if isinstance(entries, list) else ():
        name = _mapping(entry).get("name")
        if (isinstance(name, str) and name.startswith(SHOTS_DIRECTORY)
                and name.lower().endswith(SHOT_SUFFIXES)):
            found.append(name)
    return found


def _builds_on(document: Mapping[str, Any]) -> str | None:
    raw = _mapping(_mapping(document.get("metadata")).get("dispatch")).get("builds_on")
    return raw.strip() if isinstance(raw, str) and raw.strip() else None


def resolve(
    task_depends_on: list[str],
    gate: Gate,
    read: Callable[[str], Mapping[str, Any] | None],
) -> Decision:
    """Whether a gated review runs, from its parents' changes.

    The parents and, along each one's `builds_on`, the steps whose branches
    it started from -- a chain's last step records only its own diff -- are
    read through `read(task_id)`, the tenant-gated document read. Lines are
    summed over every document read; a step that joined several carries
    their diffs in its own, so the sum can only over-count, and an over-count
    only runs a review.
    """
    queue = list(dict.fromkeys(task_depends_on))
    seen: set[str] = set()
    visited: list[str] = []
    unknown: list[str] = []
    hits: dict[str, None] = {}
    shots: dict[str, None] = {}
    total = 0
    cut = False
    while queue:
        task_id = queue.pop(0)
        if task_id in seen:
            continue
        seen.add(task_id)
        if len(visited) >= MAX_DOCUMENTS_READ:
            unknown.append(task_id)
            break
        visited.append(task_id)
        document = read(task_id)
        if document is None:
            unknown.append(task_id)
            continue
        paths, lines, truncated = change_of(document)
        for path in paths:
            if any(matches(path, glob) for glob in gate.risk_paths):
                hits.setdefault(path.lstrip("/"), None)
        shots.update(dict.fromkeys(shots_of(document)))
        if lines is None:
            unknown.append(task_id)
        else:
            total += lines
        cut = cut or truncated
        upstream = _builds_on(document)
        if upstream and upstream not in seen:
            queue.append(upstream)
    common = {"changed_lines": total, "min_lines": gate.min_lines, "read": tuple(visited),
              "unreadable": tuple(unknown), "shots": tuple(shots)}
    if hits:
        return Decision(True, REASON_RISK, risk_hits=tuple(hits), **common)
    if total > gate.min_lines:
        return Decision(True, REASON_OVER, **common)
    if unknown:
        return Decision(True, REASON_UNREADABLE, **common)
    if cut:
        return Decision(True, REASON_CUT, **common)
    if not visited:
        return Decision(True, REASON_NO_PARENTS, **common)
    return Decision(False, REASON_SMALL, **common)


def verdict_text(reason: str) -> str:
    """The verdict.json a skipped review leaves: a MERGE naming why."""
    return json.dumps({"verdict": "MERGE", "findings": [], "skipped": reason},
                      sort_keys=True) + "\n"


def skip_patch(stored: Mapping[str, Any], decision: Decision, at: Any) -> dict[str, Any]:
    """The fields a skipped review is ended with, from the document as stored.

    `result_summary` is the platform's only: the step never ran, so there is
    nothing of a worker's in it to keep. `metadata.review_gate.resolved`
    records the decision, merged into the block as stored.
    """
    text = verdict_text(decision.text)
    metadata = dict(_mapping(stored.get("metadata")))
    block = dict(_mapping(metadata.get(REVIEW_GATE_METADATA_KEY)))
    block["resolved"] = {**decision.to_detail(), "at": at}
    metadata[REVIEW_GATE_METADATA_KEY] = block
    return {
        "metadata": metadata,
        "result_summary": {
            REVIEW_SKIPPED_KEY: decision.text,
            REVIEW_GATE_METADATA_KEY: decision.to_detail(),
            "artifacts": [{
                "name": VERDICT_FILE,
                "bytes": len(text.encode("utf-8")),
                INLINE_ARTIFACT_KEY: text,
                "written_by": INLINE_WRITER,
                "content_type": "application/json",
            }],
        },
    }


def _round_block(metadata: Any) -> Mapping[str, Any]:
    for key in ROUND_KEYS:
        block = _mapping(_mapping(metadata).get(key))
        if block:
            return block
    return {}


def round_merge_task(metadata: Any) -> str | None:
    """The merge task a CI-fix, conflict-fix or pick-fix round serves, or None."""
    raw = _round_block(metadata).get("merge_task")
    return raw if isinstance(raw, str) and raw else None


def pull_request_ended(merge_task: Mapping[str, Any] | None) -> str | None:
    """`merged` / `closed` when the merge task records its pull request ended, else None.

    From the merge task only: SUCCEEDED (the merge step merged it, or saw it
    merged outside the workflow), or the ending mergewake's pull request read
    wrote into `merge_wait.wake_reason`.
    """
    if merge_task is None:
        return None
    if merge_task.get("state") == "SUCCEEDED":
        return "merged"
    reason = _mapping(_mapping(merge_task.get("metadata")).get(MERGE_WAIT_KEY)).get("wake_reason")
    return PR_ENDED_REASONS.get(reason) if isinstance(reason, str) else None


def noop_round_reason(
    metadata: Any, read: Callable[[str], Mapping[str, Any] | None],
) -> str | None:
    """Why a fix round would buy nothing, or None when it runs (check (a))."""
    merge_task = round_merge_task(metadata)
    if merge_task is None:
        return None
    ended = pull_request_ended(read(merge_task))
    if ended is None:
        return None
    number = _round_block(metadata).get("pull_request")
    which = f"pull request #{number}" if isinstance(number, int) else "its pull request"
    return f"{which} is already {ended}, so the fix round has nothing to fix"


def end_if_skipped(store: Any, task: Any, at: Any) -> Any | None:
    """End a PARKED gated review SUCCEEDED when its parents' change buys none.

    The one call site is `Scheduler._promote_dependant`, before the
    `browser_if_ui` resolution. None when the review runs (nothing written);
    else the store's `GuardedWrite` for the PARKED -> SUCCEEDED write.
    """
    gate = policy_of(getattr(task, "metadata", None))
    if gate is None or getattr(getattr(task, "state", None), "value", None) != "PARKED":
        return None
    decision = resolve(
        list(task.depends_on), gate,
        lambda task_id: store.task_document(task_id, tenant_id=task.tenant_id),
    )
    log.info("review gate decided as the step is readied",
             extra={"task_id": task.id, "tenant_id": task.tenant_id,
                    "run": decision.run, "reason": decision.reason})
    if decision.run:
        return None
    return store.succeed_without_worker(
        task, patch_from=lambda stored: skip_patch(stored, decision, at),
        detail={"review_gate": decision.to_detail()},
    )


def end_if_noop(store: Any, task: Any) -> Any | None:
    """End a READY fix round whose pull request already ended, before a lease.

    Called from admission (`Scheduler._admit_one`). None when the round runs;
    else the store's `GuardedWrite` for the READY -> CANCELLED write.
    """
    reason = noop_round_reason(
        getattr(task, "metadata", None),
        lambda task_id: store.task_document(task_id, tenant_id=task.tenant_id),
    )
    if reason is None or getattr(getattr(task, "state", None), "value", None) != "READY":
        return None
    return store.skip_before_lease(task, reason)
