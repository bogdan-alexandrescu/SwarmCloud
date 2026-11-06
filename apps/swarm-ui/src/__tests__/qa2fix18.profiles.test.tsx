// QA2-FIX-18 (visual QA pass 2, part of #2694): Capacity › Pools › By runner
// profile, cap2-12.
//
// The count note read 'counts read 10d ago' beside the refresh chip's 'read
// 0 s ago': two reads of one page, ten days apart. The note's instant is the
// server's `generated_at`, when the counters were last reported; the chip is
// when this page last read them. The note now says it is the counters' report
// time, and its `<time>` says which instant it is.

import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { Capacity } from '../types'

const loadCapacity = vi.hoisted(() => vi.fn<() => Promise<Result<Capacity>>>())
vi.mock('../api', () => ({ loadCapacity }))

const { ProfilesScreen } = await import('../Profiles')

const WAIT = { timeout: 5000 } as const
const REPORTED = '2026-10-19T12:00:00Z'

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

function capacity(): Capacity {
  return {
    tenant_id: 'eng',
    generated_at: REPORTED,
    pools_complete: true,
    pools: [{
      name: 'global', hard_limit: 40, adaptive_target: null, quota_derived_limit: null,
      effective_limit: 40, active: 1, available: 39, enabled: true, updated_at: REPORTED,
    }],
    runner_profiles: {
      merge: {
        resource_class: 'standard', backend: 'CLOUD_RUN_JOB', provider: 'git', units: 1, pools: ['global'],
        admission: {
          units: 1, headroom: 39, basis: 'measured', blockers: [], binding: ['global'],
          counterfactual: [], complete: true, unread: [], uncapped: [],
        },
      },
    },
  } as Capacity
}

describe('cap2-12: the count note says the counters were reported, not read', () => {
  it('names the old instant as the counters’ report time, the read left to the refresh chip', async () => {
    // A page read just now off counters reported days ago: the fixture build's case.
    loadCapacity.mockResolvedValue({ status: 'ok', data: capacity(), fetchedAt: Date.now(), serverAt: REPORTED })
    render(<ProfilesScreen />)
    await waitFor(() => expect(screen.getAllByText('merge').length).toBeGreaterThan(0), WAIT)
    const at = document.querySelector<HTMLTimeElement>(`time[datetime="${REPORTED}"]`)!
    expect(at).not.toBeNull()
    const note = at.parentElement!
    // MUTATION: `CountsAge` back to `counts read{' '}`.
    expect(note.textContent).toMatch(/counters last reported \S/)
    expect(note.textContent).not.toMatch(/counts read/)
    // MUTATION: the `<time>`'s title back to the bare instant.
    expect(at.getAttribute('title')).toBe(`${REPORTED}: when the server last reported these counters. The refresh control says when this page last read them.`)
  })
})
