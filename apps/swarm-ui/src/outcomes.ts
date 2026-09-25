// THE OUTCOME LEDGER'S CONTRACT AND ITS PURE HALF (#185).
//
// `GET /v1/outcomes` is the one aggregate the Timeline reads. Its response is
// typed here field for field from the contract the API lane builds against
// (swarm_api/outcomes.py), and nothing in this module computes a figure the
// server serves: the rate, its Wilson interval, the totals, the failure
// classes and the percentiles all arrive computed. Restating any of them here
// would be the mirrored copy this repository keeps paying for -- the server is
// the one implementation, and the UI draws what it says.
//
// What IS here, and why it is pure:
//
//   * the VIEW -- every filter the page has -- as a hash query and back, so a
//     link reproduces the page and the tests can drive it without a DOM;
//   * the view as the route's query string, so the page cannot ask for a
//     parameter combination the contract refuses;
//   * the words a bucket is read out as, in the zone the server bucketed in
//     (the response's `tz`, never the browser's guess at it);
//   * which axis ticks carry a label, at each drawing's width (TS-2, TS-3).
//
// NULL MEANS NOT KNOWN, NEVER ZERO. Every nullable field below is nullable in
// the contract for that reason, and no helper here turns one into a 0.

// ---------------------------------------------------------------------------
// The response
// ---------------------------------------------------------------------------

export type LedgerBucketSize = 'hour' | 'day' | 'week' | 'month'

/** k succeeded of n decided, with its Wilson 95 % interval. null when n = 0. */
export interface Rate {
  k: number
  n: number
  p: number
  lo: number
  hi: number
}

export interface CancelSplit {
  total: number
  requested: number
  after_failure: number
  workflow_sweep: number
  other: number
}

export interface CostCell {
  /** null when no attempt reported a cost: an absence, never $0.00. */
  sum_usd: number | null
  attempts: number
  reporting: number
}

export type FailureClassKey =
  | 'runner_error'
  | 'timeout'
  | 'lost_worker'
  | 'could_not_start'
  | 'outputs_missing'
  | 'dispatch_failed'
  | 'other'
  | 'no_reason'

export type CancelCauseKey = 'requested' | 'after_failure' | 'workflow_sweep' | 'other'

export type BucketState = 'sealed' | 'open' | 'unread'

export type UnreadReason = 'derive_budget' | 'read_failed' | 'too_large'

/** One bucket. When `state` is unread, every count below is null. */
export interface OutcomeBucket {
  start: string
  end: string
  state: BucketState
  /** end > generated_at: the current period, drawn as "so far". */
  in_progress: boolean
  unread_reason: UnreadReason | null
  /** By created_at: the ONLY submission-time field on a bucket. */
  submitted: number | null
  ended: number | null
  succeeded: number | null
  failed: number | null
  dead_lettered: number | null
  cancelled: CancelSplit | null
  rate: Rate | null
  failure_classes: Record<FailureClassKey, number> | null
  cost: CostCell | null
}

export interface Stat {
  n: number
  p50_s: number
  p95_s: number
  max_s: number
  /** Only when n < 5: the values themselves, because a percentile of four is a guess. */
  values_s: number[] | null
}

export interface LatencyOutcome {
  n: number
  wait: Stat | null
  run: Stat | null
  total: Stat | null
}

export interface LatencyRow {
  runner_profile: string
  /** null when the row's tasks do not share one timeout_seconds. */
  timeout_s: number | null
  succeeded: LatencyOutcome
  /** Includes dead-lettered. */
  failed: LatencyOutcome
}

export type GroupBy = 'runner_profile' | 'submitted_by' | 'tenant_id'

export interface GroupRow {
  key: string
  submitted: number
  ended: number
  succeeded: number
  failed: number
  dead_lettered: number
  cancelled: CancelSplit
  rate: Rate | null
  cost: CostCell
  declared_cost: boolean
  /** Aligned 1:1 with buckets; a null entry is an unread bucket. The whole field is null past 120 buckets. */
  series: Array<{ k: number; n: number } | null> | null
}

export interface WorkflowFailedRow {
  workflow_id: string
  tenant_id: string
  submitted_by: string | null
  first_failed: { step_id: string; task_id: string; failure_class: FailureClassKey; ended_at: string } | null
  cascade_cancelled: number
  last_ended_at: string
  state: string
  steps: {
    total: number
    succeeded: number
    failed: number
    dead_lettered: number
    cancelled: number
    open: number
    unreadable: number
  } | null
}

