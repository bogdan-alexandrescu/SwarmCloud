"""`cost-report`: reported spend over a window, with its coverage (docs/schedules.md §3.11, lane S10g).

AN API ACTION, NO AGENT (SD8, §2.7). A firing reads attempt and task
documents swarm-api already holds and records what it found as work items of
`kind: api_action` on the firing. It creates no task, takes no capacity and
needs no worker; the firing record is the report's history.

WHAT A FIRING DOES.

  1. READ every attempt CREATED in the window -- `day` (24 hours) or `week`
     (168 hours) ending at the firing's `now`, `since` inclusive and `until`
     exclusive, as `GET /v1/attempts` reads them (`Store.page_attempts`) --
     and each attempt's task (`Store.tasks_by_id`), under the schedule's
     tenant only.
  2. SUM `cost_usd` by schedule (the task's `metadata.schedule.schedule_id`,
     else `unscheduled`), repository (`owner/repo` of the task's
     `repository_url`, else `no repository`), runner profile and person (the
     task's `submitted_by`). Every row and the total carry their COVERAGE:
     how many attempts there were and how many reported a cost.
  3. RECORD the report as the api_action `report` (outcome `succeeded`).
  4. With `post_to_issue`, POST it as one comment and record the api_action
     `comment` -- `succeeded`, or `failed` with the reason. Two items make
     the firing `partial` when the comment fails (§2.9), so a comment GitHub
     refused never hides behind a stored report.

THE ATTEMPTS LIST'S RULE (routes/attempts.py `list_attempts`). `cost_usd` is
None when the runner reported nothing, and NULL IS NOT ZERO: such an attempt
is counted, a row none of whose attempts reported is `cost_usd: null`, and
any row or total with an attempt that reported nothing is `lower_bound: true`
("at least $X"). The total's `coverage` is that route's own block
(`spend_coverage`), so it says WHY each unreported attempt has no cost: still
running, a profile that reports none by design, not recorded, or unknown.
$0.00 is a measurement and is counted as one.

BOUNDED. At most `MAX_ATTEMPTS` attempts per tenant are read; past it the
report says `truncated` and every figure is a lower bound. Each breakdown
keeps its `MAX_ROWS` costliest rows and folds the rest into one `other` row
that says how many it holds, so the stored firing stays far under
Firestore's document limit and nothing is dropped without a count.

THE COMMENT (§3.11, "Blast radius: none, or one comment").
  * It is written with the SCHEDULE'S OWN TENANT'S forge credential
    (`swarm-tenant-<t>-git`), through `GitHubWriter.create_comment` -- the
    issue run's write path, whose 403 names the missing permission
    (`writeback_forbidden`, `issues: write`). Lane S0 (§0.1) recorded no
    `issues: write` check for the onboarding App; whatever a tenant's
    credential lacks is reported on the `comment` item in GitHub's own
    terms, never swallowed.
  * Only to a repository the tenant has REGISTERED (`Repositories.find`
    under its tenant id). Anything else is `repository_not_registered`, and
    nothing is written: the schedule's parameters must not turn a tenant's
    credential into a writer to repositories the tenant never connected.
  * It opens with a marker carrying the firing id, and a firing first looks
    for that marker (`find_comment`), so a firing finished again after a
    crash between the post and its record does not post twice.
  * It carries the totals, the coverage and the schedule, repository and
    profile breakdowns. It does NOT carry the by-person rows: those are
    SwarmCloud sign-in emails, and an issue may be public. They stay on the
    firing, where only the tenant's members read them.

PLATFORM VARIANT (admin, `scope: {mode: "platform"}`, §3.13). The same sums
over EVERY tenant's attempts, as aggregates only: by tenant and by profile,
never a person, a repository or a schedule, so no tenant's identities or
repository names land on a firing another tenant's members can read
(invariant 9). It is never posted: §3.11 makes it visible to admins only,
and a GitHub comment is not; `post_to_issue` there records the `comment`
item `failed` with `platform_report_not_posted`.

DRY RUN (§2.8): the same report computed from Firestore, and what the
comment would be -- where, whether the repository is registered, its size.
Nothing is recorded and nothing is written to GitHub; no credential is read.

INVARIANTS. No task, no lease, no capacity (1-3 untouched); no profile is
named, because nothing runs (10); every read is the schedule's tenant's own,
except the admin's platform variant, which keeps aggregates only (9).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence

from swarm_common.models import Attempt, Task

from ..forge import ForgeReadError
from ..forgewrite import ForgeWriteError
from ..repositories import Repositories, repo_id_for
from ..routes.attempts import spend_coverage
from ..scheduletypes import BudgetCaps
from ..validation import SCHEDULE_METADATA_KEY, parse_issue_ref
from . import issue_sweep

log = logging.getLogger(__name__)

TYPE = "cost-report"

#: SD4 leaves this type's caps to its lane. A firing runs no model and no
#: worker, so its own cost is $0.00: the dollar caps are the smallest a
#: schedule's budget accepts (a cent, `schedules.resolve_budget` refuses
#: zero) with a day of five firings' room. `max_concurrent` is 2 because a
#: firing makes at most two items, the report and its comment.
BUDGET_CAPS = BudgetCaps(per_run_usd=0.01, per_day_usd=0.05, max_concurrent=2)

WINDOWS: dict[str, timedelta] = {"day": timedelta(hours=24), "week": timedelta(days=7)}

#: Attempts read per tenant per firing. Past it, the report says `truncated`.
MAX_ATTEMPTS = 5000
PAGE_SIZE = 500
#: Tenants read by the platform variant (`Store.list_tenants`' own default).
MAX_TENANTS = 200
#: Rows kept per breakdown; the rest are folded into one `other` row.
MAX_ROWS = 50
#: Rows per breakdown in the comment.
COMMENT_ROWS = 15

REPORT = "report"
COMMENT = "comment"
UNSCHEDULED = "unscheduled"
NO_REPOSITORY = "no repository"
TASK_GONE = "unknown (task gone)"
OTHER = "other"

TENANT_DIMENSIONS = ("schedule", "repository", "profile", "person")
PLATFORM_DIMENSIONS = ("tenant", "profile")
#: The breakdowns a comment carries: never `person` (see the module's docstring).
COMMENT_DIMENSIONS = ("schedule", "repository", "profile")

REPOSITORY_NOT_REGISTERED = "repository_not_registered"
PLATFORM_NOT_POSTED = "platform_report_not_posted"
NO_ROOM = "no_room"


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


def window_of(params: Any, now: datetime) -> tuple[datetime, datetime]:
    return now - WINDOWS[params.window], now


def read_tenant(store: Any, tenant_id: str, since: datetime, until: datetime,
                limit: int | None = None) -> tuple[list[Attempt], dict[str, Task], bool]:
    """The tenant's attempts created in [since, until), their tasks, and whether the read stopped at `limit`."""
    limit = MAX_ATTEMPTS if limit is None else limit
    rows: list[Attempt] = []
    token: str | None = None
    truncated = False
    while True:
        page = store.page_attempts(tenant_id, limit=min(PAGE_SIZE, limit - len(rows)),
                                   page_token=token, since=since, until=until)
        # Filtered again here: an index mistake must not move the tenant boundary.
        rows.extend(a for a in page.items if a.tenant_id == tenant_id)
        token = page.next_page_token
        if not token:
            break
        if len(rows) >= limit:
            truncated = True
            break
    tasks = store.tasks_by_id(tenant_id, (a.task_id for a in rows))
    return rows, tasks, truncated


