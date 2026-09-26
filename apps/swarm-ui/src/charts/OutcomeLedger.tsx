// THE OUTCOME LEDGER (#185): four aligned lanes on one time axis, the readout
// that is also the legend, and the pick that moves across all four.
//
// NO CHARTING LIBRARY. `TimeSeries.tsx` stays the only module that imports one
// (chart.tokenspend.test.tsx holds that), and nothing here needs one: every
// mark is a rect or a polyline between two values the server measured, with a
// scale whose domain is those values and no `.nice()`. The hatch is the shared
// `HatchDef`, so "not read" looks the same here as in every inspector chart.
//
// WHAT EACH LANE IS, and the rule it cannot break:
//
//   1  Success rate. succeeded / (succeeded + failed + dead-lettered), cancels
//      excluded (owner decision). `--series-1`, never `--ok`: a line in a
//      verdict colour reads as a verdict (§1.6). The y scale is FIXED at
//      0-100 %, so a small move is not magnified. The Wilson band sits behind
//      the line on `--surface-2` with its two edges ruled -- not a hatch, which
//      means "not measured". A point is hollow under five decided, so one of
//      two is never an outage. A bucket with nothing decided has NO point and
//      the line breaks there: a gap, never 0 % (honesty rule 6).
//   2  Decided work, by the bucket it ended in. Succeeded up in TS-4's solid
//      `--ok` (owner decision), failed and dead-lettered down in TS-4's solid
//      `--bad` with the 2px ground-coloured cut, never under 4px, all on ONE
//      count scale through zero. A measured zero is the axis tick alone.
//   3  Cancelled, on its OWN scale with its max printed, so 305 cancels on
//      22 Sep can no longer flatten that day's 8 failures. Three marks, all in
//      TS-4's cancel vocabulary and none with a hue (a cancel is not a
//      verdict), stacked up from the axis:
//        requested (and other)  TS-4's flat "ended" bars -- a cancel;
//        after a cancel         the flat bars inside a 1px outline -- a
//                               cascade (the outline) that began at a cancel
//                               somebody asked for (the bars);
//        after a failure        the 1px outline alone, no fill -- a cascade
//                               that began at a failure, however many steps
//                               down it reached (workflow sweeps included,
//                               which only a failure starts).
//      The two cascades used to be one outline, because the scheduler writes
//      the same words for both; the route splits them now (#185, decision 2)
//      by where each chain began, not by the state of the step just above
//      (the review of #217), and the readout names each. The after-a-cancel
//      mark is never under 7px and its bars start at its own top, so a bar
//      always shows inside the outline: at 3-4px on a page-anchored pattern
//      it drew as the after-a-failure outline pixel for pixel. The minimum
//      heights come out of the tallest mark, never out of the lane's top.
//   4  Throughput (owner decision, from the Flow concept): submitted per bucket
//      against finished per bucket, answering "are we keeping up". THE ONLY
//      LANE ON THE SUBMISSION-TIME BASIS, and its label says so. Submitted is a
//      `--series-2` step line and finished `--series-5` neutral columns: they
//      are counts, not outcomes, so neither takes a TS-4 form -- the Flow
//      concept's outline column would have been lane 3's "after a failure"
//      mark meaning a second thing on the same chart.
//
// EVERY BUCKET IS DRAWN, from `since` to `until`. A bucket the server could not
// read is one hatched band the full height of all four lanes, with no column
// and no digit. The current bucket gets a dashed right edge and reads "so far".
//
// DRAWN THREE TIMES, NEVER SCALED (§7.2, AG-20's mechanism plus a page-width
// entry). The ledger sits in a content column up to ~1144px wide, so AG-20's
// 640 drawing scaled across it would carry ~21px ticks, and scaled into a
// phone ~6px ones. Each drawing below has its own geometry and label stride;
// the sheet shows the one whose width the container can hold. A drawing whose
// buckets would fall under the column floor grows wider than its nominal
// width and scrolls inside the chart instead of shrinking -- at 390 that is
// the 26px floor, opening at the newest end (TS-3).
//
// THE SCALE DOES NOT SCROLL (wireframe_390: `100┤ … 0┤ … ok ┤ … cx ┤` pinned,
// older days behind the fade). Each drawing is three layers: a gutter SVG as
// wide as the drawing's left margin, holding every tick; the lane labels, as
// HTML over the plot's left edge on the page's own ground; and the plot, the
// ONLY part inside the scroller -- grid, marks, time axis and the columns a
// reader picks. Drawn in one SVG inside the scroller, the whole scale scrolled
// off on open: at 390 and 14 days the plot opens 52px in, past every tick,
// and at 24h or 30d past every lane label, the throughput lane's
// `by created_at` among them. The plot's SVG keeps the drawing's coordinates
// (its viewBox starts at the left margin), so a mark is at the same x in
// every layer and in every test that reads one.
//
// THE MARKS ARE PRESENTATIONAL; THE COLUMNS SPEAK. Each drawing's SVG is
// `aria-hidden`, and over it sits one HTML column per bucket, `role="img"`,
// named with its full time and every count (TS-9). The columns are one tab
// stop with a roving tabindex, starting on the newest bucket. Only the
// drawing the sheet shows is in the tab order: the others are `display: none`.

import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type FocusEvent,
  type KeyboardEvent,
  type UIEvent,
} from 'react'

import {
  LOW_N,
  axisLabels,
  bucketName,
  bucketSaid,
  dayStarts,
  interval,
  nothingReadWords,
  pct,
  spanCoverage,
  unitWord,
  unreadWords,
  wallOf,
  type LedgerBucketSize,
  type OutcomeBucket,
  type Outcomes,
} from '../outcomes'
import { Mark } from '../primitives'
import { HatchDef, useHatchId } from './parts'

