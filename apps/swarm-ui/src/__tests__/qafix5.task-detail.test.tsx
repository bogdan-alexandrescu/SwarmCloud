/**
 * AN AGENT'S DETAILS PANE AND ITS ATTEMPTS TAB (QAFIX5-TASK-DETAIL, part of
 * #1680). Visual QA run 5b (wf_025ae735511546d99eff, 2026-10-22, the fixture
 * build of main at 58cd0c1e, 1440 and 390 px, both themes) found or still saw:
 *
 *   5b:QA-008  (comment 6343756607) at 390 the Resources card clipped the
 *              Memory row after `22s ago · worst` and squeezed the Memory and
 *              CPU peak bars to nothing: the value column was `auto` and
 *              `nowrap`, so it took the row.
 *   r2:FF-D5a  (comment 6298220484) Resources' `CPU mean 0.8` and the
 *              over-time summary's `mean 2.8` are two sources -- the
 *              attempt's CPU-seconds over its runner time, and the mean of the
 *              5 s samples -- and neither said which it was (c05).
 *   r3:task-007 (comment 6314119814) Attempts said attempt 3's peak memory
 *              was `not measured` while Details read 1.72 GiB off its
 *              heartbeat (c06).
 *   QA-113     (comment 6283738266) `3 attempts · 8 events` over `attempts ?
 *              3`; a clipped `partial`; `model: not measured` unexplained; at
 *              390 a long key/value list and `gen` / `2` split over two lines.
 *   QA-115     (comment 6283738952) the Failed card's `no end cause recorded
 *              · the error below is what it wrote` read as no cause, and at
 *              390 its last log line was cut to `resul…`.
 */
import { act, render, waitFor, within } from '@testing-library/react'
import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { AgentRun, ResourceClasses } from '../api'
import type { Result } from '../fetch'
import type { AttemptRow, Task, TaskEvent } from '../types'
import type { UtilisationRead } from '../utilisation'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import { at, attempt, ev, task as runTask } from './runfixture'

