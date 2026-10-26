/**
 * VISUAL QA PASS 1 ON WORK › WORKFLOWS (#2391, lane VQ-WORKFLOWS, 2026-10-26):
 * VQ-013 to VQ-016, each against workflows.html V2.
 *
 *  VQ-013  the list's two notes are one muted line, and a row's name, id and
 *          runners are one line, the current step its only second line.
 *  VQ-014  a graph that fits across is as tall as it is: the page is the one
 *          scroll container.
 *  VQ-015  one back path, the breadcrumb, naming the workflow and carrying the
 *          list's filters; the step card's action is `Stop step`.
 *  VQ-016  the Table's and the Timeline's arrival pick opens no card under its
 *          row, so the view arrives whole.
 */
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useState } from 'react'

import type { Result } from '../fetch'
import type { WorkflowBoard, WorkflowUsage } from '../api'
import type { Task, TaskState, Workflow, WorkflowStep } from '../types'
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

const { WorkflowsScreen } = await import('../Workflows')
const { crumbsOf, SECTIONS } = await import('../App')

const T0 = Date.parse('2026-10-26T01:00:00.000Z')
const iso = (offsetSeconds: number) => new Date(T0 + offsetSeconds * 1000).toISOString()

function step(step_id: string, depends_on: string[] = [], over: Partial<WorkflowStep> = {}): WorkflowStep {
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

/** A running three-step chain: `plan` done, `build` running, `ship` parked behind it. */
const chain = workflow(
  'wf_chain',
  'RUNNING',
  [
    step('plan', [], { task_id: 'task_plan' }),
    step('build', ['plan'], { task_id: 'task_build' }),
    step('ship', ['build'], { task_id: 'task_ship' }),
  ],
  600,
)
const chainTasks = new Map<string, Task>([
  ['task_plan', task('task_plan', 'SUCCEEDED', { metadata: { unit: 'JOIN-UI-PERSON' } })],
  ['task_build', task('task_build', 'RUNNING', { metadata: { unit: 'JOIN-UI-PERSON' } })],
  ['task_ship', task('task_ship', 'PARKED', { started_at: null, metadata: { unit: 'JOIN-UI-PERSON' } })],
])
/** An older workflow whose step task the board's window missed: its row is unread. */
const older = workflow('wf_older', 'SUCCEEDED', [step('implement', [], { task_id: 'task_older' })], 7200)

function serve(workflows: Workflow[], taskById: Map<string, Task>, nextPageToken: string | null = null): void {
  api.loadWorkflowBoard.mockResolvedValue({
    status: 'ok',
    data: { workflows, taskById, statesDetail: null, nextPageToken },
    fetchedAt: T0,
  } satisfies Result<WorkflowBoard>)
}

/** An IntersectionObserver that never reports a row in view. */
class NeverInView {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
  takeRecords(): IntersectionObserverEntry[] {
    return []
  }
}

function viewport(width: number) {
  vi.stubGlobal('matchMedia', (query: string) => {
    const max = /max-width:\s*(\d+)px/.exec(query)
    const min = /min-width:\s*(\d+)px/.exec(query)
    return {
      matches: (max === null || width <= Number(max[1])) && (min === null || width >= Number(min[1])) && (max !== null || min !== null),
      media: query,
      onchange: null,
      addEventListener: () => {},
      removeEventListener: () => {},
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    }
  })
}

function Routed({ initial }: { initial: string }) {
  const [view, setView] = useState(initial)
  return <WorkflowsScreen view={view} onView={setView} />
}

beforeEach(() => {
  vi.spyOn(Date, 'now').mockReturnValue(T0)
  vi.stubGlobal('IntersectionObserver', NeverInView)
  serve([chain], chainTasks)
  api.loadWorkflowUsage.mockResolvedValue({ status: 'empty', fetchedAt: T0 } satisfies Result<WorkflowUsage>)
  api.loadAttempts.mockResolvedValue({ status: 'empty', fetchedAt: T0 })
  api.loadTaskEventsPage.mockResolvedValue({ status: 'empty', fetchedAt: T0 })
  api.loadWorkflow.mockImplementation(ownFromBoard(api.loadWorkflowBoard))
  api.loadOlderWorkflows.mockReset()
  try {
    window.localStorage.clear()
  } catch {
    /* storage refused */
  }
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

const row = (id: string) => document.querySelector<HTMLElement>(`.wfl-table tr[data-workflow="${id}"]`)!

describe('VQ-013: the list is compact rows under one note', () => {
  // MUTATION: draw the two notes as two CountNotes again.
  it('says what the counts are of and how many rows are read in one note', async () => {
    // Built at runtime: an opaque cursor, not a credential.
    serve([chain, older], chainTasks, ['page', 'two'].join('-'))
    render(<WorkflowsScreen />)
    await waitFor(() => expect(row('wf_older')).toBeTruthy())
    const notes = document.querySelectorAll('.wfl .c-count-note')
    expect(notes).toHaveLength(1)
    expect(notes[0]!.textContent).toBe(
      'Counts and order are of the 2 newest workflows; older ones are on the server and not read yet · ' +
        '1 of 2 rows have their steps read; older rows show ids only until they scroll into view',
    )
    // No margin of its own on top of the list's gap.
    expect(painted(notes[0]!, 'margin', { width: 1440 })).toBe('0')
  })

  // MUTATION: put the id or the runner mix back on lines of their own.
  it('draws the name, its id and its runners on one line, and the current step as the one line under it', async () => {
    render(<WorkflowsScreen />)
    await waitFor(() => expect(row('wf_chain')).toBeTruthy())
    const name = row('wf_chain').querySelector<HTMLElement>('td[data-col="workflow"]')!
    const line = name.querySelector<HTMLElement>(':scope > .wfl-line')!
    expect(line, 'no name line').toBeTruthy()
    expect([...line.children].map((c) => c.className)).toEqual(['', 'id', 'wfl-mix'])
    expect(line.querySelector(':scope > a')!.textContent).toBe('JOIN-UI-PERSON')
    expect(line.querySelector(':scope > .id')!.textContent).toBe('wf_chain')
    // The current step is outside the line: the row's only second line, cut with its title.
    const why = name.querySelector<HTMLElement>(':scope > .wfl-why')!
    expect(why.textContent).toBe('build running')
    expect(why.title).toBe('build running')
    expect(painted(why, 'white-space', { width: 1440 })).toBe('nowrap')
    // One line in the table; the phone card lets it wrap.
    expect(painted(line, 'display', { width: 1440 })).toBe('flex')
    expect(painted(line, 'flex-wrap', { width: 1440 })).toBe('nowrap')
    expect(painted(line, 'flex-wrap', { width: 390 })).toBe('wrap')
    // The runner mix rides the line where the Runners column is not drawn, and not where it is.
    const mix = line.querySelector(':scope > .wfl-mix')!
    expect(painted(mix, 'display', { width: 1440 })).toBe('block')
    expect(painted(mix, 'display', { width: 1680 })).toBe('none')
  })
})

describe('VQ-014: a graph that fits across is as tall as it is', () => {
  // MUTATION: drop the `fits-x` class, or its rule: the canvas box is held to
  // the screen again and is a second scroller under the page's head.
  it('lets a short chain’s canvas box grow with the page, and keeps a wide one bounded', async () => {
    render(<Routed initial="wf=wf_chain" />)
    const wrap = await waitFor(() => {
      const w = document.querySelector<HTMLElement>('.wf-canvas-row > .wf-canvas-wrap')
      expect(w).toBeTruthy()
      return w!
    })
    expect(wrap.classList.contains('fits-x'), 'a three-step chain does not fit across').toBe(true)
    expect(painted(wrap, 'max-height', { width: 1440 })).toBe('none')
    expect(painted(wrap, ['overflow', 'overflow-y'], { width: 1440 })).toBe('visible')
    // The control: a canvas wider than its column keeps V034's bound.
    wrap.classList.remove('fits-x')
    expect(painted(wrap, 'max-height', { width: 1440 })).toMatch(/100vh/)
    expect(painted(wrap, ['overflow', 'overflow-y'], { width: 1440 })).toBe('auto')
  })
})

describe('VQ-015: one back path, by name, and Stop step', () => {
  const work = SECTIONS.find((s) => s.id === 'work')!
  const tab = work.tabs.find((t) => t.id === 'workflows')!
  const crumbs = (view: string, named: string | null) =>
    crumbsOf({
      at: { sectionId: 'work', tab: 'workflows', taskId: null, taskPane: 'detail', view },
      head: work.label,
      home: `${work.id}/${work.tabs[0]!.id}`,
      tab,
      tabs: work.tabs.length,
      title: named ?? 'wf_chain',
      closeTo: 'work/running/recent',
      named,
    })

  // MUTATION: label the object by its id alone again, or drop the filters
  // from the list segment.
  it('names the workflow last and goes back to the list it was opened from', () => {
    const c = crumbs('wf=wf_chain&state=failed&owner=priya', 'JOIN-UI-PERSON')
    expect(c.map((x) => x.label)).toEqual([work.label, 'Workflows · Failed · owner: priya', 'JOIN-UI-PERSON'])
    expect(c[1]!.to).toBe('work/workflows?state=failed&owner=priya')
    // Unfiltered, it is the list's own address; with no name read yet, the id.
    const plain = crumbs('wf=wf_chain', null)
    expect(plain.map((x) => x.label)).toEqual([work.label, tab.label, 'wf_chain'])
    expect(plain[1]!.to).toBe('work/workflows')
  })

  // MUTATION: draw the `‹ Workflows` link over the page head again.
  it('draws no back link of its own under the breadcrumb', async () => {
    render(<Routed initial="wf=wf_chain&state=failed" />)
    await screen.findByRole('heading', { level: 1, name: /JOIN-UI-PERSON|wf_chain/ })
    await waitFor(() => expect(document.querySelector('.wfp')).toBeTruthy())
    expect([...document.querySelectorAll('a')].filter((a) => (a.textContent ?? '').startsWith('‹'))).toEqual([])
  })

  // MUTATION: hand the card StopRun's inline `stop` again.
  it('gives the step card a Stop step button in the page head’s danger style', async () => {
    render(<Routed initial="wf=wf_chain" />)
    const group = await screen.findByRole('group', { name: 'Stop step' })
    const stop = within(group).getByRole('button', { name: 'Stop step' })
    const cancel = screen.getByRole('button', { name: 'Cancel workflow' })
    // The head's Cancel workflow: the danger outline at the default size.
    expect(cancel.classList.contains('is-danger')).toBe(true)
    expect(stop.classList.contains('is-danger'), 'Stop step is not the danger outline').toBe(true)
    expect(stop.classList.contains('is-sm'), 'Stop step is the small button').toBe(false)
  })
})

describe('VQ-016: the Table and the Timeline arrive whole', () => {
  // MUTATION: draw the arrival pick's card under its row again.
  for (const tabName of ['table', 'timeline'] as const) {
    it(`opens no card under the running step on arrival in the ${tabName}, and opens it when the row is asked for`, async () => {
      viewport(1099)
      render(<Routed initial={`wf=wf_chain&tab=${tabName}`} />)
      const pick = await waitFor(() => {
        const b = document.querySelector<HTMLButtonElement>('.wf-pick[data-step="build"]')
        expect(b?.getAttribute('aria-pressed')).toBe('true')
        return b!
      })
      expect(document.querySelector('.wf-inspect'), 'the arrival pick drew its card under the row').toBeNull()
      fireEvent.click(pick)
      await waitFor(() => expect(document.querySelector('.wf-inspect')).toBeTruthy())
      expect(document.activeElement?.classList.contains('wf-inspect-close')).toBe(true)
    })
  }
})
