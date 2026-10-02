// PROVIDER QUOTA'S CAP AND THE POOL'S CEILING, TOLD APART (#128).
//
// `Quota cap 50` on Provider quota and `Ceiling 20` on Pools are two figures:
// the first is the quota document's own cap, ONE input to the pool's ceiling.
// Already true before this file: the column is `Quota cap` and the row names
// the pool it feeds (`honesty.admin.test.tsx`, CP-8). What this file adds:
//
//   * the Feeds pool link lands on THAT pool's row on Pools
//     (`#capacity/pools?pool=provider:X:tenant:Y`), where `Set by` says which
//     figure binds;
//   * `setBy` has a term of its own for the 0 a quota STATE forces
//     (EXHAUSTED, DISABLED or COOLDOWN drive `quota_derived_limit` to 0,
//     quota_broker/aimd.py `quota_derived_limit_for`), so Pools no longer
//     calls that `provider quota` as though the provider had merely lowered
//     the cap.
//
// BREAK IT: drop the `quota_derived_limit === 0` branch in `setBy` -- the term
// falls back to `provider quota`. Or link Feeds pool to `#capacity/pools`
// again -- the href names no pool.

import { render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { Capacity, Pool, QuotaState } from '../types'

const api = vi.hoisted(() => ({ loadAdminQuota: vi.fn(), loadCapacity: vi.fn() }))
vi.mock('../api', async (importOriginal) => ({ ...(await importOriginal<typeof import('../api')>()), ...api }))

const { QuotaDetailScreen } = await import('../QuotaDetail')
const { CapacityScreen } = await import('../Capacity')
const { setBy } = await import('../types')

const WAIT = { timeout: 5000 } as const

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: new Date().toISOString() }
}

function quota(over: Partial<QuotaState>): QuotaState {
  return {
    provider: 'anthropic', tenant_id: 'eng', state: 'AVAILABLE', updated_at: new Date().toISOString(),
    configured_hard_max: 50, adaptive_target: null, quota_derived_limit: null, requests_remaining: 120,
    tokens_remaining: null, reset_at: null, cooldown_until: null, last_429_at: null,
    retry_after_seconds: null, success_count: 10, rate_limit_count: 0, effective_limit: 50, ...over,
  } as QuotaState
}

function pool(name: string, over: Partial<Pool> = {}): Pool {
  return {
    name, hard_limit: 20, adaptive_target: null, quota_derived_limit: null, effective_limit: 20,
    active: 0, available: 20, enabled: true, updated_at: new Date().toISOString(), ...over,
  }
}

describe('Provider quota links each row to the pool its cap feeds (#128)', () => {
  it('links Feeds pool to that pool\'s row on Pools', async () => {
    api.loadAdminQuota.mockResolvedValue(ok({ quota: [quota({}), quota({ tenant_id: 'research' })] }))
    render(<QuotaDetailScreen />)
    const [th] = await screen.findAllByRole('rowheader', { name: 'research' }, WAIT)
    const link = th!.closest('tr')!.querySelector('td[data-label="Feeds pool"] a')!
    expect(link.getAttribute('href')).toBe(`#capacity/pools?pool=${encodeURIComponent('provider:anthropic:tenant:research')}`)
  })
})

describe('setBy names the zero a quota state forces (#128)', () => {
  it('calls a state-forced 0 a quota state, not a provider quota', () => {
    const by = setBy(pool('provider:anthropic:tenant:eng', { quota_derived_limit: 0, effective_limit: 0, available: 0 }))
    expect(by.term).toBe('quota state')
    expect(by.detail).toMatch(/EXHAUSTED, DISABLED or COOLDOWN/)
    expect(by.detail).toContain('20')
  })

  it('still calls a positive quota cap a provider quota', () => {
    expect(setBy(pool('provider:anthropic:tenant:eng', { quota_derived_limit: 5, effective_limit: 5 })).term).toBe('provider quota')
  })

  it('draws the term in Pools\' Set by column', async () => {
    const data: Capacity = {
      tenant_id: 'eng',
      generated_at: new Date().toISOString(),
      pools: [pool('global'), pool('provider:anthropic:tenant:eng', { quota_derived_limit: 0, effective_limit: 0, available: 0 })],
      runner_profiles: {},
    }
    api.loadCapacity.mockResolvedValue(ok(data))
    render(<CapacityScreen />)
    await waitFor(() => {
      const th = [...document.querySelectorAll('.cap-families tbody th[title]')].find(
        (t) => t.getAttribute('title') === 'provider:anthropic:tenant:eng',
      )
      expect(th).toBeTruthy()
      expect(th!.closest('tr')!.querySelector('td[data-label="Set by"]')!.textContent).toBe('quota state')
    }, WAIT)
  })
})
