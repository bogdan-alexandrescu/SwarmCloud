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

/**
 * AG-20 (owner decision, 2026-09-25). EVERY INSPECTOR CHART IS AUTHORED AT TWO
 * WIDTHS, AND NEITHER IS EVER SCALED BELOW THE WIDTH IT WAS DRAWN AT.
 *
 * Each chart was one SVG with a 640-unit viewBox under `width: 100%`, in a
 * 400-720px inspector column, so it was scaled to about 330-430px and its
 * tick text -- `--t-micro`, 12px in the chart's own units -- rendered at 6-8px,
 * under the type scale's floor. design-system.md §7.2 says charts are authored
 * twice, not scaled; nothing had done it.
 *
 * So a chart draws each of its roots once per entry here, with its own
 * geometry and its own tick count, and the sheet shows one of them
 * (`.ctl-chart.has-narrow`, `@container ctl-chart` in styles.css): the wide
 * drawing only in a box at least `wide.w` across, so it never shrinks; the
 * narrow one otherwise, with a `min-width` of `narrow.w`, so it never shrinks
 * either. Both are in the markup, which is what keeps this SSR-safe -- nothing
 * measures the DOM, the reason the charts were scaled in the first place
 * (charts/README.md).
 *
 * THE NARROW WIDTH IS THE SMALLEST INSPECTOR IT HAS TO FIT. At 360px, the
 * narrowest common phone, the full-screen drawer less its two 16px gutters
 * leaves 328px; 300 leaves room for a card's own inset. At the 480px default
 * column the narrow drawing is what shows, and the sheet lets it grow to
 * 360px -- 1.2x, 14.4px text -- rather than to the whole column, where a
 * drawing made for 300 would carry 20px tick labels.
 *
 * `ticks` is what each drawing asks `ValueAxis` for along its width. The
 * narrow one asks for fewer, and `minGapPx` then drops whatever still does
 * not fit, so it can never carry more ticks than the wide one.
 */
export const DRAWN = [
  { key: 'wide', w: 640, ticks: 3 },
  { key: 'narrow', w: 300, ticks: 2 },
] as const

export type Drawn = (typeof DRAWN)[number]

/** The class a drawing's root <svg> carries: the sheet picks one by it. */
export function drawnClass(d: Drawn): string {
  return d.key === 'wide' ? 'ctl-chart-svg is-wide' : 'ctl-chart-svg is-narrow'
}
