// THE WORKFLOW'S OTHER VIEWS, ITS DATA EDGES, AND ITS SCRUBBERS -- AS BEHAVIOUR.
//
// redesign-v2 §2.3: "the view mode is a property of the pane, not a route. The
// same workflow becomes a DAG, a wall-clock timeline and a table without
// navigating." And viz #1 / #9: an edge that stages a file is a different edge
// from one that only orders two steps, and a file the worker really staged is
// drawn back into the graph it came from.
//
// Every claim below is made against the DOM the real screen produced. The api
// module is replaced with fixtures built HERE, because the cases this file is
// about -- one step id repeated across three workflows, a parked step that ran
// once before, an input staged from the submission -- are exactly the ones the
// development fixture does not contain.
//
// FOUR GROUPS, one per item:
//
//  U2  the board offers Timeline and Table beside Rows and Graph, and each is
//      honest about the three kinds of nothing (not started, not read, not
//      recorded) and about time WAITED versus time RUN.
//  U3  an edge that carries a file is drawn differently from one that does not,
//      and whether the file actually arrived is a third, separate fact.
//  U5  the inspector scrubs to the next attempt of a step and to the same step
//      in the next workflow, from the keyboard as well as the pointer.

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { useState } from 'react'

import type { Result } from '../fetch'
import type { StepUsage, WorkflowBoard, WorkflowUsage } from '../api'
import type { AttemptRow, Task, TaskState, Workflow, WorkflowStep } from '../types'

