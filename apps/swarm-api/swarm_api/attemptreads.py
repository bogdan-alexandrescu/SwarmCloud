"""An agent's reads of its own tenant's SwarmCloud state (docs/design/agent-autonomy.md
§3.4, §3.5's route; lane AU-4 STATE-READS, part of #1933).

WHY. A fix run has to read the failure it is fixing, and a failure-review
agent has to read the records of the failures it reviews: an agent's state,
its attempts, its events, its log tail, its artifacts, its workflow, its
issue run, its heartbeats and its Cloud Logging lines. The agent's container
holds no credential that reaches any of them, by design, so the worker beside
it relays each read (`agent_worker.statemcp`, the `swarm-state` MCP server) to
`GET /v1/attempts/{attempt_id}/reads/{tool}`, and this module answers.

EVERY CALL, IN THIS ORDER, BEFORE ANYTHING IS READ FOR IT:

1. the tool is one of `TOOLS`, and the query names the attempt's tuple and
   only the tool's own arguments (`ARGUMENTS`). Anything else -- a
   `tenant_id` of the agent's choosing for a tool, a `namespace`, a `filter`
   -- is a 422, so no argument can widen what is read;
2. the worker's ID token is the tuple's tenant's worker account, and the
   attempt proof (`swarm-state-req/v1`, the key `state/open` registered) is
   this attempt's (`AttemptState.call_get`: 401, 403);
3. the attempt's budget: `CALLS_PER_HOUR` per attempt (429);
4. ONE read of the task and the lease, and the fence (`fence_reason`, the
   state routes' own): a stale generation, a released lease, a task that left
   the slot-holding states -- `409 attempt_fenced` (invariant 5);
5. THE TENANT IS THE LEASE'S. Every read below is scoped to
   `lease.tenant_id`, never to a name in the request. A document of another
   tenant answers exactly as a missing one does (404 `not_found`, the same
   words), as every tenant-scoped route does: a different answer would
   confirm the id exists elsewhere (invariant 9).

READ-ONLY. No handler here writes a document, an object or a log. The one
state this module keeps is the in-process budget below. Cancelling,
retrying and answering stay with people and with the platform's own paths.

REDACTED. Every answer is masked by `JsonMasker` as a whole, with the read
task's literals (`task_input.masking_for`), on top of the log and artifact
readers' own read-time redaction (`InspectionService`). A log tail, an
error, an event's detail and a result summary are agent-written text: the
mask is applied whatever the worker's pass did (`swarm_api.redaction`).

`logs_query` (§3.5) reads Cloud Logging through ONE log view,
`WORKER_LOG_VIEW_ENV` (the view `swarm-workers` on `_Default`, lane AU-5,
IAM row L-1). Unset, it answers `503 logs_unavailable`, saying what is
missing. Its filter is built here from the lease's tenant and the agent's
own attempts' executions; the agent supplies an agent id, optionally one of
its attempt ids, a severity floor and a window of at most 24 hours inside
the agent's life, and nothing that reaches the filter as text.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from swarm_common.models import Attempt, Task
from swarm_common.states import TaskState

from .agent_output import AgentOutputService
from .attemptstate import AttemptFenced, AttemptState, fence_reason, jsonable
from .childkey import AttemptTuple
from .errors import ApiError, NotFound, ValidationFailed
from .issueruns import IssueRuns
from .ratelimit import TokenBucketLimiter
from .redaction import JsonMasker
from .store import LEASES, TASKS, WORKFLOWS
from .task_input import masking_for

# --------------------------------------------------------------------------
# The vocabulary. `agent_worker.statemcp.server` restates it for its fast
# answer; tests/unit/worker/test_state_mcp.py holds the two equal.
# --------------------------------------------------------------------------

#: The route's tuple: the worker's, signed with the path, never a tool's.
TUPLE_PARAMS = ("tenant_id", "task_id", "lease_id", "generation")

#: tool -> every argument it accepts. Nothing else reaches a handler.
ARGUMENTS: dict[str, frozenset[str]] = {
    "agents_list": frozenset({"state", "window_hours", "workflow_id", "run_id", "limit"}),
    "agent_view": frozenset({"agent_id"}),
    "agent_events": frozenset({"agent_id", "limit"}),
    "agent_log_tail": frozenset({"agent_id", "attempt_id", "stream"}),
    "agent_artifacts": frozenset({"agent_id", "name"}),
    "workflow_view": frozenset({"workflow_id"}),
    "run_view_sc": frozenset({"run_id"}),
    "heartbeats": frozenset({"agent_id"}),
    "logs_query": frozenset({"agent_id", "attempt_id", "severity", "since", "until"}),
}
REQUIRED: dict[str, frozenset[str]] = {
    "agents_list": frozenset(),
    "agent_view": frozenset({"agent_id"}),
    "agent_events": frozenset({"agent_id"}),
    "agent_log_tail": frozenset({"agent_id"}),
    "agent_artifacts": frozenset({"agent_id"}),
    "workflow_view": frozenset({"workflow_id"}),
    "run_view_sc": frozenset({"run_id"}),
    "heartbeats": frozenset({"agent_id"}),
    "logs_query": frozenset({"agent_id"}),
}
TOOLS = tuple(ARGUMENTS)

#: Per attempt, per hour (§3.4). In-process, as `ratelimit` says why: with N
#: instances the ceiling is N times this, and the worker's relay holds its
#: own copy of the same number before a request leaves.
CALLS_PER_HOUR = 120

#: §3.4's bounds.
MAX_WINDOW_HOURS = 7 * 24
DEFAULT_WINDOW_HOURS = 24
MAX_LIST = 50
DEFAULT_LIST = 20
MAX_EVENTS = 200
MAX_ATTEMPTS = 20
LOG_TAIL_BYTES = 64 * 1024
ARTIFACT_BYTES = 256 * 1024
ARTIFACT_LIST = 200
#: One free-text field (an error, a `last_error`), and a result summary's JSON.
TEXT_CHARS = 4000
SUMMARY_BYTES = 32 * 1024
STREAMS = ("stdout", "stderr", "agent_stdout", "agent_stderr")
DEFAULT_STREAM = "agent_stdout"

#: §3.5's bounds.
LOG_ENTRIES = 200
LOG_BYTES = 64 * 1024
LOG_WINDOW = timedelta(hours=24)
DEFAULT_LOG_WINDOW = timedelta(hours=1)
#: A log line can land a little before the task document's `created_at` (the
#: dispatch) and after its `completed_at` (the upload): the agent's life, padded.
LOG_LIFE_SLACK = timedelta(minutes=5)
SEVERITIES = ("DEFAULT", "DEBUG", "INFO", "NOTICE", "WARNING", "ERROR", "CRITICAL",
              "ALERT", "EMERGENCY")
#: The log view's full resource name, set on swarm-api by terraform once the
#: view exists (lane AU-5): `projects/<p>/locations/global/buckets/_Default/
#: views/swarm-workers`. Unset means no view, and `logs_query` says so.
WORKER_LOG_VIEW_ENV = "WORKER_LOG_VIEW"
_VIEW = re.compile(r"^projects/[a-z][a-z0-9-]{4,29}/locations/[a-z0-9-]+/buckets/[A-Za-z0-9_-]+"
                   r"/views/[A-Za-z0-9_-]+$")
#: The GKE namespace a tenant's agents run in (`scheduler.dispatch`'s default
#: `namespace_template`, restated: the scheduler is another image).
NAMESPACE_TEMPLATE = "swarm-tenant-{tenant}"
#: A name that goes into the filter as a literal. Anything else never does.
_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_ARTIFACT_NAME = re.compile(r"^[^\x00-\x1f\x7f]{1,512}$")


class LogsUnavailable(ApiError):
    """No log view is configured, or the configured name is not a view."""

    status_code = 503
    code = "logs_unavailable"


class LogsReadFailed(ApiError):
    """Cloud Logging did not answer. The status only: the body is Google's."""

    status_code = 502
    code = "logs_read_failed"