export interface OutcomeTotals {
  complete: boolean
  buckets: number
  buckets_read: number
  submitted: number
  ended: number
  succeeded: number
  failed: number
  dead_lettered: number
  cancelled: CancelSplit
  rate: Rate | null
  failure_classes: Record<FailureClassKey, number>
  cost: CostCell & {
    by_outcome: Record<'succeeded' | 'failed' | 'dead_lettered' | 'cancelled', CostCell>
    per_succeeded_task: {
      n: number
      of: number
      p50_usd: number | null
      p95_usd: number | null
      max_usd: number | null
      values_usd: number[] | null
    }
    retries: CostCell
    declared: CostCell & { profiles: string[] }
  }
}

export type OutcomeScope =
  | { kind: 'tenant'; tenant_id: string }
  | { kind: 'platform'; tenants: string[]; excluded: string[]; tenants_complete: boolean }

export interface Outcomes {
  scope: OutcomeScope
  tz: string
  requested: { span: string | null; since: string | null; until: string | null; bucket: string }
  since: string
  until: string
  bucket: LedgerBucketSize
  bucket_chosen_by: 'server' | 'caller'
  basis: { outcomes: string; submitted: string }
  filters: {
    profile: string[]
    submitted_by: string[]
    kind: string
    tenant: string[]
    exclude_tenant: string[]
    group: GroupBy
  }
  vocab: {
    failure_classes: Array<{ key: FailureClassKey; label: string }>
    cancel_causes: Array<{ key: CancelCauseKey; label: string }>
    classifier_version: number
  }
  buckets: OutcomeBucket[]
  totals: OutcomeTotals
  retries: {
    tries: Array<{
      attempts: string
      tasks: number
      succeeded: number
      failed: number
      dead_lettered: number
      cancelled: number
    }>
    needed_retry: { k: number; of: number }
    rescued: number
    failed_after_retry: number
    not_final: { attempts: number; by_exit: Array<{ exit_code: number | null; label: string; n: number }> }
    admissions_without_attempt_doc: number
  }
  latency: { percentile_method: string; by_profile: LatencyRow[] }
  groups: { by: GroupBy; rows_total: number; rows: GroupRow[] }
  workflows_failed: {
    applicable: boolean
    with_ended_steps: number
    with_failed_steps: number
    rows_total: number
    rows: WorkflowFailedRow[]
    failing_steps: Array<{ step_id: string; n: number }>
  }
  coverage: {
    days: { total: number; sealed: number; live: number; unread: number }
    unread: Array<{ tenant_id: string; day: string; reason: UnreadReason }>
    derived_now: number
    built_through: string | null
    reopened: number
    /** null when the count query failed: not read, never 0. */
    terminal_without_completed_at: number | null
    wait_excluded: number
    seal_grace_s: number
  }
  previous: {
    since: string
    until: string
    complete: boolean
    succeeded: number
    failed: number
    dead_lettered: number
    cancelled_total: number
    rate: Rate | null
  } | null
  reads: number
  cached: boolean
  generated_at: string
}

// ---------------------------------------------------------------------------
// The view: every filter, as the hash carries it
// ---------------------------------------------------------------------------

export const SPANS = ['24h', '7d', '14d', '30d', '90d'] as const
export type Span = (typeof SPANS)[number]

/**
 * 14 DAYS, THE OWNER'S DEFAULT (#185, 2026-09-25). Long enough that a quiet
 * week and a busy one are both on the page, short enough that every bucket is
 * a day with a readable label at 1440.
 */
export const DEFAULT_SPAN: Span = '14d'

export const BUCKET_CHOICES = ['auto', 'hour', 'day', 'week', 'month'] as const
export type BucketChoice = (typeof BUCKET_CHOICES)[number]

export const KINDS = ['all', 'standalone', 'steps'] as const
export type Kind = (typeof KINDS)[number]

export const GROUPS: readonly GroupBy[] = ['runner_profile', 'submitted_by', 'tenant_id']

