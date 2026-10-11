// LIVE QA, ADMIN › POOL LIMITS (2026-10-11).
//
//   * ADM-7: the screen reads again on Capacity's cadence, so In use and the
//     editor's impact are not reasoned from a read nobody refreshed;
//   * ADM-9: a `?pool=` naming no pool says so, rather than drawing the full
//     table with nothing marked and no word.

import { render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { Capacity, Pool } from '../types'
import { POOLS_POLL_MS } from '../capacityPoll'

const api = vi.hoisted(() => ({
  loadCapacity: vi.fn(),
  loadAdminPools: vi.fn(),
  loadMe: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => {
  const real = await importOriginal<typeof import('../api')>()
  return { ...real, ...api }
})

const { AdminSettingsScreen } = await import('../AdminSettings')

const WAIT = { timeout: 5000 } as const

afterEach(() => {
  vi.useRealTimers()
  window.location.hash = ''
  api.loadCapacity.mockReset()
  api.loadAdminPools.mockReset()
  api.loadMe.mockReset()
})

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-10-11T12:00:00Z' }
}

function pool(name: string, active: number): Pool {
  return {
    name,
    hard_limit: 100,
    adaptive_target: null,
    quota_derived_limit: null,
    effective_limit: 100,
    active,
    available: 100 - active,
    enabled: true,
    updated_at: '2026-10-11T12:00:00Z',
  }
}

function capacity(active: number): Capacity {
  return {
    pools: [pool('global', active), pool('tenant:eng', 2)],
    runner_profiles: {
      'claude-code': { resource_class: 'standard', backend: 'cloudrun', provider: 'anthropic', units: 1, pools: ['global', 'tenant:eng'] },
    },
    tenant_id: 'eng',
    generated_at: '2026-10-11T12:00:00Z',
  }
}

function serve(): void {
  api.loadAdminPools.mockResolvedValue(ok({ pools: [] }))
  api.loadMe.mockResolvedValue(undefined)
}

describe('live QA admin: Pool limits', () => {
  it('ADM-7: reads the pools again on the Capacity cadence', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    serve()
    api.loadCapacity.mockResolvedValueOnce(ok(capacity(14))).mockResolvedValue(ok(capacity(10)))
    render(<AdminSettingsScreen />)
    await waitFor(() => expect(document.getElementById('limit-global')).not.toBeNull(), WAIT)
    const reads = api.loadCapacity.mock.calls.length
    expect(reads).toBeGreaterThanOrEqual(1)
    // MUTATION: drop `pollMs` from the Screen: no second read is ever made.
    await vi.advanceTimersByTimeAsync(POOLS_POLL_MS + 1_000)
    await waitFor(() => expect(api.loadCapacity.mock.calls.length).toBeGreaterThan(reads), WAIT)
    await waitFor(() => expect(document.getElementById('limit-global')!.textContent).toContain('10'), WAIT)
  })

  it('ADM-9: a link to a pool that does not exist says there is no such pool', async () => {
    serve()
    api.loadCapacity.mockResolvedValue(ok(capacity(10)))
    window.location.hash = '#admin/limits?pool=nope%3Ax'
    render(<AdminSettingsScreen />)
    await waitFor(() => expect(document.getElementById('limit-global')).not.toBeNull(), WAIT)
    // MUTATION: draw no notice for an unknown `?pool=`.
    const said = document.querySelector('.adm-no-target')
    expect(said, 'an unknown ?pool= is ignored without a word').not.toBeNull()
    expect(said!.textContent).toContain('No pool named nope:x')
  })

  it('ADM-9: a link to a pool that exists draws no such notice', async () => {
    serve()
    api.loadCapacity.mockResolvedValue(ok(capacity(10)))
    window.location.hash = '#admin/limits?pool=tenant%3Aeng'
    render(<AdminSettingsScreen />)
    await waitFor(() => expect(document.querySelector('tr.is-target')?.id).toBe('limit-tenant:eng'), WAIT)
    expect(document.querySelector('.adm-no-target')).toBeNull()
  })
})
