// The ONLY module in this repository that imports a charting library.
//
// THE DECISION (owner, 2026-09-22): a charting library rather than hand-rolled
// SVG. The library is visx 4.0.0 -- `@visx/scale`, `@visx/shape`, `@visx/axis`,
// `@visx/group`. What it was chosen against, with the measurements, is in
// README.md beside this file -- including where that disagrees with the
// audit's own §B5 recommendation, and why.
//
// THE REASON THIS WRAPPER EXISTS, rather than screens calling visx directly:
// every charting library draws a missing measurement as a gap, as a zero, or
// as a line straight through it. All three are lies in this product, whose
// best property is that it says "not reported — No attempt reported a cost.
// This is an absent measurement, not $0.00" and, two tiles along, renders a
// checkpoint count of `0` as a DIGIT because that zero was measured.
//
// That rule is enforced HERE, once, and a new chart cannot opt out of it by
// accident:
//
//   * An absence never reaches the library. `ChartPoint`'s absent arm has no
//     `value` field at all (series.ts), so there is no number to scale and the
//     compiler rejects the code that would invent one.
//   * The line is drawn with `defined={p => p.measured}`, hard-coded. There is
//     no `defined`, `connectNulls`, `data` or `children` prop on this
//     component: a caller cannot reach past it to the library.
//   * With nothing measured there is no axis, because an axis over an empty
//     domain is a range the data does not support.
//   * No `.nice()`, anywhere. Nice rounding moves the top of the axis out to a
//     tidy number nobody recorded.
//   * The coverage sentence is rendered by this component, from the same
//     object the total comes from. There is no way to ask for the total
//     without it.
//
// This repository has `check-contract-parity.sh` because a rule restated in a
// second place drifts. This is the same argument applied to a drawing rule.
//
// SSR / renderToStaticMarkup / jsdom. Nothing here measures the DOM. The SVG
// has an intrinsic size and a viewBox, and CSS scales it inside its column, so
// the whole chart renders under `renderToStaticMarkup` and under Vitest's
// jsdom, where layout is 0x0. A responsive container that measures its parent
// renders an EMPTY DIV in both -- which is why one is not used here, and it is
// the measured reason recharts was rejected.

import { AxisBottom, AxisLeft } from '@visx/axis'
import { Group } from '@visx/group'
import { scaleLinear, scaleUtc } from '@visx/scale'
import { LinePath } from '@visx/shape'
import { useId } from 'react'

import { HELP, helpAnchor } from '../help'
import {
  coverageOf,
  inTimeOrder,
  measuredRuns,
  stepAfter,
  timeExtent,
  valueExtent,
  type ChartPoint,
  type Extent,
  type ZeroRule,
} from './series'

export interface TimeSeriesProps {
  /** The series. Order does not matter; it is drawn in time order. */
  points: readonly ChartPoint[]
  /** Above the plot. Also the accessible name. */
  title: string
  /**
   * What ONE point is, singular -- "attempt", "run", "day". The coverage
   * sentence is built from it, so "7 of 13 attempts reported" is a sentence
   * this component writes rather than one a screen passes in and can get
   * wrong.
   */
  noun: string
  /**
   * How a measured value is written. It MUST render 0 as a zero figure
   * ("$0.00", "0"), because a measured zero is a measurement.
   */
  format: (value: number) => string
  /**
   * The class-level sentence for the absences in this series -- what it means
   * that these points have no number. Per-point reasons come from the points.
   */
  absentCopy: string
  /** Where the value axis may start. No default; see `ZeroRule`. */
  zero: ZeroRule
  /** Intrinsic size. CSS scales the result to the column; see the note above. */
  width?: number
  height?: number
}

// Compact, per D2 (Lens / Nomad density): a 132px plot, 10.5px tick labels,
// 12px caption. The margins are the smallest that still fit a "$0.0000" tick
// label and a UTC time without the two colliding.
const MARGIN = { top: 10, right: 14, bottom: 22, left: 56 }