# --------------------------------------------------------------------------
# Checking a request's arguments: shape only, before any read
# --------------------------------------------------------------------------


def tool_arguments(tool: str, params: list[tuple[str, str]]) -> dict[str, str]:
    """The tool's arguments out of the query, or a 422/404.

    `params` is every query pair. The tuple's are dropped (the route parsed
    them); every other name must be the tool's own, once. So a `tenant_id`
    cannot be a tool argument, and `namespace=`, `filter=` or `resource=`
    are refused rather than ignored: an argument nobody reads is one the next
    change might start reading."""
    if tool not in ARGUMENTS:
        raise NotFound(f"no read tool {tool!r}; the tools are {', '.join(TOOLS)}")
    out: dict[str, str] = {}
    seen_tuple: set[str] = set()
    for name, value in params:
        if name in TUPLE_PARAMS:
            if name in seen_tuple:
                raise ValidationFailed(f"{name} is named twice")
            seen_tuple.add(name)
            continue
        if name not in ARGUMENTS[tool]:
            raise ValidationFailed(
                f"{tool} takes {sorted(ARGUMENTS[tool])}; {name!r} is not one of them")
        if name in out:
            raise ValidationFailed(f"{name} is named twice")
        out[name] = value
    missing = sorted(REQUIRED[tool] - set(out))
    if missing:
        raise ValidationFailed(f"{tool} needs {missing}")
    return out


