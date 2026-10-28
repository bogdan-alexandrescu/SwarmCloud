// LANE QA1-FIX-08 (console visual QA pass 1, 2026-10-28; part of #2596): the
// Pools findings whose file is Capacity.tsx.
//
//   capacity-01  every row's `limit` link went to `#admin/limits`, an admin
//                route, so a member was dropped on Overview with no message.
//                A member's Pools page links to no admin-only route.
//   capacity-02  a pool holding 5 units printed `0 holders` when no lease on
//                the read named it: a count the two reads disagree about is
//                `holders`, with the reason, never 0.
//   capacity-12  the phone row read `0 /6 /10`: the configured limit is said
//                in words there (`lowered from 10`), not as a second `/N`.
//   capacity-14  backend pools were named by their enum (`CLOUD_RUN_JOB`);
//                they read `Cloud Run` / `GKE Autopilot`, the id kept in the
//                mono sub-line.

import { render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { Capacity, LeasePage, LeaseRow, Pool } from '../types'

const api = vi.hoisted(() => ({ loadCapacity: vi.fn(), loadLeases: vi.fn(), loadMe: vi.fn() }))
vi.mock('../api', async (importOriginal) => ({ ...(await importOriginal<typeof import('../api')>()), ...api }))

const { CapacityScreen } = await import('../Capacity')

const WAIT = { timeout: 5000 } as const

afterEach(() => {
  window.history.replaceState(null, '', '/')
  document.body.innerHTML = ''
  vi.clearAllMocks()
  vi.unstubAllGlobals()
})

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-10-28T10:00:00Z' }
}

function pool(name: string, active: number, limit: number, over: Partial<Pool> = {}): Pool {
  return {
    name, hard_limit: limit, adaptive_target: null, quota_derived_limit: null,
    effective_limit: limit, active, available: Math.max(0, limit - active), enabled: true,
    updated_at: '2026-10-28T10:00:00Z', ...over,
  }
}

function board(): Capacity {
  return {
    tenant_id: 'eng',
    generated_at: '2026-10-28T10:00:00Z',
    pools_complete: true,
    pools: [
      pool('global', 4, 50),
      pool('backend:CLOUD_RUN_JOB', 5, 20),
      pool('backend:GKE_AUTOPILOT', 0, 10),
      // Lowered: AIMD holds it at 6 of its configured 10.
      pool('tenant:eng', 0, 10, { effective_limit: 6, adaptive_target: 6, available: 6 }),
      pool('runner:codex', 0, 8),
    ],
    runner_profiles: {},
  } as unknown as Capacity
}

function lease(n: number, pools: string[]): LeaseRow {
  return {
    lease_id: `lease-${String(n).padStart(10, '0')}`, task_id: `task-${String(n).padStart(10, '0')}`, attempt_id: `att-${n}`,
    tenant_id: 'eng', generation: 1, pools, units: 1, dispatch_state: 'DISPATCHED',
    created_at: '2026-10-28T09:00:00Z', dispatch_deadline: '2026-10-28T09:05:00Z', expires_at: '2026-10-28T11:00:00Z',
    heartbeat_at: '2026-10-28T09:59:00Z', released_at: null, release_reason: null, released: false, expired: false,
    dispatch_overdue: false, silent_seconds: 30, heartbeat_ever: true, last_error: null,
  } as unknown as LeaseRow
}

/** A complete lease read whose rows name only `global`, as the fixture's do. */
function leases(): LeasePage {
  const rows = [1, 2, 3, 4].map((n) => lease(n, ['global']))
  return {
    leases: rows, thresholds: { heartbeat_grace_seconds: 90, lease_timeout_seconds: 120 },
    evaluated_at: '2026-10-28T10:00:00Z', active_only: true, tenant_id: null, units_held: 4,
    truncated: false, active_beyond_window: 0,
  } as unknown as LeasePage
}

function me(isAdmin: boolean) {
  return ok({ principal: { email: 'alice@example.com', is_admin: isAdmin } })
}

async function renderPools(isAdmin: boolean): Promise<void> {
  api.loadCapacity.mockResolvedValue(ok(board()))
  api.loadLeases.mockResolvedValue(ok(leases()))
  api.loadMe.mockResolvedValue(me(isAdmin))
  render(<CapacityScreen />)
  await waitFor(() => expect(document.querySelector('.cap-families tbody tr')).not.toBeNull(), WAIT)
}

