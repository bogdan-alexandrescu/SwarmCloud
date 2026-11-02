// JEV-USAGE-PAGE (part of #2688; the owner's pick of 2 November 2026): the
// read behind Capacity › Jev usage, `GET /v1/jev-usage` (swarm-api
// `jevusage`), normalised here so the page never reads a raw field.
//
// A DOLLAR IS NULL, NEVER 0, WITHOUT A RATE. swarm-api prices calls at the
// platform setting `jev_token_rates`, which has no default; until it is set
// every `usd` is null and the page says "rate not set". Output tokens are null
// where no record carried them. `normJevUsage` keeps both nulls: a figure that
// was not recorded is not a zero.

import { read, route, noteFixtureProbe, write, type Result } from './fetch'
import { USE_FIXTURES } from './api'
import { fixtureJevUsage } from './jevUsage.fixture'

export const JEV_USAGE_ROUTE = '/v1/jev-usage'

/** Who asked: the worker's judgments (by question), the gate, the agent's tool, the browser. */
export type JevUsageGroup =
  | 'worker_judgment'
  | 'quality_gate'
  | 'decision_tool'
  | 'jev_browser'
  | 'unattributed'
  | 'other'

export const JEV_GROUP_LABEL: Readonly<Record<JevUsageGroup, string>> = {
  worker_judgment: 'Worker judgment',
  quality_gate: 'Quality gate',
  decision_tool: 'Agent decision tool',
  jev_browser: 'Jev browser',
  unattributed: 'Not split by kind',
  other: 'Other',
}

export interface JevOutcomes {
  ok: number
  failed: number
  skipped: number
  /** Each skip's reason and its count, by reason. */
  skippedByReason: ReadonlyArray<{ reason: string; count: number }>
  /** Calls whose outcome no record carried. */
  unrecorded: number
}

export interface JevTally {
  calls: number
  inputTokens: number
  /** Null where no record carried output tokens. */
  outputTokens: number | null
  /** Null where no record carried an outcome: never "0 ok, 0 failed". */
  outcomes: JevOutcomes | null
  /** Null while the rate is not set, or a figure needs a rate that is not. */
  usd: number | null
}

export interface JevKindRow extends JevTally {
  kind: string
  group: JevUsageGroup
}

export interface JevDay extends JevTally {
  day: string
  kinds: readonly JevKindRow[]
}

export interface JevUsage {
  tenantId: string
  window: { from: string; to: string; days: number }
  rate: { input: number | null; output: number | null; set: boolean }
  days: readonly JevDay[]
  total: JevTally & { byKind: readonly JevKindRow[] }
  /** Calls whose records carry no day. */
  undated: JevTally | null
  month: { month: string; usedTokens: number; capTokens: number | null; usedFraction: number | null }
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}

function count(v: unknown): number {
  return typeof v === 'number' && Number.isFinite(v) && v >= 0 ? Math.floor(v) : 0
}

function figure(v: unknown): number | null {
  return typeof v === 'number' && Number.isFinite(v) && v >= 0 ? v : null
}

const GROUPS = new Set<string>(Object.keys(JEV_GROUP_LABEL))

function normOutcomes(v: unknown): JevOutcomes | null {
  if (!isRecord(v)) return null
  const o = v
  const byReason = isRecord(o.skipped_by_reason) ? o.skipped_by_reason : {}
  return {
    ok: count(o.ok),
    failed: count(o.failed),
    skipped: count(o.skipped),
    skippedByReason: Object.entries(byReason)
      .map(([reason, n]) => ({ reason, count: count(n) }))
      .filter((r) => r.count > 0)
      .sort((a, b) => a.reason.localeCompare(b.reason)),
    unrecorded: count(o.unrecorded),
  }
}

function normTally(v: Record<string, unknown>): JevTally {
  return {
    calls: count(v.calls),
    inputTokens: count(v.input_tokens),
    outputTokens: figure(v.output_tokens),
    outcomes: normOutcomes(v.outcomes),
    usd: figure(v.usd),
  }
}

