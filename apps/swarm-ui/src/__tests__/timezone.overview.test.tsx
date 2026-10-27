// OVERVIEW IN THE PERSON'S ZONE (VQ-023, owner decision 2026-10-26; lane
// TIMEZONE-SETTING, part of #2391). Overview printed UTC and counted "today"
// from 00:00 UTC whatever the reader's day was. Its times and its "today"
// now follow the zone picked in Profile, UTC by default, each labelled.
//
// Each test names the mutation that turns it red.

import { act, cleanup, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Result } from '../fetch'
import { LifecycleBand, readReach, waitGroups, type OverviewTaskPage } from '../OverviewRegions'
import { notePerson, resetTimeZone, setZone } from '../timezone'
import type { Stats } from '../types'
import { task } from './runfixture'

const LA = 'America/Los_Angeles'
/** 09:05 PDT on Monday 2026-10-26. */
const NOW = Date.parse('2026-10-26T16:05:00Z')

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: NOW, serverAt: new Date(NOW).toISOString() }
}

const text = (n: Element | null | undefined): string => (n?.textContent ?? '').replace(/\s+/g, ' ').trim()

const STATS: Stats = {
  tenant_id: 'eng',
  tasks_by_state: { QUEUED: 0, READY: 0, PARKED: 0, LEASED: 0, DISPATCHED: 0, STARTING: 0, RUNNING: 0 },
  dispatch_paused: false,
  limits: {},
  generated_at: new Date(NOW).toISOString(),
}

/** Ended at 03:00Z (20:00 PDT on the 25th) and 10:00Z (03:00 PDT on the 26th). */
const PAGE: OverviewTaskPage = {
  tasks: [
    task({ id: 'tsk_a', state: 'SUCCEEDED', created_at: '2026-10-25T20:00:00Z', completed_at: '2026-10-26T03:00:00Z' }),
    task({ id: 'tsk_b', state: 'FAILED', created_at: '2026-10-26T09:00:00Z', completed_at: '2026-10-26T10:00:00Z' }),
  ],
}

beforeEach(() => {
  vi.useFakeTimers({ now: NOW, toFake: ['Date'] })
  window.localStorage.clear()
  resetTimeZone()
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  window.localStorage.clear()
  resetTimeZone()
})

function finishedToday(): Element {
  const { container } = render(<LifecycleBand stats={ok(STATS)} tasks={ok(PAGE)} />)
  return [...container.querySelectorAll('#ov-band .ov-lc')][2]!
}

describe("Overview's 'today' is the person's day", () => {
  /** MUTATION: name a zone other than UTC when nobody has picked one. */
  it('counts from 00:00 UTC by default, and says so', () => {
    const done = finishedToday()
    expect(text(done.querySelector('.ov-lc-h small'))).toBe('since 00:00 UTC')
    expect(text(done.querySelector('.ov-lc-n'))).toBe('2')
  })

  /** MUTATION: keep `isTodayUtc` in LifecycleBand, or the fixed 'since 00:00 UTC'. */
  it('counts from local midnight in the picked zone, and names it', () => {
    notePerson('ada@example.com')
    setZone(LA)
    const done = finishedToday()
    expect(text(done.querySelector('.ov-lc-h small'))).toBe('since 00:00 PDT')
    // 03:00Z is 20:00 PDT yesterday: only the 10:00Z failure finished today.
    expect(text(done.querySelector('.ov-lc-n'))).toBe('1')
  })

  /** MUTATION: read the zone once rather than subscribing to it. */
  it('follows a pick made while the page is open', () => {
    notePerson('ada@example.com')
    const done = finishedToday()
    expect(text(done.querySelector('.ov-lc-h small'))).toBe('since 00:00 UTC')
    act(() => {
      setZone(LA)
    })
    expect(text(done.querySelector('.ov-lc-h small'))).toBe('since 00:00 PDT')
    expect(text(done.querySelector('.ov-lc-n'))).toBe('1')
  })
})

describe("Overview's times of day are in the person's zone, labelled", () => {
  /** MUTATION: put back the UTC-only `hhmmUtc` in `readReach`. */
  it('says how far a partial read reaches in the zone', () => {
    expect(readReach('2026-10-26T16:06:00Z')).toBe('partial: agents created since 16:06 UTC read')
    notePerson('ada@example.com')
    setZone(LA)
    expect(readReach('2026-10-26T16:06:00Z')).toBe('partial: agents created since 09:06 PDT read')
  })

  /** MUTATION: put back the UTC-only `hhmmUtc` in `waitGroups`. */
  it("words a parked group's next eligible time in the zone", () => {
    const parked = [task({ id: 'tsk_p', state: 'PARKED', park_reason: 'PROVIDER_QUOTA_EXHAUSTED', next_eligible_at: '2026-10-26T18:51:00Z' })]
    expect(waitGroups(parked, NOW)[0]?.detail).toMatch(/next eligible 18:51 UTC/)
    notePerson('ada@example.com')
    setZone(LA)
    expect(waitGroups(parked, NOW)[0]?.detail).toMatch(/next eligible 11:51 PDT/)
  })
})
