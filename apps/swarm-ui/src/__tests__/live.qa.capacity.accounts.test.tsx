// LIVE QA, CAPACITY › ACCOUNTS (2026-10-11, swarm.example.com).
//
//   CAP-1   `move to paused` / `move to draining` (Pause and Drain since #2222)
//           wrote on the first click.
//           A state button now opens a confirm that names the account and its
//           agents, and pausing or draining needs a reason.
//   CAP-2   `save lending` sent any free text, on the first click. Ids the
//           tenant list does not hold are refused before anything is sent,
//           and a valid change is confirmed first.
//   CAP-3   A 5h window the API says has reset was drawn `~92%` with a 92%
//           bar. It reads `reset`, says what it was, and draws no bar.
//   CAP-11  Add account had no Cancel, ignored Esc and left focus on <body>.
//   CAP-12  `?account=<id not in the read>` showed another account silently.
//   CAP-13  `Last given out` from yesterday read as a bare clock time.
//
// BREAK IT: call `setAccountState` from the state button's onClick; drop the
// `unknownTenants` check from the lending button; draw the reset reading
// through the projected branch again; compare `givenAt` on 24 hours instead
// of the calendar day -- each case below fails.

import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { AccountsBoard } from '../api'
import type { Account, AccountsPage, Tenant } from '../types'

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

const { AccountsScreen, givenAt, stateNeedsReason, unknownTenants } = await import('../Accounts')

const WAIT = { timeout: 5000 } as const

afterEach(() => {
  // Unmount first: a dialog is portalled to <body>, and clearing <body> under
  // a mounted portal leaves React nothing to remove.
  cleanup()
  window.history.replaceState(null, '', '/')
  document.body.innerHTML = ''
  vi.clearAllMocks()
})

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-10-11T12:00:00Z' }
}

function account(over: Partial<Account>): Account {
  return {
    account_id: 'eng:alice-personal',
    owner_tenant: 'eng',
    label: 'alice-personal',
    provider: 'anthropic-subscription',
    state: 'AVAILABLE',
    reason: '',
    lend_to: [],
    assigned: 7,
    windows: {},
    observed_at: null,
    stale: false,
    unreadable_by: [],
    unreadable_now: [],
    last_assigned_at: null,
    ...over,
  }
}

function board(accounts: Account[], tenants: Tenant[] | null = [{ tenant_id: 'eng' }, { tenant_id: 'smoke' }] as Tenant[]): AccountsBoard {
  return {
    page: { accounts, tenant_id: 'eng', unreadable_documents: [], unreadable_document_count: 0 } as AccountsPage,
    tenants,
    tenantsDetail: tenants === null ? 'not read in this test' : null,
    readAt: Date.now(),
  } as AccountsBoard
}

async function mount(accounts: Account[], tenants?: Tenant[] | null): Promise<void> {
  api.loadAccountsBoard.mockResolvedValue(ok(tenants === undefined ? board(accounts) : board(accounts, tenants)))
  render(<AccountsScreen />)
  await screen.findAllByText(accounts[0]!.account_id, undefined, WAIT)
}

/** Open the first account's sheet (#2222): its controls live there. */
function openFirst(): void {
  fireEvent.click(document.querySelector<HTMLButtonElement>('button.acct-open')!)
}

