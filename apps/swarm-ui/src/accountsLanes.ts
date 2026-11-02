/**
 * CAPACITY › ACCOUNTS, THE TIMELINE VIEW (#2222, ACCOUNTS-TIMELINE; BGD
 * 2026-10-24 17:33Z: proposal A with a Table / Timeline toggle whose Timeline
 * draws every account the way proposal B does).
 *
 * The pure half of the Timeline: which view the page opens on, where an
 * instant sits on the shared axis, how many accounts can take an agent over
 * time (by windows only, and at this pace), how the agents waiting on the pool
 * queue against the free slots, and what each window's band is. No React and
 * no DOM, so every rule is tested without a render.
 *
 * NOTHING HERE RE-DERIVES THE BROKER'S RULE. Who can take an agent now, what
 * blocks the rest and when that clears are `accountsPool.ts`'s readings of
 * `can_take_new_agent`, `blocked_by` and `next_eligible_at`, the same
 * functions the Table ranks by; the pace is `forecastOf`'s, the same function
 * the Table's forecast says. A hold's deadline is the broker's own
 * `latest_hold_deadline` (`holdDeadlineOf`), never one computed here.
 */
import {
  blocksOf,
  clockOf,
  closedSpans,
  floorOf,
  freeSlots,
  isDuplicateBlock,
  isWindowBlock,
  rankAccounts,
  type Forecast,
  type PoolAccount,
} from './accountsPool'
import { readingOf } from './types'

const HOUR_MS = 3_600_000
const DAY_MS = 24 * HOUR_MS
/** The axis runs from now to seven days out: the longest window there is. */
export const WEEK_MS = 7 * DAY_MS
/** The near part of B's split axis. */
export const NEAR_MS = 12 * HOUR_MS
/**
 * Where the split axis turns from hours to days. Proposal B gives the next 12
 * hours a little over a quarter of the track: enough that a 5-hour reset an
 * hour out is not drawn on top of "now", while the week still reads.
 */
export const SPLIT_AT = 0.28

// ---------------------------------------------------------------------------
// The view: Table by default, remembered (BGD, "Table default, remembered")
// ---------------------------------------------------------------------------

export type AccountsView = 'table' | 'timeline'
/** The browser's memory of the last choice (`swarm.<screen>.<thing>`, as RepoGraph's drawing). */
export const VIEW_KEY = 'swarm.accounts.view'

/**
 * The view to draw. The address wins, because a followed link means what it
 * says (`?view=timeline`); then the browser's last choice; then Table.
 */
export function viewOf(param: string | null, stored: string | null): AccountsView {
  if (param === 'timeline' || param === 'table') return param
  return stored === 'timeline' ? 'timeline' : 'table'
}

// ---------------------------------------------------------------------------
// The axis
// ---------------------------------------------------------------------------

/** B's three axes: the split one (12 hours, then the week), the 12 hours alone, the week alone. */
export type AxisKind = 'split' | '12h' | '7d'

/** Where an instant sits on the track, 0 (now) to 1 (the axis' end), clamped. */
export function xOf(t: number, now: number, axis: AxisKind): number {
  const d = t - now
  if (!Number.isFinite(d) || d <= 0) return 0
  if (axis === '12h') return Math.min(1, d / NEAR_MS)
  if (axis === '7d') return Math.min(1, d / WEEK_MS)
  if (d <= NEAR_MS) return (d / NEAR_MS) * SPLIT_AT
  return Math.min(1, SPLIT_AT + ((d - NEAR_MS) / (WEEK_MS - NEAR_MS)) * (1 - SPLIT_AT))
}

/** The last instant the axis shows. */
export function axisEnd(now: number, axis: AxisKind): number {
  return now + (axis === '12h' ? NEAR_MS : WEEK_MS)
}

/** Whether an instant is on the axis at all (a reset past its end is said in words, not drawn). */
export function onAxis(t: number, now: number, axis: AxisKind): boolean {
  return Number.isFinite(t) && t >= now && t <= axisEnd(now, axis)
}

export interface Tick {
  at: number
  x: number
  label: string
}

const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']

/** `Sun 25`: a UTC day, the way the week's ticks read. */
export function dayLabel(t: number): string {
  const d = new Date(t)
  return `${WEEKDAYS[d.getUTCDay()]} ${d.getUTCDate()}`
}

