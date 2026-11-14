"""`graph-latency-report`: one issue per repository whose cold graph answer p95
nears the target (#1840; docs/design/graph-answer-cache.md §1.3).

AN API TYPE (SD8, §2.7), built like `release-health`. Nothing here runs an
agent, submits a task or holds capacity: each repository in scope is one
`api_action` on the firing, ended the moment it is recorded.

WHAT A FIRING DOES, per repository in scope, as the schedule's tenant, with
that tenant's forge credential, through `ctx.forge_writer`:

  1. READ the newest `window` promoted versions and each one's measurement
     (`graphreports.measurements`: promotion's `answer_latency`, else the
     indexer's `latency.json` beside the manifest).
  2. DECIDE (`graphreports.decide`): the newest measured p95 over its own
     run's `reference_ms` at or over `ratio_threshold` OPENS one issue, found
     again by the marker its body opens with, or COMMENTS on the open one
     unless it already names that version; back under after an over reading
     comments "under again" once; no measurement for 3 days after one is
     reported once. It NEVER closes the issue: a person does.
  3. COUNT (#4030): the repository's seven days of graph delivery by how the
     graph reached each attempt and the fallbacks by reason
     (`graphreports.delivery_report`), and the replicas' merged served
     p50/p95 against 300 ms. These go on the firing record, the type's
     history, and write nothing to GitHub.

A repository's GitHub failure is that repository's (`failed`, with the code),
as in `release-health`. DRY RUN (§2.8): the same reads, what each repository
WOULD do, no write.

INVARIANTS. No task, lease or capacity (1-3). Every registration, version,
object and attempt is read under the schedule's tenant, with that tenant's own
token (9). The caller picks a ratio, a window and a label, never a URL, a
command or a credential (10).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping

from .. import graphreports
from ..errors import ApiError
from ..forgewrite import ISSUES_WRITE, ForgeWriteError, GitHubWriter
from ..repositories import Repositories
from ..routes.graph_cache import StatsReader, stats_url
from ..scheduletypes import BudgetCaps
from ..validation import IssueRef
from . import issue_sweep, release_health

log = logging.getLogger(__name__)

TYPE = "graph-latency-report"

#: SD4: this type runs no model and reports no cost, so the dollar caps only
#: have to be above zero; `max_concurrent` is the repositories one firing
#: reads, each one `api_action` ended as it is recorded.
BUDGET_CAPS = BudgetCaps(per_run_usd=0.01, per_day_usd=1.0, max_concurrent=10)

REPOSITORY_NOT_REGISTERED = release_health.REPOSITORY_NOT_REGISTERED


@dataclass
class Reading:
    repo_id: str
    repository: str | None = None
    ref: IssueRef | None = None
    readings: graphreports.Readings | None = None
    action: str = graphreports.NOTHING
    reason: str = ""
    issue: int | None = None
    delivery: dict[str, Any] | None = None
    served: dict[str, Any] | None = None
    written: dict[str, Any] = field(default_factory=dict)
    code: str | None = None
    message: str | None = None

    @property
    def failed(self) -> bool:
        return self.code is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "repo_id": self.repo_id, "repository": self.repository, "action": self.action,
            "reason": self.reason, "issue": self.issue,
            "readings": self.readings.as_dict() if self.readings is not None else None,
            "delivery": self.delivery, "served_latency": self.served,
            "written": self.written, "code": self.code, "message": self.message,
        }


def _issue(ref: IssueRef, number: int) -> IssueRef:
    return IssueRef(owner=ref.owner, repo=ref.repo, number=int(number))


def read(ctx: Any, writer: GitHubWriter, reading: Reading, params: Any, token: str,
         tenant_id: str) -> Reading:
    """Steps 1-2: the window's measurements, the open issue, and the decision."""
    reader = getattr(getattr(ctx, "inspection", None), "_reader", None)
    readings = graphreports.measurements(ctx.db, reader, tenant_id, reading.repo_id, params.window)
    reading.readings = readings
    assert reading.ref is not None
    found = release_health.streak_issue(writer, reading.ref, params.issue_label,
                                        graphreports.issue_marker(reading.repo_id), token)
    if found is not None:
        reading.issue = found[0]
    body = found[1] if found is not None else ""

    def said(marker: str) -> bool:
        assert reading.ref is not None and reading.issue is not None
        if marker in body:
            return True
        return writer.find_comment(_issue(reading.ref, reading.issue), marker, token) is not None

    reading.action, reading.reason = graphreports.decide(
        readings, params.ratio_threshold, ctx.now(), issue_open=found is not None, said=said)
    return reading


