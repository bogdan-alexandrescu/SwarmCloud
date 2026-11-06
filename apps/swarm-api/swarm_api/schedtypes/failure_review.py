"""`failure-review`: root-cause every failure of the last 7 hours and run its fix
(docs/schedules.md §3.16, docs/design/failure-review.md; lane FR-3 TYPE, part of #1933).

The owner's request (BGD, 2026-10-22): "run every 6 hrs and investigate all of
the agents and workflows that failed in the past 7 hrs ... Propose a solution
or a fix and, for each proposed solution, dispatch a workflow to design, plan,
implement, review test and merge each fix".

WHAT A FIRING DOES (the `observer`'s pattern: the tick never waits on an agent).

  1. TAKE IN every earlier firing whose investigator has ended. Its `rca.json`
     (read through the API's own masked artifact reader) is validated against
     the digest that firing handed over (`failurecauses.validate_rca`, §4):
     every reviewed failure accounted for exactly once, every cause with
     evidence. A file that fails, or an investigator that did not succeed,
     FILES NOTHING: the review ends `failed` with why, and its window is
     reviewed again by the next firing (§2, §9). A valid file is acted on,
     cause by cause (`failurecauses.decide`, §5, §6):

       * a NEW platform cause is filed in the bug form in the platform's own
         repository and gets ONE fix run (issue-sweep's `start_run`, the
         schedule's gate deciding plan and merge);
       * a new cause in a HOLD category (`iam`, `contract`, `secrets`,
         `tenancy`, `shared`, `cost`, or a stop `issueruns.hard_stop_hold`
         finds in its files and fix) is filed `needs-owner` and gets NO run;
       * a cause ALREADY FILED gets one comment per firing with its new
         failures; a run only when none is live and fewer than two were made;
         past two it is held for the owner;
       * a cause in ANOTHER REPOSITORY'S OWN CODE is a finding in that
         tenant (`failure_findings`), never an issue here (§1);
       * a cause found again with NO NEW FAILURE (the hour two windows share)
         files, comments and runs nothing;
       * a cause the investigator marks `action: none` (no code change: fixed
         on main already, or a process cause) is recorded `no_change` with its
         why and listed in the summary, and files and runs nothing -- until it
         recurs, when it is a `fix` (#2003);
       * a cause marked `action: existing` is commented and run on that OPEN
         issue of the platform's own repository; any other number is refused
         and the cause filed new, the reason in the summary. A hold wins over
         either.

     Then one summary (§8): on the review record, and as one comment on the
     schedule's `summary_issue` when it names an issue in the platform's own
     repository.
  2. GATHER AND BUILD THE DIGEST (`failuredigest`, lane FR-2) over this
     firing's window: `[fire - 7 h, fire)`, or from the last SUCCEEDED
     review's end minus the hour of overlap, capped 48 h back. Skip rules
     before any spend; content only for the platform's own agents (owner
     decision Q1). With nothing to review the summary says "0 failures in the
     window" and no agent runs.
  3. SUBMIT ONE `claude-code` INVESTIGATOR in the platform tenant, on the
     SwarmCloud repository (the schedule's tenant's registration marked
     `platform: true`), strategy `collect`: it changes nothing, it writes
     `rca.json` and `report.md`. The digest is in its prompt as DATA between
     nonce lines. It carries this firing's schedule mark, so its own failure
     is the digest's `self` rule and never reviewed (§9).

AS WHOM (§1). swarm-api reads every tenant's records (the digest) and writes
`failure_reviews`, `failure_causes`, `failure_findings`; issues, comments and
runs are made in the PLATFORM tenant -- the schedule's -- with its forge slot
(`swarm-tenant-<platform tenant>-git`) and as the schedule's owner. No other
tenant's credential is read; no other tenant's repository is written.

EXACTLY ONCE. A review's progress is recorded cause by cause; the issue is
found by its marker before one is filed (`failurecauses.marker`), a comment by
its marker before one is posted, and a fix run by the live runs on its issue
before one is started. A retried firing, or the next one after a crash, carries
on where the last stopped and writes nothing twice.

THE GATE (owner decision Q8). The type's default is `merge: approve`, and
nothing here sets `auto`: the owner switches it once through the audited merge
switch. Every hold of FR-1 still stops a run at plan and at merge.

THE BUDGET (owner decision Q9): tripwires, not limits on fix runs --
`per_run_usd` 200, `per_day_usd` 1000, `max_concurrent` 1 (one firing at a
time). Fix runs are not the firing's work items: they hold no place in
`max_concurrent`, and a firing does not stay live while they run.

INVARIANTS. The investigator and each fix run's planner are ordinary QUEUED
tasks admitted like any other (1-3); the profile is named here (10); every
cross-tenant read is the digest's, platform-authored fields only for other
tenants (9), and a tenant's finding holds only its own failures.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Sequence

from google.cloud.firestore_v1.base_query import FieldFilter

from .. import failurecauses, failuredigest, refusals
from ..attemptreads import WORKER_LOG_VIEW_ENV
from ..errors import ApiError, ValidationFailed
from ..failurecauses import Action, Cause, FailureCauses, Facts
from ..forge import read_open_work
from ..forgechecks import evaluate, required_status_checks
from ..forgewrite import ForgeWriteError, ForgeWriteNotFound, GitHubWriter
from ..issueci import excerpt_at
from ..issuecomments import bounded as bounded_body
from ..issuecomments import code, plain, prose
from ..issueruns import TERMINAL_RUN_STATES, IssueRuns, RunState, hard_stop_hold, refuse_auto_merge
from ..repositories import Repositories, hard_stop_paths_of, platform_of
from ..routes.runs import start_run
from ..schemas import TaskCreate
from ..scheduletypes import BudgetCaps
from ..validation import MERGE_WAIT_METADATA_KEY, SCHEDULE_METADATA_KEY, IssueRef
from . import issue_sweep

log = logging.getLogger(__name__)

TYPE = "failure-review"

#: Owner decision Q9, 2026-10-22: "Tripwires: per_run_usd 200, per_day_usd
#: 1000, max_concurrent 1". Far above measured spend, they stop only a
#: runaway; `max_concurrent` 1 is one firing at a time, NOT a fix-run limit
#: (the owner's "no cap" on fix runs stands, §7). A schedule may set lower
#: figures; raising them is a change to this line, in a pull request.
BUDGET_CAPS = BudgetCaps(per_run_usd=200, per_day_usd=1000, max_concurrent=1)

#: The investigator's profile, named here and nowhere else (invariant 10).
PROFILE = "claude-code"
#: It changes nothing in the repository: its answer is its artifacts.
STRATEGY = "collect"
RCA_FILE = "rca.json"
REPORT_FILE = "report.md"
#: The worker's Proposal J file: no change plus this is `no_change`, a success.
VERIFICATION_FILE = "verification.md"
#: §4: at most 16 child agents, depth 1 (docs/design/child-tasks.md).
MAX_CHILDREN = 16

#: One record per firing: what it handed over, what it found, what it did.
REVIEWS = "failure_reviews"
#: One small record per schedule: the end of its last SUCCEEDED review's
#: window, where the next window starts after a failed one (§2).
CURSORS = "failure_review_cursors"
#: §1: a cause in a tenant's own repository, held in that tenant.
FINDINGS = "failure_findings"
TASKS = "tasks"

INVESTIGATING, FILING, FILED, FAILED = "investigating", "filing", "filed", "failed"
OPEN_STATES = (INVESTIGATING, FILING)

#: The bug form's label (`.github/ISSUE_TEMPLATE/bug_report.yml`) and §6's hold label.
BUG_LABEL = "bug"
HOLD_LABEL = "needs-owner"

#: An investigator that is not found this long after its firing (its submission
#: was lost) ends its review `failed`, so the window moves back to be read again.
LOST_AFTER = timedelta(hours=48)
#: Reviews taken in per firing, oldest first.
MAX_INTAKE = 8
#: Bounds on what a body quotes of the investigator's text.
MAX_TITLE_CHARS = 200
MAX_FIELD_CHARS = 4000
MAX_EVIDENCE = 10
MAX_IDS_LISTED = 30

#: §6's S0: the guarantees this platform exists to keep (CLAUDE.md "Issues").
S0_HOLDS = frozenset({"tenancy", "shared", "secrets"})
_S0_WORDS = re.compile(r"capacity|admission|fenc|tenant[ _-]?isolation|secret|deny[ _-]?list", re.I)
SEVERITY_S0 = ("S0 — a platform guarantee at risk (tenant isolation, the other team's resources, "
               "over-admission or a slot released under a running worker, a stale generation that "
               "ran, a secret printed)")
SEVERITY_S1 = "S1 — blocked: I cannot do the thing at all"
#: The form's "Where you saw it" options, by the cause's component.
SURFACES = (
    ("scheduler", "Scheduler — admission and dispatch (apps/scheduler)"),
    ("reconciler", "Reconciler — lease repair and reclaim (apps/reconciler)"),
    ("quota", "Quota broker (apps/quota-broker)"),
    ("swarm-api", "API — /v1, including sign-in and tenant resolution (apps/swarm-api)"),
    ("api", "API — /v1, including sign-in and tenant resolution (apps/swarm-api)"),
    ("browser", "Worker on GKE — browser (apps/agent-worker, images/agent-runtime-browser, kubernetes)"),
    ("worker", "Worker on Cloud Run (apps/agent-worker, images/agent-runtime-base)"),
    ("terraform", "Terraform or infrastructure (terraform)"),
    ("ci", "Release pipeline or CI (.github/workflows)"),
    ("release", "Release pipeline or CI (.github/workflows)"),
    ("script", "Scripts or make targets (scripts, Makefile)"),
)
NOT_SURE = "Not sure"

_UNSAFE_MARK = re.compile(r"[^A-Za-z0-9_-]")


class FailureReviewNoPlatformRepository(ValidationFailed):
    code = "failure_review_no_platform_repository"


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def _aware(moment: Any) -> datetime | None:
    if not isinstance(moment, datetime):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def mark_id(firing_id: str) -> str:
    """A firing id as a marker carries it: `failurecauses`' marker grammar has no `:`."""
    return _UNSAFE_MARK.sub("-", str(firing_id))


