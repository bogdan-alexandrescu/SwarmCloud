"""The wire shapes swarm-api documents in its OpenAPI document (part of #72).

`task_to_api` (codec.py) builds a task's JSON as a plain dict, so FastAPI had
nothing to describe it with, and the console copied the shape by hand into
`apps/swarm-ui/src/types.ts` with no check that noticed a change. `Task` below
is that shape as a model. It is attached to `GET /v1/tasks/{task_id}` as a
DOCUMENTED response only (`responses=`, never `response_model=`), so nothing
here validates, filters or reorders what the route serves: the dict
`task_to_api` builds is still the payload, byte for byte.

`scripts/gen-api-types.sh` turns the OpenAPI document into
`apps/swarm-ui/src/generated/api-types.ts`, and the console's `Task` extends
the generated one. Two tests keep the three in step: one holds this model
to what the routes serve in both directions (every served key declared,
every required field served), and a drift test fails when the
committed TypeScript differs from what the script writes now.

REQUIRED here means what every route that serves a task sends, the summary
listing (`view=summary`) included; everything else has a default and is
optional in the generated type. (The console's `Task` still narrows `input` and
`result_summary`, which the summary listing omits, to required, as it always
has: its screens read them from the detail.) A field a route sends that this model does not declare fails
the first of those tests, so a new field is added here, the types regenerated, and
the console reads it from the generated type.

Nested shapes the console still types by hand (`blocked_by`, `waiting`,
`dispatch`, ...) are `Any` here, `unknown` in the generated type, and narrowed
by `types.ts`'s `Task`; describing them is a later slice of #72.

THE REST OF THE CONSOLE'S CORE SHAPES (below `Task`): the pools, runner
profiles and their admission, capacity, the task page, attempts, leases and
heartbeats, quota, tenants, stats, `me`, the dispatch switch, the workflow,
providers and events -- every shape `test_ui_api_field_contract.py` once held
to `types.ts` by regex. Same conventions, same documented-only attachment
(each route's `responses=`), and `tests/unit/control_plane/test_wire_types.py`
holds each to every route that serves it, both directions, and shows the
codec-built rows are served unchanged. Each console interface in `types.ts`
extends its generated shape and keeps only its own narrowing.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from swarm_common.states import TaskState


class TaskLinks(BaseModel):
    """`links` on a task: its console page, for surfaces OUTSIDE the console."""

    console: str | None = Field(description=(
        "This agent's page on the deployment's console, the same in every state; null when the "
        "deployment has no console (`agent_console_url`)."
    ))


class Task(BaseModel):
    """`task_to_api` in codec.py, plus what the task routes add beside it
    (`submitted_by_name`, the attempt totals, `contract_acceptance`)."""

    model_config = ConfigDict(title="Task")

    id: str
    tenant_id: str
    state: TaskState
    runner_profile: str
    resource_class: str
    provider: str | None
    priority: int
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    submitted_by: str | None
    submitted_by_name: str | None = Field(None, description=(
        "The submitter's display name (`people.add_names`); null when none is set."
    ))
    attempt_count: int
    max_attempts: int
    platform_refusals: int = Field(None, description=(
        "Contract request 123: consecutive dispatches the cluster refused (Job quota or an "
        "admission policy). Each parks PLATFORM_REFUSED without spending an attempt; a dispatch "
        "that succeeds resets it to 0."
    ))
    park_reason: str | None
    blocked_by: list[Any] | None
    workflow_id: str | None
    step_id: str | None
    depends_on: list[str] | None
    parent_task_id: str | None = Field(None, description=(
        "Contract request 14 (docs/design/child-tasks.md §6.3). The task whose agent submitted "
        "this one, and that parent's attempt; set by swarm-api from the submitting attempt, null "
        "for everything else. OPTIONAL: an API older than the change does not send them. Not "
        "`depends_on`: a parent can cancel its children and awaits them, a dependency does "
        "neither."
    ))
    parent_attempt_id: str | None = None
    forge_credential: str | None = Field(None, description=(
        "archived issue 780 OB7: the slot the task's GitHub token is read from, by name."
    ))
    forge_access: Literal["read", "write"] | None = Field(None, description=(
        "`read` or `write`; null when the task names no GitHub credential."
    ))
    forge_credential_source: str | None = Field(None, description=(
        "\"the submitter's GitHub credential\", \"tenant token, service submission\", \"owner's GitHub "
        "connection, service submission\" (INDEX-CRED), or null."
    ))
    additional_repositories: list[Any] | None = Field(None, description=(
        "Contract request 95 (docs/design/multi-repository-steps.md §4.1): the repositories this "
        "task reads beside its own, each pinned to a commit. Null when it reads none. OPTIONAL: "
        "an older API does not send it."
    ))
    cancel_requested: bool
    repository_url: str | None
    model: str | None
    model_class: str | None = Field(None, description=(
        "The model class the task runs (contract request 104, archived issue 1435), RESOLVED: the "
        "stored class, or its profile's default when none was stored, which "
        "`model_class_defaulted` then says. Null on a profile that runs no Claude model. "
        "OPTIONAL: an older API does not send it."
    ))
    model_class_defaulted: bool = None
    timeout_seconds: int | None
    timeout_seconds_asked: int | None = Field(None, description=(
        "Contract request 131: the `timeout_seconds` the spec asked for when the API stored a "
        "lower one (the profile's cap, or a child's parent's), else null. A record only: the "
        "limit is `timeout_seconds`. OPTIONAL: an older API does not send it."
    ))
    max_turns: int | None = Field(None, description=(
        "Contract request 133: the task's resolved turn cap, which the worker passes to the agent "
        "CLI, or null for none. OPTIONAL: an older API does not send it."
    ))
    next_eligible_at: datetime | None = Field(description=(
        "When a PARKED task becomes eligible again. Null unless it is parked."
    ))
    metadata: dict[str, Any] | None = Field(description=(
        "MASKED, like `input` (owner decision, 2026-09-26: masked everywhere). The caller's keys "
        "come through the API's masker; the platform's own (`dispatch`, `input_from`, "
        "`expected_outputs`, which submission refuses from callers) as stored. "
        "`metadata_redaction_count` is how many masks it took."
    ))
    description: str | None = Field(None, description=(
        "A single task's own description (archived issue 2688): plain text of at most 600 "
        "characters, masked as its metadata is. Null for a workflow's step, which inherits "
        "nothing; OPTIONAL, as an API older than the field sends none."
    ))
    repository_ref: str | None
    input: Any = Field(None, description=(
        "Absent from a `view=summary` listing row (`codec.SUMMARY_DROPPED_KEYS`). "
        "THE MASKED INPUT. `GET /v1/tasks/{id}` serves the input the API masked at read time, "
        "never the input as submitted -- even to the tenant that submitted it (owner decision, "
        "2026-09-26). Screens still draw the `/input` copy, which carries the prompt and the rest "
        "as blocks."
    ))
    input_redaction_count: int | None = Field(None, description=(
        "How many credential-shaped strings the API masked in `input` and in `metadata`. "
        "OPTIONAL: an API older than the change sends neither, and serves both unmasked, so "
        "absent is not zero."
    ))
    metadata_redaction_count: int | None = None
    last_error: str | None = Field(description=(
        "MASKED too, since the archived pull request 229 review: the agent's stderr tail and its "
        "own summary text, by the input's masker, string by string (an artifact's `name` and "
        "`uri` as stored). Each count is how many masks that took; optional for the reason the "
        "two above are."
    ))
    last_error_redaction_count: int | None = None
    result_summary: dict[str, Any] | None = Field(None, description=(
        "The agent's own summary, masked string by string. Absent from a `view=summary` "
        "listing row (`codec.SUMMARY_DROPPED_KEYS`)."
    ))
    result_summary_redaction_count: int | None = None
    repository_url_redaction_count: int | None = Field(None, description=(
        "Userinfo that could carry a credential is masked whole; submission refuses it now."
    ))
    latest_checkpoint: str | None
    human_wait: Any = Field(None, description=(
        "Contract request 70 (LB-C), docs/design/live-browser.md §4.1: set while a RUNNING "
        "browser task holds its pod for a person, null otherwise, and cleared when the hold "
        "parks. OPTIONAL: an API older than LB-L12 does not send it. Read through `handoffOf` "
        "(Handoff.tsx), never raw."
    ))
    start_problem: Any = Field(None, description=(
        "F1-POD: why the current attempt's GKE pod is not starting, as the reconciler last read "
        "it (`reconciler.startproblem`); null when nothing is recorded. Kept on a cancelled task, "
        "which is how `elapsed()` tells \"started, did not reach running\" from \"never ran\". "
        "OPTIONAL: an older API does not send it. Drawn by `PodStartProblem`."
    ))
    owner_answers: list[Any] = Field(None, description=(
        "TASK-ANSWERS (part of archived issue 1206): the owner's answers to the agent's "
        "questions, in question order, lifted by the API out of `metadata.owner_answers`; `[]` "
        "when none is answered. OPTIONAL: an older API does not send it. The card reads them from "
        "`GET /questions` instead, beside each question."
    ))
    end_cause: str | None = Field(None, description=(
        "Contract request 23 (archived pull request 217). Written by every terminal writer beside "
        "`completed_at`. Null on SUCCEEDED and on a task that ended before this field existed. "
        "One of `END_CAUSES` from this build's contract; typed as a string because a newer API "
        "may send a value this build does not know."
    ))
    waiting_for: Any = Field(None, description=(
        "archived issue 362. Which pool refuses this READY task NOW, read live on the GET by "
        "`swarm_api/waiting.py` from the task's own pools. Null for every other state; OPTIONAL "
        "because an older API does not send it. `blocked_by` is the scheduler's record from its "
        "LAST pass; this is the current reading."
    ))
    waiting: Any = Field(None, description=(
        "archived issue 1782 (WAIT-API). Why a PARKED, READY or QUEUED task waits, built by "
        "`swarm_api/waiting_detail.py` from what the platform recorded -- never read from GitHub "
        "or a provider. `GET /v1/tasks/{id}` serves it in full; a listing row serves "
        "`WaitingRow`. Null for every other state; OPTIONAL because an older API does not send "
        "it."
    ))
    account: Any = Field(None, description=(
        "archived issue 379. The subscription account the LATEST attempt runs on, derived by "
        "`swarm_api/task_accounts.py` from this task's own account events. OPTIONAL and nullable: "
        "an older API does not send it, and a route that did not read it sends null. Read it "
        "through `accountText`, never raw."
    ))
    heartbeat_at: datetime | None = Field(None, description=(
        "archived issue 179. The worker's last beat, from the task's current LEASE (the worker "
        "never writes it to the task), read by `swarm_api/heartbeats.py` on `GET /v1/tasks` and "
        "`GET /v1/tasks/{id}` for LEASED, DISPATCHED, STARTING and RUNNING. Null when the lease "
        "has NEVER beaten -- never the lease's creation time -- and null for every other state. "
        "OPTIONAL because an older API does not send it. Read it through `silentWorkerLine` "
        "(Agents.tsx), and only when `heartbeat` is `'read'`."
    ))
    heartbeat_grace_seconds: int | None = Field(None, description=(
        "The reconciler's grace, resolved server-side; null where `heartbeat` is."
    ))
    heartbeat: str | None = Field(None, description=(
        "What the reading is: `'read'` (the lease was read), `'not read'` (the read failed -- a "
        "null `heartbeat_at` then means unknown, never silent), `'no lease'`, or null for a task "
        "that holds no lease."
    ))
    browser_used: bool | None = Field(None, description=(
        "WHETHER A BROWSER RUNTIME USED ITS BROWSER (BROWSER-USE-FLAG, archived issue 2322), the "
        "worker's verdict lifted out of `result_summary` by `codec.browser_use_to_api`, on the "
        "list rows too. True or false only on a browser runtime that ended with its counts read; "
        "NULL IS NOT FALSE: it is a runtime with no browser, an agent not ended, one that "
        "predates the record, or counts the worker could not read "
        "(`browser_use.unknown_because`). Read it through `browserUseOf` (BrowserUse.tsx). "
        "OPTIONAL: an older API does not send it."
    ))
    browser_use: Any = Field(None, description=(
        "Its counts and seconds; null when the worker recorded none."
    ))
    attempts: int | None = Field(None, description=(
        "EVERY ATTEMPT'S COST AND TIME (lane review P1, 2026-10-05), from "
        "`swarm_api/attempt_totals.py` on `GET /v1/tasks/{id}` and the workflow read. "
        "`result_summary` and `started_at` describe only the LAST attempt; these sum all of them. "
        "OPTIONAL because an older API and the list routes do not send them. `cost_usd_total` "
        "null is NOT MEASURED, never $0; `cost_incomplete` true makes it a floor (\"at least\"). "
        "`attempts_read: 'failed'` means the attempts were not read and every figure is null."
    ))
    attempts_with_cost: int | None = None
    cost_usd_total: float | None = None
    cost_incomplete: bool | None = None
    duration_s_total: float | None = None
    duration_incomplete: bool | None = None
    first_started_at: datetime | None = None
    last_attempt_cost_usd: float | None = None
    last_attempt_duration_s: float | None = None
    attempts_read: Literal["ok", "failed"] = None
    current_generation: int = Field(description=(
        "THE FENCING GENERATION. `models.py:177`, now served by `task_to_api`. It increments on "
        "admission (`admission.py:186`) AND on a reconciler fence "
        "(`reconciler/store.py:220-227`), so `current_generation > attempt_count` is exactly \"a "
        "stale worker was fenced out\" -- the only cheap signal that CONTRACT invariant 5 has "
        "fired. `AttemptRow.generation` is a different and narrower thing: the generation ONE "
        "attempt was minted with. NOT OPTIONAL and 0 IS AN ANSWER: the API sends it on every "
        "task, and zero means never admitted. Read `> attempt_count` rather than truthiness."
    ))
    current_lease_id: str | None = Field(description=(
        "The lease holding this task's capacity, or null if it holds none. Null on a QUEUED, "
        "PARKED, READY or terminal task -- those cost nothing (invariant 1). It is the join key "
        "onto `LeaseRow`, and comparing that row's `generation` against `current_generation` "
        "above is how a stalled task is diagnosed: a task at generation 2 whose named lease is "
        "still at generation 1 and unreleased is holding a slot for work that will never run. "
        "That is the twenty-minute DISPATCHED stall on task_b5dc2568713a40158851, which no screen "
        "could explain because neither number reached the browser."
    ))
    dispatch: Any = Field(None, description=(
        "`codec.dispatch_of`, lifted out of `metadata.dispatch` by the API. OPTIONAL ON PURPOSE, "
        "and it is the one field in this interface whose absence means something. `task_to_api` "
        "always sends it -- filling the API's defaults for a task submitted before the feature "
        "existed -- so a missing key here is not an old TASK, it is an older API than this "
        "bundle. Those are different and `dispatchOf` below keeps them different: an old task "
        "reads `collect`, an old API reads \"not reported\"."
    ))
    contract_acceptance: Any = Field(None, description=(
        "The step's `contract_acceptance` (jev-contract.md sections 5.4 and 8.2), served on the "
        "detail only and only where Jev judged the step's work contract: read by "
        "`ContractJev.contractAcceptance`, drawn as Jev's."
    ))
    links: TaskLinks | None = Field(None, description=(
        "The console link, for surfaces OUTSIDE the console (MCP, sc, plugin, PR bodies); inside "
        "it the address bar is the link."
    ))
    claude_session: Any = Field(None, description=(
        "The Claude Code session that submitted it (SC-SESSION-SCOPE), lifted out of `metadata`; "
        "read by the sc plugin, the console does not scope by it."
    ))
    budget: Any = Field(None, description=(
        "Contract request 120: the attempt's resolved per-agent budget, `{usd?, tokens?}`, or null "
        "for none."
    ))


class TaskEnvelope(BaseModel):
    """`GET /v1/tasks/{task_id}`: the one task, under `task`."""

    model_config = ConfigDict(title="TaskEnvelope")

    task: Task


# --------------------------------------------------------------------------
# The shapes the console's field-contract table held by regex (part of #72).
#
# Same conventions as `Task`: REQUIRED is what every route serving the shape
# sends (and the console reads as required); everything else has a default and
# is optional in the generated type. Nested shapes the console still narrows by
# hand are `Any`. Each is attached to its route(s) with `responses=` only, so
# the dict the route builds is still what it serves.
# --------------------------------------------------------------------------


class Pool(BaseModel):
    """`pool_to_api` in codec.py: one capacity pool's ceiling and use."""

    model_config = ConfigDict(title="Pool")

    name: str
    hard_limit: int | None
    adaptive_target: int | None
    quota_derived_limit: int | None
    effective_limit: int | None
    active: int
    available: int | None
    enabled: bool
    updated_at: datetime
    lowered_by: list[Any] | None = Field(None, description=(
        "What lowered `effective_limit` below `hard_limit`, one entry per cause."
    ))


