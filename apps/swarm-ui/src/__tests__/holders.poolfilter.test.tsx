// HOLDERS, FILTERED TO ONE POOL BY THE LINK THAT NAMED IT (#125).
//
// A Pools row links its name to `#capacity/holders?pool=<name>`. Holders
// reads the pool off the address and draws only the leases that name it,
// says which pool it is narrowed to, and offers the way back to every pool.
//
// BREAK IT: ignore `?pool=` in Holders -- all three leases draw. Or filter on
// the tenant rather than the lease's `pools` -- `lease-2` (eng, no
// runner:codex) is drawn.

import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { HoldersBoard } from '../api'
import type { Result } from '../fetch'
import type { LeasePage, LeaseRow } from '../types'

const api = vi.hoisted(() => ({ loadHolders: vi.fn() }))
vi.mock('../api', async (importOriginal) => ({ ...(await importOriginal<typeof import('../api')>()), ...api }))

const { HoldersScreen } = await import('../Holders')

const WAIT = { timeout: 5000 } as const

afterEach(() => {
  window.history.replaceState(null, '', '/')
})

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-10-02T10:00:00Z' }
}

function lease(n: number, pools: string[], tenant = 'eng'): LeaseRow {
  return {
    lease_id: `lease-${String(n).padStart(10, '0')}`,
    task_id: `task-${String(n).padStart(10, '0')}`,
    attempt_id: `att-${n}`,
    tenant_id: tenant,
    generation: 1,
    pools,
    units: 1,
    dispatch_state: 'DISPATCHED',
    created_at: '2026-10-02T09:00:00Z',
    dispatch_deadline: '2026-10-02T09:05:00Z',
    expires_at: '2026-10-02T11:00:00Z',
    heartbeat_at: '2026-10-02T09:59:00Z',
    released_at: null,
    release_reason: null,
    released: false,
    expired: false,
    dispatch_overdue: false,
    silent_seconds: 30,
    heartbeat_ever: true,
    last_error: null,
  } as LeaseRow
}

function board(): HoldersBoard {
  return {
    page: {
      leases: [
        lease(1, ['global', 'runner:codex']),
        lease(2, ['global', 'runner:claude-code']),
        lease(3, ['global', 'runner:codex'], 'research'),
      ],
      thresholds: { heartbeat_grace_seconds: 90, lease_timeout_seconds: 120 },
      evaluated_at: '2026-10-02T10:00:00Z',
      active_only: true,
      tenant_id: null,
      units_held: 3,
      truncated: false,
      active_beyond_window: 0,
    } as unknown as LeasePage,
    pools: null,
    poolsDetail: 'not read in this test',
  }
}

/** The task ids the Every holder table draws. */
function drawn(): string[] {
  const section = screen.getByText('Every holder').closest('section')!
  return [...section.querySelectorAll('tbody th a')].map((a) => a.getAttribute('title') ?? '?').sort()
}

describe('Holders narrows to the pool a link named (#125)', () => {
  it('draws only the leases that name the pool, and says which pool', async () => {
    window.history.replaceState(null, '', `/capacity/holders?pool=${encodeURIComponent('runner:codex')}`)
    api.loadHolders.mockResolvedValue(ok(board()))
    render(<HoldersScreen />)
    await screen.findByText('Every holder', undefined, WAIT)
    await waitFor(() => expect(drawn()).toEqual(['task-0000000001', 'task-0000000003']), WAIT)
    const chip = document.querySelector('.hold-pool')
    expect(chip, 'nothing says the table is narrowed to a pool').not.toBeNull()
    expect(chip!.textContent).toContain('runner:codex')
  })

  it('offers every pool again, and draws them all', async () => {
    window.history.replaceState(null, '', `/capacity/holders?pool=${encodeURIComponent('runner:codex')}`)
    api.loadHolders.mockResolvedValue(ok(board()))
    render(<HoldersScreen />)
    await screen.findByText('Every holder', undefined, WAIT)
    fireEvent.click(screen.getByRole('button', { name: 'All pools' }))
    await waitFor(() => expect(drawn()).toHaveLength(3), WAIT)
    expect(document.querySelector('.hold-pool')).toBeNull()
  })

  it('without a linked pool, draws every lease and no pool chip', async () => {
    api.loadHolders.mockResolvedValue(ok(board()))
    render(<HoldersScreen />)
    await screen.findByText('Every holder', undefined, WAIT)
    expect(drawn()).toHaveLength(3)
    expect(document.querySelector('.hold-pool')).toBeNull()
  })
})