# --------------------------------------------------------------------------
# Summing
# --------------------------------------------------------------------------


def _cost(attempt: Attempt) -> float | None:
    cost = attempt.cost_usd
    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        return float(cost)
    return None


def repository_of(task: Task | None) -> str:
    url = (task.repository_url if task is not None else None) or ""
    if not url:
        return NO_REPOSITORY if task is not None else TASK_GONE
    path = url.split("://", 1)[-1].split("/", 1)[-1].strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    return path or NO_REPOSITORY


def schedule_of(task: Task | None) -> str:
    if task is None:
        return TASK_GONE
    mark = (task.metadata or {}).get(SCHEDULE_METADATA_KEY)
    if isinstance(mark, Mapping) and mark.get("schedule_id"):
        return str(mark["schedule_id"])
    return UNSCHEDULED


def keys_of(attempt: Attempt, task: Task | None, *, platform: bool) -> dict[str, str]:
    if platform:
        return {"tenant": attempt.tenant_id,
                "profile": task.runner_profile if task is not None else TASK_GONE}
    return {
        "schedule": schedule_of(task),
        "repository": repository_of(task),
        "profile": task.runner_profile if task is not None else TASK_GONE,
        "person": task.submitted_by if task is not None else TASK_GONE,
    }


def _row() -> dict[str, Any]:
    return {"attempts": 0, "attempts_with_cost": 0, "cost_usd": 0.0}


