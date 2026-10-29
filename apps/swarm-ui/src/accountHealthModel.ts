// CAPACITY › ACCOUNT HEALTH, THE ARITHMETIC (ACCOUNT-HEALTH-PAGE, part of #2302).
//
// Everything the page draws is computed here, from two reads and nothing else:
// `GET /v1/accounts` (each row's `health_status` and `health_24h`, which
// ACCOUNT-429-SCOPE added) and `GET /v1/accounts/health` (every account's
// record in one read: status, counts, the hourly series, the last 50 events).
// Pure, so the rules the mock-ups were picked for -- the tiles agree with the
// list, the worst account comes first, one mark per served event, and the
// console's incident grouping -- are tested without a DOM.
//
// THE STATUS IS THE BROKER'S WORD. The console never derives `limited` or
// `erroring` from the counts: `quota_broker.accountstore.health_status` owns
// that rule, and a second copy here would be the drift this repository keeps
// paying for. The console only COUNTS the words and sorts by them.

import { incidentsOf } from './accountHealth'
import { accountName } from './accountName'
import { isWithheldDuplicate, type PoolAccount } from './accountsPool'
import { unreadableFor } from './types'
import type { AccountHealthEvent } from './api'

/** `HEALTH_KINDS` in quota_broker/accountstore.py, and nothing else. */
export const HEALTH_KINDS = [
  'rate_limit',
  'exhausted',
  'assign_refused',
  'overloaded',
  'server_error',
  'network',
  'auth',
  'refresh_failed',
  'reauth',
] as const
export type HealthKind = (typeof HEALTH_KINDS)[number]

/** `ACCOUNT_HEALTH_STATUSES` in swarm_api/schemas.py: the broker's four words. */
export const HEALTH_STATUSES = ['healthy', 'limited', 'erroring', 'reauth_needed'] as const
export type HealthStatus = (typeof HEALTH_STATUSES)[number]

/**
 * THE THREE FAMILIES the page draws (proposal B's legend): limits, provider
 * errors, credential trouble. Each has a fixed colour slot AND a fixed shape
 * (diamond, circle, square), so a family never depends on hue alone.
 */
export type HealthFamily = 'lim' | 'err' | 'cred'
export const FAMILY_OF: Readonly<Record<HealthKind, HealthFamily>> = {
  rate_limit: 'lim',
  exhausted: 'lim',
  // A refusal at assignment is the broker withholding the account. Counted with
  // the limits in the 24 h columns, as the mock-up's "What it reads" table
  // does; the incident rule below moves a refusal with no window to the
  // credential incident it belongs to.
  assign_refused: 'lim',
  overloaded: 'err',
  server_error: 'err',
  network: 'err',
  auth: 'cred',
  refresh_failed: 'cred',
  reauth: 'cred',
}
export const FAMILIES: readonly { key: HealthFamily; label: string; one: string }[] = [
  { key: 'lim', label: 'Limits', one: 'limit' },
  { key: 'err', label: 'Provider errors', one: 'provider error' },
  { key: 'cred', label: 'Credential', one: 'credential event' },
]

export const KIND_LABEL: Readonly<Record<HealthKind, string>> = {
  rate_limit: 'Rate-limit hit',
  exhausted: 'Exhausted report',
  assign_refused: 'Refused at assignment',
  overloaded: 'Overloaded (529)',
  server_error: 'Server error (5xx)',
  network: 'Network error',
  auth: 'Auth refused (401)',
  refresh_failed: 'Credential refresh failed',
  reauth: 'Re-auth marked',
}

export const STATUS_WORD: Readonly<Record<HealthStatus, string>> = {
  healthy: 'Healthy',
  limited: 'Limited',
  erroring: 'Erroring',
  reauth_needed: 'Needs re-auth',
}

/**
 * What each word costs the pool, under its tile (proposal B, F1). Healthy is
 * the health record's word, not the pool's: an account skipped here or
 * withheld as a second registration is Healthy and still takes no agent, so
 * its hint says what the word means rather than "nothing to do" (visual QA
 * pass 2, capacity-04), and `healthyWithheld` names the ones the pool holds.
 */
export const STATUS_HINT: Readonly<Record<HealthStatus, string>> = {
  healthy: 'no limit, error or sign-in trouble',
  limited: 'withheld until their windows reset',
  erroring: 'still assigned; errors do not withhold',
  reauth_needed: 'withheld until someone signs in',
}

