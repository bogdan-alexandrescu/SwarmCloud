// A REAL ZERO IS DRAWN BY `Mark`, SO IT CARRIES ITS SENTENCE (part of #76).
//
// The Accounts empty pool and Holders' two measured zeros -- the accounting
// drift card over every live lease, and a pool filter no lease matches -- drew
// `.ctl-mark is-zero` by hand: two visible words, no `role="img"`, no
// accessible name. A screen reader heard "real zero" with nothing to say what
// was read or what came back empty. `Mark` publishes `say` as the mark's name.
//
// BREAK IT: put back `<span className="ctl-mark is-zero">real zero</span>` in
// either screen -- `getAllByRole('img', { name })` finds nothing and the
// every-zero-has-a-role sweep names the bare span.

import { render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { AccountsBoard, HoldersBoard } from '../api'
import type { Result } from '../fetch'
import type { AccountsPage, LeasePage, LeaseRow, Pool } from '../types'

const api = vi.hoisted(() => ({
  loadAccountsBoard: vi.fn(),
  beginAccountSignIn: vi.fn(),
  finishAccountSignIn: vi.fn(),
  refreshAccount: vi.fn(),
  removeAccount: vi.fn(),
  setAccountLending: vi.fn(),
  setAccountState: vi.fn(),
  loadHolders: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => ({ ...(await importOriginal<typeof import('../api')>()), ...api }))

const { AccountsScreen } = await import('../Accounts')
const { HoldersScreen } = await import('../Holders')

const WAIT = { timeout: 5000 } as const

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: new Date().toISOString() }
}

function lease(n: number): LeaseRow {
  return {
    lease_id: `lease-${String(n).padStart(10, '0')}`,
    task_id: `task-${String(n).padStart(10, '0')}`,
    attempt_id: `att-${n}`,
    tenant_id: 'eng',
    generation: 1,
    pools: ['global', 'resource:standard'],
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
  }
}

function pool(name: string, active: number): Pool {
  return {
    name,
    hard_limit: 40,
    adaptive_target: null,
    quota_derived_limit: null,
    effective_limit: 40,
    active,
    available: 40 - active,
    enabled: true,
    updated_at: '2026-09-24T10:00:00Z',
  }
}

/** Every live lease, read whole, agreeing with every counter. */
function holders(): HoldersBoard {
  return {
    page: {
      leases: [lease(1), lease(2)],
      thresholds: { heartbeat_grace_seconds: 90, lease_timeout_seconds: 120 },
      evaluated_at: '2026-09-24T10:00:00Z',
      active_only: true,
      tenant_id: null,
      units_held: 2,
      truncated: false,
      active_beyond_window: 0,
    } as unknown as LeasePage,
    pools: [pool('global', 2), pool('resource:standard', 2)],
    poolsDetail: null,
  }
}

function emptyAccounts(): AccountsBoard {
  return {
    page: { accounts: [], tenant_id: 'eng', unreadable_documents: [], unreadable_document_count: 0 } as AccountsPage,
    tenants: [],
    tenantsDetail: null,
    readAt: Date.now(),
  } as AccountsBoard
}

/** Every `real zero` drawn is an image with a non-empty name; return them. */
function realZeros(scope: HTMLElement): HTMLElement[] {
  const marks = within(scope).getAllByRole('img', { name: /\S/ }).filter((m) => m.textContent === 'real zero')
  for (const m of marks) expect(m.getAttribute('aria-label')?.trim().length ?? 0).toBeGreaterThan(20)
  return marks
}

/** No `.ctl-mark.is-zero` anywhere in the render is a bare, unnamed span. */
function expectEveryZeroNamed(): void {
  const zeros = [...document.querySelectorAll('.ctl-mark.is-zero')]
  expect(zeros.length, 'the render drew no real-zero mark at all').toBeGreaterThan(0)
  for (const z of zeros) {
    expect(z.getAttribute('role'), `a hand-drawn real zero: ${z.outerHTML}`).toBe('img')
    expect(z.getAttribute('aria-label')?.trim() ?? '', `an unnamed real zero: ${z.outerHTML}`).not.toBe('')
  }
}

afterEach(() => {
  window.history.replaceState(null, '', '/')
})

describe('the Accounts empty pool draws its real zero with Mark', () => {
  it('names the zero as the sentence of what was read', async () => {
    api.loadAccountsBoard.mockResolvedValue(ok(emptyAccounts()))
    render(<AccountsScreen />)
    const heading = await screen.findByText('No accounts registered', undefined, WAIT)
    const panel = heading.closest('.state') as HTMLElement
    const [mark] = realZeros(panel)
    expect(mark, 'the empty pool has no named real-zero mark').toBeTruthy()
    expect(mark!.getAttribute('aria-label')).toMatch(/no accounts/i)
    expectEveryZeroNamed()
  })
})

describe('Holders draws its real zeros with Mark', () => {
  it('names the drift card agreement over every live lease', async () => {
    api.loadHolders.mockResolvedValue(ok(holders()))
    render(<HoldersScreen />)
    await screen.findByText('Every holder', undefined, WAIT)
    const title = [...document.querySelectorAll('.ctl-card-title')].find((t) =>
      (t.textContent ?? '').startsWith('Accounting drift'),
    )
    const card = title!.closest('.ctl-card') as HTMLElement
    const [mark] = realZeros(card)
    expect(mark, 'the drift card has no named real-zero mark').toBeTruthy()
    expect(mark!.getAttribute('aria-label')).toMatch(/counter/i)
    expectEveryZeroNamed()
  })

  it('names the pool a filter found no lease holding', async () => {
    window.history.replaceState(null, '', `/capacity/holders?pool=${encodeURIComponent('resource:browser')}`)
    api.loadHolders.mockResolvedValue(ok(holders()))
    render(<HoldersScreen />)
    await screen.findByText('Every holder', undefined, WAIT)
    const row = document.querySelector('.hold-all tbody tr.hold-empty') as HTMLElement
    expect(row, 'the filtered table drew no empty row').toBeTruthy()
    const [mark] = realZeros(row)
    expect(mark, 'the empty pool row has no named real-zero mark').toBeTruthy()
    expect(mark!.getAttribute('aria-label')).toContain('resource:browser')
    expectEveryZeroNamed()
  })
})
