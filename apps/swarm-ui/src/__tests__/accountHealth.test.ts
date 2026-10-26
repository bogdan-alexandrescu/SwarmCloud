// ONE ACCOUNT'S HEALTH RECORD, READ FOR DRAWING (#2302, ACCOUNT-HEALTH-SHEET).
//
// What this file pins, in `accountHealthRecord.ts`:
//   * THE INCIDENT RULE (mock-up D, "What it reads"), the console's and not the
//     broker's, so it is written once and held here: a limit opens on a hit and
//     takes later limit events until its reset; errors and credential events
//     join while each comes within 45 minutes of the last; a refusal with no
//     window joins the credential incident; only the newest error or
//     credential incident can be open; every event sits in exactly one.
//   * the limited stretches the hourly chart shades, from each hit to its
//     window's reset, and `limited_until` beyond them;
//   * the 168 hourly bars, built from the served series, not from the events.
//
// BREAK IT: make the error gap 60 minutes -- the two-storms case reads one
// incident. Let a windowless refusal stay a limit -- the re-auth case opens a
// limit incident. Open every credential incident on reauth_needed -- the
// newest-only case fails. Bucket the bars from `events` -- the series case
// loses the hour the last 50 no longer hold.

import { describe, expect, it } from 'vitest'

import type { AccountHealthEvent, AccountHealthKind } from '../api'
import {
  HOUR_MS,
  SERIES_HOURS,
  hourlyBars,
  incidentSummary,
  incidentTitle,
  incidentsOf,
  limitedBands,
} from '../accountHealthRecord'

const NOW = Date.UTC(2026, 9, 25, 7, 10)
const MIN = 60_000
const at = (minutesAgo: number) => new Date(NOW - minutesAgo * MIN).toISOString()

function ev(minutesAgo: number, kind: AccountHealthKind, over: Partial<AccountHealthEvent> = {}): AccountHealthEvent {
  return {
    at: at(minutesAgo),
    kind,
    tenant_id: 'eng',
    task_id: null,
    window: null,
    reset_at: null,
    message: `${kind} message`,
    own: true,
    ...over,
  }
}