/**
 * The three drawings. `w` is the nominal width; `floor` the narrowest a
 * bucket's column may get before the drawing grows and scrolls instead; the
 * lane heights are that drawing's own. The wide lane heights are the
 * synthesis's (64, 168, 56) plus the throughput lane.
 *
 * THE NARROW FLOOR IS 26px, the phone column floor the Timeline already drew
 * (TS-3): a column narrower than that cannot be told from its neighbour by a
 * thumb, and the step buttons beside the readout are the 44px target.
 */
export const LEDGER_DRAWN = [
  { key: 'wide', w: 1080, left: 56, right: 16, floor: 8, lanes: [64, 168, 56, 64], gap: 26 },
  { key: 'mid', w: 640, left: 48, right: 12, floor: 8, lanes: [56, 128, 48, 56], gap: 24 },
  { key: 'narrow', w: 300, left: 38, right: 8, floor: 26, lanes: [48, 104, 40, 48], gap: 24 },
] as const

export type LedgerDrawn = (typeof LEDGER_DRAWN)[number]

/** How far apart two axis labels must sit, in px: `Sep 21` is ~44px of --t-micro mono. */
const LABEL_PX = 52

/** A phone's hourly axis labels every third hour on the clock (TS-3). */
const PHONE_HOUR_STRIDE = 3

/** A failed column that is not zero is never thinner than the 2px cut plus 2px of `--bad` (WF-1). */
const MIN_FAILED_PX = 4

/** A lane-3 mark that counts anything is never under one flat bar's height. */
const MIN_CANCEL_PX = 3

/** TS-4's flat "ended" bars: a 3px bar every 5px (§15.3). */
const FLAT_BAR = 3
const FLAT_PERIOD = 5

/**
 * AN "AFTER A CANCEL" MARK IS NEVER UNDER 7px: its 1px outline, one whole
 * 3px flat bar and the 2px gap after it, and the outline again -- exactly one
 * period of its key (`.ol-k.is-after-cancel`: the bars from the top inner edge
 * of a 1px border). Anything shorter has no room for a bar, and at 3-4px it
 * drew as the bare "after a failure" outline pixel for pixel (the review of
 * #217). The bars inside it are drawn from the MARK's own top edge
 * (`innerBars`), not from a page-anchored pattern, so a bar always lands
 * inside the outline whatever height the stack below leaves it at.
 */
const MIN_AFTER_CANCEL_PX = 1 + FLAT_PERIOD + 1

/**
 * Lane 3's three marks, stacked up from the baseline: requested (and other),
 * after a cancel, after a failure. Each is its count on the lane's scale, but
 * never under its minimum -- and THE MINIMUMS ARE TAKEN BACK FROM THE TALLEST
 * MARK when the stack would pass the lane's top. A minimum is extra height on
 * top of the count's, so a full column with a small cascade on it rose past
 * the lane into its label; now the column's total stays on the scale, and the
 * readout and the Table carry the exact split.
 */
function laneThreeStack(flatN: number, cascN: number, afterN: number, k: number, laneH: number): [number, number, number] {
  const marks = [
    { n: flatN, min: MIN_CANCEL_PX },
    { n: cascN, min: MIN_AFTER_CANCEL_PX },
    { n: afterN, min: MIN_CANCEL_PX },
  ].map((m) => {
    const min = m.n === 0 ? 0 : m.min
    return { min, h: m.n === 0 ? 0 : Math.max(min, m.n * k) }
  })
  let over = marks.reduce((s, m) => s + m.h, 0) - laneH
  for (const m of [...marks].sort((a, b) => b.h - a.h)) {
    if (over <= 0) break
    const give = Math.min(over, m.h - m.min)
    m.h -= give
    over -= give
  }
  return [marks[0]!.h, marks[1]!.h, marks[2]!.h]
}

/**
 * The flat bars inside an "after a cancel" outline whose outer edges are
 * `top` and `bottom`: from the outline's inner top edge, a 3px bar every 5px,
 * cut at its inner bottom edge -- the key's own drawing, anchored to the mark.
 */
function innerBars(top: number, bottom: number): Array<{ y: number; h: number }> {
  const bars: Array<{ y: number; h: number }> = []
  const floor = bottom - 1
  for (let y = top + 1; y < floor; y += FLAT_PERIOD) {
    bars.push({ y: r1(y), h: r1(Math.min(FLAT_BAR, floor - y)) })
  }
  return bars
}

const TOP = 26

interface Geo {
  d: LedgerDrawn
  n: number
  pitch: number
  plotW: number
  W: number
  H: number
  left: number
  lane: Array<{ y: number; h: number }>
  axisY: number
  bar: number
  x0: (i: number) => number
  cx: (i: number) => number
}

function geometry(d: LedgerDrawn, n: number): Geo {
  const avail = d.w - d.left - d.right
  const pitch = n === 0 ? avail : Math.max(d.floor, avail / n)
  const plotW = pitch * Math.max(1, n)
  const lane: Array<{ y: number; h: number }> = []
  let y = TOP
  for (const h of d.lanes) {
    lane.push({ y, h })
    y += h + d.gap
  }
  const axisY = y - d.gap + 16
  return {
    d,
    n,
    pitch,
    plotW,
    W: Math.ceil(d.left + plotW + d.right),
    H: axisY + 6,
    left: d.left,
    lane,
    axisY,
    bar: Math.max(1, Math.min(pitch - 2, pitch * 0.66, 28)),
    x0: (i) => d.left + i * pitch,
    cx: (i) => d.left + i * pitch + pitch / 2,
  }
}