export function windowWord(w: string | null): string {
  return w === 'five_hour' ? '5-hour' : w === 'seven_day' ? '7-day' : w === null ? '' : 'other'
}

// ---------------------------------------------------------------------------
// The served shapes
// ---------------------------------------------------------------------------

export type KindCounts = Record<HealthKind, number>

/** One event, as swarm-api serves it to this viewer (`routes/accounts.py` `_health_event`). */
export interface HealthEvent {
  at: string
  kind: HealthKind
  /** Null on another workspace's event read by a borrower. */
  tenant_id: string | null
  task_id: string | null
  window: string | null
  reset_at: string | null
  /** Null on another workspace's event read by a borrower: withheld, not empty. */
  message: string | null
  own: boolean
}

export interface HealthHour {
  hour: string
  counts: Partial<KindCounts>
  total: number
}

/** One account's record in `GET /v1/accounts/health`. */
export interface AccountHealthRecord {
  account_id: string
  label: string
  owner_tenant: string
  status: HealthStatus | null
  limited_until: string | null
  counts: { last_1h: KindCounts | null; last_24h: KindCounts | null; last_7d: KindCounts | null }
  hourly: HealthHour[]
  /** Newest first, at most `events_max`. */
  events: HealthEvent[]
  observed_at: string | null
}

export interface PoolHealth {
  tenant_id: string | null
  accounts: AccountHealthRecord[]
  events_max: number | null
  truncated: boolean
  observed_at: string | null
}

/**
 * The two fields ACCOUNT-429-SCOPE added to each `GET /v1/accounts` row.
 * ABSENT (undefined) is a broker older than the record: not served. NULL is a
 * broker that serves the record and could not read it this time: unknown.
 * Neither is ever drawn as `healthy` or as a zero.
 */
export interface HealthRowFields {
  health_status?: HealthStatus | string | null
  health_24h?: Partial<KindCounts> | null
}
export type HealthAccount = PoolAccount & HealthRowFields

/** The pool-wide read, as the page holds it. */
export type HealthRead =
  | { kind: 'ok'; pool: PoolHealth }
  /** This deployment's API or broker predates the record: a dash and its reason. */
  | { kind: 'not_served'; reason: string }
  /** The read was made and failed. */
  | { kind: 'unreadable'; reason: string }

// ---------------------------------------------------------------------------
// Status: one source for the tiles AND the list, so they cannot disagree
// ---------------------------------------------------------------------------

/** A status, or why there is none. */
export type StatusCell = { status: HealthStatus } | { status: null; why: string }

export const NOT_SERVED_WHY = 'This deployment does not serve account health (its broker predates the health record).'
export const UNREAD_WHY = "The broker could not read this account's health record on this read."

/**
 * THE ONE PLACE A ROW'S STATUS IS READ. The tiles count it and the list draws
 * it, so a tile can never say 2 limited over a list showing 3. The row's own
 * `health_status` first (the listing is the read the rest of Capacity uses),
 * then the pool read's record for the same account.
 *
 * When neither says, the row's own `state`: REAUTH_REQUIRED is the broker's
 * own answer that the provider refused the credential, so an account whose
 * health is null (not served, or unread on this read) still reads as needing
 * re-auth. It lives here, not in the page, so `worstFirst` sorts that row
 * first and the tiles, the list and the phone's cards agree (BGD, 2026-10-26,
 * #2391). Its figures stay dashes with their reason: the state says nothing
 * about counts.
 */
export function statusOf(a: HealthAccount, record: AccountHealthRecord | undefined): StatusCell {
  const word = a.health_status
  if (isStatus(word)) return { status: word }
  if (record !== undefined && isStatus(record.status)) return { status: record.status }
  if (a.state === 'REAUTH_REQUIRED') return { status: 'reauth_needed' }
  if (word === undefined && record === undefined) return { status: null, why: NOT_SERVED_WHY }
  return { status: null, why: UNREAD_WHY }
}

function isStatus(v: unknown): v is HealthStatus {
  return typeof v === 'string' && (HEALTH_STATUSES as readonly string[]).includes(v)
}

export type Tally = Record<HealthStatus, number> & { unknown: number }

