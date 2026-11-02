/**
 * LIVE QA OF swarm.example.com, SECTION "WORK › WORKFLOWS" (2026-10-11): the S1
 * and S2 findings, each held by the property the finding broke rather than
 * by markup.
 *
 *   WF-1 (S1)  a search finds a row whose name has not been read yet, and
 *              says "no match" only over rows it could judge;
 *   WF-2       the list pages past its newest 100 and says what it counted;
 *   WF-3       a step's task id is searched, and a pasted workflow id the
 *              list does not hold offers that workflow's page;
 *   WF-4       "keeps running" names only steps that have not ended (and
 *              WF-14: one run of text, so no gap lands before a comma);
 *   WF-6       the attempt stepper and the table count one thing, or say
 *              which each counts;
 *   WF-7/17    another workflow or another tab is a new history entry;
 *   WF-8       the phone draws a card per row, not the desktop table.
 *
 * WF-5 (the merge card's CI clock) is held in merge.card.test.tsx.
 */
import STYLES from '../styles.css?raw'
import WF_CSS from '../styles/workflows.css?raw'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { WorkflowBoard, WorkflowRead, WorkflowUsage } from '../api'
import type { AttemptRow, Task, TaskState, Workflow, WorkflowPage, WorkflowStep } from '../types'
import { cascade } from './cssgate'
import { painted } from './marks'
import { ownFromBoard } from './workflowsById'

