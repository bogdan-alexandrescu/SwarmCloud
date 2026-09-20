import { noteFixtureProbe, read, type Result } from './fetch'
import type {
  Capacity, DispatchControl, Me, ProvidersPage, Stats, Task, TaskEvent, TaskPage,
  TaskState, TaskWindow, Tenant, Workflow,
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

  // GET /v1/tasks/{id} returns the task document itself; tolerate a wrapper in
  // case a caller routes through the create shape.
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
  const ev = (type: string, minsAgo: number, detail: Record<string, unknown> | null = null) => ({
    event_id: `${task.id}-${type}`,
    task_id: task.id,
    type,
    at: at(minsAgo),
    attempt_id: `att-${task.id.slice(-4)}`,
    lease_id: `lease-${task.id.slice(-4)}`,
    generation: task.attempt_count,
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
        ev('checkpoint_completed', 4, { checkpoint_id: 'ckpt-3' }),
        ...(task.state === 'SUCCEEDED' ? [ev('succeeded', 1, { exit_code: 0 })] : []),
        ...(task.state === 'FAILED' ? [ev('failed', 1, { exit_code: 1, error: task.last_error })] : []),
      ],
      eventsDetail: null,
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