def summary_marker(firing_id: str) -> str:
    return f"<!-- failure-review summary firing={mark_id(firing_id)} -->"


def finding_id(fingerprint: str, tenant_id: str, failure_id: str) -> str:
    """One finding per cause, tenant and failed agent: found again, never written twice."""
    digest = hashlib.sha256(f"{fingerprint}|{tenant_id}|{failure_id}".encode()).hexdigest()
    return f"ff_{digest[:20]}"


def platform_registration(ctx: Any, tenant_id: str) -> dict[str, Any] | None:
    """The schedule's tenant's registration marked `platform: true` (SD7, §1)."""
    repos = Repositories(ctx.db, now=ctx.now)
    token: str | None = None
    while True:
        page, token = repos.list(tenant_id, limit=100, page_token=token)
        for row in page:
            if row.get("tenant_id") == tenant_id and platform_of(row) and not row.get("archived"):
                return dict(row)
        if not token:
            return None


def _require_platform(firing: Any) -> dict[str, Any] | None:
    """The platform registration; None when there is none and the refusal is off.

    A new refusal ships report-only (refusals.py): off, the firing is let
    through with nothing reviewed and the log names the refusal.
    """
    tenant_id = str(firing.schedule["tenant_id"])
    registration = platform_registration(firing.ctx, tenant_id)
    if registration is None:
        refusals.refuse(FailureReviewNoPlatformRepository(
            "failure-review runs in the tenant that registered the SwarmCloud repository with "
            "platform: true, and this schedule's tenant has no such registration",
            detail={"tenant_id": tenant_id},
        ))
    return registration


def _slug(registration: Mapping[str, Any]) -> str:
    return f"{registration.get('owner')}/{registration.get('repo')}"


def _ref(registration: Mapping[str, Any], number: int = 0) -> IssueRef:
    return IssueRef(owner=str(registration["owner"]), repo=str(registration["repo"]), number=number)


# --------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------


def _review_ref(db: Any, firing_id: str) -> Any:
    return db.collection(REVIEWS).document(str(firing_id))


def _save(db: Any, review: dict[str, Any], now: datetime, **patch: Any) -> dict[str, Any]:
    review.update(patch)
    review["updated_at"] = now
    _review_ref(db, review["firing_id"]).set(review)
    return review


def last_succeeded_until(db: Any, schedule_id: str) -> datetime | None:
    snap = db.collection(CURSORS).document(str(schedule_id)).get()
    return _aware((snap.to_dict() or {}).get("until")) if snap.exists else None


def _advance_cursor(db: Any, review: Mapping[str, Any], now: datetime) -> None:
    ref = db.collection(CURSORS).document(str(review["schedule_id"]))
    snap = ref.get()
    held = _aware((snap.to_dict() or {}).get("until")) if snap.exists else None
    until = _aware(review.get("until"))
    if until is not None and (held is None or until > held):
        ref.set({"schedule_id": review["schedule_id"], "tenant_id": review["tenant_id"],
                 "until": until, "firing_id": review["firing_id"], "updated_at": now})


def open_reviews(db: Any, schedule: Mapping[str, Any]) -> list[dict[str, Any]]:
    """This schedule's reviews still to take in, oldest first (equality filters only)."""
    rows: list[dict[str, Any]] = []
    for state in OPEN_STATES:
        query = (db.collection(REVIEWS)
                 .where(filter=FieldFilter("schedule_id", "==", schedule["schedule_id"]))
                 .where(filter=FieldFilter("state", "==", state)))
        rows += [snap.to_dict() or {} for snap in query.stream()]
    rows = [r for r in rows if r.get("tenant_id") == schedule["tenant_id"]
            and r.get("schedule_id") == schedule["schedule_id"] and r.get("state") in OPEN_STATES]
    rows.sort(key=lambda r: (_aware(r.get("until")) or datetime.min.replace(tzinfo=timezone.utc),
                             str(r.get("firing_id"))))
    return rows[:MAX_INTAKE]


