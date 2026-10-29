/**
 * CAPACITY › ACCOUNTS, ANSWER FIRST (#2222, proposal A, BGD 2026-10-24).
 *
 * The pure half of the page: which accounts can take a new agent, in what
 * order the rest free up, what the waiting list says, and when the pool goes
 * dark at this pace. No React and no DOM, so every rule here is tested
 * without a render.
 *
 * WHY THIS EXISTS. On 2026-10-24 the page read "7 accounts · 7 available"
 * while 28 agents waited: "available" was the account's state, and nothing on
 * the page said a floor or a cap was refusing every one of them. The broker
 * now says so per account (`account_to_api` in quota_broker/main.py:
 * `can_take_new_agent`, `blocked_by`, `next_eligible_at`, `max_agents`,
 * `organisation`, the duplicate marks), and this module reads those fields
 * rather than re-deriving the rule, so the page and `choose()` cannot drift.
 *
 * THE FIELDS ARE OPTIONAL HERE because `types.ts` `Account` predates them and
 * an older broker serves none of them. Absent is said as absent: a dash with
 * its reason, never a number this page invented.
 */
import { accountName } from './accountName'
import {
  FIVE_HOUR,
  SEVEN_DAY,
  bindingWindow,
  humaniseUntil,
  timeAgo,
  unreadableFor,
  type Account,
  type Task,
} from './types'

// ---------------------------------------------------------------------------
// What GET /v1/accounts serves beyond `types.ts` `Account`
// ---------------------------------------------------------------------------

/** A window under its floor (`blocking_windows`). `used` is 0-1; `floor` is the share that must stay LEFT. */
export interface WindowBlock {
  window: string
  used: number
  floor: number
  resets_at: string
  /** The window blocks only because a reading older than 30 min counts at half. */
  aged_reading?: boolean
}
/** Every slot taken: `cap` agents hold it, of `max_agents`. */
export interface CapBlock {
  cap: number
  max_agents: number
}
/** Never measured and already holding its one agent (BGD, "One until measured"). */
export interface FirstReadingBlock {
  first_reading: 'pending' | string
  assigned: number
  max_before_reading: number
}
/** The later registration of one subscription: withheld, handed out through the other. */
export interface DuplicateBlock {
  duplicate_of: string | null
  reason: string | null
}
/** Identical readings on two polls. ADVISORY: `choose()` still hands it out. */
export interface PossibleDuplicateBlock {
  possible_duplicate: string | null
  advisory: true
  reason: string
}
export type BlockEntry = WindowBlock | CapBlock | FirstReadingBlock | DuplicateBlock | PossibleDuplicateBlock

export interface PoolFields {
  max_agents?: number | null
  can_take_new_agent?: boolean
  blocked_by?: BlockEntry[]
  next_eligible_at?: string | null
  /** The organisation the credential runs on, as the provider named it; null when it has not said. */
  organisation?: { name: string | null; plan: string | null } | null
  /** The other registration of the same organisation, named only inside this workspace. */
  duplicate_of?: string | null
  duplicate_reason?: string | null
  possible_duplicate?: { of: string | null; reason: string } | null
}

export type PoolAccount = Account & PoolFields

export function isWindowBlock(b: BlockEntry): b is WindowBlock {
  return typeof (b as WindowBlock).window === 'string'
}
export function isCapBlock(b: BlockEntry): b is CapBlock {
  return typeof (b as CapBlock).cap === 'number' && typeof (b as CapBlock).max_agents === 'number'
}
export function isFirstReadingBlock(b: BlockEntry): b is FirstReadingBlock {
  return typeof (b as FirstReadingBlock).first_reading === 'string'
}
export function isDuplicateBlock(b: BlockEntry): b is DuplicateBlock {
  return 'duplicate_of' in b
}

// ---------------------------------------------------------------------------
// The rule, as deployed
// ---------------------------------------------------------------------------

