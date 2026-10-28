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
})
