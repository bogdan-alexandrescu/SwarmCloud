// WHO HOLDS AN ACCOUNT, AND WHO HELD IT (#379 parts 2 and 3).
//
// swarm-api decides what each viewer may see (swarm_api/accountholds.py); this
// screen renders what arrives and must not blur the cases it is handed:
//
//   * a hold whose worker named NO task reads "task not recorded" -- not as an
//     unverified one, and not as a blank;
//   * a hold that named a task its tenant does not own reads "unverified",
//     with no id to show;
//   * other tenants' holds are a count ("+1 held by other tenants") for a
//     borrower and "N agents · tenant" for the owner -- never a task;
//   * an empty history says so, rather than rendering an empty list that
//     looks like a load that never finished.
//
// The panel is COLLAPSED and loads nothing until asked: a row opening must not
// cost broker reads nobody wanted. That is asserted too.

import { describe, expect, it, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'

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

function holders(over: Partial<AccountHolders>): AccountHolders {
  return { account_id: 'eng:laptop', viewer: 'owner', total: 0, holders: [], others: 0, ...over }
}

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
  const open = await screen.findByRole('button', { name: /laptop/ }, WAIT)
  if (open.getAttribute('aria-expanded') !== 'true') fireEvent.click(open)
}

beforeEach(() => {
  reads.read.mockReset()
})

describe('holding now', () => {
  it('loads nothing until the tab is asked for', async () => {
    serve({ holders: holders({}) })
    await openRow(account())

    expect(await screen.findByRole('tab', { name: 'Holding now (3)' }, WAIT)).toBeTruthy()
    expect(reads.read).not.toHaveBeenCalled()
  })

  it('links the own task, its attempt and since when, and says "task not recorded" apart from it', async () => {
    const since = new Date(Date.now() - 38 * MINUTES).toISOString()
    serve({
      holders: holders({
        total: 2,
        holders: [
          { since, task_id: 'task_abc', attempt: 2, recorded: true, verified: true },
          { since, recorded: false, verified: false },
        ],
      }),
    })
    await openRow(account())
    fireEvent.click(await screen.findByRole('tab', { name: 'Holding now (3)' }, WAIT))

    const link = await screen.findByRole('link', { name: 'task_abc' }, WAIT)
    expect(link.getAttribute('href')).toBe('#work/task/task_abc')
    expect(screen.getByText(/attempt 2/)).toBeTruthy()
    expect(screen.getAllByText(/since \d\d:\d\d \(38m\)/).length).toBe(2)
    expect(screen.getByText('task not recorded')).toBeTruthy()
    // NOT RECORDED IS NOT UNVERIFIED. A regression that folds the two would
    // tell the reader a worker lied when it merely did not say.
    expect(screen.queryByText('unverified')).toBeNull()
  })

  it('marks a task its tenant does not own as unverified, with no id to show', async () => {
    serve({
      holders: holders({
        total: 1,
        holders: [{ since: new Date().toISOString(), recorded: true, verified: false }],
      }),
    })
    await openRow(account())
    fireEvent.click(await screen.findByRole('tab', { name: /Holding now/ }, WAIT))

    expect(await screen.findByText('unverified', undefined, WAIT)).toBeTruthy()
    expect(screen.queryByRole('link', { name: /task/ })).toBeNull()
    expect(screen.queryByText('task not recorded')).toBeNull()
  })

  it('gives a borrower a count of other tenants and no tenant name', async () => {
    serve({ holders: holders({ viewer: 'borrower', total: 1, others: 1 }) })
    await openRow(account({ owner_tenant: 'eng' }), 'research')
    fireEvent.click(await screen.findByRole('tab', { name: /Holding now/ }, WAIT))

    expect(await screen.findByText('+1 held by other tenants', undefined, WAIT)).toBeTruthy()
  })

  it('gives the owner its borrowers by tenant and count', async () => {
    serve({
      holders: holders({ total: 2, others: 2, by_tenant: [{ tenant: 'research', n: 2 }] }),
    })
    await openRow(account())
    fireEvent.click(await screen.findByRole('tab', { name: /Holding now/ }, WAIT))

    const row = await screen.findByText(/2 agents ·/, undefined, WAIT)
    expect(row.textContent).toContain('research')
    // The by-tenant line replaces the anonymous count; both would double it.
    expect(screen.queryByText(/held by other tenants/)).toBeNull()
  })
})

