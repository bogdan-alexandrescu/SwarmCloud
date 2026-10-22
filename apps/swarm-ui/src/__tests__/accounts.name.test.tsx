// AN ACCOUNT CAN BE RENAMED (#1913; owner decision 2026-10-22, BGD,
// "Editable display name").
//
// An account's label is its id (`<owner>:<label>`), built into its Secret
// Manager secret name and a Kubernetes annotation, so it cannot change. What
// changes is a NAME: display text, shown wherever the console names the
// account in place of its label. The id stays printed where it was, so the
// Name never hides which account it is. An account with no Name shows its
// label exactly as before. Rename, in the account's pane, edits the Name in
// place and saves at once; clearing the field removes it.
//
// And, from the same pane (owner decision 2026-10-22, "Danger outline
// unarmed"; part of #1680): Remove, unarmed or disabled, is drawn as a danger
// OUTLINE -- the danger ink on its border and its words, no fill -- and once
// armed it is the danger fill.

import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { AccountsBoard } from '../api'
import type { Result } from '../fetch'
import type { Account, AccountsPage } from '../types'
import type { CascadeEnv } from './cssgate'
import { THEMES, painted } from './marks'

const api = vi.hoisted(() => ({
  loadAccountsBoard: vi.fn(),
  beginAccountSignIn: vi.fn(),
  finishAccountSignIn: vi.fn(),
  refreshAccount: vi.fn(),
  removeAccount: vi.fn(),
  setAccountLending: vi.fn(),
  setAccountModelClasses: vi.fn(),
  setAccountName: vi.fn(),
  setAccountState: vi.fn(),
  loadTask: vi.fn(async () => ({
    status: 'error',
    error: { kind: 'not_found', httpStatus: 404, code: 'not_found', message: 'not read in this test' },
  })),
}))
vi.mock('../api', async (importOriginal) => ({ ...(await importOriginal<typeof import('../api')>()), ...api }))

const { AccountsScreen } = await import('../Accounts')
const { accountName } = await import('../accountName')
const { deriveChecks } = await import('../checks')

const WAIT = { timeout: 5000 } as const
const WIDE: CascadeEnv = { width: 1440 }

afterEach(() => {
  cleanup()
  window.history.replaceState(null, '', '/')
  document.body.innerHTML = ''
  vi.clearAllMocks()
  vi.unstubAllGlobals()
  vi.unstubAllEnvs()
})

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: '2026-10-22T02:00:00Z' }
}

function account(over: Partial<Account> = {}): Account {
  return {
    account_id: 'eng:ai-eng-2',
    owner_tenant: 'eng',
    label: 'ai-eng-2',
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
    page: { accounts, tenant_id: 'eng', unreadable_documents: [], unreadable_document_count: 0 } as AccountsPage,
    tenants: [{ tenant_id: 'eng' }],
    tenantsDetail: null,
    readAt: Date.now(),
  } as AccountsBoard
}

/** Mount the screen with `chosen` open in the pane; the pane's region. */
async function mount(accounts: Account[], chosen: string, shown: string): Promise<HTMLElement> {
  window.history.replaceState(null, '', `/capacity/accounts?account=${encodeURIComponent(chosen)}`)
  api.loadAccountsBoard.mockResolvedValue(ok(board(accounts)))
  render(<AccountsScreen />)
  return screen.findByRole('region', { name: `Account ${shown}` }, WAIT)
}

/** The list row for `id`: its heading and the id printed under it. */
function row(id: string): { name: string; id: string } {
  const open = document.querySelector<HTMLElement>(`.acct-open[data-account="${id}"]`)
  expect(open, `no row for ${id}`).toBeTruthy()
  const li = open!.closest('li')!
  return {
    name: li.querySelector('.acct-li-name')!.textContent ?? '',
    id: li.querySelector('.acct-li-meta .raw')!.textContent ?? '',
  }
}