const api = vi.hoisted(() => ({
  loadWorkflowBoard: vi.fn(),
  loadWorkflowUsage: vi.fn(),
  loadAttempts: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => {
  const real = await importOriginal<typeof import('../api')>()
  return { ...real, ...api }
})

const { WorkflowCard, WorkflowsScreen } = await import('../Workflows')
const { inputsByStep, layoutOf, stepDuration } = await import('../dag')

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const T0 = Date.parse('2026-09-24T12:00:00.000Z')
const iso = (offsetSeconds: number) => new Date(T0 + offsetSeconds * 1000).toISOString()

function step(step_id: string, depends_on: string[], over: Partial<WorkflowStep> = {}): WorkflowStep {
  return {
    step_id,
    runner_profile: 'claude-code',
    resource_class: 'standard',
    depends_on,
    input_from: {},
    task_id: null,
    ...over,
  }
}

function task(id: string, state: TaskState, over: Partial<Task> = {}): Task {
  return {
    id,
    tenant_id: 'u-bogdan',
    state,
    runner_profile: 'claude-code',
    resource_class: 'standard',
    provider: 'anthropic',
    priority: 0,
    created_at: iso(-600),
    updated_at: iso(-60),
    started_at: null,
    completed_at: null,
    submitted_by: 'bogdan@saga.xyz',
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

function workflow(workflow_id: string, createdSecondsAgo: number, steps: WorkflowStep[]): Workflow {
  return {
    workflow_id,
    state: 'RUNNING',
    tenant_id: 'u-bogdan',
    stored_state: 'RUNNING',
    state_source: 'derived',
    rollup: {
      state: 'RUNNING',
      complete: true,
      reason: 'steps_hold_capacity',
      counts: { RUNNING: 1 },
      unreadable_steps: [],
      unstarted_steps: [],
      steps_read: steps.length,
    },
    created_at: iso(-createdSecondsAgo),
    updated_at: iso(-60),
    submitted_by: 'bogdan@saga.xyz',
    priority: 0,
    on_step_failure: 'FAIL_WORKFLOW',
    cancel_requested: false,
    steps,
  }
}

/**
 * THREE WORKFLOWS, AND `plan` IS IN ALL OF THEM. Newest first: `wf_new` (ten
 * minutes old), `wf_mid` (an hour), `wf_old` (two hours). The board's own
 * order is deliberately NOT that order, so a scrubber that walked the board's
 * array instead of the workflows' ages would visit them wrongly.
 */
function board(): { board: WorkflowBoard; usage: WorkflowUsage } {
  const wfOld = workflow('wf_old', 7200, [
    step('plan', [], { task_id: 't_plan_old' }),
    step('build', ['plan'], { task_id: 't_build_old', input_from: { plan: 'plan.md' } }),
  ])
  const wfNew = workflow('wf_new', 600, [
    step('plan', [], { task_id: 't_plan' }),
    // A DATA edge: `build` stages `plan.md` out of `plan`.
    step('build', ['plan'], { task_id: 't_build', input_from: { plan: 'plan.md' } }),
    // An ORDER edge: `scan` waits for `plan` and takes nothing from it.
    step('scan', ['plan'], { task_id: 't_scan' }),
    // No task: the workflow has not reached it.
    step('ship', ['build', 'scan'], { runner_profile: 'generic' }),
  ])
  const wfMid = workflow('wf_mid', 3600, [
    step('plan', [], { task_id: 't_plan_mid' }),
    // A task id the task read did not return: state unread.
    step('lost', ['plan'], { task_id: 't_lost' }),
  ])

  const tasks: Task[] = [
    task('t_plan', 'SUCCEEDED', { started_at: iso(-590), completed_at: iso(-500), attempt_count: 3 }),
    // Running: started 400s ago, still going.
    task('t_build', 'RUNNING', { started_at: iso(-400) }),
    // PARKED AFTER AN EARLIER ATTEMPT RAN. `started_at` is overwritten per
    // attempt (control.py:410-413) and survives the park, so a naive reading of
    // it says "running for 7m 30s" about a step that is holding nothing.
    task('t_scan', 'PARKED', {
      started_at: iso(-450),
      attempt_count: 2,
      park_reason: 'QUOTA_EXHAUSTED',
      updated_at: iso(-100),
    }),
    // Terminal with no completion time: when it ended was never written.
    task('t_plan_mid', 'FAILED', { created_at: iso(-3600), started_at: iso(-3500) }),
    task('t_plan_old', 'SUCCEEDED', {
      created_at: iso(-7200),
      started_at: iso(-7190),
      completed_at: iso(-7000),
    }),
    task('t_build_old', 'SUCCEEDED', {
      created_at: iso(-7200),
      started_at: iso(-6990),
      completed_at: iso(-6800),
      result_summary: {
        staged_inputs: [
          { task_id: 't_plan_old', filename: 'plan.md', path: 'plan.md', bytes: 2048 },
          // NO task_id. viz #9: this came from the submission, not from nowhere.
          { filename: 'brief.txt', path: 'brief.txt', bytes: 10 },
        ],
      },
    }),
  ]
  const taskById = new Map(tasks.map((t) => [t.id, t]))

  const usageOf = (costUsd: number | null, attempts = 1): StepUsage => ({
    attempts,
    attemptsWithCost: costUsd === null ? 0 : attempts,
    attemptsWithTokens: 0,
    checkpoints: 0,
    costUsd,
    inputTokens: null,
    outputTokens: null,
    cacheReadTokens: null,
    cacheCreationTokens: null,
  })
  const usage: WorkflowUsage = {
    byTaskId: new Map([
      ['t_plan', usageOf(0.5, 3)],
      ['t_build', usageOf(0.1)],
      ['t_scan', usageOf(null)],
      ['t_plan_mid', usageOf(null)],
      ['t_plan_old', usageOf(null)],
      ['t_build_old', usageOf(0.2)],
    ]),
    notSampled: new Set(),
    failed: new Map(),
    sampleLimit: 12,
    tasksRequested: 7,
  }

  return {
    board: { workflows: [wfOld, wfNew, wfMid], taskById, statesDetail: null },
    usage,
  }
}

function attempt(n: number, over: Partial<AttemptRow> = {}): AttemptRow {
  return {
    attempt_id: `att_${n}`,
    task_id: 't_plan',
    tenant_id: 'u-bogdan',
    generation: n,
    lease_id: `lease_${n}`,
    backend: 'cloud-run',
    execution_name: null,
    created_at: iso(-600 + n * 5),
    started_at: iso(-598 + n * 5),
    completed_at: iso(-596 + n * 5),
    exit_code: 0,
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

/** `t_plan`'s three attempts, NEWEST FIRST -- the order the route serves them. */
const PLAN_ATTEMPTS: AttemptRow[] = [
  attempt(3, { exit_code: 0, cost_usd: 0.3 }),
  attempt(2, { exit_code: 137, cost_usd: 0.2, error: 'killed: out of memory\nat step 4' }),
  attempt(1, { exit_code: 1, error: 'lease lost' }),
]

beforeEach(() => {
  vi.spyOn(Date, 'now').mockReturnValue(T0)
  const { board: b, usage } = board()
  api.loadWorkflowBoard.mockResolvedValue({ status: 'ok', data: b, fetchedAt: T0 } satisfies Result<WorkflowBoard>)
  api.loadWorkflowUsage.mockResolvedValue({ status: 'ok', data: usage, fetchedAt: T0 } satisfies Result<WorkflowUsage>)
  api.loadAttempts.mockImplementation(async (taskId: string) =>
    taskId === 't_plan'
      ? { status: 'ok', data: { attempts: PLAN_ATTEMPTS }, fetchedAt: T0 }
      : { status: 'empty', fetchedAt: T0 },
  )
})

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

async function landed(): Promise<void> {
  render(<WorkflowsScreen />)
  await screen.findByText('wf_new')
  // The attempt read the board starts once it has landed; its figures are what
  // the table's cost column sorts on.
  await waitFor(() => expect(api.loadWorkflowUsage).toHaveBeenCalled())
}

function boardModes(): HTMLButtonElement[] {
  const seg = document.querySelector('.wf-chrome .ctl-seg')
  expect(seg, 'the board has no mode control').toBeTruthy()
  return [...seg!.querySelectorAll<HTMLButtonElement>('button')]
}

function chooseBoard(label: string): void {
  const b = boardModes().find((x) => x.textContent === label)
  expect(b, `the board mode control has no "${label}"`).toBeTruthy()
  fireEvent.click(b!)
}

function cardOf(id: string): HTMLElement {
  const c = [...document.querySelectorAll<HTMLElement>('.wf-card')].find(
    (el) => el.querySelector('.wf-bar .id')?.textContent === id,
  )
  expect(c, `no card for ${id}`).toBeTruthy()
  return c!
}

function track(card: HTMLElement, stepId: string): HTMLElement {
  const t = card.querySelector<HTMLElement>(`.wf-tl-track[data-step="${stepId}"]`)
  expect(t, `no timeline track for ${stepId}`).toBeTruthy()
  return t!
}

const pct = (el: HTMLElement, prop: 'left' | 'width') => parseFloat(el.style[prop])

function rowOrder(card: HTMLElement): string[] {
  return [...card.querySelectorAll<HTMLElement>('.wf-table tbody tr')].map(
    (tr) => tr.getAttribute('data-step') ?? '',
  )
}

function cell(card: HTMLElement, stepId: string, col: string): HTMLElement {
  const td = card.querySelector<HTMLElement>(`.wf-table tr[data-step="${stepId}"] td[data-col="${col}"]`)
  expect(td, `no ${col} cell for ${stepId}`).toBeTruthy()
  return td!
}

function sortBy(card: HTMLElement, col: string): HTMLElement {
  const th = card.querySelector<HTMLElement>(`.wf-table th[data-col="${col}"]`)
  expect(th, `no ${col} column head`).toBeTruthy()
  const b = th!.querySelector('button')
  expect(b, `the ${col} column head is not a control`).toBeTruthy()
  fireEvent.click(b!)
  return th!
}

// ---------------------------------------------------------------------------
// U2 -- Timeline and Table, beside Rows and Graph
// ---------------------------------------------------------------------------

describe('U2: the view modes', () => {
  it('offers Timeline and Table beside Rows and Graph, as one segmented control', async () => {
    await landed()
    const buttons = boardModes()
    expect(buttons.map((b) => b.textContent)).toEqual(['Rows', 'Graph', 'Timeline', 'Table'])
    // Rows is what the board lands on, as before.
    expect(buttons.find((b) => b.textContent === 'Rows')!.getAttribute('aria-pressed')).toBe('true')
    expect(document.querySelector('.wf-canvas, .wf-timeline, .wf-table')).toBeNull()
  })

  it('opens every workflow as a timeline, and draws no canvas', async () => {
    await landed()
    chooseBoard('Timeline')
    for (const id of ['wf_new', 'wf_mid', 'wf_old']) {
      const c = cardOf(id)
      expect(c.querySelector('.wf-timeline'), `${id} is not drawn as a timeline`).toBeTruthy()
      expect(c.querySelector('.wf-canvas')).toBeNull()
      expect(c.querySelector('.wf-table')).toBeNull()
    }
  })

  it('opens every workflow as a table, one row per step, in dependency order', async () => {
    await landed()
    chooseBoard('Table')
    expect(rowOrder(cardOf('wf_new'))).toEqual(['plan', 'build', 'scan', 'ship'])
    expect(rowOrder(cardOf('wf_mid'))).toEqual(['plan', 'lost'])
    expect(document.querySelector('.wf-canvas')).toBeNull()
  })

  it('switches ONE open workflow without touching the others, and a board choice overrules it', async () => {
    await landed()
    // Rows: open one card, which lands on its graph.
    fireEvent.click(cardOf('wf_new').querySelector('.wf-bar')!)
    const newCard = cardOf('wf_new')
    expect(newCard.querySelector('.wf-canvas')).toBeTruthy()
    const perCard = newCard.querySelector('.wf-viewbar .ctl-seg')
    expect(perCard, 'an open workflow has no view control of its own').toBeTruthy()
    fireEvent.click(within(perCard as HTMLElement).getByText('Table'))
    expect(cardOf('wf_new').querySelector('.wf-table')).toBeTruthy()
    // Nothing else opened.
    expect(cardOf('wf_mid').querySelector('.wf-body')).toBeNull()
    // A board-wide instruction clears the per-card one.
    chooseBoard('Graph')
    expect(cardOf('wf_new').querySelector('.wf-canvas')).toBeTruthy()
    expect(cardOf('wf_new').querySelector('.wf-table')).toBeNull()
  })

  describe('the timeline', () => {
    it('draws time WAITED apart from time RUN, and the run starts where the wait ends', async () => {
      await landed()
      chooseBoard('Timeline')
      const t = track(cardOf('wf_new'), 'plan')
      const waited = t.querySelector<HTMLElement>('.wf-tl-span.is-waited')
      const ran = t.querySelector<HTMLElement>('.wf-tl-span.is-ran')
      expect(waited, 'plan has no waited span').toBeTruthy()
      expect(ran, 'plan has no ran span').toBeTruthy()
      expect(pct(waited!, 'left')).toBeCloseTo(0, 3)
      // 10s of a 600s axis, then 90s.
      expect(pct(waited!, 'width')).toBeCloseTo((10 / 600) * 100, 2)
      expect(pct(ran!, 'left')).toBeCloseTo(pct(waited!, 'left') + pct(waited!, 'width'), 2)
      expect(pct(ran!, 'width')).toBeCloseTo((90 / 600) * 100, 2)
      expect(t.getAttribute('role')).toBe('img')
      expect(t.getAttribute('aria-label')).toMatch(/waited/)
      // It retried: three attempts, and the wait is to the LATEST start.
      expect(t.getAttribute('aria-label')).toMatch(/3 attempts/)
    })

    it('draws a running step as open-ended, reaching now, and never with an end', async () => {
      await landed()
      chooseBoard('Timeline')
      const t = track(cardOf('wf_new'), 'build')
      const running = t.querySelector<HTMLElement>('.wf-tl-span.is-running')
      expect(running, 'build has no running span').toBeTruthy()
      expect(running!.classList.contains('is-open')).toBe(true)
      expect(pct(running!, 'left') + pct(running!, 'width')).toBeCloseTo(100, 2)
      expect(t.querySelector('.wf-tl-span.is-ran')).toBeNull()
    })

    it('never draws a parked step as running, although it has a start time', async () => {
      await landed()
      chooseBoard('Timeline')
      const t = track(cardOf('wf_new'), 'scan')
      const spans = t.querySelectorAll<HTMLElement>('.wf-tl-span')
      expect(spans).toHaveLength(1)
      expect(spans[0]!.classList.contains('is-waiting')).toBe(true)
      expect(spans[0]!.classList.contains('is-open')).toBe(true)
      expect(t.querySelector('.is-running, .is-ran')).toBeNull()
      expect(t.getAttribute('aria-label')).toMatch(/2 attempts/)
    })

    it('draws the three kinds of nothing as three different words, and no bar for any of them', async () => {
      await landed()
      chooseBoard('Timeline')
      const unstarted = track(cardOf('wf_new'), 'ship')
      expect(unstarted.querySelector('.wf-tl-span')).toBeNull()
      expect(unstarted.textContent).toContain('not started')

      const unread = track(cardOf('wf_mid'), 'lost')
      expect(unread.querySelector('.wf-tl-span')).toBeNull()
      expect(unread.textContent).toContain('task unread')

      // FAILED with no completion time: it ran, it ended, nobody wrote when. A
      // bar reaching "now" would say it is still going; a bar of any length
      // would invent an end. The wait up to its start is real and is drawn.
      const unrecorded = track(cardOf('wf_mid'), 'plan')
      expect(unrecorded.querySelector('.wf-tl-span.is-ran, .wf-tl-span.is-running')).toBeNull()
      expect(unrecorded.textContent).toContain('end not recorded')
    })

    it('puts a time axis over the bars, starting at zero', async () => {
      await landed()
      chooseBoard('Timeline')
      const ticks = cardOf('wf_new').querySelectorAll('.wf-tl-tick')
      expect(ticks.length).toBeGreaterThan(1)
      expect(ticks[0]!.textContent).toBe('0')
    })
  })

  describe('the table', () => {
    it('sorts by cost, and an unmeasured cost is LAST whichever way it is sorted', async () => {
      await landed()
      chooseBoard('Table')
      const c = cardOf('wf_new')
      await waitFor(() => expect(cell(c, 'plan', 'cost').textContent).toContain('$0.50'))
      expect(cell(c, 'scan', 'cost').textContent).toBe('not reported')
      expect(cell(c, 'ship', 'cost').textContent).toBe('not started')

      const th = sortBy(c, 'cost')
      expect(th.getAttribute('aria-sort')).toBe('ascending')
      expect(rowOrder(c)).toEqual(['build', 'plan', 'scan', 'ship'])

      sortBy(c, 'cost')
      expect(th.getAttribute('aria-sort')).toBe('descending')
      // An absence is not a small number and it is not a large one.
      expect(rowOrder(c)).toEqual(['plan', 'build', 'scan', 'ship'])
    })

    it('sorts by time run, and a parked step is between attempts, not "so far"', async () => {
      await landed()
      chooseBoard('Table')
      const c = cardOf('wf_new')
      expect(cell(c, 'scan', 'ran').textContent).toBe('between attempts')
      expect(cell(c, 'scan', 'ran').textContent).not.toMatch(/so far/)
      sortBy(c, 'ran')
      expect(rowOrder(c)).toEqual(['plan', 'build', 'scan', 'ship'])
      sortBy(c, 'ran')
      expect(rowOrder(c)).toEqual(['build', 'plan', 'scan', 'ship'])
    })

    it('sorts by attempts and by state', async () => {
      await landed()
      chooseBoard('Table')
      const c = cardOf('wf_new')
      expect(cell(c, 'plan', 'attempts').textContent).toBe('3 of 3')
      expect(cell(c, 'ship', 'attempts').textContent).toBe('not started')
      sortBy(c, 'attempts')
      expect(rowOrder(c)).toEqual(['build', 'scan', 'plan', 'ship'])
      sortBy(c, 'state')
      // Running, then waiting, then succeeded; the step with no task last.
      expect(rowOrder(c)).toEqual(['build', 'scan', 'plan', 'ship'])
    })
  })
})

// ---------------------------------------------------------------------------
// One step, one story: the node, the timeline, the table and the inspector
// ---------------------------------------------------------------------------
//
// Four places draw a step's time, and each of these cases was a place where one
// of them said something the others did not. The worker code behind each:
//
//   * `control.park()` transitions to PARKED and releases the lease; it never
//     calls `record_attempt_end`, so a parked attempt keeps completed_at and
//     exit_code null for good. Its missing end is NOT "still going".
//   * `scheduler.store.create_attempt` writes the attempt at dispatch with
//     started_at null; the worker fills it in `record_attempt_start`. So a
//     LEASED or DISPATCHED task's newest attempt has no start because it has
//     not started YET -- not because it was fenced, parked or failed.
//   * a task cancelled while it was queued gets a completed_at and never a
//     started_at. Nothing in that span was time run.

function oneStepCard(
  t: Task,
  attempts: AttemptRow[],
  view: 'graph' | 'timeline' | 'table' = 'table',
  extra: { steps?: WorkflowStep[]; tasks?: Task[] } = {},
) {
  const w = workflow('wf_one', 900, [...(extra.steps ?? []), step('work', [], { task_id: t.id })])
  const tasks = new Map<string, Task>([[t.id, t], ...(extra.tasks ?? []).map((x) => [x.id, x] as [string, Task])])
  const load = async (): Promise<Result<{ attempts: AttemptRow[] }>> =>
    attempts.length === 0
      ? { status: 'empty', fetchedAt: T0 }
      : { status: 'ok', data: { attempts }, fetchedAt: T0 }
  return render(
    <WorkflowCard
      workflow={w}
      taskById={tasks}
      expanded
      usage={{ kind: 'ready', usage: null }}
      onToggle={() => {}}
      openStages={{}}
      onToggleStage={() => {}}
      view={view}
      loadAttempts={load}
      reload={() => {}}
    />,
  )
}

async function inspectWork(t: Task, attempts: AttemptRow[]): Promise<HTMLElement> {
  const { container } = oneStepCard(t, attempts)
  pick(container as HTMLElement, 'work')
  const i = inspector(container as HTMLElement)
  await within(i).findByText(`attempt ${attempts.length} of ${attempts.length}`)
  return i
}

describe('U2: a step reads the same in every view', () => {
  it('does not call a parked attempt running in the inspector, where the node and the timeline say waiting', async () => {
    // PARKED on QUOTA_EXHAUSTED two hours into its run. The node says
    // `between attempts`, the timeline draws it waiting; the inspector said
    // `2h 0m so far` (a measured, ticking cell) and `still running`.
    const parked = task('t_park', 'PARKED', {
      created_at: iso(-9000),
      started_at: iso(-7200),
      park_reason: 'QUOTA_EXHAUSTED',
      updated_at: iso(-60),
    })
    const i = await inspectWork(parked, [
      attempt(1, { task_id: 't_park', created_at: iso(-7300), started_at: iso(-7200), completed_at: null, exit_code: null }),
    ])
    const took = fact(i, 'took')
    const exit = fact(i, 'exit')
    expect(took, 'a parked attempt is timed as if it were still running').not.toMatch(/so far/)
    expect(exit, 'a parked attempt is called running').not.toBe('still running')
    expect(took).toMatch(/end not written/)
    // And its figure is an absence, not a measurement.
    const tookLi = [...i.querySelectorAll('.ctl-fact')].find((el) => el.querySelector('b')?.textContent === 'took')!
    expect(tookLi.classList.contains('is-absent')).toBe(true)
  })

  it('says a dispatched attempt has not started YET, not that it never started', async () => {
    // A GKE cold start: DISPATCHED, and the attempt the scheduler wrote at
    // dispatch carries started_at null until the container records it.
    const dispatched = task('t_disp', 'DISPATCHED', { created_at: iso(-120) })
    const i = await inspectWork(dispatched, [
      attempt(1, { task_id: 't_disp', created_at: iso(-60), started_at: null, completed_at: null, exit_code: null }),
    ])
    expect(fact(i, 'took')).toBe('not started yet')
    expect(fact(i, 'exit'), 'an attempt that has not started is called running').not.toBe('still running')
    expect(i.textContent).not.toMatch(/never started/)
  })

  it('still times a running attempt, and only that one, as "so far"', async () => {
    const running = task('t_run', 'RUNNING', { created_at: iso(-600), started_at: iso(-300), attempt_count: 2 })
    const i = await inspectWork(running, [
      attempt(1, { task_id: 't_run', created_at: iso(-590), started_at: iso(-580), completed_at: null, exit_code: null }),
      attempt(2, { task_id: 't_run', created_at: iso(-310), started_at: iso(-300), completed_at: null, exit_code: null }),
    ])
    expect(fact(i, 'took')).toBe('5m 0s so far')
    expect(fact(i, 'exit')).toBe('still running')
    // The EARLIER attempt of the same running task stopped without an end: it
    // is over, whatever the task is doing now.
    fireEvent.click(within(i).getByRole('button', { name: 'Previous attempt' }))
    expect(fact(i, 'took')).not.toMatch(/so far/)
    expect(fact(i, 'exit')).not.toBe('still running')
  })

  it('says a step cancelled before it started never started, in the graph, the timeline and the table alike', () => {
    // Cancelled while queued: a completed_at, no started_at. The node said
    // `ran 3m 0s`; the table said `not started`; the timeline drew no run.
    const plan = task('t_first', 'SUCCEEDED', { created_at: iso(-600), started_at: iso(-590), completed_at: iso(-500) })
    const dropped = task('t_drop', 'CANCELLED', { created_at: iso(-480), completed_at: iso(-300) })
    const { container } = oneStepCard(dropped, [], 'graph', {
      steps: [step('first', [], { task_id: 't_first' })],
      tasks: [plan],
    })
    const root = container as HTMLElement
    // EVERYTHING THE NODE SAYS ABOUT ITS TIME, wherever the tier puts it: the
    // duration line where it is drawn, and the `ran` figure at the Figures tier
    // -- which is where this two-step card lands, and where the line is not
    // drawn for a step that never started because the figure already says so.
    const workNode = nodeNamed(root, 'work')
    const said = [
      ...workNode.querySelectorAll('.node-dur'),
      ...[...workNode.querySelectorAll('.node-num')]
        .filter((n) => n.querySelector('dt')?.textContent === 'ran')
        .map((n) => n.querySelector('dd')!),
    ].map((e) => e.textContent ?? '')
    expect(said.length, 'the node says nothing about its time').toBeGreaterThan(0)
    expect(said.filter((t) => /^ran\b|^\d/.test(t)), 'the node claims run time nothing recorded').toEqual([])
    expect(said).toContain('never started')

    const seg = root.querySelector('.wf-viewbar .ctl-seg') as HTMLElement
    fireEvent.click(within(seg).getByText('Timeline'))
    const t = track(root, 'work')
    expect(t.querySelector('.wf-tl-span.is-ran, .wf-tl-span.is-running')).toBeNull()
    expect(t.textContent).toContain('never started')

    fireEvent.click(within(seg).getByText('Table'))
    expect(cell(root, 'work', 'ran').textContent).toBe('never started')
  })
})

// ---------------------------------------------------------------------------
// The node's own duration line agrees with the timeline
// ---------------------------------------------------------------------------

describe('a step waiting for its next attempt', () => {
  it('is waiting on the node too, not "running" off a start time that belongs to an attempt that is over', () => {
    // LEASED for attempt 2: `started_at` is attempt 1's, and it survived the
    // reclaim. The node's line read `running 7m 30s` about a step holding
    // nothing -- the same misreading the timeline refuses.
    const retry = task('t_retry', 'LEASED', { started_at: iso(-450), attempt_count: 2 })
    const d = stepDuration({ kind: 'state', state: 'LEASED', task: retry }, T0)
    expect(d.kind).toBe('queued')
    expect(d.text).toBe('waiting 10m 0s')
    expect(d.text).not.toMatch(/running/)
    // A step that really is running still says so.
    const live = task('t_live', 'RUNNING', { started_at: iso(-60) })
    expect(stepDuration({ kind: 'state', state: 'RUNNING', task: live }, T0).text).toBe('running 1m 0s')
  })
})

// ---------------------------------------------------------------------------
// U3 -- data edges, and the files that actually arrived
// ---------------------------------------------------------------------------

function edgeCard(): { w: Workflow; tasks: Map<string, Task> } {
  const w = workflow('wf_edges', 600, [
    step('plan', [], { task_id: 'tp' }),
    step('build', ['plan'], { task_id: 'tb', input_from: { plan: 'plan.md' } }),
    step('scan', ['plan'], { task_id: 'ts' }),
    step('check', ['build'], { task_id: 'tc', input_from: { build: 'patch.diff' } }),
  ])
  const tasks = new Map<string, Task>([
    ['tp', task('tp', 'SUCCEEDED', { started_at: iso(-590), completed_at: iso(-500) })],
    [
      'tb',
      task('tb', 'SUCCEEDED', {
        started_at: iso(-490),
        completed_at: iso(-300),
        result_summary: {
          staged_inputs: [{ task_id: 'tp', filename: 'plan.md', path: 'plan.md', bytes: 2048 }],
        },
      }),
    ],
    ['ts', task('ts', 'SUCCEEDED', { started_at: iso(-490), completed_at: iso(-400) })],
    // Running: it staged `patch.diff` when it started, and result_summary is
    // written at FINISH, so the staging is not reported yet. Not "not staged".
    ['tc', task('tc', 'RUNNING', { started_at: iso(-200) })],
  ])
  return { w, tasks }
}

function Card({ w, tasks }: { w: Workflow; tasks: Map<string, Task> }) {
  const [stages, setStages] = useState<Record<string, boolean>>({})
  return (
    <WorkflowCard
      workflow={w}
      taskById={tasks}
      expanded
      usage={{ kind: 'ready', usage: null }}
      onToggle={() => {}}
      openStages={stages}
      onToggleStage={(key, was) => setStages((s) => ({ ...s, [key]: !was }))}
      reload={() => {}}
    />
  )
}

function nodeNamed(root: ParentNode, name: string): HTMLElement {
  const n = [...root.querySelectorAll<HTMLElement>('.node')].find((el) =>
    el.querySelector('.node-id')?.textContent?.startsWith(name),
  )
  expect(n, `no node named ${name}`).toBeTruthy()
  return n!
}

describe('U3: an edge that carries a file', () => {
  it('draws input_from apart from depends_on, one mark per pair either way', () => {
    const { w, tasks } = edgeCard()
    const { container } = render(<Card w={w} tasks={tasks} />)
    // THE MARKS ARE THE GROUPS THAT NAME THEIR PAIR. The halo under each edge
    // is painted in a layer of its own, beneath every stroke, and its group
    // carries the kind class so the sheet can widen a data edge's clearance --
    // but it names no pair, because it is clearance and not a mark.
    const links = container.querySelectorAll('.wf-link[data-edge]')
    expect(links).toHaveLength(3)
    // One path per pair is unchanged: a data edge is a KIND of edge, not a
    // second edge drawn on top of the first.
    expect(container.querySelectorAll('.wf-edge')).toHaveLength(3)
    for (const l of links) expect(l.querySelectorAll('.wf-edge')).toHaveLength(1)
    expect(container.querySelectorAll('.wf-link.is-order[data-edge]')).toHaveLength(1)
    expect(container.querySelectorAll('.wf-link.is-staged[data-edge]')).toHaveLength(1)
    expect(container.querySelectorAll('.wf-link.is-declared[data-edge]')).toHaveLength(1)
    expect(container.querySelector('.wf-link.is-order[data-edge]')!.getAttribute('data-edge')).toBe('plan->scan')
    expect(container.querySelector('.wf-link.is-staged[data-edge]')!.getAttribute('data-edge')).toBe('plan->build')
    expect(container.querySelector('.wf-link.is-declared[data-edge]')!.getAttribute('data-edge')).toBe('build->check')
  })

  it('names the file on the dependency line, and marks whether it arrived', () => {
    const { w, tasks } = edgeCard()
    const { container } = render(<Card w={w} tasks={tasks} />)
    const build = nodeNamed(container, 'build')
    expect(build.querySelector('.node-dep')!.textContent).toBe('↑ plan (plan.md)')
    expect(build.querySelector('.node-dep-file.is-staged')).toBeTruthy()
    const check = nodeNamed(container, 'check')
    expect(check.querySelector('.node-dep-file.is-declared')).toBeTruthy()
    // The title says which kind of not-yet it is.
    expect(check.querySelector('.node-dep-file')!.getAttribute('title')).toMatch(/finish/)
    const scan = nodeNamed(container, 'scan')
    expect(scan.querySelector('.node-dep')!.textContent).toBe('↑ plan')
    expect(scan.querySelector('.node-dep-file')).toBeNull()
  })

  it('makes the node tall enough for the longer dependency line it now prints', () => {
    const plain = layoutOf([step('a', []), step('b', ['a'])])
    const withFile = layoutOf([
      step('a', []),
      step('b', ['a'], { input_from: { a: 'a-very-long-artifact-name-that-wraps.md' } }),
    ])
    const hOf = (l: typeof plain) => l.nodes.find((n) => n.step.step_id === 'b')!.h
    expect(hOf(withFile)).toBeGreaterThan(hOf(plain))
  })

  it('lists staged files in the table, with their size, and one from the submission as such', async () => {
    await landed()
    chooseBoard('Table')
    const old = cardOf('wf_old')
    const inputs = cell(old, 'build', 'inputs')
    expect(inputs.textContent).toContain('plan.md')
    expect(inputs.textContent).toContain('2 KiB')
    expect(inputs.textContent).toContain('brief.txt')
    expect(inputs.textContent).toContain('submission')
    // A declared input on a step still running is not "not staged".
    const fresh = cell(cardOf('wf_new'), 'build', 'inputs')
    expect(fresh.textContent).toContain('plan.md')
    expect(fresh.textContent).toContain('reported at finish')
    // A step that declares nothing says so; it is not an absence.
    expect(cell(cardOf('wf_new'), 'scan', 'inputs').textContent).toBe('none')
  })

  it('does not call a file "not reported" when the report holds an entry it could not read', () => {
    // `build` declared `plan.md` from `plan`, and its result's only staged
    // entry has no filename -- counted as malformed. That entry may BE plan.md.
    // The table said `1 unreadable`; the graph said "its result lists no such
    // file", which the result does not say.
    const w = workflow('wf_bad', 600, [
      step('plan', [], { task_id: 'tp' }),
      step('build', ['plan'], { task_id: 'tb', input_from: { plan: 'plan.md' } }),
    ])
    const tasks = new Map<string, Task>([
      ['tp', task('tp', 'SUCCEEDED', { started_at: iso(-590), completed_at: iso(-500) })],
      [
        'tb',
        task('tb', 'SUCCEEDED', {
          started_at: iso(-490),
          completed_at: iso(-300),
          result_summary: { staged_inputs: [{ task_id: 'tp', path: 'plan.md', bytes: 2048 }] },
        }),
      ],
    ])
    const { container } = render(<Card w={w} tasks={tasks} />)
    const file = nodeNamed(container, 'build').querySelector<HTMLElement>('.node-dep-file')!
    expect(file.getAttribute('title'), 'the graph says the result lists no such file').not.toMatch(/lists no such file/)
    expect(file.getAttribute('title')).toMatch(/could not read/)
    // And the count reaches the card in every view, not only the table cell.
    const mark = container.querySelector<HTMLElement>('.wf-viewbar .wf-unreadable')
    expect(mark, 'the unreadable entry is counted only in the table').toBeTruthy()
    expect(mark!.textContent).toBe('1 input unreadable')
    expect(mark!.getAttribute('aria-label')).toMatch(/build/)
  })

  it('draws a collapsed stage’s edge as the weakest of the edges it stands for, whichever child is drawn last', () => {
    // Every edge into a COLLAPSED stage attaches to the band's centre, so all
    // thirteen share one path, and each group's halo paints over the ones
    // before it. The band edge used to show the LAST child's kind as the
    // whole stage's. Four orders of the same thirteen children:
    type Kid = 'running' | 'staged' | 'order'
    const fan = (kids: Kid[]) => {
      const steps: WorkflowStep[] = [step('plan', [], { task_id: 'tp' })]
      const tasks = new Map<string, Task>([
        ['tp', task('tp', 'SUCCEEDED', { started_at: iso(-590), completed_at: iso(-500) })],
      ])
      kids.forEach((k, n) => {
        const id = `impl-${n + 1}`
        steps.push(step(id, ['plan'], { task_id: `t_${id}`, input_from: k === 'order' ? {} : { plan: 'plan.md' } }))
        tasks.set(
          `t_${id}`,
          k === 'running'
            ? task(`t_${id}`, 'RUNNING', { started_at: iso(-400) })
            : task(`t_${id}`, 'SUCCEEDED', {
                started_at: iso(-400),
                completed_at: iso(-100),
                result_summary:
                  k === 'staged'
                    ? { staged_inputs: [{ task_id: 'tp', filename: 'plan.md', path: 'plan.md', bytes: 10 }] }
                    : {},
              }),
        )
      })
      steps.push(step('report', kids.map((_, n) => `impl-${n + 1}`)))
      return { w: workflow('wf_fan', 600, steps), tasks }
    }
    const bandEdges = (kids: Kid[]) => {
      const { w, tasks } = fan(kids)
      const { container, unmount } = render(<Card w={w} tasks={tasks} />)
      expect(container.querySelector('.wf-band'), 'the thirteen-wide stage is not collapsed').toBeTruthy()
      const into = [...container.querySelectorAll('.wf-link')].filter((g) =>
        (g.getAttribute('data-edge') ?? '').startsWith('plan->'),
      )
      expect(into).toHaveLength(13)
      const classes = new Set(into.map((g) => g.getAttribute('class')))
      unmount()
      return classes
    }
    const twelve = (k: Kid): Kid[] => Array.from({ length: 12 }, () => k)

    // Twelve still running, the last one staged: ONE of thirteen reported.
    expect(bandEdges([...twelve('running'), 'staged'])).toEqual(new Set(['wf-link is-data is-declared']))
    // Twelve staged, the last still running: still not every one reported.
    expect(bandEdges([...twelve('staged'), 'running'])).toEqual(new Set(['wf-link is-data is-declared']))
    // Twelve that take nothing and one that takes a file: it is a data edge,
    // and not every member of it reported one.
    expect(bandEdges([...twelve('order'), 'staged'])).toEqual(new Set(['wf-link is-data is-declared']))
    // All thirteen reported: then, and only then, the stage's edge is solid.
    expect(bandEdges([...twelve('staged'), 'staged'])).toEqual(new Set(['wf-link is-data is-staged']))
  })

  it('marks, on the open card, a staged input no edge can carry', async () => {
    await landed()
    chooseBoard('Graph')
    const mark = cardOf('wf_old').querySelector<HTMLElement>('.wf-strays')
    expect(mark, 'an input from outside the graph is not marked').toBeTruthy()
    expect(mark!.textContent).toBe('1 input off-graph')
    expect(mark!.getAttribute('aria-label')).toMatch(/brief\.txt/)
    expect(mark!.getAttribute('aria-label')).toMatch(/submission/)
    expect(cardOf('wf_new').querySelector('.wf-strays')).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// U5 -- the scrubbers
// ---------------------------------------------------------------------------

function pick(card: HTMLElement, stepId: string): void {
  const b = card.querySelector<HTMLButtonElement>(`.wf-pick[data-step="${stepId}"]`)
  expect(b, `no way to select ${stepId}`).toBeTruthy()
  fireEvent.click(b!)
}

function inspector(card: HTMLElement): HTMLElement {
  const i = card.querySelector<HTMLElement>('.wf-inspect')
  expect(i, 'the card holds no inspector').toBeTruthy()
  return i!
}

function fact(root: HTMLElement, key: string): string {
  const li = [...root.querySelectorAll('.ctl-fact')].find((el) => el.querySelector('b')?.textContent === key)
  expect(li, `no ${key} fact`).toBeTruthy()
  return (li!.textContent ?? '').slice(key.length)
}

describe('U5: scrubbing', () => {
  it('steps to the next and previous attempt of a step, by pointer and by keyboard', async () => {
    await landed()
    chooseBoard('Table')
    const c = cardOf('wf_new')
    pick(c, 'plan')
    const i = inspector(c)
    // The link to the full run is on the inspector; selecting did not navigate.
    expect(i.querySelector('a[href="#work/task/t_plan"]')).toBeTruthy()
    // Newest attempt first, because that is the one a reader asked about.
    await within(i).findByText('attempt 3 of 3')
    expect(fact(i, 'gen')).toBe('3')
    // AT ITS END, NOT DISABLED: `aria-disabled`, so the control keeps focus and
    // the arrow keys keep working (see the next test).
    expect(within(i).getByRole('button', { name: 'Next attempt' }).getAttribute('aria-disabled')).toBe('true')

    fireEvent.click(within(i).getByRole('button', { name: 'Previous attempt' }))
    expect(within(i).getByText('attempt 2 of 3')).toBeTruthy()
    expect(fact(i, 'gen')).toBe('2')
    expect(fact(i, 'exit')).toBe('137')

    const group = i.querySelector<HTMLElement>('[data-scrub="attempt"]')
    expect(group).toBeTruthy()
    fireEvent.keyDown(group!, { key: 'ArrowLeft' })
    expect(within(i).getByText('attempt 1 of 3')).toBeTruthy()
    expect(within(i).getByRole('button', { name: 'Previous attempt' }).getAttribute('aria-disabled')).toBe('true')
    // Pressing a control at its end does nothing.
    fireEvent.click(within(i).getByRole('button', { name: 'Previous attempt' }))
    expect(within(i).getByText('attempt 1 of 3')).toBeTruthy()
    // And the unmeasured cost of attempt 1 is a word, not $0.00.
    expect(fact(i, 'cost')).toBe('not reported')
    fireEvent.keyDown(group!, { key: 'ArrowRight' })
    expect(within(i).getByText('attempt 2 of 3')).toBeTruthy()
  })

  it('steps to the same step in the next workflow, newest first, and keeps focus on the control', async () => {
    await landed()
    chooseBoard('Table')
    pick(cardOf('wf_new'), 'plan')
    const first = inspector(cardOf('wf_new'))
    expect(within(first).getByText('workflow 1 of 3')).toBeTruthy()
    expect(
      within(first).getByRole('button', { name: 'Same step, newer workflow' }).getAttribute('aria-disabled'),
    ).toBe('true')

    fireEvent.click(within(first).getByRole('button', { name: 'Same step, older workflow' }))
    // The selection MOVED: it is now wf_mid's `plan`, inspected in wf_mid's card.
    expect(cardOf('wf_new').querySelector('.wf-inspect')).toBeNull()
    const second = inspector(cardOf('wf_mid'))
    expect(within(second).getByText('workflow 2 of 3')).toBeTruthy()
    expect(second.querySelector('a[href="#work/task/t_plan_mid"]')).toBeTruthy()
    // Pressing it again must keep working: focus followed the selection.
    expect(document.activeElement).toBe(
      within(second).getByRole('button', { name: 'Same step, older workflow' }),
    )

    const group = second.querySelector<HTMLElement>('[data-scrub="workflow"]')
    expect(group).toBeTruthy()
    fireEvent.keyDown(group!, { key: 'ArrowRight' })
    const third = inspector(cardOf('wf_old'))
    expect(within(third).getByText('workflow 3 of 3')).toBeTruthy()
    const older = within(third).getByRole('button', { name: 'Same step, older workflow' })
    expect(older.getAttribute('aria-disabled')).toBe('true')
    // FOCUS LANDS ON THE CONTROL THE READER WAS PRESSING, even at its end. It
    // used to fall back to the other button, so a reader holding ArrowRight
    // found themselves on "newer" with nothing said.
    expect(document.activeElement).toBe(older)
    // And from there the other direction still works.
    fireEvent.keyDown(document.activeElement!, { key: 'ArrowLeft' })
    expect(within(inspector(cardOf('wf_mid'))).getByText('workflow 2 of 3')).toBeTruthy()
  })

  it('keeps focus on a scrub control that reaches its end, so the arrow keys keep working', async () => {
    await landed()
    chooseBoard('Table')
    const c = cardOf('wf_new')
    pick(c, 'plan')
    const i = inspector(c)
    await within(i).findByText('attempt 3 of 3')
    const prev = within(i).getByRole('button', { name: 'Previous attempt' })
    prev.focus()
    expect(document.activeElement).toBe(prev)
    fireEvent.keyDown(prev, { key: 'ArrowLeft' })
    fireEvent.keyDown(prev, { key: 'ArrowLeft' })
    expect(within(i).getByText('attempt 1 of 3')).toBeTruthy()
    // A BROWSER TAKES FOCUS OFF A CONTROL THE MOMENT IT BECOMES `disabled`
    // (the HTML focus fixup rule), and from <body> no arrow key reaches the
    // scrubber. jsdom does not run that rule, so this asks the question it can
    // answer: at its end, can the control take focus at all?
    //
    // Focus goes to ANOTHER REAL CONTROL first, not through `blur()`. The first
    // version of this test called `blur()` on the focused button, and jsdom's
    // `blur()` is a no-op on a disabled element -- so focus never left it, and
    // the test passed against the very defect it names (CI run 35979486837).
    within(i).getByRole('button', { name: 'Stop inspecting this step' }).focus()
    expect(document.activeElement).not.toBe(prev)
    prev.focus()
    expect(document.activeElement, 'the control at its end cannot take focus').toBe(prev)
    fireEvent.keyDown(document.activeElement!, { key: 'ArrowRight' })
    expect(within(i).getByText('attempt 2 of 3')).toBeTruthy()
  })

  it('says a step with no task has no attempt to scrub, rather than showing none', async () => {
    await landed()
    chooseBoard('Timeline')
    const c = cardOf('wf_new')
    pick(c, 'ship')
    const i = inspector(c)
    expect(i.querySelector('[data-scrub="attempt"]')!.textContent).toContain('not started')
    expect(api.loadAttempts).not.toHaveBeenCalledWith(undefined)
    // Picking it again puts it down.
    pick(c, 'ship')
    expect(c.querySelector('.wf-inspect')).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// The visual QA pass of 2026-09-25 (epics #83 and #87)
// ---------------------------------------------------------------------------
//
// One box each, committed RED against the code it describes before the fix
// landed.

describe('the QA pass: the table, the inspector and the board chrome', () => {
  it('WF-6: lists every declared input on its own line, in the order the step depends on them', () => {
    // `input_from` in the REVERSE of `depends_on`, which is what a Firestore map
    // is free to hand back. merge-1..4 each declared two files and failed on
    // the second; the cell showed one, clipped, and a different one per read.
    const w = workflow('wf_merge', 600, [
      step('left', [], { task_id: 'tl' }),
      step('right', [], { task_id: 'tr' }),
      step('merge', ['left', 'right'], { task_id: 'tm', input_from: { right: 'right.md', left: 'left.md' } }),
    ])
    const tasks = new Map<string, Task>([
      ['tl', task('tl', 'SUCCEEDED', { started_at: iso(-590), completed_at: iso(-500) })],
      ['tr', task('tr', 'SUCCEEDED', { started_at: iso(-590), completed_at: iso(-500) })],
      [
        'tm',
        task('tm', 'FAILED', {
          started_at: iso(-490),
          completed_at: iso(-300),
          result_summary: { staged_inputs: [{ task_id: 'tl', filename: 'left.md', path: 'left.md', bytes: 10 }] },
        }),
      ],
    ])
    const { container } = render(<Card w={w} tasks={tasks} />)
    fireEvent.click(within(container.querySelector<HTMLElement>('.wf-viewbar')!).getByText('Table'))
    const td = cell(container as HTMLElement, 'merge', 'inputs')
    const files = [...td.querySelectorAll('.wf-input')].map((el) => (el.textContent ?? '').split(' ←')[0])
    expect(files, 'the inputs follow the map, not depends_on').toEqual(['left.md', 'right.md'])
    // ONE LINE EACH: a break between the two, and no comma-joined run for the
    // cell's `nowrap` to cut the second one off.
    expect(td.querySelectorAll('br'), 'the two inputs share one clipped line').toHaveLength(1)
    expect(td.textContent).not.toContain(', ')
  })

  it('WF-6: orders a step’s inputs by depends_on whatever order the map arrives in', () => {
    // Every permutation of three keys, plus one key the list does not name --
    // which `validate_dag` should make impossible, and which still has to land
    // somewhere deterministic: last, by name.
    const orders: Array<Record<string, string>> = [
      { a: 'a.md', b: 'b.md', c: 'c.md', z: 'z.md' },
      { c: 'c.md', z: 'z.md', b: 'b.md', a: 'a.md' },
      { z: 'z.md', b: 'b.md', a: 'a.md', c: 'c.md' },
      { b: 'b.md', c: 'c.md', a: 'a.md', z: 'z.md' },
    ]
    for (const input_from of orders) {
      const steps = [step('a', []), step('b', []), step('c', []), step('m', ['c', 'a', 'b'], { input_from })]
      const declared = [...inputsByStep(steps, new Map()).get('m')!.declared.keys()]
      expect(declared, JSON.stringify(input_from)).toEqual(['c', 'a', 'b', 'z'])
    }
  })

  it('WF-18: says a finished step that never got an attempt never started, as the table does', async () => {
    const dropped = task('t_drop', 'CANCELLED', { created_at: iso(-480), completed_at: iso(-300) })
    const { container } = oneStepCard(dropped, [], 'table')
    const root = container as HTMLElement
    pick(root, 'work')
    const scrub = inspector(root).querySelector<HTMLElement>('[data-scrub="attempt"]')!
    await waitFor(() => expect(scrub.textContent).not.toContain('reading'))
    expect(scrub.textContent, 'an ended step is promised an attempt "yet"').not.toContain('no attempt yet')
    expect(scrub.textContent).toContain('never started')
    // The same word the table prints for the same step.
    expect(cell(root, 'work', 'ran').textContent).toBe('never started')
  })

  it('WF-18: still says "no attempt yet" for a step that is waiting for its first', async () => {
    const waiting = task('t_wait', 'READY', { created_at: iso(-120) })
    const { container } = oneStepCard(waiting, [], 'table')
    const root = container as HTMLElement
    pick(root, 'work')
    const scrub = inspector(root).querySelector<HTMLElement>('[data-scrub="attempt"]')!
    await waitFor(() => expect(scrub.textContent).toContain('no attempt yet'))
  })

  it('CH-5: draws the inspector’s run link in the link treatment, not the browser’s blue', async () => {
    const running = task('t_run', 'RUNNING', { created_at: iso(-600), started_at: iso(-300) })
    const { container } = oneStepCard(running, [])
    pick(container as HTMLElement, 'work')
    const i = inspector(container as HTMLElement)
    const run = i.querySelector('a.wf-inspect-run')!
    expect(run.getAttribute('href')).toBe('#work/task/t_run')
    expect(run.classList.contains('ctl-link'), 'the run link is unclassed and falls back to UA blue').toBe(true)
    // Let the attempt read the inspector started settle inside the test.
    const scrub = i.querySelector<HTMLElement>('[data-scrub="attempt"]')!
    await waitFor(() => expect(scrub.textContent).not.toContain('reading'))
  })

  it('WF-3: widens a data edge’s clearance with its stroke, from the layer beneath the strokes', () => {
    // The halos moved into one layer under every stroke. The sheet widens a
    // DATA edge's halo through `.wf-link.is-data .wf-edge-halo`, so the halo
    // layer has to keep the kind class or that clearance silently narrows.
    const { w, tasks } = edgeCard()
    const { container } = render(<Card w={w} tasks={tasks} />)
    const layer = container.querySelector('svg.wf-edges > .wf-edge-halos')
    expect(layer, 'the halos are not in a layer of their own').toBeTruthy()
    expect(layer!.querySelectorAll('.wf-edge-halo')).toHaveLength(3)
    expect(layer!.querySelectorAll('.wf-link.is-data .wf-edge-halo')).toHaveLength(2)
    expect(layer!.querySelectorAll('.wf-link.is-order .wf-edge-halo')).toHaveLength(1)
  })

  it('WF-22: shows the sample mark only while an open card draws step figures', async () => {
    // Twelve tasks is the attempt read's ceiling; this board asked for more,
    // so the mark has something to say.
    const { usage } = board()
    api.loadWorkflowUsage.mockResolvedValue({
      status: 'ok',
      data: { ...usage, notSampled: new Set(['t_beyond']), tasksRequested: 8 },
      fetchedAt: T0,
    } satisfies Result<WorkflowUsage>)
    await landed()
    const mark = () => document.querySelector('.wf-caveats .ctl-mark.is-partial')
    // The Table draws cost and tokens per step: the coverage of that read is
    // worth stating there. Waiting for it here is also what proves the read
    // has LANDED, so the absences below are not just an early render.
    chooseBoard('Table')
    await waitFor(() => expect(mark()?.textContent).toBe('6/8 sampled'))
    // Rows draws no step figure at all, and its spend column is summed from a
    // different source the sample says nothing about.
    chooseBoard('Rows')
    expect(mark(), 'a caveat about figures nobody can see').toBeNull()
    // The Timeline draws times from the task read, not the sample.
    chooseBoard('Timeline')
    expect(mark()).toBeNull()
    // The Graph, at the Figures tier these small workflows land on, draws
    // them on every node.
    chooseBoard('Graph')
    expect(mark()?.textContent).toBe('6/8 sampled')
  })
})
