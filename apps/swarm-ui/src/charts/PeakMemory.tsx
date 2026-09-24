// Peak memory REACHED BY T+n, for one attempt (redesign-v2 §4 viz #4).
//
// WHAT THIS IS, AND THE ONE WORD THAT KEEPS IT TRUE. Every fifth heartbeat
// carries `peak_rss_bytes`, and that figure is a RUNNING MAXIMUM: the worker's
// `merge_rss` is `max()` and never resets. Labelled "memory" it would claim a
// flat footprint for an agent that spiked once at minute two and released it.
// Labelled "peak reached by T+n" it is exactly true, so that is the title,
// the axis is T+ from the attempt's start, and the line is a STEP that holds
// each reading until the next -- the lower bound, which is all that is known
// between two samples. Never smoothed.
//
// WHY T+ IS MEASURED FROM `started_at` AND NOT FROM `elapsed_seconds`. The
// heartbeat does carry `elapsed_seconds`, and redesign-v2 names it. It is the
// AGENT CHILD's clock, and before the child exists the worker writes `0`
// (lifecycle.py: `round(self._child.elapsed_seconds, 1) if self._child else
// 0`). Every heartbeat taken during clone and restore would therefore sit at
// T+0 whenever it was actually taken -- a measured-looking zero that is a
// placeholder. The event's own `at` minus the attempt's `started_at` is a
// real position for every reading, and it is the same origin the checkpoint
// strip beside this uses, so the two can be read against each other.
//
// THE FINAL FIGURE IS NOT JOINED TO THE LINE. `attempt.peak_rss_bytes` is
// written once, at the end. It is drawn as its own mark at the attempt's end
// and the step line does not run to it: the heartbeats between the last one
// on this page and the end may exist and be off the page, and a stroke across
// them would be a line across an absence.
//
// Rendered only when at least one heartbeat for this attempt is on the page.
// With none, the requested-vs-utilised bar above already carries the absence
// in its own encoding, and a chart of nothing would restate it in a second.

import { EVENT_PAGE_LIMIT } from '../api'
import { spanText, instant } from '../duration'
import { bytesLabel, type AttemptRow, type TaskEvent } from '../types'
import { ChartTitle, HatchDef, useHatchId } from './parts'
import { reading, valueExtent, type ChartPoint, type Extent } from './series'
import { StepLine, ValueAxis, linearScale, type LinearScale } from './TimeSeries'

const W = 640
const H = 120
const M = { top: 8, right: 16, bottom: 22, left: 64 }
const SLIVER = 8

const NO_READING =
  'This heartbeat carried no memory reading: the sampler had not produced one when it was written. An absent reading, not zero bytes.'

function peakOf(e: TaskEvent): number | null {
  const v = e.detail?.['peak_rss_bytes']
  return typeof v === 'number' && Number.isFinite(v) ? v : null
}

/**
 * The readings, exported so the test builds them from the code the chart
 * uses. `unplaced` counts heartbeats whose `at` did not parse: they have no
 * position on the axis, so they are left out and COUNTED, never dropped.
 */
export function peakReadings(
  attempt: AttemptRow,
  events: readonly TaskEvent[] | null,
): { points: ChartPoint[]; unplaced: number; origin: number | null } {
  const origin = instant(attempt.started_at)
  const points: ChartPoint[] = []
  let unplaced = 0
  if (origin === null) return { points, unplaced, origin }
  for (const e of events ?? []) {
    if (e.type !== 'heartbeat' || e.attempt_id !== attempt.attempt_id) continue
    const t = instant(e.at)
    if (t === null) {
      unplaced += 1
      continue
    }
    const at = t - origin
    points.push(reading(at, `T+${spanText(at)}`, peakOf(e), NO_READING))
  }
  return { points, unplaced, origin }
}

