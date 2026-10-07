// QA PASS G3 (2026-10-07, swarm.saga.xyz, code pointers against 9cf90837):
// one workflow's DETAIL page -- its Graph, Steps table, Timeline and step
// drawer. One case per finding; each fails on the code before its fix.
//
//   G3-02  a verdict-skipped step is a known $0, drawn as the skip hatch, and
//          says `skipped by verdict` on the Timeline
//   G3-03  a step that never had an attempt is a known $0, not a coverage gap
//   G3-05  the sample mark counts this workflow's tasks, not the board's
//   G3-10  the Steps table's State, Step and Why columns, and no cut absence
//   G3-11  a failure cause is never cut inside a parenthesis
//   G3-12  the Steps table's head is sticky (the Lanes axis is TimelineLanes.tsx,
//          another PR's territory, and is not held here)
//   G3-13  the Timeline has a key, a run is 3:1 on the card, a park is drawn
//   G3-19  the pending review verdict is one sentence
//   G3-25  the integrator is a task link, not a bare 8-character id
//   G3-26  one run, one duration: the drawer's `took` and the table's `ran`
//   G3-27  a linear chain's nodes use the column
//   G3-28  the page's own pick on arrival does not scroll the page
//   G3-32  a stacked row with no why prints no `Why` label
//   G3-33  the Tokens column sorts
//   G3-34  stacked rows clear their state edge; the skip hatch stays off the figures

import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, waitFor, within } from '@testing-library/react'

import type { Result } from '../fetch'
import type { StepUsage, WorkflowUsage } from '../api'
import type { AttemptRow, Task, TaskState, Workflow, WorkflowStep } from '../types'
import { painted, resolveColour, THEMES } from './marks'
import { contrast } from './spaceprobe'