/**
 * THE FLOORS AS DEPLOYED (BGD, 2026-10-24 09:16Z; PR 2223): the share of a
 * window that must stay LEFT for the account to take a new agent.
 * `WINDOW_FLOORS` and `DEFAULT_ASSIGN_FLOOR` in quota_broker/accounts.py are
 * the source. The broker serves a floor only inside a `blocked_by` entry, so a
 * window that blocks nothing is drawn against these; a served floor always
 * wins over them (`floorOf`).
 */
export const FLOORS: Readonly<Record<string, number>> = { [FIVE_HOUR]: 0.15, [SEVEN_DAY]: 0.05 }
/** Any other window is held to the default floor (`window_floor`). */
export const DEFAULT_FLOOR = 0.15
/** `DEFAULT_MAX_AGENTS`: what an account takes until its owner sets another number. */
export const DEFAULT_MAX_AGENTS = 6
/** `DEFAULT_STALE_AFTER`: a reading older than this counts at half. */
export const AGED_AFTER_MIN = 30
const WEEK_MS = 7 * 86_400_000
const HOUR_MS = 3_600_000

export function blocksOf(a: PoolAccount): BlockEntry[] {
  return Array.isArray(a.blocked_by) ? a.blocked_by : []
}

/** The floor a window is held to: the broker's own when it served one, else the deployed rule. */
export function floorOf(a: PoolAccount, key: string): number {
  const served = blocksOf(a).find((b): b is WindowBlock => isWindowBlock(b) && b.window === key)
  if (served !== undefined && Number.isFinite(served.floor)) return served.floor
  return FLOORS[key] ?? DEFAULT_FLOOR
}

/** A window this console does not know by name (not 5-hour, not 7-day). */
export function unknownWindows(a: Account): string[] {
  return Object.keys(a.windows ?? {}).filter((k) => k !== FIVE_HOUR && k !== SEVEN_DAY).sort()
}

/** The account's agent cap: served, else the deployed default (an older broker serves none). */
export function maxAgentsOf(a: PoolAccount): number | null {
  return typeof a.max_agents === 'number' && Number.isFinite(a.max_agents) ? a.max_agents : null
}

/** `MAX_AGENTS_BEFORE_READING`: a never-measured account takes this many until its first reading. */
export const MAX_BEFORE_READING = 1

export function freeSlots(a: PoolAccount): number {
  const max = a.observed_at === null ? Math.min(MAX_BEFORE_READING, maxAgentsOf(a) ?? DEFAULT_MAX_AGENTS) : (maxAgentsOf(a) ?? DEFAULT_MAX_AGENTS)
  return Math.max(0, max - Math.max(0, a.assigned))
}

/** The withheld registration of a pair: in `blocked_by` with `duplicate_of`. */
export function isWithheldDuplicate(a: PoolAccount): boolean {
  return blocksOf(a).some(isDuplicateBlock)
}

/**
 * WHETHER THIS WORKSPACE CAN BE HANDED IT NOW. The broker's `can_take_new_agent`
 * is "for a tenant it serves that has not reported it unreadable", so the
 * per-tenant half (`unreadable_now`) is applied here, as `choose()` applies it.
 * Null: the broker did not say (it predates the field).
 *
 * A STATE A PERSON SET WINS OVER THE FLAG (visual QA pass 2, cap2-01; part of
 * #2694). `choose()` hands out only an AVAILABLE account, so a paused,
 * draining or signed-out one is a known No whatever the flag says: the flag
 * read before a Pause (or served by a broker that has not re-read it) kept
 * the row and the verdict at "Yes · 6 free" after the account was paused.
 */
export function canTake(a: PoolAccount, scope: string | null): boolean | null {
  if (a.state !== 'AVAILABLE') return false
  if (typeof a.can_take_new_agent !== 'boolean') return null
  return a.can_take_new_agent && !unreadableFor(a, scope)
}

// ---------------------------------------------------------------------------
// The ranking: "Known clock first" (BGD, 2026-10-24 17:33Z)
// ---------------------------------------------------------------------------

/**
 * The four groups, in rank order. BGD decided the first three: Can take now ->
 * Clears at a known time (soonest first) -> Frees when an agent finishes (full,
 * no clock). The fourth holds what none of the three can: an account a PERSON
 * holds (paused, draining, signed out, skipped here, a withheld duplicate) or
 * one the broker said nothing about (an older broker, the fifth). Each is drawn
 * only when it has a row.
 */
