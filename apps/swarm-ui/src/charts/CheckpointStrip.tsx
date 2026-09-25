// Checkpoint cadence, along one attempt (redesign-v2 §4 viz #6).
//
// One dot per checkpoint at T+ from the attempt's start; dot AREA is the
// checkpoint's `size_bytes`; a HOLLOW dot is a checkpoint whose location is
// not known on this page. The table under this strip is not replaced -- it
// still carries every id, size and uri as text. The strip adds the one thing
// a table of ids cannot show: whether checkpoints were written steadily
// through the attempt or stopped partway, which is the property CONTRACT
// invariant 8 (checkpointing is mandatory and periodic) is about.
//
// TWO RECORDS, NEITHER COMPLETE (`checkpointsFor`, types.ts). The attempt
// document lists ids only; the `checkpoint_completed` event carries the time,
// size and uri. So:
//
//   * a checkpoint whose event is ON the page has a position, and a size if
//     the event carried one;
//   * a checkpoint whose event is NOT on the page has NO POSITION AT ALL. It
//     is not drawn on the time axis -- where it would claim an instant nobody
//     recorded -- but in a separate tray to the right of the axis, hollow, one
//     ring per id for as many as the tray has room for and a `+N` for the
//     rest (`trayLayout`), so it is counted and never silently lost.
//
// A hollow dot is NOT a broken checkpoint (types.ts, and redesign-v2 §4 row 6
// "what it must not imply"): this screen reads one page of events,
// oldest-first, and does not follow the page token the events route returns
// (help topic `event-paging`), so the LATER checkpoints of a long attempt are
// exactly the ones that fall off. And the strip says nothing about what
// is inside a checkpoint: contents are recorded nowhere.
//
// SIZE IS AREA, NOT RADIUS. A radius proportional to bytes makes a checkpoint
// twice the size look four times as big. The radius is proportional to the
// square root, with a 2px floor so the smallest real checkpoint stays a
// visible mark; the floor is the one distortion, and it only ever makes a
// small checkpoint look bigger than it is, never a big one smaller. A
// checkpoint with no recorded size gets a fixed small DASHED ring: no area,
// because no size.

import { instant, spanText } from '../duration'
import { bytesLabel, checkpointsFor, type AttemptRow, type CheckpointRow, type TaskEvent } from '../types'
import { ChartTitle, DRAWN, drawnClass, type Drawn } from './parts'
import { ValueAxis, linearScale } from './TimeSeries'

// NO WIDTH HERE (AG-20): each drawing in `DRAWN` (parts.tsx) brings its own.
const H = 46
const M = { top: 10, right: 14, bottom: 20, left: 14 }
const R_MAX = 7
const R_MIN = 2
const R_UNSIZED = 3
const TRAY_STEP = 12
/** From the axis's end to the first ring: the divider sits halfway along it. */
const TRAY_GAP = 20
/**
 * Room for the tray's `+N`: four characters of `--t-micro` mono ("+999") at
 * about 7.2 units each, and the space before them.
 */
const MORE_W = 36

/**
 * THE TRAY IS BOUNDED, AND THE AXIS KEEPS AT LEAST HALF THE PLOT (fix-up on
 * #170). The tray was `20 + 12n` units wide with no upper bound and was
 * subtracted from the plot, so in the 300-unit drawing -- the one the 480px
 * inspector and every phone show -- the time axis was 252 - 12n wide: zero at
 * 21 off-page checkpoints and negative past it, and `linearScale` does not
 * clamp, so the track, the dots and the axis were drawn backwards into the
 * margin with the tray on top of them. The worker checkpoints every 120s
 * (agent_worker/config.py), so a 42-minute attempt whose event read failed
 * puts 21 there. The 640 drawing broke the same way at 50.
 *
 * So the tray gets at most half of the drawing's plot width, and within it as
 * many rings as fit; past that it draws as many as fit beside a `+N` and
 * COUNTS the rest. None is dropped: every id is still a row in the attempt's
 * checkpoint table under this strip, and the count says how many are not
 * drawn here. Whatever the tray holds, the axis is at least half the plot.
 */
function trayLayout(d: Drawn, unplaced: number): { width: number; shown: number } {
  if (unplaced === 0) return { width: 0, shown: 0 }
  const plot = d.w - M.left - M.right
  const budget = Math.floor(plot / 2)
  const fits = Math.floor((budget - TRAY_GAP) / TRAY_STEP)
  if (unplaced <= fits) return { width: TRAY_GAP + unplaced * TRAY_STEP, shown: unplaced }
  const shown = Math.max(0, Math.floor((budget - TRAY_GAP - MORE_W) / TRAY_STEP))
  return { width: TRAY_GAP + shown * TRAY_STEP + MORE_W, shown }
}