export function TimeSeries({
  points,
  title,
  noun,
  format,
  absentCopy,
  zero,
  width = 640,
  height = 132,
}: TimeSeriesProps) {
  // Unique per instance: two charts on one screen must not share a <pattern>
  // id. The colons React puts in useId() are legal in an HTML id and in a
  // url(#...) reference, but they break document.querySelector, so they go.
  const hatchId = `ctl-hatch-${useId().replace(/:/g, '')}`

  const ordered = inTimeOrder(points)
  const cover = coverageOf(ordered)
  const vExtent = valueExtent(ordered, zero)
  const tExtent = timeExtent(ordered)

  if (ordered.length === 0) {
    return (
      <figure className="ctl-chart" aria-label={title}>
        <ChartHead title={title} />
        <p className="ctl-chart-note" role="status">
          No {noun} to plot. This is an empty series, not a series of zeroes.
        </p>
      </figure>
    )
  }

  // NOTHING MEASURED. No domain exists, so no axis is drawn. A library asked
  // to chart this produces an axis from 0 to 1 and an empty plot area, which
  // reads as "measured, and flat at zero". The sentence is the honest render.
  //
  // §6.9'S SHAPE: HEADING, ONE SENTENCE, A LINK OUT. This was a 40-word
  // paragraph -- the largest block of prose in the agent drawer (§12.5) -- and
  // it ended with `absentCopy`, which is written for the PLOTTED case: Token
  // spend's says "Hover a hatched band for the reason", and in this state
  // there is no plot and no band to hover. The heading is the fact, the
  // sentence is its size, and why an absence is not a zero is one topic away.
  // `absentCopy` stays where it is true -- under a plot that has absences.
  //
  // NO `.ctl-mark` HERE, on purpose: `Mark` lives in AgentDetail.tsx, which
  // imports this layer through TokenSpend, and the chart layer importing a
  // screen would close that loop. Promoting `Mark` into the primitives is the
  // primitive-collapse pass's to do, not this one's.
  if (vExtent === null || tExtent === null) {
    return (
      <figure className="ctl-chart" aria-label={title}>
        <ChartHead title={title} />
        <div className="ctl-empty" role="status">
          <h3>Nothing was measured</h3>
          <p>
            None of the {cover.points} {plural(noun, cover.points)} reported a value.
          </p>
          <a href={`#${helpAnchor('absent-vs-zero')}`}>{HELP['absent-vs-zero'].title}</a>
        </div>
      </figure>
    )
  }

  const innerW = Math.max(1, width - MARGIN.left - MARGIN.right)
  const innerH = Math.max(1, height - MARGIN.top - MARGIN.bottom)

  // THE TIME AXIS SPANS EVERY POINT, INCLUDING THE ABSENT ONES. When a cost
  // was not reported, the instant of that attempt was still measured; hiding
  // it would shorten the window the chart claims to cover.
  //
  // A single point (or several at one instant) has no span. Rather than invent
  // one, the scale collapses to the middle of the plot and the axis carries
  // that one instant.
  const xScale = scaleUtc({
    domain: [new Date(tExtent.lo), new Date(tExtent.hi)],
    range: tExtent.degenerate ? [innerW / 2, innerW / 2] : [0, innerW],
  })

  // NO .nice(). The ends of this axis are measurements.
  const yScale = scaleLinear<number>({
    domain: [vExtent.lo, vExtent.hi],
    range: vExtent.degenerate ? [innerH / 2, innerH / 2] : [innerH, 0],
  })

  const yTicks = valueTicks(vExtent, yScale.ticks(3))
  const xTicks = tExtent.degenerate ? [new Date(tExtent.lo)] : undefined

  const absentPoints = ordered.filter(
    (p): p is Extract<ChartPoint, { measured: false }> => !p.measured,
  )
  const drawsALine = measuredRuns(ordered).some((run) => run.length >= 2)
  // Wide enough to read as a region rather than as a mark, narrow enough that
  // two adjacent absences stay two.
  const bandW = Math.max(4, Math.min(14, innerW / Math.max(4, ordered.length * 2)))

  return (
    <figure className="ctl-chart" aria-label={title}>
      <ChartHead title={title} />
      <svg
        className="ctl-chart-svg"
        width={width}
        height={height}
        viewBox={`0 0 ${width} ${height}`}
        // A GROUP, NOT AN IMAGE: an image's children are presentational, and
        // every absence band's <title> below is the sentence saying why that
        // point has no value. Those reasons are the accessible route to what
        // the band means; one label for the whole chart would hide all of them.
        role="group"
        aria-label={title}
      >
        <defs>
          {/* The absence texture. The same idea as `--ctl-hatch` in
              styles.css, which CSS gradients cannot paint into an SVG shape:
              diagonal stripes read as "no data here" at a glance and survive
              a greyscale screenshot, which a colour alone does not. */}
          <pattern
            id={hatchId}
            className="ctl-chart-hatch"
            width={6}
            height={6}
            patternUnits="userSpaceOnUse"
            patternTransform="rotate(45)"
          >
            <line x1={0} y1={0} x2={0} y2={6} strokeWidth={2} />
          </pattern>
        </defs>

        <Group left={MARGIN.left} top={MARGIN.top}>
          {/* The zero rule, drawn only when zero is inside the domain. It is
              what makes a MEASURED zero legible: its dot sits ON this line,
              where an absence has no dot at all. */}
          {vExtent.lo <= 0 && vExtent.hi >= 0 && !vExtent.degenerate && (
            <line
              className="ctl-chart-zeroline"
              x1={0}
              x2={innerW}
              y1={yScale(0)}
              y2={yScale(0)}
              data-testid="zero-rule"
            />
          )}

          {/* EVERY ABSENCE IS DRAWN, as a region with no height of its own.
              It has an x -- the instant is known -- and deliberately no y: a
              mark at a height would be a value. */}
          {absentPoints.map((p) => (
            <rect
              key={`absent-${p.at}-${p.label}`}
              className="ctl-chart-absent"
              data-testid="absent-band"
              data-label={p.label}
              data-at={p.at}
              x={xScale(new Date(p.at)) - bandW / 2}
              y={0}
              width={bandW}
              height={innerH}
              fill={`url(#${hatchId})`}
            >
              <title>{`${p.label} — not measured. ${p.why}`}</title>
            </rect>
          ))}

          {/* THE LINE. `defined` is hard-coded and there is no prop that can
              change it: d3 ends the current subpath at an absence and starts a
              new one after it, so no stroke ever crosses a gap. Removing this
              one accessor makes `honesty.chart.test.tsx` fail by name -- that
              is the mutation this wrapper is here to survive. */}
          {drawsALine && (
            <LinePath<ChartPoint>
              className="ctl-chart-line"
              data={ordered}
              x={(p) => xScale(new Date(p.at))}
              // NaN, not 0. d3 does not call this accessor for a point its
              // `defined` rejected, but if a future edit ever routes an
              // absence here, NaN produces no segment. A `0` would produce a
              // vertex on the baseline -- the exact defect.
              y={(p) => (p.measured ? yScale(p.value) : Number.NaN)}
              defined={(p) => p.measured}
            />
          )}

          {/* Every measured point, including the zeroes. A lone measurement
              between two absences has no line and would otherwise not be
              drawn at all. */}
          {ordered.map((p) =>
            p.measured ? (
              <circle
                key={`dot-${p.at}-${p.label}`}
                className={p.value === 0 ? 'ctl-chart-dot is-zero' : 'ctl-chart-dot'}
                data-testid="measured-dot"
                data-label={p.label}
                data-value={p.value}
                cx={xScale(new Date(p.at))}
                cy={yScale(p.value)}
                r={p.value === 0 ? 3.5 : 2.75}
              >
                <title>{`${p.label} — ${format(p.value)}, measured`}</title>
              </circle>
            ) : null,
          )}

          <AxisLeft
            scale={yScale}
            tickValues={yTicks}
            tickFormat={(v) => format(Number(v))}
            numTicks={yTicks.length}
            axisClassName="ctl-chart-axis"
            tickClassName="ctl-chart-tick"
            axisLineClassName="ctl-chart-axisline"
            tickLength={3}
          />
          <AxisBottom
            top={innerH}
            scale={xScale}
            tickValues={xTicks}
            numTicks={Math.min(4, ordered.length)}
            axisClassName="ctl-chart-axis"
            tickClassName="ctl-chart-tick"
            axisLineClassName="ctl-chart-axisline"
            tickLength={3}
          />
        </Group>
      </svg>

      {/* THE COVERAGE SENTENCE. Rendered from the same `Coverage` the total
          comes from, by this component, always. A total over a partial series
          that does not say so is one of the four things this wrapper exists to
          make impossible. */}
      <figcaption className="ctl-chart-cov" data-partial={cover.partial ? 'yes' : 'no'}>
        {cover.sum === null ? (
          <span className="ctl-em">not measured</span>
        ) : (
          <>
            <strong>{format(cover.sum)}</strong> total
            {cover.mean !== null && cover.measured > 1 && (
              <> · {format(cover.mean)} mean</>
            )}{' '}
            over{' '}
            {cover.partial ? (
              <>
                <strong>
                  {cover.measured} of {cover.points}
                </strong>{' '}
                {plural(noun, cover.points)} that reported
              </>
            ) : (
              <>
                all {cover.points} {plural(noun, cover.points)}
              </>
            )}
            . Times UTC.
          </>
        )}
      </figcaption>

      {cover.absent > 0 && (
        <p className="ctl-chart-note" role="status">
          <span className="ctl-chart-key" aria-hidden="true" /> {cover.absent} of{' '}
          {cover.points} {plural(noun, cover.points)} reported no value —{' '}
          {absentCopy}
        </p>
      )}
    </figure>
  )
}