def investigator(db: Any, review: Mapping[str, Any]) -> dict[str, Any] | None:
    """The firing's investigator: its task marked with the firing, in the schedule's tenant."""
    tenant_id = review["tenant_id"]
    query = (db.collection(TASKS)
             .where(filter=FieldFilter("tenant_id", "==", tenant_id))
             .where(filter=FieldFilter(f"metadata.{SCHEDULE_METADATA_KEY}.firing_id", "==",
                                       review["firing_id"])))
    rows = [snap.to_dict() or {} for snap in query.stream()]
    rows = [r for r in rows if r.get("tenant_id") == tenant_id
            and _mapping(_mapping(r.get("metadata")).get(SCHEDULE_METADATA_KEY)).get("firing_id")
            == review["firing_id"]
            and _mapping(r.get("metadata")).get("failure_review") == review["firing_id"]
            and not r.get("parent_task_id")]
    rows.sort(key=lambda r: str(r.get("id")))
    return rows[0] if rows else None


# --------------------------------------------------------------------------
# The digest and the investigator (§2, §3, §4)
# --------------------------------------------------------------------------


def scope_of(schedule: Mapping[str, Any], registration: Mapping[str, Any]) -> failuredigest.Scope:
    """Owner decision Q1: the platform tenant's agents, and agents on the SwarmCloud repository."""
    return failuredigest.Scope(platform_tenant=str(schedule["tenant_id"]),
                               platform_repositories=frozenset({_slug(registration).lower()}))


def _ci_excerpt(ctx: Any, token: Callable[[], str]) -> Callable[[Mapping[str, Any]], str | None]:
    """A merge step's CI excerpt at the head it parked on (§3), with the platform's slot.

    Asked only for the platform's own agents (the digest's rule), so the
    platform tenant's credential reads only the platform's own repository.
    """
    def read(task: Mapping[str, Any]) -> str | None:
        wait = _mapping(_mapping(task.get("metadata")).get(MERGE_WAIT_METADATA_KEY))
        number, head = wait.get("pull_request"), wait.get("head")
        slug = failuredigest.repository_slug(task.get("repository_url"))
        if not (isinstance(number, int) and not isinstance(number, bool) and number > 0
                and isinstance(head, str) and slug):
            return None
        owner, _, repo = slug.partition("/")
        ref = IssueRef(owner=owner, repo=repo, number=number)
        writer: GitHubWriter = ctx.forge_writer
        secret = token()
        pull = writer.read_pull(ref, number, secret)
        rules = writer.branch_rules(ref, pull.base_ref, secret) if pull.base_ref else []
        reading = evaluate(required_status_checks(rules),
                           writer.check_runs(ref, head, secret, head_ref=pull.head_ref),
                           writer.commit_statuses(ref, head, secret))
        if not reading.failing_runs and not reading.failing_statuses:
            return None
        return excerpt_at(writer, ref, head, reading, secret, label=f"failure-review {task.get('id')}")
    return read


def _token_reader(ctx: Any, tenant_id: str) -> Callable[[], str]:
    held: dict[str, str] = {}

    def token() -> str:
        if "token" not in held:
            tenant = ctx.store.get_tenant(tenant_id)
            if tenant is None:
                raise ApiError(f"tenant {tenant_id} is not registered")
            held["token"] = ctx.forge_tokens.token_for(tenant)
        return held["token"]
    return token


def content_for(ctx: Any, tenant_id: str) -> failuredigest.ApiContent:
    return failuredigest.ApiContent(
        inspection=ctx.inspection, log_view=os.environ.get(WORKER_LOG_VIEW_ENV, ""),
        ci_excerpt=_ci_excerpt(ctx, _token_reader(ctx, tenant_id)))


def _evidence_of(docs: failuredigest.Documents, refs: Mapping[str, Mapping[str, str]]) -> dict[str, Any]:
    """Per reference, what the platform wrote about that failure and nothing else:
    what a tenant's finding may quote of its own failure (§1)."""
    out: dict[str, Any] = {}
    for ref, where in refs.items():
        kind, doc_id = where.get("kind"), where.get("id")
        doc = (docs.tasks.get(doc_id) if kind == "agent" else
               docs.workflows.get(doc_id) if kind == "workflow" else docs.runs.get(doc_id)) or {}
        out[ref] = {
            "kind": kind,
            "state": doc.get("state"),
            "profile": doc.get("runner_profile"),
            "end_cause": doc.get("end_cause"),
            "last_error": failuredigest.redacted(doc.get("last_error") or doc.get("error"),
                                                 failuredigest.ERROR_BYTES),
        }
    return out


def digest_for(firing: Any, registration: Mapping[str, Any], *, content: Any = None
               ) -> tuple[failuredigest.Digest, failuredigest.Documents, list[dict[str, Any]]]:
    """This firing's window, read and built (§2, §3), with the known causes it names."""
    ctx = firing.ctx
    schedule = firing.schedule
    since, until = failuredigest.window(firing.now, last_succeeded_until(ctx.db, schedule["schedule_id"]))
    docs = failuredigest.gather(ctx.db, since=since, until=until)
    known = FailureCauses(ctx.db, now=ctx.now).known()
    digest = failuredigest.build(docs, since=since, until=until, firing_id=str(firing.firing["firing_id"]),
                                 scope=scope_of(schedule, registration), content=content, known=known)
    return digest, docs, known


def prompt(digest_text: str, firing_id: str, repository: str) -> str:
    """The investigator's brief: the digest as data, and §4's `rca.json` exactly."""
    return (
        f"You are SwarmCloud's failure reviewer, firing {firing_id}, working in a checkout of "
        f"{repository}, the SwarmCloud platform's own repository. Find the ROOT CAUSE of every "
        "failure in the digest below, exhaustively: trace each to a code site, not to a symptom "
        "(\"the reconciler's lease-silent reclaim fires before the OOM classifier reads the pod's "
        "status\", not \"the agent died\"). Read the code, your own tenant's records "
        "(`swarm-state`), and GitHub (`pr_checks`, `run_list`, `run_view`, `pr_view`, "
        f"`issue_list`). You may start at most {MAX_CHILDREN} child agents, one per candidate "
        "group beyond the first, depth 1, each given its group as data and asked for one cause "
        "entry with evidence; merge groups that share a cause and split one that has two.\n\n"
        "Do not change, create or delete any file in the repository, file no issue and start no "
        "run: SwarmCloud files the issues and starts the fix runs from your rca.json.\n\n"
        f"{failuredigest.as_data(digest_text)}\n"
        f"Write $SWARM_ARTIFACTS_DIR/{RCA_FILE}, one JSON object:\n"
        '{"firing_id": "' + firing_id + '", "causes": [{"fingerprint_of": {"component": "...", '
        '"site": "path/to/file.py::function", "class": "short_snake_case_class", "signature": '
        '"the error with numbers and ids as <n>"}, "known_fingerprint": null, "failures": '
        '["f-001"], "cause": "One sentence: what in the code made these fail, as a fact.", '
        '"evidence": [{"command": "what you ran or read", "result": "what it showed"}], "fix": '
        '"What to change, and the red-first test that proves it.", "files": ["path"], "hold": '
        '"none", "where": "platform", "action": "fix", "existing_issue": null}], "unexplained": [{"failure": "f-007", "tried": "what you '
        'read and why it did not settle it"}]}\n'
        "Rules: every reference (f-001, ...) in the digest, including those listed only by "
        "reference, appears in exactly ONE cause or in `unexplained`, and nothing else does. "
        "Every cause has at least one evidence entry. `known_fingerprint` is a fingerprint from "
        "the digest's known_causes when this is that cause again, else null. `hold` is one of "
        "none, iam, contract, secrets, tenancy, shared, cost: anything touching IAM, "
        "apps/common/swarm_common/, secrets, tenant isolation, the other team's resources in "
        "the shared project, or cost (sizes, budgets, ceilings) is held, never none. `where` "
        "is platform for a cause in SwarmCloud, tenant_repo for a cause in another "
        "repository's own code (a tenant's test or build). `action` is fix (SwarmCloud files an "
        "issue and starts a fix run), none (no code change is needed: the fix is already on "
        "main, or a process cause with no code site; `fix` then says why, with the evidence), "
        "or existing (the cause is an OPEN issue already in the platform repository: its number "
        "in `existing_issue`, and SwarmCloud comments and runs there instead of filing). Say "
        "it in `action`, never only in prose: prose is not read. A failure you cannot explain from "
        "what you were given (another tenant's content is not shared; a digest bound dropped "
        "its detail) goes in `unexplained` with why, never a guess.\n"
        f"Then write $SWARM_ARTIFACTS_DIR/{REPORT_FILE}: each cause with its evidence and fix, "
        f"each unexplained failure with why, and the counts. Last, write "
        f"$SWARM_ARTIFACTS_DIR/{VERIFICATION_FILE}: one paragraph saying this was a report-only "
        "failure review that changed no file, and how many causes and unexplained failures "
        f"{RCA_FILE} holds.\n"
    )