def _add(row: dict[str, Any], cost: float | None) -> None:
    row["attempts"] += 1
    if cost is not None:
        row["attempts_with_cost"] += 1
        row["cost_usd"] = round(row["cost_usd"] + cost, 6)


def _close(row: dict[str, Any], *, truncated: bool = False) -> dict[str, Any]:
    """NULL IS NOT ZERO, and a sum over unreported rows is a floor."""
    out = dict(row)
    if out["attempts_with_cost"] == 0:
        out["cost_usd"] = None
    out["lower_bound"] = truncated or out["attempts_with_cost"] < out["attempts"]
    return out


def _rank(item: tuple[str, dict[str, Any]]) -> tuple[float, int, str]:
    key, row = item
    return (-(row["cost_usd"] or 0.0), -row["attempts"], key)


def bounded_rows(rows: Mapping[str, dict[str, Any]], limit: int = MAX_ROWS) -> list[dict[str, Any]]:
    """Costliest first. Past `limit`, the rest are ONE `other` row that counts what it holds."""
    ordered = sorted(rows.items(), key=_rank)
    kept = [{"key": key, **_close(row)} for key, row in ordered[:limit]]
    rest = ordered[limit:]
    if rest:
        other = _row()
        for _key, row in rest:
            other["attempts"] += row["attempts"]
            other["attempts_with_cost"] += row["attempts_with_cost"]
            other["cost_usd"] = round(other["cost_usd"] + row["cost_usd"], 6)
        kept.append({"key": OTHER, "keys": len(rest), **_close(other)})
    return kept


def summarise(attempts: Sequence[Attempt], tasks: Mapping[str, Task], *, platform: bool,
              truncated: bool = False) -> dict[str, Any]:
    """The totals, their coverage and one breakdown per dimension."""
    dimensions = PLATFORM_DIMENSIONS if platform else TENANT_DIMENSIONS
    total = _row()
    by: dict[str, dict[str, dict[str, Any]]] = {d: {} for d in dimensions}
    for attempt in attempts:
        task = tasks.get(attempt.task_id)
        cost = _cost(attempt)
        _add(total, cost)
        for dimension, key in keys_of(attempt, task, platform=platform).items():
            _add(by[dimension].setdefault(key, _row()), cost)
    return {
        "total": _close(total, truncated=truncated),
        "coverage": spend_coverage(attempts, tasks),
        "by": {d: bounded_rows(by[d]) for d in dimensions},
    }