function ChartHead({ title }: { title: string }) {
  return <p className="ctl-chart-title">{title}</p>
}

// ---------------------------------------------------------------------------
// THE PRIMITIVES EVERY OTHER CHART IS BUILT FROM
// ---------------------------------------------------------------------------
//
// WHY THEY LIVE IN THIS FILE. `chart.tokenspend.test.tsx` fails if any module
// other than this one imports the charting library, and that rule is worth
// more than a tidy file layout: it is what keeps the absent-value rules in one
// place. The phase bars, the peak-memory step line, the checkpoint strip and
// the diffstat (`AttemptPhases.tsx`, `PeakMemory.tsx`, `CheckpointStrip.tsx`,
// `Diffstat.tsx`) therefore reach visx ONLY through the four exports below,
// and each export carries one of the rules with it:
//
//   linearScale  a domain the caller built from MEASUREMENTS (an `Extent`),
//                never `.nice()`d, collapsing to the middle when degenerate
//                rather than inventing a span;
//   ValueAxis    ticks whose ends ARE the extent -- the top of an axis is the
//                largest measurement, never a rounder number past it;
//   StepLine     `defined` hard-coded, over `ChartPoint`s, so an absence has
//                no value to plot and breaks the stroke;
//   LinearScale  the type, so a chart can hold a scale without importing
//                the library to name it.
//
// There is deliberately no bar, no stack and no area primitive. A bar here is
// a plain `<rect>` between two recorded instants, drawn by the chart that owns
// the data, because the audit's §B5 finding still stands for stacking: a
// stacking function given an unknown segment gives it height 0 and the bar
// silently shortens. Nothing in this layer stacks.

