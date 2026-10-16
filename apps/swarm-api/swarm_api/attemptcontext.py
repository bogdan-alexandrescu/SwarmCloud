"""Layer I, in-flight work, for one attempt (docs/design/context-quality.md §4.2, §4.4; lane CTXQ-I).

Layers S, T, H and R are staged with the graph snapshot; layer I cannot be,
because it changes by the minute. This module answers it live:

    POST /v1/attempts/{attempt_id}/context/inflight
         {files, + the attempt's fencing fields}
      -> the OTHER in-flight lanes of the task's own tenant and repository
         whose territory meets those files

The agent asks through swarm-graph's one remote tool, `in_flight`
(`agent_worker.graphmcp.remote`); the worker, which holds the attempt key,
signs and sends it.

EVERY CALL, IN THIS ORDER -- the git tools' and the search tool's checks
(`swarm_api.gittools`, `swarm_api.search`), reused rather than restated:

1. **who.** `ChildService.authenticate_worker` (the tenant's worker service
   account, audience-pinned) and `ChildService.verify_proof` (the attempt key
   this attempt registered before its agent existed). Every failure is ONE
   403, `child_submit_unproven`, and nothing is read for it.
2. **the fence (invariant 5).** The task must be RUNNING under exactly this
   attempt's live lease with no cancel requested; a stale attempt is 409
   `child_submit_fenced`. Nothing is written either way: this route reads, so it
   keeps no audit and no quota, and a fenced call has nothing to record.
3. **the signed spec.** The repository is read from the task document only
   after its spec signature verifies (`verdictpublish.spec_refusal`); an
   unverified one is 422 (`spec_unverified: ...`), before the forge is read. The
   caller names files, never a repository, a tenant's other id or a token
   (invariant 10): the body is strict.

WHAT IS LISTED, all of it the task's own tenant's (invariant 9):

* **open pull requests** of the repository whose changed files meet the
  territory, read with the tenant's own token through
  `territory.open_pull_requests` (cached OPEN_PULLS_TTL_SECONDS per tenant
  and repository). A pull request whose files GitHub did not give is
  `pull_requests_unread`; a forge that could not be read is
  `pull_requests_read.ok: false` with its code, never an empty list.
* **live issue-run steps** (`territory.live_lanes`, which reads
  `IssueRuns.live` for this tenant only), one row per planned step whose
  declared `files` meet the territory. A run still planning is
  `lanes_undeclared`.
* **running tasks** of this tenant on this repository that are not a step of
  a live run: they declare no files, so their territory is not known and
  they are `tasks_undeclared` -- named, never counted as "no overlap".

THE TERRITORY is the named files, widened one hop (§4.2, "expanded 1 hop")
to their callers and covering tests through `territory.expand` when the
repository has a promoted graph; without one, the named files alone, and
`territory.expanded` says so.

THE CALLER'S OWN WORK IS LEFT OUT: its own task, and the live run whose
workflow (or CI fix round) it is a step of, with that run's pull request.
That run's steps are the caller's own plan, which `issueruns.territory_refusal`
already refused to let overlap in parallel.

`complete` is true only when every source was read in full: the pull
requests read, none unread or truncated, no undeclared lane or task, no list
cut at its cap. The worker's tool says UNKNOWN for what was not read.

INVARIANTS. Nothing here creates infrastructure demand (1), touches capacity
(2, 3) or the lease (5: a fenced call is refused, never released). Every read
is the attempt's own tenant's (9). The caller names paths only (10).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from swarm_common.states import TaskState

from . import territory as _territory
from .childkey import AttemptTuple
from .children import ChildService, ChildUnauthenticated, ChildUnproven, _lease_is_live, parse_body
from .errors import ApiError, ValidationFailed
from .gittools import REQUEST_ID, GitToolRefused, task_repository
from .issueruns import MAX_STEP_FILES, TERMINAL_RUN_STATES, IssueRuns
from .repositories import repo_id_for
from .store import LEASES, TASKS, Store
from .verdictpublish import spec_refusal

log = logging.getLogger(__name__)

MAX_BODY_BYTES = 16 * 1024
#: The territory is widened this far (§4.2: "expanded 1 hop").
EXPAND_DEPTH = 1
#: Running tasks read per call. More than this is `tasks_truncated`.
MAX_RUNNING_TASKS = 100

CONTEXT_FENCED = "child_submit_fenced"
UNPROVEN_MESSAGE = "this request is not proven for this attempt"
_GITHUB_REPOSITORY = re.compile(
    r"^(?:https://(?:www\.)?github\.com/|git@github\.com:)([^/\s]+)/([^/\s]+?)(?:\.git)?/?$"
)


# THE CODES ARE THE CHILD ROUTES' OWN. The attempt proof and the fence here are
# `ChildService`'s, so a caller is refused with the codes that already name
# them, on main and always enforced (tests/unit/control_plane/
# test_new_refusals_ship_off.py). A new code would have to ship behind a
# refusal switch, whose off position lets an unproven or fenced caller
# through -- the breach itself. The worker reads any 409 as the fence.


class ContextUnproven(ApiError):
    status_code = 403
    code = "child_submit_unproven"


class ContextFenced(ApiError):
    status_code = 409
    code = CONTEXT_FENCED


class ContextRefused(ValidationFailed):
    """A refusal the worker passes to the agent: 422 `validation_failed`, the
    reason in the message. NEVER 409, which the worker reads as the fence."""


class InFlightBody(BaseModel):
    # Strict: a `repository`, a `tenant` or a `token` in the body is a 422
    # (invariant 10). Exactly what `agent_worker.graphmcp.remote.InFlightRelay.call` sends.
    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(min_length=1, max_length=64)
    task_id: str = Field(min_length=1, max_length=128)
    attempt_id: str = Field(min_length=1, max_length=128)
    lease_id: str = Field(min_length=1, max_length=128)
    generation: StrictInt = Field(ge=1)
    request_id: str = Field(pattern=REQUEST_ID)
    files: list[str] = Field(min_length=1, max_length=MAX_STEP_FILES)

    @field_validator("files")
    @classmethod
    def _normalised(cls, files: list[str]) -> list[str]:
        return sorted(dict.fromkeys(_territory.normalise_entry(f) for f in files))


def repository_key(url: Any) -> str | None:
    """`owner/repo`, lower-cased, of a github.com URL; None for anything else."""
    matched = _GITHUB_REPOSITORY.match(url) if isinstance(url, str) else None
    if matched is None:
        return None
    return f"{matched.group(1)}/{matched.group(2)}".lower()


#: (tenant, registration-shaped record) -> `territory.open_pull_requests`'s answer.
PullReader = Callable[[Any, Mapping[str, Any]], dict[str, Any]]


@dataclass
class AttemptContext:
    """The route's logic. Built per request from the app context."""

    db: Any
    store: Store
    children: ChildService
    settings: Any
    pulls: PullReader
    expander: Callable[[str, str, list[str]], dict[str, Any] | None]
    now: Callable[[], Any]

    @classmethod
    def from_context(cls, ctx: Any, *, verifier: Any,
                     pulls: PullReader | None = None) -> "AttemptContext":
        children = ChildService(
            settings=ctx.settings, db=ctx.db, store=ctx.store, submissions=ctx.submissions,
            verifier=verifier, limiter=ctx.limiter, metrics=ctx.metrics, now=ctx.now,
        )

        def read_pulls(tenant: Any, record: Mapping[str, Any]) -> dict[str, Any]:
            return _territory.open_pull_requests(ctx, record, tenant, tenant)

        def expand(tenant_id: str, repo_id: str, files: list[str]) -> dict[str, Any] | None:
            return _expand(ctx, tenant_id, repo_id, files)

        return cls(db=ctx.db, store=ctx.store, children=children, settings=ctx.settings,
                   pulls=pulls or read_pulls, expander=expand, now=ctx.now)

    # -- the call ------------------------------------------------------------

    def handle(self, attempt_id: str, raw: bytes, *, authorization: str | None,
               proof: str | None, timestamp: str | None, path: str) -> dict[str, Any]:
        self.children._require_configured()
        if len(raw) > MAX_BODY_BYTES:
            raise ContextRefused(f"an in-flight request is at most {MAX_BODY_BYTES} bytes")
        body: InFlightBody = parse_body(InFlightBody, raw)
        attempt = AttemptTuple(body.tenant_id, body.task_id, attempt_id, body.lease_id,
                               body.generation)
        self._prove(body, attempt, raw=raw, authorization=authorization, proof=proof,
                    timestamp=timestamp, path=path)
        task = self._fence(attempt)
        unverified = spec_refusal(self.settings, task, attempt.task_id)
        if unverified:
            raise ContextRefused(f"spec_unverified: {unverified}")
        try:
            ref = task_repository(task)
        except GitToolRefused as refused:
            raise ContextRefused(f"no_repository: {refused.message}") from None
        answer = self.in_flight(attempt, task, ref.owner, ref.repo, body.files)
        # Counts only: a path, a title or a run's plan is the tenant's.
        log.info("in-flight context tenant=%s task=%s attempt=%s files=%d pulls=%d lanes=%d "
                 "tasks=%d complete=%s", attempt.tenant_id, attempt.task_id, attempt.attempt_id,
                 len(body.files), len(answer["pull_requests"]), len(answer["lanes"]),
                 len(answer["tasks_undeclared"]), answer["complete"])
        return answer

    def _prove(self, body: InFlightBody, attempt: AttemptTuple, *, raw: bytes,
               authorization: str | None, proof: str | None, timestamp: str | None,
               path: str) -> None:
        """The git tools' proof (`GitTools._prove`): one 403 for every cause."""
        try:
            self.children.authenticate_worker(authorization, body.tenant_id)
            if body.attempt_id != attempt.attempt_id:
                raise ChildUnproven("the body names another attempt")
            self.children.verify_proof(attempt, method="POST", path=path, body=raw,
                                       proof=proof, timestamp=timestamp)
        except (ChildUnauthenticated, ChildUnproven):
            raise ContextUnproven(UNPROVEN_MESSAGE) from None

    def _fence(self, attempt: AttemptTuple) -> dict[str, Any]:
        """The task, when this attempt still holds it; else 409 (invariant 5)."""
        task_snap = self.db.collection(TASKS).document(attempt.task_id).get()
        lease_snap = self.db.collection(LEASES).document(attempt.lease_id).get()
        task = task_snap.to_dict() if task_snap.exists else None
        lease = lease_snap.to_dict() if lease_snap.exists else None
        if (task is None or task.get("tenant_id") != attempt.tenant_id
                or task.get("state") != TaskState.RUNNING.value
                or task.get("cancel_requested")
                or not _lease_is_live(task, lease, attempt, self.now())):
            raise ContextFenced("this attempt is not RUNNING under its live lease; it was "
                                "superseded, and nothing was read")
        return task

    # -- the answer ----------------------------------------------------------

    def in_flight(self, attempt: AttemptTuple, task: Mapping[str, Any], owner: str, repo: str,
                  files: list[str]) -> dict[str, Any]:
        tenant_id = attempt.tenant_id
        repository = f"{owner}/{repo}"
        repo_id = repo_id_for(tenant_id, owner, repo)
        scope = _territory_of(self.expander, tenant_id, repo_id, files)
        paths, seams = scope["paths"], scope["seams"]

        lanes, lanes_more = _territory.live_lanes(self.db, tenant_id, owner, repo, now=self.now)
        runs = _live_runs(self.db, tenant_id, now=self.now)
        own = _own_run(runs, task, attempt.task_id)
        own_run = own.id if own is not None else None
        number = (own.pull_request or {}).get("number") if own is not None else None
        own_pull = number if isinstance(number, int) else None
        lanes = [lane for lane in lanes if lane["run_id"] != own_run]
        overlap = _territory.overlap_lanes(lanes, paths, seams)

        pulls_read: dict[str, Any]
        pull_rows: dict[str, Any] = {"pull_requests": [], "pull_requests_unread": [],
                                     "pull_requests_truncated": False}
        tenant = self.store.get_tenant(tenant_id)
        if tenant is None:
            pulls_read = {"ok": False, "code": "tenant_unregistered", "read_at": None}
        else:
            try:
                snapshot = self.pulls(tenant, {"repo_id": repo_id, "owner": owner, "repo": repo})
            except Exception as exc:  # noqa: BLE001 -- said in the answer, never a 500
                log.warning("in-flight context tenant=%s pulls unreadable (%s)", tenant_id,
                            type(exc).__name__)
                snapshot = {"ok": False, "code": getattr(exc, "code", None) or "forge_unreadable",
                            "read_at": None, "pull_requests": [],
                            "pull_requests_truncated": False}
            pulls_read = {"ok": bool(snapshot.get("ok")), "code": snapshot.get("code"),
                          "read_at": snapshot.get("read_at")}
            if pulls_read["ok"]:
                pull_rows = _territory.overlap_pull_requests(
                    snapshot, paths, seams, [own_pull] if own_pull is not None else [])

        covered: set[str] = set()
        for run in runs:
            covered |= _workflows(run)
        tasks, tasks_more = self._running_tasks(tenant_id, repository, attempt.task_id,
                                                covered=covered)
        complete = bool(
            pulls_read["ok"] and not pull_rows["pull_requests_unread"]
            and not pull_rows["pull_requests_truncated"]
            and not overlap["lanes_undeclared"] and not lanes_more
            and not tasks and not tasks_more
        )
        return {
            "repository": repository,
            "read_at": _iso(self.now()),
            "files": files,
            "territory": scope["record"],
            "pull_requests_read": pulls_read,
            **pull_rows,
            "lanes": overlap["lanes"],
            "lanes_undeclared": overlap["lanes_undeclared"],
            "lanes_truncated": lanes_more,
            "tasks_undeclared": tasks,
            "tasks_truncated": tasks_more,
            "excluded": {"run_id": own_run, "pull_request": own_pull},
            "complete": complete,
        }

    def _running_tasks(self, tenant_id: str, repository: str, own_task: str, *,
                       covered: set[str]) -> tuple[list[dict[str, Any]], bool]:
        """This tenant's RUNNING tasks on this repository that no live run covers.

        `covered`: every live run's workflows -- the caller's own run's, left
        out, and the others', already listed as their lanes."""
        page = self.store.list_tasks(tenant_id, state=TaskState.RUNNING,
                                     limit=MAX_RUNNING_TASKS, submitted_by=None)
        rows: list[dict[str, Any]] = []
        for item in page.items:
            if item.tenant_id != tenant_id or item.id == own_task:
                continue
            if repository_key(item.repository_url) != repository.lower():
                continue
            workflow = item.workflow_id
            if workflow and workflow in covered:
                continue
            rows.append({"task_id": item.id, "runner_profile": item.runner_profile,
                         "workflow_id": workflow, "step_id": item.step_id})
        rows.sort(key=lambda r: r["task_id"])
        return rows, page.next_page_token is not None


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _territory_of(expander: Callable[[str, str, list[str]], dict[str, Any] | None],
                  tenant_id: str, repo_id: str, files: list[str]) -> dict[str, Any]:
    """The paths the overlap is checked against, its seams, and what was done."""
    expanded: dict[str, Any] | None = None
    reason: str | None = None
    try:
        expanded = expander(tenant_id, repo_id, files)
        if expanded is None:
            reason = "no promoted graph for this repository: the named files only"
    except Exception as exc:  # noqa: BLE001 -- the named files still stand
        reason = f"the graph could not be read ({type(exc).__name__}): the named files only"
        log.info("in-flight context tenant=%s repo_id=%s graph skipped (%s)", tenant_id,
                 repo_id, type(exc).__name__)
    if expanded is None:
        expanded = {"named": files, "callers": [], "tests": [], "seams": []}
    paths = _territory.territory_paths(expanded)
    return {
        "paths": paths,
        "seams": {s["path"] for s in expanded.get("seams") or []},
        "record": {"expanded": reason is None, "depth": EXPAND_DEPTH, "paths": len(paths),
                   "reason": reason},
    }