def compute(ctx: Any, schedule: Mapping[str, Any], params: Any, now: datetime) -> dict[str, Any]:
    """The report (§3.11). The platform variant reads every tenant, aggregates only."""
    since, until = window_of(params, now)
    platform = (schedule.get("scope") or {}).get("mode") == "platform"
    report: dict[str, Any] = {
        "type": TYPE,
        "scope": "platform" if platform else "tenant",
        "window": {"name": params.window, "from": since.isoformat(), "to": until.isoformat()},
        "truncated": [],
    }
    attempts: list[Attempt] = []
    tasks: dict[str, Task] = {}
    if platform:
        tenants = [t.tenant_id for t in ctx.store.list_tenants(limit=MAX_TENANTS)]
        if len(tenants) >= MAX_TENANTS:
            report["truncated"].append("tenants")
        report["tenants"] = len(tenants)
    else:
        tenants = [str(schedule["tenant_id"])]
    for tenant_id in tenants:
        rows, found, cut = read_tenant(ctx.store, tenant_id, since, until)
        attempts.extend(rows)
        # Task ids are unique platform-wide, and each was read under its own tenant.
        tasks.update(found)
        if cut:
            report["truncated"].append(f"attempts:{tenant_id}" if platform else "attempts")
    report.update(summarise(attempts, tasks, platform=platform, truncated=bool(report["truncated"])))
    return report


# --------------------------------------------------------------------------
# The comment
# --------------------------------------------------------------------------


def marker(firing_id: str) -> str:
    return f"<!-- swarm:cost-report {firing_id} -->"


def money(row: Mapping[str, Any]) -> str:
    cost = row.get("cost_usd")
    if cost is None:
        return "not reported"
    return f"{'at least ' if row.get('lower_bound') else ''}${cost:,.2f}"


