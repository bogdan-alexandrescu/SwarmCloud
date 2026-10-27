// ONE CLOCK READ FOR A SCHEDULE FIXTURE (CLOCK-ONCE, part of #2391; BGD
// 2026-10-26, "Read the clock once in the fixtures").
//
// THE DEFECT. The schedules fixtures built every timestamp from its own
// `Date.now()`, and the page read the clock again when it rendered. A slow
// runner put milliseconds between those reads, and two figures sit on a
// rounding edge: a firing whose end was read 1 ms after its start was 60m +
// 1ms long, which `pageScale` rounds up to "+1h15m"; a next run built an hour
// out and drawn a second later was 59m59s away, which `untilWords` says as
// "in 60m" (PR 2468's CI run 40355734084).
//
// THE RULE. A fixture takes no clock reads: every timestamp is an offset from
// SCHEDULE_NOW, and `holdScheduleClock()` stops `Date` at that instant, so the
// page's `Date.now()` and `useNow()` read the same one. Only `Date` is faked:
// timers stay real, so `waitFor` and `findBy*` poll as before.

import { vi } from 'vitest'

/** The one instant a schedules test is set at. */
export const SCHEDULE_NOW = Date.parse('2026-10-26T12:00:00.000Z')

/** Stop `Date` at `at` (SCHEDULE_NOW): call in `beforeEach`; `vi.useRealTimers()` in `afterEach`. */
export function holdScheduleClock(at: number = SCHEDULE_NOW): void {
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(at)
}

/** `m` minutes before `now`, never a fresh clock read. */
export const minutesAgo = (m: number, now: number = SCHEDULE_NOW): string => new Date(now - m * 60_000).toISOString()

/** `h` hours after `now`, never a fresh clock read. */
export const hoursFromNow = (h: number, now: number = SCHEDULE_NOW): string => new Date(now + h * 3_600_000).toISOString()
