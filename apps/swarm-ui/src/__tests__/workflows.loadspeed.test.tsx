/**
 * THE WORKFLOWS PAGES MAKE NO READ BEYOND THE LIST (console load speed,
 * owner's pick 2026-11-01). Measured in the owner's browser: the list gated
 * on `GET /v1/tasks?limit=200` (328 KB, 3.8 s), then read 18 workflows by id
 * as their rows scrolled in and 12 tasks' attempts for the rows' cost. The
 * list's step blocks (#2993) carry each step's state, names and totals, so:
 *
 *  - the list reads no workflow by id and no attempts, scrolled or searched;
 *  - one workflow's page reads its own workflow, the newest `AROUND_LIMIT`
 *    workflows around it, and its own steps' attempts -- no task list.
 *
 * MUTATIONS: put `useRowReads` back on the list; put the list's
 * `useWorkflowUsage` back; read the board around a page at the list's size
 * or with a task read; hand the page's attempt read the neighbours' steps.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'

import type { Result } from '../fetch'
import type { WorkflowBoard, WorkflowUsage } from '../api'
import type { Task, TaskState, Workflow, WorkflowStep } from '../types'
import { ownFromBoard } from './workflowsById'

const api = vi.hoisted(() => ({
  loadWorkflowBoard: vi.fn(),
  loadWorkflowUsage: vi.fn(),
  loadWorkflow: vi.fn(),
  loadTasks: vi.fn(),
  loadAttempts: vi.fn(),
  loadOlderWorkflows: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => {
  const real = await importOriginal<typeof import('../api')>()
  return { ...real, ...api }
})

const { WorkflowsScreen, AROUND_LIMIT } = await import('../Workflows')

const T0 = Date.parse('2026-11-01T12:00:00.000Z')
const iso = (offsetSeconds: number) => new Date(T0 + offsetSeconds * 1000).toISOString()

function step(step_id: string, depends_on: string[], over: Partial<WorkflowStep> = {}): WorkflowStep {
  return { step_id, runner_profile: 'claude-code', resource_class: 'standard', depends_on, input_from: {}, task_id: null, ...over }
}

function task(id: string, state: TaskState, over: Partial<Task> = {}): Task {
  return {
    id,
    tenant_id: 'eng',
    state,
    runner_profile: 'claude-code',
    resource_class: 'standard',
    provider: 'anthropic',
    priority: 0,
    created_at: iso(-600),
    updated_at: iso(-60),
    started_at: iso(-500),
    completed_at: state === 'SUCCEEDED' ? iso(-100) : null,
    submitted_by: 'priya@example.com',
    attempt_count: 1,
    max_attempts: 3,
    park_reason: null,
    blocked_by: null,
    workflow_id: null,
    step_id: null,
    depends_on: null,
    cancel_requested: false,
    repository_url: null,
    model: null,
    timeout_seconds: null,
    next_eligible_at: null,
    metadata: null,
    repository_ref: null,
    input: null,
    last_error: null,
    result_summary: null,
    latest_checkpoint: null,
    current_generation: 1,
    current_lease_id: null,
    ...over,
  }
}

function workflow(id: string, state: TaskState, steps: WorkflowStep[], ageSeconds = 600): Workflow {
  return {
    workflow_id: id,
    state,
    tenant_id: 'eng',
    stored_state: state,
    state_source: 'derived',
    rollup: { state, complete: true, reason: 'test', counts: {}, unreadable_steps: [], unstarted_steps: [], steps_read: steps.length },
    created_at: iso(-ageSeconds),
    updated_at: iso(-60),
    submitted_by: 'priya@example.com',
    priority: 0,
    on_step_failure: 'fail_workflow',
    cancel_requested: false,
    steps,
  }
}

/**
 * Two workflows of the same shape. `wf_old`'s step is not in the join: the
 * list read served no block for it, the case `useRowReads` read by id.
 */
function board(): WorkflowBoard {
  const fresh = workflow('wf_fresh', 'SUCCEEDED', [step('build', [], { task_id: 't_fresh' })])
  const old = workflow('wf_old', 'SUCCEEDED', [step('build', [], { task_id: 't_old' })], 7200)
  return {
    workflows: [fresh, old],
    taskById: new Map([['t_fresh', task('t_fresh', 'SUCCEEDED', { metadata: { title: 'Fresh build' }, cost_usd_total: 0.5, attempts: 1, attempts_read: 'ok' })]]),
    statesDetail: null,
  }
}

/** Every row reports itself in view, as a tall screen would. */
class InView {
  constructor(readonly callback: IntersectionObserverCallback) {}
  observe(el: Element): void {
    const entry = { target: el, isIntersecting: true, intersectionRatio: 1 } as unknown as IntersectionObserverEntry
    queueMicrotask(() => this.callback([entry], this as unknown as IntersectionObserver))
  }
  unobserve(): void {}
  disconnect(): void {}
  takeRecords(): IntersectionObserverEntry[] {
    return []
  }
}