class ProfileBlocker(BaseModel):
    """One pool that refuses a runner profile's next agent (`ProfileAdmission.blockers`)."""

    model_config = ConfigDict(title="ProfileBlocker")

    pool: str
    reason: str
    limit: int | None
    active: int
    group: str | None


class Counterfactual(BaseModel):
    """What one relaxed ceiling would buy a runner profile (`ProfileAdmission.counterfactual`)."""

    model_config = ConfigDict(title="Counterfactual")

    pool: str
    action: str
    headroom_after: int | None
    basis_after: str
    delta: int | None
    next_binding: list[str]


class ProfileAdmission(BaseModel):
    """How many more agents of one runner profile fit now, and what binds it."""

    model_config = ConfigDict(title="ProfileAdmission")

    units: int
    headroom: int | None
    basis: str
    blockers: list[ProfileBlocker]
    binding: list[str]
    counterfactual: list[Counterfactual]
    complete: bool
    unread: list[str]
    uncapped: list[str]


class RunnerProfile(BaseModel):
    """One entry of `GET /v1/capacity`'s `runner_profiles`, keyed by the profile's name."""

    model_config = ConfigDict(title="RunnerProfile")

    resource_class: str
    backend: str
    provider: str | None
    units: int
    pools: list[str]
    pool_units: dict[str, int] | None = Field(None, description=(
        "Contract request 129: the units this profile takes from each of its pools."
    ))
    available: bool | None = None
    disabled_reason: str | None = None
    admission: ProfileAdmission | None = None
    input_contract: Any = Field(None, description="What the profile's input must carry.")


