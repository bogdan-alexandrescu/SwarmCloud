/**
 * THE LAST FOUR CUTS OF VISUAL QA PASS 1 ON WORK › WORKFLOWS (#2391 comment
 * 58, lane workflows-cuts, 2026-10-27). Each was text the screen cut where a
 * reader needed it, the whole of it only in a title:
 *
 *  ID     the list cut each row's id at a different width at 1440
 *         (`wf_aud…`, `wf_b…`, 0px): a shrink factor of 100 with no floor.
 *  WHY    with the runners drawn under the name, the current step kept
 *         ~135px beside them: 'failed at checkpoints: e…'.
 *  NOTE   the graph card of a step parked on DEPENDENCY_INCOMPLETE cut its
 *         fixed sentence to 'Waiting on an earlier…' on its one note line.
 *  SHAPE  the same-step row's 'shape: …' was cut beside the ◀ position ▶ nav
 *         at 390 (scrollWidth 317 > clientWidth 300).
 */
import { cleanup, render, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useState } from 'react'

import type { Result } from '../fetch'
import type { WorkflowBoard, WorkflowUsage } from '../api'
import type { Task, TaskState, Workflow, WorkflowStep } from '../types'
import { reasonCopy } from '../types'
import type { CascadeEnv } from './cssgate'
import { build, painted } from './marks'
import { NODE_CHROME_W, monoW, nodeWidthAt } from '../dag'
import { ownFromBoard } from './workflowsById'
import { ShapeWords, shapeWords } from '../WorkflowViews'

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

const { WorkflowsScreen, cardNote } = await import('../Workflows')

const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }

const T0 = Date.parse('2026-10-27T01:00:00.000Z')
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
    completed_at: state === 'SUCCEEDED' ? iso(-400) : null,
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

/** A running chain: `plan` done, `build` running, `report` parked behind it on DEPENDENCY_INCOMPLETE. */
const chain: Workflow = {
  workflow_id: 'wf_audit7c52762aa41b9e0d3f',
  state: 'RUNNING',
  tenant_id: 'eng',
  stored_state: 'RUNNING',
  state_source: 'derived',
  rollup: { state: 'RUNNING', complete: true, reason: 'test', counts: {}, unreadable_steps: [], unstarted_steps: [], steps_read: 3 },
  created_at: iso(-600),
  updated_at: iso(-60),
  submitted_by: 'priya@example.com',
  priority: 0,
  on_step_failure: 'fail_workflow',
  cancel_requested: false,
  steps: [
    step('plan', [], { task_id: 'task_plan' }),
    step('build', ['plan'], { task_id: 'task_build' }),
    step('report', ['build'], { task_id: 'task_report' }),
  ],
}
const unit = { unit: 'Audit the operator surface' }
const chainTasks = new Map<string, Task>([
  ['task_plan', task('task_plan', 'SUCCEEDED', { metadata: unit })],
  ['task_build', task('task_build', 'RUNNING', { metadata: unit })],
  ['task_report', task('task_report', 'PARKED', { started_at: null, park_reason: 'DEPENDENCY_INCOMPLETE', metadata: unit })],
])

function serve(workflows: Workflow[], taskById: Map<string, Task>): void {
  api.loadWorkflowBoard.mockResolvedValue({
    status: 'ok',
    data: { workflows, taskById, statesDetail: null, nextPageToken: null },
    fetchedAt: T0,
  } satisfies Result<WorkflowBoard>)
}

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

const hosts: HTMLElement[] = []

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
  for (const h of hosts.splice(0)) h.remove()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

/** A `<n>ch` length as a number of characters, or NaN for anything else. */
const chOf = (v: string | null) => {
  const m = /^([\d.]+)ch$/.exec(v ?? '')
  return m === null ? Number.NaN : Number(m[1])
}

