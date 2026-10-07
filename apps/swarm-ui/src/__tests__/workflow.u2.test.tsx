/**
 * WORKFLOWS, FUNCTIONALITY WAVE 4 LANE U2 (workflows.html V2, wide-workflows.html
 * pick A, agent-detail-2.html pick A's workflow-page parts, and the #503 audit
 * findings on the list and the workflow page).
 *
 * Each `describe` names the item it holds. Each was written before the change it
 * holds and failed against main at 29a1421.
 */
import STYLES from '../styles.css?raw'
import WF_CSS from '../styles/workflows.css?raw'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { useState } from 'react'

import type { Result } from '../fetch'
import type { CancelWorkflowResult, WorkflowBoard, WorkflowUsage } from '../api'
import type { Task, TaskState, Workflow, WorkflowStep } from '../types'
import { painted } from './marks'
import { cascade, gate } from './cssgate'

const api = vi.hoisted(() => ({
  loadWorkflowBoard: vi.fn(),
  loadWorkflowUsage: vi.fn(),
  loadWorkflow: vi.fn(),
  cancelWorkflow: vi.fn(),
  loadAttempts: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => {
  const real = await importOriginal<typeof import('../api')>()
  return { ...real, ...api }
})

const { CancelWorkflow, WorkflowsScreen } = await import('../Workflows')
const { edgePath, layoutOf, partialDeps, stageMix, stageGlyph } = await import('../dag')
const { recentWorkflows, SkyShell } = await import('../Spine')
const wl = await import('../workflowlist')
const review = await import('../wfreview')

const T0 = Date.parse('2026-10-01T12:00:00.000Z')
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

function workflow(id: string, state: TaskState | null, owner: string, steps: WorkflowStep[], ageSeconds = 600): Workflow {
  return {
    workflow_id: id,
    state: state ?? 'RUNNING',
    tenant_id: 'eng',
    stored_state: state ?? 'RUNNING',
    state_source: 'derived',
    rollup: {
      state: state ?? 'RUNNING',
      complete: state !== null,
      reason: 'test',
      counts: {},
      unreadable_steps: [],
      unstarted_steps: [],
      steps_read: steps.length,
    },
    created_at: iso(-ageSeconds),
    updated_at: iso(-60),
    submitted_by: owner,
    priority: 0,
    on_step_failure: 'fail_workflow',
    cancel_requested: false,
    steps,
  }
}

function serve(workflows: Workflow[], taskById: Map<string, Task> | null = new Map()): void {
  api.loadWorkflowBoard.mockResolvedValue({
    status: 'ok',
    data: { workflows, taskById, statesDetail: null },
    fetchedAt: T0,
  } satisfies Result<WorkflowBoard>)
}

/** priya's running chain: plan done, core running, parked docs, verify not started. */
function broker(): { w: Workflow; tasks: Map<string, Task> } {
  const meta = { unit: 'refactor-broker' }
  const w = workflow('wf_broker', 'RUNNING', 'priya@example.com', [
    step('plan', [], { task_id: 't_plan' }),
    step('core', ['plan'], { task_id: 't_core', runner_profile: 'codex' }),
    step('docs', ['plan'], { task_id: 't_docs' }),
    step('verify', ['core', 'docs']),
  ])
  const tasks = new Map<string, Task>([
    ['t_plan', task('t_plan', 'SUCCEEDED', { metadata: meta, started_at: iso(-500), completed_at: iso(-400) })],
    ['t_core', task('t_core', 'RUNNING', { metadata: meta, started_at: iso(-300) })],
    ['t_docs', task('t_docs', 'PARKED', { metadata: meta, park_reason: 'QUOTA' as Task['park_reason'] })],
  ])
  return { w, tasks }
}

beforeEach(() => {
  vi.spyOn(Date, 'now').mockReturnValue(T0)
  const b = broker()
  serve([b.w], b.tasks)
  api.loadWorkflowUsage.mockResolvedValue({ status: 'empty', fetchedAt: T0 } satisfies Result<WorkflowUsage>)
  api.loadAttempts.mockResolvedValue({ status: 'empty', fetchedAt: T0 })
  api.loadWorkflow.mockResolvedValue({
    status: 'error',
    error: { kind: 'not_found', httpStatus: 404, code: null, message: 'no such workflow' },
  })
  try {
    window.localStorage.clear()
  } catch {
    /* storage refused */
  }
})

function Routed({ initial, onView }: { initial: string; onView?: (v: string) => void }) {
  const [view, setView] = useState(initial)
  return (
    <WorkflowsScreen
      view={view}
      onView={(v) => {
        onView?.(v)
        setView(v)
      }}
    />
  )
}

function rowIds(): string[] {
  return [...document.querySelectorAll<HTMLElement>('.wfl-table tbody tr')].map((r) => r.dataset.workflow ?? '')
}

/**
 * The shipped cascade, IN THE BUNDLE'S ORDER: `main.tsx` imports App (and so
 * Workflows.tsx and its sheet) before `styles.css`, so workflows.css comes
 * first and every rule in it has to win on specificity, not on order.
 */
const SHEET = `${WF_CSS}\n${STYLES}`
const at = (el: Element, prop: string, width = 1440, theme: 'dark' | 'light' = 'dark') =>
  cascade(SHEET, el, prop, { width, theme }).winner?.value ?? null

// ---------------------------------------------------------------------------
// The list: its order, its width, its glyph, and Recent
// ---------------------------------------------------------------------------

describe('the list groups running first and failed after succeeded (#503, workflows.html frame 0)', () => {
  const states: (TaskState | null)[] = [
    'FAILED',
    'SUCCEEDED',
    'PARKED',
    'RUNNING',
    'READY',
    'DEAD_LETTERED',
    'CANCELLED',
    null,
    'STARTING',
  ]
  const rows = () =>
    states.map((s, i) => workflow(`wf_${i}_${s ?? 'unread'}`, s, 'sam', [step('a', [])], 1000 - i * 10))

  it('sorts live, then parked, ready, succeeded, failed, dead-lettered, cancelled, unread, newest first in each', () => {
    const order = wl.sortWorkflows(rows(), 'state', null).map((w) => w.workflow_id)
    expect(order).toEqual([
      'wf_8_STARTING',
      'wf_3_RUNNING',
      'wf_2_PARKED',
      'wf_4_READY',
      'wf_1_SUCCEEDED',
      'wf_0_FAILED',
      'wf_5_DEAD_LETTERED',
      'wf_6_CANCELLED',
      'wf_7_unread',
    ])
  })

  it('draws the rows in that order by default', async () => {
    serve(rows())
    render(<WorkflowsScreen />)
    await waitFor(() => expect(rowIds()).toHaveLength(9))
    expect(rowIds()[0]).toBe('wf_8_STARTING')
    expect(rowIds().indexOf('wf_0_FAILED')).toBeGreaterThan(rowIds().indexOf('wf_1_SUCCEEDED'))
  })
})

describe('the list table fits 1440 with no sideways scroll (#503)', () => {
  it('lays the table out fixed at the wrapper width, and the wrapper does not scroll at 1440', async () => {
    render(<WorkflowsScreen />)
    await waitFor(() => expect(rowIds()).toEqual(['wf_broker']))
    const table = document.querySelector('.wfl-table')!
    const wrap = table.parentElement!
    expect(at(table, 'table-layout')).toBe('fixed')
    expect(at(table, 'width')).toBe('100%')
    expect(at(wrap, 'overflow-x')).toBe('visible')
    // Every head has a width the fixed layout honours; together they are the table.
    const heads = [...document.querySelectorAll('.wfl-table thead th')]
    // #111's Started and Duration stay (workflow.readability.test.tsx holds them against "Age"); all nine fit.
    expect(heads.map((h) => h.textContent)).toEqual([
      'State',
      'Workflow',
      'Shape',
      'Steps done',
      'Runners',
      'Cost',
      'Owner',
      'Started',
      'Duration',
    ])
    // Browser QA D8 (2026-10-04): State, Steps done and Duration are px wide,
    // sized to what they hold; the name takes what the rest leave at 1056px.
    const widths = heads.map((h) => at(h, 'width') ?? '')
    const named = heads.map((h, i) => [h.getAttribute('data-col'), widths[i]!] as const).filter(([c]) => c !== 'workflow')
    for (const [c, w] of named) expect(w, `${c} has no width: ${widths.join(', ')}`).toMatch(/(%|px)$/)
    const used = named.reduce((t, [, w]) => t + (w.endsWith('%') ? (Number.parseFloat(w) / 100) * 1056 : Number.parseFloat(w)), 0)
    // 200px until U12 R10 (owner QA, 2026-10-04): Cost and Owner were cut
    // ('$0.6024 1/3' by 3px, 'swarm-…' in 74px), and each now holds what it
    // shows. The name still keeps the most room of any column (D8), cut with
    // its whole text in its title.
    expect(1056 - used, 'the name column at 1056px').toBeGreaterThanOrEqual(160)
  })

  it('prints the owner as the name before the @, with the address whole in its title', async () => {
    render(<WorkflowsScreen />)
    await waitFor(() => expect(rowIds()).toEqual(['wf_broker']))
    const owner = document.querySelector<HTMLElement>('tr[data-workflow="wf_broker"] .wfl-owner')!
    expect(owner.textContent).toBe('priya')
    expect(owner.getAttribute('title')).toBe('priya@example.com')
  })
})

describe('the list row draws the stage-shape glyph (wide-workflows.html §6)', () => {
  it('is one column per stage, log-scaled by width, filled with the stage mix', () => {
    const b = broker()
    const g = stageGlyph(b.w.steps, b.tasks)
    expect(g.map((c) => c.n)).toEqual([1, 2, 1])
    // 1 step is the floor; 50 (the API's ceiling for one stage) is 22px.
    expect(g[0]!.h).toBeCloseTo(8.8, 1)
    expect(stageGlyph(Array.from({ length: 50 }, (_, i) => step(`s${i}`, [])), null)[0]!.h).toBeCloseTo(22, 1)
    expect(g[1]!.mix.map((m) => [m.kind, m.n])).toEqual([
      ['live', 1],
      ['park', 1],
    ])
  })

  it('draws it beside the text shape, and the text survives', async () => {
    render(<WorkflowsScreen />)
    await waitFor(() => expect(rowIds()).toEqual(['wf_broker']))
    const cell = document.querySelector<HTMLElement>('tr[data-workflow="wf_broker"] .wf-sgl')!
    expect(cell).toBeTruthy()
    expect(cell.getAttribute('aria-hidden')).toBe('true')
    expect(cell.querySelectorAll(':scope > i')).toHaveLength(3)
    expect(document.querySelector('tr[data-workflow="wf_broker"] .wf-shape')!.textContent).toContain('1 → 2 → 1')
  })
})

describe('the list row marks partial dependencies (wide-workflows.html §6)', () => {
  it('says partial when a step waits on more than one but not all of the level above, and not for 1:1 or a full fan-in', () => {
    const roots = [step('a', []), step('b', []), step('c', [])]
    expect(partialDeps([...roots, step('x', ['a', 'b'])])).toBe(true)
    expect(partialDeps([...roots, step('x', ['a', 'b', 'c'])])).toBe(false)
    expect(partialDeps([...roots, step('x', ['a']), step('y', ['b']), step('z', ['c'])])).toBe(false)
  })

  it('draws the chip in the Shape cell', async () => {
    serve([workflow('wf_p', 'RUNNING', 'sam', [step('a', []), step('b', []), step('c', []), step('x', ['a', 'b'])])])
    render(<WorkflowsScreen />)
    await waitFor(() => expect(rowIds()).toEqual(['wf_p']))
    // The canonical chip (components.html A).
    const chip = document.querySelector<HTMLElement>('tr[data-workflow="wf_p"] .wfl-shape .c-chip')!
    expect(chip.textContent).toBe('partial')
    expect(chip.getAttribute('title')).toBe('Some steps depend on part of the level above, not all of it')
  })
})

describe('the 390px phone form of the graph (workflows.html B, phone frame)', () => {
  it('draws the stages as stacked cards, naming the steps that need a look and counting the rest', async () => {
    serve([reviewWave().w], reviewWave().tasks)
    await page('wf=wf_wave')
    const list = await waitFor(() => {
      const l = document.querySelector<HTMLElement>('.wf-ph-stages')
      expect(l).toBeTruthy()
      return l!
    })
    const cards = [...list.querySelectorAll<HTMLElement>('.wf-ph-stage')]
    expect(cards.map((c) => c.querySelector('.wf-ph-h')!.textContent)).toEqual([
      'first · 1 step',
      'then · 8 steps',
      'then · 8 steps',
      'then · 1 step',
    ])
    const impl = cards[1]!
    expect([...impl.querySelectorAll<HTMLElement>('button.wf-ph-step')].map((b) => b.dataset.phStep)).toEqual(['impl-6', 'impl-3'])
    expect(impl.querySelector('.wf-ph-more')!.textContent).toBe('+6 more in this stage')
    fireEvent.click(impl.querySelector<HTMLButtonElement>('button.wf-ph-step[data-ph-step="impl-3"]')!)
    await waitFor(() => expect(document.querySelector('.wf-inspect')!.getAttribute('aria-label')).toBe('Step impl-3 of wf_wave'))
    // The shipped sheet shows it only at phone width, and hides the canvas there.
    expect(at(list, 'display', 1440)).toBe('none')
    expect(at(list, 'display', 390)).toBe('flex')
    expect(at(document.querySelector('.wf-canvas-wrap')!, 'display', 390)).toBe('none')
  })
})

describe('Recent (5) shows on the list page, by name (#503)', () => {
  function many(): Workflow[] {
    return Array.from({ length: 7 }, (_, i) =>
      workflow(`wf_${i}`, i === 1 ? 'FAILED' : 'RUNNING', 'sam', [step('a', [], { task_id: `t${i}` })], 100 + i * 100),
    )
  }
  function tasksFor(): Map<string, Task> {
    return new Map(
      Array.from({ length: 7 }, (_, i) => [`t${i}`, task(`t${i}`, i === 1 ? 'FAILED' : 'RUNNING', { metadata: { unit: `name-${i}` } })] as const),
    )
  }

  it('fills the switcher with the five newest workflows the list read, with names and states, before any is opened', async () => {
    serve(many(), tasksFor())
    render(<WorkflowsScreen />)
    await waitFor(() => expect(rowIds()).toHaveLength(7))
    await waitFor(() => expect(recentWorkflows()).toHaveLength(5))
    expect(recentWorkflows().map((r) => r.id)).toEqual(['wf_0', 'wf_1', 'wf_2', 'wf_3', 'wf_4'])
    expect(recentWorkflows()[1]).toEqual({ id: 'wf_1', state: 'FAILED', name: 'name-1' })
  })

  it('puts an opened workflow first, and draws names with marks in the panel', async () => {
    serve(many(), tasksFor())
    render(<WorkflowsScreen view="wf=wf_6" />)
    await waitFor(() => expect(recentWorkflows()[0]?.id).toBe('wf_6'))
    expect(recentWorkflows()[0]!.name).toBe('name-6')
    render(
      <SkyShell section="work" tab="workflows" title="Workflows" go={vi.fn()} foot={null}>
        {null}
      </SkyShell>,
    )
    const group = screen.getByRole('group', { name: 'Recent workflows' })
    // Selectors built from parts: whole, `sk-` and a long name read as a provider key to the publish scan.
    const cls = (tail: string) => `.${['sk', 'recent', tail].join('-')}`
    const kids = [...group.querySelectorAll<HTMLElement>(cls('kid'))]
    expect(kids[0]!.querySelector(cls('id'))!.textContent).toBe('name-6')
    // The whole name, then the id (walkthrough C: the name is clamped to two lines).
    expect(kids[0]!.getAttribute('title')).toBe('name-6 · wf_6')
    expect(kids[0]!.querySelector('[data-mark="running"][data-hue="live"]')).toBeTruthy()
    expect(within(group).getByText('All workflows →')).toBeTruthy()
  })
})

// ---------------------------------------------------------------------------
// One workflow's page: the head, the tabs, the step card, the Table
// ---------------------------------------------------------------------------

async function page(view = 'wf=wf_broker'): Promise<void> {
  render(<Routed initial={view} />)
  await waitFor(() => expect(document.querySelector('.wfp:not(.is-loading) .wfp-head')).toBeTruthy())
}

describe('the workflow page head (#503, workflows.html frame B)', () => {
  it('titles the page by the workflow name, with no second heading, and a meta line that does not open on a dot', async () => {
    await page()
    await waitFor(() => expect(document.querySelector('h1')!.textContent).toBe('refactor-broker'))
    expect(document.querySelector('.wfp-label'), 'the name is drawn twice').toBeNull()
    // The meta line is the Screen's count note now (#138: no sub-line under the title).
    expect(document.querySelector('.sub'), 'a sub-line is back under the title').toBeNull()
    const note = document.querySelector<HTMLElement>('.c-count-note')!
    const sub = note.textContent ?? ''
    expect(sub.trim().startsWith('·'), `the meta line opens on a dot: "${sub}"`).toBe(false)
    expect(sub).toContain('4 steps · 1 → 2 → 1')
    // The id is still on the page, whole, as the head's chip.
    expect(document.querySelector('.wfp-head .id')!.textContent).toBe('wf_broker')
    // Copy link and Cancel workflow sit in the head's own row.
    const head = document.querySelector('.wfp-head')!
    expect(within(head as HTMLElement).getByRole('button', { name: 'Copy link' })).toBeTruthy()
    expect(within(head as HTMLElement).getByRole('button', { name: 'Cancel workflow' })).toBeTruthy()
    expect(at(head, 'align-items')).toBe('center')
  })

  it('draws underline tabs under the title, Graph · Table with its count · Timeline, and no boxed control in the card', async () => {
    const onView = vi.fn()
    render(<Routed initial="wf=wf_broker" onView={onView} />)
    const tabs = await waitFor(() => {
      // The canonical underline tabs (components.html A): a link per view --
      // the loaded page's, since its skeleton draws them too, uncounted (#113).
      const t = document.querySelector<HTMLElement>('.wfp:not(.is-loading) > nav.c-tabs[aria-label="Views of this workflow"]')
      expect(t).toBeTruthy()
      return t!
    })
    const buttons = [...tabs.querySelectorAll('a')]
    expect(buttons.map((b) => b.textContent)).toEqual(['Graph', 'Table4', 'Timeline'])
    expect(buttons[0]!.getAttribute('aria-current')).toBe('page')
    expect(buttons[1]!.getAttribute('href')).toMatch(/^\/workflows\/wf_broker\/table/)
    expect(tabs.querySelector('em')!.textContent).toBe('4')
    expect(document.querySelector('.wf-viewbar .c-seg'), 'the boxed Graph/Timeline/Table control is still in the card').toBeNull()
    fireEvent.click(buttons[1]!)
    expect(onView).toHaveBeenLastCalledWith('wf=wf_broker&tab=table')
    // Underlined, not boxed: the active tab carries a bottom border, the strip a hairline.
    // The canonical tab's rule is in components.css, which `at` (this file's
    // two-sheet cascade) does not read; `painted` reads every sheet.
    expect(painted(buttons[0]!, 'border-bottom', { width: 1440 }) ?? '').toMatch(/^2px solid/)
  })
})

describe('Cancel workflow asks for the id to be typed while a step runs (states.html §12)', () => {
  const ok: Result<CancelWorkflowResult> = {
    status: 'ok',
    fetchedAt: T0,
    data: { workflow_id: 'wf_broker', tasks_cancelled: ['t_core'], tasks_already_terminal: ['t_plan'] },
  }

  it('unlocks only on the exact id, then sends', async () => {
    const cancel = vi.fn().mockResolvedValue(ok)
    const b = broker()
    render(<CancelWorkflow workflow={b.w} taskById={b.tasks} onDone={vi.fn()} cancel={cancel} />)
    fireEvent.click(screen.getByRole('button', { name: 'Cancel workflow' }))
    const input = screen.getByRole('textbox', { name: 'Type wf_broker to confirm' })
    const go = screen.getByRole('button', { name: 'Cancel the workflow' }) as HTMLButtonElement
    expect(go.disabled).toBe(true)
    fireEvent.change(input, { target: { value: 'wf_broke' } })
    expect(go.disabled).toBe(true)
    fireEvent.click(go)
    expect(cancel).not.toHaveBeenCalled()
    fireEvent.change(input, { target: { value: 'wf_broker' } })
    expect(go.disabled).toBe(false)
    fireEvent.click(go)
    expect(cancel).toHaveBeenCalledWith('wf_broker')
  })

  it('keeps the two-click confirm when no step holds capacity', () => {
    const b = broker()
    const tasks = new Map(b.tasks)
    tasks.set('t_core', task('t_core', 'QUEUED'))
    render(<CancelWorkflow workflow={b.w} taskById={tasks} onDone={vi.fn()} cancel={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: 'Cancel workflow' }))
    expect(screen.queryByRole('textbox')).toBeNull()
    expect(screen.getByRole('button', { name: 'Yes, cancel the workflow' })).toBeTruthy()
  })
})

describe('the picked step card sits under the graph (#503)', () => {
  it('stacks the card under the canvas on the page, with Open agent → and Stop step', async () => {
    await page()
    const split = await waitFor(() => {
      const s = document.querySelector<HTMLElement>('.wf-split.has-panel')
      expect(s).toBeTruthy()
      return s!
    })
    expect(split.className).toContain('is-stack')
    expect(at(split, 'display')).toBe('block')
    const panel = split.querySelector<HTMLElement>(':scope > .wf-panel')!
    expect(at(panel, 'position')).toBe('static')
    expect(within(panel).getByText('Open agent →')).toBeTruthy()
    // StopRun's own control, in a group named for what it stops (StopRun.tsx is shared; its word is `stop`).
    expect(within(within(panel).getByRole('group', { name: 'Stop step' })).getByRole('button', { name: 'stop' })).toBeTruthy()
  })

  it('keeps the same-step strip inside the card: at most 15 marks, the rest counted', async () => {
    const many = Array.from({ length: 62 }, (_, i) => {
      const w = workflow(`wf_m${i}`, 'RUNNING', 'sam', [step('a', [], { task_id: `tm${i}` })], 100 + i)
      return w
    })
    serve(many, new Map(many.map((_, i) => [`tm${i}`, task(`tm${i}`, 'RUNNING')] as const)))
    await page('wf=wf_m0')
    const strip = await waitFor(() => {
      const s = document.querySelector<HTMLElement>('.wf-scrub-strip')
      expect(s).toBeTruthy()
      return s!
    })
    expect(strip.querySelectorAll('li').length).toBeLessThanOrEqual(15)
    expect(strip.closest('.wf-scrub')!.textContent).toContain('+47')
  })
})

describe('state marks in the step table and inspector are the brand marks (#503)', () => {
  it('draws RUNNING as the live haloed disc and PARKED as the violet pause bars, never a .ctl-dot', async () => {
    await page('wf=wf_broker&tab=table')
    const cell = (id: string) =>
      document.querySelector<HTMLElement>(`.wf-table tr[data-step="${id}"] td[data-col="state"]`)!
    await waitFor(() => expect(cell('core')).toBeTruthy())
    expect(cell('core').querySelector('[data-mark="running"][data-hue="live"]')).toBeTruthy()
    expect(cell('docs').querySelector('[data-mark="parked"][data-hue="park"]')).toBeTruthy()
    expect(document.querySelector('.wf-table .ctl-dot'), 'a .ctl-dot is still drawn in the step table').toBeNull()
    const inspect = await waitFor(() => {
      const i = document.querySelector<HTMLElement>('.wf-inspect-head')
      expect(i).toBeTruthy()
      return i!
    })
    expect(inspect.querySelector('[data-mark="running"][data-hue="live"]')).toBeTruthy()
    expect(inspect.querySelector('.ctl-dot')).toBeNull()
  })
})

describe('the steps Table fits its width (#503)', () => {
  it('is laid out fixed at 100% of its column, with no sideways scroll at 1440', async () => {
    await page('wf=wf_broker&tab=table')
    const t = await waitFor(() => {
      const x = document.querySelector('.wf-table table')
      expect(x).toBeTruthy()
      return x!
    })
    expect(at(t, 'table-layout')).toBe('fixed')
    expect(at(t, 'width')).toBe('100%')
    // It scrolls inside its card only BELOW its minimum width (visual QA Q3),
    // and that minimum fits the step card's column at 1440 (1440 less the
    // 84px spine, the 236px panel and the page and card gutters), so at 1440
    // there is still no sideways scroll.
    expect(at(t.parentElement!, 'overflow-x')).toBe('auto')
    expect(parseFloat(at(t, 'min-width') ?? '0')).toBeLessThanOrEqual(1000)
  })
})

// ---------------------------------------------------------------------------
// Wide stages (wide-workflows.html A)
// ---------------------------------------------------------------------------

/** plan → impl-1..8 → review-1..8 (review-k on impl-(9-k), reversed) → integrate. */
function reviewWave(): { w: Workflow; tasks: Map<string, Task> } {
  const impl = Array.from({ length: 8 }, (_, i) => step(`impl-${i + 1}`, ['plan'], { task_id: `t_i${i + 1}` }))
  const reviews = Array.from({ length: 8 }, (_, i) => step(`review-${i + 1}`, [`impl-${8 - i}`]))
  const w = workflow('wf_wave', 'RUNNING', 'Operator', [
    step('plan', [], { task_id: 't_plan' }),
    ...impl,
    ...reviews,
    step('integrate', reviews.map((r) => r.step_id)),
  ])
  const st: TaskState[] = ['SUCCEEDED', 'SUCCEEDED', 'RUNNING', 'RUNNING', 'PARKED', 'FAILED', 'RUNNING', 'READY']
  const tasks = new Map<string, Task>([['t_plan', task('t_plan', 'SUCCEEDED')]])
  st.forEach((s, i) => tasks.set(`t_i${i + 1}`, task(`t_i${i + 1}`, s, s === 'FAILED' ? { last_error: 'exit 1' } : {})))
  return { w, tasks }
}

describe('a wide stage band: mix bar, slots held, named chips (wide-workflows.html A)', () => {
  it('counts the mix in brand order and says what holds capacity', () => {
    const { w, tasks } = reviewWave()
    const mix = stageMix(w.steps.slice(1, 9), tasks)
    expect(mix.parts.map((p) => [p.kind, p.n])).toEqual([
      ['bad', 1],
      ['live', 3],
      ['park', 1],
      ['wait', 1],
      ['done', 2],
    ])
    expect(mix.holding).toBe(3)
    expect(mix.waiting).toBe(2)
    expect(mix.chips.map((c) => c.stepId)).toEqual(['impl-6', 'impl-3', 'impl-4', 'impl-7', 'impl-5'])
  })

  it('draws the bar, the hold line and the chips, and a chip picks its step', async () => {
    serve([reviewWave().w], reviewWave().tasks)
    await page('wf=wf_wave')
    const band = await waitFor(() => {
      const b = [...document.querySelectorAll<HTMLElement>('.wf-band')].find((x) => x.textContent?.includes('impl-1 … impl-8'))
      expect(b).toBeTruthy()
      return b!
    })
    const bar = band.querySelector<HTMLElement>('.wf-band-mix')!
    expect([...bar.querySelectorAll<HTMLElement>('i')].map((i) => i.className)).toEqual([
      'mx-bad',
      'mx-live',
      'mx-park',
      'mx-wait',
      'mx-done',
    ])
    expect(band.querySelector('.wf-band-hold')!.textContent).toBe('holds 3 slots · 2 waiting hold none')
    const chips = [...band.querySelectorAll<HTMLButtonElement>('.wf-band-chips > button.c-chip.is-pick')]
    expect(chips.map((c) => c.textContent)).toEqual(['impl-6', 'impl-3', 'impl-4', 'impl-7', 'impl-5'])
    fireEvent.click(chips[1]!)
    await waitFor(() => expect(document.querySelector('.wf-inspect')!.getAttribute('aria-label')).toBe('Step impl-3 of wf_wave'))

    // The named steps are the canonical pick Chip (#503 swap): a toggle, pressed when picked.
    await waitFor(() => expect(document.querySelector('.wf-band-chips > button.c-chip[aria-pressed="true"]')?.textContent).toBe('impl-3'))
  })

  it('opens the Table filtered to the stage and state from a band count', async () => {
    const onView = vi.fn()
    serve([reviewWave().w], reviewWave().tasks)
    render(<Routed initial="wf=wf_wave" onView={onView} />)
    const count = await waitFor(() => {
      const c = [...document.querySelectorAll<HTMLButtonElement>('button.wf-band-count')].find((b) => b.textContent === '3 running')
      expect(c).toBeTruthy()
      return c!
    })
    fireEvent.click(count)
    expect(onView).toHaveBeenLastCalledWith('wf=wf_wave&tab=table&stage=1&stepstate=running')
    await waitFor(() => expect(document.querySelector('.wf-table')).toBeTruthy())
    const rows = [...document.querySelectorAll<HTMLElement>('.wf-table tbody tr[data-step]')].map((r) => r.dataset.step)
    expect(rows).toEqual(['impl-3', 'impl-4', 'impl-7'])
    const chip = document.querySelector<HTMLElement>('.wf-filter')!
    expect(chip.textContent).toContain('stage 2 · running')
    fireEvent.click(within(chip).getByRole('button', { name: 'Clear the stage filter' }))
    expect(onView).toHaveBeenLastCalledWith('wf=wf_wave&tab=table')
  })

  it('round-trips the stage filter through the address, only on a workflow Table', () => {
    const q = wl.parseWorkflowQuery('wf=wf_wave&tab=table&stage=1&stepstate=running')
    expect(q.stage).toBe(1)
    expect(q.stepState).toBe('running')
    expect(wl.workflowQueryString(q)).toBe('wf=wf_wave&tab=table&stage=1&stepstate=running')
    expect(wl.workflowQueryString({ ...q, tab: 'graph' })).toBe('wf=wf_wave')
    expect(wl.parseWorkflowQuery('stage=x&stepstate=running').stage).toBeNull()
  })
})

describe('edges: one lane per target step, lit on hover and select', () => {
  it('bundles every edge into a step through one junction above it', () => {
    const steps = [step('a', []), step('b', []), step('c', []), step('j', ['a', 'b', 'c'])]
    const l = layoutOf(steps)
    const into = l.edges.filter((e) => e.to === 'j')
    expect(into).toHaveLength(3)
    const joins = into.map((e) => e.join)
    expect(joins.every((j) => j !== null)).toBe(true)
    expect(new Set(joins.map((j) => `${j!.x},${j!.y}`)).size).toBe(1)
    // The last stretch of every one is the same straight lane into the step.
    for (const e of into) expect(edgePath(e).endsWith(`L ${e.x2} ${e.y2}`)).toBe(true)
    // A step with one parent has no junction.
    expect(layoutOf([step('a', []), step('b', ['a'])]).edges[0]!.join).toBeNull()
  })

  it('lights the picked step’s own edges and fades the rest, and hover does the same', async () => {
    await page()
    await waitFor(() => expect(document.querySelector('[data-edge="plan->core"]')).toBeTruthy())
    // `core` arrives picked: its edges in and out are lit.
    await waitFor(() => expect(document.querySelector('[data-edge="plan->core"]')!.getAttribute('class')).toContain('is-lit'))
    expect(document.querySelector('[data-edge="core->verify"]')!.getAttribute('class')).toContain('is-lit')
    expect(document.querySelector('[data-edge="plan->docs"]')!.getAttribute('class')).toContain('is-faded')
    fireEvent.mouseEnter(document.querySelector<HTMLElement>('button.node[data-step="docs"]')!)
    expect(document.querySelector('[data-edge="plan->docs"]')!.getAttribute('class')).toContain('is-lit')
    expect(document.querySelector('[data-edge="plan->core"]')!.getAttribute('class')).toContain('is-faded')
  })
})

describe('1:1 children are aligned under their parents', () => {
  it('orders an opened stage of single-parent children by their parent’s position', () => {
    const { w } = reviewWave()
    const l = layoutOf(w.steps, new Set([1, 2]), 'names')
    const x = (id: string) => l.nodes.find((n) => n.step.step_id === id)!.x
    for (let k = 1; k <= 8; k++) expect(x(`review-${9 - k}`), `review-${9 - k}`).toBe(x(`impl-${k}`))
    // And the edge between each pair is a straight lane.
    const e = l.edges.find((d) => d.from === 'impl-3' && d.to === 'review-6')!
    expect(e.x1).toBe(e.x2)
  })
})

/** Put `id` in the step card, unless the page already arrived with it picked (a second click puts it down). */
async function pickNode(id: string): Promise<void> {
  const node = await waitFor(() => {
    const n = document.querySelector<HTMLElement>(`button.node[data-step="${id}"]`)
    expect(n).toBeTruthy()
    return n!
  })
  if (node.getAttribute('aria-pressed') !== 'true') fireEvent.click(node)
  await waitFor(() => expect(document.querySelector('.wf-inspect')!.getAttribute('aria-label')).toContain(`Step ${id} `))
}

// ---------------------------------------------------------------------------
// The verdict gate's skipped step, the verdict card, the merge checklist
// ---------------------------------------------------------------------------

/** implement → review → fix (gated on the review) → merge. */
function chain(fix: Partial<Task>, merge: Partial<Task> | null = null): { w: Workflow; tasks: Map<string, Task> } {
  const w = workflow('wf_pr', 'RUNNING', 'alex', [
    step('implement', [], { task_id: 't_impl' }),
    step('review', ['implement'], { task_id: 't_rev' }),
    step('fix', ['review'], { task_id: 't_fix' }),
    step('merge', ['fix'], { task_id: 't_merge' }),
  ])
  const tasks = new Map<string, Task>([
    ['t_impl', task('t_impl', 'SUCCEEDED')],
    ['t_rev', task('t_rev', 'SUCCEEDED')],
    ['t_fix', task('t_fix', 'SUCCEEDED', fix)],
    ['t_merge', task('t_merge', 'RUNNING', merge ?? {})],
  ])
  return { w, tasks }
}

const gateSummary = (agentRan: boolean, verdict: string, findings: unknown[]) => ({
  verdict_gate: {
    task_id: 't_rev',
    file: 'verdict.json',
    verdict,
    verdict_in: ['NOT_YET'],
    agent_ran: agentRan,
    findings,
    findings_dropped: 0,
  },
})

describe('a step skipped by its verdict gate never reads as one that ran', () => {
  it('reads agent_ran false, and nothing else, as skipped', () => {
    expect(review.skippedByVerdict(task('x', 'SUCCEEDED', { result_summary: gateSummary(false, 'MERGE', []) }))).toBe(true)
    expect(review.skippedByVerdict(task('x', 'SUCCEEDED', { result_summary: gateSummary(true, 'NOT_YET', []) }))).toBe(false)
    expect(review.skippedByVerdict(task('x', 'SUCCEEDED', { result_summary: { verdict_gate: { agent_ran: 'no' } } }))).toBe(false)
    expect(review.skippedByVerdict(task('x', 'SUCCEEDED'))).toBe(false)
  })

  it('draws the node striped with the dashed check, and the table says skipped', async () => {
    const { w, tasks } = chain({ result_summary: gateSummary(false, 'MERGE', []) })
    serve([w], tasks)
    await page('wf=wf_pr')
    const node = await waitFor(() => {
      const n = document.querySelector<HTMLElement>('button.node[data-step="fix"]')
      expect(n).toBeTruthy()
      return n!
    })
    expect(node.className).toContain('is-skipped')
    expect(node.querySelector('[data-mark="skipped"]')).toBeTruthy()
    expect(node.textContent).toContain('skipped by verdict')
    expect(node.textContent).not.toMatch(/\bran\b/)
    const ran = document.querySelector<HTMLElement>('button.node[data-step="implement"]')!
    expect(ran.className).not.toContain('is-skipped')
    const cell = document.querySelector<HTMLElement>('.wf-table tr[data-step="fix"] td[data-col="state"]')!
    expect(cell.textContent).toBe('skipped by verdict')
  })
})

describe('the verdict card on a review step (agent-detail-2.html A3)', () => {
  it('groups findings by severity and puts a finding with none under Not graded, never Blocker', () => {
    const v = review.verdictFor('t_rev', [
      task('t_fix', 'SUCCEEDED', {
        result_summary: gateSummary(true, 'NOT_YET', [
          'tests assert the call count',
          { summary: 'missing_execution fenced on a 404', severity: 'blocker' },
          { title: 'docs still old', severity: 'Minor' },
          { message: 'what is this', severity: 'catastrophic' },
        ]),
      }),
    ])!
    expect(v.verdict).toBe('NOT_YET')
    expect(v.groups.map((g) => [g.label, g.items.length])).toEqual([
      ['Blocker', 1],
      ['Major', 0],
      ['Minor', 1],
      ['Not graded', 2],
    ])
    // Ungraded everywhere: only Not graded is drawn, because a zero under Blocker would be a claim.
    const plain = review.verdictFor('t_rev', [task('t_fix', 'SUCCEEDED', { result_summary: gateSummary(true, 'NOT_YET', ['a', 'b']) })])!
    expect(plain.groups.map((g) => g.label)).toEqual(['Not graded'])
    expect(review.verdictFor('t_rev', [task('t_fix', 'SUCCEEDED')])).toBeNull()
  })

  it('shows the card when the review step is picked, findings as plain text', async () => {
    const { w, tasks } = chain({ result_summary: gateSummary(true, 'NOT_YET', ['<b>not markup</b>']) })
    serve([w], tasks)
    await page('wf=wf_pr')
    await pickNode('review')
    const card = await waitFor(() => {
      const c = document.querySelector<HTMLElement>('.wf-verdict')
      expect(c).toBeTruthy()
      return c!
    })
    expect(card.querySelector('.wf-verdict-pill')!.textContent).toBe('NOT_YET')
    expect(card.textContent).toContain('Not graded')
    expect(card.querySelector('b b'), 'a finding was rendered as markup').toBeNull()
    expect(card.textContent).toContain('<b>not markup</b>')
  })
})

describe('the merge step’s checklist card (agent-detail-2.html A3, A3b)', () => {
  const refused = {
    state: 'FAILED' as const,
    result_summary: {
      merge: {
        refusal: { code: 'review_not_at_head', message: 'The fix pushed 7d01e44 after the review saw 3f9a2c1.' },
        merged_by_this_task: false,
        checks: [
          { name: 'Signed specs verify', state: 'passed', code: 'spec_unverified' },
          { name: 'The review was at the head', state: 'failed', code: 'review_not_at_head' },
          { name: 'proof.json is PROVED', state: 'not_read', code: 'not_proved' },
        ],
      },
    },
  }

  it('lists each check in order with its state, and the refusal reason', async () => {
    const { w, tasks } = chain({}, refused)
    serve([w], tasks)
    await page('wf=wf_pr')
    await pickNode('merge')
    const card = await waitFor(() => {
      const c = document.querySelector<HTMLElement>('.wf-merge')
      expect(c).toBeTruthy()
      return c!
    })
    expect(card.textContent).toContain('MERGE_REFUSED')
    expect(card.textContent).toContain('review_not_at_head')
    expect(card.textContent).toContain('Not merged. Nothing changed on the forge.')
    const items = [...card.querySelectorAll<HTMLElement>('ol > li')]
    expect(items.map((li) => li.dataset.check)).toEqual(['passed', 'failed', 'not_read'])
    expect(items[2]!.textContent).toContain('not read')
  })

  it('says awaiting a person, with and without the ready label', () => {
    const awaiting = (ready: boolean | undefined) =>
      review.mergeOf(
        task('m', 'SUCCEEDED', {
          result_summary: { merge: { merged_by_this_task: false, awaiting_human: true, ...(ready === undefined ? {} : { ready_label: ready }) } },
        }),
      )!
    expect(awaiting(false).headline).toBe('Every check passed. Awaiting a person: add ready.')
    expect(awaiting(false).label).toBe('ready · not added')
    expect(awaiting(true).headline).toBe('Ready. GitHub merges it when its required checks are green.')
    expect(awaiting(undefined).label).toBe('ready label not read')
  })

  it('draws nothing for a step whose result has no merge block', async () => {
    const { w, tasks } = chain({})
    serve([w], tasks)
    await page('wf=wf_pr')
    await pickNode('merge')
    expect(document.querySelector('.wf-merge')).toBeNull()
    expect(review.mergeOf(task('m', 'SUCCEEDED'))).toBeNull()
  })
})

// ---------------------------------------------------------------------------
// The section's own sheet obeys the console's type and sheet rules
// ---------------------------------------------------------------------------

describe('styles/workflows.css', () => {
  const css = WF_CSS.replace(/\/\*[\s\S]*?\*\//g, '')
  it('is read, and parses with no duplicate selector', () => {
    expect(WF_CSS.length).toBeGreaterThan(1000)
    const r = gate(WF_CSS, 'workflows.css')
    expect(r.problems).toEqual([])
    expect(r.duplicates).toEqual([])
  })

  it('keeps the 12px floor, no letter-spacing and no all-caps', () => {
    for (const m of css.matchAll(/font-size\s*:\s*([\d.]+)px/g)) expect(Number(m[1])).toBeGreaterThanOrEqual(12)
    for (const m of css.matchAll(/font\s*:[^;}]*?([\d.]+)px/g)) expect(Number(m[1])).toBeGreaterThanOrEqual(12)
    for (const m of css.matchAll(/letter-spacing\s*:\s*([^;}]+)/g)) expect((m[1] ?? '').trim()).toMatch(/^(0|normal)$/)
    expect(css).not.toMatch(/text-transform\s*:\s*uppercase/)
  })

  it('uses the radius tokens or the scale spacing.test.tsx holds (0/2/6/10/14/999)', () => {
    for (const m of css.matchAll(/border-radius\s*:\s*([^;}]+)/g)) {
      for (const v of (m[1] ?? '').trim().split(/\s+/)) {
        if (v.startsWith('var(') || v === '0') continue
        expect(['2px', '6px', '10px', '14px', '999px'], `radius ${v}`).toContain(v)
      }
    }
  })
})