const api = vi.hoisted(() => ({
  loadWorkflowBoard: vi.fn(),
  loadWorkflowUsage: vi.fn(),
  loadAttempts: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => {
  const real = await importOriginal<typeof import('../api')>()
  return { ...real, ...api }
})

const { WorkflowCard, integratorShort } = await import('../Workflows')
const { workflowSpend, dueSteps, layoutOf, nodeWidthAt, CHAIN_NODE_W } = await import('../dag')
const { attemptFacts, attemptPhase, failureCause } = await import('../stepviews')
const { TIMELINE_KEY } = await import('../WorkflowViews')
const { durationText } = await import('../measure')

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const T0 = Date.parse('2026-10-07T12:00:00.000Z')
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
    tenant_id: 'u-qa',
    state,
    runner_profile: 'claude-code',
    resource_class: 'standard',
    provider: 'anthropic',
    priority: 0,
    created_at: iso(-600),
    updated_at: iso(-20),
    started_at: null,
    completed_at: null,
    submitted_by: 'qa@saga.xyz',
    attempt_count: 1,
    max_attempts: 3,
    park_reason: null,
    blocked_by: null,
    workflow_id: 'wf_g3',
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

function workflow(steps: WorkflowStep[], state: Workflow['state'] = 'SUCCEEDED'): Workflow {
  return {
    workflow_id: 'wf_g3',
    state,
    tenant_id: 'u-qa',
    stored_state: state,
    state_source: 'derived',
    rollup: {
      state,
      complete: true,
      reason: 'all_steps_terminal',
      counts: {},
      unreadable_steps: [],
      unstarted_steps: [],
      steps_read: steps.length,
    },
    created_at: iso(-600),
    updated_at: iso(-20),
    submitted_by: 'qa@saga.xyz',
    priority: 0,
    on_step_failure: 'CONTINUE',
    cancel_requested: false,
    steps,
  }
}

function usageOf(n: Partial<StepUsage>): StepUsage {
  return {
    attempts: 1,
    attemptsWithCost: n.costUsd == null ? 0 : 1,
    attemptsWithTokens: n.inputTokens == null ? 0 : 1,
    checkpoints: 0,
    costUsd: null,
    inputTokens: null,
    outputTokens: null,
    cacheReadTokens: null,
    cacheCreationTokens: null,
    ...n,
  }
}

const INTEGRATOR = 'task_7d1e04fb18680'

/**
 * wf_dbc5242d's shape: implement -> review -> fix, integrate, succeeded, and
 * the fix step skipped by its verdict gate after 1m 15s of publishing.
 */
function skippedRun(): { w: Workflow; tasks: Map<string, Task>; usage: WorkflowUsage } {
  const tasks = new Map<string, Task>([
    [
      INTEGRATOR,
      task(INTEGRATOR, 'SUCCEEDED', {
        started_at: iso(-500),
        completed_at: iso(-314),
        dispatch: { strategy: 'integrate', carrier: 'branches', role: 'integrator', integrates: [] },
      } as Partial<Task>),
    ],
    ['t_rev', task('t_rev', 'SUCCEEDED', { started_at: iso(-300), completed_at: iso(-200) })],
    [
      't_fix',
      task('t_fix', 'SUCCEEDED', {
        started_at: iso(-100),
        completed_at: iso(-25),
        result_summary: { verdict_gate: { agent_ran: false, verdict: 'MERGE', task_id: 't_rev' } },
      }),
    ],
  ])
  const w = workflow([
    step('implement', [], { task_id: INTEGRATOR }),
    step('review', ['implement'], { task_id: 't_rev' }),
    step('fix', ['review'], { task_id: 't_fix' }),
  ])
  const usage: WorkflowUsage = {
    byTaskId: new Map([
      [INTEGRATOR, usageOf({ costUsd: 0.5, inputTokens: 900, outputTokens: 100 })],
      ['t_rev', usageOf({ costUsd: 0.25, inputTokens: 40_000, outputTokens: 2_000 })],
      ['t_fix', usageOf({})],
    ]),
    notSampled: new Set(),
    failed: new Map(),
    sampleLimit: 12,
    tasksRequested: 3,
  }
  return { w, tasks, usage }
}

/** wf_3d9dbdc0's shape: implement failed, review and fix cancelled before they ever started. */
function neverStartedRun(): { w: Workflow; tasks: Map<string, Task>; usage: WorkflowUsage } {
  const tasks = new Map<string, Task>([
    ['t_impl', task('t_impl', 'FAILED', { started_at: iso(-400), completed_at: iso(-214), last_error: 'expected outputs missing (not written: the agent’s diff was empty)' })],
    ['t_rev', task('t_rev', 'CANCELLED', { attempt_count: 0, completed_at: iso(-214) })],
    ['t_fix', task('t_fix', 'CANCELLED', { attempt_count: 0, completed_at: iso(-214) })],
  ])
  const w = workflow(
    [
      step('implement', [], { task_id: 't_impl' }),
      step('review', ['implement'], { task_id: 't_rev' }),
      step('fix', ['review'], { task_id: 't_fix' }),
    ],
    'FAILED',
  )
  const usage: WorkflowUsage = {
    byTaskId: new Map([
      ['t_impl', usageOf({ costUsd: 0.961, inputTokens: 1_000 })],
      ['t_rev', usageOf({ attempts: 0 })],
      ['t_fix', usageOf({ attempts: 0 })],
    ]),
    notSampled: new Set(),
    failed: new Map(),
    sampleLimit: 12,
    tasksRequested: 3,
  }
  return { w, tasks, usage }
}

const noAttempts = async (): Promise<Result<{ attempts: AttemptRow[] }>> => ({ status: 'empty', fetchedAt: T0 })

function card(
  w: Workflow,
  tasks: Map<string, Task>,
  usage: WorkflowUsage,
  view: 'graph' | 'timeline' | 'table',
  extra: Partial<Parameters<typeof WorkflowCard>[0]> = {},
) {
  vi.spyOn(Date, 'now').mockReturnValue(T0)
  return render(
    <WorkflowCard
      workflow={w}
      taskById={tasks}
      usage={{ kind: 'ready', usage }}
      openStages={{}}
      onToggleStage={() => {}}
      view={view}
      loadAttempts={noAttempts}
      reload={() => {}}
      {...extra}
    />,
  )
}

const totalOf = (root: HTMLElement) => root.querySelector<HTMLElement>('.wf-table-total .wf-spend')!

// ---------------------------------------------------------------------------
// G3-02 / G3-03: known $0s are not coverage gaps
// ---------------------------------------------------------------------------

describe('G3-02: a verdict-skipped step is a known $0 and is drawn as skipped', () => {
  it('leaves the skipped step out of the cost coverage, so a succeeded run is not a floor', () => {
    const { w, tasks, usage } = skippedRun()
    const spend = workflowSpend(w.steps, tasks, usage.byTaskId)
    expect(spend.skipped).toBe(1)
    expect(dueSteps(spend)).toBe(2)
    expect(spend.covered).toBe(2)
    const { container } = card(w, tasks, usage, 'table')
    const total = totalOf(container)
    expect(total.querySelector('.wf-spend-cov'), 'a 2/3 floor over a step whose agent never ran').toBeNull()
    expect(total.getAttribute('aria-label')).toContain('1 skipped by verdict spent nothing')
    // The control: a step that ran and did not report is still a gap.
    const gap = skippedRun()
    gap.tasks.set('t_fix', task('t_fix', 'SUCCEEDED', { started_at: iso(-100), completed_at: iso(-25) }))
    expect(dueSteps(workflowSpend(gap.w.steps, gap.tasks, gap.usage.byTaskId))).toBe(3)
  })

  it('prints `none` for the skipped step’s cost and tokens, never `not reported`', () => {
    const { w, tasks, usage } = skippedRun()
    const { container } = card(w, tasks, usage, 'table')
    const row = container.querySelector<HTMLElement>('.wf-table tr[data-step="fix"]')!
    expect(row.querySelector('td[data-col="cost"]')!.textContent).toBe('none')
    expect(row.querySelector('td[data-col="tokens"]')!.textContent).toBe('none')
  })

  it('draws the skipped run as the skip hatch and says `skipped by verdict` in its name', () => {
    const { w, tasks, usage } = skippedRun()
    const { container } = card(w, tasks, usage, 'timeline')
    const track = container.querySelector<HTMLElement>('.wf-tl-track[data-step="fix"]')!
    expect(track.querySelector('.wf-tl-span.is-ran.is-skipped')).not.toBeNull()
    expect(track.getAttribute('aria-label')).toContain('skipped by verdict')
    expect(track.getAttribute('aria-label')).not.toContain('ended succeeded')
    // The control: the review step ran, and is a run.
    const ran = container.querySelector<HTMLElement>('.wf-tl-track[data-step="review"]')!
    expect(ran.querySelector('.wf-tl-span.is-ran.is-skipped')).toBeNull()
    expect(ran.getAttribute('aria-label')).toContain('ended succeeded')
  })
})

describe('G3-03: a step that never had an attempt is a known $0', () => {
  it('counts only steps with an attempt in the coverage', () => {
    const { w, tasks, usage } = neverStartedRun()
    const spend = workflowSpend(w.steps, tasks, usage.byTaskId)
    expect(spend.noAttempt).toBe(2)
    expect(dueSteps(spend)).toBe(1)
    const { container } = card(w, tasks, usage, 'table')
    const total = totalOf(container)
    expect(total.querySelector('.wf-spend-cov'), '$0.961 1/3 over two steps that never started').toBeNull()
    expect(total.getAttribute('aria-label')).toContain('2 with no attempt spent nothing')
    expect(total.getAttribute('aria-label')).not.toContain('floor')
  })

  it('a step with no task yet is no attempt; an unread task stays a gap', () => {
    const tasks = new Map<string, Task>([['t_a', task('t_a', 'RUNNING', { started_at: iso(-60) })]])
    const steps = [step('a', [], { task_id: 't_a' }), step('b', ['a']), step('c', ['a'], { task_id: 't_lost' })]
    const spend = workflowSpend(steps, tasks, new Map([['t_a', usageOf({ costUsd: 0.1 })]]))
    expect(spend.noAttempt).toBe(1)
    expect(dueSteps(spend)).toBe(2)
  })
})

// ---------------------------------------------------------------------------
// G3-05: the sample mark is this workflow's
// ---------------------------------------------------------------------------

describe('G3-05: the sample mark counts this workflow’s tasks only', () => {
  const boardWide = (own: WorkflowUsage, missing: string[] = []): WorkflowUsage => {
    const notSampled = new Set(Array.from({ length: 284 }, (_, i) => `t_other_${i}`))
    for (const id of missing) {
      own.byTaskId.delete(id)
      notSampled.add(id)
    }
    return { ...own, notSampled, tasksRequested: 296 }
  }

  it('is silent when every one of this workflow’s steps was sampled', () => {
    const { w, tasks, usage } = skippedRun()
    const { container } = card(w, tasks, boardWide(usage), 'table')
    expect(container.querySelector('.wf-table-head .ctl-mark.is-partial'), '12/296 sampled on a 3-step page').toBeNull()
  })

  it('counts this workflow’s tasks when one of them is outside the ceiling', () => {
    const { w, tasks, usage } = skippedRun()
    const { container } = card(w, tasks, boardWide(usage, ['t_fix']), 'table')
    const mark = container.querySelector<HTMLElement>('.wf-table-head .ctl-mark.is-partial')!
    expect(mark.textContent).toBe('2/3 sampled')
    expect(mark.getAttribute('aria-label')).toContain('2 of this workflow’s 3 tasks')
  })
})

// ---------------------------------------------------------------------------
// G3-10 / G3-12 / G3-34: the Steps table's sheet
// ---------------------------------------------------------------------------

const COLS = ['step', 'state', 'why', 'runner', 'waited', 'ran', 'attempts', 'cost', 'tokens', 'inputs'] as const
const hosts: HTMLElement[] = []
afterEach(() => {
  for (const h of hosts.splice(0)) h.remove()
})

function sheetTable(skipped = false): HTMLElement {
  const host = document.createElement('div')
  host.innerHTML =
    '<div class="wf-card"><div class="wf-table-box"><div class="ctl-table wf-table is-scroll"><table><thead><tr>' +
    COLS.map((c) => `<th data-col="${c}">${c}</th>`).join('') +
    `</tr></thead><tbody><tr data-step="s" class="t-neu${skipped ? ' is-skipped' : ''}">` +
    COLS.map((c) => `<td data-col="${c}"${c === 'step' ? '' : ` data-label="${c}"`}>${c === 'why' ? '<span class="wf-why">x</span>' : 'x'}</td>`).join('') +
    '</tr></tbody></table></div></div></div>'
  document.body.appendChild(host)
  hosts.push(host)
  return host
}

const px = (v: string | null): number => (v === null ? NaN : Number.parseFloat(v))

describe('G3-10: the Steps table keeps the words that carry meaning', () => {
  it('gives State the list’s 136px and Step at least 96px', () => {
    const h = sheetTable()
    expect(px(painted(h.querySelector('th[data-col="state"]')!, 'width', { width: 1440 }))).toBe(136)
    expect(px(painted(h.querySelector('th[data-col="step"]')!, 'width', { width: 1440 }))).toBeGreaterThanOrEqual(96)
  })

  it('wraps Why onto two lines before cutting it', () => {
    const h = sheetTable()
    expect(painted(h.querySelector('td[data-col="why"]')!, 'white-space', { width: 1440 })).toBe('normal')
    expect(painted(h.querySelector('.wf-why')!, '-webkit-line-clamp', { width: 1440 })).toBe('2')
  })

  it('never prints `no attempt yet` in the Cost or Tokens cell of a step waiting for its first attempt', () => {
    const tasks = new Map<string, Task>([['t_a', task('t_a', 'READY', { attempt_count: 0 })]])
    const w = workflow([step('a', [], { task_id: 't_a' })], 'RUNNING')
    const usage: WorkflowUsage = { byTaskId: new Map([['t_a', usageOf({ attempts: 0 })]]), notSampled: new Set(), failed: new Map(), sampleLimit: 12, tasksRequested: 1 }
    const { container } = card(w, tasks, usage, 'table')
    const row = container.querySelector<HTMLElement>('.wf-table tr[data-step="a"]')!
    expect(row.querySelector('td[data-col="cost"]')!.textContent).toBe('none yet')
    expect(row.querySelector('td[data-col="tokens"]')!.textContent).toBe('none yet')
  })
})

describe('G3-12: the Steps table’s head stays in view', () => {
  it('sticks the head to the top, under the drawer’s band, inside a wrapper that is no scroll container', () => {
    const h = sheetTable()
    const thead = h.querySelector('thead')!
    expect(painted(thead, 'position', { width: 1440 })).toBe('sticky')
    expect(painted(thead, 'top', { width: 1440 })).toBe('0')
    expect(Number(painted(thead, 'z-index', { width: 1440 }))).toBeLessThan(3)
    // `auto` (or `hidden`, `scroll`) makes the wrapper the head's scroll
    // container, where a sticky head never sticks.
    expect(painted(h.querySelector('.wf-table')!, ['overflow-x', 'overflow'], { width: 1440 })).toBe('clip')
  })
})

describe('G3-34: stacked step rows clear their state edge', () => {
  it('pads the first label past the 4px edge', () => {
    const h = sheetTable()
    const pad = painted(h.querySelector('tr[data-step]')!, ['padding-left', 'padding'], { width: 390, container: 360 })
    const left = pad === null ? NaN : px(pad.split(/\s+/).length === 4 ? pad.split(/\s+/)[3]! : pad)
    expect(left).toBeGreaterThanOrEqual(12)
  })

  it('keeps the skip hatch off the figures: the cells draw none, the row a strip by the edge', () => {
    const h = sheetTable(true)
    const env = { width: 390, container: 360 }
    expect(painted(h.querySelector('td[data-col="cost"]')!, ['background', 'background-image'], env)).toBe('none')
    expect(painted(h.querySelector('tr[data-step]')!, 'background-size', env)).toMatch(/^10px /)
  })
})

// ---------------------------------------------------------------------------
// G3-11: the cause is not cut inside a parenthesis
// ---------------------------------------------------------------------------

describe('G3-11: a failure cause never opens a parenthesis it does not close', () => {
  it('splits at the first `: ` outside any parenthesis and drops the parenthetical', () => {
    expect(failureCause('expected outputs missing (not written: the agent’s diff was empty, nothing to publish)')).toBe(
      'expected outputs missing',
    )
    expect(failureCause('expected outputs missing (not written')).toBe('expected outputs missing')
    // The controls: the existing groupings hold.
    expect(failureCause('input collision: `plan.md` is staged by both a and b')).toBe('input collision')
    expect(failureCause('quota (5h window): exhausted')).toBe('quota')
  })
})

// ---------------------------------------------------------------------------
// G3-13: the Timeline says what its bars are
// ---------------------------------------------------------------------------

describe('G3-13: the Timeline draws a key, a visible run and a park', () => {
  it('prints a one-line key above the axis: on parents, waited, parked, ran, skipped', () => {
    const { w, tasks, usage } = skippedRun()
    const { container } = card(w, tasks, usage, 'timeline')
    const key = container.querySelector<HTMLElement>('.wf-timeline > .wf-tl-key')!
    expect(key).not.toBeNull()
    const words = within(key).getAllByRole('listitem').map((li) => li.textContent)
    expect(words).toEqual(TIMELINE_KEY.map((k) => k.word))
    expect(words).toEqual(expect.arrayContaining(['waited', 'ran', 'on parents', 'parked']))
    // Above the axis: the key comes before the scale in the grid.
    expect(key.compareDocumentPosition(container.querySelector('.wf-tl-scale')!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('gives a finished run a core at least 3:1 against the card, in both themes', () => {
    const { w, tasks, usage } = skippedRun()
    const { container } = card(w, tasks, usage, 'timeline')
    const run = container.querySelector<HTMLElement>('.wf-tl-track[data-step="review"] .wf-tl-span.is-ran')!
    for (const theme of THEMES) {
      const image = painted(run, 'background-image', { width: 1440, theme }) ?? ''
      const m = /linear-gradient\(\s*([^,]+?)\s*,/.exec(image)
      expect(m, `${theme}: a finished run has no solid core`).not.toBeNull()
      const core = resolveColour(m![1]!, theme)
      for (const card of ['var(--surface)', 'var(--surface-2)']) {
        expect(contrast(core, resolveColour(card, theme)), `${theme}: core on ${card}`).toBeGreaterThanOrEqual(3)
      }
    }
  })

  it('draws a step parked now in the parked hatch', () => {
    const tasks = new Map<string, Task>([
      ['t_a', task('t_a', 'SUCCEEDED', { started_at: iso(-590), completed_at: iso(-500) })],
      ['t_b', task('t_b', 'PARKED', { started_at: iso(-480), park_reason: 'quota' } as Partial<Task>)],
    ])
    const w = workflow([step('a', [], { task_id: 't_a' }), step('b', ['a'], { task_id: 't_b' })], 'RUNNING')
    const usage: WorkflowUsage = { byTaskId: new Map(), notSampled: new Set(), failed: new Map(), sampleLimit: 12, tasksRequested: 2 }
    const { container } = card(w, tasks, usage, 'timeline')
    const parked = container.querySelector<HTMLElement>('.wf-tl-track[data-step="b"] .wf-tl-span.is-waiting.is-parked')
    expect(parked, 'a parked step drew an outline only').not.toBeNull()
    for (const theme of THEMES) {
      expect(painted(parked!, ['background', 'background-image'], { width: 1440, theme })).toMatch(/repeating-linear-gradient/)
    }
  })
})

// ---------------------------------------------------------------------------
// G3-19, G3-25, G3-28: the page's head and drawer
// ---------------------------------------------------------------------------

function pendingReview(): { w: Workflow; tasks: Map<string, Task> } {
  const tasks = new Map<string, Task>([
    ['t_impl', task('t_impl', 'SUCCEEDED', { started_at: iso(-500), completed_at: iso(-400) })],
    ['t_rev', task('t_rev', 'SUCCEEDED', { started_at: iso(-300), completed_at: iso(-200) })],
    [
      't_fix',
      task('t_fix', 'READY', {
        attempt_count: 0,
        dispatch: { strategy: 'collect', carrier: 'artifact', role: null, integrates: [], verdict_gate: { task_id: 't_rev' } },
      } as unknown as Partial<Task>),
    ],
  ])
  const w = workflow(
    [
      step('implement', [], { task_id: 't_impl' }),
      step('review', ['implement'], { task_id: 't_rev' }),
      step('fix', ['review'], { task_id: 't_fix' }),
    ],
    'RUNNING',
  )
  return { w, tasks }
}

const EMPTY_USAGE: WorkflowUsage = { byTaskId: new Map(), notSampled: new Set(), failed: new Map(), sampleLimit: 12, tasksRequested: 0 }

describe('G3-19: the pending review verdict is one sentence', () => {
  it('keeps `not read yet` inline and starts no line with a colon', async () => {
    const { w, tasks } = pendingReview()
    const { container } = card(w, tasks, EMPTY_USAGE, 'graph', { page: true, picked: 'review', onPick: () => {} })
    const p = await waitFor(() => {
      const el = container.querySelector<HTMLElement>('.wf-verdict.is-pending')
      expect(el).not.toBeNull()
      return el!
    })
    const text = (p.textContent ?? '').replace(/\s+/g, ' ')
    expect(text).toContain('not read yet — no step that gates on this review')
    expect(text).not.toMatch(/not read yet\s*:/)
    // A column flex box made each text run a line of its own.
    expect(painted(p, 'display', { width: 1440 })).toBe('block')
  })
})

describe('G3-25: the integrator is a task, linked to its agent', () => {
  it('names the integrator task and links it, rather than an 8-character `via`', () => {
    const { w, tasks, usage } = skippedRun()
    const { container } = card(w, tasks, usage, 'graph')
    const fact = [...container.querySelectorAll<HTMLElement>('.wf-dispatch .ctl-fact')].find((li) => li.querySelector('b')?.textContent === 'integrator')
    expect(fact, 'no integrator fact').toBeDefined()
    const link = fact!.querySelector<HTMLAnchorElement>('a')!
    expect(link.getAttribute('href')).toBe(`#work/task/${INTEGRATOR}`)
    expect(link.textContent).toBe('task_…18680')
    expect(link.getAttribute('title')).toBe(INTEGRATOR)
    expect([...container.querySelectorAll('.wf-dispatch b')].map((b) => b.textContent)).not.toContain('via')
    expect(integratorShort('abc')).toBe('abc')
  })
})

describe('G3-28: the page’s own pick on arrival does not scroll the page', () => {
  it('brings the card into view only when a reader picked the step', async () => {
    const scroll = vi.fn()
    Object.defineProperty(Element.prototype, 'scrollIntoView', { value: scroll, configurable: true, writable: true })
    const { w, tasks } = pendingReview()
    const onPick = vi.fn()
    const props = { page: true, onPick } as const
    const r = card(w, tasks, EMPTY_USAGE, 'graph', { ...props, picked: 'implement' })
    await waitFor(() => expect(r.container.querySelector('.wf-panel')).not.toBeNull())
    expect(scroll, 'the arrival pick scrolled the page down to the card').not.toHaveBeenCalled()
    // The control: a reader's pick still brings the card to them.
    const node = r.container.querySelector<HTMLButtonElement>('button.node[data-step="review"], button.wf-pick[data-step="review"]')!
    act(() => {
      fireEvent.click(node)
    })
    expect(onPick).toHaveBeenCalledWith('wf_g3', 'review')
    r.rerender(
      <WorkflowCard
        workflow={w}
        taskById={tasks}
        usage={{ kind: 'ready', usage: EMPTY_USAGE }}
        openStages={{}}
        onToggleStage={() => {}}
        view="graph"
        loadAttempts={noAttempts}
        reload={() => {}}
        {...props}
        picked="review"
      />,
    )
    await waitFor(() => expect(scroll).toHaveBeenCalled())
  })
})

// ---------------------------------------------------------------------------
// G3-26: one run, one duration
// ---------------------------------------------------------------------------

describe('G3-26: the drawer’s `took` and the table’s `ran` print one run once', () => {
  const attempt = (startS: number, endS: number): AttemptRow =>
    ({
      attempt_id: 'att_1',
      generation: 1,
      started_at: iso(startS),
      completed_at: iso(endS),
      exit_code: 1,
      cost_usd: null,
      input_tokens: null,
      output_tokens: null,
      cache_read_input_tokens: null,
      cache_creation_input_tokens: null,
      checkpoints: [],
      error: null,
    }) as unknown as AttemptRow

  it('prints the task’s figure when the attempt is the same run a second apart', () => {
    // The task: 3m 6.4s. The attempt document: 3m 7.1s.
    const taskRanMs = 186_400
    const facts = attemptFacts(attempt(-187.1, 0), attemptPhase('FAILED', true), T0, null, taskRanMs)
    const took = facts.find((f) => f.key === 'took')!
    expect(took.cell.text).toBe(durationText(taskRanMs))
    expect(took.cell.text).toBe('3m 6s')
  })

  it('keeps the attempt’s own figure when it is a different run', () => {
    const facts = attemptFacts(attempt(-60, 0), attemptPhase('FAILED', true), T0, null, 186_400)
    expect(facts.find((f) => f.key === 'took')!.cell.text).toBe('1m 0s')
  })
})

// ---------------------------------------------------------------------------
// G3-27: a chain uses the column
// ---------------------------------------------------------------------------

describe('G3-27: a linear chain’s nodes use the room beside them', () => {
  it('widens a chain’s nodes up to CHAIN_NODE_W, and leaves a fan alone', () => {
    const chain = [step('implement', []), step('review', ['implement']), step('fix', ['review'], { input_from: { review: 'swarm-work.patch' } } as Partial<WorkflowStep>)]
    const l = layoutOf(chain)
    expect(l.nodeW).toBeGreaterThan(nodeWidthAt('figures', chain))
    expect(l.nodeW).toBeLessThanOrEqual(CHAIN_NODE_W)
    const fan = [step('a', []), step('b', ['a']), step('c', ['a'])]
    expect(layoutOf(fan).nodeW).toBe(nodeWidthAt('figures', fan))
  })
})

// ---------------------------------------------------------------------------
// G3-32, G3-33: the table's rows
// ---------------------------------------------------------------------------

describe('G3-32: a row with no why carries no `Why` label', () => {
  it('labels only the why cells that hold one', () => {
    // The review FAILED (it has a why); implement and fix succeeded (they do not).
    const { w, tasks, usage } = skippedRun()
    tasks.set('t_rev', task('t_rev', 'FAILED', { started_at: iso(-300), completed_at: iso(-200), last_error: 'review crashed: exit 1' }))
    const { container } = card(w, tasks, usage, 'table')
    const failed = container.querySelector<HTMLElement>('.wf-table tr[data-step="review"] td[data-col="why"]')!
    expect(failed.textContent).not.toBe('')
    expect(failed.getAttribute('data-label')).toBe('Why')
    const rows = [...container.querySelectorAll<HTMLElement>('.wf-table tr[data-step] td[data-col="why"]')]
    const empty = rows.filter((td) => td.textContent === '')
    expect(empty.length, 'the fixture has no row without a why').toBe(2)
    for (const td of empty) expect(td.hasAttribute('data-label'), 'an empty why cell printed its label').toBe(false)
  })
})

describe('G3-33: the Tokens column sorts, as Cost does', () => {
  it('has a sort control and orders the rows by the tokens they print', () => {
    const { w, tasks, usage } = skippedRun()
    const { container } = card(w, tasks, usage, 'table')
    const head = container.querySelector<HTMLElement>('.wf-table th[data-col="tokens"]')!
    const button = head.querySelector('button')
    expect(button, 'Tokens has no sort control').not.toBeNull()
    fireEvent.click(button!)
    expect(head.getAttribute('aria-sort')).toBe('ascending')
    const order = () => [...container.querySelectorAll<HTMLElement>('.wf-table tbody tr[data-step]')].map((r) => r.dataset.step)
    // fix spent none (0), implement 1,000, review 42,000.
    expect(order()).toEqual(['fix', 'implement', 'review'])
    fireEvent.click(button!)
    expect(order()).toEqual(['review', 'implement', 'fix'])
  })
})
