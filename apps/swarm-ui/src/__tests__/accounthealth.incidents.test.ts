// ACCOUNT-HEALTH-PAGE (part of #2302): THE PAGE'S INCIDENTS, FROM THE ONE RULE.
//
// At 390 the page groups each account's served events into incidents by
// proposal D's rule. The rule is written once, as `incidentsOf` in
// accountHealth.ts, and its cases live in accountHealth.test.ts (BGD,
// 2026-10-25: two copies of a grouping rule are two answers to "how many
// incidents did this account have"). This file holds the page's side: that
// `incidents()` groups through that one function and only names each
// incident's account, and the pool's ordering and re-auth cards around it.
//
// BREAK IT: give accountHealthModel.ts its own grouping loop again -- the
// one-copy case turns red. Drop the re-auth card for an account with no
// credential event, or order limits before errors -- a pool case turns red.

import { describe, expect, it, vi } from 'vitest'

vi.mock('../accountHealth', async (real) => {
  const mod = await real<typeof import('../accountHealth')>()
  return { ...mod, incidentsOf: vi.fn(mod.incidentsOf) }
})

import { incidentsOf } from '../accountHealth'
import {
  incidents,
  poolIncidents,
  type AccountHealthRecord,
  type HealthEvent,
  type HealthKind,
  type PoolHealth,
} from '../accountHealthModel'

const NOW = Date.UTC(2026, 9, 25, 7, 10)
const MIN = 60_000
const H = 60 * MIN

function ev(minutesAgo: number, kind: HealthKind, o: { window?: string; resetIn?: number } = {}): HealthEvent {
  return {
    at: new Date(NOW - minutesAgo * MIN).toISOString(),
    kind,
    tenant_id: 'eng',
    task_id: `task_${minutesAgo}`,
    window: o.window ?? null,
    reset_at: o.resetIn === undefined ? null : new Date(NOW + o.resetIn * MIN).toISOString(),
    message: `${kind} at -${minutesAgo}m`,
    own: true,
  }
}

function record(id: string, events: HealthEvent[]): AccountHealthRecord {
  return {
    account_id: id,
    label: id.split(':')[1] ?? id,
    owner_tenant: 'eng',
    status: null,
    limited_until: null,
    counts: { last_1h: null, last_24h: null, last_7d: null },
    hourly: [],
    // Served newest first, as the broker serves them.
    events: [...events].sort((x, y) => Date.parse(y.at) - Date.parse(x.at)),
    observed_at: null,
  }
}

describe('the rule is the console’s one copy, in accountHealth.ts', () => {
  it('groups through incidentsOf and only names each incident’s account', () => {
    const events = [
      ev(300, 'rate_limit', { window: 'five_hour', resetIn: -200 }),
      ev(250, 'assign_refused', { window: 'five_hour' }),
      ev(90, 'rate_limit', { window: 'five_hour', resetIn: 60 }),
      ev(70, 'refresh_failed'),
      ev(40, 'assign_refused'),
      ...[200, 190, 140, 30, 25].map((m) => ev(m, 'overloaded')),
    ]
    const r = record('eng:a', events)
    vi.mocked(incidentsOf).mockClear()
    const got = incidents(r, 'erroring', NOW)

    expect(incidentsOf).toHaveBeenCalledTimes(1)
    expect(vi.mocked(incidentsOf).mock.calls[0]![0]).toBe(r.events)
    const shared = incidentsOf(r.events as Parameters<typeof incidentsOf>[0], 'erroring', NOW)
    const shape = (x: { from: number; last: number; to: number | null; window: string | null; open: boolean; events: unknown[] }) => ({
      from: x.from,
      last: x.last,
      to: x.to,
      window: x.window,
      open: x.open,
      events: x.events,
    })
    expect(got.map((x) => ({ family: x.family, ...shape(x) }))).toEqual(shared.map((x) => ({ family: x.fam, ...shape(x) })))
    expect(got.every((x) => x.account_id === 'eng:a' && x.label === 'a')).toBe(true)
  })
})

describe('the pool: open problems first, worst first', () => {
  it('orders the open incidents credential, then errors, then limits, and leaves the closed ones after', () => {
    const pool: PoolHealth = {
      tenant_id: 'eng',
      events_max: 50,
      truncated: false,
      observed_at: null,
      accounts: [
        record('eng:lim', [ev(20, 'rate_limit', { window: 'seven_day', resetIn: 600 })]),
        record('eng:err', [5, 4, 3].map((m) => ev(m, 'overloaded'))),
        record('eng:cred', [ev(10, 'auth'), ev(9, 'reauth')]),
        record('eng:old', [ev(3 * 24 * 60, 'network')]),
      ],
    }
    const status = { 'eng:lim': 'limited', 'eng:err': 'erroring', 'eng:cred': 'reauth_needed', 'eng:old': 'healthy' } as const
    const got = poolIncidents(pool, (id) => status[id as keyof typeof status], NOW)

    expect(got.open.map((x) => x.account_id)).toEqual(['eng:cred', 'eng:err', 'eng:lim'])
    expect(got.closed.map((x) => x.account_id)).toEqual(['eng:old'])
    expect(NOW - got.closed[0]!.from).toBe(3 * 24 * H)
  })
})

describe('the pool: an account that needs re-auth always has an open credential incident', () => {
  it('opens one with no events when the served events hold no credential event, or the pool has no record', () => {
    const pool: PoolHealth = {
      tenant_id: 'eng',
      events_max: 50,
      truncated: false,
      observed_at: null,
      accounts: [
        // Re-auth older than 7 days: the record is empty.
        record('eng:empty', []),
        // The credential event was pushed out of the newest 50 by limit events.
        record('eng:pushed', [ev(20, 'rate_limit', { window: 'five_hour', resetIn: 30 })]),
        record('eng:quiet', []),
      ],
    }
    const status = { 'eng:empty': 'reauth_needed', 'eng:pushed': 'reauth_needed', 'eng:quiet': 'healthy', 'eng:gone': 'reauth_needed' } as const
    const got = poolIncidents(pool, (id) => status[id as keyof typeof status] ?? null, NOW, [
      { account_id: 'eng:gone', label: 'gone' },
      { account_id: 'eng:quiet', label: 'quiet' },
    ])

    const cred = got.open.filter((x) => x.family === 'cred')
    expect(cred.map((x) => x.account_id).sort()).toEqual(['eng:empty', 'eng:gone', 'eng:pushed'])
    expect(cred.every((x) => x.events.length === 0)).toBe(true)
    // Credential first, before the open limit.
    expect(got.open.map((x) => x.family)).toEqual(['cred', 'cred', 'cred', 'lim'])
    expect(got.open.some((x) => x.account_id === 'eng:quiet')).toBe(false)
  })

  it('does not add a second one when the served events already hold the open credential incident', () => {
    const pool: PoolHealth = {
      tenant_id: 'eng',
      events_max: 50,
      truncated: false,
      observed_at: null,
      accounts: [record('eng:cred', [ev(10, 'auth'), ev(9, 'reauth')])],
    }
    const got = poolIncidents(pool, () => 'reauth_needed', NOW, [{ account_id: 'eng:cred', label: 'cred' }])

    expect(got.open).toHaveLength(1)
    expect(got.open[0]!.events).toHaveLength(2)
  })
})
