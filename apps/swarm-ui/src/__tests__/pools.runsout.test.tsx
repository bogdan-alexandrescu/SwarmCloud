// POOLS ALONE SAYS WHICH CEILING TO RAISE FOR EACH PROFILE (#124).
//
// Pools › By runner profile is where Profile headroom went (#432): the tab is
// gone, its route is `/capacity/pools/profiles`, and the matrix is the screen.
// What the issue still asked for, and the matrix did not draw:
//
//   * a `Runs out first` column naming EVERY binding pool with its Fits -- how
//     many more of this profile that pool alone has room for -- so the one
//     ceiling to raise is on the row, not in a sentence behind a click;
//   * an opened row that is a per-pool Fits table with a `+N if lifted`
//     column, in place of the counterfactual sentences.
//
// BREAK IT: name only `binding[0]` in the column -- `browser`, where two pools
// tie, loses `backend:gke`. Or compute Fits as `available` without dividing by
// the profile's units -- `browser` weighs 2, so resource:browser's 3 free
// units would read 3 rather than 1. Or drop the counterfactual lookup -- the
// opened `claude-code` row loses its `+3`.

import { fireEvent, render, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { Capacity, Pool, ProfileAdmission, RunnerProfile } from '../types'

const loadCapacity = vi.hoisted(() => vi.fn<() => Promise<Result<Capacity>>>())
vi.mock('../api', async (importOriginal) => ({ ...(await importOriginal<typeof import('../api')>()), loadCapacity }))

const { ProfilesScreen } = await import('../Profiles')
const { fitsIn } = await import('../ProfileMatrix')
const { SECTIONS } = await import('../App')
const { addressToPath } = await import('../paths')

const WAIT = { timeout: 5000 } as const

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

const CAPACITY: Capacity = {
  tenant_id: 'eng',
  generated_at: '2026-10-01T10:00:00Z',
  pools: [
    pool('global', 31, 40),
    pool('tenant:eng', 18, 20),
    pool('resource:standard', 26, 40),
    pool('resource:browser', 5, 8),
    pool('runner:claude-code', 12, 20),
    pool('backend:cloudrun', 27, 40),
    pool('backend:gke', 6, 8),
    pool('provider:anthropic', 22, 22),
  ],
  runner_profiles: {
    'claude-code': profile({
      pools: ['global', 'tenant:eng', 'resource:standard', 'runner:claude-code', 'backend:cloudrun', 'provider:anthropic'],
      admission: admission({
        headroom: 0,
        binding: ['provider:anthropic'],
        counterfactual: [
          { pool: 'provider:anthropic', action: 'raise', headroom_after: 3, basis_after: 'measured', delta: 3, next_binding: ['tenant:eng'] },
        ],
      }),
    }),
    // Two pools tie and both bind. Weighs 2 units, so Fits is free units / 2.
    browser: profile({
      resource_class: 'browser', backend: 'gke', provider: null, units: 2,
      pools: ['global', 'tenant:eng', 'resource:browser', 'backend:gke'],
      admission: admission({ units: 2, headroom: 1, binding: ['resource:browser', 'backend:gke'] }),
    }),
  },
}

async function renderBoard(): Promise<void> {
  loadCapacity.mockResolvedValue({ status: 'ok', data: CAPACITY, fetchedAt: Date.now(), serverAt: CAPACITY.generated_at })
  render(<ProfilesScreen />)
  await waitFor(() => expect(document.querySelector('.cap-mx tr[data-profile]')).not.toBeNull(), WAIT)
}

function row(name: string): HTMLElement {
  const r = document.querySelector<HTMLElement>(`.cap-mx tr[data-profile="${name}"]`)
  expect(r, `the matrix has no row for ${name}`).not.toBeNull()
  return r!
}

describe('Pools › By runner profile names what runs out first (#124)', () => {
  it('has a Runs out first column and no counterfactual sentence column', async () => {
    await renderBoard()
    const heads = [...document.querySelectorAll('.cap-mx thead th')].map((th) => (th.textContent ?? '').trim())
    expect(heads).toContain('Runs out first')
    expect(heads, 'the sentence column is still on the row').not.toContain('If lifted')
  })

  it('lists every binding pool with how many more of the profile it fits', async () => {
    await renderBoard()
    const cc = row('claude-code').querySelector('td[data-label="Runs out first"]')!
    const ccItems = [...cc.querySelectorAll('[data-pool]')].map((el) => el.getAttribute('data-pool'))
    expect(ccItems).toEqual(['provider:anthropic'])
    expect(cc.textContent).toContain('anthropic')
    expect(cc.textContent).toContain('fits 0')

    const br = row('browser').querySelector('td[data-label="Runs out first"]')!
    const brItems = [...br.querySelectorAll('[data-pool]')].map((el) => el.getAttribute('data-pool')).sort()
    expect(brItems, 'a tie lost one of the binding pools').toEqual(['backend:gke', 'resource:browser'])
    // resource:browser: 3 free units, 2 per browser task -> fits 1.
    const res = br.querySelector('[data-pool="resource:browser"]')!
    expect(res.textContent).toContain('fits 1')
  })

  it('opens a row into a per-pool Fits table with a +N if lifted column', async () => {
    await renderBoard()
    const toggle = row('claude-code').querySelector<HTMLButtonElement>('button.cap-mx-toggle')!
    fireEvent.click(toggle)
    const exp = document.querySelector('.cap-mx tr.cap-mx-exp')
    expect(exp).not.toBeNull()
    const table = exp!.querySelector('table')
    expect(table, 'the opened row is not a table').not.toBeNull()
    const heads = [...table!.querySelectorAll('thead th')].map((th) => (th.textContent ?? '').trim())
    expect(heads).toEqual(expect.arrayContaining(['Pool', 'Fits', '+N if lifted']))
    // One row per pool the profile clears.
    const rows = [...table!.querySelectorAll('tbody tr[data-pool]')]
    expect(rows.map((r) => r.getAttribute('data-pool'))).toEqual(CAPACITY.runner_profiles['claude-code']!.pools)
    const anthropic = table!.querySelector('tbody tr[data-pool="provider:anthropic"]')!
    expect(anthropic.querySelector('td[data-label="Fits"]')!.textContent).toBe('0')
    expect(anthropic.querySelector('td[data-label="+N if lifted"]')!.textContent).toBe('+3')
    expect(anthropic.className).toContain('is-binding')
    // tenant:eng has 2 free units for a 1-unit profile, and does not bind.
    const tenant = table!.querySelector('tbody tr[data-pool="tenant:eng"]')!
    expect(tenant.querySelector('td[data-label="Fits"]')!.textContent).toBe('2')
    expect(tenant.className).not.toContain('is-binding')
    // No counterfactual sentence survives in the opened row.
    expect(exp!.textContent).not.toMatch(/if anthropic lifted/)
  })

  it('works Fits out per task, never per unit, and never invents a figure', () => {
    expect(fitsIn(pool('resource:browser', 5, 8), 2)).toBe(1)
    expect(fitsIn(pool('global', 40, 40), 1)).toBe(0)
    expect(fitsIn({ ...pool('global', 1, 4), effective_limit: null, hard_limit: null, available: null }, 1)).toBeNull()
    expect(fitsIn(null, 1)).toBeNull()
    // Paused: `available` is still 12, but admission refuses the pool.
    expect(fitsIn({ ...pool('provider:anthropic', 10, 22), enabled: false }, 1)).toBe(0)
  })

  it('says a paused binding pool fits 0, in the column and the opened table', async () => {
    const paused: Capacity = {
      ...CAPACITY,
      pools: CAPACITY.pools.map((p) => (p.name === 'provider:anthropic' ? { ...pool(p.name, 10, 22), enabled: false } : p)),
      runner_profiles: {
        'claude-code': {
          ...CAPACITY.runner_profiles['claude-code']!,
          admission: admission({
            headroom: 0,
            binding: ['provider:anthropic'],
            counterfactual: [
              { pool: 'provider:anthropic', action: 'resume', headroom_after: 2, basis_after: 'measured', delta: 2, next_binding: ['tenant:eng'] },
            ],
          }),
        },
      },
    }
    loadCapacity.mockResolvedValue({ status: 'ok', data: paused, fetchedAt: Date.now(), serverAt: paused.generated_at })
    render(<ProfilesScreen />)
    await waitFor(() => expect(document.querySelector('.cap-mx tr[data-profile]')).not.toBeNull(), WAIT)
    const cell = row('claude-code').querySelector('td[data-label="Runs out first"]')!
    expect(cell.querySelector('[data-pool="provider:anthropic"]')!.textContent).toContain('fits 0')
    fireEvent.click(row('claude-code').querySelector<HTMLButtonElement>('button.cap-mx-toggle')!)
    const exp = document.querySelector('.cap-mx tr.cap-mx-exp')!
    const anthropic = exp.querySelector('tbody tr[data-pool="provider:anthropic"]')!
    expect(anthropic.querySelector('td[data-label="Fits"]')!.textContent).toBe('0')
  })
})

describe('Profile headroom is a view of Pools, not a tab of its own (#124)', () => {
  it('no Capacity tab is called Profile headroom, and the profile view lives under the Pools path', () => {
    const capacity = SECTIONS.find((s) => s.id === 'capacity')!
    const labels = capacity.tabs.map((t) => t.label)
    expect(labels).not.toContain('Profile headroom')
    expect(labels).toContain('By runner profile')
    expect(addressToPath('capacity/profiles').startsWith('/capacity/pools/')).toBe(true)
  })
})
