import type { PoolFields } from './accountsPool'
import {
  HEALTH_KINDS,
  normPoolHealth,
  type AccountHealthRecord,
  type HealthEvent,
  type HealthKind,
  type HealthRead,
  type HealthStatus,
  type PoolHealth,
} from './accountHealthModel'
import { chosenTenant, noteFixtureProbe, read, readGeneration, route, tenantQuery, write, type ApiError, type ApiRoute, type Result } from './fetch'
// A VALUE import, not a type: the attempt fixture needs the terminal-state set
// so a live task's newest attempt is rendered as live, and the task fixture
// needs the concurrency set to decide which rows hold a lease -- invariant 1's
// four states, not a second list written out here.
import { CONCURRENCY_STATES, MODEL_PRICE_RATES, TERMINAL_STATES } from './types'
// The one rule for summing a nullable measurement: null until something
// actually reported it, so an unmeasured sample never becomes a confident zero.
import { sumReported } from './measure'
import { TASK_PAGE_LIMIT } from './pageLimits'
import { normUtilisation, type UtilisationRead } from './utilisation'
import { noteProfile, revisionAfterZoneSaves } from './timezone'
import type { Outcomes } from './outcomes'
import { ledgerFixture } from './outcomes.fixture'
import {
  appendReadable, normIndexDoc, normLanguages, normPermissionsFromTokens, normReadable, normRepoDetail, normRepoList, normResolvedFromTokens, normTestSelection, normTokens,
  type GitToken, type IndexDoc, type LanguageRow, type Permissions, type ReadableList, type RepoDetail, type RepoRecord,
  type ResolvedToken, type TestSelection, type TokenScope,
} from './RepositoriesData'
import {
  normCallGraph, normImpact, normModuleGraph, normSymbolSearch, normSymbolTests,
  type CallGraph, type ImpactPlan, type ModuleGraph, type SymbolSearch, type SymbolTests,
} from './RepoGraphData'
import type {
  AnswerVia, ArtifactContent, ArtifactListing, LogStream, LogStreamName, OwnerAnswer, TaskAnswer, TaskInputCopy, TaskTranscript, TranscriptStep,
  CheckpointRecord, CheckpointsPage, LogAttempt, TaskLogs,
  Capacity, DispatchControl, Me, ProvidersPage, Stats, Task, TaskEvent, TaskPage,
  AdminPool, AttemptRow, AttemptsPage, TaskEventsPage, TaskAccount, LeaseHeartbeat, LeaseHeartbeatPage, LeasePage, LeaseRow, Pool, ProfileAdmission, QuotaState, ResourceClassSpec,
  ModelPrice, ModelPriceChange, ModelPriceRate, ModelPriceTable, ModelPriceTier, PlatformSettings,
  RunnerInputContract, Runtime, TaskState, Waiting,
  TaskWindow, Tenant,
  Workflow, WorkflowPage,
  Account, AccountStateName, AccountsPage, RefreshResponse,
  AccountAuthorization, AccountExchangeResponse,
  IssuePreviewRead, IssueRefDoc, IssueRun, IssueRunPage, IssueRunRead, IssueRunSummaryPage, RunCreateBody, RunPlan,
  GitHubAuthorization, GitHubAuthorizeSurface, GitHubDisconnectResponse, GitHubExchangeBody, GitHubExchangeResponse, OnboardingDoc,
  OnboardingDismissal, OwnerTokenResponse, AccessInstallRequestResponse,
  AdminGrantAnswer, AdminRemoveAnswer, AdminWorkspaceView, LoanDeclineAnswer, LoanRequestAnswer, NewWorkspaceBody, PeopleDoc, ScopedPeopleDoc, WorkspaceActionAnswer, WorkspaceKind, WorkspaceLimitsAnswer, WorkspaceLoanAnswer, WorkspaceState, WorkspaceView,
  AccessCheckName, AccessDisableResponse, AccessEnableResponse, AccessGrantResponse, AccessMembers, AccessMode, AccessOverview,
  AccessOwners, AccessRepositoryPage, AccessRevokeResponse, AccessVerifyResponse,
  NotificationTopic, Preferences, PreferencesSaved, PreferencesWrite, Profile, TimeZoneSource, ProfileClaudeAccount, ProfileClaudeAccounts, ProfileDefault,
  ProfileGitHub, ProfileIdentity, ProfileKeyStatus, ProfileNotifications, ProfileQuotaWindow, ProfileSection, ProfileTypeSafe, ProfileWorkspace,
  ReminderCadence, WorkspaceRole,
  DefaultRoleChange, PersonRoleChange, PersonRoles, PersonWorkspaceRole, RoleSummary, WorkspaceRoleEvent, WorkspaceRoleMe,
  WorkspaceRoleOverride, WorkspaceRoles, WorkspaceRoster,
  JoinedMember, JoinedMemberRemoval, JoinedMembers, JoinLinkState, JoinLookup, JoinRequestMine, JoinRequestState,
  AdminWorkspaceRoles, AdminWorkspaceRolesPage,
  QaIdentityGrantDoc, QaIdentityList, QaIdentityMine,
  RunnerRecentUse, RunnerSize, RunnerSizeChanges, RunnerSizesDoc, RunnerSizesPreview, RunnerSizeVersion,
} from './types'

// The fetch contract lives in fetch.ts. This file is only the list of reads
// this product performs, and what "empty" means for each of them -- which is
// per-endpoint and cannot be guessed: a capacity response with no pools and a
// task page with no tasks are both non-empty objects.

export type { Result, ApiError } from './fetch'
export type { AnswerVia, OwnerAnswer, OwnerAnswerVersion } from './types'

/** Fixtures, so the UI can be worked on before DNS resolves. Exported so a
 *  component that keeps its own loaders (CheckpointBrowser.tsx) reads this one
 *  flag rather than restating the expression. */
export const USE_FIXTURES = import.meta.env.DEV && !import.meta.env.VITE_LIVE

/**
 * THE INSTANT EVERY FIXTURE TIME IS COUNTED FROM (QAFIX6-FIXTURE a, visual QA
 * run 6 flow-find-failure-desktop-02 and -phone-03, part of #1680): the
 * capture clock `qa/routes.json` freezes the page at. Each fixture read used
 * to mint its times from its own `Date.now()`, so a finished task's end moved
 * on every open -- 'ended 07:53:11', then 07:53:22, then 07:54:04 on the
 * phone. Read once, here, so every read, every reload and both devices serve
 * the same document. A capture sees the times it always saw, because its page
 * clock IS this instant; a session on a running clock sees them as past.
 * `fetchedAt` stays the client's own clock: it is when the browser read, not
 * a time the fixture serves. Held to routes.json by qafix6.fixture.test.tsx.
 */
export const FIXTURE_CLOCK = '2026-10-19T12:00:00Z'
const FIXTURE_NOW = Date.parse(FIXTURE_CLOCK)

/**
 * WHO THE FIXTURE BUILD ANSWERS AS (QAFIX4-FIXTURE-A; docs/design/visual-qa.md
 * §4.3, visual QA run 4 admin-004 and admin-005). `member` is the fixture
 * identity every capture has always seen: alice@example.com in u-bogdan, not
 * a platform admin, administering no group workspace. `platform-admin` is the
 * same person as a platform admin who also administers one group workspace,
 * `eng`, so the admin pages draw their controls rather than their refusals.
 *
 * `swarm_qa capture` sets the cookie (`PERSONA_COOKIE` in swarm_qa.py) on the
 * console's origin before every route's navigation, `member` included, from
 * the route's `persona` in qa/routes.json. A cookie, not storage: the capture
 * empties local and session storage on every load. Read ONCE, here, and only
 * in the fixture build, so no live read ever depends on it; a dev server
 * opened by hand has no cookie and is the member, as is any other value.
 */
export type FixturePersona = 'member' | 'platform-admin' | 'workspace-admin'
const PERSONA_COOKIE = 'swarm_qa_persona'

function readFixturePersona(): FixturePersona {
  if (typeof document === 'undefined') return 'member'
  for (const part of document.cookie.split(';')) {
    const at = part.indexOf('=')
    if (at > 0 && part.slice(0, at).trim() === PERSONA_COOKIE) {
      const value = part.slice(at + 1).trim()
      return value === 'platform-admin' || value === 'workspace-admin' ? value : 'member'
    }
  }
  return 'member'
}

const FIXTURE_PERSONA: FixturePersona = USE_FIXTURES ? readFixturePersona() : 'member'
/** Whether the fixture caller is a platform admin: the `platform-admin` persona only. */
const fixtureIsAdmin = FIXTURE_PERSONA === 'platform-admin'
/**
 * The `workspace-admin` persona (ADMIN-PEOPLE-ROLES): eng's stored admin and
 * no platform admin, so Admin › People and roles is answered scoped to eng
 * (S5f) and every platform Admin read refuses it as the member's does.
 */
const fixtureIsWorkspaceAdmin = FIXTURE_PERSONA === 'workspace-admin'
/** The group workspace the `platform-admin` persona administers: `tenant:eng` in fixtureCapacity is its pool. */
const FIXTURE_GROUP = { tenant_id: 'eng', principal: 'eng@example.com' } as const

/** A refusal as an admin-gated route answers a caller who is not a platform admin (`admin_auth`). */
function fixtureAdminRefusal<T>(target: ApiRoute, latencyMs: number): Result<T> {
  noteFixtureProbe(target, latencyMs, false)
  return {
    status: 'error',
    error: { kind: 'admin_required', httpStatus: 403, code: 'forbidden', message: 'admin group membership is required' },
  }
}

export async function loadCapacity(options: { frame?: boolean } = {}): Promise<Result<Capacity>> {
  if (USE_FIXTURES) return fixtureCapacity()
  // `frame`: the shell's capacity meter reads this too, as the FRAME's read
  // (CH-2), so its age never stands in for the page's own.
  return read<Capacity>(route('/v1/capacity'), (d) => d.pools.length === 0, { frame: options.frame === true })
}

/**
 * `GET /v1/admin/pools` (#133): every pool, with who last changed it through
 * an admin route, when, and what changed. Admin-gated, so a non-admin's 403 is
 * information. Pool limits joins it onto `/v1/capacity` by pool name; the
 * record is not on `/v1/capacity` because every tenant member reads that.
 */
export async function loadAdminPools(): Promise<Result<{ pools: AdminPool[] }>> {
  if (USE_FIXTURES) return fixtureAdminPools()
  return read<{ pools: AdminPool[] }>(route('/v1/admin/pools'), (d) => d.pools.length === 0)
}

async function fixtureAdminPools(): Promise<Result<{ pools: AdminPool[] }>> {
  const cap = await fixtureCapacity()
  noteFixtureProbe(route('/v1/admin/pools'), 0, true)
  if (cap.status !== 'ok') return { status: 'empty', fetchedAt: Date.now() }
  // One pool carries a record, so the row that prints it is looked at; the
  // rest are null, as a pool no admin has touched is.
  const pools = cap.data.pools.map((p, i): AdminPool =>
    i === 0 && p.hard_limit !== null
      ? {
          ...p,
          admin_changed_by: 'ops@example.com',
          admin_changed_at: new Date(FIXTURE_NOW - 3 * 3_600_000).toISOString(),
          admin_change: { hard_limit: { from: p.hard_limit * 2, to: p.hard_limit } },
        }
      : { ...p, admin_changed_by: null, admin_changed_at: null, admin_change: null },
  )
  return { status: 'ok', fetchedAt: Date.now(), data: { pools } }
}

// `TASK_PAGE_LIMIT` lives in `pageLimits.ts`: the check layer (checks.ts)
// names it, and importing it from here dragged this client -- and Vite's
// `import.meta.env` -- into a layer the node test loader runs bare.
export { TASK_PAGE_LIMIT }

/**
 * A page of the task list. `limit` is the full page unless a caller has a
 * reason to ask for less -- the Agents list at phone width asks for 50
 * (Agents.tsx, `PHONE_PAGE_LIMIT`).
 *
 * `view: 'summary'` (#168) asks for `GET /v1/tasks?view=summary`: each row
 * WITHOUT `input` and `result_summary` and their redaction counts -- the keys
 * absent, not null (`codec.task_to_api`) -- and with `metadata` cut to its
 * `title` alone (SUMMARY-TITLE, `codec.SUMMARY_METADATA_KEYS`). Those are
 * most of a row's 10-20 KiB (docs/web-ui §2.5), so a polled list that draws
 * none of them asks for it: the Overview and the Agents list. Anything that
 * reads a row's input, the rest of its metadata or its result summary reads
 * `loadTask` instead, which is always the full row. No `view` is the full
 * row, as before.
 */
export async function loadTasks(
  limit: number = TASK_PAGE_LIMIT,
  options: { view?: 'summary'; submittedBy?: 'me'; pageToken?: string | null } = {},
): Promise<Result<TaskPage>> {
  // A FIXTURE LIST IS ONE PAGE: it never hands out a token, so a page after
  // it is the end of the list.
  if (USE_FIXTURES) {
    return options.pageToken ? { status: 'empty', fetchedAt: Date.now() } : fixtureTasks()
  }
  // One route with the `?state=` and paged reads below: `/v1/tasks` (CH-18),
  // whatever page size and view the caller asked for, and the page after a
  // token (the timeline's span read, live QA RT-1). `submitted_by=me` (U5)
  // narrows the page to the caller IN THE QUERY, so the page is full of
  // theirs rather than the tenant's newest filtered down (live QA SUB-6).
  const q = new URLSearchParams({ limit: String(limit) })
  if (options.view === 'summary') q.set('view', 'summary')
  if (options.submittedBy === 'me') q.set('submitted_by', 'me')
  if (options.pageToken) q.set('page_token', options.pageToken)
  return read<TaskPage>(route('/v1/tasks', {}, q), (d) => d.tasks.length === 0)
}

/**
 * Workflows plus the task documents their steps point at.
 *
 * Two requests, because `GET /v1/workflows` returns steps with NO state
 * (codec.py:354-365) -- a step's state only exists on the task it created. The
 * list endpoint does not join for us the way `GET /v1/workflows/{id}` does, so
 * the join happens here, once.
 *
 * The second request is allowed to fail on its own. When it does the board
 * still renders -- the DAG shape, the dependencies and the step names are all
 * in the first response -- but `taskById` is null and every node says its state
 * could not be read. That is the point: a failed join must not look like a
 * workflow full of idle steps.
 */
export interface WorkflowBoard {
  workflows: Workflow[]
  /** null means the task read failed; an EMPTY map means it succeeded with no tasks. */
  taskById: Map<string, Task> | null
  /** Why states are missing, for the banner. null when the join succeeded. */
  statesDetail: string | null
  /**
   * The list read's `next_page_token`: older workflows exist past these 100
   * (live QA WF-2). Absent or null when the read holds the whole history, or
   * when it is not a list read at all.
   */
  nextPageToken?: string | null
}

/**
 * EVERY LIST READ OF WORKFLOWS ASKS FOR THE SUMMARY VIEW (live QA CH-21,
 * 2026-10-11): each step without its masked `input`, which no list, board or
 * count in this console draws. A step's input is read on its workflow
 * (`loadWorkflow`) or its task. An API older than the view ignores the
 * parameter and serves the full rows, which carry the same fields and more.
 */
export const WORKFLOW_LIST_VIEW = 'view=summary'

export async function loadWorkflowBoard(): Promise<Result<WorkflowBoard>> {
  if (USE_FIXTURES) return fixtureWorkflowBoard()

  const [wf, tasks] = await Promise.all([
    read<WorkflowPage>(route('/v1/workflows?limit=100', {}, WORKFLOW_LIST_VIEW), (d) => d.workflows.length === 0),
    read<TaskPage>(route('/v1/tasks', {}, 'limit=200'), (d) => d.tasks.length === 0),
  ])

  // The workflow read decides whether there is a screen at all.
  if (wf.status === 'loading' || wf.status === 'error') return wf
  if (wf.status === 'stale') {
    return { status: 'stale', data: emptyBoard(wf.data.workflows), fetchedAt: wf.fetchedAt, error: wf.error }
  }
  if (wf.status === 'empty') return wf

  // A task read that did not produce rows is not the same as one that failed.
  // `empty` is a real answer -- no tasks exist -- so the join is complete and
  // every step is legitimately "not started". Only a FAILURE leaves states
  // unknown, and only that sets statesDetail.
  if (tasks.status === 'error' || tasks.status === 'stale') {
    return {
      status: 'ok',
      fetchedAt: wf.fetchedAt,
      serverAt: wf.serverAt,
      data: {
        workflows: wf.data.workflows,
        taskById: null,
        statesDetail: tasks.error.message,
        nextPageToken: wf.data.next_page_token ?? null,
      },
    }
  }

  const taskById = new Map<string, Task>()
  if (tasks.status === 'ok') for (const t of tasks.data.tasks) taskById.set(t.id, t)
  return {
    status: 'ok',
    fetchedAt: wf.fetchedAt,
    serverAt: wf.serverAt,
    data: { workflows: wf.data.workflows, taskById, statesDetail: null, nextPageToken: wf.data.next_page_token ?? null },
  }
}

/**
 * THE NEXT 100 WORKFLOWS, OLDER THAN A LIST READ (live QA WF-2, 2026-10-11).
 * The list read only its newest 100 and offered no way past them, while its
 * counts and its "oldest first" read as the tenant's whole history:
 * `Failed 15` against 52 failed workflows. `token` is the previous read's
 * `next_page_token`. No task join: the list reads each older row's steps by
 * id as it scrolls in (`useRowReads`), as it already does for the first 100.
 */
export async function loadOlderWorkflows(token: string): Promise<Result<WorkflowPage>> {
  if (USE_FIXTURES) return { status: 'ok', fetchedAt: Date.now(), data: { workflows: [], next_page_token: null } }
  return read<WorkflowPage>(
    route('/v1/workflows', {}, new URLSearchParams({ limit: '100', page_token: token, view: 'summary' })),
    (d) => d.workflows.length === 0,
  )
}

function emptyBoard(workflows: Workflow[]): WorkflowBoard {
  return { workflows, taskById: null, statesDetail: 'The agent read did not complete.' }
}

/**
 * The workflow LIST alone -- no join, for the landing screen.
 *
 * `loadWorkflowBoard` above exists to draw the graph, so it pays for a second
 * request to learn each step's state. Overview does not draw a graph: it asks
 * whether anything has stopped moving, and `GET /v1/workflows` already answers
 * that without help. Every row carries `rollup.counts`, which the route derived
 * server-side from the step tasks it read (routes/workflows.py:64-84), so the
 * per-state breakdown this screen needs is in the first response and a join
 * here would be a second read of documents the API has already read.
 *
 * The page also carries `rollup_report`, which says whether the derivation was
 * complete. A caller that ignores it can report an all-clear over workflows
 * whose state was never established.
 */
export async function loadWorkflows(limit: number = 100): Promise<Result<WorkflowPage>> {
  if (USE_FIXTURES) return fixtureWorkflows()
  // 100, matching loadWorkflowBoard: `max_workflow_steps` is 50 and the route's
  // step-read budget is what actually binds, so a larger page buys rows whose
  // rollup is incomplete rather than more information. A caller that lists a
  // handful (Submit's recent card, live QA SUB-6) asks for fewer.
  // Both are the one registry route, `/v1/workflows`: `route` keys by the
  // template without its query.
  if (limit !== 100) return read<WorkflowPage>(route('/v1/workflows', {}, `limit=${limit}&${WORKFLOW_LIST_VIEW}`), (d) => d.workflows.length === 0)
  return read<WorkflowPage>(route('/v1/workflows?limit=100', {}, WORKFLOW_LIST_VIEW), (d) => d.workflows.length === 0)
}

/**
 * HOW MANY EVENTS A RUN SCREEN ASKS FOR, and why it has to ask at all.
 *
 * `GET /v1/tasks/{id}/events` with no `limit` answers with the API's DEFAULT
 * page, `default_page_size = 50` (swarm_api/settings.py), not its maximum.
 * Every comment in this client -- and redesign-v2 §4 -- reasons about "the
 * 200-event page", and every run screen was in fact reading 50: at one
 * heartbeat event per 150s plus a checkpoint event per 120s, that page ends
 * around the half-hour mark of an attempt, so the peak-memory line, the
 * checkpoint strip and the checkpoint table's locations all stopped there.
 *
 * 200 is `max_page_size`, the server's own cap (`paged_limit` clamps to it,
 * silently). Asking for more would be clamped to the same 200 and would make
 * a full page look like one that asked for less. The route pages -- since #19
 * it returns `next_page_token` whenever more events exist, and takes
 * `order=desc` -- but this client reads ONE page, oldest-first, and does not
 * follow the page token, so a page that comes back FULL is a window, and the
 * charts say so. That limit is this client's, not the API's (help topic
 * `event-paging`).
 *
 * WHAT THIS CANNOT SEE. `MAX_PAGE_SIZE` is environment-overridable and set
 * nowhere in this repository today. A deployment that lowered it would clamp
 * this request below 200, and a full page would then arrive SHORT of this
 * number and not be flagged as full. Nothing here ever reads a short page as
 * complete in the other direction either: an interval with no recorded end
 * stays open whatever the page length (duration.ts).
 */
export const EVENT_PAGE_LIMIT = 200

/**
 * HOW MANY ATTEMPT DOCUMENTS A RUN SCREEN ASKS FOR: the same cap, for the
 * same reason.
 *
 * `GET /v1/tasks/{id}/attempts` goes through the same `paged_limit`
 * (routes/tasks.py), so with no `limit` it returned 50 attempts, NEWEST
 * first. task_d18d8d8b044d469cb43c reached 83 attempts on 2026-09-23 before
 * the retry cap was enforced on the dispatch-failure path; the inspector
 * would have shown 50 of them and the phase chart's sum would have said
 * "over 50 of 50". The sum now counts against `task.attempt_count`, which
 * catches a short read whatever its cause; this constant makes the read
 * short less often.
 *
 * Stated as its own constant, not as `EVENT_PAGE_LIMIT`, because the two
 * routes happen to share a cap today and nothing makes them share one.
 */
export const ATTEMPT_PAGE_LIMIT = 200

/**
 * One agent, with its event timeline.
 *
 * Two reads, not three. `GET /v1/tasks/{id}/artifacts` is not called because
 * it would be a second copy of something the first read already carries. The
 * route used to read a Firestore subcollection nothing writes, and answered
 * [] for every task; it has since been pointed at the real writer, and
 * `store.list_artifacts` now serves the manifest out of `task.result_summary`
 * (`artifacts`, `artifacts_skipped`, and a `complete` flag that is simply
 * whether `result_summary` exists). That is the same field this screen reads
 * off the task document, so a third request would fetch the same manifest
 * again -- one more read that could fail on its own and disagree with the
 * first. See ResultSummary in types.ts.
 */
export interface AgentDetail {
  task: Task
  /** null means the event read FAILED. An empty array means there are none. */
  events: TaskEvent[] | null
  eventsDetail: string | null
}

/**
 * HOW MANY OF THE CALLER'S OTHER TENANTS A TASK 404 ASKS (#1194 c15). The
 * probes run one after another, so a caller in many groups costs this many
 * reads at most, once per task and chosen tenant (`ELSEWHERE_KEEP_MS`, and
 * until a write or sign-out: `readGeneration`), never a burst against the
 * 20 rps per-principal budget. A chosen value: a task in a tenant past the
 * eighth is drawn as today's 404, which is never a wrong answer.
 */
export const ELSEWHERE_PROBE_CAP = 8

/**
 * How long one probe's answer is kept for the same task under the same chosen
 * tenant. The drawer re-reads a failed task on its back-off, and asking every
 * other tenant again on each of those reads would multiply the probe by the
 * poll; a minute covers the reads a person waits through before they act.
 */
export const ELSEWHERE_KEEP_MS = 60_000

const elsewhereKept = new Map<string, { at: number; answer: Promise<TenantChoice | null> }>()

/**
 * THE ONE OTHER TENANT OF THE CALLER'S THAT HOLDS `taskId`, or null.
 *
 * `GET /v1/tasks/{id}` answers the same 404 for a task that does not exist and
 * for one in another tenant (store.py), and this does not widen that answer:
 * it asks ONLY under the tenants `GET /v1/tenants/mine` lists for this caller
 * -- each with that tenant's own `X-Swarm-Tenant`, which the API honours only
 * for a membership it confirmed -- one at a time and at most
 * `ELSEWHERE_PROBE_CAP`. Exactly one 200 names the tenant; none, or more than
 * one (a task id is unique, so that would be a confused API), is null and the
 * 404 is drawn as it always was. The choice itself is never changed here.
 */
export function taskElsewhere(taskId: string): Promise<TenantChoice | null> {
  const chosen = chosenTenant()
  const key = `${readGeneration()}\n${chosen ?? ''}\n${taskId}`
  const kept = elsewhereKept.get(key)
  if (kept !== undefined && Date.now() - kept.at < ELSEWHERE_KEEP_MS) return kept.answer
  const answer = (async (): Promise<TenantChoice | null> => {
    const mine = await loadMyTenants()
    if (mine.status !== 'ok' && mine.status !== 'stale') return null
    // The chosen tenant already answered 404. With no choice the API applied
    // the caller's default, which /v1/tenants/me names and this does not
    // read; asking it again under its own header costs one read and is safe.
    const others = mine.data.filter((t) => t.tenant_id !== chosen).slice(0, ELSEWHERE_PROBE_CAP)
    const holders: TenantChoice[] = []
    for (const t of others) {
      const r = await read<unknown>(route('/v1/tasks/{id}', { id: taskId }), () => false, { tenant: t.tenant_id, frame: true })
      if (r.status === 'ok') holders.push(t)
    }
    return holders.length === 1 ? (holders[0] ?? null) : null
  })()
  elsewhereKept.set(key, { at: Date.now(), answer })
  return answer
}

/**
 * A task read's failure, with `elsewhere` set when it was a 404 and one other
 * tenant of the caller's holds the task (`taskElsewhere`). Every other failure
 * is returned as it came.
 */
async function withElsewhere(taskId: string, failed: { status: 'error'; error: ApiError }): Promise<{ status: 'error'; error: ApiError }> {
  if (failed.error.kind !== 'not_found') return failed
  const found = await taskElsewhere(taskId)
  if (found === null) return failed
  return { ...failed, error: { ...failed.error, elsewhere: { tenant_id: found.tenant_id, display_name: found.display_name } } }
}

export async function loadAgentDetail(taskId: string): Promise<Result<AgentDetail>> {
  if (USE_FIXTURES) return fixtureAgentDetail(taskId)

  const id = { id: taskId }
  const [task, events] = await Promise.all([
    read<{ task: Task } | Task>(route('/v1/tasks/{id}', id), () => false),
    read<{ events: TaskEvent[] }>(route(`/v1/tasks/{id}/events?limit=${EVENT_PAGE_LIMIT}`, id), () => false),
  ])

  if (task.status === 'loading') return task
  // A 404 asks the caller's other tenants whether one holds it (#1194 c15).
  if (task.status === 'error') return withElsewhere(taskId, task)
  if (task.status === 'empty') {
    return {
      status: 'error',
      error: {
        kind: 'not_found',
        httpStatus: 404,
        code: 'not_found',
        message: 'This agent does not exist, or it belongs to another tenant.',
      },
    }
  }

  // GET /v1/tasks/{id} returns {"task": {...}} (routes/tasks.py), NOT the bare
  // document -- verified against the live API. The fallback stays because the
  // create and cancel routes return the same wrapper and a caller could route
  // through either, but the wrapper is the norm rather than the exception.
  const raw = task.data as { task?: Task } & Task
  const unwrapped: Task = raw.task ?? raw

  const eventList =
    events.status === 'ok' ? events.data.events
    : events.status === 'empty' ? []
    : null

  return {
    status: task.status === 'stale' ? 'stale' : 'ok',
    data: {
      task: unwrapped,
      events: eventList,
      eventsDetail:
        eventList === null
          ? events.status === 'error' || events.status === 'stale'
            ? events.error.message
            : 'The event read did not complete.'
          : null,
    },
    fetchedAt: task.fetchedAt,
    error: task.status === 'stale' ? task.error : undefined,
  } as Result<AgentDetail>
}

/**
 * `_publish`'s return value, for the strategy this task actually chose.
 *
 * The three shapes are the worker's, not invented ones, INCLUDING the reason
 * strings -- those are what `PublishOutcome` falls back to matching when a task
 * carries no dispatch block, so a paraphrase here would exercise the fallback
 * against text production never produces.
 */
function fixtureGit(task: Task): Record<string, unknown> {
  const common = {
    base: 'd41f0c9a7b',
    commits: [
      {
        sha: '9b1c7f00aa', subject: 'Harden the destroy guard against an unreadable cluster',
        author: 'agent', committed_at: new Date(FIXTURE_NOW).toISOString(),
        files_changed: 3, insertions: 61, deletions: 12, binary_files: 0,
      },
    ],
    commit_count: 1, insertions: 61, deletions: 12,
    dirty: [], patch: 'diff.patch', patch_bytes: 91233,
    repository: 'example-org/agent-swarm-infra',
  }
  const d = task.dispatch
  if (d?.strategy === 'collect' || d === undefined || d === null) {
    return {
      ...common,
      strategy: 'collect',
      published: false,
      publish_reason:
        "strategy is 'collect': the patch is harvested and nothing is pushed. " +
        "Submit with strategy 'direct-pr' to open a pull request from this agent",
    }
  }
  if (d.role === 'contributor') {
    return {
      ...common,
      role: 'contributor',
      default_branch: 'main', can_push: true,
      branch: `swarm/${task.id}`, pushed_head: '9b1c7f00aa', auto_committed: false,
      published: true,
      publish_reason:
        "strategy is 'integrate' and this step is a contributor: its branch was " +
        'pushed and no pull request was opened. The integrator step merges this ' +
        'branch and opens the single pull request for the whole workflow.',
    }
  }
  return {
    ...common,
    role: d.role,
    default_branch: 'main', can_push: true,
    branch: `swarm/${task.id}`, pushed_head: '9b1c7f00aa', auto_committed: false,
    published: true,
    pull_request: { number: 412, url: 'https://github.com/example-org/agent-swarm-infra/pull/412', state: 'open', created: true },
    ...(d.role === 'integrator'
      ? { integrated: { merged: d.integrates, conflicted: [], missing: [], complete: true } }
      : {}),
  }
}

async function fixtureAgentDetail(taskId: string): Promise<Result<AgentDetail>> {
  const page = await fixtureTasks()
  const listed = page.status === 'ok' ? page.data.tasks : []
  // THE WAITING FIXTURES (#1782) and THE BUDGET ONES (#2459) are reached by
  // id and kept off the list, so every count the list's fixtures pin stays
  // where it was.
  const task = listed.find((t) => t.id === taskId) ?? fixtureOffList(listed, taskId)
  if (!task) {
    return {
      status: 'error',
      error: {
        kind: 'not_found',
        httpStatus: 404,
        code: 'not_found',
        message: 'This agent does not exist, or it belongs to another tenant.',
      },
    }
  }
  // THE NEWEST ATTEMPT `fixtureAttempts` SERVES, and every event's time
  // inside its span (QAFIX4-FIXTURE-B F14a, run 4 task-003): events minted at
  // fixed minutes put attempt 3 running at 11:52 under a task submitted at
  // 11:56. The id is the attempts read's own: a differently-spelled one landed
  // every event under "an attempt no document describes", and the
  // per-attempt joins -- checkpoint sizes, live heartbeat readings, events
  // grouped under their attempt -- were never seen working.
  const spans = fixtureSpans(task)
  const span = spans[spans.length - 1]
  const prior = spans[spans.length - 2]
  const attemptId = span === undefined ? null : fixtureAttemptId(task.id, span.n)
  const usage = fixtureUsage(task)
  // `event_id` is a COUNTER, not the type: two heartbeats in one attempt are
  // ordinary and a type-derived id made React render duplicate keys the moment
  // this fixture grew a second one.
  let seq = 0
  const ev = (type: string, t: number, detail: Record<string, unknown> | null = null) => ({
    event_id: `${task.id}-${++seq}-${type}`,
    task_id: task.id,
    type,
    at: new Date(Math.round(t)).toISOString(),
    attempt_id: attemptId,
    lease_id: task.current_lease_id ?? `lease_${span?.n ?? 0}`,
    // The generation of the attempt these events belong to -- not the task's
    // attempt counter, which made the timeline claim gen 1 under attempt 3.
    generation: span?.n ?? 0,
    detail,
  })
  const events = []
  // THE SUBMITTED EVENT IS WRITTEN WITH THE TASK (QA1 overview-agents-58), so
  // a task nothing has admitted still has one: without it a READY agent's
  // Progress drew the events read as answering with none.
  if (span === undefined) events.push(ev('submitted', Date.parse(task.created_at)))
  if (span !== undefined) {
    // Admission to start: a few seconds each, never back past the end of the
    // attempt before it, or the submission (`fixtureSpans` leaves a gap).
    const begin = span.started ?? span.admitted
    const floor = prior?.ended ?? prior?.admitted ?? Date.parse(task.created_at)
    const lead = Math.min(40_000, (begin - floor) * 0.8)
    events.push(ev('ready', begin - lead))
    events.push(ev('lease_acquired', begin - lead * 0.75))
    const end = span.ended ?? Date.parse(task.updated_at)
    if (task.state !== 'LEASED') {
      // The DISPATCHED event's detail carries the execution name: the
      // workaround from before the attempts read existed.
      events.push(ev('dispatched', span.started === null ? begin + (end - begin) * 0.3 : begin - lead * 0.5, {
        execution_name: `swarm-job-u-bogdan-claude-code-${span.n}`, backend: 'CLOUD_RUN_JOB',
      }))
    }
    if (span.started !== null) {
      events.push(ev('starting', begin - lead * 0.25))
      if (task.state !== 'STARTING') {
        const ran = end - begin
        events.push(ev('running', begin))
        // Every fifth heartbeat carries a live resource reading
        // (lifecycle._heartbeat). It is the ONLY per-attempt measurement a
        // running agent has: `peak_rss_bytes` on the attempt document is
        // written at the end.
        events.push(ev('heartbeat', begin + ran * 0.3, { elapsed_seconds: Math.round(ran * 0.3) / 1000, peak_rss_bytes: 1_610_612_736, checkpoints: 0 }))
        // The worker writes all four keys (control.record_checkpoint). The
        // attempt document stores only the id, so the size and the uri exist
        // HERE and nowhere else.
        const prefix = fixtureCheckpointPrefix(task.id)
        events.push(ev('checkpoint_completed', begin + ran * 0.5, {
          checkpoint_id: 'ckpt_0001',
          uri: `gs://swarm-workspaces/${prefix}/ckpt_0001`,
          size_bytes: 9_102_336,
          seq: 1,
        }))
        events.push(ev('heartbeat', begin + ran * 0.7, { elapsed_seconds: Math.round(ran * 0.7) / 1000, peak_rss_bytes: 1_842_000_000, checkpoints: 1 }))
        // The budget agents' 80% warning (#2459): the live one's, and the
        // stopped one's on its way to the stop.
        if (task.id === FIXTURE_BUDGET_WARN || task.id === FIXTURE_BUDGET_EXCEEDED) {
          events.push(ev('running', begin + ran * 0.8, FIXTURE_BUDGET_WARNING))
        }
        // Deliberately NO matching event for ckpt_0002, which the attempt
        // lists: that is the real paging case -- an id with no size and no
        // uri, which must render as "the event is off this page", not as a
        // blank cell.
        if (task.state === 'SUCCEEDED') events.push(ev('succeeded', end, { exit_code: 0 }))
        if (task.state === 'FAILED') events.push(ev('failed', end, { exit_code: fixtureFailedExitCode(task.last_error), error: task.last_error }))
      }
    }
  }
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      task: {
        ...task,
        result_summary:
          task.state === 'SUCCEEDED'
            ? {
                artifacts: [
                  { name: 'report.md', bytes: 8241, uri: 'gs://swarm-artifacts-dev/u-bogdan/report.md' },
                  { name: 'diff.patch', bytes: 91233, uri: 'gs://swarm-artifacts-dev/u-bogdan/diff.patch' },
                ],
                artifact_bytes: 99474,
                // The git block the worker's `_publish` writes, DERIVED FROM
                // THIS TASK'S OWN STRATEGY rather than fixed. The publish
                // outcome and the dispatch are two renderings of one decision,
                // and a fixture that let them disagree would make the screen
                // look right while showing an impossible run.
                git: fixtureGit(task),
                logs: {
                  stdout: `gs://swarm-logs/tenants/u-bogdan/tasks/${task.id}/attempts/${attemptId}/logs/final/stdout.log`,
                  stderr: `gs://swarm-logs/tenants/u-bogdan/tasks/${task.id}/attempts/${attemptId}/logs/final/stderr.log`,
                },
                // The runner's own report of the newest attempt: the figures
                // its attempt document carries (`fixtureUsage`), not others.
                ...(usage === null
                  ? {}
                  : {
                      runner: {
                        usage: {
                          input_tokens: usage.input_tokens,
                          output_tokens: usage.output_tokens,
                          total_cost_usd: usage.cost_usd,
                          models: ['claude-opus-5'],
                        },
                      },
                    }),
              }
            : task.result_summary,
      },
      events,
      eventsDetail: null,
    },
  }
}

/**
 * The resource-class catalogue: what a class is GIVEN.
 *
 * `GET /v1/resource-classes` serves `RESOURCE_CLASSES` from the frozen
 * contract. Added for exactly one screen -- the agent run detail, which
 * renders peak RSS and peak disk against the ceiling they were measured
 * under -- and it is a route rather than a table because a served value
 * cannot drift at all. (`check-contract-parity.sh` gained a TypeScript section
 * and now asserts the copies this client does keep; a copy of these five
 * numbers would still be a copy, edited in two places every time a class is
 * resized. The route is the better answer, not merely the safer one.)
 *
 * There is no empty case: the catalogue always has three classes, so a 200
 * with an empty object is a failure wearing a success code, not a platform
 * with no sizes.
 */
export type ResourceClasses = Record<string, ResourceClassSpec>

export async function loadResourceClasses(): Promise<Result<{ resource_classes: ResourceClasses }>> {
  if (USE_FIXTURES) return fixtureResourceClasses()
  return read<{ resource_classes: ResourceClasses }>(
    route('/v1/resource-classes'),
    (d) => Object.keys(d.resource_classes ?? {}).length === 0,
  )
}

/**
 * A task's checkpoints, across every attempt.
 *
 * `GET /v1/tasks/{id}/checkpoints`. Newest attempt first, newest checkpoint
 * first within an attempt. That is a reading order, not a resume order: a
 * resume restores only the checkpoint `task.latest_checkpoint` names (the
 * page's `latest_checkpoint`, and the row with `is_latest_pointer`), or
 * nothing -- there is no fallback to the newest one listed (#347).
 *
 * NO EMPTY PREDICATE IS PASSED, deliberately. `checkpoints: []` with
 * `listed: true` is a real answer -- this task has written none -- and the
 * server never returns it for a listing that failed: a failed listing is a
 * 503, which `read` classifies as an error. Collapsing the two into `empty`
 * here would throw away the distinction the endpoint was built to keep.
 *
 * Per-checkpoint failures survive INSIDE the rows: `manifest: 'unreadable'`
 * with `resumable: null` is a checkpoint nothing could be established about,
 * and it must not be drawn as one that cannot be resumed.
 */
export async function loadCheckpoints(
  taskId: string,
  options: { attemptId?: string; pageToken?: string; limit?: number } = {},
): Promise<Result<CheckpointsPage>> {
  if (USE_FIXTURES) return fixtureCheckpoints(taskId)
  const query = new URLSearchParams()
  if (options.attemptId) query.set('attempt_id', options.attemptId)
  if (options.pageToken) query.set('page_token', options.pageToken)
  if (options.limit) query.set('limit', String(options.limit))
  // The path is a literal of its own and the query is passed beside it,
  // rather than interpolated into the same template. That is not style: the
  // UI/API seam test in test_runtimes_screen.py scans this file for versioned
  // path literals and compares their SHAPE against the router's declarations,
  // and a trailing `${suffix}` inside the template normalises to a path
  // segment the API does not serve. Keeping them apart keeps the seam
  // checkable -- and `route()` keys the registry by the literal (CH-18).
  return read<CheckpointsPage>(route('/v1/tasks/{id}/checkpoints', { id: taskId }, query), () => false)
}

/**
 * A window of an attempt's stdout and stderr, already redacted by the server.
 *
 * `GET /v1/tasks/{id}/logs`. The API applies credential redaction at READ
 * time, unconditionally, because the worker's own pass replaces registered
 * literal values only and is skipped entirely for a run that registered none.
 * NOTHING IN THIS CLIENT MAY UNDO THAT, and nothing may render `content`
 * without reading `status` first: `null` means absent or unreadable, and `''`
 * means an object that exists and is empty.
 *
 * Paging is by RAW BYTE OFFSET (`next_offset`), not by line and not by the
 * length of the text returned -- redaction makes the text shorter than the
 * bytes it came from. Windows are aligned to whitespace by the server so a
 * credential can never be split across two of them.
 *
 * `stream` IS REPEATABLE (#184), and a list is sent as one `stream=` per
 * name, in order. No stream means `[stdout, stderr]` -- the RUNNER process's
 * streams, exactly as before -- so every existing caller is unchanged.
 * `agent_stdout` and `agent_stderr` are the agent CLI's own.
 */
export async function loadTaskLogs(
  taskId: string,
  options: {
    attemptId?: string
    stream?: LogStreamName | readonly LogStreamName[]
    source?: 'auto' | 'final' | 'live'
    offset?: number
    limitBytes?: number
  } = {},
): Promise<Result<TaskLogs>> {
  if (USE_FIXTURES) return fixtureTaskLogs(taskId, options.stream)
  const query = new URLSearchParams()
  if (options.attemptId) query.set('attempt_id', options.attemptId)
  const streams: readonly LogStreamName[] =
    options.stream === undefined ? [] : typeof options.stream === 'string' ? [options.stream] : options.stream
  for (const s of streams) query.append('stream', s)
  if (options.source) query.set('source', options.source)
  if (options.offset !== undefined) query.set('offset', String(options.offset))
  if (options.limitBytes !== undefined) query.set('limit_bytes', String(options.limitBytes))
  // Path literal and query kept apart; see loadCheckpoints above for why.
  return read<TaskLogs>(route('/v1/tasks/{id}/logs', { id: taskId }, query), () => false)
}

/**
 * THE CHECKPOINT FIXTURE EXERCISES THE ABSENCES, not the happy path.
 *
 * One checkpoint whose manifest is present and resumable, and one whose
 * manifest is `unreadable` with `resumable: null` -- the row that must not
 * render as "cannot be resumed". A fixture that only ever produced good rows
 * would let `resumable ?? false` ship looking correct in development, which is
 * exactly how the absent-measurement bugs in this product got in.
 */
async function fixtureCheckpoints(taskId: string): Promise<Result<CheckpointsPage>> {
  await new Promise((r) => setTimeout(r, 90))
  noteFixtureProbe(route('/v1/tasks/{id}/checkpoints', { id: taskId }), 90, true)
  const prefix = fixtureCheckpointPrefix(taskId)
  // THE CHECKPOINTS ITS ATTEMPTS WROTE, under the attempt that wrote them
  // (QAFIX4-FIXTURE-B F12, run 4 task-004): the listing named attempts `att_2`
  // and `att_1`, which the attempts read never served -- one of them never
  // started -- so '2 written, 2 kept' sat over a paragraph calling both
  // written checkpoints reclaimed. Written is kept here; a reclaimed one is a
  // component test's case (checkpoint.reclaimed.test.tsx), not this fixture's.
  const task = fixtureTaskDoc(taskId)
  const span = task === undefined ? undefined : fixtureNewestSpan(task)
  const written = span?.checkpoints ?? []
  const iso = (t: number | null | undefined) => (t === null || t === undefined ? null : new Date(t).toISOString())
  const begin = span?.started ?? 0
  const end = span?.ended ?? (task === undefined ? begin : Date.parse(task.updated_at))
  const record = (checkpointId: string): CheckpointRecord => {
    const base = {
      checkpoint_id: checkpointId,
      attempt_id: fixtureAttemptId(taskId, span?.n ?? 0),
      attempt_known: true,
      attempt_created_at: iso(span?.admitted),
      attempt_completed_at: iso(span?.ended),
      prefix: `${prefix}/${checkpointId}`,
      uri: `gs://swarm-workspaces/${prefix}/${checkpointId}`,
      is_latest_pointer: checkpointId === written[written.length - 1],
      generation: span?.n ?? 0,
    }
    if (checkpointId === 'ckpt_0002') {
      return {
        ...base,
        objects: [
          { name: 'manifest.json', key: `${prefix}/ckpt_0002/manifest.json`, bytes: 412 },
          { name: 'workspace.tar.zst', key: `${prefix}/ckpt_0002/workspace.tar.zst`, bytes: 18_442_240 },
        ],
        stored_bytes: 18_442_652,
        manifest: 'present',
        manifest_detail: null,
        created_at: iso(begin + (end - begin) * 0.8),
        seq: 2,
        label: 'periodic',
        archive_bytes: 18_442_240,
        archive_sha256: '3b1f…c7a2',
        file_count: 184,
        resumable: true,
        resumable_detail: null,
      }
    }
    // THE ABSENCE: its manifest is unreadable, so `resumable` is NULL, NOT
    // FALSE. "cannot tell" and "cannot resume" are different sentences and
    // the row renders them differently.
    return {
      ...base,
      objects: [
        { name: 'workspace.tar.zst', key: `${prefix}/${checkpointId}/workspace.tar.zst`, bytes: 9_102_336 },
      ],
      stored_bytes: 9_102_336,
      manifest: 'unreadable',
      manifest_detail: 'The object is there and did not parse as JSON.',
      created_at: null,
      seq: null,
      label: null,
      archive_bytes: null,
      archive_sha256: null,
      file_count: null,
      resumable: null,
      resumable_detail: 'Nothing could be established without the manifest.',
    }
  }
  const checkpoints = [...written].reverse().map(record)
  const pointer = written[written.length - 1]
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      task_id: taskId,
      tenant_id: 'u-bogdan',
      prefix,
      count: checkpoints.length,
      total_found: checkpoints.length,
      next_page_token: null,
      listed: true,
      truncated: false,
      latest_checkpoint:
        pointer === undefined
          ? { pointer: null, status: 'unset', checkpoint_id: null }
          : { pointer: `${prefix}/${pointer}`, status: 'present', checkpoint_id: pointer },
      checkpoints,
    },
  }
}

/**
 * THE ATTEMPT A FIXTURE LOG, TRANSCRIPT OR ANSWER IS READ FROM: the task's
 * newest, as the routes pick it with no `attempt_id` (QAFIX4-FIXTURE-B F14b,
 * run 4 flow-find-failure D7). They named `att_2` or `att_fixture` for every
 * task, under a source line for the task's own newest attempt. A task never
 * admitted has none yet.
 */
function fixtureLogAttempt(task: Task | undefined): { attemptId: string | null; attempt: LogAttempt; span: FixtureAttemptSpan | undefined } {
  const span = task === undefined ? undefined : fixtureNewestSpan(task)
  if (task === undefined || span === undefined) {
    return {
      attemptId: null,
      attempt: { status: 'no_attempt_yet', known: false, generation: null, created_at: null, completed_at: null, exit_code: null },
      span: undefined,
    }
  }
  return {
    attemptId: fixtureAttemptId(task.id, span.n),
    attempt: {
      status: 'latest',
      known: true,
      generation: span.n,
      created_at: new Date(span.admitted).toISOString(),
      completed_at: span.ended === null ? null : new Date(span.ended).toISOString(),
      exit_code: span.exit,
    },
    span,
  }
}

/**
 * How a fixture task's agent CLI ended, as its transcript's last `result`
 * says it: success for a task that succeeded, its error for one that failed,
 * and nothing for one stopped before the CLI finished (cancelled, parked, or
 * still running). ONE FAILED TASK ENDS IN THE CLI'S OWN SUCCESS, `task_8e33a3de`
 * (QA-115, kept as built): the CLI finished and the task failed after it, the
 * case Logs explains. Every other failed task's transcript ends in its error
 * (run 4 flow-find-failure D6), where they all used to end 'Summary: Done.'.
 */
function fixtureCliResult(task: Task): { subtype: string; is_error: boolean; text: string } | null {
  if (task.state === 'SUCCEEDED' || (task.state === 'FAILED' && task.id === 'task_8e33a3de')) {
    return { subtype: 'success', is_error: false, text: '## Summary\n\nDone.' }
  }
  if (task.state === 'FAILED') {
    return { subtype: 'error_during_execution', is_error: true, text: task.last_error ?? 'The agent stopped on an error.' }
  }
  return null
}

/**
 * THE LOG FIXTURE CARRIES ALL THREE CONTENT STATES.
 *
 * stdout has text; stderr exists and is EMPTY (`''`, which is a real answer
 * about a quiet agent and must not render as a failed read). The distinction
 * between `null` and `''` is the whole reason `LogStream.content` is nullable,
 * and a fixture that never produced `''` would leave the branch that renders
 * it unexercised. An attempt that has not started has no log object yet:
 * `absent`, the third state.
 *
 * THE TASK'S OWN NEWEST ATTEMPT (`fixtureLogAttempt`): its id in every key
 * and uri, its times in the lines, a LIVE tail while it runs and the final
 * object once it has ended, and its own end in the last line.
 *
 * THE AGENT STREAMS (#184) are answered only when asked for by name, as the
 * route does: `agent_stdout` is NDJSON ending as its transcript ends, and
 * `agent_stderr` is a measured empty object -- the reference task's shape.
 */
async function fixtureTaskLogs(
  taskId: string,
  asked?: LogStreamName | readonly LogStreamName[],
): Promise<Result<TaskLogs>> {
  await new Promise((r) => setTimeout(r, 110))
  noteFixtureProbe(route('/v1/tasks/{id}/logs', { id: taskId }), 110, true)
  const task = fixtureTaskDoc(taskId)
  const { attemptId, attempt, span } = fixtureLogAttempt(task)
  const clock = (t: number) => new Date(Math.round(t)).toISOString().slice(11, 19)
  const started = span?.started ?? null
  const ended = span?.ended ?? null
  const source = ended === null ? 'live' : 'final'
  const last = ended ?? (task === undefined ? FIXTURE_NOW : Date.parse(task.updated_at))
  const wrote = span?.checkpoints.includes('ckpt_0002') === true
  const body =
    started === null || task === undefined
      ? ''
      : [
          `[${clock(started)}] cloning ${task.repository_url ?? 'https://github.com/example-org/example.git'}`,
          `[${clock(started + 8_000)}] workspace ready at /workspace (tmpfs)`,
          `[${clock(started + 8_000)}] ${task.runner_profile}: starting`,
          ...(wrote ? [`[${clock(started + (last - started) * 0.8)}] checkpoint ckpt_0002 written (18.4 MB)`] : []),
          ...(ended === null
            ? []
            : [`[${clock(ended)}] ${span?.exit ? `exited ${span.exit}: ${task.last_error ?? span.error ?? ''}`.trim() : 'exited 0'}`]),
        ].join('\n')
  const result = task === undefined ? null : fixtureCliResult(task)
  const agentOut =
    started === null
      ? ''
      : [
          '{"type":"system","subtype":"init","model":"claude-opus-5"}',
          '{"type":"assistant","message":{"content":[{"type":"text","text":"Reading the repository."}]}}',
          ...(result === null || ended === null ? [] : [JSON.stringify({ type: 'result', subtype: result.subtype, is_error: result.is_error, result: result.text })]),
        ].join('\n')
  const names: readonly LogStreamName[] =
    asked === undefined ? ['stdout', 'stderr'] : typeof asked === 'string' ? [asked] : asked
  const attemptPrefix = `tenants/u-bogdan/tasks/${taskId}/attempts/${attemptId ?? 'none'}/logs/${source}`
  const pick = (n: LogStreamName): LogStream => {
    const content = n === 'agent_stdout' ? agentOut : n === 'stdout' ? body : ''
    const size = new TextEncoder().encode(content).length
    if (started === null) {
      return {
        stream: n, source: null, status: 'absent',
        detail: attemptId === null ? 'No attempt has been admitted yet.' : 'The attempt has not started, so it has written no log.',
        key: null, uri: null, content: null, total_bytes: null, offset: 0, returned_bytes: 0, next_offset: null,
        truncated: false, redacted: false, redaction_count: 0, tail_window: null,
      }
    }
    const key = `${attemptPrefix}/${n}${source === 'live' ? '.tail' : ''}.log`
    return {
      stream: n,
      source,
      status: 'ok',
      detail: null,
      key,
      uri: `gs://swarm-logs/${key}`,
      content,
      total_bytes: size,
      offset: 0,
      returned_bytes: size,
      next_offset: null,
      truncated: false,
      redacted: false,
      redaction_count: 0,
      tail_window: source === 'live' && size > 0 ? { object_offset: 0, stream_size: size, published_at: new Date(last - 3_000).toISOString() } : null,
      object_updated_at: new Date(source === 'live' ? last - 3_000 : last).toISOString(),
      // AGED AGAINST THE BROWSER'S CLOCK (QA2 overview-agents-39): the console
      // adds the time since the read to `age_seconds` and ages an attempt's end
      // by its own clock, so an age measured at FIXTURE_NOW read `34m ago` on
      // an object whose attempt the picker said ended days ago.
      age_seconds: Math.max(0, Math.round((Date.now() - (source === 'live' ? last - 3_000 : last)) / 1000)),
    }
  }
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      task_id: taskId,
      tenant_id: 'u-bogdan',
      attempt_id: attemptId,
      attempt,
      read_at: new Date(FIXTURE_NOW).toISOString(),
      prefix: `tenants/u-bogdan/tasks/${taskId}/attempts/${attemptId ?? 'none'}/`,
      redaction: { applied_at_read_time: true, rules: 6 },
      streams: names.map(pick),
    },
  }
}

/**
 * ONE artifact's content, resolved by the SERVER from the task's manifest.
 *
 * `GET /v1/tasks/{id}/artifacts/content?name=`. The parameter is a NAME, not a
 * path: the server matches it for exact equality against the manifest the
 * worker wrote into this task's own `result_summary` and rebuilds the object
 * key from the task document. Nothing in this client may construct a key, a
 * prefix or a `gs://` uri and send it -- a route that accepted one would be a
 * path-traversal hole into another tenant's prefix, and the reason it does not
 * is that no caller can express one.
 *
 * `status` MUST be read before `content`. `null` is absent, unreadable or
 * binary; `''` is a real, empty artifact. And `truncated` must be surfaced: a
 * capped window rendered without it reads as the whole output, which is the
 * thing the server went out of its way to make visible.
 */
export async function loadArtifactContent(
  taskId: string,
  name: string,
  options: { offset?: number; limitBytes?: number } = {},
): Promise<Result<ArtifactContent>> {
  if (USE_FIXTURES) return fixtureArtifactContent(taskId, name)
  const query = new URLSearchParams({ name })
  if (options.offset !== undefined) query.set('offset', String(options.offset))
  if (options.limitBytes !== undefined) query.set('limit_bytes', String(options.limitBytes))
  // Path literal and query kept apart; see loadCheckpoints above for why.
  return read<ArtifactContent>(route('/v1/tasks/{id}/artifacts/content', { id: taskId }, query), () => false)
}

// ---------------------------------------------------------------------------
// The Artifacts tab (#184): what a task took in and what it produced
// ---------------------------------------------------------------------------
//
// EVERY READ AND EVERY DOWNLOAD GOES THROUGH THE API, never a signed GCS URL
// (the owner's decision, 2026-09-25), so read-time credential redaction and
// the tenant check apply to every byte. Nothing here builds an object key, a
// prefix or a `gs://` uri; a file is named by the NAME its manifest spells,
// on the task that owns it -- a staged input by the UPSTREAM task's id.

/**
 * One task document, on its own: `GET /v1/tasks/{id}`. The Artifacts pane
 * re-reads it every 5 s while the task runs, and the events, attempts and
 * catalogue `loadAgentRun` adds are nothing that pane draws.
 */
export async function loadTask(taskId: string): Promise<Result<Task>> {
  if (USE_FIXTURES) {
    const detail = await fixtureAgentDetail(taskId)
    return detail.status === 'ok' ? { status: 'ok', data: detail.data.task, fetchedAt: detail.fetchedAt } : (detail as Result<Task>)
  }
  const got = await read<{ task: Task } | Task>(route('/v1/tasks/{id}', { id: taskId }), () => false)
  if (got.status === 'loading' || got.status === 'error') return got
  if (got.status === 'empty') {
    return {
      status: 'error',
      error: {
        kind: 'not_found',
        httpStatus: 404,
        code: 'not_found',
        message: 'This agent does not exist, or it belongs to another tenant.',
      },
    }
  }
  // The route wraps the document in `{task}`; see loadAgentDetail.
  const raw = got.data as { task?: Task } & Task
  const task: Task = raw.task ?? raw
  return got.status === 'stale'
    ? { status: 'stale', data: task, fetchedAt: got.fetchedAt, error: got.error }
    : { status: 'ok', data: task, fetchedAt: got.fetchedAt, serverAt: got.serverAt }
}

/**
 * ONE PARENT'S CHILDREN (D15): `GET /v1/tasks?parent_task_id=`, the list
 * route's filter that docs/design/child-tasks.md §6.3 names as the only source
 * a tree may be drawn from. NOT ON MAIN'S API YET, and an API that does not
 * know the filter IGNORES it and answers the tenant's newest tasks -- so this
 * read is never trusted as a child list on its own: `childrenOf`
 * (AgentChildren.tsx) keeps a row only when it names this parent, and calls
 * the read "not served" when any row does not carry the field at all.
 *
 * No empty predicate: `tasks: []` from an API that honours the filter is a
 * real zero, and the screen decides that, not this read.
 */
export async function loadChildren(taskId: string): Promise<Result<TaskPage>> {
  if (USE_FIXTURES) {
    await new Promise((r) => setTimeout(r, 60))
    noteFixtureProbe(route('/v1/tasks', {}, new URLSearchParams({ parent_task_id: taskId })), 60, true)
    return { status: 'ok', fetchedAt: Date.now(), data: { tasks: [], next_page_token: null, tenant_id: 'u-bogdan' } }
  }
  return read<TaskPage>(
    route('/v1/tasks', {}, new URLSearchParams({ parent_task_id: taskId, limit: String(TASK_PAGE_LIMIT) })),
    () => false,
  )
}

/**
 * HOW MANY ENTRIES THE ARTIFACTS PANE ASKS THE LISTING FOR: 200, which is
 * `max_page_size`, the server's own cap.
 *
 * WITHOUT IT THE LISTING WAS 50. The artifacts route goes through
 * `paged_limit` (routes/tasks.py) like every list route, so a request with no
 * `limit` got `default_page_size`. `store.list_artifacts` then returned
 * `manifest.artifacts[:50]` with `complete: true` and nothing saying it had
 * cut the list. A browser run that took 60 screenshots drew 50 rows and a
 * chip reading 50. The route returns no `next_page_token` and accepts none, so
 * one read goes no further than this. The pane lists anything past it from
 * the task's own manifest, which is the record the route serves, and it says
 * how many the route listed (Artifacts.tsx `Files`).
 *
 * This is its own constant, not `ATTEMPT_PAGE_LIMIT`, for the reason that one
 * gives: the two routes share a cap today, and nothing makes them share one.
 * A deployment that lowered `MAX_PAGE_SIZE` would clamp this read further.
 * The pane still catches that, because it counts against the manifest and
 * not against this number.
 */
export const ARTIFACT_PAGE_LIMIT = 200

/**
 * The artifact LISTING: `GET /v1/tasks/{id}/artifacts`. The manifest from the
 * task's own result summary -- no GCS call -- plus, since #184, each file's
 * `kind` and `role` from the server's one name table.
 *
 * No empty predicate: `artifacts: []` with `complete: true` is a real zero,
 * and with `complete: false` it is "uploaded when the attempt ends". The
 * screen tells the two apart by `complete`, which a collapse to `empty`
 * would throw away.
 *
 * `limit` IS SENT, AT `ARTIFACT_PAGE_LIMIT`. See that constant for what not
 * sending it cost.
 */
export async function loadArtifactListing(taskId: string): Promise<Result<ArtifactListing>> {
  if (USE_FIXTURES) return fixtureArtifactListing(taskId)
  return read<ArtifactListing>(
    route('/v1/tasks/{id}/artifacts', { id: taskId }, `limit=${ARTIFACT_PAGE_LIMIT}`),
    () => false,
  )
}

/**
 * THE URL OF ONE ARTIFACT'S BYTES, for an `<img src>`, an `open full` link or
 * a `download` link: `GET /v1/tasks/{id}/artifacts/raw?name=&disposition=`.
 *
 * A URL AND NOT A READ, on purpose. The response is a byte stream, and `read`
 * classifies any non-JSON 2xx as an expired session -- correctly, for a JSON
 * route. The browser fetches this itself, with the same-origin session cookie
 * every read already rides on, so the tenant check and read-time redaction
 * (for text) happen on the server exactly as for the content route. The name
 * is the manifest's, verbatim; the server resolves the object.
 *
 * The browser sends no `X-Swarm-Tenant` on such a fetch, so the chosen tenant
 * rides as `?tenant=` (`tenantQuery`) -- without it every artifact of a task
 * listed under a switched tenant would 404. Computed at render; the shell
 * remounts the page on a switch, so a link never carries a stale choice.
 *
 * `redaction: 'none'` is the OWNER-ONLY RAW PATCH (#1857): the server serves
 * a `.patch` as stored to the task's submitter, always as an attachment, and
 * refuses everyone else 403. Only `ArtifactViewer`'s `RawPatchDownload` asks
 * for it; every other link keeps the default, which is redacted.
 */
export function artifactRawUrl(
  taskId: string,
  name: string,
  disposition: 'inline' | 'attachment',
  options: { redaction?: 'none' } = {},
): string {
  const query = new URLSearchParams({ name, disposition })
  if (options.redaction === 'none') query.set('redaction', 'none')
  return route('/v1/tasks/{id}/artifacts/raw', { id: taskId }, tenantQuery(query)).url
}

/**
 * The agent's final answer: `GET /v1/tasks/{id}/answer`. See `TaskAnswer` for
 * the four statuses; a failed READ of this route is a `Result` error, which is
 * a fifth thing and is drawn as one.
 */
export async function loadAnswer(taskId: string, options: { attemptId?: string } = {}): Promise<Result<TaskAnswer>> {
  if (USE_FIXTURES) return fixtureAnswer(taskId)
  const query = new URLSearchParams()
  if (options.attemptId) query.set('attempt_id', options.attemptId)
  return read<TaskAnswer>(route('/v1/tasks/{id}/answer', { id: taskId }, query), () => false)
}

/** One option of an agent's question, as the worker validated it. */
export interface QuestionOption {
  label: string | null
  description: string | null
}

/** One question an agent asked its owner (`questions.json`). */
export interface AgentQuestion {
  question: string | null
  options: QuestionOption[]
  /** One of the options' labels, or null when the agent recommended none. */
  recommended: string | null
  context: string | null
  /** The owner's stored answer; null or absent when unanswered (or from an API older than answers). */
  answer?: OwnerAnswer | null
}

/**
 * `GET /v1/tasks/{id}/questions`. `status` is `ok`, `none` (asked none --
 * `questions: []`, never a 404) or `unavailable` (the worker counted some
 * and they could not be read; `unavailable_because` says why).
 */
export interface TaskQuestions {
  task_id: string
  count: number
  status: 'ok' | 'none' | 'unavailable'
  questions: AgentQuestion[]
  attempt_id?: string | null
  unavailable_because?: string
  redacted: boolean
  redaction_count: number
  /** Whether this viewer may answer: the task's submitter or an admin. Absent from an API older than answers. */
  may_answer?: boolean
  /** Why not, when `may_answer` is false. */
  may_answer_because?: string | null
}

/** `POST /v1/tasks/{id}/answers`: an option label, free text, or both. */
export interface AnswerChoice {
  question_index: number
  choice: string | null
  text: string | null
  /**
   * Where it was answered (ANSWER-SYNC). The console leaves it out, which the
   * API stores as null; the plugin sends `claude-code` with its session.
   * Neither changes who may answer.
   */
  via?: AnswerVia | null
  session?: string | null
}

/**
 * What the task's agent asked its owner: `GET /v1/tasks/{id}/questions`, the
 * file the worker validated, redacted by the artifact read (part of #1206).
 */
export async function loadQuestions(taskId: string): Promise<Result<TaskQuestions>> {
  if (USE_FIXTURES) {
    noteFixtureProbe(route('/v1/tasks/{id}/questions', { id: taskId }), 0, true)
    return {
      status: 'ok',
      fetchedAt: Date.now(),
      data: fixtureHeldQuestions(taskId) ?? { task_id: taskId, count: 0, status: 'none', questions: [], redacted: false, redaction_count: 0 },
    }
  }
  return read<TaskQuestions>(route('/v1/tasks/{id}/questions', { id: taskId }), () => false)
}

/**
 * The owner answers one of the agent's questions: `POST /v1/tasks/{id}/answers`
 * (TASK-ANSWERS, owner decision 2026-10-13). Stored on the task, where whoever
 * launched the work reads it; answers with the questions as they now read.
 * Re-answering replaces the answer, and the API keeps the replaced one.
 */
export async function answerQuestion(taskId: string, answer: AnswerChoice): Promise<Result<TaskQuestions>> {
  const target = route('/v1/tasks/{id}/answers', { id: taskId })
  if (USE_FIXTURES) {
    noteFixtureProbe(target, 0, false)
    return {
      status: 'error',
      error: { kind: 'not_found', httpStatus: 404, code: null, message: 'The fixture build stores no answers.' },
    }
  }
  return (await write(target, 'POST', answer)) as Result<TaskQuestions>
}

/**
 * THE TASK'S INPUT, MASKED: `GET /v1/tasks/{id}/input` (#184 follow-up). What
 * Inputs and Details draw -- the prompt, the rest of the input and the whole
 * of it, each as the server's redactor left it, with its count. Never
 * `task.input`, which is the document as submitted.
 */
export async function loadTaskInput(taskId: string): Promise<Result<TaskInputCopy>> {
  if (USE_FIXTURES) return fixtureTaskInput(taskId)
  return read<TaskInputCopy>(route('/v1/tasks/{id}/input', { id: taskId }), () => false)
}

/**
 * The copies already read, by task. An input never changes once submitted, so
 * each pane -- Details re-reads with the drawer every 10 s, Inputs with its
 * pane every 5 s -- asks through here, and a copy that was READ is answered
 * from memory without a request. A failed read is not kept: the next poll
 * asks again, which is how a copy that failed once comes back.
 */
const INPUT_COPIES = new Map<string, Result<TaskInputCopy>>()
/** Enough for every drawer a session opens; the oldest goes first past it. */
const INPUT_COPIES_KEPT = 64
/**
 * THE READ IN FLIGHT, by task (PR #210 re-review). Details and Inputs ask for
 * the same copy when a drawer opens, and each poll of a pane whose copy failed
 * asks again: without this, a copy slower than the poll stacked one request
 * per tick, each one more redaction of the whole input on the server. One
 * request per task at a time; every asker shares its answer.
 */
const INPUT_READS = new Map<string, Promise<Result<TaskInputCopy>>>()

export function loadTaskInputOnce(taskId: string): Promise<Result<TaskInputCopy>> {
  const held = INPUT_COPIES.get(taskId)
  if (held !== undefined) return Promise.resolve(held)
  const flying = INPUT_READS.get(taskId)
  if (flying !== undefined) return flying
  const read = loadTaskInput(taskId)
    .then((got) => {
      if (got.status === 'ok') {
        if (INPUT_COPIES.size >= INPUT_COPIES_KEPT) {
          const oldest = INPUT_COPIES.keys().next().value
          if (oldest !== undefined) INPUT_COPIES.delete(oldest)
        }
        INPUT_COPIES.set(taskId, got)
      }
      return got
    })
    .finally(() => {
      INPUT_READS.delete(taskId)
    })
  INPUT_READS.set(taskId, read)
  return read
}

/**
 * The agent's transcript as steps, parsed and redacted by the server:
 * `GET /v1/tasks/{id}/transcript`.
 *
 * With no `offset` the server serves the window `source` chooses: from the
 * start of a final object, or the newest tail of a live one -- which is what
 * the 5 s live read asks for, replacing its list each time. A final
 * transcript is paged from `next_offset`. `includeRaw` adds each step's
 * redacted source record, for `show records`.
 */
export async function loadTranscript(
  taskId: string,
  options: {
    attemptId?: string
    source?: 'auto' | 'final' | 'live'
    offset?: number
    limitBytes?: number
    includeRaw?: boolean
  } = {},
): Promise<Result<TaskTranscript>> {
  if (USE_FIXTURES) return fixtureTranscript(taskId, options.includeRaw === true)
  const query = new URLSearchParams()
  if (options.attemptId) query.set('attempt_id', options.attemptId)
  if (options.source) query.set('source', options.source)
  if (options.offset !== undefined) query.set('offset', String(options.offset))
  if (options.limitBytes !== undefined) query.set('limit_bytes', String(options.limitBytes))
  if (options.includeRaw) query.set('include_raw', 'true')
  return read<TaskTranscript>(route('/v1/tasks/{id}/transcript', { id: taskId }, query), () => false)
}

/**
 * `GET /v1/workflows/{id}`: the workflow, its steps and every step's task.
 * The Artifacts pane reads it only for a task that IS a workflow step, to name
 * the step that produced each staged file and to see whether that upstream
 * run still lists it.
 */
export interface WorkflowRead {
  workflow: Workflow
  tasks: Task[]
}

export async function loadWorkflow(workflowId: string): Promise<Result<WorkflowRead>> {
  if (USE_FIXTURES) return fixtureWorkflowRead(workflowId)
  return read<WorkflowRead>(route('/v1/workflows/{id}', { id: workflowId }), () => false)
}

/**
 * HOW MANY WORKFLOWS ONE `GET /v1/workflows?ids=` NAMES: the route's own cap,
 * `MAX_WORKFLOW_IDS` (routes/workflows.py). A caller with more asks in chunks.
 */
export const WORKFLOW_IDS_PER_READ = 50

/** `GET /v1/workflows?ids=`: the named workflows, their step tasks, and the ids not served. */
interface WorkflowsByIdPage extends WorkflowPage {
  tasks?: Task[]
  missing?: string[]
}

/**
 * The answer to one by-id read. `batched` is false when the API ignored `ids`
 * -- one older than the parameter answers with its newest page instead, with
 * no `missing` -- and then `reads` is empty and nothing may be concluded
 * about the ids from it: the caller reads them one by one.
 */
export interface WorkflowReads {
  batched: boolean
  reads: Map<string, WorkflowRead>
  missing: string[]
}

/**
 * MANY WORKFLOWS IN ONE READ (live QA CH-6, 2026-10-11): the Timeline asked
 * for each workflow its lanes name with its own `GET /v1/workflows/{id}` -- 52
 * of them on one load. `GET /v1/workflows?ids=` serves up to
 * `WORKFLOW_IDS_PER_READ` of them, each with the step tasks the derivation
 * read, in the summary view (no step input: nothing that batches draws one).
 * Each workflow's `tasks` are its steps' tasks, as far as they were read.
 */
export async function loadWorkflowsById(ids: readonly string[]): Promise<Result<WorkflowReads>> {
  if (USE_FIXTURES) {
    const reads = new Map<string, WorkflowRead>()
    for (const id of ids) {
      const r = await fixtureWorkflowRead(id)
      if (r.status === 'ok') reads.set(id, r.data)
    }
    return { status: 'ok', fetchedAt: Date.now(), data: { batched: true, reads, missing: ids.filter((i) => !reads.has(i)) } }
  }
  const query = new URLSearchParams()
  for (const id of ids) query.append('ids', id)
  query.set('view', 'summary')
  const r = await read<WorkflowsByIdPage>(route('/v1/workflows', {}, query), () => false)
  if (r.status !== 'ok' && r.status !== 'stale') return r as Result<WorkflowReads>
  const page = r.data
  const batched = Array.isArray(page.missing) && Array.isArray(page.tasks)
  const reads = new Map<string, WorkflowRead>()
  if (batched) {
    const taskById = new Map((page.tasks ?? []).map((t) => [t.id, t]))
    for (const w of page.workflows) {
      const tasks = w.steps
        .map((st) => (st.task_id ? taskById.get(st.task_id) : undefined))
        .filter((t): t is Task => t !== undefined)
      reads.set(w.workflow_id, { workflow: w, tasks })
    }
  }
  const data: WorkflowReads = { batched, reads, missing: batched ? (page.missing ?? []) : [] }
  return r.status === 'ok'
    ? { status: 'ok', data, fetchedAt: r.fetchedAt, serverAt: r.serverAt }
    : { status: 'stale', data, fetchedAt: r.fetchedAt, error: r.error }
}

// ---------------------------------------------------------------------------
// Issue runs (#454; intake-tenants.html 1A): the preview, the runs, the plan
// ---------------------------------------------------------------------------
//
// The routes are swarm-api's on main (#511): routes/issues.py and
// routes/runs.py. Every refusal comes back as `error` with the server's
// `code`, which the screens branch on (forge.py's five preview codes;
// `plan_changed` for a stale digest) -- never as an empty result.

/**
 * `GET /v1/issues/preview?issue=<ref>`: the issue read with the TENANT's forge
 * credential, before anything is created. Never empty: a 200 is an issue.
 */
export async function loadIssuePreview(issue: string): Promise<Result<IssuePreviewRead>> {
  if (USE_FIXTURES) return fixtureIssuePreview(issue)
  return read<IssuePreviewRead>(route('/v1/issues/preview', {}, new URLSearchParams({ issue })), () => false)
}

/**
 * `GET /v1/runs?view=summary`: the tenant's runs, newest first. Empty is a
 * measured zero.
 *
 * THE SUMMARY VIEW (RT-7, 2026-10-11): the full rows were 1.5 MB for 50 runs,
 * almost all of it plans, open work and per-check lists the list never draws.
 * Both callers -- the Runs list and Submit's recent list -- read only the
 * row's id, issue, title, state, approval, workflow and creator. A run's own
 * page reads `loadRun`, which is always the full row.
 */
export async function loadRuns(
  pageToken: string | null = null,
  limit: number | null = null,
): Promise<Result<IssueRunSummaryPage>> {
  if (USE_FIXTURES) return fixtureRuns(pageToken)
  const query = new URLSearchParams({ view: 'summary' })
  if (pageToken !== null) query.set('page_token', pageToken)
  // `limit` null is the route's own page, as before.
  if (limit !== null) query.set('limit', String(limit))
  return read<IssueRunSummaryPage>(route('/v1/runs', {}, query), (d) => d.runs.length === 0 && d.next_page_token === null)
}

/** `GET /v1/runs/{run_id}`. The read advances the run (routes/runs.py). */
export async function loadRun(runId: string): Promise<Result<IssueRunRead>> {
  if (USE_FIXTURES) return fixtureRun(runId)
  return read<IssueRunRead>(route('/v1/runs/{id}', { id: runId }), () => false)
}

/** `POST /v1/runs`: plan an issue. The body is RunCreate's fields and nothing else. */
export async function createRun(body: RunCreateBody): Promise<Result<IssueRunRead>> {
  if (USE_FIXTURES) return fixtureCreateRun(body)
  return write(route('/v1/runs'), 'POST', body) as Promise<Result<IssueRunRead>>
}

/** Approve THE DIGEST OF THE PLAN SHOWN (D3); a different plan is refused `plan_changed`. */
export async function approvePlan(runId: string, planDigest: string): Promise<Result<IssueRunRead>> {
  if (USE_FIXTURES) return fixturePlanAction(runId, 'approve')
  return write(route('/v1/runs/{id}/plan:approve', { id: runId }), 'POST', { plan_digest: planDigest }) as Promise<
    Result<IssueRunRead>
  >
}

/** Replace the plan, carrying the digest it replaces so two editors cannot overwrite each other. */
export async function editPlan(runId: string, planDigest: string, plan: RunPlan): Promise<Result<IssueRunRead>> {
  if (USE_FIXTURES) return fixturePlanAction(runId, 'edit', plan)
  return write(route('/v1/runs/{id}/plan:edit', { id: runId }), 'POST', { plan_digest: planDigest, plan }) as Promise<
    Result<IssueRunRead>
  >
}

/** Turn the plan down. The shown digest is sent, so a plan edited since is not rejected unseen. */
export async function rejectPlan(runId: string, planDigest: string | null, reason: string): Promise<Result<IssueRunRead>> {
  if (USE_FIXTURES) return fixturePlanAction(runId, 'reject', undefined, reason)
  const body: Record<string, string> = {}
  if (planDigest !== null) body.plan_digest = planDigest
  if (reason.trim() !== '') body.reason = reason.trim()
  return write(route('/v1/runs/{id}/plan:reject', { id: runId }), 'POST', body) as Promise<Result<IssueRunRead>>
}

/**
 * THE DEVELOPMENT FIXTURES FOR RUNS. Example data, in the mock-up's words
 * (intake-tenants.html): one run waiting for approval and one running. Each
 * fixture registers its route as a live call would (CH-18).
 */
const FIXTURE_PLAN: RunPlan = {
  summary: 'Sum the steps\' recorded spend into the workflow read, then draw a cost column with a dash and a reason for unrecorded spend.',
  steps: [
    { step_id: 'api', title: 'api: sum step spend into the workflow read', prompt: 'Add the sum to routes/workflows.py, with tests first.' },
    { step_id: 'ui', title: 'ui: the cost column', prompt: 'Draw the column in Workflows.tsx, a dash and its reason where spend was not recorded.' },
  ],
}

function fixtureIssueRun(id: string, over: Partial<IssueRun> = {}): IssueRun {
  const issue: IssueRefDoc = {
    ref: 'example-org/agent-swarm-infra#512', owner: 'example-org', repo: 'agent-swarm-infra', number: 512,
    url: 'https://github.com/example-org/agent-swarm-infra/issues/512',
    repository_url: 'https://github.com/example-org/agent-swarm-infra',
  }
  const at = new Date(FIXTURE_NOW - 6 * 60_000).toISOString()
  return {
    id, tenant_id: 'u-bogdan', state: 'PLANNED', terminal: false, issue,
    plan_approval: 'required', auto_merge: false, fix_rounds: 3, planner_task_id: 'task_planner_fixture',
    // The digest is computed text in the live API; the fixture's is built, not a literal.
    plan: FIXTURE_PLAN, plan_digest: `sha256:${'9f2c41'.padEnd(64, '0')}`, plan_revision: 1, plan_edited_by: null,
    workflow_id: null, created_by: 'operator@example.com', created_at: at, updated_at: at,
    approved_by: null, approved_at: null, approved_digest: null, rejected_by: null, rejection_reason: null,
    error: null,
    history: [{ at, from: null, to: 'PLANNING', by: 'operator@example.com' }, { at, from: 'PLANNING', to: 'PLANNED', by: 'swarm-api' }],
    // THE ISSUE AS THE RUN READ IT AT SUBMISSION (QA2-FIX-20, runs2-03): the
    // list leads each row with its title, as the live API's runs carry it.
    issue_read: {
      title: "The workflow list shows no cost column, so a finished workflow's spend needs opening every step",
      labels: ['enhancement', 'ui'], state: 'open', comments: 4, url: issue.url,
      body: 'See what a finished workflow cost without opening each step.', body_truncated: false, body_redacted: false, read_at: at,
    },
    issue_read_error: null,
    ...over,
  }
}

/**
 * THE FIXTURE RUNS REMEMBER WHAT WAS DONE TO THEM (QA1-FIX-24, part of
 * #2596): a run planned from Submit is a new run created now (runs-submit-15),
 * and an approval or a rejection is still there on the next read
 * (runs-submit-03), as the live API's would be. Kept for the page's life.
 */
const fixtureRunsMade: IssueRun[] = []
const fixtureRunsDecided = new Map<string, Partial<IssueRun>>()

function fixtureIssueRuns(): IssueRun[] {
  const planned = new Date(FIXTURE_NOW - 40 * 60_000).toISOString()
  const approved = new Date(FIXTURE_NOW - 30 * 60_000).toISOString()
  const created = new Date(FIXTURE_NOW - 50 * 60_000).toISOString()
  return [
    ...fixtureRunsMade,
    fixtureIssueRun('run_4c1e09d2'),
    // The workflow it started is one the workflow fixture holds (QA1-FIX-22, runs-submit-04).
    fixtureIssueRun('run_19ab77e0', {
      state: 'RUNNING', workflow_id: 'wf_audit_01', approved_by: 'operator@example.com',
      approved_at: approved, created_at: created,
      // Every transition to its state (runs-submit-06): the approval and the start too.
      history: [
        { at: created, from: null, to: 'PLANNING', by: 'operator@example.com' },
        { at: planned, from: 'PLANNING', to: 'PLANNED', by: 'swarm-api' },
        { at: approved, from: 'PLANNED', to: 'APPROVED', by: 'operator@example.com' },
        { at: approved, from: 'APPROVED', to: 'RUNNING', by: 'swarm-api' },
      ],
    }),
  ].map((r) => ({ ...r, ...(fixtureRunsDecided.get(r.id) ?? {}) }))
}

/** The issue the fixture reads for `owner/repo#N` or its URL, or null for anything else. */
function fixtureIssueDoc(issue: string): IssuePreviewRead['issue'] | null {
  const m = /([\w.-]+)\/([\w.-]+)(?:#|\/issues\/)(\d+)/.exec(issue)
  if (m === null) return null
  const [, owner, repo, num] = m as unknown as [string, string, string, string]
  const ref = `${owner}/${repo}#${num}`
  const url = `https://github.com/${owner}/${repo}/issues/${num}`
  return {
        ref, owner, repo, number: Number(num), url, repository_url: `https://github.com/${owner}/${repo}`,
        title: 'The workflow list shows no cost column, so a finished workflow\'s spend needs opening every step',
        body: 'What are you trying to do?\nSee what a finished workflow cost without opening each step.\n\nWhat would you like to see?\nA cost column on Work › Workflows, with a dash and a reason when a step\'s spend was not recorded.',
        body_truncated: false, body_redacted: false, labels: ['enhancement', 'ui'], state: 'open', comments: 4,
  }
}

async function fixtureIssuePreview(issue: string): Promise<Result<IssuePreviewRead>> {
  await new Promise((r) => setTimeout(r, 60))
  noteFixtureProbe(route('/v1/issues/preview', {}, new URLSearchParams({ issue })), 60, true)
  const doc = fixtureIssueDoc(issue)
  if (doc === null) {
    return { status: 'error', error: { kind: 'invalid', httpStatus: 422, code: 'validation_failed',
      message: 'issue must name an issue: owner/repo#N or its URL' } }
  }
  return {
    status: 'ok', fetchedAt: Date.now(),
    data: {
      // THE CAPACITY FIXTURE'S TENANT, and the merge step's answer as the live
      // API serves it since contract request 47 (QA1-FIX-12 runs-submit-13,
      // chrome-C29).
      tenant_id: 'u-bogdan',
      auto_merge: { available: true, requires: '#295', reason: null },
      issue: doc,
    },
  }
}

async function fixtureRuns(pageToken: string | null): Promise<Result<IssueRunPage>> {
  await new Promise((r) => setTimeout(r, 60))
  noteFixtureProbe(route('/v1/runs'), 60, true)
  const runs = pageToken === null ? fixtureIssueRuns() : []
  return { status: 'ok', fetchedAt: Date.now(), data: { runs, next_page_token: null, tenant_id: 'u-bogdan' } }
}

async function fixtureRun(runId: string): Promise<Result<IssueRunRead>> {
  await new Promise((r) => setTimeout(r, 60))
  noteFixtureProbe(route('/v1/runs/{id}', { id: runId }), 60, true)
  const run = fixtureIssueRuns().find((r) => r.id === runId)
  if (run === undefined) {
    return { status: 'error', error: { kind: 'not_found', httpStatus: 404, code: 'not_found', message: `run '${runId}' not found` } }
  }
  return { status: 'ok', fetchedAt: Date.now(), data: { run } }
}

async function fixtureCreateRun(body: RunCreateBody): Promise<Result<IssueRunRead>> {
  await new Promise((r) => setTimeout(r, 60))
  noteFixtureProbe(route('/v1/runs'), 60, true)
  const at = new Date().toISOString()
  const id = `run_${Array.from(crypto.getRandomValues(new Uint8Array(4)), (b) => b.toString(16).padStart(2, '0')).join('')}`
  // THE RUN KEEPS THE ISSUE IT WAS PLANNED FROM, AND THE READ (QA2-FIX-07,
  // runs2-06): created after the read, so "older than the issue snapshot"
  // was never true of it. Read as the route reads it, at creation.
  const doc = fixtureIssueDoc(body.issue)
  const issued: Partial<IssueRun> = doc === null ? {} : {
    issue: { ref: doc.ref, owner: doc.owner, repo: doc.repo, number: doc.number, url: doc.url, repository_url: doc.repository_url },
    issue_read: {
      title: doc.title, labels: doc.labels, state: doc.state === 'closed' ? 'closed' : 'open', comments: doc.comments, url: doc.url, body: doc.body,
      body_truncated: doc.body_truncated, body_redacted: doc.body_redacted, read_at: at,
    },
    issue_read_error: null,
  }
  const run = fixtureIssueRun(id, {
    ...issued,
    state: 'PLANNED', plan_approval: body.plan_approval, fix_rounds: body.fix_rounds, auto_merge: body.auto_merge,
    created_at: at, updated_at: at,
    history: [{ at, from: null, to: 'PLANNING', by: 'operator@example.com' }, { at, from: 'PLANNING', to: 'PLANNED', by: 'swarm-api' }],
  })
  fixtureRunsMade.unshift(run)
  return { status: 'ok', fetchedAt: Date.now(), data: { run } }
}

async function fixturePlanAction(runId: string, action: 'approve' | 'edit' | 'reject', plan?: RunPlan, reason = ''): Promise<Result<IssueRunRead>> {
  await new Promise((r) => setTimeout(r, 60))
  // Spelled out per action, not `plan:${action}`: tests/unit/control_plane
  // test_every_v1_path_the_ui_calls_is_served_by_this_api reads these literals
  // and a template hole in the verb matches no route the API declares.
  const planPath = action === 'approve' ? '/v1/runs/{id}/plan:approve'
    : action === 'edit' ? '/v1/runs/{id}/plan:edit' : '/v1/runs/{id}/plan:reject'
  noteFixtureProbe(route(planPath, { id: runId }), 60, true)
  const now = new Date().toISOString()
  const was = fixtureIssueRuns().find((r) => r.id === runId) ?? fixtureIssueRun(runId)
  // AN APPROVED PLAN STARTS A WORKFLOW OF ITS OWN STEPS, just queued
  // (QA2-FIX-07, runs2-24): it linked wf_audit_01, five other steps, two of
  // them succeeded nine days before the approval.
  const started = action === 'approve'
    ? fixtureMakeWorkflow(
        (was.plan ?? FIXTURE_PLAN).steps.map((st, i, all) => ({
          step_id: st.step_id,
          // A plan step with no `depends_on` runs as a chain (PlanStepDoc).
          depends_on: st.depends_on ?? (i === 0 ? [] : [all[i - 1]?.step_id ?? '']),
          runner_profile: 'claude-code',
        })),
        'operator@example.com',
        0,
      )
    : null
  const over: Partial<IssueRun> = action === 'approve'
    ? { state: 'RUNNING', workflow_id: started?.workflow_id ?? null, approved_by: 'operator@example.com', approved_at: now, updated_at: now,
        history: [...was.history, { at: now, from: 'PLANNED', to: 'APPROVED', by: 'operator@example.com' }, { at: now, from: 'APPROVED', to: 'RUNNING', by: 'swarm-api' }] }
    : action === 'reject'
      // The reason typed is kept, trimmed as the live write sends it (QA2-FIX-07, runs2-20).
      ? { state: 'REJECTED', terminal: true, rejected_by: 'operator@example.com', rejection_reason: reason.trim() === '' ? null : reason.trim(), updated_at: now,
          history: [...was.history, { at: now, from: 'PLANNED', to: 'REJECTED', by: 'operator@example.com' }] }
      : { plan: plan ?? FIXTURE_PLAN, plan_revision: 2, plan_edited_by: 'operator@example.com' }
  fixtureRunsDecided.set(runId, { ...(fixtureRunsDecided.get(runId) ?? {}), ...over })
  return { status: 'ok', fetchedAt: Date.now(), data: { run: { ...was, ...over } } }
}

/**
 * THE ARTIFACTS FIXTURES, derived from the task fixture so the joins are real:
 * a finished task lists its files with the kinds and roles the server would
 * give them, and an unfinished one lists nothing, incomplete -- "uploaded when
 * the attempt ends", never "none".
 */
async function fixtureArtifactListing(taskId: string): Promise<Result<ArtifactListing>> {
  await new Promise((r) => setTimeout(r, 40))
  noteFixtureProbe(route('/v1/tasks/{id}/artifacts', { id: taskId }), 40, true)
  const detail = await fixtureAgentDetail(taskId)
  if (detail.status !== 'ok') return detail as Result<ArtifactListing>
  const finished = detail.data.task.state === 'SUCCEEDED'
  // The newest attempt's, as every per-task fixture read names it (F14b).
  const att = fixtureLogAttempt(detail.data.task).attemptId ?? 'none'
  const uri = (name: string) => `gs://swarm-artifacts-dev/tenants/u-bogdan/tasks/${taskId}/attempts/${att}/artifacts/${name}`
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      task_id: taskId,
      complete: finished,
      attempt_id: finished ? att : null,
      artifact_bytes: finished ? 118_823 : null,
      artifacts_skipped: [],
      artifacts: finished
        ? [
            { name: 'report.md', bytes: 8241, uri: uri('report.md'), attempt_id: att, kind: 'markdown', content_type: 'text/markdown', role: null },
            { name: 'diff.patch', bytes: 91233, uri: uri('diff.patch'), attempt_id: att, kind: 'text', content_type: 'text/plain', role: null },
            { name: 'claude-code.stdout.log', bytes: 9397, uri: uri('claude-code.stdout.log'), attempt_id: att, kind: 'log', content_type: 'text/plain', role: 'agent_stdout' },
            { name: 'claude-code.stderr.log', bytes: 0, uri: uri('claude-code.stderr.log'), attempt_id: att, kind: 'log', content_type: 'text/plain', role: 'agent_stderr' },
            { name: 'claude-transcript.json', bytes: 10181, uri: uri('claude-transcript.json'), attempt_id: att, kind: 'json', content_type: 'application/json', role: 'agent_transcript' },
          ]
        : [],
    },
  }
}

/**
 * The fixture's copy of a task's input. The fixture tasks carry no credential,
 * so their copy is their input with a measured zero beside it -- the shape the
 * server sends, not a masking done here.
 */
async function fixtureTaskInput(taskId: string): Promise<Result<TaskInputCopy>> {
  await new Promise((r) => setTimeout(r, 30))
  noteFixtureProbe(route('/v1/tasks/{id}/input', { id: taskId }), 30, true)
  const detail = await fixtureAgentDetail(taskId)
  if (detail.status !== 'ok') return detail as Result<TaskInputCopy>
  const raw = detail.data.task.input
  const input: Record<string, unknown> =
    raw !== null && typeof raw === 'object' && !Array.isArray(raw) ? (raw as Record<string, unknown>) : {}
  const prompt = input['prompt']
  const others = Object.fromEntries(Object.entries(input).filter(([k]) => k !== 'prompt'))
  const clean = (text: string) => ({ text, redaction_count: 0 })
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      task_id: taskId,
      tenant_id: detail.data.task.tenant_id,
      read_at: new Date(FIXTURE_NOW).toISOString(),
      prompt_key: typeof prompt === 'string' ? 'string' : 'prompt' in input ? 'other' : 'missing',
      prompt: typeof prompt === 'string' ? clean(prompt) : null,
      rest: typeof prompt === 'string' && Object.keys(others).length > 0 ? clean(JSON.stringify(others, null, 2)) : null,
      full: clean(JSON.stringify(input, null, 2)),
      // The fixture's metadata carries no credential either: its copy is the
      // metadata with a measured zero, platform keys named as the server does.
      metadata: {
        value: detail.data.task.metadata ?? {},
        redaction_count: 0,
        platform_keys: Object.keys(detail.data.task.metadata ?? {}).filter((k) =>
          ['dispatch', 'input_from', 'expected_outputs'].includes(k),
        ),
      },
      redacted: false,
      redaction_count: 0,
      redaction: { applied_at_read_time: true, rules: 11 },
    },
  }
}

async function fixtureAnswer(taskId: string): Promise<Result<TaskAnswer>> {
  await new Promise((r) => setTimeout(r, 50))
  noteFixtureProbe(route('/v1/tasks/{id}/answer', { id: taskId }), 50, true)
  const detail = await fixtureAgentDetail(taskId)
  if (detail.status !== 'ok') return detail as Result<TaskAnswer>
  const state = detail.data.task.state
  const finished = TERMINAL_STATES.has(state)
  const content =
    '## Summary\n\nThe capacity reservation is all-or-nothing across every pool.\n\n' +
    '- concurrency counts from `LEASED`\n- a stale worker exits without running the agent\n'
  const ok = state === 'SUCCEEDED'
  const { attemptId, attempt } = fixtureLogAttempt(detail.data.task)
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      task_id: taskId,
      tenant_id: 'u-bogdan',
      attempt_id: attemptId,
      attempt,
      read_at: new Date(FIXTURE_NOW).toISOString(),
      status: ok ? 'ok' : finished ? 'absent' : 'not_yet',
      source: ok ? 'agent_result_event' : null,
      object: ok
        ? { stream: 'agent_stdout', source: 'final', uri: null, object_updated_at: new Date(FIXTURE_NOW - 60_000).toISOString() }
        : null,
      format: ok ? 'markdown' : null,
      content: ok ? content : null,
      complete: ok ? true : null,
      is_error: ok ? false : null,
      subtype: ok ? 'success' : null,
      stop_reason: ok ? 'end_turn' : null,
      terminal_reason: ok ? 'completed' : null,
      num_turns: ok ? 7 : null,
      bytes: ok ? new TextEncoder().encode(content).length : null,
      redacted: false,
      redaction_count: 0,
      // What the server says of a capture that was not cut, as #188 sends it.
      capture_truncated: ok ? false : null,
      detail: null,
    },
  }
}

async function fixtureTranscript(taskId: string, includeRaw: boolean): Promise<Result<TaskTranscript>> {
  await new Promise((r) => setTimeout(r, 60))
  noteFixtureProbe(route('/v1/tasks/{id}/transcript', { id: taskId }), 60, true)
  const detail = await fixtureAgentDetail(taskId)
  if (detail.status !== 'ok') return detail as Result<TaskTranscript>
  const task = detail.data.task
  // The newest attempt's transcript (F14b): its id, its end, its own result.
  const { attemptId, attempt, span } = fixtureLogAttempt(task)
  const finished = span?.ended != null
  const result = fixtureCliResult(task)
  const step = (n: number, over: Partial<TranscriptStep>): TranscriptStep => ({
    id: `L${n * 120}:0`,
    line_offset: n * 120,
    block: 0,
    kind: 'text',
    role: 'assistant',
    parent_tool_use_id: null,
    text: null,
    tool: null,
    tool_result: null,
    meta: null,
    truncated_fields: [],
    raw: includeRaw ? '{"type":"assistant"}' : null,
    ...over,
  })
  // An attempt that has not started has written no transcript yet.
  const started = span?.started != null
  // THE BROWSER QA AGENT'S LOG IS LONG (BROWSER-TAB-TABS, -LOGS and -LIVE, part of #2423): the
  // Browser tab's findings were measured on running browser agents with
  // logs of 20 and more steps, and a four-step log cannot show a log beside
  // the frame that runs past its pane. Forty browser actions, each a call
  // and its result, for the render to be checked against. THE FINISHED
  // BROWSER AGENT, task_f523c160 (FIXTURE-BROWSER-DONE, part of #2423), has
  // a short one, six actions and its result: its Browser tab is reviewed
  // for the end time, the links and the recordings, not for a long log.
  const browserInput = (action: string, i: number): Record<string, string> =>
    action === 'browser_navigate'
      ? { url: `https://example.com/page-${i + 1}` }
      : action === 'browser_click'
        ? { element: `Link ${i + 1} in the page's list`, ref: `e${i + 10}` }
        : action === 'browser_type'
          ? { element: 'Search box', ref: `e${i + 10}`, text: `query ${i + 1}` }
          : {}
  const browserSteps = (from: number, count: number): TranscriptStep[] =>
    Array.from({ length: count }, (_, i) => {
      const id = `toolu_b${String(i).padStart(2, '0')}`
      const action = ['browser_navigate', 'browser_snapshot', 'browser_click', 'browser_type'][i % 4]!
      return [
        // THE INPUTS A BROWSER TOOL CARRIES (QA2 overview-agents-31): `{ "step": n }`
        // held no address, element or text, so the transcript's one-line gist
        // (Artifacts.tsx `inputGist`) had nothing to show beside the tool's name.
        step(from + 2 * i, { kind: 'tool_call', tool: { id, name: action, input: JSON.stringify(browserInput(action, i), null, 2) } }),
        step(from + 2 * i + 1, { kind: 'tool_result', role: 'user', tool_result: { tool_use_id: id, is_error: false, content: `Step ${i + 1}: ${action} done; the page says "Example Domain".`, images: 0 } }),
      ]
    }).flat()
  const long = taskId === 'task_b40e5a1c' ? browserSteps(4, 40) : taskId === 'task_f523c160' ? browserSteps(4, 6) : []
  const steps: TranscriptStep[] = started
    ? [
        step(0, { kind: 'init', role: 'system', meta: { model: 'claude-opus-5', permission_mode: 'bypassPermissions', tool_count: 14 } }),
        step(1, { text: 'Reading the repository before changing anything.' }),
        step(2, { kind: 'tool_call', tool: { id: 'toolu_01', name: 'Read', input: '{\n  "file_path": "CONTRACT.md"\n}' } }),
        step(3, { kind: 'tool_result', role: 'user', tool_result: { tool_use_id: 'toolu_01', is_error: false, content: '# The contract\n...', images: 0 } }),
        ...long,
        ...(finished && result !== null
          ? [step(4 + long.length, { kind: 'result', role: null, text: result.text, meta: { subtype: result.subtype, is_error: result.is_error, num_turns: 7 } })]
          : []),
      ]
    : []
  const size = started ? Math.max(4096, steps.length * 120) : 0
  const at = span?.ended ?? FIXTURE_NOW - 4_000
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      task_id: taskId,
      tenant_id: 'u-bogdan',
      attempt_id: attemptId,
      attempt,
      read_at: new Date(FIXTURE_NOW).toISOString(),
      stream: {
        stream: 'agent_stdout',
        source: started ? (finished ? 'final' : 'live') : null,
        status: started ? 'ok' : 'absent',
        detail: started ? null : 'The attempt has not started, so it has written no transcript.',
        uri: started
          ? `gs://swarm-logs/tenants/u-bogdan/tasks/${taskId}/attempts/${attemptId}/logs/${finished ? 'final' : 'live'}/agent_stdout${finished ? '' : '.tail'}.log`
          : null,
        object_updated_at: started ? new Date(at).toISOString() : null,
        age_seconds: started ? Math.max(0, Math.round((Date.now() - at) / 1000)) : null,
        total_bytes: started ? size : null,
        offset: 0,
        returned_bytes: size,
        next_offset: null,
        truncated: false,
        tail_window: !started || finished ? null : { object_offset: 0, stream_size: size, published_at: new Date(at).toISOString() },
      },
      format: started ? 'claude-stream-json' : null,
      steps,
      complete: finished,
      window_starts_mid_stream: false,
      skipped_lines: 0,
      answer_in_window: finished && result !== null,
      // Known whole only for a whole final read, as #188 decides it.
      capture_truncated: finished ? false : null,
      redaction: { applied_at_read_time: true, rules: 6 },
      redaction_count: 0,
    },
  }
}

async function fixtureWorkflowRead(workflowId: string): Promise<Result<WorkflowRead>> {
  await new Promise((r) => setTimeout(r, 80))
  noteFixtureProbe(route('/v1/workflows/{id}', { id: workflowId }), 80, true)
  const workflow = fixtureWorkflowRows().find((w) => w.workflow_id === workflowId)
  if (workflow === undefined) {
    return {
      status: 'error',
      error: { kind: 'not_found', httpStatus: 404, code: 'not_found', message: 'This workflow does not exist, or it belongs to another tenant.' },
    }
  }
  const page = await fixtureTasks()
  // THE STEPS' OWN TASKS (review of QA2-FIX-16 runs2-36): the fixture tasks
  // carry no workflow_id, so a filter on it alone served a read with no step
  // task and Outcomes could not name the workflow Work › Workflows names.
  const stepTasks = new Set(workflow.steps.map((s) => s.task_id).filter((id): id is string => !!id))
  const tasks =
    page.status === 'ok' ? page.data.tasks.filter((t) => t.workflow_id === workflowId || stepTasks.has(t.id)) : []
  return { status: 'ok', fetchedAt: Date.now(), data: { workflow, tasks } }
}

/**
 * STOP ONE AGENT. `POST /v1/tasks/{id}/cancel`.
 *
 * The only write in this product that ends work rather than shaping it, so the
 * caller confirms first -- see `StopRun.tsx`, which owns the confirmation and
 * is the only thing that calls this.
 *
 * `released_immediately` is the field that matters in the response and it is
 * NOT the HTTP status: a 200 means the request was recorded. False means the
 * task held capacity and keeps it until the worker acts on the flag, which is
 * a different thing to tell someone than "it stopped".
 */
export async function cancelTask(taskId: string): Promise<Result<CancelResult>> {
  if (USE_FIXTURES) return fixtureCancel(taskId)
  return write(route('/v1/tasks/{id}/cancel', { id: taskId }), 'POST') as Promise<
    Result<CancelResult>
  >
}

/**
 * ONLY WHAT THIS CLIENT READS.
 *
 * The response also carries the re-read `task`, and it is deliberately not
 * typed here: the stop control reloads the screen rather than trusting an echo,
 * so nothing consumes it -- and declaring a field nothing reads is what forced
 * the fixture below to fabricate a whole Task in order to satisfy a shape.
 * A fixture that invents data is the thing README.md says fixtures must not be.
 */
export interface CancelResult {
  /**
   * TRUE only when the task held no capacity and went straight to CANCELLED.
   * False means the flag is set and a live container is still running.
   */
  released_immediately: boolean
}

/**
 * CANCEL ONE WORKFLOW. `POST /v1/workflows/{id}/cancel`
 * (apps/swarm-api/swarm_api/routes/workflows.py `cancel_workflow`).
 *
 * The server sets `cancel_requested` on the workflow and asks every step that
 * has a task to stop (`Store.cancel_workflow`): a step that holds no capacity
 * goes straight to CANCELLED, a running one keeps its lease until its worker
 * acts on the flag, and a step that has not been created yet never will be.
 * The caller confirms first -- see `CancelWorkflow` in Workflows.tsx, the only
 * thing that calls this -- and re-reads the board rather than trusting an echo.
 */
export async function cancelWorkflow(workflowId: string): Promise<Result<CancelWorkflowResult>> {
  if (USE_FIXTURES) return fixtureCancelWorkflow(workflowId)
  return write(route('/v1/workflows/{id}/cancel', { id: workflowId }), 'POST') as Promise<
    Result<CancelWorkflowResult>
  >
}

/** Only what this client reads of the cancel response (store.py `cancel_workflow`). */
export interface CancelWorkflowResult {
  workflow_id: string
  /** Task ids whose cancel was recorded. */
  tasks_cancelled: string[]
  /** Task ids that had already ended (or were not found), so nothing was asked of them. */
  tasks_already_terminal: string[]
}

async function fixtureCancelWorkflow(workflowId: string): Promise<Result<CancelWorkflowResult>> {
  await new Promise((r) => setTimeout(r, 60))
  // The same `route()` call as the live path, so the fixture lands in the same
  // registry record a live write would (CH-18). It reports no task ids: a
  // fixture that invented which tasks it cancelled would be fabricating data.
  noteFixtureProbe(route('/v1/workflows/{id}/cancel', { id: workflowId }), 60, true)
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: { workflow_id: workflowId, tasks_cancelled: [], tasks_already_terminal: [] },
  }
}

async function fixtureArtifactContent(
  taskId: string,
  name: string,
): Promise<Result<ArtifactContent>> {
  await new Promise((r) => setTimeout(r, 30))
  // The same `route()` call as the live path, so a fixture read lands in the
  // same registry record a live one would (CH-18).
  noteFixtureProbe(
    route('/v1/tasks/{id}/artifacts/content', { id: taskId }, new URLSearchParams({ name })),
    30,
    true,
  )
  // The newest attempt's artifacts, and its own reported cost (F14b, F14c).
  const task = fixtureTaskDoc(taskId)
  const att = fixtureLogAttempt(task).attemptId ?? 'none'
  const cost = task === undefined ? null : fixtureUsage(task)?.cost_usd ?? null
  const bodies: Record<string, string> = {
    'synthesis.md':
      '# Synthesis\n\nFive agents looked at the same question.\n\n' +
      '- cold start dominates the wall clock\n' +
      '- fencing held on every retry\n\n' +
      '```\nreserve(all_or_nothing)\n```\n\nSee `rollup.py` for the derivation.\n',
    'claude-transcript.json': JSON.stringify(
      [
        { type: 'system', subtype: 'init', model: 'claude-opus-4' },
        { type: 'assistant', message: { content: [{ type: 'text', text: 'Reading the repository.' }] } },
        { type: 'result', subtype: 'success', total_cost_usd: cost },
      ],
      null,
      2,
    ),
  }
  const content = bodies[name] ?? `fixture artifact ${name}\n`
  const bytes = new TextEncoder().encode(content).length
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      task_id: taskId,
      tenant_id: 'u-bogdan',
      attempt_id: att,
      artifact: { name, bytes, uri: `gs://swarm-artifacts/${name}` },
      status: 'ok',
      detail: null,
      key: `tenants/u-bogdan/tasks/${taskId}/attempts/${att}/artifacts/${name}`,
      uri: `gs://swarm-artifacts/${name}`,
      content,
      total_bytes: bytes,
      offset: 0,
      returned_bytes: bytes,
      next_offset: null,
      truncated: false,
      redacted: false,
      redaction_count: 0,
      redaction: { applied_at_read_time: true, rules: 11 },
    },
  }
}

async function fixtureCancel(taskId: string): Promise<Result<CancelResult>> {
  await new Promise((r) => setTimeout(r, 60))
  noteFixtureProbe(route('/v1/tasks/{id}/cancel', { id: taskId }), 60, true)
  // `released_immediately: false` is the fixture's answer on purpose: it is
  // the case the confirmation copy is written for, and a fixture that always
  // reported an instant stop would let that copy be developed against the
  // easier half.
  return { status: 'ok', fetchedAt: Date.now(), data: { released_immediately: false } }
}

async function fixtureResourceClasses(): Promise<Result<{ resource_classes: ResourceClasses }>> {
  await new Promise((r) => setTimeout(r, 40))
  noteFixtureProbe(route('/v1/resource-classes'), 40, true)
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    // The real three, so the requested-vs-utilised bars are exercised in
    // development rather than shipping having never been looked at.
    data: {
      resource_classes: {
        standard: { name: 'standard', cpu: 4, memory_gib: 8, disk_gib: 4, units: 1 },
        browser: { name: 'browser', cpu: 4, memory_gib: 8, disk_gib: 4, units: 1 },
        large: { name: 'large', cpu: 8, memory_gib: 32, disk_gib: 16, units: 4 },
      },
    },
  }
}

/**
 * THE RUNTIME CATALOGUE AND THE BACKENDS IT LANDS ON.
 *
 * THREE READS, AND THE FAILURES ARE NOT POOLED. Only `/v1/runtimes` can decide
 * there is no screen: it is the catalogue, and without it there is nothing to
 * draw. The other two each degrade to a `null` field with the reason beside it,
 * because a backend whose pool read failed and a backend with no ceiling
 * configured produce the same blank cell unless something keeps them apart --
 * which is the bug this whole UI exists to avoid.
 *
 * `/v1/capacity` supplies the `backend:*` pool counters, so the topology panel
 * can say how loaded each backend is rather than only that it exists.
 * `/v1/resource-classes` supplies the WHOLE sizing catalogue, including a class
 * no runtime references -- which `/v1/runtimes` cannot show, since it only ever
 * reports the class each runtime resolved to.
 */
export interface RuntimeTopology {
  /** Keyed by runner-profile name, exactly as a caller must spell it. */
  runtimes: Record<string, Runtime>
  /** null means the capacity read FAILED. An empty array means there are none. */
  pools: Pool[] | null
  poolsDetail: string | null
  /**
   * The pools each runtime must clear, by runner-profile name: the same
   * `/v1/capacity` read's `runner_profiles[name].pools`, which the server
   * builds with `pool_names_for` for the calling tenant. Absent or null when
   * that read failed -- never rebuilt here from a naming rule.
   */
  profilePools?: Record<string, string[]> | null
  /**
   * Of those, the pools with no document, by runner-profile name: the same
   * read's `admission.uncapped`, which admission skips as unlimited (QA
   * G5-04). Absent or null when that read failed or served no admission block.
   */
  profileUncapped?: Record<string, string[]> | null
  /** null means the class-catalogue read failed or served nothing. */
  classes: ResourceClasses | null
  classesDetail: string | null
}

export async function loadRuntimeTopology(): Promise<Result<RuntimeTopology>> {
  if (USE_FIXTURES) return fixtureRuntimeTopology()

  const [runtimes, capacity, classes] = await Promise.all([
    readRuntimes(),
    read<Capacity>(route('/v1/capacity'), () => false),
    loadResourceClasses(),
  ])

  if (runtimes.status !== 'ok' && runtimes.status !== 'stale') {
    return runtimes as Result<RuntimeTopology>
  }

  const poolsOk = capacity.status === 'ok' || capacity.status === 'stale'
  const classesOk = classes.status === 'ok' || classes.status === 'stale'

  return {
    status: runtimes.status,
    fetchedAt: runtimes.fetchedAt,
    data: {
      runtimes: runtimes.data.runtimes,
      pools: poolsOk ? capacity.data.pools : null,
      profilePools: poolsOk
        ? Object.fromEntries(
            Object.entries(capacity.data.runner_profiles ?? {}).map(([name, p]) => [name, p.pools]),
          )
        : null,
      profileUncapped: poolsOk
        ? Object.fromEntries(
            Object.entries(capacity.data.runner_profiles ?? {}).flatMap(([name, p]) =>
              p.admission === undefined ? [] : [[name, p.admission.uncapped]],
            ),
          )
        : null,
      poolsDetail: poolsOk
        ? null
        : capacity.status === 'error'
          ? capacity.error.message
          : 'The capacity read did not complete.',
      classes: classesOk ? classes.data.resource_classes : null,
      classesDetail: classesOk
        ? null
        : classes.status === 'error'
          ? classes.error.message
          : classes.status === 'empty'
            ? 'The catalogue route answered with no classes at all, which a healthy API cannot do — every runtime resolves to one.'
            : 'The resource-class read did not complete.',
    },
    error: runtimes.status === 'stale' ? runtimes.error : undefined,
  } as Result<RuntimeTopology>
}

/**
 * THE ONE FIXTURE IN THIS FILE THAT DELIBERATELY IS NOT REAL, and the reason is
 * the whole point of the screen it feeds.
 *
 * README.md says "fixtures are real" and every other fixture here honours that.
 * This one must not. The shipped catalogue lives in `swarm_common.profiles`,
 * `check-contract-parity.sh` does not read TypeScript, and a fixture is a copy
 * nothing ever compares -- so pasting the real five profiles and their real
 * cpu/memory/disk/unit figures in here would plant exactly the drift the route
 * exists to remove, in the file that reads the route. `RESOURCE_UNITS` in
 * types.ts is that mistake already made once; it is recorded in
 * docs/contract-change-requests.md and is not being made twice.
 *
 * So these names are obviously invented, and they are shaped to exercise every
 * branch the screen has rather than to resemble production:
 *   * two distinct resolved backends, so the topology panel groups;
 *   * one runtime whose DECLARED backend is AUTO, so the declared/resolved
 *     split is drawn at all -- no shipped profile is AUTO, which means live
 *     data would never once exercise it;
 *   * one runtime needing no provider, one needing all of its credentials, one
 *     accepting any single one of several;
 *   * three sizes and a duplicated image, so "what sets this apart" has both
 *     unique and shared facts to report;
 *   * a class in `resource_classes` that no runtime uses, so the sizing panel's
 *     "nothing routes here" row is seen in development.
 */
const FIXTURE_RUNTIMES: Record<string, Runtime> = {
  'demo-scribe': {
    name: 'demo-scribe', image: 'demo-runtime-base',
    backend: 'CLOUD_RUN_JOB', resolved_backend: 'CLOUD_RUN_JOB',
    provider: 'demo-vendor', secrets: ['DEMO_API_KEY', 'DEMO_OAUTH_TOKEN'], secrets_any_of: true,
    timeout_seconds: 7200, resource_class: 'demo-small',
    resources: { name: 'demo-small', cpu: 3, memory_gib: 7, disk_gib: 3, units: 1 },
    available: true, disabled_reason: '',
    // Invented classes, for the same reason as the names: a class this
    // deployment pins nothing for (`demo-later`) is the chip that says so.
    model_classes: ['demo-deep', 'demo-quick', 'demo-later'], default_model_class: 'demo-deep',
    models: { 'demo-deep': 'demo-model-deep-2', 'demo-quick': 'demo-model-quick-2' },
  },
  'demo-probe': {
    name: 'demo-probe', image: 'demo-runtime-base',
    backend: 'CLOUD_RUN_JOB', resolved_backend: 'CLOUD_RUN_JOB',
    provider: null, secrets: [], secrets_any_of: false,
    timeout_seconds: 300, resource_class: 'demo-small',
    resources: { name: 'demo-small', cpu: 3, memory_gib: 7, disk_gib: 3, units: 1 },
    available: true, disabled_reason: '',
    // Runs no model: the `—` branch of the classes row.
    model_classes: [], default_model_class: null, models: {},
  },
  'demo-viewport': {
    name: 'demo-viewport', image: 'demo-runtime-viewport',
    backend: 'GKE_AUTOPILOT', resolved_backend: 'GKE_AUTOPILOT',
    provider: 'demo-vendor', secrets: ['DEMO_API_KEY'], secrets_any_of: false,
    timeout_seconds: 5400, resource_class: 'demo-medium',
    resources: { name: 'demo-medium', cpu: 6, memory_gib: 15, disk_gib: 7, units: 2 },
    // THE DISABLED EXEMPLAR. A fixture set where every profile is available
    // means the disabled branch of the catalogue screen ships unexercised --
    // the same argument `demo-wide` makes for declaring AUTO, since nothing in
    // the shipped catalogue is AUTO either.
    available: false,
    // THE ADVICE NAMES A RUNTIME (QAFIX4-FIXTURE-B F13, run 4 capacity-006): a
    // caller picks a runtime by name, never a sizing class, so 'Use
    // demo-small' advised something nobody can choose. demo-probe is the one
    // runtime here that needs no demo-vendor credential; demo-scribe needs one.
    disabled_reason: 'demo-vendor refused the registered credential. Use demo-probe, which needs no demo-vendor credential.',
    model_classes: ['demo-deep'], default_model_class: 'demo-deep', models: { 'demo-deep': 'demo-model-deep-2' },
  },
  'demo-wide': {
    name: 'demo-wide', image: 'demo-runtime-wide',
    // Declared AUTO, resolved by the platform. Nothing in the shipped
    // catalogue is AUTO, so without this the split ships unexercised.
    backend: 'AUTO', resolved_backend: 'CLOUD_RUN_JOB',
    provider: 'other-vendor', secrets: ['OTHER_API_KEY', 'OTHER_REGION'], secrets_any_of: false,
    timeout_seconds: 10800, resource_class: 'demo-wide',
    resources: { name: 'demo-wide', cpu: 7, memory_gib: 30, disk_gib: 15, units: 4 },
    available: true, disabled_reason: '',
    model_classes: [], default_model_class: null, models: {},
  },
}

async function fixtureRuntimeTopology(): Promise<Result<RuntimeTopology>> {
  await new Promise((r) => setTimeout(r, 60))
  noteFixtureProbe(route('/v1/runtimes'), 60, true)
  const capacity = await fixtureCapacity()
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      runtimes: FIXTURE_RUNTIMES,
      pools: capacity.status === 'ok' ? capacity.data.pools : null,
      poolsDetail:
        capacity.status === 'ok' ? null : 'The fixture capacity read did not complete.',
      classes: Object.fromEntries(
        // Every class the invented runtimes resolve to, plus one nothing routes
        // to -- the row the sizing panel has to be able to draw.
        [...Object.values(FIXTURE_RUNTIMES).map((r) => [r.resources.name, r.resources] as const),
         ['demo-idle', { name: 'demo-idle', cpu: 1, memory_gib: 2, disk_gib: 1, units: 1 }] as const],
      ),
      classesDetail: null,
    },
  }
}

/**
 * ONE AGENT RUN, COMPLETE: the task, every attempt, every event, and the
 * ceilings the attempts' measurements should be read against.
 *
 * Four reads, and THE FAILURES ARE NOT POOLED. Only the task read can decide
 * there is no screen; the other three each degrade to a `null` field with the
 * reason beside it, because a failed attempts query and a task that has never
 * been admitted produce the same empty panel unless something keeps them
 * apart -- which is the bug this whole UI exists to avoid.
 *
 * `attempts: []` therefore means the query SUCCEEDED and the task has no
 * attempt document (QUEUED, PARKED, or READY and waiting for capacity).
 * `attempts: null` means the query failed and nothing may be concluded.
 */
/**
 * THE ONE READ OF `GET /v1/runtimes`. The catalogue screen reaches it through
 * `loadRuntimeTopology`, and since #185 the Timeline's profile filter through
 * `loadRunnerProfiles`; both call this, so the route has one reader and its
 * payload one type. Two readers of one route drift, which is the rule
 * tests/unit/control_plane/test_runtimes_screen.py holds.
 */
function readRuntimes(): Promise<Result<{ runtimes: Record<string, Runtime> }>> {
  return read<{ runtimes: Record<string, Runtime> }>(
    route('/v1/runtimes'),
    (d) => Object.keys(d.runtimes ?? {}).length === 0,
  )
}

/** The fixture catalogue's names, recorded as a read of the same route. */
async function fixtureRuntimeNames(): Promise<Result<string[]>> {
  await new Promise((r) => setTimeout(r, 90))
  noteFixtureProbe(route('/v1/runtimes'), 90, true)
  return { status: 'ok', fetchedAt: Date.now(), data: Object.keys(FIXTURE_RUNTIMES).sort() }
}

/** The fixture catalogue's model classes, recorded as a read of the same route. */
async function fixtureRuntimeModelClasses(): Promise<Result<Record<string, ModelClassOffer>>> {
  await new Promise((r) => setTimeout(r, 60))
  noteFixtureProbe(route('/v1/runtimes'), 60, true)
  return { status: 'ok', fetchedAt: Date.now(), data: offersOf(FIXTURE_RUNTIMES) }
}

function offersOf(runtimes: Record<string, Runtime>): Record<string, ModelClassOffer> {
  return Object.fromEntries(
    Object.entries(runtimes).map(([name, r]) => [
      name,
      // `?? []`, `?? null`, `?? {}`: an API older than the three fields
      // serves none of them, and its runners then offer no class at all --
      // the step runs its runner's default, which is what that API runs.
      { model_classes: r.model_classes ?? [], default_model_class: r.default_model_class ?? null, models: r.models ?? {} },
    ]),
  )
}

/**
 * What a step on each runner may name as `model_class` (docs/design/
 * model-per-step.md §4.8, #1435): the accepted classes, the default, and the
 * model each is pinned to, by runner-profile name. Both Submit forms read the
 * runners themselves from `/v1/capacity`, which carries no classes, so the
 * class select reads them from `/v1/runtimes` -- through `readRuntimes`, the
 * route's one reader.
 */
export interface ModelClassOffer {
  model_classes: string[]
  default_model_class: string | null
  models: Record<string, string>
}

export async function loadModelClasses(): Promise<Result<Record<string, ModelClassOffer>>> {
  if (USE_FIXTURES) return fixtureRuntimeModelClasses()
  const r = await readRuntimes()
  if (r.status === 'ok' || r.status === 'stale') {
    return { status: 'ok', fetchedAt: r.fetchedAt, data: offersOf(r.data.runtimes) }
  }
  return r as Result<Record<string, ModelClassOffer>>
}

export interface AgentRun {
  task: Task
  /** null means the event read FAILED. An empty array means there are none. */
  events: TaskEvent[] | null
  eventsDetail: string | null
  /** null means the attempt read FAILED. An empty array means none exist. */
  attempts: AttemptRow[] | null
  attemptsDetail: string | null
  /** null means the catalogue read FAILED, so no "requested" side may be drawn. */
  classes: ResourceClasses | null
  /** Why the catalogue is missing, and whether the route exists at all. */
  classesDetail: string | null
  /** True when the API did not recognise /v1/resource-classes -- a deployment
   *  older than the route, which is a different fix from a failed read. */
  classesRouteMissing: boolean
}

/**
 * THE MASKED INPUT IS NOT PART OF THIS READ (PR #210 re-review). It was a
 * fifth read in the `Promise.all` below, so the whole drawer -- the task's
 * state, its events, its attempts -- waited on `/v1/tasks/{id}/input`, and a
 * copy that took a minute blanked Details for a minute. Details' Input reads
 * it on its own (`loadTaskInputOnce`, as Inputs does), so a slow or failed
 * copy blanks only the blocks that draw it.
 */
export async function loadAgentRun(taskId: string): Promise<Result<AgentRun>> {
  if (USE_FIXTURES) return fixtureAgentRun(taskId)

  const id = { id: taskId }
  const [task, events, attempts, classes] = await Promise.all([
    read<{ task: Task } | Task>(route('/v1/tasks/{id}', id), () => false),
    read<{ events: TaskEvent[] }>(route(`/v1/tasks/{id}/events?limit=${EVENT_PAGE_LIMIT}`, id), () => false),
    // Each row carries the attempt's CPU as typed fields (contract request
    // #15). The `include=usage` this read used to send asked the server for
    // #188's interim reading off the task's events, which the typed fields
    // replaced; nothing extra is asked for now.
    read<{ attempts: AttemptRow[]; accounts_by_attempt?: Record<string, TaskAccount> }>(
      route(`/v1/tasks/{id}/attempts?limit=${ATTEMPT_PAGE_LIMIT}`, id),
      (d) => d.attempts.length === 0,
    ),
    loadResourceClasses(),
  ])

  // #379: each attempt's account is served BESIDE the rows, keyed by attempt
  // id, so a row stays the shape `GET /v1/attempts` serves; joined on here so
  // the attempt card reads it off its own row. Absent on an older API.
  const withAccounts = (d: { attempts: AttemptRow[]; accounts_by_attempt?: Record<string, TaskAccount> }) =>
    d.accounts_by_attempt === undefined
      ? d.attempts
      : d.attempts.map((a) => ({ ...a, account: d.accounts_by_attempt?.[a.attempt_id] ?? null }))

  if (task.status === 'loading') return task
  // A 404 asks the caller's other tenants whether one holds it (#1194 c15).
  if (task.status === 'error') return withElsewhere(taskId, task)
  if (task.status === 'empty') {
    return {
      status: 'error',
      error: {
        kind: 'not_found',
        httpStatus: 404,
        code: 'not_found',
        message: 'This agent does not exist, or it belongs to another tenant.',
      },
    }
  }

  // GET /v1/tasks/{id} returns {"task": {...}}, not the bare document. The
  // fallback stays because create and cancel return the same wrapper and a
  // caller could route through either.
  const raw = task.data as { task?: Task } & Task
  const unwrapped: Task = raw.task ?? raw

  const eventList = events.status === 'ok' ? events.data.events : events.status === 'empty' ? [] : null
  // `empty` is a real answer here and collapses to [], which is NOT the same
  // as the null a failure produces. The screen prints two different sentences.
  const attemptList =
    attempts.status === 'ok' ? withAccounts(attempts.data)
    : attempts.status === 'stale' ? withAccounts(attempts.data)
    : attempts.status === 'empty' ? []
    : null

  const classesOk = classes.status === 'ok' || classes.status === 'stale'

  return {
    status: task.status === 'stale' ? 'stale' : 'ok',
    data: {
      task: unwrapped,
      events: eventList,
      eventsDetail:
        eventList === null
          ? events.status === 'error' || events.status === 'stale'
            ? events.error.message
            : 'The event read did not complete.'
          : null,
      attempts: attemptList,
      attemptsDetail:
        attemptList === null
          ? attempts.status === 'error'
            ? attempts.error.message
            : 'The attempt read did not complete.'
          : null,
      classes: classesOk ? classes.data.resource_classes : null,
      classesDetail: classesOk
        ? null
        : classes.status === 'error'
          ? classes.error.message
          : classes.status === 'empty'
            ? 'The catalogue route returned no classes, which cannot happen for a healthy API — the frozen catalogue always has three.'
            : 'The resource-class read did not complete.',
      classesRouteMissing: classes.status === 'error' && classes.error.kind === 'not_found',
    },
    fetchedAt: task.fetchedAt,
    error: task.status === 'stale' ? task.error : undefined,
  } as Result<AgentRun>
}

/**
 * A task's CPU, memory and throttling over time, `GET /v1/tasks/{id}/utilisation`
 * (LIVE-UTIL-API, #1696): per attempt, newest first, one point per 5 s window.
 * Details' chart reads it on its own clock while the task runs
 * (`UtilisationPanel.tsx`), so a slow or failed read blanks only the chart.
 *
 * No empty predicate: `attempts: []` is a task nothing was admitted for, and
 * the chart says so in words rather than this read calling it empty.
 */
export async function loadUtilisation(taskId: string): Promise<Result<UtilisationRead>> {
  if (USE_FIXTURES) return fixtureUtilisation(taskId)
  const got = await read<unknown>(route('/v1/tasks/{id}/utilisation', { id: taskId }), () => false)
  if (got.status === 'ok') return { ...got, data: normUtilisation(got.data) }
  if (got.status === 'stale') return { ...got, data: normUtilisation(got.data) }
  return got
}

/**
 * Twenty minutes of 5 s points for the fixture: a run that idles, works near
 * its 4 cores and is throttled at the top, with one window the worker could
 * not read -- every shape the chart draws, in development.
 */
async function fixtureUtilisation(taskId: string): Promise<Result<UtilisationRead>> {
  const now = FIXTURE_NOW
  // The newest attempt's points, inside its own run (F14a): up to twenty
  // minutes of them, ending at its end or now, and never before its start.
  const task = fixtureTaskDoc(taskId)
  const { attemptId, span } = fixtureLogAttempt(task)
  const last = span?.ended ?? now
  const n = span?.started == null ? 240 : Math.max(2, Math.min(240, Math.floor((last - span.started) / 5_000)))
  const points = Array.from({ length: n }, (_, i) => {
    const cpu = i === Math.floor(n / 2) ? null : Math.min(4, 0.4 + 1.6 * (1 + Math.sin(i / 14)) + (i % 37 === 0 ? 1.5 : 0))
    return {
      at: new Date(last - (n - i) * 5_000).toISOString(),
      cpu_cores: cpu === null ? null : Number(cpu.toFixed(3)),
      memory_bytes: Math.round((1.5 + i / n * 2.2) * 1024 ** 3),
      throttled_share: cpu === null ? null : cpu >= 3.6 ? 0.35 : 0,
    }
  })
  return {
    status: 'ok',
    fetchedAt: now,
    data: {
      task_id: taskId,
      read_at: new Date(now).toISOString(),
      attempts: [
        {
          attempt_id: attemptId ?? 'none',
          generation: span?.n ?? 0,
          cpu_limit_cores: 4,
          memory_limit_bytes: 8 * 1024 ** 3,
          throttled_seconds: 41.2,
          peak_throttled_share: 0.35,
          mean_throttled_share: 0.06,
          points,
        },
      ],
    },
  }
}

async function fixtureAgentRun(taskId: string): Promise<Result<AgentRun>> {
  const [detail, attempts, classes] = await Promise.all([
    fixtureAgentDetail(taskId),
    fixtureAttempts(taskId),
    fixtureResourceClasses(),
  ])
  if (detail.status !== 'ok') return detail as Result<AgentRun>
  return {
    status: 'ok',
    fetchedAt: detail.fetchedAt,
    data: {
      task: detail.data.task,
      events: detail.data.events,
      eventsDetail: detail.data.eventsDetail,
      attempts: attempts.status === 'ok' ? attempts.data.attempts.map(fixtureCpu) : null,
      attemptsDetail: attempts.status === 'ok' ? null : 'The fixture attempt read did not complete.',
      classes: classes.status === 'ok' ? classes.data.resource_classes : null,
      classesDetail: null,
      classesRouteMissing: false,
    },
  }
}

/**
 * The typed CPU fields (contract request #15) on one fixture attempt: none
 * for one that never started, and the attempt's figures -- live while it
 * runs, the figures at exit once it ended -- for the rest, so every shape of
 * the Details CPU rows is looked at in development.
 */
function fixtureCpu(a: AttemptRow): AttemptRow {
  if (a.started_at === null) {
    return {
      ...a,
      cpu_seconds: null,
      peak_cpu_cores: null,
      mean_cpu_cores: null,
      cpu_limit_cores: null,
      cpu_measured_at: null,
      cpu_limit_source: null,
      cpu_reading_age_seconds: null,
    }
  }
  // Contract request #26: dated and sourced. A running attempt's reading is
  // 20 s old, an ended one's was written at its end.
  const age = a.completed_at === null ? 20 : Math.max(0, (FIXTURE_NOW - Date.parse(a.completed_at)) / 1000)
  return {
    ...a,
    cpu_seconds: 402.311,
    peak_cpu_cores: 1.62,
    mean_cpu_cores: 0.842,
    cpu_limit_cores: 2,
    cpu_measured_at: new Date(FIXTURE_NOW - age * 1000).toISOString(),
    cpu_limit_source: 'cgroup',
    cpu_reading_age_seconds: Math.round(age),
  }
}

export async function loadStats(options: { frame?: boolean; platform?: boolean } = {}): Promise<Result<Stats>> {
  if (USE_FIXTURES) return fixtureStats(options)
  // A successful read always yields twelve numbers, because count_tasks_by_state
  // iterates the whole enum and writes a key for each. So there is no empty
  // state here -- and an error must never render as "0 RUNNING", because
  // "0 RUNNING" and "stats failed" are opposite facts.
  //
  // `frame`: the shell's panel counts (Live, Waiting) read this as the FRAME's
  // read (CH-2), so its age never stands in for the page's own.
  //
  // `platform`: ask for the platform half too (`?scope=platform`, #2149),
  // which the API serves to an admin only. Platform counts' Run asks for it
  // and nothing else does: it is twelve count()s over every tenant, and a
  // read on a timer must never bill them (owner ruling 2026-10-07, #138).
  const path = options.platform === true ? '/v1/stats?scope=platform' : '/v1/stats'
  return read<Stats>(route(path), () => false, { frame: options.frame === true })
}

export async function loadDispatchControl(): Promise<Result<DispatchControl>> {
  if (USE_FIXTURES) return fixtureDispatch()
  // Admin-gated. A 403 here is information, not a failure.
  return read<DispatchControl>(route('/v1/admin/dispatch'), () => false)
}

export async function loadProviders(): Promise<Result<ProvidersPage>> {
  if (USE_FIXTURES) return fixtureProviders()
  return read<ProvidersPage>(route('/v1/providers'), (d) => d.providers.length === 0)
}

/** One page of tasks in one state. Server-side filter, backed by a real index. */
export async function loadTasksInState(
  state: TaskState,
  // THE OVERVIEW'S STATE READS (live QA OV-1/OV-4, 2026-10-11) ask for the
  // summary rows, as its page read does (#168); everyone else keeps the full
  // 200-row page this always served.
  options: { limit?: number; view?: 'summary' } = {},
): Promise<Result<TaskPage>> {
  if (USE_FIXTURES) {
    const page = await fixtureTasks()
    if (page.status !== 'ok') return page
    const tasks = page.data.tasks.filter((t) => t.state === state)
    return tasks.length === 0
      ? { status: 'empty', fetchedAt: Date.now() }
      : { status: 'ok', fetchedAt: Date.now(), data: { ...page.data, tasks } }
  }
  return read<TaskPage>(
    route(
      '/v1/tasks',
      {},
      new URLSearchParams({ state, limit: String(options.limit ?? 200), ...(options.view === 'summary' ? { view: 'summary' } : {}) }),
    ),
    (d) => d.tasks.length === 0,
  )
}

async function fixtureStats(options: { frame?: boolean } = {}): Promise<Result<Stats>> {
  await new Promise((r) => setTimeout(r, 220))
  noteFixtureProbe(route('/v1/stats'), 220, true, undefined, { frame: options.frame === true })
  // COUNTED FROM THE TASK LIST, never typed beside it (QAFIX4-FIXTURE-B F9,
  // run 4 overview-automate-002 and -003): typed apart, Overview's Waiting
  // read 2 over a list of 3 and Holding capacity 5 over 7 that hold it. Every
  // state keeps its key, as `count_tasks_by_state` writes one for each.
  const tasksByState: Record<string, number> = {
    SUBMITTED: 0, QUEUED: 0, PARKED: 0, READY: 0, LEASED: 0, DISPATCHED: 0,
    STARTING: 0, RUNNING: 0, SUCCEEDED: 0, FAILED: 0, CANCELLED: 0,
    DEAD_LETTERED: 0,
  }
  for (const t of fixtureTaskRows()) tasksByState[t.state] = (tasksByState[t.state] ?? 0) + 1
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      tenant_id: 'u-bogdan',
      dispatch_paused: false,
      tasks_by_state: tasksByState,
      limits: { max_batch_size: 100, max_input_bytes: 65536, max_workflow_steps: 50 },
      generated_at: new Date(FIXTURE_NOW).toISOString(),
    },
  }
}

async function fixtureDispatch(): Promise<Result<DispatchControl>> {
  await new Promise((r) => setTimeout(r, 120))
  // A non-admin genuinely cannot read this, and the board must render that as
  // information rather than breakage. The fixture exercises that path.
  noteFixtureProbe(route('/v1/admin/dispatch'), 120, false)
  return {
    status: 'error',
    error: {
      kind: 'admin_required',
      httpStatus: 403,
      code: 'forbidden',
      message: 'admin group membership is required',
    },
  }
}

async function fixtureProviders(): Promise<Result<ProvidersPage>> {
  await new Promise((r) => setTimeout(r, 200))
  noteFixtureProbe(route('/v1/providers'), 200, true)
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      tenant_id: 'u-bogdan',
      generated_at: new Date(FIXTURE_NOW).toISOString(),
      providers: [
        {
          provider: 'anthropic',
          credential_registered: true,
          runner_profiles: ['browser', 'claude-code'],
          quota: {
            provider: 'anthropic', tenant_id: 'u-bogdan', state: 'THROTTLED',
            updated_at: new Date(FIXTURE_NOW - 40_000).toISOString(),
            configured_hard_max: 50, adaptive_target: 6, quota_derived_limit: null,
            requests_remaining: 118, tokens_remaining: null,
            reset_at: new Date(FIXTURE_NOW + 900_000).toISOString(),
            cooldown_until: null,
            last_429_at: new Date(FIXTURE_NOW - 240_000).toISOString(),
            retry_after_seconds: null, success_count: 412, rate_limit_count: 9,
            effective_limit: 6,
          },
        },
        {
          provider: 'openai',
          credential_registered: false,
          runner_profiles: ['codex'],
          // No quota document for this tenant yet. UNKNOWN, not zeros.
          quota: null,
        },
      ],
    },
  }
}

/**
 * `GET /v1/tenants/me`. `frame` marks the product header's own read, which is
 * the FRAME's and not the screen's: it lands in the tab-wide registry like any
 * read, and the head's "newest read" -- the screen's own (CH-2) -- does not
 * count it. Platform counts reads the same route as a screen, without it.
 */
export async function loadMe(options: { frame?: boolean } = {}): Promise<Result<Me>> {
  if (USE_FIXTURES) return fixtureMe(options)
  return read<Me>(route('/v1/tenants/me'), () => false, { frame: options.frame === true })
}

/** One tenant the caller may select: an entry of `GET /v1/tenants/mine`. */
export interface TenantChoice {
  tenant_id: string
  /**
   * The workspace's stored name when `group` is present (MINE-NAMES: the
   * server read it, so a rename shows at once); otherwise the registered
   * group's email -- unique where the tenant id is not; "Your workspace (u-…)" for your own.
   */
  display_name: string
  /** The registered group's email, present only when `display_name` is the stored name. Absent from an older API. */
  group?: string
  /** `user` for the caller's own personal workspace, `group` for a tenant group (#1200). Absent from an older API. */
  kind?: 'user' | 'group'
  /**
   * The caller's STORED role there (docs/design/workspace-roles.md §4.3):
   * `{name, source}`. Absent from an older API, and absent when the role could
   * not be read -- the cue to show the derived role, never a guessed one.
   * `roles.ts` `roleOf` reads it.
   */
  role?: { name: string; source: string }
  /**
   * How the caller reaches it (§4.2.2): `personal`, `member`, or
   * `platform_admin` for a group workspace a platform admin is not in.
   * Absent from an older API.
   */
  via?: 'personal' | 'member' | 'platform_admin'
}

/**
 * `GET /v1/tenants/mine`: the caller's own ready personal workspace FIRST
 * (#1200: it is the default), then every registered tenant group the caller
 * is a VERIFIED member of, in the admin order -- the only values
 * `X-Swarm-Tenant` can select. A frame read, like `loadMe`. An empty list is
 * a caller in no tenant group without a ready workspace, which is an answer,
 * not a failure.
 */
export async function loadMyTenants(): Promise<Result<TenantChoice[]>> {
  if (USE_FIXTURES) {
    await new Promise((r) => setTimeout(r, 150))
    noteFixtureProbe(route('/v1/tenants/mine'), 150, true, undefined, { frame: true })
    // The member has a personal tenant and no group, so there is nothing to
    // pick. The platform-admin persona is also a member of `eng`, where its
    // STORED role is admin -- what Admin › Workspaces you admin lists.
    if (!fixtureIsAdmin && !fixtureIsWorkspaceAdmin) return { status: 'empty', fetchedAt: Date.now() }
    return { status: 'ok', fetchedAt: Date.now(), data: fixtureTenantChoices() }
  }
  return read<TenantChoice[]>(route('/v1/tenants/mine'), (rows) => rows.length === 0, { frame: true })
}

/**
 * ONE ENTRY OF THE NOTIFICATION FEED (#1206, proposal A). `id` is stable: the
 * same underlying thing has the same id on every read, so "read" survives a
 * re-read. A `needs_you` item is DERIVED from live state, not stored, and
 * leaves the feed once that state is resolved -- a handled request clears
 * itself without anyone marking it.
 */
export interface NotificationItem {
  id: string
  group: 'admin' | 'approval' | 'task' | 'account' | 'finished'
  needs_you: boolean
  title: string
  detail: string | null
  /** A console path, `/admin/people` or `/agents/live/<task>/browser`. */
  href: string
  at: string
  read: boolean
}

/** `GET /v1/notifications`: needs-you items, then at most 50 updates from the last 7 days. */
export interface NotificationFeed {
  items: NotificationItem[]
  unread_needs_you: number
  unread_updates: number
  /** Requests waiting on any admin; 0 for a non-admin. */
  admin_pending: number
  /**
   * Workspaces waiting to be created (Admin › Tenants); null for a non-admin
   * or an admin source that could not be read, absent from an older API.
   */
  tenants_waiting?: number | null
  /**
   * Platform secrets with no enabled version (Admin › Platform secrets); null
   * for a non-admin or a count that could not be read, absent from an older API.
   */
  platform_secrets_unset?: number | null
  read_at: string
}

/**
 * `GET /v1/notifications`, read by the frame on every page. A FRAME read: the
 * header's data age is the page's, and the bell's poll is not the page's data.
 *
 * The fixture build serves no feed, so it answers the 404 an API without the
 * route would, and the bell hides exactly as it does there. It records NO
 * probe: the fixture build makes no request, and a failing row for it on API
 * reads -- on every page of every fixture session -- would be a failure that
 * never happened, pushed above the ones being demonstrated.
 */
export async function loadNotifications(): Promise<Result<NotificationFeed>> {
  const target = route('/v1/notifications')
  // The fixture item is Carol's workspace request (`wsr_c4r0l`), dated from
  // its own `requested_at` as the API dates it (notifications.py), so the bell
  // and Admin › People and roles give one age for one request (QA1 admin-shell-06).
  if (USE_FIXTURES) return { status: 'ok', fetchedAt: Date.now(), data: { items: [{ id: 'admin:ws:wsr_c4r0l', group: 'admin', needs_you: true, title: 'Carol asked for a workspace', detail: null, href: '/admin/people', at: fixtureAgo(40 * 60_000), read: false }], unread_needs_you: 1, unread_updates: 0, admin_pending: 1, tenants_waiting: null, platform_secrets_unset: null, read_at: new Date().toISOString() } } as Result<NotificationFeed>
  const r = await read<NotificationFeed>(target, () => false, { frame: true })
  // A 200 with no list is not an empty feed, and drawing "Nothing needs you"
  // over an answer nobody could read would be a claim; it is a failure.
  if ((r.status === 'ok' || r.status === 'stale') && !Array.isArray(r.data.items)) {
    return { status: 'error', error: { kind: 'server_error', httpStatus: 200, code: null, message: 'The notifications answer carried no list of items.' } }
  }
  return r
}

/** `POST /v1/notifications/read {"ids": [...]}`. */
export async function markNotificationsRead(ids: readonly string[]): Promise<Result<unknown>> {
  const target = route('/v1/notifications/read')
  if (USE_FIXTURES) return scheduleNotServed(target)
  return write(target, 'POST', { ids })
}

/** `POST /v1/notifications/read-all`. */
export async function markAllNotificationsRead(): Promise<Result<unknown>> {
  const target = route('/v1/notifications/read-all')
  if (USE_FIXTURES) return scheduleNotServed(target)
  return write(target, 'POST')
}

/**
 * A row-bounded window, assembled by following page tokens.
 *
 * NEVER LET A CLAMP READ AS AN END OF DATA. `paged_limit` silently takes
 * min(requested, 200), so a client asking for limit=500 gets 200 rows and a
 * next_page_token with no error and no warning. The only honest end-of-data
 * signal is a null next_page_token, so that -- not a short page -- is what
 * stops this loop.
 */
export async function loadTaskWindow(budget: number): Promise<Result<TaskWindow>> {
  if (USE_FIXTURES) return fixtureWindow(budget)

  const tasks: Task[] = []
  let token: string | null = null
  let pages = 0
  let moreExist = false

  while (tasks.length < budget) {
    const qs = new URLSearchParams({ limit: '200' })
    if (token) qs.set('page_token', token)
    const page: Result<TaskPage> = await read<TaskPage>(route('/v1/tasks', {}, qs), () => false)
    if (page.status === 'error') {
      // Partial data plus a failure is still a failure to describe a window:
      // a span computed from half the rows would be wrong in a way nothing on
      // screen could reveal.
      if (tasks.length === 0) return page
      moreExist = true
      break
    }
    if (page.status === 'loading') break
    pages++
    if (page.status === 'empty') break
    if (page.status === 'ok' || page.status === 'stale') {
      tasks.push(...page.data.tasks)
      token = page.data.next_page_token ?? null
      if (!token) break
    }
  }
  if (token) moreExist = true

  const trimmed = tasks.slice(0, budget)
  const times = trimmed
    .map((t) => t.created_at)
    .filter(Boolean)
    .sort()
  return {
    status: trimmed.length === 0 ? 'empty' : 'ok',
    fetchedAt: Date.now(),
    data: {
      tasks: trimmed,
      moreExist: moreExist || trimmed.length < tasks.length,
      pages,
      from: times[0] ?? null,
      to: times[times.length - 1] ?? null,
    },
  } as Result<TaskWindow>
}

/** `GET /v1/tenants/mine` for the platform-admin persona: its own workspace first (#1200), then `eng`. */
function fixtureTenantChoices(): TenantChoice[] {
  return [
    { tenant_id: 'u-bogdan', display_name: 'Your workspace (u-bogdan)', kind: 'user', role: { name: 'admin', source: 'personal' }, via: 'personal' },
    { tenant_id: FIXTURE_GROUP.tenant_id, display_name: FIXTURE_GROUP.principal, kind: 'group', role: { name: 'admin', source: 'override' }, via: 'member' },
  ]
}

/**
 * THE WORKSPACE THE FIXTURE ACTS IN, as the API honours `X-Swarm-Tenant`: `eng`
 * when it is picked by a persona `/v1/tenants/mine` lists it for, else the
 * personal one. Every fixture read that names the acting workspace asks this
 * (QA2-FIX-07, admin-shell-01: `/v1/tenants/me` honoured the pick, but
 * `/v1/profile` and `/v1/access` did not, so Workspace Settings and GitHub
 * access still described u-bogdan after the eng card's link picked eng).
 */
function fixtureActingTenant(): string {
  return chosenTenant() === FIXTURE_GROUP.tenant_id && (fixtureIsAdmin || fixtureIsWorkspaceAdmin) ? FIXTURE_GROUP.tenant_id : 'u-bogdan'
}

async function fixtureMe(options: { frame?: boolean } = {}): Promise<Result<Me>> {
  await new Promise((r) => setTimeout(r, 150))
  noteFixtureProbe(route('/v1/tenants/me'), 150, true, undefined, { frame: options.frame === true })
  // THE PICK IS HONOURED, AS THE API HONOURS `X-Swarm-Tenant` (QA1-FIX-32,
  // admin-shell-01): Admin › Workspaces you admin selects `eng` and opens
  // Workspace Settings, which read this and drew the personal workspace. A
  // persona that `/v1/tenants/mine` lists `eng` for reads it when it is picked.
  const id = fixtureActingTenant()
  const group = id === FIXTURE_GROUP.tenant_id
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      tenant: {
        tenant_id: id, kind: group ? 'group' : 'user', principal: group ? FIXTURE_GROUP.principal : 'alice@example.com',
        // THE PERSONAL WORKSPACE IS THE SIGNED-IN PERSON'S (QA2-FIX-07, config2-06):
        // alice@example.com's, so named Alice. 'Bogdan' read as someone else's
        // workspace that alice administered, which a personal one never is.
        display_name: group ? FIXTURE_GROUP.principal : 'Alice', created_at: new Date(FIXTURE_NOW - 86_400_000 * 9).toISOString(),
        max_active: 2, capacity_units: 40, monthly_budget_usd: null, enabled: true,
        credentials: [
          'anthropic',
        ],
        service_account: `swarm-agent-worker-${id}@example-project.iam.gserviceaccount.com`,
        gcs_prefix: `gs://swarm-artifacts-dev/${id}`, namespace: `swarm-tenant-${id}`,
      },
      principal: {
        email: 'alice@example.com', domain: 'example.com',
        // The persona decides it (FIXTURE_PERSONA): the member is not an admin.
        groups: ['eng@example.com'], is_admin: fixtureIsAdmin,
      },
      // UNDECLARED, ON PURPOSE. There is no API behind the fixtures, so there
      // is no environment it runs as; this is the shape of an API whose
      // ENVIRONMENT is unset (the frozen default fills in "dev"). Brand.tsx
      // then falls back to the build and the host, and a laptop reads Local.
      environment: 'dev',
      environment_declared: false,
    },
  }
}

async function fixtureWindow(budget: number): Promise<Result<TaskWindow>> {
  const page = await fixtureTasks()
  if (page.status !== 'ok') return page as Result<TaskWindow>
  const tasks = page.data.tasks.slice(0, budget)
  const times = tasks.map((t) => t.created_at).sort()
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      tasks,
      moreExist: true,
      pages: 1,
      from: times[0] ?? null,
      to: times[times.length - 1] ?? null,
    },
  }
}

/**
 * The three tenants fixtureCapacity has pools for, each figure the one its
 * pool says: `capacity_units` is the `tenant:<id>` pool's hard limit (40, 10,
 * 2), and u-bogdan is fixtureMe's tenant field for field.
 */
function fixtureRoster(): Tenant[] {
  const created = (days: number) => new Date(FIXTURE_NOW - 86_400_000 * days).toISOString()
  const t = (tenant_id: string, kind: string, principal: string, display_name: string, days: number, max_active: number,
    capacity_units: number, credentials: string[]): Tenant => ({
    tenant_id, kind, principal, display_name, created_at: created(days), max_active, capacity_units,
    monthly_budget_usd: null, enabled: true, credentials,
    service_account: `swarm-agent-worker-${tenant_id}@example-project.iam.gserviceaccount.com`,
    gcs_prefix: `gs://swarm-artifacts-dev/${tenant_id}`, namespace: `swarm-tenant-${tenant_id}`,
  })
  return [
    t('u-bogdan', 'user', 'alice@example.com', 'Alice', 9, 2, 40, ['anthropic']),
    t(FIXTURE_GROUP.tenant_id, 'group', FIXTURE_GROUP.principal, 'Engineering', 30, 4, 10, ['anthropic', 'openai']),
    t('smoke', 'group', 'smoke@example.com', 'Smoke tests', 60, 1, 2, []),
  ]
}

/** `GET /v1/admin/tenants`, admin-gated. A 403 here is information. */
export async function loadTenants(): Promise<Result<{ tenants: Tenant[] }>> {
  if (USE_FIXTURES) return fixtureTenants()
  return read<{ tenants: Tenant[] }>(route('/v1/admin/tenants'), (d) => d.tenants.length === 0)
}

async function fixtureTenants(): Promise<Result<{ tenants: Tenant[] }>> {
  await new Promise((r) => setTimeout(r, 140))
  // The roster is admin-gated: the platform-admin persona reads it, the
  // member is refused exactly as before.
  if (fixtureIsAdmin) {
    noteFixtureProbe(route('/v1/admin/tenants'), 140, true)
    return { status: 'ok', fetchedAt: Date.now(), data: { tenants: fixtureRoster() } }
  }
  noteFixtureProbe(route('/v1/admin/tenants'), 140, false)
  return {
    status: 'error',
    error: {
      kind: 'admin_required',
      httpStatus: 403,
      code: 'forbidden',
      message: 'admin group membership is required',
    },
  }
}

/**
 * Unreleased leases. The freshest failure signal on the platform: it sees a
 * silent worker up to five minutes before the reconciler acts on it.
 *
 * Admin-gated, so a 403 here renders as information.
 */
export async function loadLeases(): Promise<Result<LeasePage>> {
  if (USE_FIXTURES) return fixtureLeases()
  return read<LeasePage>(route('/v1/admin/leases?active_only=true&limit=200'), (d) => d.leases.length === 0)
}

/**
 * The current lease heartbeat of each of the caller's slot-holding tasks
 * (`GET /v1/leases`, #179). TENANT-SCOPED, not admin-gated: this is how a
 * member's Agents list says a worker has gone silent without reading
 * `/v1/admin/leases` or one task's events per row.
 *
 * Empty is a real answer -- no task of this tenant holds a slot -- because the
 * route serves every live lease of the tenant, unpaged.
 */
export async function loadLeaseHeartbeats(): Promise<Result<LeaseHeartbeatPage>> {
  if (USE_FIXTURES) return fixtureLeaseHeartbeats()
  return read<LeaseHeartbeatPage>(route('/v1/leases'), (d) => d.heartbeats.length === 0)
}

/** Every attempt of one task, newest first. Tenant-scoped. */
export async function loadAttempts(taskId: string): Promise<Result<{ attempts: AttemptRow[] }>> {
  if (USE_FIXTURES) return fixtureAttempts(taskId)
  return read<{ attempts: AttemptRow[] }>(
    route(`/v1/tasks/{id}/attempts?limit=${ATTEMPT_PAGE_LIMIT}`, { id: taskId }),
    (d) => d.attempts.length === 0,
  )
}

/**
 * ONE PAGE OF THE TENANT'S ATTEMPTS ACROSS TASKS: `GET /v1/attempts`, the
 * bars of the Timeline's Lanes page (redesign-v2 S4, the UI half). `since`
 * is inclusive and `until` exclusive on `created_at`, both ISO instants; rows
 * come newest first; `pageToken` asks for the page after one already read.
 * No attempt in the window is `empty`, which is an answer, not a failure.
 */
export async function loadAttemptsPage(opts: {
  since: string
  until: string
  pageToken?: string | null
}): Promise<Result<AttemptsPage>> {
  if (USE_FIXTURES) return fixtureAttemptsPage(opts.since, opts.until)
  const q = new URLSearchParams({ since: opts.since, until: opts.until, limit: String(ATTEMPT_PAGE_LIMIT) })
  if (opts.pageToken) q.set('page_token', opts.pageToken)
  return read<AttemptsPage>(route('/v1/attempts', {}, q), (d) => d.attempts.length === 0)
}

/**
 * ONE PAGE OF ONE TASK'S EVENTS, NEWEST FIRST by default (`order=desc`), and
 * the page after it by `pageToken`: the UI half of redesign-v2 S1. Every
 * other event read in this client asks for the OLDEST page, so a long run
 * shows its beginning; a timeline lane needs its end.
 */
export async function loadTaskEventsPage(
  taskId: string,
  opts: { order?: 'asc' | 'desc'; pageToken?: string | null },
): Promise<Result<TaskEventsPage>> {
  const order = opts.order ?? 'desc'
  if (USE_FIXTURES) return fixtureEventsPage(taskId, order)
  const q = new URLSearchParams({ limit: String(EVENT_PAGE_LIMIT), order })
  if (opts.pageToken) q.set('page_token', opts.pageToken)
  // An empty page is still a page: "no events" is drawn as such, not as a failure.
  return read<TaskEventsPage>(route('/v1/tasks/{id}/events', { id: taskId }, q), () => false)
}

async function fixtureAttemptsPage(since: string, until: string): Promise<Result<AttemptsPage>> {
  const tasks = await fixtureTasks()
  noteFixtureProbe(route('/v1/attempts'), 200, true)
  if (tasks.status !== 'ok') return { status: 'empty', fetchedAt: Date.now() }
  const lo = Date.parse(since)
  const hi = Date.parse(until)
  const rows: AttemptRow[] = []
  for (const t of tasks.data.tasks) {
    const got = await fixtureAttempts(t.id)
    if (got.status === 'ok') rows.push(...got.data.attempts)
  }
  const inWindow = rows
    .filter((a) => Date.parse(a.created_at) >= lo && Date.parse(a.created_at) < hi)
    .sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at))
  if (inWindow.length === 0) return { status: 'empty', fetchedAt: Date.now() }
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: { tenant_id: 'u-bogdan', read_at: new Date(FIXTURE_NOW).toISOString(), attempts: inWindow, next_page_token: null, coverage: { scope: 'page' } },
  }
}

async function fixtureEventsPage(taskId: string, order: 'asc' | 'desc'): Promise<Result<TaskEventsPage>> {
  const detail = await fixtureAgentDetail(taskId)
  if (detail.status !== 'ok') return detail.status === 'error' ? detail : { status: 'ok', fetchedAt: Date.now(), data: { events: [], next_page_token: null } }
  const events = [...(detail.data.events ?? [])].sort((a, b) => Date.parse(a.at) - Date.parse(b.at))
  if (order === 'desc') events.reverse()
  return { status: 'ok', fetchedAt: Date.now(), data: { events, next_page_token: null } }
}

/** The fixture leases' tasks: holding-capacity tasks `fixtureTasks` serves. */
const FIXTURE_LEASE_TASKS: Readonly<Record<string, string>> = {
  d1: 'task_a073aff5',
  c1: 'task_8ea5c26d',
  b1: 'task_19d91b82',
  a1: 'task_460409cd',
}

async function fixtureLeases(): Promise<Result<LeasePage>> {
  await new Promise((r) => setTimeout(r, 180))
  noteFixtureProbe(route('/v1/admin/leases'), 180, true)
  const iso = (secAgo: number) => new Date(FIXTURE_NOW - secAgo * 1000).toISOString()
  const row = (
    id: string,
    silent: number,
    extra: Partial<LeaseRow> = {},
  ): LeaseRow => ({
    lease_id: id,
    // A TASK THE AGENT FIXTURE KNOWS (QA1-FIX-12 chrome-C25): Holders names a
    // holder by its task read, and `task_d1` is not one, so every row fell
    // back to its short id on the fixture console.
    task_id: FIXTURE_LEASE_TASKS[id] ?? `task_${id}`,
    attempt_id: `att_${id}`,
    tenant_id: 'u-bogdan',
    generation: 1,
    pools: ['global', 'tenant:u-bogdan'],
    units: 1,
    dispatch_state: 'DISPATCHED',
    created_at: iso(600),
    dispatch_deadline: iso(300),
    expires_at: iso(-120),
    heartbeat_at: iso(silent),
    released_at: null,
    release_reason: null,
    released: false,
    expired: false,
    dispatch_overdue: false,
    silent_seconds: silent,
    heartbeat_ever: true,
    last_error: null,
    ...extra,
  })
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      // One of each classification, so all three treatments are visible while
      // working on this screen rather than only the happy one.
      leases: [
        row('d1', 400, { expired: true, expires_at: iso(60) }),
        row('c1', 140),
        // LEASED AFTER SUBMITTED (visual QA pass 2, ovag2-03, 2026-10-29): every
        // row was leased 10 min ago, before `task_19d91b82` (3 min) and
        // `task_460409cd` (1 min) were submitted, so Overview's held-since-lease
        // age ran longer than Agents' since-submitted one. Each is leased after
        // its task, its dispatch deadline after its lease.
        row('b1', 20, { created_at: iso(160), dispatch_deadline: iso(130) }),
        row('a1', 0, {
          created_at: iso(40),
          dispatch_deadline: iso(10),
          dispatch_state: 'LEASED',
          dispatch_overdue: true,
          heartbeat_ever: false,
          heartbeat_at: null,
          silent_seconds: 480,
          last_error: 'BACKEND_REJECTED (attempt att_a1)',
        }),
      ],
      thresholds: { heartbeat_grace_seconds: 90, lease_timeout_seconds: 120 },
      evaluated_at: new Date(FIXTURE_NOW).toISOString(),
      active_only: true,
      tenant_id: null,
      units_held: 4,
      // Every live lease is in the four rows above, so the drift check on the
      // Holders screen is evidence -- the shape `/v1/admin/leases` serves for
      // a fleet smaller than its limit.
      active_beyond_window: 0,
      truncated: false,
      examined: 4,
    },
  }
}

async function fixtureLeaseHeartbeats(): Promise<Result<LeaseHeartbeatPage>> {
  await new Promise((r) => setTimeout(r, 150))
  noteFixtureProbe(route('/v1/leases'), 150, true)
  // FROM THE TASK FIXTURE, so every row joins a task the Agents list draws:
  // a heartbeat for a task id no row has would exercise nothing.
  const page = await fixtureTasks()
  const held = page.status === 'ok'
    ? page.data.tasks.filter((t) => CONCURRENCY_STATES.has(t.state) && t.current_lease_id !== null)
    : []
  const now = FIXTURE_NOW
  const iso = (secAgo: number) => new Date(now - secAgo * 1000).toISOString()
  const grace = 90
  const row = (task: (typeof held)[number], i: number): LeaseHeartbeat => {
    // One of each treatment, so all three are visible while working on the
    // list: a silent worker, one that has never beaten (a booting worker is
    // not a silent one), and healthy ones. The LEASED task is the booting
    // one: no container has run for it yet.
    const kind = i === 0 ? 'silent' : task.state === 'LEASED' ? 'never' : 'well'
    const silent = kind === 'silent' ? grace + 240 : kind === 'never' ? null : 12
    return {
      task_id: task.id,
      lease_id: task.current_lease_id ?? '',
      attempt_id: `att_${task.id.slice(-4)}_${task.current_generation}`,
      generation: task.current_generation,
      task_state: task.state,
      dispatch_state: task.state === 'LEASED' ? 'LEASED' : 'DISPATCHED',
      // The booting one was admitted a minute ago and its dispatch deadline
      // is still ahead; the others were admitted long enough ago that theirs
      // has passed, which `dispatch_overdue` reports whether or not the worker
      // beats (`Lease.dispatch_overdue`, contract change request 9).
      created_at: iso(kind === 'never' ? 60 : 900),
      dispatch_deadline: iso(kind === 'never' ? -420 : 420),
      expires_at: iso(kind === 'silent' ? 30 : -120),
      heartbeat_at: silent === null ? null : iso(silent),
      silent_seconds: silent,
      silent: kind === 'silent',
      expired: kind === 'silent',
      dispatch_overdue: kind !== 'never',
    }
  }
  return {
    status: 'ok',
    fetchedAt: now,
    data: {
      tenant_id: 'u-bogdan',
      read_at: new Date(now).toISOString(),
      thresholds: { heartbeat_grace_seconds: grace, lease_timeout_seconds: 120 },
      heartbeats: held.map(row),
    },
  }
}

/**
 * The exit code a FAILED fixture task's newest attempt ended with: the
 * non-zero code its own `last_error` names ("exit status 2: ..."), else 1.
 *
 * The worker writes the failing exit code on the attempt and the same failure
 * into the task's `last_error`, so the real API never serves a failed task
 * whose newest attempt exited 0. The fixture did, on every failed task, and
 * visual QA run 2 reported the contradiction (WG-4, #1680). Fixture-only:
 * nothing on the live path calls this.
 */
function fixtureFailedExitCode(lastError: string | null): number {
  const m = lastError?.match(/\bexit (?:status|code) (\d+)\b/i)
  const named = m ? Number(m[1]) : 0
  return named !== 0 ? named : 1
}

async function fixtureAttempts(taskId: string): Promise<Result<{ attempts: AttemptRow[] }>> {
  await new Promise((r) => setTimeout(r, 160))
  noteFixtureProbe(route('/v1/tasks/{id}/attempts', { id: taskId }), 160, true)
  const task = fixtureTaskDoc(taskId)
  if (task === undefined) return { status: 'ok', fetchedAt: Date.now(), data: { attempts: [] } }
  return { status: 'ok', fetchedAt: Date.now(), data: { attempts: fixtureAttemptRows(task) } }
}

/**
 * A fixture task's attempt documents, NEWEST FIRST, from its timeline
 * (`fixtureSpans`). Every read that shows an attempt -- the Attempts tab, the
 * run's header, a workflow step's figures, the spend panel -- reads these, so
 * a step's total is its attempts' sum and the panel's attempt is one of them
 * (QAFIX4-FIXTURE-B F14c, run 4 flow-workflow-graph-light-07).
 *
 * THE NEWEST ATTEMPT FOLLOWS THE TASK'S STATE. A live one has no exit code,
 * no `peak_rss_bytes` and no spend yet -- both are written at its end -- so
 * the live-agent path, with memory off the newest heartbeat, is the one seen
 * in development. A failed task's newest attempt exits as its `last_error`
 * says. The older attempts ran before usage capture: every spend figure null,
 * which must render as em dashes and never as $0.00. The never-started one
 * (dispatch refused) has no exit code and no measurement at all.
 */
function fixtureAttemptRows(task: Task): AttemptRow[] {
  const spans = fixtureSpans(task)
  const newest = spans.length
  const iso = (t: number | null) => (t === null ? null : new Date(t).toISOString())
  const usage = fixtureUsage(task)
  return spans
    .map((sp): AttemptRow => {
      const latest = sp.n === newest
      const refused = sp.error === 'BACKEND_REJECTED'
      const ended = sp.ended !== null
      return {
        attempt_id: fixtureAttemptId(task.id, sp.n),
        task_id: task.id,
        tenant_id: 'u-bogdan',
        generation: sp.n,
        // THE NEWEST ATTEMPT HOLDS THE TASK'S LEASE when the task holds one.
        // `attemptEnd` reads an attempt as over once the task stops holding
        // its lease, so lease ids that never matched showed every live agent
        // in development as ended.
        lease_id: latest ? (task.current_lease_id ?? `lease_${sp.n}`) : `lease_${sp.n}`,
        backend: 'CLOUD_RUN_JOB',
        execution_name: refused || (latest && task.state === 'LEASED') ? null : `swarm-job-u-bogdan-claude-code-${sp.n}`,
        // PRODUCTION SHAPE: a started attempt's `created_at` IS its start. The
        // worker's `record_attempt_start` rewrites the scheduler's document
        // with a non-merge `.set()` (control.py), so admission survives only as
        // the `lease_acquired` event; a never-started attempt keeps its
        // admission, because its document was never rewritten.
        created_at: iso(sp.admitted) ?? task.created_at,
        started_at: iso(sp.started),
        completed_at: iso(sp.ended),
        exit_code: sp.exit,
        error: sp.error,
        peak_rss_bytes: sp.started === null || !ended ? null : sp.n === 2 && newest >= 3 ? 7_730_941_132 : 1_842_000_000,
        peak_disk_bytes: latest && ended && sp.started !== null ? 2_147_483_648 : null,
        oom_near_miss: sp.n === 2 && newest >= 3,
        checkpoints: sp.checkpoints,
        input_tokens: null,
        output_tokens: null,
        cache_read_input_tokens: null,
        cache_creation_input_tokens: null,
        cost_usd: null,
        ...(latest && ended && sp.started !== null && usage !== null ? usage : {}),
      }
    })
    .reverse()
}

/**
 * Leases plus the pool counters they should agree with.
 *
 * Two reads, because the drift check needs both sides. The LEASE read decides
 * whether there is a screen; a failed pool read leaves the holder table fully
 * trustworthy and only the comparison unavailable, so it degrades to `pools:
 * null` with the reason rather than failing the screen.
 */
export interface HoldersBoard {
  page: LeasePage
  /** null means the pool read FAILED. An empty array means there are none. */
  pools: Pool[] | null
  poolsDetail: string | null
}

export async function loadHolders(): Promise<Result<HoldersBoard>> {
  if (USE_FIXTURES) return fixtureHolders()

  const [leases, capacity] = await Promise.all([
    // NEVER `empty` FROM THE LEASE READ ALONE -- see `holdersAreEmpty`. The
    // page comes back as a page, and whether it is a real zero is decided
    // below with the counters in hand.
    read<LeasePage>(route('/v1/admin/leases?active_only=true&limit=200'), () => false),
    read<Capacity>(route('/v1/capacity'), () => false),
  ])

  if (leases.status === 'loading' || leases.status === 'error' || leases.status === 'empty') {
    return leases as Result<HoldersBoard>
  }

  const pools = capacity.status === 'ok' || capacity.status === 'stale' ? capacity.data.pools : null
  const detail =
    pools === null
      ? capacity.status === 'error' || capacity.status === 'stale'
        ? capacity.error.message
        : 'The pool read did not complete.'
      : null

  if (leases.status === 'ok' && holdersAreEmpty(leases.data, pools)) {
    return { status: 'empty', fetchedAt: leases.fetchedAt, serverAt: leases.serverAt }
  }

  return {
    status: leases.status,
    fetchedAt: leases.fetchedAt,
    data: { page: leases.data, pools, poolsDetail: detail },
  } as Result<HoldersBoard>
}

/**
 * WHETHER THE HOLDERS SCREEN MAY SAY "NOTHING HOLDS CAPACITY" (CP-7, visual QA
 * 2026-09-25).
 *
 * `empty` is drawn as a REAL ZERO, and it is two claims, not one: no live
 * lease exists, AND no pool counter is holding units. The second is the one
 * that matters most here -- a counter left non-zero with nothing behind it is
 * exactly the leak the drift card exists to find -- and it used to be dropped:
 * the capacity read was issued alongside the lease read and then discarded
 * whenever the lease page came back with no rows, so a leaked counter with
 * zero live leases rendered as the calmest thing on the screen.
 *
 * So `empty` needs BOTH, measured: a lease page that vouches it left no live
 * lease out (`active_beyond_window === 0`, not truncated -- an older API that
 * cannot say goes to the screen as a page, where the coverage is marked
 * unreported), and a counter read that succeeded with every `active` at 0.
 * A counter read that FAILED is not a zero either: the page goes to the screen,
 * where the drift card says the comparison was not made.
 */
function holdersAreEmpty(page: LeasePage, pools: Pool[] | null): boolean {
  const noLiveLease =
    page.leases.length === 0 && page.active_beyond_window === 0 && page.truncated !== true
  return noLiveLease && pools !== null && pools.every((p) => p.active === 0)
}

async function fixtureHolders(): Promise<Result<HoldersBoard>> {
  const [l, c] = await Promise.all([fixtureLeases(), fixtureCapacity()])
  if (l.status !== 'ok' || c.status !== 'ok') return l as Result<HoldersBoard>
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: { page: l.data, pools: c.data.pools, poolsDetail: null },
  }
}

/** `GET /v1/admin/quota`. Admin-gated, so a 403 renders as information. */
export async function loadAdminQuota(): Promise<Result<{ quota: QuotaState[] }>> {
  if (USE_FIXTURES) return fixtureAdminQuota()
  return read<{ quota: QuotaState[] }>(route('/v1/admin/quota'), (d) => d.quota.length === 0)
}

/**
 * THE FIXTURE'S QUOTA DOCUMENTS, one source for Provider quota and for the
 * provider pools they feed (QAFIX6-FIXTURE i, run 5b QA-051, box 6343783860).
 * The broker writes each document's cap into its `provider:<p>:tenant:<t>`
 * pool as `quota_derived_limit`, with its `adaptive_target`
 * (quota_broker/service.py `_pool_write`, aimd.py `quota_derived_limit_for`),
 * and the pool's ceiling is the least of those and its hard limit
 * (`SlotPool.effective_limit`). Typed apart, openai · eng read exhausted at
 * cap 0 beside a pool at ceiling 5 and 'ok'.
 *
 * One of every treatment, so none of them ships unlooked-at: a healthy row,
 * THROTTLED (the one a fallthrough would mislabel), EXHAUSTED, whose cap of 0
 * is a FACT that closes its pool, and UNKNOWN, which is not healthy but does
 * not close anything: `QuotaState.effective_limit` forces 0 only for
 * EXHAUSTED, DISABLED and COOLDOWN, so UNKNOWN's cap is its configured max.
 */
function fixtureQuotaRows(): QuotaState[] {
  const q = (
    provider: string,
    tenant: string,
    state: string,
    extra: Partial<QuotaState> = {},
  ): QuotaState => ({
    provider, tenant_id: tenant, state,
    updated_at: new Date(FIXTURE_NOW - 60_000).toISOString(),
    configured_hard_max: 50, adaptive_target: null, quota_derived_limit: null,
    requests_remaining: null, tokens_remaining: null, reset_at: null,
    cooldown_until: null, last_429_at: null, retry_after_seconds: null,
    success_count: 120, rate_limit_count: 0, effective_limit: 50,
    ...extra,
  })
  return [
    q('anthropic', 'eng', 'AVAILABLE', { requests_remaining: 4210 }),
    q('anthropic', 'u-bogdan', 'THROTTLED', {
      adaptive_target: 6, effective_limit: 6, rate_limit_count: 9,
      last_429_at: new Date(FIXTURE_NOW - 240_000).toISOString(),
    }),
    q('openai', 'eng', 'EXHAUSTED', { effective_limit: 0, rate_limit_count: 41 }),
    q('openai', 'u-bogdan', 'UNKNOWN', { success_count: 0 }),
  ]
}

async function fixtureAdminQuota(): Promise<Result<{ quota: QuotaState[] }>> {
  await new Promise((r) => setTimeout(r, 170))
  noteFixtureProbe(route('/v1/admin/quota'), 170, true)
  // `feeds_pool` as `GET /v1/admin/quota` serves it (routes/admin.py): the
  // pool document each cap feeds, so Provider quota says where the pool's
  // ceiling sits against the cap rather than leaving two numbers to disagree.
  const pools = new Map(fixturePools().map((p) => [p.name, p]))
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      quota: fixtureQuotaRows().map((q) => ({ ...q, feeds_pool: pools.get(`provider:${q.provider}:tenant:${q.tenant_id}`) ?? null })),
    },
  }
}

/**
 * A platform admin's reset of one tenant's AIMD target (QUOTA-RESET), from
 * Admin › Pool limits (POOL-LIMITS-QUOTA, BGD 2026-10-25, part of #2302).
 * The route proxies to the quota broker, which clamps the target into
 * [min_target, configured_hard_max] and answers `{ quota, reset }`: `reset`
 * carries `from_target`, `to_target` and `clamped`, which the row draws.
 */
export async function resetAdaptiveTarget(
  provider: string,
  tenant: string,
  body: { target: number; reason: string; clear_cooldown: boolean },
): Promise<Result<unknown>> {
  if (USE_FIXTURES) {
    await new Promise((r) => setTimeout(r, 200))
    const q = fixtureQuotaRows().find((row) => row.provider === provider && row.tenant_id === tenant)
    const max = q?.configured_hard_max ?? body.target
    const to = Math.min(body.target, max)
    return {
      status: 'ok',
      fetchedAt: Date.now(),
      data: {
        quota: { ...q, adaptive_target: to },
        reset: { from_target: q?.adaptive_target ?? max, to_target: to, clamped: to !== body.target },
      },
    }
  }
  return write(route('/v1/admin/quota/{provider}/{tenant}/adaptive-target', { provider, tenant }), 'PUT', body)
}

/**
 * A platform admin's change to one tenant's configured max (the broker's
 * `configured_hard_max`), from Admin › Pool limits (POOL-LIMITS-QUOTA). The
 * broker lowers the target to a max set below it in the same write; the
 * answer's `change` says so (`target_clamped`, `from_target`, `to_target`).
 */
export async function setQuotaHardMax(
  provider: string,
  tenant: string,
  body: { hard_max: number; reason: string },
): Promise<Result<unknown>> {
  if (USE_FIXTURES) {
    await new Promise((r) => setTimeout(r, 200))
    const q = fixtureQuotaRows().find((row) => row.provider === provider && row.tenant_id === tenant)
    const from = q?.adaptive_target ?? null
    const to = from === null ? null : Math.min(from, body.hard_max)
    return {
      status: 'ok',
      fetchedAt: Date.now(),
      data: {
        quota: { ...q, configured_hard_max: body.hard_max, adaptive_target: to },
        change: {
          from_hard_max: q?.configured_hard_max ?? null, to_hard_max: body.hard_max,
          from_target: from, to_target: to, target_clamped: from !== null && to !== null && to < from,
        },
      },
    }
  }
  return write(route('/v1/admin/quota/{provider}/{tenant}/hard-max', { provider, tenant }), 'PUT', body)
}

/**
 * Set a pool's hard limit, routing by the pool's own name.
 *
 * The API has one route per pool KIND rather than one generic route, so the
 * name has to be decomposed. Two of these did not exist until 2026-09-20 --
 * backend, and the per-tenant slice of a provider -- which is precisely why
 * backend:CLOUD_RUN_JOB and provider:anthropic:tenant:u-bogdan became the
 * binding constraints the moment every other pool was raised.
 */
export async function setPoolLimit(poolName: string, limit: number): Promise<Result<unknown>> {
  const parts = poolName.split(':')
  let path: ApiRoute | null = null

  // One route per pool KIND, each keyed by its template (CH-18): a limit set on
  // two runners is one `/v1/admin/limits/runner/{name}` in the registry, not two.
  if (poolName === 'global') path = route('/v1/admin/limits/global')
  else if (parts[0] === 'tenant' && parts[1]) path = route('/v1/admin/limits/tenant/{name}', { name: parts[1] })
  else if (parts[0] === 'resource' && parts[1]) path = route('/v1/admin/limits/resource/{name}', { name: parts[1] })
  else if (parts[0] === 'runner' && parts[1]) path = route('/v1/admin/limits/runner/{name}', { name: parts[1] })
  else if (parts[0] === 'backend' && parts[1]) path = route('/v1/admin/limits/backend/{name}', { name: parts[1] })
  else if (parts[0] === 'provider' && parts[1] && parts[2] === 'tenant' && parts[3])
    path = route('/v1/admin/limits/provider/{name}/tenant/{tenant}', { name: parts[1], tenant: parts[3] })
  else if (parts[0] === 'provider' && parts[1]) path = route('/v1/admin/limits/provider/{name}', { name: parts[1] })

  if (path === null) {
    // Fail loudly rather than POSTing somewhere plausible. A limit that
    // silently went nowhere is worse than one that refused.
    return {
      status: 'error',
      error: {
        kind: 'invalid',
        httpStatus: null,
        code: null,
        message: `No admin route covers a pool named "${poolName}".`,
      },
    }
  }

  if (USE_FIXTURES) {
    await new Promise((r) => setTimeout(r, 200))
    // The route's shape -- `{ pool }` with the pool's new hard limit -- because
    // the editor compares that pool with what was asked (AdminSettings `saveOutcome`).
    return { status: 'ok', fetchedAt: Date.now(), data: { pool: { name: poolName, hard_limit: limit } } }
  }

  return write(path, 'PUT', { limit })
}

// --------------------------------------------------------------------------
// Platform settings: the model price table (#1481)
// --------------------------------------------------------------------------
// `GET /v1/settings/model-prices` serves the table to every signed-in member
// (PRICES-FOLLOWUP); `PUT /v1/admin/settings` (`Store.set_platform_settings`)
// is admin-only and writes it. The fixture build serves both (QAFIX4-FIXTURE-A,
// run-4 admin-001): one table, `fixturePrices`, so the members' read and the
// admin's settings never disagree about a price.
//
// NORMALISE OR DROP, NEVER GUESS. An entry with a rate that is not a finite
// number of 0 or more, or with no `as_of`, is dropped here exactly as
// `modelprices.entry` drops it on the server: its model then reads "cost not
// reported", which is what swarm-api prices it at. A zero in its place would
// be a figure nobody set.

/**
 * The fixture's price table, as `GET /v1/settings/model-prices` serves it: the
 * models the fixture runners are pinned to (FIXTURE_RUNTIMES) and the one the
 * fixture attempts ran. One is tiered, so the tier rows are drawn; one is the
 * seed (`code`, no admin), one an admin's edit. Built per call, dated off now.
 */
function fixturePrices(): Record<string, unknown> {
  const day = (days: number) => new Date(FIXTURE_NOW - 86_400_000 * days).toISOString().slice(0, 10)
  return {
    'claude-opus-5': { input: 5, output: 25, cache_read: 0.5, cache_write: 6.25, currency: 'USD', source: 'pricing-page', as_of: day(3), updated_by: null },
    'demo-model-deep-2': { input: 3, output: 15, cache_read: 0.3, cache_write: 3.75, currency: 'USD', source: 'console', as_of: day(12), updated_by: 'ops@example.com' },
    'demo-model-quick-2': {
      input: 0.8, output: 4, cache_read: 0.08, cache_write: 1, currency: 'USD', source: 'code', as_of: day(40), updated_by: null,
      tiers: [
        { input: 0.8, output: 4, cache_read: 0.08, cache_write: 1, up_to_prompt_tokens: 200_000 },
        { input: 1.6, output: 6, cache_read: 0.16, cache_write: 2, up_to_prompt_tokens: null },
      ],
    },
  }
}

/** `GET /v1/admin/settings` in the fixture build: the two repository limits set by an admin, and the price table above. */
function fixturePlatformSettings(): Record<string, unknown> {
  return {
    merge_by_default: false,
    max_additional_repositories: 4,
    additional_repositories_budget_gib: 20,
    model_prices: fixturePrices(),
    updated_at: new Date(FIXTURE_NOW - 2 * 86_400_000).toISOString(),
    updated_by: 'ops@example.com',
    settings_document: 'present',
  }
}

function normRates(r: Record<string, unknown>): Record<ModelPriceRate, number> | null {
  const rates: Partial<Record<ModelPriceRate, number>> = {}
  for (const k of MODEL_PRICE_RATES) {
    const v = r[k]
    if (typeof v !== 'number' || !Number.isFinite(v) || v < 0) return null
    rates[k] = v
  }
  return rates as Record<ModelPriceRate, number>
}

/**
 * A tiered entry's tiers, or null when they are not tiers: two or more, each
 * with its four rates, ceilings that rise, and no ceiling on the last alone.
 * A malformed list drops the WHOLE entry, as `modelprices.entry` does: its
 * first tier alone would under-price every prompt past its ceiling.
 */
function normTiers(raw: unknown): ModelPriceTier[] | null {
  if (!Array.isArray(raw) || raw.length < 2) return null
  const out: ModelPriceTier[] = []
  for (const [i, t] of raw.entries()) {
    if (typeof t !== 'object' || t === null || Array.isArray(t)) return null
    const r = t as Record<string, unknown>
    const rates = normRates(r)
    const up = r.up_to_prompt_tokens ?? null
    const last = i === raw.length - 1
    if (rates === null) return null
    if (last ? up !== null : typeof up !== 'number' || !Number.isInteger(up) || up < 1) return null
    const before = out[out.length - 1]?.up_to_prompt_tokens
    if (!last && before !== undefined && before !== null && (up as number) <= before) return null
    out.push({ ...rates, up_to_prompt_tokens: up as number | null })
  }
  return out
}

function normModelPrice(raw: unknown): ModelPrice | null {
  if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) return null
  const r = raw as Record<string, unknown>
  const rates = normRates(r)
  if (rates === null) return null
  if (r.currency !== 'USD' || typeof r.source !== 'string' || typeof r.as_of !== 'string') return null
  const tiers = r.tiers === undefined || r.tiers === null ? undefined : normTiers(r.tiers)
  if (tiers === null) return null
  return {
    ...rates,
    currency: 'USD',
    source: r.source,
    as_of: r.as_of,
    updated_by: typeof r.updated_by === 'string' ? r.updated_by : null,
    ...(tiers === undefined ? {} : { tiers }),
  }
}

/** `model_prices` as served, each malformed entry dropped (see above). */
export function normModelPrices(raw: unknown): Record<string, ModelPrice> {
  if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) return {}
  const out: Record<string, ModelPrice> = {}
  for (const [name, entry] of Object.entries(raw as Record<string, unknown>)) {
    const price = normModelPrice(entry)
    if (price !== null) out[name] = price
  }
  return out
}

function normPlatformSettings(raw: unknown): PlatformSettings {
  const s = raw as PlatformSettings
  return { ...s, model_prices: normModelPrices((raw as { model_prices?: unknown } | null)?.model_prices) }
}

/**
 * `GET /v1/settings/model-prices`: the price table for every signed-in
 * member, admin or not, normalised as above. The console's price table reads
 * this, so a non-admin sees the figures read-only.
 */
export async function loadModelPrices(): Promise<Result<ModelPriceTable>> {
  if (USE_FIXTURES) {
    await new Promise((r) => setTimeout(r, 90))
    noteFixtureProbe(route('/v1/settings/model-prices'), 90, true)
    return { status: 'ok', fetchedAt: Date.now(), data: { model_prices: normModelPrices(fixturePrices()) } }
  }
  const r = await read<unknown>(route('/v1/settings/model-prices'), () => false)
  if (r.status === 'ok' || r.status === 'stale') {
    return { ...r, data: { model_prices: normModelPrices((r.data as { model_prices?: unknown } | null)?.model_prices) } }
  }
  return r as Result<ModelPriceTable>
}

/** `GET /v1/admin/settings`, with its price table normalised. */
export async function loadPlatformSettings(): Promise<Result<PlatformSettings>> {
  if (USE_FIXTURES) {
    await new Promise((r) => setTimeout(r, 90))
    const target = route('/v1/admin/settings')
    if (!fixtureIsAdmin) return fixtureAdminRefusal(target, 90)
    noteFixtureProbe(target, 90, true)
    return { status: 'ok', fetchedAt: Date.now(), data: normPlatformSettings(fixturePlatformSettings()) }
  }
  const r = await read<unknown>(route('/v1/admin/settings'), () => false)
  if (r.status === 'ok' || r.status === 'stale') return { ...r, data: normPlatformSettings(r.data) }
  return r as Result<PlatformSettings>
}

/**
 * `PUT /v1/admin/settings` with `model_prices` and nothing else, so a save
 * never rewrites a setting the price table did not touch. Each named entry
 * replaces that model's whole entry; every model not named keeps its own
 * (`Store._model_prices_after`). The response is the settings AS STORED, which
 * is what the table draws next, and carries who saved and when.
 */
export async function saveModelPrices(changes: Record<string, ModelPriceChange>): Promise<Result<PlatformSettings>> {
  const r = await write(route('/v1/admin/settings'), 'PUT', { model_prices: changes })
  if (r.status === 'ok' || r.status === 'stale') return { ...r, data: normPlatformSettings(r.data) }
  return r as Result<PlatformSettings>
}

/**
 * Which price a refused save is about. FastAPI's RequestValidationError is
 * wrapped into `detail.errors[].loc`, here `["body", "model_prices", <model>,
 * <rate>]`: the message is kept under `<model>/<rate>`, or under `<model>`
 * when the loc names the entry and no rate (a missing `as_of`, an unknown
 * key). A model name never holds a `/` (`modelprices.MODEL_NAME`). What names
 * no model is returned as `unattributed`, to be shown above the table, never
 * pinned to a guess: a message under the wrong cell sends someone editing a
 * figure that was never the problem.
 */
export function priceRefusals(error: ApiError): { fields: Record<string, string>; unattributed: string | null } {
  const detail = typeof error.detail === 'object' && error.detail !== null ? (error.detail as Record<string, unknown>) : {}
  const fields: Record<string, string> = {}
  for (const e of Array.isArray(detail.errors) ? detail.errors : []) {
    if (typeof e !== 'object' || e === null) continue
    const loc = Array.isArray((e as { loc?: unknown }).loc) ? ((e as { loc: unknown[] }).loc).map(String) : []
    const path = loc[0] === 'body' ? loc.slice(1) : loc
    if (path[0] !== 'model_prices' || path[1] === undefined) continue
    const msg = typeof (e as { msg?: unknown }).msg === 'string' ? (e as { msg: string }).msg : error.message
    const rate = MODEL_PRICE_RATES.find((k) => k === path[2]) ?? (path[2] === 'as_of' ? 'as_of' : undefined)
    const key = rate === undefined ? path[1] : `${path[1]}/${rate}`
    fields[key] = fields[key] === undefined ? msg : `${fields[key]}; ${msg}`
  }
  return { fields, unattributed: Object.keys(fields).length === 0 ? error.message : null }
}

// --------------------------------------------------------------------------
// Fixtures
// --------------------------------------------------------------------------
// These are the REAL pool names and the REAL live values read from the dev
// platform on 2026-09-19, not invented ones -- including tenant:u-bogdan at 40
// against a configured 2, which is the out-of-band drift `make pool-check`
// found. A fixture that shows a tidy platform teaches the wrong thing about
// what this screen is for.

/**
 * The `admission` block /v1/capacity serves for the pool rows below.
 *
 * NOT hand-written and NOT recomputed here: the literal output of
 * `swarm_api.headroom.analyse_profile` over exactly those rows. That is why a
 * fixture can carry it without becoming a second implementation of the rule --
 * and `tests/unit/control_plane/test_blocker_ui_surface.py` re-runs the
 * analyser over the same rows and asserts this is still what it produces, so a
 * fixture that drifts from the server fails a test rather than teaching a shape
 * the API never sends.
 *
 * STRICT JSON ON PURPOSE, quoted keys and all: that test parses this literal,
 * and `make test` runs with no node, so it has to be readable as text.
 *
 * Two entries teach the thing the panel exists for. `mock` is capped by THREE
 * pools tied at 15 and `codex` by three tied at 10, so every single-ceiling
 * counterfactual on them reads zero. Those zeros are the shape an operator has
 * to recognise, and they are real numbers off the dev platform, not staged ones.
 *
 * `browser` was regenerated at 1 unit when contract request 79 dropped the
 * class from 2; test_blocker_ui_surface.py's `FIXTURE_PROFILES` carries the
 * weight it is generated with, so change both in one commit.
 */
const FIXTURE_ADMISSION: Record<string, ProfileAdmission> = {
  "mock": {
    "units": 1,
    "headroom": 15,
    "basis": "measured",
    "blockers": [],
    "binding": [
      "global",
      "resource:standard",
      "backend:CLOUD_RUN_JOB"
    ],
    "counterfactual": [
      {
        "pool": "global",
        "action": "raise",
        "headroom_after": 15,
        "basis_after": "measured",
        "delta": 0,
        "next_binding": [
          "resource:standard",
          "backend:CLOUD_RUN_JOB"
        ]
      },
      {
        "pool": "tenant:u-bogdan",
        "action": "raise",
        "headroom_after": 15,
        "basis_after": "measured",
        "delta": 0,
        "next_binding": [
          "global",
          "resource:standard",
          "backend:CLOUD_RUN_JOB"
        ]
      },
      {
        "pool": "resource:standard",
        "action": "raise",
        "headroom_after": 15,
        "basis_after": "measured",
        "delta": 0,
        "next_binding": [
          "global",
          "backend:CLOUD_RUN_JOB"
        ]
      },
      {
        "pool": "runner:mock",
        "action": "raise",
        "headroom_after": 15,
        "basis_after": "measured",
        "delta": 0,
        "next_binding": [
          "global",
          "resource:standard",
          "backend:CLOUD_RUN_JOB"
        ]
      },
      {
        "pool": "backend:CLOUD_RUN_JOB",
        "action": "raise",
        "headroom_after": 15,
        "basis_after": "measured",
        "delta": 0,
        "next_binding": [
          "global",
          "resource:standard"
        ]
      }
    ],
    "complete": true,
    "unread": [],
    "uncapped": []
  },
  "generic": {
    "units": 1,
    "headroom": 10,
    "basis": "measured",
    "blockers": [],
    "binding": [
      "runner:generic"
    ],
    "counterfactual": [
      {
        "pool": "global",
        "action": "raise",
        "headroom_after": 10,
        "basis_after": "measured",
        "delta": 0,
        "next_binding": [
          "runner:generic"
        ]
      },
      {
        "pool": "tenant:u-bogdan",
        "action": "raise",
        "headroom_after": 10,
        "basis_after": "measured",
        "delta": 0,
        "next_binding": [
          "runner:generic"
        ]
      },
      {
        "pool": "resource:standard",
        "action": "raise",
        "headroom_after": 10,
        "basis_after": "measured",
        "delta": 0,
        "next_binding": [
          "runner:generic"
        ]
      },
      {
        "pool": "runner:generic",
        "action": "raise",
        "headroom_after": 15,
        "basis_after": "measured",
        "delta": 5,
        "next_binding": [
          "global",
          "resource:standard",
          "backend:CLOUD_RUN_JOB"
        ]
      },
      {
        "pool": "backend:CLOUD_RUN_JOB",
        "action": "raise",
        "headroom_after": 10,
        "basis_after": "measured",
        "delta": 0,
        "next_binding": [
          "runner:generic"
        ]
      }
    ],
    "complete": true,
    "unread": [],
    "uncapped": []
  },
  "claude-code": {
    "units": 1,
    "headroom": 0,
    "basis": "measured",
    "blockers": [
      {
        "pool": "provider:anthropic:tenant:u-bogdan",
        "reason": "PROVIDER_CONCURRENCY_LIMIT",
        "limit": 5,
        "active": 5,
        "group": "no_room"
      }
    ],
    "binding": [
      "provider:anthropic:tenant:u-bogdan"
    ],
    "counterfactual": [
      {
        "pool": "global",
        "action": "raise",
        "headroom_after": 0,
        "basis_after": "measured",
        "delta": 0,
        "next_binding": [
          "provider:anthropic:tenant:u-bogdan"
        ]
      },
      {
        "pool": "tenant:u-bogdan",
        "action": "raise",
        "headroom_after": 0,
        "basis_after": "measured",
        "delta": 0,
        "next_binding": [
          "provider:anthropic:tenant:u-bogdan"
        ]
      },
      {
        "pool": "resource:standard",
        "action": "raise",
        "headroom_after": 0,
        "basis_after": "measured",
        "delta": 0,
        "next_binding": [
          "provider:anthropic:tenant:u-bogdan"
        ]
      },
      {
        "pool": "runner:claude-code",
        "action": "raise",
        "headroom_after": 0,
        "basis_after": "measured",
        "delta": 0,
        "next_binding": [
          "provider:anthropic:tenant:u-bogdan"
        ]
      },
      {
        "pool": "backend:CLOUD_RUN_JOB",
        "action": "raise",
        "headroom_after": 0,
        "basis_after": "measured",
        "delta": 0,
        "next_binding": [
          "provider:anthropic:tenant:u-bogdan"
        ]
      },
      {
        "pool": "provider:anthropic",
        "action": "raise",
        "headroom_after": 0,
        "basis_after": "measured",
        "delta": 0,
        "next_binding": [
          "provider:anthropic:tenant:u-bogdan"
        ]
      },
      {
        "pool": "provider:anthropic:tenant:u-bogdan",
        "action": "raise",
        "headroom_after": 5,
        "basis_after": "measured",
        "delta": 5,
        "next_binding": [
          "runner:claude-code",
          "provider:anthropic"
        ]
      }
    ],
    "complete": true,
    "unread": [],
    "uncapped": []
  },
  "codex": {
    "units": 1,
    "headroom": 10,
    "basis": "measured",
    "blockers": [],
    "binding": [
      "runner:codex",
      "provider:openai",
      "provider:openai:tenant:u-bogdan"
    ],
    "counterfactual": [
      {
        "pool": "global",
        "action": "raise",
        "headroom_after": 10,
        "basis_after": "measured",
        "delta": 0,
        "next_binding": [
          "runner:codex",
          "provider:openai",
          "provider:openai:tenant:u-bogdan"
        ]
      },
      {
        "pool": "tenant:u-bogdan",
        "action": "raise",
        "headroom_after": 10,
        "basis_after": "measured",
        "delta": 0,
        "next_binding": [
          "runner:codex",
          "provider:openai",
          "provider:openai:tenant:u-bogdan"
        ]
      },
      {
        "pool": "resource:standard",
        "action": "raise",
        "headroom_after": 10,
        "basis_after": "measured",
        "delta": 0,
        "next_binding": [
          "runner:codex",
          "provider:openai",
          "provider:openai:tenant:u-bogdan"
        ]
      },
      {
        "pool": "runner:codex",
        "action": "raise",
        "headroom_after": 10,
        "basis_after": "measured",
        "delta": 0,
        "next_binding": [
          "provider:openai",
          "provider:openai:tenant:u-bogdan"
        ]
      },
      {
        "pool": "backend:CLOUD_RUN_JOB",
        "action": "raise",
        "headroom_after": 10,
        "basis_after": "measured",
        "delta": 0,
        "next_binding": [
          "runner:codex",
          "provider:openai",
          "provider:openai:tenant:u-bogdan"
        ]
      },
      {
        "pool": "provider:openai",
        "action": "raise",
        "headroom_after": 10,
        "basis_after": "measured",
        "delta": 0,
        "next_binding": [
          "runner:codex",
          "provider:openai:tenant:u-bogdan"
        ]
      },
      {
        "pool": "provider:openai:tenant:u-bogdan",
        "action": "raise",
        "headroom_after": 10,
        "basis_after": "measured",
        "delta": 0,
        "next_binding": [
          "runner:codex",
          "provider:openai"
        ]
      }
    ],
    "complete": true,
    "unread": [],
    "uncapped": []
  },
  "browser": {
    "units": 1,
    "headroom": 0,
    "basis": "measured",
    "blockers": [
      {
        "pool": "provider:anthropic:tenant:u-bogdan",
        "reason": "PROVIDER_CONCURRENCY_LIMIT",
        "limit": 5,
        "active": 5,
        "group": "no_room"
      }
    ],
    "binding": [
      "provider:anthropic:tenant:u-bogdan"
    ],
    "counterfactual": [
      {
        "pool": "global",
        "action": "raise",
        "headroom_after": 0,
        "basis_after": "measured",
        "delta": 0,
        "next_binding": [
          "provider:anthropic:tenant:u-bogdan"
        ]
      },
      {
        "pool": "tenant:u-bogdan",
        "action": "raise",
        "headroom_after": 0,
        "basis_after": "measured",
        "delta": 0,
        "next_binding": [
          "provider:anthropic:tenant:u-bogdan"
        ]
      },
      {
        "pool": "resource:browser",
        "action": "raise",
        "headroom_after": 0,
        "basis_after": "measured",
        "delta": 0,
        "next_binding": [
          "provider:anthropic:tenant:u-bogdan"
        ]
      },
      {
        "pool": "runner:browser",
        "action": "raise",
        "headroom_after": 0,
        "basis_after": "measured",
        "delta": 0,
        "next_binding": [
          "provider:anthropic:tenant:u-bogdan"
        ]
      },
      {
        "pool": "backend:GKE_AUTOPILOT",
        "action": "raise",
        "headroom_after": 0,
        "basis_after": "measured",
        "delta": 0,
        "next_binding": [
          "provider:anthropic:tenant:u-bogdan"
        ]
      },
      {
        "pool": "provider:anthropic",
        "action": "raise",
        "headroom_after": 0,
        "basis_after": "measured",
        "delta": 0,
        "next_binding": [
          "provider:anthropic:tenant:u-bogdan"
        ]
      },
      {
        "pool": "provider:anthropic:tenant:u-bogdan",
        "action": "raise",
        "headroom_after": 4,
        "basis_after": "measured",
        "delta": 4,
        "next_binding": [
          "resource:browser",
          "runner:browser",
          "backend:GKE_AUTOPILOT"
        ]
      }
    ],
    "complete": true,
    "unread": [],
    "uncapped": []
  },
  "claude-code-browser": {
    "units": 1,
    "headroom": 0,
    "basis": "measured",
    "blockers": [
      {
        "pool": "provider:anthropic:tenant:u-bogdan",
        "reason": "PROVIDER_CONCURRENCY_LIMIT",
        "limit": 5,
        "active": 5,
        "group": "no_room"
      }
    ],
    "binding": [
      "provider:anthropic:tenant:u-bogdan"
    ],
    "counterfactual": [
      {
        "pool": "global",
        "action": "raise",
        "headroom_after": 0,
        "basis_after": "measured",
        "delta": 0,
        "next_binding": [
          "provider:anthropic:tenant:u-bogdan"
        ]
      },
      {
        "pool": "tenant:u-bogdan",
        "action": "raise",
        "headroom_after": 0,
        "basis_after": "measured",
        "delta": 0,
        "next_binding": [
          "provider:anthropic:tenant:u-bogdan"
        ]
      },
      {
        "pool": "resource:browser",
        "action": "raise",
        "headroom_after": 0,
        "basis_after": "measured",
        "delta": 0,
        "next_binding": [
          "provider:anthropic:tenant:u-bogdan"
        ]
      },
      {
        "pool": "backend:GKE_AUTOPILOT",
        "action": "raise",
        "headroom_after": 0,
        "basis_after": "measured",
        "delta": 0,
        "next_binding": [
          "provider:anthropic:tenant:u-bogdan"
        ]
      },
      {
        "pool": "provider:anthropic",
        "action": "raise",
        "headroom_after": 0,
        "basis_after": "measured",
        "delta": 0,
        "next_binding": [
          "provider:anthropic:tenant:u-bogdan"
        ]
      },
      {
        "pool": "provider:anthropic:tenant:u-bogdan",
        "action": "raise",
        "headroom_after": 4,
        "basis_after": "measured",
        "delta": 4,
        "next_binding": [
          "resource:browser",
          "backend:GKE_AUTOPILOT"
        ]
      }
    ],
    "complete": true,
    "unread": [],
    "uncapped": [
      "runner:claude-code-browser"
    ]
  }
}

/** `headroom.blocked_reason_groups()`, verbatim. Same test pins it. */
const FIXTURE_REASON_GROUPS: Record<string, string[]> = {
  "needs_action": [
    "BUDGET_LIMIT",
    "DEPENDENCY",
    "MANUAL_PAUSE",
    "POOL_LIMIT_UNSET",
    "QUOTA_EXHAUSTED"
  ],
  "no_room": [
    "BACKEND_LIMIT",
    "COOLDOWN",
    "GLOBAL_CONCURRENCY_LIMIT",
    "PROVIDER_CONCURRENCY_LIMIT",
    "RESOURCE_CLASS_LIMIT",
    "RUNNER_LIMIT",
    "SCHEDULED_RETRY",
    "TENANT_LIMIT"
  ]
}

/**
 * `swarm_api.runnerinputs.input_contract()` over the frozen catalogue, verbatim.
 *
 * Served on every fixture profile so the submit screens' REQUIRED-KEYS path can
 * be reached without a deployment. Without it `requiredInputKeys` read null for
 * every profile, and a fixture session could only ever show the unread mark --
 * never the form refusing `claude-code` a blank prompt, which is the branch the
 * rebuilt flow exists for (docs/web-ui/ux-plan.md §1.2).
 *
 * A block for EVERY profile, empty lists included, for the reason
 * `input_contract` gives: an empty list is a measured "nothing required" and
 * must be reachable too. Strict JSON because
 * tests/unit/control_plane/test_ui_fixture_input_contract.py parses this literal
 * and compares it with the server's function -- so a runner that starts
 * demanding a key fails that test here rather than teaching the screen a rule
 * production does not apply.
 *
 * `inputs` ON claude-code, codex, claude-code-large AND claude-code-lightpanda, the profiles that take
 * MCP servers and declare nothing else a form draws (QAFIX6-FIXTURE f, run 6
 * overview-automate-019, box 6343758363): with no profile serving `inputs`,
 * `mcpProfiles` (McpServers.tsx) read the fixture as an API too old to say,
 * and the register form printed 'This API does not say…' under a table listing
 * docs-search for claude-code. Each block is `input_contract()`'s whole
 * `inputs`, which the test above holds it to; the other profiles still leave
 * it out, which that test permits. claude-code-browser takes MCP servers too,
 * but its block declares `qa_identity`, and serving it would offer the QA
 * identity on every fixture Submit form, which qa-identity.test.tsx holds the
 * fixture to not declaring. No fixture MCP server allows it, so the form and
 * the table still agree.
 *
 * The value type is NAMED, as FixtureAvailability's below is: the reader takes
 * the first `{` after the declaration as the literal. It widens
 * RunnerInputContract (types.ts) by the `inputs` the API serves and McpServers
 * reads through `unknown`.
 */
type FixtureInputContract = RunnerInputContract & { inputs?: Record<string, unknown> }
const FIXTURE_INPUT_CONTRACTS: Record<string, FixtureInputContract> = {
  "mock": {"required_keys": []},
  "generic": {"required_keys": []},
  "claude-code": {"required_keys": ["prompt"], "inputs": {"issue": {"kind": "integer", "minimum": 1, "maximum": 999999, "choices": [], "required": false, "means": "an issue in the task's repository: its title, body and comments are written to issue.md in the workspace and named in the prompt"}, "mcp_servers": {"kind": "list", "minimum": 1, "maximum": 4, "choices": [], "required": false, "means": "registered MCP servers, by name, the agent may call during its run; each must be registered and enabled for the task's workspace and allowed for this profile (docs/design/registered-mcp-servers.md)"}}},
  "codex": {"required_keys": ["prompt"], "inputs": {"issue": {"kind": "integer", "minimum": 1, "maximum": 999999, "choices": [], "required": false, "means": "an issue in the task's repository: its title, body and comments are written to issue.md in the workspace and named in the prompt"}, "mcp_servers": {"kind": "list", "minimum": 1, "maximum": 4, "choices": [], "required": false, "means": "registered MCP servers, by name, the agent may call during its run; each must be registered and enabled for the task's workspace and allowed for this profile (docs/design/registered-mcp-servers.md)"}}},
  "browser": {"required_keys": []},
  "merge": {"required_keys": []},
  "post-verdict": {"required_keys": []},
  "claude-code-review": {"required_keys": ["prompt"]},
  "indexer": {"required_keys": ["prompt"]},
  "claude-code-browser": {"required_keys": ["prompt"]},
  "claude-code-large": {"required_keys": ["prompt"], "inputs": {"issue": {"kind": "integer", "minimum": 1, "maximum": 999999, "choices": [], "required": false, "means": "an issue in the task's repository: its title, body and comments are written to issue.md in the workspace and named in the prompt"}, "mcp_servers": {"kind": "list", "minimum": 1, "maximum": 4, "choices": [], "required": false, "means": "registered MCP servers, by name, the agent may call during its run; each must be registered and enabled for the task's workspace and allowed for this profile (docs/design/registered-mcp-servers.md)"}}},
  "claude-code-lightpanda": {"required_keys": ["prompt"], "inputs": {"issue": {"kind": "integer", "minimum": 1, "maximum": 999999, "choices": [], "required": false, "means": "an issue in the task's repository: its title, body and comments are written to issue.md in the workspace and named in the prompt"}, "mcp_servers": {"kind": "list", "minimum": 1, "maximum": 4, "choices": [], "required": false, "means": "registered MCP servers, by name, the agent may call during its run; each must be registered and enabled for the task's workspace and allowed for this profile (docs/design/registered-mcp-servers.md)"}}},
  "await-input": {"required_keys": []},
  "fix-loop": {"required_keys": []}
}

/**
 * Whether each profile may be dispatched, as `/v1/capacity` now serves it
 * (CP-3, visual QA 2026-09-25). A fixture whose `codex` is on offer develops
 * Pools, Profile headroom and Submit against a platform that does not exist --
 * which is how the disabled branch of all three shipped unexercised.
 *
 * A COPY OF THE FROZEN CATALOGUE'S TWO FIELDS, and therefore a strict-JSON
 * literal: `test_capacity_serves_availability.py` reads it with the same
 * `json_literal` the input-contract table above is held by and compares it to
 * `RUNNER_PROFILES`, so re-enabling a profile there fails here until this
 * follows.
 *
 * The value type is NAMED rather than written inline: the reader takes the
 * first `{` after the declaration as the literal, and an inline object type
 * would be that brace.
 */
interface FixtureAvailability {
  available: boolean
  disabled_reason: string
}
const FIXTURE_AVAILABILITY: Record<string, FixtureAvailability> = {
  "mock": {"available": true, "disabled_reason": ""},
  "generic": {"available": true, "disabled_reason": ""},
  "claude-code": {"available": true, "disabled_reason": ""},
  "codex": {"available": false, "disabled_reason": "codex is disabled until the ChatGPT-plan pool lands: the quota broker leasing OpenAI plan accounts and the codex runner using them. A follow-up turns it on once both have merged. Use claude-code."},
  "browser": {"available": true, "disabled_reason": ""},
  "merge": {"available": true, "disabled_reason": ""},
  "post-verdict": {"available": false, "disabled_reason": "the merge chain (#295) is disabled for every tenant until signed step specs (#342) are enforced and the review and merge GitHub Apps exist."},
  "claude-code-review": {"available": false, "disabled_reason": "the merge chain (#295) is disabled for every tenant until signed step specs (#342) are enforced and the review and merge GitHub Apps exist."},
  "indexer": {"available": true, "disabled_reason": ""},
  "claude-code-browser": {"available": true, "disabled_reason": ""},
  "claude-code-large": {"available": true, "disabled_reason": ""},
  "claude-code-lightpanda": {"available": true, "disabled_reason": ""},
  "await-input": {"available": true, "disabled_reason": ""},
  "fix-loop": {"available": false, "disabled_reason": "fix-loop is not available yet: it ships unavailable until its acceptance run passes, as docs/design/fix-loop.md says."}
}

async function fixtureCapacity(): Promise<Result<Capacity>> {
  await new Promise((r) => setTimeout(r, 400))
  noteFixtureProbe(route('/v1/capacity'), 400, true)
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      generated_at: new Date(FIXTURE_NOW).toISOString(),
      tenant_id: 'u-bogdan',
      // The listing was not truncated, so a pool absent from it is
      // unconfigured rather than unread.
      pools_complete: true,
      blocked_reason_groups: FIXTURE_REASON_GROUPS,
      // Six real entries from the frozen catalogue, with the pool lists
      // pool_names_for builds for one tenant. Not invented: an empty map here
      // meant the headroom rows never rendered in development, which is how a
      // panel ships untested.
      runner_profiles: {
        mock: {
          resource_class: 'standard', backend: 'CLOUD_RUN_JOB', provider: null, units: 1,
          ...FIXTURE_AVAILABILITY.mock,
          pools: ['global', 'tenant:u-bogdan', 'resource:standard', 'runner:mock', 'backend:CLOUD_RUN_JOB'],
          admission: FIXTURE_ADMISSION.mock,
          input_contract: FIXTURE_INPUT_CONTRACTS.mock,
        },
        generic: {
          resource_class: 'standard', backend: 'CLOUD_RUN_JOB', provider: null, units: 1,
          ...FIXTURE_AVAILABILITY.generic,
          pools: ['global', 'tenant:u-bogdan', 'resource:standard', 'runner:generic', 'backend:CLOUD_RUN_JOB'],
          admission: FIXTURE_ADMISSION.generic,
          input_contract: FIXTURE_INPUT_CONTRACTS.generic,
        },
        'claude-code': {
          resource_class: 'standard', backend: 'CLOUD_RUN_JOB', provider: 'anthropic', units: 1,
          ...FIXTURE_AVAILABILITY['claude-code'],
          pools: [
            'global', 'tenant:u-bogdan', 'resource:standard', 'runner:claude-code',
            'backend:CLOUD_RUN_JOB', 'provider:anthropic', 'provider:anthropic:tenant:u-bogdan',
          ],
          admission: FIXTURE_ADMISSION['claude-code'],
          input_contract: FIXTURE_INPUT_CONTRACTS['claude-code'],
        },
        codex: {
          resource_class: 'standard', backend: 'CLOUD_RUN_JOB', provider: 'openai', units: 1,
          ...FIXTURE_AVAILABILITY.codex,
          pools: [
            'global', 'tenant:u-bogdan', 'resource:standard', 'runner:codex',
            'backend:CLOUD_RUN_JOB', 'provider:openai', 'provider:openai:tenant:u-bogdan',
          ],
          admission: FIXTURE_ADMISSION.codex,
          input_contract: FIXTURE_INPUT_CONTRACTS.codex,
        },
        browser: {
          resource_class: 'browser', backend: 'GKE_AUTOPILOT', provider: 'anthropic', units: 1,
          ...FIXTURE_AVAILABILITY.browser,
          pools: [
            'global', 'tenant:u-bogdan', 'resource:browser', 'runner:browser',
            'backend:GKE_AUTOPILOT', 'provider:anthropic', 'provider:anthropic:tenant:u-bogdan',
          ],
          admission: FIXTURE_ADMISSION.browser,
          input_contract: FIXTURE_INPUT_CONTRACTS.browser,
        },
        // Contract request 67: claude-code on the browser image, so it costs a
        // browser class (1 unit since contract request 79; 2 before) and runs
        // where the browser image must. ITS ADMISSION ROW IS SERVED
        // (QAFIX4-FIXTURE-B F10, run 4 capacity-004): without one, Can start and
        // Runs out first read a dash beside its full provider pool and Headroom
        // counted its seven pools unread. Its runner pool has no document, which
        // the analyser reports as uncapped, not unread.
        'claude-code-browser': {
          resource_class: 'browser', backend: 'GKE_AUTOPILOT', provider: 'anthropic', units: 1,
          ...FIXTURE_AVAILABILITY['claude-code-browser'],
          pools: [
            'global', 'tenant:u-bogdan', 'resource:browser', 'runner:claude-code-browser',
            'backend:GKE_AUTOPILOT', 'provider:anthropic', 'provider:anthropic:tenant:u-bogdan',
          ],
          admission: FIXTURE_ADMISSION['claude-code-browser'],
          input_contract: FIXTURE_INPUT_CONTRACTS['claude-code-browser'],
        },
      },
      pools: fixturePools(),
    },
  }
}

/**
 * THE POOL DOCUMENTS `/v1/capacity` serves, and `feeds_pool` names.
 *
 * TWO RULES THEY WERE TYPED APART FROM (QAFIX6-FIXTURE c and i, part of
 * #1680). `global` and `tenant:u-bogdan` count the leases the task list holds,
 * one unit each, because every lease is a unit of every pool on its profile's
 * list (invariant 2): typed as 5, they sat under Overview's 7 holding
 * capacity (run 6 task-007, box 6343775508). The 7 is a LITERAL because
 * tests/unit/control_plane/test_blocker_ui_surface.py reads these rows out of
 * this file, between `fixtureCapacity` and `fixtureTasks`, to regenerate
 * FIXTURE_ADMISSION; qafix6.fixture.test.tsx holds it to the task list, so a
 * live task added or removed fails there by name. And each provider-tenant pool
 * carries what its quota document's broker writes -- `quota_derived_limit`,
 * the document's cap, and its `adaptive_target` -- so its ceiling is the
 * least of those and its hard limit, as `SlotPool.effective_limit` computes,
 * and `available` is that ceiling less its leases (box 6343783860).
 */
function fixturePools(): Capacity['pools'] {
  const quota = new Map(fixtureQuotaRows().map((q) => [`provider:${q.provider}:tenant:${q.tenant_id}`, q]))
  const pool = (
    name: string,
    hard: number,
    active: number,
    extra: Partial<Capacity['pools'][number]> = {},
  ) => {
    const fed = quota.get(name)
    const adaptive = extra.adaptive_target ?? fed?.adaptive_target ?? null
    const derived = fed === undefined ? null : fed.effective_limit
    const effective = Math.max(0, Math.min(hard, adaptive ?? hard, derived ?? hard))
    return {
      name,
      hard_limit: hard,
      quota_derived_limit: derived,
      active,
      enabled: true,
      updated_at: new Date(FIXTURE_NOW).toISOString(),
      ...extra,
      adaptive_target: adaptive,
      effective_limit: effective,
      available: Math.max(0, effective - active),
    }
  }
  return [
    // 22, not 20, since it counts the seven leases: 22 - 7 = 15 keeps
    // FIXTURE_ADMISSION's `mock` capped by THREE pools tied at 15 (global,
    // resource:standard, backend:CLOUD_RUN_JOB), the shape that literal's note
    // says it exists to teach. At 20 global bound alone at 13.
    pool('global', 22, 7),
    pool('tenant:u-bogdan', 40, 7),
    pool('provider:anthropic', 10, 5),
    pool('provider:anthropic:tenant:u-bogdan', 5, 5),
    pool('provider:anthropic:tenant:eng', 5, 0),
    pool('provider:openai', 10, 0),
    pool('provider:openai:tenant:eng', 5, 0),
    // THE POOL openai u-bogdan's QUOTA ROW FEEDS (QAFIX4-FIXTURE-B F11, run 4
    // capacity-009): `pool_names_for` gives every provider its per-tenant
    // slice, so codex's list carries it and Provider quota's row links a
    // pool the board and Holders' accounting both list. At 10, so codex
    // stays tied, now across three ceilings (FIXTURE_ADMISSION's note).
    pool('provider:openai:tenant:u-bogdan', 10, 0),
    pool('runner:claude-code', 10, 5),
    pool('runner:mock', 20, 0),
    pool('runner:browser', 4, 0),
    pool('runner:codex', 10, 0),
    pool('runner:generic', 10, 0),
    pool('resource:standard', 20, 5),
    pool('resource:browser', 4, 0),
    pool('resource:large', 2, 0),
    pool('backend:CLOUD_RUN_JOB', 20, 5),
    pool('backend:GKE_AUTOPILOT', 4, 0),
    pool('tenant:smoke', 2, 0),
    // Paused, and at a reduced ceiling: the two states this screen must
    // make obvious at a glance.
    pool('tenant:eng', 10, 0, { enabled: false, adaptive_target: 6 }),
  ]
}

/** The questions the fixture's held merge names (#2313), by asking task and index. */
const FIXTURE_HELD = [
  { task_id: 'task_wait_asked_implement', step_id: 'implement', question_index: 0, question: 'Keep /agents/views as a redirect to the new Views page?' },
  { task_id: 'task_wait_asked_fix', step_id: 'fix', question_index: 1, question: 'Rename the views flag in this change or in a follow-up?' },
]

/** What each asking task of the fixture's held merge asked: `GET /v1/tasks/{id}/questions`. */
function fixtureHeldQuestions(taskId: string): TaskQuestions | null {
  const option = (label: string, description: string) => ({ label, description })
  const asked: Record<string, AgentQuestion[]> = {
    task_wait_asked_implement: [
      {
        question: 'Keep /agents/views as a redirect to the new Views page?',
        options: [
          option('Redirect', 'Old links keep working; the redirect is removed in a later release.'),
          option('Remove', 'Old links 404; nothing else changes.'),
        ],
        recommended: 'Redirect',
        context: 'Bookmarks and three runbooks link the old address.',
        answer: null,
      },
    ],
    task_wait_asked_fix: [
      {
        question: 'Add a test for the empty Views state?',
        options: [option('Yes', 'One more component test.'), option('No', 'Covered by the page test.')],
        recommended: 'Yes',
        context: null,
        answer: { question_index: 0, question: 'Add a test for the empty Views state?', choice: 'Yes', text: null, by: 'bogdan@example.com', at: new Date(FIXTURE_NOW - 30 * 60_000).toISOString(), history: [] },
      },
      {
        question: 'Rename the views flag in this change or in a follow-up?',
        options: [
          option('This change', 'One pull request; the review re-runs on the rename.'),
          option('Follow-up', 'This merges as reviewed; the rename is its own lane.'),
        ],
        recommended: 'Follow-up',
        context: null,
        answer: null,
      },
    ],
  }
  const questions = asked[taskId]
  if (questions === undefined) return null
  return { task_id: taskId, count: questions.length, status: 'ok', questions, redacted: false, redaction_count: 0, may_answer: true }
}

/**
 * ONE WAITING AGENT PER REASON (#1782, WAIT-UI-PANEL), each carrying the full
 * `waiting` object `GET /v1/tasks/{id}` serves (swarm_api/waiting_detail.py),
 * so the agent page's "Why it's waiting" panel renders every reason in
 * development: `/agents/waiting/work/task/task_wait_<reason>`.
 *
 * The CI one is the case that asked for the panel: SIZES-SCHED-2's merge step
 * on 2026-10-21, head 1968b677, five pending checks, two wakes, the attempts
 * refunded, and a history long enough to fold. Several facts are null on
 * purpose -- an unrecorded expiry, a place in line the API does not compute,
 * an unread dependency state -- because "not recorded" is a path the panel
 * must draw, and a fixture that filled every field would hide it.
 */
function fixtureWaitingTasks(listed: Task[]): Task[] {
  const base = listed.find((t) => t.state === 'PARKED') ?? listed[0]
  if (base === undefined) return []
  const now = FIXTURE_NOW
  const at = (minsAgo: number) => new Date(now - minsAgo * 60_000).toISOString()
  const one = (
    reason: string,
    summary: string,
    resumes: string,
    detail: Record<string, unknown>,
    over: { next?: number | null; since?: number | null; history?: Waiting['history']; task?: Partial<Task> } = {},
  ): Task => {
    const next = over.next === undefined ? -12 : over.next
    const nextAt = next === null ? null : at(next)
    const ready = reason === 'READY_CAPACITY'
    const since = over.since === undefined ? 47 : over.since
    const waiting: Waiting = {
      reason,
      summary,
      since: since === null ? null : at(since),
      next_check_at: nextAt,
      resumes_when: resumes,
      detail,
      // The history `_history_text` writes for the one event that began the
      // wait: a READY row's is the promotion, every other row's the park.
      history: over.history ?? [
        ready
          ? { at: at(since ?? 47), kind: 'ready', text: 'ready: promoted' }
          : { at: at(since ?? 47), kind: 'parked', text: `parked: ${reason}` },
      ],
      history_read: true,
      read_at: at(0),
    }
    // Its own times derive from its submission and its last change, as every
    // fixture task's do (`fixtureTimed`), not the listed task it is built on.
    return fixtureTimed({
      ...base,
      id: `task_wait_${reason.toLowerCase()}`,
      state: ready ? 'READY' : 'PARKED',
      park_reason: ready ? null : (reason as Task['park_reason']),
      blocked_by: null,
      next_eligible_at: nextAt,
      current_lease_id: null,
      created_at: at(60),
      updated_at: at(3),
      waiting,
      ...over.task,
    })
  }
  const checks = ['checkov', 'ci-gate', 'python (unit)', 'ui (typecheck + component)', 'integration (emulator)']
  const pr = { number: 1779, url: 'https://github.com/example-org/agent-swarm-infra/pull/1779' }
  return [
    one(
      'CI_PENDING',
      'CI on PR #1779: 5 checks pending (checkov, ci-gate, python (unit), ...); re-checked 2 times; next check soon',
      "its pull request's checks settle (swarm-api's wake tick marks the step), or the fallback re-check",
      {
        pull_request: pr,
        head: '1968b677e0c4a51f2d93b7c86a0e15d4f3b2a917',
        code: 'CI_PENDING',
        pending: checks,
        failed: ['shell'],
        wakes: 2,
        attempts_counted: false,
        last_checked_at: at(16),
        wake_requested_at: null,
      },
      {
        task: { workflow_id: 'wf_sizes_sched_2', step_id: 'merge' },
        history: [
          { at: at(47), kind: 'parked', text: 'parked: CI_PENDING, 5 checks pending' },
          { at: at(32), kind: 'ready', text: 'ready: woke on the fallback re-check (was CI_PENDING)' },
          { at: at(31), kind: 'running', text: 'running: an attempt started (a re-check, for a merge step)' },
          { at: at(31), kind: 'parked', text: 'parked: CI_PENDING, 5 checks pending, wake 1, attempt refunded' },
          { at: at(17), kind: 'ready', text: 'ready: woke on the fallback re-check (was CI_PENDING)' },
          { at: at(16), kind: 'running', text: 'running: an attempt started (a re-check, for a merge step)' },
          { at: at(16), kind: 'parked', text: 'parked: CI_PENDING, 5 checks pending, wake 2, attempt refunded' },
        ],
      },
    ),
    one(
      'READY_CAPACITY',
      'Ready; capacity binds on runner:merge (4 of 4 units in use)',
      'runner:merge has room for 1 units and the scheduler admits it',
      {
        pool: 'runner:merge', pool_state: 'full', active: 4, limit: 4, units: 1,
        place_in_line: null,
        place_in_line_unavailable: 'not computed: it would read every READY task behind the binding pool on each request',
        last_pass: [{ reason: 'RUNNER_LIMIT', pool: 'runner:merge', active: 4, limit: 4 }],
      },
      { next: null, since: 9 },
    ),
    one(
      'SCHEDULED_RETRY',
      'Retry scheduled: attempt 2 of 3 (worker_exit)',
      'the retry time passes and the scheduler re-admits it',
      {
        last_error: 'exit status 1: pytest collected 2 failures in tests/unit/control_plane/test_waiting_detail.py',
        attempt: 1, next_attempt: 2, max_attempts: 3, next_try_at: at(-4), cause: 'worker_exit',
      },
      { next: -4, since: 6 },
    ),
    one(
      'PROVIDER_QUOTA_EXHAUSTED',
      'Provider quota exhausted on anthropic (account acct-team-2)',
      "the provider's window passes and the scheduler re-admits it",
      { provider: 'anthropic', account: 'acct-team-2', provider_state: 'exhausted', source: 'worker', account_pool_reason: null, wait_seconds: 1800, until: at(-25) },
      { next: -25 },
    ),
    one(
      'PROVIDER_COOLDOWN',
      'Provider cooling down on anthropic',
      "the provider's window passes and the scheduler re-admits it",
      { provider: 'anthropic', account: null, provider_state: 'cooldown', source: null, account_pool_reason: null, wait_seconds: 300, until: at(-5) },
      { next: -5 },
    ),
    one(
      'PROVIDER_OUTAGE',
      'Provider outage on anthropic',
      "the provider's window passes and the scheduler re-admits it",
      { provider: 'anthropic', account: null, provider_state: 'outage', source: 'status page', account_pool_reason: null, wait_seconds: null, until: at(-10) },
      { next: -10 },
    ),
    one(
      'DEPENDENCY_INCOMPLETE',
      'Waiting on 2 upstream steps: implement (RUNNING), task_a3881bec (state not read)',
      'every upstream step it depends on has SUCCEEDED',
      {
        waiting_on: [
          { task_id: 'task_a073aff5', step_id: 'implement', state: 'RUNNING' },
          { task_id: 'task_a3881bec', step_id: null, state: null },
        ],
        states_read: true,
      },
      { next: null },
    ),
    one(
      'CHILDREN_INCOMPLETE',
      'Waiting for 1 of 2 children to finish (1 RUNNING)',
      'every child it submitted has finished',
      {
        children: [
          { task_id: 'task_8ea5c26d', state: 'RUNNING' },
          { task_id: 'task_99284d38', state: 'SUCCEEDED' },
        ],
        unfinished: 1, total: 2, states_read: true,
      },
      { next: null },
    ),
    one(
      'AWAITING_INPUT',
      'Waiting for a member of u-bogdan to answer; /sc answer wf_sizes_sched_2 approve',
      'a member of the workspace answers, or the window expires and its default applies',
      {
        who_may_answer: { tenant_id: 'u-bogdan', text: 'any member of workspace u-bogdan' },
        opened_at: at(47), expires_at: at(-240), answer_command: '/sc answer wf_sizes_sched_2 approve',
      },
      { next: null },
    ),
    // A real hand-off: a browser agent nobody took control of in time.
    one(
      'HUMAN_REQUIRED',
      'Waiting for a person: sign in on its console page, then say it is ready',
      "a member of the workspace calls human-ready (the console's Ready button)",
      {
        who_may_answer: { tenant_id: 'u-bogdan', text: 'any member of workspace u-bogdan' },
        expires_at: null, answer_route: 'POST /v1/tasks/task_wait_human_required/human-ready', cause: null,
      },
      { next: null, task: { runner_profile: 'claude-code-browser', resource_class: 'browser', blocked_by: [{ reason: 'HUMAN_REQUIRED' }] } },
    ),
    // A merge step held by the owner's questions (#2313): HUMAN_REQUIRED too,
    // but no browser and no Ready -- the questions, answered on the steps
    // that asked them (`fixtureHeldQuestions`).
    one(
      'HUMAN_REQUIRED',
      'Waiting for answers to 2 owner questions before merging (implement: 1, fix: 1)',
      'the last named question is answered (it resumes on its own)',
      {
        park_phase: 'owner_questions',
        questions: FIXTURE_HELD,
        unreadable: 0,
        who_may_answer: { submitted_by: 'bogdan@example.com', admin: true, text: 'bogdan@example.com, who launched workflow wf_ga_move_views, or an admin' },
      },
      {
        next: null,
        since: 9,
        history: [{ at: at(9), kind: 'parked', text: 'parked: HUMAN_REQUIRED, attempt refunded' }],
        task: {
          id: 'task_wait_owner_questions',
          runner_profile: 'merge',
          workflow_id: 'wf_ga_move_views',
          step_id: 'merge',
          blocked_by: [
            {
              reason: 'HUMAN_REQUIRED',
              park_phase: 'owner_questions',
              owner_questions: { code: 'owner_questions_open', questions: FIXTURE_HELD, unreadable: 0 },
            },
          ],
        },
      },
    ),
    one(
      'CREDENTIAL_MISSING',
      'Credential missing: anthropic key, github/token',
      "the workspace registers the credential and the scheduler's credential sweep finds it",
      {
        provider: 'anthropic', secret_names: ['anthropic key', 'github/token'], account_pool: null,
        forge_connection: null, workspace_secrets: [{ consumer: 'github', slot: 'token' }], mcp_servers: [],
      },
    ),
    one('BUDGET_EXHAUSTED', 'Budget exhausted', 'the budget is raised or its period resets', { budget: null }, { next: null }),
    one(
      'MANUAL_PAUSE',
      'Paused by alice@example.com',
      "what paused it clears and the scheduler's pause sweep returns it",
      { paused_by: 'alice@example.com', paused_at: at(47), error: null },
      { next: null },
    ),
  ]
}

/**
 * THE BUDGET AGENTS (#2459, BUDGET-FIXTURES): one per state of the agent's
 * Budget card (AgentBudget.tsx), so the card is drawn in development and a
 * visual-QA run captures it (`budget-*` in qa/routes.json). Like the waiting
 * agents they are reached BY ID AND KEPT OFF THE LISTS (BGD, 2026-10-27):
 * the agents list, the task window, Outcomes and every count stay as other
 * tests pin them.
 *
 * Their shapes are agent.budget.test.tsx's: a step's $1 under a $5 default,
 * the warning a `running` event carrying `cause: budget_warning` at $0.81,
 * and a stop at $1.02 on a turn boundary. The figures agree everywhere they
 * are shown: the warning's and the stop's `used`, the attempt's usage
 * (`fixtureUsage`), and the runner's report in the result summary.
 */
const FIXTURE_BUDGET_WARN = 'task_budget_warn'
const FIXTURE_BUDGET_EXCEEDED = 'task_budget_exceeded'
const FIXTURE_BUDGET_NONE = 'task_budget_none'

/** `dispatch.budget` as `swarm_api.budget.dispatch_block` writes it: a step's $1 under a $5 default. */
const FIXTURE_BUDGET = { usd: 1, from: { step: { usd: 1 }, workspace: { usd: 5 }, ceiling: { usd: 50 } } }

/** The 80% warning's detail, on a `running` event as the worker writes it today (contract request 121 gives it its own type). */
const FIXTURE_BUDGET_WARNING = {
  cause: 'budget_warning',
  percent: 80,
  told: true,
  limit: { usd: 1 },
  used: { tokens: 812_000, usd: 0.81 },
}

/** What the stopped agent's runner reported: 1.02M tokens and $1.02, the `used` its stop says. */
const FIXTURE_BUDGET_STOP_USAGE = {
  input_tokens: 18_400, output_tokens: 21_600, cache_read_input_tokens: 920_000, cache_creation_input_tokens: 60_000, cost_usd: 1.02,
}

/** What each budget agent's runner reported: tokens summing to the `used` its warning or its stop says. */
const FIXTURE_BUDGET_USAGE: Readonly<Record<string, NonNullable<ReturnType<typeof fixtureUsage>>>> = {
  [FIXTURE_BUDGET_WARN]: {
    input_tokens: 14_200, output_tokens: 17_800, cache_read_input_tokens: 732_000, cache_creation_input_tokens: 48_000, cost_usd: 0.81,
  },
  [FIXTURE_BUDGET_EXCEEDED]: FIXTURE_BUDGET_STOP_USAGE,
}

function fixtureBudgetTasks(listed: Task[]): Task[] {
  const base = listed.find((t) => t.runner_profile === 'claude-code') ?? listed[0]
  if (base === undefined) return []
  const at = (minsAgo: number) => new Date(FIXTURE_NOW - minsAgo * 60_000).toISOString()
  const dispatch = { strategy: 'collect', carrier: 'checkpoints', role: null, integrates: [] }
  // One attempt each, submitted `since` minutes ago and last changed `last`
  // minutes ago; `fixtureTimed` derives the rest, as every fixture task's.
  const one = (id: string, state: TaskState, since: number, last: number, over: Partial<Task>): Task =>
    fixtureTimed({
      ...base,
      id,
      state,
      runner_profile: 'claude-code',
      resource_class: 'standard',
      provider: 'anthropic',
      model: 'claude-opus-5',
      metadata: null,
      input: null,
      workflow_id: null,
      step_id: null,
      depends_on: null,
      park_reason: null,
      blocked_by: null,
      next_eligible_at: null,
      attempt_count: 1,
      current_generation: 1,
      current_lease_id: CONCURRENCY_STATES.has(state) ? `lease_${id.replace('task_', '')}` : null,
      created_at: at(since),
      updated_at: at(last),
      end_cause: null,
      last_error: null,
      result_summary: null,
      dispatch: dispatch as Task['dispatch'],
      ...over,
    })
  // The budgeted blocks are the codec's four keys plus `budget`, kept as
  // named values: an inline dispatch literal is held to exactly the four
  // (test_dispatch_ui_surface.py), which predate the budget.
  const warned = { ...dispatch, budget: FIXTURE_BUDGET } as unknown as Task['dispatch']
  const stopped = { ...dispatch, strategy: 'direct-pr', budget: FIXTURE_BUDGET } as unknown as Task['dispatch']
  const usage = FIXTURE_BUDGET_STOP_USAGE
  const exceeded = one(FIXTURE_BUDGET_EXCEEDED, 'FAILED', 50, 22, {
    dispatch: stopped,
    end_cause: 'budget_exceeded',
    last_error:
      'budget_exceeded: the agent used $1.02 of a budget of $1.00 and was stopped at a turn boundary; ' +
      'its work was checkpointed and published as it stood',
  })
  return [
    // A RUNNING agent past 80% of its budget: the amber warning.
    one(FIXTURE_BUDGET_WARN, 'RUNNING', 30, 0, {
      dispatch: warned,
      heartbeat_at: at(1),
    }),
    // A FAILED agent the worker stopped on its budget (contract request 118),
    // its work published: a `direct-pr` step, so its pull request and the
    // Changes pane's publish agree with the card's "published as it stood".
    {
      ...exceeded,
      result_summary: {
        budget: { limit: { usd: 1 }, used: { tokens: 1_020_000, usd: 1.02 }, warned: true, end_cause: 'budget_exceeded', stopped: 'turn_boundary' },
        git: {
          ...fixtureGit(exceeded),
          pull_request: { number: 2470, url: 'https://github.com/example-org/agent-swarm-infra/pull/2470', state: 'open', created: true },
        },
        runner: {
          usage: {
            input_tokens: usage.input_tokens,
            output_tokens: usage.output_tokens,
            total_cost_usd: usage.cost_usd,
            models: ['claude-opus-5'],
          },
        },
      } as unknown as Task['result_summary'],
    },
    // A SUCCEEDED agent with no budget: the card draws nothing.
    one(FIXTURE_BUDGET_NONE, 'SUCCEEDED', 90, 80, {}),
  ]
}

/**
 * THE AGENT ANOTHER USER SUBMITTED (#1857, RAW-PATCH-FIXTURE; BGD's decision
 * 2026-10-27). The raw patch is offered to a task's submitter only, and every
 * other fixture task was submitted by the signed-in fixture user, so the
 * console only ever showed the offer. This one finished agent was submitted by
 * someone else: its Artifacts tab lists the same `diff.patch` with the
 * redacted download alone, beside any of the user's own agents (for example
 * `task_99284d38`), which also offer "Download raw patch".
 *
 * Reached BY ID AND KEPT OFF THE LISTS, as the budget agents are, so every
 * list and count other tests pin stays where it was. Everything else is a
 * finished claude-code agent's ordinary shape; only `submitted_by` differs.
 */
const FIXTURE_OTHER_SUBMITTER = 'task_patch_other_submitter'

function fixtureOtherSubmitterTasks(listed: Task[]): Task[] {
  const base = listed.find((t) => t.state === 'SUCCEEDED' && t.runner_profile === 'claude-code') ?? listed[0]
  if (base === undefined) return []
  const at = (minsAgo: number) => new Date(FIXTURE_NOW - minsAgo * 60_000).toISOString()
  return [
    fixtureTimed({
      ...base,
      id: FIXTURE_OTHER_SUBMITTER,
      state: 'SUCCEEDED',
      submitted_by: 'dave@example.com',
      metadata: { title: 'Tidy the lease test fixtures' },
      input: null,
      workflow_id: null,
      step_id: null,
      depends_on: null,
      attempt_count: 1,
      current_generation: 1,
      current_lease_id: null,
      created_at: at(70),
      // Its end: `fixtureTimed` derives the start and the completion from it.
      updated_at: at(64),
      last_error: null,
      dispatch: { strategy: 'collect', carrier: 'checkpoints', role: null, integrates: [] },
    }),
  ]
}

/** A task a per-task read answers and no list returns: a waiting one (#1782), a budget one (#2459), or another user's (#1857). */
function fixtureOffList(listed: Task[], taskId: string): Task | undefined {
  return (
    fixtureWaitingTasks(listed).find((t) => t.id === taskId) ??
    fixtureBudgetTasks(listed).find((t) => t.id === taskId) ??
    fixtureOtherSubmitterTasks(listed).find((t) => t.id === taskId)
  )
}

// ---------------------------------------------------------------------------
// THE FIXTURE TIMELINE (QAFIX4-FIXTURE-B, visual QA run 4 F12, F14a-c; part
// of #1680). Every time a fixture read shows for a task -- its submission, its
// latest start and end, each attempt's admission, start and end, its events,
// its checkpoints, its logs' attempt -- derives from ONE task document: its
// `created_at` (submitted), its `updated_at` (its last change: now while it
// holds capacity, its end once it has one) and its attempt counter. They were
// minted apart, so attempt 3 started before its task was submitted, every
// checkpoint belonged to an attempt that wrote none, and every workflow step
// ran 5m 0s. Fixture-only: nothing on the live path calls any of this.
// ---------------------------------------------------------------------------

/** One attempt of a fixture task, in epoch ms. */
interface FixtureAttemptSpan {
  n: number
  admitted: number
  started: number | null
  ended: number | null
  exit: number | null
  error: string | null
  checkpoints: string[]
}

/** A stable small number per id, so a task's run length and figures do not move between reads. */
function fixtureSeed(id: string): number {
  let h = 0
  for (const c of id) h = (h * 31 + c.charCodeAt(0)) % 100_003
  return h
}

/** How long a task's attempts ran, per task (2-8 minutes and some seconds), never past 80% of `windowMs`. */
function fixtureRunMs(id: string, windowMs: number): number {
  const h = fixtureSeed(id)
  return Math.min(((2 + (h % 7)) * 60 + (h % 50)) * 1000, Math.floor(windowMs * 0.8))
}

/** The attempt id `fixtureAttempts` serves for attempt `n` of a task. */
function fixtureAttemptId(taskId: string, n: number): string {
  return `att_${taskId.slice(-4)}_${n}`
}

/**
 * A fixture task's attempts, oldest first, from its own document.
 *
 * One per `attempt_count`, none for a task never admitted (generation 0).
 * Each attempt has its own slice of the task's span, in order. The canonical
 * three-attempt shape keeps what it always taught: attempt 1 never started
 * (dispatch rejected), attempt 2 ran and failed, and the newest follows the
 * task's state -- open while it holds capacity, not started while LEASED or
 * DISPATCHED. Only the newest attempt that ran wrote checkpoints, the two the
 * listing serves; ids are the listing's, so the records and the bucket agree.
 */
function fixtureSpans(task: Task): FixtureAttemptSpan[] {
  const k = task.current_generation === 0 ? 0 : task.attempt_count
  if (k <= 0) return []
  const begin = Date.parse(task.created_at)
  const end = Date.parse(task.updated_at)
  const live = CONCURRENCY_STATES.has(task.state)
  const slice = (end - begin) / k
  const spans: FixtureAttemptSpan[] = []
  for (let n = 1; n <= k; n++) {
    const admitted = Math.round(begin + (n - 1) * slice + 0.1 * slice)
    if (n < k && k >= 3 && n === 1) {
      spans.push({ n, admitted, started: null, ended: null, exit: null, error: 'BACKEND_REJECTED', checkpoints: [] })
    } else if (n < k) {
      spans.push({ n, admitted, started: admitted, ended: Math.round(admitted + 0.6 * slice), exit: 1, error: 'exit status 1', checkpoints: [] })
    } else {
      const started = task.state === 'LEASED' || task.state === 'DISPATCHED' ? null : admitted
      const ended = live ? null : end
      const exit = ended === null ? null : task.state === 'FAILED' ? fixtureFailedExitCode(task.last_error) : 0
      const ran = started !== null && task.state !== 'STARTING'
      spans.push({
        n, admitted, started, ended, exit,
        error: exit ? `exit status ${exit}` : null,
        checkpoints: ran ? ['ckpt_0001', 'ckpt_0002'] : [],
      })
    }
  }
  return spans
}

/** Where a fixture task's checkpoints are listed (`fixtureCheckpoints`' prefix). */
function fixtureCheckpointPrefix(taskId: string): string {
  return `tenants/u-bogdan/tasks/${taskId}/checkpoints`
}

/**
 * The task's own times, from its timeline: `started_at` is its LATEST start
 * (stepviews' `ran` and the header's Elapsed both read it so), `completed_at`
 * its end once terminal, and `latest_checkpoint` the newest checkpoint its
 * attempts wrote -- the listing's pointer -- or none.
 */
function fixtureTimed(task: Task): Task {
  const spans = fixtureSpans(task)
  const started = [...spans].reverse().find((sp) => sp.started !== null)?.started ?? null
  const written = spans[spans.length - 1]?.checkpoints ?? []
  const pointer = written[written.length - 1]
  return {
    ...task,
    started_at: started === null ? null : new Date(started).toISOString(),
    completed_at: TERMINAL_STATES.has(task.state) ? task.updated_at : null,
    latest_checkpoint: pointer === undefined ? null : `${fixtureCheckpointPrefix(task.id)}/${pointer}`,
  }
}

/** A task's newest attempt in the fixture, or undefined when it was never admitted. */
function fixtureNewestSpan(task: Task): FixtureAttemptSpan | undefined {
  const spans = fixtureSpans(task)
  return spans[spans.length - 1]
}

/**
 * The task document a per-task fixture read answers from: the listed one, a
 * waiting one (#1782), or -- for an id neither has, as a component test hands
 * one -- the first finished task under that id, so the read answers in the
 * same shape it always did rather than inventing a second one.
 */
function fixtureTaskDoc(taskId: string): Task | undefined {
  const listed = fixtureTaskRows()
  const known = listed.find((t) => t.id === taskId) ?? fixtureOffList(listed, taskId)
  if (known !== undefined) return known
  const stand = listed.find((t) => t.state === 'SUCCEEDED')
  return stand === undefined ? undefined : { ...stand, id: taskId }
}

/**
 * What a finished attempt's runner reported, per task, so no two read alike.
 * mock and generic report nothing (the frozen catalogue says only claude-code
 * and codex produce usage); `task_wf_scan_a` is a MEASURED zero, the digit
 * case; `task_f523c160` reported tokens and no cost, which must not read as
 * free.
 */
function fixtureUsage(task: Task): Pick<
  AttemptRow,
  'input_tokens' | 'output_tokens' | 'cache_read_input_tokens' | 'cache_creation_input_tokens' | 'cost_usd'
> | null {
  if (task.runner_profile === 'mock' || task.runner_profile === 'generic') return null
  const budgeted = FIXTURE_BUDGET_USAGE[task.id]
  if (budgeted !== undefined) return budgeted
  if (task.id === 'task_wf_scan_a') {
    return { input_tokens: 0, output_tokens: 0, cache_read_input_tokens: 0, cache_creation_input_tokens: 0, cost_usd: 0 }
  }
  const h = fixtureSeed(task.id)
  return {
    input_tokens: 6_000 + (h % 17) * 911,
    output_tokens: 900 + (h % 13) * 233,
    cache_read_input_tokens: 40_000 + (h % 23) * 9_871,
    cache_creation_input_tokens: 3_000 + (h % 11) * 1_207,
    cost_usd: task.id === 'task_f523c160' ? null : Math.round((0.05 + (h % 40) * 0.0113) * 10_000) / 10_000,
  }
}

async function fixtureTasks(): Promise<Result<TaskPage>> {
  await new Promise((r) => setTimeout(r, 350))
  noteFixtureProbe(route('/v1/tasks'), 350, true)
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: { tasks: [...fixtureTasksMade, ...fixtureTaskRows()], next_page_token: null, tenant_id: 'u-bogdan' },
  }
}

/** The fixture's task list, built at one instant: `fixtureTasks` serves it, and the stats and workflow rows count it. */
function fixtureTaskRows(): Task[] {
  const now = FIXTURE_NOW
  const at = (minsAgo: number) => new Date(now - minsAgo * 60_000).toISOString()

  // The fan-out that actually ran on 2026-09-19: fourteen script fixes, one
  // failure reclaimed three times, plus a workflow. Real shapes, real states.
  const mk = (
    id: string,
    state: TaskState,
    profile: string,
    minsAgo: number,
    extra: Partial<Task> = {},
  ): Task => timed(minsAgo, {
    id,
    tenant_id: 'u-bogdan',
    state,
    runner_profile: profile,
    resource_class: profile === 'browser' || profile === 'claude-code-browser' ? 'browser' : 'standard',
    provider: profile === 'mock' ? null : 'anthropic',
    priority: 0,
    created_at: at(minsAgo),
    // The three times below are set by `timed` from the task's timeline.
    updated_at: at(minsAgo),
    started_at: null,
    completed_at: null,
    submitted_by: 'alice@example.com',
    // A NEVER-ADMITTED TASK COUNTS NO ATTEMPT (QA1 overview-agents-58): the
    // generation below is 0 for QUEUED and READY, and `fixtureSpans` serves
    // them no attempt, so a counter of 1 drew `counts 1 · returned 0`.
    attempt_count: state === 'FAILED' ? 3 : state === 'QUEUED' || state === 'READY' ? 0 : 1,
    max_attempts: 3,
    park_reason: null,
    blocked_by: null,
    workflow_id: null,
    step_id: null,
    depends_on: null,
    cancel_requested: false,
    repository_url: 'https://github.com/example-org/agent-swarm-infra',
    model: profile === 'mock' ? null : 'claude-opus-5',
    timeout_seconds: 3600,
    // Only a PARKED task has one. Left null elsewhere on purpose: a date here
    // on a RUNNING task would imply a retry that is not scheduled. AFTER THE
    // CLOCK (QAFIX6-FIXTURE f, run 6 overview-automate-015): `minsAgo - 15`
    // put the 24-minute-old parked step's at 11:51 with the page at 12:00, a
    // time to wait for that had already passed.
    next_eligible_at: state === 'PARKED' ? at(-6) : null,
    metadata: null,
    repository_ref: 'main',
    // THE FENCING PAIR, as `task_to_api` now serves it. Two different rules,
    // both taken from the contract rather than drawn by eye here:
    //
    // A LEASE IS HELD ONLY IN THE FOUR CONCURRENCY STATES (invariant 1).
    // QUEUED, PARKED, READY and every terminal state cost nothing, and
    // `control.park` and `control.finish` both write `current_lease_id: None`
    // on the way out. A fixture that gave every row a lease id would show a
    // released slot as held, on the screens built to find held slots.
    //
    // THE GENERATION IS NEVER RESET, so a finished task keeps the one it ran
    // at, and only a never-admitted task is at 0. It equals `attempt_count`
    // unless a reconciler fenced a stale worker; `task_19d91b82` overrides it
    // in `extra` below to the fenced shape, which is the live twenty-minute
    // stall and the one row that exercises the glyph.
    current_generation:
      state === 'QUEUED' || state === 'READY' ? 0 : state === 'FAILED' ? 3 : 1,
    current_lease_id: CONCURRENCY_STATES.has(state)
      ? `lease_${id.replace('task_', '')}`
      : null,
    // `task_to_api` sends this on every task, filling the API's defaults for a
    // task that predates the feature -- so the fixture default is the same pair
    // and the rows that differ say so in `extra`. A fixture that omitted it
    // would exercise only the "this API did not report a dispatch" path, which
    // is the one production should never take.
    dispatch: { strategy: 'collect', carrier: 'checkpoints', role: null, integrates: [] },
    input: null,
    last_error: state === 'FAILED' ? 'exit status 1: pytest collected 2 failures' : null,
    result_summary: state === 'SUCCEEDED' ? { artifacts: 1, checkpoints: 2 } : null,
    latest_checkpoint: null,
    ...extra,
  })
  // ITS LAST CHANGE, from which `fixtureTimed` derives the rest: now while it
  // holds capacity; its end once terminal, after a run of its own length;
  // the park or release of its last attempt when it waits again; its
  // submission when nothing was ever admitted.
  function timed(minsAgo: number, task: Task): Task {
    const span = minsAgo * 60_000
    const last =
      task.current_generation === 0 || task.attempt_count === 0
        ? now - span
        : CONCURRENCY_STATES.has(task.state)
          ? now
          : TERMINAL_STATES.has(task.state)
            ? now - span + fixtureRunMs(task.id, span)
            : now - span + fixtureRunMs(task.id, span * 0.6)
    return fixtureTimed({ ...task, updated_at: new Date(last).toISOString() })
  }

  return [
    // THE REFERENCE AGENT, on its third attempt: attempt 1 was refused at
    // dispatch and attempt 2 failed, so the Attempts tab, the events and
    // the checkpoints all have more than one attempt to agree about.
    mk('task_a073aff5', 'RUNNING', 'claude-code', 4, { attempt_count: 3, current_generation: 3 }),
    // A LONE AGENT WITH A TITLE (SUMMARY-TITLE): its Agents row reads the
    // title, beside task_a073aff5 above, which has none and reads
    // `claude-code agent · a073aff5`. The summary view serves metadata as
    // the title alone, so this is the whole of what the list sees. No
    // `(#N)` in it: the copy sweep (walk.copy.test.tsx) reads every page
    // this build draws and refuses an issue number in visible copy.
    mk('task_8ea5c26d', 'RUNNING', 'claude-code', 5, {
      metadata: { title: 'Fix flaky lease heartbeat test' },
    }),
    // THE BROWSER QA AGENT: the one fixture task with a live browser, so
    // the Browser pane has a live session to draw in this build, and the
    // QA route `task-browser` (qa/routes.json) captures it rather than an
    // ordinary agent's empty pane (QAFIX2-QABROWSER, owner decision
    // 2026-10-21). `loadLiveSession` answers `live_browser: true` for this
    // id alone. task_a073aff5 stays the ordinary running agent the tests
    // name. The prompt is the label a QA reader recognises it by.
    mk('task_b40e5a1c', 'RUNNING', 'claude-code-browser', 6, {
      input: { prompt: 'Browser QA agent: open https://example.com and report what the page says.' },
    }),
    // THE FENCED SHAPE, which is the twenty-minute DISPATCHED stall that
    // exposed the missing fields: generation 2 against attempt_count 1,
    // still naming the generation-1 lease that was never released. This is
    // the row that tells a developer whether an indicator reads correctly;
    // with every fixture row at generation == attempt_count, a glyph that
    // never fires and a glyph that is not wired look identical.
    mk('task_19d91b82', 'DISPATCHED', 'claude-code', 3, {
      input: { prompt: 'Make the reconciler release a lease whose generation is stale.' },
      current_generation: 2,
      current_lease_id: 'lease_723362a25968460aae35',
    }),
    // WHAT EACH OF THE FIVE NEWEST LONE AGENTS DID (visual QA pass 2,
    // runs2-10): with no title and no prompt, Submit's "Start from a recent
    // one" listed all five as `claude-code agent · <hex>`. Each has the
    // prompt a person would have typed; the list reads its first line.
    mk('task_92eb4807', 'STARTING', 'claude-code', 2, {
      input: { prompt: 'Add a test for a park that outlives its provider wait.' },
    }),
    mk('task_460409cd', 'LEASED', 'claude-code', 1, {
      input: { prompt: 'Show the queue depth per pool on the Capacity page.' },
    }),
    // A READY ROW SAYS WHICH POOL REFUSES IT (visual QA pass 1,
    // overview-agents-56): the sidebar named `anthropic · u-bogdan` full and
    // this row named nothing. Its `waiting_for` is the capacity fixture's own
    // claude-code blocker, the per-tenant anthropic slice at 5 of 5.
    mk('task_950b8e4a', 'READY', 'claude-code', 1, {
      input: { prompt: 'Rename the dispatch strategies in the API docs.' },
      waiting_for: {
        as_of: new Date(FIXTURE_NOW).toISOString(),
        admissible_now: false,
        holds_capacity: false,
        lead: { pool: 'provider:anthropic:tenant:u-bogdan', state: 'full', active: 5, limit: 5, units: 1, reason: 'PROVIDER_CONCURRENCY_LIMIT' },
        pools: [{ pool: 'provider:anthropic:tenant:u-bogdan', state: 'full', active: 5, limit: 5, units: 1, reason: 'PROVIDER_CONCURRENCY_LIMIT' }],
        complete: true,
      },
    }),
    mk('task_e7d210b6', 'QUEUED', 'claude-code', 0, {
      input: { prompt: 'Write the runbook for rotating a tenant git secret.' },
    }),
    // THE FAILED TASK WHOSE CLI REPORTED SUCCESS (QA-115, kept as built):
    // its transcript ends `result · success` and the task still failed
    // after it, which is the case Logs explains. The only one: every other
    // failed task's transcript ends in its error (`fixtureTranscript`).
    // SUBMITTED 52 MINUTES BEFORE THE CLOCK, NOT 42 (QAFIX6-FIXTURE b, run 6
    // flow-find-failure-desktop-01): at 42 it ended 6 s from task_fan_3, so
    // both read '34m ago' and 'the most recently failed' had no answer on the
    // page. At 52 it ends ten minutes earlier, still inside its workflow's
    // order (research at 58, review cancelled at 62).
    mk('task_8e33a3de', 'FAILED', 'claude-code', 52, {
      metadata: { title: FIXTURE_WORKFLOW_TITLES.wf_bcdc9180e4fb4a209f31 },
    }),
    mk('task_a3881bec', 'SUCCEEDED', 'claude-code', 58, {
      metadata: { title: FIXTURE_WORKFLOW_TITLES.wf_bcdc9180e4fb4a209f31 },
    }),
    mk('task_99284d38', 'SUCCEEDED', 'claude-code', 50),
    mk('task_f278adec', 'SUCCEEDED', 'claude-code', 48),
    // THE FINISHED BROWSER AGENT (FIXTURE-BROWSER-DONE, part of #2423; BGD
    // 2026-10-26 "Convert one"): this row was an ordinary claude-code one
    // no test named, converted rather than a row added so every fixture
    // total stands. A finished agent's Browser tab (BT-006, BT-007) renders
    // from it: its end time, the links to Attempts and Artifacts, and the
    // recordings panel, empty here because the fixture build does not serve
    // recordings. task_b40e5a1c stays the one RUNNING browser agent.
    mk('task_f523c160', 'SUCCEEDED', 'claude-code-browser', 47, {
      input: { prompt: 'Browser QA agent: open https://example.com, follow its one link, and report both pages.' },
    }),
    mk('task_812d51aa', 'SUCCEEDED', 'claude-code', 46),
    mk('task_108ef29c', 'SUCCEEDED', 'mock', 90),
    mk('task_1beb89a5', 'CANCELLED', 'claude-code', 62, {
      metadata: { title: FIXTURE_WORKFLOW_TITLES.wf_bcdc9180e4fb4a209f31 },
    }),
    // A standalone `direct-pr` task: the one shape that opens a pull
    // request on its own, so the row chip and the Code panel's published
    // path are both reachable in development.
    mk('task_c41d90b7', 'SUCCEEDED', 'claude-code', 20, {
      dispatch: { strategy: 'direct-pr', carrier: 'checkpoints', role: null, integrates: [] },
    }),
    // Part of a workflow, so the graph view has something with edges.
    //
    // THE WORKFLOW IS AN `integrate` ONE, and the roles are the real ones
    // `_step_dispatch` assigns: every step a contributor except the single
    // sink, whose `integrates` is the topological prefix in apply order.
    // A `collect` fixture would have left the integrator badge, the
    // contributor publish panel and the one-pull-request rollup unrendered
    // in development -- which is how a panel ships untested.
    // Two attempts: a retry that predates usage capture, then one with figures.
    mk('task_wf_plan', 'SUCCEEDED', 'claude-code', 30, {
      workflow_id: 'wf_audit_01', step_id: 'plan', depends_on: [],
      attempt_count: 2, current_generation: 2,
      metadata: { title: FIXTURE_WORKFLOW_TITLES.wf_audit_01 },
      dispatch: { strategy: 'integrate', carrier: 'checkpoints', role: 'contributor', integrates: [] },
    }),
    mk('task_wf_scan_a', 'SUCCEEDED', 'claude-code', 24, {
      workflow_id: 'wf_audit_01', step_id: 'scan-scripts', depends_on: ['plan'],
      metadata: { title: FIXTURE_WORKFLOW_TITLES.wf_audit_01 },
      dispatch: { strategy: 'integrate', carrier: 'checkpoints', role: 'contributor', integrates: [] },
    }),
    mk('task_wf_scan_b', 'RUNNING', 'claude-code', 24, {
      workflow_id: 'wf_audit_01', step_id: 'scan-terraform', depends_on: ['plan'],
      metadata: { title: FIXTURE_WORKFLOW_TITLES.wf_audit_01 },
      dispatch: { strategy: 'integrate', carrier: 'checkpoints', role: 'contributor', integrates: [] },
    }),
    // PARKED, not "BLOCKED". There is no BLOCKED task state -- a step
    // waiting on a dependency is PARKED with DEPENDENCY_INCOMPLETE.
    mk('task_wf_report', 'PARKED', 'claude-code', 24, {
      workflow_id: 'wf_audit_01', step_id: 'report',
      metadata: { title: FIXTURE_WORKFLOW_TITLES.wf_audit_01 },
      depends_on: ['scan-scripts', 'scan-terraform'],
      park_reason: 'DEPENDENCY_INCOMPLETE',
      // The listing row's `waiting` (#1782, WAIT-API), in the sentence
      // `swarm_api/waiting_detail.py` writes, so the Waiting tab's short
      // reason ("waiting on scan-terraform") is reachable in development.
      waiting: { reason: 'DEPENDENCY_INCOMPLETE', summary: 'Waiting on 1 upstream steps: scan-terraform (RUNNING)' },
      dispatch: {
        strategy: 'integrate', carrier: 'checkpoints', role: 'integrator',
        integrates: ['task_wf_plan', 'task_wf_scan_a', 'task_wf_scan_b'],
      },
    }),
    // THE FAN-IN, reproduced from the live workflow that proved the graph
    // could not express one: `wf_5e5ad3b6f7da4299a839`, five independent
    // steps and a sixth depending on ALL five. Rendered by the old view it
    // was indistinguishable from a chain of five, so a fixture that only
    // ever held the two-fork shape would have let that ship again.
    //
    // The step ids are the real ones, `allornothing` included, because the
    // join's dependency line is the string that used to be ellipsed and its
    // length is the reason.
    ...(['cold-start', 'fencing', 'allornothing', 'checkpoints', 'absentzero'].map(
      (stepId, i) =>
        mk(`task_fan_${i}`, i === 3 ? 'FAILED' : 'SUCCEEDED', 'claude-code', 40 - i, {
          workflow_id: 'wf_5e5ad3b6f7da4299a839', step_id: stepId, depends_on: [],
          metadata: { title: FIXTURE_WORKFLOW_TITLES.wf_5e5ad3b6f7da4299a839 },
          // A failure that names a code other than 1, so the fixture's
          // attempts are seen following `last_error` rather than a
          // constant (pytest exits 2 when collection is interrupted).
          ...(i === 3 ? { last_error: 'exit status 2: pytest was interrupted: 1 error during collection' } : {}),
          dispatch: { strategy: 'integrate', carrier: 'checkpoints', role: 'contributor', integrates: [] },
        }),
    )),
    // CANCELLED BEFORE IT RAN: `checkpoints` failed, so the join was never
    // admitted -- no attempt, no start, no `ran`, no figures.
    mk('task_fan_join', 'CANCELLED', 'claude-code', 35, {
      workflow_id: 'wf_5e5ad3b6f7da4299a839', step_id: 'synthesis',
      depends_on: ['cold-start', 'fencing', 'allornothing', 'checkpoints', 'absentzero'],
      attempt_count: 0, current_generation: 0,
      metadata: { title: FIXTURE_WORKFLOW_TITLES.wf_5e5ad3b6f7da4299a839 },
      dispatch: {
        strategy: 'integrate', carrier: 'checkpoints', role: 'integrator',
        integrates: ['task_fan_0', 'task_fan_1', 'task_fan_2', 'task_fan_3', 'task_fan_4'],
      },
    }),
  ]
}

/**
 * THE TITLE EACH FIXTURE WORKFLOW'S STEPS CARRY (QAFIX4-FIXTURE-B F14c, run 4
 * flow-workflow-graph-light-01): the list names a workflow by its steps'
 * `metadata.title` (`workflowLabel`, stepviews.ts), and no fixture step had
 * one, so all three read 'Untitled workflow'.
 */
const FIXTURE_WORKFLOW_TITLES = {
  wf_audit_01: 'Audit the operator scripts and the Terraform',
  wf_5e5ad3b6f7da4299a839: 'Check the five contract invariants, then write one synthesis',
  wf_bcdc9180e4fb4a209f31: 'Research, draft and review the restore runbook',
} as const

async function fixtureWorkflowBoard(): Promise<Result<WorkflowBoard>> {
  await new Promise((r) => setTimeout(r, 300))
  noteFixtureProbe(route('/v1/workflows'), 300, true)
  // A route a non-admin genuinely cannot read, so the strip's 403 cell -- the
  // one that must read as information rather than breakage -- is visible in
  // development instead of only in production.
  noteFixtureProbe(route('/v1/admin/dispatch'), 120, false)

  // Reuse the task fixture so the join is a REAL join: if a step_id or task_id
  // stops matching, the fixture shows "state unknown" exactly as production
  // would, instead of quietly carrying a state of its own.
  const tasks = await fixtureTasks()
  const taskById = new Map<string, Task>()
  if (tasks.status === 'ok') for (const t of tasks.data.tasks) taskById.set(t.id, t)

  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: { taskById, statesDetail: null, workflows: fixtureWorkflowRows(tasks.status === 'ok' ? tasks.data.tasks : undefined) },
  }
}

/**
 * The workflow rows, ONCE, because two screens read them.
 *
 * `loadWorkflowBoard` (the graph) and `loadWorkflows` (Overview's progress
 * check) hit the same route in production, so a fixture that gave them
 * different rows would let the two screens disagree in development about what
 * exists -- which is precisely the class of defect the live UI audit kept
 * finding.
 */
function fixtureWorkflowRows(rows: Task[] = fixtureTaskRows()): Workflow[] {
  // A WORKFLOW SPANS ITS STEPS (QAFIX4-FIXTURE-B F14c, run 4
  // flow-workflow-graph-light-12): created when its first step task was,
  // last changed when its last one was. Typed apart, the fan-in's 3h 31m
  // matched neither interval its steps drew.
  const byId = new Map(rows.map((t) => [t.id, t]))
  const spanOf = (taskIds: string[]): { created_at: string; updated_at: string } => {
    const ts = taskIds.map((id) => byId.get(id)).filter((t): t is Task => t !== undefined)
    const first = Math.min(...ts.map((t) => Date.parse(t.created_at)))
    const last = Math.max(...ts.map((t) => Date.parse(t.updated_at)))
    return { created_at: new Date(first).toISOString(), updated_at: new Date(last).toISOString() }
  }
  const step = (
    step_id: string,
    depends_on: string[],
    task_id: string | null,
    // A MAP, as the API serves it -- upstream step_id -> artifact filename.
    // This fixture built a bare string, which is the shape nothing ever sends;
    // a fixture that disagrees with the response is a development screen that
    // renders correctly and a production one that does not.
    input_from: Record<string, string> = {},
    // The step's input as the workflow route serves it, masked by its task's
    // masker (QA2-FIX-13, overview-agents-50): the prompt the workflow gave it.
    input: Record<string, unknown> = {},
  ) => ({
    step_id,
    runner_profile: 'claude-code',
    resource_class: 'standard',
    depends_on,
    input_from,
    timeout_seconds: 3600,
    task_id,
    input,
  })

  const workflows: Workflow[] = [
    {
      workflow_id: 'wf_audit_01',
      tenant_id: 'u-bogdan',
      // The fixture mirrors what the API now sends: a DERIVED state, the
      // stored copy beside it, and the drift between them. `wf_audit_01`
      // carries a deliberate disagreement so the drift line has something
      // to render in development.
      state: 'RUNNING',
      stored_state: 'QUEUED',
      state_source: 'derived',
      rollup: {
        state: 'RUNNING',
        complete: true,
        reason: 'steps_hold_capacity',
        // Counted from the steps' tasks below the list, never typed here.
        counts: {},
        unreadable_steps: [],
        unstarted_steps: ['publish'],
        steps_read: 4,
      },
      drift: {
        stored: 'QUEUED',
        derived: 'RUNNING',
        agrees: false,
        reason: 'steps_hold_capacity',
        steps_read: 4,
        unreadable_steps: [],
        repaired: true,
      },
      ...spanOf(['task_wf_plan', 'task_wf_scan_a', 'task_wf_scan_b', 'task_wf_report']),
      submitted_by: 'alice@example.com',
      priority: 0,
      on_step_failure: 'FAIL_WORKFLOW',
      cancel_requested: false,
      steps: [
        step('plan', [], 'task_wf_plan'),
        step('scan-scripts', ['plan'], 'task_wf_scan_a', { plan: 'plan.md' }),
        step('scan-terraform', ['plan'], 'task_wf_scan_b', { plan: 'plan.md' }),
        step('report', ['scan-scripts', 'scan-terraform'], 'task_wf_report'),
        // No task_id: the workflow has not reached it. Renders as
        // "not started", which is NOT the same as "state unknown".
        step('publish', ['report'], null),
      ],
    },
      // THE SHAPE THE GRAPH EXISTS TO DISTINGUISH, reproduced from the live
      // workflow that proved the view could not express one:
      // `wf_5e5ad3b6f7da4299a839` on 2026-09-22, five independent steps and a
      // sixth depending on ALL five. Rendered by the old view it was
      // indistinguishable from a chain of five -- one `THEN` bar between the
      // five and the sixth, the identical mark a linear workflow draws -- and
      // the dependency line that would have disambiguated it was ellipsed at
      // `allorn...`. Five edges now arrive at `synthesis` and four would arrive
      // at the last step of a chain, so the two no longer render alike.
      //
      // JOINED to the six `task_fan_*` documents in the task fixture, so this
      // row exercises the derived rollup and the census as well as the shape. A
      // fixture whose only workflow is a two-way join never shows the case that
      // was got wrong, and one with no tasks never shows the heading deriving.
      //
      // The step ids are the real ones, `allornothing` included, because the
      // join's dependency line is the string that used to be ellipsed and its
      // length is the reason.
      {
        workflow_id: 'wf_5e5ad3b6f7da4299a839',
        tenant_id: 'u-bogdan',
        state: 'FAILED',
        stored_state: 'FAILED',
        state_source: 'derived',
        rollup: {
          state: 'FAILED',
          complete: true,
          reason: 'terminal_worst_first',
          counts: {},
          unreadable_steps: [],
          unstarted_steps: [],
          steps_read: 6,
        },
        ...spanOf(['task_fan_0', 'task_fan_1', 'task_fan_2', 'task_fan_3', 'task_fan_4', 'task_fan_join']),
        submitted_by: 'alice@example.com',
        priority: 0,
        on_step_failure: 'FAIL_WORKFLOW',
        cancel_requested: false,
        steps: [
          step('cold-start', [], 'task_fan_0'),
          step('fencing', [], 'task_fan_1'),
          step('allornothing', [], 'task_fan_2'),
          step('checkpoints', [], 'task_fan_3', {}, { prompt: 'Audit the checkpoint writer: every running attempt checkpoints on its period, and a parked one before it exits.' }),
          step('absentzero', [], 'task_fan_4'),
          // `input_from` MIRRORS `depends_on` here, and it is a MAP -- upstream
          // step_id to artifact filename -- because that is what the API serves
          // and what `WorkflowStep.input_from` is typed as. This row was first
          // written with a bare string, which is the shape nothing ever sends.
          step(
            'synthesis',
            ['cold-start', 'fencing', 'allornothing', 'checkpoints', 'absentzero'],
            'task_fan_join',
            {
              'cold-start': 'cold-start.md',
              fencing: 'fencing.md',
              allornothing: 'allornothing.md',
              checkpoints: 'checkpoints.md',
              absentzero: 'absentzero.md',
            },
          ),
        ],
      },
      {
        // AN API OLDER THAN `rollup.py`, which is what production was still
        // running on 2026-09-22: `state` straight off the Firestore document,
        // no `state_source: 'derived'`, no `rollup`, no `drift`. This payload
        // is the one that printed "QUEUED" over steps reading succeeded, failed
        // and cancelled, so it has to be renderable in development or the fix
        // for it cannot be looked at. The heading must claim NO state here.
        workflow_id: 'wf_bcdc9180e4fb4a209f31',
        tenant_id: 'u-bogdan',
        state: 'QUEUED',
        stored_state: 'QUEUED',
        state_source: 'stored',
        ...spanOf(['task_a3881bec', 'task_8e33a3de', 'task_1beb89a5']),
        submitted_by: 'alice@example.com',
        priority: 0,
        on_step_failure: 'FAIL_WORKFLOW',
        cancel_requested: false,
        steps: [
          step('research', [], 'task_a3881bec'),
          step('draft', ['research'], 'task_8e33a3de'),
          step('review', ['draft'], 'task_1beb89a5'),
        ],
      },
  ]
  // THE ROLLUP COUNTS ITS OWN STEPS (QAFIX6-FIXTURE i, run 5b QA-042, box
  // 6343779183): Workflows' Steps done reads `rollup.counts.SUCCEEDED`, and
  // wf_audit_01's were typed as one succeeded beside two step tasks the
  // Timeline drew as succeeded. Counted here as `rollup.py` counts them: each
  // step's task state, `unstarted` for a step with no task yet.
  return [...fixtureWorkflowsMade, ...workflows].map((w) => {
    if (w.rollup === undefined || w.rollup === null) return w
    const counts: Record<string, number> = {}
    for (const st of w.steps) {
      const key = !st.task_id ? 'unstarted' : (byId.get(st.task_id)?.state ?? 'unreadable')
      counts[key] = (counts[key] ?? 0) + 1
    }
    return { ...w, rollup: { ...w.rollup, counts } }
  })
}

/**
 * THE WORKFLOWS MADE IN THIS PAGE'S LIFE (QA2-FIX-07, runs2-02 and runs2-24):
 * by Submit a workflow and by an approved plan, newest first. No step has a
 * task yet, so each reads QUEUED with every step unstarted, as `rollup.py`
 * derives a workflow none of whose steps has started (`steps_not_started`).
 */
const fixtureWorkflowsMade: Workflow[] = []

type FixtureStepAsked = { step_id: string; depends_on: string[]; runner_profile: string; resource_class?: string; input_from?: Record<string, string> }

function fixtureMakeWorkflow(steps: FixtureStepAsked[], by: string, priority: number, onStepFailure = 'FAIL_WORKFLOW'): Workflow {
  const at = new Date().toISOString()
  const id = `wf_${Array.from(crypto.getRandomValues(new Uint8Array(10)), (b) => b.toString(16).padStart(2, '0')).join('')}`
  const workflow: Workflow = {
    workflow_id: id, tenant_id: fixtureActingTenant(),
    state: 'QUEUED', stored_state: 'QUEUED', state_source: 'derived',
    rollup: {
      state: 'QUEUED', complete: true, reason: 'steps_not_started', counts: {},
      unreadable_steps: [], unstarted_steps: steps.map((st) => st.step_id), steps_read: 0,
    },
    created_at: at, updated_at: at, submitted_by: by, priority, on_step_failure: onStepFailure, cancel_requested: false,
    steps: steps.map((st) => ({
      step_id: st.step_id, runner_profile: st.runner_profile, resource_class: st.resource_class ?? 'standard',
      depends_on: st.depends_on, input_from: st.input_from ?? {}, timeout_seconds: 3600, task_id: null,
    })),
  }
  fixtureWorkflowsMade.unshift(workflow)
  return workflow
}

/** SubmitWorkflow.tsx's `DispatchEcho`: what the create says it will do with the steps' work. */
type FixtureDispatchEcho = { strategy: string; carrier: string; integrator_step_id: string | null }

/**
 * `POST /v1/workflows` in the fixture build (QA2-FIX-07, runs2-02): the
 * workflow the body asks for, created now and readable by its id after, as
 * the 201 serves it (`state_source: 'stored'` on the create response). The
 * fixture checks nothing the form has not; the live route is the gate.
 */
export async function fixtureSubmitWorkflow(body: Record<string, unknown>): Promise<{ workflow: Workflow; dispatch: FixtureDispatchEcho }> {
  await new Promise((r) => setTimeout(r, 120))
  noteFixtureProbe(route('/v1/workflows'), 120, true)
  const asked = Array.isArray(body.steps) ? (body.steps as Record<string, unknown>[]) : []
  const made = fixtureMakeWorkflow(
    asked.map((st, i) => ({
      step_id: typeof st.step_id === 'string' ? st.step_id : `step-${i + 1}`,
      depends_on: Array.isArray(st.depends_on) ? st.depends_on.filter((d): d is string => typeof d === 'string') : [],
      runner_profile: typeof st.runner_profile === 'string' ? st.runner_profile : 'claude-code',
      resource_class: typeof st.resource_class === 'string' ? st.resource_class : undefined,
      input_from: typeof st.input_from === 'object' && st.input_from !== null && !Array.isArray(st.input_from) ? (st.input_from as Record<string, string>) : undefined,
    })),
    'alice@example.com',
    typeof body.priority === 'number' ? body.priority : 0,
    typeof body.on_step_failure === 'string' ? body.on_step_failure : 'FAIL_WORKFLOW',
  )
  const { rollup: _rollup, ...stored } = made
  // The create's echo (`DispatchEcho`), not a task's dispatch block.
  const echo = {
    strategy: typeof body.strategy === 'string' ? body.strategy : 'collect',
    carrier: typeof body.carrier === 'string' ? body.carrier : 'checkpoints',
    integrator_step_id: null,
  }
  return { workflow: { ...stored, state_source: 'stored' }, dispatch: echo }
}

/**
 * THE AGENTS STARTED IN THIS PAGE'S LIFE (QA2-FIX-07, runs2-01): Start one
 * agent answered a 404 in the fixture build. Listed first and readable by id,
 * QUEUED and never admitted, as a 201 leaves a task.
 */
const fixtureTasksMade: Task[] = []

/** `POST /v1/tasks` in the fixture build: the task the body asks for, as the 201 serves it. */
export async function fixtureSubmitTask(body: Record<string, unknown>): Promise<{ task: Task; scheduler_woken: boolean }> {
  await new Promise((r) => setTimeout(r, 120))
  noteFixtureProbe(route('/v1/tasks'), 120, true)
  const at = new Date().toISOString()
  const id = `task_${Array.from(crypto.getRandomValues(new Uint8Array(4)), (b) => b.toString(16).padStart(2, '0')).join('')}`
  // A never-admitted row of the list, so every field the list serves is there.
  const like = fixtureTaskRows().find((t) => t.state === 'QUEUED' && t.workflow_id === null) ?? fixtureTaskRows()[0]
  const profile = typeof body.runner_profile === 'string' ? body.runner_profile : 'claude-code'
  const task: Task = {
    ...(like as Task),
    id, tenant_id: fixtureActingTenant(), state: 'QUEUED', runner_profile: profile,
    created_at: at, updated_at: at, started_at: null, completed_at: null,
    submitted_by: 'alice@example.com', attempt_count: 0, current_generation: 0, current_lease_id: null,
    park_reason: null, blocked_by: null, workflow_id: null, step_id: null, depends_on: null, cancel_requested: false,
    priority: typeof body.priority === 'number' ? body.priority : 0,
    input: typeof body.input === 'object' && body.input !== null ? (body.input as Task['input']) : null,
    repository_url: typeof body.repository_url === 'string' ? body.repository_url : like?.repository_url ?? null,
    next_eligible_at: null, metadata: null, last_error: null, result_summary: null, latest_checkpoint: null,
  }
  fixtureTasksMade.unshift(task)
  return { task, scheduler_woken: true }
}

/**
 * The same rows as a PAGE, for Overview's workflow check.
 *
 * `rollup_report` is filled in as the route would fill it for this page --
 * one workflow examined, one written because the fixture's stored state
 * deliberately disagrees, nothing truncated and no read budget spent. A
 * fixture that omitted it would exercise the `undefined` branch of every
 * provenance test on this screen and never the populated one.
 */
async function fixtureWorkflows(): Promise<Result<WorkflowPage>> {
  await new Promise((r) => setTimeout(r, 300))
  noteFixtureProbe(route('/v1/workflows'), 300, true)
  const workflows = fixtureWorkflowRows()
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      workflows,
      next_page_token: null,
      tenant_id: 'u-bogdan',
      rollup_report: {
        examined: workflows.length,
        written: 1,
        agreed: 0,
        disagreed: 1,
        unknown: 0,
        truncated: false,
        step_reads: 4,
        step_read_budget_exhausted: false,
      },
    },
  }
}

// --------------------------------------------------------------------------
// The account pool
// --------------------------------------------------------------------------
// These are quota-broker's routes, reached through swarm-api. That is not a
// layering preference: refreshing an OAuth credential REVOKES the token it
// replaces, so the broker is the platform's single writer for subscription
// credentials and two writers racing on one account brick it. Nothing in this
// file may reach Secret Manager, mint a token, or "helpfully" retry a refresh.
//
// WHICH OF THEM SWARM-API PASSES THROUGH, because this file has been wrong
// about that in both directions. `routes/accounts.py` registers GET "",
// POST "", POST /authorize, POST /exchange, POST /{id}/refresh,
// PUT /{id}/lending, PUT /{id}/model-classes, PUT /{id}/name, PUT /{id}/state and
// DELETE /{id}; the `AccountPool` protocol in `brokerclient.py` -- which is
// "deliberately not a generic HTTP client", so there is no fallthrough -- has
// a method for each of those and for nothing else.
//
// THE TWO SIGN-IN ROUTES ARE AMONG THEM NOW. For one deploy they were not,
// and this comment carried a long apology for it in place of a proxy; the
// proxy is built (`begin_sign_in` and `finish_sign_in` in routes/accounts.py),
// and forwards to quota-broker's own `POST /v1/accounts/authorize` and
// `POST /v1/accounts/exchange` (`begin_account_authorization` and
// `finish_account_authorization` in quota_broker/main.py).
//
// THE PROXY'S BODIES ARE NARROWER THAN THE BROKER'S, and that is not cosmetic.
// `AccountSignInStart` is `{label, lend_to}` and `AccountSignInFinish` is
// `{state, code}` (swarm_api/schemas.py), both on `StrictModel`, whose
// `model_config` is `extra="forbid"`. A body carrying `owner_tenant` or
// `provider`, which this file used to send because the BROKER takes them, is
// refused 422 by FastAPI before any handler runs: not a message about the
// sign-in, and the first thing an operator adding a real account would have
// met. The tenant comes from the verified token and the provider is fixed to
// SUBSCRIPTION_PROVIDER inside the proxy, so neither is this page's to send.
//
// A 404 or a 405 from either path therefore no longer means "this gap is
// known": it means the API answering is not the one in this repository, and
// `SignInFailure` in Accounts.tsx says that rather than blaming the request.
// Adding a paste box back here to route around one would reinstate the flow
// this screen exists to remove.

/**
 * Everything the Accounts screen needs, in one load.
 *
 * Two reads, and only the first decides whether there is a screen:
 *
 *  - `/v1/accounts` IS the screen. A failure here is a failure. It also
 *    supplies the owner: `routes/accounts.py` echoes `tenant_id` from the
 *    VERIFIED TOKEN rather than from the broker's answer, precisely so a page
 *    can never be told it is looking at a tenant it is not. That makes it the
 *    right value to register under and to display, and strictly better than
 *    `/v1/tenants/me` -- it is the same value the register route will file the
 *    account under, not a second read that could disagree with it.
 *  - `/v1/admin/tenants` is only for offering lending suggestions. A non-admin
 *    genuinely cannot read it, so its absence is information and lending falls
 *    back to typed tenant ids with that said on screen.
 */
export interface AccountsBoard {
  page: AccountsPage
  /** null means the tenant list was not readable. An EMPTY array means there are none. */
  tenants: Tenant[] | null
  tenantsDetail: string | null
  /**
   * WHEN THIS BOARD WAS READ, as a browser instant.
   *
   * Carried on the data because `Screen` hands `children` the data alone, and
   * anything below that compares a server-supplied instant against
   * `Date.now()` needs it. This board only reloads on a mutation, so a row
   * whose window resets while the page sits open produces a reset instant in
   * the past with nothing wrong anywhere -- and a screen that cannot say how
   * old the figure is has to guess at a cause for that. It is not a
   * measurement of the platform's clock and must not be used as one: it is the
   * age of what is on screen, which is the part that IS measurable here.
   */
  readAt: number
  /**
   * True only on the development fixture's board. Accounts' Holding now and
   * History reads live in Accounts.tsx and had no fixture, so in development
   * they always drew a 404 (visual QA V017); the board that came from the
   * fixture is what tells them to answer from it too, without importing the
   * build flag into a module the tests mock.
   */
  fixture?: true
}

export async function loadAccountsBoard(): Promise<Result<AccountsBoard>> {
  if (USE_FIXTURES) return fixtureAccountsBoard()

  const [accounts, tenants] = await Promise.all([
    // `() => false`, NOT `accounts.length === 0`, and the reason is the whole
    // point of the screen: Screen renders `empty` INSTEAD of its children, so
    // an empty pool would hide the register form -- the one control that fixes
    // an empty pool. Zero accounts is handled inside the body as a state panel
    // sitting above a form that is still there.
    read<AccountsPage>(route('/v1/accounts'), () => false),
    read<{ tenants: Tenant[] }>(route('/v1/admin/tenants'), () => false),
  ])

  if (accounts.status === 'loading' || accounts.status === 'error' || accounts.status === 'empty') {
    return accounts as Result<AccountsBoard>
  }

  const tenantList =
    tenants.status === 'ok' || tenants.status === 'stale' ? tenants.data.tenants
    : tenants.status === 'empty' ? []
    : null
  const tenantsDetail =
    tenantList === null
      ? tenants.status === 'error' || tenants.status === 'stale'
        ? tenants.error.message
        : 'The tenant list read did not complete.'
      : null

  return {
    status: accounts.status,
    fetchedAt: accounts.fetchedAt,
    // Only an `ok` read carries the server's own `generated_at`; a `stale` one
    // is by definition not reporting a fresh server time, and Screen reads the
    // age off `fetchedAt` in that case.
    ...(accounts.status === 'ok' ? { serverAt: accounts.serverAt } : {}),
    // `accounts.fetchedAt` rather than `Date.now()`: on a `stale` read this is
    // the instant of the read that produced these rows, which is older, and it
    // is the rows' age that a reader needs.
    data: { page: accounts.data, tenants: tenantList, tenantsDetail, readAt: accounts.fetchedAt },
  } as Result<AccountsBoard>
}

/**
 * ADDING AN ACCOUNT IS A SIGN-IN. There is no keychain paste on this surface
 * and there must not be one again.
 *
 * The old flow asked a person to run `security find-generic-password`, copy a
 * JSON blob and paste it without reformatting it. That was a bad experience
 * and it was also fragile: the commonest failure was a hand-edited value that
 * parsed on the operator's machine and not here, reported as a 422 about a
 * missing refresh token, which named the symptom rather than the cause.
 *
 * It is now two calls. `authorize` returns a URL the person opens; they sign
 * in to Claude as they normally would; the callback page DISPLAYS a code; they
 * paste it and `exchange` redeems it. The PKCE verifier stays server-side,
 * keyed by `state`, so nothing secret passes through this file in either
 * direction -- the request carries a code, and the response carries an
 * account and an expiry.
 *
 * `POST /v1/accounts`, the keychain route, still exists in the API. It is
 * deliberately not called from anywhere in this app: keeping one paste box
 * "just in case" is how the flow that was removed comes back.
 */
export async function beginAccountSignIn(body: {
  /**
   * The label, and nothing the proxy does not have a field for.
   *
   * NO `owner_tenant` AND NO `provider`, both of which this call used to send
   * because quota-broker's own route takes them. `AccountSignInStart` is a
   * `StrictModel`, so an extra key is a 422 raised by validation -- a refusal
   * about the shape of the request, not about the sign-in, on the one control
   * this screen exists for. The tenant is resolved from the verified token by
   * the proxy and the provider is fixed there; a page cannot influence either,
   * which is the rule every other account route already follows.
   */
  label: string
  lend_to: string[]
}): Promise<Result<AccountAuthorization>> {
  const res = USE_FIXTURES
    ? await fixtureBeginSignIn(body)
    : await write(route('/v1/accounts/authorize'), 'POST', body)
  if (res.status !== 'ok') return res as Result<AccountAuthorization>

  const b = res.data
  // A 200 WITHOUT A URL IS NOT A SUCCESS. Rendering a "sign in" button that
  // opens `undefined` would put the failure two clicks away from its cause,
  // in a new tab, where the message explaining it is not.
  if (!isRecord(b) || typeof b.authorize_url !== 'string' || typeof b.state !== 'string') {
    return {
      status: 'error',
      error: {
        kind: 'server_error',
        httpStatus: 200,
        code: null,
        message:
          'The platform answered the sign-in request without a URL to open, so there is nothing to sign in to. Nothing was registered and nothing was changed.',
      },
    }
  }
  // Checked because this value is handed to `window.open`. It comes from our
  // own API and should always be the provider's https URL; a scheme check
  // costs nothing and is the difference between a bad deployment and a bad
  // deployment that opens a `javascript:` URL in the operator's browser.
  if (!/^https:\/\//i.test(b.authorize_url)) {
    return {
      status: 'error',
      error: {
        kind: 'server_error',
        httpStatus: 200,
        code: null,
        message:
          'The platform returned a sign-in URL this page will not open, because it is not an https address. Nothing was registered and nothing was changed. This is a deployment fault rather than anything you did.',
      },
    }
  }
  const ttl = b.expires_in_seconds
  return {
    status: 'ok',
    fetchedAt: res.fetchedAt,
    data: {
      authorize_url: b.authorize_url,
      state: b.state,
      // Absent stays absent. See `AccountAuthorization.expires_in_seconds`:
      // a countdown to a deadline nobody reported is invented arithmetic.
      expires_in_seconds: typeof ttl === 'number' && Number.isFinite(ttl) ? ttl : null,
    },
  }
}

/**
 * `POST /v1/accounts/exchange`. The paste, redeemed.
 *
 * SENT VERBATIM. The callback page renders `<code>#<state>` and people paste
 * what is on screen; `split_pasted_code` in the broker is the one reader of
 * that shape, and a second implementation here would disagree with it the
 * first time the page changed -- by refusing a paste the platform would have
 * accepted. So nothing is split, trimmed into fields or normalised here.
 *
 * A FAILED EXCHANGE DOES NOT ALWAYS LEAVE THE SIGN-IN OPEN, and this comment
 * used to say that it did. Four refusals, reading
 * `finish_account_authorization` in quota_broker/main.py:
 *
 *  - a paste whose state belongs to ANOTHER sign-in is refused before the
 *    pending record is even read, so this one is untouched and still open;
 *  - a token-endpoint refusal -- a mistyped, spent or expired code -- happens
 *    after the record is read and before `ref.delete()`, which runs only once
 *    an account exists, so that one is still open too;
 *  - "this sign-in took too long" deletes the pending record for age. It is
 *    raised BEFORE the token endpoint is called, so no credential was
 *    redeemed and NOTHING WAS CREATED;
 *  - "this sign-in has expired or was already completed" is raised because the
 *    record is ABSENT, and the one place that deletes a record without also
 *    raising is the line after `_provision_and_register` returns. So the
 *    commonest way to reach this message is that the sign-in SUCCEEDED -- the
 *    account exists, and on a re-auth its credential has just been replaced.
 *
 * THE FIRST TWO LEAVE THE SIGN-IN REDEEMABLE. The last two end it, and they
 * end it in OPPOSITE DIRECTIONS: one means nothing happened, the other means
 * it probably all happened. A caller that merges them writes "nothing was
 * created" over an account that now exists. `exchangeRefusal` in
 * Accounts.tsx is where they are told apart, and it matches the broker's own
 * wording positively so that an unrecognised refusal promises nothing rather
 * than promising the commonest thing.
 */
export async function finishAccountSignIn(body: {
  state: string
  code: string
}): Promise<Result<AccountExchangeResponse>> {
  const res = USE_FIXTURES
    ? await fixtureFinishSignIn(body)
    : await write(route('/v1/accounts/exchange'), 'POST', body)
  if (res.status !== 'ok') return res as Result<AccountExchangeResponse>

  const b = res.data
  if (!isRecord(b) || !isRecord(b.account) || typeof b.account.account_id !== 'string') {
    // A 201 whose body does not name the account is not "nothing happened".
    // The code may well have been redeemed and the account registered, and
    // telling somebody to try again would spend a single-use code on a
    // sign-in that had already succeeded.
    //
    // `httpStatus: 201` IS LOAD-BEARING, not decoration. `classify` only ever
    // builds an ApiError for a response that was not ok, so 201 cannot arrive
    // from a real refusal -- which makes it an exact marker, and
    // `exchangeRefusal` in Accounts.tsx keys its `accepted_unnamed` treatment
    // off it rather than off this sentence. The advice rendered under this
    // message has to agree with it; it used to say "pasting again costs
    // nothing" directly below "do not sign in again yet".
    return {
      status: 'error',
      error: {
        kind: 'server_error',
        httpStatus: 201,
        code: null,
        message:
          'The platform accepted the sign-in but did not say which account it created. The code may already have been redeemed, so do not sign in again yet — refresh the pool and look for the label first.',
      },
    }
  }
  const expires = b.expires_at
  return {
    status: 'ok',
    fetchedAt: res.fetchedAt,
    data: {
      account: b.account as unknown as Account,
      expires_at: typeof expires === 'string' ? expires : null,
      note: typeof b.note === 'string' ? b.note : undefined,
    },
  }
}

/** `PUT /v1/accounts/{id}/lending`. The whole list, not a delta. */
export async function setAccountLending(
  accountId: string,
  lendTo: string[],
): Promise<Result<unknown>> {
  if (USE_FIXTURES) return fixtureLending(accountId, lendTo)
  return write(route('/v1/accounts/{account_id}/lending', { account_id: accountId }), 'PUT', { lend_to: lendTo })
}

/**
 * An account as the broker serves it once it carries `model_classes` (owner
 * decision R5, 2026-10-17). EMPTY MEANS EVERY CLASS, and so does ABSENT: an
 * older broker serves no such field, and an account it lists has always
 * served every class, so reading absent as "none" would claim every step on
 * it was parked. Local to this file and Accounts.tsx until the field moves
 * into `Account` in types.ts.
 */
/**
 * `account_to_api` as served, beyond `types.ts` `Account`: the classes it
 * serves, and the pool's answer per account (#2222: `can_take_new_agent`,
 * `blocked_by`, `next_eligible_at`, `max_agents`, the organisation and the
 * duplicate marks), typed in accountsPool.ts.
 */
export type ServedAccount = Account & PoolFields & { model_classes?: string[] }

/**
 * `PUT /v1/accounts/{id}/model-classes`. The whole list, not a delta, exactly
 * like lending -- and `[]` is EVERY class, not none. A step whose class no
 * account serves waits parked and holds nothing, so narrowing this is never
 * an outage the platform hides; it is one the Accounts screen has to say.
 */
export async function setAccountModelClasses(
  accountId: string,
  modelClasses: string[],
): Promise<Result<unknown>> {
  if (USE_FIXTURES) return fixtureModelClasses(accountId, modelClasses)
  return write(route('/v1/accounts/{account_id}/model-classes', { account_id: accountId }), 'PUT', {
    model_classes: modelClasses,
  })
}

/**
 * `PUT /v1/accounts/{id}/name` (#1913): set the account's Name, or `null` to
 * clear it so the label shows again. Sent as typed; the broker trims it and
 * refuses an empty, over-60-character or control-character Name with a 422 in
 * its own words, which the Rename editor draws. Display text only: the id, the
 * secret, the holds and the lending do not change.
 */
export async function setAccountName(accountId: string, displayName: string | null): Promise<Result<unknown>> {
  if (USE_FIXTURES) return fixtureName(accountId, displayName)
  return write(route('/v1/accounts/{account_id}/name', { account_id: accountId }), 'PUT', {
    display_name: displayName,
  })
}

/**
 * `PUT /v1/accounts/{id}/max-agents` (#2222): how many agents the account may
 * hold at once, 1 to `MAX_AGENTS_LIMIT`. Owner only. swarm-api checks the range
 * before it asks the broker and answers a 422 in the broker's words, which
 * the detail's Change editor draws. Lowering it moves no agent already on it.
 */
export async function setAccountMaxAgents(accountId: string, maxAgents: number): Promise<Result<unknown>> {
  if (USE_FIXTURES) return fixtureMaxAgents(accountId, maxAgents)
  return write(route('/v1/accounts/{account_id}/max-agents', { account_id: accountId }), 'PUT', {
    max_agents: maxAgents,
  })
}

// ---------------------------------------------------------------------------
// One account's health (#2302, ACCOUNT-HEALTH-SHEET)
// ---------------------------------------------------------------------------
// `GET /v1/accounts/{id}/health` (ACCOUNT-429-SCOPE, PR 2305) as
// `routes/accounts.py` `health_view` serves it: allow-lists, not types, on
// both sides. The broker owns the record; swarm-api serves a value outside
// its lists as nothing, and this read keeps what it does not know as nothing
// too -- a count it was not sent is `null`, never a 0, because a 0 drawn for a
// count nobody measured is the one thing the Health section must never show.

/** The four words, by the broker's rule (`accountstore.health_status`). */
export type AccountHealthStatus = 'healthy' | 'limited' | 'erroring' | 'reauth_needed'
export const ACCOUNT_HEALTH_STATUSES: readonly AccountHealthStatus[] = ['healthy', 'limited', 'erroring', 'reauth_needed']

/** `ACCOUNT_HEALTH_KINDS` in swarm-api's schemas.py, in its order. */
export type AccountHealthKind =
  | 'rate_limit'
  | 'exhausted'
  | 'auth'
  | 'overloaded'
  | 'server_error'
  | 'network'
  | 'assign_refused'
  | 'reauth'
  | 'refresh_failed'
export const ACCOUNT_HEALTH_KINDS: readonly AccountHealthKind[] = [
  'rate_limit',
  'exhausted',
  'auth',
  'overloaded',
  'server_error',
  'network',
  'assign_refused',
  'reauth',
  'refresh_failed',
]

/** `ACCOUNT_HEALTH_WINDOWS`: the window a rate-limit event names. */
export type AccountHealthWindow = 'five_hour' | 'seven_day' | 'other'
const HEALTH_WINDOWS: readonly AccountHealthWindow[] = ['five_hour', 'seven_day', 'other']

/** One span's counts by kind. Every kind is present; a kind the broker did not send is 0 (swarm-api's `_health_counts`). */
export type AccountHealthCounts = Record<AccountHealthKind, number>

export interface AccountHealthEvent {
  at: string
  kind: AccountHealthKind
  /** null for a borrower reading another workspace's event, which also has no task and no message. */
  tenant_id: string | null
  task_id: string | null
  window: AccountHealthWindow | null
  reset_at: string | null
  /** Redacted twice: by the worker before it sent it and by swarm-api on the way out. */
  message: string | null
  own: boolean
}

export interface AccountHealthHour {
  hour: string
  counts: Partial<AccountHealthCounts>
  total: number
}

export interface AccountHealth {
  account_id: string
  /** null when the record carries no word swarm-api knows: drawn as a dash, never as `healthy`. */
  status: AccountHealthStatus | null
  limited_until: string | null
  /** Each span null when the broker sent none. */
  counts: { last_1h: AccountHealthCounts | null; last_24h: AccountHealthCounts | null; last_7d: AccountHealthCounts | null }
  /** The 7-day hourly series; an hour with no events may be absent. */
  hourly: AccountHealthHour[]
  /** The record's last 50, newest first. */
  events: AccountHealthEvent[]
  observed_at: string | null
}

const isHealthKind = (v: unknown): v is AccountHealthKind => ACCOUNT_HEALTH_KINDS.includes(v as AccountHealthKind)
const healthStr = (v: unknown): string | null => (typeof v === 'string' && v !== '' ? v : null)
const count = (v: unknown): number => (typeof v === 'number' && Number.isInteger(v) && v >= 0 ? v : 0)

function healthCounts(raw: unknown): AccountHealthCounts | null {
  if (!isRecord(raw)) return null
  const out = {} as AccountHealthCounts
  for (const k of ACCOUNT_HEALTH_KINDS) out[k] = count(raw[k])
  return out
}

/** The served body, by the same allow-lists the route applies; null when it is not a health record at all. */
export function normaliseAccountHealth(raw: unknown, accountId: string): AccountHealth | null {
  if (!isRecord(raw)) return null
  const counts = isRecord(raw.counts) ? raw.counts : {}
  const hourly: AccountHealthHour[] = []
  for (const h of Array.isArray(raw.hourly) ? raw.hourly : []) {
    if (!isRecord(h) || healthStr(h.hour) === null || Number.isNaN(Date.parse(h.hour as string))) continue
    const c: Partial<AccountHealthCounts> = {}
    let total = 0
    if (isRecord(h.counts)) {
      for (const k of ACCOUNT_HEALTH_KINDS) {
        const n = count(h.counts[k])
        if (n > 0) {
          c[k] = n
          total += n
        }
      }
    }
    hourly.push({ hour: h.hour as string, counts: c, total })
  }
  const events: AccountHealthEvent[] = []
  for (const e of Array.isArray(raw.events) ? raw.events : []) {
    if (!isRecord(e) || !isHealthKind(e.kind) || healthStr(e.at) === null || Number.isNaN(Date.parse(e.at as string))) continue
    events.push({
      at: e.at as string,
      kind: e.kind,
      tenant_id: healthStr(e.tenant_id),
      task_id: healthStr(e.task_id),
      window: HEALTH_WINDOWS.includes(e.window as AccountHealthWindow) ? (e.window as AccountHealthWindow) : null,
      reset_at: healthStr(e.reset_at),
      message: typeof e.message === 'string' ? e.message : null,
      own: e.own === true,
    })
  }
  // NEWEST FIRST, whatever order arrived: the list draws them in this order.
  events.sort((x, y) => Date.parse(y.at) - Date.parse(x.at))
  return {
    account_id: healthStr(raw.account_id) ?? accountId,
    status: ACCOUNT_HEALTH_STATUSES.includes(raw.status as AccountHealthStatus) ? (raw.status as AccountHealthStatus) : null,
    limited_until: healthStr(raw.limited_until),
    counts: {
      last_1h: healthCounts(counts.last_1h),
      last_24h: healthCounts(counts.last_24h),
      last_7d: healthCounts(counts.last_7d),
    },
    hourly,
    events,
    observed_at: healthStr(raw.observed_at),
  }
}

/**
 * `GET /v1/accounts/{id}/health`: one read, made when the account's sheet
 * opens and when its reader asks again. Never `empty`: a record with no
 * events is a measured zero the section says in words, and it needs the
 * record in hand to say it. A 404 comes back as the error it is -- the
 * section reads `not_found` as "this deployment does not serve the record",
 * never as zero events.
 */
export async function loadAccountHealth(accountId: string): Promise<Result<AccountHealth>> {
  if (USE_FIXTURES) return fixtureAccountHealth(accountId)
  const res = await read<unknown>(route('/v1/accounts/{account_id}/health', { account_id: accountId }), () => false)
  if (res.status !== 'ok' && res.status !== 'stale') return res as Result<AccountHealth>
  const data = normaliseAccountHealth(res.data, accountId)
  if (data === null) {
    return {
      status: 'error',
      error: {
        kind: 'server_error',
        httpStatus: 200,
        code: null,
        message: 'The health read answered, but not with a health record.',
      },
    }
  }
  return { ...res, data }
}

/** `PUT /v1/accounts/{id}/state`. `reason` is for the next person, so it is required here. */
export async function setAccountState(
  accountId: string,
  state: AccountStateName,
  reason: string,
): Promise<Result<unknown>> {
  if (USE_FIXTURES) return fixtureState(accountId, state, reason)
  return write(route('/v1/accounts/{account_id}/state', { account_id: accountId }), 'PUT', { state, reason })
}

/**
 * `POST /v1/accounts/{id}/refresh`.
 *
 * A 200 HERE DOES NOT MEAN THE REFRESH WORKED. The route reports the outcome
 * in the body -- `refresh.refreshed` and `refresh.reason` -- and answers 200
 * for `reauth_required` exactly as it does for `refreshed`. Treating the status
 * as the answer would put "succeeded" on screen for the one case that needs a
 * human, which is this repository's defining bug wearing a different hat.
 */
export async function refreshAccount(accountId: string): Promise<Result<RefreshResponse>> {
  const res = USE_FIXTURES
    ? await fixtureRefresh(accountId)
    : await write(route('/v1/accounts/{account_id}/refresh', { account_id: accountId }), 'POST')
  if (res.status !== 'ok') return res as Result<RefreshResponse>

  const body = res.data
  if (!isRecord(body) || !isRecord(body.refresh) || typeof body.refresh.refreshed !== 'boolean') {
    // A 200 whose body does not say what happened is not a success. Something
    // MAY have been rotated, and the previous access token may already be
    // revoked, so this must not read as "nothing happened" either.
    return {
      status: 'error',
      error: {
        kind: 'server_error',
        httpStatus: 200,
        code: null,
        message:
          'The refresh route answered without saying whether the credential was exchanged. ' +
          'Refreshing revokes the previous access token, so the account may have been rotated anyway — reload before trying again.',
      },
    }
  }
  return { status: 'ok', data: body as unknown as RefreshResponse, fetchedAt: res.fetchedAt }
}

/**
 * `DELETE /v1/accounts/{id}`.
 *
 * Removes the Firestore document. The SECRET IS RETAINED, deliberately:
 * deleting a Secret Manager secret is irreversible and takes its version
 * history with it, so an account removed by mistake stays re-registerable.
 * The screen has to say that, because "remove" otherwise reads as "destroy".
 */
export async function removeAccount(accountId: string): Promise<Result<unknown>> {
  if (USE_FIXTURES) return fixtureRemove(accountId)
  return write(route('/v1/accounts/{account_id}', { account_id: accountId }), 'DELETE')
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}

// --------------------------------------------------------------------------
// Account fixtures
// --------------------------------------------------------------------------
// Mutable on purpose: registering, refreshing, lending and removing all take
// effect here, so the screen can be worked on -- and its failure treatments
// actually looked at -- with no deployed API at all. A fixture that only
// serves reads means every write path ships having never been seen.
//
// The set below carries ONE OF EVERY TREATMENT, because a tidy fixture teaches
// the wrong thing about what this screen is for: a live account, a stale one
// (`~`), one whose five-hour window has already reset, one that has never been
// observed at all, one in REAUTH_REQUIRED, and one the provider reports only a
// five-hour window for.

const ISO = (msFromNow: number): string => new Date(FIXTURE_NOW + msFromNow).toISOString()

function fixtureAccount(
  owner: string,
  label: string,
  extra: Partial<ServedAccount> = {},
): ServedAccount {
  return {
    account_id: `${owner}:${label}`,
    owner_tenant: owner,
    label,
    provider: 'anthropic',
    state: 'AVAILABLE',
    reason: '',
    lend_to: [],
    assigned: 0,
    windows: {},
    observed_at: ISO(-4 * 60_000),
    stale: false,
    unreadable_by: [],
    unreadable_now: [],
    // A fixture default of `null` would make EVERY development row say "never
    // assigned", which is the one warning that means "no worker can reach the
    // broker" -- the fixture must not teach that shape as normal. The rows
    // that carry it do so deliberately, below.
    last_assigned_at: ISO(-26 * 60_000),
    // Every class, which is what a broker that predates the field means too.
    model_classes: [],
    ...extra,
  }
}

let fixtureAccounts: ServedAccount[] = [
  fixtureAccount('u-bogdan', 'primary', {
    assigned: 2,
    // THE POOL'S ANSWER (#2222), on every row: can it take a new agent, what
    // stops it, and when that clears. One of each group the page ranks by.
    max_agents: 6,
    can_take_new_agent: true,
    blocked_by: [],
    next_eligible_at: null,
    organisation: { name: 'Example', plan: 'Team' },
    duplicate_of: 'u-bogdan:team-copy',
    duplicate_reason: 'registered on the same organisation as u-bogdan:team-copy',
    windows: {
      five_hour: { utilization: 0.41, resets_at: ISO(2 * 3600_000 + 51 * 60_000), reset: false },
      seven_day: { utilization: 0.63, resets_at: ISO(70 * 3600_000), reset: false },
    },
  }),
  fixtureAccount('u-bogdan', 'overflow', {
    // Observed two hours ago: past DEFAULT_STALE_AFTER, so every figure on this
    // row is marked `~` and nothing on it may be read as current.
    observed_at: ISO(-2 * 3600_000),
    stale: true,
    // 12% left on its five-hour window, counted at half because the reading
    // is old: under the 15% floor until a fresh reading or the reset.
    max_agents: 6,
    can_take_new_agent: false,
    blocked_by: [
      { window: 'five_hour', used: 0.88, floor: 0.15, resets_at: ISO(3600_000 + 43 * 60_000), aged_reading: true },
    ],
    next_eligible_at: ISO(3600_000 + 43 * 60_000),
    organisation: null,
    windows: {
      // A five-hour window resets within five hours (QAFIX6-FIXTURE f, run 6
      // overview-automate-016): 19h 43m read as a window no provider has.
      five_hour: { utilization: 0.88, resets_at: ISO(3600_000 + 43 * 60_000), reset: false },
      seven_day: { utilization: 0.12, resets_at: ISO(94 * 3600_000), reset: false },
    },
  }),
  fixtureAccount('u-bogdan', 'weekly-burn', {
    state: 'PAUSED',
    reason: 'held back for the release run on Monday',
    max_agents: 6,
    can_take_new_agent: false,
    blocked_by: [],
    next_eligible_at: null,
    organisation: { name: 'Personal', plan: 'Max' },
    windows: {
      // Already past its reset: the figure describes a window that has refilled.
      five_hour: { utilization: 0.97, resets_at: ISO(-6 * 60_000), reset: true },
      seven_day: { utilization: 0.94, resets_at: ISO(70 * 3600_000), reset: false },
    },
  }),
  fixtureAccount('u-bogdan', 'never-polled', {
    // Registered, never observed. Utilisation is UNKNOWN, emphatically not 0%.
    // It takes one agent until its first reading (BGD, "One until measured").
    max_agents: 6,
    can_take_new_agent: true,
    blocked_by: [],
    next_eligible_at: null,
    observed_at: null,
    stale: true,
    windows: {},
    // And never handed to an agent either. On one row that is just a new
    // account; the panel counts it because on EVERY row it is the shape of
    // workers that cannot reach the broker at all.
    last_assigned_at: null,
  }),
  fixtureAccount('u-bogdan', 'retired-laptop', {
    state: 'REAUTH_REQUIRED',
    reason: 'refresh failed: reauth_required',
    lend_to: ['eng'],
    max_agents: 6,
    can_take_new_agent: false,
    blocked_by: [],
    next_eligible_at: null,
    observed_at: ISO(-3 * 86_400_000),
    stale: true,
    windows: {
      five_hour: { utilization: 0.2, resets_at: ISO(-2 * 86_400_000), reset: true },
      seven_day: { utilization: 0.55, resets_at: ISO(-86_400_000), reset: true },
    },
  }),
  fixtureAccount('eng', 'shared', {
    assigned: 3,
    lend_to: ['u-bogdan'],
    // THE ROW THAT LOOKS HEALTHIEST AND SERVES NOBODY HERE. A borrower that was
    // never granted secretAccessor on a lent secret reports the account
    // unreadable; `choose()` then skips it for that tenant and for nobody else,
    // so it keeps its window, its headroom and three agents of the OWNER's --
    // and without these two fields it renders as the best account on the
    // screen. This is the fixture for the defect, and it is in development so
    // the treatment cannot ship unlooked-at.
    unreadable_by: ['u-bogdan'],
    unreadable_now: ['u-bogdan'],
    // The broker's answer is for the owner; this tenant is skipped all the same.
    max_agents: 6,
    can_take_new_agent: true,
    // IDENTICAL READINGS WITH ANOTHER WORKSPACE'S ACCOUNT (#2222, BGD Q1, Q2):
    // advisory, so it still takes agents, and the other side is not named.
    blocked_by: [
      {
        possible_duplicate: null,
        advisory: true,
        reason: 'identical readings on two polls with an account in another workspace',
      },
    ],
    possible_duplicate: { of: null, reason: 'identical readings on two polls with an account in another workspace' },
    next_eligible_at: null,
    windows: {
      // Only one window came back. The 7D column says so rather than showing 0%.
      five_hour: { utilization: 0.56, resets_at: ISO(3600_000 + 12 * 60_000), reset: false },
    },
  }),
  // THE GROUPS #2222's PAGE RANKS BY, one row each: a five-hour floor that
  // clears soon, a weekly floor that clears in days, a full account, and the
  // later registration of one subscription, withheld.
  fixtureAccount('u-bogdan', 'five-hour-floor', {
    max_agents: 6,
    can_take_new_agent: false,
    blocked_by: [{ window: 'five_hour', used: 0.92, floor: 0.15, resets_at: ISO(40 * 60_000), aged_reading: false }],
    next_eligible_at: ISO(40 * 60_000),
    organisation: { name: 'Example', plan: 'Team' },
    windows: {
      five_hour: { utilization: 0.92, resets_at: ISO(40 * 60_000), reset: false },
      seven_day: { utilization: 0.41, resets_at: ISO(4 * 86_400_000 + 3 * 3600_000), reset: false },
    },
  }),
  fixtureAccount('u-bogdan', 'weekly-floor', {
    max_agents: 6,
    can_take_new_agent: false,
    blocked_by: [{ window: 'seven_day', used: 0.97, floor: 0.05, resets_at: ISO(2 * 86_400_000 + 21 * 3600_000), aged_reading: false }],
    next_eligible_at: ISO(2 * 86_400_000 + 21 * 3600_000),
    organisation: { name: 'Example', plan: 'Team' },
    windows: {
      five_hour: { utilization: 0.31, resets_at: ISO(-3 * 3600_000), reset: true },
      seven_day: { utilization: 0.97, resets_at: ISO(2 * 86_400_000 + 21 * 3600_000), reset: false },
      // A window this console does not know by name: listed, held to the default floor.
      harbor_lantern: { utilization: 0.1, resets_at: ISO(-9 * 86_400_000), reset: true },
    },
  }),
  fixtureAccount('u-bogdan', 'full-six', {
    assigned: 6,
    max_agents: 6,
    can_take_new_agent: false,
    blocked_by: [{ cap: 6, max_agents: 6 }],
    next_eligible_at: null,
    organisation: { name: 'Example', plan: 'Team' },
    lend_to: ['smoke'],
    windows: {
      five_hour: { utilization: 0.52, resets_at: ISO(4 * 3600_000 + 39 * 60_000), reset: false },
      seven_day: { utilization: 0.71, resets_at: ISO(2 * 86_400_000 + 6 * 3600_000), reset: false },
    },
  }),
  fixtureAccount('u-bogdan', 'team-copy', {
    max_agents: 6,
    can_take_new_agent: false,
    blocked_by: [{ duplicate_of: 'u-bogdan:primary', reason: 'registered on the same organisation as u-bogdan:primary' }],
    next_eligible_at: null,
    organisation: { name: 'Example', plan: 'Team' },
    duplicate_of: 'u-bogdan:primary',
    duplicate_reason: 'registered on the same organisation as u-bogdan:primary',
    last_assigned_at: null,
    windows: {
      five_hour: { utilization: 0.41, resets_at: ISO(2 * 3600_000 + 51 * 60_000), reset: false },
      seven_day: { utilization: 0.63, resets_at: ISO(70 * 3600_000), reset: false },
    },
  }),
]

/** The tenant the fixture's verified token would carry. */
const FIXTURE_TENANT = 'u-bogdan'

async function fixtureAccountsBoard(): Promise<Result<AccountsBoard>> {
  await new Promise((r) => setTimeout(r, 210))
  noteFixtureProbe(route('/v1/accounts'), 210, true)
  // The admin tenant list is the one read a non-admin genuinely cannot do, and
  // the fixture exercises that branch rather than the happy one -- otherwise
  // the "no picker, and here is why" copy ships unlooked-at.
  noteFixtureProbe(route('/v1/admin/tenants'), 90, false, 'admin_required')
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      // `eng:shared` is in the list because it is LENT to this tenant, which is
      // what `for_tenant` returns. It is deliberately not removable, pausable
      // or refreshable from here, and the fixture is what makes that path
      // visible in development.
      page: {
        // On the page's clock, as the board reads them on the fixture's (cap2-06).
        accounts: fixtureAccounts.map((a) => fixtureOnClock({ ...a }, Date.now())),
        tenant_id: FIXTURE_TENANT,
        // A document this tenant owns and the store could not parse. The rows
        // above are six; the pool is seven. Development has to be able to see
        // the case where the list is SHORT, because that is the one where
        // every figure on the screen is quietly computed over the wrong set.
        unreadable_documents: ['u-bogdan:half-written'],
        unreadable_document_count: 1,
      },
      tenants: null,
      tenantsDetail: 'Admin group membership is required for this endpoint.',
      readAt: Date.now(),
      fixture: true,
    },
  }
}

// ---------------------------------------------------------------------------
// Capacity › Account health (ACCOUNT-HEALTH-PAGE, part of #2302)
// ---------------------------------------------------------------------------

/**
 * Everything Capacity › Account health draws, in one load of two reads:
 *
 *  - `/v1/accounts` IS the screen, as on Accounts: the rows, each with the
 *    broker's `health_status` and `health_24h`, and the pool's own answer to
 *    "can it take work" (`can_take_new_agent`, `max_agents`), which the fifth
 *    tile repeats in one line and links back to. A failure here is a failure.
 *  - `/v1/accounts/health` is every account's record in ONE read (status,
 *    counts, the hourly series, the last 50 events). Its failure does not
 *    blank the page: a 404 is a deployment that does not serve the record
 *    (`not_served`, a dash with its reason, never a zero) and anything else
 *    is a read that failed (`unreadable`), each said once on the page.
 */
export interface AccountHealthBoard {
  page: AccountsPage
  health: HealthRead
  /** When the reads were made: the page's one clock ("now", "ago", open or closed). */
  readAt: number
}

export async function loadAccountHealthBoard(): Promise<Result<AccountHealthBoard>> {
  if (USE_FIXTURES) return fixtureAccountHealthBoard()
  const [accounts, health] = await Promise.all([
    read<AccountsPage>(route('/v1/accounts'), () => false),
    read<unknown>(route('/v1/accounts/health'), () => false),
  ])
  if (accounts.status === 'loading' || accounts.status === 'error' || accounts.status === 'empty') {
    return accounts as Result<AccountHealthBoard>
  }
  return {
    status: accounts.status,
    fetchedAt: accounts.fetchedAt,
    ...(accounts.status === 'ok' ? { serverAt: accounts.serverAt } : {}),
    data: { page: accounts.data, health: healthReadOf(health), readAt: accounts.fetchedAt },
  } as Result<AccountHealthBoard>
}

function healthReadOf(r: Result<unknown>): HealthRead {
  if (r.status === 'ok' || r.status === 'stale') {
    const pool = normPoolHealth(r.data)
    return pool === null
      ? { kind: 'unreadable', reason: 'The health read answered in a shape this console does not know.' }
      : { kind: 'ok', pool }
  }
  if (r.status === 'error' && r.error.httpStatus === 404) {
    return {
      kind: 'not_served',
      reason:
        'GET /v1/accounts/health answers 404: this deployment predates the pool-wide health read (ACCOUNT-HEALTH-PAGE).',
    }
  }
  if (r.status === 'error') return { kind: 'unreadable', reason: r.error.message }
  return { kind: 'unreadable', reason: 'The health read did not complete.' }
}

/**
 * THE DEVELOPMENT POOL'S HEALTH, one of every state the page draws: a storm of
 * provider errors (erroring), two windows limited, a credential that died
 * (needs re-auth, with the refusals that follow it), quiet healthy accounts
 * and one with no events in 7 days. Built off the fixture accounts, so the
 * rows, the tiles and the lanes agree, and relative to FIXTURE_NOW.
 */
function fixturePoolHealth(): PoolHealth {
  const H = 3_600_000
  const M = 60_000
  const top = Math.floor(FIXTURE_NOW / H) * H
  type Ev = [number, HealthKind, { w?: string; r?: number; m?: string; t?: string }?]
  const plan: Record<string, Ev[]> = {
    'u-bogdan:primary': [[-50 * H, 'network', { m: 'the connection to the provider was reset' }], [-98 * H, 'overloaded']],
    'u-bogdan:overflow': [
      ...Array.from({ length: 9 }, (_, i): Ev => [-(40 - i * 4) * M, i % 3 === 0 ? 'server_error' : 'overloaded']),
      [-30 * H, 'overloaded'], [-30 * H + 8 * M, 'overloaded'],
    ],
    'u-bogdan:weekly-burn': [
      // Its reset is the one Accounts draws (seven_day resets_at, ISO(70 h)): QA1-FIX-01 capacity-04.
      [-3 * H, 'rate_limit', { w: 'seven_day', r: 70 * H }],
      [-2 * H, 'assign_refused', { w: 'seven_day', m: 'not assigned: its 7-day window is under its floor' }],
      [-27 * H, 'exhausted'],
    ],
    'u-bogdan:five-hour-floor': [
      // Its reset is the one Accounts draws (five_hour resets_at, ISO(40 min)): QA1-FIX-01 capacity-04.
      [-90 * M, 'rate_limit', { w: 'five_hour', r: 40 * M }],
      [-70 * M, 'rate_limit', { w: 'five_hour', r: 40 * M }],
      [-52 * H, 'rate_limit', { w: 'five_hour', r: -48 * H }],
      [-120 * H, 'rate_limit', { w: 'five_hour', r: -117 * H }],
    ],
    // The re-auth story sits on the account whose state IS REAUTH_REQUIRED, so
    // its row, its callout and the phone's re-auth card tell one story.
    'u-bogdan:retired-laptop': [
      [-30 * H, 'refresh_failed', { m: 'the credential refresh was refused · credential [redacted]' }],
      [-70 * M, 'refresh_failed', { m: 'the credential refresh was refused · credential [redacted]' }],
      [-62 * M, 'auth', { m: '401 from the provider: the sign-in expired · credential [redacted]' }],
      [-61 * M, 'reauth', { m: 'marked as needing re-auth by the worker after a 401' }],
      [-40 * M, 'assign_refused', { m: 'not assigned: the account needs re-auth' }],
      [-12 * M, 'assign_refused', { m: 'not assigned: the account needs re-auth' }],
    ],
    'u-bogdan:full-six': [[-140 * H, 'network', { m: 'read timeout after 600 s, stream closed' }]],
  }
  // accountstore.health_status's order: re-auth, then limited -- by the window
  // readings OR by a 429 short of its reset (`limited_until`), so an account
  // whose phone card is an open limit never has a Healthy row -- then erroring.
  const status = (a: ServedAccount, limitedUntil: string | null): HealthStatus =>
    a.state === 'REAUTH_REQUIRED' ? 'reauth_needed'
    : a.can_take_new_agent === false && (a.blocked_by ?? []).some((b) => 'window' in b) ? 'limited'
    : limitedUntil !== null && Date.parse(limitedUntil) > FIXTURE_NOW ? 'limited'
    : a.account_id === 'u-bogdan:overflow' ? 'erroring'
    : 'healthy'
  const accounts = fixtureAccounts.map((a): AccountHealthRecord => {
    const events: HealthEvent[] = (plan[a.account_id] ?? [])
      .map(([dt, kind, o = {}]) => ({
        at: ISO(dt),
        kind,
        tenant_id: o.t ?? FIXTURE_TENANT,
        task_id: `task_${a.label.replace(/[^a-z0-9]/g, '')}${Math.abs(dt) % 9973}`,
        window: o.w ?? null,
        reset_at: o.r === undefined ? null : ISO(o.r),
        message: o.m ?? (kind === 'rate_limit' ? 'the provider rejected a request at its rate limit' : kind === 'overloaded' ? '529 from the provider: overloaded' : kind === 'server_error' ? '503 service unavailable, retry later' : kind === 'exhausted' ? 'quota exhausted · parked at checkpoint 4, capacity released' : ''),
        own: true,
      }))
      .sort((x, y) => Date.parse(y.at) - Date.parse(x.at))
    const hourly = Array.from({ length: 168 }, (_, i) => {
      const at = top - (167 - i) * H
      const counts: Partial<Record<HealthKind, number>> = {}
      for (const e of events) {
        const t = Date.parse(e.at)
        if (t >= at && t < at + H) counts[e.kind] = (counts[e.kind] ?? 0) + 1
      }
      return { hour: new Date(at).toISOString(), counts, total: Object.values(counts).reduce((n, v) => n + (v ?? 0), 0) }
    })
    const since = (h: number) => {
      const c = Object.fromEntries(HEALTH_KINDS.map((k) => [k, 0])) as Record<HealthKind, number>
      for (const e of events) if (Date.parse(e.at) >= FIXTURE_NOW - h * H) c[e.kind] += 1
      return c
    }
    const limit = events.find((e) => e.kind === 'rate_limit' && e.reset_at !== null && Date.parse(e.reset_at) > FIXTURE_NOW)
    return {
      account_id: a.account_id,
      label: a.label,
      owner_tenant: a.owner_tenant,
      status: status(a, limit?.reset_at ?? null),
      limited_until: limit?.reset_at ?? null,
      counts: { last_1h: since(1), last_24h: since(24), last_7d: since(168) },
      hourly,
      events,
      observed_at: ISO(0),
    }
  })
  return { tenant_id: FIXTURE_TENANT, accounts, events_max: 50, truncated: false, observed_at: ISO(0) }
}

async function fixtureAccountHealthBoard(): Promise<Result<AccountHealthBoard>> {
  await new Promise((r) => setTimeout(r, 180))
  noteFixtureProbe(route('/v1/accounts'), 180, true)
  noteFixtureProbe(route('/v1/accounts/health'), 120, true)
  // ONE CLOCK FOR EVERY CAPACITY FIXTURE (QA2-FIX-07 review, cap2-06): the
  // board, the Accounts list and the account sheet are all moved onto the
  // browser's clock, so weekly-burn's limit ends at the same instant on each.
  const now = Date.now()
  const pool = fixtureOnClock(fixturePoolHealth(), now)
  const byId = new Map(pool.accounts.map((r) => [r.account_id, r]))
  return {
    status: 'ok',
    fetchedAt: now,
    data: {
      page: {
        accounts: fixtureAccounts.map((a) => fixtureOnClock({
          ...a,
          health_status: byId.get(a.account_id)?.status ?? null,
          health_24h: byId.get(a.account_id)?.counts.last_24h ?? null,
        }, now)),
        tenant_id: FIXTURE_TENANT,
        unreadable_documents: [],
        unreadable_document_count: 0,
      },
      health: { kind: 'ok', pool },
      // The fixture's events are dated off FIXTURE_NOW and moved onto `now` by
      // the same span, so `now` is when it was read: the page's clock is the
      // read's, and no limit reads as long cleared nor incident as closed.
      readAt: now,
    },
  }
}

function fixtureRefused(message: string): Result<unknown> {
  return {
    status: 'error',
    error: { kind: 'invalid', httpStatus: 422, code: 'validation_failed', message },
  }
}

/**
 * The pending sign-ins this fixture is holding, keyed by state.
 *
 * A MAP RATHER THAN A SINGLE SLOT, because the two refusals worth exercising
 * both need more than one: a code reused after it succeeded, and a paste whose
 * state belongs to a different sign-in than the one this page started. Both
 * are copy on the screen, and copy that no fixture can reach is copy that
 * ships having never been read.
 */
const fixturePending = new Map<string, { label: string; lend_to: string[] }>()

async function fixtureBeginSignIn(body: {
  label: string
  lend_to: string[]
}): Promise<Result<unknown>> {
  await new Promise((r) => setTimeout(r, 260))
  // The label rule is the BROKER's -- `validate_label` runs inside
  // /v1/accounts/authorize, before a URL is returned, so an unusable label is
  // caught before anybody signs in. The fixture applies the same refusal so
  // that the message an operator gets in development is the one production
  // gives, rather than a happy path only production disagrees with.
  if (!/^[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?$/.test(body.label)) {
    return fixtureRefused(
      `account label '${body.label}' must be lowercase letters, digits and dashes, ` +
        'starting and ending alphanumeric, at most 40 characters -- it becomes part ' +
        'of a Secret Manager name and a Kubernetes annotation',
    )
  }
  // 43 characters, which is what 32 random bytes base64url-encode to. Anything
  // shorter is the length the real authorize endpoint refuses, so a fixture
  // that produced a short state would be modelling a broken server.
  const state = `fixture-${Math.random().toString(36).slice(2)}${Math.random()
    .toString(36)
    .slice(2)}`.padEnd(43, 'x')
  fixturePending.set(state, { label: body.label, lend_to: body.lend_to })
  // NOTED ONLY ON THE PATH THAT SUCCEEDS. `noteFixtureProbe` renders a non-ok
  // probe as a 403 or a 503, and this route's refusal is a 422 -- so noting
  // the refusal above would put a wrong status on the Reference page, which
  // exists to say what these routes actually answered. A route with no row is
  // a route nothing has called yet: true, and not a claim about the platform.
  noteFixtureProbe(route('/v1/accounts/authorize'), 260, true)
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      // The provider's real authorize host and real redirect, so the screen's
      // copy -- which reads the callback host out of this URL rather than
      // restating it -- is exercised rather than stubbed.
      authorize_url:
        'https://claude.com/cai/oauth/authorize?code=true&response_type=code' +
        '&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback' +
        `&code_challenge_method=S256&state=${encodeURIComponent(state)}`,
      state,
      expires_in_seconds: 900,
    },
  }
}

async function fixtureFinishSignIn(body: {
  state: string
  code: string
}): Promise<Result<unknown>> {
  await new Promise((r) => setTimeout(r, 480))

  const hash = body.code.indexOf('#')
  const pastedState = hash === -1 ? null : body.code.slice(hash + 1).trim()
  if (pastedState && pastedState !== body.state) {
    return fixtureRefused(
      'the pasted code belongs to a different sign-in than the one this page ' +
        'started. Press Add account and sign in again.',
    )
  }
  const pending = fixturePending.get(body.state)
  if (!pending) {
    // The record is ABSENT. In the broker this is overwhelmingly a sign-in
    // that already SUCCEEDED, because the only delete that does not also
    // raise is the one after the account is written -- which is why the
    // screen's treatment for this may not say "nothing was created", and why
    // the fixture reaches it by pasting twice rather than by timing out.
    return fixtureRefused(
      'this sign-in has expired or was already completed. Codes are ' +
        'single-use; press Add account to start again.',
    )
  }
  const code = (hash === -1 ? body.code : body.code.slice(0, hash)).trim()
  // TWO SENTINEL CODES, because the screen has two treatments that no ordinary
  // paste can reach and unreadable copy is copy that ships unread. They stand
  // where a 15-minute wait and a broker bug would be, and nothing but a
  // deliberate paste produces either.
  //
  // `expired` -> the age refusal. Deletes the record BEFORE any redemption,
  // so nothing was created: the opposite consequence to the branch above,
  // which is the whole reason they are two treatments and not one.
  if (code.toLowerCase() === 'expired') {
    fixturePending.delete(body.state)
    return fixtureRefused(
      'this sign-in took too long and the code will have expired. ' +
        'Press Add account to start again.',
    )
  }
  // A short code stands in for one the endpoint rejects -- the commonest real
  // cause being that the person took too long. The pending record SURVIVES it,
  // exactly as the broker's does, so the screen's "your sign-in is still open,
  // paste again" treatment is reachable in development.
  if (code.length < 6) {
    return fixtureRefused(
      'the sign-in code was not accepted. Codes are single-use and expire ' +
        'quickly, so this usually means it was already redeemed or that too ' +
        'much time passed. Sign in again to get a new one.',
    )
  }

  const existing = fixtureAccounts.find(
    (a) => a.account_id === `${FIXTURE_TENANT}:${pending.label}`,
  )
  const account: Account = existing
    ? // Re-registering preserves state and readings, exactly as accountstore.py
      // does -- REAUTH_REQUIRED included, which is why the screen puts the
      // state back as a visible second call rather than assuming it moved.
      { ...existing, lend_to: pending.lend_to }
    : fixtureAccount(FIXTURE_TENANT, pending.label, {
        lend_to: pending.lend_to,
        observed_at: null,
        stale: true,
        windows: {},
      })
  fixtureAccounts = existing
    ? fixtureAccounts.map((a) => (a.account_id === account.account_id ? account : a))
    : [...fixtureAccounts, account]
  // Single-use, and deleted only once the account exists.
  fixturePending.delete(body.state)
  noteFixtureProbe(route('/v1/accounts/exchange'), 480, true)
  // `unnamed` -> the second sentinel: a 201 whose body names no account. The
  // account IS registered above and the record IS gone, which is exactly the
  // situation the treatment describes -- "look for the label in the pool" is
  // advice a fixture can now be checked against, and it finds it.
  if (code.toLowerCase() === 'unnamed') {
    return { status: 'ok', fetchedAt: Date.now(), data: {} }
  }
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      account,
      expires_at: ISO(8 * 3600_000),
      note: 'stored write-only; no route in this service returns key material',
    },
  }
}

async function fixtureLending(accountId: string, lendTo: string[]): Promise<Result<unknown>> {
  await new Promise((r) => setTimeout(r, 180))
  fixtureAccounts = fixtureAccounts.map((a) =>
    a.account_id === accountId ? { ...a, lend_to: lendTo } : a,
  )
  return { status: 'ok', fetchedAt: Date.now(), data: { ok: true } }
}

async function fixtureModelClasses(accountId: string, modelClasses: string[]): Promise<Result<unknown>> {
  await new Promise((r) => setTimeout(r, 180))
  const found = fixtureAccounts.find((a) => a.account_id === accountId)
  if (!found) return fixtureRefused(`no account '${accountId}'`)
  const account: ServedAccount = { ...found, model_classes: modelClasses }
  fixtureAccounts = fixtureAccounts.map((a) => (a.account_id === accountId ? account : a))
  // The route answers `{"account": ...}`, the same shape lending's does.
  return { status: 'ok', fetchedAt: Date.now(), data: { account } }
}

async function fixtureName(accountId: string, displayName: string | null): Promise<Result<unknown>> {
  await new Promise((r) => setTimeout(r, 180))
  const found = fixtureAccounts.find((a) => a.account_id === accountId)
  if (!found) return fixtureRefused(`no account '${accountId}'`)
  // The broker's two commonest refusals, so the editor's fault can be seen
  // without a deployment; `validate_display_name` is the rule itself.
  const name = displayName === null ? null : displayName.trim()
  if (name === '') return fixtureRefused("an account's Name is 1 to 60 characters once trimmed, and this one is empty; send null to clear it so the label shows")
  if (name !== null && name.length > 60) return fixtureRefused(`an account's Name is at most 60 characters once trimmed; this one is ${name.length}`)
  const account: Account = { ...found, display_name: name }
  fixtureAccounts = fixtureAccounts.map((a) => (a.account_id === accountId ? account : a))
  return { status: 'ok', fetchedAt: Date.now(), data: { account } }
}

/**
 * THE HEALTH RECORD THE FIXTURE SERVES (#2302): the account's own record off
 * the health board (`fixturePoolHealth`), so the sheet, the board and the list
 * tell one story (QA2-FIX-07, cap2-06: the sheet had a plan of its own on the
 * browser's clock, and drew weekly-burn Healthy with no event in 24 h beside a
 * board that drew it Limited with two). `eng:shared` is a deployment whose
 * broker predates the record (a 404).
 */
async function fixtureAccountHealth(accountId: string): Promise<Result<AccountHealth>> {
  await new Promise((r) => setTimeout(r, 160))
  if (!fixtureAccounts.some((a) => a.account_id === accountId)) {
    return { status: 'error', error: { kind: 'not_found', httpStatus: 404, code: 'not_found', message: `no account '${accountId}'` } }
  }
  if (accountId === 'eng:shared') {
    return { status: 'error', error: { kind: 'not_found', httpStatus: 404, code: null, message: 'Not Found' } }
  }
  const record = fixturePoolHealth().accounts.find((r) => r.account_id === accountId)
  if (record === undefined) {
    return { status: 'error', error: { kind: 'not_found', httpStatus: 404, code: 'not_found', message: `no account '${accountId}'` } }
  }
  const now = Date.now()
  // The sheet measures on the browser's clock, so the record is moved onto it whole.
  const r = fixtureOnClock(record, now)
  return {
    status: 'ok',
    fetchedAt: now,
    data: {
      account_id: r.account_id, status: r.status, limited_until: r.limited_until, counts: r.counts,
      hourly: r.hourly.filter((h) => h.total > 0), events: r.events, observed_at: r.observed_at,
    } as AccountHealth,
  }
}

const FIXTURE_ISO_AT = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$/

/**
 * A fixture document dated off FIXTURE_NOW, every time in it moved by the same
 * span onto `now`: what a page that measures on the browser's clock reads, so
 * "4 min ago" on the fixture's clock is still 4 min ago on the page's
 * (QA2-FIX-07, cap2-06: the Accounts list read every account "read 10d ago").
 */
function fixtureOnClock<T>(doc: T, now: number): T {
  const shift = now - FIXTURE_NOW
  const move = (v: unknown): unknown => {
    if (typeof v === 'string') return FIXTURE_ISO_AT.test(v) ? new Date(Date.parse(v) + shift).toISOString() : v
    if (Array.isArray(v)) return v.map(move)
    if (v !== null && typeof v === 'object') return Object.fromEntries(Object.entries(v).map(([k, x]) => [k, move(x)]))
    return v
  }
  return move(doc) as T
}

async function fixtureMaxAgents(accountId: string, maxAgents: number): Promise<Result<unknown>> {
  await new Promise((r) => setTimeout(r, 180))
  const found = fixtureAccounts.find((a) => a.account_id === accountId)
  if (!found) return fixtureRefused(`no account '${accountId}'`)
  // swarm-api's `_checked_max_agents`, in its words.
  if (!Number.isInteger(maxAgents) || maxAgents < 1 || maxAgents > 50) {
    return fixtureRefused(
      `an account's max_agents is 1 to 50, and this is ${maxAgents}; pause the account to stop it taking new agents`,
    )
  }
  const account: ServedAccount = { ...found, max_agents: maxAgents }
  fixtureAccounts = fixtureAccounts.map((a) => (a.account_id === accountId ? account : a))
  return { status: 'ok', fetchedAt: Date.now(), data: { account } }
}

async function fixtureState(
  accountId: string,
  state: AccountStateName,
  reason: string,
): Promise<Result<unknown>> {
  await new Promise((r) => setTimeout(r, 180))
  fixtureAccounts = fixtureAccounts.map((a) =>
    a.account_id === accountId ? { ...a, state, reason } : a,
  )
  return { status: 'ok', fetchedAt: Date.now(), data: { ok: true } }
}

async function fixtureRefresh(accountId: string): Promise<Result<unknown>> {
  await new Promise((r) => setTimeout(r, 620))
  const account = fixtureAccounts.find((a) => a.account_id === accountId)
  if (!account) return fixtureRefused(`no account '${accountId}'`)

  // REAUTH_REQUIRED keeps failing on purpose: it is the one outcome an operator
  // has to act on, and the only way to see that copy is for a fixture to
  // produce it.
  if (account.state === 'REAUTH_REQUIRED') {
    return {
      status: 'ok',
      fetchedAt: Date.now(),
      data: {
        refresh: {
          tenant_id: account.label, provider: 'account',
          refreshed: false, reason: 'reauth_required', expires_at: null,
        },
        account,
      } satisfies RefreshResponse,
    }
  }
  const refreshed: Account = { ...account, observed_at: new Date(FIXTURE_NOW).toISOString(), stale: false }
  fixtureAccounts = fixtureAccounts.map((a) => (a.account_id === accountId ? refreshed : a))
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      refresh: {
        tenant_id: account.label, provider: 'account',
        refreshed: true, reason: 'refreshed', expires_at: ISO(8 * 3600_000),
      },
      account: refreshed,
    } satisfies RefreshResponse,
  }
}

async function fixtureRemove(accountId: string): Promise<Result<unknown>> {
  await new Promise((r) => setTimeout(r, 200))
  fixtureAccounts = fixtureAccounts.filter((a) => a.account_id !== accountId)
  return { status: 'ok', fetchedAt: Date.now(), data: { removed: accountId, secret: 'retained' } }
}

// ===========================================================================
// Overview (the landing screen) -- two reads nothing else needed
// ===========================================================================
//
// Everything else the Overview draws comes from loaders that already existed
// above: loadCapacity, loadTasks, loadStats, loadProviders and loadLeases.
// Only these two are new, and both exist because the shape the Overview needs
// is genuinely different from what an existing loader returns.

/**
 * The subscription pool ALONE.
 *
 * Deliberately not `loadAccountsBoard`: that one also reads
 * `/v1/admin/tenants`, which exists to populate the lending picker on the
 * Accounts screen. The Overview offers no lending control, so paying for an
 * admin read -- and, for a non-admin, taking a guaranteed 403 -- to render a
 * headroom figure would be a request spent on nothing.
 *
 * `accounts.length === 0` IS empty here, and the panel says what that means:
 * no account is registered, so the subscription pool supplies nothing. That is
 * a real answer, not a failure, and it is the difference between "the pool is
 * empty" and "we could not read the pool".
 */
export async function loadAccountPool(): Promise<Result<AccountsPage>> {
  if (USE_FIXTURES) return fixtureAccountPool()
  return read<AccountsPage>(route('/v1/accounts'), (d) => d.accounts.length === 0)
}

async function fixtureAccountPool(): Promise<Result<AccountsPage>> {
  await new Promise((r) => setTimeout(r, 190))
  noteFixtureProbe(route('/v1/accounts'), 190, true)
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      accounts: fixtureAccounts.map((a) => ({ ...a })),
      tenant_id: FIXTURE_TENANT,
      // The same shortfall the Accounts board fixture carries. Both fixtures
      // read the same route, so a difference between them would be a shape the
      // API never sends.
      unreadable_documents: ['u-bogdan:half-written'],
      unreadable_document_count: 1,
    },
  }
}

/**
 * HOW MANY TASKS THE SPEND FIGURE IS SUMMED OVER, and why it is a small number.
 *
 * `cost_usd` and the four token counts live on the ATTEMPT (codec.py:285), and
 * there is no route that aggregates them -- no sum, no group-by, no date
 * range. The only way to total them from a browser is one
 * `GET /v1/tasks/{id}/attempts` per task, so the sample size is a request
 * count in disguise.
 *
 * Twelve, at three in flight, because the API's token bucket is 20 rps per
 * principal per instance and EVERY OPEN TAB counts against it. The Overview
 * already spends five requests on its other panels; a fan-out of thirty would
 * make the landing screen the thing that 429s the operator who opened it.
 * Three in flight, started only after the task page has landed, keeps the peak
 * under half the bucket with room for a second tab.
 *
 * The consequence is stated on the panel rather than hidden: this is a sample
 * of the most recent work, not the tenant's bill. An aggregate route is the
 * request filed in the report.
 */
const SPEND_SAMPLE_TASKS = 12
const SPEND_CONCURRENCY = 3

/**
 * What the Overview's spend panel is allowed to claim.
 *
 * Every field that could be zero is `number | null` on purpose. An attempt
 * that reports no cost is NOT an attempt that cost nothing -- `record_usage`
 * in agent_worker/control.py omits a key the runner did not report, and says
 * so in its own docstring: "None means not reported and zero means cost
 * nothing, and a mock task is genuinely the second while a result that failed
 * to parse is the first". So a total is null until at least one attempt
 * actually carried the figure, and `$0.00` is never printed over an unmeasured
 * sample.
 */
export interface SpendRollup {
  /** The tenant these tasks belong to, as the task page reported it. */
  tenantId: string | null
  /** Tasks on the page the sample was drawn from. */
  tasksOnPage: number
  /** Tasks on that page that have ever run. The population, not the sample. */
  tasksWithAttempts: number
  /** Tasks whose attempts were actually read. At most SPEND_SAMPLE_TASKS. */
  tasksSampled: number
  /** Tasks whose attempt read FAILED. Their spend is in none of the figures. */
  failedReads: number
  /** Why, for the panel. Null when every read in the sample succeeded. */
  failedDetail: string | null
  /** Attempts returned across the sample. */
  attempts: number
  /** Of those, how many carried a cost figure. The rest are not zero. */
  attemptsWithCost: number
  /** Of those, how many carried any token figure. */
  attemptsWithTokens: number
  costUsd: number | null
  inputTokens: number | null
  outputTokens: number | null
  cacheReadTokens: number | null
  cacheCreationTokens: number | null
  /** Oldest and newest `created_at` among the SAMPLED tasks. The real span. */
  from: string | null
  to: string | null
}

/**
 * Sum spend over the most recent tasks of an already-loaded page.
 *
 * Takes the page rather than re-reading `/v1/tasks`: the Overview has it in
 * hand, and a second copy could disagree with the one the rest of the screen
 * is drawn from -- two panels describing different sets of tasks with no way
 * to tell.
 *
 * `page.tasks` arrives newest-first (store.py:408 orders by created_at
 * DESCENDING), so `slice` takes the most recent, not an arbitrary twelve.
 */
export async function loadSpend(page: TaskPage): Promise<Result<SpendRollup>> {
  const withAttempts = page.tasks.filter((t) => t.attempt_count > 0)
  const sample = withAttempts.slice(0, SPEND_SAMPLE_TASKS)
  const base = {
    tenantId: page.tenant_id ?? null,
    tasksOnPage: page.tasks.length,
    tasksWithAttempts: withAttempts.length,
    tasksSampled: sample.length,
  }

  // No task has ever run. A real zero, and the panel says exactly that --
  // distinct from "we could not read the attempts".
  if (sample.length === 0) return { status: 'empty', fetchedAt: Date.now() }

  const results = await mapWithLimit(sample, SPEND_CONCURRENCY, (t) =>
    USE_FIXTURES ? fixtureSpendAttempts(t) : loadAttempts(t.id),
  )

  let attempts = 0
  let attemptsWithCost = 0
  let attemptsWithTokens = 0
  let failedReads = 0
  let failedDetail: string | null = null
  // `null` until a figure is actually seen. Seeding these at 0 is how an
  // unmeasured sample becomes a confident "$0.00".
  let cost: number | null = null
  let input: number | null = null
  let output: number | null = null
  let cacheRead: number | null = null
  let cacheCreate: number | null = null

  const add = (acc: number | null, v: number | null): number | null =>
    typeof v === 'number' && Number.isFinite(v) ? (acc ?? 0) + v : acc

  for (const r of results) {
    if (r.status === 'error') {
      failedReads++
      // The FIRST failure's message, kept verbatim. Collapsing several causes
      // into one sentence is the mistake this codebase keeps paying for.
      failedDetail ??= r.error.message
      continue
    }
    // `empty` means the task reports attempts but the subcollection returned
    // none. Nothing to add, and nothing failed.
    if (r.status !== 'ok' && r.status !== 'stale') continue
    for (const a of r.data.attempts) {
      attempts++
      if (typeof a.cost_usd === 'number' && Number.isFinite(a.cost_usd)) attemptsWithCost++
      const anyToken =
        [a.input_tokens, a.output_tokens, a.cache_read_input_tokens, a.cache_creation_input_tokens]
          .some((v) => typeof v === 'number' && Number.isFinite(v))
      if (anyToken) attemptsWithTokens++
      cost = add(cost, a.cost_usd)
      input = add(input, a.input_tokens)
      output = add(output, a.output_tokens)
      cacheRead = add(cacheRead, a.cache_read_input_tokens)
      cacheCreate = add(cacheCreate, a.cache_creation_input_tokens)
    }
  }

  // EVERY read failed. There is no partial figure to show and showing the
  // zeros would be a number nobody measured, so this is an error, not a
  // rollup with empty columns.
  if (failedReads === sample.length) {
    const first = results.find((r) => r.status === 'error')
    return {
      status: 'error',
      error:
        first && first.status === 'error'
          ? first.error
          : {
              kind: 'server_error',
              httpStatus: null,
              code: null,
              message: 'No attempt read completed.',
            },
    }
  }

  const times = sample
    .map((t) => t.created_at)
    .filter((v): v is string => typeof v === 'string')
    .sort()

  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      ...base,
      failedReads,
      failedDetail,
      attempts,
      attemptsWithCost,
      attemptsWithTokens,
      costUsd: cost,
      inputTokens: input,
      outputTokens: output,
      cacheReadTokens: cacheRead,
      cacheCreationTokens: cacheCreate,
      from: times[0] ?? null,
      to: times[times.length - 1] ?? null,
    },
  }
}

/**
 * HOW MANY STEPS THE WORKFLOW BOARD READS ATTEMPTS FOR, and why it is bounded.
 *
 * Same wall as `loadSpend` above and the same arithmetic: `cost_usd` and the
 * four token counts live on the ATTEMPT (codec.py:285) and no route aggregates
 * them, so a per-step figure costs one `GET /v1/tasks/{id}/attempts` per step.
 * A board showing ten workflows of five steps would be fifty requests against a
 * 20 rps bucket shared with every other tab the operator has open.
 *
 * Twelve tasks at three in flight, started only after the board itself has
 * landed. A step outside the sample renders `not sampled` -- which is a THIRD
 * absence, distinct from "no attempt reported a cost" and from "the read
 * failed", and the three must not render alike. An aggregation route is the
 * standing request (audit S3); until it exists, a bounded sample that says so
 * is the honest shape.
 */
const STEP_USAGE_SAMPLE_TASKS = 12
const STEP_USAGE_CONCURRENCY = 3

/** What one step's attempts add up to. Every summable field is nullable. */
export interface StepUsage {
  /** Attempts returned for this task. A real count -- 0 is possible and means the read succeeded with none. */
  attempts: number
  /** Of those, how many carried a cost. The rest are not zero. */
  attemptsWithCost: number
  /** Of those, how many carried any token figure. */
  attemptsWithTokens: number
  /** Checkpoint ids listed across the attempts. COUNTED, so 0 is a digit. */
  checkpoints: number
  costUsd: number | null
  inputTokens: number | null
  outputTokens: number | null
  cacheReadTokens: number | null
  cacheCreationTokens: number | null
}

export interface WorkflowUsage {
  /** Usage for every task whose attempts were read. */
  byTaskId: Map<string, StepUsage>
  /** Task ids left out because the sample ceiling was reached. Not zero-cost. */
  notSampled: Set<string>
  /** Task id -> why its attempt read failed. Not zero-cost either. */
  failed: Map<string, string>
  /** Published so the screen can say what the ceiling was, rather than imply none. */
  sampleLimit: number
  /** Distinct task ids the board asked about. */
  tasksRequested: number
}

/**
 * Attempt usage for the tasks a workflow board has on screen.
 *
 * Takes the ids rather than re-reading the board: the screen has them in hand,
 * and a second read could disagree with the graph that is already drawn.
 */
export async function loadWorkflowUsage(
  taskIds: readonly string[],
): Promise<Result<WorkflowUsage>> {
  const wanted = Array.from(new Set(taskIds))
  const sample = wanted.slice(0, STEP_USAGE_SAMPLE_TASKS)
  const notSampled = new Set(wanted.slice(STEP_USAGE_SAMPLE_TASKS))

  // No step has a task yet. A real zero -- the workflow has not reached any of
  // them -- and distinct from "we could not read the attempts".
  if (sample.length === 0) return { status: 'empty', fetchedAt: Date.now() }

  const results = await mapWithLimit(sample, STEP_USAGE_CONCURRENCY, (id) =>
    USE_FIXTURES ? fixtureStepAttempts(id) : loadAttempts(id),
  )

  const byTaskId = new Map<string, StepUsage>()
  const failed = new Map<string, string>()
  results.forEach((r, i) => {
    const id = sample[i]
    if (id === undefined) return
    if (r.status === 'error') {
      failed.set(id, r.error.message)
      return
    }
    // `loading` cannot be awaited into existence here, but the union permits
    // it and a silent `continue` would record the task as measured-with-zero.
    if (r.status === 'loading') {
      failed.set(id, 'The attempt read did not complete.')
      return
    }
    byTaskId.set(id, rollUpAttempts(r.status === 'empty' ? [] : r.data.attempts))
  })

  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      byTaskId,
      notSampled,
      failed,
      sampleLimit: STEP_USAGE_SAMPLE_TASKS,
      tasksRequested: wanted.length,
    },
  }
}

/**
 * Sum one task's attempts.
 *
 * Each field is summed SEPARATELY and stays null until some attempt actually
 * carried it -- `record_spend` writes only the keys the runner reported, so an
 * attempt can have input tokens and no output, and `(input ?? 0) + (output ??
 * 0)` counts the missing half as a zero. That is the same defect
 * `AgentDetail.tsx:408` records having already been fixed once.
 */
function rollUpAttempts(attempts: readonly AttemptRow[]): StepUsage {
  return {
    attempts: attempts.length,
    attemptsWithCost: attempts.filter((a) => typeof a.cost_usd === 'number' && Number.isFinite(a.cost_usd)).length,
    attemptsWithTokens: attempts.filter((a) =>
      [a.input_tokens, a.output_tokens, a.cache_read_input_tokens, a.cache_creation_input_tokens].some(
        (v) => typeof v === 'number' && Number.isFinite(v),
      ),
    ).length,
    checkpoints: attempts.reduce((t, a) => t + a.checkpoints.length, 0),
    costUsd: sumReported(attempts, (a) => a.cost_usd),
    inputTokens: sumReported(attempts, (a) => a.input_tokens),
    outputTokens: sumReported(attempts, (a) => a.output_tokens),
    cacheReadTokens: sumReported(attempts, (a) => a.cache_read_input_tokens),
    cacheCreationTokens: sumReported(attempts, (a) => a.cache_creation_input_tokens),
  }
}

/**
 * Attempts for the workflow board's step figures: THE SAME ROWS the Attempts
 * tab and the run's panel read (`fixtureAttemptRows`), so a step's total is
 * its attempts' sum. Its own rows used to give every step one attempt at
 * $0.0061 and 58.4k tokens -- the cancelled synthesis too -- beside a panel
 * whose attempt alone cost $0.418 (QAFIX4-FIXTURE-B F14c, run 4
 * flow-workflow-graph-light-07 and -08). The mix the node has to survive is
 * in `fixtureUsage`: figures on the newest attempt and none on a retry
 * (`task_wf_plan`), a measured zero (`task_wf_scan_a`), and nothing yet on a
 * running step (`task_wf_scan_b`).
 */
async function fixtureStepAttempts(taskId: string): Promise<Result<{ attempts: AttemptRow[] }>> {
  return fixtureAttempts(taskId)
}

/**
 * `Promise.all` with a ceiling on how many are in flight.
 *
 * Not a utility for its own sake: `Promise.all` over the sample would put
 * twelve requests on the wire at once, and the API's bucket is 20 rps per
 * principal shared with every other tab the operator has open. This is the
 * one place in this file that fans out, so the limiter lives here rather than
 * in a lib nothing else imports.
 */
async function mapWithLimit<T, R>(
  items: readonly T[],
  limit: number,
  fn: (item: T) => Promise<R>,
): Promise<R[]> {
  const out = new Array<R>(items.length)
  let next = 0
  const worker = async (): Promise<void> => {
    for (;;) {
      const i = next++
      const item = items[i]
      if (item === undefined) return
      out[i] = await fn(item)
    }
  }
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker))
  return out
}

/**
 * Attempts for the spend fixture: the same rows every other attempt read
 * serves (`fixtureAttemptRows`), so the spend panel's sum is the attempts'.
 * The mix it has to survive is `fixtureUsage`'s: figures on the newest
 * finished attempt, none on a retry, a task whose attempt reported tokens and
 * no cost, and mock and generic runners that report nothing at all.
 */
async function fixtureSpendAttempts(task: Task): Promise<Result<{ attempts: AttemptRow[] }>> {
  return fixtureAttempts(task.id)
}

// ---------------------------------------------------------------------------
// The outcome ledger (#185): `GET /v1/outcomes`, and the catalogue its profile
// filter offers.
// ---------------------------------------------------------------------------

/**
 * One read of the outcome ledger. The query comes from `outcomesQuery` in
 * outcomes.ts, which is the only place the page's view becomes route
 * parameters, so no screen can send a combination the contract refuses.
 *
 * THE ROUTE IS THE TEMPLATE (CH-18): the query goes through `route()`'s third
 * argument, so the probe registry keys this as `/v1/outcomes` however many
 * spans and filters a reader tries.
 *
 * NEVER EMPTY. A span always has buckets -- a bucket with nothing in it is a
 * measured zero the ledger draws -- so a 2xx is always `ok`, and "nothing
 * happened in these fourteen days" is fourteen drawn zeroes, not an empty
 * state.
 */
export async function loadOutcomes(query: URLSearchParams): Promise<Result<Outcomes>>
export async function loadOutcomes<S extends OutcomeSection>(
  query: URLSearchParams,
  sections: readonly S[],
): Promise<Result<OutcomesPart<S>>>
export async function loadOutcomes(
  query: URLSearchParams,
  sections: readonly OutcomeSection[] = [],
): Promise<Result<Outcomes | OutcomesPart<OutcomeSection>>> {
  const q = new URLSearchParams(query)
  for (const s of sections) q.append('section', s)
  if (USE_FIXTURES) return fixtureOutcomes(q)
  return read<Outcomes>(route('/v1/outcomes', {}, q), () => false)
}

/**
 * THE PARTS OF THE LEDGER ONE READ MAY ASK FOR (#377): `section`, repeatable,
 * names the response keys wanted -- `swarm_api.outcomes.SECTIONS`, in its
 * order, which `test_outcomes_sections.py` holds this list to. Everything
 * else in the payload (the resolved range, scope, filters, vocab, reads,
 * cached, generated_at) is the envelope and comes with every section. No
 * section is the whole payload, as it always was.
 */
export const OUTCOME_SECTIONS = [
  'buckets',
  'totals',
  'retries',
  'latency',
  'groups',
  'workflows_failed',
  'coverage',
  'previous',
] as const
export type OutcomeSection = (typeof OUTCOME_SECTIONS)[number]

/** A sectioned payload: the envelope, and only the sections `S` that were asked for. */
export type OutcomesPart<S extends OutcomeSection> = Omit<Outcomes, OutcomeSection> & Pick<Outcomes, S>

async function fixtureOutcomes(query: URLSearchParams): Promise<Result<Outcomes>> {
  await new Promise((r) => setTimeout(r, 260))
  const target = route('/v1/outcomes', {}, query)
  // Platform scope is admin-only, and the member persona is not an admin
  // (`fixtureMe`), so the fixture answers it the way the route would.
  if (query.get('scope') === 'platform' && !fixtureIsAdmin) {
    noteFixtureProbe(target, 260, false)
    return {
      status: 'error',
      error: {
        kind: 'admin_required',
        httpStatus: 403,
        code: 'forbidden',
        message: 'admin group membership is required for this operation',
      },
    }
  }
  noteFixtureProbe(target, 260, true)
  // A sectioned read carries the envelope and its sections only, as the route
  // serves it, so a card that reads a key it did not ask for fails here too.
  const asked = query.getAll('section')
  // Ending at the fixture's clock, so the decided window is the fourteen days before it (QAFIX6-FIXTURE e).
  const whole: Record<string, unknown> = { ...ledgerFixture(FIXTURE_CLOCK) }
  if (asked.length > 0) {
    for (const s of OUTCOME_SECTIONS) if (!asked.includes(s)) delete whole[s]
  }
  return { status: 'ok', fetchedAt: Date.now(), data: whole as unknown as Outcomes }
}

/**
 * The runner profiles the catalogue serves, by name -- disabled ones too,
 * because a task that ran on a profile since disabled still has outcomes to
 * filter by. Never a hardcoded list: the catalogue is frozen contract data
 * that can gain entries.
 */
export async function loadRunnerProfiles(): Promise<Result<string[]>> {
  if (USE_FIXTURES) return fixtureRuntimeNames()
  const r = await readRuntimes()
  if (r.status === 'ok' || r.status === 'stale') {
    return { status: 'ok', fetchedAt: r.fetchedAt, data: Object.keys(r.data.runtimes).sort() }
  }
  return r as Result<string[]>
}

// ---------------------------------------------------------------------------
// Work › Repositories and Git tokens (repositories.html, picked 2026-10-05;
// docs/repo-index.md §6.1, docs/git-tokens.md §7). The routes are being built
// in parallel by lanes RI1, RI2 and GT1; every answer is normalised in
// RepositoriesData.ts, and a route that is not there yet reads as a 404 with
// no code, which the screens draw as "not served yet" naming the route.
//
// THE FIXTURE BUILD SERVES WHAT THE CAPTURES DRAW (QAFIX4-FIXTURE-A, visual
// QA run 4 admin-002, admin-003, overview-automate-009..012, work-a-011), and
// nothing else: `FIXTURE_READS` answers the registered repositories, the
// Settings pages' reads, the people list and the runner sizes, each as the
// raw body the route serves, so the SAME normaliser a live answer meets runs
// over it. Since QAFIX6-FIXTURE d it answers the onboarding, GitHub
// permissions, access and access-owner reads too. Every other `readAs` route
// -- git tokens, the graph, an owner's repository page, the members list -- is
// still answered as not served, exactly as an API without it.
// ---------------------------------------------------------------------------

// THE ANSWER NAMES THE ROUTE IT WAS ASKED FOR (QA2-FIX-07, admin2-08): it
// said "no repository or git-token routes" after Approve, Roles in workspaces
// and Runner sizes Save too, none of which is either.
function repoNotServed<T>(target: ApiRoute, method = 'GET'): Result<T> {
  noteFixtureProbe(target, 0, false)
  return {
    status: 'error',
    error: { kind: 'not_found', httpStatus: 404, code: null, message: `The fixture build does not serve this route (${method} ${target.template}).`, template: target.template },
  }
}

/** A fixture read's answer: the raw body the route serves, or `admin` for an admin-gated route the member is refused. */
type FixtureBody = { body: unknown } | 'admin'

const FIXTURE_HOUR = 3_600_000
const fixtureAgo = (ms: number): string => new Date(FIXTURE_NOW - ms).toISOString()
/** A short commit sha, as the fixture tasks write them, from a prefix. */
const fixtureSha = (prefix: string): string => prefix.padEnd(10, '0')

/**
 * The registered repositories (run-4 work-a-011): three, one with a long
 * name so the list's overflow is drawn. Each index is small and agrees with
 * its own index document (`fixtureRepoIndex`): `coverage.modules` is the
 * number of modules that document lists.
 */
const FIXTURE_REPOS = [
  { repo_id: 'repo_5e1f0c2a9b3d4e61', repo: 'example-api', on_change: 'webhook', interval_hours: 24, modules: 6, tested: 4, edges: 18, sha: '4c1e9a' },
  { repo_id: 'repo_7c2d9e4f1a0b3c85', repo: 'example-web', on_change: 'poll', interval_hours: 12, modules: 4, tested: 1, edges: 3, sha: '8d02f7' },
  { repo_id: 'repo_a4b8c1d2e3f40917', repo: 'example-payments-reconciliation-service-monorepo', on_change: 'off', interval_hours: 168, modules: 8, tested: 5, edges: 27, sha: 'b37a51' },
] as const

/**
 * Each fixture registration's `agents_may_file_issues` once the fixture build
 * changed it (`patchRepositoryFileIssues`); absent is the platform default the
 * API applies to a registration that is not `platform: true`, off.
 */
const fixtureFileIssues = new Map<string, boolean>()

/**
 * Each fixture registration's `workflow_files` once the fixture build changed
 * it (`patchRepositoryWorkflowFiles`); absent is off, the API's default (#2029).
 */
const fixtureWorkflowFiles = new Map<string, boolean>()

/**
 * The fixture registrations the fixture build removed (`deleteRepository`):
 * gone from its list and its counts, and still among its grants, so the
 * page draws each again as a dashed "chosen, not registered" card.
 */
const fixtureRemoved = new Set<string>()

/**
 * `GET /v1/repositories/counts` for the fixture registrations: secrets and QA
 * targets, counts only. Both are 0 because the fixture build serves no QA
 * target (`loadQaTargets`) and no secret granted to a repository
 * (`fixtureWorkspaceSecrets`): a card saying "QA targets: 1" would link to a
 * list that says there are none.
 */
function fixtureRepositoryCounts(): unknown {
  return {
    tenant_id: FIXTURE_TENANT,
    counts: FIXTURE_REPOS.map((r) => ({
      repo_id: r.repo_id, repository: `example-org/${r.repo}`, secrets: 0, qa_targets: 0,
    })).filter((c) => !fixtureRemoved.has(c.repo_id)),
  }
}

function fixtureRepositories(): unknown {
  return {
    repositories: FIXTURE_REPOS.map((r, i) => ({
      repo_id: r.repo_id, tenant_id: FIXTURE_TENANT, forge: 'github', owner: 'example-org', repo: r.repo,
      repository_url: `https://github.com/example-org/${r.repo}`, default_branch: 'main',
      allowed_profiles: ['claude-code'], created_by: 'alice@example.com', created_at: fixtureAgo((20 - i * 5) * 24 * FIXTURE_HOUR),
      index: {
        interval_hours: r.interval_hours, on_change: r.on_change, min_change_interval_minutes: 120, paused: false,
        current_sha: fixtureSha(r.sha), last_indexed_at: fixtureAgo((i + 2) * FIXTURE_HOUR), head_sha: fixtureSha(r.sha),
        behind_by: 0, stale: false, in_flight_task_id: null,
        coverage: { modules: r.modules, modules_with_tests: r.tested, test_map_edges: r.edges, always_tests: 1 },
        next_run_at: new Date(FIXTURE_NOW + (r.interval_hours - i - 2) * FIXTURE_HOUR).toISOString(),
        last_failure: null, keep_versions: 5,
      },
      last_run: null,
      index_credential: { source: 'tenant', secret_name: `swarm-tenant-${FIXTURE_TENANT}-git` },
      agents_may_file_issues: fixtureFileIssues.get(r.repo_id) ?? false,
      workflow_files: fixtureWorkflowFiles.get(r.repo_id) ?? false,
      // Two of three open to agents, so Submit's Additional repositories picker
      // draws a list and the gate both (QA1-FIX-10, runs-submit-16).
      readable_by_tasks: i < 2, archived: false,
    })).filter((x) => !fixtureRemoved.has(x.repo_id)),
  }
}

/** `GET /v1/repositories/{repo_id}/index?format=json` for a fixture repository: its modules, as many as its coverage says. */
function fixtureRepoIndex(repoId: string): unknown {
  const r = FIXTURE_REPOS.find((x) => x.repo_id === repoId)
  if (r === undefined) return undefined
  const modules = Array.from({ length: r.modules }, (_v, i) => ({ path: `src/module_${i + 1}`, files: 4 + i, lines: 300 + 40 * i, language: 'typescript' }))
  return { index: { commit_sha: fixtureSha(r.sha), truncated: [] }, document: { modules, truncated: [] } }
}

/**
 * `GET /v1/repositories/{repo_id}/graph-cache` for a fixture repository
 * (CACHE-4-CONSOLE): two replicas read and ONE NOT READ (a peer that timed
 * out), so the fixture build draws the hard case of §5.3, and no `carried`
 * count, so the reuse tile is its dash. `shards=1` adds a page of the list,
 * changed shards first. Digests are built here, never written out whole.
 */
function fixtureGraphCache(url: string): unknown {
  const repoId = fixtureSegment(url, 3)
  const r = FIXTURE_REPOS.find((x) => x.repo_id === repoId)
  if (r === undefined) return undefined
  const MiB = 1024 * 1024
  const digest = (seed: string) => `sha256:${seed.repeat(Math.ceil(64 / seed.length)).slice(0, 64)}`
  const current = digest(`${r.sha}c`)
  const warm = { digest: current, ms: 2140, shards: 46, replayed: 200 }
  const replicas = [
    {
      replica: 'swarm-graph-0', state: 'read', started_at: fixtureAgo(3 * 24 * FIXTURE_HOUR), uptime_seconds: 3 * 24 * 3600,
      result_bytes: 61 * MiB, result_bound: 256 * MiB, shard_bytes: 188 * MiB, shard_bound: 512 * MiB,
      answers_hot: 412, shards_hot: 31, last_warm: warm,
    },
    {
      replica: 'swarm-graph-1', state: 'read', started_at: fixtureAgo(5 * FIXTURE_HOUR), uptime_seconds: 5 * 3600,
      result_bytes: 23 * MiB, result_bound: 256 * MiB, shard_bytes: 97 * MiB, shard_bound: 512 * MiB,
      answers_hot: 140, shards_hot: 18, last_warm: { ...warm, ms: 1870 },
    },
    { replica: 'swarm-graph-2', state: 'not_read', reason: 'timeout' },
  ]
  const layers = ['symbols', 'callers', 'callees', 'tests', 'signatures', 'files', 'terms']
  const rows = Array.from({ length: r.modules * layers.length }, (_v, i) => {
    const layer = layers[i % layers.length]!
    const mod = `src/module_${Math.floor(i / layers.length) + 1}`
    const changed = i % 5 === 0
    const hits = changed ? 3 + i : 40 + ((i * 37) % 300)
    return {
      layer, module: mod, blob: digest(`${(i + 11).toString(16)}e`), raw_bytes: 4096 + i * 1531,
      last_merge: changed ? 'changed' : 'unchanged',
      replicas: [{ replica: 'swarm-graph-0', hot: i % 3 !== 0 }, { replica: 'swarm-graph-1', hot: i % 4 === 0 }],
      hits_24h: hits, last_read: fixtureAgo((i % 9) * 6 * 60_000 + 40_000),
    }
  }).sort((a, b) => Number(b.last_merge === 'changed') - Number(a.last_merge === 'changed') || b.hits_24h - a.hits_24h)
  const page = Math.max(1, Number(new URLSearchParams(url.split('?')[1] ?? '').get('page') ?? '1') || 1)
  const withShards = (url.split('?')[1] ?? '').includes('shards=1')
  return {
    repo_id: repoId,
    index: { commit_sha: fixtureSha(r.sha), manifest_digest: current, previous_sha: fixtureSha(`${r.sha}p`), previous_digest: digest(`${r.sha}p`) },
    // swarm-graph-1 restarted 5 h ago, so the window covers 5 h, not 24.
    facts: {
      window_hours: 5, answers: 1284, hit: 1168, carried: null, miss: 116, hit_rate: 1168 / 1284,
      reused_share: null, served_p95_ms: 38, last_warm: warm,
    },
    replicas,
    stats: { state: 'read' },
    latency: { p50_ms: 61, p95_ms: 152, max_ms: 410, reference_ms: 20.5, symbols: 9120 },
    ...(withShards
      ? {
          shards: {
            state: 'read', comparison: 'read', total: rows.length, changed: rows.filter((x) => x.last_merge === 'changed').length,
            page, page_rows: 100, rows: rows.slice((page - 1) * 100, page * 100),
          },
        }
      : {}),
  }
}

// ---------------------------------------------------------------------------
// SETUP AND ACCESS IN THE FIXTURE BUILD (QAFIX6-FIXTURE d, visual QA run 6
// work-b-006, part of #1680). These reads were answered as not served, so no
// run had ever drawn /setup's checklist, its GitHub card or /access's table.
// Each is the raw body its route serves (swarm_api/onboarding.py `derive`,
// forgeperms.py, access.py), for alice@example.com connected through the App
// as `alice-example`, over the three registered repositories under
// `example-org`. Their states are mixed on purpose, and they agree with each
// other: `example-labs` asks for SAML SSO, so orgs_enabled failed with the
// server's own §2.3 copy; example-web's verify has not run, so access_verified
// is in progress; the org's installation has not accepted `actions: read`, so
// every repository reports `actions` blocked. A checklist that read all done
// would hide the screens' failure paths, which are what they are for.
// ---------------------------------------------------------------------------

/** The App connection every Setup and Access read names. Names only: no value of any credential. */
function fixtureGitHubConnection(): Record<string, unknown> {
  return {
    connection_id: 'conn_alice_example', forge: 'github', method: 'app_user', forge_login: 'alice-example',
    forge_user_id: 4_210_771, token_id: 'gt_alice_example_app', secret_name: `swarm-tenant-${FIXTURE_TENANT}-github-alice-example`,
    state: 'active', failure: null,
    access_expires_at: new Date(FIXTURE_NOW + 6 * FIXTURE_HOUR).toISOString(),
    refresh_expires_at: new Date(FIXTURE_NOW + 180 * 24 * FIXTURE_HOUR).toISOString(),
    refreshed_at: fixtureAgo(2 * FIXTURE_HOUR), refreshing: false,
    created_at: fixtureAgo(9 * 24 * FIXTURE_HOUR), connected_at: fixtureAgo(9 * 24 * FIXTURE_HOUR), revoked_at: null,
  }
}

/** Each registered repository's grant and its last verify: example-web's has not run yet. */
function fixtureAccessGrants(): Record<string, unknown>[] {
  return FIXTURE_REPOS.map((r, i) => {
    const verified = r.repo !== 'example-web'
    const check = (state: string) => ({ state, code: null, checked_at: verified ? fixtureAgo((3 + i) * FIXTURE_HOUR) : null })
    return {
      repo_id: r.repo_id, repository: `example-org/${r.repo}`, owner: 'example-org',
      mode: i === 2 ? 'read' : 'write', can_push: true, archived: false,
      granted_at: fixtureAgo((8 - i) * 24 * FIXTURE_HOUR), granted_by: 'alice@example.com',
      checks: verified
        ? { clone: check('ok'), push: check(i === 2 ? 'not_required' : 'ok'), pull_request: check(i === 2 ? 'not_required' : 'ok') }
        : {},
      verified_at: verified ? fixtureAgo((3 + i) * FIXTURE_HOUR) : null,
    }
  })
}

/**
 * When Hide was pressed in this page's life, or null (QA2-FIX-07,
 * overview-agents-04): `POST /v1/onboarding/dismiss` is answered as the route
 * answers it and the next read says so, as the server keeps it per person.
 */
let fixtureOnboardingHiddenAt: string | null = null

/** `GET /v1/onboarding`: seven steps as `derive` serves them with the workspace gate off, in mixed states. */
function fixtureOnboarding(): unknown {
  // THE PICKED WORKSPACE'S CHECKLIST (QA2-FIX-07 review, admin-shell-01): the
  // read names the acting workspace, and eng, a group, has people to invite;
  // the strip drew u-bogdan's 'a personal workspace has nobody to invite'.
  const acting = fixtureActingTenant()
  const group = acting === FIXTURE_GROUP.tenant_id
  const checked = fixtureAgo(4 * 60_000)
  const step = (name: string, state: string, evidence: Record<string, unknown>, issues: unknown[] = []) => ({
    step: name, state, code: null, copy: null, checked_at: state === 'todo' ? null : checked, evidence, issues,
  })
  const ssoUrl = 'https://github.com/orgs/example-labs/sso'
  const steps = [
    step('signed_in', 'done', { email: 'alice@example.com', tenant_id: acting }),
    step('github_connected', 'done', { via: 'user', kind: 'app_user', forge_login: 'alice-example', token_state: 'active' }),
    step('app_installed', 'done', { needed: true, read: true, source: 'github', login: 'alice-example', installed: ['example-org'], not_installed: ['example-labs'], install_url: null, app_name: null }),
    {
      ...step('orgs_enabled', 'failed', {
        owners: [
          { owner: 'alice-example', owner_type: 'User', source: 'account', reach: 'reachable', registered: 0 },
          { owner: 'example-org', owner_type: 'Organization', source: 'orgs', reach: 'reachable', registered: FIXTURE_REPOS.length },
          { owner: 'example-labs', owner_type: 'Organization', source: 'refusal', reach: 'sso_required', registered: 0 },
        ],
        orgs_read: true, orgs_capped: false, sso_hidden_orgs: 0, read_at: checked,
      }, [{
        code: 'SSO_NOT_AUTHORISED', owner: 'example-labs', url: ssoUrl,
        copy: "example-labs uses SAML single sign-on and GitHub has not linked your session to it yet. Open https://github.com/orgs/example-labs/sso, sign in with example-labs's identity provider, then press Re-check. Nothing in SwarmCloud has to change.",
      }]),
      code: 'SSO_NOT_AUTHORISED',
    },
    step('repos_chosen', 'done', { repositories: fixtureAccessGrants().map((g) => ({ repository: g.repository, mode: g.mode })) }),
    step('access_verified', 'in_progress', {
      repositories: fixtureAccessGrants().map((g) => ({ repository: g.repository, result: g.verified_at === null ? 'pending' : 'passed' })),
    }),
    step('ready', 'todo', { waiting_for: 'orgs_enabled' }),
  ]
  return {
    tenant_id: acting, user: 'alice@example.com', user_hash: '5a1ce0e8a3b7c912', derived_at: new Date(FIXTURE_NOW).toISOString(),
    steps, next_step: 'orgs_enabled', complete: false, dismissed: fixtureOnboardingHiddenAt !== null, dismissed_at: fixtureOnboardingHiddenAt, source: 'derived on this read',
    // config_setup.py's field, which the live read carries and the strip's
    // Secrets set and People invited are drawn from (QA1-FIX-10, config-09).
    // It agrees with `fixtureSecretsOverview`'s workspace scope: MCP server
    // kb's credential is the one thing needed and not stored.
    config_setup: {
      secrets: {
        needed: 1, done: false,
        items: [{ kind: 'mcp_server', name: 'kb', needed_by: 'kb', where: 'config/secrets' }],
        sources: { mcp_servers: { needed: 1, reason: null }, qa_targets: { needed: 0, reason: null }, provider_key: { needed: 0, reason: null } },
      },
      // config_setup.people_step's two answers: eng's only role holder is the
      // reader (`fixtureAdminWorkspacesRoles`), so nobody else is in it yet.
      people: group
        ? { applies: true, done: false, holders: 0, admins: 0, pending: null, reason: null, pending_reason: 'this workspace takes no requests to join; its people come from its group' }
        : { applies: false, done: null, holders: null, admins: null, pending: null, reason: 'a personal workspace has nobody to invite', pending_reason: null },
    },
  }
}

/** `GET /v1/forge/permissions`: forgeperms.py's REQUIRED, the App, the one installation, and each registered repository. */
function fixtureForgePermissions(): unknown {
  const req = (permission: string, access: string, needed_for: string, capabilities: string[]) => ({ permission, access, needed_for, capabilities })
  const required = [
    req('contents', 'write', "runs can't push their branch, and merges can't land", ['push', 'merge']),
    req('pull_requests', 'write', "runs can't open or update pull requests, or merge them", ['pull_request', 'merge']),
    req('statuses', 'read', "merges can't see CI, so they fail", ['merge', 'ci']),
    req('checks', 'read', "merges and CI fixes can't see check runs", ['merge', 'ci']),
    req('issues', 'write', "issue runs and the observer can't read or comment on issues", ['issues']),
    req('actions', 'read', "ci-flake-hunter and release-health can't read workflow runs", ['actions']),
  ]
  const app = { contents: 'write', pull_requests: 'write', statuses: 'read', checks: 'read', issues: 'write', actions: 'read', metadata: 'read' }
  const { actions: _notAccepted, ...installed } = app
  const actionsGap = { ...required[5], granted: null }
  return {
    checked_at: fixtureAgo(4 * 60_000),
    required,
    workflow_files_required: req('workflows', 'write', 'workflow-file pushes fall back to workflow-lines.patch (Workflows: not granted)', ['workflow_files']),
    app: { configured: true, slug: 'swarmcloud', granted: app, missing: [], settings_url: null, error: null },
    orgs: [{
      org: 'example-org', account_type: 'Organization', installation_id: 51_877_204,
      granted: installed, missing: [actionsGap], pending: [{ permission: 'actions', requested: 'read', granted: null }],
      review_url: 'https://github.com/organizations/example-org/settings/installations/51877204', error: null,
    }],
    repositories: FIXTURE_REPOS.map((r) => ({
      repo_id: r.repo_id, repo: `example-org/${r.repo}`, credential: 'app', installation_id: 51_877_204,
      capabilities: { push: true, pull_request: true, merge: true, ci: true, issues: true, actions: false },
      blocked_by: [actionsGap], error: null,
    })),
    cached: false,
  }
}

/** `GET /v1/access`: the connection, the enabled owner and the three grants. */
function fixtureAccess(): unknown {
  return {
    connection: fixtureGitHubConnection(),
    orgs: [{
      owner: 'example-org', method: 'app', owner_type: 'Organization', installation_id: 51_877_204, repository_selection: 'selected',
      install_state: 'installed', sso: 'ok', enabled: true, enabled_at: fixtureAgo(9 * 24 * FIXTURE_HOUR), checked_at: fixtureAgo(4 * 60_000),
    }],
    requested: [],
    grants: fixtureAccessGrants(),
    tenant_id: fixtureActingTenant(),
  }
}

/** `GET /v1/access/orgs`: the person's account, the installed org, and the org that asks for SSO. */
function fixtureAccessOwners(): unknown {
  const owner = (name: string, type: string, state: string, sso: string, enabled: boolean, installationId: number | null) => ({
    owner: name, owner_type: type, installation_id: installationId, repository_selection: installationId === null ? null : 'selected',
    install_state: state, sso, enabled,
    install_url: state === 'installed' ? null : 'https://github.com/apps/swarmcloud/installations/new',
  })
  return {
    owners: [
      owner('alice-example', 'User', 'not_installed', 'ok', false, null),
      owner('example-org', 'Organization', 'installed', 'ok', true, 51_877_204),
      owner('example-labs', 'Organization', 'not_installed', 'required', false, null),
    ],
    orgs_listed: true,
    install_url: 'https://github.com/apps/swarmcloud/installations/new',
    app_name: null,
    tenant_id: FIXTURE_TENANT,
  }
}

/** The person both personas are: the shell's alice@example.com, platform role per persona. */
function fixtureIdentity(): Record<string, unknown> {
  return {
    email: 'alice@example.com', display_name: null, display_name_suggested: 'Alice', domain: 'example.com',
    platform_role: fixtureIsAdmin ? 'admin' : null, is_admin: fixtureIsAdmin,
  }
}

/**
 * `GET /v1/profile` (run-4 overview-automate-012): the person, their
 * workspaces (`eng` too for the platform-admin persona, as /tenants/mine
 * says), a GitHub connection per workspace, the TypeSafe key the provider-keys
 * fixture describes, and the Claude accounts fixtureAccounts holds.
 */
function fixtureProfile(): unknown {
  const acting = fixtureActingTenant()
  const workspaces: Record<string, unknown>[] = [{
    tenant_id: FIXTURE_TENANT, display_name: `Your workspace (${FIXTURE_TENANT})`, kind: 'user',
    role: { name: 'admin', source: 'personal', via: 'personal' }, state: 'ready', selectable: true, is_acting: acting === FIXTURE_TENANT, is_default: true,
  }]
  // Every persona `/v1/tenants/mine` lists eng for, and acting in it once it is picked (admin-shell-01).
  if (fixtureIsAdmin || fixtureIsWorkspaceAdmin) {
    workspaces.push({
      tenant_id: FIXTURE_GROUP.tenant_id, display_name: FIXTURE_GROUP.principal, kind: 'group',
      role: { name: 'admin', source: 'override', via: 'member' }, state: 'ready', selectable: true, is_acting: acting === FIXTURE_GROUP.tenant_id, is_default: false,
    })
  }
  const github = workspaces.map((w, i) => ({
    tenant_id: w.tenant_id, state: 'active', forge_login: 'alice-example', method: 'app',
    access_expires_at: new Date(FIXTURE_NOW + (6 - i) * FIXTURE_HOUR).toISOString(), refreshed_at: fixtureAgo((2 + i) * FIXTURE_HOUR),
  }))
  return {
    identity: fixtureIdentity(),
    workspaces,
    default_workspace: { tenant_id: null, source: null, ignored: null },
    connections: {
      github,
      typesafe: {
        personal: { set: true, set_at: fixtureAgo(5 * 24 * FIXTURE_HOUR), set_by: 'alice@example.com', version: 2 },
        acting_workspace: { tenant_id: acting, set: false, set_at: null, set_by: null, version: null },
        uses: 'user',
      },
    },
    notifications: { preferences: { muted: ['finished'], reminder: 'daily' }, topics: [...NOTIFICATION_TOPICS] },
    // Nothing saved: UTC. A zone this browser cached moves to the server on
    // the first read, which the fixture build does not serve, so it stays here.
    time_zone: 'UTC',
    time_zone_source: 'default',
    claude_accounts: {
      personal_workspace: FIXTURE_TENANT,
      // The accounts Capacity › Accounts lists, so the two pages agree: the
      // tenant's own, and the one lent to it. A Name given on Capacity ›
      // Accounts (or cleared there) shows here too; until one is given or
      // cleared, `primary` carries one, so the fixture build draws a named
      // account beside unnamed ones (#1913).
      accounts: fixtureAccounts.map((a) => ({
        account_id: a.account_id, label: a.label, relation: a.owner_tenant === FIXTURE_TENANT ? 'own' : 'borrowed', state: a.state,
        windows: a.windows, lent_to_count: a.owner_tenant === FIXTURE_TENANT ? a.lend_to.length : null, stale: a.stale,
        display_name: a.display_name !== undefined ? a.display_name : (a.account_id === `${FIXTURE_TENANT}:primary` ? 'Main subscription' : null),
      })),
      unreadable_document_count: 0,
    },
    revision: 3,
    unavailable: [],
  }
}

/** `GET /v1/provider-keys/{provider}` (run-4 overview-automate-009): your key set, the personal tenant's not, so yours is used. Never a value: only whether one is set. */
function fixtureProviderKeys(provider: string): unknown {
  return {
    provider,
    user: { available: true, set: true, set_at: fixtureAgo(5 * 24 * FIXTURE_HOUR), set_by: 'alice@example.com', version: '2' },
    tenant: { tenant_id: FIXTURE_TENANT, set: false, set_at: null, set_by: null, version: null },
    resolution: { tenant_id: FIXTURE_TENANT, uses: 'user' },
    consumed_by: ['jev'],
  }
}

/** `GET /v1/judgment`: the personal workspace's Jev backend, editable by its owner; TypeSafe is available because your key is set. */
function fixtureJudgment(): unknown {
  return {
    tenant_id: FIXTURE_TENANT, can_edit: true,
    jev: {
      backend: 'internal-jev', label: 'Internal Jev', available: true, reason: null, default: 'internal-jev',
      set_at: fixtureAgo(3 * 24 * FIXTURE_HOUR), set_by: 'alice@example.com',
      options: [
        { backend: 'internal-jev', label: 'Internal Jev', available: true, reason: null },
        { backend: 'internal-laya', label: 'Internal Laya', available: true, reason: null },
        { backend: 'typesafe', label: 'TypeSafe', available: true, reason: null },
      ],
    },
  }
}

/** `GET /v1/code-intelligence` (run-4 overview-automate-010): off, as it is by default, and the owner may switch it. */
function fixtureCodeIntelligence(): unknown {
  return { tenant_id: FIXTURE_TENANT, can_edit: true, jev: false, context: 'on', updated_at: null, updated_by: null }
}

/** `GET /v1/qa-settings` (QA-FLOWS-UI): no value stored, so the default 15; the owner may change it. */
function fixtureQaSettings(): unknown {
  return {
    tenant_id: FIXTURE_TENANT, qa_flows_per_step: 15, defaults: { qa_flows_per_step: 15 },
    bounds: { qa_flows_per_step: { min: 1, max: 30 } }, set_at: null, set_by: null, can_edit: true,
  }
}

/** `GET /v1/workspace/roles`: u-bogdan is a personal workspace, which has only its owner, the admin. */
function fixtureWorkspaceRoles(): unknown {
  return {
    tenant_id: FIXTURE_TENANT, kind: 'user', default_role: null, default_source: null, can_manage: true,
    me: { email: 'alice@example.com', role: 'admin', source: 'personal', via: 'personal' },
    people: [], roles: FIXTURE_ROLE_SUMMARIES, audit: [],
  }
}

const FIXTURE_ROLE_SUMMARIES = [
  { name: 'viewer', summary: 'Reads the workspace: agents, runs, logs and artifacts.' },
  { name: 'member', summary: 'Starts and cancels agents and runs, and answers their questions.' },
  { name: 'admin', summary: 'Changes the workspace: its people, roles, keys and settings.' },
]

/** `GET /v1/admin/workspaces/roles` (People's roles card): `eng`, whose first admin is the persona. */
function fixtureAdminWorkspacesRoles(): unknown {
  return {
    workspaces: [{
      tenant_id: FIXTURE_GROUP.tenant_id, group: FIXTURE_GROUP.principal, registered_by: 'ops@example.com',
      kind: 'group', default_role: 'member', default_source: 'set',
      people: [{ email: 'alice@example.com', role: 'admin', source: 'override', granted_by: 'ops@example.com', granted_at: fixtureAgo(30 * 24 * FIXTURE_HOUR), reason: 'first admin' }],
    }],
    roles: FIXTURE_ROLE_SUMMARIES,
    next_page_token: null,
  }
}

/** The grid's columns (ADMIN-PEOPLE-ROLES): eng, the personas' group, and research. */
const FIXTURE_PEOPLE_COLUMNS = [
  { tenant_id: FIXTURE_GROUP.tenant_id, group: FIXTURE_GROUP.principal, display_name: FIXTURE_GROUP.principal, default_role: 'member', unavailable: false },
  { tenant_id: 'research', group: 'research@example.com', display_name: 'research@example.com', default_role: 'viewer', unavailable: false },
]

/**
 * `GET /v1/admin/people` as the `workspace-admin` persona reads it (S5f):
 * eng alone, its people's cells and when they were last seen, nothing else.
 * ops, a platform owner, is marked `platform_admin` as `people.mark_platform_admin`
 * marks them in either read, so the scoped read draws "(platform admin)" too
 * (ROLE-MARK; part of #2391).
 */
function fixtureScopedPeople(): unknown {
  const people = [
    { email: 'alice@example.com', display_name: null, roles: { eng: 'admin' }, last_active: fixtureAgo(5 * 60_000) },
    { email: 'bob@example.com', display_name: 'Bob', roles: { eng: 'member' }, last_active: fixtureAgo(3 * FIXTURE_HOUR) },
    { email: 'erin@example.com', display_name: null, roles: { eng: 'viewer' }, last_active: null },
    { email: 'ops@example.com', display_name: null, roles: { eng: 'admin' }, last_active: fixtureAgo(FIXTURE_HOUR), platform_admin: true },
  ]
  return { scope: 'workspaces', workspaces: [FIXTURE_PEOPLE_COLUMNS[0]], workspaces_error: null, people, count: people.length, pending: 0 }
}

/**
 * `GET /v1/admin/people` (run-4 admin-003): four people -- three signed in
 * with a ready workspace, one pending (a requested workspace) -- and one
 * admin, the persona. `count` and `pending` are counted from the rows.
 * alice and bogdan, on the admins list, are marked `platform_admin` with admin
 * in every column, as `people.role_cell` and `people.mark_platform_admin`
 * serve them: the page draws "(platform admin)" from the marker alone (ROLE-MARK).
 */
function fixturePeople(): unknown {
  const ws = (state: string, id: string | null, extra: Record<string, unknown> = {}) => ({
    state, setup_url: '', setup_command: '', kind: 'personal', workspace_id: id, tenant_id: id, ...extra,
  })
  const claude = (source: string, own: number, lent: number, lentBy: string[], providerKey = false) => ({
    source, own, lent, lent_by: lentBy, provider_key: providerKey,
  })
  const ownAccounts = fixtureAccounts.filter((a) => a.owner_tenant === FIXTURE_TENANT)
  const people = [
    {
      email: 'carol@example.com', display_name: 'Carol', teams: [], github: 'none', roles: { eng: 'none', research: 'none' },
      workspace: ws('requested', 'w-c4r01e', { request_id: 'wsr_c4r0l', requested_at: fixtureAgo(40 * 60_000), requested_via: 'console' }),
      claude_account: claude('none', 0, 0, []), loan_request: null,
      first_seen: fixtureAgo(45 * 60_000), last_seen: fixtureAgo(40 * 60_000), last_submitted: null, last_active: fixtureAgo(40 * 60_000),
    },
    {
      email: 'alice@example.com', display_name: null, teams: [FIXTURE_GROUP.principal], github: 'connected', roles: { eng: 'admin', research: 'admin' }, platform_admin: true,
      workspace: ws('ready', FIXTURE_TENANT, { ready_at: fixtureAgo(9 * 24 * FIXTURE_HOUR) }),
      claude_account: claude('own', ownAccounts.length, 1, [FIXTURE_GROUP.tenant_id]), loan_request: null,
      first_seen: fixtureAgo(9 * 24 * FIXTURE_HOUR), last_seen: fixtureAgo(5 * 60_000), last_submitted: fixtureAgo(20 * 60_000), last_active: fixtureAgo(5 * 60_000),
    },
    {
      email: 'bob@example.com', display_name: 'Bob', teams: [FIXTURE_GROUP.principal], github: 'connected', roles: { eng: 'member', research: 'viewer' },
      workspace: ws('ready', 'u-bob', { ready_at: fixtureAgo(6 * 24 * FIXTURE_HOUR) }),
      claude_account: claude('lent', 0, 1, [FIXTURE_TENANT]), loan_request: { state: 'lent', requested_at: fixtureAgo(6 * 24 * FIXTURE_HOUR), decided_at: fixtureAgo(5 * 24 * FIXTURE_HOUR) },
      first_seen: fixtureAgo(6 * 24 * FIXTURE_HOUR), last_seen: fixtureAgo(3 * FIXTURE_HOUR), last_submitted: fixtureAgo(4 * FIXTURE_HOUR), last_active: fixtureAgo(3 * FIXTURE_HOUR),
    },
    {
      email: 'bogdan@example.com', display_name: 'Bogdan Alexandrescu', teams: [FIXTURE_GROUP.principal], github: 'connected', roles: { eng: 'admin', research: 'admin' }, platform_admin: true,
      workspace: ws('ready', 'u-bogdan', { ready_at: fixtureAgo(12 * 24 * FIXTURE_HOUR) }),
      claude_account: { source: 'lent', own: 0, lent: 7, lent_by: ['eng'], provider_key: false, lent_accounts: Array.from({ length: 7 }, (_, i) => ({ account_id: i === 0 ? 'ai-eng-1-max' : `ai-eng-${i + 1}`, label: i === 0 ? 'ai-eng-1-max' : `ai-eng-${i + 1}`, owner_tenant: 'eng', owner_name: 'Engineering', state: 'AVAILABLE' })) },
      loan_request: null,
      first_seen: fixtureAgo(12 * 24 * FIXTURE_HOUR), last_seen: fixtureAgo(2 * 60_000), last_submitted: fixtureAgo(30 * 60_000), last_active: fixtureAgo(2 * 60_000),
    },
    {
      email: 'dave@example.com', display_name: null, teams: [], github: 'expired', roles: { eng: 'none', research: 'admin' },
      workspace: ws('ready', 'u-dave', { ready_at: fixtureAgo(20 * 24 * FIXTURE_HOUR) }),
      claude_account: claude('provider_key', 0, 0, [], true), loan_request: null,
      first_seen: fixtureAgo(20 * 24 * FIXTURE_HOUR), last_seen: fixtureAgo(4 * 24 * FIXTURE_HOUR), last_submitted: null, last_active: fixtureAgo(4 * 24 * FIXTURE_HOUR),
    },
  ]
  return {
    scope: 'platform',
    workspaces: FIXTURE_PEOPLE_COLUMNS,
    workspaces_error: null,
    people,
    count: people.length,
    pending: people.filter((p) => p.workspace.state === 'requested').length,
    audit: [
      ...Array.from({ length: 12 }, (_, i) => ({ action: 'qa_target_credential_set', target_workspace_id: 'eng', by: 'alice@example.com', at: fixtureAgo((i + 1) * 60_000), detail: {} })),
      ...Array.from({ length: 10 }, (_, i) => ({ action: 'repository_workflow_files_set', target_workspace_id: 'eng', by: 'alice@example.com', at: fixtureAgo((i + 20) * 60_000), detail: {} })),
      { action: 'approve', target_workspace_id: 'u-bob', by: 'alice@example.com', at: fixtureAgo(5 * 24 * FIXTURE_HOUR), detail: {} },
      { action: 'frobnicated_mcp_widget', target_workspace_id: 'eng', by: 'alice@example.com', at: fixtureAgo(6 * 24 * FIXTURE_HOUR), detail: {} },
    ],
    lendable_accounts: ownAccounts.map((a) => ({ account_id: a.account_id, owner_tenant: a.owner_tenant, label: a.label, state: a.state, lend_to: a.lend_to })),
    lendable_error: null,
    provisioning: { available: true, approved_waiting: 0 },
    admins: [{ email: 'ops@example.com', role: 'owner', granted_by: 'config:PLATFORM_OWNER', granted_at: null }, { email: 'bogdan@example.com', role: 'owner', granted_by: 'config:PLATFORM_OWNER', granted_at: null }, { email: 'alice@example.com', role: 'admin', granted_by: 'ops@example.com', granted_at: fixtureAgo(30 * 24 * FIXTURE_HOUR) }],
    admins_error: null,
  }
}

/**
 * Admin › Runner sizes (run-4 admin-002): the three classes the page sizes,
 * at the figures `/v1/resource-classes` serves (fixtureResourceClasses), in
 * effect as version 2 of a two-version history. `browser` carries its live
 * pod's split. The bounds are the page's own checks' inputs.
 */
const FIXTURE_LARGE: RunnerSize = { cpu_request: 8, cpu_limit: 8, memory_gib: 32, disk_gib: 16, units: 4 }
const FIXTURE_SIZES: Record<string, RunnerSize> = {
  standard: { cpu_request: 4, cpu_limit: 4, memory_gib: 8, disk_gib: 4, units: 1 },
  browser: {
    cpu_request: 4, cpu_limit: 4, memory_gib: 8, disk_gib: 4, units: 1,
    live_split: { worker: { cpu_request: 3, cpu_limit: 3, memory_gib: 6 }, display: { cpu_request: 1, cpu_limit: 1, memory_gib: 2 } },
  },
  large: FIXTURE_LARGE,
}
const FIXTURE_SIZE_PRICES = { vcpu_hour: 0.0864, gib_hour: 0.009, source: 'Cloud Run list price, us-central1' }

function fixtureRunnerSizes(): RunnerSizesDoc {
  return {
    current_version: 2, in_effect_version: 2, malformed: false,
    classes: FIXTURE_SIZES,
    defaults: { ...FIXTURE_SIZES, large: { ...FIXTURE_LARGE, memory_gib: 16 } },
    bounds: {
      cpu_request: { min: 0.5, max: 16, step: 0.5 }, cpu_burst_max_ratio: 2,
      memory_gib: { min: 1, max: 64 }, memory_per_cpu: { min: 1, max: 8 },
      live_split: { worker_min_cpu: 1, worker_min_memory_gib: 2, display_min_cpu: 0.5, display_min_memory_gib: 1 },
    },
    followers: {
      // GKE profiles follow a resize, Cloud Run ones stay at the code default
      // (owner Q3): fixtureCapacity runs these four on CLOUD_RUN_JOB.
      standard: { follows: [], fixed: ['mock', 'generic', 'claude-code', 'codex'] },
      browser: { follows: ['browser', 'claude-code-browser'], fixed: [] },
      large: { follows: [], fixed: [] },
    },
    prices: FIXTURE_SIZE_PRICES,
    updated_at: fixtureAgo(4 * 24 * FIXTURE_HOUR), updated_by: 'ops@example.com',
  }
}

/** The audit, newest first: version 2 raised `large`'s memory from the code default the history began at. */
function fixtureRunnerSizeVersions(): unknown {
  return {
    versions: [
      { version: 2, previous_version: 1, created_at: fixtureAgo(4 * 24 * FIXTURE_HOUR), created_by: 'ops@example.com', reason: 'large agents hit the memory limit on monorepo builds', changed: [{ class: 'large', field: 'memory_gib', from: 16, to: 32 }], restored_from: null },
      { version: 1, previous_version: 0, created_at: fixtureAgo(21 * 24 * FIXTURE_HOUR), created_by: 'ops@example.com', reason: 'first written: the code defaults', changed: [], restored_from: null },
    ],
  }
}

/** The `n`th path segment of a fixture read's URL: 3 is the provider of a provider-keys read. */
function fixtureSegment(url: string, n: number): string {
  return decodeURIComponent((url.split('?')[0] ?? '').split('/')[n] ?? '')
}

/** What a fixture route answers, by template; absent is not served. Admin routes refuse the member. */
const FIXTURE_READS: Readonly<Record<string, (url: string) => FixtureBody | undefined>> = {
  '/v1/repositories': () => ({ body: fixtureRepositories() }),
  // Config › Secrets and Admin › Platform secrets (SSC-UI).
  '/v1/secrets/overview': (url) => ({ body: fixtureSecretsOverview(url) }),
  '/v1/admin/platform-secrets': () => (fixtureIsAdmin ? { body: fixturePlatformSecrets() } : 'admin'),
  '/v1/repositories/counts': () => ({ body: fixtureRepositoryCounts() }),
  '/v1/repositories/{repo_id}/index': (url) => {
    const body = fixtureRepoIndex(fixtureSegment(url, 3))
    return body === undefined ? undefined : { body }
  },
  '/v1/repositories/{repo_id}/graph-cache': (url) => {
    const body = fixtureGraphCache(url)
    return body === undefined ? undefined : { body }
  },
  '/v1/provider-keys/{provider}': (url) => ({ body: fixtureProviderKeys(fixtureSegment(url, 3)) }),
  '/v1/judgment': () => ({ body: fixtureJudgment() }),
  '/v1/code-intelligence': () => ({ body: fixtureCodeIntelligence() }),
  '/v1/qa-settings': () => ({ body: fixtureQaSettings() }),
  '/v1/profile': () => ({ body: fixtureProfile() }),
  '/v1/workspace/roles': () => ({ body: fixtureWorkspaceRoles() }),
  '/v1/admin/people': () => (fixtureIsAdmin ? { body: fixturePeople() } : fixtureIsWorkspaceAdmin ? { body: fixtureScopedPeople() } : 'admin'),
  '/v1/admin/workspaces/roles': () => (fixtureIsAdmin ? { body: fixtureAdminWorkspacesRoles() } : 'admin'),
  // Admin › Tenants' "Being created": nothing is being built.
  '/v1/admin/workspaces': () => (fixtureIsAdmin ? { body: { workspaces: [] } } : 'admin'),
  '/v1/admin/runner-sizes': () => (fixtureIsAdmin ? { body: fixtureRunnerSizes() } : 'admin'),
  '/v1/admin/runner-sizes/versions': () => (fixtureIsAdmin ? { body: fixtureRunnerSizeVersions() } : 'admin'),
  // Setup and Access (QAFIX6-FIXTURE d): the checklist, the GitHub card and the grants table.
  '/v1/onboarding': () => ({ body: fixtureOnboarding() }),
  '/v1/forge/permissions': () => ({ body: fixtureForgePermissions() }),
  '/v1/access': () => ({ body: fixtureAccess() }),
  '/v1/access/orgs': () => ({ body: fixtureAccessOwners() }),
}

/** A read whose body is normalised before any screen sees it. */
async function readAs<T>(target: ApiRoute, norm: (raw: unknown) => T, isEmpty: (d: T) => boolean = () => false): Promise<Result<T>> {
  if (USE_FIXTURES) {
    const answer = FIXTURE_READS[target.template]?.(target.url)
    if (answer === undefined) return repoNotServed<T>(target)
    await new Promise((r) => setTimeout(r, 80))
    if (answer === 'admin') return fixtureAdminRefusal<T>(target, 80)
    noteFixtureProbe(target, 80, true)
    const data = norm(answer.body)
    return isEmpty(data) ? { status: 'empty', fetchedAt: Date.now() } : { status: 'ok', fetchedAt: Date.now(), data }
  }
  const r = await read<unknown>(target, () => false)
  if (r.status === 'ok') {
    const data = norm(r.data)
    return isEmpty(data) ? { status: 'empty', fetchedAt: r.fetchedAt, serverAt: r.serverAt } : { ...r, data }
  }
  if (r.status === 'stale') return { ...r, data: norm(r.data) }
  return r
}

/**
 * A fixture read's raw body, through the same table and probe as `readAs`,
 * for a reader that walks pages and filters rows itself (Dispatch.tsx's
 * `loadTaskReadableRepositories`). In the fixture build `fetch.read` asks the
 * dev server, which serves no API, so that reader drew a 404 over a route
 * this table answers.
 */
export function fixtureRead(target: ApiRoute): Promise<Result<unknown>> {
  return readAs(target, (raw) => raw)
}

async function writeTo(target: ApiRoute, method: 'POST' | 'PUT' | 'DELETE', body?: unknown): Promise<Result<unknown>> {
  if (USE_FIXTURES) return repoNotServed<unknown>(target, method)
  return write(target, method, body)
}

/** `GET /v1/repositories`: the tenant's registrations with freshness, schedule and coverage. */
export async function loadRepositories(): Promise<Result<RepoRecord[]>> {
  return readAs(route('/v1/repositories'), normRepoList, (d) => d.length === 0)
}

/**
 * The registration fields the hard stops read (docs/schedules.md §4.4, lane
 * S11): `platform`, set and cleared by a platform admin only, and
 * `hard_stop_paths`, whose `floor` (the four defaults, served by the API so
 * this file does not restate them) a member cannot remove.
 */
export interface RepoHardStops {
  platform: boolean
  hard_stop_paths: string[]
  floor: string[]
}

/**
 * The hard-stop fields of a registration, or null when the API did not serve
 * them (one older than S11): an absent list is not an empty one, and an
 * absent `platform` is not a confident "no".
 */
export function normRepoHardStops(raw: unknown): RepoHardStops | null {
  if (typeof raw !== 'object' || raw === null) return null
  const outer = raw as Record<string, unknown>
  const rec = (typeof outer.repository === 'object' && outer.repository !== null ? outer.repository : outer) as Record<string, unknown>
  if (typeof rec.platform !== 'boolean' || !Array.isArray(rec.hard_stop_paths)) return null
  const strings = (v: unknown): string[] => (Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : [])
  return { platform: rec.platform, hard_stop_paths: strings(rec.hard_stop_paths), floor: strings(rec.hard_stop_paths_floor) }
}

/** A registration's detail with its hard-stop fields, read from the one answer. */
export type RepoDetailRead = RepoDetail & { hard_stops: RepoHardStops | null; context: RepoContext }

/** `GET /v1/repositories/{repo_id}`: one registration, its last index runs, who used its index, and its hard stops. */
export async function loadRepository(repoId: string): Promise<Result<RepoDetailRead | null>> {
  return readAs(route('/v1/repositories/{repo_id}', { repo_id: repoId }), (raw) => {
    const d = normRepoDetail(raw)
    return d === null ? null : { ...d, hard_stops: normRepoHardStops(raw), context: normRepoContext(raw) }
  })
}

/** `GET /v1/repositories/{repo_id}/index?format=json`: the current index's structured document. */
export async function loadRepositoryIndex(repoId: string): Promise<Result<IndexDoc | null>> {
  return readAs(route('/v1/repositories/{repo_id}/index', { repo_id: repoId }, new URLSearchParams({ format: 'json' })), normIndexDoc)
}

/** `GET /v1/repositories/{repo_id}/graph`: the module dependency graph, aggregated for drawing (Graph A). */
export async function loadRepositoryGraph(repoId: string): Promise<Result<ModuleGraph | null>> {
  return readAs(route('/v1/repositories/{repo_id}/graph', { repo_id: repoId }), normModuleGraph)
}

// ---------------------------------------------------------------------------
// The Cache section (docs/design/graph-answer-cache.md §5.2-§5.5, lane
// CACHE-4-CONSOLE): `GET /v1/repositories/{repo_id}/graph-cache`, as
// swarm-api's `routes/graph_cache.py` answers it. Every figure the route
// leaves null stays null here: a replica that was not read is its reason,
// never a panel of zeros, and `carried` is null until carry forward exists.
// ---------------------------------------------------------------------------

/** Why swarm-graph, or one replica of it, was not read (graph_cache.py's reasons). */
export type GraphCacheReason = string

export interface GraphCacheWarm {
  digest: string
  ms: number | null
  shards: number | null
  replayed: number | null
}

export type GraphCacheReplica =
  | {
      replica: string
      state: 'read'
      started_at: string | null
      uptime_seconds: number | null
      result_bytes: number | null
      result_bound: number | null
      shard_bytes: number | null
      shard_bound: number | null
      answers_hot: number | null
      shards_hot: number | null
      last_warm: GraphCacheWarm | null
    }
  | { replica: string; state: 'not_read'; reason: GraphCacheReason }

/** §5.2's tiles, summed over the replicas read; null where nothing was read. */
export interface GraphCacheFacts {
  window_hours: number | null
  answers: number | null
  hit: number | null
  /** Null until carry forward exists (BGD Q4): never a 0 that says it ran. */
  carried: number | null
  miss: number | null
  hit_rate: number | null
  reused_share: number | null
  served_p95_ms: number | null
  last_warm: GraphCacheWarm | null
}

/**
 * The current version's `answer_latency` (§1.2): the uncached p95 and the
 * reference workload's time, or why it was not measured.
 */
export type GraphCacheLatency =
  | { state: 'measured'; p95_ms: number; reference_ms: number | null }
  | { state: 'unmeasured'; cause: string }

export interface GraphCacheShardRow {
  layer: string
  module: string
  blob: string
  raw_bytes: number | null
  /** Null when there is no previous version to compare with. */
  last_merge: 'changed' | 'unchanged' | null
  replicas: { replica: string; hot: boolean }[]
  hits_24h: number | null
  last_read: string | null
}

export type GraphCacheShards =
  | {
      state: 'read'
      comparison: string
      total: number
      changed: number | null
      page: number
      page_rows: number
      rows: GraphCacheShardRow[]
    }
  | { state: 'not_read'; reason: string }

export interface GraphCacheDoc {
  repo_id: string
  index: { commit_sha: string | null; manifest_digest: string | null; previous_sha: string | null; previous_digest: string | null } | null
  facts: GraphCacheFacts
  replicas: GraphCacheReplica[]
  /** Whether swarm-graph itself answered; `not_read` with `replicas: []` when it did not. */
  stats: { state: 'read' } | { state: 'not_read'; reason: GraphCacheReason }
  latency: GraphCacheLatency | null
  /** Present only on a `shards=1` read. */
  shards: GraphCacheShards | null
}

const gcRec = (v: unknown): Record<string, unknown> => (typeof v === 'object' && v !== null && !Array.isArray(v) ? (v as Record<string, unknown>) : {})
const gcNum = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) && v >= 0 ? v : null)
const gcStr = (v: unknown): string | null => (typeof v === 'string' && v !== '' ? v : null)

function normGraphCacheWarm(v: unknown): GraphCacheWarm | null {
  const w = gcRec(v)
  const digest = gcStr(w.digest)
  return digest === null ? null : { digest, ms: gcNum(w.ms), shards: gcNum(w.shards), replayed: gcNum(w.replayed) }
}

function normGraphCacheReplica(v: unknown): GraphCacheReplica | null {
  const r = gcRec(v)
  const replica = gcStr(r.replica)
  if (replica === null) return null
  // Anything but a replica the route says it read is one it did not read.
  if (r.state !== 'read') return { replica, state: 'not_read', reason: gcStr(r.reason) ?? 'bad_response' }
  return {
    replica,
    state: 'read',
    started_at: gcStr(r.started_at),
    uptime_seconds: gcNum(r.uptime_seconds),
    result_bytes: gcNum(r.result_bytes),
    result_bound: gcNum(r.result_bound),
    shard_bytes: gcNum(r.shard_bytes),
    shard_bound: gcNum(r.shard_bound),
    answers_hot: gcNum(r.answers_hot),
    shards_hot: gcNum(r.shards_hot),
    last_warm: normGraphCacheWarm(r.last_warm),
  }
}

/**
 * `answer_latency` as §1.2 records it: the median run's `p95_ms` and its
 * `reference_ms`, at the top or under `run`; `{state: "unmeasured", cause}`,
 * or the route's `not_recorded`, otherwise.
 */
function normGraphCacheLatency(v: unknown): GraphCacheLatency | null {
  if (typeof v !== 'object' || v === null) return null
  const l = gcRec(v)
  if (l.state === 'unmeasured') return { state: 'unmeasured', cause: gcStr(l.cause) ?? 'not_recorded' }
  const run = gcNum(l.p95_ms) !== null ? l : gcRec(l.run)
  const p95 = gcNum(run.p95_ms)
  if (p95 === null) return { state: 'unmeasured', cause: 'malformed' }
  return { state: 'measured', p95_ms: p95, reference_ms: gcNum(run.reference_ms) ?? gcNum(l.reference_ms) }
}

function normGraphCacheShards(v: unknown): GraphCacheShards | null {
  if (typeof v !== 'object' || v === null) return null
  const s = gcRec(v)
  if (s.state !== 'read') return { state: 'not_read', reason: gcStr(s.reason) ?? 'bad_response' }
  const rows = (Array.isArray(s.rows) ? s.rows : []).map((x): GraphCacheShardRow => {
    const r = gcRec(x)
    const merge = r.last_merge === 'changed' || r.last_merge === 'unchanged' ? r.last_merge : null
    return {
      layer: gcStr(r.layer) ?? '',
      module: gcStr(r.module) ?? '',
      blob: gcStr(r.blob) ?? '',
      raw_bytes: gcNum(r.raw_bytes),
      last_merge: merge,
      replicas: (Array.isArray(r.replicas) ? r.replicas : []).flatMap((m) => {
        const name = gcStr(gcRec(m).replica)
        return name === null ? [] : [{ replica: name, hot: gcRec(m).hot === true }]
      }),
      hits_24h: gcNum(r.hits_24h),
      last_read: gcStr(r.last_read),
    }
  })
  return {
    state: 'read',
    comparison: gcStr(s.comparison) ?? 'read',
    total: gcNum(s.total) ?? rows.length,
    changed: gcNum(s.changed),
    page: gcNum(s.page) ?? 1,
    page_rows: gcNum(s.page_rows) ?? 100,
    rows,
  }
}

/** The route's body, held to the fields §5.2 draws. */
export function normGraphCache(raw: unknown): GraphCacheDoc {
  const d = gcRec(raw)
  const f = gcRec(d.facts)
  const ix = typeof d.index === 'object' && d.index !== null ? gcRec(d.index) : null
  const stats = gcRec(d.stats)
  return {
    repo_id: gcStr(d.repo_id) ?? '',
    index:
      ix === null
        ? null
        : { commit_sha: gcStr(ix.commit_sha), manifest_digest: gcStr(ix.manifest_digest), previous_sha: gcStr(ix.previous_sha), previous_digest: gcStr(ix.previous_digest) },
    facts: {
      window_hours: gcNum(f.window_hours),
      answers: gcNum(f.answers),
      hit: gcNum(f.hit),
      carried: gcNum(f.carried),
      miss: gcNum(f.miss),
      hit_rate: gcNum(f.hit_rate),
      reused_share: gcNum(f.reused_share),
      served_p95_ms: gcNum(f.served_p95_ms),
      last_warm: normGraphCacheWarm(f.last_warm),
    },
    replicas: (Array.isArray(d.replicas) ? d.replicas : []).map(normGraphCacheReplica).filter((r): r is GraphCacheReplica => r !== null),
    stats: stats.state === 'read' ? { state: 'read' } : { state: 'not_read', reason: gcStr(stats.reason) ?? 'bad_response' },
    latency: normGraphCacheLatency(d.latency),
    shards: normGraphCacheShards(d.shards),
  }
}

/**
 * `GET /v1/repositories/{repo_id}/graph-cache[?shards=1&page=N]`: the Cache
 * section's one read (§5.3). `shardsPage` null asks for no shard list, so a
 * folded list costs nothing (§5.2).
 */
export async function loadRepositoryGraphCache(repoId: string, shardsPage: number | null = null): Promise<Result<GraphCacheDoc>> {
  const q = shardsPage === null ? undefined : new URLSearchParams({ shards: '1', page: String(shardsPage) })
  return readAs(route('/v1/repositories/{repo_id}/graph-cache', { repo_id: repoId }, q), normGraphCache)
}

/** `GET /v1/repositories/{repo_id}/symbols?q=`: symbols whose id contains `q`. */
export async function searchRepositorySymbols(repoId: string, q: string): Promise<Result<SymbolSearch | null>> {
  return readAs(route('/v1/repositories/{repo_id}/symbols', { repo_id: repoId }, new URLSearchParams({ q })), normSymbolSearch)
}

/** `GET /v1/repositories/{repo_id}/symbols?id=&depth=&direction=`: the call graph centred on one symbol. */
export async function loadSymbolGraph(
  repoId: string,
  id: string,
  depth: number,
  direction: 'both' | 'callers' | 'callees',
): Promise<Result<CallGraph | null>> {
  const q = new URLSearchParams({ id, depth: String(depth), direction })
  return readAs(route('/v1/repositories/{repo_id}/symbols', { repo_id: repoId }, q), normCallGraph)
}

/** `GET /v1/repositories/{repo_id}/symbols?id=&tests=1`: the tests that reach one symbol. */
export async function loadSymbolTests(repoId: string, id: string): Promise<Result<SymbolTests | null>> {
  return readAs(route('/v1/repositories/{repo_id}/symbols', { repo_id: repoId }, new URLSearchParams({ id, tests: '1' })), normSymbolTests)
}

/**
 * `POST /v1/repositories/{repo_id}/impact`: a pull request or a commit -> the
 * test plan (repo-index.md §4.3a). A query, not a change: the API reads the
 * diff from the forge and stores the plan keyed by its inputs, so asking twice
 * for one head is the same plan. The body names the change and nothing else
 * (the route refuses `paths`, `tests` or a command: invariant 10).
 */
export async function queryRepositoryImpact(repoId: string, change: { pull_request: number } | { commit: string }): Promise<Result<ImpactPlan | null>> {
  const r = await writeTo(route('/v1/repositories/{repo_id}/impact', { repo_id: repoId }), 'POST', change)
  if (r.status === 'ok') return { ...r, data: normImpact(r.data) }
  if (r.status === 'stale') return { ...r, data: normImpact(r.data) }
  return r
}

/**
 * `POST /v1/repositories/{repo_id}/tests:select`: changed paths -> the tests
 * that cover them, the always-run tests, the paths no edge covers and the
 * suite to run for those (repo-index.md §4.3). A query, not a change: the
 * route reads the promoted index and stores nothing. The body is the paths,
 * as data, and nothing else (the route's `extra="forbid"`: invariant 10).
 */
export async function selectRepositoryTests(repoId: string, paths: readonly string[]): Promise<Result<TestSelection | null>> {
  const r = await writeTo(route('/v1/repositories/{repo_id}/tests:select', { repo_id: repoId }), 'POST', { paths })
  if (r.status === 'ok') return { ...r, data: normTestSelection(r.data) }
  if (r.status === 'stale') return { ...r, data: normTestSelection(r.data) }
  return r
}

/**
 * `GET /v1/repositories/{repo_id}/languages`: per language, grammar, server
 * and status (routes/repositories.py `repository_languages`). An API that
 * does not serve it answers 404/501, and Settings then draws the index
 * document's own `languages` rows instead (QA G4-13).
 */
export async function loadRepositoryLanguages(repoId: string): Promise<Result<LanguageRow[]>> {
  return readAs(route('/v1/repositories/{repo_id}/languages', { repo_id: repoId }), normLanguages, (rows) => rows.length === 0)
}

/** The token that resolves here and its capability row, derived from `GET /v1/git-tokens` (R2); no per-repository token route exists. */
export async function loadResolvedToken(repoId: string): Promise<Result<ResolvedToken>> {
  return readAs(route('/v1/git-tokens'), (raw) => normResolvedFromTokens(raw, repoId))
}

/**
 * Pages read at most, whatever `next_page` says: the API caps the list at
 * `MAX_READABLE_PAGES` (10, repositories.py) and says `capped`, so this is
 * only a backstop against an answer that never stops offering a next page.
 */
const READABLE_PAGE_BACKSTOP = 50

/**
 * `GET /v1/repositories/readable`, EVERY page: what the tenant's git token can
 * read, for Register C (OB0, docs/onboarding.md §5). Page 1 is asked for with
 * no `page`, then `?page=N` for each `next_page` the API offers, until it
 * offers none. `capped` arrives on the last page and is kept, so the picker
 * can say the token reads more than is listed.
 *
 * A failure on page 1 is the read's failure. A failure on a later page is
 * not: the pages already read are served, with `gap` naming the page that did
 * not come back, so the picker says the list is short instead of passing it
 * off as everything.
 */
export async function loadReadableRepositories(): Promise<Result<ReadableList>> {
  const first = await readAs(route('/v1/repositories/readable'), normReadable)
  if (first.status !== 'ok') return first
  let list = first.data
  let asked = 1
  while (list.next_page !== null && list.next_page > asked && asked < READABLE_PAGE_BACKSTOP) {
    asked = list.next_page
    const more = await readAs(route('/v1/repositories/readable', {}, new URLSearchParams({ page: String(asked) })), normReadable)
    if (more.status === 'ok') {
      list = appendReadable(list, more.data)
      continue
    }
    const message = more.status === 'error' || more.status === 'stale' ? more.error.message : 'the page was not read'
    list = { ...list, next_page: null, gap: { page: asked, message } }
    break
  }
  if (list.repositories.length === 0 && list.gap === null) return { status: 'empty', fetchedAt: first.fetchedAt, serverAt: first.serverAt }
  return { ...first, data: list }
}

export interface RegisterRepositoryBody {
  repository: string
  default_branch?: string
  allowed_profiles?: string[]
  /** repo-index.md §3.3: 1-168 or `'off'`; `poll`, `webhook` (poll and push, graph-service.md §6.2) or `off`. */
  index?: { interval_hours: number | 'off'; on_change: 'poll' | 'webhook' | 'off' }
  /** #2029: sent only when the workspace's admin ticked it; absent is off. */
  workflow_files?: boolean
}

/** `POST /v1/repositories`: register; the API reads the forge once and is idempotent on `repo_id`. */
export async function registerRepository(body: RegisterRepositoryBody): Promise<Result<unknown>> {
  return writeTo(route('/v1/repositories'), 'POST', body)
}

/**
 * `DELETE /v1/repositories/{repo_id}`: unregister (member role). The API
 * disables every grant on the registration's secrets in the transaction that
 * removes it (`delete_repository`), so a re-registration revives none. The
 * person's own grant on the repository (`/v1/access`) is not touched.
 */
export async function deleteRepository(repoId: string): Promise<Result<unknown>> {
  const target = route('/v1/repositories/{repo_id}', { repo_id: repoId })
  if (USE_FIXTURES) {
    if (!FIXTURE_REPOS.some((r) => r.repo_id === repoId) || fixtureRemoved.has(repoId)) return repoNotServed<unknown>(target)
    await new Promise((r) => setTimeout(r, 180))
    noteFixtureProbe(target, 180, true)
    fixtureRemoved.add(repoId)
    return { status: 'ok', fetchedAt: Date.now(), data: null }
  }
  // The API answers 204 with no body; `write` reads that as the success it is.
  return write(target, 'DELETE')
}

/** One registration's counts: its repository secrets for agents and its QA targets. Never a name. */
export interface RepoCounts {
  repo_id: string
  secrets: number | null
  qa_targets: number | null
}

const countOf = (v: unknown): number | null => (typeof v === 'number' && Number.isInteger(v) && v >= 0 ? v : null)

/** The served counts by `repo_id`; a row without a `repo_id` is dropped, a count not a whole number is null. */
export function normRepoCounts(raw: unknown): Record<string, RepoCounts> {
  const rows = typeof raw === 'object' && raw !== null ? (raw as Record<string, unknown>).counts : null
  const out: Record<string, RepoCounts> = {}
  for (const row of Array.isArray(rows) ? rows : []) {
    if (typeof row !== 'object' || row === null) continue
    const r = row as Record<string, unknown>
    if (typeof r.repo_id !== 'string') continue
    out[r.repo_id] = { repo_id: r.repo_id, secrets: countOf(r.secrets), qa_targets: countOf(r.qa_targets) }
  }
  return out
}

/**
 * `GET /v1/repositories/counts`: every registration's secret and QA-target
 * counts in one read (REPOS-FOLLOWUPS), for Config › Repositories' cards.
 */
export async function loadRepositoryCounts(): Promise<Result<Record<string, RepoCounts>>> {
  return readAs(route('/v1/repositories/counts'), normRepoCounts)
}

/** `POST /v1/repositories/{repo_id}/index:run`: queue an index run now. */
export async function runRepositoryIndex(repoId: string, kind: 'full' | 'incremental'): Promise<Result<unknown>> {
  return writeTo(route('/v1/repositories/{repo_id}/index:run', { repo_id: repoId }), 'POST', { kind })
}

/** `GET /v1/git-tokens`: the tenant's token records, metadata only. */
export async function loadGitTokens(): Promise<Result<GitToken[]>> {
  return readAs(route('/v1/git-tokens'), normTokens, (d) => d.length === 0)
}

/** Per token × repository, the eight capabilities, built from the probe summaries `GET /v1/git-tokens` carries. */
export async function loadTokenPermissions(): Promise<Result<Permissions>> {
  return readAs(route('/v1/git-tokens'), normPermissionsFromTokens, (d) => d.rows.length === 0)
}

/** `POST /v1/git-tokens`: create a slot's RECORD. It takes no value; the value comes from create-secrets.sh. */
export async function registerTokenSlot(body: { scope: TokenScope; repo_id?: string }): Promise<Result<unknown>> {
  return writeTo(route('/v1/git-tokens'), 'POST', body)
}

/** `POST /v1/git-tokens/{token_id}/verify`: re-probe now. */
export async function verifyGitToken(tokenId: string): Promise<Result<unknown>> {
  return writeTo(route('/v1/git-tokens/{token_id}/verify', { token_id: tokenId }), 'POST')
}

// ---------------------------------------------------------------------------
// Provider keys a person or an admin sets: TypeSafe (KEYS-API, PR #1284;
// docs/provider-keys.md). Config › Secrets' TypeSafe key form (Secrets.tsx)
// is the only caller. THE KEY IS AN ARGUMENT OF `putProviderKey` AND NOTHING ELSE: it goes
// into that one request's body and is not kept, logged or returned here, and
// no response carries it back. The status is copied field by field
// (`normKeyStatus`), so anything a response carried by mistake is never drawn.
// ---------------------------------------------------------------------------

/** Whose key: the caller's own (`u-<you>`), or the selected tenant's (admins). */
export type ProviderKeyScope = 'user' | 'tenant'

/** One key's status: never a value, its length, a prefix or a suffix. */
export interface ProviderKeyStatus {
  /** False for `user` when the caller has no ready personal workspace to keep a key in. */
  available: boolean
  set: boolean
  set_at: string | null
  set_by: string | null
  version: string | null
}

/** `GET /v1/provider-keys/{provider}`. */
export interface ProviderKeys {
  provider: string
  user: ProviderKeyStatus
  tenant: ProviderKeyStatus & { tenant_id: string }
  /** Which key a task the caller submits in `tenant_id` would use. */
  resolution: { tenant_id: string; uses: ProviderKeyScope | 'none' }
  /** What reads these keys today. Empty: nothing does yet (docs/provider-keys.md). */
  consumed_by: string[]
}

function keyField(v: unknown): string | null {
  return typeof v === 'string' && v !== '' ? v : typeof v === 'number' ? String(v) : null
}

function normKeyStatus(raw: unknown): ProviderKeyStatus {
  const r = (typeof raw === 'object' && raw !== null ? raw : {}) as Record<string, unknown>
  return {
    available: r.available !== false,
    set: r.set === true,
    set_at: keyField(r.set_at),
    set_by: keyField(r.set_by),
    version: keyField(r.version),
  }
}

function normProviderKeys(raw: unknown): ProviderKeys {
  const r = (typeof raw === 'object' && raw !== null ? raw : {}) as Record<string, unknown>
  const tenant = (typeof r.tenant === 'object' && r.tenant !== null ? r.tenant : {}) as Record<string, unknown>
  const res = (typeof r.resolution === 'object' && r.resolution !== null ? r.resolution : {}) as Record<string, unknown>
  const uses = res.uses === 'user' || res.uses === 'tenant' ? res.uses : 'none'
  const tenantId = keyField(res.tenant_id) ?? keyField(tenant.tenant_id) ?? ''
  return {
    provider: keyField(r.provider) ?? '',
    user: normKeyStatus(r.user),
    tenant: { ...normKeyStatus(r.tenant), tenant_id: keyField(tenant.tenant_id) ?? tenantId },
    resolution: { tenant_id: tenantId, uses },
    consumed_by: Array.isArray(r.consumed_by) ? r.consumed_by.filter((c): c is string => typeof c === 'string') : [],
  }
}

/** `GET /v1/provider-keys/{provider}`: your key's status, your selected tenant's, and which a task here would use. */
export async function loadProviderKeys(provider: string): Promise<Result<ProviderKeys>> {
  return readAs(route('/v1/provider-keys/{provider}', { provider }), normProviderKeys)
}

/**
 * `PUT /v1/provider-keys/{provider}/user|tenant`: store a key. `linkToken` is
 * the `t` of a one-time link minted for this scope, sent back so the API can
 * consume it; it authorises nothing (the caller's session does).
 */
export async function putProviderKey(provider: string, scope: ProviderKeyScope, apiKey: string, linkToken: string | null): Promise<Result<unknown>> {
  const target = scope === 'user'
    ? route('/v1/provider-keys/{provider}/user', { provider })
    : route('/v1/provider-keys/{provider}/tenant', { provider })
  return writeTo(target, 'PUT', linkToken === null ? { api_key: apiKey } : { api_key: apiKey, link_token: linkToken })
}

/** `DELETE /v1/provider-keys/{provider}/user|tenant`: destroy every version of the key. */
export async function deleteProviderKey(provider: string, scope: ProviderKeyScope): Promise<Result<unknown>> {
  const target = scope === 'user'
    ? route('/v1/provider-keys/{provider}/user', { provider })
    : route('/v1/provider-keys/{provider}/tenant', { provider })
  return writeTo(target, 'DELETE')
}

// ---------------------------------------------------------------------------
// The workspace secrets store (SEC-STORE-API; docs/design/workspace-secrets.md
// §5). Settings › Workspace › Secrets (WorkspaceSecrets.tsx) is the only
// caller. A VALUE IS AN ARGUMENT OF `createWorkspaceSecret` AND NOTHING ELSE:
// it goes into that one request's body and is not kept, logged or returned
// here. A record is copied field by field from an allow-list
// (`normWorkspaceSecret`), so a value, a length or the last characters a
// response carried by mistake are never drawn -- the API's own `record_api`
// is an allow-list for the same reason.
// ---------------------------------------------------------------------------

/** One grant as the list serves it: a consumer name and whether it is on. */
export interface WorkspaceSecretGrant {
  consumer: string
  enabled: boolean
  /** The slot it fills; null when the list did not say. */
  slot: string | null
  /**
   * The hosts a host-scoped slot's secret may be sent to (`cf-access`,
   * swarm_api `slot_hosts`), as served: sorted, empty for every other slot.
   */
  hosts: string[]
}

/** One stored secret: never a value, its length, a prefix or a suffix. */
export interface WorkspaceSecret {
  name: string
  provider: string
  kind: string
  current_version: string | null
  /** The newest write: the rotation if there was one, else the creation. */
  set_at: string | null
  set_by: string | null
  grants: WorkspaceSecretGrant[]
  /**
   * When an attempt was last released it; null when none was. Undefined when
   * the API does not serve the field: SEC-STORE-API's list does not yet, and
   * the page says so rather than "never".
   */
  last_used_at: string | null | undefined
}

/** `GET /v1/workspace/secrets`, as the page reads it. */
export interface WorkspaceSecretList {
  tenant_id: string
  /** Only an explicit true: an unread flag offers no write. */
  can_manage: boolean
  secrets: WorkspaceSecret[]
}

/** One slot a consumer reads (§2.4): the kinds it takes. */
export interface WorkspaceSecretSlot {
  slot: string
  kinds: string[]
}

/**
 * One consumer of the catalogue (§2.3), as `Consumer.to_api` serves it.
 * `grantable` false with a slot is a LOCKED consumer (`component:git`,
 * §6.2); false with none is one that would be delivered nothing.
 */
export interface WorkspaceSecretConsumer {
  consumer: string
  kind: string
  name: string
  slots: WorkspaceSecretSlot[]
  grantable: boolean
  note: string
}

/** `GET /v1/workspace/secrets/consumers`: what the add form offers, and the grant matrix's columns. */
export interface WorkspaceSecretCatalogue {
  kinds: string[]
  providers: string[]
  consumers: WorkspaceSecretConsumer[]
}

/** What a create sends: `value` for a one-line kind, `fields` for a structured one. */
export interface WorkspaceSecretCreate {
  name: string
  provider: string
  kind: string
  value?: string
  fields?: Record<string, string>
}

function wsText(v: unknown): string | null {
  return typeof v === 'string' && v !== '' ? v : typeof v === 'number' ? String(v) : null
}

function wsStrings(v: unknown): string[] {
  return Array.isArray(v) ? v.filter((s): s is string => typeof s === 'string' && s !== '') : []
}

function normWorkspaceSecret(raw: unknown): WorkspaceSecret | null {
  if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) return null
  const r = raw as Record<string, unknown>
  const name = wsText(r.name)
  if (name === null) return null
  const grants = (Array.isArray(r.grants) ? r.grants : []).flatMap((g): WorkspaceSecretGrant[] => {
    if (typeof g !== 'object' || g === null) return []
    const r = g as Record<string, unknown>
    const consumer = wsText(r.consumer)
    return consumer === null ? [] : [{ consumer, enabled: r.enabled === true, slot: wsText(r.slot), hosts: wsStrings(r.hosts) }]
  })
  const rotated = wsText(r.rotated_at)
  return {
    name,
    provider: wsText(r.provider) ?? '',
    kind: wsText(r.kind) ?? '',
    current_version: wsText(r.current_version),
    set_at: rotated ?? wsText(r.created_at),
    set_by: rotated !== null ? wsText(r.rotated_by) : wsText(r.created_by),
    grants,
    last_used_at: 'last_used_at' in r ? wsText(r.last_used_at) : undefined,
  }
}

function normWorkspaceSecretList(raw: unknown): WorkspaceSecretList {
  const r = (typeof raw === 'object' && raw !== null ? raw : {}) as Record<string, unknown>
  return {
    tenant_id: wsText(r.tenant_id) ?? '',
    can_manage: r.can_manage === true,
    secrets: (Array.isArray(r.secrets) ? r.secrets : []).map(normWorkspaceSecret).filter((s): s is WorkspaceSecret => s !== null),
  }
}

function normWorkspaceSecretCatalogue(raw: unknown): WorkspaceSecretCatalogue {
  const r = (typeof raw === 'object' && raw !== null ? raw : {}) as Record<string, unknown>
  return { kinds: wsStrings(r.kinds), providers: wsStrings(r.providers), consumers: normWorkspaceSecretConsumers(r.consumers) }
}

/** The catalogue's consumers, copied from an allow-list; one without a name is dropped. */
function normWorkspaceSecretConsumers(raw: unknown): WorkspaceSecretConsumer[] {
  return (Array.isArray(raw) ? raw : []).flatMap((c): WorkspaceSecretConsumer[] => {
    if (typeof c !== 'object' || c === null) return []
    const r = c as Record<string, unknown>
    const consumer = wsText(r.consumer)
    const kind = wsText(r.kind)
    const name = wsText(r.name)
    if (consumer === null || kind === null || name === null) return []
    const slots = (Array.isArray(r.slots) ? r.slots : []).flatMap((s): WorkspaceSecretSlot[] => {
      if (typeof s !== 'object' || s === null) return []
      const slot = wsText((s as Record<string, unknown>).slot)
      return slot === null ? [] : [{ slot, kinds: wsStrings((s as Record<string, unknown>).kinds) }]
    })
    return [{ consumer, kind, name, slots, grantable: r.grantable === true, note: wsText(r.note) ?? '' }]
  })
}

/**
 * The fixture build's list: secrets on the personal workspace, which its
 * owner manages. One is granted to two consumers and not a third; the other
 * is a Cloudflare Access service token in claude-code-browser's `cf-access`
 * slot, its grant holding two hosts (CF-ACCESS-HOSTS-UI, part of #2442). No
 * value: the API serves none.
 */
async function fixtureWorkspaceSecrets(target: ApiRoute): Promise<Result<WorkspaceSecretList>> {
  await new Promise((r) => setTimeout(r, 80))
  noteFixtureProbe(target, 80, true)
  const at = new Date(Date.now() - 2 * 86_400_000).toISOString()
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: normWorkspaceSecretList({
      tenant_id: 'u-bogdan',
      can_manage: true,
      secrets: [{
        name: 'typesafe-key', provider: 'typesafe', kind: 'api_key', current_version: '1',
        created_at: at, created_by: 'alice@example.com', rotated_at: null, rotated_by: null,
        grants: [
          { consumer: 'tool:jev-browser', enabled: true },
          { consumer: 'component:judge', enabled: true },
          { consumer: 'profile:claude-code', enabled: false },
        ],
      }, {
        name: 'admin-access', provider: 'other', kind: 'oauth_client', current_version: '1',
        created_at: at, created_by: 'alice@example.com', rotated_at: null, rotated_by: null,
        grants: [{ consumer: 'profile:claude-code-browser', slot: 'cf-access', enabled: true, hosts: ['admin.example.com', 'api.admin.example.com'] }],
      }],
    }),
  }
}

/** The fixture build's answer to a secrets WRITE: not served, as an API without the routes answers. */
function fixtureSecretsNotServed(target: ApiRoute): Result<unknown> {
  noteFixtureProbe(target, 0, false)
  return {
    status: 'error',
    error: { kind: 'not_found', httpStatus: 404, code: null, message: 'The fixture build serves no workspace-secret writes.', template: target.template },
  }
}

/** `GET /v1/workspace/secrets`: the selected workspace's secrets and grants, and whether this caller manages them. */
export async function loadWorkspaceSecrets(): Promise<Result<WorkspaceSecretList>> {
  const target = route('/v1/workspace/secrets')
  if (USE_FIXTURES) return fixtureWorkspaceSecrets(target)
  const r = await read<unknown>(target, () => false)
  if (r.status === 'ok' || r.status === 'stale') return { ...r, data: normWorkspaceSecretList(r.data) }
  return r
}

const FIXTURE_SECRET_KINDS = ['api_key', 'bearer_token', 'basic_auth', 'oauth_client', 'opaque']
const FIXTURE_NO_SLOT = 'declares no secret slot yet; a grant would deliver nothing'

/**
 * The fixture build's catalogue: some of `_catalogue()`'s profiles, and its
 * tools and components, with the slots §2.4 gives them. claude-code-browser
 * also reads `cf-access`, the slot contract request 117 (accepted
 * 2026-10-26, not yet applied) gives it, so the grant matrix's hosts field
 * can be drawn for review.
 */
const FIXTURE_SECRET_CONSUMERS = [
  ...['browser', 'claude-code', 'claude-code-browser', 'codex'].map((name) => ({
    consumer: `profile:${name}`, kind: 'profile', name,
    slots: name === 'claude-code-browser'
      ? [{ slot: 'files', kinds: FIXTURE_SECRET_KINDS }, { slot: 'cf-access', kinds: ['oauth_client'] }]
      : [{ slot: 'files', kinds: FIXTURE_SECRET_KINDS }],
    grantable: true, note: '',
  })),
  { consumer: 'tool:jev-browser', kind: 'tool', name: 'jev-browser', slots: [{ slot: 'typesafe', kinds: ['api_key'] }], grantable: true, note: '' },
  { consumer: 'tool:dev-browser', kind: 'tool', name: 'dev-browser', slots: [], grantable: false, note: FIXTURE_NO_SLOT },
  { consumer: 'tool:playwright', kind: 'tool', name: 'playwright', slots: [], grantable: false, note: FIXTURE_NO_SLOT },
  { consumer: 'component:judge', kind: 'component', name: 'judge', slots: [{ slot: 'typesafe', kinds: ['api_key'] }], grantable: true, note: '' },
  { consumer: 'component:merge', kind: 'component', name: 'merge', slots: [], grantable: false, note: FIXTURE_NO_SLOT },
  { consumer: 'component:indexer', kind: 'component', name: 'indexer', slots: [], grantable: false, note: FIXTURE_NO_SLOT },
  { consumer: 'component:git', kind: 'component', name: 'git', slots: [{ slot: 'forge', kinds: ['forge_token'] }], grantable: false, note: 'forge tokens only, never granted from this page (design §6.2)' },
]

/** `GET /v1/workspace/secrets/consumers`: the kinds and providers a secret may be stored as, and the consumers it may be granted to. */
export async function loadWorkspaceSecretCatalogue(): Promise<Result<WorkspaceSecretCatalogue>> {
  const target = route('/v1/workspace/secrets/consumers')
  if (USE_FIXTURES) {
    noteFixtureProbe(target, 0, true)
    return {
      status: 'ok',
      fetchedAt: Date.now(),
      data: normWorkspaceSecretCatalogue({ kinds: FIXTURE_SECRET_KINDS, providers: ['anthropic', 'brave', 'openai', 'typesafe', 'other'], consumers: FIXTURE_SECRET_CONSUMERS }),
    }
  }
  const r = await read<unknown>(target, () => false)
  if (r.status === 'ok' || r.status === 'stale') return { ...r, data: normWorkspaceSecretCatalogue(r.data) }
  return r
}

/**
 * `POST /v1/workspace/secrets`: store a secret, its value once. `linkToken`
 * is the `t` of a one-time add link, sent back so the API can consume it; it
 * authorises nothing (the caller's session does). The answer is not handed
 * back: it is a record, and the page reads the list again.
 */
export async function createWorkspaceSecret(body: WorkspaceSecretCreate, linkToken: string | null): Promise<Result<null>> {
  const target = route('/v1/workspace/secrets')
  if (USE_FIXTURES) return fixtureSecretsNotServed(target) as Result<null>
  const r = await write(target, 'POST', linkToken === null ? body : { ...body, link_token: linkToken })
  return r.status === 'error' ? r : { status: 'ok', fetchedAt: Date.now(), data: null }
}

/** A secrets write's `secret`, from the allow-list; null when the answer carried none. */
function servedSecret(r: Result<unknown>): WorkspaceSecret | null {
  const data = r.status === 'ok' || r.status === 'stale' ? r.data : null
  return typeof data === 'object' && data !== null ? normWorkspaceSecret((data as Record<string, unknown>).secret) : null
}

/** What a rotate sends: `value` for a one-line kind, `fields` for a structured one. */
export type WorkspaceSecretRotate = { value: string } | { fields: Record<string, string> }

/**
 * `PUT /v1/workspace/secrets/{name}/value`: a new version, its value once.
 * `linkToken` is the `t` of a one-time rotate link for this name, sent back
 * so the API can consume it. The answer is the record from the allow-list
 * (never a value), or null when it carried none.
 */
export async function rotateWorkspaceSecret(name: string, body: WorkspaceSecretRotate, linkToken: string | null): Promise<Result<WorkspaceSecret | null>> {
  const target = route('/v1/workspace/secrets/{name}/value', { name })
  if (USE_FIXTURES) return fixtureSecretsNotServed(target) as Result<null>
  const r = await write(target, 'PUT', linkToken === null ? body : { ...body, link_token: linkToken })
  if (r.status === 'error') return r
  return { status: 'ok', fetchedAt: Date.now(), data: servedSecret(r) }
}

/**
 * A grant write's body: `{enabled}`, `slot` only for a consumer that declares
 * several, and `hosts` only for a host-scoped slot (`cf-access`). Never a
 * value: a grant names who may read a secret, not what it is.
 */
function grantBody(enabled: boolean, slot: string | null, hosts: readonly string[] | null): Record<string, unknown> {
  return { enabled, ...(slot === null ? {} : { slot }), ...(hosts === null ? {} : { hosts: [...hosts] }) }
}

/**
 * `PUT /v1/workspace/secrets/{name}/grants/{consumer}`: ONE cell of the
 * matrix, `{enabled}` and nothing else. No slot: every consumer in the
 * catalogue reads one, and the API defaults to it (§2.2); `slot` is sent
 * only for a consumer that declares several, and `hosts` only for a
 * host-scoped slot's cell. The answer is the record's grants after the
 * write, or null when it carried none.
 */
export async function setWorkspaceSecretGrant(name: string, consumer: string, enabled: boolean, slot: string | null = null, hosts: readonly string[] | null = null): Promise<Result<WorkspaceSecret | null>> {
  const target = route('/v1/workspace/secrets/{name}/grants/{consumer}', { name, consumer })
  if (USE_FIXTURES) return fixtureSecretsNotServed(target) as Result<null>
  const r = await write(target, 'PUT', grantBody(enabled, slot, hosts))
  if (r.status === 'error') return r
  return { status: 'ok', fetchedAt: Date.now(), data: servedSecret(r) }
}

/** `DELETE /v1/workspace/secrets/{name}`: destroy every version and disable every grant; the body repeats the name. */
export async function deleteWorkspaceSecret(name: string): Promise<Result<unknown>> {
  const target = route('/v1/workspace/secrets/{name}', { name })
  if (USE_FIXTURES) return fixtureSecretsNotServed(target)
  return write(target, 'DELETE', { name })
}

// ---------------------------------------------------------------------------
// Config › Secrets (lane SSC-UI, part of #2158; docs/design/secrets-scopes.md
// §4, §6.1, §6.2). Secrets.tsx and AdminPlatformSecrets.tsx are the callers.
// THE OVERVIEW IS COPIED FROM AN ALLOW-LIST, field by field, as every secrets
// read here is: `git_tokens` stores a `last4` the API never serves, and if a
// response ever carried it, or a value, a length or a version name, nothing
// below would copy it, so nothing could draw it.
// ---------------------------------------------------------------------------

/** The three scopes of §2.1, as `GET /v1/secrets/overview?scope=` spells them. */
export type SecretsScope = 'workspace' | 'personal' | 'repository'

/** One entry of a row's `shadowed_by` or `shadows` (Q1: the person's, then the repository's, then the workspace's). */
export interface SecretsShadow {
  scope: SecretsScope
  /** "agents of <person>", "agents that clone <owner/repo>", "agents of this workspace". */
  where: string
  slot: string | null
  person: string | null
  repository: string | null
}

/** One row of the overview. Which fields mean anything depends on `section`. */
export interface SecretsRow {
  section: string
  store: string
  name: string
  scope: string | null
  kind: string | null
  provider: string | null
  /** `set`, `unset`, a token's state, or a login's. */
  status: string
  set_at: string | null
  set_by: string | null
  consumers: string[]
  shadowed_by: SecretsShadow[]
  shadows: SecretsShadow[]
  can_manage: boolean
  /** Yours: whether this personal secret is opted in to the selected workspace; null where not asked. */
  opted_in: boolean | null
  /** An MCP server's consumer (`mcp:<name>`) and the store secrets granted to it. */
  consumer: string | null
  secrets: string[]
  /** A GitHub token's login, verify time, expiry and slot scope. Never any character of it. */
  forge_login: string | null
  verified_at: string | null
  expires_at: string | null
  token_scope: string | null
  /** A QA target's id, environment and which of its two values (`credential`, `challenge_bypass`). */
  target_id: string | null
  environment: string | null
  field: string | null
  site: string | null
  /** The console page a status-only row is managed on. */
  page: string | null
}

export interface SecretsOverview {
  tenant_id: string
  scope: SecretsScope
  repo_id: string | null
  repository: string | null
  personal_tenant_id: string | null
  /** Whether the caller manages this scope; null when the API did not say, which is not a yes. */
  can_manage: boolean | null
  rows: SecretsRow[]
}

const SECRETS_SCOPES: readonly SecretsScope[] = ['workspace', 'personal', 'repository']

function asScope(v: unknown): SecretsScope | null {
  return typeof v === 'string' && (SECRETS_SCOPES as readonly string[]).includes(v) ? (v as SecretsScope) : null
}

function normShadows(raw: unknown): SecretsShadow[] {
  return (Array.isArray(raw) ? raw : []).flatMap((s): SecretsShadow[] => {
    if (typeof s !== 'object' || s === null) return []
    const r = s as Record<string, unknown>
    const scope = asScope(r.scope)
    const where = wsText(r.where)
    return scope === null || where === null
      ? []
      : [{ scope, where, slot: wsText(r.slot), person: wsText(r.person), repository: wsText(r.repository) }]
  })
}

function normSecretsRow(raw: unknown): SecretsRow | null {
  if (typeof raw !== 'object' || raw === null) return null
  const r = raw as Record<string, unknown>
  const section = wsText(r.section)
  const name = wsText(r.name)
  if (section === null || name === null) return null
  return {
    section,
    store: wsText(r.store) ?? '',
    name,
    scope: wsText(r.scope),
    kind: wsText(r.kind),
    provider: wsText(r.provider),
    status: wsText(r.status) ?? 'unknown',
    set_at: wsText(r.set_at),
    set_by: wsText(r.set_by),
    consumers: wsStrings(r.consumers),
    shadowed_by: normShadows(r.shadowed_by),
    shadows: normShadows(r.shadows),
    can_manage: r.can_manage === true,
    opted_in: typeof r.opted_in === 'boolean' ? r.opted_in : null,
    consumer: wsText(r.consumer),
    secrets: wsStrings(r.secrets),
    forge_login: wsText(r.forge_login),
    verified_at: wsText(r.verified_at),
    expires_at: wsText(r.expires_at),
    // A git row's `scope` is the token's slot (tenant, user, repository), not a secrets scope.
    token_scope: section === 'github' ? wsText(r.scope) : null,
    target_id: wsText(r.target_id),
    environment: wsText(r.environment),
    field: wsText(r.field),
    site: wsText(r.site),
    page: wsText(r.page),
  }
}

export function normSecretsOverview(raw: unknown): SecretsOverview {
  const r = (typeof raw === 'object' && raw !== null ? raw : {}) as Record<string, unknown>
  return {
    tenant_id: wsText(r.tenant_id) ?? '',
    scope: asScope(r.scope) ?? 'workspace',
    repo_id: wsText(r.repo_id),
    repository: wsText(r.repository),
    personal_tenant_id: wsText(r.personal_tenant_id),
    can_manage: typeof r.can_manage === 'boolean' ? r.can_manage : null,
    rows: (Array.isArray(r.rows) ? r.rows : []).map(normSecretsRow).filter((x): x is SecretsRow => x !== null),
  }
}

/** `GET /v1/secrets/overview?scope=..[&repo_id=..]`: every store of one scope, status only. */
export async function loadSecretsOverview(scope: SecretsScope, repoId: string | null = null): Promise<Result<SecretsOverview>> {
  const q = new URLSearchParams({ scope })
  if (scope === 'repository' && repoId !== null) q.set('repo_id', repoId)
  return readAs(route('/v1/secrets/overview', {}, q), normSecretsOverview)
}

/**
 * WHERE A STORE SECRET IS WRITTEN: the selected workspace's own store, or one
 * of its repositories' (`/v1/repositories/{repo_id}/secrets`, the same
 * shapes). A form holds the target it was opened for and writes there and
 * nowhere else, so a form opened on one repository cannot submit to another
 * scope (SSC-UI's "cannot be submitted from the wrong scope").
 */
export type SecretStoreTarget = { scope: 'workspace' } | { scope: 'repository'; repoId: string }

/** A one-time link the page minted for itself: its token, held in memory only, and its expiry. */
export interface MintedLink {
  token: string
  expires_at: string | null
}

// ---------------------------------------------------------------------------
// Config › Secrets › Repository › GitHub Actions (lane SSC-UI-ACTIONS, part of
// #2158; docs/design/secrets-scopes.md §3, §6.1, and BGD's answers in §11).
// SecretsActions.tsx is the only caller; the routes are SSC-ACTIONS-API's
// (apps/swarm-api/swarm_api/routes/actions_secrets.py).
//
// A VALUE IS AN ARGUMENT OF `setActionsSecret` AND NOTHING ELSE. §11 Q5: the
// value is sealed in swarm-api with PyNaCl, so the page sends it as `value`
// in that one PUT, through the one-time form, and never seals it here (no
// libsodium in the UI). GitHub serves no value, and every list is copied
// from an allow-list, so nothing a response carried by mistake is drawn.
//
// NOTHING HERE CREATES AN ENVIRONMENT (§11 Q4). The one environment call is
// a GET of its branch rule; a missing environment is answered with GitHub's
// page to create it, which the page links to.
// ---------------------------------------------------------------------------

/** An environment's branch rule as `actions_secrets.RULE_*` spells it. */
export type ActionsRule = 'main_only' | 'protected_branches' | 'custom_branches' | 'any_branch'

const ACTIONS_RULES: readonly ActionsRule[] = ['main_only', 'protected_branches', 'custom_branches', 'any_branch']

/** One secret's name and times, as GitHub serves them, beside SwarmCloud's own record of its writes. */
export interface ActionsSecretName {
  name: string
  updated_at: string | null
  /** True when SwarmCloud's `actions_secret_writes` holds a write of it; false is "set on GitHub". */
  set_by_swarmcloud: boolean
  set_by: string | null
  set_at: string | null
}

/** `GET .../actions/environments/{environment}/rule`'s answer: whether it exists and lets only the default branch deploy. */
export interface ActionsEnvironmentRule {
  environment: string
  exists: boolean
  rule: ActionsRule | null
  main_only: boolean
  /** The registration's default branch, read from GitHub: the one branch "main only" means. */
  branch: string
  branches: { name: string; type: string }[]
  /** GitHub's page to create it, served only for a missing environment. */
  create_url: string | null
  /** GitHub's page listing the repository's environments. */
  settings_url: string | null
  /** The API's steps to create a missing one, in words. */
  steps: string | null
}

export interface ActionsEnvironment {
  environment: string
  rule: ActionsEnvironmentRule
  secrets: ActionsSecretName[]
}

/** `GET /v1/repositories/{repo_id}/actions/secrets`: the repository's secrets and each environment's. */
export interface ActionsSecretList {
  repo_id: string
  /** `owner/repo`, as the API names the registration; null when it did not. */
  repository: string | null
  /** The repository-level secrets (the API's `repository_secrets`). */
  secrets: ActionsSecretName[]
  environments: ActionsEnvironment[]
  /** The API lists at most 30 environments; true when it may have stopped there. */
  environments_truncated: boolean
  /** GitHub's create-environment page, served on the list even with no environment yet. */
  create_url: string | null
  /**
   * Whether this caller passes both checks a write runs (workspace manager,
   * and GitHub calls them admin of the repository, asked live). It decides
   * only what the pane offers: every write is authorised again by the API.
   * Null when the API did not say.
   */
  can_write: boolean | null
  /** The API's sentence for why not, when `can_write` is false. */
  cannot_write_because: string | null
  /** When GitHub was asked; null when the manager rule refused first. */
  checked_at: string | null
}

/**
 * A GitHub address the API served, or null. Only `https://github.com/` is
 * linked: these become an `href`, and a page that links wherever a response
 * points is one bad response away from sending a person somewhere else.
 */
export function githubUrl(v: unknown): string | null {
  if (typeof v !== 'string') return null
  try {
    const u = new URL(v)
    return u.protocol === 'https:' && u.hostname === 'github.com' ? u.toString() : null
  } catch {
    return null
  }
}

function normActionsName(raw: unknown): ActionsSecretName | null {
  if (typeof raw !== 'object' || raw === null) return null
  const r = raw as Record<string, unknown>
  if (typeof r.name !== 'string' || r.name === '') return null
  return {
    name: r.name,
    updated_at: wsText(r.updated_at) ?? wsText(r.created_at),
    set_by_swarmcloud: r.set_by_swarmcloud === true,
    set_by: wsText(r.set_by),
    set_at: wsText(r.set_at),
  }
}

function normActionsNames(raw: unknown): ActionsSecretName[] {
  return (Array.isArray(raw) ? raw : []).map(normActionsName).filter((s): s is ActionsSecretName => s !== null)
}

export function normActionsRule(raw: unknown, environment = ''): ActionsEnvironmentRule {
  const r = (typeof raw === 'object' && raw !== null ? raw : {}) as Record<string, unknown>
  const rule = ACTIONS_RULES.find((x) => x === r.rule) ?? null
  return {
    environment: wsText(r.environment) ?? environment,
    exists: r.exists === true,
    rule,
    // Main only is the API's word, never inferred: a missing flag is not main only.
    main_only: r.main_only === true && rule === 'main_only',
    branch: wsText(r.branch) ?? 'main',
    branches: (Array.isArray(r.branches) ? r.branches : []).flatMap((b) => {
      if (typeof b !== 'object' || b === null) return []
      const o = b as Record<string, unknown>
      return typeof o.name === 'string' ? [{ name: o.name, type: typeof o.type === 'string' ? o.type : '' }] : []
    }),
    create_url: githubUrl(r.create_url),
    settings_url: githubUrl(r.settings_url),
    steps: wsText(r.steps),
  }
}

/**
 * The list, from an allow-list. `repository` is the `owner/repo` name and
 * the repository-level rows are `repository_secrets` (SSC-ACTIONS-API-2: the
 * route's spread used to overwrite the name with the rows).
 */
export function normActionsSecretList(raw: unknown): ActionsSecretList {
  const r = (typeof raw === 'object' && raw !== null ? raw : {}) as Record<string, unknown>
  const environments = (Array.isArray(r.environments) ? r.environments : []).flatMap((e): ActionsEnvironment[] => {
    if (typeof e !== 'object' || e === null) return []
    const o = e as Record<string, unknown>
    if (typeof o.environment !== 'string' || o.environment === '') return []
    return [{ environment: o.environment, rule: normActionsRule(o.rule, o.environment), secrets: normActionsNames(o.secrets) }]
  })
  return {
    repo_id: wsText(r.repo_id) ?? '',
    repository: wsText(r.repository),
    secrets: normActionsNames(r.repository_secrets),
    environments,
    environments_truncated: r.environments_truncated === true,
    create_url: githubUrl(r.create_url),
    can_write: typeof r.can_write === 'boolean' ? r.can_write : null,
    cannot_write_because: wsText(r.cannot_write_because),
    checked_at: wsText(r.checked_at),
  }
}

/** The fixture build's repository, the mock-up's example data (config-e.html S4b2). */
const FIXTURE_ACTIONS_REPO = 'example-org/example-api'

/** A fixture repository id ending in this reads as one whose App lacks Secrets permission. */
export const FIXTURE_ACTIONS_UNGRANTED = '-ungranted'

function fixtureActionsRule(environment: string, rule: ActionsRule | null): ActionsEnvironmentRule {
  const base = `https://github.com/${FIXTURE_ACTIONS_REPO}/settings/environments`
  return normActionsRule({
    environment, exists: rule !== null, rule, main_only: rule === 'main_only', branch: 'main',
    branches: rule === 'main_only' ? [{ name: 'main', type: 'branch' }] : [],
    create_url: rule === null ? `${base}/new` : null, settings_url: base,
    steps: rule === null ? `Create the environment '${environment}' on GitHub, set its deployment branches to 'Selected branches and tags' with the one rule 'main', then check its rule here again` : null,
  }, environment)
}

function fixtureActionsRefusal<T>(target: ApiRoute): Result<T> {
  noteFixtureProbe(target, 0, false, 'conflict')
  return {
    status: 'error',
    error: {
      kind: 'conflict', httpStatus: 409, code: 'conflict', template: target.template,
      message: "the SwarmCloud GitHub App's installation has not granted the permission this needs (Secrets and Environments: Read and write); the installation's owner accepts it on GitHub, and nothing was written",
      detail: { reason: 'secrets_not_granted', repository: FIXTURE_ACTIONS_REPO, review_url: 'https://github.com/organizations/example-org/settings/installations/1/permissions/update' },
    },
  } as Result<T>
}

/** `GET /v1/repositories/{repo_id}/actions/secrets`: names and times, read live from GitHub. */
export async function loadActionsSecrets(repoId: string): Promise<Result<ActionsSecretList>> {
  const target = route('/v1/repositories/{repo_id}/actions/secrets', { repo_id: repoId })
  if (USE_FIXTURES) {
    await new Promise((r) => setTimeout(r, 80))
    if (repoId.endsWith(FIXTURE_ACTIONS_UNGRANTED)) return fixtureActionsRefusal(target)
    noteFixtureProbe(target, 80, true)
    const day = (n: number) => new Date(FIXTURE_NOW - n * 86_400_000).toISOString()
    return {
      status: 'ok',
      fetchedAt: Date.now(),
      data: normActionsSecretList({
        repo_id: repoId,
        repository: FIXTURE_ACTIONS_REPO,
        repository_secrets: [
          { name: 'TEST_DB_PASSWORD', updated_at: day(5), set_by_swarmcloud: true, set_by: 'operator@example.com', set_at: day(5) },
          { name: 'SENTRY_DSN', updated_at: day(40), set_by_swarmcloud: false, set_by: null, set_at: null },
        ],
        environments: [
          { environment: 'repo-secrets', rule: fixtureActionsRule('repo-secrets', 'main_only'),
            secrets: [{ name: 'MERGE_APP_PRIVATE_KEY', updated_at: day(2), set_by_swarmcloud: true, set_by: 'operator@example.com', set_at: day(2) }] },
          { environment: 'staging', rule: fixtureActionsRule('staging', 'any_branch'), secrets: [] },
        ],
        environments_truncated: false,
        create_url: `https://github.com/${FIXTURE_ACTIONS_REPO}/settings/environments/new`,
        // The mock-up's sentence: a writer, GitHub asked a minute before the capture clock.
        can_write: true,
        cannot_write_because: null,
        checked_at: new Date(FIXTURE_NOW - 60_000).toISOString(),
      }),
    }
  }
  const r = await read<unknown>(target, () => false)
  if (r.status === 'ok' || r.status === 'stale') return { ...r, data: normActionsSecretList(r.data) }
  return r
}

/** `GET .../actions/environments/{environment}/rule`: §11 Q4's Check rule. It writes nothing. */
export async function checkActionsRule(repoId: string, environment: string): Promise<Result<ActionsEnvironmentRule>> {
  const target = route('/v1/repositories/{repo_id}/actions/environments/{environment}/rule', { repo_id: repoId, environment })
  if (USE_FIXTURES) {
    noteFixtureProbe(target, 0, true)
    const known: Readonly<Record<string, ActionsRule>> = Object.fromEntries([['repo-secrets', 'main_only'], ['staging', 'any_branch']])
    return { status: 'ok', fetchedAt: Date.now(), data: fixtureActionsRule(environment, known[environment] ?? null) }
  }
  const r = await read<unknown>(target, () => false)
  if (r.status === 'ok' || r.status === 'stale') return { ...r, data: normActionsRule(r.data, environment) }
  return r
}

/** A one-time link to the Set form, as the page holds it: never its URL, which carries the token. */
export interface ActionsLink {
  token: string
  expires_at: string | null
}

export interface SecretStoreApi {
  target: SecretStoreTarget
  list(): Promise<Result<WorkspaceSecretList>>
  create(body: WorkspaceSecretCreate, linkToken: string | null): Promise<Result<null>>
  rotate(name: string, body: WorkspaceSecretRotate, linkToken: string | null): Promise<Result<WorkspaceSecret | null>>
  remove(name: string): Promise<Result<unknown>>
  grant(name: string, consumer: string, enabled: boolean, slot?: string | null, hosts?: readonly string[] | null): Promise<Result<WorkspaceSecret | null>>
  mintLink(action: 'add' | 'rotate', name: string | null): Promise<Result<MintedLink>>
}

/** A link answer's token, read off its `path` (`...&t=<token>`); never the path or the URL kept. */
function mintedFrom(r: Result<unknown>): Result<MintedLink> {
  if (r.status === 'error') return r
  const data = (r.status === 'ok' || r.status === 'stale') && typeof r.data === 'object' && r.data !== null ? (r.data as Record<string, unknown>) : {}
  const path = wsText(data.path) ?? wsText(data.url) ?? ''
  const q = path.indexOf('?')
  const token = q === -1 ? null : new URLSearchParams(path.slice(q + 1)).get('t')
  if (token === null || token === '') {
    return { status: 'error', error: { kind: 'server_error', httpStatus: null, code: null, message: 'The link answer carried no token.', method: 'POST' } }
  }
  return { status: 'ok', fetchedAt: Date.now(), data: { token, expires_at: wsText(data.expires_at) } }
}

/**
 * The fixture build's link: a token built at runtime, so the one-time form
 * can be drawn for review. Its writes are still refused (`fixtureSecretsNotServed`).
 */
function fixtureMinted(target: ApiRoute): Result<MintedLink> {
  noteFixtureProbe(target, 0, true)
  const token = Array.from({ length: 4 }, () => Math.random().toString(36).slice(2)).join('')
  return { status: 'ok', fetchedAt: Date.now(), data: { token, expires_at: new Date(Date.now() + 600_000).toISOString() } }
}

async function mint(target: ApiRoute, body: unknown): Promise<Result<MintedLink>> {
  if (USE_FIXTURES) return fixtureMinted(target)
  return mintedFrom(await write(target, 'POST', body))
}

function linkBody(action: 'add' | 'rotate', name: string | null): Record<string, string> {
  return action === 'rotate' && name !== null ? { action, name } : { action }
}

async function fixtureRepositorySecrets(target: ApiRoute, repoId: string): Promise<Result<WorkspaceSecretList>> {
  await new Promise((r) => setTimeout(r, 80))
  noteFixtureProbe(target, 80, true)
  const at = (days: number) => new Date(Date.parse(FIXTURE_CLOCK) - days * 86_400_000).toISOString()
  const secret = (name: string, kind: string, days: number, granted: string[]) => ({
    name, provider: 'other', kind, scope: 'repository', repo_id: repoId, current_version: '1',
    created_at: at(days), created_by: 'alice@example.com', rotated_at: null, rotated_by: null,
    grants: granted.map((consumer) => ({ consumer, enabled: true })),
  })
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: normWorkspaceSecretList({
      tenant_id: FIXTURE_TENANT,
      can_manage: true,
      secrets: [secret('test-db-password', 'opaque', 5, ['profile:claude-code']), secret('search-api', 'api_key', 1, ['profile:claude-code'])],
    }),
  }
}

const WORKSPACE_STORE: SecretStoreApi = {
  target: { scope: 'workspace' },
  list: loadWorkspaceSecrets,
  create: createWorkspaceSecret,
  rotate: rotateWorkspaceSecret,
  remove: deleteWorkspaceSecret,
  grant: (name, consumer, enabled, slot = null, hosts = null) => setWorkspaceSecretGrant(name, consumer, enabled, slot, hosts),
  mintLink: (action, name) => mint(route('/v1/workspace/secrets/links'), linkBody(action, name)),
}

function repositoryStore(repoId: string): SecretStoreApi {
  const repo_id = repoId
  return {
    target: { scope: 'repository', repoId },
    async list() {
      const target = route('/v1/repositories/{repo_id}/secrets', { repo_id })
      if (USE_FIXTURES) return fixtureRepositorySecrets(target, repoId)
      const r = await read<unknown>(target, () => false)
      if (r.status === 'ok' || r.status === 'stale') return { ...r, data: normWorkspaceSecretList(r.data) }
      return r
    },
    async create(body, linkToken) {
      const target = route('/v1/repositories/{repo_id}/secrets', { repo_id })
      if (USE_FIXTURES) return fixtureSecretsNotServed(target) as Result<null>
      const r = await write(target, 'POST', linkToken === null ? body : { ...body, link_token: linkToken })
      return r.status === 'error' ? r : { status: 'ok', fetchedAt: Date.now(), data: null }
    },
    async rotate(name, body, linkToken) {
      const target = route('/v1/repositories/{repo_id}/secrets/{name}/value', { repo_id, name })
      if (USE_FIXTURES) return fixtureSecretsNotServed(target) as Result<null>
      const r = await write(target, 'PUT', linkToken === null ? body : { ...body, link_token: linkToken })
      return r.status === 'error' ? r : { status: 'ok', fetchedAt: Date.now(), data: servedSecret(r) }
    },
    async remove(name) {
      const target = route('/v1/repositories/{repo_id}/secrets/{name}', { repo_id, name })
      if (USE_FIXTURES) return fixtureSecretsNotServed(target)
      return write(target, 'DELETE', { name })
    },
    async grant(name, consumer, enabled, slot = null, hosts = null) {
      const target = route('/v1/repositories/{repo_id}/secrets/{name}/grants/{consumer}', { repo_id, name, consumer })
      if (USE_FIXTURES) return fixtureSecretsNotServed(target) as Result<null>
      const r = await write(target, 'PUT', grantBody(enabled, slot, hosts))
      return r.status === 'error' ? r : { status: 'ok', fetchedAt: Date.now(), data: servedSecret(r) }
    },
    mintLink: (action, name) => mint(route('/v1/repositories/{repo_id}/secrets/links', { repo_id }), linkBody(action, name)),
  }
}

/** The store a target names: the workspace's own, or one repository's. */
export function secretStore(target: SecretStoreTarget): SecretStoreApi {
  return target.scope === 'workspace' ? WORKSPACE_STORE : repositoryStore(target.repoId)
}

/** `POST /v1/provider-keys/{provider}/links`: a one-time link to the key form, for `user` (yours) or `tenant` (the workspace's). */
export function mintProviderKeyLink(provider: string, scope: ProviderKeyScope): Promise<Result<MintedLink>> {
  return mint(route('/v1/provider-keys/{provider}/links', { provider }), { scope })
}

/** `POST /v1/tenants/me/credentials/links` (SSC-LINKS): a one-time link to the runner-key form for one provider. */
export function mintRunnerKeyLink(provider: string): Promise<Result<MintedLink>> {
  return mint(route('/v1/tenants/me/credentials/links'), { provider })
}

/**
 * `POST /v1/tenants/me/credentials`: the workspace's runner LLM key, its
 * value in this one body and nowhere else; `linkToken` is the one-time link's.
 */
export async function putRunnerKey(provider: string, apiKey: string, linkToken: string | null): Promise<Result<unknown>> {
  const target = route('/v1/tenants/me/credentials')
  if (USE_FIXTURES) return fixtureSecretsNotServed(target)
  return write(target, 'POST', linkToken === null ? { provider, api_key: apiKey } : { provider, api_key: apiKey, link_token: linkToken })
}

/** `DELETE /v1/browser-logins/{name}`: remove a saved browser session of the selected (personal) workspace. */
export async function deleteBrowserLogin(name: string): Promise<Result<unknown>> {
  const target = route('/v1/browser-logins/{name}', { name })
  if (USE_FIXTURES) return fixtureSecretsNotServed(target)
  return write(target, 'DELETE')
}

// ---------------------------------------------------------------------------
// Admin › Platform secrets (SSC-UI over SSC-PLATFORM-STATUS; §5). STATUS
// ONLY: the route reads Secret Manager metadata and never a payload, and
// this copies set, version count, newest time and who writes it, nothing else.
// ---------------------------------------------------------------------------

export interface PlatformSecretRow {
  secret_id: string
  managed_by: string
  written_by: string
  /** `set`, `not_set`, `not_declared` or `unreadable`. */
  status: string
  /** Why an `unreadable` row is: `permission_denied` or `read_failed`. */
  reason: string | null
  enabled_versions: number | null
  newest_enabled_at: string | null
  rotation_in_grace: boolean
  /** `platform` (the catalogue) or `account` (the quota broker's, two halves each). */
  family: string
  owner_tenant: string | null
  label: string | null
  half: string | null
}

export interface PlatformSecrets {
  secrets: PlatformSecretRow[]
  accounts_unread: boolean
  accounts_refused: number
  accounts_truncated: boolean
  read_at: string | null
}

export function normPlatformSecrets(raw: unknown): PlatformSecrets {
  const r = (typeof raw === 'object' && raw !== null ? raw : {}) as Record<string, unknown>
  const rows = (Array.isArray(r.secrets) ? r.secrets : []).flatMap((x): PlatformSecretRow[] => {
    if (typeof x !== 'object' || x === null) return []
    const s = x as Record<string, unknown>
    const id = wsText(s.secret_id)
    if (id === null) return []
    return [{
      secret_id: id,
      managed_by: wsText(s.managed_by) ?? '',
      written_by: wsText(s.written_by) ?? '',
      status: wsText(s.status) ?? 'unreadable',
      reason: wsText(s.reason),
      enabled_versions: typeof s.enabled_versions === 'number' ? s.enabled_versions : null,
      newest_enabled_at: wsText(s.newest_enabled_at),
      rotation_in_grace: s.rotation_in_grace === true,
      family: wsText(s.family) ?? 'platform',
      owner_tenant: wsText(s.owner_tenant),
      label: wsText(s.label),
      half: wsText(s.half),
    }]
  })
  return {
    secrets: rows,
    accounts_unread: r.accounts_unread === true,
    accounts_refused: typeof r.accounts_refused === 'number' ? r.accounts_refused : 0,
    accounts_truncated: r.accounts_truncated === true,
    read_at: wsText(r.read_at),
  }
}

/** `GET /v1/admin/platform-secrets`: a platform admin's; anyone else is the route's 403. */
export async function loadPlatformSecrets(): Promise<Result<PlatformSecrets>> {
  return readAs(route('/v1/admin/platform-secrets'), normPlatformSecrets, (d) => d.secrets.length === 0)
}

/**
 * The fixture build's overview, one scope at a time: the shapes
 * `swarm_api.secrets_overview` serves, with a shadowed name, a needed key
 * and a QA sign-in not set, so every state of the page can be drawn. No value.
 */
function fixtureSecretsOverview(url: string): unknown {
  const q = new URLSearchParams(url.slice(url.indexOf('?') + 1))
  const scope = q.get('scope') ?? 'workspace'
  const at = (days: number) => new Date(Date.parse(FIXTURE_CLOCK) - days * 86_400_000).toISOString()
  const base = { section: 'agents', store: 'workspace_secrets', consumers: ['profile:claude-code'], shadowed_by: [], shadows: [], can_manage: true }
  const github = (scopeOf: string) => ({
    section: 'github', store: 'git_tokens', name: `swarm-tenant-${FIXTURE_TENANT}-git`, scope: scopeOf, kind: 'app_user',
    status: 'active', forge_login: 'alice-example', verified_at: at(0.002), expires_at: new Date(Date.parse(FIXTURE_CLOCK) + 7 * 3_600_000).toISOString(),
    set_at: at(3), set_by: 'alice@example.com', consumers: [], can_manage: false, page: '/repositories/tokens',
  })
  if (scope === 'repository') {
    const repoId = q.get('repo_id') ?? FIXTURE_REPOS[0].repo_id
    const repo = FIXTURE_REPOS.find((r) => r.repo_id === repoId) ?? FIXTURE_REPOS[0]
    const repository = `example-org/${repo.repo}`
    return {
      tenant_id: FIXTURE_TENANT, scope, repo_id: repoId, repository, personal_tenant_id: null, can_manage: true,
      rows: [
        { ...base, name: 'search-api', scope: 'repository', kind: 'api_key', status: 'set', set_at: at(1), set_by: 'alice@example.com',
          shadows: [{ scope: 'workspace', where: 'agents of this workspace', slot: 'files' }],
          shadowed_by: [{ scope: 'personal', where: 'agents of alice@example.com', person: 'alice@example.com', slot: 'files' }] },
        { ...base, name: 'test-db-password', scope: 'repository', kind: 'opaque', status: 'set', set_at: at(5), set_by: 'alice@example.com' },
        { section: 'qa_sign_in', store: 'qa_targets', name: 'Studio', target_id: 'qat_fixture1', environment: 'staging', field: 'credential', status: 'set', set_at: at(2), set_by: 'alice@example.com', consumers: [], can_manage: true },
        { section: 'qa_sign_in', store: 'qa_targets', name: 'Studio', target_id: 'qat_fixture1', environment: 'production', field: 'credential', status: 'unset', set_at: null, set_by: null, consumers: [], can_manage: true },
        { section: 'actions', store: 'actions_secret_writes', name: 'MERGE_APP_PRIVATE_KEY', environment: 'repo-secrets', status: 'set', set_at: at(2), set_by: 'alice@example.com', consumers: [], can_manage: true },
      ],
    }
  }
  if (scope === 'personal') {
    return {
      tenant_id: FIXTURE_TENANT, scope, repo_id: null, repository: null, personal_tenant_id: FIXTURE_TENANT, can_manage: true,
      rows: [
        { ...base, name: 'search-api', scope: 'personal', kind: 'api_key', status: 'set', set_at: at(2), set_by: 'alice@example.com', opted_in: null,
          shadows: [{ scope: 'workspace', where: 'agents of this workspace', slot: 'files' }] },
        { section: 'keys', store: 'provider_keys', name: 'typesafe', status: 'set', set_at: at(6), set_by: 'alice@example.com', consumers: [], can_manage: true },
        { section: 'browser_sessions', store: 'browser_logins', name: 'studio-staging', site: 'studio.example.com', status: 'active', set_at: at(2), set_by: 'alice@example.com', expires_at: new Date(Date.parse(FIXTURE_CLOCK) + 5 * 86_400_000).toISOString(), consumers: [], can_manage: true },
        github('user'),
      ],
    }
  }
  return {
    tenant_id: FIXTURE_TENANT, scope: 'workspace', repo_id: null, repository: null, personal_tenant_id: null, can_manage: true,
    rows: [
      { ...base, name: 'typesafe-key', scope: 'workspace', kind: 'api_key', status: 'set', set_at: at(2), set_by: 'alice@example.com',
        shadowed_by: [{ scope: 'personal', where: 'agents of alice@example.com', person: 'alice@example.com', slot: 'typesafe' }] },
      { section: 'keys', store: 'provider_keys', name: 'typesafe', status: 'set', set_at: at(5), set_by: 'alice@example.com', consumers: [], can_manage: true },
      { section: 'keys', store: 'runner_provider_keys', name: 'anthropic', status: 'set', set_at: null, set_by: null, consumers: ['profile:claude-code'], can_manage: true },
      { section: 'keys', store: 'mcp_servers', name: 'kb', consumer: 'mcp:kb', secrets: [], status: 'unset', set_at: null, set_by: null, consumers: ['mcp:kb'], can_manage: true },
      { section: 'accounts', store: 'pool_accounts', name: 'subscription accounts', status: 'elsewhere', consumers: [], can_manage: false, page: '/capacity/accounts' },
      github('tenant'),
    ],
  }
}

/** The fixture build's Admin › Platform secrets: one not declared, one in a rotation's grace, two account halves. */
function fixturePlatformSecrets(): unknown {
  const at = (days: number) => new Date(Date.parse(FIXTURE_CLOCK) - days * 86_400_000).toISOString()
  const tf = 'terraform'
  const row = (secret_id: string, status: string, versions: number | null, days: number | null, managed_by: string, written_by: string) => ({
    secret_id, status, reason: null, enabled_versions: versions, newest_enabled_at: days === null ? null : at(days),
    rotation_in_grace: (versions ?? 0) > 1, managed_by, written_by, family: 'platform',
  })
  return {
    secrets: [
      row('swarm-github-app-private-key', 'set', 1, 41, tf, 'scripts/create-secrets.sh --github-app private-key'),
      row('swarm-github-app-webhook-secret', 'set', 1, 41, tf, 'scripts/create-secrets.sh --github-app-webhook-secret'),
      row('swarm-search-api-key', 'set', 2, 0.25, tf, 'scripts/create-secrets.sh --search-api-key'),
      row('swarm-live-key', 'not_declared', null, null, tf, 'scripts/create-secrets.sh --live-key'),
      row('swarm-workspace-grant-key', 'set', 1, 23, tf, 'scripts/create-secrets.sh --workspace-grant-key'),
      ...['access', 'refresh'].map((half) => ({
        ...row(`swarm-account-${FIXTURE_TENANT}--max-1${half === 'refresh' ? '-refresh' : ''}`, 'set', 1, 2, 'quota-broker', 'the quota broker (Capacity › Accounts)'),
        family: 'account', owner_tenant: FIXTURE_TENANT, label: 'max-1', half,
      })),
    ],
    counts: {}, accounts_unread: false, accounts_refused: 0, accounts_truncated: false, read_at: FIXTURE_CLOCK,
  }
}

/**
 * `POST /v1/repositories/{repo_id}/actions/links`: the page mints its own
 * one-time link on Open the one-time form (§6.1, "a link the page mints for
 * itself on click"). The answer's `url` and `path` carry the token; only the
 * token is kept, to go back once as the write's `link_token`, and neither is
 * drawn or put in the address.
 */
export async function mintActionsLink(repoId: string, name: string, environment: string | null): Promise<Result<ActionsLink>> {
  const target = route('/v1/repositories/{repo_id}/actions/links', { repo_id: repoId })
  if (USE_FIXTURES) {
    noteFixtureProbe(target, 0, true)
    const bytes = crypto.getRandomValues(new Uint8Array(24))
    const token = Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')
    return { status: 'ok', fetchedAt: Date.now(), data: { token, expires_at: new Date(Date.now() + 600_000).toISOString() } }
  }
  const r = await write(target, 'POST', { name, environment })
  if (r.status === 'error') return r
  const raw = r.status === 'ok' || r.status === 'stale' ? r.data : null
  const d = (typeof raw === 'object' && raw !== null ? raw : {}) as Record<string, unknown>
  const path = typeof d.path === 'string' ? d.path : typeof d.url === 'string' ? d.url : ''
  const at = path.indexOf('?')
  const token = at === -1 ? null : new URLSearchParams(path.slice(at + 1)).get('t')
  if (token === null || token === '') {
    return { status: 'error', error: { kind: 'server_error', httpStatus: null, code: null, message: 'The API answered without a one-time link; nothing was written.', method: 'POST' } }
  }
  return { status: 'ok', fetchedAt: Date.now(), data: { token, expires_at: wsText(d.expires_at) } }
}

/** What a set sends, besides the value: where, the typed environment name when its rule asks for it, and the link. */
export interface ActionsSecretTarget {
  environment: string | null
  /** §11 Q4: the environment's name, typed, for one whose rule is not main only. */
  confirm_environment: string | null
  link_token: string | null
}

/**
 * `PUT /v1/repositories/{repo_id}/actions/secrets/{name}`: the value, once,
 * in this one body. swarm-api seals it and drops it (§11 Q5). The answer is
 * whether GitHub created it (201) or updated it (204); no value comes back.
 */
export async function setActionsSecret(repoId: string, name: string, value: string, at: ActionsSecretTarget): Promise<Result<{ created: boolean }>> {
  const target = route('/v1/repositories/{repo_id}/actions/secrets/{name}', { repo_id: repoId, name })
  if (USE_FIXTURES) {
    noteFixtureProbe(target, 0, true)
    return { status: 'ok', fetchedAt: Date.now(), data: { created: true } }
  }
  const body: Record<string, string> = { value }
  if (at.environment !== null) body.environment = at.environment
  if (at.confirm_environment !== null) body.confirm_environment = at.confirm_environment
  if (at.link_token !== null) body.link_token = at.link_token
  const r = await write(target, 'PUT', body)
  if (r.status === 'error') return r
  const raw = r.status === 'ok' || r.status === 'stale' ? r.data : null
  const d = (typeof raw === 'object' && raw !== null ? raw : {}) as Record<string, unknown>
  return { status: 'ok', fetchedAt: Date.now(), data: { created: d.created === true } }
}

/** `DELETE /v1/repositories/{repo_id}/actions/secrets/{name}`: the body repeats the name, typed. */
export async function deleteActionsSecret(repoId: string, name: string, confirmName: string, environment: string | null): Promise<Result<unknown>> {
  const target = route('/v1/repositories/{repo_id}/actions/secrets/{name}', { repo_id: repoId, name })
  if (USE_FIXTURES) {
    noteFixtureProbe(target, 0, true)
    return { status: 'ok', fetchedAt: Date.now(), data: null }
  }
  return write(target, 'DELETE', environment === null ? { confirm_name: confirmName } : { confirm_name: confirmName, environment })
}

// ---------------------------------------------------------------------------
// Settings › Profile (docs/design/user-profile.md §4.2; PROFILE-API, PR #1544).
// Profile.tsx is the only caller. Every field is copied BY NAME from an
// allow-list, as the API builds it, so a field a response carries by mistake
// -- a secret's name, a token id, a raw document -- is never drawn. A section
// the response leaves out stays out (it is named in `unavailable`), and is
// never filled in with an empty default that would read as "you have none".
// ---------------------------------------------------------------------------

/** `preferences.TOPICS`, in the API's order. */
export const NOTIFICATION_TOPICS: readonly NotificationTopic[] = ['approval', 'waiting_step', 'question', 'failed', 'finished', 'account', 'admin']

/** `preferences.REMINDERS`, in the API's order. */
export const REMINDER_CADENCES: readonly ReminderCadence[] = ['as_set', 'every_4h', 'daily', 'never']

const PROFILE_SECTIONS: readonly ProfileSection[] = ['preferences', 'github', 'typesafe', 'claude_accounts']

function pfObj(v: unknown): Record<string, unknown> | null {
  return typeof v === 'object' && v !== null && !Array.isArray(v) ? (v as Record<string, unknown>) : null
}

function pfStr(v: unknown): string | null {
  return typeof v === 'string' && v !== '' ? v : null
}

function pfStrOrNum(v: unknown): string | null {
  return typeof v === 'number' && Number.isFinite(v) ? String(v) : pfStr(v)
}

function pfNum(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

function normRole(raw: unknown): WorkspaceRole {
  const r = pfObj(raw) ?? {}
  return { name: pfStr(r.name) ?? '', source: pfStr(r.source) ?? '', via: pfStr(r.via) }
}

function normIdentity(raw: unknown): ProfileIdentity {
  const r = pfObj(raw) ?? {}
  return {
    email: pfStr(r.email) ?? '',
    display_name: pfStr(r.display_name),
    display_name_suggested: pfStr(r.display_name_suggested),
    domain: pfStr(r.domain),
    platform_role: pfStr(r.platform_role),
    is_admin: r.is_admin === true,
    is_admin_unresolved: r.is_admin_unresolved === true,
  }
}

function normWorkspace(raw: unknown): ProfileWorkspace | null {
  const r = pfObj(raw)
  const tenantId = r === null ? null : pfStr(r.tenant_id)
  if (r === null || tenantId === null) return null
  return {
    tenant_id: tenantId,
    display_name: pfStr(r.display_name) ?? tenantId,
    kind: pfStr(r.kind) ?? '',
    role: normRole(r.role),
    state: pfStr(r.state),
    selectable: r.selectable !== false,
    is_acting: r.is_acting === true,
    is_default: r.is_default === true,
  }
}

function normDefault(raw: unknown): ProfileDefault {
  const r = pfObj(raw) ?? {}
  return { tenant_id: pfStr(r.tenant_id), source: pfStr(r.source), ignored: pfStr(r.ignored) }
}

function normGitHub(raw: unknown): ProfileGitHub | null {
  const r = pfObj(raw)
  const tenantId = r === null ? null : pfStr(r.tenant_id)
  if (r === null || tenantId === null) return null
  const f = pfObj(r.failure)
  return {
    tenant_id: tenantId,
    state: pfStr(r.state),
    forge_login: pfStr(r.forge_login),
    method: pfStr(r.method),
    access_expires_at: pfStr(r.access_expires_at),
    refreshed_at: pfStr(r.refreshed_at),
    failure: f === null ? null : { code: pfStr(f.code), recovery: pfStr(f.recovery) },
  }
}

function normProfileKey(raw: unknown): ProfileKeyStatus {
  const r = pfObj(raw) ?? {}
  return { set: r.set === true, set_at: pfStr(r.set_at), set_by: pfStr(r.set_by), version: pfStrOrNum(r.version) }
}

function normTypeSafe(raw: unknown): ProfileTypeSafe {
  const r = pfObj(raw) ?? {}
  const acting = pfObj(r.acting_workspace) ?? {}
  return {
    personal: pfObj(r.personal) === null ? null : normProfileKey(r.personal),
    acting_workspace: { ...normProfileKey(acting), tenant_id: pfStr(acting.tenant_id) ?? '' },
    uses: r.uses === 'user' || r.uses === 'tenant' ? r.uses : 'none',
  }
}

function normTopics(raw: unknown): NotificationTopic[] {
  const given = Array.isArray(raw) ? raw : []
  return NOTIFICATION_TOPICS.filter((t) => given.includes(t))
}

function normPreferencesSet(raw: unknown): Preferences {
  const r = pfObj(raw) ?? {}
  const reminder = REMINDER_CADENCES.find((c) => c === r.reminder) ?? 'as_set'
  return { muted: normTopics(r.muted), reminder }
}

function normNotifications(raw: unknown): ProfileNotifications {
  const r = pfObj(raw) ?? {}
  const topics = normTopics(r.topics)
  return { preferences: normPreferencesSet(r.preferences), topics: topics.length === 0 ? [...NOTIFICATION_TOPICS] : topics }
}

function normWindow(raw: unknown): ProfileQuotaWindow | undefined {
  const r = pfObj(raw)
  return r === null ? undefined : { utilization: pfNum(r.utilization), resets_at: pfStr(r.resets_at) }
}

function normAccount(raw: unknown): ProfileClaudeAccount | null {
  const r = pfObj(raw)
  if (r === null || (r.relation !== 'own' && r.relation !== 'borrowed')) return null
  const w = pfObj(r.windows) ?? {}
  const windows: ProfileClaudeAccount['windows'] = {}
  const five = normWindow(w.five_hour)
  const seven = normWindow(w.seven_day)
  if (five !== undefined) windows.five_hour = five
  if (seven !== undefined) windows.seven_day = seven
  return {
    account_id: pfStr(r.account_id),
    label: pfStr(r.label),
    relation: r.relation,
    state: pfStr(r.state),
    windows,
    lent_to_count: pfNum(r.lent_to_count),
    // An account with no freshness flag is not claimed fresh.
    stale: r.stale !== false,
    // The account's Name (#1913): null when it has none, so the label shows.
    display_name: pfStr(r.display_name),
  }
}

function normClaudeAccounts(raw: unknown): ProfileClaudeAccounts {
  const r = pfObj(raw) ?? {}
  const list = Array.isArray(r.accounts) ? r.accounts : []
  return {
    personal_workspace: pfStr(r.personal_workspace),
    accounts: list.map(normAccount).filter((a): a is ProfileClaudeAccount => a !== null),
    unreadable_document_count: pfNum(r.unreadable_document_count) ?? 0,
  }
}

function pfPresent<T>(v: T | null): v is T {
  return v !== null
}

export function normProfile(raw: unknown): Profile {
  const r = pfObj(raw) ?? {}
  const unavailable = Array.isArray(r.unavailable) ? PROFILE_SECTIONS.filter((s) => (r.unavailable as unknown[]).includes(s)) : []
  const c = pfObj(r.connections) ?? {}
  const connections: Profile['connections'] = {}
  if (Array.isArray(c.github)) connections.github = c.github.map(normGitHub).filter(pfPresent)
  if (pfObj(c.typesafe) !== null) connections.typesafe = normTypeSafe(c.typesafe)
  const profile: Profile = {
    identity: normIdentity(r.identity),
    workspaces: Array.isArray(r.workspaces) ? r.workspaces.map(normWorkspace).filter(pfPresent) : [],
    default_workspace: normDefault(r.default_workspace),
    connections,
    revision: pfNum(r.revision),
    unavailable,
    ...normTimeZone(r),
  }
  if (pfObj(r.notifications) !== null) profile.notifications = normNotifications(r.notifications)
  if (pfObj(r.claude_accounts) !== null) profile.claude_accounts = normClaudeAccounts(r.claude_accounts)
  return profile
}

const TIME_ZONE_SOURCES: readonly TimeZoneSource[] = ['profile', 'default', 'unavailable']

/**
 * `time_zone` and `time_zone_source` (TIMEZONE-SERVER). A source this console
 * does not know, or a zone that is not text where one is claimed, reads as
 * unread: never as UTC, which would move every time on the page.
 */
function normTimeZone(r: Record<string, unknown>): Pick<Profile, 'time_zone' | 'time_zone_source'> {
  const source = TIME_ZONE_SOURCES.find((s) => s === r.time_zone_source)
  const zone = pfStr(r.time_zone)
  if (source === undefined) return {}
  if (source === 'unavailable' || zone === null) return { time_zone: null, time_zone_source: 'unavailable' }
  return { time_zone: zone, time_zone_source: source }
}

function normPreferencesSaved(raw: unknown): PreferencesSaved {
  const r = pfObj(raw) ?? {}
  const id = pfObj(r.identity) ?? {}
  return {
    identity: { display_name: pfStr(id.display_name), display_name_suggested: pfStr(id.display_name_suggested) },
    default_workspace: normDefault(r.default_workspace),
    notifications: normNotifications(r.notifications),
    revision: pfNum(r.revision) ?? 0,
    ...normTimeZone(r),
  }
}

/**
 * `GET /v1/profile`: the signed-in person's own profile, each section failing
 * alone. Every answered read tells `timezone.ts` who is signed in and their
 * zone (TIMEZONE-SERVER), so Profile's read is also the zone's, not a second one.
 */
export async function loadProfile(): Promise<Result<Profile>> {
  const r = await readAs(route('/v1/profile'), normProfile)
  if (r.status === 'ok' || r.status === 'stale') noteProfile(r.data)
  return r
}

/**
 * `PUT /v1/profile/preferences`: replace the whole set at `body.revision`. A
 * stale revision is a 409 `preferences_changed` (another tab saved first); a
 * refused name is a 422 with its `display_name_*` code and a message that names
 * the rule. A save that leaves the zone as stored goes at the revision a zone
 * pick on this page moved `body.revision` to (`revisionAfterZoneSaves`), so
 * Profile's next save after a pick is not refused as changed elsewhere.
 */
export async function putPreferences(body: PreferencesWrite): Promise<Result<PreferencesSaved>> {
  const sent = body.time_zone === undefined ? { ...body, revision: await revisionAfterZoneSaves(body.revision) } : body
  const r = await writeTo(route('/v1/profile/preferences'), 'PUT', sent)
  if (r.status === 'ok' || r.status === 'stale') return { ...r, data: normPreferencesSaved(r.data) }
  if (r.status === 'empty') return { status: 'error', error: { kind: 'server_error', httpStatus: null, code: null, message: 'The save answered with nothing to read back. Reload to see what was stored.', method: 'PUT' } }
  return r
}

// ---------------------------------------------------------------------------
// The workspace's Jev judgment backend (JUDGE-SELECT; swarm_api/judgment.py).
// Internal Jev is the default; Internal Laya and TypeSafe are the others. The
// backend is chosen BY NAME: no URL or key goes in the PUT body.
// ---------------------------------------------------------------------------

export type JevBackend = 'internal-jev' | 'internal-laya' | 'typesafe'

export const JEV_BACKENDS: readonly JevBackend[] = ['internal-jev', 'internal-laya', 'typesafe']

export interface JevOption {
  backend: JevBackend
  label: string
  available: boolean
  /** Why not, in the API's words; null when available. */
  reason: string | null
}

/** The `jev` object: the chosen backend, whether it can run, and every option. */
export interface JevStatus extends JevOption {
  default: JevBackend
  set_at: string | null
  set_by: string | null
  options: JevOption[]
}

/** `GET /v1/judgment` and `GET|PUT /v1/workspaces/{id}/judgment-backend`. */
export interface Judgment {
  tenant_id: string
  can_edit: boolean
  jev: JevStatus
}

function jevBackend(v: unknown): JevBackend {
  return v === 'internal-laya' || v === 'typesafe' ? v : 'internal-jev'
}

function normJevOption(raw: unknown): JevOption {
  const r = (typeof raw === 'object' && raw !== null ? raw : {}) as Record<string, unknown>
  return {
    backend: jevBackend(r.backend),
    label: keyField(r.label) ?? '',
    available: r.available === true,
    reason: keyField(r.reason),
  }
}

function normJudgment(raw: unknown): Judgment {
  const r = (typeof raw === 'object' && raw !== null ? raw : {}) as Record<string, unknown>
  const jev = (typeof r.jev === 'object' && r.jev !== null ? r.jev : {}) as Record<string, unknown>
  const options = Array.isArray(jev.options) ? jev.options.map(normJevOption) : []
  return {
    tenant_id: keyField(r.tenant_id) ?? '',
    can_edit: r.can_edit === true,
    jev: {
      ...normJevOption(jev),
      default: jevBackend(jev.default),
      set_at: keyField(jev.set_at),
      set_by: keyField(jev.set_by),
      options: JEV_BACKENDS.map((b) => options.find((o) => o.backend === b) ?? { backend: b, label: b, available: false, reason: null }),
    },
  }
}

/** `GET /v1/judgment`: the selected workspace's Jev backend and what is available. */
export async function loadJudgment(): Promise<Result<Judgment>> {
  return readAs(route('/v1/judgment'), normJudgment)
}

/** `PUT /v1/workspaces/{tenant_id}/judgment-backend`: the owner's or an admin's choice. */
export async function putJudgmentBackend(tenantId: string, backend: JevBackend): Promise<Result<unknown>> {
  return writeTo(route('/v1/workspaces/{tenant_id}/judgment-backend', { tenant_id: tenantId }), 'PUT', { judgment_backend: backend })
}

// ---------------------------------------------------------------------------
// Code intelligence (CTXQ-UI over CTXQ-SET; docs/design/context-quality.md
// §4.7). The workspace's switch is ONE FIELD of `workspace_settings/{tenant_id}`
// (`code_intelligence`, beside `judgment_backend` and `merge`), written by
// swarm-api (`swarm_api/codeintel.py`); the API answers and takes the flat
// shape: `{jev, context, context_ab, defaults, updated_at, updated_by,
// can_edit, repositories}` and a PUT body of `{jev}`. `jev` -- whether Jev is ever sent code -- is OFF unless
// the API says, explicitly, `true`: an answer without the field is a workspace
// that never turned it on. A repository's `context` override is
// `on | off`, or absent for inherit, and travels on `PATCH
// /v1/repositories/{repo_id}` as `merge_policy` does, `inherit` clearing it.
// These shapes follow CTXQ-SET's API; a fix to them is here.
// ---------------------------------------------------------------------------

export interface CodeIntelligence {
  tenant_id: string
  /** The workspace's owner or a platform admin; the API is the gate. */
  can_edit: boolean
  /** Whether Jev is sent excerpts of this workspace's code. Off by default. */
  jev: boolean
  /** The local context default (pack and graph tools); nothing leaves the platform. On by default. */
  context: boolean
  set_at: string | null
  set_by: string | null
}

function normCodeIntelligence(raw: unknown): CodeIntelligence {
  const r = (typeof raw === 'object' && raw !== null ? raw : {}) as Record<string, unknown>
  const defaults = (typeof r.defaults === 'object' && r.defaults !== null ? r.defaults : {}) as Record<string, unknown>
  // `context` is `on | off | inherit`; inherit is the platform default.
  const context = r.context === 'on' || r.context === 'off' ? r.context : defaults.context
  return {
    tenant_id: keyField(r.tenant_id) ?? '',
    can_edit: r.can_edit === true,
    jev: r.jev === true,
    context: context !== 'off',
    set_at: keyField(r.updated_at),
    set_by: keyField(r.updated_by),
  }
}

/** `GET /v1/code-intelligence`: the selected workspace's Code intelligence setting. */
export async function loadCodeIntelligence(): Promise<Result<CodeIntelligence>> {
  return readAs(route('/v1/code-intelligence'), normCodeIntelligence)
}

/** `PUT /v1/workspaces/{tenant_id}/code-intelligence`: the owner's or an admin's switch. */
export async function putCodeIntelligence(tenantId: string, jev: boolean): Promise<Result<unknown>> {
  return writeTo(route('/v1/workspaces/{tenant_id}/code-intelligence', { tenant_id: tenantId }), 'PUT', { jev })
}

// The per-agent budget (BUDGET-UI-SETTINGS; part of #2459,
// docs/design/agent-budget.md §2, §4). A workspace admin sets one default, in
// dollars OR tokens (`PUT /v1/workspaces/{tenant_id}/budget`); a platform
// admin sets the ceiling no default may exceed, in either unit or both
// (`agent_budget_ceiling` in `PUT /v1/admin/settings`). Off until a workspace
// admin sets one: the ceiling alone limits nothing (§4). The bounds and the
// refusal are the API's (`swarm_api.budget`); this file only draws them.

/** A budget's unit: `budget.UNITS`. Dollars are the `cost_usd` estimate; tokens the runner's own count. */
export type BudgetUnit = 'usd' | 'tokens'

/** A limit in either unit or both; null is "no limit in that unit" (`budget.Limits`). */
export interface BudgetLimits {
  usd: number | null
  tokens: number | null
}

/** `GET /v1/budget` and the PUT's answer (`budget.view`). */
export interface WorkspaceBudget {
  tenant_id: string
  /** The workspace default; null is off, the default. */
  budget: { unit: BudgetUnit; amount: number } | null
  /** The platform ceiling the default must stay under, per unit. */
  ceiling: BudgetLimits
  /** The largest amount the API takes in each unit; null when it was not served. */
  max: BudgetLimits
  set_at: string | null
  set_by: string | null
  /** The workspace's owner or admin, or a platform admin; the API is the gate. */
  can_edit: boolean
}

const positive = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) && v > 0 ? v : null)

/** `{usd, tokens}` as served, a unit that is not a positive number read as no limit, never guessed. */
export function normBudgetLimits(raw: unknown): BudgetLimits {
  const r = (typeof raw === 'object' && raw !== null && !Array.isArray(raw) ? raw : {}) as Record<string, unknown>
  const tokens = positive(r.tokens)
  return { usd: positive(r.usd), tokens: tokens !== null && Number.isInteger(tokens) ? tokens : null }
}

function normWorkspaceBudget(raw: unknown): WorkspaceBudget {
  const r = (typeof raw === 'object' && raw !== null ? raw : {}) as Record<string, unknown>
  const b = (typeof r.budget === 'object' && r.budget !== null ? r.budget : null) as Record<string, unknown> | null
  const unit = b?.unit === 'usd' || b?.unit === 'tokens' ? b.unit : null
  const amount = positive(b?.amount)
  const bounds = (typeof r.bounds === 'object' && r.bounds !== null ? r.bounds : {}) as Record<string, { max?: unknown } | undefined>
  return {
    tenant_id: keyField(r.tenant_id) ?? '',
    budget: unit !== null && amount !== null ? { unit, amount } : null,
    ceiling: normBudgetLimits(r.ceiling),
    max: { usd: positive(bounds.usd?.max), tokens: positive(bounds.tokens?.max) },
    set_at: keyField(r.set_at),
    set_by: keyField(r.set_by),
    can_edit: r.can_edit === true,
  }
}

/** `GET /v1/budget`: the selected workspace's per-agent budget, its ceiling and whether the caller may change it. */
export async function loadWorkspaceBudget(): Promise<Result<WorkspaceBudget>> {
  return readAs(route('/v1/budget'), normWorkspaceBudget)
}

/**
 * `PUT /v1/workspaces/{tenant_id}/budget`: the default, or null to turn it off. A default above the platform ceiling
 * in its unit is refused 422 `budget_above_ceiling` with nothing changed (`budgetRefusalWords` says it in words).
 */
export async function putWorkspaceBudget(tenantId: string, budget: { unit: BudgetUnit; amount: number } | null): Promise<Result<WorkspaceBudget>> {
  const r = await writeTo(route('/v1/workspaces/{tenant_id}/budget', { tenant_id: tenantId }), 'PUT', { budget })
  if (r.status === 'ok' || r.status === 'stale') return { ...r, data: normWorkspaceBudget(r.data) }
  return r as Result<WorkspaceBudget>
}

/** The platform ceiling in a platform settings read: `agent_budget_ceiling`, which `PlatformSettings` keeps as served. */
export function budgetCeilingOf(settings: PlatformSettings): BudgetLimits {
  return normBudgetLimits((settings as unknown as { agent_budget_ceiling?: unknown }).agent_budget_ceiling)
}

/**
 * `PUT /v1/admin/settings` with `agent_budget_ceiling` and nothing else, so a save never rewrites another setting. The
 * ceiling is replaced whole: a unit left out has none, and `{}` turns it off (`Store.set_platform_settings`).
 */
export async function saveBudgetCeiling(ceiling: BudgetLimits): Promise<Result<PlatformSettings>> {
  const body: Record<string, number> = {}
  if (ceiling.usd !== null) body.usd = ceiling.usd
  if (ceiling.tokens !== null) body.tokens = ceiling.tokens
  const r = await write(route('/v1/admin/settings'), 'PUT', { agent_budget_ceiling: body })
  if (r.status === 'ok' || r.status === 'stale') return { ...r, data: normPlatformSettings(r.data) }
  return r as Result<PlatformSettings>
}

/**
 * What a budget does, in the console's words (#2459, BGD 2026-10-26), drawn by both settings cards so they cannot
 * disagree: the worker's 80% note and its stop at the next turn boundary (docs/design/agent-budget.md §5).
 */
export const BUDGET_DOES =
  'At 80% of its budget an agent is told to finish and publish. At 100% it stops at its next turn boundary, keeps its work and ends “stopped at its budget”.'

/**
 * A typed amount in its unit, or null: dollars a number above 0, tokens a whole number of at least 1. Commas, spaces
 * and a leading `$` are allowed. The upper bound is the API's (`bounds`), which refuses past it with 422.
 */
export function parseBudgetAmount(unit: BudgetUnit, typed: string): number | null {
  const t = typed.replace(/[,\s_]/g, '').replace(/^\$/, '')
  if (unit === 'tokens') {
    if (!/^\d+$/.test(t)) return null
    const n = Number(t)
    return Number.isSafeInteger(n) && n >= 1 ? n : null
  }
  if (!/^(?:\d+(?:\.\d*)?|\.\d+)$/.test(t)) return null
  const n = Number(t)
  return Number.isFinite(n) && n > 0 ? n : null
}

/** `$1.00`, as `budget.describe` writes dollars. */
export function budgetDollars(n: number): string {
  return `$${n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

/** `15,000,000 tokens`. */
export function budgetTokens(n: number): string {
  return `${n.toLocaleString('en-US')} ${n === 1 ? 'token' : 'tokens'}`
}

/** One amount in its unit. */
export function budgetAmount(unit: BudgetUnit, n: number): string {
  return unit === 'usd' ? budgetDollars(n) : budgetTokens(n)
}

/** A limit in words, both units joined by "and", or null when it names neither (`budget.describe`). */
export function budgetLimitsWords(l: BudgetLimits): string | null {
  const parts = [...(l.usd !== null ? [budgetDollars(l.usd)] : []), ...(l.tokens !== null ? [budgetTokens(l.tokens)] : [])]
  return parts.length === 0 ? null : parts.join(' and ')
}

/**
 * A refused save of a workspace default, in words, from `budget_above_ceiling`'s detail (`{budget, ceiling}`): what
 * was asked, the ceiling it is above, and what may be saved instead. Null for any other refusal, which is drawn as
 * the API wrote it.
 */
export function budgetRefusalWords(error: ApiError): string | null {
  if (error.code !== 'budget_above_ceiling') return null
  const d = (typeof error.detail === 'object' && error.detail !== null ? error.detail : {}) as Record<string, unknown>
  const asked = normBudgetLimits(d.budget)
  const ceiling = normBudgetLimits(d.ceiling)
  const unit: BudgetUnit | null = asked.usd !== null ? 'usd' : asked.tokens !== null ? 'tokens' : null
  const top = unit === null ? null : ceiling[unit]
  if (unit === null || top === null) return error.message
  return (
    `${budgetAmount(unit, asked[unit]!)} is above the platform ceiling of ${budgetAmount(unit, top)}, so nothing was ` +
    `changed. Save ${budgetAmount(unit, top)} or less, or ask a platform admin to raise the ceiling.`
  )
}

/**
 * The workspace's visual-QA settings (QA-FLOWS-UI over QA-FLOWS-CAP, part of #2136; `swarm_api.qasettings`). One
 * setting: `qa_flows_per_step`, how many read-only flows one live-browser step of a QA run runs when the run names
 * none of its own. Its default and bounds are the API's, served with it, so this file does not restate them; the
 * fallbacks below are only for an answer that omits them (`qaspec.FLOWS_PER_STEP_DEFAULT`, `MIN_`/`MAX_FLOWS_PER_STEP`).
 */
export interface QaSettings {
  tenant_id: string
  /** The value in force: the stored one, else the default. */
  qa_flows_per_step: number
  /** The platform default (15). */
  default_flows_per_step: number
  min_flows_per_step: number
  max_flows_per_step: number
  /** The workspace's owner or admin, or a platform admin; the API is the gate. */
  can_edit: boolean
  set_at: string | null
  set_by: string | null
}

function qaInt(v: unknown, fallback: number): number {
  return typeof v === 'number' && Number.isInteger(v) ? v : fallback
}

function normQaSettings(raw: unknown): QaSettings {
  const r = (typeof raw === 'object' && raw !== null ? raw : {}) as Record<string, unknown>
  const sub = (v: unknown) => (typeof v === 'object' && v !== null ? v : {}) as Record<string, unknown>
  const bound = sub(sub(r.bounds).qa_flows_per_step)
  const fallback = qaInt(sub(r.defaults).qa_flows_per_step, 15)
  return {
    tenant_id: keyField(r.tenant_id) ?? '',
    qa_flows_per_step: qaInt(r.qa_flows_per_step, fallback),
    default_flows_per_step: fallback,
    min_flows_per_step: qaInt(bound.min, 1),
    max_flows_per_step: qaInt(bound.max, 30),
    can_edit: r.can_edit === true,
    set_at: keyField(r.set_at),
    set_by: keyField(r.set_by),
  }
}

/** `GET /v1/qa-settings`: the selected workspace's QA settings (any role in it). */
export async function loadQaSettings(): Promise<Result<QaSettings>> {
  return readAs(route('/v1/qa-settings'), normQaSettings)
}

/**
 * `PUT /v1/workspaces/{tenant_id}/qa-settings` `{qa_flows_per_step}`: its owner or admin, or a platform admin; anyone
 * else is 403 `role_admin_required` with nothing written, and a value outside 1-30 is the API's 422. The answer is the
 * settings as they now stand.
 */
export async function putQaSettings(tenantId: string, flowsPerStep: number): Promise<Result<QaSettings>> {
  const r = await writeTo(route('/v1/workspaces/{tenant_id}/qa-settings', { tenant_id: tenantId }), 'PUT', { qa_flows_per_step: flowsPerStep })
  return r.status === 'ok' || r.status === 'stale' ? { ...r, data: normQaSettings(r.data) } : (r as Result<QaSettings>)
}

/** A repository's `context` override; `inherit` is the absent field. */
export type RepoContext = 'on' | 'off' | 'inherit'

/** The registration's `context`, `inherit` when absent or unknown. */
export function normRepoContext(raw: unknown): RepoContext {
  if (typeof raw !== 'object' || raw === null) return 'inherit'
  const outer = raw as Record<string, unknown>
  const rec = (typeof outer.repository === 'object' && outer.repository !== null ? outer.repository : outer) as Record<string, unknown>
  return rec.context === 'on' || rec.context === 'off' ? rec.context : 'inherit'
}

/** `PATCH /v1/repositories/{repo_id}` with `context`: on, off, or inherit to clear it. */
export async function patchRepositoryContext(repoId: string, context: RepoContext): Promise<Result<unknown>> {
  const target = route('/v1/repositories/{repo_id}', { repo_id: repoId })
  if (USE_FIXTURES) return repoNotServed<unknown>(target, 'PATCH')
  return write(target, 'PATCH', { context })
}

/**
 * `PATCH /v1/repositories/{repo_id}` with `index.on_change` alone: the API
 * merges `index` field by field (`Repositories.patch`), so the interval and
 * the throttle are left as they are. Any member of the workspace may send it.
 */
export async function patchRepositoryTrigger(repoId: string, onChange: 'poll' | 'webhook' | 'off'): Promise<Result<unknown>> {
  const target = route('/v1/repositories/{repo_id}', { repo_id: repoId })
  if (USE_FIXTURES) return repoNotServed<unknown>(target, 'PATCH')
  return write(target, 'PATCH', { index: { on_change: onChange } })
}

/**
 * `PATCH /v1/repositories/{repo_id}` with `index.keep_versions` alone
 * (INDEX-RETENTION): how many past index versions the repository keeps,
 * 1-100. The same merge and the same members as the change trigger.
 */
export async function patchRepositoryKeepVersions(repoId: string, keepVersions: number): Promise<Result<unknown>> {
  const target = route('/v1/repositories/{repo_id}', { repo_id: repoId })
  if (USE_FIXTURES) return repoNotServed<unknown>(target, 'PATCH')
  return write(target, 'PATCH', { index: { keep_versions: keepVersions } })
}

/**
 * `PATCH /v1/repositories/{repo_id}` with `agents_may_file_issues` alone (AU-0
 * REG-FLAG, docs/design/agent-autonomy.md §3.2). Only the workspace's admin
 * may send it (routes/repositories.py `_context_by`); the API writes the
 * `agents_may_file_issues_set` audit entry with the change. The fixture build
 * keeps the value for its own registrations, so its list reads it back.
 */
export async function patchRepositoryFileIssues(repoId: string, value: boolean): Promise<Result<unknown>> {
  const target = route('/v1/repositories/{repo_id}', { repo_id: repoId })
  if (USE_FIXTURES) {
    if (!FIXTURE_REPOS.some((r) => r.repo_id === repoId)) return repoNotServed<unknown>(target)
    await new Promise((r) => setTimeout(r, 180))
    noteFixtureProbe(target, 180, true)
    fixtureFileIssues.set(repoId, value)
    return { status: 'ok', fetchedAt: Date.now(), data: { repository: { repo_id: repoId, agents_may_file_issues: value }, tenant_id: FIXTURE_TENANT } }
  }
  return write(target, 'PATCH', { agents_may_file_issues: value })
}

/**
 * `PATCH /v1/repositories/{repo_id}` with `workflow_files` alone (#2029,
 * WF-APP-TOKEN). Only the workspace's admin may send it; the API writes the
 * `repository_workflow_files_set` audit entry with the change. The fixture
 * build keeps the value for its own registrations, so its list reads it back.
 */
export async function patchRepositoryWorkflowFiles(repoId: string, value: boolean): Promise<Result<unknown>> {
  const target = route('/v1/repositories/{repo_id}', { repo_id: repoId })
  if (USE_FIXTURES) {
    if (!FIXTURE_REPOS.some((r) => r.repo_id === repoId)) return repoNotServed<unknown>(target)
    await new Promise((r) => setTimeout(r, 180))
    noteFixtureProbe(target, 180, true)
    fixtureWorkflowFiles.set(repoId, value)
    return { status: 'ok', fetchedAt: Date.now(), data: { repository: { repo_id: repoId, workflow_files: value }, tenant_id: FIXTURE_TENANT } }
  }
  return write(target, 'PATCH', { workflow_files: value })
}

// ---------------------------------------------------------------------------
// Connect GitHub (#780, OB3): the SwarmCloud GitHub App's user authorisation.
//
// The routes are swarm_api/routes/forgeapp.py and routes/onboarding.py. NONE
// ANSWERS A VALUE -- no token, no code, no state after `authorize` minted it
// -- and nothing here logs or keeps what it posts. The fixture build serves
// the onboarding read (QAFIX6-FIXTURE d, `fixtureOnboarding`) and answers each
// write as not served, like the git-token routes above.
// ---------------------------------------------------------------------------

/** `GET /v1/onboarding`: the caller's six steps, derived on this read. */
export async function loadOnboarding(): Promise<Result<OnboardingDoc>> {
  return readAs(route('/v1/onboarding'), (raw) => raw as OnboardingDoc)
}

/** `POST /v1/onboarding/github/authorize`: a single-use, ten-minute authorise URL for this person. */
export async function authorizeGitHub(surface: GitHubAuthorizeSurface = 'console'): Promise<Result<GitHubAuthorization>> {
  return writeTo(route('/v1/onboarding/github/authorize'), 'POST', { surface }) as Promise<Result<GitHubAuthorization>>
}

/**
 * `POST /v1/onboarding/github/exchange`: the callback's `state` and `code`
 * (or GitHub's `error`). The server spends the state first, so a second post
 * of the same pair is refused `AUTHORISATION_EXPIRED` -- the callback page
 * posts once.
 */
export async function exchangeGitHub(body: GitHubExchangeBody): Promise<Result<GitHubExchangeResponse>> {
  return writeTo(route('/v1/onboarding/github/exchange'), 'POST', body) as Promise<Result<GitHubExchangeResponse>>
}

/** `DELETE /v1/onboarding/github`: revoke at GitHub, disable the slot's versions, delete the caller's grants. */
export async function disconnectGitHub(): Promise<Result<GitHubDisconnectResponse>> {
  return writeTo(route('/v1/onboarding/github'), 'DELETE') as Promise<Result<GitHubDisconnectResponse>>
}

/**
 * `POST /v1/onboarding/github/token` (D5): the signed-in person's fallback
 * token for ONE owner, probed and stored once in their per-owner slot. The
 * one route a value goes out on: posted once, never retried, never kept or
 * logged here, and the answer carries the token's names, never the value.
 */
export async function storeOwnerToken(owner: string, token: string): Promise<Result<OwnerTokenResponse>> {
  return writeTo(route('/v1/onboarding/github/token'), 'POST', { owner, token }) as Promise<Result<OwnerTokenResponse>>
}

/** `POST /v1/onboarding/dismiss`: hide the caller's checklist (`true`), or show it again. Steps keep their state. */
export async function dismissOnboarding(dismissed = true): Promise<Result<OnboardingDismissal>> {
  if (USE_FIXTURES) {
    const target = route('/v1/onboarding/dismiss')
    await new Promise((r) => setTimeout(r, 80))
    noteFixtureProbe(target, 80, true)
    fixtureOnboardingHiddenAt = dismissed ? new Date().toISOString() : null
    const doc = fixtureOnboarding() as { tenant_id: string; user_hash: string }
    return {
      status: 'ok', fetchedAt: Date.now(),
      data: { tenant_id: doc.tenant_id, user_hash: doc.user_hash, dismissed, dismissed_at: fixtureOnboardingHiddenAt },
    }
  }
  return writeTo(route('/v1/onboarding/dismiss'), 'POST', { dismissed }) as Promise<Result<OnboardingDismissal>>
}

// ---------------------------------------------------------------------------
// GitHub permissions health (#1086, PERM-2 over PERM-1's read).
//
// `GET /v1/forge/permissions` is swarm_api/forgeperms.py. It answers what the
// platform needs (`required`), what the App requests (`app`), what each org's
// installation granted (`orgs`) and what each registered repository can do
// (`repositories`). Every call it makes is a GET and it stores nothing; a fix
// is always a link to GitHub, which is why the only URLs kept here are
// github.com pages. `refresh=true` skips the server's 60 s cache.
//
// NORMALISE OR DROP, NEVER GUESS. A field of the wrong type is null (not
// read), never false (refused) or true (granted): a dot or a card that turned
// green on a malformed answer would hide the one failure this read exists for.
// ---------------------------------------------------------------------------

/** One unmet requirement: what it is, and in plain words what breaks without it. */
export interface ForgeGap {
  /** GitHub's permission name; null on a token's measured refusal, whose grant is not readable. */
  permission: string | null
  access: string | null
  granted: string | null
  needed_for: string
  capabilities: string[]
}

/** A permission the App requests above what an installation accepted. */
export interface ForgePending {
  permission: string
  requested: string
  granted: string | null
}

export interface ForgeAppCard {
  configured: boolean
  slug: string | null
  /** What the App requests (`GET /app`'s permissions); null when it was not read. */
  granted: Record<string, string> | null
  /** Null when `GET /app` was not read; then `error` says why. */
  missing: ForgeGap[] | null
  /** Served to platform admins only; null for everyone else. */
  settings_url: string | null
  error: string | null
}

export interface ForgeOrgCard {
  org: string
  account_type: string | null
  installation_id: number
  /**
   * GitHub's permission map for the installation (`contents: write`), the
   * ones SwarmCloud does not use included; null when it was not read.
   */
  granted: Record<string, string> | null
  missing: ForgeGap[] | null
  pending: ForgePending[] | null
  review_url: string | null
  error: string | null
}

/** A capability's state: granted, refused (measured), or null (not measured). */
export type ForgeCapability = boolean | null

/** What reaches a repository: the App's installation, or the tenant's stored git access. */
export type ForgeReach = 'app' | 'token'

export interface ForgeRepoStatus {
  repo_id: string
  repo: string
  credential: ForgeReach | null
  installation_id: number | null
  capabilities: Record<string, ForgeCapability>
  blocked_by: ForgeGap[]
  error: string | null
}

export interface ForgePermissionsDoc {
  checked_at: string | null
  required: ForgeGap[]
  /**
   * What a registration with `workflow_files` on needs besides `required`
   * (#2029, forgeperms.py's `WORKFLOW_FILES_REQUIREMENT`: workflows: write);
   * null from an API that does not serve it. Never in `missing`: a switched-on
   * repository's row carries `capabilities.workflow_files` and its gap instead.
   */
  workflow_files_required: ForgeGap | null
  app: ForgeAppCard
  orgs: ForgeOrgCard[]
  repositories: ForgeRepoStatus[]
  cached: boolean
}

function fpStr(v: unknown): string | null {
  return typeof v === 'string' && v !== '' ? v : null
}

function fpObj(v: unknown): Record<string, unknown> | null {
  return typeof v === 'object' && v !== null && !Array.isArray(v) ? (v as Record<string, unknown>) : null
}

function fpList(v: unknown): Record<string, unknown>[] {
  return Array.isArray(v) ? v.map(fpObj).filter((o): o is Record<string, unknown> => o !== null) : []
}

/** A fix link is a github.com page, or nothing: a button to anywhere else is not drawn. */
function fpGitHubUrl(v: unknown): string | null {
  const s = fpStr(v)
  return s !== null && s.startsWith('https://github.com/') ? s : null
}

/** A permission map, keeping only string levels; not an object at all is null (not read), never `{}` (nothing granted). */
function fpPermMap(v: unknown): Record<string, string> | null {
  const o = fpObj(v)
  if (o === null) return null
  const out: Record<string, string> = {}
  for (const [k, level] of Object.entries(o)) if (typeof level === 'string' && level !== '') out[k] = level
  return out
}

function fpGap(o: Record<string, unknown>): ForgeGap | null {
  const why = fpStr(o.needed_for)
  if (why === null) return null
  return {
    permission: fpStr(o.permission),
    access: fpStr(o.access),
    granted: fpStr(o.granted),
    needed_for: why,
    capabilities: Array.isArray(o.capabilities) ? o.capabilities.filter((c): c is string => typeof c === 'string') : [],
  }
}

function fpGaps(v: unknown): ForgeGap[] | null {
  if (!Array.isArray(v)) return null
  return fpList(v).map(fpGap).filter((g): g is ForgeGap => g !== null)
}

export function normForgePermissions(raw: unknown): ForgePermissionsDoc {
  const doc = fpObj(raw) ?? {}
  const app = fpObj(doc.app) ?? {}
  const orgs: ForgeOrgCard[] = []
  for (const o of fpList(doc.orgs)) {
    const org = fpStr(o.org)
    if (org === null || typeof o.installation_id !== 'number') continue
    orgs.push({
      org,
      account_type: fpStr(o.account_type),
      installation_id: o.installation_id,
      granted: fpPermMap(o.granted),
      missing: fpGaps(o.missing),
      pending: Array.isArray(o.pending)
        ? fpList(o.pending).flatMap((p) => {
            const permission = fpStr(p.permission)
            const requested = fpStr(p.requested)
            return permission === null || requested === null ? [] : [{ permission, requested, granted: fpStr(p.granted) }]
          })
        : null,
      review_url: fpGitHubUrl(o.review_url),
      error: fpStr(o.error),
    })
  }
  const repositories: ForgeRepoStatus[] = []
  for (const r of fpList(doc.repositories)) {
    const repoId = fpStr(r.repo_id)
    const name = fpStr(r.repo)
    if (repoId === null || name === null) continue
    const caps: Record<string, ForgeCapability> = {}
    for (const [k, v] of Object.entries(fpObj(r.capabilities) ?? {})) caps[k] = typeof v === 'boolean' ? v : null
    repositories.push({
      repo_id: repoId,
      repo: name,
      credential: r.credential === 'app' || r.credential === 'token' ? r.credential : null,
      installation_id: typeof r.installation_id === 'number' ? r.installation_id : null,
      capabilities: caps,
      blocked_by: fpGaps(r.blocked_by) ?? [],
      error: fpStr(r.error),
    })
  }
  const wf = fpObj(doc.workflow_files_required)
  return {
    checked_at: fpStr(doc.checked_at),
    required: fpGaps(doc.required) ?? [],
    workflow_files_required: wf === null ? null : fpGap(wf),
    app: {
      configured: app.configured === true,
      slug: fpStr(app.slug),
      granted: fpPermMap(app.granted),
      missing: fpGaps(app.missing),
      settings_url: fpGitHubUrl(app.settings_url),
      error: fpStr(app.error),
    },
    orgs,
    repositories,
    cached: doc.cached === true,
  }
}

/** `GET /v1/forge/permissions`: the tenant's GitHub permissions; `refresh` reads GitHub again. */
export async function loadForgePermissions(refresh = false): Promise<Result<ForgePermissionsDoc>> {
  return readAs(route('/v1/forge/permissions', {}, refresh ? 'refresh=true' : undefined), normForgePermissions)
}

// ---------------------------------------------------------------------------
// Personal workspaces (#847, lane W8; docs/workspaces.md §6.3).
//
// The person's routes are swarm_api/routes/workspaces.py and are ALWAYS THE
// CALLER'S OWN: no route takes a tenant, an email or a workspace id, so the
// console cannot ask for anyone else's. The admin routes are
// routes/people.py and address a person by WORKSPACE ID, never an email, so
// a URL names nobody -- bar the admins routes, which the API defines by
// email. None takes or answers a credential, an image, a command or a
// resource spec (invariant 10). The fixture build answers each as not served.
// ---------------------------------------------------------------------------

/** `GET /v1/workspace`: the caller's own record, or `{state: "none"}`. */
export async function loadWorkspace(): Promise<Result<WorkspaceView>> {
  return readAs(route('/v1/workspace'), (raw) => raw as WorkspaceView)
}

/** `POST /v1/workspace`: request it. 202 made or re-opened, 200 already standing; the record either way. */
export async function requestWorkspace(): Promise<Result<WorkspaceView>> {
  return writeTo(route('/v1/workspace'), 'POST', { via: 'console' }) as Promise<Result<WorkspaceView>>
}

/** `POST /v1/workspace/loan-request`: ask an admin to lend a Claude account. Idempotent. */
export async function requestLoan(): Promise<Result<LoanRequestAnswer>> {
  return writeTo(route('/v1/workspace/loan-request'), 'POST', { via: 'console' }) as Promise<Result<LoanRequestAnswer>>
}

/**
 * `GET /v1/admin/people` (a platform admin: everyone, pending first, every
 * workspace's column, the last 50 audit entries, the lendable accounts; a
 * workspace admin: only the workspaces they administer, `ScopedPeopleDoc`).
 */
export async function loadPeople(): Promise<Result<PeopleDoc | ScopedPeopleDoc>> {
  return readAs(route('/v1/admin/people'), (raw) => raw as PeopleDoc | ScopedPeopleDoc)
}

// -- Admin › Runner sizes (SIZES-UI; part of #1689) ---------------------------
//
// docs/design/runner-sizes.md §4.5. Every route is `admin_auth`. The sizes,
// bounds, followers and prices are SERVED, never copied here: the frozen
// contract owns them and a copy is one more number to drift.

/** `GET /v1/admin/runner-sizes`: the current version, the defaults, the frozen bounds, followers, prices. */
export async function loadRunnerSizes(): Promise<Result<RunnerSizesDoc>> {
  return readAs(route('/v1/admin/runner-sizes'), (raw) => raw as RunnerSizesDoc)
}

/**
 * `POST /v1/admin/runner-sizes:preview`: what `classes` would do, nothing
 * written. A refused body is `valid: false` with every refusal, not a 422,
 * so the console previews as an admin types.
 */
export async function previewRunnerSizes(classes: RunnerSizeChanges, baseVersion: number): Promise<Result<RunnerSizesPreview>> {
  if (USE_FIXTURES) return fixtureRunnerSizesPreview(classes, baseVersion)
  return writeTo(route('/v1/admin/runner-sizes:preview'), 'POST', { classes, base_version: baseVersion }) as Promise<Result<RunnerSizesPreview>>
}

/**
 * The preview's answer in the fixture build (run-4 admin-002): nothing is
 * resized there, so every class's units and cost stay what they are and the
 * body is valid -- the preview's table is drawn, at the figures the document
 * serves. A cost is the class's request at the document's prices. Admin-gated
 * like the route.
 */
async function fixtureRunnerSizesPreview(classes: RunnerSizeChanges, baseVersion: number): Promise<Result<RunnerSizesPreview>> {
  const target = route('/v1/admin/runner-sizes:preview')
  await new Promise((r) => setTimeout(r, 80))
  if (!fixtureIsAdmin) return fixtureAdminRefusal(target, 80)
  noteFixtureProbe(target, 80, true)
  const named = Object.keys(classes)
  const asked = named.length === 0 ? Object.keys(FIXTURE_SIZES) : named.filter((c) => c in FIXTURE_SIZES)
  const cost = (s: RunnerSize) => Math.round((s.cpu_request * FIXTURE_SIZE_PRICES.vcpu_hour + s.memory_gib * FIXTURE_SIZE_PRICES.gib_hour) * 10_000) / 10_000
  const units = Object.fromEntries(Object.entries(FIXTURE_SIZES).map(([c, s]) => [c, s.units]))
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      base_version: baseVersion,
      valid: true,
      refusals: [],
      classes: Object.fromEntries(Object.entries(FIXTURE_SIZES).map(([c, s]) => [c, { units_before: s.units, units_after: s.units, cost_hour_before: cost(s), cost_hour_after: cost(s) }])),
      // The resource pools' ceilings in fixtureCapacity, each admitting the same number before and after.
      pools: [['resource:standard', 20], ['resource:browser', 4], ['resource:large', 2]].map(([pool, limit]) => {
        const cls = String(pool).slice('resource:'.length)
        const admits = { [cls]: Math.floor(Number(limit) / (units[cls] ?? 1)) }
        return { pool: String(pool), hard_limit: Number(limit), admits_before: admits, admits_after: admits }
      }),
      recent_use: Object.fromEntries(asked.map((c) => [c, fixtureRecentUse(c)])),
      below_recent_peak: [],
    },
  }
}

/** A class's 7-day use, every share null where no record feeds it (LIVE-UTIL's samples are not in the fixture build). */
function fixtureRecentUse(cls: string): RunnerRecentUse {
  const size = FIXTURE_SIZES[cls]
  return {
    attempts: cls === 'standard' ? 42 : cls === 'browser' ? 6 : 3,
    since: fixtureAgo(7 * 24 * FIXTURE_HOUR),
    memory_peak_gib_max: size === undefined ? null : Math.round(size.memory_gib * 0.7 * 10) / 10,
    memory_peak_gib_p95: size === undefined ? null : Math.round(size.memory_gib * 0.55 * 10) / 10,
    cpu_mean_cores_median: null,
    share_idle: null, share_above_request: null, share_at_limit: null, share_at_limit_long: null, throttled_share: null,
    oom_near_miss: 0,
    by_size_version: [],
  }
}

/**
 * `PUT /v1/admin/runner-sizes`: version `base_version + 1`. 409
 * `stale_base_version` when another admin saved in between; 422
 * `size_out_of_bounds`; 422 `below_recent_peak` for an unconfirmed memory cut.
 */
export async function saveRunnerSizes(body: {
  base_version: number
  reason: string
  classes: RunnerSizeChanges
  confirm_below_recent_peak: boolean
}): Promise<Result<RunnerSizesDoc>> {
  return writeTo(route('/v1/admin/runner-sizes'), 'PUT', body) as Promise<Result<RunnerSizesDoc>>
}

/** `POST /v1/admin/runner-sizes/restore`: a NEW version with `version`'s classes (0: the code defaults). */
export async function restoreRunnerSizes(body: {
  base_version: number
  version: number
  reason: string
  confirm_below_recent_peak: boolean
}): Promise<Result<RunnerSizesDoc>> {
  return writeTo(route('/v1/admin/runner-sizes/restore'), 'POST', body) as Promise<Result<RunnerSizesDoc>>
}

/** `GET /v1/admin/runner-sizes/versions`: the audit, newest first. A row that is not an object is dropped. */
export async function loadRunnerSizeVersions(): Promise<Result<RunnerSizeVersion[]>> {
  return readAs(
    route('/v1/admin/runner-sizes/versions?limit=50'),
    (raw) => {
      const rows = (raw as { versions?: unknown } | null)?.versions
      return Array.isArray(rows) ? rows.filter((r): r is RunnerSizeVersion => typeof r === 'object' && r !== null && typeof (r as RunnerSizeVersion).version === 'number') : []
    },
    (d) => d.length === 0,
  )
}

/** `POST /v1/admin/workspaces/{workspace_id}/approve` (admin): approve, then publish the workspace id. */
export async function approveWorkspace(workspaceId: string): Promise<Result<WorkspaceActionAnswer>> {
  return writeTo(route('/v1/admin/workspaces/{workspace_id}/approve', { workspace_id: workspaceId }), 'POST') as Promise<Result<WorkspaceActionAnswer>>
}

/** `POST /v1/admin/workspaces/{workspace_id}/deny` (admin): the reason is shown to the person. */
export async function denyWorkspace(workspaceId: string, reason: string): Promise<Result<WorkspaceActionAnswer>> {
  return writeTo(route('/v1/admin/workspaces/{workspace_id}/deny', { workspace_id: workspaceId }), 'POST', { reason }) as Promise<Result<WorkspaceActionAnswer>>
}

/** `POST /v1/admin/workspaces/{workspace_id}/retry` (admin): from failed or needs_owner, publishes again. */
export async function retryWorkspace(workspaceId: string): Promise<Result<WorkspaceActionAnswer>> {
  return writeTo(route('/v1/admin/workspaces/{workspace_id}/retry', { workspace_id: workspaceId }), 'POST') as Promise<Result<WorkspaceActionAnswer>>
}

/** `PUT /v1/admin/workspaces/{workspace_id}/limits` (admin): the ceiling, `{max_active}` and nothing else. */
export async function setWorkspaceCeiling(workspaceId: string, maxActive: number): Promise<Result<WorkspaceLimitsAnswer>> {
  return writeTo(route('/v1/admin/workspaces/{workspace_id}/limits', { workspace_id: workspaceId }), 'PUT', { max_active: maxActive }) as Promise<
    Result<WorkspaceLimitsAnswer>
  >
}

// Workspace kinds (#847, lanes WK-RECORD and WK-UI-DIALOG; docs/design/workspace-kinds.md §4.2.2). An admin files a
// personal, group or service workspace and follows its job. The routes are routes/people.py's; a record is addressed
// by its `w-` id, and the list's filters are a kind and a state, so no URL names an email.

/** A record as `people._admin_view` serves it, with an absent `kind` read as `personal` (`workspaces.kind_of`). */
export function normAdminWorkspace(raw: unknown): AdminWorkspaceView {
  const rec = (typeof raw === 'object' && raw !== null ? raw : {}) as AdminWorkspaceView
  return { ...rec, kind: rec.kind ?? 'personal' }
}

/**
 * `POST /v1/admin/workspaces` (admin): file a workspace of any kind. 202 when this call made the record (already
 * `approved` when the requester may approve it), 200 when one already stood; the record either way. A refusal is the
 * API's code and sentence (design §4.2.3), drawn as it came.
 */
export async function createAdminWorkspace(body: NewWorkspaceBody & { first_admin?: string }): Promise<Result<FiledAdminWorkspace>> {
  const r = await writeTo(route('/v1/admin/workspaces'), 'POST', body)
  return r.status === 'ok' ? { ...r, data: normAdminWorkspace(r.data) } : (r as Result<FiledAdminWorkspace>)
}

/**
 * WHAT `first_admin` CAME TO (WORKSPACE-ADMIN-FIELDS, part of #2157; config-e.html S6c): the person named on Add a new
 * workspace, granted `admin` in that workspace alone through the role override (docs/design/workspace-roles.md §8).
 * `member: false` is someone not yet in the group: the role is stored and takes effect once they are in it, because
 * membership stays the group's. `granted: false` says why in `reason` (`workspace_exists`, or a refusal's code).
 */
export interface FirstAdminAnswer {
  email: string
  role: 'admin'
  granted: boolean
  /** Whether the group holds them now; null when the directory did not answer. Absent when nothing was granted. */
  member?: boolean | null
  reason?: string
}

/** A filed record as `POST /v1/admin/workspaces` answers it: the record, and `first_admin` when one was named. */
export type FiledAdminWorkspace = AdminWorkspaceView & { first_admin?: FirstAdminAnswer | null }

/**
 * Said on `window` once a workspace is renamed, with its tenant id as `detail`, so the shell reads its own name again
 * (`/v1/tenants/me`) and the switcher and the headers draw the new one without a reload.
 */
export const WORKSPACE_RENAMED_EVENT = 'swarm:workspace-renamed'

/** `PUT /v1/workspaces/{tenant_id}/name`'s answer: the display name changed, the id never does. */
export interface WorkspaceRenameAnswer {
  tenant_id: string
  display_name: string
  previous_display_name: string | null
  changed: boolean
}

/**
 * `PUT /v1/workspaces/{tenant_id}/name` (WORKSPACE-ADMIN-FIELDS, S6a): rename a workspace -- its display name, never
 * its id. Its admins and platform admins only; anyone else is 403 `role_admin_required` with nothing written, and a
 * name the API refuses (empty, over 60 characters, a control or invisible character) is its 422, drawn as it came.
 */
export async function renameWorkspace(tenantId: string, displayName: string): Promise<Result<WorkspaceRenameAnswer>> {
  const r = (await writeTo(route('/v1/workspaces/{tenant_id}/name', { tenant_id: tenantId }), 'PUT', { display_name: displayName })) as Result<WorkspaceRenameAnswer>
  if (r.status === 'ok' && r.data.changed) globalThis.dispatchEvent?.(new CustomEvent(WORKSPACE_RENAMED_EVENT, { detail: tenantId }))
  return r
}

/** `GET /v1/admin/workspaces/{workspace_id}` (admin): one record, for the dialog's progress view. */
export async function loadAdminWorkspace(workspaceId: string): Promise<Result<AdminWorkspaceView>> {
  return readAs(route('/v1/admin/workspaces/{workspace_id}', { workspace_id: workspaceId }), (raw) =>
    normAdminWorkspace((raw as { workspace?: unknown } | null)?.workspace),
  )
}

/** `GET /v1/admin/workspaces?kind=&state=` (admin): records, newest first, at most 200 -- Admin › Tenants' "Being created". */
export async function loadAdminWorkspaces(filter: { kind?: WorkspaceKind; state?: Exclude<WorkspaceState, 'none'> } = {}): Promise<Result<AdminWorkspaceView[]>> {
  const q = new URLSearchParams()
  if (filter.kind !== undefined) q.set('kind', filter.kind)
  if (filter.state !== undefined) q.set('state', filter.state)
  return readAs(route('/v1/admin/workspaces', {}, q.toString() === '' ? undefined : q), (raw) => {
    const rows = (raw as { workspaces?: unknown } | null)?.workspaces
    return Array.isArray(rows) ? rows.map(normAdminWorkspace) : []
  })
}

// The QA identity (lane QI-UI; docs/design/qa-identity.md §4.3, §4.6; part of #1649). The routes are
// swarm_api/routes/qa_identity.py's. An identity is a NAME from the catalogue's closed list (`console`), and a grant is
// addressed by workspace id: no URL or body here names an account, a host or a credential (invariant 10). The fixture
// build answers each as not served, so the Submit option is never offered there.

/** `GET /v1/qa-identities/mine` (any member): whether THIS workspace may name each identity. An unshaped answer is none. */
export async function loadQaIdentityMine(): Promise<Result<QaIdentityMine>> {
  return readAs(route('/v1/qa-identities/mine'), (raw) => {
    const rows = (raw as { identities?: unknown } | null)?.identities
    return {
      identities: Array.isArray(rows)
        ? rows
            .filter((r): r is Record<string, unknown> => typeof r === 'object' && r !== null && typeof (r as { identity?: unknown }).identity === 'string')
            .map((r) => ({ identity: r.identity as string, configured: r.configured === true, granted: r.granted === true }))
        : [],
    }
  })
}

/** `GET /v1/admin/qa-identities` (admin): each identity, its host and account, and every grant, revoked ones kept. */
export async function loadQaIdentities(): Promise<Result<QaIdentityList>> {
  return readAs(route('/v1/admin/qa-identities'), (raw) => {
    const rows = (raw as { identities?: unknown } | null)?.identities
    return {
      identities: Array.isArray(rows)
        ? rows.map((r) => {
            const e = (typeof r === 'object' && r !== null ? r : {}) as QaIdentityList['identities'][number]
            return { ...e, grants: Array.isArray(e.grants) ? e.grants : [] }
          })
        : [],
    }
  })
}

/** `PUT /v1/admin/qa-identities/{identity}/grants/{tenant_id}` (admin): grant, with a reason of 1-200 characters. */
export async function grantQaIdentity(identity: string, tenantId: string, reason: string): Promise<Result<QaIdentityGrantDoc>> {
  return writeTo(route('/v1/admin/qa-identities/{identity}/grants/{tenant_id}', { identity, tenant_id: tenantId }), 'PUT', { reason }) as Promise<
    Result<QaIdentityGrantDoc>
  >
}

/** `DELETE /v1/admin/qa-identities/{identity}/grants/{tenant_id}` (admin): revoke; the API keeps the document, `revoked`. */
export async function revokeQaIdentity(identity: string, tenantId: string): Promise<Result<QaIdentityGrantDoc>> {
  return writeTo(route('/v1/admin/qa-identities/{identity}/grants/{tenant_id}', { identity, tenant_id: tenantId }), 'DELETE') as Promise<
    Result<QaIdentityGrantDoc>
  >
}

/** `PUT /v1/admin/people/{workspace_id}/loan` (admin): lend an account to the person (`lend: true`) or reclaim it. */
export async function setWorkspaceLoan(workspaceId: string, accountId: string, lend: boolean): Promise<Result<WorkspaceLoanAnswer>> {
  return writeTo(route('/v1/admin/people/{workspace_id}/loan', { workspace_id: workspaceId }), 'PUT', { account_id: accountId, lend }) as Promise<
    Result<WorkspaceLoanAnswer>
  >
}

/**
 * `POST /v1/admin/people/{workspace_id}/loan/decline` (admin): close the person's open loan
 * request without lending. The reason is optional and shown to them; none sends `{}`.
 */
export async function declineLoanRequest(workspaceId: string, reason: string): Promise<Result<LoanDeclineAnswer>> {
  const text = reason.trim()
  return writeTo(route('/v1/admin/people/{workspace_id}/loan/decline', { workspace_id: workspaceId }), 'POST', text === '' ? {} : { reason: text }) as Promise<
    Result<LoanDeclineAnswer>
  >
}

/** `PUT /v1/admin/admins/{email}` (admin): grant admin. Idempotent (`changed: false`). */
export async function grantAdmin(email: string): Promise<Result<AdminGrantAnswer>> {
  return writeTo(route('/v1/admin/admins/{email}', { email }), 'PUT') as Promise<Result<AdminGrantAnswer>>
}

/** `DELETE /v1/admin/admins/{email}` (admin): refused for the owner and for the last admin, with the API's message. */
export async function removeAdmin(email: string): Promise<Result<AdminRemoveAnswer>> {
  return writeTo(route('/v1/admin/admins/{email}', { email }), 'DELETE') as Promise<Result<AdminRemoveAnswer>>
}

// ---------------------------------------------------------------------------
// Workspace roles (docs/design/workspace-roles.md §4.3, lane ROLES-UI-PEOPLE;
// part of #1518).
//
// The routes are swarm_api/routes/roles.py. THE EMAIL IS IN THE BODY, never
// the path, so an address lands in no request log -- `DELETE` included. The
// workspace's own routes act in the tenant `X-Swarm-Tenant` selects; the
// admin's, under the admin prefix (Admin › People), name a group workspace in the
// path and act without switching to it. Every write carries `via: "console"`,
// which the server shows beside it and never acts on (a platform admin's is
// recorded `platform_admin` whatever it says). The fixture build answers each
// as not served.
// ---------------------------------------------------------------------------

function isObj(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}

function str(v: unknown): string {
  return typeof v === 'string' ? v : ''
}

function strOrNull(v: unknown): string | null {
  return typeof v === 'string' && v !== '' ? v : null
}

function normOverride(raw: unknown): WorkspaceRoleOverride | null {
  if (!isObj(raw) || str(raw.email) === '') return null
  return {
    email: str(raw.email),
    role: str(raw.role),
    source: str(raw.source) || 'override',
    granted_by: str(raw.granted_by),
    granted_at: str(raw.granted_at),
    granted_via: str(raw.granted_via),
    reason: str(raw.reason),
  }
}

function normEvent(raw: unknown): WorkspaceRoleEvent | null {
  if (!isObj(raw) || str(raw.action) === '') return null
  const target = strOrNull(raw.target_email)
  return {
    action: str(raw.action),
    by: str(raw.by),
    at: str(raw.at),
    ...(target === null ? {} : { target_email: target }),
    detail: isObj(raw.detail) ? raw.detail : {},
  }
}

function normSummary(raw: unknown): RoleSummary | null {
  return isObj(raw) && str(raw.name) !== '' ? { name: str(raw.name), summary: str(raw.summary) } : null
}

function list<T>(raw: unknown, norm: (v: unknown) => T | null): T[] {
  return (Array.isArray(raw) ? raw : []).map(norm).filter((v): v is T => v !== null)
}

/** A workspace's roster, as either the workspace's or the admin's route serves it. */
export function normWorkspaceRoster(raw: unknown): WorkspaceRoster {
  const r = isObj(raw) ? raw : {}
  return {
    tenant_id: str(r.tenant_id),
    // Only an explicit `user` is a personal workspace: the fixed-admin line is
    // drawn on the server's word, never on a guess from the id.
    kind: r.kind === 'user' ? 'user' : 'group',
    default_role: strOrNull(r.default_role),
    default_source: strOrNull(r.default_source),
    people: list(r.people, normOverride),
    roles: list(r.roles, normSummary),
    audit: list(r.audit, normEvent),
  }
}

export function normWorkspaceRoles(raw: unknown): WorkspaceRoles {
  const r = isObj(raw) ? raw : {}
  const me = isObj(r.me) ? r.me : {}
  const mine: WorkspaceRoleMe = { email: str(me.email), role: str(me.role), source: str(me.source), via: str(me.via) }
  if (strOrNull(me.granted_by) !== null) mine.granted_by = str(me.granted_by)
  if (strOrNull(me.granted_at) !== null) mine.granted_at = str(me.granted_at)
  // Only an explicit true manages: an unread flag is never offered controls.
  return { ...normWorkspaceRoster(raw), me: mine, can_manage: r.can_manage === true }
}

/** `GET /v1/workspace/roles`: the selected workspace's default, overrides, roles and events, and the caller's own role (every role, Q7). */
export async function loadWorkspaceRoles(): Promise<Result<WorkspaceRoles>> {
  return readAs(route('/v1/workspace/roles'), normWorkspaceRoles)
}

/** `PUT /v1/workspace/roles/person` (admin): give one person their own role. */
export async function setWorkspacePersonRole(email: string, role: string, reason = ''): Promise<Result<PersonRoleChange>> {
  return writeTo(route('/v1/workspace/roles/person'), 'PUT', roleBody({ email: email.trim(), role }, reason)) as Promise<Result<PersonRoleChange>>
}

/** `DELETE /v1/workspace/roles/person` (admin): return one person to the default. */
export async function clearWorkspacePersonRole(email: string, reason = ''): Promise<Result<PersonRoleChange>> {
  return writeTo(route('/v1/workspace/roles/person'), 'DELETE', roleBody({ email: email.trim() }, reason)) as Promise<Result<PersonRoleChange>>
}

/** `PUT /v1/workspace/roles/default` (admin): `viewer` or `member`; `admin` is 409. */
export async function setWorkspaceDefaultRole(role: string, reason = ''): Promise<Result<DefaultRoleChange>> {
  return writeTo(route('/v1/workspace/roles/default'), 'PUT', roleBody({ role }, reason)) as Promise<Result<DefaultRoleChange>>
}

// ---------------------------------------------------------------------------
// Join requests (lane JOIN-UI-PERSON; docs/design/join-requests.md §4.2,
// §4.8; part of #2325). The routes are swarm_api/routes/join_requests.py.
//
// THE CODE TRAVELS IN A BODY, NEVER A PATH OR A QUERY (§4.2). The shared link
// carries it in the URL's fragment, which a browser never sends; the /join
// page reads it there and posts it here, so it never reaches a request log or
// a load balancer's access log. Every body is a closed model on the server (an
// extra field is 422), so none of these carries the roles' `via`.
// ---------------------------------------------------------------------------

const JOIN_STATES: readonly JoinRequestState[] = ['requested', 'approved', 'denied', 'withdrawn', 'expired']

export function normJoinRequest(raw: unknown): JoinRequestMine | null {
  if (!isObj(raw) || str(raw.ref) === '') return null
  const state = JOIN_STATES.find((s) => s === raw.state)
  // A state this client does not know is dropped rather than drawn as one it does.
  if (state === undefined) return null
  const out: JoinRequestMine = {
    ref: str(raw.ref), state, display_name: str(raw.display_name),
    requested_at: str(raw.requested_at), expires_at: str(raw.expires_at),
  }
  if (strOrNull(raw.decided_at) !== null) out.decided_at = str(raw.decided_at)
  if (strOrNull(raw.reason) !== null) out.reason = str(raw.reason)
  return out
}

function normJoinLink(raw: unknown): JoinLinkState {
  const r = isObj(raw) ? raw : {}
  // Only an explicit true is on: an unread flag never claims a working link.
  const out: JoinLinkState = { on: r.on === true, set_by: str(r.set_by), set_at: str(r.set_at) }
  if (strOrNull(r.link) !== null) out.link = str(r.link)
  return out
}

function normJoinedMember(raw: unknown): JoinedMember | null {
  if (!isObj(raw) || str(raw.email) === '') return null
  return {
    email: str(raw.email), by: str(raw.by), at: str(raw.at), via: str(raw.via),
    group_changed: raw.group_changed === true,
    still_in_group: typeof raw.still_in_group === 'boolean' ? raw.still_in_group : null,
  }
}

function asResult<T>(r: Result<unknown>, norm: (raw: unknown) => T): Result<T> {
  if (r.status === 'ok' || r.status === 'stale') return { ...r, data: norm(r.data) }
  return r
}

/** `POST /v1/join/lookup {code}`: the workspace a working code names, by its display name; any code that does not work is the one 404 `join_link_unknown`. */
export async function lookupJoinCode(code: string): Promise<Result<JoinLookup>> {
  const r = await writeTo(route('/v1/join/lookup'), 'POST', { code })
  return asResult(r, (raw) => ({ display_name: str(isObj(raw) ? raw.display_name : undefined) }))
}

/** `POST /v1/join-requests {code, message?}`: 202 with the new request, or 200 with the person's open one there. */
export async function askToJoin(code: string, message = ''): Promise<Result<JoinRequestMine>> {
  const note = message.trim()
  const r = await writeTo(route('/v1/join-requests'), 'POST', note === '' ? { code } : { code, message: note })
  return asResult(r, (raw) => normJoinRequest(raw) ?? { ref: '', state: 'requested', display_name: '', requested_at: '', expires_at: '' })
}

/** `GET /v1/join-requests/mine`: the caller's own requests, newest first, every state. */
export async function loadMyJoinRequests(): Promise<Result<JoinRequestMine[]>> {
  return readAs(route('/v1/join-requests/mine'), (raw) => list(isObj(raw) ? raw.requests : undefined, normJoinRequest), (d) => d.length === 0)
}

/** `POST /v1/join-requests/{ref}/withdraw`: the requester's own open request. */
export async function withdrawJoinRequest(ref: string): Promise<Result<unknown>> {
  return writeTo(route('/v1/join-requests/{ref}/withdraw', { ref }), 'POST')
}

/** `GET /v1/workspace/join-link` (admin): whether the selected workspace's link is on. Never the code. */
export async function loadJoinLink(): Promise<Result<JoinLinkState>> {
  return readAs(route('/v1/workspace/join-link'), normJoinLink)
}

/** `PUT /v1/workspace/join-link {on, rotate}` (admin): `link` is in the answer only when a code was just made. */
export async function setJoinLink(on: boolean, rotate = false): Promise<Result<JoinLinkState>> {
  return asResult(await writeTo(route('/v1/workspace/join-link'), 'PUT', { on, rotate }), normJoinLink)
}

/** `GET /v1/workspace/members` (admin): who holds a platform membership here, by an approved request. */
export async function loadJoinedMembers(): Promise<Result<JoinedMembers>> {
  return readAs(route('/v1/workspace/members'), (raw) => {
    const r = isObj(raw) ? raw : {}
    return { platform_memberships: r.platform_memberships === true, members: list(r.members, normJoinedMember) }
  })
}

/** `DELETE /v1/workspace/members/person {email, reason?}` (admin): ends a platform membership; the email in the body. */
export async function removeJoinedMember(email: string, reason = ''): Promise<Result<JoinedMemberRemoval>> {
  const why = reason.trim()
  const body = why === '' ? { email: email.trim() } : { email: email.trim(), reason: why }
  return writeTo(route('/v1/workspace/members/person'), 'DELETE', body) as Promise<Result<JoinedMemberRemoval>>
}

/** `GET /v1/admin/workspaces/{tenant_id}/roles` (platform admin): any registered group workspace's roster, without selecting it. */
export async function loadAdminWorkspaceRoles(tenantId: string): Promise<Result<WorkspaceRoster>> {
  return readAs(route('/v1/admin/workspaces/{tenant_id}/roles', { tenant_id: tenantId }), normWorkspaceRoster)
}

/** One page's entries as `AdminWorkspaceRoles`: an entry without an id is dropped, and only an explicit `unavailable` withholds the roster. */
export function normAdminWorkspaceRolesPage(raw: unknown): AdminWorkspaceRolesPage {
  const r = isObj(raw) ? raw : {}
  const roles = r.roles
  const workspaces = (Array.isArray(r.workspaces) ? r.workspaces : []).flatMap((w): AdminWorkspaceRoles[] => {
    if (!isObj(w) || str(w.tenant_id) === '') return []
    const head = { tenant_id: str(w.tenant_id), group: str(w.group), registered_by: str(w.registered_by) }
    if (w.unavailable === true) return [{ ...head, roster: null, unavailable: { code: str(w.code), message: str(w.message) } }]
    // The page carries the role table once; each roster is read with it.
    return [{ ...head, roster: normWorkspaceRoster({ ...w, roles, audit: [] }), unavailable: null }]
  })
  return { workspaces, next_page_token: strOrNull(r.next_page_token) }
}

/**
 * `GET /v1/admin/workspaces/roles` (platform admin): every registered group
 * workspace's default and overrides, a page at a time -- one read per page,
 * never one per workspace. `pageToken` is the previous page's `next_page_token`.
 */
export async function loadAdminWorkspacesRoles(pageToken: string | null = null): Promise<Result<AdminWorkspaceRolesPage>> {
  const q = pageToken === null ? undefined : new URLSearchParams({ page_token: pageToken })
  return readAs(route('/v1/admin/workspaces/roles', {}, q), normAdminWorkspaceRolesPage, (d) => d.workspaces.length === 0)
}

/** `PUT /v1/admin/workspaces/{tenant_id}/roles/person` (platform admin): how a group workspace's first admin is named (Q2). */
export async function setAdminWorkspacePersonRole(tenantId: string, email: string, role: string, reason = ''): Promise<Result<PersonRoleChange>> {
  return writeTo(route('/v1/admin/workspaces/{tenant_id}/roles/person', { tenant_id: tenantId }), 'PUT', roleBody({ email: email.trim(), role }, reason)) as Promise<
    Result<PersonRoleChange>
  >
}

/** `DELETE /v1/admin/workspaces/{tenant_id}/roles/person` (platform admin). */
export async function clearAdminWorkspacePersonRole(tenantId: string, email: string, reason = ''): Promise<Result<PersonRoleChange>> {
  return writeTo(route('/v1/admin/workspaces/{tenant_id}/roles/person', { tenant_id: tenantId }), 'DELETE', roleBody({ email: email.trim() }, reason)) as Promise<
    Result<PersonRoleChange>
  >
}

/** `PUT /v1/admin/workspaces/{tenant_id}/roles/default` (platform admin). */
export async function setAdminWorkspaceDefaultRole(tenantId: string, role: string, reason = ''): Promise<Result<DefaultRoleChange>> {
  return writeTo(route('/v1/admin/workspaces/{tenant_id}/roles/default', { tenant_id: tenantId }), 'PUT', roleBody({ role }, reason)) as Promise<
    Result<DefaultRoleChange>
  >
}

/** `GET /v1/admin/roles/person?email=` (platform admin): every override one person holds, across the group workspaces. */
export async function loadAdminPersonRoles(email: string): Promise<Result<PersonRoles>> {
  const q = new URLSearchParams({ email: email.trim() })
  return readAs(route('/v1/admin/roles/person', {}, q), (raw): PersonRoles => {
    const r = isObj(raw) ? raw : {}
    const workspaces = (Array.isArray(r.workspaces) ? r.workspaces : []).flatMap((w): PersonWorkspaceRole[] => {
      if (!isObj(w) || str(w.tenant_id) === '') return []
      if (w.unavailable === true) return [{ tenant_id: str(w.tenant_id), group: str(w.group), unavailable: true }]
      const o = normOverride(w)
      return o === null ? [] : [{ ...o, tenant_id: str(w.tenant_id), group: str(w.group), ...(strOrNull(w.key) === null ? {} : { key: str(w.key) }) }]
    })
    return { email: str(r.email), workspaces }
  })
}

/** A role write's body: the reason only when one was given, and where it was made. */
function roleBody(fields: Record<string, string>, reason: string): Record<string, string> {
  const why = reason.trim()
  return { ...fields, ...(why === '' ? {} : { reason: why }), via: 'console' }
}

// ---------------------------------------------------------------------------
// The access API (#780, OB4), read and written by the Access page (OB8).
//
// The routes are swarm_api/routes/access.py. Each acts as the caller with
// THEIR OWN GitHub connection, inside the caller's tenant (`tenant_scope`);
// none answers a value. The fixture build serves `/v1/access` and
// `/v1/access/orgs` (QAFIX6-FIXTURE d) and answers the rest as not served.
// ---------------------------------------------------------------------------

/** `GET /v1/access`: the caller's connection, enabled owners and grants (no GitHub read). */
export async function loadAccess(): Promise<Result<AccessOverview>> {
  return readAs(route('/v1/access'), (raw) => raw as AccessOverview)
}

/**
 * The GitHub App's name as the console says it: the display name the API
 * serves as `app_name` (the deployment's GITHUB_APP_NAME), or, when it serves
 * none, "the SwarmCloud GitHub App". Never a hard-coded deployment's name
 * (#1647).
 */
export function githubAppName(served: unknown): string {
  const name = typeof served === 'string' ? served.trim() : ''
  return name !== '' ? name : 'the SwarmCloud GitHub App'
}

/** `GET /v1/access/orgs`: every owner the caller's connection reaches, each with its install state. */
export async function loadAccessOwners(): Promise<Result<AccessOwners>> {
  return readAs(route('/v1/access/orgs'), (raw) => raw as AccessOwners)
}

/** `POST /v1/access/orgs`: enable an owner the App is installed on. */
export async function enableAccessOwner(owner: string): Promise<Result<AccessEnableResponse>> {
  return writeTo(route('/v1/access/orgs'), 'POST', { owner }) as Promise<Result<AccessEnableResponse>>
}

/** `DELETE /v1/access/orgs/{owner}`: disable it, and delete every grant under it in one write. */
export async function disableAccessOwner(owner: string): Promise<Result<AccessDisableResponse>> {
  return writeTo(route('/v1/access/orgs/{owner}', { owner }), 'DELETE') as Promise<Result<AccessDisableResponse>>
}

/**
 * `GET /v1/access/orgs/{owner}/repositories?page=N&q=text`: ONE page of the
 * owner's installation, searched server-side (a substring of the name).
 */
export async function loadAccessRepositories(owner: string, page = 1, q = ''): Promise<Result<AccessRepositoryPage>> {
  const query = new URLSearchParams({ page: String(page) })
  if (q.trim() !== '') query.set('q', q.trim())
  return readAs(route('/v1/access/orgs/{owner}/repositories', { owner }, query), (raw) => raw as AccessRepositoryPage)
}

/** `PUT /v1/access/grants/{repo_id}`: grant read or write. The server reads the repository once, as the caller. */
export async function putAccessGrant(repoId: string, repository: string, mode: AccessMode): Promise<Result<AccessGrantResponse>> {
  return writeTo(route('/v1/access/grants/{repo_id}', { repo_id: repoId }), 'PUT', { repository, mode }) as Promise<Result<AccessGrantResponse>>
}

/** `DELETE /v1/access/grants/{repo_id}`: revoke the grant. */
export async function revokeAccessGrant(repoId: string): Promise<Result<AccessRevokeResponse>> {
  return writeTo(route('/v1/access/grants/{repo_id}', { repo_id: repoId }), 'DELETE') as Promise<Result<AccessRevokeResponse>>
}

/**
 * `POST /v1/access/grants/{repo_id}/verify`: run clone, push and pull request
 * now, which only read. `['push_test']` is D6's opt-in write, on a write
 * grant only: one `swarmcloud/onboarding-check-<nonce>` branch created and
 * deleted.
 */
export async function verifyAccessGrant(repoId: string, checks?: AccessCheckName[]): Promise<Result<AccessVerifyResponse>> {
  return writeTo(route('/v1/access/grants/{repo_id}/verify', { repo_id: repoId }), 'POST', checks === undefined ? {} : { checks }) as Promise<
    Result<AccessVerifyResponse>
  >
}

/** `POST /v1/access/orgs/{owner}/install-request`: record that the person asked {owner}'s owners to install the App (ORG_APPROVAL_PENDING). */
export async function requestAccessInstall(owner: string): Promise<Result<AccessInstallRequestResponse>> {
  return writeTo(route('/v1/access/orgs/{owner}/install-request', { owner }), 'POST') as Promise<Result<AccessInstallRequestResponse>>
}

/** `GET /v1/access/members` (admin): every member's connection state and grants in the caller's tenant. */
export async function loadAccessMembers(): Promise<Result<AccessMembers>> {
  return readAs(route('/v1/access/members'), (raw) => raw as AccessMembers)
}

/**
 * THE REGISTRATION ID OF A TYPED `owner/repo`, for `PUT /v1/access/grants/{repo_id}`.
 *
 * A repository picked from a listing carries its `repo_id`, minted by the
 * server; a TYPED one has none, and the route is keyed on it. This is
 * `swarm_api/repositories.py::repo_id_for`, restated: `repo_` + the first 16
 * hex of sha256(tenant + `github.com/` + lower-cased owner/repo). A drift
 * cannot grant the wrong repository: the server recomputes it from the body's
 * `repository` and refuses a mismatch ("repo_id is not this tenant's id for
 * that repository"). `__tests__/access.test.tsx` pins it to a value the
 * Python recipe produced.
 */
export async function typedRepoId(tenantId: string, repository: string): Promise<string> {
  const bytes = new TextEncoder().encode(`${tenantId}github.com/${repository.toLowerCase()}`)
  const digest = await globalThis.crypto.subtle.digest('SHA-256', bytes)
  const hex = [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, '0')).join('')
  return `repo_${hex.slice(0, 16)}`
}

// ---------------------------------------------------------------------------
// Schedules and approvals (docs/schedules.md §7.1; the console is §6, lane S7)
// ---------------------------------------------------------------------------

/**
 * Today's reported spend of one schedule, in its own zone (§4.3). `partial`
 * says some attempt never reported a cost, so `reported_usd` is a floor: the
 * console draws it with `Mark kind="partial"`, never as the whole figure.
 */
export interface ScheduleSpendToday {
  day: string
  reported_usd: number
  unreported_attempts: number
  coverage: 'complete' | 'partial'
}

/** `last_firing`, the copy the list reads (§1.1). */
export interface ScheduleLastFiring {
  firing_id: string
  slot: string | null
  outcome: string | null
  work_ref?: unknown
  ended_at: string | null
}

/**
 * One `schedules/` document as `routes/schedules.py::schedule_to_api` serves
 * it: the stored fields, and `words`, `tier`, `spend_today` and
 * `pending_approvals` computed on read. Every map is the caller's or the
 * type's; the console reads and never writes one in this lane.
 */
export interface Schedule {
  schedule_id: string
  tenant_id: string
  name: string
  type: string
  scope: { mode: 'repos' | 'all' | 'platform'; repo_ids?: string[] }
  cron: string
  timezone: string
  params: Record<string, unknown>
  gate: { plan?: string; run?: string; merge?: string; approvers?: string; approval_ttl_hours?: number }
  budget: { per_run_usd?: number; per_day_usd?: number; max_concurrent?: number }
  policy: { overlap?: string; catch_up?: string; jitter?: boolean; dry_run?: boolean; max_consecutive_failures?: number }
  state: 'enabled' | 'paused' | 'auto_paused' | 'disabled'
  pause: { by?: string; at?: string; reason?: string | null; code?: string | null } | null
  owner: string
  created_by: string
  created_at: string
  updated_by?: string | null
  updated_at?: string | null
  next_run_at: string | null
  last_firing: ScheduleLastFiring | null
  consecutive_failures?: number
  revision: number
  /** The cron in words, or null when the tick's parser refused it. */
  words: string | null
  /** The risk tier (§4.1), or null when the type has left the catalogue. */
  tier: string | null
  spend_today: ScheduleSpendToday
  pending_approvals: number
  /** Served by `GET /v1/schedules/{id}` only (`_budget_use`); the list never carries it. */
  budget_use?: ScheduleBudgetUse | null
}

/**
 * Today's burn as the tick's budget gate counts it (§4.3), from
 * `routes/schedules.py::_budget_use`: every figure is the server's, so the
 * Budget tab's bar draws and computes none. `unreported_usd` is each attempt
 * that never reported a cost at `per_run_usd`; `live_usd` each live work item
 * at `per_run_usd`; `free_usd` what is left of `per_day_usd`, never below 0.
 */
export interface ScheduleBudgetUse {
  day: string
  per_day_usd: number
  per_run_usd: number
  reported_usd: number
  unreported_attempts: number
  unreported_usd: number
  live_firings: number
  live_items: number
  live_usd: number
  free_usd: number
}

/** One thing a firing created (§1.2). */
export interface FiringWork {
  kind: 'issue_run' | 'task' | 'workflow' | 'api_action'
  id: string
  repo_id?: string | null
  outcome?: string | null
}

/** One `schedule_firings/` document as `firing_to_api` serves it (§1.2). */
export interface ScheduleFiring {
  firing_id: string
  schedule_id: string
  type: string
  slot: string | null
  fired_at: string | null
  trigger: 'cron' | 'catch_up' | 'run_now' | 'queued'
  state: string
  work: FiringWork[]
  skip: { code: string; detail?: unknown } | null
  dry_run: unknown
  approval_id: string | null
  outcome: string | null
  cost: { reported_usd?: number; unreported_attempts?: number } | null
  ended_at: string | null
  /**
   * Issue runs refused at planning for what their issue asks
   * (`issueruns.PLANNING_REFUSALS`): recorded here and not counted in
   * `outcome`, which stays `succeeded` for them. Absent when there were none.
   */
  refused_runs?: FiringRefusedRun[]
  /**
   * What the executor selected and why (`Firing.explain`, #1133): for the
   * issue types, `describe`'s answer. Read here only for `own_live`.
   */
  selection?: { own_live?: FiringOwnLive | null } & Record<string, unknown>
}

/**
 * A schedule's live runs against its OWN limit, never the tenant's (lane
 * SCHED): `bound` names the parameter, `max_live_runs` or `max_pending_plans`.
 */
export interface FiringOwnLive {
  running: number
  limit: number
  bound: string
}

/** The `NOTHING_STARTED` skip's detail (`schedulefire.nothing_started`): every field optional. */
export interface NothingStartedDetail {
  reason?: string
  bound?: string | null
  own_live?: FiringOwnLive | null
  passed_over_by_reason?: Record<string, number>
  failures?: { repository?: string; error?: string }[]
  start_failures?: { issue?: string; error?: string }[]
  truncated?: boolean
}

/** One `refused_runs` entry: the run, its issue, and the refusal's code. */
export interface FiringRefusedRun {
  run_id: string
  issue: string
  code: string
}

/** One `schedule_audit/` entry (§4.8). Never updated after it is written. */
export interface ScheduleAuditEntry {
  schedule_id: string
  action: string
  by: string
  at: string
  detail: Record<string, unknown>
}

/** One inbox item (§4.5): a stored approval, or a PLANNED run projected into it. */
export interface ApprovalItem {
  approval_id: string
  kind: string
  subject: { run_id?: string; issue?: string; schedule_id?: string | null; firing_id?: string | null } & Record<string, unknown>
  summary: string
  state: string
  requested_at: string | null
  expires_at: string | null
  projected?: boolean
  /**
   * The approvers' display names, index for index with `approvers`
   * (`people.add_names`, user-profile.md §4.4): served on the inbox rows and
   * the detail alike. null when `approvers` is a rule such as `members`; a
   * member who set no name is a null entry; absent from an older API.
   */
  approver_names?: (string | null)[] | null
}

/** `GET /v1/schedules`: the tenant's schedules, by name. */
export async function loadSchedules(): Promise<Result<{ schedules: Schedule[]; tenant_id: string }>> {
  if (USE_FIXTURES) return fixtureSchedules()
  // Never `empty`: the list draws its own Absent beside the built-in rows,
  // which exist whether or not the tenant has made a schedule (§8.3).
  return read(route('/v1/schedules'), () => false)
}

/**
 * The tenant's schedules in the fixture build (run-4 overview-automate-001):
 * one enabled and one paused, of two catalogue types at their fixed risk
 * tiers (scheduletypes.py). The built-in rows beside them are the fixture's
 * registered repositories, read by the page itself. No firing, no approval
 * waiting and no spend today, so nothing here is a figure another read has
 * to agree with.
 */
async function fixtureSchedules(): Promise<Result<{ schedules: Schedule[]; tenant_id: string }>> {
  await new Promise((r) => setTimeout(r, 110))
  noteFixtureProbe(route('/v1/schedules'), 110, true)
  return { status: 'ok', fetchedAt: Date.now(), data: { schedules: fixtureScheduleRows(), tenant_id: FIXTURE_TENANT } }
}

/**
 * `schedules.state_word`, first match wins, as the route serves it beside each
 * row (QA2-FIX-07, automate2-13: every State cell read "not served", the
 * paused row too). No approval waits in the fixture, so never
 * `waiting_on_approval`.
 */
function fixtureStateWord(s: Schedule): string {
  if (s.state !== 'enabled') return s.state === 'auto_paused' && s.pause?.code === 'RUN_OVER_BUDGET' ? 'paused_by_tripwire' : 'paused'
  if ((s.consecutive_failures ?? 0) > 0) return 'failing'
  return s.last_firing === null ? 'never_fired' : 'active'
}

function fixtureScheduleRows(): Schedule[] {
  const day = new Date(FIXTURE_NOW).toISOString().slice(0, 10)
  const at = (h: number) => new Date(FIXTURE_NOW + h * FIXTURE_HOUR).toISOString()
  const base = {
    tenant_id: FIXTURE_TENANT, params: {}, owner: 'alice@example.com', created_by: 'alice@example.com',
    last_firing: null, consecutive_failures: 0,
    spend_today: { day, reported_usd: 0, unreported_attempts: 0, coverage: 'complete' as const }, pending_approvals: 0,
  }
  const schedules: Schedule[] = [
    {
      ...base, schedule_id: 'sch_3f9a0c1e7b25', name: 'Plan open issues', type: 'issue-plan-only', tier: 'R1',
      scope: { mode: 'repos', repo_ids: [FIXTURE_REPOS[0].repo_id] }, cron: '0 6 * * 1-5', timezone: 'UTC', words: 'At 06:00, Monday to Friday',
      gate: { plan: 'approve' }, budget: { per_run_usd: 2, per_day_usd: 6, max_concurrent: 1 }, policy: { overlap: 'skip', catch_up: 'latest', dry_run: false },
      state: 'enabled', pause: null, created_at: fixtureAgo(12 * 24 * FIXTURE_HOUR), next_run_at: at(18), revision: 3,
    },
    {
      ...base, schedule_id: 'sch_8b2e6d4a1c90', name: 'Weekly cost report', type: 'cost-report', tier: 'R0',
      scope: { mode: 'all' }, cron: '0 8 * * 1', timezone: 'UTC', words: 'At 08:00 on Monday',
      gate: { run: 'auto' }, budget: { per_run_usd: 0.5 }, policy: { overlap: 'skip', catch_up: 'skip', dry_run: false },
      state: 'paused', pause: { by: 'alice@example.com', at: fixtureAgo(2 * 24 * FIXTURE_HOUR), reason: 'holiday week', code: null },
      created_at: fixtureAgo(30 * 24 * FIXTURE_HOUR), next_run_at: null, revision: 5,
    },
  ]
  return schedules.map((row) => ({ ...row, state_word: fixtureStateWord(row) }))
}

/**
 * One listed schedule and no firings, as the route serves a schedule that has
 * not fired (QA2-FIX-07, automate2-12: a row opened a 404 under its raw id).
 * Any other id is the route's own 404.
 */
async function fixtureSchedule(scheduleId: string): Promise<Result<{ schedule: Schedule; firings: ScheduleFiring[] }>> {
  const target = route('/v1/schedules/{id}', { id: scheduleId })
  await new Promise((r) => setTimeout(r, 90))
  const schedule = fixtureScheduleRows().find((s) => s.schedule_id === scheduleId)
  if (schedule === undefined) {
    noteFixtureProbe(target, 90, false)
    return { status: 'error', error: { kind: 'not_found', httpStatus: 404, code: 'not_found', message: `schedule '${scheduleId}' not found`, template: target.template } }
  }
  noteFixtureProbe(target, 90, true)
  return { status: 'ok', fetchedAt: Date.now(), data: { schedule, firings: [] } }
}

/** `GET /v1/schedules/{id}`: one schedule and its last 50 firings, newest first. */
export async function loadSchedule(scheduleId: string): Promise<Result<{ schedule: Schedule; firings: ScheduleFiring[] }>> {
  if (USE_FIXTURES) return fixtureSchedule(scheduleId)
  return read(route('/v1/schedules/{id}', { id: scheduleId }), () => false)
}

/** `GET /v1/schedules/{id}/audit`: the decisions, newest first (§4.8). */
export async function loadScheduleAudit(scheduleId: string): Promise<Result<{ audit: ScheduleAuditEntry[] }>> {
  return read(route('/v1/schedules/{id}/audit', { id: scheduleId }), (d) => d.audit.length === 0)
}

/**
 * `GET /v1/approvals`: the inbox, oldest first -- the Automate badge and
 * Overview's "Waiting on you" card. `empty` is a measured nothing-waiting.
 * The development fixtures serve no inbox, and say so by being empty rather
 * than by inventing items.
 */
export async function loadApprovals(
  options: { frame?: boolean; kind?: string; state?: 'pending' | 'all' | 'decided' | 'expired' } = {},
): Promise<Result<{ approvals: ApprovalItem[] }>> {
  if (USE_FIXTURES) return { status: 'empty', fetchedAt: Date.now() }
  // The frame reads this on every page. A 200 without an `approvals` list is
  // not a measured empty inbox, and passing it on as one would draw "nothing
  // waiting" over an answer nobody read; it is a failure, as an unreadable
  // body is. `kind` is the route's own filter (`?kind=`, §4.5).
  //
  // `state` is the route's too, and it DEFAULTS TO `pending` (QA2-FIX-07,
  // automate2-01): the Approvals list asks `all` so its Decided and Expired
  // cards can be drawn from what the server holds; the frame's badge and the
  // Schedules card keep the inbox by sending none.
  const query = new URLSearchParams()
  if (options.kind !== undefined && options.kind !== '') query.set('kind', options.kind)
  if (options.state !== undefined && options.state !== 'pending') query.set('state', options.state)
  const r = await read<{ approvals: ApprovalItem[] }>(
    route('/v1/approvals', {}, query.toString() === '' ? undefined : query),
    (d) => Array.isArray(d.approvals) && d.approvals.length === 0,
    { frame: options.frame === true },
  )
  if (r.status === 'ok' && !Array.isArray(r.data.approvals)) {
    return {
      status: 'error',
      error: { kind: 'server_error', httpStatus: 200, code: null, message: 'The approvals answer carried no list of approvals.' },
    }
  }
  return r
}

/**
 * `GET /v1/approvals/{id}`: one item, with what it is decided on -- the run
 * for a projected plan, and for a merge the head it was checked at and what
 * its requirements say. `digest` is what an approve sends back: the server
 * refuses one that no longer matches (409 `merge_changed`, `plan_changed`).
 */
export interface ApprovalDetail {
  approval: ApprovalItem & {
    digest?: unknown
    approvers?: unknown
    hold?: { code?: string; approvers?: string; matched?: string[]; reasons?: string[] } | null
    decided_by?: string | null
    /** The decider's display name: null when they set none or `decided_by` is not a person (`approval-expiry`). */
    decided_by_name?: string | null
    decided_at?: string | null
    reason?: string | null
  }
  run?: { id?: string; state?: string; plan?: { summary?: string; steps?: unknown[]; files?: string[] } | null } & Record<string, unknown>
  merge?: {
    green_sha?: string | null
    pull_request?: { number?: number; url?: string } | null
    requirements_met?: boolean | null
    requirements_unmet?: string[]
    files_checked_head?: string | null
  }
}

export async function loadApproval(itemId: string): Promise<Result<ApprovalDetail>> {
  const target = route('/v1/approvals/{id}', { id: itemId })
  if (USE_FIXTURES) return scheduleNotServed(target)
  return read(target, () => false)
}

/**
 * `POST /v1/approvals/{id}:approve` with the digest the person was shown.
 * A one-person tenant's own hold answers 403 `hold_approver_required` with
 * `detail.confirm`, the words to type; the retry sends them as `confirm`.
 */
export async function approveApproval(itemId: string, body: { digest: unknown; confirm?: string }): Promise<Result<unknown>> {
  const target = route('/v1/approvals/{id}:approve', { id: itemId })
  if (USE_FIXTURES) return scheduleNotServed(target)
  return write(target, 'POST', body)
}

/** `POST /v1/approvals/{id}:reject`: any member may, and a reason is required (§4.5). */
export async function rejectApproval(itemId: string, body: { reason: string }): Promise<Result<unknown>> {
  const target = route('/v1/approvals/{id}:reject', { id: itemId })
  if (USE_FIXTURES) return scheduleNotServed(target)
  return write(target, 'POST', body)
}

/**
 * One row of `GET /v1/admin/schedules` (§5.3, `_admin_row`): what an admin
 * sees of another tenant's schedule. Not its parameters, gate or spec --
 * those are the tenant's decisions.
 */
export interface AdminScheduleRow {
  schedule_id: string
  tenant_id: string
  name: string
  type: string
  tier: string | null
  cron: string
  timezone: string
  words: string | null
  next_run_at: string | null
  last_outcome: string | null
  last_firing_at: string | null
  spend_today: ScheduleSpendToday
  pending_approvals: number
  state: Schedule['state']
  pause: Schedule['pause']
  owner: string
}

/** The schedule tick's last hour (§2.10), as the admin list serves it. */
export interface ScheduleTickHealth {
  window_minutes: number
  ticks: number
  disabled: number
  errors: number
  truncated: number
  fired: number
  max_lateness_seconds: number | null
  last: Record<string, unknown> | null
}

export async function loadAdminSchedules(
  pageToken: string | null = null,
): Promise<Result<{ schedules: AdminScheduleRow[]; next_page_token: string | null; tick: ScheduleTickHealth }>> {
  const target = route('/v1/admin/schedules', {}, pageToken === null ? undefined : new URLSearchParams({ page_token: pageToken }))
  if (USE_FIXTURES) return scheduleNotServed(target)
  // Never `empty`: the screen draws its own Absent beside the tick's health.
  return read(target, () => false)
}

/** `POST /v1/admin/schedules/{id}:pause|disable|enable` (§5.3), audited as `admin:<email>`. */
export async function adminScheduleAction(
  scheduleId: string,
  action: 'pause' | 'disable' | 'enable',
  reason?: string,
): Promise<Result<{ schedule: AdminScheduleRow }>> {
  const target =
    action === 'pause'
      ? route('/v1/admin/schedules/{id}:pause', { id: scheduleId })
      : action === 'disable'
        ? route('/v1/admin/schedules/{id}:disable', { id: scheduleId })
        : route('/v1/admin/schedules/{id}:enable', { id: scheduleId })
  if (USE_FIXTURES) return scheduleNotServed(target)
  const body = action === 'enable' || reason === undefined || reason.trim() === '' ? undefined : { reason: reason.trim() }
  return (await write(target, 'POST', body)) as Result<{ schedule: AdminScheduleRow }>
}

/** `POST /v1/admin/tenants/{tenant}/schedules:pause`, typed with the tenant id (§2.11). */
export async function adminPauseTenantSchedules(tenantId: string, confirm: string, reason?: string): Promise<Result<unknown>> {
  const target = route('/v1/admin/tenants/{tenant}/schedules:pause', { tenant: tenantId })
  if (USE_FIXTURES) return scheduleNotServed(target)
  return write(target, 'POST', { confirm, ...(reason === undefined || reason.trim() === '' ? {} : { reason: reason.trim() }) })
}

/**
 * One row of `GET /v1/schedule-types` (§7.1, `scheduletypes.describe`): what
 * the create form offers. The gates and the parameter schema are the type's,
 * served, so the form never restates the catalogue.
 */
export interface ScheduleTypeRow {
  name: string
  description: string
  params_schema: { properties?: Record<string, ParamSchema>; required?: string[] }
  default_gate: Schedule['gate']
  floor_gate: Schedule['gate']
  min_interval_minutes: number
  budget_caps: Schedule['budget'] | null
  scopes: string[]
  /** The scopes this caller may create the type in; absent from an older API. */
  creatable_scopes?: string[]
  gate_notes?: string | null
  available: boolean
  disabled_reason: string | null
}

/** The subset of a JSON-schema property the form draws a field for. */
export interface ParamSchema {
  type?: string
  title?: string
  description?: string
  default?: unknown
  enum?: unknown[]
  minimum?: number
  maximum?: number
}

/** `POST /v1/schedules:preview` (§6.3), from the tick's own parser. */
export interface SchedulePreview {
  words: string
  /** ISO 8601 in the schedule's zone, with its offset. */
  next: string[]
  min_gap_minutes: number
  /** Why the type would refuse this cadence, or null. */
  refusal: unknown
}

/** The create body (§7.1). Never an image, a command or a resource spec (invariant 10). */
export interface ScheduleCreateBody {
  name: string
  type: string
  scope: Schedule['scope']
  cron: string
  timezone: string
  params?: Record<string, unknown>
  gate?: Schedule['gate']
  budget?: Schedule['budget']
  policy?: Schedule['policy']
  state?: 'enabled' | 'paused'
  client_request_id?: string
}

/** The edit body: what changed, and the revision it was read at. `type` is not editable. */
export type ScheduleEditBody = Partial<Omit<ScheduleCreateBody, 'type' | 'state' | 'client_request_id'>> & { revision: number }

/**
 * The catalogue rows of the two types the fixture's schedules are
 * (scheduletypes.py: their descriptions, gates, caps and parameters), so New
 * schedule and an edit can be walked in the fixture build (QA2-FIX-07,
 * automate2-12: it opened "serves no schedule routes" over a list it had just
 * served).
 */
function fixtureScheduleTypes(): ScheduleTypeRow[] {
  return [
    {
      name: 'issue-plan-only',
      description: 'Plan the ready open issues and wait: nothing executes until a person approves each plan.',
      params_schema: {
        properties: {
          max_pending_plans: { type: 'integer', title: 'Max Pending Plans', default: 5, minimum: 1, maximum: 20 },
          fix_rounds: { type: 'integer', title: 'Fix Rounds', default: 3, minimum: 1, maximum: 5 },
        },
      },
      default_gate: { run: 'auto', plan: 'approve', merge: 'approve' },
      floor_gate: { run: 'auto', plan: 'approve', merge: 'approve' },
      min_interval_minutes: 15,
      budget_caps: { per_run_usd: 3, per_day_usd: 30, max_concurrent: 5 },
      scopes: ['repos', 'all'], creatable_scopes: ['repos', 'all'], gate_notes: null,
      available: true, disabled_reason: null,
    },
    {
      name: 'cost-report',
      description: 'Sum reported cost over the window by schedule, repository, profile and person, with coverage.',
      params_schema: {
        properties: {
          window: { type: 'string', title: 'Window', default: 'day', enum: ['day', 'week'] },
          post_to_issue: { type: 'string', title: 'Post To Issue', default: null },
        },
      },
      default_gate: { run: 'auto', plan: 'auto', merge: 'off' },
      floor_gate: { run: 'auto', plan: 'auto', merge: 'off' },
      min_interval_minutes: 24 * 60,
      budget_caps: null,
      scopes: ['repos', 'all', 'platform'], creatable_scopes: fixtureIsAdmin ? ['repos', 'all', 'platform'] : ['repos', 'all'], gate_notes: null,
      available: true, disabled_reason: null,
    },
  ]
}

function scheduleNotServed<T>(target: ApiRoute): Result<T> {
  noteFixtureProbe(target, 0, false)
  return {
    status: 'error',
    error: { kind: 'not_found', httpStatus: 404, code: null, message: 'The fixture build serves no schedule routes.', template: target.template },
  }
}

/** A schedule write: the answer's `schedule`, or the refusal as served. */
async function scheduleWrite(target: ApiRoute, method: 'POST' | 'PATCH', body: unknown): Promise<Result<{ schedule: Schedule }>> {
  if (USE_FIXTURES) return scheduleNotServed(target)
  return (await write(target, method, body)) as Result<{ schedule: Schedule }>
}

/** `GET /v1/schedule-types`: the types this caller may create, with their gates. */
export async function loadScheduleTypes(): Promise<Result<{ types: ScheduleTypeRow[] }>> {
  if (USE_FIXTURES) {
    await new Promise((r) => setTimeout(r, 90))
    noteFixtureProbe(route('/v1/schedule-types'), 90, true)
    return { status: 'ok', fetchedAt: Date.now(), data: { types: fixtureScheduleTypes() } }
  }
  return read(route('/v1/schedule-types'), () => false)
}

/**
 * `POST /v1/schedules:preview`: the words, the next five and the type's
 * refusal. A bad expression or zone is a 422 with the parser's reason.
 */
export async function previewSchedule(body: { cron: string; timezone: string; type?: string }): Promise<Result<{ preview: SchedulePreview }>> {
  const target = route('/v1/schedules:preview')
  if (USE_FIXTURES) return scheduleNotServed(target)
  return (await write(target, 'POST', body)) as Result<{ preview: SchedulePreview }>
}

/** `POST /v1/schedules`: create. A repeated `client_request_id` returns the first one's schedule. */
export async function createSchedule(body: ScheduleCreateBody): Promise<Result<{ schedule: Schedule }>> {
  return scheduleWrite(route('/v1/schedules'), 'POST', body)
}

/** `PATCH /v1/schedules/{id}`: a stale revision is 409 `schedule_changed`; `gate.merge: auto` is 422 `use_merge_switch`. */
export async function updateSchedule(scheduleId: string, body: ScheduleEditBody): Promise<Result<{ schedule: Schedule }>> {
  return scheduleWrite(route('/v1/schedules/{id}', { id: scheduleId }), 'PATCH', body)
}

/** `POST /v1/schedules/{id}:pause` (§2.11): no new firings; live work continues. */
export async function pauseSchedule(scheduleId: string, body: { reason?: string } = {}): Promise<Result<{ schedule: Schedule }>> {
  return scheduleWrite(route('/v1/schedules/{id}:pause', { id: scheduleId }), 'POST', body)
}

/** `POST /v1/schedules/{id}:resume` (§1.3): from paused or auto-paused; a disabled one is an admin's (403 `admin_required`). */
export async function resumeSchedule(scheduleId: string): Promise<Result<{ schedule: Schedule }>> {
  return scheduleWrite(route('/v1/schedules/{id}:resume', { id: scheduleId }), 'POST', {})
}

/**
 * `POST /v1/schedules/{id}:run` (§7.1): a `run_now` firing under the gate's
 * `run` point, the overlap and the budget. `dry_run` computes what it would
 * create and runs no agent (§2.8). The answer is the firing as written.
 */
export async function runSchedule(scheduleId: string, body: { dry_run: boolean }): Promise<Result<{ firing: ScheduleFiring }>> {
  const target = route('/v1/schedules/{id}:run', { id: scheduleId })
  if (USE_FIXTURES) return scheduleNotServed(target)
  return (await write(target, 'POST', body)) as Result<{ firing: ScheduleFiring }>
}

/** `DELETE /v1/schedules/{id}?confirm=<name>` (§7.1): the name typed; firings and audit stay. */
export async function deleteSchedule(scheduleId: string, confirm: string): Promise<Result<{ deleted: string }>> {
  const target = route('/v1/schedules/{id}', { id: scheduleId }, new URLSearchParams({ confirm }))
  if (USE_FIXTURES) return scheduleNotServed(target)
  return (await write(target, 'DELETE')) as Result<{ deleted: string }>
}

/**
 * `POST /v1/schedules/{id}:merge-mode`, the SD3 switch (§4.2). The server
 * holds every rule -- the type's floor, the second person, the platform
 * admin, the one-person tenant's typed name -- and says which one refused.
 */
export async function setMergeMode(
  scheduleId: string,
  body: { mode: 'approve' | 'auto'; revision: number; confirm?: string },
): Promise<Result<{ schedule: Schedule }>> {
  return scheduleWrite(route('/v1/schedules/{id}:merge-mode', { id: scheduleId }), 'POST', body)
}

// ---------------------------------------------------------------------------
// The live browser (docs/design/live-browser.md §3.3, §4.2, §5.1-5.2, §5.7,
// §6, §6.1; lane L11). The routes are swarm-api's, built by lane L9 in
// parallel; this client is written against the design's interface and names
// each route once, here. NO FIXTURES: the fixture build has no browser to
// show and invents none, so every route answers as not served -- which the
// split reads as "this agent has no live browser" and draws no Browser tab.
// ---------------------------------------------------------------------------

/** The top frame's origin as the GATE read it from Chromium over CDP (§5.7), never from the page. */
export interface LiveOrigin {
  /** `https://github.com`: scheme, host and port, nothing else. */
  origin: string
  /** The host as Chromium reports it, punycode included (`xn--...`). */
  host: string
  /** The host decoded for display; differs from `host` only for punycode. */
  display_host?: string
  /** The registrable domain (public-suffix aware), emphasised in the strip. */
  registrable_domain: string
  /** `https:` or `http:`. The gate refuses control over `http:` (§5.7 item 4). */
  scheme: string
  /** True when the host is an IP literal. The gate refuses control (§5.7 item 4). */
  ip_literal?: boolean
  /** True while the page reports a certificate error; the gate refuses control (§5.4.4). */
  cert_error?: boolean
  /**
   * The top frame's path, redacted, no query or fragment (LB-R6): the
   * caption under the device reads it, the strip never does. Null or absent
   * when the gate reported none (a gate that predates it, or a path
   * swarm-api refused).
   */
  path?: string | null
}

/** `Task.human_wait` as the session read serves it (§4.1 step 3). */
export interface LiveHumanWait {
  /** Redacted by the worker before it was stored (§4.1). */
  reason: string
  /** The host the agent said it would ask to sign in to, or null when it named none. */
  expect: string | null
  requested_at: string
  deadline: string
}

/** One device preset, served from the gate's fixed list (§6.1). A caller never sends dimensions. */
export interface LivePreset {
  name: string
  width: number
  height: number
  scale: number
  mobile: boolean
}

/** The device the gate has applied (§6.1). */
export interface LiveDevice {
  preset: string
  orientation: 'portrait' | 'landscape'
  /**
   * The person in control works it by touch (§6.1): pointer events become
   * touch events at the gate. The preset's touch AND the controller's toggle
   * (LB-DEV-B), so a phone with touch turned off reports false.
   */
  touch: boolean
}

/**
 * One live browser session, as the CALLER may see it (`LiveRead.session`).
 * What the caller may do is the server's answer in `you`; the console only
 * draws it.
 */
export interface LiveSession {
  session_id: string
  /** `open` while the tunnel is up; `closed` once the attempt ended or was cancelled. */
  state: 'open' | 'closed'
  /** Why it closed (`cancelled`, `attempt_ended`, ...), when it has. */
  closed_reason?: string | null
  origin: LiveOrigin | null
  human_wait: LiveHumanWait | null
  /** The person in control, or null in view mode. One at a time, always (§4.2). */
  controller: { email: string; since: string } | null
  /**
   * Whether the submitter opened control to every member of the tenant (§5.1,
   * Q1). The task's setting, carried into each new attempt's session (QA12-11).
   */
  open_to_members: boolean
  /**
   * The device the gate applied, or null while nothing reports it to
   * swarm-api (LB-SEAM, #1030): the gate applies it, but no relay message or
   * ctl report carries it yet, so the API serves null rather than a guess.
   * `origin` is null for the same reason.
   */
  device: LiveDevice | null
  presets: LivePreset[]
  /** What THIS caller may do (§5.1). The server decides; the console only draws it. */
  you: {
    email: string
    /** May take control now: the submitter, or any member once control is opened (§5.1). */
    can_control: boolean
    /** May take control from someone who holds it: the submitter only (§5.1). */
    can_preempt: boolean
    /** May open control to every member, and close it again: the submitter only (§5.1). */
    can_open: boolean
    /** May switch the device, rotate it and turn touch on or off: the person in control only (§6.1, Q13; LB-DEV-B). */
    can_switch_device: boolean
  }
  /**
   * WHETHER THE AGENT IS USING THIS BROWSER (LIVE-UNUSED, #1030), as the
   * gate's CDP proxy reported it: when the agent first connected (null until
   * it has), how many connections it holds now (null while no gate has
   * reported it -- never read as "none"), and when the gate published its
   * first frame. Optional: an older API serves none of them.
   */
  agent_connected_at?: string | null
  agent_connections?: number | null
  first_frame_at?: string | null
}

/**
 * `GET /v1/tasks/{task_id}/live/session`. `live_browser` is the task's
 * profile's catalogue property (contract request 71, LB-D), never inferred
 * here from a profile name; `session` is null until the gate's tunnel is up
 * (a LEASED or STARTING attempt, or a browser being started for a person).
 *
 * A TASK WITH NO LIVE BROWSER IS A 200, NOT A 404. The split asks this of
 * every running agent it opens; a 404 for each ordinary one would put a
 * failing route in the data-source strip on every open. A 404 is what the
 * route answers for a task the caller cannot see at all -- another tenant's
 * answers exactly as a missing one (§5.2 step 2) -- and the split reads it,
 * like any failure, as "no Browser tab".
 */
export interface LiveRead {
  live_browser: boolean
  session: LiveSession | null
}

export async function loadLiveSession(taskId: string): Promise<Result<LiveRead>> {
  const target = route('/v1/tasks/{id}/live/session', { id: taskId })
  if (USE_FIXTURES) {
    // ONE FIXTURE AGENT HAS A LIVE BROWSER: task_b40e5a1c, the Browser QA
    // agent on `claude-code-browser` (QAFIX2-QABROWSER, owner decision
    // 2026-10-21). Its session is shaped as swarm-api's `_view` serves an
    // open one: id `live_<attempt id>`, the gate's preset list, the agent
    // driving with nobody in control, and the agent connected, so the strip
    // reads live rather than "isn't using this browser". Every other task is
    // answered as the API answers a task with none, so opening a fixture
    // agent records no failed route. The viewer ticket is still not served
    // (`mintLiveTicket`): the fixture build has no gate to draw frames from.
    noteFixtureProbe(target, 0, true)
    if (taskId !== 'task_b40e5a1c') {
      return { status: 'ok', fetchedAt: Date.now(), data: { live_browser: false, session: null } }
    }
    const at = (minsAgo: number) => new Date(FIXTURE_NOW - minsAgo * 60_000).toISOString()
    return {
      status: 'ok',
      fetchedAt: Date.now(),
      data: {
        live_browser: true,
        session: {
          session_id: 'live_att_1',
          state: 'open',
          closed_reason: null,
          origin: { origin: 'https://example.com', host: 'example.com', registrable_domain: 'example.com', scheme: 'https:' },
          human_wait: null,
          controller: null,
          open_to_members: false,
          device: { preset: 'desktop', orientation: 'landscape', touch: false },
          // swarm-api `LIVE_PRESETS`, the gate's fixed list (§6.1).
          presets: [
            { name: 'desktop', width: 1440, height: 900, scale: 1, mobile: false },
            { name: 'iphone-15', width: 393, height: 852, scale: 3, mobile: true },
            { name: 'phone-390', width: 390, height: 844, scale: 3, mobile: true },
            { name: 'phone-380', width: 380, height: 800, scale: 3, mobile: true },
            { name: 'pixel-8', width: 412, height: 915, scale: 2.625, mobile: true },
            { name: 'iphone-se', width: 375, height: 667, scale: 2, mobile: true },
            { name: 'ipad-7', width: 810, height: 1080, scale: 2, mobile: true },
          ],
          // The fixture identity (`fixtureMe`) submitted it, so may take
          // control and open it to members; nobody holds control to switch
          // the device or to be preempted.
          you: { email: 'alice@example.com', can_control: true, can_preempt: false, can_open: true, can_switch_device: false },
          agent_connected_at: at(5),
          agent_connections: 1,
          first_frame_at: at(5),
        },
      },
    }
  }
  return read<LiveRead>(target, () => false)
}

/** A step of a browser agent's start-up: lane STARTUP-API's `startup_progress` steps, in order. */
export type StartupStepName =
  | 'pod_scheduled'
  | 'image_pulling'
  | 'image_pulled'
  | 'container_started'
  | 'browser_launching'
  | 'browser_ready'
  | 'first_frame'

/**
 * `GET /v1/tasks/{task_id}/startup` (lane STARTUP-API, owner request
 * 2026-10-13): the CURRENT attempt's start-up, oldest step first --
 * `lease_acquired`, `dispatched`, `starting`, `running` and every
 * `startup_progress` step. `estimate` is the median seconds from `dispatched`
 * to each step over the profile's last 20 completed start-ups; `{}` with
 * `sample_size` 0 when there is no history, and the bar then shows no time.
 */
export interface StartupRead {
  attempt_id: string | null
  generation: number | null
  steps: { step: string; at: string }[]
  estimate: Partial<Record<string, number>>
  sample_size: number
  /** `first_frame` seen, or the task is no longer starting or running. */
  done: boolean
}

/**
 * A 404 IS "NOT DEPLOYED YET", not a failure of the agent: the route ships in
 * a parallel lane, and StartupProgress then draws the bar from the task's own
 * events (`useStartup`). No empty predicate: a start-up with no step yet is a
 * real answer.
 */
export async function loadStartup(taskId: string): Promise<Result<StartupRead>> {
  const target = route('/v1/tasks/{id}/startup', { id: taskId })
  // The fixture build has no browser agent to start: answered as the route
  // not being served, so the fixture exercises the events fallback.
  if (USE_FIXTURES) return liveNotServed(target)
  return read<StartupRead>(target, () => false)
}

/**
 * The viewer ticket's answer. `session_id` names the session the ticket is
 * bound to; the socket is opened on it (`liveSocketUrl`), not on the task.
 * `socket_path` is the server's own statement of that address.
 */
export interface LiveTicket {
  ticket: string
  session_id: string
  expires_in: number
  socket_path: string
}

/**
 * `POST /v1/tasks/{task_id}/live/ticket`: a one-use viewer ticket, 30 seconds
 * (§5.2 step 3), rate limited per person (§5.10). It travels ONLY as the
 * websocket's first message (`openLiveSocket`), never in a URL: load
 * balancer, IAP and Cloud Run request logs record URLs (§5.2).
 */
export async function mintLiveTicket(taskId: string): Promise<Result<LiveTicket>> {
  const target = route('/v1/tasks/{id}/live/ticket', { id: taskId })
  if (USE_FIXTURES) return liveNotServed(target)
  return (await write(target, 'POST')) as Result<LiveTicket>
}

/**
 * The live websocket's address: `wss://<console host>/v1/live/{session_id}/stream`
 * through the same IAP load balancer as every `/v1` read (§3.3 step 3);
 * swarm-api proxies it to the relay. It carries the session id AND NOTHING
 * ELSE -- no ticket, no tenant, no query.
 */
export function liveSocketUrl(sessionId: string): string {
  const loc = typeof window === 'undefined' ? null : window.location
  const scheme = loc !== null && loc.protocol === 'http:' ? 'ws' : 'wss'
  return `${scheme}://${loc?.host ?? ''}${route('/v1/live/{id}/stream', { id: sessionId }).url}`
}

/**
 * What swarm-api pushes down the live socket as TEXT frames, between the
 * relay's binary RFB frames: `{"type": "status", ...}`, sent when it changes
 * (swarm_api/routes/live.py `_status`). `controller` is the controller's
 * email, or null in view mode; `cause` says why, once `open` is false.
 */
export interface LiveStatus {
  open: boolean
  in_control: boolean
  controller: string | null
  cause: string | null
}

/** The members of a WebSocket the live channel drives: the ones a test fake has to carry. */
export interface RawLiveSocket {
  binaryType: string
  readonly readyState: number
  readonly protocol?: string
  onopen: ((ev: Event) => void) | null
  onmessage: ((ev: MessageEvent) => void) | null
  onclose: ((ev: CloseEvent) => void) | null
  onerror: ((ev: Event) => void) | null
  send(data: string | ArrayBufferLike | ArrayBufferView): void
  close(): void
}

/**
 * The relay's word that it refused a control message on this link
 * (`{"type": "refused", "reason": "control", "cause": ...}`, swarm_live
 * rendezvous.py `viewer_text`): the cause, such as `wrong_session` or
 * `session_closed`, or `unknown` from a relay that names none. Null for any
 * other frame. Without it, a console whose control the relay turned away drew
 * "you are in control" over a browser that ignored every move (LB-CURSOR2).
 */
export function liveRefusal(text: string): string | null {
  let msg: unknown
  try {
    msg = JSON.parse(text)
  } catch {
    return null
  }
  if (typeof msg !== 'object' || msg === null) return null
  const m = msg as Record<string, unknown>
  if (m.type !== 'refused' || m.reason !== 'control') return null
  return typeof m.cause === 'string' && /^[a-z_]{1,64}$/.test(m.cause) ? m.cause : 'unknown'
}

function liveStatus(text: string): LiveStatus | null {
  let msg: unknown
  try {
    msg = JSON.parse(text)
  } catch {
    return null
  }
  if (typeof msg !== 'object' || msg === null || (msg as { type?: unknown }).type !== 'status') return null
  const m = msg as Record<string, unknown>
  return {
    open: m.open === true,
    in_control: m.in_control === true,
    controller: typeof m.controller === 'string' ? m.controller : null,
    cause: typeof m.cause === 'string' ? m.cause : null,
  }
}

/**
 * THE LIVE SOCKET AS AN RFB CLIENT MAY SEE IT: binary frames only.
 *
 * swarm-api interleaves its own `{"type": "status"}` TEXT frames with the
 * relay's binary RFB stream on one socket. An RFB client reads every frame as
 * protocol bytes, so a status frame ahead of "RFB 003.008\n" is a handshake
 * that never completes: the Browser tab sat on "Connecting" forever on
 * 2026-10-12 (LB-STREAM, #1030). This channel owns the raw socket's handlers,
 * hands text frames to `onStatus` and forwards binary frames, unchanged and
 * in order, to its own `onmessage`.
 *
 * It is a WebSocket as noVNC 1.4.0 (core/websock.js `attach`) uses one: it
 * checks that `send`, `close`, `binaryType`, `onerror`, `onmessage`,
 * `onopen`, `protocol` and `readyState` exist, sets `binaryType` and the four
 * `on*` handlers, reads `readyState` (WebSocket's numbers), `protocol`, and
 * calls `send` and `close`. Nothing else. The live thumbnail's RFB client
 * (LiveThumb.tsx) reads the same channel, so the split lives in one place.
 *
 * The ticket goes as the FIRST message, before `onopen` runs (§5.2), so
 * whatever the consumer sends from its open handler follows it.
 */
export class LiveChannel {
  onopen: ((ev: Event) => void) | null = null
  onmessage: ((ev: { data: ArrayBuffer | Blob }) => void) | null = null
  onclose: ((ev: CloseEvent) => void) | null = null
  onerror: ((ev: Event) => void) | null = null

  constructor(
    private readonly raw: RawLiveSocket,
    ticket: string,
    onStatus?: (status: LiveStatus) => void,
    onRefused?: (cause: string) => void,
  ) {
    raw.binaryType = 'arraybuffer'
    raw.onopen = (ev) => {
      raw.send(JSON.stringify({ ticket }))
      this.onopen?.(ev)
    }
    raw.onmessage = (ev) => {
      if (typeof ev.data === 'string') {
        const status = liveStatus(ev.data)
        if (status !== null) onStatus?.(status)
        else {
          const refused = liveRefusal(ev.data)
          if (refused !== null) onRefused?.(refused)
        }
        return
      }
      this.onmessage?.({ data: ev.data as ArrayBuffer | Blob })
    }
    raw.onclose = (ev) => this.onclose?.(ev)
    raw.onerror = (ev) => this.onerror?.(ev)
  }

  get binaryType(): string {
    return this.raw.binaryType
  }

  set binaryType(value: string) {
    this.raw.binaryType = value
  }

  get readyState(): number {
    return this.raw.readyState
  }

  get protocol(): string {
    return this.raw.protocol ?? ''
  }

  send(data: ArrayBufferLike | ArrayBufferView): void {
    this.raw.send(data)
  }

  close(): void {
    this.raw.close()
  }
}

/** Open the session's live socket as a `LiveChannel`: the ticket first, then binary frames only. */
export function openLiveChannel(
  sessionId: string,
  ticket: string,
  onStatus?: (status: LiveStatus) => void,
  onRefused?: (cause: string) => void,
): LiveChannel {
  return new LiveChannel(new WebSocket(liveSocketUrl(sessionId)), ticket, onStatus, onRefused)
}

/**
 * Open the live channel (`LiveChannel`: the ticket as the FIRST message,
 * §5.2, then binary frames only) and hand it to `attach` IN THE SAME 'open'
 * HANDLER. No byte goes before the ticket; swarm-api answers nothing until it
 * has checked it, then proxies the RFB stream (and closes the socket if it
 * refuses). Its status frames go to `onStatus`, never to `attach`'s consumer.
 *
 * WHY `attach` RUNS INSIDE THE HANDLER, NOT AFTER THE PROMISE. noVNC only
 * reads frames once it has set the channel's `onmessage`, and an RFB server
 * speaks first (its ProtocolVersion). A frame that arrives before that is
 * dropped, and noVNC then waits forever for a handshake that already went
 * past. Constructing noVNC synchronously here, before control returns to the
 * event loop, means no frame can arrive in between; and the socket is still
 * open, so noVNC never sees an already-closed channel. If `attach` throws,
 * the socket is closed and the promise rejects, so the caller retries.
 */
export function openLiveSocket<T>(
  sessionId: string,
  ticket: string,
  attach: (channel: LiveChannel) => T,
  onStatus?: (status: LiveStatus) => void,
  onRefused?: (cause: string) => void,
): Promise<T> {
  return new Promise((resolve, reject) => {
    const channel = openLiveChannel(sessionId, ticket, onStatus, onRefused)
    channel.onerror = () => reject(new Error('The live connection did not open.'))
    channel.onclose = () => reject(new Error('The live connection closed before it opened.'))
    channel.onopen = () => {
      // The consumer sets its own handlers in `attach`; until it does, none of these is its.
      channel.onopen = channel.onerror = channel.onclose = null
      try {
        resolve(attach(channel))
      } catch (err) {
        channel.close()
        reject(err instanceof Error ? err : new Error('The live viewer could not attach to the connection.'))
      }
    }
  })
}

/**
 * `POST /v1/tasks/{task_id}/live/control`: take control (§4.2 step 1). When
 * the top frame's registrable domain is not the one the hand-off expected,
 * the person was asked twice and `confirmed_mismatch` names both domains, so
 * the audit records what they confirmed past (§5.7 item 2). `preempt` is the
 * submitter taking control from someone who holds it (§5.1).
 */
export async function takeLiveControl(
  taskId: string,
  body: { preempt?: boolean; confirmed_mismatch?: { expected: string; actual: string } } = {},
): Promise<Result<unknown>> {
  const target = route('/v1/tasks/{id}/live/control', { id: taskId })
  if (USE_FIXTURES) return liveNotServed(target)
  return write(target, 'POST', body)
}

/** `DELETE /v1/tasks/{task_id}/live/control`: return control (§4.2 step 3). */
export async function returnLiveControl(taskId: string): Promise<Result<unknown>> {
  const target = route('/v1/tasks/{id}/live/control', { id: taskId })
  if (USE_FIXTURES) return liveNotServed(target)
  return write(target, 'DELETE')
}

/**
 * `POST /v1/tasks/{task_id}/live/control/renew`: the controller's console
 * renews while it is open. Control ends when `renew_by` passes, 60 seconds
 * after the last renewal (§4.2), so a closed tab cannot hold it.
 */
export async function renewLiveControl(taskId: string): Promise<Result<unknown>> {
  const target = route('/v1/tasks/{id}/live/control/renew', { id: taskId })
  if (USE_FIXTURES) return liveNotServed(target)
  return write(target, 'POST')
}

/**
 * `POST /v1/tasks/{task_id}/live/device`: the person in control switches the
 * device (§6.1, Q13; LB-DEV-B). A preset BY NAME from `session.presets`, an
 * orientation (the preset's natural one when absent) and touch (left as it is
 * when absent) -- never a size (invariant 10). swarm-api answers the session
 * as it stands: the gate applies the choice and reports it, so the NEXT read
 * carries the new `device`. Anyone not in control gets 409 `not_controller`.
 */
export async function setLiveDevice(
  taskId: string,
  body: { preset: string; orientation?: LiveDevice['orientation']; touch?: boolean },
): Promise<Result<unknown>> {
  const target = route('/v1/tasks/{id}/live/device', { id: taskId })
  if (USE_FIXTURES) return liveNotServed(target)
  return write(target, 'POST', body)
}

/**
 * `POST /v1/tasks/{task_id}/live/open-to-members`: the submitter opens control
 * to every member (§5.1, audited). The TASK's setting (owner decision
 * 2026-10-13, QA12-11): swarm-api keeps it on the task and every later
 * attempt's session starts with it, until `closeLiveToMembers` turns it off.
 */
export async function openLiveToMembers(taskId: string): Promise<Result<unknown>> {
  const target = route('/v1/tasks/{id}/live/open-to-members', { id: taskId })
  if (USE_FIXTURES) return liveNotServed(target)
  return write(target, 'POST')
}

/**
 * `DELETE /v1/tasks/{task_id}/live/open-to-members`: the submitter turns member
 * control off again, for this attempt and the task's later ones (QA12-11,
 * audited). Whoever holds control now keeps it until they return it.
 */
export async function closeLiveToMembers(taskId: string): Promise<Result<unknown>> {
  const target = route('/v1/tasks/{id}/live/open-to-members', { id: taskId })
  if (USE_FIXTURES) return liveNotServed(target)
  return write(target, 'DELETE')
}

/**
 * One recorded segment of a live session (§7), as `GET .../live/recordings`
 * serves it: every field both views read (`RecordingSegmentView` in
 * `swarm_api.live`, held by tests/fixtures/live/recordings.json). The
 * Attempts tab's player reads `name`, `key`, `bytes` and `created_at`; the
 * theater's scrubber reads `seq`, `object` and `started_at`.
 */
export interface LiveRecording {
  attempt_id: string
  seq: number
  /** `rec-<13-digit seq>.live.webm`. */
  name: string
  /** The object's key, from which the tenant and task are checked. */
  key: string
  /** The same object name, under the name the theater reads it by. */
  object: string
  bytes: number | null
  created_at: string | null
  /** The object's write time: the END of the minute it holds (§6.2). */
  started_at: string | null
}

/** `GET /v1/tasks/{task_id}/live/recordings`: every attempt's segments, oldest first. */
export interface LiveRecordings {
  tenant_id: string
  task_id: string
  segments: LiveRecording[]
  truncated: boolean
}

/**
 * THE ONE READ OF THE RECORDINGS ROUTE (lane LB-UX2a). The Attempts tab's
 * player (Recordings.tsx) and the theater's replay each had their own, against
 * two shapes, which is how the route came to serve both. The server sends the
 * same list as `segments` and as `recordings`; this reads `segments`, and
 * `recordings` only from a server that sends nothing else.
 *
 * NO FIXTURE BRANCH, like `loadLiveActions`: the fixture build serves no live
 * session, so no theater opens there, and the Attempts tab's panel decides
 * for itself what the fixture build shows. What each view may PLAY is still
 * its own tenant check (`segmentsOfTenant`, `ownRecording`).
 */
export async function loadLiveRecordings(taskId: string): Promise<Result<LiveRecordings>> {
  const r = await read<{ tenant_id?: unknown; task_id?: unknown; segments?: unknown; recordings?: unknown; truncated?: unknown }>(
    route('/v1/tasks/{id}/live/recordings', { id: taskId }),
    () => false,
  )
  if (r.status !== 'ok' && r.status !== 'stale') return r as Result<LiveRecordings>
  const d = r.data
  const list = Array.isArray(d.segments) ? d.segments : Array.isArray(d.recordings) ? d.recordings : []
  const data: LiveRecordings = {
    tenant_id: typeof d.tenant_id === 'string' ? d.tenant_id : '',
    task_id: typeof d.task_id === 'string' ? d.task_id : '',
    segments: list as LiveRecording[],
    truncated: d.truncated === true,
  }
  // An empty list is an answer, not a different state: each view draws its own zero.
  return { ...r, data }
}

function liveNotServed<T>(target: ApiRoute): Result<T> {
  noteFixtureProbe(target, 0, false)
  return {
    status: 'error',
    error: { kind: 'not_found', httpStatus: 404, code: null, message: 'The fixture build serves no live browser.' },
  }
}

// ---------------------------------------------------------------------------
// ADMIN › SETUP AND HEALTH (DAYONE-SETUP-UI-1; part of #2688; plan §5.1-§5.4).
//
// The client of swarm-api's Admin › Setup routes (SETUP-API-1,
// `apps/swarm-api/swarm_api/routes/setup.py`). Every route is a platform
// admin's, so a 403 is information, as every Admin read here treats it. The
// shapes are the routes' own; each read is normalised once, here, so a panel
// never guards a field the server may leave out. No answer of these routes
// carries a secret (the conversion answers `{id, slug}` only, a link's token
// is in its URL and nowhere else), and nothing here reads one.
// ---------------------------------------------------------------------------

/** The engine document's `verify` field (D26): the console's verify row. Times are epoch seconds, the engine's clock. */
export interface SetupVerify {
  check: string | null
  done: number
  total: number
  started: number | null
  expected_end: number | null
  deadline: number | null
  /** `passed`, `failed`, or null while verify runs. */
  result: string | null
}

/** `stages[n]` of the engine document: `{state, at, by}`, or null for a stage not started. */
export interface SetupStage {
  state: string
  at: string | null
  by: string | null
}

/** The engine document `swarm-install/<env>`: the CLI's, read here and never written. */
export interface SetupEngine {
  release: string | null
  hostname: string | null
  stages: Array<SetupStage | null>
  lease: { holder: string | null; heartbeat: string | null } | null
  verify: SetupVerify | null
  hand_off_at: string | null
  ready_at: string | null
  /**
   * The first team's group stage 8 created (`state.outputs["workspace_groups"]`,
   * N20): the team form starts from it. Null before stage 8, and for one person.
   */
  workspace_groups: { team: string } | null
}

/** `console/github_app`, without its state hash (the route drops it). */
export interface SetupGitHubApp {
  /** A manifest was issued, or the App exists. */
  started: boolean
  id: string | null
  slug: string | null
  installed_on: string | null
  secrets_written_at: string | null
  self_check: SetupSelfCheck | null
}

/** The sign-in client's three states as `GET /v1/admin/setup` serves them (issue #2964). */
export type SetupSignInClientState = 'waiting' | 'saved' | 'applied'

/**
 * `sign_in_client` (issue #2964, owner's pick 2026-11-01): the Desktop OAuth
 * client `sc` signs in with, which no Google API creates since the IAP OAuth
 * Admin API shut down on 2026-03-19. `waiting`: nothing saved yet; `saved`:
 * the id and the secret are stored; `applied`: IAP's programmatic-clients
 * list holds the id (the infra pass ran). `links` are the deployment's own
 * Google Cloud console pages, built by swarm-api from its project id; a link
 * that is not a console.cloud.google.com page is null. The secret is never
 * served.
 */
export interface SetupSignInClient {
  state: SetupSignInClientState
  client_id: string | null
  saved_at: string | null
  saved_by: string | null
  links: { branding: string | null; audience: string | null; create_client: string | null }
}

/**
 * The route's self-check states (`routes/setup.py` `self_check_view`, audit
 * finding 28): before swarm install's infra pass 3 gives swarm-api the App's
 * id, `check_app_key` answers `app_id_not_configured`, which is a wait for
 * that pass, never a failure.
 */
export type SetupSelfCheckState = 'passed' | 'failed' | 'waiting_for_infra_pass_3'

/** The `check_app_key` reasons that mean infra pass 3 has not landed yet (`BEFORE_PASS_3`). */
const BEFORE_PASS_3 = new Set(['app_id_not_configured', 'app_slug_not_configured'])

export interface SetupSelfCheck {
  ok: boolean
  reason: string | null
  at: string | null
  state: SetupSelfCheckState
}

/** A self-check as the route served it; one recorded before its `state` existed is read by its reason, the route's way. */
export function normSelfCheck(raw: unknown): SetupSelfCheck | null {
  const c = objOf(raw)
  if (c === null) return null
  const ok = c.ok === true
  const reason = strOf(c.reason)
  const state: SetupSelfCheckState = ok ? 'passed' : c.state === 'waiting_for_infra_pass_3' || (reason !== null && BEFORE_PASS_3.has(reason)) ? 'waiting_for_infra_pass_3' : 'failed'
  return { ok, reason, at: strOf(c.at), state }
}

/**
 * The first team's team-level grant over the repositories the GitHub App
 * installation covers (`teams/<id>.grant`, decision 3): `granted`, or
 * `waiting_for_app` / `waiting_for_repositories` until the App covers one.
 */
export interface SetupGrant {
  state: string
  account: string | null
  /** `all` or `selected`: what the installation was given on GitHub. */
  selection: string | null
  repositories: string[]
}

/** One invitee's (or the first admin's) membership of the team's group, per the Groups API; null when it did not answer. */
export interface SetupMember {
  email: string
  member: boolean | null
}

/**
 * The first workspace's smoke (`teams/<id>.smoke`, or `personal.smoke` for one
 * person): `running`, then `succeeded` or `failed`, the words the route
 * writes (`routes/setup.py` SUCCEEDED/FAILED). `detail` is the route's
 * `reason` when it failed to start.
 */
export interface SetupSmoke {
  state: string
  detail: string | null
  url: string | null
}

/** The route's word for a smoke that passed; `finish` waits for exactly this. */
export const SMOKE_SUCCEEDED = 'succeeded'

/** `console/teams/<id>`, as the status route reads it live. `smoke` is the team's smoke agent, once it has one. */
export interface SetupTeam {
  id: string
  group: string
  workspace: string
  first_admin: string | null
  invites: string[]
  /** The record's progress, with the workspace's live `workspace_state` the route reads beside it. */
  progress: Record<string, unknown>
  smoke: SetupSmoke | null
  grant: SetupGrant | null
  members: SetupMember[]
  /** The labels of the pool accounts the route found in the team's own pool, read live. */
  pool_accounts: string[]
  /** The workspace record's `w-` id: a failed workspace is retried with `retryWorkspace(workspace_id)` (audit 5b, M6). */
  workspace_id: string | null
  /** The group's members page in Google Admin, served on every row so every invite keeps its link (audit 5b, M5). */
  add_members_url: string | null
}

/**
 * `personal` (decision 7): a one-person install's first workspace, the
 * installer's personal workspace, in the first team's place. `reason` is the
 * route's code when it could not be filed on this read.
 */
export interface SetupPersonal {
  owner: string | null
  workspace: string | null
  reason: string | null
  progress: Record<string, unknown>
  grant: SetupGrant | null
  smoke: SetupSmoke | null
  pool_accounts: string[]
}

/** One of the steps the route says are needed or not (`steps_view`): `first_team`, `invites`. */
export interface SetupStepNeed {
  needed: boolean
  state: string
  reason: string | null
}

/** `console/verify_runs` (SETUP-API-2): the console's two verification runs, once Admin › Setup started them. */
export interface SetupVerifyRuns {
  task: string | null
  workflow: string | null
  repository: string | null
  /** Each run's fresh starts so far, as the route counts them in the document's `retries` (0 when it serves none). */
  retries: { task: number; workflow: number }
}

/**
 * How many fresh starts the verify-runs route gives a failed console run
 * (swarm-api's `MAX_VERIFY_RETRIES`); past them it answers 409 and starts none.
 */
export const VERIFY_RUN_RETRIES = 2

/**
 * `sizing` (INTERFACE SHEET; stage 0's SIZING-ASK, served by the status
 * route): one person or a team, and the capacity numbers the person accepted.
 * The `pool_limits` keys are terraform/infra's `pool_limits` variable.
 */
export interface SetupSizing {
  mode: 'individual' | 'team'
  members: number | null
  pool_limits: { global: number | null; default_tenant: number | null; provider_tenant: number | null }
  service_max_instances: Record<string, number>
  pool_accounts: number | null
  budget_usd: number | null
}

/** One pool account's row of `console/accounts`; the link's hash is never served. */
export interface SetupAccount {
  label: string
  owner: string | null
  link_expires: string | null
  signed_in_at: string | null
}

export interface SetupAddon {
  name: string
  /** The CLI line that enables it: enabling needs a Terraform switch. */
  enable: string
  secret_link: boolean
}

/** `GET /v1/admin/setup`: the page's whole state. */
export interface SetupState {
  env: string
  engine: SetupEngine | null
  /** False when the install lease's heartbeat is over 2 minutes old: swarm install is not running. */
  cli_running: boolean
  /** The Desktop sign-in client step; null until the route serves it: drawn as not read yet, never as done. */
  sign_in_client: SetupSignInClient | null
  github_app: SetupGitHubApp | null
  teams: SetupTeam[]
  accounts: SetupAccount[]
  finished: { at: string | null; by: string | null } | null
  addons: SetupAddon[]
  hostname: string | null
  github_org: string | null
  verify_runs: SetupVerifyRuns | null
  sizing: SetupSizing | null
  /** `individual` for one person (decision 7), else `team`; the route's `mode`, else the sizing's. */
  mode: 'individual' | 'team'
  /** The personal workspace's row for one person; null for a team. */
  personal: SetupPersonal | null
  /** Whether the first-team and invites steps are needed, as the route says; null from a route that serves none. */
  steps: { first_team: SetupStepNeed | null; invites: SetupStepNeed | null } | null
}

const objOf = (v: unknown): Record<string, unknown> | null => (typeof v === 'object' && v !== null && !Array.isArray(v) ? (v as Record<string, unknown>) : null)
const strOf = (v: unknown): string | null => (typeof v === 'string' && v !== '' ? v : typeof v === 'number' ? String(v) : null)
const numOf = (v: unknown): number | null => {
  if (typeof v === 'number' && Number.isFinite(v)) return v
  // A time the record stored as a timestamp reaches JSON as ISO text: read it as epoch seconds too.
  if (typeof v === 'string' && v !== '') {
    const ms = Date.parse(v)
    return Number.isNaN(ms) ? null : ms / 1000
  }
  return null
}

/** A Google Cloud console page, or null: the only pages the sign-in client step links. */
const cloudConsolePage = (v: unknown): string | null => (typeof v === 'string' && v.startsWith('https://console.cloud.google.com/') ? v : null)

/** `sign_in_client`, or null when the route serves none (a swarm-api before #2964) or a state it never writes. */
export function normSignInClient(raw: unknown): SetupSignInClient | null {
  const d = objOf(raw)
  if (d === null || (d.state !== 'waiting' && d.state !== 'saved' && d.state !== 'applied')) return null
  const links = objOf(d.links) ?? {}
  return {
    state: d.state,
    client_id: strOf(d.client_id),
    saved_at: strOf(d.saved_at),
    saved_by: strOf(d.saved_by),
    links: { branding: cloudConsolePage(links.branding), audience: cloudConsolePage(links.audience), create_client: cloudConsolePage(links.create_client) },
  }
}

function normVerify(raw: unknown): SetupVerify | null {
  const v = objOf(raw)
  if (v === null) return null
  return {
    check: strOf(v.check),
    done: numOf(v.done) ?? 0,
    total: numOf(v.total) ?? 0,
    started: numOf(v.started),
    expected_end: numOf(v.expected_end),
    deadline: numOf(v.deadline),
    result: strOf(v.result),
  }
}

/**
 * An engine time as ISO text. The engine writes `lease.heartbeat`,
 * `hand_off_at`, `ready_at` and `stages[].at` as epoch seconds (audit 3, A7);
 * a route that converts them serves ISO text. Both read as the same instant,
 * and anything that is neither is null, so no field renders Invalid Date.
 */
function timeOf(v: unknown): string | null {
  const seconds = typeof v === 'number' ? v : typeof v === 'string' && /^\d+(\.\d+)?$/.test(v.trim()) ? Number(v) : null
  if (seconds !== null) {
    const at = new Date(seconds * 1000)
    return Number.isFinite(seconds) && !Number.isNaN(at.getTime()) ? at.toISOString() : null
  }
  if (typeof v !== 'string' || v === '') return null
  return Number.isNaN(Date.parse(v)) ? null : v
}

function normEngine(raw: unknown): SetupEngine | null {
  const e = objOf(raw)
  if (e === null) return null
  const stages = Array.isArray(e.stages)
    ? e.stages.map((s) => {
        const o = objOf(s)
        return o === null || strOf(o.state) === null ? null : { state: strOf(o.state) as string, at: timeOf(o.at), by: strOf(o.by) }
      })
    : []
  const lease = objOf(e.lease)
  const teamGroup = strOf(objOf(e.workspace_groups)?.team)
  return {
    release: strOf(e.release),
    hostname: strOf(e.hostname),
    stages,
    lease: lease === null ? null : { holder: strOf(lease.holder), heartbeat: timeOf(lease.heartbeat) },
    verify: normVerify(e.verify),
    hand_off_at: timeOf(e.hand_off_at),
    ready_at: timeOf(e.ready_at),
    workspace_groups: teamGroup === null ? null : { team: teamGroup },
  }
}

function normGitHubApp(raw: unknown): SetupGitHubApp | null {
  const g = objOf(raw)
  if (g === null) return null
  return {
    started: g.started === true || strOf(g.id) !== null,
    id: strOf(g.id),
    slug: strOf(g.slug),
    installed_on: strOf(g.installed_on),
    secrets_written_at: strOf(g.secrets_written_at),
    self_check: normSelfCheck(g.self_check),
  }
}

function normSmoke(raw: unknown): SetupSmoke | null {
  const smoke = objOf(raw)
  return smoke === null ? null : { state: strOf(smoke.state) ?? 'waiting', detail: strOf(smoke.detail) ?? strOf(smoke.reason), url: strOf(smoke.url) }
}

function normGrant(raw: unknown): SetupGrant | null {
  const grant = objOf(raw)
  return grant === null || strOf(grant.state) === null
    ? null
    : {
        state: strOf(grant.state) as string,
        account: strOf(grant.account),
        selection: strOf(grant.selection),
        repositories: Array.isArray(grant.repositories) ? grant.repositories.filter((r): r is string => typeof r === 'string' && r !== '') : [],
      }
}

const labelsOf = (v: unknown): string[] => (Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string' && x !== '') : [])

function normPersonal(raw: unknown): SetupPersonal | null {
  const p = objOf(raw)
  if (p === null) return null
  const progress = objOf(p.progress) ?? {}
  // The route reads the workspace's state live and serves it beside the record, as `workspace_state`.
  const live = strOf(p.workspace_state)
  return {
    owner: strOf(p.owner),
    workspace: strOf(p.workspace),
    reason: strOf(p.reason),
    progress: live === null ? progress : { ...progress, workspace_state: live },
    grant: normGrant(p.grant),
    smoke: normSmoke(p.smoke),
    pool_accounts: labelsOf(p.pool_accounts),
  }
}

function normStepNeed(raw: unknown): SetupStepNeed | null {
  const s = objOf(raw)
  return s === null ? null : { needed: s.needed !== false, state: strOf(s.state) ?? '', reason: strOf(s.reason) }
}

function normTeam(raw: unknown): SetupTeam | null {
  const t = objOf(raw)
  const id = t === null ? null : strOf(t.id)
  if (t === null || id === null) return null
  const progress = objOf(t.progress) ?? {}
  // The route reads the workspace's state live and serves it beside the record, as `workspace_state`.
  const live = strOf(t.workspace_state)
  return {
    id,
    group: strOf(t.group) ?? '',
    workspace: strOf(t.workspace) ?? id,
    first_admin: strOf(t.first_admin),
    invites: Array.isArray(t.invites) ? t.invites.filter((x): x is string => typeof x === 'string') : [],
    progress: live === null ? progress : { ...progress, workspace_state: live },
    smoke: normSmoke(t.smoke),
    grant: normGrant(t.grant),
    members: (Array.isArray(t.members) ? t.members : [])
      .map(objOf)
      .filter((m): m is Record<string, unknown> => m !== null && strOf(m.email) !== null)
      .map((m) => ({ email: strOf(m.email) as string, member: typeof m.member === 'boolean' ? m.member : null })),
    pool_accounts: labelsOf(t.pool_accounts),
    workspace_id: strOf(t.workspace_id),
    add_members_url: adminMembersPage(t.add_members_url),
  }
}

/** `console/verify_runs` as the status route serves it: `{verify_runs: {console_task, console_workflow}, retries, repository}`. */
function normVerifyRuns(raw: unknown): SetupVerifyRuns | null {
  const doc = objOf(raw)
  const ids = objOf(doc?.verify_runs)
  if (doc === null || ids === null) return null
  const task = strOf(ids.console_task)
  const workflow = strOf(ids.console_workflow)
  // The route's `retries` field; a document written before it kept `<key>_retries` beside each id.
  const counts = objOf(doc.retries)
  const count = (key: string): number => {
    const n = counts?.[key] ?? ids[`${key}_retries`]
    return typeof n === 'number' && Number.isFinite(n) ? Math.max(0, Math.trunc(n)) : 0
  }
  return task === null && workflow === null
    ? null
    : { task, workflow, repository: strOf(doc.repository), retries: { task: count('console_task'), workflow: count('console_workflow') } }
}

/** `sizing`, or null until stage 0 recorded one. A mode other than the two is not a sizing. */
export function normSizing(raw: unknown): SetupSizing | null {
  const d = objOf(raw)
  if (d === null || (d.mode !== 'individual' && d.mode !== 'team')) return null
  const limits = objOf(d.pool_limits) ?? {}
  const services = objOf(d.service_max_instances) ?? {}
  const max: Record<string, number> = {}
  for (const [name, v] of Object.entries(services)) if (typeof v === 'number' && Number.isFinite(v)) max[name] = v
  const n = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null)
  return {
    mode: d.mode,
    members: n(d.members),
    pool_limits: { global: n(limits.global), default_tenant: n(limits.default_tenant), provider_tenant: n(limits.provider_tenant) },
    service_max_instances: max,
    pool_accounts: n(d.pool_accounts),
    budget_usd: n(d.budget_usd),
  }
}

/** One normaliser for `GET /v1/admin/setup`, exported so a panel's fixture is read the way the live answer is. */
export function normSetupState(raw: unknown): SetupState {
  const d = objOf(raw) ?? {}
  const accounts = objOf(d.accounts) ?? {}
  const finished = objOf(d.finished)
  const sizing = normSizing(d.sizing)
  const mode = d.mode === 'individual' || d.mode === 'team' ? d.mode : (sizing?.mode ?? 'team')
  const steps = objOf(d.steps)
  return {
    env: strOf(d.env) ?? '',
    engine: normEngine(d.engine),
    cli_running: d.cli_running === true,
    sign_in_client: normSignInClient(d.sign_in_client),
    github_app: normGitHubApp(d.github_app),
    teams: Array.isArray(d.teams) ? d.teams.map(normTeam).filter((t): t is SetupTeam => t !== null) : [],
    accounts: Object.keys(accounts)
      .sort()
      .map((label) => {
        const a = objOf(accounts[label]) ?? {}
        return { label, owner: strOf(a.owner), link_expires: strOf(a.link_expires), signed_in_at: strOf(a.signed_in_at) }
      }),
    finished: finished === null ? null : { at: strOf(finished.at), by: strOf(finished.by) },
    addons: Array.isArray(d.addons)
      ? d.addons
          .map(objOf)
          .filter((a): a is Record<string, unknown> => a !== null && strOf(a.name) !== null)
          .map((a) => ({ name: strOf(a.name) as string, enable: strOf(a.enable) ?? `swarm addon enable ${strOf(a.name)}`, secret_link: a.secret_link === true }))
      : [],
    hostname: strOf(d.hostname),
    github_org: strOf(d.github_org),
    verify_runs: normVerifyRuns(d.verify_runs),
    sizing,
    mode,
    personal: mode === 'individual' ? normPersonal(d.personal) : null,
    steps: steps === null ? null : { first_team: normStepNeed(steps.first_team), invites: normStepNeed(steps.invites) },
  }
}

/** `POST /v1/admin/setup/github-app/manifest`: the App manifest and the github.com form it posts to. */
export interface GitHubAppManifestStart {
  manifest: Record<string, unknown>
  /** `https://github.com/organizations/<org>/settings/apps/new?state=…` */
  action: string
}

/** `GET /v1/admin/setup/github-app/installation`. */
export interface GitHubAppInstallation {
  installed: boolean
  org: string | null
  slug: string | null
}

/** `POST /v1/admin/setup/links`: a one-time link. Its URL carries the token: shown once, never stored here. */
export interface SetupLink {
  kind: string
  label: string
  owner: string
  url: string | null
  path: string
  expires_at: string | null
}

export type SetupLinkKind = 'pool-account' | 'team-git' | 'addon-secret'

/**
 * One check's last result in `health/latest`, read from the row
 * swarm-doctor writes (`doctor.Finding.to_json`: `{id, status, detail, step,
 * fix, cause, repair}`). "Not checked" is its own result, never a pass.
 */
export interface HealthCheck {
  /** The manifest entry's id; the doctor writes no other title. */
  id: string
  /** `passed` (met), `failed` (unmet), or `not_checked` (unknown, or anything unread). */
  result: HealthResult
  detail: string | null
  /** The install step that satisfies the entry, when one does. */
  step: string | null
}

export type HealthResult = 'passed' | 'failed' | 'not_checked'

/** One doctor finding (a check that is not met): what the probe saw, why, and how to repair it. */
export interface HealthFinding {
  id: string
  /** `failed` (unmet: it drifted) or `not_checked` (unknown: the probe could not say). */
  result: Exclude<HealthResult, 'passed'>
  /** What the probe saw. */
  detail: string | null
  step: string | null
  /** The doctor's fix, in words: what the entry is for and what repairs it. */
  fix: string | null
  /** Why a probe saw a 403: `propagating` or `missing_grant`; null otherwise. */
  cause: string | null
  /** `swarm install --repair <id>`, or null when no install step makes the entry. */
  repair: string | null
}

export interface SetupHealth {
  run_at: string | null
  checks: HealthCheck[]
  findings: HealthFinding[]
}

/**
 * The doctor's `status` (swarm_install/exitcodes.py's MET, UNMET, UNKNOWN) as
 * the console's result. Only the doctor's own words are read: a row in any
 * other shape (a `result: passed` the doctor never writes) is not checked,
 * so a fake in the console's own shape cannot draw a clean page.
 */
const HEALTH_RESULT_OF_STATUS: Record<string, HealthResult> = { met: 'passed', unmet: 'failed', unknown: 'not_checked' }

const healthResultOf = (status: unknown): HealthResult => {
  const s = strOf(status)
  return (s !== null && Object.prototype.hasOwnProperty.call(HEALTH_RESULT_OF_STATUS, s) ? HEALTH_RESULT_OF_STATUS[s] : undefined) ?? 'not_checked'
}

const doctorRows = (rows: unknown): Record<string, unknown>[] =>
  (Array.isArray(rows) ? rows : []).map(objOf).filter((r): r is Record<string, unknown> => r !== null && strOf(r.id) !== null)

/** `GET /v1/admin/setup/health`'s `latest`, or null when no doctor run is recorded. */
export function normSetupHealth(raw: unknown): SetupHealth | null {
  const latest = objOf(objOf(raw)?.latest)
  if (latest === null) return null
  const checks = doctorRows(latest.checks).map((c) => ({
    id: strOf(c.id) as string,
    result: healthResultOf(c.status),
    detail: strOf(c.detail),
    step: strOf(c.step),
  }))
  // The doctor lists every finding that is not met; a met row here is not a finding.
  const findings = doctorRows(latest.findings).flatMap((f): HealthFinding[] => {
    const result = healthResultOf(f.status)
    if (result === 'passed') return []
    return [{ id: strOf(f.id) as string, result, detail: strOf(f.detail), step: strOf(f.step), fix: strOf(f.fix), cause: strOf(f.cause), repair: strOf(f.repair) }]
  })
  return { run_at: strOf(latest.run_at), checks, findings }
}

/** A write's answer as T, or the write's own error. */
async function setupWrite<T>(target: ApiRoute, method: 'POST' | 'PUT', body: unknown, norm: (raw: unknown) => T, fixture: () => T): Promise<Result<T>> {
  if (USE_FIXTURES) {
    await new Promise((r) => setTimeout(r, 120))
    if (!fixtureIsAdmin) return fixtureAdminRefusal<T>(target, 120)
    noteFixtureProbe(target, 120, true)
    return { status: 'ok', data: fixture(), fetchedAt: Date.now() }
  }
  const r = await write(target, method, body)
  if (r.status === 'ok' || r.status === 'stale') return { ...r, data: norm(r.data) }
  return r as Result<T>
}

/** `GET /v1/admin/setup`. */
export async function loadSetup(): Promise<Result<SetupState>> {
  const target = route('/v1/admin/setup')
  if (USE_FIXTURES) {
    await new Promise((r) => setTimeout(r, 120))
    if (!fixtureIsAdmin) return fixtureAdminRefusal<SetupState>(target, 120)
    noteFixtureProbe(target, 120, true)
    return { status: 'ok', data: normSetupState(fixtureSetupState()), fetchedAt: Date.now() }
  }
  const r = await read<unknown>(target, () => false)
  if (r.status === 'ok' || r.status === 'stale') return { ...r, data: normSetupState(r.data) }
  return r as Result<SetupState>
}

/** `POST /v1/admin/setup/github-app/manifest`. */
export function startGitHubApp(): Promise<Result<GitHubAppManifestStart>> {
  return setupWrite(
    route('/v1/admin/setup/github-app/manifest'),
    'POST',
    {},
    (raw) => ({ manifest: objOf(objOf(raw)?.manifest) ?? {}, action: strOf(objOf(raw)?.action) ?? '' }),
    () => ({ manifest: { name: 'SwarmCloud prod' }, action: 'https://github.com/organizations/example-org/settings/apps/new' }),
  )
}

/**
 * `POST /v1/admin/setup/github-app/conversion` with `{code, state}`. The
 * answer is read for `id` and `slug` ONLY: GitHub's secrets went straight to
 * Secret Manager, and were the server ever to answer more, nothing past those
 * two fields reaches a screen.
 */
export function convertGitHubApp(code: string, state: string): Promise<Result<{ id: string | null; slug: string | null }>> {
  return setupWrite(
    route('/v1/admin/setup/github-app/conversion'),
    'POST',
    { code, state },
    (raw) => ({ id: strOf(objOf(raw)?.id), slug: strOf(objOf(raw)?.slug) }),
    () => ({ id: '4242', slug: 'swarmcloud-prod' }),
  )
}

/**
 * `POST /v1/admin/setup/sign-in-client` with exactly `{client_id, client_secret}`
 * (issue #2964). swarm-api adds the secret as a new version of
 * `swarm-iap-desktop-client-secret` and answers the `sign_in_client` object,
 * which never carries the secret; nothing here keeps it past the request.
 */
export function saveSignInClient(clientId: string, clientSecret: string): Promise<Result<SetupSignInClient | null>> {
  return setupWrite(
    route('/v1/admin/setup/sign-in-client'),
    'POST',
    { client_id: clientId, client_secret: clientSecret },
    normSignInClient,
    () => normSignInClient({ ...fixtureSignInClient(), state: 'saved', client_id: clientId }),
  )
}

/** `GET /v1/admin/setup/github-app/installation`: is the App on the org yet (polled). */
export async function checkGitHubAppInstallation(): Promise<Result<GitHubAppInstallation>> {
  const target = route('/v1/admin/setup/github-app/installation')
  const norm = (raw: unknown): GitHubAppInstallation => ({ installed: objOf(raw)?.installed === true, org: strOf(objOf(raw)?.org), slug: strOf(objOf(raw)?.slug) })
  if (USE_FIXTURES) {
    await new Promise((r) => setTimeout(r, 120))
    if (!fixtureIsAdmin) return fixtureAdminRefusal<GitHubAppInstallation>(target, 120)
    noteFixtureProbe(target, 120, true)
    return { status: 'ok', data: { installed: true, org: 'example-org', slug: 'swarmcloud-prod' }, fetchedAt: Date.now() }
  }
  const r = await read<unknown>(target, () => false)
  if (r.status === 'ok' || r.status === 'stale') return { ...r, data: norm(r.data) }
  return r as Result<GitHubAppInstallation>
}

/** `POST /v1/admin/setup/github-app/self-check`: the `/admin/forge/app` check, recorded. */
export function runGitHubAppSelfCheck(): Promise<Result<SetupSelfCheck>> {
  return setupWrite(
    route('/v1/admin/setup/github-app/self-check'),
    'POST',
    undefined,
    (raw) => normSelfCheck(raw) ?? { ok: false, reason: null, at: null, state: 'failed' },
    () => ({ ok: true, reason: null, at: new Date(FIXTURE_NOW).toISOString(), state: 'passed' }),
  )
}

/**
 * `POST /v1/admin/setup/smoke` (audit finding 20): the first workspace's
 * smoke (the first team's; for one person the personal workspace's) started
 * again after it failed. A running or passed smoke is answered as it stands.
 */
export function restartSetupSmoke(): Promise<Result<{ workspace: string | null; smoke: SetupSmoke | null }>> {
  return setupWrite(
    route('/v1/admin/setup/smoke'),
    'POST',
    undefined,
    (raw) => ({ workspace: strOf(objOf(raw)?.workspace), smoke: normSmoke(objOf(raw)?.smoke) }),
    () => ({ workspace: 'engineering', smoke: { state: 'running', detail: null, url: null } }),
  )
}

/**
 * `POST /v1/admin/setup/teams`: the same call `swarm team add` makes. It
 * answers each invite with the group's Google Admin members page (#26), kept
 * so the form opens it; a page that is not Google Admin's is dropped.
 */
export function createSetupTeam(body: { group: string; display_name?: string; first_admin?: string; invites: string[] }): Promise<Result<{ team: string | null; invites: SetupInvite[] }>> {
  return setupWrite(
    route('/v1/admin/setup/teams'),
    'POST',
    body,
    (raw) => {
      const d = objOf(raw) ?? {}
      return { team: strOf(d.team), invites: (Array.isArray(d.invites) ? d.invites : []).map(normInvite).filter((i): i is SetupInvite => i !== null) }
    },
    () => ({ team: body.group.split('@')[0] ?? null, invites: body.invites.map((email) => ({ email, add_members_url: 'https://admin.google.com/ac/groups', member: false })) }),
  )
}

/** `POST /v1/admin/setup/links`: a one-time link for one named person. */
export function mintSetupLink(body: { kind: SetupLinkKind; label: string; owner: string }): Promise<Result<SetupLink>> {
  return setupWrite(
    route('/v1/admin/setup/links'),
    'POST',
    body,
    (raw) => {
      const d = objOf(raw) ?? {}
      return { kind: strOf(d.kind) ?? body.kind, label: strOf(d.label) ?? body.label, owner: strOf(d.owner) ?? body.owner, url: strOf(d.url), path: strOf(d.path) ?? '', expires_at: strOf(d.expires_at) }
    },
    () => ({ ...body, url: null, path: '/setup/link', expires_at: new Date(FIXTURE_NOW + 24 * 3_600_000).toISOString() }),
  )
}

/** `POST /v1/admin/setup/teams/{id}/invites`'s answer: the Google Admin page that adds them to the group, and whether they are in yet. */
export interface SetupInvite {
  email: string
  add_members_url: string | null
  member: boolean | null
}

/** A members page as the setup routes serve it: kept only when it is Google Admin's. */
function adminMembersPage(raw: unknown): string | null {
  const url = strOf(raw)
  return url !== null && url.startsWith('https://admin.google.com/') ? url : null
}

/** One invite as the team routes answer it; only a Google Admin page is kept as its members page. */
function normInvite(raw: unknown): SetupInvite | null {
  const d = objOf(raw)
  const email = d === null ? null : strOf(d.email)
  if (d === null || email === null) return null
  return { email, add_members_url: adminMembersPage(d.add_members_url), member: typeof d.member === 'boolean' ? d.member : null }
}

/**
 * `POST /v1/admin/setup/teams/{id}/invites` (decision 4): records the invite
 * and answers the Google Admin members page. swarm-api holds Groups Reader
 * only, so it adds no one; the status route then polls the membership.
 */
export function inviteToSetupTeam(teamId: string, email: string): Promise<Result<SetupInvite>> {
  return setupWrite(
    route('/v1/admin/setup/teams/{id}/invites', { id: teamId }),
    'POST',
    { email },
    (raw) => {
      const d = objOf(raw) ?? {}
      return normInvite({ ...d, email: strOf(d.email) ?? email }) ?? { email, add_members_url: null, member: null }
    },
    () => ({ email, add_members_url: 'https://admin.google.com/ac/groups', member: false }),
  )
}

/**
 * `POST /v1/admin/setup/verify-runs` (SETUP-API-2): the console's two
 * verification runs, a task and a workflow, as the first team on a repository
 * of its grant. Called again it answers the runs already started.
 */
export function startSetupVerifyRuns(): Promise<Result<SetupVerifyRuns>> {
  return setupWrite(
    route('/v1/admin/setup/verify-runs'),
    'POST',
    undefined,
    (raw) => normVerifyRuns(raw) ?? { task: null, workflow: null, repository: strOf(objOf(raw)?.repository), retries: { task: 0, workflow: 0 } },
    () => ({ task: 'task_fixture_verify', workflow: 'wf_fixture_verify', repository: 'example-org/app', retries: { task: 0, workflow: 0 } }),
  )
}

/** `POST /v1/admin/setup/finish`: `console/finished`, once every console step is done; the CLI then runs verify. */
export function finishSetup(): Promise<Result<{ at: string | null }>> {
  return setupWrite(
    route('/v1/admin/setup/finish'),
    'POST',
    undefined,
    (raw) => ({ at: strOf(objOf(objOf(raw)?.finished)?.at) }),
    () => ({ at: new Date(FIXTURE_NOW).toISOString() }),
  )
}

/** `GET /v1/admin/setup/health`: the latest scheduled doctor run, or null. */
export async function loadSetupHealth(): Promise<Result<SetupHealth | null>> {
  const target = route('/v1/admin/setup/health')
  if (USE_FIXTURES) {
    await new Promise((r) => setTimeout(r, 120))
    if (!fixtureIsAdmin) return fixtureAdminRefusal<SetupHealth | null>(target, 120)
    noteFixtureProbe(target, 120, true)
    return { status: 'ok', data: normSetupHealth(fixtureSetupHealth()), fetchedAt: Date.now() }
  }
  const r = await read<unknown>(target, () => false)
  if (r.status === 'ok' || r.status === 'stale') return { ...r, data: normSetupHealth(r.data) }
  return r as Result<SetupHealth | null>
}

/** The fixture build's sign-in client before anything is saved: the example project's three console pages. */
function fixtureSignInClient(): Record<string, unknown> {
  const page = (path: string) => `https://console.cloud.google.com/auth/${path}?project=example`
  return { state: 'waiting', client_id: null, saved_at: null, saved_by: null, links: { branding: page('branding'), audience: page('audience'), create_client: page('clients/create') } }
}

/** The fixture build's install, mid-verify: every console step done, verify on its third check. */
function fixtureSetupState(): Record<string, unknown> {
  const at = (minAgo: number) => new Date(FIXTURE_NOW - minAgo * 60_000).toISOString()
  const t0 = FIXTURE_NOW / 1000 - 240
  return {
    env: 'prod',
    engine: {
      release: 'v1.0.0',
      hostname: 'console.example.com',
      stages: Array.from({ length: 16 }, (_, i) => (i <= 13 ? { state: i === 9 ? 'switches_on' : 'done', at: at(40 - i), by: 'ana@example.com' } : null)),
      lease: { holder: 'ana@example.com laptop', heartbeat: at(0) },
      verify: { check: 'digests', done: 2, total: 8, started: t0, expected_end: t0 + 600, deadline: t0 + 1800, result: null },
      hand_off_at: at(30),
      ready_at: null,
    },
    cli_running: true,
    sign_in_client: { ...fixtureSignInClient(), state: 'applied', client_id: 'fixture-desktop-client.apps.googleusercontent.com', saved_at: at(38), saved_by: 'ana@example.com' },
    github_app: { started: true, id: 4242, slug: 'swarmcloud-prod', installed_on: 'example-org', secrets_written_at: at(25), self_check: { ok: true, reason: null, at: at(20) } },
    teams: [
      {
        id: 'engineering',
        group: 'engineering@example.com',
        workspace: 'engineering',
        first_admin: 'ana@example.com',
        invites: ['ravi@example.com', 'lea@example.com'],
        progress: { workspace_state: 'ready', created_at: at(18) },
        smoke: { state: 'succeeded', detail: 'green in 2 min 48 s', url: null },
        grant: { state: 'granted', account: 'example-org', selection: 'selected', repositories: ['example-org/app', 'example-org/web'] },
        members: [
          { email: 'ana@example.com', member: true },
          { email: 'lea@example.com', member: false },
          { email: 'ravi@example.com', member: true },
        ],
      },
    ],
    accounts: {
      ravi: { owner: 'ravi@example.com', link_expires: at(-1400), signed_in_at: at(12) },
      lea: { owner: 'lea@example.com', link_expires: at(-1430), signed_in_at: null },
    },
    finished: { at: at(5), by: 'ana@example.com' },
    addons: [
      { name: 'brave-search', enable: 'swarm addon enable brave-search', secret_link: true },
      { name: 'ci', enable: 'swarm addon enable ci', secret_link: false },
    ],
    hostname: 'console.example.com',
    github_org: 'example-org',
    sizing: {
      mode: 'team',
      members: 12,
      pool_limits: { global: 40, default_tenant: 20, provider_tenant: 20, resource_classes: {}, runner_profiles: {}, backends: {} },
      service_max_instances: { 'swarm-api': 4, 'swarm-scheduler': 2, 'swarm-quota-broker': 1, 'swarm-reconciler': 1 },
      pool_accounts: 3,
      budget_usd: 900,
    },
  }
}

/**
 * The fixture build's latest doctor run, in the shape swarm-doctor writes
 * (`scheduled_doctor.report()`: every row is `doctor.Finding.to_json()`, and
 * `findings` repeats each row that is not met): two met, one unmet, one
 * unknown. AdminSetupHealth.test.tsx holds these rows to the writer's keys.
 */
export function fixtureSetupHealth(): Record<string, unknown> {
  const fix = (why: string, step: string, id: string) => `${why} Fix: the install's step ${step} makes it: swarm install --repair ${id}.`
  const rows = [
    { id: 'api.artifactregistry', status: 'met', detail: 'artifactregistry.googleapis.com is enabled', step: 'foundations.apis', fix: fix('The registry the images are pulled from.', 'foundations.apis', 'api.artifactregistry'), cause: '', repair: 'swarm install --repair api.artifactregistry' },
    { id: 'api.cloudkms', status: 'unmet', detail: 'cloudkms.googleapis.com is not enabled', step: 'foundations.apis', fix: fix('The keys the state bucket and the secrets are encrypted with.', 'foundations.apis', 'api.cloudkms'), cause: '', repair: 'swarm install --repair api.cloudkms' },
    { id: 'api.cloudbuild', status: 'met', detail: 'cloudbuild.googleapis.com is enabled', step: 'foundations.apis', fix: fix('The builds the release images come from.', 'foundations.apis', 'api.cloudbuild'), cause: '', repair: 'swarm install --repair api.cloudbuild' },
    { id: 'quota.cloud-run-jobs', status: 'unknown', detail: 'missing grant: swarm-doctor@example.iam.gserviceaccount.com lacks serviceusage.quotas.get on projects/example, so this could not be checked', step: null, fix: 'Fix: no install step makes this; see the entry\'s check: quota.cloud-run-jobs.', cause: 'missing_grant', repair: null },
  ]
  return {
    latest: {
      run_at: new Date(FIXTURE_NOW - 2 * 60_000).toISOString(),
      checks: rows,
      findings: rows.filter((r) => r.status !== 'met'),
    },
  }
}
