// THE PERSON'S TIME ZONE (VQ-023, owner decision 2026-10-26; lane
// TIMEZONE-SETTING, part of #2391): one zone per person, UTC by default,
// every time labelled with it, and "today" starting at its midnight.
//
// Each test names the mutation that turns it red.

import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import {
  UTC,
  ZONE_KEY,
  allZones,
  canonicalZone,
  currentZone,
  formatClock,
  formatDateTime,
  isToday,
  notePerson,
  resetTimeZone,
  setZone,
  startOfDay,
  zoneLabel,
} from '../timezone'

const LA = 'America/Los_Angeles'
/** 09:05 PDT on 2026-10-26: still the 26th in Los Angeles, as in UTC. */
const NOON_ISH = Date.parse('2026-10-26T16:05:09Z')

beforeEach(() => {
  window.localStorage.clear()
  resetTimeZone()
})

afterEach(() => {
  window.localStorage.clear()
  resetTimeZone()
})

describe('the formatter labels every time with its zone', () => {
  /** MUTATION: drop the label from `formatClock`, or format in the browser's zone. */
  it('prints the time of day in the zone, named', () => {
    expect(formatClock(NOON_ISH, UTC)).toBe('16:05 UTC')
    expect(formatClock(NOON_ISH, LA)).toBe('09:05 PDT')
    expect(formatClock('2026-12-01T16:05:00Z', LA)).toBe('08:05 PST')
    expect(formatClock('2026-10-26T00:30:00Z', UTC)).toBe('00:30 UTC')
    expect(formatDateTime(NOON_ISH, LA)).toBe('Oct 26 09:05:09 PDT')
    expect(zoneLabel(NOON_ISH, LA)).toBe('PDT')
  })

  it('returns an unreadable instant as given rather than "NaN:NaN"', () => {
    expect(formatClock('not a time', UTC)).toBe('not a time')
  })

  /** MUTATION: `formatClock(at, z = UTC)` instead of the page's zone. */
  it('formats in the current zone when none is named', () => {
    notePerson('ada@example.com')
    expect(formatClock(NOON_ISH)).toBe('16:05 UTC')
    setZone(LA)
    expect(formatClock(NOON_ISH)).toBe('09:05 PDT')
  })
})

describe("'today' starts at the zone's midnight", () => {
  /** MUTATION: count from UTC midnight whatever the zone. */
  it('is local midnight in the zone, through both summer-time changes', () => {
    expect(new Date(startOfDay(NOON_ISH, UTC)).toISOString()).toBe('2026-10-26T00:00:00.000Z')
    expect(new Date(startOfDay(NOON_ISH, LA)).toISOString()).toBe('2026-10-26T07:00:00.000Z')
    // 2026-11-01 ends summer time at 02:00 PDT: the day began in PDT.
    expect(new Date(startOfDay('2026-11-01T20:00:00Z', LA)).toISOString()).toBe('2026-11-01T07:00:00.000Z')
    expect(new Date(startOfDay('2026-11-02T12:00:00Z', LA)).toISOString()).toBe('2026-11-02T08:00:00.000Z')
    // 2026-03-08 starts it at 02:00 PST: the day began in PST.
    expect(new Date(startOfDay('2026-03-08T20:00:00Z', LA)).toISOString()).toBe('2026-03-08T08:00:00.000Z')
    // Before 07:00Z it is still yesterday in Los Angeles.
    expect(new Date(startOfDay('2026-10-26T03:00:00Z', LA)).toISOString()).toBe('2026-10-25T07:00:00.000Z')
  })

  /** MUTATION: drop the second offset pass in `startOfDay`, or the skipped-midnight step. */
  it('is the first instant of the date in every zone, on every day of a year', () => {
    const zones = [UTC, LA, 'Europe/London', 'Asia/Kolkata', 'Australia/Lord_Howe', 'America/Santiago', 'Asia/Beirut', 'Pacific/Chatham']
    const day = (ms: number, z: string) => formatDateTime(ms, z).split(' ').slice(0, 2).join(' ')
    let visited = 0
    for (const z of zones) {
      for (let t = Date.parse('2026-01-01T12:00:00Z'); t < Date.parse('2027-01-01T00:00:00Z'); t += 86_400_000) {
        const start = startOfDay(t, z)
        expect(day(start, z), `${z} ${new Date(t).toISOString()}`).toBe(day(t, z))
        expect(day(start - 1000, z), `${z} ${new Date(t).toISOString()}`).not.toBe(day(t, z))
        visited += 1
      }
    }
    expect(visited).toBe(zones.length * 365)
  })

  /** MUTATION: `isToday` compares UTC dates. */
  it("counts an instant as today by the zone's day", () => {
    const early = '2026-10-26T03:00:00Z' // 20:00 PDT on the 25th
    expect(isToday(early, NOON_ISH, UTC)).toBe(true)
    expect(isToday(early, NOON_ISH, LA)).toBe(false)
    expect(isToday('2026-10-26T10:00:00Z', NOON_ISH, LA)).toBe(true)
    expect(isToday('2026-10-27T07:00:00Z', NOON_ISH, LA)).toBe(false)
    expect(isToday(null, NOON_ISH, LA)).toBe(false)
  })
})

describe('the setting is per person, UTC by default', () => {
  /** MUTATION: default to the browser's zone. */
  it('is UTC before anyone picks', () => {
    expect(currentZone()).toBe(UTC)
    notePerson('ada@example.com')
    expect(currentZone()).toBe(UTC)
  })

  /** MUTATION: key the storage by browser rather than by the signed-in person. */
  it('keeps one zone per signed-in person', () => {
    notePerson('ada@example.com')
    expect(setZone(LA)).toBe(LA)
    expect(window.localStorage.getItem(`${ZONE_KEY}:ada@example.com`)).toBe(LA)
    resetTimeZone()
    notePerson('bob@example.com')
    expect(currentZone()).toBe(UTC)
    resetTimeZone()
    notePerson('ada@example.com')
    expect(currentZone()).toBe(LA)
  })

  it('uses the one stored zone before the person is known', () => {
    window.localStorage.setItem(`${ZONE_KEY}:ada@example.com`, LA)
    resetTimeZone()
    expect(currentZone()).toBe(LA)
  })

  it('removes the key for UTC, which is what nothing stored means', () => {
    notePerson('ada@example.com')
    setZone(LA)
    setZone(UTC)
    expect(window.localStorage.getItem(`${ZONE_KEY}:ada@example.com`)).toBeNull()
    expect(currentZone()).toBe(UTC)
  })

  /** MUTATION: store whatever was typed. */
  it('refuses a zone the browser does not know and changes nothing', () => {
    notePerson('ada@example.com')
    setZone(LA)
    expect(setZone('Mars/Olympus_Mons')).toBeNull()
    expect(currentZone()).toBe(LA)
    expect(canonicalZone('')).toBeNull()
  })

  it('reads a stored zone that is no longer valid as UTC', () => {
    window.localStorage.setItem(`${ZONE_KEY}:ada@example.com`, 'Nowhere/Gone')
    notePerson('ada@example.com')
    expect(currentZone()).toBe(UTC)
  })
})

describe('the zone list', () => {
  it('offers UTC first, then every IANA zone the browser knows', () => {
    const zones = allZones()
    expect(zones[0]).toBe(UTC)
    expect(zones).toContain(LA)
    expect(zones).toContain('Europe/London')
    expect(zones.filter((z) => z === UTC)).toHaveLength(1)
    expect(zones.length).toBeGreaterThan(300)
  })
})