export function CheckpointStrip({
  attempt,
  events,
}: {
  attempt: AttemptRow
  events: TaskEvent[] | null
}) {
  const rows = checkpointsFor(attempt, events)
  if (rows.length === 0) return null

  // The origin is the attempt's start, the same one the peak-memory line uses.
  // An attempt with checkpoints and no start time cannot happen in the worker,
  // and if it ever does every checkpoint goes to the tray rather than being
  // positioned against a different origin.
  const origin = instant(attempt.started_at)
  const placed: { row: CheckpointRow; t: number }[] = []
  const unplaced: CheckpointRow[] = []
  for (const row of rows) {
    const at = instant(row.at)
    if (origin === null || at === null) unplaced.push(row)
    else placed.push({ row, t: at - origin })
  }

  const end = origin === null ? null : instant(attempt.completed_at)
  const ts = placed.map((p) => p.t)
  if (end !== null && origin !== null) ts.push(end - origin)
  const lo = Math.min(0, ...ts)
  const hi = Math.max(0, ...ts)
  const extent = { lo, hi, degenerate: lo === hi }

  const maxBytes = Math.max(0, ...placed.map((p) => p.row.bytes ?? 0))
  const radius = (b: number | null) =>
    b === null ? R_UNSIZED : maxBytes > 0 ? Math.max(R_MIN, R_MAX * Math.sqrt(b / maxBytes)) : R_MIN
  const hollow = rows.some((r) => r.uri === null)
  const mid = (H - M.top - M.bottom) / 2

  return (
    <figure className="ctl-chart ctl-ckpt-strip has-narrow" aria-label="Checkpoint cadence">
      <ChartTitle>Checkpoint cadence</ChartTitle>
      {hollow && (
        <ul className="ctl-chart-legend">
          <li>
            <span className="ctl-swatch is-ring" aria-hidden="true" />
            no location
          </li>
        </ul>
      )}
      {/* TWICE, NOT SCALED (AG-20): one drawing per width, each its own
          scale, tick count and tray -- the tray is bounded by the width it is
          drawn at (`trayLayout`), so the axis keeps at least half the plot. */}
      {DRAWN.map((d) => {
        const tray = trayLayout(d, unplaced.length)
        const innerW = d.w - M.left - M.right - tray.width
        const x = linearScale(extent, [0, innerW])
        const drawn = unplaced.slice(0, tray.shown)
        const more = unplaced.length - drawn.length
        return (
          <svg
            key={d.key}
            className={drawnClass(d)}
            width={d.w}
            height={H}
            viewBox={`0 0 ${d.w} ${H}`}
            role="group"
            aria-label="When each checkpoint of this attempt completed, from the start of the attempt, sized by bytes"
          >
            <g transform={`translate(${M.left},${M.top})`}>
              <line className="ctl-ckpt-track" x1={0} x2={innerW} y1={mid} y2={mid} />
              {placed.map(({ row, t }) => (
                <circle
                  key={row.checkpoint_id}
                  className={dotClass(row)}
                  data-testid="ckpt-dot"
                  data-id={row.checkpoint_id}
                  data-bytes={row.bytes ?? ''}
                  cx={x(t)}
                  cy={mid}
                  r={radius(row.bytes)}
                >
                  <title>{`${row.checkpoint_id} · T+${spanText(t)} · ${row.bytes === null ? 'size —' : bytesLabel(row.bytes)}`}</title>
                </circle>
              ))}
              {unplaced.length > 0 && (
                // THE TRAY. Past a divider, off the time axis, in id order: these
                // have no recorded instant, so they have no x.
                <g data-testid="ckpt-tray" transform={`translate(${innerW + TRAY_GAP},0)`}>
                  <line className="ctl-ckpt-divider" x1={-TRAY_GAP / 2} x2={-TRAY_GAP / 2} y1={-4} y2={mid * 2 + 4} />
                  {drawn.map((row, i) => (
                    <circle
                      key={row.checkpoint_id}
                      className="ctl-ckpt-dot is-hollow is-unsized"
                      data-testid="ckpt-offpage"
                      data-id={row.checkpoint_id}
                      cx={i * TRAY_STEP}
                      cy={mid}
                      r={R_UNSIZED}
                      role="img"
                      aria-label={`${row.checkpoint_id}: listed on the attempt document; its event is not on this page, so when it completed, its size and its location are unknown here. Not evidence that it is broken.`}
                    >
                      <title>{`${row.checkpoint_id} · not on this page`}</title>
                    </circle>
                  ))}
                  {more > 0 && (
                    // COUNTED, NOT DROPPED. The figure is the text; the
                    // sentence is the group's name, so it is reachable
                    // without a hover.
                    <g
                      data-testid="ckpt-more"
                      role="img"
                      aria-label={`${more} more checkpoint${more === 1 ? '' : 's'} listed on the attempt document whose event${more === 1 ? ' is' : 's are'} not on this page, not drawn here for room. Each is a row in the checkpoint table below. Not evidence that any is broken.`}
                    >
                      <title>{`${more} more not on this page`}</title>
                      <text className="ctl-ckpt-more" x={drawn.length * TRAY_STEP - R_UNSIZED} y={mid} dy="0.35em">
                        {`+${more}`}
                      </text>
                    </g>
                  )}
                </g>
              )}
              {placed.length > 0 || end !== null ? (
                <ValueAxis
                  side="bottom"
                  top={mid * 2}
                  scale={x}
                  extent={extent}
                  format={(v) => `T+${spanText(v)}`}
                  ticks={d.ticks}
                  minGapPx={64}
                />
              ) : null}
            </g>
          </svg>
        )
      })}
    </figure>
  )
}

function dotClass(row: CheckpointRow): string {
  return [
    'ctl-ckpt-dot',
    row.uri === null ? 'is-hollow' : '',
    row.bytes === null ? 'is-unsized' : '',
  ]
    .filter(Boolean)
    .join(' ')
}