/** A visx linear scale, by its type only. */
export type LinearScale = ReturnType<typeof scaleLinear<number>>

/**
 * A linear scale over an extent the DATA produced.
 *
 * `extent` comes from `valueExtent`, `phaseExtent` or an equivalent built from
 * measured values only, so there is no way to hand this a domain that an
 * absence widened. A degenerate extent (every value the same) collapses to the
 * middle of the range instead of being padded out to a span nobody recorded --
 * the same rule `TimeSeries` applies to its own axes above.
 */
export function linearScale(extent: Extent, range: readonly [number, number]): LinearScale {
  const mid = (range[0] + range[1]) / 2
  return scaleLinear<number>({
    domain: [extent.lo, extent.hi],
    range: extent.degenerate ? [mid, mid] : [range[0], range[1]],
  })
}

/**
 * An axis whose ends are the extent, with its interior ticks spaced.
 *
 * `keep` names values that must be labelled when they are inside the extent
 * (the phase chart keeps 0, which is admission). `minGapPx` drops a tick whose
 * label would sit on top of one already kept; the ends and `keep` win, the
 * library's interior ticks give way. Nothing outside the extent is ever
 * labelled.
 */
export function ValueAxis({
  side,
  scale,
  extent,
  format,
  top = 0,
  left = 0,
  ticks = 3,
  minGapPx = 0,
  keep = [],
}: {
  side: 'bottom' | 'left'
  scale: LinearScale
  extent: Extent
  format: (value: number) => string
  top?: number
  left?: number
  ticks?: number
  minGapPx?: number
  keep?: readonly number[]
}) {
  const base = valueTicks(extent, scale.ticks(ticks))
  const inside = (v: number) => v >= extent.lo && v <= extent.hi
  // Priority order: the values the caller must see, then the top, then the
  // floor, then the library's interior ticks.
  const ranked = [
    ...keep.filter(inside),
    extent.hi,
    extent.lo,
    ...base.filter((v) => v !== extent.hi && v !== extent.lo),
  ]
  const chosen: number[] = []
  for (const v of ranked) {
    if (chosen.includes(v)) continue
    const px = scale(v)
    if (chosen.some((c) => Math.abs(scale(c) - px) < minGapPx)) continue
    chosen.push(v)
  }
  const values = extent.degenerate ? [extent.lo] : chosen.sort((a, b) => a - b)
  const common = {
    top,
    left,
    scale,
    tickValues: values,
    tickFormat: (v: unknown) => format(Number(v)),
    numTicks: values.length,
    axisClassName: 'ctl-chart-axis',
    tickClassName: 'ctl-chart-tick',
    axisLineClassName: 'ctl-chart-axisline',
    tickLength: 3,
  }
  return side === 'bottom' ? <AxisBottom {...common} /> : <AxisLeft {...common} />
}

