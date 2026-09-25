// Where each attempt's wall clock went, and how long the agent actually worked.
//
// Two drawings and one sentence, in one figure, because they answer one
// question from two sides:
//
//   1. PHASE BARS (redesign-v2 §4 viz #3). One row per attempt, queue / cold
//      start / run, on ONE shared axis whose zero is ADMISSION. The queue
//      extends left of zero, the container start and the run extend right.
//      §9's measured run -- 3m 09s of cold start, 18s of agent -- reads off
//      this as one long amber segment and one short blue one, which is the
//      distinction no screen drew before.
//
//   2. RETRY LOLLIPOP (viz #15). One stem per attempt, run time only, so a
//      retried task's attempts can be compared at a glance.
//
//   3. THE SUM, stated once under both: the per-attempt run intervals added
//      up, with how many attempts it covers. `task.started_at` is overwritten
//      on every attempt (control.py), so the task document cannot answer "how
//      long did the agent work"; only this sum can.
//
// WHY THE AXIS IS ANCHORED AT ADMISSION AND NOTHING IS STACKED. The audit's
// §B5 finding: a stacked bar given an unknown segment gives it height 0, its
// neighbours abut, and the bar silently shortens. Here every segment is drawn
// between two RECORDED instants measured from the attempt's own admission, so
// no segment's position depends on another's length. An unknown queue start
// removes the queue and nothing else; the cold start and run stay exactly
// where they were measured.
//
// ADMISSION IS THE `lease_acquired` EVENT, and a row can lack it. The worker
// rewrites `attempt.created_at` to its own start time (duration.ts says
// where), so for a started attempt whose `lease_acquired` is off the page,
// admission -- the zero of this axis -- is unknown. Its queue and cold start
// are hatched, and its run, still measured, is NOT drawn on the axis:
// starting it at the admission rule would be the §B5 defect again, an
// unknown cold start given zero width. Its length is the figure at the end of
// the row, and it is in the lollipop and the sum.
//
// OPEN IS DRAWN AS OPEN. A segment with no recorded end -- a run still going,
// a run whose end was never written, a container that never came up -- is a
// wash of its colour up to the newest instant this page holds for it (a LOWER
// bound, see duration.ts for which events count) and a chevron where an end
// cap would be. It is never closed at "now" and never closed at a guess; until
// the events route is paged, "at least this long" is all the page supports.

import { PHASE_LABEL, phaseExtent, phasesFor, spanText, workSum } from '../duration'
import type { AttemptPhases, Segment, WorkSum } from '../duration'
import type { AttemptRow, Task, TaskEvent } from '../types'
import { ChartTitle, DRAWN, HatchDef, drawnClass, useHatchId, type Drawn } from './parts'
import { ValueAxis, linearScale, type LinearScale } from './TimeSeries'

// Compact by the same D2 rule `TimeSeries` follows: a 10px bar in an 18px
// row (the dataviz spec caps a bar at 24px and lets the rest be air), and a
// right gutter wide enough for "≥ 23h 59m".
//
// NO WIDTH HERE (AG-20): each drawing in `DRAWN` (parts.tsx) brings its own,
// and both keep these margins, because "≥ 23h 59m" is as long at either.
const M = { top: 4, right: 76, bottom: 22, left: 24 }
const ROW = 18
const BAR = 10
/** Room left of the plot for an ABSENT queue's hatch, when no queue was measured. */
const PAD = 12
/** An absence's width: a region, not a mark, and never a duration. */
const SLIVER = 8