const api = vi.hoisted(() => ({
  loadWorkflowBoard: vi.fn(),
  loadWorkflowUsage: vi.fn(),
  loadWorkflow: vi.fn(),
  loadAttempts: vi.fn(),
  loadOlderWorkflows: vi.fn(),
  loadTaskEventsPage: vi.fn(),
  cancelTask: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => {
  const real = await importOriginal<typeof import('../api')>()
  return { ...real, ...api }
})

const { WorkflowsScreen, listSummary } = await import('../Workflows')
const { StopRun, survivorsOf } = await import('../StopRun')

const T0 = Date.parse('2026-10-11T11:20:00.000Z')
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
    started_at: iso(-500),
    completed_at: state === 'SUCCEEDED' || state === 'FAILED' ? iso(-400) : null,
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

function workflow(id: string, state: TaskState, steps: WorkflowStep[], ageSeconds: number): Workflow {
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

function attempt(n: number, taskId: string, over: Partial<AttemptRow> = {}): AttemptRow {
  return {
    attempt_id: `att_${n}`,
    task_id: taskId,
    tenant_id: 'eng',
    generation: n,
    lease_id: `lease_${n}`,
    backend: 'cloud-run',
    execution_name: null,
    created_at: iso(-600 + n * 60),
    started_at: iso(-598 + n * 60),
    completed_at: iso(-590 + n * 60),
    exit_code: 75,
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
    ...over,
  }
}

/**
 * The live shape of WF-1: the newest row's step is in the board's task
 * window, and an older one's is not, so its name is unknown until its own
 * read lands.
 */
const fresh = workflow('wf_fresh', 'SUCCEEDED', [step('implement', { task_id: 'task_fresh' })], 600)
const old = workflow('wf_old', 'SUCCEEDED', [step('implement', { task_id: 'task_old' })], 7200)
const oldTask = task('task_old', 'SUCCEEDED', { metadata: { unit: 'VQA-L10' } })

function serve(workflows: Workflow[], taskById: Map<string, Task> | null, nextPageToken: string | null = null): void {
  api.loadWorkflowBoard.mockResolvedValue({
    status: 'ok',
    data: { workflows, taskById, statesDetail: null, nextPageToken },
    fetchedAt: T0,
  } satisfies Result<WorkflowBoard>)
}

/** An IntersectionObserver that never reports a row in view: nothing scrolls here. */
class NeverInView {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
  takeRecords(): IntersectionObserverEntry[] {
    return []
  }
}

beforeEach(() => {
  vi.spyOn(Date, 'now').mockReturnValue(T0)
  vi.stubGlobal('IntersectionObserver', NeverInView)
  serve([fresh, old], new Map([['task_fresh', task('task_fresh', 'SUCCEEDED', { metadata: { unit: 'fresh-title' } })]]))
  api.loadWorkflowUsage.mockResolvedValue({ status: 'empty', fetchedAt: T0 } satisfies Result<WorkflowUsage>)
  api.loadAttempts.mockResolvedValue({ status: 'empty', fetchedAt: T0 })
  api.loadTaskEventsPage.mockResolvedValue({ status: 'empty', fetchedAt: T0 })
  // The page reads its own workflow first (live QA CH-21), as the API serves it.
  api.loadWorkflow.mockImplementation(ownFromBoard(api.loadWorkflowBoard))
  api.loadOlderWorkflows.mockReset()
  try {
    window.localStorage.clear()
  } catch {
    /* storage refused */
  }
})

afterEach(() => {
  // Unmount BEFORE the mocks are restored. In vitest 3.2 `restoreAllMocks`
  // also resets every `vi.fn()` above to return undefined, and this hook runs
  // before setup.ts's `cleanup()` (hooks stack), so a render still landing
  // in between -- a tab click's re-render, a read resolving late on a loaded
  // runner -- called `loadWorkflowUsage(...).then` on undefined (CI, PR #1375).
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

const rowIds = () => [...document.querySelectorAll<HTMLElement>('.wfl-table tbody tr')].map((r) => r.dataset.workflow ?? '')

// ---------------------------------------------------------------------------
// WF-1 (S1)
// ---------------------------------------------------------------------------

describe('WF-1: a search finds a row by the name the list read served, without scrolling to it', () => {
  // THE NAMES COME WITH THE LIST (console load speed, owner's pick
  // 2026-11-01): each step's block carries its task's `unit` and `title`, so
  // a search reads every row's name from the list read and reads no row by id.
  it('finds a row by its step block’s name, with no read of its own', async () => {
    serve([fresh, old], new Map([['task_fresh', task('task_fresh', 'SUCCEEDED', { metadata: { unit: 'fresh-title' } })], ['task_old', oldTask]]))
    render(<WorkflowsScreen view="q=VQA-L10" />)
    await waitFor(() => expect(rowIds()).toEqual(['wf_old']))
    expect(document.querySelector('.wfl-none')).toBeNull()
    // MUTATION: put `useRowReads` back -- the search reads rows by id.
    expect(api.loadWorkflow).not.toHaveBeenCalled()
  })

  it('a row the list read served no name for is not read for the search either', async () => {
    render(<WorkflowsScreen view="q=VQA-L10" />)
    await waitFor(() => expect(document.querySelector('.wfl-none')?.textContent).toMatch(/^No match among all 2 workflows read\./))
    expect(api.loadWorkflow).not.toHaveBeenCalled()
  })

  it('says "no match" over every row the list read', async () => {
    render(<WorkflowsScreen view="q=nothing-is-called-this" />)
    await waitFor(() => expect(document.querySelector('.wfl-none')?.textContent).toMatch(/^No match among all 2 workflows read\./))
  })
})

// ---------------------------------------------------------------------------
// WF-2
// ---------------------------------------------------------------------------

describe('WF-2: the list pages past its newest 100 and says what its counts are of', () => {
  const older = [
    workflow('wf_older_a', 'FAILED', [step('implement')], 90_000),
    workflow('wf_older_b', 'FAILED', [step('implement')], 95_000),
  ]
  // Built at runtime: an opaque cursor, not a credential.
  const cursor = ['page', 'two'].join('-')

  it('follows the page token on asking, and the older rows join the counts and the order', async () => {
    serve([fresh, old], new Map(), cursor)
    api.loadOlderWorkflows.mockResolvedValue({
      status: 'ok',
      data: { workflows: older, next_page_token: null },
      fetchedAt: T0,
    } satisfies Result<WorkflowPage>)
    render(<WorkflowsScreen view="sort=oldest" />)
    await waitFor(() => expect(rowIds()).toEqual(['wf_old', 'wf_fresh']))
    // Before: the counts say they are of the newest read, not of everything.
    expect(document.querySelector('.wfl')!.textContent).toMatch(/Counts and order are of the 2 newest workflows/)
    fireEvent.click(screen.getByRole('button', { name: 'Load 100 older' }))
    await waitFor(() => expect(rowIds()).toEqual(['wf_older_b', 'wf_older_a', 'wf_old', 'wf_fresh']))
    expect(api.loadOlderWorkflows).toHaveBeenCalledWith(cursor)
    // "oldest first" is now the oldest workflow there is, and the server said
    // nothing is older, so the list stops offering more and stops qualifying.
    expect(screen.queryByRole('button', { name: 'Load 100 older' })).toBeNull()
    expect(document.querySelector('.wfl-foot')!.textContent).toMatch(/4 of 4 read .* no older workflows/)
    expect(document.querySelector('.wfl')!.textContent).not.toMatch(/Counts and order are of/)
    const failed = within(document.querySelector<HTMLElement>('.wfl-seg')!).getByText('Failed').closest('button, a')!
    expect(failed.textContent).toMatch(/2/)
  })

  it('the header census says it is of the newest read when older workflows exist', () => {
    const parked = workflow('wf_p', 'PARKED', [step('merge')], 60)
    const running = workflow('wf_r', 'RUNNING', [step('implement')], 30)
    expect(listSummary({ workflows: [running, parked, fresh], nextPageToken: cursor })).toBe(
      '1 running · 1 parked · 1 finished · 0 failed · of the 3 newest',
    )
    // WF-10: a parked workflow is not counted as running.
    expect(listSummary({ workflows: [running, parked, fresh], nextPageToken: null })).toBe(
      '1 running · 1 parked · 1 finished · 0 failed',
    )
  })
})

// ---------------------------------------------------------------------------
// WF-3
// ---------------------------------------------------------------------------

describe('WF-3: a task id is searched, and a pasted workflow id outside the list opens its page', () => {
  it('finds the row whose step ran as the task id typed', async () => {
    render(<WorkflowsScreen view="q=task_fresh" />)
    await waitFor(() => expect(rowIds()).toEqual(['wf_fresh']))
  })

  it('offers the page of a whole workflow id the list does not hold', async () => {
    const id = `wf_${'0123456789abcdef0123'}`
    render(<WorkflowsScreen view={`q=${id}`} />)
    const link = await waitFor(() => {
      const a = document.querySelector<HTMLAnchorElement>('[data-fact="pasted-id"] a')
      expect(a).not.toBeNull()
      return a!
    })
    expect(link.getAttribute('href')).toBe(`/workflows/${id}`)
  })
})

// ---------------------------------------------------------------------------
// WF-4 and WF-14
// ---------------------------------------------------------------------------

describe('WF-4: the stop dialog says only steps still going keep running', () => {
  // implement → review → merge, plus a parallel `docs`; stopping `merge`.
  const steps = [
    step('implement', { task_id: 't_impl' }),
    step('review', { task_id: 't_rev', depends_on: ['implement'] }),
    step('docs', { task_id: 't_docs' }),
    step('lint', { task_id: 't_lint' }),
    step('merge', { task_id: 't_merge', depends_on: ['review'] }),
  ]
  const wf = workflow('wf_s9', 'PARKED', steps, 600)
  const tasks = new Map<string, Task>([
    ['t_impl', task('t_impl', 'SUCCEEDED')],
    ['t_rev', task('t_rev', 'SUCCEEDED')],
    ['t_docs', task('t_docs', 'RUNNING')],
    ['t_lint', task('t_lint', 'RUNNING')],
    ['t_merge', task('t_merge', 'PARKED', { park_reason: 'CI_PENDING' })],
  ])

  it('leaves out every step whose task has ended', () => {
    expect(survivorsOf(steps[4]!, steps, tasks).map((s) => s.step_id)).toEqual(['docs', 'lint'])
    // Every step ended: nobody keeps running.
    const done = new Map([...tasks].map(([k, t]) => [k, { ...t, state: 'SUCCEEDED' as TaskState }]))
    expect(survivorsOf(steps[4]!, steps, done)).toEqual([])
  })

  it('draws the survivors as one run of text, without a gap before a comma', () => {
    render(<StopRun task={tasks.get('t_merge')!} what="step merge" workflow={wf} step={steps[4]!} taskById={tasks} reload={() => {}} />)
    fireEvent.click(screen.getByRole('button', { name: 'stop' }))
    const li = [...document.querySelectorAll('.stop-blast .ctl-fact')].find((el) => el.querySelector('b')?.textContent === 'keeps running')
    expect(li, 'no keeps-running fact').toBeTruthy()
    // The key and ONE value element: a flex row lays out two items, not one per step.
    expect([...li!.children].map((c) => c.tagName)).toEqual(['B', 'SPAN'])
    expect(li!.children[1]!.textContent).toBe('docs, lint')
  })
})

// ---------------------------------------------------------------------------
// WF-6
// ---------------------------------------------------------------------------

describe('WF-6: the attempt stepper and the table count the same thing, or say which each counts', () => {
  it('names the stepper position a run when CI parks were refunded from the count', async () => {
    const merge = step('merge', { task_id: 't_merge', runner_profile: 'merge' })
    const wf = workflow('wf_plugin', 'SUCCEEDED', [merge], 3600)
    const t = task('t_merge', 'SUCCEEDED', { attempt_count: 1, max_attempts: 10, current_generation: 5, runner_profile: 'merge' })
    serve([wf], new Map([['t_merge', t]]))
    api.loadWorkflow.mockResolvedValue({ status: 'ok', data: { workflow: wf, tasks: [t] }, fetchedAt: T0 } satisfies Result<WorkflowRead>)
    // Five attempt documents: four CI parks and the merge. Newest first.
    api.loadAttempts.mockResolvedValue({
      status: 'ok',
      data: { attempts: [5, 4, 3, 2, 1].map((n) => attempt(n, 't_merge', n === 5 ? { exit_code: 0 } : { error: 'CI_PENDING' })) },
      fetchedAt: T0,
    })
    render(<WorkflowsScreen view="wf=wf_plugin&tab=table" />)
    const pickButton = await waitFor(() => {
      const b = document.querySelector<HTMLButtonElement>('.wf-pick[data-step="merge"]')
      expect(b).not.toBeNull()
      return b!
    })
    if (document.querySelector('.wf-inspect') === null) fireEvent.click(pickButton)
    const pos = await waitFor(() => {
      const p = document.querySelector<HTMLElement>('[data-scrub="attempt"] .wf-scrub-pos')
      expect(p).not.toBeNull()
      return p!
    })
    // Not "attempt 5 of 5" beside a table saying "1 of 10".
    expect(pos.textContent).not.toMatch(/^attempt 5 of 5$/)
    expect(pos.textContent).toBe('run 5 of 5')
    // V200 (#1427): ◀ position ▶ is one unit that never wraps -- at 400 the
    // whole sentence filled the line and ▶ wrapped alone -- and the counted
    // figure is after it, outside it, where it may wrap on its own.
    // MUTATION: put the counted figure back inside the position, or the
    // buttons back outside `.wf-scrub-nav`.
    const scrub = document.querySelector<HTMLElement>('[data-scrub="attempt"]')!
    const nav = pos.parentElement!
    expect(nav.classList.contains('wf-scrub-nav')).toBe(true)
    expect(within(nav).getByRole('button', { name: 'Previous attempt' })).toBeTruthy()
    expect(within(nav).getByRole('button', { name: 'Next attempt' })).toBeTruthy()
    const counted = scrub.querySelector<HTMLElement>(':scope > .wf-scrub-counted')!
    expect(counted.textContent).toBe('1 of 10 counted')
    expect(nav.nextElementSibling).toBe(counted)
    expect(counted.title).toMatch(/a wait for CI is refunded and not counted/)
    expect(painted(nav, 'white-space', { width: 400 })).toBe('nowrap')
    // WF-20: a park is not filed under `error`. Step back to a CI park.
    fireEvent.click(within(document.querySelector<HTMLElement>('[data-scrub="attempt"]')!).getByRole('button', { name: 'Previous attempt' }))
    const keys = [...document.querySelectorAll('[data-scrub="attempt"] .ctl-fact > b')].map((b) => b.textContent)
    expect(keys).toContain('parked')
    expect(keys).not.toContain('error')
  })
})

// ---------------------------------------------------------------------------
// WF-7 and WF-17
// ---------------------------------------------------------------------------

describe('WF-7/WF-17: another view of a workflow is a new history entry', () => {
  it('pushes the new address when a tab is chosen, so Back returns to the one before', async () => {
    const push = vi.spyOn(window.history, 'pushState')
    const onView = vi.fn()
    render(<WorkflowsScreen view="wf=wf_fresh" onView={onView} />)
    // The loaded page's tabs, not the skeleton's: those are replaced when the read lands.
    await waitFor(() => expect(document.querySelector('.wfp:not(.is-loading)')).not.toBeNull())
    const table = await waitFor(() => {
      const a = [...document.querySelectorAll<HTMLAnchorElement>('.c-tabs a')].find((x) => x.getAttribute('href') === '/workflows/wf_fresh/table')
      expect(a).toBeTruthy()
      return a!
    })
    fireEvent.click(table)
    expect(push).toHaveBeenCalledWith(null, '', '/workflows/wf_fresh/table')
    expect(onView).toHaveBeenLastCalledWith('wf=wf_fresh&tab=table')
  })
})

// ---------------------------------------------------------------------------
// WF-8
// ---------------------------------------------------------------------------

describe('WF-8: the phone draws a card per workflow, not the desktop table', () => {
  const SHEET = `${WF_CSS}\n${STYLES}`
  const at = (el: Element, prop: string, width: number) => cascade(SHEET, el, prop, { width, theme: 'dark' }).winner?.value ?? null

  it('lays each row out as a card at 400 and as a table row at 1440', async () => {
    render(<WorkflowsScreen />)
    await waitFor(() => expect(rowIds()).toHaveLength(2))
    const tr = document.querySelector('tbody tr.wfl-row')!
    expect(at(tr, 'display', 400)).toBe('grid')
    expect(at(tr, 'display', 1440)).not.toBe('grid')
    // The held State column is let go, so it no longer takes 40% of the width.
    const state = tr.querySelector('td[data-col="state"]')!
    expect(at(state, 'position', 400)).toBe('static')
    // The figures the scroller hid are on the card.
    for (const col of ['done', 'cost', 'owner', 'submitted', 'duration']) {
      expect(at(tr.querySelector(`td[data-col="${col}"]`)!, 'display', 400), col).toBe('block')
    }
    expect(at(document.querySelector('.wfl-table thead')!, 'display', 400)).toBe('none')
  })
})