export function PeakMemoryChart({
  attempt,
  events,
}: {
  attempt: AttemptRow
  events: TaskEvent[] | null
}) {
  const hatchId = useHatchId()
  const { points, unplaced, origin } = peakReadings(attempt, events)
  if (origin === null || (points.length === 0 && unplaced === 0)) return null

  // The end-of-attempt figure, placed at the attempt's recorded end.
  const end = instant(attempt.completed_at)
  const exit =
    attempt.peak_rss_bytes !== null && end !== null
      ? { at: end - origin, value: attempt.peak_rss_bytes }
      : null

  const measuredValues: ChartPoint[] = exit
    ? [...points, { at: exit.at, label: 'at exit', measured: true, value: exit.value }]
    : points
  const vExtent = valueExtent(measuredValues, 'anchored')
  const ats = measuredValues.map((p) => p.at)
  const tLo = Math.min(0, ...ats)
  const tHi = Math.max(0, ...ats)
  const tExtent: Extent = { lo: tLo, hi: tHi, degenerate: tLo === tHi }
  const measuredN = points.filter((p) => p.measured).length
  const pageFull = events !== null && events.length >= EVENT_PAGE_LIMIT

  // The largest reading and WHEN it was reached, from the readings alone.
  let top: { at: number; value: number } | null = null
  for (const p of points) {
    if (p.measured && (top === null || p.value > top.value)) top = { at: p.at, value: p.value }
  }

  const innerW = W - M.left - M.right
  const innerH = H - M.top - M.bottom
  const x = linearScale(tExtent, [0, innerW])

  return (
    <figure className="ctl-chart ctl-peak" aria-label="Peak RSS reached by T+n">
      <ChartTitle>Peak RSS reached by T+n</ChartTitle>
      {vExtent === null ? (
        // NOTHING MEASURED: no value axis, because an axis over an empty
        // domain is a range the data does not support. The heartbeats still
        // happened, so they are counted.
        <p className="ctl-chart-note" role="status" data-testid="peak-none">
          <span className="ctl-chart-key" aria-hidden="true" /> {points.length} heartbeats · no reading
        </p>
      ) : (
        <svg
          className="ctl-chart-svg"
          width={W}
          height={H}
          viewBox={`0 0 ${W} ${H}`}
          role="group"
          aria-label="Peak resident memory reached by each heartbeat, from the start of this attempt"
        >
          <HatchDef id={hatchId} />
          <g transform={`translate(${M.left},${M.top})`}>
            <PeakPlot
              points={points}
              exit={exit}
              x={x}
              vExtent={vExtent}
              tExtent={tExtent}
              innerH={innerH}
              hatchId={hatchId}
            />
          </g>
        </svg>
      )}
      <figcaption className="ctl-chart-cov" data-testid="peak-cov" data-partial={measuredN < points.length || unplaced > 0 ? 'yes' : 'no'}>
        {top !== null && (
          <>
            peak <strong>{bytesLabel(top.value)}</strong> by T+{spanText(top.at)} ·{' '}
          </>
        )}
        {measuredN} of {points.length + unplaced} heartbeats
        {exit !== null && <> · at exit {bytesLabel(exit.value)}</>}
        {pageFull && (
          <>
            {' '}
            ·{' '}
            <span
              className="ctl-chart-flag"
              role="img"
              aria-label={`The events page is full at ${EVENT_PAGE_LIMIT}, the route's cap, and it returns no page token — so later heartbeats for this attempt may exist and the line ends where the page does, not where the attempt did.`}
            >
              page full
            </span>
          </>
        )}
      </figcaption>
    </figure>
  )
}

function PeakPlot({
  points,
  exit,
  x,
  vExtent,
  tExtent,
  innerH,
  hatchId,
}: {
  points: readonly ChartPoint[]
  exit: { at: number; value: number } | null
  x: LinearScale
  vExtent: Extent
  tExtent: Extent
  innerH: number
  hatchId: string
}) {
  const y = linearScale(vExtent, [innerH, 0])
  return (
    <>
      {points.map((p) =>
        p.measured ? null : (
          <rect
            key={`absent-${p.at}`}
            className="ctl-chart-absent"
            data-testid="absent-band"
            x={x(p.at) - SLIVER / 2}
            y={0}
            width={SLIVER}
            height={innerH}
            fill={`url(#${hatchId})`}
          >
            <title>{`${p.label} — not measured`}</title>
          </rect>
        ),
      )}
      <StepLine points={points} x={(at) => x(at)} y={(v) => y(v)} />
      {points.map((p) =>
        p.measured ? (
          <circle
            key={`dot-${p.at}`}
            className={p.value === 0 ? 'ctl-chart-dot is-zero' : 'ctl-chart-dot'}
            data-testid="measured-dot"
            data-value={p.value}
            cx={x(p.at)}
            cy={y(p.value)}
            r={p.value === 0 ? 3.5 : 2.75}
          >
            <title>{`${p.label} — ${bytesLabel(p.value)}`}</title>
          </circle>
        ) : null,
      )}
      {exit !== null && (
        // A diamond, so the end-of-attempt figure is never mistaken for one
        // more heartbeat: it comes from a different write, at a different time.
        <path
          className="ctl-chart-exit"
          data-testid="exit-mark"
          d={`M${x(exit.at)},${y(exit.value) - 5} l5,5 l-5,5 l-5,-5 Z`}
        >
          <title>{`at exit — ${bytesLabel(exit.value)}`}</title>
        </path>
      )}
      <ValueAxis side="left" scale={y} extent={vExtent} format={bytesLabel} minGapPx={14} />
      <ValueAxis
        side="bottom"
        top={innerH}
        scale={x}
        extent={tExtent}
        format={(v) => `T+${spanText(v)}`}
        minGapPx={64}
      />
    </>
  )
}
