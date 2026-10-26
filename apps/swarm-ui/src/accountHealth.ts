/**
 * ONE ACCOUNT'S HEALTH RECORD, READ FOR DRAWING (#2302).
 *
 * Pure: no React, no fetch. What `GET /v1/accounts/{id}/health` serves
 * (`AccountHealth` in api.ts) becomes the families and words the console
 * draws, the 7-day hourly series, the limited stretches the chart shades, and
 * the incidents proposal D groups events into. The account sheet's Health
 * section (AccountSheet.tsx) reads it, and so does the Account health page:
 * THE INCIDENT RULE IS WRITTEN ONCE, HERE, because it is the console's and not
 * the broker's, and two copies of a grouping rule are two answers to "how many
 * incidents did this account have".
 */
import type { AccountHealthEvent, AccountHealthHour, AccountHealthKind, AccountHealthStatus, AccountHealthWindow } from './api'

export const HOUR_MS = 3_600_000
/** The series' length: the record's 7 days, an hour a bar. */
export const SERIES_HOURS = 168
/** How many events the broker keeps per account (`accountstore`'s cap). */
export const RECORD_KEEPS = 50

/**
 * THREE FAMILIES, FIXED TO THE KIND AND NEVER TO RANK (mock-ups A and D):
 * provider errors, limits, credential. A family has a shape as well as a
 * colour (circle, diamond, square) so it survives greyscale.
 */
export type HealthFamily = 'err' | 'lim' | 'cred'

export const FAMILIES: readonly { f: HealthFamily; label: string }[] = [
  { f: 'err', label: 'Provider errors' },
  { f: 'lim', label: 'Limits' },
  { f: 'cred', label: 'Credential' },
]

/** Each kind's family, its row label, and its singular and plural for a sentence; in the counts table's order. */
export const KINDS: readonly { k: AccountHealthKind; fam: HealthFamily; label: string; one: string; pl: string }[] = [
  { k: 'overloaded', fam: 'err', label: 'Overloaded (529)', one: 'overloaded (529)', pl: 'overloaded (529)' },
  { k: 'server_error', fam: 'err', label: 'Server error (5xx)', one: 'server error (5xx)', pl: 'server errors (5xx)' },
  { k: 'network', fam: 'err', label: 'Network error', one: 'network error', pl: 'network errors' },
  { k: 'rate_limit', fam: 'lim', label: 'Rate-limit hit', one: 'rate-limit hit', pl: 'rate-limit hits' },
  { k: 'exhausted', fam: 'lim', label: 'Exhausted report', one: 'exhausted report', pl: 'exhausted reports' },
  { k: 'assign_refused', fam: 'lim', label: 'Refused at assignment', one: 'refusal at assignment', pl: 'refusals at assignment' },
  { k: 'auth', fam: 'cred', label: 'Auth refused (401)', one: 'auth refusal (401)', pl: 'auth refusals (401)' },
  { k: 'refresh_failed', fam: 'cred', label: 'Credential refresh failed', one: 'failed credential refresh', pl: 'failed credential refreshes' },
  { k: 'reauth', fam: 'cred', label: 'Re-auth marked', one: 're-auth mark', pl: 're-auth marks' },
]

const KIND = new Map(KINDS.map((k) => [k.k, k]))

export function familyOf(kind: AccountHealthKind): HealthFamily {
  return KIND.get(kind)?.fam ?? 'err'
}

export function kindLabel(kind: AccountHealthKind): string {
  return KIND.get(kind)?.label ?? kind
}

/** "3 rate-limit hits", "1 rate-limit hit". */
export function kindCount(kind: AccountHealthKind, n: number): string {
  const k = KIND.get(kind)
  if (k === undefined) return `${n} ${kind}`
  return `${n} ${n === 1 ? k.one : k.pl}`
}

export const STATUS_WORD: Record<AccountHealthStatus, string> = {
  healthy: 'Healthy',
  limited: 'Limited',
  erroring: 'Erroring',
  reauth_needed: 'Needs re-auth',
}

