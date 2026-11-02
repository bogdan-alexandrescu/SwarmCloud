// THE APPLIED LIMIT BESIDE THE CEILING, AND WHY (#3047).
//
// On 24 October admission refused steps on the provider limit while 6 or 7
// agents ran against a ceiling of 40, and nothing said why. Capacity › Pools
// draws the applied value with `/40` beside it; the `/40`'s title now says
// which input lowered it and when the broker last set it, from the
// `lowered_by` /v1/capacity serves.
//
// BREAK IT: go back to the title `Configured hard limit is 40` -- the input
// and its instant are gone.

import { render, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { Capacity, Pool } from '../types'

const api = vi.hoisted(() => ({ loadCapacity: vi.fn() }))
vi.mock('../api', async (importOriginal) => ({ ...(await importOriginal<typeof import('../api')>()), ...api }))

const { CapacityScreen, ceilingTitle } = await import('../Capacity')

const WAIT = { timeout: 5000 } as const
const NAME = 'provider:anthropic:tenant:eng'

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-10-24T11:00:00Z' }
}

function lowered(): Pool {
  return {
    name: NAME, hard_limit: 40, adaptive_target: 7, quota_derived_limit: null,
    effective_limit: 7, active: 7, available: 0, enabled: true,
    updated_at: '2026-10-24T10:31:00Z',
    lowered_by: [
      { input: 'adaptive_target', value: 7, set_at: '2026-10-24T10:31:00+00:00',
        last_rate_limited_at: '2026-10-24T10:30:00+00:00' },
    ],
  }
}

describe('#3047: the Capacity page says what lowered a ceiling, and when', () => {
  it('words the applied value, the ceiling, the input and its instant', () => {
    expect(ceilingTitle(lowered())).toBe(
      'Configured hard limit is 40; 7 applies, lowered by the adaptive target (7), set 2026-10-24T10:31:00+00:00.',
    )
    expect(ceilingTitle({ ...lowered(), lowered_by: [] })).toBe('Configured hard limit is 40; 7 applies.')
  })

  it('draws it on the pool row beside the applied value', async () => {
    const data: Capacity = {
      tenant_id: 'eng', generated_at: '2026-10-24T11:00:00Z', pools: [lowered()], runner_profiles: {},
    }
    api.loadCapacity.mockResolvedValue(ok(data))
    render(<CapacityScreen />)
    await waitFor(() => expect(document.querySelector('.cap-families tbody tr .cap-was')).not.toBeNull(), WAIT)
    const was = document.querySelector('.cap-was')!
    expect(was.textContent).toBe('/40')
    expect(was.getAttribute('title')).toContain('lowered by the adaptive target (7), set 2026-10-24T10:31:00+00:00')
  })
})
