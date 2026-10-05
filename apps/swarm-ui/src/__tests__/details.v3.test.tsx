/**
 * THE AGENT DETAILS TAB, v3 (agent-details-v3.html A, picked 2026-10-04).
 *
 * Measured live at 1440 on a running claude-code agent: the Details pane was
 * 2,770px tall, the facts scattered, six tiles in an uneven grid (two saying
 * only `written at exit`), platform words (`documents · counter agrees`), a
 * block of ten underlined help links, the whole prompt inline and two nested
 * scroll containers. The pick puts everything needed to judge a running agent
 * in the first ~900px: a 2-line header, a Now card, one stat strip, then
 * Progress | Resources side by side from a 640px pane.
 *
 * jsdom has no layout engine, so "the first 900px" is asserted as the ORDER
 * and WEIGHT of what precedes the two column heads (nothing folded open,
 * no attempt card and no chart above them), and the 640px switch and the one
 * scroll container through the shipped stylesheets' cascade (`painted`).
 *
 * MUTATIONS, one per block: put the id back on its own header line or drop
 * the elapsed headline; lead a failed agent with the Now card, or drop the
 * checkpoint time's honest dash; print a pending cost at full size or as $0;
 * drop the `@container ag-pane (min-width: 640px)` rule; give any element
 * inside the split a second `overflow: auto`; move the Attempts section out
 * of its disclosure. Each turns a case red.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { renderToStaticMarkup } from 'react-dom/server'
import { useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { HELP } from '../help'
import type { AgentRun } from '../api'
import type { Result } from '../fetch'
import type { ResourceClasses } from '../api'
import type { Task, TaskPage } from '../types'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import { attempt, ev, task as runTask } from './runfixture'

const api = vi.hoisted(() => ({
  loadTask: vi.fn(),
  loadChildren: vi.fn(),
  loadAttempts: vi.fn(),
  loadTranscript: vi.fn(),
  loadTaskLogs: vi.fn(),
  loadAgentRun: vi.fn(),
  loadCheckpoints: vi.fn(),
  loadResourceClasses: vi.fn(),
  loadTaskInputOnce: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { AgentSplit } = await import('../AgentSplit')
const { Run } = await import('../AgentDetail')
const { resetListSnap } = await import('../listSnap')

const ID = 'task_0123456789abcdef0123'
const WIDE: CascadeEnv = { width: 1440 }
const MIN = 60_000

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

const CLASSES: ResourceClasses = {
  standard: { name: 'standard', cpu: 2, memory_gib: 8, disk_gib: 10, units: 2 },
} as unknown as ResourceClasses

/** An instant `m` minutes before now. */
function ago(m: number): string {
  return new Date(Date.now() - m * MIN).toISOString()
}

function running(over: Partial<Task> = {}): Task {
  return runTask({
    id: ID,
    state: 'RUNNING',
    created_at: ago(15),
    started_at: ago(14),
    completed_at: null,
    attempt_count: 1,
    max_attempts: 3,
    workflow_id: 'wf_a',
    step_id: 'refactor-backoff',
    ...over,
  })
}

function runOf(over: Partial<AgentRun> = {}): AgentRun {
  return {
    task: running(),
    events: [],
    eventsDetail: null,
    attempts: [attempt(1, { created_at: ago(15), started_at: ago(14), completed_at: null, exit_code: null })],
    attemptsDetail: null,
    classes: CLASSES,
    classesDetail: null,
    classesRouteMissing: false,
    ...over,
  }
}

function host(r: AgentRun): HTMLElement {
  const el = document.createElement('div')
  el.innerHTML = renderToStaticMarkup(<Run run={r} headed />)
  document.body.appendChild(el)
  hosts.push(el)
  return el
}
const hosts: HTMLElement[] = []

/** What a sighted reader sees: the text without a help card's hidden description or its `?`. */
function seen(el: Element | null): string {
  if (el === null) return ''
  const c = el.cloneNode(true) as Element
  for (const n of c.querySelectorAll('[data-help-description], button[aria-expanded], [role="tooltip"]')) n.remove()
  return (c.textContent ?? '').replace(/\s+/g, ' ').trim()
}