describe('the incident rule', () => {
  it('groups a hit with the limit events until its reset, and opens a new one after it', () => {
    const events = [
      ev(30, 'assign_refused', { window: 'five_hour' }),
      ev(60, 'exhausted'),
      ev(70, 'rate_limit', { window: 'five_hour', reset_at: at(-120) }),
      // Two days earlier: a closed limit, its reset long past.
      ev(2900, 'assign_refused', { window: 'five_hour' }),
      ev(3000, 'rate_limit', { window: 'five_hour', reset_at: at(2800) }),
      // After that reset: its own incident.
      ev(2700, 'exhausted'),
    ]
    const inc = incidentsOf(events, 'limited', NOW)
    expect(inc.map((x) => [x.fam, x.events.length, x.open])).toEqual([
      ['lim', 3, true],
      ['lim', 1, false],
      ['lim', 2, false],
    ])
    expect(incidentTitle(inc[0]!)).toBe('Rate-limited on its 5-hour window')
    expect(incidentSummary(inc[0]!)).toBe('1 rate-limit hit · 1 exhausted report · 1 refusal at assignment')
  })

  it('splits two error storms more than 45 minutes apart, and opens only the newest while erroring', () => {
    const events = [
      ev(5, 'overloaded'),
      ev(20, 'overloaded'),
      ev(40, 'server_error'),
      // 46 minutes before the storm above: a separate one.
      ev(86, 'network'),
      ev(100, 'overloaded'),
    ]
    const inc = incidentsOf(events, 'erroring', NOW)
    expect(inc.map((x) => [x.fam, x.events.length, x.open])).toEqual([
      ['err', 3, true],
      ['err', 2, false],
    ])
    expect(incidentTitle(inc[0]!)).toBe('Erroring: 3 provider errors')
    // The same storm, its last event over an hour old: closed whatever the status.
    expect(incidentsOf([ev(61, 'overloaded'), ev(70, 'overloaded')], 'erroring', NOW)[0]!.open).toBe(false)
    // The same storm on a healthy account: closed.
    expect(incidentsOf(events, 'healthy', NOW)[0]!.open).toBe(false)
  })

  it('puts a windowless refusal in the credential incident and opens it while the account needs re-auth', () => {
    const events = [
      ev(10, 'assign_refused'),
      ev(40, 'assign_refused'),
      ev(70, 'reauth'),
      ev(71, 'auth'),
      ev(90, 'refresh_failed'),
      ev(100, 'refresh_failed'),
      // A credential event a day before: its own, and closed (only the newest is open).
      ev(1500, 'refresh_failed'),
    ]
    const inc = incidentsOf(events, 'reauth_needed', NOW)
    expect(inc.map((x) => [x.fam, x.events.length, x.open])).toEqual([
      ['cred', 6, true],
      ['cred', 1, false],
    ])
    expect(incidentTitle(inc[0]!)).toBe('Needs re-auth')
    expect(incidentTitle(inc[1]!)).toBe('One credential refresh failed')
    expect(incidentsOf(events, 'healthy', NOW)[0]!.open).toBe(false)
  })

  it('places every event of the last 50 in exactly one incident, oldest first inside it', () => {
    const kinds: AccountHealthKind[] = ['overloaded', 'rate_limit', 'auth', 'network', 'exhausted', 'reauth']
    const events = Array.from({ length: 60 }, (_, i) =>
      ev(i * 37, kinds[i % kinds.length]!, kinds[i % kinds.length] === 'rate_limit' ? { window: 'seven_day', reset_at: at(i * 37 - 90) } : {}),
    )
    const inc = incidentsOf(events, 'healthy', NOW)
    const placed = inc.flatMap((x) => x.events)
    expect(placed.length).toBe(50)
    expect(new Set(placed).size).toBe(50)
    for (const x of inc) {
      const t = x.events.map((e) => Date.parse(e.at))
      expect(t).toEqual([...t].sort((p, q) => p - q))
    }
  })
})

describe('the limited stretches', () => {
  it('runs from each hit to its reset, merges a hit inside a stretch of its window, and keeps limited_until', () => {
    const bands = limitedBands(
      [
        ev(60, 'rate_limit', { window: 'five_hour', reset_at: at(-100) }),
        ev(120, 'rate_limit', { window: 'five_hour', reset_at: at(-90) }),
        ev(3000, 'rate_limit', { window: 'seven_day', reset_at: at(2500) }),
        ev(10, 'overloaded'),
      ],
      at(-100),
      NOW,
    )
    expect(bands.map((b) => [b.window, b.hits, (b.to - b.from) / MIN])).toEqual([
      ['seven_day', 1, 500],
      ['five_hour', 2, 220],
    ])
    // A limited_until the events no longer carry (its hit aged out of the last 50): shaded from now.
    const aged = limitedBands([], at(-45), NOW)
    expect(aged).toEqual([{ from: NOW, to: NOW + 45 * MIN, window: null, hits: 0 }])
  })
})

describe('the hourly series', () => {
  it('is 168 hours ending at the hour that holds now, split by family, from the served series', () => {
    const hour = (h: number) => new Date(NOW - (NOW % HOUR_MS) - h * HOUR_MS).toISOString()
    const bars = hourlyBars(
      [
        { hour: hour(0), counts: { overloaded: 2, rate_limit: 1 }, total: 3 },
        { hour: hour(167), counts: { auth: 4 }, total: 4 },
        // Older than 7 days: outside the chart.
        { hour: hour(168), counts: { network: 9 }, total: 9 },
      ],
      NOW,
    )
    expect(bars.length).toBe(SERIES_HOURS)
    expect(bars[167]).toMatchObject({ err: 2, lim: 1, cred: 0, n: 3 })
    expect(bars[0]).toMatchObject({ err: 0, lim: 0, cred: 4, n: 4 })
    expect(bars.reduce((n, b) => n + b.n, 0)).toBe(7)
  })
})
