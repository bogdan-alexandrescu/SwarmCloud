// CPU cores and memory over an attempt's run, each against its limit, with
// the CPU-throttled windows shaded (LIVE-UTIL-UI; part of #1689).
//
// WHAT IT REPLACES ON THE GLASS. Details said an attempt's CPU limit, its peak
// and its mean, and memory as a peak. The owner, 2026-10-20: "those will
// always show maximum when in fact the pods do not run at max cpu the entire
// time". The utilisation route (`swarm_api.utilisation`, #1696) serves one
// point per 5 s window -- cores used over the window, memory at its end, the
// share of its CFS periods that were throttled -- and this draws them.
//
// TWO PLOTS ON ONE TIME AXIS, not two series on one: cores and bytes share no
// scale, and a second y axis on the right is the chart every reader reads
// against the wrong axis. Each plot's value axis runs from 0 to the larger of
// its limit and its largest reading, so the limit is a dashed line at the top
// and every reading sits under it (or over it, visibly, if a limit were ever
// exceeded).
//
// LINES STRAIGHT BETWEEN SAMPLES (`SampleLine`), broken at a window the
// worker could not read, which is a hatched sliver. A reading is a window's
// mean, not a running maximum, so it is not held forward as PeakMemory holds
// its peak.
//
// THROTTLED WINDOWS ARE MEASURED, not inferred from a reading at the limit:
// `throttled_share` is the kernel's nr_throttled over nr_periods for that
// window. A window with any throttled period is shaded across the CPU plot;
// a window whose throttling was not read is not.
//
// NOTHING MEASURED IS A SENTENCE, never an empty axis or a zero: "no samples
// yet" while the attempt runs and has not beaten, "not measured" once it
// ended with none (or with every sample unread).
//
// Drawn twice, not scaled (AG-20, `DRAWN` in parts.tsx), so a phone shows the
// 300-unit drawing at the full width of its column. Colours are the series
// and state tokens styles.css defines for both themes.

import { spanText } from '../duration'
import { bytesLabel } from '../types'
import { cpuSummary, cpuSummaryText, throttledSpans, type UtilisationAttempt } from '../utilisation'
import { ChartTitle, DRAWN, HatchDef, VALUE_LABEL_GAP_PX, drawnClass, useHatchId, type Drawn } from './parts'
import { reading, valueExtent, type ChartPoint, type Extent } from './series'
import { SampleLine, ValueAxis, linearScale, type LinearScale } from './TimeSeries'

const PLOT_H = 72
const GAP = 14
const M = { top: 8, right: 16, bottom: 22, left: 64 }
const H = M.top + PLOT_H + GAP + PLOT_H + M.bottom
const SLIVER = 4

const NO_CPU = 'The worker could not read the CPU counters for this window. An absent reading, not zero cores.'
const NO_MEM = 'The worker could not read memory for this window. An absent reading, not zero bytes.'

function coresLabel(v: number): string {
  return `${Number(v.toFixed(2))}`
}

interface Plotted {
  origin: number
  cpu: ChartPoint[]
  mem: ChartPoint[]
}

function plotted(a: UtilisationAttempt): Plotted | null {
  const dated = a.points
    .map((p) => ({ p, t: Date.parse(p.at) }))
    .filter((d) => Number.isFinite(d.t))
    .sort((x, y) => x.t - y.t)
  const first = dated[0]
  if (first === undefined) return null
  const origin = first.t
  return {
    origin,
    cpu: dated.map(({ p, t }) => reading(t - origin, `T+${spanText(t - origin)}`, p.cpu_cores, NO_CPU)),
    mem: dated.map(({ p, t }) => reading(t - origin, `T+${spanText(t - origin)}`, p.memory_bytes, NO_MEM)),
  }
}

/** The plot's value extent: 0 to the larger of the limit and the readings. */
function withLimit(points: readonly ChartPoint[], limit: number | null): Extent | null {
  const base = valueExtent(points, 'anchored')
  if (base === null) return null
  const hi = limit === null ? base.hi : Math.max(base.hi, limit)
  return { lo: base.lo, hi, degenerate: base.lo === hi }
}

/**
 * The chart's state in one line, for the disclosure that holds it: the CPU
 * summary when anything was measured, else which kind of nothing. Exported so
 * Details' summary row and the chart cannot word the same state two ways.
 */
