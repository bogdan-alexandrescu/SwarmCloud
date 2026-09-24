// Plain-SVG pieces the inspector charts share. NO charting library here:
// `TimeSeries.tsx` stays the only module that imports one, and a test fails
// if that stops being true. Nothing in this file draws a value; it draws the
// ABSENCE texture and the chart head, which have to look the same everywhere
// or they stop meaning one thing.

import { useId } from 'react'

/**
 * A per-instance id for the hatch `<pattern>`.
 *
 * Unique per chart: two charts on one screen must not share a pattern id, and
 * an attempt card draws up to three. The colons React puts in `useId()` break
 * `document.querySelector`, so they go -- the same reason `TimeSeries` gives.
 */
export function useHatchId(): string {
  return `ctl-hatch-${useId().replace(/:/g, '')}`
}

/**
 * The absence texture, as an SVG pattern.
 *
 * `--ctl-hatch` is a CSS gradient and cannot paint into an SVG shape, so the
 * same 45-degree stripe is drawn here, stroked by `.ctl-chart-hatch line`
 * exactly as `TimeSeries` strokes its own. Diagonal stripes read as "no data
 * here" at a glance and survive a greyscale screenshot; a colour does not.
 */
export function HatchDef({ id }: { id: string }) {
  return (
    <defs>
      <pattern
        id={id}
        className="ctl-chart-hatch"
        width={6}
        height={6}
        patternUnits="userSpaceOnUse"
        patternTransform="rotate(45)"
      >
        <line x1={0} y1={0} x2={0} y2={6} strokeWidth={2} />
      </pattern>
    </defs>
  )
}

/** The chart's title, above the plot. Also the figure's accessible name. */
export function ChartTitle({ children }: { children: string }) {
  return <p className="ctl-chart-title">{children}</p>
}