/**
 * THE ONE TENANT THE TOOLBAR NAMES, and only as a one-click exclusion (owner
 * decision on #185: "the verify tenant is included, with a one-click
 * exclude"). The smoke and verification runs submit under it -- 390 of dev's
 * 730 tasks on 25 Sep -- so in platform scope it can outweigh every real
 * tenant. The button is drawn only when the admin tenant listing actually
 * serves a tenant by this id, and it sends `exclude_tenant`, not an include
 * list, so a tenant created tomorrow is still counted.
 */
export const VERIFY_TENANT = 'verify'

export interface LedgerView {
  /** null when an explicit range is set. */
  span: Span | null
  /** YYYY-MM-DD or an ISO instant with its offset, exactly as sent. */
  since: string | null
  /** Exclusive, as the route reads it. */
  until: string | null
  bucket: BucketChoice
  platform: boolean
  tenant: string[]
  exclude_tenant: string[]
  profile: string[]
  submitted_by: string[]
  kind: Kind
  group: GroupBy
  table: boolean
  /** The view a zoom came from, as its own query string, so the chip can go back. */
  back: string | null
}

export const DEFAULT_VIEW: LedgerView = {
  span: DEFAULT_SPAN,
  since: null,
  until: null,
  bucket: 'auto',
  platform: false,
  tenant: [],
  exclude_tenant: [],
  profile: [],
  submitted_by: [],
  kind: 'all',
  group: 'runner_profile',
  table: false,
  back: null,
}

const isSpan = (s: string | null): s is Span => (SPANS as readonly string[]).includes(s ?? '')
const isBucket = (s: string | null): s is BucketChoice => (BUCKET_CHOICES as readonly string[]).includes(s ?? '')
const isKind = (s: string | null): s is Kind => (KINDS as readonly string[]).includes(s ?? '')
const isGroup = (s: string | null): s is GroupBy => (GROUPS as readonly string[]).includes(s ?? '')

/** A tenant id is [a-z0-9-] (identity._TENANT_SAFE); anything else never reaches the route. */
const TENANT_ID = /^[a-z0-9-]{1,63}$/
const PROFILE_NAME = /^[a-z0-9][a-z0-9._-]{0,62}$/

function uniq(values: readonly string[]): string[] {
  return Array.from(new Set(values)).sort()
}

/**
 * The hash's query, read into a view. Anything the page could not send is
 * dropped rather than guessed at: an unknown span falls back to the default,
 * a range needs both ends, and the tenant filters exist only in platform scope.
 */
export function parseView(query: string | null | undefined): LedgerView {
  const q = new URLSearchParams((query ?? '').replace(/^\?/, ''))
  const since = q.get('since')
  const until = q.get('until')
  const range = since !== null && since !== '' && until !== null && until !== ''
  const span = q.get('span')
  const bucket = q.get('bucket')
  const kind = q.get('kind')
  const group = q.get('group')
  const tenant = uniq(q.getAll('tenant').filter((t) => TENANT_ID.test(t)))
  const exclude = uniq(q.getAll('exclude_tenant').filter((t) => TENANT_ID.test(t)))
  // Tenant and exclude_tenant are mutually exclusive in the contract; an
  // include list wins, because it is the narrower of the two claims.
  const platform = q.get('scope') === 'platform' || tenant.length > 0 || exclude.length > 0
  const view: LedgerView = {
    span: range ? null : isSpan(span) ? span : DEFAULT_SPAN,
    since: range ? since : null,
    until: range ? until : null,
    bucket: isBucket(bucket) ? bucket : 'auto',
    platform,
    tenant: platform ? tenant : [],
    exclude_tenant: platform && tenant.length === 0 ? exclude : [],
    profile: uniq(q.getAll('profile').filter((p) => PROFILE_NAME.test(p))),
    submitted_by: uniq(q.getAll('submitted_by').map((s) => s.trim().toLowerCase()).filter((s) => s.includes('@'))),
    kind: isKind(kind) ? kind : 'all',
    group: isGroup(group) && (group !== 'tenant_id' || platform) ? group : 'runner_profile',
    table: q.get('table') === '1',
    back: q.get('back'),
  }
  return monthAllowed(view) ? view : { ...view, bucket: 'auto' }
}

/**
 * The view as the hash writes it: canonical order, defaults left out, so the
 * default page is plain `#work/timeline` and two routes to one view produce
 * one address.
 */