export function windowWord(w: AccountHealthWindow | null): string {
  return w === 'five_hour' ? '5-hour' : w === 'seven_day' ? '7-day' : w === 'other' ? 'other' : ''
}

// ---------------------------------------------------------------------------
// The series and its shaded stretches
// ---------------------------------------------------------------------------

export interface HourBar {
  /** The hour's start, ms. */
  t: number
  err: number
  lim: number
  cred: number
  n: number
}

/**
 * The 168 hours ending with the hour that holds `now`, oldest first, each
 * split by family. Built from the served `hourly` (which may omit a quiet
 * hour), never from the events: the series counts events older than the last
 * 50, which the events list no longer holds.
 */
export function hourlyBars(hourly: readonly AccountHealthHour[], now: number): HourBar[] {
  const end = now - (now % HOUR_MS)
  const start = end - (SERIES_HOURS - 1) * HOUR_MS
  const bars: HourBar[] = []
  for (let i = 0; i < SERIES_HOURS; i++) bars.push({ t: start + i * HOUR_MS, err: 0, lim: 0, cred: 0, n: 0 })
  for (const h of hourly) {
    const t = Date.parse(h.hour)
    const i = Math.floor((t - start) / HOUR_MS)
    if (i < 0 || i >= SERIES_HOURS) continue
    const bar = bars[i] as HourBar
    for (const k of KINDS) {
      const n = h.counts[k.k] ?? 0
      bar[k.fam] += n
      bar.n += n
    }
  }
  return bars
}

export interface LimitedBand {
  from: number
  to: number
  window: AccountHealthWindow | null
  hits: number
}

/**
 * THE ACCOUNT'S LIMITED STRETCHES: from each rate-limit hit to its window's
 * reset. A hit inside a stretch of the same window joins it (and may carry it
 * further). `limited_until` past the last stretch's end -- the broker's own
 * record of a hit whose event aged out of the last 50 -- runs from now.
 */
export function limitedBands(
  events: readonly AccountHealthEvent[],
  limitedUntil: string | null,
  now: number,
): LimitedBand[] {
  const out: LimitedBand[] = []
  const hits = events
    .filter((e) => e.kind === 'rate_limit' && e.reset_at !== null)
    .map((e) => ({ at: Date.parse(e.at), to: Date.parse(e.reset_at as string), w: e.window }))
    .filter((h) => !Number.isNaN(h.at) && !Number.isNaN(h.to) && h.to > h.at)
    .sort((x, y) => x.at - y.at)
  for (const h of hits) {
    const last = out[out.length - 1]
    if (last !== undefined && last.window === h.w && h.at <= last.to) {
      last.hits += 1
      if (h.to > last.to) last.to = h.to
      continue
    }
    out.push({ from: h.at, to: h.to, window: h.w, hits: 1 })
  }
  const until = limitedUntil === null ? NaN : Date.parse(limitedUntil)
  if (!Number.isNaN(until) && until > now && !out.some((b) => b.from <= now && b.to >= until)) {
    out.push({ from: now, to: until, window: null, hits: 0 })
  }
  return out
}

// ---------------------------------------------------------------------------
// Incidents (proposal D, picked for the phone and the Account health page)
// ---------------------------------------------------------------------------
//
// THE RULE, written down because it is the console's (mock-up D, "What it
// reads"), and tested in accountHealth.test.ts:
//
//   1. Events are taken oldest first, from the record's last 50.
//   2. A refusal at assignment with NO window is the broker withholding an
//      account that needs re-auth, so it is a credential event. Every other
//      kind keeps its family.
//   3. An event joins the newest incident of its family when
//        * limits: it comes at or before that incident's reset (the latest
//          `reset_at` any of its events carried); a limit incident with no
//          reset joins nothing;
//        * errors and credential: it comes within 45 minutes of that
//          incident's last event.
//      Otherwise it opens a new incident.
//   4. Open or closed:
//        * a limit is open while its reset is still ahead;
//        * only the NEWEST error or credential incident can be open: a
//          credential one while the account's status is reauth_needed, an
//          error one while it is erroring and its last event is under an hour
//          old.
//   5. Every event of the last 50 sits in exactly one incident. Returned
//      newest first.

