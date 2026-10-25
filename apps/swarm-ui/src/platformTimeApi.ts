/*
 * ADMIN › PLATFORM COUNTS, OVER TIME: THE READ (COUNTS-TIME-VIEW, #2149).
 *
 * One read of `GET /v1/outcomes` in platform scope with `usage=submitted`, the
 * v9 `usage` block COUNTS-ROLLUP added (docs/outcomes.md, "Agents over time").
 * BGD's decisions, 2026-10-23: the unit is agents counted on the day they were
 * SUBMITTED, with their retries a second figure in the same bucket; the
 * breakdowns are agent type, model, workspace and pool account; cost is
 * "Estimated cost (list price, not a bill)" with its coverage.
 *
 * WHY ITS OWN MODULE AND NOT `loadOutcomes`. The Timeline's query builder
 * (`outcomesQuery`, outcomes.ts) asks for `compare=previous` and the Timeline's
 * groups only, and its `Outcomes` type has no `usage`. This view sends none of
 * the Timeline's sections it does not draw (`section=totals`, the cheapest, is
 * the one it asks for), so the route folds the same days and serves the usage
 * projection of them -- which costs no read of its own (`usage` is not part
 * of the cache key) -- and nothing else.
 *
 * NOTHING HERE RUNS ON ITS OWN. The screen calls `loadPlatformTime` from its
 * Run button and nowhere else: the read is billed by the days it covers, and
 * the page's decided rule is a priced button, never a refresh.
 */

import { USE_FIXTURES, FIXTURE_CLOCK } from './api'
import { read, route, type Result } from './fetch'
import { viewerZone } from './outcomes'

/** The three bucket sizes this view offers, each over BGD's span (#2149): 30 days, 26 weeks, 12 months. */
export type TimeBucket = 'day' | 'week' | 'month'

export const TIME_BUCKETS: ReadonlyArray<{ key: TimeBucket; label: string; span: string; count: number }> = [
  { key: 'day', label: 'Day', span: '30 days', count: 30 },
  { key: 'week', label: 'Week', span: '26 weeks', count: 26 },
  { key: 'month', label: 'Month', span: '12 months', count: 12 },
]

/**
 * THE BREAKDOWNS (BGD, 2026-10-23), each the route's `group`. `none` asks for
 * `runner_profile` -- the route always groups by something -- and draws no
 * group rows, so None and Agent type are one read: picking either after the
 * other draws at once, from the read already paid for.
 */
export type Breakdown = 'none' | 'runner_profile' | 'model' | 'tenant_id' | 'pool_account'

export const BREAKDOWNS: ReadonlyArray<{ key: Breakdown; group: string; label: string; plural: string; says: string }> = [
  { key: 'none', group: 'runner_profile', label: 'None', plural: 'buckets', says: 'One total per bucket.' },
  { key: 'runner_profile', group: 'runner_profile', label: 'Agent type', plural: 'agent types', says: 'Runner profile, as submitted.' },
  { key: 'model', group: 'model', label: 'Model', plural: 'models', says: 'The model the agent ran; “not ended” until it ends.' },
  { key: 'tenant_id', group: 'tenant_id', label: 'Workspace', plural: 'workspaces', says: 'The workspace the agent was submitted in.' },
  {
    key: 'pool_account',
    group: 'pool_account',
    label: 'Pool account',
    plural: 'pool accounts',
    says: 'The account its last attempt held, so an agent on two accounts counts once, on the last; “not ended” until it ends.',
  },
]

export function breakdownOf(key: Breakdown): (typeof BREAKDOWNS)[number] {
  return BREAKDOWNS.find((b) => b.key === key) ?? BREAKDOWNS[0]!
}

// ---------------------------------------------------------------------------
// The response: the `usage` block, as `swarm_api.outcomes._Usage.to_api` serves it
// ---------------------------------------------------------------------------

/** A sum beside how much of it was measured. `sum` is null when nothing reported: an absence, never 0. */
export interface TokenSum {
  sum: number | null
  reporting: number
  attempts: number
}

export type TokenKind = 'input_tokens' | 'output_tokens' | 'cache_read_input_tokens' | 'cache_creation_input_tokens'

export const TOKEN_KINDS: ReadonlyArray<{ key: TokenKind; label: string }> = [
  { key: 'input_tokens', label: 'Input' },
  { key: 'output_tokens', label: 'Output' },
  { key: 'cache_read_input_tokens', label: 'Cache read' },
  { key: 'cache_creation_input_tokens', label: 'Cache write' },
]

