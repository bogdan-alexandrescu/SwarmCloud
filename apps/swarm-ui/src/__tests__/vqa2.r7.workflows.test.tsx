// VISUAL QA RE-RUN, LANE R7 (#1427): THE WORKFLOW LIST AND THE WORKFLOW PAGE.
//
// Ten rendering findings from the 2026-10-16 re-run, each pinned by the
// property that was wrong rather than by the markup that fixed it. The browser
// renders that confirmed them are in the PR (vqa2-r7-<id>.png); what jsdom can
// hold is held here: the value the cascade gives a property at a width, which
// element a value is in, the width a column asks for.
//
//   V101  a failed list card at 400 is tinted across the card, not in its State cell
//   V103  the page head is two chip lines in both states at a phone
//   V163  the list's Cost cell keeps "N of M reported" whole, on its own line
//   V164  the list prints the cost and the runner mix below 900px
//   V200  (workflows.liveqa.test.tsx, WF-6) ◀ position ▶ is one unit
//   V201  every Stop-step fact is one key column and one value column
//   V202  the step table's figure columns hold their values; Why takes the rest
//   V203  a fully sampled table draws no empty head row
//   V204  a stacked step card is inset as far on the right as on the left
//   V213  a toned row's held first cell paints the row's wash
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, waitFor, within } from '@testing-library/react'
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

const { WorkflowCard, WorkflowsScreen } = await import('../Workflows')
const { StopRun } = await import('../StopRun')

const T0 = Date.parse('2026-10-16T12:00:00.000Z')
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
    workflow_id: 'wf_r7',
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
    workflow_id: 'wf_r7',
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

/** A read in which every task was sampled: nothing for the sample mark to say. */
const ALL_SAMPLED: WorkflowUsage = {
  byTaskId: new Map<string, StepUsage>(),
  notSampled: new Set(),
  failed: new Map(),
  sampleLimit: 12,
  tasksRequested: 0,
}
const noAttempts = async (): Promise<Result<{ attempts: AttemptRow[] }>> => ({ status: 'empty', fetchedAt: T0 })

/** A failed fan-in: a failed step with a reason, one blocked after it, two that succeeded. */
function failedRun(): { w: Workflow; tasks: Map<string, Task> } {
  const w = workflow(
    [
      step('cold-start', [], { task_id: 't_a' }),
      step('checkpoints', [], { task_id: 't_c' }),
      step('fencing', [], { task_id: 't_f' }),
      step('synthesis', ['cold-start', 'checkpoints', 'fencing'], { task_id: 't_s' }),
    ],
    'FAILED',
  )
  const tasks = new Map<string, Task>([
    ['t_a', task('t_a', 'SUCCEEDED', { completed_at: iso(-200) })],
    ['t_c', task('t_c', 'FAILED', { completed_at: iso(-200), attempt_count: 3, last_error: 'exit status 1: pytest collected 2 failures' })],
    ['t_f', task('t_f', 'SUCCEEDED', { completed_at: iso(-200) })],
    ['t_s', task('t_s', 'CANCELLED', { completed_at: iso(-100) })],
  ])
  return { w, tasks }
}

