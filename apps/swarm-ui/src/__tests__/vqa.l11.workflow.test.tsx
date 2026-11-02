// VISUAL QA LANE L11 (#1038): THE WORKFLOW PAGE AND ITS GRAPH.
//
// Twelve rendering findings from the merged visual QA report, each pinned here
// by the property that was wrong rather than by the markup that fixed it. The
// browser renders that found them are in the PR; what jsdom can hold is held
// here: a role and where focus is, the value the cascade gives a property at a
// width, the width a node is laid out at, a link's text, a skeleton's line.
//
//   V001  the stop confirmation on a graph node opens as a modal over the page
//   V034  the canvas wrapper is bounded by the screen, so its scrollbar is on it
//   V035  a stacked step card draws no right-edge fade and its why line wraps
//   V097  the workflow page's body has one frame, not a frame inside a frame
//   V098  the Step column grows to the longest name, out of Why's slack
//   V099  the Table's inspector opens under the row when the split is too narrow
//   V100  the Timeline's likewise; the × closes the head's first line
//   V101  the held State cell keeps a failed row's tint; the corner is the head's
//   V102  a fan-in's `(file)` units fit the node; the integrator id is whole
//   V103  the page skeleton draws the count note's line
//   V110  the empty list says what a workflow is and links to Submit › workflow
//   V111  an uncounted step's Changes link prints no dash where a count goes
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { useState } from 'react'

import type { Result } from '../fetch'
import type { StepUsage, WorkflowBoard, WorkflowUsage } from '../api'
import type { AttemptRow, Task, TaskState, Workflow, WorkflowStep } from '../types'
import { painted } from './marks'
import { ownFromBoard } from './workflowsById'