/** An error or credential event further than this from the last one starts a new incident. */
export const INCIDENT_GAP_MS = 45 * 60_000
/** An error incident whose last event is older than this is closed, whatever the status. */
export const ERROR_OPEN_MS = HOUR_MS

export interface Incident {
  fam: HealthFamily
  /** First and last event, ms. */
  from: number
  last: number
  /** A limit's reset, ms; null for the other families. */
  to: number | null
  window: AccountHealthWindow | null
  /** Oldest first. */
  events: AccountHealthEvent[]
  open: boolean
}

export function incidentFamily(e: AccountHealthEvent): HealthFamily {
  return e.kind === 'assign_refused' && e.window === null ? 'cred' : familyOf(e.kind)
}

export function incidentsOf(
  events: readonly AccountHealthEvent[],
  status: AccountHealthStatus | null,
  now: number,
): Incident[] {
  const list = events
    .slice(0, RECORD_KEEPS)
    .filter((e) => !Number.isNaN(Date.parse(e.at)))
    .sort((x, y) => Date.parse(x.at) - Date.parse(y.at))
  const out: Incident[] = []
  for (const e of list) {
    const at = Date.parse(e.at)
    const fam = incidentFamily(e)
    const reset = e.reset_at === null ? NaN : Date.parse(e.reset_at)
    let cur: Incident | undefined
    for (let i = out.length - 1; i >= 0 && cur === undefined; i--) {
      if (out[i]?.fam === fam) cur = out[i]
    }
    const joins =
      cur !== undefined && (fam === 'lim' ? cur.to !== null && at <= cur.to : at - cur.last <= INCIDENT_GAP_MS)
    if (cur !== undefined && joins) {
      cur.events.push(e)
      cur.last = at
      if (fam === 'lim' && !Number.isNaN(reset) && (cur.to === null || reset > cur.to)) cur.to = reset
      continue
    }
    out.push({
      fam,
      from: at,
      last: at,
      to: fam === 'lim' && !Number.isNaN(reset) ? reset : null,
      window: e.window,
      events: [e],
      open: false,
    })
  }
  const newest = new Map<HealthFamily, Incident>()
  for (const x of out) newest.set(x.fam, x)
  for (const x of out) {
    if (x.fam === 'lim') x.open = x.to !== null && x.to > now
    else if (newest.get(x.fam) !== x) x.open = false
    else if (x.fam === 'cred') x.open = status === 'reauth_needed'
    else x.open = status === 'erroring' && now - x.last < ERROR_OPEN_MS
  }
  return out.reverse()
}

/** "3 rate-limit hits · 1 exhausted report", in the counts table's kind order. */
export function incidentSummary(x: Incident): string {
  const c = new Map<AccountHealthKind, number>()
  for (const e of x.events) c.set(e.kind, (c.get(e.kind) ?? 0) + 1)
  return KINDS.filter((k) => c.has(k.k))
    .map((k) => kindCount(k.k, c.get(k.k) as number))
    .join(' · ')
}

/** The incident's title, in mock-up D's words. */
export function incidentTitle(x: Incident): string {
  if (x.fam === 'lim') return x.window !== null && x.window !== 'other' ? `Rate-limited on its ${windowWord(x.window)} window` : 'Limited'
  if (x.fam === 'cred' && x.open) return 'Needs re-auth'
  const only = x.events.length === 1 ? x.events[0] : undefined
  if (only !== undefined) return `One ${kindLabel(only.kind).toLowerCase()}`
  if (x.fam === 'cred') return `${x.events.length} credential events`
  return `Erroring: ${x.events.length} provider errors`
}
