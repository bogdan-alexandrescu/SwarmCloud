import { noteFixtureProbe, read, write, type Result } from './fetch'
// A VALUE import, not a type: the attempt fixture needs the terminal-state set
// so a live task's newest attempt is rendered as live.
import { TERMINAL_STATES } from './types'
import type {
  Capacity, DispatchControl, Me, ProvidersPage, Stats, Task, TaskEvent, TaskPage,
  AttemptRow, LeasePage, LeaseRow, Pool, QuotaState, ResourceClassSpec, TaskState,
  TaskWindow, Tenant,
  Workflow,
  Account, AccountStateName, AccountsPage, RefreshResponse,
  AccountAuthorization, AccountExchangeResponse,
} from './types'

// The fetch contract lives in fetch.ts. This file is only the list of reads
// this product performs, and what "empty" means for each of them -- which is
// per-endpoint and cannot be guessed: a capacity response with no pools and a
// task page with no tasks are both non-empty objects.

export type { Result, ApiError } from './fetch'

/** Fixtures, so the UI can be worked on before DNS resolves. */
const USE_FIXTURES = import.meta.env.DEV && !import.meta.env.VITE_LIVE

export async function loadCapacity(): Promise<Result<Capacity>> {
  if (USE_FIXTURES) return fixtureCapacity()
  return read<Capacity>('/v1/capacity', (d) => d.pools.length === 0)
}

export async function loadTasks(): Promise<Result<TaskPage>> {
  if (USE_FIXTURES) return fixtureTasks()
  // Page size caps at 200 server-side (deps.py:194-199). Asking for more is
  // silently clamped, which would make "200 tasks" look like the whole truth.
  return read<TaskPage>('/v1/tasks?limit=200', (d) => d.tasks.length === 0)
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
}

export async function loadWorkflowBoard(): Promise<Result<WorkflowBoard>> {
  if (USE_FIXTURES) return fixtureWorkflowBoard()

  const [wf, tasks] = await Promise.all([
    read<{ workflows: Workflow[] }>('/v1/workflows?limit=100', (d) => d.workflows.length === 0),
    read<TaskPage>('/v1/tasks?limit=200', (d) => d.tasks.length === 0),
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
      data: { workflows: wf.data.workflows, taskById: null, statesDetail: tasks.error.message },
    }
  }

  const taskById = new Map<string, Task>()
  if (tasks.status === 'ok') for (const t of tasks.data.tasks) taskById.set(t.id, t)
  return {
    status: 'ok',
    fetchedAt: wf.fetchedAt,
    serverAt: wf.serverAt,
    data: { workflows: wf.data.workflows, taskById, statesDetail: null },
  }
}

function emptyBoard(workflows: Workflow[]): WorkflowBoard {
  return { workflows, taskById: null, statesDetail: 'The task read did not complete.' }
}

/**
 * One agent, with its event timeline.
 *
 * Two reads, not three. `GET /v1/tasks/{id}/artifacts` is deliberately NOT
 * called: it reads a Firestore subcollection nothing writes, so it returns []
 * for every task forever. Artifacts come off `task.result_summary`, which the
 * worker really populates. See ResultSummary in types.ts.
 */
export interface AgentDetail {
  task: Task
  /** null means the event read FAILED. An empty array means there are none. */
  events: TaskEvent[] | null
  eventsDetail: string | null
}