function hourLabel(t: number): string {
  const d = new Date(t)
  return d.getUTCHours() === 0 ? dayLabel(t) : `${String(d.getUTCHours()).padStart(2, '0')}:00Z`
}

/**
 * The axis' labelled ticks. Candidates are every few UTC hours in the near
 * part and every UTC midnight in the week; a label is kept only when it
 * clears the one before it, "now" at the left and the split mark, by `gap`
 * of the track -- so a label is never drawn over another at any width. The
 * phone's gap is wider and its candidates sparser (B1p: no hour ticks, every
 * other day).
 */
export function ticksOf(now: number, axis: AxisKind, phone: boolean): Tick[] {
  const gap = phone ? 0.16 : 0.06
  const nowGap = phone ? 0.26 : 0.1
  const cands: number[] = []
  const firstHour = Math.ceil(now / HOUR_MS) * HOUR_MS
  const firstDay = Math.ceil(now / DAY_MS) * DAY_MS
  if (axis === '12h') {
    const step = phone ? 3 : 2
    for (let t = firstHour; t < now + NEAR_MS; t += HOUR_MS) if (new Date(t).getUTCHours() % step === 0) cands.push(t)
  } else {
    if (axis === 'split' && !phone) {
      for (let t = firstHour; t < now + NEAR_MS; t += HOUR_MS) if (new Date(t).getUTCHours() % 3 === 0) cands.push(t)
    }
    const every = phone ? 2 : 1
    let i = 0
    for (let t = firstDay; t < now + WEEK_MS; t += DAY_MS) {
      if (axis === 'split' && t <= now + NEAR_MS) continue
      if (i++ % every === 0) cands.push(t)
    }
  }
  const out: Tick[] = []
  let last = -Infinity
  for (const at of cands) {
    const x = xOf(at, now, axis)
    if (x < nowGap || x > 1 - gap) continue
    if (axis === 'split' && Math.abs(x - SPLIT_AT) < gap) continue
    if (x - last < gap) continue
    out.push({
      at,
      x,
      label: axis === '12h' ? hourLabel(at) : at - now <= NEAR_MS && axis === 'split' ? hourLabel(at) : dayLabel(at),
    })
    last = x
  }
  return out
}

// ---------------------------------------------------------------------------
// How many can take an agent, over time
// ---------------------------------------------------------------------------

/** From `at` until the next step, `count` accounts can take an agent. */
export interface Step {
  at: number
  count: number
}

/**
 * When each account can take an agent BY ITS WINDOWS, if nothing more is used:
 * now for the ones that can, at their clock for the ones a window holds, never
 * (absent) for a full account, a person's hold, one the broker says nothing
 * about, or a clock already past. Read off `rankAccounts`, the Table's own groups.
 */
function opensAt(accounts: readonly PoolAccount[], scope: string | null, now: number): Map<PoolAccount, number> {
  const out = new Map<PoolAccount, number>()
  for (const g of rankAccounts(accounts, scope)) {
    for (const a of g.accounts) {
      if (g.key === 'now') out.set(a, now)
      else if (g.key === 'clock') {
        // A clock already past says "at the broker's next check", which no
        // axis can place: it is left out rather than drawn at now, where it
        // would make the first step disagree with the verdict's N.
        const at = clockOf(a)
        if (at !== null && at > now) out.set(a, at)
      }
    }
  }
  return out
}

function stepsFrom(times: number[], countAt: (t: number) => number, now: number, end: number): Step[] {
  const at = [...new Set([now, ...times.filter((t) => t > now && t < end)])].sort((x, y) => x - y)
  const out: Step[] = []
  for (const t of at) {
    const count = countAt(t)
    if (out.length > 0 && out[out.length - 1]!.count === count) continue
    out.push({ at: t, count })
  }
  return out
}

/**
 * THE POOL ROW: how many accounts can take an agent from each instant, by
 * windows only. Its first step is the verdict's N, because both are
 * `rankAccounts`' "Can take now".
 */
export function poolSteps(
  accounts: readonly PoolAccount[],
  scope: string | null,
  now: number,
  end: number = now + WEEK_MS,
): Step[] {
  const open = opensAt(accounts, scope, now)
  const times = [...open.values()]
  return stepsFrom(times, (t) => [...open.values()].filter((o) => o <= t).length, now, end)
}