class Capacity(BaseModel):
    """`GET /v1/capacity`: the caller's pools and what each runner profile can admit."""

    model_config = ConfigDict(title="Capacity")

    pools: list[Pool]
    runner_profiles: dict[str, RunnerProfile]
    tenant_id: str | None = None
    pools_complete: bool | None = None
    blocked_reason_groups: dict[str, list[str]] | None = None
    generated_at: datetime


class TaskPage(BaseModel):
    """`GET /v1/tasks`: one page of tasks. The paging key is `next_page_token`."""

    model_config = ConfigDict(title="TaskPage")

    tasks: list[Task]
    next_page_token: str | None = Field(None, description=(
        "Pass back as `page_token` for the next page; null on the last. This API has never "
        "sent `next_cursor`."
    ))
    tenant_id: str | None = None


class AttemptRow(BaseModel):
    """`attempt_to_api` in codec.py: one attempt, with its five spend fields."""

    model_config = ConfigDict(title="AttemptRow")

    attempt_id: str
    task_id: str
    tenant_id: str
    generation: int
    lease_id: str
    backend: str
    execution_name: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    exit_code: int | None
    error: str | None
    error_redaction_count: int | None = None
    peak_rss_bytes: int | None
    peak_disk_bytes: int | None
    oom_near_miss: bool
    checkpoints: list[str]
    input_tokens: int | None
    output_tokens: int | None
    cache_read_input_tokens: int | None
    cache_creation_input_tokens: int | None
    cost_usd: float | None
    cpu_seconds: float | None = None
    peak_cpu_cores: float | None = None
    mean_cpu_cores: float | None = None
    cpu_limit_cores: float | None = None
    cpu_measured_at: datetime | None = None
    cpu_limit_source: str | None = None
    cpu_reading_age_seconds: float | None = None
    model: str | None = None
    size_version: int | None = None
    sized_cpu_request: float | None = None
    sized_cpu_limit: float | None = None
    sized_memory_gib: float | None = None
    sized_disk_gib: float | None = None
    sized_live_split: Any = None
    account_id: str | None = None
    credential_source: str | None = None
    credential_tier: str | None = None


