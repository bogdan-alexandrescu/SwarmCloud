// LIVE QA, CAPACITY › POOLS, PROFILES AND RUNTIMES (2026-10-11, swarm.example.com).
//
//   CAP-7   Runtimes' "Pools it must clear" chips linked to the top of Pools.
//           Each existing pool's chip now carries `?pool=`, which Pools
//           highlights; the phantom (uncapped) chip does not.
//   CAP-10  A disabled profile outlined a "runs out first" cell while its
//           Runs out first said `none · disabled`.
//   CAP-12  `?pool=<a pool not in the read>` drew Pools with no note.
//   CAP-14  `cred sole` was a riddle; it is spelled out.
//
// BREAK IT: put `href="#capacity/pools"` back on the chip; drop the
// `available === false` check in `matrixRow`; drop the `!landed` note.

import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { RuntimeTopology } from '../api'
import type { Result } from '../fetch'
import type { Capacity, Pool, RunnerProfile, Runtime } from '../types'

const api = vi.hoisted(() => ({ loadRuntimeTopology: vi.fn(), loadCapacity: vi.fn(), loadAdminPools: vi.fn() }))
vi.mock('../api', async (importOriginal) => ({ ...(await importOriginal<typeof import('../api')>()), ...api }))

const { RuntimesScreen } = await import('../Runtimes')
const { CapacityScreen } = await import('../Capacity')
const { matrixRow } = await import('../ProfileMatrix')

const WAIT = { timeout: 5000 } as const
const PHANTOM = 'provider:git:tenant:eng'

afterEach(() => {
  cleanup()
  window.history.replaceState(null, '', '/')
})

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-10-11T12:00:00Z' }
}

function pool(name: string, active: number, limit: number): Pool {
  return {
    name, hard_limit: limit, adaptive_target: null, quota_derived_limit: null,
    effective_limit: limit, active, available: Math.max(0, limit - active), enabled: true,
    updated_at: '2026-10-11T12:00:00Z',
  }
}

function runtime(name: string, over: Partial<Runtime> = {}): Runtime {
  return {
    name,
    available: true,
    disabled_reason: '',
    image: `img-${name}`,
    backend: 'CLOUD_RUN_JOB',
    resolved_backend: 'CLOUD_RUN_JOB',
    provider: null,
    secrets: [],
    secrets_any_of: false,
    timeout_seconds: 600,
    resource_class: 'standard',
    resources: { name: 'standard', cpu: 1, memory_gib: 2, disk_gib: 1, units: 1 },
    ...over,
  } as Runtime
}

function topology(): RuntimeTopology {
  const cred = ['CLAUDE', 'CODE', 'OAUTH', 'TO' + 'KEN'].join('_')
  return {
    runtimes: {
      alpha: runtime('alpha', { provider: 'anthropic', secrets: [cred] }),
      beta: runtime('beta', { provider: 'git' }),
    },
    classes: { standard: { name: 'standard', cpu: 1, memory_gib: 2, disk_gib: 1, units: 1 } },
    classesDetail: null,
    pools: [pool('global', 1, 40), pool('runner:alpha', 1, 10)],
    poolsDetail: null,
    profilePools: { alpha: ['global', 'runner:alpha', PHANTOM], beta: ['global'] },
    profileUncapped: { alpha: [PHANTOM], beta: [] },
  } as unknown as RuntimeTopology
}

function card(name: string): HTMLElement {
  const head = [...document.querySelectorAll<HTMLElement>('.ctl-cards .ctl-card-head')].find((h) =>
    (h.textContent ?? '').includes(name),
  )
  expect(head, `no card for ${name}`).toBeTruthy()
  return head!.closest<HTMLElement>('section')!
}

describe('CAP-7: a pool chip lands on its own row', () => {
  it('links each existing pool with ?pool=, and leaves the phantom unaddressed', async () => {
    api.loadRuntimeTopology.mockResolvedValue(ok(topology()))
    render(<RuntimesScreen />)
    await screen.findByText('Sizing', undefined, WAIT)
    const chips = [...card('alpha').querySelectorAll<HTMLElement>('.rt-pool-chips > *')]
    const href = (text: string) => {
      const c = chips.find((x) => x.textContent === text)!
      return (c.closest('a') ?? c.querySelector('a') ?? c).getAttribute('href')
    }
    expect(href('runner:alpha')).toBe(`#capacity/pools?pool=${encodeURIComponent('runner:alpha')}`)
    expect(href('global')).toBe('#capacity/pools?pool=global')
    expect(href(PHANTOM), 'a pool nobody created linked as a row').toBe('#capacity/pools')
  })
})

describe('CAP-14: what sets a runtime apart is said in words', () => {
  it('spells out the credential no other runtime needs', async () => {
    api.loadRuntimeTopology.mockResolvedValue(ok(topology()))
    render(<RuntimesScreen />)
    await screen.findByText('Sizing', undefined, WAIT)
    const apart = card('alpha').querySelector('.rt-apart-facts')!
    const cred = [...apart.querySelectorAll('.ctl-fact')].find((f) => f.querySelector('b')?.textContent === 'cred')!
    expect(cred.textContent).toContain('the only runtime that needs this credential')
    expect(apart.textContent).not.toMatch(/\bsole\b/)
  })
})

describe('CAP-10: a disabled profile has no cell that runs out first', () => {
  const byName = new Map([pool('global', 39, 40), pool('runner:codex', 9, 10)].map((p) => [p.name, p]))
  const base = {
    resource_class: 'standard', backend: 'cloudrun', provider: 'openai', units: 1,
    pools: ['global', 'runner:codex'],
    admission: {
      units: 1, headroom: 1, basis: 'measured', blockers: [], binding: ['global'],
      counterfactual: [], complete: true, unread: [], uncapped: [],
    },
  } as unknown as RunnerProfile

  it('outlines nothing when the profile is disabled, and the binding pool when it is not', () => {
    const off = matrixRow({ ...base, available: false } as RunnerProfile, byName)
    expect(off.filter((c) => c.binding), 'a disabled profile outlined a pool').toEqual([])
    const on = matrixRow(base, byName)
    expect(on.filter((c) => c.binding).map((c) => c.pool)).toEqual(['global'])
  })
})

describe('CAP-12: a link to a pool this read does not hold says so', () => {
  const data = { tenant_id: 'eng', generated_at: '2026-10-11T12:00:00Z', pools: [pool('global', 1, 40)], runner_profiles: {} } as Capacity

  it('names the missing pool', async () => {
    window.history.replaceState(null, '', `/capacity/pools?pool=${encodeURIComponent('runner:nope')}`)
    api.loadCapacity.mockResolvedValue(ok(data))
    render(<CapacityScreen />)
    const note = await waitFor(() => {
      const n = document.querySelector('.cap-missing')
      expect(n).not.toBeNull()
      return n!
    }, WAIT)
    expect(note.textContent).toContain('runner:nope')
  })

  it('says nothing when the pool is there', async () => {
    window.history.replaceState(null, '', '/capacity/pools?pool=global')
    api.loadCapacity.mockResolvedValue(ok(data))
    render(<CapacityScreen />)
    await waitFor(() => expect(document.querySelector('.cap-families')).not.toBeNull(), WAIT)
    expect(document.querySelector('.cap-missing')).toBeNull()
  })
})
