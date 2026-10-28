// chrome-C16 (visual QA pass 1, QA1-FIX-24, part of #2596): a run's progress
// line printed the local clock with no zone, so '10-27 17:02:06' could not be
// set beside a log line or a CI run's time. BGD's rule: UTC times are written
// with Z.

import { afterEach, describe, expect, it } from 'vitest'
import { cleanup, render } from '@testing-library/react'

import { ProgressTime } from '../RunSteps'

afterEach(cleanup)

const NOW = Date.parse('2026-10-28T09:30:00.000Z')

describe('a progress line’s time', () => {
  it('is the UTC clock with its Z, and the date when it is not today', () => {
    const { container } = render(<ProgressTime at="2026-10-27T17:02:06.000Z" prev={null} now={NOW} />)
    const t = container.querySelector('time')!
    // MUTATION: read the local clock (getHours) and the Z is gone.
    expect(t.textContent).toBe('10-27 17:02:06Z')
    expect(t.getAttribute('dateTime')).toBe('2026-10-27T17:02:06.000Z')
  })

  it('is the clock alone today, UTC, with the gap since the line above', () => {
    const { container } = render(<ProgressTime at="2026-10-28T09:05:30.000Z" prev="2026-10-28T09:05:00.000Z" now={NOW} />)
    const t = container.querySelector('time')!
    expect(t.textContent).toMatch(/^09:05:30Z \+/)
  })

  // Where CI's clock is UTC the cases above prove only the Z: local and UTC
  // hours agree there. In Tokyo (UTC+9, no daylight saving) 17:02:06Z on the
  // 27th is 02:02 on the 28th, today, so a local reading prints neither the
  // date nor the hour.
  it('reads the UTC clock where the local zone is not UTC', () => {
    const before = process.env.TZ
    process.env.TZ = 'Asia/Tokyo'
    try {
      // The premise: the zone took, and the local day is the 28th.
      expect(new Date('2026-10-27T17:02:06.000Z').getDate()).toBe(28)
      const { container } = render(<ProgressTime at="2026-10-27T17:02:06.000Z" prev={null} now={NOW} />)
      // MUTATION: read getHours/getDate and this prints '02:02:06Z'.
      expect(container.querySelector('time')!.textContent).toBe('10-27 17:02:06Z')
    } finally {
      if (before === undefined) delete process.env.TZ
      else process.env.TZ = before
    }
  })
})