/**
 * A step-after line through a running maximum, broken at every absence.
 *
 * The geometry is `stepAfter` (series.ts), which adds a corner only between
 * two measured neighbours; `defined` is hard-coded here exactly as it is on
 * `TimeSeries`' line, and there is no prop that can change it. With no run of
 * two or more measured points there is no line at all -- the caller draws the
 * points themselves.
 */
export function StepLine({
  points,
  x,
  y,
}: {
  points: readonly ChartPoint[]
  x: (at: number) => number
  y: (value: number) => number
}) {
  const vertices = stepAfter(points)
  if (!measuredRuns(vertices).some((run) => run.length >= 2)) return null
  return (
    <LinePath<ChartPoint>
      className="ctl-chart-line is-step"
      data-testid="step-line"
      data={vertices}
      x={(p) => x(p.at)}
      // NaN rather than 0, for the reason given on `TimeSeries`' line.
      y={(p) => (p.measured ? y(p.value) : Number.NaN)}
      defined={(p) => p.measured}
    />
  )
}

/**
 * The tick values, and the third structural rule.
 *
 * The ends are the extent itself, so the TOP of the axis is the largest
 * measurement (or the zero anchor) and never a rounder number past it. The
 * interior ticks are the library's -- d3's tick selection over a continuous
 * domain is the one genuinely hard piece of arithmetic in an axis and is the
 * reason a scale library is worth a dependency -- but any that fall on or
 * outside the ends are dropped, so nothing outside the data is ever labelled.
 */
function valueTicks(extent: Extent, libraryTicks: readonly number[]): number[] {
  if (extent.degenerate) return [extent.lo]
  const inner = libraryTicks.filter((t) => t > extent.lo && t < extent.hi)
  const all = [extent.lo, ...inner, extent.hi]
  return all.filter((v, i) => all.indexOf(v) === i)
}

function plural(noun: string, n: number): string {
  return n === 1 ? noun : `${noun}s`
}