def _id(args: Mapping[str, str], name: str) -> str:
    value = args[name]
    if not _ID.fullmatch(value):
        raise ValidationFailed(f"{name} must be an id")
    return value


def _optional_id(args: Mapping[str, str], name: str) -> str | None:
    return _id(args, name) if name in args else None


def _int(args: Mapping[str, str], name: str, *, default: int, low: int, high: int) -> int:
    if name not in args:
        return default
    raw = args[name]
    if not re.fullmatch(r"\d{1,6}", raw) or not low <= int(raw) <= high:
        raise ValidationFailed(f"{name} must be an integer from {low} to {high}")
    return int(raw)


def _instant(args: Mapping[str, str], name: str) -> datetime | None:
    if name not in args:
        return None
    try:
        value = datetime.fromisoformat(args[name].replace("Z", "+00:00"))
    except ValueError:
        raise ValidationFailed(f"{name} must be an ISO 8601 instant") from None
    if value.tzinfo is None:
        raise ValidationFailed(f"{name} must carry a time zone")
    return value.astimezone(timezone.utc)


def _text(value: Any, limit: int = TEXT_CHARS) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= limit else text[:limit] + f"... [{len(text) - limit} more chars]"


def _bounded_json(value: Any, limit: int = SUMMARY_BYTES) -> Any:
    """`value` when its JSON fits, else a note of its size and its head."""
    if value is None:
        return None
    raw = json.dumps(jsonable(value), sort_keys=True, default=str)
    if len(raw.encode("utf-8")) <= limit:
        return jsonable(value)
    return {"truncated": True, "bytes": len(raw.encode("utf-8")),
            "head": raw.encode("utf-8")[:limit].decode("utf-8", "ignore")}


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _value(enum_or_none: Any) -> Any:
    return getattr(enum_or_none, "value", enum_or_none)


# --------------------------------------------------------------------------
# The projections. FR-2's digest reads these shapes (failure-review.md §3).
# No `input`, no prompt: an agent reads what happened, not what was asked.
# --------------------------------------------------------------------------


def agent_row(task: Task) -> dict[str, Any]:
    """One row of `agents_list`."""
    return {
        "id": task.id,
        "state": _value(task.state),
        "runner_profile": task.runner_profile,
        "end_cause": _value(task.end_cause),
        "last_error": _text(task.last_error),
        "workflow_id": task.workflow_id,
        "step_id": task.step_id,
        "parent_task_id": task.parent_task_id,
        "attempt_count": task.attempt_count,
        "created_at": _iso(task.created_at),
        "completed_at": _iso(task.completed_at),
    }


