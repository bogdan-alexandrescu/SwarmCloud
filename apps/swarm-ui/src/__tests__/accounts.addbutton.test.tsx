// ACCOUNTS: THE TABLE FILLS THE SCREEN, AND EVERY COLUMN SAYS WHAT IT MEASURES
// (#127).
//
// Already true before this file, and pinned here: the table is titled
// `Subscription accounts` (it was `The pool`, which collided with the Pools
// tab), and its five column heads are still `cs status`'s -- renaming one would
// break that parity, so every addition below is INSIDE a cell, never a head.
//
// What this file added:
//   * the sign-in form sits behind an `Add account` button -- except when the
//     pool is empty (the form is the one control that fixes that) or a
//     sign-in is already under way (hiding it would hide a live link);
//   * the Clears cell names its window visibly (`5h window`), not only in a
//     `title=`;
//   * each label carries a faint `last given out` sub-line;
//   * the `~ is projected` legend is drawn only when a `~` is on screen, and
//     says `no projected figures` otherwise.
//
// BREAK IT: render AddAccount unconditionally again -- the first case finds
// the form before the click. Or drop the `.acct-clears-win` span -- the
// window name is back in a tooltip only.

import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { AccountsBoard } from '../api'
import type { Result } from '../fetch'
import type { Account, AccountsPage } from '../types'

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

const { AccountsScreen } = await import('../Accounts')

const WAIT = { timeout: 5000 } as const
const HOUR = 3_600_000

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: new Date().toISOString() }
}

function account(over: Partial<Account>): Account {
  return {
    account_id: 'eng:laptop',
    owner_tenant: 'eng',
    label: 'laptop',
    provider: 'anthropic-subscription',
    state: 'AVAILABLE',
    reason: '',
    lend_to: [],
    assigned: 0,
    windows: {},
    observed_at: null,
    stale: false,
    unreadable_by: [],
    unreadable_now: [],
    last_assigned_at: null,
    ...over,
  }
}

function board(accounts: Account[]): AccountsBoard {
  return {
    page: {
      accounts,
      tenant_id: 'eng',
      unreadable_documents: [],
      unreadable_document_count: 0,
    } as AccountsPage,
    tenants: [],
    tenantsDetail: null,
    readAt: Date.now(),
  } as AccountsBoard
}

/** Measured a minute ago: 5h at 80% binds, 7d at 10%. Nothing projected. */
function live(over: Partial<Account> = {}): Account {
  const now = Date.now()
  return account({
    observed_at: new Date(now - 60_000).toISOString(),
    last_assigned_at: new Date(now - 5 * 60_000).toISOString(),
    windows: {
      five_hour: { utilization: 0.8, resets_at: new Date(now + 2 * HOUR).toISOString(), reset: false },
      seven_day: { utilization: 0.1, resets_at: new Date(now + 90 * HOUR).toISOString(), reset: false },
    },
    ...over,
  })
}

async function mount(accounts: Account[]): Promise<void> {
  api.loadAccountsBoard.mockResolvedValue(ok(board(accounts)))
  render(<AccountsScreen />)
  await screen.findByText('Subscription accounts', { selector: 'h2' }, WAIT)
}

function rowOf(label: string): HTMLElement {
  const btn = [...document.querySelectorAll('.acct-open')].find((b) => b.textContent?.includes(label))
  expect(btn, `no row for ${label}`).toBeTruthy()
  return btn!.closest('tr') as HTMLElement
}

describe('the sign-in form is behind a control (#127)', () => {
  it('draws an Add account button and no form until it is pressed', async () => {
    await mount([live()])
    expect(screen.queryByText('Add an account', { selector: 'h2' })).toBeNull()
    const add = screen.getByRole('button', { name: 'Add account' })
    expect(add.getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(add)
    expect(screen.getByText('Add an account', { selector: 'h2' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Add account' }).getAttribute('aria-expanded')).toBe('true')
  })

  it('keeps the form open on an empty pool, where it is the only fix', async () => {
    api.loadAccountsBoard.mockResolvedValue(ok(board([])))
    render(<AccountsScreen />)
    await screen.findByText('No accounts registered', undefined, WAIT)
    expect(screen.getByText('Add an account', { selector: 'h2' })).toBeTruthy()
  })
})

describe('the table names itself and qualifies its columns (#127)', () => {
  it('is titled Subscription accounts and keeps cs status\'s five heads', async () => {
    await mount([live()])
    const title = document.querySelector('section.acct-list > h2')
    expect(title, 'the table card has no title').not.toBeNull()
    expect(title!.firstChild?.textContent?.trim()).toBe('Subscription accounts')
    const heads = [...document.querySelectorAll('table.accounts thead th')].map((th) => (th.textContent ?? '').trim())
    expect(heads).toEqual(['Account', '5h used', '7d used', 'Clears', 'State'])
  })

  it('says which window Clears counts down, in the cell', async () => {
    await mount([live()])
    const clears = rowOf('laptop').querySelector('td[data-label="Clears"]')!
    const win = clears.querySelector('.acct-clears-win')
    expect(win, 'the window is named only in a tooltip').not.toBeNull()
    expect(win!.textContent).toBe('5h window')
  })

  it('carries a last given out sub-line under each label, including never', async () => {
    await mount([live(), account({ account_id: 'eng:fresh', label: 'fresh' })])
    const given = rowOf('laptop').querySelector('.acct-given')
    expect(given, 'no last-given-out sub-line').not.toBeNull()
    expect(given!.textContent).toMatch(/^given out .+ ago$/)
    expect(rowOf('fresh').querySelector('.acct-given')!.textContent).toBe('never given out')
  })
})

describe('the ~ legend is drawn only when a ~ is (#127)', () => {
  it('says there are no projected figures when every reading is current', async () => {
    await mount([live()])
    const foot = document.querySelector('.provenance')!.textContent ?? ''
    expect(document.querySelector('.acct-tilde')).toBeNull()
    expect(foot).not.toContain('~ is projected')
    expect(foot).toContain('no projected figures')
  })

  it('keeps the legend when a projected figure is on screen', async () => {
    await mount([live({ stale: true })])
    expect(document.querySelector('.acct-tilde')).not.toBeNull()
    expect(document.querySelector('.provenance')!.textContent).toContain('~ is projected, not measured')
  })
})