class AttemptPage(BaseModel):
    """`GET /v1/tasks/{task_id}/attempts`."""

    model_config = ConfigDict(title="AttemptPage")

    task_id: str
    read_at: datetime
    attempts: list[AttemptRow]
    accounts_by_attempt: dict[str, Any] = Field(description=(
        "The subscription account each attempt ran on, keyed by attempt id."
    ))


class LeaseThresholds(BaseModel):
    """The reconciler's own thresholds, served with the leases so no client restates them."""

    model_config = ConfigDict(title="LeaseThresholds")

    heartbeat_grace_seconds: int
    lease_timeout_seconds: int


class LeaseRow(BaseModel):
    """`lease_to_api` in codec.py. The lease's own state is `dispatch_state`, never `state`:
    nothing writes STARTING or RUNNING to a lease, so it is not the task's state."""

    model_config = ConfigDict(title="LeaseRow")

    lease_id: str
    task_id: str
    attempt_id: str
    tenant_id: str
    generation: int
    pools: list[str]
    units: int
    pool_units: dict[str, int] | None = None
    dispatch_state: str
    created_at: datetime
    dispatch_deadline: datetime
    expires_at: datetime
    heartbeat_at: datetime | None
    released_at: datetime | None
    release_reason: str | None
    released: bool
    expired: bool
    dispatch_overdue: bool
    silent_seconds: int
    heartbeat_ever: bool
    last_error: str | None