const api = vi.hoisted(() => ({
  loadWorkflowBoard: vi.fn(),
  loadWorkflowUsage: vi.fn(),
  loadWorkflow: vi.fn(),
  loadAttempts: vi.fn(),
  cancelTask: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => {
  const real = await importOriginal<typeof import('../api')>()
  return { ...real, ...api }
})

const { WorkflowCard, WorkflowsScreen, DOCK_MIN_PX } = await import('../Workflows')
const { StopRun } = await import('../StopRun')
const { stepColumnWidth } = await import('../WorkflowViews')
const { depUnits, monoW, nodeWidthAt, MONO_ADVANCE_EM, NODE_CHROME_W, NODE_W } = await import('../dag')

const T0 = Date.parse('2026-10-11T12:00:00.000Z')
const iso = (offsetSeconds: number) => new Date(T0 + offsetSeconds * 1000).toISOString()

function step(step_id: string, depends_on: string[], over: Partial<WorkflowStep> = {}): WorkflowStep {
  return { step_id, runner_profile: 'claude-code', resource_class: 'standard', depends_on, input_from: {}, task_id: null, ...over }
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
    started_at: iso(-300),
    completed_at: null,
    submitted_by: 'qa@example.com',
    attempt_count: 1,
    max_attempts: 3,
    park_reason: null,
    blocked_by: null,
    workflow_id: 'wf_l11',
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

function workflow(steps: WorkflowStep[], state: Workflow['state'] = 'RUNNING'): Workflow {
  return {
    workflow_id: 'wf_l11',
    state,
    tenant_id: 'u-qa',
    stored_state: state,
    state_source: 'derived',
    rollup: { state, complete: true, reason: 'test', counts: {}, unreadable_steps: [], unstarted_steps: [], steps_read: steps.length },
    created_at: iso(-600),
    updated_at: iso(-20),
    submitted_by: 'qa@example.com',
    priority: 0,
    on_step_failure: 'fail_workflow',
    cancel_requested: false,
    steps,
  }
}

const NO_USAGE: WorkflowUsage = {
  byTaskId: new Map<string, StepUsage>(),
  notSampled: new Set(),
  failed: new Map(),
  sampleLimit: 12,
  tasksRequested: 0,
}
const noAttempts = async (): Promise<Result<{ attempts: AttemptRow[] }>> => ({ status: 'empty', fetchedAt: T0 })

/** A running fan-in: three parents that each stage a file into `merge`. */
function fanIn(): { w: Workflow; tasks: Map<string, Task> } {
  const files = { 'cold-start': 'cold-start.md', allornothing: 'allornothing.md', checkpoints: 'checkpoints.md' }
  const w = workflow([
    step('cold-start', [], { task_id: 't_a' }),
    step('allornothing', [], { task_id: 't_b' }),
    step('checkpoints', [], { task_id: 't_c' }),
    step('merge', Object.keys(files), { task_id: 't_m', input_from: files }),
  ])
  const tasks = new Map<string, Task>([
    ['t_a', task('t_a', 'SUCCEEDED', { completed_at: iso(-200) })],
    ['t_b', task('t_b', 'SUCCEEDED', { completed_at: iso(-200) })],
    ['t_c', task('t_c', 'SUCCEEDED', { completed_at: iso(-200) })],
    ['t_m', task('t_m', 'RUNNING')],
  ])
  return { w, tasks }
}

function card(w: Workflow, tasks: Map<string, Task>, view: 'graph' | 'table' | 'timeline', extra: Record<string, unknown> = {}) {
  return render(
    <WorkflowCard
      workflow={w}
      taskById={tasks}
      usage={{ kind: 'ready', usage: NO_USAGE }}
      openStages={{}}
      onToggleStage={() => {}}
      view={view}
      loadAttempts={noAttempts}
      reload={() => {}}
      {...extra}
    />,
  )
}

const hosts: HTMLElement[] = []
function html(markup: string): HTMLElement {
  const host = document.createElement('div')
  host.innerHTML = markup
  document.body.appendChild(host)
  hosts.push(host)
  return host
}

beforeEach(() => {
  vi.spyOn(Date, 'now').mockReturnValue(T0)
  api.loadWorkflowUsage.mockResolvedValue({ status: 'empty', fetchedAt: T0 } satisfies Result<WorkflowUsage>)
  api.loadAttempts.mockResolvedValue({ status: 'empty', fetchedAt: T0 })
  api.loadWorkflow.mockImplementation(ownFromBoard(api.loadWorkflowBoard))
})

afterEach(() => {
  cleanup()
  for (const h of hosts.splice(0)) h.remove()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('V001: the stop confirmation on a graph node leaves the card', () => {
  // MUTATION: drop `overlay` from the node's StopRun, or render the confirm in
  // place again -- the dialog is then inside the canvas, not over the page.
  it('opens over the page as a modal, takes focus, and gives it back on Escape', () => {
    const { w, tasks } = fanIn()
    const { container } = card(w, tasks, 'graph')
    const slot = container.querySelector<HTMLElement>('.node[data-step="merge"]')!.parentElement!
    const stop = within(slot.querySelector<HTMLElement>('.node-stop')!).getByRole('button', { name: 'Stop' })
    stop.focus()
    fireEvent.click(stop)

    const dialog = screen.getByRole('dialog', { name: 'Stop step merge' })
    expect(dialog.getAttribute('aria-modal')).toBe('true')
    // Out of the card and the canvas entirely: nothing it says can be clipped by them.
    expect(container.contains(dialog), 'the dialog is still inside the card').toBe(false)
    expect(dialog.closest('.c-scrim')).not.toBeNull()
    // Its question and its facts are in it, and it holds focus -- not <body>.
    expect(within(dialog).getByRole('heading', { name: 'Stop step merge?' })).toBeTruthy()
    expect(document.activeElement).toBe(dialog)
    // The button that opened it stays on the node, and says it opened a dialog.
    expect(stop.isConnected).toBe(true)
    expect(stop.getAttribute('aria-expanded')).toBe('true')

    fireEvent.keyDown(dialog, { key: 'Escape' })
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(document.activeElement).toBe(stop)
  })

  it('wraps Tab inside the modal', () => {
    const { w, tasks } = fanIn()
    const { container } = card(w, tasks, 'graph')
    const slot = container.querySelector<HTMLElement>('.node[data-step="merge"]')!.parentElement!
    fireEvent.click(within(slot.querySelector<HTMLElement>('.node-stop')!).getByRole('button', { name: 'Stop' }))
    const dialog = screen.getByRole('dialog')
    const keep = within(dialog).getByRole('button', { name: 'keep running' })
    keep.focus()
    fireEvent.keyDown(dialog, { key: 'Tab' })
    expect(document.activeElement).toBe(within(dialog).getByRole('button', { name: /^stop step/ }))
  })

  it('keeps the in-place dialog where there is room, and still moves focus into it', () => {
    const { container } = render(<StopRun task={task('t_x', 'RUNNING')} what="step x" reload={() => {}} variant="inline" />)
    fireEvent.click(within(container).getByRole('button', { name: 'stop' }))
    const group = within(container).getByRole('group', { name: 'Stop step x' })
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(document.activeElement).toBe(group)
    fireEvent.click(within(group).getByRole('button', { name: 'keep running' }))
    expect(document.activeElement).toBe(within(container).getByRole('button', { name: 'stop' }))
  })
})

describe('V034: the canvas wrapper is bounded by the screen', () => {
  // MUTATION: take `max-height` off `.wf-canvas-wrap`, or put `overflow-x`
  // back alone: the wrapper is then as tall as a 2,200px graph again.
  it('has a viewport-relative max-height and scrolls on both axes', () => {
    const wrap = html('<div class="wf-card is-page"><div class="wf-body"><div class="wf-canvas-wrap"></div></div></div>').querySelector(
      '.wf-canvas-wrap',
    )!
    const max = painted(wrap, 'max-height', { width: 1440 })
    expect(max, 'the canvas wrapper has no height bound').not.toBeNull()
    expect(max).toMatch(/100vh/)
    expect(painted(wrap, ['overflow', 'overflow-y'], { width: 1440 })).toBe('auto')
  })
})

describe('V035: a stacked step card is not a scroller', () => {
  const stacked = () =>
    html(
      '<div class="wf-card"><div class="wf-table-box"><div class="ctl-table wf-table is-scroll has-more"><table><tbody>' +
        '<tr data-step="report" class="t-park"><td data-col="step">report</td><td data-col="state">parked</td>' +
        '<td data-col="why" data-label="Why"><span class="wf-why">Waiting on an earlier step in its workflow.</span></td></tr>' +
        '</tbody></table></div></div></div>',
    )
  // MUTATION: drop the stacked form's `mask-image: none`, or the why line's
  // `white-space: normal` -- the long reason widens the card and the fade dims
  // the state word at its right edge ("succeede").
  it('draws no right-edge fade and lets the why line wrap and shrink', () => {
    const host = stacked()
    const env = { width: 400, container: 334 }
    const table = host.querySelector('.wf-table')!
    expect(painted(table, ['mask-image', '-webkit-mask-image'], env)).toBe('none')
    const why = host.querySelector('td[data-col="why"]')!
    expect(painted(why, 'min-width', env)).toBe('0')
    expect(painted(host.querySelector('.wf-why')!, 'white-space', env)).toBe('normal')
    // The control: as a wide scroll table the fade is still drawn.
    expect(painted(table, ['mask-image', '-webkit-mask-image'], { width: 1440, container: 1020 })).toMatch(/linear-gradient/)
  })
})

describe('V097: the workflow page body has one frame', () => {
  // MUTATION: give `.wfp .wf-card.is-page > .wf-body` its border back.
  it('draws no border on the body inside the page card, and keeps the card’s', () => {
    const host = html('<div class="wfp"><section class="section wf-card is-open is-page"><div class="wf-body"></div></section></div>')
    const body = host.querySelector('.wf-body')!
    expect(painted(body, ['border', 'border-width'], { width: 1440 })).toBe('0')
    expect(painted(host.querySelector('.wf-card')!, ['border', 'border-width'], { width: 1440 })).toMatch(/^1px solid/)
    // The control: on the list, under a bar, the body keeps its three edges.
    const listed = html('<section class="section wf-card is-open"><div class="wf-body"></div></section>').querySelector('.wf-body')!
    expect(painted(listed, ['border', 'border-width'], { width: 1440 })).toMatch(/^1px solid/)
  })
})

describe('V098: the Step column grows to its longest name', () => {
  // MUTATION: drop the head cell's style, or size it by anything but the names.
  it('asks for the longest name at the mono advance, capped by the table’s slack', () => {
    const rows = [{ step: { step_id: 'plan' } }, { step: { step_id: 'scan-terraform' } }]
    const w = String(stepColumnWidth(rows).width)
    expect(w).toContain(`14 * ${MONO_ADVANCE_EM} * var(--t-body)`)
    expect(w).toContain('100cqi - 980px')
  })

  it('puts it on the Step head cell of a table with a Why column', () => {
    const w = workflow([step('scan-terraform', [], { task_id: 't_s' }), step('report', ['scan-terraform'])])
    const tasks = new Map([['t_s', task('t_s', 'FAILED', { completed_at: iso(-10), last_error: 'exit status 1' })]])
    const { container } = card(w, tasks, 'table')
    const th = container.querySelector<HTMLElement>('.wf-table th[data-col="step"]')!
    expect(container.querySelector('.wf-table th[data-col="why"]'), 'the fixture has no Why column').not.toBeNull()
    expect(th.style.width).toContain('14 * 0.6')
  })
})

describe('V099, V100: the inspector docks only beside a split wide enough for the view', () => {
  function measuredAs(width: number) {
    vi.stubGlobal(
      'ResizeObserver',
      class {
        constructor(private cb: () => void) {}
        observe() {
          this.cb()
        }
        disconnect() {}
      },
    )
    vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockImplementation(function (this: HTMLElement) {
      return this.classList.contains('is-rows') ? width : 0
    })
  }
  const picked = (view: 'table' | 'timeline') => {
    const { w, tasks } = fanIn()
    return card(w, tasks, view, { picked: 'merge', onPick: () => {} })
  }

  // MUTATION: dock by the viewport alone again (`narrow = useNarrow()`).
  it('opens the Table’s inspector under its row in a 1,020px split, and docks it in a wide one', async () => {
    measuredAs(1020)
    const narrow = picked('table')
    await waitFor(() => expect(narrow.container.querySelector('.wf-inline-row .wf-inspect')).not.toBeNull())
    expect(narrow.container.querySelector('.wf-panel')).toBeNull()
    narrow.unmount()

    measuredAs(DOCK_MIN_PX.table)
    const wide = picked('table')
    await waitFor(() => expect(wide.container.querySelector('.wf-panel .wf-inspect')).not.toBeNull())
  })

  it('does the same for the Timeline at its own width', async () => {
    measuredAs(DOCK_MIN_PX.timeline - 1)
    const narrow = picked('timeline')
    await waitFor(() => expect(narrow.container.querySelector('.wf-split-main .wf-inspect')).not.toBeNull())
    expect(narrow.container.querySelector('.wf-panel')).toBeNull()
    narrow.unmount()

    measuredAs(DOCK_MIN_PX.timeline)
    const wide = picked('timeline')
    await waitFor(() => expect(wide.container.querySelector('.wf-panel .wf-inspect')).not.toBeNull())
  })

  // MUTATION: put the × back after the links.
  it('ends the inspector head’s first line with the ×, beside the workflow', () => {
    const { container } = picked('table')
    const head = container.querySelector<HTMLElement>('.wf-inspect-head')!
    const close = within(head).getByRole('button', { name: 'Stop inspecting this step' })
    expect(close.previousElementSibling?.classList.contains('wf-inspect-wf')).toBe(true)
    const wf = head.querySelector('.wf-inspect-wf')!
    expect(painted(wf, ['flex', 'flex-basis'], { width: 1440 })).toMatch(/calc\(100% - 28px/)
  })
})

describe('V101: the held State cell of the list keeps a failed row’s tint', () => {
  const list = () =>
    html(
      '<div class="table-wrap is-scroll wfl-scroll"><table class="wfl-table"><thead><tr><th>State</th><th>Workflow</th></tr></thead>' +
        '<tbody><tr class="wfl-row is-failed"><td>failed</td><td>wf</td></tr><tr class="wfl-row"><td>running</td><td>wf</td></tr></tbody></table></div>',
    )
  // MUTATION: drop the held-cell branch of the failed-row rule.
  it('paints the failed tint on the held cell at 400px, and --surface on a healthy one', () => {
    const host = list()
    const [failed, ok] = [...host.querySelectorAll('tbody tr')].map((r) => r.querySelector('td')!)
    expect(painted(failed!, ['background', 'background-color'], { width: 400 })).toBe('var(--s-badb)')
    expect(painted(ok!, ['background', 'background-color'], { width: 400 })).toBe('var(--surface)')
  })

  // MUTATION: drop `.wfl-table thead`'s fill: the corner is then the one tinted head cell.
  it('fills the head row with the corner’s colour, on thead', () => {
    const host = list()
    const thead = host.querySelector('thead')!
    const corner = host.querySelector('th')!
    expect(painted(thead, ['background', 'background-color'], { width: 400 })).toBe('var(--surface-2)')
    expect(painted(corner, ['background', 'background-color'], { width: 400 })).toBe('var(--surface-2)')
  })
})

describe('V102: a fan-in names its files whole, and the integrator is its whole id', () => {
  // MUTATION: drop `depW` from `nodeWidthAt`.
  it('lays every tier out wide enough for the widest dependency unit, up to the full tier', () => {
    const { w } = fanIn()
    const widest = Math.max(...w.steps.flatMap((s) => depUnits(s).map((u) => u.text.length)))
    expect(widest, 'the fixture has no unit wider than a names-tier line').toBe('(allornothing.md),'.length)
    for (const tier of ['names', 'details', 'figures'] as const) {
      const room = nodeWidthAt(tier, w.steps) - NODE_CHROME_W
      expect(room, `${tier}: the widest unit wraps inside itself`).toBeGreaterThanOrEqual(monoW(widest, 12))
    }
    // A path longer than the full tier's column does not widen the canvas past it.
    const long = workflow([step('a', []), step('b', ['a'], { input_from: { a: `${'x'.repeat(80)}.md` } })])
    expect(nodeWidthAt('names', long.steps)).toBeLessThanOrEqual(NODE_W)
  })
})

describe('V103: the page skeleton draws the count note’s line', () => {
  function Routed({ initial }: { initial: string }) {
    const [view, setView] = useState(initial)
    return <WorkflowsScreen view={view} onView={setView} />
  }
  // MUTATION: drop the placeholder note: the tabs drop by its line on load.
  it('puts a count-note line before the page frame while it reads, where the loaded page puts its census', async () => {
    let land: (r: Result<WorkflowBoard>) => void = () => {}
    api.loadWorkflowBoard.mockReturnValue(new Promise<Result<WorkflowBoard>>((r) => (land = r)))
    render(<Routed initial="wf=wf_l11" />)
    const page = document.querySelector<HTMLElement>('.wfp.is-loading')!
    const note = page.previousElementSibling
    expect(note?.classList.contains('c-count-note'), 'no count-note line over the skeleton').toBe(true)
    expect(note!.getAttribute('aria-hidden')).toBe('true')
    expect(note!.querySelectorAll('.wfl-skel').length).toBeGreaterThan(0)
    expect(painted(note!, 'display', { width: 1440 })).toBe('flow-root')

    const { w, tasks } = fanIn()
    await act(async () => land({ status: 'ok', data: { workflows: [w], taskById: tasks, statesDetail: null }, fetchedAt: T0 }))
    await waitFor(() => expect(document.querySelector('.wfp.is-loading')).toBeNull())
    const loaded = document.querySelector<HTMLElement>('.wfp')!
    expect(loaded.parentElement!.querySelector(':scope > .c-count-note')).not.toBeNull()
  })

  it('makes the page head two lines in both states at a phone', () => {
    const host = html('<div class="wfp-screen"><div class="c-phead"><div class="head"></div><div class="c-acts"></div></div></div>')
    const acts = host.querySelector('.c-acts')!
    expect(painted(acts, ['flex', 'flex-basis'], { width: 400 })).toBe('1 0 100%')
  })
})

describe('V110: the empty list says what a workflow is and where one is submitted', () => {
  // MUTATION: drop the empty state's link.
  it('links to Submit › workflow', async () => {
    api.loadWorkflowBoard.mockResolvedValue({ status: 'empty', fetchedAt: T0 })
    render(<WorkflowsScreen view="" onView={() => {}} />)
    const link = await screen.findByRole('link', { name: /Submit a workflow/ })
    expect(link.getAttribute('href')).toBe('#work/new-workflow')
    expect(document.body.textContent).toMatch(/A workflow is a set of steps submitted together/)
  })
})
