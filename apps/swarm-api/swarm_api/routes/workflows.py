"""Workflow routes.

The tenant on every read comes from `tenant_scope`, not from `auth.tenant_id`:
a tenant id is derived from the verified identity but is not unique to one, so
the raw string filters by a value two unrelated principals can both hold. See
`swarm_api.deps.tenant_scope`.

EVERY READ DERIVES. `Workflow.state` in Firestore is a cache of a computation
over the step tasks, and until this change nothing ever refreshed it -- a
workflow read QUEUED for its whole life. Both read routes now compute the state
from the steps, serve that, report whether it agrees with the stored copy, and
write the stored copy back when it does not. See `swarm_api.rollup` for why the
derived value is the one a reader is given.

WRITING ON A GET is deliberate and is the narrow kind: the write fires only when
the two records already disagree, so a healthy platform does zero writes here,
and the disagreement is counted and logged BEFORE the repair (`rollup._record`)
so nothing is resolved without a trace. The alternative -- a background sweep as
the only writer -- would leave every workflow nobody had swept unqueryable, which
is the half of the owner's decision the write exists to satisfy.

NAMES BESIDE EMAILS (docs/design/user-profile.md §4.4). Every workflow and
step task these reads serve carries `submitted_by_name` beside
`submitted_by`, from one lookup per response (`_named`). A name is a label;
the submitter filter keeps using the email.
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from swarm_common.models import Task
from swarm_common.states import TERMINAL_STATES

from .. import sessiontag
from ..auth import AuthContext
from .tasks import require_member
from ..attempt_totals import IN_LIMIT, totals_for, with_totals
from ..children import PARENT_CANCELLED, ChildService
from ..codec import task_to_api, workflow_claude_session, workflow_dispatch, workflow_to_api
from ..waiting_detail import build_waiting, waiting_row
from ..task_accounts import accounts_for
from ..errors import ApiError, Conflict, Forbidden, ValidationFailed
from .. import refusals, roles
from ..mcp_credentials import (
    McpCredentials,
    RowKeys,
    SecretManagerMcpCredentials,
    WorkflowFinished,
    check_restore,
    promote_restored,
    restore_workflow,
    step_task_docs,
)
from ..mcp_servers import McpUnavailable, require_enabled as require_mcp_enabled
from ..people import add_names, name_lookup
from ..deps import (
    AppContext,
    current_auth,
    get_context,
    paged_limit,
    submission_scope,
    tenant_scope,
)
from ..rollup import RollupResult, SweepReport
from ..schemas import McpCredentialCreate, WorkflowCreate, _mcp_credentials_field
from ..stalls import stalled_workflows
from ..store import CancelContended, workflow_listing_token
from ..waker import ring

log = logging.getLogger(__name__)

_TERMINAL = frozenset(s.value for s in TERMINAL_STATES)

router = APIRouter(prefix="/v1/workflows", tags=["workflows"])

#: `GET /v1/workflows?view=` (live QA CH-21). `full`, or no `view` at all, is
#: the row this route has always served; `summary` drops each step's input.
WORKFLOW_VIEWS: tuple[str, ...] = ("full", "summary")

#: `GET /v1/workflows?ids=` (live QA CH-6): how many workflows one read may
#: name. 50 workflows of up to `max_workflow_steps` (50) steps is the step-read
#: cost of a 50-row list page; the Timeline asks in chunks of this size.
MAX_WORKFLOW_IDS = 50

#: `GET /v1/workflows?claude_session=&repository=` (CHECK-FILTER, #1749): the
#: store page the filter reads, and how many of them one request reads before
#: it answers with what matched and a token. Neither the session nor the
#: repository is a field of the frozen `Workflow` -- both are on its step
#: tasks -- so no Firestore index can serve them and the route filters after
#: deriving. 10 pages of 100 is 1,000 running workflows a request: past the
#: 250 tenant eng had running on 2026-10-20, and a bound on what one request
#: can cost, since every row scanned is a rollup's step reads.
FILTER_PAGE_SIZE = 100
MAX_FILTER_PAGES = 10

#: Why a row the scope filter read was not served, counted in
#: `filter.left_out`: the bridge reports other sessions' and untagged work
#: as left out (SC-SESSION-SCOPE), and cannot count what it never received.
LEFT_OUT_REASONS: tuple[str, ...] = ("other_session", "untagged", "other_repository")

#: `owner@host:path`, the scp-like spelling git accepts for ssh -- the same
#: pattern `swarm_mcp.checkout._SCP_LIKE` reads.
_SCP_LIKE = re.compile(r"^(?:[^@/\s]+@)?(?P<host>[^:/\s]+):(?P<path>[^\s]+)$")


def repository_key(value: Any) -> str | None:
    """`host/owner/repo`, lower-cased, for any spelling of one repository --
    https (with or without `.git`, userinfo or a port), ssh://, git@host:, or
    bare `owner/repo` (GitHub) -- and None for anything that names none.

    THE BRIDGE'S RULE, RESTATED BECAUSE THE TWO APPS SHARE NO PACKAGE BUT THE
    FROZEN ONE: `swarm_mcp.check.repo_key` is what the bridge scopes by, and a
    filter that normalised differently would leave out work the bridge counts
    as in scope. `test_workflow_list_scope_filter` holds the two equal over
    every spelling either has met."""
    text = str(value or "").strip()
    if not text:
        return None
    if "://" not in text and "@" not in text and ":" not in text:
        # `host/owner/repo` names its host; only a bare `owner/repo` is
        # GitHub's. A GitHub owner never holds a dot, a host always does.
        first = text.strip("/").split("/", 1)[0]
        text = f"https://{text}" if "." in first else f"https://github.com/{text}"
    scp = None if "://" in text else _SCP_LIKE.match(text)
    if scp and not text.startswith(("/", ".", "~")):
        text = f"https://{scp.group('host')}/{scp.group('path').lstrip('/')}"
    try:
        parts = urlsplit(text)
        host = parts.hostname
    except ValueError:
        return None
    if parts.scheme.lower() not in ("https", "ssh", "git+ssh") or not host:
        return None
    segments = [s for s in parts.path.strip("/").split("/") if s]
    if len(segments) < 2:
        return None
    owner, repo = segments[0], segments[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    return f"{host}/{owner}/{repo}".lower() if owner and repo else None


@router.post("", status_code=status.HTTP_201_CREATED)
def create_workflow(
    body: WorkflowCreate,
    response: Response,
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    # `member` or above (workspace-roles.md §4.1), before anything is written.
    require_member(ctx, auth)
    submission = ctx.submissions.submit_workflow(auth, body)
    workflow = submission.workflow
    response.headers["Location"] = f"/v1/workflows/{workflow.workflow_id}"
    return {
        # No rollup on the create response. The step tasks were written
        # microseconds ago by `create_workflow` and deriving over them would
        # spend a read per step to be told what this request just decided. The
        # response says `state_source: "stored"`, which is true and is the point
        # of that field.
        #
        # Each step's input is masked by its own task's masker (the PR #229
        # review): the tasks this request just built carry the workflow's
        # metadata, and the step copy must mask what the task copy masks.
        "workflow": workflow_to_api(
            workflow,
            step_tasks={task.id: task for task in submission.tasks},
            console_url=ctx.settings.console_url,
        ),
        # Echoed so the caller sees what was ACCEPTED rather than what they
        # sent: a submission that named neither field gets the defaults back,
        # and an `integrate` workflow is told which step will open the one PR.
        "dispatch": {
            "strategy": submission.dispatch.strategy,
            "carrier": submission.dispatch.carrier,
            "integrator_step_id": submission.integrator_step_id,
            # Only when set, and as RESOLVED: a continuation of a continuation
            # names the original task, whose branch is the one pushed to.
            **(
                {"continues_task": submission.dispatch.continues}
                if submission.dispatch.continues
                else {}
            ),
        },
    }


@router.get("")
def list_workflows(
    limit: int | None = Query(default=None, ge=1),
    page_token: str | None = Query(default=None),
    state: list[str] | None = Query(default=None),
    active: bool = Query(default=False),
    view: str | None = Query(default=None, description="`full` (default) or `summary`"),
    ids: list[str] | None = Query(default=None),
    claude_session: str | None = Query(default=None),
    repository: str | None = Query(default=None),
    tenant_id: str = Depends(tenant_scope),
    submitted_by: str | None = Depends(submission_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """One page of the tenant's workflows, newest first.

    `state` (repeatable) keeps only the workflows whose DERIVED state is one of
    those named, and `active=true` only those whose derived state is not
    terminal (UNKNOWN included) -- the bridge's `sc workflows` asks for the
    unfinished ones (owner decision 2026-10-02: running workflows show in
    Claude Code without attaching each by id).

    TWO FILTERS, ONE ON EACH RECORD (owner decision 2026-10-06, P4). The QUERY
    filters on the stored state (`Store.stored_states_for`): it leaves out only
    the stored states no derivation can change (SUCCEEDED, CANCELLED), so a
    workflow whose cache is stale is still fetched. Then each row is derived
    and filtered on the derived state, which is still the only one served, and
    the read repairs a stale cache. Before this the filter ran on the derived
    state alone, over a page of the tenant's whole history: 4 pages of 50 to
    find 7 running workflows. Now the running set is one page, newest first,
    paged on the same (created_at, workflow_id) keyset. A page can still come
    back short when stale rows derived terminal; page on until the token is
    null. `filter.stored_states` says which stored states the query asked
    for, and null when it could not narrow it (UNKNOWN named, or no filter).

    `view=summary` (live QA CH-21, 2026-10-11) serves each step without its
    `input` and that input's masking counts -- the keys absent, not null, as
    `GET /v1/tasks?view=summary` drops its rows' -- and says `"view":
    "summary"`. Every other field, the derived state and the rollup included,
    is the full view's: the console's lists draw no step input, and a step's
    input is read on its workflow or its task.

    `ids` (repeatable, at most `MAX_WORKFLOW_IDS`; live QA CH-6) reads THOSE
    workflows instead of a page: the Timeline named 52 workflows and asked for
    each with its own `GET /v1/workflows/{id}`. The rows come back in the
    order asked, derived like any other row; `missing` names the ids not
    served (absent, another tenant's or not the caller's, never told apart),
    and `tasks` carries every step task the derivation read, served as
    `GET /v1/workflows/{id}` serves its own, with its account and attempt
    totals. It takes no `page_token`, `state` or `active`: it is not a page.

    `claude_session` and `repository` (CHECK-FILTER, owner decision
    2026-10-20, #1749) keep only the workflows a step task of which carries
    that `metadata.claude_session` / clones that repository (any spelling,
    `repository_key`); each combines with the state filters. The sc plugin's
    check and attach-all read only their own session's work this way, where
    they used to page through the whole tenant's running set and stop after
    four pages of it. Neither is a field of the frozen `Workflow`, so no
    index can serve them: the route derives a store page, keeps what matches,
    and pages on INSIDE the request until `limit` rows matched or
    `MAX_FILTER_PAGES` store pages were read -- one page of what matched,
    and a token that continues past its last row. `filter` echoes both
    (normalised), so a client tells this route from one that ignored them,
    and `filter.left_out` counts the rows read and not served, by why.
    """
    if view is not None and view not in WORKFLOW_VIEWS:
        raise ValidationFailed(
            f"unknown view {view!r}",
            detail={"known_views": list(WORKFLOW_VIEWS)},
        )
    summary = view == "summary"
    session = repo = None
    if claude_session is not None:
        session = sessiontag.valid(claude_session)
        if session is None:
            raise ValidationFailed(
                f"claude_session {claude_session!r} is not a Claude Code session id",
                detail={"pattern": sessiontag.PATTERN},
            )
    if repository is not None:
        repo = repository_key(repository)
        if repo is None:
            raise ValidationFailed(
                f"repository {repository!r} names no repository; give a URL or owner/repo",
            )
    if ids:
        if session is not None or repo is not None:
            raise ValidationFailed(
                "ids reads the named workflows, not a page: it takes no claude_session or repository",
                detail={"ids_exclusive_of": ["claude_session", "repository"]},
            )
        return _by_ids(
            ids, view=view, page_token=page_token, state=state, active=active,
            tenant_id=tenant_id, submitted_by=submitted_by, ctx=ctx,
        )
    stored_states = ctx.store.stored_states_for(active=active, states=state)
    scope: dict[str, Any] = {}
    if session is None and repo is None:
        page = ctx.store.list_workflows(
            tenant_id,
            limit=paged_limit(ctx, limit),
            page_token=page_token,
            submitted_by=submitted_by,
            stored_states=stored_states,
        )
        results, report = ctx.rollups.for_workflows(tenant_id, page.items)
        results = _in_states(results, active=active, state=state)
        next_page_token = page.next_page_token
    else:
        results, report, next_page_token, left_out = _scoped_page(
            ctx, tenant_id, limit=paged_limit(ctx, limit), page_token=page_token,
            submitted_by=submitted_by, stored_states=stored_states, active=active, state=state,
            session=session, repo=repo,
        )
        scope = {"claude_session": session, "repository": repo, "left_out": left_out}
    # The summary view is the Workflows board's one read: each step carries
    # what the board draws (`codec.step_task_summary`), its attempt totals
    # read here in batches. The full view reads no attempts, as before.
    step_totals = _results_totals(ctx, tenant_id, results, submitted_by) if summary else None
    payload = {
        # Each step's input is masked by its own task's masker, from the step
        # tasks the rollup already read (the PR #229 review); a step whose task
        # the read budget left unread borrows a sibling's, and a workflow none
        # of whose tasks were read serves its step inputs as null.
        "workflows": [
            workflow_to_api(
                r.workflow,
                r.to_api(),
                step_tasks=r.step_tasks,
                console_url=ctx.settings.console_url,
                summary=summary,
                step_totals=step_totals,
            )
            for r in results
        ],
        "next_page_token": next_page_token,
        "tenant_id": tenant_id,
        # What was filtered and where. A client that sees `stored_states` knows
        # the running set came from the indexed query, not a walk through
        # history, and that one page holds it unless a token says otherwise; a
        # deployment older than this serves no `filter` at all.
        "filter": {
            "active": active,
            "states": list(state) if state else None,
            "stored_states": stored_states,
            **scope,
        },
        # What deriving this page cost and whether it was complete. A caller
        # that saw `step_read_budget_exhausted` knows some rows below read
        # UNKNOWN because this route stopped reading, not because anything is
        # wrong with those workflows.
        "rollup_report": report.to_api(),
        # What the reconciler's workflow stall check last found for this
        # tenant (#616): the console's Overview "Needs a look" and `sc trouble`
        # read it here. `count` is null, with `check_error` saying why, when
        # the check's result is not known -- never zero (swarm_api.stalls).
        "stalled_workflows": stalled_workflows(ctx.store.db, tenant_id),
    }
    _named(ctx, payload["workflows"], [])
    if summary:
        payload["view"] = "summary"
    return payload


def _in_states(
    results: list[RollupResult], *, active: bool, state: list[str] | None
) -> list[RollupResult]:
    """The rows whose DERIVED state the `active` / `state` filters keep."""
    if active:
        results = [r for r in results if r.to_api().get("state") not in _TERMINAL]
    if state:
        wanted = set(state)
        results = [r for r in results if r.to_api().get("state") in wanted]
    return results


def _left_out_because(result: RollupResult, session: str | None, repo: str | None) -> str | None:
    """Why the scope filter does not serve this row (`LEFT_OUT_REASONS`), or
    None when it matches. The session is the one the row is SERVED with
    (`workflow_claude_session`, off its step tasks through their maskers), so
    the filter keeps exactly the rows a client comparing `claude_session`
    would keep; a workflow none of whose step tasks could be read carries no
    tag and counts as untagged."""
    if session is not None:
        tagged = workflow_claude_session(result.step_tasks).get(sessiontag.KEY)
        if tagged != session:
            return "other_session" if tagged else "untagged"
    if repo is not None:
        if repo not in {repository_key(t.repository_url) for t in result.step_tasks.values()}:
            return "other_repository"
    return None


def _merge(total: SweepReport, part: SweepReport) -> None:
    for name in ("examined", "written", "agreed", "disagreed", "unknown", "step_reads"):
        setattr(total, name, getattr(total, name) + getattr(part, name))
    total.step_read_budget_exhausted = total.step_read_budget_exhausted or part.step_read_budget_exhausted


def _scoped_page(
    ctx: AppContext,
    tenant_id: str,
    *,
    limit: int,
    page_token: str | None,
    submitted_by: str | None,
    stored_states: list[str] | None,
    active: bool,
    state: list[str] | None,
    session: str | None,
    repo: str | None,
) -> tuple[list[RollupResult], SweepReport, str | None, dict[str, int]]:
    """`(rows, report, next_page_token, left_out)`: one page of the rows the
    session / repository filter keeps, read store page by store page.

    THE TOKEN IS THE LAST ROW SERVED'S. When `limit` matches are reached
    before the end of a store page, the page's own token would skip the rows
    after the last one served, so the token is minted on that row
    (`workflow_listing_token`) and the rest of the page is read again by the
    next request -- and counted in `left_out` only then, so a walk counts
    each row once. When the store page ends exactly there, or the scan stops
    at `MAX_FILTER_PAGES`, the store page's token is the position."""
    matched: list[RollupResult] = []
    report = SweepReport()
    left_out = {reason: 0 for reason in LEFT_OUT_REASONS}
    token = page_token
    for _ in range(MAX_FILTER_PAGES):
        page = ctx.store.list_workflows(
            tenant_id,
            limit=FILTER_PAGE_SIZE,
            page_token=token,
            submitted_by=submitted_by,
            stored_states=stored_states,
        )
        results, read = ctx.rollups.for_workflows(tenant_id, page.items)
        _merge(report, read)
        token = page.next_page_token
        kept = _in_states(results, active=active, state=state)
        for index, result in enumerate(kept):
            why = _left_out_because(result, session, repo)
            if why is not None:
                left_out[why] += 1
                continue
            matched.append(result)
            if len(matched) == limit:
                if index < len(kept) - 1:
                    token = workflow_listing_token(result.workflow)
                return matched, report, token, left_out
        if token is None:
            break
    return matched, report, token, left_out


#: Attempts queries a workflow read runs at once. Each is one `in` query of
#: `IN_LIMIT` step tasks; a page of 100 workflows of 5 steps is 17 of them.
TOTALS_WORKERS = 8


def _step_totals(ctx: AppContext, tenant_id: str, task_ids: list[str]) -> dict[str, dict[str, Any]]:
    """`totals_for` over these step tasks, its `IN_LIMIT` chunks run CONCURRENTLY.

    WHY (console load speed, owner's pick 2026-11-01). swarm-api's time per
    request was serial Firestore round trips, not CPU (Cloud Monitoring,
    2026-11-01: 47-68% CPU on 4-8 instances), and `totals_for` runs its
    chunks one after another. Each chunk is the same one query it always
    was, and each answers for its own tasks alone, so running them at once
    changes no figure: a page's attempts cost one round trip of wall time,
    not one per 30 step tasks.
    """
    wanted = list(dict.fromkeys(t for t in task_ids if t))
    chunks = [wanted[i : i + IN_LIMIT] for i in range(0, len(wanted), IN_LIMIT)]
    if len(chunks) <= 1:
        return totals_for(ctx.db, tenant_id, wanted)
    out: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=min(TOTALS_WORKERS, len(chunks)),
                            thread_name_prefix="step-totals") as pool:
        for part in pool.map(lambda chunk: totals_for(ctx.db, tenant_id, chunk), chunks):
            out.update(part)
    return out


def _results_totals(
    ctx: AppContext, tenant_id: str, results: list[RollupResult], submitted_by: str | None
) -> dict[str, dict[str, Any]]:
    """The attempt totals of every step task the rollup read for these rows.

    The tasks are the ones the rollup's `get_all` already returned
    (`RollupResult.step_tasks`); no task document is read again. Filtered by
    submitter as `_by_ids` filters its `tasks`: a step task that is not a
    continuation-scoped caller's gets no entry, so its step's `task_summary`
    is null whatever workflow it claims to belong to.
    """
    ids = [
        task.id
        for r in results
        for task in r.step_tasks.values()
        if submitted_by is None or task.submitted_by == submitted_by
    ]
    return _step_totals(ctx, tenant_id, ids)


def _named(ctx: AppContext, workflows: list[dict], tasks: list[dict]) -> None:
    """`submitted_by_name` on every workflow and step task served, from ONE
    lookup for the whole response."""
    add_names(name_lookup(ctx), [*workflows, *tasks], ("submitted_by",))


def _by_ids(
    ids: list[str],
    *,
    view: str | None,
    page_token: str | None,
    state: list[str] | None,
    active: bool,
    tenant_id: str,
    submitted_by: str | None,
    ctx: AppContext,
) -> dict:
    """`GET /v1/workflows?ids=`: the named workflows, with their step tasks."""
    wanted = list(dict.fromkeys(i for i in ids if i))
    if len(wanted) > MAX_WORKFLOW_IDS:
        raise ValidationFailed(
            f"at most {MAX_WORKFLOW_IDS} ids in one read, {len(wanted)} given",
            detail={"max_ids": MAX_WORKFLOW_IDS},
        )
    if page_token is not None or state or active:
        raise ValidationFailed(
            "ids reads the named workflows, not a page: it takes no page_token, state or active",
            detail={"ids_exclusive_of": ["page_token", "state", "active"]},
        )
    summary = view == "summary"
    found = ctx.store.workflows_by_id(tenant_id, wanted, submitted_by=submitted_by)
    results, report = ctx.rollups.for_workflows(tenant_id, found)
    # Every step task the derivation read, once, filtered as
    # `GET /v1/workflows/{id}` filters its own: a row that is not the
    # caller's is not served whatever workflow it claims to belong to.
    tasks: dict[str, Task] = {}
    for r in results:
        for task in r.step_tasks.values():
            if submitted_by is None or task.submitted_by == submitted_by:
                tasks.setdefault(task.id, task)
    step_tasks = list(tasks.values())
    accounts = accounts_for(ctx.db, tenant_id, step_tasks)
    totals = _step_totals(ctx, tenant_id, [t.id for t in step_tasks])
    served = {w.workflow_id for w in found}
    payload = {
        "workflows": [
            workflow_to_api(
                r.workflow,
                r.to_api(),
                step_tasks=r.step_tasks,
                console_url=ctx.settings.console_url,
                summary=summary,
                # Only the step tasks this read serves: a row that is not the
                # caller's carries a null block, as it is left out of `tasks`.
                step_totals=totals,
            )
            for r in results
        ],
        "tasks": [
            with_totals(
                task_to_api(
                    t,
                    account=accounts.get(t.id),
                    console_url=ctx.settings.console_url,
                    # #1782: a waiting step task's one line, off its document.
                    waiting=waiting_row(build_waiting(t, as_of=ctx.now())),
                ),
                totals,
            )
            for t in step_tasks
        ],
        "missing": [i for i in wanted if i not in served],
        "next_page_token": None,
        "tenant_id": tenant_id,
        "filter": {"active": False, "states": None, "stored_states": None, "ids": wanted},
        "rollup_report": report.to_api(),
        "stalled_workflows": stalled_workflows(ctx.store.db, tenant_id),
    }
    _named(ctx, payload["workflows"], payload["tasks"])
    if summary:
        payload["view"] = "summary"
    return payload


@router.get("/{workflow_id}")
def get_workflow(
    workflow_id: str,
    tenant_id: str = Depends(tenant_scope),
    submitted_by: str | None = Depends(submission_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    workflow = ctx.store.get_workflow(tenant_id, workflow_id, submitted_by=submitted_by)
    # The same filter on the step tasks: a workflow the caller submitted has
    # step tasks the caller submitted, and a row that is not theirs is not
    # served whatever workflow it claims to belong to.
    page = ctx.store.list_tasks(
        tenant_id, workflow_id=workflow_id, limit=200, submitted_by=submitted_by
    )
    # EVERY STEP'S TASK, READ BY ID WHEN THE PAGE DOES NOT HOLD IT (STATUS-TRUE,
    # part of #1914). A merge fix round is appended as a step (`ci-fix:<n>`)
    # after submission, and its task belongs to the round's OWN workflow
    # (`mergewake._hand_off`), so the `workflow_id` query never returns it:
    # wf_15160fca1d07438ab594 derived UNKNOWN "step_tasks_missing ... ci-fix:1"
    # while that task read RUNNING. A page the workflow outgrew is the same
    # gap. The step ids are in hand, so the missing ones are point reads
    # (`tasks_by_id`, tenant-checked), filtered by submitter as the page is;
    # every step is then either on the page or read by id, which is what makes
    # one still missing ABSENT, and the derivation complete.
    tasks = list(page.items)
    held = {t.id for t in tasks}
    named = [s.task_id for s in workflow.steps if s.task_id and s.task_id not in held]
    if named:
        found = ctx.store.tasks_by_id(tenant_id, named)
        tasks += [
            found[i] for i in dict.fromkeys(named)
            if i in found and (submitted_by is None or found[i].submitted_by == submitted_by)
        ]
    # Derived from the tasks this route already loaded rather than from a second
    # read of the same documents.
    result = ctx.rollups.for_workflow_from_tasks(workflow, tasks, complete=True)
    accounts = accounts_for(ctx.db, tenant_id, tasks)
    totals = _step_totals(ctx, tenant_id, [t.id for t in tasks])

    payload = {
        # The step copies of each input are masked by the tasks' own maskers,
        # so `workflow.steps[i].input` and `tasks[i].input` agree (the PR #229
        # review), from the tasks this route already loaded.
        "workflow": workflow_to_api(
            workflow,
            result.to_api(),
            step_tasks={t.id: t for t in tasks},
            console_url=ctx.settings.console_url,
            step_totals=totals,
        ),
        # Read back off the tasks, which is where the options are stored; the
        # frozen `Workflow` dataclass has no metadata field to hold them. The
        # list route has no equivalent because it loads no tasks. The
        # workflow's own tasks only: a fix round's task read by id is its own
        # workflow's, with that workflow's options.
        "dispatch": workflow_dispatch(page.items),
        # #379: each step's account, one bounded query per 30 steps. And each
        # step's totals across every attempt (lane review P1), which the
        # console's Workflows cost columns show, read the same bounded way
        # (`attempt_totals.totals_for`).
        "tasks": [
            with_totals(
                task_to_api(
                    t,
                    account=accounts.get(t.id),
                    console_url=ctx.settings.console_url,
                    # #1782: a waiting step task's one line, off its document.
                    waiting=waiting_row(build_waiting(t, as_of=ctx.now())),
                ),
                totals,
            )
            for t in tasks
        ],
    }
    _named(ctx, [payload["workflow"]], payload["tasks"])
    return payload


@router.post("/{workflow_id}/cancel")
def cancel_workflow(
    workflow_id: str,
    # The scope decides whose workflow may be cancelled; `auth` records who did.
    background: BackgroundTasks,
    tenant_id: str = Depends(tenant_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    # Anyone's workflow, but only by a `member` or above (Q8): a `viewer`
    # cancels nothing, their own included.
    require_member(ctx, auth)
    targets: list = []
    # A step whose cancel stayed contended through its retries (CANCEL-RETRY,
    # #1822) does not stop the others: the store cancels them and then refuses
    # with what it did. Every step it cancelled is still stopped, cascaded and
    # woken below, so the refusal is RETURNED, not raised -- a raised error's
    # response would drop these background tasks, and a retry names no
    # execution for a step whose first cancel already committed.
    refused: CancelContended | None = None
    try:
        result = ctx.store.cancel_workflow(
            tenant_id, workflow_id, by=auth.email, tenant_member=auth.tenant_member,
            targets=targets,
        )
    except CancelContended as exc:
        refused = exc
        result = exc.result or {}
    # Each running step's execution is asked to stop now, as the task route
    # does (#627); a step cancelled before names none.
    for target in targets:
        background.add_task(ctx.executions.cancel, target)
    # A cancelled step's children are cancelled with it (OD-B15-4,
    # docs/design/child-tasks.md §3.4); the scheduler's sweep makes it certain.
    service = ChildService(
        settings=ctx.settings, db=ctx.db, store=ctx.store, submissions=ctx.submissions,
        verifier=None, now=ctx.now,
    )
    children: list = []
    for task_id in result.get("tasks_cancelled") or []:
        try:
            service.cascade(
                tenant_id, task_id, why=PARENT_CANCELLED, by=auth.email, targets=children
            )
        except Exception:
            log.exception("child cascade of step %s failed; the scheduler sweep retries it", task_id)
    for child_target in children:
        background.add_task(ctx.executions.cancel, child_target)
    # ONE DRAIN, NOT ONE FINISH WAKE PER STEP (#636). Every step of this
    # workflow is cancelled here, so no dependant inside it waits to be
    # released; what a cancel can free is capacity -- a step with no live
    # worker gives its lease back in its cancel -- and READY work elsewhere
    # waited a safety tick for it. A drain admits that work and runs the
    # dependency sweep too, which also resolves the dependants of the children
    # the cascade above cancelled -- so unlike the task route, those children
    # ring no `task_finished` of their own. After the response, as the task
    # route does.
    if result.get("tasks_cancelled"):
        background.add_task(
            ring, ctx.waker, ctx.metrics, "workflow_cancelled",
            tenant_id=tenant_id, workflow_id=workflow_id,
        )
    if refused is not None:
        # The shape and header `main.api_error_handler` gives a raised
        # `RateLimited`; FastAPI attaches `background` to a returned response.
        return JSONResponse(
            status_code=refused.status_code,
            content=refused.to_payload(),
            headers={"Retry-After": str(max(1, int(refused.retry_after_seconds) + 1))},
        )
    return result


# --------------------------------------------------------------------------
# A workflow's per-run MCP credentials, re-stored by its caller
# --------------------------------------------------------------------------

#: The re-store route (docs/design/registered-mcp-servers.md §4.10, BGD
#: 2026-10-26). Its body carries credentials, so `main.UNCAPTURED_ROUTES`
#: names it.
MCP_CREDENTIALS_ROUTE = "/v1/workflows/{workflow_id}/mcp-credentials"


class McpCredentialRestore(BaseModel):
    """`{"mcp_credentials": {<server>: {"value", "expires_in"}}}`: the same
    per-server object the workflow-create body takes, checked the same way
    (`mcp_credentials.check_credential`), and never dumped or repr'd."""

    model_config = ConfigDict(extra="forbid")

    mcp_credentials: dict[str, McpCredentialCreate] | None = _mcp_credentials_field()


class McpCredentialRestoreForbidden(Forbidden):
    code = "mcp_credential_restore_forbidden"


def _refused(error: ApiError, established: type[ApiError]) -> ApiError:
    """A NEW refusal ships off (observer proposal I; `swarm_api.refusals`),
    as `egress._refuse` does: the refusal is always made, and with its switch
    off the caller gets the same status and message under the established
    code (`conflict` 409, `forbidden` 403); on, `refuse` raises it whole."""
    refusals.refuse(error)
    return established(error.message, detail=error.detail)


def _may_restore(ctx: AppContext, auth: AuthContext, submitted_by: str | None) -> bool:
    """The workflow's submitter (a tenant's service identity included), or
    an admin of this workspace (a platform admin is one, `roles.resolve`)."""
    email = (auth.email or "").strip().lower()
    if submitted_by and email and submitted_by.strip().lower() == email:
        return True
    if auth.is_admin:
        return True
    store = getattr(ctx, "roles", None)
    me = store.try_resolve(auth) if store is not None else None
    return me is not None and me.role == roles.ADMIN


@router.put("/{workflow_id}/mcp-credentials")
def restore_mcp_credentials(
    workflow_id: str,
    body: McpCredentialRestore,
    background: BackgroundTasks,
    tenant_id: str = Depends(tenant_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """Replace the workflow's per-run credential for each server the body
    names; servers it does not name are untouched (§4.10, BGD 2026-10-26).

    A step that waited past its credential's expiry is PARKED
    CREDENTIAL_MISSING with `blocked_by[].credential == "mcp"`; the re-store
    returns each such step of this workflow to READY in this request, and
    wakes the scheduler, whose admission asks the whole credential question
    again before any lease (invariants 1-3). A child task of a step is
    promoted by the scheduler's credential sweep on that drain.

    404 for a workflow of another tenant, as GET answers; 403 for anyone but
    its submitter or an admin of the workspace; 409 `workflow_finished` once
    every step has ended; 422 for a server no step names, before anything
    is stored. The answer names each server and its new `expires_at`, never
    a value; so does the log."""
    require_mcp_enabled()
    workflow = ctx.store.get_workflow(tenant_id, workflow_id, submitted_by=None)
    if not _may_restore(ctx, auth, workflow.submitted_by):
        raise _refused(McpCredentialRestoreForbidden(
            "only the identity that submitted this workflow, or an admin of the "
            "workspace, may re-store its MCP credentials; nothing was stored",
            detail={"workflow_id": workflow_id}), Forbidden)
    docs = step_task_docs(ctx.db, tenant_id, workflow_id)
    finished = (all(d.get("state") in _TERMINAL for d in docs.values()) if docs
                else getattr(workflow.state, "value", workflow.state) in _TERMINAL)
    if finished:
        raise _refused(WorkflowFinished(
            f"workflow {workflow_id!r} has finished, so no step of it will use a "
            "credential; nothing was stored", detail={"workflow_id": workflow_id}), Conflict)
    credentials = body.mcp_credentials or {}
    registrations = check_restore(ctx.db, tenant_id, credentials, docs.values())
    keys = RowKeys.from_settings(ctx.settings)
    if not keys.configured:
        raise McpUnavailable(
            "this deployment has no swarm-child-key, so it can neither attest a per-run "
            "MCP credential nor attach one; nothing was stored")
    submissions = ctx.submissions
    if submissions.mcp_credential_writer is None:
        submissions.mcp_credential_writer = SecretManagerMcpCredentials(ctx.settings.project_id)
    writer = submissions.mcp_credential_writer
    store = McpCredentials(ctx.db, writer, now=ctx.now, keys=keys)
    stored = restore_workflow(
        store, ctx.db, writer, tenant_id=tenant_id, workflow_id=workflow_id,
        registrations=registrations, credentials=credentials, by=auth.email)
    promoted = promote_restored(
        ctx.db, tenant_id=tenant_id, workflow_id=workflow_id, task_ids=list(docs),
        servers=registrations, now=ctx.now())
    log.info("mcp credentials re-stored workflow=%s tenant=%s by=%s servers=%s promoted=%d",
             workflow_id, tenant_id, auth.email, sorted(registrations), promoted)
    background.add_task(
        ring, ctx.waker, ctx.metrics, "mcp_credential_restored",
        tenant_id=tenant_id, workflow_id=workflow_id,
    )
    return {"stored": stored, "promoted": promoted}
