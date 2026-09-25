// THE TIMELINE AND THE TENANTS TABLE, AS BEHAVIOUR.
//
// Work > Timeline had no component test at all. What it draws is a window of
// rows -- bounded by COUNT, labelled by the span the rows turned out to cover
// -- and every defect the 2026-09-25 visual QA pass found on it (epic #84) was
// the screen saying something about that window that the window did not
// support: a time axis that skipped every empty hour, `08 PM` three times with
// no day, `Last 500 tasks` over a window that held all 170, `partial` over a
// table that held every row. The tenants table's credential tags (epic #86)
// ran together into one word.
//
// The api module is replaced with windows built HERE, in the viewer's LOCAL
// time -- `new Date(y, m, d, h, min)` -- because the chart buckets in the
// viewer's zone (`bucketStart`), and a fixture written in UTC would put its
// rows in a different hour on every machine that is not.
//
// Every case below was committed RED against the code it describes before the
// fix landed.

import { describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen, within } from '@testing-library/react'

import type { Result } from '../fetch'
import { HELP } from '../help'
import type { Task, TaskState, TaskWindow, Tenant } from '../types'

const api = vi.hoisted(() => ({
  loadTaskWindow: vi.fn(),
  loadTenants: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => {
  const real = await importOriginal<typeof import('../api')>()
  return { ...real, ...api }
})

const { ActivityScreen, TenantsScreen, axisLabels, nextBucket, outcomeBuckets } = await import('../Activity')

/** An instant in the VIEWER'S zone, 24 September 2026 unless said otherwise. */
const local = (hour: number, minute = 0, day = 24) => new Date(2026, 8, day, hour, minute).toISOString()

function task(id: string, state: TaskState, over: Partial<Task> = {}): Task {
  return {
    id,
    tenant_id: 'eng',
    state,
    runner_profile: 'mock',
    resource_class: 'standard',
    provider: null,
    priority: 0,
    created_at: local(9),
    updated_at: local(9),
    started_at: null,
    completed_at: null,
    submitted_by: 'bogdan@saga.xyz',
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

function windowOf(tasks: Task[], moreExist: boolean): TaskWindow {
  const times = tasks.map((t) => t.created_at).sort()
  return { tasks, moreExist, pages: 1, from: times[0] ?? null, to: times[times.length - 1] ?? null }
}

function serve(w: TaskWindow): void {
  api.loadTaskWindow.mockResolvedValue({ status: 'ok', data: w, fetchedAt: Date.now() } satisfies Result<TaskWindow>)
}

async function timeline(w: TaskWindow, bucket?: 'hour' | 'day'): Promise<HTMLElement> {
  serve(w)
  const { container } = render(<ActivityScreen />)
  await screen.findByText(/Outcomes by/)
  if (bucket !== undefined) {
    const group = [...container.querySelectorAll('label')].find((l) => l.textContent?.startsWith('Group by'))
    fireEvent.change(group!.querySelector('select')!, { target: { value: bucket } })
    await screen.findByText(`Outcomes by ${bucket}`)
  }
  return container as HTMLElement
}

/**
 * The chart's columns, each as its task count, read off `data-total`.
 *
 * NOT OFF A `title` ANY MORE (TS-9). The count used to live in a hover-only
 * `title=`, which §8.3 forbids for a value: a phone has no hover and a keyboard
 * cannot reach one. The column now carries its counts in its accessible name
 * and the legend reads them out; `data-total` is the machine-readable total.
 */
function totals(root: HTMLElement): number[] {
  return [...root.querySelectorAll('.chart .col')].map((c) => Number(c.getAttribute('data-total')))
}

/** The same date the axis prints, in the viewer's locale. */
const dayOf = (h: number, d = 24) => new Date(2026, 8, d, h).toLocaleDateString(undefined, { day: 'numeric', month: 'short' })
/** An hourly column's full label: its date and its hour. */
const hourLabel = (h: number, d = 24) =>
  `${dayOf(h, d)} ${new Date(2026, 8, d, h).toLocaleTimeString(undefined, { hour: '2-digit' })}`

/** The legend's counts, in its order: succeeded, failed, cancelled, still open. */
function readout(root: HTMLElement): number[] {
  return [...root.querySelectorAll('.chart-legend .cl-n')].map((n) => Number(n.textContent))
}

/** A figure on the metric strip, by its label -- the label's own words, not its help note. */
function figure(root: HTMLElement, label: string): HTMLElement {
  const m = [...root.querySelectorAll('.ctl-metrics .ctl-metric')].find(
    (el) => el.querySelector('.ctl-metric-label')?.firstChild?.textContent === label,
  )
  expect(m, `no "${label}" figure on the strip`).toBeTruthy()
  return m as HTMLElement
}

/** A row whose result carries this usage block, as `finish()` writes it. */
function spent(id: string, usage: Record<string, number> | null, over: Partial<Task> = {}): Task {
  return task(id, 'SUCCEEDED', {
    completed_at: local(10),
    result_summary: usage === null ? null : { runner: { usage } },
    ...over,
  })
}

function section(root: HTMLElement, heading: string): HTMLElement {
  const h = [...root.querySelectorAll('section h2')].find((el) => el.textContent === heading)
  expect(h, `no ${heading} section`).toBeTruthy()
  return h!.closest('section') as HTMLElement
}

describe('the outcomes chart', () => {
  it('TS-1: draws every hour between the first and the last, and an empty one as zero', async () => {
    const root = await timeline(
      windowOf(
        [
          // Submitted at 08:10 and finished at 10:05. Its submission hour drew a
          // bar-less column of its own: `submitted` was counted there for an
          // overlay that was never drawn.
          task('a', 'SUCCEEDED', { created_at: local(8, 10), completed_at: local(10, 5) }),
          task('b', 'FAILED', { created_at: local(12, 50), completed_at: local(13, 20) }),
        ],
        false,
      ),
      'hour',
    )
    // 10, 11, 12 and 13 -- the idle hours drawn as the measured zeroes they
    // are, and nothing before the first outcome.
    expect(totals(root), 'the x-axis skips the empty hours, or starts at a submission').toEqual([1, 0, 0, 1])
    // The first column's time, off its accessible name (TS-9) rather than the
    // hover-only `title` it used to carry.
    const first = root.querySelector('.chart .col')!.getAttribute('aria-label') ?? ''
    expect(first.startsWith(`${hourLabel(10)}:`), `the first column is named "${first}"`).toBe(true)
  })

  it('TS-2: carries the date at each day boundary when grouped by hour', async () => {
    const root = await timeline(
      windowOf(
        [
          task('late', 'SUCCEEDED', { created_at: local(22, 10, 23), completed_at: local(22, 30, 23) }),
          task('early', 'SUCCEEDED', { created_at: local(1, 10), completed_at: local(1, 30) }),
        ],
        false,
      ),
      'hour',
    )
    const labels = [...root.querySelectorAll('.chart .col-label')].map((l) => l.textContent)
    const date = (d: number) => new Date(2026, 8, d).toLocaleDateString(undefined, { day: 'numeric', month: 'short' })
    // 22:00 on the 23rd, 23:00, then midnight on the 24th and 01:00.
    expect(labels).toHaveLength(4)
    expect(labels[0], 'the first column names no day').toBe(date(23))
    expect(labels[2], 'midnight is an hour with no date').toBe(date(24))
    expect(labels[3]).toBe(new Date(2026, 8, 24, 1).toLocaleTimeString(undefined, { hour: '2-digit' }))
    // And the boundary is DRAWN, on midnight's column -- not on the chart's
    // first, which starts a day only because the chart starts there.
    const cols = [...root.querySelectorAll('.chart .col')]
    expect(cols.map((c) => c.classList.contains('is-day-start'))).toEqual([false, false, true, false])
  })

  it('TS-22: says which series is on which basis in the legend', async () => {
    const root = await timeline(windowOf([task('open', 'RUNNING', { created_at: local(9) })], false))
    const legend = root.querySelector('.chart-legend')!
    // Two bases, because two series are bucketed differently: the outcomes by
    // `completed_at`, and `still open` by `created_at` -- an open task has no
    // other time. The legend named one basis, after all four swatches.
    const bases = [...legend.querySelectorAll('.cl-basis')]
    expect(bases.map((b) => b.textContent), 'the legend puts `still open` under `completed_at`').toEqual([
      'by completed_at',
      'by created_at',
    ])
    // Each beside the swatches it qualifies: the outcomes, then the open key,
    // then its own basis.
    const after = (a: Element, b: Element) => (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING) !== 0
    const open = legend.querySelector('.k.open')!
    expect(after(legend.querySelector('.k.cancelled')!, bases[0]!)).toBe(true)
    expect(after(bases[0]!, open)).toBe(true)
    expect(after(open, bases[1]!)).toBe(true)
  })
})

describe('the axis arithmetic', () => {
  const at = (h: number, day = 24) => new Date(2026, 8, day, h).getTime()

  it('steps each bucket to the next one in local time', () => {
    expect(nextBucket(at(23, 23), 'hour')).toBe(at(0, 24))
    expect(nextBucket(at(0, 23), 'day')).toBe(at(0, 24))
    // 21 September 2026 is a Monday, the ISO week's first day.
    expect(nextBucket(new Date(2026, 8, 21).getTime(), 'week')).toBe(new Date(2026, 8, 28).getTime())
    expect(nextBucket(new Date(2026, 8, 1).getTime(), 'month')).toBe(new Date(2026, 9, 1).getTime())
  })

  it('makes no column from a submission alone, and fills every gap between outcomes with a zero', () => {
    const { buckets } = outcomeBuckets(
      [
        task('a', 'SUCCEEDED', { created_at: local(3), completed_at: local(10) }),
        task('b', 'CANCELLED', { created_at: local(4), completed_at: local(14) }),
      ],
      'hour',
    )
    expect(buckets.map(([k]) => new Date(k).getHours())).toEqual([10, 11, 12, 13, 14])
    expect(buckets.map(([, c]) => c.succeeded + c.failed + c.cancelled + c.open)).toEqual([1, 0, 0, 0, 1])
  })

  it('thins labels on a long hourly axis without ever dropping a day', () => {
    // Thirty hours from 10:00 on the 23rd: past the point where every column
    // can carry a label, and across one midnight.
    const keys = Array.from({ length: 30 }, (_, i) => at(10, 23) + i * 3_600_000)
    const labels = axisLabels(keys, 'hour')
    expect(labels).toHaveLength(30)
    const date = (d: number) => new Date(2026, 8, d).toLocaleDateString(undefined, { day: 'numeric', month: 'short' })
    // The first column and the first of each day carry the date.
    expect(labels[0]).toBe(date(23))
    const midnight = keys.findIndex((k) => new Date(k).getDate() === 24)
    expect(labels[midnight]).toBe(date(24))
    // No two neighbours are both labelled, so none runs into the next.
    for (let i = 1; i < labels.length; i++) {
      expect(labels[i] !== '' && labels[i - 1] !== '', `columns ${i - 1} and ${i} are both labelled`).toBe(false)
    }
    // And the thinning still labels the axis: every other column at worst.
    expect(labels.filter((l) => l !== '').length).toBeGreaterThanOrEqual(13)
  })

  it('labels every column of a short axis', () => {
    const keys = [at(9), at(10), at(11)]
    expect(axisLabels(keys, 'hour').every((l) => l !== '')).toBe(true)
  })
})

describe('the window it describes', () => {
  it('TS-8: says "All N tasks" when nothing older exists, and "Last N" only when it was cut', async () => {
    // Two rows under the default budget of 500, and nothing older: this window
    // IS the tenant's history. It read `Last 500 tasks`.
    const all = await timeline(
      windowOf([task('a', 'SUCCEEDED', { completed_at: local(10) }), task('b', 'SUCCEEDED', { completed_at: local(11) })], false),
    )
    expect(all.querySelector('.wb-span strong')!.textContent, 'the Rows budget printed over a complete window').toBe(
      'All 2 tasks',
    )
  })

  it('TS-8: says "Last N tasks" with the rows it read when older ones exist', async () => {
    const cut = await timeline(
      windowOf([task('a', 'SUCCEEDED', { completed_at: local(10) }), task('b', 'SUCCEEDED', { completed_at: local(11) })], true),
    )
    expect(cut.querySelector('.wb-span strong')!.textContent).toBe('Last 2 tasks')
  })

  it('TS-10: marks the People table partial only when the window is', async () => {
    const whole = await timeline(windowOf([task('a', 'SUCCEEDED', { completed_at: local(10) })], false))
    expect(
      section(whole, 'People').querySelector('.ctl-mark.is-partial'),
      '`partial` over a table that holds every task',
    ).toBeNull()
    expect(section(whole, 'People').querySelector('.ctl-panel-note')!.textContent).toContain('client-side')
  })

  it('TS-10: keeps the mark when older tasks were not read', async () => {
    const cut = await timeline(windowOf([task('a', 'SUCCEEDED', { completed_at: local(10) })], true))
    expect(section(cut, 'People').querySelector('.ctl-mark.is-partial')).toBeTruthy()
  })
})

// ---------------------------------------------------------------------------
// The owner's decisions on epic #84, 2026-09-25 (TS-3, TS-9, TS-11, TS-12)
// ---------------------------------------------------------------------------
//
// Each case was committed RED against the code it describes before the change
// landed, and the PR that carries them names the red run.

/** Five rows over three hours: 2 succeeded at 10, 1 open since 11, 1 failed and 1 cancelled at 12. */
const MIXED = (): TaskWindow =>
  windowOf(
    [
      task('a', 'SUCCEEDED', { created_at: local(9, 50), completed_at: local(10, 5) }),
      task('b', 'SUCCEEDED', { created_at: local(9, 55), completed_at: local(10, 40) }),
      task('e', 'RUNNING', { created_at: local(11, 10) }),
      task('c', 'FAILED', { created_at: local(11, 50), completed_at: local(12, 20) }),
      task('d', 'CANCELLED', { created_at: local(11, 55), completed_at: local(12, 30) }),
    ],
    false,
  )

/** Thirty hours from 10:00 on the 23rd, one outcome in each: past the six columns that all fit a label, and across a midnight. */
const THIRTY_HOURS = (): TaskWindow =>
  windowOf(
    Array.from({ length: 30 }, (_, i) =>
      task(`h${i}`, 'SUCCEEDED', {
        created_at: new Date(2026, 8, 23, 10 + i, 1).toISOString(),
        completed_at: new Date(2026, 8, 23, 10 + i, 5).toISOString(),
      }),
    ),
    false,
  )

/** Six hours, 10:00 to 15:00 on the 24th, one outcome in each: short enough that every label fits at 390. */
const SIX_HOURS = (): TaskWindow =>
  windowOf(
    Array.from({ length: 6 }, (_, i) =>
      task(`s${i}`, 'SUCCEEDED', { created_at: local(10 + i, 1), completed_at: local(10 + i, 5) }),
    ),
    false,
  )

describe('the chart is read out, not hovered (TS-9)', () => {
  it('is one named group of columns, each an image named with its full time and its four counts', async () => {
    const root = await timeline(MIXED(), 'hour')
    const chart = root.querySelector('.chart')!
    expect(chart.getAttribute('role')).toBe('group')
    expect(chart.getAttribute('aria-label')).toBe('Task outcomes by hour')
    const cols = [...chart.querySelectorAll('.col')]
    expect(cols).toHaveLength(3)
    for (const c of cols) {
      expect(c.getAttribute('role')).toBe('img')
      // §8.3: no value lives only in a hover. The title is gone; the counts
      // are the column's name and the legend's readout.
      expect(c.hasAttribute('title'), 'a column still carries a hover-only title').toBe(false)
    }
    expect(cols.map((c) => c.getAttribute('aria-label'))).toEqual([
      `${hourLabel(10)}: 2 succeeded, 0 failed, 0 cancelled, 0 still open`,
      `${hourLabel(11)}: 0 succeeded, 0 failed, 0 cancelled, 1 still open`,
      `${hourLabel(12)}: 0 succeeded, 1 failed, 1 cancelled, 0 still open`,
    ])
  })

  it('reads out the window totals in the legend, summed from the columns it draws', async () => {
    const root = await timeline(MIXED(), 'hour')
    const legend = root.querySelector<HTMLElement>('.chart-legend')!
    expect(readout(root), 'the legend carries no counts').toEqual([2, 1, 1, 1])
    // THE READOUT AND THE BARS CANNOT DISAGREE: its totals are the columns'.
    const drawn = totals(root).reduce((a, b) => a + b, 0)
    expect(readout(root).reduce((a, b) => a + b, 0), 'the legend totals are not the sum of the columns').toBe(drawn)
    expect((legend.textContent ?? '').replace(/\s+/g, ' ').trim()).toBe(
      'succeeded 2 · failed 1 · cancelled 1 by completed_at · still open 1 by created_at',
    )
    // Not a live region: the focused column's name already carries the same
    // counts, and a live legend would read them twice on every arrow press.
    expect(legend.getAttribute('aria-live')).toBeNull()
  })

  it('swaps to one column\'s counts when it is hovered, tapped or focused, and back on "all", Escape or leaving', async () => {
    const root = await timeline(MIXED(), 'hour')
    const chart = root.querySelector<HTMLElement>('.chart')!
    const cols = [...chart.querySelectorAll<HTMLElement>('.col')]
    const legend = root.querySelector<HTMLElement>('.chart-legend')!
    const prefix = () => legend.querySelector('.cl-at')?.textContent?.trim() ?? null
    const picked = () => cols.filter((c) => c.classList.contains('is-picked'))

    // Hover.
    fireEvent.mouseEnter(cols[2]!)
    expect(readout(root)).toEqual([0, 1, 1, 0])
    expect(prefix()).toBe(`${hourLabel(12)} ·`)
    expect(picked()).toEqual([cols[2]])
    // `all`, a text button, returns to the window.
    fireEvent.click(within(legend).getByRole('button', { name: 'all' }))
    expect(readout(root)).toEqual([2, 1, 1, 1])
    expect(prefix()).toBeNull()
    expect(picked()).toEqual([])
    expect(within(legend).queryByRole('button', { name: 'all' }), '"all" is drawn with nothing picked').toBeNull()

    // Tap: a phone has no hover, so a click picks too.
    fireEvent.click(cols[1]!)
    expect(readout(root)).toEqual([0, 0, 0, 1])
    expect(prefix()).toBe(`${hourLabel(11)} ·`)
    // Escape returns to the window.
    fireEvent.keyDown(cols[1]!, { key: 'Escape' })
    expect(readout(root)).toEqual([2, 1, 1, 1])

    // Focus.
    act(() => cols[0]!.focus())
    expect(readout(root)).toEqual([2, 0, 0, 0])
    expect(picked()).toEqual([cols[0]])
    // Leaving the chart returns to the window.
    fireEvent.mouseLeave(chart)
    expect(readout(root)).toEqual([2, 1, 1, 1])
    expect(picked()).toEqual([])
  })

  it('is one tab stop, moved by the arrows, Home and End', async () => {
    const root = await timeline(MIXED(), 'hour')
    const cols = [...root.querySelectorAll<HTMLElement>('.chart .col')]
    const stops = () => cols.filter((c) => c.getAttribute('tabindex') === '0')
    // The newest column, where the chart opens (TS-3).
    expect(stops(), 'the chart is not exactly one tab stop').toEqual([cols[2]])
    for (const c of cols.slice(0, 2)) expect(c.getAttribute('tabindex')).toBe('-1')

    act(() => cols[2]!.focus())
    fireEvent.keyDown(cols[2]!, { key: 'ArrowLeft' })
    expect(document.activeElement).toBe(cols[1])
    expect(stops()).toEqual([cols[1]])
    fireEvent.keyDown(cols[1]!, { key: 'Home' })
    expect(document.activeElement).toBe(cols[0])
    fireEvent.keyDown(cols[0]!, { key: 'ArrowLeft' })
    expect(document.activeElement, 'ArrowLeft past the oldest column went somewhere').toBe(cols[0])
    fireEvent.keyDown(cols[0]!, { key: 'End' })
    expect(document.activeElement).toBe(cols[2])
    fireEvent.keyDown(cols[2]!, { key: 'ArrowRight' })
    expect(document.activeElement).toBe(cols[2])
    expect(stops()).toEqual([cols[2]])
  })

  it('keeps "all" for the pointer and the Tab that reach it, and puts focus back on the column when it is pressed', async () => {
    // THE PATHS A REAL INPUT TAKES TO `all`. The button sits in the legend,
    // below the chart, so a pointer leaves the columns and a Tab leaves the
    // chart on the way to it. Each of those used to clear the pick, which
    // unmounted the button before it could be pressed -- and for the Tab it
    // took focus with it, to <body> (WCAG 2.4.3). The case above clicks the
    // button directly, which no pointer, key or finger can do; this one gets
    // there the way they do.
    const root = await timeline(MIXED(), 'hour')
    const chart = root.querySelector<HTMLElement>('.chart')!
    const cols = [...chart.querySelectorAll<HTMLElement>('.col')]
    const legend = root.querySelector<HTMLElement>('.chart-legend')!
    const all = () => within(legend).queryByRole('button', { name: 'all' })
    const picked = () => cols.filter((c) => c.classList.contains('is-picked'))
    const WINDOW = [2, 1, 1, 1]

    // POINTER: from a column, down across the legend, onto `all`.
    fireEvent.mouseEnter(cols[0]!)
    const button = all()
    expect(button, 'hovering a column drew no "all"').not.toBeNull()
    fireEvent.mouseOut(cols[0]!, { relatedTarget: legend })
    fireEvent.mouseOut(legend, { relatedTarget: button })
    expect(all(), 'moving the pointer onto "all" took "all" away').toBe(button)
    expect(readout(root), 'the readout gave up the column on the way to "all"').toEqual([2, 0, 0, 0])
    fireEvent.click(button!)
    expect(readout(root)).toEqual(WINDOW)
    expect(all()).toBeNull()

    // KEYBOARD: Tab onto the chart lands on its stop, the newest column, which
    // picks it; the next Tab is `all`.
    act(() => cols[2]!.focus())
    const tabbed = all()
    expect(tabbed, 'focusing a column drew no "all"').not.toBeNull()
    act(() => tabbed!.focus())
    expect(document.activeElement, 'Tab from the chart to "all" dropped focus').toBe(tabbed)
    expect(readout(root), 'reaching "all" put the window back before it was pressed').toEqual([0, 1, 1, 0])
    // Pressing it returns the window and hands focus back to the column the
    // chart's one tab stop is on -- not to <body> with the button that had it.
    // Focus comes back WITHOUT picking, the state Escape on a column leaves.
    fireEvent.click(tabbed!)
    expect(readout(root)).toEqual(WINDOW)
    expect(all()).toBeNull()
    expect(document.activeElement, 'pressing "all" dropped focus').toBe(cols[2])
    expect(picked(), 'handing focus back picked the column again').toEqual([])

    // Escape on `all` is the same press.
    fireEvent.keyDown(cols[2]!, { key: 'ArrowLeft' })
    expect(picked()).toEqual([cols[1]])
    const again = all()
    act(() => again!.focus())
    fireEvent.keyDown(again!, { key: 'Escape' })
    expect(readout(root)).toEqual(WINDOW)
    expect(document.activeElement, 'Escape on "all" dropped focus').toBe(cols[1])

    // And leaving the chart and its readout for the rest of the page is still
    // leaving: focus on the Group-by select puts the window back.
    fireEvent.keyDown(cols[1]!, { key: 'ArrowRight' })
    expect(picked()).toEqual([cols[2]])
    const group = [...root.querySelectorAll('label')].find((l) => l.textContent?.startsWith('Group by'))!
    act(() => group.querySelector('select')!.focus())
    expect(readout(root)).toEqual(WINDOW)
    expect(picked()).toEqual([])
  })
})

describe('the hourly chart at phone width (TS-3)', () => {
  it('opens at its newest end, and fades its left edge only while older buckets are off-screen', async () => {
    // jsdom lays nothing out, so the scroller's geometry is stated here: 1,200px
    // of columns in a 330px chart, which is the 390pt case the QA pass shot.
    const proto = HTMLElement.prototype
    const isChart = (el: HTMLElement) => el.classList.contains('chart')
    Object.defineProperty(proto, 'scrollWidth', { configurable: true, get(this: HTMLElement) { return isChart(this) ? 1200 : 0 } })
    Object.defineProperty(proto, 'clientWidth', { configurable: true, get(this: HTMLElement) { return isChart(this) ? 330 : 0 } })
    try {
      const root = await timeline(THIRTY_HOURS(), 'hour')
      const chart = root.querySelector<HTMLElement>('.chart')!
      expect(chart.scrollLeft, 'the chart opened at its oldest end').toBeGreaterThanOrEqual(1200 - 330)
      expect(chart.classList.contains('has-older'), 'older buckets are off-screen and nothing says so').toBe(true)
      // Scrolled back to the oldest column, there is nothing older to cue.
      chart.scrollLeft = 0
      fireEvent.scroll(chart)
      expect(chart.classList.contains('has-older')).toBe(false)
    } finally {
      Reflect.deleteProperty(proto, 'scrollWidth')
      Reflect.deleteProperty(proto, 'clientWidth')
    }
  })

  it('labels every third hour and every day at phone width, and nothing in between', async () => {
    const root = await timeline(THIRTY_HOURS(), 'hour')
    const cols = [...root.querySelectorAll('.chart .col')]
    expect(cols).toHaveLength(30)
    // What a phone shows: a label with words in it that is not wide-only.
    const shown = cols.flatMap((c, i) => {
      const l = c.querySelector('.col-label')!
      return l.classList.contains('is-wide-only') || (l.textContent ?? '').trim() === '' ? [] : [i]
    })
    const hourAt = (i: number) => (10 + i) % 24
    for (const i of shown) {
      const text = cols[i]!.querySelector('.col-label')!.textContent
      const isDate = text === dayOf(0, 23) || text === dayOf(0, 24)
      expect(isDate || hourAt(i) % 3 === 0, `column ${i} (${hourAt(i)}:00) is labelled "${text}" at phone width`).toBe(true)
    }
    for (let j = 1; j < shown.length; j++) {
      expect(shown[j]! - shown[j - 1]!, `labels on columns ${shown[j - 1]} and ${shown[j]} crowd each other`).toBeGreaterThanOrEqual(3)
    }
    // Every day is still named: the chart's first column and midnight.
    expect(shown).toContain(0)
    expect(shown).toContain(14)
    expect(shown.length).toBeGreaterThanOrEqual(8)
  })

  it('thins a short hourly axis at phone width too: every third hour, at any length', async () => {
    // THE DECISION HAS NO LENGTH IN IT: "labels thin to every 3rd hour at phone
    // width". An hourly axis of six columns or fewer used to keep every label
    // at 390 because they fit -- which is a different rule from the one decided.
    const root = await timeline(SIX_HOURS(), 'hour')
    const cols = [...root.querySelectorAll('.chart .col')]
    expect(cols).toHaveLength(6)
    const text = (c: Element) => (c.querySelector('.col-label')!.textContent ?? '').trim()
    const cls = (c: Element) => c.querySelector('.col-label')!.classList
    // A wide screen still labels all six.
    expect(cols.filter((c) => !cls(c).contains('is-phone-only') && text(c) !== ''), 'the wide axis lost a label').toHaveLength(6)
    // A phone: the day, on the first column, and `03 PM` -- the one hour on the
    // three-hour clock that is at least three columns past it.
    const shown = cols.flatMap((c, i) => (cls(c).contains('is-wide-only') || text(c) === '' ? [] : [i]))
    expect(shown, 'a short hourly axis is not thinned at phone width').toEqual([0, 5])
    expect(text(cols[0]!)).toBe(dayOf(0, 24))
    expect(text(cols[5]!)).toBe(new Date(2026, 8, 24, 15).toLocaleTimeString(undefined, { hour: '2-digit' }))
  })
})

describe('the figures under the chart (TS-11, TS-12)', () => {
  it('TS-11: are the boxless metric strip, holding Completed, Attempts consumed and Token spend -- and no Submitted', async () => {
    const root = await timeline(MIXED())
    expect(root.querySelector('.tiles, .tile'), 'a boxed tile is still drawn').toBeNull()
    const strip = root.querySelector('.ctl-metrics')
    expect(strip, 'the figures are not on the metric strip').not.toBeNull()
    const labels = [...strip!.querySelectorAll('.ctl-metric-label')].map((l) => l.firstChild?.textContent)
    expect(labels).toEqual(['Completed', 'Attempts consumed', 'Token spend'])
    // `w.tasks.length` is the window bar's own figure; a fourth tile repeated it.
    expect(strip!.textContent).not.toContain('Submitted')
  })

  it('TS-12: sums the cost the results carry, and marks the sum partial when some rows carry none', async () => {
    const root = await timeline(
      windowOf([spent('a', { total_cost_usd: 0.5 }), spent('b', { total_cost_usd: 0.25 }), spent('c', null)], false),
    )
    const m = figure(root, 'Token spend')
    expect(m.querySelector('.ctl-metric-value')!.textContent).toBe('$0.75')
    expect(m.querySelector('.ctl-metric-foot')!.textContent).toBe('2 of 3 tasks · from result')
    const mark = m.querySelector('.ctl-metric-sub .ctl-mark.is-partial')
    expect(mark, 'a sum over 2 of 3 rows is drawn as a total').not.toBeNull()
    // Its name says BOTH reasons a result-sum can fall short.
    expect(mark!.getAttribute('aria-label')).toMatch(/no cost/i)
    expect(mark!.getAttribute('aria-label')).toMatch(/last attempt/i)
    // The row count that stood where the figure should be, and its bar, are gone.
    expect(m.textContent).not.toMatch(/of rows carry usage/)
    expect(root.querySelector('.coverage')).toBeNull()
    // The label carries the help topic, as a description, not a glyph.
    expect(m.querySelector('.ctl-metric-label')!.getAttribute('aria-describedby')).not.toBeNull()
  })

  it('TS-12: draws no partial mark when every row carries a cost for its only attempt', async () => {
    const root = await timeline(
      windowOf([spent('a', { total_cost_usd: 0.5 }), spent('b', { total_cost_usd: 0.25 })], false),
    )
    const m = figure(root, 'Token spend')
    expect(m.querySelector('.ctl-metric-value')!.textContent).toBe('$0.75')
    expect(m.querySelector('.ctl-metric-foot')!.textContent).toBe('2 of 2 tasks · from result')
    expect(m.querySelector('.ctl-mark.is-partial')).toBeNull()
  })

  it('TS-12: marks the sum partial when a summed task ran more than once, because a result is its last attempt', async () => {
    const root = await timeline(
      windowOf([spent('a', { total_cost_usd: 0.5 }), spent('b', { total_cost_usd: 0.25 }, { attempt_count: 2 })], false),
    )
    expect(figure(root, 'Token spend').querySelector('.ctl-mark.is-partial')).not.toBeNull()
  })

  it('TS-12: prints a measured zero as $0.00, because it was measured', async () => {
    const root = await timeline(windowOf([spent('a', { total_cost_usd: 0 })], false))
    const m = figure(root, 'Token spend')
    expect(m.querySelector('.ctl-metric-value')!.textContent).toBe('$0.00')
    expect(m.classList.contains('is-absent')).toBe(false)
  })

  it('TS-12: says "not recorded" -- never $0.00 -- when no result carries a cost, even one carrying tokens', async () => {
    const root = await timeline(windowOf([spent('a', { input_tokens: 1200 }), spent('b', null)], false))
    const m = figure(root, 'Token spend')
    expect(m.classList.contains('is-absent')).toBe(true)
    expect(m.querySelector('.ctl-metric-value')!.textContent).toBe('not recorded')
    expect(m.textContent, 'an absent spend printed as money').not.toMatch(/\$/)
    const mark = m.querySelector('.ctl-metric-sub .ctl-mark.is-absent')
    expect(mark).not.toBeNull()
    expect(mark!.getAttribute('aria-label')).toBe(
      'Token spend: no task in this window carries a cost in its result. That is an absent measurement, not a spend of zero.',
    )
    expect(m.querySelector(`a[href="#help/tokens-reported"]`)).not.toBeNull()
  })

  it('TS-12: the help topic says the Timeline sum covers each task\'s last attempt only', () => {
    expect(HELP['tokens-reported'].long.join(' ')).toMatch(/Timeline.*last attempt/)
  })
})

describe('the tenants table', () => {
  it('AH-20: wraps the credential tags so they do not run together', async () => {
    const tenant: Tenant = {
      tenant_id: 'eng',
      kind: 'group',
      principal: 'eng@saga.xyz',
      display_name: null,
      created_at: local(9),
      max_active: 10,
      capacity_units: 20,
      monthly_budget_usd: null,
      enabled: true,
      credentials: ['anthropic', 'openai'],
      service_account: 'swarm-eng@example.iam.gserviceaccount.com',
      gcs_prefix: null,
      namespace: null,
    }
    api.loadTenants.mockResolvedValue({
      status: 'ok',
      data: { tenants: [tenant] },
      fetchedAt: Date.now(),
    } satisfies Result<{ tenants: Tenant[] }>)
    const { container } = render(<TenantsScreen />)
    await screen.findByText('eng@saga.xyz')
    const cell = container.querySelector('td[data-label="Credentials"]')!
    expect(cell.querySelectorAll('.tags > .tag'), 'two tags with nothing spacing them').toHaveLength(2)
  })
})
