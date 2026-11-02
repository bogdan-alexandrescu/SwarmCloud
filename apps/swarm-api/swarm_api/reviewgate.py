"""The review gate: whether a gated review runs, decided by swarm-api (#2524).

The token analysis of the week 2026-10-20..27 (#2524) found every change got
a review, whatever it touched and however small. BGD decided on 2026-10-27:
"Risk paths + over 300 lines; reuse renders"; and on 2026-10-28 WHO ends a
skipped review: "No contract change. The scheduler holds the review PARKED
(as the #748 control-publish hold does) and swarm-api ends it PARKED ->
SUCCEEDED on a wake." `swarm_common.states` says of PARKED -> SUCCEEDED
"only swarm-api writes it", and that stays true.

THE BLOCK. A workflow step may carry `metadata.review_gate = {"min_lines":
300, "risk_paths": [...]}` (`validation.resolve_review_gate`, a 422 for an
unknown key or a bad type), or `false` to switch the gate off for it.

EVERY REVIEW STEP BY DEFAULT (BGD 2026-10-28, page M8: "every review step").
A review step -- one another step's `when` reads a verdict from, the issue
run's compiled review among them -- that names no block is stored with
`validation.REVIEW_GATE_DEFAULT_BLOCK` (`is_default`), and its policy is
read when it is decided: the workspace's `workspace_settings/{tenant}.
review_gate` (`min_lines`, `risk_paths`, `enabled: false` to switch the
default off) where set, else the platform's (`DEFAULT_MIN_LINES`,
`DEFAULT_RISK_PATHS`). Whatever list a step or a workspace names, the
platform's risk paths stay in it (`_gate_from`): a risk path always runs the
review, never skipped on line count.

THE REQUIREMENTS CHECK (BGD 2026-10-28 ~18:25Z, "Gate + cheap requirements
check"). An issue run's `requirements-check` step carries
`review_gate.requirements_check` (`is_requirements_check`) and is held like
the review it stands beside, then decided the other way round: it runs only
when that review would be skipped. When the review is then skipped, the
check's requirements.json `requirements` list -- and nothing else from it
(`requirements_from`) -- is folded into the skipped review's inline verdict,
whose `verdict` stays the gate's MERGE; `issueruns.requirements_finding`
reads it exactly as after a full review. The check's own change is not
counted by `resolve`: it is read-only.

THE HOLD AND THE DECISION. A gated step whose parents all SUCCEEDED is held
PARKED by the scheduler exactly as `integrate`'s gated integrator is
(`scheduler.loop.held_for_control_publish`, restated from
`verdictpublish.held_by_scheduler`): no lease, no pool count, no execution
(invariants 1-3). swarm-api receives the same `task_finished` wake
(`verdictpublish.on_task_finished`), claims the step under the same
`metadata.control_publish` marker, and `resolve` reads the parents' changed
paths and changed-line counts:

  * the review RUNS when that touches any risk path (`DEFAULT_RISK_PATHS`
    when the block names none: credentials and the git token path, tenant
    isolation, redaction, IAM, the frozen contract) or changes more than
    `min_lines` lines in total;
  * the review RUNS when what was changed cannot be told: a parent that
    cannot be read, has no git record, has no line counts, or whose file
    list was cut with no risk path in what it kept. A gate that could not
    see the diff is not one that may wave it through;
  * a review that runs is DECLINED, with the decision as the reason, and the
    parent's wake is rung again: the scheduler promotes it on that wake;
  * otherwise swarm-api ends it PARKED -> SUCCEEDED in the guarded write that
    publishes an integrator, and rings `task_finished` for it, so its
    dependants are released.

A lost wake, a swarm-api that is down, a claim whose holder died: the
scheduler's hold ends and the review is promoted and RUNS. The fallback
spends a review; it never invents a MERGE.

WHAT A SKIPPED REVIEW LEAVES. Everything downstream reads a review through
its `verdict.json`: the integrator's `when` gate, the re-review the merge's
`on_merge_verdict` appends, the merge step, swarm-api's own publish of the
integrator and an issue run's per-requirement read. The verdict is written
INLINE in the manifest -- the `result_summary.artifacts` entry for
`verdict.json` carries its text under `inline`, `written_by:
"control_plane"`, and no `uri` -- because no attempt ran, so there is no
attempt prefix in the bucket to put it under. The worker stages it from
there (`agent_worker.inputs.inline_artifact`) and swarm-api serves it,
redacted, through the same artifact read as any other
(`inspect.InspectionService.read_artifact`, `inline_verdict`). The text is

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
was found is recorded on the decision (`renders_published`); it does not
move the review off the browser (BGD 2026-10-28: a follow-up lane).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Callable, Mapping

from .validation import (
    REVIEW_GATE_DEFAULT_BLOCK,
    REVIEW_GATE_DEFAULT_MIN_LINES,
    REVIEW_GATE_MAX_MIN_LINES,
    REVIEW_GATE_MAX_PATH_CHARS,
    REVIEW_GATE_MAX_RISK_PATHS,
    REVIEW_GATE_METADATA_KEY,
    REVIEW_GATE_REQUIREMENTS_CHECK,
)

log = logging.getLogger(__name__)

#: BGD's value, 2026-10-27 (`validation.REVIEW_GATE_DEFAULT_MIN_LINES`).
DEFAULT_MIN_LINES = REVIEW_GATE_DEFAULT_MIN_LINES

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
    # CI: what runs with the repository's tokens (BGD 2026-10-28, #2524).
    ".github/workflows/",
    ".github/actions/",
)

#: The marker `validation.REVIEW_GATE_DEFAULT_BLOCK` stores on a review step
#: that named no gate: its policy is the workspace's, else the platform's.
DEFAULT_MARKER = next(iter(REVIEW_GATE_DEFAULT_BLOCK))
#: The field of `workspace_settings/{tenant_id}` a workspace's default lives
#: in: `{"enabled": bool, "min_lines": int, "risk_paths": [glob, ...]}`,
#: every key optional. `enabled: false` switches the default off for the
#: workspace: a review step that names no gate then always runs.
WORKSPACE_SETTINGS = "workspace_settings"
WORKSPACE_FIELD = "review_gate"
WORKSPACE_KEYS = ("enabled", "min_lines", "risk_paths")

#: What an issue run's requirements check writes, and the most of it read.
REQUIREMENTS_FILE = "requirements.json"
MAX_REQUIREMENTS_BYTES = 64 * 1024
#: The note kept per requirement when they are folded into a skipped
#: review's inline verdict, which must stay under `MAX_INLINE_BYTES`.
FOLDED_NOTE_CHARS = (200, 60, 0)

#: The artifact a review writes and every dependant reads.
VERDICT_FILE = "verdict.json"
#: `result_summary` keys a skipped review carries.
REVIEW_SKIPPED_KEY = "review_skipped"
#: The manifest entry's key for text the platform wrote in place of an
#: upload, who wrote it, and its bound. `agent_worker.inputs.INLINE_ARTIFACT_KEY`,
#: `INLINE_WRITER` and `MAX_INLINE_BYTES`, held equal by the test.
INLINE_ARTIFACT_KEY = "inline"
INLINE_WRITER = "control_plane"
MAX_INLINE_BYTES = 4096

#: Where the rendered-review rule's screenshots live, and what is an image
#: there. `agent_worker.verdict.RENDERED_REVIEW_SHOTS` / `SHOT_SUFFIXES`.
SHOTS_DIRECTORY = "shots/"
SHOT_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif")

#: Task documents read per decision, as in `scheduler.runtime.MAX_DOCUMENTS_READ`.
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
REASON_OFF = "review_gate_off"



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


def _gate_from(block: Mapping[str, Any]) -> Gate | None:
    """`min_lines` and `risk_paths` read from a block; None when they cannot be.

    The platform's risk paths are ALWAYS among the gate's (BGD 2026-10-28):
    a step or a workspace naming its own list adds to them, and never
    narrows them, so a change to credentials, IAM, redaction, tenant
    isolation, `.github/workflows/` or the frozen contract is never skipped
    on its line count.
    """
    min_lines = block.get("min_lines", DEFAULT_MIN_LINES)
    if (isinstance(min_lines, bool) or not isinstance(min_lines, int)
            or not 0 <= min_lines <= REVIEW_GATE_MAX_MIN_LINES):
        return None
    paths = block.get("risk_paths")
    if paths is None:
        risk = DEFAULT_RISK_PATHS
    elif (isinstance(paths, list) and paths and len(paths) <= REVIEW_GATE_MAX_RISK_PATHS
          and all(isinstance(p, str) and p and len(p) <= REVIEW_GATE_MAX_PATH_CHARS
                  for p in paths)):
        risk = tuple(dict.fromkeys([*DEFAULT_RISK_PATHS, *paths]))
    else:
        return None
    return Gate(min_lines, risk)


def is_default(metadata: Any) -> bool:
    """Whether the step's gate is the default one (`REVIEW_GATE_DEFAULT_BLOCK`)."""
    return _mapping(_mapping(metadata).get(REVIEW_GATE_METADATA_KEY)).get(DEFAULT_MARKER) is True


