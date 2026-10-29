/**
 * AN AGENT'S DETAILS PANE AND ITS ATTEMPTS TAB, VISUAL QA RUN 6
 * (QAFIX6-TASKDETAIL, part of #1680; workflow wf_31aca00c977e4d37a935,
 * 2026-10-23, the fixture build of main at d3cdbd62, 1440 and 390 px, both
 * themes). One `describe` per lane item, each named by its #1680 box:
 *
 *   a  6298220484  CPU against one limit (c05, `Name every count where it is
 *                  printed`).
 *   b  6283738266  why the model is not recorded, by attempt (6:task-003).
 *   c  6343768697  a gap and a dot after the `partial` chip (6:task-004).
 *   d  6283738266  QA-113's remaining shorthand.
 *   e  6:task-008  Progress bars on one time scale.
 *   f  6314119814, 6343769495  Peak memory: the newest's figure, the worst
 *                  named whole under it (c06).
 *   g  6343792637  the Cost tile's line wraps.
 *   h  6343756607  Resources at 390: the peak bars keep a width.
 *   i  6283738952  QA-115: the Failed card, one link to Attempts.
 *
 * jsdom applies no container query, so every width question goes to the
 * shipped sheet's cascade (`painted`) with the pane's container width stated:
 * the 1440 split's pane is 480-639 px, a phone's is its viewport.
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

const ID = 'task_0123456789abcdef0126'
const GIB = 1024 ** 3
/** The 1440 split's Details pane: a 480-639 px container. */
const PANE_1440: CascadeEnv = { width: 1440, container: 560 }
/** A phone: the pane is the viewport. */
const PANE_390: CascadeEnv = { width: 390, container: 390 }

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
    created_at: at(0),
    started_at: at(30),
    completed_at: null,
    attempt_count: 3,
    max_attempts: 3,
    current_generation: 3,
    current_lease_id: 'lse_3',
    ...over,
  })
}

function failed(over: Partial<Task> = {}): Task {
  return running({ state: 'FAILED', completed_at: at(40), current_lease_id: null, last_error: 'exit status 1', ...over } as Partial<Task>)
}

