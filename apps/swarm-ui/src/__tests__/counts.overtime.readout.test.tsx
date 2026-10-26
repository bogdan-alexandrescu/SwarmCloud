// ADMIN › PLATFORM COUNTS, OVER TIME: THE READOUT OVER THE CHART AND THE AXIS
// LABELS (COUNTS-ONE-RUN, #2149).
//
//   * F6 (docs/web-ui/mockups/counts-a.html) draws the readout of the picked
//     or hovered bucket OVER the chart, on the side away from that bucket.
//     It sat below the chart in the page flow. It stays a live region, the
//     arrow keys still move it, and on a phone (F7) it stays below the chart.
//   * The cost axis printed "$443" and "$400" nearly touching at 1440 (PR
//     2289's render): no two labels on an axis may overlap in any drawing,
//     at the largest size overtime.css lets a drawing grow to.
//
// jsdom draws no layout, so the side is the readout's `data-side` and the
// overlay is overtime.css's rule for it, both read here.

import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'

import type { Result } from '../fetch'
import type { PlatformTime, UsageBucket, UsageFigures } from '../platformTimeApi'

const loadPlatformTime = vi.hoisted(() => vi.fn<(q: URLSearchParams) => Promise<Result<PlatformTime>>>())
vi.mock('../platformTimeApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../platformTimeApi')>()),
  loadPlatformTime,
}))

const { PlatformOverTime, forgetOverTime } = await import('../PlatformOverTime')

const css = readFileSync(join(__dirname, '..', 'styles', 'overtime.css'), 'utf8')

function figures(agents: number, usd: number): UsageFigures {
  const tok = (sum: number) => ({ sum, reporting: agents, attempts: agents })
  return {
    agents,
    agents_ended: agents,
    retries: Math.round(agents / 10),
    attempts: agents,
    run_s: { sum: agents * 1800, measured: agents, attempts: agents },
    tokens: {
      input_tokens: tok(agents * 1000),
      output_tokens: tok(agents * 200),
      cache_read_input_tokens: tok(agents * 5000),
      cache_creation_input_tokens: tok(agents * 300),
    },
    cost: { sum_usd: usd, estimated_usd: null, estimated: 0, agents, reporting: agents },
  }
}

// Eight weeks whose cost peaks just over $400 -- PR 2289 measured "$443" and
// "$400" nearly touching on the cost axis. At $445 the $400 tick sits 14.6
// units under the top label in the 640 drawing: kept by the shared 14px gap,
// two 12px labels with 2.6 between them, then drawn up to 1.2 times larger.
const USD = [120, 210, 400, 445, 380, 300, 260, 90]
const BUCKETS: UsageBucket[] = USD.map((usd, i) => {
  const start = new Date(Date.UTC(2026, 7, 31 + i * 7)).toISOString()
  const end = new Date(Date.UTC(2026, 7, 38 + i * 7)).toISOString()
  return { start, end, state: 'sealed', in_progress: false, unread_reason: null, ...figures(100 + i * 9, usd) }
})

function payload(): Result<PlatformTime> {
  return {
    status: 'ok',
    fetchedAt: Date.now(),
    data: {
      scope: { kind: 'platform', tenants: ['eng', 'smoke'], excluded: [], tenants_complete: true },
      tz: 'UTC',
      since: BUCKETS[0]!.start,
      until: BUCKETS[BUCKETS.length - 1]!.end,
      bucket: 'week',
      reads: 120,
      cached: false,
      generated_at: new Date().toISOString(),
      usage: {
        basis: 'created_at',
        cost_label: 'Estimated cost (list price, not a bill)',
        buckets: BUCKETS,
        totals: { complete: true, ...figures(1000, USD.reduce((a, b) => a + b, 0)) },
        groups: null,
        breakdowns: {},
      },
    },
  } as unknown as Result<PlatformTime>
}