export function tally(accounts: readonly HealthAccount[], records: ReadonlyMap<string, AccountHealthRecord>): Tally {
  const t: Tally = { healthy: 0, limited: 0, erroring: 0, reauth_needed: 0, unknown: 0 }
  for (const a of accounts) {
    const s = statusOf(a, records.get(a.account_id))
    if (s.status === null) t.unknown += 1
    else t[s.status] += 1
  }
  return t
}

/**
 * THE HEALTHY ACCOUNTS THE POOL STILL WITHHOLDS (visual QA pass 2,
 * capacity-04; part of #2694). The broker's word stays the status -- the
 * tiles count what the list draws -- but "Healthy 5" over a pool where
 * Accounts says two of them cannot take an agent read as five to hand out.
 * Withheld is what a person or the pool's own rule holds, not a clock or a
 * cap: a state someone set (paused, draining), an account this workspace
 * could not read (skipped here), or the later registration of one
 * subscription. A full account is not here: it frees when an agent finishes.
 */
export function healthyWithheld(
  accounts: readonly HealthAccount[],
  records: ReadonlyMap<string, AccountHealthRecord>,
  scope: string | null,
): HealthAccount[] {
  return accounts.filter(
    (a) =>
      statusOf(a, records.get(a.account_id)).status === 'healthy' &&
      (a.state !== 'AVAILABLE' || unreadableFor(a, scope) || isWithheldDuplicate(a)),
  )
}

/** The line under the Healthy tile naming them, or null when none is withheld. */
export function healthyWithheldLine(withheld: readonly HealthAccount[]): string | null {
  if (withheld.length === 0) return null
  const names = withheld.map(accountName).join(', ')
  return withheld.length === 1
    ? `1 of them cannot take an agent: ${names}`
    : `${withheld.length} of them cannot take an agent: ${names}`
}

/** Whether ANY account's health is served on this deployment. */
export function healthServed(accounts: readonly HealthAccount[], read: HealthRead): boolean {
  if (read.kind === 'ok') return true
  return accounts.some((a) => a.health_status !== undefined)
}

// ---------------------------------------------------------------------------
// Counts
// ---------------------------------------------------------------------------

export function emptyCounts(): KindCounts {
  return Object.fromEntries(HEALTH_KINDS.map((k) => [k, 0])) as KindCounts
}

function asCounts(raw: Partial<KindCounts> | null | undefined): KindCounts | null {
  if (raw === null || raw === undefined || typeof raw !== 'object') return null
  const out = emptyCounts()
  for (const k of HEALTH_KINDS) {
    const n = raw[k]
    out[k] = typeof n === 'number' && Number.isFinite(n) ? n : 0
  }
  return out
}

/** The last 24 h by kind: the row's own, else the pool record's; null when neither was served. */
export function counts24h(a: HealthAccount, record: AccountHealthRecord | undefined): KindCounts | null {
  return asCounts(a.health_24h) ?? asCounts(record?.counts.last_24h)
}

export function familyCount(c: KindCounts, fam: HealthFamily): number {
  let n = 0
  for (const k of HEALTH_KINDS) if (FAMILY_OF[k] === fam) n += c[k]
  return n
}

export function total(c: KindCounts | null): number {
  if (c === null) return 0
  let n = 0
  for (const k of HEALTH_KINDS) n += c[k]
  return n
}

// ---------------------------------------------------------------------------
// Worst first
// ---------------------------------------------------------------------------

/** Re-auth needs a person, erroring is happening now, limited waits on a clock. */
export const SEVERITY: Readonly<Record<HealthStatus, number>> = {
  reauth_needed: 0,
  erroring: 1,
  limited: 2,
  healthy: 3,
}

/**
 * The accounts worst first: by the broker's word (re-auth, erroring, limited,
 * healthy, then unknown), then by how much happened in the last 24 hours, then
 * by name -- so the order is stable between polls that changed nothing.
 */
export function worstFirst(
  accounts: readonly HealthAccount[],
  records: ReadonlyMap<string, AccountHealthRecord>,
): HealthAccount[] {
  const rank = (a: HealthAccount) => {
    const s = statusOf(a, records.get(a.account_id))
    return s.status === null ? 4 : SEVERITY[s.status]
  }
  const busy = (a: HealthAccount) => total(counts24h(a, records.get(a.account_id)))
  return [...accounts].sort(
    (x, y) => rank(x) - rank(y) || busy(y) - busy(x) || x.account_id.localeCompare(y.account_id),
  )
}