def _expand(ctx: Any, tenant_id: str, repo_id: str, files: list[str]) -> dict[str, Any] | None:
    """`territory.expand` at depth 1 over the promoted graph, or None without one."""
    from .impact import ImpactService

    service = ImpactService.from_context(ctx)
    if service.index.registrations.find(tenant_id, repo_id) is None:
        return None
    _record, version, _fresh = service.version_and_freshness(tenant_id, repo_id)
    graph = service.open(tenant_id, repo_id, version)
    if graph is None:
        return None
    return _territory.expand(graph, files, depth=EXPAND_DEPTH, seams={})


def _live_runs(db: Any, tenant_id: str, *, now: Callable[[], Any]) -> list[Any]:
    runs, _more = IssueRuns(db, now=now).live(tenant_id, limit=_territory.MAX_LIVE_RUNS)
    return [r for r in runs if r.tenant_id == tenant_id and r.state not in TERMINAL_RUN_STATES]


def _workflows(run: Any) -> set[str]:
    out = {w for w in getattr(run, "ci_fix_workflows", None) or [] if w}
    if run.workflow_id:
        out.add(run.workflow_id)
    return out


def _own_run(runs: list[Any], task: Mapping[str, Any], task_id: str) -> Any:
    """The live run this task belongs to, or None: a step of its workflow or of
    one of its CI fix rounds, its planner, or the task its pull request is on."""
    workflow = task.get("workflow_id")
    for run in runs:
        if ((workflow and workflow in _workflows(run)) or run.planner_task_id == task_id
                or getattr(run, "pr_task_id", None) == task_id):
            return run
    return None


__all__ = [
    "CONTEXT_FENCED", "EXPAND_DEPTH", "AttemptContext", "ContextFenced",
    "ContextRefused", "ContextUnproven", "InFlightBody", "repository_key",
]
