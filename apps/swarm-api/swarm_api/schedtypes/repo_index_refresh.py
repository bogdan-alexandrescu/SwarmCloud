"""`repo-index-refresh`: queue an index run per repository in scope (docs/schedules.md §3.3, lane S6).

THROUGH THE INDEX'S OWN QUEUEING. Each repository's run is
`RepoIndex.request_run` -- what "Index now" calls -- so it takes that path's
in-flight rule unchanged: the registration's `index.in_flight_task_id` is
CLAIMED in a transaction before anything is submitted, and a repository with
a run in flight records its head as pending and submits nothing (§8.3: the
change trigger and a schedule share the rule, so the two never duplicate a
run). The run record says `trigger: manual` and `requested_by` the
schedule's owner, as an "Index now" by that member would; the task itself
carries `metadata.schedule`, which is what links it to the firing.

MARKED THROUGH THE SUBMISSION, NOT AFTER IT. `RepoIndex` submits the indexer
task itself (`indexer_task`, a service submission run with the tenant's
token, owner's D4). It is handed `MarkedSubmissions`, which adds this
firing's `metadata.schedule` to each spec inside `schedule_mark_allowed`,
exactly as `Firing.submit_tasks` does. So a firing that stops half-way is
finished by adopting its marked tasks (§2.2), never by indexing again.

Per repository, in scope order, up to the firing's room (`max_concurrent`
less this schedule's live work):

    paused          the registration's indexing is paused: not queued
    current         `only_if_behind` and the head is the indexed commit
    in_flight       a run is in flight: its head is recorded as pending
    queued          an indexer task was submitted
    failed          the head could not be read, or the submission refused

A firing where every repository failed and nothing was queued ends `refused`
with `index_refresh_failed`, naming each repository's code (§2.7). A head
GitHub would not serve (`HeadUnreadable` is a 502) is that repository's
failure, not the firing's: retried every tick, a deleted branch would hold
the firing claimed for ever. Any other 5xx propagates, and the next tick
finishes the firing.

`kind` is what is asked for: `full`, or `incremental`, which
`repoindex.choose_kind` grants only when §3.4 of repo-index.md allows and
otherwise records why it ran full.

DRY RUN (§2.8): each repository's state from its registration, read from
Firestore only; no head is read, nothing is claimed or submitted.

INVARIANTS. An indexer task is an ordinary QUEUED task admitted like any
other (1-3). The profile is `indexer`, named by `repoindex.indexer_task`
(10). Every registration is read under the schedule's tenant (9).
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Sequence

from .. import refusals
from ..errors import ApiError, Conflict
from ..forge import ForgeReadError
from ..repoindex import IndexPaused, RepoIndex, read_head
from ..schemas import TaskCreate
from ..validation import SCHEDULE_METADATA_KEY, schedule_mark_allowed
from . import issue_sweep

log = logging.getLogger(__name__)

TYPE = "repo-index-refresh"


class IndexRefreshFailed(Conflict):
    code = "index_refresh_failed"


class MarkedSubmissions:
    """`SubmissionService`, with every task spec marked as this firing's work."""

    def __init__(self, submissions: Any, mark: Mapping[str, Any]) -> None:
        self._submissions = submissions
        self._mark = dict(mark)

    def submit_tasks(self, auth: Any, specs: Sequence[TaskCreate], **kwargs: Any) -> Any:
        marked = [
            spec.model_copy(update={"metadata": {**spec.metadata, SCHEDULE_METADATA_KEY: self._mark}})
            for spec in specs
        ]
        with schedule_mark_allowed(self._mark):
            return self._submissions.submit_tasks(auth, marked, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._submissions, name)


def _index(firing: Any) -> RepoIndex:
    ctx = firing.ctx
    return RepoIndex(
        ctx.db, store=ctx.store, submissions=MarkedSubmissions(ctx.submissions, firing.mark),
        inspection=ctx.inspection, tokens=ctx.forge_tokens, forge=ctx.forge, now=ctx.now,
    )


def create(firing: Any) -> list[dict[str, Any]]:
    params = issue_sweep.params_of(firing)
    tenant_id = str(firing.schedule["tenant_id"])
    index = _index(firing)
    tenant = firing.ctx.store.get_tenant(tenant_id)
    work: list[dict[str, Any]] = []
    outcomes: dict[str, str] = {}
    for repo_id in firing.repo_ids:
        if len(work) >= firing.room:
            outcomes[repo_id] = "room"
            continue
        try:
            record = index.registrations.get(tenant_id, repo_id)
            if (record.get("index") or {}).get("paused"):
                outcomes[repo_id] = "paused"
                continue
            if params.only_if_behind:
                head = read_head(record, tenant, tokens=firing.ctx.forge_tokens, forge=firing.ctx.forge)
                if head == (record.get("index") or {}).get("current_sha"):
                    outcomes[repo_id] = "current"
                    continue
            answer = index.request_run(firing.owner, tenant_id, repo_id, tenant=tenant, kind=params.kind)
        except IndexPaused:
            outcomes[repo_id] = "paused"
            continue
        except ForgeReadError as exc:
            outcomes[repo_id] = f"failed: {exc.code}"
            continue
        except ApiError as exc:
            if exc.status_code >= 500:
                raise
            outcomes[repo_id] = f"failed: {exc.code}"
            continue
        run = answer.get("run") or {}
        if answer.get("coalesced") or not run.get("task_id"):
            outcomes[repo_id] = "in_flight"
            continue
        outcomes[repo_id] = "queued"
        work.append({"kind": "task", "id": run["task_id"], "repo_id": repo_id})
    if not work and outcomes and all(o.startswith("failed") for o in outcomes.values()):
        # A new refusal ships report-only (refusals.py): off, the firing is
        # let through with nothing queued and the log names the refusal.
        refusals.refuse(IndexRefreshFailed(
            "no repository in scope could be queued for indexing",
            detail={"repositories": outcomes},
        ))
    log.info("schedule %s firing %s: index runs %s", firing.schedule["schedule_id"],
             firing.firing["firing_id"], outcomes)
    return work


def dry_run(firing: Any) -> dict[str, Any]:
    params = issue_sweep.params_of(firing)
    tenant_id = str(firing.schedule["tenant_id"])
    index = _index(firing)
    rows: list[dict[str, Any]] = []
    room = firing.room
    for repo_id in firing.repo_ids:
        record = index.registrations.find(tenant_id, repo_id) or {}
        state = record.get("index") or {}
        if state.get("paused"):
            would = "paused"
        elif state.get("in_flight_task_id"):
            would = "in_flight"
        elif room <= 0:
            would = "room"
        else:
            would = "queue" if not params.only_if_behind else "queue_if_behind"
            room -= 1
        rows.append({
            "repo_id": repo_id,
            "repository": f"{record.get('owner')}/{record.get('repo')}" if record else None,
            "would": would,
            "current_sha": state.get("current_sha"),
            "head_sha": state.get("head_sha"),
        })
    return {"kind": params.kind, "only_if_behind": params.only_if_behind,
            "room": firing.room, "repositories": rows}
