import type { Capacity, Task, TaskPage, TaskState, Workflow } from './types'

// THE ONE RULE THIS FILE EXISTS TO ENFORCE
// ----------------------------------------
// A failed request must never be representable as empty data.
//
// That is not a general principle here, it is this platform's defining bug. On
// 2026-09-18 a sweep of its operational scripts confirmed 56 places where a
// probe failure was rendered as an absence: `status.sh` printed "no swarm
// services deployed" when a session had expired, and finished on "nothing needs
// attention". The same day a build wrote a manifest listing zero images and
// reported "ok built 6 image(s)".
//
// So `load` returns a discriminated union and there is no way to reach the rows
// without saying what you will do when there are none AND what you will do when
// the answer never arrived. A component cannot accidentally render `[]` for a
// 403, because a 403 never produces an array.

export type Loaded<T> =
  | { status: 'loading' }
  | { status: 'ok'; data: T; fetchedAt: Date }
  /**
   * The request did not produce an answer. `detail` is for the person reading
   * the screen, and must say what failed rather than what it implies -- "could
   * not read capacity" not "no capacity configured".
   */
  | { status: 'failed'; detail: string; hint?: string }

/** Fixtures, so the UI can be worked on before DNS resolves. */
const USE_FIXTURES = import.meta.env.DEV && !import.meta.env.VITE_LIVE

async function request<T>(path: string): Promise<Loaded<T>> {
  try {
    const res = await fetch(path, {
      headers: { accept: 'application/json' },
      // Behind IAP the browser already holds the session cookie; nothing here
      // handles tokens, and nothing here should.
      credentials: 'same-origin',
    })

    if (res.status === 401 || res.status === 403) {
      return {
        status: 'failed',
        detail: `The API refused this request (HTTP ${res.status}).`,
        hint: 'Your IAP session may have expired. Reloading the page will re-authenticate.',
      }
    }
    if (!res.ok) {
      let body = ''
      try {
        body = (await res.text()).slice(0, 200)
      } catch {
        // A body we cannot read is not a body that changes the diagnosis.
      }
      return {
        status: 'failed',
        detail: `The API returned HTTP ${res.status}.`,
        hint: body || undefined,
      }
    }
    return { status: 'ok', data: (await res.json()) as T, fetchedAt: new Date() }
  } catch (err) {
    // Network failure, DNS, CORS, an aborted request. Emphatically NOT "no data".
    return {
      status: 'failed',
      detail: 'Could not reach the API.',
      hint: err instanceof Error ? err.message : undefined,
    }
  }
}

export async function loadCapacity(): Promise<Loaded<Capacity>> {
  if (USE_FIXTURES) return fixtureCapacity()
  return request<Capacity>('/v1/capacity')
}

export async function loadTasks(): Promise<Loaded<TaskPage>> {
  if (USE_FIXTURES) return fixtureTasks()
  // Page size caps at 200 server-side (deps.py). Asking for more is silently
  // clamped, which would make "200 tasks" look like the whole truth.
  return request<TaskPage>('/v1/tasks?limit=200')
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

export async function loadWorkflowBoard(): Promise<Loaded<WorkflowBoard>> {
  if (USE_FIXTURES) return fixtureWorkflowBoard()

  const [wf, tasks] = await Promise.all([
    request<{ workflows: Workflow[] }>('/v1/workflows?limit=100'),
    request<TaskPage>('/v1/tasks?limit=200'),
  ])

  // The workflow read is the one that decides whether there is a screen at all.
  if (wf.status !== 'ok') return wf

  if (tasks.status !== 'ok') {
    return {
      status: 'ok',
      fetchedAt: wf.fetchedAt,
      data: {
        workflows: wf.data.workflows,
        taskById: null,
        statesDetail:
          tasks.status === 'failed'
            ? tasks.detail
            : 'The task read did not complete.',
      },
    }
  }

  const taskById = new Map<string, Task>()
  for (const t of tasks.data.tasks) taskById.set(t.id, t)
  return {
    status: 'ok',
    fetchedAt: wf.fetchedAt,
    data: { workflows: wf.data.workflows, taskById, statesDetail: null },
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

async function fixtureCapacity(): Promise<Loaded<Capacity>> {
  await new Promise((r) => setTimeout(r, 400))
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
    fetchedAt: new Date(),
    data: {
      generated_at: new Date().toISOString(),
      runner_profiles: {},
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

async function fixtureTasks(): Promise<Loaded<TaskPage>> {
  await new Promise((r) => setTimeout(r, 350))
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
    ...extra,
  })

  return {
    status: 'ok',
    fetchedAt: new Date(),
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
      next_cursor: null,
    },
  }
}

async function fixtureWorkflowBoard(): Promise<Loaded<WorkflowBoard>> {
  await new Promise((r) => setTimeout(r, 300))

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
    fetchedAt: new Date(),
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
