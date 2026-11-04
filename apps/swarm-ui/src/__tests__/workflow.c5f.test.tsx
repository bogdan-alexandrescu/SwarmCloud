/**
 * WORKFLOWS LIST, FUNCTIONALITY WAVE 16 LANE C5F (QA pass on swarm.example.com,
 * 2026-10-07: G3-06, G3-09, G3-14, G3-20, G3-21).
 *
 * Each `describe` names the finding it holds; each failed against main at
 * 33ea743 before the change it holds.
 */
import STYLES from '../styles.css?raw'
import WF_CSS from '../styles/workflows.css?raw'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { WorkflowBoard, WorkflowUsage } from '../api'
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

const { WorkflowsScreen, utcStamp } = await import('../Workflows')
const wl = await import('../workflowlist')

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
  const old = workflow('wf_old', 'CANCELLED', 'swarm-ci-fix@example-project.iam.gserviceaccount.com', [step('a', { task_id: 't_old' })], 7200)
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
  return [...document.querySelectorAll<HTMLElement>('.wfg-table tbody tr')].map((r) => r.dataset.workflow ?? '')
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

describe('G3-06: a row whose steps the list read did not serve stays an id, and the list says how many are read', () => {
  it('stepsRead is true only when every step that has a task is in the read', () => {
    const b = board()
    const [fresh, old] = b.workflows
    expect(wl.stepsRead(fresh!, b.taskById)).toBe(true)
    expect(wl.stepsRead(old!, b.taskById)).toBe(false)
    expect(wl.stepsRead(fresh!, null)).toBe(false)
    // A step with no task never ran and has nothing to read.
    expect(wl.stepsRead(workflow('wf_q', 'QUEUED', 'sam', [step('a')], 10), new Map())).toBe(true)
  })

  it('says once above the table how many rows have their steps read, and reads no row by id as it scrolls in', async () => {
    // THE LIST READ SERVES EVERY STEP'S BLOCK (console load speed, owner's
    // pick 2026-11-01): a row it did not serve stays an id, and the list
    // reads nothing more for it -- its page reads it.
    FakeObserver.all = []
    vi.stubGlobal('IntersectionObserver', FakeObserver)
    render(<WorkflowsScreen />)
    await waitFor(() => expect(rowIds()).toEqual(['wf_fresh', 'wf_old']))
    expect(row('wf_old').querySelector('td[data-col="workflow"] > a')!.textContent).toBe('wf_old')
    const note = document.querySelector('.wfg .c-count-note')
    expect(note?.textContent).toBe("1 of 2 rows have their steps read; the list read did not serve the rest's, which each one's page reads")
    // The note sits above the table, once.
    expect(document.querySelectorAll('.wfg .c-count-note')).toHaveLength(1)
    expect(note!.compareDocumentPosition(document.querySelector('.wfg-table')!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    scrollIn(row('wf_old'))
    // MUTATION: put `useRowReads` back -- the row reads its workflow by id.
    await new Promise((r) => setTimeout(r, 20))
    expect(api.loadWorkflow).not.toHaveBeenCalled()
    expect(row('wf_old').querySelector('td[data-col="workflow"] > a')!.textContent).toBe('wf_old')
  })
})

// ---------------------------------------------------------------------------
// G3-09
// ---------------------------------------------------------------------------

describe('G3-09: the Workflow column holds at least 280px at 1440 and the id never wraps', () => {
  // THE ONE-LINE ROW (the redesign, frame graph-workflows-rest): Shape and
  // Runners are no longer columns at any width, they are in the inspector's
  // summary, and the Workflow column takes every pixel the others leave.
  it('draws no Shape or Runners column, gives the Workflow column the rest of the row, and the runners in the summary', async () => {
    render(<WorkflowsScreen />)
    await waitFor(() => expect(rowIds()).toEqual(['wf_fresh', 'wf_old']))
    const heads = [...document.querySelectorAll('.wfg-table thead th')]
    const shown = heads.filter((h) => at(h, 'display') !== 'none')
    expect(shown.map((h) => h.getAttribute('data-col'))).toEqual(['state', 'workflow', 'steps', 'needs', 'spent', 'owner', 'changed'])
    const r = row('wf_fresh')
    expect(r.querySelector('td[data-col="shape"], td[data-col="runners"], .wfl-mix')).toBeNull()
    const name = r.querySelector('td[data-col="workflow"]')!
    expect(at(name, 'width')).toBe('100%')
    expect(at(name, 'min-width')).toBe('10em')
    // The runners are one click away, in the summary the row opens.
    fireEvent.click(r.querySelector<HTMLElement>('[data-col="state"]')!)
    const sum = document.querySelector('aside[aria-label="Workflow inspector"] .wfg-sum')!
    expect([...sum.querySelectorAll('.wfg-sum-dl > dt')].map((d) => d.textContent)).toContain('Runners')
    expect(sum.querySelector('.wfg-sum-dl .wf-mix')).toBeTruthy()
  })

  it('cuts the id on one line with an ellipsis', async () => {
    render(<WorkflowsScreen />)
    await waitFor(() => expect(rowIds()).toEqual(['wf_fresh', 'wf_old']))
    // The id is the summary's own line since the redesign (frame graph-workflows-rest).
    fireEvent.click(row('wf_fresh').querySelector<HTMLElement>('[data-col="state"]')!)
    const id = document.querySelector('aside[aria-label="Workflow inspector"] .wfg-sum > .id')!
    expect(id.textContent).toBe('wf_fresh')
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
    // The redesign's toolbar (frame graph-workflows-rest) sets two picks a
    // row at 390, each select the width of its pick and never wider.
    const pick = document.querySelector('.wfg-pick')!
    const select = pick.querySelector('select')!
    expect(at(pick, 'min-width', 390)).toBe('0')
    expect(at(pick, 'max-width', 390)).toBe('100%')
    expect(at(pick, 'flex', 390)).toBe('1 1 40%')
    expect(at(select, 'width', 390)).toBe('100%')
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
      'swarm-ci-fix@example-project.iam.gserviceaccount.com',
    ])
    expect(opts.map((o) => o.value)).toEqual(['priya@example.com', 'swarm-ci-fix@example-project.iam.gserviceaccount.com'])
  })
})