/** The four lanes' scales, from the measured buckets only. No `.nice()`: the top is the largest value recorded. */
interface Scales {
  up: number
  down: number
  cancelled: number
  flow: number
  /** How many buckets the maxima were taken over. At 0 there is no scale, and no lane prints a `max`. */
  read: number
}

function scalesOf(buckets: readonly OutcomeBucket[]): Scales {
  let up = 0
  let down = 0
  let cancelled = 0
  let flow = 0
  let read = 0
  for (const b of buckets) {
    if (b.state === 'unread') continue
    read += 1
    up = Math.max(up, b.succeeded ?? 0)
    down = Math.max(down, (b.failed ?? 0) + (b.dead_lettered ?? 0))
    cancelled = Math.max(cancelled, b.cancelled?.total ?? 0)
    flow = Math.max(flow, b.submitted ?? 0, b.ended ?? 0)
  }
  return { up, down, cancelled, flow, read }
}

const r1 = (v: number) => Math.round(v * 10) / 10

/**
 * The lane labels, by drawing: the narrow one cannot carry the long form.
 *
 * A `max` IS PRINTED ONLY OVER READ BUCKETS: with none read there is no
 * scale, and `max 0` would be a measurement nobody made.
 *
 * THE NARROW LABELS FIT THE PHONE'S PLOT. They sit over the plot's pinned
 * left edge, one line each, so the longest (~36 characters of --t-micro mono,
 * ~260px) has to fit the 320px a 390 viewport leaves beside the 38px scale;
 * `Submitted vs finished · by created_at · max 338` did not.
 */
function laneLabels(d: LedgerDrawn, s: Scales, basis: Outcomes['basis']): string[] {
  const narrow = d.key === 'narrow'
  const max = (n: number) => (s.read === 0 ? '' : ` · max ${n}`)
  // THE BASIS IS THE ROUTE'S WORD (`basis.submitted`), never restated here:
  // the label is the one place the throughput lane says what it is placed by.
  return [
    narrow ? 'Success rate' : 'Success rate · cancels excluded',
    narrow ? 'Decided · ok up, failed down' : 'Decided, by the bucket it ended · succeeded up, failed down',
    `Cancelled · own scale${max(s.cancelled)}`,
    narrow
      ? `Throughput · by ${basis.submitted}${max(s.flow)}`
      : `Throughput · submitted (by ${basis.submitted}, the only lane on submission time) vs finished${max(s.flow)}`,
  ]
}

/**
 * Where an HTML lane label's box starts so its baseline sits where the SVG
 * label's did (`lane.y - 8`): the --t-micro line box is 17.4px and its
 * baseline ~12px down. It stays inside the gap above its lane (24-26px), so
 * it never covers a mark of the lane above.
 */
const LABEL_RISE = 20

/**
 * One drawing: the SVG marks and, over them, the columns a reader picks.
 */
