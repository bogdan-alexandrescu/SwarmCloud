// Token spend, attempt by attempt: the first chart drawn through the wrapper.
//
// WHY THIS SERIES AND NOT A TIDIER ONE. Six of the thirteen attempts in the
// live deployment carry no cost figure at all, so this is a genuinely mixed
// series of measured and absent points -- including measured ZEROES -- which
// is the exact case naive charting gets wrong. A chart whose data happened to
// be complete would exercise none of the rules the wrapper enforces.
//
// This module imports `TimeSeries`. It does NOT import visx, and nothing on a
// screen does: `charts.wrapper.test.ts` fails if any file outside
// `src/charts/TimeSeries.tsx` reaches for the library.

import { reading, type ChartPoint } from './series'
import { TimeSeries } from './TimeSeries'
import type { AttemptRow } from '../types'

/**
 * Money, as a STRING, for the axis ticks and the coverage line.
 *
 * Two decimals everywhere would round a measured $0.0043 to `$0.00`, which
 * makes a real measurement read as a zero -- the same lie this product
 * refuses, arriving from the other direction. So a non-zero figure under a
 * cent keeps four decimals, and only a value that IS zero prints `$0.00`.
 */
export function usdText(v: number): string {
  if (v === 0) return '$0.00'
  if (Math.abs(v) < 0.01) return `$${v.toFixed(4)}`
  return `$${v.toFixed(2)}`
}

/**
 * Why this attempt has no cost, in the words the attempt itself supports.
 *
 * The four cases are `AttemptSpend`'s, kept in the same order and to the same
 * meanings. They are four different sentences on purpose: telling the owner of
 * a RUNNING attempt that its numbers are missing sends them to rebuild an
 * image over an attempt that is working correctly.
 *
 * NOTE THE THIRD CASE. An attempt that never started consumed no tokens, and
 * it is still plotted as an ABSENCE rather than as a measured zero: nothing
 * wrote `cost_usd: 0` on that document, and a client that supplies the zero is
 * re-deriving a measurement the API never made. That is the defect class
 * `test_blocker_ui_surface.py` §1 exists to catch, in a chart.
 */
export function whyNoCost(a: AttemptRow, profile: string): string {
  const reports = profile === 'claude-code' || profile === 'codex'
  if (!reports) {
    return `the ${profile} runner does not report cost at all, so this is an absence of measurement rather than a run that cost nothing.`
  }
  if (a.completed_at === null && a.started_at !== null) {
    return 'this attempt has not finished. Spend is parsed out of the runner’s result and written when the attempt ends.'
  }
  if (a.started_at === null) {
    return 'this attempt never started. No document records a cost for it, and this client will not supply the zero the platform did not write.'
  }
  return 'this attempt finished and recorded no usage — attempts that ran before the worker capture shipped carry null for every spend field. An absent measurement, not a free run.'
}

/**
 * The points, exported so the test can build them from the same code the
 * screen does rather than from a fixture that could drift away from it.
 *
 * `created_at` is ISO-8601 UTC from `attempt_to_api`. An attempt whose
 * timestamp does not parse has no position on a time axis -- that is an absent
 * INSTANT, not an absent value, and there is nowhere honest to draw it. Those
 * are dropped from the series and counted back in the note below, so the chart
 * never silently covers fewer attempts than it claims.
 */
export function spendPoints(
  attempts: readonly AttemptRow[],
  profile: string,
): { points: ChartPoint[]; undatable: number } {
  const points: ChartPoint[] = []
  let undatable = 0
  attempts.forEach((a, i) => {
    const at = Date.parse(a.created_at)
    if (!Number.isFinite(at)) {
      undatable += 1
      return
    }
    points.push(reading(at, `attempt ${i + 1}`, a.cost_usd, whyNoCost(a, profile)))
  })
  return { points, undatable }
}

export function TokenSpendChart({
  attempts,
  profile,
}: {
  attempts: readonly AttemptRow[]
  profile: string
}) {
  const { points, undatable } = spendPoints(attempts, profile)

  // A chart of one point is a worse rendering of one number than the number
  // is. The attempt cards below already carry it, in full sentences.
  if (points.length < 2) return null

  return (
    <div className="ctl-chart-wrap">
      <TimeSeries
        points={points}
        title="Token spend, attempt by attempt"
        noun="attempt"
        format={usdText}
        absentCopy="an absent measurement, not $0.00. Hover a hatched band for the reason that attempt has no figure."
        // Money has a meaningful zero, and a spend axis that started at the
        // cheapest attempt would make a 3-cent spread look like a cliff.
        zero="anchored"
      />
      {undatable > 0 && (
        <p className="ctl-chart-note" role="status">
          {undatable} attempt{undatable === 1 ? '' : 's'} could not be placed on
          the time axis because {undatable === 1 ? 'its' : 'their'}{' '}
          <code>created_at</code> did not parse, so {undatable === 1 ? 'it is' : 'they are'}{' '}
          not in this chart at all.
        </p>
      )}
    </div>
  )
}