def is_requirements_check(metadata: Any) -> bool:
    """Whether the step is an issue run's requirements check, not a review.

    Held like a gated review, and decided the other way round: it runs only
    when the review it stands beside would be skipped.
    """
    block = _mapping(_mapping(metadata).get(REVIEW_GATE_METADATA_KEY))
    return block.get(REVIEW_GATE_REQUIREMENTS_CHECK) is True


#: The ends of a requirements check that confirm nothing and fail nothing.
CHECK_ENDS_WITHOUT_LIST = ("FAILED", "DEAD_LETTERED")


def check_ended_without_list(task: Mapping[str, Any]) -> bool:
    """A requirements check that FAILED or was DEAD_LETTERED (#2524).

    A FAILED CHECK IS NOT A FAILED RUN. It times out, runs out of attempts or
    its agent errors: then it confirmed nothing, and reads as "no list". The
    review beside it still ends as the gate decides, so a skipped review
    publishes "part of #N" as it did before the check existed. The scheduler
    agrees (`scheduler.reviewgate.check_ended_without_list`): it neither
    cancels the review over it nor fails the workflow. A check somebody
    CANCELLED is a stop, and still takes the review with it.
    """
    return (is_requirements_check(_mapping(task).get("metadata"))
            and _mapping(task).get("state") in CHECK_ENDS_WITHOUT_LIST)