const api = vi.hoisted(() => ({
  loadAttempts: vi.fn(),
  loadAgentDetail: vi.fn(),
  loadUtilisation: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  // Every read this file does not answer never answers, so nothing it does
  // not name can land and change what is asserted.
  const quiet = Object.fromEntries(
    Object.entries(actual)
      .filter(([k, v]) => k.startsWith('load') && typeof v === 'function')
      .map(([k]) => [k, () => new Promise(() => {})]),
  )
  return { ...actual, ...quiet, ...api, USE_FIXTURES: false }
})

const { Run } = await import('../AgentDetail')
const { AttemptTimelineScreen } = await import('../AttemptTimeline')
const { UtilisationPanel } = await import('../UtilisationPanel')

const ID = 'task_0123456789abcdef0125'
const GIB = 1024 ** 3
const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

const CLASSES = {
  standard: { name: 'standard', cpu: 2, memory_gib: 8, disk_gib: 10, units: 2 },
} as unknown as ResourceClasses

function running(over: Partial<Task> = {}): Task {
  return runTask({
    id: ID,
    state: 'RUNNING',
    runner_profile: 'claude-code',
    resource_class: 'standard',
    started_at: at(30),
    completed_at: null,
    attempt_count: 3,
    max_attempts: 3,
    current_generation: 3,
    // Attempt 3 holds the lease, so it is open (`attemptEnd`).
    current_lease_id: 'lse_3',
    ...over,
  })
}

/** The fixture build's shape: 2 attempts ended, attempt 2 the worst, attempt 3 with no written peak. */
function threeAttempts(ended: boolean): AttemptRow[] {
  return [
    attempt(1, { task_id: ID, created_at: at(0), started_at: at(1), completed_at: at(10), exit_code: 1, peak_rss_bytes: 2 * GIB }),
    attempt(2, { task_id: ID, created_at: at(11), started_at: at(12), completed_at: at(28), exit_code: 1, peak_rss_bytes: 7.2 * GIB }),
    attempt(3, {
      task_id: ID,
      created_at: at(29),
      started_at: at(30),
      completed_at: ended ? at(40) : null,
      exit_code: ended ? 1 : null,
      peak_cpu_cores: 1.6,
      mean_cpu_cores: 0.8,
      cpu_limit_cores: 2,
      cpu_limit_source: 'cgroup',
      cpu_seconds: 600,
    }),
  ]
}

/** Attempt 3's heartbeat: its high-water mark so far, 1.72 GiB. */
function beat(): TaskEvent {
  return { ...ev('heartbeat', at(35), 'att_3', { peak_rss_bytes: 1.72 * GIB }), task_id: ID, generation: 3 }
}

function runOf(task: Task, attempts: AttemptRow[], events: TaskEvent[] = []): AgentRun {
  return {
    task,
    events,
    eventsDetail: null,
    attempts,
    attemptsDetail: null,
    classes: CLASSES,
    classesDetail: null,
    classesRouteMissing: false,
  }
}

const hosts: HTMLElement[] = []
function host(r: AgentRun, lastLine?: JSX.Element): HTMLElement {
  const el = document.createElement('div')
  el.innerHTML = renderToStaticMarkup(<Run run={r} headed lastLine={lastLine} />)
  document.body.appendChild(el)
  hosts.push(el)
  return el
}

function resourcesRow(el: HTMLElement, name: string): HTMLElement {
  const row = [...el.querySelectorAll<HTMLElement>('.dt-resources > .dt-rr')].find(
    (r) => r.querySelector('.dt-rr-n')?.textContent === name,
  )
  expect(row, `no Resources row ${name}`).toBeDefined()
  return row!
}

async function attemptsTab(task: Task, rows: AttemptRow[], events: TaskEvent[]): Promise<HTMLElement> {
  api.loadAttempts.mockResolvedValue(ok({ attempts: rows }))
  api.loadAgentDetail.mockResolvedValue(ok({ task, events, eventsDetail: null }))
  const { container } = render(<AttemptTimelineScreen taskId={ID} />)
  await waitFor(() => expect(container.querySelector('.ctl-card-foot')).not.toBeNull())
  return container as HTMLElement
}

function card(el: HTMLElement, ordinal: number): HTMLElement {
  const c = [...el.querySelectorAll<HTMLElement>('.att-card')].find((x) =>
    (x.querySelector('.ctl-card-title')?.textContent ?? '').startsWith(`Attempt ${ordinal} `),
  )
  expect(c, `no card for attempt ${ordinal}`).toBeDefined()
  return c!
}

function fact(c: HTMLElement, key: string): HTMLElement {
  const f = [...c.querySelectorAll<HTMLElement>('.ctl-fact')].find((x) => x.querySelector(':scope > b')?.textContent === key)
  expect(f, `no ${key} fact`).toBeDefined()
  return f!
}

/** A track list split on its top-level spaces: `76px minmax(48px, 1fr) auto` is three. */
function tracks(cols: string | null): string[] {
  expect(cols).not.toBeNull()
  return cols!.split(/\s+(?![^(]*\))/)
}

beforeEach(() => {
  api.loadAttempts.mockReturnValue(new Promise(() => {}))
  api.loadAgentDetail.mockReturnValue(new Promise(() => {}))
  api.loadUtilisation.mockReturnValue(new Promise(() => {}))
})

afterEach(() => {
  for (const h of hosts.splice(0)) h.remove()
  vi.clearAllMocks()
})

// ---------------------------------------------------------------------------
// 5b:QA-008: the Resources card at 390
// ---------------------------------------------------------------------------

describe('5b:QA-008: every Resources row keeps its bar at 390, and its value wraps inside the card', () => {
  it('gives the bar a track that cannot collapse and the value one that can shrink', () => {
    const el = host(runOf(running(), threeAttempts(false), [beat()]))
    const row = resourcesRow(el, 'Memory')
    for (const env of [PHONE, WIDE]) {
      const [, bar, value] = tracks(painted(row, 'grid-template-columns', env))
      // MUTATION: put `minmax(0, 1fr)` back as the bar's track -- at 390 the bar is squeezed to 0 px.
      expect(bar, 'the bar track').toMatch(/^minmax\([1-9]\d*px, 1fr\)$/)
      // MUTATION: put `auto` back as the value's track -- the value takes the row and the card clips it.
      expect(value, 'the value track').toMatch(/^minmax\(0(px)?, /)
    }
  })

  it('lets the value wrap rather than run out of the card', () => {
    const el = host(runOf(running(), threeAttempts(false), [beat()]))
    const v = resourcesRow(el, 'Memory').querySelector<HTMLElement>('.dt-rr-v')!
    // The row this was seen on: the newest attempt's 1.72 GiB, and the worst named after it.
    expect(v.textContent).toMatch(/^1\.72 GiB \d+% of 8 GiB · so far · .* · worst 7\.20 GiB, attempt 2$/)
    // MUTATION: put `white-space: nowrap` back on `.dt-rr-v` -- the note is clipped after `worst`.
    expect(painted(v, 'white-space', PHONE)).not.toBe('nowrap')
    // MUTATION: drop `overflow-wrap: anywhere` -- an unbroken figure still runs past the card's edge.
    expect(painted(v, 'overflow-wrap', PHONE)).toBe('anywhere')
  })
})

// ---------------------------------------------------------------------------
// r2:FF-D5a: two CPU means, each named by its source (c05)
// ---------------------------------------------------------------------------

describe('r2:FF-D5a: each CPU mean on Details says which mean it is', () => {
  it('names Resources’ mean as the attempt’s CPU-seconds over its runner time', () => {
    const el = host(runOf(running(), threeAttempts(false), [beat()]))
    const v = resourcesRow(el, 'CPU mean').querySelector('.dt-rr-v')?.textContent
    // MUTATION: drop the source words from the CPU mean row -- `0.8 40% of 2 cores` beside the summary's other mean.
    expect(v).toBe('0.8 40% of 2 cores · CPU-seconds over runner time')
  })

  it('names the over-time summary’s mean as the mean of its 5 s samples, and its limit as the cgroup’s', async () => {
    const p = (i: number, cpu: number) => ({ at: at(i / 12), cpu_cores: cpu, memory_bytes: 2 * GIB, throttled_share: 0 })
    const data: UtilisationRead = {
      task_id: ID,
      read_at: at(1),
      attempts: [
        {
          attempt_id: 'att_3',
          generation: 3,
          cpu_limit_cores: 2,
          memory_limit_bytes: 8 * GIB,
          throttled_seconds: 0,
          peak_throttled_share: 0,
          mean_throttled_share: 0,
          points: [p(0, 1), p(1, 1), p(2, 1)],
        },
      ],
    }
    api.loadUtilisation.mockResolvedValue(ok(data))
    const { container } = render(<UtilisationPanel taskId={ID} live={false} />)
    await act(async () => {})
    const head = container.querySelector('[data-testid="util-headline"]')?.textContent ?? ''
    // QA1 overview-agents-24/25: the title prints no mean at all, so the pane
    // has one mean on its glass (Resources'); the samples' is the chart caption.
    // MUTATION: leave the summary at `CPU: mean 1 of 2 cores` -- two means on one pane.
    expect(head).not.toMatch(/mean/)
    expect(container.querySelector('[data-testid="util-summary"]')?.textContent).toContain('mean 1 of 2 cores')
  })
})

// ---------------------------------------------------------------------------
// r3:task-007: Attempts reads the figure Details reads (c06)
// ---------------------------------------------------------------------------

describe('r3:task-007: the Attempts tab’s peak memory agrees with Details', () => {
  it('reads a running attempt’s heartbeat high-water mark, as Details does, never `not measured`', async () => {
    const t = running()
    const rows = threeAttempts(false)
    const details = host(runOf(t, rows, [beat()]))
    // What Details reads for attempt 3 (c06): its own 1.72 GiB, so far.
    expect(resourcesRow(details, 'Memory').querySelector('.dt-rr-v > b')?.textContent).toBe('1.72 GiB')
    const el = await attemptsTab(t, rows, [beat()])
    const f = fact(card(el, 3), 'peak memory')
    // MUTATION: read only `peak_rss_bytes` in the Attempts card -- attempt 3 says `not measured` beside Details' 1.72 GiB.
    expect(f.textContent).toMatch(/^peak memory1\.72 GiB · so far · heartbeat /)
    expect(f.textContent).not.toMatch(/not measured/)
    // MUTATION: draw the heartbeat's figure in the absent tone -- a reading is not an absence.
    expect(f.classList.contains('is-absent')).toBe(false)
  })

  it('calls an ended attempt’s heartbeat figure its last reading, never a peak', async () => {
    const t = running({ state: 'FAILED', completed_at: at(40) })
    const rows = threeAttempts(true)
    const details = host(runOf(t, rows, [beat()]))
    expect(resourcesRow(details, 'Memory').querySelector('.dt-rr-v')?.textContent).toContain('last reading')
    const el = await attemptsTab(t, rows, [beat()])
    // MUTATION: say `so far` of an attempt that is over -- it tells the reader to wait for a write that will not come.
    expect(fact(card(el, 3), 'peak memory').textContent).toMatch(/^peak memory1\.72 GiB · last reading · heartbeat /)
    // A written peak is still the peak, unqualified.
    expect(fact(card(el, 2), 'peak memory').textContent).toBe('peak memory7.20 GiB')
  })
})

// ---------------------------------------------------------------------------
// QA-113: the Attempts tab
// ---------------------------------------------------------------------------

describe('QA-113: the Attempts tab counts once, wraps rather than clips, and explains its absences', () => {
  it('counts the attempts once, in the summary, and labels the toolbar by what it is about', async () => {
    const el = await attemptsTab(running(), threeAttempts(false), [beat()])
    const toolbar = el.querySelector<HTMLElement>('.ctl-toolbar')!
    // MUTATION: put the bare `<Count n={attempts}>` back on the toolbar -- `attempts ? 3` under `3 attempts · 1 event`.
    expect(toolbar.querySelector('.c-chip')).toBeNull()
    expect(toolbar.textContent).not.toMatch(/\b3\b/)
    // MUTATION: leave the eyebrow `attempts` -- the toolbar is the event page's, and its `?` opens event paging.
    expect(toolbar.querySelector('.ctl-eyebrow')?.firstChild?.textContent).toBe('event history')
    // The event count is the toolbar's alone since QA2-FIX-13 (overview-agents-41): the summary counts attempts.
    expect(within(el).getByText('3 attempts')).toBeTruthy()
  })

  it('wraps the toolbar’s tally instead of clipping its `partial` mark', async () => {
    const el = await attemptsTab(running(), threeAttempts(false), [beat()])
    const note = el.querySelector<HTMLElement>('.ctl-toolbar > .ctl-card-note')!
    expect(note.querySelector('.ctl-mark.is-partial')).not.toBeNull()
    for (const env of [WIDE, PHONE]) {
      // MUTATION: drop the `.att-pane .ctl-toolbar > .ctl-card-note` rule -- `nowrap` and `flex: none` clip the mark.
      expect(painted(note, 'white-space', env)).toBe('normal')
      expect(painted(note, ['flex-shrink', 'flex'], env)).toMatch(/^1 1 /)
      expect(painted(note, 'min-width', env)).toBe('0')
    }
  })

  it('says why no model is recorded, in words, not `not measured`', async () => {
    const rows = threeAttempts(false).map((a) => ({ ...a, model: null }))
    const el = await attemptsTab(running(), rows, [beat()])
    const f = fact(card(el, 3), 'model')
    // MUTATION: put `<Mark kind="absent">` back -- a model is not a measurement, and `not measured` explains nothing.
    expect(f.textContent).not.toMatch(/not measured/)
    expect(f.querySelector('.ctl-mark.is-absent')?.textContent).toBe('not recorded')
    // MUTATION: leave the reason in the mark's accessible name only -- the glass shows two words and no why.
    // The reason that fits attempt 3, the newest of a claude-code task (6:task-003, qafix6.taskdetail).
    expect(f.querySelector('.att-model-why')?.textContent).toBe('the worker did not write it')
    expect(f.classList.contains('is-absent')).toBe(true)
  })

  it('at 390 keeps the heading whole and the facts compact', async () => {
    const el = await attemptsTab(running(), threeAttempts(false), [beat()])
    const c = card(el, 2)
    const label = c.querySelector<HTMLElement>('.ctl-card-title > .att-label')!
    expect(label.textContent).toBe('Attempt 2 · gen 2')
    // MUTATION: drop `white-space: nowrap` from `.att-label` -- `Attempt 2 · gen` / `2` on two lines.
    expect(painted(label, 'white-space', PHONE)).toBe('nowrap')
    // MUTATION: drop `flex-wrap: wrap` from the attempt card's head -- the note keeps squeezing the title.
    expect(painted(c.querySelector<HTMLElement>('.ctl-card-head')!, 'flex-wrap', PHONE)).toBe('wrap')
    const facts = c.querySelector<HTMLElement>('.ctl-card-body > .ctl-facts')!
    // MUTATION: drop the phone grid -- every fact is its own line again, eight lines a card.
    expect(painted(facts, 'display', PHONE)).toBe('grid')
    expect(painted(facts, 'grid-template-columns', PHONE)).toBe('repeat(3, minmax(0, 1fr))')
    const short = [...facts.querySelectorAll<HTMLElement>('.ctl-fact.att-fact-short')].map((f) => f.querySelector('b')?.textContent)
    // MUTATION: drop `att-fact-short` from ran, exit or checkpoints -- the short figures each take a line.
    expect(short).toEqual(['ran', 'exit', 'checkpoints'])
    for (const f of facts.querySelectorAll<HTMLElement>('.ctl-fact.att-fact-short')) {
      expect(painted(f, ['grid-column', 'grid-column-start'], PHONE)).toBe('auto')
    }
    // An id takes the whole row: it does not fit a third of one.
    const id = fact(c, 'lease')
    // MUTATION: let an id fact take one grid cell -- `lse_…` wraps letter by letter.
    expect(painted(id, ['grid-column', 'grid-column-start'], PHONE)).toBe('1 / -1')
  })
})

// ---------------------------------------------------------------------------
// QA-115: the Failed card
// ---------------------------------------------------------------------------

describe('QA-115: the Failed card leads with where its cause is, and its last log line wraps', () => {
  function failedRun(over: Partial<Task> = {}): AgentRun {
    return runOf(
      running({ state: 'FAILED', completed_at: at(40), last_error: 'exit status 1', end_cause: null, ...over } as Partial<Task>),
      threeAttempts(true),
    )
  }

  it('says the error below is why it ended before it says no end-cause code was written', () => {
    const el = host(failedRun())
    const c = el.querySelector<HTMLElement>('.dt-card[data-lead="failed"]')!
    const absent = c.querySelector<HTMLElement>('.dt-nfs > .dt-nf.is-absent')!
    // MUTATION: restore `no end cause recorded · the error below is what it wrote` -- it opens on "no cause".
    expect(absent.textContent).toBe('why it ended: the error below · no end-cause code was written')
    expect(c.querySelector('pre.dt-err')?.textContent).toBe('exit status 1')
  })

  it('still says only that nothing was recorded when there is no error either', () => {
    const el = host(failedRun({ last_error: null }))
    const c = el.querySelector<HTMLElement>('.dt-card[data-lead="failed"]')!
    // MUTATION: point at "the error below" when there is none.
    expect(c.querySelector('.dt-nfs > .dt-nf.is-absent')?.textContent).toBe('no end cause recorded')
  })

  it('wraps the last log line inside the Failed card at 390 instead of cutting it to `resul…`', () => {
    const line = (
      <button type="button" className="ag-loglast">
        <b>Last log line (not the error)</b>
        <span className="ag-loglast-line mono">result · error_during_execution · exit status 1</span>
        <span className="ag-loglast-open">Open logs ›</span>
      </button>
    )
    const el = host(failedRun(), line)
    const c = el.querySelector<HTMLElement>('.dt-card[data-lead="failed"]')!
    // MUTATION: drop `dt-fail` from DtFailure's section -- nothing scopes the wrap to the Failed card.
    expect(c.classList.contains('dt-fail')).toBe(true)
    const strip = c.querySelector<HTMLElement>(':scope > .ag-loglast')!
    const text = strip.querySelector<HTMLElement>('.ag-loglast-line')!
    // MUTATION: drop the `.dt-fail > .ag-loglast` wrap -- the line keeps one row beside its label and is cut.
    expect(painted(strip, 'flex-wrap', PHONE)).toBe('wrap')
    // MUTATION: leave agents.css's `nowrap` / ellipsis in force inside the Failed card -- `resul…` at 390.
    expect(painted(text, 'white-space', PHONE)).toBe('normal')
    expect(painted(text, 'overflow-wrap', PHONE)).toBe('anywhere')
    expect(painted(text, 'text-overflow', PHONE)).toBe('clip')
  })
})
