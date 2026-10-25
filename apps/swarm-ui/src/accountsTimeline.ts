/**
 * CAPACITY › ACCOUNTS, THE TIMELINE VIEW (#2222; BGD 2026-10-24 17:33Z:
 * proposal A with a Table / Timeline toggle whose Timeline draws every
 * account the way proposal B does, docs/web-ui/mockups/accounts-b.html).
 *
 * The pure half of the Timeline: where an instant sits on the axis, each
 * window as a band against its floor, how many accounts can take an agent
 * over the week, where the next free slot is, and the pool row "at this
 * pace". No React and no DOM, so every rule here is tested without a render.
 *
 * NOTHING HERE RE-DERIVES THE BROKER'S RULE. Whether an account can take an
 * agent, what blocks it and when it clears are `accountsPool.ts`'s readings
 * of `GET /v1/accounts` (`canTake`, `groupOf`, `clockOf`), the same ones the
 * Table ranks by; the "at this pace" row is `forecastOf`'s spans, the same
 * ones the forecast tiles read. Two views of one page must never disagree.
 */
import { accountName } from './accountName'
import {
  FLOORS,
  DEFAULT_FLOOR,
  blocksOf,
  clockOf,
  closedSpans,
  floorOf,
  groupOf,
  isDuplicateBlock,
  isWindowBlock,
  unknownWindows,
  windowName,
  type Forecast,
  type PoolAccount,
} from './accountsPool'
import { FIVE_HOUR, SEVEN_DAY, readingOf } from './types'

const HOUR_MS = 3_600_000
const DAY_MS = 24 * HOUR_MS

// ---------------------------------------------------------------------------
// The split axis
// ---------------------------------------------------------------------------

/**
 * THE AXIS IS SPLIT (proposal B, "Risks"): a 5-hour window is invisible on a
 * 7-day axis, so the first NEAR_SHARE of a track is the next NEAR_HOURS and
 * the rest is the week. The break is drawn and both zones are labelled,
 * because a split axis read as a plain one misplaces every instant after it.
 */
export const NEAR_HOURS = 12
export const SPAN_HOURS = 7 * 24
/** The share of the track the next 12 hours take: B's 26%. */
export const NEAR_SHARE = 0.26

/** Where `at` sits on the track, 0-100 (percent), clamped to the axis. */
export function axisX(at: number, now: number): number {
  const h = (at - now) / HOUR_MS
  if (!(h > 0)) return 0
  if (h <= NEAR_HOURS) return (h / NEAR_HOURS) * NEAR_SHARE * 100
  return Math.min(100, (NEAR_SHARE + ((h - NEAR_HOURS) / (SPAN_HOURS - NEAR_HOURS)) * (1 - NEAR_SHARE)) * 100)
}

/** The axis' far end: seven days out. */
export function axisEnd(now: number): number {
  return now + SPAN_HOURS * HOUR_MS
}

const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']

/** `09:04Z`. */
export function clockZ(t: number): string {
  const d = new Date(t)
  return `${String(d.getUTCHours()).padStart(2, '0')}:${String(d.getUTCMinutes()).padStart(2, '0')}Z`
}

/** `Sun 25`: a day on the axis, in UTC like every time on the page. */
export function dayLabel(t: number): string {
  const d = new Date(t)
  return `${DAYS[d.getUTCDay()]} ${d.getUTCDate()}`
}

export interface Tick {
  /** Percent along the track. */
  x: number
  label: string
  /** `now`: bold; `minor`: dropped on a phone; `before`: the label sits left of its line. */
  kind: 'now' | 'major' | 'minor' | 'before'
}

/**
 * The ticks: now; every three hours on the clock inside the next 12 hours
 * (minor, so a phone drops them); a midnight inside the next 12 hours with
 * its label LEFT of its line, so it never meets the break; then each midnight
 * of the week, every other one minor. Nothing past 93%, where a label would
 * run off the track.
 */
