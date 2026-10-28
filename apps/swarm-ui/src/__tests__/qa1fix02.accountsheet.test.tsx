// VISUAL QA PASS 1, LANE QA1-FIX-02 (part of #2596): the account sheet's
// keyboard and its crumb, as the cap sweep lane found them on the fixture
// console on 2026-10-28.
//
//   * capacity-07: the desktop sheet dims the page like a modal, so it is a
//     dialog (role=dialog, aria-modal) and Tab wraps at its ends instead of
//     walking to the Reads dock and the rail behind the scrim;
//   * capacity-08: `Change the cap` puts focus in `Agents at once`, and Cancel
//     and Save hand it back to `Change the cap` -- Save across the reload that
//     remounts the screen -- not to <body> or the sheet's Close;
//   * capacity-22: the crumb names the account after its chevron.
//
// MUTATION: drop `trapTab` from Accounts.tsx's keydown handler -- the wrap
// cases find focus left on the last and first stops. Drop `role`/`aria-modal`
// from the sheet's <aside> -- the dialog case finds no dialog. Drop
// `field.current?.focus()` or `change.current?.focus()` in AgentCap -- the cap
// cases find <body>. Clear `capFocusBack` before the remount (or never set it)
// -- the Save case finds the sheet's Close. Put back `'Accounts ›'` alone --
// the crumb case reads nothing after the chevron.

import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { AccountsBoard } from '../api'
import type { Result } from '../fetch'
import type { Account, AccountsPage } from '../types'
import type { PoolAccount } from '../accountsPool'
import { tabStops } from '../focus'

const api = vi.hoisted(() => ({
  loadAccountsBoard: vi.fn(),
  beginAccountSignIn: vi.fn(),
  finishAccountSignIn: vi.fn(),
  refreshAccount: vi.fn(),
  removeAccount: vi.fn(),
  setAccountLending: vi.fn(),
  setAccountState: vi.fn(),
  setAccountMaxAgents: vi.fn(),
  loadTask: vi.fn(),
}))
vi.mock('../api', () => api)

const reads = vi.hoisted(() => ({ read: vi.fn() }))
vi.mock('../fetch', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../fetch')>()),
  read: reads.read,
}))

const { AccountsScreen } = await import('../Accounts')

const WAIT = { timeout: 5000 } as const
const HOUR = 3_600_000
const DAY = 24 * HOUR

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: new Date().toISOString() }
}

function account(over: Partial<PoolAccount> = {}): PoolAccount {
  const now = Date.now()
  return {
    account_id: 'eng:primary',
    owner_tenant: 'eng',
    label: 'primary',
    provider: 'anthropic',
    state: 'AVAILABLE',
    reason: '',
    lend_to: [],
    assigned: 0,
    observed_at: new Date(now - 5 * 60_000).toISOString(),
    stale: false,
    unreadable_by: [],
    unreadable_now: [],
    last_assigned_at: new Date(now - 26 * 60_000).toISOString(),
    windows: {
      five_hour: { utilization: 0.4, resets_at: new Date(now + HOUR).toISOString(), reset: false },
      seven_day: { utilization: 0.3, resets_at: new Date(now + 3 * DAY).toISOString(), reset: false },
    },
    max_agents: 6,
    can_take_new_agent: true,
    blocked_by: [],
    next_eligible_at: null,
    organisation: { name: 'Example', plan: 'Team' },
    ...over,
  }
}

async function open(): Promise<HTMLElement> {
  api.loadAccountsBoard.mockResolvedValue(
    ok({
      page: { accounts: [account()] as Account[], tenant_id: 'eng', unreadable_documents: [], unreadable_document_count: 0 } as AccountsPage,
      tenants: [],
      tenantsDetail: null,
      readAt: Date.now(),
    } as AccountsBoard),
  )
  render(<AccountsScreen />)
  await screen.findByText('Subscription accounts', { selector: 'h2' }, WAIT)
  fireEvent.click(document.querySelector<HTMLButtonElement>('button.acct-open[data-account="eng:primary"]')!)
  return document.querySelector<HTMLElement>('.acct-sheet')!
}

const sheet = () => document.querySelector<HTMLElement>('.acct-sheet')!
const changeCap = () => screen.getByRole('button', { name: 'Change the cap' })

beforeEach(() => {
  api.loadAccountsBoard.mockReset()
  api.setAccountMaxAgents.mockReset()
  reads.read.mockReset()
  reads.read.mockResolvedValue(ok({ account_id: 'eng:primary', viewer: 'owner', total: 0, holders: [], others: 0 }))
})

describe('capacity-07: the desktop sheet is a dialog and holds Tab', () => {
  it('is announced as a modal dialog named for its account', async () => {
    await open()
    const d = screen.getByRole('dialog', { name: 'Account primary' })
    expect(d).toBe(sheet())
    expect(d.getAttribute('aria-modal')).toBe('true')
  })

  it('wraps Tab from its last stop to its first, and Shift+Tab from its first to its last', async () => {
    await open()
    const stops = tabStops(sheet())
    expect(stops.length, 'the sheet has no tab stops to hold').toBeGreaterThan(2)
    const first = stops[0]!
    const last = stops[stops.length - 1]!
    last.focus()
    fireEvent.keyDown(last, { key: 'Tab' })
    expect(document.activeElement, 'Tab left the sheet from its last stop').toBe(first)
    fireEvent.keyDown(first, { key: 'Tab', shiftKey: true })
    expect(document.activeElement, 'Shift+Tab left the sheet from its first stop').toBe(last)
  })
})

describe('capacity-08: the cap editor takes focus and gives it back', () => {
  it('focuses Agents at once on open, and Change the cap on Cancel', async () => {
    await open()
    fireEvent.click(changeCap())
    expect(document.activeElement).toBe(screen.getByRole('spinbutton', { name: 'Agents at once' }))
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(document.activeElement).toBe(changeCap())
  })

  it('hands focus back to Change the cap after Save, across the reload that remounts the sheet', async () => {
    api.setAccountMaxAgents.mockResolvedValue(ok({}))
    await open()
    fireEvent.click(changeCap())
    fireEvent.change(screen.getByRole('spinbutton', { name: 'Agents at once' }), { target: { value: '4' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save cap' }))
    await waitFor(() => expect(api.loadAccountsBoard.mock.calls.length).toBeGreaterThanOrEqual(2), WAIT)
    await waitFor(() => expect(document.activeElement).toBe(changeCap()), WAIT)
    expect(document.activeElement?.classList.contains('acct-sheet-close')).toBe(false)
  })
})

describe('capacity-22: the crumb names the account', () => {
  it('reads Accounts › primary, not a chevron and nothing', async () => {
    await open()
    expect(sheet().querySelector('.acct-sheet-crumb')!.textContent).toBe('Accounts › primary')
  })
})