def workspace_enabled(setting: Any) -> bool:
    """False only when the workspace switched the default gate off (`enabled: false`)."""
    return _mapping(setting).get("enabled") is not False


def policy_of(metadata: Any, workspace: Any = None) -> Gate | None:
    """The step's gate, or None for a step with none.

    The document is tenant-writable, so a block this reader cannot make sense
    of is no gate at all: the review runs, as it did before gates existed.
    A default block (`is_default`) takes the workspace's `min_lines` and
    `risk_paths` (`workspace`, `workspace_settings/{tenant}.review_gate`)
    when it sets them, else the platform's; a workspace value this reader
    cannot make sense of is no gate either. Whether the workspace switched
    the default off is `workspace_enabled`'s, asked first.
    """
    block = metadata.get(REVIEW_GATE_METADATA_KEY) if isinstance(metadata, Mapping) else None
    if not isinstance(block, Mapping) or "resolved" in block:
        return None
    if block.get(DEFAULT_MARKER) is True:
        setting = workspace if isinstance(workspace, Mapping) else {}
        return _gate_from({k: setting[k] for k in ("min_lines", "risk_paths") if k in setting})
    return _gate_from(block)


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
        if self.reason == REASON_OFF:
            return "review runs: the workspace's default review gate is off"
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
        if is_requirements_check(_mapping(document).get("metadata")):
            # An issue run's requirements check is read-only and changes
            # nothing the review would read; it builds on a parent read anyway.
            visited.pop()
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


def verdict_text(reason: str, requirements: list[dict[str, Any]] | None = None) -> str:
    """The verdict.json a skipped review leaves: a MERGE naming why.

    With the requirements check's `requirements` (an issue run, #2524), the
    list is carried as a review's is, so `issueruns.requirements_finding`
    reads it exactly as after a full review. The verdict is always the
    gate's MERGE: the check confirms requirements, it never reviews.
    """
    document: dict[str, Any] = {"verdict": "MERGE", "findings": [], "skipped": reason}
    if requirements is not None:
        document["requirements"] = requirements
    return json.dumps(document, sort_keys=True) + "\n"


def requirements_from(text: str | None) -> list[dict[str, Any]] | None:
    """The `requirements` list of a requirements check's requirements.json, or None.

    Only that list is taken, and only entries of the shape a review writes
    (`index`, `met`, `note`); a `verdict`, `findings` or any other key the
    file holds is ignored, so nothing the check writes is read as a verdict.
    Whether the list is complete is `issueruns.requirements_finding`'s to
    judge, strictly, as for a review's.
    """
    if not isinstance(text, str):
        return None
    try:
        document = json.loads(text)
    except ValueError:
        return None
    entries = _mapping(document).get("requirements")
    if not isinstance(entries, list):
        return None
    kept: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, Mapping) or set(entry) - {"index", "met", "note"}:
            return None
        kept.append({k: entry[k] for k in ("index", "met", "note") if k in entry})
    return kept