// ---------------------------------------------------------------------------
// The swimlane (proposal B, F3)
// ---------------------------------------------------------------------------

export const HOUR_MS = 3_600_000
export const SPAN_HOURS = 168

/** The first hour of the 7-day span: the served series' own first hour, else now's hour less 167. */
export function spanStart(pool: PoolHealth | null, now: number): number {
  for (const r of pool?.accounts ?? []) {
    const first = r.hourly[0]
    if (first !== undefined) {
      const t = Date.parse(first.hour)
      if (Number.isFinite(t)) return t
    }
  }
  return Math.floor(now / HOUR_MS) * HOUR_MS - (SPAN_HOURS - 1) * HOUR_MS
}

/** One mark on a lane: one served event, never a bucket of several. */
export interface LaneMark {
  account_id: string
  event: HealthEvent
  family: HealthFamily
  at: number
}

/** Every served event inside the span, one mark each. */
export function laneMarks(record: AccountHealthRecord, start: number, now: number): LaneMark[] {
  const out: LaneMark[] = []
  for (const e of record.events) {
    const at = Date.parse(e.at)
    if (!Number.isFinite(at) || at < start || at > now + HOUR_MS) continue
    out.push({ account_id: record.account_id, event: e, family: FAMILY_OF[e.kind] ?? 'err', at })
  }
  return out
}

/** A limited stretch on a lane: from a rate-limit hit to its window's reset. */
export interface LimitBand {
  from: number
  to: number
  window: string | null
  hits: number
}

/**
 * The account's limited stretches, from its own rate-limit events (oldest
 * first): a hit opens a band to its `reset_at`, and a later hit on the same
 * window before that reset joins it. A hit with no reset draws no band -- the
 * console does not invent how long a limit lasted.
 */
export function limitBands(record: AccountHealthRecord): LimitBand[] {
  const out: LimitBand[] = []
  for (const e of [...record.events].reverse()) {
    if (e.kind !== 'rate_limit') continue
    const from = Date.parse(e.at)
    const to = e.reset_at === null ? NaN : Date.parse(e.reset_at)
    if (!Number.isFinite(from) || !Number.isFinite(to)) continue
    const last = out[out.length - 1]
    if (last !== undefined && from <= last.to && e.window === last.window) {
      last.hits += 1
      if (to > last.to) last.to = to
      continue
    }
    out.push({ from, to, window: e.window, hits: 1 })
  }
  return out
}

/** The pool's events per hour, by family: the sum of every account's served series. */
export interface PoolHour {
  at: number
  lim: number
  err: number
  cred: number
  total: number
}

export function poolHours(pool: PoolHealth, start: number): PoolHour[] {
  const out: PoolHour[] = Array.from({ length: SPAN_HOURS }, (_, i) => ({ at: start + i * HOUR_MS, lim: 0, err: 0, cred: 0, total: 0 }))
  for (const r of pool.accounts) {
    for (const h of r.hourly) {
      const i = Math.round((Date.parse(h.hour) - start) / HOUR_MS)
      const slot = out[i]
      if (slot === undefined) continue
      for (const k of HEALTH_KINDS) {
        const n = h.counts[k] ?? 0
        if (n > 0) {
          slot[FAMILY_OF[k]] += n
          slot.total += n
        }
      }
    }
  }
  return out
}

/**
 * The 7-day count the record SERVES, summed over the pool. When it is more
 * than the marks drawn, the difference is events older than each account's
 * last 50: counted in the series and the 7-day totals, with their messages
 * gone. The page says so rather than letting the two figures disagree.
 */
export function served7d(pool: PoolHealth): number {
  return pool.accounts.reduce((n, r) => n + total(asCounts(r.counts.last_7d)), 0)
}

// ---------------------------------------------------------------------------
// Incidents (proposal D's rule; docs/quota-management.md "Incidents")
// ---------------------------------------------------------------------------

// THE RULE IS accountHealth.ts's `incidentsOf` (BGD, 2026-10-25): the account
// sheet's Health section and this page group events by one function, so the
// phone's cards and the sheet cannot disagree about where an incident starts
// or whether it is open. This page only names each incident's account.
export { ERROR_OPEN_MS, INCIDENT_GAP_MS } from './accountHealth'

