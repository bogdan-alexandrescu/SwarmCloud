/**
 * WORK › TIMELINE › LANES (timeline.html pick A, owner 2026-10-02).
 *
 * One lane per agent over wall-clock time, grouped under its workflow,
 * standalone agents last. The marks are `lanes.ts`'s; this file reads and
 * draws them. Its reads, each with its own honest state:
 *
 *   GET /v1/attempts             the bars. Newest first, paged; "Read the next
 *                                page" follows the token. How many pages are
 *                                left is a dash: the route returns a token,
 *                                never a total.
 *   GET /v1/tasks                names, profiles, states and workflows for the
 *                                lanes, paged back across the span
 *                                (`taskReadCovers`); a task the pages did not
 *                                reach is read on its own (`loadTask`), up to
 *                                MISSING_TASK_READS.
 *   GET /v1/workflows?ids=       each workflow drawn, 50 to a read (CH-6): its
 *                                state, and the steps that never had an attempt
 *                                (drawn, no bar). `GET /v1/workflows/{id}` each
 *                                only where the API does not take `ids`.
 *   GET /v1/tasks/{id}/events    ONE PAGE PER LANE ON SCREEN, newest first
 *                                (`order=desc`, redesign-v2 S1's UI half): the
 *                                park, fence and cancel marks. The open lane's
 *                                list follows the page token for older events.
 *   GET /v1/outcomes             the one-line strip above the lanes, on its
 *                                own, so its failure leaves the lanes drawn.
 *
 * State, profile, workflow and kind filters run HERE, over the rows read,
 * because `/v1/attempts` takes none of them -- and the coverage line says so.
 *
 * Shared components (U0's, components.html A) are not on main yet, so the few
 * this page needs are local and prefixed `Tl` for a later pass to swap.
 */
import { Fragment, useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type PointerEvent as ReactPointerEvent, type ReactNode } from 'react'

import {
  loadAttemptsPage,
  loadOutcomes,
  loadTask,
  loadTaskEventsPage,
  loadTasks,
  loadWorkflow,
  loadWorkflowsById,
  WORKFLOW_IDS_PER_READ,
  TASK_PAGE_LIMIT,
  type WorkflowRead,
} from './api'
import { agentKind, agentName, shortTaskId } from './agentlist'
import { HELP } from './help'
import { eventKind } from './events'
import type { ApiError, Result } from './fetch'
import {
  ALL_STATES,
  LANE_GROUPS,
  LANE_KINDS,
  LANE_SPANS,
  MERGE_TRACK_PX,
  PILL_MIN_PX,
  attemptWord,
  axisTicks,
  axisTickText,
  buildLane,
  filterLanes,
  groupLanes,
  lanePills,
  lanesWindow,
  madeOnOutcomes,
  outcomesQueryFor,
  parseLanesView,
  pillName,
  serializeLanesView,
  spanDays,
  zoomedTo,
  type Lane,
  type LaneBlock,
  type LaneGroup,
  type LaneMark,
  type LaneSeg,
  type LanesView,
  type Pill,
} from './lanes'
import { STATE_MARK, type MarkName } from './marks'
import { Button, ButtonLink, Chip, MarkIcon, Segmented, StateMark } from './components'
import { DEFAULT_VIEW, alignedWords, ledgerRange, outcomesQuery, type Outcomes } from './outcomes'
import { formatClock, formatDateTime } from './timezone'
import { useTimeZone } from './useTimeZone'
import { CountNote, PageHead, RefreshControl, useClaimPageAge, useIdleStop, usePoll } from './Shell'
import type { AttemptRow, Task, TaskEvent, TaskState } from './types'
import { formatDuration, TERMINAL_STATES } from './types'
import { useInView } from './useInView'
import { useEscapeLayer } from './escape'
import { VALUE_LABEL_GAP_PX } from './charts/parts'
import { axisZone } from './charts/series'
import { SKIPPED_WORD } from './dag'
import { gateVerdictOf, skippedByVerdict } from './wfreview'
import { workflowLabel } from './stepviews'
import { eventWord } from './words'
import { AGE_TICK_MS, useNow } from './useNow'
import './styles/timeline.css'

/**
 * HOW MANY TASKS NOT ON THE TASK PAGE ARE READ ONE BY ONE. A 90-day span can
 * name hundreds; past this the lane is labelled by its id and says why, rather
 * than the page sending a read per lane before it draws anything.
 */
export const MISSING_TASK_READS = 40

/**
 * HOW MANY ATTEMPT PAGES ONE READ FOLLOWS ON ITS OWN (live QA RT-1,
 * 2026-10-11): ten pages of ATTEMPT_PAGE_LIMIT (200) is 2,000 attempts, which
 * on the busiest tenant measured (about 40 an hour) is two days of a span. A
 * wider span stops here rather than sending a read per 200 attempts for 90
 * days on every minute's re-read; the head then names the reach drawn, not the
 * span, and "Read the next page" goes on by hand.
 */
export const SPAN_PAGES_READ = 10

/**
 * HOW MANY TASK PAGES THE SPAN'S TASK READ FOLLOWS (live QA RT-1, review of
 * 2026-10-11). One page of the 200 newest tasks covered about five hours, so
 * on a 7d to 90d span every lane older than that drew as an anonymous bar --
 * no name, state, profile or workflow -- and any filter dropped it. The task
 * read now pages back, in the summary view, until it reaches the span's
 * start. A tenant also creates tasks that never run, so it is allowed twice
 * the attempts read's pages to cover the same reach; past that a lane is read
 * on its own, up to MISSING_TASK_READS.
 */
export const TASK_PAGES_READ = 2 * SPAN_PAGES_READ

/**
 * Whether the task read has gone far enough back to stop after this page.
 *
 * Newest first, so it has when the page's oldest task was created before the
 * span's start. On a re-read of the same span (`prior`, the tasks already
 * read), a task that has ended cannot change, so the read stops once it has
 * passed the oldest task still able to: the minute's re-read is then one
 * page, not the whole span again. An empty page is the end of the list.
 */
export function taskReadCovers(page: readonly Task[], since: number, prior: ReadonlyMap<string, Task> | null): boolean {
  let oldest = Infinity
  for (const t of page) {
    const c = Date.parse(t.created_at)
    if (Number.isFinite(c) && c < oldest) oldest = c
  }
  if (!Number.isFinite(oldest)) return true
  if (oldest < since) return true
  if (prior === null) return false
  let oldestLive = Infinity
  for (const t of prior.values()) {
    if (TERMINAL_STATES.has(t.state)) continue
    const c = Date.parse(t.created_at)
    if (Number.isFinite(c) && c < oldestLive) oldestLive = c
  }
  return oldest <= oldestLive
}

/** Past this many days the bars are slivers, and the page suggests Outcomes. */
const SUGGEST_OUTCOMES_DAYS = 7

/**
 * The slivers note names the span on screen, "Over 30 days ...", not the
 * threshold, which read as "the past 7 days" on a 30-day page (QA1
 * runs-submit-34). A zoomed window is a fraction of a day long, so it is
 * floored: "Over 7 days" is still true of 7.4.
 */
export function sliversNote(days: number): string {
  return `Over ${Math.floor(days)} days the bars are slivers: counts over the span read better as a ledger.`
}

/** A drag shorter than this on the axis is a click, not a zoom. */
const DRAG_MIN_PX = 4

// ---------------------------------------------------------------------------
// The two pages
// ---------------------------------------------------------------------------

/**
 * Lanes | Outcomes: the Timeline's two pages, one strip on each. `otherTitle`
 * is the other page's link title: where the two read the same span name over
 * different ranges, both ranges (QA G3-08).
 */
export function TimelinePages({ at, otherTitle }: { at: 'lanes' | 'outcomes'; otherTitle?: string | undefined }) {
  return (
    <nav className="tl-pages" aria-label="Timeline pages">
      <a href="/timeline" aria-current={at === 'lanes' ? 'page' : undefined} title={at === 'outcomes' ? otherTitle : undefined}>
        Lanes
      </a>
      <a href="/timeline/outcomes" aria-current={at === 'outcomes' ? 'page' : undefined} title={at === 'lanes' ? otherTitle : undefined}>
        Outcomes
      </a>
    </nav>
  )
}

// ---------------------------------------------------------------------------
// Words
// ---------------------------------------------------------------------------

// On the person's clock, the axis's (VQ-023): the window header, a mark's
// time and the axis must not read two zones on one screen. The zone is said
// once, under the axis's lane count and in the footnote.
const dayOf = (ms: number) => formatDateTime(ms).split(' ').slice(0, 2).join(' ')

function clock(ms: number, wide = false): string {
  const hm = formatClock(ms).slice(0, 5)
  return wide ? `${dayOf(ms)} ${hm}` : hm
}

/**
 * The axis's labels, placed (browser QA D4/D5, 2026-10-04).
 *
 * A DAY TICK SAYS ITS DATE. The label used to be `clock(t, wide && hour === 0
 * ? false : wide)`, which dropped the date exactly on the midnight ticks -- and
 * past 36 hours every tick is a midnight, so 7d to 90d read "00:00" across the
 * whole axis. Ticks a day or more apart are dates ("Oct 2"); inside a day a
 * tick is its hour, and the one on midnight is the date it starts.
 *
 * A TICK NEVER SITS UNDER "now". The now label is drawn at the right edge
 * whenever the window ends at the present; a tick in the last `NOW_GAP_PCT` of
 * the track overprinted it ("1now0" at 24h), so it is not drawn.
 *
 * AND THE GAP IS PIXELS ONCE THE TRACK IS MEASURED (QA G3-15, 2026-10-07): a
 * percentage of a 358 px phone track is 21 px, so `18:00` at 21:07 still ran
 * into `now`. With the track's width known, a label is kept only when it ends
 * `VALUE_LABEL_GAP_PX` (the ledger's rule, 14 px) before `now` and before the
 * next label. Widths are estimated from the mono micro face: 12 px at about
 * 0.6 em a character, rounded up as `AttemptPhases`'s axis does.
 */
const NOW_GAP_PCT = 6
/** One character of the tick face (`var(--t-micro)` mono), rounded up. */
export const TICK_CHAR_PX = 7.5
/** A tick label's left padding (`.tl-tk`). */
export const TICK_PAD_PX = 5
/** The `now` label: three characters, its padding and its 2 px rule (`.tl-tk.is-now`). */
export const NOW_LABEL_PX = 3 * TICK_CHAR_PX + 5 + 2

export function axisLabels(
  since: number,
  until: number,
  nowAt: number,
  trackPx: number | null = null,
): { t: number; pct: number; label: string }[] {
  // On the person's clock, ticks and words alike (`lanes.ts`, VQ-023).
  const ticks = axisTicks(since, until)
  const nowShown = until >= nowAt - 60_000
  const placed = ticks
    .map((t) => ({ t, pct: pct(t, since, until), label: axisTickText(t, ticks) }))
    .filter((x) => !(nowShown && x.pct > 100 - NOW_GAP_PCT))
  if (trackPx === null || !(trackPx > 0)) return placed
  const limit = nowShown ? trackPx - NOW_LABEL_PX - VALUE_LABEL_GAP_PX : trackPx
  const kept: typeof placed = []
  let lastEnd = -Infinity
  for (const x of placed) {
    const from = (x.pct / 100) * trackPx
    const to = from + TICK_PAD_PX + x.label.length * TICK_CHAR_PX
    if (to > limit || from - lastEnd < VALUE_LABEL_GAP_PX) continue
    kept.push(x)
    lastEnd = to
  }
  return kept
}

/** A lane mark's disc (`.tl-mkr`: 18 px). */
export const MARK_PX = 18
/** The clear pixels between two marks pushed together, so they read as two. */
export const MARK_GAP_PX = 2
/**
 * Where a mark's disc starts against its instant: an end mark sits 2 px after
 * it (`.tl-mkr.is-end`, Q6), a mid-lane mark is centred on it (`.tl-mkr`).
 */
const MARK_FROM_PX = { end: 2, mid: -MARK_PX / 2 } as const
/**
 * THE CLEAR PIXELS BETWEEN 'now' AND THE CHART'S EDGE that a bar or an end
 * mark keeps (visual QA r3:work-a-005, seen again in run 5b, 2026-10-22). A
 * bar running to now was laid out flush with the track's right edge, against
 * the chart's border, and read as cut off there; a073aff5's merged pill read
 * as a half-clipped '3'. Four pixels show the bar's own end inside the chart.
 */
export const TRACK_END_PX = 4

/**
 * WHERE A LANE'S MARKS ARE DRAWN, as percentages of its track (VQA V039).
 *
 * The track clips (`overflow: hidden`) and an end mark starts 2 px AFTER its
 * instant, so a task that ended at `now` drew its mark wholly past the
 * track's right edge, and marks a minute apart near `now` drew one disc over
 * the other. With the track measured, the marks are laid out from the right:
 * the last disc ends at the track's edge or before, and each earlier one ends
 * `MARK_GAP_PX` before the next starts. A mark moves only when it has to, and
 * only left -- its title still gives the instant. Unmeasured, the instant.
 * The marks of a lane with no bars: a lane's row lays out both (`laneLayout`).
 */
export function markLefts(marks: readonly { pct: number; end: boolean }[], trackPx: number | null): number[] {
  return laneLayout([], marks, trackPx).marks
}

/**
 * A held, fenced or parked bar's painted floor (`.tl-seg.is-hold` and its
 * siblings' `min-width`). The CSS floor was 8 px, but the bar's 5 px padding
 * and 1.5 px borders painted it 12-13 px whatever its width said, so a layout
 * that took 8 px as the floor still drew bars past the track's edge.
 */
export const BAR_MIN_PX = PILL_MIN_PX
/**
 * A merged pill's floor (`.tl-seg.is-merged`'s `min-width`; c00): at a lone
 * pill's 14 px the padding and borders left 1 px for its count.
 */
export const PILL_COUNT_MIN_PX = 24
/**
 * The narrowest bar that prints its words (`gen 2`, `parked`). Below it the
 * 5 px padding left a 1-4 px window, and the clipped first letter read as a
 * glyph on every floor-width bar; the bar's title still says them (V039).
 */
export const BAR_WORDS_MIN_PX = 36

/**
 * CROWDED ATTEMPTS AT THE COUNTED PILL'S OWN WIDTH (owner pick c00; visual QA
 * run 6, 6:work-a-004, box 6314122402).
 *
 * `lanePills` merges an attempt that starts inside the pill before it at a
 * LONE pill's floor, but a merged pill paints at `PILL_COUNT_MIN_PX`, wider:
 * two attempts five minutes apart became a counted pill whose floor reached
 * past a third attempt an hour later, and `laneLayout` then pushed the two
 * pills left against each other, a pile at 'now'. Here, on the same fixed
 * `MERGE_TRACK_PX` scale (so 1440 and 390 merge alike), an attempt pill joins
 * the one before it when it starts inside that pill's painted floor, and,
 * from the right, when the room left before 'now' -- `TRACK_END_PX`, and the
 * end mark's when the lane has one -- would push it into the pill before it.
 * A finished park wholly under a merged pill is under it, as `lanePills`
 * draws it; the end marks `lanePills` placed stay after the last pill, whose
 * end this never moves.
 */