export function AttemptDurations({
  task,
  attempts,
  events,
}: {
  task: Task
  attempts: readonly AttemptRow[]
  events: TaskEvent[] | null
}) {
  const hatchId = useHatchId()
  const phases = phasesFor(task, attempts, events)
  const sum = workSum(phases)
  const rows = phases.rows
  if (rows.length === 0 && phases.undatable === 0) return null

  const anyOpen = rows.some((r) => [r.queue, r.cold, r.run].some((s) => s?.kind === 'open'))
  const anyAbsent = rows.some((r) => [r.queue, r.cold, r.run].some((s) => s?.kind === 'absent'))

  return (
    <figure className="ctl-chart ctl-phases has-narrow" aria-label="Attempt phases">
      <ChartTitle>Attempt phases</ChartTitle>
      {/* THE LEGEND IS THE IDENTITY. The series palette is not separable in
          greyscale (styles.css says so beside `--series-1`), so every hue is
          named here, and position carries it too: queue is always left of
          admission, run is always last. Open and absent get a key only when
          one is drawn, so the legend never names a mark the reader cannot
          find. */}
      <ul className="ctl-chart-legend">
        {(['queue', 'cold', 'run'] as const).map((p) => (
          <li key={p}>
            <span className={`ctl-swatch is-${p}`} aria-hidden="true" />
            {PHASE_LABEL[p]}
          </li>
        ))}
        {anyOpen && (
          <li>
            <span className="ctl-swatch is-open" aria-hidden="true" />
            open
          </li>
        )}
        {anyAbsent && (
          <li>
            <span className="ctl-chart-key" aria-hidden="true" />
            not measured
          </li>
        )}
      </ul>

      {rows.length > 0 && <PhaseBars rows={rows} hatchId={hatchId} />}
      {rows.length >= 2 && <RetryLollipop rows={rows} hatchId={hatchId} />}
      <SumLine sum={sum} />
    </figure>
  )
}

// ---------------------------------------------------------------------------
// 1. Phase bars
// ---------------------------------------------------------------------------

function PhaseBars({ rows, hatchId }: { rows: readonly AttemptPhases[]; hatchId: string }) {
  const extent = phaseExtent(rows)
  if (extent === null) return null
  // TWICE, NOT SCALED (AG-20): one drawing per width, each its own scale.
  return (
    <>
      {DRAWN.map((d) => (
        <PhaseDrawing key={d.key} d={d} rows={rows} extent={extent} hatchId={`${hatchId}-${d.key}`} />
      ))}
    </>
  )
}

function PhaseDrawing({
  d,
  rows,
  extent,
  hatchId,
}: {
  d: Drawn
  rows: readonly AttemptPhases[]
  extent: NonNullable<ReturnType<typeof phaseExtent>>
  hatchId: string
}) {
  const innerW = d.w - M.left - M.right
  const height = M.top + rows.length * ROW + M.bottom
  const x = linearScale(extent, [PAD, innerW])
  const zeroX = x(0)

  return (
    <svg
      className={drawnClass(d)}
      width={d.w}
      height={height}
      viewBox={`0 0 ${d.w} ${height}`}
      // A GROUP, NOT AN IMAGE. `role="img"` makes every child presentational,
      // which would hide each segment's own sentence -- the accessible name
      // that says "open, at least 5m" or "not measured, and why" -- behind
      // one label for the whole chart. The sentences are the reason the
      // segments carry `aria-label` at all.
      role="group"
      aria-label="Queue, cold start and run for each attempt, measured from admission"
    >
      <HatchDef id={hatchId} />
      <g transform={`translate(${M.left},${M.top})`}>
        {/* ADMISSION, the one instant every row shares. Dashed and hairline,
            because it is an axis and not a measurement. */}
        <line
          className="ctl-chart-zeroline"
          data-testid="admission-rule"
          x1={zeroX}
          x2={zeroX}
          y1={0}
          y2={rows.length * ROW}
        />
        {rows.map((r, i) => (
          <PhaseRow
            key={r.attempt.attempt_id}
            r={r}
            y={i * ROW + (ROW - BAR) / 2}
            x={x}
            innerW={innerW}
            hatchId={hatchId}
          />
        ))}
        <ValueAxis
          side="bottom"
          top={rows.length * ROW}
          scale={x}
          extent={extent}
          format={(v) => (v === 0 ? 'admitted' : spanText(v))}
          keep={[0]}
          ticks={d.ticks}
          minGapPx={52}
        />
      </g>
    </svg>
  )
}

function runStart(r: AttemptPhases): number {
  return r.cold.kind === 'closed' ? r.cold.to : 0
}