class LeasePage(BaseModel):
    """`GET /v1/admin/leases`."""

    model_config = ConfigDict(title="LeasePage")

    leases: list[LeaseRow]
    next_page_token: str | None = None
    thresholds: LeaseThresholds
    evaluated_at: datetime
    active_only: bool
    tenant_id: str | None
    units_held: int
    units_held_by_pool: dict[str, int] | None = None
    active_beyond_window: int
    truncated: bool
    examined: int


class LeaseHeartbeat(BaseModel):
    """One row of `GET /v1/leases`: a live lease's heartbeat, for a tenant's member."""

    model_config = ConfigDict(title="LeaseHeartbeat")

    task_id: str
    lease_id: str
    attempt_id: str
    generation: int
    task_state: TaskState
    dispatch_state: str
    created_at: datetime
    dispatch_deadline: datetime
    expires_at: datetime
    heartbeat_at: datetime | None
    silent_seconds: int | None
    silent: bool
    expired: bool
    dispatch_overdue: bool


class LeaseHeartbeatPage(BaseModel):
    """`GET /v1/leases`."""

    model_config = ConfigDict(title="LeaseHeartbeatPage")

    tenant_id: str
    read_at: datetime
    thresholds: LeaseThresholds
    heartbeats: list[LeaseHeartbeat]