export interface UsageFigures {
  /** Agents placed here by `created_at`, each once. */
  agents: number
  /** Of those, the ones whose end was read: only they carry a measured figure. */
  agents_ended: number
  /** Attempts beyond each agent's first. */
  retries: number
  attempts: number
  /** Over the STARTED attempts; an attempt with no end is not measured, never 0 s. */
  run_s: { sum: number | null; measured: number; attempts: number }
  tokens: Record<TokenKind, TokenSum>
  /** `reporting` of `agents` is "k of n agents reported"; `estimated_usd` is INCLUDED in `sum_usd`. */
  cost: { sum_usd: number | null; estimated_usd: number | null; agents: number; reporting: number }
}

/** An unread bucket carries every figure null: never a partial sum. */
export type UnreadFigures = { [K in keyof UsageFigures]: null }

export type UsageBucket = {
  start: string
  end: string
  state: 'sealed' | 'open' | 'unread'
  in_progress: boolean
  unread_reason: 'derive_budget' | 'read_failed' | 'too_large' | null
} & (UsageFigures | UnreadFigures)

export type UsageGroupRow = { key: string } & UsageFigures

export interface PlatformUsage {
  basis: string
  cost_label: string
  buckets: UsageBucket[]
  totals: { complete: boolean } & UsageFigures
  groups: { by: string; rows_total: number; rows: UsageGroupRow[] }
}

/** The part of the outcomes envelope this view reads, and its usage block. */
export interface PlatformTime {
  scope:
    | { kind: 'tenant'; tenant_id: string }
    | { kind: 'platform'; tenants: string[]; excluded: string[]; tenants_complete: boolean }
  tz: string
  since: string
  until: string
  bucket: TimeBucket | 'hour'
  reads: number
  cached: boolean
  generated_at: string
  usage: PlatformUsage
}

/** A read bucket, narrowed: its figures are numbers. */
export function isRead(b: UsageBucket): b is UsageBucket & UsageFigures {
  return b.state !== 'unread' && b.agents !== null
}

// ---------------------------------------------------------------------------
// The query
// ---------------------------------------------------------------------------