def investigator_task(registration: Mapping[str, Any], digest_text: str, firing_id: str) -> TaskCreate:
    return TaskCreate(
        runner_profile=PROFILE,
        input={"prompt": prompt(digest_text, firing_id, _slug(registration))},
        metadata={"failure_review": firing_id},
        repository_url=str(registration["repository_url"]),
        repository_ref=registration.get("default_branch") or None,
        strategy=STRATEGY,
    )


# --------------------------------------------------------------------------
# Rendering (§6 step 1: the bug form's sections, in its order)
# --------------------------------------------------------------------------


def severity(cause: Cause) -> str:
    if cause.hold in S0_HOLDS or _S0_WORDS.search(f"{cause.signature} {cause.cause}"):
        return SEVERITY_S0
    return SEVERITY_S1


def surface(component: str) -> str:
    text = component.lower()
    for word, option in SURFACES:
        if word in text:
            return option
    return NOT_SURE


def _failures_line(review: Mapping[str, Any], refs: Sequence[str]) -> str:
    """Agent ids for the platform's own failures; counts per tenant label for the rest (§5)."""
    platform = review["tenant_id"]
    labels = {v: k for k, v in _mapping(review.get("tenants")).items()}
    own: list[str] = []
    counts: dict[str, int] = {}
    for ref in refs:
        where = _mapping(_mapping(review.get("refs")).get(ref))
        if where.get("tenant_id") == platform:
            own.append(f"{where.get('kind')} `{where.get('id')}`")
        else:
            label = labels.get(str(where.get("tenant_id")), "another tenant")
            counts[label] = counts.get(label, 0) + 1
    parts = own[:MAX_IDS_LISTED]
    if len(own) > MAX_IDS_LISTED:
        parts.append(f"and {len(own) - MAX_IDS_LISTED} more of the platform's own")
    parts += [f"{n} in tenant {label}" for label, n in sorted(counts.items())]
    return "; ".join(parts) or "none"


def _window(review: Mapping[str, Any]) -> str:
    return f"{_mapping(review.get('window')).get('from')} to {_mapping(review.get('window')).get('to')}"


# `plain`, `prose` and `code` live in `issuecomments` now: every writer that
# posts to GitHub writes plain text, as this one did first (BGD, 2026-10-26,
# part of #2391).


def render_issue(review: Mapping[str, Any], cause: Cause, component: str, *, held: bool,
                 literals: Sequence[str] = ()) -> tuple[str, str]:
    def text(value: str, one_line: bool = False) -> str:
        return prose(value, MAX_FIELD_CHARS, literals=literals, one_line=one_line)

    title = plain(cause.cause, MAX_TITLE_CHARS, literals=literals, one_line=True)
    evidence = [f"- {code(e.command, MAX_FIELD_CHARS, literals=literals)}: {text(e.result, one_line=True)}"
                for e in cause.evidence[:MAX_EVIDENCE]]
    files = ", ".join(code(f, 300, literals=literals) for f in cause.files) or "none named"
    hold_line = (f"Held for the owner (`{HOLD_LABEL}`, category `{cause.hold}`): no fix run is started."
                 if held else "A fix run is started for this issue by the failure-review schedule.")
    body = "\n".join([
        failurecauses.marker(cause.fingerprint),
        "### What happened",
        "",
        f"{len(cause.failures)} failure(s) in the window {_window(review)} share one cause: {text(cause.cause)}",
        "",
        "### Severity",
        "",
        severity(cause),
        "",
        "### Where you saw it",
        "",
        surface(component),
        "",
        "### Environment",
        "",
        NOT_SURE,
        "",
        "### Steps to reproduce",
        "",
        f"Found by the failure-review schedule `{review['schedule_id']}`, firing `{review['firing_id']}`, "
        f"from the failures it reviewed: {_failures_line(review, cause.failures)}.",
        "",
        "### What you expected instead",
        "",
        "These agents end without this failure: the cause below is fixed in the code, with a "
        "test that fails before the fix and passes after it.",
        "",
        "### Identifiers",
        "",
        f"- Fingerprint: `{cause.fingerprint}`",
        f"- Signature: {code(cause.signature, 500, literals=literals)}",
        f"- Failures: {_failures_line(review, cause.failures)}",
        "",
        "### Screenshots, errors, logs",
        "",
        *evidence,
        "",
        "### Anything else",
        "",
        f"**Proposed fix.** {text(cause.fix)}",
        "",
        f"**Files.** {files}",
        "",
        hold_line,
        "",
    ])
    return title, bounded_body(body)


def render_comment(review: Mapping[str, Any], cause: Cause, new_failures: int, *, action: Action,
                   record: Mapping[str, Any], literals: Sequence[str] = ()) -> str:
    lines = [
        failurecauses.comment_marker(cause.fingerprint, mark_id(review["firing_id"])),
        f"Seen again by failure-review firing `{review['firing_id']}` over {_window(review)}: "
        f"{new_failures} new failure(s) ({_failures_line(review, cause.failures)}).",
    ]
    fixed_by = _mapping(record.get("fixed_by"))
    if fixed_by:
        lines += ["", f"This cause was marked fixed by run `{fixed_by.get('run_id')}` "
                      f"({fixed_by.get('pull_request') or 'its merge'}), and it recurred."]
    if action == Action.COMMENT_AND_RUN:
        lines += ["", "A new fix run is started for it."]
    elif action == Action.HOLD:
        lines += ["", f"Held for the owner (`{HOLD_LABEL}`): " + (
            f"category `{cause.hold}`." if cause.hold != "none" else
            f"{failurecauses.MAX_FIX_RUNS} fix runs did not hold.") + " No fix run is started."]
    else:
        lines += ["", "No new fix run: one is live, a pull request is open, or the issue was closed "
                      "as not planned."]
    for e in cause.evidence[:3]:
        lines.append(f"- {code(e.command, 300, literals=literals)}: "
                     f"{prose(e.result, 600, literals=literals, one_line=True)}")
    return bounded_body("\n".join(lines) + "\n")