describe('the Name is shown in place of the label, with the id still printed', () => {
  it('shows a named account by its Name on its row and in its pane', async () => {
    const pane = await mount([account({ display_name: 'AI Eng 2' })], 'eng:ai-eng-2', 'AI Eng 2')
    // MUTATION: print `account.label` in `.acct-li-name` (Accounts.tsx
    // AcctItem) -- the row reads `ai-eng-2` after the owner renamed it.
    expect(row('eng:ai-eng-2').name).toBe('AI Eng 2')
    // MUTATION: print the Name in place of the id under the heading -- which
    // account it is is no longer on the row.
    expect(row('eng:ai-eng-2').id).toBe('eng:ai-eng-2')
    // MUTATION: print `account.label` in the pane's <h3> (Accounts.tsx Detail).
    expect(pane.querySelector('.acct-dhead h3')!.textContent).toBe('AI Eng 2')
    // MUTATION: drop the id from the pane's subtitle (`.acct-idt`).
    expect(pane.querySelector('.acct-idt')!.textContent).toContain('eng:ai-eng-2')
  })

  it('shows an unnamed account by its label, exactly as before', async () => {
    const pane = await mount([account({ display_name: null })], 'eng:ai-eng-2', 'ai-eng-2')
    // MUTATION: make `accountName` return '' for a missing Name -- the row's
    // heading is empty.
    expect(row('eng:ai-eng-2').name).toBe('ai-eng-2')
    expect(pane.querySelector('.acct-dhead h3')!.textContent).toBe('ai-eng-2')
  })

  it('reads an absent, blank or null Name as no Name at all', () => {
    // MUTATION: return `a.display_name ?? a.label` -- a blank Name prints as
    // an empty heading.
    expect(accountName({ label: 'ai-eng-2', display_name: '   ' })).toBe('ai-eng-2')
    expect(accountName({ label: 'ai-eng-2', display_name: null })).toBe('ai-eng-2')
    expect(accountName({ label: 'ai-eng-2' })).toBe('ai-eng-2')
    expect(accountName({ label: 'ai-eng-2', display_name: 'AI Eng 2' })).toBe('AI Eng 2')
  })
})

describe('the Overview names an account by its Name too', () => {
  it("puts the Name in the Accounts check's detail", () => {
    const loading = { status: 'loading', since: 0 } as const
    const checks = deriveChecks(
      {
        capacity: loading,
        tasks: loading,
        leases: loading,
        providers: loading,
        stats: loading,
        workflows: loading,
        accounts: ok({
          accounts: [account({ state: 'REAUTH_REQUIRED', display_name: 'AI Eng 2' })],
          tenant_id: 'eng',
        } as AccountsPage),
      },
      Date.now(),
    )
    const accounts = checks.find((c) => c.label === 'Accounts')!
    expect(accounts.status).toBe('found')
    const detail = accounts.status === 'found' ? accounts.problems.map((p) => p.detail).join(' ') : ''
    // MUTATION: name accounts by `a.label` in checks.ts `accountCheck` -- the
    // Overview's check says `ai-eng-2` after the owner renamed it.
    expect(detail).toContain('AI Eng 2')
  })
})

