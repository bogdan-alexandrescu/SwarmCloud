// The shape of the only data a chart in this product may receive, and the
// arithmetic that is allowed over it.
//
// WHY THIS FILE EXISTS AT ALL. This console's single best property is that it
// tells an ABSENT measurement apart from a MEASURED ZERO, and says which in
// words: "not reported — No attempt reported a cost. This is an absent
// measurement, not $0.00" beside a checkpoint count of `0` rendered as a
// DIGIT, because that zero was measured. Every charting library on the market
// takes `number | null` and draws the null as a gap, as a zero, or as a line
// straight through it. All three are claims this platform cannot support.
//
// So the absence never reaches the library. `ChartPoint` is a discriminated
// union with NO `value` field on its absent arm: there is no number to hand a
// scale, and TypeScript refuses the code that would try. The library only ever
// sees points that were measured.
//
// Pure: no React, no visx, no DOM. `TimeSeries.tsx` is the only module in this
// repository that imports a charting library, and it imports this one for
// every decision that is about the DATA rather than about pixels.

/**
 * One point of a series.
 *
 * `at` is an epoch-milliseconds instant. It is present on BOTH arms on
 * purpose: when a cost was not reported, the *time* of the attempt is still a
 * measurement, and drawing the absence at its real position on the time axis
 * is honest. What is unknown is the value, and only the value.
 */
export type ChartPoint =
  | {
      readonly at: number
      readonly label: string
      readonly measured: true
      readonly value: number
    }
  | {
      readonly at: number
      readonly label: string
      readonly measured: false
      /** Why there is no number. Rendered verbatim; never "0", never "—". */
      readonly why: string
    }

/** A point that was measured. `value` may be 0; a measured zero is a fact. */
export function measured(at: number, label: string, value: number): ChartPoint {
  return { at, label, measured: true, value }
}

/** A point that was not measured, and the sentence that says why. */
export function absent(at: number, label: string, why: string): ChartPoint {
  return { at, label, measured: false, why }
}

/**
 * The constructor for a nullable field off the API, and the reason this module
 * is worth its length.
 *
 * `why` is a REQUIRED parameter, not an optional one. Every call site that
 * holds a `number | null` is therefore forced, by the compiler, to have
 * already written the sentence explaining the null before it can plot the
 * series at all. That is the same discipline `AttemptSpend` applies by hand in
 * prose -- a runner that does not report, an attempt that has not finished, an
 * attempt that never started, and an image too old to capture are four
 * different sentences -- expressed as a type.
 *
 * A non-finite number is NOT a measurement. NaN and +/-Infinity come out of
 * arithmetic over missing inputs, and a scale given one produces an axis with
 * no domain at all, so they are recorded as absences with the reason stated.
 */
export function reading(
  at: number,
  label: string,
  value: number | null | undefined,
  why: string,
): ChartPoint {
  if (value === null || value === undefined) return absent(at, label, why)
  if (!Number.isFinite(value)) {
    return absent(
      at,
      label,
      'The value read back was not a finite number, so nothing is plotted for it.',
    )
  }
  return measured(at, label, value)
}

/**
 * What a series actually supports being said about it.
 *
 * `sum`, `mean`, `min` and `max` are `null` when NOTHING was measured, rather
 * than 0. A sum of no measurements is not zero; it is unknown, and this is the
 * type that makes a caller handle that rather than print `$0.00`.
 *
 * `absent` is carried beside every figure so that no total can be rendered
 * without its coverage: `TimeSeries` prints both from one object, and there is
 * no way to ask it for the total alone.
 */
export interface Coverage {
  /** Every point, measured or not. */
  readonly points: number
  readonly measured: number
  readonly absent: number
  /** Over the MEASURED points only, and null when there are none. */
  readonly sum: number | null
  readonly mean: number | null
  readonly min: number | null
  readonly max: number | null
  /** True when at least one point had no measurement. */
  readonly partial: boolean
}

export function coverageOf(points: readonly ChartPoint[]): Coverage {
  let sum = 0
  let n = 0
  let min: number | null = null
  let max: number | null = null
  for (const p of points) {
    if (!p.measured) continue
    n += 1
    sum += p.value
    if (min === null || p.value < min) min = p.value
    if (max === null || p.value > max) max = p.value
  }
  return {
    points: points.length,
    measured: n,
    absent: points.length - n,
    sum: n === 0 ? null : sum,
    mean: n === 0 ? null : sum / n,
    min,
    max,
    partial: n !== points.length,
  }
}

/**
 * Where the value axis is allowed to start and stop.
 *
 * `anchored` — the quantity has a meaningful zero (money, tokens, counts, a
 * duration). The floor may be pulled down to 0 so that "small" reads as small
 * rather than as the bottom of a zoomed window.
 *
 * `from-data` — it does not (a temperature, a rate, a delta). The axis spans
 * exactly the measurements and no further.
 *
 * There is NO DEFAULT and the prop is required on the component. Both choices
 * are defensible and only the caller knows which; a default would make one of
 * them happen silently, and a value axis whose floor was chosen by a library
 * default is the quietest way to misrepresent a magnitude.
 */
export type ZeroRule = 'anchored' | 'from-data'

export interface Extent {
  readonly lo: number
  readonly hi: number
  /** lo === hi: every measurement is the same number. */
  readonly degenerate: boolean
}

/**
 * The value extent, or null when nothing was measured.
 *
 * Null is the important return. A chart with no measurements has NO domain,
 * and the honest rendering is no axis at all -- not an axis from 0 to 1, which
 * is a range the data does not support and which every library will invent for
 * you. `TimeSeries` renders the sentence instead.
 *
 * Note what this deliberately does NOT do: it never calls d3's `.nice()`. Nice
 * rounding moves the ends of the axis OUT to tidy numbers, so the top of the
 * axis stops being a measurement and starts being a number nobody recorded.
 */
export function valueExtent(points: readonly ChartPoint[], zero: ZeroRule): Extent | null {
  const c = coverageOf(points)
  if (c.min === null || c.max === null) return null
  const lo = zero === 'anchored' ? Math.min(0, c.min) : c.min
  const hi = zero === 'anchored' ? Math.max(0, c.max) : c.max
  return { lo, hi, degenerate: lo === hi }
}

/** The time extent, over every point: an absent value still has a real instant. */
export function timeExtent(points: readonly ChartPoint[]): Extent | null {
  const first = points[0]
  if (first === undefined) return null
  let lo = first.at
  let hi = first.at
  for (const p of points) {
    if (p.at < lo) lo = p.at
    if (p.at > hi) hi = p.at
  }
  return { lo, hi, degenerate: lo === hi }
}

/**
 * The runs of consecutive measured points.
 *
 * A line may only be drawn WITHIN a run. Between two runs there is an absence,
 * and a stroke across it would assert a measurement that was never taken --
 * the single failure this whole module exists to prevent. `TimeSeries` uses
 * this to decide whether any line is drawable at all; the rendering itself is
 * done by the library's `defined` accessor, which produces the same partition.
 */
export function measuredRuns(points: readonly ChartPoint[]): ChartPoint[][] {
  const runs: ChartPoint[][] = []
  let current: ChartPoint[] = []
  for (const p of points) {
    if (p.measured) {
      current.push(p)
    } else if (current.length > 0) {
      runs.push(current)
      current = []
    }
  }
  if (current.length > 0) runs.push(current)
  return runs
}

/** Sorted oldest-first. Charts are drawn in time order, never in array order. */
export function inTimeOrder(points: readonly ChartPoint[]): ChartPoint[] {
  return [...points].sort((a, b) => a.at - b.at)
}
