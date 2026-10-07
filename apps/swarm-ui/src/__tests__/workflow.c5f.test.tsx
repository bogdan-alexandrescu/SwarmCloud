/**
 * WORKFLOWS LIST, FUNCTIONALITY WAVE 16 LANE C5F (QA pass on swarm.saga.xyz,
 * 2026-10-07: G3-06, G3-09, G3-14, G3-20, G3-21).
 *
 * Each `describe` names the finding it holds; each failed against main at
 * 33ea743 before the change it holds.
 */
import STYLES from '../styles.css?raw'
import WF_CSS from '../styles/workflows.css?raw'
import { act, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { WorkflowBoard, WorkflowRead, WorkflowUsage } from '../api'
import type { Task, TaskState, Workflow, WorkflowStep } from '../types'
import { cascade } from './cssgate'

const api = vi.hoisted(() => ({
  loadWorkflowBoard: vi.fn(),
  loadWorkflowUsage: vi.fn(),
  loadWorkflow: vi.fn(),
  loadAttempts: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => {
  const real = await importOriginal<typeof import('../api')>()
  return { ...real, ...api }
})

const { WorkflowsScreen } = await import('../Workflows')
const wl = await import('../workflowlist')
const { clockTime } = await import('../types')

const T0 = Date.parse('2026-10-07T12:00:00.000Z')
const iso = (offsetSeconds: number) => new Date(T0 + offsetSeconds * 1000).toISOString()

function step(step_id: string, over: Partial<WorkflowStep> = {}): WorkflowStep {
  return { step_id, runner_profile: 'claude-code', resource_class: 'standard', depends_on: [], input_from: {}, task_id: null, ...over }
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
    started_at: null,
    completed_at: null,
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

function workflow(id: string, state: TaskState, owner: string, steps: WorkflowStep[], ageSeconds: number): Workflow {
  return {
    workflow_id: id,
    state,
    tenant_id: 'eng',
    stored_state: state,
    state_source: 'derived',
    rollup: { state, complete: true, reason: 'test', counts: {}, unreadable_steps: [], unstarted_steps: [], steps_read: steps.length },
    created_at: iso(-ageSeconds),
    updated_at: iso(-60),
    submitted_by: owner,
    priority: 0,
    on_step_failure: 'fail_workflow',
    cancel_requested: false,
    steps,
  }
}

/** One workflow whose step task is in the board's task window, one whose is not. */
function board(): { workflows: Workflow[]; taskById: Map<string, Task>; oldTask: Task } {
  const fresh = workflow('wf_fresh', 'SUCCEEDED', 'priya@example.com', [step('a', { task_id: 't_fresh' })], 600)
  const old = workflow('wf_old', 'CANCELLED', 'swarm-ci-fix@saga-swarm.iam.gserviceaccount.com', [step('a', { task_id: 't_old' })], 7200)
  const taskById = new Map<string, Task>([
    ['t_fresh', task('t_fresh', 'SUCCEEDED', { metadata: { unit: 'fresh-title' }, started_at: iso(-500), completed_at: iso(-400) })],
  ])
  const oldTask = task('t_old', 'CANCELLED', { metadata: { unit: 'Commits SwarmCloud old title' }, started_at: iso(-7000), completed_at: iso(-6000) })
  return { workflows: [fresh, old], taskById, oldTask }
}

function serve(workflows: Workflow[], taskById: Map<string, Task> | null): void {
  api.loadWorkflowBoard.mockResolvedValue({
    status: 'ok',
    data: { workflows, taskById, statesDetail: null },
    fetchedAt: T0,
  } satisfies Result<WorkflowBoard>)
}

const notFound = {
  status: 'error',
  error: { kind: 'not_found', httpStatus: 404, code: null, message: 'no such workflow' },
} as const

beforeEach(() => {
  vi.spyOn(Date, 'now').mockReturnValue(T0)
  const b = board()
  serve(b.workflows, b.taskById)
  api.loadWorkflowUsage.mockResolvedValue({ status: 'empty', fetchedAt: T0 } satisfies Result<WorkflowUsage>)
  api.loadAttempts.mockResolvedValue({ status: 'empty', fetchedAt: T0 })
  api.loadWorkflow.mockResolvedValue(notFound)
  try {
    window.localStorage.clear()
  } catch {
    /* storage refused */
  }
})

afterEach(() => {
  vi.unstubAllGlobals()
})

function rowIds(): string[] {
  return [...document.querySelectorAll<HTMLElement>('.wfl-table tbody tr')].map((r) => r.dataset.workflow ?? '')
}

const row = (id: string) => document.querySelector<HTMLElement>(`tr[data-workflow="${id}"]`)!

const SHEET = `${WF_CSS}\n${STYLES}`
const at = (el: Element, prop: string, width = 1440) => cascade(SHEET, el, prop, { width, theme: 'dark' }).winner?.value ?? null

// ---------------------------------------------------------------------------
// G3-06
// ---------------------------------------------------------------------------

/** An IntersectionObserver double: nothing is in view until `scrollIn` says so. */
class FakeObserver {
  static all: FakeObserver[] = []
  readonly targets = new Set<Element>()
  constructor(readonly callback: IntersectionObserverCallback) {
    FakeObserver.all.push(this)
  }
  observe(el: Element): void {
    this.targets.add(el)
  }
  unobserve(el: Element): void {
    this.targets.delete(el)
  }
  disconnect(): void {
    this.targets.clear()
  }
  takeRecords(): IntersectionObserverEntry[] {
    return []
  }
}

function scrollIn(el: Element): void {
  act(() => {
    for (const o of FakeObserver.all) {
      if (!o.targets.has(el)) continue
      const entry = { target: el, isIntersecting: true, intersectionRatio: 1 } as unknown as IntersectionObserverEntry
      o.callback([entry], o as unknown as IntersectionObserver)
    }
  })
}

describe('G3-06: a row whose steps the task window missed reads them as it scrolls in, and the list says how many are read', () => {
  it('stepsRead is true only when every step that has a task is in the read', () => {
    const b = board()
    const [fresh, old] = b.workflows
    expect(wl.stepsRead(fresh!, b.taskById)).toBe(true)
    expect(wl.stepsRead(old!, b.taskById)).toBe(false)
    expect(wl.stepsRead(fresh!, null)).toBe(false)
    // A step with no task never ran and has nothing to read.
    expect(wl.stepsRead(workflow('wf_q', 'QUEUED', 'sam', [step('a')], 10), new Map())).toBe(true)
  })

  it('says once above the table how many rows have their steps read, and reads the rest in view', async () => {
    FakeObserver.all = []
    vi.stubGlobal('IntersectionObserver', FakeObserver)
    const b = board()
    api.loadWorkflow.mockResolvedValue({
      status: 'ok',
      data: { workflow: b.workflows[1]!, tasks: [b.oldTask] },
      fetchedAt: T0,
    } satisfies Result<WorkflowRead>)
    render(<WorkflowsScreen />)
    await waitFor(() => expect(rowIds()).toEqual(['wf_fresh', 'wf_old']))
    // Before the old row is in view: its id alone, and the note says so.
    expect(row('wf_old').querySelector('.wfl-name > a')!.textContent).toBe('wf_old')
    const note = document.querySelector('.wfl .c-count-note')
    expect(note?.textContent).toBe('1 of 2 rows have their steps read; older rows show ids only until they scroll into view')
    // The note sits above the table, once.
    expect(document.querySelectorAll('.wfl .c-count-note')).toHaveLength(1)
    expect(note!.compareDocumentPosition(document.querySelector('.wfl-table')!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    // The fresh row was read by the window; it never asks.
    expect(api.loadWorkflow).not.toHaveBeenCalledWith('wf_fresh')

    scrollIn(row('wf_old'))
    await waitFor(() => expect(row('wf_old').querySelector('.wfl-name > a')!.textContent).toBe('Commits SwarmCloud old title'))
    expect(api.loadWorkflow).toHaveBeenCalledWith('wf_old')
    expect(api.loadWorkflow).toHaveBeenCalledTimes(1)
    // The duration the detail page knows: submission to the last end.
    expect(row('wf_old').querySelector('.wfl-dur')!.textContent).toBe('20m 0s')
    // Every row read: no note.
    expect(document.querySelector('.wfl .c-count-note')).toBeNull()
  })

  it('a row whose own read failed stays an id and the note counts it as not read', async () => {
    FakeObserver.all = []
    vi.stubGlobal('IntersectionObserver', FakeObserver)
    render(<WorkflowsScreen />)
    await waitFor(() => expect(rowIds()).toEqual(['wf_fresh', 'wf_old']))
    scrollIn(row('wf_old'))
    await waitFor(() => expect(api.loadWorkflow).toHaveBeenCalledWith('wf_old'))
    await waitFor(() =>
      expect(document.querySelector('.wfl .c-count-note')?.textContent).toBe(
        '1 of 2 rows have their steps read; older rows show ids only until they scroll into view; 1 could not be read',
      ),
    )
    expect(row('wf_old').querySelector('.wfl-name > a')!.textContent).toBe('wf_old')
  })
})

// ---------------------------------------------------------------------------
// G3-09
// ---------------------------------------------------------------------------

describe('G3-09: the Workflow column holds at least 280px at 1440 and the id never wraps', () => {
  it('drops Shape and Runners below 1680 and carries the runner mix on the name’s second line', async () => {
    render(<WorkflowsScreen />)
    await waitFor(() => expect(rowIds()).toEqual(['wf_fresh', 'wf_old']))
    const heads = [...document.querySelectorAll('.wfl-table thead th')]
    const shown = heads.filter((h) => at(h, 'display') !== 'none')
    expect(shown.map((h) => h.getAttribute('data-col'))).toEqual(['state', 'workflow', 'done', 'cost', 'owner', 'submitted', 'duration'])
    const used = shown
      .filter((h) => h.getAttribute('data-col') !== 'workflow')
      .reduce((t, h) => t + Number.parseFloat(at(h, 'width') ?? 'NaN'), 0)
    // The table is 1054-1056px at 1440 (QA measured 1054).
    expect(1054 - used, 'the name column at 1440').toBeGreaterThanOrEqual(280)
    // The row's cells follow their heads.
    const r = row('wf_fresh')
    expect(at(r.querySelector('td[data-col="shape"]')!, 'display')).toBe('none')
    expect(at(r.querySelector('td[data-col="runners"]')!, 'display')).toBe('none')
    const inline = r.querySelector('.wfl-name .wfl-mix')!
    expect(inline, 'no runner mix under the name').toBeTruthy()
    expect(at(inline, 'display')).not.toBe('none')
    // At 1680 the columns come back and the inline mix goes.
    expect(at(heads.find((h) => h.getAttribute('data-col') === 'shape')!, 'display', 1680)).not.toBe('none')
    expect(at(inline, 'display', 1680)).toBe('none')
  })

  it('cuts the id on one line with an ellipsis', async () => {
    render(<WorkflowsScreen />)
    await waitFor(() => expect(rowIds()).toEqual(['wf_fresh', 'wf_old']))
    const id = row('wf_fresh').querySelector('.wfl-name > .id')!
    expect(at(id, 'white-space')).toBe('nowrap')
    expect(at(id, 'overflow')).toBe('hidden')
    expect(at(id, 'text-overflow')).toBe('ellipsis')
  })
})

// ---------------------------------------------------------------------------
// G3-14
// ---------------------------------------------------------------------------

describe('G3-14: at 390 the owner select fits its column', () => {
  it('lets each pick and its select shrink to the row', async () => {
    render(<WorkflowsScreen />)
    await waitFor(() => expect(rowIds()).toHaveLength(2))
    const pick = document.querySelector('.wfl-pick')!
    const select = pick.querySelector('select')!
    expect(at(pick, 'min-width', 390)).toBe('0')
    expect(at(pick, 'max-width', 390)).toBe('100%')
    expect(at(pick, 'flex', 390)).toBe('1 1 100%')
    expect(at(select, 'min-width', 390)).toBe('0')
    expect(at(select, 'max-width', 390)).toBe('100%')
  })

  it('names each owner by the part before the @, with the whole address in its title', async () => {
    render(<WorkflowsScreen />)
    await waitFor(() => expect(rowIds()).toHaveLength(2))
    const owner = screen.getByRole('combobox', { name: /owner/ })
    const opts = [...owner.querySelectorAll('option')].filter((o) => o.value !== '')
    expect(opts.map((o) => o.textContent)).toEqual(['priya', 'swarm-ci-fix'])
    expect(opts.map((o) => o.getAttribute('title'))).toEqual([
      'priya@example.com',
      'swarm-ci-fix@saga-swarm.iam.gserviceaccount.com',
    ])
    expect(opts.map((o) => o.value)).toEqual(['priya@example.com', 'swarm-ci-fix@saga-swarm.iam.gserviceaccount.com'])
  })
})

// ---------------------------------------------------------------------------
// G3-20
// ---------------------------------------------------------------------------

describe('G3-20: the time column is the submission the newest-first order sorts by', () => {
  it('names the column Submitted and prints each row’s submit time, newest first', async () => {
    // b was submitted later, but its first step started earlier than a's.
    const a = workflow('wf_a', 'SUCCEEDED', 'sam', [step('s', { task_id: 't_a' })], 300)
    const b = workflow('wf_b', 'SUCCEEDED', 'sam', [step('s', { task_id: 't_b' })], 200)
    serve(
      [a, b],
      new Map([
        ['t_a', task('t_a', 'SUCCEEDED', { started_at: iso(-100), completed_at: iso(-50) })],
        ['t_b', task('t_b', 'SUCCEEDED', { started_at: iso(-190), completed_at: iso(-50) })],
      ]),
    )
    render(<WorkflowsScreen view="sort=newest" />)
    await waitFor(() => expect(rowIds()).toEqual(['wf_b', 'wf_a']))
    const heads = [...document.querySelectorAll('.wfl-table thead th')].map((th) => th.textContent)
    expect(heads).toContain('Submitted')
    expect(heads).not.toContain('Started')
    const cells = rowIds().map((id) => row(id).querySelector<HTMLElement>('td[data-col="submitted"]')!)
    expect(cells.map((c) => c.textContent)).toEqual([b.created_at, a.created_at].map((t) => clockTime(t, T0)!.text))
    // The first step's start stays one hover away.
    expect(cells[0]!.getAttribute('title')).toMatch(/^submitted .*; first step started /)
  })
})

// ---------------------------------------------------------------------------
// G3-21
// ---------------------------------------------------------------------------

describe('G3-21: an empty search says it searched only the newest workflows read', () => {
  it('names how many it searched', async () => {
    render(<WorkflowsScreen view="q=zzzqqq" />)
    await waitFor(() => expect(document.querySelector('.wfl-none')).toBeTruthy())
    expect(document.querySelector('.wfl-none')!.textContent).toMatch(
      /^No match among the 2 newest workflows read; older ones are not searched\.Clear the filters$/,
    )
  })
})