describe('ID: the list draws every row’s id under its name (visual QA pass 1 overview-agents-61, #2596)', () => {
  /** A list row's name cell: a name of `name`'s length on its line, then the id under it. */
  function nameWith(name: string, id: string): HTMLElement {
    const host = document.createElement('div')
    host.className = 'wfl'
    host.innerHTML = `<table><tbody><tr><td class="wfl-name"><span class="wfl-line"><a>${name}</a></span><span class="id">${id}</span></td></tr></tbody></table>`
    document.body.appendChild(host)
    hosts.push(host)
    return host.querySelector<HTMLElement>('.wfl-name > .id')!
  }

  // MUTATION: put the id back on the name's line with `flex: 0 100 auto;
  // max-width: 10ch`: every 45-character title is cut again at 1440, and this
  // rule no longer matches the id at all.
  it('draws the id as its own line, cut with an ellipsis and never wrapped, under names of every length', () => {
    const ids = [
      nameWith('Audit the operator surface for every tenant', 'wf_audit7c52762aa41b9e0d3f'),
      nameWith('Build', 'wf_b81f02c9e4d7a3b6150c'),
    ]
    for (const id of ids) {
      expect(painted(id, 'display', WIDE)).toBe('block')
      expect(painted(id, 'white-space', WIDE)).toBe('nowrap')
      expect(painted(id, 'text-overflow', WIDE)).toBe('ellipsis')
      // No 10ch cap: the id is cut by its cell, not at `wf_` and six characters.
      expect(chOf(painted(id, 'max-width', WIDE))).toBeNaN()
    }
  })

  // MUTATION: draw the id inside `.wfl-line` again: `mixGoesUnder` counts its
  // width and a 420px line sends the runners under a name it no longer shares.
  it('leaves the id out of the name line’s sum, so the runners stay beside a name that has the line', async () => {
    viewport(1440)
    vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockImplementation(function (this: HTMLElement) {
      return this.classList.contains('wfl-line') ? 420 : 0
    })
    render(<WorkflowsScreen />)
    const td = await waitFor(() => {
      const t = document.querySelector<HTMLElement>(`.wfl-table tr[data-workflow="${chain.workflow_id}"] td[data-col="workflow"]`)
      expect(t).toBeTruthy()
      return t!
    })
    expect(td.querySelector(':scope > .wfl-line > .id')).toBeNull()
    expect(td.querySelector(':scope > .id')?.getAttribute('title')).toBe(chain.workflow_id)
    // 420 less the mix's gap (8) and the mix (176) leaves the name 236px: over 200.
    await waitFor(() => expect(td.querySelector(':scope > .wfl-line > .wfl-mix'), 'the runners went under a 236px name').not.toBeNull())
  })
})

describe('WHY: with the runners under the name, the current step has a line of its own', () => {
  // MUTATION: put back `.wfl-sub { flex-wrap: nowrap }` and the step's
  // `flex: 1 1 auto`: beside the mix's 11rem it keeps ~135px at 1440.
  it('wraps the second line so the step takes the whole width under the runners, cut with its title', () => {
    const why = build('div.wfl span.wfl-name span.wfl-sub span.wfl-why', hosts)
    const sub = why.parentElement!
    const mix = document.createElement('span')
    mix.className = 'wfl-mix'
    sub.insertBefore(mix, why)
    expect(painted(sub, 'display', WIDE)).toBe('flex')
    expect(painted(sub, 'flex-wrap', WIDE), 'the step shares a line with the runners').toBe('wrap')
    const [, , basis] = (painted(why, 'flex', WIDE) ?? '').split(/\s+/)
    expect(basis, 'the step does not take a line of its own').toBe('100%')
    // The runners keep their one whole chip on the line above it.
    expect((painted(mix, 'flex', WIDE) ?? '').split(/\s+/).slice(0, 2)).toEqual(['0', '0'])
    // Still one line, cut, the whole text in the title.
    expect(painted(why, 'white-space', WIDE)).toBe('nowrap')
    expect(painted(why, 'text-overflow', WIDE)).toBe('ellipsis')
  })

  // The control: the phone card reads the step whole, as before.
  it('leaves the phone card’s step uncut at 390', () => {
    const under = build('div.wfl span.wfl-name span.wfl-sub span.wfl-why', hosts)
    const alone = build('div.wfl span.wfl-name span.wfl-why', hosts)
    for (const why of [under, alone]) {
      expect(painted(why, 'white-space', PHONE) ?? 'normal').toBe('normal')
      expect(painted(why, 'text-overflow', PHONE) ?? 'clip').toBe('clip')
    }
  })
})