export function utilisationHeadline(attempt: UtilisationAttempt | null, live: boolean): { measured: boolean; text: string } {
  const data = attempt === null ? null : plotted(attempt)
  const any =
    attempt !== null &&
    data !== null &&
    (valueExtent(data.cpu, 'anchored') !== null || valueExtent(data.mem, 'anchored') !== null)
  if (any && attempt !== null) return { measured: true, text: cpuSummaryText(cpuSummary(attempt)) }
  const text =
    attempt !== null && data === null && live
      ? 'no samples yet: the first heartbeat carries them, within a few minutes of the start'
      : attempt === null && live
        ? 'no samples yet: nothing has been admitted'
        : 'not measured: no CPU or memory sample was recorded for this attempt'
  return { measured: false, text }
}

export function UtilisationChart({
  attempt,
  live,
}: {
  /** The attempt drawn: the newest the route served, or null when it served none. */
  attempt: UtilisationAttempt | null
  /** Whether the task is still running, which decides which kind of nothing. */
  live: boolean
}) {
  const hatchId = useHatchId()
  const data = attempt === null ? null : plotted(attempt)
  const cpuExtent = data === null || attempt === null ? null : withLimit(data.cpu, attempt.cpu_limit_cores)
  const memExtent = data === null || attempt === null ? null : withLimit(data.mem, attempt.memory_limit_bytes)

  if (attempt === null || data === null || (cpuExtent === null && memExtent === null)) {
    const words = utilisationHeadline(attempt, live).text
    return (
      <figure className="ctl-chart ctl-util" aria-label="CPU and memory over time">
        <ChartTitle>CPU and memory over time</ChartTitle>
        <p className="ctl-chart-note" role="status" data-testid="util-none">
          <span className="ctl-chart-key" aria-hidden="true" /> {words}
        </p>
      </figure>
    )
  }

  const summary = cpuSummary(attempt)
  const spans = throttledSpans(attempt.points).map(([s, e]) => [s - data.origin, e - data.origin] as const)
  const last = data.cpu[data.cpu.length - 1]?.at ?? 0
  const tExtent: Extent = { lo: 0, hi: last, degenerate: last === 0 }
  const memPeak = data.mem.reduce<number | null>((m, p) => (p.measured && (m === null || p.value > m) ? p.value : m), null)

  return (
    <figure className="ctl-chart ctl-util has-narrow" aria-label="CPU and memory over time">
      <ChartTitle>CPU and memory over time</ChartTitle>
      <ul className="ctl-chart-legend">
        <li>
          <span className="ctl-swatch is-cpu" aria-hidden="true" /> CPU cores
        </li>
        <li>
          <span className="ctl-swatch is-mem" aria-hidden="true" /> memory
        </li>
        <li>
          <span className="ctl-swatch is-limit" aria-hidden="true" /> limit
        </li>
        <li>
          <span className="ctl-swatch is-throttled" aria-hidden="true" /> CPU-throttled
        </li>
      </ul>
      {DRAWN.map((d) => (
        <svg
          key={d.key}
          className={drawnClass(d)}
          width={d.w}
          height={H}
          viewBox={`0 0 ${d.w} ${H}`}
          role="group"
          aria-label="CPU cores and memory in use over this attempt, each against its limit, with throttled windows shaded"
        >
          <HatchDef id={`${hatchId}-${d.key}`} />
          <UtilPlots
            d={d}
            cpu={data.cpu}
            mem={data.mem}
            cpuExtent={cpuExtent}
            memExtent={memExtent}
            cpuLimit={attempt.cpu_limit_cores}
            memLimit={attempt.memory_limit_bytes}
            spans={spans}
            tExtent={tExtent}
            hatchId={`${hatchId}-${d.key}`}
          />
        </svg>
      ))}
      <figcaption className="ctl-chart-cov">
        <span data-testid="util-summary">{cpuSummaryText(summary)}</span>
        <br />
        <span data-testid="util-mem-summary">
          {memPeak === null
            ? 'Memory: not measured'
            : `Memory: peak ${bytesLabel(memPeak)}${attempt.memory_limit_bytes === null ? ' · limit not reported' : ` of ${bytesLabel(attempt.memory_limit_bytes)}`}`}
          {live && ' · live'}
        </span>
      </figcaption>
    </figure>
  )
}