# --------------------------------------------------------------------------
# Acting on one cause (§5, §6)
# --------------------------------------------------------------------------


class _Filing:
    """One review's filing: the platform registration, its credential and the stores."""

    def __init__(self, firing: Any, registration: Mapping[str, Any], review: dict[str, Any]) -> None:
        self.firing = firing
        self.ctx = firing.ctx
        self.registration = registration
        self.review = review
        self.causes = FailureCauses(self.ctx.db, now=self.ctx.now)
        self.runs = IssueRuns(self.ctx.db, now=self.ctx.now)
        self.writer: GitHubWriter = self.ctx.forge_writer
        self.token = _token_reader(self.ctx, str(review["tenant_id"]))
        #: Starts refused this time: a summary of this pass, the rest is `tally`'s.
        self.start_failures: list[dict[str, Any]] = []

    # -- the issue's state, read through the forge and the run store --

    def facts(self, record: Mapping[str, Any]) -> Facts:
        number = record.get("issue")
        if not number:
            return Facts()
        ref = _ref(self.registration, int(number))
        rows = self.runs.for_issue(str(self.review["tenant_id"]), ref)
        live = any(r.state not in TERMINAL_RUN_STATES for r in rows)
        open_pr = any(_mapping(r.pull_request).get("state") == "open"
                      and not _mapping(r.pull_request).get("merged") for r in rows)
        closed = None
        issue = self.writer.read_issue(ref, int(number), self.token())
        if issue.get("state") == "closed":
            closed = str(issue.get("state_reason") or failurecauses.CLOSED_COMPLETED)
        return Facts(live_run=live, open_pr=open_pr, issue_closed=closed)

    def refresh_fixed(self, record: dict[str, Any]) -> dict[str, Any]:
        """A cause whose last fix run merged is `fixed` (§5), so its recurrence says so."""
        if record.get("state") != failurecauses.FIXING:
            return record
        runs = [r for r in record.get("runs") or [] if isinstance(r, Mapping)]
        if not runs:
            return record
        try:
            run = self.runs.get(str(self.review["tenant_id"]), str(runs[-1].get("run_id")))
        except ApiError:
            return record
        pull = _mapping(run.pull_request)
        if run.state == RunState.DONE and pull.get("merged"):
            merge = {"run_id": run.id, "pull_request": pull.get("url")}
            self.causes.set_state(record["fingerprint"], failurecauses.FIXED, merge=merge)
            return {**record, "state": failurecauses.FIXED, "fixed_by": merge}
        return record

    # -- the writes, each found again before it is made --

    def ensure_issue(self, cause: Cause, component: str, *, held: bool) -> int:
        record = self.causes.get(cause.fingerprint) or {}
        if record.get("issue"):
            return int(record["issue"])
        token = self.token()
        ref = _ref(self.registration)
        found = self.writer.find_issue(ref, failurecauses.marker(cause.fingerprint), token)
        if found is None:
            title, body = render_issue(self.review, cause, component, held=held, literals=(token,))
            labels = (BUG_LABEL, HOLD_LABEL) if held else (BUG_LABEL,)
            found = self.writer.create_issue(ref, title=title, body=body, labels=labels, token=token)
        self.causes.record_issue(cause.fingerprint, found.number)
        return found.number

    def comment(self, cause: Cause, number: int, new_failures: int, action: Action,
                record: Mapping[str, Any]) -> None:
        token = self.token()
        ref = _ref(self.registration, number)
        marker = failurecauses.comment_marker(cause.fingerprint, mark_id(self.review["firing_id"]))
        if self.writer.find_comment(ref, marker, token) is None:
            self.writer.create_comment(ref, render_comment(self.review, cause, new_failures, action=action,
                                                           record=record, literals=(token,)), token)

    def hold_label(self, number: int) -> None:
        """§6 step 2 for a cause held LATER (`HOLD`): its issue already exists, so
        `needs-owner` is added to it beside the comment, and issue-sweep's
        default `labels_exclude` keeps the sweep off it. A label that fails is
        logged: the cause stays held, and the comment already says why."""
        ref = _ref(self.registration, number)
        try:
            dropped = self.writer.add_labels(ref, number, [HOLD_LABEL], self.token())
        except ForgeWriteError as exc:
            log.warning("failure-review %s: #%s held but not labelled %s (%s)",
                        self.review["firing_id"], number, HOLD_LABEL, exc.code)
            return
        if dropped:
            log.warning("failure-review %s: #%s held but not labelled %s: the repository has no such label",
                        self.review["firing_id"], number, HOLD_LABEL)

    def start(self, cause: Cause, number: int) -> str | None:
        """One fix run on the issue (§6 step 3), unless one of this schedule's is already live."""
        tenant_id = str(self.review["tenant_id"])
        schedule_id = str(self.firing.schedule["schedule_id"])
        ref = _ref(self.registration, number)
        for row in self.runs.for_issue(tenant_id, ref):
            if row.state not in TERMINAL_RUN_STATES and (row.schedule or {}).get("schedule_id") == schedule_id:
                # Made by an earlier attempt at this filing and not recorded: adopted.
                self.causes.record_run(cause.fingerprint, row.id, firing_id=self.review["firing_id"])
                return row.id
        params = issue_sweep.params_of(self.firing)
        options = issue_sweep.run_options(self.firing.schedule, params.fix_rounds)
        try:
            refuse_auto_merge(options.auto_merge)
            tenant = self.ctx.store.get_tenant(tenant_id)
            open_work = read_open_work(ref, tenant, tokens=self.ctx.forge_tokens, issues=self.ctx.forge,
                                       read_at=self.ctx.now(), person=self.firing.owner.email)
            run = start_run(
                self.ctx, self.firing.owner, ref, open_work=open_work,
                plan_approval=options.plan_approval, auto_merge=options.auto_merge,
                fix_rounds=options.fix_rounds, created_by=f"schedule:{schedule_id}",
                schedule=self.firing.mark, merge_approval=options.merge_approval,
            )
        except ApiError as exc:
            if exc.status_code >= 500:
                raise
            self.start_failures.append({"issue": number, "error": exc.code})
            log.warning("failure-review %s: fix run on #%s not started (%s)",
                        self.review["firing_id"], number, exc.code)
            return None
        self.causes.record_run(cause.fingerprint, run.id, firing_id=self.review["firing_id"])
        return run.id

    def report(self, cause: Cause) -> int:
        """§1: a finding in each failed agent's own tenant, quoting only that tenant's failure."""
        db = self.ctx.db
        refs = _mapping(self.review.get("refs"))
        evidence = _mapping(self.review.get("evidence"))
        made = 0
        for ref in cause.failures:
            where = _mapping(refs.get(ref))
            tenant_id, failure = str(where.get("tenant_id") or ""), str(where.get("id") or "")
            if not tenant_id or not failure:
                continue
            fid = finding_id(cause.fingerprint, tenant_id, failure)
            doc_ref = db.collection(FINDINGS).document(fid)
            if doc_ref.get().exists:
                continue
            doc_ref.set({
                "finding_id": fid, "tenant_id": tenant_id, "kind": where.get("kind"),
                "task_id": failure if where.get("kind") == "agent" else None,
                "failure_id": failure, "fingerprint": cause.fingerprint,
                # Plain, as written (FAILURE-STORE-PLAIN, part of #2391): the
                # console shows these through React, which escapes them itself,
                # so a stored `&gt;` read '-&gt;'. Masked and neutralised still.
                "cause": plain(cause.cause, 2000, one_line=True),
                "fix": plain(cause.fix, MAX_FIELD_CHARS),
                "evidence": dict(_mapping(evidence.get(ref))),
                "schedule_id": self.review["schedule_id"], "firing_id": self.review["firing_id"],
                "state": "open", "created_at": self.ctx.now(),
            })
            made += 1
        return made

    # -- one cause --

    @staticmethod
    def intent_of(cause: Cause, before: Mapping[str, Any] | None) -> str:
        """§4's `action`, as this firing will act on it (#2003). Decided once, with
        the record as it was BEFORE this firing observed the cause, and saved.

        A hold wins: a held cause, or one whose record is held, is `fix`
        (filed or commented `needs-owner`, no run). A `none` whose record was
        already `no_change` has recurred: the code was not fixed after all.
        """
        state = (before or {}).get("state")
        if cause.hold != "none" or state == failurecauses.HELD:
            return "fix"
        if cause.action == "none":
            return "fix" if state == failurecauses.NO_CHANGE else "none"
        return "existing" if cause.action == "existing" else "fix"

    def existing_refusal(self, number: int | None) -> str | None:
        """Why `existing_issue` is not an open issue of the platform's own
        repository, or None. The number is read only there: nothing is ever
        written to another repository. A refusal files the cause new."""
        slug = _slug(self.registration)
        if not number:
            return "action existing names no existing_issue"
        try:
            issue = self.writer.read_issue(_ref(self.registration, number), number, self.token())
        except ForgeWriteNotFound:
            return f"#{number} is not an issue in {slug}"
        where = str(issue.get("repository_url") or "")
        if where and not where.lower().endswith(f"/repos/{slug.lower()}"):
            return f"#{number} is not in {slug} (moved to another repository)"
        if issue.get("pull_request"):
            return f"#{number} is a pull request, not an issue"
        if issue.get("state") != "open":
            return f"#{number} is closed"
        return None

    def hold_of(self, cause: Cause) -> str:
        """§6 step 5 at proposal: the investigator's hold, widened by FR-1's stops over its files."""
        if cause.hold != "none":
            return cause.hold
        found = hard_stop_hold(cause.files, platform=True,
                               hard_stop_paths=hard_stop_paths_of(self.registration),
                               prompt_text=cause.fix)
        return str(found["reasons"][0]) if found else "none"

    def act(self, cause: Cause, component: str, failure_ids: list[str]) -> dict[str, Any]:
        progress = dict(_mapping(_mapping(self.review.get("progress")).get(cause.fingerprint)))
        if progress.get("done"):
            return progress
        cause = replace(cause, hold=self.hold_of(cause))
        if "new" not in progress:
            # What is new is decided and SAVED before the record counts it: a
            # crash between the two must not turn a new failure into one the
            # retry reads as already counted, and so never files.
            before = self.causes.get(cause.fingerprint)
            counted = set((before or {}).get("failure_ids") or [])
            progress = self._progress(cause.fingerprint, {
                "new": [f for f in dict.fromkeys(failure_ids) if f not in counted],
                "created": before is None, "intent": self.intent_of(cause, before)})
        if not progress.get("observed"):
            # Idempotent for one firing: a second observation counts nothing.
            self.causes.observe(cause, failure_ids=failure_ids, firing_id=self.review["firing_id"],
                                title=plain(cause.cause, MAX_TITLE_CHARS, one_line=True))
            progress = self._progress(cause.fingerprint, {**progress, "observed": True})
        if cause.where == "tenant_repo":
            return self._progress(cause.fingerprint, {**progress, "action": Action.REPORT.value,
                                                      "findings": self.report(cause), "done": True})
        if not progress["new"]:
            # The hour two windows share, or a retried firing: found, nothing new.
            return self._progress(cause.fingerprint, {**progress, "action": None, "done": True})
        record = self.refresh_fixed(self.causes.get(cause.fingerprint) or {})
        if "action" not in progress:
            # A progress saved before `intent` existed is today's `fix`.
            if progress.get("intent") == "existing" and not record.get("issue"):
                refused = self.existing_refusal(cause.existing_issue)
                if refused:
                    progress = self._progress(cause.fingerprint, {**progress, "intent": "fix", "existing_refused": {
                        "issue": cause.existing_issue, "reason": refused}})
                else:
                    # Recorded first: a retry finds the issue on the record and
                    # carries on as for any cause already filed.
                    self.causes.record_issue(cause.fingerprint, int(cause.existing_issue or 0))
                    record = {**record, "issue": int(cause.existing_issue or 0)}
                    progress = self._progress(cause.fingerprint, {**progress, "existing": record["issue"]})
            if progress.get("intent") == "none":
                action = Action.NO_CHANGE
            else:
                action = failurecauses.decide(record if record.get("issue") else None, hold=cause.hold,
                                              where=cause.where, facts=self.facts(record))
            progress = self._progress(cause.fingerprint, {**progress, "action": action.value})
        action = Action(progress["action"])
        if action == Action.NO_CHANGE:
            self.causes.record_no_change(cause.fingerprint, firing_id=self.review["firing_id"],
                                         fix=plain(cause.fix, MAX_FIELD_CHARS),
                                         evidence=[e.model_dump() for e in cause.evidence[:MAX_EVIDENCE]])
            return self._progress(cause.fingerprint, {**progress, "done": True})
        if action in (Action.FILE_AND_RUN, Action.FILE_HELD):
            number = self.ensure_issue(cause, component, held=action == Action.FILE_HELD)
        else:
            number = int(record["issue"])
            self.comment(cause, number, len(progress["new"]), action, record)
        if action in (Action.FILE_HELD, Action.HOLD):
            self.causes.set_state(cause.fingerprint, failurecauses.HELD)
        if action == Action.HOLD:
            # After the hold is recorded, so a label GitHub refuses cannot undo it.
            self.hold_label(number)
        if action in (Action.FILE_AND_RUN, Action.COMMENT_AND_RUN):
            started = self.start(cause, number)
            progress["run_id"] = started
        return self._progress(cause.fingerprint, {**progress, "issue": number, "done": True})

    def _progress(self, fp: str, progress: dict[str, Any]) -> dict[str, Any]:
        all_progress = dict(_mapping(self.review.get("progress")))
        all_progress[fp] = progress
        _save(self.ctx.db, self.review, self.ctx.now(), progress=all_progress)
        return progress