function table(w: Workflow, tasks: Map<string, Task>, usage: WorkflowUsage = ALL_SAMPLED) {
  return render(
    <WorkflowCard
      workflow={w}
      taskById={tasks}
      usage={{ kind: 'ready', usage }}
      openStages={{}}
      onToggleStage={() => {}}
      view="table"
      loadAttempts={noAttempts}
      reload={() => {}}
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

/** The list's markup as Workflows.tsx draws it: a card per row at 640 and below. */
function listRow(failed: boolean): HTMLElement {
  return html(
    '<div class="wfl"><div class="table-wrap is-scroll wfl-scroll"><table class="wfl-table"><thead><tr><th data-col="state">State</th></tr></thead><tbody>' +
      `<tr class="wfl-row${failed ? ' is-failed' : ''}">` +
      '<td class="wfl-state" data-col="state">failed</td>' +
      '<td class="wfl-name" data-col="workflow"><a href="#">wf</a><span class="wfl-mix"><span class="wf-mix"><span class="c-chip">claude-code</span></span></span></td>' +
      '<td class="num" data-col="cost"><span class="wf-spend">$0.426<span class="wf-spend-cov">3 of 4 reported</span></span></td>' +
      '<td data-col="runners"><span class="wf-mix"><span class="c-chip">claude-code</span></span></td>' +
      '</tr></tbody></table></div></div>',
  )
}
const fill = ['background', 'background-color']

describe('V101: a failed list card is tinted across the card', () => {
  // MUTATION: drop the card's row tint, or the held-cell `background: none`:
  // the State cell alone is red again, a bar that stops short of the card.
  it('tints the row and no cell at 400, the held State cell included', () => {
    const host = listRow(true)
    const row = host.querySelector('tr.wfl-row')!
    expect(painted(row, fill, { width: 400 })).toBe('var(--s-badb)')
    for (const td of host.querySelectorAll('tr.wfl-row > td')) {
      expect(painted(td, fill, { width: 400 }), (td as HTMLElement).dataset.col).toBe('none')
    }
    // The control: a healthy card is not tinted.
    const ok = listRow(false).querySelector('tr.wfl-row')!
    expect(painted(ok, fill, { width: 400 })).not.toBe('var(--s-badb)')
  })
})

describe('V103: the page head is the same height while it reads as when it has read', () => {
  // MUTATION: drop the phone rule that gives the cost its own line: the
  // loaded head is one chip line or two by what the cost says.
  it('gives the cost slot a line of its own at a phone, and shares the line wider', () => {
    const host = html('<div class="wfp"><div class="wfp-head"><div class="wfp-chips"><span class="c-chip">on failure</span><span class="wfp-cost"><span class="c-chip">$0.0366</span></span></div></div></div>')
    const cost = host.querySelector('.wfp-cost')!
    expect(painted(cost, ['flex', 'flex-basis'], { width: 400 })).toBe('1 0 100%')
    expect(painted(cost, ['flex', 'flex-basis'], { width: 1440 })).toBeNull()
  })

  function Routed({ initial }: { initial: string }) {
    const [view, setView] = useState(initial)
    return <WorkflowsScreen view={view} onView={setView} />
  }
  // MUTATION: draw the skeleton's cost bar as a bare chip again, or let the
  // other bars keep `--lh-meta`'s margins (a 27px chip beside a 22px one).
  it('draws the skeleton’s cost bar in the same slot, and the other bars at the text’s line', async () => {
    let land: (r: Result<WorkflowBoard>) => void = () => {}
    api.loadWorkflowBoard.mockReturnValue(new Promise<Result<WorkflowBoard>>((r) => (land = r)))
    render(<Routed initial="wf=wf_r7" />)
    const chips = document.querySelector<HTMLElement>('.wfp.is-loading .wfp-chips')!
    const last = chips.lastElementChild!
    expect(last.classList.contains('wfp-cost'), 'the skeleton’s cost bar is not in the cost slot').toBe(true)
    expect(last.querySelector('.c-chip > .wfl-skel')).not.toBeNull()
    const bar = chips.querySelector(':scope > .c-chip > .wfl-skel')!
    expect(painted(bar, 'margin', { width: 400 })).toMatch(/var\(--lh-flush\)/)
    expect(painted(last.querySelector('.wfl-skel')!, 'margin', { width: 400 })).toMatch(/var\(--lh-meta\)/)
    const { w, tasks } = failedRun()
    await act(async () => land({ status: 'ok', data: { workflows: [w], taskById: tasks, statesDetail: null }, fetchedAt: T0 }))
    await waitFor(() => expect(document.querySelector('.wfp.is-loading')).toBeNull())
    expect(document.querySelector('.wfp-head > .wfp-chips > .wfp-cost')).not.toBeNull()
  })

  // Rendered in Chromium at 400 (2026-10-16): the read head was 98px while
  // the cost said "cost not reported" and 101px once the figure landed.
  // MUTATION: drop the line height from the absent cost (its chip is the
  // bare 12px line, 3.4px shorter than the figure's), or keep the full
  // line (its dashed underline makes the chip 1px taller).
  it('sets "cost not reported" in the cost chip on the figure’s own line', () => {
    const host = html('<div class="wfp"><div class="wfp-head"><div class="wfp-chips"><span class="wfp-cost"><span class="c-chip"><span class="wf-cell is-absent">cost not reported</span></span></span></div></div></div>')
    const absent = host.querySelector('.wf-cell.is-absent')!
    for (const width of [400, 1440]) {
      expect(painted(absent, 'line-height', { width })).toBe('var(--lh-micro)')
      expect(painted(absent, 'margin-bottom', { width })).toBe('-1px')
    }
  })
})

describe('V163: the list’s Cost cell keeps its coverage whole', () => {
  // MUTATION: drop the coverage line's `display: block` or its wrapping:
  // the cell's nowrap clip takes "reported" off "3 of 4".
  it('puts the coverage on its own line, wrapping at its spaces, in a column that holds its longest word', () => {
    const host = listRow(false)
    const cov = host.querySelector('.wf-spend-cov')!
    expect(painted(cov, 'display', { width: 1440 })).toBe('block')
    expect(painted(cov, 'margin-left', { width: 1440 })).toBe('0')
    expect(painted(cov, 'white-space', { width: 1440 })).toBe('normal')
    // The control: the cell itself still clips on one line, so the coverage's
    // own wrapping is what keeps it whole.
    expect(painted(cov.closest('td')!, 'white-space', { width: 1440 })).toBe('nowrap')
    // "reported", its longest word, is 8 characters of 12px mono; the cell pads 10px a side.
    const th = html('<div class="wfl"><table class="wfl-table"><thead><tr><th data-col="cost">Cost</th></tr></thead></table></div>').querySelector('th')!
    expect(Number.parseFloat(painted(th, 'width', { width: 1440 }) ?? '0')).toBeGreaterThanOrEqual('reported'.length * 0.6 * 12 + 20)
  })
})

describe('V164: the list prints the cost and the runner mix below 900px', () => {
  // MUTATION: put back the old list bar's `.wf-spend` / `.wf-mix`
  // `display: none` in either narrow block.
  it('hides neither at a phone, a tablet or a desktop', () => {
    const host = listRow(false)
    const spend = host.querySelector('.wf-spend')!
    const mixes = [...host.querySelectorAll('.wf-mix')]
    for (const width of [390, 400, 600, 700, 899, 1440]) {
      expect(painted(spend, 'display', { width }), `cost at ${width}`).not.toBe('none')
      for (const m of mixes) expect(painted(m, 'display', { width }), `runner mix at ${width}`).not.toBe('none')
    }
  })
})

describe('V201: every Stop-step fact is a key and one value', () => {
  // MUTATION: unwrap a value (its text and `<code>` become loose items again),
  // or put the fact back on a wrapping flex row.
  it('gives each fact exactly a key and a `.stop-val`, laid out as two columns', () => {
    const steps = [step('scan', [], { task_id: 't_scan' }), step('report', ['scan'])]
    const w = workflow(steps)
    const t = task('t_scan', 'RUNNING')
    const { container } = render(
      <StopRun task={t} what="step scan" workflow={w} step={steps[0]!} taskById={new Map([['t_scan', t]])} reload={() => {}} variant="inline" />,
    )
    fireEvent.click(within(container).getByRole('button', { name: 'stop' }))
    const facts = [...container.querySelectorAll<HTMLElement>('.stop-facts > .ctl-fact')]
    expect(facts.length, 'the stop, attempt, work, effect and blast facts').toBeGreaterThanOrEqual(5)
    for (const li of facts) {
      const key = li.querySelector(':scope > b')!.textContent
      expect([...li.children].map((c) => c.tagName + (c.className ? `.${c.className}` : '')), key!).toEqual(['B', 'SPAN.stop-val'])
      const loose = [...li.childNodes].filter((n) => n.nodeType === Node.TEXT_NODE && n.textContent!.trim() !== '')
      expect(loose, `${key}: text outside its value`).toEqual([])
      expect(painted(li, 'display', { width: 400 }), key!).toBe('grid')
      expect(painted(li, 'grid-template-columns', { width: 400 }), key!).toBe('13ch minmax(0, 1fr)')
    }
    const stops = facts.find((li) => li.querySelector('b')!.textContent === 'stops')!
    expect(stops.querySelector('.stop-val')!.textContent).toBe('t_scan step scan')
  })
})

describe('V202: the step table’s figure columns are as wide as what they print', () => {
  const FIGURES = ['state', 'waited', 'ran', 'attempts', 'cost', 'tokens'] as const
  // MUTATION: drop the inline widths (every figure column is its ceiling
  // again, and Why is 60-75px at 1440), or size a column by anything but its
  // own longest value.
  it('asks each figure column for its longest value, its head as a floor and its ceiling as a cap', () => {
    const { w, tasks } = failedRun()
    const { container } = table(w, tasks)
    expect(container.querySelector('.wf-table th[data-col="why"]'), 'the fixture has no Why column').not.toBeNull()
    for (const col of FIGURES) {
      const th = container.querySelector<HTMLElement>(`.wf-table th[data-col="${col}"]`)!
      const width = th.style.width
      expect(width, col).toMatch(/^min\(max\(calc\(\d+ \* 0\.6 \* var\(--t-meta\) \+ 12px\), calc\(\d+ \* 0\.6 \* var\(--t-(micro|meta)\) \+ \d+px\)\), \d+px\)$/)
      const cells = [...container.querySelectorAll<HTMLElement>(`.wf-table tbody tr[data-step] > td[data-col="${col}"]`)]
      // The state cell's text is its word; every other cell's is its figure.
      const longest = Math.max(...cells.map((td) => td.textContent!.trim().length))
      expect(width, `${col} is not sized to its longest value (${longest})`).toContain(`calc(${longest} * 0.6 * var(`)
    }
    // Ran's ceiling holds `between attempts` (16 characters of 12px mono).
    const ran = container.querySelector<HTMLElement>('.wf-table th[data-col="ran"]')!.style.width
    expect(Number(/(\d+)px\)$/.exec(ran)![1])).toBeGreaterThanOrEqual(16 * 0.6 * 12 + 12)
  })
})

describe('V203: a fully sampled table draws no empty head row', () => {
  // MUTATION: hand the table the sample note as an element again: it renders
  // nothing, and the table draws an empty 32px row and its margin.
  it('draws no head row when every task was sampled, and the mark when one was not', () => {
    const { w, tasks } = failedRun()
    const full = table(w, tasks)
    expect(full.container.querySelector('.wf-table-head')).toBeNull()
    full.unmount()
    // The control: a task outside the read ceiling is said, in the head row.
    const partial = table(w, tasks, { ...ALL_SAMPLED, notSampled: new Set(['t_s']) })
    expect(partial.container.querySelector('.wf-table-head .wf-table-marks')?.textContent).toMatch(/sampled/)
  })
})

describe('V204: a stacked step card is inset on the right as on the left', () => {
  // MUTATION: put the row's right padding back at 4px.
  it('pads the stacked row 14px on both sides', () => {
    const host = html(
      '<div class="wf-card"><div class="wf-table-box"><div class="ctl-table wf-table is-scroll"><table><tbody>' +
        '<tr data-step="plan" class="t-neu"><td data-col="step">plan</td><td data-col="state">succeeded</td></tr>' +
        '</tbody></table></div></div></div>',
    )
    const row = host.querySelector('tr[data-step]')!
    const env = { width: 400, container: 334 }
    expect(painted(row, ['padding', 'padding-right'], env)).toBe('6px 14px 6px 14px')
  })
})

describe('V213: a toned row’s held first cell paints the row’s wash', () => {
  const outcomes = (tone: string) =>
    html(
      '<div class="ctl-table is-scroll"><table><thead><tr><th scope="col">Workflow</th><th scope="col">By</th></tr></thead><tbody>' +
        `<tr class="${tone}"><th scope="row">wf_bcdc…9f31</th><td>engineer-a</td></tr>` +
        '</tbody></table></div>',
    )
  // MUTATION: drop the toned held-cell rules: the held Workflow cell paints
  // `--surface` over the row's red, the one untinted cell in a failed row.
  it('composites the wash over --surface in the held cell, for each tone', () => {
    const cases: [string, RegExp][] = [
      ['is-bad', /^color-mix\(in srgb, var\(--bad\) 8%, var\(--surface\)\)$/],
      ['is-warn', /^color-mix\(in srgb, var\(--warn\) 8%, var\(--surface\)\)$/],
      ['is-paused', /^color-mix\(in srgb, var\(--paused\) 7%, var\(--surface\)\)$/],
    ]
    for (const [tone, want] of cases) {
      const held = outcomes(tone).querySelector('tbody th')!
      expect(painted(held, fill, { width: 400 }), tone).toMatch(want)
    }
    // The control: an untoned row's held cell is still the opaque surface.
    expect(painted(outcomes('').querySelector('tbody th')!, fill, { width: 400 })).toBe('var(--surface)')
  })
})