function Drawing({
  d,
  data,
  scales,
  pickedAt,
  stopAt,
  older,
  onColumnFocus,
  onColumnPick,
  onKeys,
  onPlotScroll,
  register,
  registerPlot,
}: {
  d: LedgerDrawn
  data: Outcomes
  scales: Scales
  pickedAt: number
  stopAt: number
  /** Whether older buckets are off this drawing's left edge, for TS-3's fade. */
  older: boolean
  onColumnFocus: (i: number, el: HTMLElement) => void
  onColumnPick: (i: number) => void
  onKeys: (e: KeyboardEvent<HTMLDivElement>) => void
  onPlotScroll: (key: string, e: UIEvent<HTMLDivElement>) => void
  register: (key: string, i: number, el: HTMLDivElement | null) => void
  registerPlot: (key: string, el: HTMLDivElement | null) => void
}) {
  const { buckets, bucket, tz } = data
  const g = geometry(d, buckets.length)
  const hatch = `${useHatchId()}-${d.key}`
  const flat = `${hatch}-flat`
  const starts = buckets.map((b) => b.start)
  const labels = axisLabels(
    starts,
    bucket,
    tz,
    d.key === 'narrow' && bucket === 'hour' ? PHONE_HOUR_STRIDE : LABEL_PX / g.pitch,
    d.key === 'narrow' ? PHONE_HOUR_STRIDE : null,
  )
  const boundary = dayStarts(
    starts.map((s) => wallOf(s, tz)),
    bucket,
  )
  const [L1, L2, L3, L4] = g.lane as [Geo['lane'][0], Geo['lane'][0], Geo['lane'][0], Geo['lane'][0]]
  const words = laneLabels(d, scales, data.basis)
  const right = g.left + g.plotW
  const bottom = L4.y + L4.h

  // Lane 1: y fixed at 0-100 %.
  const ry = (p: number) => r1(L1.y + L1.h - p * L1.h)
  // Lane 2: one count scale through zero. Both sides share `k`.
  const span2 = scales.up + scales.down
  const k2 = span2 === 0 ? 0 : L2.h / span2
  const zeroY = span2 === 0 ? L2.y + L2.h / 2 : L2.y + scales.up * k2
  const k3 = scales.cancelled === 0 ? 0 : L3.h / scales.cancelled
  const k4 = scales.flow === 0 ? 0 : L4.h / scales.flow

  // The rate line's runs: consecutive buckets with a rate. Unread and
  // nothing-decided buckets break it; the current bucket joins by a dash.
  const runs: number[][] = []
  let run: number[] = []
  buckets.forEach((b, i) => {
    if (b.state !== 'unread' && b.rate !== null) run.push(i)
    else if (run.length > 0) {
      runs.push(run)
      run = []
    }
  })
  if (run.length > 0) runs.push(run)

  // The submitted step line's runs: every read bucket; unread breaks it.
  const flowRuns: number[][] = []
  let fr: number[] = []
  buckets.forEach((b, i) => {
    if (b.state !== 'unread' && b.submitted !== null) fr.push(i)
    else if (fr.length > 0) {
      flowRuns.push(fr)
      fr = []
    }
  })
  if (fr.length > 0) flowRuns.push(fr)

  // The plot layer's width: everything right of the scale column.
  const plotLayerW = g.W - g.left

  return (
    <div className={`ol-drawing is-${d.key}`} data-drawn={d.w}>
      {/* THE SCALE COLUMN: every tick, pinned, outside the scroller. */}
      <svg
        className="ol-gutter"
        width={g.left}
        height={g.H}
        viewBox={`0 0 ${g.left} ${g.H}`}
        aria-hidden="true"
        focusable="false"
      >
        {[1, 0.5, 0].map((t) => (
          <text key={`r${t}`} className="ol-tick" x={g.left - 6} y={ry(t) + 4} textAnchor="end">{`${t * 100}%`}</text>
        ))}
        <text className="ol-tick" x={g.left - 6} y={r1(zeroY) + 4} textAnchor="end">
          0
        </text>
        {scales.up > 0 && (
          <text className="ol-tick" x={g.left - 6} y={L2.y + 8} textAnchor="end">
            {scales.up}
          </text>
        )}
        {scales.down > 0 && (
          <text className="ol-tick" x={g.left - 6} y={L2.y + L2.h} textAnchor="end">
            {scales.down}
          </text>
        )}
      </svg>

      {/* THE LANE LABELS, pinned over the plot's left edge on the page's ground. */}
      {g.lane.map((l, i) => (
        <span
          key={`lab${i}`}
          className="ol-lane-label"
          aria-hidden="true"
          style={{ left: `${g.left}px`, top: `${l.y - LABEL_RISE}px` }}
        >
          {words[i]}
        </span>
      ))}

      <div
        ref={(el) => registerPlot(d.key, el)}
        className={older ? 'ol-plot has-older' : 'ol-plot'}
        style={{ marginLeft: `${g.left}px` }}
        onScroll={(e) => onPlotScroll(d.key, e)}
      >
        <div className="ol-canvas" style={{ width: `${plotLayerW}px`, height: `${g.H}px` }}>
          <svg
            className="ol-svg"
            width={plotLayerW}
            height={g.H}
            viewBox={`${g.left} 0 ${plotLayerW} ${g.H}`}
            aria-hidden="true"
            focusable="false"
          >
            <HatchDef id={hatch} />
            <defs>
              {/* TS-4's flat "ended" bars: a 3px bar every 5px, anchored to the page so columns line up. */}
              <pattern id={flat} width={4} height={5} patternUnits="userSpaceOnUse">
                <rect className="ol-flat" x={0} y={0} width={4} height={3} />
              </pattern>
            </defs>

            {/* The pick: a surface step and a 2px ink rule over the column, no hue (§1.3). */}
            {pickedAt >= 0 && (
              <g className="ol-picked">
                <rect className="ol-sel" x={r1(g.x0(pickedAt))} y={TOP - 18} width={r1(g.pitch)} height={r1(bottom - TOP + 20)} />
                <rect className="ol-sel-rule" x={r1(g.x0(pickedAt))} y={TOP - 18} width={r1(g.pitch)} height={2} />
              </g>
            )}

            {/* The scale's rules, which run under the columns; their numbers are in the gutter. */}
            <g className="ol-scale">
              {[1, 0.5, 0].map((t) => (
                <line key={`r${t}`} className={t === 0 ? 'ol-base' : 'ol-grid'} x1={g.left} x2={right} y1={ry(t)} y2={ry(t)} />
              ))}
              <line className="ol-base" x1={g.left} x2={right} y1={r1(zeroY)} y2={r1(zeroY)} />
              <line className="ol-base" x1={g.left} x2={right} y1={L3.y + L3.h} y2={L3.y + L3.h} />
              <line className="ol-base" x1={g.left} x2={right} y1={L4.y + L4.h} y2={L4.y + L4.h} />
            </g>

            {/* Day boundaries on an hourly axis, drawn as well as labelled. */}
            {boundary.map((b, i) =>
              b && i > 0 ? (
                <line key={`day${i}`} className="ol-day" x1={r1(g.x0(i))} x2={r1(g.x0(i))} y1={L1.y - 4} y2={bottom} />
              ) : null,
            )}

            {/* Lane 1: the band, then the line, then the points. */}
            <g className="ol-lane is-rate">
              {runs.map((rr, ri) => {
                const pts = rr.map((i) => ({ i, r: buckets[i]!.rate!, now: buckets[i]!.in_progress }))
                const hi = pts.map((p) => `${r1(g.cx(p.i))},${ry(p.r.hi)}`).join(' ')
                const lo = pts
                  .slice()
                  .reverse()
                  .map((p) => `${r1(g.cx(p.i))},${ry(p.r.lo)}`)
                  .join(' ')
                const last = pts[pts.length - 1]!
                const solid = last.now ? pts.slice(0, -1) : pts
                return (
                  <g key={`run${ri}`} data-run={ri}>
                    {pts.length > 1 && <polygon className="ol-m-band" points={`${hi} ${lo}`} />}
                    {pts.length > 1 && <polyline className="ol-m-edge" points={hi} />}
                    {pts.length > 1 && (
                      <polyline className="ol-m-edge" points={pts.map((p) => `${r1(g.cx(p.i))},${ry(p.r.lo)}`).join(' ')} />
                    )}
                    {solid.length > 1 && (
                      <polyline className="ol-m-rate" points={solid.map((p) => `${r1(g.cx(p.i))},${ry(p.r.p)}`).join(' ')} />
                    )}
                    {last.now && pts.length > 1 && (
                      <polyline
                        className="ol-m-rate is-so-far"
                        points={`${r1(g.cx(pts[pts.length - 2]!.i))},${ry(pts[pts.length - 2]!.r.p)} ${r1(g.cx(last.i))},${ry(last.r.p)}`}
                      />
                    )}
                    {pts.map((p) => (
                      <circle
                        key={`pt${p.i}`}
                        className={p.r.n >= LOW_N && !p.now ? 'ol-m-pt' : 'ol-m-pt is-hollow'}
                        cx={r1(g.cx(p.i))}
                        cy={ry(p.r.p)}
                        r={3}
                        data-i={p.i}
                      />
                    ))}
                  </g>
                )
              })}
            </g>

            {/* Lanes 2-4, and the unread bands, per bucket. */}
            {buckets.map((b, i) => {
              const x = r1(g.cx(i) - g.bar / 2)
              const w = r1(g.bar)
              const tick = (y: number, key: string) => (
                <line key={key} className="ol-zero" x1={r1(g.cx(i) - 3)} x2={r1(g.cx(i) + 3)} y1={r1(y)} y2={r1(y)} />
              )
              if (b.state === 'unread') {
                return (
                  <rect
                    key={b.start}
                    className="ol-unread"
                    data-i={i}
                    x={r1(g.x0(i))}
                    y={L1.y}
                    width={r1(g.pitch)}
                    height={bottom - L1.y}
                    fill={`url(#${hatch})`}
                  />
                )
              }
              const ok = b.succeeded ?? 0
              const bad = (b.failed ?? 0) + (b.dead_lettered ?? 0)
              const flatN = (b.cancelled?.requested ?? 0) + (b.cancelled?.other ?? 0)
              const cascN = b.cancelled?.after_cancel ?? 0
              const afterN = (b.cancelled?.after_failure ?? 0) + (b.cancelled?.workflow_sweep ?? 0)
              const [flatH, cascH, afterH] = laneThreeStack(flatN, cascN, afterN, k3, L3.h)
              // The stack's edges, rounded ONCE each, so neighbouring marks
              // share an edge exactly and none passes the lane's top or base.
              const e0 = L3.y + L3.h
              const e1 = r1(e0 - flatH)
              const e2 = r1(e0 - flatH - cascH)
              const e3 = r1(e0 - flatH - cascH - afterH)
              const fin = b.ended ?? 0
              const sub = b.submitted ?? 0
              return (
                <g key={b.start} className="ol-bucket" data-i={i}>
                  {/* Lane 2 */}
                  {ok > 0 && <rect className="ol-m-ok" x={x} y={r1(zeroY - ok * k2)} width={w} height={r1(ok * k2)} />}
                  {bad > 0 && (
                    <>
                      <rect className="ol-m-bad" x={x} y={r1(zeroY)} width={w} height={r1(Math.max(MIN_FAILED_PX, bad * k2))} />
                      <rect className="ol-m-cut" x={x} y={r1(zeroY)} width={Math.min(2, w)} height={r1(Math.max(MIN_FAILED_PX, bad * k2))} />
                    </>
                  )}
                  {ok === 0 && bad === 0 && tick(zeroY, 'z2')}
                  {/* Lane 3 */}
                  {flatH > 0 && (
                    <rect className="ol-m-ended" x={x} y={e1} width={w} height={r1(e0 - e1)} fill={`url(#${flat})`} />
                  )}
                  {cascH > 0 && (
                    <g className="ol-m-casc">
                      {/* After a cancel: the flat bars (a cancel) in the outline (a cascade),
                          the bars counted from the mark's own top so one always shows. */}
                      {innerBars(e2, e1).map((bar) => (
                        <rect
                          key={bar.y}
                          className="ol-flat"
                          x={r1(x + 1)}
                          y={bar.y}
                          width={r1(Math.max(1, w - 2))}
                          height={bar.h}
                        />
                      ))}
                      <rect
                        className="ol-m-after-cancel"
                        x={r1(x + 0.5)}
                        y={r1(e2 + 0.5)}
                        width={r1(Math.max(0, w - 1))}
                        height={r1(Math.max(0, e1 - e2 - 1))}
                      />
                    </g>
                  )}
                  {afterH > 0 && (
                    <rect
                      className="ol-m-after"
                      x={r1(x + 0.5)}
                      y={r1(e3 + 0.5)}
                      width={r1(Math.max(0, w - 1))}
                      height={r1(Math.max(0, e2 - e3 - 1))}
                    />
                  )}
                  {flatN + cascN + afterN === 0 && tick(L3.y + L3.h, 'z3')}
                  {/* Lane 4: finished columns; the submitted line is drawn once below. */}
                  {fin > 0 && <rect className="ol-m-fin" x={x} y={r1(L4.y + L4.h - fin * k4)} width={w} height={r1(fin * k4)} />}
                  {fin === 0 && sub === 0 && tick(L4.y + L4.h, 'z4')}
                  {/* The current bucket's dashed right edge: the side the missing part is on. */}
                  {b.in_progress && (
                    <line className="ol-so-far" x1={r1(g.x0(i) + g.pitch)} x2={r1(g.x0(i) + g.pitch)} y1={L1.y} y2={bottom} />
                  )}
                </g>
              )
            })}

            <g className="ol-lane is-flow">
              {flowRuns.map((rr, ri) => {
                const y = (i: number) => r1(L4.y + L4.h - (buckets[i]!.submitted ?? 0) * k4)
                const d0 = rr
                  .map((i, j) => `${j === 0 ? 'M' : 'L'}${r1(g.x0(i))},${y(i)} L${r1(g.x0(i) + g.pitch)},${y(i)}`)
                  .join(' ')
                return <path key={`flow${ri}`} className="ol-m-sub" d={d0} />
              })}
            </g>

            {/* The time axis. */}
            {labels.map((l, i) =>
              l === '' ? null : (
                <text key={`ax${i}`} className="ol-tick ol-axis-label" x={r1(g.cx(i))} y={g.axisY} textAnchor="middle">
                  {l}
                </text>
              ),
            )}
          </svg>

          <div
            className="ol-cols"
            role="group"
            aria-label={`Outcomes by ${bucket}, ${buckets.length} ${buckets.length === 1 ? 'bucket' : 'buckets'}, four lanes`}
            style={{ left: 0, width: `${r1(g.plotW)}px`, height: `${g.H}px` }}
            onKeyDown={onKeys}
          >
            {buckets.map((b, i) => (
              <div
                key={b.start}
                ref={(el) => register(d.key, i, el)}
                className={i === pickedAt ? 'ol-col is-picked' : 'ol-col'}
                role="img"
                aria-label={bucketSaid(b, bucket, tz)}
                tabIndex={i === stopAt ? 0 : -1}
                data-i={i}
                data-start={b.start}
                data-state={b.state}
                style={{ left: `${r1(i * g.pitch)}px`, width: `${r1(g.pitch)}px` }}
                onMouseEnter={() => onColumnPick(i)}
                onClick={() => onColumnPick(i)}
                onFocus={(e) => onColumnFocus(i, e.currentTarget)}
              />
            ))}
          </div>
        </div>
      </div>
    </div>
  )
}