/** `YYYY-MM-DD` of an instant on `tz`'s calendar. */
export function dayIn(ms: number, tz: string): string {
  const parts = new Intl.DateTimeFormat('en-CA', { timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(new Date(ms))
  const get = (t: string) => parts.find((p) => p.type === t)?.value ?? ''
  return `${get('year')}-${get('month')}-${get('day')}`
}

/**
 * THE SINCE FOR A SPAN, as a calendar day in the viewer's zone. The route owns
 * alignment (docs/outcomes.md, "Bucket boundaries"): it floors `since` to the
 * boundary that contains it, so the week view sends the day 25 weeks before
 * today (floored to that week's Monday: 26 weeks with this one) and the month
 * view the first of the month eleven months back (12 months with this one).
 * The day view sends `span=30d`, which the route already resolves.
 */
export function sinceFor(bucket: TimeBucket, now: number, tz: string): string | null {
  if (bucket === 'day') return null
  const today = dayIn(now, tz)
  if (bucket === 'week') {
    const back = Date.parse(`${today}T00:00:00Z`) - 25 * 7 * 86_400_000
    return new Date(back).toISOString().slice(0, 10)
  }
  const [y, m] = today.split('-').map(Number) as [number, number]
  const index = y * 12 + (m - 1) - 11
  return `${Math.floor(index / 12)}-${String((index % 12) + 1).padStart(2, '0')}-01`
}

/** The route's query for a bucket and a breakdown. `tz` is required by the contract: the viewer's zone. */
export function platformTimeQuery(bucket: TimeBucket, breakdown: Breakdown, tz: string, now: number = Date.now()): URLSearchParams {
  const q = new URLSearchParams()
  q.set('tz', tz)
  const since = sinceFor(bucket, now, tz)
  if (since === null) q.set('span', '30d')
  else q.set('since', since)
  q.set('bucket', bucket)
  q.set('scope', 'platform')
  q.set('group', breakdownOf(breakdown).group)
  q.set('usage', 'submitted')
  q.set('section', 'totals')
  return q
}

/** The read a bucket and breakdown share: None and Agent type send the same query, so they are one read. */
export function viewKey(bucket: TimeBucket, breakdown: Breakdown): string {
  return `${bucket}|${breakdownOf(breakdown).group}`
}

/** Calendar days a span covers, for its price. */
export function spanDays(bucket: TimeBucket, now: number, tz: string): number {
  const since = sinceFor(bucket, now, tz)
  if (since === null) return 30
  return Math.round((Date.parse(`${dayIn(now, tz)}T00:00:00Z`) - Date.parse(`${since}T00:00:00Z`)) / 86_400_000) + 1
}

/**
 * WHAT A RUN WILL COST, in Firestore reads, before it is pressed. The measured
 * warm read (docs/outcomes.md's table: one tenant, 30 days, 36 reads) is one
 * stored day document per UTC day the span touches -- one more than its days,
 * because the viewer's days straddle UTC's -- plus the live delta, plus four
 * counts. In platform scope the day documents are per workspace. A cold day
 * costs more (it is derived), and the figure says "~" for that reason: it is
 * the price of sealed days, which is what a backfilled platform reads.
 *
 * `workspaces` null: not known yet. The price is then per workspace, never a
 * guess at the platform's size presented as a total.
 */
export function readEstimate(days: number, workspaces: number | null): { total: number | null; perWorkspace: number } {
  const perWorkspace = days + 2
  return { total: workspaces === null ? null : perWorkspace * workspaces + 4, perWorkspace }
}

// ---------------------------------------------------------------------------
// The read
// ---------------------------------------------------------------------------

/**
 * One read of the usage view. THE ROUTE IS THE TEMPLATE (CH-18): the query goes
 * through `route()`'s third argument, so the probe registry keys this as
 * `/v1/outcomes` like the Timeline's reads. A 2xx is always `ok`: a span always
 * has buckets, and a bucket with nothing in it is a measured zero.
 */
export async function loadPlatformTime(query: URLSearchParams): Promise<Result<PlatformTime>> {
  if (USE_FIXTURES) return fixturePlatformTime(query)
  return read<PlatformTime>(route('/v1/outcomes', {}, query), () => false)
}

/**
 * THE DEV PREVIEW'S ANSWER: example data, never product data. Built from the
 * query, so the bucket and breakdown picked are the ones drawn; deterministic,
 * so a screenshot is the same twice. It has one unread bucket, an in-progress
 * one, partial cost and token coverage, a stopped-run estimate, and the mock
 * runner's measured $0.00, because those are the cases the view exists to draw
 * honestly. Profiles are the catalogue's; pool accounts are `acct-claude-*`.
 */
async function fixturePlatformTime(query: URLSearchParams): Promise<Result<PlatformTime>> {
  await new Promise((r) => setTimeout(r, 240))
  return { status: 'ok', fetchedAt: Date.now(), data: platformTimeExample(query, Date.parse(FIXTURE_CLOCK)) }
}

const EXAMPLE_GROUPS: Record<string, ReadonlyArray<[string, number]>> = {
  runner_profile: [['claude-code', 0.5], ['claude-code-large', 0.21], ['indexer', 0.15], ['browser', 0.07], ['mock', 0.07]],
  model: [['claude-sonnet-5', 0.58], ['claude-opus-5-5', 0.27], ['not recorded', 0.08], ['not ended', 0.07]],
  tenant_id: [['eng', 0.71], ['smoke', 0.2], ['u-operator', 0.09]],
  pool_account: [['acct-claude-01', 0.27], ['acct-claude-02', 0.21], ['acct-claude-03', 0.17], ['acct-claude-04', 0.12], ['not recorded', 0.16], ['not ended', 0.07]],
}

/** Exported for the dev preview's own test of its shape; the screen never calls it. */
export function platformTimeExample(query: URLSearchParams, now: number): PlatformTime {
  const tz = query.get('tz') ?? viewerZone()
  const bucket = (query.get('bucket') ?? 'week') as TimeBucket
  const group = query.get('group') ?? 'runner_profile'
  const count = TIME_BUCKETS.find((b) => b.key === bucket)?.count ?? 26
  const perDay = 52
  const days = bucket === 'day' ? 1 : bucket === 'week' ? 7 : 30
  // THE EXAMPLE'S BUCKETS ARE CALENDAR BUCKETS, as the route's are: days,
  // Monday weeks, first-of-month months, the newest the one holding `now`.
  // Placed at noon UTC, so the viewer's zone, whatever it is, labels each on
  // its own date (a midnight UTC month start is the 30th of the month before
  // in Los Angeles). Example data: the route's own edges are local midnights.
  const today = Date.parse(`${dayIn(now, tz)}T12:00:00Z`)
  const monday = today - ((new Date(today).getUTCDay() + 6) % 7) * 86_400_000
  const edge = (i: number): string => {
    const k = i - (count - 1)
    if (bucket === 'day') return new Date(today + k * 86_400_000).toISOString()
    if (bucket === 'week') return new Date(monday + k * 7 * 86_400_000).toISOString()
    const d = new Date(today)
    return new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + k, 1, 12)).toISOString()
  }
  const empty = (): UsageFigures => ({
    agents: 0, agents_ended: 0, retries: 0, attempts: 0,
    run_s: { sum: null, measured: 0, attempts: 0 },
    tokens: {
      input_tokens: { sum: null, reporting: 0, attempts: 0 },
      output_tokens: { sum: null, reporting: 0, attempts: 0 },
      cache_read_input_tokens: { sum: null, reporting: 0, attempts: 0 },
      cache_creation_input_tokens: { sum: null, reporting: 0, attempts: 0 },
    },
    cost: { sum_usd: null, estimated_usd: null, agents: 0, reporting: 0 },
  })
  const add = (a: UsageFigures, b: UsageFigures) => {
    const plus = (x: number | null, y: number | null) => (x === null ? y : y === null ? x : x + y)
    a.agents += b.agents
    a.agents_ended += b.agents_ended
    a.retries += b.retries
    a.attempts += b.attempts
    a.run_s = { sum: plus(a.run_s.sum, b.run_s.sum), measured: a.run_s.measured + b.run_s.measured, attempts: a.run_s.attempts + b.run_s.attempts }
    for (const t of TOKEN_KINDS) {
      const x = a.tokens[t.key]
      const y = b.tokens[t.key]
      a.tokens[t.key] = { sum: plus(x.sum, y.sum), reporting: x.reporting + y.reporting, attempts: x.attempts + y.attempts }
    }
    a.cost = {
      sum_usd: plus(a.cost.sum_usd, b.cost.sum_usd),
      estimated_usd: plus(a.cost.estimated_usd, b.cost.estimated_usd),
      agents: a.cost.agents + b.cost.agents,
      reporting: a.cost.reporting + b.cost.reporting,
    }
  }
  const figuresFor = (agents: number, i: number, open: boolean): UsageFigures => {
    const ended = open ? Math.round(agents * 0.9) : agents
    const retries = Math.round(agents * (0.08 + 0.03 * ((i * 7) % 5) / 4))
    const attempts = ended + Math.round((retries * ended) / Math.max(1, agents))
    const gap = i % 4 === 1 ? 2 : 0
    const tok = (per: number, short: number) => ({ sum: attempts === 0 ? null : (attempts - short) * per, reporting: attempts - short, attempts })
    return {
      agents, agents_ended: ended, retries, attempts,
      run_s: { sum: ended === 0 ? null : (attempts - gap) * 1080, measured: attempts - gap, attempts },
      tokens: {
        input_tokens: tok(146_000, gap),
        output_tokens: tok(22_700, gap),
        cache_read_input_tokens: tok(640_000, 0),
        cache_creation_input_tokens: tok(39_500, gap * 3),
      },
      cost: {
        sum_usd: ended === 0 ? null : Math.round((ended - gap) * 0.87 * (1 + 0.14 * Math.sin(i / 1.7)) * 100) / 100,
        estimated_usd: i % 3 === 0 && ended > 0 ? 1.02 : null,
        agents,
        reporting: ended - gap,
      },
    }
  }
  const buckets: UsageBucket[] = []
  const total = empty()
  for (let i = 0; i < count; i++) {
    const open = i === count - 1
    const start = edge(i)
    const stop = edge(i + 1)
    if (i === count - 4) {
      buckets.push({
        start, end: stop, state: 'unread', in_progress: false, unread_reason: 'read_failed',
        agents: null, agents_ended: null, retries: null, attempts: null, run_s: null, tokens: null, cost: null,
      })
      continue
    }
    const wave = 1 + 0.18 * Math.sin(i / 2.3) + i / (count * 2.5)
    const agents = Math.round(perDay * days * wave * (open ? 0.55 : 1))
    const f = figuresFor(agents, i, open)
    add(total, f)
    buckets.push({ start, end: stop, state: open ? 'open' : 'sealed', in_progress: open, unread_reason: null, ...f })
  }
  const rows = splitExample(total, EXAMPLE_GROUPS[group] ?? EXAMPLE_GROUPS.runner_profile!)
  return {
    scope: { kind: 'platform', tenants: ['eng', 'smoke', 'u-operator'], excluded: [], tenants_complete: true },
    tz,
    since: buckets[0]!.start,
    until: buckets[buckets.length - 1]!.end,
    bucket,
    reads: readEstimate(spanDays(bucket, now, tz), 3).total ?? 0,
    cached: false,
    generated_at: new Date(now).toISOString(),
    usage: {
      basis: 'created_at',
      cost_label: 'Estimated cost (list price, not a bill)',
      buckets,
      totals: { complete: false, ...total },
      groups: { by: group, rows_total: rows.length, rows },
    },
  }
}