export interface Incident {
  account_id: string
  label: string
  family: HealthFamily
  /** Oldest first. */
  events: HealthEvent[]
  from: number
  last: number
  /** A limit incident's reset: the latest `reset_at` of the events in it. */
  to: number | null
  window: string | null
  open: boolean
}

/**
 * One account's served events as incidents, by the shared rule
 * (`incidentsOf`), each named for its account. Newest first. The record holds
 * at most `events_max` (50) events, newest first, which is what `incidentsOf`
 * keeps; a limit's `to` is its reset, null on the other families.
 */
export function incidents(record: AccountHealthRecord, status: HealthStatus | null, now: number): Incident[] {
  // The page's event is the API's, with `window` read as any string: the rule
  // copies the window and never branches on it.
  const served = record.events as unknown as readonly AccountHealthEvent[]
  return incidentsOf(served, status, now).map((x) => ({
    account_id: record.account_id,
    label: record.label || record.account_id,
    family: x.fam,
    events: x.events as unknown as HealthEvent[],
    from: x.from,
    last: x.last,
    to: x.to,
    window: x.window,
    open: x.open,
  }))
}

/** Open first, worst first: credential, then errors, then limits; newer first within each. */
export const OPEN_ORDER: Readonly<Record<HealthFamily, number>> = { cred: 0, err: 1, lim: 2 }

/**
 * Every account in the pool's incidents, open and closed. An account whose
 * status is `reauth_needed` always has an open credential incident: when its
 * served events hold none (the record is empty, the re-auth is older than 7
 * days, or 50 newer events pushed it out) the incident is drawn with no events
 * (`events: []`, `from` and `last` NaN), so the phone never says "nothing is
 * open" beside a summary counting an account that needs re-auth.
 */
export function poolIncidents(
  pool: PoolHealth,
  statusFor: (id: string) => HealthStatus | null,
  now: number,
  accounts: readonly { account_id: string; label: string }[] = [],
): { open: Incident[]; closed: Incident[] } {
  const all = pool.accounts.flatMap((r) => incidents(r, statusFor(r.account_id), now))
  const labels = new Map<string, string>(pool.accounts.map((r) => [r.account_id, r.label || r.account_id]))
  for (const a of accounts) if (!labels.has(a.account_id)) labels.set(a.account_id, a.label || a.account_id)
  for (const [id, label] of labels) {
    if (statusFor(id) !== 'reauth_needed') continue
    if (all.some((x) => x.account_id === id && x.family === 'cred' && x.open)) continue
    all.push({ account_id: id, label, family: 'cred', events: [], from: NaN, last: NaN, to: null, window: null, open: true })
  }
  // An undated incident (NaN) sorts as the oldest of its family.
  const t = (x: Incident) => (Number.isFinite(x.from) ? x.from : -Infinity)
  const newer = (x: Incident, y: Incident) => (t(y) === t(x) ? 0 : t(y) > t(x) ? 1 : -1)
  const open = all
    .filter((x) => x.open)
    .sort((x, y) => OPEN_ORDER[x.family] - OPEN_ORDER[y.family] || newer(x, y))
  const closed = all.filter((x) => !x.open).sort(newer)
  return { open, closed }
}

/** An incident's title, as D's cards word it. */
export function incidentTitle(x: Incident): string {
  const name = x.label
  if (x.family === 'lim') return x.window ? `${name} rate-limited on its ${windowWord(x.window)} window` : `${name} limited`
  if (x.family === 'cred' && x.open) return `${name} needs re-auth`
  if (x.events.length === 1) return `${name}: one ${KIND_LABEL[x.events[0]!.kind].toLowerCase()}`
  if (x.family === 'cred') return `${name}: ${x.events.length} credential events`
  return `${name} erroring: ${x.events.length} provider errors`
}

/** "2 rate-limit hits · 1 refused at assignment". */
export function incidentSummary(x: Incident): string {
  const c = new Map<HealthKind, number>()
  for (const e of x.events) c.set(e.kind, (c.get(e.kind) ?? 0) + 1)
  return HEALTH_KINDS.filter((k) => c.has(k))
    .map((k) => `${c.get(k)} × ${KIND_LABEL[k].toLowerCase()}`)
    .join(' · ')
}

// ---------------------------------------------------------------------------
// Time, in the console's UTC clock words
// ---------------------------------------------------------------------------

const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
const p2 = (n: number) => String(n).padStart(2, '0')