type Pane = 'detail' | 'logs' | 'children' | 'attempts' | 'artifacts' | 'checkpoints'
const PANES: readonly Pane[] = ['logs', 'children', 'attempts', 'artifacts', 'checkpoints']

function Routed({ start }: { start: Pane }) {
  const [pane, setPane] = useState<Pane>(start)
  const go = (to: string) => {
    const last = to.split('/').pop() as Pane
    setPane(PANES.includes(last) ? last : 'detail')
  }
  return (
    <div className="app has-inspector">
      <AgentSplit taskId={ID} pane={pane} artifact={null} closeTo="work/running/live" go={go} base={`work/task/${ID}`} />
    </div>
  )
}

/** The split on Details for a RUNNING agent: opened on Attempts, then Details picked. */
async function detailsOfRunning(t: Task, r: AgentRun): Promise<HTMLElement> {
  api.loadTask.mockResolvedValue(ok(t))
  api.loadAgentRun.mockResolvedValue(ok(r))
  render(<Routed start="attempts" />)
  await waitFor(() => expect(document.querySelector('.ag-head-facts')).not.toBeNull())
  const list = screen.getByRole('tablist', { name: 'Agent panes' })
  const details = within(list)
    .getAllByRole('tab')
    .find((b) => b.querySelector('.c-tab-label')?.textContent === 'Details')!
  fireEvent.click(details)
  return waitFor(() => {
    const d = document.querySelector<HTMLElement>('.ag-split .dt')
    expect(d).not.toBeNull()
    return d!
  })
}

beforeEach(() => {
  localStorage.clear()
  resetListSnap()
  api.loadAgentRun.mockReturnValue(new Promise(() => {}))
  api.loadAttempts.mockResolvedValue(ok({ attempts: [] }))
  api.loadTranscript.mockReturnValue(new Promise(() => {}))
  api.loadTaskLogs.mockReturnValue(new Promise(() => {}))
  api.loadCheckpoints.mockReturnValue(new Promise(() => {}))
  api.loadTaskInputOnce.mockReturnValue(new Promise(() => {}))
  api.loadResourceClasses.mockResolvedValue(ok({ resource_classes: CLASSES }))
  api.loadChildren.mockResolvedValue(ok({ tasks: [] } satisfies TaskPage))
})

afterEach(() => {
  for (const h of hosts.splice(0)) h.remove()
  delete document.documentElement.dataset.agentList
})

describe('the header is two lines: state, name and the elapsed headline; then one meta line', () => {
  it('says elapsed · attempt n of N beside the title and keeps the id behind a copy button', async () => {
    const t = running({ dispatch: { strategy: 'integrate', carrier: 'checkpoints', role: 'contributor', integrates: [] } })
    api.loadTask.mockResolvedValue(ok(t))
    render(<Routed start="attempts" />)
    const head = await waitFor(() => {
      const h = document.querySelector<HTMLElement>('.ag-head')
      expect(h?.querySelector('.ag-head-facts')).toBeTruthy()
      return h!
    })
    const lines = [...head.children].filter((c) => !c.classList.contains('ag-parent'))
    expect(lines.map((l) => l.className.split(' ')[0])).toEqual(['ag-head-row', 'ag-head-id', 'ag-head-facts'])
    const row = head.querySelector('.ag-head-row')!
    expect(row.querySelector('.ag-head-title')?.textContent).toBe('refactor-backoff')
    expect(row.querySelector('.ag-head-actions'), 'the actions share line 1').not.toBeNull()
    const hl = row.querySelector('.ag-head-hl')!
    expect(hl.textContent).toMatch(/^1[34]m \d+s · attempt 1 of 3$/)
    expect(hl.querySelector('b')?.textContent).toMatch(/^1[34]m \d+s$/)
    // The id is printed once, on its own line under the name, with its copy (#94).
    expect(head.querySelector('.ag-head-id .tid-text')?.textContent).toBe(ID)
    expect(head.querySelector('.ag-head-facts')!.textContent).not.toContain(ID)
    const copy = head.querySelector<HTMLButtonElement>('.ag-head-id .tid-copy')!
    expect(copy.title).toContain(ID)
    // Plain-language dispatch chips, in STRATEGY_LABEL's words.
    const chips = [...head.querySelectorAll('.ag-head-dchip')].map((c) => seen(c))
    expect(chips).toEqual(['One PR for all steps', 'adds to the shared PR'])
    // Tenant, gen and units left the header.
    expect(head.textContent).not.toMatch(/tenant|gen 1|\b2u\b/)
    expect(head.querySelector('.ag-head-facts')!.textContent).toContain('2 vCPU · 8 GiB')
  })
})

