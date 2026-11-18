// The one real chart, on the shape of data the live deployment actually has:
// thirteen attempts of which six carry no cost figure at all.
//
// This is the case the audit says breaks naive charting, and it is why this
// series was chosen to exercise the wrapper rather than a tidy one. Six
// absences, a measured zero among the seven that reported, and four DIFFERENT
// reasons for an absence -- a runner that does not report, an attempt still
// running, an attempt that never started, and an image too old to capture.

import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { TokenSpendChart, spendPoints, usdText, whyNoCost } from '../charts/TokenSpend'
import type { AttemptRow } from '../types'

const T0 = Date.UTC(2026, 8, 22, 9, 0, 0)
const HOUR = 3_600_000

function attempt(i: number, over: Partial<AttemptRow> = {}): AttemptRow {
  return {
    attempt_id: `att_${i}`,
    task_id: 'tsk_1',
    tenant_id: 'ten_1',
    generation: i,
    lease_id: `lease_${i}`,
    backend: 'CLOUD_RUN',
    execution_name: `exec-${i}`,
    created_at: new Date(T0 + i * HOUR).toISOString(),
    started_at: new Date(T0 + i * HOUR + 60_000).toISOString(),
    completed_at: new Date(T0 + i * HOUR + 600_000).toISOString(),
    exit_code: 0,
    error: null,
    peak_rss_bytes: 1_000_000,
    peak_disk_bytes: null,
    oom_near_miss: false,
    checkpoints: [],
    input_tokens: 1000,
    output_tokens: 200,
    cache_read_input_tokens: null,
    cache_creation_input_tokens: null,
    cost_usd: 0.25,
    ...over,
  }
}

/** Thirteen attempts; six with no cost, for four different reasons. */
function thirteen(): AttemptRow[] {
  const costs: Array<number | null> = [
    0.25, null, 0.31, 0, 0.12, null, null, 0.4, null, 0.18, null, 0.09, null,
  ]
  return costs.map((c, i) => {
    const over: Partial<AttemptRow> = { cost_usd: c }
    if (i === 6) {
      // Still running: no completion, so no spend has been written yet.
      over.completed_at = null
    }
    if (i === 8) {
      // Never started: admitted, then the attempt was fenced away.
      over.started_at = null
      over.completed_at = null
    }
    return attempt(i, over)
  })
}

/**
 * ONE DRAWING'S MARKS. The chart is drawn once per width (AG-20: a 640-unit
 * and a 300-unit SVG, and the sheet shows one), so a count over the whole
 * figure counts every band twice. The first root is the wide drawing; the
 * pair itself is `chart.narrow.test.tsx`'s to assert.
 */
function drawing(container: Element): Element {
  const svg = container.querySelector('svg.ctl-chart-svg')
  expect(svg, 'the chart drew no plot').not.toBeNull()
  return svg!
}