/** A bucket's full name, with `so far` on the current one. */
function atName(b: OutcomeBucket, bucket: LedgerBucketSize, tz: string): string {
  return `${bucketName(b.start, bucket, tz)}${b.in_progress ? ' · so far' : ''}`
}

/** The values a readout prints: the span's totals, or one read bucket's. */
interface Shown {
  succeeded: number
  failed: number
  dead_lettered: number
  requested: number
  other: number
  after_failure: number
  after_cancel: number
  workflow_sweep: number
  cancelled: number
  submitted: number
  ended: number
  rate: Outcomes['totals']['rate']
}

function shownOf(data: Outcomes, b: OutcomeBucket | null): Shown {
  if (b === null) {
    const t = data.totals
    return {
      succeeded: t.succeeded,
      failed: t.failed,
      dead_lettered: t.dead_lettered,
      requested: t.cancelled.requested,
      other: t.cancelled.other,
      after_failure: t.cancelled.after_failure,
      after_cancel: t.cancelled.after_cancel,
      workflow_sweep: t.cancelled.workflow_sweep,
      cancelled: t.cancelled.total,
      submitted: t.submitted,
      ended: t.ended,
      rate: t.rate,
    }
  }
  return {
    succeeded: b.succeeded ?? 0,
    failed: b.failed ?? 0,
    dead_lettered: b.dead_lettered ?? 0,
    requested: b.cancelled?.requested ?? 0,
    other: b.cancelled?.other ?? 0,
    after_failure: b.cancelled?.after_failure ?? 0,
    after_cancel: b.cancelled?.after_cancel ?? 0,
    workflow_sweep: b.cancelled?.workflow_sweep ?? 0,
    cancelled: b.cancelled?.total ?? 0,
    submitted: b.submitted ?? 0,
    ended: b.ended ?? 0,
    rate: b.rate,
  }
}