export async function loadAgentDetail(taskId: string): Promise<Result<AgentDetail>> {
  if (USE_FIXTURES) return fixtureAgentDetail(taskId)

  const path = `/v1/tasks/${encodeURIComponent(taskId)}`
  const [task, events] = await Promise.all([
    read<{ task: Task } | Task>(path, () => false),
    read<{ events: TaskEvent[] }>(`${path}/events`, () => false),
  ])

  if (task.status === 'loading' || task.status === 'error') return task
  if (task.status === 'empty') {
    return {
      status: 'error',
      error: {
        kind: 'not_found',
        httpStatus: 404,
        code: 'not_found',
        message: 'This task does not exist, or it belongs to another tenant.',
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

async function fixtureAgentDetail(taskId: string): Promise<Result<AgentDetail>> {
  const page = await fixtureTasks()
  const task = page.status === 'ok' ? page.data.tasks.find((t) => t.id === taskId) : undefined
  if (!task) {
    return {
      status: 'error',
      error: {
        kind: 'not_found',
        httpStatus: 404,
        code: 'not_found',
        message: 'This task does not exist, or it belongs to another tenant.',
      },
    }
  }
  const at = (minsAgo: number) => new Date(Date.now() - minsAgo * 60_000).toISOString()
  // THE SAME ATTEMPT ID `fixtureAttempts` MINTS FOR ITS NEWEST ROW. It used to
  // be a differently-spelled id, so in development every event landed under
  // "an attempt no document describes" and the per-attempt joins -- checkpoint
  // sizes, live heartbeat readings, events grouped under their attempt -- were
  // the one part of the screen nobody ever saw working. A fixture that
  // exercises fewer code paths than production is a fixture that hides bugs.
  const attemptId = `att_${task.id.slice(-4)}_3`
  // `event_id` is a COUNTER, not the type: two heartbeats in one attempt are
  // ordinary and a type-derived id made React render duplicate keys the moment
  // this fixture grew a second one.
  let seq = 0
  const ev = (type: string, minsAgo: number, detail: Record<string, unknown> | null = null) => ({
    event_id: `${task.id}-${++seq}-${type}`,
    task_id: task.id,
    type,
    at: at(minsAgo),
    attempt_id: attemptId,
    lease_id: `lease-${task.id.slice(-4)}`,
    // The generation of the attempt these events belong to, matching
    // `fixtureAttempts`' newest row -- not the task's attempt counter, which is
    // a different number and made the timeline claim gen 1 under attempt 3.
    generation: 3,
    detail,
  })
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
                logs: {
                  stdout: 'gs://swarm-artifacts-dev/u-bogdan/logs/stdout.log',
                  stderr: 'gs://swarm-artifacts-dev/u-bogdan/logs/stderr.log',
                },
                runner: {
                  usage: {
                    input_tokens: 8,
                    output_tokens: 402,
                    total_cost_usd: 0.0642028,
                    models: ['claude-opus-5'],
                  },
                },
              }
            : task.result_summary,
      },
      events: [
        ev('ready', 12),
        ev('lease_acquired', 10),
        // The workaround for having no attempts/leases read path: the
        // DISPATCHED event's detail carries the execution name.
        ev('dispatched', 9, { execution_name: 'swarm-job-u-bogdan-claude-code-abc12', backend: 'CLOUD_RUN_JOB' }),
        ev('starting', 8),
        ev('running', 8),
        // Every fifth heartbeat carries a live resource reading
        // (lifecycle._heartbeat). It is the ONLY per-attempt measurement a
        // running agent has: `peak_rss_bytes` on the attempt document is
        // written at the end.
        ev('heartbeat', 6, { elapsed_seconds: 120.4, peak_rss_bytes: 1_610_612_736, checkpoints: 1 }),
        // The worker writes all four keys (control.record_checkpoint). The
        // attempt document stores only the id, so the size and the uri exist
        // HERE and nowhere else.
        ev('checkpoint_completed', 4, {
          checkpoint_id: 'ckpt-00001',
          uri: 'gs://swarm-artifacts-dev/u-bogdan/checkpoints/ckpt-00001/workspace.tar.zst',
          size_bytes: 41_235_988,
          seq: 1,
        }),
        ev('heartbeat', 3, { elapsed_seconds: 300.1, peak_rss_bytes: 1_842_000_000, checkpoints: 2 }),
        // Deliberately NO matching event for ckpt-00002, which `fixtureAttempts`
        // lists on the attempt: that is the real paging case -- an id with no
        // size and no uri, which must render as "the event is off this page",
        // not as a blank cell.
        ...(task.state === 'SUCCEEDED' ? [ev('succeeded', 1, { exit_code: 0 })] : []),
        ...(task.state === 'FAILED' ? [ev('failed', 1, { exit_code: 1, error: task.last_error })] : []),
      ],
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
 * under -- and it is a route rather than a table in this repository because
 * `check-contract-parity.sh` does not cover TypeScript, so a hand copy would
 * drift silently the first time a class is resized.
 *
 * There is no empty case: the catalogue always has three classes, so a 200
 * with an empty object is a failure wearing a success code, not a platform
 * with no sizes.
 */
export type ResourceClasses = Record<string, ResourceClassSpec>

export async function loadResourceClasses(): Promise<Result<{ resource_classes: ResourceClasses }>> {
  if (USE_FIXTURES) return fixtureResourceClasses()
  return read<{ resource_classes: ResourceClasses }>(
    '/v1/resource-classes',
    (d) => Object.keys(d.resource_classes ?? {}).length === 0,
  )
}

async function fixtureResourceClasses(): Promise<Result<{ resource_classes: ResourceClasses }>> {
  await new Promise((r) => setTimeout(r, 40))
  noteFixtureProbe('/v1/resource-classes', 40, true)
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    // The real three, so the requested-vs-utilised bars are exercised in
    // development rather than shipping having never been looked at.
    data: {
      resource_classes: {
        standard: { name: 'standard', cpu: 4, memory_gib: 8, disk_gib: 4, units: 1 },
        browser: { name: 'browser', cpu: 8, memory_gib: 16, disk_gib: 8, units: 2 },
        large: { name: 'large', cpu: 8, memory_gib: 32, disk_gib: 16, units: 4 },
      },
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

export async function loadAgentRun(taskId: string): Promise<Result<AgentRun>> {
  if (USE_FIXTURES) return fixtureAgentRun(taskId)

  const path = `/v1/tasks/${encodeURIComponent(taskId)}`
  const [task, events, attempts, classes] = await Promise.all([
    read<{ task: Task } | Task>(path, () => false),
    read<{ events: TaskEvent[] }>(`${path}/events`, () => false),
    read<{ attempts: AttemptRow[] }>(`${path}/attempts`, (d) => d.attempts.length === 0),
    loadResourceClasses(),
  ])

  if (task.status === 'loading' || task.status === 'error') return task
  if (task.status === 'empty') {
    return {
      status: 'error',
      error: {
        kind: 'not_found',
        httpStatus: 404,
        code: 'not_found',
        message: 'This task does not exist, or it belongs to another tenant.',
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
    attempts.status === 'ok' ? attempts.data.attempts
    : attempts.status === 'stale' ? attempts.data.attempts
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
      attempts: attempts.status === 'ok' ? attempts.data.attempts : null,
      attemptsDetail: attempts.status === 'ok' ? null : 'The fixture attempt read did not complete.',
      classes: classes.status === 'ok' ? classes.data.resource_classes : null,
      classesDetail: null,
      classesRouteMissing: false,
    },
  }
}

export async function loadStats(): Promise<Result<Stats>> {
  if (USE_FIXTURES) return fixtureStats()
  // A successful read always yields twelve numbers, because count_tasks_by_state
  // iterates the whole enum and writes a key for each. So there is no empty
  // state here -- and an error must never render as "0 RUNNING", because
  // "0 RUNNING" and "stats failed" are opposite facts.
  return read<Stats>('/v1/stats', () => false)
}

export async function loadDispatchControl(): Promise<Result<DispatchControl>> {
  if (USE_FIXTURES) return fixtureDispatch()
  // Admin-gated. A 403 here is information, not a failure.
  return read<DispatchControl>('/v1/admin/dispatch', () => false)
}

export async function loadProviders(): Promise<Result<ProvidersPage>> {
  if (USE_FIXTURES) return fixtureProviders()
  return read<ProvidersPage>('/v1/providers', (d) => d.providers.length === 0)
}

/** One page of tasks in one state. Server-side filter, backed by a real index. */
export async function loadTasksInState(state: TaskState): Promise<Result<TaskPage>> {
  if (USE_FIXTURES) {
    const page = await fixtureTasks()
    if (page.status !== 'ok') return page
    const tasks = page.data.tasks.filter((t) => t.state === state)
    return tasks.length === 0
      ? { status: 'empty', fetchedAt: Date.now() }
      : { status: 'ok', fetchedAt: Date.now(), data: { ...page.data, tasks } }
  }
  return read<TaskPage>(
    `/v1/tasks?state=${encodeURIComponent(state)}&limit=200`,
    (d) => d.tasks.length === 0,
  )
}

async function fixtureStats(): Promise<Result<Stats>> {
  await new Promise((r) => setTimeout(r, 220))
  noteFixtureProbe('/v1/stats', 220, true)
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      tenant_id: 'u-bogdan',
      dispatch_paused: false,
      tasks_by_state: {
        SUBMITTED: 0, QUEUED: 0, PARKED: 1, READY: 1, LEASED: 1, DISPATCHED: 1,
        STARTING: 1, RUNNING: 2, SUCCEEDED: 7, FAILED: 1, CANCELLED: 1,
        DEAD_LETTERED: 0,
      },
      limits: { max_batch_size: 100, max_input_bytes: 65536, max_workflow_steps: 50 },
      generated_at: new Date().toISOString(),
    },
  }
}

async function fixtureDispatch(): Promise<Result<DispatchControl>> {
  await new Promise((r) => setTimeout(r, 120))
  // A non-admin genuinely cannot read this, and the board must render that as
  // information rather than breakage. The fixture exercises that path.
  noteFixtureProbe('/v1/admin/dispatch', 120, false)
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
  noteFixtureProbe('/v1/providers', 200, true)
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      tenant_id: 'u-bogdan',
      generated_at: new Date().toISOString(),
      providers: [
        {
          provider: 'anthropic',
          credential_registered: true,
          runner_profiles: ['browser', 'claude-code'],
          quota: {
            provider: 'anthropic', tenant_id: 'u-bogdan', state: 'THROTTLED',
            updated_at: new Date(Date.now() - 40_000).toISOString(),
            configured_hard_max: 50, adaptive_target: 6, quota_derived_limit: null,
            requests_remaining: 118, tokens_remaining: null,
            reset_at: new Date(Date.now() + 900_000).toISOString(),
            cooldown_until: null,
            last_429_at: new Date(Date.now() - 240_000).toISOString(),
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

export async function loadMe(): Promise<Result<Me>> {
  if (USE_FIXTURES) return fixtureMe()
  return read<Me>('/v1/tenants/me', () => false)
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
    const page: Result<TaskPage> = await read<TaskPage>(
      `/v1/tasks?${qs.toString()}`,
      () => false,
    )
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

async function fixtureMe(): Promise<Result<Me>> {
  await new Promise((r) => setTimeout(r, 150))
  noteFixtureProbe('/v1/tenants/me', 150, true)
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      tenant: {
        tenant_id: 'u-bogdan', kind: 'user', principal: 'bogdan@saga.xyz',
        display_name: 'Bogdan', created_at: new Date(Date.now() - 86_400_000 * 9).toISOString(),
        max_active: 2, capacity_units: 40, monthly_budget_usd: null, enabled: true,
        credentials: ['anthropic'], service_account: 'swarm-agent-u-bogdan@saga-agents-staging.iam.gserviceaccount.com',
        gcs_prefix: 'gs://swarm-artifacts-dev/u-bogdan', namespace: 'swarm-u-bogdan',
      },
      principal: {
        email: 'bogdan@saga.xyz', domain: 'saga.xyz',
        groups: ['eng@saga.xyz'], is_admin: false,
      },
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

/** `GET /v1/admin/tenants`, admin-gated. A 403 here is information. */
export async function loadTenants(): Promise<Result<{ tenants: Tenant[] }>> {
  if (USE_FIXTURES) return fixtureTenants()
  return read<{ tenants: Tenant[] }>('/v1/admin/tenants', (d) => d.tenants.length === 0)
}

async function fixtureTenants(): Promise<Result<{ tenants: Tenant[] }>> {
  await new Promise((r) => setTimeout(r, 140))
  noteFixtureProbe('/v1/admin/tenants', 140, false)
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
  return read<LeasePage>('/v1/admin/leases?active_only=true&limit=200', (d) => d.leases.length === 0)
}

/** Every attempt of one task, newest first. Tenant-scoped. */
export async function loadAttempts(taskId: string): Promise<Result<{ attempts: AttemptRow[] }>> {
  if (USE_FIXTURES) return fixtureAttempts(taskId)
  return read<{ attempts: AttemptRow[] }>(
    `/v1/tasks/${encodeURIComponent(taskId)}/attempts`,
    (d) => d.attempts.length === 0,
  )
}

async function fixtureLeases(): Promise<Result<LeasePage>> {
  await new Promise((r) => setTimeout(r, 180))
  noteFixtureProbe('/v1/admin/leases', 180, true)
  const iso = (secAgo: number) => new Date(Date.now() - secAgo * 1000).toISOString()
  const row = (
    id: string,
    silent: number,
    extra: Partial<LeaseRow> = {},
  ): LeaseRow => ({
    lease_id: id,
    task_id: `task_${id}`,
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
        row('b1', 20),
        row('a1', 0, {
          dispatch_state: 'LEASED',
          dispatch_overdue: true,
          heartbeat_ever: false,
          heartbeat_at: null,
          silent_seconds: 480,
          last_error: 'BACKEND_REJECTED (attempt att_a1)',
        }),
      ],
      thresholds: { heartbeat_grace_seconds: 90, lease_timeout_seconds: 120 },
      evaluated_at: new Date().toISOString(),
      active_only: true,
      tenant_id: null,
      units_held: 4,
    },
  }
}

async function fixtureAttempts(taskId: string): Promise<Result<{ attempts: AttemptRow[] }>> {
  await new Promise((r) => setTimeout(r, 160))
  noteFixtureProbe(`/v1/tasks/{id}/attempts`, 160, true)
  const iso = (m: number) => new Date(Date.now() - m * 60_000).toISOString()
  // THE NEWEST ATTEMPT FOLLOWS THE TASK'S STATE. A fixture that always returned
  // a finished attempt meant the live-agent path -- no exit code, no
  // `peak_rss_bytes` yet, the resource figure coming off the newest heartbeat
  // event instead -- was never once looked at in development, and that is the
  // path someone opens this screen for when they are worried.
  const page = await fixtureTasks()
  const task = page.status === 'ok' ? page.data.tasks.find((t) => t.id === taskId) : undefined
  const live = task !== undefined && !TERMINAL_STATES.has(task.state)
  const mk = (n: number, exit: number | null, extra: Partial<AttemptRow> = {}): AttemptRow => ({
    attempt_id: `att_${taskId.slice(-4)}_${n}`,
    task_id: taskId,
    tenant_id: 'u-bogdan',
    generation: n,
    lease_id: `lease_${n}`,
    backend: 'CLOUD_RUN_JOB',
    execution_name: `swarm-job-u-bogdan-claude-code-${n}`,
    created_at: iso(40 - n * 10),
    started_at: iso(39 - n * 10),
    completed_at: exit === null ? null : iso(35 - n * 10),
    exit_code: exit,
    error: exit ? 'exit status 1' : null,
    peak_rss_bytes: 1_842_000_000,
    peak_disk_bytes: null,
    oom_near_miss: n === 2,
    checkpoints: [],
    input_tokens: null,
    output_tokens: null,
    cache_read_input_tokens: null,
    cache_creation_input_tokens: null,
    cost_usd: null,
    ...extra,
  })
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      attempts: [
        // The newest attempt carries the measurements a current worker image
        // records, so the requested-vs-utilised bars and the spend columns are
        // exercised in development. `ckpt-00002` deliberately has no
        // `checkpoint_completed` event in fixtureAgentDetail: an id with no
        // size and no uri is the ordinary paging case, not a defect.
        mk(3, live ? null : 0, {
          checkpoints: ['ckpt-00001', 'ckpt-00002'],
          // A live attempt has written NO final measurement and no spend: both
          // are written when it ends. The screen falls back to the heartbeat
          // for memory and says five different things about the rest.
          ...(live
            ? { completed_at: null, peak_rss_bytes: null, peak_disk_bytes: null }
            : {
                peak_disk_bytes: 2_147_483_648,
                input_tokens: 18_422,
                output_tokens: 7_311,
                cache_read_input_tokens: 412_880,
                cache_creation_input_tokens: 21_004,
                cost_usd: 0.418_2,
              }),
        }),
        // The older two ran before usage capture landed: every spend figure is
        // null, which must render as em dashes and never as $0.00.
        mk(2, 1, { peak_rss_bytes: 7_730_941_132 }),
        // Never started: dispatch was rejected, so there is no exit code and no
        // measurement at all. It must not read as "exit 0" or as 0 bytes.
        mk(1, null, {
          peak_rss_bytes: null,
          started_at: null,
          completed_at: null,
          execution_name: null,
          error: 'BACKEND_REJECTED',
        }),
      ],
    },
  }
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
    read<LeasePage>('/v1/admin/leases?active_only=true&limit=200', (d) => d.leases.length === 0),
    read<Capacity>('/v1/capacity', () => false),
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

  return {
    status: leases.status,
    fetchedAt: leases.fetchedAt,
    data: { page: leases.data, pools, poolsDetail: detail },
  } as Result<HoldersBoard>
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
  return read<{ quota: QuotaState[] }>('/v1/admin/quota', (d) => d.quota.length === 0)
}

async function fixtureAdminQuota(): Promise<Result<{ quota: QuotaState[] }>> {
  await new Promise((r) => setTimeout(r, 170))
  noteFixtureProbe('/v1/admin/quota', 170, true)
  const q = (
    provider: string,
    tenant: string,
    state: string,
    extra: Partial<QuotaState> = {},
  ): QuotaState => ({
    provider, tenant_id: tenant, state,
    updated_at: new Date(Date.now() - 60_000).toISOString(),
    configured_hard_max: 50, adaptive_target: null, quota_derived_limit: null,
    requests_remaining: null, tokens_remaining: null, reset_at: null,
    cooldown_until: null, last_429_at: null, retry_after_seconds: null,
    success_count: 120, rate_limit_count: 0, effective_limit: 50,
    ...extra,
  })
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      // One of every treatment, so none of them ships unlooked-at: a healthy
      // row, THROTTLED (the one a fallthrough would mislabel), an effective
      // limit of 0 that is a FACT, and UNKNOWN which is not healthy.
      quota: [
        q('anthropic', 'eng', 'AVAILABLE', { requests_remaining: 4210 }),
        q('anthropic', 'u-bogdan', 'THROTTLED', {
          adaptive_target: 6, effective_limit: 6, rate_limit_count: 9,
          last_429_at: new Date(Date.now() - 240_000).toISOString(),
        }),
        q('openai', 'eng', 'EXHAUSTED', { effective_limit: 0, rate_limit_count: 41 }),
        q('openai', 'u-bogdan', 'UNKNOWN', { effective_limit: 0, success_count: 0 }),
      ],
    },
  }
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
  let path: string | null = null

  if (poolName === 'global') path = '/v1/admin/limits/global'
  else if (parts[0] === 'tenant' && parts[1]) path = `/v1/admin/limits/tenant/${encodeURIComponent(parts[1])}`
  else if (parts[0] === 'resource' && parts[1]) path = `/v1/admin/limits/resource/${encodeURIComponent(parts[1])}`
  else if (parts[0] === 'runner' && parts[1]) path = `/v1/admin/limits/runner/${encodeURIComponent(parts[1])}`
  else if (parts[0] === 'backend' && parts[1]) path = `/v1/admin/limits/backend/${encodeURIComponent(parts[1])}`
  else if (parts[0] === 'provider' && parts[1] && parts[2] === 'tenant' && parts[3])
    path = `/v1/admin/limits/provider/${encodeURIComponent(parts[1])}/tenant/${encodeURIComponent(parts[3])}`
  else if (parts[0] === 'provider' && parts[1]) path = `/v1/admin/limits/provider/${encodeURIComponent(parts[1])}`

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
    return { status: 'ok', fetchedAt: Date.now(), data: { pool: poolName, limit } }
  }

  return write(path, 'PUT', { limit })
}

// --------------------------------------------------------------------------
// Fixtures
// --------------------------------------------------------------------------
// These are the REAL pool names and the REAL live values read from the dev
// platform on 2026-09-19, not invented ones -- including tenant:u-bogdan at 40
// against a configured 2, which is the out-of-band drift `make pool-check`
// found. A fixture that shows a tidy platform teaches the wrong thing about
// what this screen is for.

async function fixtureCapacity(): Promise<Result<Capacity>> {
  await new Promise((r) => setTimeout(r, 400))
  noteFixtureProbe('/v1/capacity', 400, true)
  const pool = (
    name: string,
    hard: number,
    active: number,
    extra: Partial<Capacity['pools'][number]> = {},
  ) => ({
    name,
    hard_limit: hard,
    adaptive_target: null,
    quota_derived_limit: null,
    effective_limit: hard,
    active,
    available: Math.max(0, hard - active),
    enabled: true,
    updated_at: new Date().toISOString(),
    ...extra,
  })

  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      generated_at: new Date().toISOString(),
      // The real five from the frozen catalogue, with the pool lists
      // pool_names_for builds for one tenant. Not invented: an empty map here
      // meant the headroom rows never rendered in development, which is how a
      // panel ships untested.
      runner_profiles: {
        mock: {
          resource_class: 'standard', backend: 'CLOUD_RUN_JOB', provider: null, units: 1,
          pools: ['global', 'tenant:u-bogdan', 'resource:standard', 'runner:mock', 'backend:CLOUD_RUN_JOB'],
        },
        generic: {
          resource_class: 'standard', backend: 'CLOUD_RUN_JOB', provider: null, units: 1,
          pools: ['global', 'tenant:u-bogdan', 'resource:standard', 'runner:generic', 'backend:CLOUD_RUN_JOB'],
        },
        'claude-code': {
          resource_class: 'standard', backend: 'CLOUD_RUN_JOB', provider: 'anthropic', units: 1,
          pools: [
            'global', 'tenant:u-bogdan', 'resource:standard', 'runner:claude-code',
            'backend:CLOUD_RUN_JOB', 'provider:anthropic', 'provider:anthropic:tenant:u-bogdan',
          ],
        },
        codex: {
          resource_class: 'standard', backend: 'CLOUD_RUN_JOB', provider: 'openai', units: 1,
          pools: [
            'global', 'tenant:u-bogdan', 'resource:standard', 'runner:codex',
            'backend:CLOUD_RUN_JOB', 'provider:openai',
          ],
        },
        browser: {
          resource_class: 'browser', backend: 'GKE_AUTOPILOT', provider: 'anthropic', units: 2,
          pools: [
            'global', 'tenant:u-bogdan', 'resource:browser', 'runner:browser',
            'backend:GKE_AUTOPILOT', 'provider:anthropic', 'provider:anthropic:tenant:u-bogdan',
          ],
        },
      },
      pools: [
        pool('global', 20, 5),
        pool('tenant:u-bogdan', 40, 5),
        pool('provider:anthropic', 10, 5),
        pool('provider:anthropic:tenant:u-bogdan', 5, 5),
        pool('provider:anthropic:tenant:eng', 5, 0),
        pool('provider:openai', 10, 0),
        pool('provider:openai:tenant:eng', 5, 0),
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
        pool('tenant:eng', 10, 0, { enabled: false, adaptive_target: 6, effective_limit: 6 }),
      ],
    },
  }
}

async function fixtureTasks(): Promise<Result<TaskPage>> {
  await new Promise((r) => setTimeout(r, 350))
  noteFixtureProbe('/v1/tasks', 350, true)
  const now = Date.now()
  const at = (minsAgo: number) => new Date(now - minsAgo * 60_000).toISOString()

  // The fan-out that actually ran on 2026-09-19: fourteen script fixes, one
  // failure reclaimed three times, plus a workflow. Real shapes, real states.
  const mk = (
    id: string,
    state: TaskState,
    profile: string,
    minsAgo: number,
    extra: Partial<Task> = {},
  ): Task => ({
    id,
    tenant_id: 'u-bogdan',
    state,
    runner_profile: profile,
    resource_class: profile === 'browser' ? 'browser' : 'standard',
    provider: profile === 'mock' ? null : 'anthropic',
    priority: 0,
    created_at: at(minsAgo),
    updated_at: at(Math.max(0, minsAgo - 6)),
    started_at: state === 'QUEUED' || state === 'READY' ? null : at(minsAgo - 1),
    completed_at: ['SUCCEEDED', 'FAILED', 'CANCELLED'].includes(state) ? at(minsAgo - 6) : null,
    submitted_by: 'bogdan@saga.xyz',
    attempt_count: state === 'FAILED' ? 3 : 1,
    max_attempts: 3,
    park_reason: null,
    blocked_by: null,
    workflow_id: null,
    step_id: null,
    depends_on: null,
    cancel_requested: false,
    repository_url: 'https://github.com/bogdan-alexandrescu/SwarmCloud',
    model: profile === 'mock' ? null : 'claude-opus-5',
    timeout_seconds: 3600,
    // Only a PARKED task has one. Left null elsewhere on purpose: a date here
    // on a RUNNING task would imply a retry that is not scheduled.
    next_eligible_at: state === 'PARKED' ? at(minsAgo - 15) : null,
    metadata: null,
    repository_ref: 'main',
    input: null,
    last_error: state === 'FAILED' ? 'exit status 1: pytest collected 2 failures' : null,
    result_summary: state === 'SUCCEEDED' ? { artifacts: 1, checkpoints: 2 } : null,
    latest_checkpoint:
      state === 'SUCCEEDED' || state === 'PARKED'
        ? 'gs://swarm-artifacts-dev/u-bogdan/checkpoints/ckpt-3.tar.zst'
        : null,
    ...extra,
  })

  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      tasks: [
        mk('task_a073aff5', 'RUNNING', 'claude-code', 4),
        mk('task_8ea5c26d', 'RUNNING', 'claude-code', 5),
        mk('task_19d91b82', 'DISPATCHED', 'claude-code', 3),
        mk('task_92eb4807', 'STARTING', 'claude-code', 2),
        mk('task_460409cd', 'LEASED', 'claude-code', 1),
        mk('task_950b8e4a', 'READY', 'claude-code', 1),
        mk('task_e7d210b6', 'QUEUED', 'claude-code', 0),
        mk('task_8e33a3de', 'FAILED', 'claude-code', 42),
        mk('task_a3881bec', 'SUCCEEDED', 'claude-code', 58),
        mk('task_99284d38', 'SUCCEEDED', 'claude-code', 50),
        mk('task_f278adec', 'SUCCEEDED', 'claude-code', 48),
        mk('task_f523c160', 'SUCCEEDED', 'claude-code', 47),
        mk('task_812d51aa', 'SUCCEEDED', 'claude-code', 46),
        mk('task_108ef29c', 'SUCCEEDED', 'mock', 90),
        mk('task_1beb89a5', 'CANCELLED', 'claude-code', 62),
        // Part of a workflow, so the graph view has something with edges.
        mk('task_wf_plan', 'SUCCEEDED', 'claude-code', 30, {
          workflow_id: 'wf_audit_01', step_id: 'plan', depends_on: [],
        }),
        mk('task_wf_scan_a', 'SUCCEEDED', 'claude-code', 24, {
          workflow_id: 'wf_audit_01', step_id: 'scan-scripts', depends_on: ['plan'],
        }),
        mk('task_wf_scan_b', 'RUNNING', 'claude-code', 24, {
          workflow_id: 'wf_audit_01', step_id: 'scan-terraform', depends_on: ['plan'],
        }),
        // PARKED, not "BLOCKED". There is no BLOCKED task state -- a step
        // waiting on a dependency is PARKED with DEPENDENCY_INCOMPLETE.
        mk('task_wf_report', 'PARKED', 'claude-code', 24, {
          workflow_id: 'wf_audit_01', step_id: 'report',
          depends_on: ['scan-scripts', 'scan-terraform'],
          park_reason: 'DEPENDENCY_INCOMPLETE',
        }),
      ],
      next_page_token: null,
      tenant_id: 'u-bogdan',
    },
  }
}