def write(writer: GitHubWriter, reading: Reading, params: Any, schedule_id: str, token: str) -> None:
    """Do `reading.action` on GitHub. Each write is found again by its marker."""
    ref, readings = reading.ref, reading.readings
    if reading.action == graphreports.NOTHING or ref is None or readings is None \
            or readings.newest is None:
        return
    if reading.action in (graphreports.OPEN_ISSUE, graphreports.OPEN_UNMEASURED):
        what = f"a new issue on {ref.repository}"
        newest = readings.newest
        title = graphreports.title(ref.repository, newest) \
            if reading.action == graphreports.OPEN_ISSUE \
            else graphreports.unmeasured_title(ref.repository, newest)
        data = writer._call(
            "POST", writer._url(ref, "issues"), token, what, needs=ISSUES_WRITE, ok=(200, 201),
            payload={
                "title": title,
                "body": graphreports.issue_body(reading.repo_id, ref.repository, readings,
                                                params.ratio_threshold, reading.action,
                                                schedule_id),
                "labels": [params.issue_label],
            },
        )
        number = data.get("number") if isinstance(data, Mapping) else None
        if not isinstance(number, int) or isinstance(number, bool):
            raise ForgeWriteError(f"GitHub's answer for {what} names no issue number")
        reading.issue = number
        reading.written = {"issue_opened": number}
        return
    assert reading.issue is not None
    body = graphreports.comment_body(readings, params.ratio_threshold, reading.action)
    comment = writer.create_comment(_issue(ref, reading.issue), body, token)
    reading.written = {"comment_id": comment.id}


def count(ctx: Any, reading: Reading, tenant_id: str,
          read: graphreports.AttemptsRead | None = None) -> graphreports.AttemptsRead:
    """Step 3 (#4030): seven days of delivery and the served latency, recorded
    only. Returns the tenant's attempts read, for the firing's next repository."""
    since, until = graphreports.window_until(ctx.now())
    read = read if read is not None else graphreports.read_attempts(ctx.store, tenant_id, since, until)
    reading.delivery = graphreports.delivery_report(
        ctx.store, ctx.db, tenant_id, since, until, repository=reading.repository, read=read)
    reading.served = graphreports.read_served_latency(
        ctx, tenant_id, reading.repo_id, StatsReader(stats_url(ctx.settings)))
    return read


def _failed(reading: Reading, exc: Exception) -> Reading:
    reading.code = exc.code if isinstance(exc, ApiError) else type(exc).__name__
    # ApiError messages are constant, token-free sentences (errors.py).
    reading.message = exc.message if isinstance(exc, ApiError) else None
    reading.action = graphreports.NOTHING
    return reading


def _readings(firing: Any, repo_ids: list[str], params: Any, *, writes: bool) -> Iterator[Reading]:
    """Each repository's reading, one at a time, so the caller records it before the next."""
    ctx = firing.ctx
    tenant_id = str(firing.schedule["tenant_id"])
    schedule_id = str(firing.schedule["schedule_id"])
    writer: GitHubWriter = ctx.forge_writer
    registrations = Repositories(ctx.db, now=ctx.now)
    token = ""
    token_error: Exception | None = None
    attempts_read: graphreports.AttemptsRead | None = None
    try:
        for repo_id in repo_ids:
            reading = Reading(repo_id=repo_id)
            registration = registrations.find(tenant_id, repo_id)
            if registration is None:
                reading.code = REPOSITORY_NOT_REGISTERED
                reading.message = f"repository {repo_id!r} is not registered in this tenant"
                yield reading
                continue
            reading.ref = IssueRef(owner=str(registration["owner"]),
                                   repo=str(registration["repo"]), number=0)
            reading.repository = reading.ref.repository
            if not token and token_error is None:
                try:
                    token = ctx.forge_tokens.token_for(ctx.store.get_tenant(tenant_id))
                except ApiError as exc:
                    token_error = exc
            if token_error is not None:
                yield _failed(reading, token_error)
                continue
            try:
                read(ctx, writer, reading, params, token, tenant_id)
                if writes:
                    write(writer, reading, params, schedule_id, token)
            except ApiError as exc:
                # GitHub's answer for this repository: its failure, not the firing's.
                _failed(reading, exc)
            try:
                attempts_read = count(ctx, reading, tenant_id, attempts_read)
            except Exception as exc:  # noqa: BLE001 -- a count never fails the report
                log.warning("graph-latency-report repo_id=%s delivery count not read error=%s",
                            repo_id, type(exc).__name__)
                reading.delivery = {"state": "not_read", "reason": type(exc).__name__}
            yield reading
    finally:
        token = ""


def create(firing: Any) -> list[dict[str, Any]]:
    params = issue_sweep.params_of(firing)
    schedule_id = str(firing.schedule["schedule_id"])
    room = max(0, int(firing.room))
    held = firing.repo_ids[room:]
    if held:
        log.info("schedule %s firing %s: %d repositories past the room wait for the next firing",
                 schedule_id, firing.firing["firing_id"], len(held))
    work: list[dict[str, Any]] = []
    for reading in _readings(firing, firing.repo_ids[:room], params, writes=True):
        # Recorded as soon as it is done (§2.2).
        work.append(firing.api_action(
            f"{TYPE}:{reading.repo_id}",
            outcome="failed" if reading.failed else "succeeded",
            detail=reading.as_dict(), repo_id=reading.repo_id,
        ))
        log.info("schedule %s firing %s: %s %s (%s)", schedule_id, firing.firing["firing_id"],
                 reading.repo_id, reading.code or reading.action, reading.reason)
    return work


def dry_run(firing: Any) -> dict[str, Any]:
    params = issue_sweep.params_of(firing)
    room = max(0, int(firing.room))
    rows = [reading.as_dict() for reading in _readings(firing, firing.repo_ids[:room], params, writes=False)]
    return {
        "ratio_threshold": params.ratio_threshold,
        "window": params.window,
        "issue_label": params.issue_label,
        "room": room,
        "held_for_room": list(firing.repo_ids[room:]),
        "repositories": rows,
    }
