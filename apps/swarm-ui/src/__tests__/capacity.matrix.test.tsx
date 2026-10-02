// Pools › By runner profile: the profile-by-pool matrix (capacity.html §B,
// decided 2026-10-01, #124). Each row outlines EXACTLY the pool that limits
// that profile -- the server's `admission.binding` -- and no other cell.
//
// Outlining one cell too many sends an operator to raise a pool that changes
// nothing; one too few hides the pool that does. Both are asserted.
//
// BREAK IT: outline the cell with the least room instead of the server's
// binding list -- `codex` below then outlines tenant:eng (2 free) rather than
// runner:codex. Or outline only the first binding pool -- `browser`, where two
// pools tie, loses one.

import { fireEvent, render, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { Capacity, Pool, ProfileAdmission, RunnerProfile } from '../types'

const loadCapacity = vi.hoisted(() => vi.fn<() => Promise<Result<Capacity>>>())
vi.mock('../api', () => ({ loadCapacity }))

const { ProfilesScreen } = await import('../Profiles')
const { matrixRow } = await import('../ProfileMatrix')

function pool(name: string, active: number, limit: number): Pool {
  return {
    name, hard_limit: limit, adaptive_target: null, quota_derived_limit: null,
    effective_limit: limit, active, available: Math.max(0, limit - active), enabled: true,
    updated_at: '2026-10-01T10:00:00Z',
  }
}

function admission(over: Partial<ProfileAdmission>): ProfileAdmission {
  return {
    units: 1, headroom: 1, basis: 'measured', blockers: [], binding: [],
    counterfactual: [], complete: true, unread: [], uncapped: [], ...over,
  }
}

function profile(over: Partial<RunnerProfile>): RunnerProfile {
  return { resource_class: 'standard', backend: 'cloudrun', provider: 'anthropic', units: 1, pools: [], ...over }
}

const POOLS = [
  pool('global', 31, 40),
  pool('tenant:eng', 18, 20),
  pool('resource:standard', 26, 40),
  pool('resource:browser', 4, 4),
  pool('runner:claude-code', 12, 20),
  pool('runner:codex', 9, 10),
  pool('runner:browser', 4, 4),
  pool('backend:cloudrun', 27, 40),
  pool('backend:gke', 4, 4),
  pool('provider:anthropic', 22, 22),
]

const CAPACITY: Capacity = {
  tenant_id: 'eng',
  generated_at: '2026-10-01T10:00:00Z',
  pools: POOLS,
  runner_profiles: {
    // The provider binds, though tenant:eng has less room in units: the
    // server's list is what is outlined, never a client minimum.
    'claude-code': profile({
      pools: ['global', 'tenant:eng', 'resource:standard', 'runner:claude-code', 'backend:cloudrun', 'provider:anthropic'],
      admission: admission({ headroom: 0, binding: ['provider:anthropic'] }),
    }),
    // runner:codex binds at 1 free while tenant:eng has 2.
    codex: profile({
      provider: null,
      pools: ['global', 'tenant:eng', 'resource:standard', 'runner:codex', 'backend:cloudrun'],
      admission: admission({ headroom: 1, binding: ['runner:codex'] }),
    }),
    // Two pools tie and both bind.
    browser: profile({
      resource_class: 'browser', backend: 'gke', provider: null,
      pools: ['global', 'tenant:eng', 'resource:browser', 'runner:browser', 'backend:gke'],
      admission: admission({ headroom: 0, binding: ['resource:browser', 'backend:gke'] }),
    }),
  },
}

/** The pools whose cell is outlined in one profile's matrix row. */
function outlined(name: string): string[] {
  const row = document.querySelector(`.cap-mx tr[data-profile="${name}"]`)
  expect(row, `the matrix has no row for ${name}`).not.toBeNull()
  return [...row!.querySelectorAll('td.is-binding')].map((td) => td.getAttribute('data-pool') ?? '?').sort()
}

describe('the profile matrix outlines exactly the binding cell', () => {
  it('outlines the pool the server says limits each profile, and no other cell in its row', async () => {
    loadCapacity.mockResolvedValue({ status: 'ok', data: CAPACITY, fetchedAt: Date.now(), serverAt: CAPACITY.generated_at })
    render(<ProfilesScreen />)
    await waitFor(() => expect(document.querySelector('.cap-mx tr[data-profile]')).not.toBeNull())

    expect(outlined('claude-code')).toEqual(['provider:anthropic'])
    expect(outlined('codex')).toEqual(['runner:codex'])
    expect(outlined('browser')).toEqual(['backend:gke', 'resource:browser'])

    // Control: every row has cells that are NOT outlined, so the assertion
    // above could have found more than it did.
    for (const name of ['claude-code', 'codex', 'browser']) {
      const row = document.querySelector(`.cap-mx tr[data-profile="${name}"]`)!
      expect(row.querySelectorAll('td[data-pool]:not(.is-binding)').length).toBeGreaterThan(0)
    }
  })

  it('draws the units free in each cell, with leased/ceiling under it', async () => {
    loadCapacity.mockResolvedValue({ status: 'ok', data: CAPACITY, fetchedAt: Date.now(), serverAt: CAPACITY.generated_at })
    render(<ProfilesScreen />)
    await waitFor(() => expect(document.querySelector('.cap-mx tr[data-profile]')).not.toBeNull())
    const cell = document.querySelector('.cap-mx tr[data-profile="codex"] td[data-pool="runner:codex"]')!
    expect(cell.querySelector('b')?.textContent).toBe('1')
    expect(cell.querySelector('small')?.textContent).toBe('9/10')
  })

  // RE-POINTED (#124): the opened row is a per-pool Fits table now, not a
  // sentence; what runs out first is the outlined row of that table.
  it('opens a row into a table that marks what runs out first', async () => {
    loadCapacity.mockResolvedValue({ status: 'ok', data: CAPACITY, fetchedAt: Date.now(), serverAt: CAPACITY.generated_at })
    render(<ProfilesScreen />)
    await waitFor(() => expect(document.querySelector('.cap-mx tr[data-profile]')).not.toBeNull())
    const toggle = document.querySelector<HTMLButtonElement>('.cap-mx tr[data-profile="claude-code"] button')!
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(toggle)
    expect(toggle.getAttribute('aria-expanded')).toBe('true')
    const exp = document.querySelector('.cap-mx tr.cap-mx-exp')
    expect(exp?.querySelector('tr.is-binding')?.getAttribute('data-pool')).toBe('provider:anthropic')
  })

  it('takes the binding pool over a roomier pool of the same family', () => {
    const byName = new Map([
      ['provider:anthropic', pool('provider:anthropic', 1, 30)],
      ['provider:anthropic:tenant:eng', pool('provider:anthropic:tenant:eng', 10, 10)],
    ])
    const cells = matrixRow(
      profile({
        pools: ['provider:anthropic', 'provider:anthropic:tenant:eng'],
        admission: admission({ binding: ['provider:anthropic:tenant:eng'] }),
      }),
      byName,
    )
    const provider = cells.find((c) => c.family === 'provider')!
    expect(provider.pool).toBe('provider:anthropic:tenant:eng')
    expect(provider.binding).toBe(true)
    expect(cells.filter((c) => c.binding)).toHaveLength(1)
  })
})