async function drawn(): Promise<void> {
  loadPlatformTime.mockResolvedValue(payload())
  render(<PlatformOverTime workspaces={2} />)
  fireEvent.click(screen.getByRole('button', { name: /^Run/ }))
  await waitFor(() => expect(document.querySelector('.pot-agents')).not.toBeNull())
}

/** The agents chart's widest drawing: the hit columns pick a bucket. */
function hits(): Element[] {
  return Array.from(document.querySelectorAll('.pot-agents svg.is-xl .pot-hit'))
}

beforeEach(() => {
  loadPlatformTime.mockReset()
  forgetOverTime()
})

describe('the readout (F6): over the chart, on the side away from its bucket', () => {
  it('sits with the chart, not below it in the flow, and away from a bucket on the right', async () => {
    await drawn()
    fireEvent.click(hits()[BUCKETS.length - 1]!)
    const ro = document.querySelector<HTMLElement>('.pot-readout')!
    expect(ro).not.toBeNull()
    // MUTATION: draw the readout after the legend again, outside the plot.
    expect(ro.parentElement?.classList.contains('pot-plot'), 'the readout is not drawn with the chart').toBe(true)
    expect(ro.parentElement?.querySelector('.pot-chart')).not.toBeNull()
    // MUTATION: always draw it on one side.
    expect(ro.dataset.side).toBe('left')
    fireEvent.click(hits()[0]!)
    expect(document.querySelector<HTMLElement>('.pot-readout')!.dataset.side).toBe('right')
  })

  it('follows the hovered bucket too, and the picked one wins', async () => {
    await drawn()
    expect(document.querySelector('.pot-readout')).toBeNull()
    fireEvent.mouseEnter(hits()[1]!)
    // MUTATION: draw the readout on a click only.
    const hover = document.querySelector<HTMLElement>('.pot-readout')
    expect(hover, 'hovering a bucket draws no readout').not.toBeNull()
    expect(hover!.dataset.side).toBe('right')
    expect(hover!.hasAttribute('data-hover')).toBe(true)
    fireEvent.mouseLeave(document.querySelector('.pot-agents svg.is-xl')!)
    expect(document.querySelector('.pot-readout')).toBeNull()
    fireEvent.click(hits()[6]!)
    fireEvent.mouseEnter(hits()[0]!)
    const pinned = document.querySelector<HTMLElement>('.pot-readout')!
    expect(pinned.dataset.side, 'a hover moved the picked readout').toBe('left')
    expect(pinned.hasAttribute('data-hover')).toBe(false)
  })

  it('stays a live region the arrow keys move', async () => {
    await drawn()
    const chart = document.querySelector<HTMLElement>('.pot-agents .pot-chart')!
    fireEvent.keyDown(chart, { key: 'End' })
    const ro = document.querySelector<HTMLElement>('.pot-readout')!
    expect(ro.getAttribute('role')).toBe('status')
    expect(ro.getAttribute('aria-live')).toBe('polite')
    expect(ro.dataset.side).toBe('left')
    fireEvent.keyDown(chart, { key: 'Home' })
    expect(document.querySelector<HTMLElement>('.pot-readout')!.dataset.side).toBe('right')
    fireEvent.keyDown(chart, { key: 'Escape' })
    expect(document.querySelector('.pot-readout')).toBeNull()
  })

  it('is laid over the chart only where the chart is wide, and below it on a phone', () => {
    // The overlay is a container rule on the plot, so a phone's plot (about
    // 340 wide) keeps the readout in the flow under the chart, as F7 draws it.
    // MUTATION: drop the container query, or position the readout absolutely everywhere.
    expect(css).toMatch(/\.pot-plot\s*\{[^}]*container:\s*pot-plot\s*\/\s*inline-size/)
    const at = css.indexOf('@container pot-plot (min-width: 640px)')
    expect(at, 'no wide-plot rule for the readout').toBeGreaterThan(-1)
    const block = css.slice(at, css.indexOf('\n}', at))
    expect(block).toMatch(/\.pot-readout\s*\{[^}]*position:\s*absolute/)
    expect(block).toMatch(/\.pot-readout\[data-side='left'\]\s*\{[^}]*left:/)
    expect(block).toMatch(/\.pot-readout\[data-side='right'\]\s*\{[^}]*right:/)
    const outside = css.slice(0, at) + css.slice(css.indexOf('\n}', at))
    expect(outside, 'the readout is laid over the chart outside the wide-plot rule').not.toMatch(/\.pot-readout\s*\{[^}]*position:\s*absolute/)
  })
})