def _cell(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def render_comment(report: Mapping[str, Any], firing_id: str, schedule_id: str) -> str:
    total = report["total"]
    coverage = report["coverage"]
    window = report["window"]
    lines = [
        marker(firing_id),
        f"### SwarmCloud cost report: {window['name']}",
        "",
        f"{window['from']} to {window['to']} (schedule `{_cell(schedule_id)}`, firing `{_cell(firing_id)}`).",
        "",
        f"**Total: {money(total)}** over {total['attempts']} attempts, "
        f"{total['attempts_with_cost']} of which reported a cost.",
    ]
    if total["attempts"] > total["attempts_with_cost"]:
        lines.append(
            f"Without a cost: {coverage['in_flight_unreported']} still running, "
            f"{coverage['not_reported_by_profile']} on profiles that report none, "
            f"{coverage['not_recorded']} not recorded, {coverage['profile_unknown']} unknown."
        )
    if report.get("truncated"):
        lines.append(f"The read stopped at its bound ({', '.join(report['truncated'])}): every figure is a floor.")
    for dimension in COMMENT_DIMENSIONS:
        rows = (report.get("by") or {}).get(dimension) or []
        if not rows:
            continue
        lines += ["", f"| {dimension} | cost | attempts | with a cost |", "|---|---:|---:|---:|"]
        for row in rows[:COMMENT_ROWS]:
            key = f"{OTHER} ({row['keys']})" if row.get("keys") else row["key"]
            lines.append(f"| {_cell(key)} | {money(row)} | {row['attempts']} | {row['attempts_with_cost']} |")
        if len(rows) > COMMENT_ROWS:
            lines.append(f"| {len(rows) - COMMENT_ROWS} more rows on the firing | | | |")
    lines += ["", "The by-person breakdown is on the firing in SwarmCloud, not here."]
    return "\n".join(lines) + "\n"


def _target(ctx: Any, schedule: Mapping[str, Any], post_to_issue: str) -> tuple[Any, str | None, str]:
    """(the issue, a refusal code or None, its sentence) -- read from Firestore only."""
    ref = parse_issue_ref(post_to_issue)
    if (schedule.get("scope") or {}).get("mode") == "platform":
        return ref, PLATFORM_NOT_POSTED, (
            "a platform cost report covers every tenant and is visible to admins only "
            "(docs/schedules.md §3.11), so it is never posted to GitHub; read it on the firing"
        )
    tenant_id = str(schedule["tenant_id"])
    found = Repositories(ctx.db, now=ctx.now).find(tenant_id, repo_id_for(tenant_id, ref.owner, ref.repo))
    if found is None:
        return ref, REPOSITORY_NOT_REGISTERED, (
            f"{ref.repository} is not a repository this tenant has registered, so the report "
            "is not posted there; register it, or name an issue in a registered repository"
        )
    return ref, None, ""


def post(ctx: Any, schedule: Mapping[str, Any], ref: Any, body: str, firing_id: str) -> dict[str, Any]:
    """Post `body` once, with the schedule's tenant's credential. Returns the item's outcome and detail."""
    token = ""
    try:
        tenant = ctx.store.get_tenant(str(schedule["tenant_id"]))
        if tenant is None:
            return {"outcome": "failed", "code": "tenant_not_found",
                    "message": "the schedule's tenant is not on record"}
        token = ctx.forge_tokens.token_for(tenant)
        writer = ctx.forge_writer
        found = writer.find_comment(ref, marker(firing_id), token)
        comment = found or writer.create_comment(ref, body, token)
        return {"outcome": "succeeded", "comment_id": comment.id, "url": comment.url,
                "already_posted": found is not None}
    except (ForgeReadError, ForgeWriteError) as exc:
        # Both raise constant, token-free sentences (forge.py, forgewrite.py).
        # `writeback_forbidden` names the permission the credential lacks.
        return {"outcome": "failed", "code": exc.code, "message": exc.message}
    finally:
        token = ""


# --------------------------------------------------------------------------
# The executor seam (`schedulefire.Executor`)
# --------------------------------------------------------------------------


def _ids(firing: Any) -> tuple[str, str]:
    return str(firing.firing["firing_id"]), str(firing.schedule["schedule_id"])


def create(firing: Any) -> list[dict[str, Any]]:
    params = issue_sweep.params_of(firing)
    firing_id, schedule_id = _ids(firing)
    if firing.room < 1:
        log.info("schedule %s firing %s: no room for the cost report", schedule_id, firing_id)
        return []
    report = compute(firing.ctx, firing.schedule, params, firing.now)
    comment: dict[str, Any] | None = None
    if params.post_to_issue:
        ref, code, message = _target(firing.ctx, firing.schedule, params.post_to_issue)
        where = {"issue": ref.short}
        if firing.room < 2:
            comment = {"outcome": "failed", **where, "code": NO_ROOM,
                       "message": "max_concurrent leaves room for the report only, so it was not posted"}
        elif code:
            comment = {"outcome": "failed", **where, "code": code, "message": message}
        else:
            # Posted BEFORE either item is recorded: a crash in between is
            # finished again, and the marker finds the comment already there.
            comment = {**post(firing.ctx, firing.schedule, ref,
                              render_comment(report, firing_id, schedule_id), firing_id), **where}
    if comment is not None and firing.room < 2:
        # No room for a second item: the reason is on the report's own item.
        report["comment"] = {k: v for k, v in comment.items() if k != "outcome"}
    work = [firing.api_action(REPORT, outcome="succeeded", detail=report)]
    if comment is not None and firing.room >= 2:
        detail = {k: v for k, v in comment.items() if k != "outcome"}
        work.append(firing.api_action(COMMENT, outcome=comment["outcome"], detail=detail))
    total = report["total"]
    log.info("schedule %s firing %s: cost report %s over %d attempts (%d with a cost)%s%s",
             schedule_id, firing_id, money(total), total["attempts"], total["attempts_with_cost"],
             " truncated" if report["truncated"] else "",
             f"; comment {comment.get('code') or 'posted'}" if comment is not None else "")
    return work


def dry_run(firing: Any) -> dict[str, Any]:
    params = issue_sweep.params_of(firing)
    firing_id, schedule_id = _ids(firing)
    report = compute(firing.ctx, firing.schedule, params, firing.now)
    out: dict[str, Any] = {"report": report, "comment": None}
    if params.post_to_issue:
        ref, code, message = _target(firing.ctx, firing.schedule, params.post_to_issue)
        body = render_comment(report, firing_id, schedule_id)
        out["comment"] = {
            "issue": ref.short,
            "would": "not_post" if code else "post",
            **({"code": code, "message": message} if code else {}),
            "bytes": len(body.encode()),
        }
    return out
