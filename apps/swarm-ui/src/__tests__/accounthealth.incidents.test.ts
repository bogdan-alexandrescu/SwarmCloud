// ACCOUNT-HEALTH-PAGE (part of #2302): THE CONSOLE'S INCIDENT RULE.
//
// At 390 the page groups each account's served events into incidents, by
// proposal D's fixed rule (docs/quota-management.md, "Incidents"): a rate-limit
// hit opens a limit incident that later limit events join until its reset; a
// burst of errors of one family is one incident while each event comes within
// 45 minutes of the last; a refusal with no window belongs to the re-auth
// incident. D's own risk was "two storms 50 minutes apart read as two
// incidents" -- the rule must say which, and this file holds it there.
//
// THE RULE ITSELF is accountHealth.ts's `incidentsOf`, shared with the account
// sheet (BGD, 2026-10-25) and tested there, in accountHealth.test.ts. This
// file holds the page to it: `incidents` is that rule with each incident named
// for its account, the cases below still read the rule through the page, and
// the pool's ordering and its event-less re-auth card are the page's own.
//
// BREAK IT: widen the gap to 60 minutes, let a limit event join after its
// reset, file a windowless refusal with the limits, open an error incident on
// a healthy account, or give the page its own copy of the rule again -- a case
// below turns red.

import { describe, expect, it } from 'vitest'

import { incidentsOf } from '../accountHealth'
import { loadAccountHealthBoard, type AccountHealthEvent } from '../api'
import {
  INCIDENT_GAP_MS,
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

describe('errors: a burst of one family is one incident', () => {
  it('splits two storms 50 minutes apart into two incidents', () => {
    // Storm one: 4 errors from -200m to -170m. Storm two starts 50 min after its last event.
    const storm1 = [200, 190, 180, 170].map((m) => ev(m, 'overloaded'))
    const storm2 = [120, 115, 110].map((m) => ev(m, 'server_error'))
    const got = incidents(record('eng:a', [...storm1, ...storm2]), 'healthy', NOW)

    expect(got.map((x) => x.events.length)).toEqual([3, 4])
    expect(got.every((x) => x.family === 'err')).toBe(true)
    expect(170 - 120).toBeGreaterThan(INCIDENT_GAP_MS / MIN)
  })

  it('keeps two storms 40 minutes apart as one incident', () => {
    const events = [200, 190, 180, 140, 135].map((m) => ev(m, 'network'))
    const got = incidents(record('eng:a', events), 'healthy', NOW)

    expect(got).toHaveLength(1)
    expect(got[0]!.events).toHaveLength(5)
  })

  it('opens the newest error incident only while the account is erroring and it is under an hour old', () => {
    const events = [30, 25, 20].map((m) => ev(m, 'overloaded'))

    expect(incidents(record('eng:a', events), 'erroring', NOW)[0]!.open).toBe(true)
    expect(incidents(record('eng:a', events), 'healthy', NOW)[0]!.open).toBe(false)
    const old = [130, 125, 120].map((m) => ev(m, 'overloaded'))
    expect(incidents(record('eng:a', old), 'erroring', NOW)[0]!.open).toBe(false)
  })
})

describe('limits: a hit opens one incident that later limit events join until its reset', () => {
  it('joins a refusal on the same window before the reset, and opens a new incident after it', () => {
    const events = [
      ev(300, 'rate_limit', { window: 'five_hour', resetIn: -200 }),
      ev(250, 'assign_refused', { window: 'five_hour' }),
      // After the first reset (-200m): a new incident.
      ev(90, 'rate_limit', { window: 'five_hour', resetIn: 60 }),
      ev(30, 'exhausted'),
    ]
    const got = incidents(record('eng:a', events), 'limited', NOW)

    expect(got.map((x) => [x.family, x.events.length, x.open])).toEqual([
      ['lim', 2, true],
      ['lim', 2, false],
    ])
    expect(got[0]!.to).toBe(NOW + 60 * MIN)
  })

  it('never joins a limit event to an incident with no reset', () => {
    const got = incidents(record('eng:a', [ev(50, 'exhausted'), ev(40, 'exhausted')]), 'healthy', NOW)

    expect(got).toHaveLength(2)
  })
})

describe('credential: a refusal with no window belongs to the re-auth incident', () => {
  it('groups failed refreshes, the 401, the re-auth mark and the refusals after it', () => {
    const events = [
      ev(70, 'refresh_failed'),
      ev(62, 'auth'),
      ev(61, 'reauth'),
      ev(40, 'assign_refused'),
      ev(12, 'assign_refused'),
    ]
    const got = incidents(record('eng:a', events), 'reauth_needed', NOW)

    expect(got).toHaveLength(1)
    expect(got[0]!.family).toBe('cred')
    expect(got[0]!.events.map((e) => e.kind)).toEqual(['refresh_failed', 'auth', 'reauth', 'assign_refused', 'assign_refused'])
    expect(got[0]!.open).toBe(true)
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

describe('the page groups by the shared rule, not a copy of it', () => {
  it('names each of incidentsOf’s incidents for its account, and nothing else, across the development pool', async () => {
    const r = await loadAccountHealthBoard()
    if (r.status !== 'ok' || r.data.health.kind !== 'ok') throw new Error('the fixture board did not load')
    const row = new Map(r.data.page.accounts.map((a) => [a.account_id, a.health_status]))
    let compared = 0
    for (const rec of r.data.health.pool.accounts) {
      const status = row.get(rec.account_id) ?? null
      const page = incidents(rec, status, r.data.readAt)
      const shared = incidentsOf(rec.events as unknown as AccountHealthEvent[], status, r.data.readAt)
      expect(page.map(({ account_id, label, family, ...x }) => ({ fam: family, ...x }))).toEqual(shared)
      expect(page.every((x) => x.account_id === rec.account_id && x.label === (rec.label || rec.account_id))).toBe(true)
      compared += page.length
    }
    expect(compared).toBeGreaterThan(5)
  })

  it('groups only the newest 50 events, as the sheet does, when a record holds more', () => {
    // 60 network errors 2 hours apart: 60 one-event incidents by the gap rule,
    // of which the shared rule keeps the newest 50 (RECORD_KEEPS).
    const many = Array.from({ length: 60 }, (_, i) => ev(10 + i * 120, 'network'))
    const got = incidents(record('eng:a', many), 'healthy', NOW)

    expect(got).toHaveLength(50)
    expect(got[0]!.from).toBe(NOW - 10 * MIN)
    expect(got[49]!.from).toBe(NOW - (10 + 49 * 120) * MIN)
  })
})
