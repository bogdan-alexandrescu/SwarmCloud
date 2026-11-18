// A CLIMBED TARGET IS DATED BY ITS CLIMB, NOT BY THE CUT.
//
// The quota broker writes `target_changed_at` only when the adaptive target
// moves, so a target the +1-per-successes step raised after the last rate
// limit is served with `set_by: 'climb'` and the instant it rose. Capacity ›
// Pools words it as `raised <at>`, beside `since the rate limit at` and `set`.
//
// BREAK IT: drop the `climb` branch in `ceilingTitle` -- the title reads
// `set <at>` and no longer says the target rose.

import { describe, expect, it } from 'vitest'

import { ceilingTitle } from '../Capacity'
import type { Pool } from '../types'

function climbed(): Pool {
  return {
    name: 'provider:anthropic:tenant:eng', hard_limit: 40, adaptive_target: 9, quota_derived_limit: null,
    effective_limit: 9, active: 9, available: 0, enabled: true,
    updated_at: '2026-10-24T14:05:00Z',
    lowered_by: [
      { input: 'adaptive_target', value: 9, set_at: '2026-10-24T13:40:00+00:00', set_by: 'climb',
        quota_written_at: '2026-10-24T14:05:00+00:00', last_rate_limited_at: '2026-10-24T10:30:00+00:00' },
    ],
  }
}

describe('a target that rose after the last cut is dated by its climb', () => {
  it('words a climb as raised at the instant the target last changed', () => {
    expect(ceilingTitle(climbed())).toBe(
      'Configured hard limit is 40; 9 applies, lowered by the adaptive target (9), raised 2026-10-24T13:40:00+00:00.',
    )
    expect(ceilingTitle(climbed())).not.toContain('10:30')
    expect(ceilingTitle(climbed())).not.toContain('14:05')
  })
})