class QuotaState(BaseModel):
    """`quota_to_api` in codec.py: one provider's quota reading for one tenant."""

    model_config = ConfigDict(title="QuotaState")

    provider: str
    tenant_id: str
    state: str
    updated_at: datetime
    configured_hard_max: int
    adaptive_target: int | None
    quota_derived_limit: int | None
    requests_remaining: int | None
    tokens_remaining: int | None
    reset_at: datetime | None
    cooldown_until: datetime | None
    last_429_at: datetime | None
    retry_after_seconds: int | None
    success_count: int
    rate_limit_count: int
    effective_limit: int
    feeds_pool: Pool | None = Field(None, description="The pool this reading lowers, if any.")


class QuotaPage(BaseModel):
    """`GET /v1/admin/quota`."""

    model_config = ConfigDict(title="QuotaPage")

    tenant_id: str | None
    quota: list[QuotaState]
    truncated: bool
    generated_at: datetime


class Tenant(BaseModel):
    """`tenant_to_api` in codec.py."""

    model_config = ConfigDict(title="Tenant")

    tenant_id: str
    kind: str
    principal: str
    display_name: str | None
    created_at: datetime
    max_active: int
    capacity_units: int
    monthly_budget_usd: float | None
    enabled: bool
    credentials: list[str]
    service_account: str | None
    gcs_prefix: str | None
    namespace: str | None
    workspace_kind: str | None = Field(None, description=(
        "The workspace record's kind; null for a Terraform tenant (WK-ADMIN-LIST)."
    ))
    workspace_id: str | None = Field(None, description=(
        "The workspace record's w- id; null for a Terraform tenant (WK-ADMIN-LIST)."
    ))


class TenantPage(BaseModel):
    """`GET /v1/admin/tenants`."""

    model_config = ConfigDict(title="TenantPage")

    tenants: list[Tenant]
    workspaces_unread: bool = Field(description=(
        "Whether the workspace records could not be read, so `workspace_kind` and "
        "`workspace_id` read null for every row."
    ))


