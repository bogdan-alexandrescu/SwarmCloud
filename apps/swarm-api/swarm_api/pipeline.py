"""`GET /v1/pipeline`: the Pipeline board's one read (docs/design/plans-pipeline.md §2.1, §3.2).

Every card the board draws, each already in its ONE stage, so the browser
places nothing itself:

  found     a pending `proposal` approval; an issue run in PLANNING
  planned   an issue run in PLANNED (`plan`, or `hold` when held)
  running   APPROVED, RUNNING
  pr_ci     CHECKING, FIXING; a pending `merge` approval, folded into its
            run's card -- a card is placed once
  ended     DONE, FAILED, CANCELLED, REJECTED, NOT_READY; a `proposal`
            approved (filed), rejected, expired or superseded

`waits_on_person` is true exactly for a pending `plan`, `hold`, `merge` or
`proposal` item, the inbox's own pending projection (`approvals.project_run`,
`approvals.pending`, `approvals._merge_run_waits`) -- never `approvals.inbox`
itself, which materialises firing records and closes stale merges: this read
WRITES NOTHING and holds nothing (invariant 1).

BOUNDED, AND SAYS SO. Live runs and the newest runs are each one read of at
most `approvals.HISTORY_LIMIT`; the pending records one of `INBOX_SCAN`, and
the closed proposals one more of `HISTORY_LIMIT`, as `approvals.history`
reads. Ended cards are the last PIPELINE_WINDOW_DAYS days. A read that came
back full is named in `truncated`; `by_stage.*.hidden` counts the cards a
filter left out, so a column's count is the API's, never 0 for "not read".

ONE FAILED READ IS NOT A FAILED BOARD (`C:P6c`). Each read is its own: one
that raises is named in `partial` with status 503 and the cards it would have
fed are absent; the others still answer.

TENANT-SCOPED (invariant 9): every query is filtered on the caller's
tenant, and every row is filtered again here.

Failure-review causes filed with no run yet (§2.1's third `found` source)
are not served: `failure_causes/{fingerprint}` is a platform record with no
tenant on it, so no tenant's board may read it. A cause's card appears once
its fix run exists, as a `failure_review` run.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping

from google.cloud.firestore_v1.base_query import FieldFilter

from . import approvals, schedules
from .issueruns import IssueRun, IssueRuns, RunState

log = logging.getLogger(__name__)

#: How far back an ended card reaches (§3.2).
PIPELINE_WINDOW_DAYS = 14

FOUND, PLANNED, RUNNING, PR_CI, ENDED = "found", "planned", "running", "pr_ci", "ended"
STAGES = (FOUND, PLANNED, RUNNING, PR_CI, ENDED)

RUN_STAGE: dict[RunState, str] = {
    RunState.PLANNING: FOUND,
    RunState.PLANNED: PLANNED,
    RunState.APPROVED: RUNNING,
    RunState.RUNNING: RUNNING,
    RunState.CHECKING: PR_CI,
    RunState.FIXING: PR_CI,
    RunState.DONE: ENDED,
    RunState.FAILED: ENDED,
    RunState.CANCELLED: ENDED,
    RunState.REJECTED: ENDED,
    RunState.NOT_READY: ENDED,
}

#: `?state=`: which columns a state filter leaves. `decided` is the ended
#: cards a person (or expiry) decided: a proposal closed by a decision, a run
#: whose plan was rejected.
STATE_STAGES: dict[str, tuple[str, ...]] = {
    "waiting": (FOUND, PLANNED),
    "running": (RUNNING, PR_CI),
    "decided": (ENDED,),
    "ended": (ENDED,),
}
STATE_FILTERS = tuple(STATE_STAGES)
#: `?waits=`: only the cards that wait on a person.
WAITS_PERSON = "person"

#: The reads `partial` and `truncated` name.
READ_RUNS, READ_APPROVALS, READ_SCHEDULES = "runs", "approvals", "schedules"
#: What a failed read answers in `partial`: the read was not served.
FAILED_READ_STATUS = 503

_DECIDED_PROPOSAL_STATES = (approvals.APPROVED, approvals.REJECTED, approvals.EXPIRED)
_CLOSED_PROPOSAL_STATES = _DECIDED_PROPOSAL_STATES + (approvals.SUPERSEDED,)


def _iso(moment: Any) -> str | None:
    return approvals._iso(moment)


def _aware(moment: Any) -> datetime | None:
    return approvals._aware(moment)


def _run_since(run: IssueRun, stage: str) -> datetime | None:
    """When the run entered `stage`: the newest history entry into a state of
    that stage from a state of another, else its creation."""
    for entry in reversed(run.history):
        try:
            to, frm = RunState(entry.get("to")), entry.get("from")
        except ValueError:
            continue
        before = RUN_STAGE.get(RunState(frm)) if frm in RunState._value2member_map_ else None
        if RUN_STAGE[to] == stage and before != stage:
            return _aware(entry.get("at"))
    return _aware(run.created_at)


def _title(run: IssueRun) -> str:
    read = run.issue_read or {}
    return str(read.get("title") or (run.plan or {}).get("summary") or run.issue.short)[:approvals.SUMMARY_CHARS]


def run_card(run: IssueRun, schedule: Mapping[str, Any] | None,
             merge: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """One run's card. A PLANNED run carries its projected plan or hold item;
    a run with a pending merge approval carries that approval."""
    stage = RUN_STAGE[run.state]
    served = run.to_api()
    subject: dict[str, Any] = {"run_id": run.id, "issue": run.issue.short, "title": _title(run)}
    kind: str | None = None
    expires: Any = None
    if run.state == RunState.PLANNED:
        item = approvals.project_run(run, schedule)
        kind, expires = item["kind"], item.get("expires_at")
        subject["approval_id"] = item["approval_id"]
    elif merge is not None:
        kind, expires = approvals.MERGE, merge.get("expires_at")
        subject["approval_id"] = merge.get("approval_id")
    pull = run.pull_request
    return {
        "card_id": approvals.PROJECTED_PREFIX + run.id,
        "stage": stage,
        "source": served["source"],
        "subject": subject,
        "state": run.state.value.lower(),
        "waits_on_person": kind is not None,
        "approval_kind": kind,
        "expires_at": _iso(expires),
        "since": _iso(_run_since(run, stage)),
        # §3.4's thread is lane P3's; until a run has one, no last word.
        "last_word": None,
        "revision": run.plan_revision if run.plan is not None else None,
        "plan_digest": run.plan_digest,
        "pull_request": None if pull is None else {
            "number": pull.get("number"), "url": pull.get("url"), "checks": pull.get("checks"),
        },
        # Used for `?state=decided`, dropped before serving.
        "_decided": run.state == RunState.REJECTED,
    }


def proposal_card(doc: Mapping[str, Any], schedule: Mapping[str, Any] | None) -> dict[str, Any]:
    """An observer or flake proposal's card. Its source is its schedule's type
    (§3.1), read from the approval's `subject.schedule_id`."""
    subject = doc.get("subject") or {}
    stype = (schedule or {}).get("type")
    pending = doc.get("state") == approvals.PENDING
    out_subject: dict[str, Any] = {"title": str(doc.get("summary") or subject.get("title") or ""),
                                   "approval_id": doc.get("approval_id")}
    if subject.get("issue"):
        out_subject["issue"] = subject.get("issue")
    return {
        "card_id": doc.get("approval_id"),
        "stage": FOUND if pending else ENDED,
        "source": {
            "kind": str(stype) if stype else approvals.PROPOSAL,
            "schedule_type": str(stype) if stype else None,
            "schedule_id": subject.get("schedule_id"),
            "firing_id": subject.get("firing_id"),
            "fingerprint": None,
            "proposal_id": doc.get("approval_id"),
        },
        "subject": out_subject,
        "state": str(doc.get("state")),
        "waits_on_person": pending,
        "approval_kind": approvals.PROPOSAL,
        "expires_at": _iso(doc.get("expires_at")) if pending else None,
        "since": _iso(doc.get("requested_at") if pending else doc.get("decided_at")),
        "last_word": None,
        "revision": None,
        "plan_digest": None,
        "pull_request": None,
        "_decided": doc.get("state") in _DECIDED_PROPOSAL_STATES,
    }