export function axisTicks(now: number): Tick[] {
  const out: Tick[] = [{ x: 0, label: `now ${clockZ(now)}`, kind: 'now' }]
  const firstMidnight = new Date(now)
  firstMidnight.setUTCHours(24, 0, 0, 0)
  const nearMidnight = firstMidnight.getTime() < now + NEAR_HOURS * HOUR_MS ? firstMidnight.getTime() : null
  const step = 3 * HOUR_MS
  for (let t = Math.ceil(now / step) * step; t < now + (NEAR_HOURS - 1.5) * HOUR_MS; t += step) {
    const x = axisX(t, now)
    const nearIt = nearMidnight !== null && nearMidnight - t >= 0 && nearMidnight - t <= 3.5 * HOUR_MS
    if (x > 11 && x < NEAR_SHARE * 100 - 5 && !nearIt) out.push({ x, label: clockZ(t), kind: 'minor' })
  }
  if (nearMidnight !== null) out.push({ x: axisX(nearMidnight, now), label: dayLabel(nearMidnight), kind: 'before' })
  const d = new Date(now + NEAR_HOURS * HOUR_MS)
  d.setUTCHours(24, 0, 0, 0)
  for (let t = d.getTime(), j = 0; t < axisEnd(now) - 3 * HOUR_MS; t += DAY_MS, j++) {
    const x = axisX(t, now)
    if (x > 93) break
    out.push({ x, label: dayLabel(t), kind: j % 2 === 1 ? 'minor' : 'major' })
  }
  return out
}

// ---------------------------------------------------------------------------
// A window as a band
// ---------------------------------------------------------------------------

export interface Band {
  key: string
  /** `5-hour`, `7-day`, or the provider's own name. */
  name: string
  /** One of the two windows this console knows by name. */
  known: boolean
  /**
   * `pending`: the account was never measured, so no band (never 0%, never
   * 100%). `absent`: measured, but the provider serves no such window.
   * `reset`: past its reset, so full, with no use read since. `reading`: a
   * figure.
   */
  kind: 'pending' | 'absent' | 'reset' | 'reading'
  /** The floor it is held to: the broker's own when it served one. */
  floor: number
  /** Share left now, 0-1 (a reading only). */
  left?: number
  /** Share used, 0-1 (a reading only). */
  used?: number
  /** Under its floor: the broker said so, or (an older broker) the rule says so. */
  blocked: boolean
  /** The reading is older than 30 min, so the broker counts it at half. */
  aged: boolean
  /** When the window resets (a reading or a reset), epoch ms. */
  resetsAt?: number
  /** Where it resets on the track, 0-100. */
  x?: number
}

/**
 * The account's windows, 7-day first (the one a week turns on), then 5-hour,
 * then any the console does not know by name -- drawn, never dropped.
 */
export function bandsOf(a: PoolAccount, now: number): Band[] {
  const keys = [SEVEN_DAY, FIVE_HOUR, ...unknownWindows(a)]
  // A broker that serves `blocked_by` has said which windows block; trust it.
  const served = Array.isArray(a.blocked_by)
  return keys.map((key): Band => {
    const base = { key, name: windowName(key), known: key === FIVE_HOUR || key === SEVEN_DAY, floor: floorOf(a, key) }
    const r = readingOf(a, key)
    if (r.kind === 'never') return { ...base, kind: 'pending', blocked: false, aged: false }
    if (r.kind === 'absent') return { ...base, kind: 'absent', blocked: false, aged: false }
    const resetsAt = Date.parse(r.resetsAt)
    const x = Number.isFinite(resetsAt) ? axisX(resetsAt, now) : undefined
    if (r.kind === 'reset') return { ...base, kind: 'reset', blocked: false, aged: false, resetsAt, x }
    const used = r.pct / 100
    const left = 1 - used
    const aged = r.kind === 'stale'
    const blocked = served
      ? blocksOf(a).some((b) => isWindowBlock(b) && b.window === key)
      : left * (aged ? 0.5 : 1) < (FLOORS[key] ?? DEFAULT_FLOOR)
    return { ...base, kind: 'reading', used, left, blocked, aged, resetsAt, x }
  })
}

/**
 * Band height: the share left on a SQUARE-ROOT scale, so a 5% floor is a
 * visible line and not one pixel (B, "Risks"). Every band also writes its
 * figure in words, because a square-root scale is not one anyone reads off.
 */
export function bandHeight(share: number): number {
  return Math.sqrt(Math.max(0, Math.min(1, share))) * 100
}

// ---------------------------------------------------------------------------
// The pool over the week
// ---------------------------------------------------------------------------

export interface Segment {
  from: number
  to: number
  /** How many accounts can take an agent from `from` to `to`. */
  open: number
}

/** Cut [now, end) at `cuts`, counting `openAt(t)` over each piece and merging equal neighbours. */
function segmentsOf(now: number, cuts: readonly number[], openAt: (t: number) => number): Segment[] {
  const end = axisEnd(now)
  const points = [...new Set([now, ...cuts.filter((t) => t > now && t < end), end])].sort((x, y) => x - y)
  const out: Segment[] = []
  for (let i = 0; i + 1 < points.length; i++) {
    const from = points[i]!
    const open = openAt(from)
    const last = out[out.length - 1]
    if (last !== undefined && last.open === open) last.to = points[i + 1]!
    else out.push({ from, to: points[i + 1]!, open })
  }
  return out
}

