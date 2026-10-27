// A CHART'S TIME AXIS ON THE PERSON'S CLOCK (VQ-023, owner decision
// 2026-10-26; lane TIMEZONE-SHARED, part of #2391, review of 2026-10-26).
// `TimeSeries` drew its axis with d3's UTC ticks and captioned it with a
// literal ". Times UTC." whatever zone the person picked. Its ticks, their
// words and the caption's zone now come from `charts/series.ts`.
//
// MUTATIONS: put the literal ". Times UTC." back, or drop the axis's
// `tickValues`/`tickFormat` from `clockTicks`/`clockTickText` (d3's UTC
// ticks) -- each turns a case red.

import { cleanup, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { TimeSeries } from '../charts/TimeSeries'
import { measured, type ChartPoint } from '../charts/series'
import { notePerson, resetTimeZone, setZone } from '../timezone'

const HOUR = 3_600_000
/** 06:00Z on 2026-10-26, 23:00 PDT the evening before; one point an hour for a day. */
const T0 = Date.parse('2026-10-26T06:00:00Z')
const POINTS: ChartPoint[] = Array.from({ length: 25 }, (_, i) => measured(T0 + i * HOUR, `run ${i}`, i))

function chart() {
  return render(
    <TimeSeries points={POINTS} title="Runs over a day" noun="run" format={String} absentCopy="no reading." zero="anchored" />,
  )
}

/** The wide drawing's time-axis words. */
function timeTicks(container: HTMLElement): string[] {
  const wide = container.querySelector('svg.is-wide')!
  return [...wide.querySelectorAll('.visx-axis-bottom .ctl-chart-tick text')].map((t) => t.textContent ?? '')
}

beforeEach(() => {
  window.localStorage.clear()
  resetTimeZone()
})

afterEach(() => {
  cleanup()
  window.localStorage.clear()
  resetTimeZone()
})

describe('TimeSeries draws its time axis in the setting, UTC by default', () => {
  it('ticks on UTC hours, the date on 00:00 UTC, and says Times UTC when nothing is picked', () => {
    const { container } = chart()
    expect(timeTicks(container)).toEqual(['06:00', '12:00', '18:00', 'Oct 27'])
    expect(container.querySelector('figcaption')!.textContent).toContain('Times UTC.')
  })

  it('ticks on Los Angeles hours, the date on its midnight, and says Times PDT once that is picked', () => {
    notePerson('pat@example.com')
    setZone('America/Los_Angeles')
    const { container } = chart()
    // Oct 26 06:00Z is 23:00 PDT on the 25th; the next 6-hourly LA ticks are 00:00 (Oct 26), 06:00, 12:00, 18:00.
    expect(timeTicks(container)).toEqual(['Oct 26', '06:00', '12:00', '18:00'])
    const caption = container.querySelector('figcaption')!.textContent!
    expect(caption).toContain('Times PDT.')
    expect(caption).not.toContain('UTC')
  })
})
