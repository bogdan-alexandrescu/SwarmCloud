// A LARGE AGENT TAKES 1 UNIT IN EACH PROVIDER POOL (contract request 129, #2950).
//
// Admission now charges a lease its resource units in every compute pool and 1
// in each `provider:*` pool, and the API serves that per pool: a profile's
// `pool_units` on `/v1/capacity`, a lease row's `pool_units` and the page's
// `units_held_by_pool` on `/v1/admin/leases`. The console divided a provider
// pool's room by the agent's units in every pool, so it said one large agent
// fitted where four did, called a provider limit of 2 "below" a large agent,
// and drew a false +3 provider delta per large lease on the drift card.
//
// BREAK IT: make `unitsIn` ignore `pool_units` (every pool takes `units`), or
// sum `l.units` over `l.pools` in Holders.tsx's `driftRows` again -- every
// case below that names a provider pool goes red. Remove the `units` fallback
// and the cases for an older API (no `pool_units` served) go red instead.

import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { HoldersBoard } from '../api'
import type { Result } from '../fetch'
import type { LeasePage, LeaseRow, Pool, RunnerProfile, Task } from '../types'

const api = vi.hoisted(() => ({ loadHolders: vi.fn() }))
vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { HoldersScreen } = await import('../Holders')
const { fitsIn } = await import('../ProfileMatrix')
const { ceilingFigure } = await import('../Blockers')
const { blockerCeiling, ceilingCopy, costInWords, leaseHeld, taskWeight, unitsIn } = await import('../types')

const WAIT = { timeout: 5000 } as const
const PROVIDER = 'provider:anthropic'
const POOLS = ['global', 'tenant:eng', 'resource:large', 'runner:claude-code-large', PROVIDER]

/** A large profile as `/v1/capacity` serves it since contract request 129. */
const LARGE: Pick<RunnerProfile, 'units' | 'pools' | 'pool_units'> = {
  units: 4,
  pools: POOLS,
  pool_units: { global: 4, 'tenant:eng': 4, 'resource:large': 4, 'runner:claude-code-large': 4, [PROVIDER]: 1 },
}

function pool(name: string, active: number, limit: number): Pool {
  return {
    name, hard_limit: limit, adaptive_target: null, quota_derived_limit: null,
    effective_limit: limit, active, available: limit - active, enabled: true,
    updated_at: '2026-11-01T06:00:00Z',
  }
}

describe('one agent is weighed per pool, from what the API serves', () => {
  it('reads the served share, and `units` where nothing is served', () => {
    expect(unitsIn(LARGE, PROVIDER)).toBe(1)
    expect(unitsIn(LARGE, 'resource:large')).toBe(4)
    // A pool the map does not name takes `units`, as `held_pool_units` does.
    expect(unitsIn(LARGE, 'backend:GKE_AUTOPILOT')).toBe(4)
    // An API older than the field admitted by `units` in every pool.
    expect(unitsIn({ units: 4 }, PROVIDER)).toBe(4)
    expect(unitsIn(4, PROVIDER)).toBe(4)
    expect(unitsIn(null, PROVIDER)).toBeNull()
  })

  it('fits four large agents in four free provider slots, not one', () => {
    expect(fitsIn(pool(PROVIDER, 46, 50), LARGE)).toBe(4)
    expect(fitsIn(pool('resource:large', 5, 12), LARGE)).toBe(1)
    // Without `pool_units` the old division stands.
    expect(fitsIn(pool(PROVIDER, 46, 50), { units: 4 })).toBe(1)
  })

  it('says what one agent costs per pool, in words', () => {
    expect(costInWords(LARGE)).toBe('4 units in each of 4 pools, 1 unit in each of 1 pool')
    expect(costInWords({ units: 4, pools: ['global', 'resource:large', 'tenant:eng'] })).toBe('4 units in each of 3 pools')
  })
})