function PhaseRow({
  r,
  y,
  x,
  innerW,
  hatchId,
}: {
  r: AttemptPhases
  y: number
  x: LinearScale
  /** The plot's width in THIS drawing: the run figure sits past its end. */
  innerW: number
  hatchId: string
}) {
  return (
    <g data-testid="phase-row" data-attempt={r.ordinal}>
      <text className="ctl-chart-rowlabel" x={-6} y={y + BAR - 1} textAnchor="end">
        {r.ordinal}
      </text>
      <SegmentMark s={r.queue} ordinal={r.ordinal} y={y} x={x} hatchId={hatchId} anchor={0} />
      <SegmentMark s={r.cold} ordinal={r.ordinal} y={y} x={x} hatchId={hatchId} anchor={0} />
      {r.run !== null &&
        (r.placed ? (
          <SegmentMark s={r.run} ordinal={r.ordinal} y={y} x={x} hatchId={hatchId} anchor={runStart(r)} />
        ) : (
          <UnplacedRun s={r.run} ordinal={r.ordinal} />
        ))}
      {r.dispatchedAt !== null && r.dispatchedAt > 0 && (
        // The `dispatched` event inside the cold start: the backend call
        // returned here, and everything after it is the container coming up
        // -- the 3m 09s of §9. A surface-coloured cut, so it reads as a
        // boundary within one segment rather than as a fourth one.
        <line
          className="ctl-phase-cut"
          data-testid="dispatched-cut"
          x1={x(r.dispatchedAt)}
          x2={x(r.dispatchedAt)}
          y1={y}
          y2={y + BAR}
        >
          <title>{`dispatched +${spanText(r.dispatchedAt)}`}</title>
        </line>
      )}
      <text className="ctl-chart-rowlabel is-figure" x={innerW + 10} y={y + BAR - 1} data-testid="run-figure">
        {runFigure(r)}
      </text>
    </g>
  )
}

/** The run time at the end of the row: a figure, a lower bound, or the fact. */
function runFigure(r: AttemptPhases): string {
  if (r.run === null) return r.neverRan ? 'never ran' : '—'
  if (r.run.kind === 'closed') return spanText(r.run.ms)
  if (r.run.kind === 'open') return `≥ ${spanText(r.run.atLeastMs)}`
  return '—'
}

/** The sentence for one segment: published as its accessible name. */
function sentence(s: Segment, ordinal: number): string {
  const head = `Attempt ${ordinal} ${PHASE_LABEL[s.phase]}`
  if (s.kind === 'closed') return `${head}: ${spanText(s.ms)}, recorded.`
  if (s.kind === 'open') return `${head}: open, at least ${spanText(s.atLeastMs)}. ${s.why}`
  return `${head}: not measured. ${s.why}`
}

/** The tooltip for one segment: the phase and its figure, and nothing else. */
function tip(s: Segment): string {
  const label = PHASE_LABEL[s.phase]
  if (s.kind === 'closed') return `${label} ${spanText(s.ms)}`
  if (s.kind === 'open') return `${label} ≥ ${spanText(s.atLeastMs)} · open`
  return `${label} —`
}

function SegmentMark({
  s,
  ordinal,
  y,
  x,
  hatchId,
  anchor,
}: {
  s: Segment
  ordinal: number
  y: number
  x: LinearScale
  hatchId: string
  /** Where an ABSENT segment's hatch sits: the instant it would have started at. */
  anchor: number
}) {
  const common = {
    role: 'img' as const,
    'aria-label': sentence(s, ordinal),
    'data-phase': s.phase,
    'data-kind': s.kind,
  }

  if (s.kind === 'absent') {
    // A REGION WITH NO EXTENT OF ITS OWN. The width is fixed in pixels and
    // means nothing on the axis; the hatch is what says "not measured". A
    // queue's sits just left of admission, where the queue would have ended.
    const at = s.phase === 'queue' ? x(0) - SLIVER - 1 : x(anchor) + 1
    return (
      <g {...common} data-testid="phase-absent">
        <title>{tip(s)}</title>
        <rect
          className="ctl-chart-absent"
          x={at}
          y={y - 2}
          width={SLIVER}
          height={BAR + 4}
          fill={`url(#${hatchId})`}
        />
      </g>
    )
  }

  if (s.kind === 'closed') {
    const x0 = x(s.from)
    const x1 = x(s.to)
    // THE 2px SURFACE GAP between touching segments is the inset on each
    // side. A measured span too short to survive the inset keeps a 1px mark
    // rather than disappearing: a measured 0.1s is still a measurement.
    const width = Math.max(1, x1 - x0 - 2)
    return (
      <g {...common} data-testid="phase-closed" data-ms={s.ms}>
        <title>{tip(s)}</title>
        <rect className={`ctl-phase is-${s.phase}`} x={x0 + 1} y={y} width={width} height={BAR} />
      </g>
    )
  }

  // OPEN. A wash up to the lower bound, then a chevron where the end cap
  // would be. No closing edge is drawn, because no end was recorded.
  const x0 = x(s.from)
  const xs = x(s.seen)
  const width = Math.max(0, xs - x0 - 1)
  return (
    <g {...common} data-testid="phase-open" data-at-least-ms={s.atLeastMs} data-live={s.live ? 'yes' : 'no'}>
      <title>{tip(s)}</title>
      {width > 0 && (
        <rect className={`ctl-phase is-${s.phase} is-open`} x={x0 + 1} y={y} width={width} height={BAR} />
      )}
      <path
        className={`ctl-phase-chevron is-${s.phase}`}
        d={`M${xs + 1},${y} L${xs + 6},${y + BAR / 2} L${xs + 1},${y + BAR} Z`}
      />
    </g>
  )
}