def _runs(ctx: Any, tenant_id: str, cutoff: datetime) -> tuple[list[IssueRun], bool]:
    """The tenant's live runs and its runs ended inside the window, and
    whether either read came back full."""
    store = IssueRuns(ctx.db, now=ctx.now)
    live, more_live = store.live(tenant_id, limit=approvals.HISTORY_LIMIT)
    newest, next_token = store.list(tenant_id, limit=approvals.HISTORY_LIMIT)
    found: dict[str, IssueRun] = {}
    for run in [*live, *newest]:
        if run.tenant_id != tenant_id or run.id in found:
            continue
        if RUN_STAGE[run.state] == ENDED and (_aware(run.updated_at) or _aware(run.created_at)) < cutoff:
            continue
        found[run.id] = run
    return list(found.values()), bool(more_live) or next_token is not None


def _approval_records(ctx: Any, tenant_id: str, cutoff: datetime) -> tuple[list[dict[str, Any]], bool]:
    """The tenant's pending records, and its proposals closed inside the
    window; and whether a read came back full. The closed read is
    `approvals.decided_records`' query on the index that exists
    (approvals-tenant-state-requested), over this window."""
    pending = approvals.pending(ctx.db, tenant_id)
    since = cutoff - timedelta(hours=schedules.APPROVAL_TTL_MAX_HOURS)
    query = (
        ctx.db.collection(approvals.COLLECTION)
        .where(filter=FieldFilter("tenant_id", "==", tenant_id))
        .where(filter=FieldFilter("state", "in", list(_CLOSED_PROPOSAL_STATES)))
        .where(filter=FieldFilter("requested_at", ">=", since))
        .order_by("requested_at")
        .limit(approvals.HISTORY_LIMIT)
    )
    read = [snap.to_dict() or {} for snap in query.stream()]
    closed = [r for r in read if r.get("tenant_id") == tenant_id and r.get("kind") == approvals.PROPOSAL
              and r.get("state") in _CLOSED_PROPOSAL_STATES
              and (_aware(r.get("decided_at")) or _aware(r.get("requested_at")) or cutoff) >= cutoff]
    rows = [r for r in pending if r.get("tenant_id") == tenant_id] + closed
    return rows, len(pending) >= approvals.INBOX_SCAN or len(read) >= approvals.HISTORY_LIMIT