/** When an account can take an agent by the clock: now, its clock, or never (full, held by a person, unknown). */
function opensAt(a: PoolAccount, scope: string | null, now: number): number | null {
  const g = groupOf(a, scope)
  if (g === 'now') return now
  if (g !== 'clock') return null
  const at = clockOf(a)
  return at === null ? null : Math.max(now, at)
}

/**
 * THE POOL ROW: how many accounts can take an agent over the week, BY THE
 * CLOCK ONLY and if nothing more is used. A full account frees when an agent
 * finishes, which no clock says, so it is never counted in; nor is one a
 * person holds.
 */
export function openRow(accounts: readonly PoolAccount[], scope: string | null, now: number): Segment[] {
  const at = accounts.map((a) => opensAt(a, scope, now)).filter((t): t is number => t !== null)
  return segmentsOf(now, at, (t) => at.filter((x) => x <= t).length)
}

/** The next free slot: now when any account can take an agent, else the first clock; null when none says. */
export function nextSlotAt(accounts: readonly PoolAccount[], scope: string | null, now: number): number | null {
  const at = accounts.map((a) => opensAt(a, scope, now)).filter((t): t is number => t !== null)
  return at.length === 0 ? null : Math.min(...at)
}

/**
 * THE POOL ROW AT THIS PACE, from `forecastOf` and nothing else: the 7-day
 * windows each subscription closes and reopens at its own pace, counted over
 * the week. Null when there is no pace to draw (no subscription measured, or
 * one that can take agents was never measured): a row drawn then would be a
 * guess.
 */
export function paceRow(f: Forecast, now: number): Segment[] | null {
  if (f.paces.length === 0 || f.unmeasured.length > 0) return null
  const spans = f.paces.map((p) => closedSpans(p, now))
  const cuts = spans.flat().flat()
  return segmentsOf(now, cuts, (t) => spans.filter((s) => !s.some(([a, b]) => t >= a && t < b)).length)
}

/**
 * Where one account's 7-day window reaches its floor at this pace, before it
 * resets: the dotted line on its band. Null when it does not, or no pace.
 */
export function paceCloseAt(f: Forecast, a: PoolAccount, now: number): { at: number; from: number; to: number } | null {
  const p = f.paces.find((x) => x.account.account_id === a.account_id)
  if (p === undefined || p.perHour <= 0 || p.above <= 0) return null
  const at = now + (p.above / p.perHour) * HOUR_MS
  if (at >= p.resetsAt) return null
  return { at, from: p.above + p.floor, to: p.floor }
}

// ---------------------------------------------------------------------------
// One subscription registered twice
// ---------------------------------------------------------------------------

export interface DupMark {
  /** `withheld`: this registration is not handed out; `handed`: the other is withheld; `possible`: identical readings. */
  kind: 'withheld' | 'handed' | 'possible'
  /** The other registration's name, ONLY when it is this workspace's own; null otherwise. */
  other: string | null
}

/**
 * The duplicate flag on a lane. ANOTHER WORKSPACE'S ACCOUNT IS NEVER NAMED
 * (BGD, POOL-DUP-DETECT Q2, 2026-10-24 17:33Z): the other side is named only
 * when it is in this read AND owned by the same workspace as this one.
 */
export function dupMark(a: PoolAccount, accounts: readonly PoolAccount[]): DupMark | null {
  const nameOf = (id: string | null | undefined): string | null => {
    if (!id) return null
    const o = accounts.find((x) => x.account_id === id)
    return o !== undefined && o.owner_tenant === a.owner_tenant ? accountName(o) : null
  }
  const withheld = blocksOf(a).find(isDuplicateBlock)
  if (withheld !== undefined) return { kind: 'withheld', other: nameOf(withheld.duplicate_of ?? a.duplicate_of) }
  const twin = accounts.find((o) =>
    blocksOf(o).some((b) => isDuplicateBlock(b) && (b.duplicate_of ?? o.duplicate_of) === a.account_id),
  )
  if (twin !== undefined) return { kind: 'handed', other: nameOf(twin.account_id) }
  if (a.possible_duplicate) return { kind: 'possible', other: nameOf(a.possible_duplicate.of) }
  const maybe = accounts.find((o) => o.possible_duplicate?.of === a.account_id)
  if (maybe !== undefined) return { kind: 'possible', other: nameOf(maybe.account_id) }
  return null
}