/**
 * THE OVERLAY FITS (review, 2026-10-25): at 1440 the overlaid readout's
 * figures were 317px in a 282px box, and 633px in 247px with a breakdown, so
 * it scrolled and sliced its last lines -- and a hover's readout takes no
 * pointer, so they could not be reached. jsdom lays nothing out, so the box's
 * two heights are given here: a readout left over the chart never holds more
 * than it shows, and one that would is put under the chart.
 */
function heights(content: number, room: number): () => void {
  const sh = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'scrollHeight')
  const ch = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'clientHeight')
  Object.defineProperty(HTMLElement.prototype, 'scrollHeight', { configurable: true, get() { return this.classList.contains('pot-readout') ? content : 0 } })
  // Under the chart the box is as tall as what it holds.
  Object.defineProperty(HTMLElement.prototype, 'clientHeight', {
    configurable: true,
    get() { return this.classList.contains('pot-readout') ? (this.dataset.place === 'under' ? content : room) : 0 },
  })
  return () => {
    Object.defineProperty(HTMLElement.prototype, 'scrollHeight', sh ?? { configurable: true, value: 0 })
    Object.defineProperty(HTMLElement.prototype, 'clientHeight', ch ?? { configurable: true, value: 0 })
  }
}

describe('the overlaid readout never holds more than it shows', () => {
  it('goes under the chart when its figures are taller than the room over it', async () => {
    const restore = heights(317, 282)
    try {
      await drawn()
      fireEvent.click(hits()[BUCKETS.length - 1]!)
      const ro = document.querySelector<HTMLElement>('.pot-readout')!
      // MUTATION: lay the readout over the chart whatever its height.
      expect(ro.dataset.place, 'a readout taller than its room stayed over the chart').toBe('under')
      expect(ro.scrollHeight).toBeLessThanOrEqual(ro.clientHeight)
      // A hover's readout is measured too.
      fireEvent.keyDown(document.querySelector<HTMLElement>('.pot-agents .pot-chart')!, { key: 'Escape' })
      fireEvent.mouseEnter(hits()[1]!)
      const hover = document.querySelector<HTMLElement>('.pot-readout')!
      expect(hover.dataset.place).toBe('under')
      expect(hover.scrollHeight).toBeLessThanOrEqual(hover.clientHeight)
    } finally {
      restore()
    }
  })

  it('stays over the chart when it fits', async () => {
    const restore = heights(240, 282)
    try {
      await drawn()
      fireEvent.click(hits()[0]!)
      const ro = document.querySelector<HTMLElement>('.pot-readout')!
      // MUTATION: always put it under the chart.
      expect(ro.hasAttribute('data-place'), 'a readout that fits was put under the chart').toBe(false)
      expect(ro.scrollHeight).toBeLessThanOrEqual(ro.clientHeight)
    } finally {
      restore()
    }
  })

  it('clips rather than scrolls over the chart, and the place under it is the flow', () => {
    const at = css.indexOf('@container pot-plot (min-width: 640px)')
    const block = css.slice(at, css.indexOf('\n}', at))
    // MUTATION: give the overlay a scrollbar again.
    expect(block).not.toMatch(/overflow(-y)?:\s*auto/)
    expect(block).toMatch(/\.pot-readout\s*\{[^}]*overflow:\s*hidden/)
    expect(block).toMatch(/\.pot-readout\[data-place='under'\]\s*\{[^}]*position:\s*static/)
    // The dark theme's shadow: the token, which carries a value per theme.
    expect(block).toMatch(/\.pot-readout\s*\{[^}]*box-shadow:\s*var\(--ctl-shadow-pop\)/)
  })
})

