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
 *
 * And the re-review's NOT_YET and minors (lane VQ-WORKFLOWS-FIX2): the name
 * keeps 200px at 1440 with the runners moved under it, the graph card's stop
 * control has its own place, the state drift is said in words, the Table's
 * step name reads whole, and the id is drawn once, behind Copy id.
 *
 * And VQ-WORKFLOWS-FIX3's two clean-ups: the page's same-step row does not
 * render the id at all (it was rendered and hidden by CSS), and the Step
 * column is sized at the measured mono advance, not 0.6em.
 */
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useState } from 'react'

import type { Result } from '../fetch'
import type { WorkflowBoard, WorkflowUsage } from '../api'
import type { Task, TaskState, Workflow, WorkflowStep } from '../types'
import { painted } from './marks'
import { MONO_ADVANCE_EM, monoW } from '../dag'
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

const { WorkflowsScreen, WorkflowCard } = await import('../Workflows')
const { stepColumnWidth } = await import('../WorkflowViews')
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

const row = (id: string) => document.querySelector<HTMLElement>(`.wfg-table tr[data-workflow="${id}"]`)!

describe('VQ-013: the list is compact rows under one note', () => {
  // MUTATION: draw the two notes as two CountNotes again.
  it('says what the counts are of and how many rows are read in one note', async () => {
    // Built at runtime: an opaque cursor, not a credential.
    serve([chain, older], chainTasks, ['page', 'two'].join('-'))
    render(<WorkflowsScreen />)
    await waitFor(() => expect(row('wf_older')).toBeTruthy())
    const notes = document.querySelectorAll('.wfg .c-count-note')
    expect(notes).toHaveLength(1)
    expect(notes[0]!.textContent).toBe(
      'Counts and order are of the 2 newest workflows; older ones are on the server and not read yet · ' +
        "1 of 2 rows have their steps read; the list read did not serve the rest's, which each one's page reads",
    )
    // No margin of its own on top of the list's gap.
    expect(painted(notes[0]!, 'margin', { width: 1440 })).toBe('0')
  })

  // THE ROW IS ONE LINE (the redesign, frame graph-workflows-rest): the name
  // alone in its cell; the id, the runners and the current step moved into
  // the inspector's summary the row opens.
  // MUTATION: draw the id, the mix or the why line in the row again, or drop
  // any of them from `WorkflowSummary`.
  it('draws the name alone on the row, and the id, its runners and the current step in the summary the row opens', async () => {
    render(<WorkflowsScreen />)
    await waitFor(() => expect(row('wf_chain')).toBeTruthy())
    const name = row('wf_chain').querySelector<HTMLElement>('td[data-col="workflow"]')!
    expect([...name.children].map((c) => c.tagName)).toEqual(['A'])
    expect(name.querySelector(':scope > a')!.textContent).toBe('JOIN-UI-PERSON')
    expect(name.querySelector('.id, .wfl-why, .wf-mix')).toBeNull()
    fireEvent.click(row('wf_chain').querySelector<HTMLElement>('[data-col="state"]')!)
    const sum = document.querySelector<HTMLElement>('aside[aria-label="Workflow inspector"] .wfg-sum')!
    expect(sum, 'no summary').toBeTruthy()
    expect(sum.querySelector(':scope > .id')!.textContent).toBe('wf_chain')
    expect(sum.querySelector('.wf-mix')!.getAttribute('title')).toBe('Runner profiles: claude-code ×3')
    const why = sum.querySelector<HTMLElement>('.wfg-sum-why > .wfl-why')!
    expect(why.textContent).toBe('build running')
    expect(why.title).toBe('build running')
    // The summary has the room: the step reads whole, wrapped, never cut.
    expect(painted(why, 'white-space', { width: 1440 }) ?? 'normal').toBe('normal')
  })

  // 'keeps room on the name’s line for one whole runner chip' went with the
  // runners on the row: the redesign (frame graph-workflows-rest) draws them
  // in the inspector's summary, tested above.
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
    // Found by selector, then by role inside it: a role query over the whole
    // page walks every node's accessible name and ran past findBy's 1s.
    const group = await waitFor(
      () => {
        const g = document.querySelector<HTMLElement>('.wf-inspect-acts[role="group"]')
        expect(g).toBeTruthy()
        return g!
      },
      { timeout: 5000 },
    )
    expect(group.getAttribute('aria-label')).toBe('Stop step')
    // `Stop`, and the head's `Stop workflow`: one verb (visual QA pass 1 overview-agents-83, #2596).
    const stop = within(group).getByRole('button', { name: 'Stop' })
    const head = document.querySelector<HTMLElement>('.wfp:not(.is-loading) .wfp-head')!
    const cancel = within(head).getByRole('button', { name: 'Stop workflow' })
    // The head's Stop workflow: the danger outline at the default size.
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

// VQ-013 RE-REVIEW (the name and the runners both read at 1440, by
// `mixGoesUnder` measuring the name's line against LINE_MIX_PX) went with the
// runners on the row: the redesign (frame graph-workflows-rest) draws the row
// as one line and the runners in the inspector's summary.

describe('VQ-WORKFLOWS re-review minors', () => {
  // MUTATION: drop the slot's grid, which leaves the stop control anchored to
  // the slot's foot while the card's rows run on under it (it covered
  // scan-terraform's `↑ plan (plan.md)` at 1440).
  it('gives the graph card’s stop control a place in the card’s own box, below everything the card draws', async () => {
    render(<Routed initial="wf=wf_chain" />)
    const node = await waitFor(() => {
      const n = document.querySelector<HTMLElement>('.wf-canvas .node[data-step="build"]')
      expect(n).toBeTruthy()
      return n!
    })
    const slot = node.parentElement!
    const stop = slot.querySelector<HTMLElement>(':scope > .node-stop')!
    expect(stop, 'a running step draws no stop control').toBeTruthy()
    const env = { width: 1440 }
    // One cell: the card fills it and grows with what it draws; the stop sits
    // at the cell's foot, so in the card's own bottom padding whatever its height.
    expect(painted(slot, 'display', env)).toBe('grid')
    expect(painted(node, 'position', env)).toBe('relative')
    expect(painted(node, 'grid-area', env)).toBe('1 / 1')
    expect(painted(stop, 'position', env)).toBe('relative')
    expect(painted(stop, 'grid-area', env)).toBe('1 / 1')
    expect(painted(stop, 'align-self', env)).toBe('end')
    // The strip's inset is unchanged: 10px up, the text column's left and right.
    expect(painted(stop, 'bottom', env)).toBe('10px')
    expect(painted(stop, 'left', env)).toBe('15px')
    expect(painted(stop, 'margin-right', env)).toBe('27px')
  })

  // MUTATION: leave `button.node`'s `height: 100%` in force on the canvas.
  // The card is then the slot's inline height (`layoutOf`'s 107px for
  // scan-terraform), its rows run on past its padding, and the stop at the
  // cell's foot covers `(plan.md)` -- what the re-review rendered at 1440.
  // MUTATION: give the slot its inline `height` again rather than a
  // `min-height`. A grid with a fixed height holds its one row, and so the
  // card, to `layoutOf`'s figure in the browser, and the stop covered
  // `(plan.md)` at 1440 in both themes (VQ-WORKFLOWS-FIX3 review).
  it('lets a card taller than its slot carry the stop below its last line, not over it', async () => {
    render(<Routed initial="wf=wf_chain" />)
    const node = await waitFor(() => {
      const n = document.querySelector<HTMLElement>('.wf-canvas .node[data-step="build"]')
      expect(n).toBeTruthy()
      return n!
    })
    const slot = node.parentElement!
    const stop = slot.querySelector<HTMLElement>(':scope > .node-stop')!
    const env = { width: 1440 }
    // The layout's height is a floor, never a fixed height.
    const fixed = slot.style.height !== ''
    const slotH = Number.parseFloat(slot.style.minHeight || slot.style.height)
    expect(slotH, 'the slot has no inline height').toBeGreaterThan(0)
    // jsdom lays nothing out, so this is the box the cascade gives, counted:
    // the one-cell grid's row is as tall as the card, and the card is either
    // a percentage of the slot or as tall as what it draws (never less than
    // the slot, as it stretches).
    const height = painted(node, 'height', env) ?? 'auto'
    const alignSelf = painted(node, 'align-self', env) ?? 'normal'
    const cardH = (content: number) => {
      const pct = /^([\d.]+)%$/.exec(height)
      if (pct !== null) return (slotH * Number(pct[1])) / 100
      expect(height).toBe('auto')
      expect(['normal', 'stretch']).toContain(alignSelf)
      // A slot of fixed height holds its one-cell grid to that height, and
      // the card stretched to the cell with it, whatever the card draws.
      if (fixed) return slotH
      return Math.max(slotH, content)
    }
    const pad = (painted(node, 'padding', env) ?? '').split(/\s+/).map((v) => Number.parseFloat(v))
    const strip = pad[2]!
    expect(strip, 'the running card reserves no strip at its foot').toBe(42)
    const lift = Number.parseFloat(painted(stop, 'bottom', env) ?? '0')
    // 28px: the rendered stop button the 42px strip was measured from (styles.css `.node`).
    const STOP_H = 28
    // Cards that fit, and cards whose rows run 1-60px past what the layout counted.
    for (let over = -20; over <= 60; over++) {
      const content = slotH + over
      const card = cardH(content)
      const lastLine = content - strip - 1
      const stopTop = card - lift - STOP_H
      expect(stopTop, `a card ${over}px over its slot: the stop covers its last line`).toBeGreaterThanOrEqual(lastLine)
    }
  })

  // MUTATION: print `drift.stored` and `drift.derived` as they come (QUEUED, RUNNING).
  it('says the state drift in words, not capitals, on the board card and never on the page', async () => {
    const drifted: Workflow = {
      ...chain,
      drift: { stored: 'QUEUED', derived: 'RUNNING', agrees: false, reason: 'test', steps_read: 3, unreadable_steps: [], repaired: false },
    }
    serve([drifted], chainTasks)
    const routed = render(<Routed initial="wf=wf_chain" />)
    await waitFor(() => expect(document.querySelector('.wfp:not(.is-loading) .wfp-head')).toBeTruthy())
    // MUTATION: draw `StateDrift` on the page again: `stored as queued · steps
    // running` beside a head that says running (visual QA pass 1 overview-agents-81, #2596).
    expect(document.querySelector('.wf-drift'), 'the page prints the stored state').toBeNull()
    routed.unmount()
    render(
      <WorkflowCard
        workflow={drifted}
        taskById={chainTasks}
        usage={{ kind: 'ready', usage: null }}
        openStages={{}}
        onToggleStage={() => {}}
        reload={() => {}}
      />,
    )
    const drift = await waitFor(() => {
      const d = document.querySelector<HTMLElement>('.wf-drift')
      expect(d).toBeTruthy()
      return d!
    })
    const facts = [...drift.querySelectorAll('.ctl-fact')].map((f) => (f.textContent ?? '').replace(/\s+/g, ' ').trim())
    expect(facts).toEqual(['stored as queued', 'steps running'])
    expect(drift.textContent).not.toMatch(/[A-Z]{3,}/)
    expect(drift.getAttribute('aria-label')).toBe('Stored as queued; the steps say running.')
  })

  // MUTATION: put the Step cell's padding back to 6px a side (or the right
  // side's 2px back on it): `scan-terraform` (14) drew as `scan-terraf…`.
  it('lets the Table’s longest step name read whole in the column sized for it', async () => {
    const terraform = workflow('wf_tf', 'RUNNING', [step('plan', [], { task_id: 'task_plan' }), step('scan-terraform', ['plan'], { task_id: 'task_build' })], 600)
    serve([terraform], chainTasks)
    render(<Routed initial="wf=wf_tf&tab=table" />)
    const cell = await waitFor(() => {
      const c = document.querySelector<HTMLElement>('.wf-table tr[data-step="scan-terraform"] td[data-col="step"]')
      expect(c).toBeTruthy()
      return c!
    })
    const pad = (painted(cell, 'padding', { width: 1440 }) ?? '').split(/\s+/).map((v) => Number.parseFloat(v))
    expect(pad).toHaveLength(4)
    const [, right, , left] = pad as [number, number, number, number]
    // `stepColumnWidth` (WorkflowViews.tsx): n x the mono advance x 14px +
    // 22px. Drawn: the name at the face's measured advance, the cell's
    // padding, and the pick's 4px a side and 1px border (`.wf-table .wf-pick`).
    for (let n = 1; n <= 60; n++) {
      const column = n * MONO_ADVANCE_EM * 14 + 22
      const drawn = n * MONO_ADVANCE_EM * 14 + left + right + 2 * 4 + 2
      expect(drawn, `a ${n}-character name is cut`).toBeLessThanOrEqual(column)
    }
  })

  // MUTATION: drop the selected name's wrap. At 1440 with the nav open the
  // column is held to its container cap (`96px + 100cqi - 980px`, about
  // 121px), not to the name, and the selected `scan-terraform` drew as
  // `scan-terraf…` (re-review: scrollWidth 118, clientWidth 105).
  it('lets the selected step’s name read whole at 1440 with the nav open, where the column is at its cap', async () => {
    const terraform = workflow('wf_tf', 'RUNNING', [step('plan', [], { task_id: 'task_plan' }), step('scan-terraform', ['plan'], { task_id: 'task_build' })], 600)
    serve([terraform], chainTasks)
    render(<Routed initial="wf=wf_tf&tab=table" />)
    const pickOf = (id: string) => document.querySelector<HTMLElement>(`.wf-table tr[data-step="${id}"] td[data-col="step"] > .wf-pick`)
    fireEvent.click(await waitFor(() => {
      const p = pickOf('scan-terraform')
      expect(p).toBeTruthy()
      return p!
    }))
    const pick = await waitFor(() => {
      const p = pickOf('scan-terraform')
      expect(p?.getAttribute('aria-pressed')).toBe('true')
      return p!
    })
    // The Steps box at 1440 with the nav open (the re-review's Step cell: 123px).
    const env = { width: 1440, container: 1005 }
    const cell = pick.parentElement!
    const [, right, , left] = (painted(cell, 'padding', env) ?? '').split(/\s+/).map((v) => Number.parseFloat(v)) as [number, number, number, number]
    const name = 'scan-terraform'
    const column = Math.min(name.length * MONO_ADVANCE_EM * 14 + 22, 96 + env.container - 980)
    const drawn = (chars: number) => chars * MONO_ADVANCE_EM * 14 + left + right + 2 * 4 + 2
    // The case is real: on one line the name does not fit the capped column.
    expect(drawn(name.length)).toBeGreaterThan(column)
    const id = pick.querySelector<HTMLElement>(':scope > .wf-pick-id')!
    expect(painted(pick, 'white-space', env)).toBe('normal')
    expect(painted(id, 'white-space', env)).toBe('normal')
    expect(painted(id, 'overflow-wrap', env)).toBe('anywhere')
    // Wrapped at its hyphen, each part fits the column.
    for (const part of ['scan-', 'terraform']) expect(drawn(part.length), part).toBeLessThanOrEqual(column)
    // The control: a row that is not selected keeps D12's one line, cut.
    const other = pickOf('plan')!
    expect(other.getAttribute('aria-pressed')).toBe('false')
    expect(painted(other.querySelector(':scope > .wf-pick-id')!, 'white-space', env)).toBe('nowrap')
  })

  // MUTATION: draw the id as text in the head's chips again, or leave the scrubber's copy of it.
  it('draws the workflow id once, behind its Copy id control', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } })
    render(<Routed initial="wf=wf_chain" />)
    const head = await waitFor(() => {
      const h = document.querySelector<HTMLElement>('.wfp:not(.is-loading) .wfp-head')
      expect(h).toBeTruthy()
      return h!
    })
    expect(head.querySelector('.id'), 'the head prints the id as text').toBeNull()
    expect(head.textContent).not.toContain('wf_chain')
    const copy = within(head).getByRole('button', { name: 'Copy workflow id wf_chain' })
    expect(copy.textContent).toBe('Copy id')
    expect(copy.getAttribute('title')).toBe('wf_chain')
    fireEvent.click(copy)
    expect(writeText).toHaveBeenCalledWith('wf_chain')
    await waitFor(() => expect(copy.textContent).toBe('Id copied'))
    // The inspector's same-step row does not render it again on the page:
    // not drawn and hidden, absent.
    const scrub = await waitFor(() => {
      const el = document.querySelector<HTMLElement>('.wfp-screen .wf-scrub[data-scrub="workflow"]')
      expect(el).toBeTruthy()
      return el!
    })
    expect(scrub.querySelector('.id'), 'the same-step row renders the id').toBeNull()
    expect(scrub.textContent).not.toContain('wf_chain')
  })

  // MUTATION: drop the id from every same-step row, not only the page's, or
  // keep the stylesheet's `display: none` on it.
  it('keeps the id on a card that is not a workflow’s page, where no head carries it, and hides it with no rule', () => {
    render(
      <div className="wfp-screen">
        <WorkflowCard
          workflow={chain}
          taskById={chainTasks}
          usage={{ kind: 'ready', usage: null }}
          openStages={{}}
          onToggleStage={() => {}}
          picked="build"
          onPick={() => {}}
          reload={() => {}}
        />
      </div>,
    )
    const id = document.querySelector<HTMLElement>('.wf-scrub[data-scrub="workflow"] > .id')
    expect(id, 'the card’s same-step row lost its id').toBeTruthy()
    expect(id!.textContent).toBe('wf_chain')
    for (const width of [1440, 390]) expect(painted(id!, 'display', { width }), `hidden at ${width}`).not.toBe('none')
  })
})

describe('the Step column at the measured mono advance', () => {
  // MUTATION: size the column at 0.6em a character again. The face's advance
  // is 0.602em, so past a few characters the rounding cut the name's tail.
  it('asks for every name length at least the name’s measured advance plus its 22px of chrome', () => {
    for (let n = 1; n <= 60; n++) {
      const width = String(stepColumnWidth([{ step: { step_id: 'x'.repeat(n) } }]).width)
      const asked = /^min\(calc\((\d+) \* ([\d.]+) \* var\(--t-body\) \+ 22px\),/.exec(width)
      expect(asked, width).toBeTruthy()
      const column = Number(asked![1]) * Number(asked![2]) * 14 + 22
      expect(monoW(n, 14) + 22, `a ${n}-character name is cut`).toBeLessThanOrEqual(column)
    }
  })
})