function UtilPlots({
  d,
  cpu,
  mem,
  cpuExtent,
  memExtent,
  cpuLimit,
  memLimit,
  spans,
  tExtent,
  hatchId,
}: {
  d: Drawn
  cpu: readonly ChartPoint[]
  mem: readonly ChartPoint[]
  cpuExtent: Extent | null
  memExtent: Extent | null
  cpuLimit: number | null
  memLimit: number | null
  spans: ReadonlyArray<readonly [number, number]>
  tExtent: Extent
  hatchId: string
}) {
  const innerW = d.w - M.left - M.right
  const x = linearScale(tExtent, [0, innerW])
  const clampX = (t: number) => x(Math.min(tExtent.hi, Math.max(tExtent.lo, t)))
  return (
    <>
      <g transform={`translate(${M.left},${M.top})`}>
        {spans.map(([s, e]) => (
          <rect
            key={`thr-${s}`}
            className="ctl-util-throttled"
            data-testid="util-throttled"
            x={clampX(s)}
            y={0}
            width={Math.max(0, clampX(e) - clampX(s))}
            height={PLOT_H}
          >
            <title>{`CPU-throttled from T+${spanText(Math.max(0, s))} to T+${spanText(e)}`}</title>
          </rect>
        ))}
        <Series
          points={cpu}
          extent={cpuExtent}
          limit={cpuLimit}
          x={x}
          innerW={innerW}
          kind="cpu"
          format={coresLabel}
          hatchId={hatchId}
        />
      </g>
      <g transform={`translate(${M.left},${M.top + PLOT_H + GAP})`}>
        <Series
          points={mem}
          extent={memExtent}
          limit={memLimit}
          x={x}
          innerW={innerW}
          kind="mem"
          format={bytesLabel}
          hatchId={hatchId}
        />
        <ValueAxis
          side="bottom"
          top={PLOT_H}
          scale={x}
          extent={tExtent}
          format={(v) => `T+${spanText(v)}`}
          ticks={d.ticks}
          minGapPx={64}
        />
      </g>
    </>
  )
}

function Series({
  points,
  extent,
  limit,
  x,
  innerW,
  kind,
  format,
  hatchId,
}: {
  points: readonly ChartPoint[]
  extent: Extent | null
  limit: number | null
  x: LinearScale
  innerW: number
  kind: 'cpu' | 'mem'
  format: (v: number) => string
  hatchId: string
}) {
  const name = kind === 'cpu' ? 'CPU' : 'memory'
  if (extent === null) {
    // This series alone was never read: the other plot still stands, and this
    // one says so in words across its band rather than drawing a zero.
    return (
      <>
        <rect className="ctl-chart-absent" x={0} y={0} width={innerW} height={PLOT_H} fill={`url(#${hatchId})`} />
        <text className="ctl-chart-rowlabel" x={4} y={PLOT_H / 2} data-testid={`util-${kind}-none`}>
          {name} not measured
        </text>
      </>
    )
  }
  const y = linearScale(extent, [PLOT_H, 0])
  const fmt = kind === 'cpu' ? (v: number) => `${coresLabel(v)} cores` : bytesLabel
  return (
    <>
      {points.map((p) =>
        p.measured ? null : (
          <rect
            key={`absent-${p.at}`}
            className="ctl-chart-absent"
            data-testid={`util-${kind}-absent`}
            x={x(p.at) - SLIVER / 2}
            y={0}
            width={SLIVER}
            height={PLOT_H}
            fill={`url(#${hatchId})`}
          >
            <title>{`${p.label} — ${name} not measured`}</title>
          </rect>
        ),
      )}
      {limit !== null && (
        <line
          className="ctl-util-limit"
          data-testid={`util-${kind}-limit`}
          data-value={limit}
          x1={0}
          x2={innerW}
          y1={y(limit)}
          y2={y(limit)}
        >
          <title>{`${name} limit — ${fmt(limit)}`}</title>
        </line>
      )}
      <SampleLine
        points={points}
        x={(at) => x(at)}
        y={(v) => y(v)}
        className={`ctl-chart-line ctl-util-line is-${kind}`}
        testId={`util-${kind}-line`}
      />
      {/* A lone reading between two absences has no neighbour to join; it is
          a dot, so it is still drawn. */}
      {points.map((p, i) =>
        p.measured && !points[i - 1]?.measured && !points[i + 1]?.measured ? (
          <circle key={`dot-${p.at}`} className={`ctl-util-dot is-${kind}`} cx={x(p.at)} cy={y(p.value)} r={2.5}>
            <title>{`${p.label} — ${fmt(p.value)}`}</title>
          </circle>
        ) : null,
      )}
      <ValueAxis side="left" scale={y} extent={extent} format={format} minGapPx={VALUE_LABEL_GAP_PX} keep={limit === null ? [] : [limit]} />
    </>
  )
}