class Stats(BaseModel):
    """`GET /v1/stats`. `platform_tasks_by_state` is ABSENT, not empty, for a non-admin:
    an absent key is not a count of nothing."""

    model_config = ConfigDict(title="Stats")

    tenant_id: str
    tasks_by_state: dict[str, int]
    dispatch_paused: bool
    limits: dict[str, int]
    generated_at: datetime
    platform_tasks_by_state: dict[str, int] | None = Field(None, description=(
        "Served only to an admin who asks for `scope=platform`; absent otherwise."
    ))
    dispatch_state: str | None = Field(None, description="running, paused or unknown (U26).")
    control_document: str | None = Field(None, description="present or missing (U26).")


class Me(BaseModel):
    """`GET /v1/tenants/me`: the caller and the workspace they are in."""

    model_config = ConfigDict(title="Me")

    tenant: Any
    principal: Any
    environment: str
    environment_declared: bool
    role: Any = Field(None, description="The caller's role in the selected workspace, {name, source}.")
    features: Any = Field(None, description=(
        "caller_mcp/await_input/service_answer, each true, false or \"unknown\" (FEATURES-READ)."
    ))


class DispatchControl(BaseModel):
    """`GET /v1/admin/dispatch`: the platform dispatch switch."""

    model_config = ConfigDict(title="DispatchControl")

    dispatch_paused: bool
    updated_at: datetime | None
    updated_by: str | None
    reason: str | None
    dispatch_state: str | None = Field(None, description="running, paused or unknown (U26).")
    control_document: str | None = Field(None, description="present or missing (U26).")


class Workflow(BaseModel):
    """`GET /v1/workflows/{workflow_id}`: the workflow, its steps and its derived state."""

    model_config = ConfigDict(title="Workflow")

    workflow_id: str
    state: str
    tenant_id: str
    stored_state: str
    state_source: str
    rollup: Any = None
    drift: Any = None
    created_at: datetime
    updated_at: datetime
    submitted_by: str | None
    submitted_by_name: str | None = None
    priority: int
    on_step_failure: str
    cancel_requested: bool
    steps: list[Any]
    pull_request: Any = None
    claude_session: Any = Field(None, description=(
        "The Claude Code session that submitted it (SC-SESSION-SCOPE); read by the sc plugin."
    ))
    links: Any = Field(None, description=(
        "The console link, for surfaces OUTSIDE the console (MCP, sc, plugin, PR bodies)."
    ))
    repository_url: str | None = Field(None, description="The workflow's repository, masked as a task's.")
    title: str | None = None
    description: str | None = Field(None, description="The submitter's plain-text description.")
    description_source: str | None = Field(None, description="Who wrote the description.")
    about: Any = Field(None, description="{purpose, goals, output}: the description in three parts.")
    feature: Any = Field(None, description="{title, url?} of the feature it builds.")
    started_from: str | None = Field(None, description=(
        "schedule, quality_check, issue_run, claude_code or console."
    ))


class WorkflowEnvelope(BaseModel):
    """`GET /v1/workflows/{workflow_id}`: the workflow, its step tasks and its dispatch."""

    model_config = ConfigDict(title="WorkflowEnvelope")

    workflow: Workflow
    tasks: list[Task]
    dispatch: Any


class ProviderEntry(BaseModel):
    """One provider of `GET /v1/providers`."""

    model_config = ConfigDict(title="ProviderEntry")

    provider: str
    credential_registered: bool
    credential_source: str | None = None
    runnable_profiles: list[str] | None = None
    runner_profiles: list[str]
    quota: QuotaState | None


class ProvidersPage(BaseModel):
    """`GET /v1/providers`."""

    model_config = ConfigDict(title="ProvidersPage")

    tenant_id: str
    providers: list[ProviderEntry]
    account_pool: str | None = None
    generated_at: datetime


class TaskEvent(BaseModel):
    """`event_to_api` in codec.py: one entry of a task's event log."""

    model_config = ConfigDict(title="TaskEvent")

    event_id: str
    task_id: str
    type: str
    at: datetime
    attempt_id: str | None
    lease_id: str | None
    generation: int | None
    detail: dict[str, Any] | None
    detail_redaction_count: int | None = None


class TaskEventPage(BaseModel):
    """`GET /v1/tasks/{task_id}/events`."""

    model_config = ConfigDict(title="TaskEventPage")

    task_id: str
    events: list[TaskEvent]
    next_page_token: str | None