describe('history', () => {
  it('says an empty window is empty', async () => {
    serve({
      history: {
        account_id: 'eng:laptop', viewer: 'owner', from: null, to: null,
        spans: [], next_cursor: null,
      },
    })
    await openRow(account())
    fireEvent.click(await screen.findByRole('tab', { name: 'History' }, WAIT))

    expect(await screen.findByText('No holds recorded in this window.', undefined, WAIT)).toBeTruthy()
  })

  it('links own spans and counts everyone else, with no other tenant span', async () => {
    const at = new Date(Date.now() - 120 * MINUTES).toISOString()
    const until = new Date(Date.now() - 90 * MINUTES).toISOString()
    serve({
      history: {
        account_id: 'eng:laptop', viewer: 'borrower',
        from: new Date(Date.now() - 7 * 1440 * MINUTES).toISOString(),
        to: new Date().toISOString(),
        spans: [
          { since: at, until, end: 'released', mine: true, task_id: 'task_mine', attempt: 1, recorded: true, verified: true },
          { since: at, until: null, end: null, mine: true, recorded: false, verified: false },
        ],
        others: 3,
        next_cursor: '2026-09-30T12:00:00+00:00|1',
      },
    })
    await openRow(account(), 'research')
    fireEvent.click(await screen.findByRole('tab', { name: 'History' }, WAIT))

    expect(await screen.findByRole('link', { name: 'task_mine' }, WAIT)).toBeTruthy()
    expect(screen.getByText('3 other agents in this window')).toBeTruthy()
    expect(screen.queryByText('another tenant')).toBeNull()
    expect(screen.getByText('task not recorded')).toBeTruthy()
    expect(screen.getByText(/for 30m · released/)).toBeTruthy()
    // Exactly the two own spans are listed; the others are a count, not rows.
    expect(document.querySelectorAll('.acct-spans li').length).toBe(2)

    fireEvent.click(screen.getByRole('button', { name: 'Older' }))
    await screen.findByText('3 other agents in this window', undefined, WAIT)
    const urls = reads.read.mock.calls.map((c) => (c[0] as { url: string }).url)
    expect(urls.some((u) => u.includes('cursor=2026-09-30T12'))).toBe(true)
  })

  it('never shows another tenant\'s times to a borrower, only the count', async () => {
    const at = new Date(Date.now() - 120 * MINUTES).toISOString()
    const until = new Date(Date.now() - 90 * MINUTES).toISOString()
    serve({
      history: {
        account_id: 'eng:laptop', viewer: 'borrower',
        from: new Date(Date.now() - 7 * 1440 * MINUTES).toISOString(),
        to: new Date().toISOString(),
        spans: [
          { since: at, until, end: 'released', mine: true, task_id: 'task_mine', attempt: 1, recorded: true, verified: true },
        ],
        others: 1,
      },
    })
    await openRow(account(), 'research')
    fireEvent.click(await screen.findByRole('tab', { name: 'History' }, WAIT))

    expect(await screen.findByText('1 other agent in this window', undefined, WAIT)).toBeTruthy()
    expect(document.querySelectorAll('.acct-spans li').length).toBe(1)
    expect(screen.queryByRole('button', { name: 'Older' })).toBeNull()
  })

  it('says a borrower scan was limited, with no Older button', async () => {
    serve({
      history: {
        account_id: 'eng:laptop', viewer: 'borrower',
        from: null, to: null,
        spans: [], others: 40, scan_limited: true,
      },
    })
    await openRow(account(), 'research')
    fireEvent.click(await screen.findByRole('tab', { name: 'History' }, WAIT))

    expect(await screen.findByText(/Older history was not searched further/, undefined, WAIT)).toBeTruthy()
    expect(screen.getByText('40 other agents in this window')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Older' })).toBeNull()
  })
})