export function serializeView(v: LedgerView): string {
  const q = new URLSearchParams()
  if (v.since !== null && v.until !== null) {
    q.set('since', v.since)
    q.set('until', v.until)
  } else if (v.span !== null && v.span !== DEFAULT_SPAN) {
    q.set('span', v.span)
  }
  if (v.bucket !== 'auto') q.set('bucket', v.bucket)
  if (v.platform && v.tenant.length === 0 && v.exclude_tenant.length === 0) q.set('scope', 'platform')
  for (const t of v.platform ? v.tenant : []) q.append('tenant', t)
  for (const t of v.platform && v.tenant.length === 0 ? v.exclude_tenant : []) q.append('exclude_tenant', t)
  for (const p of v.profile) q.append('profile', p)
  for (const s of v.submitted_by) q.append('submitted_by', s)
  if (v.kind !== 'all') q.set('kind', v.kind)
  if (v.group !== 'runner_profile') q.set('group', v.group)
  if (v.table) q.set('table', '1')
  if (v.back !== null) q.set('back', v.back)
  return q.toString()
}

/** Days the view's range covers, or null when it cannot be known without the server. */
export function viewDays(v: LedgerView): number | null {
  if (v.span !== null) return v.span === '24h' ? 1 : Number(v.span.replace('d', ''))
  if (v.since === null || v.until === null) return null
  const a = Date.parse(v.since)
  const b = Date.parse(v.until)
  return Number.isFinite(a) && Number.isFinite(b) ? (b - a) / 86_400_000 : null
}

/** Month buckets over a span under 60 days draw one lonely column: the route refuses it (422), so the page never asks. */
export function monthAllowed(v: LedgerView): boolean {
  if (v.bucket !== 'month') return true
  const d = viewDays(v)
  return d !== null && d >= 60
}

/** At most 2,000 buckets: an hourly 90 days is 2,160, which the route refuses. */
export function hourAllowed(v: LedgerView): boolean {
  const d = viewDays(v)
  return d === null || d * 24 <= 2000
}

/**
 * The route's query for a view. `tz` is REQUIRED by the contract and is the
 * viewer's IANA zone; `compare=previous` is always asked for, because the
 * headline's delta needs the span before this one and the route prices it in.
 * The tenant filters go only in platform scope: in tenant scope the route
 * refuses them (422 `conflicting_scope`) and would name no tenant anyway.
 */
export function outcomesQuery(v: LedgerView, tz: string): URLSearchParams {
  const q = new URLSearchParams()
  q.set('tz', tz)
  if (v.since !== null && v.until !== null) {
    q.set('since', v.since)
    q.set('until', v.until)
  } else {
    q.set('span', v.span ?? DEFAULT_SPAN)
  }
  q.set('bucket', monthAllowed(v) && (v.bucket !== 'hour' || hourAllowed(v)) ? v.bucket : 'auto')
  if (v.platform) {
    q.set('scope', 'platform')
    for (const t of v.tenant) q.append('tenant', t)
    if (v.tenant.length === 0) for (const t of v.exclude_tenant) q.append('exclude_tenant', t)
  }
  for (const p of v.profile) q.append('profile', p)
  for (const s of v.submitted_by) q.append('submitted_by', s)
  if (v.kind !== 'all') q.set('kind', v.kind)
  q.set('group', v.platform || v.group !== 'tenant_id' ? v.group : 'runner_profile')
  q.set('compare', 'previous')
  return q
}

/** The number of secondary filters set, for the phone's `Filters · N`. */
export function filtersSet(v: LedgerView): number {
  return (
    (v.bucket !== 'auto' ? 1 : 0) +
    (v.platform ? 1 : 0) +
    (v.tenant.length > 0 || v.exclude_tenant.length > 0 ? 1 : 0) +
    (v.profile.length > 0 ? 1 : 0) +
    (v.submitted_by.length > 0 ? 1 : 0) +
    (v.kind !== 'all' ? 1 : 0)
  )
}

/** The span a view reads, in words: `14d`, or the range's two ends. */
export function spanWords(v: LedgerView): string {
  if (v.span !== null) return v.span
  return 'this range'
}

// ---------------------------------------------------------------------------
// Figures as words
// ---------------------------------------------------------------------------

/** A proportion as a percentage with one decimal and a space before the sign: `90.7 %`. */
export function pct(p: number): string {
  return `${(p * 100).toFixed(1)} %`
}