def attempt_row(attempt: Attempt) -> dict[str, Any]:
    """One attempt in `agent_view`: what explains an end."""
    started = attempt.started_at or attempt.created_at
    ran = (None if attempt.completed_at is None or started is None
           else max(0.0, (attempt.completed_at - started).total_seconds()))
    return {
        "attempt_id": attempt.attempt_id,
        "generation": attempt.generation,
        "backend": attempt.backend,
        "created_at": _iso(attempt.created_at),
        "started_at": _iso(attempt.started_at),
        "completed_at": _iso(attempt.completed_at),
        "run_seconds": ran,
        "exit_code": attempt.exit_code,
        "error": _text(attempt.error),
        "peak_rss_bytes": attempt.peak_rss_bytes,
        "oom_near_miss": attempt.oom_near_miss,
        "checkpoints": len(attempt.checkpoints or []),
    }


def agent_detail(task: Task, attempts: list[Attempt]) -> dict[str, Any]:
    return {
        **agent_row(task),
        "updated_at": _iso(task.updated_at),
        "started_at": _iso(task.started_at),
        "resource_class": task.resource_class,
        "repository_url": task.repository_url,
        "repository_ref": task.repository_ref,
        "max_attempts": task.max_attempts,
        "current_generation": task.current_generation,
        "park_reason": _value(task.park_reason),
        "depends_on": list(task.depends_on or []),
        "cancel_requested": task.cancel_requested,
        "result_summary": _bounded_json(task.result_summary),
        "attempts": [attempt_row(a) for a in attempts],
    }


# --------------------------------------------------------------------------
# logs_query's filter: built here, from the lease's tenant and the agent's
# own executions. Nothing the caller sends reaches it as text.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Execution:
    backend: str
    name: str


def _quoted(value: str) -> str:
    # Only `_NAME`-shaped values arrive here; quoting is the second lock.
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def logs_filter(tenant_id: str, executions: list[Execution], *, severity: str,
                since: datetime, until: datetime) -> str:
    """The Logging query language filter for these executions of this tenant.

    A GKE execution is its Job's pods in the TENANT's namespace; a Cloud Run
    execution is its execution label on a `cloud_run_job` resource. The
    namespace is derived from `tenant_id` alone, so no execution name can
    point the filter at another namespace, and an execution of another
    tenant never reaches this function (its attempt is not the tenant's)."""
    if not _NAME.fullmatch(tenant_id):
        raise LogsUnavailable(f"tenant {tenant_id!r} has no log namespace name")
    if severity not in SEVERITIES:
        raise ValidationFailed(f"severity must be one of {', '.join(SEVERITIES)}")
    namespace = NAMESPACE_TEMPLATE.format(tenant=tenant_id)
    clauses = []
    for execution in executions:
        if not _NAME.fullmatch(execution.name):
            continue
        if execution.backend == "GKE_AUTOPILOT":
            clauses.append(
                f'(resource.type="k8s_container" AND resource.labels.namespace_name='
                f'{_quoted(namespace)} AND labels."k8s-pod/job-name"={_quoted(execution.name)})')
        else:
            clauses.append(
                f'(resource.type="cloud_run_job" AND labels."run.googleapis.com/execution_name"='
                f'{_quoted(execution.name)})')
    if not clauses:
        return ""
    return " AND ".join([
        "(" + " OR ".join(clauses) + ")",
        f"severity>={severity}",
        f'timestamp>="{since.astimezone(timezone.utc).isoformat()}"',
        f'timestamp<="{until.astimezone(timezone.utc).isoformat()}"',
    ])


class CloudLogsReader:
    """`entries.list` over ONE log view, as swarm-api's own account.

    Built lazily (no client at import or at `create_app`). Reads only: the
    account's one role here is `logging.viewAccessor` on the view (L-1)."""

    URL = "https://logging.googleapis.com/v2/entries:list"
    SCOPE = "https://www.googleapis.com/auth/logging.read"

    def __init__(self, session_factory: Callable[[], Any] | None = None) -> None:
        self._session_factory = session_factory
        self._session: Any = None

    def _authorized(self) -> Any:
        if self._session is None:
            if self._session_factory is not None:
                self._session = self._session_factory()
            else:
                import google.auth
                from google.auth.transport.requests import AuthorizedSession

                credentials, _ = google.auth.default(scopes=[self.SCOPE])
                self._session = AuthorizedSession(credentials)
        return self._session

    def entries(self, view: str, filter_text: str, *, limit: int) -> list[dict[str, Any]]:
        body = {"resourceNames": [view], "filter": filter_text,
                "orderBy": "timestamp desc", "pageSize": limit}
        try:
            response = self._authorized().post(self.URL, json=body, timeout=20)
        except Exception as exc:  # the transport's own error types vary
            raise LogsReadFailed(f"Cloud Logging did not answer ({type(exc).__name__})") from None
        if response.status_code != 200:
            raise LogsReadFailed(f"Cloud Logging answered {response.status_code}")
        payload = response.json() or {}
        rows = payload.get("entries") or []
        return [row for row in rows if isinstance(row, dict)][:limit]