export function hm(t: number): string {
  const d = new Date(t)
  return `${p2(d.getUTCHours())}:${p2(d.getUTCMinutes())}Z`
}
export function hms(t: number): string {
  const d = new Date(t)
  return `${p2(d.getUTCHours())}:${p2(d.getUTCMinutes())}:${p2(d.getUTCSeconds())}Z`
}
export function day(t: number): string {
  const d = new Date(t)
  return `${MON[d.getUTCMonth()]} ${d.getUTCDate()}`
}
/** `06:12Z` today, `Oct 22 14:05Z` otherwise. */
export function when(t: number, now: number): string {
  return day(t) === day(now) ? hm(t) : `${day(t)} ${hm(t)}`
}
export function whenS(t: number, now: number): string {
  return day(t) === day(now) ? hms(t) : `${day(t)} ${hms(t)}`
}
/** `12 min`, `3 h 5 min`, `2 d 4 h`. */
export function span(ms: number): string {
  let m = Math.round(Math.abs(ms) / 60_000)
  if (m < 60) return `${m} min`
  let h = Math.floor(m / 60)
  m %= 60
  if (h < 24) return `${h} h${m ? ` ${m} min` : ''}`
  const d = Math.floor(h / 24)
  h %= 24
  return `${d} d${h ? ` ${h} h` : ''}`
}

// ---------------------------------------------------------------------------
// The served payload, by allow-list
// ---------------------------------------------------------------------------

function rec(v: unknown): Record<string, unknown> | null {
  return typeof v === 'object' && v !== null && !Array.isArray(v) ? (v as Record<string, unknown>) : null
}
const str = (v: unknown): string | null => (typeof v === 'string' ? v : null)

function normCounts(v: unknown): KindCounts | null {
  const r = rec(v)
  return r === null ? null : asCounts(r as Partial<KindCounts>)
}

function normEvent(v: unknown): HealthEvent | null {
  const r = rec(v)
  if (r === null) return null
  const at = str(r.at)
  const kind = str(r.kind)
  if (at === null || kind === null || !(HEALTH_KINDS as readonly string[]).includes(kind)) return null
  return {
    at,
    kind: kind as HealthKind,
    tenant_id: str(r.tenant_id),
    task_id: str(r.task_id),
    window: str(r.window),
    reset_at: str(r.reset_at),
    message: str(r.message),
    own: r.own === true,
  }
}

/**
 * `GET /v1/accounts/health`, kept to the fields this page draws. Null when the
 * answer is not that shape at all -- read as unreadable, never as an empty pool.
 */
export function normPoolHealth(raw: unknown): PoolHealth | null {
  const r = rec(raw)
  if (r === null || !Array.isArray(r.accounts)) return null
  const accounts: AccountHealthRecord[] = []
  for (const item of r.accounts) {
    const a = rec(item)
    const id = a === null ? null : str(a.account_id)
    if (a === null || id === null) continue
    const counts = rec(a.counts) ?? {}
    const hourly: HealthHour[] = []
    for (const h of Array.isArray(a.hourly) ? a.hourly : []) {
      const hr = rec(h)
      const hour = hr === null ? null : str(hr.hour)
      if (hr === null || hour === null) continue
      const c = normCounts(hr.counts) ?? emptyCounts()
      const kept: Partial<KindCounts> = {}
      for (const k of HEALTH_KINDS) if (c[k] > 0) kept[k] = c[k]
      hourly.push({ hour, counts: kept, total: total(c) })
    }
    accounts.push({
      account_id: id,
      label: str(a.label) ?? '',
      owner_tenant: str(a.owner_tenant) ?? '',
      status: isStatus(a.status) ? a.status : null,
      limited_until: str(a.limited_until),
      counts: {
        last_1h: normCounts(counts.last_1h),
        last_24h: normCounts(counts.last_24h),
        last_7d: normCounts(counts.last_7d),
      },
      hourly,
      events: (Array.isArray(a.events) ? a.events : []).map(normEvent).filter((e): e is HealthEvent => e !== null),
      observed_at: str(a.observed_at),
    })
  }
  return {
    tenant_id: str(r.tenant_id),
    accounts,
    events_max: typeof r.events_max === 'number' ? r.events_max : null,
    truncated: r.truncated === true,
    observed_at: str(r.observed_at),
  }
}