def _components(raw: Any) -> dict[str, str]:
    """fingerprint_of.component per computed fingerprint, for the form's "Where"."""
    out: dict[str, str] = {}
    if not isinstance(raw, Mapping):
        return out
    for item in raw.get("causes") or []:
        of = _mapping(_mapping(item).get("fingerprint_of"))
        fp = failurecauses.fingerprint(str(of.get("component")), str(of.get("site")),
                                       str(of.get("class")), str(of.get("signature")))
        out[fp] = str(of.get("component") or "")
        known = _mapping(item).get("known_fingerprint")
        if isinstance(known, str):
            out.setdefault(known, str(of.get("component") or ""))
    return out


def _read_rca(ctx: Any, task: Mapping[str, Any]) -> str | None:
    try:
        row = ctx.inspection.read_artifact(str(task["tenant_id"]), str(task["id"]),
                                           submitted_by=None, name=RCA_FILE)
    except ApiError as exc:
        if exc.status_code >= 500:
            raise
        return None
    if row.get("status") != "ok" or row.get("truncated"):
        return None
    return str(row.get("content") or "")


def _fail(firing: Any, review: dict[str, Any], why: str) -> dict[str, Any]:
    """§9: nothing is half filed. The review ends `failed`; its window is read again."""
    return _save(firing.ctx.db, review, firing.now, state=FAILED,
                 last_error=failuredigest.redacted(why, failuredigest.ERROR_BYTES),
                 summary={**_mapping(review.get("summary")), "failed": True})


