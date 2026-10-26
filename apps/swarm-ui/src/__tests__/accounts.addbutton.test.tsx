// ACCOUNTS: THE LIST FILLS THE SCREEN, AND EVERY FIGURE SAYS WHAT IT MEASURES
// (#127).
//
// Pinned here: the list is titled `Subscription accounts` (it was `The pool`,
// which collided with the Pools tab). It was `cs status`'s five-column table
// until A1 (#503) made it a compact list beside the chosen account; the five
// facts are still there, as the item's 5h figure and clears line and the
// pane's 5h / 7d / Clears tiles, under the same words.
//
// What this file added:
//   * the sign-in form sits behind an `Add account` button -- except when the
//     pool is empty (the form is the one control that fixes that) or a
//     sign-in is already under way (hiding it would hide a live link);
//   * Clears names its window visibly (`5h`, `5h window`), not only in a
//     `title=`;
//   * each label carries a faint `last given out` sub-line;
//   * the `~ is projected` legend is drawn only when a `~` is on screen, and
//     says `no projected figures` otherwise.
//
// BREAK IT: render AddAccount unconditionally again -- the first case finds
// the form before the click. Or drop the `.acct-clears-win` span -- the
// window name is back in a tooltip only.

import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, onTestFinished, vi } from 'vitest'

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
  const btn = [...document.querySelectorAll('button.acct-open')].find((b) => b.textContent?.includes(label))
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

describe('the list names itself and qualifies its figures (#127, #503)', () => {
  it('is titled Subscription accounts, and carries cs status\'s facts under its words', async () => {
    await mount([live()])
    const title = document.querySelector('section.acct-list .acct-list-head > h2')
    expect(title, 'the list has no title').not.toBeNull()
    expect(title!.firstChild?.textContent?.trim()).toBe('Subscription accounts')
    // #2222: the ranked table names each window at its figure, and so does
    // the opened account's sheet, each window under its own word.
    expect(document.querySelector('table.accounts')).toBeNull()
    expect([...rowOf('laptop').querySelectorAll('.acct-fig b')].map((b) => b.textContent)).toEqual(['5-hour', '7-day'])
    fireEvent.click(rowOf('laptop').querySelector('button.acct-open')!)
    const wins = [...document.querySelectorAll('.acct-sheet .acct-ss-wins .acct-win-l > b')].map((t) => t.textContent)
    expect(wins).toEqual(['5-hour', '7-day'])
  })

  it('says when a blocked account clears, from the broker, in its row and its sheet', async () => {
    // Only Date is pinned, to midday UTC: run after 22:00Z, a reset two hours
    // out falls on the next day and is drawn with its date ("Oct 26 01:41Z"),
    // which the bare `HH:MMZ` reading below does not match.
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date('2026-10-25T12:00:00Z'))
    onTestFinished(() => {
      vi.useRealTimers()
    })
    const at = new Date(Date.now() + 2 * HOUR).toISOString()
    await mount([
      live({
        can_take_new_agent: false,
        blocked_by: [{ window: 'five_hour', used: 0.9, floor: 0.15, resets_at: at, aged_reading: false }],
        next_eligible_at: at,
      } as Partial<Account>),
    ])
    expect(rowOf('laptop').querySelector('.acct-clr')).not.toBeNull()
    fireEvent.click(rowOf('laptop').querySelector('button.acct-open')!)
    const lines = [...document.querySelectorAll('.acct-sheet .acct-sv-why')].map((p) => p.textContent ?? '')
    expect(lines.some((l) => /^Clears: \d\d:\d\dZ · in (1h 5\dm|2h)/.test(l)), lines.join(' | ')).toBe(true)
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
    const now = Date.now()
    const old = live({
      stale: true,
      observed_at: new Date(now - 2 * HOUR).toISOString(),
      windows: {
        ...live().windows,
        // A window with no column, read stale: its figure is drawn with `~`.
        harbor_lantern: { utilization: 0.4, resets_at: new Date(now + 9 * HOUR).toISOString(), reset: false },
      },
    })
    await mount([old])
    // The table draws an old reading pale with its age, never a `~`.
    expect(document.querySelector('.provenance')!.textContent).toContain('no projected figures')
    fireEvent.click(rowOf('laptop').querySelector('button.acct-open')!)
    expect(document.querySelector('.acct-extra-windows')!.textContent).toContain('~40% used')
    expect(document.querySelector('.provenance')!.textContent).toContain('~ is an old reading, not current')
  })
})