async function fixtureWorkflowBoard(): Promise<Result<WorkflowBoard>> {
  await new Promise((r) => setTimeout(r, 300))
  noteFixtureProbe('/v1/workflows', 300, true)
  // A route a non-admin genuinely cannot read, so the strip's 403 cell -- the
  // one that must read as information rather than breakage -- is visible in
  // development instead of only in production.
  noteFixtureProbe('/v1/admin/dispatch', 120, false)

  // Reuse the task fixture so the join is a REAL join: if a step_id or task_id
  // stops matching, the fixture shows "state unknown" exactly as production
  // would, instead of quietly carrying a state of its own.
  const tasks = await fixtureTasks()
  const taskById = new Map<string, Task>()
  if (tasks.status === 'ok') for (const t of tasks.data.tasks) taskById.set(t.id, t)

  const step = (
    step_id: string,
    depends_on: string[],
    task_id: string | null,
    input_from: string | null = null,
  ) => ({
    step_id,
    runner_profile: 'claude-code',
    resource_class: 'standard',
    depends_on,
    input_from,
    timeout_seconds: 3600,
    task_id,
  })

  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      taskById,
      statesDetail: null,
      workflows: [
        {
          workflow_id: 'wf_audit_01',
          tenant_id: 'u-bogdan',
          state: 'RUNNING',
          created_at: new Date(Date.now() - 30 * 60_000).toISOString(),
          updated_at: new Date().toISOString(),
          submitted_by: 'bogdan@saga.xyz',
          priority: 0,
          on_step_failure: 'FAIL_WORKFLOW',
          cancel_requested: false,
          steps: [
            step('plan', [], 'task_wf_plan'),
            step('scan-scripts', ['plan'], 'task_wf_scan_a', 'plan'),
            step('scan-terraform', ['plan'], 'task_wf_scan_b', 'plan'),
            step('report', ['scan-scripts', 'scan-terraform'], 'task_wf_report'),
            // No task_id: the workflow has not reached it. Renders as
            // "not started", which is NOT the same as "state unknown".
            step('publish', ['report'], null),
          ],
        },
      ],
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
// PUT /{id}/lending, PUT /{id}/state and DELETE /{id}; the `AccountPool`
// protocol in `brokerclient.py` -- which is "deliberately not a generic HTTP
// client", so there is no fallthrough -- has a method for each of those and
// for nothing else.
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
}