export type GroupKey = 'now' | 'clock' | 'agent' | 'person' | 'unknown'
export const GROUP_TITLE: Readonly<Record<GroupKey, string>> = {
  now: 'Can take now',
  clock: 'Clears at a known time',
  agent: 'Frees when an agent finishes',
  person: 'Waits for a person',
  unknown: 'The broker does not say',
}
const GROUP_ORDER: readonly GroupKey[] = ['now', 'clock', 'agent', 'person', 'unknown']

export function groupOf(a: PoolAccount, scope: string | null): GroupKey {
  const can = canTake(a, scope)
  if (can === true) return 'now'
  if (can === null) return 'unknown'
  if (a.state !== 'AVAILABLE' || unreadableFor(a, scope) || isWithheldDuplicate(a)) return 'person'
  const blocks = blocksOf(a)
  if (blocks.some(isWindowBlock) && clockOf(a) !== null) return 'clock'
  if (blocks.some((b) => isCapBlock(b) || isFirstReadingBlock(b))) return 'agent'
  return clockOf(a) !== null ? 'clock' : 'person'
}

/** When the account can next take an agent by the clock, or null when no clock says. */
export function clockOf(a: PoolAccount): number | null {
  const at = a.next_eligible_at ? Date.parse(a.next_eligible_at) : Number.NaN
  if (Number.isFinite(at)) return at
  // An older broker: the earliest reset of a window that blocks.
  const resets = blocksOf(a)
    .filter(isWindowBlock)
    .map((b) => Date.parse(b.resets_at))
    .filter(Number.isFinite)
  return resets.length > 0 ? Math.min(...resets) : null
}

/**
 * A CLOCK THAT HAS GONE (visual QA pass 1 capacity-03): the reset it names is
 * already past and no reading has come in since it, so it is not when the
 * account clears -- the next reading is. The fixture's frozen clock showed it
 * ("Clears Oct 19 12:40Z · now" on Oct 28), and a broker whose poll stalled
 * serves the same shape. A reading taken after the reset that still blocks is
 * the broker's own word, so that clock stands.
 */
export function clockPassed(a: PoolAccount, now: number): boolean {
  const at = clockOf(a)
  if (at === null || at > now) return false
  const read = a.observed_at ? Date.parse(a.observed_at) : Number.NaN
  return !(Number.isFinite(read) && read >= at)
}

/** The share left on the binding window, 0-1; a reset window is full again. */
function headroom(a: Account): number {
  const b = bindingWindow(a)
  if (b === null) return 1
  return b.window.reset ? 1 : Math.max(0, 1 - b.window.utilization)
}

function byName(a: Account, b: Account): number {
  return accountName(a).localeCompare(accountName(b)) || a.account_id.localeCompare(b.account_id)
}

export interface RankedGroup {
  key: GroupKey
  title: string
  accounts: PoolAccount[]
}

/**
 * The table's rows, grouped and in order. Within Can take now: most free
 * slots, then most headroom (the account `choose()` picks first). Within
 * Clears at a known time: soonest first. The other two by name.
 */
export function rankAccounts(accounts: readonly PoolAccount[], scope: string | null): RankedGroup[] {
  const groups = new Map<GroupKey, PoolAccount[]>(GROUP_ORDER.map((k) => [k, []]))
  for (const a of accounts) groups.get(groupOf(a, scope))?.push(a)
  groups.get('now')?.sort((x, y) => freeSlots(y) - freeSlots(x) || headroom(y) - headroom(x) || byName(x, y))
  groups.get('clock')?.sort((x, y) => (clockOf(x) ?? Infinity) - (clockOf(y) ?? Infinity) || byName(x, y))
  groups.get('agent')?.sort(byName)
  groups.get('person')?.sort(byName)
  groups.get('unknown')?.sort(byName)
  return GROUP_ORDER.map((key) => ({ key, title: GROUP_TITLE[key], accounts: groups.get(key) ?? [] })).filter(
    (g) => g.accounts.length > 0,
  )
}