describe('a provider limit of 2 is not below a large agent', () => {
  const blocker = { reason: 'PROVIDER_CAPACITY', pool: PROVIDER, limit: 2, active: 2, group: null }

  it('is full, a wait, when the profile says the agent takes 1 there', () => {
    expect(blockerCeiling(blocker, LARGE)).toBe('full')
    expect(ceilingCopy(blocker, 'This pool', LARGE)).toBeNull()
    expect(ceilingFigure(blocker, LARGE)).toBe('2 of 2 units in use')
  })

  it('still finds a compute pool below the class, and names the share there', () => {
    const small = { reason: 'RESOURCE_CAPACITY', pool: 'resource:large', limit: 2, active: 0, group: null }
    expect(blockerCeiling(small, LARGE)).toBe('below-units')
    expect(ceilingFigure(small, LARGE)).toBe('limit 2 · one agent takes 4 units here · 0 units held')
    // A provider limit of 1 under a large agent's share of 1 is not below it either.
    expect(blockerCeiling({ ...blocker, limit: 1 }, LARGE)).toBe('full')
  })

  it("weighs a waiting task's blockers by its served waiting_for rows", () => {
    const task = {
      state: 'READY',
      blocked_by: [{ ...blocker }],
      waiting_for: {
        as_of: '2026-11-01T06:00:00Z', admissible_now: false, holds_capacity: false, lead: null, complete: true,
        pools: [
          { pool: 'resource:large', state: 'open', active: 0, limit: 40, units: 4, reason: null },
          { pool: PROVIDER, state: 'full', active: 2, limit: 2, units: 1, reason: 'PROVIDER_CAPACITY' },
        ],
      },
    } as unknown as Task
    expect(unitsIn(taskWeight(task, 4), PROVIDER)).toBe(1)
    expect(unitsIn(taskWeight(task, 4), 'resource:large')).toBe(4)
  })
})

function lease(n: number, extra: Partial<LeaseRow> = {}): LeaseRow {
  return {
    lease_id: `lease-${String(n).padStart(10, '0')}`,
    task_id: `task-${String(n).padStart(10, '0')}`,
    attempt_id: `att-${n}`,
    tenant_id: 'eng',
    generation: 1,
    pools: ['global', 'resource:large', PROVIDER],
    units: 4,
    dispatch_state: 'DISPATCHED',
    created_at: '2026-11-01T05:00:00Z',
    dispatch_deadline: '2026-11-01T05:05:00Z',
    expires_at: '2026-11-01T07:00:00Z',
    heartbeat_at: '2026-11-01T05:59:00Z',
    released_at: null,
    release_reason: null,
    released: false,
    expired: false,
    dispatch_overdue: false,
    silent_seconds: 30,
    heartbeat_ever: true,
    last_error: null,
    ...extra,
  }
}

describe('a lease holds what it took in each pool', () => {
  it('reads the served pool_units, and units in every pool for a lease from before', () => {
    const now = lease(1, { pool_units: { global: 4, 'resource:large': 4, [PROVIDER]: 1 } })
    expect(leaseHeld(now)).toEqual({ global: 4, 'resource:large': 4, [PROVIDER]: 1 })
    expect(leaseHeld(lease(2))).toEqual({ global: 4, 'resource:large': 4, [PROVIDER]: 4 })
  })
})

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-11-01T06:00:00Z' }
}

/**
 * Two large leases: one reserved since request 129 (1 in the provider pool),
 * one before it (4 there). The counters hold what admission charged: 8 on
 * `global` and `resource:large`, 5 on the provider pool.
 */
function board(page: Partial<LeasePage>): HoldersBoard {
  return {
    page: {
      leases: [
        lease(1, { pool_units: { global: 4, 'resource:large': 4, [PROVIDER]: 1 } }),
        lease(2),
      ],
      thresholds: { heartbeat_grace_seconds: 90, lease_timeout_seconds: 120 },
      evaluated_at: '2026-11-01T06:00:00Z',
      active_only: true,
      tenant_id: null,
      units_held: 8,
      active_beyond_window: 0,
      truncated: false,
      examined: 2,
      ...page,
    } as LeasePage,
    pools: [pool('global', 8, 100), pool('resource:large', 8, 40), pool(PROVIDER, 5, 50)],
    poolsDetail: null,
  }
}

function driftCard(): Element {
  const title = [...document.querySelectorAll('.ctl-card-title')].find((t) =>
    (t.textContent ?? '').startsWith('Accounting drift'),
  )
  expect(title, 'no accounting-drift card').toBeTruthy()
  return title!.closest('.ctl-card')!
}

describe('the drift card compares what each lease holds per pool', () => {
  it('finds no provider delta for large leases, summing the rows by their pool_units', async () => {
    api.loadHolders.mockResolvedValue(ok(board({})))
    render(<HoldersScreen />)
    await screen.findByText('Every holder', undefined, WAIT)
    // Summing `units` in every pool read 8 on the provider pool against its 5.
    expect(driftCard().querySelector('.ctl-mark.is-zero')?.textContent).toBe('none')
  })

  it('reads the served units_held_by_pool first', async () => {
    // The served sum says the provider pool holds 4, its counter says 5: a
    // real delta the card must show, whatever the rows sum to.
    api.loadHolders.mockResolvedValue(ok(board({ units_held_by_pool: { global: 8, 'resource:large': 8, [PROVIDER]: 4 } })))
    render(<HoldersScreen />)
    await screen.findByText('Every holder', undefined, WAIT)
    expect(driftCard().querySelector('.ctl-mark.is-zero')).toBeNull()
    expect(driftCard().textContent).toContain(PROVIDER.replace('provider:', ''))
  })
})