/** A rate's interval, `86.8–93.5 %`. */
export function interval(r: Rate): string {
  return `${(r.lo * 100).toFixed(1)}–${(r.hi * 100).toFixed(1)} %`
}

/** The whole rate in one clause: `90.7 % (272 of 300 · 95 % 86.8–93.5 %)`. */
export function rateClause(r: Rate): string {
  return `${pct(r.p)} (${r.k} of ${r.n} · 95 % ${interval(r)})`
}

/** A point is filled at five decided or more; below that it is hollow, so 1 of 2 never reads as an outage. */
export const LOW_N = 5

/** Money as the contract serves it (USD), for a person: never a `$0.00` for an absence -- the caller checks for null first. */
export function money(n: number): string {
  if (n === 0) return '$0.00'
  if (Math.abs(n) < 0.01) return `$${n.toFixed(4)}`
  return `$${n.toFixed(2)}`
}

/** Seconds as the shortest honest duration: `48s`, `11m`, `2h 5m`, `1d 3h`. */
export function secs(s: number): string {
  const v = Math.max(0, Math.round(s))
  if (v < 60) return `${v}s`
  const m = Math.floor(v / 60)
  if (m < 60) return v % 60 === 0 || m >= 10 ? `${m}m` : `${m}m ${v % 60}s`
  const h = Math.floor(m / 60)
  if (h < 24) return m % 60 === 0 ? `${h}h` : `${h}h ${m % 60}m`
  const d = Math.floor(h / 24)
  return h % 24 === 0 ? `${d}d` : `${d}d ${h % 24}h`
}

// ---------------------------------------------------------------------------
// Time, in the zone the server bucketed in
// ---------------------------------------------------------------------------

/** The browser's IANA zone, which the route requires; 'UTC' when the runtime will not say. */
export function viewerZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
  } catch {
    return 'UTC'
  }
}

/** A bucket start's wall clock in `tz`: its calendar date and its hour. */
export interface Wall {
  /** `2026-09-21`, the date on the zone's calendar. */
  date: string
  hour: number
}

const wallCache = new Map<string, Intl.DateTimeFormat>()

function wallFormat(tz: string): Intl.DateTimeFormat {
  let f = wallCache.get(tz)
  if (f === undefined) {
    f = new Intl.DateTimeFormat('en-CA', {
      timeZone: tz,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      hourCycle: 'h23',
    })
    wallCache.set(tz, f)
  }
  return f
}

export function wallOf(iso: string, tz: string): Wall {
  const parts = wallFormat(tz).formatToParts(new Date(iso))
  const get = (t: string) => parts.find((p) => p.type === t)?.value ?? ''
  return { date: `${get('year')}-${get('month')}-${get('day')}`, hour: Number(get('hour')) % 24 }
}

function fmt(iso: string, tz: string, opts: Intl.DateTimeFormatOptions): string {
  return new Date(iso).toLocaleString(undefined, { timeZone: tz, ...opts })
}

/** The date an axis prints: `Sep 21` (or the locale's order). */
export function dateLabel(iso: string, tz: string): string {
  return fmt(iso, tz, { day: 'numeric', month: 'short' })
}

/** An hour as an axis prints it: `08 PM` (or the locale's). */
export function hourLabel(iso: string, tz: string): string {
  return fmt(iso, tz, { hour: '2-digit' })
}

/** The label an axis tick carries for a bucket, before any thinning. */
export function tickLabel(iso: string, bucket: LedgerBucketSize, tz: string): string {
  if (bucket === 'hour') return hourLabel(iso, tz)
  if (bucket === 'month') return fmt(iso, tz, { month: 'short' })
  return dateLabel(iso, tz)
}

/**
 * A bucket's FULL name, for the readout and a column's accessible name (TS-9):
 * the date and hour of an hour, the date and weekday of a day, `week of` its
 * Monday, a month with its year. Never the axis's thinned label.
 */
export function bucketName(iso: string, bucket: LedgerBucketSize, tz: string): string {
  if (bucket === 'hour') return `${dateLabel(iso, tz)} ${hourLabel(iso, tz)}`
  if (bucket === 'day') return `${dateLabel(iso, tz)} · ${fmt(iso, tz, { weekday: 'short' })}`
  if (bucket === 'week') return `week of ${dateLabel(iso, tz)}`
  return fmt(iso, tz, { month: 'long', year: 'numeric' })
}