describe('Rename in the pane', () => {
  it('saves a Name at once and shows it after the reload', async () => {
    api.setAccountName.mockResolvedValue(ok({ account: account({ display_name: 'AI Eng 2' }) }))
    const pane = await mount([account()], 'eng:ai-eng-2', 'ai-eng-2')
    // MUTATION: drop the Rename button (Accounts.tsx Rename) -- no way to name it.
    fireEvent.click(within(pane).getByRole('button', { name: 'Rename' }))
    const input = within(pane).getByRole('textbox', { name: 'Name for eng:ai-eng-2' }) as HTMLInputElement
    // Keyboard reachable: the field takes focus when the editor opens.
    // MUTATION: drop `autoFocus` from the Name field.
    expect(document.activeElement).toBe(input)
    fireEvent.change(input, { target: { value: 'AI Eng 2' } })
    api.loadAccountsBoard.mockResolvedValue(ok(board([account({ display_name: 'AI Eng 2' })])))
    fireEvent.click(within(pane).getByRole('button', { name: 'Save name' }))
    // MUTATION: send `account.label` or the untyped draft -- the call differs.
    await waitFor(() => expect(api.setAccountName).toHaveBeenCalledWith('eng:ai-eng-2', 'AI Eng 2'), WAIT)
    // MUTATION: drop `reload()` after a saved Name -- the old label stays up.
    await screen.findByRole('region', { name: 'Account AI Eng 2' }, WAIT)
    expect(row('eng:ai-eng-2').name).toBe('AI Eng 2')
  })

  it('saves on Enter and cancels on Escape without sending anything', async () => {
    api.setAccountName.mockResolvedValue(ok({ account: account({ display_name: 'Typed' }) }))
    const pane = await mount([account()], 'eng:ai-eng-2', 'ai-eng-2')
    fireEvent.click(within(pane).getByRole('button', { name: 'Rename' }))
    const input = within(pane).getByRole('textbox', { name: 'Name for eng:ai-eng-2' })
    fireEvent.change(input, { target: { value: 'Abandoned' } })
    fireEvent.keyDown(input, { key: 'Escape' })
    // MUTATION: drop the Escape handler -- the editor stays open.
    expect(within(pane).queryByRole('textbox', { name: 'Name for eng:ai-eng-2' })).toBeNull()
    expect(api.setAccountName).not.toHaveBeenCalled()

    fireEvent.click(within(pane).getByRole('button', { name: 'Rename' }))
    const again = within(pane).getByRole('textbox', { name: 'Name for eng:ai-eng-2' })
    fireEvent.change(again, { target: { value: 'Typed' } })
    // MUTATION: make the editor a <div> rather than a <form> -- Enter sends nothing.
    fireEvent.submit(again.closest('form')!)
    await waitFor(() => expect(api.setAccountName).toHaveBeenCalledWith('eng:ai-eng-2', 'Typed'), WAIT)
  })

  it('clears the Name when the field is emptied, and the label shows again', async () => {
    api.setAccountName.mockResolvedValue(ok({ account: account({ display_name: null }) }))
    const pane = await mount([account({ display_name: 'AI Eng 2' })], 'eng:ai-eng-2', 'AI Eng 2')
    fireEvent.click(within(pane).getByRole('button', { name: 'Rename' }))
    const input = within(pane).getByRole('textbox', { name: 'Name for eng:ai-eng-2' }) as HTMLInputElement
    // The editor opens on the current Name, not on an empty field.
    expect(input.value).toBe('AI Eng 2')
    fireEvent.change(input, { target: { value: '   ' } })
    api.loadAccountsBoard.mockResolvedValue(ok(board([account({ display_name: null })])))
    fireEvent.click(within(pane).getByRole('button', { name: 'Save name' }))
    // MUTATION: send '' for a cleared field -- the broker refuses an empty
    // Name, so a Name could never be removed.
    await waitFor(() => expect(api.setAccountName).toHaveBeenCalledWith('eng:ai-eng-2', null), WAIT)
    await screen.findByRole('region', { name: 'Account ai-eng-2' }, WAIT)
    expect(row('eng:ai-eng-2').name).toBe('ai-eng-2')
  })

  it('draws a refusal in its own words and keeps the editor open', async () => {
    const words = "an account's Name is at most 60 characters once trimmed; this one is 61."
    api.setAccountName.mockResolvedValue({
      status: 'error',
      error: { kind: 'invalid', httpStatus: 422, code: 'validation_failed', message: words },
    })
    const pane = await mount([account()], 'eng:ai-eng-2', 'ai-eng-2')
    fireEvent.click(within(pane).getByRole('button', { name: 'Rename' }))
    fireEvent.change(within(pane).getByRole('textbox', { name: 'Name for eng:ai-eng-2' }), {
      target: { value: 'x'.repeat(61) },
    })
    fireEvent.click(within(pane).getByRole('button', { name: 'Save name' }))
    const alert = await within(pane).findByRole('alert', undefined, WAIT)
    // MUTATION: draw a generic "could not save" in place of `error.message`.
    expect(alert.textContent).toContain(words)
    expect(alert.textContent).toContain('The Name was not changed.')
    // MUTATION: close the editor on a refusal -- the typed Name is lost.
    expect(within(pane).getByRole('textbox', { name: 'Name for eng:ai-eng-2' })).toBeTruthy()
  })

  it('offers a borrower no Rename', async () => {
    const lent = account({ account_id: 'other:lent', owner_tenant: 'other', label: 'lent', lend_to: ['eng'] })
    const pane = await mount([lent], 'other:lent', 'lent')
    // MUTATION: draw Rename on every account -- the broker refuses the
    // borrower, after the console offered it.
    expect(within(pane).queryByRole('button', { name: 'Rename' })).toBeNull()
  })
})

describe('Remove is a danger outline until it is armed (part of #1680, box comment 6314115845)', () => {
  it('draws the unarmed button in the danger ink with no fill, and the armed one filled', async () => {
    const pane = await mount([account()], 'eng:ai-eng-2', 'ai-eng-2')
    const btn = within(pane).getByRole('button', { name: 'remove ai-eng-2' }) as HTMLButtonElement
    expect(btn.disabled).toBe(true)
    for (const theme of THEMES) {
      const env = { ...WIDE, theme }
      // MUTATION: draw the unarmed button `secondary` again (Accounts.tsx
      // Remove) -- `.c-btn:disabled` paints `--line` on `--surface-2`.
      expect(painted(btn, ['border-color', 'border'], env)).toBe('var(--s-bad)')
      // MUTATION: drop the `.c-btn.is-danger:disabled` rule in
      // styles/capacity.css -- the system's disabled `--surface-2` fill wins.
      expect(painted(btn, ['background', 'background-color'], env)).toBe('transparent')
      // MUTATION: leave the words in `--text-dim` -- the outline reads as a
      // red box around an off label rather than a danger control.
      expect(painted(btn, 'color', env)).toBe('var(--s-bad)')
    }

    fireEvent.change(within(pane).getByRole('textbox', { name: 'Type ai-eng-2 to confirm removal' }), {
      target: { value: 'ai-eng-2' },
    })
    const armed = within(pane).getByRole('button', { name: 'remove ai-eng-2' }) as HTMLButtonElement
    expect(armed.disabled).toBe(false)
    // MUTATION: keep the outline once armed -- the one irreversible control
    // loses its fill when a click would fire it.
    expect(painted(armed, ['background', 'background-color'], WIDE)).toBe('var(--s-bad)')
  })
})
