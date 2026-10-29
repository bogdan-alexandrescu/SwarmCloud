// LIVE QA, CAPACITY › HOLDERS (2026-10-11, swarm.example.com).
//
//   CAP-5  `?pool=nope:x` read "No lease holds nope:x right now · real zero":
//          a measured zero for a pool that does not exist. It now says the
//          capacity read has no such pool, and draws no real zero.
//   CAP-6  `?pool=runner:mock` read "0 of 12" and then "12 rows" over an
//          empty table. The note counts what is drawn, out of what was read.
//   CAP-9  "Dispatch: dispatched" beside a 33-minute hold read as a stuck
//          dispatch. The column says what the lease records.
//
// BREAK IT: drop the `unknownPool` branch -- CAP-5 finds a real zero again;
// print the unfiltered count alone -- CAP-6 reads "3 rows".

import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { HoldersBoard } from '../api'
import type { Result } from '../fetch'
import type { LeasePage, LeaseRow, Pool } from '../types'

const api = vi.hoisted(() => ({ loadHolders: vi.fn() }))
vi.mock('../api', async (importOriginal) => ({ ...(await importOriginal<typeof import('../api')>()), ...api }))

const { HoldersScreen } = await import('../Holders')

const WAIT = { timeout: 5000 } as const

afterEach(() => {
  cleanup()
  window.history.replaceState(null, '', '/')
})

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-10-11T12:00:00Z' }
}

function lease(n: number, pools: string[]): LeaseRow {
  return {
    lease_id: `lease-${String(n).padStart(10, '0')}`,
    task_id: `task-${String(n).padStart(10, '0')}`,
    attempt_id: `att-${n}`,
    tenant_id: 'eng',
    generation: 1,
    pools,
    units: 1,
    dispatch_state: 'DISPATCHED',
    created_at: '2026-10-11T11:00:00Z',
    dispatch_deadline: '2026-10-11T11:05:00Z',
    expires_at: '2026-10-11T13:00:00Z',
    heartbeat_at: '2026-10-11T11:59:00Z',
    released_at: null,
    release_reason: null,
    released: false,
    expired: false,
    dispatch_overdue: false,
    silent_seconds: 5,
    heartbeat_ever: true,
    last_error: null,
  } as LeaseRow
}

function pool(name: string, active: number): Pool {
  return {
    name, hard_limit: 40, adaptive_target: null, quota_derived_limit: null,
    effective_limit: 40, active, available: 40 - active, enabled: true, updated_at: '2026-10-11T12:00:00Z',
  }
}

function board(): HoldersBoard {
  return {
    page: {
      leases: [lease(1, ['global', 'runner:codex']), lease(2, ['global', 'runner:codex']), lease(3, ['global', 'runner:claude-code'])],
      thresholds: { heartbeat_grace_seconds: 90, lease_timeout_seconds: 120 },
      evaluated_at: '2026-10-11T12:00:00Z',
      active_only: true,
      tenant_id: null,
      units_held: 3,
      truncated: false,
      active_beyond_window: 0,
    } as unknown as LeasePage,
    pools: [pool('global', 3), pool('runner:codex', 2), pool('runner:claude-code', 1), pool('runner:mock', 0)],
    poolsDetail: null,
  }
}

async function at(poolName: string): Promise<HTMLElement> {
  window.history.replaceState(null, '', `/capacity/holders?pool=${encodeURIComponent(poolName)}`)
  api.loadHolders.mockResolvedValue(ok(board()))
  render(<HoldersScreen />)
  return (await screen.findByText('Every holder', undefined, WAIT)).closest('section')!
}

describe('CAP-5: a pool that does not exist is not a zero', () => {
  it('says the capacity read has no such pool, and draws no real zero for it', async () => {
    const table = await at('nope:x')
    const row = await waitFor(() => {
      const r = table.querySelector('tbody tr.hold-empty')
      expect(r).not.toBeNull()
      return r!
    }, WAIT)
    expect(row.textContent).toContain('No pool is named nope:x')
    expect([...row.querySelectorAll('.ctl-mark')].map((m) => m.textContent), 'a zero claimed for an unmeasured pool').not.toContain('none')
  })

  it('still says real zero for a pool that exists and nobody holds', async () => {
    const table = await at('runner:mock')
    const row = await waitFor(() => {
      const r = table.querySelector('tbody tr.hold-empty')
      expect(r).not.toBeNull()
      return r!
    }, WAIT)
    expect(row.textContent).toContain('No lease holds mock')
    expect(row.querySelector('[title="runner:mock"]')).not.toBeNull()
    expect([...row.querySelectorAll('.ctl-mark')].map((m) => m.textContent)).toContain('none')
  })
})

describe('CAP-6: a filtered table counts what it draws', () => {
  it('reads 0 of 3 rows over an empty filter, never 3 rows', async () => {
    const table = await at('runner:mock')
    const note = table.querySelector('.ctl-toolbar .ctl-card-note')!
    await waitFor(() => expect(note.textContent).toMatch(/^0 of 3 rows/), WAIT)
  })

  it('reads 2 of 3 rows for a pool two leases hold, and the plain count with no filter', async () => {
    const table = await at('runner:codex')
    await waitFor(() => expect(table.querySelectorAll('tbody tr:not(.hold-empty)')).toHaveLength(2), WAIT)
    expect(table.querySelector('.ctl-toolbar .ctl-card-note')!.textContent).toMatch(/^2 of 3 rows/)
    cleanup()
    window.history.replaceState(null, '', '/capacity/holders')
    render(<HoldersScreen />)
    const all = (await screen.findByText('Every holder', undefined, WAIT)).closest('section')!
    expect(all.querySelector('.ctl-toolbar .ctl-card-note')!.textContent).toMatch(/^3 rows/)
  })
})

describe('CAP-9: the lease column says what the lease records', () => {
  it('is headed Lease written, not Dispatch', async () => {
    const table = await at('runner:codex')
    const heads = [...table.querySelectorAll('thead th')].map((th) => th.textContent)
    expect(heads).toContain('Lease written')
    expect(heads).not.toContain('Dispatch')
  })
})