function normKinds(v: unknown): JevKindRow[] {
  if (!Array.isArray(v)) return []
  return v.filter(isRecord).map((k) => ({
    ...normTally(k),
    kind: typeof k.kind === 'string' ? k.kind : 'other',
    group: (typeof k.group === 'string' && GROUPS.has(k.group) ? k.group : 'other') as JevUsageGroup,
  }))
}

/** The served body as the page reads it, or null when its shape is unknown. */
export function normJevUsage(v: unknown): JevUsage | null {
  if (!isRecord(v) || !Array.isArray(v.days) || !isRecord(v.total) || !isRecord(v.month)) return null
  const window = isRecord(v.window) ? v.window : {}
  const rate = isRecord(v.rate) ? v.rate : {}
  const month = v.month
  const input = figure(rate.input_usd_per_million)
  return {
    tenantId: typeof v.tenant_id === 'string' ? v.tenant_id : '',
    window: {
      from: typeof window.from === 'string' ? window.from : '',
      to: typeof window.to === 'string' ? window.to : '',
      days: count(window.days),
    },
    rate: { input, output: figure(rate.output_usd_per_million), set: rate.set === true && input !== null },
    days: v.days.filter(isRecord).map((d) => ({
      ...normTally(d),
      day: typeof d.day === 'string' ? d.day : '',
      kinds: normKinds(d.kinds),
    })),
    total: { ...normTally(v.total), byKind: normKinds(v.total.by_kind) },
    undated: isRecord(v.undated) ? normTally(v.undated) : null,
    month: {
      month: typeof month.month === 'string' ? month.month : '',
      usedTokens: count(month.used_tokens),
      capTokens: figure(month.cap_tokens),
      usedFraction: figure(month.used_fraction),
    },
  }
}

/** Capacity › Jev usage's one read. */
export async function loadJevUsage(): Promise<Result<JevUsage>> {
  if (USE_FIXTURES) {
    await new Promise((r) => setTimeout(r, 120))
    noteFixtureProbe(route(JEV_USAGE_ROUTE), 120, true)
    return { status: 'ok', fetchedAt: Date.now(), data: normJevUsage(fixtureJevUsage()) as JevUsage }
  }
  const r = await read<unknown>(route(JEV_USAGE_ROUTE), () => false)
  if (r.status !== 'ok' && r.status !== 'stale') return r as Result<JevUsage>
  const usage = normJevUsage(r.data)
  if (usage === null) {
    return {
      status: 'error',
      error: {
        kind: 'server_error',
        httpStatus: 200,
        code: null,
        message: 'GET /v1/jev-usage answered in a shape this console does not know.',
      },
    }
  }
  return { ...r, data: usage }
}

/** Jev's rate, USD per million tokens, as the platform settings hold it: a direction not set is null. */
export interface JevRate {
  input: number | null
  output: number | null
}

/** `jev_token_rates` in a platform settings read (`PlatformSettings` keeps it as served). */
export function jevRateOf(settings: unknown): JevRate {
  const raw = isRecord(settings) && isRecord(settings.jev_token_rates) ? settings.jev_token_rates : {}
  return { input: figure(raw.input), output: figure(raw.output) }
}

/** A typed rate, or null: a number of dollars at or above 0. Commas, spaces and a leading `$` are allowed. */
export function parseJevRate(typed: string): number | null {
  const t = typed.replace(/[,\s_]/g, '').replace(/^\$/, '')
  if (!/^\d+(\.\d+)?$/.test(t)) return null
  const v = Number(t)
  return Number.isFinite(v) && v <= 1_000_000 ? v : null
}

/**
 * `PUT /v1/admin/settings` with `jev_token_rates` and nothing else, so a save never rewrites another setting. The rate
 * is replaced whole: a direction left out has none, and `{}` clears it (`Store.set_platform_settings`).
 */
export async function saveJevRate(rate: JevRate): Promise<Result<JevRate>> {
  const body: Record<string, number> = {}
  if (rate.input !== null) body.input = rate.input
  if (rate.output !== null) body.output = rate.output
  const r = await write(route('/v1/admin/settings'), 'PUT', { jev_token_rates: body })
  if (r.status === 'ok' || r.status === 'stale') return { ...r, data: jevRateOf(r.data) }
  return r as Result<JevRate>
}
