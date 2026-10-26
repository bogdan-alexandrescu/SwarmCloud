// THE SCHEDULES FIXTURES READ THE CLOCK ONCE (CLOCK-ONCE, part of #2391; BGD
// 2026-10-26, "Read the clock once in the fixtures").
//
// WHAT EACH CASE HOLDS, on a clock that moves between two reads the way a slow
// runner's does:
//   * a firing built the old way -- start and end each from their own
//     `Date.now()` -- is 60m + 1ms long when the clock moves 1 ms between the
//     reads, and the page's scale says "+1h15m"; built from `scheduleclock`'s
//     one instant it is 60m and says "+1h", and the builder reads no clock;
//   * a next run built the old way an hour out and drawn a second later is
//     "in 60m"; under `holdScheduleClock` the fixture and the page read the
//     same instant and it is "in 1h" (PR 2468's CI run 40355734084).
//
// The old builder is kept here, verbatim, as the control: it is what
// schedules.head, schedules.history-timeline and schedules.onepage used.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { offsetWords, pageScale } from '../ScheduleHistoryTimeline'
import { untilWords } from '../Schedules'
import { SCHEDULE_NOW, holdScheduleClock, minutesAgo } from './scheduleclock'

/** The fixtures' old helper: one fresh clock read per timestamp. */
const oldMinutesAgo = (m: number) => new Date(Date.now() - m * 60_000).toISOString()

/** A `Date.now` that moves `stepMs` on every read, from SCHEDULE_NOW. */
function movingClock(stepMs: number) {
  let t = SCHEDULE_NOW
  return vi.spyOn(Date, 'now').mockImplementation(() => (t += stepMs))
}

/** The page's last axis tick for a firing that ran from `fired` to `ended`. */
const lastTick = (fired: string, ended: string) => offsetWords(pageScale(Date.parse(ended) - Date.parse(fired)).span)

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('the schedules fixtures read the clock once', () => {
  it('a firing an hour long is +1h, not +1h15m, when the clock moves 1 ms between reads', () => {
    movingClock(1)
    // The control: two reads, 1 ms apart, and the axis rounds 60m + 1ms up.
    expect(lastTick(oldMinutesAgo(1440), oldMinutesAgo(1440 - 60))).toBe('+1h15m')
    const reads = movingClock(1)
    // MUTATION: `minutesAgo` back on `Date.now()` and this reads +1h15m.
    expect(lastTick(minutesAgo(1440), minutesAgo(1440 - 60))).toBe('+1h')
    expect(reads, 'the fixture read the clock').not.toHaveBeenCalled()
  })

  it('a next run an hour out is "in 1h", not "in 60m", when the page draws a second later', () => {
    movingClock(1000)
    // The control: the fixture's read, then the page's a second later.
    expect(untilWords(oldMinutesAgo(-60), Date.now())).toBe('in 60m')
    vi.restoreAllMocks()
    // MUTATION: drop `holdScheduleClock` and the page's read is the moving one.
    holdScheduleClock()
    const next = minutesAgo(-60)
    expect(untilWords(next, Date.now())).toBe('in 1h')
    expect(Date.now(), 'the page and the fixture read two instants').toBe(SCHEDULE_NOW)
  })
})
