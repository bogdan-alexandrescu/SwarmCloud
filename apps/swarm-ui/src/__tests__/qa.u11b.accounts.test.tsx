/**
 * BROWSER QA U11b D24 (owner, 2026-10-04; live console at main 69416faf,
 * 1440x900): Accounts › History drew its Holder column 45px wide ("tas…")
 * with ~340px of the table unused. `max-width: 0` on an auto-layout cell
 * gave Holder no minimum, and the When bar's column took the slack. The
 * table is laid out fixed: Started, Held and Ended are sized to what they
 * hold, When is a share, and Holder takes the rest, cut with its whole text
 * as the cell's title.
 *
 * MUTATIONS: put `max-width: 0` back on an auto table, give When the slack,
 * or drop the Holder cell's title -- each turns a case red.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'

import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import { fixedColumns } from './tablefit'

import type { Result } from '../fetch'
import type { AccountsBoard } from '../api'
import type { Account, AccountsPage } from '../types'
import type { AccountHolders, HoldHistory } from '../Accounts'

const api = vi.hoisted(() => ({
  loadAccountsBoard: vi.fn(),
  beginAccountSignIn: vi.fn(),
  finishAccountSignIn: vi.fn(),
  refreshAccount: vi.fn(),
  removeAccount: vi.fn(),
  setAccountLending: vi.fn(),
  setAccountState: vi.fn(),
}))
vi.mock('../api', () => api)

const reads = vi.hoisted(() => ({ read: vi.fn() }))
vi.mock('../fetch', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../fetch')>()),
  read: reads.read,
}))

const { AccountsScreen } = await import('../Accounts')

const WAIT = { timeout: 5000 } as const

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-10-01T10:00:00Z' }
}

function account(over: Partial<Account> = {}): Account {
  return {
    account_id: 'eng:laptop',
    owner_tenant: 'eng',
    label: 'laptop',
    provider: 'anthropic',
    state: 'AVAILABLE',
    reason: '',
    lend_to: ['research'],
    assigned: 3,
    windows: {},
    observed_at: null,
    stale: false,
    unreadable_by: [],
    unreadable_now: [],
    last_assigned_at: null,
    ...over,
  }
}

function board(a: Account, tenant = 'eng'): AccountsBoard {
  return {
    page: {
      accounts: [a],
      tenant_id: tenant,
      unreadable_documents: [],
      unreadable_document_count: 0,
    } as AccountsPage,
    tenants: [],
    tenantsDetail: null,
    readAt: Date.now(),
  } as AccountsBoard
}

const MINUTES = 60_000

/** Answer each read by its URL, so the test says which route got what. */
function serve(byPath: { holders?: AccountHolders; history?: HoldHistory }) {
  reads.read.mockImplementation(async (target: { url: string }) => {
    if (target.url.includes('/holders')) return ok(byPath.holders)
    if (target.url.includes('/history')) return ok(byPath.history)
    throw new Error(`unexpected read ${target.url}`)
  })
}

async function openRow(a: Account, tenant = 'eng') {
  api.loadAccountsBoard.mockResolvedValue(ok(board(a, tenant)))
  render(<AccountsScreen />)
  // The list item, not any button naming the account: the pane opens the
  // first account on its own now (#503), and its controls name it too.
  await screen.findByText('Subscription accounts', undefined, WAIT)
  const open = document.querySelector<HTMLButtonElement>('.acct-li > button.acct-open')!
  if (open.getAttribute('aria-current') !== 'true') fireEvent.click(open)
}

const WIDE: CascadeEnv = { width: 1440 }

beforeEach(() => {
  reads.read.mockReset()
})

/** The account pane's content box at 1440: the work column less the 380px list and the pane's padding. */
const PANE = 1056 - 380 - 32
const TASK = 'task_7f3e2c9a1b8d4e6f0a5c'

describe('D24: History gives the Holder its room', () => {
  it('lays the table out fixed, Holder the widest column, cut with its whole text as the title', async () => {
    const at = new Date(Date.now() - 120 * MINUTES).toISOString()
    const until = new Date(Date.now() - 90 * MINUTES).toISOString()
    serve({
      history: {
        account_id: 'eng:laptop', viewer: 'owner',
        from: new Date(Date.now() - 7 * 1440 * MINUTES).toISOString(),
        to: new Date().toISOString(),
        spans: [{ since: at, until, end: 'released', mine: true, task_id: TASK, attempt: 1, recorded: true, verified: true }],
      },
    })
    await openRow(account())
    fireEvent.click(await screen.findByRole('tab', { name: 'History' }, WAIT))
    await screen.findByRole('link', { name: TASK }, WAIT)
    const table = document.querySelector<HTMLTableElement>('table.acct-hist')!
    expect(painted(table, 'table-layout', WIDE)).toBe('fixed')
    const { cols } = fixedColumns(table, PANE, WIDE)
    const w = (c: string) => {
      const b = cols.find((x) => x.col === c)!
      return b.end - b.start
    }
    for (const c of ['started', 'held', 'ended', 'when']) expect(w('holder'), c).toBeGreaterThan(w(c))
    expect(w('holder')).toBeGreaterThanOrEqual(200)
    expect(w('when')).toBeLessThanOrEqual(PANE * 0.3)
    const who = table.querySelector<HTMLElement>('tbody td.acct-hist-who')!
    expect(who.getAttribute('title')).toContain(TASK)
    expect(painted(who, 'text-overflow', WIDE)).toBe('ellipsis')
  })
})