// ---------------------------------------------------------------------------
// G3-20
// ---------------------------------------------------------------------------

// THE TIME COLUMN IS CHANGED since the redesign (frame graph-workflows-rest);
// the submission the newest-first order sorts by is in the summary.
describe('G3-20: the submission the newest-first order sorts by is in the summary', () => {
  it('orders by submission, newest first, and prints each one’s submit time in its summary', async () => {
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
    const heads = [...document.querySelectorAll('.wfg-table thead th')].map((th) => th.textContent)
    expect(heads).toContain('Changed')
    expect(heads).not.toContain('Started')
    const submitted = (id: string) => {
      fireEvent.click(row(id).querySelector<HTMLElement>('[data-col="state"]')!)
      const dl = document.querySelector(`aside[aria-label="Workflow inspector"] .wfg-sum[data-workflow="${id}"] .wfg-sum-dl`)!
      const dt = [...dl.querySelectorAll('dt')].find((d) => d.textContent === 'Submitted')!
      // Read now: the inspector redraws the same node for the next pick.
      const dd = dt.nextElementSibling as HTMLElement
      return { text: dd.textContent, title: dd.getAttribute('title') }
    }
    const cells = rowIds().map(submitted)
    expect(cells.map((c) => c.text)).toEqual([b.created_at, a.created_at].map((t) => utcStamp(t, T0)))
    // UTC with Z, as the rest of the console writes it (visual QA pass 1 C-10, #2596).
    expect(cells[0]!.text).toMatch(/Z$/)
    // The first step's start stays one hover away.
    expect(cells[0]!.title).toMatch(/^submitted .*; first step started /)
  })
})

// ---------------------------------------------------------------------------
// G3-21
// ---------------------------------------------------------------------------

describe('G3-21: an empty search says it searched only the newest workflows read', () => {
  it('names how many it searched', async () => {
    render(<WorkflowsScreen view="q=zzzqqq" />)
    // The search reads the unread row's name before it says "no match" (WF-1);
    // its read fails here, so the answer is over both rows, and the read held
    // every workflow there is (no page token), so it says all of them.
    await waitFor(() =>
      expect(document.querySelector('.wfl-none')?.textContent).toMatch(
        /^No match among all 2 workflows read\.Clear the filters$/,
      ),
    )
  })
})
