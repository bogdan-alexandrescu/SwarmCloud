"""Task routes.

Read the tenant argument on every store call below. It comes from
`tenant_scope`, never from `auth.tenant_id` and never from a path, a query
string or a header.

WHY THE DEPENDENCY RATHER THAN `auth.tenant_id`. This docstring used to claim
that deriving the id from the verified token was the whole boundary -- "there is
no request a caller can construct that reads another tenant's task". That was
wrong, and the counterexample is two VERIFIED identities holding the SAME id:
the frozen `tenant_id_for_group` slugs the local part only, so `eng@saga.xyz`
and `eng@partner.com` both derive `eng`, and a registered group `u-eng@saga.xyz`
derives what the personal tenant of `eng@saga.xyz` derives. Submitting was
already refused by `ensure_tenant`; listing, reading and CANCELLING were not.
`tenant_scope` applies that same principal check before any store call here.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query, Response, status
from fastapi.responses import StreamingResponse

from swarm_common.states import TaskState

from ..agent_output import AgentOutputService
from ..auth import AuthContext
from ..codec import attempt_to_api, task_to_api
from ..deps import AppContext, current_auth, get_context, paged_limit, tenant_scope
from ..errors import ValidationFailed
from ..schemas import TaskBatchCreate, TaskCreate
from ..task_input import TaskMasking, input_copy, masking_for

router = APIRouter(prefix="/v1/tasks", tags=["tasks"])


def agent_output_service(ctx: AppContext = Depends(get_context)) -> AgentOutputService:
    """The Artifacts tab's reads, built around the one `InspectionService` the app holds.

    A dependency, as `routes.checkpoints.checkpoint_content` is, so a test can
    swap the window size through `app.dependency_overrides` -- FastAPI's own
    injection point -- instead of patching an import.
    """
    return AgentOutputService(ctx.inspection)


def _event_to_api(event, masking: TaskMasking) -> dict:
    """One event, its `detail` masked by the task's masker (the PR #229 review).

    A FAILED event's `detail.error` is the stderr tail `last_error` is, and a
    detail can quote the agent anywhere else; every string in it is masked by
    the rules and the literals the task's input named, string by string --
    never by key, because the keys are the platform's (`TaskMasking.leaves`).
    """
    detail, count = masking.leaves(event.detail)
    return {
        "event_id": event.event_id,
        "task_id": event.task_id,
        "type": event.type.value,
        "at": event.at,
        "attempt_id": event.attempt_id,
        "lease_id": event.lease_id,
        "generation": event.generation,
        "detail": detail,
        "detail_redaction_count": count,
    }


@router.post("", status_code=status.HTTP_201_CREATED)
def create_task(
    body: TaskCreate,
    response: Response,
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    result = ctx.submissions.submit_tasks(auth, [body])
    task = result.tasks[0]
    response.headers["Location"] = f"/v1/tasks/{task.id}"
    return {"task": task_to_api(task), "scheduler_woken": result.woke_scheduler}


@router.post("/batch", status_code=status.HTTP_201_CREATED)
def create_task_batch(
    body: TaskBatchCreate,
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    result = ctx.submissions.submit_tasks(auth, body.tasks)
    return {
        "tasks": [task_to_api(task) for task in result.tasks],
        "count": len(result.tasks),
        "scheduler_woken": result.woke_scheduler,
    }


@router.get("")
def list_tasks(
    state: str | None = Query(default=None),
    workflow_id: str | None = Query(default=None),
    runner_profile: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1),
    page_token: str | None = Query(default=None),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    parsed_state: TaskState | None = None
    if state is not None:
        try:
            parsed_state = TaskState(state)
        except ValueError:
            raise ValidationFailed(
                f"unknown state {state!r}",
                detail={"known_states": [s.value for s in TaskState]},
            ) from None
    page = ctx.store.list_tasks(
        tenant_id,
        state=parsed_state,
        workflow_id=workflow_id,
        runner_profile=runner_profile,
        limit=paged_limit(ctx, limit),
        page_token=page_token,
    )
    return {
        "tasks": [task_to_api(task) for task in page.items],
        "next_page_token": page.next_page_token,
        "tenant_id": tenant_id,
    }


@router.get("/{task_id}")
def get_task(
    task_id: str,
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    return {"task": task_to_api(ctx.store.get_task(tenant_id, task_id))}


@router.post("/{task_id}/cancel")
def cancel_task(
    task_id: str,
    # Both: the scope decides WHOSE task may be cancelled, `auth` records WHO
    # cancelled it. Cancelling is a write, and it was reachable across a tenant
    # id collision until this dependency existed.
    tenant_id: str = Depends(tenant_scope),
    auth: AuthContext = Depends(current_auth),
    ctx: AppContext = Depends(get_context),
) -> dict:
    task = ctx.store.request_cancel(tenant_id, task_id, by=auth.email)
    return {
        "task": task_to_api(task),
        # A task holding capacity stays in its state until the worker or the
        # reconciler releases the lease; decrementing the pool from here would
        # free a slot that a live container still occupies.
        "released_immediately": task.state.value == "CANCELLED",
    }


@router.get("/{task_id}/events")
def list_events(
    task_id: str,
    limit: int | None = Query(default=None, ge=1),
    page_token: str | None = Query(default=None),
    order: Literal["asc", "desc"] = Query(default="asc"),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """One page of a task's events, and the token for the next.

    Without a token this is what it always was -- the OLDEST `limit` events,
    ascending -- so the web UI and `swarm_mcp.follow`, which pass neither
    parameter, get byte-for-byte the rows they got before. What is new is that
    the rest of the history is reachable: `next_page_token` is non-null exactly
    when more events exist in the requested order, and `order=desc` puts the
    END of a run on the first page.

    A token is bound to this task and to the order it was minted for; sending
    it anywhere else is a 422 rather than a plausible wrong page. See
    `Store._keyset_page` for why events that share a timestamp are neither
    dropped nor repeated at a page boundary.

    Each `detail` is masked by the task's masker, so the task is read first --
    which `Store.list_events` does anyway for its tenant check.
    """
    masking = masking_for(ctx.store.get_task(tenant_id, task_id))
    page = ctx.store.list_events(
        tenant_id,
        task_id,
        limit=paged_limit(ctx, limit),
        page_token=page_token,
        descending=order == "desc",
    )
    return {
        "task_id": task_id,
        "events": [_event_to_api(e, masking) for e in page.items],
        "next_page_token": page.next_page_token,
    }


@router.get("/{task_id}/attempts")
def list_attempts(
    task_id: str,
    limit: int | None = Query(default=None, ge=1),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """Every attempt for one task, newest first.

    P4, and the thing that makes a retried task legible. `result_summary` is
    written once, by `finish()`, at terminal state -- so a task that failed
    twice and succeeded on the third attempt carries ONLY attempt three's
    numbers, and the first two attempts' exit codes, errors and peak RSS were
    unreachable through any API.

    Tenant-scoped like events and artifacts, not admin-gated: these are the
    caller's own attempts.

    Each row carries the attempt's CPU -- `cpu_seconds`, `peak_cpu_cores`,
    `mean_cpu_cores`, `cpu_limit_cores` -- as typed fields (contract request
    #15, accepted on #184, 2026-09-25). They replaced #188's opt-in
    `include=usage`, which read the task's events once per request to find the
    worker's newest HEARTBEAT reading. That parameter is no longer read: an
    older client that still sends it gets the same rows, with the figures in
    them.

    And when they were written (contract request #26, accepted on #184,
    2026-09-26): `cpu_measured_at`, the worker's clock at the reading;
    `cpu_limit_source`, `cgroup` or `resource_class`; and
    `cpu_reading_age_seconds`, the reading's age against this response's
    `read_at`. All three are null on an attempt from before the change.
    """
    # Resolve the task first so a wrong id is a 404 about the TASK rather than
    # an empty attempt list, which would read as "this task never ran". Its
    # masker masks each attempt's `error` (the PR #229 review).
    masking = masking_for(ctx.store.get_task(tenant_id, task_id))
    attempts = ctx.store.list_attempts(
        tenant_id, task_id, limit=paged_limit(ctx, limit)
    )
    read_at = ctx.now()
    return {
        "task_id": task_id,
        # The clock each row's `cpu_reading_age_seconds` is taken against.
        "read_at": read_at,
        "attempts": [attempt_to_api(a, masking=masking, read_at=read_at) for a in attempts],
    }


@router.get("/{task_id}/artifacts")
def list_artifacts(
    task_id: str,
    limit: int | None = Query(default=None, ge=1),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
    service: AgentOutputService = Depends(agent_output_service),
) -> dict:
    """What this attempt left in GCS, by reference.

    `complete` is the field that stops an empty list being read as an answer: it
    is false until the task reaches a terminal state and the worker writes its
    result summary, so "no artifacts yet" and "this task produced none" are
    distinguishable. `artifacts_skipped` names the files the worker dropped at
    the size cap, for the same reason.

    No download URL is minted here. The `uri` is a `gs://` reference, and the
    bytes are read through `/artifacts/content` and `/artifacts/raw`, which
    apply the tenant check and read-time redaction a signed URL would skip.

    Per entry (#184): `attempt_id` (the attempt its stored uri lies under),
    `kind` and `content_type` from the one name table, and `role` --
    `agent_stdout`, `agent_stderr` or `agent_transcript` for the agent's own
    streams, from `result_summary.agent_streams` (or the runner's naming
    convention for an attempt made before it existed). Top level: the
    manifest's `attempt_id`. No object is read.
    """
    return service.list_artifacts(tenant_id, task_id, limit=paged_limit(ctx, limit))


@router.get("/{task_id}/artifacts/content")
def read_artifact(
    task_id: str,
    name: str = Query(..., min_length=1, max_length=512),
    offset: int = Query(default=0, ge=0),
    limit_bytes: int | None = Query(default=None, ge=1),
    tenant_id: str = Depends(tenant_scope),
    service: AgentOutputService = Depends(agent_output_service),
) -> dict:
    """ONE artifact's content, resolved by the server from the task's manifest.

    THE PARAMETER IS A NAME AND NOT A PATH, and that is deliberate to the point
    of being the reason this route is shaped the way it is. A route that
    accepted a GCS path, or a key, or even a "relative" one, would be a
    path-traversal hole into another tenant's prefix the moment any segment
    check was missed -- and the only thing standing between it and invariant 9
    would be the completeness of a sanitiser. Here `name` is matched for exact
    equality against the entries `AgentLifecycle._upload_outputs` wrote into
    THIS task's `result_summary`, and the object key is rebuilt from the task
    document's own tenant and id. A caller who sends a path gets a 404 about an
    artifact this task does not list, because that is what it is.

    The tenant boundary is the SAME one the listing route uses and is not
    re-derived: `tenant_scope` decides the scope, `Store.get_task` 404s a task
    belonging to anyone else with the message a missing task gets, and only
    then does a key exist.

    BOUNDED AND REDACTED. `limit_bytes` is clamped into the service's range and
    every response carries `truncated` and `next_offset`; a cut window also
    carries a `detail` saying so in words, because a reader who takes a
    truncated transcript for the whole output has been misled by the response
    rather than by the data. Content is run through `swarm_api.redaction` on
    the way out whatever happened at write time -- see `read_artifact`'s
    docstring for why the worker's pass is not a guarantee this route may lean
    on. An artifact that is not text is reported as `binary` with no bytes, not
    base64-encoded: bytes nothing can scan are bytes this route does not serve
    -- `/artifacts/raw` is the route for bytes, and says what it did to them.

    `kind` and `content_type` (#184) are the listing's, from the same name
    table; the NUL sniff above stays the authority on whether it is text.
    """
    return service.read_artifact(
        tenant_id,
        task_id,
        name=name,
        offset=offset,
        limit_bytes=limit_bytes,
    )


@router.get("/{task_id}/artifacts/raw", response_class=StreamingResponse)
def read_artifact_raw(
    task_id: str,
    name: str = Query(..., min_length=1, max_length=512),
    disposition: str | None = Query(default=None),
    tenant_id: str = Depends(tenant_scope),
    service: AgentOutputService = Depends(agent_output_service),
) -> StreamingResponse:
    """ONE artifact's BYTES: for an `<img>`, an "open full", and a download (#184).

    THROUGH THE API, NEVER A SIGNED URL -- the owner's decision, so the tenant
    check and read-time redaction apply to every byte. Resolved exactly as
    `/artifacts/content` resolves a name (a NAME from this task's manifest,
    never a path), and classified from the object's first 4096 bytes:

      * an image -- an image extension AND matching magic bytes -- is served as
        stored, `image/png|jpeg|gif|webp`, `X-Swarm-Redaction: not-applied`;
      * text -- no NUL -- is ALWAYS `text/plain; charset=utf-8`, never HTML or
        JSON, so nothing an agent wrote renders here; redacted window by
        window, `X-Swarm-Redaction: applied`;
      * anything else is `application/octet-stream`, an attachment whatever
        `disposition` asked, not redacted, and says so.

    `disposition` is `inline` or `attachment` (the default). Every error --
    404 not listed, 410 listed but reclaimed, 503 unreadable, 422 a bad
    disposition -- is the JSON envelope, sent before any byte. Chunked, with
    `X-Artifact-Bytes` carrying the stored size instead of a Content-Length.
    Staged inputs are read the same way, from the upstream task.
    """
    raw = service.raw_artifact(tenant_id, task_id, name=name, disposition=disposition)
    return StreamingResponse(raw.chunks, media_type=raw.media_type, headers=raw.headers)


@router.get("/{task_id}/checkpoints")
def list_checkpoints(
    task_id: str,
    attempt_id: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1),
    page_token: str | None = Query(default=None),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """Every checkpoint this task has written, across every attempt.

    READ-ONLY, and it has to be: a checkpoint is the entire value of an attempt
    that was parked or killed, and the only component allowed to remove one is
    `reconciler.checkpoints`, which decides by reference rather than by age.
    Nothing on this path writes, deletes or mutates an object or a document.

    TENANT SCOPE. `tenant_scope` -- not `auth.tenant_id` -- then
    `Store.get_task`, which 404s a task belonging to anyone else with the same
    message a missing task gets. Only after both does a prefix exist, and it is
    built from the task document's own tenant and id rather than from anything
    in the request. `attempt_id` can only NARROW that prefix and is validated as
    a single path segment first, so there is no value of it that reaches
    another tenant's objects.

    WHY ACROSS ATTEMPTS. A resume is by definition a new attempt, and
    `CheckpointManager.find_latest` scans the whole task prefix to pick what to
    restore from -- so the checkpoint that matters after a crash was written by
    the attempt that died. Listing only the current attempt would hide it.

    THE THREE ANSWERS. A failed LISTING is a 503, never `{"checkpoints": []}`
    with a 200. A checkpoint whose manifest is missing is reported `absent` and
    `resumable: false` -- the manifest is the commit marker, so there is nothing
    to resume from. A checkpoint whose manifest could not be READ is reported
    `unreadable` with `resumable: null`, because "cannot resume" and "cannot
    tell" are different facts.
    """
    return ctx.inspection.list_checkpoints(
        tenant_id,
        task_id,
        attempt_id=attempt_id,
        limit=paged_limit(ctx, limit),
        page_token=page_token,
    )


@router.get("/{task_id}/logs")
def read_logs(
    task_id: str,
    attempt_id: str | None = Query(default=None),
    # Repeatable (#184): `?stream=agent_stderr&stream=stdout`. None serves the
    # runner's two streams, exactly as before.
    stream: list[str] | None = Query(default=None),
    source: str = Query(default="auto"),
    offset: int = Query(default=0, ge=0),
    limit_bytes: int | None = Query(default=None, ge=1),
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """An attempt's captured stdout and stderr, redacted at read time.

    THE REDACTION IS NOT BELT-AND-BRACES, IT IS THE BRACES. The worker's own
    scrubbing pass replaces REGISTERED LITERAL VALUES -- the provider key this
    platform resolved out of Secret Manager -- and matches no patterns at all;
    both `_redact_before_upload` and `_publish_live_logs` skip the pass
    entirely when no secret was registered, which is every `mock`-profile run.
    So a token the agent minted, an `Authorization:` header a tool echoed, or
    an `.env` printed out of a cloned repository reaches the bucket in the
    clear. Everything served here is therefore run through
    `swarm_api.redaction` on the way out, unconditionally, and the response
    says so in `redaction.applied_at_read_time`.

    The same filter is applied to any upstream error string this route reports,
    because a storage client's exception can quote the request that failed and
    a signed URL is a credential with an expiry.

    Nothing in this route reads, logs or echoes a request header. The
    `Authorization` header is consumed by `deps.current_auth` and by nothing
    else in the process.

    TWO OBJECTS PER STREAM. `logs/<stream>.log` is the complete record, written
    once when the attempt finishes; `logs/live/<stream>.tail.log` is a bounded
    window republished every few seconds while it runs. `source=auto` (the
    default) prefers the record and falls back to the tail when the record is
    ABSENT -- never when it is unreadable, because serving a different object
    in place of a failed read reports success over the wrong window.

    Each stream reports `ok`, `absent` or `unreadable` independently, and
    `content` is null rather than `""` in the latter two, so a client that
    renders content without reading status shows nothing instead of an empty
    log that looks like a silent agent.

    WHOSE STREAMS (#184). `stdout` and `stderr` are the RUNNER process's --
    its own JSON log lines -- which is what the drawer's "Output, as the agent
    wrote it" panel showed under the wrong name. `agent_stdout` and
    `agent_stderr` are the agent CLI's own; `stream` repeats to ask for
    several. For a runner with no agent CLI they are `not_applicable`. The
    response carries `read_at`, and every stream its object's
    `object_updated_at` and `age_seconds`, so a live read says how old it is.
    """
    return ctx.inspection.read_logs(
        tenant_id,
        task_id,
        attempt_id=attempt_id,
        stream=stream,
        source=source,
        offset=offset,
        limit_bytes=limit_bytes,
    )


@router.get("/{task_id}/transcript")
def read_transcript(
    task_id: str,
    attempt_id: str | None = Query(default=None),
    source: str = Query(default="auto"),
    offset: int = Query(default=0, ge=0),
    limit_bytes: int | None = Query(default=None, ge=1),
    include_raw: bool = Query(default=False),
    tenant_id: str = Depends(tenant_scope),
    service: AgentOutputService = Depends(agent_output_service),
) -> dict:
    """The agent's own stdout, parsed into steps: text, thinking, tool calls, results (#184).

    The same object `/logs?stream=agent_stdout` would serve -- the final copy,
    the live tail while it runs, or the runner's artifact for an attempt made
    before the worker published agent streams -- parsed ON THE SERVER, every
    string redacted after JSON decoding and capped at 16 KiB. Windows cut on
    newlines only. `format` is sniffed (`claude-stream-json`, `claude-json`,
    `ndjson`, `text`); for `text`, `steps` is null and the raw `/logs` window
    is the view. `include_raw=true` adds each event's own record, redacted.
    Absent and unreadable are 200s with `stream.status`.
    """
    return service.read_transcript(
        tenant_id,
        task_id,
        attempt_id=attempt_id,
        source=source,
        offset=offset,
        limit_bytes=limit_bytes,
        include_raw=include_raw,
    )


@router.get("/{task_id}/answer")
def read_answer(
    task_id: str,
    attempt_id: str | None = Query(default=None),
    tenant_id: str = Depends(tenant_scope),
    service: AgentOutputService = Depends(agent_output_service),
) -> dict:
    """The agent's final answer, as Markdown, redacted (#184).

    The last `result` event's `result` in the agent's stdout -- never
    `result_summary.runner.summary`, which the runner cuts at 2,000 characters,
    unless there is no result event, and then marked as the cut summary it may
    be. `status` is `ok`, `not_yet` (still running), `absent` (ended with
    neither) or `unreadable` (a read failed; nothing further down is tried).
    """
    return service.read_answer(tenant_id, task_id, attempt_id=attempt_id)


@router.get("/{task_id}/input")
def read_input(
    task_id: str,
    tenant_id: str = Depends(tenant_scope),
    ctx: AppContext = Depends(get_context),
) -> dict:
    """The task's input as a screen may draw it: masked at read time, counted (#184).

    The prompt, the rest of the input and the whole input, each as
    `redaction.redact` returns it with its own `redaction_count` -- the same
    redactor, and the same count, every other output this API serves carries
    -- and the task's metadata as `metadata`, from the same masker, with its
    own count. `GET /v1/tasks/{id}` serves the same masking of the input and
    the metadata as objects (owner decision, 2026-09-26: masked everywhere).
    See `swarm_api.task_input`.
    """
    task = ctx.store.get_task(tenant_id, task_id)
    return input_copy(task, read_at=ctx.now())