/**
 * A run whose attempt has no known admission: measured, and not positioned.
 *
 * No geometry at all, on purpose. Every x on this axis is a distance from
 * admission, and this run's distance from admission IS the cold start that
 * is unknown. The mark keeps the phase, the kind and the sentence, so the
 * figure at the end of the row is still explained and a screen reader still
 * reaches it.
 */
function UnplacedRun({ s, ordinal }: { s: Segment; ordinal: number }) {
  return (
    <g
      role="img"
      aria-label={`${sentence(s, ordinal)} This attempt’s admission is not on this page, so where its run falls on this axis is unknown and it is not drawn on it; its length is the figure at the end of the row.`}
      data-phase={s.phase}
      data-kind={s.kind}
      data-placed="no"
      data-testid="phase-unplaced"
      // React leaves an attribute out when its value is undefined, so each
      // of these appears only on the kind it describes.
      data-ms={s.kind === 'closed' ? s.ms : undefined}
      data-at-least-ms={s.kind === 'open' ? s.atLeastMs : undefined}
      data-live={s.kind === 'open' ? (s.live ? 'yes' : 'no') : undefined}
    >
      <title>{tip(s)}</title>
    </g>
  )
}

// ---------------------------------------------------------------------------
// 2. Retry lollipop
// ---------------------------------------------------------------------------

const L = { top: 8, right: 14, bottom: 18, left: 56 }
const LH = 96
/** A stem per attempt, never wider apart than this: two attempts are not a spread. */
const BAND_MAX = 44

function RetryLollipop({ rows, hatchId }: { rows: readonly AttemptPhases[]; hatchId: string }) {
  let hi = 0
  for (const r of rows) {
    if (r.run?.kind === 'closed') hi = Math.max(hi, r.run.ms)
    if (r.run?.kind === 'open') hi = Math.max(hi, r.run.atLeastMs)
  }
  // A run time has a meaningful zero, so the axis is anchored there -- and it
  // ends at the longest RECORDED run or lower bound, never past it.
  const extent = { lo: 0, hi, degenerate: hi === 0 }
  const innerH = LH - L.top - L.bottom
  const y = linearScale(extent, [innerH, 0])

  return (
    <>
      <ChartTitle>Run per attempt</ChartTitle>
      {/* TWICE, NOT SCALED (AG-20). The value axis is vertical, so the two
          drawings share it; what the width changes is the stems' spacing.
          EACH DRAWS ITS OWN HATCH. The absent stem's pattern was defined only
          in the phase-bar SVG above, and a pattern inside an SVG the sheet
          hides does not paint into another one. */}
      {DRAWN.map((d) => {
        const innerW = d.w - L.left - L.right
        const band = Math.min(BAND_MAX, innerW / rows.length)
        const id = `${hatchId}-lolly-${d.key}`
        return (
          <svg
            key={d.key}
            className={drawnClass(d)}
            width={d.w}
            height={LH}
            viewBox={`0 0 ${d.w} ${LH}`}
            role="group"
            aria-label="Run time of each attempt"
          >
            <HatchDef id={id} />
            <g transform={`translate(${L.left},${L.top})`}>
              <line className="ctl-chart-zeroline" x1={0} x2={rows.length * band} y1={y(0)} y2={y(0)} />
              {rows.map((r, i) => (
                <Lolly key={r.attempt.attempt_id} r={r} cx={band * (i + 0.5)} y={y} innerH={innerH} hatchId={id} />
              ))}
              <ValueAxis side="left" scale={y} extent={extent} format={spanText} minGapPx={14} />
            </g>
          </svg>
        )
      })}
    </>
  )
}