export function crowdPills(pills: readonly Pill[], since: number, until: number, endMark: boolean): Pill[] {
  const unit = (until - since) / MERGE_TRACK_PX
  const isAttempt = (p: Pill) => p.segs.every((s) => s.kind === 'hold' || s.kind === 'cut')
  const floor = (p: Pill) => (p.segs.length > 1 ? PILL_COUNT_MIN_PX : PILL_MIN_PX) * unit
  const start = (p: Pill) => Math.max(p.from, since)
  const reach = (p: Pill) => Math.max(p.to, start(p) + floor(p)) + MARK_GAP_PX * unit
  const join = (a: Pill, b: Pill): Pill => ({ segs: [...a.segs, ...b.segs], from: Math.min(a.from, b.from), to: Math.max(a.to, b.to) })
  const groups: Pill[] = []
  for (const p of pills.filter(isAttempt)) {
    const last = groups[groups.length - 1]
    if (last !== undefined && start(p) < reach(last)) groups[groups.length - 1] = join(last, p)
    else groups.push({ ...p, segs: [...p.segs] })
  }
  // From 'now' leftward: the room each pill has once the ones after it are placed.
  let limit = until - (TRACK_END_PX + (endMark ? MARK_FROM_PX.end + MARK_PX : 0)) * unit
  for (let i = groups.length - 1; i >= 0; i--) {
    let g = groups[i]!
    let at = Math.min(start(g), limit - floor(g))
    while (i > 0 && reach(groups[i - 1]!) > at) {
      g = join(groups[i - 1]!, g)
      groups.splice(i - 1, 2, g)
      i--
      at = Math.min(start(g), limit - floor(g))
    }
    limit = at - MARK_GAP_PX * unit
  }
  const merged = groups.filter((g) => g.segs.length > 1)
  const parks = pills.filter((p) => !isAttempt(p) && (p.segs[0]!.future || !merged.some((g) => p.from >= g.from && p.to <= g.to)))
  return [...groups, ...parks].sort((a, b) => a.from - b.from)
}

/**
 * WHERE A LANE'S BARS AND MARKS ARE DRAWN, together (VQA2 V039, 2026-10-16).
 *
 * `markLefts` kept the marks inside the track and apart, but the bars were
 * placed at their instants with a pixel floor: three short attempts in the
 * last half hour of a 24 h span painted three 12 px bars over each other
 * and past the track's right edge, under the end mark. Measured, every item
 * is laid out from the right, in one pass, by where it starts:
 *
 * - a bar starts at its instant and is at least `BAR_MIN_PX` wide (or the
 *   `min` it is given: a merged pill's `PILL_COUNT_MIN_PX`), as the CSS
 *   floor drew it, but ends before the next item's start less a gap; a
 *   bar that then has no room is moved left, a long one is trimmed at its
 *   end, so its start stays its instant;
 * - an end mark ends before the next bar or mark;
 * - a mid-lane mark (a cancel request, drawn over its bar) keeps clear of
 *   the other marks only, and moves no bar.
 *
 * Nothing moves right, and only as far left as it has to; the titles still
 * give the instants. The right-most item ends `TRACK_END_PX` short of the
 * track's edge (r3:work-a-005), so a bar running to now shows its end. Unmeasured, the instants and the bars' old floor.
 */
export function laneLayout(
  bars: readonly { from: number; to: number; min?: number }[],
  marks: readonly { pct: number; end: boolean }[],
  trackPx: number | null,
): { bars: { left: number; width: number }[]; marks: number[] } {
  // Unmeasured, a bar keeps its 0.4 % floor inside the track: started in the
  // last 0.4 % before 'now', it ran past the now line until the axis measured
  // the track (6:work-a-004), so its floor is taken leftward from the edge.
  const plain = {
    bars: bars.map((b) => {
      const width = Math.max(b.to - b.from, 0.4)
      return { left: Math.max(0, Math.min(b.from, 100 - width)), width }
    }),
    marks: marks.map((m) => m.pct),
  }
  if (trackPx === null || !(trackPx > 0)) return plain
  const px = (p: number) => (p / 100) * trackPx
  const back = (x: number) => (x / trackPx) * 100
  type Item = { kind: 'bar' | 'end' | 'mid'; i: number; start: number }
  const items: Item[] = [
    ...bars.map((b, i) => ({ kind: 'bar' as const, i, start: px(b.from) })),
    ...marks.map((m, i) => ({
      kind: m.end ? ('end' as const) : ('mid' as const),
      i,
      start: px(m.pct) + (m.end ? MARK_FROM_PX.end : MARK_FROM_PX.mid),
    })),
  ]
  // Right to left; at one start a mark is placed before the bar under it.
  const rank = { mid: 0, end: 1, bar: 2 } as const
  items.sort((a, b) => b.start - a.start || rank[a.kind] - rank[b.kind] || b.i - a.i)
  // The next start to the right, of a bar, an end mark and a mid mark: the
  // first is the track's edge less `TRACK_END_PX`, so nothing touches it.
  const end = trackPx - TRACK_END_PX + MARK_GAP_PX
  const edge = { bar: end, end, mid: end }
  const out = { bars: plain.bars.slice(), marks: plain.marks.slice() }
  for (const it of items) {
    if (it.kind === 'bar') {
      const b = bars[it.i]!
      const floor = b.min ?? BAR_MIN_PX
      const natural = Math.max(px(b.to), px(b.from) + floor)
      const right = Math.min(natural, edge.bar - MARK_GAP_PX, edge.end - MARK_GAP_PX)
      const left = Math.max(0, Math.min(px(b.from), right - floor))
      const width = Math.max(right - left, Math.min(floor, trackPx))
      out.bars[it.i] = { left: back(left), width: back(width) }
      edge.bar = Math.min(edge.bar, left)
      continue
    }
    const from = it.kind === 'end' ? MARK_FROM_PX.end : MARK_FROM_PX.mid
    const limit = it.kind === 'end' ? Math.min(edge.bar, edge.end, edge.mid) : Math.min(edge.end, edge.mid)
    const start = Math.max(0, Math.min(it.start, limit - MARK_GAP_PX - MARK_PX))
    out.marks[it.i] = back(start - from)
    edge[it.kind] = Math.min(edge[it.kind], start)
  }
  return out
}

/**
 * WHERE `+` AND `−` ZOOM (QA G3-30, 2026-10-07). About the window's middle,
 * `+` on a 24 h span at 21:07 drew 03:07–15:07 and dropped the last six
 * hours, running work included. A window that ends at the present keeps
 * ending there; one in the past zooms about its middle, never past now.
 * `endsNow` is said by the caller for a span, which ends at its read, however
 * long ago that read was.
 */
export function zoomWindow(
  win: { since: number; until: number },
  factor: number,
  now: number,
  endsNow = win.until >= now - 60_000,
): { since: number; until: number } {
  const width = (win.until - win.since) * factor
  if (endsNow) return { since: Math.max(0, now - width), until: now }
  const mid = (win.since + win.until) / 2
  const until = Math.min(mid + width / 2, now)
  return { since: Math.max(0, until - width), until }
}

/**
 * THE LANES' GRIDLINES, FROM THE AXIS'S OWN TICKS (browser QA N16,
 * 2026-10-04). They were a CSS gradient repeated every sixth of the track --
 * a scale of their own -- so at 1440 the lines fell at 730/865/1000 while the
 * ticks sat at 605/808/1012. One 1px line at each tick's percentage, as one
 * hard-stop gradient the chart sets once (`--tl-grid`) and every lane track
 * paints; percentages of the track, so they hold at any width.
 */
export function gridlineImage(ticks: readonly { pct: number }[]): string {
  if (ticks.length === 0) return 'none'
  const stops = ticks.flatMap((t) => {
    const p = Number(t.pct.toFixed(4))
    return [`transparent ${p}%`, `var(--line-soft) ${p}%`, `var(--line-soft) calc(${p}% + 1px)`, `transparent calc(${p}% + 1px)`]
  })
  return `linear-gradient(to right, ${stops.join(', ')})`
}

/**
 * The axis's one-line count, by the grouping in use (browser QA D9): grouped
 * by profile the page said "0 workflows · 0 standalone" over 25 lanes, because
 * it counted only workflow and standalone blocks.
 */
export function laneSummary(blocks: readonly LaneBlock[], by: LaneGroup): string {
  const lanes = blocks.reduce((n, b) => n + b.lanes.length, 0)
  const lanesWord = `${lanes} ${lanes === 1 ? 'lane' : 'lanes'}`
  if (by === 'workflow') {
    const wf = blocks.filter((b) => b.kind === 'workflow').length
    const solo = blocks.find((b) => b.kind === 'standalone')?.lanes.length ?? 0
    return `${wf} ${wf === 1 ? 'workflow' : 'workflows'} · ${solo} standalone`
  }
  if (by === 'profile') {
    const p = blocks.filter((b) => b.kind === 'profile').length
    return `${lanesWord} in ${p} ${p === 1 ? 'profile' : 'profiles'}`
  }
  return lanesWord
}

/**
 * The window moved by half its width, earlier (-1) or later (+1), keeping its
 * width; a later move stops at `now`.
 */
export function panWindow(win: { since: number; until: number }, dir: -1 | 1, now: number): { since: number; until: number } {
  const width = win.until - win.since
  const until = Math.min(win.until + (dir * width) / 2, now)
  return { since: Math.max(0, until - width), until }
}

function rangeWords(since: number, until: number): string {
  const wide = until - since > 36 * 3_600_000 || dayOf(since) !== dayOf(until)
  return `${clock(since, wide)}–${clock(until, wide)}`
}

function failureWords(e: ApiError): string {
  return e.httpStatus === null ? `did not answer (${e.message})` : `answered ${e.httpStatus}`
}

const MARK_OF: Record<LaneMark['kind'], MarkName | 'warn'> = {
  cancel_requested: 'warn',
  succeeded: 'succeeded',
  failed: 'failed',
  dead_lettered: 'dead',
  cancelled: 'cancelled',
}
const MARK_HUE: Record<LaneMark['kind'], string> = {
  cancel_requested: 'warn',
  succeeded: 'neu',
  failed: 'bad',
  dead_lettered: 'bad',
  cancelled: 'neu',
}
const MARK_WORD: Record<LaneMark['kind'], string> = {
  cancel_requested: 'cancel asked',
  succeeded: 'succeeded',
  failed: 'failed',
  dead_lettered: 'dead-lettered',
  cancelled: 'cancelled',
}

// ---------------------------------------------------------------------------
// Read state
// ---------------------------------------------------------------------------

type AttemptsRead =
  | { key: string; status: 'loading' }
  | { key: string; status: 'error'; error: ApiError; at: number }
  | { key: string; status: 'done'; rows: AttemptRow[]; next: string | null; pages: number; fetchedAt: number; more: 'idle' | 'reading' | ApiError }

type EventsRead =
  | { status: 'reading' }
  | { status: 'error'; httpStatus: number | null; message: string }
  | { status: 'ok'; events: TaskEvent[]; next: string | null; older: 'idle' | 'reading' | string }

type WorkflowState =
  | { status: 'ok'; read: WorkflowRead }
  | { status: 'error'; why: string; route: string; limited: boolean }

/**
 * HOW MANY WORKFLOW READS ARE IN FLIGHT AT ONCE, and how long a 429 waits
 * before it is asked again (QA G3-07, 2026-10-07). The page used to ask for
 * every workflow its lanes name at once -- 49 of them on a busy day -- and 15
 * came back 429, each printed as `steps — (/v1/workflows/wf_… answered 429)`.
 * Four at a time is what the route answered without refusing; a refused read
 * keeps its slot while it waits, so the whole queue slows down with it rather
 * than the other three asking straight back into the limit.
 */
export const WORKFLOW_READS_AT_ONCE = 4
export const WORKFLOW_429_WAITS_MS: readonly number[] = [1_000, 3_000, 9_000]

/** A queue that runs at most `limit` reads at once, in the order they were asked. */
export function readQueue(limit: number): <T>(read: () => Promise<T>) => Promise<T> {
  let active = 0
  const waiting: Array<() => void> = []
  const next = () => {
    if (active >= limit) return
    const go = waiting.shift()
    if (go === undefined) return
    active += 1
    go()
  }
  return <T,>(read: () => Promise<T>) =>
    new Promise<T>((resolve, reject) => {
      waiting.push(() => {
        read()
          .then(resolve, reject)
          .finally(() => {
            active -= 1
            next()
          })
      })
      next()
    })
}

/** A read asked again after each wait in `waits` while the route answers 429; any other answer is returned as it came. */
export async function withBackoff<T>(
  read: () => Promise<Result<T>>,
  waits: readonly number[],
  sleep: (ms: number) => Promise<void> = (ms) => new Promise((r) => setTimeout(r, ms)),
): Promise<Result<T>> {
  for (let i = 0; ; i += 1) {
    const r = await read()
    if (r.status !== 'error' || r.error.httpStatus !== 429 || i >= waits.length) return r
    await sleep(waits[i]!)
  }
}

/**
 * How long the outcome strip waits for /v1/outcomes before it says so. The
 * ledger over a day is the heaviest read on the page; past this the strip
 * prints "no answer" rather than "Reading…" beside lanes already drawn, and
 * a late answer still replaces it.
 */
export const STRIP_DEADLINE_MS = 20_000

export type StripRead = { status: 'loading' } | { status: 'ok'; data: Outcomes } | { status: 'error'; error: ApiError }

const isData = <T,>(r: Result<T>): r is Extract<Result<T>, { data: T }> => r.status === 'ok' || r.status === 'stale'

// ---------------------------------------------------------------------------
// The screen
// ---------------------------------------------------------------------------

/**
 * How often both Timeline pages (Lanes and Outcomes) re-read while open:
 * every 60 seconds (#117, owner ruling 2026-10-07). Slower than the capacity
 * screens' 30 because a timeline is read for its shape over hours, and one
 * minute is a sliver of the narrowest span it offers. It polls through
 * `usePoll` (Shell.tsx): nothing while the tab is hidden, and nothing after
 * `IDLE_STOP_MS` without input, behind the head's `Paused · resume`.
 */
export const TIMELINE_POLL_MS = 60_000

