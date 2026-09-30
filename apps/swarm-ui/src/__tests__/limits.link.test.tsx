// A TENANT'S ENFORCED LINK LANDS ON ITS POOL'S ROW, THROUGH THE ROUTER (#134).
//
// The Tenants roster links each Enforced figure to
// `#admin/limits?pool=tenant:<id>`. The first cut read the query when Pool
// limits mounted and was tested on the screen alone, with no router -- and
// passed, while in the app the normalise effect had already rewritten the
// address to `#admin/limits` before the screen's async read drew a row, so
// the link opened the page at no row at all. This renders the whole App.

import { render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { Capacity, Pool } from '../types'

const api = vi.hoisted(() => ({
  loadCapacity: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => {
  const real = await importOriginal<typeof import('../api')>()
  return { ...real, ...api }
})

const { App, canonical, fromHash } = await import('../App')

const WAIT = { timeout: 5000 } as const

afterEach(() => {
  window.location.hash = ''
})

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-09-29T10:00:00Z' }
}

function pool(name: string): Pool {
  return {
    name,
    hard_limit: 10,
    adaptive_target: null,
    quota_derived_limit: null,
    effective_limit: 10,
    active: 2,
    available: 8,
    enabled: true,
    updated_at: '2026-09-29T10:00:00Z',
  }
}

function capacity(): Capacity {
  return {
    pools: [pool('global'), pool('tenant:eng'), pool('tenant:research')],
    runner_profiles: {
      'claude-code': { resource_class: 'standard', backend: 'cloudrun', provider: 'anthropic', units: 1, pools: ['global', 'tenant:eng'] },
    },
    tenant_id: 'eng',
    generated_at: '2026-09-29T10:00:00Z',
  }
}

describe('Pool limits keeps the row a link named (#134)', () => {
  it('keeps ?pool= on the canonical address of Pool limits, and nowhere else', () => {
    window.location.hash = '#admin/limits?pool=tenant%3Aresearch'
    const r = fromHash()
    expect(canonical(r)).toBe('admin/limits?pool=tenant%3Aresearch')
    window.location.hash = '#admin/tenants?pool=tenant%3Aresearch'
    expect(canonical(fromHash())).toBe('admin/tenants')
  })

  it('marks exactly the linked row when the whole app is opened at the link', async () => {
    api.loadCapacity.mockResolvedValue(ok(capacity()))
    window.location.hash = '#admin/limits?pool=tenant%3Aresearch'
    render(<App />)
    await waitFor(() => expect(document.getElementById('limit-tenant:research')).not.toBeNull(), WAIT)
    await waitFor(() => expect(document.querySelectorAll('tr.is-target').length).toBe(1), WAIT)
    expect(document.querySelector('tr.is-target')!.id).toBe('limit-tenant:research')
    expect(window.location.hash).toBe('#admin/limits?pool=tenant%3Aresearch')
  })

  it('moves the mark when a second link is followed on the mounted screen', async () => {
    api.loadCapacity.mockResolvedValue(ok(capacity()))
    window.location.hash = '#admin/limits?pool=tenant%3Aresearch'
    render(<App />)
    await waitFor(() => expect(document.querySelector('tr.is-target')?.id).toBe('limit-tenant:research'), WAIT)
    window.location.hash = '#admin/limits?pool=tenant%3Aeng'
    window.dispatchEvent(new HashChangeEvent('hashchange'))
    await waitFor(() => expect(document.querySelector('tr.is-target')?.id).toBe('limit-tenant:eng'), WAIT)
    expect(document.querySelectorAll('tr.is-target').length).toBe(1)
  })
})
