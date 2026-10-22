// THE ADD FORM NEVER SENDS A LABEL THE BROKER WOULD REFUSE (#1912).
//
// `AI-ENG-2` reached the broker, which refused it for its capitals, and the
// refusal came back as "a service the API depends on did not answer". Owner
// decision 2026-10-22, "Lowercase as typed, plus 400": the form lowercases the
// label as it is typed, refuses any other character, a leading or trailing
// dash and more than 40 characters inline with Start disabled, and a 422 the
// platform still sends is drawn as a refusal carrying its own sentence.
//
// The rule is the broker's `validate_label` (`quota_broker/accounts.py`
// `_LABEL`): `^[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?$`.

import { fireEvent, render, screen, waitFor } from '@testing-library/react'
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

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now(), serverAt: new Date().toISOString() }
}

function account(label: string): Account {
  return {
    account_id: `eng:${label}`,
    owner_tenant: 'eng',
    label,
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

/** An empty pool draws the form with no click: it is the one fix. */
async function mount(accounts: Account[] = []): Promise<HTMLInputElement> {
  api.loadAccountsBoard.mockResolvedValue(ok(board(accounts)))
  render(<AccountsScreen />)
  if (accounts.length > 0) {
    fireEvent.click(await screen.findByRole('button', { name: 'Add account' }, WAIT))
  }
  return (await screen.findByLabelText('label', undefined, WAIT)) as HTMLInputElement
}

function type(input: HTMLInputElement, value: string): void {
  fireEvent.change(input, { target: { value } })
}

function start(): HTMLButtonElement {
  return screen.getByRole('button', { name: 'Start the sign-in' }) as HTMLButtonElement
}

function preview(): string {
  return document.querySelector('.acct-add-form .acct-flags code')?.textContent ?? ''
}

function fault(): HTMLElement | null {
  return document.querySelector('.acct-label-fault')
}

function ruleLine(): HTMLElement {
  return document.querySelector('.acct-label-rule') as HTMLElement
}

describe('the label is lowercased as it is typed (#1912)', () => {
  it('turns AI-ENG-2 into ai-eng-2 in the input and in the preview', async () => {
    const input = await mount()
    type(input, 'AI-ENG-2')
    // MUTATION: drop `.toLowerCase()` from the label's onChange and the input keeps AI-ENG-2.
    expect(input.value).toBe('ai-eng-2')
    // MUTATION: render the preview from the raw typed value and it reads eng:AI-ENG-2.
    expect(preview()).toBe('eng:ai-eng-2')
    // MUTATION: count capitals as a fault after lowercasing and Start stays disabled.
    expect(fault()).toBeNull()
    expect(start().disabled).toBe(false)
  })

  it('sends the lowercased label', async () => {
    api.beginAccountSignIn.mockResolvedValue(
      ok({ authorize_url: 'https://claude.ai/oauth/authorize?x=1', state: 's'.repeat(43), expires_in_seconds: 900 }),
    )
    const input = await mount()
    type(input, 'AI-ENG-2')
    fireEvent.click(start())
    await waitFor(() => expect(api.beginAccountSignIn).toHaveBeenCalled(), WAIT)
    // MUTATION: send `label` before lowercasing it and the broker sees AI-ENG-2 again.
    expect(api.beginAccountSignIn.mock.calls.at(-1)?.[0]).toMatchObject({ label: 'ai-eng-2' })
  })

  it('compares the lowercased label when it warns that a label is taken', async () => {
    const input = await mount([account('ai-eng-2')])
    type(input, 'AI-ENG-2')
    // MUTATION: compare the typed value instead of the lowercased one and the REPLACES warning is lost.
    expect(screen.getByText('ai-eng-2 already exists in this pool.')).toBeTruthy()
  })
})

describe('a label the broker would refuse is refused at the form (#1912)', () => {
  const refused: Array<[string, string, RegExp]> = [
    ['an underscore', 'a_b', /"_" is not allowed/],
    ['a space', 'a b', /a space is not allowed/],
    ['a dot', 'a.b', /"\." is not allowed/],
    ['a leading dash', '-ab', /must start with a letter or a digit/],
    ['a trailing dash', 'ab-', /must end with a letter or a digit/],
    ['41 characters', 'a'.repeat(41), /41 characters; at most 40/],
  ]

  it.each(refused)('%s shows the fault in words and disables Start', async (_name, value, words) => {
    const input = await mount()
    type(input, value)
    // MUTATION: return null from `labelFault` for this case and no fault is drawn.
    expect(fault(), `no fault for ${value}`).not.toBeNull()
    expect(fault()!.textContent).toMatch(words)
    // MUTATION: drop `is-fault` from the rule line and the rule is not marked as the fault.
    expect(ruleLine().classList.contains('is-fault')).toBe(true)
    // MUTATION: drop `aria-invalid` from the label input and a screen reader is not told.
    expect(input.getAttribute('aria-invalid')).toBe('true')
    // MUTATION: drop `fault !== null` from Start's `disabled` and the refused label is sendable.
    expect(start().disabled).toBe(true)
  })

  it('never sends a refused label, even on Enter', async () => {
    const input = await mount()
    type(input, 'a_b')
    fireEvent.submit(input.closest('form')!)
    // MUTATION: drop the `fault !== null` backstop in `start` and Enter sends a_b.
    expect(api.beginAccountSignIn).not.toHaveBeenCalledWith(expect.objectContaining({ label: 'a_b' }))
  })

  it('enables Start for a label that fits, with no fault drawn', async () => {
    const input = await mount()
    type(input, 'a'.repeat(40))
    // MUTATION: make the length limit 39 and a 40-character label is refused.
    expect(fault()).toBeNull()
    expect(ruleLine().classList.contains('is-fault')).toBe(false)
    expect(input.getAttribute('aria-invalid')).toBeNull()
    expect(start().disabled).toBe(false)
  })
})

describe('a 422 from the platform is a refusal with its sentence (#1912)', () => {
  it('draws the broker\'s label refusal as a refusal, not an outage', async () => {
    const sentence =
      "account label 'x' must be lowercase letters, digits and dashes, starting and ending alphanumeric, at most 40 characters"
    api.beginAccountSignIn.mockResolvedValue({
      status: 'error',
      error: { kind: 'invalid', httpStatus: 422, code: 'validation_failed', message: sentence, method: 'POST' },
    })
    const input = await mount()
    type(input, 'x')
    fireEvent.click(start())
    const refusal = await screen.findByText('The platform refused this sign-in:', undefined, WAIT)
    // MUTATION: draw an `invalid` start failure through `SignInFailure` and this line is gone.
    expect(refusal.closest('.acct-refused')!.textContent).toContain(sentence)
    // MUTATION: route a 422 to the outage panel and it says a service did not answer.
    expect(document.body.textContent).not.toMatch(/did not answer/)
  })
})