/**
 * A COVER NEVER RUNS PAST ITS CELL (re-review of PR 2348, 2026-10-25): the
 * overlay sizes the readout's columns at minmax(118px, 1fr), about 136 at
 * 1440, and `.pot-cov` kept its table rule `white-space: nowrap`, so "N of N
 * attempts reported" drew 20px over the Cache read column beside it, "N of N
 * agents reported" 18px and "k estimated from a stopped run ($x)" 76px, over
 * the chart and under it, in both themes. jsdom lays nothing out, so the rule
 * that decides it is read: every cover in the readout wraps inside its cell.
 */
describe('a readout cover is never wider than its cell, by the rule (jsdom lays nothing out, so this reads white-space and min-width, not geometry)', () => {
  it('sets every cover in the readout to wrap, and no wide-plot rule sets it back to one line', async () => {
    const style = document.createElement('style')
    style.textContent = css
    document.head.appendChild(style)
    try {
      await drawn()
      fireEvent.click(hits()[BUCKETS.length - 1]!)
      const covers = Array.from(document.querySelectorAll<HTMLElement>('.pot-readout .pot-cov'))
      expect(covers.length, 'the readout drew no cover to measure').toBeGreaterThan(0)
      for (const c of covers) {
        // MUTATION: drop `.pot-readout .pot-cov { white-space: normal }`.
        expect(getComputedStyle(c).whiteSpace, `"${c.textContent}" stays on one line past its cell`).not.toBe('nowrap')
      }
      // Its grid cell may shrink to its column rather than grow past it.
      const row = document.querySelector<HTMLElement>('.pot-readout .pot-readout-row')!
      expect(getComputedStyle(row).minWidth, 'a readout cell grows past its column').toMatch(/^0(px)?$/)
    } finally {
      style.remove()
    }
    // jsdom skips a container rule, so the overlay's own block is read.
    const at = css.indexOf('@container pot-plot (min-width: 640px)')
    const block = css.slice(at, css.indexOf('\n}', at))
    expect(block, 'the overlay puts a readout cover back on one line').not.toMatch(/\.pot-cov\s*\{[^}]*white-space:\s*nowrap/)
    expect(block).not.toMatch(/\.pot-readout-row\s*\{[^}]*min-width:\s*(?!0)/)
  })
})

/**
 * THE OVERLAY STILL FITS WITH EVERY COVER ON ITS LINE (re-review of PR 2348,
 * 2026-10-25): wrapping the covers ended the overlap but gave up F6 -- each
 * long cover took a second line in its 136px column, the overlay's content
 * grew to 254-289px in its 248px box, and the fit measure sent 25 of 26 picked
 * readouts, and every hovered one with a stopped-run estimate, under the
 * chart, so the page under it jumped 153px as the pointer crossed the bars.
 *
 * jsdom lays nothing out, so the 1440 render's geometry is modelled from what
 * the review measured there: a column 136px wide (minmax(118px, 1fr) at that
 * plot width), 8px between columns, a cover about 5.6px a character ("N of N
 * attempts reported", 28 characters, drew 156px). A row takes as many columns
 * as the overlay's own rules in overtime.css give it (read here: jsdom skips a
 * container rule). Every cover that does not fit its cell adds its second
 * line, 17px, to the readout, which with every cover on one line is 236px in
 * the 248px it has.
 */
const COLUMN_PX = 136
const COLUMN_GAP_PX = 8
const GLYPH_PX = 5.6
const ONE_LINE_PX = 236
const ROOM_PX = 248
const SECOND_LINE_PX = 17

