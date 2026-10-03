// #92, the Holders half (overview attention -> holders): the silent-workers
// item links here, so the table has to answer "which worker is silent". It
// had no heartbeat column at all.
//
// Each test names the mutation that turns it red.

import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { HoldersBoard } from '../api'
import type { Result } from '../fetch'
import type { LeasePage, LeaseRow } from '../types'

const api = vi.hoisted(() => ({ loadHolders: vi.fn() }))
vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { HoldersScreen } = await import('../Holders')

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-09-24T10:00:00Z' }
}

function lease(id: string, over: Partial<LeaseRow>): LeaseRow {
  return {
    lease_id: `lease-${id}`,
    task_id: id,
    attempt_id: `att-${id}`,
    tenant_id: 'eng',
    generation: 1,
    pools: ['global'],
    units: 1,
    dispatch_state: 'DISPATCHED',
    created_at: '2026-09-24T09:00:00Z',
    dispatch_deadline: '2026-09-24T09:05:00Z',
    expires_at: '2026-09-24T11:00:00Z',
    heartbeat_at: '2026-09-24T09:59:00Z',
    released_at: null,
    release_reason: null,
    released: false,
    expired: false,
    dispatch_overdue: false,
    silent_seconds: 30,
    heartbeat_ever: true,
    last_error: null,
    ...over,
  }
}

function board(leases: LeaseRow[]): HoldersBoard {
  return {
    page: {
      leases,
      thresholds: { heartbeat_grace_seconds: 90, lease_timeout_seconds: 120 },
      evaluated_at: '2026-09-24T10:00:00Z',
      active_only: true,
      tenant_id: null,
      units_held: leases.length,
      active_beyond_window: 0,
      truncated: false,
    } as unknown as LeasePage,
    pools: [],
    poolsDetail: null,
  }
}

const text = (n: Element | null | undefined): string => (n?.textContent ?? '').replace(/\s+/g, ' ').trim()

describe('#92: holders, the overview silent-workers link’s destination, has a heartbeat column', () => {
  /**
   * MUTATION: drop the Heartbeat column, or colour it from a local threshold
   * instead of the page's `heartbeat_grace_seconds`.
   */
  it('says, per lease, how long its worker has been silent and whether that is past the grace', async () => {
    api.loadHolders.mockResolvedValue(
      ok(
        board([
          lease('tsk_beating', { silent_seconds: 30 }),
          lease('tsk_quiet', { silent_seconds: 400 }),
          lease('tsk_gone', { silent_seconds: 900, expired: true }),
          lease('tsk_never', { silent_seconds: 45, heartbeat_ever: false, heartbeat_at: null }),
        ]),
      ),
    )
    render(<HoldersScreen />)
    await screen.findByText('Every holder', undefined, { timeout: 5000 })
    const heads = [...document.querySelectorAll('.hold-all thead th')].map(text)
    expect(heads).toContain('Heartbeat')

    const cell = (id: string): Element => {
      const row = [...document.querySelectorAll('.hold-all tbody tr')].find((r) =>
        r.querySelector(`a[title="${id}"]`),
      )
      expect(row, `no holder row for ${id}`).toBeDefined()
      const c = row!.querySelector('td[data-label="Heartbeat"]')
      expect(c, `${id} has no heartbeat cell`).not.toBeNull()
      return c!
    }
    expect(text(cell('tsk_beating'))).toBe('beating · 30s')
    expect(cell('tsk_beating').querySelector('.sk-st[data-tone="ok"]')).not.toBeNull()
    expect(text(cell('tsk_quiet'))).toBe('silent · 6m 40s')
    expect(cell('tsk_quiet').querySelector('.sk-st[data-tone="warn"]')).not.toBeNull()
    expect(text(cell('tsk_gone'))).toBe('presumed dead · 15m 0s')
    expect(cell('tsk_gone').querySelector('.sk-st[data-tone="bad"]')).not.toBeNull()
    // Never beaten: the age counts from the lease's creation, and says so.
    expect(text(cell('tsk_never'))).toBe('beating · never beat, 45s')
  })
})
