// THE SHARED AXIS IN THE PERSON'S ZONE (VQ-023, owner decision 2026-10-26,
// "A per-person setting" and "Shared helpers first, then pages"; lane
// TIMEZONE-SHARED, part of #2391). Lanes put its ticks on the browser's own
// clock, unlabelled, whatever zone the person picked. The shared axis
// helpers now follow the setting, UTC by default, and a day tick is the
// zone's midnight.
//
// Each test names the mutation that turns it red.

import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { axisZone, clockTickText, clockTicks } from '../charts/series'
import { axisTickText, axisTicks, zoomedTo, DEFAULT_LANES_VIEW } from '../lanes'
import { axisLabels } from '../TimelineLanes'
import { formatClock, notePerson, resetTimeZone, setZone, startOfDay } from '../timezone'

const LA = 'America/Los_Angeles'
const MIN = 60_000
const HOUR = 3_600_000
const DAY = 24 * HOUR
/** 09:05 PDT on Monday 2026-10-26. */
const NOW = Date.parse('2026-10-26T16:05:00Z')

const iso = (t: number) => new Date(t).toISOString()

function pickLA(): void {
  notePerson('pat@example.com')
  setZone(LA)
}

beforeEach(() => {
  window.localStorage.clear()
  resetTimeZone()
})

afterEach(() => {
  window.localStorage.clear()
  resetTimeZone()
})

describe('Lanes ticks follow the setting, UTC by default', () => {
  // Mutation: axisTicks back on `getTimezoneOffset` (the browser's clock).
  it('puts a 24h axis on UTC hours, and its midnight is 00:00 UTC, when nothing is picked', () => {
    const ticks = axisTicks(NOW - DAY, NOW)
    expect(ticks.map(iso)).toEqual([
      '2026-10-25T18:00:00.000Z',
      '2026-10-26T00:00:00.000Z',
      '2026-10-26T06:00:00.000Z',
      '2026-10-26T12:00:00.000Z',
    ])
    expect(ticks.map((t) => axisTickText(t, ticks))).toEqual(['18:00', 'Oct 26', '06:00', '12:00'])
  })

  it('puts the same axis on Los Angeles hours once that is picked, the date on its midnight', () => {
    pickLA()
    const ticks = axisTicks(NOW - DAY, NOW)
    expect(ticks.map((t) => formatClock(t, LA))).toEqual(['12:00 PDT', '18:00 PDT', '00:00 PDT', '06:00 PDT'])
    expect(iso(ticks[2]!)).toBe('2026-10-26T07:00:00.000Z')
    expect(ticks.map((t) => axisTickText(t, ticks))).toEqual(['12:00', '18:00', 'Oct 26', '06:00'])
  })

  // Mutation: day ticks at multiples of 86 400 000 ms (00:00 UTC) whatever the zone.
  it('starts every day tick of a 7d axis at the zone’s midnight', () => {
    pickLA()
    const ticks = axisTicks(NOW - 7 * DAY, NOW)
    expect(ticks.length).toBeGreaterThan(4)
    for (const t of ticks) {
      expect(formatClock(t, LA)).toBe('00:00 PDT')
      expect(startOfDay(t, LA)).toBe(t)
    }
    const labels = ticks.map((t) => axisTickText(t, ticks))
    expect(labels[labels.length - 1]).toBe('Oct 26')
    expect(new Set(labels).size).toBe(labels.length)
  })

  it('draws the page’s own axis labels on the picked clock (TimelineLanes)', () => {
    pickLA()
    const labels = axisLabels(NOW - DAY, NOW, NOW).map((l) => [l.label, iso(l.t)])
    // Los Angeles' Oct 26 begins at 07:00Z; 00:00Z is 17:00 PDT on the 25th, no tick of a 6-hour axis there.
    expect(labels).toEqual([
      ['12:00', '2026-10-25T19:00:00.000Z'],
      ['18:00', '2026-10-26T01:00:00.000Z'],
      ['Oct 26', '2026-10-26T07:00:00.000Z'],
      ['06:00', '2026-10-26T13:00:00.000Z'],
    ])
  })

  // Mutation: zoomedTo writing the zone's wall clock into the address.
  it('keeps the zoom it sends to the address and the API an ISO instant in UTC', () => {
    pickLA()
    const v = zoomedTo(DEFAULT_LANES_VIEW, Date.parse('2026-10-26T07:00:00Z'), NOW)
    expect(v.since).toBe('2026-10-26T07:00:00.000Z')
    expect(v.until).toBe('2026-10-26T16:05:00.000Z')
  })
})