def _entry_text(entry: Mapping[str, Any]) -> str:
    if isinstance(entry.get("textPayload"), str):
        return entry["textPayload"]
    payload = entry.get("jsonPayload")
    if isinstance(payload, dict):
        message = payload.get("message")
        if isinstance(message, str):
            return message
        return json.dumps(payload, sort_keys=True, default=str)
    return ""


def log_rows(entries: list[Mapping[str, Any]], *, max_bytes: int = LOG_BYTES) -> tuple[list[dict], bool]:
    """Entries as `{timestamp, severity, resource, text}`, newest first, within `max_bytes`."""
    rows: list[dict[str, Any]] = []
    spent = 0
    for entry in entries:
        resource = entry.get("resource") if isinstance(entry.get("resource"), dict) else {}
        row = {"timestamp": entry.get("timestamp"), "severity": entry.get("severity"),
               "resource": resource.get("type"), "text": _text(_entry_text(entry))}
        size = len(json.dumps(row, default=str).encode("utf-8"))
        if spent + size > max_bytes:
            return rows, True
        rows.append(row)
        spent += size
    return rows, False


# --------------------------------------------------------------------------
# The service
# --------------------------------------------------------------------------

#: The attempts' budget, shared by every request this process serves.
_BUDGET = TokenBucketLimiter(CALLS_PER_HOUR / 3600.0, CALLS_PER_HOUR)