/** The overlay's rules that set a readout row's `grid-column: span N`, as [selector, N]. */
function overlaySpans(): Array<[string, number]> {
  const at = css.indexOf('@container pot-plot (min-width: 640px)')
  const block = css.slice(at, css.indexOf('\n}', at)).replace(/\/\*[\s\S]*?\*\//g, '')
  return Array.from(block.matchAll(/([^{}]+)\{([^}]*)\}/g))
    .map((m) => [m[1]!.trim(), /grid-column:\s*span\s+(\d+)/.exec(m[2]!)?.[1]] as const)
    .filter((r): r is readonly [string, string] => r[1] !== undefined && /pot-readout-row/.test(r[0]))
    .map(([sel, n]) => [sel, Number(n)])
}

/** How many of the readout's covers would take a second line at 1440. */
function wrappedCovers(ro: HTMLElement): number {
  const spans = overlaySpans()
  let wrapped = 0
  for (const row of Array.from(ro.querySelectorAll<HTMLElement>('.pot-readout-row'))) {
    const span = spans.find(([sel]) => row.matches(sel))?.[1] ?? 1
    const cell = COLUMN_PX * span + COLUMN_GAP_PX * (span - 1)
    for (const c of Array.from(row.querySelectorAll('.pot-cov'))) if ((c.textContent ?? '').length * GLYPH_PX > cell) wrapped += 1
  }
  return wrapped
}

function laidOut1440(): () => void {
  const sh = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'scrollHeight')
  const ch = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'clientHeight')
  const content = (el: HTMLElement) => ONE_LINE_PX + SECOND_LINE_PX * wrappedCovers(el)
  Object.defineProperty(HTMLElement.prototype, 'scrollHeight', { configurable: true, get() { return this.classList.contains('pot-readout') ? content(this) : 0 } })
  Object.defineProperty(HTMLElement.prototype, 'clientHeight', {
    configurable: true,
    get() { return this.classList.contains('pot-readout') ? (this.dataset.place === 'under' ? content(this) : ROOM_PX) : 0 },
  })
  return () => {
    Object.defineProperty(HTMLElement.prototype, 'scrollHeight', sh ?? { configurable: true, value: 0 })
    Object.defineProperty(HTMLElement.prototype, 'clientHeight', ch ?? { configurable: true, value: 0 })
  }
}

describe('the overlaid readout keeps every cover on one line at 1440, so it stays over the chart (F6)', () => {
  it('stays over the chart picked and hovered, with a stopped-run estimate too', async () => {
    // Every third week carries a stopped-run estimate, as the dev fixture does.
    const estimated: UsageBucket[] = BUCKETS.map((b, i) => {
      if (i % 3 !== 0) return b
      const f = figures(100 + i * 9, USD[i]!)
      return { start: b.start, end: b.end, state: 'sealed', in_progress: false, unread_reason: null, ...f, cost: { ...f.cost, estimated: 40, estimated_usd: 123.45 } }
    })
    const p = payload()
    if (p.status === 'ok') p.data.usage.buckets = estimated
    loadPlatformTime.mockResolvedValue(p)
    const restore = laidOut1440()
    try {
      render(<PlatformOverTime workspaces={2} />)
      fireEvent.click(screen.getByRole('button', { name: /^Run/ }))
      await waitFor(() => expect(document.querySelector('.pot-agents')).not.toBeNull())
      let seen = 0
      for (let i = 0; i < estimated.length; i += 1) {
        // MUTATION: drop the overlay's `grid-column: span 2` on the long-cover
        // rows, and "100 of 100 attempts reported" takes a second line in its
        // 136px column, the readout outgrows its room and goes under the chart.
        fireEvent.click(hits()[i]!)
        const picked = document.querySelector<HTMLElement>('.pot-readout')!
        expect(picked.querySelectorAll('.pot-cov').length, 'the readout drew no cover to measure').toBeGreaterThan(0)
        expect(wrappedCovers(picked), `week ${i}: a cover takes a second line over the chart`).toBe(0)
        expect(picked.hasAttribute('data-place'), `week ${i}: the picked readout went under the chart`).toBe(false)
        fireEvent.keyDown(document.querySelector<HTMLElement>('.pot-agents .pot-chart')!, { key: 'Escape' })
        fireEvent.mouseEnter(hits()[i]!)
        const hovered = document.querySelector<HTMLElement>('.pot-readout')!
        expect(hovered.hasAttribute('data-place'), `week ${i}: the hovered readout went under the chart`).toBe(false)
        fireEvent.mouseLeave(hits()[i]!.closest('svg')!)
        seen += 1
      }
      expect(seen).toBe(estimated.length)
      expect(estimated.some((b) => (b.cost?.estimated ?? 0) > 0), 'no week carried an estimate').toBe(true)
    } finally {
      restore()
    }
  })
})