describe('CAP-1: a state change is confirmed before it is written', () => {
  it('opens a confirm naming the account and its agents, and writes nothing on the first click', async () => {
    api.setAccountState.mockResolvedValue(ok({}))
    await mount([account({})])
    openFirst()
    fireEvent.click(await screen.findByRole('button', { name: 'Drain' }, WAIT))
    expect(api.setAccountState, 'the first click wrote').not.toHaveBeenCalled()
    const dialog = await screen.findByRole('dialog', undefined, WAIT)
    expect(dialog.textContent).toContain('eng:alice-personal')
    expect(dialog.textContent).toMatch(/7 agents on it now will be moved off/)
  })

  it('refuses to pause or drain with no reason, and writes the reason it was given', async () => {
    api.setAccountState.mockResolvedValue(ok({}))
    await mount([account({})])
    openFirst()
    fireEvent.click(await screen.findByRole('button', { name: 'Pause' }, WAIT))
    const dialog = await screen.findByRole('dialog', undefined, WAIT)
    const verb = within(dialog).getByRole('button', { name: 'Pause' })
    expect((verb as HTMLButtonElement).disabled, 'pause with an empty reason is armed').toBe(true)
    fireEvent.click(verb)
    expect(api.setAccountState).not.toHaveBeenCalled()
    fireEvent.change(within(dialog).getByRole('textbox'), { target: { value: 'held for the release' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Pause' }))
    await waitFor(() => expect(api.setAccountState).toHaveBeenCalledWith('eng:alice-personal', 'PAUSED', 'held for the release'), WAIT)
  })

  it('writes nothing when the confirm is dismissed', async () => {
    await mount([account({})])
    openFirst()
    fireEvent.click(await screen.findByRole('button', { name: 'Drain' }, WAIT))
    const dialog = await screen.findByRole('dialog', undefined, WAIT)
    fireEvent.click(within(dialog).getByRole('button', { name: /^Leave it/ }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull(), WAIT)
    expect(api.setAccountState).not.toHaveBeenCalled()
  })

  it('needs a reason to take an account out of rotation, and none to put it back', () => {
    expect(stateNeedsReason('PAUSED')).toBe(true)
    expect(stateNeedsReason('DRAINING')).toBe(true)
    expect(stateNeedsReason('AVAILABLE')).toBe(false)
  })
})

describe('CAP-2: lending is checked against the tenants and confirmed', () => {
  it('refuses an id the tenant list does not hold, before anything is sent', async () => {
    await mount([account({})])
    openFirst()
    fireEvent.click(screen.getByRole('button', { name: 'Edit lending' }))
    const field = await screen.findByRole('combobox', { name: /is lent to$/ }, WAIT)
    fireEvent.change(field, { target: { value: 'no-such-tenant, BAD ID!' } })
    const save = screen.getByRole('button', { name: 'Save lending' }) as HTMLButtonElement
    expect(save.disabled, 'an unknown tenant armed the save').toBe(true)
    fireEvent.click(save)
    expect(api.setAccountLending).not.toHaveBeenCalled()
    expect(screen.getByRole('alert').textContent).toContain('no-such-tenant')
  })

  it('confirms a valid loan naming the tenant, and sends only from the confirm', async () => {
    api.setAccountLending.mockResolvedValue(ok({}))
    await mount([account({})])
    openFirst()
    fireEvent.click(screen.getByRole('button', { name: 'Edit lending' }))
    fireEvent.change(await screen.findByRole('combobox', { name: /is lent to$/ }, WAIT), { target: { value: 'smoke' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save lending' }))
    expect(api.setAccountLending, 'the first click lent the account').not.toHaveBeenCalled()
    const dialog = await screen.findByRole('dialog', undefined, WAIT)
    expect(dialog.querySelector('h2')!.textContent).toBe('Lend alice-personal to smoke?')
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save lending' }))
    await waitFor(() => expect(api.setAccountLending).toHaveBeenCalledWith('eng:alice-personal', ['smoke']), WAIT)
  })

  it('checks the id shape when the tenant list was not read, and keeps an id already lent to', () => {
    expect(unknownTenants(['smoke', 'BAD', 'ID!'], null, [])).toEqual(['BAD', 'ID!'])
    expect(unknownTenants(['smoke', 'gone'], [{ tenant_id: 'smoke' }], ['gone'])).toEqual([])
    expect(unknownTenants(['nope'], [{ tenant_id: 'smoke' }], [])).toEqual(['nope'])
  })
})

describe('CAP-3: a window that has reset has no current figure', () => {
  const RESET = account({
    account_id: 'eng:aieng-claude1',
    label: 'aieng-claude1',
    state: 'PAUSED',
    observed_at: new Date(Date.now() - 5 * 3_600_000).toISOString(),
    windows: { five_hour: { utilization: 0.92, resets_at: new Date(Date.now() - 5 * 3_600_000).toISOString(), reset: true } },
  })

  it('says reset with no current figure, no bar and no tilde, in the table and the sheet', async () => {
    await mount([RESET])
    // The ranked table (#2222): reset, when, and nothing read since -- never the old figure, never a fill.
    const cell = document.querySelector<HTMLElement>('tr.acct-rank-row .acct-fig')!
    expect(cell.textContent).toMatch(/^5-hour reset .+Znothing read since$/)
    expect(cell.textContent).not.toContain('92')
    expect(cell.querySelector('.ctl-util-fill'), 'a fill for a window that refilled').toBeNull()
    // The sheet's window says the same, in the same words (#2222, F4).
    openFirst()
    const win = document.querySelector<HTMLElement>('.acct-sheet .acct-ss-wins > .acct-win')!
    expect(win.textContent).toMatch(/^5-hour reset .+Znothing read since$/)
    expect(win.textContent).not.toContain('92')
    expect(win.querySelector('.ctl-util-fill'), 'a fill for a window that refilled').toBeNull()
    expect(win.querySelector('.acct-tilde'), 'a reset window is drawn as a projection').toBeNull()
    // No `~` on screen, so the foot does not explain one.
    expect(document.querySelector('.provenance')!.textContent).toContain('no projected figures')
  })
})

describe('CAP-11: the add form can be closed, and focus follows it', () => {
  it('focuses the label field on open, and Esc and Cancel close it back to the button', async () => {
    await mount([account({})])
    // An account is open in the sheet (#2222), so the form has something to give way to.
    openFirst()
    const add = await screen.findByRole('button', { name: /Add account/ }, WAIT)
    fireEvent.click(add)
    const label = await screen.findByRole('textbox', { name: 'label' }, WAIT)
    await waitFor(() => expect(document.activeElement).toBe(label), WAIT)
    // One thing in the sheet: the account's detail steps aside for the form.
    expect(document.querySelector('.acct-pane-detail')).toBeNull()
    fireEvent.keyDown(label, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('textbox', { name: 'label' })).toBeNull(), WAIT)
    expect(document.activeElement).toBe(add)
    // Escape closed the form, not the sheet under it.
    expect(document.querySelector('.acct-pane-detail')).not.toBeNull()
    fireEvent.click(add)
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel' }, WAIT))
    await waitFor(() => expect(screen.queryByRole('textbox', { name: 'label' })).toBeNull(), WAIT)
  })

  it('returns focus to the row the back button left', async () => {
    await mount([account({}), account({ account_id: 'eng:devops-team', label: 'devops-team' })])
    const row = document.querySelector<HTMLButtonElement>('.acct-open[data-account="eng:devops-team"]')!
    fireEvent.click(row)
    fireEvent.click(document.querySelector<HTMLButtonElement>('.acct-back')!)
    await waitFor(() => expect((document.activeElement as HTMLElement | null)?.dataset.account).toBe('eng:devops-team'), WAIT)
  })
})

describe('CAP-12: a link to an account this read does not hold says so', () => {
  it('names the missing id', async () => {
    window.history.replaceState(null, '', `/capacity/accounts?account=${encodeURIComponent('eng:nope')}`)
    await mount([account({})])
    const note = await waitFor(() => {
      const n = document.querySelector('.acct-missing')
      expect(n).not.toBeNull()
      return n!
    }, WAIT)
    expect(note.textContent).toContain('eng:nope')
  })

  it('says nothing when the link names an account that is there', async () => {
    window.history.replaceState(null, '', `/capacity/accounts?account=${encodeURIComponent('eng:alice-personal')}`)
    await mount([account({})])
    expect(document.querySelector('.acct-missing')).toBeNull()
  })
})

describe('CAP-13: a hand-out on another day carries its date', () => {
  it('prints the date for yesterday evening, even under 24 hours ago', () => {
    // UTC since VQ-035: the day is the UTC day its Z names.
    const now = Date.UTC(2026, 9, 11, 5, 30)
    const yesterday = new Date(Date.UTC(2026, 9, 10, 20, 0)).toISOString()
    expect(givenAt(yesterday, now)).not.toMatch(/^\d\d:\d\dZ$/)
    expect(givenAt(yesterday, now)).toBe('Oct 10 20:00Z')
    expect(givenAt(new Date(Date.UTC(2026, 9, 11, 4, 0)).toISOString(), now)).toBe('04:00Z')
  })
})
