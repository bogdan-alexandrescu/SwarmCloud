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
import { NODE_CHROME_W, NODE_W, monoW } from '../dag'
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

describe('ID: the list cuts every row’s id at one width', () => {
  /** A list row's name line: a name of `name`'s length, then the id. */
  function lineWith(name: string, id: string): HTMLElement {
    const host = document.createElement('div')
    host.className = 'wfl'
    host.innerHTML = `<table><tbody><tr><td class="wfl-name"><span class="wfl-line"><a>${name}</a><span class="id">${id}</span></span></td></tr></tbody></table>`
    document.body.appendChild(host)
    hosts.push(host)
    return host.querySelector<HTMLElement>('.wfl-line > .id')!
  }

  // MUTATION: put back `flex: 0 100 auto; min-width: 0`: the id collapses to
  // 0px under a long name, and each name length cuts it somewhere else.
  it('floors and caps the id at the same width, of `wf_` and six characters, under names of every length', () => {
    const ids = [
      lineWith('Audit the operator surface for every tenant', 'wf_audit7c52762aa41b9e0d3f'),
      lineWith('Build', 'wf_b81f02c9e4d7a3b6150c'),
      lineWith('Rotate the platform’s merge App key and verify the release', 'wf_0d9e3f41ac77b2e9d8f1'),
    ]
    const mins = ids.map((id) => painted(id, 'min-width', WIDE))
    const maxes = ids.map((id) => painted(id, 'max-width', WIDE))
    for (const m of mins) expect(chOf(m), `the id's floor is ${m}`).toBeGreaterThan(0)
    expect(new Set(mins).size, 'rows floor the id at different widths').toBe(1)
    // A floor alone lets a short name hand the id more room: the cap makes the cut one width.
    expect(maxes, 'the id is not capped at its floor').toEqual(mins)
    // `wf_` and six characters, then the ellipsis.
    expect(chOf(mins[0]!)).toBe(10)
    expect(painted(ids[0]!, 'text-overflow', WIDE)).toBe('ellipsis')
    expect(painted(ids[0]!, 'white-space', WIDE)).toBe('nowrap')
  })

  // MUTATION: leave `mixGoesUnder` counting the id as a gap only: a 420px
  // line keeps the runners beside a name of 420 - 72 - 16 - 176 = 156px.
  it('counts the id’s width when it decides whether the runners go under the name', async () => {
    viewport(1440)
    const idW = monoW(10, 12)
    vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockImplementation(function (this: HTMLElement) {
      return this.classList.contains('wfl-line') ? 420 : 0
    })
    vi.spyOn(HTMLElement.prototype, 'offsetWidth', 'get').mockImplementation(function (this: HTMLElement) {
      return this.classList.contains('id') ? idW : 0
    })
    render(<WorkflowsScreen />)
    const td = await waitFor(() => {
      const t = document.querySelector<HTMLElement>(`.wfl-table tr[data-workflow="${chain.workflow_id}"] td[data-col="workflow"]`)
      expect(t).toBeTruthy()
      return t!
    })
    expect(td.querySelector(':scope > .wfl-line > .id')?.getAttribute('title')).toBe(chain.workflow_id)
    // 420 less the id (72), its gap and the mix's (16) and the mix (176) leaves the name 156px: under 200.
    expect(420 - idW - 16 - 176).toBeLessThan(200)
    await waitFor(() => expect(td.querySelector(':scope > .wfl-sub > .wfl-mix'), 'the runners stayed beside a 156px name').not.toBeNull())
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
  // MUTATION: draw `reasonCopy('DEPENDENCY_INCOMPLETE')` on the note line as
  // it is: 43 characters cut to 'Waiting on an earlier…' at 1440.
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
    // Measured as the layout measures a node's text (mono is the wider face), against the card's content box.
    expect(monoW(text.length, 12), `'${text}' is wider than the card`).toBeLessThanOrEqual(NODE_W - NODE_CHROME_W)
    expect(text).toBe('waits on an earlier step')
    expect(note.getAttribute('title')).toBe(sentence)
    // Still one line, so `layoutOf`'s NOTE_H holds.
    expect(painted(note, '-webkit-line-clamp', WIDE)).toBe('1')
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
})