/**
 * THE AXIS LABELS. A label is --t-micro, 12px, in the drawing's own units,
 * and overtime.css lets a drawing grow to 1.2 times its authored width. Two
 * value labels need their 12 units plus 4 between them; two x labels need
 * their text's width (a mono glyph is about 0.62em) plus 6.
 */
const FONT = 12

/** Each tick label's y on one axis: its text's own y under the tick group's translate. */
function valueLabelYs(axis: Element): number[] {
  return Array.from(axis.querySelectorAll('.ctl-chart-tick')).map((g) => {
    const shift = /translate\(\s*[-\d.]+\s*,\s*([-\d.]+)\s*\)/.exec(g.getAttribute('transform') ?? '')
    return Number(shift?.[1] ?? 0) + Number(g.querySelector('text')?.getAttribute('y') ?? NaN)
  })
}

describe('the axis labels never overlap', () => {
  it('keeps every two value labels on an axis a label and a gap apart, in every drawing', async () => {
    await drawn()
    const svgs = Array.from(document.querySelectorAll('.pot-cost svg.pot-svg, .pot-agents svg.pot-svg'))
    expect(svgs.length, 'no drawings to measure').toBeGreaterThanOrEqual(5)
    let pairs = 0
    for (const svg of svgs) {
      for (const axis of Array.from(svg.querySelectorAll('.ctl-chart-axis'))) {
        const ys = valueLabelYs(axis).sort((a, b) => a - b)
        expect(ys.every(Number.isFinite), 'a tick label with no position').toBe(true)
        for (let i = 1; i < ys.length; i += 1) {
          pairs += 1
          // MUTATION: pass VALUE_LABEL_GAP_PX (14) as this view's axis gap, and
          // "$445" sits 14.6 units over "$400": two 12px labels 2.6 apart.
          expect(ys[i]! - ys[i - 1]!, `${svg.getAttribute('aria-label')}: labels ${ys[i - 1]} and ${ys[i]} overlap`).toBeGreaterThanOrEqual(FONT + 4)
        }
      }
    }
    expect(pairs, 'no two labels on any axis, so nothing was measured').toBeGreaterThan(0)
  })

  it('thins the x labels so no two touch, in every drawing', async () => {
    await drawn()
    const svgs = Array.from(document.querySelectorAll('.pot-cost svg.pot-svg, .pot-agents svg.pot-svg'))
    let pairs = 0
    for (const svg of svgs) {
      const xs = Array.from(svg.querySelectorAll('text.pot-x')).map((t) => ({ x: Number(t.getAttribute('x')), w: (t.textContent ?? '').length * FONT * 0.62 }))
      for (let i = 1; i < xs.length; i += 1) {
        pairs += 1
        expect(xs[i]!.x - xs[i - 1]!.x, `${svg.getAttribute('aria-label')}: x labels touch`).toBeGreaterThanOrEqual((xs[i]!.w + xs[i - 1]!.w) / 2 + 6)
      }
    }
    expect(pairs).toBeGreaterThan(0)
  })
})