/** `n` split by `weights` into integers that add up to `n` exactly (largest remainder). */
function splitInt(n: number, weights: readonly number[]): number[] {
  const sum = weights.reduce((x, y) => x + y, 0)
  if (sum === 0) return weights.map(() => 0)
  const raw = weights.map((w) => (n * w) / sum)
  const out = raw.map(Math.floor)
  const order = raw.map((r, i) => [r - Math.floor(r), i] as const).sort((x, y) => y[0] - x[0])
  const left = n - out.reduce((x, y) => x + y, 0)
  for (let k = 0; k < left; k++) out[order[k % order.length]![1]]! += 1
  return out
}

/** `x` split by `weights` into cents that add up to `x`; null stays null in every part. */
function splitMoney(x: number | null, weights: readonly number[]): Array<number | null> {
  if (x === null) return weights.map(() => null)
  return splitInt(Math.round(x * 100), weights).map((c) => c / 100)
}

/**
 * The example's group rows, cut from its totals so that the rows add up to
 * the total row exactly, as the route's do. "not ended" holds the agents
 * whose end was not read and nothing measured; mock reports a measured $0.00
 * and 0 tokens, so the others carry all of the money and tokens.
 */
function splitExample(total: UsageFigures, shares: ReadonlyArray<readonly [string, number]>): UsageGroupRow[] {
  const keys = shares.map(([k]) => k)
  const notEnded = keys.indexOf('not ended')
  const w = shares.map(([k, s]) => (k === 'not ended' ? 0 : s))
  const paying = shares.map(([k, s]) => (k === 'not ended' || k === 'mock' ? 0 : s))
  const ended = splitInt(total.agents_ended, w)
  const open = total.agents - total.agents_ended
  const agents = ended.map((n, i) => (i === notEnded ? n + open : notEnded === -1 ? n + splitInt(open, w)[i]! : n))
  const retries = splitInt(total.retries, w)
  const attempts = splitInt(total.attempts, w)
  const started = splitInt(total.run_s.attempts, w)
  const measured = splitInt(total.run_s.measured, w)
  const runSum = splitMoney(total.run_s.sum, w)
  const reporting = splitInt(total.cost.reporting, w)
  const cost = splitMoney(total.cost.sum_usd, paying)
  const estimated = splitMoney(total.cost.estimated_usd, paying)
  const tokens = TOKEN_KINDS.map((t) => ({
    key: t.key,
    sum: splitInt(total.tokens[t.key].sum ?? 0, paying),
    reporting: splitInt(total.tokens[t.key].reporting, w),
    attempts: splitInt(total.tokens[t.key].attempts, w),
  }))
  return keys.map((key, i) => {
    const mock = key === 'mock'
    const tok = {} as Record<TokenKind, TokenSum>
    for (const t of tokens) {
      tok[t.key] = { sum: t.attempts[i] === 0 ? null : mock ? 0 : t.sum[i]!, reporting: t.reporting[i]!, attempts: t.attempts[i]! }
    }
    return {
      key,
      agents: agents[i]!,
      agents_ended: ended[i]!,
      retries: retries[i]!,
      attempts: attempts[i]!,
      run_s: { sum: started[i] === 0 ? null : runSum[i]!, measured: measured[i]!, attempts: started[i]! },
      tokens: tok,
      cost: {
        sum_usd: reporting[i] === 0 ? null : mock ? 0 : cost[i]!,
        estimated_usd: mock || estimated[i] === 0 ? null : estimated[i]!,
        agents: agents[i]!,
        reporting: reporting[i]!,
      },
    }
  })
}