const fetched: string[] = []

beforeEach(() => {
  vi.spyOn(Date, 'now').mockReturnValue(T0)
  vi.stubGlobal('IntersectionObserver', InView)
  fetched.length = 0
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      fetched.push(String(input))
      return new Response('{}', { status: 404 })
    }),
  )
  api.loadWorkflowBoard.mockResolvedValue({ status: 'ok', data: board(), fetchedAt: T0 } satisfies Result<WorkflowBoard>)
  api.loadWorkflowUsage.mockResolvedValue({ status: 'empty', fetchedAt: T0 } satisfies Result<WorkflowUsage>)
  // The page's own read, served off a board of its own: a double that called
  // `loadWorkflowBoard` would count as a board read the page made.
  const own = board()
  api.loadWorkflow.mockImplementation(ownFromBoard(async () => ({ status: 'ok', data: own, fetchedAt: T0 })))
  api.loadTasks.mockResolvedValue({ status: 'empty', fetchedAt: T0 })
  try {
    window.localStorage.clear()
  } catch {
    /* storage refused */
  }
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.clearAllMocks()
})

const rowName = (id: string) =>
  document.querySelector(`.wfl-table tbody tr[data-workflow="${id}"] .wfl-line > a`)?.textContent ?? null

describe('the list: no per-row read', () => {
  it('reads no workflow by id and no attempts, with every row in view and a search typed', async () => {
    render(<WorkflowsScreen />)
    await waitFor(() => expect(rowName('wf_fresh')).toBe('Fresh build'))
    expect(rowName('wf_old')).toBe('wf_old')
    // Scrolled in and searched: the list still reads nothing more.
    await act(async () => {
      await new Promise((r) => setTimeout(r, 20))
    })
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'nothing-matches' } })
    await act(async () => {
      await new Promise((r) => setTimeout(r, 20))
    })
    expect(api.loadWorkflow).not.toHaveBeenCalled()
    expect(api.loadWorkflowUsage).not.toHaveBeenCalled()
    expect(api.loadTasks).not.toHaveBeenCalled()
    expect(api.loadWorkflowBoard).toHaveBeenCalledTimes(1)
    expect(fetched.filter((u) => u.includes('/v1/tasks') || u.includes('/v1/workflows'))).toEqual([])
  })

  it('totals a row’s cost from its steps’ blocks, with no attempt read behind it', async () => {
    render(<WorkflowsScreen />)
    await waitFor(() => expect(rowName('wf_fresh')).toBe('Fresh build'))
    const cost = document.querySelector('.wfl-table tbody tr[data-workflow="wf_fresh"] [data-col="cost"]')
    expect(cost?.textContent).toMatch(/\$0\.50/)
    expect(cost?.textContent).not.toMatch(/reading/)
  })

  it('says the unserved rows once, as not served rather than as waiting to scroll in', async () => {
    render(<WorkflowsScreen />)
    await waitFor(() => expect(rowName('wf_fresh')).toBe('Fresh build'))
    const note = document.querySelector('.wfl .c-count-note')?.textContent ?? ''
    expect(note).toMatch(/1 of 2 rows have their steps read/)
    expect(note).not.toMatch(/scroll into view/)
  })
})

describe('one workflow’s page: no task read', () => {
  it('reads the workflows around it at AROUND_LIMIT, never the task list, and only its own steps’ attempts', async () => {
    render(<WorkflowsScreen view="wf=wf_fresh" />)
    await waitFor(() => expect(document.querySelector('.wf-canvas')).toBeTruthy())
    await waitFor(() => expect(api.loadWorkflowBoard).toHaveBeenCalled())
    expect(AROUND_LIMIT).toBeLessThan(100)
    for (const call of api.loadWorkflowBoard.mock.calls) expect(call[0]).toBe(AROUND_LIMIT)
    expect(api.loadTasks).not.toHaveBeenCalled()
    expect(fetched.filter((u) => /\/v1\/tasks\?(.*&)?limit=/.test(u))).toEqual([])
    await waitFor(() => expect(api.loadWorkflowUsage).toHaveBeenCalled())
    for (const call of api.loadWorkflowUsage.mock.calls) expect(call[0]).toEqual(['t_fresh'])
  })

  it('reads as loading while its attempt read is out, never as not started', async () => {
    api.loadWorkflowUsage.mockReturnValue(new Promise(() => {}))
    render(<WorkflowsScreen view="wf=wf_fresh&tab=table" />)
    await waitFor(() => expect(document.querySelector('.wf-canvas, table')).toBeTruthy())
    const page = document.body.textContent ?? ''
    expect(page).not.toMatch(/not started/i)
  })
})