describe('token spend, attempt by attempt', () => {
  it('reports seven of thirteen attempts and never totals over all thirteen', () => {
    const { container } = render(
      <TokenSpendChart attempts={thirteen()} profile="claude-code" />,
    )
    const caption = container.querySelector('.ctl-chart-cov')?.textContent ?? ''

    // 0.25+0.31+0+0.12+0.40+0.18+0.09 = 1.35, over seven of thirteen.
    expect(caption).toContain('$1.35')
    expect(caption).toContain('7 of 13 attempts that reported')
    expect(caption).not.toContain('13 attempts.')

    // Six absences drawn, six absences named.
    expect(drawing(container).querySelectorAll('[data-testid="absent-band"]')).toHaveLength(6)
    expect(container.querySelector('.ctl-chart-note')?.textContent).toContain(
      '6 of 13 attempts reported no value',
    )
  })

  it('renders the measured zero as a zero and the absences as sentences', () => {
    const { container } = render(
      <TokenSpendChart attempts={thirteen()} profile="claude-code" />,
    )

    const zero = container.querySelector('[data-testid="measured-dot"][data-label="attempt 4"]')
    expect(zero?.getAttribute('data-value')).toBe('0')
    expect(zero?.querySelector('title')?.textContent).toContain('$0.00, measured')

    const reasons = [...drawing(container).querySelectorAll('[data-testid="absent-band"] title')].map(
      (t) => t.textContent ?? '',
    )
    expect(reasons).toHaveLength(6)
    // Four different sentences, not one repeated. The running attempt and the
    // one that never started must not be told the same thing.
    expect(reasons.some((r) => r.includes('has not finished'))).toBe(true)
    expect(reasons.some((r) => r.includes('never started'))).toBe(true)
    expect(reasons.some((r) => r.includes('before the worker capture shipped'))).toBe(true)
    for (const r of reasons) expect(r).toContain('not measured')
  })

  it('never plots a zero for an attempt that never started', () => {
    // It consumed no tokens, and NOTHING WROTE THAT DOWN. Supplying the zero
    // here would be the client re-deriving a measurement the API never made.
    const rows = thirteen()
    const { points } = spendPoints(rows, 'claude-code')
    const p = points[8]
    expect(p?.label).toBe('attempt 9')
    expect(p?.measured).toBe(false)
    expect(whyNoCost(rows[8] as AttemptRow, 'claude-code')).toContain('never started')
  })

  it('says the runner does not report at all, when that is the reason', () => {
    const { container } = render(
      <TokenSpendChart attempts={thirteen()} profile="mock" />,
    )
    // Every cost that IS present still plots; the absences get the runner's
    // reason rather than the image's.
    const reasons = [...container.querySelectorAll('[data-testid="absent-band"] title')].map(
      (t) => t.textContent ?? '',
    )
    for (const r of reasons) expect(r).toContain('the mock runner does not report cost at all')
  })

  it('drops an attempt whose created_at does not parse, and says it did', () => {
    const rows = [...thirteen(), attempt(99, { created_at: 'not-a-timestamp' })]
    const { container } = render(<TokenSpendChart attempts={rows} profile="claude-code" />)
    expect(container.textContent).toContain('could not be placed on the time axis')
    // Still thirteen plotted points, not fourteen.
    expect(container.querySelector('.ctl-chart-cov')?.textContent).toContain('7 of 13')
  })

  it('renders nothing at all for a single attempt', () => {
    const { container } = render(
      <TokenSpendChart attempts={[attempt(0)]} profile="claude-code" />,
    )
    expect(container.textContent).toBe('')
  })

  it('writes a measured sub-cent cost as a figure, never as $0.00', () => {
    // Rounding 0.0043 to "$0.00" makes a measurement read as a zero, which is
    // this product's own lie arriving from the other direction.
    expect(usdText(0.0043)).toBe('$0.0043')
    expect(usdText(0)).toBe('$0.00')
    expect(usdText(1.2)).toBe('$1.20')
  })
})

describe('the charting library is reachable from exactly one module', () => {
  // A SOURCE-TEXT ASSERTION, DELIBERATELY, and the only one in this file.
  //
  // It asserts an ABSENCE across the whole tree -- that no other module
  // imports visx -- and no render test can make that claim: a screen that
  // imported the library and used it correctly today produces identical DOM
  // to one that did not, and the rule would only break on the chart somebody
  // adds next month. The existing Python suite keeps the same class of check
  // for the same stated reason (`test_blocker_ui_surface.py` §1).
  it('only src/charts/TimeSeries.tsx imports the charting library', () => {
    // Every module under src/, as TEXT, through Vite's own resolver rather
    // than through `node:fs` -- which would need `@types/node` in an app that
    // has deliberately never had it.
    const sources = import.meta.glob('../**/*.{ts,tsx}', {
      eager: true,
      query: '?raw',
      import: 'default',
    }) as Record<string, string>

    // Assembled rather than written out, so this file is not its own first
    // offender. A scanner that cannot be run over itself is a scanner with an
    // exemption in it.
    const needle = ['@vi', 'sx/'].join('')

    const files = Object.keys(sources)
    expect(files.length, 'the source scan found no modules at all').toBeGreaterThan(20)

    const offenders = files.filter(
      (path) =>
        !path.endsWith('charts/TimeSeries.tsx') && (sources[path] ?? '').includes(needle),
    )
    expect(
      offenders,
      'a module other than the chart wrapper reached for the charting library directly; ' +
        'the absent-value rule is enforced in the wrapper and cannot be enforced anywhere else',
    ).toEqual([])
  })
})