/** A short instant for the facts line: `Sep 12, 00:00`. */
export function instantLabel(iso: string, tz: string): string {
  return fmt(iso, tz, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' })
}

/**
 * Which columns of an hourly axis begin a day on the zone's calendar: the
 * first column, and every one whose date differs from the one before it.
 * Nothing longer than an hour starts a day part-way through an axis.
 */
export function dayStarts(walls: readonly Wall[], bucket: LedgerBucketSize): boolean[] {
  return walls.map((w, i) => bucket === 'hour' && (i === 0 || walls[i - 1]!.date !== w.date))
}

/**
 * The labels under an axis, `''` where a tick carries none.
 *
 * `stride` is how many columns a label needs, from the drawing's column pitch.
 * TWO RULES FROM TS-2 SURVIVE EVERY WIDTH: an hour is not a time without its
 * day, so the first column of each day carries the DATE and is always
 * labelled; and no label lands within a stride before a day's date, so a date
 * never has a neighbour's label pressed against it.
 *
 * `clock` (TS-3, owner decision 2026-09-25): at phone width an hourly axis
 * labels every third hour ON THE CLOCK -- 00, 03, 06 -- whatever its length,
 * so the ticks read as a scale and not as an accident of where the span began.
 */
export function axisLabels(
  starts: readonly string[],
  bucket: LedgerBucketSize,
  tz: string,
  stride: number,
  clock: number | null = null,
): string[] {
  const walls = starts.map((s) => wallOf(s, tz))
  const day = dayStarts(walls, bucket)
  const step = Math.max(1, Math.ceil(stride))
  const dayWithin = (i: number): boolean => {
    for (let j = 1; j < step; j++) if (day[i + j] === true) return true
    return false
  }
  const out = starts.map(() => '')
  let last = -Infinity
  starts.forEach((s, i) => {
    if (day[i] === true) {
      // The chart's first column starts a day only because the chart starts
      // there; when the real boundary is within a stride, the boundary wins.
      if (i === 0 && step > 1 && dayWithin(0)) return
      out[i] = dateLabel(s, tz)
      last = i
      return
    }
    if (clock !== null && bucket === 'hour' && walls[i]!.hour % clock !== 0) return
    if (i - last < step) return
    if (step > 1 && dayWithin(i)) return
    out[i] = tickLabel(s, bucket, tz)
    last = i
  })
  return out
}

// ---------------------------------------------------------------------------
// A bucket as a sentence
// ---------------------------------------------------------------------------

/** Why a bucket was not read, in the reader's words. */
export function unreadWords(r: UnreadReason | null): string {
  if (r === 'derive_budget') return 'not built yet: past this read’s budget'
  if (r === 'too_large') return 'too large to read'
  if (r === 'read_failed') return 'the read failed'
  return 'not read'
}

/** Whether a sealed-or-open bucket measured nothing at all -- a MEASURED zero, drawn as the axis tick. */
export function measuredNothing(b: OutcomeBucket): boolean {
  return b.state !== 'unread' && (b.ended ?? 0) === 0 && (b.submitted ?? 0) === 0
}

/**
 * The accessible name of a bucket's column: its full name and every count,
 * or `not read` and the reason with no count at all.
 */
export function bucketSaid(b: OutcomeBucket, bucket: LedgerBucketSize, tz: string): string {
  const name = bucketName(b.start, bucket, tz)
  if (b.state === 'unread') return `${name}: not read, ${unreadWords(b.unread_reason)}. No count is shown for it.`
  const c = b.cancelled
  const parts = [
    `${b.succeeded ?? 0} succeeded`,
    `${(b.failed ?? 0) + (b.dead_lettered ?? 0)} failed`,
    b.rate === null ? 'no rate, nothing decided' : `rate ${pct(b.rate.p)} (${b.rate.k} of ${b.rate.n}, 95 % ${interval(b.rate)})`,
    `${c?.total ?? 0} cancelled, ${(c?.after_failure ?? 0) + (c?.workflow_sweep ?? 0)} after a failure`,
    `${b.submitted ?? 0} submitted`,
  ]
  const when = b.in_progress ? ', so far' : b.state === 'open' ? ', still settling' : ''
  return `${name}${when}: ${parts.join(', ')}`
}