def _merge_run(ctx: Any, tenant_id: str, doc: Mapping[str, Any]) -> IssueRun | None:
    """A pending merge's run, when it still waits on that merge; None otherwise."""
    data = approvals._merge_run(ctx.db, doc)
    if not approvals._merge_run_waits(doc, data) or (data or {}).get("tenant_id") != tenant_id:
        return None
    return IssueRun.from_firestore(data or {})


def _read(partial: list[dict[str, Any]], name: str, fn: Callable[[], Any], empty: Any) -> Any:
    try:
        return fn()
    except Exception:  # noqa: BLE001 -- one failed read is named in `partial`, never the whole board
        log.exception("pipeline read %s failed", name)
        partial.append({"read": name, "status": FAILED_READ_STATUS})
        return empty


def pipeline(ctx: Any, tenant_id: str, *, source: str | None = None, state: str | None = None,
             waits: str | None = None) -> dict[str, Any]:
    """§3.2's response for `tenant_id`, filtered by source kind, state and waits."""
    now = ctx.now()
    cutoff = now - timedelta(days=PIPELINE_WINDOW_DAYS)
    partial: list[dict[str, Any]] = []
    truncated: list[str] = []
    runs, runs_full = _read(partial, READ_RUNS, lambda: _runs(ctx, tenant_id, cutoff), ([], False))
    records, records_full = _read(partial, READ_APPROVALS, lambda: _approval_records(ctx, tenant_id, cutoff),
                                  ([], False))
    if runs_full:
        truncated.append(READ_RUNS)
    if records_full:
        truncated.append(READ_APPROVALS)

    seen: dict[str, Mapping[str, Any] | None] = {}
    schedules_failed = False

    def schedule_of(schedule_id: Any) -> Mapping[str, Any] | None:
        nonlocal schedules_failed
        if not schedule_id:
            return None
        key = str(schedule_id)
        if key not in seen:
            try:
                seen[key] = approvals.read_schedule(ctx.db, tenant_id, key)
            except Exception:  # noqa: BLE001 -- named in `partial`; the card still draws
                log.exception("pipeline schedule read failed")
                schedules_failed = True
                seen[key] = None
        return seen[key]

    by_run: dict[str, IssueRun] = {run.id: run for run in runs}
    merges: dict[str, Mapping[str, Any]] = {}
    proposals: list[Mapping[str, Any]] = []
    for doc in records:
        if doc.get("kind") == approvals.MERGE and doc.get("state") == approvals.PENDING:
            run = _read(partial, READ_RUNS, lambda doc=doc: _merge_run(ctx, tenant_id, doc), None)
            if run is not None:
                by_run.setdefault(run.id, run)
                merges[run.id] = doc
        elif doc.get("kind") == approvals.PROPOSAL:
            proposals.append(doc)

    cards: list[dict[str, Any]] = []
    for run in by_run.values():
        sid = (run.schedule or {}).get("schedule_id")
        cards.append(run_card(run, schedule_of(sid) if run.state == RunState.PLANNED else None,
                              merges.get(run.id) if RUN_STAGE[run.state] == PR_CI else None))
    for doc in proposals:
        cards.append(proposal_card(doc, schedule_of((doc.get("subject") or {}).get("schedule_id"))))
    if schedules_failed:
        partial.append({"read": READ_SCHEDULES, "status": FAILED_READ_STATUS})
    # A read that failed twice (a merge's run) is named once.
    partial = [dict(p) for p in {(p["read"], p["status"]): p for p in partial}.values()]

    def shown(card: Mapping[str, Any]) -> bool:
        if source is not None and card["source"].get("kind") != source:
            return False
        if state is not None:
            if card["stage"] not in STATE_STAGES[state]:
                return False
            if state == "decided" and not card["_decided"]:
                return False
        return not (waits == WAITS_PERSON and not card["waits_on_person"])

    by_stage: dict[str, dict[str, Any]] = {s: {"count": 0, "hidden": 0, "oldest_at": None} for s in STAGES}
    served: list[dict[str, Any]] = []
    for card in cards:
        card = {k: v for k, v in card.items() if k != "_decided"} if shown(card) else {**card, "_hidden": True}
        column = by_stage[card["stage"]]
        if card.pop("_hidden", False):
            column["hidden"] += 1
            continue
        column["count"] += 1
        if card["since"] and (column["oldest_at"] is None or card["since"] < column["oldest_at"]):
            column["oldest_at"] = card["since"]
        served.append(card)
    # A card that waits on a person sorts first, oldest first; then the rest, newest first.
    waiting = sorted((c for c in served if c["waits_on_person"]), key=lambda c: (c["since"] or "", c["card_id"]))
    rest = sorted((c for c in served if not c["waits_on_person"]), key=lambda c: (c["since"] or "", c["card_id"]),
                  reverse=True)
    return {
        "cards": waiting + rest,
        "by_stage": by_stage,
        # Across the whole board, whatever the filters: the lead strip's and
        # the Approvals page's one count (§2.2).
        "waits_on_person": sum(1 for c in cards if c["waits_on_person"]),
        "partial": partial,
        "truncated": truncated,
        "window_days": PIPELINE_WINDOW_DAYS,
    }


