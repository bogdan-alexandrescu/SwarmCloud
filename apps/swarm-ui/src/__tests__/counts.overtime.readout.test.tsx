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