describe('NOTE: a step waiting on its parents says so in a note that fits the card', () => {
  /** The content box of the NARROWEST card this workflow draws: the names tier's, which every tier is at least as wide as. */
  const narrowest = (steps: readonly WorkflowStep[]) => nodeWidthAt('names', steps) - NODE_CHROME_W
  const parked = (text: string) => ({ kind: 'why' as const, text, full: text, warn: false })

  // MUTATION: draw `reasonCopy('DEPENDENCY_INCOMPLETE')` on the note line as
  // it is: 43 characters cut to 'Waiting on an earlier…' at 1440. Or restore
  // the first fix's 'waits on an earlier step', which fits the full tier's
  // NODE_W box and was cut at the names tier Auto zoom picked (review of U02).
  it('draws the short form on the card, the whole sentence in its title', async () => {
    viewport(1440)
    render(<Routed initial={`wf=${chain.workflow_id}`} />)
    const note = await waitFor(() => {
      const n = document.querySelector<HTMLElement>('button.node[data-step="report"] .node-note')
      expect(n).toBeTruthy()
      return n!
    })
    const sentence = reasonCopy('DEPENDENCY_INCOMPLETE')
    expect(sentence).toBe('Waiting on an earlier step in its workflow.')
    const text = note.textContent ?? ''
    // Measured as the layout measures a node's text (mono is the wider face), against the narrowest card's content box.
    expect(monoW(text.length, 12), `'${text}' is wider than the names-tier card`).toBeLessThanOrEqual(narrowest(chain.steps))
    // `waiting on build` is 16 characters, wider than this workflow's names-tier box: the short form stands in.
    expect(text).toBe('awaits parent')
    expect(note.getAttribute('title')).toBe(sentence)
    // Still one line, so `layoutOf`'s NOTE_H holds.
    expect(painted(note, '-webkit-line-clamp', WIDE)).toBe('1')
  })

  it('names the parent, as a not-started dependant does, when that fits the names-tier card', () => {
    // A long id widens every card at the names tier; the short parent's name then fits.
    const notes = step('publish-release-notes', ['plan', 'build'])
    const steps = [step('plan', [], { task_id: 't_plan' }), step('build', ['plan'], { task_id: 't_build' }), notes]
    const tasks = new Map<string, Task>([
      ['t_plan', task('t_plan', 'SUCCEEDED')],
      ['t_build', task('t_build', 'RUNNING')],
    ])
    const note = cardNote(parked(reasonCopy('DEPENDENCY_INCOMPLETE')), notes, steps, tasks)
    expect(note.text).toBe('waiting on build')
    expect(monoW(note.text.length, 12)).toBeLessThanOrEqual(narrowest(steps))
  })

  it('fits the narrowest card any workflow can draw when the names do not fit', () => {
    // Short ids: the names-tier box is the state row's alone, its floor, and
    // `waiting on plan, build` (22) and `waiting on build` (16) overrun it.
    const report = step('report', ['plan', 'build'])
    const lone = step('report', ['build'])
    const steps = [step('plan'), step('build'), report]
    const one = [step('build'), lone]
    const many = cardNote(parked(reasonCopy('DEPENDENCY_INCOMPLETE')), report, steps, null)
    const single = cardNote(parked(reasonCopy('DEPENDENCY_INCOMPLETE')), lone, one, null)
    expect([many.text, single.text]).toEqual(['awaits parents', 'awaits parent'])
    expect(monoW(many.text.length, 12)).toBeLessThanOrEqual(narrowest(steps))
    // Any other note is left as it is.
    const other = parked('blocked: plan failed')
    expect(cardNote(other, report, steps, null)).toBe(other)
  })
})

describe('SHAPE: the same-step row’s shape drops to its own line on a phone', () => {
  // MUTATION: drop the phone rule: at 390 the shape stays beside the nav,
  // nowrap, and 'shape: 2 steps in 2 stages (1 → 1)' is cut.
  it('lets the shape wrap on a line of its own at 390, and keeps the one cut line at 1440', () => {
    const shape = build('div.wf-scrub span.wf-scrub-shape', hosts)
    const row = shape.parentElement!
    const nav = document.createElement('span')
    nav.className = 'wf-scrub-nav'
    row.insertBefore(nav, shape)
    expect(painted(row, 'flex-wrap', PHONE)).toBe('wrap')
    expect(painted(shape, 'white-space', PHONE), 'the shape is cut at 390').toBe('normal')
    expect(painted(shape, ['flex', 'flex-basis'], PHONE)).toBe('1 1 100%')
    // The control: at 1440 the shape stays beside the nav, one line, cut with its title.
    expect(painted(shape, 'white-space', WIDE)).toBe('nowrap')
    expect(painted(shape, 'text-overflow', WIDE)).toBe('ellipsis')
    expect(painted(shape, ['flex', 'flex-basis'], WIDE)).toBe('0 1 auto')
    expect(painted(nav, 'white-space', PHONE)).toBe('nowrap')
  })

  // MUTATION: drop `order: 1` or the parenthetical's inline-block: the strip's
  // ◉ drops to a line of its own, and the shape breaks as '(1 → 2 → 1' / '→ 1)'.
  it('keeps the strip beside the nav and moves the parenthetical down whole at 390 (review of U02)', () => {
    const paren = build('div.wf-scrub span.wf-scrub-shape span.wf-scrub-paren', hosts)
    const shape = paren.parentElement!
    expect(painted(shape, 'order', PHONE)).toBe('1')
    expect(painted(paren, 'display', PHONE)).toBe('inline-block')
    expect(painted(paren, 'max-width', PHONE)).toBe('100%')
    // The control: at 1440 the one cut line is plain inline text, ellipsed as before.
    expect(painted(paren, 'display', WIDE) ?? 'inline').toBe('inline')
    expect(painted(shape, 'order', WIDE) ?? '0').toBe('0')
  })

  it('renders the shape’s words unchanged, the parenthetical in its own span', () => {
    const words = shapeWords({ steps: 4, levels: 4, text: '1 → 2 → 1 → 1' })
    render(
      <span className="wf-scrub-shape">
        shape: <ShapeWords words={words} />
      </span>,
    )
    const shape = document.querySelector<HTMLElement>('.wf-scrub-shape')
    expect(shape?.textContent).toBe('shape: 4 steps in 4 stages (1 → 2 → 1 → 1)')
    expect(shape?.querySelector('.wf-scrub-paren')?.textContent).toBe('(1 → 2 → 1 → 1)')
  })
})