def _fitted(reason: str, requirements: list[dict[str, Any]] | None) -> str:
    """`verdict_text`, the notes shortened until it fits `MAX_INLINE_BYTES`;
    without the list when even no note fits, which reads as not all met."""
    if requirements is None:
        return verdict_text(reason)
    for chars in FOLDED_NOTE_CHARS:
        cut = [{**entry, **({"note": entry["note"][:chars]}
                            if isinstance(entry.get("note"), str) else {})}
               for entry in requirements]
        text = verdict_text(reason, cut)
        if len(text.encode("utf-8")) <= MAX_INLINE_BYTES:
            return text
    return verdict_text(reason)


def skip_summary(decision: Decision,
                 requirements: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """The `result_summary` a skipped review is ended with.

    The platform's only: the step never ran, so there is nothing of a
    worker's in it to keep. `requirements`, an issue run's requirements
    check's list, is folded into the verdict (`verdict_text`).
    """
    text = _fitted(decision.text, requirements)
    return {
        REVIEW_SKIPPED_KEY: decision.text,
        REVIEW_GATE_METADATA_KEY: decision.to_detail(),
        "artifacts": [{
            "name": VERDICT_FILE,
            "bytes": len(text.encode("utf-8")),
            INLINE_ARTIFACT_KEY: text,
            "written_by": INLINE_WRITER,
            "content_type": "application/json",
        }],
    }


#: `result_summary` key of a requirements check the review made unneeded.
REQUIREMENTS_CHECK_KEY = "requirements_check"


def check_summary(decision: Decision | None, text: str) -> dict[str, Any]:
    """The `result_summary` of a requirements check ended without an agent:
    the review runs, and answers the requirements itself. Not
    `result_summary.skipped`, for the reason `skip_summary` gives."""
    return {REQUIREMENTS_CHECK_KEY: {
        "needed": False, "text": text,
        **({REVIEW_GATE_METADATA_KEY: decision.to_detail()} if decision is not None else {}),
    }}


def with_resolved(metadata: Mapping[str, Any], decision: Decision, at: Any) -> dict[str, Any]:
    """`metadata` with `review_gate.resolved` recording the decision, the block kept."""
    out = dict(metadata)
    block = dict(_mapping(out.get(REVIEW_GATE_METADATA_KEY)))
    block["resolved"] = {**decision.to_detail(), "at": at}
    out[REVIEW_GATE_METADATA_KEY] = block
    return out


def gated(metadata: Any) -> bool:
    """The steps the scheduler holds for this gate: any carrying a `review_gate` block.

    Restated as part of `scheduler.loop.held_for_control_publish`; the two
    are held equal over the same documents by test_review_gate.py. A block
    `policy_of` cannot read is still held, and then declined: the review runs.
    """
    return isinstance(_mapping(metadata).get(REVIEW_GATE_METADATA_KEY), Mapping)


def inline_verdict(result_summary: Any, entry: Mapping[str, Any]) -> str | None:
    """The text of the inline verdict a skipped review carries, or None for any other entry.

    ONLY the one shape `skip_summary` writes: `verdict.json`,
    `written_by: "control_plane"`, no `uri`, at most `MAX_INLINE_BYTES`, on a
    task whose `result_summary.review_skipped` says the gate skipped it.
    `agent_worker.inputs.inline_artifact` admits exactly the same entries.
    The task document is tenant-writable, so this admits nothing a tenant
    could not already write on its own task, and what it returns is served
    through the same redaction as a bucket read.
    """
    text = entry.get(INLINE_ARTIFACT_KEY)
    if (entry.get("name") != VERDICT_FILE or entry.get("written_by") != INLINE_WRITER
            or entry.get("uri") or not isinstance(text, str)):
        return None
    if not isinstance(_mapping(result_summary).get(REVIEW_SKIPPED_KEY), str):
        return None
    return text if len(text.encode("utf-8")) <= MAX_INLINE_BYTES else None


# ---------------------------------------------------------------------------
# The workspace's default, settable as the other workspace defaults are
# (`qasettings`): `workspace_settings/{tenant}.review_gate`, read by
# `verdictpublish` when it decides a review step stored with the default.
# ---------------------------------------------------------------------------

#: The audit row of a change.
AUDIT_ACTION = "workspace_review_gate_set"
SET_AT = "review_gate_set_at"
SET_BY = "review_gate_set_by"


def stored_setting(doc: Mapping[str, Any] | None) -> dict[str, Any]:
    """The workspace's block as this module could have written it, else {}.

    Range-checked as it is read, because the document has other writers:
    a value `policy_of` could not use is no setting, and the platform's
    default applies.
    """
    block = _mapping(_mapping(doc).get(WORKSPACE_FIELD))
    if set(block) - set(WORKSPACE_KEYS):
        return {}
    if "enabled" in block and not isinstance(block["enabled"], bool):
        return {}
    if _gate_from({k: block[k] for k in ("min_lines", "risk_paths") if k in block}) is None:
        return {}
    return dict(block)


def checked_setting(body: Mapping[str, Any]) -> dict[str, Any]:
    """A PUT body as stored: `enabled`, `min_lines`, `risk_paths`, each optional;
    a 422 (`invalid_review_gate`) for anything a step's own block would be
    refused for (`validation.resolve_review_gate`'s rules)."""
    from .validation import ReviewGateInvalid, _checked_review_gate

    unknown = sorted(set(body) - set(WORKSPACE_KEYS))
    if unknown:
        raise ReviewGateInvalid(
            f"a workspace's review gate takes only {list(WORKSPACE_KEYS)}",
            detail={"field": WORKSPACE_FIELD, "unknown": unknown})
    out: dict[str, Any] = {}
    if body.get("enabled") is not None:
        if not isinstance(body["enabled"], bool):
            raise ReviewGateInvalid("review_gate.enabled must be true or false",
                                    detail={"field": f"{WORKSPACE_FIELD}.enabled"})
        out["enabled"] = body["enabled"]
    policy = {k: body[k] for k in ("min_lines", "risk_paths") if body.get(k) is not None}
    if policy:
        checked = _checked_review_gate({REVIEW_GATE_METADATA_KEY: policy}, step_id=None,
                                       workflow_step=True) or {}
        out.update({k: checked[k] for k in policy})
    return out


def setting_view(tenant_id: str, doc: Mapping[str, Any], *, editable: bool) -> dict[str, Any]:
    """What GET and PUT answer: the policy in force, the platform's, who set it."""
    stored = stored_setting(doc)
    set_at = doc.get(SET_AT) if stored else None
    set_by = doc.get(SET_BY) if stored else None
    return {
        "tenant_id": tenant_id,
        WORKSPACE_FIELD: {
            "enabled": workspace_enabled(stored),
            "min_lines": stored.get("min_lines", DEFAULT_MIN_LINES),
            "risk_paths": list(stored.get("risk_paths") or []),
        },
        "defaults": {"enabled": True, "min_lines": DEFAULT_MIN_LINES,
                     "risk_paths": list(DEFAULT_RISK_PATHS)},
        "set_at": set_at.isoformat() if hasattr(set_at, "isoformat") else None,
        "set_by": set_by if isinstance(set_by, str) else None,
        "can_edit": editable,
    }


class ReviewGateSettingsStore:
    """`workspace_settings/{tenant}.review_gate`, read and audited write."""

    def __init__(self, db: Any, *, now: Callable[[], Any]) -> None:
        self._db = db
        self._now = now

    def _ref(self, tenant_id: str) -> Any:
        return self._db.collection(WORKSPACE_SETTINGS).document(tenant_id)

    def document(self, tenant_id: str) -> dict[str, Any]:
        snap = self._ref(tenant_id).get()
        return (snap.to_dict() or {}) if snap.exists else {}

    def put(self, tenant_id: str, value: dict[str, Any], *, by: str) -> None:
        """Store `value` and its audit in one transaction; the same value again writes nothing."""
        from google.cloud import firestore
        from swarm_common.admission import _snapshot

        from .admins import AUDIT_COLLECTION

        ref = self._ref(tenant_id)
        audit = self._db.collection(AUDIT_COLLECTION).document()
        now = self._now()
        transaction = self._db.transaction()

        @firestore.transactional
        def _write(txn: Any) -> None:
            snap = _snapshot(txn.get(ref))
            previous = stored_setting(snap.to_dict() if snap.exists else None)
            if previous == value:
                return
            fields = {"tenant_id": tenant_id, WORKSPACE_FIELD: value, SET_AT: now, SET_BY: by}
            # `update` replaces the whole map, where a merging `set` would
            # keep a key the new value leaves out; the document's other
            # fields are other features' and are kept either way.
            if snap.exists:
                txn.update(ref, fields)
            else:
                txn.set(ref, fields)
            txn.set(audit, {
                "action": AUDIT_ACTION, "target_tenant_id": tenant_id, "by": by, "at": now,
                "detail": {"tenant_id": tenant_id, "from": previous, "to": value},
            })

        _write(transaction)