describe('the chart axis helpers (charts/series.ts)', () => {
  // Mutation: stepping by instants from midnight, which on a change day lands on 04:00 PDT.
  it('keeps hour ticks on round hours and skips the hour summer time removes', () => {
    const from = Date.parse('2026-03-08T08:00:00Z') // 00:00 PST, the day clocks go forward
    const ticks = clockTicks(from, from + 4 * HOUR, 6, LA, [HOUR])
    expect(ticks.map((t) => formatClock(t, LA))).toEqual(['00:00 PST', '01:00 PST', '03:00 PDT', '04:00 PDT'])
  })

  it('draws the hour summer time repeats once, and stays on round hours after it', () => {
    const from = Date.parse('2026-11-01T07:00:00Z') // 00:00 PDT, the day clocks go back
    const ticks = clockTicks(from, from + 6 * HOUR, 6, LA, [HOUR])
    expect(ticks.map((t) => formatClock(t, LA))).toEqual(['00:00 PDT', '01:00 PDT', '02:00 PST', '03:00 PST', '04:00 PST'])
  })

  it('puts a day tick at the zone’s midnight on either side of a change', () => {
    const ticks = clockTicks(Date.parse('2026-10-30T12:00:00Z'), Date.parse('2026-11-04T12:00:00Z'), 7, LA)
    expect(ticks.map((t) => formatClock(t, LA))).toEqual(['00:00 PDT', '00:00 PDT', '00:00 PST', '00:00 PST', '00:00 PST'])
    expect(ticks.map((t) => clockTickText(t, ticks, LA))).toEqual(['Oct 31', 'Nov 1', 'Nov 2', 'Nov 3', 'Nov 4'])
  })

  it('counts a several-day step from the zone’s days, so the same day is a tick in any window', () => {
    const until = Date.parse('2026-11-20T00:00:00Z')
    const a = clockTicks(Date.parse('2026-10-01T00:00:00Z'), until, 7, LA)
    const b = clockTicks(Date.parse('2026-10-05T00:00:00Z'), Date.parse('2026-11-24T00:00:00Z'), 7, LA)
    expect(a.length).toBeGreaterThan(2)
    for (let i = 1; i < a.length; i += 1) expect(Math.round((a[i]! - a[i - 1]!) / DAY)).toBe(14)
    for (const t of a) expect(formatClock(t, LA).slice(0, 5)).toBe('00:00')
    const shared = b.filter((t) => t < until)
    expect(shared.length).toBeGreaterThan(1)
    for (const t of shared) expect(a).toContain(t)
  })

  it('offers minutes for a short chart', () => {
    const from = Date.parse('2026-10-26T16:00:30Z')
    const ticks = clockTicks(from, from + 10 * MIN, 4, 'UTC')
    expect(ticks.map((t) => clockTickText(t, ticks, 'UTC'))).toEqual(['16:05', '16:10'])
  })

  // Mutation: axisZone returning a constant "UTC" (TimeSeries' "Times UTC.").
  it('names the zone the axis is drawn in, and both across a change', () => {
    expect(axisZone(NOW - DAY, NOW)).toBe('UTC')
    pickLA()
    expect(axisZone(NOW - DAY, NOW)).toBe('PDT')
    expect(axisZone(Date.parse('2026-10-30T12:00:00Z'), Date.parse('2026-11-04T12:00:00Z'))).toBe('PDT–PST')
  })
})