def take_in_one(firing: Any, registration: Mapping[str, Any], review: dict[str, Any]) -> dict[str, Any] | None:
    """Act on one earlier firing's `rca.json`, or end it; None while its investigator runs."""
    ctx = firing.ctx
    if review.get("state") == INVESTIGATING:
        task = investigator(ctx.db, review)
        if task is None:
            born = _aware(review.get("created_at"))
            if born is not None and firing.now - born > LOST_AFTER:
                return _fail(firing, review, "the investigator was never found")
            return None
        state = str(task.get("state") or "")
        if state not in ("SUCCEEDED", "FAILED", "DEAD_LETTERED", "CANCELLED"):
            return None
        if state != "SUCCEEDED":
            return _fail(firing, review, f"the investigator ended {state}: {task.get('last_error') or ''}")
        raw = _read_rca(ctx, task)
        if raw is None:
            return _fail(firing, review, f"the investigator wrote no readable {RCA_FILE}")
        try:
            causes, unexplained = failurecauses.validate_rca(
                raw, firing_id=str(review["firing_id"]), refs=list(_mapping(review.get("refs"))),
                known=list(review.get("known") or []))
        except ValidationFailed as exc:
            return _fail(firing, review, exc.message)
        components = _components(_parse(raw))
        review = _save(ctx.db, review, firing.now, state=FILING, task_id=task.get("id"),
                       causes=[{"fingerprint": c.fingerprint, "component": components.get(c.fingerprint, ""),
                                **_cause_doc(c)} for c in causes],
                       unexplained=[u.model_dump() for u in unexplained])
    filing = _Filing(firing, registration, review)
    refs = _mapping(review.get("refs"))
    for doc in review.get("causes") or []:
        cause = _cause_from(doc)
        ids = [str(_mapping(refs.get(r)).get("id")) for r in cause.failures if r in refs]
        filing.act(cause, str(doc.get("component") or ""), ids)
    summary = {**tally(review), "start_failures": filing.start_failures}
    review = _save(ctx.db, review, firing.now, state=FILED, summary=summary)
    _advance_cursor(ctx.db, review, firing.now)
    post_summary(firing, registration, review)
    return review


def tally(review: Mapping[str, Any]) -> dict[str, Any]:
    """§8's counts, from what each cause's progress records: a resumed filing counts it all."""
    counts = {"causes": 0, "new": 0, "known": 0, "seen_again": 0, "issues_filed": 0,
              "issues_updated": 0, "runs_started": 0, "held": 0, "findings": 0,
              "unexplained": len(review.get("unexplained") or [])}
    no_change: list[str] = []
    refused: list[dict[str, Any]] = []
    for fp, progress in _mapping(review.get("progress")).items():
        progress = _mapping(progress)
        counts["causes"] += 1
        counts["new" if progress.get("created") else "known"] += 1
        action = progress.get("action")
        if action is None:
            counts["seen_again"] += 1
        elif action in (Action.FILE_AND_RUN.value, Action.FILE_HELD.value):
            counts["issues_filed"] += 1
        elif action in (Action.COMMENT.value, Action.COMMENT_AND_RUN.value, Action.HOLD.value):
            counts["issues_updated"] += 1
        if action in (Action.FILE_HELD.value, Action.HOLD.value):
            counts["held"] += 1
        if progress.get("run_id"):
            counts["runs_started"] += 1
        counts["findings"] += int(progress.get("findings") or 0)
        if action == Action.NO_CHANGE.value:
            no_change.append(str(fp))
        if progress.get("existing_refused"):
            refused.append({"fingerprint": str(fp), **_mapping(progress["existing_refused"])})
    return {**counts, "no_change": sorted(no_change), "existing_refused": refused}


def _parse(raw: str) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _cause_doc(cause: Cause) -> dict[str, Any]:
    return {
        "signature": cause.signature, "failures": list(cause.failures), "cause": cause.cause,
        "evidence": [e.model_dump() for e in cause.evidence], "fix": cause.fix,
        "files": list(cause.files), "hold": cause.hold, "where": cause.where, "known": cause.known,
        "action": cause.action, "existing_issue": cause.existing_issue,
    }


def _cause_from(doc: Mapping[str, Any]) -> Cause:
    return Cause(
        fingerprint=str(doc["fingerprint"]), signature=str(doc.get("signature") or ""),
        failures=tuple(doc.get("failures") or ()), cause=str(doc.get("cause") or ""),
        evidence=tuple(failurecauses.Evidence.model_validate(e) for e in doc.get("evidence") or ()),
        fix=str(doc.get("fix") or ""), files=tuple(doc.get("files") or ()), hold=str(doc.get("hold") or "none"),
        where=str(doc.get("where") or "platform"), known=bool(doc.get("known")),
        action=str(doc.get("action") or "fix"),
        existing_issue=int(doc["existing_issue"]) if doc.get("existing_issue") else None,
    )


# --------------------------------------------------------------------------
# The summary (§8)
# --------------------------------------------------------------------------


