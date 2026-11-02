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
 * put Progress back beside Resources in a column grid (DETAILS-BUILDING folded
 * Resources under one row); give any element
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

// ONE FROZEN INSTANT FOR THE WHOLE FILE: every fixture (`ago`, `ok`'s
// fetchedAt) and every clock the panes read. Faking Date in one test was not
// enough (PR 1184, runs 38552512519 and the one after): `useNow` keeps one
// clock per cadence in module state, and a `renderToStaticMarkup` render
// creates that clock without subscribing, so nothing ever deletes it. The
// static render of an EARLIER test left a clock at its own instant; a later
// test's fixtures were built from a later Date.now(), so the strip read
// `59s ago` for a heartbeat built `ago(1)`, and under a per-test frozen
// instant Elapsed read `0s`. With Date frozen here, before any fixture or
// render, every clock this file creates holds the same instant. Only Date is
// faked: waitFor and the panes' intervals keep real timers.
const FROZEN = new Date('2026-10-14T12:00:00Z')

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(FROZEN)
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
  vi.useRealTimers()
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
    // TWO LINES (VQ-010, BGD's visual QA 2026-10-26): the id is on the meta line.
    expect(lines.map((l) => l.className.split(' ')[0])).toEqual(['ag-head-row', 'ag-head-facts'])
    const row = head.querySelector('.ag-head-row')!
    expect(row.querySelector('.ag-head-title')?.textContent).toBe('refactor-backoff')
    expect(row.querySelector('.ag-head-actions'), 'the actions share line 1').not.toBeNull()
    const hl = row.querySelector('.ag-head-hl')!
    expect(hl.textContent).toMatch(/^1[34]m \d+s · attempt 1 of 3$/)
    expect(hl.querySelector('b')?.textContent).toMatch(/^1[34]m \d+s$/)
    // The id is printed once, whole, with its copy (#94), on the meta line (VQ-010).
    expect(head.querySelectorAll('.tid-text')).toHaveLength(1)
    expect(head.querySelector('.ag-head-facts .ag-head-id .tid-text')?.textContent).toBe(ID)
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
    expect(now.querySelector('.dt-card-head > h2')?.textContent).toBe('Now')
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
    expect(lead.querySelector('.dt-card-head > h2')?.textContent).toBe('Outcome')
    expect(lead.textContent).toMatch(/Pull request\s*#512\s*opened/)
  })

  it('a failed agent leads with the failure: a plain sentence, its writer and the whole error', () => {
    const error = 'reconciled: lease expired with no heartbeat for 10m; generation 3 fenced'
    const t = running({ state: 'FAILED', completed_at: ago(1), end_cause: 'lost_worker', last_error: error, attempt_count: 3 })
    const el = host(runOf({ task: t, attempts: [attempt(1, { created_at: ago(15), started_at: ago(14), completed_at: null, exit_code: null })] }))
    const lead = el.querySelector<HTMLElement>('.dt > .dt-now')!
    expect(lead.dataset.lead).toBe('failed')
    expect(lead.querySelector('.dt-card-head > h2')?.textContent).toBe('Failed: the worker stopped answering')
    expect(lead.querySelector('.dt-chip')?.textContent).toBe('lost_worker')
    expect(lead.textContent).toMatch(/Error written by\s*reconciler/)
    expect(lead.querySelector('pre')?.textContent).toBe(error)
    // The lead precedes the figures: the Resources row, the strip folded under it (DETAILS-BUILDING).
    const kids = [...el.querySelector('.dt')!.children]
    const row = el.querySelector('.dt > details.dt-res')!
    expect(row.querySelector('.dt-strip')).not.toBeNull()
    expect(kids.indexOf(lead)).toBeLessThan(kids.indexOf(row))
  })

  it.each([
    ['OOMKilled, exit 137', 'reconciled: ran out of memory: the 8 GiB limit was reached (OOMKilled, exit 137); lease silent for 132s'],
    ['stalled', 'reconciled: ran out of memory: the 8 GiB limit was reached (stalled at the limit; its lease went silent); lease silent for 102s'],
  ])('a worker lost to its memory limit says it ran out of memory, not that it stopped answering (#1926, %s)', (_, error) => {
    const t = running({ state: 'FAILED', completed_at: ago(1), end_cause: 'lost_worker', last_error: error, attempt_count: 1 })
    const el = host(runOf({ task: t, attempts: [attempt(1, { created_at: ago(15), started_at: ago(14), completed_at: null, exit_code: null })] }))
    const lead = el.querySelector<HTMLElement>('.dt > .dt-now')!
    // MUTATION: drop the out-of-memory reading in `failureSay` -> the heading
    // reads "Failed: the worker stopped answering", which is what hid #1926.
    expect(lead.querySelector('.dt-card-head > h2')?.textContent).toBe('Failed: it ran out of memory at its limit')
    // The frozen end cause is still shown verbatim beside it.
    expect(lead.querySelector('.dt-chip')?.textContent).toBe('lost_worker')
    expect(lead.querySelector('pre')?.textContent).toBe(error)
  })

  it('an agent the worker stopped on its budget says so in the Budget card\'s words (contract request 118)', () => {
    const error = 'budget_exceeded: the agent used $1.02 of a budget of $1.00 and was stopped at a turn boundary'
    const t = running({ state: 'FAILED', completed_at: ago(1), end_cause: 'budget_exceeded', last_error: error, attempt_count: 1 })
    const el = host(runOf({ task: t, attempts: [attempt(1, { created_at: ago(15), started_at: ago(14), completed_at: ago(1) })] }))
    const lead = el.querySelector<HTMLElement>('.dt > .dt-now')!
    expect(lead.dataset.lead).toBe('failed')
    // MUTATION: put back "it spent its budget" -> one end in two wordings,
    // beside the Budget card's "stopped at its budget".
    expect(lead.querySelector('.dt-card-head > h2')?.textContent).toBe('Failed: it was stopped at its budget')
    expect(lead.querySelector('.dt-chip')?.textContent).toBe('budget_exceeded')
  })

  it('a fix loop that ended without a merge says so, not as a crash (contract request 126)', () => {
    const error = 'fix_loop_exhausted: the person answered stop after round 3'
    const t = running({ state: 'FAILED', completed_at: ago(1), end_cause: 'fix_loop_exhausted', last_error: error, attempt_count: 1 })
    const el = host(runOf({ task: t, attempts: [attempt(1, { created_at: ago(15), started_at: ago(14), completed_at: ago(1) })] }))
    const lead = el.querySelector<HTMLElement>('.dt > .dt-now')!
    expect(lead.dataset.lead).toBe('failed')
    // MUTATION: drop `fix_loop_exhausted` from END_CAUSE_SAY -> the head reads
    // "Failed: undefined", or tsc refuses the Record.
    expect(lead.querySelector('.dt-card-head > h2')?.textContent).toBe('Failed: its fix loop ended without a merge')
    expect(lead.querySelector('.dt-chip')?.textContent).toBe('fix_loop_exhausted')
  })

  it('a cancelled agent with no error leads with the neutral Cancelled card, not an Outcome', () => {
    const t = running({ state: 'CANCELLED', completed_at: ago(1), started_at: null, end_cause: null, last_error: null, attempt_count: 0 })
    const el = host(runOf({ task: t, attempts: [] }))
    const lead = el.querySelector<HTMLElement>('.dt > .dt-now')!
    expect(lead.dataset.lead).toBe('failed')
    expect(lead.classList.contains('is-neu'), 'a cancel reads as a failure').toBe(true)
    expect(lead.querySelector('.dt-card-head > h2')?.textContent).toBe('Cancelled')
    expect(lead.textContent).toContain('No error text was written on the agent.')
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
    expect(c.map((x) => x.label)).toEqual(['Run · attempt 1', 'Attempt', 'Peak memory', 'CPU', 'Cost'])
    expect(c[0]!.value).toMatch(/^1[34]m \d+s$/)
    expect(c[1]!.value).toBe('1 of 3')
    expect(c[2]!.value).toBe('2.00 GiB 25%')
    expect(c[2]!.sub).toMatch(/^of 8 GiB · so far · 1m ago$/)
    expect(c[3]!.value).toBe('1.6 / 2 cores')
    // The limit is the class's own, so its name says so (FF-D5a, run 2).
    // MUTATION: drop the limit's words from the CPU tile's sub-line.
    expect(c[3]!.sub).toBe('peak · mean 0.7 · standard limit')
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

  // FF-D5a (visual QA run 2, #1680): the header said `4 vCPU`, the tile `1.6
  // / 2 cores` and the summary `mean 2.1 of 4 cores` -- the class's size, the
  // limit the worker read and the series' limit, with no word for which was
  // which. The tile and the Resources rows now say whose limit their
  // denominator is, and the class's size beside it when the two differ.
  it('CPU names its denominator: the limit’s source, and the class’s size when it differs', () => {
    const classes = { standard: { name: 'standard', cpu: 4, memory_gib: 8, disk_gib: 10, units: 2 } } as unknown as ResourceClasses
    const a = attempt(1, {
      created_at: ago(15),
      started_at: ago(14),
      completed_at: null,
      exit_code: null,
      peak_cpu_cores: 1.6,
      mean_cpu_cores: 0.7,
      cpu_limit_cores: 2,
      cpu_limit_source: 'cgroup',
      cpu_seconds: 600,
    })
    const el = host(runOf({ attempts: [a], classes }))
    const cpu = cells(el).find((x) => x.label === 'CPU')!
    // MUTATION: draw `/ ${cls.cpu} cores`; the denominator is the limit the worker read.
    expect(cpu.value).toBe('1.6 / 2 cores')
    // MUTATION: drop `(class 4 vCPU)` when the limit and the class differ.
    expect(cpu.sub).toBe('peak · mean 0.7 · cgroup limit (class 4 vCPU)')
    const note = (name: string) =>
      seen(
        [...el.querySelectorAll<HTMLElement>('.dt-resources > .dt-rr')]
          .find((r) => seen(r.querySelector('.dt-rr-n')) === name)!
          .querySelector('.dt-rr-v'),
      )
    // MUTATION: leave the Resources row's note at `of 2 cores`.
    expect(note('CPU peak')).toBe('1.6 80% of 2 cores · cgroup limit (class 4 vCPU)')
    // MUTATION: repeat the limit's words on the mean row too; once per card is enough.
    // The mean row names its own source instead (FF-D5a, QAFIX5-TASK-DETAIL).
    expect(note('CPU mean')).toBe('0.7 35% of 2 cores · CPU-seconds over runner time')
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

describe('Progress takes the pane; Resources is folded under its row (DETAILS-BUILDING)', () => {
  // The two columns are gone (BGD 2026-10-27): the Resources card moved under
  // the one Resources row, so Progress stands alone at every pane width.
  it('draws Progress outside the disclosure and the Resources card inside it, with no column grid left', () => {
    const el = host(runOf())
    const progress = el.querySelector<HTMLElement>('.dt > .dt-progress')
    expect(progress, 'Progress is not a card of its own on the pane').not.toBeNull()
    expect(progress!.closest('details')).toBeNull()
    expect(el.querySelector('.dt-resources')!.closest('details.dt-res')).not.toBeNull()
    expect(el.querySelector('.dt-cols')).toBeNull()
  })

  // c08 (owner decision 2026-10-22, box comment 6314120811): one order at
  // every width. A phone moved the strip above the Now card; it no longer does.
  it('on a phone the strip stays below the Now card, as at 1440', () => {
    const el = host(runOf())
    const strip = el.querySelector<HTMLElement>('.dt-strip')!
    // MUTATION: put `.dt > .dt-strip { order: -1 }` back in details.css's phone block.
    expect(Number(painted(strip, 'order', { width: 390, container: 380 }) ?? '0')).toBe(0)
    expect(Number(painted(strip, 'order', { width: 1440, container: 700 }) ?? '0')).toBe(0)
  })

  it('from 480 to 639px the strip sticks to the top of the pane; at 640 and over, and under 480, it scrolls with the rest', () => {
    const el = host(runOf())
    const strip = el.querySelector<HTMLElement>('.dt-strip')!
    for (const container of [480, 600]) {
      expect(painted(strip, 'position', { width: 1440, container }), `the strip does not stick in a ${container}px pane`).toBe('sticky')
      expect(painted(strip, 'top', { width: 1440, container })).toBe('0')
    }
    expect(painted(strip, 'position', { width: 1440, container: 700 }) ?? 'static').toBe('static')
    // A phone-narrow pane (G2-01, QA 2026-10-07): stuck there, the two-column
    // strip left about 150px of a 390x844 screen to scroll.
    expect(painted(strip, 'position', { width: 1440, container: 380 }) ?? 'static').toBe('static')
  })

  // AGENT-HEAD-B (BGD's pick AH1 B, 2026-10-28, part of #2596): in the split
  // the COLUMN scrolls, so the strip's sticky inset is measured from the
  // column's content edge, under the stuck tab strip. At `top: 0` it stuck
  // 18px down, behind the tabs; it sits at the tabs' foot, the strip's height
  // as measured (`--ag-tabs-h`) less the column's 18px top padding.
  it('in the split, the stuck strip sits under the stuck tab strip, not under or over it', () => {
    const el = document.createElement('div')
    el.innerHTML = `<div class="drawer ctl-drawer ag-split"><div class="ag-split-pane">${renderToStaticMarkup(<Run run={runOf()} headed />)}</div></div>`
    document.body.appendChild(el)
    hosts.push(el)
    const strip = el.querySelector<HTMLElement>('.dt-strip')!
    const column = el.querySelector<HTMLElement>('.ag-split')!
    const tabsEdge = document.createElement('div')
    tabsEdge.className = 'ag-tabs-edge'
    column.prepend(tabsEdge)
    for (const env of [{ width: 1440, height: 900, container: 600 }, { width: 900, height: 900, container: 480 }]) {
      expect(painted(strip, 'position', env)).toBe('sticky')
      // MUTATION: drop the split's offset in details.css and the strip sticks at `0`, under the tabs.
      expect(painted(strip, 'top', env), `the strip hides under the tabs at ${env.width}`).toBe('calc(57px - 18px)')
      // Under the tab strip's layer, so a strip never paints over the tabs while it slides into place.
      expect(Number(painted(strip, 'z-index', env))).toBeLessThan(Number(painted(tabsEdge, 'z-index', env)))
    }
    // Outside the split the strip sticks to its own scroller's top, as before.
    expect(painted(host(runOf()).querySelector('.dt-strip')!, 'top', { width: 1440, container: 600 })).toBe('0')
  })
})

describe('the detail has ONE scroll container', () => {
  it('nothing in the split scrolls but its column', async () => {
    const t = running()
    const el = await detailsOfRunning(t, runOf({ task: t }))
    const split = el.closest<HTMLElement>('.ag-split')!
    const scrollers = [split, ...split.querySelectorAll<HTMLElement>('*')].filter((n) => {
      const v = painted(n, ['overflow-y', 'overflow'], WIDE) ?? 'visible'
      return /\b(auto|scroll)\b/.test(v)
    })
    // The tab strip scrolls sideways only (Q4); it is not a vertical scroller.
    const vertical = scrollers.filter((n) => !n.classList.contains('c-tabs'))
    // The COLUMN, not its pane (AGENT-HEAD-B, BGD's pick AH1 B, 2026-10-28):
    // the head scrolls away with the detail and the tab strip sticks.
    expect(vertical, 'a scroller other than the column').toEqual([split])
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
    // DETAILS-BUILDING: Building, Now, the one Resources row (the strip and
    // the Resources card folded inside it), then Progress and Input.
    const order = ['.dt-building', '.dt-now', 'details.dt-res > summary', '.dt-strip', '.dt-resources .dt-card-head', '.dt-progress .dt-card-head', '.dt-input']
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
