/**
 * BROWSER QA U10b (owner, 2026-10-04; live console at 1440x900): Capacity ›
 * Accounts › History.
 *
 *   D24  History was an unstyled list with browser bullets, and its bars were
 *        near empty: each was 8rem for a seven-day window, so a 30-minute hold
 *        was a 1px sliver. It is the picked table (components.html A: 32px
 *        rows, sentence-case heads, a 6px bar), the bar scaled to the holds
 *        shown, with the scale named in its head.
 *
 * MUTATIONS: render the `<ul>` again, or scale the bar to the whole window --
 * each turns a case red.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'

import type { CascadeEnv } from './cssgate'
import { painted } from './marks'

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

describe('D24: Accounts › History is the picked table', () => {
  it('draws one row per hold with sentence-case heads, 32px rows and a 6px bar scaled to the holds shown', async () => {
    const at = new Date(Date.now() - 120 * MINUTES).toISOString()
    const until = new Date(Date.now() - 90 * MINUTES).toISOString()
    serve({
      history: {
        account_id: 'eng:laptop', viewer: 'owner',
        from: new Date(Date.now() - 7 * 1440 * MINUTES).toISOString(),
        to: new Date().toISOString(),
        spans: [
          { since: at, until, end: 'released', mine: true, task_id: 'task_mine', attempt: 1, recorded: true, verified: true },
          { since: until, until: null, end: null, mine: true, recorded: false, verified: false },
        ],
      },
    })
    await openRow(account())
    fireEvent.click(await screen.findByRole('tab', { name: 'History' }, WAIT))
    await screen.findByRole('link', { name: 'task_mine' }, WAIT)
    expect(document.querySelector('ul.acct-spans'), 'the bulleted list is back').toBeNull()
    const table = document.querySelector<HTMLTableElement>('table.acct-hist')!
    expect(table).not.toBeNull()
    const heads = [...table.querySelectorAll('thead th')].map((th) => th.textContent ?? '')
    expect(heads[0]).toBe('Holder')
    for (const h of heads) expect(h, 'a head in capitals').not.toMatch(/^[A-Z ]{3,}$/)
    const rows = [...table.querySelectorAll('tbody tr')]
    expect(rows.length).toBe(2)
    expect(painted(rows[0]!.querySelector('td')!, 'height', WIDE)).toBe('32px')
    const bar = rows[0]!.querySelector<HTMLElement>('.acct-hist-bar')!
    expect(painted(bar, 'height', WIDE)).toBe('6px')
    // Scaled to the holds shown (two hours), the 30-minute hold is a quarter of the bar, not a 1px sliver.
    const fill = bar.querySelector<HTMLElement>('i')!
    expect(Number.parseFloat(fill.style.width)).toBeGreaterThan(20)
    expect(rows[0]!.textContent).toContain('30m')
    expect(rows[0]!.textContent).toContain('released')
    expect(rows[1]!.textContent).toContain('holding')
  })
})