export async function loadAccountsBoard(): Promise<Result<AccountsBoard>> {
  if (USE_FIXTURES) return fixtureAccountsBoard()

  const [accounts, tenants] = await Promise.all([
    // `() => false`, NOT `accounts.length === 0`, and the reason is the whole
    // point of the screen: Screen renders `empty` INSTEAD of its children, so
    // an empty pool would hide the register form -- the one control that fixes
    // an empty pool. Zero accounts is handled inside the body as a state panel
    // sitting above a form that is still there.
    read<AccountsPage>('/v1/accounts', () => false),
    read<{ tenants: Tenant[] }>('/v1/admin/tenants', () => false),
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
    : await write('/v1/accounts/authorize', 'POST', body)
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
    : await write('/v1/accounts/exchange', 'POST', body)
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
  return write(`/v1/accounts/${encodeURIComponent(accountId)}/lending`, 'PUT', { lend_to: lendTo })
}

/** `PUT /v1/accounts/{id}/state`. `reason` is for the next person, so it is required here. */
export async function setAccountState(
  accountId: string,
  state: AccountStateName,
  reason: string,
): Promise<Result<unknown>> {
  if (USE_FIXTURES) return fixtureState(accountId, state, reason)
  return write(`/v1/accounts/${encodeURIComponent(accountId)}/state`, 'PUT', { state, reason })
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
    : await write(`/v1/accounts/${encodeURIComponent(accountId)}/refresh`, 'POST')
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
  return write(`/v1/accounts/${encodeURIComponent(accountId)}`, 'DELETE')
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

const ISO = (msFromNow: number): string => new Date(Date.now() + msFromNow).toISOString()

function fixtureAccount(
  owner: string,
  label: string,
  extra: Partial<Account> = {},
): Account {
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
    ...extra,
  }
}

let fixtureAccounts: Account[] = [
  fixtureAccount('u-bogdan', 'primary', {
    assigned: 2,
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
    windows: {
      five_hour: { utilization: 0.88, resets_at: ISO(19 * 3600_000 + 43 * 60_000), reset: false },
      seven_day: { utilization: 0.12, resets_at: ISO(94 * 3600_000), reset: false },
    },
  }),
  fixtureAccount('u-bogdan', 'weekly-burn', {
    state: 'PAUSED',
    reason: 'held back for the release run on Monday',
    windows: {
      // Already past its reset: the figure describes a window that has refilled.
      five_hour: { utilization: 0.97, resets_at: ISO(-6 * 60_000), reset: true },
      seven_day: { utilization: 0.94, resets_at: ISO(70 * 3600_000), reset: false },
    },
  }),
  fixtureAccount('u-bogdan', 'never-polled', {
    // Registered, never observed. Utilisation is UNKNOWN, emphatically not 0%.
    observed_at: null,
    stale: true,
    windows: {},
  }),
  fixtureAccount('u-bogdan', 'retired-laptop', {
    state: 'REAUTH_REQUIRED',
    reason: 'refresh failed: reauth_required',
    lend_to: ['eng'],
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
    windows: {
      // Only one window came back. The 7D column says so rather than showing 0%.
      five_hour: { utilization: 0.56, resets_at: ISO(3600_000 + 12 * 60_000), reset: false },
    },
  }),
]

/** The tenant the fixture's verified token would carry. */
const FIXTURE_TENANT = 'u-bogdan'

async function fixtureAccountsBoard(): Promise<Result<AccountsBoard>> {
  await new Promise((r) => setTimeout(r, 210))
  noteFixtureProbe('/v1/accounts', 210, true)
  // The admin tenant list is the one read a non-admin genuinely cannot do, and
  // the fixture exercises that branch rather than the happy one -- otherwise
  // the "no picker, and here is why" copy ships unlooked-at.
  noteFixtureProbe('/v1/admin/tenants', 90, false, 'admin_required')
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      // `eng:shared` is in the list because it is LENT to this tenant, which is
      // what `for_tenant` returns. It is deliberately not removable, pausable
      // or refreshable from here, and the fixture is what makes that path
      // visible in development.
      page: { accounts: fixtureAccounts.map((a) => ({ ...a })), tenant_id: FIXTURE_TENANT },
      tenants: null,
      tenantsDetail: 'Admin group membership is required for this endpoint.',
      readAt: Date.now(),
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
  noteFixtureProbe('/v1/accounts/authorize', 260, true)
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
  noteFixtureProbe('/v1/accounts/exchange', 480, true)
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
  const refreshed: Account = { ...account, observed_at: new Date().toISOString(), stale: false }
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
  return read<AccountsPage>('/v1/accounts', (d) => d.accounts.length === 0)
}

async function fixtureAccountPool(): Promise<Result<AccountsPage>> {
  await new Promise((r) => setTimeout(r, 190))
  noteFixtureProbe('/v1/accounts', 190, true)
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: { accounts: fixtureAccounts.map((a) => ({ ...a })), tenant_id: FIXTURE_TENANT },
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
 * Attempts for the spend fixture.
 *
 * Separate from `fixtureAttempts` above, which reports null for every usage
 * field -- a faithful picture of an attempt that ran before usage capture, and
 * the right default for the attempt timeline. Summing those would show the
 * spend panel only in its "nothing measured" state, so the one state that
 * ships unlooked-at would be the ordinary one.
 *
 * The mix here is the one the panel has to survive: figures present on the
 * newest attempt, absent on a retry, and a task whose attempts carry tokens
 * but no cost at all.
 */
async function fixtureSpendAttempts(task: Task): Promise<Result<{ attempts: AttemptRow[] }>> {
  await new Promise((r) => setTimeout(r, 90))
  noteFixtureProbe(`/v1/tasks/{id}/attempts`, 90, true)
  const seed = task.id.length + task.attempt_count
  const mk = (n: number, usage: Partial<AttemptRow>): AttemptRow => ({
    attempt_id: `${task.id}-a${n}`,
    task_id: task.id,
    tenant_id: task.tenant_id,
    generation: n,
    lease_id: `lease-${task.id}-${n}`,
    backend: 'CLOUD_RUN_JOB',
    execution_name: null,
    created_at: task.created_at,
    started_at: task.started_at,
    completed_at: task.completed_at,
    exit_code: null,
    error: null,
    peak_rss_bytes: null,
    peak_disk_bytes: null,
    oom_near_miss: false,
    checkpoints: [],
    input_tokens: null,
    output_tokens: null,
    cache_read_input_tokens: null,
    cache_creation_input_tokens: null,
    cost_usd: null,
    ...usage,
  })

  // `mock` and `generic` runners report nothing at all -- the frozen catalogue
  // says only claude-code and codex produce usage -- so their attempts stay
  // all-null and the panel has to count them as unmeasured rather than free.
  if (task.runner_profile === 'mock' || task.runner_profile === 'generic') {
    return { status: 'ok', fetchedAt: Date.now(), data: { attempts: [mk(1, {})] } }
  }

  const rows = [
    mk(task.attempt_count, {
      input_tokens: 14_000 + seed * 137,
      output_tokens: 2_100 + seed * 31,
      cache_read_input_tokens: 96_000 + seed * 811,
      cache_creation_input_tokens: 11_500 + seed * 57,
      cost_usd: 0.18 + (seed % 7) * 0.043,
    }),
  ]
  // A retry that predates usage capture: real attempt, no figures on it.
  if (task.attempt_count > 1) rows.push(mk(task.attempt_count - 1, {}))
  return { status: 'ok', fetchedAt: Date.now(), data: { attempts: rows } }
}