/**
 * THE PACE ROW: the pool row with each subscription's 7-day window spent at
 * this pace -- the same `forecastOf` paces and closed stretches the Table's
 * forecast says when the pool goes dark from. Null when the forecast cannot be
 * taken (no pace, or an account with no 7-day reading to take one from: the
 * Table's forecast says so in the same words).
 */
export function paceSteps(
  accounts: readonly PoolAccount[],
  scope: string | null,
  now: number,
  forecast: Forecast,
  end: number = now + WEEK_MS,
): Step[] | null {
  if (forecast.paces.length === 0 || forecast.unmeasured.length > 0) return null
  const open = opensAt(accounts, scope, now)
  const spans = new Map(forecast.paces.map((p) => [p.account, closedSpans(p, now)]))
  const times = [...open.values(), ...[...spans.values()].flat().flat()]
  const countAt = (t: number) =>
    [...open.entries()].filter(([a, o]) => o <= t && !(spans.get(a) ?? []).some(([s, e]) => t >= s && t < e)).length
  return stepsFrom(times, countAt, now, end)
}

/** Where the next free slot is: now when any account can take an agent, else the first clock. */
export function nextFreeAt(accounts: readonly PoolAccount[], scope: string | null, now: number): number | null {
  const steps = poolSteps(accounts, scope, now, Infinity)
  const first = steps.find((s) => s.count > 0)
  return first === undefined ? null : first.at
}

// ---------------------------------------------------------------------------
// The waiting agents, queued against the free slots
// ---------------------------------------------------------------------------

export interface QueueSlot {
  account: PoolAccount
  n: number
}
export interface Queue {
  /** Start now, on the accounts that can take one. */
  now: QueueSlot[]
  /** Start at a clock, each on the account that clears then, soonest first. */
  later: Array<QueueSlot & { at: number }>
  /** Wait for an agent to finish: no clock frees a slot for them. */
  rest: number
}

/**
 * The waiting agents laid against the free slots, oldest first: the open
 * accounts' slots now, then each clocked account's slots at its clock, the
 * rest for an agent to finish. COMPUTED from the readings and the caps, not
 * observed: admission and another ceiling can start fewer, and the row says so.
 */
export function queueOf(accounts: readonly PoolAccount[], scope: string | null, waiting: number, now: number): Queue {
  let left = Math.max(0, waiting)
  const q: Queue = { now: [], later: [], rest: 0 }
  for (const g of rankAccounts(accounts, scope)) {
    if (g.key !== 'now' && g.key !== 'clock') continue
    for (const a of g.accounts) {
      if (left === 0) break
      const n = Math.min(left, freeSlots(a))
      if (n === 0) continue
      left -= n
      if (g.key === 'now') q.now.push({ account: a, n })
      else {
        // A clock already past is the broker's next check: queued there, at
        // now, and said so, never at a time that has gone.
        const at = clockOf(a)
        if (at !== null) q.later.push({ account: a, n, at: Math.max(now, at) })
        else left += n
      }
    }
  }
  q.rest = left
  return q
}

// ---------------------------------------------------------------------------
// One window's band
// ---------------------------------------------------------------------------

export type BandKind = 'live' | 'stale' | 'reset' | 'never' | 'absent'
export interface Band {
  key: string
  kind: BandKind
  /** Share left as read, 0-1 (live and stale). */
  left: number | null
  /** Share left as the broker counts it: an aged reading at half. */
  counted: number | null
  floor: number
  resetsAt: number | null
  /** The broker names this window in `blocked_by`: it is under its floor. */
  blocks: boolean
}

/** One window, as the band draws it. A reset window has no figure: it is full, nothing read since. */
export function bandOf(a: PoolAccount, key: string): Band {
  const r = readingOf(a, key)
  const floor = floorOf(a, key)
  const blocks = blocksOf(a).some((b) => isWindowBlock(b) && b.window === key)
  if (r.kind === 'never' || r.kind === 'absent')
    return {
      key,
      kind: r.kind,
      left: null,
      counted: null,
      floor,
      resetsAt: null,
      blocks,
    }
  const resetsAt = Date.parse(r.resetsAt)
  const at = Number.isFinite(resetsAt) ? resetsAt : null
  if (r.kind === 'reset')
    return {
      key,
      kind: 'reset',
      left: null,
      counted: null,
      floor,
      resetsAt: at,
      blocks,
    }
  const left = Math.max(0, 1 - r.pct / 100)
  return {
    key,
    kind: r.kind,
    left,
    counted: r.kind === 'stale' ? left / 2 : left,
    floor,
    resetsAt: at,
    blocks,
  }
}