function Lolly({
  r,
  cx,
  y,
  innerH,
  hatchId,
}: {
  r: AttemptPhases
  cx: number
  y: LinearScale
  innerH: number
  hatchId: string
}) {
  const label = (
    <text className="ctl-chart-rowlabel" x={cx} y={innerH + 13} textAnchor="middle">
      {r.ordinal}
    </text>
  )
  const base = y(0)

  if (r.run === null) {
    // NEVER STARTED: no run, and a known one -- nothing ran. The hollow ring
    // on the zero rule is the product's mark for a MEASURED zero
    // (`.ctl-chart-dot.is-zero`); a run that has not started YET gets no mark
    // at all, because its run time is not zero, it is not begun.
    return (
      <g data-testid="lolly" data-attempt={r.ordinal} data-kind={r.neverRan ? 'never-ran' : 'pending'}>
        {r.neverRan && (
          <circle className="ctl-chart-dot is-zero" cx={cx} cy={base} r={3.5}>
            <title>{`${r.ordinal} · never ran`}</title>
          </circle>
        )}
        {label}
      </g>
    )
  }

  const s = r.run
  if (s.kind === 'absent') {
    return (
      <g data-testid="lolly" data-attempt={r.ordinal} data-kind="absent" role="img" aria-label={sentence(s, r.ordinal)}>
        <rect
          className="ctl-chart-absent"
          x={cx - SLIVER / 2}
          y={0}
          width={SLIVER}
          height={innerH}
          fill={`url(#${hatchId})`}
        >
          <title>{`${r.ordinal} · run —`}</title>
        </rect>
        {label}
      </g>
    )
  }

  if (s.kind === 'open') {
    const top = y(s.atLeastMs)
    return (
      <g data-testid="lolly" data-attempt={r.ordinal} data-kind="open" role="img" aria-label={sentence(s, r.ordinal)}>
        <title>{`${r.ordinal} · ≥ ${spanText(s.atLeastMs)} · open`}</title>
        <line className="ctl-lolly-stem is-open" x1={cx} x2={cx} y1={base} y2={top} />
        {/* An arrowhead, not a dot: the stem reaches a lower bound and the
            value is somewhere above it. */}
        <path className="ctl-lolly-open" d={`M${cx - 4},${top + 4} L${cx},${top - 1} L${cx + 4},${top + 4}`} />
        {label}
      </g>
    )
  }

  const top = y(s.ms)
  return (
    <g data-testid="lolly" data-attempt={r.ordinal} data-kind="closed" data-ms={s.ms}>
      <line className="ctl-lolly-stem" x1={cx} x2={cx} y1={base} y2={top} />
      <circle className={s.ms === 0 ? 'ctl-chart-dot is-zero' : 'ctl-chart-dot'} cx={cx} cy={top} r={4}>
        <title>{`${r.ordinal} · ${spanText(s.ms)}`}</title>
      </circle>
      {label}
    </g>
  )
}

// ---------------------------------------------------------------------------
// 3. The sum
// ---------------------------------------------------------------------------

/**
 * Summed agent work, and what it covers, in one line.
 *
 * Rendered from the same `WorkSum` the total comes from, so the total cannot
 * be shown without its coverage -- `TimeSeries`' coverage rule, applied to a
 * duration. An open attempt's lower bound is stated AFTER the total and is
 * never inside it.
 *
 * `n` is the attempts the TASK records, not the documents that came back, so
 * a read that returned 50 of 83 says "of 83" and names the 33 it did not see.
 */
function SumLine({ sum }: { sum: WorkSum }) {
  const covered = sum.closed + sum.neverRan
  return (
    <figcaption className="ctl-chart-cov" data-testid="work-sum" data-partial={sum.partial ? 'yes' : 'no'}>
      ran{' '}
      {sum.ms === null ? <span className="ctl-em">—</span> : <strong>{spanText(sum.ms)}</strong>} over{' '}
      <strong>
        {covered} of {sum.attempts}
      </strong>
      {sum.open > 0 && (
        <>
          {' '}
          · {sum.open} open, ≥ {spanText(sum.openAtLeastMs)}
        </>
      )}
      {sum.neverRan > 0 && <> · {sum.neverRan} never ran</>}
      {sum.absent > 0 && <> · {sum.absent} unreadable</>}
      {sum.undatable > 0 && <> · {sum.undatable} undated</>}
      {sum.missing > 0 && <> · {sum.missing} not returned</>}
    </figcaption>
  )
}