export interface OutcomeLedgerProps {
  data: Outcomes
  /** The picked bucket, by its start -- never by index, which a re-read would silently repoint. */
  picked: string | null
  onPick: (start: string | null) => void
  /** Zoom the whole page to one bucket. Absent on an hourly axis, where there is nothing smaller to show. */
  onZoom?: ((b: OutcomeBucket) => void) | undefined
}

/**
 * The ledger and its readout. THE LEGEND IS THE READOUT (TS-9): with nothing
 * picked it prints the span's totals; hover, tap and focus pick a bucket and
 * it prints that bucket's full name and values; `all`, Escape and leaving the
 * chart and its readout put the span back. It is not a live region: the
 * focused column's name already says the same counts.
 */
export function OutcomeLedger({ data, picked, onPick, onZoom }: OutcomeLedgerProps) {
  const { buckets, bucket, tz } = data
  const starts = useMemo(() => buckets.map((b) => b.start), [buckets])
  const scales = useMemo(() => scalesOf(buckets), [buckets])
  const pickedAt = picked === null ? -1 : starts.indexOf(picked)
  const [stop, setStop] = useState<string | null>(null)
  const stopAt = stop !== null && starts.includes(stop) ? starts.indexOf(stop) : starts.length - 1
  const pickedBucket = pickedAt >= 0 ? buckets[pickedAt]! : null

  // Each drawing's columns, so a key press moves focus within the drawing it came from.
  const cols = useRef(new Map<string, Array<HTMLDivElement | null>>())
  const register = (key: string, i: number, el: HTMLDivElement | null) => {
    const list = cols.current.get(key) ?? []
    list[i] = el
    cols.current.set(key, list)
  }
  const lastFocused = useRef<HTMLElement | null>(null)
  const quiet = useRef(false)
  const allButton = useRef<HTMLButtonElement>(null)

  // TS-3: OPEN AT THE NEWEST END, and fade the left edge only while something
  // older is off-screen. Keyed on the axis, so a re-read of the same span
  // leaves a reader who scrolled back where they were.
  //
  // ONE SCROLLER PER DRAWING, each opened the first time it is SHOWN: a
  // drawing under `display: none` has no width to scroll, so it is opened when
  // the chart's box changes and the sheet swaps it in (a phone turned
  // sideways), not left at its oldest bucket. A drawing already opened keeps
  // wherever its reader scrolled it.
  const figure = useRef<HTMLElement>(null)
  const plots = useRef(new Map<string, HTMLDivElement | null>())
  const registerPlot = (key: string, el: HTMLDivElement | null) => {
    plots.current.set(key, el)
  }
  const [older, setOlder] = useState<Readonly<Record<string, boolean>>>({})
  const axis = `${bucket}:${starts[0] ?? ''}:${starts[starts.length - 1] ?? ''}`
  const opened = useRef<{ axis: string; keys: Set<string> }>({ axis: '', keys: new Set() })
  const openNewest = useCallback(() => {
    if (opened.current.axis !== axis) opened.current = { axis, keys: new Set() }
    const seen: Record<string, boolean> = {}
    for (const [key, el] of plots.current) {
      if (el === null || el.clientWidth === 0) continue
      if (!opened.current.keys.has(key)) {
        el.scrollLeft = el.scrollWidth
        opened.current.keys.add(key)
      }
      seen[key] = el.scrollLeft > 0
    }
    setOlder((o) => (Object.keys(seen).every((k) => o[k] === seen[k]) ? o : { ...o, ...seen }))
  }, [axis])
  useLayoutEffect(() => {
    openNewest()
  }, [openNewest])
  useEffect(() => {
    const el = figure.current
    if (el === null || typeof ResizeObserver === 'undefined') return
    const watch = new ResizeObserver(() => openNewest())
    watch.observe(el)
    return () => watch.disconnect()
  }, [openNewest])
  const onPlotScroll = (key: string, e: UIEvent<HTMLDivElement>) => {
    const now = e.currentTarget.scrollLeft > 0
    setOlder((o) => (o[key] === now ? o : { ...o, [key]: now }))
  }

  const drawingOf = (el: Element | null): string | null =>
    el?.closest('.ol-drawing')?.className.match(/is-(wide|mid|narrow)/)?.[1] ?? null

  const restore = () => {
    const hadFocus = allButton.current !== null && allButton.current === document.activeElement
    onPick(null)
    if (!hadFocus) return
    const key = drawingOf(lastFocused.current) ?? 'wide'
    quiet.current = true
    try {
      cols.current.get(key)?.[stopAt]?.focus({ preventScroll: true })
    } finally {
      quiet.current = false
    }
  }

  const onColumnPick = (i: number) => onPick(starts[i] ?? null)
  const onColumnFocus = (i: number, el: HTMLElement) => {
    lastFocused.current = el
    setStop(starts[i] ?? null)
    if (quiet.current) return
    onPick(starts[i] ?? null)
    el.scrollIntoView?.({ block: 'nearest', inline: 'nearest' })
  }
  const onKeys = (e: KeyboardEvent<HTMLDivElement>) => {
    const last = starts.length - 1
    const from = Number((e.target as HTMLElement).getAttribute('data-i') ?? stopAt)
    const next =
      e.key === 'ArrowRight'
        ? Math.min(last, from + 1)
        : e.key === 'ArrowLeft'
          ? Math.max(0, from - 1)
          : e.key === 'Home'
            ? 0
            : e.key === 'End'
              ? last
              : null
    if (next === null || last < 0) return
    e.preventDefault()
    const key = drawingOf(e.currentTarget) ?? 'wide'
    cols.current.get(key)?.[next]?.focus()
  }
  const onReadoutKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === 'Escape') restore()
  }
  const onBlur = (e: FocusEvent<HTMLDivElement>) => {
    const to = e.relatedTarget
    if (!(to instanceof Node) || !e.currentTarget.contains(to)) onPick(null)
  }
  // The phone's 44px steps (§7.2): a 26px column is not a 44px target.
  const step = (by: number) => {
    if (starts.length === 0) return
    const from = pickedAt >= 0 ? pickedAt : stopAt
    const to = Math.max(0, Math.min(starts.length - 1, from + by))
    onPick(starts[to] ?? null)
    setStop(starts[to] ?? null)
  }

  const shown = shownOf(data, pickedBucket !== null && pickedBucket.state !== 'unread' ? pickedBucket : null)
  const settleMin = Math.round(data.coverage.seal_grace_s / 60)
  const failedThere = pickedBucket === null ? 0 : (pickedBucket.failed ?? 0) + (pickedBucket.dead_lettered ?? 0)
  const units = unitWord(bucket)
  // THE SPAN'S TOTALS ARE SUMS OVER THE READ BUCKETS ONLY (TS-9): partial
  // when one was not read, and no figure at all when none was.
  const cov = spanCoverage(data)
  const spanNotRead = pickedBucket === null && cov.none

  return (
    <div className="ol-chart-readout" onMouseLeave={restore} onBlur={onBlur} onKeyDown={onReadoutKeyDown}>
      <figure ref={figure} className="ol-chart" aria-label={`Outcome ledger by ${bucket}, ${buckets.length} ${units}`}>
        {LEDGER_DRAWN.map((d) => (
          <Drawing
            key={d.key}
            d={d}
            data={data}
            scales={scales}
            pickedAt={pickedAt}
            stopAt={stopAt}
            older={older[d.key] === true}
            onColumnFocus={onColumnFocus}
            onColumnPick={onColumnPick}
            onKeys={onKeys}
            onPlotScroll={onPlotScroll}
            register={register}
            registerPlot={registerPlot}
          />
        ))}
      </figure>

      <div className="ol-readout">
        <p className="ol-readout-head">
          <button type="button" className="ol-step" aria-label="Earlier bucket" onClick={() => step(-1)}>
            ‹
          </button>
          <b className="ol-at">
            {pickedBucket !== null ? atName(pickedBucket, bucket, tz) : `all ${buckets.length} ${units}`}
          </b>
          {pickedBucket === null && !cov.complete && !cov.none && (
            <span className="ol-at-cover">
              <Mark
                kind="partial"
                say={`${cov.of - cov.read} of the span's ${cov.of} ${cov.unit} could not be read, so these totals cover ${cov.read} of them and each is a floor.`}
              />{' '}
              {cov.read} of {cov.of} {cov.unit}
            </span>
          )}
          <button type="button" className="ol-step" aria-label="Later bucket" onClick={() => step(1)}>
            ›
          </button>
        </p>

        {spanNotRead ? (
          <p className="ol-legend">
            <Mark
              kind="unread"
              say={`None of the span's ${cov.of} ${cov.unit} could be read, so there is no total to print: ${cov.reasons.join('; ')}.`}
            />{' '}
            <span className="ol-q">{nothingReadWords(cov)}</span>
          </p>
        ) : pickedBucket !== null && pickedBucket.state === 'unread' ? (
          <p className="ol-legend">
            <Mark
              kind="unread"
              say={`${bucketName(pickedBucket.start, bucket, tz)} was not read: ${unreadWords(pickedBucket.unread_reason)}. It is left out of every total, and no count is drawn for it.`}
            />{' '}
            <span className="ol-q">{unreadWords(pickedBucket.unread_reason)}</span>
          </p>
        ) : (
          <p className="ol-legend">
            <span className="ol-li">
              <i className="ol-k is-rate" aria-hidden /> rate{' '}
              {shown.rate === null ? (
                <b className="ol-n is-phrase">no finished work</b>
              ) : (
                <>
                  <b className="ol-n">{pct(shown.rate.p)}</b>{' '}
                  <span className="ol-q">
                    {shown.rate.k} of {shown.rate.n} · 95 % {interval(shown.rate)}
                  </span>
                </>
              )}
            </span>
            <span className="ol-li">
              <i className="ol-k is-ok" aria-hidden /> succeeded <b className="ol-n">{shown.succeeded}</b>
            </span>
            <span className="ol-li">
              <i className="ol-k is-bad" aria-hidden /> failed <b className="ol-n">{shown.failed}</b>
            </span>
            <span className="ol-li">
              dead-lettered <b className="ol-n">{shown.dead_lettered}</b>
            </span>
            {/* ONE NUMBER PER KEY, AND IT IS THE NUMBER THE KEY'S MARK DRAWS
                (TS-9). The flat bars are requested + other, the outlined bars
                after_cancel, and the bare outline after_failure +
                workflow_sweep -- lane 3's three marks -- so the cancelled
                total, which no single mark draws, stands unkeyed. */}
            <span className="ol-li">
              cancelled <b className="ol-n">{shown.cancelled}</b>
            </span>
            <span className="ol-li">
              <i className="ol-k is-ended" aria-hidden /> requested or other{' '}
              <b className="ol-n">{shown.requested + shown.other}</b>{' '}
              <span className="ol-q">
                requested {shown.requested} · other {shown.other}
              </span>
            </span>
            <span className="ol-li">
              <i className="ol-k is-after" aria-hidden /> after a failure{' '}
              <b className="ol-n">{shown.after_failure + shown.workflow_sweep}</b>{' '}
              <span className="ol-q">incl. workflow sweep {shown.workflow_sweep}</span>
            </span>
            <span className="ol-li">
              <i className="ol-k is-after-cancel" aria-hidden /> after a cancel{' '}
              <b className="ol-n">{shown.after_cancel}</b>
            </span>
            <span className="ol-li">
              <i className="ol-k is-sub" aria-hidden /> submitted <b className="ol-n">{shown.submitted}</b>{' '}
              <span className="ol-q">by {data.basis.submitted}</span>
            </span>
            <span className="ol-li">
              <i className="ol-k is-fin" aria-hidden /> finished <b className="ol-n">{shown.ended}</b>
            </span>
            {pickedBucket !== null && pickedBucket.state === 'open' && !pickedBucket.in_progress && (
              <span className="ol-li ol-q">settling, final {settleMin} min after the bucket</span>
            )}
          </p>
        )}

        <p className="ol-keys" aria-hidden>
          <span>
            <i className="ol-k is-zero" /> real zero
          </span>
          <span>
            <i className="ol-k is-so-far" /> so far
          </span>
          <span>
            <i className="ol-k is-unread" /> not read
          </span>
          <span>
            <i className="ol-k is-hollow" /> under {LOW_N} decided
          </span>
        </p>

        {pickedBucket !== null && (
          <p className="ol-actions">
            <button type="button" className="sbf-mini ol-all" ref={allButton} onClick={restore}>
              all
            </button>
            {onZoom !== undefined && bucket !== 'hour' && (
              <button type="button" className="sbf-mini ol-zoom" onClick={() => onZoom(pickedBucket)}>
                zoom to {bucketName(pickedBucket.start, bucket, tz).split(' · ')[0]}
              </button>
            )}
            {failedThere > 0 && (
              // The Agents list cannot filter by completed_at (the drill-through
              // route is not in #185's contract), so the link says what it
              // cannot do rather than pretending to be the day's list.
              <a className="ctl-link ol-drill" href="#work/running/recent/failed">
                {failedThere} failed that {bucket === 'hour' ? 'hour' : bucket} →{' '}
                <span className="ol-q">not limited to {bucketName(pickedBucket.start, bucket, tz).split(' · ')[0]}</span>
              </a>
            )}
          </p>
        )}
      </div>
    </div>
  )
}