export function TimelineLanesScreen({
  view: hashView = null,
  onView,
}: {
  /** The address's query, when it carries one. */
  view?: string | null
  /** Write a new view to the address. */
  onView?: (query: string) => void
} = {}) {
  const [own, setOwn] = useState<string>(hashView ?? '')
  const query = onView === undefined ? own : (hashView ?? '')
  const view = useMemo(() => parseLanesView(query), [query])
  const setView = useCallback(
    (next: LanesView) => {
      const q = serializeLanesView(next)
      setOwn(q)
      onView?.(q)
    },
    [onView],
  )
  const now = useNow(AGE_TICK_MS)

  // THE WINDOW IS ANCHORED when it is read: a span ends at the moment of the
  // read (or the refresh), not at every tick, so the bars and the axis agree.
  const [nonce, setNonce] = useState(0)
  const spanKey = view.since !== null ? `${view.since}|${view.until}` : `span:${view.span}`
  const [anchor, setAnchor] = useState(() => Date.now())
  /** The lane track's width, measured on the axis; null until it is (V039). */
  const [trackPx, setTrackPx] = useState<number | null>(null)
  /** The phone's filter sheet, behind `Filters · N` (visual QA VQ-024). */
  const [sheet, setSheet] = useState(false)
  /** The phone's legend, behind `Key` (VQ-024, decided 2026-10-26). */
  const [key, setKey] = useState(false)
  // ESCAPE FOLDS THEM (visual QA pass 1, runs-submit-36): open, the sheet and
  // the key stayed open through Escape. It folds the one open and hands focus
  // back to its toggle, as a picker's Escape does.
  const sheetBtn = useRef<HTMLButtonElement | null>(null)
  const keyBtn = useRef<HTMLButtonElement | null>(null)
  useEscapeLayer(sheet, () => {
    setSheet(false)
    sheetBtn.current?.focus()
  })
  useEscapeLayer(key, () => {
    setKey(false)
    keyBtn.current?.focus()
  })
  const lastSpan = useRef(spanKey)
  if (lastSpan.current !== spanKey) {
    lastSpan.current = spanKey
    setAnchor(Date.now())
  }
  const win = useMemo(() => lanesWindow(view, anchor), [view, anchor])
  const sinceIso = new Date(win.since).toISOString()
  const untilIso = new Date(win.until).toISOString()
  const readKey = `${sinceIso}|${untilIso}|${nonce}`
  const refresh = () => {
    setAnchor(Date.now())
    setNonce((n) => n + 1)
  }
  // THE CADENCE (#117) and THE ONE AGE (#98): a re-read every
  // `TIMELINE_POLL_MS` re-anchors the span at the read, as a refresh does,
  // and the head's refresh control carries the age, so the frame's head
  // prints none.
  const { idle, resume } = useIdleStop(true)
  usePoll(TIMELINE_POLL_MS, refresh, idle)
  useClaimPageAge(true)

  // ---- the attempts ------------------------------------------------------
  const [att, setAtt] = useState<AttemptsRead>({ key: readKey, status: 'loading' })
  /**
   * The last drawing and the span it was read for, kept dimmed while the same
   * span is read again (a refresh, the minute's re-read).
   */
  const [drawn, setDrawnRead] = useState<{ span: string; read: Extract<AttemptsRead, { status: 'done' }> } | null>(null)
  const setDrawn = (read: Extract<AttemptsRead, { status: 'done' }>) => setDrawnRead({ span: spanKey, read })
  /** The span of the last drawing whose page chain finished. */
  const drawnSpan = useRef<string | null>(null)
  useEffect(() => {
    let live = true
    setAtt({ key: readKey, status: 'loading' })
    // A RE-READ OF THE SAME SPAN KEEPS THE LAST DRAWING UNTIL ITS CHAIN ENDS
    // (review of RT-1): drawing each page as it landed shrank a wide span to
    // its newest few hours and regrew it over up to ten reads, every minute,
    // and an older lane open in the inspector vanished meanwhile.
    const holding = drawnSpan.current === spanKey
    // THE LANES COVER THE SPAN PICKED (live QA RT-1, 2026-10-11): one page of
    // 200 attempts, newest first, covered about the last five hours, so 7d to
    // 90d all drew the same lanes in the right-hand sliver of their axis. The
    // read now follows the page token until the span is read or
    // SPAN_PAGES_READ pages are in; past that the head says how far back the
    // drawing reaches, and "Read the next page" goes further.
    const follow = (rows: AttemptRow[], pageToken: string | null, pages: number): void => {
      loadAttemptsPage({ since: sinceIso, until: untilIso, pageToken }).then((r) => {
        if (!live) return
        if (r.status === 'loading') return
        if (r.status === 'error') {
          if (pages === 0) setAtt({ key: readKey, status: 'error', error: r.error, at: Date.now() })
          else {
            // What was read is drawn, with the failure on "Read the next page".
            const partial = { key: readKey, status: 'done' as const, rows, next: pageToken, pages, fetchedAt: Date.now(), more: r.error }
            drawnSpan.current = spanKey
            setAtt(partial)
            setDrawn(partial)
          }
          return
        }
        const all = r.status === 'empty' ? rows : [...rows, ...r.data.attempts]
        const next = r.status === 'empty' ? null : r.data.next_page_token
        const goOn = next !== null && pages + 1 < SPAN_PAGES_READ
        const done = {
          key: readKey,
          status: 'done' as const,
          rows: all,
          next,
          pages: pages + 1,
          fetchedAt: r.fetchedAt,
          more: goOn ? ('reading' as const) : ('idle' as const),
        }
        if (!goOn) drawnSpan.current = spanKey
        if (!(holding && goOn)) {
          setAtt(done)
          setDrawn(done)
        }
        if (goOn) follow(all, next, pages + 1)
      })
    }
    follow([], null, 0)
    return () => {
      live = false
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [readKey])

  const readNextPage = () => {
    if (att.status !== 'done' || att.next === null || att.more === 'reading') return
    const base = att
    setAtt({ ...base, more: 'reading' })
    loadAttemptsPage({ since: sinceIso, until: untilIso, pageToken: base.next }).then((r) => {
      setAtt((cur) => {
        if (cur.key !== base.key || cur.status !== 'done') return cur
        if (r.status === 'error') return { ...cur, more: r.error }
        if (r.status === 'loading') return cur
        const rows = r.status === 'empty' ? [] : r.data.attempts
        const next = { ...cur, rows: [...cur.rows, ...rows], next: r.status === 'empty' ? null : r.data.next_page_token, pages: cur.pages + 1, more: 'idle' as const }
        setDrawn(next)
        return next
      })
    })
  }

  // ANOTHER SPAN'S DRAWING IS NOT SHOWN UNDER THIS ONE'S AXIS (visual QA
  // pass 2, runs2-21): switched from 14d to 24h, the 14d lanes stayed for
  // seconds under the new dates, the head said '25 lanes' and the groups
  // '5 of 6 steps have attempts' for a span that had one. The kept drawing
  // belongs to its span: a re-read of the same span keeps it dimmed, a new
  // span is 'reading…' until its own read lands, as on first load.
  const kept = drawn !== null && drawn.span === spanKey ? drawn.read : null
  const shown = att.status === 'done' ? att : kept
  const rereading = att.status === 'loading' && kept !== null
  const rows = useMemo(() => shown?.rows ?? [], [shown])
  /**
   * How far back the drawing reaches when the read stopped short of the span:
   * the oldest attempt read, or null when the whole span is read.
   */
  const reach = useMemo(() => drawnReach(shown), [shown])

  // ---- the tasks behind the lanes ----------------------------------------
  const [taskPage, setTaskPage] = useState<Map<string, Task> | 'error' | null>(null)
  /** The task read has stopped paging: what it did not reach is read one by one. */
  const [tasksDone, setTasksDone] = useState(false)
  /** The last finished task read, and the span it was for. */
  const taskPrior = useRef<{ spanKey: string; tasks: Map<string, Task> } | null>(null)
  useEffect(() => {
    let live = true
    const since = win.since
    const prior = taskPrior.current !== null && taskPrior.current.spanKey === spanKey ? taskPrior.current.tasks : null
    if (prior === null) setTaskPage(null)
    setTasksDone(false)
    const finish = (m: Map<string, Task>) => {
      taskPrior.current = { spanKey, tasks: m }
      setTaskPage(m)
      setTasksDone(true)
    }
    // The summary view (live QA RT-7b): names, profiles, states and workflows are
    // all a lane reads from it; the full rows were 2.7 MB a minute. A step's
    // full row, with its verdict, comes from its workflow read. THE READ PAGES
    // BACK ACROSS THE SPAN (RT-1): see TASK_PAGES_READ and taskReadCovers.
    const follow = (m: Map<string, Task>, pageToken: string | null, pages: number): void => {
      loadTasks(TASK_PAGE_LIMIT, { view: 'summary', pageToken }).then((r) => {
        if (!live) return
        if (r.status === 'loading') return
        if (r.status === 'error') {
          if (pages === 0 && prior === null) {
            setTaskPage('error')
            setTasksDone(true)
          } else finish(m)
          return
        }
        const page = isData(r) ? r.data.tasks : []
        const merged = new Map(m)
        for (const t of page) merged.set(t.id, t)
        const next = isData(r) ? (r.data.next_page_token ?? null) : null
        if (next === null || pages + 1 >= TASK_PAGES_READ || taskReadCovers(page, since, prior)) {
          finish(merged)
          return
        }
        if (prior === null) setTaskPage(merged)
        follow(merged, next, pages + 1)
      })
    }
    follow(prior === null ? new Map() : new Map(prior), null, 0)
    return () => {
      live = false
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nonce, spanKey])

  const [single, setSingle] = useState<Map<string, Task | string>>(new Map())
  const asked = useRef(new Set<string>())
  useEffect(() => {
    asked.current = new Set()
    setSingle(new Map())
  }, [nonce])

  const [workflows, setWorkflows] = useState<Map<string, WorkflowState>>(new Map())
  const askedWf = useRef(new Set<string>())
  /** The read the workflow answers belong to: one landing after a refresh is dropped. */
  const wfGen = useRef(nonce)
  useEffect(() => {
    wfGen.current = nonce
    // A SETTLED WORKFLOW IS READ ONCE (live QA RT-7b, 2026-10-11): the
    // minute's re-read asked again for all 42 workflow documents, up to
    // 181 KB each, though a workflow whose every step has ended cannot change.
    // Those keep their read; the rest are asked again.
    setWorkflows((m) => {
      const kept = new Map<string, WorkflowState>()
      for (const [id, w] of m) if (workflowSettled(w)) kept.set(id, w)
      askedWf.current = new Set(kept.keys())
      return kept
    })
  }, [nonce])
  const wfQueue = useRef(readQueue(WORKFLOW_READS_AT_ONCE))
  /** One workflow, read on its own: the path an API without `?ids=` is read by. */
  const askOne = useCallback((wf: string, gen: number) => {
    void wfQueue.current(() => withBackoff(() => loadWorkflow(wf), WORKFLOW_429_WAITS_MS)).then((r) => {
      if (wfGen.current !== gen) return
      const route = `/v1/workflows/${wf}`
      setWorkflows((m) =>
        new Map(m).set(
          wf,
          isData(r)
            ? { status: 'ok', read: r.data }
            : r.status === 'error'
              ? { status: 'error', why: failureWords(r.error), route, limited: r.error.httpStatus === 429 }
              : { status: 'error', why: 'not read', route, limited: false },
        ),
      )
    })
  }, [])
  // THE WORKFLOWS THE LANES NAME, IN ONE READ PER 50 (live QA CH-6,
  // 2026-10-11): one `GET /v1/workflows/{id}` per workflow was 52 reads on one
  // load. The asks of one render are gathered and sent as
  // `GET /v1/workflows?ids=` in chunks of WORKFLOW_IDS_PER_READ, through the
  // same queue and the same 429 waits; an API that ignores `ids` has each
  // read on its own, as before.
  const wfPending = useRef<{ gen: number; ids: string[] } | null>(null)
  const flushWorkflows = useCallback(() => {
    const pending = wfPending.current
    wfPending.current = null
    if (pending === null) return
    const { gen, ids } = pending
    for (let at = 0; at < ids.length; at += WORKFLOW_IDS_PER_READ) {
      const chunk = ids.slice(at, at + WORKFLOW_IDS_PER_READ)
      void wfQueue.current(() => withBackoff(() => loadWorkflowsById(chunk), WORKFLOW_429_WAITS_MS)).then((r) => {
        if (wfGen.current !== gen) return
        if (isData(r) && !r.data.batched) {
          for (const wf of chunk) askOne(wf, gen)
          return
        }
        setWorkflows((m) => {
          const n = new Map(m)
          for (const wf of chunk) {
            const route = `/v1/workflows?ids=${wf}`
            const one = isData(r) ? r.data.reads.get(wf) : undefined
            n.set(
              wf,
              one !== undefined
                ? { status: 'ok', read: one }
                : isData(r)
                  ? { status: 'error', why: 'not found in this workspace', route, limited: false }
                  : r.status === 'error'
                    ? { status: 'error', why: failureWords(r.error), route, limited: r.error.httpStatus === 429 }
                    : { status: 'error', why: 'not read', route, limited: false },
            )
          }
          return n
        })
      })
    }
  }, [askOne])
  const askWorkflow = useCallback(
    (wf: string) => {
      askedWf.current.add(wf)
      const gen = wfGen.current
      if (wfPending.current !== null && wfPending.current.gen === gen) {
        wfPending.current.ids.push(wf)
        return
      }
      wfPending.current = { gen, ids: [wf] }
      queueMicrotask(flushWorkflows)
    },
    [flushWorkflows],
  )
  const retryWorkflow = useCallback(
    (wf: string) => {
      setWorkflows((m) => {
        const n = new Map(m)
        n.delete(wf)
        return n
      })
      askWorkflow(wf)
    },
    [askWorkflow],
  )

  /** Every task document read, by any of the three reads. */
  const tasks = useMemo(() => {
    const m = new Map<string, Task>()
    if (taskPage instanceof Map) for (const [k, t] of taskPage) m.set(k, t)
    for (const [k, t] of single) if (typeof t !== 'string') m.set(k, t)
    for (const w of workflows.values()) if (w.status === 'ok') for (const t of w.read.tasks) m.set(t.id, t)
    return m
  }, [taskPage, single, workflows])

  const tasksNow = useRef(tasks)
  tasksNow.current = tasks
  const laneIds = useMemo(() => Array.from(new Set(rows.map((a) => a.task_id))), [rows])

  // Tasks the task read did not reach, one by one, up to the cap.
  useEffect(() => {
    if (taskPage === null || !tasksDone) return
    const missing = laneIds.filter((id) => !tasks.has(id) && !asked.current.has(id))
    const room = MISSING_TASK_READS - asked.current.size
    for (const id of missing.slice(0, Math.max(0, room))) {
      asked.current.add(id)
      loadTask(id).then((r) => {
        setSingle((m) => new Map(m).set(id, isData(r) ? r.data : r.status === 'error' ? `${r.error.httpStatus ?? 'no answer'}` : 'not read'))
      })
    }
  }, [taskPage, tasksDone, laneIds, tasks])

  // Each workflow the lanes name, once.
  useEffect(() => {
    for (const id of laneIds) {
      const wf = tasks.get(id)?.workflow_id ?? null
      if (wf === null || askedWf.current.has(wf)) continue
      askWorkflow(wf)
    }
  }, [laneIds, tasks, askWorkflow])

  // ---- one events page per lane in view -----------------------------------
  const [events, setEvents] = useState<Map<string, EventsRead>>(new Map())
  // THE ASKED SET BELONGS TO ONE READ. A refresh keeps the drawing mounted,
  // so no row leaves the view and none would ask again on its own: each row
  // re-asks on the new `nonce`, and the first ask of a read starts its set
  // and map afresh. React runs the rows' effects before this screen's, so the
  // reset lives in ensureEvents, with the screen's effect covering a read in
  // which no row asked. A page landing after a newer read began is dropped.
  const nonceNow = useRef(nonce)
  nonceNow.current = nonce
  const askedEv = useRef({ gen: nonce, ids: new Set<string>() })
  // AN ENDED LANE'S EVENTS ARE READ ONCE (live QA RT-7b): the minute's
  // re-read asked again for every lane's events page, though a task that has
  // ended writes no more events. Those pages are kept across a refresh.
  const eventsNow = useRef(events)
  eventsNow.current = events
  const startRead = () => {
    if (askedEv.current.gen === nonceNow.current) return
    const kept = new Map<string, EventsRead>()
    for (const [id, e] of eventsNow.current) {
      const t = tasksNow.current.get(id)
      if (e.status === 'ok' && t !== undefined && TERMINAL_STATES.has(t.state)) kept.set(id, e)
    }
    askedEv.current = { gen: nonceNow.current, ids: new Set(kept.keys()) }
    setEvents(kept)
  }
  useEffect(startRead, [nonce])
  const ensureEvents = useCallback((id: string) => {
    startRead()
    if (askedEv.current.ids.has(id)) return
    askedEv.current.ids.add(id)
    const gen = askedEv.current.gen
    setEvents((m) => new Map(m).set(id, { status: 'reading' }))
    loadTaskEventsPage(id, { order: 'desc' }).then((r) => {
      if (askedEv.current.gen !== gen) return
      setEvents((m) =>
        new Map(m).set(
          id,
          isData(r)
            ? { status: 'ok', events: r.data.events, next: r.data.next_page_token ?? null, older: 'idle' }
            : r.status === 'empty'
              ? { status: 'ok', events: [], next: null, older: 'idle' }
              : r.status === 'error'
                ? { status: 'error', httpStatus: r.error.httpStatus, message: r.error.message }
                : { status: 'error', httpStatus: null, message: 'the read did not complete' },
        ),
      )
    })
  }, [])
  const retryEvents = (id: string) => {
    askedEv.current.ids.delete(id)
    ensureEvents(id)
  }
  const readOlder = (id: string) => {
    const cur = events.get(id)
    if (cur?.status !== 'ok' || cur.next === null || cur.older === 'reading') return
    const token = cur.next
    const gen = askedEv.current.gen
    setEvents((m) => new Map(m).set(id, { ...cur, older: 'reading' }))
    loadTaskEventsPage(id, { order: 'desc', pageToken: token }).then((r) => {
      if (askedEv.current.gen !== gen) return
      setEvents((m) => {
        const c = m.get(id)
        if (c?.status !== 'ok') return m
        if (isData(r)) return new Map(m).set(id, { status: 'ok', events: [...c.events, ...r.data.events], next: r.data.next_page_token ?? null, older: 'idle' })
        if (r.status === 'empty') return new Map(m).set(id, { ...c, next: null, older: 'idle' })
        return new Map(m).set(id, { ...c, older: r.status === 'error' ? `the older page ${failureWords(r.error)}` : 'the older page was not read' })
      })
    })
  }

  // ---- the lanes -----------------------------------------------------------
  const allLanes = useMemo(() => {
    const byTask = new Map<string, AttemptRow[]>()
    for (const a of rows) byTask.set(a.task_id, [...(byTask.get(a.task_id) ?? []), a])
    const out: Lane[] = []
    for (const [id, as] of byTask) {
      const e = events.get(id)
      out.push(buildLane(id, tasks.get(id) ?? null, as, e?.status === 'ok' ? e.events : null, anchor))
    }
    // Workflow steps that never had an attempt, from the workflow read.
    // ONLY OF A WORKFLOW WITH AN ATTEMPT IN THIS SPAN (visual QA pass 2,
    // runs2-22): a settled workflow's read is kept across reads, so 24h
    // reached from 14d drew 14d's failed workflow as '0 of 6 steps have
    // attempts' and a 'synthesis · never ran' lane, where 24h loaded directly
    // drew no lane. A span draws the same lanes however it is reached.
    const ranIn = new Set<string>()
    for (const id of byTask.keys()) {
      const wf = tasks.get(id)?.workflow_id ?? null
      if (wf !== null) ranIn.add(wf)
    }
    for (const [wfId, w] of workflows) {
      if (w.status !== 'ok' || !ranIn.has(wfId)) continue
      for (const t of w.read.tasks) {
        if (!byTask.has(t.id) && t.attempt_count === 0) out.push(buildLane(t.id, t, [], [], anchor))
      }
    }
    return out
  }, [rows, tasks, events, workflows, anchor])
  const lanes = useMemo(() => filterLanes(allLanes, view), [allLanes, view])
  const blocks = useMemo(() => groupLanes(lanes, view.by), [lanes, view.by])
  const neverRan = allLanes.filter((l) => l.neverRan).length

  // ---- the outcome strip -----------------------------------------------------
  // The person's zone, so the strip's buckets and the footnote agree with the axis.
  const tz = useTimeZone()
  const stripQuery = useMemo(
    () =>
      outcomesQuery(
        {
          ...DEFAULT_VIEW,
          span: view.since !== null ? null : (view.span ?? '24h'),
          since: view.since,
          until: view.until,
          profile: view.profile,
          kind: view.kind,
        },
        tz,
      ).toString(),
    [view.since, view.until, view.span, view.profile, view.kind, tz],
  )
  const [strip, setStrip] = useState<StripRead>({ status: 'loading' })
  useEffect(() => {
    let live = true
    let landed = false
    setStrip({ status: 'loading' })
    // EVERY WAY THE READ ENDS IS SAID (visual QA Q6, 2026-10-02): the strip
    // read "Reading /v1/outcomes…" beside drawn lanes and never moved, because
    // an `empty` answer was mapped back to loading and a read that never
    // answered had no end. An empty answer, a thrown read and no answer by
    // the deadline are each an honest error; a late answer still replaces it.
    const fail = (message: string, httpStatus: number | null = null) =>
      setStrip({ status: 'error', error: { kind: 'unreachable', httpStatus, code: null, message } })
    const deadline = setTimeout(() => {
      if (live && !landed) fail(`no answer after ${Math.round(STRIP_DEADLINE_MS / 1000)} s`)
    }, STRIP_DEADLINE_MS)
    ;(loadOutcomes(new URLSearchParams(stripQuery), ['totals']) as Promise<Result<Outcomes>>).then(
      (r) => {
        if (!live) return
        landed = true
        clearTimeout(deadline)
        if (isData(r)) setStrip({ status: 'ok', data: r.data })
        else if (r.status === 'error') setStrip({ status: 'error', error: r.error })
        else if (r.status === 'empty') fail('the route answered with nothing')
        else fail('the read did not finish')
      },
      (e: unknown) => {
        if (!live) return
        landed = true
        clearTimeout(deadline)
        fail(e instanceof Error ? e.message : String(e))
      },
    )
    return () => {
      live = false
      clearTimeout(deadline)
    }
  }, [stripQuery, nonce])

  // ---- what the page draws ----------------------------------------------------
  const selected = view.lane === null ? null : (allLanes.find((l) => l.taskId === view.lane) ?? null)
  const days = spanDays(view, anchor)
  const back = view.back === null ? null : parseLanesView(view.back)
  const zoomed = view.since !== null
  const select = (id: string | null) => setView({ ...view, lane: id })

  const profiles = useMemo(() => uniqSorted(allLanes.map((l) => l.task?.runner_profile ?? null)), [allLanes])
  const workflowIds = useMemo(() => uniqSorted(allLanes.map((l) => l.task?.workflow_id ?? null)), [allLanes])
  /**
   * THE NAMES THE WORKFLOWS LIST SHOWS (live QA RT-25, 2026-10-11): the
   * Workflow filter and the group heads were bare `wf_…` ids beside a list
   * that calls them PERM-2 and S9. A workflow's label is its step tasks'
   * `metadata.unit` (`workflowLabel`), from the workflow read.
   */
  const wfNames = useMemo(() => {
    const m = new Map<string, string>()
    for (const [id, w] of workflows) {
      if (w.status !== 'ok') continue
      const label = workflowLabel(w.read.workflow, new Map(w.read.tasks.map((t) => [t.id, t])))
      if (label !== null) m.set(id, label)
    }
    return m
  }, [workflows])

  /**
   * A NEW WINDOW, WITH THE WAY BACK TO THE SPAN IT STARTED FROM (live QA
   * RT-22b, 2026-10-11): each `+` and `−` made the window it left the way
   * back, so the address carried the last zoom, encoded once more, and the
   * chip went back one click instead of to the span. The first zoom's way
   * back is kept until a span is picked.
   */
  const toWindow = (since: number, until: number) => {
    const z = zoomedTo(view, since, until)
    setView(view.back !== null && view.since !== null ? { ...z, back: view.back } : z)
  }
  const zoomBy = (factor: number) => {
    const at = Date.now()
    const z = zoomWindow(win, factor, at, view.since === null || win.until >= at - 60_000)
    toWindow(z.since, z.until)
  }
  /** Half a window earlier (-1) or later (+1), never past now (RT-22c: there was no pan). */
  const panBy = (dir: -1 | 1) => {
    const p = panWindow(win, dir, Date.now())
    toWindow(p.since, p.until)
  }

  return (
    <div className="tl-page">
      {/* TITLE LEFT, ACTIONS RIGHT (#138): the refresh, with its ticking
          age and the cadence, is the head's one action; the span and the
          lane count are the note over the first card. */}
      <PageHead title="Timeline">
        <RefreshControl
          readAt={shown === null ? null : shown.fetchedAt}
          now={now}
          cadence={{ base: TIMELINE_POLL_MS, wait: TIMELINE_POLL_MS }}
          reading={att.status === 'loading'}
          idle={idle}
          onRefresh={refresh}
          onResume={() => {
            resume()
            refresh()
          }}
        />
      </PageHead>

      <TimelinePages at="lanes" otherTitle={bothRanges(strip, win.since, win.until) ?? undefined} />

      <CountNote>
        {reach === null ? rangeWords(win.since, win.until) : `newest ${shown?.rows.length ?? 0} attempts, ${rangeWords(reach, win.until)} of ${rangeWords(win.since, win.until)}`} ·{' '}
        {shown === null ? 'reading…' : `${lanes.length === allLanes.length ? '' : `${lanes.length} of `}${allLanes.length} ${allLanes.length === 1 ? 'lane' : 'lanes'}`}
      </CountNote>

      {madeOnOutcomes(query) && (
        <p className="tl-note">
          This link was made on the Outcomes page, and Lanes reads only its span.{' '}
          <a href={`/timeline/outcomes?${query}`}>Open it on Outcomes ›</a>
        </p>
      )}

      <div className="tl-ctl" role="group" aria-label="Timeline span and filters">
        <Segmented
          label="Span"
          className="tl-span"
          value={zoomed ? null : view.span}
          options={LANE_SPANS.map((s) => ({ key: s, label: s }))}
          onChange={(s) => setView({ ...view, span: s, since: null, until: null, back: null })}
        />
        <span className="tl-zoom">
          <Button size="sm" aria-label="Earlier" title="Half a window earlier" onClick={() => panBy(-1)}>
            ‹
          </Button>
          <Button size="sm" aria-label="Zoom out" onClick={() => zoomBy(2)}>
            −
          </Button>
          <Button size="sm" aria-label="Zoom in" onClick={() => zoomBy(0.5)}>
            +
          </Button>
          <Button size="sm" aria-label="Later" title="Half a window later, up to now" disabled={win.until >= anchor - 60_000} onClick={() => panBy(1)}>
            ›
          </Button>
        </span>
        {/* THE FILTERS FOLD ON A PHONE (visual QA VQ-024, 2026-10-26): the
            controls and the strip filled the first screen at 390 and the
            first lane was below the fold. Behind one `Filters · N`, as
            Outcomes folds its own; on a wide screen the sheet is
            `display: contents` and its controls sit in this row. */}
        <button
          ref={sheetBtn}
          type="button"
          className="tl-sheet-toggle"
          aria-expanded={sheet}
          aria-controls="tl-sheet"
          onClick={() => setSheet(!sheet)}
        >
          Filters · {lanesFiltersSet(view)}
        </button>
        {/* THE LEGEND FOLDS ON A PHONE TOO (VQ-024, "Fold the legend",
            BGD 2026-10-26): with the filters folded, the legend's seven marks
            still stood between the strip and the first lane at 390. Closed by
            default there; a wide screen shows it always and has no button.
            The outcome strip's counts are not folded with it. */}
        <button
          ref={keyBtn}
          type="button"
          className="tl-key-toggle"
          aria-expanded={key}
          aria-controls="tl-key"
          onClick={() => setKey(!key)}
        >
          Key
        </button>
        <div className={sheet ? 'tl-sheet is-open' : 'tl-sheet'} id="tl-sheet">
          <TlPicker
            label="State"
            options={ALL_STATES}
            selected={view.state}
            word={(s) => s.toLowerCase().replace('_', '-')}
            onChange={(next) => setView({ ...view, state: next as TaskState[] })}
          />
          {/* THE PROFILES ARE THE LANES' OWN (visual QA QA-095, run 2): until
              the attempts are read there are none to count, and `all 0` read
              as a measured zero. It says what the strip below says instead. */}
          <TlPicker
            label="Profile"
            options={profiles}
            read={shown !== null ? 'read' : att.status === 'error' ? 'failed' : 'reading'}
            selected={view.profile}
            onChange={(next) => setView({ ...view, profile: next })}
          />
          <label className={view.wf !== null ? 'tl-flt is-on' : 'tl-flt'}>
            Workflow{' '}
            <select className="tl-wf-sel" value={view.wf ?? ''} onChange={(e) => setView({ ...view, wf: e.target.value === '' ? null : e.target.value })}>
              <option value="">any</option>
              {[...workflowIds, ...(view.wf !== null && !workflowIds.includes(view.wf) ? [view.wf] : [])].map((w) => (
                <option key={w} value={w}>
                  {wfNames.has(w) ? `${wfNames.get(w)!} · ${w}` : w}
                </option>
              ))}
            </select>
          </label>
          <Segmented
            label="Kind"
            className="tl-kind"
            value={view.kind}
            options={LANE_KINDS.map((k) => ({ key: k, label: k === 'steps' ? 'workflow steps' : k }))}
            onChange={(k) => setView({ ...view, kind: k })}
          />
          <label className="tl-flt">
            Group by{' '}
            <select value={view.by} onChange={(e) => setView({ ...view, by: e.target.value as LanesView['by'] })}>
              {LANE_GROUPS.map((g) => (
                <option key={g} value={g}>
                  {g}
                </option>
              ))}
            </select>
          </label>
        </div>
      </div>
      {/* THE WAY BACK HAS ITS OWN LINE (QA G3-31, 2026-10-07): inserted
          among the controls, the chip moved every control after it, and the
          one being pressed, `+`, jumped from under the pointer. */}
      {back !== null && zoomed && (
        <p className="tl-back">
          <Chip onClick={() => setView(back)}>
            ← {back.since !== null && back.until !== null ? rangeWords(Date.parse(back.since), Date.parse(back.until)) : back.span} · zoomed to {rangeWords(win.since, win.until)}
          </Chip>
        </p>
      )}

      <OutcomeStrip
        strip={strip}
        lanes={allLanes}
        // `shown` keeps the last read's rows while a re-read is out (`drawn`),
        // so the counts stay those of the lanes still drawn -- figures for what
        // is on screen, never zeros for a read that has not landed (QA-093).
        lanesRead={shown !== null ? 'read' : att.status === 'error' ? 'failed' : 'reading'}
        eventsReading={allLanes.some((l) => !l.neverRan && events.get(l.taskId)?.status === 'reading')}
        view={view}
        since={win.since}
        until={win.until}
        reach={reach}
      />

      {days > SUGGEST_OUTCOMES_DAYS && (
        <p className="tl-note tl-suggest">
          <span>{sliversNote(days)}</span>{' '}
          <a href={`/timeline/outcomes?${outcomesQueryFor(view)}`}>Open Outcomes ›</a>
        </p>
      )}

      <Legend open={key} />

      {att.status === 'error' ? (
        <div className="tl-state is-bad" role="alert">
          <p>
            The attempts could not be read: the read {failureWords(att.error)} at {formatDateTime(att.at).split(' ')[2]}. Nothing is drawn, because nothing was read.
          </p>
          <Button size="sm" onClick={refresh}>
            Try again
          </Button>
        </div>
      ) : shown === null ? (
        <div className="tl-state" aria-busy="true">
          <p>{`Reading attempts for ${rangeWords(win.since, win.until)}…`}</p>
          <div className="tl-skel" aria-hidden="true" />
          <div className="tl-skel is-short" aria-hidden="true" />
        </div>
      ) : allLanes.length === 0 ? (
        <div className="tl-state">
          <p>{`No attempt held capacity between ${rangeWords(win.since, win.until).replace('–', ' and ')}.`}</p>
          <p>
            Waiting work costs nothing and draws no bar, so this is not &ldquo;nothing exists&rdquo;. <a href="/agents/waiting">Agents › Waiting ›</a>
          </p>
        </div>
      ) : lanes.length === 0 ? (
        <div className="tl-state">
          <p>{`No lane matches ${filterWords(view)} among the ${rows.length} attempts read.`}</p>
          <Button size="sm" onClick={() => setView({ ...view, state: [], profile: [], wf: null, kind: 'all', lane: null })}>
            Clear filters
          </Button>
        </div>
      ) : (
        <div className={rereading ? 'tl-chart is-stale' : 'tl-chart'} aria-busy={rereading}>
          <div
            className="tl"
            role="group"
            aria-label="Lanes"
            style={{ '--tl-grid': gridlineImage(axisLabels(win.since, win.until, anchor)) } as CSSProperties}
          >
            <Axis
              since={win.since}
              until={win.until}
              nowAt={anchor}
              summary={laneSummary(blocks, view.by)}
              onZoom={toWindow}
              onTrackPx={setTrackPx}
            />
            {blocks.map((b) => (
              <Fragment key={b.key}>
                <GroupHead
                  block={b}
                  since={win.since}
                  until={win.until}
                  name={b.kind === 'workflow' ? (wfNames.get(b.key) ?? lanesLabel(allLanes.filter((l) => l.task?.workflow_id === b.key))) : null}
                  workflow={b.kind === 'workflow' ? (workflows.get(b.key) ?? null) : null}
                  onRetry={() => retryWorkflow(b.key)}
                  ran={b.kind === 'workflow' ? allLanes.filter((l) => !l.neverRan && l.task?.workflow_id === b.key) : []}
                  empty={b.kind === 'workflow' ? allLanes.filter((l) => l.neverRan && l.task?.workflow_id === b.key) : []}
                />
                {b.lanes.map((l) => [
                  <LaneRow
                    key={l.taskId}
                    lane={l}
                    since={win.since}
                    until={win.until}
                    trackPx={trackPx}
                    events={events.get(l.taskId) ?? null}
                    single={single.get(l.taskId) ?? null}
                    taskPageFailed={taskPage === 'error'}
                    selected={view.lane === l.taskId}
                    onSelect={() => select(view.lane === l.taskId ? null : l.taskId)}
                    onSeen={ensureEvents}
                    readNonce={nonce}
                    onRetry={retryEvents}
                  />,
                  // UNDER THE LANE THAT WAS CLICKED (browser QA D10): drawn
                  // after the footer, the summary and "Open in Agents" landed
                  // 11,000px down a long page, out of sight of the click.
                  selected !== null && selected.taskId === l.taskId && (
                    <LaneDetail
                      key={`${l.taskId}:detail`}
                      lane={selected}
                      events={events.get(selected.taskId) ?? null}
                      onClose={() => {
                        // FOCUS GOES BACK TO THE LANE'S NAME (visual QA pass
                        // 2, runs2-26): Escape left focus in the panel being
                        // removed, and the page handed it to its h1. The
                        // name that opened the panel takes it first.
                        const id = selected.taskId
                        const row = [...document.querySelectorAll<HTMLElement>('.tl-row[data-lane]')].find((el) => el.dataset.lane === id)
                        row?.querySelector<HTMLButtonElement>('button.tl-name')?.focus()
                        select(null)
                      }}
                      onOlder={() => readOlder(selected.taskId)}
                      onRead={() => ensureEvents(selected.taskId)}
                      workflowName={selected.task?.workflow_id ? (wfNames.get(selected.task.workflow_id) ?? null) : null}
                    />
                  ),
                ])}
              </Fragment>
            ))}
          </div>
          {rereading && <p className="tl-q">Reading the span again… the lanes above are the last read.</p>}
        </div>
      )}

      {shown !== null && att.status !== 'error' && shown.next !== null && (
        <div className="tl-state is-partial" role="status" aria-label="More attempts in this span">
          <p>
            {shown.rows.length} attempts drawn, newest first. Older attempts in this span: <b className="tl-dash">—</b> the route returns a page token, not a total.
          </p>
          {typeof shown.more === 'object' && <p>{`The next page ${failureWords(shown.more)}.`}</p>}
          <Button size="sm" onClick={readNextPage} disabled={shown.more === 'reading'}>
            {shown.more === 'reading' ? 'Reading…' : 'Read the next page ›'}
          </Button>
        </div>
      )}

      {shown !== null && att.status !== 'error' && (
        <p className="tl-cov">
          <span>
            <b>{shown.rows.length}</b> attempts from {shown.pages === 1 ? 'page 1' : `pages 1–${shown.pages}`} of the attempt list (since {clock(win.since, true)}, until {clock(win.until, true)}) · {shown.next === null ? 'no next page' : 'a next page exists'}
          </span>
          <span>an attempt created before the span is not on this read</span>
          {neverRan > 0 && <span>{neverRan} workflow {neverRan === 1 ? 'step' : 'steps'} with no attempt, from the workflow reads</span>}
          <span>events: the newest page per lane in view</span>
          <span>State, profile and workflow filters run in this browser over the rows read</span>
          <span>times {tz}</span>
        </p>
      )}

      {view.lane !== null && selected === null && shown !== null && (
        <p className="tl-note">{`The lane ${view.lane} is not among the lanes read for this span.`}</p>
      )}
    </div>
  )
}

/**
 * THE REACH OF A SHORT READ (live QA RT-1): the oldest attempt's creation
 * time when a next page remains, else null. The head prints this in place of
 * the span, so "the lanes cover Jul 13–Oct 11" is never said of a read that
 * reaches back five hours.
 */
export function drawnReach(read: { rows: readonly AttemptRow[]; next: string | null } | null): number | null {
  if (read === null || read.next === null || read.rows.length === 0) return null
  let oldest = Infinity
  for (const a of read.rows) {
    const t = Date.parse(a.created_at)
    if (Number.isFinite(t) && t < oldest) oldest = t
  }
  return Number.isFinite(oldest) ? oldest : null
}

/** A workflow read whose workflow and every step have ended: nothing in it can change. */
export function workflowSettled(w: WorkflowState): boolean {
  if (w.status !== 'ok') return false
  if (!WORKFLOW_ENDED.has(w.read.workflow.state)) return false
  return w.read.tasks.every((t) => TERMINAL_STATES.has(t.state))
}
const WORKFLOW_ENDED = new Set(['SUCCEEDED', 'FAILED', 'CANCELLED'])

function uniqSorted(xs: ReadonlyArray<string | null>): string[] {
  return Array.from(new Set(xs.filter((x): x is string => x !== null))).sort()
}

/** How many of the filters a lane must match are set: the `Filters · N` count (VQ-024). */
export function lanesFiltersSet(v: LanesView): number {
  return (v.state.length > 0 ? 1 : 0) + (v.profile.length > 0 ? 1 : 0) + (v.wf !== null ? 1 : 0) + (v.kind !== 'all' ? 1 : 0)
}

function filterWords(v: LanesView): string {
  const parts: string[] = []
  if (v.state.length > 0) parts.push(`state ${v.state.map((s) => s.toLowerCase()).join(' or ')}`)
  if (v.profile.length > 0) parts.push(`profile ${v.profile.join(' or ')}`)
  if (v.wf !== null) parts.push(`workflow ${v.wf}`)
  if (v.kind !== 'all') parts.push(v.kind === 'steps' ? 'workflow steps' : 'standalone')
  return parts.length === 0 ? 'these filters' : parts.join(' and ')
}

// ---------------------------------------------------------------------------
// Local components (prefixed for U0's shared set to replace)
// ---------------------------------------------------------------------------

function TlPicker({
  label,
  options,
  selected,
  word = (s) => s,
  read = 'read',
  onChange,
}: {
  label: string
  options: readonly string[]
  /**
   * Whether `options` were read off the lanes (QA-095). A fixed list (the
   * states) is always `read`; the profiles are the lanes' own, and before the
   * attempts land `all 0` counted the profiles of no lanes.
   */
  read?: 'read' | 'reading' | 'failed'
  selected: readonly string[]
  word?: (s: string) => string
  onChange: (next: string[]) => void
}) {
  const summary =
    selected.length > 0
      ? selected.map(word).join(', ')
      : read === 'reading'
        ? 'all · reading'
        : read === 'failed'
          ? 'all · not read'
          : // NO COUNT ON 'all' (visual QA pass 1, runs-submit-37): `all 0`
            // and `all 1` counted the profiles seen in the span, and read as
            // zero or one profile. Narrowed, a filter names its picks;
            // unnarrowed it is all.
            'all'
  // ESCAPE AND A PRESS OUTSIDE CLOSE IT (QA G3-18, 2026-10-07): the checklist
  // stayed open through Escape and across a `+` zoom until its own button was
  // pressed again. Escape hands focus back to the button that opened it.
  // The element's own `open` is the state: the browser toggles it from the
  // summary, and these two only ever close it.
  const box = useRef<HTMLDetailsElement | null>(null)
  // ESCAPE FROM ANYWHERE WHILE IT IS OPEN (live QA RT-23): the keydown below
  // fires only with focus inside, and a pointer that opened it leaves focus
  // on the page in some browsers, so Escape did nothing.
  const [isOpen, setIsOpen] = useState(false)
  useEscapeLayer(isOpen, () => {
    const el = box.current
    if (el === null) return
    el.open = false
    setIsOpen(false)
    el.querySelector('summary')?.focus()
  })
  useEffect(() => {
    const away = (e: PointerEvent) => {
      const el = box.current
      if (el !== null && el.open && e.target instanceof Node && !el.contains(e.target)) el.open = false
    }
    document.addEventListener('pointerdown', away)
    return () => document.removeEventListener('pointerdown', away)
  }, [])
  return (
    <details
      ref={box}
      className={selected.length > 0 ? 'tl-flt tl-pick is-on' : 'tl-flt tl-pick'}
      onToggle={(e) => setIsOpen(e.currentTarget.open)}
      onKeyDown={(e) => {
        const el = box.current
        if (e.key !== 'Escape' || el === null || !el.open) return
        e.preventDefault()
        e.stopPropagation()
        el.open = false
        el.querySelector('summary')?.focus()
      }}
    >
      <summary>
        {label} <i>{summary}</i>
      </summary>
      {/* EVERY OPTION IN VIEW (live QA RT-23): at 320px the State list ended
          after `failed`, with `cancelled` and `dead-lettered` scrolled out of
          a panel that gave no sign it scrolls. */}
      <div className="tl-pop" role="group" aria-label={label} style={{ maxHeight: 'min(70vh, 480px)' }}>
        {options.length === 0 && (
          <span className="tl-q">
            {read === 'reading' ? 'the lanes are still being read' : read === 'failed' ? 'the lanes were not read' : 'none in the rows read'}
          </span>
        )}
        {options.map((o) => (
          <label key={o}>
            <input
              type="checkbox"
              checked={selected.includes(o)}
              onChange={(e) => onChange(e.target.checked ? [...selected, o].sort() : selected.filter((s) => s !== o))}
            />{' '}
            {word(o)}
          </label>
        ))}
      </div>
    </details>
  )
}

function Legend({ open }: { open: boolean }) {
  return (
    <p className={open ? 'tl-legend tl-key is-open' : 'tl-legend tl-key'} id="tl-key">
      <span>
        <i className="tl-lg is-hold" /> held capacity (leased → running)
      </span>
      <span>
        <i className="tl-lg is-wait" /> waiting, free (queued, ready)
      </span>
      <span>
        <i className="tl-lg is-park" /> parked, free
      </span>
      <span>
        <i className="tl-lg is-cut" /> fenced generation
      </span>
      <span className="tl-mk is-warn">
        <MarkIcon mark="warn" /> cancel asked
      </span>
      <span className="tl-mk is-neu">
        <MarkIcon mark="succeeded" /> ended
      </span>
      <span className="tl-mk is-bad">
        <MarkIcon mark="failed" /> failed · dead-lettered
      </span>
    </p>
  )
}


function pct(t: number, since: number, until: number): number {
  return ((Math.min(Math.max(t, since), until) - since) / (until - since)) * 100
}

function Axis({
  since,
  until,
  nowAt,
  summary,
  onZoom,
  onTrackPx,
}: {
  since: number
  until: number
  nowAt: number
  summary: string
  onZoom: (since: number, until: number) => void
  /** Told the track's measured width: every lane's track is the same column (V039). */
  onTrackPx?: (px: number | null) => void
}) {
  // The track's width, so the ticks keep their pixel gaps (G3-15). Null
  // until measured, and in a browser without ResizeObserver: the
  // percentage rule alone then.
  const track = useRef<HTMLDivElement | null>(null)
  const [trackPx, setTrackPx] = useState<number | null>(null)
  useEffect(() => {
    const el = track.current
    if (el === null || typeof ResizeObserver === 'undefined') return
    const measure = () => {
      const px = el.getBoundingClientRect().width || null
      setTrackPx(px)
      onTrackPx?.(px)
    }
    measure()
    const watch = new ResizeObserver(measure)
    watch.observe(el)
    return () => watch.disconnect()
    // `onTrackPx` is a state setter, stable for the screen's life.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])
  const ticks = axisLabels(since, until, nowAt, trackPx)
  const [drag, setDrag] = useState<{ x0: number; x1: number; w: number; left: number } | null>(null)
  const at = (x: number, d: { w: number; left: number }) => since + (Math.min(Math.max(x - d.left, 0), d.w) / d.w) * (until - since)
  const down = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (e.button !== 0) return
    const r = e.currentTarget.getBoundingClientRect()
    if (!(r.width > 0)) return
    // Captured, so a drag by touch or pen keeps reaching the axis.
    e.currentTarget.setPointerCapture?.(e.pointerId)
    setDrag({ x0: e.clientX, x1: e.clientX, w: r.width, left: r.left })
  }
  const move = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (drag !== null) setDrag({ ...drag, x1: e.clientX })
  }
  const up = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (drag === null) return
    const d = { ...drag, x1: e.clientX }
    setDrag(null)
    if (Math.abs(d.x1 - d.x0) < DRAG_MIN_PX) return
    onZoom(at(Math.min(d.x0, d.x1), d), at(Math.max(d.x0, d.x1), d))
  }
  const brush =
    drag !== null && Math.abs(drag.x1 - drag.x0) >= DRAG_MIN_PX
      ? {
          left: ((Math.min(drag.x0, drag.x1) - drag.left) / drag.w) * 100,
          width: (Math.abs(drag.x1 - drag.x0) / drag.w) * 100,
          words: rangeWords(at(Math.min(drag.x0, drag.x1), drag), at(Math.max(drag.x0, drag.x1), drag)),
        }
      : null
  return (
    <div className="tl-row tl-axis">
      <div className="tl-lab">
        <small>{summary}</small>
        <small className="tl-zone">times {axisZone(since, until)}</small>
      </div>
      <div
        ref={track}
        className="tl-trk"
        title="Drag across the axis to zoom"
        onPointerDown={down}
        onPointerMove={move}
        onPointerUp={up}
        onPointerCancel={() => setDrag(null)}
      >
        {ticks.map((t) => (
          <span key={t.t} className="tl-tk" style={{ left: `${t.pct}%` }}>
            {t.label}
          </span>
        ))}
        {until >= nowAt - 60_000 && <span className="tl-tk is-now">now</span>}
        {brush !== null && (
          <span className="tl-brush" style={{ left: `${brush.left}%`, width: `${brush.width}%` }}>
            <b>drag to zoom: {brush.words}</b>
          </span>
        )}
      </div>
    </div>
  )
}

/**
 * A WORKFLOW'S NAME FROM ITS OWN LANES (visual QA VQ-025, 2026-10-26): the
 * group row printed the raw id and "workflow: reading…" for seconds, until the
 * workflow read landed and `workflowLabel` named it from its steps' tasks --
 * tasks the lanes already carry. The same rule (`unit` before `title`, the
 * first that has one), over the lanes drawn; the workflow read's own label
 * wins once it lands.
 */
export function lanesLabel(lanes: readonly Lane[]): string | null {
  for (const key of ['unit', 'title'] as const) {
    for (const l of lanes) {
      const value = l.task?.metadata?.[key]
      if (typeof value === 'string' && value.trim() !== '') return value.trim()
    }
  }
  return null
}

/**
 * Where a block's lanes draw, as a share of the window: the first segment or
 * mark to the last (timeline.html A's bracket on a workflow's group row,
 * VQ-025). Null for a block with nothing drawn in the window.
 */
export function blockSpan(lanes: readonly Lane[], since: number, until: number): { left: number; width: number } | null {
  let from = Infinity
  let to = -Infinity
  for (const l of lanes) {
    for (const s of l.segs) {
      from = Math.min(from, s.from)
      to = Math.max(to, s.to)
    }
    for (const m of l.marks) {
      from = Math.min(from, m.at)
      to = Math.max(to, m.at)
    }
  }
  if (!(from <= to) || to < since || from > until) return null
  const left = pct(from, since, until)
  return { left, width: pct(to, since, until) - left }
}

/**
 * A LANE'S NAME, on its row and in its panel (visual QA pass 2, runs2-27): a
 * step is its step id (`agentName`); a lone agent is its own title when it
 * has one, else its profile in full, then its short id. `claude-code agent ·
 * 108ef29c` was cut to `claude-c… · 108ef29c` on every standalone row, so the
 * part a person reads was lost and the hex kept; 'agent' says nothing a lane
 * does not.
 */
export function laneName(t: Task): string {
  if ((t.step_id ?? null) !== null) return agentName(t)
  const title = t.metadata?.title
  const what = typeof title === 'string' && title.trim() !== '' ? title.trim() : t.runner_profile === 'browser' ? agentKind(t.runner_profile) : t.runner_profile
  return `${what} · ${shortTaskId(t.id)}`
}

/**
 * A GROUP'S SPAN STOPS AT 'now', ITS RIGHT EDGE IN VIEW (visual QA pass 2,
 * runs2-16; pass 1 runs-submit-35): a running workflow's span reached the
 * track's right edge, under the 'now' rule and the chart's border, so its
 * right edge was never seen and it read as an open outline. It keeps
 * `TRACK_END_PX` clear of the edge, as a lane's bars do (`laneLayout`).
 */
export function bracketStyle(b: { left: number; width: number }): CSSProperties {
  return { left: `${b.left}%`, width: `min(${b.width}%, calc(${100 - b.left}% - ${TRACK_END_PX}px))` }
}

function GroupHead({
  block,
  since,
  until,
  workflow,
  onRetry,
  name = null,
  ran = [],
  empty = [],
}: {
  block: LaneBlock
  /** The window, so a workflow's bracket is placed on the lanes' own scale. */
  since: number
  until: number
  workflow: WorkflowState | null
  onRetry: () => void
  /** The workflow's label, when its steps carry one (RT-25); the id is then its title. */
  name?: string | null
  /** Its lanes with an attempt in this span, before the filters. */
  ran?: readonly Lane[]
  /** Its steps drawn as an empty lane (a task with no attempt yet), before the filters. */
  empty?: readonly Lane[]
}) {
  let title: string
  let note: ReactNode
  let noteTitle: string | undefined
  let gap: string | null = null
  if (block.kind === 'workflow') {
    title = name ?? block.key
    // NAME, THEN STEPS · ID (timeline.html A; VQ-025): with a name in the
    // title the id closes the subtitle, so the row still says which workflow
    // it is; nameless, the id is the title and is not said twice. The id goes
    // LAST, after the steps and the state (review of VQ-025): leading, a
    // real-length id (`wf_5e5ad3b6f7da4299a839 · `) filled the 215px note cell
    // at 1440 and cut the steps count and the state off. The whole note is the
    // `<small>`'s title, so what the width cuts is read on hover.
    const id = name === null ? '' : ` · ${block.key}`
    // A FAILED WORKFLOW READ IS SAID IN WORDS, with the route in the title
    // only (QA G3-07): the subtitle printed the raw route and status, which
    // the label's width cut to `steps — (/v1/workflows/wf_55…`.
    if (workflow === null) {
      noteTitle = `steps: reading…${id}`
      note = noteTitle
    } else if (workflow.status === 'error') {
      const why = `steps not read: ${workflow.limited ? 'rate-limited' : workflow.why}`
      note = (
        <>
          <span className="tl-wf-unread" title={`${workflow.route} ${workflow.why}`}>
            {why} ·{' '}
            <button type="button" className="c-link is-sm tl-wf-retry" onClick={onRetry}>
              retry
            </button>
          </span>
          {id}
        </>
      )
      noteTitle = `${why}${id}`
    } else {
      noteTitle = `${stepsNote(workflow.read, ran)}${id}`
      note = noteTitle
    }
    if (workflow?.status === 'ok') gap = stepsGap(workflow.read, ran, empty)
  } else if (block.kind === 'standalone') {
    title = 'Standalone'
    note = `${block.lanes.length} ${block.lanes.length === 1 ? 'agent' : 'agents'} in this span`
  } else if (block.kind === 'unread') {
    title = 'Agent not read'
    note = `${block.lanes.length} ${block.lanes.length === 1 ? 'lane' : 'lanes'}: workflow and profile unknown`
  } else if (block.kind === 'profile') {
    title = block.key
    note = `${block.lanes.length} ${block.lanes.length === 1 ? 'lane' : 'lanes'}`
  } else {
    title = 'All lanes'
    note = `${block.lanes.length}`
  }
  const state = workflow?.status === 'ok' ? workflow.read.workflow.state : null
  // Only a workflow is one thing with a span; a profile or the standalone block is a list.
  const bracket = block.kind === 'workflow' ? blockSpan(block.lanes, since, until) : null
  return (
    <div className="tl-row is-group" data-group={block.key}>
      <div className="tl-lab">
        {/* THE TITLE GIVES WAY AS A LANE'S NAME DOES (visual QA run 3,
            work-a-004, 2026-10-21). A bare link or text run in this flex
            `<b>` is as wide as its text, so a long title ran to the column's
            edge and was sheared there with no ellipsis, while the lanes under
            it ended in `…` before their mark. `.tl-name-k` is the name part
            that shrinks and cuts (timeline.css); the link keeps its colour. */}
        <b>
          {state !== null && state in STATE_MARK && <StateMark state={state as TaskState} bare />}
          {block.kind === 'workflow' && workflow?.status === 'ok' ? (
            <a className="tl-name-k" href={`/workflows/${encodeURIComponent(block.key)}`} title={block.key}>{title}</a>
          ) : (
            <span className="tl-name-k" title={title}>
              {title}
            </span>
          )}
        </b>
        <small title={noteTitle}>{note}</small>
        {gap !== null && <small className="tl-wf-gap">{gap}</small>}
      </div>
      <div className="tl-trk">
        {bracket !== null && (
          <span className="tl-wf-span" aria-hidden="true" style={bracketStyle(bracket)} />
        )}
      </div>
    </div>
  )
}

/**
 * A WORKFLOW'S HEADER COUNTS THE STEPS ITS LANES DRAW (visual QA run 4,
 * work-a-008, 2026-10-22): wf_audit_01 said '5 steps' over four lanes, its
 * fifth step not started, and nothing said which was missing. A step draws a
 * lane here only with an attempt in the span, so the header says how many
 * do -- which stays true for a step not started, one never admitted, and one
 * whose only attempt is outside the span, none of which an empty lane can
 * place under every filter -- and `stepsGap` names the others.
 */
function stepRan(step: { step_id: string; task_id?: string | null }, ran: readonly Lane[]): boolean {
  return ran.some(
    (l) => (step.task_id != null && l.taskId === step.task_id) || (typeof step.step_id === 'string' && l.task?.step_id === step.step_id),
  )
}

export function stepsNote(read: WorkflowRead, ran: readonly Lane[]): string {
  const steps = read.workflow.steps
  const n = steps.length
  const state = read.workflow.state.toLowerCase().replace('_', '-')
  // A lane of this workflow is a step that ran, even where the read's step
  // list does not name its task.
  const k = Math.min(n, Math.max(steps.filter((s) => stepRan(s, ran)).length, new Set(ran.map((l) => l.taskId)).size))
  if (k >= n) return `${n} ${n === 1 ? 'step' : 'steps'} · ${state}`
  return `${k} of ${n} ${n === 1 ? 'step has' : 'steps have'} attempts in this span · ${state}`
}

// A step whose task has no attempt yet is drawn as its own empty lane
// ('never ran yet · no attempt', from the workflow read), so it is not one
// the header names as having no lane: that would deny the lane under it.
export function stepsGap(read: WorkflowRead, ran: readonly Lane[], empty: readonly Lane[] = []): string | null {
  const why = (s: { task_id?: string | null }): string => {
    if (s.task_id === null) return 'not started'
    const t = read.tasks.find((x) => x.id === s.task_id)
    return t !== undefined && t.attempt_count === 0 ? 'no attempt yet' : 'no attempt in this span'
  }
  const out = read.workflow.steps
    .filter((s) => typeof s.step_id === 'string' && !stepRan(s, ran) && !stepRan(s, empty))
    .map((s) => `${s.step_id} (${why(s)})`)
  return out.length === 0 ? null : `no lane: ${out.join(', ')}`
}

function laneNote(lane: Lane, events: EventsRead | null, single: Task | string | null, taskPageFailed: boolean): string {
  const t = lane.task
  if (t === null) {
    const why =
      typeof single === 'string'
        ? `its read answered ${single}`
        : single === null
          ? taskPageFailed
            ? 'the agent list read failed'
            : `older than the agent read reached, and over the ${MISSING_TASK_READS}-read cap or still reading`
          : 'not read'
    return `profile —, agent not read (${why})`
  }
  // THE ID IS THE LABEL'S TOOLTIP, not the note's first word (walkthrough G).
  const parts = [t.runner_profile]
  if (lane.neverRan) parts.push(TERMINAL_STATES.has(t.state) ? 'never ran · no attempt' : 'never ran yet · no attempt')
  // THE TASK'S COUNT BESIDE THE SPAN'S (live QA RT-24): `2 attempts` sat over
  // an inspector saying `current generation 4`; two were in the span read.
  else if (t.attempt_count > lane.attempts.length) parts.push(`${lane.attempts.length} of ${t.attempt_count} attempts in this span`)
  else if (lane.attempts.length > 1) parts.push(`${lane.attempts.length} attempts`)
  if (!lane.neverRan && events?.status === 'error') {
    parts.push(`fence and cancel marks: — this lane's events read failed (${events.httpStatus ?? 'no answer'})`)
  }
  return parts.join(' · ')
}

/**
 * HOW LONG A LANE'S ATTEMPTS HELD CAPACITY: its bars (held and fenced), not
 * its parks or its waits, which hold nothing (invariant 1). Null for a lane
 * that never ran, or whose every bar ended at an instant nobody wrote --
 * a duration made of guesses is not one.
 */
function heldFor(lane: Lane): number | null {
  const bars = lane.segs.filter((s) => (s.kind === 'hold' || s.kind === 'cut') && (s.endKnown || s.open))
  if (bars.length === 0) return null
  return bars.reduce((sum, s) => sum + Math.max(0, s.to - s.from), 0)
}

/**
 * A LANE READS ITS EVENTS ONLY ONCE IT IS ON SCREEN (live QA CH-6,
 * 2026-10-11): the page made 25 events reads on load at 1440. The cards'
 * `CARD_ROOT_MARGIN` reads 200px ahead, which is right for a card a reader
 * scrolls to and wrong for a lane: a lane is a ~30px row, so 200px either
 * side is a dozen lanes nobody is looking at. The selected lane reads its own
 * events wherever it is (`onRead`).
 */
export const LANE_ROOT_MARGIN = '0px'

function LaneRow({
  lane,
  since,
  until,
  trackPx,
  events,
  single,
  taskPageFailed,
  selected,
  onSelect,
  onSeen,
  readNonce,
  onRetry,
}: {
  lane: Lane
  since: number
  until: number
  trackPx: number | null
  events: EventsRead | null
  single: Task | string | null
  taskPageFailed: boolean
  selected: boolean
  onSelect: () => void
  onSeen: (id: string) => void
  /** The screen's read generation: a refresh asks a row still in view again. */
  readNonce: number
  onRetry: (id: string) => void
}) {
  const [ref, inView] = useInView<HTMLDivElement>(LANE_ROOT_MARGIN)
  useEffect(() => {
    if (inView && !lane.neverRan) onSeen(lane.taskId)
  }, [inView, lane.neverRan, lane.taskId, onSeen, readNonce])
  const t = lane.task
  const name = t !== null ? laneName(t) : lane.taskId
  const held = heldFor(lane)
  // A STEP ITS REVIEW VERDICT SKIPPED IS NOT A RUN (QA G3-02, 2026-10-07):
  // `fix ✓ · 1m 15s` read as a successful run where the graph says `agent not
  // run`. Its attempt held capacity to publish the reviewed work, and its bar
  // stays; the label says the agent did not run and which verdict kept it.
  const skipped = t !== null && t.state === 'SUCCEEDED' && skippedByVerdict(t)
  const wide = until - since > 36 * 3_600_000
  // A current state that began at the window's end is still drawn: its
  // floor is what says, at 'now', that the lane is parked or waiting.
  const segs = lane.segs.filter((s) => s.to > since && (s.from < until || (s.current && s.from <= until)))
  // CROWDED ATTEMPTS ARE ONE COUNTED PILL (owner, c00, 2026-10-22; box
  // comment 6314122402): `lanePills` merges the attempts closer than a pill
  // at this span's scale, and puts the end mark after the last pill. The
  // pills a floor widens (not a wait's 2 px line) are laid out with the
  // marks, so none is drawn past the track's edge or over another (V039).
  // The lane's current state is not among them: it is drawn on its own rail
  // beneath the pills, to now (work-a-005), so it moves no pill.
  const written = lane.marks.filter((m) => m.at >= since && m.at <= until)
  const crowd = lanePills(segs, written, since, until)
  const marks = crowd.marks
  const pills = crowdPills(crowd.pills, since, until, marks.some((m) => m.kind !== 'cancel_requested'))
  const layout = laneLayout(
    pills.map((p) => ({ from: pct(p.from, since, until), to: pct(p.to, since, until), min: p.segs.length > 1 ? PILL_COUNT_MIN_PX : undefined })),
    marks.map((m) => ({ pct: pct(m.at, since, until), end: m.kind !== 'cancel_requested' })),
    trackPx,
  )
  const lefts = layout.marks
  // Each seg's pill; a seg in none (a wait, the current state, a park under
  // a merged pill) is drawn at its instants or not at all.
  const pillOf = new Map(pills.flatMap((p, j) => p.segs.map((s) => [s, j] as const)))
  const boxOf = new Map(segs.flatMap((s, i) => (pillOf.has(s) ? [[i, layout.bars[pillOf.get(s)!]!] as const] : [])))
  // The open merged pill's attempt list, and what opened it: a tap holds it
  // open past the pointer leaving; a blur or Escape closes it either way.
  const [shown, setShown] = useState<{ at: number; how: 'hover' | 'focus' | 'tap' } | null>(null)
  const open = shown !== null && pills[shown.at] !== undefined && pills[shown.at]!.segs.length > 1 ? pills[shown.at]! : null
  const listId = (j: number) => `tl-pill-${lane.taskId}-${j}`
  // A LONE AGENT'S ID IS WHAT TELLS ITS LANE FROM THE NEXT (VQA2 V212): the
  // name `claude-code task · a073aff5` was ellipsized from its end, so the
  // id went first and every standalone lane read `claude-code task · a0…`.
  // The kind gives way; the short id after it never does.
  const idAt = t !== null && t.step_id == null ? name.lastIndexOf(' · ') : -1
  return (
    <div ref={ref} className={selected ? 'tl-row is-sel' : 'tl-row'} data-lane={lane.taskId}>
      <div className="tl-lab">
        {/* THE NAME, THE STATE AND HOW LONG IT HELD CAPACITY -- `implement ✓
            · 1h 18m` (walkthrough G, owner 2026-10-03). The label led with
            the task id; the id is the name's tooltip now. A lone agent's
            line wraps (pass 2 review, runs2-27): beside the mark and the time
            held, 240 px left `claude-c… · 99284d38`. */}
        <b className={idAt < 0 ? undefined : 'is-split'}>
          <button
            type="button"
            className={idAt < 0 ? 'tl-name' : 'tl-name is-split'}
            aria-pressed={selected}
            onClick={onSelect}
            title={`${name} · ${lane.taskId}`}
          >
            {idAt < 0 ? (
              name
            ) : (
              <>
                <span className="tl-name-k">{name.slice(0, idAt)}</span>
                <span className="tl-name-id">{name.slice(idAt)}</span>
              </>
            )}
          </button>
          {t !== null && <StateMark state={t.state} bare />}
          {!skipped && held !== null && (
            <span className="tl-dur" title="Time its attempts held capacity">
              {' · '}
              {formatDuration(held)}
            </span>
          )}
        </b>
        {/* THE VERDICT NOTE HAS ITS OWN LINE (live QA RT-9, 2026-10-11): beside
            the name it took the name's room, and `fix` and `re-review` were
            cut to `f.` and `r.` with the label column half empty. */}
        {skipped && (
          <small className="tl-dur is-skip" title={`${SKIPPED_WORD}: its agent was not run, and its attempt only published the reviewed work`}>
            {`not run (verdict ${(t !== null ? gateVerdictOf(t) : null) ?? '—'})`}
          </small>
        )}
        <small>{laneNote(lane, events, single, taskPageFailed)}</small>
        {events?.status === 'error' && !lane.neverRan && (
          <button type="button" className="c-link tl-retry" onClick={() => onRetry(lane.taskId)}>
            read its events again
          </button>
        )}
      </div>
      <div className="tl-trk">
        {segs.map((s, i) => {
          const j = pillOf.get(s)
          const p = j === undefined ? null : pills[j]!
          // A park under a merged pill is not drawn apart from it.
          if (p === null && s.kind === 'park' && !s.current) return null
          if (p !== null && p.segs.length > 1) {
            // A merged pill is drawn once, where its oldest attempt was.
            if (p.segs[0] !== s) return null
            return (
              <MergedPill
                key={i}
                p={p}
                attempts={lane.attempts}
                box={boxOf.get(i)!}
                wide={wide}
                listId={listId(j!)}
                expanded={open === p}
                onShow={(how) => setShown((was) => showPill(was, j!, how))}
                onHide={(how) => setShown((was) => hidePill(was, j!, how))}
              />
            )
          }
          return (
            <Seg
              key={i}
              s={s}
              since={since}
              until={until}
              box={boxOf.get(i)}
              words={trackPx === null || !boxOf.has(i) || (boxOf.get(i)!.width / 100) * trackPx >= BAR_WORDS_MIN_PX}
              wide={wide}
              multi={lane.attempts.length > 1}
            />
          )
        })}
        {/* A MARK IS REACHABLE (live QA RT-24, 2026-10-11): `title`-only spans
            took no focus and did nothing on a click. Each is a control now,
            named by its words, that opens the lane's inspector. */}
        {marks.map((m, i) => (
          <span
            key={`m${i}`}
            className={`tl-mkr is-${MARK_HUE[m.kind]}${m.kind === 'cancel_requested' ? '' : ' is-end'}`}
            data-mark={m.kind}
            style={{ left: `${lefts[i]!}%` }}
            title={`${MARK_WORD[m.kind]} ${clock(written[i]!.at, wide)}`}
            role="button"
            tabIndex={0}
            aria-label={`${name}: ${MARK_WORD[m.kind]} ${clock(written[i]!.at, wide)}`}
            onClick={onSelect}
            onKeyDown={(e) => {
              if (e.key !== 'Enter' && e.key !== ' ') return
              e.preventDefault()
              onSelect()
            }}
          >
            <MarkIcon mark={MARK_OF[m.kind]} />
            {m.kind === 'cancel_requested' && <span className="tl-tag">{`cancel asked ${clock(m.at, wide)}`}</span>}
          </span>
        ))}
      </div>
      {/* THE MERGED PILL'S ATTEMPTS, UNDER THE TRACK (c00): the track and the
          chart clip, so a list inside the track would be cut at its edge. */}
      {open !== null && (
        <ul id={listId(shown!.at)} className="tl-pill-list" aria-label={`${name}: ${pillName(open, lane.attempts)}`}>
          {open.segs.map((s) => (
            <li key={s.attemptId ?? `${s.from}`}>{attemptLine(s, lane.attempts, wide)}</li>
          ))}
        </ul>
      )}
    </div>
  )
}

type PillShown = { at: number; how: 'hover' | 'focus' | 'tap' } | null

/** A merged pill asked to open: a tap on a tapped pill closes it, and a tap holds it past hover and focus. */
function showPill(was: PillShown, at: number, how: 'hover' | 'focus' | 'tap'): PillShown {
  if (was !== null && was.at === at && was.how === 'tap') return how === 'tap' ? null : was
  return { at, how }
}

/** A merged pill let go of: the pointer leaving closes only what hovering opened; a blur or Escape closes it. */
function hidePill(was: PillShown, at: number, how: 'hover' | 'focus' | 'escape'): PillShown {
  if (was === null || was.at !== at) return was
  if (how === 'hover' && was.how !== 'hover') return was
  return null
}

/** One attempt in a merged pill's list: `gen 2 · failed · 14:02 → 14:07`. */
function attemptLine(s: LaneSeg, attempts: readonly AttemptRow[], wide: boolean): string {
  const word = attemptWord(s, attempts.find((a) => a.attempt_id === s.attemptId))
  return `gen ${s.gen ?? '—'} · ${word} · ${clock(s.from, wide)} → ${s.open ? 'now' : clock(s.to, wide)}`
}

/**
 * CROWDED ATTEMPTS AS ONE PILL (owner, c00, 2026-10-22): the count inside, in
 * the newest attempt's style, named `3 attempts, newest failed`. Hovering,
 * focusing or tapping it lists each attempt under the track (`LaneRow`).
 */
function MergedPill({
  p,
  attempts,
  box,
  wide,
  listId,
  expanded,
  onShow,
  onHide,
}: {
  p: Pill
  attempts: readonly AttemptRow[]
  box: { left: number; width: number }
  wide: boolean
  listId: string
  expanded: boolean
  onShow: (how: 'hover' | 'focus' | 'tap') => void
  onHide: (how: 'hover' | 'focus' | 'escape') => void
}) {
  const newest = p.segs[p.segs.length - 1]!
  const cls = [
    'tl-seg',
    `is-${newest.kind}`,
    'is-merged',
    newest.open ? 'is-open' : '',
    newest.endKnown ? '' : 'is-unknown-end',
  ]
    .filter((c) => c !== '')
    .join(' ')
  return (
    <button
      type="button"
      className={cls}
      data-seg={newest.kind}
      style={{ left: `${box.left}%`, width: `${box.width}%` }}
      title={`${p.segs.length} attempts: ${p.segs.map((s) => attemptLine(s, attempts, wide)).join('; ')}`}
      aria-label={pillName(p, attempts)}
      aria-expanded={expanded}
      aria-controls={expanded ? listId : undefined}
      onMouseEnter={() => onShow('hover')}
      onMouseLeave={() => onHide('hover')}
      onFocus={() => onShow('focus')}
      onBlur={() => onHide('focus')}
      onClick={() => onShow('tap')}
      onKeyDown={(e) => {
        if (e.key !== 'Escape' || !expanded) return
        e.stopPropagation()
        onHide('escape')
      }}
    >
      <span className="tl-seg-n" aria-hidden="true">
        {p.segs.length}
      </span>
    </button>
  )
}

export function Seg({
  s,
  since,
  until,
  box,
  words = true,
  wide,
  multi,
}: {
  s: LaneSeg
  since: number
  until: number
  /** Where `laneLayout` drew it; a wait is drawn at its instants. */
  box?: { left: number; width: number }
  /** Wide enough for its words; a narrower bar says them in its title only. */
  words?: boolean
  wide: boolean
  multi: boolean
}) {
  const left = box?.left ?? pct(s.from, since, until)
  const width = box?.width ?? Math.max(pct(s.to, since, until) - left, s.kind === 'wait' ? 0 : 0.4)
  // THE CURRENT STATE IS HELD TO ITS END, 'now' (work-a-005): anchored at
  // its right edge, so the CSS floor widens it leftward from now, never past
  // the track's edge.
  const right = s.current ? 100 - pct(s.to, since, until) : null
  const cls = [
    'tl-seg',
    `is-${s.kind}`,
    s.current ? 'is-now' : '',
    s.open ? 'is-open' : '',
    s.future ? 'is-future' : '',
    s.endKnown ? '' : 'is-unknown-end',
  ]
    .filter((c) => c !== '')
    .join(' ')
  const span = `${clock(s.from, wide)} → ${s.open ? 'now' : clock(s.to, wide)}`
  const title =
    s.kind === 'hold'
      ? `${s.label ?? 'attempt'}: held capacity ${span}${s.endKnown ? '' : ' (no completed_at: its end was never written)'}`
      : s.kind === 'cut'
        ? `${s.label}: ${span}, then fenced`
        : s.kind === 'park'
          ? s.future
            ? `expected to resume by ${clock(s.to, wide)}; nothing held`
            : `parked ${span}${s.reason ? ` · ${s.reason}` : ''}; nothing held`
          : `${s.current && s.label !== null ? `${s.label}, ` : ''}waiting ${span}; nothing held`
  const fromTask = s.kind === 'park' && s.current && s.label !== null ? ` (${s.label})` : ''
  return (
    <span
      className={cls}
      data-seg={s.kind}
      style={right !== null ? { right: `${right}%`, width: `${width}%` } : { left: `${left}%`, width: `${width}%` }}
      title={`${title}${fromTask}`}
    >
      {words && s.kind === 'cut' && <span className="tl-seg-t">{s.label}</span>}
      {words && s.kind === 'hold' && multi && <span className="tl-seg-t">{s.label}</span>}
      {words && s.kind === 'park' && !s.future && !s.current && <span className="tl-seg-t">parked</span>}
    </span>
  )
}

// ---------------------------------------------------------------------------
// The outcome strip
// ---------------------------------------------------------------------------

/**
 * BOTH RANGES, WHERE THE TWO PAGES LINK (QA G3-08): Outcomes reads the span
 * from a bucket boundary, these lanes the last span to the minute. Null until
 * the strip's read has said which range it covered.
 */
export function bothRanges(strip: StripRead, since: number, until: number): string | null {
  if (strip.status !== 'ok') return null
  const aligned = alignedWords(strip.data)
  return `Outcomes reads ${ledgerRange(strip.data)}${aligned === null ? '' : ` (${aligned})`}; these lanes read ${rangeWords(since, until)}`
}

/** One bucket of the ledger, the most a span's alignment moves either end of it. */
const BUCKET_MS = { hour: 3_600_000, day: 86_400_000, week: 7 * 86_400_000, month: 31 * 86_400_000 } as const

/**
 * THE HEADLINE NAMES ITS OWN WINDOW (visual QA run 6, 6:work-a-003; box
 * 6343760837). '91 % success, 272 of 300 decided', the largest text on the
 * page, is the ledger's over Sep 12 – Sep 25, above lanes drawn over the last
 * 24 hours; only the small line under it said so. When either end of the
 * ledger's range lies more than one of its buckets from the span's (more than
 * the alignment `alignedWords` explains), the window, in the ledger's zone:
 * `Sep 12 – Sep 25`, or `Oct 23, 09:00–21:00` inside one day. Null when the
 * two agree.
 */
export function decidedWindow(
  d: Pick<Outcomes, 'since' | 'until' | 'tz' | 'bucket'>,
  since: number,
  until: number,
): string | null {
  const from = Date.parse(d.since)
  const to = Date.parse(d.until)
  const slack = BUCKET_MS[d.bucket] ?? BUCKET_MS.day
  if (Math.abs(from - since) <= slack && Math.abs(to - until) <= slack) return null
  const day = new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric', timeZone: d.tz })
  const hm = new Intl.DateTimeFormat('en-GB', { hour: '2-digit', minute: '2-digit', hourCycle: 'h23', timeZone: d.tz })
  const a = day.format(from)
  const b = day.format(to)
  return a === b ? `${a}, ${hm.format(from)}–${hm.format(to)}` : `${a} – ${b}`
}

function OutcomeStrip({
  strip,
  lanes,
  lanesRead,
  eventsReading = false,
  view,
  since,
  until,
  reach = null,
}: {
  strip: StripRead
  lanes: readonly Lane[]
  /**
   * Whether the attempts behind `lanes` have been read (visual QA QA-093 and
   * QA-095, 2026-10-20). Before they land `lanes` is empty, and fences and
   * parks counted over it printed '0 generations fenced' and '0 parks, 0
   * still parked' as measured, beside "Parks and fences count only the lanes
   * drawn" over no lane at all. Until then the counts' places say why there
   * is no figure.
   */
  lanesRead: 'reading' | 'failed' | 'read'
  /**
   * WHETHER A LANE'S EVENTS READ IS STILL OUT (visual QA run 6, 6:work-a-001;
   * QA-027, box 6283709068). The capture shot 'events not read on 17 lanes'
   * while the page text, read a moment later, had the figures: nothing said
   * the cells were still reading. The fenced and parks cells carry
   * `aria-busy` while any read is out, which the capture waits on; a read
   * that failed is settled, so 'events not read' then stands unflagged.
   */
  eventsReading?: boolean
  view: LanesView
  since: number
  until: number
  /** The oldest attempt drawn when the read stopped short of the span (RT-1). */
  reach?: number | null
}) {
  const fenced = lanes.reduce((n, l) => n + l.segs.filter((s) => s.kind === 'cut').length, 0)
  const drawn = lanes.filter((l) => !l.neverRan)
  const unread = drawn.filter((l) => !l.eventsRead).length
  const parks = drawn.reduce((n, l) => n + l.segs.filter((s) => s.kind === 'park' && !s.future).length, 0)
  const still = drawn.filter((l) => l.task?.state === 'PARKED').length
  const busy = lanesRead === 'reading' || eventsReading ? true : undefined
  const link = (
    <a href={`/timeline/outcomes?${outcomesQueryFor(view)}`} title={bothRanges(strip, since, until) ?? undefined}>
      Outcomes for this span ›
    </a>
  )
  // THE STRIP'S COUNTS ARE THE LEDGER'S, OVER THE LEDGER'S RANGE (QA G3-08):
  // it said "Decided counts cover 20:57–20:57" over counts the route had read
  // from 22:00, so its numbers matched neither page.
  const aligned = strip.status === 'ok' ? alignedWords(strip.data) : null
  // THE FIGURES ARE SHOWN ONLY OVER THE SPAN THEY COVER (visual QA pass 2,
  // runs2-04; pass 1 runs-submit-10): led by their window, 'Outcomes Oct 6 –
  // Oct 19: 91 % success' still sat beside lanes drawn over Oct 28–29, and
  // every span button kept the same figures. When the outcomes read answered
  // for a window more than a bucket off the span's, its figures are not this
  // span's: their place is a dash and the window the read answered for.
  const own = strip.status === 'ok' ? decidedWindow(strip.data, since, until) : null
  return (
    <div className="tl-strip">
      {strip.status === 'loading' ? (
        <p className="tl-q">Reading outcomes…</p>
      ) : strip.status === 'error' ? (
        <p>
          <span>{`Outcomes not read (${strip.error.httpStatus === null ? '' : `HTTP ${strip.error.httpStatus}: `}${strip.error.message}).`}</span> The lanes below are unaffected.
        </p>
      ) : (
        <p className="tl-strip1">
          {own !== null ? (
            <span className="tl-x tl-off">
              outcomes <b className="tl-dash">—</b> not this span, read for <span className="tl-win">{own}</span>
            </span>
          ) : strip.data.totals.rate === null ? (
            <span className="tl-x">
              <b className="tl-dash">—</b> success: no agent was decided in this span
            </span>
          ) : (
            <span className="tl-x">
              <b>{Math.round(strip.data.totals.rate.p * 100)} %</b> success, {strip.data.totals.rate.k} of {strip.data.totals.rate.n} decided
            </span>
          )}
          {/* THE FINISHED TOTAL, SPLIT INTO PARTS THAT ADD UP TO IT (#123):
              succeeded + failed + cancelled = finished, with failed FAILED +
              DEAD_LETTERED, the one definition the ledger uses. The rate
              beside it leaves the cancels out, so its n is not this total. */}
          {/* EACH FIGURE KEEPS ITS WORD AND ITS SEPARATOR (VQA V105): the
              words between the figures were bare flex items, so at 400px
              "succeeded ·" wrapped inside itself and left each "·" alone on
              a line. A part is one unbreakable `.tl-xp`; the line breaks
              only between parts, after a separator, never before one. */}
          {own === null && (
          <span className="tl-x tl-ended">
            <span className="tl-xp">
              <b className="tl-n-ended">{strip.data.totals.ended}</b> finished:
            </span>{' '}
            <span className="tl-xp">
              <b className="tl-n-ok">{strip.data.totals.succeeded}</b> succeeded ·
            </span>{' '}
            <span className="tl-xp">
              <b className="tl-n-bad">{strip.data.totals.failed + strip.data.totals.dead_lettered}</b> failed ·
            </span>{' '}
            <span className="tl-xp">
              <b className="tl-n-cancelled">{strip.data.totals.cancelled.total}</b> cancelled
            </span>
          </span>
          )}
          {/* ONE CELL WHILE COUNTING, ONE WHEN SHORT (visual QA pass 1,
              runs-submit-33 and chrome-C17): the fenced and parks cells each
              printed a dash, 'reading the lanes' while the read was out and
              'events not read on 332 lanes' twice once it settled, which read
              as a failure on every lane. While any read is out the counts'
              place says counting; a read that settled short says of how many
              lanes, once, and how to read the rest. A fence count already
              seen stands as its floor. */}
          {lanesRead === 'failed' ? (
            <span className="tl-x">
              fenced and parks <b className="tl-dash">—</b> the lanes were not read
            </span>
          ) : lanesRead === 'reading' || (unread > 0 && eventsReading) ? (
            fenced > 0 ? (
              <span className="tl-x" aria-busy={busy}>
                <b>{fenced}+</b> generations fenced, counting…
              </span>
            ) : (
              <span className="tl-x" aria-busy={busy}>
                fenced and parks <b className="tl-dash">—</b> counting…
              </span>
            )
          ) : unread > 0 ? (
            <>
              {fenced > 0 && (
                <span className="tl-x" aria-busy={busy}>
                  <b>{fenced}+</b> generations fenced
                </span>
              )}
              {/* ONE LANE COUNT, AND 'NOT COUNTED' SAID PLAINLY (visual QA
                  pass 2, runs2-15 and runs2-25): the head said '25 lanes' and
                  this '18 of 24', its count leaving out the workflow step
                  drawn with no attempt, and 'events not read' read as a
                  failure. Out of the head's lanes, the ones whose events are
                  not counted yet; a step that never ran has nothing to count. */}
              <span className="tl-x" aria-busy={busy}>
                {fenced > 0 ? 'parks and further fences' : 'fenced and parks'} <b className="tl-dash">—</b> not counted on {unread} of {lanes.length}{' '}
                {lanes.length === 1 ? 'lane' : 'lanes'} · open a lane to count
              </span>
            </>
          ) : (
            <>
              <span className="tl-x" aria-busy={busy}>
                <b>{fenced}</b> {fenced === 1 ? 'generation' : 'generations'} fenced
              </span>
              <span className="tl-x" aria-busy={busy}>
                <b>{parks}</b> {parks === 1 ? 'park' : 'parks'}, {still} still parked
              </span>
            </>
          )}
          {link}
        </p>
      )}
      {strip.status !== 'ok' && strip.status !== 'loading' && link}
      {/* WHAT IT COUNTS, IN THE READER'S WORDS (walkthrough E): where the
          counts come from is the Outcomes help topic, one click away. */}
      <p className="tl-q">
        {strip.status === 'ok' && own !== null ? (
          <>
            The lanes cover <span className="tl-lanes-range">{rangeWords(reach ?? since, until)}</span>
            {reach !== null && ', the newest attempts of the span, with a next page unread'}. Parks and fences count only the lanes drawn.{' '}
          </>
        ) : strip.status === 'ok' ? (
          <>
            Decided counts cover <span className="tl-strip-range">{ledgerRange(strip.data)}</span>
            {aligned !== null && `, ${aligned} as on Outcomes`}; the lanes cover{' '}
            <span className="tl-lanes-range">{rangeWords(reach ?? since, until)}</span>
            {reach !== null && ', the newest attempts of the span, with a next page unread'}. Parks and fences count only the lanes drawn.{' '}
          </>
        ) : (
          <>Decided counts cover {rangeWords(since, until)}; parks and fences count only the lanes drawn. </>
        )}
        <a className="ctl-link" href={`#${HELP['outcome-buckets'].anchor}`}>How these are counted</a>
      </p>
    </div>
  )
}

// ---------------------------------------------------------------------------
// The open lane: summary and newest events, a sheet on a phone
// ---------------------------------------------------------------------------

function LaneDetail({
  lane,
  events,
  onClose,
  onOlder,
  onRead,
  workflowName = null,
}: {
  lane: Lane
  events: EventsRead | null
  onClose: () => void
  onOlder: () => void
  onRead: () => void
  /** The lane's workflow by its label (`workflowLabel`), null when it has none or was not read. */
  workflowName?: string | null
}) {
  useEffect(() => {
    if (!lane.neverRan) onRead()
  }, [lane.taskId, lane.neverRan, onRead])
  // ESCAPE CLOSES IT (live QA RT-23, 2026-10-11): only its × did.
  useEscapeLayer(true, onClose)
  const t = lane.task
  const name = t !== null ? laneName(t) : lane.taskId
  const href = `/agents/recent/${encodeURIComponent(lane.taskId)}/attempts`
  const costs = lane.attempts.map((a) => a.cost_usd)
  const reported = costs.filter((c): c is number => c !== null)
  const created = t !== null ? Date.parse(t.created_at) : null
  const ended = t?.completed_at ? Date.parse(t.completed_at) : null
  return (
    <section className="tl-detail" aria-label={`${name} (${lane.taskId})`}>
      <div className="tl-card">
        <div className="tl-card-head">
          <h2>{name}</h2>
          {t !== null ? <StateMark state={t.state} /> : <span className="tl-q">state — agent not read</span>}
          <button type="button" className="tl-close" aria-label="Close" onClick={onClose}>
            ×
          </button>
        </div>
        {/* BY NAME, THE GENERATION ONCE (visual QA pass 1, runs-submit-38):
            the line printed `task_fan_0` under its own name and the workflow
            as `wf_5e5ad3b6f7da4299a839`. The ids are the line's title, for a
            reader who needs them; a workflow with no label is said as one. */}
        <p className="tl-q" title={t?.workflow_id ? `${lane.taskId} · ${t.workflow_id}` : lane.taskId}>
          {t === null
            ? 'agent not read'
            : `${t.runner_profile} · ${t.resource_class} · generation ${t.current_generation}${
                t.workflow_id ? ` · workflow ${workflowName ?? 'with no name'}` : ''
              }`}
        </p>
        <dl className="tl-kv">
          <dt>Attempts</dt>
          <dd>
            {lane.attempts.length === 0
              ? 'none: this step never ran'
              : lane.segs
                  .filter((s) => s.kind === 'hold' || s.kind === 'cut')
                  .map((s) => `${s.label}: ${clock(s.from, true)} → ${s.open ? 'running' : s.endKnown ? `${clock(s.to, true)} · ${formatDuration(s.to - s.from)}` : '— no completed_at: worker killed'}`)
                  .join('; ')}
          </dd>
          {lane.segs.some((s) => s.kind === 'cut') && (
            <>
              <dt>Fenced</dt>
              <dd>A stale generation exits without running the agent and without touching the lease (invariant 5).</dd>
            </>
          )}
          <dt>Parked</dt>
          <dd>
            {!lane.eventsRead && !lane.neverRan
              ? '— needs the events page'
              : (() => {
                  const ps = lane.segs.filter((s) => s.kind === 'park' && !s.future)
                  return ps.length === 0
                    ? 'never, in the events read'
                    : ps.map((p) => `${clock(p.from, true)} → ${p.open ? 'now' : clock(p.to, true)}${p.reason ? ` · ${p.reason}` : ''}`).join('; ') + ' · free, no capacity held'
                })()}
          </dd>
          <dt>Wall time</dt>
          <dd>
            {created === null
              ? '— agent not read'
              : ended === null
                ? `${clock(created, true)} → not finished`
                : `${clock(created, true)} → ${clock(ended, true)} · ${formatDuration(ended - created)}`}
          </dd>
          <dt>Cost</dt>
          <dd>
            {lane.attempts.length === 0
              ? '— no attempt'
              : reported.length === 0
                ? '— not reported by any attempt read'
                : `$${reported.reduce((a, b) => a + b, 0).toFixed(2)} · reported by ${reported.length} of ${costs.length} attempts`}
          </dd>
        </dl>
        <p className="tl-acts">
          <ButtonLink kind="primary" href={href}>
            Open in Agents ›
          </ButtonLink>
        </p>
      </div>
      <div className="tl-card">
        <div className="tl-card-head">
          <h2>Events</h2>
          <span className="tl-q">
            {events?.status === 'ok'
              ? `${events.events.length} read · newest first · ${events.next === null ? 'all read' : 'older events exist: — the route returns a page token, not a total'}`
              : ''}
          </span>
        </div>
        {lane.neverRan ? (
          <p className="tl-q">No attempt, so no run events to read.</p>
        ) : events === null || events.status === 'reading' ? (
          <p className="tl-q">Reading this lane&rsquo;s newest events…</p>
        ) : events.status === 'error' ? (
          <p className="tl-q">{`Events not read: the read answered ${events.httpStatus ?? 'nothing'} (${events.message}).`}</p>
        ) : events.events.length === 0 ? (
          <p className="tl-q">This agent has no events.</p>
        ) : (
          <ol className="tl-evl">
            {events.events.map((e) => (
              <EventRow key={e.event_id} e={e} current={t?.current_generation ?? null} />
            ))}
          </ol>
        )}
        {events?.status === 'ok' && events.next !== null && (
          <Button onClick={onOlder} disabled={events.older === 'reading'}>
            {events.older === 'reading' ? 'Reading…' : 'Older events ›'}
          </Button>
        )}
        {events?.status === 'ok' && typeof events.older === 'string' && events.older !== 'idle' && events.older !== 'reading' && (
          <p className="tl-q">{events.older}</p>
        )}
      </div>
    </section>
  )
}

const EVENT_MARK: Record<string, { mark: MarkName | 'warn'; hue: string }> = {
  succeeded: { mark: 'succeeded', hue: 'neu' },
  failed: { mark: 'failed', hue: 'bad' },
  dead_lettered: { mark: 'dead', hue: 'bad' },
  cancelled: { mark: 'cancelled', hue: 'neu' },
  cancel_requested: { mark: 'warn', hue: 'warn' },
  generation_fenced: { mark: 'warn', hue: 'warn' },
  parked: { mark: 'parked', hue: 'park' },
  running: { mark: 'running', hue: 'live' },
  lease_acquired: { mark: 'starting', hue: 'live' },
  dispatched: { mark: 'starting', hue: 'live' },
  starting: { mark: 'starting', hue: 'live' },
  ready: { mark: 'ready', hue: 'neu' },
  queued: { mark: 'queued', hue: 'neu' },
  submitted: { mark: 'queued', hue: 'neu' },
}

/**
 * One event: its time, its mark, and its kind IN WORDS (`eventWord`, as
 * Agents' events say it; visual QA pass 1, runs-submit-38: the rows printed
 * `checkpoint_completed`). Its generation only where it is not the one the
 * panel's header names, so an older attempt's events still say whose they are.
 */
function EventRow({ e, current }: { e: TaskEvent; current: number | null }) {
  const kind = eventKind(e)
  const m = EVENT_MARK[kind] ?? { mark: 'queued' as const, hue: 'neu' }
  const t = Date.parse(e.at)
  const fence =
    kind === 'generation_fenced' && typeof e.detail?.['expected_generation'] === 'number'
      ? ` · gen ${String(e.detail['expected_generation'])} → ${String(e.detail['observed_generation'] ?? '—')}`
      : ''
  const reason = kind === 'parked' && typeof e.detail?.['reason'] === 'string' ? ` · ${e.detail['reason']}` : ''
  return (
    <li className={kind === 'generation_fenced' ? 'tl-evr is-fence' : 'tl-evr'}>
      <span className="tl-evt">{Number.isFinite(t) ? clock(t, true) : '—'}</span>
      <span className={`tl-mk is-${m.hue}`}>
        <MarkIcon mark={m.mark} />
      </span>
      <span className="tl-evk">
        {eventWord(e)}
        {fence}
        {reason}
      </span>
      {e.generation !== null && e.generation !== current && <span className="tl-evg">{`gen ${e.generation}`}</span>}
    </li>
  )
}