function rowOf(name: string): HTMLElement {
  const th = [...document.querySelectorAll('.cap-families tbody th[title]')].find((t) => t.getAttribute('title') === name)
  expect(th, `no row for ${name}`).toBeTruthy()
  return th!.closest('tr') as HTMLElement
}

const flat = (el: Element | null | undefined) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()

describe('capacity-01: a member is never linked to an admin-only route', () => {
  it('draws no `limit` link, and no `#admin/` href at all, for a member', async () => {
    await renderPools(false)
    // MUTATION: draw the `limit` link whatever `useIsAdmin` says -- 5 admin hrefs.
    const admin = [...document.querySelectorAll('.cap-families a')].filter((a) => (a.getAttribute('href') ?? '').startsWith('#admin/'))
    expect(admin.map((a) => a.getAttribute('href'))).toEqual([])
    // The holders link stays, alone: no dangling ` · ` after it.
    expect(flat(rowOf('global').querySelector('.cap-open'))).toBe('4 holders')
  })

  it('keeps `limit` for an admin (control)', async () => {
    await renderPools(true)
    await waitFor(() => expect(flat(rowOf('global').querySelector('.cap-open'))).toBe('4 holders · limit'), WAIT)
    const limit = [...rowOf('global').querySelectorAll('.cap-open a')].find((a) => a.textContent === 'limit')
    expect(limit!.getAttribute('href')).toBe('#admin/limits?pool=global')
  })
})

describe('capacity-02: a pool with leased units never reads `0 holders`', () => {
  it('says `holders`, with why, when no lease on the read names a pool that holds units', async () => {
    await renderPools(false)
    const link = rowOf('backend:CLOUD_RUN_JOB').querySelector('.cap-open a')!
    // MUTATION: drop the `pool.active > 0` arm in HoldersLink -- `0 holders`.
    expect(link.textContent).toBe('holders')
    expect(link.getAttribute('title')).toMatch(/5 units are leased on this pool, yet no live lease on the lease read names it/)
    // A pool with nothing leased is a counted zero (control), and a counted pool its count.
    expect(rowOf('runner:codex').querySelector('.cap-open a')!.textContent).toBe('0 holders')
    expect(rowOf('global').querySelector('.cap-open a')!.textContent).toBe('4 holders')
  })
})

describe('capacity-12: the phone row says the configured limit in words', () => {
  it('reads `lowered from 10` beside the ceiling on the stacked row, not `/10`', async () => {
    vi.stubGlobal('matchMedia', (q: string) => ({
      matches: q === '(max-width: 560px)', media: q, addEventListener() {}, removeEventListener() {},
    }))
    await renderPools(false)
    const ceil = rowOf('tenant:eng').querySelector('.cap-ceil')!
    // MUTATION: keep `/{hard_limit}` on the stacked row -- `6/10`, read as `0 /6 /10`.
    expect(flat(ceil)).toBe('6 lowered from 10')
    expect(ceil.querySelector('.cap-was')!.textContent).toBe('lowered from 10')
    expect(flat(ceil)).not.toMatch(/\/10/)
  })

  it('keeps `6/10` in the desktop table, where Leased and Ceiling are columns (control)', async () => {
    await renderPools(false)
    expect(flat(rowOf('tenant:eng').querySelector('.cap-ceil'))).toBe('6/10')
  })
})

describe('capacity-14: a backend pool is named in words, its id kept under it', () => {
  it('reads `Cloud Run` and `GKE Autopilot`, with `backend:CLOUD_RUN_JOB` as the sub-line', async () => {
    await renderPools(false)
    const row = rowOf('backend:CLOUD_RUN_JOB')
    // MUTATION: `poolLabel(pool.name)` for the name -- `CLOUD_RUN_JOB`.
    expect(row.querySelector('th a')!.textContent).toBe('Cloud Run')
    expect(row.querySelector('th .ctl-sub')!.textContent).toBe('backend:CLOUD_RUN_JOB')
    expect(rowOf('backend:GKE_AUTOPILOT').querySelector('th a')!.textContent).toBe('GKE Autopilot')
    expect(row.querySelector('[aria-label^="Cloud Run: 5 of 20"]')).not.toBeNull()
    // Every other pool keeps its label (control).
    expect(rowOf('tenant:eng').querySelector('th a')!.textContent).toBe('eng')
  })
})