def summary_text(review: Mapping[str, Any]) -> str:
    s = _mapping(review.get("summary"))
    skipped = _mapping(review.get("skipped"))
    rules = ", ".join(f"{rule} {int(skipped.get(rule) or 0)}" for rule in failuredigest.SKIP_RULES)
    no_change = [str(fp) for fp in s.get("no_change") or []]
    refused = [_mapping(r) for r in s.get("existing_refused") or []]
    read = len(_mapping(review.get("refs")))
    head = (f"0 failures in the window {_window(review)}." if read == 0 else
            f"{read} failure(s) reviewed in the window {_window(review)}.")
    return "\n".join([
        summary_marker(str(review["firing_id"])),
        f"**failure-review firing `{review['firing_id']}`.** {head}",
        "",
        f"- skipped by rule: {rules}",
        f"- causes: {int(s.get('causes') or 0)} ({int(s.get('new') or 0)} new, {int(s.get('known') or 0)} known, "
        f"{int(s.get('seen_again') or 0)} with nothing new)",
        f"- issues filed: {int(s.get('issues_filed') or 0)}; updated: {int(s.get('issues_updated') or 0)}",
        f"- fix runs started: {int(s.get('runs_started') or 0)}; held `{HOLD_LABEL}`: {int(s.get('held') or 0)}",
        f"- no change needed: {len(no_change)}" + (f" ({', '.join(f'`{fp}`' for fp in no_change)})"
                                                   if no_change else ""),
        *[f"- existing issue refused, filed new: `{r.get('fingerprint')}` named #{r.get('issue')}: "
          f"{r.get('reason')}" for r in refused],
        f"- findings reported to tenants: {int(s.get('findings') or 0)}",
        f"- unexplained: {int(s.get('unexplained') or 0)}",
        "",
    ])


def post_summary(firing: Any, registration: Mapping[str, Any], review: dict[str, Any]) -> None:
    """One comment on `summary_issue`, in the platform's own repository only (§8)."""
    params = issue_sweep.params_of(firing)
    if not params.summary_issue:
        return
    repository, _, number = params.summary_issue.partition("#")
    if repository.lower() != _slug(registration).lower():
        _save(firing.ctx.db, review, firing.now,
              summary={**_mapping(review.get("summary")), "summary_issue": "not_the_platform_repository"})
        return
    token = _token_reader(firing.ctx, str(review["tenant_id"]))()
    ref = _ref(registration, int(number))
    writer: GitHubWriter = firing.ctx.forge_writer
    try:
        if writer.find_comment(ref, summary_marker(str(review["firing_id"])), token) is None:
            writer.create_comment(ref, bounded_body(summary_text(review)), token)
    except ForgeWriteError as exc:
        log.warning("failure-review %s: summary not posted (%s)", review["firing_id"], exc.code)


# --------------------------------------------------------------------------
# The executor seam (`schedulefire.Executor`)
# --------------------------------------------------------------------------


def take_in(firing: Any, registration: Mapping[str, Any]) -> list[dict[str, Any]]:
    done: list[dict[str, Any]] = []
    for review in open_reviews(firing.ctx.db, firing.schedule):
        if review.get("firing_id") == firing.firing["firing_id"]:
            continue
        try:
            ended = take_in_one(firing, registration, review)
        except ForgeWriteError as exc:
            # The code only, never GitHub's answer. The review stays `filing`
            # and the next firing carries on where this one stopped.
            log.warning("failure-review %s: filing stopped (%s): %s", review.get("firing_id"),
                        exc.code, exc.message)
            continue
        if ended is not None:
            done.append({"firing_id": ended["firing_id"], "state": ended["state"],
                         "summary": dict(_mapping(ended.get("summary")))})
    return done


def _review_doc(firing: Any, digest: failuredigest.Digest, docs: failuredigest.Documents,
                known: Sequence[Mapping[str, Any]], text: str, dropped: Sequence[str]) -> dict[str, Any]:
    window = digest.doc["window"]
    return {
        "firing_id": firing.firing["firing_id"], "schedule_id": firing.schedule["schedule_id"],
        "tenant_id": firing.schedule["tenant_id"], "state": INVESTIGATING,
        "window": dict(window), "since": datetime.fromisoformat(window["from"]),
        "until": datetime.fromisoformat(window["to"]),
        "refs": digest.refs, "tenants": digest.tenants,
        "skipped": dict(digest.doc["skipped"]), "skipped_ids": digest.skipped,
        "known": [str(k.get("fingerprint")) for k in known if k.get("fingerprint")],
        "evidence": _evidence_of(docs, digest.refs), "truncated": list(docs.truncated),
        "digest_bytes": len(text.encode("utf-8")), "dropped": list(dropped),
        "progress": {}, "summary": {}, "created_at": firing.now, "updated_at": firing.now,
    }


def create(firing: Any) -> list[dict[str, Any]]:
    registration = _require_platform(firing)
    if registration is None:
        firing.explain({"reason": FailureReviewNoPlatformRepository.code})
        return []
    ctx = firing.ctx
    taken = take_in(firing, registration)
    digest, docs, known = digest_for(firing, registration,
                                     content=content_for(ctx, str(firing.schedule["tenant_id"])))
    text, dropped = failuredigest.bounded(digest.doc)
    review = _review_doc(firing, digest, docs, known, text, dropped)
    explained = {"window": dict(digest.doc["window"]), "reviewed": len(digest.refs),
                 "skipped": dict(digest.doc["skipped"]), "taken_in": taken}
    if not digest.refs:
        # §8: silence is told apart from a firing that did not run.
        review = _save(ctx.db, review, firing.now, state=FILED, summary=tally(review))
        _advance_cursor(ctx.db, review, firing.now)
        post_summary(firing, registration, review)
        firing.explain({**explained, "reason": "no_failures_in_window"})
        return []
    if firing.room < 1:
        firing.explain({**explained, "reason": "no_room"})
        return []
    # The record first, then the investigator: an investigator without its
    # record would be an rca.json nobody can validate.
    _save(ctx.db, review, firing.now)
    fid = str(firing.firing["firing_id"])
    work = firing.submit_tasks([investigator_task(registration, text, fid)])
    firing.explain({**explained, "investigator": work[0]["id"], "digest_bytes": len(text.encode("utf-8"))})
    log.info("failure-review %s: %d failure(s) in %s, investigator %s, %d review(s) taken in",
             fid, len(digest.refs), _window(review), work[0]["id"], len(taken))
    return work


def dry_run(firing: Any) -> dict[str, Any]:
    """§2.8: the window, what would be reviewed and skipped, and what would be taken in.

    Reads only: no content (no log or forge read), nothing written, no agent.
    """
    registration = _require_platform(firing)
    if registration is None:
        return {"profile": PROFILE, "repository": None, "reason": FailureReviewNoPlatformRepository.code,
                "would_investigate": False, "to_take_in": []}
    digest, _docs, _known = digest_for(firing, registration)
    text, dropped = failuredigest.bounded(digest.doc)
    pending = []
    for review in open_reviews(firing.ctx.db, firing.schedule):
        task = investigator(firing.ctx.db, review) if review.get("state") == INVESTIGATING else None
        pending.append({"firing_id": review.get("firing_id"), "state": review.get("state"),
                        "investigator": (task or {}).get("id"), "investigator_state": (task or {}).get("state")})
    return {
        "profile": PROFILE,
        "repository": _slug(registration),
        "window": dict(digest.doc["window"]),
        "reviewed": len(digest.refs),
        "skipped": dict(digest.doc["skipped"]),
        "groups": len(digest.doc.get("groups") or []),
        "digest_bytes": len(text.encode("utf-8")),
        "dropped": len(dropped),
        "would_investigate": bool(digest.refs),
        "to_take_in": pending,
    }