class AttemptReads:
    """The read routes' logic. Built per request from the app context."""

    def __init__(
        self,
        *,
        ctx: Any,
        state: AttemptState,
        log_view: str | None,
        logs: CloudLogsReader | None = None,
        budget: TokenBucketLimiter | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._ctx = ctx
        self._state = state
        self._log_view = (log_view or "").strip() or None
        self._logs = logs or CloudLogsReader()
        self._budget = budget if budget is not None else _BUDGET
        self._now = now or ctx.now

    @classmethod
    def from_context(cls, ctx: Any, *, verifier: Any,
                     logs: CloudLogsReader | None = None) -> "AttemptReads":
        return cls(ctx=ctx, state=AttemptState.from_context(ctx, verifier=verifier),
                   log_view=os.environ.get(WORKER_LOG_VIEW_ENV, ""), logs=logs)

    # -- the gate ------------------------------------------------------------

    def read(self, tool: str, attempt_id: str, *, params: list[tuple[str, str]],
             tenant_id: str, task_id: str, lease_id: str, generation: int,
             authorization: str | None, proof: str | None, timestamp: str | None,
             signed_path: str) -> dict[str, Any]:
        args = tool_arguments(tool, params)
        attempt = self._state.call_get(
            attempt_id, tenant_id=tenant_id, task_id=task_id, lease_id=lease_id,
            generation=generation, authorization=authorization, proof=proof,
            timestamp=timestamp, signed_path=signed_path)
        self._budget.check(f"reads:{attempt.attempt_id}")
        tenant = self._tenant_of_lease(attempt)
        result, literals = getattr(self, f"_{tool}")(tenant, args)
        masked, count = JsonMasker(result, literals=literals).value(result)
        return {"ok": True, "tool": tool, "tenant_id": tenant, "result": masked,
                "redaction": {"applied_at_read_time": True, "count": count}}

    def _read_doc(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        snap = self._ctx.db.collection(collection).document(doc_id).get()
        return (snap.to_dict() or {}) if snap.exists else None

    def _tenant_of_lease(self, attempt: AttemptTuple) -> str:
        """The fence, then the lease's tenant: the one scope every read uses."""
        task = self._read_doc(TASKS, attempt.task_id)
        lease = self._read_doc(LEASES, attempt.lease_id)
        reason = fence_reason(task, lease, attempt)
        if reason is not None:
            raise AttemptFenced(f"this attempt no longer owns its task: {reason}")
        assert lease is not None
        tenant = lease.get("tenant_id")
        if not isinstance(tenant, str) or not tenant:
            raise AttemptFenced("this attempt no longer owns its task: the lease names no tenant")
        return tenant

    # -- reads ---------------------------------------------------------------

    def _task(self, tenant: str, agent_id: str) -> Task:
        # `Store.get_task`: a missing task and another tenant's are one answer.
        return self._ctx.store.get_task(tenant, agent_id, submitted_by=None)

    def _literals(self, task: Task) -> tuple[str, ...]:
        return tuple(masking_for(task).literals)

    def _agents_list(self, tenant: str, args: Mapping[str, str]) -> tuple[dict, tuple]:
        state: TaskState | None = None
        if "state" in args:
            try:
                state = TaskState(args["state"])
            except ValueError:
                raise ValidationFailed(
                    f"state must be one of {', '.join(s.value for s in TaskState)}") from None
        hours = _int(args, "window_hours", default=DEFAULT_WINDOW_HOURS, low=1,
                     high=MAX_WINDOW_HOURS)
        limit = _int(args, "limit", default=DEFAULT_LIST, low=1, high=MAX_LIST)
        workflow_id = _optional_id(args, "workflow_id")
        run_id = _optional_id(args, "run_id")
        if workflow_id and run_id:
            raise ValidationFailed("name workflow_id or run_id, not both")
        if run_id:
            run = IssueRuns(self._ctx.db, now=self._ctx.now).get(tenant, run_id)
            workflow_id = run.workflow_id
            if not workflow_id:
                return {"agents": [], "window_hours": hours, "more": False,
                        "run_id": run_id, "workflow_id": None}, ()
        page = self._ctx.store.list_tasks(tenant, state=state, workflow_id=workflow_id,
                                          limit=limit, submitted_by=None)
        cutoff = self._now() - timedelta(hours=hours)
        inside = [t for t in page.items if t.created_at is None or t.created_at >= cutoff]
        more = page.next_page_token is not None and len(inside) == len(page.items)
        return {"agents": [agent_row(t) for t in inside], "window_hours": hours,
                "more": more, "workflow_id": workflow_id}, ()

    def _agent_view(self, tenant: str, args: Mapping[str, str]) -> tuple[dict, tuple]:
        task = self._task(tenant, _id(args, "agent_id"))
        attempts = self._ctx.store.list_attempts(tenant, task.id, limit=MAX_ATTEMPTS)
        return {"agent": agent_detail(task, attempts)}, self._literals(task)

    def _agent_events(self, tenant: str, args: Mapping[str, str]) -> tuple[dict, tuple]:
        task = self._task(tenant, _id(args, "agent_id"))
        limit = _int(args, "limit", default=MAX_EVENTS, low=1, high=MAX_EVENTS)
        page = self._ctx.store.list_events(tenant, task.id, limit=limit, descending=True)
        events = [{"type": _value(e.type), "at": _iso(e.at),
                   "generation": e.generation,
                   "detail": _bounded_json(e.detail, 4096)}
                  for e in reversed(page.items)]
        return {"agent_id": task.id, "events": events,
                "more": page.next_page_token is not None}, self._literals(task)

    def _agent_log_tail(self, tenant: str, args: Mapping[str, str]) -> tuple[dict, tuple]:
        agent_id = _id(args, "agent_id")
        attempt_id = _optional_id(args, "attempt_id")
        stream = args.get("stream", DEFAULT_STREAM)
        if stream not in STREAMS:
            raise ValidationFailed(f"stream must be one of {', '.join(STREAMS)}")
        task = self._task(tenant, agent_id)

        def window(offset: int, length: int) -> dict[str, Any]:
            # `read_logs` redacts at read time and never cuts inside a token.
            return self._ctx.inspection.read_logs(
                tenant, task.id, submitted_by=None, attempt_id=attempt_id, stream=[stream],
                source="auto", offset=offset, limit_bytes=length)

        first = window(0, LOG_TAIL_BYTES)
        entry = (first.get("streams") or [{}])[0]
        total = entry.get("total_bytes")
        if entry.get("status") == "ok" and isinstance(total, int) and total > LOG_TAIL_BYTES:
            first = window(total - LOG_TAIL_BYTES, LOG_TAIL_BYTES)
            entry = (first.get("streams") or [{}])[0]
        return {
            "agent_id": task.id,
            "attempt_id": first.get("attempt_id"),
            "stream": stream,
            "status": entry.get("status"),
            "source": entry.get("source"),
            "total_bytes": entry.get("total_bytes"),
            "offset": entry.get("offset"),
            "from_start": not entry.get("offset"),
            "content": entry.get("content"),
            "redaction_count": entry.get("redaction_count"),
            "detail": entry.get("detail"),
        }, self._literals(task)

    def _agent_artifacts(self, tenant: str, args: Mapping[str, str]) -> tuple[dict, tuple]:
        task = self._task(tenant, _id(args, "agent_id"))
        service = AgentOutputService(self._ctx.inspection)
        if "name" not in args:
            listing = service.list_artifacts(tenant, task.id, limit=ARTIFACT_LIST,
                                             submitted_by=None)
            return {"agent_id": task.id, "artifacts": jsonable(listing)}, self._literals(task)
        name = args["name"]
        if not _ARTIFACT_NAME.fullmatch(name):
            raise ValidationFailed("name must be an artifact name from the listing")
        content = service.read_artifact(tenant, task.id, submitted_by=None, name=name,
                                        offset=0, limit_bytes=ARTIFACT_BYTES)
        return {"agent_id": task.id, "artifact": jsonable(content)}, self._literals(task)

    def _workflow_view(self, tenant: str, args: Mapping[str, str]) -> tuple[dict, tuple]:
        workflow_id = _id(args, "workflow_id")
        doc = self._read_doc(WORKFLOWS, workflow_id)
        if doc is None or doc.get("tenant_id") != tenant:
            # Checked here, before `Store.get_workflow`: its answer for another
            # tenant's workflow names that it exists (`workflow_not_in_tenant`).
            raise NotFound(f"workflow {workflow_id!r} not found")
        workflow = self._ctx.store.get_workflow(tenant, workflow_id, submitted_by=None)
        tasks = list(self._ctx.store.list_tasks(tenant, workflow_id=workflow_id, limit=200,
                                                submitted_by=None).items)
        held = {t.id for t in tasks}
        named = [s.task_id for s in workflow.steps if s.task_id and s.task_id not in held]
        if named:
            tasks += list(self._ctx.store.tasks_by_id(tenant, named).values())
        by_id = {t.id: t for t in tasks}
        result = self._ctx.rollups.for_workflow_from_tasks(workflow, tasks, complete=True)
        steps = []
        for step in workflow.steps:
            task = by_id.get(step.task_id or "")
            steps.append({
                "step_id": step.step_id,
                "runner_profile": step.runner_profile,
                "depends_on": list(step.depends_on or []),
                "task_id": step.task_id,
                "state": _value(task.state) if task else None,
                "end_cause": _value(task.end_cause) if task else None,
                "last_error": _text(task.last_error) if task else None,
            })
        return {"workflow": {
            "workflow_id": workflow.workflow_id,
            "state": _value(workflow.state),
            "created_at": _iso(workflow.created_at),
            "updated_at": _iso(workflow.updated_at),
            "on_step_failure": workflow.on_step_failure,
            "cancel_requested": workflow.cancel_requested,
            "steps": steps,
            "workflow_result": jsonable(result.to_api()),
        }}, ()

    def _run_view_sc(self, tenant: str, args: Mapping[str, str]) -> tuple[dict, tuple]:
        # `IssueRuns.get`, never `routes.runs.advance_run`: that one MOVES a
        # run, and every tool here is a read.
        run = IssueRuns(self._ctx.db, now=self._ctx.now).get(tenant, _id(args, "run_id"))
        return {"run": _bounded_json(run.to_api(), 4 * SUMMARY_BYTES)}, ()

    def _heartbeats(self, tenant: str, args: Mapping[str, str]) -> tuple[dict, tuple]:
        task = self._task(tenant, _id(args, "agent_id"))
        now = self._now()
        lease_row = None
        if task.current_lease_id:
            lease = self._read_doc(LEASES, task.current_lease_id)
            if lease is not None and lease.get("tenant_id") == tenant \
                    and lease.get("task_id") == task.id:
                beat = lease.get("heartbeat_at")
                silent = (None if not isinstance(beat, datetime)
                          else max(0, int((now - beat).total_seconds())))
                lease_row = {
                    "lease_id": task.current_lease_id,
                    "attempt_id": lease.get("attempt_id"),
                    "generation": lease.get("generation"),
                    "created_at": jsonable(lease.get("created_at")),
                    "heartbeat_at": jsonable(beat),
                    "expires_at": jsonable(lease.get("expires_at")),
                    "released_at": jsonable(lease.get("released_at")),
                    "silent_seconds": silent,
                }
        page = self._ctx.store.list_events(tenant, task.id, limit=MAX_EVENTS, descending=True)
        series = [{"at": _iso(e.at), "generation": e.generation,
                   "detail": _bounded_json(e.detail, 1024)}
                  for e in reversed(page.items) if _value(e.type) == "heartbeat"]
        return {"agent_id": task.id, "read_at": _iso(now), "lease": lease_row,
                "heartbeats": series}, self._literals(task)

    def _logs_query(self, tenant: str, args: Mapping[str, str]) -> tuple[dict, tuple]:
        if not self._log_view:
            raise LogsUnavailable(
                f"no log view is configured ({WORKER_LOG_VIEW_ENV} is unset on swarm-api): "
                "the view swarm-workers and its viewAccessor binding (IAM row L-1) are not "
                "applied yet; read agent_log_tail instead")
        if not _VIEW.fullmatch(self._log_view):
            raise LogsUnavailable(f"{WORKER_LOG_VIEW_ENV} does not name a log view")
        task = self._task(tenant, _id(args, "agent_id"))
        attempt_id = _optional_id(args, "attempt_id")
        severity = args.get("severity", "DEFAULT").upper()
        attempts = self._ctx.store.list_attempts(tenant, task.id, limit=MAX_ATTEMPTS)
        if attempt_id is not None:
            attempts = [a for a in attempts if a.attempt_id == attempt_id]
            if not attempts:
                raise NotFound(f"attempt {attempt_id!r} not found")
        now = self._now()
        born = (task.created_at or now) - LOG_LIFE_SLACK
        ended = (task.completed_at or now) + LOG_LIFE_SLACK
        until = _instant(args, "until") or min(now, ended)
        since = _instant(args, "since") or (until - DEFAULT_LOG_WINDOW)
        if since >= until:
            raise ValidationFailed("since must be before until")
        if until - since > LOG_WINDOW:
            raise ValidationFailed("the window is at most 24 hours")
        if since < born or until > ended:
            raise ValidationFailed("the window must lie inside this agent's life "
                                   f"({born.isoformat()} to {ended.isoformat()})")
        executions = [Execution(a.backend, a.execution_name) for a in attempts
                      if a.execution_name and a.tenant_id == tenant]
        filter_text = logs_filter(tenant, executions, severity=severity, since=since,
                                  until=until)
        base = {"agent_id": task.id, "since": since.isoformat(), "until": until.isoformat(),
                "severity": severity}
        if not filter_text:
            return {**base, "entries": [], "truncated": False,
                    "detail": "no attempt of this agent recorded an execution"}, ()
        entries = self._logs.entries(self._log_view, filter_text, limit=LOG_ENTRIES)
        rows, cut = log_rows(entries)
        return {**base, "entries": rows, "truncated": cut}, self._literals(task)