/**
 * A band's height for a share, 0-1 of the band's full height. Square root, as
 * B draws it: a 5% floor and a 2% reading are a hair apart on a linear scale
 * and the whole question is which side of the floor the reading is on.
 */
export function heightOf(share: number): number {
  return Math.sqrt(Math.max(0, Math.min(1, share)))
}

/**
 * The 7-day share left at this pace from now to the floor, as points
 * `[t, share]` -- the dotted forecast line on a lane. Empty when the window
 * has no pace or does not reach its floor before it resets.
 */
export function paceLine(forecast: Forecast, a: PoolAccount, now: number, samples = 16): Array<[number, number]> {
  const p = forecast.paces.find((x) => x.account === a)
  if (p === undefined || p.perHour <= 0 || p.above <= 0) return []
  const start = p.above + p.floor
  const until = Math.min(p.resetsAt, now + (p.above / p.perHour) * HOUR_MS)
  const out: Array<[number, number]> = []
  for (let i = 0; i <= samples; i++) {
    const t = now + ((until - now) * i) / samples
    out.push([t, Math.max(p.floor, start - (p.perHour * (t - now)) / HOUR_MS)])
  }
  return out
}

// ---------------------------------------------------------------------------
// A full lane's holds, and the kept half of a duplicate pair
// ---------------------------------------------------------------------------

/**
 * A listing row as the Timeline reads it: the broker's `latest_hold_deadline`
 * (#2222, B3/B4), the latest `expires_at` among the agents holding it now.
 * Absent from a broker older than the field; null when it holds none.
 */
export type LanedAccount = PoolAccount & { latest_hold_deadline?: string | null }

/**
 * THE LATEST HOLD DEADLINE, OR WHY THERE IS NONE. The instant the last of its
 * agents can still hold a slot, drawn as "latest", never "expected": an agent
 * that finishes sooner frees its slot sooner, and nothing says when. A
 * deadline the broker does not serve is a dash with its reason, never a time
 * this page made up.
 */
export function holdDeadlineOf(
  a: LanedAccount,
  now: number,
): { at: number; reason: null } | { at: null; reason: string } {
  if (!('latest_hold_deadline' in a) || a.latest_hold_deadline === undefined)
    // PLAIN WORDS (QA2-FIX-14, capacity-21): `not served by this broker`.
    return { at: null, reason: 'not reported' }
  if (a.latest_hold_deadline === null) return { at: null, reason: 'the broker read no live hold' }
  const at = Date.parse(a.latest_hold_deadline)
  if (!Number.isFinite(at)) return { at: null, reason: 'not an instant the broker served' }
  // A deadline already behind the clock has stopped counting; the next read drops it.
  if (at <= now) return { at: null, reason: 'passed; the next read drops it' }
  return { at, reason: null }
}

/**
 * THE KEPT HALF OF A DUPLICATE PAIR (#2222, mock-up B's "older, handed out"):
 * the registration the broker keeps handing out while it withholds the later
 * one. The broker names the pair on both rows (`duplicate_of`, or only
 * `duplicate_reason` when the other is another workspace's) and puts only the
 * withheld one in `blocked_by`; a row another account's withheld block points
 * at is kept too, for a broker that names the pair on one row only. Returns
 * the other half's name, or null when it is not in this read -- another
 * workspace's account is never named (BGD, Q2) -- and undefined when this
 * account is not a kept half.
 */
export function keptOf(
  a: PoolAccount,
  accounts: readonly PoolAccount[],
  names: ReadonlyMap<string, string>,
): string | null | undefined {
  if (blocksOf(a).some(isDuplicateBlock)) return undefined
  const twin = accounts.find(
    (o) => o.account_id !== a.account_id && blocksOf(o).some((b) => isDuplicateBlock(b) && b.duplicate_of === a.account_id),
  )
  if (twin !== undefined) return names.get(twin.account_id) ?? null
  if (a.duplicate_of) return names.get(a.duplicate_of) ?? null
  if (a.duplicate_reason) return null
  return undefined
}