describe('the Now card leads, and becomes the outcome or the failure', () => {
  it('a running agent: phase, heartbeat and the latest checkpoint time from its event', () => {
    const a = attempt(1, { created_at: ago(15), started_at: ago(14), completed_at: null, exit_code: null, checkpoints: ['c1'] })
    const el = host(
      runOf({
        attempts: [a],
        events: [ev('lease_acquired', ago(15), 'att_1'), ev('running', ago(14), 'att_1'), ev('checkpoint_completed', ago(1), 'att_1')],
      }),
    )
    const now = el.querySelector<HTMLElement>('.dt > .dt-now')!
    expect(now.dataset.lead).toBe('now')
    expect(now.querySelector('.dt-card-head > b')?.textContent).toBe('Now')
    expect(now.querySelector('.dt-phase.is-cur')?.textContent).toMatch(/^run/)
    expect(now.textContent).toMatch(/Checkpoints\s*1 · last 1m ago/)
    expect(now.textContent).toMatch(/Heartbeat/)
    // Its one '?' explains what it shows -- checkpoints -- not capacity.
    const help = [...now.querySelectorAll('.dt-card-head button[aria-label^="Help: "]')].map((b) => b.getAttribute('aria-label'))
    expect(help).toEqual([`Help: ${HELP.checkpoints.title}`])
  })

  it('a checkpoint whose event is not in the window says so with a dash, never a time', () => {
    const a = attempt(1, { created_at: ago(15), started_at: ago(14), completed_at: null, exit_code: null, checkpoints: ['c1', 'c2'] })
    const el = host(runOf({ attempts: [a], events: [ev('running', ago(14), 'att_1')] }))
    const now = el.querySelector<HTMLElement>('.dt-now')!
    expect(now.textContent).toMatch(/Checkpoints\s*2 · — time not on this page/)
  })

  it('a finished agent leads with its Outcome', () => {
    const t = running({
      state: 'SUCCEEDED',
      completed_at: ago(1),
      result_summary: {
        git: { branch: 'swarm/x', pull_request: { number: 512, url: 'https://example.invalid/pr/512' }, published: true },
        artifacts: [],
      },
    })
    const el = host(runOf({ task: t, attempts: [attempt(1, { created_at: ago(15), started_at: ago(14), completed_at: ago(1) })] }))
    const lead = el.querySelector<HTMLElement>('.dt > .dt-now')!
    expect(lead.dataset.lead).toBe('outcome')
    expect(lead.querySelector('.dt-card-head > b')?.textContent).toBe('Outcome')
    expect(lead.textContent).toMatch(/Pull request\s*#512\s*opened/)
  })

  it('a failed agent leads with the failure: a plain sentence, its writer and the whole error', () => {
    const error = 'reconciled: lease expired with no heartbeat for 10m; generation 3 fenced'
    const t = running({ state: 'FAILED', completed_at: ago(1), end_cause: 'lost_worker', last_error: error, attempt_count: 3 })
    const el = host(runOf({ task: t, attempts: [attempt(1, { created_at: ago(15), started_at: ago(14), completed_at: null, exit_code: null })] }))
    const lead = el.querySelector<HTMLElement>('.dt > .dt-now')!
    expect(lead.dataset.lead).toBe('failed')
    expect(lead.querySelector('.dt-card-head > b')?.textContent).toBe('Failed: the worker stopped answering')
    expect(lead.querySelector('.dt-chip')?.textContent).toBe('lost_worker')
    expect(lead.textContent).toMatch(/Error written by\s*reconciler/)
    expect(lead.querySelector('pre')?.textContent).toBe(error)
    // The lead precedes the strip.
    const kids = [...el.querySelector('.dt')!.children]
    expect(kids.indexOf(lead)).toBeLessThan(kids.indexOf(el.querySelector('.dt-strip')!))
  })

  it('a cancelled agent with no error leads with the neutral Cancelled card, not an Outcome', () => {
    const t = running({ state: 'CANCELLED', completed_at: ago(1), started_at: null, end_cause: null, last_error: null, attempt_count: 0 })
    const el = host(runOf({ task: t, attempts: [] }))
    const lead = el.querySelector<HTMLElement>('.dt > .dt-now')!
    expect(lead.dataset.lead).toBe('failed')
    expect(lead.classList.contains('is-neu'), 'a cancel reads as a failure').toBe(true)
    expect(lead.querySelector('.dt-card-head > b')?.textContent).toBe('Cancelled')
    expect(lead.textContent).toContain('No error text was written on the task.')
  })
})

describe('one stat strip: Elapsed · Attempt · Peak memory · CPU · Cost', () => {
  function cells(el: HTMLElement) {
    return [...el.querySelectorAll<HTMLElement>('.dt-strip > .dt-sc')].map((c) => ({
      label: seen(c.querySelector('.dt-sc-l')),
      value: seen(c.querySelector('.dt-sc-v')),
      sub: seen(c.querySelector('.dt-sc-s')),
      cls: c.className,
    }))
  }

  it('a running agent: memory with % of its limit, CPU against its cores, cost small and pending', () => {
    const a = attempt(1, {
      created_at: ago(15),
      started_at: ago(14),
      completed_at: null,
      exit_code: null,
      peak_cpu_cores: 1.6,
      mean_cpu_cores: 0.7,
      cpu_limit_cores: 2,
      cpu_seconds: 600,
    })
    const beat = ev('heartbeat', ago(1), 'att_1', { peak_rss_bytes: 2 * 1024 ** 3 })
    const c = cells(host(runOf({ attempts: [a], events: [beat] })))
    expect(c.map((x) => x.label)).toEqual(['Elapsed', 'Attempt', 'Peak memory', 'CPU', 'Cost'])
    expect(c[0]!.value).toMatch(/^1[34]m \d+s$/)
    expect(c[1]!.value).toBe('1 of 3')
    expect(c[2]!.value).toBe('2.00 GiB 25%')
    expect(c[2]!.sub).toMatch(/^of 8 GiB · so far · 1m ago$/)
    expect(c[3]!.value).toBe('1.6 / 2 cores')
    expect(c[3]!.sub).toBe('peak · mean 0.7')
    // NOT DUE YET: small, muted, words -- never a figure.
    expect(c[4]!.value).toBe('at exit')
    expect(c[4]!.cls).toContain('is-reading')
    expect(c[4]!.sub).toMatch(/tokens/)
  })

  it('an unknown figure is a dash with its reason, never 0, and wears the muted form', () => {
    const t = running({ state: 'SUCCEEDED', completed_at: ago(1) })
    const a = attempt(1, { created_at: ago(15), started_at: ago(14), completed_at: ago(1) })
    const c = cells(host(runOf({ task: t, attempts: [a] })))
    const cost = c.find((x) => x.label === 'Cost')!
    expect(cost.value).toBe('— not reported')
    expect(cost.cls).toContain('is-absent')
    expect(cost.sub).toMatch(/tokens not reported/)
    const mem = c.find((x) => x.label === 'Peak memory')!
    expect(mem.value).toBe('— not recorded')
    expect(mem.cls).toContain('is-absent')
  })

  it('a failed attempt read leaves the task’s own figures and dashes the rest', () => {
    const c = cells(host(runOf({ attempts: null, attemptsDetail: 'HTTP 503.' })))
    expect(c.find((x) => x.label === 'Attempt')!.value).toBe('1 of 3')
    for (const label of ['Peak memory', 'CPU', 'Cost']) {
      const cell = c.find((x) => x.label === label)!
      expect(cell.value, label).toBe('— read failed')
      expect(cell.cls, label).toContain('is-unread')
    }
  })
})

describe('Progress | Resources: two columns from a 640px pane, one below', () => {
  it('switches on the pane’s width through the shipped sheet', () => {
    const el = host(runOf())
    const cols = el.querySelector<HTMLElement>('.dt-cols')!
    const two = painted(cols, 'grid-template-columns', { width: 1440, container: 700 })
    expect(two?.trim().split(/\s+(?![^(]*\))/)).toHaveLength(2)
    const one = painted(cols, 'grid-template-columns', { width: 1440, container: 600 })
    expect(one?.trim().split(/\s+(?![^(]*\))/) ?? ['auto']).toHaveLength(1)
  })

  it('on a phone the strip moves above the Now card', () => {
    const el = host(runOf())
    const strip = el.querySelector<HTMLElement>('.dt-strip')!
    expect(Number(painted(strip, 'order', { width: 390, container: 380 }) ?? '0')).toBeLessThan(0)
    expect(Number(painted(strip, 'order', { width: 1440, container: 700 }) ?? '0')).toBe(0)
  })

  it('below 640px the strip sticks to the top of the pane; at 640 and over it scrolls with the rest', () => {
    const el = host(runOf())
    const strip = el.querySelector<HTMLElement>('.dt-strip')!
    for (const container of [380, 600]) {
      expect(painted(strip, 'position', { width: 1440, container }), `the strip does not stick in a ${container}px pane`).toBe('sticky')
      expect(painted(strip, 'top', { width: 1440, container })).toBe('0')
    }
    expect(painted(strip, 'position', { width: 1440, container: 700 }) ?? 'static').toBe('static')
  })
})

describe('the detail has ONE scroll container', () => {
  it('nothing inside the split scrolls but its pane', async () => {
    const t = running()
    const el = await detailsOfRunning(t, runOf({ task: t }))
    const split = el.closest<HTMLElement>('.ag-split')!
    const scrollers = [split, ...split.querySelectorAll<HTMLElement>('*')].filter((n) => {
      const v = painted(n, ['overflow-y', 'overflow'], WIDE) ?? 'visible'
      return /\b(auto|scroll)\b/.test(v)
    })
    // The tab strip scrolls sideways only (Q4); it is not a vertical scroller.
    const vertical = scrollers.filter((n) => !n.classList.contains('c-tabs'))
    expect(vertical.map((n) => n.className)).toEqual([expect.stringContaining('ag-split-pane')])
    // And no inline style makes one either (the prompt's own 320px box).
    const inline = [...split.querySelectorAll<HTMLElement>('[style]')].filter((n) =>
      /auto|scroll/.test(`${n.style.overflow} ${n.style.overflowY}`),
    )
    expect(inline).toHaveLength(0)
  })
})

describe('the first 900px: Now, the strip and both column heads, with nothing heavy above them', () => {
  it('keeps the order and folds the long sections at 1440', () => {
    const el = host(runOf())
    const dt = el.querySelector<HTMLElement>('.dt')!
    const order = ['.dt-now', '.dt-strip', '.dt-progress .dt-card-head', '.dt-resources .dt-card-head', '.dt-input']
    const at = order.map((s) => {
      const n = dt.querySelector(s)
      expect(n, s).not.toBeNull()
      return n!
    })
    for (let i = 1; i < at.length; i++) {
      expect(at[i - 1]!.compareDocumentPosition(at[i]!) & Node.DOCUMENT_POSITION_FOLLOWING, order[i]).toBeTruthy()
    }
    // Nothing is folded open on arrival.
    expect(dt.querySelectorAll('details[open]')).toHaveLength(0)
    // Every attempt card, chart and the full timeline is behind a disclosure.
    for (const heavy of dt.querySelectorAll('.att-card, figure.ctl-chart, .ctl-chart-wrap, .dt-evs-all, pre.json')) {
      expect(heavy.closest('details'), heavy.className.toString()).not.toBeNull()
    }
    // The help-link block is gone: one `?` per card instead.
    expect(dt.querySelector('.att-legend')).toBeNull()
    for (const card of dt.querySelectorAll('.dt-card')) {
      expect(card.querySelectorAll(':scope > .dt-card-head [aria-expanded]').length, card.className).toBeLessThanOrEqual(1)
    }
  })
})