/** The fixture build's shape: attempt 2 the worst (7.20 GiB, a near miss), attempt 3 the newest. */
function threeAttempts(ended: boolean, over3: Partial<AttemptRow> = {}): AttemptRow[] {
  return [
    attempt(1, { task_id: ID, created_at: at(0), started_at: at(1), completed_at: at(10), exit_code: 1, peak_rss_bytes: 2 * GIB }),
    attempt(2, {
      task_id: ID,
      created_at: at(11),
      started_at: at(12),
      completed_at: at(28),
      exit_code: 1,
      peak_rss_bytes: 7.2 * GIB,
      oom_near_miss: true,
    }),
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
      ...over3,
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
function host(r: AgentRun): HTMLElement {
  const el = document.createElement('div')
  el.innerHTML = renderToStaticMarkup(<Run run={r} headed />)
  document.body.appendChild(el)
  hosts.push(el)
  return el
}

function tile(el: HTMLElement, label: RegExp): HTMLElement {
  const c = [...el.querySelectorAll<HTMLElement>('.dt-strip > .dt-sc')].find((x) => label.test(x.querySelector('.dt-sc-l')?.textContent ?? ''))
  expect(c, `no tile ${label}`).toBeDefined()
  return c!
}

function resourcesRow(el: HTMLElement, name: string): HTMLElement {
  const row = [...el.querySelectorAll<HTMLElement>('.dt-resources > .dt-rr')].find((r) => r.querySelector('.dt-rr-n')?.textContent === name)
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

/** A sub-line that is whole: no clamp, nothing hidden, at the width asked. */
function expectWhole(sub: HTMLElement, env: CascadeEnv): void {
  expect(painted(sub, ['-webkit-line-clamp'], env) ?? 'none').toBe('none')
  expect(painted(sub, ['line-clamp'], env) ?? 'none').toBe('none')
  expect(painted(sub, 'overflow', env) ?? 'visible').toBe('visible')
  expect(painted(sub, 'text-overflow', env) ?? 'clip').not.toBe('ellipsis')
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
// a. CPU against one limit (box 6298220484, c05)
// ---------------------------------------------------------------------------

describe('a: every `cgroup limit` figure on Details divides by one limit', () => {
  it('prints the tile, Resources and the over-time summary against the attempt’s 2 cores', async () => {
    const el = host(runOf(running(), threeAttempts(false), [beat()]))
    // The utilisation route serves the attempt document's own `cpu_limit_cores`
    // (swarm_api/utilisation.py), so a served series carries the same 2. This
    // pins that case only, and it holds on main too. The brief's other case
    // (the 5 s samples carry 4, the record 2, as the fixture build serves) is
    // named in UtilisationPanel.tsx, outside this lane's files: its change and
    // test wait on the owner (questions.json, item-a-utilisation-limit.patch).
    const p = (i: number, cpu: number) => ({ at: at(30 + i / 12), cpu_cores: cpu, memory_bytes: 2 * GIB, throttled_share: 0 })
    const data: UtilisationRead = {
      task_id: ID,
      read_at: at(36),
      attempts: [
        {
          attempt_id: 'att_3',
          generation: 3,
          cpu_limit_cores: 2,
          memory_limit_bytes: 8 * GIB,
          throttled_seconds: 0,
          peak_throttled_share: 0,
          mean_throttled_share: 0,
          points: [p(0, 1), p(1, 1.6), p(2, 0.4)],
        },
      ],
    }
    api.loadUtilisation.mockResolvedValue(ok(data))
    const { container } = render(<UtilisationPanel taskId={ID} live={false} />)
    await act(async () => {})
    const texts = [
      tile(el, /^CPU/).textContent ?? '',
      resourcesRow(el, 'CPU peak').querySelector('.dt-rr-v')?.textContent ?? '',
    ]
    // The over-time fold's title prints no core count since QA1
    // overview-agents-24/25: there is nothing on it to divide.
    expect(container.querySelector('[data-testid="util-headline"]')?.textContent ?? '').not.toMatch(/cores/)
    // Each figure called the cgroup's, with the core count it divides by.
    const limits = texts.map((t) => {
      expect(t, t).toMatch(/cgroup limit/)
      return /(?:\/|of) (\d+(?:\.\d+)?) cores/.exec(t)?.[1] ?? null
    })
    // MUTATION: divide the CPU tile by the class's cpu instead of the attempt's limit -- `/ 4 cores` beside `of 2 cores`.
    expect(new Set(limits)).toEqual(new Set(['2']))
  })

  it('prints the series’ own 4 cores nowhere on the glass when the attempt record’s limit is 2', async () => {
    // The fixture build's case (api.ts): the 5 s samples carry 4 cores, the
    // attempt record 2. Each figure is named by where it came from.
    const el = host(runOf(running(), threeAttempts(false), [beat()]))
    const p = (i: number, cpu: number) => ({ at: at(30 + i / 12), cpu_cores: cpu, memory_bytes: 2 * GIB, throttled_share: 0.06 })
    const data: UtilisationRead = {
      task_id: ID,
      read_at: at(36),
      attempts: [
        {
          attempt_id: 'att_3',
          generation: 3,
          cpu_limit_cores: 4,
          memory_limit_bytes: 8 * GIB,
          throttled_seconds: 41.2,
          peak_throttled_share: 0.35,
          mean_throttled_share: 0.06,
          points: [p(0, 2.8), p(1, 2.8), p(2, 2.8)],
        },
      ],
    }
    api.loadUtilisation.mockResolvedValue(ok(data))
    const { container } = render(<UtilisationPanel taskId={ID} live={false} />)
    await act(async () => {})
    const head = container.querySelector('[data-testid="util-headline"]')?.textContent ?? ''
    const pane = `${el.querySelector('.dt-strip')?.textContent ?? ''} ${el.querySelector('.dt-resources')?.textContent ?? ''} ${head}`
    // QA1 overview-agents-24/25: the fold's title is its two shares alone; the
    // series' `mean 2.8 of 4 cores` is the chart's caption, inside the fold.
    // MUTATION: put back `mean 2.8 of 4 cores (the limit the 5 s samples carry; …)` on the title.
    expect(head).toBe('CPU and memory over time · at the limit 0% of the time · throttled 6%')
    // The series' 4 cores are printed nowhere on the glass: the strip and
    // Resources divide by the record's 2, and the title prints no count.
    expect(pane).not.toMatch(/\b4 cores/)
    expect(pane).toMatch(/\b2 cores/)
  })
})

// ---------------------------------------------------------------------------
// b. Why the model is not recorded (box 6283738266, 6:task-003)
// ---------------------------------------------------------------------------

describe('b: the reason under the model’s `not recorded` is the one that can be true for that attempt', () => {
  it('says the worker did not write it on the newest claude-code attempt, and `older attempt` on the others', async () => {
    const rows = threeAttempts(false).map((a) => ({ ...a, model: null }))
    const el = await attemptsTab(running(), rows, [beat()])
    // MUTATION: put back the one sentence `older attempt, or no-model runner` -- neither half fits the newest claude-code attempt.
    expect(fact(card(el, 3), 'model').querySelector('.att-model-why')?.textContent).toBe('the worker did not write it')
    expect(fact(card(el, 2), 'model').querySelector('.att-model-why')?.textContent).toBe('older attempt')
  })

  it('says `no model` for a runner that starts none, on every attempt', async () => {
    const t = running({ runner_profile: 'mock' })
    const rows = threeAttempts(false).map((a) => ({ ...a, model: null }))
    const el = await attemptsTab(t, rows, [beat()])
    // MUTATION: test `isLatest` before the runner -- the newest mock attempt reads `the worker did not write it`.
    for (const n of [1, 3]) expect(fact(card(el, n), 'model').querySelector('.att-model-why')?.textContent).toBe('no model')
  })
})

// ---------------------------------------------------------------------------
// c. A gap after `partial` (box 6343768697, 6:task-004)
// ---------------------------------------------------------------------------

describe('c: the event-history line puts a gap and a dot between the `partial` chip and its count', () => {
  it('draws a separator element between the chip and `2 attempts without events`', async () => {
    // One event, attempt 1's: attempts 2 and 3 have none on this page.
    const events = [{ ...ev('heartbeat', at(5), 'att_1', {}), task_id: ID, generation: 1 }]
    const el = await attemptsTab(running(), threeAttempts(false), events)
    const note = el.querySelector<HTMLElement>('.ctl-toolbar > .ctl-card-note')!
    expect(note.textContent).toMatch(/2 attempts without events/)
    const chip = note.querySelector<HTMLElement>('.ctl-mark.is-partial')!
    const sep = chip.nextElementSibling as HTMLElement | null
    // MUTATION: drop the `att-mark-sep` span -- `partial` sits a bare space from `2 attempts without events`.
    expect(sep?.classList.contains('att-mark-sep')).toBe(true)
    expect(sep?.nextSibling?.textContent).toMatch(/^2 attempts\b/)
    for (const env of [PANE_1440, PANE_390]) {
      // MUTATION: drop the separator's rules in details.css -- the span is an empty space again.
      expect(painted(sep!, 'content', env, 'before')).toBe('"·"')
      expect(painted(sep!, ['margin-inline', 'margin-inline-start'], env)).toBe('4px')
    }
  })
})

// ---------------------------------------------------------------------------
// d. QA-113's remaining shorthand (box 6283738266)
// ---------------------------------------------------------------------------

describe('d: the Attempts tab spells its nouns out and titles `gen`', () => {
  it('prints no bare `att`, `ev`, `ckpt`, `rss` or `blind`, and every `gen` is titled', async () => {
    const events = [{ ...ev('heartbeat', at(5), 'att_1', {}), task_id: ID, generation: 1 }]
    const el = await attemptsTab(running(), threeAttempts(false), events)
    const text = el.textContent ?? ''
    // Ids are verbatim (`att_1`), so only whole words are counted.
    const words = text.split(/[^A-Za-z]+/)
    // MUTATION: put `3 att · 9 ev` back in the summary -- the shorthand QA-113 named.
    for (const bare of ['att', 'ev', 'ckpt', 'rss']) expect(words, bare).not.toContain(bare)
    // `blind` as a tally (`2 blind`); the help card's prose may use the word in a sentence.
    expect(text).not.toMatch(/\b\d+ blind\b/)
    const labels = [...el.querySelectorAll<HTMLElement>('.att-label')]
    expect(labels.length).toBe(3)
    // MUTATION: drop the title from the attempt label -- `gen 2` is a key nothing explains.
    for (const l of labels) expect(l.title).toMatch(/fencing generation/)
  })
})

// ---------------------------------------------------------------------------
// e. Progress on one scale (6:task-008)
// ---------------------------------------------------------------------------

describe('e: the Progress card draws every attempt on the longest attempt’s scale', () => {
  it('draws attempts of 8 s, 48 s and 98 s at widths in proportion', () => {
    const s = (sec: number) => sec / 60
    const t = failed({ created_at: at(0), started_at: at(0), completed_at: at(s(200)), attempt_count: 3 })
    const rows = [
      attempt(1, { task_id: ID, created_at: at(0), started_at: at(0), completed_at: at(s(8)), exit_code: 1 }),
      attempt(2, { task_id: ID, created_at: at(s(20)), started_at: at(s(20)), completed_at: at(s(68)), exit_code: 1 }),
      attempt(3, { task_id: ID, created_at: at(s(100)), started_at: at(s(100)), completed_at: at(s(198)), exit_code: 1 }),
    ]
    const el = host(runOf(t, rows))
    const bars = [...el.querySelectorAll<HTMLElement>('.dt-progress .dt-phrow:not(.is-head)')]
    expect(bars.map((b) => b.querySelector('b')?.textContent)).toEqual(['8s', '48s', '1m 38s'])
    const grow = (i: HTMLElement) => Number(i.style.flexGrow || 0)
    const share = bars.map((b) => {
      const segs = [...b.querySelectorAll<HTMLElement>('.dt-phb > i')]
      const all = segs.reduce((n, i) => n + grow(i), 0)
      const filled = segs.filter((i) => !i.classList.contains('is-rest')).reduce((n, i) => n + grow(i), 0)
      return filled / all
    })
    // MUTATION: drop the `is-rest` filler -- each bar fills its own track and 8 s looks as long as 98 s.
    expect(share[0]).toBeCloseTo(8 / 98, 3)
    expect(share[1]).toBeCloseTo(48 / 98, 3)
    expect(share[2]).toBeCloseTo(1, 3)
    // The filler takes back the 2 px flex gap in front of it, so a shorter
    // row's phases get the same free width as the longest row's.
    const rest = bars[0]!.querySelector<HTMLElement>('.dt-phb > .is-rest')!
    // MUTATION: drop the filler's `margin-left: -2px` -- every shorter row loses one more gap of its track.
    expect(painted(rest, 'margin-left', PANE_1440)).toBe('-2px')
  })
})

// ---------------------------------------------------------------------------
// f. Peak memory, as decided (boxes 6314119814 c06, 6343769495)
// ---------------------------------------------------------------------------

describe('f: Peak memory is the newest attempt’s figure, with the worst named whole under it', () => {
  it('shows attempt 3’s 1.72 GiB and `worst 7.20 GiB, attempt 2 · OOM near miss`, unclamped at 1440', () => {
    const el = host(runOf(running(), threeAttempts(false), [beat()]))
    const c = tile(el, /^Peak memory/)
    expect(c.querySelector('.dt-sc-v')?.firstChild?.textContent?.trim()).toBe('1.72 GiB')
    const sub = c.querySelector<HTMLElement>('.dt-sc-s')!
    expect(sub.textContent).toMatch(/worst 7\.20 GiB, attempt 2 · OOM near miss$/)
    // MUTATION: drop `whole` from the Peak memory cell -- the 480-639 px pane clamps it to `worst 7.20 GiB, attempt…`.
    expectWhole(sub, PANE_1440)
  })
})

// ---------------------------------------------------------------------------
// g. The Cost tile (box 6343792637)
// ---------------------------------------------------------------------------

describe('g: the Cost tile’s line wraps instead of being cut at 1440', () => {
  it('leaves `cost 1 of 3 reported · last attempt …` whole', () => {
    const rows = threeAttempts(true).map((a, i) => ({ ...a, cost_usd: i === 0 ? 0.42 : null }))
    const el = host(runOf(failed(), rows))
    const sub = tile(el, /^Cost/).querySelector<HTMLElement>('.dt-sc-s')!
    expect(sub.textContent).toMatch(/^cost 1 of 3 reported · last attempt not reported/)
    // MUTATION: leave the Cost cell clamped -- `cost 1 of 3 reported · last...` at 1440.
    expectWhole(sub, PANE_1440)
  })
})

// ---------------------------------------------------------------------------
// h. Resources at 390 (box 6343756607)
// ---------------------------------------------------------------------------

describe('h: at 390 the Memory and CPU peak bars keep a width', () => {
  it('gives each Resources bar a phone min-width', () => {
    const el = host(runOf(running(), threeAttempts(false), [beat()]))
    for (const name of ['Memory', 'CPU peak']) {
      const bar = resourcesRow(el, name).querySelector<HTMLElement>('.dt-bar')!
      // MUTATION: drop the phone `.dt-rr > .dt-bar { min-width }` rule -- a track list change squeezes the bar to 0 px again.
      expect(painted(bar, 'min-width', PANE_390), name).toBe('48px')
    }
  })
})

// ---------------------------------------------------------------------------
// i. QA-115: the Failed card (box 6283738952)
// ---------------------------------------------------------------------------

describe('i: a failed task’s Details offers one link to Attempts, and explains the missing end cause', () => {
  it('has exactly one Attempts link and says where the cause is', () => {
    const el = host(runOf(failed({ end_cause: null } as Partial<Task>), threeAttempts(true)))
    const links = [...el.querySelectorAll<HTMLAnchorElement>('a[href]')].filter((a) => /\/attempts(?:$|[?#])/.test(a.getAttribute('href') ?? ''))
    // MUTATION: put `Attempts ›` back in the Failed card's head -- two links to one tab.
    expect(links.length).toBe(1)
    const c = el.querySelector<HTMLElement>('.dt-card[data-lead="failed"]')!
    expect(within(c).getByText('why it ended: the error below · no end-cause code was written')).toBeTruthy()
  })
})