// ---------------------------------------------------------------------------
// Words
// ---------------------------------------------------------------------------

/** `five_hour` -> `5-hour`, `seven_day` -> `7-day`; any other window by its own name. */
export function windowName(key: string): string {
  if (key === FIVE_HOUR) return '5-hour'
  if (key === SEVEN_DAY) return '7-day'
  return key.replace(/_/g, '-')
}

/** A share as a percent: whole, except under 10%, where the half an aged reading counts at shows (`4.5%`). */
export function pct(share: number): string {
  const p = Math.round(share * 1000) / 10
  return Number.isInteger(p) || p >= 10 ? `${Math.round(p)}%` : `${p.toFixed(1)}%`
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

/**
 * An instant in UTC, the way the issue and the scheduler's waiting line write
 * it: `10:10Z` on the same UTC day as `now`, `Oct 25 18:59Z` on any other. A
 * bare clock from another day reads as today's.
 */
export function utcAt(at: number | string, now: number): string {
  const t = typeof at === 'number' ? at : Date.parse(at)
  if (!Number.isFinite(t)) return '—'
  const d = new Date(t)
  const hm = `${String(d.getUTCHours()).padStart(2, '0')}:${String(d.getUTCMinutes()).padStart(2, '0')}Z`
  const n = new Date(now)
  const sameDay =
    d.getUTCFullYear() === n.getUTCFullYear() && d.getUTCMonth() === n.getUTCMonth() && d.getUTCDate() === n.getUTCDate()
  return sameDay ? hm : `${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()} ${hm}`
}

/** `in 1h 06m`, from `humaniseUntil`, the Clears column's own clock. */
export function inTime(at: number, now: number): string {
  const s = humaniseUntil(at - now)
  return s === 'now' ? 'now' : `in ${s}`
}

/** `organisation Example · Team`, or null when the broker recorded none. */
export function organisationLine(a: PoolAccount): string | null {
  const o = a.organisation
  if (!o || (!o.name && !o.plan)) return null
  return [o.name ?? 'unnamed organisation', o.plan ?? 'plan not reported'].join(' · ')
}

// ---------------------------------------------------------------------------
// The verdict
// ---------------------------------------------------------------------------

export interface Verdict {
  /** How many can take a new agent now. Null when the broker says nothing about any. */
  n: number | null
  /** How many accounts the read holds. */
  m: number
  /** Documents the store could not read: the sentence then says "of at least M". */
  short: boolean
  /** The free slots over the accounts that can take one. */
  free: number
  /** The ones that can take one, in rank order. */
  open: PoolAccount[]
  /** The first account to free by the clock, and when. */
  next: { account: PoolAccount; at: number; window: string | null } | null
  /** The one after it by the clock. */
  then: { account: PoolAccount; at: number; window: string | null } | null
  /** The first full account (frees when an agent finishes), when no clock comes first. */
  full: PoolAccount | null
  /** How many are under a floor, per window, among the ones that cannot take one. */
  underBy: Array<{ window: string; count: number }>
}

function blockingWindowAt(a: PoolAccount, at: number): string | null {
  const w = blocksOf(a)
    .filter(isWindowBlock)
    .find((b) => Date.parse(b.resets_at) === at)
  return w === undefined ? null : w.window
}

export function verdictOf(
  accounts: readonly PoolAccount[],
  scope: string | null,
  shortfall: number,
  now: number = Date.now(),
): Verdict {
  const ranked = rankAccounts(accounts, scope)
  const group = (k: GroupKey) => ranked.find((g) => g.key === k)?.accounts ?? []
  // An empty pool is a known zero; a pool the broker said nothing about is not.
  const known = accounts.length === 0 || accounts.some((a) => typeof a.can_take_new_agent === 'boolean')
  const open = group('now')
  // A clock that has gone names no time anything frees (capacity-03).
  const clocked = group('clock').filter((a) => !clockPassed(a, now))
  const timed = (a: PoolAccount | undefined) => {
    if (a === undefined) return null
    const at = clockOf(a)
    return at === null ? null : { account: a, at, window: blockingWindowAt(a, at) }
  }
  const under = new Map<string, number>()
  for (const a of accounts) {
    if (canTake(a, scope) === true) continue
    for (const b of blocksOf(a).filter(isWindowBlock)) under.set(b.window, (under.get(b.window) ?? 0) + 1)
  }
  return {
    n: known ? open.length : null,
    m: accounts.length,
    short: shortfall > 0,
    free: open.reduce((s, a) => s + freeSlots(a), 0),
    open,
    next: timed(clocked[0]),
    then: timed(clocked[1]),
    full: group('agent')[0] ?? null,
    underBy: [...under.entries()]
      .map(([window, count]) => ({ window, count }))
      .sort((x, y) => y.count - x.count || x.window.localeCompare(y.window)),
  }
}

// ---------------------------------------------------------------------------
// Who waits on the pool
// ---------------------------------------------------------------------------

/**
 * A PARKED task waiting on the account pool. The listing row's `waiting` is
 * `{reason, summary}` only (codec.py), so the pool park is told from a
 * provider's own refusal by the summary `waiting_detail._pool` writes for it:
 * both carry PROVIDER_QUOTA_EXHAUSTED, and only the pool's names the pool.
 */
export const POOL_WAIT_PREFIX = 'No pool account can take an agent'

export function waitsOnPool(t: Pick<Task, 'state' | 'waiting'>): boolean {
  const w = t.waiting
  return (
    t.state === 'PARKED' &&
    !!w &&
    w.reason === 'PROVIDER_QUOTA_EXHAUSTED' &&
    typeof w.summary === 'string' &&
    w.summary.startsWith(POOL_WAIT_PREFIX)
  )
}

export interface PoolWaiting {
  agents: number
  /** Later steps of the same workflows, parked on DEPENDENCY_INCOMPLETE. */
  later: number
  /** The page was cut short (`next_page_token`): the figures are lower bounds. */
  atLeast: boolean
}

/**
 * The waiting count, "Agents, steps as a note" (BGD): the agents that wait on
 * the pool, and the later steps of THEIR workflows that wait on them -- the 32
 * DEPENDENCY_INCOMPLETE parks #2222 counted beside the 28.
 */
export function poolWaiting(tasks: ReadonlyArray<Pick<Task, 'state' | 'waiting' | 'workflow_id'>>, truncated: boolean): PoolWaiting {
  const onPool = tasks.filter(waitsOnPool)
  const flows = new Set(onPool.map((t) => t.workflow_id).filter((w): w is string => typeof w === 'string' && w !== ''))
  const later = tasks.filter(
    (t) =>
      t.state === 'PARKED' &&
      t.waiting?.reason === 'DEPENDENCY_INCOMPLETE' &&
      typeof t.workflow_id === 'string' &&
      flows.has(t.workflow_id),
  ).length
  return { agents: onPool.length, later, atLeast: truncated }
}

// ---------------------------------------------------------------------------
// The forecast: "From today's readings" (BGD, 2026-10-24 17:33Z)
// ---------------------------------------------------------------------------

/**
 * One subscription's 7-day pace: its used share over the hours since its week
 * opened (`resets_at` minus 7 days). No new API field: both are served.
 */
export interface Pace {
  account: PoolAccount
  /** Share of the window spent per hour, 0-1 per hour. */
  perHour: number
  /** Share left above its floor now (an aged reading counts at half). */
  above: number
  floor: number
  resetsAt: number
}

export interface Forecast {
  /** The subscriptions the pace was taken over: a withheld duplicate counted once. */
  paces: Pace[]
  /** The pool's pace: every subscription's, summed, in share-of-a-week per hour. */
  poolPerHour: number
  /** Accounts that can take agents with no 7-day reading to take a pace from. */
  unmeasured: PoolAccount[]
  /** When the last open account reaches its 7-day floor at this pace; null when not within the horizon. */
  darkAt: number | null
  /** Already dark on the weekly windows: every subscription is under its 7-day floor now. */
  darkNow: boolean
  /** Which account closes last, at `darkAt`. */
  lastOpen: PoolAccount | null
  /** When the first one comes back after `darkAt`, and which. */
  backAt: number | null
  back: PoolAccount | null
}

/** How far ahead the forecast looks: past this, "not within 8 days". */
export const FORECAST_HORIZON_MS = 8 * 86_400_000

/**
 * The closed stretches of one subscription's 7-day window at its own pace:
 * closed when its share left reaches its floor, open again at its reset, and
 * at the same pace through the week after. Exported for the Timeline's pace
 * row, so the row and this forecast are one computation.
 */
export function closedSpans(p: Pace, now: number): Array<[number, number]> {
  const spans: Array<[number, number]> = []
  if (p.perHour <= 0) return p.above <= 0 ? [[now, p.resetsAt]] : spans
  const close = now + (p.above / p.perHour) * HOUR_MS
  if (p.above <= 0) spans.push([now, p.resetsAt])
  else if (close < p.resetsAt) spans.push([close, p.resetsAt])
  // The next week, at the same pace, from a full window.
  const next = p.resetsAt + ((1 - p.floor) / p.perHour) * HOUR_MS
  if (next < p.resetsAt + WEEK_MS) spans.push([next, p.resetsAt + WEEK_MS])
  return spans
}

export function forecastOf(accounts: readonly PoolAccount[], scope: string | null, now: number): Forecast {
  const paces: Pace[] = []
  const unmeasured: PoolAccount[] = []
  for (const a of accounts) {
    // Counted once: the withheld registration is the other's subscription.
    // A paused, signed-out or skipped account takes no agent either way.
    if (isWithheldDuplicate(a) || a.state !== 'AVAILABLE' || unreadableFor(a, scope)) continue
    const w = a.windows?.[SEVEN_DAY]
    const resetsAt = w ? Date.parse(w.resets_at) : Number.NaN
    if (a.observed_at === null || !w || !Number.isFinite(resetsAt) || !Number.isFinite(w.utilization)) {
      unmeasured.push(a)
      continue
    }
    const floor = floorOf(a, SEVEN_DAY)
    if (w.reset) {
      // Reset, nothing read since: a full window and no pace to spend it at.
      paces.push({ account: a, perHour: 0, above: 1 - floor, floor, resetsAt: resetsAt + WEEK_MS })
      continue
    }
    const hours = (now - (resetsAt - WEEK_MS)) / HOUR_MS
    const left = Math.max(0, 1 - w.utilization) * (a.stale && a.observed_at !== null ? 0.5 : 1)
    paces.push({
      account: a,
      perHour: hours > 0 ? Math.max(0, w.utilization) / hours : 0,
      above: Math.max(0, left - floor),
      floor,
      resetsAt,
    })
  }
  const poolPerHour = paces.reduce((s, p) => s + p.perHour, 0)
  const empty: Forecast = { paces, poolPerHour, unmeasured, darkAt: null, darkNow: false, lastOpen: null, backAt: null, back: null }
  // An account with no 7-day reading may be open: the pool is not dark while it is.
  if (paces.length === 0 || unmeasured.length > 0) return empty

  const spans = paces.map((p) => closedSpans(p, now))
  const closedAt = (i: number, t: number) => spans[i]!.some(([s, e]) => t >= s && t < e)
  const candidates = [now, ...spans.flat().map(([s]) => s)].filter((t) => t >= now && t <= now + FORECAST_HORIZON_MS).sort((x, y) => x - y)
  const darkAt = candidates.find((t) => paces.every((_, i) => closedAt(i, t))) ?? null
  if (darkAt === null) return empty
  let lastOpen: PoolAccount | null = null
  let lastStart = -Infinity
  let backAt = Infinity
  let back: PoolAccount | null = null
  paces.forEach((p, i) => {
    const span = spans[i]!.find(([s, e]) => darkAt >= s && darkAt < e)
    if (span === undefined) return
    if (span[0] > lastStart) {
      lastStart = span[0]
      lastOpen = p.account
    }
    if (span[1] < backAt) {
      backAt = span[1]
      back = p.account
    }
  })
  return {
    ...empty,
    darkAt,
    darkNow: darkAt === now,
    lastOpen,
    backAt: Number.isFinite(backAt) ? backAt : null,
    back,
  }
}

// ---------------------------------------------------------------------------
// One account's forecast, for its detail (#2222, ACCOUNTS-A-DETAIL)
// ---------------------------------------------------------------------------

/**
 * One subscription's own 7-day outlook, the pool forecast's rule applied to
 * one account: its used share over the hours since its week opened
 * (`resets_at` minus 7 days), so no new API field. An aged reading counts at
 * half, as the broker counts it.
 *
 *   `floorAt`  when, at this pace, its share left reaches its 7-day floor;
 *              null when that is not before its week resets
 *   `underNow` already under its floor
 *   `resetsAt` when its week resets: the served instant, or the one after it
 *              for a window that already reset
 *   `read`     whether `resetsAt` is the served instant (true) or one week on
 *              from a reset nothing has been read since (false)
 *
 * Null when there is no 7-day reading to take a pace from.
 */
export interface AccountPace {
  perHour: number
  floor: number
  floorAt: number | null
  underNow: boolean
  resetsAt: number
  read: boolean
}

export function accountPaceOf(a: PoolAccount, now: number): AccountPace | null {
  const w = a.windows?.[SEVEN_DAY]
  const resetsAt = w ? Date.parse(w.resets_at) : Number.NaN
  if (a.observed_at === null || !w || !Number.isFinite(resetsAt) || !Number.isFinite(w.utilization)) return null
  const floor = floorOf(a, SEVEN_DAY)
  if (w.reset) return { perHour: 0, floor, floorAt: null, underNow: false, resetsAt: resetsAt + WEEK_MS, read: false }
  const hours = (now - (resetsAt - WEEK_MS)) / HOUR_MS
  const perHour = hours > 0 ? Math.max(0, w.utilization) / hours : 0
  const left = Math.max(0, 1 - w.utilization) * (a.stale ? 0.5 : 1)
  const above = left - floor
  if (above <= 0) return { perHour, floor, floorAt: now, underNow: true, resetsAt, read: true }
  const at = perHour > 0 ? now + (above / perHour) * HOUR_MS : Infinity
  return { perHour, floor, floorAt: at < resetsAt ? at : null, underNow: false, resetsAt, read: true }
}

/**
 * The detail's head chip: `Can take a new agent · 6 of 6 slots free`,
 * `Full · 6 of 6 agents`, `Cannot take a new agent`, or null when the broker
 * does not say. A never-measured account counts one slot (BGD, "One until
 * measured").
 */
export function slotsLine(a: PoolAccount, scope: string | null): string | null {
  const can = canTake(a, scope)
  if (can === null) return null
  const max = a.observed_at === null ? Math.min(MAX_BEFORE_READING, maxAgentsOf(a) ?? DEFAULT_MAX_AGENTS) : (maxAgentsOf(a) ?? DEFAULT_MAX_AGENTS)
  if (can) return `Can take a new agent · ${freeSlots(a)} of ${max} slot${max === 1 ? '' : 's'} free`
  const cap = blocksOf(a).find(isCapBlock)
  if (cap !== undefined && groupOf(a, scope) === 'agent') return `Full · ${cap.cap} of ${cap.max_agents} agents`
  return 'Cannot take a new agent'
}

/**
 * An age past a day in days AND hours (QA G5-23): `given out 1d 20h ago`, not
 * `44h ago`, which is arithmetic a reader does before reacting. Under a day it
 * is `timeAgo`'s. Floored, so an age never reads older than it is; a whole
 * number of days drops its `0h`.
 */
export function agoPastDay(when: string, now: number = Date.now()): string {
  const t = Date.parse(when)
  if (!Number.isFinite(t)) return timeAgo(when, now)
  const h = Math.floor((now - t) / 3_600_000)
  if (h < 24) return timeAgo(t, now)
  const d = Math.floor(h / 24)
  const r = h % 24
  return r === 0 ? `${d}d ago` : `${d}d ${r}h ago`
}
